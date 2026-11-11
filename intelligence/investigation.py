"""Evidence-governed AI investigation (M3, offline): evidence pack -> brief -> model -> M2 verifier -> analyst review.

  1. build_brief      selects the evidence an investigation of bank-vs-benchmark differences needs (overall,
                      vintage, industry, loan size, portfolio age, limitations) from the pack, as plain records.
                      Selection only: no value is computed. For an external provider the brief is restricted to
                      records the egress policy approves and scanned for forbidden content.
  2. the provider     (intelligence/provider.py) returns structured items: observations, comparisons, possible
                      explanations (hypotheses), questions, limitations and missing information, each citing the
                      evidence keys it relies on and declaring any arithmetic as an approved operation.
  3. verification     every item becomes an M2 Claim and runs through the deterministic verifier: numbers must equal
                      the cited evidence, comparisons must cite a difference, causes and evaluations are refused
                      unless supported and approved, hypotheses must be hedged. Status, methodology and required
                      caveats are set from the cited evidence by this module, never taken from the model.
  4. analyst review   NOTHING the model writes is accepted automatically. Items the verifier rejected are BLOCKED
                      (they cannot be approved); every other item waits for an analyst's explicit decision.
                      Approving an interpretation records the analyst's judgement only: it stays UNVERIFIED and is
                      not FalconBridge methodology approval.
The AI layer performs no arithmetic of its own; the only arithmetic is the verifier recomputing a declared
difference, sum or ratio from cited evidence.
"""
import re
from dataclasses import asdict, dataclass, field

from . import egress as EG
from . import provider as PV
from . import verifier as VF
from .claims import Calculation, Claim, ClaimType, Outcome, ValueUse
from .evidence import Kind, ValidationStatus, canonical_json, sha256_of

TASK = "bank_vs_benchmark_investigation"
PROMPT_VERSION = "investigation-1.0"
INSTRUCTIONS = """You assist a credit analyst reviewing an SBA 7(a) lender. Use ONLY the evidence items in the brief.
Investigate material differences between the bank and the SBA benchmark, looking at vintage, industry, loan size
and portfolio age evidence. Return JSON {"items": [...]}; each item has:
  type: OBSERVATION | COMPARISON | HYPOTHESIS | QUESTION | LIMITATION | MISSING_INFORMATION
  text: one sentence. Every number must come from a cited item, written as shown or as a percentage with up to two
        decimals; a difference between two percentages is written in percentage points.
  cites: the brief keys the sentence relies on (required for every number and every factual statement).
  calculation: optional {"operation": "difference" | "sum" | "ratio", "operands": [key, key], "result": number,
        "unit": "ratio" | "count" | "usd"} for any figure not in the brief.
Rules: never call the bank better, worse, strong, weak or eligible; never state a cause as fact (a possible
explanation is a HYPOTHESIS worded with may / might / could); never invent criteria, thresholds or figures; state
what information is missing as MISSING_INFORMATION phrased as a question; respect each item's limitations
(preliminary benchmark, small samples, partial year)."""
TYPE_MAP = {"OBSERVATION": ClaimType.OBSERVATION, "COMPARISON": ClaimType.COMPARISON,
            "HYPOTHESIS": ClaimType.HYPOTHESIS, "QUESTION": ClaimType.QUESTION, "LIMITATION": ClaimType.LIMITATION,
            "MISSING_INFORMATION": ClaimType.QUESTION}
LABEL = {"OBSERVATION": "AI observation", "COMPARISON": "AI comparison", "HYPOTHESIS": "AI interpretation",
         "QUESTION": "AI question", "LIMITATION": "AI limitation", "MISSING_INFORMATION": "Missing information"}
DIMENSIONS = ("overall", "vintage", "industry", "loan_size", "portfolio_age")
BRIEF_PATTERNS = (
    r"bank\.full\.total\.total\.(loans|orig|active|age|chgoff|cum_def|cum_loss|severity)",
    r"bank\.fy2000\.(total|above|below)\.total\.(loans|cum_def|cum_loss|severity)",
    r"benchmark\.(total|above|below)\.total\.(loans|cum_def|cum_loss|severity)",
    r"compare\.(total|above|below)\.total\.(cum_def|cum_loss)\.diff",
    r"seasoning\..+",
    r"bank\.(full|fy2000)\.total\.vintage\.\d{4}\.(loans|cum_def)",
    r"concentration\.total\.(top1|top5|industries)",
    r"limitation\.(partial_year|total\.(limited|limited_full|small_sample_industries)|(above|below)\.limited)",
)
TOP_INDUSTRIES = 6


class InvestigationError(RuntimeError):
    pass


def dimension(key):
    if key.startswith("seasoning."):
        return "portfolio_age"
    if ".vintage." in key:
        return "vintage"
    if ".industry." in key or key.startswith("concentration."):
        return "industry"
    if re.search(r"\.(above|below)\.", key):
        return "loan_size"
    return "overall"


def _brief_item(r):
    return {"key": r.metric_key, "label": r.label, "value": r.value, "unit": r.unit, "kind": r.kind.value,
            "validation_status": r.validation_status.value, "methodology": r.methodology.value,
            "limitations": list(r.limitations), "dimension": dimension(r.metric_key)}


# ------------------------------------------------------------------------------------------- external safeguards
# Free text never leaves the machine: an external brief carries keys, values, units and caveat CODES only, and the
# whole request (instructions + brief) is scanned for these terms as well as the egress forbidden markers.
EXTERNAL_FORBIDDEN_TERMS = ("FalconBridge", "workbook", "Workbook", "Sector_Pivot", "Orig_v2", "PerformanceSummary",
                            "Combined_Sector", "Glossary", "Benchmark_Notes", "template", "Template", "investor",
                            "Investor", "loan tape", "app_data", "analysis.json")
CAVEAT_CODES = (("PRELIMINARY", "PRELIMINARY_BENCHMARK"), ("Not adjusted for vintage", "NOT_MIX_ADJUSTED"),
                ("Small sample", "SMALL_SAMPLE"), ("Limited data", "SMALL_SAMPLE"),
                ("Original amounts", "ORIGINAL_AMOUNTS"), ("date-based", "BENCHMARK_DEFAULT_DATE_BASED"))
STATUS_CODE = {"INDEPENDENTLY_VERIFIED": "verified", "RECONCILED_TO_TEMPLATE": "reconciled",
               "PRELIMINARY_METHODOLOGY": "preliminary", "PENDING_CLIENT_APPROVAL": "pending_approval",
               "UNVERIFIED": "unverified"}


def _external_item(r):
    lims = " | ".join(r.limitations)
    return {"key": r.metric_key, "value": r.value, "unit": r.unit, "dimension": dimension(r.metric_key),
            "status": STATUS_CODE[r.validation_status.value],
            "caveats": sorted({code for text, code in CAVEAT_CODES if text in lims})}


def is_synthetic(pack):
    """Synthetic is a property of the evidence, never just the caller's word: the lender is labelled SYNTHETIC, the
    calculation version is the synthetic fixture's, no SBA release files or validation records are involved and
    every record is UNVERIFIED."""
    h = pack.header
    return (str(h["bank"][0]).startswith("SYNTHETIC") and str(h.get("calculation_version", "")).startswith(
                "synthetic-fixture") and not h.get("release_files") and not h.get("release_folder")
            and not h.get("validations_applied")
            and all(r.validation_status is ValidationStatus.UNVERIFIED for r in pack.records))


@dataclass(frozen=True)
class ExternalAuthorization:
    """FalconBridge's explicit approval to send ONE synthetic evidence pack to an external provider."""
    pack_fingerprint: str
    approved_by: str
    approved_on: str
    purpose: str
    max_usd: float


def _check_external(pack, provider, ledger, authorization):
    if not is_synthetic(pack):
        raise InvestigationError("real bank evidence may not be sent to an external AI provider")
    if not isinstance(authorization, ExternalAuthorization):
        raise InvestigationError("an external call needs an explicit ExternalAuthorization")
    if not (authorization.approved_by and authorization.approved_on and authorization.purpose):
        raise InvestigationError("the authorization must name who approved it, when and for what")
    if authorization.pack_fingerprint != pack.fingerprint():
        raise InvestigationError("the authorization was given for a different evidence pack")
    if not 0 < authorization.max_usd <= PV.HARD_CAP_USD or ledger.limit_usd > authorization.max_usd:
        raise InvestigationError(f"budget outside the authorization (cap ${PV.HARD_CAP_USD:.2f})")


def build_brief(pack, external=False, policy=EG.DEFAULT_POLICY):
    """Evidence brief for the provider (selection only). An external brief is reduced to keys, values, units and
    caveat codes and scanned for forbidden content."""
    recs = [r for r in pack.records if r.kind is not Kind.HYPOTHESIS and isinstance(r.value, (int, float))
            and any(re.fullmatch(p, r.metric_key) for p in BRIEF_PATTERNS)]
    bank_years = {m.group(1) for r in recs for m in [re.match(r"bank\.full\.total\.vintage\.(\d{4})\.", r.metric_key)]
                  if m}
    shares = sorted(((r.value, r.metric_key[len("bank.full.total.industry."):-len(".share")]) for r in pack.records
                     if r.kind is Kind.FACT and re.fullmatch(r"bank\.full\.total\.industry\..+\.share", r.metric_key)),
                    reverse=True)[:TOP_INDUSTRIES]
    wanted = {f"benchmark.total.vintage.{y}.cum_def" for y in bank_years}
    for _, n in shares:
        wanted |= {f"bank.full.total.industry.{n}.{m}" for m in ("share", "loans", "cum_def")}
        wanted |= {f"bank.fy2000.total.industry.{n}.{m}" for m in ("loans", "cum_def")}     # benchmark basis
        wanted |= {f"benchmark.total.industry.{n}.cum_def", f"compare.total.industry.{n}.cum_def.diff"}
    recs += [r for r in pack.records if r.metric_key in wanted and isinstance(r.value, (int, float))]
    seen, items = set(), []
    for r in sorted(recs, key=lambda r: r.metric_key):
        if r.metric_key not in seen:
            seen.add(r.metric_key)
            items.append(_external_item(r) if external else _brief_item(r))
    brief = {"as_of": pack.header["as_of"], "pack_fingerprint": pack.fingerprint(), "items": items}
    if external:
        _scan(brief, pack)
    return brief


def _scan(obj, pack):
    text = canonical_json(obj)
    h = pack.header
    for marker in EG.FORBIDDEN_MARKERS + EXTERNAL_FORBIDDEN_TERMS + tuple(
            str(x) for x in (h.get("source_id"), h.get("job_id"), h.get("owner")) if x and len(str(x)) >= 8):
        if marker and marker in text:
            raise InvestigationError(f"request refused: forbidden content would leave the machine ({marker[:30]!r})")


@dataclass
class ReviewItem:
    item_id: str
    ai_type: str
    label: str
    text: str
    cites: list
    claim_id: str
    verifier_outcome: str
    verifier_findings: list
    status: str
    methodology: str
    caveats: list
    state: str                          # BLOCKED | PENDING_REVIEW | ANALYST_APPROVED | ANALYST_REJECTED
    standing: str = "NONE"              # after a decision: ESTABLISHED_FACT | PROPOSED_INTERPRETATION | QUESTION | NONE
    decision: dict = None


@dataclass
class Investigation:
    investigation_id: str
    synthetic: bool
    provider: str
    model: str
    provenance: str
    request_fingerprint: str
    pack_fingerprint: str
    cost_usd: float
    items: list = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    malformed: list = field(default_factory=list)
    request: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)
    decisions: list = field(default_factory=list)
    verifier_version: str = VF.VERIFIER_VERSION

    def item(self, item_id):
        for it in self.items:
            if it.item_id == item_id:
                return it
        raise InvestigationError(f"unknown item {item_id}")

    def to_dict(self):
        return asdict(self)


def _resolve(pack, key):
    for kind in (Kind.FACT, Kind.DERIVED):
        try:
            return pack.by_key(key, kind)
        except KeyError:
            continue
    return None


def _claim(pack, n, item):
    h = pack.header
    cited, refs = [], []
    for k in item.get("cites") or []:
        r = _resolve(pack, str(k))
        if r is None:
            refs.append("EV-UNKNOWN-" + sha256_of(str(k))[:12])        # the verifier reports it as unknown evidence
        else:
            cited.append(r)
            refs.append(r.evidence_id)
    calc = None
    c = item.get("calculation")
    if c:
        ops = [_resolve(pack, str(k)) for k in c.get("operands", [])]
        calc = Calculation(str(c.get("operation")), tuple(o.evidence_id if o else "EV-UNKNOWN" for o in ops),
                           float(c.get("result", float("nan"))), str(c.get("unit", "")))
    ctype = TYPE_MAP[item["type"]]
    status = ValidationStatus.UNVERIFIED if ctype is ClaimType.HYPOTHESIS else VF.effective_status(cited)
    return Claim(claim_id=f"CL-ai-{n:02d}", claim_type=ctype, source_id=h["source_id"], bank=tuple(h["bank"]),
                 as_of=h["as_of"], text=str(item["text"]), evidence_refs=tuple(dict.fromkeys(refs)),
                 values=tuple(ValueUse(r.evidence_id, r.value, r.unit) for r in dict.fromkeys(cited)
                              if isinstance(r.value, (int, float)) and not isinstance(r.value, bool)),
                 calculation=calc, validation_status=status, methodology=VF.effective_methodology(cited),
                 caveats=tuple(sorted(VF.required_caveats(pack, cited), key=lambda x: x.value)))


def _well_formed(item):
    if not isinstance(item, dict):
        return "item is not an object"
    if item.get("type") not in TYPE_MAP:
        return f"unknown item type {item.get('type')!r}"
    if not isinstance(item.get("text"), str) or not item["text"].strip():
        return "item has no text"
    if not isinstance(item.get("cites", []), list):
        return "cites must be a list"
    c = item.get("calculation")
    if c is not None and not (isinstance(c, dict) and isinstance(c.get("operands"), list)
                              and isinstance(c.get("result"), (int, float))):
        return "malformed calculation"
    return ""


def investigate(pack, provider, ledger, synthetic=None, policy=EG.DEFAULT_POLICY, authorization=None,
                max_output_tokens=4000):
    """Run one investigation. An external provider receives only a pack that IS synthetic (checked from the evidence)
    under an explicit authorization for that exact pack, within the authorised budget."""
    synth = is_synthetic(pack)
    if synthetic is not None and bool(synthetic) != synth:
        raise InvestigationError("the caller's synthetic flag does not match the evidence")
    if provider.external:
        _check_external(pack, provider, ledger, authorization)
    brief = build_brief(pack, external=provider.external, policy=policy)
    request = PV.ProviderRequest(TASK, PROMPT_VERSION, INSTRUCTIONS, brief, max_output_tokens)
    if provider.external:
        _scan({"instructions": INSTRUCTIONS, "brief": brief}, pack)
    response = PV.call(provider, request, ledger)
    return _process(pack, request, response, synth)


def _process(pack, request, response, synth, version=None):
    """Verify every returned item and queue it for review (shared by live/replay runs and audit replays)."""
    raw = response.content.get("items") if isinstance(response.content, dict) else None
    if not isinstance(raw, list):
        raise InvestigationError("provider response has no item list")
    brief = request.brief
    inv = Investigation("INV-" + sha256_of({"req": request.fingerprint(), "resp": response.content})[:16], synth,
                        response.provider, response.model, response.provenance, request.fingerprint(),
                        brief["pack_fingerprint"], response.cost_usd,
                        request={"task": request.task, "prompt_version": request.prompt_version,
                                 "instructions": request.instructions, "brief": request.brief,
                                 "max_output_tokens": request.max_output_tokens, "fingerprint": request.fingerprint()},
                        response=asdict(response), verifier_version=version or VF.VERIFIER_VERSION)
    cited_dims = set()
    for n, item in enumerate(raw, 1):
        why = _well_formed(item)
        if why:
            inv.malformed.append({"position": n, "reason": why})
            continue
        claim = _claim(pack, n, item)
        v = VF.verify_claim(pack, claim, version=inv.verifier_version)
        state = "BLOCKED" if v.outcome is Outcome.REJECTED else "PENDING_REVIEW"
        inv.items.append(ReviewItem(
            f"AI-{n:02d}", item["type"], LABEL[item["type"]], item["text"], list(item.get("cites") or []),
            claim.claim_id, v.outcome.value, [f"{f.rule}: {f.message}" for f in v.findings],
            v.effective_validation_status.value, v.effective_methodology.value,
            [c.value for c in v.required_caveats], state))
        if state != "BLOCKED" and item["type"] not in ("QUESTION", "MISSING_INFORMATION"):
            cited_dims |= {dimension(str(k)) for k in item.get("cites") or []}
    briefed = {it["dimension"] for it in brief["items"]}
    inv.coverage = {"examined": sorted(cited_dims), "not_examined": [d for d in DIMENSIONS if d not in cited_dims],
                    "available_in_brief": sorted(briefed)}
    return inv


def _standing(decision, it):
    """What an approval makes of an item. Only a statement the verifier ACCEPTED outright can become an established
    (numbers-checked) fact. Anything the verifier sent to review (e.g. "outperforms the benchmark": unapproved
    methodology) and every hypothesis can at most be a PROPOSED INTERPRETATION, whatever the analyst decides."""
    if decision == "REJECT":
        return "NONE"
    if it.ai_type in ("QUESTION", "MISSING_INFORMATION"):
        return "QUESTION"
    if it.ai_type == "HYPOTHESIS" or it.verifier_outcome != "ACCEPTED":
        return "PROPOSED_INTERPRETATION"
    return "ESTABLISHED_FACT"


MEANING = {
    "NONE": "rejected by the analyst",
    "QUESTION": "approved as a question to raise; it asserts no finding",
    "PROPOSED_INTERPRETATION": "proposed interpretation approved for discussion by the analyst; NOT an established "
                               "conclusion, still UNVERIFIED and not FalconBridge methodology approval",
    "ESTABLISHED_FACT": "established: every number was checked against recorded evidence and the analyst approved it "
                        "(not independent validation, not methodology approval)",
}


def decide(inv, item_id, decision, reviewer, note="", at=""):
    """Record an analyst's explicit decision. BLOCKED items cannot be approved; approving anything that is not an
    outright-accepted fact needs a note; every decision names its reviewer and is appended to inv.decisions."""
    it = inv.item(item_id)
    if decision not in ("APPROVE", "REJECT"):
        raise InvestigationError("decision must be APPROVE or REJECT")
    if not reviewer:
        raise InvestigationError("a decision must name its reviewer")
    if it.decision is not None:
        raise InvestigationError(f"{item_id} already has a decision")
    if decision == "APPROVE" and it.state == "BLOCKED":
        raise InvestigationError(f"{item_id} failed verification and cannot be approved")
    standing = _standing(decision, it)
    if decision == "APPROVE" and standing == "PROPOSED_INTERPRETATION" and not note:
        raise InvestigationError("approving an interpretation requires the reviewer's note")
    if decision == "REJECT" and not note:
        raise InvestigationError("a rejection requires a reason")
    it.decision = {"decision": decision, "reviewer": reviewer, "note": note, "at": at, "meaning": MEANING[standing]}
    it.standing = standing
    it.state = "ANALYST_APPROVED" if decision == "APPROVE" else "ANALYST_REJECTED"
    if standing == "PROPOSED_INTERPRETATION":
        it.label = "Proposed interpretation"
    inv.decisions.append({"seq": len(inv.decisions) + 1, "item": item_id, "decision": decision, "reviewer": reviewer,
                          "note": note, "at": at})
    return it


def summary(inv):
    states, standing = {}, {}
    for it in inv.items:
        states[it.state] = states.get(it.state, 0) + 1
        if it.decision:
            standing[it.standing] = standing.get(it.standing, 0) + 1
    return {"items": len(inv.items), "malformed": len(inv.malformed), "by_state": states, "by_standing": standing,
            "undecided": sum(1 for it in inv.items if it.state == "PENDING_REVIEW"), "coverage": inv.coverage}


# ------------------------------------------------------------------------------------------------ audit and replay
def audit_bundle(inv, ledger=None):
    """Everything needed to audit and replay one investigation: the exact request, the provider's response (content,
    usage, cost, provenance), every item's evidence references and verification, every analyst decision in order
    and the ledger. Sealed with a SHA-256 of its content."""
    body = {"format": "fbx-ai-investigation-audit-1", "investigation": inv.to_dict(),
            "ledger": list(getattr(ledger, "entries", []) or [])}
    return {**body, "bundle_sha256": sha256_of(body)}


def _check_bundle(bundle, pack):
    body = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    if sha256_of(body) != bundle.get("bundle_sha256"):
        raise InvestigationError("audit bundle failed its integrity check (content was altered)")
    rec = bundle["investigation"]
    if pack.fingerprint() != rec["pack_fingerprint"]:
        raise InvestigationError("the evidence pack differs from the one investigated")
    req = rec["request"]
    request = PV.ProviderRequest(req["task"], req["prompt_version"], req["instructions"], req["brief"],
                                 req["max_output_tokens"])
    if request.fingerprint() != rec["request_fingerprint"]:
        raise InvestigationError("recorded request does not match its fingerprint")
    if request.brief.get("pack_fingerprint") != pack.fingerprint():
        raise InvestigationError("the recorded brief was not built from this evidence pack")
    for item in request.brief["items"]:                    # every value the model saw is this pack's own value
        r = _resolve(pack, item["key"])
        if r is None or r.value != item["value"]:
            raise InvestigationError(f"recorded brief item {item['key']!r} does not match the evidence")
    resp = rec["response"]
    replay = PV.ReplayProvider({rec["request_fingerprint"]: {"model": resp["model"], "content": resp["content"],
                                                            "provenance": "audit replay of " + resp["provider"]}})
    return rec, request, PV.call(replay, request, PV.BudgetLedger(0.0))


def replay_bundle(bundle, pack):
    """Reproduce a recorded investigation offline UNDER THE VERIFIER VERSION IT WAS RECORDED WITH (records made before
    versioning used m2-1.0) and re-apply its decisions in order. Returns (investigation, differences); an empty list
    means the record was reproduced exactly. The bundle itself is never modified."""
    rec, request, response = _check_bundle(bundle, pack)
    inv = _process(pack, request, response, is_synthetic(pack), rec.get("verifier_version", "m2-1.0"))
    for d in rec["decisions"]:
        decide(inv, d["item"], d["decision"], d["reviewer"], d["note"], d["at"])
    return inv, _differences(rec["items"], inv.items, rec["malformed"], inv.malformed)


def _differences(old_items, new_items, old_malformed, new_malformed):
    diffs = []
    for old, new in zip(old_items, new_items):
        for k in ("item_id", "text", "cites", "verifier_outcome", "verifier_findings", "state", "standing", "status",
                  "caveats", "decision"):
            if old[k] != getattr(new, k):
                diffs.append({"item": old["item_id"], "field": k, "recorded": old[k], "replayed": getattr(new, k)})
    if len(old_items) != len(new_items) or old_malformed != new_malformed:
        diffs.append({"field": "items", "recorded": len(old_items), "replayed": len(new_items)})
    return diffs


def reassess_bundle(bundle, pack, version=None, assessed_on=""):
    """A SEPARATE, sealed re-assessment of a recorded investigation under another verifier version. The original
    bundle is not modified: the re-assessment references it by its SHA-256 and records, item by item, the original
    and revised verification and the reason for every change. Analyst decisions are not carried over or made."""
    version = version or VF.VERIFIER_VERSION
    rec, request, response = _check_bundle(bundle, pack)
    original_version = rec.get("verifier_version", "m2-1.0")
    revised = _process(pack, request, response, is_synthetic(pack), version)
    items = []
    for old, new in zip(rec["items"], revised.items):
        changed = old["verifier_outcome"] != new.verifier_outcome or old["verifier_findings"] != new.verifier_findings
        items.append({"item_id": old["item_id"], "ai_type": old["ai_type"], "text": old["text"], "cites": old["cites"],
                      "original": {"verifier_outcome": old["verifier_outcome"], "findings": old["verifier_findings"],
                                   "state": old["state"]},
                      "revised": {"verifier_outcome": new.verifier_outcome, "findings": new.verifier_findings,
                                  "state": new.state},
                      "changed": changed})
    body = {"format": "fbx-ai-reassessment-1", "source_bundle_sha256": bundle["bundle_sha256"],
            "source_investigation": rec["investigation_id"], "request_fingerprint": rec["request_fingerprint"],
            "original_verifier_version": original_version, "revised_verifier_version": version,
            "assessed_on": assessed_on, "items": items,
            "summary": {"changed": sum(1 for i in items if i["changed"]), "items": len(items),
                        "original_states": _count(i["original"]["state"] for i in items),
                        "revised_states": _count(i["revised"]["state"] for i in items)},
            "note": "Re-verification only: the provider response is the recorded one (no API call); no analyst "
                    "decision is made or carried over; the original sealed bundle is unchanged."}
    return {**body, "reassessment_sha256": sha256_of(body)}


def _count(values):
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


__all__ = ["build_brief", "investigate", "decide", "summary", "audit_bundle", "replay_bundle", "reassess_bundle",
           "is_synthetic",
           "ExternalAuthorization", "Investigation", "ReviewItem"]
