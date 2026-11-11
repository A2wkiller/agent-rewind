# TraceGate

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![tests](https://github.com/A2wkiller/ai-token-firewall/actions/workflows/tests.yml/badge.svg)](https://github.com/A2wkiller/ai-token-firewall/actions/workflows/tests.yml)

A plug-and-play skill for AI coding tools (Claude Code, Gemini CLI, Antigravity, OpenCode, Codex). It gives your session **real backtracking**: the AI logs what it does, snapshots your project at checkpoints, and can **rewind the project to a checkpoint and continue** when something goes wrong. It also gives you a rough USD cost estimate at the start of the session.

No API keys. No `.env`. No provider API calls. No network. Python 3 standard library only.

![TraceGate session flow](docs/images/flow.png)

## What it does

1. **First message of a session:** the AI reads the skill, tells you backtracking has started, and gives a rough cost estimate. The estimate uses the model's real price per 1M input and output tokens from [`pricing.json`](skills/tracegate/pricing.json) (actual provider prices, checked 2026-11-11, sources inside). Raw USD, no currency conversion.
2. **During the session:** the AI logs steps (thoughts, tool calls, edits) and stores git snapshots as checkpoints. Checkpoints never commit on your branch or touch your staging area.
3. **"Go back to before X":** the AI runs `rewind`. The script takes a safety snapshot of the current state, restores the project to the checkpoint, and the session continues from there.
4. **"Tell me about my backtracking":** the AI prints the report: steps, files touched, checkpoints, rewinds, and logged token cost.

Session logs are stored in `~/.tracegate/sessions/`, outside your project.

## Real runs

These are captures of the actual script running (a demo project, a real git repo, TraceGate v0.1). No mock-ups.

**Session start with cost estimate:**

![tracegate start](docs/images/start.png)

**Checkpoint, then rewind after the AI went wrong:**

![tracegate rewind](docs/images/rewind.png)

**End-of-session report ("tell me about my backtracking"):**

![tracegate report](docs/images/report.png)

## Install

```bash
git clone https://github.com/A2wkiller/ai-token-firewall.git
cd ai-token-firewall
python3 skills/tracegate/install.py claude-code --always-on
# also: codex | gemini-cli | antigravity | opencode | --dest <folder>
```

`--always-on` adds one line to the tool's global instruction file telling it to read the skill at the first message. Without it, the tool decides whether to load the skill from its description.

Prices: `skills/tracegate/pricing.json` ships real provider prices checked 2026-11-11 with their sources. Prices change (two Gemini entries are promos that expire 2026-11-30 / 2026-12-31), so verify against the linked provider pages if your estimate matters.

## Commands

| Command | What it does |
| --- | --- |
| `start` | Open a session, print the banner and USD estimate |
| `log <thought\|tool\|edit\|note>` | Record one step, optional `--files`, `--tok-in/--tok-out` |
| `checkpoint "<label>"` | Git snapshot under `refs/tracegate/`, branch and staging untouched |
| `rewind <n\|label> [--yes]` | Restore project files to a checkpoint (safety snapshot first), continue the session. Without `--yes` it only prints the plan |
| `cost [--budget USD]` | Logged token cost so far; exit 1 when over budget ("ask the user whether to continue") |
| `end --summary "..."` | Close the session, print the report |
| `report` | Print the report again (default: latest session) |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

17 real tests, standard library only: estimate math, pricing lookup, checkpoint isolation from your staging area, rewind restore and safety snapshot, budget exit codes, installer behavior. They run the actual scripts in temp directories with real git repos. CI runs them on Python 3.11 and 3.13.

## Honest status

What was actually run (Linux, Python 3, real git repos):

- the full test suite above, green
- the installer for all five targets, including `--always-on` and re-running it
- a scripted demo session (the captures above): start, log, checkpoint, a real rewind that restored a file, cost check, end report

What was NOT tested, and you should check on your real tools:

- Whether each tool picks up the skill from the folder the installer uses. Those folders are best guesses and not verified: `~/.claude/skills`, `~/.codex/skills`, `~/.gemini/skills`, `~/.gemini/antigravity/skills`, `~/.config/opencode/skills`. If one is wrong, use `--dest` or paste the SKILL.md into that tool's instruction file.
- Whether the AI actually follows the skill on the first message. A skill is instructions, not a hook, so it depends on the model. `--always-on` makes this more likely, not certain.
- Token counts come from the AI itself; they are rough. The script does not measure real usage.
- Rewind restores tracked files. Files created after the checkpoint are left in place, and git-ignored files (like `.env`) are not part of snapshots.

## Security

See [SECURITY.md](SECURITY.md): how to report a vulnerability, what the script does on your machine, and the one rule that matters - never log secrets.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

## License

MIT - see [LICENSE](LICENSE).
