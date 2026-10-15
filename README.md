# Student Profile Verification API (v6.0.0)

A headless FastAPI backend for collecting scholarship application documents and assisting with verification. The system performs automated completeness and OCR-based checks, surfaces potential issues as review flags, and leaves final decisions to administrators.

Key characteristics
- Headless API — intended to be used with a separate frontend.
- Automated checks — completeness checks and optional OCR content extraction.
- Admin-driven decisions — the engine flags problems; administrators perform the final review.
- Signed, expiring URLs for CSV/dossier exports.

Contents
- Setup
- OCR (optional)
- Production checklist
- Authentication
- Integration examples
- OCR via iLovePDF
- Privacy notes

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # set ADMIN_API_KEY and SIGNING_SECRET
python main.py              # then visit: http://127.0.0.1:8001/docs
```

## OCR (optional, recommended)
- Windows: install Tesseract (UB-Mannheim build recommended). The service auto-detects Tesseract in Program Files.
- Ubuntu/Debian: `sudo apt install tesseract-ocr`

If Tesseract is not available, uploads are accepted but images will be flagged as NEEDS_MANUAL_CHECK for admin review. Check `GET /api/health` -> `ocr_available` to see if OCR is enabled. Set `STRICT_OCR=true` to make failed CNIC/marksheet content checks block the application.

## Production checklist
- Set `APP_ENV=production`, and choose strong values for `ADMIN_API_KEY` and `SIGNING_SECRET`.
- Configure `ALLOWED_ORIGINS` with your frontend domains. Set `PUBLIC_BASE_URL` when running behind a proxy.
- Use a persistent disk for `storage/` and `data/` (these directories are ephemeral on some hosts and will be wiped on redeploy).
- Serve over HTTPS; configure proxy limits for uploads (e.g., nginx `client_max_body_size 40m`).
- Regularly back up `data/applications.db` and the `storage/` directory.

## Authentication
- Student: `Authorization: Bearer <access_token>` (an access token is returned on first submission).
- Admin: `X-Admin-Key: <ADMIN_API_KEY>`.
- CSV / dossier links: signed, time-limited URLs.

See INTEGRATION_GUIDE.md for example frontend workflows and request examples.

## OCR with iLovePDF (optional)
1. Create a free account at https://www.iloveapi.com/signup and copy your project Public Key.
2. Add `ILOVEPDF_PUBLIC_KEY=project_public_...` to your `.env` and restart the server.
3. Confirm in `GET /api/health` that `"ocr_provider": "ilovepdf"`.

Notes:
- Text PDFs are parsed locally for free. Scanned PDFs and photos are sent to iLovePDF OCR (max 5 pages per document).
- Original filenames are never sent to the OCR provider; remote tasks are deleted after download.
- OCR results are cached per document to avoid repeated usage of OCR credits.
- Extracted fields include CNIC number, father name, date of birth, roll number, marks, percentage, CGPA, passing year, board/university, and degree.
- The system cross-checks fields (e.g., CNIC front vs back, CNIC vs marksheet). Failed checks are presented as admin review flags and do not auto-reject applications.

## Privacy
When using third-party OCR (e.g., iLovePDF), student documents may be processed on their servers in the configured `ILOVEPDF_REGION`. Include this in your student consent and privacy notice.

## Notes and recommendations
- The application focuses on flagging and assisting reviews; it is not intended to make irrevocable decisions without human oversight.
- Configure monitoring and backups before moving to production.
- For integration examples and more details, refer to INTEGRATION_GUIDE.md.
