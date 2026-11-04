"""Industry-matched SBA 7(a) benchmark for PerformanceSummary X:CE - PRELIMINARY, pending FalconBridge confirmation.

Reproduces FalconBridge's 'SBA Orig_v2' method (acceptance/benchmark/BENCHMARK_METHODOLOGY.md) from correctly parsed
public FOIA files of the selected release:
  population   ApprovalFiscalYear >= 2000, LoanStatus <> CANCLD, FirstDisbursementDate present
  vintage      calendar year of FirstDisbursementDate
  groups       Standard: GrossApproval > threshold (default 350,000) / Small: the rest; CRE: TermInMonths > 120 / Non-CRE: the rest
  industry     NaicsDescription ("N/A" when blank)
  defaulted    ChargeOffDate present;  outstanding: no PaidInFullDate and no ChargeOffDate  (date-based, as in Orig_v2)
  amounts      GrossApproval and GrossChargeOffAmount, whole dollars (Orig_v2 loads them as Int64)
The results are written to hidden sheets laid out like the Orig_v2 Combined_* sheets (key in column G, values in
H..AC), so the template's own INDEX/MATCH formulas work unchanged once the external-link prefix '[2]' is removed.
Sector sheets: every SBA industry, all vintages (identical for every lender of a release). Vintage sheets: only
loans in the selected lender's industries (FalconBridge's industry-matched method; Orig_v2 Sector_Flag_All = "In").
"""
import json
from collections import defaultdict
from pathlib import Path

from openpyxl.styles import Font

import config as C
from source_data import _files_signature, _reader, parse_date, source_files

GROUPS = [("", None), ("_Standard", (0, "Standard")), ("_Small", (0, "Small")),
          ("_Non-CRE", (1, "Non-CRE")), ("_CRE", (1, "CRE"))]
# Combined_* columns written (Orig_v2 letters); AC is a formula as in Orig_v2
COLS = [("H", "Loan Count (orig.)"), ("I", "Est Outstanding: loan count (no PaidInFullDate, no ChargeOffDate)"),
        ("K", "ChargeOff Loan Count (ChargeOffDate present)"), ("N", "Total GrossApp (original $)"),
        ("O", "Est Outstanding: GrossApp of those loans (ORIGINAL $, not a balance)"),
        ("Q", "Total GrossApp of ChargeOff Loans"), ("T", "Total ChargeOff Amt (GrossChargeOffAmount)"),
        ("W", "Total GrossApp of PIF Loans"), ("Z", "PURCH(NOT C/O): GrossApp of outstanding loans"),
        ("AA", "LIQUID: GrossApp of outstanding loans")]
METRICS = [c for c, _ in COLS]
CACHE_VERSION = 1


def _num(s):
    s = s.strip()
    return float(s) if s else None


def above(gross, threshold):
    """Size classification shared by the benchmark and the bank's threshold groups: the loan's GrossApproval rounded
    to whole dollars (Python round, as Orig_v2 loads amounts as Int64) is greater than the threshold."""
    return round(gross or 0) > threshold


def threshold_label(threshold):
    """Short label used in headings: 350000 -> '350K', 1000000 -> '1M', 1500000 -> '1.5M', 337550 -> '$337,550'."""
    t = int(threshold)
    if t >= 1_000_000 and t % 100_000 == 0:
        return f"{t / 1_000_000:g}M"
    if t % 1000 == 0:
        return f"{t // 1000:,}K"
    return f"${t:,}"


_POPULATIONS = {}


def population(folder, progress=print):
    """The benchmark population of one release as compact arrays (one entry per loan, in file order), with the
    population counts. Kept in memory per process; about 32 MB for the 2026-06-30 release."""
    from array import array
    files = source_files(folder)
    sig = [str(Path(folder).resolve())] + _files_signature(files)
    key = json.dumps(sig)
    if key in _POPULATIONS:
        return _POPULATIONS[key]
    progress("Reading the SBA benchmark population from the historical files (first use per release)...")
    pop = {"signature": sig, "files": [p.name for p in files], "industries": [], "ind": array("i"),
           "year": array("h"), "gross": array("q"), "co": array("q"), "flags": array("b")}
    ind_ix = {}
    counts, as_of = defaultdict(int), defaultdict(int)
    late = []
    for path in files:
        f, rd, h = _reader(path)
        ix = {k: h.index(k) for k in ("ApprovalFiscalYear", "LoanStatus", "FirstDisbursementDate", "GrossApproval",
                                      "GrossChargeOffAmount", "TermInMonths", "NaicsDescription", "PaidInFullDate",
                                      "ChargeOffDate", "BankName", "BorrName", "AsOfDate")}
        with f:
            for r in rd:
                counts["records"] += 1
                fy = _num(r[ix["ApprovalFiscalYear"]])
                if fy is None or fy < C.BENCHMARK_FIRST_FY:
                    counts["excluded: ApprovalFiscalYear before benchmark start"] += 1
                    continue
                status = r[ix["LoanStatus"]].strip()
                fd = parse_date(r[ix["FirstDisbursementDate"]]) if r[ix["FirstDisbursementDate"]].strip() else None
                if status == C.CANCELLED_STATUS:
                    counts["excluded: LoanStatus = CANCLD"] += 1
                    continue
                if fd is None:
                    counts["excluded: no FirstDisbursementDate"] += 1
                    continue
                g = round(_num(r[ix["GrossApproval"]]) or 0)
                co_amt = round(_num(r[ix["GrossChargeOffAmount"]]) or 0)
                term = _num(r[ix["TermInMonths"]]) or 0
                desc = r[ix["NaicsDescription"]] or C.NA_SECTOR
                chg, pif = bool(r[ix["ChargeOffDate"]].strip()), bool(r[ix["PaidInFullDate"]].strip())
                if desc not in ind_ix:
                    ind_ix[desc] = len(pop["industries"])
                    pop["industries"].append(desc)
                pop["ind"].append(ind_ix[desc])
                pop["year"].append(fd.year)
                pop["gross"].append(g)
                pop["co"].append(co_amt)
                pop["flags"].append((term > C.BENCHMARK_CRE_MIN_TERM) | chg << 1 | pif << 2
                                    | (status == "PURCH(NOT C/O)") << 3 | (status == "LIQUID") << 4)
                counts["loans in benchmark population"] += 1
                as_of[r[ix["AsOfDate"]].strip()] += 1
                if status == "CHGOFF" and not chg:
                    counts["definition gap: LoanStatus CHGOFF without ChargeOffDate (benchmark: outstanding)"] += 1
                if status != "CHGOFF" and chg:
                    counts[f"definition gap: ChargeOffDate with LoanStatus {status or 'blank'} (benchmark: defaulted)"] += 1
                if status == "EXEMPT" and pif:
                    counts["definition gap: LoanStatus EXEMPT with PaidInFullDate (benchmark: not outstanding)"] += 1
                if status == "P I F" and not pif:
                    counts["definition gap: LoanStatus P I F without PaidInFullDate (benchmark: outstanding)"] += 1
                if fd.year > C.TEMPLATE_HIDDEN_TAB_LAST_YEAR:
                    late.append(f"{r[ix['BorrName']].strip()} ({r[ix['BankName']]}; FirstDisbursementDate {fd}; "
                                f"{desc}; ${g:,})")
    pop.update(counts=dict(counts), as_of_values=sorted(as_of), late_vintage_loans=late)
    _POPULATIONS[key] = pop
    return pop


def group(pop, threshold):
    """Grouped benchmark rows [desc, year, size, cre, *metrics] at a threshold, in the order of first occurrence (the
    order aggregate() has always produced). Standard: above(GrossApproval, threshold); Small: the rest."""
    agg = defaultdict(lambda: [0] * len(METRICS))
    names = pop["industries"]
    for i, y, g, co_amt, fl in zip(pop["ind"], pop["year"], pop["gross"], pop["co"], pop["flags"]):
        chg, pif = bool(fl & 2), bool(fl & 4)
        v = agg[(names[i], y, "Standard" if g > threshold else "Small", "CRE" if fl & 1 else "Non-CRE")]
        v[0] += 1; v[2] += chg; v[3] += g; v[5] += g if chg else 0; v[6] += co_amt; v[7] += g if pif else 0
        if not chg and not pif:
            v[1] += 1; v[4] += g
            v[8] += g if fl & 8 else 0
            v[9] += g if fl & 16 else 0
    return [list(k) + v for k, v in agg.items()]


def aggregate(folder, progress=print, threshold=None):
    """Grouped benchmark population of one release at a threshold (default C.BENCHMARK_STANDARD_MIN).
    The default threshold is cached in output/.cache exactly as before; other thresholds are grouped in memory."""
    t = C.BENCHMARK_STANDARD_MIN if threshold is None else int(threshold)
    files = source_files(folder)
    sig = [str(Path(folder).resolve())] + _files_signature(files)
    cache = C.CACHE_DIR / f"benchmark_{Path(folder).name}.json"
    default = t == C.BENCHMARK_STANDARD_MIN
    if default and cache.exists():
        data = json.loads(cache.read_text())
        if data.get("signature") == sig and data.get("version") == CACHE_VERSION:
            data["threshold"] = t
            return data
    if default:
        progress("Building SBA benchmark population from the historical files (first run per release, ~1 minute)...")
    pop = population(folder, progress)
    data = {"version": CACHE_VERSION, "signature": sig, "files": pop["files"], "counts": pop["counts"],
            "as_of_values": pop["as_of_values"], "late_vintage_loans": pop["late_vintage_loans"],
            "rows": group(pop, t)}
    if default:
        C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    data["threshold"] = t
    return data


def tables(data, sector_labels):
    """Combined_* tables for one lender: {suffix: {"vintage": {year: metrics}, "sector": {label: metrics}}}.
    Text matching is case-insensitive, as in Excel's SUMIFS / COUNTIF / MATCH."""
    lender = {s.casefold() for s in sector_labels}
    out = {sfx: {"vintage": defaultdict(lambda: [0] * len(METRICS)), "sector": defaultdict(lambda: [0] * len(METRICS))}
           for sfx, _ in GROUPS}
    display, years = {}, set()
    for row in data["rows"]:
        desc, year, size, cre, vals = row[0], row[1], row[2], row[3], row[4:]
        k = desc.casefold()
        display.setdefault(k, desc)
        years.add(year)
        for sfx, flt in GROUPS:
            if flt and (size, cre)[flt[0]] != flt[1]:
                continue
            targets = [out[sfx]["sector"][k]] + ([out[sfx]["vintage"][year]] if k in lender else [])
            for t in targets:
                for i, x in enumerate(vals):
                    t[i] += x
    for s in sector_labels:                    # the lender's own labels are shown as written in its FOIA rows
        display[s.casefold()] = s
    span = range(min(min(years), C.BENCHMARK_FIRST_VINTAGE), max(years) + 1)
    res = {}
    for sfx, _ in GROUPS:
        sec = out[sfx]["sector"]
        keys = sorted(set(sec) | lender, key=lambda k: display[k].casefold())
        res[sfx] = {"vintage": {y: list(out[sfx]["vintage"].get(y, [0] * len(METRICS))) for y in span},
                    "sector": {display[k]: list(sec.get(k, [0] * len(METRICS))) for k in keys}}
    return res


def totals(data):
    rows = data["rows"]
    return sum(r[4] for r in rows), sum(r[7] for r in rows)       # loan count, original GrossApproval


def write_sheets(wb, tabs, as_of, lender_label):
    """Ten hidden sheets named and laid out like Orig_v2's Combined_* sheets."""
    made = []
    for kind, key_label in (("Vintage", "Vintage (year of FirstDisbursementDate)"), ("Sector", "NAICSDescription")):
        for sfx, _ in GROUPS:
            name = f"Combined_{kind}{sfx}"
            ws = wb.create_sheet(name)
            ws.sheet_state = "hidden"
            ws["A1"] = (f"PRELIMINARY industry-matched SBA 7(a) benchmark - public SBA FOIA as of {as_of:%m/%d/%Y}, "
                        f"FY{C.BENCHMARK_FIRST_FY} onward. Group: {sfx.strip('_') or 'All SBA 7(a)'}. " +
                        (f"Only loans in the industries of {lender_label} (FalconBridge industry-matched method)."
                         if kind == "Vintage" else "Every SBA industry, all vintages (same for every lender)."))
            ws["A1"].font = Font(bold=True)
            ws["A2"] = "Generated by bank_workbook_generator/benchmark.py; layout of FalconBridge's SBA Orig_v2 Combined_* sheets."
            ws["G3"] = key_label
            for col, head in COLS:
                ws[f"{col}3"] = head
            ws["AC3"] = "% Defaulted (Q/N)"
            rows = tabs[sfx][kind.lower()]
            for i, (k, vals) in enumerate(rows.items(), start=4):
                if kind == "Sector" and isinstance(k, str) and k.startswith("="):
                    ws.cell(i, 7).value = k
                    ws.cell(i, 7).data_type = "s"
                else:
                    ws[f"G{i}"] = k
                for (col, _), v in zip(COLS, vals):
                    ws[f"{col}{i}"] = v
                ws[f"AC{i}"] = f'=IFERROR(Q{i}/N{i},"na")'
            for col, w in (("A", 4), ("G", 48)):
                ws.column_dimensions[col].width = w
            made.append(name)
    return made


GROUP_NAMES = {"_Standard": "Standard", "_Small": "Small", "_Non-CRE": "Non-CRE", "_CRE": "CRE", "": "All SBA 7(a)"}


def empty_sector_groups(tabs, sector_labels):
    """Lender industries with no SBA loans in a group (their ratio cells are guarded and show n/a)."""
    return [(GROUP_NAMES[sfx], s) for sfx, _ in GROUPS for s in sector_labels if tabs[sfx]["sector"][s][0] == 0]


def empty_vintage_groups(tabs, years):
    """Vintage rows (template benchmark rows 1999-2026) with no SBA loans in a group."""
    return [(GROUP_NAMES[sfx], y) for sfx, _ in GROUPS for y in years if tabs[sfx]["vintage"].get(y, [0])[0] == 0]


def notes_rows(data, as_of, folder, sector_labels, flags, tabs):
    """Content of the Benchmark_Notes sheet: [(kind, label, text, detail)]."""
    cnt = data["counts"]
    t = data.get("threshold", C.BENCHMARK_STANDARD_MIN)
    default = t == C.BENCHMARK_STANDARD_MIN
    n, g = totals(data)
    gaps = {k.split(": ", 1)[1]: v for k, v in cnt.items() if k.startswith("definition gap")}
    excl = {k.split(": ", 1)[1]: v for k, v in cnt.items() if k.startswith("excluded")}
    na = C.NA_SECTOR in sector_labels
    rows = [
        ("head", "Status", None, None),
        ("row", "Status", "PRELIMINARY - subject to FalconBridge's confirmation of the benchmark methodology.", ""),
        ("row", "What this benchmark is",
         "An INDUSTRY-MATCHED SBA 7(a) benchmark. Vintage rows and the totals (rows 6 and 60) cover SBA loans in this "
         "lender's industries only; each sector row covers every SBA loan in that industry (identical for every lender "
         "of the same release). It is not a benchmark of all SBA 7(a) loans, although the template titles read "
         "'All SBA 7(a)'.", ""),
        ("row", "Method", "FalconBridge's existing method, recovered from its 'SBA Orig_v2' workbook (Power Query steps, "
         "Combined_* sheets) and the FinWise and US Metro reference workbooks, which it reproduces exactly. Template "
         "formulas in X:CE are unchanged except that they read the hidden Combined_* sheets of this workbook instead of "
         "the external Orig_v2 file.", "acceptance/benchmark/BENCHMARK_METHODOLOGY.md"),
        ("row", "Source data", f"Public SBA 7(a) FOIA, AsOfDate {as_of:%m/%d/%Y} (every benchmark loan carries this "
         f"AsOfDate), as processed by fix_foia.py; folder {Path(folder).resolve()}", ", ".join(data["files"])),
        ("row", "Population", f"ApprovalFiscalYear >= {C.BENCHMARK_FIRST_FY}; LoanStatus not CANCLD; "
         "FirstDisbursementDate present. Vintage = calendar year of FirstDisbursementDate.",
         f"{n:,} loans, original GrossApproval ${g:,.0f}. Excluded: " + "; ".join(f"{k} {v:,}" for k, v in excl.items())),
        ("row", "Groups", f"Standard: GrossApproval > ${t:,}; Small: the rest. CRE: TermInMonths > "
         f"{C.BENCHMARK_CRE_MIN_TERM} (a term proxy, not collateral data); Non-CRE: the rest.", "" if default else
         f"Original loan balance threshold selected for this workbook: ${t:,} (default ${C.BENCHMARK_STANDARD_MIN:,}). "
         "GrossApproval is rounded to whole dollars before the comparison, for the benchmark and for the bank's "
         "threshold groups (Threshold_Analysis sheet) alike."),
        ("row", "Lender's industry list", f"{len(sector_labels)} industries = this workbook's sector rows (the "
         "NaicsDescription of the lender's eligible loans).",
         "Includes N/A (blank NAICS code). The FalconBridge reference workbooks contain no N/A row, so including it in "
         "the industry-matched population is unconfirmed." if na else ""),
        ("row", "Amounts", "ORIGINAL approval amounts (GrossApproval) and gross charge-offs, whole dollars per loan as "
         "loaded by Orig_v2. 'Est. Orig. Bal. of loans Outstanding' is the original amount of loans with no paid-in-full "
         "or charge-off date - not a current balance. (Orig_v2 also computes an amortised estimate that "
         "PerformanceSummary does not use; it is not produced here.)", ""),
        ("head", "Definitions that differ from the lender columns K:W (kept provisionally, not harmonised)", None, None),
        ("row", "Defaulted", "Benchmark: ChargeOffDate present. Lender columns: LoanStatus CHGOFF (Lookup).", ""),
        ("row", "Outstanding", "Benchmark: no PaidInFullDate and no ChargeOffDate. Lender columns: LoanStatus EXEMPT.", ""),
        ("row", "Approval years", f"Benchmark: FY{C.BENCHMARK_FIRST_FY} onward. Lender columns: all approval years "
         "in the release.", ""),
        ("row", "Loans where the definitions disagree (whole benchmark population)",
         "; ".join(f"{k}: {v:,}" for k, v in gaps.items()) or "none", ""),
        ("head", "Cells flagged for analyst review (template formulas kept unchanged; not validated economic measures)",
         None, None),
    ]
    rows += [("row", issue, cells, why) for issue, cells, why in flags]
    empty = empty_sector_groups(tabs, sector_labels)
    empty_v = empty_vintage_groups(tabs, range(C.BENCHMARK_FIRST_VINTAGE, C.TEMPLATE_HIDDEN_TAB_LAST_YEAR + 1))
    if empty or empty_v:
        rows.append(("row", "4. Groups with no SBA loans (shown as n/a)",
                     "; ".join([f"{s} - {g}" for g, s in empty] + [f"vintage {y} - {g}" for g, y in empty_v]),
                     "These industries or vintages have no SBA loans in the group, so their averages and percentages "
                     "have no denominator. Their ratio cells (and any total whose denominator is zero) show n/a instead "
                     "of #DIV/0!; every other formula is unchanged. Totals are unaffected (an empty row carries zero "
                     "weight)."))
    else:
        rows.append(("row", "4. Industries with no SBA loans in a group (#DIV/0!)", "none", ""))
    rows += [
        ("row", "LIQUID (%) and PURCH(NOT C/O) (%)", "0% in every group because this FOIA release publishes no LIQUID "
         "or PURCH(NOT C/O) loan status - not evidence that no loans are in liquidation or purchased.", ""),
        ("head", "Differences from FalconBridge's historical benchmark values", None, None),
        ("row", "Source-import errors not reproduced", "FalconBridge's Power Query import (Csv.Document, "
         "QuoteStyle.None) split records at line breaks inside text fields: 2 loans dropped, 1 charge-off and 1 "
         "paid-in-full status lost, and 1 loan (disbursement date 2028) absent. This workbook uses the files as "
         "published, so benchmark figures can differ slightly from FalconBridge's (largest at 2026-06-30: +0.58 "
         "percentage points, Standard % Loss Severity, 2022 vintage)." if default else
         "published. FalconBridge's own benchmark values exist only for the $350,000 grouping, so the Standard and "
         f"Small groups at ${t:,} are not reconciled to FalconBridge reference values; they are verified by an "
         "independent recomputation from the FOIA files.", "acceptance/benchmark/BENCHMARK_RECONCILIATION.md"
         if default else "acceptance/threshold/"),
        ("row", "Period labels", f"All five groups are labelled from this release's AsOfDate ({as_of:%m/%Y}). The "
         "FalconBridge references label three groups 'as of 3/2026' although their values reproduce exactly from "
         "6/2026 data.", ""),
        ("row", "Loans disbursed after the template's last vintage row (2026)",
         "; ".join(data["late_vintage_loans"]) or "none",
         "Included in sector rows and row 6 (no year filter there); no vintage row." if data["late_vintage_loans"] else ""),
    ]
    return rows
