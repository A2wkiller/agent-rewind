"""Tests for llm_stream_router: UnifiedStreamChunk, provider detection,
message formatters, and provider stream normalization (all with fake
provider clients; no network, no API keys)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import llm_stream_router as lsr
from llm_stream_router import MultiProviderStreamRouter, UnifiedStreamChunk


def make_router() -> MultiProviderStreamRouter:
    return MultiProviderStreamRouter()


# ---------------------------------------------------------------------------
# UnifiedStreamChunk dataclass
# ---------------------------------------------------------------------------

def test_chunk_requires_channel():
    with pytest.raises(TypeError):
        UnifiedStreamChunk()  # type: ignore[call-arg]


def test_chunk_defaults():
    c = UnifiedStreamChunk(channel="ASSISTANT_OUTPUT")
    assert c.delta_text == ""
    assert c.tool_name is None
    assert c.tool_call_id is None
    assert c.provider_signature is None


def test_chunk_full_fields():
    c = UnifiedStreamChunk(
        channel="TOOL_CALL",
        delta_text='{"a": 1}',
        tool_name="read_workspace_file",
        tool_call_id="call_1",
        provider_signature="sig",
    )
    assert c.channel == "TOOL_CALL"
    assert c.tool_name == "read_workspace_file"
    assert c.tool_call_id == "call_1"
    assert c.provider_signature == "sig"


def test_chunk_thought_channel():
    c = UnifiedStreamChunk(channel="THOUGHT_BLOCK", delta_text="thinking...")
    assert c.channel == "THOUGHT_BLOCK"
    assert c.delta_text == "thinking..."


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-sonnet-5-5", "anthropic"),
        ("claude-opus-4-1", "anthropic"),
        ("CLAUDE-3-HAIKU", "anthropic"),
        ("anthropic-custom", "anthropic"),
        ("gemini-2.5-pro", "gemini"),
        ("GEMINI-flash", "gemini"),
        ("gpt-4o", "openai"),
        ("gpt-4.1-mini", "openai"),
        ("o3-mini", "openai"),
        ("text-embedding-3-large", "openai"),
        ("", "openai"),
    ],
)
def test_detect_provider(model, expected):
    assert make_router().detect_provider(model) == expected


# ---------------------------------------------------------------------------
# Client construction from environment
# ---------------------------------------------------------------------------

def test_clients_none_without_env_keys(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    r = MultiProviderStreamRouter()
    assert r.anthropic_client is None
    assert r.gemini_client is None
    assert r.openai_client is None


def test_anthropic_client_constructed_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    r = MultiProviderStreamRouter()
    assert r.anthropic_client is not None
    assert r.gemini_client is None
    assert r.openai_client is None


# ---------------------------------------------------------------------------
# Message formatters (static methods)
# ---------------------------------------------------------------------------

def test_fmt_anthropic_system_becomes_user():
    out = MultiProviderStreamRouter._format_messages_for_anthropic(
        [{"role": "system", "content": "policy"}]
    )
    assert out == [{"role": "user", "content": "policy"}]


def test_fmt_anthropic_thinking_unmodified_becomes_assistant():
    out = MultiProviderStreamRouter._format_messages_for_anthropic(
        [{"role": "assistant", "channel": "thinking", "content": "reasoning"}]
    )
    assert out == [{"role": "assistant", "content": "reasoning"}]


def test_fmt_anthropic_thinking_user_modified_becomes_user():
    out = MultiProviderStreamRouter._format_messages_for_anthropic(
        [{
            "role": "assistant",
            "channel": "thinking",
            "content": "edited",
            "is_user_modified": True,
        }]
    )
    assert out == [{"role": "user", "content": "edited"}]


def test_fmt_anthropic_tool_result_wrapped():
    out = MultiProviderStreamRouter._format_messages_for_anthropic(
        [{"role": "tool", "tool_name": "read_workspace_file", "content": "file body"}]
    )
    assert out[0]["role"] == "user"
    assert '<tool_result tool="read_workspace_file">' in out[0]["content"]
    assert "file body" in out[0]["content"]


def test_fmt_anthropic_tool_result_default_tool_name():
    out = MultiProviderStreamRouter._format_messages_for_anthropic(
        [{"role": "tool", "content": "x"}]
    )
    assert '<tool_result tool="tool">' in out[0]["content"]


def test_fmt_anthropic_plain_roles():
    out = MultiProviderStreamRouter._format_messages_for_anthropic([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "mystery", "content": "fallback"},
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


def test_fmt_gemini_roles():
    out = MultiProviderStreamRouter._format_messages_for_gemini([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert [c.role for c in out] == ["user", "model"]
    assert out[0].parts[0].text == "hi"
    assert out[1].parts[0].text == "hello"


def test_fmt_gemini_non_string_content_coerced():
    out = MultiProviderStreamRouter._format_messages_for_gemini(
        [{"role": "user", "content": 42}]
    )
    assert out[0].parts[0].text == "42"


def test_fmt_openai_passthrough_roles():
    out = MultiProviderStreamRouter._format_messages_for_openai([
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ])
    assert [(m["role"], m["content"]) for m in out] == [
        ("system", "s"), ("user", "u"), ("assistant", "a"),
    ]


def test_fmt_openai_tool_result_wrapped():
    out = MultiProviderStreamRouter._format_messages_for_openai(
        [{"role": "tool", "tool_name": "search", "content": "res"}]
    )
    assert out[0]["role"] == "user"
    assert '<tool_result name="search">' in out[0]["content"]
    assert "res" in out[0]["content"]


def test_fmt_openai_unknown_role_dropped():
    out = MultiProviderStreamRouter._format_messages_for_openai(
        [{"role": "mystery", "content": "gone"}, {"role": "user", "content": "kept"}]
    )
    assert len(out) == 1
    assert out[0]["content"] == "kept"


# ---------------------------------------------------------------------------
# Missing-client errors
# ---------------------------------------------------------------------------

async def test_stream_anthropic_requires_client():
    r = make_router()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        async for _ in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 100, "", None):
            pass


async def test_stream_gemini_requires_client():
    r = make_router()
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        async for _ in r._stream_gemini("gemini-2.5-pro", "sys", [], 100, "", None):
            pass


async def test_stream_openai_requires_client():
    r = make_router()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        async for _ in r._stream_openai("gpt-4o", "sys", [], 100, "", None):
            pass


# ---------------------------------------------------------------------------
# stream_turn dispatch
# ---------------------------------------------------------------------------

async def test_stream_turn_dispatches_anthropic(monkeypatch):
    r = make_router()
    called = {}

    async def fake(model, system_prompt, lineage, max_output_tokens, prefill, tools):
        called["model"] = model
        yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="ok")

    monkeypatch.setattr(r, "_stream_anthropic", fake)
    chunks = [c async for c in r.stream_turn("claude-sonnet-5-5", "s", [], 100)]
    assert called["model"] == "claude-sonnet-5-5"
    assert len(chunks) == 1


async def test_stream_turn_dispatches_gemini(monkeypatch):
    r = make_router()
    called = {}

    async def fake(model, system_prompt, lineage, max_output_tokens, prefill, tools):
        called["hit"] = True
        yield UnifiedStreamChunk(channel="THOUGHT_BLOCK", delta_text="t")

    monkeypatch.setattr(r, "_stream_gemini", fake)
    chunks = [c async for c in r.stream_turn("gemini-2.5-pro", "s", [], 100)]
    assert called["hit"] is True
    assert chunks[0].channel == "THOUGHT_BLOCK"


async def test_stream_turn_dispatches_openai(monkeypatch):
    r = make_router()
    called = {}

    async def fake(model, system_prompt, lineage, max_output_tokens, prefill, tools):
        called["hit"] = True
        yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="o")

    monkeypatch.setattr(r, "_stream_openai", fake)
    chunks = [c async for c in r.stream_turn("gpt-4o", "s", [], 100)]
    assert called["hit"] is True
    assert chunks[0].delta_text == "o"


# ---------------------------------------------------------------------------
# Anthropic streaming with a fake client
# ---------------------------------------------------------------------------

class _FakeAnthropicStream:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()


class _FakeAnthropicMessages:
    def __init__(self, events):
        self._events = events
        self.last_kwargs = None

    def stream(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicStream(self._events)


class _FakeAnthropicClient:
    def __init__(self, events):
        self.messages = _FakeAnthropicMessages(events)


def anthropic_router(events):
    r = make_router()
    r.anthropic_client = _FakeAnthropicClient(events)
    return r


async def test_anthropic_text_delta_maps_to_assistant_output():
    events = [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text="Hello")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=" world")),
    ]
    r = anthropic_router(events)
    chunks = [c async for c in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 100, "", None)]
    assert [c.channel for c in chunks] == ["ASSISTANT_OUTPUT", "ASSISTANT_OUTPUT"]
    assert chunks[0].delta_text == "Hello"
    assert chunks[1].delta_text == " world"


async def test_anthropic_thinking_and_signature_deltas():
    events = [
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="thinking_delta", thinking="hmm")),
        SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="signature_delta", signature="sig123")),
    ]
    r = anthropic_router(events)
    chunks = [c async for c in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 100, "", None)]
    assert chunks[0].channel == "THOUGHT_BLOCK"
    assert chunks[0].delta_text == "hmm"
    assert chunks[1].channel == "THOUGHT_BLOCK"
    assert chunks[1].delta_text == ""
    assert chunks[1].provider_signature == "sig123"


async def test_anthropic_tool_use_carries_name_and_id():
    events = [
        SimpleNamespace(
            type="content_block_start",
            content_block=SimpleNamespace(type="tool_use", name="read_workspace_file", id="toolu_1"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"path":'),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="input_json_delta", partial_json='"a.py"}'),
        ),
    ]
    r = anthropic_router(events)
    chunks = [c async for c in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 100, "", None)]
    assert len(chunks) == 2
    assert all(c.channel == "TOOL_CALL" for c in chunks)
    assert all(c.tool_name == "read_workspace_file" for c in chunks)
    assert all(c.tool_call_id == "toolu_1" for c in chunks)
    assert chunks[0].delta_text + chunks[1].delta_text == '{"path":"a.py"}'


async def test_anthropic_max_tokens_floor_is_64():
    r = anthropic_router([])
    async for _ in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 5, "", None):
        pass
    assert r.anthropic_client.messages.last_kwargs["max_tokens"] == 64


async def test_anthropic_system_prompt_cached_block():
    r = anthropic_router([])
    async for _ in r._stream_anthropic("claude-sonnet-5-5", "You are helpful.", [], 100, "", None):
        pass
    system = r.anthropic_client.messages.last_kwargs["system"]
    assert system[0]["text"] == "You are helpful."
    assert system[0]["cache_control"] == {"type": "ephemeral"}


async def test_anthropic_empty_system_prompt_sends_empty_list():
    r = anthropic_router([])
    async for _ in r._stream_anthropic("claude-sonnet-5-5", "", [], 100, "", None):
        pass
    assert r.anthropic_client.messages.last_kwargs["system"] == []


async def test_anthropic_thinking_enabled_above_1200_tokens():
    r = anthropic_router([])
    async for _ in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 8000, "", None):
        pass
    thinking = r.anthropic_client.messages.last_kwargs["thinking"]
    assert thinking == {"type": "enabled", "budget_tokens": 4000}


async def test_anthropic_thinking_budget_minimum_1024():
    r = anthropic_router([])
    async for _ in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 1500, "", None):
        pass
    thinking = r.anthropic_client.messages.last_kwargs["thinking"]
    assert thinking["budget_tokens"] == 1024


async def test_anthropic_no_thinking_below_1200_tokens():
    r = anthropic_router([])
    async for _ in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 1000, "", None):
        pass
    assert "thinking" not in r.anthropic_client.messages.last_kwargs


async def test_anthropic_prefill_disables_thinking_and_appends_assistant_message():
    r = anthropic_router([])
    async for _ in r._stream_anthropic(
        "claude-sonnet-5-5", "sys", [{"role": "user", "content": "go"}], 8000, "partial code  ", None
    ):
        pass
    kwargs = r.anthropic_client.messages.last_kwargs
    assert "thinking" not in kwargs
    last = kwargs["messages"][-1]
    assert last["role"] == "assistant"
    assert last["content"][0]["text"] == "partial code"


async def test_anthropic_tools_kwarg_forwarded():
    r = anthropic_router([])
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]
    async for _ in r._stream_anthropic("claude-sonnet-5-5", "sys", [], 100, "", tools):
        pass
    assert r.anthropic_client.messages.last_kwargs["tools"] == tools


# ---------------------------------------------------------------------------
# OpenAI streaming with a fake client
# ---------------------------------------------------------------------------

class _FakeOpenAIStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class _FakeOpenAICompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeOpenAIStream(self._chunks)


class _FakeOpenAIClient:
    def __init__(self, chunks):
        self.chat = SimpleNamespace(completions=_FakeOpenAICompletions(chunks))


def openai_chunk(delta):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


async def test_openai_content_and_reasoning_channels():
    chunks = [
        openai_chunk(SimpleNamespace(reasoning_content="thinking", tool_calls=None, content=None)),
        openai_chunk(SimpleNamespace(reasoning_content=None, tool_calls=None, content="answer")),
    ]
    r = make_router()
    r.openai_client = _FakeOpenAIClient(chunks)
    out = [c async for c in r._stream_openai("gpt-4o", "sys", [], 100, "", None)]
    assert out[0].channel == "THOUGHT_BLOCK"
    assert out[0].delta_text == "thinking"
    assert out[1].channel == "ASSISTANT_OUTPUT"
    assert out[1].delta_text == "answer"


async def test_openai_tool_call_arguments_streamed_with_name():
    tc1 = SimpleNamespace(id="call_9", function=SimpleNamespace(name="search", arguments='{"q":'))
    tc2 = SimpleNamespace(id=None, function=SimpleNamespace(name=None, arguments='"x"}'))
    chunks = [
        openai_chunk(SimpleNamespace(reasoning_content=None, tool_calls=[tc1], content=None)),
        openai_chunk(SimpleNamespace(reasoning_content=None, tool_calls=[tc2], content=None)),
    ]
    r = make_router()
    r.openai_client = _FakeOpenAIClient(chunks)
    out = [c async for c in r._stream_openai("gpt-4o", "sys", [], 100, "", None)]
    assert all(c.channel == "TOOL_CALL" for c in out)
    assert out[0].tool_name == "search"
    assert out[0].tool_call_id == "call_9"
    assert out[1].tool_name == "search"  # name carries over across chunks
    assert out[0].delta_text + out[1].delta_text == '{"q":"x"}'


async def test_openai_empty_choices_skipped():
    chunks = [SimpleNamespace(choices=[]), openai_chunk(SimpleNamespace(reasoning_content=None, tool_calls=None, content="hi"))]
    r = make_router()
    r.openai_client = _FakeOpenAIClient(chunks)
    out = [c async for c in r._stream_openai("gpt-4o", "sys", [], 100, "", None)]
    assert len(out) == 1
    assert out[0].delta_text == "hi"


async def test_openai_prefill_messages_built():
    r = make_router()
    r.openai_client = _FakeOpenAIClient([])
    async for _ in r._stream_openai(
        "gpt-4o", "sys", [{"role": "user", "content": "go"}], 100, "partial answer", None
    ):
        pass
    msgs = r.openai_client.chat.completions.last_kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[-2] == {"role": "assistant", "content": "partial answer"}
    assert msgs[-1]["role"] == "user"
    assert "partial answer" in msgs[-1]["content"]


async def test_openai_max_completion_tokens_floor_and_stream_flag():
    r = make_router()
    r.openai_client = _FakeOpenAIClient([])
    async for _ in r._stream_openai("gpt-4o", "", [], 10, "", None):
        pass
    kwargs = r.openai_client.chat.completions.last_kwargs
    assert kwargs["max_completion_tokens"] == 64
    assert kwargs["stream"] is True
    assert kwargs["messages"] == []


# ---------------------------------------------------------------------------
# Gemini streaming with a fake client
# ---------------------------------------------------------------------------

class _FakeGeminiStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
        return gen()


class _FakeGeminiModels:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_call = None

    async def generate_content_stream(self, model, contents, config):
        self.last_call = {"model": model, "contents": contents, "config": config}
        return _FakeGeminiStream(self._chunks)


class _FakeGeminiClient:
    def __init__(self, chunks):
        self.aio = SimpleNamespace(models=_FakeGeminiModels(chunks))


def gemini_chunk(parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))])


async def test_gemini_thought_and_text_parts():
    parts = [
        SimpleNamespace(thought=True, text="reasoning", thought_signature="gsig", function_call=None),
        SimpleNamespace(thought=False, text="answer", thought_signature=None, function_call=None),
    ]
    r = make_router()
    r.gemini_client = _FakeGeminiClient([gemini_chunk(parts)])
    out = [c async for c in r._stream_gemini("gemini-2.5-pro", "sys", [], 100, "", None)]
    assert out[0].channel == "THOUGHT_BLOCK"
    assert out[0].provider_signature == "gsig"
    assert out[1].channel == "ASSISTANT_OUTPUT"
    assert out[1].delta_text == "answer"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Bug in llm_stream_router._stream_gemini: getattr(fn, 'id', fallback) "
        "returns None when the SDK object has id=None, so the call_<name> "
        "fallback never fires and tool_call_id is lost."
    ),
)
async def test_gemini_function_call_part():
    fn = SimpleNamespace(args={"path": "a.py"}, name="read_workspace_file", id=None)
    parts = [SimpleNamespace(thought=False, text=None, thought_signature=None, function_call=fn)]
    r = make_router()
    r.gemini_client = _FakeGeminiClient([gemini_chunk(parts)])
    out = [c async for c in r._stream_gemini("gemini-2.5-pro", "sys", [], 100, "", None)]
    assert len(out) == 1
    assert out[0].channel == "TOOL_CALL"
    assert out[0].tool_name == "read_workspace_file"
    assert out[0].tool_call_id == "call_read_workspace_file"
    assert json.loads(out[0].delta_text) == {"path": "a.py"}


async def test_gemini_empty_candidates_skipped():
    chunks = [SimpleNamespace(candidates=[]), gemini_chunk([SimpleNamespace(thought=False, text="hi", thought_signature=None, function_call=None)])]
    r = make_router()
    r.gemini_client = _FakeGeminiClient(chunks)
    out = [c async for c in r._stream_gemini("gemini-2.5-pro", "sys", [], 100, "", None)]
    assert len(out) == 1
    assert out[0].delta_text == "hi"


async def test_gemini_prefill_adds_model_turn_and_continuation_anchor():
    r = make_router()
    r.gemini_client = _FakeGeminiClient([])
    prefill = "x" * 200
    async for _ in r._stream_gemini("gemini-2.5-pro", "sys", [{"role": "user", "content": "go"}], 100, prefill, None):
        pass
    contents = r.gemini_client.aio.models.last_call["contents"]
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].text == prefill
    # The continuation anchor uses only the last 120 characters
    assert ("x" * 120) in contents[2].parts[0].text
    assert ("x" * 200) not in contents[2].parts[0].text


async def test_gemini_config_floor_and_thoughts():
    r = make_router()
    r.gemini_client = _FakeGeminiClient([])
    async for _ in r._stream_gemini("gemini-2.5-pro", "sys", [], 10, "", None):
        pass
    config = r.gemini_client.aio.models.last_call["config"]
    assert config.max_output_tokens == 64
    assert config.system_instruction == "sys"
    assert config.thinking_config.include_thoughts is True
