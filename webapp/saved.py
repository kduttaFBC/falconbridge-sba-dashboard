"""Saved analyses: app_data/analyses/<id>/analysis.json - the criteria, the frozen dashboard figures, the source release
and calculation version, and the workbook job generated for exactly the same criteria. A saved analysis is never
recomputed or overwritten; every new analysis gets a new id."""
import json
import uuid
from datetime import datetime

from . import settings
from .jobs import JOB_ID

DIR = settings.APP_DATA_DIR / "analyses"


def save(owner, payload, job_id, view):
    a = {"id": uuid.uuid4().hex, "owner": owner, "created": datetime.now().isoformat(timespec="seconds"),
         "criteria": payload["criteria"], "view": view, "job_id": job_id, "payload": payload}
    d = DIR / a["id"]
    d.mkdir(parents=True, exist_ok=False)
    tmp = d / "analysis.json.tmp"
    tmp.write_text(json.dumps(a, default=str), encoding="utf-8")
    tmp.replace(d / "analysis.json")
    return a


def load(analysis_id):
    if not JOB_ID.match(analysis_id or ""):
        return None
    f = DIR / analysis_id / "analysis.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


def listing(owner):
    out = []
    if DIR.is_dir():
        for d in DIR.iterdir():
            a = load(d.name)
            if a and a["owner"] == owner:
                a.pop("payload", None)
                out.append(a)
    return sorted(out, key=lambda a: a["created"], reverse=True)
