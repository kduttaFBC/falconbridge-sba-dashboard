"""Offline, end-to-end demonstration of an evidence-governed AI investigation on SYNTHETIC data (M3, Phase C).

  python -m webapp.investigation_demo [--out FOLDER]

SYNTHETIC lender (webapp/synthetic_analysis.py) -> evidence pack (intel_adapter) -> evidence brief -> replay provider
(intelligence/replay_synthetic.py: hand-authored, not model output; $0; no network) -> M2 verifier -> analyst review
queue -> scripted analyst decisions -> walkthrough (Markdown + JSON). No real bank evidence, FalconBridge workbook
content or client information is used, and nothing is sent anywhere.
"""
import argparse
import json
import os
import sys

from intelligence import investigation as INV
from intelligence import provider as PV
from intelligence import replay_synthetic as RS

from . import intel_adapter as IA
from . import synthetic_analysis as SY

# the analyst's decisions in the demonstration (reviewer and notes are part of the record)
DECISIONS = [
    ("AI-01", "APPROVE", "Matches the evidence; keep with the preliminary-benchmark and portfolio-age caveats."),
    ("AI-02", "APPROVE", "Portfolio age context for every overall comparison."),
    ("AI-03", "APPROVE", "Material: the oldest vintage is far above the benchmark for that year."),
    ("AI-04", "APPROVE", "Material: small loans are above the small-loan benchmark."),
    ("AI-05", "APPROVE", "Concentration fact."),
    ("AI-06", "APPROVE", "Industry fact; 20 loans, so treat with care."),
    ("AI-07", "APPROVE", "Plausible and consistent with the age shares; to be tested vintage by vintage."),
    ("AI-08", "REJECT", "Not established: the evidence does not split small loans by industry; ask the bank instead."),
    ("AI-09", "APPROVE", "Send to the bank."),
    ("AI-10", "APPROVE", "Send to the bank."),
    ("AI-11", "APPROVE", "Raise with FalconBridge."),
    ("AI-12", "APPROVE", "Standard limitation."),
    ("AI-14", "REJECT", "Evaluative conclusion; FalconBridge has no approved criteria and the benchmark is "
                        "preliminary."),
    ("AI-13", "APPROVE", "(attempt) The system must refuse: the item failed verification."),
]


def run(reviewer="Analyst (demonstration)", at="2026-11-06T12:00:00"):
    saved = SY.synthetic_saved()
    pack = IA.extract(saved)
    ledger = PV.BudgetLedger(limit_usd=0.0)
    inv = INV.investigate(pack, PV.ReplayProvider(RS.recordings()), ledger, synthetic=True)
    log = []
    for item_id, decision, note in DECISIONS:
        try:
            INV.decide(inv, item_id, decision, reviewer, note, at)
            log.append({"item": item_id, "decision": decision, "result": "recorded"})
        except INV.InvestigationError as e:
            log.append({"item": item_id, "decision": decision, "result": f"refused: {e}"})
    return saved, pack, inv, ledger, log


def walkthrough(saved, pack, inv, ledger, log):
    brief = INV.build_brief(pack)
    s = INV.summary(inv)
    L = [f"# Synthetic AI investigation walkthrough: {saved['criteria']['bank_name']}", "",
         "**SYNTHETIC DATA.** Invented lender and figures (webapp/synthetic_analysis.py); the model answer is a "
         "hand-authored replay recording, not output of any AI model. Offline; no network; cost $0.00.", "",
         "## 1. Evidence pack and brief",
         f"- Evidence pack: {len(pack.records):,} records, fingerprint `{pack.fingerprint()[:16]}`; every record is "
         "UNVERIFIED (synthetic data is outside the validated calculation range).",
         f"- Brief sent to the provider: {len(brief['items'])} evidence items across "
         f"{', '.join(sorted({i['dimension'] for i in brief['items']}))}.",
         f"- Request fingerprint `{inv.request_fingerprint[:16]}`; provider `{inv.provider}` ({inv.provenance}); "
         f"budget ledger: ${ledger.spent_usd:.2f} of ${ledger.limit_usd:.2f}.", "",
         "## 2. What the model returned and what the controls did", "",
         "| Item | Type | Statement | Verifier | State after review | Standing |", "|---|---|---|---|---|---|"]
    for it in inv.items:
        why = "; ".join(f.split(":")[0] for f in it.verifier_findings) or "all checks passed"
        L.append(f"| {it.item_id} | {it.label} | {it.text} | {it.verifier_outcome} ({why}) | {it.state} | "
                 f"{it.standing} |")
    for m in inv.malformed:
        L.append(f"| item {m['position']} | malformed | (not processed) | {m['reason']} | REFUSED | NONE |")
    L += ["", "## 3. Evidence behind each approved statement", ""]
    for it in inv.items:
        if it.state == "ANALYST_APPROVED":
            L.append(f"- **{it.item_id}** cites {', '.join(f'`{c}`' for c in it.cites) or '(no evidence: a question)'}; "
                     f"validation {it.status}; methodology {it.methodology}; caveats "
                     f"{', '.join(it.caveats) or 'none'}. Decision: {it.decision['meaning']}. Note: {it.decision['note']}")
    L += ["", "## 4. Analyst decisions", ""]
    L += [f"- {x['item']} {x['decision']}: {x['result']}" for x in log]
    L += ["", "## 5. Coverage and summary", "",
          f"- Dimensions examined by verified items: {', '.join(inv.coverage['examined'])}; not examined: "
          f"{', '.join(inv.coverage['not_examined']) or 'none'}.",
          f"- States: {json.dumps(s['by_state'])}; standing after review: {json.dumps(s['by_standing'])}; "
          f"malformed items: {s['malformed']}; items still undecided: {s['undecided']}.",
          "- Only statements the verifier accepted outright can become ESTABLISHED_FACT; anything sent to review "
          "(such as \"outperforms the benchmark\") can at most be a PROPOSED_INTERPRETATION.", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    saved, pack, inv, ledger, log = run()
    md = walkthrough(saved, pack, inv, ledger, log)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "synthetic_investigation.md"), "w", encoding="utf-8") as f:
            f.write(md)
        bundle = INV.audit_bundle(inv, ledger)
        with open(os.path.join(a.out, "synthetic_investigation_audit.json"), "w", encoding="utf-8") as f:
            json.dump(bundle, f, indent=1)
        _, diffs = INV.replay_bundle(bundle, pack)                  # the record must reproduce exactly
        print(f"audit bundle {bundle['bundle_sha256'][:16]}: replay differences = {len(diffs)}")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
