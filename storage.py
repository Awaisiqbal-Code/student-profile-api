"""Safe file handling: whitelist, size limit, magic-byte check, private storage."""
import io
import re
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile, status
from PIL import Image

from config import settings
from models import DocumentInfo

Image.MAX_IMAGE_PIXELS = 60_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

ALLOWED_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass
class PreparedFile:
    doc_type: str
    original_name: str
    ext: str
    content: bytes


def _safe_original_name(name: str) -> str:
    base = Path(name).name
    return (re.sub(r"[^\w.\- ]", "_", base)[:100]).strip() or "file"


def _content_matches_extension(ext: str, content: bytes) -> bool:
    if ext == ".pdf":
        return content.startswith(b"%PDF-")
    if ext in (".jpg", ".jpeg"):
        return content.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".webp":
        return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


async def prepare_upload(doc_type: str, upload: Optional[UploadFile]) -> Optional[PreparedFile]:
    """Reads and validates an upload in memory. Nothing is written to disk here."""
    if upload is None or not upload.filename:
        return None

    ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{doc_type}: '{ext or 'unknown'}' files are not allowed. Use JPG, PNG, WEBP or PDF.",
        )

    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    content = await upload.read(limit + 1)
    await upload.close()

    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"{doc_type}: the uploaded file is empty.")
    if len(content) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{doc_type}: file is larger than {settings.MAX_UPLOAD_MB} MB.",
        )
    if not _content_matches_extension(ext, content):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{doc_type}: file content does not match its '{ext}' extension.",
        )
    if ext != ".pdf":
        try:
            with Image.open(io.BytesIO(content)) as img:
                img.verify()
        except Exception:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"{doc_type}: image is corrupted or too large to process.",
            )

    return PreparedFile(doc_type=doc_type, original_name=_safe_original_name(upload.filename), ext=ext, content=content)


def _student_dir(student_id: str) -> Path:
    folder = settings.STORAGE_DIR / student_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_file(student_id: str, prepared: PreparedFile) -> DocumentInfo:
    stored_name = f"{prepared.doc_type}_{uuid.uuid4().hex}{prepared.ext}"
    (_student_dir(student_id) / stored_name).write_bytes(prepared.content)
    return DocumentInfo(
        doc_type=prepared.doc_type,
        stored_name=stored_name,
        original_name=prepared.original_name,
        content_type=ALLOWED_TYPES[prepared.ext],
        size_bytes=len(prepared.content),
        uploaded_at=datetime.now(timezone.utc).isoformat(),
    )


def document_path(student_id: str, doc: DocumentInfo) -> Path:
    root = settings.STORAGE_DIR.resolve()
    path = (root / student_id / doc.stored_name).resolve()
    if root not in path.parents:
        raise ValueError("Invalid document path.")
    return path


def delete_document(student_id: str, doc: DocumentInfo) -> None:
    try:
        path = document_path(student_id, doc)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".ocr.txt").unlink(missing_ok=True)
    except Exception:
        pass
