import tempfile
import pymupdf
import pymupdf4llm
import re  
from pathlib import Path

def extract_text_from_image(file: str, min_len: int = 2) -> str:
    '''
    uses pymupdf.pixmap to read file and then the pdfocr_tobytes method to perform OCR
    '''
    try:
        pmap = pymupdf.Pixmap(str(file))
        doc = pymupdf.open("pdf", pmap.pdfocr_tobytes())
        res = "".join([page.get_text() for page in doc.pages()])
        return res if len(res) > min_len else ""
    except Exception as e:
        print(f"Warning: Could not perform OCR on image {file}: {str(e)}")
        return f"[Image: {Path(file).name} - OCR not available]"

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
    Gets the markdown output from the PDF.
    Stores the images in a separate folder which are then read and OCR'd
    the output of which is added to the markdown output
    '''
    with tempfile.TemporaryDirectory() as image_folder:
        md = pymupdf4llm.to_markdown(
            doc=str(file), write_images=True, image_path=image_folder, table_strategy="lines"
        )
        md = replace_image_tags(md, image_folder)
    return md
