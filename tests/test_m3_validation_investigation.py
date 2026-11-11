"""M3: clean-room numerical validation from the SBA FOIA files, and the offline evidence-governed AI investigation on
SYNTHETIC data (provider-independent interface, replay only, M2 verification, analyst review). Offline throughout;
temporary app data; no paid or live calls."""
import ast
import copy
import json
import os
import socket
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FBX_APP_DATA", tempfile.mkdtemp(prefix="fbx_m3_test_"))
from fastapi.testclient import TestClient   # noqa: E402

from intelligence import cleanroom_foia as CR          # noqa: E402
from intelligence import egress as EG                  # noqa: E402
from intelligence import investigation as INV          # noqa: E402
from intelligence import provider as PV                # noqa: E402
from intelligence import replay_synthetic as RS        # noqa: E402
from intelligence import validation as VAL             # noqa: E402
from intelligence.evidence import EvidenceError, Methodology as ME, ValidationStatus as VS   # noqa: E402
from webapp import analysis as AN                      # noqa: E402
from webapp import app as A                            # noqa: E402
from webapp import generator_bridge as GB              # noqa: E402
from webapp import intel_adapter as IA                 # noqa: E402
from webapp import investigation_demo as DEMO          # noqa: E402
from webapp import presentation as PR                  # noqa: E402
from webapp import saved as SV                         # noqa: E402
from webapp import settings                            # noqa: E402
from webapp import synthetic_analysis as SY            # noqa: E402

RELEASE = ROOT / "source_data" / "20260630"
real = pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
client = TestClient(A.app)


# ------------------------------------------------------------------------------------------ independence / offline
def _imports(path):
    tree = ast.parse(Path(path).read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module.split(".")[0])
    return out


@pytest.mark.parametrize("module", ["cleanroom_foia", "investigation", "provider", "replay_synthetic"])
def test_m3_modules_import_no_application_engine_sdk_or_network(module):
    used = _imports(ROOT / "intelligence" / f"{module}.py")
    banned = {"webapp", "bank_workbook_generator", "acceptance", "oracle", "anthropic", "openai", "requests", "httpx",
              "urllib", "socket", "http", "aiohttp", "subprocess"}
    assert not used & banned, used & banned


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------------------------------ clean-room validation
@pytest.fixture(scope="module")
def metro():
    sel = GB.resolve("20260630", "US Metro Bank", "CA", "58310")
    payload = AN.compute("20260630", sel, GB.GC.BENCHMARK_STANDARD_MIN)
    return SV.save(settings.LOCAL_USER, payload, "0" * 32, "both")


@pytest.fixture(scope="module")
def produced(metro):
    return CR.produce(metro, str(RELEASE), "2026-11-06")


@real
def test_cleanroom_recomputes_and_agrees_with_the_analysis(metro, produced):
    rec, rep = produced
    assert rep["disagree"] == 0 and rep["agree"] == rep["values"] > 150
    keys = dict(rec.values)
    assert keys["bank.full.total.total.loans"] == 937 and keys["bank.full.total.total.orig"] == 1_536_344_900
    assert abs(keys["bank.full.total.total.cum_def"] - 0.016022053381372893) < 1e-15
    assert rec.source_id == metro["id"] and rec.payload_sha256 == IA.payload_fingerprint(metro)
    assert rec.as_of == "2026-06-30" and "LocationID 444026" in rec.population and "937 eligible" in rec.population
    assert rec.methodology_version == CR.METHODOLOGY_VERSION and len(rec.input_fingerprints) == 4
    assert not any(k.startswith("benchmark.") for k in keys)                # the benchmark is not recomputed


@real
def test_validation_is_numerical_only_and_keeps_methodology_separate(metro, produced):
    rec, _ = produced
    pk = IA.extract(metro, [rec])
    full, fy, bm = (pk.by_key(k) for k in ("bank.full.total.total.cum_def", "bank.fy2000.total.total.cum_def",
                                            "benchmark.total.total.cum_def"))
    assert full.validation_status is VS.INDEPENDENTLY_VERIFIED and full.numerically_verified
    assert full.methodology is ME.UNCONFIRMED_WORKBOOK_METHOD                # numbers validated, method not approved
    assert fy.validation_status is VS.PENDING_CLIENT_APPROVAL and fy.numerically_verified
    assert bm.validation_status is VS.PRELIMINARY_METHODOLOGY and not bm.numerically_verified
    assert all(r.methodology is not ME.CLIENT_APPROVED for r in pk.records)
    text = PR.build(metro, validations=[rec])["limitations"][-1]["text"]
    assert "numbers only" in text and "not approved" in text and "not independently recomputed" in text


@real
def test_record_binding_refuses_other_populations_payloads_and_periods(metro, produced):
    rec, _ = produced
    bad = copy.deepcopy(metro)
    bad["payload"]["identity"]["eligible"] = 936
    with pytest.raises(CR.CleanRoomError, match="population differs"):
        CR.produce(bad, str(RELEASE), "2026-11-06")
    changed = copy.deepcopy(metro)
    changed["payload"]["bank"]["full"]["total"]["total"]["avg"] += 1      # a different payload
    assert "different payload" in IA.extract(changed, [rec]).header["validations_ignored"][0]["reason"]
    other = replace(rec, as_of="2025-06-30")
    other = VAL.make_validation_record(**{k: getattr(other, k) for k in (
        "source_system", "source_id", "payload_sha256", "method", "performed_on", "performed_by", "reference",
        "values", "input_fingerprints", "as_of", "population", "methodology_version")})
    assert "reporting period" in IA.extract(metro, [other]).header["validations_ignored"][0]["reason"]


@real
def test_disagreement_is_shown_as_unverified(metro, produced):
    rec, _ = produced
    vals = dict(rec.values)
    vals["bank.full.total.total.cum_def"] += 0.001
    wrong = VAL.make_validation_record(rec.source_system, rec.source_id, rec.payload_sha256, rec.method,
                                       rec.performed_on, rec.performed_by, rec.reference, vals,
                                       input_fingerprints=rec.input_fingerprints, as_of=rec.as_of)
    m = PR.build(metro, validations=[wrong])
    assert "disagrees with 1 bank figure" in m["limitations"][-1]["text"]
    assert PR.pack_for(metro, [wrong]).by_key("bank.full.total.total.cum_def").validation_status is VS.UNVERIFIED


@real
def test_records_are_stored_tamper_evident_and_used_by_the_pages(metro, produced):
    rec, _ = produced
    d = str(settings.APP_DATA_DIR)
    path = CR.save_record(d, rec)
    assert CR.load_records(d, metro["id"]) == (rec,)
    ex = client.get(f"/analyses/{metro['id']}/executive").text
    assert "recomputed independently from the SBA FOIA data" in ex and "numbers only" in ex
    assert rec.record_id in client.get(f"/analyses/{metro['id']}/evidence").text
    doc = json.loads(Path(path).read_text())
    doc["values"][0][1] += 1
    Path(path).write_text(json.dumps(doc))
    try:
        with pytest.raises(EvidenceError, match="integrity"):
            CR.load_records(d, metro["id"])
        assert client.get(f"/analyses/{metro['id']}/executive").status_code == 409
    finally:
        os.remove(path)


def test_cleanroom_definitions_on_hand_made_rows():
    def row(st, amt, fy="2010", fd="2010-05-01", co="0"):
        return {"LoanStatus": st, "GrossApproval": str(amt), "SBAGuaranteedApproval": str(amt * 0.75),
                "ApprovalFiscalYear": fy, "FirstDisbursementDate": fd, "GrossChargeOffAmount": co}
    rows = [row("CHGOFF", 100_000, co="40000"), row("CLSLN", 300_000.5, co="0"), row("P I F", 200_000),
            row("CURR", 400_000, fd="2024-06-30"), row("PURCH(NOT C/O)", 500_000, fy="1999", fd="1999-01-15")]
    from datetime import date
    v = CR.recompute(rows, date(2026, 6, 30), 300_000)
    assert v["bank.full.total.total.loans"] == 5 and v["bank.full.total.total.chgoff"] == 2
    assert v["bank.full.total.total.cum_def"] == pytest.approx(400_000.5 / 1_500_000.5)
    assert v["bank.full.total.total.severity"] == pytest.approx(40_000 / 400_000.5)
    assert v["bank.full.total.total.age"] == pytest.approx((24 + 329) / 2)   # 2024-06-30 -> 24; 1999-01-15 -> 329
    assert v["bank.fy2000.total.total.loans"] == 4                           # FY1999 loan excluded
    assert v["bank.full.above.total.loans"] == 2                             # 300,000.5 rounds half-even to 300,000


# ------------------------------------------------------------------------------------------ AI investigation
@pytest.fixture(scope="module")
def synthetic_pack():
    return IA.extract(SY.synthetic_saved())


def run(pack):
    return INV.investigate(pack, PV.ReplayProvider(RS.recordings()), PV.BudgetLedger(0.0), synthetic=True)


def test_synthetic_pack_is_labelled_and_covers_every_dimension(synthetic_pack):
    assert "SYNTHETIC" in synthetic_pack.header["bank"][0] and "SYNTHETIC" in synthetic_pack.header["calculation_version"]
    assert all(r.validation_status is VS.UNVERIFIED for r in synthetic_pack.records)
    brief = INV.build_brief(synthetic_pack)
    assert {i["dimension"] for i in brief["items"]} == set(INV.DIMENSIONS)
    text = json.dumps(brief)
    assert synthetic_pack.header["source_id"] not in text and "EV-" not in text
    assert not any(m in text for m in EG.FORBIDDEN_MARKERS)


def test_investigation_verifies_every_item_and_routes_everything_to_review(synthetic_pack, no_network):
    before = synthetic_pack.fingerprint()
    inv = run(synthetic_pack)
    assert synthetic_pack.fingerprint() == before                            # the pack is never modified
    states = {it.item_id: it.state for it in inv.items}
    assert set(states.values()) == {"PENDING_REVIEW", "BLOCKED"}            # nothing is accepted automatically
    assert all(it.standing == "NONE" and it.decision is None for it in inv.items)
    blocked = {it.item_id: {f.split(":")[0] for f in it.verifier_findings} for it in inv.items if it.state == "BLOCKED"}
    assert blocked == {"AI-13": {"value_unsupported"}, "AI-15": {"causal_unsupported", "methodology_not_approved"},
                       "AI-16": {"criteria_invented"},
                       "AI-17": {"evidence_unknown", "value_unsupported", "reporting_period"},
                       "AI-18": {"calculation", "value_unsupported"},
                       "AI-19": {"segment_mismatch"}, "AI-20": {"denominator_scope"}}
    assert inv.item("AI-14").verifier_outcome == "NEEDS_ANALYST_REVIEW"     # evaluative wording goes to review
    assert inv.malformed == [{"position": 21, "reason": "unknown item type 'SPECULATION'"}]
    assert inv.item("AI-03").verifier_outcome == "ACCEPTED"                  # a rounded (0.2567) difference
    assert inv.coverage["not_examined"] == [] and inv.cost_usd == 0.0
    for it in inv.items:
        if it.ai_type not in ("QUESTION", "MISSING_INFORMATION") and it.state != "BLOCKED":
            assert it.cites                                                  # every statement cites evidence
    assert all(it.status == "UNVERIFIED" for it in inv.items if it.ai_type == "HYPOTHESIS")


def test_replay_is_deterministic_and_never_reuses_a_recording_for_another_request(synthetic_pack):
    assert run(synthetic_pack).to_dict() == run(synthetic_pack).to_dict()
    brief = INV.build_brief(synthetic_pack)
    other = PV.ProviderRequest(INV.TASK, "investigation-0.9", INV.INSTRUCTIONS, brief)
    with pytest.raises(PV.ReplayMiss):
        PV.call(PV.ReplayProvider(RS.recordings()), other, PV.BudgetLedger(0.0))
    changed = copy.deepcopy(synthetic_pack.records)                          # any change to the evidence
    pk2 = replace(synthetic_pack, records=tuple(r if r.metric_key != "benchmark.total.total.cum_def" else
                                                replace(r, value=0.061) for r in changed))
    with pytest.raises(PV.ReplayMiss):
        INV.investigate(pk2, PV.ReplayProvider(RS.recordings()), PV.BudgetLedger(0.0), synthetic=True)


def _auth(pack, max_usd=2.0, **kw):
    d = dict(pack_fingerprint=pack.fingerprint(), approved_by="FalconBridge (test)", approved_on="2026-11-06",
             purpose="first controlled synthetic experiment", max_usd=max_usd)
    d.update(kw)
    return INV.ExternalAuthorization(**d)


def test_live_provider_accepts_only_authorised_synthetic_evidence(synthetic_pack, metro, no_network, monkeypatch):
    live = PV.LiveProvider()
    real = IA.extract(metro)
    assert INV.is_synthetic(synthetic_pack) and not INV.is_synthetic(real)
    with pytest.raises(INV.InvestigationError, match="does not match the evidence"):     # a false synthetic label
        INV.investigate(real, live, PV.BudgetLedger(2.0), synthetic=True, authorization=_auth(real))
    with pytest.raises(INV.InvestigationError, match="real bank evidence"):
        INV.investigate(real, live, PV.BudgetLedger(2.0), authorization=_auth(real))
    with pytest.raises(INV.InvestigationError, match="explicit ExternalAuthorization"):
        INV.investigate(synthetic_pack, live, PV.BudgetLedger(2.0))
    with pytest.raises(INV.InvestigationError, match="different evidence pack"):
        INV.investigate(synthetic_pack, live, PV.BudgetLedger(2.0), authorization=_auth(synthetic_pack,
                                                                                      pack_fingerprint="0" * 64))
    with pytest.raises(INV.InvestigationError, match="name who approved"):
        INV.investigate(synthetic_pack, live, PV.BudgetLedger(2.0), authorization=_auth(synthetic_pack, approved_by=""))
    with pytest.raises(PV.LiveCallsDisabled, match="FBX_AI_LIVE"):     # all checks pass; the switch is off
        INV.investigate(synthetic_pack, live, PV.BudgetLedger(2.0), authorization=_auth(synthetic_pack))
    monkeypatch.setenv(PV.LIVE_SWITCH_ENV, "enabled")
    with pytest.raises(PV.LiveCallsDisabled, match="not implemented"):  # no SDK, no client in this build
        INV.investigate(synthetic_pack, live, PV.BudgetLedger(2.0), authorization=_auth(synthetic_pack))


def test_external_brief_carries_no_free_text_or_confidential_terms(synthetic_pack):
    brief = INV.build_brief(synthetic_pack, external=True)
    assert all(set(i) == {"key", "value", "unit", "dimension", "status", "caveats"} for i in brief["items"])
    text = json.dumps({"instructions": INV.INSTRUCTIONS, "brief": brief})
    assert not any(term in text for term in INV.EXTERNAL_FORBIDDEN_TERMS + EG.FORBIDDEN_MARKERS)
    with pytest.raises(INV.InvestigationError, match="forbidden content"):
        INV._scan({"note": "from the FalconBridge workbook"}, synthetic_pack)


def test_budget_is_capped_disabled_by_default_and_has_a_kill_switch(synthetic_pack):
    assert PV.BudgetLedger().limit_usd == 0.0
    with pytest.raises(PV.BudgetExceeded):
        PV.BudgetLedger(2.01)                                                # above the $2 experiment cap
    request = PV.ProviderRequest(INV.TASK, INV.PROMPT_VERSION, INV.INSTRUCTIONS,
                                 INV.build_brief(synthetic_pack, external=True), max_output_tokens=16000)
    worst = PV.worst_case_cost(request, "claude-opus-5-5")
    assert 0 < worst < 0.5
    with pytest.raises(PV.BudgetExceeded):
        PV.BudgetLedger(0.0).authorise(request, worst)                       # disabled by default: refused
    with pytest.raises(PV.BudgetExceeded, match="no finite"):
        PV.BudgetLedger(2.0).authorise(request, PV.worst_case_cost(request, "unknown-model"))
    ledger = PV.BudgetLedger(2.0)
    ledger.authorise(request, worst)
    ledger.kill("analyst stopped the experiment")
    with pytest.raises(PV.BudgetExceeded, match="kill switch"):
        ledger.authorise(request, worst)
    ledger = PV.BudgetLedger(0.5)                                            # during usage: an overrun kills the ledger
    over = PV.ProviderResponse(request.fingerprint(), "anthropic", "claude-opus-5-5", {"items": []}, cost_usd=0.6)
    with pytest.raises(PV.BudgetExceeded):
        ledger.charge(over, authorised_usd=worst)
    assert ledger.killed and ledger.spent_usd == 0.6 and ledger.entries[-1]["killed"]
    ledger = PV.BudgetLedger(2.0)
    ledger.charge(PV.ProviderResponse("x", "anthropic", "m", {}, cost_usd=0.9), authorised_usd=0.5)
    assert "exceeded the authorised" in ledger.killed                        # cost above the pre-authorised bound


def test_unsupported_judgement_can_never_become_an_established_conclusion(synthetic_pack):
    inv = run(synthetic_pack)
    with pytest.raises(INV.InvestigationError, match="note"):
        INV.decide(inv, "AI-14", "APPROVE", "analyst")
    it = INV.decide(inv, "AI-14", "APPROVE", "analyst", "worth discussing")
    assert it.standing == "PROPOSED_INTERPRETATION" and it.label == "Proposed interpretation"
    assert "NOT an established conclusion" in it.decision["meaning"]
    assert INV.decide(inv, "AI-01", "APPROVE", "analyst").standing == "ESTABLISHED_FACT"
    assert INV.decide(inv, "AI-07", "APPROVE", "analyst", "plausible").standing == "PROPOSED_INTERPRETATION"
    assert INV.decide(inv, "AI-09", "APPROVE", "analyst").standing == "QUESTION"


def test_investigation_is_auditable_and_replays_exactly(synthetic_pack, tmp_path):
    saved, pack, inv, ledger, log = DEMO.run()
    bundle = json.loads(json.dumps(INV.audit_bundle(inv, ledger)))           # through JSON, as stored on disk
    replayed, diffs = INV.replay_bundle(bundle, pack)
    assert diffs == [] and [d["item"] for d in bundle["investigation"]["decisions"]][:2] == ["AI-01", "AI-02"]
    assert bundle["investigation"]["request"]["brief"]["items"] and bundle["investigation"]["response"]["content"]
    assert all(i["claim_id"] and "verifier_outcome" in i for i in bundle["investigation"]["items"])
    tampered = copy.deepcopy(bundle)
    tampered["investigation"]["items"][12]["state"] = "ANALYST_APPROVED"
    with pytest.raises(INV.InvestigationError, match="integrity"):
        INV.replay_bundle(tampered, pack)
    other = replace(pack, records=pack.records[:-1])
    with pytest.raises(INV.InvestigationError, match="differs"):
        INV.replay_bundle(bundle, other)


def test_analyst_decisions_are_explicit_and_constrained(synthetic_pack):
    inv = run(synthetic_pack)
    with pytest.raises(INV.InvestigationError, match="cannot be approved"):
        INV.decide(inv, "AI-13", "APPROVE", "analyst", "x")
    with pytest.raises(INV.InvestigationError, match="note"):
        INV.decide(inv, "AI-07", "APPROVE", "analyst")
    with pytest.raises(INV.InvestigationError, match="reason"):
        INV.decide(inv, "AI-08", "REJECT", "analyst")
    with pytest.raises(INV.InvestigationError, match="reviewer"):
        INV.decide(inv, "AI-01", "APPROVE", "")
    it = INV.decide(inv, "AI-07", "APPROVE", "analyst", "plausible")
    assert it.state == "ANALYST_APPROVED" and it.status == "UNVERIFIED" and "not FalconBridge" in it.decision["meaning"]
    with pytest.raises(INV.InvestigationError, match="already"):
        INV.decide(inv, "AI-07", "REJECT", "analyst", "changed my mind")
    assert INV.decide(inv, "AI-14", "REJECT", "analyst", "evaluative").state == "ANALYST_REJECTED"


def test_demonstration_walkthrough(no_network):
    saved, pack, inv, ledger, log = DEMO.run()
    md = DEMO.walkthrough(saved, pack, inv, ledger, log)
    assert "SYNTHETIC DATA" in md and "not output of any AI model" in md
    s = INV.summary(inv)
    assert s["by_state"] == {"ANALYST_APPROVED": 11, "ANALYST_REJECTED": 2, "BLOCKED": 7} and s["undecided"] == 0
    assert s["by_standing"] == {"ESTABLISHED_FACT": 7, "PROPOSED_INTERPRETATION": 1, "QUESTION": 3, "NONE": 2}
    assert {"item": "AI-13", "decision": "APPROVE",
            "result": "refused: AI-13 failed verification and cannot be approved"} in log
    assert ledger.spent_usd == 0.0 and ledger.entries[0]["provider"] == "replay"
