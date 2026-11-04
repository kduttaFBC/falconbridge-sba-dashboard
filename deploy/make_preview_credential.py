"""Create a preview sign-in for one named user: prints a random password (give it to the user through a separate
channel, e.g. by phone) and the FBX_PREVIEW_USERS entry to paste into Render's secret environment variable.
Only the entry (a scrypt hash) is stored anywhere; the password is shown once and never saved.
Usage: python deploy/make_preview_credential.py <username>
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webapp.preview_auth import hash_password  # noqa: E402

if len(sys.argv) != 2 or ":" in sys.argv[1] or ";" in sys.argv[1]:
    sys.exit("usage: python deploy/make_preview_credential.py <username without ':' or ';'>")
user = sys.argv[1]
password = secrets.token_urlsafe(18)
print(f"username: {user}\npassword: {password}\nFBX_PREVIEW_USERS entry: {user}:{hash_password(password, secrets.token_bytes(16))}")
