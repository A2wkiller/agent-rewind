#!/usr/bin/env python3
"""Copy the tracegate skill into your agent tool's skills folder.

  python3 install.py claude-code            # ~/.claude/skills/tracegate
  python3 install.py codex                  # ~/.codex/skills/tracegate
  python3 install.py gemini-cli             # ~/.gemini/skills/tracegate
  python3 install.py antigravity            # ~/.gemini/antigravity/skills/tracegate
  python3 install.py opencode               # ~/.config/opencode/skills/tracegate
  python3 install.py --dest /some/dir       # any folder
Add --always-on to also append a one-line instruction to the tool's global
instruction file so the skill is read on the first message every session.
"""
import argparse, shutil
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tool", nargs="?", choices=TARGETS)
    p.add_argument("--dest")
    p.add_argument("--always-on", action="store_true")
    a = p.parse_args()
    if not a.tool and not a.dest:
        p.error("give a tool name or --dest")
    skills, instr = (Path(a.dest), None) if a.dest else TARGETS[a.tool]
    out = skills / "tracegate"
    src = Path(__file__).resolve().parent
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns("__pycache__", "install.py"))
    print(f"installed to {out}")
    if a.always_on and instr:
        instr.parent.mkdir(parents=True, exist_ok=True)
        cur = instr.read_text() if instr.exists() else ""
        if MARK not in cur:
            instr.write_text(cur + ("\n" if cur and not cur.endswith("\n") else "") + LINE.format(path=out))
            print(f"added always-on line to {instr}")
        else:
            print(f"always-on line already in {instr}")


if __name__ == "__main__":
    main()
