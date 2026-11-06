"""Bank Workbook Generator - Stage 1 (local).

Select the SBA FOIA release (reporting period) and the bank -> confirm the lender's identity -> extract the bank's
eligible FOIA records from the four processed historical files (fix_foia.py output) -> populate a clean copy of
FalconBridge's existing Excel template. Excel's existing formulas perform the Performance Summary calculations when
the file is opened.

Usage:
  python3 generate_bank_workbook.py                                   # interactive: period, search, selection
  python3 generate_bank_workbook.py --as-of 2026-06-30 --fdic-cert 35323
  python3 generate_bank_workbook.py --as-of 2026-06-30 --bank-name "FinWise Bank" --bank-state UT

Different lenders are never combined: if a Bank Name + Bank State holds records of more than one lender, the
generator stops and lists the evidence (FDIC certificate, SBA LocationID, city) so the analyst can choose.
"""
import argparse
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark as B                    # noqa: E402
import config as C                       # noqa: E402
import source_data as S                  # noqa: E402
import threshold_analysis as TA          # noqa: E402
from foia_xml_writer import write_foia_sheet          # noqa: E402
from template_populator import build_template_copy   # noqa: E402
from validation import benchmark_checks, build_checks  # noqa: E402

VERSION = ("stage1-1.5 (configurable original loan balance threshold; preliminary industry-matched benchmark; "
           "template corrections D-1/D-1c/D-1d in ChgOffs_byNAICs&ChgOffYr)")
RESOLUTION = {
    "UNIQUE": "Unique - one lender under this BankName and BankState",
    "RESTRICTED_TO_FDIC_CERT": "Restricted to the selected FDIC certificate; other lenders with the same BankName and "
                               "BankState are excluded",
    "RESTRICTED_TO_LOCATION_IDS": "Restricted to the SBA LocationIDs selected by the analyst",
}


# ---------------------------------------------------------------- reporting period
def _folder_date(name):
    return datetime.strptime(name, "%Y%m%d").date() if re.fullmatch(r"\d{8}", name) else None


def choose_period_interactively():
    snaps = S.snapshots()
    if not snaps:
        raise SystemExit(f"No FOIA release folders found in {C.SOURCE_DIR}. Put the fix_foia.py output for a release in "
                         f"source_data/<YYYYMMDD>, e.g. source_data/20260630.")
    names = list(snaps)
    if len(names) == 1:
        print(f"Reporting period (SBA FOIA release): {names[0]}")
        return snaps[names[0]], _folder_date(names[0])
    print("\nAvailable SBA FOIA releases:")
    for i, n in enumerate(names, start=1):
        print(f"{i:>3}  {n}")
    while True:
        pick = input("Enter the number of the reporting period: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(names):
            n = names[int(pick) - 1]
            return snaps[n], _folder_date(n)


# ---------------------------------------------------------------- bank selection
def _print_lenders(lenders):
    for x in lenders:
        print(f"      FDIC cert {x['cert'] or '(none)':>8}  SBA LocationID {x['location_id']:>8}  {x['city'][:24]:24} "
              f"{x['eligible']:>8,} eligible loans")


def _resolve_interactively(directory, name, state, entry, cert):
    locs = None
    while True:
        try:
            return S.resolve_lender(directory, cert=cert, bank_name=name, bank_state=state, location_ids=locs)
        except S.LenderSelectionError as e:
            print(f"\n{e}")
            if len(entry["certs"]) > 1 and (cert is None or S.norm_id(cert) not in entry["certs"]):
                cert = input("Enter the FDIC certificate of the bank you want (Enter to cancel): ").strip() or None
                if cert is None:
                    return None
            else:
                ids = input("Enter the SBA LocationIDs to include, separated by commas (Enter to cancel): ").strip()
                if not ids:
                    return None
                locs = [x.strip() for x in ids.split(",") if x.strip()]


def choose_bank_interactively(directory):
    while True:
        q = input("\nType part of the bank name, or its FDIC certificate number ('q' to quit): ").strip()
        if q.lower() == "q":
            sys.exit(0)
        if len(q) < 2:
            print("Please type at least 2 characters.")
            continue
        if q.isdigit():
            hits = [(k, v) for k, v in directory.items() if S.norm_id(q) in v["certs"] and v["eligible"] > 0]
        else:
            hits = [(k, v) for k, v in directory.items() if q.lower() in k[0].lower() and v["eligible"] > 0]
        hits.sort(key=lambda kv: -kv[1]["eligible"])
        if not hits:
            print("No bank found with eligible loans for that text. Try a shorter or different spelling.")
            continue
        print(f"\n{'#':>3}  {'Bank Name':50} {'State':5} {'Eligible loans':>14}  {'FDIC cert(s)':18} Lender IDs")
        for i, ((name, state), v) in enumerate(hits[:40], start=1):
            certs = ", ".join(c or "(none)" for c in v["certs"])
            flag = "  <- more than one lender" if len(v["certs"]) > 1 else ""
            print(f"{i:>3}  {name[:50]:50} {state:5} {v['eligible']:>14,}  {certs[:18]:18} {v['location_ids']:>3}{flag}")
        if len(hits) > 40:
            print(f"     ... {len(hits) - 40} more; type a longer name to narrow the list.")
        pick = input("\nEnter the number of the bank (or press Enter to search again): ").strip()
        if not (pick.isdigit() and 1 <= int(pick) <= min(40, len(hits))):
            continue
        (name, state), v = hits[int(pick) - 1]
        sel = _resolve_interactively(directory, name, state, v, S.norm_id(q) if q.isdigit() else None)
        if sel is None:
            continue
        print(f"\nSelected lender: {sel['bank_name']}, {sel['bank_state']} - FDIC cert {sel['cert'] or '(none)'}")
        _print_lenders(sel["selected"])
        if sel["excluded"]:
            print("   Not included (other lenders with the same Bank Name and Bank State):")
            _print_lenders(sel["excluded"])
        if input("Generate the workbook for this lender? [y/n]: ").strip().lower().startswith("y"):
            return sel


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:80]


# ---------------------------------------------------------------- generation
def _event_years(rows):
    years = set()
    for r in rows:
        for field in (C.F_FIRST_DISBURSEMENT, "PaidInFullDate", "ChargeOffDate"):
            if r.get(field, "").strip():
                years.add(S.parse_date(r[field]).year)
    return years


def benchmark_guards(tabs, sector_labels):
    """Loan and defaulted counts behind each benchmark ratio, for the empty-group safeguard (template_populator)."""
    years = range(C.BENCHMARK_FIRST_VINTAGE, C.TEMPLATE_HIDDEN_TAB_LAST_YEAR + 1)
    out = {}
    for (sfx, _), base in zip(B.GROUPS, ("X", "AJ", "AV", "BH", "BT")):
        vint, sec = tabs[sfx]["vintage"], tabs[sfx]["sector"]
        v = {y: (vint.get(y, [0] * 10)[0], vint.get(y, [0] * 10)[2]) for y in years}
        out[base] = {"vintage": v, "sector": {s: sec[s][0] for s in sector_labels},
                     "row6": (sum(sec[s][0] for s in sector_labels), sum(sec[s][2] for s in sector_labels)),
                     "r20": (sum(a for a, _ in v.values()), sum(b for _, b in v.values())),
                     "r57": sum(v[y][0] for y in range(2019, 2027))}
    return out


def generate(selection, folder, as_of_expected=None, output_dir=None, overwrite=False, log=print, inputs="interactive",
             threshold=None):
    t0 = time.time()
    threshold = C.BENCHMARK_STANDARD_MIN if threshold is None else int(threshold)
    if threshold < 0:
        raise SystemExit("The original loan balance threshold must be a whole-dollar amount of 0 or more.")
    default_t = threshold == C.BENCHMARK_STANDARD_MIN
    bank_name, bank_state = selection["bank_name"], selection["bank_state"]
    files = S.source_files(folder)
    log(f"Extracting '{bank_name}, {bank_state}' (FDIC cert {selection['cert'] or 'none'}, "
        f"LocationID {', '.join(selection['location_ids'])}) from the historical files...")
    ext = S.extract_bank(bank_name, bank_state, folder, cert=selection["cert"] or None,
                         location_ids=selection["location_ids"])
    rows = ext["rows"]
    if not rows:
        raise SystemExit(f"No eligible loans for '{bank_name}, {bank_state}'.")
    lists = S.build_lists(rows)
    as_of = S.snapshot_as_of(lists["as_of_values"], as_of_expected)
    for name, *_ in lists["sectors"] + lists["subprograms"]:
        if name.startswith("="):
            raise SystemExit(f"A sector or subprogram name starts with '=' ({name!r}); it would be read as a formula.")
    years = _event_years(rows)
    late = sorted(y for y in years if y > C.TEMPLATE_HIDDEN_TAB_LAST_YEAR)
    if late:
        raise SystemExit(f"This release has disbursement, paid-in-full or charge-off years {late} after "
                         f"{C.TEMPLATE_HIDDEN_TAB_LAST_YEAR}, the last year built into the template's hidden tabs "
                         f"(ChgOffs, OrigAmt, CumPrepayments, Status_by_Vintage). Those tabs must be extended "
                         f"before this period can be reported; nothing was generated.")
    early = sorted(y for y in years if y < C.TEMPLATE_HIDDEN_TAB_FIRST_YEAR)
    excluded = ext["excluded"]
    na = lists["na"]
    log(f"  records {ext['total_records']:,}; eligible {len(rows):,}; excluded {excluded or 'none'}")
    log(f"  sectors {len(lists['sectors'])} named + {'N/A' if na else 'no N/A'}; "
        f"subprograms {len(lists['subprograms'])}; vintages {lists['vintages'][0]}-{lists['vintages'][-1]}")

    output_dir = Path(output_dir or C.OUTPUT_DIR / f"{as_of:%Y%m%d}")
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = {"UNIQUE": "", "RESTRICTED_TO_FDIC_CERT": f"_FDIC{selection['cert']}",
              "RESTRICTED_TO_LOCATION_IDS": "_LocID" + "-".join(selection["location_ids"])}[selection["status"]]
    tsfx = "" if default_t else f"_GT{threshold}"
    out = output_dir / f"{safe_filename(bank_name)}_{bank_state}{suffix}_{as_of:%Y%m%d}{tsfx}.xlsx"
    if out.exists() and not overwrite:
        raise SystemExit(f"{out} already exists. Move it, or re-run with --overwrite to replace it.")

    log("Industry-matched SBA benchmark (PRELIMINARY)...")
    bdata = B.aggregate(folder, progress=log, threshold=threshold)
    S.snapshot_as_of(bdata["as_of_values"], as_of)          # every benchmark loan carries the selected AsOfDate
    sector_labels = [s[0] for s in lists["sectors"]] + ([C.NA_SECTOR] if na else [])
    tabs = B.tables(bdata, sector_labels)
    btot = B.totals(bdata)
    q = (as_of.month - 1) // 3 + 1
    bench = {
        "period_label": f"FY{C.BENCHMARK_FIRST_FY} - Q{q} {as_of.year} Originations & Performance "
                        f"(as of {as_of.month}/{as_of.year})",
        "banner": "PRELIMINARY industry-matched SBA 7(a) benchmark: vintage rows and row 6 = SBA loans in this lender's "
                  "industries; sector rows = all SBA loans in each industry. Subject to FalconBridge's methodology "
                  "confirmation - see Benchmark_Notes.",
        "totals": btot,
        "write_sheets": lambda wb: B.write_sheets(wb, tabs, as_of, f"{bank_name}, {bank_state}"),
        "notes": lambda flags: B.notes_rows(bdata, as_of, folder, sector_labels, flags, tabs),
        "group_titles": {"AJ": f"All SBA 7(a) - Standard (Bal > {B.threshold_label(threshold)})",
                         "AV": f"All SBA 7(a)  - Small (Bal <= {B.threshold_label(threshold)})"},
        "guards": benchmark_guards(tabs, sector_labels),
        "extra_sheets": lambda wb, ps, sm: TA.write_sheet(wb, ps, sm, rows, as_of, threshold, sector_labels),
    }
    log(f"  benchmark population {btot[0]:,} loans; lender's industries {len(sector_labels)}")

    statuses = sorted({r[C.F_LOAN_STATUS].strip() for r in rows})
    city = ", ".join(sorted({x["city"] for x in selection["selected"]}))
    street = "; ".join(sorted({x["street"] for x in selection["selected"]}))
    info = [
        ("Generated (local time)", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Generator version", VERSION),
        ("Reporting period: SBA FOIA AsOfDate", as_of.strftime("%m/%d/%Y")),
        ("FOIA release folder", str(Path(folder).resolve())),
        ("Note on dates", "The SBA FOIA AsOfDate is independent of any FFIEC Call Report date; the two are not assumed equal."),
        ("Selection entered by analyst", inputs),
        ("Selection: BankName", bank_name),
        ("Selection: BankState", bank_state),
        ("Lender identity: FDIC certificate (BankFDICNumber)", selection["cert"] or "none published"),
        ("Underlying SBA lender IDs (LocationID) combined", f"{len(ext['location_ids'])}: {', '.join(ext['location_ids'][:50])}"
         + (" ..." if len(ext["location_ids"]) > 50 else "")),
        ("Lender identity: BankCity / BankStreet in FOIA", f"{city} / {street}"),
        ("Lender identity: resolution", RESOLUTION[selection["status"]]),
        ("Other lenders under the same BankName + BankState (excluded)",
         "; ".join(f"FDIC {x['cert'] or 'none'} / LocationID {x['location_id']} ({x['city']}, {x['records']:,} records)"
                   for x in selection["excluded"]) or "none"),
        ("Records excluded because they belong to another lender", ext["other_lender_records"]),
        ("Source AsOfDate", as_of.strftime("%m/%d/%Y")),
        ("Records for selection (all statuses)", ext["total_records"]),
        ("Excluded: blank LoanStatus", excluded.get("blank LoanStatus", 0)),
        ("Excluded: LoanStatus = CANCLD", excluded.get("LoanStatus = CANCLD", 0)),
        ("Excluded: blank FirstDisbursementDate", excluded.get("blank FirstDisbursementDate", 0)),
        ("Eligible loans written to FOIA tab", len(rows)),
        ("Eligible GrossApproval (sum, ORIGINAL approval amounts)", lists["eligible_balance"]),
        ("Named sectors (NaicsDescription)", len(lists["sectors"])),
        ("N/A sector (blank NAICS): loans", na[1] if na else 0),
        ("N/A sector (blank NAICS): GrossApproval", na[2] if na else 0),
        ("Subprograms", len(lists["subprograms"])),
        ("Vintage years present (FirstDisbursementDate)", f"{lists['vintages'][0]}-{lists['vintages'][-1]}"),
        ("LoanStatus values among eligible loans", ", ".join(statuses)),
        ("Sector order", "Sum of GrossApproval descending; ties alphabetical; N/A last"),
        ("Population rule", "Exclude blank LoanStatus, LoanStatus = CANCLD, blank FirstDisbursementDate"),
        ("Amounts", "All dollar amounts are ORIGINAL approval amounts (GrossApproval, SBAGuaranteedApproval) or gross "
                    "charge-offs from SBA FOIA. 'Est. Orig. Bal. of loans Outstanding' is the original amount of loans "
                    "still active - not a current balance."),
        ("NOT AVAILABLE: securitization block (PerformanceSummary C:J)",
         "Requires the lender's loan tape (current unguaranteed balances, WALA, credit and SBSS scores); not in FOIA."),
        ("Benchmark (PerformanceSummary X:CE): status", "PRELIMINARY industry-matched SBA 7(a) benchmark, "
         "FalconBridge's SBA Orig_v2 method; subject to FalconBridge's methodology confirmation. See Benchmark_Notes."),
        ("Benchmark: source", f"Public SBA 7(a) FOIA, AsOfDate {as_of:%m/%d/%Y} (same release folder), ApprovalFiscalYear "
         f">= {C.BENCHMARK_FIRST_FY}, LoanStatus not CANCLD, FirstDisbursementDate present; files read as published "
         "(FalconBridge's source-import errors are not reproduced)"),
        ("Benchmark: population (loans / original GrossApproval)", f"{btot[0]:,} / ${btot[1]:,.0f}"),
        ("Benchmark: lender's industries used for vintage rows and row 6", len(sector_labels)),
        ("Original loan balance threshold", f"${threshold:,}" + (" (default)" if default_t else
         f" (selected; default ${C.BENCHMARK_STANDARD_MIN:,})") + ": Standard / above = GrossApproval rounded to whole "
         "dollars greater than the threshold; Small / at or below = the rest. Same rule for the bank and the benchmark."),
        ("Threshold_Analysis sheet", "The bank's loans above / at or below the threshold (full history and FY2000 "
         "onward), with the PerformanceSummary K:W definitions and its own PASS/FAIL checks."),
        ("Benchmark: data sheets", "Hidden Combined_Vintage[_Standard|_Small|_Non-CRE|_CRE] and Combined_Sector[...] "
         "(layout of Orig_v2); PerformanceSummary X:CE keeps the template formulas, re-pointed from the external "
         "workbook to these sheets"),
        ("Benchmark: generator module", "bank_workbook_generator/benchmark.py"),
        ("LIQUID (%) and PURCH(NOT C/O) (%)", "Computed by the template from LoanStatus. " + (
            "This FOIA release publishes no LIQUID or PURCH(NOT C/O) statuses for these loans, so both columns show "
            "0% by construction - this is not evidence that no loans are in liquidation or purchased."
            if not {"LIQUID", "PURCH(NOT C/O)"} & set(statuses) else "These statuses are present in this release.")),
        ("Overview (hidden sheet)", "Kept as in the FinWise reference: fixed 2023-2025 quarters; its unguaranteed "
                                    "columns (FOIA!BA:BC) are not defined in the reference and show 0."),
        ("Years before the template's hidden-tab range (1991)",
         f"{early} - included in PerformanceSummary but not in the hidden tabs" if early else "none"),
        ("Removed FinWise-specific content", "Securitization block, FinWise's benchmark values (replaced by this "
                                             "release's benchmark), Sheet1 loan tape, "
                                             "ChargeOffsByVintage_Sec, Lookup O:P, external links, FinWise footnotes"),
        ("Validation", "See the Validation sheet: each key cell against a recomputation from the FOIA rows."),
        ("Template", str(C.TEMPLATE_PATH.name)),
        ("Template SHA-256", S.sha256(C.TEMPLATE_PATH)),
    ]
    log_file = Path(folder) / "correction_log.txt"
    if log_file.exists():
        info.append(("fix_foia.py correction log in release folder (SHA-256)", S.sha256(log_file)))
    for f in files:
        info.append((f"Source file SHA-256: {f.name}", S.sha256(f)))
    log("Preparing template copy (phase 1)...")
    phase = build_template_copy(out, lists, as_of, info, len(rows), log=log,
                                validation_checks=lambda ps, sm: build_checks(rows, as_of, ps, sm)
                                + benchmark_checks(ps, tabs, btot), bench=bench)
    log("Writing FOIA rows (phase 2)...")
    stats = write_foia_sheet(out, rows, phase["helper_formulas"], as_of, log=log)
    if any(stats.values()):
        log(f"  notes: {stats}")
    log(f"Done in {time.time() - t0:.0f}s -> {out}")
    log("Open the file in Microsoft Excel; Excel calculates the Performance Summary and the Validation sheet on opening.")
    return out, {"extract": ext, "lists": lists, "phase": phase, "stats": stats, "as_of": as_of}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--as-of", help="reporting period = SBA FOIA AsOfDate, YYYY-MM-DD (uses source_data/YYYYMMDD)")
    ap.add_argument("--foia-dir", type=Path, help="folder with the fix_foia.py output for one release (instead of --as-of)")
    ap.add_argument("--fdic-cert", help="FDIC certificate of the bank (BankFDICNumber)")
    ap.add_argument("--bank-name")
    ap.add_argument("--bank-state")
    ap.add_argument("--location-id", action="append", help="SBA LocationID to include (repeat for several)")
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--overwrite", action="store_true", help="replace an existing workbook of the same name")
    ap.add_argument("--threshold", type=int, help=f"original loan balance threshold in whole dollars (default "
                                                   f"{C.BENCHMARK_STANDARD_MIN})")
    a = ap.parse_args()

    expected = date.fromisoformat(a.as_of) if a.as_of else None
    if a.foia_dir:
        folder = a.foia_dir
    elif expected:
        folder = C.SOURCE_DIR / f"{expected:%Y%m%d}"
        if not folder.is_dir():
            raise SystemExit(f"No FOIA release folder {folder}. Available: {', '.join(S.snapshots()) or 'none'}")
    elif a.fdic_cert or a.bank_name or a.bank_state:
        raise SystemExit("Give the reporting period with --as-of YYYY-MM-DD (or --foia-dir).")
    else:
        folder, expected = choose_period_interactively()
    try:
        directory, as_of_values = S.lender_directory(folder)
        S.snapshot_as_of(as_of_values, expected)
        if a.fdic_cert or a.bank_name or a.bank_state:
            sel = S.resolve_lender(directory, cert=a.fdic_cert, bank_name=a.bank_name, bank_state=a.bank_state,
                                   location_ids=a.location_id)
            inputs = "; ".join(f"{k}={v}" for k, v in (
                ("as-of", a.as_of), ("foia-dir", a.foia_dir), ("fdic-cert", a.fdic_cert), ("bank-name", a.bank_name),
                ("bank-state", a.bank_state), ("location-id", ",".join(a.location_id or [])),
                ("threshold", a.threshold)) if v)
        else:
            sel, inputs = choose_bank_interactively(directory), "interactive"
        generate(sel, folder, expected, a.output_dir, a.overwrite, inputs=inputs, threshold=a.threshold)
    except (S.SourceError, S.LenderSelectionError) as e:
        raise SystemExit(f"\nNOT GENERATED.\n{e}")


if __name__ == "__main__":
    main()
