"""Benchmark Intelligence, Phase A (approved 2026-11-09, limited): three SBA comparisons for one Saved Analysis.

  B0  the existing, approved (preliminary) industry-matched SBA benchmark, READ UNCHANGED from the Saved Analysis:
      all SBA 7(a) loans approved FY2000+ in the industries where the bank has any eligible loan, pooled at the SBA's
      own industry mix (template AE6 = SUMPRODUCT(industry rate, industry SBA original $) / SUM(SBA original $)).
  B1  INVESTIGATIVE: all SBA 7(a) loans approved FY2000+, every industry, same definitions (not industry-matched).
  B2  INVESTIGATIVE: B0's SBA industry rates weighted by the bank's FY2000+ original-$ industry mix
      (direct standardisation on industry only; not adjusted for vintage, loan size or term).

Same measure throughout: % Cum. Defaulted = original GrossApproval of defaulted loans / original GrossApproval.
SBA population and default definition are the benchmark's (FY2000+, not CANCLD, disbursed; ChargeOffDate present);
the bank side is the Saved Analysis's FY2000+ population (status-based, as in the workbook).

Industry contributions (exact, additive):
  industry-mix component_i      = (w_i - s_i) * (r_i - B0)     sums to B2 - B0
  within-industry component_i   = w_i * (b_i - r_i)            sums to R_covered - B2
  sparse-industry adjustment    = R_bank - R_covered           (0 when no industry is excluded)
  where w = bank FY2000+ share (renormalised over covered industries), s = SBA share within B0, r = SBA industry
  rate, b = bank industry rate. Their total is exactly R_bank - B0.

Sparse SBA industries (fewer than SPARSE SBA loans) are excluded from B2 and listed; B2 is renormalised over the
covered industries only if they hold at least COVERAGE_MIN of the bank's FY2000+ original amount, otherwise it is
NOT computed. Nothing is substituted.

The frozen output is a Benchmark Intelligence record bound to the Saved Analysis (id + payload SHA-256), stored next
to it (app_data/benchmark_intel/<analysis id>/BI-*.json); the Saved Analysis itself is never modified. Inputs that do
not reconcile with the frozen Saved Analysis raise BenchmarkIntelError instead of being repaired.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from intelligence.evidence import EvidenceError, canonical_json, sha256_of

from . import analysis as AN
from . import insights as IN
from . import settings

VERSION = "bench-intel-1.0"
SYSTEM = "foia-benchmark-intel"
CREDIBLE, SPARSE = 100, 30          # SBA loans per industry: credible >= 100; thin 30-99; sparse < 30
COVERAGE_MIN = 0.95                 # B2 is computed only if covered industries hold >= 95% of the bank's FY2000+ $
SMALL_BANK = IN.MIN_INDUSTRY_LOANS  # bank industry rows with fewer loans are flagged "small sample"
DIR_NAME = "benchmark_intel"
TOL = 1e-12


class BenchmarkIntelError(RuntimeError):
    pass


def _close(a, b, rel=TOL, abs_=0.5e-6):
    return abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))


def credibility(sba_loans):
    return "credible" if sba_loans >= CREDIBLE else ("thin" if sba_loans >= SPARSE else "sparse")


def compute(saved, bdata=None):
    """Benchmark Intelligence content for one Saved Analysis (deterministic). `bdata`: the release's grouped SBA
    benchmark population (analysis.bench_data); read from the release when omitted."""
    p, c = saved["payload"], saved["payload"]["criteria"]
    if "benchmark" not in p or "fy2000" not in p.get("bank", {}):
        raise BenchmarkIntelError("the Saved Analysis has no benchmark or FY2000+ bank figures")
    if bdata is None:
        bdata = AN.bench_data(c["period"], c["threshold"])
    b0 = p["benchmark"]["total"]["total"]
    sectors = p["benchmark"]["total"]["sector"]
    names = [s["name"] for s in sectors]
    bank_t = p["bank"]["fy2000"]["total"]["total"]
    bank_i = {r["name"]: r for r in p["bank"]["fy2000"]["total"]["industry"]}
    if set(bank_i) != set(names):
        raise BenchmarkIntelError("bank industry rows and benchmark industry rows differ in the Saved Analysis")

    # ---- SBA side: exact integer aggregates from the release, reconciled with the frozen payload
    tabs = AN.B.tables(bdata, names)[""]["sector"]
    sba = {}
    for s in sectors:
        m = tabs[s["name"]]
        loans, orig, d_orig = m[0], m[3], m[5]
        if loans != s["loans"] or not _close(orig, s["orig"]) or \
                (orig and isinstance(s["cum_def"], (int, float)) and not _close(d_orig / orig, s["cum_def"])):
            raise BenchmarkIntelError(f"release data for industry {s['name']!r} do not reconcile with the Saved "
                                      "Analysis's frozen benchmark")
        sba[s["name"]] = (loans, orig, d_orig)
    N = sum(v[1] for v in sba.values())
    D = sum(v[2] for v in sba.values())
    if sum(v[0] for v in sba.values()) != b0["loans"] or not _close(N, b0["orig"]):
        raise BenchmarkIntelError("benchmark totals do not reconcile with the Saved Analysis")
    if not _close(D / N, b0["cum_def"]):
        raise BenchmarkIntelError("B0 recomputed from its industries differs from the frozen B0")
    B0 = b0["cum_def"]                                   # read unchanged (approved calculation)

    # ---- B1: all SBA, every industry
    rows = bdata["rows"]
    a_loans, a_orig, a_def = sum(r[4] for r in rows), sum(r[7] for r in rows), sum(r[9] for r in rows)
    pop = p.get("benchmark_population") or {}
    if pop and (pop.get("loans") != a_loans or not _close(pop.get("orig", 0), a_orig)):
        raise BenchmarkIntelError("all-SBA population does not reconcile with the Saved Analysis")

    # ---- bank side (frozen FY2000+ population)
    R_orig, R_def = bank_t["orig"], bank_t["chgoff_orig"]
    if not R_orig:
        raise BenchmarkIntelError("the bank has no loans approved FY2000 onward")
    if not _close(sum(r["orig"] for r in bank_i.values()), R_orig) or \
            not _close(sum(r["chgoff_orig"] for r in bank_i.values()), R_def):
        raise BenchmarkIntelError("bank industry rows do not add up to the bank's FY2000+ totals")
    R_bank = bank_t["cum_def"]

    # ---- B2 with explicit coverage
    exposed = [n for n in names if bank_i[n]["orig"] > 0]
    sparse = [n for n in exposed if credibility(sba[n][0]) == "sparse"]
    covered_orig = sum(bank_i[n]["orig"] for n in exposed if n not in sparse)
    coverage = covered_orig / R_orig

    def share(level):
        return sum(bank_i[x]["orig"] for x in exposed if credibility(sba[x][0]) == level) / R_orig
    computed = coverage >= COVERAGE_MIN and covered_orig > 0
    w_used = {n: (bank_i[n]["orig"] / covered_orig if computed and n in exposed and n not in sparse else 0.0)
              for n in names}
    r = {n: (sba[n][2] / sba[n][1] if sba[n][1] else None) for n in names}
    B2 = sum(w_used[n] * r[n] for n in names if w_used[n]) if computed else None
    R_cov = (sum(bank_i[n]["chgoff_orig"] for n in exposed if n not in sparse) / covered_orig) if covered_orig else None

    industries = []
    for n in names:
        bi, (sl, so, sd) = bank_i[n], sba[n]
        w = bi["orig"] / R_orig
        s = so / N
        b = bi["chgoff_orig"] / bi["orig"] if bi["orig"] else None
        row = {"name": n, "naics": bi.get("naics", ""), "bank_loans": bi["loans"], "bank_orig": bi["orig"],
               "bank_defaulted_orig": bi["chgoff_orig"], "bank_share": w, "bank_cum_def": b,
               "sba_loans": sl, "sba_orig": so, "sba_defaulted_orig": sd, "sba_share": s, "sba_cum_def": r[n],
               "credibility": credibility(sl), "bank_small_sample": 0 < bi["loans"] < SMALL_BANK,
               "bank_exposure": bi["orig"] > 0, "excluded_from_b2": n in sparse, "b2_weight": w_used[n]}
        if computed:
            row["mix"] = (w_used[n] - s) * (r[n] - B0) if r[n] is not None else 0.0
            row["within"] = w_used[n] * (b - r[n]) if w_used[n] else 0.0
        industries.append(row)
    industries.sort(key=lambda x: (-abs(x.get("mix", 0) + x.get("within", 0)), -x["bank_orig"], x["name"]))

    decomposition = None
    if computed:
        decomposition = {"gap": R_bank - B0, "mix_total": sum(x["mix"] for x in industries),
                         "within_total": sum(x["within"] for x in industries),
                         "sparse_adjustment": R_bank - R_cov}
        if not (_close(decomposition["mix_total"], B2 - B0, abs_=1e-12) and
                _close(decomposition["within_total"], R_cov - B2, abs_=1e-12) and
                _close(decomposition["mix_total"] + decomposition["within_total"] +
                       decomposition["sparse_adjustment"], R_bank - B0, abs_=1e-12)):
            raise BenchmarkIntelError("industry contributions do not add up (internal check)")

    bank_full = p["bank"]["full"]["total"]["total"]
    pre_only = sum(1 for n in names if bank_i[n]["loans"] == 0)
    content = {
        "version": VERSION,
        "analysis": {"id": saved["id"], "payload_sha256": sha256_of(p), "created": saved["created"],
                     "calculation_version": p.get("calculation_version", "")},
        "criteria": {k: c[k] for k in ("period", "as_of", "bank_name", "bank_state", "cert", "location_ids",
                                       "threshold")},
        "release": p.get("release", {}),
        "rules": {"credible_sba_loans": CREDIBLE, "sparse_sba_loans": SPARSE, "coverage_min": COVERAGE_MIN,
                  "bank_small_sample_loans": SMALL_BANK},
        "bank_fy2000": {"loans": bank_t["loans"], "orig": R_orig, "defaulted_orig": R_def, "cum_def": R_bank},
        "bank_full": {"loans": bank_full["loans"], "orig": bank_full["orig"],
                      "defaulted_orig": bank_full["chgoff_orig"], "cum_def": bank_full["cum_def"]},
        "B0": {"loans": b0["loans"], "orig": b0["orig"], "defaulted_orig": D, "cum_def": B0, "industries": len(names)},
        "B1": {"loans": a_loans, "orig": a_orig, "defaulted_orig": a_def, "cum_def": a_def / a_orig},
        "B2": {"computed": computed, "cum_def": B2, "coverage": coverage, "credible_share": share("credible"),
               "thin_share": share("thin"), "sparse_share": share("sparse"),
               "industries_used": sum(1 for n in names if w_used[n]), "excluded": sorted(sparse),
               "covered_bank_orig": covered_orig, "covered_bank_cum_def": R_cov},
        "population": {"industries_in_b0": len(names), "industries_with_fy2000_bank_loans": len(exposed),
                       "industries_pre_fy2000_only": pre_only,
                       "sba_definition_gaps": pop.get("definition_gaps", {})},
        "industries": industries,
        "decomposition": decomposition,
    }
    return content


def record_id(content):
    return "BI-" + sha256_of(content)[:24]


def folder(app_data, analysis_id):
    return Path(app_data) / DIR_NAME / analysis_id


def freeze(saved, app_data=None, bdata=None):
    """Write the record next to the Saved Analysis (never inside it). Idempotent: an identical record is not rewritten."""
    content = compute(saved, bdata)
    rid = record_id(content)
    d = folder(app_data or settings.APP_DATA_DIR, saved["id"])
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{rid}.json"
    if not f.exists():
        tmp = f.with_suffix(".tmp")
        tmp.write_text(canonical_json({"record_id": rid, "content": content}), encoding="utf-8")
        tmp.replace(f)
    return rid, content


def load_frozen(app_data, analysis_id):
    """{record id: content} of stored records; an altered or misplaced record raises (never silently skipped)."""
    d = folder(app_data, analysis_id)
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("BI-*.json")):
        doc = json.loads(f.read_text(encoding="utf-8"))
        rid, content = doc.get("record_id"), doc.get("content")
        if rid != record_id(content) or f.stem != rid or content["analysis"]["id"] != analysis_id:
            raise EvidenceError(f"benchmark intelligence record {f.name} failed its integrity check")
        out[rid] = content
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Freeze Benchmark Intelligence records next to Saved Analyses")
    ap.add_argument("analysis_ids", nargs="+")
    a = ap.parse_args(argv)
    from . import saved as SV
    for i in a.analysis_ids:
        s = SV.load(i)
        if s is None:
            raise SystemExit(f"unknown Saved Analysis {i}")
        rid, c = freeze(s)
        b2 = c["B2"]["cum_def"]
        print(json.dumps({"analysis": i, "record": rid, "bank_fy2000": c["bank_fy2000"]["cum_def"],
                          "B0": c["B0"]["cum_def"], "B1": c["B1"]["cum_def"], "B2": b2,
                          "coverage": c["B2"]["coverage"], "frozen_at": datetime.now().isoformat(timespec="seconds")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
