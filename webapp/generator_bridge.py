"""Calls the existing, validated generator unchanged (bank_workbook_generator/). No financial logic lives here."""
import hashlib
import re
import sys
import threading
import zipfile
from datetime import date

from openpyxl import load_workbook

from . import settings

sys.path.insert(0, str(settings.GENERATOR_DIR))
import benchmark as B                     # noqa: E402  (generator modules, imported as-is)
import config as GC                       # noqa: E402
import generate_bank_workbook as G        # noqa: E402
import source_data as S                   # noqa: E402
import threshold_analysis as TA           # noqa: E402

GENERATOR_VERSION = G.VERSION
RESOLUTION_TEXT = G.RESOLUTION
_directories, _lock = {}, threading.Lock()


def periods():
    """Available SBA FOIA releases: {"20260630": folder}."""
    return {k: v for k, v in S.snapshots().items() if G._folder_date(k)}


def period_date(period):
    return G._folder_date(period)


def directory(period):
    """Lender directory of one release (the generator's own cached directory); AsOfDate confirmed for the period."""
    with _lock:
        if period not in _directories:
            folder = periods()[period]
            d, as_of_values = S.lender_directory(folder, progress=lambda *_: None)
            S.snapshot_as_of(as_of_values, period_date(period))
            _directories[period] = d
        return _directories[period]


def search(period, query, limit=40):
    """Same rule as the command-line menu: digits = FDIC certificate, otherwise part of the bank name;
    only lenders with eligible loans; largest first."""
    q = query.strip()
    d = directory(period)
    if q.isdigit():
        hits = [(k, v) for k, v in d.items() if S.norm_id(q) in v["certs"] and v["eligible"] > 0]
    else:
        hits = [(k, v) for k, v in d.items() if q.lower() in k[0].lower() and v["eligible"] > 0]
    hits.sort(key=lambda kv: (-kv[1]["eligible"], kv[0]))
    return hits[:limit], len(hits)


def states(period):
    """States with eligible lenders: [(state, number of banks)], alphabetical."""
    n = {}
    for (name, state), v in directory(period).items():
        if v["eligible"] > 0:
            n[state] = n.get(state, 0) + 1
    return sorted(n.items())


ALL_STATES = "*"


def banks(period, state):
    """Every eligible bank (name) in a state - or in every state when state is ALL_STATES - largest first, with the
    identity evidence the dropdown shows."""
    out = []
    for (name, st), v in directory(period).items():
        if (state == ALL_STATES or st == state) and v["eligible"] > 0:
            cities = sorted({x["city"] for x in v["lenders"] if x["eligible"] > 0})
            out.append({"name": name, "state": st, "eligible": v["eligible"], "certs": [c for c in v["certs"] if c],
                        "cities": cities, "several_lenders": len(v["certs"]) > 1})
    names = {}
    for b in out:
        names[b["name"].casefold()] = names.get(b["name"].casefold(), 0) + 1
    for b in out:
        b["similar_name"] = names[b["name"].casefold()] > 1
    out.sort(key=lambda b: (-b["eligible"], b["name"]))
    return out


def resolve(period, bank_name, bank_state, cert=None, location_ids=None):
    """The generator's identity rules (raises S.LenderSelectionError with the evidence when ambiguous)."""
    return S.resolve_lender(directory(period), cert=cert or None, bank_name=bank_name, bank_state=bank_state,
                            location_ids=location_ids or None)


def entry(period, bank_name, bank_state):
    return directory(period).get((bank_name, bank_state))


def generate(period, selection, output_dir, inputs, log, threshold=None):
    folder = periods()[period]
    return G.generate(selection, folder, period_date(period), output_dir=output_dir, overwrite=False, log=log,
                      inputs=inputs, threshold=threshold)


SelectionError, SourceError = S.LenderSelectionError, S.SourceError
REQUIRED_SHEETS = ("PerformanceSummary", "FOIA", "Benchmark_Notes", "Combined_Sector", "Generation_Info", "Validation",
                   "Threshold_Analysis")


def post_checks(path):
    """Checks executed on the generated file after generation (file structure only; no Excel calculation)."""
    checks = []
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        checks.append(("Workbook file is a complete, readable .xlsx package", bad is None, bad or "all parts readable"))
        names = z.namelist()
        ext = [n for n in names if "externalLink" in n]
        checks.append(("No links to external workbooks", not ext, ", ".join(ext) or "none"))
        wbx = z.read("xl/workbook.xml").decode()
        rels = z.read("xl/_rels/workbook.xml.rels").decode()
        rid = re.search(r'<sheet [^>]*name="FOIA"[^>]*r:id="([^"]+)"', wbx).group(1)
        tgt = (re.search(rf'<Relationship [^>]*Id="{rid}"[^>]*Target="([^"]+)"', rels) or
               re.search(rf'<Relationship [^>]*Target="([^"]+)"[^>]*Id="{rid}"', rels)).group(1).lstrip("/")
        xml = z.read(tgt if tgt.startswith("xl/") else "xl/" + tgt).decode("utf-8", "ignore")
        last = int(re.search(r'<dimension ref="[A-Z]+\d+:[A-Z]+(\d+)"', xml).group(1))
        rows = max(int(r) for r in re.findall(r'<row r="(\d+)"', xml))
        checks.append(("FOIA sheet size label covers every loan row", last == rows, f"label row {last}, last row {rows}"))
    wb = load_workbook(path, read_only=True)
    missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
    checks.append(("Required sheets present (incl. Benchmark_Notes, Validation)", not missing,
                   ", ".join(missing) or f"{len(wb.sheetnames)} sheets"))
    gi = {str(r[0]): r[1] for r in wb["Generation_Info"].iter_rows(min_row=3, max_col=2, values_only=True) if r[0]}
    n_val = wb["Validation"]["B3"].value
    ta = wb["Threshold_Analysis"]
    gi["_threshold"], gi["_threshold_checks"] = ta["B3"].value, ta["B5"].value
    gi["_titles"] = (wb["PerformanceSummary"]["AJ2"].value, wb["PerformanceSummary"]["AV2"].value)
    wb.close()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return checks, gi, n_val, sha
