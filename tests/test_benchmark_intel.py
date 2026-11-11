"""Benchmark Intelligence Phase A: B0 (unchanged), B1, B2, industry contributions, independent validation, evidence
reconciliation and claim verification (m2-1.3 benchmark identity).

Saved Analyses are computed by the existing engine from the 2026-06-30 release into a TEMPORARY app-data folder; the
project's own app_data/ is never read or written. Offline: no network, no AI.
"""
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FBX_APP_DATA", tempfile.mkdtemp(prefix="fbx_bench_test_"))
from fastapi.testclient import TestClient   # noqa: E402

from intelligence import cleanroom_benchmark as CB   # noqa: E402
from intelligence import verifier as VF              # noqa: E402
from intelligence.claims import Calculation, Claim, ClaimType as T, Outcome, ValueUse  # noqa: E402
from intelligence.evidence import EvidenceError, sha256_of   # noqa: E402
from webapp import analysis as AN            # noqa: E402
from webapp import app as A                  # noqa: E402
from webapp import benchmark_intel as BI     # noqa: E402
from webapp import benchmark_view as BV      # noqa: E402
from webapp import generator_bridge as GB    # noqa: E402
from webapp import presentation as PR        # noqa: E402
from webapp import saved as SV               # noqa: E402
from webapp import settings                  # noqa: E402

RELEASE = ROOT / "source_data" / "20260630"
pytestmark = pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
client = TestClient(A.app)
BANKS = {"metro": ("US Metro Bank", "CA", "58310"), "finwise": ("FinWise Bank", "UT", "35323"),
         "shoreham": ("Shoreham Bank", "RI", "27417"), "tiny": ("National City Bank of the Midwest", "MI", "5029")}
TOL = 1e-12


def test_uses_a_temporary_app_data_folder():
    assert settings.APP_DATA_DIR != (ROOT / "app_data").resolve()


@pytest.fixture(scope="module")
def saved():
    out = {}
    for k, (name, state, cert) in BANKS.items():
        sel = GB.resolve("20260630", name, state, cert)
        out[k] = SV.save(settings.LOCAL_USER, AN.compute("20260630", sel, GB.GC.BENCHMARK_STANDARD_MIN), "0" * 32,
                         "both")
    return out


@pytest.fixture(scope="module")
def content(saved):
    return {k: BI.compute(saved[k]) for k in ("metro", "finwise", "shoreham")}


@pytest.fixture(scope="module")
def validated(saved, content):
    """Freeze each record and validate it independently from the raw FOIA files (temporary app data)."""
    out = {}
    for k, c in content.items():
        rid, frozen = BI.freeze(saved[k])
        assert frozen == c
        rec = CB.produce(rid, c, str(RELEASE), "2026-11-09")
        CB.save_record(str(settings.APP_DATA_DIR), saved[k]["id"], rec)
        out[k] = (rid, rec)
    return out


# ------------------------------------------------------------------------------------------- B0 / B1 / B2
@pytest.mark.parametrize("bank", ["metro", "finwise", "shoreham"])
def test_b0_is_the_frozen_benchmark_unchanged(saved, content, bank):
    p, c = saved[bank]["payload"], content[bank]
    assert c["B0"]["cum_def"] == p["benchmark"]["total"]["total"]["cum_def"]          # identical, not recomputed
    assert c["B0"]["loans"] == p["benchmark"]["total"]["total"]["loans"]
    assert abs(c["B0"]["defaulted_orig"] / c["B0"]["orig"] - c["B0"]["cum_def"]) < TOL   # pooled ratio = SUMPRODUCT
    assert c["bank_fy2000"]["cum_def"] == p["bank"]["fy2000"]["total"]["total"]["cum_def"]
    assert c["B1"]["loans"] == p["benchmark_population"]["loans"]


def test_shoreham_golden_values(content):
    """Pinned from the independent read-only investigation of 2026-11-09 (raw CSVs, csv module only)."""
    c = content["shoreham"]
    assert abs(c["B0"]["cum_def"] - 0.06823110572457414) < TOL and c["B0"]["loans"] == 1_001_823
    assert c["B0"]["defaulted_orig"] == 23_239_331_034 and c["B0"]["orig"] == 340_597_309_500
    assert abs(c["B1"]["cum_def"] - 31_599_993_447 / 459_030_841_579) < TOL and c["B1"]["loans"] == 1_402_629
    assert abs(c["bank_fy2000"]["cum_def"] - 51_794_100 / 1_097_412_800) < TOL and c["bank_fy2000"]["loans"] == 932
    assert c["B0"]["industries"] == 237 and c["population"]["industries_pre_fy2000_only"] == 33
    b2 = c["B2"]
    assert b2["computed"] and len(b2["excluded"]) == 2 and abs(b2["sparse_share"] - (1 - b2["coverage"])) < TOL
    assert round(b2["cum_def"], 6) == 0.046848          # 4.6839% before excluding the two sparse industries


@pytest.mark.parametrize("bank", ["metro", "finwise", "shoreham"])
def test_contribution_identities_are_exact(content, bank):
    c = content[bank]
    ind, d, B0, B2, R = c["industries"], c["decomposition"], c["B0"]["cum_def"], c["B2"]["cum_def"], \
        c["bank_fy2000"]["cum_def"]
    assert abs(sum(x["bank_share"] for x in ind) - 1) < TOL and abs(sum(x["sba_share"] for x in ind) - 1) < TOL
    # B2 computed with the SBA's own weights reproduces B0 exactly (the two differ only in weighting)
    assert abs(sum(x["sba_share"] * x["sba_cum_def"] for x in ind if x["sba_cum_def"] is not None) - B0) < TOL
    assert abs(sum(x["b2_weight"] for x in ind) - 1) < TOL
    assert abs(d["mix_total"] - (B2 - B0)) < TOL
    assert abs(d["within_total"] - (c["B2"]["covered_bank_cum_def"] - B2)) < TOL
    assert abs(d["mix_total"] + d["within_total"] + d["sparse_adjustment"] - (R - B0)) < TOL
    assert abs(d["gap"] - (R - B0)) < TOL


def test_lender_without_fy2000_loans_is_refused_not_estimated(saved):
    with pytest.raises(BI.BenchmarkIntelError, match="no loans approved FY2000"):
        BI.compute(saved["tiny"])


# ------------------------------------------------------------------- sparse / coverage rules (synthetic data)
def _synthetic(sba_rows, bank_rows):
    """sba_rows: {industry: (loans, orig, defaulted_orig)}; bank_rows: {industry: (loans, orig, defaulted_orig)}."""
    rows = [[n, 2010, "Small", "Non-CRE", L, 0, 0, O, 0, D, 0, 0, 0, 0] for n, (L, O, D) in sba_rows.items()]
    rows.append(["Other", 2010, "Small", "Non-CRE", 1000, 0, 0, 10**9, 0, 5 * 10**7, 0, 0, 0, 0])
    names = list(sba_rows)
    sec = [{"name": n, "loans": sba_rows[n][0], "orig": sba_rows[n][1],
            "cum_def": sba_rows[n][2] / sba_rows[n][1]} for n in names]
    N, D = sum(v[1] for v in sba_rows.values()), sum(v[2] for v in sba_rows.values())
    bt = {"loans": sum(v[0] for v in bank_rows.values()), "orig": sum(v[1] for v in bank_rows.values()),
          "chgoff_orig": sum(v[2] for v in bank_rows.values())}
    bt["cum_def"] = bt["chgoff_orig"] / bt["orig"]
    inds = [{"name": n, "loans": bank_rows.get(n, (0, 0, 0))[0], "orig": bank_rows.get(n, (0, 0, 0))[1],
             "chgoff_orig": bank_rows.get(n, (0, 0, 0))[2], "naics": ""} for n in names]
    crit = {"period": "20260630", "as_of": "2026-06-30", "bank_name": "Synthetic", "bank_state": "ZZ", "cert": "1",
            "location_ids": ["1"], "threshold": 350000}
    p = {"criteria": crit, "benchmark": {"total": {"total": {"loans": sum(v[0] for v in sba_rows.values()), "orig": N,
                                                             "cum_def": D / N}, "sector": sec}},
         "bank": {"fy2000": {"total": {"total": bt, "industry": inds}}, "full": {"total": {"total": bt}}},
         "benchmark_population": {"loans": sum(r[4] for r in rows), "orig": sum(r[7] for r in rows)}}
    return {"id": "f" * 32, "created": "2026-11-09T00:00:00", "payload": p}, {"rows": rows}


def test_sparse_industries_are_excluded_and_listed_never_substituted():
    s, b = _synthetic({"A": (500, 10**8, 5 * 10**6), "B": (12, 10**6, 10**5)},
                      {"A": (40, 9.8 * 10**7, 10**6), "B": (2, 2 * 10**6, 0)})
    c = BI.compute(s, b)
    assert c["B2"]["excluded"] == ["B"] and abs(c["B2"]["coverage"] - 0.98) < TOL
    assert abs(c["B2"]["cum_def"] - 0.05) < TOL                          # only A's SBA rate, renormalised weight 1
    assert abs(c["decomposition"]["sparse_adjustment"] - (c["bank_fy2000"]["cum_def"] - 10**6 / (9.8 * 10**7))) < TOL


def test_b2_not_computed_below_the_coverage_minimum():
    s, b = _synthetic({"A": (500, 10**8, 5 * 10**6), "B": (12, 10**6, 10**5)},
                      {"A": (40, 9 * 10**7, 10**6), "B": (2, 10**7, 0)})        # 10% of the bank in a sparse industry
    c = BI.compute(s, b)
    assert not c["B2"]["computed"] and c["B2"]["cum_def"] is None and c["decomposition"] is None
    assert abs(c["B2"]["coverage"] - 0.9) < TOL and c["B2"]["excluded"] == ["B"]


def test_inputs_that_do_not_reconcile_are_refused():
    s, b = _synthetic({"A": (500, 10**8, 5 * 10**6)}, {"A": (40, 10**8, 10**6)})
    s["payload"]["benchmark"]["total"]["total"]["cum_def"] = 0.06            # frozen B0 no longer matches its rows
    with pytest.raises(BI.BenchmarkIntelError):
        BI.compute(s, b)


# ------------------------------------------------------------------------------------- independent validation
def test_clean_room_module_imports_no_application_code():
    code = ("import sys, intelligence.cleanroom_benchmark; "
            "print([m for m in sys.modules if m.split('.')[0] in ('webapp', 'bank_workbook_generator', 'benchmark', "
            "'config', 'source_data', 'threshold_analysis')])")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"


@pytest.mark.parametrize("bank", ["metro", "finwise", "shoreham"])
def test_every_value_is_independently_verified(saved, validated, bank):
    rid, rec = validated[bank]
    m = BV.build(saved[bank])
    pack = m["pack"]
    assert pack.header["source_id"] == rid and rec.record_id in pack.header["validation_records"]
    vals = {r.metric_key: r.value for r in pack.records}
    agree, disagree, missing = CB.compare(rec, vals)
    assert not disagree and missing == ["bench_intel.bank_full.cum_def"]     # validated by the Saved Analysis's record
    assert agree == len(rec.values) and len(rec.values) > 1000
    keys = dict(rec.values)
    assert all(k in keys for k in ("bench_intel.B0.cum_def", "bench_intel.B1.cum_def", "bench_intel.B2.cum_def"))
    assert any(".bank_share" in k for k in keys) and any(".sba_cum_def" in k for k in keys)     # weights and rates
    assert m["reconciliation"]["frozen"] == "matches" and m["reconciliation"]["b0_identical"]
    assert all(r.numerically_verified for r in pack.records if r.metric_key != "bench_intel.bank_full.cum_def")


def test_altered_records_are_refused(saved, validated):
    d = BI.folder(settings.APP_DATA_DIR, saved["shoreham"]["id"])
    f = next(d.glob("BI-*.json"))
    doc = json.loads(f.read_text())
    doc["content"]["B1"]["cum_def"] = 0.05
    bad = d.parent / "tampered"
    bad.mkdir(exist_ok=True)
    (bad / f.name).write_text(json.dumps(doc))
    with pytest.raises(EvidenceError):
        BI.load_frozen(settings.APP_DATA_DIR, "tampered")


def test_validation_for_a_different_record_does_not_apply(saved, content, validated):
    rid, rec = validated["shoreham"]
    other = dict(content["shoreham"], version="bench-intel-x")                 # any content change = new record
    pack = BV.build_pack(BI.record_id(other), other, [rec])
    assert not pack.header["validation_records"] and not any(r.numerically_verified for r in pack.records)


# ------------------------------------------------------------------------------------- claims verification
def _claim(pack, text, keys, ctype=T.OBSERVATION, calc=None, version=None):
    recs = [pack.by_key(k) for k in keys]
    c = Claim("CL-test", ctype, pack.header["source_id"], tuple(pack.header["bank"]), pack.header["as_of"], text,
              tuple(r.evidence_id for r in recs), tuple(ValueUse(r.evidence_id, r.value, r.unit) for r in recs),
              calc(recs) if calc else None, VF.effective_status(recs), VF.effective_methodology(recs),
              tuple(sorted(VF.required_caveats(pack, recs), key=lambda x: x.value)))
    return VF.verify_claim(pack, c, version=version)


def _rules(v):
    return {f.rule for f in v.findings}


@pytest.fixture(scope="module")
def bpack(saved, validated):
    return BV.build(saved["shoreham"])["pack"]


def test_view_statements_all_pass_verification(saved, validated):
    for k in ("metro", "finwise", "shoreham"):
        st = BV.build(saved[k])["statements"]
        assert len(st) >= 5 and all(s["outcome"] == "ACCEPTED" for s in st), [s for s in st if s["outcome"] != "ACCEPTED"]


def test_b1_or_b2_cannot_be_presented_as_the_sba_benchmark(bpack):
    v = _claim(bpack, "The SBA benchmark % Cum. Defaulted is 6.88%.", ["bench_intel.B1.cum_def"])
    assert v.outcome is Outcome.REJECTED and "benchmark_label" in _rules(v)
    v = _claim(bpack, "The SBA benchmark % Cum. Defaulted is 4.68%.", ["bench_intel.B2.cum_def"])
    assert v.outcome is Outcome.REJECTED and "benchmark_label" in _rules(v)
    ok = _claim(bpack, "Investigative all-SBA comparison (every industry): % Cum. Defaulted 6.88%.",
                ["bench_intel.B1.cum_def"])
    assert ok.outcome is Outcome.ACCEPTED, ok.findings


def test_labels_cannot_be_swapped_between_comparisons(bpack):
    v = _claim(bpack, "The all-SBA comparison (every industry) has a % Cum. Defaulted of 4.68%.",
               ["bench_intel.B2.cum_def"])
    assert "benchmark_label" in _rules(v)
    v = _claim(bpack, "Weighted to the bank's industry mix, the SBA % Cum. Defaulted is 6.82%.",
               ["bench_intel.B0.cum_def"])
    assert "benchmark_label" in _rules(v)
    v = _claim(bpack, "All SBA 7(a) loans have a % Cum. Defaulted of 6.82%.", ["bench_intel.B0.cum_def"])
    assert "benchmark_label" in _rules(v)
    ok = _claim(bpack, "The industry-matched SBA benchmark (preliminary) has a % Cum. Defaulted of 6.82%.",
                ["bench_intel.B0.cum_def"])
    assert "benchmark_label" not in _rules(ok)


def test_populations_cannot_be_mixed(bpack):
    v = _claim(bpack, "The bank's full-history % Cum. Defaulted is 4.63% and the all-SBA comparison (every industry) "
               "is 6.88%.", ["bench_intel.bank_full.cum_def", "bench_intel.B1.cum_def"], T.COMPARISON)
    assert "population_mismatch" in _rules(v)
    v = _claim(bpack, "Over the bank's full history the % Cum. Defaulted is 4.72%.", ["bench_intel.bank_fy2000.cum_def"])
    assert "population_mismatch" in _rules(v)


def test_differences_must_pair_the_same_measure(bpack):
    calc = lambda r: Calculation("difference", (r[0].evidence_id, r[1].evidence_id), r[0].value - r[1].value, "ratio")  # noqa
    v = _claim(bpack, "The all-SBA comparison (every industry) minus coverage is 0.0.", ["bench_intel.B1.cum_def",
               "bench_intel.B2.coverage"], T.CALCULATION, calc)
    assert v.outcome is Outcome.REJECTED and "segment_mismatch" in _rules(v)


def test_no_verdict_against_investigative_comparisons(bpack):
    v = _claim(bpack, "For loans approved FY2000 onward the bank's % Cum. Defaulted is lower than the comparison "
               "weighted to the bank's industry mix.", ["bench_intel.bank_fy2000.cum_def", "bench_intel.B2.cum_def"],
               T.COMPARISON)
    assert v.outcome is not Outcome.ACCEPTED


def test_main_claims_verification_rejects_calling_b0_all_sba(saved):
    pack = PR.pack_for(saved["shoreham"])
    text = "The All SBA 7(a) % Cum. Defaulted is 6.82%."
    v = _claim(pack, text, ["benchmark.total.total.cum_def"])
    assert v.outcome is Outcome.REJECTED and "benchmark_label" in _rules(v)
    old = _claim(pack, text, ["benchmark.total.total.cum_def"], version="m2-1.2")      # earlier records replay as before
    assert "benchmark_label" not in _rules(old)
    assert VF.VERIFIER_VERSION == "m2-1.3" and "m2-1.2" in VF.VERSIONS


# --------------------------------------------------------------------------------------------- view and route
def test_route_is_off_unless_enabled(saved, monkeypatch):
    monkeypatch.setattr(BV, "ENABLED", False)
    assert client.get(f"/analyses/{saved['shoreham']['id']}/benchmarks").status_code == 404


def test_view_renders_labels_status_and_reconciliation(saved, validated, monkeypatch):
    monkeypatch.setattr(BV, "ENABLED", True)
    r = client.get(f"/analyses/{saved['shoreham']['id']}/benchmarks")
    assert r.status_code == 200
    t = r.text
    for s in ("6.82%", "6.88%", "4.68%", "4.72%", "investigative comparisons", "Investigative",
              "Current benchmark (preliminary)", "Numerator", "Denominator", "Weighting", "Coverage",
              "Independently verified", "identical", "frozen copy matches", "Hotels (except Casino Hotels) and Motels"):
        assert s in t, s
    assert "approved SBA benchmark" not in t


def test_view_refuses_other_users_and_lenders_without_fy2000_loans(saved, monkeypatch):
    monkeypatch.setattr(BV, "ENABLED", True)
    other = SV.save("someone-else", saved["shoreham"]["payload"], "0" * 32, "both")
    assert client.get(f"/analyses/{other['id']}/benchmarks").status_code == 403
    assert client.get(f"/analyses/{saved['tiny']['id']}/benchmarks").status_code == 409


def test_executive_summary_does_not_change_with_benchmarks_enabled(saved, validated, monkeypatch):
    a = saved["shoreham"]
    monkeypatch.setattr(BV, "ENABLED", False)
    off = client.get(f"/analyses/{a['id']}/executive").text
    monkeypatch.setattr(BV, "ENABLED", True)
    on = client.get(f"/analyses/{a['id']}/executive").text
    assert off == on and "/benchmarks" not in on
    assert sha256_of(a["payload"]) == sha256_of(SV.load(a["id"])["payload"])        # Saved Analysis untouched


def test_documented_source_notes_are_shown_with_the_comparisons(saved, monkeypatch):
    notes = saved["finwise"]["payload"].get("source_notes") or []
    if not notes:
        pytest.skip("protected source-notes file absent")
    monkeypatch.setattr(BV, "ENABLED", True)
    t = client.get(f"/analyses/{saved['finwise']['id']}/benchmarks").text
    assert "Source-data note" in t and notes[0]["title"].replace("'", "&#39;") in t
