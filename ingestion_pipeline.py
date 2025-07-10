import os
import argparse
import time
import re
import uuid
import tempfile
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from markdown_converter import convert_files_to_markdown, linearize_markdown_tables, is_heading
from bs4 import BeautifulSoup
from azure.ai.contentsafety import ContentSafetyClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from azure.ai.contentsafety.models import AnalyzeTextOptions, TextCategory
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient, models
import tiktoken


# Load environment variables from a .env file for secure credential management
load_dotenv()

def get_args():
    """
    Parses and returns command-line arguments.
    This allows for dynamic configuration of the pipeline from the command line.
    """
    parser = argparse.ArgumentParser(description="Build a RAG pipeline from source files to a vector store.")
    # The primary input can be a local file path or a URL
    parser.add_argument("input_path", type=str, help="Path to the input file or directory.")
    # The name of the collection in the Qdrant vector store
    parser.add_argument("--collection_name", type=str, default="rag_collection", help="Name of the Qdrant collection.")
    # The maximum size of each text chunk in tokens
    parser.add_argument("--chunk_size", type=int, default=400, help="Maximum number of tokens per chunk.")
    # The number of tokens to overlap between adjacent chunks to maintain context
    parser.add_argument("--chunk_overlap", type=int, default=80, help="Number of tokens to overlap between chunks.")
    return parser.parse_args()

def clean_and_normalize(text):
    """
    Performs a series of cleaning and normalization steps on the raw text.
    This ensures the text is in a consistent format for chunking and embedding.
    """
    print("Step 2: Cleaning and normalizing text...")
    
    # Convert Markdown tables into a sentence-based format for better embedding.
    # This helps the model understand the relationships in tabular data.
    text = linearize_markdown_tables(text)
    
    # Remove any lingering HTML tags from the text.
    soup = BeautifulSoup(text, "html.parser")
    cleaned_text = soup.get_text()
    
    # Convert text to lowercase for consistency.
    cleaned_text = cleaned_text.lower()
    
    print("Text cleaning and normalization complete.")
    return cleaned_text

class RecursiveCharacterTextSplitter:
    """
    A custom text splitter that recursively breaks down text to meet a target chunk size.
    It uses a series of separators, starting with the most semantically significant ones.
    """
    def __init__(self, chunk_size=1000, chunk_overlap=200, separators=None):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # The separators are ordered from largest to smallest semantic unit.
        self.separators = separators or ["\n\n", "\n", ". ", " ", ""]
        # Use tiktoken to accurately measure token count, matching the LLM's perspective.
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def _split_text(self, text, separators):
        # If the text is empty, there's nothing to split.
        if not text:
            return []
        
        final_chunks = []
        # Start with the first (most significant) separator.
        separator = separators[0]
        
        # Split the text based on the current separator.
        if separator:
            splits = text.split(separator)
        else: # If the separator is an empty string, split by individual characters.
            splits = list(text)

        good_splits = []
        for s in splits:
            # If a split is already smaller than the chunk size, keep it.
            if len(self.tokenizer.encode(s, allowed_special="all")) < self.chunk_size:
                good_splits.append(s)
            else:
                # If we have accumulated good splits, merge them into chunks first.
                if good_splits:
                    merged_text = self._merge_splits(good_splits, separator)
                    final_chunks.extend(merged_text)
                    good_splits = []
                
                # If a split is too large, recursively call this function with the next separator.
                other_chunks = self._split_text(s, separators[1:])
                final_chunks.extend(other_chunks)
        
        # Merge any remaining good splits.
        if good_splits:
            merged_text = self._merge_splits(good_splits, separator)
            final_chunks.extend(merged_text)
            
        return final_chunks

    def _merge_splits(self, splits, separator):
        """Merges smaller splits into chunks that respect the chunk size and overlap."""
        docs = []
        current_doc = []
        total = 0
        for d in splits:
            _len = len(self.tokenizer.encode(d, allowed_special="all"))
            # If adding the next split exceeds the chunk size, finalize the current chunk.
            if total + _len > self.chunk_size:
                if total > 0:
                    doc = separator.join(current_doc)
                    if doc:
                        docs.append(doc)
                
                # Slide the window forward by removing splits from the beginning
                # until the overlap is respected.
                while total > self.chunk_overlap:
                    _len_val = len(self.tokenizer.encode(current_doc[0], allowed_special="all"))
                    total -= _len_val
                    current_doc = current_doc[1:]
            
            current_doc.append(d)
            total += _len
        
        # Add the last remaining chunk.
        doc = separator.join(current_doc)
        if doc:
            docs.append(doc)
        return docs

    def split_text(self, text):
        """Public method to start the splitting process."""
        return self._split_text(text, self.separators)

def chunk_text(text, file_path, chunk_size, chunk_overlap):
    """
    Chunks the text using a token-based recursive splitter with hybrid heading detection.
    This optimized version processes document sections in a unified manner.
    """
    print("Step 3: Chunking text using hybrid heading detection...")
    
    lines = text.splitlines()
    
    # First, identify all semantic headings in the document.
    # This allows us to create sections based on the document's own structure.
    headings = []
    for i, line in enumerate(lines):
        prev_line = lines[i-1] if i > 0 else ""
        if is_heading(line, prev_line):
            headings.append((i, line.strip()))

    # Consolidate all parts of the document into a single list of sections.
    # Each section is a tuple containing its associated heading and content.
    sections = []

    # If no headings are found, treat the entire document as a single section.
    if not headings:
        sections.append(("Document", text))
    else:
        # Handle any text that appears before the first heading (the "preamble").
        first_heading_start_line = headings[0][0]
        if first_heading_start_line > 0:
            preamble = "\n".join(lines[:first_heading_start_line]).strip()
            if preamble:
                sections.append(("Introduction", preamble))
        
        # Create a section for the content under each identified heading.
        for i, (start_line, heading_text) in enumerate(headings):
            end_line = headings[i+1][0] if i + 1 < len(headings) else len(lines)
            content = "\n".join(lines[start_line + 1 : end_line]).strip()
            if content: # Ensure we don't create sections for headings with no content.
                sections.append((heading_text, content))

    final_chunks = []
    # Instantiate the text splitter once to be reused for all sections, improving efficiency.
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    # Atomically process each section, splitting it into smaller chunks.
    for heading, content in sections:
        split_docs = text_splitter.split_text(content)
        for doc in split_docs:
            # Associate each final chunk with its parent heading for context.
            final_chunks.append({"heading": heading, "content": doc})

    # Add a rich set of metadata to each chunk for downstream applications.
    for i, chunk in enumerate(final_chunks):
        chunk_text = chunk["content"]
        # A unique ID for each chunk is essential for storage and retrieval.
        chunk["chunk_id"] = str(uuid.uuid4())
        # The original file path is important for traceability.
        chunk["file_path"] = file_path
        # Store the token count for potential analysis or filtering later.
        chunk["token_count"] = len(tiktoken.get_encoding("cl100k_base").encode(chunk_text, allowed_special="all"))
        # The source helps identify the origin of the data.
        chunk["source"] = file_path
        # The index of the chunk within the document.
        chunk["chunk_index"] = i

    print(f"Text chunking complete. Found {len(final_chunks)} chunk(s).")
    return final_chunks

def check_content_safety(chunks):
    """
    Filters out chunks that are flagged as unsafe by Azure AI Content Safety.
    This is a crucial step for building a responsible AI application.
    """
    print("Step 4: Checking content safety...")
    
    # Retrieve Azure credentials from environment variables.
    endpoint = os.getenv("AZURE_CS_ENDPOINT")
    key = os.getenv("AZURE_CS_KEY")

    # If credentials are not available or are placeholders, skip the check.
    if not endpoint or not key or "YOUR_AZURE" in endpoint or "YOUR_AZURE" in key:
        print("Warning: Azure Content Safety credentials not found or are placeholders. Skipping safety check.")
        return chunks

    try:
        # Initialize the Content Safety client.
        client = ContentSafetyClient(endpoint, AzureKeyCredential(key))
    except Exception as e:
        print(f"Warning: Could not create Azure Content Safety client: {e}. Skipping safety check.")
        return chunks

    safe_chunks = []
    for chunk in chunks:
        text_to_analyze = chunk["content"]
        # Define the categories of harmful content to check for.
        request = AnalyzeTextOptions(text=text_to_analyze, categories=[TextCategory.HATE, TextCategory.SELF_HARM, TextCategory.SEXUAL, TextCategory.VIOLENCE])

        try:
            # Send the text to the Azure service for analysis.
            response = client.analyze_text(request)
        except HttpResponseError as e:
            print(f"Error analyzing text for content safety: {e}")
            # In case of an API error, we can choose to be permissive and keep the chunk.
            safe_chunks.append(chunk)
            continue

        # A chunk is considered unsafe if any category has a severity level above zero.
        is_unsafe = any(analysis.severity > 0 for analysis in response.categories_analysis)

        if not is_unsafe:
            safe_chunks.append(chunk)
        else:
            print(f"Chunk with heading '{chunk['heading']}' flagged as unsafe and will be skipped.")

    print(f"Content safety check complete. {len(safe_chunks)} out of {len(chunks)} chunks are safe.")
    return safe_chunks

def embed_and_store(chunks, collection_name):
    """
    Generates vector embeddings for each chunk and stores them in a Qdrant collection.
    """
    print("Step 5: Embedding and storing chunks in Qdrant...")
    
    # Initialize the sentence transformer model for creating embeddings.
    # "all-MiniLM-L6-v2" is a good default for performance and quality.
    embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

    # Initialize the client to connect to the Qdrant vector store.
    client = QdrantClient("localhost", port=6333)

    # The vector size must match the output dimension of the embedding model.
    vector_size = embedding_model.get_sentence_embedding_dimension()

    # Recreate the collection to ensure a fresh start. For production, an incremental
    # update strategy would be more appropriate.
    client.recreate_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE)
    )
    print(f"Qdrant collection '{collection_name}' created or recreated.")

    # Prepare the data for embedding and storage.
    points = []
    all_texts = [chunk["content"] for chunk in chunks]
    
    # Generate embeddings for all chunks in a single batch for efficiency.
    print(f"Generating embeddings for {len(all_texts)} chunks...")
    t_embed0 = time.time()
    all_embeddings = embedding_model.encode(all_texts, batch_size=32, show_progress_bar=True)
    t_embed1 = time.time()
    print(f"Embedding generation took {t_embed1-t_embed0:.2f} seconds.")

    # Create Qdrant PointStruct objects, which combine the vector and its metadata.
    for i, (chunk, emb) in enumerate(zip(chunks, all_embeddings)):
        point = models.PointStruct(
            id=chunk["chunk_id"],      # The unique identifier for the point
            vector=emb.tolist(),       # The vector embedding
            payload=chunk              # The associated metadata
        )
        points.append(point)

    # Upsert all the points to the Qdrant collection in a single, efficient operation.
    client.upsert(
        collection_name=collection_name,
        points=points,
        wait=True  # Wait for the operation to complete.
    )

    print(f"Successfully stored {len(chunks)} chunks in Qdrant collection '{collection_name}'.")

def main():
    """The main function that orchestrates the entire RAG ingestion pipeline."""
    # A mapping from URL content types to file extensions for cleaner logic.
    CONTENT_TYPE_MAP = {
        'application/pdf': '.pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
        'text/plain': '.txt',
        'text/markdown': '.md'
    }

    # Parse command-line arguments
    args = get_args()
    start_time = time.time()
    
    input_path = args.input_path
    temp_file_path = None

    try:
        # If the input path is a URL, download the file to a temporary location.
        if input_path.startswith(('http://', 'https://')):
            print(f"URL detected. Downloading file from {input_path}...")
            response = requests.get(input_path, stream=True)
            response.raise_for_status()  # Raise an exception for bad status codes
            
            # Infer the file extension from the 'Content-Type' header using the map.
            content_type = response.headers.get('content-type', '')
            file_extension = ".tmp"  # Default extension
            for c_type, ext in CONTENT_TYPE_MAP.items():
                if c_type in content_type:
                    file_extension = ext
                    break

            # Stream the downloaded content into a temporary file.
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
                temp_file_path = temp_file.name
                for http_chunk in response.iter_content(chunk_size=8192):
                    temp_file.write(http_chunk)
            
            print(f"File downloaded to temporary path: {temp_file_path}")
            processing_path = temp_file_path
        else:
            # If it's a local path, use it directly.
            processing_path = input_path

        print("--- Starting RAG Pipeline ---")

        # Step 1: Convert the source file (any format) into clean Markdown text.
        markdown_text = convert_files_to_markdown(processing_path)

        # Step 2: Normalize the text by cleaning HTML, linearizing tables, etc.
        cleaned_text = clean_and_normalize(markdown_text)

        # Step 3: Chunk the normalized text into smaller, semantically coherent pieces.
        chunks = chunk_text(cleaned_text, processing_path, args.chunk_size, args.chunk_overlap)

        # Step 4: Check each chunk for harmful content.
        safe_chunks = check_content_safety(chunks)

        # Step 5: Generate embeddings and store the safe chunks in the vector store.
        if safe_chunks:
            embed_and_store(safe_chunks, args.collection_name)
        else:
            print("No safe chunks to process. Halting pipeline.")

        end_time = time.time()
        print(f"--- RAG Pipeline Finished in {end_time - start_time:.2f} seconds ---")

    except requests.exceptions.RequestException as e:
        print(f"Error downloading file from URL: {e}")
    finally:
        # Ensure the temporary file is deleted after processing.
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
            print(f"Cleaned up temporary file: {temp_file_path}")

# Standard Python entry point
if __name__ == "__main__":
    main()
