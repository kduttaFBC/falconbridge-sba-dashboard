"""Lender claims assessment (local prototype, deterministic, offline).

An analyst types or selects a lender performance claim. The assessor:
  1. parses it against a small fixed vocabulary (metric, population, period, comparison, target);
  2. asks for clarification when the metric, population or period is missing or ambiguous, or when the claim uses
     a vague quantifier ("minimal", "low") that has no defined threshold;
  3. returns CANNOT_ESTABLISH for evaluative claims ("better", "outperforms", "strong") because FalconBridge has not
     approved evaluation criteria, and for measures the SBA FOIA evidence does not contain;
  4. otherwise compares the claim with ONE evidence record (or the bank-minus-benchmark difference) and returns
     SUPPORTED or CONTRADICTED. That is numerical support only; methodology approval is reported separately.

Nothing is estimated or invented: every value comes from the Saved Analysis's evidence pack, the restated evidence
sentence is verified by the M2 verifier, and the same inputs always give the same assessment id and result.
"""
import hashlib
import json
import re

from intelligence import verifier as VF
from intelligence.claims import Calculation, Claim, ClaimType as T, ValueUse
from intelligence.evidence import Kind, Methodology as ME

from . import presentation as PR

ASSESSOR_VERSION = "claims-assessment-1.0"
SUPPORTED, CONTRADICTED, CANNOT, CLARIFY = "SUPPORTED", "CONTRADICTED", "CANNOT_ESTABLISH", "NEEDS_CLARIFICATION"
OUTCOME_TEXT = {SUPPORTED: "Supported by the numbers", CONTRADICTED: "Contradicted by the numbers",
                CANNOT: "Cannot be established", CLARIFY: "Needs clarification"}

METRICS = {
    "cum_def": ("% Cum. Defaulted", "ratio", "original amount of loans later charged off / original amount"),
    "cum_loss": ("% Cum. Loss", "ratio", "gross charge-offs / original amount"),
    "severity": ("% Loss Severity", "ratio", "gross charge-offs / original amount of charged-off loans"),
    "chgoff": ("Number of loans charged off", "count", "count of loans with SBA status charged off"),
    "chgoff_amt": ("Gross charge-off amount", "usd", "sum of gross charge-off amounts (dollars)"),
}
POPULATIONS = {"full": "All of the bank's loans in SBA FOIA data (full history)",
               "fy2000": "Bank loans approved FY2000 onward (the SBA benchmark's period)"}
COMPARATORS = {"lt": "below", "le": "at or below", "gt": "above", "ge": "at or above"}

PRESETS = [
    ("historical-default-2pct", "Our historical default rate is below 2%."),
    ("better-than-benchmark", "Our SBA portfolio performs better than the industry benchmark."),
    ("minimal-chargeoffs", "Our historical chargeoffs are minimal."),
    ("cumdef-below-2pct-defined", "Our % Cum. Defaulted over our full history is below 2% as of June 30, 2026."),
    ("cumdef-lower-than-benchmark", "Our % Cum. Defaulted for loans approved FY2000 onward is lower than the SBA "
                                    "benchmark as of June 30, 2026."),
    ("cumloss-below-0.5pct", "Our % Cum. Loss over our full history is below 0.5% as of June 30, 2026."),
]
I = re.IGNORECASE
EVALUATIVE = re.compile(r"\b(better|worse|outperform\w*|underperform\w*|superior|inferior|strong(?:er|est)?|"
                        r"weak(?:er|est)?|healthier|safer|riskier|best|worst|excellent|exceptional|top[- ]tier|"
                        r"high[- ]quality|low[- ]risk|eligible|qualif\w*)\b", I)
VAGUE = re.compile(r"\b(minimal|negligible|low|very low|minor|few|rare(?:ly)?|small|insignificant|modest|limited|"
                   r"high|elevated|significant|substantial|nominal|immaterial)\b", I)
UNAVAILABLE = re.compile(r"\b(delinquen\w*|past[- ]due|net charge[- ]?offs?|recover(?:y|ies)|balances?|"
                         r"outstanding principal|return on|roa|roe|yield|interest|non[- ]?accrual)\b", I)
PEERS = re.compile(r"\b(peers?|competitors?|other (?:banks|lenders)|similar (?:banks|lenders))\b", I)
BENCHMARK = re.compile(r"\b(benchmark|industry average|industry|sba average|sba 7\(a\) average|national average)\b", I)
METRIC_EXACT = [
    ("cum_def", re.compile(r"%?\s*cum(?:ulative|\.)?\s*default(?:ed)?(?:\s+rate)?", I)),
    ("cum_loss", re.compile(r"%?\s*cum(?:ulative|\.)?\s*loss(?:\s+rate)?", I)),
    ("severity", re.compile(r"(?:loss\s+)?severity", I)),
    ("chgoff", re.compile(r"(?:number|count) of (?:loans )?charged[- ]?off(?: loans)?|charged[- ]?off loans|"
                          r"(?:number|count) of charge[- ]?offs", I)),
    ("chgoff_amt", re.compile(r"(?:gross )?charge[- ]?off (?:amount|dollars)|dollars? (?:of )?charged[- ]?off|"
                              r"gross charge[- ]?offs", I)),
]
METRIC_AMBIGUOUS = [
    (re.compile(r"\bdefault(?:s|ed)?(?:\s+rate)?\b", I), ("cum_def",),
     "\"default rate\" can be measured several ways; FalconBridge's defined measure is % Cum. Defaulted"),
    (re.compile(r"\bcharge[- ]?offs?\b|\bchargeoffs?\b|\bcharged[- ]?off\b", I), ("chgoff", "chgoff_amt", "cum_loss"),
     "\"charge-offs\" can mean a number of loans, a dollar amount or a rate"),
    (re.compile(r"\bloss(?:es)?(?:\s+rate)?\b", I), ("cum_loss", "severity"),
     "\"loss\" can mean % Cum. Loss or % Loss Severity"),
    (re.compile(r"\bperform(?:s|ance|ing)?\b|\bcredit quality\b|\bportfolio quality\b", I), tuple(METRICS),
     "\"performance\" is not one measure"),
]
FULL = re.compile(r"\b(historical(?:ly)?|all[- ]time|full history|since inception|lifetime|all (?:of )?(?:our|its|the) "
                  r"loans|over (?:our|its|the bank's) full history)\b", I)
FY2000 = re.compile(r"\b(fy\s?2000\+?|fy\s?2000 onward|approved (?:in )?fy\s?2000|since 2000)\b", I)
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
DATE = re.compile(rf"\b(?:as of|through|at)\s+(?:({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})|(\d{{4}})-(\d{{2}})-(\d{{2}}))"
                  r"|\b(?:q([1-4])\s*(\d{4})|(\d{4})\s*q([1-4]))\b", I)
QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
TARGET_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b)", I)
TARGET_USD = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(million|billion|thousand|m|bn|k)?\b", I)
TARGET_NUM = re.compile(r"(?<![\w.$])(\d[\d,]*)(?![\w.%])")
COMP = [("le", re.compile(r"\b(at most|no more than|not more than|does not exceed|do not exceed|<=)", I)),
        ("ge", re.compile(r"\b(at least|no less than|not less than|>=)", I)),
        ("lt", re.compile(r"\b(below|under|less than|lower than|fewer than|beneath|<)", I)),
        ("gt", re.compile(r"\b(above|over|more than|greater than|higher than|exceeds?|in excess of|>)", I))]
SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9}


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())[:500]


def parse(text):
    """Text -> what the claim states (None for anything it does not state). No defaults are filled in."""
    t = _norm(text)
    out = {"text": t, "metric": None, "metric_candidates": [], "metric_note": "", "population": None, "period": None,
           "comparator": None, "target": None, "target_unit": None, "against": None, "evaluative": None,
           "vague": None, "unavailable": None}
    if not t:
        return out
    stripped = re.sub(r"\b7\s?\(a\)", " ", t)
    m = EVALUATIVE.search(stripped)
    out["evaluative"] = m.group(0) if m else None
    m = UNAVAILABLE.search(stripped)
    out["unavailable"] = m.group(0) if m else None
    for key, rx in METRIC_EXACT:
        if rx.search(stripped):
            out["metric"] = key
            break
    if out["metric"] is None:
        for rx, cands, note in METRIC_AMBIGUOUS:
            if rx.search(stripped):
                out["metric_candidates"], out["metric_note"] = list(cands), note
                break
    if FY2000.search(stripped):
        out["population"] = "fy2000"
    elif FULL.search(stripped):
        out["population"] = "full"
    d = DATE.search(stripped)
    if d:
        if d.group(1):
            mon = MONTHS.split("|").index(d.group(1).lower()) + 1
            out["period"] = f"{int(d.group(3)):04d}-{mon:02d}-{int(d.group(2)):02d}"
        elif d.group(4):
            out["period"] = f"{d.group(4)}-{d.group(5)}-{d.group(6)}"
        else:
            q, y = (d.group(7), d.group(8)) if d.group(7) else (d.group(10), d.group(9))
            out["period"] = f"{y}-{QUARTER_END[int(q)]}"
        stripped = stripped.replace(d.group(0), " ")
    for key, rx in COMP:
        if rx.search(stripped):
            out["comparator"] = key
            break
    if PEERS.search(stripped):
        out["against"] = "peers"
    elif BENCHMARK.search(stripped):
        out["against"] = "benchmark"
    m = TARGET_PCT.search(stripped)
    if m:
        out["target"], out["target_unit"] = float(m.group(1)) / 100, "ratio"
    else:
        m = TARGET_USD.search(stripped)
        if m:
            out["target"] = float(m.group(1).replace(",", "")) * SCALE.get((m.group(2) or "").lower(), 1.0)
            out["target_unit"] = "usd"
        else:
            nums = [int(x.replace(",", "")) for x in TARGET_NUM.findall(stripped)
                    if not re.fullmatch(r"(19|20)\d{2}", x.replace(",", ""))]
            if nums:
                out["target"], out["target_unit"] = float(nums[0]), "count"
    if out["metric"] is None and out["metric_candidates"] and out["target_unit"]:
        fits = [k for k in out["metric_candidates"] if METRICS[k][1] == out["target_unit"]]
        if len(fits) == 1 and out["metric_candidates"] != ["cum_def"]:
            out["metric"] = fits[0]               # "charge-offs below $5 million" can only be the dollar amount
    v = VAGUE.search(stripped)
    if v and out["target"] is None and out["against"] is None:
        out["vague"] = v.group(0)
    return out


def _spec(text, choices):
    """Parsed claim, with the analyst's explicit clarification choices applied (choices win; nothing is guessed)."""
    s = parse(text)
    for k in ("metric", "population", "period"):
        v = (choices or {}).get(k)
        if v:
            s[k] = v
            s.setdefault("chosen", []).append(k)
    return s


def _assessment_id(spec, pack):
    key = {k: spec.get(k) for k in ("text", "metric", "population", "period", "comparator", "target", "target_unit",
                                    "against")}
    blob = json.dumps({"spec": key, "pack": pack.fingerprint(), "v": ASSESSOR_VERSION}, sort_keys=True)
    return "AS-" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def _result(outcome, spec, pack, reasons, **kw):
    out = {"assessment_id": _assessment_id(spec, pack), "assessor_version": ASSESSOR_VERSION, "outcome": outcome,
           "outcome_text": OUTCOME_TEXT[outcome], "spec": spec, "reasons": reasons, "clarify": [], "evidence": [],
           "statement": None, "methodology": None, "notes": [], "interpreted": "", "context": []}
    out.update(kw)
    return out


def _fmt(value, unit):
    return PR.pct(value) if unit == "ratio" else f"${value:,.0f}" if unit == "usd" else f"{value:,.0f}"


def _metric_choices():
    return [{"value": k, "label": f"{v[0]} ({v[2]})"} for k, v in METRICS.items()]


def assess(pack, text, choices=None):
    """Assess one claim against one evidence pack. Deterministic; never raises on user input."""
    px = PR._Pack(pack)
    h = pack.header
    spec = _spec(text, choices)
    if not spec["text"]:
        return _result(CLARIFY, spec, pack, ["Enter or select a claim."])

    context = _context(px)
    if spec["evaluative"]:
        reasons = [f"\"{spec['evaluative']}\" is an overall judgement. FalconBridge has not approved criteria for "
                   "judging a lender's performance, so the evidence cannot establish it.",
                   "Measured differences are shown below as context. A lower default rate is not, on its own, "
                   "evidence of overall superior performance: rates are not adjusted for portfolio age, loan size or "
                   "industry mix, and the benchmark methodology is preliminary."]
        if spec["against"] == "peers":
            reasons.append("The analysis also holds no peer-group evidence.")
        return _result(CANNOT, spec, pack, reasons, context=context,
                       m2=_m2_evaluative(px, spec["text"]) if spec["against"] == "benchmark" else None)
    if spec["unavailable"]:
        return _result(CANNOT, spec, pack, [f"\"{spec['unavailable']}\" is not available in SBA FOIA data or the "
                                            "Saved Analysis (it publishes original approval amounts, loan status and "
                                            "gross charge-offs only)."])
    if spec["against"] == "peers":
        return _result(CANNOT, spec, pack, ["The analysis holds no peer-group evidence; only the bank's own measures "
                                            "and the SBA industry-matched benchmark are available."])

    clarify, reasons = [], []
    if spec["vague"]:
        reasons.append(f"\"{spec['vague']}\" has no defined threshold. State the measure and a number "
                       "(for example: % Cum. Loss below 1%).")
        clarify.append({"field": "restate", "question": "Restate the claim with a measure and a number.",
                        "options": []})
    if not spec["metric"]:
        cands = spec["metric_candidates"] or list(METRICS)
        reasons.append((spec["metric_note"] + ". ") if spec["metric_note"] else "The claim does not name a defined "
                       "measure. ")
        clarify.append({"field": "metric", "question": "Which defined measure does the claim refer to?",
                        "options": [o for o in _metric_choices() if o["value"] in cands]})
    if not spec["population"]:
        reasons.append("The claim does not define the population (all loans, or loans approved FY2000 onward).")
        clarify.append({"field": "population", "question": "Which loans does the claim cover?",
                        "options": [{"value": k, "label": v} for k, v in POPULATIONS.items()]})
    if not spec["period"]:
        reasons.append("The claim does not state a reporting period.")
        clarify.append({"field": "period", "question": "As of which reporting date?",
                        "options": [{"value": h["as_of"], "label": f"{h['as_of']} (SBA FOIA release of this "
                                                                    "analysis)"}]})
    if not spec["vague"] and spec["target"] is None and spec["against"] is None:
        reasons.append("The claim states no number or benchmark to compare with.")
        clarify.append({"field": "restate", "question": "Restate the claim with a number or the SBA benchmark.",
                        "options": []})
    elif spec["target"] is not None and spec["comparator"] is None:
        reasons.append("The claim gives a number but not the comparison (below, above, at most, at least).")
        clarify.append({"field": "restate", "question": "Restate the claim with a comparison word.", "options": []})
    if spec["metric"] and spec["target_unit"] and METRICS[spec["metric"]][1] != spec["target_unit"]:
        reasons.append(f"{METRICS[spec['metric']][0]} is measured in {METRICS[spec['metric']][1]}; the claim's number "
                       f"is in {spec['target_unit']}.")
        clarify.append({"field": "restate", "question": "Restate the number in the measure's unit.", "options": []})
    if spec["against"] == "benchmark" and spec["population"] == "full":
        reasons.append("The SBA benchmark covers loans approved FY2000 onward; a full-history figure is not the same "
                       "population.")
        clarify.append({"field": "population", "question": "Compare on the benchmark's population?",
                        "options": [{"value": "fy2000", "label": POPULATIONS["fy2000"]}]})
    if spec["against"] == "benchmark" and spec["metric"] in ("chgoff", "chgoff_amt"):
        reasons.append("Counts and dollar amounts depend on portfolio size and cannot be compared with the benchmark; "
                       "choose a rate.")
        clarify.append({"field": "metric", "question": "Which rate?",
                        "options": [o for o in _metric_choices() if o["value"] in ("cum_def", "cum_loss", "severity")]})
    if clarify:
        return _result(CLARIFY, spec, pack, reasons, clarify=_dedupe(clarify))

    if spec["period"] != h["as_of"]:
        return _result(CANNOT, spec, pack, [f"The evidence is as of {h['as_of']}; the claim is about {spec['period']}. "
                                            "No evidence for that period is in this analysis."])
    if spec["metric"] not in METRICS or spec["population"] not in POPULATIONS:
        return _result(CLARIFY, spec, pack, ["Unrecognised measure or population choice."],
                       clarify=[{"field": "metric", "question": "Which defined measure?", "options": _metric_choices()}])
    return _compare(px, spec, context)


def _dedupe(items):
    seen, out = set(), []
    for c in items:
        if c["field"] not in seen:
            seen.add(c["field"])
            out.append(c)
    return out


def _context(px):
    out = []
    for m, label, _ in PR.MEASURES:
        b, s = px.get(f"bank.fy2000.total.total.{m}"), px.get(f"benchmark.total.total.{m}")
        if b is not None and s is not None:
            out.append({"label": label, "bank": PR.pct(b.value), "sba": PR.pct(s.value),
                        "diff": f"{(b.value - s.value) * 100:+.2f} pp", "evidence": [b.evidence_id, s.evidence_id]})
    return out


def _m2_evaluative(px, text):
    """Run the lender's own wording through the M2 verifier, citing the % Cum. Defaulted comparison (for display)."""
    b, s = px.get("bank.fy2000.total.total.cum_def"), px.get("benchmark.total.total.cum_def")
    if b is None or s is None:
        return None
    recs = [b, s]
    h = px.pack.header
    c = Claim("CL-assess-eval", T.COMPARISON, h["source_id"], tuple(h["bank"]), h["as_of"], text,
              (b.evidence_id, s.evidence_id), (ValueUse(b.evidence_id, b.value, b.unit),
                                               ValueUse(s.evidence_id, s.value, s.unit)),
              Calculation("difference", (b.evidence_id, s.evidence_id), b.value - s.value, "ratio"),
              VF.effective_status(recs), VF.effective_methodology(recs),
              tuple(sorted(VF.required_caveats(px.pack, recs), key=lambda x: x.value)))
    v = VF.verify_claim(px.pack, c)
    return {"outcome": v.outcome.value, "explanation": v.explanation}


def _compare(px, spec, context):
    pack, h = px.pack, px.pack.header
    m, pop = spec["metric"], spec["population"]
    label, unit, definition = METRICS[m]
    rec = px.get(f"bank.{pop}.total.total.{m}")
    pop_text = POPULATIONS[pop]
    interpreted = (f"{label} ({definition}), {pop_text.lower()}, as of {h['as_of']}, is "
                   f"{COMPARATORS[spec['comparator']] if spec['comparator'] else 'compared with'} ")
    if rec is None:
        return _result(CANNOT, spec, pack, [f"No {label} value exists for this population (for example, it has no "
                                            "loans or no charge-offs)."], interpreted=interpreted + "...")
    w = PR._Writer(px)
    notes = []
    if spec["against"] == "benchmark":
        s = px.get(f"benchmark.total.total.{m}")
        if s is None:
            return _result(CANNOT, spec, pack, ["The SBA benchmark value for this measure is not available."])
        comp = spec["comparator"] or "lt"
        d = rec.value - s.value
        holds = {"lt": d < 0, "le": d <= 0, "gt": d > 0, "ge": d >= 0}[comp]
        word = "lower than" if d < 0 else "higher than" if d > 0 else None
        statement = w.claim(T.COMPARISON, f"For loans approved FY2000 onward, the bank's {label} is {PR.pct(rec.value)}, "
                            + (f"{PR.pp(d)} {word} the SBA benchmark's {PR.pct(s.value)}." if word else
                               f"the same as the SBA benchmark's {PR.pct(s.value)}."), [rec, s],
                            calc=Calculation("difference", (rec.evidence_id, s.evidence_id), d, "ratio"))
        evidence = [rec, s]
        interpreted += "the SBA industry-matched benchmark."
        methodology = ("Benchmark methodology is preliminary (FalconBridge has not confirmed it) and the rates are not "
                       "adjusted for portfolio age, loan size or industry mix.")
        notes.append("A lower rate on one measure is not, on its own, evidence of overall superior performance.")
    else:
        comp, target = spec["comparator"], spec["target"]
        holds = {"lt": rec.value < target, "le": rec.value <= target, "gt": rec.value > target,
                 "ge": rec.value >= target}[comp]
        statement = w.claim(T.OBSERVATION, f"The bank's {label if label.startswith('%') else label.lower()} for {pop_text[0].lower() + pop_text[1:]} is "
                            f"{_fmt(rec.value, unit)}.", [rec])
        evidence = [rec]
        interpreted += f"{_fmt(target, unit)}."
        methodology = {ME.UNCONFIRMED_WORKBOOK_METHOD: "Measure follows FalconBridge's workbook definition; FalconBridge "
                                                       "has not yet confirmed the methodology.",
                       ME.APPLICATION_RULE: "Population or measure is defined by this application and awaits "
                                            "FalconBridge's approval."}.get(rec.methodology, rec.methodology.value)
        if _fmt(rec.value, unit) == _fmt(target, unit):
            notes.append("The value equals the claimed figure at displayed precision; the result uses the unrounded "
                         "value.")
    small = [r for r in evidence if any(("Small sample" in x or "Limited data" in x) for x in r.limitations)]
    lim = px.get("limitation.total.limited" if pop == "fy2000" else "limitation.total.limited_full", Kind.DERIVED)
    if small or (lim is not None and lim.value):
        notes.append("Limited data: fewer than 30 bank loans; interpret with caution.")
    if statement["outcome"] != "ACCEPTED":
        return _result(CANNOT, spec, pack, ["The evidence statement did not pass verification: "
                                            + statement["explanation"]], statement=statement)
    outcome = SUPPORTED if holds else CONTRADICTED
    reasons = [f"Evidence: {statement['text']}",
               f"The claim states the value is {COMPARATORS[comp]} "
               + ("the SBA benchmark" if spec["against"] == "benchmark" else _fmt(spec["target"], unit))
               + f"; the evidence {'supports' if holds else 'contradicts'} it numerically."]
    return _result(outcome, spec, pack, reasons, statement=statement, methodology=methodology, notes=notes,
                   interpreted=interpreted, evidence=[PR.describe(r) for r in evidence],
                   methodology_approved=all(r.methodology is ME.CLIENT_APPROVED for r in evidence),
                   context=context if spec["against"] == "benchmark" else [])
