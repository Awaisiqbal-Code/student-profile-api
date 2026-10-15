# Student Profile Verification API (v6.0.0)

Headless FastAPI backend for scholarship document collection and verification.
The engine only checks completeness and flags problems; the admin makes every final decision.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env        # set ADMIN_API_KEY and SIGNING_SECRET
python main.py              # http://127.0.0.1:8001/docs
```

## OCR (optional, recommended)
- Windows: install Tesseract (UB-Mannheim build), auto-detected in Program Files
- Ubuntu/Debian: `sudo apt install tesseract-ocr`

Without Tesseract, uploads are accepted but images are flagged NEEDS_MANUAL_CHECK for the admin.
Check `GET /api/health` -> `ocr_available`.
`STRICT_OCR=true` makes failed CNIC/Marksheet content checks block the application.

## Production checklist
- `APP_ENV=production`, strong `ADMIN_API_KEY` and `SIGNING_SECRET`
- `ALLOWED_ORIGINS` = real frontend domain(s); `PUBLIC_BASE_URL` if behind a proxy
- Persistent disk for `storage/` and `data/` (otherwise wiped on redeploy)
- HTTPS only; proxy body-size limit (e.g. nginx `client_max_body_size 40m`)
- Regular backups of `data/applications.db` and `storage/`

## Auth
| Who | How |
|---|---|
| Student | `Authorization: Bearer <access_token>` (returned once on first submission) |
| Admin | `X-Admin-Key: <ADMIN_API_KEY>` |
| CSV / dossier links | Signed, expiring URLs |

See INTEGRATION_GUIDE.md for frontend examples.


## OCR with iLovePDF (v6.1)
1. Create a free account at https://www.iloveapi.com/signup and copy your project **Public Key**.
2. Put it in `.env`: `ILOVEPDF_PUBLIC_KEY=project_public_...`
3. Restart the server. `GET /api/health` should show `"ocr_provider": "ilovepdf"`.

Text PDFs are read locally for free; scanned PDFs and photos go to iLovePDF OCR (max 5 pages,
original filenames are never sent, the remote task is deleted right after download).
OCR text is cached per document, so re-checks never spend credits twice.

Extracted fields: CNIC number, father name, date of birth, roll no, marks, percentage, CGPA,
passing year, board/university, degree. Cross-checks: CNIC front vs back, father name and DOB
CNIC vs marksheet/degree, CNIC/B-form number on marksheet, optional MIN_PERCENTAGE / MIN_CGPA.
Failed checks are review flags for the admin; they never auto-reject.

Privacy: student documents are processed on iLovePDF servers in `ILOVEPDF_REGION`.
Mention this in your student consent / privacy notice.
