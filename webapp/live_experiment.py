"""First controlled live AI experiment: ONE investigation of the SYNTHETIC Demonstration Bank.

  Pre-flight (default; no call, no network):
      .venv/bin/python -m webapp.live_experiment --out reports/live_experiment_2026-11-06
  Execute (only after the approver has reviewed the pre-flight request):
      FBX_AI_LIVE=enabled ANTHROPIC_API_KEY=... .venv/bin/python -m webapp.live_experiment \\
          --out reports/live_experiment_2026-11-06 --execute --approved-by NAME --approved-on YYYY-MM-DD \\
          --confirm-request <request fingerprint shown by the pre-flight>

Safeguards in force: synthetic evidence only (checked from the evidence), ExternalAuthorization bound to the exact
pack, external brief without free text and scanned, $2.00 ledger with a one-call limit, 8,000 output tokens, kill
switch, M2 verification of every item, nothing accepted without an analyst decision (none is made here), audit
bundle + replay recording written, offline replay check. The API key is never read, printed or stored here.
"""
import argparse
import json
import os
import sys

from intelligence import investigation as INV
from intelligence import live_anthropic as LA
from intelligence import provider as PV

from . import intel_adapter as IA
from . import synthetic_analysis as SY

BUDGET_USD = 2.00
MAX_OUTPUT_TOKENS = 8000
PURPOSE = "first controlled live AI experiment: one synthetic investigation (approved 2026-11-06)"
RESULT = "live_investigation_audit.json"


def build():
    pack = IA.extract(SY.synthetic_saved())
    if not INV.is_synthetic(pack):
        raise SystemExit("refused: the evidence pack is not synthetic")
    brief = INV.build_brief(pack, external=True)
    request = PV.ProviderRequest(INV.TASK, INV.PROMPT_VERSION, INV.INSTRUCTIONS, brief, MAX_OUTPUT_TOKENS)
    INV._scan({"instructions": INV.INSTRUCTIONS, "brief": brief}, pack)       # raises on forbidden content
    return pack, request


def preflight(out):
    pack, request = build()
    params = LA.message_params(request)
    est_in = LA.estimate_input_tokens(request)
    worst = PV.worst_case_cost(request, LA.DEFAULT_MODEL, est_in)
    summary = {
        "request_fingerprint": request.fingerprint(), "pack_fingerprint": pack.fingerprint(),
        "evidence": f"{pack.header['bank'][0]} (synthetic; {len(pack.records)} records, all UNVERIFIED)",
        "brief_items": len(request.brief["items"]),
        "brief_dimensions": sorted({i["dimension"] for i in request.brief["items"]}),
        "brief_fields": sorted({k for i in request.brief["items"] for k in i}),
        "model": params["model"], "effort": params["output_config"]["effort"], "max_tokens": params["max_tokens"],
        "structured_output": "json_schema (investigation items)", "retries": 0, "routing": "global (default)",
        "estimated_input_tokens": est_in,
        "estimate_method": f"all API parameters / {LA.CHARS_PER_TOKEN} chars per token x {LA.INPUT_MARGIN} "
                           f"(calibrated on the first live run: {LA.OBSERVED_BASELINE['input_tokens']:,} actual input "
                           "tokens)",
        "worst_case_cost_usd": round(worst, 4), "budget_usd": BUDGET_USD, "call_limit": 1,
        "price_per_mtok_usd": dict(zip(("input", "output"), PV.PRICES[params["model"]])),
        "forbidden_content_scan": "passed",
        "environment": {"FBX_AI_LIVE": "enabled" if PV.live_switch_on() else "not set",
                        "ANTHROPIC_API_KEY": "present" if os.environ.get("ANTHROPIC_API_KEY") else "not set"},
        "sdk": "anthropic==2.6.0",
    }
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "preflight_request.json"), "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "exact_api_parameters": params}, f, indent=1)
    return summary


def execute(out, approved_by, approved_on, confirm):
    if any(os.path.exists(os.path.join(out, f)) for f in (RESULT, "ledger.json")):
        raise SystemExit("refused: this one-time experiment has already been attempted (a result or ledger exists)")
    pack, request = build()
    if not confirm or not request.fingerprint().startswith(confirm) or len(confirm) < 16:
        raise SystemExit("refused: --confirm-request does not match the reviewed request fingerprint")
    auth = INV.ExternalAuthorization(pack.fingerprint(), approved_by, approved_on, PURPOSE, BUDGET_USD)
    ledger = PV.BudgetLedger(BUDGET_USD, max_calls=1)
    try:
        inv = INV.investigate(pack, LA.AnthropicProvider(), ledger, authorization=auth,
                              max_output_tokens=MAX_OUTPUT_TOKENS)
    finally:                                                                # spending is recorded even on failure
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "ledger.json"), "w", encoding="utf-8") as f:
            json.dump({"limit_usd": ledger.limit_usd, "spent_usd": ledger.spent_usd, "killed": ledger.killed,
                       "entries": ledger.entries}, f, indent=1)
    bundle = INV.audit_bundle(inv, ledger)
    with open(os.path.join(out, RESULT), "w", encoding="utf-8") as f:
        json.dump(bundle, f, indent=1)
    _, diffs = INV.replay_bundle(bundle, pack)                              # must reproduce offline
    return inv, ledger, diffs


def main(argv=None):
    ap = argparse.ArgumentParser(description="First controlled live AI experiment (synthetic only)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--approved-by", default="")
    ap.add_argument("--approved-on", default="")
    ap.add_argument("--confirm-request", default="")
    a = ap.parse_args(argv)
    if not a.execute:
        print(json.dumps(preflight(a.out), indent=1))
        return 0
    inv, ledger, diffs = execute(a.out, a.approved_by, a.approved_on, a.confirm_request)
    print(json.dumps({"investigation": inv.investigation_id, "summary": INV.summary(inv), "cost_usd": ledger.spent_usd,
                      "killed": ledger.killed, "replay_differences": len(diffs)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
