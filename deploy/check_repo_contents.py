"""Refuse a commit or push that would put confidential or oversized files into Git.

Checks every file Git would track (staged and untracked, after .gitignore): forbidden extensions and folders, files
over 1 MB, and text containing local machine paths. Exit 1 lists the violations. Installed as .git/hooks/pre-commit
and .git/hooks/pre-push; run it by hand with: python deploy/check_repo_contents.py
"""
import subprocess
import sys
from pathlib import Path

FORBIDDEN_EXT = {".xlsx", ".xlsm", ".xlsb", ".xls", ".csv", ".pkl", ".pdf", ".docx", ".log", ".sqlite", ".db"}
FORBIDDEN_DIRS = ("reference/", "source_data/", "output/", "baseline/", "app_data/", "reports/", "foia_processing/",
                  "acceptance/benchmark/", ".venv/")
MAX_BYTES = 1_000_000
LOCAL_MARKERS = ("/Users/", "C:\\Users\\", "Y:\\SBA")


def candidate_files(root):
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def violations(root):
    bad = []
    for rel in candidate_files(root):
        p = root / rel
        if Path(rel).suffix.lower() in FORBIDDEN_EXT:
            bad.append(f"{rel}: forbidden file type")
        if rel.startswith(FORBIDDEN_DIRS):
            bad.append(f"{rel}: forbidden folder")
        if p.is_file() and p.stat().st_size > MAX_BYTES:
            bad.append(f"{rel}: larger than 1 MB")
        if p.is_file() and p.suffix in {".py", ".sh", ".md", ".html", ".css", ".yaml", ".txt", ".sha256", ""}:
            text = p.read_text(encoding="utf-8", errors="ignore")
            for m in LOCAL_MARKERS:
                if m in text and rel != "deploy/check_repo_contents.py":
                    bad.append(f"{rel}: contains a local path ({m})")
    return bad


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    bad = violations(root)
    if bad:
        print("REFUSED - files that must not enter Git:\n  " + "\n  ".join(bad))
        sys.exit(1)
    print(f"OK - {len(candidate_files(root))} files, none confidential, oversized or with local paths.")
