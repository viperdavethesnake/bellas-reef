#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
#
# Assemble the bellasreef-hub payload: hub/ plus a release manifest.
#
# This is the only thing that ever produces the user-facing repo. The release
# workflow calls it on a v* tag and pushes the result; a developer can call it
# to see exactly what a user gets. Nobody edits bellasreef-hub by hand.
#
# Usage: scripts/build-hub-repo.sh <outdir> <version vX.Y.Z[-pre]> <40-hex sha>
set -euo pipefail

usage() {
    echo "usage: $0 <outdir> <version vX.Y.Z[-pre]> <40-hex sha>" >&2
    exit 2
}
[[ $# -eq 3 ]] || usage
out="$1"; version="$2"; sha="$3"
[[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || { echo "build-hub-repo: bad version '${version}'" >&2; exit 2; }
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "build-hub-repo: bad sha '${sha}'" >&2; exit 2; }

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -d "${repo}/hub" ]] || { echo "build-hub-repo: ${repo}/hub is missing" >&2; exit 1; }
if [[ -e "$out" ]] && [[ -n "$(ls -A "$out")" ]]; then
    echo "build-hub-repo: ${out} is not empty; refusing to assemble over it" >&2
    exit 1
fi

# The contracts version comes from the avahi record, which scripts/check.sh
# gates against the installed bellasreef-contracts package — so this needs no
# Python and cannot disagree with what the hub will advertise.
contracts="$(sed -n 's|.*<txt-record>contracts=\([^<]*\)</txt-record>.*|\1|p' \
    "${repo}/hub/deploy/avahi/bellasreef.service")"
[[ -n "$contracts" ]] || { echo "build-hub-repo: no contracts= TXT record in the avahi service file" >&2; exit 1; }

mkdir -p "$out"
cp -R "${repo}/hub/." "${out}/"
cp "${repo}/LICENSE" "${out}/LICENSE"
find "$out" -name '.DS_Store' -delete

# A developer's own hub/deploy/.env (or a stray hub/deploy/.env.local) must
# never ship: `cp -R` copies whatever is sitting in the dev tree, gitignored
# or not, so this is the one place that guarantees a secrets file never
# leaves the assembler. .env.example is the committed template and stays.
find "${out}/deploy" -maxdepth 1 \( -name '.env' -o -name '.env.*' \) ! -name '.env.example' -delete

printf 'BELLASREEF_VERSION=%s\nBELLASREEF_TAG=%s\nBELLASREEF_CONTRACTS=%s\n' \
    "$version" "$sha" "$contracts" > "${out}/deploy/release.env"

echo "assembled ${version} (${sha:0:12}) into ${out}"
