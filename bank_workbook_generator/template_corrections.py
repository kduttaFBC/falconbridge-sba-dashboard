"""Approved corrections to formula defects in FalconBridge's template, applied to the GENERATED COPY only.

FalconBridge's template file (config.TEMPLATE_PATH) is never modified. Each correction is guarded: the template cell
must hold exactly the known original formula and the year headers its references depend on must be unchanged;
otherwise generation stops (TemplateCorrectionError) so that a changed template is reviewed instead of being altered
silently. After the corrections, the year-alignment scanner must find no misaligned formula in the sheet.

D-1  (approved 2026-11-05): four Grand Total cells of ChgOffs_byNAICs&ChgOffYr referenced the wrong year.
D-1c (verified and included 2026-11-05): two Grand Total cumulative paid-in-full ratios summed paid-in-full amounts
     only to 2024 while their denominators and the industry rows run to 2026.
D-1d (approved 2026-11-05): the 2025 Grand Total paid-in-full count (FA2) is blank although the template's own shared
     formula fill EH2:FB2 covers it; FalconBridge_FA2_Formula_Investigation_2026-11-05.
Evidence: FalconBridge_D1_Formula_Correction_Validation_2026-11-05 (industry rows, independent recomputation from the
FOIA source data for three banks, FalconBridge's Orig_v2 severity definition). No methodology is changed.
"""
import formula_alignment as FA

SHEET = FA.SHEET
APPROVED = "2026-11-05"

# (id, cell, original template formula, replacement, meaning, {header cell: expected year})
CORRECTIONS = (
    ("D-1", "DL2", "=IFERROR(BZ2/AK2,0)", "=IFERROR(BY2/AK2,0)",
     "Grand Total estimated prepayment rate 2025 = paid-in-full original amount 2025 (BY2) / balance at year-end "
     "2024 (AK2); the original divided 2026 paid-in-full (BZ2) by the 2024 balance",
     {"DL3": 2025, "BY3": 2025, "BZ3": 2026, "AK3": 2024}),
    ("D-1", "DM2", "=IFERROR(CA2/AL2,0)", "=IFERROR(BZ2/AL2,0)",
     "Grand Total estimated prepayment rate 2026 (to the as-of date) = paid-in-full 2026 (BZ2) / balance at "
     "year-end 2025 (AL2); the original referenced the empty column CA",
     {"DM3": 2026, "BZ3": 2026, "AL3": 2025}),
    ("D-1", "MV2", '=IFERROR(ID2/GP2,"")', '=IFERROR(IC2/GP2,"")',
     "Grand Total loss severity 2025 = charge-off amount 2025 (IC2) / original amount of loans charged off in 2025 "
     "(GP2); the original divided 2026 charge-offs (ID2) by 2025 defaults",
     {"MV3": 2025, "IC3": 2025, "ID3": 2026, "GP3": 2025}),
    ("D-1", "MW2", '=IFERROR(IE2/GQ2,"")', '=IFERROR(ID2/GQ2,"")',
     "Grand Total loss severity 2026 = charge-off amount 2026 (ID2) / original amount of loans charged off in 2026 "
     "(GQ2); the original referenced the empty column IE",
     {"MW3": 2026, "ID3": 2026, "GQ3": 2026}),
    ("D-1c", "DO2",
     '=SUM(AP2:BX2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,"<="&$DM$3,FOIA!$AS:$AS,$A$1)',
     '=SUM(AP2:BZ2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,"<="&$DM$3,FOIA!$AS:$AS,$A$1)',
     "Grand Total cumulative paid-in-full share ('Total') = paid-in-full 1990-2026 (AP2:BZ2) / originations to "
     "2026; the original summed paid-in-full only to 2024 (AP2:BX2)",
     {"AP3": 1990, "BX3": 2024, "BZ3": 2026, "DM3": 2026}),
    ("D-1c", "DP2",
     '=SUM(BF2:BX2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,">="&2006,FOIA!$AO:$AO,"<="&$DM$3,'
     'FOIA!$AS:$AS,$A$1)',
     '=SUM(BF2:BZ2)/SUMIFS(FOIA!$O:$O,FOIA!$AR:$AR,"Yes",FOIA!$AO:$AO,">="&2006,FOIA!$AO:$AO,"<="&$DM$3,'
     'FOIA!$AS:$AS,$A$1)',
     "Grand Total cumulative paid-in-full share ('2006-2026') = paid-in-full 2006-2026 (BF2:BZ2) / originations "
     "2006-2026; the original summed paid-in-full only to 2024 (BF2:BX2)",
     {"BF3": 2006, "BX3": 2024, "BZ3": 2026, "DM3": 2026}),
    ("D-1d", "FA2", None, "=SUM(FA4:FA1331)",
     "Grand Total count of loans paid in full in 2025 = sum of the industry rows' 2025 counts (FA4:FA1331), the "
     "formula the template's shared fill EH2:FB2 assigns to this cell and uses for 2006-2024 and 2026; the template "
     "cell was blank although the sheet's 'Total' (FD2) already includes the 2025 counts",
     {"DQ2": "Grand Total", "FA3": 2025, "EZ3": 2024, "FB3": 2026,
      "EZ2": "=SUM(EZ4:EZ1331)", "FB2": "=SUM(FB4:FB1331)"}),
)


# Template issues found by the year-alignment check that are NOT corrected (no approval). They are recorded in the
# generation record; any other finding stops generation. Empty since D-1d (FA2) was approved, corrected and validated
# on 2026-11-05 (FA2 = 54 / 45 / 27 for US Metro / FinWise / Shoreham, equal to the FOIA source data).
KNOWN_UNRESOLVED = {}


class TemplateCorrectionError(RuntimeError):
    pass


def _year(v):
    return int(v) if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()) else v


def apply(wb, log=print):
    """Apply every correction to the generated copy `wb` (guarded). Returns (generation-record rows, scan result)."""
    ws = wb[SHEET]
    for cid, cell, original, _, _, context in CORRECTIONS:
        for h, expected in context.items():          # year headers (int) and exact neighbouring labels / formulas
            found = _year(ws[h].value) if isinstance(expected, int) else ws[h].value
            if found != expected:
                raise TemplateCorrectionError(
                    f"Template changed: {SHEET}!{h} is {ws[h].value!r}, expected {expected!r} (correction {cid} "
                    f"{cell}). Generation stopped; the template corrections must be re-reviewed.")
        found = ws[cell].value
        if found != original:
            raise TemplateCorrectionError(
                f"Template changed: {SHEET}!{cell} is {found!r}, expected "
                + ("a blank cell" if original is None else f"the known original formula {original!r}")
                + f" (correction {cid}). Generation stopped; the template corrections must be re-reviewed.")
    for _, cell, _, replacement, _, _ in CORRECTIONS:
        ws[cell] = replacement
    result = FA.scan(FA.cells_of(ws))
    unexpected = [x for x in result["findings"] if x["cell"] not in KNOWN_UNRESOLVED]
    if unexpected:
        raise TemplateCorrectionError(
            f"{SHEET}: year-alignment check failed after the template corrections: "
            + "; ".join(f"{x['cell']} ({x['rule']}): {x['formula']}" for x in unexpected))
    known = sorted({x["cell"] for x in result["findings"]})
    log(f"Template corrections applied to the generated copy: {', '.join(c[1] for c in CORRECTIONS)}; "
        f"year-alignment check PASS ({result['rate_cells_checked']} rate and {result['pattern_cells_checked']} "
        "Grand Total formulas)")
    groups = {}
    for cid, cell, *_ in CORRECTIONS:
        groups.setdefault(cid, []).append(cell)
    rows = [("Template corrections (FalconBridge template file unchanged)",
             f"{len(CORRECTIONS)} formulas in {SHEET} corrected in this generated copy only ("
             + "; ".join(f"{cid}: {', '.join(cells)}" for cid, cells in groups.items()) + f"). Approved {APPROVED}. "
             "Each correction makes a Grand Total formula refer to the year stated in its column header, or restores "
             "the template's own formula where it was missing; no methodology is changed.")]
    for cid, cell, original, replacement, meaning, _ in CORRECTIONS:
        rows.append((f"Template correction {cid}: {SHEET}!{cell}",
                     f"{meaning}. Original: {original if original is not None else '(blank cell - no formula)'}"
                     f"   Replacement: {replacement}"))
    rows.append((f"Year-alignment check: {SHEET}",
                 f"PASS - {result['rate_cells_checked']} rate formulas and {result['pattern_cells_checked']} Grand "
                 "Total formulas checked against the year headers; 0 misaligned. Not checked by design: the 1990 "
                 "first-year columns (no prior year) and the Grand Totals the template starts at 2006 "
                 f"({len(result['leading_blank_totals'])} blank cells before 2006)."
                 + (f" Known template issue not corrected: {', '.join(known)}." if known else "")))
    for cell in known:
        cid, text = KNOWN_UNRESOLVED[cell]
        rows.append((f"Known template issue {cid} (not corrected): {SHEET}!{cell}", text))
    return rows, result
