"""Phase 1 lending activity: annual / quarterly originations, gross vs original unguaranteed, charge-off events,
geography, loan characteristics, industry x year, concentration by year, and loan-level drill-down.

Checks: consistency with the existing engine (vintage rows, charge-offs by year, totals); independent clean-room
recomputation from the FOIA files (intelligence/cleanroom_foia.recompute_activity); drill-down cells reconcile to
their aggregates; borrower name and street never leave the server; definitions at boundaries."""
import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FBX_APP_DATA", tempfile.mkdtemp(prefix="fbx_activity_test_"))
from intelligence import cleanroom_foia as CR        # noqa: E402
from webapp import analysis as AN                    # noqa: E402
from webapp import generator_bridge as GB            # noqa: E402
from webapp import lending_activity as LA            # noqa: E402

RELEASE = ROOT / "source_data" / "20260630"
pytestmark = pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
AS_OF = date(2026, 6, 30)
BANKS = {"metro": ("US Metro Bank", "CA", "58310"), "finwise": ("FinWise Bank", "UT", "35323"),
         "shoreham": ("Shoreham Bank", "RI", "27417")}
MEASURES = ("loans", "gross", "guar", "unguar", "default_loans", "default_gross", "gross_chargeoff")


@pytest.fixture(scope="module")
def data():
    out = {}
    for k, b in BANKS.items():
        sel = GB.resolve("20260630", *b)
        ext, _ = AN.bank_data("20260630", sel)
        out[k] = {"sel": sel, "rows": ext["rows"], "payload": AN.compute("20260630", sel, 350000),
                  "activity": LA.compute(ext["rows"], AN.status_map(), AS_OF)}
    return out


@pytest.fixture(scope="module")
def cleanroom(data):
    lenders = {k: (d["sel"]["bank_name"], d["sel"]["bank_state"], d["sel"]["cert"] or "", set(d["sel"]["location_ids"]))
               for k, d in data.items()}
    found, as_of, _ = CR.read_release(str(RELEASE), lenders)
    assert as_of == AS_OF
    return {k: CR.recompute_activity(found[k]["rows"]) for k in data}


def test_payload_carries_the_activity_section(data):
    a = data["metro"]["payload"]["activity"]
    assert a["version"] == LA.ACTIVITY_VERSION and a["as_of"] == "2026-06-30" and not a["partial_quarter"]
    assert a == data["metro"]["activity"]


@pytest.mark.parametrize("bank", list(BANKS))
def test_activity_is_consistent_with_existing_engine_measures(data, bank):
    d = data[bank]
    p, a = d["payload"], d["activity"]
    vint = {r["year"]: r for r in p["bank"]["full"]["total"]["vintage"]}
    for m in a["annual"]:
        v = vint.get(m["period"], {})
        assert m["loans"] == v.get("loans", 0)
        assert m["gross"] == pytest.approx(v.get("orig", 0) or 0)
        assert m["unguar"] == pytest.approx(v.get("unguar", 0) or 0)
        assert m["default_loans"] == v.get("chgoff", 0)
    cy = {r["year"]: r for r in p["chgoff_by_year"]["total"]["years"]}
    for e in a["chargeoff_events"]["annual"]:
        assert (e["loans"], round(e["gross_chargeoff"], 2)) == (cy.get(e["period"], {}).get("loans", 0),
                                                             round(cy.get(e["period"], {}).get("amount", 0), 2))
    t, k = a["totals"], p["bank"]["full"]["total"]["total"]
    assert (t["loans"], t["default_loans"]) == (k["loans"], k["chgoff"])
    assert t["gross"] == pytest.approx(k["orig"]) and t["unguar"] == pytest.approx(k["unguar"])
    assert sum(m["loans"] for m in a["quarterly"]) == t["loans"]
    assert sum(m["gross"] for m in a["quarterly"]) == pytest.approx(t["gross"])
    for geo in ("project", "borrower"):
        assert sum(s["loans"] for s in a["geography"][geo]) == t["loans"]
    for ch in a["characteristics"].values():
        assert sum(i["loans"] for i in ch["items"]) == t["loans"]


@pytest.mark.parametrize("bank", list(BANKS))
def test_activity_matches_the_independent_cleanroom_recomputation(data, cleanroom, bank):
    a, ind = data[bank]["activity"], cleanroom[bank]
    checked = 0
    for table, items, key in (("annual", a["annual"], "period"), ("quarterly", a["quarterly"], "period"),
                              ("project_state", a["geography"]["project"], "state"),
                              ("borrower_state", a["geography"]["borrower"], "state")):
        for m in items:
            for meas in MEASURES:
                assert m[meas] == pytest.approx(ind.get((table, m[key], meas), 0), abs=1e-6), (table, m[key], meas)
                checked += 1
    for ch_key, ch in a["characteristics"].items():
        for i in ch["items"]:
            for meas in MEASURES:
                assert i[meas] == pytest.approx(ind.get((ch_key, i["value"], meas), 0), abs=1e-6)
    for row in a["industry_year"]["industries"]:
        for y, c in row["cells"].items():
            for meas in ("loans", "gross", "unguar"):
                assert c[meas] == pytest.approx(ind[("industry_year", (row["name"], int(y)), meas)], abs=1e-6)
    for book, table in (("annual", "events_annual"), ("quarterly", "events_quarterly")):
        for e in a["chargeoff_events"][book]:
            for meas in ("loans", "gross", "unguar", "gross_chargeoff"):
                assert e[meas] == pytest.approx(ind.get((table, e["period"], meas), 0), abs=1e-6)
    assert a["chargeoff_events"]["undated"]["loans"] == ind[("events", "undated", "loans")]
    assert checked > 500


def test_original_unguaranteed_is_gross_minus_guaranteed_and_never_a_balance(data):
    a = data["finwise"]["activity"]
    for m in a["annual"] + a["quarterly"]:
        assert m["unguar"] == pytest.approx(m["gross"] - m["guar"])
        if m["gross"]:
            assert m["unguar_share"] == pytest.approx(m["unguar"] / m["gross"])
    assert "balance" not in " ".join(a["annual"][0])                 # no balance field exists


def test_growth_is_relative_to_the_prior_period_and_undefined_after_zero(data):
    a = data["metro"]["activity"]["annual"]
    assert a[-1]["period"] == 2026 and a[-1]["partial"] and a[-1]["gross_change"] is None   # part-year: no growth
    for prev, cur in zip(a, a[1:-1]):
        if prev["gross"]:
            assert cur["gross_change"] == pytest.approx((cur["gross"] - prev["gross"]) / prev["gross"])
        else:
            assert cur["gross_change"] is None
    assert a[0]["gross_change"] is None


def test_concentration_by_year_uses_that_years_originations(data):
    a = data["metro"]["activity"]
    by_year = {c["year"]: c for c in a["concentration_by_year"]}
    gross_2025 = [r["cells"]["2025"]["gross"] for r in a["industry_year"]["industries"] if "2025" in r["cells"]]
    assert by_year[2025]["top1"] == pytest.approx(max(gross_2025) / sum(gross_2025))
    assert by_year[2025]["gross"] == pytest.approx(next(m["gross"] for m in a["annual"] if m["period"] == 2025))


# ----------------------------------------------------------------------------------------------------- drill-down
@pytest.mark.parametrize("bank", list(BANKS))
def test_every_drilldown_cell_reconciles_to_its_aggregate(data, bank):
    d = data[bank]
    a, sm = d["activity"], AN.status_map()
    cells = ([("year", str(m["period"]), m) for m in a["annual"] if m["loans"]]
             + [("quarter", m["period"], m) for m in a["quarterly"] if m["loans"]]
             + [("project_state", s["state"], s) for s in a["geography"]["project"]]
             + [("borrower_state", s["state"], s) for s in a["geography"]["borrower"]]
             + [(k, i["value"], i) for k, ch in a["characteristics"].items() for i in ch["items"]])
    for dim, value, agg in cells:
        t = LA.loan_rows(LA.select(d["rows"], sm, dim, value), sm)["totals"]
        assert t["loans"] == agg["loans"] and t["default_loans"] == agg["default_loans"], (dim, value)
        assert t["gross"] == pytest.approx(agg["gross"]) and t["unguar"] == pytest.approx(agg["unguar"])
        assert t["gross_chargeoff"] == pytest.approx(agg["gross_chargeoff"])
    for e in a["chargeoff_events"]["annual"]:
        t = LA.loan_rows(LA.select(d["rows"], sm, "chargeoff_year", str(e["period"])), sm)["totals"]
        assert t["loans"] == e["loans"] and t["gross_chargeoff"] == pytest.approx(e["gross_chargeoff"])
    for row in a["industry_year"]["industries"][:10]:
        t = LA.loan_rows(LA.select(d["rows"], sm, "industry", row["name"]), sm)["totals"]
        assert t["loans"] == row["loans"] and t["gross"] == pytest.approx(row["gross"])


def test_drilldown_never_exposes_borrower_name_or_street(data):
    d = data["metro"]
    out = LA.loan_rows(LA.select(d["rows"], AN.status_map(), "year", "2025"), AN.status_map())
    names = {r["BorrName"] for r in d["rows"]} | {r["BorrStreet"] for r in d["rows"]}
    text = repr(out)
    assert not any(n and n in text for n in names if len(n) > 4)
    assert all(not ({"BorrName", "BorrStreet"} & set(i)) for i in out["loans"])
    assert len({i["ref"] for i in out["loans"]}) == len(out["loans"])     # stable, unique references
    again = LA.loan_rows(LA.select(d["rows"], AN.status_map(), "year", "2025"), AN.status_map())
    assert [i["ref"] for i in again["loans"]] == [i["ref"] for i in out["loans"]]


def test_drilldown_rejects_unknown_dimensions(data):
    with pytest.raises(ValueError):
        LA.select(data["metro"]["rows"], AN.status_map(), "BorrName", "x")


# ------------------------------------------------------------------------------------------------ definitions
def _row(fd, gross="100000", guar="75000", status="CURR", co="", amt="0", **kw):
    r = {"FirstDisbursementDate": fd, "GrossApproval": gross, "SBAGuaranteedApproval": guar, "LoanStatus": status,
         "ChargeOffDate": co, "GrossChargeOffAmount": amt, "NaicsDescription": "Test", "ProjectState": "NY",
         "BorrState": "NJ", "ProcessingMethod": "7a General", "BusinessAge": "", "FixedOrVariableInterestInd": "V"}
    r.update(kw)
    return r


def test_definitions_at_boundaries():
    sm = AN.status_map()
    rows = [_row("2025-03-31"), _row("2025-04-01", gross="150000", guar="112500"),
            _row("2024-01-15", status="CHGOFF", co="2025-07-02", amt="40000"),
            _row("2023-05-01", status="CHGOFF", amt="10000"),                    # default status, no charge-off date
            _row("2023-06-01", status="P I F", co="2024-01-01"),                 # a date but not in default status
            _row("2022-02-02", gross="350000.4", guar="262500")]
    a = LA.compute(rows, sm, AS_OF)
    q = {m["period"]: m for m in a["quarterly"]}
    assert q["2025Q1"]["loans"] == 1 and q["2025Q2"]["loans"] == 1                  # quarter boundary
    assert a["chargeoff_events"]["annual"][-1]["period"] == 2025                   # by charge-off date, not vintage
    assert a["chargeoff_events"]["undated"]["loans"] == 1 and a["chargeoff_events"]["dated_not_in_default_status"] == 1
    bands = {i["value"]: i["loans"] for i in a["characteristics"]["size_band"]["items"]}
    assert bands["$150K or less"] == 5 and bands["$150K to $350K"] == 1                # 150,000 and 350,000.4 -> 350,000
    geo = {s["state"]: s["loans"] for s in a["geography"]["project"]}
    assert geo == {"NY": 6} and {s["state"] for s in a["geography"]["borrower"]} == {"NJ"}   # never mixed
    assert LA.size_band(150_000) == "$150K or less" and LA.size_band(150_001) == "$150K to $350K"
    assert LA.size_band(2_000_000) == "$1M to $2M" and LA.size_band(2_000_001) == "Over $2M"


# ------------------------------------------------------------------------------------------- drill-down endpoints
from fastapi.testclient import TestClient            # noqa: E402

from webapp import app as A                          # noqa: E402
from webapp import saved as SV                       # noqa: E402
from webapp import settings                          # noqa: E402

client = TestClient(A.app)
LIVE = {"period": "20260630", "name": "US Metro Bank", "state": "CA", "cert": "58310", "threshold": "350000"}


def test_live_drilldown_reconciles_and_hides_borrower_identity(data):
    r = client.get("/dashboard/loans", params={**LIVE, "dim": "quarter", "value": "2025Q3"})
    assert r.status_code == 200
    j = r.json()
    assert j["reconciles"] and all(j["checks"].values()) and j["totals"]["loans"] == 55
    assert j["selection"] == "first disbursed in 2025Q3" and "not current balances" in j["amounts"]
    names = {x["BorrName"] for x in data["metro"]["rows"]} | {x["BorrStreet"] for x in data["metro"]["rows"]}
    assert not any(n and len(n) > 4 and n in r.text for n in names)
    assert {"BorrName", "BorrStreet"} == set(j["hidden_fields"])


def test_saved_analysis_drilldown_reconciles_to_frozen_figures(data):
    a = SV.save(settings.LOCAL_USER, data["finwise"]["payload"], "0" * 32, "both")
    ind = data["finwise"]["activity"]["industry_year"]["industries"][0]
    j = client.get(f"/analyses/{a['id']}/loans", params={"dim": "industry", "value": ind["name"]}).json()
    assert j["reconciles"] and j["totals"]["loans"] == ind["loans"]
    y = next(iter(ind["cells"]))
    j = client.get(f"/analyses/{a['id']}/loans", params={"dim": "industry_year", "value": f"{ind['name']}|{y}"}).json()
    assert j["reconciles"] and j["totals"]["loans"] == ind["cells"][y]["loans"]


def test_older_saved_analyses_report_that_no_figure_was_stored(data):
    legacy = {k: v for k, v in data["shoreham"]["payload"].items() if k != "activity"}
    a = SV.save(settings.LOCAL_USER, legacy, "0" * 32, "both")
    j = client.get(f"/analyses/{a['id']}/loans", params={"dim": "year", "value": "2007"}).json()
    assert j["displayed"] is None and j["reconciles"] is False and j["checks"] == {} and j["totals"]["loans"] > 0


def test_drilldown_access_and_input_controls(data, monkeypatch):
    assert client.get("/dashboard/loans", params={**LIVE, "dim": "BorrName", "value": "x"}).status_code == 400
    assert client.get("/analyses/" + "f" * 32 + "/loans", params={"dim": "year", "value": "2025"}).status_code == 404
    a = SV.save(settings.LOCAL_USER, data["metro"]["payload"], "0" * 32, "both")
    monkeypatch.setattr(settings, "LOCAL_USER", "someone-else")
    assert client.get(f"/analyses/{a['id']}/loans", params={"dim": "year", "value": "2025"}).status_code == 403
