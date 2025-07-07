# Make sure to install pdfminer.six, pi-heif, unstructured-inference, and pdf2image before running:
# pip install pdfminer.six pi-heif unstructured-inference pdf2image
# Also, install Poppler and add it to your PATH: https://github.com/oschwartz10612/poppler-windows/releases/
# 
# After downloading and extracting Poppler, add the 'bin' folder (e.g., C:\poppler\Library\bin) to your system PATH environment variable.
# Restart your terminal or IDE after updating PATH.
# 
# If you still see "Unable to get page count. Is poppler installed and in PATH?", double-check:
#   1. The 'bin' folder path is correct and added to PATH.
#   2. You restarted your terminal/IDE.
#   3. You can run 'where pdfinfo' in Command Prompt and it finds the executable.
#
# NOTE: This script is configured to AVOID Tesseract and OCR completely.
# Do NOT install unstructured[pytesseract] or tesseract-ocr unless you need OCR for scanned PDFs.

from unstructured.partition.pdf import partition_pdf
from unstructured.partition.docx import partition_docx
from unstructured.partition.doc import partition_doc
from sentence_splitter import SentenceSplitter
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer
import uuid
import json
import shutil
import sys
import re
import os

# Initialize tools
splitter = SentenceSplitter(language='en')
embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
tokenizer = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")

MAX_TOKENS = 512

# Check if pdfinfo (Poppler) is available
if shutil.which("pdfinfo") is None:
    sys.exit(
        "❌ Poppler is not installed or not in PATH. "
        "Download from https://github.com/oschwartz10612/poppler-windows/releases/ "
        "and add the 'bin' folder to your PATH. "
        "Then restart your terminal or IDE."
    )

def is_section_heading(text):
    # Resume-style section headings (case-insensitive, allow trailing colon)
    section_keywords = [
        "objectives", "professional summary", "manual testing", "professional experience",
        "technical exposure", "educational credentials", "project summary", "personal details"
    ]
    # Project N, e.g., "Project 1", "Project 2", etc.
    if re.match(r"^Project\s+\d+", text, re.IGNORECASE):
        return True
    # Section keywords
    for kw in section_keywords:
        if text.lower().strip(":") == kw:
            return True
    # All caps or title case, or numbered heading
    if text.isupper() or text == text.title():
        return True
    # Numbered heading (e.g., 1.India..., 2. Physical..., 1.1 Subsection)
    if re.match(r"^\d+(\.\d+)*[\.\)]?\s*[A-Z]", text):
        return True
    return False

def is_bullet(text):
    return bool(re.match(r"^\s*(□|-|\*|\u2022)\s+", text.strip()))

def extract_table_from_element(element):
    # If the element is a table, extract as list of lists or CSV
    if hasattr(element, "metadata") and getattr(element.metadata, "category", "") == "Table":
        try:
            table_data = element.text.strip().split("\n")
            rows = [re.split(r"\s{2,}|\t", row) for row in table_data if row.strip()]
            # Format as markdown table if possible
            if rows and len(rows) > 1:
                header = "| " + " | ".join(cell.strip() for cell in rows[0]) + " |"
                separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
                body = "\n".join(
                    "| " + " | ".join(cell.strip() for cell in row) + " |"
                    for row in rows[1:]
                )
                markdown_table = "\n".join([header, separator, body])
                return markdown_table
            else:
                # Fallback: join as plain text
                return "\n".join(["\t".join(row) for row in rows])
        except Exception:
            return None
    return None

def chunk_text_by_tokens(sentences, max_tokens, tokenizer):
    chunks = []
    current_chunk = []
    current_tokens = 0
    for sent in sentences:
        sent_tokens = len(tokenizer.encode(sent, add_special_tokens=False))
        if current_tokens + sent_tokens > max_tokens and current_chunk:
            chunks.append(" ".join(current_chunk))
            current_chunk = []
            current_tokens = 0
        current_chunk.append(sent)
        current_tokens += sent_tokens
    if current_chunk:
        chunks.append(" ".join(current_chunk))
    return chunks

def extract_md_headings_and_chunks(filepath):
    """
    Extracts chunks from a markdown file by splitting content according to the provided headings (chunk by section).
    Returns a list of dicts: [{"section": heading, "chunk": content}, ...]
    """
    # Dynamically extract all section headings (lines not indented, not empty, not a list, not a table, not a number, not a figure, not a page number)
    import re
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Heuristic: a heading is a line that is not empty, not indented, not a bullet, not a table, not a number, not a figure, not a page number, and is surrounded by blank lines or file start/end
    headings = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Exclude lines that are likely not headings
        if (
            line.startswith(" ") or line.startswith("\t") or
            stripped.startswith("-") or stripped.startswith("*") or stripped.startswith("•") or
            stripped.startswith("|") or stripped.startswith("#") or
            re.match(r"^\d+(\.|\))", stripped) or
            re.match(r"^Figure ", stripped) or
            re.match(r"^Table ", stripped) or
            re.match(r"^\d+$", stripped) or
            len(stripped) < 3
        ):
            continue
        # Heading is surrounded by blank lines or file start/end
        prev_blank = (i == 0) or not lines[i-1].strip()
        next_blank = (i == len(lines)-1) or not lines[i+1].strip()
        if prev_blank and next_blank:
            headings.append(stripped)

    # Remove duplicates while preserving order
    seen = set()
    unique_headings = []
    for h in headings:
        if h not in seen:
            unique_headings.append(h)
            seen.add(h)
    headings = unique_headings

    # Now chunk by section using these headings
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Build regex pattern to match all headings
    escaped_headings = [re.escape(h) for h in headings]
    pattern = r"(^|\n)(?P<heading>" + "|".join(escaped_headings) + ")\s*\n"
    matches = list(re.finditer(pattern, content, re.MULTILINE))
    chunks = []
    for i, match in enumerate(matches):
        heading = match.group("heading")
        start = match.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(content)
        chunk_text = content[start:end].strip()
        if chunk_text:
            chunks.append({
                "section": heading,
                "chunk": chunk_text
            })
    return chunks

def process_elements(elements):
    # If elements are from markdown, they have 'section' attribute
    if elements and hasattr(elements[0], "section"):
        chunks = []
        for el in elements:
            chunk_text = el.text.strip()
            if chunk_text:
                chunks.append({
                    "section": getattr(el, "section", "Unknown"),
                    "chunk": chunk_text
                })
        return chunks

    chunks = []
    current_section = None
    content_lines = []
    project_section = None
    project_lines = []
    personal_section = False
    personal_lines = []
    tech_section = False
    tech_lines = []

    # Patterns for personal details fields
    personal_fields = [
        r"^D\.?O\.?B\.?:", r"^Date of Birth:", r"^Nationality:", r"^Gender:", r"^Father'?s Name:",
        r"^Address", r"^Permanent Address", r"^Present Address", r"^Marital Status:", r"^Contact", r"^Email"
    ]
    personal_fields_regex = re.compile("|".join(personal_fields), re.IGNORECASE)

    # Patterns for technical exposure fields
    tech_fields = [
        r"^Programming Languages", r"^Web Technologies", r"^Operating Systems", r"^Framework Tool",
        r"^WebDriver", r"^Bug Tracking Tool", r"^Software Testing"
    ]
    tech_fields_regex = re.compile("|".join(tech_fields), re.IGNORECASE)

    def is_personal_detail_line(text):
        return bool(personal_fields_regex.match(text.strip())) or (
            text.strip().startswith("●") and len(text.strip()) < 100
        )

    def is_tech_detail_line(text):
        return bool(tech_fields_regex.match(text.strip())) or (
            text.strip().startswith("⮚") or text.strip().startswith("●")
        )

    def flush_section():
        nonlocal current_section, content_lines
        if current_section and content_lines:
            chunk_text = "\n".join(content_lines).strip()
            if chunk_text:
                chunks.append({
                    "section": current_section,
                    "chunk": chunk_text
                })
        content_lines = []

    def flush_project():
        nonlocal project_section, project_lines
        if project_section and project_lines:
            chunk_text = "\n".join(project_lines).strip()
            if chunk_text:
                chunks.append({
                    "section": project_section,
                    "chunk": chunk_text
                })
        project_section = None
        project_lines = []

    def flush_personal():
        nonlocal personal_lines
        if personal_lines:
            formatted = []
            for line in personal_lines:
                line = line.strip()
                if not line.startswith("●"):
                    line = "● " + line
                formatted.append(line)
            chunk_text = "\n".join(formatted).strip()
            if chunk_text:
                chunks.append({
                    "section": "Personal Details",
                    "chunk": chunk_text
                })
        personal_lines = []

    def flush_tech():
        nonlocal tech_lines
        if tech_lines:
            formatted = []
            for line in tech_lines:
                line = line.strip()
                if not (line.startswith("⮚") or line.startswith("●")):
                    line = "⮚ " + line
                formatted.append(line)
            chunk_text = "\n".join(formatted).strip()
            if chunk_text:
                chunks.append({
                    "section": "Technical Exposure",
                    "chunk": chunk_text
                })
        tech_lines = []

    for el in elements:
        text = el.text.strip() if hasattr(el, "text") else str(el)
        if not text:
            continue
        table = extract_table_from_element(el)
        if table:
            flush_section()
            flush_project()
            flush_personal()
            flush_tech()
            chunks.append({
                "section": current_section if current_section else "Unknown",
                "chunk": table
            })
            continue

        # Detect start of "Personal Details" section
        if re.match(r"personal details", text, re.IGNORECASE):
            flush_section()
            flush_project()
            flush_personal()
            flush_tech()
            personal_section = True
            continue

        # Detect start of "Technical Exposure" section
        if re.match(r"technical exposure", text, re.IGNORECASE):
            flush_section()
            flush_project()
            flush_personal()
            flush_tech()
            tech_section = True
            continue

        # If in "Personal Details", collect all lines until next section or project
        if personal_section:
            if (is_section_heading(text) and not re.match(r"personal details", text, re.IGNORECASE)) or re.match(r"^Project\s+\d+", text, re.IGNORECASE):
                flush_personal()
                personal_section = False
            else:
                if text:
                    personal_lines.append(text)
                continue

        # If in "Technical Exposure", collect all lines until next section or project
        if tech_section:
            if (is_section_heading(text) and not re.match(r"technical exposure", text, re.IGNORECASE)) or re.match(r"^Project\s+\d+", text, re.IGNORECASE):
                flush_tech()
                tech_section = False
            else:
                if text:
                    tech_lines.append(text)
                continue

        # If a line matches a personal detail field, treat as part of personal details
        if is_personal_detail_line(text):
            personal_section = True
            personal_lines.append(text)
            continue

        # If a line matches a technical exposure field, treat as part of technical exposure
        if is_tech_detail_line(text):
            tech_section = True
            tech_lines.append(text)
            continue

        # Detect start of a project section
        if re.match(r"^Project\s+\d+", text, re.IGNORECASE):
            flush_section()
            flush_project()
            flush_personal()
            flush_tech()
            project_section = text
            project_lines = []
            continue

        # If in a project section, collect all lines until next section or next project
        if project_section:
            if (
                (is_section_heading(text) and not re.match(r"^Project\s+\d+", text, re.IGNORECASE))
                and not re.match(r"^(Description|Responsibility)", text, re.IGNORECASE)
            ):
                flush_project()
                project_section = None
            else:
                project_lines.append(text)
                continue

        if is_section_heading(text):
            flush_section()
            flush_project()
            flush_personal()
            flush_tech()
            current_section = text
            content_lines = []
            continue

        content_lines.append(text)

    # Flush any remaining content
    flush_section()
    flush_project()
    flush_personal()
    flush_tech()
    return chunks

def load_elements(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        return partition_pdf(
            filepath,
            ocr_strategy="hi_res",
            extract_images_in_pdf=False,
            infer_table_structure=True,
            strategy="hi_res",
        )
    elif ext == ".docx":
        return partition_docx(filepath)
    elif ext == ".doc":
        return partition_doc(filepath)
    elif ext == ".txt":
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
        class DummyElement:
            def __init__(self, text):
                self.text = text
                self.metadata = type("Meta", (), {})()
        return [DummyElement(t) for t in text.split("\n\n") if t.strip()]
    elif ext == ".md":
        # For markdown, return heading-based chunks as special elements
        md_chunks = extract_md_headings_and_chunks(filepath)
        class DummyElement:
            def __init__(self, section, text):
                self.text = text
                self.section = section
                self.metadata = type("Meta", (), {})()
        return [DummyElement(chunk["section"], chunk["chunk"]) for chunk in md_chunks]
    else:
        raise ValueError(f"Unsupported file extension: {ext}")


def extract_headlines_from_markdown(filepath):
    """
    Extracts all unique section headlines from a markdown file in the order they appear.
    Returns a list of headlines.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    headlines = []
    # A headline is a line that is not empty, not indented, not a bullet, not a table, not a number, not a figure, not a page number, and is surrounded by blank lines or file start/end
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Exclude lines that are likely not headings
        if (
            line.startswith(" ") or line.startswith("\t") or
            stripped.startswith("-") or stripped.startswith("*") or stripped.startswith("•") or
            stripped.startswith("|") or stripped.startswith("#") or
            re.match(r"^\d+(\.|\))", stripped) or
            re.match(r"^Figure ", stripped) or
            re.match(r"^Table ", stripped) or
            re.match(r"^\d+$", stripped) or
            len(stripped) < 3
        ):
            continue
        prev_blank = (i == 0) or not lines[i-1].strip()
        next_blank = (i == len(lines)-1) or not lines[i+1].strip()
        if prev_blank and next_blank:
            headlines.append(stripped)
    # Remove duplicates while preserving order
    seen = set()
    unique_headlines = []
    for h in headlines:
        if h and h not in seen:
            unique_headlines.append(h)
            seen.add(h)
    return unique_headlines

def extract_headings_and_chunks_from_markdown(filepath):
    """
    Reads a markdown file and splits it into chunks based on detected section headlines.
    Returns a list of dicts: {"heading": heading, "content": content}
    """
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    headlines = extract_headlines_from_markdown(filepath)
    if not headlines:
        return []
    # Find the line numbers for each heading
    heading_indices = []
    for idx, line in enumerate(lines):
        if line.strip() in headlines:
            heading_indices.append(idx)
    # Build chunks based on heading positions
    chunks = []
    for i, idx in enumerate(heading_indices):
        heading = lines[idx].strip()
        start = idx + 1
        end = heading_indices[i + 1] if i + 1 < len(heading_indices) else len(lines)
        content = "".join(lines[start:end]).strip()
        if content:
            chunks.append({"heading": heading, "content": content})
    return chunks

def analyze_markdown_and_create_chunks(md_filepath, output_json_path):
    """
    Reads a markdown file, splits it into chunks by headlines, assigns unique IDs and embeddings, and saves as JSON.
    """
    chunks = extract_headings_and_chunks_from_markdown(md_filepath)
    for chunk in chunks:
        chunk_text = chunk["content"]
        chunk["id"] = str(uuid.uuid4())
        chunk["embedding"] = embedding_model.encode(chunk_text).tolist()
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)
    print(f"✅ Total Chunks Created: {len(chunks)}")
    if chunks:
        print(f"🔹 Sample Chunk:\nHeading: {chunks[0]['heading']}\nContent: {chunks[0]['content'][:300]}...")
    else:
        print("⚠️ No chunks were created. Check if the file contains extractable text.")

# Example usage:
if __name__ == "__main__":
    md_filepath = "Mastering_AI_Agents.md"  # Change to your markdown file
    output_json_path = "chunk_embeddings.json"
    analyze_markdown_and_create_chunks(md_filepath, output_json_path)