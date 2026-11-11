"""Provider-independent model interface with offline replay, a budget ledger and a live-call lock (M3).

No SDK is imported and no network connection is made anywhere in this module. A provider receives a
ProviderRequest (task, prompt version, instructions, evidence brief) and returns a ProviderResponse whose `content`
is the model's structured answer. Two providers exist:

  ReplayProvider  returns a recorded response for the exact request fingerprint; a request it has not recorded is a
                  ReplayMiss (it never falls through to a live call). Cost: $0.
  LiveProvider    a placeholder that always refuses: live calls are disabled until FalconBridge approves the egress
                  classes, the SDK installation and the budget. It exists so the call path is visible and testable.

Every response is charged to a BudgetLedger before it is used; the ledger refuses (kill switch) any call whose
estimated cost would exceed its limit, and a limit of $0 refuses every paid call.
"""
import math
import os
from dataclasses import dataclass, field

from .evidence import canonical_json, sha256_of

HARD_CAP_USD = 2.00                         # the first controlled experiment can never be authorised above this
LIVE_SWITCH_ENV = "FBX_AI_LIVE"             # live calls additionally require this to be "enabled" (default: unset)
# USD per million tokens (input, output), from the provider's published price list; thinking tokens bill as output
# confirmed 2026-11-06 against platform.claude.com/docs/en/about-claude/pricing (global routing; no data-residency
# multiplier); Haiku's price is the <=100K-token prompt rate
PRICES = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


def live_switch_on():
    return os.environ.get(LIVE_SWITCH_ENV, "") == "enabled"


def worst_case_cost(request, model, input_tokens=None):
    """Upper bound for one call: input tokens (a provider-specific calibrated estimate when given, else the generic
    estimate with a 25% margin) plus the FULL output allowance at the output price, so even a maximum-length answer,
    thinking included, cannot exceed what was authorised."""
    if model not in PRICES:
        return math.inf
    pin, pout = PRICES[model]
    tokens_in = input_tokens if input_tokens is not None else request.estimated_input_tokens() * 1.25
    return (tokens_in * pin + request.max_output_tokens * pout) / 1e6


class ProviderError(RuntimeError):
    pass


class ReplayMiss(ProviderError):
    pass


class LiveCallsDisabled(ProviderError):
    pass


class BudgetExceeded(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderRequest:
    task: str
    prompt_version: str
    instructions: str
    brief: dict
    max_output_tokens: int = 4000

    def fingerprint(self):
        return sha256_of({"task": self.task, "prompt_version": self.prompt_version, "instructions": self.instructions,
                          "brief": self.brief, "max_output_tokens": self.max_output_tokens})

    def estimated_input_tokens(self):
        return len(canonical_json({"i": self.instructions, "b": self.brief})) // 4 + 1


@dataclass(frozen=True)
class ProviderResponse:
    request_fingerprint: str
    provider: str
    model: str
    content: dict
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    provenance: str = ""             # e.g. "replay fixture (hand-authored, not model output)"


@dataclass
class BudgetLedger:
    """Disabled by default ($0). A limit above HARD_CAP_USD is refused. kill() stops all further calls, and the ledger
    kills itself when spending reaches the limit or a response costs more than was authorised."""
    limit_usd: float = 0.0
    spent_usd: float = 0.0
    entries: list = field(default_factory=list)
    killed: str = ""
    max_calls: int = 0                 # 0 = unlimited (replay); the live experiment uses 1

    def __post_init__(self):
        if not 0 <= self.limit_usd <= HARD_CAP_USD:
            raise BudgetExceeded(f"budget ${self.limit_usd:.2f} is outside $0-${HARD_CAP_USD:.2f}")

    def kill(self, reason):
        self.killed = reason or "kill switch"

    def authorise(self, request, estimated_cost_usd):
        if self.killed:
            raise BudgetExceeded(f"kill switch engaged: {self.killed}")
        if self.max_calls and len(self.entries) >= self.max_calls:
            raise BudgetExceeded(f"call limit reached ({self.max_calls})")
        if not (estimated_cost_usd >= 0 and math.isfinite(estimated_cost_usd)):
            raise BudgetExceeded("no finite cost estimate: refused")
        if self.spent_usd + estimated_cost_usd > self.limit_usd + 1e-12:
            raise BudgetExceeded(f"kill switch: estimated ${estimated_cost_usd:.4f} would exceed the remaining budget "
                                 f"(${self.limit_usd - self.spent_usd:.4f} of ${self.limit_usd:.2f})")

    def charge(self, response, authorised_usd=None):
        self.spent_usd += response.cost_usd                  # money already spent is always recorded
        over = self.spent_usd > self.limit_usd + 1e-12
        if authorised_usd is not None and response.cost_usd > authorised_usd + 1e-12:
            self.kill(f"response cost ${response.cost_usd:.4f} exceeded the authorised ${authorised_usd:.4f}")
        if over:
            self.kill("budget exhausted")
        elif self.limit_usd and self.spent_usd >= self.limit_usd - 1e-12:
            self.kill("budget fully used")
        self.entries.append({"request": response.request_fingerprint, "provider": response.provider,
                             "model": response.model, "cost_usd": response.cost_usd,
                             "input_tokens": response.input_tokens, "output_tokens": response.output_tokens,
                             "spent_usd": self.spent_usd, "killed": self.killed})
        if over:
            raise BudgetExceeded("response cost exceeds the budget; kill switch engaged")


class ReplayProvider:
    """Offline provider: answers only requests it holds a recording for, keyed by the request fingerprint."""
    name, external = "replay", False

    def __init__(self, recordings):
        self.recordings = dict(recordings)            # {request fingerprint: {"model", "content", "provenance"}}

    def estimate_cost(self, request):
        return 0.0

    def complete(self, request):
        fp = request.fingerprint()
        rec = self.recordings.get(fp)
        if rec is None:
            raise ReplayMiss(f"no recorded response for request {fp[:16]} (the brief, instructions or prompt version "
                             "changed since recording); nothing was sent anywhere")
        return ProviderResponse(fp, self.name, rec.get("model", "replay"), rec["content"],
                                request.estimated_input_tokens(), len(canonical_json(rec["content"])) // 4 + 1, 0.0,
                                rec.get("provenance", "replay recording"))


class LiveProvider:
    """Placeholder for a future approved provider. Always refuses: no SDK, no network, no paid call."""
    external = True

    def __init__(self, name="anthropic", model="claude-opus-5-5"):
        self.name, self.model = name, model

    def estimate_cost(self, request):
        return worst_case_cost(request, self.model)

    def complete(self, request):
        if not live_switch_on():
            raise LiveCallsDisabled(f"live AI calls are disabled ({LIVE_SWITCH_ENV} is not 'enabled')")
        raise LiveCallsDisabled("live AI calls are not implemented in this build: no SDK is installed and no client "
                                "is wired; this requires FalconBridge's approval")


def call(provider, request, ledger):
    """The only way the investigation reaches a provider: budget first, then the call, then the charge."""
    estimate = provider.estimate_cost(request)
    ledger.authorise(request, estimate)
    response = provider.complete(request)
    ledger.charge(response, authorised_usd=estimate)      # recorded (and the kill switch checked) before any use
    if response.request_fingerprint != request.fingerprint():
        raise ProviderError("response does not belong to this request")
    return response
