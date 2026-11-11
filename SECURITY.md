# Security Policy

## Supported versions

TraceGate is pre-1.0. Only the latest commit on `main` gets security fixes.

## Reporting a vulnerability

Do not open a public issue for security problems. Report them privately through
GitHub's "Report a vulnerability" button on the Security tab (private
vulnerability reporting), or contact the maintainer through their GitHub
profile. You will get an acknowledgement within a few days.

## What TraceGate does on your machine (threat model)

- Runs one Python 3 script, standard library only. No network calls, no API
  keys, no `.env`, no third-party packages.
- Writes session logs to `~/.tracegate/sessions/` (directory mode `0700`,
  files mode `0600`). Logs can contain task descriptions and step notes, so
  they are readable only by your user.
- `checkpoint` runs `git add -A` and `git write-tree` against a temporary
  index file (`GIT_INDEX_FILE` inside `.git`), never your real index. The
  temp file is removed even on failure. The snapshot is stored as a dangling
  commit under `refs/tracegate/`, which never touches your branches, HEAD, or
  staging area. Git ignores apply, so ignored files such as `.env` are not
  captured unless you pass `--full` on purpose.
- `rewind` overwrites tracked files (worktree + index) from a checkpoint. It
  always takes a safety snapshot first, never rewrites history, and deletes
  files created after the checkpoint only when you pass `--delete-new --yes`.
  Without `--yes` it only prints the plan. `switch` works the same way for
  named `branch` snapshots.
- `clean` deletes session logs older than the given age and `refs/tracegate/*`
  refs in the current repo. It prints the full plan first and deletes only
  with `--yes`.
- `install.py` copies the skill folder, merges TraceGate hook entries into the
  tool's own hook config (`~/.claude/settings.json`, `~/.codex/hooks.json`,
  `~/.gemini/settings.json`, `~/.gemini/config/hooks.json`), and optionally
  appends one marked line (`<!-- tracegate -->`) to the tool's global
  instruction file. Existing config is kept, the file is backed up next to the
  original before the first change, invalid JSON is refused, and it refuses
  to replace a symlink or a folder that is not a previous TraceGate install.
- The installed hooks run `python3 .../tracegate.py hook <event>` when the
  tool fires them: they read the tool's JSON payload on stdin, append to the
  session log, and can snapshot the project (the pre-tool hook does this
  before risky shell commands). Hooks never block, deny, or rewrite your
  agent's actions; the worst a hook failure does is print a warning in the
  tool. Hooks execute with your user privileges, like any tool hook -
  review the entries the installer prints.

## What you must do

- Never log secrets. Session logs are plain JSONL: do not put API keys,
  passwords, tokens, or private file contents into `log` messages or task
  descriptions. The skill instructs the AI not to, but the AI chooses what it
  logs.
- The AI chooses when to run the script. A skill is instructions, not a
  sandbox: review what your agent runs, as always.
- Checkpoint refs live in your local clone. `git push` does not send
  `refs/tracegate/*` by default; do not push them to a shared remote if the
  snapshots may contain sensitive working-tree files.
