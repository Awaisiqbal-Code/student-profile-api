"""Dossier (HTML + real PDF), CSV export and ZIP archives. All user data is escaped."""
import base64
import csv
import html
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from PIL import Image, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from config import settings
from models import DocumentInfo
from optimizer import DOC_RULES, rule_for
from security import signed_document_url
from storage import document_path


def _e(value: Any) -> str:
    return html.escape("-" if value is None or value == "" else str(value))


def prepared_jpeg(path: Path, max_px: int = 1600) -> Optional[bytes]:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            return buf.getvalue()
    except Exception:
        return None


def _doc_items(record: Dict[str, Any]) -> List[Tuple[str, DocumentInfo]]:
    return [(t, DocumentInfo(**d)) for t, d in record.get("documents", {}).items()]


def _profile_rows(record: Dict[str, Any]) -> List[Tuple[str, Any]]:
    return [
        ("Student ID", record.get("student_id")),
        ("Full Name", record.get("full_name")),
        ("Email", record.get("email")),
        ("Phone", record.get("phone")),
        ("Status", record.get("status")),
        ("Completion", f"{record.get('completion_score', 0)}%"),
        ("Submitted At", record.get("submitted_at")),
        ("Reviewed At", record.get("reviewed_at") or "Pending"),
        ("Admin Notes", record.get("admin_notes")),
    ]


DOSSIER_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;background:#f8fafc;color:#0f172a;margin:0;padding:20px;font-size:13px}
.wrap{max-width:820px;margin:0 auto;background:#fff;padding:28px;border:1px solid #e2e8f0;border-radius:12px}
.bar{display:flex;justify-content:space-between;align-items:center;background:#0f172a;color:#fff;padding:10px 16px;border-radius:8px;margin-bottom:18px}
.bar button{background:#22c55e;color:#fff;border:0;padding:8px 16px;border-radius:6px;font-weight:700;cursor:pointer}
h2{margin:0 0 4px}.muted{color:#64748b;font-size:12px}
table{width:100%;border-collapse:collapse;margin:12px 0}th,td{border:1px solid #e2e8f0;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#f1f5f9;width:28%}.flag{background:#fff7ed;border-left:4px solid #f97316;padding:6px 10px;margin:4px 0}
.doc{border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin:16px 0;page-break-inside:avoid}
.doc img{max-width:100%;max-height:520px;display:block;margin:10px auto;border:1px solid #cbd5e1;border-radius:6px}
@media print{body{background:#fff;padding:0}.bar{display:none}.wrap{border:0}}
"""


def build_dossier_html(record: Dict[str, Any], base_url: str) -> str:
    sid = record["student_id"]
    audits = {a["doc_type"]: a for a in record.get("document_audits", [])}

    rows = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in _profile_rows(record))
    flags = "".join(f'<div class="flag">{_e(f["issue_description"])}</div>' for f in record.get("review_flags", []))
    flags_html = f"<h3>Review Flags</h3>{flags}" if flags else ""

    sections = []
    for i, (doc_type, doc) in enumerate(_doc_items(record), start=1):
        label = rule_for(doc_type)["label"]
        audit = audits.get(doc_type, {})
        if doc.content_type.startswith("image/"):
            data = prepared_jpeg(document_path(sid, doc))
            media = (f'<img src="data:image/jpeg;base64,{base64.b64encode(data).decode()}" alt="{_e(label)}">'
                     if data else '<p class="muted">Image could not be rendered.</p>')
        else:
            url = signed_document_url(base_url, sid, doc_type)
            media = (f'<p>PDF document - <a href="{_e(url)}" target="_blank" rel="noopener">open original</a> '
                     f'<span class="muted">(link valid {settings.SIGNED_URL_TTL_HOURS}h)</span></p>')
        sections.append(
            f'<div class="doc"><h3>{i}. {_e(label)}</h3>'
            f'<p class="muted">File: {_e(doc.original_name)} | Audit: {_e(audit.get("status"))} - {_e(audit.get("remarks"))}</p>'
            f"{media}</div>"
        )
    docs_html = "".join(sections) or '<p class="muted">No documents uploaded.</p>'

    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>"
        f"<title>Dossier - {_e(sid)}</title><style>{DOSSIER_CSS}</style></head><body>"
        "<div class='bar'><strong>Applicant Dossier</strong>"
        "<button onclick='window.print()'>Print / Save as PDF</button></div>"
        "<div class='wrap'><h2>Student Scholarship Verification Dossier</h2>"
        "<p class='muted'>Candidate record and document inspection report</p>"
        f"<table>{rows}</table>{extracted_html(record)}{flags_html}"
        f"<h3>Uploaded Documents ({len(sections)})</h3>{docs_html}"
        "</div></body></html>"
    )


def build_dossier_pdf(record: Dict[str, Any]) -> bytes:
    sid = record["student_id"]
    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=9, leading=12)
    audits = {a["doc_type"]: a for a in record.get("document_audits", [])}

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm, title=f"Dossier {sid}")

    story: List[Any] = [Paragraph("Student Scholarship Verification Dossier", styles["Title"]), Spacer(1, 4 * mm)]
    table = Table(
        [[Paragraph(f"<b>{_e(k)}</b>", small), Paragraph(_e(v), small)] for k, v in _profile_rows(record)],
        colWidths=[45 * mm, 135 * mm],
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story += [table, Spacer(1, 6 * mm)]
    story += extracted_pdf_flowables(record, small, styles)

    flags = record.get("review_flags", [])
    if flags:
        story.append(Paragraph("Review Flags", styles["Heading3"]))
        story += [Paragraph("&bull; " + _e(f["issue_description"]), small) for f in flags]

    max_w, max_h = A4[0] - 30 * mm, A4[1] - 60 * mm
    for doc_type, doc in _doc_items(record):
        audit = audits.get(doc_type, {})
        story += [
            PageBreak(),
            Paragraph(_e(rule_for(doc_type)["label"]), styles["Heading2"]),
            Paragraph(f"File: {_e(doc.original_name)} | Audit: {_e(audit.get('status'))} - {_e(audit.get('remarks'))}", small),
            Spacer(1, 4 * mm),
        ]
        if doc.content_type.startswith("image/"):
            data = prepared_jpeg(document_path(sid, doc))
            if data:
                with Image.open(io.BytesIO(data)) as im:
                    w, h = im.size
                scale = min(max_w / w, max_h / h)
                story.append(RLImage(io.BytesIO(data), width=w * scale, height=h * scale))
            else:
                story.append(Paragraph("Image could not be rendered.", small))
        else:
            story.append(Paragraph("PDF original is included in the ZIP download.", small))

    pdf.build(story)
    return buf.getvalue()


def _csv_safe(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def build_approved_csv(records: List[Dict[str, Any]], base_url: str) -> str:
    doc_columns = {t: f"{t.lower()}_url" for t in DOC_RULES}
    fields = ["student_id", "full_name", "email", "phone", "status", "completion_score",
              "total_documents", *EXTRACTED_CSV_COLUMNS, *doc_columns.values(), "admin_notes", "submitted_at", "reviewed_at"]

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for rec in records:
        docs = rec.get("documents", {})
        row = {
            "student_id": rec["student_id"], "full_name": rec["full_name"], "email": rec["email"],
            "phone": rec["phone"], "status": rec["status"], "completion_score": rec.get("completion_score"),
            "total_documents": len(docs), "admin_notes": rec.get("admin_notes"),
            "submitted_at": rec.get("submitted_at"), "reviewed_at": rec.get("reviewed_at"),
        }
        row.update(extracted_csv_fields(rec))
        for doc_type, col in doc_columns.items():
            row[col] = (signed_document_url(base_url, rec["student_id"], doc_type, settings.CSV_LINK_TTL_HOURS)
                        if doc_type in docs else "")
        writer.writerow({k: _csv_safe(v) for k, v in row.items()})
    return out.getvalue()


def zip_entries(record: Dict[str, Any], folder: str = "") -> List[Tuple[Path, str]]:
    entries = []
    for doc_type, doc in _doc_items(record):
        path = document_path(record["student_id"], doc)
        if path.exists():
            entries.append((path, f"{folder}{doc_type}{path.suffix}"))
    return entries


def build_zip(entries: List[Tuple[Path, str]]):
    tmp = tempfile.SpooledTemporaryFile(max_size=50 * 1024 * 1024)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in entries:
            zf.write(path, arcname)
    tmp.seek(0)
    return tmp


def iter_file(fileobj, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    try:
        while chunk := fileobj.read(chunk_size):
            yield chunk
    finally:
        fileobj.close()


# ---------------------------------------------------------------------------
# OCR extracted data in dossier / PDF / CSV
# ---------------------------------------------------------------------------
EXTRACTED_LABELS = [
    ("cnic_number", "CNIC Number"), ("father_name", "Father Name"), ("date_of_birth", "Date of Birth"),
    ("roll_no", "Roll No"), ("marks", "Marks"), ("percentage", "Percentage"), ("cgpa", "CGPA"),
    ("passing_year", "Passing Year"), ("institution", "Board / University"), ("degree", "Degree"),
]
EXTRACTED_CSV_COLUMNS = ["cnic_number", "father_name", "date_of_birth", "percentage", "cgpa",
                         "passing_year", "institution", "cross_check_failures"]


def _ocr_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    return (record.get("extracted_data") or {}).get("summary", {}) or {}


def extracted_html(record: Dict[str, Any]) -> str:
    summary = _ocr_summary(record)
    checks = record.get("cross_checks", [])
    if not summary and not checks:
        return ""
    rows = "".join(
        f"<tr><th>{_e(label)}</th><td>{_e(summary.get(key))}</td></tr>"
        for key, label in EXTRACTED_LABELS if summary.get(key) not in (None, "")
    )
    rows += "".join(
        f"<tr><th>{_e(c['check'])}</th><td>{_e(c['status'])} - {_e(c['details'])}</td></tr>" for c in checks
    )
    return f"<h3>Extracted Document Data (OCR)</h3><table>{rows}</table>"


def extracted_pdf_flowables(record: Dict[str, Any], small, styles) -> List[Any]:
    summary = _ocr_summary(record)
    checks = record.get("cross_checks", [])
    if not summary and not checks:
        return []
    data = [[Paragraph(f"<b>{_e(label)}</b>", small), Paragraph(_e(summary.get(key)), small)]
            for key, label in EXTRACTED_LABELS if summary.get(key) not in (None, "")]
    data += [[Paragraph(f"<b>{_e(c['check'])}</b>", small),
              Paragraph(f"{_e(c['status'])} - {_e(c['details'])}", small)] for c in checks]
    table = Table(data, colWidths=[45 * mm, 135 * mm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f5f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return [Paragraph("Extracted Document Data (OCR)", styles["Heading3"]), table, Spacer(1, 6 * mm)]


def extracted_csv_fields(rec: Dict[str, Any]) -> Dict[str, Any]:
    summary = _ocr_summary(rec)
    row = {col: summary.get(col, "") for col in EXTRACTED_CSV_COLUMNS[:-1]}
    row["cross_check_failures"] = "; ".join(
        c["check"] for c in rec.get("cross_checks", []) if c.get("status") == "FAILED"
    )
    return row
