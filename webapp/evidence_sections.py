"""Analyst Evidence, organized like the original dashboard: Performance Summary, Portfolio Age and Vintage, Industry
Exposure, Benchmark Analysis, Supplementary Analysis and Methodology.

Presentation only. Every table cell is ONE evidence record of the Saved Analysis's evidence pack (its value read
unchanged from the frozen dashboard payload by intel_adapter); nothing is calculated here. Number formats mirror the
dashboard's own (dashboard.html: money/pct/pp/int/months) so the figures read the same on both screens. Findings,
comparisons and limitations are the view model's M2-checked claims (presentation.build), regrouped by section.
"""
import html
import re
from decimal import ROUND_HALF_UP, Decimal

from intelligence.evidence import Kind, ValidationStatus as VS

from . import insights as IN
from . import presentation as PR

MINUS = "−"
PS_ROWS = (("loans", "# of Loans Orig.", "int"), ("orig", "Orig. Bal. ($)", "money"),
           ("avg", "Average Balance ($)", "money"), ("active", "Est. # of Loans Outstanding", "int"),
           ("active_orig", "Est. Orig. Bal. of loans Outstanding ($)", "money"),
           ("age", "Avg. Loan Age (mths)", "months"), ("chgoff", "# of Loans Defaulted", "int"),
           ("cum_def", "% Cum. Defaulted", "pct"), ("severity", "% Loss Severity †", "pct"),
           ("cum_loss", "% Cum. Loss †", "pct"), ("purch_pct", "PURCH(NOT C/O) (%)", "pct"))
CAVEAT_TEXT = {"PRELIMINARY_BENCHMARK": "preliminary benchmark methodology",
               "NOT_MIX_ADJUSTED": "not adjusted for portfolio age, loan size or industry mix",
               "SMALL_SAMPLE": "small sample", "PARTIAL_YEAR": "includes the partial reporting year",
               "UNVERIFIED": "includes unverified values"}
STATUS_PHRASE = {
    VS.INDEPENDENTLY_VERIFIED: "independently verified by an attached validation record",
    VS.RECONCILED_TO_TEMPLATE: "bank workbook measures reconcile to FalconBridge's workbook formulas",
    VS.PENDING_CLIENT_APPROVAL: "application-defined measures pending FalconBridge approval",
    VS.PRELIMINARY_METHODOLOGY: "SBA benchmark methodology preliminary, pending FalconBridge confirmation",
    VS.UNVERIFIED: "unverified values present",
}


# ------------------------------------------------------------------------------- dashboard-equivalent formats
def fixed(x, digits):
    """JavaScript Number.prototype.toFixed: the exact binary value rounded half away from zero (the dashboard's
    rounding). Python's own formatting rounds exact halves to even, which can differ in the last digit."""
    return str(Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def f_money(v):                                                      # dashboard.html money()
    a = abs(v)
    return ("$" + fixed(v / 1e9, 2) + "B" if a >= 1e9 else "$" + fixed(v / 1e6, 1) + "M" if a >= 1e6 else
            "$" + fixed(v / 1e3, 0) + "K" if a >= 1e3 else "$" + fixed(v, 0))


def f_pp(v):                                                         # dashboard.html pp()
    return ("+" if v >= 0 else MINUS) + fixed(abs(v * 100), 2) + " pp"


FMT = {"int": lambda v: f"{v:,.0f}", "money": f_money, "pct": lambda v: fixed(v * 100, 2) + "%",
       "months": lambda v: fixed(v, 1), "pp": f_pp, "text": str}


class _S:
    def __init__(self, model, pack):
        self.m, self.pack = model, pack
        self.by_key = {(r.metric_key, r.kind): r for r in pack.records}
        self.used = []                                    # records shown in the current section's tables

    def rec(self, key, kind=Kind.FACT):
        r = self.by_key.get((key, kind))
        return r if r is not None and (PR._num(r.value) or (r.unit == "text" and r.value)) else None

    def cell(self, key, fmt, kind=Kind.FACT, dash="–", extra=""):
        r = self.rec(key, kind)
        if r is None:
            return {"text": dash}
        self.used.append(r)
        return {"text": FMT[fmt](r.value) + extra, "ev": r.evidence_id, "status": PR.STATUS_TEXT[r.validation_status]}

    def status_line(self):
        found = {r.validation_status for r in self.used}
        out = [STATUS_PHRASE[s] for s in (VS.INDEPENDENTLY_VERIFIED, VS.RECONCILED_TO_TEMPLATE, VS.PENDING_CLIENT_APPROVAL,
                                           VS.PRELIMINARY_METHODOLOGY, VS.UNVERIFIED) if s in found]
        if VS.INDEPENDENTLY_VERIFIED not in found:
            out.append("no independent validation record attached")
        self.used = []
        return out


VINTAGE_HEAD = ["Vintage", "# of Loans Orig.", "Orig. Bal.", "Est. # Outstanding", "# Defaulted", "% Cum. Defaulted",
                "% Cum. Loss", "SBA # of Loans", "SBA % Cum. Defaulted", "SBA % Cum. Loss"]


def industry_name(n):                                               # dashboard.html ind()
    return "Not classified (no NAICS code)" if n == "N/A" else n


def _limits(item):
    return [CAVEAT_TEXT[c] for c in item.get("caveats", []) if c in CAVEAT_TEXT]


def _item(item, ref=None, title=None, anchor=None):
    out = dict(item)
    out["anchor"] = anchor or item.get("ref") or ""
    out["heading"] = title or item.get("title") or ""
    out["limits"] = _limits(item)
    out["proposed"] = item.get("type") == "HYPOTHESIS"
    return out


def _years(s, prefix):
    return sorted({int(m.group(1)) for (k, kind) in s.by_key if kind is Kind.FACT
                   for m in [re.fullmatch(rf"{re.escape(prefix)}\.vintage\.(\d{{4}})\.loans", k)] if m
                   and s.rec(k) is not None and s.rec(k).value})


def _industries(s, prefix):
    names = []
    for (k, kind), r in s.by_key.items():
        m = re.fullmatch(rf"{re.escape(prefix)}\.industry\.(.+)\.orig", k)
        if m and kind is Kind.FACT and PR._num(r.value) and (s.rec(f"{prefix}.industry.{m.group(1)}.loans") or
                                                            {"value": 0}).value:
            names.append((r.value, m.group(1)))
    return [n for _, n in sorted(names, key=lambda x: (-x[0], x[1]))]


def build_sections(model, pack):
    s = _S(model, pack)
    m = model
    t = m["threshold_text"]
    dash = f"/analyses/{m['analysis_id']}"
    pyr = s.rec("limitation.partial_year", Kind.DERIVED)
    py = pyr.value if pyr is not None else None
    yl = lambda y: f"{y}*" if py and int(y) == py else str(y)          # noqa: E731
    findings = {f.get("title"): f for f in m["findings"]}
    perf_f = [f for f in m["findings"] if f.get("title") != "Portfolio age"]
    age_f = [f for f in m["findings"] if f.get("title") == "Portfolio age"]
    sections = []

    # ------------------------------------------------------------------------------ 1. Performance Summary
    full_l, fy_l = s.rec("bank.full.total.total.loans"), s.rec("bank.fy2000.total.total.loans")
    same = full_l is not None and fy_l is not None and full_l.value == fy_l.value
    rows = []
    for k, label, fmt in PS_ROWS:
        d = s.rec(f"compare.total.total.{k}.diff", Kind.DERIVED) if k in ("cum_def", "severity", "cum_loss") else None
        diff = s.cell(f"compare.total.total.{k}.diff", "pp", Kind.DERIVED, dash="",
                      extra=(" below SBA" if d.value < 0 else " above SBA" if d.value > 0 else "")) if d else {"text": ""}
        rows.append({"cls": "hl" if k == "cum_def" else "", "cells": [
            {"text": label}, s.cell(f"bank.full.total.total.{k}", fmt),
            s.cell(f"benchmark.total.total.{k}", fmt) if k != "age" else {"text": "–"},
            s.cell(f"benchmark.above.total.{k}", fmt) if k != "age" else {"text": "–"}, diff]})
    perf = {"id": "performance", "title": "Performance Summary",
            "subtitle": "FalconBridge PerformanceSummary measures · amounts are original approval amounts",
            "tables": [{"head": ["Measure", "Bank<br><small>full history" + (" = FY2000 onward" if same else "")
                                 + "</small>", "All SBA 7(a)<br><small>industry-matched</small>",
                                 f"Standard<br><small>&gt; {t}</small>",
                                 "Bank vs All SBA<br><small>FY2000 onward basis</small>"], "rows": rows,
                        "note": "† " + IN.SEVERITY_NOTE}],
            "status": s.status_line(),
            "findings": [_item(f) for f in perf_f],
            "links": [("Executive Portfolio: Performance Summary", f"{dash}?tab=executive")]}
    sections.append(perf)

    # ------------------------------------------------------------------------------ 2. Portfolio Age and Vintage
    band_rows = []
    bands = sorted({k.split(".")[1] for (k, kind) in s.by_key if kind is Kind.DERIVED and k.startswith("seasoning.")},
                   key=lambda b: int(b.split("-")[0]))
    for i, b in enumerate(bands):
        lo, hi = b.split("-")
        label = f"Disbursed {hi} or earlier" if i == 0 else f"Disbursed {lo}–{yl(hi)}"
        band_rows.append({"cells": [{"text": label}, s.cell(f"seasoning.{b}.bank_share", "pct", Kind.DERIVED),
                                    s.cell(f"seasoning.{b}.sba_share", "pct", Kind.DERIVED)]})
    vrows, early = [], []
    for y in _years(s, "bank.full.total"):
        b, sb = f"bank.full.total.vintage.{y}", f"benchmark.total.vintage.{y}"
        n = s.rec(f"{b}.loans").value
        (vrows if y >= 1999 else early).append({"cells": [{"text": yl(y), "tag": "small sample" if n < IN.MIN_INDUSTRY_LOANS else ""},
                                s.cell(f"{b}.loans", "int"), s.cell(f"{b}.orig", "money"),
                                s.cell(f"{b}.active", "int", dash="0"), s.cell(f"{b}.chgoff", "int", dash="0"),
                                s.cell(f"{b}.cum_def", "pct"), s.cell(f"{b}.cum_loss", "pct"),
                                s.cell(f"{sb}.loans", "int"), s.cell(f"{sb}.cum_def", "pct"),
                                s.cell(f"{sb}.cum_loss", "pct")]})
    age = s.rec("bank.full.total.total.age")
    act = s.rec("bank.full.total.total.active")
    tables = []
    if band_rows:
        tables.append({"title": "Share of original gross amount by disbursement year",
                       "head": ["Disbursement year", "Bank", "SBA benchmark"], "rows": band_rows,
                       "note": (f"Average age of the bank's estimated outstanding loans: {age.value:.1f} months "
                                f"({act.value:,} of {full_l.value:,} loans estimated outstanding)."
                                if age is not None and act is not None and full_l is not None else "")})
    if vrows:
        tables.append({"id": "vintages", "title": "Vintage history (PerformanceSummary vintage rows)",
                       "head": VINTAGE_HEAD, "rows": vrows,
                       "note": "Bank: full history. SBA vintages cover SBA loans in this bank's industries "
                               "(FalconBridge's industry-matched method, PRELIMINARY)."
                               + (f" * {py} is a partial year: loans first disbursed through {m['as_of_text']}."
                                  if py else "")})
    age_more = [{"title": "Vintages before 1999 (in the full-history totals; FalconBridge's PerformanceSummary hides "
                          "these vintage rows)", "head": VINTAGE_HEAD, "rows": early}] if early else []
    sections.append({"id": "age", "title": "Portfolio Age and Vintage",
                     "subtitle": "vintage = year of first disbursement · rates are cumulative to the reporting date",
                     "tables": tables, "more": age_more, "status": s.status_line(),
                     "findings": [_item(f) for f in age_f] + [_item(p, anchor=f"proposed-{i}", title="Possible "
                                                                    "explanation")
                                                              for i, p in enumerate(m["proposals"], 1)],
                     "links": [("Executive Portfolio: Is the overall comparison like-for-like?",
                                f"{dash}?tab=executive"),
                               ("Portfolio Analysis: % Cum. Defaulted by vintage chart", f"{dash}?tab=analysis")]})

    # ------------------------------------------------------------------------------ 3. Industry Exposure
    def ind_row(n, prefix="bank.full.total"):
        b, sb = f"{prefix}.industry.{n}", f"benchmark.total.industry.{n}"
        loans = s.rec(f"{b}.loans").value
        return {"cells": [{"text": industry_name(n), "tag": "small sample" if loans < IN.MIN_INDUSTRY_LOANS else ""},
                          s.cell(f"{b}.naics", "text", dash=""), s.cell(f"{b}.share", "pct"),
                          s.cell(f"{b}.loans", "int"), s.cell(f"{b}.orig", "money"), s.cell(f"{b}.active", "int"),
                          s.cell(f"{b}.cum_def", "pct"), s.cell(f"{b}.cum_loss", "pct"), s.cell(f"{sb}.loans", "int"),
                          s.cell(f"{sb}.cum_def", "pct"), s.cell(f"{sb}.cum_loss", "pct")]}
    names = _industries(s, "bank.full.total")
    ihead = ["Industry (NAICS description)", "NAICS", "% of Total Orig.", "# of Loans", "Orig. Bal.",
             "Est. # Outstanding", "% Cum. Defaulted", "% Cum. Loss", "SBA # of Loans", "SBA % Cum. Defaulted",
             "SBA % Cum. Loss"]
    conc = [(k, lbl) for k, lbl in (("top1", "Largest industry"), ("top5", "Largest five"), ("top10", "Largest ten"))]
    crow = {"cells": [s.cell(f"concentration.total.{k}", "pct", Kind.DERIVED) for k, _ in conc]
            + [s.cell("concentration.total.industries", "int", Kind.DERIVED)]}
    ind_tables = [{"title": "Concentration (share of original gross amount)",
                   "head": [lbl for _, lbl in conc] + ["Industries with loans"], "rows": [crow], "compact": True}]
    if names:
        ind_tables.append({"title": (f"Largest {min(15, len(names))} industries" if len(names) > 1 else "Industry")
                           + " by original gross amount",
                           "head": ihead, "rows": [ind_row(n) for n in names[:15]],
                           "note": f"Bank: full history. SBA: every SBA loan in the industry, FY2000 onward "
                                   f"(PRELIMINARY). \"Small sample\": fewer than {IN.MIN_INDUSTRY_LOANS} bank loans. "
                                   "NAICS: the code shown in FalconBridge's workbook."})
    ind_more = [{"title": f"All {len(names)} industries", "head": ihead, "rows": [ind_row(n) for n in names]}] \
        if len(names) > 15 else []
    sections.append({"id": "industry", "title": "Industry Exposure",
                     "subtitle": "concentration across the bank's lending industries", "tables": ind_tables,
                     "more": ind_more, "status": s.status_line(),
                     "findings": [_item(m["concentration"], anchor="concentration", title="Industry concentration")]
                     if m.get("concentration") else [],
                     "links": [("Portfolio Analysis: Industries (industry exposure chart)", f"{dash}?tab=analysis")]})

    # ------------------------------------------------------------------------------ 4. Benchmark Analysis
    groups = (("total", "All SBA 7(a)"), ("above", f"Standard (> {t})"), ("below", f"Small (<= {t})"),
              ("noncre", "Non-CRE (term <= 10 yrs)"), ("cre", "CRE (term > 10 yrs)"))
    grows = []
    for g, label in groups:
        split = g in ("total", "above", "below")
        d = s.rec(f"compare.{g}.total.cum_def.diff", Kind.DERIVED)
        grows.append({"cls": "hl" if g == "total" else "", "cells": [
            {"text": label}, s.cell(f"benchmark.{g}.total.loans", "int"), s.cell(f"benchmark.{g}.total.cum_def", "pct"),
            s.cell(f"bank.fy2000.{g}.total.loans", "int") if split else {"text": "–"},
            s.cell(f"bank.fy2000.{g}.total.cum_def", "pct") if split else {"text": "not split by term"},
            s.cell(f"compare.{g}.total.cum_def.diff", "pp", Kind.DERIVED, extra=(" below SBA" if d.value < 0 else
                                                                                   " above SBA" if d.value > 0 else ""))
            if d else {"text": "–"}]})
    bl, sl, ni = (s.rec("bank.fy2000.total.total.loans"), s.rec("benchmark.total.total.loans"),
                  s.rec("concentration.total.industries", Kind.DERIVED))
    basis = (f"{m['bank']['name']} loans approved FY2000 onward ({bl.value:,} loans) vs the industry-matched SBA 7(a) "
             f"benchmark: {sl.value:,} SBA loans in this bank's {ni.value:,} industries, approved FY2000 onward."
             if bl is not None and sl is not None and ni is not None else
             "The bank has no loans approved FY2000 onward; no benchmark comparison is available.")
    cnames = _industries(s, "bank.fy2000.total")[:15]
    crows = []
    for n in cnames:
        b, sb = f"bank.fy2000.total.industry.{n}", f"benchmark.total.industry.{n}"
        loans = s.rec(f"{b}.loans").value
        d = s.rec(f"compare.total.industry.{n}.cum_def.diff", Kind.DERIVED)
        small = loans < IN.MIN_INDUSTRY_LOANS
        crows.append({"cells": [{"text": industry_name(n), "tag": "small sample" if small else ""},
                                s.cell(f"{b}.loans", "int"), s.cell(f"{b}.cum_def", "pct"), s.cell(f"{sb}.cum_def", "pct"),
                                s.cell(f"compare.total.industry.{n}.cum_def.diff", "pp", Kind.DERIVED,
                                       extra="" if small or d is None else (" below SBA" if d.value < 0 else
                                                                           " above SBA" if d.value > 0 else "")),
                                s.cell(f"{b}.cum_loss", "pct"), s.cell(f"{sb}.cum_loss", "pct")]})
    btables = [{"title": "% Cum. Defaulted: bank vs SBA benchmark groups", "lead": basis,
                "head": ["SBA benchmark group", "SBA # of Loans", "SBA % Cum. Defaulted", "Bank # of Loans",
                         "Bank % Cum. Defaulted", "Difference"], "rows": grows,
                "note": f"Bank Standard / Small rates use the bank threshold split at {t} (enhancement). Differences "
                        "are arithmetic differences of the rates, not tests of statistical significance."}]
    if crows:
        btables.append({"title": "Industry comparison", "subtitle": "bank FY2000 onward vs every SBA loan in the "
                        f"industry · largest {len(crows)} by bank amount",
                        "head": ["Industry", "Bank # of Loans", "Bank % Cum. Defaulted", "SBA % Cum. Defaulted",
                                 "Difference", "Bank % Cum. Loss", "SBA % Cum. Loss"], "rows": crows})
    drows = [{"cls": "hl" if k == "cum_def" else "",
              "cells": [{"text": label}] + [s.cell(f"benchmark.{g}.total.{k}", fmt) for g, _ in groups]}
             for k, label, fmt in PS_ROWS if k != "age"]
    more = [{"title": "Detailed benchmark measures (SBA benchmark, FY2000 onward)",
             "head": ["Measure"] + [html.escape(lbl) for _, lbl in groups], "rows": drows}]
    for h, hl in (("full", "full history"), ("fy2000", "FY2000 onward")):
        more.append({"id": "groups" if h == "full" else "", "title": f"Bank loan size split ({hl})",
                     "badge": "Enhancement beyond FalconBridge's original workbook",
                     "head": ["Measure", f"Above {t}", f"At or below {t}", "All loans"],
                     "rows": [{"cls": "hl" if k == "cum_def" else "",
                               "cells": [{"text": label}] + [s.cell(f"bank.{h}.{g}.total.{k}", fmt)
                                                             for g in ("above", "below", "total")]}
                              for k, label, fmt in PS_ROWS if k not in ("avg", "age", "purch_pct")]})
    sections.append({"id": "benchmark", "title": "Benchmark Analysis",
                     "subtitle": "comparable default measures across SBA benchmark groups · loans approved FY2000 "
                                 "onward", "tables": btables, "more": more, "status": s.status_line(),
                     "findings_id": "compare",
                     "findings": [_item(r, anchor=f"compare-{r['measure']}", title=f"{r['label']} vs SBA benchmark")
                                  for r in m["compare"]],
                     "empty": "No comparison: the bank has no loans approved FY2000 onward." if not m["compare"]
                     else "",
                     "links": [("Benchmark Analysis tab (vintage chart by benchmark group)", f"{dash}?tab=benchmark")]})

    # ------------------------------------------------------------------------------ 5. Supplementary Analysis
    srows = []
    for y in _years(s, "bank.full.total"):
        b = f"bank.full.total.vintage.{y}"
        srows.append({"cells": [{"text": yl(y)}, s.cell(f"{b}.loans", "int"), s.cell(f"{b}.orig", "money"),
                                s.cell(f"{b}.pif_pct", "pct")]})
    subs = sorted(((r.value, k.split(".")[4]) for (k, kind), r in s.by_key.items()
                   if kind is Kind.FACT and re.fullmatch(r"bank\.full\.total\.subprogram\..+\.orig", k)
                   and PR._num(r.value)), key=lambda x: -x[0])
    subrows = []
    for _, n in subs:
        b = f"bank.full.total.subprogram.{n}"
        subrows.append({"cells": [{"text": n}, s.cell(f"{b}.loans", "int"), s.cell(f"{b}.orig", "money"),
                                  s.cell(f"{b}.active", "int"), s.cell(f"{b}.chgoff", "int"), s.cell(f"{b}.cum_def", "pct"),
                                  s.cell(f"{b}.severity", "pct"), s.cell(f"{b}.cum_loss", "pct")]})
    stables = []
    if srows:
        stables.append({"title": "Original gross amount and paid in full by vintage", "head":
                        ["Vintage", "# of Loans", "Orig. Bal.", "% Paid in full"], "rows": srows,
                        "note": "% Paid in full: original amount of loans in status P I F / original gross amount."})
    if subrows:
        stables.append({"title": "Subprograms (full history)", "head":
                        ["Subprogram (SBA program name)", "# of Loans Orig.", "Orig. Bal.", "Est. # Outstanding",
                         "# Defaulted", "% Cum. Defaulted", "% Loss Severity", "% Cum. Loss"], "rows": subrows,
                        "note": "The dashboard shows FalconBridge's display labels for subprograms; the evidence layer "
                                "holds SBA's program names only."})
    sections.append({"id": "supplementary", "title": "Supplementary Analysis",
                     "badge": "Not in FalconBridge's PerformanceSummary",
                     "subtitle": "origination by vintage, payoff patterns and subprograms", "tables": stables,
                     "status": s.status_line(), "findings": [],
                     "dashboard_only": [
                         ("Gross charge-offs by charge-off year", f"{dash}?tab=reports",
                          "Methodology & Reports: Supplementary analysis"),
                         ("Loan status as of the reporting date", f"{dash}?tab=reports",
                          "Methodology & Reports: Supplementary analysis"),
                         ("Static pool: timing of prepayments and losses", f"{dash}?tab=analysis",
                          "Portfolio Analysis: Static pool")],
                     "links": [("Methodology & Reports: Supplementary analysis charts", f"{dash}?tab=reports"),
                               ("Portfolio Analysis: Subprograms", f"{dash}?tab=analysis"),
                               ("Lending Activity: originations, unguaranteed exposure, geography, loan drill-down",
                                f"{dash}?tab=activity")]})

    # ------------------------------------------------------------------------------ 6. Methodology
    sections.append({"id": "methodology", "title": "Methodology", "subtitle": "definitions, limitations, data "
                     "availability and provenance", "methodology": True, "tables": [], "status": [],
                     "findings": [_item(x, anchor=f"lim-{i}", title=f"Limitation {i}")
                                  for i, x in enumerate(m["limitations"], 1)],
                     "rules": [IN.SEVERITY_NOTE] + list(IN.RULES),
                     "links": [("Methodology & Reports: disclosures and FalconBridge definitions",
                                f"{dash}?tab=reports")]})
    return sections
