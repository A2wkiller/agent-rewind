"""Tests for tracegate_bridge message formatters (DAG lineage replay with
native tool_call / tool_result entries) and the extended bridge router."""
from __future__ import annotations

import pytest

from tracegate_bridge import AgentToolRegistry, MultiProviderStreamRouter


@pytest.fixture
def router():
    return MultiProviderStreamRouter(tool_registry=AgentToolRegistry())


# ---------------------------------------------------------------------------
# Anthropic replay
# ---------------------------------------------------------------------------

def test_bridge_fmt_anthropic_system_becomes_user(router):
    out = router._format_messages_for_anthropic([{"role": "system", "content": "skill"}])
    assert out == [{"role": "user", "content": "skill"}]


def test_bridge_fmt_anthropic_tool_call_replayed_as_tool_use_block(router):
    out = router._format_messages_for_anthropic([{
        "role": "assistant",
        "channel": "tool_call",
        "content": '{"path": "a.py"}',
        "tool_name": "read_workspace_file",
        "tool_call_id": "toolu_abc",
    }])
    assert out[0]["role"] == "assistant"
    block = out[0]["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "toolu_abc"
    assert block["name"] == "read_workspace_file"
    assert block["input"] == {"path": "a.py"}


def test_bridge_fmt_anthropic_tool_call_invalid_json_wrapped_raw(router):
    out = router._format_messages_for_anthropic([{
        "role": "assistant",
        "channel": "tool_call",
        "content": "{not json",
        "tool_name": "t",
        "tool_call_id": "toolu_x",
    }])
    assert out[0]["content"][0]["input"] == {"raw": "{not json"}


def test_bridge_fmt_anthropic_tool_call_empty_content_empty_input(router):
    out = router._format_messages_for_anthropic([{
        "role": "assistant",
        "channel": "tool_call",
        "content": "   ",
        "tool_name": "t",
        "tool_call_id": "toolu_x",
    }])
    assert out[0]["content"][0]["input"] == {}


def test_bridge_fmt_anthropic_tool_result_replayed_as_tool_result_block(router):
    out = router._format_messages_for_anthropic([{
        "role": "tool",
        "content": "file body",
        "tool_name": "read_workspace_file",
        "tool_call_id": "toolu_abc",
    }])
    assert out[0]["role"] == "user"
    block = out[0]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "toolu_abc"
    assert block["content"] == "file body"


def test_bridge_fmt_anthropic_default_tool_ids_are_deterministic(router):
    out = router._format_messages_for_anthropic([{
        "role": "assistant", "channel": "tool_call", "content": "{}", "tool_name": "search",
    }])
    assert out[0]["content"][0]["id"] == "toolu_search"


def test_bridge_fmt_anthropic_edited_thinking_becomes_user(router):
    out = router._format_messages_for_anthropic([{
        "role": "assistant", "channel": "thinking", "content": "edited", "is_user_modified": True,
    }])
    assert out == [{"role": "user", "content": "edited"}]


def test_bridge_fmt_anthropic_signed_thinking_becomes_assistant(router):
    out = router._format_messages_for_anthropic([{
        "role": "assistant", "channel": "thinking", "content": "orig", "is_user_modified": False,
    }])
    assert out == [{"role": "assistant", "content": "orig"}]


# ---------------------------------------------------------------------------
# Gemini replay
# ---------------------------------------------------------------------------

def test_bridge_fmt_gemini_tool_call_becomes_function_call_part(router):
    out = router._format_messages_for_gemini([{
        "role": "assistant",
        "channel": "tool_call",
        "content": '{"path": "a.py"}',
        "tool_name": "read_workspace_file",
    }])
    assert out[0].role == "model"
    fc = out[0].parts[0].function_call
    assert fc is not None
    assert fc.name == "read_workspace_file"
    assert fc.args == {"path": "a.py"}


def test_bridge_fmt_gemini_tool_result_becomes_function_response(router):
    out = router._format_messages_for_gemini([{
        "role": "tool", "content": "body", "tool_name": "read_workspace_file",
    }])
    assert out[0].role == "user"
    fr = out[0].parts[0].function_response
    assert fr is not None
    assert fr.name == "read_workspace_file"
    assert fr.response == {"result": "body"}


def test_bridge_fmt_gemini_tool_call_invalid_json_raw_args(router):
    out = router._format_messages_for_gemini([{
        "role": "assistant", "channel": "tool_call", "content": "{bad", "tool_name": "t",
    }])
    assert out[0].parts[0].function_call.args == {"raw": "{bad"}


def test_bridge_fmt_gemini_plain_roles(router):
    out = router._format_messages_for_gemini([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert [c.role for c in out] == ["user", "model"]


# ---------------------------------------------------------------------------
# OpenAI replay
# ---------------------------------------------------------------------------

def test_bridge_fmt_openai_tool_call_replayed_with_tool_calls_array(router):
    out = router._format_messages_for_openai([{
        "role": "assistant",
        "channel": "tool_call",
        "content": '{"q": "x"}',
        "tool_name": "search",
        "tool_call_id": "call_1",
    }])
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] is None
    tc = msg["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["type"] == "function"
    assert tc["function"] == {"name": "search", "arguments": '{"q": "x"}'}


def test_bridge_fmt_openai_tool_call_empty_args_default_object(router):
    out = router._format_messages_for_openai([{
        "role": "assistant", "channel": "tool_call", "content": " ", "tool_name": "t",
    }])
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_bridge_fmt_openai_tool_result_uses_tool_role_and_id(router):
    out = router._format_messages_for_openai([{
        "role": "tool", "content": "body", "tool_name": "search", "tool_call_id": "call_1",
    }])
    assert out[0] == {"role": "tool", "tool_call_id": "call_1", "content": "body"}


def test_bridge_fmt_openai_default_call_id(router):
    out = router._format_messages_for_openai([{
        "role": "assistant", "channel": "tool_call", "content": "{}", "tool_name": "search",
    }])
    assert out[0]["tool_calls"][0]["id"] == "call_search"


def test_bridge_fmt_openai_passthrough_roles(router):
    out = router._format_messages_for_openai([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
        {"role": "unknown", "content": "dropped"},
    ])
    assert [m["role"] for m in out] == ["system", "user", "assistant"]


# ---------------------------------------------------------------------------
# Extended router: registry wiring + enable_tools flag
# ---------------------------------------------------------------------------

def test_bridge_router_holds_registry(router):
    assert isinstance(router.registry, AgentToolRegistry)


def test_bridge_detect_provider_same_matrix(router):
    assert router.detect_provider("claude-opus-4-1") == "anthropic"
    assert router.detect_provider("gemini-2.5-flash") == "gemini"
    assert router.detect_provider("gpt-4o") == "openai"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Missing piece: tracegate_bridge.MultiProviderStreamRouter.stream_turn "
        "dispatches to self._stream_anthropic/_stream_gemini/_stream_openai, but "
        "those methods only exist in llm_stream_router.py; the bridge router "
        "cannot stream until they are added (same gap as the missing FastAPI app)."
    ),
)
async def test_bridge_stream_turn_dispatches_to_provider_methods(router, monkeypatch):
    captured = {}

    async def fake(model, system_prompt, lineage, max_output_tokens, prefill, tools):
        captured["tools"] = tools
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr(router, "_stream_anthropic", fake)
    async for _ in router.stream_turn("claude-sonnet-5-5", "s", [], 100, enable_tools=True):
        pass
    assert captured["tools"] is not None
    assert any(t["name"] == "read_workspace_file" for t in captured["tools"])


async def test_bridge_router_lacks_provider_stream_methods(router):
    # Documents the current gap precisely: remove this test once the methods exist.
    assert not hasattr(router, "_stream_anthropic")
    assert not hasattr(router, "_stream_gemini")
    assert not hasattr(router, "_stream_openai")


def test_bridge_module_constants():
    import tracegate_bridge as bridge

    assert bridge.MAX_AGENT_TOOL_LOOPS == 8
    assert isinstance(bridge.TOOL_REGISTRY, AgentToolRegistry)
    assert isinstance(bridge.STREAM_ROUTER, MultiProviderStreamRouter)
