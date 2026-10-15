# Frontend Integration Guide - v6.0.0

Base URL (local): `http://127.0.0.1:8001` - Swagger: `/docs`

## 0. Changes from v5
1. Students get an `access_token` on first submission. Save it; send `Authorization: Bearer <token>`.
2. All admin endpoints need the `X-Admin-Key` header. Never put this key in the student app.
3. `/uploads/...` URLs are gone. Files are private; use `view_url` from the admin API.
4. Only append a file to FormData if one was actually selected.
5. `missing_documents` gives `form_field` + `label` for each missing item.
6. Score: Profile, CNIC Front, CNIC Back, Marksheet = 25% each.
7. Allowed files: JPG, PNG, WEBP, PDF, max 5 MB each.

## 1. Student portal

### 1.1 Submit
```javascript
const API = "http://127.0.0.1:8001";
function addFile(fd, field, input) {
  if (input?.files?.[0]) fd.append(field, input.files[0]); // never append undefined
}
const fd = new FormData();
fd.append("student_id", "STU-2026-0042");
fd.append("full_name", "Hamza Tariq");
fd.append("email", "hamza@example.com");
fd.append("phone", "+923001234567");
addFile(fd, "cnic_front_file", cnicFrontInput);
addFile(fd, "cnic_back_file", cnicBackInput);
addFile(fd, "marksheet_file", marksheetInput);
addFile(fd, "certificate_file", certificateInput);  // optional
addFile(fd, "income_proof_file", incomeInput);      // optional
addFile(fd, "domicile_file", domicileInput);        // optional

const res = await fetch(`${API}/api/v1/optimize-profile-upload`, { method: "POST", body: fd });
const data = await res.json();
if (!res.ok) showErrors(data.detail);  // 422 = [{field, message}], 413/415 = file problems
saveStudentToken(data.access_token);   // shown ONLY ONCE
```

### 1.2 Status
```javascript
const s = await (await fetch(`${API}/api/v1/student/status/${studentId}`, {
  headers: { Authorization: `Bearer ${token}` },
})).json();
// s.status, s.completion_score, s.notification, s.feedback_for_student
// s.missing_documents: [{doc_type, form_field, label}]
// s.issues: show these too (e.g. unreadable file)
// s.admin_notes, s.can_edit (false after APPROVED/REJECTED)
```

### 1.3 Add missing documents
```javascript
const fd = new FormData();
addFile(fd, "marksheet_file", marksheetInput);
await fetch(`${API}/api/v1/student/${studentId}/add-documents`, {
  method: "POST", headers: { Authorization: `Bearer ${token}` }, body: fd,
});
```

### 1.4 Fix profile
```javascript
await fetch(`${API}/api/v1/student/${studentId}/profile`, {
  method: "PATCH",
  headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  body: JSON.stringify({ phone: "+923009876543" }),
});
```

## 2. Admin dashboard (header `X-Admin-Key`)
```javascript
const ADMIN_HEADERS = { "X-Admin-Key": adminKey };
await fetch(`${API}/api/v1/admin/applications?status_filter=PENDING_REVIEW`, { headers: ADMIN_HEADERS });
await fetch(`${API}/api/v1/admin/applications/${id}`, { headers: ADMIN_HEADERS });
await fetch(`${API}/api/v1/admin/applications/${id}/decision`, {
  method: "POST",
  headers: { ...ADMIN_HEADERS, "Content-Type": "application/json" },
  body: JSON.stringify({ status: "APPROVED", admin_notes: "Originals verified." }),
});
// admin_notes REQUIRED for REJECTED / NEEDS_REVISION; 409 if not PENDING_REVIEW
```

### Protected downloads (links cannot send headers -> use blob)
```javascript
async function openProtected(path, filename) {
  const res = await fetch(`${API}${path}`, { headers: ADMIN_HEADERS });
  if (!res.ok) throw new Error(await res.text());
  const url = URL.createObjectURL(await res.blob());
  if (filename) { const a = document.createElement("a"); a.href = url; a.download = filename; a.click(); }
  else window.open(url, "_blank");
}
openProtected(`/api/v1/admin/applications/${id}/dossier`);
openProtected(`/api/v1/admin/applications/${id}/download-pdf`, `${id}_dossier.pdf`);
openProtected(`/api/v1/admin/applications/${id}/download-zip`, `${id}_documents.zip`);
openProtected(`/api/v1/export-approved-csv`, "approved_students.csv");
openProtected(`/api/v1/admin/export-all-approved-zip`, "approved_students.zip");
```
CSV document links are signed and valid for 7 days.
Lost student token: `POST /api/v1/admin/applications/{id}/reset-token`.

## 3. Endpoints
| Method | Endpoint | Auth |
|---|---|---|
| POST | /api/v1/optimize-profile-upload | none (new) / Bearer (resubmit) |
| GET | /api/v1/student/status/{id} | Bearer |
| POST | /api/v1/student/{id}/add-documents | Bearer |
| PATCH | /api/v1/student/{id}/profile | Bearer |
| GET | /api/v1/admin/applications | Admin |
| GET | /api/v1/admin/applications/{id} | Admin |
| POST | /api/v1/admin/applications/{id}/decision | Admin |
| POST | /api/v1/admin/applications/{id}/reset-token | Admin |
| GET | /api/v1/admin/applications/{id}/documents/{doc_type} | Admin or signed link |
| GET | /api/v1/admin/applications/{id}/dossier | Admin |
| GET | /api/v1/admin/applications/{id}/download-pdf | Admin |
| GET | /api/v1/admin/applications/{id}/download-zip | Admin |
| GET | /api/v1/admin/export-all-approved-zip | Admin |
| GET | /api/v1/export-approved-csv | Admin |
| GET | /api/health | none |

## 4. Error codes
| Code | Meaning |
|---|---|
| 400 | No files / empty file / too many extra files |
| 401 | Wrong or missing token / admin key |
| 404 | Student or document not found |
| 409 | Application locked, or approving a non-PENDING_REVIEW app |
| 413 | File too large |
| 415 | File type not allowed / corrupted / content mismatch |
| 422 | Invalid name, email, phone or student_id |


## 5. OCR extracted data (admin, v6.1)
`GET /api/v1/admin/applications/{id}` now also returns:
- `extracted_data.summary`: cnic_number, father_name, date_of_birth, roll_no, marks, percentage, cgpa, passing_year, institution, degree
- `extracted_data.documents`: the same fields per document
- `cross_checks`: `[{check, status: PASSED | FAILED | NOT_AVAILABLE, details}]`

The review queue rows include `cross_check_failures` (count). Show a warning badge when it is > 0.
The dossier, PDF and CSV include the extracted data automatically.
