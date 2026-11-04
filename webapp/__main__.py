"""Start the local prototype:  .venv/bin/python -m webapp   (then open http://127.0.0.1:8000)"""
import ipaddress

import uvicorn

from . import settings

from .preview_auth import parse_users

if settings.AUTH_MODE == "local" and not ipaddress.ip_address(settings.HOST).is_loopback:
    raise SystemExit(f"Refusing to listen on {settings.HOST}: local mode has no sign-in and must use 127.0.0.1.")
if settings.AUTH_MODE == "preview" and not parse_users(settings.PREVIEW_USERS_SPEC):
    raise SystemExit("Refusing to start: preview mode needs FBX_PREVIEW_USERS (see deploy/make_preview_credential.py).")
if settings.AUTH_MODE not in ("local", "preview"):
    raise SystemExit(f"Refusing to start: unknown FBX_AUTH_MODE {settings.AUTH_MODE!r}.")
settings.check_app_data_location()
print(f"FalconBridge SBA FOIA prototype: http://{settings.HOST}:{settings.PORT}   (Ctrl+C to stop)")
uvicorn.run("webapp.app:app", host=settings.HOST, port=settings.PORT, workers=1, log_level="warning")
