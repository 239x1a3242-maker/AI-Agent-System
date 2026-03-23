"""
File processing module for the AI Agent System.
Handles extraction of text content from various file formats.
"""

import os
import io
import json
import csv
import base64
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
import uuid
import time
import httpx

# Maximum file size (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# Maximum content length per file (80KB chars)
MAX_CONTENT_LENGTH = 80000

# Maximum CSV/Excel rows
MAX_ROWS = 250

# Allowed file extensions
ALLOWED_EXTENSIONS = {
    '.txt', '.md', '.py', '.js', '.html', '.css', '.yaml', '.yml', 
    '.xml', '.sql', '.log', '.json', '.csv', '.pdf', '.docx',
    '.xlsx', '.xlsm', '.xls', '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'
}

# Upload directory
UPLOAD_DIR = Path(__file__).parent.parent.parent.parent / "workspace" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_WORKER_URL = os.getenv(
    "IMAGE_WORKER_URL",
    "https://your-image-worker.your-subdomain.workers.dev",
).strip().rstrip("/")
IMAGE_WORKER_PROMPT = os.getenv(
    "IMAGE_WORKER_PROMPT",
    "Describe this image in detail. If there is readable text in the image, transcribe it exactly.",
)


def _get_image_worker_timeout() -> float:
    raw = os.getenv("IMAGE_WORKER_TIMEOUT_SECONDS", "45").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 45.0


IMAGE_WORKER_TIMEOUT_SECONDS = _get_image_worker_timeout()


@dataclass
class FileProcessResult:
    """Result of file processing."""
    filename: str
    mime_type: str
    size: int
    content: str
    success: bool
    error: Optional[str] = None


def detect_mime_type(filename: str, content: bytes) -> str:
    """Detect MIME type from filename and content."""
    mime_type, _ = mimetypes.guess_type(filename)
    if mime_type:
        return mime_type
    
    # Check magic bytes for common formats
    if content.startswith(b'%PDF'):
        return 'application/pdf'
    elif content.startswith(b'PK'):
        return 'application/zip'  # DOCX, XLSX are ZIP-based
    elif content.startswith(b'\x89PNG'):
        return 'image/png'
    elif content.startswith(b'\xff\xd8'):
        return 'image/jpeg'
    elif content.startswith(b'GIF8'):
        return 'image/gif'
    
    return 'application/octet-stream'


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal."""
    # Remove path components
    filename = os.path.basename(filename)
    # Remove non-alphanumeric characters except dots and dashes
    filename = ''.join(c for c in filename if c.isalnum() or c in '._-')
    return filename


def save_uploaded_file(content: bytes, original_filename: str) -> Path:
    """Save uploaded file to disk with unique name."""
    timestamp = int(time.time())
    unique_id = str(uuid.uuid4())[:8]
    safe_name = sanitize_filename(original_filename)
    filename = f"{timestamp}_{unique_id}_{safe_name}"
    filepath = UPLOAD_DIR / filename
    
    with open(filepath, 'wb') as f:
        f.write(content)
    
    return filepath


def extract_text_from_txt(content: bytes) -> str:
    """Extract text from plain text files."""
    encodings = ['utf-8', 'utf-16', 'latin-1', 'cp1252', 'iso-8859-1']
    
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    
    # Fallback: decode with errors ignored
    return content.decode('utf-8', errors='ignore')


def extract_text_from_json(content: bytes) -> str:
    """Extract and format JSON content."""
    text = extract_text_from_txt(content)
    try:
        data = json.loads(text)
        return json.dumps(data, indent=2, ensure_ascii=False)
    except json.JSONDecodeError:
        return text


def extract_text_from_csv(content: bytes) -> str:
    """Extract text from CSV files."""
    text = extract_text_from_txt(content)
    lines = []
    
    try:
        reader = csv.reader(io.StringIO(text))
        for i, row in enumerate(reader):
            if i >= MAX_ROWS:
                lines.append(f"... ({MAX_ROWS}+ rows truncated)")
                break
            lines.append(' | '.join(row))
    except Exception:
        return text[:MAX_CONTENT_LENGTH]
    
    return '\n'.join(lines)


def extract_text_from_pdf(content: bytes) -> str:
    """Extract text from PDF files using PyPDF2."""
    try:
        from PyPDF2 import PdfReader
        
        reader = PdfReader(io.BytesIO(content))
        pages_text = []
        
        for i, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(f"--- Page {i + 1} ---\n{page_text}")
            except Exception as e:
                pages_text.append(f"--- Page {i + 1} ---\n[Error extracting: {str(e)}]")
        
        return '\n\n'.join(pages_text)
    except ImportError:
        return "[PDF extraction requires PyPDF2 library]"
    except Exception as e:
        return f"[Error reading PDF: {str(e)}]"


def extract_text_from_docx(content: bytes) -> str:
    """Extract text from DOCX files using python-docx."""
    try:
        from docx import Document
        
        doc = Document(io.BytesIO(content))
        paragraphs = []
        
        for para in doc.paragraphs:
            if para.text.strip():
                paragraphs.append(para.text)
        
        # Extract tables
        for table in doc.tables:
            paragraphs.append("\n--- Table ---")
            for row in table.rows:
                row_text = ' | '.join(cell.text for cell in row.cells)
                paragraphs.append(row_text)
        
        return '\n'.join(paragraphs)
    except ImportError:
        return "[DOCX extraction requires python-docx library]"
    except Exception as e:
        return f"[Error reading DOCX: {str(e)}]"


def extract_text_from_excel(content: bytes, extension: str) -> str:
    """Extract text from Excel files."""
    try:
        if extension in ['.xlsx', '.xlsm']:
            from openpyxl import load_workbook
            
            wb = load_workbook(io.BytesIO(content), data_only=True)
            sheets_text = []
            
            for sheet_name in wb.sheetnames:
                sheet = wb[sheet_name]
                sheets_text.append(f"\n=== Sheet: {sheet_name} ===")
                
                row_count = 0
                for row in sheet.iter_rows(values_only=True):
                    if row_count >= MAX_ROWS:
                        sheets_text.append(f"... ({MAX_ROWS}+ rows truncated)")
                        break
                    row_text = ' | '.join(str(cell) if cell is not None else '' for cell in row)
                    sheets_text.append(row_text)
                    row_count += 1
            
            return '\n'.join(sheets_text)
        
        elif extension == '.xls':
            import xlrd
            
            wb = xlrd.open_workbook(file_contents=content)
            sheets_text = []
            
            for sheet_idx in range(wb.nsheets):
                sheet = wb.sheet_by_index(sheet_idx)
                sheets_text.append(f"\n=== Sheet: {sheet.name} ===")
                
                for row_idx in range(min(sheet.nrows, MAX_ROWS)):
                    row_values = sheet.row_values(row_idx)
                    row_text = ' | '.join(str(cell) for cell in row_values)
                    sheets_text.append(row_text)
                
                if sheet.nrows > MAX_ROWS:
                    sheets_text.append(f"... ({MAX_ROWS}+ rows truncated)")
            
            return '\n'.join(sheets_text)
    
    except ImportError as e:
        lib_name = 'openpyxl' if 'openpyxl' in str(e) else 'xlrd'
        return f"[Excel extraction requires {lib_name} library]"
    except Exception as e:
        return f"[Error reading Excel: {str(e)}]"


def _call_image_description_worker(content: bytes, filename: str, mime_type: str) -> Tuple[Optional[str], Optional[str]]:
    """Call the Cloudflare image worker and return (description, error)."""
    worker_url = os.getenv("IMAGE_WORKER_URL", IMAGE_WORKER_URL).strip().rstrip("/")
    if not worker_url:
        return None, "IMAGE_WORKER_URL is not configured"

    endpoint = worker_url
    if not endpoint.endswith("/describe"):
        endpoint = f"{endpoint}/describe"

    payload = {
        "filename": filename,
        "mime_type": mime_type,
        "prompt": IMAGE_WORKER_PROMPT,
        "image": base64.b64encode(content).decode("ascii"),
    }

    try:
        response = httpx.post(
            endpoint,
            json=payload,
            timeout=IMAGE_WORKER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        return None, f"Image worker request failed: {str(e)}"

    description = ""
    if isinstance(data, dict):
        primary = data.get("description")
        if isinstance(primary, str) and primary.strip():
            description = primary.strip()
        elif isinstance(data.get("result"), dict):
            nested = data["result"]
            nested_description = nested.get("description")
            if isinstance(nested_description, str) and nested_description.strip():
                description = nested_description.strip()
            else:
                nested_response = nested.get("response")
                if isinstance(nested_response, str) and nested_response.strip():
                    description = nested_response.strip()
        else:
            response_text = data.get("response")
            if isinstance(response_text, str) and response_text.strip():
                description = response_text.strip()
    elif isinstance(data, str):
        description = data.strip()

    if not description:
        return None, "Image worker returned an empty description"

    return description, None


def extract_text_from_image(content: bytes, filename: str, mime_type: str) -> str:
    """Extract metadata and AI description from uploaded images."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        
        img = Image.open(io.BytesIO(content))
        info = [
            f"Image: {filename}",
            f"Format: {img.format}",
            f"Size: {img.size[0]}x{img.size[1]} pixels",
            f"Mode: {img.mode}"
        ]
        
        # Try to get EXIF data
        try:
            exif = img._getexif()
            if exif:
                exif_data = []
                for tag_id, value in exif.items():
                    tag = TAGS.get(tag_id, tag_id)
                    exif_data.append(f"{tag}: {value}")
                if exif_data:
                    info.append("EXIF Data:")
                    info.extend(exif_data)
        except Exception:
            pass

        description, worker_error = _call_image_description_worker(content, filename, mime_type)
        info.append("\nImage Description (Workers AI):")
        if description:
            info.append(description)
        else:
            info.append(f"[Unavailable: {worker_error}]")
        
        return '\n'.join(info)
    
    except ImportError:
        return "[Image processing requires Pillow library]"
    except Exception as e:
        return f"[Error reading image: {str(e)}]"


def process_file(content: bytes, filename: str) -> FileProcessResult:
    """Process a file and extract its text content."""
    print(
        f"[file_processor.py::process_file] start filename={filename} size={len(content)}",
        flush=True,
    )
    # Check file size
    if len(content) > MAX_FILE_SIZE:
        return FileProcessResult(
            filename=filename,
            mime_type='',
            size=len(content),
            content='',
            success=False,
            error=f"File too large. Maximum size is {MAX_FILE_SIZE / 1024 / 1024}MB"
        )
    
    # Check extension
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return FileProcessResult(
            filename=filename,
            mime_type='',
            size=len(content),
            content='',
            success=False,
            error=f"File type '{ext}' not supported. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    
    # Detect MIME type
    mime_type = detect_mime_type(filename, content)
    
    # Extract content based on file type
    try:
        if ext in ['.txt', '.md', '.py', '.js', '.html', '.css', '.yaml', '.yml', '.xml', '.sql', '.log']:
            extracted = extract_text_from_txt(content)
        elif ext == '.json':
            extracted = extract_text_from_json(content)
        elif ext == '.csv':
            extracted = extract_text_from_csv(content)
        elif ext == '.pdf':
            extracted = extract_text_from_pdf(content)
        elif ext == '.docx':
            extracted = extract_text_from_docx(content)
        elif ext in ['.xlsx', '.xlsm', '.xls']:
            extracted = extract_text_from_excel(content, ext)
        elif ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif']:
            extracted = extract_text_from_image(content, filename, mime_type)
        else:
            extracted = extract_text_from_txt(content)
        
        # Truncate if too long
        if len(extracted) > MAX_CONTENT_LENGTH:
            extracted = extracted[:MAX_CONTENT_LENGTH] + f"\n\n... (content truncated, {len(extracted)} chars total)"
        
        # Save file to disk
        filepath = save_uploaded_file(content, filename)
        print(
            f"[file_processor.py::process_file] success filename={filename} saved={filepath.name} chars={len(extracted)}",
            flush=True,
        )

        return FileProcessResult(
            filename=filename,
            mime_type=mime_type,
            size=len(content),
            content=extracted,
            success=True
        )
    
    except Exception as e:
        print(
            f"[file_processor.py::process_file] error filename={filename} error={str(e)}",
            flush=True,
        )
        return FileProcessResult(
            filename=filename,
            mime_type=mime_type,
            size=len(content),
            content='',
            success=False,
            error=str(e)
        )


def format_files_for_prompt(files: List[FileProcessResult]) -> str:
    """Format processed files for inclusion in LLM prompt."""
    if not files:
        return ""
    
    sections = ["=" * 50, "ATTACHED FILES", "=" * 50]
    
    for i, file in enumerate(files, 1):
        sections.append(f"\n--- File {i}: {file.filename} ---")
        sections.append(f"Type: {file.mime_type}")
        sections.append(f"Size: {file.size} bytes")
        sections.append(f"Status: {'Success' if file.success else 'Failed'}")
        
        if file.success:
            sections.append(f"\nContent:\n{file.content}")
        else:
            sections.append(f"Error: {file.error}")
        
        sections.append("-" * 40)
    
    return '\n'.join(sections)


def process_uploaded_files(files: List[Tuple[bytes, str]]) -> List[Dict[str, Any]]:
    """Process multiple uploaded files and return results."""
    print(
        f"[file_processor.py::process_uploaded_files] batch_start total_files={len(files)}",
        flush=True,
    )
    results = []
    
    for content, filename in files:
        result = process_file(content, filename)
        results.append({
            'filename': result.filename,
            'mime_type': result.mime_type,
            'size': result.size,
            'content': result.content,
            'success': result.success,
            'error': result.error
        })

    success_count = sum(1 for item in results if item.get("success"))
    print(
        f"[file_processor.py::process_uploaded_files] batch_done success={success_count} failed={len(results) - success_count}",
        flush=True,
    )

    return results
