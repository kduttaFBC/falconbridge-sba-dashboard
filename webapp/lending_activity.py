"""Lending activity, unguaranteed exposure, concentrations and loan characteristics (Phase 1 dashboard expansion).

Population: the engine's eligible rows for the selected lender, exactly the rows behind PerformanceSummary K:W
(LoanStatus present and not CANCLD, FirstDisbursementDate present; all approval years). Status groups come from the
template's Lookup table (status_map), so "default status" = group CHGOFF (LoanStatus CHGOFF or CLSLN), as in K:W.

Amounts are ORIGINAL approval amounts: gross = GrossApproval; guaranteed = SBAGuaranteedApproval; original
unguaranteed = GrossApproval - SBAGuaranteedApproval (the definition in FalconBridge's Origination workbook and the
generator's Threshold_Analysis sheet). None is a current or outstanding balance; SBA FOIA data has no balances.

Three different things are kept apart:
  * originations: loans by the year / quarter of FirstDisbursementDate (the vintage basis of both workbooks);
  * cohort status: of the loans originated in a period, those now in default status and their original amounts
    (the existing % Cum. Defaulted / % Cum. Loss definitions applied to the period's loans);
  * charge-off events: loans in default status by the year / quarter of their ChargeOffDate, with their original
    amounts and the gross charge-off cash amount (GrossChargeOffAmount). Default-status loans without a ChargeOffDate
    are reported as undated; charge-off dates on loans not in default status are counted and excluded.
Deliberately NOT computed (unconfirmed business rules): proportional "unguaranteed charge-off" amounts, annual loss /
prepayment / severity rates, loss severity by charge-off period, term-based "collateral type".
"""
import hashlib
from collections import OrderedDict, defaultdict
from datetime import date

from . import generator_bridge as GB

C, TA = GB.GC, GB.TA
parse_date = GB.S.parse_date
ACTIVITY_VERSION = "activity-1.0"
SIZE_BANDS = ((0, 150_000, "$150K or less"), (150_000, 350_000, "$150K to $350K"), (350_000, 1_000_000,
              "$350K to $1M"), (1_000_000, 2_000_000, "$1M to $2M"), (2_000_000, None, "Over $2M"))
CHARACTERISTICS = (("processing", "ProcessingMethod", "Processing method"),
                   ("business_age", "BusinessAge", "Business age (as reported to SBA)"),
                   ("rate_type", "FixedOrVariableInterestInd", "Interest rate type"),
                   ("size_band", None, "Original loan size (application bands)"))
RATE_TYPE = {"F": "Fixed", "V": "Variable"}


def _f(r, field):
    v = (r.get(field) or "").strip()
    return float(v) if v else 0.0


def size_band(gross):
    """Application bands (not a FalconBridge rule) on whole-dollar GrossApproval, as the threshold groups round."""
    g = round(gross)
    for _, hi, label in SIZE_BANDS:
        if hi is None or g <= hi:
            return label
    return SIZE_BANDS[-1][2]


def quarter_of(d):
    return d.year, (d.month - 1) // 3 + 1


def loan_ref(r):
    """Stable reference for a FOIA row (the public 7(a) file has no loan number): a hash of the row's own public
    fields, identical for the same row in the same release."""
    key = "|".join(r.get(k, "") for k in ("AsOfDate", "ApprovalDate", "FirstDisbursementDate", "GrossApproval",
                                         "SBAGuaranteedApproval", "NaicsCode", "BorrZip", "BorrCity", "TermInMonths",
                                         "LoanStatus", "LocationID", "BorrName", "BorrStreet"))
    return "L-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]


class _Acc:
    """Sums for one bucket (one period, industry, state or characteristic)."""
    __slots__ = ("loans", "gross", "guar", "def_loans", "def_gross", "def_unguar", "chg_amt")

    def __init__(self):
        self.loans = self.def_loans = 0
        self.gross = self.guar = self.def_gross = self.def_unguar = self.chg_amt = 0.0

    def add(self, r, defaulted):
        g, gu = _f(r, "GrossApproval"), _f(r, "SBAGuaranteedApproval")
        self.loans += 1
        self.gross += g
        self.guar += gu
        if defaulted:
            self.def_loans += 1
            self.def_gross += g
            self.def_unguar += g - gu
            self.chg_amt += _f(r, "GrossChargeOffAmount")

    def out(self):
        unguar = self.gross - self.guar
        ratio = lambda a, b: a / b if b else None          # noqa: E731
        return {"loans": self.loans, "gross": self.gross, "guar": self.guar, "unguar": unguar,
                "unguar_share": ratio(unguar, self.gross), "avg": ratio(self.gross, self.loans),
                "default_loans": self.def_loans, "default_gross": self.def_gross, "default_unguar": self.def_unguar,
                "gross_chargeoff": self.chg_amt, "cum_def": ratio(self.def_gross, self.gross),
                "cum_loss": ratio(self.chg_amt, self.gross)}


def _change(cur, prev):
    return (cur - prev) / prev if prev else None


def _series(buckets, keys):
    out = []
    prev = None
    for k in keys:
        m = buckets[k].out() if k in buckets else _Acc().out()
        m["period"] = k
        m["gross_change"] = _change(m["gross"], prev["gross"]) if prev else None
        m["unguar_change"] = _change(m["unguar"], prev["unguar"]) if prev else None
        m["loans_change"] = _change(m["loans"], prev["loans"]) if prev else None
        out.append(m)
        prev = m
    return out


def compute(rows, status_map, as_of):
    """Rows: the engine's eligible rows of one lender (all approval years). Returns the payload section."""
    is_def = lambda r: status_map[r[C.F_LOAN_STATUS].strip()] == "CHGOFF"     # noqa: E731
    ann, qtr = defaultdict(_Acc), defaultdict(_Acc)
    ev_ann = defaultdict(lambda: {"loans": 0, "gross": 0.0, "unguar": 0.0, "gross_chargeoff": 0.0})
    ev_qtr = defaultdict(lambda: {"loans": 0, "gross": 0.0, "unguar": 0.0, "gross_chargeoff": 0.0})
    undated = {"loans": 0, "gross": 0.0, "gross_chargeoff": 0.0}
    dated_not_default = 0
    states = {"project": defaultdict(_Acc), "borrower": defaultdict(_Acc)}
    chars = {k: defaultdict(_Acc) for k, _, _ in CHARACTERISTICS}
    ind_year = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0.0]))         # industry -> year -> [n, gross, ung]
    total = _Acc()
    for r in rows:
        d = parse_date(r[C.F_FIRST_DISBURSEMENT])
        dflt = is_def(r)
        y, q = quarter_of(d)
        for acc in (ann[y], qtr[(y, q)], total, states["project"][(r.get("ProjectState") or "").strip() or "Not reported"],
                    states["borrower"][(r.get("BorrState") or "").strip() or "Not reported"]):
            acc.add(r, dflt)
        for key, field, _ in CHARACTERISTICS:
            if field is None:
                label = size_band(_f(r, "GrossApproval"))
            else:
                raw = (r.get(field) or "").strip()
                label = RATE_TYPE.get(raw, raw) if key == "rate_type" else raw
            chars[key][label or "Not reported"].add(r, dflt)
        sector = r.get(C.F_SECTOR) or C.NA_SECTOR
        cell = ind_year[sector][y]
        g, gu = _f(r, "GrossApproval"), _f(r, "SBAGuaranteedApproval")
        cell[0] += 1
        cell[1] += g
        cell[2] += g - gu
        co = (r.get("ChargeOffDate") or "").strip()
        if dflt and co:
            cd = parse_date(co)
            for book, k in ((ev_ann, cd.year), (ev_qtr, quarter_of(cd))):
                e = book[k]
                e["loans"] += 1
                e["gross"] += g
                e["unguar"] += g - gu
                e["gross_chargeoff"] += _f(r, "GrossChargeOffAmount")
        elif dflt:
            undated["loans"] += 1
            undated["gross"] += g
            undated["gross_chargeoff"] += _f(r, "GrossChargeOffAmount")
        elif co:
            dated_not_default += 1
    if not rows:
        return {"version": ACTIVITY_VERSION, "empty": True}
    first = min(ann)
    last_y, last_q = quarter_of(as_of)
    years = list(range(first, max(max(ann), last_y) + 1))
    fq = min(qtr)
    quarters = []
    y, q = fq
    while (y, q) <= (last_y, last_q) or (y, q) <= max(qtr):
        quarters.append((y, q))
        y, q = (y, q + 1) if q < 4 else (y + 1, 1)
    q_end = date(last_y, 3 * last_q, 1)
    partial_quarter = not (as_of.month == 3 * last_q and as_of == _month_end(q_end))
    annual = _series(ann, years)
    quarterly = _series(qtr, quarters)
    year_partial = as_of.month != 12 or as_of.day != 31
    for m in annual:
        m["partial"] = year_partial and m["period"] == as_of.year
        if m["partial"]:                     # a part-year is not comparable with a full year: no growth figure
            m["gross_change"] = m["unguar_change"] = m["loans_change"] = None
    if partial_quarter_flag(as_of) and quarterly:
        quarterly[-1]["partial"] = True
    for m in quarterly:
        m["label"] = f"Q{m['period'][1]} {m['period'][0]}"
        m["period"] = f"{m['period'][0]}Q{m['period'][1]}"
    ev_years = sorted(ev_ann)
    events = {"annual": [dict(period=k, **ev_ann[k]) for k in range(min(ev_years), max(ev_years) + 1)]
              if ev_years else [],
              "quarterly": [dict(period=f"{k[0]}Q{k[1]}", label=f"Q{k[1]} {k[0]}", **ev_qtr[k])
                            for k in _quarter_range(min(ev_qtr), max(ev_qtr))] if ev_qtr else [],
              "undated": undated, "dated_not_in_default_status": dated_not_default}
    for book in ("annual", "quarterly"):
        for e in events[book]:
            for k in ("loans", "gross", "unguar", "gross_chargeoff"):
                e.setdefault(k, 0)
    tot = total.out()

    def window(ms):
        if not ms:
            return None
        g, u = sum(m["gross"] for m in ms), sum(m["unguar"] for m in ms)
        return {"from": ms[0]["label"], "to": ms[-1]["label"], "quarters": len(ms), "loans": sum(m["loans"] for m in ms),
                "gross": g, "unguar": u, "unguar_share": u / g if g else None}
    last4, prior4 = window(quarterly[-4:]), window(quarterly[-8:-4] if len(quarterly) >= 8 else [])
    recent = {"last4": last4, "prior4": prior4,
              "gross_change": _change(last4["gross"], prior4["gross"]) if last4 and prior4 else None,
              "unguar_change": _change(last4["unguar"], prior4["unguar"]) if last4 and prior4 else None,
              "basis": "the four most recent quarters to the release date, against the four before them"}
    industries = sorted(ind_year, key=lambda s: -sum(v[1] for v in ind_year[s].values()))
    industry_year = {"years": years, "industries": [
        {"name": s, "cells": {str(yy): {"loans": v[0], "gross": v[1], "unguar": v[2]} for yy, v in ind_year[s].items()},
         "loans": sum(v[0] for v in ind_year[s].values()), "gross": sum(v[1] for v in ind_year[s].values()),
         "unguar": sum(v[2] for v in ind_year[s].values()),
         "unguar_share": (sum(v[2] for v in ind_year[s].values()) / sum(v[1] for v in ind_year[s].values())
                          if sum(v[1] for v in ind_year[s].values()) else None)} for s in industries]}
    conc = []
    for yy in years:
        gs = sorted((v[yy][1] for v in ind_year.values() if yy in v), reverse=True)
        tg = sum(gs)
        conc.append({"year": yy, "industries": len(gs), "top1": gs[0] / tg if tg else None,
                     "top5": sum(gs[:5]) / tg if tg else None, "gross": tg})
    geo = {k: sorted(({"state": s, **a.out(), "share": a.gross / total.gross if total.gross else None}
                      for s, a in book.items()), key=lambda x: -x["gross"]) for k, book in states.items()}
    characteristics = {}
    for key, field, label in CHARACTERISTICS:
        order = [b[2] for b in SIZE_BANDS] if key == "size_band" else None
        items = [{"value": v, **a.out(), "share": a.gross / total.gross if total.gross else None}
                 for v, a in chars[key].items()]
        items.sort(key=(lambda x: order.index(x["value"]) if x["value"] in order else 99) if order else
                   (lambda x: -x["gross"]))
        characteristics[key] = {"label": label, "field": field or "GrossApproval (banded)", "items": items}
    return {"version": ACTIVITY_VERSION, "as_of": as_of.isoformat(), "partial_quarter": partial_quarter,
            "partial_year": as_of.month != 12 or as_of.day != 31,
            "totals": tot, "recent": recent, "annual": annual, "quarterly": quarterly, "chargeoff_events": events,
            "geography": geo, "characteristics": characteristics, "industry_year": industry_year,
            "concentration_by_year": conc, "size_bands": [b[2] for b in SIZE_BANDS]}


def partial_quarter_flag(as_of):
    y, q = quarter_of(as_of)
    return as_of != _month_end(date(y, 3 * q, 1))


def _month_end(d):
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def _quarter_range(a, b):
    out, (y, q) = [], a
    while (y, q) <= b:
        out.append((y, q))
        y, q = (y, q + 1) if q < 4 else (y + 1, 1)
    return out


# ------------------------------------------------------------------------------------------------ loan drill-down
DRILL_FIELDS = (("ref", None, "Loan reference"), ("ApprovalDate", "ApprovalDate", "Approval date"),
                ("ApprovalFiscalYear", "ApprovalFiscalYear", "Approval FY"),
                ("FirstDisbursementDate", "FirstDisbursementDate", "First disbursement"),
                ("GrossApproval", "GrossApproval", "Gross approval ($)"),
                ("SBAGuaranteedApproval", "SBAGuaranteedApproval", "SBA guaranteed ($)"),
                ("unguar", None, "Original unguaranteed ($)"), ("TermInMonths", "TermInMonths", "Term (months)"),
                ("NaicsCode", "NaicsCode", "NAICS"), ("NaicsDescription", "NaicsDescription", "Industry"),
                ("Subprogram", "Subprogram", "Subprogram"), ("ProcessingMethod", "ProcessingMethod", "Processing"),
                ("LoanStatus", "LoanStatus", "Loan status"), ("PaidInFullDate", "PaidInFullDate", "Paid in full"),
                ("ChargeOffDate", "ChargeOffDate", "Charge-off date"),
                ("GrossChargeOffAmount", "GrossChargeOffAmount", "Gross charge-off ($)"),
                ("BorrCity", "BorrCity", "Borrower city"), ("BorrState", "BorrState", "Borrower state"),
                ("BorrZip", "BorrZip", "Borrower ZIP"), ("ProjectState", "ProjectState", "Project state"),
                ("BusinessAge", "BusinessAge", "Business age"), ("JobsSupported", "JobsSupported", "Jobs supported"))
# Never shown: BorrName, BorrStreet (hidden by decision of 2026-11-06), and anything not in the public FOIA row.
HIDDEN_FIELDS = ("BorrName", "BorrStreet")
DIMENSIONS = {"year", "quarter", "industry", "project_state", "borrower_state", "processing", "business_age",
              "rate_type", "size_band", "chargeoff_year", "chargeoff_quarter", "vintage", "industry_year"}


def select(rows, status_map, dim, value):
    """The loans behind one aggregate cell. Same population and definitions as compute(); unknown dimensions raise."""
    if dim not in DIMENSIONS:
        raise ValueError(f"unknown drill-down dimension {dim!r}")
    is_def = lambda r: status_map[r[C.F_LOAN_STATUS].strip()] == "CHGOFF"     # noqa: E731
    out = []
    for r in rows:
        d = parse_date(r[C.F_FIRST_DISBURSEMENT])
        if dim in ("year", "vintage"):
            ok = str(d.year) == value
        elif dim == "quarter":
            ok = f"{d.year}Q{quarter_of(d)[1]}" == value
        elif dim == "industry":
            ok = (r.get(C.F_SECTOR) or C.NA_SECTOR) == value
        elif dim == "industry_year":
            name, _, yy = value.rpartition("|")
            ok = (r.get(C.F_SECTOR) or C.NA_SECTOR) == name and str(d.year) == yy
        elif dim == "project_state":
            ok = ((r.get("ProjectState") or "").strip() or "Not reported") == value
        elif dim == "borrower_state":
            ok = ((r.get("BorrState") or "").strip() or "Not reported") == value
        elif dim == "size_band":
            ok = size_band(_f(r, "GrossApproval")) == value
        elif dim in ("chargeoff_year", "chargeoff_quarter"):
            co = (r.get("ChargeOffDate") or "").strip()
            if not (is_def(r) and co):
                ok = False
            else:
                cd = parse_date(co)
                ok = (str(cd.year) if dim == "chargeoff_year" else f"{cd.year}Q{quarter_of(cd)[1]}") == value
        else:
            field = {"processing": "ProcessingMethod", "business_age": "BusinessAge",
                     "rate_type": "FixedOrVariableInterestInd"}[dim]
            raw = (r.get(field) or "").strip()
            label = RATE_TYPE.get(raw, raw) if dim == "rate_type" else raw
            ok = (label or "Not reported") == value
        if ok:
            out.append(r)
    return out


def loan_rows(rows, status_map):
    """Public, allow-listed view of selected loans (borrower name and street never included), with totals."""
    is_def = lambda r: status_map[r[C.F_LOAN_STATUS].strip()] == "CHGOFF"     # noqa: E731
    items, tot = [], {"loans": 0, "gross": 0.0, "guar": 0.0, "unguar": 0.0, "default_loans": 0,
                      "gross_chargeoff": 0.0}
    for r in sorted(rows, key=lambda r: (parse_date(r[C.F_FIRST_DISBURSEMENT]), -_f(r, "GrossApproval"))):
        g, gu = _f(r, "GrossApproval"), _f(r, "SBAGuaranteedApproval")
        item = OrderedDict()
        for key, field, _ in DRILL_FIELDS:
            if key == "ref":
                item[key] = loan_ref(r)
            elif key == "unguar":
                item[key] = g - gu
            else:
                item[key] = (r.get(field) or "").strip()
        item["default_status"] = is_def(r)
        items.append(item)
        tot["loans"] += 1
        tot["gross"] += g
        tot["guar"] += gu
        tot["unguar"] += g - gu
        tot["default_loans"] += is_def(r)
        tot["gross_chargeoff"] += _f(r, "GrossChargeOffAmount") if is_def(r) else 0.0
    return {"columns": [(k, label) for k, _, label in DRILL_FIELDS], "loans": items, "totals": tot}


POPULATION = ("eligible SBA 7(a) loans of this lender in the SBA FOIA release (LoanStatus present and not CANCLD, first "
              "disbursed), all approval years")
DIM_TEXT = {"year": "first disbursed in {v}", "vintage": "first disbursed in {v}", "quarter": "first disbursed in {v}",
            "industry": "industry {v}", "industry_year": "industry {n}, first disbursed in {y}",
            "project_state": "project state {v}", "borrower_state": "borrower state {v}",
            "processing": "processing method {v}", "business_age": "business age {v}", "rate_type": "rate type {v}",
            "size_band": "original loan size {v}", "chargeoff_year": "in default status with a charge-off date in {v}",
            "chargeoff_quarter": "in default status with a charge-off date in {v}"}


def describe(dim, value):
    name, _, yy = value.rpartition("|")
    return DIM_TEXT[dim].format(v=value, n=name, y=yy)


def aggregate(activity, dim, value):
    """The figures the dashboard displays for one drill-down cell (None when the activity section has no such cell)."""
    if not activity or activity.get("empty"):
        return None
    if dim in ("year", "vintage"):
        return next((m for m in activity["annual"] if str(m["period"]) == value), None)
    if dim == "quarter":
        return next((m for m in activity["quarterly"] if m["period"] == value), None)
    if dim in ("project_state", "borrower_state"):
        book = activity["geography"]["project" if dim == "project_state" else "borrower"]
        return next((m for m in book if m["state"] == value), None)
    if dim in ("processing", "business_age", "rate_type", "size_band"):
        return next((m for m in activity["characteristics"][dim]["items"] if m["value"] == value), None)
    if dim in ("chargeoff_year", "chargeoff_quarter"):
        book = activity["chargeoff_events"]["annual" if dim == "chargeoff_year" else "quarterly"]
        return next((m for m in book if str(m["period"]) == value), None)
    if dim == "industry":
        return next((m for m in activity["industry_year"]["industries"] if m["name"] == value), None)
    if dim == "industry_year":
        name, _, yy = value.rpartition("|")
        row = next((m for m in activity["industry_year"]["industries"] if m["name"] == name), None)
        return row["cells"].get(yy) if row else None
    return None


def reconcile(totals, agg):
    """Which displayed measures the selected loans reproduce exactly ({} when there is nothing to compare)."""
    if not agg:
        return {}
    pairs = (("loans", "loans"), ("gross", "gross"), ("unguar", "unguar"), ("default_loans", "default_loans"),
             ("gross_chargeoff", "gross_chargeoff"))
    return {k: abs((totals.get(k) or 0) - (agg.get(a) or 0)) <= 0.005 for k, a in pairs if a in agg}
