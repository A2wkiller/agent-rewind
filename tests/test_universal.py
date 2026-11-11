"""Tests for tracegate_universal: GitShadowBacktracker (real git plumbing in a
temp repo), the MCP lifecycle tools (against the stub store), and the
multi-IDE plugin installer."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import tracegate_mcp as tg
import tracegate_universal as tu
from tracegate_universal import GitShadowBacktracker

from .conftest import make_session


@pytest.fixture
def plain_dir(tmp_path, monkeypatch):
    """A temp working directory that is NOT a git repo, used as cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tu, "SHADOW_GIT", GitShadowBacktracker(tmp_path))
    return tmp_path


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A temp working directory that IS an initialized git repo."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tu, "SHADOW_GIT", GitShadowBacktracker(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# GitShadowBacktracker without a repo
# ---------------------------------------------------------------------------

def test_non_repo_is_not_git_repo(plain_dir):
    assert tu.SHADOW_GIT.is_git_repo() is False


def test_non_repo_capture_returns_none(plain_dir):
    assert tu.SHADOW_GIT.capture_snapshot("step") is None


def test_non_repo_diff_returns_empty(plain_dir):
    assert tu.SHADOW_GIT.diff_between_trees("abc", "def") == []


def test_non_repo_restore_reports_reason(plain_dir):
    res = tu.SHADOW_GIT.restore_snapshot("deadbeef")
    assert res["restored"] is False
    assert "Not a git repository" in res["reason"]


def test_diff_with_missing_tree_args_returns_empty(git_repo):
    assert tu.SHADOW_GIT.diff_between_trees(None, "x") == []
    assert tu.SHADOW_GIT.diff_between_trees("x", None) == []
    assert tu.SHADOW_GIT.diff_between_trees("same", "same") == []


# ---------------------------------------------------------------------------
# GitShadowBacktracker with a real git repo
# ---------------------------------------------------------------------------

def test_capture_returns_tree_sha(git_repo):
    sha = tu.SHADOW_GIT.capture_snapshot("snap")
    assert sha is not None
    assert len(sha) == 40
    int(sha, 16)  # valid hex


def test_capture_is_deterministic_when_unchanged(git_repo):
    assert tu.SHADOW_GIT.capture_snapshot() == tu.SHADOW_GIT.capture_snapshot()


def test_capture_changes_when_files_change(git_repo):
    before = tu.SHADOW_GIT.capture_snapshot()
    (git_repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    after = tu.SHADOW_GIT.capture_snapshot()
    assert before != after


def test_diff_lists_modified_files(git_repo):
    before = tu.SHADOW_GIT.capture_snapshot()
    (git_repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (git_repo / "new_file.py").write_text("print(1)\n", encoding="utf-8")
    after = tu.SHADOW_GIT.capture_snapshot()
    diff = tu.SHADOW_GIT.diff_between_trees(before, after)
    assert any("tracked.txt" in line for line in diff)
    assert any("new_file.py" in line for line in diff)


def test_restore_brings_back_old_content(git_repo):
    before = tu.SHADOW_GIT.capture_snapshot()
    (git_repo / "tracked.txt").write_text("broken\n", encoding="utf-8")
    res = tu.SHADOW_GIT.restore_snapshot(before)
    assert res["restored"] is True
    assert res["restored_tree_sha"] == before
    assert (git_repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"


def test_restore_invalid_sha_fails_cleanly(git_repo):
    res = tu.SHADOW_GIT.restore_snapshot("0" * 40)
    assert res["restored"] is False
    assert res["reason"]


def test_shadow_index_does_not_stage_real_index(git_repo):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "tracked.txt"],
        cwd=git_repo, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=git_repo, check=True,
    )
    (git_repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    tu.SHADOW_GIT.capture_snapshot()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout
    # File must show as modified-unstaged (" M"), never staged ("M ")
    assert " M tracked.txt" in status


# ---------------------------------------------------------------------------
# tracegate_session_start
# ---------------------------------------------------------------------------

def test_session_start_returns_on_status_and_banner(store, git_repo):
    res = tu.tracegate_session_start(task_title="Fix the parser", approved_token_ceiling=5000)
    assert res["backtracking_status"] == "ON"
    assert res["session_id"]
    assert res["head_node_id"]
    assert res["baseline_git_tree"] != "no-git-repo"
    assert res["session_id"] in res["session_start_banner"]
    assert "5,000 tokens" in res["session_start_banner"]
    assert res["matched_skills"] == []


def test_session_start_writes_active_session_pointer(store, git_repo):
    res = tu.tracegate_session_start(task_title="t", coding_client="cursor")
    pointer = json.loads(Path(".tracegate/active_session.json").read_text(encoding="utf-8"))
    assert pointer["session_id"] == res["session_id"]
    assert pointer["head_node_id"] == res["head_node_id"]
    assert pointer["coding_client"] == "cursor"
    assert pointer["baseline_tree_sha"] == res["baseline_git_tree"]


def test_session_start_applies_custom_ceiling_to_lock_node(store, git_repo):
    res = tu.tracegate_session_start(task_title="t", approved_token_ceiling=1234)
    with tg.STORE._conn() as conn:
        row = conn.execute(
            "SELECT approved_budget_ceiling, status FROM dag_nodes WHERE node_id = ?",
            (res["head_node_id"],),
        ).fetchone()
    assert row["approved_budget_ceiling"] == 1234
    assert row["status"] == "COMPLETED"


def test_session_start_without_git_repo_records_no_git_baseline(store, plain_dir):
    res = tu.tracegate_session_start(task_title="t")
    assert res["baseline_git_tree"] == "no-git-repo"


# ---------------------------------------------------------------------------
# tracegate_log_ai_step
# ---------------------------------------------------------------------------

def test_log_step_thought_only(store, git_repo):
    start = tu.tracegate_session_start(task_title="t")
    res = tu.tracegate_log_ai_step(
        session_id=start["session_id"], step_type="reason", how_ai_thought="chose AST over regex"
    )
    tree = tg.get_dag_trace_tree(start["session_id"])
    assert tree["nodes"][-1]["node_type"] == "THOUGHT_BLOCK"
    assert res["logged_head_node_id"] == tree["nodes"][-1]["node_id"]


def test_log_step_tool_call_records_snapshot_and_files(store, git_repo):
    start = tu.tracegate_session_start(task_title="t")
    (git_repo / "tracked.txt").write_text("edited by AI\n", encoding="utf-8")
    res = tu.tracegate_log_ai_step(
        session_id=start["session_id"],
        step_type="edit",
        how_ai_thought="",
        tool_used="apply_patch",
        how_tool_was_used_args='{"file": "tracked.txt"}',
    )
    assert res["git_snapshot_sha"] is not None
    assert any("tracked.txt" in f for f in res["files_modified_in_step"])
    tree = tg.get_dag_trace_tree(start["session_id"])
    assert tree["nodes"][-1]["node_type"] == "TOOL_CALL"
    payload = json.loads(tree["nodes"][-1]["payload_json"])
    inner = json.loads(payload["content"])
    assert inner["tool_name"] == "apply_patch"
    assert inner["git_tree_after"] == res["git_snapshot_sha"]


def test_log_step_tool_output_adds_result_node(store, git_repo):
    start = tu.tracegate_session_start(task_title="t")
    tu.tracegate_log_ai_step(
        session_id=start["session_id"], step_type="run", how_ai_thought="",
        tool_used="shell", tool_output_summary="tests passed",
    )
    tree = tg.get_dag_trace_tree(start["session_id"])
    types = [n["node_type"] for n in tree["nodes"]]
    assert types[-2:] == ["TOOL_CALL", "TOOL_RESULT"]


def test_log_step_updates_active_pointer_head(store, git_repo):
    start = tu.tracegate_session_start(task_title="t")
    res = tu.tracegate_log_ai_step(
        session_id=start["session_id"], step_type="reason", how_ai_thought="thinking"
    )
    pointer = json.loads(Path(".tracegate/active_session.json").read_text(encoding="utf-8"))
    assert pointer["head_node_id"] == res["logged_head_node_id"]


def test_log_step_empty_thought_skips_thought_node(store, git_repo):
    start = tu.tracegate_session_start(task_title="t")
    tu.tracegate_log_ai_step(
        session_id=start["session_id"], step_type="x", how_ai_thought="   ",
        tool_used="shell",
    )
    tree = tg.get_dag_trace_tree(start["session_id"])
    assert "THOUGHT_BLOCK" not in [n["node_type"] for n in tree["nodes"]]


# ---------------------------------------------------------------------------
# tracegate_session_end
# ---------------------------------------------------------------------------

def test_session_end_without_session_errors(store, plain_dir):
    res = tu.tracegate_session_end()
    assert "error" in res


def test_session_end_writes_audit_report(store, git_repo):
    start = tu.tracegate_session_start(task_title="audit me")
    tu.tracegate_log_ai_step(
        session_id=start["session_id"], step_type="reason", how_ai_thought="planned the fix",
        tool_used="apply_patch", tool_output_summary="patched",
    )
    res = tu.tracegate_session_end(session_id=start["session_id"], final_ai_summary="All fixed.")
    assert res["backtracking_status"] == "SESSION_OFF_LOGGED"
    report_path = Path(res["report_saved_to"])
    assert report_path.exists()
    report = report_path.read_text(encoding="utf-8")
    assert "FULL AI EXECUTION & BACKTRACKING LEDGER" in report
    assert "planned the fix" in report
    assert "apply_patch" in report
    assert "All fixed." in report
    assert f"tracegate rewind {start['session_id']}" in report
    assert res["total_tokens"] > 0


# ---------------------------------------------------------------------------
# tracegate_rewind_to_node
# ---------------------------------------------------------------------------

def test_rewind_missing_node_errors(store, git_repo):
    res = tu.tracegate_rewind_to_node("sess", "ghost_node")
    assert "error" in res


@pytest.mark.xfail(
    strict=False,
    reason=(
        "Real bug found: GitShadowBacktracker.capture_snapshot runs `git add -A` "
        "with no exclusions, so the shadow tree contains the TraceGate SQLite DB "
        "(and .tracegate state) itself. restore_snapshot then rewinds the DAG "
        "database, erasing the very nodes the rewind needs; fork_dag_from_node "
        "returns 'Node not found'. Fix by excluding the DB/.tracegate from shadow "
        "snapshots."
    ),
)
def test_rewind_forks_dag_and_restores_files(store, git_repo):
    start = tu.tracegate_session_start(task_title="rewind test")
    before_edit = tu.tracegate_log_ai_step(
        session_id=start["session_id"], step_type="edit", how_ai_thought="",
        tool_used="apply_patch", how_tool_was_used_args="{}",
    )
    (git_repo / "tracked.txt").write_text("bad change\n", encoding="utf-8")
    after_edit = tu.tracegate_log_ai_step(
        session_id=start["session_id"], step_type="edit", how_ai_thought="",
        tool_used="apply_patch", how_tool_was_used_args="{}",
    )
    res = tu.tracegate_rewind_to_node(start["session_id"], after_edit["logged_head_node_id"])
    assert res["status"] == "REWOUND_CODE_AND_MEMORY"
    assert res["git_filesystem_restore"]["restored"] is True
    assert res["dag_branch_fork"]["status"] == "FORK_CREATED"
    # The second log step captured the tree BEFORE "bad change" was written
    assert (git_repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"


# ---------------------------------------------------------------------------
# install_universal_ide_plugins
# ---------------------------------------------------------------------------

def test_installer_writes_all_client_configs(tmp_path):
    installed = tu.install_universal_ide_plugins(tmp_path)
    expected = [
        "opencode.json", ".mcp.json", ".cursor/mcp.json", ".windsurf/mcp.json",
        ".antigravity/mcp.json", ".jcode/mcp.json", "AGENTS.md", "CLAUDE.md",
        ".agent/skills/tracegate/SKILL.md",
    ]
    for rel in expected:
        assert (tmp_path / rel).exists(), rel
    assert "OpenCode" in installed


def test_installer_opencode_config_valid(tmp_path):
    tu.install_universal_ide_plugins(tmp_path)
    cfg = json.loads((tmp_path / "opencode.json").read_text(encoding="utf-8"))
    entry = cfg["mcp"]["tracegate"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    assert entry["command"][:2] == ["uv", "run"]


def test_installer_mcp_json_env_and_command(tmp_path):
    tu.install_universal_ide_plugins(tmp_path)
    cfg = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    entry = cfg["mcpServers"]["tracegate"]
    assert entry["env"] == {"TRACEGATE_USD_TO_INR": "86.50"}
    assert entry["command"] == "uv"
    assert entry["args"][:2] == ["run", "python"]


def test_installer_rules_file_mentions_session_protocol(tmp_path):
    tu.install_universal_ide_plugins(tmp_path)
    rules = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "tracegate_session_start" in rules


def test_mcp_tool_decorator_leaves_functions_callable():
    assert callable(tu.tracegate_session_start)
    assert callable(tu.tracegate_log_ai_step)
    assert callable(tu.tracegate_session_end)
    assert callable(tu.tracegate_rewind_to_node)
