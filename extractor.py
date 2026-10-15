"""Field extraction from document text + cross-document consistency checks."""
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel

from config import settings


class CrossCheck(BaseModel):
    check: str
    status: Literal["PASSED", "FAILED", "NOT_AVAILABLE"]
    details: str


CNIC_RE = re.compile(r"(?<!\d)(\d{5})[-\s]?(\d{7})[-\s]?(\d)(?!\d)")
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-]((?:19|20)\d{2})(?!\d)")
DOB_LABEL_RE = re.compile(
    r"(?:date\s*of\s*birth|d\.?\s*o\.?\s*b\.?|birth\s*date)\s*[:\-]?\s*(\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2})", re.I
)
FATHER_RE = re.compile(r"(?:father|husband)(?:'s)?\s*name\s*[:\-]?\s*([A-Za-z][A-Za-z .]{2,40})", re.I)
RELATION_RE = re.compile(r"\b(?:s/o|d/o|w/o|son\s+of|daughter\s+of)\s*[:\-.]?\s*([A-Za-z][A-Za-z .]{2,40})", re.I)
ROLL_RE = re.compile(r"roll\s*(?:no|number|#)\.?\s*[:\-]?\s*([A-Za-z0-9-]{3,15})", re.I)
CGPA_RE = re.compile(r"\bc?gpa\b\s*[:\-]?\s*([0-4](?:\.\d{1,2})?)(?!\d)", re.I)
PCT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,2})?)\s*%")
MARKS_RE = re.compile(r"(?<![\d/.-])(\d{2,4})\s*(?:/|out\s+of)\s*(\d{3,4})(?![\d/])", re.I)
YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")
INSTITUTION_RE = re.compile(r"^[^\n]*\b(?:board of|university|institute of|college)\b[^\n]*$", re.I | re.M)
DEGREE_RE = re.compile(r"\b(?:bachelor|master)s?\s+of\s+[A-Za-z ]{3,40}", re.I)
DEGREE_KEYWORDS = [
    ("secondary school certificate", "Matric (SSC)"), ("matriculation", "Matric (SSC)"),
    ("higher secondary", "Intermediate (HSSC)"), ("intermediate", "Intermediate (HSSC)"),
    ("associate degree", "Associate Degree"), ("doctor of philosophy", "PhD"), ("phd", "PhD"),
    ("mphil", "MPhil"), ("mba", "MBA"), ("msc", "MSc"), ("bsc", "BSc"),
]
NAME_STOPWORDS = {"gender", "country", "date", "identity", "holder", "signature", "stay", "birth",
                  "issue", "expiry", "number", "no", "cnic", "male", "female", "roll", "class", "group"}


def _clean_name(raw: str) -> Optional[str]:
    words: List[str] = []
    for word in raw.replace(".", " ").split():
        if word.lower() in NAME_STOPWORDS:
            break
        words.append(word)
        if len(words) == 4:
            break
    name = " ".join(words).title()
    return name if len(name) >= 3 else None


def _norm_date(d: str, m: str, y: str) -> Optional[str]:
    day, month = int(d), int(m)
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    return f"{day:02d}-{month:02d}-{y}"


def _find_cnic(text: str) -> Optional[str]:
    m = CNIC_RE.search(text)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _find_dob(text: str, fallback_earliest: bool) -> Optional[str]:
    m = DOB_LABEL_RE.search(text)
    if m:
        dm = DATE_RE.search(m.group(1))
        if dm:
            return _norm_date(*dm.groups())
    if fallback_earliest:
        dates = [(int(y), _norm_date(d, mo, y)) for d, mo, y in DATE_RE.findall(text)]
        dates = [x for x in dates if x[1]]
        if dates:
            return min(dates)[1]
    return None


def _find_father(text: str) -> Optional[str]:
    for rx in (FATHER_RE, RELATION_RE):
        m = rx.search(text)
        if m:
            name = _clean_name(m.group(1))
            if name:
                return name
    return None


def _find_marks(text: str) -> Optional[Tuple[int, int]]:
    best: Optional[Tuple[int, int]] = None
    for a, b in MARKS_RE.findall(text):
        obtained, total = int(a), int(b)
        if 100 <= total <= 2000 and not 1980 <= total <= 2049 and 0 < obtained <= total and obtained >= total * 0.2:
            if best is None or total > best[1]:
                best = (obtained, total)
    return best


def _find_degree(text: str) -> Optional[str]:
    m = DEGREE_RE.search(text)
    if m:
        return re.sub(r"\s+", " ", m.group(0)).strip().title()
    lower = text.lower()
    for keyword, label in DEGREE_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", lower):
            return label
    return None


def extract_fields(doc_type: str, text: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "cnic_number": _find_cnic(text),
        "father_name": _find_father(text),
        "date_of_birth": _find_dob(text, fallback_earliest=doc_type in ("CNIC_Front", "CNIC")),
    }
    if doc_type in ("Marksheet", "Certificate"):
        roll = ROLL_RE.search(text)
        fields["roll_no"] = roll.group(1) if roll else None
        cgpa = CGPA_RE.search(text)
        fields["cgpa"] = float(cgpa.group(1)) if cgpa else None
        marks = _find_marks(text)
        if marks:
            fields["marks"] = f"{marks[0]}/{marks[1]}"
            fields["percentage"] = round(marks[0] / marks[1] * 100, 2)
        else:
            pcts = [float(p) for p in PCT_RE.findall(text) if 0 < float(p) <= 100]
            fields["percentage"] = max(pcts) if pcts else None
        years = [int(y) for y in YEAR_RE.findall(text)]
        fields["passing_year"] = max(years) if years else None
        inst = INSTITUTION_RE.search(text)
        fields["institution"] = re.sub(r"\s+", " ", inst.group(0)).strip()[:100] if inst else None
        if doc_type == "Certificate":
            fields["degree"] = _find_degree(text)
    return {k: v for k, v in fields.items() if v not in (None, "")}


SUMMARY_PRIORITY = {
    "cnic_number": ["CNIC_Front", "CNIC", "CNIC_Back"],
    "father_name": ["CNIC_Front", "CNIC", "Marksheet", "Certificate"],
    "date_of_birth": ["CNIC_Front", "CNIC", "Marksheet", "Certificate"],
    "roll_no": ["Marksheet", "Certificate"],
    "marks": ["Marksheet"],
    "percentage": ["Marksheet", "Certificate"],
    "cgpa": ["Marksheet", "Certificate"],
    "passing_year": ["Marksheet", "Certificate"],
    "institution": ["Marksheet", "Certificate"],
    "degree": ["Certificate"],
}


def build_summary(extracted: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for field, sources in SUMMARY_PRIORITY.items():
        for source in sources:
            value = extracted.get(source, {}).get(field)
            if value not in (None, ""):
                summary[field] = value
                break
    return summary


def _names_similar(a: str, b: str) -> bool:
    ta = {t for t in re.split(r"[\s.'-]+", a.lower()) if len(t) >= 3}
    tb = {t for t in re.split(r"[\s.'-]+", b.lower()) if len(t) >= 3}
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.5


def run_cross_checks(extracted: Dict[str, Dict[str, Any]], summary: Dict[str, Any]) -> List[CrossCheck]:
    checks: List[CrossCheck] = []

    def add(name: str, ok: Optional[bool], details: str) -> None:
        status = "NOT_AVAILABLE" if ok is None else ("PASSED" if ok else "FAILED")
        checks.append(CrossCheck(check=name, status=status, details=details))

    cnic_doc = extracted.get("CNIC_Front") or extracted.get("CNIC") or {}
    front = cnic_doc.get("cnic_number")
    back = extracted.get("CNIC_Back", {}).get("cnic_number")
    if front and back:
        add("CNIC number (front vs back)", front == back, f"Front: {front} | Back: {back}")
    elif "CNIC_Front" in extracted and "CNIC_Back" in extracted:
        add("CNIC number (front vs back)", None, "Number could not be read on both sides.")

    for doc in ("Marksheet", "Certificate"):
        other = extracted.get(doc, {}).get("cnic_number")
        if other and front:
            add(f"CNIC/B-Form number on {doc}", other == front, f"CNIC: {front} | {doc}: {other}")

    cnic_father = cnic_doc.get("father_name")
    for doc in ("Marksheet", "Certificate"):
        acad_father = extracted.get(doc, {}).get("father_name")
        if cnic_father and acad_father:
            add(f"Father name (CNIC vs {doc})", _names_similar(cnic_father, acad_father),
                f"CNIC: {cnic_father} | {doc}: {acad_father}")

    cnic_dob = cnic_doc.get("date_of_birth")
    for doc in ("Marksheet", "Certificate"):
        acad_dob = extracted.get(doc, {}).get("date_of_birth")
        if cnic_dob and acad_dob:
            add(f"Date of birth (CNIC vs {doc})", cnic_dob == acad_dob, f"CNIC: {cnic_dob} | {doc}: {acad_dob}")

    if settings.MIN_PERCENTAGE > 0:
        pct = summary.get("percentage")
        if pct is None:
            add("Minimum percentage", None, "Percentage could not be read from the documents.")
        else:
            add("Minimum percentage", pct >= settings.MIN_PERCENTAGE, f"{pct}% (required {settings.MIN_PERCENTAGE}%)")

    if settings.MIN_CGPA > 0:
        cgpa = summary.get("cgpa")
        if cgpa is None:
            add("Minimum CGPA", None, "CGPA could not be read from the documents.")
        else:
            add("Minimum CGPA", cgpa >= settings.MIN_CGPA, f"{cgpa} (required {settings.MIN_CGPA})")

    return checks
