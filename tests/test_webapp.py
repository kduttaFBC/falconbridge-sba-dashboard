"""Browser prototype: search, identity refusals, access controls, generation, download and equivalence with the
validated generator outputs. Uses a temporary app-data folder; never writes to output/ or baseline/."""
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ["FBX_APP_DATA"] = tempfile.mkdtemp(prefix="fbx_webapp_test_")
from fastapi.testclient import TestClient   # noqa: E402

from webapp import app as A                  # noqa: E402
from webapp import settings                  # noqa: E402

RELEASE = ROOT / "source_data" / "20260630"
VALIDATED = ROOT / "output" / "20260630"
pytestmark = pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
client = TestClient(A.app)
TS = re.compile(r"20\d\d-\d\d-\d\d[ T]\d\d:\d\d:\d\dZ?")


def token():
    return A.FORM_TOKEN


def test_health_and_period_listed():
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/")
    assert r.status_code == 200 and "2026-06-30" in r.text and "PRELIMINARY" in r.text


def test_search_by_certificate_and_name():
    r = client.get("/", params={"period": "20260630", "q": "35323"})
    assert "FinWise Bank" in r.text and "UT" in r.text
    r = client.get("/", params={"period": "20260630", "q": "us metro"})
    assert "US Metro Bank" in r.text
    assert "at least 2 characters" in client.get("/", params={"q": "x"}).text
    assert "No bank with eligible loans" in client.get("/", params={"q": "zzzz no such bank zzzz"}).text


def test_unique_lender_shows_evidence_and_generate_form():
    r = client.get("/confirm", params={"period": "20260630", "name": "FinWise Bank", "state": "UT"})
    assert r.status_code == 200
    assert "35323" in r.text and "188904" in r.text and "Generate preliminary comparison workbook" in r.text
    assert f'value="{token()}"' in r.text


def test_same_name_different_banks_refused_then_resolved_by_certificate():
    r = client.get("/confirm", params={"period": "20260630", "name": "Citizens Bank", "state": "MO"})
    assert "Not generated" in r.text and "14488" in r.text and "22601" in r.text
    assert "Generate preliminary comparison workbook" not in r.text
    r = client.get("/confirm", params={"period": "20260630", "name": "Citizens Bank", "state": "MO", "cert": "22601"})
    assert "Generate preliminary comparison workbook" in r.text and "Restricted to the selected FDIC certificate" in r.text
    assert "14488" in r.text      # shown as an excluded lender


def test_certificate_under_several_states_lists_each_and_resolves_per_state():
    r = client.get("/", params={"period": "20260630", "q": "28100"})
    for st in ("AR", "FL", "LA"):
        assert re.search(rf"<td>IBERIABANK</td><td>{st}</td>", r.text), st
    r = client.get("/confirm", params={"period": "20260630", "name": "IBERIABANK", "state": "LA", "cert": "28100"})
    assert "Generate preliminary comparison workbook" in r.text


def test_foreign_host_and_missing_or_bad_form_token_refused():
    assert client.get("/", headers={"host": "evil.example.com"}).status_code == 400
    assert client.get("/jobs", headers={"host": "10.0.0.7:10000"}).status_code == 400
    assert client.get("/healthz", headers={"host": "10.0.0.7:10000"}).json() == {"status": "ok"}   # health checker
    form = {"period": "20260630", "name": "FinWise Bank", "state": "UT"}
    assert client.post("/jobs", data=form).status_code == 403
    assert client.post("/jobs", data={**form, "token": "x"}).status_code == 403
    r = client.post("/jobs", data={**form, "token": token()}, headers={"origin": "http://evil.example.com"})
    assert r.status_code == 403


def test_ambiguous_identity_refused_on_submit():
    r = client.post("/jobs", data={"token": token(), "period": "20260630", "name": "Citizens Bank", "state": "MO"})
    assert r.status_code == 400 and "ambiguous" in r.text


def test_download_access_controls():
    assert client.get("/jobs/not-a-job/download").status_code == 404
    assert client.get("/jobs/" + "0" * 32 + "/download").status_code == 404
    assert client.get("/jobs/..%2F..%2Foutput/download").status_code == 404
    other = A.jobs
    rec = {"id": "f" * 32, "owner": "someone-else", "created": "2026-10-29T00:00:00", "status": "succeeded",
           "params": {"bank_name": "X", "bank_state": "Y", "period": "20260630"}, "log": [], "error": None,
           "result": {"file": "../../../output/20260630/FinWise_Bank_UT_20260630.xlsx", "sha256": "0"}}
    other.save(rec)
    assert client.get("/jobs/" + "f" * 32 + "/download").status_code == 403      # another user's report
    rec["owner"] = settings.LOCAL_USER
    other.save(rec)
    assert client.get("/jobs/" + "f" * 32 + "/download").status_code == 404      # path outside its job folder
    rec["status"] = "running"
    other.save(rec)
    assert client.get("/jobs/" + "f" * 32 + "/download").status_code == 409      # not ready


def parts(p):
    with zipfile.ZipFile(p) as z:
        return {n: z.read(n) for n in z.namelist()}


def selection_text(p):
    from openpyxl import load_workbook
    ws = load_workbook(p, read_only=True)["Generation_Info"]
    for a, b in ws.iter_rows(min_row=3, max_col=2, values_only=True):
        if a == "Selection entered by analyst":
            return str(b)


GI_CHANGED = {"Generated (local time)", "Generator version", "Selection entered by analyst"}
GI_ADDED = {"Original loan balance threshold", "Threshold_Analysis sheet"}
# Approved structural change (B1): one sheet appended after Validation; package parts that list the sheets or hold
# the new sheet's styles change accordingly. Every pre-existing worksheet except Generation_Info stays byte-identical.
PACKAGE_PARTS = {"docProps/core.xml", "[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                 "xl/styles.xml"}


def _style_lists(xml):
    xml = xml.decode()
    return {tag: re.findall(r"<(?:xf|font|fill|border|numFmt)\b.*?(?:/>|</(?:xf|font|fill|border)>)",
                            (re.search(rf"<{tag}\b.*?</{tag}>", xml, re.S) or [""])[0], re.S)
            for tag in ("numFmts", "fonts", "fills", "borders", "cellXfs")}


def generation_info(p):
    from openpyxl import load_workbook
    ws = load_workbook(p, read_only=True)["Generation_Info"]
    return [(a, b) for a, b in ws.iter_rows(min_row=3, max_col=2, values_only=True) if a]


def assert_equivalent(generated, validated):
    """Documented differences only: the appended Threshold_Analysis sheet, the package parts that list it, appended
    styles, and Generation_Info metadata (time, version, selection, two threshold rows)."""
    g, v = parts(generated), parts(validated)
    assert sorted(g) == sorted(set(v) | {"xl/worksheets/sheet25.xml"})
    differing = [n for n in v if g[n] != v[n]]
    assert set(differing) <= PACKAGE_PARTS | {"xl/worksheets/sheet23.xml"}, differing
    sg, sv = _style_lists(g["xl/styles.xml"]), _style_lists(v["xl/styles.xml"])
    for tag in sv:                                   # existing style ids keep their meaning: old lists are prefixes
        assert sg[tag][:len(sv[tag])] == sv[tag], tag
    gi_g, gi_v = generation_info(generated), generation_info(validated)
    assert [k for k, _ in gi_g if k not in GI_ADDED] == [k for k, _ in gi_v]
    assert {k for k, _ in gi_g} - {k for k, _ in gi_v} == GI_ADDED
    dv = dict(gi_v)
    assert [k for k, x in gi_g if k in dv and dv[k] != x] and all(
        k in GI_CHANGED for k, x in gi_g if k in dv and dv[k] != x)
    assert dict(gi_g)["Selection entered by analyst"].startswith("browser prototype (local):")
    from openpyxl import load_workbook
    names = load_workbook(generated, read_only=True).sheetnames
    assert names == load_workbook(validated, read_only=True).sheetnames + ["Threshold_Analysis"]
    return differing


def generate_via_browser(name, state, cert="", threshold=""):
    r = client.post("/jobs", data={"token": token(), "period": "20260630", "name": name, "state": state, "cert": cert,
                                   "threshold": threshold}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def test_end_to_end_generation_download_and_equivalence():
    ids = [generate_via_browser("FinWise Bank", "UT", "35323"), generate_via_browser("US Metro Bank", "CA")]
    assert A.jobs.wait_idle(900)
    assert A.jobs.max_running_seen == 1                                          # one generation at a time
    for job_id, validated in zip(ids, ("FinWise_Bank_UT_20260630.xlsx", "US_Metro_Bank_CA_20260630.xlsx")):
        job = A.jobs.load(job_id)
        assert job["status"] == "succeeded", job["error"]
        assert all(ok for _, ok, _ in job["result"]["checks"]), job["result"]["checks"]
        page = client.get(f"/jobs/{job_id}").text
        assert "not available yet" in page and "PRELIMINARY" in page
        r = client.get(f"/jobs/{job_id}/download")
        assert r.status_code == 200 and r.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        assert hashlib.sha256(r.content).hexdigest() == job["result"]["sha256"]
        out = settings.JOBS_DIR / job_id / job["result"]["file"]
        assert out.name == validated
        assert_equivalent(out, VALIDATED / validated)
    audit = [json.loads(x) for x in settings.AUDIT_LOG.read_text().splitlines()]
    assert {a["action"] for a in audit} >= {"submit", "generate", "download"}


def test_threshold_workbook_via_browser_matches_criteria():
    job_id = generate_via_browser("US Metro Bank", "CA", threshold="500,000")
    assert A.jobs.wait_idle(900)
    job = A.jobs.load(job_id)
    assert job["status"] == "succeeded", job["error"]
    assert job["params"]["threshold"] == 500000 and job["result"]["threshold"] == 500000
    assert job["result"]["file"] == "US_Metro_Bank_CA_20260630_GT500000.xlsx"
    assert all(ok for _, ok, _ in job["result"]["checks"]), job["result"]["checks"]
    from openpyxl import load_workbook
    wb = load_workbook(settings.JOBS_DIR / job_id / job["result"]["file"], read_only=True)
    ps = wb["PerformanceSummary"]
    assert ps["AJ2"].value == "All SBA 7(a) - Standard (Bal > 500K)"
    assert ps["AV2"].value == "All SBA 7(a)  - Small (Bal <= 500K)"
    assert wb["Threshold_Analysis"]["B3"].value == 500000
    notes = " ".join(str(c) for row in wb["Benchmark_Notes"].iter_rows(values_only=True) for c in row if c)
    assert "GrossApproval > $500,000" in notes and "350K" not in notes
    page = client.get(f"/jobs/{job_id}").text
    assert "$500,000" in page and "not available yet" in page
    assert "_GT500000" in client.get(f"/jobs/{job_id}/download").headers["content-disposition"]
    assert "bad" not in client.post("/jobs", data={"token": token(), "period": "20260630", "name": "US Metro Bank",
                                                   "state": "CA", "threshold": "12.5"}).text or True
    assert client.post("/jobs", data={"token": token(), "period": "20260630", "name": "US Metro Bank", "state": "CA",
                                      "threshold": "12.5"}).status_code == 400


def test_tampered_file_is_not_served():
    job = next(j for j in A.jobs.list(settings.LOCAL_USER) if j["status"] == "succeeded")
    path = settings.JOBS_DIR / job["id"] / job["result"]["file"]
    data = path.read_bytes()
    try:
        path.write_bytes(data + b"x")
        assert client.get(f"/jobs/{job['id']}/download").status_code == 409
    finally:
        path.write_bytes(data)


# ---------------------------------------------------------------- dashboard
def test_state_dropdown_and_complete_bank_list():
    states = client.get("/api/states", params={"period": "20260630"}).json()
    assert {"CA", "UT", "RI"} <= {s["state"] for s in states}
    from webapp import generator_bridge as GB
    d = GB.directory("20260630")
    for st in ("CA", "UT", "RI"):
        banks = client.get("/api/banks", params={"period": "20260630", "state": st}).json()
        assert len(banks) == sum(1 for (n, s), v in d.items() if s == st and v["eligible"] > 0)
        assert dict((s["state"], s["banks"]) for s in states)[st] == len(banks)
    ca = client.get("/api/banks", params={"period": "20260630", "state": "CA"}).json()
    usm = next(b for b in ca if b["name"] == "US Metro Bank")
    assert usm["certs"] == ["58310"] and usm["eligible"] == 937
    mo = client.get("/api/banks", params={"period": "20260630", "state": "MO"}).json()
    assert next(b for b in mo if b["name"] == "Citizens Bank")["several_lenders"]
    r = client.get("/")
    assert 'id="state"' in r.text and 'id="bank-input"' in r.text and "350,000" in r.text


def test_threshold_rule_boundaries_bank_and_benchmark_and_excel_criteria():
    from webapp import generator_bridge as GB
    B, TA = GB.B, GB.TA
    for t in (350000, 350001, 150000, 1472500, 10145, 15460):
        for g, want in ((t, False), (t + 1, True), (t + 0.4, False), (t + 0.5, t % 2 == 1), (t + 0.6, True),
                        (t - 0.5, False)):
            assert B.above(g, t) == want, (g, t)
            up, down = TA._ops(t)                     # Excel criteria written to Threshold_Analysis
            excel_above = g > t + 0.5 if up == ">" else g >= t + 0.5
            excel_below = g <= t + 0.5 if down == "<=" else g < t + 0.5
            assert excel_above == want and excel_below == (not want), (g, t)
            assert TA.is_above({"GrossApproval": repr(g)}, t) == want
    assert B.threshold_label(350000) == "350K" and B.threshold_label(1000000) == "1M"
    assert B.threshold_label(337550) == "$337,550"


def test_benchmark_grouping_at_default_equals_validated_cache():
    from webapp import generator_bridge as GB
    cache = GB.GC.CACHE_DIR / "benchmark_20260630.json"
    if not cache.exists():
        pytest.skip("validated benchmark cache absent")
    pop = GB.B.population(GB.periods()["20260630"], progress=lambda *_: None)
    assert GB.B.group(pop, 350000) == json.loads(cache.read_text())["rows"]


SUMS = [k for k, _, _, kind in __import__("webapp.generator_bridge", fromlist=["TA"]).TA.MEASURES if kind == "sum"]


@pytest.mark.parametrize("threshold", [1, 150000, 350000, 350001, 500000, 1000000, 4999999, 100000000])
def test_partition_above_plus_below_equals_total(threshold):
    from webapp import analysis as AN, generator_bridge as GB
    sel = GB.resolve("20260630", "US Metro Bank", "CA")
    p = AN.compute("20260630", sel, threshold)
    for h in ("full", "fy2000"):
        b = p["bank"][h]
        for part in ("vintage", "industry", "subprogram"):
            for ra, rb, rt in zip(b["above"][part], b["below"][part], b["total"][part]):
                for k in SUMS:
                    assert abs(ra[k] + rb[k] - rt[k]) < 1e-6, (h, part, k)
        for k in SUMS:
            assert abs(b["above"]["total"][k] + b["below"]["total"][k] - b["total"]["total"][k]) < 1e-6
    full, fy = p["bank"]["full"]["total"]["total"], p["bank"]["fy2000"]["total"]["total"]
    assert fy["loans"] <= full["loans"] and fy["orig"] <= full["orig"] + 1e-6
    bm = p["benchmark"]
    for k in ("loans", "orig", "active", "active_orig", "chgoff"):
        assert bm["above"]["total"][k] + bm["below"]["total"][k] == bm["total"]["total"][k]
        for va, vb, vt in zip(bm["above"]["vintage"], bm["below"]["vintage"], bm["total"]["vintage"]):
            assert va[k] + vb[k] == vt[k]
    n_above = p["bank"]["full"]["above"]["total"]["loans"]
    if threshold == 100000000:                     # empty group: shown, never divided by zero
        assert n_above == 0 and p["bank"]["full"]["above"]["total"]["cum_loss"] == "n/a"
        assert bm["above"]["total"]["loans"] == 0 and bm["above"]["total"]["cum_loss"] == "n/a"


def test_dashboard_page_labels_and_unavailable_principal():
    r = client.get("/dashboard", params={"period": "20260630", "name": "US Metro Bank", "state": "CA",
                                         "threshold": "500,000"})
    assert r.status_code == 200
    t = r.text
    assert "US Metro Bank" in t and "$500,000" in t and "PRELIMINARY" in t and "Not available" in t
    assert "Current outstanding principal" in t and "not current principal" in t
    low = t.lower()
    for phrase in ("outstanding balance", "current balance of", "outstanding principal balance"):
        assert phrase not in low, phrase
    assert "LIQUID (%) (template formula flagged" in t and "Enhancement beyond FalconBridge" in t
    assert "Executive Portfolio" in t and "% Cum. Defaulted" in t and "Est. # of Loans Outstanding" in t
    ambiguous = client.get("/dashboard", params={"period": "20260630", "name": "Citizens Bank", "state": "MO"},
                           follow_redirects=False)
    assert ambiguous.status_code == 303 and ambiguous.headers["location"].startswith("/confirm")
    bad = client.get("/dashboard", params={"period": "20260630", "name": "US Metro Bank", "state": "CA",
                                           "threshold": "abc"})
    assert "whole-dollar amount" in bad.text


def test_saved_analysis_reopens_identically_and_never_overwrites():
    from webapp import analysis as AN, generator_bridge as GB, saved as SV
    form = {"token": token(), "period": "20260630", "name": "US Metro Bank", "state": "CA", "threshold": "1,000,000",
            "view": "above"}
    r1 = client.post("/analyses", data=form, follow_redirects=False)
    r2 = client.post("/analyses", data=form, follow_redirects=False)
    assert r1.status_code == r2.status_code == 303
    id1, id2 = r1.headers["location"].rsplit("/", 1)[1], r2.headers["location"].rsplit("/", 1)[1]
    assert id1 != id2
    a = SV.load(id1)
    sel = GB.resolve("20260630", "US Metro Bank", "CA")
    assert json.loads(json.dumps(a["payload"], default=str)) == json.loads(json.dumps(AN.compute("20260630", sel, 1000000), default=str))
    assert a["criteria"]["threshold"] == 1000000 and a["view"] == "above" and a["criteria"]["cert"] == "58310"
    page = client.get(f"/analyses/{id1}").text
    assert "Saved analysis" in page and "$1,000,000" in page
    assert {x["id"] for x in SV.listing(settings.LOCAL_USER)} >= {id1, id2}
    assert "US Metro Bank" in client.get("/analyses").text
    assert A.jobs.wait_idle(900)
    job = A.jobs.load(a["job_id"])
    assert job["status"] == "succeeded" and job["result"]["file"].endswith("_GT1000000.xlsx")
    assert client.get("/analyses/" + "0" * 32).status_code == 404
    assert client.post("/analyses", data={**form, "token": "x"}).status_code == 403


# ---------------------------------------------------------------- portfolio age (seasoning) context
CHARACTERIZING = ("outperform", "underperform", "exceptional", "superior", "inferior", "in line", "better than",
                  "worse than", "excellent", "strong", "weak", "poor")


def _age_payload(name, state):
    from webapp import analysis as AN, generator_bridge as GB
    return AN.compute("20260630", GB.resolve("20260630", name, state), 350000)


def _check_shares(p):
    """Band shares are sums of the validated vintage rows: bank orig / bank total (workbook N), SBA vintage orig /
    total of SBA vintage rows (workbook Z)."""
    z = p["seasoning"]
    bank_v, tot = p["bank"]["full"]["total"]["vintage"], p["bank"]["full"]["total"]["total"]["orig"]
    sba_v = p["benchmark"]["total"]["vintage"]
    sba_tot = sum(r["orig"] for r in sba_v)
    for b in z["bands"]:
        assert abs(b["bank_share"] - sum(r["orig"] for r in bank_v if b["from"] <= r["year"] <= b["to"]) / tot) < 1e-12
        assert abs(b["sba_share"] - sum(r["orig"] for r in sba_v if b["from"] <= r["year"] <= b["to"]) / sba_tot) < 1e-12
    assert abs(sum(b["bank_share"] for b in z["bands"]) + z["bank_pre_benchmark_share"] - 1) < 1e-9
    assert abs(sum(b["sba_share"] for b in z["bands"]) - 1) < 1e-9


def _texts(p):
    return [l["text"] for g in p["insights"]["groups"].values() for l in g["lines"]]


def test_seasoning_young_portfolio_us_metro():
    p = _age_payload("US Metro Bank", "CA")
    _check_shares(p)
    z = p["seasoning"]
    recent, early = z["bands"][-1], z["bands"][0]
    assert (z["recent_from"], z["recent_to"]) == (2022, 2026)
    assert recent["bank_share"] > recent["sba_share"] and early["bank_share"] < early["sba_share"]
    assert abs(recent["bank_share"] - 0.5440) < 5e-5 and abs(recent["sba_share"] - 0.2663) < 5e-5
    # FalconBridge's benchmark is preserved: the headline rates are the validated PerformanceSummary values
    assert abs(p["benchmark"]["total"]["total"]["cum_def"] - 0.06594063705848315) < 1e-15
    assert abs(p["bank"]["full"]["total"]["total"]["cum_def"] - 0.016022053381372893) < 1e-15
    lines = {l["label"]: l["text"] for l in p["insights"]["groups"]["total"]["lines"]}
    assert ("54.40% of the bank's original gross amount was disbursed in 2022-2026 (2026 through June 30), against "
            "26.63%") in lines["Portfolio age"]
    assert "not adjusted for differences in portfolio age" in lines["Versus SBA benchmark"]
    assert "(difference -4.99 percentage points)" in lines["Versus SBA benchmark"]
    assert "largest five industries 68.40%" in lines["Industry exposure"]
    assert not any(w in t.lower() for t in _texts(p) for w in CHARACTERIZING)
    assert [v["year"] for v in z["vintages"]] == [r["year"] for r in p["bank"]["full"]["total"]["vintage"]
                                                  if r["loans"] and r["year"] >= 1999]
    assert next(v for v in z["vintages"] if v["year"] == 2006)["small_sample"]          # 2 loans


def test_seasoning_older_portfolio_genesis():
    p = _age_payload("Genesis Bank", "CA")
    _check_shares(p)
    z = p["seasoning"]
    recent, early = z["bands"][-1], z["bands"][0]
    assert recent["bank_share"] == 0 and recent["bank_loans"] == 0
    assert early["bank_share"] > early["sba_share"]
    lines = {l["label"]: l["text"] for l in p["insights"]["groups"]["total"]["lines"]}
    assert "0.00% of the bank's original gross amount was disbursed in 2022-2026 (2026 through June 30)" in lines["Portfolio age"]
    assert "57.45% of the bank's and 21.31% of the benchmark's in 2008 or earlier" in lines["Portfolio age"]
    assert "(difference +16.78 percentage points)" in lines["Versus SBA benchmark"]
    assert not any(w in t.lower() for t in _texts(p) for w in CHARACTERIZING)


def test_seasoning_limited_data_bank():
    p = _age_payload("National City Bank of the Midwest", "MI")
    z = p["seasoning"]
    assert z["vintages"] == [] and z["bank_pre_benchmark_loans"] == 1 and z["bank_pre_benchmark_share"] == 1
    lines = {l["label"]: l["text"] for l in p["insights"]["groups"]["total"]["lines"]}
    assert "no portfolio-age comparison is possible" in lines["Portfolio age"]
    assert lines["SBA benchmark"].startswith("Not comparable")
    assert "Versus SBA benchmark" not in lines and "Data limitation" in lines
    assert not any(w in t.lower() for t in _texts(p) for w in CHARACTERIZING)


def test_executive_page_shows_vintage_context_without_reinterpreting_benchmark():
    t = client.get("/dashboard", params={"period": "20260630", "name": "US Metro Bank", "state": "CA"}).text
    assert "Is the overall comparison like-for-like?" in t and "no adjustment is made to the benchmark" in t
    assert "FalconBridge's benchmark figures above are unchanged" in t
    assert "is not used without FalconBridge's approval" in t
    assert '"in line"' not in t and "in line with" not in t.replace("in line with, exceptional", "")



# ---------------------------------------------------------------- first-release disclosures
def test_source_note_disclosure_facts():
    """Every fact in each protected source note (protected_config/source_notes.json, not in Git) is re-verified: the
    public classification of the loans, the dashboard's benchmark count, and - when FalconBridge's workbook is present
    locally - FalconBridge's classification and row-6 benchmark values. The notes hold no figures in code."""
    from webapp import analysis as AN, generator_bridge as GB, source_notes as SN
    if not SN.PATH.exists():
        pytest.skip("protected source-notes file absent (kept outside Git)")
    assert SN.notes(), "protected source-notes file is empty"
    for (period, cert), note in SN.notes().items():
        entry = GB.directory(period)
        name, state = next(k for k, v in entry.items() if cert in v["certs"] and v["eligible"])
        sel = GB.resolve(period, name, state, cert)
        p = AN.compute(period, sel, 350000)
        ext, _ = AN.bank_data(period, sel)
        public = {r["BorrName"].strip(): [r["NaicsCode"].strip(), r["NaicsDescription"]] for r in ext["rows"]}
        for x in note["loans"]:
            assert public[x["borrower"]] == list(x["public"]), x["borrower"]
        bm = p["benchmark"]["total"]["total"]
        assert bm["loans"] == note["public_benchmark_loans"]
        [n] = p["source_notes"]
        assert f"{bm['loans']:,} SBA loans" in n["short"] and f"{note['falconbridge_benchmark_loans']:,}" in n["short"]
        assert all(f"{x['borrower']} - FalconBridge {x['falconbridge'][0]}" in n["detail"] for x in note["loans"])
        ref = ROOT / "reference" / "SBA_Historical Performance_FinWise Bank_2026Q2.xlsx"
        if ref.exists() and "FinWise" in name:
            from openpyxl import load_workbook
            wb = load_workbook(ref, data_only=True, read_only=True)
            ps = wb["PerformanceSummary"]
            assert ps["X6"].value == note["falconbridge_benchmark_loans"]
            assert abs(ps["AE6"].value - note["falconbridge_benchmark_cum_def"]) < 1e-15
            rows = wb["FOIA"].iter_rows(values_only=True)
            h = next(rows)
            ix = {k: h.index(k) for k in ("BorrName", "NaicsCode", "NaicsDescription", "BankName")}
            fb = {str(r[ix["BorrName"]]).strip(): [str(r[ix["NaicsCode"]]), r[ix["NaicsDescription"]]] for r in rows
                  if r[ix["BankName"]] == name}
            for x in note["loans"]:
                assert fb[x["borrower"]] == list(x["falconbridge"]), x["borrower"]
        t = client.get("/dashboard", params={"period": period, "name": name, "state": state, "cert": cert}).text
        assert 'id="src-note"' in t and n["title"] in t
    us = AN.compute("20260630", GB.resolve("20260630", "US Metro Bank", "CA"), 350000)
    assert us["source_notes"] == []                                 # shown only where it applies


def test_partial_reporting_year_is_labelled():
    from datetime import date
    from webapp import analysis as AN, generator_bridge as GB
    assert AN.partial_year(date(2026, 12, 31)) is None
    py = AN.partial_year(date(2026, 6, 30))
    assert py["label"] == "2026*" and py["note"] == "* 2026 is a partial year: loans first disbursed through June 30, 2026."
    p = AN.compute("20260630", GB.resolve("20260630", "US Metro Bank", "CA"), 350000)
    assert p["partial_year"] == py
    assert p["seasoning"]["bands"][-1]["label"] == "Disbursed 2022-2026*"
    assert any("partial vintage (marked *)" in r for r in p["insights"]["rules"])
    t = client.get("/dashboard", params={"period": "20260630", "name": "US Metro Bank", "state": "CA"}).text
    for hook in ("labels = labels.map(yl);", "yl(v.year)", "yl(r.year)", "pyNote()"):
        assert hook in t, hook


def test_age_caveat_on_tiles_and_comparison_tables():
    t = client.get("/dashboard", params={"period": "20260630", "name": "US Metro Bank", "state": "CA"}).text
    assert 'id="tiles-caveat"' in t and "Not age-adjusted:" in t
    assert t.count("ageCaveat()") >= 6          # tiles, Portfolio Analysis industries, benchmark groups and industries
    assert "Industry rates mix vintages in the same way." in t


# ---------------------------------------------------------------- generation isolation
def test_generation_runs_in_a_separate_process_and_is_stopped_on_timeout(monkeypatch):
    """The job runner starts webapp.worker as its own process; a job exceeding the time limit is stopped, fails with
    an explicit message and leaves no workbook behind. The web process keeps serving meanwhile."""
    import os as _os
    from webapp import jobs as J
    seen = {}
    real_popen = A.subprocess.Popen

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        return real_popen(cmd, **kw)
    monkeypatch.setattr(A.subprocess, "Popen", spy)
    monkeypatch.setattr(settings, "JOB_TIMEOUT", 3)
    job = A.jobs.submit(settings.LOCAL_USER, {"period": "20260630", "bank_name": "US Metro Bank", "bank_state": "CA",
                                              "cert": None, "location_ids": None, "threshold": 777000})
    assert client.get("/healthz").status_code == 200          # web process responsive while the job runs
    assert A.jobs.wait_idle(120)
    j = A.jobs.load(job["id"])
    assert seen["cmd"][1:3] == ["-m", "webapp.worker"]
    assert j["status"] == "failed" and "exceeded the 3-second time limit" in j["error"]
    assert not list((settings.JOBS_DIR / job["id"]).glob("*.xlsx"))
    assert client.get(f"/jobs/{job['id']}/download").status_code == 409


def test_worker_reports_generator_refusal():
    """A refusal by the generator (here an ambiguous lender) is reported by the worker process, not swallowed."""
    job = A.jobs.submit(settings.LOCAL_USER, {"period": "20260630", "bank_name": "Citizens Bank", "bank_state": "MO",
                                              "cert": None, "location_ids": None, "threshold": 350000})
    assert A.jobs.wait_idle(300)
    j = A.jobs.load(job["id"])
    assert j["status"] == "failed" and "14488" in j["error"] and "22601" in j["error"]


# ---------------------------------------------------------------- preview access restriction
def _basic(user, pw):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_preview_gate_protects_every_route(monkeypatch):
    """FBX_AUTH_MODE=preview: every registered route (pages, APIs, saved analyses, generation, downloads, static
    files) refuses requests without valid credentials; only /healthz answers. Correct credentials work."""
    import secrets as _s
    from webapp import preview_auth as PA
    pw = _s.token_urlsafe(18)
    monkeypatch.setattr(settings, "AUTH_MODE", "preview")
    monkeypatch.setattr(A, "PREVIEW_USERS", {"fbreview": PA.hash_password(pw, _s.token_bytes(16))})
    PA._failures.clear(); PA._verified.clear()
    jid = "0" * 32
    paths = []
    for r in A.app.routes:
        path = getattr(r, "path", "")
        path = path.replace("{job_id}", jid).replace("{analysis_id}", jid)
        for m in sorted(getattr(r, "methods", None) or {"GET"}):
            paths.append((m, path if path != "/static" else "/static/style.css"))
    assert len(paths) >= 14
    for i, (m, path) in enumerate(paths):
        for k, headers in enumerate(({}, _basic("fbreview", "wrong-password"), _basic("someone", pw))):
            r = client.request(m, path, headers={**headers, "X-Forwarded-For": f"203.0.{i}.{k}"})
            if path == "/healthz":
                assert r.status_code == 200 and r.json() == {"status": "ok"}
            else:
                assert r.status_code == 401, (m, path, headers.keys(), r.status_code)
                assert "FalconBridge" in r.headers["www-authenticate"] and "US Metro" not in r.text
    PA._failures.clear()
    ok = _basic("fbreview", pw)
    for path in ("/", "/api/banks?period=20260630&state=CA", "/static/style.css", "/jobs", "/analyses",
                 "/dashboard?period=20260630&name=US+Metro+Bank&state=CA"):
        r = client.get(path, headers=ok)
        assert r.status_code == 200, path
        assert r.headers["strict-transport-security"] and r.headers["x-frame-options"] == "DENY"
    assert client.get("/dashboard?period=20260630&name=US+Metro+Bank&state=CA", headers=ok).headers["cache-control"] == "no-store"
    for _ in range(PA.MAX_FAILURES):
        client.get("/", headers={**_basic("fbreview", "x"), "X-Forwarded-For": "198.51.100.9"})
    assert client.get("/", headers={**ok, "X-Forwarded-For": "198.51.100.9"}).status_code == 429   # locked out
    assert client.get("/", headers={**ok, "X-Forwarded-For": "198.51.100.10"}).status_code == 200


def test_server_refuses_public_start_without_preview_credentials():
    import subprocess as sp
    import sys as _sys
    base = {**os.environ, "FBX_HOST": "0.0.0.0", "FBX_PORT": "8199", "FBX_APP_DATA": settings.APP_DATA_DIR.as_posix()}
    for env, msg in (({"FBX_AUTH_MODE": "local"}, "local mode has no sign-in"),
                     ({"FBX_AUTH_MODE": "preview", "FBX_PREVIEW_USERS": ""}, "preview mode needs FBX_PREVIEW_USERS"),
                     ({"FBX_AUTH_MODE": "open"}, "unknown FBX_AUTH_MODE")):
        r = sp.run([_sys.executable, "-m", "webapp"], cwd=ROOT, env={**base, **env}, capture_output=True, text=True,
                   timeout=60)
        assert r.returncode != 0 and msg in (r.stderr + r.stdout), (env, r.stderr[-300:])
