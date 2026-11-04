"""Independent reconciliation of a threshold-specific workbook (any original loan balance threshold).

Recomputes, straight from the SBA FOIA CSV files and WITHOUT importing the generator, every value of
  * the Threshold_Analysis sheet (bank: total / above / at or below, full history and FY2000 onward, every row;
    charge-offs by year; loan status), and
  * the PerformanceSummary benchmark blocks All SBA 7(a) (X), Standard (AJ) and Small (AV): counts, original
    amounts, active loans, defaulted loans, % Cum. Defaulted, % Loss Severity, % Cum. Loss, PURCH (%), on row 6,
    the vintage rows 1999-2026 and the sector rows,
and compares them with the workbook values after recalculation (LibreOffice headless, or desktop Excel).
Definitions restated here from the template formulas and FalconBridge's Orig_v2 method:
  bank:      eligible = LoanStatus not blank / not CANCLD, FirstDisbursementDate present; status groups from the
             template Lookup table; defaulted = group CHGOFF; active = group EXEMPT
  benchmark: ApprovalFiscalYear >= 2000, not CANCLD, disbursed; defaulted = ChargeOffDate present; outstanding = no
             PaidInFullDate and no ChargeOffDate; amounts rounded to whole dollars
  size:      GrossApproval rounded half-to-even to whole dollars > threshold (Decimal arithmetic here)
Usage: oracle.py --workbook RECALCULATED.xlsx --release source_data/20260630 --bank-name N --bank-state S
                 [--cert C] --threshold T [--out result.json]
"""
import argparse
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string as CI, get_column_letter as L

STATUS_GROUP = {"P I F": "P I F", "CURR": "EXEMPT", "PURCH(NOT C/O)": "EXEMPT", "PSTDUE": "EXEMPT",
                "DEFERD": "EXEMPT", "DELINQ": "EXEMPT", "CHGOFF": "CHGOFF", "LIQUID": "EXEMPT", "CLSLN": "CHGOFF",
                "EXEMPT": "EXEMPT"}
NA = "n/a"


def pdate(t):
    t = t.strip()
    if "/" in t:
        m, d, y = t.split("/")
        return date(int(y), int(m), int(d))
    return date.fromisoformat(t[:10])


def num(t):
    t = t.strip()
    return float(t) if t else 0.0


def whole(t):
    return int(Decimal(t.strip() or "0").quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def nid(t):
    t = t.strip()
    return str(int(t)) if t.isdigit() else t


def months(d0, d1):
    n = (d1.year - d0.year) * 12 + d1.month - d0.month
    return n - 1 if d1.day < d0.day else n


def div(a, b):
    return a / b if b else NA


def read(release, name, state, cert):
    bank, bench = [], []
    as_of = None
    for path in sorted(glob.glob(os.path.join(release, "FOIA*7a*.csv"))):
        with open(path, encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            h = next(rd)
            ix = {k: i for i, k in enumerate(h)}
            for r in rd:
                g = lambda k: r[ix[k]]
                as_of = as_of or pdate(g("AsOfDate"))
                st, fd = g("LoanStatus").strip(), g("FirstDisbursementDate").strip()
                if g("BankName") == name and g("BankState") == state and (cert is None or nid(g("BankFDICNumber")) == cert):
                    if st and st != "CANCLD" and fd:
                        bank.append({k: g(k) for k in ("GrossApproval", "SBAGuaranteedApproval", "ApprovalFiscalYear",
                                                       "FirstDisbursementDate", "NaicsDescription", "Subprogram",
                                                       "LoanStatus", "ChargeOffDate", "GrossChargeOffAmount")})
                fy = g("ApprovalFiscalYear").strip()
                if fy and float(fy) >= 2000 and st != "CANCLD" and fd:
                    bench.append((g("NaicsDescription") or "N/A", pdate(fd).year, whole(g("GrossApproval")),
                                  num(g("TermInMonths")) > 120, bool(g("ChargeOffDate").strip()),
                                  bool(g("PaidInFullDate").strip()), whole(g("GrossChargeOffAmount")), st))
    return bank, bench, as_of


def bank_measures(rows, as_of):
    grp = lambda r: STATUS_GROUP[r["LoanStatus"].strip()]
    m = {"loans": len(rows), "orig": sum(num(r["GrossApproval"]) for r in rows),
         "guar": sum(num(r["SBAGuaranteedApproval"]) for r in rows)}
    act = [r for r in rows if grp(r) == "EXEMPT"]
    co = [r for r in rows if grp(r) == "CHGOFF"]
    m.update(unguar=m["orig"] - m["guar"], avg=div(m["orig"], m["loans"]), active=len(act),
             active_orig=sum(num(r["GrossApproval"]) for r in act),
             age=div(sum(months(pdate(r["FirstDisbursementDate"]), as_of) for r in act), len(act)),
             chgoff=len(co), chgoff_orig=sum(num(r["GrossApproval"]) for r in co),
             chgoff_amt=sum(num(r["GrossChargeOffAmount"]) for r in co),
             pif_orig=sum(num(r["GrossApproval"]) for r in rows if grp(r) == "P I F"),
             purch_orig=sum(num(r["GrossApproval"]) for r in rows if r["LoanStatus"].strip() == "PURCH(NOT C/O)"))
    m.update(cum_def=div(m["chgoff_orig"], m["orig"]), severity=div(m["chgoff_amt"], m["chgoff_orig"]),
             cum_loss=div(m["chgoff_amt"], m["orig"]), pif_pct=div(m["pif_orig"], m["orig"]),
             purch_pct=div(m["purch_orig"], m["orig"]))
    return m


HEADINGS = {"Loans originated (#)": "loans", "Original gross approval ($)": "orig",
            "Original SBA-guaranteed approval ($)": "guar", "Original unguaranteed approval ($)": "unguar",
            "Average original loan ($)": "avg", "Active loans (#)": "active",
            "Original amount of active loans ($) - not a balance": "active_orig",
            "Average age of active loans (months)": "age", "Charged-off loans (#)": "chgoff",
            "Original amount of charged-off loans ($)": "chgoff_orig", "Gross charge-off amount ($)": "chgoff_amt",
            "Original amount of paid-in-full loans ($)": "pif_orig", "Original amount PURCH(NOT C/O) ($)": "purch_orig",
            "% Cum. Defaulted": "cum_def", "% Loss Severity": "severity", "% Cum. Loss": "cum_loss",
            "% Paid in full": "pif_pct", "PURCH(NOT C/O) (%)": "purch_pct"}


class Compare:
    def __init__(self):
        self.n, self.fails = 0, []

    def __call__(self, where, expected, got):
        self.n += 1
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            ok = isinstance(got, (int, float)) and abs(got - expected) <= 1e-9 * max(1, abs(expected))
        else:
            ok = (got in (NA, "na", "", None)) if expected in (NA, "") else got == expected
        if not ok:
            self.fails.append((where, expected, got))


def check_threshold_sheet(ws, bank, as_of, t, cmp):
    above = lambda r: whole(r["GrossApproval"]) > t
    fy2000 = lambda r: r["ApprovalFiscalYear"].strip() != "" and float(r["ApprovalFiscalYear"]) >= 2000
    vals = {(r, c): v for r, row in enumerate(ws.iter_rows(values_only=True), start=1) for c, v in enumerate(row, 1)}
    blocks = {}
    for c in range(3, ws.max_column + 1):
        h8 = vals.get((8, c))
        if isinstance(h8, str) and " - " in h8:
            hist = "fy2000" if h8.startswith("FY2000") else "full"
            gtxt = h8.split(" - ", 1)[1]
            blocks[c] = (hist, "total" if gtxt == "Total" else "above" if gtxt.startswith("Above") else "below")
    cols = {}
    for c0, blk in blocks.items():
        for c in range(c0, c0 + len(HEADINGS)):
            cols[c] = (blk, HEADINGS[vals[(9, c)]])
    sets = {}
    for hist in ("full", "fy2000"):
        hs = [r for r in bank if hist == "full" or fy2000(r)]
        for g in ("total", "above", "below"):
            sets[(hist, g)] = [r for r in hs if g == "total" or above(r) == (g == "above")]
    section, r = None, 10
    cache = {}
    while True:
        a, b = vals.get((r, 1)), vals.get((r, 2))
        if isinstance(a, str) and a.startswith("Charge-offs by year"):
            break
        if a is None and isinstance(b, str):
            section = b.split(" ")[0]
        elif a is not None:
            for c, ((hist, g), mk) in cols.items():
                rows = sets[(hist, g)]
                key = (hist, g, section, a)
                if key not in cache:
                    if a == "TOTAL" and section is None:
                        sub = rows
                    elif section == "Vintage":
                        sub = [x for x in rows if pdate(x["FirstDisbursementDate"]).year == a]
                    elif section == "Industry":
                        sub = [x for x in rows if x["NaicsDescription"] == a]
                    else:
                        sub = [x for x in rows if x["Subprogram"] == a]
                    cache[key] = bank_measures(sub, as_of)
                cmp(f"Threshold_Analysis!{L(c)}{r} [{hist} {g} {section or ''} {a} {mk}]", cache[key][mk], vals.get((r, c)))
        r += 1
    # charge-offs by year and loan status (full history)
    grp = lambda x: STATUS_GROUP[x["LoanStatus"].strip()]
    r += 2
    while vals.get((r, 1)) not in (None, "No ChargeOffDate"):
        y = vals[(r, 1)]
        for j, g in enumerate(("total", "above", "below")):
            co = [x for x in sets[("full", g)] if grp(x) == "CHGOFF" and x["ChargeOffDate"].strip()
                  and pdate(x["ChargeOffDate"]).year == y]
            cmp(f"Threshold_Analysis!{L(3 + 2 * j)}{r} [charge-offs {y} {g} #]", len(co), vals.get((r, 3 + 2 * j)))
            cmp(f"Threshold_Analysis!{L(4 + 2 * j)}{r} [charge-offs {y} {g} $]",
                sum(num(x["GrossChargeOffAmount"]) for x in co), vals.get((r, 4 + 2 * j)))
        r += 1
    while not (isinstance(vals.get((r, 1)), str) and vals[(r, 1)].startswith("Loan status")):
        r += 1
    r += 2
    while vals.get((r, 1)) is not None:
        s = vals[(r, 1)]
        for j, g in enumerate(("total", "above", "below")):
            sub = [x for x in sets[("full", g)] if x["LoanStatus"].strip() == s]
            cmp(f"Threshold_Analysis!{L(3 + 2 * j)}{r} [status {s} {g} #]", len(sub), vals.get((r, 3 + 2 * j)))
            cmp(f"Threshold_Analysis!{L(4 + 2 * j)}{r} [status {s} {g} $]",
                sum(num(x["GrossApproval"]) for x in sub), vals.get((r, 4 + 2 * j)))
        r += 1
    cmp("Threshold_Analysis!F5 [sheet FAIL count]", 0, vals.get((5, 6)))
    cmp("Threshold_Analysis!B3 [threshold]", t, vals.get((3, 2)))


def bench_tables(bench, t, lender_industries):
    """Combined_* equivalents: {group: {"vintage": {year: m}, "sector": {casefold label: m}}}, m = H I K N O Q T W Z AA."""
    out = {g: {"vintage": defaultdict(lambda: [0] * 10), "sector": defaultdict(lambda: [0] * 10)}
           for g in ("all", "standard", "small")}
    lender = {s.casefold() for s in lender_industries}
    for desc, y, g, cre, chg, pif, co, st in bench:
        k = desc.casefold()
        for grp in ("all", "standard" if g > t else "small"):
            targets = [out[grp]["sector"][k]] + ([out[grp]["vintage"][y]] if k in lender else [])
            for v in targets:
                v[0] += 1; v[2] += chg; v[3] += g; v[5] += g if chg else 0; v[6] += co; v[7] += g if pif else 0
                if not chg and not pif:
                    v[1] += 1; v[4] += g; v[8] += g if st == "PURCH(NOT C/O)" else 0
                    v[9] += g if st == "LIQUID" else 0
    return out


def ps_row(m):
    """PerformanceSummary benchmark measures of one vintage / sector row from Combined_* metrics m."""
    H, I, K, N, O, Q, T, W, Z, AA = m
    cum_def = Q / N if N else NA
    cum_loss = T / N if N else NA
    sev = (cum_loss / cum_def if cum_def else NA) if cum_loss not in (0, NA) else ("" if N else NA)
    return {"loans": H, "orig": N, "avg": N / H if H else NA, "active": I, "active_orig": O, "defaulted": K,
            "cum_def": cum_def, "severity": sev, "cum_loss": cum_loss, "purch": Z / N if N else NA}


def ps_total(rows):
    """Row 6: sums, and the template's weighted averages (SUMPRODUCT ignores text)."""
    nz = lambda v: v if isinstance(v, (int, float)) else 0
    s = lambda k: sum(r[k] for r in rows)
    N, D = s("orig"), s("defaulted")
    cum_def = sum(nz(r["cum_def"]) * r["orig"] for r in rows) / N if N else NA
    sev = sum(nz(r["severity"]) * r["defaulted"] for r in rows) / D if D else NA
    return {"loans": s("loans"), "orig": N, "avg": N / s("loans") if s("loans") else NA, "active": s("active"),
            "active_orig": s("active_orig"), "defaulted": D, "cum_def": cum_def, "severity": sev,
            "cum_loss": cum_def * sev if NA not in (cum_def, sev) else NA,
            "purch": sum(nz(r["purch"]) * r["orig"] for r in rows) / N if N else NA}


OFFSETS = {"loans": 0, "orig": 1, "avg": 3, "active": 4, "active_orig": 5, "defaulted": 6, "cum_def": 7,
           "severity": 8, "cum_loss": 9, "purch": 11}


def check_benchmark(wb, bench, t, cmp):
    ps = wb["PerformanceSummary"]
    vals = {(r, c): v for r, row in enumerate(ps.iter_rows(values_only=True), start=1) for c, v in enumerate(row, 1)}
    sec_rows, year_rows, r = {}, {}, 1
    while (r, 2) in vals or r < 30:
        b = vals.get((r, 2))
        if b == "TOTAL by Industry - SBA Sectors":
            r += 1
            while vals.get((r, 1)) not in (None, ""):
                sec_rows[vals[(r, 1)]] = r
                r += 1
            break
        a = vals.get((r, 1))
        if isinstance(a, int) and 1999 <= a <= 2026 and r > 20:
            year_rows[a] = r
        r += 1
    tabs = bench_tables(bench, t, sec_rows)
    for grp, base in (("all", "X"), ("standard", "AJ"), ("small", "AV")):
        rows_s = []
        for label, r in sec_rows.items():
            m = ps_row(tabs[grp]["sector"].get(label.casefold(), [0] * 10))
            rows_s.append(m)
            for k, off in OFFSETS.items():
                cmp(f"PerformanceSummary!{L(CI(base) + off)}{r} [{grp} sector {label} {k}]", m[k],
                    vals.get((r, CI(base) + off)))
        tot = ps_total(rows_s)
        for k, off in OFFSETS.items():
            cmp(f"PerformanceSummary!{L(CI(base) + off)}6 [{grp} row 6 {k}]", tot[k], vals.get((6, CI(base) + off)))
        for y, r in year_rows.items():
            m = ps_row(tabs[grp]["vintage"].get(y, [0] * 10))
            for k, off in OFFSETS.items():
                cmp(f"PerformanceSummary!{L(CI(base) + off)}{r} [{grp} vintage {y} {k}]", m[k],
                    vals.get((r, CI(base) + off)))
    lab = (f"{t / 1_000_000:g}M" if t >= 1_000_000 and t % 100_000 == 0 else f"{t // 1000:,}K" if t % 1000 == 0
           else f"${t:,}")
    cmp("PerformanceSummary!AJ2 [title]", f"All SBA 7(a) - Standard (Bal > {lab})", vals.get((2, CI("AJ"))))
    cmp("PerformanceSummary!AV2 [title]", f"All SBA 7(a)  - Small (Bal <= {lab})", vals.get((2, CI("AV"))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--release", required=True)
    ap.add_argument("--bank-name", required=True)
    ap.add_argument("--bank-state", required=True)
    ap.add_argument("--cert")
    ap.add_argument("--threshold", type=int, required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    bank, bench, as_of = read(a.release, a.bank_name, a.bank_state, nid(a.cert) if a.cert else None)
    wb = load_workbook(a.workbook, data_only=True, read_only=True)
    cmp = Compare()
    check_threshold_sheet(wb["Threshold_Analysis"], bank, as_of, a.threshold, cmp)
    n_sheet = cmp.n
    check_benchmark(wb, bench, a.threshold, cmp)
    v = wb["Validation"]
    cmp("Validation!F3 [Validation sheet FAIL count]", 0, v["F3"].value)
    res = {"workbook": os.path.basename(a.workbook), "bank": f"{a.bank_name}, {a.bank_state}", "threshold": a.threshold,
           "bank_loans": len(bank), "benchmark_loans": len(bench), "values_compared": cmp.n,
           "threshold_sheet_values": n_sheet, "benchmark_values": cmp.n - n_sheet, "differences": len(cmp.fails),
           "first_differences": [list(map(str, f)) for f in cmp.fails[:20]],
           "validation_sheet": [v["B3"].value, v["D3"].value, v["F3"].value]}
    print(json.dumps(res, indent=1))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res, f, indent=1)
    sys.exit(1 if cmp.fails else 0)


if __name__ == "__main__":
    main()
