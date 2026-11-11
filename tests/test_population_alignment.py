"""Verifier m2-1.2: evidence-based population alignment for bank vs SBA benchmark comparisons.

Every comparison must pair the SAME measure for the SAME segment (loan size group; total / vintage year / industry).
A full-history bank figure may stand against the FY2000+ SBA benchmark only when the evidence proves the segment's
FY2000+ bank population is identical: a subset with the same loan count AND the same original amount. Equal rates prove
nothing. Real Shoreham (pre-FY2000 loans) and US Metro (none) analyses come from the existing engine; synthetic packs
are altered in memory for adversarial cases. Offline; nothing is written."""
import hashlib
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FBX_APP_DATA", tempfile.mkdtemp(prefix="fbx_align_test_"))
from intelligence import investigation as INV        # noqa: E402
from intelligence import verifier as VF              # noqa: E402
from intelligence.claims import Calculation, Claim, ClaimType as T, Outcome, ValueUse   # noqa: E402
from intelligence.evidence import EvidencePack, Methodology as ME   # noqa: E402
from webapp import analysis as AN                    # noqa: E402
from webapp import generator_bridge as GB            # noqa: E402
from webapp import intel_adapter as IA               # noqa: E402
from webapp import synthetic_analysis as SY          # noqa: E402

RELEASE = ROOT / "source_data" / "20260630"
real = pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
LIVE_DIR = ROOT / "reports" / "live_experiment_2026-11-06"
SYN = IA.extract(SY.synthetic_saved())


def _real(name, state, cert):
    payload = AN.compute("20260630", GB.resolve("20260630", name, state, cert), GB.GC.BENCHMARK_STANDARD_MIN)
    return IA.extract({"id": hashlib.sha256(cert.encode()).hexdigest()[:32], "created": "2026-11-06T00:00:00",
                       "criteria": payload["criteria"], "payload": payload})


@pytest.fixture(scope="module")
def shoreham():
    return _real("Shoreham Bank", "RI", "27417")


@pytest.fixture(scope="module")
def metro():
    return _real("US Metro Bank", "CA", "58310")


def val(pk, k):
    return INV._resolve(pk, k).value


def claim(pk, keys, text=None, calc=None, ctype=T.COMPARISON, version=None):
    recs = [INV._resolve(pk, k) for k in keys]
    if text is None:
        text = " and ".join(f"{val(pk, k) * 100:.2f}%" for k in keys if INV._resolve(pk, k).unit == "ratio")
        text = f"Figures cited: {text}."
    cc = None
    if calc:
        a, b = (INV._resolve(pk, k) for k in calc)
        cc = Calculation("difference", (a.evidence_id, b.evidence_id), a.value - b.value, "ratio")
    c = Claim("CL-a", ctype, pk.header["source_id"], tuple(pk.header["bank"]), pk.header["as_of"], text,
              tuple(r.evidence_id for r in recs), tuple(ValueUse(r.evidence_id, r.value, r.unit) for r in recs), cc,
              VF.effective_status(recs), VF.effective_methodology(recs),
              tuple(sorted(VF.required_caveats(pk, recs), key=lambda x: x.value)))
    return VF.verify_claim(pk, c, version=version)


def rules(v):
    return {f.rule for f in v.findings}


# ---------------------------------------------------------------------------------------------- real populations
@real
def test_shoreham_1999_vintage_is_a_genuine_population_mismatch(shoreham):
    v = claim(shoreham, ("bank.full.total.vintage.1999.cum_def", "benchmark.total.vintage.1999.cum_def"))
    assert v.outcome is Outcome.REJECTED and rules(v) == {"population_mismatch"}
    msg = v.findings[0].message
    assert "52 loans" in msg and "9 loans" in msg and "approved before FY2000" in msg
    ok = claim(shoreham, ("bank.fy2000.total.vintage.1999.cum_def", "benchmark.total.vintage.1999.cum_def"))
    assert "population_mismatch" not in rules(ok) and "SMALL_SAMPLE" in [c.value for c in ok.required_caveats]


@real
def test_shoreham_pre_1999_vintages_and_totals_are_rejected(shoreham):
    v = claim(shoreham, ("bank.full.total.vintage.1995.cum_def", "benchmark.total.vintage.1999.cum_def"))
    assert {"population_mismatch", "segment_mismatch"} <= rules(v)
    v = claim(shoreham, ("bank.full.total.total.cum_def", "benchmark.total.total.cum_def"))
    assert rules(v) == {"population_mismatch"} and "1,187 loans" in v.findings[0].message \
        and "932 loans" in v.findings[0].message


@real
def test_identical_populations_are_accepted_for_vintages_industries_and_totals(shoreham, metro):
    assert not rules(claim(shoreham, ("bank.full.total.vintage.2005.cum_def", "benchmark.total.vintage.2005.cum_def")))
    assert not rules(claim(metro, ("bank.full.total.total.cum_def", "benchmark.total.total.cum_def")))
    ind = "Hotels (except Casino Hotels) and Motels"
    assert not rules(claim(metro, (f"bank.full.total.industry.{ind}.cum_def", f"benchmark.total.industry.{ind}.cum_def")))
    assert not rules(claim(metro, ("bank.full.above.total.cum_def", "benchmark.above.total.cum_def")))
    v = claim(metro, ("bank.fy2000.total.total.cum_def", "benchmark.cre.total.cum_def"))   # bank not split by term
    assert "segment_mismatch" in rules(v) and "not split by loan term" in v.findings[0].message


# --------------------------------------------------------------------------------------------- segment alignment
@pytest.mark.parametrize("keys", [
    ("bank.fy2000.total.vintage.2008.cum_def", "benchmark.total.vintage.2010.cum_def"),         # vintage year
    ("bank.fy2000.below.total.cum_def", "benchmark.total.total.cum_def"),                      # loan size group
    ("bank.fy2000.above.total.cum_def", "benchmark.below.total.cum_def"),
    ("bank.fy2000.total.industry.Hotels (except Casino Hotels) and Motels.cum_def",
     "benchmark.total.industry.Full-Service Restaurants.cum_def"),                             # industry
    ("bank.fy2000.total.total.cum_loss", "benchmark.total.total.cum_def"),                     # measure / denominator
    ("bank.fy2000.total.total.severity", "benchmark.total.total.cum_loss"),                    # numerator vs denominator
    ("bank.fy2000.total.total.cum_def", "benchmark.total.vintage.2008.cum_def"),               # total vs vintage
])
def test_mismatched_segments_and_measures_are_rejected(keys):
    v = claim(SYN, keys)
    assert v.outcome is Outcome.REJECTED and "segment_mismatch" in rules(v)


def test_a_declared_difference_must_pair_like_with_like():
    keys = ("bank.fy2000.total.vintage.2008.cum_def", "benchmark.total.vintage.2008.cum_def",
            "bank.fy2000.total.vintage.2010.cum_def", "benchmark.total.vintage.2010.cum_def")
    assert not rules(claim(SYN, keys, calc=keys[:2]))
    assert "segment_mismatch" in rules(claim(SYN, keys, calc=(keys[0], keys[3])))     # 2008 bank minus 2010 SBA


def test_benchmark_only_statements_and_questions_are_not_comparisons():
    lim = claim(SYN, ("benchmark.total.total.cum_def", "compare.below.total.cum_def.diff"),
                text="All benchmark figures are preliminary and the comparisons are not mix-adjusted.",
                ctype=T.LIMITATION)
    assert not rules(lim) & {"segment_mismatch", "population_mismatch"}
    q = claim(SYN, ("bank.fy2000.total.vintage.2008.cum_def", "benchmark.total.vintage.2010.cum_def"),
              text="Why does the 2008 vintage differ from later benchmark vintages?", ctype=T.QUESTION)
    assert not rules(q) & {"segment_mismatch", "population_mismatch"}


# ------------------------------------------------------------------------------ identity: counts and amounts only
def _altered(changes):
    recs = tuple(replace(r, value=changes[r.metric_key]) if r.metric_key in changes else r for r in SYN.records)
    return EvidencePack(SYN.header, recs)


V08 = ("bank.full.total.vintage.2008.cum_def", "benchmark.total.vintage.2008.cum_def")


def test_identity_is_proven_by_counts_and_amounts_in_the_synthetic_pack():
    assert val(SYN, "bank.full.total.vintage.2008.loans") == val(SYN, "bank.fy2000.total.vintage.2008.loans") == 10
    assert not rules(claim(SYN, V08))


def test_equal_rates_do_not_prove_identity():
    pk = _altered({"bank.fy2000.total.vintage.2008.loans": 7, "bank.fy2000.total.vintage.2008.orig": 4_200_000.0})
    assert val(pk, "bank.full.total.vintage.2008.cum_def") == val(pk, "bank.fy2000.total.vintage.2008.cum_def")
    v = claim(pk, V08)
    assert rules(v) == {"population_mismatch"} and "10 loans" in v.findings[0].message and "7 loans" in v.findings[0].message


def test_equal_counts_with_different_amounts_and_broken_subsets_are_rejected():
    pk = _altered({"bank.fy2000.total.vintage.2008.orig": 5_900_000.0})
    assert rules(claim(pk, V08)) == {"population_mismatch"}
    pk = _altered({"bank.fy2000.total.vintage.2008.loans": 11})
    v = claim(pk, V08)
    assert rules(v) == {"population_mismatch"} and "not a subset" in v.findings[0].message
    pk = EvidencePack(SYN.header, tuple(r for r in SYN.records
                                        if not r.metric_key.startswith("bank.fy2000.total.vintage.2008.")))
    v = claim(pk, V08)
    assert rules(v) == {"population_mismatch"} and "none were approved FY2000 onward" in v.findings[0].message


def test_text_must_describe_the_population_it_cites():
    pk = _altered({"bank.fy2000.total.vintage.2008.loans": 7, "bank.fy2000.total.vintage.2008.orig": 4_200_000.0})
    v = claim(pk, ("bank.full.total.vintage.2008.cum_def",), ctype=T.OBSERVATION,
              text="For the 2008 vintage approved FY2000 onward the bank's % Cum. Defaulted is 46.67%.")
    assert "population_mismatch" in rules(v)
    assert "population_mismatch" in rules(claim(pk, ("bank.fy2000.total.vintage.2008.cum_def",),
                                                text="Over the bank's full history the 2008 vintage % Cum. Defaulted "
                                                     "is 46.67%.", ctype=T.OBSERVATION))


# ------------------------------------------------------------------------------ caveats and methodology preserved
def test_aligned_comparisons_keep_every_methodology_caveat():
    v = claim(SYN, V08, calc=V08, text="Loans first disbursed in 2008 have a % Cum. Defaulted of 46.67%, 25.67 "
                                       "percentage points higher than the benchmark's 21.00%.")
    assert v.outcome is Outcome.ACCEPTED
    assert {"PRELIMINARY_BENCHMARK", "NOT_MIX_ADJUSTED"} <= {c.value for c in v.required_caveats}
    assert v.effective_methodology is ME.CLIENT_DIRECTED_PRELIMINARY            # never methodology approval
    bm = INV._resolve(SYN, "benchmark.total.vintage.2008.cum_def")
    assert any("date-based" in x for x in bm.limitations)                     # default-definition difference kept
    better = claim(SYN, V08, calc=V08, text="The 2008 vintage is worse than the benchmark: 46.67% against 21.00%.")
    assert better.outcome is Outcome.NEEDS_ANALYST_REVIEW                     # evaluation still needs approval


def test_versions_reproduce_their_own_rules():
    assert "population_mismatch" in rules(claim(SYN, V08, version="m2-1.1"))   # label rule, kept for its records
    assert not rules(claim(SYN, V08, version="m2-1.2"))
    assert not rules(claim(SYN, V08, version="m2-1.3"))     # m2-1.3 adds benchmark identity only (Phase A, 2026-11-09)
    assert VF.VERIFIER_VERSION == "m2-1.3" and "m2-1.2" in VF.VERSIONS


# ----------------------------------------------------------------------------------- the first live run baseline
@pytest.mark.skipif(not (LIVE_DIR / "live_investigation_audit.json").exists(), reason="live record not present")
def test_live_record_under_m2_1_2():
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in LIVE_DIR.glob("*.json")}
    bundle = json.loads((LIVE_DIR / "live_investigation_audit.json").read_text())
    inv, diffs = INV.replay_bundle(bundle, SYN)
    assert diffs == [] and inv.verifier_version == "m2-1.0"
    r = INV.reassess_bundle(bundle, SYN, version="m2-1.2")
    changed = {i["item_id"]: (i["revised"]["verifier_outcome"], [f.split(":")[0] for f in i["revised"]["findings"]])
               for i in r["items"] if i["changed"]}
    assert changed == {"AI-04": ("REJECTED", ["denominator_scope"]), "AI-14": ("ACCEPTED", [])}
    m11 = INV.reassess_bundle(bundle, SYN, version="m2-1.1")
    back = {i["item_id"] for i, j in zip(m11["items"], r["items"]) if i["revised"]["verifier_outcome"] !=
            j["revised"]["verifier_outcome"]}
    assert back == {"AI-01", "AI-02", "AI-10", "AI-11", "AI-12", "AI-13", "AI-14", "AI-15"}
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in LIVE_DIR.glob("*.json")}
    assert before == after
