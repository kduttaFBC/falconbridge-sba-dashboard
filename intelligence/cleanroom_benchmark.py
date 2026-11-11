"""Clean-room validation of Benchmark Intelligence Phase A (B0, B1, B2, industry weights, rates and contributions).

Recomputes every value of one Benchmark Intelligence record directly from the original SBA 7(a) FOIA CSV files,
independently of the dashboard (webapp), the workbook generator (bank_workbook_generator) and the benchmark cache:
nothing here imports them. Definitions are restated from the documented methodology (benchmark: FalconBridge's
Orig_v2 method as documented in acceptance/benchmark/BENCHMARK_METHODOLOGY.md; Phase A: the approved proposal
reports/FalconBridge_Benchmark_Intelligence_Proposal_2026-11-09.md), not copied from code:

  SBA population   ApprovalFiscalYear >= 2000, LoanStatus not CANCLD, FirstDisbursementDate present
  SBA defaulted    ChargeOffDate present; amounts GrossApproval rounded half-to-even to whole dollars
  industry         NaicsDescription as published ("N/A" when blank); matched case-insensitively
  lender rows      BankName and BankState equal, LocationID one of the selected ids, FDIC certificate equal (if given);
                   eligible: LoanStatus present and not CANCLD, FirstDisbursementDate present
  bank industries  every industry of the lender's eligible rows (any approval year)  -> defines B0's industries
  bank FY2000+     eligible rows with ApprovalFiscalYear >= 2000; defaulted = LoanStatus CHGOFF or CLSLN;
                   amounts GrossApproval as published
  B0 = sum defaulted SBA $ / sum SBA $ over the bank's industries     B1 = the same over every industry
  B2 = sum_i w_i r_i over industries with >= 30 SBA loans (w renormalised over them), computed only if they hold
       >= 95% of the bank's FY2000+ $;  r_i = SBA industry rate, w_i = bank FY2000+ $ share
  contributions: mix_i = (w_i - s_i)(r_i - B0), within_i = w_i (b_i - r_i), sparse adjustment = R_bank - R_covered

Output: a ValidationRecord bound to the Benchmark Intelligence record (system foia-benchmark-intel, record id, record
SHA-256), carrying the SHA-256 of every input file. Numbers only; never methodology approval.
"""
import argparse
import csv
import glob
import hashlib
import json
import os
import sys
from collections import defaultdict
from decimal import ROUND_HALF_EVEN, Decimal

from . import validation as VAL
from .evidence import EvidenceError, sha256_of

METHODOLOGY_VERSION = "cleanroom-benchmark-1.0"
SYSTEM = "foia-benchmark-intel"
DEFAULTED = {"CHGOFF", "CLSLN"}
SPARSE, COVERAGE_MIN = 30, 0.95
COLS = ("BankName", "BankState", "BankFDICNumber", "LocationID", "GrossApproval", "ApprovalFiscalYear",
        "FirstDisbursementDate", "LoanStatus", "ChargeOffDate", "NaicsDescription", "AsOfDate")


def _whole(t):
    return int(Decimal((t or "").strip() or "0").quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _amount(t):
    t = (t or "").strip()
    return float(t) if t else 0.0


def _id(t):
    t = (t or "").strip()
    return str(int(t)) if t.isdigit() else t


def read(folder, name, state, cert, locations):
    """One pass: SBA industry aggregates (casefold -> [loans, $, defaulted $]) and the lender's eligible rows."""
    files = sorted(glob.glob(os.path.join(folder, "FOIA*7a*.csv")))
    if not files:
        raise EvidenceError(f"no SBA 7(a) FOIA files in {folder}")
    sba = defaultdict(lambda: [0, 0, 0])
    lender, prints, as_of = [], [], None
    locs = {_id(x) for x in locations}
    for path in files:
        h = hashlib.sha256()

        def lines(path=path, h=h):
            with open(path, "rb") as fb:
                for raw in fb:
                    h.update(raw)
                    yield raw.decode("utf-8")
        rd = csv.reader(lines())
        head = next(rd)
        ix = {k: head.index(k) for k in COLS}
        for r in rd:
            st, fd = r[ix["LoanStatus"]].strip(), r[ix["FirstDisbursementDate"]].strip()
            as_of = as_of or r[ix["AsOfDate"]].strip()[:10]
            ind = r[ix["NaicsDescription"]] or "N/A"
            if r[ix["BankName"]] == name and r[ix["BankState"]] == state and _id(r[ix["LocationID"]]) in locs and \
                    (not cert or _id(r[ix["BankFDICNumber"]]) == cert) and st and st != "CANCLD" and fd:
                lender.append({"ind": ind, "fy": int(r[ix["ApprovalFiscalYear"]]), "status": st,
                               "gross": _amount(r[ix["GrossApproval"]])})
            fy = r[ix["ApprovalFiscalYear"]].strip()
            if not fy or int(fy) < 2000 or st == "CANCLD" or not fd:
                continue
            g = _whole(r[ix["GrossApproval"]])
            v = sba[ind.casefold()]
            v[0] += 1
            v[1] += g
            v[2] += g if r[ix["ChargeOffDate"]].strip() else 0
        prints.append((os.path.basename(path), h.hexdigest()))
    return sba, lender, prints, as_of


def recompute(sba, lender, names):
    """Independent values keyed like the Benchmark Intelligence evidence. `names`: the record's industry labels, used
    only to name the keys (matched case-insensitively); every number comes from the FOIA rows."""
    inds = {x["ind"].casefold() for x in lender}
    label = {n.casefold(): n for n in names}
    if inds != set(label):
        raise EvidenceError("the lender's industries in the FOIA files differ from the record's industry list")
    fy = [x for x in lender if x["fy"] >= 2000]
    bo, bd, bn = defaultdict(float), defaultdict(float), defaultdict(int)
    for x in fy:
        k = x["ind"].casefold()
        bo[k] += x["gross"]
        bd[k] += x["gross"] if x["status"] in DEFAULTED else 0.0
        bn[k] += 1
    R_orig, R_def = sum(bo.values()), sum(bd.values())
    N = sum(sba[k][1] for k in inds)
    D = sum(sba[k][2] for k in inds)
    B0 = D / N
    a_n, a_o, a_d = (sum(v[i] for v in sba.values()) for i in range(3))
    v = {"bank_fy2000.loans": len(fy), "bank_fy2000.orig": R_orig, "bank_fy2000.defaulted_orig": R_def,
         "bank_fy2000.cum_def": R_def / R_orig, "B0.loans": sum(sba[k][0] for k in inds), "B0.orig": N,
         "B0.defaulted_orig": D, "B0.cum_def": B0, "B1.loans": a_n, "B1.orig": a_o, "B1.defaulted_orig": a_d,
         "B1.cum_def": a_d / a_o}
    exposed = [k for k in inds if bo[k] > 0]
    covered = [k for k in exposed if sba[k][0] >= SPARSE]
    c_orig = sum(bo[k] for k in covered)
    v["B2.coverage"] = c_orig / R_orig
    computed = v["B2.coverage"] >= COVERAGE_MIN
    w = {k: (bo[k] / c_orig if computed and k in covered else 0.0) for k in inds}
    r = {k: (sba[k][2] / sba[k][1] if sba[k][1] else None) for k in inds}
    if computed:
        v["B2.cum_def"] = sum(w[k] * r[k] for k in inds if w[k])
        R_cov = sum(bd[k] for k in covered) / c_orig
        v["decomposition.gap.diff"] = R_def / R_orig - B0
        v["decomposition.mix_total.diff"] = sum((w[k] - sba[k][1] / N) * (r[k] - B0) for k in inds if r[k] is not None)
        v["decomposition.within_total.diff"] = sum(w[k] * (bd[k] / bo[k] - r[k]) for k in inds if w[k])
        v["decomposition.sparse_adjustment.diff"] = R_def / R_orig - R_cov
    for k in inds:
        n = label[k]
        p = f"industry.{n}."
        v[p + "bank_loans"] = bn[k]
        v[p + "bank_share"] = bo[k] / R_orig
        v[p + "sba_loans"] = sba[k][0]
        v[p + "sba_share"] = sba[k][1] / N
        if r[k] is not None:
            v[p + "sba_cum_def"] = r[k]
        if bo[k]:
            v[p + "bank_cum_def"] = bd[k] / bo[k]
        if computed:
            v[p + "mix.diff"] = (w[k] - sba[k][1] / N) * (r[k] - B0) if r[k] is not None else 0.0
            if w[k]:
                v[p + "within.diff"] = w[k] * (bd[k] / bo[k] - r[k])
    return {"bench_intel." + k: x for k, x in v.items()}, len(fy)


def produce(record_id, content, folder, performed_on, performed_by="clean-room producer (offline)"):
    c = content["criteria"]
    sba, lender, prints, as_of = read(folder, c["bank_name"], c["bank_state"], str(c.get("cert") or ""),
                                      c["location_ids"])
    if as_of != c["as_of"]:
        raise EvidenceError(f"release as-of {as_of} differs from the record's {c['as_of']}")
    values, n_fy = recompute(sba, lender, [x["name"] for x in content["industries"]])
    rec = VAL.make_validation_record(
        SYSTEM, record_id, sha256_of(content),
        "clean-room recomputation of Benchmark Intelligence (B0, B1, B2, industry weights, rates and contributions) "
        "from the SBA 7(a) FOIA CSV files; no application, generator, cache or workbook code used",
        performed_on, performed_by, "intelligence/cleanroom_benchmark.py (module docstring: definitions)", values,
        input_fingerprints=prints, as_of=as_of,
        population=(f"{c['bank_name']}, {c['bank_state']}, FDIC {c.get('cert') or '-'}, LocationID "
                    f"{', '.join(c['location_ids'])}: {len(lender)} eligible loans, {n_fy} approved FY2000+; SBA "
                    f"FY2000+ population {values['bench_intel.B1.loans']:,} loans"),
        methodology_version=METHODOLOGY_VERSION)
    return rec


def compare(rec, record_values):
    """(agree, disagree, missing) of a validation record against {metric key: value} of the record's evidence."""
    held = dict(rec.values)
    agree, disagree, missing = 0, [], []
    for k, x in record_values.items():
        if k not in held:
            missing.append(k)
        elif VAL.agrees(rec, held[k], x):
            agree += 1
        else:
            disagree.append((k, x, held[k]))
    return agree, disagree, missing


def store_dir(app_data, analysis_id):
    return os.path.join(app_data, "benchmark_intel", analysis_id)


def save_record(app_data, analysis_id, rec):
    d = store_dir(app_data, analysis_id)
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, rec.record_id + ".json")
    if not os.path.exists(f):
        with open(f + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(VAL.to_json(rec), fh, sort_keys=True)
        os.replace(f + ".tmp", f)
    return f


def load_records(app_data, analysis_id):
    d = store_dir(app_data, analysis_id)
    out = []
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if name.startswith("VR-") and name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                rec = VAL.from_json(json.load(fh))
            if rec.record_id + ".json" != name:
                raise EvidenceError(f"validation record file {name} does not match its content")
            out.append(rec)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clean-room validation of Benchmark Intelligence records")
    ap.add_argument("--app-data", required=True)
    ap.add_argument("--release", required=True)
    ap.add_argument("--performed-on", required=True)
    ap.add_argument("analysis_ids", nargs="+")
    a = ap.parse_args(argv)
    for aid in a.analysis_ids:
        d = os.path.join(a.app_data, "benchmark_intel", aid)
        for name in sorted(os.listdir(d)):
            if not (name.startswith("BI-") and name.endswith(".json")):
                continue
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                doc = json.load(fh)
            if "BI-" + sha256_of(doc["content"])[:24] != doc["record_id"]:
                raise EvidenceError(f"{name} failed its integrity check")
            rec = produce(doc["record_id"], doc["content"], a.release, a.performed_on)
            save_record(a.app_data, aid, rec)
            print(json.dumps({"analysis": aid, "record": doc["record_id"], "validation": rec.record_id,
                              "values": len(rec.values)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
