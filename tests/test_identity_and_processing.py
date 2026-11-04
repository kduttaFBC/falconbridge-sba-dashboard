"""Lender identity resolution, date parsing and text safety. Real-data tests use the 2026-06-30 release folder."""
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bank_workbook_generator"))
import config as C                                   # noqa: E402
import source_data as S                              # noqa: E402
from foia_xml_writer import CellWriter, _date_serial  # noqa: E402

RELEASE = C.SOURCE_DIR / "20260630"
real = pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")


def lender(cert, loc, eligible=10, city="X"):
    return {"cert": cert, "location_id": loc, "records": eligible, "eligible": eligible, "city": city, "street": "s"}


def directory(groups):
    return S._directory_from_rows([[n, s, [[x["cert"], x["location_id"], x["records"], x["eligible"], x["city"], x["street"]]
                                          for x in ls]] for (n, s), ls in groups.items()])


SYN = directory({
    ("Alpha Bank", "UT"): [lender("100", "1"), lender("100", "2")],                 # one bank, two SBA lender IDs
    ("Citizens Bank", "MO"): [lender("200", "3"), lender("201", "4")],              # two different banks
    ("Lender Co", "CA"): [lender("", "5"), lender("", "6")],                        # no FDIC cert, two IDs
    ("Big Bank", "AR"): [lender("300", "7")], ("Big Bank", "FL"): [lender("300", "8")],
    ("Mixed Bank", "TX"): [lender("400", "9"), lender("", "10")],
})


def test_one_cert_combines_its_location_ids():
    sel = S.resolve_lender(SYN, cert="100")
    assert (sel["bank_name"], sel["status"], sel["location_ids"]) == ("Alpha Bank", "UNIQUE", ["1", "2"])
    assert S.resolve_lender(SYN, bank_name="Alpha Bank", bank_state="UT")["cert"] == "100"


def test_shared_name_and_state_is_refused_without_cert():
    with pytest.raises(S.LenderSelectionError, match="different lenders"):
        S.resolve_lender(SYN, bank_name="Citizens Bank", bank_state="MO")
    sel = S.resolve_lender(SYN, cert="201", bank_name="Citizens Bank", bank_state="MO")
    assert sel["status"] == "RESTRICTED_TO_FDIC_CERT" and sel["location_ids"] == ["4"]
    assert [x["cert"] for x in sel["excluded"]] == ["200"]


def test_cert_with_and_without_records_is_refused():
    with pytest.raises(S.LenderSelectionError, match="different lenders"):
        S.resolve_lender(SYN, bank_name="Mixed Bank", bank_state="TX")


def test_no_cert_several_location_ids_needs_explicit_ids():
    with pytest.raises(S.LenderSelectionError, match="no FDIC certificate"):
        S.resolve_lender(SYN, bank_name="Lender Co", bank_state="CA")
    sel = S.resolve_lender(SYN, bank_name="Lender Co", bank_state="CA", location_ids=["0000006"])
    assert sel["status"] == "RESTRICTED_TO_LOCATION_IDS" and sel["location_ids"] == ["6"]


def test_cert_under_several_names_needs_name_and_state():
    with pytest.raises(S.LenderSelectionError, match="2 Bank Name"):
        S.resolve_lender(SYN, cert="300")
    assert S.resolve_lender(SYN, cert="300", bank_name="Big Bank", bank_state="FL")["location_ids"] == ["8"]


def test_unknown_selections_are_refused():
    for kw in ({"cert": "999"}, {"bank_name": "Nope", "bank_state": "ZZ"}, {"cert": "100", "bank_name": "Citizens Bank",
                                                                              "bank_state": "MO"}):
        with pytest.raises(S.LenderSelectionError):
            S.resolve_lender(SYN, **kw)
    with pytest.raises(S.LenderSelectionError, match="not records of the selected lender"):
        S.resolve_lender(SYN, cert="100", location_ids=["9"])


def test_dates_in_both_published_formats():
    assert S.parse_date("2026-06-30") == S.parse_date("6/30/2026") == date(2026, 6, 30)
    assert _date_serial("2026-06-30") == _date_serial("6/30/2026") == "46203"


def test_snapshot_as_of_refuses_mixed_or_wrong_period():
    assert S.snapshot_as_of(["2026-06-30"], date(2026, 6, 30)) == date(2026, 6, 30)
    with pytest.raises(S.SourceError, match="mix"):
        S.snapshot_as_of(["2026-06-30", "2026-03-31"])
    with pytest.raises(S.SourceError, match="not the selected period"):
        S.snapshot_as_of(["2026-06-30"], date(2026, 9, 30))


def test_text_is_never_turned_into_a_formula_or_number():
    cw = CellWriter()
    for field, raw in (("BorrName", "=1+1"), ("BorrName", "-ment Fitness Dallas, LLC"), ("BorrName", "+design LLC"),
                       ("BorrZip", "02108"), ("CongressionalDistrict", "04"), ("FranchiseCode", "00123")):
        xml = cw.value("C2", field, raw, "")
        assert 't="inlineStr"' in xml and "<f>" not in xml and raw.replace("&", "&amp;") in xml, (field, raw, xml)


@real
def test_real_release_identity_cases():
    d, as_of = S.lender_directory(RELEASE, progress=lambda *_: None)
    assert S.snapshot_as_of(as_of, date(2026, 6, 30)) == date(2026, 6, 30)
    for cert, name in (("35323", "FinWise Bank"), ("27417", "Shoreham Bank")):
        sel = S.resolve_lender(d, cert=cert)
        assert sel["bank_name"] == name and sel["status"] == "UNIQUE"
    with pytest.raises(S.LenderSelectionError, match="different lenders"):
        S.resolve_lender(d, bank_name="Citizens Bank", bank_state="MO")
    with pytest.raises(S.LenderSelectionError, match="Bank Name \\+ Bank State combinations"):
        S.resolve_lender(d, cert="28100")          # IBERIABANK, published under AR, FL and LA


@real
def test_generated_workbooks_are_self_contained():
    for f in sorted(p for p in (ROOT / "output" / "20260630").glob("*.xlsx") if not p.name.startswith("~$")):  # skip Excel lock files
        with zipfile.ZipFile(f) as z:
            assert not [n for n in z.namelist() if "externalLink" in n], f.name
            assert b"[1]" not in z.read("xl/workbook.xml"), f.name


@real
def test_foia_sheet_size_label_matches_rows():
    """The FOIA sheet's <dimension> must cover every written row (a stale A1:BC2 hides rows from some readers)."""
    for f in sorted(p for p in (ROOT / "output" / "20260630").glob("*.xlsx") if not p.name.startswith("~$")):
        with zipfile.ZipFile(f) as z:
            wb = z.read("xl/workbook.xml").decode()
            rels = z.read("xl/_rels/workbook.xml.rels").decode()
            rid = re.search(r'<sheet [^>]*name="FOIA"[^>]*r:id="([^"]+)"', wb).group(1)
            target = re.search(rf'<Relationship [^>]*Id="{rid}"[^>]*Target="([^"]+)"', rels) or \
                re.search(rf'<Relationship [^>]*Target="([^"]+)"[^>]*Id="{rid}"', rels)
            path = target.group(1).lstrip("/")
            xml = z.read(path if path.startswith("xl/") else "xl/" + path).decode("utf-8", "ignore")
        last = int(re.search(r'<dimension ref="[A-Z]+\d+:[A-Z]+(\d+)"', xml).group(1))
        rows = [int(r) for r in re.findall(r'<row r="(\d+)"', xml)]
        assert last == max(rows), (f.name, last, max(rows))
