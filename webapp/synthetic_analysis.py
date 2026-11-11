"""SYNTHETIC Saved Analysis for the offline AI investigation demonstration (Phase C). NOT SBA DATA.

Builds a Saved Analysis record in exactly the shape the dashboard stores (webapp/saved.py), for an invented lender,
"SYNTHETIC Demonstration Bank", so the evidence adapter, the M2 verifier and the investigation pipeline can be
demonstrated end to end without any real bank evidence, FalconBridge workbook content or client information.

How the figures are made: a short list of invented loan segments (vintage, industry, size, count, average amount,
defaulted loans, severity, active loans) is summed into the payload's tables, and the SBA benchmark rates are typed
in directly. This is test-fixture construction only; it is never used for a real lender, it is not part of the AI
layer, and the AI layer performs no arithmetic of its own. The story the numbers tell (younger portfolio, hotel
concentration, weaker small-loan results) is deliberate, so the investigation has something material to find.
"""
SYNTHETIC_ID = "5e7e7e7e5e7e7e7e5e7e7e7e5e7e7e7e"
AS_OF, THRESHOLD = "2026-06-30", 350_000
BANK = "SYNTHETIC Demonstration Bank"
VERSION = "synthetic-fixture-1.0 (SYNTHETIC data, not an SBA release; offline investigation demonstration)"
IND = {"H": ("Hotels (except Casino Hotels) and Motels", "721110"),
       "G": ("Gasoline Stations with Convenience Stores", "457110"),
       "R": ("Full-Service Restaurants", "722511"), "D": ("Offices of Dentists", "621210"),
       "T": ("General Freight Trucking, Long-Distance, Truckload", "484121")}
# (vintage, industry, loans, average original amount, defaulted loans, loss severity, active loans)
SEGMENTS = [
    (2008, "H", 4, 1_200_000, 2, 0.50, 0), (2008, "R", 6, 200_000, 2, 0.70, 0), (2010, "G", 5, 900_000, 0, 0, 0),
    (2012, "D", 8, 250_000, 1, 0.60, 1), (2015, "H", 10, 1_500_000, 1, 0.40, 3), (2016, "T", 6, 300_000, 1, 0.80, 2),
    (2018, "G", 12, 1_000_000, 0, 0, 6), (2019, "R", 5, 180_000, 1, 0.75, 2), (2020, "D", 10, 280_000, 0, 0, 7),
    (2021, "H", 18, 2_000_000, 0, 0, 15), (2022, "T", 8, 320_000, 1, 0.85, 6), (2023, "H", 22, 2_200_000, 0, 0, 21),
    (2024, "G", 15, 1_100_000, 0, 0, 15), (2025, "D", 12, 400_000, 0, 0, 12), (2025, "R", 9, 200_000, 0, 0, 9),
    (2026, "H", 8, 2_400_000, 0, 0, 8)]
# synthetic SBA benchmark: (loans, % Cum. Defaulted, % Loss Severity) per group, vintage pattern and industry rates
BENCH_TOTAL = {"total": (120_000, 0.060, 0.65), "above": (40_000, 0.045, 0.60), "below": (80_000, 0.085, 0.70)}
BENCH_VINTAGE = {y: r for y, r in [(2005, 0.22), (2006, 0.25), (2007, 0.26), (2008, 0.21), (2009, 0.12),
                                   (2010, 0.09), (2011, 0.08), (2012, 0.07), (2013, 0.06), (2014, 0.055),
                                   (2015, 0.05), (2016, 0.045), (2017, 0.04), (2018, 0.035), (2019, 0.025),
                                   (2020, 0.015), (2021, 0.01), (2022, 0.008), (2023, 0.004), (2024, 0.001),
                                   (2025, 0.0), (2026, 0.0)]}
BENCH_SECTOR = {"H": 0.035, "G": 0.065, "R": 0.10, "D": 0.03, "T": 0.09}
SBA_AGE_SHARES = {(1999, 2008): 0.20, (2009, 2021): 0.53, (2022, 2026): 0.27}


def _seg(s):
    y, i, n, avg, d, sev, act = s
    orig = n * avg
    return {"year": y, "ind": i, "loans": n, "orig": float(orig), "chgoff": d, "chgoff_orig": float(d * avg),
            "chgoff_amt": float(round(d * avg * sev)), "active": act, "active_orig": float(act * avg),
            "pif_orig": float((n - d - act) * avg), "age_months": (2026 - y) * 12 * act,
            "above": avg > THRESHOLD}


def _agg(segs):
    t = {k: sum(s[k] for s in segs) for k in ("loans", "orig", "chgoff", "chgoff_orig", "chgoff_amt", "active",
                                              "active_orig", "pif_orig", "age_months")}
    r = lambda a, b: a / b if b else "n/a"                                           # noqa: E731
    return {"loans": t["loans"], "orig": t["orig"], "guar": t["orig"] * 0.75, "unguar": t["orig"] * 0.25,
            "avg": r(t["orig"], t["loans"]), "active": t["active"], "active_orig": t["active_orig"],
            "age": r(t["age_months"], t["active"]), "chgoff": t["chgoff"], "chgoff_orig": t["chgoff_orig"],
            "chgoff_amt": t["chgoff_amt"], "pif_orig": t["pif_orig"], "purch_orig": 0.0,
            "cum_def": r(t["chgoff_orig"], t["orig"]), "severity": r(t["chgoff_amt"], t["chgoff_orig"]),
            "cum_loss": r(t["chgoff_amt"], t["orig"]), "pif_pct": r(t["pif_orig"], t["orig"]),
            "purch_pct": r(0.0, t["orig"])}


def _block(segs):
    total = _agg(segs)
    vint = [dict(_agg([s for s in segs if s["year"] == y]), year=y) for y in sorted({s["year"] for s in segs})]
    inds = []
    for k, (name, naics) in IND.items():
        rows = [s for s in segs if s["ind"] == k]
        if rows:
            row = dict(_agg(rows), name=name, naics=naics)
            row["share"] = row["orig"] / total["orig"]
            inds.append(row)
    return {"total": total, "vintage": vint, "industry": inds,
            "subprogram": [dict(total, name="Guaranty", label="Guaranty")]}


def _bench_row(loans, cum_def, sev, avg=500_000.0):
    return {"loans": loans, "orig": loans * avg, "avg": avg, "active": int(loans * 0.3), "active_orig": loans * avg * 0.3,
            "chgoff": int(loans * cum_def), "cum_def": cum_def, "severity": sev, "cum_loss": cum_def * sev,
            "purch_pct": 0.0}


def synthetic_saved():
    segs = [_seg(s) for s in SEGMENTS]
    groups = {"total": segs, "above": [s for s in segs if s["above"]], "below": [s for s in segs if not s["above"]]}
    bank = {h: {g: _block(gs) for g, gs in groups.items()} for h in ("full", "fy2000")}     # all vintages >= 2008
    bench = {}
    for g, (n, cd, sev) in BENCH_TOTAL.items():
        scale = cd / BENCH_TOTAL["total"][1]
        bench[g] = {"name": {"total": "All SBA 7(a)", "above": "Standard", "below": "Small"}[g],
                    "total": _bench_row(n, cd, sev),
                    "vintage": [dict(_bench_row(n // 22, min(0.6, v * scale), sev), year=y)
                                for y, v in BENCH_VINTAGE.items()],
                    "sector": [dict(_bench_row(n // 5, min(0.6, BENCH_SECTOR[k] * scale), sev), name=IND[k][0])
                               for k in IND]}
    comparison, industry_cmp, concentration = {}, {}, {}
    for g in groups:
        b, s = bank["fy2000"][g]["total"], bench[g]["total"]
        num = lambda v: isinstance(v, (int, float))                                 # noqa: E731
        comparison[g] = {m: {"bank": b[m], "sba": s[m], "diff": b[m] - s[m] if num(b[m]) and num(s[m]) else "n/a"}
                         for m in ("loans", "orig", "avg", "chgoff", "cum_def", "severity", "cum_loss", "purch_pct")}
        sec = {r["name"]: r for r in bench[g]["sector"]}
        industry_cmp[g] = [{"name": r["name"], "loans": r["loans"], "orig": r["orig"], "share": r["share"],
                            "bank_cum_def": r["cum_def"], "sba_cum_def": sec[r["name"]]["cum_def"],
                            "diff_cum_def": r["cum_def"] - sec[r["name"]]["cum_def"],
                            "bank_cum_loss": r["cum_loss"], "sba_cum_loss": sec[r["name"]]["cum_loss"],
                            "diff_cum_loss": r["cum_loss"] - sec[r["name"]]["cum_loss"],
                            "sba_loans": sec[r["name"]]["loans"]} for r in bank["fy2000"][g]["industry"]]
        shares = sorted((r["share"] for r in bank["full"][g]["industry"]), reverse=True)
        concentration[g] = {"top1": shares[0], "top5": sum(shares[:5]), "top10": sum(shares[:10]),
                            "industries": len(shares)}
    total_orig = bank["full"]["total"]["total"]["orig"]
    bands = [{"from": a, "to": b, "bank_share": sum(s["orig"] for s in segs if a <= s["year"] <= b) / total_orig,
              "sba_share": v} for (a, b), v in SBA_AGE_SHARES.items()]
    n = len(SEGMENTS) and bank["full"]["total"]["total"]["loans"]
    criteria = {"period": "20260630", "as_of": AS_OF, "bank_name": BANK, "bank_state": "ZZ", "cert": "",
                "location_ids": ["SYN-1"], "status": "UNIQUE", "threshold": THRESHOLD, "threshold_label": "350K",
                "default_threshold": True, "synthetic": True}
    payload = {
        "criteria": criteria,
        "identity": {"resolution": "SYNTHETIC lender (not in any SBA release)", "selected": [
            {"cert": "", "location_id": "SYN-1", "records": n, "eligible": n, "city": "", "street": ""}],
            "excluded": [], "records": n, "eligible": n, "excluded_counts": {}},
        "bank": bank, "benchmark": bench, "comparison": comparison, "industry_comparison": industry_cmp,
        "concentration": concentration,
        "seasoning": {"bands": bands, "recent_from": 2022, "recent_to": 2026, "vintages": [2008]},
        "partial_year": {"year": 2026, "through": "June 30, 2026", "label": "2026*",
                         "note": "* 2026 is a partial year: loans first disbursed through June 30, 2026."},
        "release": {"folder": "", "files": []},
        "calculation_version": VERSION,
    }
    return {"id": SYNTHETIC_ID, "owner": "synthetic", "created": "2026-11-06T00:00:00", "criteria": criteria,
            "view": "both", "job_id": "0" * 32, "payload": payload}
