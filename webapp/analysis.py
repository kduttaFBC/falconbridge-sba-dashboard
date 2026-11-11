"""Dashboard data for one analysis (period, lender, threshold). No financial logic lives here: every figure comes from
the generator's own modules - validation.Group through threshold_analysis.compute for the bank, and benchmark.aggregate /
benchmark.tables / threshold_analysis.benchmark_view for the SBA benchmark - i.e. the same calculations as the
workbook generated for the same criteria. This module only selects and arranges the values for display."""
import threading
from collections import OrderedDict

from . import generator_bridge as GB
from . import insights as IN
from . import lending_activity as LA
from . import source_notes as SN
from . import static_pool as SP

B, TA, C, S = GB.B, GB.TA, GB.GC, GB.S
_lock = threading.Lock()
_bank_cache, _bench_cache, _result_cache = OrderedDict(), OrderedDict(), OrderedDict()
_status_map = None
CALC_VERSION = "dashboard-1.0 / " + GB.GENERATOR_VERSION


def _lru(cache, key, make, size):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    value = make()
    cache[key] = value
    while len(cache) > size:
        cache.popitem(last=False)
    return value


def status_map():
    global _status_map
    if _status_map is None:
        _status_map = TA.status_map_from_template()
    return _status_map


_template_text = None


def template_text():
    """FalconBridge's own wording from the template (read-only): subprogram display labels (PerformanceSummary A->B,
    the four pairs the generator uses) and the Glossary rows."""
    global _template_text
    if _template_text is None:
        from openpyxl import load_workbook
        wb = load_workbook(C.TEMPLATE_PATH, read_only=True)
        labels = {}
        for a, b in wb[C.SH_SUMMARY].iter_rows(min_row=7, max_row=10, max_col=2, values_only=True):
            if a and b:
                labels[a] = b
        gloss = [tuple(r[1:4]) for r in wb[C.SH_GLOSSARY].iter_rows(min_row=1, max_col=4, values_only=True)]
        wb.close()
        _template_text = {"labels": labels, "glossary": gloss}
    return _template_text


def glossary(first_fy, as_of, bank_label, titles):
    """The Glossary as written in the generated workbook (generator's update_glossary wording), with the section
    headers that are formulas there resolved to their text."""
    quarter = f"Q{(as_of.month - 1) // 3 + 1} {as_of.year}"
    heads = {"=PerformanceSummary!K2": bank_label, "=PerformanceSummary!$AJ$2": titles[0],
             "=PerformanceSummary!$AV$2": titles[1]}
    out, section = [], "Industry exposure"
    for i, (b, c, d) in enumerate(template_text()["glossary"], start=1):
        if c == "Field Name":
            continue
        if i == 5:
            b = "Securitization (FalconBridge loan tape - not available from SBA FOIA data)"
        if i == 26:
            b = "All SBA 7(a) - PRELIMINARY industry-matched SBA benchmark"
        fix = lambda t: t.replace("FY2003", f"FY{first_fy}").replace("Q2 2026", quarter) if isinstance(t, str) else t
        b, c, d = fix(b), fix(c), fix(d)
        if isinstance(b, str) and b in heads:
            b = heads[b]
        if b and c and d:                                  # first row of a section: header in B
            section = b
            out.append({"section": section, "field": c, "definition": d})
        elif c and d:                                      # continuation rows: field in C, definition in D
            out.append({"section": section, "field": c, "definition": d})
        elif b and c and i > 2:                            # general rows: field in B, definition in C
            out.append({"section": section, "field": b, "definition": c})
    return out


def bank_data(period, sel):
    key = (period, sel["bank_name"], sel["bank_state"], sel["cert"], tuple(sel["location_ids"]))

    def make():
        folder = GB.periods()[period]
        ext = S.extract_bank(sel["bank_name"], sel["bank_state"], folder, cert=sel["cert"] or None,
                             location_ids=sel["location_ids"])
        lists = S.build_lists(ext["rows"])
        unknown = sorted({r[C.F_LOAN_STATUS].strip() for r in ext["rows"]} - set(status_map()))
        if unknown:
            raise GB.SourceError(f"LoanStatus values {unknown} are not in the template's Lookup table.")
        return ext, lists
    return _lru(_bank_cache, key, make, 4)


def bench_data(period, threshold):
    folder = GB.periods()[period]
    return _lru(_bench_cache, (period, threshold), lambda: B.aggregate(folder, progress=lambda *_: None,
                                                                         threshold=threshold), 3)


def parse_threshold(text):
    """Whole dollars, thousands separators and a leading $ allowed. Returns (value, error message)."""
    t = (text or "").strip().replace(",", "").replace("$", "").replace(" ", "")
    if t == "":
        return C.BENCHMARK_STANDARD_MIN, None
    if not t.isdigit():
        return None, "Enter the threshold as a whole-dollar amount, for example 350,000."
    v = int(t)
    if v > 100_000_000:
        return None, "Enter a threshold of $100,000,000 or less."
    return v, None


def _jsonable(m):
    return {k: (None if v == "" else v) for k, v in m.items()}


def compute(period, sel, threshold):
    key = (period, sel["bank_name"], sel["bank_state"], sel["cert"], tuple(sel["location_ids"]), threshold)
    with _lock:
        return _lru(_result_cache, key, lambda: _compute(period, sel, threshold), 8)


def _compute(period, sel, threshold):
    ext, lists = bank_data(period, sel)
    rows = ext["rows"]
    as_of = S.snapshot_as_of(lists["as_of_values"], GB.period_date(period))
    bdata = bench_data(period, threshold)
    sector_labels = [s[0] for s in lists["sectors"]] + ([C.NA_SECTOR] if lists["na"] else [])
    subprograms = [s[0] for s in lists["subprograms"]]
    v0, v1 = lists["vintages"][0], lists["vintages"][-1]
    years = list(range(min(1991, v0), max(2026, v1) + 1))
    res = TA.compute(rows, status_map(), as_of, threshold, years, sector_labels, subprograms)
    tabs = B.tables(bdata, sector_labels)
    bench = TA.benchmark_view(tabs, sector_labels)
    blocks = res["blocks"]

    def block(h, g):
        b = blocks[(h, g)]
        return {"total": _jsonable(b[("total", "TOTAL")]),
                "vintage": [dict(year=y, **_jsonable(b[("v", y)])) for y in years],
                "industry": [dict(name=s, **_jsonable(b[("i", s)])) for s in sector_labels],
                "subprogram": [dict(name=s, **_jsonable(b[("s", s)])) for s in subprograms]}

    bank = {h: {g: block(h, g) for g, _ in TA.GROUPS} for h, _ in TA.HISTORIES}
    chg = {g: {"years": [{"year": y, "loans": v[0], "amount": v[1]} for y, v in d["years"].items()],
               "undated": d["undated"]} for g, d in res["chgoff_by_year"].items()}
    status = {g: [{"status": s, "loans": v[0], "orig": v[1]} for s, v in d.items()] for g, d in res["status"].items()}
    benchmark = {k: {"name": v["name"], "total": _jsonable(v["total"]),
                     "vintage": [dict(year=y, **_jsonable(m)) for y, m in v["vintage"].items()],
                     "sector": [dict(name=s, **_jsonable(m)) for s, m in v["sector"].items()]}
                 for k, v in bench.items()}
    # display-only arrangements, computed here so the browser does no arithmetic: industry share of the bank's
    # original amount (the template's '% of Total Orig.' definition), concentration, and bank minus SBA differences
    concentration = {}
    for h in bank:
        for g, b in bank[h].items():
            tot = b["total"]["orig"]
            for row in b["industry"]:
                row["share"] = row["orig"] / tot if tot else "n/a"
            if h == "full":
                shares = sorted((r["share"] for r in b["industry"] if r["share"] != "n/a"), reverse=True)
                concentration[g] = {"industries": sum(1 for r in b["industry"] if r["loans"]),
                                    "top1": sum(shares[:1]) if tot else "n/a", "top5": sum(shares[:5]) if tot else "n/a",
                                    "top10": sum(shares[:10]) if tot else "n/a"}
    diff = lambda a, b: a - b if isinstance(a, (int, float)) and isinstance(b, (int, float)) else "n/a"
    comparison, industry_cmp = {}, {}
    for g in ("total", "above", "below"):
        bt, st = bank["fy2000"][g]["total"], benchmark[g]["total"]
        comparison[g] = {k: {"bank": bt[k], "sba": st[k], "diff": diff(bt[k], st[k])}
                         for k in ("loans", "orig", "avg", "chgoff", "cum_def", "severity", "cum_loss", "purch_pct")}
        sec = {r["name"]: r for r in benchmark[g]["sector"]}
        rows_i = sorted((r for r in bank["fy2000"][g]["industry"] if r["loans"]), key=lambda r: -r["orig"])
        industry_cmp[g] = [{"name": r["name"], "loans": r["loans"], "orig": r["orig"], "share": r["share"],
                            "bank_cum_def": r["cum_def"], "sba_cum_def": sec[r["name"]]["cum_def"],
                            "diff_cum_def": diff(r["cum_def"], sec[r["name"]]["cum_def"]),
                            "bank_cum_loss": r["cum_loss"], "sba_cum_loss": sec[r["name"]]["cum_loss"],
                            "diff_cum_loss": diff(r["cum_loss"], sec[r["name"]]["cum_loss"]),
                            "sba_loans": sec[r["name"]]["loans"]} for r in rows_i]
    # FalconBridge's display labels and NAICS codes (Sector_Pivot "Min of NaicsCode"), as in the workbook
    labels = template_text()["labels"]
    naics = {}
    for r in rows:
        code = r.get("NaicsCode", "").strip()
        if code:
            k = r[C.F_SECTOR]
            naics[k] = min(naics.get(k, code), code, key=lambda x: (len(x), x))
    for h in bank:
        for g in bank[h]:
            for row in bank[h][g]["subprogram"]:
                row["label"] = labels.get(row["name"], row["name"])
            for row in bank[h][g]["industry"]:
                row["naics"] = naics.get(row["name"], "")
    titles = (f"All SBA 7(a) - Standard (Bal > {B.threshold_label(threshold)})",
              f"All SBA 7(a)  - Small (Bal <= {B.threshold_label(threshold)})")
    default_line = []
    for g, name in (("total", "All SBA 7(a)"), ("above", f"Standard (> ${threshold:,})"),
                    ("below", f"Small (<= ${threshold:,})"), ("noncre", "Non-CRE (term <= 10 yrs)"),
                    ("cre", "CRE (term > 10 yrs)")):
        sb = benchmark[g]["total"]
        bk = bank["fy2000"][g]["total"] if g in bank["fy2000"] else None
        default_line.append({"group": g, "name": name, "sba_loans": sb["loans"], "sba_cum_def": sb["cum_def"],
                             "bank_loans": bk["loans"] if bk else None, "bank_cum_def": bk["cum_def"] if bk else None,
                             "diff": diff(bk["cum_def"], sb["cum_def"]) if bk and bk["loans"] else "n/a"})
    gaps = {k.split(": ", 1)[1]: v for k, v in bdata["counts"].items() if k.startswith("definition gap")}
    folder = GB.periods()[period]
    payload = {
        "criteria": {"period": period, "as_of": as_of.isoformat(), "bank_name": sel["bank_name"],
                     "bank_state": sel["bank_state"], "cert": sel["cert"], "location_ids": sel["location_ids"],
                     "status": sel["status"], "threshold": threshold, "threshold_label": B.threshold_label(threshold),
                     "default_threshold": threshold == C.BENCHMARK_STANDARD_MIN},
        "identity": {"resolution": GB.RESOLUTION_TEXT[sel["status"]], "selected": sel["selected"],
                     "excluded": sel["excluded"], "records": ext["total_records"], "eligible": len(rows),
                     "excluded_counts": ext["excluded"]},
        "bank": bank, "chgoff_by_year": chg, "status": status, "benchmark": benchmark,
        "concentration": concentration, "comparison": comparison, "industry_comparison": industry_cmp,
        "default_line": default_line, "group_titles": titles,
        "glossary": glossary(lists["first_approval_fy"], as_of, f"{sel['bank_name']}, {sel['bank_state']}", titles),
        "benchmark_population": {"loans": B.totals(bdata)[0], "orig": B.totals(bdata)[1],
                                 "industries": len(sector_labels), "definition_gaps": gaps},
        "release": {"folder": str(folder), "files": bdata["files"]},
        "calculation_version": CALC_VERSION,
    }
    payload["seasoning"] = seasoning(payload, as_of.year)
    payload["partial_year"] = partial_year(as_of)
    payload["static_pool"] = static_pool(rows, sector_labels, as_of, payload)
    payload["activity"] = LA.compute(rows, status_map(), as_of)        # Phase 1 lending activity (lending_activity.py)
    payload["source_notes"] = SN.for_bank(period, sel["cert"], payload)
    payload["insights"] = IN.build(payload)          # presentation rules over the figures above
    return payload


def seasoning(p, year):
    """Portfolio age context for the headline comparison, from validated measures only: the bank's share of original
    amount by vintage (PerformanceSummary '% of Total Orig.' N on the vintage rows, full history) and the SBA
    benchmark's (Z on the vintage rows: Y / total of vintage rows 1999-2026), grouped into three disbursement-year
    bands, plus the existing vintage-by-vintage % Cum. Defaulted of both. No mix-adjusted benchmark is calculated."""
    bank_v = p["bank"]["full"]["total"]["vintage"]
    sba_v = {r["year"]: r for r in p["benchmark"]["total"]["vintage"]}
    total = p["bank"]["full"]["total"]["total"]
    sba_total = sum(r["orig"] for r in sba_v.values())
    first_sba = min(sba_v)
    bands = [("Disbursed 2008 or earlier", first_sba, 2008), (f"Disbursed 2009-{year - 5}", 2009, year - 5),
             (f"Disbursed {year - 4}-{year}*", year - 4, year)]
    out = []
    for label, lo, hi in bands:
        b = [r for r in bank_v if lo <= r["year"] <= hi]
        s_ = [r for y, r in sba_v.items() if lo <= y <= hi]
        out.append({"label": label, "from": lo, "to": hi,
                    "bank_loans": sum(r["loans"] for r in b), "bank_orig": sum(r["orig"] for r in b),
                    "bank_share": sum(r["orig"] for r in b) / total["orig"] if total["orig"] else "n/a",
                    "sba_loans": sum(r["loans"] for r in s_),
                    "sba_share": sum(r["orig"] for r in s_) / sba_total if sba_total else "n/a"})
    pre = [r for r in bank_v if r["year"] < first_sba and r["loans"]]
    vint = [{"year": r["year"], "bank_loans": r["loans"], "bank_orig": r["orig"], "bank_cum_def": r["cum_def"],
             "sba_loans": sba_v[r["year"]]["loans"], "sba_cum_def": sba_v[r["year"]]["cum_def"],
             "small_sample": r["loans"] < IN.MIN_INDUSTRY_LOANS}
            for r in bank_v if r["loans"] and r["year"] in sba_v]
    above = p["bank"]["full"]["above"]["total"]
    return {"bands": out, "recent_from": year - 4, "recent_to": year,
            "bank_pre_benchmark_loans": sum(r["loans"] for r in pre),
            "bank_pre_benchmark_share": sum(r["orig"] for r in pre) / total["orig"] if total["orig"] else "n/a",
            "first_benchmark_vintage": first_sba, "bank_age_months": total["age"],
            "bank_outstanding_loans": total["active"], "bank_loans": total["loans"],
            "bank_avg": total["avg"], "sba_avg": p["benchmark"]["total"]["total"]["avg"],
            "bank_share_above": above["orig"] / total["orig"] if total["orig"] else "n/a",
            "vintages": vint}


def static_pool(rows, sector_labels, as_of, p):
    """ChargeOffsByVintage (webapp.static_pool), plus the vintages charted by default: the two largest by original
    amount in each of the portfolio-age bands used on Executive Portfolio (2008 or earlier, 2009 to y-5, y-4 to y)."""
    sp = SP.compute(rows, sector_labels, as_of, {r["year"]: r["loans"] for r in p["bank"]["full"]["total"]["vintage"]})
    bands = p["seasoning"]["bands"]
    default = []
    for i, b in enumerate(bands):
        lo = bands[i - 1]["to"] + 1 if i else None
        band = [v for v in sp["vintages"] if (lo is None or v["year"] >= lo) and v["year"] <= b["to"]]
        default += [v["year"] for v in sorted(band, key=lambda v: -v["orig_mm"])[:2]]
    sp["default_vintages"] = sorted(default)
    sp["band_labels"] = [b["label"] for b in bands]
    return sp


def partial_year(as_of):
    """The reporting year is partial unless the release is as of December 31: its vintage holds only the loans first
    disbursed up to the AsOfDate."""
    if (as_of.month, as_of.day) == (12, 31):
        return None
    return {"year": as_of.year, "through": f"{as_of:%B} {as_of.day}, {as_of.year}",
            "label": f"{as_of.year}*", "note": f"* {as_of.year} is a partial year: loans first disbursed through "
                                              f"{as_of:%B} {as_of.day}, {as_of.year}."}
