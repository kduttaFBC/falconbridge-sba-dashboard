"""Read the processed historical FOIA files (read-only): lender directory, lender identity resolution,
bank extraction, client-confirmed eligibility exclusions, and the bank-specific sector / subprogram / vintage lists."""
import csv
import hashlib
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import config as C


class SourceError(Exception):
    """The selected FOIA snapshot cannot be used as-is (missing files or fields, mixed AsOfDates)."""


def snapshots():
    """{folder name: folder} for every sub-folder of source_data that holds FOIA files (one per SBA release)."""
    if not C.SOURCE_DIR.exists():
        return {}
    return {d.name: d for d in sorted(C.SOURCE_DIR.iterdir()) if d.is_dir() and any(d.glob(C.SOURCE_GLOB))}


def source_files(folder):
    files = sorted(Path(folder).glob(C.SOURCE_GLOB))
    if not files:
        raise SourceError(f"No historical FOIA files ({C.SOURCE_GLOB}) found in {folder}")
    for path in files:
        f, _, header = _reader(path)
        f.close()
        missing = [x for x in C.REQUIRED_FIELDS if x not in header]
        if missing:
            raise SourceError(f"{path.name} is missing fields {missing}. Run fix_foia.py on the SBA files first.")
    return files


def _reader(path):
    f = open(path, encoding="utf-8", newline="")
    rd = csv.reader(f)
    header = next(rd)
    return f, rd, header


def parse_date(text):
    """SBA dates as published / written by fix_foia.py (YYYY-MM-DD) or as re-saved by Excel (M/D/YYYY)."""
    t = text.strip()
    if "/" in t:
        m, d, y = t.split("/")
        return date(int(y), int(m), int(d))
    return date.fromisoformat(t[:10])


parse_mdy = parse_date   # former name, kept for callers


def norm_id(text):
    """FDIC certificate / SBA LocationID compared without formatting: '0029805' and '29805' are the same ID."""
    t = text.strip()
    return str(int(t)) if t.isdigit() else t


def exclusion_reason(status, disbursement):
    """Client-confirmed population rule. Returns None when the loan is eligible."""
    s = status.strip()
    if s == "":
        return "blank LoanStatus"
    if s == C.CANCELLED_STATUS:
        return "LoanStatus = CANCLD"
    if disbursement.strip() == "":
        return "blank FirstDisbursementDate"
    return None


# ---------------------------------------------------------------- lender directory
def _files_signature(files):
    return [[p.name, p.stat().st_size, int(p.stat().st_mtime)] for p in files]


def _directory_from_rows(rows):
    out = {}
    for name, state, lenders in rows:
        ls = [dict(zip(("cert", "location_id", "records", "eligible", "city", "street"), x)) for x in lenders]
        out[(name, state)] = {"records": sum(x["records"] for x in ls), "eligible": sum(x["eligible"] for x in ls),
                              "location_ids": len({x["location_id"] for x in ls}),
                              "certs": sorted({x["cert"] for x in ls}), "lenders": ls}
    return out


def lender_directory(folder, progress=print):
    """Directory of every (BankName, BankState) in the snapshot, with the lender identity evidence behind it:
    {(name, state): {"records", "eligible", "location_ids", "certs", "lenders": [{cert, location_id, records,
    eligible, city, street}]}}, plus the snapshot's AsOfDate values (returned separately).
    Cached in output/.cache, keyed by the snapshot folder and source file names/sizes/modification times."""
    files = source_files(folder)
    sig = [str(Path(folder).resolve())] + _files_signature(files)
    cache = C.CACHE_DIR / f"lender_directory_{Path(folder).name}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get("signature") == sig and data.get("version") == 2:
            return _directory_from_rows(data["rows"]), data["as_of_values"]
    progress("Building lender directory from the historical files (first run per release, ~1 minute)...")
    d = defaultdict(lambda: {"records": 0, "eligible": 0, "city": defaultdict(int), "street": defaultdict(int)})
    as_of = defaultdict(int)
    for path in files:
        f, rd, h = _reader(path)
        i_n, i_s, i_l = h.index(C.F_BANK_NAME), h.index(C.F_BANK_STATE), h.index(C.F_LOCATION_ID)
        i_st, i_d = h.index(C.F_LOAN_STATUS), h.index(C.F_FIRST_DISBURSEMENT)
        i_c, i_city, i_str, i_a = h.index(C.F_FDIC_CERT), h.index(C.F_BANK_CITY), h.index(C.F_BANK_STREET), h.index(C.F_AS_OF)
        with f:
            for row in rd:
                as_of[row[i_a].strip()] += 1
                e = d[(row[i_n], row[i_s], norm_id(row[i_c]), norm_id(row[i_l]))]
                e["records"] += 1
                e["city"][row[i_city]] += 1
                e["street"][row[i_str]] += 1
                if exclusion_reason(row[i_st], row[i_d]) is None:
                    e["eligible"] += 1
    grouped = defaultdict(list)
    for (name, state, cert, loc), v in d.items():
        top = lambda c: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0]
        grouped[(name, state)].append([cert, loc, v["records"], v["eligible"], top(v["city"]), top(v["street"])])
    rows = [[k[0], k[1], sorted(v)] for k, v in grouped.items()]
    as_of_values = sorted(as_of)
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"version": 2, "signature": sig, "rows": rows, "as_of_values": as_of_values}))
    return _directory_from_rows(rows), as_of_values


def snapshot_as_of(as_of_values, expected=None):
    """The snapshot's single AsOfDate; refuse mixed releases or a release that is not the selected period."""
    dates = sorted({parse_date(v) for v in as_of_values if v.strip()})
    if len(dates) != 1 or any(not v.strip() for v in as_of_values):
        raise SourceError(f"The FOIA files mix AsOfDate values {as_of_values}; use files from one SBA release only.")
    if expected is not None and dates[0] != expected:
        raise SourceError(f"The FOIA files are as of {dates[0]:%m/%d/%Y}, not the selected period {expected:%m/%d/%Y}.")
    return dates[0]


# ---------------------------------------------------------------- lender identity
class LenderSelectionError(Exception):
    """The bank could not be identified without merging different lenders; the message lists the evidence."""


def _describe(lenders):
    by_cert = defaultdict(list)
    for x in lenders:
        by_cert[x["cert"] or "(no FDIC cert)"].append(x)
    lines = []
    for cert, xs in sorted(by_cert.items()):
        locs = ", ".join(f"{x['location_id']} ({x['city']}, {x['eligible']:,} eligible)" for x in xs)
        lines.append(f"    FDIC cert {cert}: SBA LocationID {locs}")
    return "\n".join(lines)


def resolve_lender(directory, cert=None, bank_name=None, bank_state=None, location_ids=None):
    """Resolve the analyst's selection to one lender, never merging different lenders.

    Rules (records sharing a BankName + BankState are combined only when they are the same lender):
    * one FDIC certificate across the BankName + BankState -> its LocationIDs are combined (approved rule);
    * several certificates (or certificate and non-certificate records) -> refused unless the analyst gives
      --fdic-cert; the population is then restricted to that certificate;
    * no certificate and several LocationIDs -> refused unless the analyst lists the LocationIDs to include;
    * a certificate published under several BankName + BankState combinations -> refused unless the analyst
      also gives the BankName and BankState to use.
    Returns {"bank_name", "bank_state", "cert", "location_ids", "status", "excluded", "candidates"}."""
    cert = norm_id(cert) if cert else None
    locs = {norm_id(x) for x in location_ids} if location_ids else None
    if bank_name is None or bank_state is None:
        if not cert:
            raise LenderSelectionError("Give an FDIC certificate, or a Bank Name and Bank State.")
        groups = [k for k, v in directory.items() if cert in v["certs"]]
        if not groups:
            raise LenderSelectionError(f"FDIC certificate {cert} does not appear in this FOIA release.")
        if len(groups) > 1:
            listing = "\n".join(f"  {n}, {s}:\n{_describe([x for x in directory[(n, s)]['lenders'] if x['cert'] == cert])}"
                                 for n, s in sorted(groups))
            raise LenderSelectionError(
                f"FDIC certificate {cert} is published under {len(groups)} Bank Name + Bank State combinations:\n"
                f"{listing}\nRe-run with --bank-name and --bank-state to choose one of them.")
        bank_name, bank_state = groups[0]
    if (bank_name, bank_state) not in directory:
        raise LenderSelectionError(f"'{bank_name}, {bank_state}' does not appear in this FOIA release.")
    lenders = directory[(bank_name, bank_state)]["lenders"]
    chosen = lenders
    status = "UNIQUE"
    if cert:
        chosen = [x for x in lenders if x["cert"] == cert]
        if not chosen:
            raise LenderSelectionError(f"FDIC certificate {cert} has no records under '{bank_name}, {bank_state}'.\n"
                                       f"  Records under that name:\n{_describe(lenders)}")
        if len(chosen) < len(lenders):
            status = "RESTRICTED_TO_FDIC_CERT"
    elif len({x["cert"] for x in lenders}) > 1:
        raise LenderSelectionError(
            f"'{bank_name}, {bank_state}' contains records of different lenders:\n{_describe(lenders)}\n"
            f"They are not combined. Re-run with --fdic-cert to choose the lender.")
    if locs is not None:
        unknown = locs - {x["location_id"] for x in chosen}
        if unknown:
            raise LenderSelectionError(f"LocationID(s) {sorted(unknown)} are not records of the selected lender:\n"
                                       f"{_describe(chosen)}")
        if len(locs) < len(chosen):
            status = "RESTRICTED_TO_LOCATION_IDS"
        chosen = [x for x in chosen if x["location_id"] in locs]
    elif not chosen[0]["cert"] and len(chosen) > 1:
        raise LenderSelectionError(
            f"'{bank_name}, {bank_state}' has no FDIC certificate and {len(chosen)} SBA LocationIDs; nothing shows they "
            f"are the same lender:\n{_describe(chosen)}\nRe-run with --location-id for each LocationID to include.")
    excluded = [x for x in lenders if x not in chosen]
    return {"bank_name": bank_name, "bank_state": bank_state, "cert": chosen[0]["cert"],
            "location_ids": sorted({x["location_id"] for x in chosen}), "status": status,
            "selected": chosen, "excluded": excluded}


# ---------------------------------------------------------------- bank extraction
def sha256(path, chunk=1 << 22):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def extract_bank(bank_name, bank_state, folder, cert=None, location_ids=None):
    """Return the selected bank's eligible rows (as dicts keyed by field name) and exclusion counts.
    Selection is an exact match on BankName and BankState as stored in the source, restricted to the resolved
    lender's FDIC certificate and LocationIDs (see resolve_lender), so other lenders sharing the name are excluded."""
    eligible, excluded = [], defaultdict(int)
    location_ids_seen, total, other_lenders = set(), 0, 0
    want_locs = {norm_id(x) for x in location_ids} if location_ids else None
    for path in source_files(folder):
        f, rd, h = _reader(path)
        i_n, i_s, i_l = h.index(C.F_BANK_NAME), h.index(C.F_BANK_STATE), h.index(C.F_LOCATION_ID)
        i_st, i_d = h.index(C.F_LOAN_STATUS), h.index(C.F_FIRST_DISBURSEMENT)
        i_c = h.index(C.F_FDIC_CERT)
        with f:
            for row in rd:
                if row[i_n] != bank_name or row[i_s] != bank_state:
                    continue
                if (cert is not None and norm_id(row[i_c]) != cert) or \
                        (want_locs is not None and norm_id(row[i_l]) not in want_locs):
                    other_lenders += 1
                    continue
                total += 1
                location_ids_seen.add(row[i_l])
                reason = exclusion_reason(row[i_st], row[i_d])
                if reason:
                    excluded[reason] += 1
                else:
                    eligible.append(dict(zip(h, row)))
    return {"rows": eligible, "total_records": total, "excluded": dict(excluded),
            "location_ids": sorted(location_ids_seen), "other_lender_records": other_lenders}


# ---------------------------------------------------------------- bank-specific lists
def _ranked(rows, field, exclude_value=None):
    """Unique values of `field` with loan count and summed GrossApproval, ordered by summed
    GrossApproval descending, ties alphabetical (client confirmed). Exact stored values."""
    agg = defaultdict(lambda: [0, 0.0])
    for r in rows:
        v = r[field]
        if exclude_value is not None and v == exclude_value:
            continue
        agg[v][0] += 1
        agg[v][1] += float(r[C.F_BALANCE] or 0)
    return [(v, n, s) for v, (n, s) in sorted(agg.items(), key=lambda kv: (-kv[1][1], kv[0]))]


def build_lists(rows):
    sectors = _ranked(rows, C.F_SECTOR, exclude_value=C.NA_SECTOR)
    na = [r for r in rows if r[C.F_SECTOR] == C.NA_SECTOR]
    na_entry = (C.NA_SECTOR, len(na), sum(float(r[C.F_BALANCE] or 0) for r in na)) if na else None
    subprograms = _ranked(rows, C.F_SUBPROGRAM)
    vintages = sorted({parse_date(r[C.F_FIRST_DISBURSEMENT]).year for r in rows})
    approval_fys = [int(r[C.F_APPROVAL_FY]) for r in rows if r[C.F_APPROVAL_FY].strip()]
    as_of = sorted({r[C.F_AS_OF] for r in rows})
    return {"sectors": sectors, "na": na_entry, "subprograms": subprograms, "vintages": vintages,
            "first_approval_fy": min(approval_fys) if approval_fys else None,
            "as_of": parse_date(as_of[-1]) if as_of else None, "as_of_values": as_of,
            "eligible_count": len(rows), "eligible_balance": sum(float(r[C.F_BALANCE] or 0) for r in rows)}
