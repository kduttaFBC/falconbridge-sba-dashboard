"""Phase 1: mechanical changes to a COPY of FalconBridge's FinWise template (openpyxl).

* PerformanceSummary: bank-specific subprogram, vintage and sector rows generated from the
  template's own row formulas; approved N/A reconciliation changes (spec 09 §5a/§5b);
  FinWise securitization content cleared; benchmark X:CE either cleared or (when a benchmark is supplied) its
  template formulas re-pointed from the external SBA Orig_v2 workbook to hidden Combined_* sheets.
* Hidden sector tabs resized to the bank's sector list (incl. the final N/A row).
* FinWise-only content removed (loan tape, securitization tab, external links, pivot cache records).
The FOIA data rows themselves are written in phase 2 (foia_xml_writer) for speed.
"""
import re
from copy import copy
from datetime import datetime

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.comments import Comment
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formula.translate import Translator
from openpyxl.styles import Alignment, Font
from openpyxl.utils import column_index_from_string as CI, get_column_letter as L

import config as C
import template_corrections

PS_LAST_COL = CI("CE")
SECURITIZATION_COLS = range(CI("C"), CI("J") + 1)
BENCHMARK_COLS = range(CI("X"), CI("CE") + 1)
BANK_COLS = range(CI("K"), CI("W") + 1)
NA_CRITERION = re.compile(r',FOIA!\$Z:\$Z,"<>"&"N/A"')
CELL_REF = re.compile(r"(?<![A-Za-z_!])(\$?)([A-Z]{1,3})(\$?)(\d+)(?![\d(])")

# Template rows (original FinWise template layout)
T_SUB, T_SUB_BLANK, SUB_CAPACITY = 7, 18, 13          # subprogram rows 7..19
T_VINT_HDR, T_VINT_FIRST, T_VINT_LAST = 20, 21, 56     # vintage years 1991..2026
T_V1999, T_V2000 = 29, 30
T_VTOT, T_BLANK, T_FOOT, T_SEC_HDR, T_SEC = 57, 58, 59, 60, 61
TEMPLATE_FIRST_YEAR, TEMPLATE_LAST_YEAR = 1991, 2026

# Approved §5a reconciliation formulas (headline row 6): whole eligible population of the
# selected bank, using the template's own row key FOIA!AS (= PerformanceSummary!K2).
ROW6 = {
    "K": '=COUNTIFS(FOIA!$AS:$AS,$K$2,FOIA!$AX:$AX,"<>"&"COMMIT")',
    "L": '=COUNTIFS(FOIA!$AS:$AS,$K$2,FOIA!$AX:$AX,"EXEMPT",FOIA!$AV:$AV,TRUE)',
    "M": '=SUMIFS(FOIA!$O:$O,FOIA!$AS:$AS,$K$2,FOIA!$AX:$AX,"<>"&"COMMIT")',
    "N": "=M6/$M$6",
    "P": '=SUMIFS(FOIA!$O:$O,FOIA!$AS:$AS,$K$2,FOIA!$AX:$AX,"EXEMPT",FOIA!$AV:$AV,TRUE)',
    "R": '=COUNTIFS(FOIA!$AS:$AS,$K$2,FOIA!$AX:$AX,"CHGOFF")',
}


def _text(v):
    return getattr(v, "text", v)


def _is_formula(v):
    return isinstance(v, str) and v.startswith("=")


def _put_text(ws, row, col, value):
    """Write a value; text that starts with '=' is stored as text, never as a formula."""
    cell = ws.cell(row, col, value)
    if _is_formula(value):
        cell.data_type = "s"
    return cell


def _remove_na(formula):
    return NA_CRITERION.sub("", formula)


def remap_rows(formula, mapping):
    """Replace row numbers of cell references (relative or absolute) per `mapping`."""
    def sub(m):
        row = int(m.group(4))
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{mapping.get(row, row)}"
    out, last = [], 0
    for m in re.finditer(r'"[^"]*"', formula):           # do not touch string literals
        out.append(CELL_REF.sub(sub, formula[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(CELL_REF.sub(sub, formula[last:]))
    return "".join(out)


class RowTemplate:
    """Captured values, styles and dimensions of one template row; formulas re-anchored on demand."""

    def __init__(self, ws, row, last_col):
        self.row = row
        self.cells = []
        for c in range(1, last_col + 1):
            cell = ws.cell(row, c)
            v = _text(cell.value)
            tr = Translator(v, f"{L(c)}{row}") if _is_formula(v) else None
            self.cells.append((v, copy(cell._style), tr))
        d = ws.row_dimensions[row]
        self.height, self.hidden, self.outline = d.height, d.hidden, d.outlineLevel

    def formula_at(self, col, row):
        v, _, tr = self.cells[col - 1]
        return tr.translate_formula(f"{L(col)}{row}") if tr else v

    def write(self, ws, row, values=None, cols_with_values=None, hidden=None):
        """Write styles for all captured columns; values only for `cols_with_values` (re-anchored)."""
        values = values or {}
        for c, (v, style, tr) in enumerate(self.cells, start=1):
            cell = ws.cell(row, c)
            cell._style = copy(style)
            if c in values:
                cell.value = values[c]
            elif cols_with_values is None or c in cols_with_values:
                cell.value = tr.translate_formula(f"{L(c)}{row}") if tr else v
            else:
                cell.value = None
        d = ws.row_dimensions[row]
        d.height, d.outlineLevel = self.height, self.outline
        d.hidden = self.hidden if hidden is None else hidden


def _clear_rows(ws, first, last, last_col, reset_style_from=None):
    for r in range(first, last + 1):
        for c in range(1, last_col + 1):
            cell = ws.cell(r, c)
            cell.value = None
            if reset_style_from is not None:
                cell._style = copy(reset_style_from.cells[c - 1][1]) if c <= len(reset_style_from.cells) else cell._style
        if r in ws.row_dimensions:
            d = ws.row_dimensions[r]
            d.hidden, d.outlineLevel, d.height = False, 0, None


# ======================================================================= PerformanceSummary
def rebuild_performance_summary(wb, lists, as_of, first_fy, log, bench=None):
    ws = wb[C.SH_SUMMARY]
    head = {(r, c): _text(ws.cell(r, c).value) for r in (2, 3, 5, 6) for c in BENCHMARK_COLS}
    T = {r: RowTemplate(ws, r, PS_LAST_COL) for r in
         [T_SUB, T_SUB_BLANK, T_VINT_HDR] + list(range(T_VINT_FIRST, T_VINT_LAST + 1)) +
         [T_VTOT, T_BLANK, T_FOOT, T_SEC_HDR, T_SEC]}
    # Established subprogram display labels = the template's own A->B pairs (client direction 3)
    label_map = {}
    for r in range(T_SUB, T_SUB + 4):
        a, b = ws.cell(r, 1).value, ws.cell(r, 2).value
        if a and b:
            label_map[a] = b
    old_max = ws.max_row

    # --- rows 2-6: headers, securitization / benchmark removal, §5a totals
    for r in range(2, 7):
        for c in list(SECURITIZATION_COLS) + list(BENCHMARK_COLS):
            if r == 4:           # keep column headings
                continue
            cell = ws.cell(r, c)
            if isinstance(cell, MergedCell):     # non-anchor cell of a merged range
                continue
            cell.value = None
    ws["C2"] = ("Securitization data: NOT AVAILABLE from public SBA FOIA data (requires the lender's loan tape: "
                "current unguaranteed balances, WALA, credit and SBSS scores)")
    if not bench:
        for col in ("X", "AJ", "AV", "BH", "BT"):
            ws[f"{col}3"] = "NOT AVAILABLE - pending FalconBridge benchmark methodology"
    q = (as_of.month - 1) // 3 + 1
    ws["K3"] = f"FY{first_fy} - Q{q} {as_of.year} Originations (as of {as_of.month}/{as_of.year})"
    for col, f in ROW6.items():
        ws[f"{col}6"] = f
    ws["Q6"] = _remove_na(_text(ws["Q6"].value))                     # §5a Q6

    # --- layout of the regenerated block
    nsub = len(lists["subprograms"])
    extra_sub = max(0, nsub - SUB_CAPACITY)
    years = list(range(min(TEMPLATE_FIRST_YEAR, lists["vintages"][0]), max(TEMPLATE_LAST_YEAR, lists["vintages"][-1]) + 1))
    r_vhdr = T_VINT_HDR + extra_sub
    year_row = {y: r_vhdr + 1 + i for i, y in enumerate(years)}
    r_vtot = year_row[years[-1]] + 1
    r_blank, r_foot, r_sec_hdr = r_vtot + 1, r_vtot + 2, r_vtot + 3
    sector_rows = lists["sectors"] + ([lists["na"]] if lists["na"] else [])
    sec_row = {name: r_sec_hdr + 1 + i for i, (name, _, _) in enumerate(sector_rows)}
    last_row = r_sec_hdr + len(sector_rows)

    _clear_rows(ws, T_SUB, max(old_max, last_row), PS_LAST_COL)
    keep = set(range(1, 3)) | set(BANK_COLS)                          # A, B and bank block K..W

    # --- subprogram rows (dynamic; §5b removes the N/A criterion in L, P, Q)
    for i in range(max(SUB_CAPACITY, nsub)):
        r = T_SUB + i
        if i < nsub:
            name = lists["subprograms"][i][0]
            vals = {1: name, 2: label_map.get(name, name)}
            for c in BANK_COLS:
                f = T[T_SUB].formula_at(c, r)
                vals[c] = _remove_na(f) if _is_formula(f) else f
            T[T_SUB].write(ws, r, vals, cols_with_values=set(), hidden=False)
        else:
            T[T_SUB_BLANK].write(ws, r, cols_with_values=set(), hidden=True)
    for dv in ws.data_validations.dataValidation:                    # A3 subprogram drop-down list
        if dv.formula1 and "$A$7" in dv.formula1:
            dv.formula1 = f"$A${T_SUB}:$A${T_SUB + max(nsub, 1) - 1}"

    # --- vintage block (dynamically extended years; existing row formulas preserved; §5b)
    T[T_VINT_HDR].write(ws, r_vhdr, cols_with_values=keep)
    for i, y in enumerate(years):
        r = year_row[y]
        if y < TEMPLATE_FIRST_YEAR:
            t = T[T_VINT_FIRST]
        elif y > TEMPLATE_LAST_YEAR:
            t = T[T_VINT_LAST]
        else:
            t = T[T_VINT_FIRST + (y - TEMPLATE_FIRST_YEAR)]
        vals = {1: y if i == 0 else f"=A{r - 1}+1", 2: f"=A{r}"}
        for c in BANK_COLS:
            f = t.formula_at(c, r)
            vals[c] = _remove_na(f) if _is_formula(f) else f
        t.write(ws, r, vals, cols_with_values=set())
    # 2019-2026 totals row: original references to rows 49..56 re-pointed to the 2019..2026 rows
    mapping = {T_VTOT: r_vtot}
    for y in range(2019, 2027):
        mapping[T_VINT_FIRST + (y - TEMPLATE_FIRST_YEAR)] = year_row[y]
    vals = {2: T[T_VTOT].cells[1][0]}
    for c in BANK_COLS:
        v = T[T_VTOT].cells[c - 1][0]
        vals[c] = remap_rows(v, mapping) if _is_formula(v) else v
    T[T_VTOT].write(ws, r_vtot, vals, cols_with_values=set())
    T[T_BLANK].write(ws, r_blank, cols_with_values=set())
    T[T_FOOT].write(ws, r_foot, cols_with_values=set())               # FinWise footnote removed
    T[T_SEC_HDR].write(ws, r_sec_hdr, cols_with_values=keep)

    # --- sector rows (dynamic list; N/A last); existing row formulas unchanged
    for name, r in sec_row.items():
        vals = {1: name, 2: name}
        for c in BANK_COLS:
            vals[c] = T[T_SEC].formula_at(c, r)
        T[T_SEC].write(ws, r, vals, cols_with_values=set(), hidden=False)
    for r in range(last_row + 1, old_max + 1):                       # old FinWise rows below the new end
        T[T_BLANK].write(ws, r, cols_with_values=set(), hidden=False)

    log(f"PerformanceSummary: {nsub} subprogram rows, {len(years)} vintage rows ({years[0]}-{years[-1]}), "
        f"{len(sector_rows)} sector rows (rows {r_sec_hdr + 1}-{last_row})")
    if bench:
        _write_benchmark(ws, T, head, years, year_row, r_vhdr, r_vtot, r_sec_hdr, sec_row, bench, log)
    return {"benchmark_rows": {"vintage_header": r_vhdr, "vintage_total_2019_2026": r_vtot, "sector_header": r_sec_hdr},
            "sector_rows": sec_row, "years": years, "subprogram_rows": nsub, "last_row": last_row,
            "year_rows": year_row, "vintage_total_row": r_vtot,
            "subprogram_row_map": {lists["subprograms"][i][0]: T_SUB + i for i in range(nsub)}}


# ======================================================================= benchmark X:CE
BENCH_GROUPS = ["X", "AJ", "AV", "BH", "BT"]


def _divides(f):
    return "/" in re.sub(r'"[^"]*"', "", f)


def _guard_row(ws, base, r, rowtype, cond, wrapped_elsewhere=()):
    """Wrap, in one row of one benchmark group, only the ratio cells whose template formula would fail: a division by
    the group's zero count / amount, or arithmetic on a cell that now shows n/a. Formulas that already return a
    value (IFERROR, a lookup of the hidden 'na', a share of the all-SBA total in row 5) are left unchanged."""
    col = lambda i: L(CI(base) + i)
    wrapped = set(wrapped_elsewhere)

    def wrap(i):
        cell = f"{col(i)}{r}"
        ws[cell].value = f'=IF({cond},"n/a",{ws[cell].value[1:]})'
        wrapped.add(cell)

    for i in (2, 3, 7, 9, 10, 11, 8):                                # severity (8) last: it reads cum. loss (9)
        f = ws[f"{col(i)}{r}"].value
        if not _is_formula(f) or f.startswith("=IFERROR("):
            continue
        refs = {m.group(2) + m.group(4) for m in CELL_REF.finditer(re.sub(r'"[^"]*"', "", f))}
        uses_wrapped = bool(refs & {w.replace("$", "") for w in wrapped})
        if rowtype in ("sector", "vintage"):
            if i == 2 and rowtype == "sector":
                continue                                             # share of the all-SBA total (row 5): valid
            if i == 7:
                continue                                             # lookup of Combined_*!AC, which is IFERROR "na"
            if i == 8:
                if uses_wrapped:
                    wrap(i)
                continue
        if _divides(f) or (uses_wrapped and not re.fullmatch(r"=\$?[A-Z]+\$?\d+", f)):
            wrap(i)
    return wrapped


def _guard_benchmark(ws, guards, year_row, sec_row, r_vhdr, r_vtot, r_sec_hdr):
    """Approved safeguard (B2): where a benchmark group has no SBA loans (or no defaulted loans for the severity
    totals), the template's ratio formulas divide by zero (#DIV/0!). Only those cells are wrapped as
    =IF(<count>=0,"n/a",<template formula>); every formula with a valid denominator is left unchanged.
    guards[base] = {"vintage": {year: (loans, defaulted)}, "sector": {label: loans}, "row6": (loans, defaulted),
    "r20": (loans, defaulted), "r57": loans}."""
    before = {c.coordinate: c.value for row in ws.iter_rows(min_col=CI("X"), max_col=PS_LAST_COL) for c in row}
    col = lambda base, i: L(CI(base) + i)
    for base, g in guards.items():
        cnt = lambda r: f"{col(base, 0)}{r}=0"
        dft = lambda r: f"{col(base, 6)}{r}=0"
        empty20 = g["r20"][0] == 0
        for y, (loans, _) in g["vintage"].items():
            if loans == 0 and y in year_row:
                r = year_row[y]
                w = _guard_row(ws, base, r, "vintage", cnt(r))
                if not empty20 and f"{col(base, 2)}{r}" in w:       # share of a non-empty vintage total: valid
                    ws[f"{col(base, 2)}{r}"].value = before[f"{col(base, 2)}{r}"]
        for label, loans in g["sector"].items():
            if loans == 0:
                _guard_row(ws, base, sec_row[label], "sector", cnt(sec_row[label]))
        loans6, def6 = g["row6"]
        w6 = set()
        if loans6 == 0:
            w6 = _guard_row(ws, base, 6, "total", cnt(6))
        elif def6 == 0:
            for i in (8, 9):                                         # severity / cum. loss divide by the defaults
                cell = f"{col(base, i)}6"
                ws[cell].value = f'=IF({dft(6)},"n/a",{ws[cell].value[1:]})'
                w6.add(cell)
        if w6:
            _guard_row(ws, base, r_sec_hdr, "total", cnt(6) if loans6 == 0 else dft(6), w6)   # row 60 repeats row 6
        loans20, def20 = g["r20"]
        if loans20 == 0:
            _guard_row(ws, base, r_vhdr, "total", cnt(r_vhdr), w6)
        elif def20 == 0 or def6 == 0:
            cell = f"{col(base, 8)}{r_vhdr}"
            ws[cell].value = f'=IF(OR({dft(r_vhdr)},{dft(6)}),"n/a",{ws[cell].value[1:]})'
        if g["r57"] == 0:
            _guard_row(ws, base, r_vtot, "total", cnt(r_vtot))
    return sum(1 for row in ws.iter_rows(min_col=CI("X"), max_col=PS_LAST_COL) for c in row
               if before.get(c.coordinate) != c.value)


def _bench_formula(f, mapping):
    """Template benchmark formula with the external SBA Orig_v2 link removed ('[2]Combined_*' -> 'Combined_*') and
    template row numbers re-pointed to the generated layout. Nothing else in the formula changes."""
    return remap_rows(f.replace("[2]", ""), mapping) if _is_formula(f) else f


def _write_benchmark(ws, T, head, years, year_row, r_vhdr, r_vtot, r_sec_hdr, sec_row, bench, log):
    vmap = {T_VINT_HDR: r_vhdr, T_VTOT: r_vtot, T_SEC_HDR: r_sec_hdr}
    for y in years:
        if TEMPLATE_FIRST_YEAR <= y <= TEMPLATE_LAST_YEAR:
            vmap[T_VINT_FIRST + (y - TEMPLATE_FIRST_YEAR)] = year_row[y]
    first_sec, last_sec = min(sec_row.values()), max(sec_row.values())
    t6 = re.match(r"=SUM\(X(\d+):X(\d+)\)", head[(6, CI("X"))])          # template: =SUM(X61:X294)
    map6 = {int(t6.group(1)): first_sec, int(t6.group(2)): last_sec}
    titles = bench.get("group_titles") or {}
    for c in BENCHMARK_COLS:
        if not isinstance(ws.cell(2, c), MergedCell) and head[(2, c)] is not None:
            ws.cell(2, c).value = titles.get(L(c), head[(2, c)])            # group titles (size groups: threshold)
        if head[(6, c)] is not None:
            ws.cell(6, c).value = _bench_formula(head[(6, c)], map6)
    for col in BENCH_GROUPS:
        ws[f"{col}3"] = bench["period_label"]
        ws[f"{col}5"], ws[f"{L(CI(col) + 1)}5"] = bench["totals"]          # typed in the template too (row 5)
    ws["X1"] = bench["banner"]
    ws["X1"].font = copy(ws["X3"].font)
    for t_row, r in [(T_VINT_HDR, r_vhdr), (T_VTOT, r_vtot), (T_SEC_HDR, r_sec_hdr)] + \
            [(t, vmap[t]) for t in range(T_VINT_FIRST, T_VINT_LAST + 1) if t in vmap]:
        for c in BENCHMARK_COLS:
            f = T[t_row].cells[c - 1][0]
            if f is not None:
                ws.cell(r, c).value = _bench_formula(f, vmap)
    for r in sec_row.values():
        for c in BENCHMARK_COLS:
            f = T[T_SEC].cells[c - 1][0]
            if f is not None:
                ws.cell(r, c).value = _bench_formula(f, {T_SEC: r})
    if bench.get("guards"):
        n = _guard_benchmark(ws, bench["guards"], year_row, sec_row, r_vhdr, r_vtot, r_sec_hdr)
        if n:
            log(f"Benchmark X:CE: {n} ratio cells guarded (n/a) where a group has no SBA loans")
    left = [f"{ws.title}!{cell.coordinate}" for row in ws.iter_rows(min_col=CI("X"), max_col=PS_LAST_COL)
            for cell in row if isinstance(cell.value, str) and re.search(r"\[\d+\]", cell.value)]
    if left:
        raise RuntimeError(f"Benchmark formulas still reference an external workbook: {left[:5]}")
    log(f"Benchmark X:CE: template formulas re-pointed to hidden Combined_* sheets (vintage rows {year_row[1999]}-"
        f"{year_row[2026]}, sector rows {first_sec}-{last_sec})")


def flag_benchmark_cells(ws, ps_info):
    """Comments on benchmark cells whose template formula is internally inconsistent (kept unchanged, provisionally).
    Returns [(issue, cells, explanation)] for the Benchmark_Notes sheet."""
    br = ps_info["benchmark_rows"]
    r20, r60 = br["vintage_header"], br["sector_header"]
    vy = ps_info["year_rows"]
    v1, v2 = vy[1999], vy[2026]
    secs = sorted(ps_info["sector_rows"].values())
    s1, s2 = secs[0], secs[-1]
    col = lambda base, i: L(CI(base) + i)
    who = "Bank Workbook Generator"
    items = []
    liq = [col(g, 10) for g in BENCH_GROUPS[1:]]
    items.append(("1. LIQUID (%) divides by loan count (sub-groups)",
                  ", ".join(f"{c}{v1}:{c}{v2}" for c in liq) + f"; {col('BH', 10)}6, {col('BT', 10)}6",
                  "Vintage rows of Standard, Small, Non-CRE and CRE divide LIQUID dollars by the NUMBER of loans "
                  "(All SBA divides by Orig. Bal.), and BR6/CD6 weight LIQUID by Est. Orig. Bal. Outstanding instead of "
                  "Orig. Bal. Not a valid percentage. Shows 0% only because this FOIA release has no LIQUID status."))
    for c in liq:
        ws[f"{c}4"].comment = Comment("ANALYST REVIEW: on vintage rows this template formula divides LIQUID $ by the "
                                      "loan count, not Orig. Bal. Kept unchanged pending FalconBridge confirmation; "
                                      "not a validated measure. See Benchmark_Notes.", who)
    for c in (col("BH", 10), col("BT", 10)):
        ws[f"{c}6"].comment = Comment("ANALYST REVIEW: weighted by Est. Orig. Bal. Outstanding (other groups weight by "
                                      "Orig. Bal.). Kept unchanged pending FalconBridge confirmation.", who)
    r20cells = [f"{col(g, i)}{r20}" for g in BENCH_GROUPS for i in (8, 9, 10, 11)]
    items.append(("2. Vintage total row mixes populations",
                  ", ".join(r20cells),
                  f"In the 'TOTAL by Vintage' row ({r20}), % Cum. Loss, LIQUID and PURCH are copied from row 6 (all "
                  "vintages of the lender's industries), while % Cum. Defaulted is the vintage-weighted figure; "
                  "% Loss Severity divides one by the other. Do not read these four cells as vintage totals."))
    for a in r20cells:
        ws[a].comment = Comment(f"ANALYST REVIEW: this cell uses row 6 (all vintages), not the vintage rows above "
                                "(template formula kept unchanged). See Benchmark_Notes.", who)
    pct = [col(g, 2) for g in BENCH_GROUPS[1:]]
    items.append(("3. Sub-group '% of Total Orig.' uses the All-SBA total",
                  ", ".join(f"{c}{s1}:{c}{s2}" for c in pct) + ", " + ", ".join(f"{c}6, {c}{r60}" for c in pct),
                  "On sector rows (and their row 6 / row 60 totals) Standard, Small, Non-CRE and CRE divide by the "
                  "All SBA 7(a) total in row 5, so the figure is the share of ALL SBA originations, not of the "
                  "sub-group. Vintage rows use the sub-group's own total."))
    for c in pct:
        ws[f"{c}4"].comment = Comment("ANALYST REVIEW: on sector rows the denominator is the All SBA 7(a) total "
                                      "(row 5), not this sub-group's total. Template formula kept. See Benchmark_Notes.", who)
    return items


def add_benchmark_notes(wb, rows):
    ws = wb.create_sheet(C.SH_BENCH_NOTES)
    ws["A1"] = "Industry-matched SBA 7(a) benchmark (PerformanceSummary X:CE) - PRELIMINARY"
    ws["A1"].font = Font(bold=True, size=13)
    r = 3
    for kind, a, b, c in rows:
        if kind == "head":
            r += 1
            ws.cell(r, 1, a).font = Font(bold=True)
        else:
            _put_text(ws, r, 1, a)
            _put_text(ws, r, 2, b)
            if c:
                _put_text(ws, r, 3, c)
        for k in (1, 2, 3):
            ws.cell(r, k).alignment = Alignment(wrap_text=True, vertical="top")
        r += 1
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 70
    ws.column_dimensions["C"].width = 90


# ======================================================================= hidden sector tabs
def _resize_sector_tab(ws, first_row, old_last, n, link_col, link_sheet_col, ps_rows, extra_values=None,
                       clear_cols=(), capacity_last=None):
    last_col = ws.max_column
    t = RowTemplate(ws, first_row, last_col)
    _clear_rows(ws, first_row, max(old_last, ws.max_row), last_col)
    if capacity_last and first_row + n - 1 > capacity_last:
        raise ValueError(f"{ws.title}: {n} sectors exceed template capacity (rows {first_row}-{capacity_last})")
    for i, ps_row in enumerate(ps_rows):
        r = first_row + i
        vals = {link_col: f"=PerformanceSummary!{link_sheet_col}{ps_row}"} if link_col else {}
        for c, v in (extra_values or {}).items():
            vals[c] = v
        for c in clear_cols:
            vals[c] = None
        t.write(ws, r, vals)
    return first_row + n - 1


def _set_cf_last_row(ws, last_row):
    old = ws.conditional_formatting
    new = ConditionalFormattingList()
    for cf in old:
        ranges = []
        for rng in str(cf.sqref).split():
            m = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)$", rng)
            ranges.append(f"{m.group(1)}{m.group(2)}:{m.group(3)}{last_row}" if m else rng)
        for rule in cf.rules:
            new.add(" ".join(ranges), rule)
    ws.conditional_formatting = new


def resize_hidden_tabs(wb, ps_info, log):
    ps_rows = list(ps_info["sector_rows"].values())
    n = len(ps_rows)
    # ChgOffs: rows 4.., A links to PerformanceSummary!B; AN "Yes" drives FOIA!AR Sector_Pick.
    ws = wb[C.SH_CHGOFFS]
    na_col = CI("NA")
    for r in range(1, ws.max_row + 1):                                 # loan-tape link column
        ws.cell(r, na_col).value = None
    _resize_sector_tab(ws, 4, 237, n, 1, "B", ps_rows, extra_values={CI("AN"): "Yes"},
                       clear_cols=(na_col,), capacity_last=1330)
    # OrigAmt: rows 5.., C links to PerformanceSummary!B
    ws = wb[C.SH_ORIGAMT]
    last = _resize_sector_tab(ws, 5, 238, n, 3, "B", ps_rows)
    _set_cf_last_row(ws, max(last, 5))
    # CumPrepayments: rows 5.., B/C link to OrigAmt (relative); column A = FinWise loan tape (cleared)
    ws = wb[C.SH_CUMPREPAY]
    for r in range(1, ws.max_row + 1):
        if r >= 4:
            ws.cell(r, 1).value = None
    last = _resize_sector_tab(ws, 5, 354, n, None, None, ps_rows, clear_cols=(1,))
    _set_cf_last_row(ws, max(last, 5))
    log(f"Hidden sector tabs resized to {n} rows (ChgOffs, OrigAmt, CumPrepayments)")


# ======================================================================= FinWise-only content
def remove_finwise_content(wb, log):
    for name in C.SHEETS_TO_REMOVE:
        if name in wb.sheetnames:
            wb.remove(wb[name])
    lk = wb[C.SH_LOOKUP]
    for r in range(1, lk.max_row + 1):
        for c in (CI("O"), CI("P")):
            lk.cell(r, c).value = None
    foia = wb[C.SH_FOIA]
    foia["AY1"] = None
    # Pivot: drop FinWise cached records; Excel rebuilds the pivot from the new FOIA rows on open.
    sp = wb[C.SH_PIVOT]
    for pt in sp._pivots:
        pt.cache.refreshOnLoad = True
        pt.cache.records = None
        pt.cache.id = None
        pt.cache.saveData = False
    for row in sp.iter_rows(min_row=4, max_row=sp.max_row, max_col=4):
        for cell in row:
            cell.value = None
    log("Removed: Sheet1, ChargeOffsByVintage_Sec, Lookup O:P, FOIA AY, pivot cache records")


def remove_external_links(wb, log):
    # Any formula still pointing at an external workbook ([1], [2]) is a FinWise remnant.
    leftovers = []
    for ws in wb.worksheets:
        if ws.title == C.SH_FOIA:
            continue
        for row in ws.iter_rows():
            for cell in row:
                v = _text(cell.value)
                if isinstance(v, str) and v.startswith("=") and re.search(r"\[\d+\]", v):
                    leftovers.append(f"{ws.title}!{cell.coordinate}")
    if leftovers:
        raise RuntimeError(f"External-link formulas remain after cleanup: {leftovers[:10]}")
    wb._external_links = []
    log("Removed external links to FinWise loan tape / benchmark workbooks (no formula references remain)")


def update_glossary(wb, first_fy, as_of, bench=False):
    g = wb[C.SH_GLOSSARY]
    quarter = f"Q{(as_of.month - 1) // 3 + 1} {as_of.year}"
    for row in g.iter_rows():
        for cell in row:
            if isinstance(cell.value, str) and ("FY2003" in cell.value or "Q2 2026" in cell.value):
                cell.value = cell.value.replace("FY2003", f"FY{first_fy}").replace("Q2 2026", quarter)
    g["B5"] = "Securitization (not applicable - FinWise-specific content removed)"
    g["B26"] = ("All SBA 7(a) - PRELIMINARY industry-matched SBA benchmark (see Benchmark_Notes)" if bench else
                "All SBA 7(a) (benchmark not available - pending FalconBridge methodology)")


def lookup_status_map(wb):
    """The template's own LoanStatus -> status-group table (Lookup!J:K), which drives FOIA!AX."""
    lk = wb[C.SH_LOOKUP]
    out = {}
    for r in range(4, lk.max_row + 1):
        j, k = lk.cell(r, CI("J")).value, _text(lk.cell(r, CI("K")).value)
        if j is None:
            continue
        out[j] = j if _is_formula(k) and k.strip() == f"=J{r}" else k
    return out


def add_validation_sheet(wb, checks):
    """Validation sheet: each key cell's live value against the expected value recomputed in Python from the same
    FOIA rows, with the template formula shown. checks = [(section, label, cell, fields, expected, fmt)]."""
    ws = wb.create_sheet(C.SH_VALIDATION)
    ws["A1"] = "Validation - workbook formulas vs independent recomputation from the FOIA rows"
    ws["A2"] = ("Expected = recomputed in Python from the loan rows written to the FOIA tab, using the template's own "
                "definitions. Workbook = live link to the cell. Amounts are ORIGINAL approval amounts or gross "
                "charge-offs from SBA FOIA - never current balances.")
    ws["A3"] = "Checks"
    ws["B3"] = len(checks)
    ws["C3"] = "PASS"
    ws["D3"] = '=COUNTIF(I:I,"PASS")'
    ws["E3"] = "FAIL"
    ws["F3"] = '=COUNTIF(I:I,"FAIL")'
    heads = [("Section", 26), ("Check", 46), ("Cell", 26), ("FOIA source fields", 34), ("Formula in cell", 70),
             ("Expected (recomputed)", 18), ("Workbook value", 18), ("Difference", 12), ("Result", 9)]
    for c, (h, w) in enumerate(heads, start=1):
        ws.cell(5, c, h)
        ws.column_dimensions[L(c)].width = w
    for i, (section, label, ref, fields, expected, fmt) in enumerate(checks, start=6):
        if ref.startswith("="):                 # a check formula of its own (supporting reconciliation)
            src, shown, live = ref, "(this row)", ref
        else:
            sheet, cell = ref.split("!")
            src, shown, live = _text(wb[sheet.strip("'")][cell].value), ref, f'=IF({ref}="","",{ref})'
        _put_text(ws, i, 1, section)
        _put_text(ws, i, 2, label)
        _put_text(ws, i, 3, shown)
        _put_text(ws, i, 4, fields)
        _put_text(ws, i, 5, src[1:] if _is_formula(src) else src)
        e = ws.cell(i, 6, expected)
        g = ws.cell(i, 7, live)
        ws.cell(i, 8, f'=IF(AND(ISNUMBER(F{i}),ISNUMBER(G{i})),G{i}-F{i},"")')
        ws.cell(i, 9, f'=IF(AND(ISNUMBER(F{i}),ISNUMBER(G{i})),IF(ABS(G{i}-F{i})<=1E-9*MAX(1,ABS(F{i})),"PASS","FAIL"),'
                      f'IF(G{i}&""=F{i}&"","PASS","FAIL"))')
        if fmt:
            e.number_format = g.number_format = fmt
    ws.freeze_panes = "A6"


def add_generation_info(wb, info):
    ws = wb.create_sheet(C.SH_INFO)
    ws["A1"] = "Bank Workbook Generator - generation record"
    for i, (k, v) in enumerate(info, start=3):
        _put_text(ws, i, 1, k)
        _put_text(ws, i, 2, v)
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 90


# ======================================================================= entry point
def build_template_copy(out_path, lists, as_of, info_rows, n_rows, log=print, validation_checks=None, bench=None):
    """Load the original template (read-only), apply mechanical changes, save to out_path.
    validation_checks(ps_info, status_map) -> checks for the Validation sheet (optional).
    bench: {"period_label", "banner", "totals", "write_sheets"(wb), "notes"(flags) -> rows} or None (X:CE cleared).
    Returns layout facts needed by phase 2 (FOIA helper formulas, styles)."""
    wb = load_workbook(C.TEMPLATE_PATH)
    foia = wb[C.SH_FOIA]
    helper = {c: _text(foia.cell(2, c).value) for c in range(CI("AO"), CI("AX") + 1)}
    if foia.max_row > 2:
        foia.delete_rows(3, foia.max_row - 2)                         # keep header + row 2 (styles)
    foia["AW2"] = datetime(as_of.year, as_of.month, as_of.day)
    foia["AY2"] = None
    if foia.auto_filter.ref:
        foia.auto_filter.ref = f"A1:AX{n_rows + 1}"
    remove_finwise_content(wb, log)
    ps_info = rebuild_performance_summary(wb, lists, as_of, lists["first_approval_fy"], log, bench=bench)
    resize_hidden_tabs(wb, ps_info, log)
    correction_rows, ps_info["template_corrections"] = template_corrections.apply(wb, log)   # guarded; copy only
    update_glossary(wb, lists["first_approval_fy"], as_of, bench=bool(bench))
    if bench:
        flags = flag_benchmark_cells(wb[C.SH_SUMMARY], ps_info)
        add_benchmark_notes(wb, bench["notes"](flags))
        ps_info["benchmark_sheets"] = bench["write_sheets"](wb)
        ps_info["benchmark_flags"] = flags
    if foia["AW1"].value != "No":
        raise RuntimeError("Template FOIA!AW1 (maturity filter) is not 'No'; the validation definitions assume it is off.")
    add_generation_info(wb, list(info_rows) + correction_rows)
    if validation_checks:
        add_validation_sheet(wb, validation_checks(ps_info, lookup_status_map(wb)))
    if bench and bench.get("extra_sheets"):         # appended last, so the existing sheets keep their positions
        ps_info["extra_sheets"] = bench["extra_sheets"](wb, ps_info, lookup_status_map(wb))
    remove_external_links(wb, log)
    wb.calculation.fullCalcOnLoad = True
    wb.save(out_path)
    return {"helper_formulas": helper, "ps_info": ps_info}
