"""server/llm.py 的 Claude API 部分：月上限闸、花费记账、拒答/截断抛错、批量提交与收结果。

全部用假的 anthropic 模块（注入 sys.modules），不导入真 SDK 的网络层，不联网。
"""
from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace as NS

import pytest

import llm


class _Usage(dict):
    def model_dump(self):
        return dict(self)


def _resp(stop="end_turn", text="答案", model="claude-opus-5-5", usage=None):
    return NS(stop_reason=stop, stop_details={"type": "x"} if stop == "refusal" else None, model=model,
              content=[NS(type="text", text=text)],
              usage=_Usage(usage or {"input_tokens": 1_000_000, "output_tokens": 0}))


@pytest.fixture
def fake_sdk(monkeypatch, tmp_path):
    """假 anthropic 包：Anthropic().messages.create 返回预设响应；记录调用参数。"""
    key = tmp_path / "key.txt"
    key.write_text("sk-test\n")
    monkeypatch.setattr(llm, "API_KEY_FILE", key)
    state = NS(resp=_resp(), calls=[], beta_calls=[])

    class _Err(Exception):
        status_code = 500

    class Anthropic:
        def __init__(self, api_key, timeout):
            assert api_key == "sk-test"
            self.messages = NS(create=lambda **kw: state.calls.append(kw) or state.resp)
            self.beta = NS(messages=NS(create=lambda **kw: state.beta_calls.append(kw) or state.resp))

    mod = types.ModuleType("anthropic")
    mod.Anthropic = Anthropic
    mod.RateLimitError = type("RateLimitError", (_Err,), {})
    mod.APIStatusError = type("APIStatusError", (_Err,), {})
    mod.APIConnectionError = type("APIConnectionError", (_Err,), {})
    types_mod = types.ModuleType("anthropic.types")
    mcp = types.ModuleType("anthropic.types.message_create_params")
    mcp.MessageCreateParamsNonStreaming = dict
    msgs = types.ModuleType("anthropic.types.messages")
    bcp = types.ModuleType("anthropic.types.messages.batch_create_params")
    bcp.Request = dict
    for name, m in {"anthropic": mod, "anthropic.types": types_mod,
                    "anthropic.types.message_create_params": mcp, "anthropic.types.messages": msgs,
                    "anthropic.types.messages.batch_create_params": bcp}.items():
        monkeypatch.setitem(sys.modules, name, m)
    return state


def _ledger():
    with llm._spend_db() as c:
        return c.execute("SELECT month, model, usd FROM api_spend ORDER BY id").fetchall()


def _fill_ledger(usd):
    with llm._spend_db() as c:
        c.execute("INSERT INTO api_spend(month, model, usd, usage) VALUES (?,?,?,?)",
                  (time.strftime("%Y-%m"), "x", usd, "{}"))


def test_api_cost_plain_usage():
    """无 iterations 时按顶层 usage 和型号价目表计：输入、缓存写 1.25×、缓存读、输出。"""
    u = {"input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
         "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert llm.api_cost("claude-opus-5-5", u) == pytest.approx(4.0 + 5.0 + 0.20 + 20.0)


def test_api_cost_unknown_model_uses_default_and_tenth_cache_read():
    """价目表里没有的型号按 (5, 25)，缓存读按输入价 0.1×。"""
    u = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert llm.api_cost("claude-unknown", u) == pytest.approx(5.0 + 0.5)


def test_api_cost_iterations_priced_per_model():
    """含 iterations 时逐条按各自型号计价，忽略顶层 token 数。"""
    u = {"input_tokens": 999_999_999, "iterations": [
        {"model": "claude-haiku-5-5", "input_tokens": 1_000_000, "output_tokens": 1_000_000},
        {"input_tokens": 1_000_000}]}  # 没写 model 的按调用型号
    assert llm.api_cost("claude-sonnet-5-5", u) == pytest.approx(0.10 + 0.50 + 2.0)


def test_api_call_refuses_when_ledger_full(fake_sdk, monkeypatch):
    """当月账本已到上限：直接拒绝，不调 SDK。"""
    monkeypatch.setattr(llm, "API_MONTHLY_CAP", 1.0)
    _fill_ledger(1.0)
    with pytest.raises(llm.BackendError, match="上限"):
        llm.api_call("sys", "user")
    assert fake_sdk.calls == []


def test_api_spent_unreadable_ledger_counts_as_over_cap(monkeypatch, tmp_path):
    """账本读不了就当已超限。"""
    monkeypatch.setattr(llm, "AUDIT_DB", tmp_path / "no-such-dir" / "audit.sqlite")
    assert llm.api_spent() == float("inf")


def test_api_call_records_spend_and_returns_text(fake_sdk):
    """正常调用：返回正文、模型、美元，并按 usage 记一行账。"""
    text, model, usd = llm.api_call("sys", "user", effort="low")
    assert (text, model) == ("答案", "claude-opus-5-5")
    assert usd == pytest.approx(4.0)
    rows = _ledger()
    assert [(r[0], r[1]) for r in rows] == [(time.strftime("%Y-%m"), "claude-opus-5-5")]
    assert rows[0][2] == pytest.approx(4.0)
    kw = fake_sdk.calls[0]
    assert kw["output_config"] == {"effort": "low"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_api_call_with_tools_uses_beta(fake_sdk):
    """带 tools/betas 走 beta.messages.create。"""
    llm.api_call("s", "u", tools=[{"type": "t"}], betas=["b1"])
    assert fake_sdk.calls == [] and fake_sdk.beta_calls[0]["betas"] == ["b1"]


def test_api_call_refusal_raises_but_still_billed(fake_sdk):
    """拒答抛错，但花费已记账。"""
    fake_sdk.resp = _resp(stop="refusal")
    with pytest.raises(llm.BackendError, match="refusal"):
        llm.api_call("s", "u")
    assert len(_ledger()) == 1


def test_api_call_max_tokens_raises(fake_sdk):
    fake_sdk.resp = _resp(stop="max_tokens")
    with pytest.raises(llm.BackendError, match="max_tokens"):
        llm.api_call("s", "u")


def test_api_call_empty_answer_raises(fake_sdk):
    fake_sdk.resp = _resp(text="   ")
    with pytest.raises(llm.BackendError, match="empty"):
        llm.api_call("s", "u")


def test_api_call_sdk_error_wrapped(fake_sdk, monkeypatch):
    """SDK 的 429 包成 BackendError。"""
    err = sys.modules["anthropic"].RateLimitError("slow down")

    def boom(**kw):
        raise err
    monkeypatch.setattr(sys.modules["anthropic"], "Anthropic",
                        lambda api_key, timeout: NS(messages=NS(create=boom)))
    with pytest.raises(llm.BackendError, match="429"):
        llm.api_call("s", "u")


def test_batch_submit_refuses_over_remaining_budget(fake_sdk, monkeypatch):
    """已花 + 预估 超上限就拒绝提交。"""
    monkeypatch.setattr(llm, "API_MONTHLY_CAP", 5.0)
    _fill_ledger(4.0)
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("不该建 client"))
    with pytest.raises(llm.BackendError, match="会超上限"):
        llm.batch_submit([("c1", "s", "u")], est_usd=2.0)


def test_batch_submit_builds_requests_with_1h_cache(fake_sdk, monkeypatch):
    """提交：每条带 custom_id，系统提示 1 小时缓存，返回 batch id。"""
    sent = {}
    monkeypatch.setattr(llm, "_client", lambda: NS(messages=NS(batches=NS(
        create=lambda requests: sent.update(reqs=requests) or NS(id="msgbatch_x")))))
    assert llm.batch_submit([("c1", "S", "U"), ("c2", "S", "V")], effort="high") == "msgbatch_x"
    assert [r["custom_id"] for r in sent["reqs"]] == ["c1", "c2"]
    p = sent["reqs"][0]["params"]
    assert p["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert p["output_config"] == {"effort": "high"} and p["messages"][0]["content"] == "U"


def _batch_row(cid, kind="succeeded", stop="end_turn", text="译文"):
    if kind != "succeeded":
        return NS(custom_id=cid, result=NS(type=kind, error=NS(error=NS(type="overloaded_error"))))
    msg = NS(model="claude-opus-5-5", stop_reason=stop, content=[NS(type="text", text=text)],
             usage=_Usage({"input_tokens": 1_000_000}))
    return NS(custom_id=cid, result=NS(type="succeeded", message=msg))


def test_batch_results_yields_and_bills_half_price(monkeypatch):
    """成功的出正文；refusal/max_tokens 出错误；失败的出错误类型；成功条目按五折记账。"""
    rows = [_batch_row("ok"), _batch_row("ref", stop="refusal"), _batch_row("cut", stop="max_tokens"),
            _batch_row("bad", kind="errored")]
    monkeypatch.setattr(llm, "_client", lambda: NS(messages=NS(batches=NS(results=lambda bid: iter(rows)))))
    out = list(llm.batch_results("mb"))
    assert out == [("ok", "译文", None), ("ref", None, "stop_reason=refusal"),
                   ("cut", None, "stop_reason=max_tokens"), ("bad", None, "errored: overloaded_error")]
    led = _ledger()
    assert [r[1] for r in led] == ["claude-opus-5-5:batch"] * 3  # 拒答/截断也花了钱
    assert sum(r[2] for r in led) == pytest.approx(3 * 4.0 * llm.BATCH_DISCOUNT)
