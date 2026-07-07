#!/usr/bin/env bash
# EVA one-line bootstrap installer (Linux / macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/arturkorb3/eva-evolutional-agent/main/install.sh | bash
#
# What it does (and nothing more):
#   1. checks for git + docker
#   2. clones the repo to $EVA_HOME (default: ~/eva) - or pulls if already there
#   3. hands over to the repo's own `./run.sh install` (PATH shim, tab-completion,
#      optional image build - all reversible via `eva uninstall`)
set -euo pipefail

REPO_URL="https://github.com/arturkorb3/eva-evolutional-agent.git"
DEST="${EVA_HOME:-$HOME/eva}"

say()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is required. Install it first (e.g. apt install git)."
if ! command -v docker >/dev/null 2>&1; then
  say "warning: docker not found - EVA runs sandboxed in Docker."
  say "         Install Docker (https://docs.docker.com/get-docker/) before first use."
fi

if [ -d "$DEST/.git" ]; then
  say "Existing checkout at $DEST - updating (git pull)..."
  git -C "$DEST" pull --ff-only || fail "git pull failed - resolve manually in $DEST."
elif [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
  fail "$DEST exists and is not an EVA checkout. Set EVA_HOME to another directory."
else
  say "Cloning EVA into $DEST ..."
  git clone --depth 1 "$REPO_URL" "$DEST"
fi

cd "$DEST"
chmod +x run.sh bin/eva 2>/dev/null || true
./run.sh install

say ""
say "EVA is installed at: $DEST"
say "Next: open a new terminal and run   eva"
