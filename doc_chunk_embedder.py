# --- Unified RAG Chunking Pipeline ---
import re
import json
import uuid
import os
import time
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer

embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
tokenizer = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")

def convert_to_text(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".md":
        with open(file_path, encoding="utf-8") as f:
            return f.read(), "md"
    elif ext == ".pdf":
        try:
            from pdf_markdown_converter import pdf_to_md
        except ImportError:
            raise ImportError("pdf_markdown_converter.py with pdf_to_md is required for PDF support.")
        return pdf_to_md(file_path), "pdf"
    elif ext == ".txt":
        with open(file_path, encoding="utf-8") as f:
            return f.read(), "txt"
    # elif ext == ".docx":
    #     from your_docx_module import docx_to_md
    #     return docx_to_md(file_path), "docx"
    else:
        raise ValueError(f"Unsupported file type: {ext}")

def split_into_sentences(text):
    # Simple sentence splitter (can be replaced with nltk or spacy for better accuracy)
    return re.split(r'(?<=[.!?])\s+', text)

def sliding_window_chunk(sentences, max_words=400, overlap_words=100):
    chunks = []
    i = 0
    n = len(sentences)
    while i < n:
        chunk = []
        word_count = 0
        j = i
        while j < n and word_count < max_words:
            words = sentences[j].split()
            if word_count + len(words) > max_words and chunk:
                break
            chunk.append(sentences[j])
            word_count += len(words)
            j += 1
        if chunk:
            chunks.append(' '.join(chunk))
        if word_count == 0:
            i += 1
        else:
            # Overlap
            overlap = 0
            k = j - 1
            while k >= i and overlap < overlap_words:
                overlap += len(sentences[k].split())
                k -= 1
            i = k + 1
    return chunks

def extract_headings_and_chunks_from_text(text, max_words=400, overlap_words=100):
    lines = text.splitlines(keepends=True)
    heading_pattern = re.compile(r"^(#+\s*.+)$")
    def is_toc_line(line):
        l = line.strip()
        return bool(re.match(r"^([A-Za-z0-9\s]+\.{2,}\s*\d+)$", l))
    def is_section_number(line):
        return bool(re.match(r"^\d+(\.\d+)*$", line.strip()))
    def is_main_heading(i, lines, stripped):
        if heading_pattern.match(stripped):
            return True
        if (
            len(stripped) > 1
            and not stripped.isdigit()
            and not (len(stripped) == 1 and stripped.isalpha())
            and not stripped.startswith('-')
            and not stripped.startswith('*')
            and not stripped.startswith('•')
            and not stripped.startswith('□')
            and not is_toc_line(stripped)
            and not is_section_number(stripped)
            and stripped[0].isupper()
            and (i == 0 or not lines[i-1].strip() or lines[i-1].strip() == '\f')
            and (i+1 == len(lines) or not lines[i+1].strip() or lines[i+1].strip() == '\f')
            and not stripped.endswith(":")
            and not stripped.lower().endswith(" is")
            and not stripped.lower().endswith(" are")
            and not any(word in stripped.lower().split()[:4] for word in ["is", "are", "was"])
        ):
            return True
        return False
    headings = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if is_main_heading(i, lines, stripped):
            headings.append((i, stripped))
    seen = set()
    unique_headings = []
    for idx, h in headings:
        if h not in seen:
            unique_headings.append((idx, h))
            seen.add(h)
    chunks = []
    # If no headings found, use sliding window chunking on the whole text
    if not unique_headings:
        sentences = split_into_sentences(text)
        sw_chunks = sliding_window_chunk(sentences, max_words, overlap_words)
        for chunk_text in sw_chunks:
            chunks.append({"heading": "", "content": chunk_text})
        return chunks
    # If preamble exists before first heading
    if unique_headings and unique_headings[0][0] > 0:
        preamble = "".join(lines[:unique_headings[0][0]]).strip()
        if preamble:
            # Recursively chunk preamble if too large
            if len(preamble.split()) > max_words:
                sentences = split_into_sentences(preamble)
                sw_chunks = sliding_window_chunk(sentences, max_words, overlap_words)
                for chunk_text in sw_chunks:
                    chunks.append({"heading": "Preamble", "content": chunk_text})
            else:
                chunks.append({"heading": "Preamble", "content": preamble})
    # For each heading section
    for i, (start_idx, heading) in enumerate(unique_headings):
        end_idx = unique_headings[i+1][0] if i+1 < len(unique_headings) else len(lines)
        content_lines = lines[start_idx+1:end_idx]
        content = "".join(content_lines).strip()
        if not content:
            continue
        # Recursively chunk if too large
        if len(content.split()) > max_words:
            sentences = split_into_sentences(content)
            sw_chunks = sliding_window_chunk(sentences, max_words, overlap_words)
            for chunk_text in sw_chunks:
                chunks.append({"heading": heading, "content": chunk_text})
        else:
            chunks.append({"heading": heading, "content": content})
    return chunks

def chunk_file_with_metadata(file_path, output_json_path, max_words=400, overlap_words=100):
    text, file_type = convert_to_text(file_path)
    chunks = extract_headings_and_chunks_from_text(text, max_words, overlap_words)
    headings = [chunk["heading"] for chunk in chunks]
    # Prepare metadata for all chunks first (except embedding)
    for chunk in chunks:
        chunk_text = chunk["content"]
        chunk["chunk_id"] = str(uuid.uuid4())
        chunk["file_path"] = file_path
        chunk["file_type"] = file_type
        chunk["word_count"] = len(chunk_text.split())
        chunk["char_count"] = len(chunk_text)
        chunk["source"] = file_path

    all_texts = [chunk["content"] for chunk in chunks]
    print(f"[INFO] Generating embeddings for {len(all_texts)} chunks...")
    t_embed0 = time.time()
    all_embeddings = embedding_model.encode(all_texts, batch_size=4, show_progress_bar=True)
    t_embed1 = time.time()
    print(f"[INFO] Embedding generation took {t_embed1-t_embed0:.2f} seconds.")
    for i, (chunk, emb) in enumerate(zip(chunks, all_embeddings)):
        chunk["embedding"] = emb.tolist()
        if (i+1) % 10 == 0 or (i+1) == len(chunks):
            print(f"[INFO] Processed {i+1}/{len(chunks)} embeddings.")
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)
    print(f"\n✅ Total Chunks Created: {len(chunks)}")
    print(f"✅ Total Headings Detected: {len(headings)}")
    print("Headings:")
    for i, heading in enumerate(headings):
        print(f"  {i+1}. {heading}")
    for i, chunk in enumerate(chunks[:3]):
        print(f"\n🔹 Chunk {i+1}:")
        print(f"Heading: {chunk['heading']}")
        print(f"Content (preview): {chunk['content'][:300]}...")

if __name__ == "__main__":
    # Example: change file_path to any supported file (md, pdf, txt)
    file_path = "AI_Security.pdf"
    output_json_path = "doc_chunk_embeddings1.json"
    # Save the markdown output from the PDF as a separate .md file
    if file_path.lower().endswith('.pdf'):
        from pdf_markdown_converter import pdf_to_md
        md_file = file_path.rsplit('.', 1)[0] + ".md"
        markdown_content = pdf_to_md(file_path)
        with open(md_file, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        print(f"[INFO] Markdown file saved as {md_file}")
    # Increase max_words to reduce chunk count for faster CPU embedding
    max_words = 1000
    overlap_words = 100
    print(f"[INFO] Starting chunking with max_words={max_words}, overlap_words={overlap_words}")
    t0 = time.time()
    chunk_file_with_metadata(file_path, output_json_path, max_words=max_words, overlap_words=overlap_words)
    t1 = time.time()
    print(f"[INFO] Total time taken: {t1-t0:.2f} seconds")
