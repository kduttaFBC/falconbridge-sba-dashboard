"""Dashboard and Excel consistency: every figure the dashboard shows for a bank and threshold must equal the cell of
the Excel workbook generated for the same criteria, after recalculation (LibreOffice headless here; desktop Excel is
a separate, manual step).

Usage: dashboard_consistency.py --workbook RECALCULATED.xlsx --bank-name N --bank-state S [--cert C] --threshold T
"""
import argparse
import json
import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string as CI, get_column_letter as L

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from webapp import analysis as AN          # noqa: E402
from webapp import generator_bridge as GB  # noqa: E402

TA = GB.TA
OFF = {"loans": 0, "orig": 1, "avg": 3, "active": 4, "active_orig": 5, "chgoff": 6, "cum_def": 7, "severity": 8,
       "cum_loss": 9, "purch_pct": 11}


def same(a, b):
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        return isinstance(b, (int, float)) and abs(a - b) <= 1e-9 * max(1, abs(a))
    return b in ("n/a", "na", "", None) if a in ("n/a", None) else a == b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--bank-name", required=True)
    ap.add_argument("--bank-state", required=True)
    ap.add_argument("--cert")
    ap.add_argument("--threshold", type=int, required=True)
    a = ap.parse_args()
    sel = GB.resolve("20260630", a.bank_name, a.bank_state, a.cert)
    p = AN.compute("20260630", sel, a.threshold)
    wb = load_workbook(a.workbook, data_only=True, read_only=True)
    ta = {(r, c): v for r, row in enumerate(wb["Threshold_Analysis"].iter_rows(values_only=True), 1)
          for c, v in enumerate(row, 1)}
    ps = {(r, c): v for r, row in enumerate(wb["PerformanceSummary"].iter_rows(values_only=True), 1)
          for c, v in enumerate(row, 1)}
    n, fails = 0, []

    def check(where, dash, cell):
        nonlocal n
        n += 1
        if not same(dash, cell):
            fails.append((where, dash, cell))

    B = p["bank"]
    years = [r["year"] for r in B["full"]["total"]["vintage"]]
    sectors = [r["name"] for r in B["full"]["total"]["industry"]]
    subs = [r["name"] for r in B["full"]["total"]["subprogram"]]
    lay = TA.layout(years, sectors, subs, [r["year"] for r in p["chgoff_by_year"]["total"]["years"]],
                    [s["status"] for s in p["status"]["total"]])
    for h, _ in TA.HISTORIES:
        for g, _ in TA.GROUPS:
            blk = B[h][g]
            items = [(("total", "TOTAL"), blk["total"])] + [(("v", r["year"]), r) for r in blk["vintage"]] + \
                    [(("i", r["name"]), r) for r in blk["industry"]] + [(("s", r["name"]), r) for r in blk["subprogram"]]
            for key, row in items:
                for mk in TA.MKEYS:
                    ref = lay["cell"](h, g, key, mk)
                    col = CI("".join(ch for ch in ref if ch.isalpha()))
                    check(f"Threshold_Analysis!{ref} {h} {g} {key} {mk}", row[mk], ta.get((lay["rows"][key], col)))
    for j, (g, _) in enumerate(TA.GROUPS):
        for y in p["chgoff_by_year"][g]["years"]:
            r = lay["co_rows"][y["year"]]
            check(f"charge-offs {y['year']} {g}", y["loans"], ta.get((r, 3 + 2 * j)))
            check(f"charge-offs $ {y['year']} {g}", y["amount"], ta.get((r, 4 + 2 * j)))
        for s in p["status"][g]:
            r = lay["st_rows"][s["status"]]
            check(f"status {s['status']} {g}", s["loans"], ta.get((r, 3 + 2 * j)))
            check(f"status $ {s['status']} {g}", s["orig"], ta.get((r, 4 + 2 * j)))
    # benchmark: PerformanceSummary row 6, vintage rows, sector rows
    sec_rows, year_rows = {}, {}
    for r in range(1, max(k[0] for k in ps) + 1):
        a_, b_ = ps.get((r, 1)), ps.get((r, 2))
        if isinstance(a_, int) and 1999 <= a_ <= 2026 and r > 20 and b_ == a_:
            year_rows[a_] = r
    hdr = next(r for (r, c), v in ps.items() if c == 2 and v == "TOTAL by Industry - SBA Sectors")
    r = hdr + 1
    while ps.get((r, 1)) not in (None, ""):
        sec_rows[ps[(r, 1)]] = r
        r += 1
    for g, base in (("total", "X"), ("above", "AJ"), ("below", "AV"), ("noncre", "BH"), ("cre", "BT")):
        bm = p["benchmark"][g]
        for k, off in OFF.items():
            check(f"PerformanceSummary!{L(CI(base) + off)}6 {g} {k}", bm["total"][k], ps.get((6, CI(base) + off)))
        for v in bm["vintage"]:
            for k, off in OFF.items():
                if g in ("noncre", "cre") and not v["loans"] and k not in ("loans", "orig", "active", "active_orig", "chgoff"):
                    continue
                check(f"PS vintage {v['year']} {g} {k}", v[k], ps.get((year_rows[v["year"]], CI(base) + off)))
        for s in bm["sector"]:
            for k, off in OFF.items():
                # the template's CRE cum. loss uses IFERROR(...,0): ratios are compared only where the row has loans
                if g in ("noncre", "cre") and not s["loans"] and k not in ("loans", "orig", "active", "active_orig", "chgoff"):
                    continue
                check(f"PS sector {s['name']} {g} {k}", s[k], ps.get((sec_rows[s["name"]], CI(base) + off)))
    # portfolio-age bands = sums of the workbook's own "% of Total Orig." on the vintage rows (bank N, SBA Z)
    for b in p["seasoning"]["bands"]:
        bank_n = sum(ps.get((r, CI("N"))) or 0 for y, r in year_rows.items() if b["from"] <= y <= b["to"])
        sba_z = sum(ps.get((r, CI("Z"))) or 0 for y, r in year_rows.items() if b["from"] <= y <= b["to"])
        check(f"seasoning bank share {b['label']} (sum of N)", b["bank_share"], bank_n)
        check(f"seasoning SBA share {b['label']} (sum of Z)", b["sba_share"], sba_z)
    # FalconBridge display labels and Glossary text, as written in the workbook
    sub_rows = {ps.get((r, 1)): ps.get((r, 2)) for r in range(7, 20) if ps.get((r, 1))}
    for row in p["bank"]["full"]["total"]["subprogram"]:
        check(f"PerformanceSummary subprogram label {row['name']}", row["label"], sub_rows.get(row["name"]))
    gl = wb["Glossary"]
    text = {str(v) for row in gl.iter_rows(values_only=True) for v in row if v is not None}
    for e in p["glossary"]:
        n += 1
        if e["definition"] not in text or e["field"] not in text:
            fails.append(("Glossary", e["field"], e["definition"][:60]))
    res = {"workbook": Path(a.workbook).name, "threshold": a.threshold, "dashboard_values_compared": n,
           "differences": len(fails), "first_differences": [list(map(str, f)) for f in fails[:15]]}
    print(json.dumps(res))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
