# FalconBridge Bank Workbook Generator

Creates the FalconBridge SBA historical performance workbook for any bank FalconBridge selects, from the historical SBA 7(a) FOIA files. It uses the same template and Excel formulas as the FinWise workbook. You pick the **reporting period** (the SBA FOIA release) and the **bank** (by FDIC certificate, or Bank Name + Bank State); the tool confirms the lender's identity, prepares that bank's eligible loans and fills a copy of the template. **Microsoft Excel then calculates the Performance Summary and the Validation sheet.**

## 1. What you need on your Mac

| Software | Why | How to get it |
|---|---|---|
| **macOS** | The launchers are macOS double-click files | — |
| **Python 3.9 or newer** | Runs the generator | Recommended: the official installer from <https://www.python.org/downloads/macos/>. Apple's Command Line Tools Python (offered automatically the first time `python3` is used) also works. |
| **Python package `openpyxl` (version 3.1.2)** | Reads and writes the Excel template | Installed by double-clicking **`Install Requirements (run once).command`** (needs an internet connection once) |
| **Microsoft Excel** (Microsoft 365 / Excel 2019 or newer) | Opens the generated workbook and calculates it | Your normal Office installation |

Disk space: about 750 MB for this folder, plus 2–45 MB per generated workbook. Memory: 8 GB RAM is sufficient.

## 2. One-time setup

1. Copy the whole **`FalconBridge_Bank_Workbook_Generator_Client`** folder to your Mac, for example into *Documents*. Keep its contents together; do not rename or move the folders inside it.
2. Install Python if it isn't already installed (see the table above).
3. Double-click **`Install Requirements (run once).command`**. When it prints *"Setup complete"*, press Enter to close the window.

**If macOS says the file "cannot be opened" or is from an unidentified developer:** right-click (or Control-click) the file, choose **Open**, then **Open** again. You only need to do this once per file.

## 2a. Add an SBA FOIA release (once per quarter)

1. Download the four SBA 7(a) FOIA files for the release into one folder (e.g. `Python/input`).
2. Run FalconBridge's existing correction script, writing straight into a folder named after the release date:

   `python3 foia_processing/fix_foia.py --input <folder with the 4 SBA files> --output source_data/20260630 --naics foia_processing/NAICS_Fix.xlsx --subprogram foia_processing/Subprogram_Fix.xlsx`

   **Do not open or re-save the files in Excel.** Excel rounds amounts, drops leading zeros (ZIP codes, lender IDs) and turns some names into `#NAME?` or dates. The generator reads the script's output directly.
3. Check `source_data/<date>/correction_log.txt` for unmapped NAICS codes or processing methods.

The generator refuses a release whose files mix AsOfDates or whose AsOfDate differs from the folder name. Earlier releases stay in their own folders; workbooks go to `output/<date>/` and are never overwritten without `--overwrite`.

## 3. Generate a workbook

1. Double-click **`Generate Bank Workbook.command`**. A Terminal window opens.
2. If more than one release is available, pick the reporting period.
3. Type part of the bank's name (e.g. `glacier`), or its FDIC certificate number, and press Enter.
   - The first run takes about 20–30 seconds while the tool indexes the historical files. Later runs are faster.
4. A numbered list appears showing **Bank Name**, **State**, **Eligible loans**, **FDIC cert(s)** and the number of SBA **Lender IDs**.
   - The same bank name can appear in several states. Pick the correct **state**.
   - Several SBA lender IDs of the **same FDIC certificate** are combined, as FalconBridge specified.
   - **Different lenders are never combined.** If a Bank Name + State holds more than one FDIC certificate (marked *more than one lender*), the tool lists each one with its lender ID and city and asks for the certificate you want. A lender with no FDIC certificate and several lender IDs needs the IDs to include.
5. Type the number of the bank, check the lender evidence shown, then type `y` to confirm.
6. The tool runs for about 20 seconds (small banks) to about 1 minute (the largest banks). When it finishes, the **`output`** folder opens. Your workbook is named:

   `output/<AsOfDate>/<Bank_Name>_<State>_<AsOfDate>.xlsx` (for example `output/20260630/Glacier_Bank_MT_20260630.xlsx`)

**Without the menus** (Terminal, from this folder):

    python3 bank_workbook_generator/generate_bank_workbook.py --as-of 2026-06-30 --fdic-cert 35323
    python3 bank_workbook_generator/generate_bank_workbook.py --as-of 2026-06-30 --bank-name "Shoreham Bank" --bank-state RI
    python3 bank_workbook_generator/generate_bank_workbook.py --as-of 2026-06-30 --bank-name "Citizens Bank" --bank-state MO --fdic-cert 14488

## 3a. Browser dashboard (local)

A local browser application on top of the same generator, which supplies every figure and every workbook.

- **Start:** double-click **Start Browser App.command**, or run `.venv/bin/python -m webapp` from this folder, then open
  **http://127.0.0.1:8000**. It listens on this computer only. Stop it with Ctrl+C or by closing the window.
- **First-time setup (once):** `python3.12 -m venv .venv && .venv/bin/python -m pip install -r requirements-web.txt`.
- **New analysis:**
  1. Choose the reporting period.
  2. Choose a **state**, then the **bank** from a searchable list of every eligible bank in that state. Type to filter by
     name, city or FDIC certificate. You can also search all states by name or certificate.
  3. Enter the **original loan balance threshold** (whole dollars, default $350,000).
  4. Confirm the lender identity. Ambiguous lenders are refused until you choose a certificate.
- **Dashboard:**
  - **Sections:** Bank Overview, Portfolio Performance, Industry Comparison, and Definitions & data.
  - **Loan group:** all loans, above the threshold, at or below it, or both groups side by side.
  - **Comparison basis:** Industry Comparison uses loans approved FY2000 onward for the bank and the benchmark.
    Bank-only views use the full history.
- **Excel workbook:** *Generate Excel workbook* creates the workbook for exactly the dashboard's criteria. At a threshold
  other than $350,000 the file name ends `_GT<threshold>`, for example `..._20260630_GT500000.xlsx`. The headings,
  Benchmark_Notes and the new **Threshold_Analysis** sheet follow the threshold.
- **Saved analyses:** *Save analysis* stores the criteria, the figures shown (frozen), the release and calculation
  version, and generates the matching workbook. Reopen saved analyses from *Saved analyses*; a new analysis never
  overwrites one.
- **Files:** workbooks, job records, saved analyses and the audit log go to `app_data/`. The app never writes to
  `output/`.
- **Command line:** `--threshold 500000` generates the same threshold workbook.
- **Tests:** `.venv/bin/python -m pytest -q tests` (about 3 minutes). Threshold acceptance:
  `acceptance/threshold/run_matrix.sh <scratch folder>`, which needs LibreOffice.
- **Not available from SBA FOIA:** current outstanding principal and balances. The dashboard shows the *original amount
  of active loans* and labels it as not a balance.
- **Benchmark:** **PRELIMINARY**.

## 4. Opening the workbook in Excel

- When you **first open** a generated workbook, Excel calculates all formulas and rebuilds the sector pivot. The status bar shows *Calculating*.
  - Small and mid-size banks: a few seconds to about 2 minutes.
  - The largest lenders (100,000+ loans): about 3–4 minutes.
  - Wait until calculation finishes.
- **Save the workbook once** after it has calculated. Later openings are then fast.
- No "update links" prompt should appear; the workbook has no external links.

## 5. What the workbook contains

- **Population rule:** the bank's loans, excluding those with a blank Loan Status, Loan Status `CANCLD`, or a blank First Disbursement Date.
- **PerformanceSummary:**
  - **Sectors:** the bank's own sector list (NAICS description), ordered by total Gross Approval. Loans without a NAICS classification appear as the final **N/A** sector.
  - **Subprograms:** the bank's own subprograms.
  - **Vintage years:** cover every disbursement year present.
- **Not available for other banks (shown as such):** the FinWise securitization section. It depends on the lender's loan tape.
- **SBA benchmark (columns X:CE) - PRELIMINARY:** an *industry-matched* SBA 7(a) benchmark computed from the same FOIA release, using FalconBridge's existing method.
  - The vintage rows and the totals cover SBA loans in this bank's industries.
  - Each sector row covers every SBA loan in that industry, the same for every bank.
  - The data sits in hidden `Combined_*` sheets.
  - The **Benchmark_Notes** tab gives the method, the source, the definitions that differ from the bank columns, and the cells flagged for analyst review.
  - Results are subject to FalconBridge's confirmation of the method.
- **Generation_Info tab:** the reporting period, the analyst's selection, the lender identity evidence (FDIC certificate, SBA lender IDs, city/street, any same-name lenders excluded), exclusion counts, eligible totals, N/A totals, items NOT AVAILABLE from FOIA, and the source files (SHA-256).
- **Validation tab:** each key Performance Summary cell against a recomputation from the FOIA rows, with the formula and FOIA fields; the top row shows the PASS/FAIL count. It also reconciles original gross, guaranteed and unguaranteed approval amounts and gross charge-offs (original amounts, **not** balances).
- **Example:** `example_output/EXAMPLE_FinWise_Bank_UT_20260630.xlsx` is an example generated for FinWise Bank, UT.

## 6. Folder contents (do not edit)

| Folder / file | Purpose |
|---|---|
| `Generate Bank Workbook.command` | Starts the generator |
| `Install Requirements (run once).command` | One-time setup |
| `bank_workbook_generator/` | Program files |
| `reference/` | FalconBridge Excel template (read-only input) |
| `source_data/<YYYYMMDD>/` | One folder per SBA FOIA release: the four `fix_foia.py` output files (read-only input) |
| `foia_processing/` | FalconBridge's `fix_foia.py` and its NAICS / Subprogram mapping files (unchanged) |
| `acceptance/` | Acceptance tests and results (FinWise, Shoreham, processing comparison) |
| `output/` | Generated workbooks. `output/.cache` holds a lender index that is rebuilt automatically. |
| `example_output/` | One example workbook |

The tool never modifies the files in `reference/` or `source_data/`.

## 7. Troubleshooting

| Message | What to do |
|---|---|
| "Required component 'openpyxl' is missing" | Run `Install Requirements (run once).command` |
| "Python 3.9 or newer was not found" | Install Python from python.org, then run the setup again |
| "No bank found…" | Type fewer letters, or a different part of the name |
| "No historical FOIA files … found" / "No FOIA release folder" | Run `fix_foia.py` into `source_data/<YYYYMMDD>` (section 2a) |
| "NOT GENERATED … records of different lenders" | Re-run with `--fdic-cert` for the bank you want (the message lists the candidates) |
| "… years after 2026 …" | The release contains years beyond the template's hidden tabs; the template must be extended first |
