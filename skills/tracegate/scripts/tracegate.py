#!/usr/bin/env python3
"""TraceGate helper. Standard library only. No network, no API keys.

Commands:
  start       open (or resume) this project's session, print the banner and a
              USD cost estimate
  log         record one step (thought, tool call, file change, note)
  checkpoint  snapshot the project as a commit under refs/tracegate/ (no commit
              on your branch, no staging change)
  rewind      restore project files to a checkpoint (worktree + index), take a
              safety snapshot first, then continue the session from there
  cost        token cost so far; with --budget, exit 1 if over
  tokens      measured token usage read from the AI tool's own transcript files
              (claude-code and codex only; see README)
  clean       prune old session logs and refs/tracegate/* refs (asks first)
  end         close the session and print the backtracking report
  report      print the report for a session (default: latest for this project)
  hook        entry point the AI tools' real hooks call (session-start,
              session-end, post-tool); installed by install.py

Data lives in ~/.tracegate/sessions/<project>/ (outside your project, mode 0700).
Sessions are keyed by git root: one project has at most one open session, and
`start` resumes it instead of fragmenting into a new one.
"""
import argparse, hashlib, json, os, re, subprocess, sys, time, uuid
from pathlib import Path

HOME = Path(os.environ.get("TRACEGATE_HOME", Path.home() / ".tracegate"))
SESS = HOME / "sessions"
PRICES = Path(__file__).resolve().parent.parent / "pricing.json"

CLEAN_DAYS = 30  # default age for `clean`


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- sessions

def git(args, cwd, env=None, check=True):
    return subprocess.run(["git"] + args, cwd=cwd, env=env,
                          capture_output=True, text=True, check=check)


def project_key(cwd):
    """(key, root_path) for the project containing cwd: git root, else cwd."""
    path = None
    try:
        path = git(["rev-parse", "--show-toplevel"], str(cwd)).stdout.strip() or None
    except Exception:
        path = None
    if not path:
        path = os.path.abspath(str(cwd))
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(path) or "root")
    return f"{base}-{hashlib.sha1(path.encode()).hexdigest()[:8]}", path


def proj_dir(cwd):
    key, _ = project_key(cwd)
    return SESS / key


def sfile(sid, cwd=None):
    """Resolve a session id to its file: per-project dir first, legacy flat
    layout second, any other project's dir last."""
    if cwd is not None:
        p = proj_dir(cwd) / f"{sid}.jsonl"
        if p.exists():
            return p
    flat = SESS / f"{sid}.jsonl"
    if flat.exists():
        return flat
    hits = sorted(SESS.glob(f"*/{sid}.jsonl")) if SESS.exists() else []
    if hits:
        return hits[-1]
    # new session: put it in the current project's dir
    return (proj_dir(cwd) if cwd is not None else SESS) / f"{sid}.jsonl"


def session_files(cwd=None):
    """Session files for this project (newest last); legacy flat files only
    when the project has none of its own."""
    files = []
    if cwd is not None:
        d = proj_dir(cwd)
        if d.exists():
            files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files and SESS.exists():
        files = sorted(SESS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files


def latest(cwd=None):
    files = session_files(cwd)
    return files[-1].stem if files else None


def append(sid, rec, cwd=None):
    p = sfile(sid, cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    if SESS.exists():
        os.chmod(SESS, 0o700)
    rec["t"] = now()
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")
    os.chmod(p, 0o600)


def read_file(p):
    out = []
    for line in Path(p).read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip a corrupted line rather than lose the whole log
    return out


def read(sid, cwd=None):
    p = sfile(sid, cwd)
    if not p.exists():
        sys.exit(f"No such session: {sid}")
    return read_file(p)


def is_open(recs):
    return not any(r["type"] == "end" for r in recs)


def find_open_session(cwd):
    for p in reversed(session_files(cwd)):
        if is_open(read_file(p)):
            return p.stem
    return None


def resolve_session(a, cwd=None):
    """Session id from --session, else the latest for this project."""
    sid = getattr(a, "session", None) or latest(cwd)
    if not sid:
        sys.exit("No session. Run start first.")
    return sid


def usd(n):
    return f"${n:,.4f}" if n < 1 else f"${n:,.2f}"


def lookup_price(model):
    try:
        data = json.loads(PRICES.read_text())
    except Exception:
        return None
    for k, v in data.get("models", {}).items():
        if k.lower() == (model or "").lower():
            return v["input_per_1m"], v["output_per_1m"]
    return None


# ---------------------------------------------------------------- start

def start_session(cwd, tool=None, model=None, task=None, est_in=0, est_out=0,
                  price_in=None, price_out=None, force_new=False):
    """Open a session for this project, or resume the open one. Returns
    (sid, resumed, banner_lines)."""
    if price_in is None or price_out is None:
        found = lookup_price(model)
        if found:
            price_in, price_out = found
    _, root = project_key(cwd)
    if not force_new:
        sid = find_open_session(cwd)
        if sid:
            append(sid, {"type": "resume"}, cwd)
            lines = [f"TraceGate backtracking RESUMED  (session {sid})",
                     "Same project, same open session. Keep logging steps."]
            return sid, True, lines
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    append(sid, {"type": "start", "model": model, "tool": tool, "task": task,
                 "project": root, "est_in": est_in, "est_out": est_out,
                 "price_in": price_in, "price_out": price_out}, cwd)
    lines = [f"TraceGate backtracking STARTED  (session {sid})"]
    if model:
        lines.append(f"Model: {model}")
    if price_in is None or price_out is None:
        lines.append("Cost estimate: price per 1M tokens unknown. Give --price-in and --price-out (USD).")
    else:
        ci, co = est_in / 1e6 * price_in, est_out / 1e6 * price_out
        lines.append(f"Price: ${price_in}/1M input, ${price_out}/1M output (USD)")
        lines.append(f"Estimate: {est_in:,} in -> {usd(ci)}, {est_out:,} out -> {usd(co)}, total ~{usd(ci + co)}")
        lines.append("Rough estimate only. Actual usage depends on the session.")
    return sid, False, lines


def cmd_start(a):
    _, _, lines = start_session(a.dir, tool=a.tool, model=a.model, task=a.task,
                                est_in=a.est_in, est_out=a.est_out,
                                price_in=a.price_in, price_out=a.price_out,
                                force_new=a.new)
    for line in lines:
        print(line)


def cmd_log(a):
    sid = resolve_session(a, a.dir)
    append(sid, {"type": "step", "kind": a.kind, "text": a.text,
                 "files": a.files or [], "tok_in": a.tok_in, "tok_out": a.tok_out}, a.dir)
    print(f"logged step in {sid}")


# ---------------------------------------------------------------- git snapshots

def git_snapshot(cwd, sid, label):
    """Snapshot the working tree as a dangling commit under refs/tracegate/.

    Uses a temporary index file so the user's real index and staging area are
    never touched, then stores the tree as a commit reachable from
    refs/tracegate/<sid>/<label> so it survives git gc and can be rewound to.
    Returns the commit sha, or None when cwd is not inside a git repo.
    """
    try:
        top = git(["rev-parse", "--show-toplevel"], str(cwd)).stdout.strip()
    except Exception:
        return None
    idx = Path(top) / ".git" / f"tracegate_index_{os.getpid()}"
    env = dict(os.environ, GIT_INDEX_FILE=str(idx))
    try:
        git(["add", "-A"], top, env)
        tree = git(["write-tree"], top, env).stdout.strip()
    except Exception:
        return None
    finally:
        idx.unlink(missing_ok=True)
    head = git(["rev-parse", "--verify", "HEAD"], top, check=False)
    cmd = ["commit-tree", tree]
    if head.returncode == 0:
        cmd += ["-p", head.stdout.strip()]
    commit = git(cmd + ["-m", f"tracegate checkpoint: {label}"], top).stdout.strip()
    ref = f"refs/tracegate/{sid}/{label.replace(' ', '-')[:40]}"
    git(["update-ref", ref, commit], top)
    return commit


def cmd_checkpoint(a):
    sid = resolve_session(a, a.dir)
    n = sum(1 for r in read(sid, a.dir) if r["type"] == "checkpoint") + 1
    label = a.label
    sha = git_snapshot(a.dir, sid, f"{n}-{label}")
    append(sid, {"type": "checkpoint", "n": n, "label": label, "commit": sha}, a.dir)
    print(f"checkpoint {n} '{label}': " + (f"git commit {sha} (ref refs/tracegate/{sid}/{n}-{label.replace(' ', '-')[:40]})"
                                            if sha else "not a git repo, label only"))


def find_checkpoint(recs, which):
    cps = [r for r in recs if r["type"] == "checkpoint"]
    if not cps:
        sys.exit("No checkpoints in this session.")
    if which is None:
        return cps[-1]
    if which.isdigit():
        for c in cps:
            if c.get("n") == int(which):
                return c
    for c in reversed(cps):  # latest matching label wins
        if c["label"] == which:
            return c
    labels = ", ".join(f"{c.get('n')}:{c['label']}" for c in cps)
    sys.exit(f"No checkpoint '{which}'. Have: {labels}")


def cmd_rewind(a):
    sid = resolve_session(a, a.dir)
    recs = read(sid, a.dir)
    cp = find_checkpoint(recs, a.checkpoint)
    if not cp.get("commit"):
        sys.exit(f"Checkpoint '{cp['label']}' has no git snapshot (was not a git repo).")
    try:
        top = git(["rev-parse", "--show-toplevel"], a.dir).stdout.strip()
    except Exception:
        sys.exit("Not inside a git repo; cannot rewind.")
    if not a.yes:
        print(f"Would rewind '{top}' to checkpoint {cp.get('n')} '{cp['label']}' ({cp['commit'][:12]}).")
        print("This overwrites tracked files (worktree + index) with the checkpoint state.")
        print("Files created after the checkpoint are left in place. Your commits are not touched.")
        print("Re-run with --yes to do it. A safety snapshot of the current state is taken first.")
        sys.exit(2)
    safety = git_snapshot(top, sid, "pre-rewind-safety")
    git(["restore", "--source=" + cp["commit"], "--staged", "--worktree", "--", "."], top)
    untracked = git(["status", "--porcelain"], top).stdout
    new_files = [l[3:] for l in untracked.splitlines() if l.startswith("?? ")]
    append(sid, {"type": "rewind", "to": cp["label"], "commit": cp["commit"],
                 "safety_commit": safety, "left_in_place": new_files}, a.dir)
    print(f"rewound to checkpoint {cp.get('n')} '{cp['label']}' ({cp['commit'][:12]})")
    print(f"current state before rewind saved as commit {safety[:12] if safety else 'unavailable'} "
          f"(ref refs/tracegate/{sid}/pre-rewind-safety)")
    print("Your branch and commits are untouched; the rewind shows up in git status.")
    if new_files:
        print("Created after the checkpoint, left in place:")
        for f in new_files:
            print(f"  {f}")
        print("Preview removing them: git clean -nd")
    print("Session continues. Keep logging steps.")


# ---------------------------------------------------------------- measured usage

def find_transcript(recs, tool):
    """Transcript path for this session: recorded by the hook, else located
    from the tool's session id. None when unknown."""
    link = next((r for r in reversed(recs) if r["type"] == "link"), {})
    p = link.get("transcript_path")
    if p and Path(p).expanduser().exists():
        return Path(p).expanduser()
    sid_tool = link.get("tool_session_id")
    if not sid_tool:
        return None
    if tool == "claude-code":
        hits = sorted(Path.home().glob(f".claude/projects/*/{sid_tool}.jsonl"))
    elif tool == "codex":
        hits = sorted(Path.home().glob(f".codex/sessions/**/rollout-*{sid_tool}*.jsonl"),
                      key=lambda q: q.stat().st_mtime)
    else:
        return None
    return hits[-1] if hits else None


def usage_claude(path):
    """Sum a Claude Code transcript: each assistant message carries a usage
    block. Dedupe by message id (a retried/streamed message repeats)."""
    seen, tot = set(), {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for rec in read_file(path):
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        u = msg.get("usage") or {}
        mid = msg.get("id") or rec.get("uuid")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        tot["input"] += u.get("input_tokens") or 0
        tot["output"] += u.get("output_tokens") or 0
        tot["cache_read"] += u.get("cache_read_input_tokens") or 0
        tot["cache_write"] += u.get("cache_creation_input_tokens") or 0
    return tot


def usage_codex(path):
    """Read a Codex rollout file: token_count events are cumulative, so the
    last one is the session total (never summed)."""
    last = None
    for rec in read_file(path):
        info = None
        if rec.get("type") == "token_count":
            info = rec.get("info")
        elif (rec.get("payload") or {}).get("type") == "token_count":
            info = rec["payload"].get("info")
        if info and info.get("total_token_usage"):
            last = info["total_token_usage"]
    if last is None:
        return None
    return {"input": (last.get("input_tokens") or 0) - (last.get("cached_input_tokens") or 0),
            "output": (last.get("output_tokens") or 0),
            "cache_read": last.get("cached_input_tokens") or 0,
            "cache_write": 0}


def measured_usage(recs, tool, transcript=None):
    """Measured usage from the tool's own transcript, or None. Only tools
    whose on-disk format we verified are supported; anything else is an
    honest None, not a guess."""
    path = Path(transcript).expanduser() if transcript else find_transcript(recs, tool)
    if not path or not path.exists():
        return None, None
    if tool == "claude-code":
        return usage_claude(path), path
    if tool == "codex":
        return usage_codex(path), path
    return None, None


def cmd_tokens(a):
    sid = resolve_session(a, a.dir)
    recs = read(sid, a.dir)
    start = next((r for r in recs if r["type"] == "start"), {})
    tool = a.tool or start.get("tool")
    if tool not in ("claude-code", "codex"):
        print(f"Measured tokens are not supported for {tool or 'this tool'}: TraceGate can only "
              "read Claude Code and Codex transcript files, whose formats were verified.")
        print("Other tools do not expose a verified local usage file. Token counts the AI logged "
              "(rough) still show in cost/report.")
        return
    usage, path = measured_usage(recs, tool, a.transcript)
    if usage is None:
        print("No transcript found for this session. Measured tokens need the tool hook "
              "(install.py) or --transcript PATH.")
        return
    print(f"Measured tokens (from {tool}'s own transcript {path}):")
    print(f"  input (uncached): {usage['input']:,}")
    print(f"  output: {usage['output']:,}")
    if usage["cache_read"]:
        print(f"  cache read: {usage['cache_read']:,}")
    if usage["cache_write"]:
        print(f"  cache write: {usage['cache_write']:,}")
    print("Cache tokens are billed at different rates; cost uses list input/output prices.")


# ---------------------------------------------------------------- cost / report

def session_cost(recs, tool=None, transcript=None):
    """(ti, to, pi, po, cost, source). Measured tool usage wins over the token
    counts the AI logged; either can be missing."""
    start = next((r for r in recs if r["type"] == "start"), {})
    tool = tool or start.get("tool")
    pi, po = start.get("price_in"), start.get("price_out")
    usage, _ = measured_usage(recs, tool, transcript)
    if usage and (usage["input"] or usage["output"]):
        ti, to, source = usage["input"], usage["output"], "measured"
    else:
        steps = [r for r in recs if r["type"] == "step"]
        ti = sum(s.get("tok_in") or 0 for s in steps)
        to = sum(s.get("tok_out") or 0 for s in steps)
        source = "logged"
    cost = None
    if (ti or to) and pi is not None and po is not None:
        cost = ti / 1e6 * pi + to / 1e6 * po
    return ti, to, pi, po, cost, source


SRC_LABEL = {"measured": "measured from the tool's transcript",
             "logged": "from the token counts the AI logged"}


def cmd_cost(a):
    sid = resolve_session(a, a.dir)
    ti, to, pi, po, cost, source = session_cost(read(sid, a.dir))
    if cost is None:
        print("No cost data yet (no token counts logged or measured, or prices unknown).")
        return
    line = f"Cost so far: {ti:,} in, {to:,} out -> {usd(cost)} (USD, {SRC_LABEL[source]})"
    if a.budget is None:
        print(line)
        return
    print(f"{line}; budget {usd(a.budget)}")
    if cost > a.budget:
        print("OVER BUDGET. Ask the user whether to continue before doing more work.")
        sys.exit(1)
    print("Under budget.")


def build_report(sid, cwd=None):
    recs = read(sid, cwd)
    start = next((r for r in recs if r["type"] == "start"), {})
    steps = [r for r in recs if r["type"] == "step"]
    cps = [r for r in recs if r["type"] == "checkpoint"]
    rewinds = [r for r in recs if r["type"] == "rewind"]
    end = next((r for r in recs if r["type"] == "end"), None)
    files = sorted({f for s in steps for f in s["files"]})
    ti, to, pi, po, cost, source = session_cost(recs)
    L = [f"TraceGate backtracking report  (session {sid})",
         f"Started {recs[0]['t']}" + (f", ended {end['t']}" if end else ", still open"),
         f"Model: {start.get('model') or 'unknown'}   Task: {start.get('task') or '-'}",
         f"Steps logged: {len(steps)}   Checkpoints: {len(cps)}   Rewinds: {len(rewinds)}", ""]
    for i, s in enumerate(steps, 1):
        fs = f"  [{', '.join(s['files'])}]" if s["files"] else ""
        L.append(f"{i:>3}. {s['t']} {s['kind']}: {s['text']}{fs}")
    for r in rewinds:
        L.append(f"     {r['t']} rewind: back to '{r['to']}' ({(r['commit'] or '')[:12]})")
    if cps:
        L += ["", "Checkpoints (git snapshots; your branch is untouched):"]
        L += [f"  - {c.get('n')}:{c['label']} @ {c['t']}: {c['commit'] or 'no git'}" for c in cps]
        L.append(f"  Rewind to one: python3 tracegate.py rewind <n|label> --yes")
        L.append(f"  Inspect first: git show <commit> ; diff: git diff HEAD <commit>")
    L += ["", f"Files touched: {', '.join(files) if files else 'none logged'}"]
    if cost is not None:
        L.append(f"Tokens: {ti:,} in, {to:,} out -> {usd(cost)} (USD, {SRC_LABEL[source]})")
    elif start.get("est_in") is not None and pi is not None and po is not None:
        L.append(f"No token counts logged or measured. Start-of-session estimate was ~{usd(start['est_in']/1e6*pi + start['est_out']/1e6*po)} USD.")
    else:
        L.append("No cost data available.")
    return "\n".join(L)


def cmd_end(a):
    sid = resolve_session(a, a.dir)
    rec = {"type": "end"}
    if a.summary:
        rec["summary"] = a.summary
    append(sid, rec, a.dir)
    print(build_report(sid, a.dir))


def cmd_report(a):
    sid = resolve_session(a, a.dir)
    print(build_report(sid, a.dir))


# ---------------------------------------------------------------- clean

def cmd_clean(a):
    cutoff = time.time() - a.older_than * 86400
    old = []
    if SESS.exists():
        for p in SESS.rglob("*.jsonl"):
            if p.stat().st_mtime < cutoff:
                old.append(p)
    refs, top = [], None
    try:
        top = git(["rev-parse", "--show-toplevel"], a.dir).stdout.strip()
        out = git(["for-each-ref", "refs/tracegate/", "--format=%(refname)"], top).stdout
        refs = [r for r in out.splitlines() if r.strip()]
    except Exception:
        top = None
    if not old and not refs:
        print("Nothing to clean.")
        return
    if old:
        print(f"Will delete {len(old)} session log(s) older than {a.older_than} days:")
        for p in old:
            print(f"  {p}")
    if refs:
        print(f"Will delete {len(refs)} tracegate git ref(s) in {top}:")
        for r in refs:
            print(f"  {r}")
        print("(Your branches and commits are not touched; only refs/tracegate/* snapshots.)")
    if not a.yes:
        print("Re-run with --yes to do it.")
        sys.exit(2)
    for p in old:
        p.unlink()
    if SESS.exists():
        for d in sorted(SESS.iterdir()):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    for r in refs:
        git(["update-ref", "-d", r], top)
    print(f"cleaned: {len(old)} log(s), {len(refs)} ref(s)")


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("start")
    x.add_argument("--model"); x.add_argument("--tool"); x.add_argument("--task")
    x.add_argument("--est-in", type=int, default=0); x.add_argument("--est-out", type=int, default=0)
    x.add_argument("--price-in", type=float, help="USD per 1M input tokens")
    x.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    x.add_argument("--dir", default=".")
    x.add_argument("--new", action="store_true", help="start a fresh session even if one is open")
    x.set_defaults(f=cmd_start)
    x = s.add_parser("log")
    x.add_argument("kind", choices=["thought", "tool", "edit", "note"]); x.add_argument("text")
    x.add_argument("--files", nargs="*"); x.add_argument("--tok-in", type=int); x.add_argument("--tok-out", type=int)
    x.add_argument("--session"); x.add_argument("--dir", default="."); x.set_defaults(f=cmd_log)
    x = s.add_parser("checkpoint")
    x.add_argument("label"); x.add_argument("--dir", default="."); x.add_argument("--session"); x.set_defaults(f=cmd_checkpoint)
    x = s.add_parser("rewind")
    x.add_argument("checkpoint", nargs="?", help="checkpoint number or label (default: latest)")
    x.add_argument("--dir", default="."); x.add_argument("--session")
    x.add_argument("--yes", action="store_true", help="actually rewind; without it only prints the plan")
    x.set_defaults(f=cmd_rewind)
    x = s.add_parser("cost")
    x.add_argument("--budget", type=float, help="USD limit; exits 1 when logged cost is over it")
    x.add_argument("--session"); x.add_argument("--dir", default="."); x.set_defaults(f=cmd_cost)
    x = s.add_parser("tokens")
    x.add_argument("--session"); x.add_argument("--dir", default=".")
    x.add_argument("--tool", help="override the tool recorded at start")
    x.add_argument("--transcript", help="path to the tool's transcript file")
    x.set_defaults(f=cmd_tokens)
    x = s.add_parser("clean")
    x.add_argument("--older-than", type=int, default=CLEAN_DAYS, help="days (default 30)")
    x.add_argument("--dir", default=".")
    x.add_argument("--yes", action="store_true", help="actually delete; without it only prints the plan")
    x.set_defaults(f=cmd_clean)
    x = s.add_parser("end")
    x.add_argument("--summary"); x.add_argument("--session"); x.add_argument("--dir", default=".")
    x.set_defaults(f=cmd_end)
    x = s.add_parser("report")
    x.add_argument("--session"); x.add_argument("--dir", default="."); x.set_defaults(f=cmd_report)
    a = p.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
