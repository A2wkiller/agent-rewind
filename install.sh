#!/bin/sh
# TraceGate one-line install:
#   curl -fsSL https://raw.githubusercontent.com/A2wkiller/ai-token-firewall/main/install.sh | sh
# or after a clone:  sh install.sh claude-code
#
# Clones (or updates) the repo into ~/.tracegate/ai-token-firewall, then runs
# the skill installer. Extra args are passed to install.py, e.g.:
#   sh install.sh claude-code --always-on
# Env overrides: TRACEGATE_SRC (where the repo lives), TRACEGATE_REPO_URL.
set -e

SRC="${TRACEGATE_SRC:-$HOME/.tracegate/ai-token-firewall}"
REPO="${TRACEGATE_REPO_URL:-https://github.com/A2wkiller/ai-token-firewall.git}"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2; exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2; exit 1
fi

if [ -d "$SRC/.git" ]; then
  echo "updating $SRC"
  git -C "$SRC" pull --ff-only
else
  echo "cloning $REPO -> $SRC"
  mkdir -p "$(dirname "$SRC")"
  git clone "$REPO" "$SRC"
fi

python3 "$SRC/skills/tracegate/install.py" "$@"
