# TraceGate

A plug-and-play skill for AI coding tools (Claude Code, Gemini CLI, Antigravity, OpenCode, Codex). It makes the AI keep a backtracking log of your session and give you a USD cost estimate at the start.

No API keys. No `.env`. No provider API calls. No network. Python 3 standard library only.

## What it does

1. **First message of a session:** the AI reads the skill, tells you backtracking has started, and gives a rough cost estimate. The estimate uses the model's price per 1M input and output tokens in raw USD (for example $2 in / $10 out). No currency conversion.
2. **During the session:** the AI logs steps (thoughts, tool calls, edits) and can store git tree snapshots as checkpoints. Checkpoints do not commit or touch your staging area.
3. **End of session:** ask the AI "tell me about my backtracking". It prints the report: steps, files touched, checkpoints, logged token cost.

Session logs are stored in `~/.tracegate/sessions/`, outside your project.

## Install

```bash
git clone https://github.com/A2wkiller/ai-token-firewall.git
cd ai-token-firewall
python3 skills/tracegate/install.py claude-code --always-on
# also: codex | gemini-cli | antigravity | opencode | --dest <folder>
```

`--always-on` adds one line to the tool's global instruction file telling it to read the skill at the first message. Without it, the tool decides whether to load the skill from its description.

Add real model prices to `skills/tracegate/pricing.json` (USD per 1M tokens). The shipped entry is a made-up example.

## Honest status

What was actually run (Linux sandbox, Python 3, fake HOME):
- the installer for all five targets, including `--always-on` and re-running it
- `tracegate.py` start / log / checkpoint / end, including the USD estimate math and the git checkpoint (project staging area unchanged)

What was NOT tested, and you must check on your real tools:
- Whether each tool picks up the skill from the folder the installer uses. Those folders are my best guess for each tool and are not verified: `~/.claude/skills`, `~/.codex/skills`, `~/.gemini/skills`, `~/.gemini/antigravity/skills`, `~/.config/opencode/skills`. If one is wrong, use `--dest` or paste the SKILL.md into that tool's instruction file.
- Whether the AI actually follows the skill on the first message. A skill is instructions, not a hook, so it depends on the model. `--always-on` makes this more likely, not certain.
- Token counts and estimates come from the AI itself. They are rough. The script does not measure real usage.
- Rewind is manual (git commands printed in the report). There is no automatic restore.

There are no automated tests. The earlier test suite, the API-key proxy code, Docker and PyPI release files were removed. All of it is in git history at commit `765d0e1`.

## License

MIT - see [LICENSE](LICENSE).
