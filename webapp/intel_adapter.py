"""FOIA application adapter for the intelligence layer (M1): Saved Analysis -> evidence pack.

Reads a Saved Analysis exactly as frozen (webapp/saved.py: criteria, payload, calculation_version) and states each
value it already holds as an evidence record. It never calls the calculation engine (webapp/analysis.py is not
imported), never recomputes a figure, never writes anything and never mutates its input. Missing or inconsistent
input is rejected (EvidenceError), not repaired.

Validation labels (M2): a value is INDEPENDENTLY_VERIFIED only when a validation record bound to THIS Saved Analysis
(id + payload SHA-256) holds an independently recomputed value that agrees with it. Bank identity, release and
calculation version alone never establish independent validation; without a record, template-defined measures are
RECONCILED_TO_TEMPLATE. Methodology approval is a separate field and is never granted by a validation record.
"""
import copy
import re

from intelligence import validation as VAL
from intelligence.evidence import (Classification as CL, EvidenceError, EvidencePack, EvidenceRecord, Kind,
                                   Methodology as ME, Source, ValidationStatus as VS, make_id, sha256_of,
                                   validate_pack)

from . import insights as IN

ADAPTER_VERSION = "foia-evidence-1.2"           # 1.2: NAICS code text records for industry rows
SMALL_SAMPLE = f"Small sample: fewer than {IN.MIN_INDUSTRY_LOANS} bank loans"
LIMITED_DATA = f"Limited data: fewer than {IN.MIN_LOANS} bank loans"
SYSTEM = "foia-saved-analysis"
REQUIRED_SAVED = ("id", "created", "criteria", "payload")
REQUIRED_PAYLOAD = ("criteria", "identity", "bank", "benchmark", "release", "calculation_version")
OPTIONAL_PAYLOAD = ("comparison", "industry_comparison", "concentration", "chgoff_by_year", "status", "default_line",
                    "benchmark_population", "seasoning", "partial_year", "insights", "source_notes", "glossary",
                    "group_titles", "static_pool")
# Engine-level validation (FalconBridge_FOIA_Calculation_Engine_Validation_Readiness_2026-11-05 and the D-1 reports):
# INFORMATION ONLY. It shows the engine's definitions were independently verified for these banks and this release;
# it is not evidence that any particular Saved Analysis was validated, so it never sets INDEPENDENTLY_VERIFIED.
VERIFIED_RELEASES = {"20260630"}
VERIFIED_CERTS = {"58310", "35323", "27417"}           # US Metro, FinWise, Shoreham
# Calculation versions whose payloads were shown identical and whose engine was validated (stage1-1.3 to 1.5); any
# other version is UNVERIFIED.
VERIFIED_VERSIONS = {"stage1-1.3", "stage1-1.4", "stage1-1.5"}
CORRECTIONS_FROM = "stage1-1.5"                       # D-1/D-1c/D-1d workbook corrections in the linked workbook

# Bank measures: unit, neutral label, FalconBridge workbook column whose definition the engine reproduces (or None),
# and whether the measure was independently recomputed from source data in the validation assessment.
BANK = {
    "loans": ("count", "Number of loans originated (excluding COMMIT)", "PerformanceSummary!K", True),
    "orig": ("usd", "Original gross approval amount", "PerformanceSummary!M", True),
    "avg": ("usd", "Average original amount per loan", "PerformanceSummary!O", True),
    "active": ("count", "Estimated number of loans still active (status group EXEMPT)", "PerformanceSummary!L", True),
    "active_orig": ("usd", "Original amount of loans still active (not a current balance)", "PerformanceSummary!P", True),
    "age": ("months", "Average age of active loans", "PerformanceSummary!Q", True),
    "chgoff": ("count", "Number of loans charged off", "PerformanceSummary!R", True),
    "cum_def": ("ratio", "% Cum. Defaulted: original amount of charged-off loans / original amount",
                "PerformanceSummary!S", True),
    "severity": ("ratio", "% Loss Severity: cumulative loss / cumulative defaulted (pooled)", "PerformanceSummary!T",
                 True),
    "cum_loss": ("ratio", "% Cum. Loss: gross charge-offs / original amount", "PerformanceSummary!U", True),
    "purch_pct": ("ratio", "PURCH(NOT C/O) original amount / original amount", "PerformanceSummary!W", False),
    "share": ("ratio", "Share of the bank's original amount (industry row)", "PerformanceSummary!N", True),
    "guar": ("usd", "SBA-guaranteed approval amount", None, False),
    "unguar": ("usd", "Unguaranteed approval amount", None, False),
    "chgoff_orig": ("usd", "Original amount of charged-off loans", None, False),
    "chgoff_amt": ("usd", "Gross charge-off amount", None, False),
    "pif_orig": ("usd", "Original amount of loans paid in full", None, False),
    "pif_pct": ("ratio", "Paid-in-full original amount / original amount", None, False),
    "purch_orig": ("usd", "Original amount of PURCH(NOT C/O) loans", None, False),
    # industry rows only: the code the generated workbook shows (Sector_Pivot "Min of NaicsCode"), read unchanged
    "naics": ("text", "NAICS code shown in the FalconBridge workbook for this industry", "Sector_Pivot (Min of NaicsCode)",
              False),
}
NAICS_NOTE = ("Workbook convention: the lowest NAICS code among the bank's loans in this industry (Sector_Pivot "
              "'Min of NaicsCode'); exact when those loans carry a single code")
BENCH = {
    "loans": ("count", "SBA benchmark: number of loans"), "orig": ("usd", "SBA benchmark: original amount"),
    "avg": ("usd", "SBA benchmark: average original amount"),
    "active": ("count", "SBA benchmark: estimated loans outstanding (no paid-in-full or charge-off date)"),
    "active_orig": ("usd", "SBA benchmark: original amount of loans outstanding"),
    "chgoff": ("count", "SBA benchmark: loans with a charge-off date"),
    "cum_def": ("ratio", "SBA benchmark: % Cum. Defaulted"),
    "severity": ("ratio", "SBA benchmark: % Loss Severity (default-count weighted across industries)"),
    "cum_loss": ("ratio", "SBA benchmark: % Cum. Loss"), "purch_pct": ("ratio", "SBA benchmark: PURCH(NOT C/O) %"),
}
BENCH_BASIS = "independent recompute from FOIA + oracle (release 2026-06-30, 3 banks); method recovered from Orig_v2"


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _version_short(v):
    m = re.search(r"stage1-\d+\.\d+", v or "")
    return m.group(0) if m else ""


def _version_tuple(short):
    m = re.fullmatch(r"stage(\d+)-(\d+)\.(\d+)", short or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _pointer(*parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


class _Builder:
    def __init__(self, saved, payload_sha, validated):
        p = saved["payload"]
        c = p["criteria"]
        self.saved, self.p, self.sha = saved, p, payload_sha
        self.sid, self.as_of, self.period = saved["id"], c["as_of"], c["period"]
        self.bank = (c["bank_name"], c["bank_state"], str(c.get("cert") or ""))
        self.version = p["calculation_version"]
        self.vshort = _version_short(self.version)
        self.records, self.na = [], 0
        self.engine_scope = (self.period in VERIFIED_RELEASES and self.bank[2] in VERIFIED_CERTS
                             and self.vshort in VERIFIED_VERSIONS)
        self.known_version = self.vshort in VERIFIED_VERSIONS
        self.validated = validated            # metric_key -> (validation record, independently recomputed value)
        self.used, self.disagreed = set(), []

    def _add(self, kind, key, label, value, unit, vs, me, cl, path="", basis="", ref="", rule="", cites=(), lim=(),
             numeric=False):
        if kind is Kind.FACT and not _num(value) and not isinstance(value, (bool, str)):
            raise EvidenceError(f"{key}: unexpected value {value!r} at {path}")
        rec = EvidenceRecord(
            evidence_id=make_id(self.sid, kind, key), kind=kind, metric_key=key, label=label, value=value, unit=unit,
            as_of=self.as_of, bank=self.bank, calculation_version=self.version, validation_status=vs, methodology=me,
            classification=cl, source=Source(SYSTEM, self.sid, self.sha, path, self.saved["created"]),
            numeric_basis=basis, numerically_verified=numeric, definition_ref=ref or "", rule=rule,
            derived_from=tuple(cites), limitations=tuple(lim))
        self.records.append(rec)
        return rec.evidence_id

    def _value(self, evidence_id):
        return next(r.value for r in self.records if r.evidence_id == evidence_id)

    def _numeric(self, key, value):
        """(True, basis) if an applicable validation record confirms the value, (False, basis) if it contradicts it,
        (None, '') if no record covers it."""
        hit = self.validated.get(key)
        if not hit:
            return None, ""
        rec, expected = hit
        self.used.add(rec.record_id)
        if VAL.agrees(rec, expected, value):
            return True, f"validation record {rec.record_id} ({rec.method}; {rec.performed_on})"
        self.disagreed.append(key)
        return False, f"validation record {rec.record_id} DISAGREES: independently recomputed {expected!r}"

    def _bank_status(self, metric, history, group, key, value):
        """(validation status, methodology, numeric basis, numerically verified, extra limitations)."""
        ref = BANK[metric][2]
        numeric, basis = self._numeric(key, value)
        template = history == "full" and group == "total" and ref is not None
        me = ME.UNCONFIRMED_WORKBOOK_METHOD if template else ME.APPLICATION_RULE
        if not self.known_version:
            return VS.UNVERIFIED, me, "calculation version outside the validated range", False, ()
        if numeric is False:
            return VS.UNVERIFIED, me, basis, False, ("Independent validation disagrees with this value",)
        if not template:
            # FY2000+ like-for-like basis, the above/below-threshold split and non-template tables are application
            # definitions: whatever their numeric verification, their methodology awaits FalconBridge's approval
            return VS.PENDING_CLIENT_APPROVAL, me, basis or "engine output (oracle-reconciled engine definitions)", \
                bool(numeric), ()
        if numeric:
            return VS.INDEPENDENTLY_VERIFIED, me, basis, True, ()
        scope = (" Engine definitions were independently verified for this bank and release (2026-11-05 assessment); "
                 "this Saved Analysis has no validation record.") if self.engine_scope else ""
        return VS.RECONCILED_TO_TEMPLATE, me, "engine reproduces FalconBridge template formulas." + scope, False, ()

    def bank_blocks(self):
        ids = {}
        for h, groups in self.p["bank"].items():
            for g, blk in groups.items():
                for dim in ("total", "vintage", "industry", "subprogram"):
                    rows = [blk[dim]] if dim == "total" else blk.get(dim, [])
                    for i, row in enumerate(rows):
                        if dim != "total" and not row.get("loans"):
                            continue
                        key_part = {"total": "total", "vintage": f"vintage.{row.get('year')}",
                                    "industry": f"industry.{row.get('name')}",
                                    "subprogram": f"subprogram.{row.get('name')}"}[dim]
                        cl = CL.FOIA_AGGREGATE_CLIENT_CORRECTED if dim in ("industry", "subprogram") else \
                            CL.PUBLIC_FOIA_AGGREGATE
                        n = row.get("loans") or 0
                        size = ((LIMITED_DATA,) if dim == "total" and 0 < n < IN.MIN_LOANS else
                                (SMALL_SAMPLE,) if dim != "total" and 0 < n < IN.MIN_INDUSTRY_LOANS else ())
                        for m, (unit, label, ref, _) in BANK.items():
                            if m not in row:
                                continue
                            v = row[m]
                            if m == "naics":                 # text, industry rows only; blank codes are not stated
                                if dim != "industry" or not isinstance(v, str) or not v.strip():
                                    continue
                            elif not _num(v):
                                self.na += 1
                                continue
                            key = f"bank.{h}.{g}.{key_part}.{m}"
                            vs, me, basis, numeric, extra = self._bank_status(m, h, g, key, v)
                            lim = (("Original amounts, not current balances",) if m in ("active_orig", "orig") else ()) \
                                + ((NAICS_NOTE,) if m == "naics" else ()) + size + extra
                            path = _pointer("bank", h, g, dim, m) if dim == "total" else _pointer("bank", h, g, dim, i, m)
                            ids[key] = self._add(Kind.FACT, key, label, v, unit, vs, me, cl, path, basis, ref or "",
                                                 lim=lim, numeric=numeric)
        return ids

    def benchmark(self):
        ids = {}
        for g, b in self.p["benchmark"].items():
            for dim in ("total", "vintage", "sector"):
                rows = [b[dim]] if dim == "total" else b.get(dim, [])
                for i, row in enumerate(rows):
                    if dim != "total" and not row.get("loans"):
                        continue
                    kp = {"total": "total", "vintage": f"vintage.{row.get('year')}",
                          "sector": f"industry.{row.get('name')}"}[dim]
                    for m, (unit, label) in BENCH.items():
                        v = row.get(m)
                        if not _num(v):
                            if m in row:
                                self.na += 1
                            continue
                        key = f"benchmark.{g}.{kp}.{m}"
                        path = _pointer("benchmark", g, dim, m) if dim == "total" else _pointer("benchmark", g, dim, i, m)
                        numeric, basis = self._numeric(key, v)
                        vs = VS.PRELIMINARY_METHODOLOGY if self.known_version and numeric is not False else VS.UNVERIFIED
                        extra = ("Independent validation disagrees with this value",) if numeric is False else ()
                        ids[key] = self._add(
                            Kind.FACT, key, label, v, unit, vs, ME.CLIENT_DIRECTED_PRELIMINARY, CL.CLIENT_METHOD_DERIVED,
                            path, basis or BENCH_BASIS + " (engine level; no validation record for this analysis)",
                            "PerformanceSummary!X:CE (industry-matched)",
                            lim=("PRELIMINARY: FalconBridge has not confirmed the benchmark methodology",
                                 "Benchmark defaults are date-based; the bank's are status-based") + extra,
                            numeric=bool(numeric))
        for i, d in enumerate(self.p.get("default_line", [])):           # Non-CRE / CRE: only here in older payloads
            if d.get("group") in ("noncre", "cre"):
                for m, field in (("loans", "sba_loans"), ("cum_def", "sba_cum_def")):
                    key = f"benchmark.{d['group']}.total.{m}"
                    if key in ids:                       # newer payloads hold these groups in 'benchmark' as well
                        stored = self._value(ids[key])
                        if _num(d.get(field)) and abs(d[field] - stored) > 1e-12 * max(1.0, abs(stored)):
                            raise EvidenceError(f"inconsistent payload: default_line {d['group']} {m} differs from "
                                                f"benchmark.{d['group']}")
                        continue
                    if _num(d.get(field)):
                        ids[key] = self._add(Kind.FACT, key, BENCH[m][1].replace("SBA benchmark", f"SBA benchmark "
                                             f"({'term > 10 yrs' if d['group'] == 'cre' else 'term <= 10 yrs'})"),
                                             d[field], BENCH[m][0], VS.PRELIMINARY_METHODOLOGY if self.known_version
                                             else VS.UNVERIFIED, ME.CLIENT_DIRECTED_PRELIMINARY,
                                             CL.CLIENT_METHOD_DERIVED, _pointer("default_line", i, field), BENCH_BASIS,
                                             lim=("PRELIMINARY: benchmark methodology not confirmed",
                                                  "'CRE' is an original-term proxy (> 120 months)"))
        return ids

    def derived(self, bank_ids, bench_ids):
        p = self.p
        vs_pre = VS.PRELIMINARY_METHODOLOGY if self.known_version else VS.UNVERIFIED
        vs_app = VS.PENDING_CLIENT_APPROVAL if self.known_version else VS.UNVERIFIED
        for g, comp in p.get("comparison", {}).items():
            for m, d in comp.items():
                b, s = bank_ids.get(f"bank.fy2000.{g}.total.{m}"), bench_ids.get(f"benchmark.{g}.total.{m}")
                if _num(d.get("diff")) and b and s:
                    self._add(Kind.DERIVED, f"compare.{g}.total.{m}.diff", f"Bank (FY2000+) minus SBA benchmark: {m}",
                              d["diff"], BANK[m][0],
                              vs_pre, ME.CLIENT_DIRECTED_PRELIMINARY, CL.CLIENT_METHOD_DERIVED,
                              rule="difference of the two cited values (as stored in the Saved Analysis)",
                              cites=(b, s), lim=("Not adjusted for vintage, size, term or industry mix; see seasoning",
                                                 "Benchmark PRELIMINARY"))
        for g, rows in p.get("industry_comparison", {}).items():
            for row in rows:
                for m in ("cum_def", "cum_loss"):
                    b = bank_ids.get(f"bank.fy2000.{g}.industry.{row['name']}.{m}")
                    s = bench_ids.get(f"benchmark.{g}.industry.{row['name']}.{m}")
                    v = row.get(f"diff_{m}")
                    if _num(v) and b and s:
                        lim = ["Benchmark PRELIMINARY"]
                        if row.get("loans", 0) < IN.MIN_INDUSTRY_LOANS:
                            lim.append(f"Small sample: fewer than {IN.MIN_INDUSTRY_LOANS} bank loans")
                        self._add(Kind.DERIVED, f"compare.{g}.industry.{row['name']}.{m}.diff",
                                  f"Bank (FY2000+) minus SBA benchmark, industry: {m}", v, "ratio", vs_pre,
                                  ME.CLIENT_DIRECTED_PRELIMINARY, CL.CLIENT_METHOD_DERIVED,
                                  rule="difference of the two cited values (as stored in the Saved Analysis)",
                                  cites=(b, s), lim=tuple(lim))
        for g, c in p.get("concentration", {}).items():
            cites = tuple(i for k, i in bank_ids.items() if k.startswith(f"bank.full.{g}.industry.") and
                          k.endswith(".share"))
            for m in ("top1", "top5", "top10", "industries"):
                if _num(c.get(m)) and cites:
                    self._add(Kind.DERIVED, f"concentration.{g}.{m}",
                              {"top1": "Largest industry share of original amount", "top5": "Top-5 industries' share",
                               "top10": "Top-10 industries' share", "industries": "Industries with loans"}[m],
                              c[m], "count" if m == "industries" else "ratio", vs_app, ME.APPLICATION_RULE,
                              CL.FOIA_AGGREGATE_CLIENT_CORRECTED, rule="sum of the largest cited industry shares",
                              cites=cites)
        sz = p.get("seasoning")
        if sz:
            for i, band in enumerate(sz.get("bands", [])):
                for side in ("bank", "sba"):
                    if _num(band.get(f"{side}_share")):
                        self._add(Kind.DERIVED, f"seasoning.{band['from']}-{band['to']}.{side}_share",
                                  f"{'Bank (full history)' if side == 'bank' else 'SBA benchmark'} share of original "
                                  f"amount disbursed {band['from']}-{band['to']}", band[f"{side}_share"], "ratio",
                                  vs_app if side == "bank" else vs_pre,
                                  ME.APPLICATION_RULE, CL.APPLICATION_DERIVED if side == "bank" else CL.CLIENT_METHOD_DERIVED,
                                  rule="portfolio-age band (insights.RULES), stored in the Saved Analysis",
                                  cites=tuple(i2 for k, i2 in (bank_ids if side == "bank" else {}).items()
                                              if k == "bank.full.total.total.orig"),
                                  lim=("Application banding rule, not a FalconBridge measure",))

    def limitations(self, bank_ids):
        p, out = self.p, []
        vs_app = VS.PENDING_CLIENT_APPROVAL if self.known_version else VS.UNVERIFIED
        for g in p["bank"].get("full", {}):
            full, fy = p["bank"]["full"][g], p["bank"].get("fy2000", {}).get(g)
            chg = p.get("chgoff_by_year", {}).get(g, {}).get("years", [])
            if fy is None:
                continue
            s = IN.sufficiency(full, fy, chg)                                 # existing presentation rule, no new math
            cite = bank_ids.get(f"bank.fy2000.{g}.total.loans") or bank_ids.get(f"bank.full.{g}.total.loans")
            for flag, label in (("limited", f"Fewer than {IN.MIN_LOANS} bank loans in the FY2000+ comparison"),
                                ("limited_full", f"Fewer than {IN.MIN_LOANS} bank loans (full history)"),
                                ("comparison", "A bank-vs-benchmark comparison is possible (FY2000+ loans > 0)")):
                self._add(Kind.DERIVED, f"limitation.{g}.{flag}", label, bool(s[flag]), "flag", vs_app,
                          ME.APPLICATION_RULE, CL.APPLICATION_DERIVED, rule="insights.sufficiency",
                          cites=(cite,) if cite else ())
            small = [r["name"] for r in full["industry"] if 0 < r["loans"] < IN.MIN_INDUSTRY_LOANS]
            self._add(Kind.DERIVED, f"limitation.{g}.small_sample_industries", f"Industries with fewer than "
                      f"{IN.MIN_INDUSTRY_LOANS} bank loans (rates can move by many points with one loan)", len(small),
                      "count", vs_app, ME.APPLICATION_RULE, CL.APPLICATION_DERIVED, rule="insights.MIN_INDUSTRY_LOANS",
                      cites=tuple(bank_ids[k] for k in (f"bank.full.{g}.industry.{n}.loans" for n in small)
                                  if k in bank_ids))
        py = p.get("partial_year")
        if py:
            self._add(Kind.DERIVED, "limitation.partial_year", f"{py['year']} is a partial year (loans first disbursed "
                      f"through {py['through']})", py["year"], "year", vs_app, ME.APPLICATION_RULE,
                      CL.APPLICATION_DERIVED, rule="reporting date before December 31")
        gaps = p.get("benchmark_population", {}).get("definition_gaps", {})
        for name, n in sorted(gaps.items()):
            if _num(n):
                self._add(Kind.DERIVED, f"limitation.benchmark_definition_gap.{name}", f"Benchmark vs bank definition "
                          f"difference: {name}", n, "count", VS.PRELIMINARY_METHODOLOGY if self.known_version else
                          VS.UNVERIFIED, ME.CLIENT_DIRECTED_PRELIMINARY, CL.CLIENT_METHOD_DERIVED,
                          rule="count stored in the Saved Analysis")
        return out


def payload_fingerprint(saved):
    return sha256_of(saved["payload"])


def extract(saved, validations=()):
    """Saved Analysis (dict as stored by webapp/saved.py) -> validated EvidencePack. Input is never modified.
    `validations`: ValidationRecords; only those bound to this exact Saved Analysis and payload are applied (an altered
    record raises EvidenceError; a record for another analysis or payload is ignored and reported in the header)."""
    if not isinstance(saved, dict):
        raise EvidenceError("a Saved Analysis record (dict) is required")
    missing = [k for k in REQUIRED_SAVED if not saved.get(k)]
    if missing:
        raise EvidenceError(f"Saved Analysis is missing {missing}")
    p = saved["payload"]
    missing = [k for k in REQUIRED_PAYLOAD if k not in p]
    if missing:
        raise EvidenceError(f"Saved Analysis payload is missing required sections {missing}")
    if saved["criteria"] != p["criteria"]:
        raise EvidenceError("Saved Analysis criteria differ from the payload's criteria")
    for k in ("as_of", "period", "bank_name", "bank_state"):
        if not p["criteria"].get(k):
            raise EvidenceError(f"payload criteria missing {k}")
    if not isinstance(p["calculation_version"], str) or not p["calculation_version"]:
        raise EvidenceError("payload has no calculation version")
    _check_consistency(p)
    before = payload_fingerprint(saved)
    validated, ignored = {}, []
    for rec in validations:
        VAL.check_integrity(rec)                        # altered records are refused outright
        why = VAL.applies_to(rec, SYSTEM, saved["id"], before, p["criteria"].get("as_of"))
        if why:
            ignored.append({"record_id": rec.record_id, "reason": why})
            continue
        for key, value in rec.values:
            validated[key] = (rec, value)
    work = copy.deepcopy(saved)                         # extraction reads a private copy; the caller's object is untouched
    b = _Builder(work, before, validated)
    bank_ids = b.bank_blocks()
    bench_ids = b.benchmark()
    b.derived(bank_ids, bench_ids)
    b.limitations(bank_ids)
    if payload_fingerprint(saved) != before:
        raise EvidenceError("Saved Analysis changed during extraction")
    present = [k for k in OPTIONAL_PAYLOAD if k in p]
    lim = ["Provenance: the Saved Analysis records the release folder and file names but not their SHA-256, the "
           "template hash, the code revision or a payload schema version"]
    if "seasoning" not in p:
        lim.append("Older payload schema: no portfolio-age (seasoning) section; not recomputed")
    if "insights" not in p:
        lim.append("Older payload schema: no stored observations; sufficiency flags derived from stored values only")
    if "static_pool" in p:
        lim.append("Static pool present but not extracted (implementation not yet committed or validated for evidence)")
    if p.get("source_notes"):
        lim.append(f"{len(p['source_notes'])} source-data note(s) exist; their text is confidential and not extracted")
    if not b.known_version:
        lim.append(f"Calculation version {p['calculation_version']!r} is outside the validated range "
                   f"{sorted(VERIFIED_VERSIONS)}: all records are UNVERIFIED")
    if not b.used:
        lim.append("No validation record applies to this Saved Analysis: no value is INDEPENDENTLY_VERIFIED")
    unknown = sorted(set(validated) - {r.metric_key for r in b.records})
    if unknown:
        raise EvidenceError(f"validation record covers values this Saved Analysis does not hold: {unknown[:3]}")
    if b.disagreed:
        lim.append(f"Independent validation DISAGREES with {len(b.disagreed)} value(s); they are UNVERIFIED")
    header = {
        "system": SYSTEM, "source_id": saved["id"], "recorded_at": saved["created"], "job_id": saved.get("job_id", ""),
        "owner": saved.get("owner", ""), "payload_sha256": before, "adapter_version": ADAPTER_VERSION,
        "calculation_version": p["calculation_version"], "calculation_version_short": b.vshort,
        "workbook_corrections_D1": bool(_version_tuple(b.vshort)) and
                                   _version_tuple(b.vshort) >= _version_tuple(CORRECTIONS_FROM),
        "as_of": b.as_of, "period": b.period, "bank": list(b.bank),
        "location_ids": list(p["criteria"].get("location_ids") or []), "threshold": p["criteria"].get("threshold"),
        "release_folder": p["release"].get("folder", ""), "release_files": list(p["release"].get("files", [])),
        "sections_present": present, "sections_missing": [k for k in OPTIONAL_PAYLOAD if k not in p],
        "validations_applied": sorted(b.used), "validations_ignored": ignored,
        "values_contradicted_by_validation": list(b.disagreed),
        "engine_validation_scope": {"note": "engine-level information only; never sets INDEPENDENTLY_VERIFIED",
                                    "this_bank_and_release_in_engine_scope": b.engine_scope,
                                    "releases": sorted(VERIFIED_RELEASES), "banks": sorted(VERIFIED_CERTS),
                                    "versions": sorted(VERIFIED_VERSIONS)},
        "not_applicable_values": b.na, "limitations": lim,
    }
    return validate_pack(EvidencePack(header=header, records=tuple(b.records)))


def _check_consistency(p):
    """Integrity of the frozen payload: partitions must add up. Inconsistent evidence is rejected, not repaired."""
    for h, groups in p["bank"].items():
        if "total" not in groups:
            raise EvidenceError(f"bank.{h} has no 'total' group")
        for g, blk in groups.items():
            for need in ("total", "vintage", "industry"):
                if need not in blk:
                    raise EvidenceError(f"bank.{h}.{g} is missing {need}")
            t = blk["total"].get("loans")
            if not isinstance(t, int):
                raise EvidenceError(f"bank.{h}.{g}.total.loans missing")
            for dim in ("vintage", "industry"):
                s = sum(r.get("loans") or 0 for r in blk[dim])
                if s != t:
                    raise EvidenceError(f"inconsistent payload: bank.{h}.{g} {dim} loans sum {s} != total {t}")
        if {"above", "below"} <= set(groups):
            if groups["above"]["total"]["loans"] + groups["below"]["total"]["loans"] != groups["total"]["total"]["loans"]:
                raise EvidenceError(f"inconsistent payload: bank.{h} above + below != total")


def verify(pack, saved):
    """True if `pack` was extracted from exactly this Saved Analysis (same id and unchanged payload)."""
    return pack.header["source_id"] == saved.get("id") and pack.header["payload_sha256"] == payload_fingerprint(saved)


def confidential_terms(saved):
    """Text in a Saved Analysis that must never appear in an exportable claim: FalconBridge display labels, glossary
    definitions, source-note text, group titles, lender address details and internal identifiers.
    Used only for local scanning; never stored as evidence and never exported."""
    p, out = saved.get("payload", {}), set()

    def walk(x):
        if isinstance(x, str):
            out.add(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk([g.get("definition") for g in p.get("glossary") or [] if isinstance(g, dict)])   # FalconBridge's wording
    for section in ("source_notes", "group_titles"):
        walk(p.get(section))
    for h in p.get("bank", {}).values():
        for blk in h.values():
            for row in blk.get("subprogram", []):
                if row.get("label") and row.get("label") != row.get("name"):
                    out.add(row["label"])
    for x in p.get("identity", {}).get("selected", []):
        out.update(str(x.get(k)) for k in ("street", "city") if x.get(k))
    out.update(str(saved.get(k)) for k in ("owner", "job_id", "id") if saved.get(k))
    out.add(str(p.get("release", {}).get("folder", "")))
    return tuple(sorted(t for t in out if len(t.strip()) >= 8))
