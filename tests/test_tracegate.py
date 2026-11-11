#!/usr/bin/env python3
"""Real tests for TraceGate: they run the actual helper and installer as
subprocesses against temp directories and real git repos. No mocks, no
invented data. Run: python3 -m unittest discover -s tests -v
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "tracegate" / "scripts" / "tracegate.py"
INSTALLER = ROOT / "skills" / "tracegate" / "install.py"
GIT = shutil.which("git")


def run(args, env_extra=None, cwd=None, check=False, stdin=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                       env=env, cwd=cwd, input=stdin)
    if check and r.returncode != 0:
        raise AssertionError(f"{args} failed ({r.returncode}): {r.stderr}")
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "tg-home"
        self.env = {"TRACEGATE_HOME": str(self.home)}
        self.proj = Path(self.tmp.name) / "proj"
        self.proj.mkdir()

    def tg(self, *args, **kw):
        return run([str(SCRIPT), *args], env_extra=self.env, **kw)

    def start_session(self, d=None):
        args = ["start", "--model", "claude-sonnet-5.5", "--tool", "claude-code",
                "--task", "test task", "--est-in", "50000", "--est-out", "8000"]
        if d is not None:
            args += ["--dir", str(d)]
        return self.tg(*args, check=True)

    def make_git_repo(self):
        subprocess.run([GIT, "init", "-q"], cwd=self.proj, check=True)
        subprocess.run([GIT, "config", "user.email", "t@example.com"], cwd=self.proj, check=True)
        subprocess.run([GIT, "config", "user.name", "T"], cwd=self.proj, check=True)
        (self.proj / "app.py").write_text("print('v1')\n")
        subprocess.run([GIT, "add", "app.py"], cwd=self.proj, check=True)
        subprocess.run([GIT, "commit", "-qm", "init"], cwd=self.proj, check=True)

    def sessions(self):
        d = self.home / "sessions"
        return sorted(d.rglob("*.jsonl")) if d.exists() else []


class TestStartAndCost(Base):
    def test_estimate_math_with_explicit_prices(self):
        r = self.tg("start", "--task", "t", "--est-in", "50000", "--est-out", "8000",
                    "--price-in", "2", "--price-out", "10", check=True)
        self.assertIn("STARTED", r.stdout)
        self.assertIn("50,000 in -> $0.1000", r.stdout)
        self.assertIn("8,000 out -> $0.0800", r.stdout)
        self.assertIn("total ~$0.18", r.stdout)
        self.assertNotIn("INR", r.stdout)

    def test_price_lookup_from_real_pricing_json(self):
        r = self.start_session()
        # claude-sonnet-5.5 is really in pricing.json at $2/$10 (checked 2026-11-11)
        self.assertIn("$2.0/1M input, $10.0/1M output", r.stdout)

    def test_unknown_model_says_price_unknown(self):
        r = self.tg("start", "--model", "no-such-model", "--task", "t",
                    "--est-in", "1", "--est-out", "1", check=True)
        self.assertIn("price per 1M tokens unknown", r.stdout)

    def test_pricing_json_has_no_placeholder_model(self):
        data = json.loads((ROOT / "skills" / "tracegate" / "pricing.json").read_text())
        self.assertNotIn("example-model", data["models"])
        self.assertTrue(data["models"])

    def test_cost_budget_exit_codes(self):
        self.start_session()
        self.tg("log", "tool", "work", "--tok-in", "1000000", "--tok-out", "1000000", check=True)
        # $2 + $10 = $12 logged
        under = self.tg("cost", "--budget", "20")
        self.assertEqual(under.returncode, 0, under.stderr)
        self.assertIn("Under budget", under.stdout)
        over = self.tg("cost", "--budget", "5")
        self.assertEqual(over.returncode, 1)
        self.assertIn("OVER BUDGET", over.stdout)
        self.assertIn("Ask the user whether to continue", over.stdout)


class TestProjectSessions(Base):
    def test_start_resumes_open_session_in_same_project(self):
        self.start_session(self.proj)
        r = self.start_session(self.proj)
        self.assertIn("RESUMED", r.stdout)
        self.assertEqual(len(self.sessions()), 1)

    def test_start_after_end_opens_a_new_session(self):
        self.start_session(self.proj)
        self.tg("end", "--dir", str(self.proj), check=True)
        r = self.start_session(self.proj)
        self.assertIn("STARTED", r.stdout)
        self.assertEqual(len(self.sessions()), 2)

    def test_new_flag_forces_a_fresh_session(self):
        self.start_session(self.proj)
        r = self.tg("start", "--new", "--dir", str(self.proj), check=True)
        self.assertIn("STARTED", r.stdout)
        self.assertEqual(len(self.sessions()), 2)

    def test_different_projects_get_different_sessions(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.start_session(self.proj)
        self.start_session(other)
        self.assertEqual(len(self.sessions()), 2)
        self.assertNotEqual(self.sessions()[0].parent, self.sessions()[1].parent)

    def test_commands_follow_the_dir_session(self):
        self.start_session(self.proj)
        r = self.tg("log", "note", "hello", "--dir", str(self.proj), check=True)
        self.assertIn("logged step", r.stdout)
        rep = self.tg("report", "--dir", str(self.proj), check=True).stdout
        self.assertIn("hello", rep)


class TestLoggingAndReport(Base):
    def test_log_and_end_report(self):
        self.start_session()
        self.tg("log", "edit", "changed app.py", "--files", "app.py", "--tok-in", "100",
                "--tok-out", "50", check=True)
        r = self.tg("end", "--summary", "done", check=True)
        self.assertIn("Steps logged: 1", r.stdout)
        self.assertIn("edit: changed app.py", r.stdout)
        self.assertIn("app.py", r.stdout)
        self.assertIn("ended", r.stdout)

    def test_session_files_are_private(self):
        self.start_session()
        for f in self.sessions():
            mode = stat.S_IMODE(f.stat().st_mode)
            self.assertEqual(mode, 0o600, oct(mode))
        d = self.sessions()[0].parent
        dmode = stat.S_IMODE(d.stat().st_mode)
        self.assertEqual(dmode, 0o700, oct(dmode))

    def test_corrupted_log_line_does_not_crash_report(self):
        self.start_session()
        f = self.sessions()[0]
        with open(f, "a") as fh:
            fh.write("not json at all\n")
        r = self.tg("report", check=True)
        self.assertIn("report", r.stdout)


@unittest.skipUnless(GIT, "git not available")
class TestCheckpointsAndRewind(Base):
    def setUp(self):
        super().setUp()
        self.make_git_repo()
        self.start_session(self.proj)

    def refs(self):
        r = subprocess.run([GIT, "for-each-ref", "refs/tracegate/", "--format=%(refname)"],
                           cwd=self.proj, capture_output=True, text=True, check=True)
        return r.stdout.split()

    def test_checkpoint_leaves_staging_untouched_and_creates_ref(self):
        (self.proj / "staged.txt").write_text("staged\n")
        subprocess.run([GIT, "add", "staged.txt"], cwd=self.proj, check=True)
        before = subprocess.run([GIT, "status", "--porcelain"], cwd=self.proj,
                                capture_output=True, text=True, check=True).stdout
        r = self.tg("checkpoint", "before-risky", "--dir", str(self.proj), check=True)
        self.assertIn("git commit", r.stdout)
        after = subprocess.run([GIT, "status", "--porcelain"], cwd=self.proj,
                               capture_output=True, text=True, check=True).stdout
        self.assertEqual(before, after)
        self.assertTrue(any("before-risky" in ref for ref in self.refs()))
        # the user's branch did not move
        head = subprocess.run([GIT, "log", "--oneline"], cwd=self.proj,
                              capture_output=True, text=True, check=True).stdout
        self.assertEqual(len(head.strip().splitlines()), 1)

    def test_rewind_requires_yes(self):
        self.tg("checkpoint", "c1", "--dir", str(self.proj), check=True)
        (self.proj / "app.py").write_text("print('broken')\n")
        r = self.tg("rewind", "c1", "--dir", str(self.proj))
        self.assertEqual(r.returncode, 2)
        self.assertEqual((self.proj / "app.py").read_text(), "print('broken')\n")

    def test_rewind_restores_files_and_continues_session(self):
        self.tg("checkpoint", "c1", "--dir", str(self.proj), check=True)
        (self.proj / "app.py").write_text("print('broken')\n")
        (self.proj / "newfile.txt").write_text("made after checkpoint\n")
        r = self.tg("rewind", "c1", "--dir", str(self.proj), "--yes", check=True)
        self.assertIn("rewound to checkpoint 1 'c1'", r.stdout)
        self.assertEqual((self.proj / "app.py").read_text(), "print('v1')\n")
        # file created after the checkpoint is left in place and reported
        self.assertTrue((self.proj / "newfile.txt").exists())
        self.assertIn("newfile.txt", r.stdout)
        # safety snapshot exists
        self.assertTrue(any("pre-rewind-safety" in ref for ref in self.refs()))
        # session continues: logging still works and report shows the rewind
        self.tg("log", "note", "after rewind", "--dir", str(self.proj), check=True)
        rep = self.tg("report", "--dir", str(self.proj), check=True).stdout
        self.assertIn("Rewinds: 1", rep)
        self.assertIn("back to 'c1'", rep)

    def test_rewind_by_number(self):
        self.tg("checkpoint", "c1", "--dir", str(self.proj), check=True)
        self.tg("checkpoint", "c2", "--dir", str(self.proj), check=True)
        (self.proj / "app.py").write_text("print('broken')\n")
        self.tg("rewind", "1", "--dir", str(self.proj), "--yes", check=True)
        self.assertEqual((self.proj / "app.py").read_text(), "print('v1')\n")

    def test_checkpoint_outside_git_repo_is_label_only(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        self.start_session(plain)  # sessions are per project: this dir needs its own
        r = self.tg("checkpoint", "nogit", "--dir", str(plain), check=True)
        self.assertIn("not a git repo, label only", r.stdout)


@unittest.skipUnless(GIT, "git not available")
class TestClean(Base):
    def setUp(self):
        super().setUp()
        self.make_git_repo()
        self.start_session(self.proj)
        self.tg("checkpoint", "c1", "--dir", str(self.proj), check=True)

    def refs(self):
        r = subprocess.run([GIT, "for-each-ref", "refs/tracegate/", "--format=%(refname)"],
                           cwd=self.proj, capture_output=True, text=True, check=True)
        return r.stdout.split()

    def test_clean_requires_yes(self):
        r = self.tg("clean", "--dir", str(self.proj))
        self.assertEqual(r.returncode, 2)
        self.assertIn("Re-run with --yes", r.stdout)
        self.assertTrue(self.refs())  # nothing deleted

    def test_clean_removes_refs_and_old_logs_keeps_recent(self):
        old_session = self.sessions()[0]
        past = time.time() - 40 * 86400
        os.utime(old_session, (past, past))
        # a second, recent session in another project must survive
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.start_session(other)
        r = self.tg("clean", "--dir", str(self.proj), "--yes", check=True)
        self.assertIn("cleaned:", r.stdout)
        self.assertFalse(self.refs())
        self.assertFalse(old_session.exists())
        remaining = self.sessions()
        self.assertEqual(len(remaining), 1)
        self.assertIn("other", remaining[0].parent.name)

    def test_clean_nothing_to_do(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        r = self.tg("clean", "--dir", str(plain), check=True)
        self.assertIn("Nothing to clean", r.stdout)


@unittest.skipUnless(GIT, "git not available")
class TestFullRestoreAndDeleteNew(Base):
    def setUp(self):
        super().setUp()
        self.make_git_repo()
        (self.proj / ".gitignore").write_text(".env\n")
        subprocess.run([GIT, "add", ".gitignore"], cwd=self.proj, check=True)
        subprocess.run([GIT, "commit", "-qm", "ignore .env"], cwd=self.proj, check=True)
        (self.proj / ".env").write_text("SECRET=one\n")
        self.start_session(self.proj)

    def status(self):
        return subprocess.run([GIT, "status", "--porcelain"], cwd=self.proj,
                              capture_output=True, text=True, check=True).stdout

    def test_full_checkpoint_restores_ignored_files_without_staging_them(self):
        self.tg("checkpoint", "with-env", "--full", "--dir", str(self.proj), check=True)
        (self.proj / ".env").write_text("SECRET=two\n")
        self.tg("rewind", "with-env", "--dir", str(self.proj), "--yes", check=True)
        self.assertEqual((self.proj / ".env").read_text(), "SECRET=one\n")
        # the ignored file is back on disk but must not be staged as tracked
        self.assertNotIn(".env", self.status())

    def test_plain_checkpoint_leaves_ignored_files_alone(self):
        self.tg("checkpoint", "plain", "--dir", str(self.proj), check=True)
        (self.proj / ".env").write_text("SECRET=two\n")
        self.tg("rewind", "plain", "--dir", str(self.proj), "--yes", check=True)
        self.assertEqual((self.proj / ".env").read_text(), "SECRET=two\n")

    def test_delete_new_requires_yes_then_deletes(self):
        self.tg("checkpoint", "c1", "--dir", str(self.proj), check=True)
        (self.proj / "scratch.txt").write_text("temp\n")
        r = self.tg("rewind", "c1", "--dir", str(self.proj), "--delete-new")
        self.assertEqual(r.returncode, 2)
        self.assertIn("would be DELETED", r.stdout)
        self.assertTrue((self.proj / "scratch.txt").exists())
        r = self.tg("rewind", "c1", "--dir", str(self.proj), "--delete-new", "--yes", check=True)
        self.assertIn("Deleted", r.stdout)
        self.assertFalse((self.proj / "scratch.txt").exists())


@unittest.skipUnless(GIT, "git not available")
class TestBranchAndSwitch(Base):
    def setUp(self):
        super().setUp()
        self.make_git_repo()
        self.start_session(self.proj)

    def test_branch_and_switch_back(self):
        r = self.tg("branch", "idea-a", "--dir", str(self.proj), check=True)
        self.assertIn("branch 'idea-a'", r.stdout)
        (self.proj / "app.py").write_text("print('idea-b')\n")
        r = self.tg("switch", "idea-a", "--dir", str(self.proj))
        self.assertEqual(r.returncode, 2)  # plan only
        self.assertEqual((self.proj / "app.py").read_text(), "print('idea-b')\n")
        self.tg("switch", "idea-a", "--dir", str(self.proj), "--yes", check=True)
        self.assertEqual((self.proj / "app.py").read_text(), "print('v1')\n")
        rep = self.tg("report", "--dir", str(self.proj), check=True).stdout
        self.assertIn("idea-a", rep)
        self.assertIn("switch: to 'idea-a'", rep)

    def test_switch_unknown_branch_lists_existing(self):
        self.tg("branch", "idea-a", "--dir", str(self.proj), check=True)
        r = self.tg("switch", "nope", "--dir", str(self.proj), "--yes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("idea-a", r.stderr)

    def test_branch_ref_is_under_refs_tracegate_branches(self):
        self.tg("branch", "idea-a", "--dir", str(self.proj), check=True)
        refs = subprocess.run([GIT, "for-each-ref", "refs/tracegate/branches/",
                               "--format=%(refname)"], cwd=self.proj,
                              capture_output=True, text=True, check=True).stdout
        self.assertIn("refs/tracegate/branches/idea-a", refs)


class TestHooks(Base):
    def hook(self, event, tool, payload, check=True):
        return self.tg("hook", event, "--tool", tool, stdin=json.dumps(payload), check=check)

    def payload(self, **kw):
        base = {"cwd": str(self.proj), "session_id": "tool-sid-123",
                "transcript_path": str(Path(self.tmp.name) / "t.jsonl"),
                "model": "claude-sonnet-5.5"}
        base.update(kw)
        return base

    def test_session_start_claude_plain_text_and_link_recorded(self):
        r = self.hook("session-start", "claude-code", self.payload())
        self.assertIn("TraceGate backtracking STARTED", r.stdout)
        recs = [json.loads(l) for l in self.sessions()[0].read_text().splitlines()]
        link = next(x for x in recs if x["type"] == "link")
        self.assertEqual(link["tool_session_id"], "tool-sid-123")
        self.assertTrue(link["transcript_path"].endswith("t.jsonl"))

    def test_session_start_gemini_is_pure_json_with_additional_context(self):
        r = self.hook("session-start", "gemini-cli", self.payload())
        out = json.loads(r.stdout)  # must be exactly one JSON object, no extra text
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TraceGate backtracking", ctx)

    def test_session_start_antigravity_inject_steps(self):
        payload = {"workspacePaths": [str(self.proj)], "conversationId": "conv-1",
                   "modelName": "gemini-3.6-flash-medium"}
        r = self.hook("session-start", "antigravity", payload)
        out = json.loads(r.stdout)
        msg = out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("TraceGate backtracking", msg)

    def test_session_start_codex_plain_text(self):
        r = self.hook("session-start", "codex", self.payload())
        self.assertIn("TraceGate backtracking STARTED", r.stdout)

    def test_second_session_start_resumes(self):
        self.hook("session-start", "claude-code", self.payload())
        r = self.hook("session-start", "claude-code", self.payload())
        self.assertIn("RESUMED", r.stdout)
        self.assertEqual(len(self.sessions()), 1)

    def test_post_tool_logs_the_edit(self):
        self.hook("session-start", "claude-code", self.payload())
        r = self.hook("post-tool", "claude-code",
                      self.payload(hook_event_name="PostToolUse", tool_name="Edit",
                                   tool_input={"file_path": "app.py"}))
        self.assertEqual(r.stdout, "")  # claude-code post-tool: silent success
        rep = self.tg("report", "--dir", str(self.proj), check=True).stdout
        self.assertIn("auto: Edit", rep)
        self.assertIn("app.py", rep)

    def test_post_tool_antigravity_returns_empty_object(self):
        self.hook("session-start", "antigravity",
                  {"workspacePaths": [str(self.proj)], "conversationId": "c1"})
        r = self.hook("post-tool", "antigravity",
                      {"workspacePaths": [str(self.proj)],
                       "toolCall": {"name": "write_to_file", "args": {"TargetFile": "x.py"}}})
        self.assertEqual(json.loads(r.stdout), {})
        rep = self.tg("report", "--dir", str(self.proj), check=True).stdout
        self.assertIn("auto: write_to_file", rep)

    def test_session_end_closes_open_session(self):
        self.hook("session-start", "claude-code", self.payload())
        self.hook("session-end", "claude-code", self.payload())
        rep = self.tg("report", "--dir", str(self.proj), check=True).stdout
        self.assertIn("ended", rep)
        self.assertIn("auto: tool session ended", self.sessions()[0].read_text())


class TestMeasuredTokens(Base):
    def write(self, name, lines):
        p = Path(self.tmp.name) / name
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        return p

    def link_via_hook(self, tool, transcript):
        self.tg("hook", "session-start", "--tool", tool, check=True,
                stdin=json.dumps({"cwd": str(self.proj), "session_id": "s1",
                                  "transcript_path": str(transcript),
                                  "model": "claude-sonnet-5.5"}))

    def test_claude_transcript_summed_and_deduped(self):
        t = self.write("claude.jsonl", [
            {"type": "user", "message": {"role": "user"}},
            {"type": "assistant", "uuid": "u1", "message": {"id": "m1",
                "usage": {"input_tokens": 100, "output_tokens": 10,
                          "cache_read_input_tokens": 500, "cache_creation_input_tokens": 50}}},
            # a retry of the same message must not double count
            {"type": "assistant", "uuid": "u2", "message": {"id": "m1",
                "usage": {"input_tokens": 100, "output_tokens": 10,
                          "cache_read_input_tokens": 500, "cache_creation_input_tokens": 50}}},
            {"type": "assistant", "uuid": "u3", "message": {"id": "m2",
                "usage": {"input_tokens": 200, "output_tokens": 20}}},
        ])
        self.link_via_hook("claude-code", t)
        r = self.tg("tokens", "--dir", str(self.proj), check=True)
        self.assertIn("input (uncached): 300", r.stdout)
        self.assertIn("output: 30", r.stdout)
        self.assertIn("cache read: 500", r.stdout)

    def test_codex_rollout_takes_last_cumulative_event(self):
        t = self.write("rollout.jsonl", [
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 400,
                                      "output_tokens": 100}}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 2500, "cached_input_tokens": 1000,
                                      "output_tokens": 300}}}},
        ])
        self.link_via_hook("codex", t)
        r = self.tg("tokens", "--dir", str(self.proj), check=True)
        self.assertIn("input (uncached): 1,500", r.stdout)  # 2500 - 1000 cached, not summed
        self.assertIn("output: 300", r.stdout)

    def test_cost_prefers_measured_over_logged(self):
        t = self.write("claude2.jsonl", [
            {"type": "assistant", "message": {"id": "m1",
                "usage": {"input_tokens": 1000000, "output_tokens": 0}}},
        ])
        self.link_via_hook("claude-code", t)  # model claude-sonnet-5.5: $2/1M in
        self.tg("log", "tool", "rough guess", "--tok-in", "5", "--tok-out", "5",
                "--dir", str(self.proj), check=True)
        r = self.tg("cost", "--dir", str(self.proj), check=True)
        self.assertIn("measured from the tool's transcript", r.stdout)
        self.assertIn("1,000,000 in", r.stdout)

    def test_unsupported_tool_says_so(self):
        self.tg("start", "--tool", "gemini-cli", "--dir", str(self.proj), check=True)
        r = self.tg("tokens", "--dir", str(self.proj), check=True)
        self.assertIn("not supported for gemini-cli", r.stdout)


class TestInstaller(Base):
    def test_install_to_dest_copies_skill_not_installer(self):
        dest = Path(self.tmp.name) / "skills"
        r = run([str(INSTALLER), "--dest", str(dest)], check=True)
        out = dest / "tracegate"
        self.assertTrue((out / "SKILL.md").exists())
        self.assertTrue((out / "scripts" / "tracegate.py").exists())
        self.assertTrue((out / "pricing.json").exists())
        self.assertFalse((out / "install.py").exists())

    def test_reinstall_over_existing(self):
        dest = Path(self.tmp.name) / "skills"
        run([str(INSTALLER), "--dest", str(dest)], check=True)
        run([str(INSTALLER), "--dest", str(dest)], check=True)
        self.assertTrue((dest / "tracegate" / "SKILL.md").exists())

    def test_refuses_symlink_target(self):
        dest = Path(self.tmp.name) / "skills"
        dest.mkdir()
        real = Path(self.tmp.name) / "elsewhere"
        real.mkdir()
        (dest / "tracegate").symlink_to(real)
        r = run([str(INSTALLER), "--dest", str(dest)])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlink", r.stderr)
        self.assertTrue(real.exists())

    def test_always_on_line_is_idempotent(self):
        fake_home = Path(self.tmp.name) / "fakehome"
        fake_home.mkdir()
        env = dict(os.environ, HOME=str(fake_home))
        for _ in range(2):
            r = subprocess.run([sys.executable, str(INSTALLER), "claude-code", "--always-on"],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        md = (fake_home / ".claude" / "CLAUDE.md").read_text()
        self.assertEqual(md.count("<!-- tracegate -->"), 1)
        self.assertTrue((fake_home / ".claude" / "skills" / "tracegate" / "SKILL.md").exists())


class TestInstallerHooks(Base):
    def setUp(self):
        self.fake_home = Path(self.tmp.name) if hasattr(self, "tmp") else None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake_home = Path(self.tmp.name) / "fakehome"
        self.fake_home.mkdir()
        self.env = dict(os.environ, HOME=str(self.fake_home))

    def install(self, *args):
        return subprocess.run([sys.executable, str(INSTALLER), *args],
                              capture_output=True, text=True, env=self.env)

    def test_claude_code_hooks_merged_idempotently_with_backup(self):
        settings = self.fake_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "opus"}))
        for _ in range(2):
            r = self.install("claude-code")
            self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(settings.read_text())
        self.assertEqual(data["model"], "opus")  # existing config kept
        hooks = data["hooks"]
        self.assertEqual(len(hooks["SessionStart"]), 1)
        self.assertIn("tracegate", hooks["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "Edit|Write|MultiEdit|NotebookEdit")
        backups = list(settings.parent.glob("settings.json.tracegate-bak-*"))
        self.assertTrue(backups)
        self.assertEqual(json.loads(backups[0].read_text()), {"model": "opus"})

    def test_gemini_cli_hooks_file(self):
        r = self.install("gemini-cli")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads((self.fake_home / ".gemini" / "settings.json").read_text())
        self.assertIn("AfterTool", data["hooks"])
        self.assertEqual(data["hooks"]["AfterTool"][0]["matcher"], "write_file|replace")

    def test_antigravity_hooks_file_shape(self):
        r = self.install("antigravity")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads((self.fake_home / ".gemini" / "config" / "hooks.json").read_text())
        self.assertIn("tracegate", data)
        self.assertIn("SessionStart", data["tracegate"])
        self.assertIn("Stop", data["tracegate"])

    def test_codex_hooks_file_and_trust_note(self):
        r = self.install("codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("trust", r.stdout.lower())
        data = json.loads((self.fake_home / ".codex" / "hooks.json").read_text())
        self.assertEqual(data["hooks"]["SessionStart"][0]["matcher"], "startup|resume")

    def test_opencode_gets_no_hooks_and_a_note(self):
        r = self.install("opencode")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no shell-command hooks", r.stdout)
        self.assertFalse((self.fake_home / ".config" / "opencode" / "hooks.json").exists())

    def test_no_hooks_flag(self):
        r = self.install("claude-code", "--no-hooks")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.fake_home / ".claude" / "settings.json").exists())

    def test_invalid_json_config_is_refused(self):
        settings = self.fake_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{ not json")
        r = self.install("claude-code")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not valid JSON", r.stderr)
        self.assertEqual(settings.read_text(), "{ not json")  # untouched


if __name__ == "__main__":
    unittest.main()
