"""Tests for tracegate_cli argument parsing and command handlers, plus the
push_repo helper. All side effects (uvicorn, webbrowser, subprocess, MCP run)
are mocked."""
from __future__ import annotations

import argparse
import subprocess
import sys

import pytest

import tracegate_mcp as tg
import tracegate_cli as cli
import push_repo

from .conftest import make_session


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def test_main_requires_a_subcommand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tracegate"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2  # argparse usage error


@pytest.mark.parametrize(
    "argv,func_name",
    [
        (["tracegate", "up"], "cmd_up"),
        (["tracegate", "mcp"], "cmd_mcp"),
        (["tracegate", "sync", "owner/repo"], "cmd_sync"),
        (["tracegate", "estimate", "hello"], "cmd_estimate"),
        (["tracegate", "tree"], "cmd_tree"),
        (["tracegate", "tree", "sess1"], "cmd_tree"),
        (["tracegate", "test"], "cmd_test"),
    ],
)
def test_main_dispatches_each_subcommand(monkeypatch, argv, func_name):
    called = {}
    monkeypatch.setattr(cli, func_name, lambda args: called.setdefault("args", args))
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    assert "args" in called


def test_up_defaults(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "cmd_up", lambda args: called.setdefault("args", args))
    monkeypatch.setattr(sys, "argv", ["tracegate", "up"])
    cli.main()
    args = called["args"]
    assert args.host == "0.0.0.0"
    assert args.port == 8000
    assert args.inr_rate == 86.50
    assert args.reload is False


def test_global_db_flag_default(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "cmd_mcp", lambda args: called.setdefault("args", args))
    monkeypatch.setattr(sys, "argv", ["tracegate", "--db", "custom.sqlite3", "mcp"])
    cli.main()
    assert called["args"].db == "custom.sqlite3"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def test_cmd_up_sets_env_and_runs_uvicorn(monkeypatch):
    ran = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: ran.setdefault("run", (a, k)))
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: ran.setdefault("opened", url))
    args = argparse.Namespace(db="x.sqlite3", inr_rate=90.0, host="127.0.0.1", port=9000,
                              open_browser=True, reload=True)
    cli.cmd_up(args)
    import os
    assert os.environ["TRACEGATE_DB_PATH"] == "x.sqlite3"
    assert os.environ["TRACEGATE_USD_TO_INR"] == "90.0"
    a, k = ran["run"]
    assert a[0] == "tracegate_bridge:app"
    assert k["host"] == "127.0.0.1"
    assert k["port"] == 9000
    assert k["reload"] is True
    assert ran["opened"] == "http://127.0.0.1:9000"


def test_cmd_mcp_stdio(monkeypatch):
    ran = {}
    monkeypatch.setattr(tg.mcp, "run", lambda **k: ran.update(k), raising=False)
    args = argparse.Namespace(db="d.sqlite3", transport="stdio", host="h", port=1)
    cli.cmd_mcp(args)
    assert ran["transport"] == "stdio"


def test_cmd_mcp_sse_passes_host_port(monkeypatch):
    ran = {}
    monkeypatch.setattr(tg.mcp, "run", lambda **k: ran.update(k), raising=False)
    args = argparse.Namespace(db="d.sqlite3", transport="sse", host="0.0.0.0", port=8001)
    cli.cmd_mcp(args)
    assert ran["transport"] == "sse"
    assert ran["host"] == "0.0.0.0"
    assert ran["port"] == 8001


def test_cmd_sync_calls_library_sync(monkeypatch, capsys):
    seen = {}

    async def fake_sync(repo, branch, github_token):
        seen.update({"repo": repo, "branch": branch, "github_token": github_token})
        return {"skills_indexed_count": 0, "indexed_skills": []}

    monkeypatch.setattr(tg, "sync_github_skill_library", fake_sync)
    args = argparse.Namespace(db="d.sqlite3", repo="owner/repo", branch="dev", token="tok")
    cli.cmd_sync(args)
    assert seen == {"repo": "owner/repo", "branch": "dev", "github_token": "tok"}
    assert "Indexed 0 skills" in capsys.readouterr().out


def test_cmd_estimate_prints_handshake_card(monkeypatch, capsys):
    monkeypatch.setattr(
        tg, "preflight_estimate_and_match_skill",
        lambda user_prompt, model: {"handshake_card_markdown": "CARD-123"},
    )
    args = argparse.Namespace(db="d.sqlite3", prompt="hi", model="claude-sonnet-5-5")
    cli.cmd_estimate(args)
    assert "CARD-123" in capsys.readouterr().out


def test_cmd_tree_no_sessions_prints_hint(store, capsys):
    args = argparse.Namespace(db="d.sqlite3", session_id=None)
    cli.cmd_tree(args)
    assert "No sessions recorded yet" in capsys.readouterr().out


def test_cmd_tree_lists_recent_sessions(store, capsys):
    make_session("sess_list_test")
    args = argparse.Namespace(db="d.sqlite3", session_id=None)
    cli.cmd_tree(args)
    out = capsys.readouterr().out
    assert "sess_list_test" in out
    assert "Recent TraceGate DAG Sessions" in out


def test_cmd_tree_unknown_session_exits_1(store):
    args = argparse.Namespace(db="d.sqlite3", session_id="ghost")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_tree(args)
    assert exc.value.code == 1


def test_cmd_tree_prints_ascii_dag(store, capsys):
    make_session("sess_tree_test")
    args = argparse.Namespace(db="d.sqlite3", session_id="sess_tree_test")
    cli.cmd_tree(args)
    out = capsys.readouterr().out
    assert "DAG Execution Tree" in out
    assert "USER_PROMPT" in out
    assert "PREFLIGHT_LOCK" in out


def test_cmd_test_invokes_pytest_and_exits_with_its_code(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda cmd: calls.append(cmd) or 3)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_test(argparse.Namespace())
    assert exc.value.code == 3
    assert calls[0][1:] == ["-m", "pytest", "test_tracegate_agentic_loop.py", "-v"]


# ---------------------------------------------------------------------------
# push_repo helper
# ---------------------------------------------------------------------------

def test_push_repo_run_cmd_returns_stdout():
    assert push_repo.run_cmd(["echo", "hello"]) == "hello"


def test_push_repo_run_cmd_exits_on_failure():
    with pytest.raises(SystemExit) as exc:
        push_repo.run_cmd(["false"])
    assert exc.value.code != 0


def test_push_repo_main_clean_tree_with_origin(monkeypatch, capsys):
    executed = []

    def fake_run_cmd(cmd):
        executed.append(cmd)
        if cmd[:2] == ["git", "status"]:
            return ""  # clean tree
        if cmd[:2] == ["git", "remote"]:
            return "origin"
        return ""

    pushed = {}
    monkeypatch.setattr(push_repo, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: pushed.setdefault("cmd", cmd))
    push_repo.main()
    assert ["git", "commit", "-m"] not in [c[:3] for c in executed]
    assert pushed["cmd"] == ["git", "push", "-u", "origin", "main"]
    assert "no new files to commit" in capsys.readouterr().out


def test_push_repo_main_asks_for_remote_when_missing(monkeypatch):
    monkeypatch.setattr(push_repo, "run_cmd", lambda cmd: "")
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    with pytest.raises(SystemExit) as exc:
        push_repo.main()
    assert exc.value.code == 1
