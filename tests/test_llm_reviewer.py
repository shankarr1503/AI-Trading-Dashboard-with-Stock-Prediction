from types import SimpleNamespace

import pytest

from backend.trading.llm_reviewer import LLMReviewer, normalize_verdict


def block_tool(name, inp, id_="t1"):
    return SimpleNamespace(type="tool_use", name=name, input=inp, id=id_)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def fake_client(responses):
    msgs = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


async def handler(name, args):
    return {"symbol": args.get("symbol"), "headlines": ["Company beats estimates"]}


def test_normalize_can_only_shrink():
    assert normalize_verdict({"decision": "approve", "size_multiplier": 5}, "m").size_multiplier == 1.0
    assert normalize_verdict({"decision": "reduce", "size_multiplier": 3}, "m").size_multiplier == 0.75
    assert normalize_verdict({"decision": "reduce", "size_multiplier": 0}, "m").size_multiplier == 0.25
    assert normalize_verdict({"decision": "veto", "size_multiplier": 1}, "m").size_multiplier == 0.0
    with pytest.raises(ValueError):
        normalize_verdict({"decision": "buy_more"}, "m")


async def test_tool_loop_then_verdict():
    responses = [
        SimpleNamespace(stop_reason="tool_use", content=[block_tool("get_news_headlines", {"symbol": "AAA"})], model="claude-opus-5-5"),
        SimpleNamespace(stop_reason="tool_use", content=[block_tool("submit_verdict", {
            "decision": "reduce", "size_multiplier": 0.5, "rationale": "Earnings tomorrow", "key_risks": ["earnings gap"]}, "t2")],
            model="claude-opus-5-5"),
    ]
    client, msgs = fake_client(responses)
    v = await LLMReviewer(client=client).review({"symbol": "AAA"}, handler)
    assert v.decision == "reduce" and v.size_multiplier == 0.5 and v.source == "llm"
    first = msgs.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default"
    assert first["output_config"] == {"effort": "medium"}
    # second call carries the tool result back
    tool_result = msgs.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "t1"


async def test_refusal_and_errors_fail_safe():
    client, _ = fake_client([SimpleNamespace(stop_reason="refusal", content=[], model="m")])
    v = await LLMReviewer(client=client, fail_mode="veto").review({}, handler)
    assert v.decision == "veto" and v.source == "fail_safe"

    import anthropic
    import httpx2

    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    client, _ = fake_client([err])
    v = await LLMReviewer(client=client, fail_mode="approve").review({}, handler)
    assert v.decision == "approve" and v.source == "fail_safe"
