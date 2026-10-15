"""Verification engine: completeness, readability, OCR text, field extraction and cross-checks.
The engine NEVER approves or rejects.
"""
import io
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pypdf
from PIL import Image, ImageOps

import ilovepdf_ocr
from config import settings
from extractor import CrossCheck, build_summary, extract_fields, run_cross_checks
from models import DocumentAuditReport, DocumentInfo, IssueDetail
from storage import document_path

logger = logging.getLogger("student_api.optimizer")

try:
    import pytesseract
    HAS_PYTESSERACT = True
except ImportError:
    pytesseract = None
    HAS_PYTESSERACT = False

_STANDARD_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
]


def _configure_tesseract() -> bool:
    if not HAS_PYTESSERACT:
        return False
    candidates = [settings.TESSERACT_CMD, *_STANDARD_TESSERACT_PATHS, shutil.which("tesseract") or ""]
    for cmd in candidates:
        if cmd and Path(cmd).exists():
            pytesseract.pytesseract.tesseract_cmd = cmd
            try:
                pytesseract.get_tesseract_version()
                return True
            except Exception:
                continue
    return False


def _select_provider() -> str:
    wanted = settings.OCR_PROVIDER
    ilove = ilovepdf_ocr.is_configured()
    if wanted == "none":
        return "none"
    if wanted == "ilovepdf":
        if not ilove:
            logger.warning("OCR_PROVIDER=ilovepdf but ILOVEPDF_PUBLIC_KEY is empty. OCR disabled.")
        return "ilovepdf" if ilove else "none"
    tess = _configure_tesseract()
    if wanted == "tesseract":
        return "tesseract" if tess else "none"
    if ilove:
        return "ilovepdf"
    return "tesseract" if tess else "none"


OCR_PROVIDER = _select_provider()
OCR_AVAILABLE = OCR_PROVIDER != "none"

CNIC_NUMBER = r"\b\d{5}[-\s]?\d{7}[-\s]?\d\b"
CNIC_FRONT_KW = ["identity card", "national identity", "pakistan", "nadra", "father name",
                 "husband name", "date of birth", "date of issue", "gender", "country of stay"]
ACADEMIC_KW = ["marks", "grade", "transcript", "marksheet", "mark sheet", "semester", "cgpa", "gpa",
               "total marks", "roll no", "roll number", "examination", "board of intermediate",
               "board of secondary", "controller of examination"]
CERT_KW = ["certificate", "degree", "awarded", "conferred", "diploma", "bachelor", "master",
           "matric", "intermediate", "secondary school", "higher secondary"]
INCOME_KW = ["income", "salary", "pay slip", "payslip", "tax", "employer", "affidavit",
             "allowance", "remuneration", "gross pay", "net pay"]
DOMICILE_KW = ["domicile", "permanent residence", "district", "prc", "tehsil", "taluka", "province"]

DOC_RULES: Dict[str, dict] = {
    "CNIC_Front": {"form_field": "cnic_front_file", "label": "CNIC (Front Side)",
                   "name_match": True, "keywords": CNIC_FRONT_KW, "patterns": [CNIC_NUMBER]},
    "CNIC_Back": {"form_field": "cnic_back_file", "label": "CNIC (Back Side)",
                  "name_match": False, "keywords": ["nadra", "registrar", "address", "pakistan", "identity"],
                  "patterns": [CNIC_NUMBER]},
    "CNIC": {"form_field": "cnic_file", "label": "CNIC (Both Sides, Single File)",
             "name_match": True, "keywords": CNIC_FRONT_KW, "patterns": [CNIC_NUMBER]},
    "Marksheet": {"form_field": "marksheet_file", "label": "Academic Marksheet / Transcript",
                  "name_match": True, "keywords": ACADEMIC_KW, "patterns": []},
    "Certificate": {"form_field": "certificate_file", "label": "Degree / Certificate",
                    "name_match": True, "keywords": CERT_KW, "patterns": []},
    "Income_Proof": {"form_field": "income_proof_file", "label": "Income Proof / Salary Slip",
                     "name_match": False, "keywords": INCOME_KW, "patterns": []},
    "Domicile": {"form_field": "domicile_file", "label": "Domicile / PRC",
                 "name_match": True, "keywords": DOMICILE_KW, "patterns": []},
}

REQUIRED_DOC_TYPES = {"CNIC_Front", "CNIC_Back", "CNIC", "Marksheet"}
COMPLETION_ITEMS = ["Profile", "CNIC_Front", "CNIC_Back", "Marksheet"]
MAX_PDF_PAGES = 5


def rule_for(doc_type: str) -> dict:
    if doc_type in DOC_RULES:
        return DOC_RULES[doc_type]
    label = doc_type.replace("Other_", "", 1).replace("_", " ").strip() or "Additional Document"
    return {"form_field": "additional_files", "label": label, "name_match": False, "keywords": [], "patterns": []}


@dataclass
class ExtractResult:
    readable: bool
    text: str = ""
    error: Optional[str] = None
    ocr_error: Optional[str] = None

    @property
    def has_text(self) -> bool:
        return len(self.text.strip()) >= 15

    @property
    def lower(self) -> str:
        return self.text.lower()


def _cache_path(path: Path) -> Path:
    return path.with_name(path.name + ".ocr.txt")


def _native_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        reader = pypdf.PdfReader(str(path))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise ValueError
            except Exception:
                raise ValueError("PDF is password protected.")
        parts = []
        for idx, page in enumerate(reader.pages):
            if idx >= MAX_PDF_PAGES:
                break
            parts.append(page.extract_text() or "")
        return " ".join(parts)
    with Image.open(path) as img:
        img.load()
    return ""


def _tesseract_image(img: Image.Image) -> str:
    try:
        return pytesseract.image_to_string(ImageOps.exif_transpose(img).convert("L"), timeout=30)
    except Exception as exc:
        logger.warning("Tesseract failed: %s", exc)
        return ""


def _tesseract_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        parts = []
        reader = pypdf.PdfReader(str(path))
        for idx, page in enumerate(reader.pages):
            if idx >= MAX_PDF_PAGES:
                break
            for img_obj in page.images:
                try:
                    with Image.open(io.BytesIO(img_obj.data)) as im:
                        parts.append(_tesseract_image(im))
                except Exception:
                    continue
        return " ".join(parts)
    with Image.open(path) as img:
        return _tesseract_image(img)


def _run_ocr(path: Path) -> str:
    if OCR_PROVIDER == "ilovepdf":
        return ilovepdf_ocr.ocr_file(path, MAX_PDF_PAGES)
    if OCR_PROVIDER == "tesseract":
        return _tesseract_file(path)
    return ""


def extract_text(path: Path, use_ocr: bool = True) -> ExtractResult:
    if not path.exists():
        return ExtractResult(False, error="File not found on server.")
    try:
        native = _native_text(path)
    except ValueError as exc:
        return ExtractResult(False, error=str(exc))
    except Exception as exc:
        return ExtractResult(False, error=f"File could not be opened ({type(exc).__name__}).")

    cache = _cache_path(path)
    if cache.exists():
        return ExtractResult(True, cache.read_text(encoding="utf-8", errors="ignore"))

    text, ocr_error = native, None
    if use_ocr and OCR_AVAILABLE and len(native.strip()) < 50:
        try:
            text = f"{native} {_run_ocr(path)}".strip()
        except ilovepdf_ocr.OcrError as exc:
            ocr_error = str(exc)
            logger.warning("OCR failed for %s: %s", path.name, exc)
        except Exception as exc:
            ocr_error = f"{type(exc).__name__}: {exc}"
            logger.warning("OCR failed for %s: %s", path.name, ocr_error)

    result = ExtractResult(True, text, ocr_error=ocr_error)
    if result.has_text:
        try:
            cache.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return result


def check_name(full_name: str, text: str) -> Tuple[bool, str]:
    tokens = [t for t in re.split(r"[\s.'-]+", full_name.lower()) if len(t) >= 3]
    if not tokens:
        return False, "name has no verifiable parts"
    words = set(re.sub(r"[^a-z0-9]+", " ", text).split())
    matched = [t for t in tokens if t in words]
    if len(matched) / len(tokens) >= 0.66:
        return True, f"Name matched ({len(matched)}/{len(tokens)} parts)."
    return False, f"student name not found in document ({len(matched)}/{len(tokens)} parts matched)"


def audit_document(
    student_id: str, doc: DocumentInfo, full_name: str
) -> Tuple[DocumentAuditReport, Optional[str], List[str], str]:
    rule = rule_for(doc.doc_type)
    label = rule["label"]
    required = doc.doc_type in REQUIRED_DOC_TYPES
    name_default = "NOT_CHECKED" if rule["name_match"] else "NOT_REQUIRED"
    has_rules = bool(rule["keywords"] or rule["patterns"])

    def report(**kw) -> DocumentAuditReport:
        return DocumentAuditReport(doc_type=doc.doc_type, **kw)

    result = extract_text(document_path(student_id, doc), use_ocr=has_rules or rule["name_match"])

    if not result.readable:
        msg = f"{label}: {result.error} Please upload a clear JPG, PNG or PDF file."
        rep = report(readable=False, content_check="NOT_CHECKED", name_check=name_default,
                     status="FAILED", remarks=result.error or "Unreadable file.")
        return (rep, msg, [], "") if required else (rep, None, [msg], "")

    if not has_rules and not rule["name_match"]:
        return report(readable=True, content_check="NOT_CHECKED", name_check="NOT_REQUIRED",
                      status="PASSED", remarks="File is readable."), None, [], result.text

    if not result.has_text:
        if result.ocr_error:
            reason = f"OCR failed: {result.ocr_error[:150]}"
        elif not OCR_AVAILABLE:
            reason = "no OCR service is configured"
        else:
            reason = "no readable text was detected"
        remark = f"Automatic content check skipped ({reason}). Admin must verify manually."
        return (report(readable=True, content_check="NOT_CHECKED", name_check=name_default,
                       status="NEEDS_MANUAL_CHECK", remarks=remark),
                None, [f"{label}: {remark}"], result.text)

    text = result.lower
    problems: List[str] = []
    notes: List[str] = []

    content_check = "NOT_CHECKED"
    if has_rules:
        ok = any(k in text for k in rule["keywords"]) or any(re.search(p, text) for p in rule["patterns"])
        content_check = "PASSED" if ok else "FAILED"
        if ok:
            notes.append("Official markers found.")
        else:
            problems.append(f"does not look like a valid {label}")

    name_check = "NOT_REQUIRED"
    if rule["name_match"]:
        ok, msg = check_name(full_name, text)
        name_check = "PASSED" if ok else "FAILED"
        (notes if ok else problems).append(msg)

    if not problems:
        return (report(readable=True, content_check=content_check, name_check=name_check,
                       status="PASSED", remarks=" ".join(notes)), None, [], result.text)

    problem_text = f"{label}: " + "; ".join(problems) + "."
    if settings.STRICT_OCR and required:
        return (report(readable=True, content_check=content_check, name_check=name_check,
                       status="FAILED", remarks=problem_text),
                problem_text + " Please upload a clear, correct document.", [], result.text)
    return (report(readable=True, content_check=content_check, name_check=name_check,
                   status="NEEDS_MANUAL_CHECK", remarks=problem_text),
            None, [problem_text + " Flagged for manual review."], result.text)


@dataclass
class VerificationOutcome:
    status: str
    completion_score: int
    missing_fields: List[str]
    issues: List[IssueDetail]
    review_flags: List[IssueDetail]
    document_audits: List[DocumentAuditReport]
    feedback_for_student: str
    extracted_data: Dict[str, Any] = field(default_factory=dict)
    cross_checks: List[CrossCheck] = field(default_factory=list)


def run_verification(student_id: str, full_name: str, documents: Dict[str, DocumentInfo]) -> VerificationOutcome:
    audits: Dict[str, DocumentAuditReport] = {}
    issues: List[IssueDetail] = []
    flags: List[IssueDetail] = []
    texts: Dict[str, str] = {}

    for doc_type, doc in documents.items():
        rep, blocking, warnings_, text = audit_document(student_id, doc, full_name)
        audits[doc_type] = rep
        if blocking:
            issues.append(IssueDetail(field_or_doc=doc_type, issue_description=blocking))
        flags.extend(IssueDetail(field_or_doc=doc_type, issue_description=w, severity="WARNING") for w in warnings_)
        if text.strip():
            texts[doc_type] = text

    extracted = {dt: extract_fields(dt, txt) for dt, txt in texts.items()}
    extracted = {k: v for k, v in extracted.items() if v}
    summary = build_summary(extracted)
    checks = run_cross_checks(extracted, summary)
    for c in checks:
        if c.status == "FAILED":
            flags.append(IssueDetail(field_or_doc="Cross-check", issue_description=f"{c.check}: {c.details}",
                                     severity="WARNING"))

    def ok(doc_type: str) -> bool:
        rep = audits.get(doc_type)
        return rep is not None and rep.status != "FAILED"

    combined = ok("CNIC")
    satisfied = {
        "Profile": True,
        "CNIC_Front": ok("CNIC_Front") or combined,
        "CNIC_Back": ok("CNIC_Back") or combined,
        "Marksheet": ok("Marksheet"),
    }

    has_combined = "CNIC" in documents
    missing = [
        item for item in ("CNIC_Front", "CNIC_Back", "Marksheet")
        if item not in documents and not (item.startswith("CNIC_") and has_combined)
    ]

    score = round(sum(satisfied.values()) / len(COMPLETION_ITEMS) * 100)
    complete = all(satisfied.values())
    status = "PENDING_REVIEW" if complete else "INCOMPLETE"

    first_name = full_name.split(" ")[0]
    if complete:
        feedback = (f"Dear {first_name}, your application is 100% complete and has been "
                    "sent to the administrator for review.")
    else:
        parts = [f"Dear {first_name}, your application is {score}% complete."]
        if missing:
            parts.append("Please upload: " + ", ".join(DOC_RULES[m]["label"] for m in missing) + ".")
        if issues:
            parts.append("Please fix: " + " ".join(i.issue_description for i in issues))
        feedback = " ".join(parts)

    return VerificationOutcome(
        status=status,
        completion_score=score,
        missing_fields=missing,
        issues=issues,
        review_flags=flags,
        document_audits=list(audits.values()),
        feedback_for_student=feedback,
        extracted_data={"summary": summary, "documents": extracted},
        cross_checks=checks,
    )
