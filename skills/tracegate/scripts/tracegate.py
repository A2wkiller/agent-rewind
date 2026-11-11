#!/usr/bin/env python3
"""TraceGate helper. Standard library only. No network, no API keys.

Commands:
  start       open a session, print the banner and a USD cost estimate
  log         record one step (thought, tool call, file change, note)
  checkpoint  record a git tree snapshot of the project (no commit, no staging change)
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


def git_tree(cwd):
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None
    idx = Path(top) / ".git" / f"tracegate_index_{os.getpid()}"
    env = dict(os.environ, GIT_INDEX_FILE=str(idx))
    try:
        subprocess.run(["git", "add", "-A"], cwd=top, env=env, capture_output=True, check=True)
        return subprocess.run(["git", "write-tree"], cwd=top, env=env, capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None
    finally:
        idx.unlink(missing_ok=True)  # never leave a stale temp index in .git


def cmd_checkpoint(a):
    sid = a.session or latest() or sys.exit("No session. Run start first.")
    sha = git_tree(a.dir)
    append(sid, {"type": "checkpoint", "label": a.label, "tree": sha})
    print(f"checkpoint '{a.label}': " + (f"git tree {sha}" if sha else "not a git repo, label only"))


def build_report(sid):
    recs = read(sid)
    start = next((r for r in recs if r["type"] == "start"), {})
    steps = [r for r in recs if r["type"] == "step"]
    cps = [r for r in recs if r["type"] == "checkpoint"]
    end = next((r for r in recs if r["type"] == "end"), None)
    files = sorted({f for s in steps for f in s["files"]})
    ti = sum(s.get("tok_in") or 0 for s in steps)
    to = sum(s.get("tok_out") or 0 for s in steps)
    L = [f"TraceGate backtracking report  (session {sid})",
         f"Started {recs[0]['t']}" + (f", ended {end['t']}" if end else ", still open"),
         f"Model: {start.get('model') or 'unknown'}   Task: {start.get('task') or '-'}",
         f"Steps logged: {len(steps)}   Checkpoints: {len(cps)}", ""]
    for i, s in enumerate(steps, 1):
        fs = f"  [{', '.join(s['files'])}]" if s["files"] else ""
        L.append(f"{i:>3}. {s['t']} {s['kind']}: {s['text']}{fs}")
    if cps:
        L += ["", "Checkpoints (git tree snapshots):"]
        L += [f"  - {c['label']} @ {c['t']}: {c['tree'] or 'no git'}" for c in cps]
        L.append("  Inspect a snapshot: git diff <tree> ; restore a file: git checkout <tree> -- <path>")
    L += ["", f"Files touched: {', '.join(files) if files else 'none logged'}"]
    pi, po = start.get("price_in"), start.get("price_out")
    if (ti or to) and pi is not None and po is not None:
        L.append(f"Logged tokens: {ti:,} in, {to:,} out -> {usd(ti/1e6*pi + to/1e6*po)} (USD, from the token counts the AI logged)")
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
    x = s.add_parser("end")
    x.add_argument("--summary"); x.add_argument("--session"); x.set_defaults(f=cmd_end)
    x = s.add_parser("report")
    x.add_argument("--session"); x.set_defaults(f=cmd_report)
    a = p.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
