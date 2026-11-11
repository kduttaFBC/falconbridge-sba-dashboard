"""M1 intelligence evidence layer: Saved Analysis -> evidence records -> deny-by-default egress.

Synthetic Saved Analyses are used wherever possible; the real-data test uses a scratch Saved Analysis (temporary
FBX_APP_DATA) built from the 2026-06-30 release and skips if that release is absent. No network, no API, no writes
outside the temporary folder.
"""
import ast
import copy
import hashlib
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FBX_APP_DATA", tempfile.mkdtemp(prefix="fbx_intel_test_"))
sys.path.insert(0, str(ROOT))
from intelligence import egress as EG                                   # noqa: E402
from intelligence import validation as VAL                              # noqa: E402
from intelligence.evidence import (Classification as CL, EvidenceError, Kind, Methodology as ME,  # noqa: E402
                                   ValidationStatus as VS, make_hypothesis, make_id, validate_record)
from webapp import intel_adapter as IA                                  # noqa: E402

CANARY = "CANARY-7f3a-CONFIDENTIAL"


# ----------------------------------------------------------------------------------------- synthetic Saved Analysis
def _row(loans, orig, chg, chg_orig, chg_amt, **extra):
    r = {"loans": loans, "orig": orig, "guar": orig * 0.75, "unguar": orig * 0.25, "avg": orig / loans if loans else "n/a",
         "active": loans - chg, "active_orig": orig - chg_orig, "age": 30.0 if loans else "n/a", "chgoff": chg,
         "chgoff_orig": chg_orig, "chgoff_amt": chg_amt, "pif_orig": 0.0, "purch_orig": 0,
         "cum_def": chg_orig / orig if orig else "n/a", "cum_loss": chg_amt / orig if orig else "n/a",
         "severity": (chg_amt / chg_orig) if chg_orig else "n/a", "pif_pct": 0.0 if orig else "n/a",
         "purch_pct": 0.0 if orig else "n/a"}
    r.update(extra)
    return r


def _block(scale=1):
    v = [_row(10 * scale, 1_000_000.0 * scale, 1, 100_000.0, 40_000.0, year=2019),
         _row(20 * scale, 3_000_000.0 * scale, 0, 0.0, 0.0, year=2024),
         _row(0, 0.0, 0, 0.0, 0.0, year=2025)]
    i = [_row(25 * scale, 3_500_000.0 * scale, 1, 100_000.0, 40_000.0, name="Hotels (except Casino Hotels) and Motels",
              share=3_500_000.0 / 4_000_000.0, naics="721110"),
         _row(5 * scale, 500_000.0 * scale, 0, 0.0, 0.0, name="Full-Service Restaurants", share=0.125, naics="722511")]
    s = [_row(30 * scale, 4_000_000.0 * scale, 1, 100_000.0, 40_000.0, name="Guaranty", label=CANARY + " label")]
    return {"total": _row(30 * scale, 4_000_000.0 * scale, 1, 100_000.0, 40_000.0), "vintage": v, "industry": i,
            "subprogram": s}


def _split(blk, above_share):
    """above/below groups whose loans add up to the total (consistency rule)."""
    a, b = copy.deepcopy(blk), copy.deepcopy(blk)
    a["total"]["loans"] = round(blk["total"]["loans"] * above_share)
    b["total"]["loans"] = blk["total"]["loans"] - a["total"]["loans"]
    for g, part in ((a, above_share), (b, 1 - above_share)):
        n = g["total"]["loans"]
        g["vintage"][0]["loans"], g["vintage"][1]["loans"] = n, 0
        g["industry"][0]["loans"], g["industry"][1]["loans"] = n, 0
    return a, b


def bench_rows():
    t = {"loans": 900_000, "orig": 3.1e11, "avg": 344_444.0, "active": 200_000, "active_orig": 1.2e11, "chgoff": 110_000,
         "cum_def": 0.065, "severity": 0.66, "cum_loss": 0.043, "purch_pct": 0.0}
    return {"total": t, "vintage": [dict(t, year=2019)], "sector": [dict(t, name="Hotels (except Casino Hotels) and Motels")]}


def synthetic_saved(cert="58310", period="20260630", version="stage1-1.5", sid="a" * 32):
    full, fy = _block(), _block()
    fa, fb = _split(full, 0.6)
    ya, yb = _split(fy, 0.6)
    crit = {"period": period, "as_of": "2026-06-30", "bank_name": "Synthetic Bank", "bank_state": "CA", "cert": cert,
            "location_ids": ["0001"], "status": "UNIQUE", "threshold": 350000, "threshold_label": "350K",
            "default_threshold": True}
    payload = {
        "criteria": crit,
        "identity": {"resolution": "Unique", "selected": [{"cert": cert, "location_id": "0001", "city": CANARY + " city",
                                                           "street": CANARY + " street", "records": 30, "eligible": 30}],
                     "excluded": [], "records": 30, "eligible": 30, "excluded_counts": {}},
        "bank": {"full": {"total": full, "above": fa, "below": fb}, "fy2000": {"total": fy, "above": ya, "below": yb}},
        "benchmark": {"total": bench_rows(), "above": bench_rows(), "below": bench_rows()},
        "comparison": {"total": {"cum_def": {"bank": 0.025, "sba": 0.065, "diff": 0.025 - 0.065},
                                 "loans": {"bank": 30, "sba": 900_000, "diff": 30 - 900_000}}},
        "industry_comparison": {"total": [{"name": "Hotels (except Casino Hotels) and Motels", "loans": 25,
                                           "diff_cum_def": 100_000.0 / 3_500_000.0 - 0.065,
                                           "diff_cum_loss": 40_000.0 / 3_500_000.0 - 0.043}]},
        "concentration": {"total": {"industries": 2, "top1": 0.875, "top5": 1.0, "top10": 1.0}},
        "chgoff_by_year": {g: {"years": [{"year": 2024, "loans": 1, "amount": 40_000.0}], "undated": 0}
                           for g in ("total", "above", "below")},
        "status": {"total": [{"status": "CHGOFF", "loans": 1, "orig": 100_000.0}]},
        "default_line": [{"group": "cre", "sba_loans": 200_000, "sba_cum_def": 0.0377},
                         {"group": "noncre", "sba_loans": 700_000, "sba_cum_def": 0.105}],
        "benchmark_population": {"loans": 1_400_000, "orig": 4.59e11, "industries": 2,
                                 "definition_gaps": {"LoanStatus CHGOFF without ChargeOffDate (benchmark: outstanding)": 20}},
        "seasoning": {"bands": [{"label": "x", "from": 1999, "to": 2008, "bank_share": 0.1, "sba_share": 0.2},
                                {"label": "y", "from": 2022, "to": 2026, "bank_share": 0.5, "sba_share": 0.27}]},
        "partial_year": {"year": 2026, "through": "June 30, 2026", "label": "2026*", "note": "partial"},
        "insights": {"rules": ["r"], "observations": [CANARY + " observation"]},
        "source_notes": [{"text": CANARY + " source note about specific loans"}],
        "glossary": [{"section": "s", "field": "f", "definition": CANARY + " FalconBridge glossary definition"}],
        "group_titles": [CANARY + " title"],
        "release": {"folder": EG.LOCAL_HOME + "someone/" + CANARY + "/source_data/20260630",
                    "files": ["FOIA_7a_" + CANARY + ".csv"]},
        "calculation_version": f"dashboard-1.0 / {version} (synthetic)",
    }
    return {"id": sid, "owner": CANARY + "-owner", "created": "2026-11-05T12:00:00", "criteria": copy.deepcopy(crit),
            "view": "both", "job_id": CANARY + "-job", "payload": payload}


def resolve(payload, pointer):
    cur = payload
    for part in pointer.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


PERMISSIVE = EG.EgressPolicy(approved_classifications=frozenset(
    {CL.PUBLIC_IDENTITY, CL.PUBLIC_FOIA_AGGREGATE, CL.FOIA_AGGREGATE_CLIENT_CORRECTED, CL.CLIENT_METHOD_DERIVED,
     CL.APPLICATION_DERIVED}), approved_by="test", approved_on="2026-11-05", purpose="test only")


# -------------------------------------------------------------------------------------------------------------- tests
def test_identifiers_are_stable_and_independent_of_position():
    s = synthetic_saved()
    a, b = IA.extract(s), IA.extract(copy.deepcopy(s))
    assert a.ids() == b.ids() and a.fingerprint() == b.fingerprint()
    r = copy.deepcopy(s)
    r["payload"]["bank"]["full"]["total"]["industry"].reverse()         # same facts, different order
    c = IA.extract(r)
    key = "bank.full.total.industry.Hotels (except Casino Hotels) and Motels.orig"
    assert c.by_key(key).evidence_id == a.by_key(key).evidence_id
    assert c.header["payload_sha256"] != a.header["payload_sha256"]   # ...but the source fingerprint records the change
    other = IA.extract(synthetic_saved(sid="b" * 32))
    assert not set(other.ids()) & set(a.ids())                          # ids are scoped to their Saved Analysis
    assert a.by_key(key).evidence_id == make_id("a" * 32, Kind.FACT, key)


def test_values_and_provenance_are_exactly_those_of_the_saved_analysis():
    s = synthetic_saved()
    pk = IA.extract(s)
    facts = [r for r in pk.records if r.kind is Kind.FACT]
    assert facts
    for r in facts:
        assert resolve(s["payload"], r.source.payload_path) == r.value, r.metric_key
        assert r.source.source_id == s["id"] and r.source.recorded_at == s["created"]
        assert r.source.payload_sha256 == IA.payload_fingerprint(s)
        assert r.calculation_version == s["payload"]["calculation_version"]
        assert r.as_of == "2026-06-30" and r.bank == ("Synthetic Bank", "CA", "58310")
    assert pk.by_key("bank.full.total.total.cum_def").value == 100_000.0 / 4_000_000.0
    assert pk.by_key("benchmark.cre.total.cum_def").value == 0.0377
    d = pk.by_key("compare.total.total.cum_def.diff", Kind.DERIVED)
    assert d.value == 0.025 - 0.065 and len(d.derived_from) == 2
    assert {pk.get(x).metric_key for x in d.derived_from} == {"bank.fy2000.total.total.cum_def",
                                                              "benchmark.total.total.cum_def"}
    assert pk.header["sections_missing"] == ["static_pool"] and pk.header["adapter_version"] == IA.ADAPTER_VERSION


def validation_record(saved, values, **kw):
    return VAL.make_validation_record(IA.SYSTEM, saved["id"], IA.payload_fingerprint(saved),
                                      kw.get("method", "independent recompute from FOIA source data (test)"),
                                      "2026-11-05", "test", "test fixture", values)


def test_validation_labels_are_conservative_and_scoped():
    pk = IA.extract(synthetic_saved())                                  # engine-scope bank, release and version
    st = lambda k, kind=Kind.FACT: pk.by_key(k, kind).validation_status
    assert st("bank.full.total.total.cum_def") is VS.RECONCILED_TO_TEMPLATE        # no record: not independently verified
    assert not any(r.validation_status is VS.INDEPENDENTLY_VERIFIED or r.numerically_verified for r in pk.records)
    assert any("No validation record applies" in x for x in pk.header["limitations"])
    assert st("bank.full.total.total.guar") is VS.PENDING_CLIENT_APPROVAL          # no template definition
    assert st("bank.fy2000.total.total.cum_def") is VS.PENDING_CLIENT_APPROVAL     # application comparison basis
    assert st("bank.full.above.total.cum_def") is VS.PENDING_CLIENT_APPROVAL       # application threshold split
    for r in pk.records:
        assert r.methodology is not ME.CLIENT_APPROVED                             # nothing is client-approved yet
        if r.metric_key.startswith(("benchmark.", "compare.")):
            assert r.validation_status is VS.PRELIMINARY_METHODOLOGY, r.metric_key
            assert r.methodology is ME.CLIENT_DIRECTED_PRELIMINARY
        if r.validation_status is VS.INDEPENDENTLY_VERIFIED:
            assert r.kind is Kind.FACT and r.metric_key.startswith("bank.full.total.")
    other_bank = IA.extract(synthetic_saved(cert="99999"))
    assert other_bank.by_key("bank.full.total.total.cum_def").validation_status is VS.RECONCILED_TO_TEMPLATE
    assert not any(r.validation_status is VS.INDEPENDENTLY_VERIFIED for r in other_bank.records)
    old = IA.extract(synthetic_saved(version="stage1-1.2"))
    assert {r.validation_status for r in old.records} == {VS.UNVERIFIED}
    assert any("outside the validated range" in x for x in old.header["limitations"])
    assert IA.extract(synthetic_saved(version="stage1-1.4")).header["workbook_corrections_D1"] is False
    assert pk.header["workbook_corrections_D1"] is True


def test_independent_verification_requires_a_record_bound_to_this_analysis():
    s = synthetic_saved()
    vals = {"bank.full.total.total.cum_def": 0.025, "bank.full.total.total.loans": 30,
            "bank.fy2000.total.total.cum_def": 0.025, "benchmark.total.total.cum_def": 0.065}
    rec = validation_record(s, vals)
    pk = IA.extract(s, [rec])
    r = pk.by_key("bank.full.total.total.cum_def")
    assert r.validation_status is VS.INDEPENDENTLY_VERIFIED and r.numerically_verified and rec.record_id in r.numeric_basis
    assert pk.by_key("bank.full.total.total.loans").validation_status is VS.INDEPENDENTLY_VERIFIED
    assert pk.by_key("bank.full.total.total.cum_loss").validation_status is VS.RECONCILED_TO_TEMPLATE  # not covered
    fy, bm = pk.by_key("bank.fy2000.total.total.cum_def"), pk.by_key("benchmark.total.total.cum_def")
    assert fy.numerically_verified and fy.validation_status is VS.PENDING_CLIENT_APPROVAL   # numbers ok, method not
    assert bm.numerically_verified and bm.validation_status is VS.PRELIMINARY_METHODOLOGY
    assert not any(x.methodology is ME.CLIENT_APPROVED for x in pk.records)              # never grants approval
    assert pk.header["validations_applied"] == [rec.record_id]
    # the same attributes (bank, release, version) but another Saved Analysis or payload: not applied
    other = synthetic_saved(sid="b" * 32)
    pk2 = IA.extract(other, [rec])
    assert pk2.by_key("bank.full.total.total.cum_def").validation_status is VS.RECONCILED_TO_TEMPLATE
    assert "validates Saved Analysis" in pk2.header["validations_ignored"][0]["reason"]
    edited = copy.deepcopy(s)
    edited["payload"]["benchmark_population"]["loans"] += 1
    assert "different payload" in IA.extract(edited, [rec]).header["validations_ignored"][0]["reason"]
    # altered record, disagreeing record, record for values the analysis does not hold
    from dataclasses import replace
    with pytest.raises(EvidenceError, match="integrity"):
        IA.extract(s, [replace(rec, values=(("bank.full.total.total.cum_def", 0.01),))])
    bad = IA.extract(s, [validation_record(s, {"bank.full.total.total.cum_def": 0.03})])
    r = bad.by_key("bank.full.total.total.cum_def")
    assert r.validation_status is VS.UNVERIFIED and "disagrees" in " ".join(r.limitations).lower()
    assert bad.header["values_contradicted_by_validation"] == ["bank.full.total.total.cum_def"]
    with pytest.raises(EvidenceError, match="does not hold"):
        IA.extract(s, [validation_record(s, {"bank.full.total.total.no_such_metric": 1.0})])
    # a record can never make a DERIVED, HYPOTHESIS or unrecorded value INDEPENDENTLY_VERIFIED
    with pytest.raises(EvidenceError, match="requires a validation record"):
        validate_record(replace(pk.by_key("bank.full.total.total.cum_loss"),
                                validation_status=VS.INDEPENDENTLY_VERIFIED))


def test_extraction_is_reproducible_and_bound_to_its_source():
    s = synthetic_saved()
    pk = IA.extract(s)
    assert IA.verify(pk, s) and IA.extract(s).fingerprint() == pk.fingerprint()
    changed = copy.deepcopy(s)
    changed["payload"]["bank"]["full"]["total"]["total"]["cum_def"] = 0.5
    assert not IA.verify(pk, changed)                                   # a changed source no longer matches the pack


@pytest.mark.parametrize("breaker,message", [
    (lambda s: s["payload"].pop("benchmark"), "missing required sections"),
    (lambda s: s.pop("created"), "missing"),
    (lambda s: s["criteria"].update(threshold=999), "criteria differ"),
    (lambda s: s["payload"].update(calculation_version=""), "no calculation version"),
    (lambda s: s["payload"]["bank"]["full"]["total"]["vintage"][0].update(loans=11), "vintage loans sum"),
    (lambda s: [x.update(loans=1) for x in (s["payload"]["bank"]["full"]["above"]["total"],      # internally consistent
                                            s["payload"]["bank"]["full"]["above"]["vintage"][0],  # 'above' group that no
                                            s["payload"]["bank"]["full"]["above"]["industry"][0])],  # longer adds up
     "above \\+ below"),
    (lambda s: s["payload"]["benchmark"].update(cre=bench_rows()), "default_line cre"),
])
def test_missing_or_inconsistent_evidence_is_rejected(breaker, message):
    s = synthetic_saved()
    breaker(s)
    with pytest.raises(EvidenceError, match=message):
        IA.extract(s)


def test_invalid_records_and_hypotheses_are_rejected():
    pk = IA.extract(synthetic_saved())
    r = pk.by_key("bank.full.total.total.cum_def")
    from dataclasses import replace
    with pytest.raises(EvidenceError, match="identifier"):
        validate_record(replace(r, evidence_id="EV-forged"))
    with pytest.raises(EvidenceError, match="only FACTs"):
        validate_record(replace(r, kind=Kind.DERIVED, rule="x", validation_status=VS.INDEPENDENTLY_VERIFIED,
                                numerically_verified=True,
                                evidence_id=make_id(r.source.source_id, Kind.DERIVED, r.metric_key)))
    with pytest.raises(EvidenceError, match="never stored"):
        validate_record(replace(r, classification=CL.CLIENT_CONFIDENTIAL))
    with pytest.raises(EvidenceError, match="cite"):
        make_hypothesis(pk, "hyp.1", "h", "statement", cites=())
    h = make_hypothesis(pk, "hyp.1", "h", "statement", cites=(r.evidence_id,))
    assert h.kind is Kind.HYPOTHESIS and h.validation_status is VS.UNVERIFIED
    assert h.evidence_id not in pk.ids()                                # the pack itself is never modified


def test_saved_analysis_is_never_mutated(tmp_path, monkeypatch):
    s = synthetic_saved()
    before = copy.deepcopy(s)
    pk = IA.extract(s)
    EG.export(pk, PERMISSIVE)
    assert s == before
    from webapp import saved as SV                                      # a Saved Analysis on disk keeps its bytes
    monkeypatch.setattr(SV, "DIR", tmp_path / "analyses")
    a = SV.save("tester", synthetic_saved()["payload"], "0" * 32, "both")
    f = tmp_path / "analyses" / a["id"] / "analysis.json"
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    IA.extract(SV.load(a["id"]))
    assert hashlib.sha256(f.read_bytes()).hexdigest() == digest


def test_no_network_no_engine_and_layer_separation(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    pk = IA.extract(synthetic_saved())
    EG.export(pk, PERMISSIVE)
    EG.preview(pk, PERMISSIVE)
    assert not any(m == "anthropic" or m.startswith("anthropic.") for m in sys.modules)
    imports = {}
    for f in list((ROOT / "intelligence").glob("*.py")) + [ROOT / "webapp" / "intel_adapter.py"]:
        names = set()
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= {(node.module or "")} | {a.name for a in node.names}
        imports[f.name] = names
    for f, names in imports.items():
        # live_anthropic.py is the single approved entry point for the live experiment (2026-11-06): it alone may
        # import the SDK, lazily inside the call (tests/test_live_provider_offline.py checks that); no module may
        # import an HTTP or socket library directly
        banned = {"requests", "httpx", "urllib", "socket", "http"} | (set() if f == "live_anthropic.py" else
                                                                      {"anthropic"})
        assert not names & banned, f
        if f != "intel_adapter.py":                                     # intelligence/ is application-independent
            assert not any(n.startswith(("webapp", "bank_workbook_generator")) for n in names), f
    assert "analysis" not in imports["intel_adapter.py"]               # the adapter never calls the calculation engine


def test_exports_never_expose_confidential_or_internal_content():
    pk = IA.extract(synthetic_saved())
    default = EG.export(pk)
    assert default["records"] == [] and default["bank"] is None        # deny by default
    for doc in (default, EG.export(pk, PERMISSIVE)):
        text = json.dumps(doc)
        assert CANARY not in text and EG.LOCAL_HOME not in text and ".csv" not in text
        assert "a" * 32 not in text                                     # no source identifiers
    full = EG.export(pk, PERMISSIVE)
    assert full["records"] and full["bank"] == ["Synthetic Bank", "CA", "58310"]
    assert all(set(r) == set(EG.EXPORT_FIELDS) for r in full["records"])
    narrow = EG.export(pk, EG.EgressPolicy(frozenset({CL.PUBLIC_FOIA_AGGREGATE}), ("bank.full.total.total.",),
                                           approved_by="test", approved_on="2026-11-05"))
    assert narrow["records"] and all(r["metric_key"].startswith("bank.full.total.total.") for r in narrow["records"])
    with pytest.raises(EG.EgressError, match="never leave"):
        EG.EgressPolicy(frozenset({CL.CLIENT_CONFIDENTIAL}), approved_by="x", approved_on="y")
    with pytest.raises(EG.EgressError, match="name who approved"):
        EG.EgressPolicy(frozenset({CL.PUBLIC_FOIA_AGGREGATE}))
    from dataclasses import replace                                     # last line of defence: forbidden markers
    leaky = replace(pk, records=pk.records[:-1] + (replace(pk.records[-1], label=f"see {EG.LOCAL_HOME}x/file.xlsx"),))
    with pytest.raises(EG.EgressError, match="forbidden"):
        EG.export(leaky, PERMISSIVE)


RELEASE = ROOT / "source_data" / "20260630"


@pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
def test_real_saved_analysis_us_metro_matches_independently_validated_values(tmp_path, monkeypatch):
    from webapp import analysis as AN, generator_bridge as GB, saved as SV
    monkeypatch.setattr(SV, "DIR", tmp_path / "analyses")
    p = AN.compute("20260630", GB.resolve("20260630", "US Metro Bank", "CA"), 350000)
    a = SV.load(SV.save("tester", json.loads(json.dumps(p, default=str)), "0" * 32, "both")["id"])
    pk = IA.extract(a)
    v = lambda k, kind=Kind.FACT: pk.by_key(k, kind).value
    # independent recomputation from the FOIA CSVs (validation assessment 2026-11-05, section 4)
    assert v("bank.full.total.total.loans") == 937
    assert v("bank.full.total.total.orig") == 1_536_344_900
    assert v("bank.full.total.total.cum_def") == pytest.approx(0.016022053381372893, abs=1e-15)
    assert v("bank.full.total.total.cum_loss") == pytest.approx(0.006853292278315892, abs=1e-15)
    assert v("bank.full.total.total.severity") == pytest.approx(0.4277411961617524, abs=1e-13)
    assert v("bank.full.total.total.age") == pytest.approx(36.68441064638783, abs=1e-10)
    assert v("benchmark.total.total.cum_def") == pytest.approx(0.06594063705869781, abs=1e-12)
    # engine scope alone (verified bank, release and version) does not make THIS analysis independently verified
    assert pk.header["engine_validation_scope"]["this_bank_and_release_in_engine_scope"] is True
    assert pk.by_key("bank.full.total.total.cum_def").validation_status is VS.RECONCILED_TO_TEMPLATE
    assert pk.by_key("benchmark.total.total.cum_def").validation_status is VS.PRELIMINARY_METHODOLOGY
    # a validation record holding the independently recomputed values (clean-room, Decimal, from the FOIA CSVs)
    rec = validation_record(a, {"bank.full.total.total.loans": 937, "bank.full.total.total.orig": 1_536_344_900,
                                "bank.full.total.total.cum_def": 0.016022053381372893,
                                "bank.full.total.total.cum_loss": 0.006853292278315891,
                                "bank.full.total.total.severity": 0.4277411961617524,
                                "bank.full.total.total.age": 36.68441064638783})
    pv = IA.extract(a, [rec])
    for k in ("loans", "orig", "cum_def", "cum_loss", "severity", "age"):
        assert pv.by_key(f"bank.full.total.total.{k}").validation_status is VS.INDEPENDENTLY_VERIFIED, k
    assert pv.by_key("bank.full.total.total.active").validation_status is VS.RECONCILED_TO_TEMPLATE
    assert pk.header["calculation_version_short"] == "stage1-1.5" and pk.header["workbook_corrections_D1"] is True
    assert EG.export(pk)["records"] == []
