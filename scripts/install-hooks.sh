#!/usr/bin/env bash
# Point git at the committed hooks directory.
#
# `core.hooksPath` is local config and cannot itself be committed, so the hooks
# live in .githooks/ (tracked) and this script wires them up. One command per
# clone, and the hook contents stay under review like any other code.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "hooks installed: core.hooksPath -> $(git config core.hooksPath)"
echo "active hooks:"
for h in .githooks/*; do
    [[ -f "$h" ]] && printf '  %-12s %s\n' "$(basename "$h")" "$([[ -x "$h" ]] && echo executable || echo 'NOT EXECUTABLE')"
done
