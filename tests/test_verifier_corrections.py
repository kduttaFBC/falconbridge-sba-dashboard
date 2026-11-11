"""Correction cycle (verifier m2-1.1): arithmetic precision, population and denominator scope, versioned replay,
separate re-assessment of the first live investigation, and calibrated input-token budgeting. Offline; no API call."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from intelligence import investigation as INV
from intelligence import live_anthropic as LA
from intelligence import provider as PV
from intelligence import verifier as VF
from intelligence.claims import Calculation, Claim, ClaimType as T, Operation, ValueUse
from webapp import intel_adapter as IA
from webapp import live_experiment as LX
from webapp import synthetic_analysis as SY

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / "reports" / "live_experiment_2026-11-06" / "live_investigation_audit.json"
live = pytest.mark.skipif(not LIVE.exists(), reason="first live experiment record not present")
PK = IA.extract(SY.synthetic_saved())
B08, S08 = "bank.fy2000.total.vintage.2008.cum_def", "benchmark.total.vintage.2008.cum_def"     # 0.4667 - 0.21


def rec(key):
    return INV._resolve(PK, key)


def claim(text, keys, calc=None, ctype=T.COMPARISON):
    recs = [rec(k) for k in keys]
    return Claim("CL-t", ctype, PK.header["source_id"], tuple(PK.header["bank"]), PK.header["as_of"], text,
                 tuple(r.evidence_id for r in recs), tuple(ValueUse(r.evidence_id, r.value, r.unit) for r in recs),
                 calc, VF.effective_status(recs), VF.effective_methodology(recs),
                 tuple(sorted(VF.required_caveats(PK, recs), key=lambda c: c.value)))


def diff(result, decimals=None, keys=(B08, S08)):
    return Calculation("difference", tuple(rec(k).evidence_id for k in keys), result, "ratio", decimals)


TEXT = "Loans first disbursed in 2008 have a % Cum. Defaulted of 46.67%, 25.67 percentage points higher than the " \
       "benchmark's 21.00%."


def rules(c, version=None):         # (m2-1.1-specific tests below pin version="m2-1.1")
    return {f.rule for f in VF.verify_claim(PK, c, version=version).findings}


# ------------------------------------------------------------------------------------------- arithmetic precision
@pytest.mark.parametrize("result,decimals,expected", [
    (0.2566666666666667, None, set()),               # exact
    (0.2567, None, set()),                           # rounded to 4 places, as written: accepted
    (0.25667, None, set()),                          # 5 places
    (0.2567, 4, set()),                              # explicit precision agrees with the literal
    (0.2566, None, {"calculation"}),                 # wrong at its own precision
    (0.2568, None, {"calculation"}),
    (-0.2567, None, {"calculation"}),                # sign error
    (0.26, None, {"calculation_precision"}),         # right only because too coarse for a ratio (< 4 places)
    (0.3, None, {"calculation_precision"}),
    (0.2567, 2, {"calculation_precision"}),          # more digits than the declared precision
    (0.2567, 6, {"calculation"}),                    # claims 6 places it does not have
    (0.2567000001, None, {"calculation"}),           # many digits, wrong at that precision
])
def test_rounded_results_are_judged_at_their_declared_precision(result, decimals, expected):
    found = rules(claim(TEXT, (B08, S08), diff(result, decimals))) & {"calculation", "calculation_precision"}
    assert found == expected


def test_precision_boundaries_counts_and_dollars():
    op = Operation.DIFFERENCE
    f = lambda result, true, unit="ratio", d=None: VF._calculation_finding(op, Calculation(op.value, (), result, unit, d),
                                                                            true, unit, "m2-1.1")   # noqa: E731
    assert f(0.1235, 0.12345) is None and f(0.1235, 0.123549) is None        # half a unit of the last place
    assert f(0.1235, 0.12355) is None and f(0.1235, 0.1235501).rule == "calculation"
    assert f(3.0, 3.0, "count") is None and f(3.4, 3.0, "count").rule == "calculation"
    assert f(1000.0, 1000.4, "usd") is None and f(1000.4, 1000.0, "usd").rule == "calculation"
    assert VF._calculation_finding(op, Calculation(op.value, (), 0.2567, "ratio"), 0.2566666, "ratio", "m2-1.0").rule \
        == "calculation"                                                     # the original rule stays available


def test_versions_are_explicit():
    c = claim(TEXT, (B08, S08), diff(0.2567))
    assert "calculation" in rules(c, "m2-1.0") and not rules(c, "m2-1.1")
    with pytest.raises(ValueError):
        VF.verify_claim(PK, c, version="m2-9.9")


# ------------------------------------------------------------------------------------------- population / scope
def test_full_history_is_never_compared_with_the_fy2000_benchmark_even_when_values_coincide():
    full, fy, bm = "bank.full.total.total.cum_def", "bank.fy2000.total.total.cum_def", "benchmark.total.total.cum_def"
    assert rec(full).value == rec(fy).value                                  # identical values in this pack
    text = "The bank's % Cum. Defaulted is 3.07%, against the SBA benchmark's 6.00%."
    v11 = "m2-1.1"                                                           # the m2-1.1 label rule, kept for replay
    assert "population_mismatch" in rules(claim(text, (full, bm)), v11)
    assert "population_mismatch" not in rules(claim(text, (fy, bm)), v11)
    assert "population_mismatch" not in rules(claim(text, (full, bm)), "m2-1.0")
    vint = "Loans first disbursed in 2008 have a % Cum. Defaulted of 46.67% against the benchmark's 21.00%."
    assert "population_mismatch" in rules(claim(vint, ("bank.full.total.vintage.2008.cum_def", S08)), v11)
    q = claim("How does the 3.07% compare with the SBA benchmark?", (full, bm), ctype=T.QUESTION)
    assert "population_mismatch" not in rules(q, v11)                        # a question asserts nothing


def test_text_population_must_match_the_cited_population():
    full, fy = "bank.full.total.total.cum_def", "bank.fy2000.total.total.cum_def"
    assert "population_mismatch" in rules(claim("For loans approved FY2000 onward the % Cum. Defaulted is 3.07%.",
                                                (full,), ctype=T.OBSERVATION), "m2-1.1")
    assert "population_mismatch" in rules(claim("Over the bank's full history the % Cum. Defaulted is 3.07%.",
                                                (fy,), ctype=T.OBSERVATION), "m2-1.1")
    assert not rules(claim("Over the bank's full history the % Cum. Defaulted is 3.07%.", (full,),
                           ctype=T.OBSERVATION)) & {"population_mismatch"}


@pytest.mark.parametrize("text,bad", [
    ("The bank's portfolio has an average age of 43.07 months.", True),
    ("Loans have an average age of 43.07 months.", True),
    ("The bank's estimated outstanding loans have an average age of 43.07 months.", False),
    ("Active loans have an average age of 43.07 months.", False),
])
def test_average_age_must_be_described_as_outstanding_loans(text, bad):
    found = "denominator_scope" in rules(claim(text, ("bank.full.total.total.age",), ctype=T.OBSERVATION))
    assert found is bad


# ------------------------------------------------------------------------------- the first live run as a baseline
@live
def test_live_record_replays_exactly_under_its_own_rules_and_is_not_modified():
    before = hashlib.sha256(LIVE.read_bytes()).hexdigest()
    bundle = json.loads(LIVE.read_text())
    inv, diffs = INV.replay_bundle(bundle, PK)
    assert diffs == [] and inv.verifier_version == "m2-1.0" and len(inv.items) == 21
    assert hashlib.sha256(LIVE.read_bytes()).hexdigest() == before


@live
def test_live_record_reassessment_is_separate_and_explains_every_change():
    bundle = json.loads(LIVE.read_text())
    r = INV.reassess_bundle(bundle, PK, version="m2-1.1", assessed_on="2026-11-06")
    assert r["source_bundle_sha256"] == bundle["bundle_sha256"] and r["revised_verifier_version"] == "m2-1.1"
    stored = LIVE.parent / "reassessment_m2-1.1.json"
    if stored.exists():
        assert json.loads(stored.read_text()) == r                           # the stored m2-1.1 record reproduces
    changed = {i["item_id"]: {f.split(":")[0] for f in i["revised"]["findings"]} for i in r["items"] if i["changed"]}
    assert changed == {"AI-01": {"population_mismatch"}, "AI-02": {"population_mismatch"},
                       "AI-04": {"denominator_scope"}, "AI-10": {"population_mismatch"},
                       "AI-11": {"population_mismatch"}, "AI-12": {"population_mismatch"},
                       "AI-13": {"population_mismatch"}, "AI-14": {"population_mismatch"},
                       "AI-15": {"population_mismatch"}}
    ai14 = next(i for i in r["items"] if i["item_id"] == "AI-14")
    assert "calculation" in ai14["original"]["findings"][0] and \
        not any(f.startswith("calculation") for f in ai14["revised"]["findings"])  # the rounding block is gone
    body = {k: v for k, v in r.items() if k != "reassessment_sha256"}
    assert INV.sha256_of(body) == r["reassessment_sha256"]
    assert bundle == json.loads(LIVE.read_text())                            # original untouched


# ------------------------------------------------------------------------------------------- token budgeting
@live
def test_calibrated_input_estimate_covers_the_observed_run():
    req = json.loads(LIVE.read_text())["investigation"]["request"]
    r = PV.ProviderRequest(req["task"], req["prompt_version"], req["instructions"], req["brief"],
                           req["max_output_tokens"])
    assert r.estimated_input_tokens() < LA.OBSERVED_BASELINE["input_tokens"]  # the old estimate was too low
    est = LA.estimate_input_tokens(r)
    assert est >= LA.OBSERVED_BASELINE["input_tokens"] * 1.4
    assert PV.worst_case_cost(r, LA.DEFAULT_MODEL, est) >= LA.OBSERVED_BASELINE["cost_usd"]


def test_budget_limits_are_unchanged():
    s = LX.preflight("")
    assert s["budget_usd"] == 2.0 and s["call_limit"] == 1 and s["max_tokens"] == 8000
    assert s["worst_case_cost_usd"] < 0.5 and "calibrated" in s["estimate_method"]
    assert PV.HARD_CAP_USD == 2.0 and LX.BUDGET_USD == 2.0 and LA.MAX_OUTPUT_TOKENS == 8000
    with pytest.raises(PV.BudgetExceeded):
        PV.BudgetLedger(2.01)
    est = LA.AnthropicProvider().estimate_cost(LX.build()[1])
    assert est == pytest.approx(s["worst_case_cost_usd"], abs=1e-4)
    assert replace  # (imported for parity with the other suites)
