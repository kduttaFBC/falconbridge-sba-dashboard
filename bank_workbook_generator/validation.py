"""Expected values for the workbook's Validation sheet, recomputed in Python from the eligible FOIA rows.

Each expected value follows the definition of the template formula in that cell (FinWise template, with the
approved changes made by template_populator):
  FOIA!AX  status group = Lookup!J:K applied to LoanStatus      FOIA!AO  year of FirstDisbursementDate
  FOIA!AV  maturity flag = TRUE for every loan (FOIA!AW1 = "No") FOIA!AT  DATEDIF(FirstDisbursementDate, as-of, "m")
All amounts are ORIGINAL approval amounts (GrossApproval, SBAGuaranteedApproval) or gross charge-offs.
"""
from openpyxl.utils import column_index_from_string as CI, get_column_letter as L

import config as C
from source_data import parse_date

USD, PCT, NUM, INT, DATE = "#,##0.00", "0.0000%", "0.0000", "#,##0", "yyyy-mm-dd"
PS = C.SH_SUMMARY


def _f(r, field):
    v = r[field].strip()
    return float(v) if v else 0.0


def _months(d0, d1):
    """Excel DATEDIF(d0, d1, "m"): complete months."""
    n = (d1.year - d0.year) * 12 + d1.month - d0.month
    return n - 1 if d1.day < d0.day else n


class Group:
    """Template column definitions K..W for one set of loans."""

    def __init__(self, rows, status_map, as_of):
        self.rows = rows
        ax = lambda r: status_map[r[C.F_LOAN_STATUS].strip()]
        self.orig = [r for r in rows if ax(r) != "COMMIT"]
        self.active = [r for r in rows if ax(r) == "EXEMPT"]
        self.chgoff = [r for r in rows if ax(r) == "CHGOFF"]
        self.K, self.L, self.R = len(self.orig), len(self.active), len(self.chgoff)
        self.M = sum(_f(r, "GrossApproval") for r in self.orig)
        self.P = sum(_f(r, "GrossApproval") for r in self.active)
        self.age = sum(_months(parse_date(r[C.F_FIRST_DISBURSEMENT]), as_of) for r in self.active)
        self.co_orig = sum(_f(r, "GrossApproval") for r in self.chgoff)
        self.co_amt = sum(_f(r, "GrossChargeOffAmount") for r in self.chgoff)
        self.raw = lambda status: sum(_f(r, "GrossApproval") for r in rows if r[C.F_LOAN_STATUS].strip() == status)


def build_checks(rows, as_of, ps_info, status_map):
    unknown = sorted({r[C.F_LOAN_STATUS].strip() for r in rows} - set(status_map))
    if unknown:
        raise RuntimeError(f"LoanStatus values {unknown} are not in the template's Lookup table (FOIA!AX would be #N/A).")
    checks = []
    add = lambda sec, label, ref, fields, exp, fmt: checks.append((sec, label, ref, fields, exp, fmt))
    allg = Group(rows, status_map, as_of)

    # --- population and identity
    sec = "Population"
    add(sec, "Eligible loans written to the FOIA tab", "=COUNTA(FOIA!$AS:$AS)-1", "all rows (FOIA!AS = BankName & BankState)",
        len(rows), INT)
    add(sec, "FOIA rows belonging to the selected bank", f"=COUNTIF(FOIA!$AS:$AS,{PS}!$K$2)", "BankName, BankState",
        len(rows), INT)
    add(sec, "Latest AsOfDate in the FOIA tab", "=MAX(FOIA!$A:$A)", "AsOfDate", as_of, DATE)
    add(sec, "Earliest AsOfDate in the FOIA tab", "=MIN(FOIA!$A:$A)", "AsOfDate", as_of, DATE)

    # --- PerformanceSummary row 6 (whole eligible population)
    sec, ref = "PerformanceSummary total (row 6)", lambda c: f"{PS}!{c}6"
    g = allg
    add(sec, "# of Loans Orig.", ref("K"), "LoanStatus (via Lookup)", g.K, INT)
    add(sec, "Est. # of Loans Outstanding (active, original-amount basis)", ref("L"), "LoanStatus = EXEMPT", g.L, INT)
    add(sec, "Orig. Bal. ($) - original GrossApproval", ref("M"), "GrossApproval", g.M, USD)
    add(sec, "Average Balance ($) - original", ref("O"), "GrossApproval", g.M / g.K if g.K else "", USD)
    add(sec, "Est. Orig. Bal. of loans Outstanding ($) - ORIGINAL amount, not a balance", ref("P"),
        "GrossApproval, LoanStatus = EXEMPT", g.P, USD)
    add(sec, "Avg. Loan Age (mths)", ref("Q"), "FirstDisbursementDate, AsOfDate", g.age / g.L if g.L else 0, NUM)
    add(sec, "# of Loans Defaulted", ref("R"), "LoanStatus = CHGOFF", g.R, INT)
    add(sec, "% Cum. Defaulted", ref("S"), "GrossApproval of CHGOFF loans / Orig. Bal.", g.co_orig / g.M if g.M else "", PCT)
    add(sec, "% Cum. Loss", ref("U"), "GrossChargeOffAmount / Orig. Bal.", g.co_amt / g.M if g.M else "", PCT)
    add(sec, "% Loss Severity", ref("T"), "GrossChargeOffAmount / GrossApproval of CHGOFF loans",
        g.co_amt / g.co_orig if g.co_amt else "", PCT)
    add(sec, "LIQUID (%)", ref("V"), "LoanStatus = LIQUID", g.raw("LIQUID") / g.M if g.M else "", PCT)
    add(sec, "PURCH(NOT C/O) (%)", ref("W"), "LoanStatus = PURCH(NOT C/O)", g.raw("PURCH(NOT C/O)") / g.M if g.M else "", PCT)

    # --- subprogram rows
    for name, r in ps_info["subprogram_row_map"].items():
        g = Group([x for x in rows if x[C.F_SUBPROGRAM] == name], status_map, as_of)
        _row_checks(add, f"Subprogram: {name}", r, g, "Subprogram")

    # --- vintage rows (year of FirstDisbursementDate)
    for year, r in ps_info["year_rows"].items():
        g = Group([x for x in rows if parse_date(x[C.F_FIRST_DISBURSEMENT]).year == year], status_map, as_of)
        _row_checks(add, f"Vintage {year}", r, g, "FirstDisbursementDate (year)", short=True)
    r = ps_info["vintage_total_row"]
    g = Group([x for x in rows if 2019 <= parse_date(x[C.F_FIRST_DISBURSEMENT]).year <= 2026], status_map, as_of)
    _row_checks(add, "Vintage 2019-2026 totals (template's fixed window)", r, g, "FirstDisbursementDate (year)", short=True)

    # --- sector rows
    for name, r in ps_info["sector_rows"].items():
        g = Group([x for x in rows if x[C.F_SECTOR] == name], status_map, as_of)
        _row_checks(add, f"Sector: {name}", r, g, "NaicsDescription", short=True)

    # --- supporting reconciliation of original amounts (not part of the FinWise layout)
    sec = "Original amounts (supporting; NOT balances)"
    gross = sum(_f(x, "GrossApproval") for x in rows)
    guar = sum(_f(x, "SBAGuaranteedApproval") for x in rows)
    a_gross = sum(_f(x, "GrossApproval") for x in allg.active)
    a_guar = sum(_f(x, "SBAGuaranteedApproval") for x in allg.active)
    add(sec, "Eligible loans: original gross approval ($)", "=SUM(FOIA!$O:$O)", "GrossApproval", gross, USD)
    add(sec, "Eligible loans: original SBA-guaranteed approval ($)", "=SUM(FOIA!$P:$P)", "SBAGuaranteedApproval", guar, USD)
    add(sec, "Eligible loans: original unguaranteed approval ($) = gross - guaranteed", "=SUM(FOIA!$O:$O)-SUM(FOIA!$P:$P)",
        "GrossApproval, SBAGuaranteedApproval", gross - guar, USD)
    add(sec, "Active (EXEMPT) loans: original gross approval ($)", '=SUMIFS(FOIA!$O:$O,FOIA!$AX:$AX,"EXEMPT")',
        "GrossApproval, LoanStatus", a_gross, USD)
    add(sec, "Active (EXEMPT) loans: original SBA-guaranteed approval ($)", '=SUMIFS(FOIA!$P:$P,FOIA!$AX:$AX,"EXEMPT")',
        "SBAGuaranteedApproval, LoanStatus", a_guar, USD)
    add(sec, "Active (EXEMPT) loans: original unguaranteed approval ($) - an upper bound, not a retained balance",
        '=SUMIFS(FOIA!$O:$O,FOIA!$AX:$AX,"EXEMPT")-SUMIFS(FOIA!$P:$P,FOIA!$AX:$AX,"EXEMPT")',
        "GrossApproval, SBAGuaranteedApproval, LoanStatus", a_gross - a_guar, USD)
    add(sec, "Charged-off loans: gross charge-off amount ($, both portions)", '=SUMIFS(FOIA!$AL:$AL,FOIA!$AX:$AX,"CHGOFF")',
        "GrossChargeOffAmount, LoanStatus", allg.co_amt, USD)
    return checks


def _row_checks(add, sec, r, g, fields, short=False):
    ref = lambda c: f"{PS}!{c}{r}"
    add(sec, "# of Loans Orig.", ref("K"), f"{fields}, LoanStatus", g.K, INT)
    add(sec, "Est. # of Loans Outstanding", ref("L"), f"{fields}, LoanStatus = EXEMPT", g.L, INT)
    add(sec, "Orig. Bal. ($)", ref("M"), f"{fields}, GrossApproval", g.M, USD)
    add(sec, "Est. Orig. Bal. of loans Outstanding ($, original)", ref("P"), f"{fields}, GrossApproval, EXEMPT", g.P, USD)
    add(sec, "# of Loans Defaulted", ref("R"), f"{fields}, LoanStatus = CHGOFF", g.R, INT)
    if not short and g.M:       # the template divides by Orig. Bal. without a guard
        add(sec, "% Cum. Loss", ref("U"), f"{fields}, GrossChargeOffAmount", g.co_amt / g.M, PCT)


def benchmark_checks(ps_info, tabs, totals):
    """Industry-matched SBA benchmark (PRELIMINARY): PerformanceSummary benchmark totals against sums of the hidden
    Combined_* tables, and the Combined_Sector table against the whole benchmark population. Checks that the
    template formulas are wired to the right rows; the independent recomputation from the FOIA CSVs is in
    acceptance/benchmark/reproduce_benchmark.py."""
    from benchmark import GROUPS
    checks = []
    add = lambda sec, label, ref, fields, exp, fmt: checks.append((sec, label, ref, fields, exp, fmt))
    r20 = ps_info["benchmark_rows"]["vintage_header"]
    sec = "Benchmark population (PRELIMINARY)"
    add(sec, "SBA loans in benchmark population = row 5", f"{PS}!X5", "FY2000+ loans, not CANCLD, disbursed", totals[0], INT)
    add(sec, "Original GrossApproval of benchmark population = row 5", f"{PS}!Y5", "GrossApproval", totals[1], USD)
    add(sec, "Combined_Sector: loans over all industries", "=SUM(Combined_Sector!H4:H100000)", "NaicsDescription",
        totals[0], INT)
    add(sec, "Combined_Sector: original GrossApproval over all industries", "=SUM(Combined_Sector!N4:N100000)",
        "GrossApproval", totals[1], USD)
    for (sfx, _), base in zip(GROUPS, ("X", "AJ", "AV", "BH", "BT")):
        c = lambda i: L(CI(base) + i)
        group = sfx.strip("_") or "All SBA 7(a)"
        for row, label, src in ((6, "row 6 (lender's industries, all vintages)",
                                 [tabs[sfx]["sector"][s] for s in ps_info["sector_rows"]]),
                                (r20, f"row {r20} (vintages 1999-2026, lender's industries)",
                                 [tabs[sfx]["vintage"].get(y, [0] * 10) for y in range(1999, 2027)])):
            s = [sum(v[i] for v in src) for i in range(10)]
            sec = f"Benchmark {group}: {label}"
            add(sec, "# of Loans Orig.", f"{PS}!{c(0)}{row}", "Combined_*!H", s[0], INT)
            add(sec, "Orig. Bal. ($) - original GrossApproval", f"{PS}!{c(1)}{row}", "Combined_*!N", s[3], USD)
            add(sec, "Est. # of Loans Outstanding (no PIF / charge-off date)", f"{PS}!{c(4)}{row}", "Combined_*!I", s[1], INT)
            add(sec, "Est. Orig. Bal. of loans Outstanding - ORIGINAL amount", f"{PS}!{c(5)}{row}", "Combined_*!O", s[4], USD)
            add(sec, "# of Loans Defaulted (ChargeOffDate present)", f"{PS}!{c(6)}{row}", "Combined_*!K", s[2], INT)
            add(sec, "% Cum. Defaulted", f"{PS}!{c(7)}{row}", "Combined_*!Q / N", s[5] / s[3] if s[3] else "n/a", PCT)
    return checks
