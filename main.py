"""Student Profile Verification API (v6) - FastAPI application."""
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import ValidationError

import database as db
import reports
from config import settings
from models import AdminDecisionRequest, DocumentInfo, ProfileInput, ProfileUpdate
from optimizer import DOC_RULES, OCR_AVAILABLE, OCR_PROVIDER, rule_for, run_verification
from security import (
    check_student_token, hash_token, is_valid_admin_key, new_student_token, require_admin,
    require_student_record, signed_document_url, verify_document_signature,
)
from storage import PreparedFile, delete_document, document_path, prepare_upload, write_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("student_api")

db.init_db()
logger.info("OCR provider: %s | STRICT_OCR: %s", OCR_PROVIDER, settings.STRICT_OCR)
if settings.STRICT_OCR and not OCR_AVAILABLE:
    logger.warning("STRICT_OCR is on but Tesseract is not installed; content checks will be flagged for manual review.")

LOCKED_STATUSES = {"APPROVED", "REJECTED"}

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description="Headless scholarship document verification API. Students use a Bearer access token; admins use the X-Admin-Key header.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
    expose_headers=["Content-Disposition"],
)

admin = APIRouter(prefix="/api/v1/admin", tags=["Admin"], dependencies=[Depends(require_admin)])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def base_url(request: Request) -> str:
    return settings.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def format_errors(exc: ValidationError) -> List[Dict[str, str]]:
    return [
        {"field": ".".join(str(p) for p in err["loc"]) or "body", "message": err["msg"].replace("Value error, ", "")}
        for err in exc.errors()
    ]


def log_event(record: Dict[str, Any], event: str, by: str, notes: Optional[str] = None) -> None:
    record.setdefault("history", []).append({"at": now_iso(), "event": event, "by": by, "notes": notes})


def ensure_editable(record: Dict[str, Any]) -> None:
    if record["status"] in LOCKED_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Application is already {record['status']}. It can no longer be changed.",
        )


def get_record_or_404(student_id: str) -> Dict[str, Any]:
    record = db.get_application(student_id)
    if not record:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Student ID '{student_id}' not found.")
    return record


def store_files(record: Dict[str, Any], prepared: List[PreparedFile]) -> None:
    docs = record.setdefault("documents", {})
    for item in prepared:
        old = docs.get(item.doc_type)
        docs[item.doc_type] = write_file(record["student_id"], item).model_dump()
        if old:
            delete_document(record["student_id"], DocumentInfo(**old))


async def reverify(record: Dict[str, Any]) -> None:
    docs = {k: DocumentInfo(**v) for k, v in record.get("documents", {}).items()}
    outcome = await run_in_threadpool(run_verification, record["student_id"], record["full_name"], docs)
    record.update(
        status=outcome.status,
        completion_score=outcome.completion_score,
        missing_fields=outcome.missing_fields,
        issues=[i.model_dump() for i in outcome.issues],
        review_flags=[f.model_dump() for f in outcome.review_flags],
        document_audits=[a.model_dump() for a in outcome.document_audits],
        feedback_for_student=outcome.feedback_for_student,
        extracted_data=outcome.extracted_data,
        cross_checks=[c.model_dump() for c in outcome.cross_checks],
    )


def missing_documents(record: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {"doc_type": d, "form_field": DOC_RULES[d]["form_field"], "label": DOC_RULES[d]["label"]}
        for d in record.get("missing_fields", [])
    ]


NOTIFICATIONS = {
    "INCOMPLETE": "Your application is incomplete. Please upload or fix the items listed below.",
    "PENDING_REVIEW": "100% complete. Your application is with the administrator for review.",
    "APPROVED": "Congratulations! Your application has been approved.",
    "NEEDS_REVISION": "The administrator has requested changes. Please read the admin notes.",
    "REJECTED": "Your application was not approved. Please read the admin notes.",
}


def student_view(record: Dict[str, Any], access_token: Optional[str] = None) -> Dict[str, Any]:
    st = record["status"]
    view = {
        "student_id": record["student_id"],
        "full_name": record["full_name"],
        "email": record["email"],
        "phone": record["phone"],
        "status": st,
        "completion_score": record.get("completion_score", 0),
        "notification": NOTIFICATIONS.get(st, ""),
        "missing_fields": record.get("missing_fields", []),
        "missing_documents": missing_documents(record),
        "issues": record.get("issues", []),
        "feedback_for_student": record.get("feedback_for_student", ""),
        "admin_notes": record.get("admin_notes") if st in ("APPROVED", "NEEDS_REVISION", "REJECTED") else None,
        "uploaded_documents": [
            {"doc_type": t, "label": rule_for(t)["label"], "original_name": d["original_name"], "uploaded_at": d["uploaded_at"]}
            for t, d in record.get("documents", {}).items()
        ],
        "can_edit": st not in LOCKED_STATUSES,
        "submitted_at": record.get("submitted_at"),
        "reviewed_at": record.get("reviewed_at"),
    }
    if access_token:
        view["access_token"] = access_token
        view["access_token_note"] = "Store this token securely. It is shown only once and is required for all student requests."
    return view


def admin_view(record: Dict[str, Any], base: str) -> Dict[str, Any]:
    view = {k: v for k, v in record.items() if k != "documents"}
    view["missing_documents"] = missing_documents(record)
    view["documents"] = [
        {
            "doc_type": t,
            "label": rule_for(t)["label"],
            "original_name": d["original_name"],
            "content_type": d["content_type"],
            "size_bytes": d["size_bytes"],
            "uploaded_at": d["uploaded_at"],
            "view_url": signed_document_url(base, record["student_id"], t),
        }
        for t, d in record.get("documents", {}).items()
    ]
    return view


async def collect_files(
    cnic_front_file: Optional[UploadFile] = File(None, description="CNIC front (JPG/PNG/WEBP/PDF)"),
    cnic_back_file: Optional[UploadFile] = File(None, description="CNIC back"),
    cnic_file: Optional[UploadFile] = File(None, description="Both CNIC sides in one file (alternative to front/back)"),
    marksheet_file: Optional[UploadFile] = File(None, description="Marksheet / transcript"),
    certificate_file: Optional[UploadFile] = File(None, description="Degree / certificate (optional)"),
    income_proof_file: Optional[UploadFile] = File(None, description="Income proof (optional)"),
    domicile_file: Optional[UploadFile] = File(None, description="Domicile / PRC (optional)"),
    additional_files: List[UploadFile] = File(default=[], description="Extra documents (optional)"),
    additional_doc_types: Optional[str] = Form(None, description="Comma-separated labels for additional_files"),
) -> List[PreparedFile]:
    slots = [
        ("CNIC_Front", cnic_front_file), ("CNIC_Back", cnic_back_file), ("CNIC", cnic_file),
        ("Marksheet", marksheet_file), ("Certificate", certificate_file),
        ("Income_Proof", income_proof_file), ("Domicile", domicile_file),
    ]
    prepared: List[PreparedFile] = []
    for doc_type, upload in slots:
        item = await prepare_upload(doc_type, upload)
        if item:
            prepared.append(item)

    extras = [f for f in additional_files if f and f.filename]
    if len(extras) > settings.MAX_ADDITIONAL_FILES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Maximum {settings.MAX_ADDITIONAL_FILES} additional files allowed.")
    labels = [x.strip() for x in additional_doc_types.split(",")] if additional_doc_types else []
    for idx, upload in enumerate(extras):
        raw = labels[idx] if idx < len(labels) else ""
        label = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")[:30] or f"Document_{idx + 1}"
        item = await prepare_upload(f"Other_{label}", upload)
        if item:
            prepared.append(item)
    return prepared


@app.get("/", tags=["Info"])
def root(request: Request) -> Dict[str, Any]:
    base = base_url(request)
    return {"service": settings.APP_NAME, "version": settings.VERSION, "status": "online",
            "docs": f"{base}/docs", "redoc": f"{base}/redoc"}


@app.get("/api/health", tags=["Info"])
def health() -> Dict[str, Any]:
    return {"status": "ok", "version": settings.VERSION, "ocr_available": OCR_AVAILABLE, "ocr_provider": OCR_PROVIDER, "timestamp": now_iso()}


@app.post("/api/v1/optimize-profile-upload", tags=["Student"],
          summary="Submit a new application (or resubmit with your access token)")
async def submit_application(
    student_id: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    files: List[PreparedFile] = Depends(collect_files),
    authorization: Optional[str] = Header(None, description="Required only when resubmitting: Bearer <access_token>"),
) -> Dict[str, Any]:
    try:
        profile = ProfileInput(student_id=student_id, full_name=full_name, email=email, phone=phone)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=format_errors(exc))

    existing = db.get_application(profile.student_id)
    access_token: Optional[str] = None

    if existing:
        check_student_token(profile.student_id, authorization)
        ensure_editable(existing)
        record = existing
        record.update(full_name=profile.full_name, email=str(profile.email), phone=profile.phone)
        event = "RESUBMITTED"
    else:
        access_token = new_student_token()
        record = {
            "student_id": profile.student_id, "full_name": profile.full_name,
            "email": str(profile.email), "phone": profile.phone,
            "documents": {}, "created_at": now_iso(), "reviewed_at": None,
            "admin_notes": None, "history": [],
        }
        event = "SUBMITTED"

    store_files(record, files)
    await reverify(record)
    record["submitted_at"] = now_iso()
    log_event(record, event, "student")

    if access_token:
        try:
            db.create_application(record, hash_token(access_token))
        except db.DuplicateApplicationError:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="This student ID was registered just now. Please log in instead.")
    else:
        db.update_application(record)

    return student_view(record, access_token)


@app.get("/api/v1/student/status/{student_id}", tags=["Student"], summary="Live status, score and missing items")
def get_student_status(record: Dict[str, Any] = Depends(require_student_record)) -> Dict[str, Any]:
    return student_view(record)


@app.post("/api/v1/student/{student_id}/add-documents", tags=["Student"],
          summary="Upload only the missing / corrected documents")
async def add_documents(
    files: List[PreparedFile] = Depends(collect_files),
    record: Dict[str, Any] = Depends(require_student_record),
) -> Dict[str, Any]:
    ensure_editable(record)
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No files were attached.")
    store_files(record, files)
    await reverify(record)
    record["submitted_at"] = now_iso()
    log_event(record, "DOCUMENTS_ADDED", "student", ", ".join(f.doc_type for f in files))
    db.update_application(record)
    return student_view(record)


@app.patch("/api/v1/student/{student_id}/profile", tags=["Student"],
           summary="Fix name / email / phone without re-uploading files")
async def update_profile(
    payload: ProfileUpdate,
    record: Dict[str, Any] = Depends(require_student_record),
) -> Dict[str, Any]:
    ensure_editable(record)
    changes = payload.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Nothing to update.")
    record.update({k: str(v) for k, v in changes.items()})
    await reverify(record)
    record["submitted_at"] = now_iso()
    log_event(record, "PROFILE_UPDATED", "student", ", ".join(changes))
    db.update_application(record)
    return student_view(record)


@admin.get("/applications", summary="Review queue (lightweight rows for a table)")
def list_applications(status_filter: str = Query("PENDING_REVIEW", description="Status or ALL")) -> List[Dict[str, Any]]:
    sf = status_filter.upper()
    records = db.list_applications(None if sf == "ALL" else sf)
    return [
        {
            "student_id": r["student_id"], "full_name": r["full_name"], "email": r["email"], "phone": r["phone"],
            "status": r["status"], "completion_score": r.get("completion_score", 0),
            "missing_fields": r.get("missing_fields", []), "review_flag_count": len(r.get("review_flags", [])),
            "document_count": len(r.get("documents", {})),
            "cross_check_failures": sum(1 for c in r.get("cross_checks", []) if c.get("status") == "FAILED"),
            "submitted_at": r.get("submitted_at"), "reviewed_at": r.get("reviewed_at"),
        }
        for r in records
    ]


@admin.get("/applications/{student_id}", summary="Full application with audits, flags and document links")
def get_application(student_id: str, request: Request) -> Dict[str, Any]:
    return admin_view(get_record_or_404(student_id), base_url(request))


@admin.post("/applications/{student_id}/decision", summary="APPROVED / NEEDS_REVISION / REJECTED")
def decide(student_id: str, decision: AdminDecisionRequest, request: Request) -> Dict[str, Any]:
    record = get_record_or_404(student_id)
    current = record["status"]

    if decision.status == "APPROVED" and current != "PENDING_REVIEW":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Only PENDING_REVIEW applications can be approved (current status: {current}).",
        )

    first_name = record["full_name"].split(" ")[0]
    record.update(status=decision.status, admin_notes=decision.admin_notes, reviewed_at=now_iso())
    if decision.status == "APPROVED":
        record["feedback_for_student"] = f"Dear {first_name}, congratulations! Your application has been APPROVED."
    elif decision.status == "NEEDS_REVISION":
        record["feedback_for_student"] = (f"Dear {first_name}, the administrator requested changes: "
                                          f"{decision.admin_notes} Please upload the corrected documents.")
    else:
        record["feedback_for_student"] = (f"Dear {first_name}, we regret that your application was not approved. "
                                          f"Reason: {decision.admin_notes}")

    log_event(record, decision.status, "admin", decision.admin_notes)
    db.update_application(record)
    return {"success": True, "application": admin_view(record, base_url(request))}


@admin.post("/applications/{student_id}/reset-token", summary="Issue a new access token if a student lost theirs")
def reset_student_token(student_id: str) -> Dict[str, str]:
    record = get_record_or_404(student_id)
    token = new_student_token()
    db.set_token_hash(student_id, hash_token(token))
    log_event(record, "TOKEN_RESET", "admin")
    db.update_application(record)
    return {"student_id": student_id, "access_token": token}


@admin.get("/applications/{student_id}/dossier", response_class=HTMLResponse, summary="Printable HTML dossier")
def view_dossier(student_id: str, request: Request) -> HTMLResponse:
    html_doc = reports.build_dossier_html(get_record_or_404(student_id), base_url(request))
    return HTMLResponse(
        html_doc,
        headers={"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"},
    )


@admin.get("/applications/{student_id}/download-pdf", summary="Native PDF dossier with embedded images")
async def download_pdf(student_id: str) -> Response:
    record = get_record_or_404(student_id)
    pdf_bytes = await run_in_threadpool(reports.build_dossier_pdf, record)
    return Response(pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{student_id}_dossier.pdf"'})


@admin.get("/applications/{student_id}/download-zip", summary="All documents of one student")
def download_zip(student_id: str) -> StreamingResponse:
    entries = reports.zip_entries(get_record_or_404(student_id))
    if not entries:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No documents found for this student.")
    return StreamingResponse(
        reports.iter_file(reports.build_zip(entries)), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{student_id}_documents.zip"'},
    )


@admin.get("/export-all-approved-zip", summary="Master ZIP of all approved students")
def export_all_approved_zip() -> StreamingResponse:
    entries = []
    for rec in db.list_applications("APPROVED"):
        folder = f"{rec['student_id']}_{re.sub(r'[^A-Za-z0-9]+', '_', rec['full_name']).strip('_')}/"
        entries += reports.zip_entries(rec, folder)
    return StreamingResponse(
        reports.iter_file(reports.build_zip(entries)), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="approved_students_documents.zip"'},
    )


app.include_router(admin)


@app.get("/api/v1/export-approved-csv", tags=["Admin"], dependencies=[Depends(require_admin)],
         summary="CSV of approved students with expiring signed document links")
def export_approved_csv(request: Request) -> Response:
    content = reports.build_approved_csv(db.list_applications("APPROVED"), base_url(request))
    return Response("\ufeff" + content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="approved_students.csv"'})


@app.get("/api/v1/admin/applications/{student_id}/documents/{doc_type}", tags=["Admin"],
         summary="Open one document (admin key OR valid signed link)")
def get_document(
    student_id: str,
    doc_type: str,
    expires: Optional[int] = Query(None),
    sig: Optional[str] = Query(None),
    x_admin_key: Optional[str] = Header(None),
) -> FileResponse:
    authorized = is_valid_admin_key(x_admin_key) or (
        expires is not None and sig and verify_document_signature(student_id, doc_type, expires, sig)
    )
    if not authorized:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Admin key or a valid, unexpired signed link is required.")

    record = get_record_or_404(student_id)
    raw = record.get("documents", {}).get(doc_type)
    if not raw:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No '{doc_type}' document for this student.")
    doc = DocumentInfo(**raw)
    path = document_path(student_id, doc)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File missing on server.")
    return FileResponse(path, media_type=doc.content_type, filename=doc.original_name, content_disposition_type="inline")


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8001)),
                reload=settings.ENV != "production")
