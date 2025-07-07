from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer
import re
import os
import base64
from io import BytesIO
import itertools

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    import PyPDF2
    PYPDF2_AVAILABLE = True
except ImportError:
    PYPDF2_AVAILABLE = False

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

if not (PDFPLUMBER_AVAILABLE or PYPDF2_AVAILABLE):
    raise ImportError("Either pdfplumber or PyPDF2 is required for PDF parsing. Please install one with 'pip install pdfplumber' or 'pip install PyPDF2'.")

def extract_pdf_content(filepath):
    """Extracts text, headings (by font size), tables, and images from a PDF file using pdfplumber."""
    if not PDFPLUMBER_AVAILABLE:
        raise ImportError("pdfplumber is required for full PDF extraction. Please install it with 'pip install pdfplumber'.")
    content = {
        "headings": [],
        "paragraphs": [],
        "tables": [],
        "images": []
    }
    with pdfplumber.open(filepath) as pdf:
        for page_num, page in enumerate(pdf.pages):
            # --- Headings by font size ---
            headings = []
            if hasattr(page, "chars") and page.chars:
                # Group chars by line (y0), then by font size
                lines = {}
                for char in page.chars:
                    y = round(char["top"], 1)
                    lines.setdefault(y, []).append(char)
                # Find the most common font size (body text)
                font_sizes = [round(char["size"], 1) for char in page.chars]
                if font_sizes:
                    body_font_size = max(set(font_sizes), key=font_sizes.count)
                else:
                    body_font_size = None
                for y, chars in lines.items():
                    text = "".join(c["text"] for c in sorted(chars, key=lambda c: c["x0"])).strip()
                    if not text:
                        continue
                    # Heading if font size is larger than body text
                    line_font_size = max(round(c["size"], 1) for c in chars)
                    if body_font_size and line_font_size > body_font_size:
                        headings.append(text)
            # Remove duplicates and short non-headings
            seen_headings = set()
            for h in headings:
                if h and h not in seen_headings and len(h) > 2:
                    content["headings"].append({"page": page_num+1, "heading": h})
                    seen_headings.add(h)

            # --- Paragraphs (group lines by vertical space) ---
            lines = (page.extract_text(layout=True) or "").splitlines()
            # Group lines into paragraphs by empty lines
            paragraphs = []
            for is_blank, group in itertools.groupby(lines, key=lambda l: not l.strip()):
                if not is_blank:
                    para = " ".join(line.strip() for line in group)
                    if para and para not in headings:
                        paragraphs.append(para)
            for para in paragraphs:
                content["paragraphs"].append({"page": page_num+1, "text": para})

            # --- Tables ---
            tables = page.extract_tables()
            for t in tables:
                if t and len(t) > 1:
                    # Clean headers and rows
                    headers = [str(h).strip() if h is not None else "" for h in t[0]]
                    rows = [[str(cell).strip() if cell is not None else "" for cell in row] for row in t[1:]]
                    # Remove empty rows
                    rows = [row for row in rows if any(cell for cell in row)]
                    if headers and rows:
                        content["tables"].append({
                            "page": page_num+1,
                            "headers": headers,
                            "rows": rows
                        })

            # --- Images ---
            for img in page.images:
                try:
                    cropped = page.crop((img["x0"], img["top"], img["x1"], img["bottom"])).to_image(resolution=150)
                    img_bytes = BytesIO()
                    cropped.save(img_bytes, format="PNG")
                    img_b64 = base64.b64encode(img_bytes.getvalue()).decode("utf-8")
                    content["images"].append({
                        "page": page_num+1,
                        "base64": img_b64
                    })
                except Exception:
                    continue
    return content

def read_document(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.pdf':
        return extract_pdf_content(filepath)
    elif ext in ('.doc', '.docx'):
        if not DOCX_AVAILABLE:
            raise ImportError("python-docx is required for DOC/DOCX parsing. Please install it with 'pip install python-docx'.")
        doc = docx.Document(filepath)
        content = {
            "headings": [],
            "paragraphs": [],
            "tables": [],
            "images": []  # Not supported by python-docx directly
        }
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            # Heuristic: heading if style name contains 'Heading'
            if para.style and 'heading' in para.style.name.lower():
                content["headings"].append({"page": 1, "heading": text})
            else:
                content["paragraphs"].append({"page": 1, "text": text})
        # Tables
        for table in doc.tables:
            headers = []
            rows = []
            for i, row in enumerate(table.rows):
                cells = [cell.text.strip() for cell in row.cells]
                if i == 0:
                    headers = cells
                else:
                    rows.append(cells)
            if headers and rows:
                content["tables"].append({
                    "page": 1,
                    "headers": headers,
                    "rows": rows
                })
        return content
    elif ext == '.txt':
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
        return {
            "headings": [],
            "paragraphs": [{"page": 1, "text": line} for line in text.splitlines() if line.strip()],
            "tables": [],
            "images": []
        }
    else:
        raise ValueError(f"Unsupported file type: {ext}")

def build_chunks_and_payloads_from_content(content):
    """Builds chunks and payloads from extracted PDF content."""
    chunks, payloads = [], []
    # Headings
    for h in content["headings"]:
        chunks.append(h["heading"])
        payloads.append({
            "type": "heading",
            "page": h["page"],
            "text": h["heading"]
        })
    # Paragraphs
    for p in content["paragraphs"]:
        if p["text"].strip():
            chunks.append(p["text"])
            payloads.append({
                "type": "paragraph",
                "page": p["page"],
                "text": p["text"]
            })
    # Tables
    for t in content["tables"]:
        # Convert headers and row cells to string, replacing None with ""
        safe_headers = [str(h) if h is not None else "" for h in t["headers"]]
        table_text = f"Table on page {t['page']}: " + ", ".join(safe_headers)
        for row in t["rows"]:
            safe_row = [str(cell) if cell is not None else "" for cell in row]
            row_text = ", ".join(safe_row)
            chunks.append(table_text + " | " + row_text)
            payloads.append({
                "type": "table_row",
                "page": t["page"],
                "headers": safe_headers,
                "row": safe_row
            })
    # Images
    for img in content["images"]:
        chunks.append(f"Image on page {img['page']}")
        payloads.append({
            "type": "image",
            "page": img["page"],
            "base64": img["base64"]
        })
    return chunks, payloads

def upload_to_qdrant(chunks, payloads, collection_name="doc_chunks1"):
    model = SentenceTransformer('all-MiniLM-L6-v2')
    vectors = model.encode(chunks).tolist()
    client = QdrantClient("localhost", port=6333)
    client.recreate_collection(
        collection_name=collection_name,
        vectors_config={"size": len(vectors[0]), "distance": "Cosine"}
    )
    client.upsert(
        collection_name=collection_name,
        points=[
            {"id": i, "vector": vectors[i], "payload": payloads[i]}
            for i in range(len(chunks))
        ]
    )
    return model, client

def search_by_model_and_company(client, model, model_name, company_name=None, top_k=5, collection_name="doc_chunks1"):
    filters = [{"key": "Model", "match": {"value": model_name}}]
    if company_name:
        filters.append({"key": "table", "match": {"value": f"{company_name} Sales by Model and Month"}})
    results = client.search(
        collection_name=collection_name,
        query_vector=model.encode([model_name]).tolist()[0],
        limit=top_k,
        query_filter={"must": filters}
    )
    print(f"\nSearch results for Model={model_name}, Company={company_name}:")
    for hit in results:
        print(hit.payload)

def get_jan_revenue_by_model(client, model, model_name, top_k=5, collection_name="doc_chunks1"):
    filters = [{"key": "Model", "match": {"value": model_name}}]
    results = client.search(
        collection_name=collection_name,
        query_vector=model.encode([model_name]).tolist()[0],
        limit=top_k,
        query_filter={"must": filters}
    )

def main():
    content = read_document("D:\\Softwares\\AI_Security.pdf")
    chunks, payloads = build_chunks_and_payloads_from_content(content)
    if not chunks:
        print("No data found in the document. Please check the file format.")
        exit(1)
    model, client = upload_to_qdrant(chunks, payloads)
    print("Document chunks uploaded successfully!")
    # Debug: print a few payloads
    print("\nSample payloads:")
    for p in payloads[:3]:
        print(p)
    # Example usage: search for a heading or paragraph
    if any(p.get("type") == "heading" for p in payloads):
        search_by_model_and_company(client, model, "Dzire", "Maruti")
        get_jan_revenue_by_model(client, model, "Baleno")

if __name__ == "__main__":
    main()