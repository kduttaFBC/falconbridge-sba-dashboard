"""M2 deterministic claim verifier: valid claims are accepted; invalid and adversarial claims are rejected (or sent to
analyst review) with a specific finding. Synthetic Saved Analyses only; offline; nothing leaves the machine.

Synthetic bank (full history = FY2000+): 30 loans, $4,000,000 original, 1 charge-off ($100,000 original, $40,000
charged off) -> % Cum. Defaulted 2.50%, % Cum. Loss 1.00%, severity 40.0%, average age 30.0 months, $3,900,000 original
amount of active loans. Benchmark % Cum. Defaulted 6.50% (difference -4.00 pp). Hotels: 25 loans, 87.5% share;
Full-Service Restaurants: 5 loans (small sample), 0.0%. 2019 vintage: 10.0%. Partial year 2026.
"""
import socket
from dataclasses import replace

import pytest

from test_intelligence_evidence import CANARY, PERMISSIVE, synthetic_saved, validation_record   # noqa: F401

from intelligence import egress as EG
from intelligence import verifier as VF
from intelligence.claims import Calculation as Calc, Caveat as C, Claim, ClaimType as T, Outcome, ValueUse
from intelligence.evidence import Classification as CL, EvidencePack, Kind, Methodology as ME, ValidationStatus as VS
from webapp import intel_adapter as IA

SAVED = synthetic_saved()
PK = IA.extract(SAVED)
TERMS = IA.confidential_terms(SAVED)
PK_B = IA.extract(synthetic_saved(sid="b" * 32, cert="11111"))                     # another bank's Saved Analysis
REGISTRY = {r.evidence_id: (PK_B.header["source_id"], tuple(PK_B.header["bank"])) for r in PK_B.records}
PK_VR = IA.extract(SAVED, [validation_record(SAVED, {"bank.full.total.total.cum_def": 0.025})])
RTT, PRE, PEN, UNV, IVF = (VS.RECONCILED_TO_TEMPLATE, VS.PRELIMINARY_METHODOLOGY, VS.PENDING_CLIENT_APPROVAL,
                           VS.UNVERIFIED, VS.INDEPENDENTLY_VERIFIED)
UWM, CDP, APR, NA, CAP = (ME.UNCONFIRMED_WORKBOOK_METHOD, ME.CLIENT_DIRECTED_PRELIMINARY, ME.APPLICATION_RULE,
                          ME.NOT_APPLICABLE, ME.CLIENT_APPROVED)
FY_CD, BM_CD = "bank.fy2000.total.total.cum_def", "benchmark.total.total.cum_def"
DIFF = "compare.total.total.cum_def.diff"
HOT, RES = "bank.full.total.industry.Hotels (except Casino Hotels) and Motels", "bank.full.total.industry.Full-Service Restaurants"


def approved_pack():
    """A pack in a FUTURE state in which FalconBridge has approved the methodology of the two compared measures."""
    recs = [replace(r, validation_status=RTT, methodology=CAP) if r.metric_key in (FY_CD, BM_CD) else r
            for r in PK.records]
    return EvidencePack(PK.header, tuple(recs))


PK_OK = approved_pack()


def rec(key, pack=PK, kind=Kind.FACT):
    return pack.by_key(key, kind)


def eid(key, pack=PK, kind=Kind.FACT):
    return rec(key, pack, kind).evidence_id


def val(key, pack=PK, kind=Kind.FACT):
    r = rec(key, pack, kind)
    return ValueUse(r.evidence_id, r.value, r.unit)


def claim(cid, ctype, text, keys=(), vs=RTT, me=UWM, caveats=(), values=None, calc=None, pack=PK, kinds=None, **kw):
    kinds = kinds or {}
    refs = tuple(eid(k, pack, kinds.get(k, Kind.FACT)) for k in keys)
    values = tuple(val(k, pack, kinds.get(k, Kind.FACT)) for k in keys) if values is None else values
    h = pack.header
    base = dict(claim_id=cid, claim_type=ctype, source_id=h["source_id"], bank=tuple(h["bank"]), as_of=h["as_of"],
                text=text, evidence_refs=refs, values=values, calculation=calc, validation_status=vs, methodology=me,
                caveats=tuple(caveats))
    base.update(kw)
    return Claim(**base)


def diff_calc(pack=PK, result=None, op="difference", keys=(FY_CD, BM_CD), unit="ratio"):
    a, b = rec(keys[0], pack), rec(keys[1], pack)
    return Calc(op, (a.evidence_id, b.evidence_id), a.value - b.value if result is None else result, unit)


DK = {DIFF: Kind.DERIVED}
COMPARE_TEXT = ("For the FY2000+ comparison, the bank's % Cum. Defaulted of 2.50% is 4.00 percentage points lower "
                "than the preliminary SBA benchmark's 6.50%.")
CMP_CAV = (C.PRELIMINARY_BENCHMARK, C.NOT_MIX_ADJUSTED, C.PENDING_CLIENT_APPROVAL)

# ------------------------------------------------------------------------------------------------- 22 valid claims
VALID = [
    (PK, claim("CL-v01", T.OBSERVATION, "The bank originated 30 loans with $4,000,000 of original approval amounts "
               "(full history).", ("bank.full.total.total.loans", "bank.full.total.total.orig"),
               caveats=(C.ORIGINAL_AMOUNTS,))),
    (PK, claim("CL-v02", T.OBSERVATION, "% Cum. Defaulted is 2.50% over the bank's full history.",
               ("bank.full.total.total.cum_def",))),
    (PK, claim("CL-v03", T.OBSERVATION, "% Cum. Loss is 1.00% and % Loss Severity is 40.0%.",
               ("bank.full.total.total.cum_loss", "bank.full.total.total.severity"))),
    (PK, claim("CL-v04", T.OBSERVATION, "Original approvals total $4.0 million.", ("bank.full.total.total.orig",),
               caveats=(C.ORIGINAL_AMOUNTS,))),
    (PK, claim("CL-v05", T.OBSERVATION, "Active loans have an average age of 30.0 months.",
               ("bank.full.total.total.age",))),
    (PK, claim("CL-v06", T.COMPARISON, COMPARE_TEXT, (FY_CD, BM_CD), vs=PRE, me=CDP, caveats=CMP_CAV,
               calc=diff_calc())),
    (PK, claim("CL-v07", T.COMPARISON, "The bank's FY2000+ % Cum. Defaulted is 4.00 percentage points lower than the "
               "preliminary SBA benchmark.", (DIFF,), vs=PRE, me=CDP, kinds=DK,
               caveats=(C.PRELIMINARY_BENCHMARK, C.NOT_MIX_ADJUSTED))),
    (PK, claim("CL-v08", T.COMPARISON, "The bank's FY2000+ % Cum. Defaulted is 400 basis points lower than the "
               "preliminary SBA benchmark.", (DIFF,), vs=PRE, me=CDP, kinds=DK,
               caveats=(C.PRELIMINARY_BENCHMARK, C.NOT_MIX_ADJUSTED))),
    (PK, claim("CL-v09", T.OBSERVATION, "Loans first disbursed in 2019 have a % Cum. Defaulted of 10.0%.",
               ("bank.full.total.vintage.2019.cum_def",))),
    (PK, claim("CL-v10", T.OBSERVATION, "Hotels (except Casino Hotels) and Motels account for 87.5% of the bank's "
               "original approval amount.", (f"{HOT}.share",))),
    (PK, claim("CL-v11", T.OBSERVATION, "The largest industry accounts for 87.5% of the bank's original approval "
               "amount.", ("concentration.total.top1",), vs=PEN, me=APR, caveats=(C.PENDING_CLIENT_APPROVAL,),
               kinds={"concentration.total.top1": Kind.DERIVED})),
    (PK, claim("CL-v12", T.OBSERVATION, "Full-Service Restaurants has 5 loans and a % Cum. Defaulted of 0.0%; with "
               "fewer than 10 loans this is a small sample.", (f"{RES}.loans", f"{RES}.cum_def"),
               caveats=(C.SMALL_SAMPLE,))),
    (PK, claim("CL-v13", T.LIMITATION, "The 2026 vintage is a partial year.", ("limitation.partial_year",), vs=PEN,
               me=APR, caveats=(C.PARTIAL_YEAR, C.PENDING_CLIENT_APPROVAL),
               kinds={"limitation.partial_year": Kind.DERIVED})),
    (PK, claim("CL-v14", T.QUESTION, "What drove the 10.0% % Cum. Defaulted for loans first disbursed in 2019?",
               ("bank.full.total.vintage.2019.cum_def",))),
    (PK, claim("CL-v15", T.QUESTION, "Does FalconBridge apply a minimum number of loans when evaluating a lender?",
               (), vs=UNV, me=NA)),
    (PK, claim("CL-v16", T.HYPOTHESIS, "The lower FY2000+ % Cum. Defaulted may partly reflect a younger portfolio: "
               "50.0% of the bank's original amount was disbursed 2022-2026.",
               (FY_CD, "seasoning.2022-2026.bank_share"), vs=UNV, me=NA, caveats=(C.PENDING_CLIENT_APPROVAL,),
               values=(val("seasoning.2022-2026.bank_share", kind=Kind.DERIVED),),
               kinds={"seasoning.2022-2026.bank_share": Kind.DERIVED})),
    (PK, claim("CL-v17", T.OBSERVATION, "The bank's % Cum. Defaulted is 2.50%.", ("bank.full.total.total.cum_def",),
               exportable=True)),
    (PK_VR, claim("CL-v18", T.OBSERVATION, "The bank's % Cum. Defaulted is 2.50% (independently recomputed).",
                  ("bank.full.total.total.cum_def",), vs=IVF, pack=PK_VR)),
    (PK_OK, claim("CL-v19", T.COMPARISON, "For the FY2000+ comparison, the bank's % Cum. Defaulted is better than the "
                  "SBA benchmark: 2.50% versus 6.50%, 4.00 percentage points lower.", (FY_CD, BM_CD), me=CAP,
                  caveats=(C.NOT_MIX_ADJUSTED,), calc=diff_calc(PK_OK), pack=PK_OK)),
    (PK, claim("CL-v20", T.OBSERVATION, "$3,900,000 of original approval amount remains with active loans.",
               ("bank.full.total.total.active_orig",), caveats=(C.ORIGINAL_AMOUNTS,))),
    (PK, claim("CL-v21", T.CALCULATION, "Hotels (except Casino Hotels) and Motels and Full-Service Restaurants "
               "together hold 30 loans.", (f"{HOT}.loans", f"{RES}.loans"), caveats=(C.SMALL_SAMPLE,), values=(),
               calc=Calc("sum", (eid(f"{HOT}.loans"), eid(f"{RES}.loans")), 30, "count"))),
    (PK, claim("CL-v22", T.CALCULATION, "Charged-off loans represent 2.5% of the original approval amount.",
               ("bank.full.total.total.chgoff_orig", "bank.full.total.total.orig"), vs=PEN, me=APR, values=(),
               caveats=(C.PENDING_CLIENT_APPROVAL, C.ORIGINAL_AMOUNTS),
               calc=Calc("ratio", (eid("bank.full.total.total.chgoff_orig"), eid("bank.full.total.total.orig")),
                         0.025, "ratio"))),
]

# ----------------------------------------------------------------------------------- 49 invalid / adversarial claims
v2 = VALID[1][1]
v6 = VALID[5][1]
B = dict(keys=("bank.full.total.total.cum_def",))
ONLY_PUBLIC = EG.EgressPolicy(frozenset({CL.PUBLIC_FOIA_AGGREGATE}), approved_by="test", approved_on="2026-11-05")
INVALID = [
    # 1. invented financial values
    ("value_mismatch", {}, replace(v2, claim_id="CL-i01", values=(replace(v2.values[0], value=0.03),))),
    ("value_unsupported", {}, replace(v2, claim_id="CL-i02", text="% Cum. Defaulted is 3.10% over the full history.")),
    ("value_unsupported", {}, claim("CL-i03", T.OBSERVATION, "Original approvals total $5,000,000.",
                                    ("bank.full.total.total.orig",), caveats=(C.ORIGINAL_AMOUNTS,))),
    ("value_unsupported", {}, claim("CL-i04", T.OBSERVATION, "The bank originated 31 loans.",
                                    ("bank.full.total.total.loans",))),
    ("value_unsupported", {}, claim("CL-i05", T.OBSERVATION, "Active loans have an average age of 42.8 months.",
                                    ("bank.full.total.total.age",))),
    # 2. correct numbers, wrong reporting period
    ("reporting_period", {}, replace(v2, claim_id="CL-i06", as_of="2025-06-30")),
    ("reporting_period", {}, replace(v2, claim_id="CL-i07", text="As of June 30, 2025, % Cum. Defaulted is 2.50%.")),
    ("reporting_period", {}, claim("CL-i08", T.OBSERVATION, "Loans first disbursed in 2020 have a % Cum. Defaulted of "
                                   "10.0%.", ("bank.full.total.vintage.2019.cum_def",))),
    ("reporting_period", {}, replace(v2, claim_id="CL-i09", text="% Cum. Defaulted was 2.50% at 2025-12-31.")),
    # 3. fabricated evidence identifiers
    ("evidence_unknown", {}, replace(v2, claim_id="CL-i10", evidence_refs=("EV-000000000000000000000000",),
                                     values=(ValueUse("EV-000000000000000000000000", 0.025, "ratio"),))),
    ("evidence_unknown", {}, replace(v2, claim_id="CL-i11", evidence_refs=v2.evidence_refs + ("EV-forged",))),
    # 4. cross-bank evidence substitution
    ("evidence_unknown", {"registry": REGISTRY},
     replace(v2, claim_id="CL-i12", evidence_refs=(eid("bank.full.total.total.cum_def", PK_B),),
             values=(val("bank.full.total.total.cum_def", PK_B),))),
    ("source_mismatch", {}, replace(v2, claim_id="CL-i13", source_id=PK_B.header["source_id"])),
    ("source_mismatch", {}, replace(v2, claim_id="CL-i14", bank=tuple(PK_B.header["bank"]))),
    # 5. unsupported performance rankings
    ("ranking_unsupported", {}, replace(v2, claim_id="CL-i15", text="The bank ranks in the top quartile of SBA lenders "
                                        "with a % Cum. Defaulted of 2.50%.")),
    ("ranking_unsupported", {}, replace(v2, claim_id="CL-i16", text="The bank has the best % Cum. Defaulted, 2.50%.")),
    ("ranking_unsupported", {}, replace(v2, claim_id="CL-i17", text="At 2.50%, the bank is a leading SBA lender.")),
    # 6. preliminary benchmark presented as approved
    ("methodology_elevated", {}, replace(v6, claim_id="CL-i18", methodology=CAP)),
    ("approval_misrepresented", {}, replace(v6, claim_id="CL-i19", text=COMPARE_TEXT.replace(
        "the preliminary SBA benchmark's", "the FalconBridge-approved SBA benchmark's"))),
    ("approval_misrepresented", {}, replace(v6, claim_id="CL-i20", text=COMPARE_TEXT + " The benchmark methodology is "
                                            "confirmed.")),
    ("caveat_missing", {}, replace(v6, claim_id="CL-i21", caveats=(C.NOT_MIX_ADJUSTED, C.PENDING_CLIENT_APPROVAL))),
    ("status_elevated", {}, replace(v6, claim_id="CL-i22", validation_status=RTT)),
    # 7. incorrect percentage formatting
    ("percentage_format", {}, replace(v2, claim_id="CL-i23", text="% Cum. Defaulted is 0.025% over the full history.")),
    ("percentage_format", {}, replace(v2, claim_id="CL-i24", text="% Cum. Defaulted is 250.0% over the full history.")),
    ("percentage_format", {}, replace(v6, claim_id="CL-i25", text=COMPARE_TEXT.replace("4.00 percentage points",
                                                                                       "4.00%"))),
    ("value_unsupported", {}, replace(v6, claim_id="CL-i26", text=COMPARE_TEXT.replace("4.00 percentage points",
                                                                                       "2.50 percentage points"))),
    # 8. small population overinterpretation
    ("caveat_missing", {}, claim("CL-i27", T.OBSERVATION, "Full-Service Restaurants has 5 loans and a % Cum. "
                                 "Defaulted of 0.0%.", (f"{RES}.loans", f"{RES}.cum_def"))),
    ("small_sample_overinterpretation", {}, claim("CL-i28", T.OBSERVATION, "Full-Service Restaurants consistently "
                                                  "shows a 0.0% % Cum. Defaulted across its 5 loans.",
                                                  (f"{RES}.loans", f"{RES}.cum_def"), caveats=(C.SMALL_SAMPLE,))),
    ("significance_unsupported", {}, replace(v6, claim_id="CL-i29", text=COMPARE_TEXT + " The gap is statistically "
                                             "significant.")),
    ("small_sample_overinterpretation", {}, claim("CL-i30", T.OBSERVATION, "With 5 loans and a 0.0% % Cum. Defaulted, "
                                                  "Full-Service Restaurants clearly demonstrates reliable credit.",
                                                  (f"{RES}.loans", f"{RES}.cum_def"), caveats=(C.SMALL_SAMPLE,))),
    # 9. unsupported causal explanations
    ("causal_unsupported", {}, replace(v2, claim_id="CL-i31", text="% Cum. Defaulted is 2.50% because of conservative "
                                       "underwriting.")),
    ("causal_unsupported", {}, replace(v6, claim_id="CL-i32", text=COMPARE_TEXT + " The gap is driven by hotel "
                                       "concentration.")),
    ("hypothesis_stated_as_fact", {}, replace(VALID[15][1], claim_id="CL-i33", text="The lower FY2000+ % Cum. "
                                              "Defaulted reflects a younger portfolio.", values=())),
    ("status_elevated", {}, replace(VALID[15][1], claim_id="CL-i34", validation_status=RTT)),
    # 10. confidential material in exportable claims
    ("confidential_content", {"policy": PERMISSIVE, "confidential_terms": TERMS},
     replace(VALID[16][1], claim_id="CL-i35", text=f"The bank's % Cum. Defaulted is 2.50% ({CANARY} label).")),
    ("confidential_content", {"policy": PERMISSIVE, "confidential_terms": TERMS},
     replace(VALID[16][1], claim_id="CL-i36", text=f"The bank's % Cum. Defaulted is 2.50% (see {EG.LOCAL_HOME}x).")),
    ("confidential_content", {"policy": PERMISSIVE, "confidential_terms": TERMS},
     replace(VALID[16][1], claim_id="CL-i37", text="The bank's % Cum. Defaulted is 2.50% (PerformanceSummary!S6).")),
    ("export_refused", {"policy": ONLY_PUBLIC, "confidential_terms": TERMS}, replace(v6, claim_id="CL-i38",
                                                                                     exportable=True)),
    ("export_refused", {}, replace(VALID[16][1], claim_id="CL-i39")),        # default policy approves nothing
    ("export_refused", {"policy": PERMISSIVE}, replace(VALID[15][1], claim_id="CL-i40", exportable=True)),
    # invented FalconBridge criteria
    ("criteria_invented", {}, replace(v2, claim_id="CL-i41", text="With a % Cum. Defaulted of 2.50%, the bank is "
                                      "eligible for FalconBridge's securitization.")),
    ("criteria_invented", {}, replace(v2, claim_id="CL-i42", text="FalconBridge should invest in this lender given its "
                                      "2.50% % Cum. Defaulted.")),
    ("criteria_invented", {}, replace(v2, claim_id="CL-i43", text="At 2.50%, the bank is within FalconBridge's minimum "
                                      "default standard.")),
    # unapproved / wrong calculations, units
    ("operation_not_approved", {}, replace(v6, claim_id="CL-i44", calculation=replace(v6.calculation,
                                                                                      operation="mix_adjusted_gap"))),
    ("calculation", {}, replace(v6, claim_id="CL-i45", calculation=replace(v6.calculation, result=-0.05))),
    ("unit_mismatch", {}, replace(v6, claim_id="CL-i46", evidence_refs=v6.evidence_refs + (
        eid("bank.full.total.total.orig"),), calculation=diff_calc(keys=(FY_CD, "bank.full.total.total.orig"),
                                                                    result=0.0, unit="ratio"))),
    ("unit_mismatch", {}, replace(v2, claim_id="CL-i47", values=(replace(v2.values[0], unit="usd"),))),
    # unsupported conclusions and comparisons
    ("methodology_not_approved", {}, replace(v6, claim_id="CL-i48", text=COMPARE_TEXT + " The bank outperforms the "
                                             "benchmark.")),                               # supported, not approved
    ("direction_contradicts_evidence", {}, replace(v6, claim_id="CL-i49", text=COMPARE_TEXT.replace("lower", "higher"))),
    ("conclusion_unsupported", {}, replace(v2, claim_id="CL-i50", text="The bank's credit quality is strong: % Cum. "
                                           "Defaulted is 2.50%.")),
    ("comparison_unsupported", {}, replace(v2, claim_id="CL-i51", text="The bank's % Cum. Defaulted of 2.50% is lower "
                                           "than the SBA benchmark.")),
    # structural and status integrity
    ("evidence_missing", {}, replace(v2, claim_id="CL-i52", evidence_refs=(), values=())),
    ("value_unsupported", {}, replace(v2, claim_id="CL-i53", evidence_refs=(eid("bank.full.total.total.loans"),))),
    ("status_elevated", {}, replace(v2, claim_id="CL-i54", validation_status=IVF)),       # no validation record
    ("methodology_mismatch", {}, replace(v2, claim_id="CL-i55", methodology=APR)),
    ("value_unsupported", {}, claim("CL-i56", T.QUESTION, "Why were 45 loans charged off?",
                                    ("bank.full.total.total.chgoff",))),
    ("structure", {}, replace(v2, claim_id="CL-i57", claim_type="OPINION")),
]


@pytest.mark.parametrize("pack,c", VALID, ids=[c.claim_id for _, c in VALID])
def test_valid_claims_are_accepted(pack, c):
    kw = {"policy": PERMISSIVE, "confidential_terms": TERMS} if c.exportable else {}
    v = VF.verify_claim(pack, c, **kw)
    assert v.outcome is Outcome.ACCEPTED, v.explanation
    assert v.findings == () and v.explanation == "all checks passed"


@pytest.mark.parametrize("rule,kw,c", INVALID, ids=[c.claim_id for _, _, c in INVALID])
def test_invalid_claims_are_rejected_or_flagged_with_a_specific_reason(rule, kw, c):
    v = VF.verify_claim(PK, c, **kw)
    assert v.outcome is not Outcome.ACCEPTED, c.claim_id
    assert rule in {f.rule for f in v.findings}, (c.claim_id, [(f.rule, f.message) for f in v.findings])
    assert all(f.message for f in v.findings)


def test_suite_size_and_outcome_classes():
    assert len(VALID) >= 20 and len(INVALID) >= 40
    outcomes = {c.claim_id: VF.verify_claim(PK, c, **kw).outcome for _, kw, c in INVALID}
    assert outcomes["CL-i48"] is Outcome.NEEDS_ANALYST_REVIEW            # numerically supported, methodology unapproved
    assert sum(o is Outcome.REJECTED for o in outcomes.values()) == len(INVALID) - 1


def test_comparative_terms_follow_evidence_not_a_word_ban():
    """The same evaluative sentence is accepted with approved methodology, sent to review when the methodology is
    preliminary, and rejected when the evidence contradicts it or the population is a small sample."""
    text = ("For the FY2000+ comparison, the bank's % Cum. Defaulted is better than the SBA benchmark: 2.50% versus "
            "6.50%, 4.00 percentage points lower.")
    ok = VALID[18][1]
    assert VF.verify_claim(PK_OK, ok).outcome is Outcome.ACCEPTED
    prelim = replace(VALID[5][1], claim_id="CL-c2", text=text)
    assert VF.verify_claim(PK, prelim).outcome is Outcome.NEEDS_ANALYST_REVIEW
    worse = replace(ok, claim_id="CL-c3", text=text.replace("better", "worse"))
    assert "direction_contradicts_evidence" in {f.rule for f in VF.verify_claim(PK_OK, worse).findings}


def test_verification_reports_effective_status_and_required_caveats():
    v = VF.verify_claim(PK, VALID[5][1])
    assert v.effective_validation_status is PRE and v.effective_methodology is CDP
    assert set(v.required_caveats) == set(CMP_CAV)
    d = v.to_dict()
    assert d["outcome"] == "ACCEPTED" and d["required_caveats"] == sorted(c.value for c in CMP_CAV)
    vr = VF.verify_claim(PK_VR, VALID[17][1])
    assert vr.effective_validation_status is IVF                         # only with a validation record


def test_changing_any_number_in_a_valid_claim_is_caught():
    """Mutation check: the valid claims are not accepted vacuously. Altering one digit of the first number written in
    each claim (a value or a year) must stop it from being accepted."""
    import re
    mutated = 0
    for pack, c in VALID:
        m = re.search(r"(?<![\w.-])(\d+(?:\.\d+)?)", c.text.replace("FY2000", ""))
        if not m:
            continue
        s = m.group(1)
        bad = replace(c, text=c.text.replace(s, s[:-1] + str((int(s[-1]) + 3) % 10), 1))
        kw = {"policy": PERMISSIVE, "confidential_terms": TERMS} if c.exportable else {}
        assert VF.verify_claim(pack, bad, **kw).outcome is not Outcome.ACCEPTED, c.claim_id
        mutated += 1
    assert mutated >= 20


def test_duplicate_claim_ids_are_rejected():
    out = VF.verify_claims(PK, [VALID[1][1], VALID[1][1]])
    assert out[0].outcome is Outcome.ACCEPTED and out[1].outcome is Outcome.REJECTED


def test_verifier_is_offline_and_never_modifies_evidence(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    before = PK.fingerprint()
    for pack, c in VALID:
        VF.verify_claim(pack, c, policy=PERMISSIVE, confidential_terms=TERMS)
    for _, kw, c in INVALID:
        VF.verify_claim(PK, c, **kw)
    assert PK.fingerprint() == before
