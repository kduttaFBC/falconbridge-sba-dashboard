"""Executive summary text and data-sufficiency flags for the dashboard - deterministic presentation rules only.

Every sentence quotes or compares values already in the analysis payload (validated measures); nothing here
calculates a new financial measure, estimates a value, or draws a conclusion beyond the stated rules:
  * no sentence characterizes a bank as better, worse, in line, exceptional or similar: differences are stated as
    numbers, and the overall comparison is always accompanied by the portfolio-age (vintage mix) facts
  * MIN_LOANS   - fewer loans than this in a comparison is flagged "limited data: interpret with caution"
  * MIN_INDUSTRY_LOANS - industry rows with fewer bank loans than this are marked "small sample"
  * charts that need a trend are shown only with at least MIN_TREND_POINTS vintages (or years) that have loans.
The rules are listed on the dashboard's Definitions & data tab.
"""
MIN_LOANS = 30
MIN_INDUSTRY_LOANS = 10
MIN_TREND_POINTS = 3

RULES = [
    "Observations state values and differences only. They never describe a bank as outperforming, underperforming, "
    "in line with, exceptional or superior: an overall rate does not account for differences in portfolio age, loan "
    "size, loan term or industry mix.",
    "Portfolio age: shares of original gross amount by disbursement year (vintage) for the bank (full history) and the "
    "SBA benchmark, in three bands - 2008 or earlier, 2009 to five years before the reporting year, and the most "
    "recent five vintages. Every overall bank-vs-benchmark comparison is shown with these shares.",
    f"Comparisons based on fewer than {MIN_LOANS} bank loans are labelled \"limited data\"; with no loans the "
    "comparison is not shown.",
    f"Industry rows with fewer than {MIN_INDUSTRY_LOANS} bank loans are marked \"small sample\"; a single loan can "
    "move such a rate by many percentage points.",
    f"Trend charts are shown only when at least {MIN_TREND_POINTS} vintages (or charge-off years) have loans.",
    "The reporting year of a release dated before December 31 is a partial vintage (marked *): it holds only loans "
    "first disbursed up to the release date, so its rates and shares are not comparable with full years.",
    f"The range of SBA benchmark vintage default rates quoted with portfolio age uses only vintages with at least "
    f"{MIN_LOANS} SBA loans; bank vintages with fewer than {MIN_INDUSTRY_LOANS} loans are marked as small samples.",
    "Differences are arithmetic differences of the displayed rates; they are not tests of statistical significance.",
]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def money(v):
    if not _num(v):
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:,.2f} billion"
    if a >= 1e6:
        return f"${v / 1e6:,.1f} million"
    if a >= 1e3:
        return f"${v / 1e3:,.0f} thousand"
    return f"${v:,.0f}"


def pct(v):
    return f"{v * 100:.2f}%" if _num(v) else "n/a"


def plural(n, word):
    many = word[:-1] + "ies" if word.endswith("y") and word[-2:-1] not in "aeiou" else word + "s"
    return f"{n:,} {word if n == 1 else many}"


def count_amount(t, what):
    return f"{plural(t['loans'], 'loan')} ({money(t['orig'])}) {what}" if t["loans"] else f"no loans {what}"


def industry_name(name):
    return "Not classified (no NAICS code published)" if name == "N/A" else name


def compare(bank, sba, measure):
    """(sign, sentence) for two rates: the values and their arithmetic difference, without characterization."""
    if not (_num(bank) and _num(sba)):
        return None, f"{measure} cannot be compared (no value for the bank or the benchmark)."
    d = bank - sba
    sign = "below" if d < 0 else "above" if d > 0 else "equal"
    return sign, (f"{measure}: bank {pct(bank)}, SBA benchmark {pct(sba)} (difference {'+' if d >= 0 else '-'}"
                  f"{abs(d) * 100:.2f} percentage points).")


def sba_range_sentence(p):
    """Symmetric context: how much the benchmark's own % Cum. Defaulted varies by vintage (workbook vintage rows)."""
    v = [r for r in p["benchmark"]["total"]["vintage"] if r["loans"] >= MIN_LOANS and _num(r["cum_def"])]
    if len(v) < 2:
        return ""
    lo, hi = min(v, key=lambda r: r["cum_def"]), max(v, key=lambda r: r["cum_def"])
    return (f" Cumulative default rates depend on how long loans have been outstanding and on conditions in each "
            f"vintage: the SBA benchmark's own % Cum. Defaulted ranges from {pct(lo['cum_def'])} ({lo['year']} vintage) "
            f"to {pct(hi['cum_def'])} ({hi['year']} vintage), among vintages with at least {MIN_LOANS} SBA loans.")


def age_sentence(sz, partial=None):
    """The portfolio-age facts that accompany every overall comparison."""
    if not sz["vintages"]:
        return (f"All of the bank's loans were disbursed before {sz['first_benchmark_vintage']}, which the SBA benchmark "
                "does not cover, so no portfolio-age comparison is possible.")
    recent = sz["bands"][-1]
    b, s = recent["bank_share"], recent["sba_share"]
    span = f"{sz['recent_from']}-{sz['recent_to']}"
    if partial and partial["year"] == sz["recent_to"]:
        span += f" ({partial['year']} through {partial['through'].rsplit(',', 1)[0]})"
    txt = (f"{pct(b)} of the bank's original gross amount was disbursed in {span}, "
           f"against {pct(s)} of the SBA benchmark's")
    early = sz["bands"][0]
    txt += (f"; {pct(early['bank_share'])} of the bank's and {pct(early['sba_share'])} of the benchmark's in "
            f"{early['to']} or earlier")
    if sz["bank_pre_benchmark_loans"]:
        txt += (f"; {pct(sz['bank_pre_benchmark_share'])} of the bank's was disbursed before "
                f"{sz['first_benchmark_vintage']}, which the benchmark does not cover")
    return txt + "."


def sufficiency(block_full, block_fy, chg_years):
    """What the data can support, for one loan group."""
    t, f = block_full["total"], block_fy["total"]
    vint = [r for r in block_full["vintage"] if r["loans"]]
    inds = [r for r in block_full["industry"] if r["loans"]]
    fy_vint = [r for r in block_fy["vintage"] if r["loans"]]
    return {"loans": t["loans"], "fy2000_loans": f["loans"], "vintages": len(vint), "industries": len(inds),
            "fy2000_vintages": len(fy_vint), "chgoff_years": sum(1 for y in chg_years if y["loans"]),
            "trend": len(vint) >= MIN_TREND_POINTS, "fy2000_trend": len(fy_vint) >= MIN_TREND_POINTS,
            "chgoff_trend": sum(1 for y in chg_years if y["loans"]) >= MIN_TREND_POINTS,
            "industry_chart": len(inds) >= 2, "comparison": f["loans"] > 0, "limited": 0 < f["loans"] < MIN_LOANS,
            "limited_full": 0 < t["loans"] < MIN_LOANS}


SEVERITY_NOTE = ("Benchmark % Loss Severity and % Cum. Loss follow FalconBridge's template: the total severity is the "
                 "average of industry severities weighted by the number of defaulted loans, and % Cum. Loss = "
                 "% Cum. Defaulted x that severity. The bank's figures are pooled (total charge-offs / original "
                 "amounts), so % Cum. Defaulted is the directly comparable measure.")


def build(p):
    """Observations for the Executive Portfolio screen (FalconBridge PerformanceSummary terms). The bank's own
    figures are full history (PerformanceSummary K:W); benchmark comparisons use the bank's loans approved FY2000
    onward, the benchmark's population, and lead with % Cum. Defaulted."""
    c, B = p["criteria"], p["bank"]
    t_label = f"${c['threshold']:,}"
    names = {"total": "all loans", "above": f"loans above {t_label}", "below": f"loans at or below {t_label}"}
    out = {"rules": RULES, "severity_note": SEVERITY_NOTE, "groups": {}}
    for g in ("total", "above", "below"):
        full, fy = B["full"][g], B["fy2000"][g]
        s = sufficiency(full, fy, p["chgoff_by_year"][g]["years"])
        t, f = full["total"], fy["total"]
        lines = []
        if not t["loans"]:
            lines.append({"kind": "limit", "label": "No matching loans", "text":
                          f"{c['bank_name']} has no eligible SBA 7(a) {names[g]} in this release."})
            out["groups"][g] = {"sufficiency": s, "headline": "No matching loans", "lines": lines, "comparison": []}
            continue
        vint = [r for r in full["vintage"] if r["loans"]]
        first, last = vint[0]["year"], vint[-1]["year"]
        span = f"in {first}" if first == last else f"between {first} and {last}"
        headline = (f"{plural(t['loans'], 'SBA 7(a) loan')} originated, {money(t['orig'])} original gross amount, "
                    f"disbursed {span}" + ("" if g == "total" else f" ({names[g]})") + ".")
        lines.append({"kind": "size", "label": "Portfolio", "text": headline + " Full history in SBA FOIA data."})
        if g == "total" and p.get("seasoning"):
            sz = p["seasoning"]
            lines.append({"kind": "age", "label": "Portfolio age", "text": age_sentence(sz, p.get("partial_year"))
                          + (f" Average age of estimated outstanding loans: {sz['bank_age_months']:.1f} months."
                             if _num(sz["bank_age_months"]) else "")
                          + (sba_range_sentence(p) if sz["vintages"] else "")})
        lines.append({"kind": "status", "label": f"As of {c['as_of']}", "text":
                      (f"{plural(t['active'], 'loan')} estimated outstanding ({money(t['active_orig'])} original "
                       "amount; current principal is not published by SBA)" if t["active"] else
                       "No loans estimated outstanding") + f"; {plural(t['chgoff'], 'loan')} defaulted (charged off)."})
        if t["chgoff"]:
            lines.append({"kind": "performance", "label": "Credit performance", "text":
                          f"% Cum. Defaulted {pct(t['cum_def'])} of original gross amount; % Cum. Loss "
                          f"{pct(t['cum_loss'])} ({money(t['chgoff_amt'])} gross charge-offs)."})
        else:
            lines.append({"kind": "performance", "label": "Credit performance",
                          "text": "No defaulted (charged-off) loans in SBA FOIA data."})
        inds = sorted((r for r in full["industry"] if r["loans"]), key=lambda r: -r["orig"])
        if len(inds) > 1:
            top = inds[0]
            top5 = p["concentration"][g]["top5"] if len(inds) >= 5 else None
            lines.append({"kind": "industry", "label": "Industry exposure", "text":
                          f"Largest industry {industry_name(top['name'])}, {pct(top['share'])} of original gross "
                          "amount" + (f"; largest five industries {pct(top5)}" if top5 is not None else "")
                          + f"; {plural(len(inds), 'industry')} with loans."})
        comp = []
        cm = p["comparison"][g]
        pre = t["loans"] - f["loans"]
        if not s["comparison"]:
            lines.append({"kind": "limit", "label": "SBA benchmark", "text":
                          "Not comparable: the bank has no loans approved FY2000 or later, the period covered by the "
                          "SBA benchmark."})
        else:
            for key, label in (("cum_def", "% Cum. Defaulted"), ("severity", "% Loss Severity"),
                               ("cum_loss", "% Cum. Loss")):
                word, text = compare(cm[key]["bank"], cm[key]["sba"], label)
                comp.append({"measure": key, "label": label, "direction": word, "text": text,
                             "bank": cm[key]["bank"], "sba": cm[key]["sba"], "diff": cm[key]["diff"]})
            basis = (f"bank loans approved FY2000 onward ({plural(f['loans'], 'loan')}"
                     + (f"; {plural(pre, 'earlier loan')} excluded" if pre else "") + ")")
            lines.append({"kind": "limit" if s["limited"] else "comparison", "label": "Versus SBA benchmark",
                          "text": f"{comp[0]['text']} Basis: {basis}. Overall rates are not adjusted for differences "
                          "in portfolio age, loan size or industry mix; compare vintage by vintage before drawing "
                          "conclusions."
                          + (f" Limited data (fewer than {MIN_LOANS} loans): interpret with caution." if s["limited"]
                             else "")})
        if s["limited_full"]:
            lines.append({"kind": "limit", "label": "Data limitation", "text":
                          f"Only {plural(t['loans'], 'loan')}: these figures describe very few loans and may not be "
                          "representative."})
        out["groups"][g] = {"sufficiency": s, "headline": headline, "lines": lines, "comparison": comp,
                            "pre_fy2000_loans": pre}
    return out
