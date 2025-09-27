# home/utils.py
import os
from PyPDF2 import PdfReader
from docx import Document as DocxDocument

def extract_text_from_file(file_path):
    _, ext = os.path.splitext(file_path.lower())
    text = ""

    try:
        if ext == ".pdf":
            reader = PdfReader(file_path)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        elif ext in [".docx"]:
            doc = DocxDocument(file_path)
            for para in doc.paragraphs:
                text += para.text + "\n"

        else:
            return "Unsupported file format."

    except Exception as e:
        return f"Error extracting text: {str(e)}"

    return text.strip()
