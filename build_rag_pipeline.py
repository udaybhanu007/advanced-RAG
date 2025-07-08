import os
import argparse
import time
import re
import uuid
import tempfile
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
import mammoth
import pandas as pd
from pdf_markdown_converter import pdf_to_md
from bs4 import BeautifulSoup
from azure.ai.contentsafety import ContentSafetyClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.ai.contentsafety.models import AnalyzeTextOptions, TextCategory
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient, models
import tiktoken


# Load environment variables from .env file
load_dotenv()

def get_args():
    """Get command-line arguments."""
    parser = argparse.ArgumentParser(description="Build a RAG pipeline from source files to a vector store.")
    parser.add_argument("input_path", type=str, help="Path to the input file or directory.")
    parser.add_argument("--collection_name", type=str, default="rag_collection", help="Name of the Qdrant collection.")
    parser.add_argument("--chunk_size", type=int, default=400, help="Maximum number of tokens per chunk.")
    parser.add_argument("--chunk_overlap", type=int, default=80, help="Number of tokens to overlap between chunks.")
    return parser.parse_args()

def convert_files_to_markdown(file_path):
    """
    Converts source files (.docx, .pdf, .xlsx, .txt, .md) to Markdown text.
    """
    print(f"Step 1: Converting {file_path} to Markdown...")
    markdown_text = ""
    file_extension = os.path.splitext(file_path)[1].lower()

    if file_extension == ".pdf":
        markdown_text = pdf_to_md(file_path)
    elif file_extension == ".docx":
        with open(file_path, "rb") as docx_file:
            result = mammoth.convert_to_markdown(docx_file)
            markdown_text = result.value
    elif file_extension == ".xlsx":
        excel_file = pd.ExcelFile(file_path)
        for sheet_name in excel_file.sheet_names:
            df = excel_file.parse(sheet_name)
            # Add sheet name as a header
            markdown_text += f"# {sheet_name}\n\n"
            # Convert dataframe to a descriptive string format
            for index, row in df.iterrows():
                row_description = f"Row {index + 1}: "
                row_description += ", ".join([f"{col} is {row[col]}" for col in df.columns])
                markdown_text += row_description + "\n"
            markdown_text += "\n"
    elif file_extension in [".md", ".txt"]:
        with open(file_path, "r", encoding="utf-8") as f:
            markdown_text = f.read()
    else:
        raise ValueError(f"Unsupported file type: {file_extension}")

    print("File conversion complete.")
    return markdown_text

def linearize_markdown_tables(text):
    """Finds Markdown tables and converts them into a sentence-based format."""
    table_pattern = re.compile(r'((?:\|.*\|(?:\r\n|\n))+)')
    
    def replace_table(match):
        table_str = match.group(1)
        lines = [line.strip() for line in table_str.strip().split('\n')]
        
        # Skip empty or malformed tables
        if len(lines) < 2:
            return table_str
            
        header = [h.strip() for h in lines[0].strip('|').split('|')]
        
        # Skip separator line
        if all(c in '-: |' for c in lines[1]):
            rows = lines[2:]
        else:
            rows = lines[1:]

        linearized_rows = []
        for row_str in rows:
            cells = [c.strip() for c in row_str.strip('|').split('|')]
            if len(cells) == len(header):
                row_desc = ". ".join([f"{header[i]}: {cells[i]}" for i in range(len(header)) if cells[i]])
                linearized_rows.append(row_desc)
        
        # Join with double newline to create distinct paragraphs for the splitter
        return "\n\n" + "\n\n".join(linearized_rows) + "\n\n"

    return table_pattern.sub(replace_table, text)

def clean_and_normalize(text):
    """
    Cleans HTML tags, linearizes tables, and normalizes the language of the text.
    """
    print("Step 2: Cleaning and normalizing text...")
    
    # Linearize tables before other cleaning
    text = linearize_markdown_tables(text)
    
    # Clean HTML tags using BeautifulSoup
    soup = BeautifulSoup(text, "html.parser")
    cleaned_text = soup.get_text()
    
    # Normalize language (e.g., convert to lowercase)
    cleaned_text = cleaned_text.lower()
    
    print("Text cleaning and normalization complete.")
    return cleaned_text

class RecursiveCharacterTextSplitter:
    """A text splitter that recursively splits text based on a list of separators."""
    def __init__(self, chunk_size=1000, chunk_overlap=200, separators=None):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or ["\n\n", "\n", ". ", " ", ""]
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def _split_text(self, text, separators):
        if not text:
            return []
        
        final_chunks = []
        separator = separators[0]
        
        if separator:
            splits = text.split(separator)
        else:
            splits = list(text)

        good_splits = []
        for s in splits:
            if len(self.tokenizer.encode(s, allowed_special="all")) < self.chunk_size:
                good_splits.append(s)
            else:
                if good_splits:
                    merged_text = self._merge_splits(good_splits, separator)
                    final_chunks.extend(merged_text)
                    good_splits = []
                
                other_chunks = self._split_text(s, separators[1:])
                final_chunks.extend(other_chunks)
        
        if good_splits:
            merged_text = self._merge_splits(good_splits, separator)
            final_chunks.extend(merged_text)
            
        return final_chunks

    def _merge_splits(self, splits, separator):
        docs = []
        current_doc = []
        total = 0
        for d in splits:
            _len = len(self.tokenizer.encode(d, allowed_special="all"))
            if total + _len > self.chunk_size:
                if total > 0:
                    doc = separator.join(current_doc)
                    if doc:
                        docs.append(doc)
                
                while total > self.chunk_overlap:
                    _len_val = len(self.tokenizer.encode(current_doc[0], allowed_special="all"))
                    total -= _len_val
                    current_doc = current_doc[1:]
            
            current_doc.append(d)
            total += _len
        
        doc = separator.join(current_doc)
        if doc:
            docs.append(doc)
        return docs

    def split_text(self, text):
        return self._split_text(text, self.separators)

def is_heading(line, prev_line):
    """Determines if a line is a heading using markdown and heuristics."""
    stripped = line.strip()
    if not stripped:
        return False

    # Markdown headings (H1-H5)
    if re.match(r"^(#{1,5}\s*.+)$", stripped):
        return True

    # Heuristic for visual headings:
    # - Short line (<= 12 words)
    # - Is title-cased or all-caps
    # - Doesn't end with punctuation that suggests it's part of a sentence
    # - Is preceded by an empty line (or is the first line)
    is_short = len(stripped.split()) <= 12
    is_title_cased = stripped.istitle() or stripped.isupper()
    is_not_sentence = not stripped.endswith(('.', ':', ',', ';', '?', '!'))
    is_standalone = not prev_line.strip()

    if is_short and is_title_cased and is_not_sentence and is_standalone:
        # A final check to avoid flagging list items
        if stripped.startswith(('*', '-', '•')):
            return False
        return True

    return False

def chunk_text(text, file_path, chunk_size, chunk_overlap):
    """
    Chunks the text using a token-based recursive splitter with hybrid heading detection.
    """
    print("Step 3: Chunking text using hybrid heading detection...")
    
    lines = text.splitlines()
    headings = []
    
    # Identify all headings first
    for i, line in enumerate(lines):
        prev_line = lines[i-1] if i > 0 else ""
        if is_heading(line, prev_line):
            headings.append((i, line.strip()))

    final_chunks = []
    
    # If no headings are found, chunk the whole document as one section
    if not headings:
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        split_docs = text_splitter.split_text(text)
        for doc in split_docs:
            final_chunks.append({"heading": "Document", "content": doc})
        
    else:
        # Process content before the first heading
        first_heading_start_line = headings[0][0]
        if first_heading_start_line > 0:
            preamble = "\n".join(lines[:first_heading_start_line]).strip()
            if preamble:
                text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
                split_docs = text_splitter.split_text(preamble)
                for doc in split_docs:
                    final_chunks.append({"heading": "Introduction", "content": doc})

        # Process content under each identified heading
        for i, (start_line, heading_text) in enumerate(headings):
            end_line = headings[i+1][0] if i + 1 < len(headings) else len(lines)
            content = "\n".join(lines[start_line + 1 : end_line]).strip()
            
            if not content:
                # If a heading has no content, we can choose to ignore it or handle it.
                # For now, we'll ignore it to prevent empty chunks.
                continue

            text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            split_docs = text_splitter.split_text(content)
            
            for doc in split_docs:
                final_chunks.append({"heading": heading_text, "content": doc})

    # Add metadata to each chunk
    for i, chunk in enumerate(final_chunks):
        chunk_text = chunk["content"]
        chunk["chunk_id"] = str(uuid.uuid4())
        chunk["file_path"] = file_path
        chunk["token_count"] = len(tiktoken.get_encoding("cl100k_base").encode(chunk_text, allowed_special="all"))
        chunk["source"] = file_path
        chunk["chunk_index"] = i

    print(f"Text chunking complete. Found {len(final_chunks)} chunk(s).")
    return final_chunks

def check_content_safety(chunks):
    """
    Checks each chunk for content safety using Azure AI Content Safety.
    """
    print("Step 4: Checking content safety...")
    
    endpoint = os.getenv("AZURE_CS_ENDPOINT")
    key = os.getenv("AZURE_CS_KEY")

    if not endpoint or not key or "YOUR_AZURE" in endpoint or "YOUR_AZURE" in key:
        print("Warning: Azure Content Safety credentials not found or are placeholders. Skipping safety check.")
        return chunks

    try:
        client = ContentSafetyClient(endpoint, AzureKeyCredential(key))
    except Exception as e:
        print(f"Warning: Could not create Azure Content Safety client: {e}. Skipping safety check.")
        return chunks

    safe_chunks = []
    for chunk in chunks:
        text_to_analyze = chunk["content"]
        request = AnalyzeTextOptions(text=text_to_analyze, categories=[TextCategory.HATE, TextCategory.SELF_HARM, TextCategory.SEXUAL, TextCategory.VIOLENCE])

        try:
            response = client.analyze_text(request)
        except HttpResponseError as e:
            print(f"Error analyzing text for content safety: {e}")
            # Decide if you want to treat this as a safe chunk or not
            safe_chunks.append(chunk)
            continue

        # Check if any category has a severity level greater than 0
        is_unsafe = False
        for analysis_result in response.categories_analysis:
            if analysis_result.severity > 0:
                is_unsafe = True
                break  # Exit early if any category is flagged

        if not is_unsafe:
            safe_chunks.append(chunk)
        else:
            print(f"Chunk with heading '{chunk['heading']}' flagged as unsafe and will be skipped.")

    print(f"Content safety check complete. {len(safe_chunks)} out of {len(chunks)} chunks are safe.")
    return safe_chunks

def embed_and_store(chunks, collection_name):
    """
    Embeds chunks and stores them in the Qdrant vector store.
    """
    print("Step 5: Embedding and storing chunks in Qdrant...")
    
    # Initialize embedding model
    embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    # Initialize Qdrant client
    client = QdrantClient("localhost", port=6333)

    # Get vector size from the model
    vector_size = embedding_model.get_sentence_embedding_dimension()

    # Recreate collection
    client.recreate_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE)
    )
    print(f"Qdrant collection '{collection_name}' created or recreated.")

    # Prepare points for upsert
    points = []
    all_texts = [chunk["content"] for chunk in chunks]
    
    print(f"Generating embeddings for {len(all_texts)} chunks...")
    t_embed0 = time.time()
    all_embeddings = embedding_model.encode(all_texts, batch_size=32, show_progress_bar=True)
    t_embed1 = time.time()
    print(f"Embedding generation took {t_embed1-t_embed0:.2f} seconds.")

    for i, (chunk, emb) in enumerate(zip(chunks, all_embeddings)):
        point = models.PointStruct(
            id=chunk["chunk_id"],
            vector=emb.tolist(),
            payload={
                "heading": chunk["heading"],
                "content": chunk["content"],
                "file_path": chunk["file_path"],
                "token_count": chunk["token_count"],
                "source": chunk["source"],
                "chunk_index": chunk["chunk_index"]
            }
        )
        points.append(point)

    # Upsert points to Qdrant in batches
    client.upsert(
        collection_name=collection_name,
        points=points,
        wait=True
    )

    print(f"Successfully stored {len(chunks)} chunks in Qdrant collection '{collection_name}'.")


def main():
    """Main function to orchestrate the RAG pipeline."""
    args = get_args()
    start_time = time.time()
    
    input_path = args.input_path
    temp_file_path = None

    try:
        # Check if input is a URL and download it
        if input_path.startswith(('http://', 'https://')):
            print(f"URL detected. Downloading file from {input_path}...")
            response = requests.get(input_path, stream=True)
            response.raise_for_status()
            
            # Determine file type from Content-Type header
            content_type = response.headers.get('content-type')
            file_extension = ".tmp" # Default
            if content_type:
                if 'application/pdf' in content_type:
                    file_extension = ".pdf"
                elif 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' in content_type:
                    file_extension = ".docx"
                elif 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' in content_type:
                    file_extension = ".xlsx"
                elif 'text/plain' in content_type:
                    file_extension = ".txt"
                elif 'text/markdown' in content_type:
                    file_extension = ".md"

            # Create a temporary file with the correct extension
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
                temp_file_path = temp_file.name
                for chunk in response.iter_content(chunk_size=8192):
                    temp_file.write(chunk)
            
            print(f"File downloaded to temporary path: {temp_file_path}")
            processing_path = temp_file_path
        else:
            processing_path = input_path

        print("--- Starting RAG Pipeline ---")

        # 1. Convert source file(s) to Markdown
        markdown_text = convert_files_to_markdown(processing_path)

        # 2. Clean and normalize the Markdown text
        cleaned_text = clean_and_normalize(markdown_text)

        # 3. Chunk the text
        chunks = chunk_text(cleaned_text, processing_path, args.chunk_size, args.chunk_overlap)

        # 4. Perform content safety check
        safe_chunks = check_content_safety(chunks)

        # 5. Embed and store in Qdrant
        if safe_chunks:
            embed_and_store(safe_chunks, args.collection_name)
        else:
            print("No safe chunks to process. Halting pipeline.")

        end_time = time.time()
        print(f"--- RAG Pipeline Finished in {end_time - start_time:.2f} seconds ---")

    except requests.exceptions.RequestException as e:
        print(f"Error downloading file: {e}")
    finally:
        # Clean up the temporary file if it was created
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
            print(f"Cleaned up temporary file: {temp_file_path}")


if __name__ == "__main__":
    main()
