"""Pydantic v2 schemas with strict input validation."""
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

ApplicationStatus = Literal["INCOMPLETE", "PENDING_REVIEW", "APPROVED", "NEEDS_REVISION", "REJECTED"]
DecisionStatus = Literal["APPROVED", "NEEDS_REVISION", "REJECTED"]

STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]*$")
PHONE_RE = re.compile(r"^\+[1-9]\d{9,14}$")


def clean_student_id(value: str) -> str:
    value = value.strip()
    if not STUDENT_ID_RE.match(value):
        raise ValueError("student_id must be 3-40 characters using letters, numbers, '-' or '_' only.")
    return value


def clean_full_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip())
    if not 3 <= len(value) <= 60:
        raise ValueError("Full name must be between 3 and 60 characters.")
    if not NAME_RE.match(value):
        raise ValueError("Full name may only contain English letters, spaces, hyphens, apostrophes and periods.")
    if re.search(r"(.)\1{3,}", value.lower()):
        raise ValueError("Full name contains suspicious repeated characters.")
    return value


def clean_phone(value: str) -> str:
    value = re.sub(r"[\s()-]", "", value.strip())
    if not PHONE_RE.match(value):
        raise ValueError("Phone must be in international format starting with '+', e.g. +923001234567.")
    return value


class ProfileInput(BaseModel):
    """Bio-data submitted with the application form."""

    student_id: str
    full_name: str
    email: EmailStr
    phone: str

    @field_validator("student_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return clean_student_id(v)

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return clean_full_name(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return clean_phone(v)


class ProfileUpdate(BaseModel):
    """Partial profile update by the student (e.g. fixing a phone number)."""

    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: Optional[str]) -> Optional[str]:
        return clean_full_name(v) if v is not None else v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        return clean_phone(v) if v is not None else v


class AdminDecisionRequest(BaseModel):
    status: DecisionStatus
    admin_notes: Optional[str] = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _notes_required(self) -> "AdminDecisionRequest":
        if self.admin_notes is not None:
            self.admin_notes = self.admin_notes.strip() or None
        if self.status in ("REJECTED", "NEEDS_REVISION") and not self.admin_notes:
            raise ValueError("admin_notes is required when rejecting or requesting revision.")
        return self


class DocumentInfo(BaseModel):
    doc_type: str
    stored_name: str
    original_name: str
    content_type: str
    size_bytes: int
    uploaded_at: str


class IssueDetail(BaseModel):
    field_or_doc: str
    issue_description: str
    severity: Literal["BLOCKING", "WARNING"] = "BLOCKING"


class DocumentAuditReport(BaseModel):
    doc_type: str
    readable: bool
    content_check: Literal["PASSED", "FAILED", "NOT_CHECKED"]
    name_check: Literal["PASSED", "FAILED", "NOT_CHECKED", "NOT_REQUIRED"]
    status: Literal["PASSED", "FAILED", "NEEDS_MANUAL_CHECK"]
    remarks: str
