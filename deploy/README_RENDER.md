# Render deployment runbook: FalconBridge client preview (PREPARED, NOTHING CREATED)

**Access.** Every route except `/healthz` requires a named preview user (HTTP Basic sign-in over Render's HTTPS):
- `FBX_AUTH_MODE=preview`, with users in the secret `FBX_PREVIEW_USERS` (scrypt hashes only);
- 10 wrong passwords from one address lock it out for 15 minutes.

This is a preview gate, not the full sign-in and organization system, which is deferred.

**Fails closed** (`deploy/start_render.sh`):
- **Data missing.** Until the disk data matches `deploy/DATA_MANIFEST.sha256`, a maintenance responder answers only
  `/healthz` and returns 503 to everything else. Render keeps the instance up so the data can be uploaded.
- **No credentials.** The app refuses to start without preview credentials, and in any unknown mode.

## What lives where

| Item | Location | In Git? |
|---|---|---|
| Code, tests, `deploy/` | Private GitHub repository: allow-list `.gitignore`; `deploy/check_repo_contents.py` runs as pre-commit and pre-push hooks | Yes |
| Confidential workbook template | Render disk: `/var/data/template/SBA_Historical Performance_FinWise Bank_2026Q2.xlsx` | **Never** |
| Public FOIA release (`fix_foia.py` output) | `/var/data/source_data/20260630/` (4 CSVs + `correction_log.txt`) | **Never** |
| Protected source notes (client-specific disclosure text and figures) | `/var/data/config/source_notes.json` (local: `protected_config/source_notes.json`) | **Never** |
| Caches (lender directory, benchmark) | `/var/data/cache/` (built automatically on first use, about 1 minute) | No |
| Jobs, workbooks, saved analyses, audit log | `/var/data/app_data/` (Render snapshots the disk daily, kept 7 days) | No |
| Preview passwords | Nowhere: shown once by `deploy/make_preview_credential.py`; only hashes go into Render | **Never** |

## Steps (one at a time; each needs approval)

In the commands below, `SRV` is the service's SSH user and host shown in the Render Dashboard (**Connect → SSH**), in
the form `srv-xxxx@ssh.oregon.render.com`.

1. **Create the private GitHub repository** (Claude, with `gh` signed in as kduttaFBC):

       gh repo create kduttaFBC/falconbridge-sba-dashboard --private --description "FalconBridge SBA FOIA dashboard (private)"

2. **Commit and push** the allow-listed files (Claude). The guard hooks run automatically.

       git add -A && python deploy/check_repo_contents.py && git commit -m "FalconBridge SBA FOIA dashboard: client preview"
       git remote add origin https://github.com/kduttaFBC/falconbridge-sba-dashboard.git && git push -u origin main

3. **Create the preview credentials** (Claude, locally):

       python deploy/make_preview_credential.py <username>

   - The password goes to you once.
   - Send it to the client by phone or text, never with the URL.
   - The `FBX_PREVIEW_USERS` entry (a hash) goes into Render. Separate several users with `;`.
4. **Render: connect GitHub** (you). In Render, connect your GitHub account and grant access to
   `falconbridge-sba-dashboard` only.
5. **Render: create the Web Service** (you; **paid**). New → Web Service → the repository, branch `main`. Then:

   | Setting | Value |
   |---|---|
   | Name | `falconbridge-sba-foia` |
   | Region | Oregon |
   | Runtime | Python 3 |
   | Build Command | `pip install -r requirements-web.txt` |
   | Start Command | `bash deploy/start_render.sh` |
   | Instance type | 1 CPU / 2 GB (Standard) |
   | Health Check Path | `/healthz` |
   | Auto-Deploy | Off |
   | Disk | name `fbx-data`, mount path `/var/data`, size 10 GB |

   **Environment:**

   | Variable | Value |
   |---|---|
   | `PYTHON_VERSION` | `3.12.15` |
   | `FBX_DATA_ROOT` | `/var/data` |
   | `FBX_APP_DATA` | `/var/data/app_data` |
   | `FBX_AUTH_MODE` | `preview` |
   | `FBX_PREVIEW_USERS` | the entry from step 3 (mark it **secret**) |
   | `FBX_SOURCE_NOTES` | `/var/data/config/source_notes.json` |
   | `FBX_JOB_TIMEOUT` | `300` |
   | `FBX_ALLOWED_HOSTS` | the service hostname, e.g. `falconbridge-sba-foia.onrender.com` (check it after creation; Render may add a suffix) |

   The first deploy starts in **maintenance mode**: the data isn't there yet, and nothing is exposed.
6. **Render: add your SSH public key** (you). Account Settings → SSH Public Keys → add the contents of a `.pub` file
   (for example `~/.ssh/fbc_render_ed25519.pub`, if that key is yours to reuse).
7. **Upload the data and template** (Claude or you, from the project folder; about 775 MB). Use
   `-i ~/.ssh/fbc_render_ed25519` if that's the key you added.

       ssh SRV 'mkdir -p /var/data/source_data/20260630 /var/data/template /var/data/config'
       scp -s source_data/20260630/* SRV:/var/data/source_data/20260630/
       scp -s "reference/SBA_Historical Performance_FinWise Bank_2026Q2.xlsx" SRV:/var/data/template/
       scp -s protected_config/source_notes.json SRV:/var/data/config/
       ssh SRV 'cd /var/data && sha256sum -c /opt/render/project/src/deploy/DATA_MANIFEST.sha256 && chmod 600 template/* config/*'

8. **Restart the service** (you). Manual Deploy → Deploy latest commit, or Restart. The start script verifies the data
   and starts the dashboard behind the sign-in.
9. **Verify** (Claude, from the project folder; about 2 minutes). Expect **all checks PASS**:

       FBX_VERIFY_PASSWORD='<password>' python deploy/verify_deployment.py https://<hostname> <username> --validated output/20260630

   The checks cover: health; every route refused without sign-in; bank list; US Metro figures; a workbook generated
   and downloaded; byte-for-byte parity with the validated workbook apart from documented metadata.
10. **Share** (you). Send the client the URL and her username. The password goes separately.
    - To revoke: replace `FBX_PREVIEW_USERS` and restart.
    - To end the preview: suspend or delete the service.

## Notes

- **First request after each restart** builds the caches (about 1 minute). Run step 9 after each deploy before telling
  the client it's ready.
- **Each workbook** is generated in its own process with the 300-second limit. Expect 30–50 seconds.
- **Deploys** cause a few seconds of downtime (a disk prevents zero-downtime deploys).
- **Saved analyses and generated workbooks** belong to the preview user who created them; other users can't list or
  download them. Organization permissions for prospective banks are deferred.
- **Generated workbooks** record the server's data folder (`/var/data/source_data/20260630`) in Generation_Info and
  Benchmark_Notes.
