"""Year-alignment scanner for the template's year-by-year grid ChgOffs_byNAICs&ChgOffYr.

The sheet is a set of blocks, each titled in row 1, with one column per calendar year (years in row 3); row 2 is the
Grand Total and rows 4.. are industries. The rate blocks are defined, for every year y and every row, as

  Estimated Prepayments by Prepay Year        = Paid In Full Loans (y)            / Balance at Year-End (y-1)
  Estimated Liquidations by Charge-off Year   = Charge-off Loans, original $ (y)  / Balance at Year-End (y-1)
  Estimated Loss Severity by Charge-Off Year  = Charge-off Loans, charge-off $ (y) / Charge-off Loans, original $ (y)

The scanner maps every same-sheet reference to its (block, year) from the sheet's own headers and reports:
  rate        a rate cell whose numerator or denominator is not the (block, year) above (e.g. a numerator one year
              late, or a reference to an empty spacer column)
  pattern     a Grand Total (row 2) formula whose year references differ from the same column's industry formula
              (row 4), e.g. a SUM range that stops at an earlier year
  total-sum   a Grand Total cell of a data block that is not =SUM(<own column>4:<own column>N) over every industry row
  missing-total  a blank Grand Total cell between filled Grand Total cells of the same block
Not findings, reported separately: the first year column of each rate block (1990), which has no prior year and
divides by the industry-name column by design ("first_year"); Grand Totals the template leaves blank before its first
filled year (it starts some Grand Totals at 2006: "leading_blank_totals"); and a summary column that totals the
industry rows' own totals (e.g. FD2 = SUM(FD4:FD1331)), which equals the year-range sum.
"""
import re

from openpyxl.utils import column_index_from_string as CI, get_column_letter as L

SHEET = "ChgOffs_byNAICs&ChgOffYr"
BAL, PIF, CO_ORIG, CO_AMT = "Balance at Year-End", "Paid In Full Loans", "Charge-off Loans (original $)", \
    "Charge-off Loans (charge-off $)"
RATE_RULES = {
    "Estimated Prepayments by Prepay Year": ((PIF, 0), (BAL, -1)),
    "Estimated Liquidations by Charge-off Year": ((CO_ORIG, 0), (BAL, -1)),
    "Estimated Loss Severity by Charge-Off Year": ((CO_AMT, 0), (CO_ORIG, 0)),
}
_RANGE = re.compile(r"(?<![A-Za-z0-9_!'\]\.])\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)")
_CELL = re.compile(r"(?<![A-Za-z0-9_!'\]\.:$])(\$?)([A-Z]{1,3})(\$?)(\d+)(?![\d(:])")
_SUM_COL = re.compile(r"^=SUM\(([A-Z]{1,3})4:([A-Z]{1,3})(\d+)\)$")


def cells_of(ws):
    """{(row, col): value} for a worksheet (normal or read-only)."""
    return {(r, c): v for r, row in enumerate(ws.iter_rows(values_only=True), 1)
            for c, v in enumerate(row, 1) if v is not None}


def layout(cells):
    """Block title and year of every year column, from rows 1 and 3. The two 'Charge-off Loans' blocks are told apart
    by order (original $ first, charge-off $ second), as in the template."""
    titles = {c: v.strip() for (r, c), v in cells.items() if r == 1 and isinstance(v, str) and v.strip()}
    starts = sorted(titles)
    co = [s for s in starts if titles[s] == "Charge-off Loans"]
    if len(co) != 2:
        raise ValueError(f"{SHEET}: expected two 'Charge-off Loans' blocks, found {len(co)}")
    names = {s: (CO_ORIG if s == co[0] else CO_AMT) if s in co else titles[s] for s in starts}
    years = {}
    for (r, c), v in cells.items():
        if r == 3 and (isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit())):
            owner = [s for s in starts if s <= c]
            if owner:
                years[c] = (names[owner[-1]], int(v))
    return years


def _row_refs(formula, row, years):
    """Year-grid references in `formula` that point at `row`: ('cell', block, year) and ('range', block, y0, y1)."""
    out = []
    for m in _RANGE.finditer(formula):
        c0, r0, c1, r1 = CI(m.group(1)), int(m.group(2)), CI(m.group(3)), int(m.group(4))
        if r0 == r1 == row and c0 in years and c1 in years:
            out.append(("range", years[c0][0], years[c0][1], years[c1][0], years[c1][1]))
    rest = _RANGE.sub("", formula)
    for m in _CELL.finditer(rest):
        c, r = CI(m.group(2)), int(m.group(4))
        if r == row:
            out.append(("cell",) + (years[c] if c in years else ("not-a-year-column:" + m.group(2), None)))
    return sorted(out, key=str)


def scan(cells):
    """Return {"findings": [...], "first_year": [...], "rate_cells_checked": n, "pattern_cells_checked": n}."""
    years = layout(cells)
    first = {}
    for c, (blk, y) in years.items():
        first[blk] = min(first.get(blk, y), y)
    last_row = max(r for (r, c), v in cells.items() if r >= 4 and c == CI("B") and isinstance(v, str) and v.strip())
    findings, first_year, n_rate, n_pat = [], [], 0, 0
    for (r, c), f in sorted(cells.items()):
        if r < 2 or r == 3 or not (isinstance(f, str) and f.startswith("=")) or c not in years:
            continue
        blk, y = years[c]
        if blk not in RATE_RULES:
            continue
        if y == first[blk]:
            first_year.append(f"{L(c)}{r}")
            continue
        n_rate += 1
        refs = [x for x in _row_refs(f, r, years) if x[0] == "cell"]
        (nb, no), (db, do) = RATE_RULES[blk]
        expected = sorted([("cell", nb, y + no), ("cell", db, y + do)], key=str)
        if refs != expected:
            findings.append({"cell": f"{L(c)}{r}", "rule": "rate", "year": y, "block": blk, "formula": f,
                             "found": refs, "expected": expected})
    # a blank Grand Total cell between filled ones in the same block is a missing total ("hole"); leading blank cells
    # (the template shows some Grand Totals only from 2006) are reported separately as a design choice
    leading_blank = []
    for blk in sorted({b for b, _ in years.values()}):
        cols = sorted(c for c in years if years[c][0] == blk)
        filled = [c for c in cols if isinstance(cells.get((2, c)), str) and str(cells[(2, c)]).startswith("=")]
        if not filled:
            continue
        for c in cols:
            if c in filled or years[c][1] == first[blk] and blk in RATE_RULES:
                continue
            if c < filled[0]:
                leading_blank.append(f"{L(c)}2")
            else:
                findings.append({"cell": f"{L(c)}2", "rule": "missing-total", "year": years[c][1], "block": blk,
                                 "formula": None, "expected": "a Grand Total formula like its neighbours"})
    for c in sorted({c for (r, c) in cells if r == 2}):
        f2, f4 = cells.get((2, c)), cells.get((4, c))
        if not (isinstance(f2, str) and f2.startswith("=")):
            continue
        m = _SUM_COL.match(f2)
        if m and m.group(1) == m.group(2) == L(c) and c not in years:
            continue                       # column total of the industry rows' own totals (e.g. FD2): equivalent form
        if c in years and years[c][0] not in RATE_RULES:          # data block Grand Total
            if not (m and m.group(1) == m.group(2) == L(c) and int(m.group(3)) >= last_row):
                findings.append({"cell": f"{L(c)}2", "rule": "total-sum", "formula": f2,
                                 "expected": f"=SUM({L(c)}4:{L(c)}<n>), n >= {last_row}"})
            continue
        if c in years and years[c][1] == first[years[c][0]]:
            continue
        if isinstance(f4, str) and f4.startswith("="):
            n_pat += 1
            grid = lambda f, r: [x for x in _row_refs(f, r, years) if x[0] == "range" or x[2] is not None]
            if grid(f2, 2) != grid(f4, 4):
                findings.append({"cell": f"{L(c)}2", "rule": "pattern", "formula": f2, "industry_formula": f4,
                                 "found": grid(f2, 2), "expected": grid(f4, 4)})
    return {"findings": findings, "first_year": first_year, "leading_blank_totals": leading_blank,
            "rate_cells_checked": n_rate, "pattern_cells_checked": n_pat}


def misaligned_cells(cells):
    return sorted({x["cell"] for x in scan(cells)["findings"]}, key=lambda a: (int(re.sub("[A-Z]", "", a)), CI(re.sub(r"\d", "", a))))
