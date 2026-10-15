"""iLovePDF / iLoveAPI OCR client.

Flow: image/PDF -> (local) PDF -> /start/pdfocr -> /upload -> /process -> /download -> text.
The remote task is deleted right after download. Original filenames are never sent.
"""
import io
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import pypdf
import requests
from PIL import Image, ImageOps

from config import settings

logger = logging.getLogger("student_api.ilovepdf")

API_BASE = "https://api.ilovepdf.com/v1"
TIMEOUT = 120

_token_lock = threading.Lock()
_token: Optional[str] = None
_token_expiry = 0.0


class OcrError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.ILOVEPDF_PUBLIC_KEY)


def _get_token(force: bool = False) -> str:
    global _token, _token_expiry
    with _token_lock:
        if not force and _token and time.time() < _token_expiry:
            return _token
        try:
            resp = requests.post(f"{API_BASE}/auth", data={"public_key": settings.ILOVEPDF_PUBLIC_KEY}, timeout=30)
        except requests.RequestException as exc:
            raise OcrError(f"auth network error ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise OcrError(f"auth failed (HTTP {resp.status_code}). Check ILOVEPDF_PUBLIC_KEY.")
        token = resp.json().get("token")
        if not token:
            raise OcrError("auth response did not contain a token.")
        _token, _token_expiry = token, time.time() + 50 * 60
        return _token


def _request(method: str, url: str, retry_auth: bool = True, **kwargs) -> requests.Response:
    timeout = kwargs.pop("timeout", TIMEOUT)
    headers = {"Authorization": f"Bearer {_get_token()}"}
    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise OcrError(f"network error ({type(exc).__name__})") from exc
    if resp.status_code == 401 and retry_auth:
        _get_token(force=True)
        return _request(method, url, retry_auth=False, timeout=timeout, **kwargs)
    if resp.status_code >= 400:
        detail = resp.text[:200].replace("\n", " ")
        raise OcrError(f"HTTP {resp.status_code}: {detail}")
    return resp


def _to_pdf_bytes(path: Path, max_pages: int) -> bytes:
    if path.suffix.lower() == ".pdf":
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            reader.decrypt("")
        elif len(reader.pages) <= max_pages:
            return path.read_bytes()
        writer = pypdf.PdfWriter()
        for idx, page in enumerate(reader.pages):
            if idx >= max_pages:
                break
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((2500, 2500))
        buf = io.BytesIO()
        im.save(buf, "PDF", resolution=200.0)
        return buf.getvalue()


def ocr_file(path: Path, max_pages: int = 5) -> str:
    if not is_configured():
        raise OcrError("ILOVEPDF_PUBLIC_KEY is not set.")

    pdf_bytes = _to_pdf_bytes(path, max_pages)
    start = _request("GET", f"{API_BASE}/start/pdfocr/{settings.ILOVEPDF_REGION}").json()
    server, task = start.get("server"), start.get("task")
    if not server or not task:
        raise OcrError("iLovePDF did not return a server/task.")
    base = f"https://{server}/v1"

    try:
        upload = _request(
            "POST", f"{base}/upload",
            data={"task": task},
            files={"file": ("document.pdf", pdf_bytes, "application/pdf")},
        ).json()
        payload = {
            "task": task,
            "tool": "pdfocr",
            "files": [{"server_filename": upload["server_filename"], "filename": "document.pdf"}],
            "ocr_languages": settings.ILOVEPDF_OCR_LANGUAGES,
        }
        result = _request("POST", f"{base}/process", json=payload).json()
        status = result.get("status")
        if status not in ("TaskSuccess", "TaskSuccessWithWarnings"):
            raise OcrError(f"OCR task ended with status {status}.")
        output = _request("GET", f"{base}/download/{task}").content
    finally:
        try:
            _request("DELETE", f"{base}/task/{task}", retry_auth=False, timeout=30)
        except Exception:
            pass

    if not output.startswith(b"%PDF-"):
        raise OcrError("unexpected download format from iLovePDF.")
    reader = pypdf.PdfReader(io.BytesIO(output))
    return " ".join((page.extract_text() or "") for page in reader.pages)
