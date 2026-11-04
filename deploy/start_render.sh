#!/usr/bin/env bash
# Render start command (FalconBridge SBA FOIA dashboard). Fails closed:
#  1. the confidential template, protected notes and public FOIA files must be on the persistent disk and match
#     deploy/DATA_MANIFEST.sha256 - they are never in GitHub or in the build. Until they are installed, a maintenance
#     responder answers /healthz (so Render keeps the instance up for the upload) and 503 to everything else;
#  2. the app is started through `python -m webapp`, which refuses to listen publicly unless FBX_AUTH_MODE=preview
#     with at least one FBX_PREVIEW_USERS credential (every route except /healthz then requires sign-in).
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${FBX_DATA_ROOT:-/var/data}"

echo "Verifying data on ${DATA} against deploy/DATA_MANIFEST.sha256 ..."
SUM=$(command -v sha256sum >/dev/null && echo "sha256sum" || echo "shasum -a 256")
if ! (cd "$DATA" && $SUM -c "$OLDPWD/deploy/DATA_MANIFEST.sha256"); then
  echo "DATA NOT INSTALLED: the template, notes or FOIA files on ${DATA} are missing or do not match the manifest." >&2
  echo "Maintenance mode: /healthz only; every other request gets 503. Upload the data, then restart the service." >&2
  exec python - "${PORT:?Render sets PORT}" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/healthz"
        body = b'{"status":"maintenance"}' if ok else b"Not available."
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json" if ok else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_POST = do_HEAD = do_GET
    def log_message(self, *a):
        pass
HTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
PY
fi

# Point the generator's fixed locations at the disk (no generator change): source_data/, reference/ (template only),
# output/.cache (lender directory and benchmark caches).
ln -sfn "$DATA/source_data" source_data
ln -sfn "$DATA/template" reference
mkdir -p output "$DATA/cache" "$DATA/app_data"
ln -sfn "$DATA/cache" output/.cache

export FBX_APP_DATA="${FBX_APP_DATA:-$DATA/app_data}"
export FBX_HOST=0.0.0.0
export FBX_PORT="${PORT:?Render sets PORT}"
exec python -m webapp
