"""Static-pool view of the bank's loans: the workbook's ChargeOffsByVintage sheet, cell for cell.

The template computes this sheet with formulas only (the generator writes no values into it), so its definitions are
transcribed here exactly; every figure is reconciled with the recalculated sheet by
acceptance/threshold/dashboard_consistency.py and tests/test_webapp.py. FOIA helper columns in brackets:
  rows      vintage D = 1991..2026; bank [AS] = the selected lender; Sector_Pick [AR] = "Yes" (the loan's NAICS
            description is one of the workbook's industry rows, all flagged "Yes" by the generator)
  E6:E41    Orig ($mm)        = SUMIFS(GrossApproval [O], vintage [AO] = YEAR(FirstDisbursementDate) = D) / 1e6
  G6:AP41   PIF $mm           = same, and PIF year [AP] = YEAR(PaidInFullDate) = column year (1991..2026)
  AR6:CA41  PIF %             = G/E
  F6:F41    Cumulative PIF %  = IFERROR(SUM(G:AP)/E, "na")
  G49:AP84  Charge-off $mm    = SUMIFS(GrossChargeOffAmount [AL], vintage = D, charge-off year [AQ] = column year) / 1e6
  AR49:CA84 Charge-off %      = IF(year < D, "", IFERROR(G/E, "na"))
  F49:F84   Cumulative charge-off % = IFERROR(SUM(G:AP)/E, "na");  A49:A84 = SUM(G:AP)
Vintages with no original amount (E = 0; #DIV/0! or "na" in the workbook) are omitted.

Display-only arrangement (approved 4 Nov 2026): a curve point k (years since first disbursement) is the running total
of the workbook's own annual % cells up to calendar year D + k, so the last point equals column F. Curves stop at the
last observed year (the data's as-of year); later years are not observed and are not shown as zero activity.
"""
from . import generator_bridge as GB

C, S = GB.GC, GB.S
FIRST, LAST = C.TEMPLATE_HIDDEN_TAB_FIRST_YEAR, C.TEMPLATE_HIDDEN_TAB_LAST_YEAR
# Data limit of the SBA FOIA release (checked by tests/test_webapp.py over the 2026-06-30 files): no PaidInFullDate is
# earlier than 2005, and 2005 holds about six times a normal year's count, so loans paid in full before 2005 carry a
# 2005 date. Prepayment timing for vintages disbursed before 2005 is therefore shown with a caveat; figures unchanged.
PIF_DATES_FROM = 2005
# FalconBridge's template has no formula in ChargeOffsByVintage!A83 (the 2025 vintage's total charge-offs; every other
# row is =SUM(G:AP)). The dashboard shows that row's total by the same annual sum and says so; the template is unchanged.
TOTAL_FORMULA_MISSING = {2025: "A83"}


def _f(r, field):
    v = r[field].strip()
    return float(v) if v else 0.0


def _year(text):
    return S.parse_date(text).year if text and text.strip() else None


def compute(rows, sector_labels, as_of, vintage_loans):
    """rows: the lender's eligible loan rows (the FOIA sheet); sector_labels: the workbook's industry rows;
    vintage_loans: {year: # of loans} from the PerformanceSummary vintage rows (context only)."""
    years = list(range(FIRST, LAST + 1))
    picks = set(sector_labels)
    orig = {v: 0.0 for v in years}
    pif = {v: {y: 0.0 for y in years} for v in years}
    co = {v: {y: 0.0 for y in years} for v in years}
    excluded = 0
    for r in rows:                                     # FOIA sheet order, as SUMIFS adds
        v = _year(r[C.F_FIRST_DISBURSEMENT])
        if v not in orig:
            continue
        if r[C.F_SECTOR] not in picks:
            excluded += 1
            continue
        gross = _f(r, "GrossApproval")
        orig[v] += gross
        py, cy = _year(r["PaidInFullDate"]), _year(r["ChargeOffDate"])
        if py in pif[v]:
            pif[v][py] += gross
        if cy in co[v]:
            co[v][cy] += _f(r, "GrossChargeOffAmount")
    last_obs = min(as_of.year, LAST)
    out = []
    for v in years:
        E = orig[v] / 1000000
        if not E:
            continue
        g_pif = [pif[v][y] / 1000000 for y in years]
        g_co = [co[v][y] / 1000000 for y in years]
        pif_pct = [x / E for x in g_pif]
        co_pct = [None if y < v else x / E for y, x in zip(years, g_co)]
        curve_pif, curve_co, run_p, run_c = [], [], 0.0, 0.0
        for y, p, c in zip(years, pif_pct, co_pct):
            run_p += p
            run_c += c or 0.0
            if v <= y <= last_obs:
                curve_pif.append(run_p)
                curve_co.append(run_c)
        out.append({"year": v, "orig_mm": E, "loans": vintage_loans.get(v, 0),
                    "pif_mm": g_pif, "pif_pct": pif_pct, "pif_cum": sum(g_pif) / E,
                    "co_mm": g_co, "co_pct": co_pct, "co_cum": sum(g_co) / E, "co_total_mm": sum(g_co),
                    "curve_pif": curve_pif, "curve_co": curve_co})
    return {"years": years, "last_observed_year": last_obs, "vintages": out, "excluded_loans": excluded,
            "pif_dates_from": PIF_DATES_FROM,
            "total_formula_missing": {str(y): c for y, c in TOTAL_FORMULA_MISSING.items() if y in {v["year"] for v in out}}}
