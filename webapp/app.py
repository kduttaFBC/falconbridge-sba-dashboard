"""FalconBridge SBA FOIA browser prototype - local, single user, 127.0.0.1 only.

Flow: reporting period -> state and bank (or search by name / FDIC certificate) -> confirm lender identity ->
dashboard for an original loan balance threshold -> Excel workbook generated for the same criteria (queued, one at a
time) -> download; analyses can be saved and reopened. Every figure and workbook comes from the generator's own
modules. No Microsoft 365 sign-in, external distribution or hosting in this phase.
"""
import json
import re
import hashlib
import hmac
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import analysis as AN
from intelligence import cleanroom_foia as CR

from . import claims_assessment as CA
from . import evidence_sections as ES
from . import generator_bridge as GB
from . import lending_activity as LA
from . import presentation as PR
from . import preview_auth as PA
from . import saved as SV
from . import settings
from .jobs import JobManager, audit

settings.check_app_data_location()
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
app = FastAPI(title="FalconBridge SBA FOIA - preliminary", docs_url=None, redoc_url=None, openapi_url=None)
class HostCheck(TrustedHostMiddleware):
    """Host-header check for every route except /healthz, which a hosting platform's health checker may call with an
    internal host name (it returns only {"status": "ok"})."""
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(HostCheck, allowed_hosts=settings.ALLOWED_HOSTS)
PREVIEW_USERS = PA.parse_users(settings.PREVIEW_USERS_SPEC)
SECURITY_HEADERS = {"Strict-Transport-Security": "max-age=31536000", "X-Frame-Options": "DENY",
                    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}


@app.middleware("http")
async def preview_gate(request: Request, call_next):
    """Preview mode: every path except /healthz requires a named user's credentials (HTTP Basic over HTTPS)."""
    if settings.AUTH_MODE not in ("preview", "public"):
        return await call_next(request)
    if settings.AUTH_MODE == "public":                      # no sign-in (approved 2026-11-11); headers still apply
        request.state.user = settings.PUBLIC_USER
    elif request.url.path != "/healthz":
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "?")
        if PA.locked(ip):
            return PlainTextResponse("Too many failed sign-in attempts. Try again in 15 minutes.", status_code=429)
        user = PA.authenticate(PREVIEW_USERS, request.headers.get("authorization"), ip)
        if user is None:
            return PlainTextResponse("Sign-in required.", status_code=401, headers={
                "WWW-Authenticate": 'Basic realm="FalconBridge SBA preview", charset="UTF-8"'})
        request.state.user = user
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
_SECRET = secrets.token_bytes(32)                       # new per server start; forms carry an HMAC token
FORM_TOKEN = hmac.new(_SECRET, b"fbx-form", hashlib.sha256).hexdigest()


def current_user(request: Request):
    """Local mode: the single local analyst. Preview mode: the named user signed in by preview_gate. (The full sign-in
    and organization system replaces this in a later, approved phase.)"""
    if settings.AUTH_MODE == "local":
        return settings.LOCAL_USER
    if settings.AUTH_MODE in ("preview", "public") and getattr(request.state, "user", None):
        return request.state.user
    raise HTTPException(503, "Sign-in is not configured.")


def check_form(request: Request, token: str):
    if not hmac.compare_digest(token or "", FORM_TOKEN):
        raise HTTPException(403, "Invalid form token - reload the page and try again.")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in settings.ALLOWED_HOSTS:
        raise HTTPException(403, "Cross-site request refused.")


def page(request, template, **ctx):
    ctx.update(request=request, token=FORM_TOKEN, version=GB.GENERATOR_VERSION)
    return templates.TemplateResponse(request, template, ctx)


def valid_period(period):
    if period not in GB.periods():
        raise HTTPException(404, "Unknown reporting period.")
    return period


def runner(job, job_dir, log):
    """Generate in a separate process (webapp.worker) with a time limit. The web process only streams the worker's log
    and reads its result, so a slow or stuck generation cannot hold the web process; on timeout the worker's whole
    process group is stopped and the job fails with an explicit message."""
    job_dir = Path(job_dir)
    for name in ("result.json", "error.txt"):
        (job_dir / name).unlink(missing_ok=True)
    env = dict(os.environ, FBX_APP_DATA=str(settings.APP_DATA_DIR), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, "-m", "webapp.worker", str(job_dir)], cwd=str(settings.PROJECT_ROOT),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            start_new_session=True)
    lines = queue.Queue()

    def pump():                                               # worker output -> queue; None marks the end
        for x in proc.stdout:
            lines.put(x)
        lines.put(None)
    threading.Thread(target=pump, daemon=True).start()
    deadline, done = time.monotonic() + settings.JOB_TIMEOUT, False
    while not done:
        try:
            line = lines.get(timeout=max(0.1, min(1.0, deadline - time.monotonic())))
        except queue.Empty:
            line = ""
        if line is None:
            done = True
        elif line.startswith("LOG "):
            log(line[4:].rstrip("\n"))
        if not done and time.monotonic() > deadline:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            for f in job_dir.glob("*.xlsx"):                  # never leave a partial workbook behind
                f.unlink(missing_ok=True)
            raise SystemExit(f"Not generated: workbook generation exceeded the {settings.JOB_TIMEOUT}-second time "
                             "limit and was stopped. No workbook was produced; try again or contact the administrator.")
    code = proc.wait()
    if code != 0:
        err = job_dir / "error.txt"
        for f in job_dir.glob("*.xlsx"):
            f.unlink(missing_ok=True)
        raise SystemExit(err.read_text(encoding="utf-8") if err.exists() else f"Generation process failed (exit {code}).")
    return json.loads((job_dir / "result.json").read_text(encoding="utf-8"))


jobs = JobManager(runner)


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    resp = templates.TemplateResponse(request, "error.html", {"request": request, "detail": exc.detail, "token": "",
                                                               "version": GB.GENERATOR_VERSION}, status_code=exc.status_code)
    return resp


# ---------------------------------------------------------------- pages
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, period: str = "", q: str = ""):
    current_user(request)
    periods = list(GB.periods())
    if not periods:
        return page(request, "index.html", periods=[], period="", q=q, hits=None, total=0, message=(
            "No SBA FOIA release found in source_data/. Prepare a release with fix_foia.py first."))
    period = period if period in periods else periods[-1]
    hits, total, message = None, 0, None
    if q.strip():
        if len(q.strip()) < 2:
            message = "Type at least 2 characters (part of the bank name, or the FDIC certificate number)."
        else:
            try:
                hits, total = GB.search(period, q)
            except GB.SourceError as e:
                message = f"The selected release cannot be used: {e}"
            if hits == []:
                message = "No bank with eligible loans matches that text. Try a shorter or different spelling."
    return page(request, "index.html", periods=periods, period=period, q=q, hits=hits, total=total, message=message,
                norm=GB.S.norm_id, states=GB.states(period), default_threshold=GB.GC.BENCHMARK_STANDARD_MIN)


@app.get("/api/states")
def api_states(request: Request, period: str):
    current_user(request)
    valid_period(period)
    return [{"state": s, "label": s or "(no state published)", "banks": n} for s, n in GB.states(period)]


@app.get("/api/banks")
def api_banks(request: Request, period: str, state: str):
    current_user(request)
    valid_period(period)
    return GB.banks(period, state)


@app.get("/confirm", response_class=HTMLResponse)
def confirm(request: Request, period: str, name: str, state: str, cert: str = "", loc: list[str] = Query(default=[]),
            threshold: str = ""):
    current_user(request)
    valid_period(period)
    entry = GB.entry(period, name, state)
    if entry is None:
        raise HTTPException(404, "Bank not found in this release.")
    try:
        sel = GB.resolve(period, name, state, cert or None, loc or None)
        error = None
    except GB.SelectionError as e:
        sel, error = None, str(e)
    choose_cert = error is not None and len(entry["certs"]) > 1 and (not cert or GB.S.norm_id(cert) not in entry["certs"])
    links = {c: "/confirm?" + urlencode({"period": period, "name": name, "state": state, "cert": c,
                                         "threshold": threshold})
             for c in entry["certs"] if c}
    return page(request, "confirm.html", period=period, name=name, state=state, cert=cert, loc=loc or [], entry=entry,
                sel=sel, error=error, choose_cert=choose_cert, cert_links=links, threshold=threshold or
                f"{GB.GC.BENCHMARK_STANDARD_MIN:,}", resolution=GB.RESOLUTION_TEXT.get(sel["status"]) if sel else None)


@app.post("/jobs")
def create_job(request: Request, token: str = Form(""), period: str = Form(...), name: str = Form(...),
               state: str = Form(...), cert: str = Form(""), loc: list[str] = Form(default=[]), threshold: str = Form("")):
    user = current_user(request)
    check_form(request, token)
    valid_period(period)
    t, terr = AN.parse_threshold(threshold)
    if terr:
        raise HTTPException(400, terr)
    try:                                         # resolve again on the server: nothing from the browser is trusted
        sel = GB.resolve(period, name, state, cert or None, loc or None)
    except GB.SelectionError as e:
        raise HTTPException(400, f"Lender identity is ambiguous: {e}")
    job = submit_job(user, period, sel, cert, loc, t)
    return RedirectResponse(f"/jobs/{job['id']}", status_code=303)


def submit_job(user, period, sel, cert, loc, threshold):
    job = jobs.submit(user, {"period": period, "bank_name": sel["bank_name"], "bank_state": sel["bank_state"],
                             "cert": cert or None, "location_ids": loc or None, "threshold": threshold})
    audit(user, "submit", job=job["id"], period=period, bank_name=sel["bank_name"], bank_state=sel["bank_state"],
          cert=cert or None, threshold=threshold)
    return job


# ---------------------------------------------------------------- dashboard
VIEWS = ("total", "above", "below", "both")


def _criteria_query(period, name, state, cert, loc, threshold):
    return urlencode([("period", period), ("name", name), ("state", state), ("cert", cert or "")]
                     + [("loc", x) for x in loc or []] + [("threshold", str(threshold))])


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, period: str, name: str, state: str, cert: str = "",
              loc: list[str] = Query(default=[]), threshold: str = "", view: str = "both", tab: str = "overview"):
    user = current_user(request)
    valid_period(period)
    t, terr = AN.parse_threshold(threshold)
    try:
        sel = GB.resolve(period, name, state, cert or None, loc or None)
    except GB.SelectionError:
        return RedirectResponse("/confirm?" + urlencode({"period": period, "name": name, "state": state,
                                                         "cert": cert, "threshold": threshold}), status_code=303)
    if terr:
        t = GB.GC.BENCHMARK_STANDARD_MIN
    payload = AN.compute(period, sel, t)
    audit(user, "dashboard", period=period, bank_name=sel["bank_name"], bank_state=sel["bank_state"],
          cert=sel["cert"], threshold=t)
    return page(request, "dashboard.html", payload=payload, data=json.dumps(payload, default=str),
                view=view if view in VIEWS else "both", tab=tab, threshold_error=terr, saved=None,
                form={"period": period, "name": name, "state": state, "cert": cert, "loc": loc,
                      "threshold": f"{t:,}"}, query=_criteria_query(period, name, state, cert, loc, t))


@app.post("/analyses")
def save_analysis(request: Request, token: str = Form(""), period: str = Form(...), name: str = Form(...),
                  state: str = Form(...), cert: str = Form(""), loc: list[str] = Form(default=[]),
                  threshold: str = Form(""), view: str = Form("both")):
    user = current_user(request)
    check_form(request, token)
    valid_period(period)
    t, terr = AN.parse_threshold(threshold)
    if terr:
        raise HTTPException(400, terr)
    try:
        sel = GB.resolve(period, name, state, cert or None, loc or None)
    except GB.SelectionError as e:
        raise HTTPException(400, f"Lender identity is ambiguous: {e}")
    payload = AN.compute(period, sel, t)
    job = submit_job(user, period, sel, cert, loc, t)
    a = SV.save(user, payload, job["id"], view if view in VIEWS else "both")
    audit(user, "save_analysis", analysis=a["id"], job=job["id"], period=period, bank_name=sel["bank_name"],
          bank_state=sel["bank_state"], cert=sel["cert"], threshold=t)
    return RedirectResponse(f"/analyses/{a['id']}", status_code=303)


@app.get("/analyses", response_class=HTMLResponse)
def analyses(request: Request):
    user = current_user(request)
    items = SV.listing(user)
    for a in items:
        a["job"] = jobs.load(a["job_id"])
    return page(request, "analyses.html", items=items)


@app.get("/analyses/{analysis_id}", response_class=HTMLResponse)
def open_analysis(request: Request, analysis_id: str, view: str = "", tab: str = "overview"):
    user = current_user(request)
    a = SV.load(analysis_id)
    if a is None:
        raise HTTPException(404, "Saved analysis not found.")
    if a["owner"] != user:
        raise HTTPException(403, "This analysis belongs to another user.")
    c = a["criteria"]
    if "insights" not in a["payload"]:                # saved before the summary existed: derive from frozen figures
        a["payload"]["insights"] = AN.IN.build(a["payload"])
    audit(user, "open_analysis", analysis=a["id"])
    return page(request, "dashboard.html", payload=a["payload"], data=json.dumps(a["payload"], default=str),
                view=view if view in VIEWS else a["view"], tab=tab, threshold_error=None,
                saved={"id": a["id"], "created": a["created"], "job": jobs.load(a["job_id"])},
                form={"period": c["period"], "name": c["bank_name"], "state": c["bank_state"], "cert": c["cert"] or "",
                      "loc": c["location_ids"] if c["status"] == "RESTRICTED_TO_LOCATION_IDS" else [],
                      "threshold": f"{c['threshold']:,}"},
                query=_criteria_query(c["period"], c["bank_name"], c["bank_state"], c["cert"],
                                      c["location_ids"] if c["status"] == "RESTRICTED_TO_LOCATION_IDS" else [],
                                      c["threshold"]))


# ------------------------------------------------------------------------------------------------------------------
# Executive / Lender view, Analyst evidence view and Lender claims assessment (local demonstration).
# All three read the same frozen Saved Analysis through the same evidence pack (webapp/presentation.py); nothing here
# calculates a financial figure, and nothing is sent to any external service.
EV_ID = re.compile(r"^EV-[0-9a-f]{24}$")


def owned_analysis(request, analysis_id):
    user = current_user(request)
    a = SV.load(analysis_id)
    if a is None:
        raise HTTPException(404, "Saved analysis not found.")
    if a["owner"] != user:
        raise HTTPException(403, "This analysis belongs to another user.")
    return user, a


def _validations(analysis_id):
    """Stored clean-room validation records of one analysis (integrity-checked; an altered file is refused)."""
    try:
        return CR.load_records(str(settings.APP_DATA_DIR), analysis_id)
    except Exception as e:                                   # noqa: BLE001 - reported, never silently ignored
        raise HTTPException(409, f"A stored validation record failed its integrity check: {e}")


def _switcher(user, current):
    return [{"id": x["id"], "label": f"{x['criteria']['bank_name']}, {x['criteria']['bank_state']} - "
             f"{x['criteria']['as_of']} - ${x['criteria']['threshold']:,} - saved {x['created'].replace('T', ' ')[:16]}",
             "current": x["id"] == current}
            for x in SV.listing(user)]


@app.get("/analyses/{analysis_id}/executive", response_class=HTMLResponse)
def executive_view(request: Request, analysis_id: str, mode: str = ""):
    user, a = owned_analysis(request, analysis_id)
    model = PR.build(a, validations=_validations(a["id"]))
    audit(user, "executive_view", analysis=a["id"], mode=mode or "standard")
    if mode == "present":
        # presentation mode receives ONLY the allow-listed subset; the full model never reaches the template
        return page(request, "present.html", present=PR.presentation(model))
    return page(request, "executive.html", m=model, analyses=_switcher(user, a["id"]), section="executive")


@app.get("/analyses/{analysis_id}/evidence", response_class=HTMLResponse)
def evidence_view(request: Request, analysis_id: str, ev: list[str] = Query(default=[])):
    user, a = owned_analysis(request, analysis_id)
    vals = _validations(a["id"])
    model = PR.build(a, validations=vals, lookup=[x for x in ev[:20] if EV_ID.match(x)])
    model["sections"] = ES.build_sections(model, PR.pack_for(a, vals))        # same pack, regrouped like the dashboard
    audit(user, "evidence_view", analysis=a["id"])
    return page(request, "evidence.html", m=model, analyses=_switcher(user, a["id"]), section="evidence")


@app.get("/analyses/{analysis_id}/claims", response_class=HTMLResponse)
def claims_view(request: Request, analysis_id: str, claim: str = "", preset: str = "", metric: str = "",
                population: str = "", period: str = ""):
    user, a = owned_analysis(request, analysis_id)
    vals = _validations(a["id"])
    model = PR.build(a, validations=vals)
    presets = dict(CA.PRESETS)
    text = presets.get(preset, claim)[:500]
    result = None
    if text or preset or claim:
        result = CA.assess(PR.pack_for(a, vals), text, {"metric": metric if metric in CA.METRICS else "",
                                                  "population": population if population in CA.POPULATIONS else "",
                                                  "period": period if re.fullmatch(r"\d{4}-\d{2}-\d{2}", period) else ""})
        audit(user, "claim_assessment", analysis=a["id"], assessment=result["assessment_id"], outcome=result["outcome"])
    return page(request, "claims.html", m=model, analyses=_switcher(user, a["id"]), section="claims",
                presets=CA.PRESETS, text=text, result=result,
                chosen={"metric": metric, "population": population, "period": period})


# ------------------------------------------------------------------------------------------------------------------
# Loan-level drill-down (Phase 1). The loans behind one displayed figure, from the same eligible rows and definitions
# as the dashboard (webapp/lending_activity.py). Borrower name and street are never returned. Every response states the
# population and whether the loans reproduce the displayed aggregate.
def _loans_json(user, period, sel, dim, value, displayed_activity, context):
    if dim not in LA.DIMENSIONS or not value or len(value) > 300:
        raise HTTPException(400, "Unknown drill-down selection.")
    ext, _ = AN.bank_data(period, sel)
    sm = AN.status_map()
    data = LA.loan_rows(LA.select(ext["rows"], sm, dim, value), sm)
    agg = LA.aggregate(displayed_activity, dim, value)
    checks = LA.reconcile(data["totals"], agg)
    audit(user, "loan_drilldown", context=context, bank_name=sel["bank_name"], bank_state=sel["bank_state"],
          dim=dim, value=value, loans=data["totals"]["loans"])
    return JSONResponse({"dimension": dim, "value": value, "selection": LA.describe(dim, value),
                         "population": LA.POPULATION, "bank": f"{sel['bank_name']}, {sel['bank_state']}",
                         "as_of": (displayed_activity or {}).get("as_of", ""), "columns": data["columns"],
                         "loans": data["loans"], "totals": data["totals"], "displayed": agg,
                         "reconciles": bool(checks) and all(checks.values()), "checks": checks,
                         "hidden_fields": list(LA.HIDDEN_FIELDS),
                         "amounts": "original approval amounts and gross charge-offs; not current balances"})


@app.get("/dashboard/loans")
def dashboard_loans(request: Request, period: str, name: str, state: str, dim: str, value: str, cert: str = "",
                    loc: list[str] = Query(default=[]), threshold: str = ""):
    user = current_user(request)
    valid_period(period)
    t, terr = AN.parse_threshold(threshold)
    try:
        sel = GB.resolve(period, name, state, cert or None, loc or None)
    except GB.SelectionError:
        raise HTTPException(400, "Lender identity is ambiguous.")
    payload = AN.compute(period, sel, GB.GC.BENCHMARK_STANDARD_MIN if terr else t)
    return _loans_json(user, period, sel, dim, value, payload.get("activity"), "dashboard")


@app.get("/analyses/{analysis_id}/loans")
def analysis_loans(request: Request, analysis_id: str, dim: str, value: str):
    user, a = owned_analysis(request, analysis_id)
    c = a["criteria"]
    try:
        sel = GB.resolve(c["period"], c["bank_name"], c["bank_state"], c["cert"] or None,
                         c["location_ids"] if c["status"] == "RESTRICTED_TO_LOCATION_IDS" else None)
    except GB.SelectionError:
        raise HTTPException(409, "The saved lender identity no longer resolves in this release.")
    # reconciled against the figures FROZEN in the Saved Analysis (absent in analyses saved before Phase 1)
    return _loans_json(user, c["period"], sel, dim, value, a["payload"].get("activity"), f"analysis {a['id']}")


def owned_job(request, job_id):
    user = current_user(request)
    job = jobs.load(job_id)
    if job is None:
        raise HTTPException(404, "Report not found.")
    if job["owner"] != user:
        raise HTTPException(403, "This report belongs to another user.")
    return user, job


@app.get("/jobs", response_class=HTMLResponse)
def history(request: Request):
    user = current_user(request)
    return page(request, "history.html", jobs=jobs.list(user))


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    _, job = owned_job(request, job_id)
    return page(request, "job.html", job=job)


@app.get("/jobs/{job_id}/download")
def download(request: Request, job_id: str):
    user, job = owned_job(request, job_id)
    if job["status"] != "succeeded":
        raise HTTPException(409, "The workbook is not ready.")
    job_dir = (settings.JOBS_DIR / job["id"]).resolve()
    path = (job_dir / job["result"]["file"]).resolve()
    if path.parent != job_dir or path.suffix != ".xlsx" or not path.is_file():
        raise HTTPException(404, "File not available.")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if sha != job["result"]["sha256"]:
        raise HTTPException(409, "The stored file no longer matches its recorded checksum; it was not served.")
    audit(user, "download", job=job["id"], file=path.name, sha256=sha)
    return FileResponse(path, filename=path.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# Benchmark Intelligence Phase A (investigative comparisons; webapp/benchmark_view.py). The route answers 404 unless
# FBX_BENCHMARK_INTEL=enabled, so the Executive Summary, dashboard and every existing route are unchanged.
from . import benchmark_view as BV  # noqa: E402
BV.register(app, owned_analysis=owned_analysis, page=page, audit=audit, switcher=_switcher)
