#!/bin/zsh
# Threshold acceptance matrix (local only): generate each bank at each threshold into a scratch folder, recalculate
# with LibreOffice headless, and reconcile every value with the independent oracle. Results: acceptance/threshold/results.
set -u
cd "$(dirname $0)/../.."
P=.venv/bin/python; W=${1:?scratch folder}; R=acceptance/threshold/results
mkdir -p $W/gen $W/recalc $R
run() {  # cert name state threshold
  local c=$1 n=$2 s=$3 t=$4
  local sfx=$([ $t = 350000 ] && echo "" || echo "_GT$t")
  local f=$(echo "$n" | sed 's/[^A-Za-z0-9]\{1,\}/_/g')_${s}_20260630${sfx}.xlsx
  $P bank_workbook_generator/generate_bank_workbook.py --as-of 2026-06-30 --fdic-cert $c --threshold $t --output-dir $W/gen --overwrite > $W/gen/${c}_$t.log 2>&1
  /opt/homebrew/bin/soffice -env:UserInstallation=file://$W/lo_${c}_${t} --headless --calc --convert-to xlsx --outdir $W/recalc $W/gen/$f > /dev/null 2>&1
  $P -I acceptance/threshold/oracle.py --workbook $W/recalc/$f --release source_data/20260630 --bank-name "$n" --bank-state $s --cert $c --threshold $t --out $R/${f%.xlsx}.json > /dev/null
  echo "$f exit=$?"
}
for t in 150000 350000 500000 1000000; do
  run 58310 "US Metro Bank" CA $t &
  run 35323 "FinWise Bank" UT $t &
  run 27417 "Shoreham Bank" RI $t &
  wait
done
