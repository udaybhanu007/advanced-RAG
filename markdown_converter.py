import os
import tempfile
import re
from pathlib import Path
import mammoth
import pandas as pd
import pymupdf
import pymupdf4llm

def is_meaningful_text(text: str, min_alphanum_ratio: float = 0.5, min_avg_word_len: int = 3) -> bool:
    """
    Checks if a string of text is likely to be meaningful content rather than OCR noise.
    """
    # Rule 1: Must have a minimum length
    if len(text) < 10:
        return False
    
    # Rule 2: Must have a high ratio of alphanumeric characters
    alphanum_chars = sum(1 for char in text if char.isalnum())
    if (alphanum_chars / len(text)) < min_alphanum_ratio:
        return False
        
    # Rule 3: Must have a reasonable average word length
    words = text.split()
    if not words or (sum(len(word) for word in words) / len(words)) < min_avg_word_len:
        return False
        
    return True

def extract_text_from_image(file: str) -> str:
    '''
    Uses pymupdf to perform OCR on an image and then filters the result
    to determine if it's meaningful text or noise.
    '''
    try:
        pmap = pymupdf.Pixmap(str(file))
        doc = pymupdf.open("pdf", pmap.pdfocr_tobytes())
        res = "".join([page.get_text() for page in doc.pages()]).strip()
        
        # After getting the OCR result, check if it's meaningful.
        if is_meaningful_text(res):
            return res
        else:
            # If it's noise, return an empty string so it doesn't pollute the document.
            return ""
    except Exception as e:
        print(f"Warning: Could not perform OCR on image {file}: {str(e)}")
        return f"[Image: {Path(file).name} - OCR not available]"

def is_table_of_contents_page(page: pymupdf.Page, toc_line_threshold: int = 5) -> bool:
    """
    Heuristically checks if a page is a Table of Contents using multiple signals.
    """
    # Signal 1: Check for a title like "Contents" or "Table of Contents"
    # We check the first few blocks of text on the page for a common ToC heading.
    # Slicing the list of blocks is a version-compatible way to get the top 10.
    top_blocks = page.get_text("blocks")[:10]
    for block in top_blocks:
        block_text = block[4].lower().strip()
        if "contents" in block_text or "table of contents" in block_text:
            return True

    # Signal 2: Check for a high density of lines ending in page numbers.
    # This is a strong indicator of a ToC.
    toc_pattern = re.compile(r'.*[\s.]{3,}\s*\d+\s*$')
    lines = page.get_text("text").split('\n')
    toc_line_count = sum(1 for line in lines if toc_pattern.match(line.strip()))
    
    # If either signal is present, we classify it as a ToC page.
    return toc_line_count > toc_line_threshold

def replace_image_tags(md: str, image_folder: str) -> str:
    '''
    Looks for all image tags that contain the filename of the image extracted from that part of the markdown output
    and replaces them with the extracted text from the image file
    '''
    def replace_tag(match):
        full_image_path = Path(image_folder) / match.group(1)
        return extract_text_from_image(full_image_path)

    return re.sub(r"!\[\]\((.*?)\)", replace_tag, md)

def pdf_to_md(file: str) -> str:
    '''
    Gets the markdown output from the PDF, ignoring headers, footers, and ToC pages.
    This is done by creating a temporary, cropped PDF file on disk.
    '''
    temp_pdf_path = None  # Initialize path to None
    try:
        original_doc = pymupdf.open(file)
        
        temp_file_handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        temp_pdf_path = temp_file_handle.name
        temp_file_handle.close()

        cropped_doc = pymupdf.open()
        for page in original_doc:
            # Heuristically check if the page is a Table of Contents and skip it if so.
            if is_table_of_contents_page(page):
                print(f"Skipping Page {page.number + 1} as it appears to be a Table of Contents.")
                continue

            content_area = pymupdf.Rect(page.rect.x0, page.rect.y0 * 1.1, page.rect.x1, page.rect.y1 * 0.9)
            new_page = cropped_doc.new_page(width=page.rect.width, height=page.rect.height)
            new_page.show_pdf_page(new_page.rect, original_doc, page.number, clip=content_area)
        
        cropped_doc.save(temp_pdf_path)
        cropped_doc.close()
        original_doc.close()

        with tempfile.TemporaryDirectory() as image_folder:
            md = pymupdf4llm.to_markdown(
                doc=temp_pdf_path, 
                write_images=True, 
                image_path=image_folder, 
                table_strategy="lines"
            )
            md = replace_image_tags(md, image_folder)
        
        return md
    finally:
        # Ensure the temporary PDF file is always deleted, even if errors occur.
        if temp_pdf_path and os.path.exists(temp_pdf_path):
            os.remove(temp_pdf_path)

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
            # Convert dataframe directly to a Markdown table string
            markdown_text += df.to_markdown(index=False)
            markdown_text += "\n\n"
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
