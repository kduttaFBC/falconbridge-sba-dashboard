"""Clean-room validation producer for the SBA FOIA application (M3).

Recomputes selected financial measures of ONE Saved Analysis directly from the original SBA 7(a) FOIA CSV files,
independently of the FalconBridge workbook calculations and of this project's calculation engine: nothing here
imports the dashboard (webapp), the workbook generator (bank_workbook_generator) or the acceptance oracle. The
definitions are restated from FalconBridge's documented definitions (workbook Glossary / Lookup table, as written in
the methodology disclosures), not copied from code:

  population   rows of the release whose BankName and BankState equal the analysis's lender, whose LocationID is
               one of the analysis's selected location ids, and whose FDIC certificate matches (when one is given)
  eligible     LoanStatus present and not CANCLD, and FirstDisbursementDate present
  FY2000+      ApprovalFiscalYear >= 2000           vintage = year of FirstDisbursementDate
  size split   GrossApproval rounded half-to-even to whole dollars > threshold = above; the rest = at or below
  defaulted    LoanStatus CHGOFF or CLSLN           active = CURR, PURCH(NOT C/O), PSTDUE, DEFERD, DELINQ, LIQUID,
                                                             EXEMPT
  paid in full LoanStatus "P I F"                    PURCH = LoanStatus PURCH(NOT C/O)
  amounts      GrossApproval / SBAGuaranteedApproval / GrossChargeOffAmount as published (original amounts)
  % Cum. Defaulted = original amount of defaulted loans / original amount
  % Cum. Loss = gross charge-offs of defaulted loans / original amount
  % Loss Severity = gross charge-offs / original amount of defaulted loans (pooled)
  Avg. loan age = whole months from FirstDisbursementDate to the as-of date (a month counts once its day is
                  reached), averaged over active loans

Output: a ValidationRecord bound to the Saved Analysis (id + payload SHA-256), its reporting period, the population
(selection rule and counts) and this methodology version, carrying the SHA-256 of every input file. It records
numbers only. It is not, and never implies, FalconBridge methodology approval. The SBA benchmark is not recomputed
here (its industry matching depends on FalconBridge's sector corrections and a template weighting): benchmark values
stay PRELIMINARY.
"""
import argparse
import csv
import glob
import hashlib

import json
import os
import sys
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from . import validation as VAL

METHODOLOGY_VERSION = "cleanroom-foia-1.0"
SYSTEM = "foia-saved-analysis"
DEFAULTED = {"CHGOFF", "CLSLN"}
ACTIVE = {"CURR", "PURCH(NOT C/O)", "PSTDUE", "DEFERD", "DELINQ", "LIQUID", "EXEMPT"}
KEEP = ("BankName", "BankState", "BankFDICNumber", "LocationID", "GrossApproval", "SBAGuaranteedApproval",
        "ApprovalFiscalYear", "FirstDisbursementDate", "LoanStatus", "GrossChargeOffAmount", "AsOfDate",
        # Phase 1 activity validation (read only; the measures above do not use them)
        "ChargeOffDate", "ProjectState", "BorrState", "NaicsDescription", "ProcessingMethod", "BusinessAge",
        "FixedOrVariableInterestInd")
TOTAL_METRICS = ("loans", "orig", "guar", "unguar", "avg", "active", "active_orig", "age", "chgoff", "chgoff_orig",
                 "chgoff_amt", "pif_orig", "purch_orig", "cum_def", "severity", "cum_loss", "pif_pct", "purch_pct")
VINTAGE_METRICS = ("loans", "orig", "chgoff", "cum_def", "cum_loss")


class CleanRoomError(RuntimeError):
    pass


def _date(t):
    t = t.strip()
    if "/" in t:
        m, d, y = t.split("/")
        return date(int(y), int(m), int(d))
    return date.fromisoformat(t[:10])


def _amount(t):
    t = (t or "").strip()
    return float(t) if t else 0.0


def _whole(t):
    return int(Decimal((t or "").strip() or "0").quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _id(t):
    t = (t or "").strip()
    return str(int(t)) if t.isdigit() else t


def _months(d0, d1):
    n = (d1.year - d0.year) * 12 + d1.month - d0.month
    return n - 1 if d1.day < d0.day else n


def read_release(folder, lenders):
    """One pass over the release. `lenders`: {key: (bank_name, bank_state, cert or '', set(location_ids))}.
    Returns ({key: {"rows": [...], "records": n, "excluded": {...}}}, as_of, [(file, sha256)])."""
    files = sorted(glob.glob(os.path.join(folder, "FOIA*7a*.csv")))
    if not files:
        raise CleanRoomError(f"no SBA 7(a) FOIA files in {folder}")
    by_name = {}
    for key, (name, state, cert, locs) in lenders.items():
        by_name.setdefault((name, state), []).append((key, cert, set(locs)))
    out = {k: {"rows": [], "records": 0, "excluded": {}} for k in lenders}
    as_of, prints = None, []
    for path in files:
        h = hashlib.sha256()

        def lines(path=path, h=h):
            with open(path, "rb") as fb:
                for raw in fb:
                    h.update(raw)
                    yield raw.decode("utf-8")
        rd = csv.reader(lines())
        head = next(rd)
        ix = {k: head.index(k) for k in KEEP}
        for r in rd:
            hits = by_name.get((r[ix["BankName"]], r[ix["BankState"]]))
            if as_of is None:
                as_of = _date(r[ix["AsOfDate"]])
            if not hits:
                continue
            for key, cert, locs in hits:
                if _id(r[ix["LocationID"]]) not in locs or (cert and _id(r[ix["BankFDICNumber"]]) != cert):
                    continue
                o = out[key]
                o["records"] += 1
                st, fd = r[ix["LoanStatus"]].strip(), r[ix["FirstDisbursementDate"]].strip()
                if not st or st == "CANCLD":
                    why = "LoanStatus = CANCLD" if st == "CANCLD" else "blank LoanStatus"
                elif not fd:
                    why = "blank FirstDisbursementDate"
                else:
                    o["rows"].append({k: r[ix[k]] for k in KEEP})
                    continue
                o["excluded"][why] = o["excluded"].get(why, 0) + 1
        prints.append((os.path.basename(path), h.hexdigest()))
    return out, as_of, prints


def measures(rows, as_of):
    st = lambda r: r["LoanStatus"].strip()                                   # noqa: E731
    d = [r for r in rows if st(r) in DEFAULTED]
    a = [r for r in rows if st(r) in ACTIVE]
    s = lambda rs, k: sum(_amount(r[k]) for r in rs)                          # noqa: E731
    m = {"loans": len(rows), "orig": s(rows, "GrossApproval"), "guar": s(rows, "SBAGuaranteedApproval"),
         "active": len(a), "active_orig": s(a, "GrossApproval"), "chgoff": len(d), "chgoff_orig": s(d, "GrossApproval"),
         "chgoff_amt": s(d, "GrossChargeOffAmount"),
         "pif_orig": s([r for r in rows if st(r) == "P I F"], "GrossApproval"),
         "purch_orig": s([r for r in rows if st(r) == "PURCH(NOT C/O)"], "GrossApproval")}
    m["unguar"] = m["orig"] - m["guar"]
    ratio = lambda x, y: x / y if y else None                                 # noqa: E731
    m.update(avg=ratio(m["orig"], m["loans"]), cum_def=ratio(m["chgoff_orig"], m["orig"]),
             severity=ratio(m["chgoff_amt"], m["chgoff_orig"]), cum_loss=ratio(m["chgoff_amt"], m["orig"]),
             pif_pct=ratio(m["pif_orig"], m["orig"]), purch_pct=ratio(m["purch_orig"], m["orig"]),
             age=ratio(sum(_months(_date(r["FirstDisbursementDate"]), as_of) for r in a), len(a)))
    return m


def recompute(rows, as_of, threshold):
    """{metric_key: value} for the population, keyed like the evidence layer (bank.<history>.<group>...)."""
    out = {}
    hist = {"full": rows, "fy2000": [r for r in rows if r["ApprovalFiscalYear"].strip()
                                     and float(r["ApprovalFiscalYear"]) >= 2000]}
    for h, hr in hist.items():
        groups = {"total": hr, "above": [r for r in hr if _whole(r["GrossApproval"]) > threshold],
                  "below": [r for r in hr if _whole(r["GrossApproval"]) <= threshold]}
        for g, gr in groups.items():
            for k, v in measures(gr, as_of).items():
                if v is not None:
                    out[f"bank.{h}.{g}.total.{k}"] = v
    years = sorted({_date(r["FirstDisbursementDate"]).year for r in rows})
    for y in years:
        vr = [r for r in rows if _date(r["FirstDisbursementDate"]).year == y]
        m = measures(vr, as_of)
        for k in VINTAGE_METRICS:
            if m[k] is not None:
                out[f"bank.full.total.vintage.{y}.{k}"] = m[k]
    return out



# ------------------------------------------------------------------------------- Phase 1: lending activity (independent)
SIZE_BAND_LIMITS = ((150_000, "$150K or less"), (350_000, "$150K to $350K"), (1_000_000, "$350K to $1M"),
                    (2_000_000, "$1M to $2M"), (None, "Over $2M"))


def _band(gross_text):
    g = _whole(gross_text)
    return next(label for limit, label in SIZE_BAND_LIMITS if limit is None or g <= limit)


def recompute_activity(rows):
    """Independent restatement of the Phase 1 activity measures: {(table, key, measure): value}. Origination periods
    are the year / quarter of FirstDisbursementDate; charge-off events are default-status loans (CHGOFF, CLSLN) by
    the year / quarter of ChargeOffDate; amounts are original (gross, guaranteed, gross - guaranteed) and gross
    charge-off cash. No proportional unguaranteed loss, rates by period or collateral proxy."""
    out = {}

    def add(table, key, r, defaulted):
        g, gu = _amount(r["GrossApproval"]), _amount(r["SBAGuaranteedApproval"])
        for m, v in (("loans", 1), ("gross", g), ("guar", gu), ("unguar", g - gu),
                     ("default_loans", 1 if defaulted else 0), ("default_gross", g if defaulted else 0.0),
                     ("gross_chargeoff", _amount(r["GrossChargeOffAmount"]) if defaulted else 0.0)):
            out[(table, key, m)] = out.get((table, key, m), 0) + v
    undated = 0
    for r in rows:
        st = r["LoanStatus"].strip()
        defaulted = st in DEFAULTED
        d = _date(r["FirstDisbursementDate"])
        q = f"{d.year}Q{(d.month + 2) // 3}"
        add("annual", d.year, r, defaulted)
        add("quarterly", q, r, defaulted)
        add("project_state", r["ProjectState"].strip() or "Not reported", r, defaulted)
        add("borrower_state", r["BorrState"].strip() or "Not reported", r, defaulted)
        add("processing", r["ProcessingMethod"].strip() or "Not reported", r, defaulted)
        add("business_age", r["BusinessAge"].strip() or "Not reported", r, defaulted)
        rt = r["FixedOrVariableInterestInd"].strip()
        add("rate_type", {"F": "Fixed", "V": "Variable"}.get(rt, rt) or "Not reported", r, defaulted)
        add("size_band", _band(r["GrossApproval"]), r, defaulted)
        add("industry_year", (r["NaicsDescription"] or "N/A", d.year), r, defaulted)
        co = r["ChargeOffDate"].strip()
        if defaulted and co:
            cd = _date(co)
            for table, key in (("events_annual", cd.year), ("events_quarterly", f"{cd.year}Q{(cd.month + 2) // 3}")):
                g, gu = _amount(r["GrossApproval"]), _amount(r["SBAGuaranteedApproval"])
                for m, v in (("loans", 1), ("gross", g), ("unguar", g - gu),
                             ("gross_chargeoff", _amount(r["GrossChargeOffAmount"]))):
                    out[(table, key, m)] = out.get((table, key, m), 0) + v
        elif defaulted:
            undated += 1
    out[("events", "undated", "loans")] = undated
    return out


def _payload_value(p, key):
    """The Saved Analysis's own value at an evidence key (data lookup only), or None when it holds none."""
    parts = key.split(".")
    _, h, g = parts[:3]
    blk = p["bank"].get(h, {}).get(g)
    if blk is None:
        return None
    if parts[3] == "total":
        v = blk["total"].get(parts[4])
    else:
        row = next((r for r in blk.get("vintage", []) if str(r.get("year")) == parts[4]), None)
        v = row.get(parts[5]) if row else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def produce(saved, folder, performed_on, performed_by="clean-room producer (offline)", release=None):
    """Recompute and return (ValidationRecord, report) for one Saved Analysis. Refuses (CleanRoomError) when the
    recomputed population does not match the analysis's own population counts."""
    return produce_many([saved], folder, performed_on, performed_by, release)[0]


def produce_many(saved_list, folder, performed_on, performed_by="clean-room producer (offline)", release=None):
    lenders = {}
    for a in saved_list:
        c = a["payload"]["criteria"]
        if not c.get("location_ids"):
            raise CleanRoomError(f"{a['id']}: the analysis records no lender location ids")
        lenders[a["id"]] = (c["bank_name"], c["bank_state"], str(c.get("cert") or ""), set(map(str, c["location_ids"])))
    found, as_of, prints = release or read_release(folder, lenders)
    results = []
    for a in saved_list:
        p, c = a["payload"], a["payload"]["criteria"]
        if as_of.isoformat() != c["as_of"]:
            raise CleanRoomError(f"release is as of {as_of}, the analysis is as of {c['as_of']}")
        pop = found[a["id"]]
        ident = p.get("identity", {})
        counts = {"records": pop["records"], "eligible": len(pop["rows"])}
        if (ident.get("records"), ident.get("eligible")) != (counts["records"], counts["eligible"]) or \
                ident.get("excluded_counts", {}) != pop["excluded"]:
            raise CleanRoomError(f"{a['id']}: population differs from the analysis (analysis {ident.get('records')} "
                                 f"records / {ident.get('eligible')} eligible / {ident.get('excluded_counts')}; "
                                 f"recomputed {counts} / {pop['excluded']})")
        values = recompute(pop["rows"], as_of, int(c["threshold"]))
        held = {k: v for k, v in values.items() if _payload_value(p, k) is not None}
        if not held:
            raise CleanRoomError(f"{a['id']}: none of the recomputed measures is held by the analysis")
        population = (f"{c['bank_name']}, {c['bank_state']}, FDIC {c.get('cert') or 'none'}, LocationID "
                      f"{', '.join(sorted(map(str, c['location_ids'])))}: {counts['records']} records, "
                      f"{counts['eligible']} eligible; excluded {json.dumps(pop['excluded'], sort_keys=True)}; "
                      f"threshold ${int(c['threshold']):,}")
        rec = VAL.make_validation_record(
            SYSTEM, a["id"], _sha(p), method="clean-room recomputation from the SBA 7(a) FOIA CSV files; no "
            "application, generator or workbook code used; definitions restated from FalconBridge's documentation",
            performed_on=performed_on, performed_by=performed_by,
            reference="intelligence/cleanroom_foia.py (module docstring: definitions)", values=held,
            input_fingerprints=prints, as_of=c["as_of"], population=population,
            methodology_version=METHODOLOGY_VERSION)
        agree = disagree = 0
        diffs = []
        for k, v in held.items():
            mine = _payload_value(p, k)
            if VAL.agrees(rec, v, mine):
                agree += 1
            else:
                disagree += 1
                diffs.append({"metric_key": k, "analysis": mine, "recomputed": v})
        not_held = sorted(set(values) - set(held))
        results.append((rec, {"analysis_id": a["id"], "bank": c["bank_name"], "record_id": rec.record_id,
                              "values": len(held), "agree": agree, "disagree": disagree, "differences": diffs,
                              "recomputed_but_not_in_analysis": not_held, "population": population}))
    return results


def _sha(payload):
    # the same canonical fingerprint as intelligence.evidence.sha256_of (imported lazily to keep this module light)
    from .evidence import sha256_of
    return sha256_of(payload)


# ------------------------------------------------------------------------------------------------ record store
def store_dir(app_data, analysis_id):
    return os.path.join(app_data, "validations", analysis_id)


def save_record(app_data, rec):
    d = store_dir(app_data, rec.source_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, rec.record_id + ".json")
    if os.path.exists(path):
        return path                                    # content-addressed: the same record is already stored
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(VAL.to_json(rec), f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return path


def load_records(app_data, analysis_id):
    """Every stored record of one analysis; an edited or corrupt file raises (it is never silently skipped)."""
    d = store_dir(app_data, analysis_id)
    if not os.path.isdir(d):
        return ()
    out = []
    for name in sorted(os.listdir(d)):
        if name.startswith("VR-") and name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as f:
                rec = VAL.from_json(json.load(f))
            if rec.record_id + ".json" != name or rec.source_id != analysis_id:
                raise VAL.EvidenceError(f"validation record file {name} does not match its content")
            out.append(rec)
    return tuple(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Clean-room validation of Saved Analyses from the SBA FOIA release")
    ap.add_argument("--app-data", required=True)
    ap.add_argument("--release", required=True)
    ap.add_argument("--performed-on", required=True)
    ap.add_argument("analysis_ids", nargs="+")
    a = ap.parse_args(argv)
    saved = []
    for i in a.analysis_ids:
        with open(os.path.join(a.app_data, "analyses", i, "analysis.json"), encoding="utf-8") as f:
            saved.append(json.load(f))
    for rec, rep in produce_many(saved, a.release, a.performed_on):
        save_record(a.app_data, rec)
        print(json.dumps({k: rep[k] for k in ("bank", "record_id", "values", "agree", "disagree")}))
        for d in rep["differences"][:10]:
            print("  DIFFERS", json.dumps(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
