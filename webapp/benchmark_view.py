"""Benchmarks view (Benchmark Intelligence Phase A): /analyses/{id}/benchmarks.

Separate from, and without changing, the Executive Summary, the dashboard and the existing evidence: it shows B0 (the
current, FalconBridge-directed preliminary benchmark, read unchanged), B1 and B2 (investigative comparisons) and the industry contribution
analysis for one Saved Analysis, each with its population, numerator, denominator, weighting, coverage, reporting
period, caveats and independent-validation status.

Every value shown is an evidence record of a Benchmark Intelligence evidence pack (system foia-benchmark-intel, bound
to the record's SHA-256). A value is INDEPENDENTLY_VERIFIED only when a clean-room validation record
(intelligence/cleanroom_benchmark.py) bound to that exact record agrees with it. Every sentence is verified by the M2
verifier (m2-1.3, whose benchmark-identity rule stops B0 / B1 / B2 labels and populations being swapped).

Off unless FBX_BENCHMARK_INTEL=enabled (the pending Render release is unaffected while the variable is unset).
"""
import os
import threading
from collections import OrderedDict
from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from intelligence import cleanroom_benchmark as CB
from intelligence import cleanroom_foia as CR
from intelligence import validation as VAL
from intelligence import verifier as VF
from intelligence.claims import Calculation, ClaimType as T
from intelligence.evidence import (Classification as CL, EvidencePack, EvidenceRecord, Kind, Methodology as ME, Source,
                                   ValidationStatus as VS, make_id, sha256_of, validate_pack)

from . import benchmark_intel as BI
from . import presentation as PR
from . import settings

ENABLED = os.environ.get("FBX_BENCHMARK_INTEL", "") == "enabled"
ADAPTER_VERSION = "bi-evidence-1.0"
VIEW_VERSION = "benchmarks-view-1.0"
INVESTIGATIVE = "INVESTIGATIVE: not FalconBridge methodology; for discussion only"
PRELIM = "PRELIMINARY: FalconBridge has not confirmed the benchmark methodology"
ORIGINAL = "Original amounts, not balances; cumulative to the reporting date, not annual"
NAMES = {
    "B0": "Current SBA benchmark (FalconBridge-directed, preliminary)",
    "B1": "All-SBA comparison (investigative)",
    "B2": "SBA weighted to the bank's industry mix (investigative)",
}
POPULATION = {
    "B0": "All SBA 7(a) loans approved FY2000 onward, not cancelled, disbursed, in the {n} industries where the bank "
          "has any eligible loan (any approval year)",
    "B1": "All SBA 7(a) loans approved FY2000 onward, not cancelled, disbursed, in every industry ({loans:,} loans)",
    "B2": "B0's SBA loans, by industry; industries with fewer than {sparse} SBA loans are excluded and listed",
}
WEIGHTING = {
    "B0": "The SBA's own original amounts (each industry counts in proportion to its SBA lending); equivalent to the "
          "template's SUMPRODUCT(industry rate, SBA original $) / SUM(SBA original $)",
    "B1": "The SBA's own original amounts, every industry",
    "B2": "The bank's FY2000+ original-$ share in each industry (direct standardisation on industry only), "
          "renormalised over covered industries",
}
DEFAULT_DEF = ("SBA: ChargeOffDate present (date-based). Bank: LoanStatus charged off (status-based), as in the "
               "workbook")


def pct(x, d=2):
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def pp(x):
    if x is None:
        return "n/a"
    return "0.00 pp" if round(x * 100, 2) == 0 else f"{x * 100:+.2f} pp"


def usd(x):
    return "n/a" if x is None else (f"${x / 1e9:,.2f} billion" if abs(x) >= 1e9 else f"${x / 1e6:,.1f} million")


# --------------------------------------------------------------------------------------------- evidence
def _specs(content):
    """(metric key, value, unit, label, payload path, benchmark, measure kind) of every value the view shows."""
    out = []
    add = lambda *a: out.append(a if len(a) == 8 else a + (None,))                      # noqa: E731
    for grp, path in (("bank_fy2000", "bank_fy2000"), ("B0", "B0"), ("B1", "B1")):
        d = content[path]
        who = {"bank_fy2000": "Bank, loans approved FY2000 onward", "B0": NAMES["B0"] + ", SBA loans in the bank's "
               "industries at the SBA's own industry mix", "B1": NAMES["B1"] + ", every industry"}[grp]
        add(f"bench_intel.{grp}.loans", d["loans"], "count", f"{who}: number of loans", f"/{path}/loans", grp, "n")
        add(f"bench_intel.{grp}.orig", d["orig"], "usd", f"{who}: original amount (denominator)", f"/{path}/orig",
            grp, "n")
        add(f"bench_intel.{grp}.defaulted_orig", d["defaulted_orig"], "usd",
            f"{who}: original amount of defaulted loans (numerator)", f"/{path}/defaulted_orig", grp, "n")
        add(f"bench_intel.{grp}.cum_def", d["cum_def"], "ratio", f"{who}: % Cum. Defaulted", f"/{path}/cum_def", grp,
            "rate")
    b2 = content["B2"]
    add("bench_intel.B2.coverage", b2["coverage"], "ratio",
        f"B2 coverage: share of the bank's FY2000+ original amount in industries with at least {BI.SPARSE} SBA loans",
        "/B2/coverage", "B2", "n")
    if b2["computed"]:
        add("bench_intel.B2.cum_def", b2["cum_def"], "ratio", NAMES["B2"] + ": % Cum. Defaulted", "/B2/cum_def", "B2",
            "rate")
        for k, lab in (("gap", "bank FY2000+ minus B0"), ("mix_total", "industry-mix component (B2 minus B0)"),
                       ("within_total", "within-industry component (bank on covered industries minus B2)"),
                       ("sparse_adjustment", "sparse-industry adjustment (bank minus bank on covered industries)")):
            add(f"bench_intel.decomposition.{k}.diff", content["decomposition"][k], "ratio",
                f"Industry contribution analysis (investigative): {lab}", f"/decomposition/{k}", "D", "diff")
    full = content["bank_full"]
    add("bench_intel.bank_full.cum_def", full["cum_def"], "ratio",
        "Bank, full history (reference only; not comparable with the SBA comparisons): % Cum. Defaulted",
        "/bank_full/cum_def", "bank_full", "rate")
    for i, x in enumerate(content["industries"]):
        n, p = x["name"], f"bench_intel.industry.{x['name']}."
        base = f"/industries/{i}/"
        add(p + "bank_loans", x["bank_loans"], "count", f"{n}: bank loans approved FY2000 onward", base + "bank_loans",
            "I", "bank", x)
        add(p + "bank_share", x["bank_share"], "ratio", f"{n}: share of the bank's FY2000+ original amount",
            base + "bank_share", "I", "bank", x)
        add(p + "sba_loans", x["sba_loans"], "count", f"{n}: SBA loans approved FY2000 onward", base + "sba_loans", "I",
            "sba", x)
        add(p + "sba_share", x["sba_share"], "ratio", f"{n}: share of B0's SBA original amount", base + "sba_share",
            "I", "sba", x)
        if x["sba_cum_def"] is not None:
            add(p + "sba_cum_def", x["sba_cum_def"], "ratio", f"{n}: SBA % Cum. Defaulted", base + "sba_cum_def", "I",
                "sba", x)
        if x["bank_cum_def"] is not None:
            add(p + "bank_cum_def", x["bank_cum_def"], "ratio", f"{n}: bank FY2000+ % Cum. Defaulted",
                base + "bank_cum_def", "I", "bank", x)
        if "mix" in x:
            add(p + "mix.diff", x["mix"], "ratio", f"{n}: industry-mix component (investigative)", base + "mix", "I",
                "diff", x)
            if x["b2_weight"]:
                add(p + "within.diff", x["within"], "ratio", f"{n}: within-industry component (investigative)",
                    base + "within", "I", "diff", x)
    return out


def _limits(group, kind, content, row=None):
    lim = [ORIGINAL]
    if group == "B0" or (group == "I" and kind == "sba"):
        lim += [PRELIM, "Weighted by the SBA's own industry mix, not the bank's",
                "Not adjusted for vintage, loan size or term", "SBA defaults are date-based; the bank's are status-based"]
    elif group == "B1":
        lim += [INVESTIGATIVE, "Includes industries in which the bank has never lent",
                "Not adjusted for vintage, industry, loan size or term"]
    elif group in ("B2", "D") or (group == "I" and kind == "diff"):
        lim += [INVESTIGATIVE, "Not adjusted for vintage, loan size or term (industry mix only)"]
    elif group == "bank_full":
        lim += ["Full history includes loans approved before FY2000; the SBA comparisons start at FY2000"]
    if row is not None:
        if row["bank_small_sample"]:
            lim.append(f"Small sample: fewer than {BI.SMALL_BANK} bank loans")
        if row["credibility"] != "credible":
            lim.append(f"Limited data: fewer than {BI.CREDIBLE} SBA loans in this industry")
    return tuple(lim)


def _status(group, kind):
    if group in ("bank_fy2000", "bank_full") or (group == "I" and kind == "bank"):
        return VS.RECONCILED_TO_TEMPLATE, ME.UNCONFIRMED_WORKBOOK_METHOD, CL.PUBLIC_FOIA_AGGREGATE
    if group == "B0" or (group == "I" and kind == "sba"):
        return VS.PRELIMINARY_METHODOLOGY, ME.CLIENT_DIRECTED_PRELIMINARY, CL.CLIENT_METHOD_DERIVED
    if group == "B1":
        return VS.PENDING_CLIENT_APPROVAL, ME.APPLICATION_RULE, CL.PUBLIC_FOIA_AGGREGATE
    return VS.PENDING_CLIENT_APPROVAL, ME.APPLICATION_RULE, CL.APPLICATION_DERIVED


def build_pack(rid, content, validations=(), saved_validations=()):
    """Benchmark Intelligence evidence pack. `validations`: clean-room benchmark records; `saved_validations`: the Saved
    Analysis's own clean-room records (they validate the full-history bank figure shown for reference)."""
    sha = sha256_of(content)
    c = content["criteria"]
    applicable = [v for v in validations if not VAL.applies_to(v, BI.SYSTEM, rid, sha, c["as_of"])]
    held = {}
    for v in applicable:
        for k, x in v.values:
            held[k] = (v, x)
    saved_held = {}
    for v in saved_validations:
        if not VAL.applies_to(v, "foia-saved-analysis", content["analysis"]["id"], content["analysis"]["payload_sha256"],
                              c["as_of"]):
            for k, x in v.values:
                saved_held[k] = (v, x)
    bank = (c["bank_name"], c["bank_state"], str(c.get("cert") or ""))
    recs = []
    for key, value, unit, label, path, group, kind, row in _specs(content):
        vs, me, cl = _status(group, kind)
        lim = list(_limits(group, kind, content, row))
        basis, numeric = "", False
        hit = held.get(key)
        if key == "bench_intel.bank_full.cum_def":
            hit = saved_held.get("bank.full.total.total.cum_def")
        if hit:
            vr, x = hit
            if VAL.agrees(vr, x, value):
                vs, numeric, basis = VS.INDEPENDENTLY_VERIFIED, True, f"validation record {vr.record_id}"
            else:
                vs, basis = VS.UNVERIFIED, f"validation record {vr.record_id} DISAGREES ({x!r})"
                lim.append("Independent validation disagrees with this value")
        if row is not None and group == "I":
            cl = CL.FOIA_AGGREGATE_CLIENT_CORRECTED if kind != "diff" else CL.APPLICATION_DERIVED
        recs.append(EvidenceRecord(
            evidence_id=make_id(rid, Kind.FACT, key), kind=Kind.FACT, metric_key=key, label=label, value=value,
            unit=unit, as_of=c["as_of"], bank=bank, calculation_version=BI.VERSION, validation_status=vs,
            methodology=me, classification=cl, source=Source(BI.SYSTEM, rid, sha, path, content["analysis"]["created"]),
            numeric_basis=basis, numerically_verified=numeric, definition_ref="webapp/benchmark_intel.py",
            limitations=tuple(lim)))
    header = {"source_id": rid, "system": BI.SYSTEM, "payload_sha256": sha, "calculation_version": BI.VERSION,
              "adapter_version": ADAPTER_VERSION, "as_of": c["as_of"], "bank": list(bank), "threshold": c["threshold"],
              "analysis_id": content["analysis"]["id"], "validation_records": [v.record_id for v in applicable]}
    return validate_pack(EvidencePack(header, tuple(recs)))


# --------------------------------------------------------------------------------------------- statements
def statements(pack, content):
    """Deterministic sentences, each verified by M2 against the Benchmark Intelligence pack."""
    w = PR._Writer(SimpleNamespace(pack=pack))
    g = lambda k: pack.by_key(k)                                                          # noqa: E731
    out = []
    bank, b0, b1 = g("bench_intel.bank_fy2000.cum_def"), g("bench_intel.B0.cum_def"), g("bench_intel.B1.cum_def")
    diff = lambda a, b: Calculation("difference", (a.evidence_id, b.evidence_id), a.value - b.value, "ratio", 4)  # noqa
    out.append(w.claim(T.OBSERVATION, f"Loans approved FY2000 onward: the bank's % Cum. Defaulted is "
                                      f"{pct(bank.value)}.", [bank]))
    out.append(w.claim(T.COMPARISON, f"For loans approved FY2000 onward, the bank's % Cum. Defaulted is "
                       f"{pct(bank.value)}; the current SBA benchmark (preliminary; SBA loans in the bank's "
                       f"industries, at the SBA's own industry mix) is {pct(b0.value)}, a difference of {pp(bank.value - b0.value)}.",
                       [bank, b0], calc=diff(bank, b0)))
    out.append(w.claim(T.COMPARISON, f"Investigative all-SBA comparison (every industry, SBA loans approved FY2000 "
                       f"onward): % Cum. Defaulted {pct(b1.value)}; the bank's FY2000+ figure differs from it by "
                       f"{pp(bank.value - b1.value)}.", [bank, b1], calc=diff(bank, b1)))
    if content["B2"]["computed"]:
        b2, cov = g("bench_intel.B2.cum_def"), g("bench_intel.B2.coverage")
        out.append(w.claim(T.COMPARISON, f"Investigative comparison weighted to the bank's industry mix: % Cum. "
                           f"Defaulted {pct(b2.value)}, covering {pct(cov.value)} of the bank's FY2000+ original "
                           f"amount; the bank's FY2000+ figure differs from it by {pp(bank.value - b2.value)}.",
                           [bank, b2, cov], calc=diff(bank, b2)))
        d = {k: g(f"bench_intel.decomposition.{k}.diff") for k in ("gap", "mix_total", "within_total",
                                                                   "sparse_adjustment")}
        out.append(w.claim(T.OBSERVATION, f"Investigative industry contribution analysis: the {pp(d['gap'].value)} "
                           f"difference between the bank and the current SBA benchmark comprises an industry-mix "
                           f"component of {pp(d['mix_total'].value)}, a within-industry component of "
                           f"{pp(d['within_total'].value)} and a sparse-industry adjustment of "
                           f"{pp(d['sparse_adjustment'].value)}.", list(d.values())))
        for x in [x for x in content["industries"] if x["bank_exposure"]][:3]:
            p = f"bench_intel.industry.{x['name']}."
            recs = [g(p + "bank_share"), g(p + "sba_share"), g(p + "mix.diff")] + \
                ([g(p + "within.diff")] if x["b2_weight"] else [])
            text = (f"{x['name']}: {pct(x['bank_share'], 1)} of the bank's FY2000+ original amount and "
                    f"{pct(x['sba_share'], 1)} of the SBA benchmark's; industry-mix component {pp(x['mix'])}"
                    + (f", within-industry component {pp(x['within'])}." if x["b2_weight"] else "."))
            out.append(w.claim(T.OBSERVATION, text, recs))
    out.append(w.claim(T.LIMITATION, "Neither investigative comparison is FalconBridge methodology; the comparison "
                       "weighted to the bank's industry mix adjusts for industry mix only, not for vintage, loan size "
                       "or term.", [g("bench_intel.B2.coverage")]))
    return out


# --------------------------------------------------------------------------------------------- model
_cache, _lock = OrderedDict(), threading.Lock()


def compute_cached(saved):
    key = (saved["id"], sha256_of(saved["payload"]))
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    content = BI.compute(saved)
    with _lock:
        _cache[key] = content
        while len(_cache) > 8:
            _cache.popitem(last=False)
    return content


def build(saved, app_data=None):
    app_data = str(app_data or settings.APP_DATA_DIR)
    content = compute_cached(saved)
    rid = BI.record_id(content)
    frozen = BI.load_frozen(app_data, saved["id"])
    vrs = CB.load_records(app_data, saved["id"])
    pack = build_pack(rid, content, vrs, CR.load_records(app_data, saved["id"]))
    recs = {r.metric_key: r for r in pack.records}
    applicable = pack.header["validation_records"]
    agree = sum(1 for r in pack.records if r.numerically_verified)
    disagree = [r.metric_key for r in pack.records if "DISAGREES" in r.numeric_basis]
    c, b2 = content["criteria"], content["B2"]

    def card(code):
        d = content[code]
        key = f"bench_intel.{code}.cum_def"
        r = recs.get(key)
        return {"code": code, "name": NAMES[code], "value": pct(d.get("cum_def")) if d.get("cum_def") is not None
                else "Not computed", "status": "Current benchmark (preliminary)" if code == "B0" else "Investigative",
                "approved": code == "B0", "loans": d.get("loans"), "numerator": usd(d.get("defaulted_orig")),
                "denominator": usd(d.get("orig")),
                "population": POPULATION[code].format(n=content["B0"]["industries"], loans=content["B1"]["loans"],
                                                      sparse=BI.SPARSE),
                "weighting": WEIGHTING[code], "evidence": r.evidence_id if r else "",
                "validation": PR.STATUS_TEXT[r.validation_status] if r else "",
                "verified": bool(r and r.numerically_verified),
                "diff": pp(content["bank_fy2000"]["cum_def"] - d["cum_def"]) if d.get("cum_def") is not None else ""}

    cards = [card("B0"), card("B1")]
    b2c = card("B2") if b2["computed"] else {"code": "B2", "name": NAMES["B2"], "value": "Not computed",
                                             "status": "Investigative", "approved": False,
                                             "population": POPULATION["B2"].format(sparse=BI.SPARSE),
                                             "weighting": WEIGHTING["B2"], "evidence": "", "validation": "",
                                             "verified": False, "diff": "", "loans": None, "numerator": "n/a",
                                             "denominator": "n/a"}
    if b2["computed"]:
        b2c.update(loans=None, numerator="weighted (see industries)",
                   denominator=f"{usd(b2['covered_bank_orig'])} of bank FY2000+ original amount (weights)")
    b2c["coverage"] = {"covered": pct(b2["coverage"], 2), "credible": pct(b2["credible_share"], 2),
                       "thin": pct(b2["thin_share"], 2), "sparse": pct(b2["sparse_share"], 2),
                       "excluded": b2["excluded"], "rule": f"Industries with fewer than {BI.SPARSE} SBA loans are "
                       f"excluded; B2 is computed only if the rest hold at least {pct(BI.COVERAGE_MIN, 0)} of the "
                       "bank's FY2000+ original amount. Nothing is substituted."}
    cards.append(b2c)
    inds = []
    for x in content["industries"]:
        if not (x["bank_exposure"] or abs(x.get("mix", 0)) >= 0.00005):
            continue
        flags = []
        if x["bank_small_sample"]:
            flags.append(f"small sample (< {BI.SMALL_BANK} bank loans)")
        if x["credibility"] != "credible":
            flags.append(f"{x['credibility']} (< {BI.CREDIBLE if x['credibility'] == 'thin' else BI.SPARSE} SBA loans)")
        if x["excluded_from_b2"]:
            flags.append("excluded from B2")
        if not x["bank_exposure"]:
            flags.append("no FY2000+ bank loans")
        inds.append({"name": x["name"], "naics": x["naics"], "bank_loans": x["bank_loans"],
                     "bank_share": pct(x["bank_share"], 1), "sba_share": pct(x["sba_share"], 1),
                     "bank_rate": pct(x["bank_cum_def"]), "sba_rate": pct(x["sba_cum_def"]),
                     "mix": pp(x.get("mix")) if "mix" in x else "n/a",
                     "within": pp(x["within"]) if x.get("b2_weight") else "–",
                     "total": pp(x.get("mix", 0) + x.get("within", 0)) if "mix" in x else "n/a",
                     "flags": flags, "evidence": recs.get(f"bench_intel.industry.{x['name']}.mix.diff").evidence_id
                     if f"bench_intel.industry.{x['name']}.mix.diff" in recs else ""})
    pop = content["population"]
    caveats = [
        "B1 and B2 are investigative comparisons prepared for discussion. They are not FalconBridge methodology and "
        "carry no verdict. Only B0, the benchmark used in the Executive Summary, is FalconBridge-directed; "
        "it is preliminary (its methodology has not been confirmed).",
        f"Reporting period: SBA FOIA release as of {PR._date_text(c['as_of'])}; SBA loans approved FY2000 onward; the "
        "bank's FY2000+ loans. Cumulative to the reporting date, not annual; original amounts, not balances.",
        f"Default definitions: {DEFAULT_DEF}. SBA-wide definition gaps: "
        + (", ".join(f"{k}: {v}" for k, v in pop["sba_definition_gaps"].items()) or "none") + ".",
        f"B0 uses {pop['industries_in_b0']} industries: every industry of the bank's eligible loans in any year, of "
        f"which {pop['industries_pre_fy2000_only']} have no FY2000+ bank loans (they enter B0 but carry no weight in "
        "B2).",
        "B2 adjusts for industry mix only. Loan age (vintage), loan size and term are not adjusted in this phase; "
        "differences in them remain inside both comparisons.",
        "Industries are matched on the published NAICS description (case-insensitive); renamed NAICS labels are not "
        "merged. The SBA population includes the bank's own loans.",
        "Industry contributions: industry-mix component = (bank share − SBA share) × (SBA industry rate − B0); "
        "within-industry component = bank share × (bank industry rate − SBA industry rate). They add up exactly to "
        "the bank − B0 difference (plus the sparse-industry adjustment). Arithmetic attribution, not a causal "
        "explanation.",
    ]
    for n in saved["payload"].get("source_notes") or []:            # documented source-data notes, as the dashboard
        caveats.append(f"Source-data note: {n.get('title', '')}. {n.get('short', '')}")
    levels = [("Bank", "loans approved FY2000 onward", content["bank_fy2000"]["cum_def"], True, False),
              ("B0", NAMES["B0"], content["B0"]["cum_def"], False, True),
              ("B1", NAMES["B1"], content["B1"]["cum_def"], False, False),
              ("B2", NAMES["B2"], b2["cum_def"], False, False)]
    top = max(x[2] for x in levels if x[2] is not None) or 1.0
    scale = [{"code": code, "label": label, "shown": pct(v) if v is not None else "Not computed",
              "width": f"{(v or 0) / top * 100:.1f}%", "bank": is_bank, "approved": approved}
             for code, label, v, is_bank, approved in levels]
    frozen_state = ("matches" if rid in frozen else ("differs" if frozen else "none"))
    m = PR.build(saved, validations=CR.load_records(app_data, saved["id"]))
    return {
        "bank": m["bank"], "as_of_text": m["as_of_text"], "threshold_text": m["threshold_text"],
        "analysis_id": saved["id"], "view_version": VIEW_VERSION, "record_id": rid,
        "bank_fy2000": {"value": pct(content["bank_fy2000"]["cum_def"]), "loans": content["bank_fy2000"]["loans"],
                        "numerator": usd(content["bank_fy2000"]["defaulted_orig"]),
                        "denominator": usd(content["bank_fy2000"]["orig"]),
                        "full": pct(content["bank_full"]["cum_def"]), "full_loans": content["bank_full"]["loans"]},
        "scale": scale, "cards": cards, "decomposition": {k: pp(v) for k, v in (content["decomposition"] or {}).items()},
        "industries": inds, "caveats": caveats, "statements": statements(pack, content),
        "reconciliation": {
            "b0_frozen": pct(saved["payload"]["benchmark"]["total"]["total"]["cum_def"], 6),
            "b0_shown": pct(content["B0"]["cum_def"], 6),
            "b0_identical": content["B0"]["cum_def"] == saved["payload"]["benchmark"]["total"]["total"]["cum_def"],
            "frozen": frozen_state, "validation_records": applicable, "agree": agree, "disagree": disagree,
            "count": len(pack.records)},
        "pack": pack,
    }


# --------------------------------------------------------------------------------------------- route
def register(app, owned_analysis, page, audit, switcher):
    @app.get("/analyses/{analysis_id}/benchmarks", response_class=HTMLResponse)
    def benchmarks_view(request: Request, analysis_id: str):
        if not ENABLED:
            raise HTTPException(404, "Not found.")
        user, a = owned_analysis(request, analysis_id)
        try:
            model = build(a)
        except BI.BenchmarkIntelError as e:
            raise HTTPException(409, f"Benchmark comparisons are not available for this analysis: {e}")
        audit(user, "benchmarks_view", analysis=a["id"], record=model["record_id"])
        return page(request, "benchmarks.html", m=model, analyses=switcher(user, a["id"]), section="benchmarks")
    return benchmarks_view
