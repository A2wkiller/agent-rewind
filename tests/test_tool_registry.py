"""Tests for tracegate_bridge.AgentToolRegistry: registration, provider schema
compilation, and safe async tool execution (offline; stub token engine)."""
from __future__ import annotations

import asyncio
import json

import pytest

import tracegate_mcp as tg
from tracegate_bridge import AgentToolRegistry


@pytest.fixture
def registry():
    return AgentToolRegistry()


# ---------------------------------------------------------------------------
# Registration and default tools
# ---------------------------------------------------------------------------

def test_register_stores_tool_definition(registry):
    registry.register("t1", "desc", {"type": "object"}, lambda: "ok")
    stored = registry._tools["t1"]
    assert stored["name"] == "t1"
    assert stored["description"] == "desc"
    assert stored["parameters"] == {"type": "object"}
    assert callable(stored["handler"])


def test_register_overwrites_same_name(registry):
    registry.register("t1", "first", {}, lambda: 1)
    registry.register("t1", "second", {}, lambda: 2)
    assert registry._tools["t1"]["description"] == "second"


def test_default_workspace_tools_registered(registry):
    assert "read_workspace_file" in registry._tools
    assert "search_github_skills" in registry._tools


def test_default_tool_schemas_require_path_and_query(registry):
    rwf = registry._tools["read_workspace_file"]
    assert rwf["parameters"]["required"] == ["path"]
    sgs = registry._tools["search_github_skills"]
    assert sgs["parameters"]["required"] == ["query"]


# ---------------------------------------------------------------------------
# Provider schema compilers
# ---------------------------------------------------------------------------

def test_anthropic_schema_shape(registry):
    tools = registry.to_anthropic_tools()
    by_name = {t["name"]: t for t in tools}
    rwf = by_name["read_workspace_file"]
    assert rwf["description"]
    assert rwf["input_schema"]["type"] == "object"
    assert "path" in rwf["input_schema"]["properties"]


def test_openai_schema_shape(registry):
    tools = registry.to_openai_tools()
    for t in tools:
        assert t["type"] == "function"
        assert set(t["function"]) == {"name", "description", "parameters"}


def test_gemini_schema_single_tool_with_all_declarations(registry):
    tools = registry.to_gemini_tools()
    assert len(tools) == 1
    decls = tools[0].function_declarations
    names = {d.name for d in decls}
    assert {"read_workspace_file", "search_github_skills"} <= names


def test_gemini_schema_empty_registry_returns_empty_list():
    r = AgentToolRegistry()
    r._tools.clear()
    assert r.to_gemini_tools() == []


def test_schemas_include_custom_registered_tool(registry):
    registry.register("custom", "c", {"type": "object", "properties": {}}, lambda: "x")
    assert any(t["name"] == "custom" for t in registry.to_anthropic_tools())
    assert any(t["function"]["name"] == "custom" for t in registry.to_openai_tools())
    decls = registry.to_gemini_tools()[0].function_declarations
    assert any(d.name == "custom" for d in decls)


# ---------------------------------------------------------------------------
# execute(): error paths
# ---------------------------------------------------------------------------

async def test_execute_unknown_tool(registry):
    res = await registry.execute("nope", "{}")
    assert res["ok"] is False
    assert "Unknown tool 'nope'" in res["raw_output"]
    assert res["tool_name"] == "nope"
    assert res["token_weight"] == tg.TOKEN_ENGINE.count_tokens(res["raw_output"])


async def test_execute_invalid_json_arguments(registry):
    res = await registry.execute("read_workspace_file", "{invalid_json")
    assert res["ok"] is False
    assert "Invalid JSON arguments" in res["raw_output"]
    assert "{invalid_json" in res["raw_output"]


async def test_execute_handler_exception_reported(registry):
    def boom(**kwargs):
        raise ValueError("kaboom")

    registry.register("boom", "b", {}, boom)
    res = await registry.execute("boom", "{}")
    assert res["ok"] is False
    out = json.loads(res["raw_output"])
    assert "kaboom" in out["error"]


async def test_execute_unexpected_handler_kwarg_reported(registry):
    registry.register("t", "d", {}, lambda: "ok")
    res = await registry.execute("t", json.dumps({"surprise": 1}))
    assert res["ok"] is False
    assert "surprise" in res["raw_output"]


# ---------------------------------------------------------------------------
# execute(): success paths
# ---------------------------------------------------------------------------

async def test_execute_async_handler_awaited(registry):
    async def slow_double(n: int) -> int:
        await asyncio.sleep(0.001)
        return n * 2

    registry.register("dbl", "d", {}, slow_double)
    res = await registry.execute("dbl", json.dumps({"n": 21}))
    assert res["ok"] is True
    assert json.loads(res["raw_output"]) == 42


async def test_execute_sync_handler_runs_in_thread(registry):
    registry.register("add", "a", {}, lambda a, b: a + b)
    res = await registry.execute("add", json.dumps({"a": 2, "b": 3}))
    assert res["ok"] is True
    assert json.loads(res["raw_output"]) == 5


async def test_execute_empty_arguments_string_means_no_args(registry):
    registry.register("ping", "p", {}, lambda: "pong")
    res = await registry.execute("ping", "   ")
    assert res["ok"] is True
    assert res["raw_output"] == "pong"


async def test_execute_string_result_returned_verbatim(registry):
    registry.register("txt", "t", {}, lambda: "plain text")
    res = await registry.execute("txt", "{}")
    assert res["raw_output"] == "plain text"
    assert res["token_weight"] > 0


async def test_execute_dict_result_json_dumped(registry):
    registry.register("obj", "o", {}, lambda: {"k": 1})
    res = await registry.execute("obj", "{}")
    assert json.loads(res["raw_output"]) == {"k": 1}


# ---------------------------------------------------------------------------
# Built-in read_workspace_file handler
# ---------------------------------------------------------------------------

async def test_read_workspace_file_line_numbers_and_slice(registry, tmp_path):
    f = tmp_path / "demo.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 21)), encoding="utf-8")
    res = await registry.execute(
        "read_workspace_file",
        json.dumps({"path": str(f), "start_line": 5, "end_line": 8}),
    )
    assert res["ok"] is True
    lines = res["raw_output"].splitlines()
    assert lines[0].startswith("5: line5")
    assert lines[-1].startswith("8: line8")
    assert len(lines) == 4


async def test_read_workspace_file_missing_file(registry, tmp_path):
    res = await registry.execute(
        "read_workspace_file", json.dumps({"path": str(tmp_path / "ghost.py")})
    )
    assert res["ok"] is True  # handler returns an error payload, not an exception
    assert "not found" in res["raw_output"]


async def test_read_workspace_file_defaults_first_120_lines(registry, tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("\n".join(f"row{i}" for i in range(1, 201)), encoding="utf-8")
    res = await registry.execute("read_workspace_file", json.dumps({"path": str(f)}))
    lines = res["raw_output"].splitlines()
    assert len(lines) == 120
    assert lines[0].startswith("1: row1")
    assert lines[-1].startswith("120: row120")


async def test_read_workspace_file_end_line_clamped_to_file_length(registry, tmp_path):
    f = tmp_path / "short.txt"
    f.write_text("only\nthree\nlines\n", encoding="utf-8")
    res = await registry.execute(
        "read_workspace_file", json.dumps({"path": str(f), "start_line": 1, "end_line": 999})
    )
    assert len(res["raw_output"].splitlines()) == 3


async def test_read_workspace_file_directory_is_not_a_file(registry, tmp_path):
    res = await registry.execute(
        "read_workspace_file", json.dumps({"path": str(tmp_path)})
    )
    assert "not found" in res["raw_output"]


# ---------------------------------------------------------------------------
# Built-in search_github_skills handler (stub store returns no skills)
# ---------------------------------------------------------------------------

async def test_search_github_skills_uses_store_match(registry):
    res = await registry.execute("search_github_skills", json.dumps({"query": "pdf parsing"}))
    assert res["ok"] is True
    assert json.loads(res["raw_output"]) == []
