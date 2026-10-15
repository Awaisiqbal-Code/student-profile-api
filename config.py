"""Central configuration loaded from environment variables / .env file."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


_load_dotenv()

ILOVEPDF_REGIONS = {"eu", "us", "fr", "de", "pl", "in", "sg"}
OCR_PROVIDERS = {"auto", "ilovepdf", "tesseract", "none"}


class Settings:
    def __init__(self) -> None:
        self.APP_NAME = "Student Profile Verification API"
        self.VERSION = "6.1.0"
        self.ENV = os.getenv("APP_ENV", "development").lower()

        self.STORAGE_DIR = Path(os.getenv("STORAGE_DIR") or BASE_DIR / "storage")
        self.DB_PATH = Path(os.getenv("DB_PATH") or BASE_DIR / "data" / "applications.db")

        self.ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
        self.SIGNING_SECRET = os.getenv("SIGNING_SECRET", "")
        self.ALLOWED_ORIGINS = [
            o.strip()
            for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
            if o.strip()
        ]
        self.PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

        self.MAX_UPLOAD_MB = _env_int("MAX_UPLOAD_MB", 5)
        self.MAX_ADDITIONAL_FILES = _env_int("MAX_ADDITIONAL_FILES", 5)
        self.STRICT_OCR = _env_bool("STRICT_OCR", False)
        self.TESSERACT_CMD = os.getenv("TESSERACT_CMD", "")

        provider = os.getenv("OCR_PROVIDER", "auto").strip().lower()
        self.OCR_PROVIDER = provider if provider in OCR_PROVIDERS else "auto"
        self.ILOVEPDF_PUBLIC_KEY = os.getenv("ILOVEPDF_PUBLIC_KEY", "").strip()
        region = os.getenv("ILOVEPDF_REGION", "eu").strip().lower()
        self.ILOVEPDF_REGION = region if region in ILOVEPDF_REGIONS else "eu"
        self.ILOVEPDF_OCR_LANGUAGES = [
            x.strip() for x in os.getenv("ILOVEPDF_OCR_LANGUAGES", "eng").split(",") if x.strip()
        ] or ["eng"]

        self.MIN_PERCENTAGE = _env_float("MIN_PERCENTAGE", 0)
        self.MIN_CGPA = _env_float("MIN_CGPA", 0)

        self.SIGNED_URL_TTL_HOURS = _env_int("SIGNED_URL_TTL_HOURS", 24)
        self.CSV_LINK_TTL_HOURS = _env_int("CSV_LINK_TTL_HOURS", 168)

        self._finalize()

    def _finalize(self) -> None:
        self.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        if not self.ADMIN_API_KEY:
            if self.ENV == "production":
                raise RuntimeError("ADMIN_API_KEY must be set in production.")
            self.ADMIN_API_KEY = secrets.token_urlsafe(24)
            print(f"\n[WARNING] ADMIN_API_KEY not set. Temporary dev key (changes on restart): {self.ADMIN_API_KEY}\n")

        if not self.SIGNING_SECRET:
            if self.ENV == "production":
                raise RuntimeError("SIGNING_SECRET must be set in production.")
            self.SIGNING_SECRET = self.ADMIN_API_KEY


settings = Settings()
