import os
import re
import json
import fitz  # PyMuPDF for PDF
import docx
import spacy
import pandas as pd
from tqdm import tqdm
from typing import List, Dict
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer
from azure.ai.contentsafety import ContentSafetyClient
from azure.ai.contentsafety.models import AnalyzeTextOptions
from azure.core.credentials import AzureKeyCredential
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv
from langsmith import traceable
 
# Load environment variables
load_dotenv()
AZURE_KEY = os.getenv("AZURE_CS_KEY")
AZURE_ENDPOINT = os.getenv("AZURE_CS_ENDPOINT")

# Azure Blob Storage configuration
AZURE_BLOB_CONN_STR = os.getenv("AZURE_BLOB_CONN_STR")
AZURE_BLOB_CONTAINER = os.getenv("AZURE_BLOB_CONTAINER")

# Constants
SOURCE_FOLDER = "source_documents"
COLLECTION_NAME = "rag_collection"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 50
MIN_CHAR_COUNT = 300
MIN_WORD_COUNT = 40

# Initialize services
nlp = spacy.load("en_core_web_sm")
embedder = SentenceTransformer("all-MiniLM-L6-v2")

# Initialize Qdrant client with error handling
try:
    qdrant = QdrantClient(
        url=os.environ.get("QDRANT_URL"),
        api_key=os.environ.get("QDRANT_API_KEY")
    )
    print("✅ Qdrant client initialized")
except Exception as e:
    print(f"⚠️ Qdrant client init failed: {e}")
    qdrant = None

# Initialize Azure Blob Storage client
try:
    blob_service_client = BlobServiceClient.from_connection_string(AZURE_BLOB_CONN_STR)
    container_client = blob_service_client.get_container_client(AZURE_BLOB_CONTAINER)
    print("✅ Azure Blob Storage client initialized")
except Exception as e:
    print(f"⚠️ Azure Blob Storage init failed: {e}")
    blob_service_client = None
    container_client = None

try:
    safety_client = ContentSafetyClient(endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY))
except Exception as e:
    print(f"⚠️ Azure Content Safety init failed: {e}")
    safety_client = None

# ---------- BLOB STORAGE FUNCTIONS ----------

def download_files_from_blob():
    """Download all files from Azure Blob Storage to local source_documents folder"""
    if not container_client:
        print("⚠️ Blob Storage not configured, skipping download")
        return {}
    
    # Create source folder if it doesn't exist
    os.makedirs(SOURCE_FOLDER, exist_ok=True)
    
    try:
        print(f"📥 Downloading files from blob container: {AZURE_BLOB_CONTAINER}")
        blob_list = container_client.list_blobs()
        downloaded_count = 0
        blob_path_mapping = {}  # Track blob paths for metadata
        
        for blob in blob_list:
            local_file_path = os.path.join(SOURCE_FOLDER, blob.name)
            
            # Create subdirectories if blob name contains path separators
            local_dir = os.path.dirname(local_file_path)
            if local_dir and local_dir != SOURCE_FOLDER:
                os.makedirs(local_dir, exist_ok=True)
            
            print(f"📥 Downloading: {blob.name}")
            
            try:
                with open(local_file_path, "wb") as download_file:
                    blob_client = container_client.get_blob_client(blob.name)
                    download_file.write(blob_client.download_blob().readall())
                downloaded_count += 1
                
                # Store mapping of local path to Azure blob path
                azure_blob_url = f"https://{blob_service_client.account_name}.blob.core.windows.net/{AZURE_BLOB_CONTAINER}/{blob.name}"
                blob_path_mapping[local_file_path] = {
                    "blob_name": blob.name,
                    "azure_url": azure_blob_url,
                    "container": AZURE_BLOB_CONTAINER
                }
                
            except Exception as e:
                print(f"❌ Failed to download {blob.name}: {e}")
                
        print(f"✅ Downloaded {downloaded_count} files to {SOURCE_FOLDER}")
        return blob_path_mapping
        
    except Exception as e:
        print(f"❌ Error downloading from blob storage: {e}")
        return {}

def upload_file_to_blob(local_file_path: str, blob_name: str = None):
    """Upload a single file to Azure Blob Storage"""
    if not container_client:
        print("⚠️ Blob Storage not configured, skipping upload")
        return False
    
    if not blob_name:
        blob_name = os.path.basename(local_file_path)
    
    try:
        print(f"📤 Uploading {local_file_path} as {blob_name}")
        with open(local_file_path, "rb") as data:
            blob_client = container_client.get_blob_client(blob_name)
            blob_client.upload_blob(data, overwrite=True)
        print(f"✅ Uploaded {blob_name} to blob storage")
        return True
    except Exception as e:
        print(f"❌ Failed to upload {local_file_path}: {e}")
        return False

def list_blob_files():
    """List all files in the blob container"""
    if not container_client:
        print("⚠️ Blob Storage not configured")
        return []
    
    try:
        blob_list = container_client.list_blobs()
        files = [blob.name for blob in blob_list]
        print(f"📋 Found {len(files)} files in blob storage:")
        for file in files:
            print(f"   - {file}")
        return files
    except Exception as e:
        print(f"❌ Error listing blob files: {e}")
        return []

# ---------- TEXT CLEANUP ----------

def clean_lines(lines: List[str]) -> List[str]:
    """
    Enhanced text cleanup to remove TOC, headers, footers, watermarks, and noise.
    Removes spaced dots, author names, book titles, and repetitive content.
    """
    cleaned = []
    author_names = set()  # Track potential author names for filtering
    book_titles = set()   # Track potential book titles for filtering
    
    # First pass: identify potential author names and book titles
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Detect potential author names (short lines with proper case, often alone)
        if (len(line.split()) <= 3 and 
            re.match(r'^[A-Z][a-z]+(?: [A-Z][a-z]+)*$', line) and
            len(line) < 50):
            author_names.add(line)
            
        # Detect potential book titles (all caps, short lines)
        if (line.isupper() and 
            len(line.split()) <= 8 and 
            len(line) < 80 and
            not re.search(r'\d', line)):  # No numbers in title
            book_titles.add(line)
    
    # Second pass: clean lines with enhanced filtering
    for line in lines:
        line = line.strip()
        
        # Skip empty lines
        if not line:
            continue
            
        # Skip very short lines (likely noise)
        if len(line) <= 4:
            continue
            
        # Enhanced TOC detection - catches various dot patterns
        if (re.search(r'\.{3,}', line) or  # 3+ consecutive dots
            re.search(r'(\. ){3,}', line) or  # spaced dots like ". . . ."
            re.search(r'(\.\.+ ){2,}', line) or  # multiple dot groups
            re.search(r'\.\s*\.\s*\.\s*\.', line)):  # flexible spaced dots
            continue
            
        # Skip lines that are mostly dots and spaces
        if re.match(r'^[\.\s]+$', line):
            continue
            
        # Skip TOC-style lines with dots and page numbers
        if re.search(r'\.+\s*\d+\s*$', line):
            continue
            
        # Skip page numbers and page indicators
        if re.match(r'^Page\s*\d+$', line, re.IGNORECASE):
            continue
        if re.match(r'^\d+\s*$', line):  # Standalone numbers
            continue
        if re.match(r'^-\s*\d+\s*-$', line):  # Page numbers like "- 15 -"
            continue
            
        # Skip chapter/section numbers only
        if re.match(r'^(Chapter|Section|Part)\s+\d+\s*$', line, re.IGNORECASE):
            continue
            
        # Skip headers/footers that are repeated author names
        if line in author_names and len(line.split()) <= 3:
            continue
            
        # Skip headers/footers that are repeated book titles
        if line in book_titles:
            continue
            
        # Skip copyright and publication info
        if re.search(r'©|\(c\)|copyright|all rights reserved', line, re.IGNORECASE):
            continue
        if re.search(r'published by|publisher|publication|isbn', line, re.IGNORECASE):
            continue
            
        # Skip lines with only special characters and numbers
        if re.match(r'^[\W\d\s]+$', line) and len(line) < 20:
            continue
            
        # Skip repetitive header/footer patterns
        if (len(line) < 50 and 
            (line.count('_') > len(line) // 3 or 
             line.count('-') > len(line) // 3 or
             line.count('=') > len(line) // 3)):
            continue
            
        # Skip URLs and email addresses
        if re.search(r'http[s]?://|www\.|@.*\.com', line, re.IGNORECASE):
            continue
            
        # Skip lines that look like table headers (mostly capital letters)
        if (len(line) > 10 and len(line.split()) > 1 and
            len([c for c in line if c.isupper()]) / len([c for c in line if c.isalpha()]) > 0.7):
            # But keep if it's a proper sentence
            if not re.search(r'\. |^[A-Z][a-z].*[a-z]$', line):
                continue
                
        # Skip lines with excessive spacing (often formatting artifacts)
        if line.count(' ') > len(line) // 2:
            continue
            
        # Skip bibliography/reference patterns
        if re.search(r'^\[\d+\]|^References?$|^Bibliography$', line, re.IGNORECASE):
            continue
            
        # Must have some alphabetic content
        if not re.search(r'[a-zA-Z]', line):
            continue
            
        # Must not be just a number or roman numeral
        if re.match(r'^[IVXLCDM\d\s\.\-]+$', line):
            continue
            
        # Check for meaningful word content
        words = line.split()
        if len(words) < 2:  # Single words are often headers/noise
            continue
            
        # Skip if too many short words (likely formatting artifacts)
        short_words = [w for w in words if len(w) <= 2]
        if len(short_words) > len(words) // 2:
            continue
            
        # If we get here, it's likely meaningful content
        cleaned.append(line)
    
    return cleaned

# ---------- HEADING DETECTION ----------

def detect_headings(lines: List[str]) -> List[tuple]:
    """
    Enhanced heading detection with better filtering for actual content sections.
    """
    headings = []
    for i, line in enumerate(lines):
        # Skip very short lines that are likely noise
        if len(line.split()) < 2:
            continue
            
        # Traditional heading patterns
        is_heading = False
        
        # All caps headings (but not too long)
        if (line.isupper() and 
            2 <= len(line.split()) <= 10 and 
            len(line) <= 100):
            is_heading = True
            
        # Title case headings
        elif (re.match(r"^[A-Z][\w\s\-:]{5,80}$", line) and
              not re.search(r'\.|,|;', line) and  # No punctuation (except colons)
              len(line.split()) >= 2):
            is_heading = True
            
        # Numbered headings
        elif re.match(r"^\d+[\.\)]\s+[A-Z]", line):
            is_heading = True
            
        # Section/Chapter headings
        elif re.match(r"^(Chapter|Section|Part|Appendix)\s+", line, re.IGNORECASE):
            is_heading = True
            
        if is_heading:
            headings.append((i, line.strip()))
            
    return headings

# ---------- FILE READERS ----------

def read_pdf(file_path: str) -> List[str]:
    doc = fitz.open(file_path)
    return [line for page in doc for line in page.get_text().split('\n')]

def read_docx(file_path: str) -> List[str]:
    doc = docx.Document(file_path)
    return [para.text for para in doc.paragraphs if para.text.strip()]

def read_txt(file_path: str) -> List[str]:
    with open(file_path, "r", encoding="utf-8") as f:
        return f.readlines()

def read_xlsx(file_path: str, blob_info: dict = None) -> List[Dict]:
    xls = pd.ExcelFile(file_path)
    sections = []
    
    # Use Azure blob path if available, otherwise local path
    azure_path = blob_info["azure_url"] if blob_info else file_path
    blob_name = blob_info["blob_name"] if blob_info else os.path.basename(file_path)
    container_name = blob_info["container"] if blob_info else "local"
    
    for sheet in xls.sheet_names:
        content = xls.parse(sheet).to_string()
        sections.append({
            "section_title": sheet,
            "section_path": sheet,
            "content": content,
            "file_path": azure_path,  # Now using Azure blob URL
            "blob_name": blob_name,
            "container": container_name,
            "document": os.path.basename(file_path)
        })
    return sections

# ---------- SECTION EXTRACTION ----------

def is_meaningful_section(content: str, title: str) -> bool:
    """
    Check if a section contains meaningful content and not just TOC/noise.
    """
    # Skip if content is too short
    if len(content.split()) < 20:
        return False
    
    # Check if section is mostly TOC content (lots of dots and numbers)
    dot_count = content.count('.')
    space_count = content.count(' ')
    if dot_count > len(content) // 10:  # More than 10% dots
        return False
    
    # Check for excessive spacing (TOC artifact)
    if space_count > len(content) // 3:
        return False
    
    # Check if it's mostly page references
    lines = content.split('\n')
    page_ref_lines = 0
    for line in lines:
        if re.search(r'\d+\s*$', line.strip()) or re.search(r'\.{3,}', line):
            page_ref_lines += 1
    
    if page_ref_lines > len(lines) // 2:  # More than half are page references
        return False
    
    # Check if title suggests it's TOC
    if re.search(r'table\s+of\s+contents|contents|index', title, re.IGNORECASE):
        return False
    
    return True

def extract_sections(lines: List[str], headings: List[tuple], file_path: str, document_name: str, blob_info: dict = None) -> List[Dict]:
    sections = []
    for idx, (line_no, title) in enumerate(headings):
        start = line_no + 1
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        
        # Enhanced filtering
        if len(content.split()) < 10:
            continue
            
        # Check if section is meaningful
        if not is_meaningful_section(content, title):
            continue
        
        # Use Azure blob path if available, otherwise local path
        azure_path = blob_info["azure_url"] if blob_info else file_path
        blob_name = blob_info["blob_name"] if blob_info else document_name
        container_name = blob_info["container"] if blob_info else "local"
            
        sections.append({
            "section_title": title,
            "section_path": title,
            "content": content,
            "file_path": azure_path,  # Now using Azure blob URL
            "blob_name": blob_name,
            "container": container_name,
            "document": document_name
        })
    return sections

# ---------- CHUNKING ----------

def spacy_sentence_chunking(text: str, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP) -> List[str]:
    """
    Split text into overlapping sentence-aware chunks using spaCy.
    """
    doc = nlp(text)
    sentences = [sent.text.strip() for sent in doc.sents if sent.text.strip()]
    chunks, current = [], ""

    for sentence in sentences:
        if len(current) + len(sentence) <= chunk_size:
            current += " " + sentence
        else:
            chunks.append(current.strip())
            current = sentence

    if current:
        chunks.append(current.strip())

    # Add overlap
    overlapped = []
    for i in range(len(chunks)):
        if i == 0:
            overlapped.append(chunks[i])
        else:
            overlap_chunk = chunks[i - 1][-overlap:] + " " + chunks[i]
            overlapped.append(overlap_chunk.strip())

    return overlapped

# ---------- AZURE SAFETY ----------

def is_chunk_safe(text: str) -> bool:
    if not safety_client:
        return True
    try:
        response = safety_client.analyze_text(AnalyzeTextOptions(text=text))
        for category in response.categories_analysis:
            if category.severity >= 3:
                return False
        return True
    except Exception as e:
        print(f"⚠️ Azure Safety check failed: {e}")
        return True

# ---------- MAIN PROCESSOR ----------

def process_file(file_path: str, blob_info: dict = None) -> List[Dict]:
    ext = os.path.splitext(file_path)[1].lower()
    filename = os.path.basename(file_path)

    if ext == ".pdf":
        lines = clean_lines(read_pdf(file_path))
        headings = detect_headings(lines)
        return extract_sections(lines, headings, file_path, filename, blob_info)
    elif ext == ".docx":
        lines = clean_lines(read_docx(file_path))
        headings = detect_headings(lines)
        return extract_sections(lines, headings, file_path, filename, blob_info)
    elif ext == ".txt":
        lines = clean_lines(read_txt(file_path))
        headings = detect_headings(lines)
        return extract_sections(lines, headings, file_path, filename, blob_info)
    elif ext in [".xlsx", ".xls"]:
        return read_xlsx(file_path, blob_info)
    else:
        print(f"⚠️ Unsupported file: {file_path}")
        return []

# ---------- CHUNK BUILDER ----------
@traceable
def build_chunks(sections: List[Dict]) -> List[Dict]:
    print("🔨 Building chunks...")
    all_chunks = []
    for sec_idx, section in enumerate(sections):
        chunks = spacy_sentence_chunking(section["content"])
        for i, chunk in enumerate(chunks):
            # Enhanced chunk validation
            if len(chunk) < MIN_CHAR_COUNT or len(chunk.split()) < MIN_WORD_COUNT:
                continue
                
            # Skip chunks that are mostly dots (TOC remnants)
            if chunk.count('.') > len(chunk) // 15:  # More than ~6.7% dots
                continue
                
            # Skip chunks with excessive repetitive patterns
            if (chunk.count('_') > len(chunk) // 10 or 
                chunk.count('-') > len(chunk) // 10 or
                chunk.count('=') > len(chunk) // 10):
                continue
                
            # Must contain meaningful sentences
            sentences = chunk.split('.')
            meaningful_sentences = [s for s in sentences if len(s.split()) >= 4]
            if len(meaningful_sentences) < 1:
                continue
                
            all_chunks.append({
                "document": section["document"],
                "file_path": section["file_path"],  # This is now the Azure blob URL
                "blob_name": section.get("blob_name", section["document"]),
                "container": section.get("container", "unknown"),
                "section_title": section["section_title"],
                "section_path": section["section_path"],
                "chunk_index": f"{sec_idx}_{i}",
                "content": chunk,
                "char_count": len(chunk),
                "word_count": len(chunk.split())
            })
    return all_chunks

# ---------- QDRANT INGEST ----------

def test_qdrant_connection():
    """Test Qdrant connection and return status"""
    if not qdrant:
        print("❌ Qdrant client not initialized")
        return False
    
    try:
        collections = qdrant.get_collections()
        print(f"✅ Qdrant connection successful. Found {len(collections.collections)} collections")
        return True
    except Exception as e:
        print(f"❌ Qdrant connection failed: {e}")
        return False

@traceable
def ingest_chunks_to_qdrant(chunks: List[Dict]):
    if not chunks:
        print("⚠️ No chunks to embed.")
        return
    
    if not qdrant:
        print("❌ Qdrant client not available. Cannot ingest chunks.")
        return
    
    # Test connection first
    if not test_qdrant_connection():
        print("❌ Cannot connect to Qdrant. Aborting ingestion.")
        return
    
    texts = [c["content"] for c in chunks]
    print("🧬 Generating embeddings...")
    vectors = embedder.encode(texts, batch_size=32, show_progress_bar=True)

    print(f"📦 Recreating collection '{COLLECTION_NAME}' in Qdrant...")
    qdrant.recreate_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={"size": vectors.shape[1], "distance": "Cosine"}
    )
    
    print("🚀 Uploading vectors to Qdrant...")
    qdrant.upload_collection(
        collection_name=COLLECTION_NAME,
        vectors=vectors,
        payload=chunks
    )
    print(f"✅ Uploaded {len(chunks)} vectors to Qdrant.")

# ---------- MAIN ----------

def main():
    print("🚀 Starting Ingestion Pipeline...")
    
    # First, download files from blob storage and get path mapping
    print("📥 Step 1: Downloading files from Azure Blob Storage...")
    blob_path_mapping = download_files_from_blob()
    
    # Check if source folder exists and has files
    if not os.path.exists(SOURCE_FOLDER):
        print(f"❌ Source folder '{SOURCE_FOLDER}' not found!")
        return
    
    files_in_folder = [f for f in os.listdir(SOURCE_FOLDER) 
                      if os.path.isfile(os.path.join(SOURCE_FOLDER, f))]
    
    if not files_in_folder:
        print(f"⚠️ No files found in '{SOURCE_FOLDER}' folder!")
        return
    
    print(f"📁 Found {len(files_in_folder)} files to process")
    
    # Process all files with blob path information
    all_sections = []
    for file in files_in_folder:
        file_path = os.path.join(SOURCE_FOLDER, file)
        print(f"📄 Processing: {file}")
        
        # Get blob info for this file
        blob_info = blob_path_mapping.get(file_path)
        
        sections = process_file(file_path, blob_info)
        all_sections.extend(sections)

    print(f"📚 Total extracted sections: {len(all_sections)}")

    if not all_sections:
        print("⚠️ No sections extracted from files!")
        return

    chunks = build_chunks(all_sections)
    print(f"🧩 Total chunks before safety: {len(chunks)}")

    safe_chunks = [c for c in tqdm(chunks, desc="🔍 Azure Safety") if is_chunk_safe(c["content"])]
    print(f"✅ Total safe chunks: {len(safe_chunks)}")

    # Directly ingest to Qdrant
    if safe_chunks:
        ingest_chunks_to_qdrant(safe_chunks)
    else:
        print("⚠️ No safe chunks to ingest!")

if __name__ == "__main__":
    main()