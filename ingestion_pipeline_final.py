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
from dotenv import load_dotenv
#from langchain.callbacks.tracers import LangChainTracer
from langsmith import traceable
 
#tracer = LangChainTracer(project_name="RAG Ingestion Pipeline")
 
# Load environment variables
load_dotenv()
AZURE_KEY = os.getenv("AZURE_CS_KEY")
AZURE_ENDPOINT = os.getenv("AZURE_CS_ENDPOINT")

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
# qdrant = QdrantClient("localhost", port=6333)
qdrant = QdrantClient(
    url=os.environ.get("qdrant-url"),
    api_key=os.environ.get("qdrant-api-key")
)

print(qdrant.get_collections())

try:
    safety_client = ContentSafetyClient(endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY))
except Exception as e:
    print(f"⚠️ Azure Content Safety init failed: {e}")
    safety_client = None

# ---------- TEXT CLEANUP ----------

def clean_lines(lines: List[str]) -> List[str]:
    """
    Remove TOC-like lines, headers, footers, and noise from extracted text.
    """
    cleaned = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.search(r'\.{5,}', line):  # TOC dot pattern
            continue
        if re.match(r'^Page\s*\d+$', line, re.IGNORECASE):
            continue
        if len(line) <= 4:
            continue
        cleaned.append(line)
    return cleaned

# ---------- HEADING DETECTION ----------

def detect_headings(lines: List[str]) -> List[tuple]:
    """
    Detect headings based on casing, punctuation, and structural clues.
    """
    headings = []
    for i, line in enumerate(lines):
        if (
            line.isupper() and len(line.split()) <= 10
        ) or re.match(r"^[A-Z][\w\s\-:]{1,80}$", line):
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

def read_xlsx(file_path: str) -> List[Dict]:
    xls = pd.ExcelFile(file_path)
    sections = []
    for sheet in xls.sheet_names:
        content = xls.parse(sheet).to_string()
        sections.append({
            "section_title": sheet,
            "section_path": sheet,
            "content": content,
            "file_path": file_path,
            "document": os.path.basename(file_path)
        })
    return sections

# ---------- SECTION EXTRACTION ----------

def extract_sections(lines: List[str], headings: List[tuple], file_path: str, document_name: str) -> List[Dict]:
    sections = []
    for idx, (line_no, title) in enumerate(headings):
        start = line_no + 1
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        if len(content.split()) < 10:
            continue
        sections.append({
            "section_title": title,
            "section_path": title,
            "content": content,
            "file_path": file_path,
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

def process_file(file_path: str) -> List[Dict]:
    ext = os.path.splitext(file_path)[1].lower()
    filename = os.path.basename(file_path)

    if ext == ".pdf":
        lines = clean_lines(read_pdf(file_path))
        headings = detect_headings(lines)
        return extract_sections(lines, headings, file_path, filename)
    elif ext == ".docx":
        lines = clean_lines(read_docx(file_path))
        headings = detect_headings(lines)
        return extract_sections(lines, headings, file_path, filename)
    elif ext == ".txt":
        lines = clean_lines(read_txt(file_path))
        headings = detect_headings(lines)
        return extract_sections(lines, headings, file_path, filename)
    elif ext in [".xlsx", ".xls"]:
        return read_xlsx(file_path)
    else:
        print(f"⚠️ Unsupported file: {file_path}")
        return []

# ---------- CHUNK BUILDER ----------
@traceable
def build_chunks(sections: List[Dict]) -> List[Dict]:
    all_chunks = []
    for sec_idx, section in enumerate(sections):
        chunks = spacy_sentence_chunking(section["content"])
        for i, chunk in enumerate(chunks):
            if len(chunk) < MIN_CHAR_COUNT or len(chunk.split()) < MIN_WORD_COUNT:
                continue
            all_chunks.append({
                "document": section["document"],
                "file_path": section["file_path"],
                "section_title": section["section_title"],
                "section_path": section["section_path"],
                "chunk_index": f"{sec_idx}_{i}",
                "content": chunk,
                "char_count": len(chunk),
                "word_count": len(chunk.split())
            })
    return all_chunks

# ---------- QDRANT INGEST ----------
@traceable
def ingest_chunks_to_qdrant(chunks: List[Dict]):
    if not chunks:
        print("⚠️ No chunks to embed.")
        return
    texts = [c["content"] for c in chunks]
    print("🧬 Generating embeddings...")
    vectors = embedder.encode(texts, batch_size=32, show_progress_bar=True)

    qdrant.recreate_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={"size": vectors.shape[1], "distance": "Cosine"}
    )
    qdrant.upload_collection(
        collection_name=COLLECTION_NAME,
        vectors=vectors,
        payload=chunks
    )
    print(f"✅ Uploaded {len(chunks)} vectors to Qdrant.")

# ---------- MAIN ----------

def main():
    all_sections = []
    for file in os.listdir(SOURCE_FOLDER):
        file_path = os.path.join(SOURCE_FOLDER, file)
        if not os.path.isfile(file_path):
            continue
        print(f"📄 Processing: {file}")
        sections = process_file(file_path)
        all_sections.extend(sections)

    print(f"📚 Total extracted sections: {len(all_sections)}")

    chunks = build_chunks(all_sections)
    print(f"🧩 Total chunks before safety: {len(chunks)}")

    safe_chunks = [c for c in tqdm(chunks, desc="🔍 Azure Safety") if is_chunk_safe(c["content"])]
    print(f"✅ Total safe chunks: {len(safe_chunks)}")

    ingest_chunks_to_qdrant(safe_chunks)

if __name__ == "__main__":
    main()
