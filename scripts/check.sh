#!/usr/bin/env bash
# The full local gate. CI runs this exact script — if it passes here it passes
# there, and vice versa.
#
# Every check runs independently and its exit code is captured. Nothing is
# chained with `&&`: a chain short-circuits on the first failure, which both
# hides the remaining failures and makes a partial run look like a pass if you
# read the wrong line of output. That mistake shipped a red build once already.
#
# A test skipped for a missing environment FAILS this gate — see conftest.py.
# The integration suites need Postgres, NATS with JetStream, and
# VictoriaMetrics; without them "all checks passed" means something narrower
# than it says, which is how a broken seed reached CI once already. To run
# anyway, and accept that those tests are not being checked:
#
#     BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh
#
# Usage:  ./scripts/check.sh            # everything
#         ./scripts/check.sh --quick    # skip the slow ones

set -uo pipefail
cd "$(dirname "$0")/.."

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

FAILED=()
run() {
    local name="$1"; shift
    printf '\033[1m▶ %s\033[0m\n' "$name"
    local out rc
    out=$("$@" 2>&1); rc=$?
    if [[ $rc -eq 0 ]]; then
        printf '  \033[32mPASS\033[0m  %s\n' "$name"
    else
        printf '  \033[31mFAIL\033[0m  %s (exit %d)\n' "$name" "$rc"
        printf '%s\n' "$out" | sed 's/^/    /'
        FAILED+=("$name")
    fi
}

run "ruff check"          uv run ruff check .
run "ruff format --check" uv run ruff format --check .
run "mypy --strict"       uv run mypy contracts/python db services
run "pytest"              uv run pytest

# Renders migrations to SQL with no database. Catches a broken revision without
# needing Postgres; the schema-vs-migration drift check is a separate,
# Postgres-backed test.
alembic_offline() {
    ( cd db && BELLASREEF_DATABASE_URL="postgresql+asyncpg://offline/offline" \
        uv run alembic upgrade head --sql )
}
[[ $QUICK -eq 0 ]] && run "alembic offline render" alembic_offline

echo
if [[ ${#FAILED[@]} -eq 0 ]]; then
    printf '\033[32m✓ all checks passed\033[0m\n'
    exit 0
fi
printf '\033[31m✗ %d check(s) failed:\033[0m %s\n' "${#FAILED[@]}" "${FAILED[*]}"
exit 1
