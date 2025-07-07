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
        # Heuristic: If result is too short, or mostly non-alphabetic, or has excessive repeated chars, treat as non-text image
        cleaned = res.strip()
        # Too short or no alphabetic chars
        if len(cleaned) < min_len or not any(c.isalpha() for c in cleaned):
            return f"[Image: {Path(file).name} - no text detected]"
        import collections
        char_counts = collections.Counter(cleaned)
        most_common = char_counts.most_common(1)[0][1] if char_counts else 0
        if most_common > 0.5 * len(cleaned):
            return f"[Image: {Path(file).name} - no text detected]"
        words = cleaned.split()
        non_alpha_words = sum(1 for w in words if not any(c.isalpha() for c in w))
        if len(words) > 0 and non_alpha_words / len(words) > 0.5:
            return f"[Image: {Path(file).name} - no text detected]"
        short_words = [w for w in words if len(w) <= 2]
        if len(words) > 0 and len(short_words) / len(words) > 0.7:
            return f"[Image: {Path(file).name} - no text detected]"
        unique_words = set(words)
        if len(words) > 0 and len(unique_words) / len(words) < 0.3:
            return f"[Image: {Path(file).name} - no text detected]"
        alpha_word_count = sum(1 for w in words if any(c.isalpha() for c in w) and len(w) > 2)
        if len(words) > 0 and alpha_word_count / len(words) < 0.2:
            return f"[Image: {Path(file).name} - no text detected]"
        # Stricter: If most words are uppercase and short, likely not real text
        upper_short = [w for w in words if w.isupper() and len(w) <= 3]
        if len(words) > 0 and len(upper_short) / len(words) > 0.6:
            return f"[Image: {Path(file).name} - no text detected]"
        # Stricter: If average word length is very low, likely not real text
        avg_word_len = sum(len(w) for w in words) / len(words) if words else 0
        if avg_word_len < 2.5:
            return f"[Image: {Path(file).name} - no text detected]"
        # Stricter: If more than 60% of characters are non-alphanumeric
        non_alnum = sum(1 for c in cleaned if not c.isalnum() and not c.isspace())
        if len(cleaned) > 0 and non_alnum / len(cleaned) > 0.6:
            return f"[Image: {Path(file).name} - no text detected]"
        # Stricter: If more than 2/3 of lines are very short (<=3 chars)
        lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
        short_lines = [l for l in lines if len(l) <= 3]
        if len(lines) > 0 and len(short_lines) / len(lines) > 0.66:
            return f"[Image: {Path(file).name} - no text detected]"
        return cleaned
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
 
 
def main():

    """

    Main function to process the PDF file and convert it to markdown

    """

    pdf_file = "D:\Springboot_project\springboot-layered-app\GenAI_Demo\.vscode\.github\AI_Security.pdf"

    try:

        print(f"Processing PDF: {pdf_file}")

        markdown_content = pdf_to_md(pdf_file)

        # Save the markdown content to a file

        output_file = "Mastering_AI_Agents.md"

        with open(output_file, 'w', encoding='utf-8') as f:

            f.write(markdown_content)

        print(f"Successfully converted PDF to markdown. Output saved as: {output_file}")

        print(f"Markdown content preview (first 500 characters):")

        print("-" * 50)

        print(markdown_content[:500] + "..." if len(markdown_content) > 500 else markdown_content)

    except FileNotFoundError:

        print(f"Error: PDF file '{pdf_file}' not found. Please check the full path and ensure the file exists at the specified location.")

    except Exception as e:

        if "no such file" in str(e):

            print(f"Error: PDF file '{pdf_file}' not found or cannot be opened. Please check the full path and ensure the file exists at the specified location.")

        else:

            print(f"Error processing PDF: {str(e)}")
 
 
if __name__ == "__main__":

    main()
