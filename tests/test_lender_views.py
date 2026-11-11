"""Executive / Lender view, Analyst evidence view, presentation mode and Lender claims assessment (local demonstration).

Saved Analyses for US Metro, FinWise and Shoreham (and a one-loan, pre-FY2000 lender for missing-evidence handling)
are computed by the existing engine from the 2026-06-30 release and saved into a TEMPORARY app-data folder; the
project's own app_data/ is never read or written. Offline: no network connection is opened.
"""
import hashlib
import html as html_lib
import os
import re
import socket
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("FBX_APP_DATA", tempfile.mkdtemp(prefix="fbx_views_test_"))
from fastapi.testclient import TestClient   # noqa: E402

from webapp import analysis as AN            # noqa: E402
from webapp import app as A                  # noqa: E402
from webapp import claims_assessment as CA   # noqa: E402
from webapp import evidence_sections as ES  # noqa: E402
from webapp import generator_bridge as GB    # noqa: E402
from webapp import intel_adapter as IA      # noqa: E402
from webapp import presentation as PR        # noqa: E402
from webapp import saved as SV               # noqa: E402
from webapp import settings                  # noqa: E402

RELEASE = ROOT / "source_data" / "20260630"
pytestmark = pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
client = TestClient(A.app)
BANKS = {"metro": ("US Metro Bank", "CA", "58310"), "finwise": ("FinWise Bank", "UT", "35323"),
         "shoreham": ("Shoreham Bank", "RI", "27417"), "tiny": ("National City Bank of the Midwest", "MI", "5029")}
EV = re.compile(r"EV-[0-9a-f]{24}")


def test_uses_a_temporary_app_data_folder():
    assert settings.APP_DATA_DIR != (ROOT / "app_data").resolve()
    assert ROOT not in settings.APP_DATA_DIR.parents


@pytest.fixture(scope="module")
def saved():
    out = {}
    for k, (name, state, cert) in BANKS.items():
        sel = GB.resolve("20260630", name, state, cert)
        payload = AN.compute("20260630", sel, GB.GC.BENCHMARK_STANDARD_MIN)
        out[k] = SV.save(settings.LOCAL_USER, payload, "0" * 32, "both")
    return out


def html(path, **params):
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text[:500]
    return r.text


def text_of(page):
    """Visible text, HTML entities resolved enough for comparisons."""
    t = re.sub(r"<[^>]+>", " ", page)
    t = html_lib.unescape(t)
    return re.sub(r"\s+", " ", t)


# ------------------------------------------------------------------------------------------------------- views
@pytest.mark.parametrize("bank", ["metro", "finwise", "shoreham"])
def test_three_demo_banks_render_with_verified_findings(saved, bank):
    a = saved[bank]
    m = PR.build(a)
    assert m["bank"]["name"] == BANKS[bank][0] and m["as_of"] == "2026-06-30"
    assert 3 <= len(m["findings"]) <= 5 and not m["withheld"]
    assert all(f["outcome"] == "ACCEPTED" for f in m["findings"] + m["compare"] + m["limitations"])
    ex = text_of(html(f"/analyses/{a['id']}/executive"))
    an = text_of(html(f"/analyses/{a['id']}/evidence"))
    for f in m["findings"]:
        assert f["text"] in ex and f["text"] in an
    for r in m["compare"]:
        for v in (r["bank"], r["sba"], r["diff"]):
            assert v in ex and v in an


def test_values_come_from_the_existing_calculation_engine(saved):
    """Executive and analyst values equal the engine's own payload (no second calculation)."""
    a = saved["metro"]
    p, m = a["payload"], PR.build(a)
    cmp = {r["measure"]: r for r in m["compare"]}
    for k in ("cum_def", "cum_loss", "severity"):
        assert cmp[k]["bank"] == PR.pct(p["comparison"]["total"][k]["bank"])
        assert cmp[k]["sba"] == PR.pct(p["comparison"]["total"][k]["sba"])
    assert m["headline"]["bank"] == "1.60%" and m["headline"]["direction"] == "Lower"
    assert any(t["shown"] == "937" for t in m["tiles"])
    reg = m["register_index"]
    for r in m["compare"]:
        b, s = (reg[i] for i in r["evidence"])
        assert b["value"] == p["comparison"]["total"][r["measure"]]["bank"]
        assert s["value"] == p["comparison"]["total"][r["measure"]]["sba"]


def test_every_finding_links_to_resolvable_evidence(saved):
    a = saved["shoreham"]
    m = PR.build(a)
    ex, an = html(f"/analyses/{a['id']}/executive"), html(f"/analyses/{a['id']}/evidence")
    anchors = set(re.findall(r'id="([^"]+)"', an))
    for f in m["findings"]:
        assert f'href="/analyses/{a["id"]}/evidence#{f["ref"]}"' in ex
        assert f["ref"] in anchors
        assert f["evidence"] and all(i in anchors for i in f["evidence"])
    for r in m["compare"] + m["limitations"]:
        assert all(i in anchors for i in r["evidence"])
    for d in m["register"]:                                          # derivations resolve inside the register
        assert all(x in anchors for x in d["derived_from"])
    for anchor in ("compare", "limitations", "vintages", "register", "unavailable", "provenance"):
        assert anchor in anchors
    assert f'href="/analyses/{a["id"]}/executive"' in an             # navigation back to the executive summary


def test_analyst_view_shows_periods_populations_denominators_and_status(saved):
    a = saved["metro"]
    an = text_of(html(f"/analyses/{a['id']}/evidence"))
    for s in ("as of 2026-06-30", "Bank loans approved FY2000 onward", "All of the bank's loans in SBA FOIA data",
              "denominator: original gross approval amount of the population", "Reconciled to FalconBridge workbook",
              "Preliminary methodology", "Proposed interpretation", "Unavailable evidence", "Checked against recorded evidence",
              "none attached (no value is independently verified)", "Payload SHA-256", "Vintage history"):
        assert s in an, s
    assert "Independently verified:" not in an                       # no record -> nothing claims that status
    assert "Verified finding" not in an                             # M2 checks are not presented as validation


def test_no_value_is_independently_verified_without_a_record(saved):
    m = PR.build(saved["metro"])
    assert all(d["status"] != "INDEPENDENTLY_VERIFIED" for d in m["register"])
    assert not m["provenance"]["validations_applied"]
    assert any("no independent validation record" in x["text"] for x in m["limitations"])


def test_wording_is_factual_not_evaluative(saved):
    words = re.compile(r"\b(better|worse|strong|weak|outperform\w*|underperform\w*|superior|inferior|eligible|"
                       r"healthy|risky)\b", re.I)
    for k in ("metro", "finwise", "shoreham"):
        m = PR.build(saved[k])
        for item in m["findings"] + m["compare"] + m["limitations"] + [m["headline"]]:
            assert not words.search(item["text"]), item["text"]


def test_missing_benchmark_evidence_is_handled_clearly(saved):
    a = saved["tiny"]
    m = PR.build(a)
    assert not m["compare"] and m["headline"]["bank"] is None
    assert any("no loans approved FY2000 onward" in x["text"] for x in m["limitations"])
    assert any("fewer than 30 loans" in x["text"] for x in m["limitations"])
    ex = text_of(html(f"/analyses/{a['id']}/executive"))
    assert "No benchmark comparison is available" in ex and "Bank vs SBA benchmark" not in ex
    an = text_of(html(f"/analyses/{a['id']}/evidence"))
    assert "No comparison: the bank has no loans approved FY2000 onward." in an


# ------------------------------------------------------------------------------------------------ presentation
def test_presentation_mode_is_an_allow_list(saved):
    a = saved["metro"]
    m = PR.build(a)
    m["findings"][0]["analyst_note"] = "INTERNAL-NOTE-SHOULD-NOT-APPEAR"
    m["internal_comment"] = "INTERNAL-NOTE-SHOULD-NOT-APPEAR"
    pres = PR.presentation(m)
    assert set(pres) == set(PR.PRESENT_FIELDS["top"]) | {"headline", "tiles", "findings", "compare", "limitations",
                                                        "concentration"}
    for kind, rows in (("finding", pres["findings"]), ("compare", pres["compare"]), ("limitation", pres["limitations"]),
                       ("tile", pres["tiles"])):
        for row in rows:
            assert set(row) <= set(PR.PRESENT_FIELDS[kind])
    assert "INTERNAL-NOTE" not in repr(pres) and not EV.search(repr(pres))


def test_presentation_page_excludes_analyst_and_internal_content(saved):
    a = saved["metro"]
    m = PR.build(a)
    page = html(f"/analyses/{a['id']}/executive", mode="present")
    visible = text_of(page)
    for f in m["findings"]:
        assert f["text"] in visible
    assert not EV.search(page)
    for forbidden in (a["id"], a["job_id"], m["provenance"]["payload_sha256"], m["pack_fingerprint"],
                      "Proposed interpretation", "may partly reflect", "NEEDS_ANALYST_REVIEW", "Verifier",
                      "CL-view", "/" + "Users" + "/", "source_data", "Analyst evidence", "Claims assessment"):
        assert forbidden not in page, forbidden
    for p in m["proposals"]:
        assert p["text"] not in page
    assert "Preliminary benchmark." in page and page.count("Preliminary benchmark.") == 1


# ------------------------------------------------------------------------------------------------------ claims
def assess(a, text, **choices):
    return CA.assess(PR.pack_for(a), text, choices)


def test_example_claims(saved):
    m, s = saved["metro"], saved["shoreham"]
    r = assess(m, "Our historical default rate is below 2%.")
    assert r["outcome"] == CA.CLARIFY and {c["field"] for c in r["clarify"]} == {"metric", "period"}
    assert [o["value"] for o in r["clarify"][0]["options"]] == ["cum_def"]          # only defined measures offered
    r = assess(m, "Our historical default rate is below 2%.", metric="cum_def", period="2026-06-30")
    assert r["outcome"] == CA.SUPPORTED and r["evidence"][0]["shown"] == "1.60%"
    assert r["methodology_approved"] is False and "not yet confirmed" in r["methodology"]
    r = assess(s, "Our historical default rate is below 2%.", metric="cum_def", period="2026-06-30")
    assert r["outcome"] == CA.CONTRADICTED and r["evidence"][0]["shown"] == "4.63%"
    r = assess(m, "Our SBA portfolio performs better than the industry benchmark.")
    assert r["outcome"] == CA.CANNOT and r["context"] and "superior performance" in " ".join(r["reasons"])
    assert r["m2"]["outcome"] == "NEEDS_ANALYST_REVIEW"
    r = assess(m, "Our historical chargeoffs are minimal.")
    assert r["outcome"] == CA.CLARIFY and "minimal" in " ".join(r["reasons"])


@pytest.mark.parametrize("text,choices,outcome", [
    ("Our % Cum. Defaulted for loans approved FY2000 onward is lower than the SBA benchmark as of June 30, 2026.", {},
     CA.SUPPORTED),
    ("Our % Cum. Defaulted over our full history is lower than the SBA benchmark as of June 30, 2026.", {}, CA.CLARIFY),
    ("Our % Cum. Loss over our full history is below 0.5% as of June 30, 2026.", {}, CA.CONTRADICTED),
    ("Our % Cum. Loss over our full history is below 1% as of June 30, 2026.", {}, CA.SUPPORTED),
    ("Our historical chargeoffs are below $5 million as of June 30, 2026.", {}, CA.CONTRADICTED),
    ("Fewer than 50 loans charged off over our full history as of 2026Q2.", {}, CA.SUPPORTED),
    ("Our % Cum. Defaulted is below 2% as of June 30, 2025.", {"population": "full"}, CA.CANNOT),
    ("Our delinquency rate is below 1% as of June 30, 2026.", {}, CA.CANNOT),
    ("We outperform our peers.", {}, CA.CANNOT),
    ("Our default rate is low.", {}, CA.CLARIFY),
    ("Our % Cum. Defaulted over our full history is below $5 million as of June 30, 2026.", {}, CA.CLARIFY),
    ("Our number of charged-off loans is lower than the SBA benchmark for loans approved FY2000 onward as of "
     "June 30, 2026.", {}, CA.CLARIFY),
    ("", {}, CA.CLARIFY),
])
def test_claim_outcomes_us_metro(saved, text, choices, outcome):
    r = assess(saved["metro"], text, **choices)
    assert r["outcome"] == outcome, (r["reasons"], r["clarify"])
    if outcome in (CA.SUPPORTED, CA.CONTRADICTED):
        assert r["statement"]["outcome"] == "ACCEPTED" and r["evidence"]


def test_claims_never_use_a_figure_that_is_not_evidence(saved):
    a = saved["tiny"]
    r = assess(a, "Our % Cum. Defaulted for loans approved FY2000 onward is lower than the SBA benchmark as of "
                  "June 30, 2026.")
    assert r["outcome"] == CA.CANNOT and not r["evidence"]


def test_claims_assessment_is_reproducible(saved):
    a, b = saved["metro"], saved["finwise"]
    t = "Our historical default rate is below 2%."
    r1, r2 = assess(a, t, metric="cum_def", period="2026-06-30"), assess(a, t, metric="cum_def", period="2026-06-30")
    assert r1 == r2
    assert assess(b, t, metric="cum_def", period="2026-06-30")["assessment_id"] != r1["assessment_id"]
    p1 = html(f"/analyses/{a['id']}/claims", claim=t, metric="cum_def", period="2026-06-30")
    p2 = html(f"/analyses/{a['id']}/claims", claim=t, metric="cum_def", period="2026-06-30")
    assert r1["assessment_id"] in p1 and re.sub(r"\s+", " ", p1) == re.sub(r"\s+", " ", p2)


def test_claims_page_flow(saved):
    a = saved["metro"]
    page = text_of(html(f"/analyses/{a['id']}/claims", preset="historical-default-2pct"))
    assert "Needs clarification" in page and "Which defined measure" in page and "As of which reporting date" in page
    page = html(f"/analyses/{a['id']}/claims", claim="Our historical default rate is below 2%.", metric="cum_def",
                period="2026-06-30")
    assert "Supported by the numbers" in page and "Not yet approved" in page
    ids = EV.findall(page)
    assert ids
    ev = html(f"/analyses/{a['id']}/evidence", ev=ids[:2])
    assert f'id="{ids[0]}"' in ev and "Requested evidence" in ev
    page = text_of(html(f"/analyses/{a['id']}/claims", preset="better-than-benchmark"))
    assert "Cannot be established" in page and "Measured differences (context, not a conclusion)" in page
    assert "not statements made by US Metro Bank" in page


# ------------------------------------------------------------------------------------------ safety and access
def test_views_do_not_modify_saved_analyses_or_create_records(saved):
    d = settings.APP_DATA_DIR
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (d / "analyses").rglob("*.json")}
    jobs = sorted((d / "jobs").iterdir()) if (d / "jobs").exists() else []
    for a in saved.values():
        html(f"/analyses/{a['id']}/executive")
        html(f"/analyses/{a['id']}/executive", mode="present")
        html(f"/analyses/{a['id']}/evidence")
        html(f"/analyses/{a['id']}/claims", preset="better-than-benchmark")
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (d / "analyses").rglob("*.json")}
    assert before == after
    assert (sorted((d / "jobs").iterdir()) if (d / "jobs").exists() else []) == jobs


def test_views_and_claims_are_offline(saved, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    a = saved["finwise"]
    PR._cache.clear()
    PR.build(a)
    assess(a, "Our % Cum. Loss over our full history is below 1% as of June 30, 2026.")


def test_access_controls_and_bad_input(saved, monkeypatch):
    a = saved["metro"]
    assert client.get("/analyses/" + "f" * 32 + "/executive").status_code == 404
    assert client.get("/analyses/not-an-id/evidence").status_code == 404
    page = html(f"/analyses/{a['id']}/evidence", ev=["EV-" + "0" * 24, "<script>"])
    assert page.count("<script>") == 1 and "Not found in this analysis" in page   # only the page's own script
    page = html(f"/analyses/{a['id']}/claims", claim="<script>alert(1)</script> below 2%", metric="bogus",
                population="x", period="2026-13-99")
    assert "<script>alert(1)</script>" not in page
    monkeypatch.setattr(settings, "LOCAL_USER", "someone-else")
    assert client.get(f"/analyses/{a['id']}/executive").status_code == 403


def test_bank_selection_lists_every_saved_analysis(saved):
    page = html("/analyses")
    for a in saved.values():
        assert f'href="/analyses/{a["id"]}/executive"' in page
    ex = html(f"/analyses/{saved['metro']['id']}/executive")
    for k in ("finwise", "shoreham"):
        assert f'value="{saved[k]["id"]}"' in ex and BANKS[k][0] in ex


# ----------------------------------------------------------------- analyst evidence organized by financial section
SECTIONS = ["performance", "age", "industry", "benchmark", "supplementary", "methodology"]


def test_evidence_page_follows_the_dashboard_sections_with_financials_first(saved):
    a = saved["metro"]
    page = html(f"/analyses/{a['id']}/evidence")
    pos = [page.index(f'class="card ae-sec" id="{sid}"') for sid in SECTIONS]
    assert pos == sorted(pos)
    perf = page[pos[0]:pos[1]]
    assert perf.index('<table class="fin') < perf.index("Findings") < perf.index('<details class="support"')
    assert '<details class="support" open' not in page and "<details open" not in page     # collapsed by default
    assert perf.count('class="ae-status"') == 1                      # one concise validation line per section
    assert page.count("Checked against recorded evidence") >= 5 and "Verified finding" not in page


def test_evidence_tables_show_the_dashboard_figures(saved):
    """Figures in the dashboard's own formats (values seen on the November 4, 2026 preview for US Metro)."""
    a = saved["metro"]
    t = text_of(html(f"/analyses/{a['id']}/evidence"))
    for s in ("# of Loans Orig. 937 905,950 219,702", "Orig. Bal. ($) $1.54B $316.39B $246.70B",
              "Average Balance ($) $1.6M $349K $1.1M", "Est. Orig. Bal. of loans Outstanding ($) $1.02B $121.34B $104.35B",
              "% Cum. Defaulted 1.60% 6.59% 5.17% \u22124.99 pp below SBA", "% Loss Severity \u2020 42.77% 66.46% 59.64% "
              "\u221223.68 pp below SBA", "2006 small sample 2 $681K 0 0 0.00% 0.00% 56,917 30.31% 20.52%",
              "Small (<= $350,000) 686,248 11.65% 134 8.22% \u22123.43 pp below SBA",
              "Hotels (except Casino Hotels) and Motels 721110 49.33% 267 $757.9M 191 0.25% 0.12% 18,149 3.52% 1.90%",
              "Car Washes 20 8.74% 6.55% +2.20 pp above SBA 3.00% 3.85%"):
        assert s in t, s


def test_dashboard_rounding_is_reproduced():
    assert ES.fixed(680.5, 0) == "681" and ES.fixed(-0.125, 2) == "-0.13" and ES.f_money(680500.0) == "$681K"
    assert ES.f_pp(-0.0499) == "\u22124.99 pp" and ES.FMT["pct"](0.016022053381372893) == "1.60%"


def test_pre_1999_vintages_are_kept_but_collapsed_as_in_the_dashboard(saved):
    a = saved["shoreham"]
    secs = ES.build_sections(PR.build(a), PR.pack_for(a))
    age = secs[1]
    main = [t for t in age["tables"] if t.get("id") == "vintages"][0]
    assert main["rows"][0]["cells"][0]["text"] == "1999"
    assert age["more"] and age["more"][0]["rows"][0]["cells"][0]["text"] == "1992"


def test_every_table_figure_is_an_evidence_record(saved):
    a = saved["finwise"]
    pack = PR.pack_for(a)
    by_id = {r.evidence_id: r for r in pack.records}
    cells = [c for s in ES.build_sections(PR.build(a), pack) for t in s["tables"] + s.get("more", [])
             for r in t["rows"] for c in r["cells"] if c.get("ev")]
    assert len(cells) > 500
    for c in cells:
        r = by_id[c["ev"]]                                           # the record exists in this analysis's pack
        shown = [ES.FMT[f](r.value) for f in ES.FMT if f != "text"] if PR._num(r.value) else [r.value]
        assert any(c["text"] == x or c["text"].startswith(x + " ") for x in shown), (c, r.value)


def test_existing_deep_links_still_resolve(saved):
    a = saved["metro"]
    ev = html(f"/analyses/{a['id']}/evidence")
    ex = html(f"/analyses/{a['id']}/executive")
    anchors = re.findall(r'id="([^"]+)"', ev)
    for target in set(re.findall(rf'href="/analyses/{a["id"]}/evidence#([^"]+)"', ex)):
        assert target in anchors, target
    claim = html(f"/analyses/{a['id']}/claims", claim="Our historical default rate is below 2%.", metric="cum_def",
                 period="2026-06-30")
    link = re.search(r'href="(/analyses/[0-9a-f]+/evidence\?ev=(EV-[0-9a-f]{24})#EV-[0-9a-f]{24})"', claim)
    page = html(link.group(1).split("#")[0].replace(f"/analyses/{a['id']}/evidence", f"/analyses/{a['id']}/evidence"),
                ev=[link.group(2)])
    assert page.count(f'id="{link.group(2)}"') == 1                  # one target, shown in Requested evidence
    for i in re.findall(r'id="(EV-[0-9a-f]{24})"', html(f"/analyses/{a['id']}/evidence")):
        assert ev.count(f'id="{i}"') == 1


def test_proposed_interpretations_are_marked_beside_the_age_finding(saved):
    a = saved["metro"]
    page = html(f"/analyses/{a['id']}/evidence")
    age = page[page.index('id="age"'):page.index('id="industry"')]
    assert "Proposed interpretation" in age and "not established, not approved" in age and "may partly reflect" in age
    assert "Limitations:" in page[page.index('id="F3"'):page.index('id="F4"')]


def test_naics_evidence_adds_records_without_changing_existing_ones(saved, monkeypatch):
    a = saved["metro"]
    new = IA.extract(a)
    naics = [r for r in new.records if r.metric_key.endswith(".naics")]
    p = a["payload"]
    expected = {(h, g, row["name"]): row["naics"] for h in p["bank"] for g in p["bank"][h]
                for row in p["bank"][h][g]["industry"] if row.get("loans") and row.get("naics")}
    assert len(naics) == len(expected) > 0
    for r in naics:
        _, h, g, _, *name, _ = r.metric_key.split(".")
        assert r.unit == "text" and r.value == expected[(h, g, ".".join(name))]
        assert IA.NAICS_NOTE in r.limitations
    monkeypatch.delitem(IA.BANK, "naics")                            # the adapter as it was before NAICS records
    old = IA.extract(a)
    kept = [r for r in new.records if not r.metric_key.endswith(".naics")]
    assert [x.to_dict() for x in kept] == [x.to_dict() for x in old.records]


def test_naics_codes_are_exact_for_the_demo_banks(saved):
    """Each industry of the three demo banks has exactly one NAICS code in the source rows, so the workbook's
    'Min of NaicsCode' is the industry's code (read-only check of the 2026-06-30 release)."""
    for k in ("metro", "finwise", "shoreham"):
        name, state, cert = BANKS[k]
        ext, _ = AN.bank_data("20260630", GB.resolve("20260630", name, state, cert))
        codes = {}
        for r in ext["rows"]:
            codes.setdefault(r[GB.GC.F_SECTOR], set()).add(r.get("NaicsCode", "").strip())
        assert all(len(v) == 1 for v in codes.values()), k
