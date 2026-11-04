"""Documented source-data notes shown with a bank's analysis.

Each note records a verified difference between FalconBridge's historical source and the public SBA FOIA release used
here; nothing in the data or the formulas is changed. The notes are client-specific (they quote FalconBridge workbook
figures and identify loans), so they are kept outside the code and outside Git, in a protected JSON file:
  FBX_SOURCE_NOTES (default: protected_config/source_notes.json; on Render /var/data/config/source_notes.json).
Every stated fact is re-checked by tests/test_webapp.py::test_source_note_disclosure_facts.
"""
import json
import os
import sys
from pathlib import Path

from . import settings

PATH = Path(os.environ.get("FBX_SOURCE_NOTES", settings.PROJECT_ROOT / "protected_config" / "source_notes.json"))
_cache = {}


def notes():
    """{(period, cert): note} from the protected file; empty (with a warning) when the file is absent."""
    key = (str(PATH), PATH.stat().st_mtime if PATH.exists() else None)
    if key not in _cache:
        if not PATH.exists():
            print(f"WARNING: source-notes file {PATH} not found; no source-data notes will be shown.", file=sys.stderr)
            _cache[key] = {}
        else:
            data = json.loads(PATH.read_text(encoding="utf-8"))
            _cache[key] = {(n["period"], n["cert"]): n for n in data["notes"]}
    return _cache[key]


def for_bank(period, cert, payload):
    """Notes for this analysis, with the dashboard's own figures filled in (All SBA 7(a) row-6 values)."""
    n = notes().get((period, cert or ""))
    if not n:
        return []
    bm = payload["benchmark"]["total"]["total"]
    loans = "; ".join(f"{x['borrower']} - FalconBridge {x['falconbridge'][0]} {x['falconbridge'][1]}, public FOIA "
                      f"{x['public'][0]} {x['public'][1]}" for x in n["loans"])
    values = {"benchmark_loans": bm["loans"], "benchmark_cum_def_pct": f"{bm['cum_def'] * 100:.2f}%",
              "falconbridge_benchmark_loans": n["falconbridge_benchmark_loans"],
              "falconbridge_benchmark_cum_def_pct": f"{n['falconbridge_benchmark_cum_def'] * 100:.2f}%"}
    short = n["short_template"].format(**values)
    detail = n["detail_template"].format(short=short, loans=loans, **values)
    facts = {k: n[k] for k in ("title", "loans", "falconbridge_benchmark_loans", "falconbridge_benchmark_cum_def",
                               "public_benchmark_loans")}
    return [{"title": n["title"], "short": short, "detail": detail, "facts": facts,
             "dashboard_benchmark_loans": bm["loans"], "dashboard_benchmark_cum_def": bm["cum_def"]}]
