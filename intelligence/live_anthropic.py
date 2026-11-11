"""Live Anthropic provider for the FIRST CONTROLLED EXPERIMENT (one synthetic investigation, approved 2026-11-06).

This is the only module that may use the Anthropic SDK, and it imports it only inside `_client()`, after every
check has passed. It plugs into intelligence/provider.py's interface, so everything upstream still applies:
the investigation refuses non-synthetic evidence and requires an ExternalAuthorization for the exact pack
(investigation.py), the brief carries keys / values / units / caveat codes only and is scanned for forbidden terms,
and the BudgetLedger authorises the WORST-CASE cost before the call and records the actual cost after it.

Call-level rules (in addition):
  * model must have a known price (provider.PRICES; confirmed against Anthropic's pricing page on 2026-11-06);
  * max output tokens <= MAX_OUTPUT_TOKENS (8,000, thinking included);
  * FBX_AI_LIVE must be "enabled" and ANTHROPIC_API_KEY present in the environment (the key is never read into
    application state, logged or stored: the SDK reads it from the environment itself);
  * no automatic retries (max_retries=0), so one attempt is one charge; default global routing (no inference_geo);
  * the model must answer in the investigation's JSON schema; refusals, truncation or unparseable output yield an
    empty item list, are still charged and keep the raw text for the audit record.
"""
import json
import math
import os

from . import provider as PV
from .evidence import canonical_json

MAX_OUTPUT_TOKENS = 8000
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
TIMEOUT_SECONDS = 300.0
# Input-token budgeting, calibrated on the first live run (2026-11-06): the exact API parameters (system, user message
# and the JSON output schema) were 24,189 characters and billed as 10,270 input tokens (2.36 characters per token);
# the earlier estimate (instructions + brief only, 4 characters per token) said 5,133. The bound below assumes a more
# token-dense 2.0 characters per token over EVERYTHING sent, plus a 25% margin.
CHARS_PER_TOKEN = 2.0
INPUT_MARGIN = 1.25
OBSERVED_BASELINE = {"characters": 24189, "input_tokens": 10270, "output_tokens": 3967, "cost_usd": 0.12042}
USER_PREAMBLE = ("Evidence brief (JSON). Use only these items; cite their keys. Answer with the JSON object described "
                 "in the instructions.\n")
ITEM_TYPES = ["OBSERVATION", "COMPARISON", "HYPOTHESIS", "QUESTION", "LIMITATION", "MISSING_INFORMATION"]
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ITEM_TYPES},
            "text": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string"}},
            "calculation": {"anyOf": [{"type": "null"}, {
                "type": "object",
                "properties": {"operation": {"type": "string", "enum": ["difference", "sum", "ratio"]},
                               "operands": {"type": "array", "items": {"type": "string"}},
                               "result": {"type": "number"},
                               "unit": {"type": "string", "enum": ["ratio", "count", "usd"]}},
                "required": ["operation", "operands", "result", "unit"], "additionalProperties": False}]}},
        "required": ["type", "text", "cites", "calculation"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}


def message_params(request, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT):
    """Exactly what is sent to the API for this request (used for the call and shown in the pre-flight summary)."""
    return {"model": model, "max_tokens": request.max_output_tokens, "system": request.instructions,
            "messages": [{"role": "user", "content": USER_PREAMBLE + canonical_json(request.brief)}],
            "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}}


def actual_cost(usage, model):
    pin, pout = PV.PRICES[model]
    tokens_in = sum(int(getattr(usage, k, 0) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                                "cache_read_input_tokens"))
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    return tokens_in, tokens_out, (tokens_in * pin + tokens_out * pout) / 1e6      # cache tokens at the full rate


def estimate_input_tokens(request, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT):
    chars = len(canonical_json(message_params(request, model, effort)))
    return math.ceil(chars / CHARS_PER_TOKEN * INPUT_MARGIN)


class AnthropicProvider:
    external = True
    name = "anthropic"

    def __init__(self, model=DEFAULT_MODEL, effort=DEFAULT_EFFORT, client=None):
        if model not in PV.PRICES:
            raise PV.ProviderError(f"no confirmed price for model {model!r}")
        self.model, self.effort, self._injected = model, effort, client      # client: offline tests only

    def estimate_cost(self, request):
        if request.max_output_tokens > MAX_OUTPUT_TOKENS:
            return math.inf                                                   # refused by the ledger
        return PV.worst_case_cost(request, self.model, estimate_input_tokens(request, self.model, self.effort))

    def _client(self):
        if not PV.live_switch_on():
            raise PV.LiveCallsDisabled(f"live AI calls are disabled ({PV.LIVE_SWITCH_ENV} is not 'enabled')")
        if self._injected is not None:
            return self._injected
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise PV.LiveCallsDisabled("ANTHROPIC_API_KEY is not set in this process's environment")
        import anthropic                                                      # only here, after every check
        return anthropic.Anthropic(max_retries=0, timeout=TIMEOUT_SECONDS)

    def complete(self, request):
        if request.max_output_tokens > MAX_OUTPUT_TOKENS:
            raise PV.ProviderError(f"max output tokens {request.max_output_tokens} exceeds {MAX_OUTPUT_TOKENS}")
        client = self._client()
        msg = client.messages.create(**message_params(request, self.model, self.effort))
        tokens_in, tokens_out, cost = actual_cost(msg.usage, self.model)
        text = "".join(getattr(b, "text", "") for b in (msg.content or []) if getattr(b, "type", "") == "text")
        content = {"items": [], "raw_text": text, "stop_reason": msg.stop_reason}
        if msg.stop_reason == "refusal":
            content["error"] = "refusal"
        elif msg.stop_reason == "max_tokens":
            content["error"] = "output truncated at the max output token limit"
        else:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                    content["items"] = parsed["items"]
                else:
                    content["error"] = "answer is not the expected JSON object"
            except ValueError as e:
                content["error"] = f"answer is not valid JSON ({e})"
        return PV.ProviderResponse(request.fingerprint(), self.name, getattr(msg, "model", self.model), content,
                                   tokens_in, tokens_out, cost,
                                   f"live {getattr(msg, 'model', self.model)} message {getattr(msg, 'id', '?')}; "
                                   f"stop_reason {msg.stop_reason}; effort {self.effort}")
