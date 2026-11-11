#!/usr/bin/env python3
"""TraceGate helper. Standard library only. No network, no API keys.

Commands:
  start       open a session, print the banner and a USD cost estimate
  log         record one step (thought, tool call, file change, note)
  checkpoint  snapshot the project as a dangling git commit (no commit on your
              branch, no staging change)
  rewind      restore project files to a checkpoint (worktree + index), take a
              safety snapshot first, then continue the session from there
  cost        print logged token cost so far; with --budget, exit 1 if over
  end         close the session and print the backtracking report
  report      print the report for a session (default: latest)
Data lives in ~/.tracegate/sessions/ (outside your project, mode 0700).
"""
import argparse, json, os, subprocess, sys, time, uuid
from pathlib import Path

HOME = Path(os.environ.get("TRACEGATE_HOME", Path.home() / ".tracegate"))
SESS = HOME / "sessions"
PRICES = Path(__file__).resolve().parent.parent / "pricing.json"


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sfile(sid):
    return SESS / f"{sid}.jsonl"


def latest():
    files = sorted(SESS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if SESS.exists() else []
    return files[-1].stem if files else None


def append(sid, rec):
    SESS.mkdir(parents=True, exist_ok=True)
    os.chmod(SESS, 0o700)
    rec["t"] = now()
    p = sfile(sid)
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")
    os.chmod(p, 0o600)


def read(sid):
    p = sfile(sid)
    if not p.exists():
        sys.exit(f"No such session: {sid}")
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip a corrupted line rather than lose the whole log
    return out


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


def cmd_start(a):
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    p_in, p_out = a.price_in, a.price_out
    if p_in is None or p_out is None:
        found = lookup_price(a.model)
        if found:
            p_in, p_out = found
    append(sid, {"type": "start", "model": a.model, "tool": a.tool, "task": a.task,
                 "est_in": a.est_in, "est_out": a.est_out, "price_in": p_in, "price_out": p_out})
    print(f"TraceGate backtracking STARTED  (session {sid})")
    if a.model:
        print(f"Model: {a.model}")
    if p_in is None or p_out is None:
        print("Cost estimate: price per 1M tokens unknown. Give --price-in and --price-out (USD).")
    else:
        ci, co = a.est_in / 1e6 * p_in, a.est_out / 1e6 * p_out
        print(f"Price: ${p_in}/1M input, ${p_out}/1M output (USD)")
        print(f"Estimate: {a.est_in:,} in -> {usd(ci)}, {a.est_out:,} out -> {usd(co)}, total ~{usd(ci + co)}")
        print("Rough estimate only. Actual usage depends on the session.")
    return sid


def cmd_log(a):
    sid = a.session or latest() or sys.exit("No session. Run start first.")
    append(sid, {"type": "step", "kind": a.kind, "text": a.text,
                 "files": a.files or [], "tok_in": a.tok_in, "tok_out": a.tok_out})
    print(f"logged step in {sid}")


def git(args, cwd, env=None, check=True):
    return subprocess.run(["git"] + args, cwd=cwd, env=env,
                          capture_output=True, text=True, check=check)


def git_snapshot(cwd, sid, label):
    """Snapshot the working tree as a dangling commit under refs/tracegate/.

    Uses a temporary index file so the user's real index and staging area are
    never touched, then stores the tree as a commit reachable from
    refs/tracegate/<sid>/<label> so it survives git gc and can be rewound to.
    Returns the commit sha, or None when cwd is not inside a git repo.
    """
    try:
        top = git(["rev-parse", "--show-toplevel"], cwd).stdout.strip()
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
    sid = a.session or latest() or sys.exit("No session. Run start first.")
    n = sum(1 for r in read(sid) if r["type"] == "checkpoint") + 1
    label = a.label
    sha = git_snapshot(a.dir, sid, f"{n}-{label}")
    append(sid, {"type": "checkpoint", "n": n, "label": label, "commit": sha})
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
    sid = a.session or latest() or sys.exit("No session. Run start first.")
    recs = read(sid)
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
                 "safety_commit": safety, "left_in_place": new_files})
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


def session_cost(recs):
    start = next((r for r in recs if r["type"] == "start"), {})
    steps = [r for r in recs if r["type"] == "step"]
    ti = sum(s.get("tok_in") or 0 for s in steps)
    to = sum(s.get("tok_out") or 0 for s in steps)
    pi, po = start.get("price_in"), start.get("price_out")
    cost = None
    if (ti or to) and pi is not None and po is not None:
        cost = ti / 1e6 * pi + to / 1e6 * po
    return ti, to, pi, po, cost


def cmd_cost(a):
    sid = a.session or latest() or sys.exit("No session. Run start first.")
    ti, to, pi, po, cost = session_cost(read(sid))
    if cost is None:
        print("No cost data yet (no token counts logged, or prices unknown).")
        return
    line = f"Cost so far: {ti:,} in, {to:,} out -> {usd(cost)} (USD, from the token counts the AI logged)"
    if a.budget is None:
        print(line)
        return
    print(f"{line}; budget {usd(a.budget)}")
    if cost > a.budget:
        print("OVER BUDGET. Ask the user whether to continue before doing more work.")
        sys.exit(1)
    print("Under budget.")


def build_report(sid):
    recs = read(sid)
    start = next((r for r in recs if r["type"] == "start"), {})
    steps = [r for r in recs if r["type"] == "step"]
    cps = [r for r in recs if r["type"] == "checkpoint"]
    rewinds = [r for r in recs if r["type"] == "rewind"]
    end = next((r for r in recs if r["type"] == "end"), None)
    files = sorted({f for s in steps for f in s["files"]})
    ti, to, pi, po, cost = session_cost(recs)
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
        L.append(f"Logged tokens: {ti:,} in, {to:,} out -> {usd(cost)} (USD, from the token counts the AI logged)")
    elif start.get("est_in") is not None and pi is not None and po is not None:
        L.append(f"No token counts logged. Start-of-session estimate was ~{usd(start['est_in']/1e6*pi + start['est_out']/1e6*po)} USD.")
    else:
        L.append("No cost data available.")
    return "\n".join(L)


def cmd_end(a):
    sid = a.session or latest() or sys.exit("No session. Run start first.")
    if a.summary:
        append(sid, {"type": "end", "summary": a.summary})
    else:
        append(sid, {"type": "end"})
    print(build_report(sid))


def cmd_report(a):
    print(build_report(a.session or latest() or sys.exit("No sessions found.")))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("start")
    x.add_argument("--model"); x.add_argument("--tool"); x.add_argument("--task")
    x.add_argument("--est-in", type=int, default=0); x.add_argument("--est-out", type=int, default=0)
    x.add_argument("--price-in", type=float, help="USD per 1M input tokens")
    x.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    x.set_defaults(f=cmd_start)
    x = s.add_parser("log")
    x.add_argument("kind", choices=["thought", "tool", "edit", "note"]); x.add_argument("text")
    x.add_argument("--files", nargs="*"); x.add_argument("--tok-in", type=int); x.add_argument("--tok-out", type=int)
    x.add_argument("--session"); x.set_defaults(f=cmd_log)
    x = s.add_parser("checkpoint")
    x.add_argument("label"); x.add_argument("--dir", default="."); x.add_argument("--session"); x.set_defaults(f=cmd_checkpoint)
    x = s.add_parser("rewind")
    x.add_argument("checkpoint", nargs="?", help="checkpoint number or label (default: latest)")
    x.add_argument("--dir", default="."); x.add_argument("--session")
    x.add_argument("--yes", action="store_true", help="actually rewind; without it only prints the plan")
    x.set_defaults(f=cmd_rewind)
    x = s.add_parser("cost")
    x.add_argument("--budget", type=float, help="USD limit; exits 1 when logged cost is over it")
    x.add_argument("--session"); x.set_defaults(f=cmd_cost)
    x = s.add_parser("end")
    x.add_argument("--summary"); x.add_argument("--session"); x.set_defaults(f=cmd_end)
    x = s.add_parser("report")
    x.add_argument("--session"); x.set_defaults(f=cmd_report)
    a = p.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
