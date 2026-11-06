"""D-1 / D-1c template corrections (ChgOffs_byNAICs&ChgOffYr) and the year-alignment scanner.

The corrections are applied to generated copies only; FalconBridge's template must keep its original formulas.
Expected values: independent recomputation from the 2026-06-30 FOIA source data (Decimal arithmetic, no generator
code), reports/FalconBridge_D1_Formula_Correction_Validation_2026-11-05.md section 5.
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bank_workbook_generator"))
import config as C                                    # noqa: E402
import formula_alignment as FA                        # noqa: E402
import template_corrections as TC                     # noqa: E402

SH = FA.SHEET
TEMPLATE_SHA256 = "e68af3f751d0ef26c2ebe6292dd9c2478b50a8b907102695b8c93227742fb1a8"
US_METRO_ORIGINAL = ROOT / "reference" / "benchmark" / "SBA_Historical Performance_US Metro Bank_2026Q2.xlsx"
RECALC = ROOT / "acceptance" / "benchmark" / "generated_recalc"
RELEASE = C.SOURCE_DIR / "20260630"
# FalconBridge's template is confidential and not in Git: skip (do not fail) on a checkout without the local data
pytestmark = pytest.mark.skipif(not C.TEMPLATE_PATH.exists(), reason="FalconBridge template absent (not in Git)")
APPROVED = {
    "DL2": ("=IFERROR(BZ2/AK2,0)", "=IFERROR(BY2/AK2,0)"),
    "DM2": ("=IFERROR(CA2/AL2,0)", "=IFERROR(BZ2/AL2,0)"),
    "MV2": ('=IFERROR(ID2/GP2,"")', '=IFERROR(IC2/GP2,"")'),
    "MW2": ('=IFERROR(IE2/GQ2,"")', '=IFERROR(ID2/GQ2,"")'),
    "DO2": ('=SUM(AP2:BX2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,"<="&$DM$3,FOIA!$AS:$AS,$A$1)',
            '=SUM(AP2:BZ2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,"<="&$DM$3,FOIA!$AS:$AS,$A$1)'),
    "DP2": ('=SUM(BF2:BX2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,">="&2006,FOIA!$AO:$AO,"<="&$DM$3,'
            'FOIA!$AS:$AS,$A$1)',
            '=SUM(BF2:BZ2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,">="&2006,FOIA!$AO:$AO,"<="&$DM$3,'
            'FOIA!$AS:$AS,$A$1)'),
    "FA2": (None, "=SUM(FA4:FA1331)"),                     # D-1d: blank in the template
}
TEMPLATE_FINDINGS = ["DL2", "DM2", "DO2", "DP2", "FA2", "MV2", "MW2"]   # the 7 approved corrections (D-1, D-1c, D-1d)
# D-1d: loans paid in full in 2025 (FOIA source data) and paid-in-full counts in 1990-2005, which the template's Grand
# Total row intentionally does not show (it starts at 2006) but FD2 ("Total") includes
PIF_2025 = {"US_Metro_Bank_CA": 54, "FinWise_Bank_UT": 45, "Shoreham_Bank_RI": 27}
PIF_BEFORE_2006 = {"US_Metro_Bank_CA": 0, "FinWise_Bank_UT": 0, "Shoreham_Bank_RI": 274}


def shown(original):
    return original if original is not None else "(blank cell - no formula)"
# independent values (FOIA source data): DL2, DM2, MV2, MW2, DO2/DP2 numerators and denominators
EXPECTED = {
    "US_Metro_Bank_CA": {"DL2": 0.13399114634402261, "DM2": 0.031120961321577951, "MV2": "", "MW2": "",
                         "pif_all": 490441600, "orig_le2026": 1536344900, "pif_2006": 490441600, "orig_2006": 1536344900,
                         "DO2": 0.31922623624421834, "DP2": 0.31922623624421834},
    "FinWise_Bank_UT": {"DL2": 0.095014237531451424, "DM2": 0.045841233211100107, "MV2": 0.89939610625,
                        "MW2": 0.87728271111111111, "pif_all": 342846500, "orig_le2026": 951646900,
                        "pif_2006": 342846500, "orig_2006": 951346900,
                        "DO2": 0.36026650220790926, "DP2": 0.36038010950579647},
    "Shoreham_Bank_RI": {"DL2": 0.11606024007146820, "DM2": 0.047455680920472398, "MV2": 0.26345565925925926,
                         "MW2": 0.94744583333333333, "pif_all": 493326600, "orig_le2026": 1142889850,
                         "pif_2006": 432997300, "orig_2006": 1013815300,
                         "DO2": 0.43164842176172971, "DP2": 0.42709682917588638},
}


def fresh_template():
    return load_workbook(C.TEMPLATE_PATH)


def test_correction_table_is_exactly_the_approved_change():
    assert {c[1]: (c[2], c[3]) for c in TC.CORRECTIONS} == APPROVED
    assert [c[0] for c in TC.CORRECTIONS] == ["D-1"] * 4 + ["D-1c"] * 2 + ["D-1d"]
    assert TC.KNOWN_UNRESOLVED == {}


def test_falconbridge_template_is_unmodified_and_holds_the_original_formulas():
    assert hashlib.sha256(C.TEMPLATE_PATH.read_bytes()).hexdigest() == TEMPLATE_SHA256
    ws = load_workbook(C.TEMPLATE_PATH, read_only=True)[SH]
    cells = FA.cells_of(ws)
    for cell, (original, _) in APPROVED.items():
        col, row = re.match(r"([A-Z]+)(\d+)", cell).groups()
        assert ws[cell].value == original, cell
    assert FA.misaligned_cells(cells) == TEMPLATE_FINDINGS


def test_apply_changes_exactly_the_approved_cells_and_records_them():
    wb = fresh_template()
    before = FA.cells_of(wb[SH])
    rows, result = TC.apply(wb, log=lambda *_: None)
    after = FA.cells_of(wb[SH])
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    expected = {(int(re.sub("[A-Z]", "", c)), sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(re.sub(r"\d", "", c)))))
                for c in APPROVED}
    assert changed == expected
    for cell, (original, replacement) in APPROVED.items():
        assert wb[SH][cell].value == replacement
        assert any(cell in k and shown(original) in v and replacement in v for k, v in rows), cell
    assert result["findings"] == []
    assert not any(k.startswith("Known template issue") for k, _ in rows)
    assert any(k.startswith("Year-alignment check") and v.startswith("PASS") for k, v in rows)
    assert "D-1d: FA2" in dict(rows)["Template corrections (FalconBridge template file unchanged)"]


def test_guards_stop_generation_on_an_unexpected_template():
    wb = fresh_template()
    ws = wb[SH]
    ws["DL2"] = "=IFERROR(BY2/AK2,0)"                      # already "corrected" upstream: must be reviewed, not re-applied
    with pytest.raises(TC.TemplateCorrectionError, match="DL2"):
        TC.apply(wb, log=lambda *_: None)
    ws["DL2"] = APPROVED["DL2"][0]
    ws["MW2"] = '=IFERROR(IE2/GQ2,0)'                      # any other formula text
    with pytest.raises(TC.TemplateCorrectionError, match="MW2"):
        TC.apply(wb, log=lambda *_: None)
    ws["MW2"] = APPROVED["MW2"][0]
    ws["BY3"] = 2024                                       # a year header the corrections depend on
    with pytest.raises(TC.TemplateCorrectionError, match="BY3"):
        TC.apply(wb, log=lambda *_: None)
    ws["BY3"] = 2025
    ws["FA2"] = "=SUM(FA4:FA1331)"                         # D-1d: FA2 must be blank in the template
    with pytest.raises(TC.TemplateCorrectionError, match="expected a blank cell"):
        TC.apply(wb, log=lambda *_: None)
    ws["FA2"] = None
    ws["EZ2"] = "=SUM(EZ4:EZ1330)"                         # D-1d: the neighbouring Grand Total pattern must be intact
    with pytest.raises(TC.TemplateCorrectionError, match="EZ2"):
        TC.apply(wb, log=lambda *_: None)
    ws["EZ2"] = "=SUM(EZ4:EZ1331)"
    ws["FA3"] = 2024                                       # D-1d: year header
    with pytest.raises(TC.TemplateCorrectionError, match="FA3"):
        TC.apply(wb, log=lambda *_: None)
    ws["FA3"] = 2025
    assert ws["DL2"].value == APPROVED["DL2"][0] and ws["FA2"].value is None   # guards ran before any write
    ws["DK2"] = "=IFERROR(BY2/AJ2,0)"                      # a misalignment elsewhere: the post-check stops generation
    with pytest.raises(TC.TemplateCorrectionError, match="DK2"):    # (raised before the workbook is saved)
        TC.apply(wb, log=lambda *_: None)


def test_scanner_negative_control_on_falconbridge_originals():
    for path in (C.TEMPLATE_PATH, US_METRO_ORIGINAL):
        if path.exists():
            assert FA.misaligned_cells(FA.cells_of(load_workbook(path, read_only=True)[SH])) == TEMPLATE_FINDINGS


def test_scanner_detects_each_kind_of_misalignment():
    base = FA.cells_of(load_workbook(C.TEMPLATE_PATH, read_only=True)[SH])
    for cell, (_, replacement) in APPROVED.items():
        col, row = re.match(r"([A-Z]+)(\d+)", cell).groups()
        base[(int(row), FA.CI(col))] = replacement
    assert FA.misaligned_cells(base) == []
    cases = {
        "DK5": "=IFERROR(BY5/AJ5,\"\")",          # industry-row numerator one year late
        "LF7": "=IFERROR(GQ7/AK7,\"\")",          # liquidation rate numerator one year late
        "MU9": "=IFERROR(IB9/GN9,\"\")",          # severity denominator one year early
        "DL2": "=IFERROR(CA2/AK2,0)",             # reference to an empty spacer column
        "DO2": APPROVED["DO2"][0],                 # Grand Total range stopping early
        "C2": "=SUM(D4:D1330)",                    # Grand Total summing another column
    }
    for cell, formula in cases.items():
        cells = dict(base)
        col, row = re.match(r"([A-Z]+)(\d+)", cell).groups()
        cells[(int(row), FA.CI(col))] = formula
        assert cell in FA.misaligned_cells(cells), cell
    hole = dict(base)
    del hole[(2, FA.CI("EZ"))]                     # a missing Grand Total between filled ones
    assert "EZ2" in FA.misaligned_cells(hole)


@pytest.mark.skipif(not RECALC.exists(), reason="recalculated generated workbooks absent")
def test_corrected_formulas_give_the_independently_validated_values():
    """Evaluate each corrected formula on the bank's own Grand Total components (LibreOffice-recalculated generated
    workbooks; these components are not changed by the corrections) and compare with the independent values."""
    for stem, exp in EXPECTED.items():
        f = RECALC / f"{stem}_20260630.xlsx"
        if not f.exists():
            continue
        v = FA.cells_of(load_workbook(f, read_only=True, data_only=True)[SH])
        g = lambda a: v.get((int(re.sub("[A-Z]", "", a)), FA.CI(re.sub(r"\d", "", a)))) or 0
        for cell in ("DL2", "DM2", "MV2", "MW2"):
            num, den = re.match(r"=IFERROR\(([A-Z]+2)/([A-Z]+2),", APPROVED[cell][1]).groups()
            got = g(num) / g(den) if g(den) else ""
            assert got == pytest.approx(exp[cell], abs=1e-12) if exp[cell] != "" else got == "", (stem, cell)
        rng = lambda a, b: sum(g(f"{FA.L(c)}2") for c in range(FA.CI(a), FA.CI(b) + 1))
        assert rng("AP", "BZ") == exp["pif_all"] and rng("BF", "BZ") == exp["pif_2006"], stem
        assert exp["pif_all"] / exp["orig_le2026"] == pytest.approx(exp["DO2"], abs=1e-12)
        assert exp["pif_2006"] / exp["orig_2006"] == pytest.approx(exp["DP2"], abs=1e-12)
        # D-1d: =SUM(FA4:FA1331) over the industry rows equals the FOIA count of 2025 payoffs, and with it the yearly
        # Grand Totals reconcile to FD2 ("Total") once the intentionally unshown 1990-2005 counts are added back
        rows = [r for r in range(4, 1332) if isinstance(v.get((r, 2)), str)]
        fa_sum = sum(v.get((r, FA.CI("FA"))) or 0 for r in rows)
        assert fa_sum == PIF_2025[stem], stem
        shown_years = sum(g(f"{FA.L(c)}2") for c in range(FA.CI("DR"), FA.CI("FB") + 1) if c != FA.CI("FA"))
        before_2006 = sum(v.get((r, c)) or 0 for r in rows for c in range(FA.CI("DR"), FA.CI("EG") + 1))
        assert before_2006 == PIF_BEFORE_2006[stem], stem
        assert g("FD2") == shown_years + fa_sum + before_2006, stem


@pytest.mark.skipif(not RELEASE.exists(), reason="2026-06-30 release folder absent")
def test_generated_workbook_carries_the_corrections_and_the_record(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / "bank_workbook_generator" / "generate_bank_workbook.py"),
                          "--as-of", "2026-06-30", "--fdic-cert", "27417", "--output-dir", str(tmp_path)],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
    assert out.returncode == 0, out.stdout + out.stderr
    wb = load_workbook(next(tmp_path.glob("Shoreham_Bank_RI_*.xlsx")), read_only=True)
    ws = wb[SH]
    for cell, (_, replacement) in APPROVED.items():
        assert ws[cell].value == replacement, cell
    assert FA.misaligned_cells(FA.cells_of(ws)) == []
    info = {r[0]: r[1] for r in wb["Generation_Info"].iter_rows(min_row=3, values_only=True) if r[0]}
    assert info["Generator version"].startswith("stage1-1.5")
    for cell, (original, replacement) in APPROVED.items():
        key = next(k for k in info if k.endswith(f"!{cell}") and k.startswith("Template correction"))
        assert shown(original) in info[key] and replacement in info[key]
    assert not any(k.startswith("Known template issue") for k in info)
    assert hashlib.sha256(C.TEMPLATE_PATH.read_bytes()).hexdigest() == TEMPLATE_SHA256
