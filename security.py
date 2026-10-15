"""Authentication: admin API key, per-student access tokens, signed document links."""
import hashlib
import hmac
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import Header, HTTPException, status

import database as db
from config import settings


def is_valid_admin_key(key: Optional[str]) -> bool:
    return bool(key) and hmac.compare_digest(key.encode(), settings.ADMIN_API_KEY.encode())


def require_admin(x_admin_key: Optional[str] = Header(None, description="Admin API key")) -> None:
    if not is_valid_admin_key(x_admin_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing admin key (X-Admin-Key header).")


def new_student_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def check_student_token(student_id: str, authorization: Optional[str]) -> None:
    token = _bearer(authorization)
    stored = db.get_token_hash(student_id)
    if not token or not stored or not hmac.compare_digest(hash_token(token), stored):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Invalid student ID or access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_student_record(
    student_id: str,
    authorization: Optional[str] = Header(None, description="Bearer <access_token>"),
) -> Dict[str, Any]:
    check_student_token(student_id, authorization)
    record = db.get_application(student_id)
    if not record:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid student ID or access token.")
    return record


def _signature(student_id: str, doc_type: str, expires: int) -> str:
    msg = f"{student_id}:{doc_type}:{expires}".encode()
    return hmac.new(settings.SIGNING_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def signed_document_url(base_url: str, student_id: str, doc_type: str, ttl_hours: Optional[int] = None) -> str:
    expires = int(time.time()) + 3600 * (ttl_hours or settings.SIGNED_URL_TTL_HOURS)
    query = urlencode({"expires": expires, "sig": _signature(student_id, doc_type, expires)})
    return f"{base_url}/api/v1/admin/applications/{student_id}/documents/{doc_type}?{query}"


def verify_document_signature(student_id: str, doc_type: str, expires: int, sig: str) -> bool:
    if expires < time.time():
        return False
    return hmac.compare_digest(_signature(student_id, doc_type, expires), sig)
