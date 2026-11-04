"""Paths, field names and template locations for the Bank Workbook Generator (Stage 1).

All source fields are referenced by NAME (never by column letter). The four processed
historical FOIA files and the FinWise template workbook are read-only inputs.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# One sub-folder per SBA FOIA release, named by its AsOfDate (e.g. source_data/20260630), holding the four
# fix_foia.py outputs (FOIA_7a_*_Corrected.csv). Files re-saved by Excel (FOIA-7a-*.csv) are also accepted.
SOURCE_DIR = PROJECT_ROOT / "source_data"
SOURCE_GLOB = "FOIA*7a*.csv"
TEMPLATE_PATH = PROJECT_ROOT / "reference" / "SBA_Historical Performance_FinWise Bank_2026Q2.xlsx"
OUTPUT_DIR = PROJECT_ROOT / "output"
CACHE_DIR = OUTPUT_DIR / ".cache"

# --- Lender selection (client confirmed: BankName + BankState; data dictionary rows 10 and 15)
F_BANK_NAME = "BankName"
F_BANK_STATE = "BankState"
F_LOCATION_ID = "LocationID"
F_FDIC_CERT = "BankFDICNumber"      # lender identity evidence (data dictionary row 11)
F_BANK_CITY = "BankCity"
F_BANK_STREET = "BankStreet"

# --- Population exclusions (client confirmed)
F_LOAN_STATUS = "LoanStatus"
F_FIRST_DISBURSEMENT = "FirstDisbursementDate"
CANCELLED_STATUS = "CANCLD"

# --- Grouping fields used by the template formulas
F_SECTOR = "NaicsDescription"   # PerformanceSummary sector rows match FOIA!Z (NaicsDescription)
F_SUBPROGRAM = "Subprogram"     # PerformanceSummary subprogram rows match FOIA!U (Subprogram)
F_BALANCE = "GrossApproval"     # Summed by the template (FOIA!O)
F_APPROVAL_FY = "ApprovalFiscalYear"
F_AS_OF = "AsOfDate"
NA_SECTOR = "N/A"               # description carried by every blank NaicsCode in the processed files

# --- Type conversion for the template FOIA tab (matches the template's existing cell types)
DATE_FIELDS = {"AsOfDate", "ApprovalDate", "FirstDisbursementDate", "PaidInFullDate", "ChargeOffDate"}
NUMERIC_FIELDS = {"GrossApproval", "SBAGuaranteedApproval", "ApprovalFiscalYear",
                  "InitialInterestRate", "TermInMonths", "NaicsCode",
                  "GrossChargeOffAmount", "JobsSupported"}
# Codes kept exactly as published (text), so leading zeros survive: BorrZip, BankZip, CongressionalDistrict and
# FranchiseCode. No template formula references these columns (FOIA!G, N, AF, AA).
NUMERIC_IF_DIGITS_FIELDS = set()

# --- Every field the generator reads; a source file missing any of them is refused
REQUIRED_FIELDS = ("AsOfDate", "BankName", "BankState", "LocationID", "BankFDICNumber", "BankCity", "BankStreet",
                   "LoanStatus", "FirstDisbursementDate", "NaicsDescription", "Subprogram", "GrossApproval",
                   "ApprovalFiscalYear")

# --- Year range built into the template's hidden tabs (ChgOffs, OrigAmt, CumPrepayments, Status_by_Vintage).
# PerformanceSummary vintage rows extend automatically; the hidden tabs do not, so a year after the last one is refused.
TEMPLATE_HIDDEN_TAB_FIRST_YEAR, TEMPLATE_HIDDEN_TAB_LAST_YEAR = 1991, 2026

# --- Template sheet names
SH_SUMMARY = "PerformanceSummary"
SH_FOIA = "FOIA"
SH_CHGOFFS = "ChgOffs_byNAICs&ChgOffYr"
SH_ORIGAMT = "OrigAmt_ChgOffs_byNAICs&Vintage"
SH_CUMPREPAY = "CumPrepayments"
SH_LOOKUP = "Lookup"
SH_GLOSSARY = "Glossary"
SH_PIVOT = "Sector_Pivot"
SH_INFO = "Generation_Info"
SH_VALIDATION = "Validation"
SHEETS_TO_REMOVE = ("Sheet1", "ChargeOffsByVintage_Sec")   # FinWise loan tape / securitization-only

# --- Industry-matched SBA 7(a) benchmark (PerformanceSummary X:CE) - PRELIMINARY, pending FalconBridge confirmation.
# Rules recovered from FalconBridge's 'SBA Orig_v2' workbook (acceptance/benchmark/BENCHMARK_METHODOLOGY.md).
BENCHMARK_FIRST_FY = 2000            # Orig_v2 loads the FY2000-FY2009, FY2010-FY2019 and FY2020-Present files
BENCHMARK_STANDARD_MIN = 350000      # Standard: GrossApproval > 350,000 ("GT 350K"); Small: the rest
BENCHMARK_CRE_MIN_TERM = 120         # CRE: TermInMonths > 120 ("GT 10 Yrs"); Non-CRE: the rest
BENCHMARK_FIRST_VINTAGE = 1999       # first vintage row with benchmark formulas in the template (rows 29-56)
SH_BENCH_NOTES = "Benchmark_Notes"
