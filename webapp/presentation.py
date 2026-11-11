"""Shared view model for the Executive / Lender view, the Analyst evidence view and presentation mode.

Data flow:  Saved Analysis (frozen) -> intel_adapter.extract -> EvidencePack -> build() -> one view model
            -> executive.html / evidence.html (and presentation(), an allow-listed subset for screen sharing).

Rules:
  * No calculation engine is called and no financial measure is computed here. Every number shown is the value of an
    evidence record; the only arithmetic is the M2 verifier's approved operations (difference, sum, ratio) on cited
    records, which the verifier recomputes itself.
  * Every displayed sentence is an M2 Claim, verified against the same pack. ACCEPTED claims are shown as findings;
    claims sent to analyst review are shown only in the analyst view as proposed interpretations; REJECTED claims are
    never shown (listed as withheld in the analyst view).
  * Wording is factual: a measure is "lower" or "higher" than the benchmark. Nothing here describes a lender as
    better, worse, strong, weak or eligible.
  * Both views read the same model, so they show identical values and evidence identifiers.
"""
import re
import threading
from collections import OrderedDict
from dataclasses import replace

from intelligence import verifier as VF
from intelligence.claims import Calculation, Claim, ClaimType as T, Outcome, ValueUse
from intelligence.evidence import Kind, ValidationStatus as VS

from . import insights as IN
from . import intel_adapter as IA

VIEW_VERSION = "views-1.0"
GROUP = "total"                    # executive findings describe all loans (the above/below split is analyst detail)

MEASURES = (
    ("cum_def", "% Cum. Defaulted", "share of original loan amount in loans that were later charged off"),
    ("cum_loss", "% Cum. Loss", "gross charge-offs as a share of original loan amount"),
    ("severity", "% Loss Severity", "gross charge-offs as a share of the original amount of the charged-off loans"),
)
DENOMINATOR = {
    "cum_def": "original gross approval amount of the population",
    "cum_loss": "original gross approval amount of the population",
    "severity": "original amount of the population's charged-off loans",
    "share": "the bank's total original gross approval amount",
    "avg": "number of loans", "purch_pct": "original gross approval amount of the population",
    "pif_pct": "original gross approval amount of the population",
}
STATUS_TEXT = {
    VS.INDEPENDENTLY_VERIFIED: "Independently verified",
    VS.RECONCILED_TO_TEMPLATE: "Reconciled to FalconBridge workbook formulas",
    VS.PRELIMINARY_METHODOLOGY: "Preliminary methodology",
    VS.PENDING_CLIENT_APPROVAL: "Application measure, pending FalconBridge approval",
    VS.UNVERIFIED: "Unverified",
}
BENCHMARK_NOTICE = ("The SBA benchmark is an industry-matched benchmark built from public SBA 7(a) FOIA data. Its "
                    "methodology is preliminary, pending FalconBridge's confirmation, and overall rates are not "
                    "adjusted for differences in portfolio age, loan size or industry mix.")
NOT_EVIDENCED = [
    ("Current outstanding balances", "SBA FOIA data publishes original approval amounts and gross charge-offs only."),
    ("Charge-offs by calendar year, loan status and static pool timing", "Shown on the dashboard (Methodology & "
     "Reports: Supplementary analysis; Portfolio Analysis: static pool); not yet part of the evidence layer."),
    ("Independent validation record for this analysis", "None attached; values are reconciled to FalconBridge's "
     "workbook formulas, not independently re-verified (planned: clean-room validation producer)."),
    ("Mix-adjusted benchmark comparison", "Not implemented; overall rates are not adjusted for vintage, size, term or "
     "industry mix."),
    ("Approved evaluation criteria", "FalconBridge has not approved criteria for judging a lender as better or worse; "
     "the views state measured differences only."),
]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def pct(v):
    return f"{v * 100:.2f}%"


def pp(v):
    return f"{abs(v) * 100:.2f} percentage points"


def count(n, word):
    return IN.plural(n, word)


class _Pack:
    def __init__(self, pack):
        self.pack = pack
        self.by_key = {(r.metric_key, r.kind): r for r in pack.records}
        self.by_id = {r.evidence_id: r for r in pack.records}

    def get(self, key, kind=Kind.FACT):
        r = self.by_key.get((key, kind))
        return r if r is not None and (kind is not Kind.FACT or _num(r.value)) else None


def population(key):
    """Plain-language population and period of an evidence record (from its canonical metric key)."""
    parts = key.split(".")
    if parts[0] == "bank" and len(parts) >= 4:
        h, g = parts[1], parts[2]
        base = {"full": "All of the bank's loans in SBA FOIA data", "fy2000": "Bank loans approved FY2000 onward"}[h]
        grp = {"total": "", "above": ", above the loan size threshold", "below": ", at or below the threshold"}[g]
        m = re.match(r"(vintage|industry|subprogram)\.(.+)\.[a-z_]+$", ".".join(parts[3:]))
        if m:
            base += {"vintage": f", first disbursed in {m.group(2)}", "industry": f", industry: {m.group(2)}",
                     "subprogram": f", subprogram: {m.group(2)}"}[m.group(1)]
        return base + grp
    if parts[0] == "benchmark":
        g = parts[1]
        m = re.match(r"(vintage|industry)\.(.+)\.[a-z_]+$", ".".join(parts[2:]))
        extra = ""
        if m:
            extra = f", first disbursed in {m.group(2)}" if m.group(1) == "vintage" else f", industry: {m.group(2)}"
        return f"SBA 7(a) benchmark ({g}), industry-matched, loans approved FY2000 onward" + extra
    if parts[0] == "compare":
        return "Bank loans approved FY2000 onward minus the SBA benchmark"
    if parts[0] == "seasoning":
        return "Share of original amount by year of first disbursement"
    return "Analysis-level"


def describe(r):
    """Analyst-facing description of one evidence record (all fields come from the record)."""
    metric = r.metric_key.rsplit(".", 1)[-1]
    if r.unit == "ratio":
        shown = pct(r.value) if not r.metric_key.endswith(".diff") else \
            f"{r.value * 100:+.2f} pp"
    elif r.unit == "usd":
        shown = f"${r.value:,.0f}"
    elif r.unit == "months":
        shown = f"{r.value:.1f} months"
    elif r.unit == "flag":
        shown = "Yes" if r.value else "No"
    elif r.unit == "count" and _num(r.value):
        shown = f"{r.value:,}"
    else:
        shown = str(r.value)
    return {
        "id": r.evidence_id, "kind": r.kind.value, "key": r.metric_key, "label": r.label, "value": r.value,
        "shown": shown, "unit": r.unit, "as_of": r.as_of, "population": population(r.metric_key),
        "denominator": DENOMINATOR.get(metric, "") if r.unit == "ratio" and not r.metric_key.endswith(".diff") else "",
        "status": r.validation_status.value, "status_text": STATUS_TEXT[r.validation_status],
        "numerically_verified": r.numerically_verified, "methodology": r.methodology.value,
        "classification": r.classification.value, "source_path": r.source.payload_path, "definition": r.definition_ref,
        "basis": r.numeric_basis, "rule": r.rule, "derived_from": list(r.derived_from),
        "limitations": list(r.limitations), "calculation_version": r.calculation_version,
    }


class _Writer:
    """Builds claims from evidence and verifies each one against the pack."""
    def __init__(self, px):
        self.px, self.h = px, px.pack.header
        self.n = 0

    def claim(self, ctype, text, records, values=None, calc=None):
        self.n += 1
        recs = list({r.evidence_id: r for r in records if r is not None}.values())
        vals = tuple(ValueUse(r.evidence_id, r.value, r.unit) for r in recs if _num(r.value)) \
            if values is None else values
        base = Claim(claim_id=f"CL-view-{self.n:02d}", claim_type=ctype, source_id=self.h["source_id"],
                     bank=tuple(self.h["bank"]), as_of=self.h["as_of"], text=text,
                     evidence_refs=tuple(r.evidence_id for r in recs), values=vals, calculation=calc)
        c = replace(base, validation_status=VF.effective_status(recs), methodology=VF.effective_methodology(recs),
                    caveats=tuple(sorted(VF.required_caveats(self.px.pack, recs), key=lambda x: x.value)))
        if ctype is T.HYPOTHESIS:
            c = replace(c, validation_status=VS.UNVERIFIED)
        v = VF.verify_claim(self.px.pack, c)
        return {"claim_id": c.claim_id, "type": ctype.value, "text": text, "evidence": [r.evidence_id for r in recs],
                "outcome": v.outcome.value, "explanation": v.explanation, "findings": [f.message for f in v.findings],
                "status": v.effective_validation_status.value,
                "status_text": STATUS_TEXT[v.effective_validation_status],
                "methodology": v.effective_methodology.value,
                "caveats": [x.value for x in v.required_caveats]}


def _diff_calc(a, b):
    return Calculation("difference", (a.evidence_id, b.evidence_id), a.value - b.value, a.unit)


_cache, _lock = OrderedDict(), threading.Lock()


def pack_for(saved, validations=()):
    """Evidence pack of a Saved Analysis, cached by (id, payload fingerprint, validation records). A Saved Analysis is
    never modified, so a cached pack can only be reused for the identical frozen payload."""
    key = (saved["id"], IA.payload_fingerprint(saved), tuple(getattr(v, "record_id", "") for v in validations))
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    pack = IA.extract(saved, validations)
    with _lock:
        _cache[key] = pack
        while len(_cache) > 8:
            _cache.popitem(last=False)
    return pack


def build(saved, validations=(), lookup=()):
    """Saved Analysis -> view model shared by the executive, analyst and presentation views. `lookup`: evidence ids to
    show individually in the analyst view (e.g. from a claims assessment)."""
    pack = pack_for(saved, validations)
    px = _Pack(pack)
    w = _Writer(px)
    h = pack.header
    c = saved["payload"]["criteria"]
    g = GROUP
    get = px.get
    tot = lambda m, hist="full": get(f"bank.{hist}.{g}.total.{m}")      # noqa: E731
    loans, orig = tot("loans"), tot("orig")
    vint = sorted((r for r in pack.records if r.kind is Kind.FACT and
                   re.fullmatch(rf"bank\.full\.{g}\.vintage\.\d{{4}}\.loans", r.metric_key) and r.value),
                  key=lambda r: r.metric_key)
    findings, compare_rows, limits, proposals = [], [], [], []

    # -------- overview tiles (each value is one evidence record)
    tiles = []
    for key, label, fmt in ((f"bank.full.{g}.total.loans", "SBA 7(a) loans", lambda v: f"{v:,}"),
                            (f"bank.full.{g}.total.orig", "Original approval amount", IN.money),
                            (f"bank.full.{g}.total.active", "Loans estimated outstanding", lambda v: f"{v:,}"),
                            (f"bank.full.{g}.total.chgoff", "Loans charged off", lambda v: f"{v:,}"),
                            (f"bank.full.{g}.total.unguar", "Original unguaranteed amount", IN.money),
                            (f"bank.full.{g}.total.cum_def", "% Cum. Defaulted (full history)", pct)):
        r = get(key)
        if r is not None:
            tiles.append({"label": label, "shown": fmt(r.value), "evidence": r.evidence_id})

    # -------- F1 portfolio
    if loans is not None and loans.value:
        first, last = (vint[0], vint[-1]) if vint else (None, None)
        span = ""
        if first is not None:
            fy, ly = first.metric_key.split(".")[4], last.metric_key.split(".")[4]
            span = f", first disbursed in {fy}" if fy == ly else f", first disbursed between {fy} and {ly}"
        text = (f"{h['bank'][0]} originated {count(loans.value, 'SBA 7(a) loan')} with {IN.money(orig.value)} of "
                f"original approval amounts{span}.")
        findings.append(dict(w.claim(T.OBSERVATION, text, [loans, orig, first, last],
                                     values=tuple(ValueUse(r.evidence_id, r.value, r.unit) for r in (loans, orig))),
                             title="Portfolio", ref="F1"))
    else:
        findings.append(dict(title="Portfolio", ref="F1", outcome="NONE", evidence=[], text=f"{h['bank'][0]} has no "
                             "eligible SBA 7(a) loans in this release.", status_text="", caveats=[]))

    # -------- F2 default history (full history)
    cd, ch = tot("cum_def"), tot("chgoff")
    if cd is not None and ch is not None and loans is not None:
        text = (f"{ch.value:,} of {loans.value:,} loans were charged off; together they represent {pct(cd.value)} "
                "of the original loan amount over the bank's full history (% Cum. Defaulted)." if ch.value else
                f"None of the bank's {count(loans.value, 'loan')} has been charged off; % Cum. Defaulted is "
                f"{pct(cd.value)} over the bank's full history.")
        findings.append(dict(w.claim(T.OBSERVATION, text, [ch, loans, cd]), title="Default history", ref="F2"))

    # -------- F3-F4 benchmark comparisons (FY2000+ basis, as in the workbook comparison)
    can_compare = get(f"limitation.{g}.comparison", Kind.DERIVED)
    can_compare = bool(can_compare and can_compare.value)
    for m, label, plain in MEASURES:
        b, s = get(f"bank.fy2000.{g}.total.{m}"), get(f"benchmark.{g}.total.{m}")
        if not (can_compare and b is not None and s is not None):
            continue
        d = b.value - s.value
        word = "lower than" if d < 0 else "higher than" if d > 0 else None
        text = (f"For loans approved FY2000 onward, the bank's {label} is {pct(b.value)}, "
                + (f"{pp(d)} {word} the SBA benchmark's {pct(s.value)}." if word else
                   f"the same as the SBA benchmark's {pct(s.value)}."))
        res = w.claim(T.COMPARISON, text, [b, s], calc=_diff_calc(b, s))
        row = dict(res, measure=m, label=label, plain=plain, bank=pct(b.value), sba=pct(s.value),
                   diff=(f"{d * 100:+.2f} pp"), direction="Lower" if d < 0 else "Higher" if d > 0 else "Same")
        compare_rows.append(row)
        if m in ("cum_def", "cum_loss"):
            findings.append(dict(res, title=f"{label} vs SBA benchmark", ref=f"F{len(findings) + 1}"))

    # -------- F5 portfolio age (the most important context for any overall comparison)
    sz = saved["payload"].get("seasoning") or {}
    recent = None
    if sz.get("recent_from") and sz.get("recent_to"):
        span = f"{sz['recent_from']}-{sz['recent_to']}"
        rb, rs = (get(f"seasoning.{span}.{side}_share", Kind.DERIVED) for side in ("bank", "sba"))
        if rb is not None and rs is not None:
            recent = (rb, rs, span)
            text = (f"{pct(rb.value)} of the bank's original loan amount was first disbursed in "
                    f"{span.replace('-', ' to ')}, compared with {pct(rs.value)} for the SBA benchmark. Recently "
                    "disbursed loans have had less time to default, so overall rates are not directly comparable.")
            findings.append(dict(w.claim(T.LIMITATION, text, [rb, rs]), title="Portfolio age",
                                 ref=f"F{len(findings) + 1}"))

    # -------- material limitations (each verified; plain text for the executive view)
    fyl = get(f"bank.fy2000.{g}.total.loans")
    if fyl is not None and loans is not None and loans.value > fyl.value and fyl.value:
        text = (f"The benchmark comparison uses {count(fyl.value, 'loan')} approved FY2000 onward; "
                f"{count(loans.value - fyl.value, 'earlier loan')} "
                f"{'is' if loans.value - fyl.value == 1 else 'are'} outside the benchmark period.")
        limits.append(w.claim(T.LIMITATION, text, [fyl, loans], values=(ValueUse(fyl.evidence_id, fyl.value, "count"),),
                              calc=_diff_calc(loans, fyl)))
    if not can_compare:
        limits.append(w.claim(T.LIMITATION, "No benchmark comparison is shown: the bank has no loans approved FY2000 "
                              "onward, the period the SBA benchmark covers.", [get(f"limitation.{g}.comparison",
                                                                                  Kind.DERIVED)]))
    for flag, text in (("limited", f"The benchmark comparison rests on fewer than {IN.MIN_LOANS} bank loans; "
                                   "interpret with caution."),
                       ("limited_full", f"The bank has fewer than {IN.MIN_LOANS} loans in total; figures may not be "
                                        "representative.")):
        r = get(f"limitation.{g}.{flag}", Kind.DERIVED)
        if r is not None and r.value:
            limits.append(w.claim(T.LIMITATION, text, [r]))
    py = get("limitation.partial_year", Kind.DERIVED)
    if py is not None:
        limits.append(w.claim(T.LIMITATION, f"{py.value} is a partial year: it holds only loans first disbursed "
                              "through the release date.", [py]))
    limits.append({"claim_id": "", "type": "LIMITATION", "outcome": "ACCEPTED", "evidence": [], "status_text": "",
                   "text": "Amounts are original approval amounts and gross charge-offs; current balances are not "
                   "published by SBA.", "caveats": [], "findings": []})
    vr = h.get("validations_applied") or []
    limits.append({"claim_id": "", "type": "LIMITATION", "outcome": "ACCEPTED", "evidence": [], "status_text": "",
                   "findings": [], "caveats": [],
                   "text": _validation_text(pack, h) if vr else
                   "Figures reproduce FalconBridge's workbook formulas; no independent validation record is "
                   "attached to this analysis yet."})

    # -------- proposed interpretation (analyst view only; never in presentation mode)
    cmp_cd = next((r for r in compare_rows if r["measure"] == "cum_def"), None)
    if recent and cmp_cd and cmp_cd["direction"] == "Lower":
        rb, rs, span = recent
        b = get(f"bank.fy2000.{g}.total.cum_def")
        if rb.value > rs.value:
            proposals.append(w.claim(T.HYPOTHESIS, "The lower FY2000+ % Cum. Defaulted may partly reflect a younger "
                                     f"portfolio: {pct(rb.value)} of the bank's original loan amount was first "
                                     f"disbursed in {span.replace('-', ' to ')}.", [b, rb],
                                     values=(ValueUse(rb.evidence_id, rb.value, rb.unit),)))

    # -------- industry concentration (overview line)
    inds = sorted((r for r in pack.records if r.kind is Kind.FACT and
                   re.fullmatch(rf"bank\.full\.{g}\.industry\..+\.share", r.metric_key)), key=lambda r: -r.value)
    concentration = None
    if inds:
        top = inds[0]
        name = IN.industry_name(top.metric_key[len(f"bank.full.{g}.industry."):-len(".share")])
        concentration = dict(w.claim(T.OBSERVATION, f"The largest industry, {name}, accounts for {pct(top.value)} of "
                                     "the bank's original approval amount.", [top]), industry=name, share=pct(top.value))

    # -------- evidence register for the analyst view
    shown_ids = {e for f in findings + limits + proposals + compare_rows + ([concentration] if concentration else [])
                 for e in f.get("evidence", [])} | {t["evidence"] for t in tiles}
    cited = {i for i in shown_ids if i in px.by_id}
    for i in list(cited):
        cited |= set(px.by_id[i].derived_from)
    register = [describe(px.by_id[i]) for i in sorted(cited, key=lambda i: px.by_id[i].metric_key)]
    vintages = []
    for r in vint:
        y = r.metric_key.split(".")[4]
        row = {"year": y, "partial": bool(py and str(py.value) == y)}
        for m in ("loans", "orig", "chgoff", "cum_def", "cum_loss", "severity"):
            x = get(f"bank.full.{g}.vintage.{y}.{m}")
            row[m] = describe(x) if x is not None else None
        x = get(f"benchmark.{g}.vintage.{y}.cum_def")
        row["sba_cum_def"] = describe(x) if x is not None else None
        small = any("Small sample" in s for s in px.by_key[(r.metric_key, Kind.FACT)].limitations)
        row["small"] = small
        vintages.append(row)
    groups = []
    for gg in ("total", "above", "below"):
        rows = []
        for m in ("loans", "orig", "chgoff", "cum_def", "cum_loss", "severity"):
            b = get(f"bank.fy2000.{gg}.total.{m}")
            s = get(f"benchmark.{gg}.total.{m}")
            f_ = get(f"bank.full.{gg}.total.{m}")
            rows.append({"measure": m, "full": describe(f_) if f_ else None, "bank": describe(b) if b else None,
                         "sba": describe(s) if s else None})
        groups.append({"group": gg, "rows": rows})
    status_counts = {}
    for r in pack.records:
        status_counts[STATUS_TEXT[r.validation_status]] = status_counts.get(STATUS_TEXT[r.validation_status], 0) + 1

    shown = [f for f in findings if f.get("outcome") in ("ACCEPTED", "NONE")]
    withheld = [x for x in findings + limits + compare_rows + proposals if x.get("outcome") == "REJECTED"]
    th = c.get("threshold")
    return {
        "view_version": VIEW_VERSION,
        "analysis_id": saved["id"], "created": saved["created"],
        "bank": {"name": h["bank"][0], "state": h["bank"][1], "cert": h["bank"][2]},
        "as_of": h["as_of"], "as_of_text": _date_text(h["as_of"]), "threshold": th, "threshold_text": f"${th:,}" if _num(th) else "",
        "headline": _headline(shown, compare_rows, loans),
        "tiles": tiles,
        "findings": shown[:5],
        "compare": [r for r in compare_rows if r["outcome"] == "ACCEPTED"],
        "limitations": [x for x in limits if x["outcome"] == "ACCEPTED"],
        "concentration": concentration if concentration and concentration["outcome"] == "ACCEPTED" else None,
        "proposals": proposals,
        "withheld": withheld,
        "notice": BENCHMARK_NOTICE,
        "register": register,
        "register_index": {d["id"]: d for d in register},
        "vintages": vintages,
        "groups": groups,
        "not_evidenced": _not_evidenced(h),
        "provenance": {k: h.get(k) for k in ("source_id", "recorded_at", "payload_sha256", "adapter_version",
                                             "calculation_version", "workbook_corrections_D1", "period",
                                             "validations_applied", "validations_ignored", "release_files",
                                             "sections_missing", "not_applicable_values")},
        "pack_limitations": list(h.get("limitations", [])),
        "pack_fingerprint": pack.fingerprint(),
        "record_count": len(pack.records),
        "status_counts": sorted(status_counts.items()),
        "engine_scope": h.get("engine_validation_scope", {}),
        "lookup": [describe(px.by_id[i]) for i in lookup if i in px.by_id],
        "lookup_missing": [i for i in lookup if i not in px.by_id],
    }


def _not_evidenced(h):
    if not h.get("validations_applied"):
        return NOT_EVIDENCED
    return [x if not x[0].startswith("Independent validation record") else
            ("Independent validation beyond the bank's totals and vintages", "The attached validation record "
             "recomputes the bank's full-history and FY2000+ totals and its vintages only; industry, subprogram and "
             "SBA benchmark figures are not independently recomputed.") for x in NOT_EVIDENCED]


def _validation_text(pack, h):
    """Numerical validation stated for what it covers: bank figures only, numbers only, never methodology."""
    contradicted = h.get("values_contradicted_by_validation") or []
    if contradicted:
        return (f"An independent recomputation from SBA FOIA data disagrees with {len(contradicted)} bank figure(s); "
                "they are marked unverified pending review.")
    return ("The bank's own figures were recomputed independently from the SBA FOIA data and agree. This confirms the "
            "numbers only: FalconBridge has not approved the methodology, and the SBA benchmark was not "
            "independently recomputed.")


def _date_text(iso):
    from datetime import date
    d = date.fromisoformat(iso)
    return f"{d:%B} {d.day}, {d.year}"


def _headline(findings, compare_rows, loans):
    """The five-second message: the % Cum. Defaulted comparison with the benchmark, stated as a fact (values are the
    verified comparison's own displayed values), or the portfolio size if no comparison exists."""
    cd = next((r for r in compare_rows if r["measure"] == "cum_def" and r["outcome"] == "ACCEPTED"), None)
    if cd:
        word = {"Lower": "lower than", "Higher": "higher than", "Same": "the same as"}[cd["direction"]]
        return {"bank": cd["bank"], "sba": cd["sba"], "direction": cd["direction"], "diff": cd["diff"],
                "kicker": "Share of original loan amount that defaulted (% Cum. Defaulted), loans approved FY2000 onward",
                "text": f"The bank's defaulted share is {word} the SBA benchmark.", "evidence": cd["evidence"]}
    f1 = findings[0] if findings else None
    return {"bank": None, "sba": None, "direction": "", "diff": "",
            "kicker": "Portfolio", "big": count(loans.value, "loan") if loans is not None else "No loans",
            "text": "No benchmark comparison is available for this lender.",
            "evidence": f1["evidence"] if f1 else []}


# ---------------------------------------------------------------------------------------------- presentation mode
# Explicit allow-list: presentation mode is BUILT from these fields only (nothing is hidden with CSS). Analyst
# commentary, proposed interpretations, withheld claims, verifier findings, evidence identifiers, internal ids,
# provenance and source paths are never copied.
PRESENT_FIELDS = {
    "top": ("bank", "as_of", "as_of_text", "notice"),
    "headline": ("bank", "sba", "big", "kicker", "text", "direction", "diff"),
    "tile": ("label", "shown"),
    "finding": ("ref", "title", "text"),
    "compare": ("label", "plain", "bank", "sba", "diff", "direction"),
    "limitation": ("text",),
    "concentration": ("industry", "share"),
}


def presentation(model):
    pick = lambda d, kind: {k: d[k] for k in PRESENT_FIELDS[kind] if k in d}   # noqa: E731
    out = {k: model[k] for k in PRESENT_FIELDS["top"]}
    out["bank"] = {"name": model["bank"]["name"], "state": model["bank"]["state"]}
    out["headline"] = pick(model["headline"], "headline")
    out["tiles"] = [pick(t, "tile") for t in model["tiles"]]
    out["findings"] = [pick(f, "finding") for f in model["findings"]]
    out["compare"] = [pick(r, "compare") for r in model["compare"]]
    out["limitations"] = [pick(x, "limitation") for x in model["limitations"]]
    out["concentration"] = ({k: model["concentration"][k] for k in PRESENT_FIELDS["concentration"]}
                            if model.get("concentration") else None)
    return out
