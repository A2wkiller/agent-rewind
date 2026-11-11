---
name: tracegate
description: Session backtracking and cost estimate. Use at the very first user message of every session, and whenever the user asks about their backtracking, session report, or cost.
---

# TraceGate

You keep a backtracking log of this session so the user can review it later. No API keys, no network, no .env. You run one small local script.

Script path: `scripts/tracegate.py` next to this file (e.g. `~/.claude/skills/tracegate/scripts/tracegate.py`). Run it with `python3` on macOS/Linux, `python` on Windows.

## 0. If a hook already started the session

On Claude Code, Codex, Gemini CLI and Antigravity, the installer wires a real SessionStart hook: the session starts by itself and you see the "TraceGate backtracking" banner in your context. In that case skip step 1 and just tell the user "TraceGate backtracking has started" with the estimate line. Hooks also auto-log file edits and auto-checkpoint before risky shell commands on those tools; you still do steps 2-4 for the things hooks cannot judge.

## 1. First message of the session (no hook, or OpenCode)

Do this before anything else, once per session:

1. Work out your model name and its price in USD per 1M input and output tokens (check `pricing.json` next to this file, or use the provider's published price). Use raw USD. Never convert to INR or any other currency. If you do not know the price, say so and ask the user.
2. Roughly estimate input and output tokens for the task the user asked for.
3. Run:
   `python3 scripts/tracegate.py start --model "<model>" --tool "<claude-code|gemini-cli|antigravity|opencode|codex>" --task "<one line>" --est-in <n> --est-out <n> --price-in <usd> --price-out <usd>`
   Sessions are per project: `start` resumes this project's open session instead of fragmenting into a new one.
4. Tell the user, in your reply: "TraceGate backtracking has started", plus the estimate line the script printed. Say it is a rough estimate.
5. Then do the user's task.

## 2. During the session

- After each meaningful step: `python3 scripts/tracegate.py log <thought|tool|edit|note> "<what and why>" --files <paths>` (hooks auto-log edits on supported tools; log the reasoning they can't see).
- Before a risky or large edit: `python3 scripts/tracegate.py checkpoint "<label>"`. Add `--full` when git-ignored files (like `.env` or build output) matter to the restore. Hooks auto-checkpoint before risky shell commands on supported tools; checkpoint yourself before risky non-shell steps.
- Keep entries short. Log honestly, including mistakes and reversals. That is what backtracking means here.
- Never log secrets: no API keys, passwords, tokens, or private file contents. Log what you did, not the data.
- If the user gave a cost limit, check with `python3 scripts/tracegate.py cost --budget <usd>` after big steps. When it says OVER BUDGET, stop and ask the user whether to continue.

## 3. Going back: rewind, branch and continue

When the user wants to undo part of the session ("go back to before X", "undo the last change"):

1. Confirm with the user first, then run `python3 scripts/tracegate.py rewind <n|label> --yes` (use the checkpoint number or label from the log). Add `--delete-new` only when the user explicitly wants files created after the checkpoint deleted.
2. The script takes a safety snapshot of the current state, restores the project files to the checkpoint, and keeps the session going. Your branch and commits are not touched.
3. Tell the user what was restored and what was left in place, then continue the task from there.

To try another approach without losing the current one: `python3 scripts/tracegate.py branch "<name>"` keeps the current state under a name; `python3 scripts/tracegate.py switch <name> --yes` comes back to it later.

## 4. When the user asks "tell me about my backtracking" (or the session ends)

Run `python3 scripts/tracegate.py end --summary "<one line>"` and show the user the report it prints: steps, files touched, checkpoints with their cost, branches, rewinds, and token cost. Add a short plain-language summary: what you tried, what you undid, what changed.

`python3 scripts/tracegate.py tokens` shows measured usage read from the tool's own transcript files when the tool exposes it (Claude Code and Codex only).

## 5. Cleanup

`python3 scripts/tracegate.py clean` prunes session logs older than 30 days and the `refs/tracegate/*` snapshots in the current repo. It prints the plan first and only deletes with `--yes`.

## Limits

- Hooks automate start, edit-logging, risky-command checkpoints and session end on Claude Code, Codex, Gemini CLI and Antigravity. Codex runs hooks only after the user trusts them (the /hooks menu). OpenCode has no shell hooks, so everything above is manual there.
- This skill only logs what you or the hooks tell it. It does not intercept tool calls beyond the hooks.
- Rewind restores tracked files to the checkpoint. Files created after the checkpoint are left in place unless the user confirms `--delete-new`. Git-ignored files (like `.env`) are only in `--full` snapshots.
