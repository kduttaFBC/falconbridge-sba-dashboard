"""One-at-a-time generation queue. Each job lives in app_data/jobs/<id>/ (job.json + the generated workbook)."""
import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime

from . import settings

JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def audit(user, action, **details):
    settings.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"time": datetime.now().isoformat(timespec="seconds"), "user": user, "action": action, **details}
    with open(settings.AUDIT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


class JobManager:
    def __init__(self, runner):
        self.runner = runner                     # runner(job: dict, job_dir: Path, log: callable) -> result dict
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.running = 0
        self.max_running_seen = 0
        self.worker = threading.Thread(target=self._loop, name="generation-worker", daemon=True)
        self.worker.start()

    # ---------------------------------------------------------------- records
    def job_dir(self, job_id):
        if not JOB_ID.match(job_id or ""):
            return None
        return settings.JOBS_DIR / job_id

    def load(self, job_id):
        d = self.job_dir(job_id)
        if d is None or not (d / "job.json").is_file():
            return None
        return json.loads((d / "job.json").read_text(encoding="utf-8"))

    def save(self, job):
        d = settings.JOBS_DIR / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=1, default=str), encoding="utf-8")
        tmp.replace(d / "job.json")

    def list(self, owner):
        out = []
        if settings.JOBS_DIR.is_dir():
            for d in settings.JOBS_DIR.iterdir():
                j = self.load(d.name)
                if j and j["owner"] == owner:
                    out.append(j)
        return sorted(out, key=lambda j: j["created"], reverse=True)

    # ---------------------------------------------------------------- queue
    def submit(self, owner, params):
        job = {"id": uuid.uuid4().hex, "owner": owner, "created": datetime.now().isoformat(timespec="seconds"),
               "status": "queued", "params": params, "log": [], "result": None, "error": None}
        self.save(job)
        self.q.put(job["id"])
        return job

    def _loop(self):
        while True:
            job_id = self.q.get()
            job = self.load(job_id)
            with self.lock:
                self.running += 1
                self.max_running_seen = max(self.max_running_seen, self.running)
            t0 = time.time()
            job["status"], job["started"] = "running", datetime.now().isoformat(timespec="seconds")
            self.save(job)

            def log(msg):
                job["log"].append(str(msg))
                self.save(job)
            try:
                job["result"] = self.runner(job, settings.JOBS_DIR / job_id, log)
                job["status"] = "succeeded"
            except SystemExit as e:              # the generator reports refusals this way
                job["status"], job["error"] = "failed", str(e.code if e.code is not None else e)
            except Exception as e:               # noqa: BLE001 - shown to the analyst, never swallowed
                job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"
            finally:
                job["finished"] = datetime.now().isoformat(timespec="seconds")
                job["seconds"] = round(time.time() - t0, 1)
                self.save(job)
                audit(job["owner"], "generate", job=job_id, status=job["status"], error=job["error"],
                      **{k: job["params"].get(k) for k in ("period", "bank_name", "bank_state", "cert")})
                with self.lock:
                    self.running -= 1
                self.q.task_done()

    def wait_idle(self, timeout=600):
        """For tests: wait until the queue is empty and nothing is running."""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if self.q.unfinished_tasks == 0 and self.running == 0:
                    return True
            time.sleep(0.2)
        return False
