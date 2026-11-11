#!/usr/bin/env python3
"""Copy the tracegate skill into your agent tool's skills folder, and wire the
tool's real lifecycle hooks so backtracking starts by itself.

  python3 install.py claude-code            # ~/.claude/skills/tracegate + hooks
  python3 install.py codex                  # ~/.codex/skills/tracegate + hooks
  python3 install.py gemini-cli             # ~/.gemini/skills/tracegate + hooks
  python3 install.py antigravity            # ~/.gemini/antigravity/skills/tracegate + hooks
  python3 install.py opencode               # ~/.config/opencode/skills/tracegate (no hooks, see below)
  python3 install.py --dest /some/dir       # any folder, no hooks

Hooks are on by default for claude-code, codex, gemini-cli and antigravity
(--no-hooks to skip). They are merged into the tool's own config file; the
existing file is backed up next to the original before the first change.

Hook config files (verified against each tool's docs, 2026-11-11):
  claude-code  ~/.claude/settings.json      SessionStart / PostToolUse / SessionEnd
  gemini-cli   ~/.gemini/settings.json      SessionStart / AfterTool / SessionEnd
  antigravity  ~/.gemini/config/hooks.json  SessionStart / PostToolUse / Stop
  codex        ~/.codex/hooks.json          SessionStart / PostToolUse / SessionEnd
Codex asks you to trust new hooks on first run (the /hooks menu); they do not
run until you trust them. That is Codex's design, not a bug.
OpenCode has no shell-command hooks (only JavaScript plugins), so no hooks are
installed there; the skill alone applies.

Add --always-on to also append a one-line instruction to the tool's global
instruction file so the skill is read on the first message every session
(a belt-and-braces fallback where hooks already start the session).
"""
import argparse, json, shutil, sys, time
from pathlib import Path

H = Path.home()
TARGETS = {
    "claude-code": (H / ".claude/skills", H / ".claude/CLAUDE.md"),
    "codex": (H / ".codex/skills", H / ".codex/AGENTS.md"),
    "gemini-cli": (H / ".gemini/skills", H / ".gemini/GEMINI.md"),
    "antigravity": (H / ".gemini/antigravity/skills", H / ".gemini/GEMINI.md"),
    "opencode": (H / ".config/opencode/skills", H / ".config/opencode/AGENTS.md"),
}
MARK = "<!-- tracegate -->"
LINE = (f"{MARK}\nAt the first user message of every session, read the tracegate skill "
        "({path}/SKILL.md) and follow it.\n")

# edit-tool matchers for the post-tool auto-log hook, per tool
EDIT_MATCHERS = {
    "claude-code": "Edit|Write|MultiEdit|NotebookEdit",
    "gemini-cli": "write_file|replace",
    "antigravity": "write_to_file|replace_file_content|multi_replace_file_content",
    "codex": "apply_patch",
}
# shell-tool matchers for the pre-tool risky-command auto-checkpoint hook
SHELL_MATCHERS = {
    "claude-code": "Bash",
    "gemini-cli": "run_shell_command",
    "antigravity": "run_command",
    "codex": "Bash|shell",
}


def hook_command(out, tool, event):
    # the interpreter running this installer, by absolute path: `python3` does
    # not exist on a stock Windows box, and PATH lookups in tool hooks are not
    # reliable on any platform.
    return f'"{sys.executable}" "{out}/scripts/tracegate.py" hook {event} --tool {tool}'


def build_hooks(tool, out):
    """The entries to merge into the tool's hook config, per tool."""
    if tool not in EDIT_MATCHERS:  # opencode: no shell-command hooks exist
        return None, None, None
    start, post, end, pre = (hook_command(out, tool, e)
                             for e in ("session-start", "post-tool", "session-end", "pre-tool"))
    m, sh = EDIT_MATCHERS[tool], SHELL_MATCHERS[tool]
    if tool == "claude-code":
        return H / ".claude" / "settings.json", "hooks", {
            "SessionStart": [{"hooks": [{"type": "command", "command": start}]}],
            "PreToolUse": [{"matcher": sh, "hooks": [{"type": "command", "command": pre}]}],
            "PostToolUse": [{"matcher": m, "hooks": [{"type": "command", "command": post}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": end}]}],
        }
    if tool == "gemini-cli":
        return H / ".gemini" / "settings.json", "hooks", {
            "SessionStart": [{"hooks": [{"name": "tracegate-start", "type": "command", "command": start}]}],
            "BeforeTool": [{"matcher": sh, "hooks": [{"name": "tracegate-risky", "type": "command", "command": pre}]}],
            "AfterTool": [{"matcher": m, "hooks": [{"name": "tracegate-log", "type": "command", "command": post}]}],
            "SessionEnd": [{"hooks": [{"name": "tracegate-end", "type": "command", "command": end}]}],
        }
    if tool == "codex":
        return H / ".codex" / "hooks.json", "hooks", {
            "SessionStart": [{"matcher": "startup|resume",
                              "hooks": [{"type": "command", "command": start}]}],
            "PreToolUse": [{"matcher": sh, "hooks": [{"type": "command", "command": pre}]}],
            "PostToolUse": [{"matcher": m, "hooks": [{"type": "command", "command": post}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": end}]}],
        }
    if tool == "antigravity":
        # top level is a map of hook names, not {"hooks": ...}
        return H / ".gemini" / "config" / "hooks.json", None, {
            "tracegate": {
                "SessionStart": [{"type": "command", "command": start}],
                "PreToolUse": [{"matcher": sh, "hooks": [{"type": "command", "command": pre}]}],
                "PostToolUse": [{"matcher": m, "hooks": [{"type": "command", "command": post}]}],
                "Stop": [{"type": "command", "command": end}],
            },
        }
    return None, None, None


def has_tracegate(entry):
    return "tracegate" in json.dumps(entry)


def merge_hooks(path, top_key, entries):
    """Idempotently merge our hook entries into the tool's config file.
    Backs the file up before the first change. Never drops existing config."""
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.exit(f"refusing to edit {path}: it is not valid JSON. Fix it or move it aside.")
        if not isinstance(data, dict):
            sys.exit(f"refusing to edit {path}: expected a JSON object at the top level.")
    container = data if top_key is None else data.setdefault(top_key, {})
    changed = False
    if top_key is None:  # antigravity: {"name": {event: ...}}
        if "tracegate" not in container or not has_tracegate(container.get("tracegate")):
            container.update(entries)
            changed = True
        elif container.get("tracegate") != entries.get("tracegate"):
            container["tracegate"] = entries["tracegate"]  # refresh to this install path
            changed = True
    else:
        for event, groups in entries.items():
            existing = container.setdefault(event, [])
            for g in groups:
                if not any(has_tracegate(e) for e in existing if isinstance(e, dict)):
                    existing.append(g)
                    changed = True
    if not changed:
        print(f"hooks already in {path}")
        return
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + f".tracegate-bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"hooks merged into {path} (backup saved next to it)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tool", nargs="?", choices=TARGETS)
    p.add_argument("--dest")
    p.add_argument("--always-on", action="store_true")
    p.add_argument("--no-hooks", action="store_true")
    a = p.parse_args()
    if not a.tool and not a.dest:
        p.error("give a tool name or --dest")
    skills, instr = (Path(a.dest), None) if a.dest else TARGETS[a.tool]
    out = skills / "tracegate"
    src = Path(__file__).resolve().parent
    if out.is_symlink():
        sys.exit(f"refusing to replace {out}: it is a symlink. Remove it yourself and re-run.")
    if out.exists():
        # only ever delete the tracegate folder the installer itself manages
        if out.name != "tracegate" or not (out / "SKILL.md").exists():
            sys.exit(f"refusing to replace {out}: does not look like a tracegate install. Move it aside yourself.")
        shutil.rmtree(out)
    skills.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__", "install.py"))
    print(f"installed to {out}")
    if a.tool and not a.no_hooks:
        path, top_key, entries = build_hooks(a.tool, out)
        if entries:
            merge_hooks(path, top_key, entries)
            if a.tool == "codex":
                print("note: Codex will ask you to trust these hooks on first run "
                      "(/hooks menu); they do not run until trusted.")
        else:
            print("note: OpenCode has no shell-command hooks (only JavaScript plugins); "
                  "skill installed without hooks.")
    if a.always_on and instr:
        instr.parent.mkdir(parents=True, exist_ok=True)
        cur = instr.read_text(encoding="utf-8") if instr.exists() else ""
        if MARK not in cur:
            instr.write_text(cur + ("\n" if cur and not cur.endswith("\n") else "") + LINE.format(path=out),
                             encoding="utf-8")
            print(f"added always-on line to {instr}")
        else:
            print(f"always-on line already in {instr}")


if __name__ == "__main__":
    main()
