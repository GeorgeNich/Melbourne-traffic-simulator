#!/usr/bin/env bash
# Local CI: every lint and test. Must pass before any merge; the pre-commit hook runs it.
# Usage: scripts/check.sh
set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$1"; }

step "Python: ruff lint"
uv run ruff check .

step "Python: ruff format"
uv run ruff format --check .

step "Python: pytest"
uv run pytest -q

# Git hooks and GUI apps don't load the shell profile, so load nvm explicitly and use .nvmrc.
# Without this, Homebrew's Node 25 would be picked up instead of Node 22.
step "Node: nvm use (.nvmrc)"
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
  echo "nvm not found at $NVM_DIR; install nvm or set NVM_DIR" >&2
  exit 1
fi
# nvm.sh is not written for `set -u`.
set +u
# shellcheck source=/dev/null
. "$NVM_DIR/nvm.sh" --no-use
nvm use >/dev/null
set -u
echo "node $(node -v), pnpm $(pnpm --version)"

step "Web: eslint"
pnpm -C web lint

step "Web: typecheck"
pnpm -C web typecheck

step "Web: vitest"
pnpm -C web test

printf '\n\033[1;32mAll checks passed.\033[0m\n'
