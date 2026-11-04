"""Post-deploy verification of the hosted preview (run from a FalconBridge machine; standard library + openpyxl).

  FBX_VERIFY_PASSWORD=<password> python deploy/verify_deployment.py https://<service>.onrender.com <username> \
      [--validated output/20260630]

Checks, in order:
  1. /healthz answers {"status": "ok"} without sign-in;
  2. every page, API, saved-analysis, generation and download route refuses requests without credentials and with a
     wrong password (401), and shows no FalconBridge content;
  3. signed in: the bank list, the US Metro dashboard (937 loans, % Cum. Defaulted 1.60% vs 6.59% at $350,000);
  4. generates the US Metro $350,000 workbook on the server, downloads it, checks its SHA-256 against the job page;
  5. with --validated: the download equals the validated US Metro workbook in every pre-existing worksheet, apart from
     documented metadata (Generation_Info; the source-folder cell of Benchmark_Notes) and the added Threshold_Analysis
     sheet - the same rule as tests/test_webapp.py.
Exit 0 only if every check passes. Nothing is changed on the server except one generated workbook job.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{(' - ' + detail) if detail else ''}")


def request(url, auth=None, data=None, headers=None):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read(), dict(r.headers), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers), url


def compare_with_validated(downloaded, validated):
    from openpyxl import load_workbook
    allowed = {"docProps/core.xml", "[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
               "xl/styles.xml"}
    zn, zv = zipfile.ZipFile(downloaded), zipfile.ZipFile(validated)
    pv, pn = set(zv.namelist()), set(zn.namelist())
    wb = load_workbook(downloaded, read_only=True)
    names = wb.sheetnames
    meta = {f"xl/worksheets/sheet{names.index(s) + 1}.xml" for s in ("Generation_Info", "Benchmark_Notes")}
    differing = sorted(n for n in pv & pn if zn.read(n) != zv.read(n) and n not in allowed | meta)
    check("download: only the Threshold_Analysis part is added", pn - pv == {"xl/worksheets/sheet25.xml"},
          str(sorted(pn - pv)))
    check("download: every other worksheet byte-identical to the validated workbook", not differing,
          ", ".join(differing) or "0 differing parts")
    vb, nb = load_workbook(validated, read_only=True)["Benchmark_Notes"], wb["Benchmark_Notes"]
    cells = [(c.coordinate, c.value, d.value) for r1, r2 in zip(vb.iter_rows(), nb.iter_rows())
             for c, d in zip(r1, r2) if c.value != d.value]
    only_folder = all("folder" in str(a) and "folder" in str(b) for _, a, b in cells)
    check("download: Benchmark_Notes differs only in the source-folder path", only_folder and len(cells) <= 1,
          f"{len(cells)} cell(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("user")
    ap.add_argument("--validated", help="folder with the validated workbooks (output/20260630)")
    a = ap.parse_args()
    base = a.url.rstrip("/")
    pw = os.environ.get("FBX_VERIFY_PASSWORD") or sys.exit("Set FBX_VERIFY_PASSWORD (not on the command line).")
    auth = (a.user, pw)

    s, body, _, _ = request(base + "/healthz")
    check("/healthz without sign-in", s == 200 and json.loads(body) == {"status": "ok"}, f"{s} {body[:60]!r}")
    zero = "0" * 32
    routes = ["/", "/api/states?period=20260630", "/api/banks?period=20260630&state=CA",
              "/confirm?period=20260630&name=US+Metro+Bank&state=CA",
              "/dashboard?period=20260630&name=US+Metro+Bank&state=CA", "/analyses", f"/analyses/{zero}", "/jobs",
              f"/jobs/{zero}", f"/jobs/{zero}/download", "/static/style.css"]
    wrong_pw_routes = {"/", "/jobs", f"/jobs/{zero}/download"}     # few wrong attempts: stays below the lockout (10)
    for r in routes:
        for label, cred in (("no sign-in", None), ("wrong password", (a.user, pw + "x"))):
            if cred and r not in wrong_pw_routes:
                continue
            s, body, _, _ = request(base + r, auth=cred)
            check(f"{r} refused ({label})", s in (401, 429) and b"US Metro" not in body and b"FinWise" not in body,
                  str(s))
    for r in ("/jobs", "/analyses"):
        s, _, _, _ = request(base + r, data=b"period=20260630")
        check(f"POST {r} refused (no sign-in)", s in (401, 429), str(s))

    s, body, _, _ = request(base + "/api/banks?period=20260630&state=CA", auth=auth)
    banks = json.loads(body) if s == 200 else []
    check("signed in: CA bank list", s == 200 and any(b["name"] == "US Metro Bank" for b in banks), f"{len(banks)} banks")
    t0 = time.time()
    s, body, _, _ = request(base + "/dashboard?period=20260630&name=US+Metro+Bank&state=CA&threshold=350000", auth=auth)
    page = body.decode("utf-8", "ignore")
    data = json.loads(re.search(r'<script id="payload" type="application/json">(.*?)</script>', page, re.S)
                      .group(1).replace("<\\/", "</")) if s == 200 else {}
    t = data.get("bank", {}).get("full", {}).get("total", {}).get("total", {})
    bm = data.get("benchmark", {}).get("total", {}).get("total", {})
    check("signed in: US Metro dashboard figures", s == 200 and t.get("loans") == 937
          and abs(t.get("cum_def", 0) - 0.016022053381372893) < 1e-15
          and abs(bm.get("cum_def", 0) - 0.06594063705848315) < 1e-15, f"{time.time() - t0:.1f}s")

    tok = re.search(r'name="token" value="([0-9a-f]+)"', page)
    form = urllib.parse.urlencode({"token": tok.group(1) if tok else "", "period": "20260630", "name": "US Metro Bank",
                                   "state": "CA", "threshold": "350000"}).encode()
    s, _, _, where = request(base + "/jobs", auth=auth, data=form, headers={"Origin": base})
    job = where.rstrip("/").rsplit("/", 1)[-1]
    check("signed in: workbook job submitted", s == 200 and re.fullmatch(r"[0-9a-f]{32}", job or "") is not None, job)
    status, t0 = "", time.time()
    while time.time() - t0 < 400:
        s, body, _, _ = request(f"{base}/jobs/{job}", auth=auth)
        txt = body.decode("utf-8", "ignore")
        if "Status: <strong>SUCCEEDED" in txt or "Status: <strong>FAILED" in txt:
            status = "SUCCEEDED" if "SUCCEEDED" in txt else "FAILED"
            break
        time.sleep(5)
    check("workbook generated on the server", status == "SUCCEEDED", f"{status or 'timeout'} after {time.time() - t0:.0f}s")
    if status == "SUCCEEDED":
        sha_page = re.search(r"SHA-256 ([0-9a-f]{16})", txt)
        s, blob, h, _ = request(f"{base}/jobs/{job}/download", auth=auth)
        out = Path("render_check_US_Metro_Bank_CA_20260630.xlsx")
        out.write_bytes(blob)
        check("download: file and checksum", s == 200 and sha_page is not None
              and hashlib.sha256(blob).hexdigest().startswith(sha_page.group(1)), f"{len(blob):,} bytes -> {out}")
        if a.validated:
            compare_with_validated(out, Path(a.validated) / "US_Metro_Bank_CA_20260630.xlsx")
    print(f"\n{sum(results)} of {len(results)} checks passed.")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
