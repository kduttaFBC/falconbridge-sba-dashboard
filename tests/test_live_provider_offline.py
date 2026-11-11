"""Live provider for the first controlled experiment, tested OFFLINE: a fake SDK client stands in for the API, the
network is blocked, and no credential is used. Nothing here can reach Anthropic."""
import ast
import json
import socket
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from intelligence import investigation as INV
from intelligence import live_anthropic as LA
from intelligence import provider as PV
from intelligence import replay_synthetic as RS
from webapp import intel_adapter as IA
from webapp import live_experiment as LX
from webapp import synthetic_analysis as SY

ROOT = Path(__file__).resolve().parents[1]
DUMMY_KEY = "sk-ant-TEST-NOT-A-REAL-KEY-0000"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.delenv(PV.LIVE_SWITCH_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


class FakeClient:
    def __init__(self, content=None, stop_reason="end_turn", text=None, usage=(5000, 3000)):
        self.calls = []
        self.messages = self
        self._text = text if text is not None else json.dumps(content if content is not None else RS.CONTENT)
        self._stop, self._usage = stop_reason, usage

    def create(self, **kw):
        self.calls.append(kw)
        return NS(id="msg_fake", model=kw["model"], stop_reason=self._stop, content=[NS(type="text", text=self._text)],
                  usage=NS(input_tokens=self._usage[0], output_tokens=self._usage[1], cache_creation_input_tokens=0,
                           cache_read_input_tokens=None))


@pytest.fixture(scope="module")
def pack():
    return IA.extract(SY.synthetic_saved())


def auth(pack, **kw):
    d = dict(pack_fingerprint=pack.fingerprint(), approved_by="test", approved_on="2026-11-06", purpose="test",
             max_usd=2.0)
    d.update(kw)
    return INV.ExternalAuthorization(**d)


def test_sdk_is_imported_only_inside_the_live_provider_call():
    for f in (ROOT / "intelligence").glob("*.py"):
        tree = ast.parse(f.read_text())
        top = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names} | \
              {n.module for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
        assert "anthropic" not in top, f.name
        anywhere = [n for n in ast.walk(tree) if isinstance(n, ast.Import) and any(a.name == "anthropic" for a in n.names)]
        assert not anywhere or f.name == "live_anthropic.py", f.name


def test_request_sent_to_the_api_is_exactly_the_reviewed_one(pack, monkeypatch):
    monkeypatch.setenv(PV.LIVE_SWITCH_ENV, "enabled")
    fake = FakeClient()
    inv = INV.investigate(pack, LA.AnthropicProvider(client=fake), PV.BudgetLedger(2.0, max_calls=1),
                          authorization=auth(pack), max_output_tokens=8000)
    (kw,) = fake.calls
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 8000 and kw["system"] == INV.INSTRUCTIONS
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "medium"
    assert "inference_geo" not in kw and "tools" not in kw
    brief = json.loads(kw["messages"][0]["content"][len(LA.USER_PREAMBLE):])
    assert brief == INV.build_brief(pack, external=True)
    sent = json.dumps(kw)
    assert not any(t in sent for t in INV.EXTERNAL_FORBIDDEN_TERMS) and pack.header["source_id"] not in sent
    assert inv.cost_usd == pytest.approx((5000 * 4 + 3000 * 20) / 1e6) and len(inv.items) == 20


def test_live_requires_the_switch_and_a_key_and_never_retries(pack, monkeypatch):
    request = PV.ProviderRequest(INV.TASK, INV.PROMPT_VERSION, INV.INSTRUCTIONS,
                                 INV.build_brief(pack, external=True), 8000)
    with pytest.raises(PV.LiveCallsDisabled, match="FBX_AI_LIVE"):
        LA.AnthropicProvider().complete(request)
    monkeypatch.setenv(PV.LIVE_SWITCH_ENV, "enabled")
    with pytest.raises(PV.LiveCallsDisabled, match="ANTHROPIC_API_KEY"):
        LA.AnthropicProvider().complete(request)
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    made = {}

    class FakeSDK:
        def __init__(self, **kw):
            made.update(kw)
            self.messages = FakeClient()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeSDK))
    LA.AnthropicProvider().complete(request)
    assert made == {"max_retries": 0, "timeout": LA.TIMEOUT_SECONDS}             # the key is never passed by us
    with pytest.raises(PV.ProviderError, match="exceeds 8000"):
        LA.AnthropicProvider(client=FakeClient()).complete(PV.ProviderRequest("t", "v", "i", {"items": []}, 8001))


def test_budget_one_call_and_overrun_kill(pack, monkeypatch):
    monkeypatch.setenv(PV.LIVE_SWITCH_ENV, "enabled")
    ledger = PV.BudgetLedger(2.0, max_calls=1)
    provider = LA.AnthropicProvider(client=FakeClient())
    INV.investigate(pack, provider, ledger, authorization=auth(pack), max_output_tokens=8000)
    with pytest.raises(PV.BudgetExceeded, match="call limit"):
        INV.investigate(pack, provider, ledger, authorization=auth(pack), max_output_tokens=8000)
    ledger = PV.BudgetLedger(2.0)
    big = LA.AnthropicProvider(client=FakeClient(usage=(5000, 200_000)))  # far beyond the authorised worst case
    with pytest.raises(PV.BudgetExceeded):
        INV.investigate(pack, big, ledger, authorization=auth(pack), max_output_tokens=8000)
    assert ledger.killed and ledger.spent_usd > 2.0 and ledger.entries      # recorded, then everything stops
    with pytest.raises(PV.BudgetExceeded, match="no finite"):
        PV.call(LA.AnthropicProvider(client=FakeClient()), PV.ProviderRequest("t", "v", "i", {"items": []}, 9000),
                PV.BudgetLedger(2.0))


@pytest.mark.parametrize("stop,text,error", [("refusal", "", "refusal"),
                                             ("max_tokens", '{"items": [', "truncated"),
                                             ("end_turn", "not json", "not valid JSON")])
def test_refusal_truncation_and_bad_output_are_charged_and_recorded(pack, monkeypatch, stop, text, error):
    monkeypatch.setenv(PV.LIVE_SWITCH_ENV, "enabled")
    ledger = PV.BudgetLedger(2.0)
    inv = INV.investigate(pack, LA.AnthropicProvider(client=FakeClient(stop_reason=stop, text=text)), ledger,
                          authorization=auth(pack), max_output_tokens=8000)
    assert inv.items == [] and error in inv.response["content"]["error"] and ledger.spent_usd > 0
    assert inv.response["content"]["raw_text"] == text


def test_preflight_makes_no_call_and_writes_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", DUMMY_KEY)
    s = LX.preflight(str(tmp_path))
    text = (tmp_path / "preflight_request.json").read_text()
    assert DUMMY_KEY not in text and s["environment"]["ANTHROPIC_API_KEY"] == "present"
    assert s["model"] == "claude-opus-5-5" and s["max_tokens"] == 8000 and s["call_limit"] == 1
    assert 0 < s["worst_case_cost_usd"] < 0.25 and s["budget_usd"] == 2.0
    assert s["brief_fields"] == ["caveats", "dimension", "key", "status", "unit", "value"]
    assert not any(t in text for t in INV.EXTERNAL_FORBIDDEN_TERMS)


def test_execute_once_with_confirmation_audit_and_replay(tmp_path, monkeypatch):
    monkeypatch.setenv(PV.LIVE_SWITCH_ENV, "enabled")
    real_cls = LA.AnthropicProvider
    monkeypatch.setattr(LA, "AnthropicProvider", lambda: real_cls(client=FakeClient()))
    fp = LX.preflight("")["request_fingerprint"]
    with pytest.raises(SystemExit, match="confirm-request"):
        LX.execute(str(tmp_path), "approver", "2026-11-06", "0" * 16)
    inv, ledger, diffs = LX.execute(str(tmp_path), "approver", "2026-11-06", fp[:16])
    assert diffs == [] and ledger.spent_usd == pytest.approx(0.08) and len(ledger.entries) == 1
    assert all(it.decision is None for it in inv.items)                      # every item awaits the analyst
    assert {it.state for it in inv.items} == {"PENDING_REVIEW", "BLOCKED"}
    assert (tmp_path / "ledger.json").exists() and (tmp_path / LX.RESULT).exists()
    with pytest.raises(SystemExit, match="already been attempted"):
        LX.execute(str(tmp_path), "approver", "2026-11-06", fp[:16])


def test_a_failed_attempt_also_blocks_a_second_call(tmp_path, monkeypatch):
    monkeypatch.setenv(PV.LIVE_SWITCH_ENV, "enabled")

    class Failing(FakeClient):
        def create(self, **kw):
            self.calls.append(kw)
            raise RuntimeError("simulated API error")
    fake = Failing()
    real_cls = LA.AnthropicProvider
    monkeypatch.setattr(LA, "AnthropicProvider", lambda: real_cls(client=fake))
    fp = LX.preflight("")["request_fingerprint"]
    with pytest.raises(RuntimeError):
        LX.execute(str(tmp_path), "approver", "2026-11-06", fp[:16])
    assert len(fake.calls) == 1 and (tmp_path / "ledger.json").exists()
    with pytest.raises(SystemExit, match="already been attempted"):
        LX.execute(str(tmp_path), "approver", "2026-11-06", fp[:16])
    assert len(fake.calls) == 1
