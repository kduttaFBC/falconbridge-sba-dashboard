"""Settings for the browser prototype. Every path can be overridden by an environment variable so the same code can
later run on Render with a persistent disk (nothing is deployed in this phase)."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATOR_DIR = PROJECT_ROOT / "bank_workbook_generator"
APP_DATA_DIR = Path(os.environ.get("FBX_APP_DATA", PROJECT_ROOT / "app_data")).resolve()
JOBS_DIR = APP_DATA_DIR / "jobs"
AUDIT_LOG = APP_DATA_DIR / "audit.log"

HOST = os.environ.get("FBX_HOST", "127.0.0.1")
PORT = int(os.environ.get("FBX_PORT", "8000"))
AUTH_MODE = os.environ.get("FBX_AUTH_MODE", "local")     # "local": single user, loopback only; "preview": named users
PREVIEW_USERS_SPEC = os.environ.get("FBX_PREVIEW_USERS", "")   # preview mode: user:scrypt-hash entries (Render secret)
LOCAL_USER = "local"
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"] + [
    h.strip() for h in os.environ.get("FBX_ALLOWED_HOSTS", "").split(",") if h.strip()]   # e.g. the Render hostname
JOB_TIMEOUT = int(os.environ.get("FBX_JOB_TIMEOUT", "300"))   # seconds; a generation is stopped after this

# Validated deliverables and client files: the app never writes inside these folders.
PROTECTED_DIRS = [PROJECT_ROOT / d for d in ("output", "baseline", "reference", "source_data", "foia_processing")]


def check_app_data_location():
    for d in PROTECTED_DIRS:
        d = d.resolve()
        if APP_DATA_DIR == d or d in APP_DATA_DIR.parents:
            raise SystemExit(f"FBX_APP_DATA ({APP_DATA_DIR}) must not be inside protected folder {d}.")
