---
name: tracegate
description: Session backtracking and cost estimate. Use at the very first user message of every session, and whenever the user asks about their backtracking, session report, or cost.
---

# TraceGate

You keep a backtracking log of this session so the user can review it later. No API keys, no network, no .env. You run one small local script.

Script path: `scripts/tracegate.py` next to this file (e.g. `~/.claude/skills/tracegate/scripts/tracegate.py`). Use `python3`.

## 1. First message of the session

Do this before anything else, once per session:

1. Work out your model name and its price in USD per 1M input and output tokens (check `pricing.json` next to this file, or use the provider's published price). Use raw USD. Never convert to INR or any other currency. If you do not know the price, say so and ask the user.
2. Roughly estimate input and output tokens for the task the user asked for.
3. Run:
   `python3 scripts/tracegate.py start --model "<model>" --tool "<claude-code|gemini-cli|antigravity|opencode|codex>" --task "<one line>" --est-in <n> --est-out <n> --price-in <usd> --price-out <usd>`
4. Tell the user, in your reply: "TraceGate backtracking has started", plus the estimate line the script printed. Say it is a rough estimate.
5. Then do the user's task.

## 2. During the session

- After each meaningful step: `python3 scripts/tracegate.py log <thought|tool|edit|note> "<what and why>" --files <paths>`
- Before a risky or large edit: `python3 scripts/tracegate.py checkpoint "<label>"` (stores a git tree snapshot, no commit, does not touch the user's staging area).
- Keep entries short. Log honestly, including mistakes and reversals. That is what backtracking means here.

## 3. When the user asks "tell me about my backtracking" (or the session ends)

Run `python3 scripts/tracegate.py end --summary "<one line>"` and show the user the report it prints. Add a short plain-language summary: what you tried, what you undid, what changed.

## Limits

- This skill only logs what you tell it to. It does not intercept tool calls.
- Rewinding is manual: use the git commands printed in the report.
