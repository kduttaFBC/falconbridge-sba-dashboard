"""Workbook generation in its own process: python -m webapp.worker <job folder>.

The web process starts one worker per job (webapp.app.runner), streams its log lines and enforces a time limit, so a
slow or stuck generation can never hold the web process, and the generator's memory is returned when the job ends.
The worker calls the existing generator unchanged and writes result.json (or error.txt) into the job folder.

Exit codes: 0 generated; 2 refused by the generator (its message in error.txt); 1 unexpected error (error.txt).
"""
import json
import sys
import traceback
from pathlib import Path

from . import generator_bridge as GB

B_LABEL = GB.B.threshold_label


def RESOLUTION_LABEL(sel):
    return f"{GB.RESOLUTION_TEXT[sel['status']]} (FDIC cert {sel['cert'] or 'none'}; LocationID " \
           f"{', '.join(sel['location_ids'])})"


def generate_job(p, job_dir, log):
    """The generation and the executed checks for one job (unchanged from the in-process runner)."""
    sel = GB.resolve(p["period"], p["bank_name"], p["bank_state"], p.get("cert"), p.get("location_ids"))
    inputs = "browser prototype (local): " + "; ".join(
        f"{k}={v}" for k, v in (("period", p["period"]), ("bank-name", p["bank_name"]), ("bank-state", p["bank_state"]),
                                ("fdic-cert", p.get("cert")), ("location-id", ",".join(p.get("location_ids") or [])))
        if v)
    threshold = p.get("threshold") or GB.GC.BENCHMARK_STANDARD_MIN
    inputs += f"; threshold={threshold}"
    out, info = GB.generate(p["period"], sel, job_dir, inputs, log, threshold=threshold)
    checks, gi, n_val, sha = GB.post_checks(out)
    wb_t, n_ta, titles = gi.pop("_threshold"), gi.pop("_threshold_checks"), gi.pop("_titles")
    ext = info["extract"]
    executed = [
        ("Lender identity resolved by the generator's rules", True, RESOLUTION_LABEL(sel)),
        ("Single SBA AsOfDate for the lender's records equals the selected period", True, str(info["as_of"])),
        ("Every benchmark loan carries the same AsOfDate (checked by the generator)", True, str(info["as_of"])),
        ("Eligible loans written to the FOIA sheet", True, f"{len(ext['rows']):,} of {ext['total_records']:,} records"),
        ("Workbook threshold equals the selected threshold (Threshold_Analysis!B3, group headings)",
         wb_t == threshold and all(B_LABEL(threshold) in t for t in titles),
         f"${wb_t:,}; " + " | ".join(titles)),
    ] + [(c, ok, detail) for c, ok, detail in checks]
    return {"file": out.name, "sha256": sha, "size": out.stat().st_size, "checks": executed, "threshold": threshold,
            "validation_sheet_checks": n_val, "threshold_sheet_checks": n_ta, "generation_info": list(gi.items()),
            "selection": {k: sel[k] for k in ("bank_name", "bank_state", "cert", "location_ids", "status")}}


def _write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main(argv):
    job_dir = Path(argv[1]).resolve()
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))

    def log(msg):
        print("LOG " + str(msg).replace("\n", " "), flush=True)
    try:
        result = generate_job(job["params"], job_dir, log)
    except SystemExit as e:                                   # the generator reports refusals this way
        _write(job_dir / "error.txt", str(e.code if e.code is not None else e))
        return 2
    except Exception as e:                                    # noqa: BLE001 - reported to the analyst
        _write(job_dir / "error.txt", f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return 1
    _write(job_dir / "result.json", json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
