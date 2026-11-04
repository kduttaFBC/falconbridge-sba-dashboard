"""Original loan balance threshold groups for the selected bank (Threshold_Analysis sheet, approved B1/B3).

The bank's loans are split with the benchmark's own size rule (benchmark.above: GrossApproval rounded to whole
dollars > threshold), so the bank and the SBA benchmark groups use the same criteria. Each measure is the existing,
validated definition of the PerformanceSummary bank columns K:W (validation.Group), applied to the loans of a group.
No new financial measure is introduced; every ratio with an empty denominator is "n/a".

compute() is the single Python implementation used for the sheet's expected values and by the dashboard; the sheet
itself holds Excel formulas on the FOIA tab (like K:W), so Excel recalculates it independently. Amounts are ORIGINAL
approval amounts or gross charge-offs - never current balances.
"""
from collections import defaultdict

from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L

import benchmark as B
import config as C
from source_data import parse_date
from validation import Group, _f

SHEET = "Threshold_Analysis"
FY_START = C.BENCHMARK_FIRST_FY
HISTORIES = [("full", "Full history (all approval years)"), ("fy2000", f"FY{FY_START} onward (benchmark comparison basis)")]
GROUPS = [("total", "Total"), ("above", "Above threshold"), ("below", "At or below threshold")]
USD, PCT, INT, NUM = "#,##0", "0.00%", "#,##0", "0.0"
# key, heading, number format, kind ("sum" measures reconcile above + at-or-below = total)
MEASURES = [
    ("loans", "Loans originated (#)", INT, "sum"),
    ("orig", "Original gross approval ($)", USD, "sum"),
    ("guar", "Original SBA-guaranteed approval ($)", USD, "sum"),
    ("unguar", "Original unguaranteed approval ($)", USD, "sum"),
    ("avg", "Average original loan ($)", USD, "ratio"),
    ("active", "Active loans (#)", INT, "sum"),
    ("active_orig", "Original amount of active loans ($) - not a balance", USD, "sum"),
    ("age", "Average age of active loans (months)", NUM, "ratio"),
    ("chgoff", "Charged-off loans (#)", INT, "sum"),
    ("chgoff_orig", "Original amount of charged-off loans ($)", USD, "sum"),
    ("chgoff_amt", "Gross charge-off amount ($)", USD, "sum"),
    ("pif_orig", "Original amount of paid-in-full loans ($)", USD, "sum"),
    ("purch_orig", "Original amount PURCH(NOT C/O) ($)", USD, "sum"),
    ("cum_def", "% Cum. Defaulted", PCT, "ratio"),
    ("severity", "% Loss Severity", PCT, "ratio"),
    ("cum_loss", "% Cum. Loss", PCT, "ratio"),
    ("pif_pct", "% Paid in full", PCT, "ratio"),
    ("purch_pct", "PURCH(NOT C/O) (%)", PCT, "ratio"),
]
MKEYS = [m[0] for m in MEASURES]
NA = "n/a"


def _div(a, b):
    return a / b if b else NA


def measures(rows, status_map, as_of):
    """Measures of one set of loans, with the definitions of PerformanceSummary K:W (validation.Group)."""
    g = Group(rows, status_map, as_of)
    guar = sum(_f(r, "SBAGuaranteedApproval") for r in g.orig)
    pif = sum(_f(r, "GrossApproval") for r in g.orig if status_map[r[C.F_LOAN_STATUS].strip()] == "P I F")
    purch = g.raw("PURCH(NOT C/O)")
    return {"loans": g.K, "orig": g.M, "guar": guar, "unguar": g.M - guar, "avg": _div(g.M, g.K),
            "active": g.L, "active_orig": g.P, "age": _div(g.age, g.L), "chgoff": g.R, "chgoff_orig": g.co_orig,
            "chgoff_amt": g.co_amt, "pif_orig": pif, "purch_orig": purch, "cum_def": _div(g.co_orig, g.M),
            "severity": _div(g.co_amt, g.co_orig), "cum_loss": _div(g.co_amt, g.M), "pif_pct": _div(pif, g.M),
            "purch_pct": _div(purch, g.M)}


def is_above(row, threshold):
    v = row["GrossApproval"].strip()
    return B.above(float(v) if v else 0, threshold)


def in_fy2000(row):
    v = row["ApprovalFiscalYear"].strip()
    return bool(v) and float(v) >= FY_START


def row_keys(years, sector_labels, subprograms):
    return ([("total", "TOTAL")] + [("v", y) for y in years] + [("i", s) for s in sector_labels]
            + [("s", s) for s in subprograms])


def _key_of(row, kind):
    if kind == "v":
        return parse_date(row[C.F_FIRST_DISBURSEMENT]).year
    return row[C.F_SECTOR] if kind == "i" else row[C.F_SUBPROGRAM]


def compute(rows, status_map, as_of, threshold, years, sector_labels, subprograms):
    """{"blocks": {(history, group): {(kind, key): measures}}, "chgoff_by_year": ..., "status": ...}."""
    hist_rows = {"full": rows, "fy2000": [r for r in rows if in_fy2000(r)]}
    blocks = {}
    for h, _ in HISTORIES:
        for gk, _ in GROUPS:
            rs = [r for r in hist_rows[h] if gk == "total" or is_above(r, threshold) == (gk == "above")]
            buckets = {k: defaultdict(list) for k in ("v", "i", "s")}
            for r in rs:
                for k in buckets:
                    buckets[k][_key_of(r, k)].append(r)
            out = {}
            for kind, key in row_keys(years, sector_labels, subprograms):
                out[(kind, key)] = measures(rs if kind == "total" else buckets[kind].get(key, []), status_map, as_of)
            blocks[(h, gk)] = out
    # charge-offs by year of ChargeOffDate (status group CHGOFF, as in K:W) and loan status, full history
    groups = {gk: [r for r in rows if gk == "total" or is_above(r, threshold) == (gk == "above")] for gk, _ in GROUPS}
    chg_years = sorted({parse_date(r["ChargeOffDate"]).year for r in rows
                        if status_map[r[C.F_LOAN_STATUS].strip()] == "CHGOFF" and r["ChargeOffDate"].strip()})
    by_year = {}
    for gk, rs in groups.items():
        co = [r for r in rs if status_map[r[C.F_LOAN_STATUS].strip()] == "CHGOFF"]
        d = {y: [0, 0.0] for y in chg_years}
        undated = 0
        for r in co:
            if r["ChargeOffDate"].strip():
                v = d[parse_date(r["ChargeOffDate"]).year]
                v[0] += 1
                v[1] += _f(r, "GrossChargeOffAmount")
            else:
                undated += 1
        by_year[gk] = {"years": d, "undated": undated}
    statuses = sorted({r[C.F_LOAN_STATUS].strip() for r in rows})
    status = {gk: {s: [sum(1 for r in rs if r[C.F_LOAN_STATUS].strip() == s),
                       sum(_f(r, "GrossApproval") for r in rs if r[C.F_LOAN_STATUS].strip() == s)] for s in statuses}
              for gk, rs in groups.items()}
    return {"threshold": threshold, "blocks": blocks, "chgoff_years": chg_years, "chgoff_by_year": by_year,
            "statuses": statuses, "status": status}


# ---------------------------------------------------------------- worksheet
def _col(block, m):
    return 3 + block * len(MEASURES) + m


def layout(years, sector_labels, subprograms, chg_years, statuses):
    """Row and column positions of every value (shared by the writer and the tests)."""
    rows, r, prev = {}, 10, "total"
    for kind, key in row_keys(years, sector_labels, subprograms):
        if kind != prev:
            r += 1                                                   # section heading row before the first entry
            prev = kind
        rows[(kind, key)] = r
        r += 1
    first, last = 10, r - 1
    r += 2
    co_head = r
    co_rows = {y: co_head + 2 + i for i, y in enumerate(chg_years)}
    co_undated = co_head + 2 + len(chg_years)
    r = co_undated + 3
    st_head = r
    st_rows = {s: st_head + 2 + i for i, s in enumerate(statuses)}
    checks_head = st_head + 2 + len(statuses) + 2
    blocks = {(h, g): i for i, (h, g) in enumerate((h, g) for h, _ in HISTORIES for g, _ in GROUPS)}
    return {"rows": rows, "first": first, "last": last, "blocks": blocks, "co_head": co_head, "co_rows": co_rows,
            "co_undated": co_undated, "st_head": st_head, "st_rows": st_rows, "checks_head": checks_head,
            "cell": lambda h, g, kind_key, mkey: f"{L(_col(blocks[(h, g)], MKEYS.index(mkey)))}{rows[kind_key]}"}


def _ops(threshold):
    """Excel criteria equal to round-half-even(GrossApproval) > T for whole-dollar T: GrossApproval > T + 0.5, and an
    exact T + 0.5 rounds up only when T is odd (Python round, as in benchmark.above)."""
    return (">=", "<") if threshold % 2 else (">", "<=")


def write_sheet(wb, ps_info, status_map, rows, as_of, threshold, sector_labels):
    years = sorted(ps_info["year_rows"])
    subprograms = list(ps_info["subprogram_row_map"])
    res = compute(rows, status_map, as_of, threshold, years, sector_labels, subprograms)
    lay = layout(years, sector_labels, subprograms, res["chgoff_years"], res["statuses"])
    ws = wb.create_sheet(SHEET)
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="DCE6F1")
    up, down = _ops(threshold)
    lab = B.threshold_label(threshold)
    ws["A1"] = f"Original loan balance threshold analysis - selected bank, threshold ${threshold:,}"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = ("Bank loans split by original loan balance with the benchmark's rule. Each measure uses the definition "
                "of PerformanceSummary columns K:W on the loans of the group. Amounts are ORIGINAL approval amounts "
                "or gross charge-offs. Current outstanding principal is NOT AVAILABLE from SBA FOIA data.")
    ws["A3"], ws["B3"] = "Original loan balance threshold ($)", threshold
    ws["B3"].number_format = USD
    ws["A4"], ws["B4"] = "Classification value (threshold + 0.5)", threshold + 0.5
    ws["C4"] = (f"Above {lab}: GrossApproval {up} B4, i.e. GrossApproval rounded to whole dollars > ${threshold:,} "
                f"(the benchmark's Standard rule). At or below {lab}: GrossApproval {down} B4 (benchmark Small).")
    ws["A6"] = ("Comparison basis: the FY2000-onward blocks use ApprovalFiscalYear >= 2000, the benchmark's population "
                "rule. Bank definitions (status-based defaulted / active) are kept; the benchmark's are date-based "
                "(see Benchmark_Notes).")
    for (h, gk), b in lay["blocks"].items():
        c0 = _col(b, 0)
        ht = dict(HISTORIES)[h]
        gt = {"total": "Total", "above": f"Above {lab}", "below": f"At or below {lab}"}[gk]
        ws.cell(8, c0, f"{ht} - {gt}").font = bold
        for m, (_, heading, fmt, _) in enumerate(MEASURES):
            c = ws.cell(9, c0 + m, heading)
            c.font, c.fill = bold, head_fill
            c.alignment = Alignment(wrap_text=True, vertical="top")
            ws.column_dimensions[L(c0 + m)].width = 15
    ws.cell(9, 1, "Key").font = bold
    ws.cell(9, 2, "Row").font = bold
    ws.row_dimensions[9].height = 60
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 44
    section = {"v": "Vintage (year of FirstDisbursementDate)", "i": "Industry (NaicsDescription)",
               "s": "Subprogram"}
    seen = set()
    crit_row = {"total": ",FOIA!$AS:$AS,PerformanceSummary!$K$2",          # the template's bank key (as in K6)
                "v": ",FOIA!$AO:$AO,$A{r}", "i": ",FOIA!$Z:$Z,$A{r}", "s": ",FOIA!$U:$U,$A{r}"}
    crit_grp = {"total": "", "above": f',FOIA!$O:$O,"{up}"&$B$4', "below": f',FOIA!$O:$O,"{down}"&$B$4'}
    crit_hist = {"full": "", "fy2000": f',FOIA!$R:$R,">={FY_START}"'}
    for (kind, key), r in lay["rows"].items():
        if kind != "total" and kind not in seen:
            seen.add(kind)
            ws.cell(r - 1, 2, section[kind]).font = bold
        if kind == "total":
            ws.cell(r, 1, "TOTAL").font = bold
            ws.cell(r, 2, "All eligible loans").font = bold
        else:
            cell = ws.cell(r, 1, key)
            if isinstance(key, str) and key.startswith("="):
                cell.data_type = "s"
            lbl = ws.cell(r, 2, key if kind != "v" else f"Vintage {key}")
            if isinstance(key, str) and key.startswith("="):
                lbl.data_type = "s"
        for (h, gk), b in lay["blocks"].items():
            x = crit_row[kind].format(r=r) + crit_grp[gk] + crit_hist[h]
            a = {mk: f"{L(_col(b, i))}{r}" for i, mk in enumerate(MKEYS)}
            f = {
                "loans": f'=COUNTIFS(FOIA!$AX:$AX,"<>COMMIT"{x})',
                "orig": f'=SUMIFS(FOIA!$O:$O,FOIA!$AX:$AX,"<>COMMIT"{x})',
                "guar": f'=SUMIFS(FOIA!$P:$P,FOIA!$AX:$AX,"<>COMMIT"{x})',
                "unguar": f'={a["orig"]}-{a["guar"]}',
                "avg": f'=IF({a["loans"]}=0,"{NA}",{a["orig"]}/{a["loans"]})',
                "active": f'=COUNTIFS(FOIA!$AX:$AX,"EXEMPT",FOIA!$AV:$AV,TRUE{x})',
                "active_orig": f'=SUMIFS(FOIA!$O:$O,FOIA!$AX:$AX,"EXEMPT",FOIA!$AV:$AV,TRUE{x})',
                "age": f'=IF({a["active"]}=0,"{NA}",SUMIFS(FOIA!$AT:$AT,FOIA!$AX:$AX,"EXEMPT",FOIA!$AV:$AV,TRUE{x})'
                       f'/{a["active"]})',
                "chgoff": f'=COUNTIFS(FOIA!$AX:$AX,"CHGOFF"{x})',
                "chgoff_orig": f'=SUMIFS(FOIA!$O:$O,FOIA!$AX:$AX,"CHGOFF"{x})',
                "chgoff_amt": f'=SUMIFS(FOIA!$AL:$AL,FOIA!$AX:$AX,"CHGOFF"{x})',
                "pif_orig": f'=SUMIFS(FOIA!$O:$O,FOIA!$AX:$AX,"P I F"{x})',
                "purch_orig": f'=SUMIFS(FOIA!$O:$O,FOIA!$AI:$AI,"PURCH(NOT C/O)"{x})',
                "cum_def": f'=IF({a["orig"]}=0,"{NA}",{a["chgoff_orig"]}/{a["orig"]})',
                "severity": f'=IF({a["chgoff_orig"]}=0,"{NA}",{a["chgoff_amt"]}/{a["chgoff_orig"]})',
                "cum_loss": f'=IF({a["orig"]}=0,"{NA}",{a["chgoff_amt"]}/{a["orig"]})',
                "pif_pct": f'=IF({a["orig"]}=0,"{NA}",{a["pif_orig"]}/{a["orig"]})',
                "purch_pct": f'=IF({a["orig"]}=0,"{NA}",{a["purch_orig"]}/{a["orig"]})',
            }
            for i, (mk, _, fmt, _) in enumerate(MEASURES):
                c = ws.cell(r, _col(b, i), f[mk])
                c.number_format = fmt

    # --- charge-offs by charge-off year (full history)
    r0 = lay["co_head"]
    ws.cell(r0, 1, "Charge-offs by year of ChargeOffDate (loans in status group CHGOFF; full history)").font = bold
    ws.cell(r0 + 1, 1, "Year").font = bold
    for j, (gk, gt) in enumerate(GROUPS):
        name = {"total": "Total", "above": f"Above {lab}", "below": f"At or below {lab}"}[gk]
        ws.cell(r0 + 1, 3 + 2 * j, f"{name}: charged-off loans (#)").font = bold
        ws.cell(r0 + 1, 4 + 2 * j, f"{name}: gross charge-off amount ($)").font = bold
    for y, r in lay["co_rows"].items():
        ws.cell(r, 1, y)
        for j, (gk, _) in enumerate(GROUPS):
            x = crit_grp[gk]
            ws.cell(r, 3 + 2 * j, f'=COUNTIFS(FOIA!$AX:$AX,"CHGOFF",FOIA!$AQ:$AQ,$A{r}{x})').number_format = INT
            ws.cell(r, 4 + 2 * j, f'=SUMIFS(FOIA!$AL:$AL,FOIA!$AX:$AX,"CHGOFF",FOIA!$AQ:$AQ,$A{r}{x})').number_format = USD
    ru = lay["co_undated"]
    ws.cell(ru, 1, "No ChargeOffDate")
    ws.cell(ru, 2, "Charged-off loans without a ChargeOffDate (not in any year row)")
    first_co, last_co = r0 + 2, ru - 1
    for j, (gk, _) in enumerate(GROUPS):
        total_cell = lay["cell"]("full", gk, ("total", "TOTAL"), "chgoff")
        rng = f"{L(3 + 2 * j)}{first_co}:{L(3 + 2 * j)}{last_co}"
        ws.cell(ru, 3 + 2 * j, f"={total_cell}-SUM({rng})" if res["chgoff_years"] else f"={total_cell}")
        ws.cell(ru, 4 + 2 * j, f"={lay['cell']('full', gk, ('total', 'TOTAL'), 'chgoff_amt')}-SUM("
                               f"{L(4 + 2 * j)}{first_co}:{L(4 + 2 * j)}{last_co})" if res["chgoff_years"] else
                f"={lay['cell']('full', gk, ('total', 'TOTAL'), 'chgoff_amt')}")

    # --- loan status (raw LoanStatus, full history)
    r0 = lay["st_head"]
    ws.cell(r0, 1, "Loan status as of the FOIA AsOfDate (LoanStatus as published; full history)").font = bold
    ws.cell(r0 + 1, 1, "LoanStatus").font = bold
    for j, (gk, _) in enumerate(GROUPS):
        name = {"total": "Total", "above": f"Above {lab}", "below": f"At or below {lab}"}[gk]
        ws.cell(r0 + 1, 3 + 2 * j, f"{name}: loans (#)").font = bold
        ws.cell(r0 + 1, 4 + 2 * j, f"{name}: original gross approval ($)").font = bold
    for s, r in lay["st_rows"].items():
        ws.cell(r, 1, s)
        for j, (gk, _) in enumerate(GROUPS):
            x = crit_grp[gk]
            ws.cell(r, 3 + 2 * j, f"=COUNTIFS(FOIA!$AI:$AI,$A{r}{x})").number_format = INT
            ws.cell(r, 4 + 2 * j, f"=SUMIFS(FOIA!$O:$O,FOIA!$AI:$AI,$A{r}{x})").number_format = USD

    # --- checks: Python expected values (same definitions) and partitions
    checks = []
    for (h, gk), b in lay["blocks"].items():
        m = res["blocks"][(h, gk)][("total", "TOTAL")]
        for mk, label, fmt, _ in MEASURES:
            checks.append((f"{dict(HISTORIES)[h]} - {dict(GROUPS)[gk]}", label,
                           lay["cell"](h, gk, ("total", "TOTAL"), mk), m[mk], fmt))
    for h, ht in HISTORIES:
        for i, (mk, label, fmt, kind) in enumerate(MEASURES):
            if kind != "sum":
                continue
            rng = lambda gk: (f"{L(_col(lay['blocks'][(h, gk)], i))}{lay['first']}:"
                              f"{L(_col(lay['blocks'][(h, gk)], i))}{lay['last']}")
            checks.append((f"Partition - {ht}", f"{label}: rows where above + at or below <> total",
                           f"=SUMPRODUCT(--(ABS({rng('above')}+{rng('below')}-{rng('total')})>0.005))", 0, INT))
    r20 = ps_info["benchmark_rows"]["vintage_header"]
    for row, rlabel in ((6, "row 6"), (r20, f"row {r20} (vintage total)")):
        for off, label in ((0, "loans"), (1, "original GrossApproval"), (4, "active loans"),
                           (5, "original amount of active loans"), (6, "defaulted loans")):
            c = lambda base: f"PerformanceSummary!{L(_col_ix(base) + off)}{row}"
            checks.append((f"Partition - SBA benchmark {rlabel}", f"Standard + Small - All: {label}",
                           f"={c('AJ')}+{c('AV')}-{c('X')}", 0, USD))
    r0 = lay["checks_head"]
    ws.cell(r0, 1, "Checks - workbook formulas vs the Python recomputation (validation.Group definitions)").font = bold
    for c, h in enumerate(("Section", "Check", "Cell / formula", "Expected (recomputed)", "Workbook value",
                           "Difference", "Result"), start=1):
        ws.cell(r0 + 1, c, h).font = bold
    for i, (sec, label, ref, exp, fmt) in enumerate(checks, start=r0 + 2):
        ws.cell(i, 1, sec)
        ws.cell(i, 2, label)
        cref = ws.cell(i, 3, ref if not ref.startswith("=") else ref[1:])
        cref.data_type = "s"
        e = ws.cell(i, 4, exp)
        g = ws.cell(i, 5, ref if ref.startswith("=") else f'=IF({ref}="","",{ref})')
        ws.cell(i, 6, f'=IF(AND(ISNUMBER(D{i}),ISNUMBER(E{i})),E{i}-D{i},"")')
        ws.cell(i, 7, f'=IF(AND(ISNUMBER(D{i}),ISNUMBER(E{i})),IF(ABS(E{i}-D{i})<=1E-9*MAX(1,ABS(D{i})),"PASS","FAIL"),'
                      f'IF(E{i}&""=D{i}&"","PASS","FAIL"))')
        e.number_format = g.number_format = fmt
    last = r0 + 1 + len(checks)
    ws["A5"], ws["B5"] = "Checks on this sheet", len(checks)
    ws["C5"], ws["D5"] = "PASS", f'=COUNTIF(G{r0 + 2}:G{last},"PASS")'
    ws["E5"], ws["F5"] = "FAIL", f'=COUNTIF(G{r0 + 2}:G{last},"FAIL")'
    for a in ("A3", "A4", "A5"):
        ws[a].font = bold
    ws.freeze_panes = "C10"
    return {"sheet": SHEET, "threshold": threshold, "checks": len(checks), "layout": lay, "result": res}


def _col_ix(letter):
    from openpyxl.utils import column_index_from_string
    return column_index_from_string(letter)


# ---------------------------------------------------------------- dashboard views of the workbook values
BENCH_VIEW_GROUPS = [("", "total", "All SBA 7(a)"), ("_Standard", "above", "Standard"), ("_Small", "below", "Small"),
                     ("_Non-CRE", "noncre", "Non-CRE"), ("_CRE", "cre", "CRE")]


def _bench_row(m):
    """PerformanceSummary benchmark measures of one vintage or sector row (template formulas, with the empty-group
    safeguard): m = Combined_* metrics H I K N O Q T W Z AA."""
    H, I, K, N, O, Q, T, W, Z, AA = m
    cum_def = _div(Q, N)
    cum_loss = _div(T, N)
    if not N:
        sev = NA
    elif cum_loss == 0:
        sev = ""                                                     # template: IF(AG=0,"",AG/AE)
    else:
        sev = _div(cum_loss, cum_def) if cum_def != NA else NA
    return {"loans": H, "orig": N, "avg": _div(N, H), "active": I, "active_orig": O, "chgoff": K,
            "cum_def": cum_def, "severity": sev, "cum_loss": cum_loss, "purch_pct": _div(Z, N)}


def _bench_total(rows):
    """Row 6: sums over the lender's industries and the template's SUMPRODUCT-weighted averages (text counts as 0)."""
    nz = lambda v: v if isinstance(v, (int, float)) else 0
    s = lambda k: sum(r[k] for r in rows)
    N, D, H = s("orig"), s("chgoff"), s("loans")
    cum_def = sum(nz(r["cum_def"]) * r["orig"] for r in rows) / N if N else NA
    sev = sum(nz(r["severity"]) * r["chgoff"] for r in rows) / D if D else NA
    return {"loans": H, "orig": N, "avg": _div(N, H), "active": s("active"), "active_orig": s("active_orig"),
            "chgoff": D, "cum_def": cum_def, "severity": sev,
            "cum_loss": cum_def * sev if NA not in (cum_def, sev) else NA,
            "purch_pct": sum(nz(r["purch_pct"]) * r["orig"] for r in rows) / N if N else NA}


def benchmark_view(tabs, sector_labels):
    """The PerformanceSummary benchmark values shown by the dashboard (groups All, Standard = above the threshold,
    Small = at or below): row 6, vintage rows 1999-2026, the lender's sector rows."""
    out = {}
    years = range(C.BENCHMARK_FIRST_VINTAGE, C.TEMPLATE_HIDDEN_TAB_LAST_YEAR + 1)
    for sfx, key, name in BENCH_VIEW_GROUPS:
        sec = {s: _bench_row(tabs[sfx]["sector"][s]) for s in sector_labels}
        out[key] = {"name": name, "total": _bench_total(list(sec.values())), "sector": sec,
                    "vintage": {y: _bench_row(tabs[sfx]["vintage"].get(y, [0] * 10)) for y in years}}
    return out


def status_map_from_template(path=C.TEMPLATE_PATH):
    """The template's LoanStatus -> status group table (Lookup!J:K), read without loading the whole workbook."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    out = {}
    for r, (j, k) in enumerate(wb[C.SH_LOOKUP].iter_rows(min_row=4, min_col=10, max_col=11, values_only=True), start=4):
        if j is None:
            continue
        out[j] = j if isinstance(k, str) and k.strip() == f"=J{r}" else k
    wb.close()
    return out
