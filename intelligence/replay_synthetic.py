"""Replay recording for the SYNTHETIC investigation demonstration.

HAND-AUTHORED FIXTURE: this is NOT the output of any AI model. It stands in for a model's structured answer so the
pipeline (brief -> provider -> M2 verifier -> analyst review) can be demonstrated and tested offline at no cost. It
contains well-formed items and deliberately defective ones (a wrong number, a cause stated as fact, an invented
FalconBridge criterion, a fabricated citation, a wrong calculation, an evaluative conclusion and a malformed item)
so the controls can be seen working. It describes only the synthetic lender of webapp/synthetic_analysis.py.

The recording is keyed by the exact request fingerprint (task, prompt version, instructions and brief); if any of
these change, the replay provider refuses with ReplayMiss instead of reusing a stale answer.
"""
PROVENANCE = "replay fixture (hand-authored for the synthetic demonstration; not output of any AI model)"
B = "bank.fy2000.total.total.cum_def"
S = "benchmark.total.total.cum_def"
R = "Full-Service Restaurants"
H = "Hotels (except Casino Hotels) and Motels"
T = "General Freight Trucking, Long-Distance, Truckload"

ITEMS = [
    {"type": "COMPARISON", "cites": [B, S],
     "text": "For loans approved FY2000 onward, the bank's % Cum. Defaulted is 3.07%, 2.93 percentage points lower "
             "than the preliminary SBA benchmark's 6.00%.",
     "calculation": {"operation": "difference", "operands": [B, S], "result": -0.02929874899575347, "unit": "ratio"}},
    {"type": "OBSERVATION", "cites": ["seasoning.2022-2026.bank_share", "seasoning.2022-2026.sba_share"],
     "text": "53.52% of the bank's original amount was first disbursed in 2022-2026, compared with 27.00% for the "
             "SBA benchmark."},
    {"type": "COMPARISON", "cites": ["bank.fy2000.total.vintage.2008.cum_def", "bank.fy2000.total.vintage.2008.loans",
                                     "benchmark.total.vintage.2008.cum_def"],
     "text": "Loans first disbursed in 2008 have a % Cum. Defaulted of 46.67%, 25.67 percentage points higher than "
             "the benchmark's 21.00% for that vintage; the vintage holds 10 loans.",
     "calculation": {"operation": "difference", "operands": ["bank.fy2000.total.vintage.2008.cum_def",
                                                             "benchmark.total.vintage.2008.cum_def"],
                     "result": 0.2567, "unit": "ratio"}},                          # rounded, as a model writes it
    {"type": "COMPARISON", "cites": ["bank.fy2000.below.total.cum_def", "benchmark.below.total.cum_def",
                                     "compare.below.total.cum_def.diff"],
     "text": "Loans at or below $350,000 approved FY2000 onward have a % Cum. Defaulted of 11.10%, 2.60 percentage "
             "points higher than the benchmark's 8.50% for small loans."},
    {"type": "OBSERVATION", "cites": [f"bank.full.total.industry.{H}.share"],
     "text": f"{H} account for 70.81% of the bank's original amount."},
    {"type": "COMPARISON", "cites": [f"bank.fy2000.total.industry.{R}.cum_def", f"bank.fy2000.total.industry.{R}.loans",
                                     f"benchmark.total.industry.{R}.cum_def", f"compare.total.industry.{R}.cum_def.diff"],
     "text": f"{R} loans have a % Cum. Defaulted of 14.87%, 4.87 percentage points higher than the benchmark's "
             "10.00% for the industry (20 loans)."},
    {"type": "HYPOTHESIS", "cites": ["seasoning.2022-2026.bank_share", B],
     "text": "The lower overall % Cum. Defaulted may partly reflect the younger portfolio: 53.52% of the bank's "
             "original amount was disbursed in 2022-2026, and recent vintages have had less time to default."},
    {"type": "HYPOTHESIS", "cites": ["compare.below.total.cum_def.diff", f"bank.full.total.industry.{R}.cum_def",
                                     f"bank.full.total.industry.{T}.cum_def"],
     "text": "The higher result for loans at or below $350,000 could relate to the restaurant and trucking exposure "
             "in that segment."},
    {"type": "QUESTION", "cites": ["bank.full.total.vintage.2008.cum_def"],
     "text": "What changes did the bank make to underwriting or loan servicing after the 2008 vintage?"},
    {"type": "MISSING_INFORMATION", "cites": ["seasoning.2022-2026.bank_share"],
     "text": "Are current outstanding balances and delinquency data available for the 2022-2026 vintages, which "
             "hold 53.52% of the original amount?"},
    {"type": "MISSING_INFORMATION", "cites": [],
     "text": "Can FalconBridge confirm the benchmark methodology so that the comparison can be used beyond a "
             "preliminary view?"},
    {"type": "LIMITATION", "cites": ["limitation.partial_year"],
     "text": "2026 is a partial year, so its vintage figures cover loans first disbursed through the release date "
             "only."},
    # ---- deliberately defective items (the controls must catch each one)
    {"type": "OBSERVATION", "cites": [B],
     "text": "The bank's % Cum. Defaulted for loans approved FY2000 onward is 2.70%."},              # wrong number
    {"type": "COMPARISON", "cites": ["compare.total.total.cum_loss.diff"],
     "text": "The bank outperforms the SBA benchmark on credit losses."},                           # evaluative
    {"type": "OBSERVATION", "cites": ["compare.total.total.cum_def.diff"],
     "text": "The lower default rate is due to stronger underwriting at the bank."},                # cause as fact
    {"type": "OBSERVATION", "cites": [B],
     "text": "With a % Cum. Defaulted of 3.07% the bank qualifies for FalconBridge's preferred lender tier."},
    {"type": "OBSERVATION", "cites": ["bank.full.total.vintage.2009.cum_def"],
     "text": "The bank's 2009 vintage % Cum. Defaulted is 0.00%."},                                 # no such evidence
    {"type": "COMPARISON", "cites": [f"bank.full.total.industry.{H}.cum_def", f"benchmark.total.industry.{H}.cum_def"],
     "text": f"{H} loans have a % Cum. Defaulted 0.50 percentage points lower than the benchmark's 3.50%.",
     "calculation": {"operation": "difference", "operands": [f"bank.full.total.industry.{H}.cum_def",
                                                             f"benchmark.total.industry.{H}.cum_def"],
                     "result": -0.005, "unit": "ratio"}},                                          # wrong arithmetic
    {"type": "COMPARISON", "cites": ["bank.fy2000.total.vintage.2008.cum_def", "benchmark.total.vintage.2010.cum_def"],
     "text": "Loans first disbursed in 2008 have a % Cum. Defaulted of 46.67%, against the benchmark's 9.00%."},  # 2008 vs 2010
    {"type": "OBSERVATION", "cites": ["bank.full.total.total.age"],
     "text": "The bank's portfolio has an average age of 43.07 months."},                     # outstanding loans only
    {"type": "SPECULATION", "text": "Default rates will fall next year."},                         # malformed type
]
CONTENT = {"items": ITEMS}


# Fingerprint of the demonstration request this fixture was written for (synthetic pack, PROMPT_VERSION
# investigation-1.0). A change to the brief, the instructions or the prompt version changes the fingerprint, and the
# replay provider then refuses (ReplayMiss) rather than answer a different question with this recording.
RECORDED_FOR = "99e7b6cfa461524b92e7c67be7c511cecd948bbe351d829cbf4264c5f31d8880"   # re-pinned 2026-11-06: brief adds FY2000+ vintages and industries


def recordings():
    return {RECORDED_FOR: {"model": "replay-fixture", "content": CONTENT, "provenance": PROVENANCE}}
