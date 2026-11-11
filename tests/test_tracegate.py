#!/usr/bin/env python3
"""Real tests for TraceGate: they run the actual helper and installer as
subprocesses against temp directories and real git repos. No mocks, no
invented data. Run: python3 -m unittest discover -s tests -v
"""
import json, os, shutil, stat, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "tracegate" / "scripts" / "tracegate.py"
INSTALLER = ROOT / "skills" / "tracegate" / "install.py"
GIT = shutil.which("git")


def run(args, env_extra=None, cwd=None, check=False):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                       env=env, cwd=cwd)
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

    def start_session(self):
        return self.tg("start", "--model", "claude-sonnet-5.5", "--tool", "claude-code",
                       "--task", "test task", "--est-in", "50000", "--est-out", "8000",
                       check=True)

    def make_git_repo(self):
        subprocess.run([GIT, "init", "-q"], cwd=self.proj, check=True)
        subprocess.run([GIT, "config", "user.email", "t@example.com"], cwd=self.proj, check=True)
        subprocess.run([GIT, "config", "user.name", "T"], cwd=self.proj, check=True)
        (self.proj / "app.py").write_text("print('v1')\n")
        subprocess.run([GIT, "add", "app.py"], cwd=self.proj, check=True)
        subprocess.run([GIT, "commit", "-qm", "init"], cwd=self.proj, check=True)

    def sessions(self):
        d = self.home / "sessions"
        return sorted(d.glob("*.jsonl")) if d.exists() else []


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
        dmode = stat.S_IMODE((self.home / "sessions").stat().st_mode)
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
        self.start_session()

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
        self.tg("log", "note", "after rewind", check=True)
        rep = self.tg("report", check=True).stdout
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
        r = self.tg("checkpoint", "nogit", "--dir", str(plain), check=True)
        self.assertIn("not a git repo, label only", r.stdout)


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


if __name__ == "__main__":
    unittest.main()
