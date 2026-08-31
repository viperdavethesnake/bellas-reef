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
cd "$(dirname "$0")/.." || exit 1

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

# Shell is gated like Python is. install-hub.sh runs on a stranger's hardware
# as the first thing this project ever does for them, and an unquoted variable
# there is not a style opinion.
#
# Not skipped when shellcheck is absent: a check that silently did not run is
# the same failure conftest.py exists to prevent, one directory over.
run "shellcheck" shellcheck scripts/*.sh hub/scripts/*.sh
run "mypy --strict"       uv run mypy contracts/python db services
run "pytest"              uv run pytest

# The published spec is the source of truth for every client (CLAUDE.md,
# "Versioned contracts"), and it is generated from the app rather than kept by
# hand. That only holds if the generated document and the committed one are the
# same document.
#
# They were not. CI regenerated the spec and uploaded it as a build artifact,
# and never once compared it to the copy in the tree — so `openapi.json` sat at
# info.version 3.0.0 while the app derived 3.3.0, with no /capabilities and no
# POST /devices. The iOS client generates from the committed file, so it was
# built, faithfully, against a contract three minor versions out of date and had
# no bindDevice method to call: the whole device-adoption flow missing from the
# artifact this project calls the source of truth, with every downstream check
# green. A generated client makes contract drift a compile error only when the
# spec it generates from is the spec the server serves. This is the check that
# makes that true.
#
# Never behind --quick. Drift is silent by nature; a check you can skip on the
# way out the door is the check that was not running when it mattered.
# Invoked indirectly as `run "..." spec_drift` below; shellcheck's
# reachability analysis for SC2329 does not trace a function name passed as a
# bare argument, and (confirmed in isolation) loses track of it entirely once
# the script's trailing top-level `exit` is added. Older shellcheck (0.9,
# what ubuntu-latest ships) reports the same thing per line as SC2317
# instead of per function as SC2329, so both codes are named.
# shellcheck disable=SC2329,SC2317
spec_drift() {
    local tmp rc=0 file
    tmp="$(mktemp -d)" || return 1
    if ! uv run python scripts/export-openapi.py \
            --out "$tmp/openapi.json" \
            --frames-out "$tmp/stream-frames.schema.json"; then
        echo "the exporter failed, so the committed spec could not be checked at all"
        rm -rf "$tmp"
        return 1
    fi
    for file in openapi.json stream-frames.schema.json; do
        if ! diff -u "$file" "$tmp/$file"; then
            echo "^^^ ${file} is stale"
            rc=1
        fi
    done
    rm -rf "$tmp"
    if [[ $rc -ne 0 ]]; then
        cat <<'DRIFT'

The committed contract is not what the app generates. Clients — including the
generated Swift one — are built from the committed file, so whatever the server
grew since it was last exported does not exist for them. Regenerate and commit
the result:

    uv run python scripts/export-openapi.py
DRIFT
    fi
    return "$rc"
}
run "openapi spec drift" spec_drift

# Step 1 of every client's journey is a Bonjour browse, and the TXT record
# carries the contracts version a client uses to decide whether it can talk to
# this hub at all. That value was hardcoded, three minor versions stale, derived
# from nothing and tested by nothing — the same failure as the spec above, in a
# file nobody thinks of as a contract.
#
# install-hub.sh installs this file verbatim, so the committed value is what
# every hub advertises. This check keeps it equal to the installed package.
# Invoked indirectly as `run "..." avahi_contracts` below; see the note on
# spec_drift above.
# shellcheck disable=SC2329,SC2317
avahi_contracts() {
    local record declared expected
    record="hub/deploy/avahi/bellasreef.service"
    declared="$(sed -n 's|.*<txt-record>contracts=\([^<]*\)</txt-record>.*|\1|p' "$record")"
    expected="$(uv run python -c 'from importlib.metadata import version
print(version("bellasreef-contracts"))')" || return 1
    if [[ -z "$declared" ]]; then
        echo "${record} advertises no contracts= TXT record; clients cannot tell what this hub speaks"
        return 1
    fi
    if [[ "$declared" != "$expected" ]]; then
        echo "${record} advertises contracts=${declared}; bellasreef-contracts is ${expected}"
        echo "A client that refuses a hub it is too old to talk to is reading this number."
        return 1
    fi
    echo "contracts=${declared}"
    return 0
}
run "avahi contracts version" avahi_contracts

# Renders migrations to SQL with no database. Catches a broken revision without
# needing Postgres; the schema-vs-migration drift check is a separate,
# Postgres-backed test.
# Invoked indirectly as `run "..." alembic_offline` below (guarded by
# --quick); see the note on spec_drift above.
# shellcheck disable=SC2329,SC2317
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
