# Hub Repo Split and Install Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `bellasreef-hub` (generated from `hub/` on each release tag) the only thing a user clones, and make `install-hub.sh` finish on a stock Raspberry Pi OS image with no hand edits; replace the workstation-side deploy/reset tools with on-Pi scripts.

**Architecture:** The dev repo gains a `hub/` directory that *is* the user-facing repo verbatim (compose, boot unit, avahi record, three lifecycle scripts, operator docs). `scripts/build-hub-repo.sh` copies it and writes `deploy/release.env` (version + image SHA); `release.yaml` runs that on every `v*` tag, retags the CI images, and pushes to `bellasreef-hub`. The installer reads its image tag from `release.env` instead of `git rev-parse`, offers the two remaining host remediations (avahi allow-interfaces, docker log rotation), reports hardware as an inventory, and creates the bind-mount directories it needs.

**Tech Stack:** bash (shellcheck-clean), pytest black-box subprocess tests with stub executables (`tests/test_install_hub.py` harness), GitHub Actions, docker buildx imagetools.

**Spec:** `docs/superpowers/specs/2026-08-30-hub-repo-split-and-install-design.md`

## Global Constraints

- Every script under `hub/scripts/` runs **on the hub, from the clone**; no ssh, no host variable, no workstation assumptions.
- Every filesystem read in `install-hub.sh` is prefixed with `$IH_ROOT`; tracked repo files (`compose.yaml`, `.env.example`) are read bare. Test-only seams are `IH_*` environment variables, undocumented in `--help`.
- Nothing hardware-side gates the install (phase 3 is an inventory). Nothing is written to `/boot/firmware/config.txt`.
- The GHCR 401 branch in phase 5 stays (packages private until the project is done).
- `IH_MIN_MEM_KB` stays 2 000 000 (flagged for measurement on coco; not this plan).
- Conventional commits; PRs, never direct pushes to main; CI green before the next PR starts.
- Container uid stays `1000:1000`; directories the containers write to are created owned `1000:1000` by the installer.
- All four PRs land before any `v*` tag is pushed (a tag runs `release.yaml`).

## PR boundaries

| PR | Tasks | Branch |
|---|---|---|
| 1 structure | 1–5 | `feat/hub-payload` |
| 2 installer | 6–11 | `feat/install-hub-inventory-and-remediation` |
| 3 lifecycle scripts | 12–14 | `feat/on-pi-lifecycle-scripts` |
| 4 docs + release | 15–17 | `docs/hub-repo-and-deploy-discipline` |

Each PR: `./scripts/check.sh` green locally (`BELLASREEF_ALLOW_ENV_SKIPS=1` on this Mac — no container runtime), push, `gh pr create`, wait for CI, merge with `gh pr merge --squash --delete-branch`.

---

## PR 1 — structure

### Task 1: Move the hub payload into `hub/`

**Files:**
- Move (git mv): `deploy/compose.yaml` → `hub/deploy/compose.yaml`; `deploy/.env.example` → `hub/deploy/.env.example`; `deploy/systemd/bellasreef.service` → `hub/deploy/systemd/bellasreef.service`; `deploy/avahi/bellasreef.service` → `hub/deploy/avahi/bellasreef.service`; `deploy/config/devices.yaml.example` → `hub/deploy/config/devices.yaml.example`; `scripts/install-hub.sh` → `hub/scripts/install-hub.sh`; `docs/host-setup.md`, `docs/hub-platform-requirements.md`, `docs/backup-restore.md` → `hub/docs/`.
- Modify: `tests/test_install_hub.py:27-28, 1330-1340`; `scripts/check.sh:51, 126`; `.github/workflows/ci.yaml:248-280` (compose job); `scripts/drill-restart.sh:31-32`; `scripts/factory-reset-pi.sh:64-67`; `scripts/deploy-pi.sh:47-50`; `hub/deploy/systemd/bellasreef.service` (comment only, if it names `deploy/`).

**Interfaces:**
- Produces: `HUB_ROOT = REPO_ROOT / "hub"` in the test module; `hub/scripts/install-hub.sh`'s `REPO_DIR` now resolves to `<clone>/hub` in the dev repo and `<clone>` in the hub repo — both correct because every path in the script is `${REPO_DIR}/deploy/...`.

- [ ] **Step 1: Create the branch and move the files**

```bash
git checkout -b feat/hub-payload
mkdir -p hub/deploy hub/scripts hub/docs
git mv deploy/compose.yaml hub/deploy/compose.yaml
git mv deploy/.env.example hub/deploy/.env.example
git mv deploy/systemd hub/deploy/systemd
git mv deploy/avahi hub/deploy/avahi
git mv deploy/config hub/deploy/config
git mv scripts/install-hub.sh hub/scripts/install-hub.sh
git mv docs/host-setup.md docs/hub-platform-requirements.md docs/backup-restore.md hub/docs/
git status --short | head -20
```

- [ ] **Step 2: Re-point the test suite**

In `tests/test_install_hub.py`:

```python
REPO_ROOT = Path(__file__).resolve().parents[1]
HUB_ROOT = REPO_ROOT / "hub"
SCRIPT = HUB_ROOT / "scripts" / "install-hub.sh"
```

and in `staged_env_path`:

```python
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
```

Search the file for the literal `REPO_ROOT / "deploy"` (the "refuses to run against a real deploy/.env" guards) and replace each with `HUB_ROOT / "deploy"`.

- [ ] **Step 3: Re-point the gate, CI, and the bench scripts**

`scripts/check.sh`:
```bash
run "shellcheck" shellcheck scripts/*.sh hub/scripts/*.sh
```
and in `avahi_contracts`: `record="hub/deploy/avahi/bellasreef.service"`.

`.github/workflows/ci.yaml` compose job: `working-directory: hub/deploy`.

`scripts/drill-restart.sh:31-32`:
```bash
COMPOSE_BASE="docker compose -f hub/deploy/compose.yaml --env-file hub/deploy/.env"
COMPOSE_ARMED="docker compose -f hub/deploy/compose.yaml -f deploy/compose.drill.yaml --env-file hub/deploy/.env"
```

`scripts/deploy-pi.sh:48` and `scripts/factory-reset-pi.sh:65`: `DEPLOY_DIR="${PI_DIR}/hub/deploy"` (both files are deleted in Task 14; this keeps them coherent until then).

`.gitignore`: `.env` already ignores `hub/deploy/.env`; add a comment line `# hub/deploy/.env — the per-host secrets install-hub.sh writes` under the existing `.env` entry.

- [ ] **Step 4: Run the suite and the gate**

Run: `uv run pytest tests/test_install_hub.py -q`
Expected: 81 passed.

Run: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh --quick`
Expected: every line PASS (shellcheck now covers `hub/scripts/install-hub.sh`).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(hub): move the hub payload into hub/ — compose, boot unit, avahi record, installer, operator docs"
```

### Task 2: compose.yaml without `build:`; the two bind-mount path variables

**Files:**
- Modify: `hub/deploy/compose.yaml` (three `build:` blocks; two `volumes:` lines under `api`), `hub/deploy/.env.example`, `hub/scripts/install-hub.sh` (phase 4 sed + key loop), `.github/workflows/ci.yaml` (compose job sed), `CONTRIBUTING.md`.
- Test: `tests/test_install_hub.py`

**Interfaces:**
- Produces: `.env` keys `BELLASREEF_BACKUP_DIR` and `BELLASREEF_ETC_DIR`, consumed by compose (`${…:?}`), by Task 10 (directory creation) and Task 14 (backup path).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install_hub.py`, after `test_phase4_writes_a_complete_locked_down_env`:

```python
def test_phase4_writes_the_two_directory_keys(tmp_path: Path) -> None:
    # compose.yaml bind-mounts the backup directory and /etc/bellasreef by
    # variable now, both interpolated as ${VAR:?} — an .env without them
    # refuses to start the whole stack with an error naming the variable.
    # The backup dir is the operating user's, not a literal /home/david.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script("--yes", root=root, stubs=stubs, env={**FAST_POLL, "HOME": "/home/tester"})
    assert result.returncode == 0, result.stdout + result.stderr
    text = envfile.read_text()
    assert "BELLASREEF_BACKUP_DIR=/home/tester/backups" in text, text
    assert "BELLASREEF_ETC_DIR=/etc/bellasreef" in text, text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_install_hub.py::test_phase4_writes_the_two_directory_keys -q`
Expected: FAIL — `BELLASREEF_BACKUP_DIR` not in the generated file.

- [ ] **Step 3: compose.yaml**

Delete the three blocks (under `hardware-io`, `control-engine`, `api`):
```yaml
    build:
      context: ..
      dockerfile: deploy/Dockerfile.<svc>
```
and the comment at lines 17-19 that explains `image:` beside `build:`. Under `api:` → `volumes:`:
```yaml
      - ${BELLASREEF_BACKUP_DIR:?}:/backups
      - ${BELLASREEF_ETC_DIR:?}:/etc/bellasreef:ro
```

- [ ] **Step 4: .env.example**

Append:
```
# ---- Host paths (api bind mounts) ----
# Written by scripts/install-hub.sh, which also creates both directories
# owned by the container uid (1000): where `bellasreef backup` lands archives
# on the host, and where the device-import manifest lives. Leave empty.
BELLASREEF_BACKUP_DIR=
BELLASREEF_ETC_DIR=
```

- [ ] **Step 5: install-hub.sh phase 4**

In `ih_phase4_configure`, add two `-e` lines to the sed that renders the env:
```bash
        -e "s|^BELLASREEF_BACKUP_DIR=.*|BELLASREEF_BACKUP_DIR=${HOME}/backups|" \
        -e "s|^BELLASREEF_ETC_DIR=.*|BELLASREEF_ETC_DIR=/etc/bellasreef|" \
```
and extend the completeness loop:
```bash
    for key in POSTGRES_PASSWORD BELLASREEF_DATABASE_URL I2C_GID GPIO_GID BELLASREEF_TAG BELLASREEF_BACKUP_DIR BELLASREEF_ETC_DIR; do
```
Update the `ih_would` line in the same function to also print `BELLASREEF_BACKUP_DIR=${HOME}/backups`.

- [ ] **Step 6: CI compose job and CONTRIBUTING**

In `ci.yaml`'s compose job sed, add:
```bash
            -e "s|^BELLASREEF_BACKUP_DIR=.*|BELLASREEF_BACKUP_DIR=/tmp/bellasreef-backups|" \
            -e "s|^BELLASREEF_ETC_DIR=.*|BELLASREEF_ETC_DIR=/tmp/bellasreef-etc|" \
```
In `CONTRIBUTING.md`, under "Working on the code", add:
```markdown
Images are built by CI from `deploy/Dockerfile.*` (multi-arch, pushed to
ghcr.io on every merge to main). `hub/deploy/compose.yaml` carries no
`build:` blocks on purpose — a hub pulls, it never builds. For a local image:

    docker build -f deploy/Dockerfile.api -t bellasreef-api:dev .
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_install_hub.py -q`
Expected: 82 passed (`test_phase4_rejects_an_env_missing_a_substituted_key` still passes — it removes a key from a copy of the example; check it still targets a key in the loop).

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(hub): compose pulls only (no build blocks); backup and etc dirs become interpolated paths written by the installer"
```

### Task 3: `scripts/build-hub-repo.sh` — the one assembler

**Files:**
- Create: `scripts/build-hub-repo.sh`
- Test: `tests/test_build_hub_repo.py`

**Interfaces:**
- Produces: `build-hub-repo.sh <outdir> <version> <sha>` → `<outdir>` holds `hub/` + `LICENSE` + `deploy/release.env` (`BELLASREEF_VERSION`, `BELLASREEF_TAG`, `BELLASREEF_CONTRACTS`). Exit 2 on bad args, 1 on a non-empty outdir. Consumed by Task 4 and Task 17.

- [ ] **Step 1: Write the failing tests**

```python
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""scripts/build-hub-repo.sh assembles exactly the user-facing payload."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build-hub-repo.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"

# The whole contract in one set. A file added under hub/ shows up here or the
# test fails, which is the point: the payload is reviewed, not accumulated.
EXPECTED = {
    "LICENSE",
    "deploy/compose.yaml",
    "deploy/.env.example",
    "deploy/release.env",
    "deploy/systemd/bellasreef.service",
    "deploy/avahi/bellasreef.service",
    "deploy/config/devices.yaml.example",
    "scripts/install-hub.sh",
    "docs/host-setup.md",
    "docs/hub-platform-requirements.md",
    "docs/backup-restore.md",
}


def build(
    out: Path, version: str = "v0.2.0-rc.2", sha: str = SHA
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(out), version, sha], capture_output=True, text=True, check=False
    )


def relative_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_assembles_exactly_the_payload(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = build(out)
    assert result.returncode == 0, result.stderr
    assert relative_files(out) == EXPECTED


def test_release_env_pins_version_tag_and_contracts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert build(out).returncode == 0
    lines = (out / "deploy/release.env").read_text().splitlines()
    assert lines[0] == "BELLASREEF_VERSION=v0.2.0-rc.2"
    assert lines[1] == f"BELLASREEF_TAG={SHA}"
    # The contracts version is the one the avahi record advertises, which
    # check.sh already gates against the installed package.
    record = (REPO_ROOT / "hub/deploy/avahi/bellasreef.service").read_text()
    advertised = record.split("<txt-record>contracts=")[1].split("<")[0]
    assert lines[2] == f"BELLASREEF_CONTRACTS={advertised}"


def test_payload_compose_has_no_build_blocks(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert build(out).returncode == 0
    assert "build:" not in (out / "deploy/compose.yaml").read_text()


def test_rejects_a_bad_version_or_sha(tmp_path: Path) -> None:
    assert build(tmp_path / "a", version="0.2.0").returncode == 2
    assert build(tmp_path / "b", sha="abc").returncode == 2
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_refuses_a_non_empty_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale").write_text("")
    result = build(out)
    assert result.returncode == 1
    assert "not empty" in result.stderr
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_build_hub_repo.py -q`
Expected: FAIL — script not found.

- [ ] **Step 3: Write the script**

```bash
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
printf 'BELLASREEF_VERSION=%s\nBELLASREEF_TAG=%s\nBELLASREEF_CONTRACTS=%s\n' \
    "$version" "$sha" "$contracts" > "${out}/deploy/release.env"

echo "assembled ${version} (${sha:0:12}) into ${out}"
```
`chmod +x scripts/build-hub-repo.sh`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_build_hub_repo.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/build-hub-repo.sh tests/test_build_hub_repo.py
git commit -m "feat(release): build-hub-repo.sh assembles the bellasreef-hub payload with a release manifest"
```

### Task 4: `release.yaml`

**Files:**
- Create: `.github/workflows/release.yaml`

**Interfaces:**
- Consumes: `scripts/build-hub-repo.sh`; images `ghcr.io/viperdavethesnake/bellasreef-<svc>:<sha>` published by `ci.yaml`.
- Produces: images retagged `:<version>`; `bellasreef-hub` commit + tag; release asset `bellasreef-hub-<version>.tar.gz`. Needs repository secret `HUB_REPO_TOKEN` for the publish step only.

- [ ] **Step 1: Write the workflow**

```yaml
name: Release

# A v* tag on main is a release. Nothing else produces a bellasreef-hub commit.
on:
  push:
    tags: ["v*"]

permissions:
  contents: write
  packages: write

jobs:
  release:
    name: retag images · assemble hub · publish
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Log in to ghcr.io
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # A retag, not a rebuild: the release image is bit-for-bit the image
      # CI published for this commit. A missing <sha> tag is the gate — the
      # tag was pushed to a commit CI never published (not on main, or red).
      - name: Retag the CI images for this release
        run: |
          set -euo pipefail
          for svc in hardware-io control-engine api; do
            img="ghcr.io/viperdavethesnake/bellasreef-${svc}"
            if ! docker buildx imagetools inspect "${img}:${GITHUB_SHA}" >/dev/null 2>&1; then
              echo "::error::no image ${img}:${GITHUB_SHA} — this tag is not on a commit CI published"
              exit 1
            fi
            docker buildx imagetools create -t "${img}:${GITHUB_REF_NAME}" "${img}:${GITHUB_SHA}"
          done

      - name: Assemble the hub payload
        run: scripts/build-hub-repo.sh "$RUNNER_TEMP/hub" "$GITHUB_REF_NAME" "$GITHUB_SHA"

      # Before the publish step on purpose: the tarball needs only
      # GITHUB_TOKEN, so a missing HUB_REPO_TOKEN loses one step, not two.
      - name: Attach the payload to the GitHub release
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          asset="$RUNNER_TEMP/bellasreef-hub-${GITHUB_REF_NAME}.tar.gz"
          tar -C "$RUNNER_TEMP" --transform "s,^hub,bellasreef-hub-${GITHUB_REF_NAME}," -czf "$asset" hub
          pre=""
          [[ "$GITHUB_REF_NAME" == *-* ]] && pre="--prerelease"
          if ! gh release view "$GITHUB_REF_NAME" >/dev/null 2>&1; then
            gh release create "$GITHUB_REF_NAME" $pre --verify-tag --title "$GITHUB_REF_NAME" \
              --notes "Images: ghcr.io/viperdavethesnake/bellasreef-{api,control-engine,hardware-io}:${GITHUB_REF_NAME} (built from ${GITHUB_SHA}). Hub payload: bellasreef-hub@${GITHUB_REF_NAME}."
          fi
          gh release upload "$GITHUB_REF_NAME" "$asset" --clobber

      - name: Publish to bellasreef-hub
        env:
          HUB_REPO_TOKEN: ${{ secrets.HUB_REPO_TOKEN }}
        run: |
          set -euo pipefail
          if [[ -z "${HUB_REPO_TOKEN}" ]]; then
            echo "::error::HUB_REPO_TOKEN is not set. Create a fine-grained PAT (repository: bellasreef-hub, Contents: read and write) and add it as an Actions secret on this repo."
            exit 1
          fi
          git clone --quiet "https://x-access-token:${HUB_REPO_TOKEN}@github.com/viperdavethesnake/bellasreef-hub.git" "$RUNNER_TEMP/hub-repo"
          cd "$RUNNER_TEMP/hub-repo"
          find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
          cp -R "$RUNNER_TEMP/hub/." .
          git config user.name "bellas-reef release"
          git config user.email "release@users.noreply.github.com"
          git add -A
          git commit --quiet -m "release ${GITHUB_REF_NAME} from bellas-reef@${GITHUB_SHA}"
          git tag "${GITHUB_REF_NAME}"
          git push --quiet origin HEAD:main "${GITHUB_REF_NAME}"
```

- [ ] **Step 2: Validate the YAML parses**

Run: `uv run python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/release.yaml')); print('ok')"`
Expected: `ok`. (If `yaml` is unavailable in the venv, `python3 -c` with the system interpreter is fine — it only needs to parse.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/release.yaml
git commit -m "ci(release): v* tags retag the CI images, assemble the hub payload, publish bellasreef-hub"
```

### Task 5: PR 1

- [ ] **Step 1: Gate, push, PR**

```bash
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh --quick
git push -u origin feat/hub-payload
gh pr create --title "refactor(hub): hub/ payload, pull-only compose, release assembler and workflow" --body "$(cat <<'EOF'
Part 1 of 4 for docs/superpowers/specs/2026-08-30-hub-repo-split-and-install-design.md.

- hub/ holds exactly what a hub runs from (compose, boot unit, avahi record, installer, operator docs)
- compose.yaml: no build: blocks; BELLASREEF_BACKUP_DIR / BELLASREEF_ETC_DIR interpolated, written by phase 4
- scripts/build-hub-repo.sh + tests: the one assembler of the bellasreef-hub payload
- .github/workflows/release.yaml: on v* — retag CI images, assemble, attach tarball, publish to bellasreef-hub (needs HUB_REPO_TOKEN)

No behaviour change for an installed hub. deploy-pi.sh / factory-reset-pi.sh re-pointed at hub/deploy and deleted in PR 3.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01WEwWri4eyKH53yQkHrDbzK
EOF
)"
```

- [ ] **Step 2: Wait for CI, merge**

Run: `gh pr checks --watch` then `gh pr merge --squash --delete-branch`; `git checkout main && git pull`.

---

## PR 2 — installer

Branch: `git checkout -b feat/install-hub-inventory-and-remediation`.

### Task 6: Phase 4 reads the tag from `deploy/release.env`

**Files:**
- Modify: `hub/scripts/install-hub.sh` (phase 4, lines ~806-836 of the moved file: the git block), `tests/test_install_hub.py`
- Delete tests: `test_phase4_pins_the_image_tag_to_the_checkouts_commit`, `test_phase4_refuses_a_commit_that_never_landed_on_main`, `test_phase4_is_unverified_when_origin_main_is_unknown`, `test_phase4_fails_without_git`.

**Interfaces:**
- Produces: seam `IH_RELEASE_ENV` (default `${REPO_DIR}/deploy/release.env`); test helper `write_release_env(path, version, tag) -> Path`; `script_env` sets `IH_RELEASE_ENV` to a good default file unless overridden.

- [ ] **Step 1: Harness support**

In `tests/test_install_hub.py`, after `FAKE_COMMIT`:

```python
FAKE_VERSION = "v0.2.0-rc.2"


def write_release_env(path: Path, version: str = FAKE_VERSION, tag: str = FAKE_COMMIT) -> Path:
    """A deploy/release.env as the release workflow writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"BELLASREEF_VERSION={version}\nBELLASREEF_TAG={tag}\nBELLASREEF_CONTRACTS=4.2.0\n"
    )
    return path


_default_release_env: Path | None = None


def default_release_env() -> Path:
    """The manifest every run sees unless a test says otherwise. The dev repo
    has no deploy/release.env — the release workflow writes it — so the
    script is pointed at one through the IH_RELEASE_ENV seam."""
    global _default_release_env
    if _default_release_env is None:
        _default_release_env = write_release_env(
            Path(tempfile.mkdtemp(prefix="ih-release-")) / "release.env"
        )
    return _default_release_env
```

In `script_env`, before `if extra:`: `environ["IH_RELEASE_ENV"] = str(default_release_env())`.

- [ ] **Step 2: Write the failing tests**

```python
def test_phase4_reads_the_tag_from_release_env(tmp_path: Path) -> None:
    # The hub checkout is bellasreef-hub, not the dev repo: its commit says
    # nothing about which images were built. The release workflow writes the
    # image SHA into deploy/release.env and that is the only source.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    manifest = write_release_env(tmp_path / "release.env", version="v0.3.0", tag="f" * 40)

    result = run_script(
        "--yes", root=root, stubs=stubs, env={**FAST_POLL, "IH_RELEASE_ENV": str(manifest)}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "image tag v0.3.0 (ffffffffffff)" in result.stdout, result.stdout
    assert f"BELLASREEF_TAG={'f' * 40}" in envfile.read_text()


def test_phase4_stops_without_a_release_manifest(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT})
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "absent")}
    )
    assert result.returncode != 0
    assert "not a released hub" in result.stdout, result.stdout
    assert "bellasreef-hub" in result.stdout
    assert not envfile.exists()


def test_phase4_rejects_a_malformed_release_tag(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT})
    write_stub(stubs, "sudo", "exit 1")
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    manifest = write_release_env(tmp_path / "release.env", tag="latest")

    result = run_script("--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(manifest)})
    assert result.returncode != 0
    assert "malformed" in result.stdout, result.stdout
    assert not envfile.exists()


def test_phase4_warns_without_git_metadata_and_continues(tmp_path: Path) -> None:
    # The release tarball has no .git. That is not a dirty checkout; it is a
    # checkout whose cleanliness cannot be checked, and the run says so.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    (stubs / "git").unlink()
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not a git checkout" in result.stdout, result.stdout
    assert envfile.exists()
```

Delete the four tests listed above. `test_phase4_refuses_a_dirty_checkout` stays as is.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_install_hub.py -q -k "release_env or release_manifest or malformed_release or without_git_metadata"`
Expected: 4 FAIL.

- [ ] **Step 4: Implement**

Near the other seams at the top of `install-hub.sh`:
```bash
# Where the image tag comes from. Written by the release workflow into the
# hub checkout; the dev repo does not have one. A seam only so the tests can
# hand the script a manifest without one existing in the tree.
IH_RELEASE_ENV="${IH_RELEASE_ENV:-}"
```
(after `REPO_DIR` is set:) `: "${IH_RELEASE_ENV:=${REPO_DIR}/deploy/release.env}"`.

Replace the block in `ih_phase4_configure` from the `command -v git` check through `ih_pass "image tag …"` with:

```bash
    # The images come from the registry; compose and the scripts come from
    # this checkout. What holds them to the same build is the release
    # manifest the release workflow wrote beside compose.yaml — this
    # checkout's own git commit is a bellasreef-hub commit and says nothing
    # about which images exist.
    if [[ ! -r "$IH_RELEASE_ENV" ]]; then
        ih_fail "no deploy/release.env; this checkout is not a released hub"
        printf '      Clone the hub repo instead: https://github.com/viperdavethesnake/bellasreef-hub\n'
        return 1
    fi
    local version tag
    version="$(sed -n 's/^BELLASREEF_VERSION=//p' "$IH_RELEASE_ENV" | head -1)"
    tag="$(sed -n 's/^BELLASREEF_TAG=//p' "$IH_RELEASE_ENV" | head -1)"
    if [[ -z "$version" || ! "$tag" =~ ^[0-9a-f]{40}$ ]]; then
        ih_fail "deploy/release.env is malformed (version='${version}', tag='${tag}'); it is written by the release workflow, not by hand"
        return 1
    fi
    # An edited hub checkout is not a release. Checked only when there is
    # git metadata to check against: the release tarball has none, and that
    # is a limit on what can be verified, not evidence of edits.
    if command -v git >/dev/null 2>&1 && git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        if [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]]; then
            ih_fail "this checkout has uncommitted changes; an edited hub checkout is not a release"
            return 1
        fi
    else
        ih_warn "not a git checkout; cannot confirm these files are unmodified"
    fi
    ih_pass "image tag ${version} (${tag:0:12})"
    local commit="$tag"
```
(`commit` keeps the existing sed line `BELLASREEF_TAG=${commit}` and the `ih_would` line working; change the `ih_would` to print `BELLASREEF_TAG=${version}`.)

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest tests/test_install_hub.py -q`
Expected: all pass (82 − 4 + 4 = 82).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(install): read the image tag from deploy/release.env; a hub checkout is a release, not a dev commit"
```

### Task 7: Phase 2 offers to set avahi `allow-interfaces`

**Files:**
- Modify: `hub/scripts/install-hub.sh` (`ih_check_avahi`, the phase-2 avahi block), `tests/test_install_hub.py` (`write_mutation_guard_stubs`, `systemctl_stub`, new tests)

**Interfaces:**
- Produces: `ih_avahi_allow_interfaces_set <conf>`; `ih_set_avahi_allow_interfaces <conf> <list>` (awk render + `sudo install`); systemctl stubs accept `restart`.

- [ ] **Step 1: Harness — systemctl `restart`**

In `write_mutation_guard_stubs`: `case "$1" in enable|reload|restart) touch …`. In `systemctl_stub`, add a line: `f"    restart) {record}exit 0 ;;",`. Add to `_MUTATION_GUARD_COMMANDS`: `"install"`.

- [ ] **Step 2: Write the failing tests**

```python
STOCK_AVAHI_CONF = "[server]\n#host-name=foo\n#allow-interfaces=eth0\n\n[wide-area]\n"


def test_phase2_offers_to_set_avahi_allow_interfaces(tmp_path: Path) -> None:
    # A stock image ships the line commented out, so every clean install
    # used to stop here with a paste-this-yourself message for a value the
    # script had already computed. The list is an exclusion of Docker's
    # bridges, not a choice of the live NIC — a down interface in it is inert.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", install_stub())
    write_stub(
        stubs,
        "systemctl",
        'case "$1" in restart|reload|enable|daemon-reload) exit 0 ;; *) exit 1 ;; esac',
    )
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text(STOCK_AVAHI_CONF)
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script("--yes", root=root, stubs=stubs)
    assert "set avahi allow-interfaces=end0,wlan0? [y/N] y (--yes)" in result.stdout, result.stdout
    text = conf.read_text()
    assert text.startswith("[server]\nallow-interfaces=end0,wlan0\n#host-name=foo\n"), text
    assert "#allow-interfaces=eth0" in text, "the stock commented line must be left alone"
    assert "PASS  avahi allow-interfaces is set" in result.stdout, result.stdout


def test_phase2_declining_allow_interfaces_leaves_the_printed_remedy(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text(STOCK_AVAHI_CONF)
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script(root=root, stubs=stubs, env={"IH_ASSUME_NO_TTY": "1"})
    assert result.returncode == 1
    assert conf.read_text() == STOCK_AVAHI_CONF
    assert "Add to /etc/avahi/avahi-daemon.conf, under [server]:" in result.stdout


def test_phase2_never_touches_an_existing_allow_interfaces_line(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    conf = root / "etc/avahi/avahi-daemon.conf"
    before = conf.read_text()

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "none")}
    )
    assert "set avahi allow-interfaces" not in result.stdout
    assert conf.read_text() == before


def test_phase2_no_allow_interfaces_offer_without_a_server_section(tmp_path: Path) -> None:
    # Nothing to insert after. Inventing a [server] header in someone's
    # config is not this script's call; the printed remedy stands.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text("# empty\n")
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script("--yes", root=root, stubs=stubs)
    assert "set avahi allow-interfaces" not in result.stdout, result.stdout
    assert conf.read_text() == "# empty\n"


def test_phase2_allow_interfaces_dry_run_writes_nothing(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text(STOCK_AVAHI_CONF)
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert "would  setting avahi allow-interfaces=end0,wlan0" in result.stdout, result.stdout
    assert conf.read_text() == STOCK_AVAHI_CONF
    assert not markers["install"].exists()
    assert not markers["systemctl"].exists()
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_install_hub.py -q -k allow_interfaces`
Expected: 5 FAIL (the "never touches" one may pass already — that is fine; it guards the new code path).

- [ ] **Step 4: Implement**

Add above `ih_check_avahi`:
```bash
ih_avahi_allow_interfaces_set() {
    grep -qE '^[[:space:]]*allow-interfaces[[:space:]]*=' "$1"
}

# Inserts the line directly under [server]. Rendered with awk into a temp
# file and installed over the original, rather than sed -i: the two seds
# disagree on `a\` and this has to behave the same on the Pi and on the
# machine running the tests. `sudo install` is what puts it back, so the
# file keeps root ownership and 0644.
ih_set_avahi_allow_interfaces() {
    local conf="$1" lan="$2" tmp rc
    tmp="$(mktemp)" || return 1
    if ! awk -v line="allow-interfaces=${lan}" \
            '{ print } /^\[server\]/ && !done { print line; done=1 }' "$conf" > "$tmp"; then
        rm -f "$tmp"; return 1
    fi
    sudo install -m 0644 "$tmp" "$conf"; rc=$?
    rm -f "$tmp"
    return $rc
}
```
In `ih_check_avahi`, replace the `grep -qE '^[[:space:]]*allow-interfaces…' "$conf"` test with `ih_avahi_allow_interfaces_set "$conf"`, and rewrite the comment at the top of that branch to: `# Phase 2 offers to write this line (ih_set_avahi_allow_interfaces); the printed remedy below is for the cases it will not touch: no [server] section, or interfaces it could not read.`

In `ih_phase2_requirements`, inside the `if (( ! IH_CHECK_ONLY )) && ! ih_check_quietly ih_check_avahi; then` block, after the service-record offer:
```bash
        # The allowlist. Only when the daemon is present, the line is absent,
        # this machine's interfaces could be read, and there is a [server]
        # section to put it under — a guessed list or an invented section
        # would be worse than the printed remedy.
        local conf="${IH_ROOT}/etc/avahi/avahi-daemon.conf" lan
        if ih_avahi_daemon_present && ! ih_avahi_allow_interfaces_set "$conf" \
                && lan="$(ih_lan_interfaces)" && grep -q '^\[server\]' "$conf"; then
            if ih_confirm "set avahi allow-interfaces=${lan}?"; then
                if ih_run "setting avahi allow-interfaces=${lan}" ih_set_avahi_allow_interfaces "$conf" "$lan"; then
                    # restart, not reload: interface config is read at start.
                    ih_run "restarting avahi" sudo systemctl restart avahi-daemon
                fi
            fi
        fi
```
Replace the old comment "Neither prompt ever touches avahi-daemon.conf's allow-interfaces" with "The allowlist is offered below; the printed remedy in ih_check_avahi covers what the offer will not touch."

- [ ] **Step 5: Run the suite**

Run: `uv run pytest tests/test_install_hub.py -q`
Expected: all pass. `test_dry_run_reports_actions_without_running_them` and `test_check_only_never_remediates_even_with_yes` still pass (the offer is inside the `! IH_CHECK_ONLY` block and goes through `ih_run`).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(install): offer to write avahi allow-interfaces for this machine's interfaces instead of stopping"
```

### Task 8: Phase 2 checks docker log rotation and offers `daemon.json`

**Files:**
- Modify: `hub/scripts/install-hub.sh`, `tests/test_install_hub.py`

**Interfaces:**
- Produces: `ih_check_docker_logging` (WARN-only), `ih_write_docker_daemon_json`.

- [ ] **Step 1: Write the failing tests**

```python
DAEMON_JSON = (
    '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" }\n}\n'
)


def test_phase2_offers_docker_log_rotation_when_daemon_json_is_absent(tmp_path: Path) -> None:
    # Docker's default json-file driver never rotates, and compose.yaml
    # deliberately carries no per-service logging: block. Six always-on
    # services fill a disk eventually, silently. hub-prereqs documented this
    # as a hand step; the installer does it.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", install_stub())
    write_stub(
        stubs,
        "systemctl",
        'case "$1" in restart|reload|enable|daemon-reload) exit 0 ;; *) exit 1 ;; esac',
    )
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "none")}
    )
    assert "configure docker log rotation (json-file, 10m x 3)? [y/N] y (--yes)" in result.stdout, (
        result.stdout
    )
    assert (root / "etc/docker/daemon.json").read_text() == DAEMON_JSON
    assert "PASS  docker log rotation configured" in result.stdout, result.stdout


def test_phase2_reports_but_never_rewrites_an_existing_daemon_json(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "etc/docker").mkdir(parents=True)
    (root / "etc/docker/daemon.json").write_text('{"data-root": "/mnt/docker"}\n')

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "none")}
    )
    assert "configure docker log rotation" not in result.stdout, result.stdout
    assert "WARN  /etc/docker/daemon.json exists but sets no log rotation" in result.stdout, (
        result.stdout
    )
    assert (root / "etc/docker/daemon.json").read_text() == '{"data-root": "/mnt/docker"}\n'


def test_phase2_declined_log_rotation_is_a_warn_not_a_gate(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    result = run_script("--check-only", root=root, stubs=stubs)
    assert "WARN  no /etc/docker/daemon.json" in result.stdout, result.stdout
    assert result.returncode == 0, "a missing daemon.json must not fail the run"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_install_hub.py -q -k "daemon_json or log_rotation"`
Expected: 3 FAIL.

- [ ] **Step 3: Implement**

After `ih_check_docker`:
```bash
# Docker's json-file driver never rotates on its own, and compose.yaml
# carries no per-service logging: block on purpose — the daemon default is
# the one place rotation exists. WARN, never FAIL: the stack runs without it;
# the disk fills later.
ih_docker_daemon_json() { printf '%s' "${IH_ROOT}/etc/docker/daemon.json"; }

ih_check_docker_logging() {
    local f
    f="$(ih_docker_daemon_json)"
    if [[ -f "$f" ]] && grep -q '"max-size"' "$f"; then
        ih_pass "docker log rotation configured"
        return 0
    fi
    if [[ -f "$f" ]]; then
        # Present but silent on rotation. Merging a stranger's JSON blind is
        # how a daemon stops starting, so this is reported and left alone.
        ih_warn "/etc/docker/daemon.json exists but sets no log rotation; container logs grow without bound"
        printf '      Add under "log-opts": { "max-size": "10m", "max-file": "3" }, then restart docker.\n'
    else
        ih_warn "no /etc/docker/daemon.json; container logs grow without bound"
    fi
    return 0
}

ih_write_docker_daemon_json() {
    local tmp rc f
    f="$(ih_docker_daemon_json)"
    tmp="$(mktemp)" || return 1
    printf '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" }\n}\n' > "$tmp"
    sudo mkdir -p "$(dirname "$f")" && sudo install -m 0644 "$tmp" "$f"; rc=$?
    rm -f "$tmp"
    return $rc
}
```
In `ih_phase2_requirements`, after the docker remediation block and before `ih_check_quietly ih_check_arch`:
```bash
    # Offered only when the file is absent — see ih_check_docker_logging.
    if (( ! IH_CHECK_ONLY )) && ih_docker_present && [[ ! -f "$(ih_docker_daemon_json)" ]]; then
        if ih_confirm "configure docker log rotation (json-file, 10m x 3)?"; then
            if ih_run "writing /etc/docker/daemon.json" ih_write_docker_daemon_json; then
                ih_run "restarting docker" sudo systemctl restart docker
            fi
        fi
    fi
```
Add `ih_check_docker_logging` to the re-check list right after `ih_check_docker`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_install_hub.py -q`
Expected: all pass. If `test_yes_accepts_offers_without_prompting` or `test_dry_run_reports_actions_without_running_them` assert an exact count of prompts, update the count (+1 for the log-rotation offer) — read the assertion before changing it.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(install): check docker log rotation; offer the documented daemon.json when none exists"
```

### Task 9: Phase 3 is an inventory

**Files:**
- Modify: `hub/scripts/install-hub.sh` (`ih_phase3_hardware`, the comment above `ih_detect_board`), `tests/test_install_hub.py` (`HIDDEN_FROM_PATH` += `"i2cget"`; phase-3 tests)

**Interfaces:**
- Produces: phase 3 lines `board`, `I2C`, `1-Wire`, `SoC PWM` as in spec §5; no prompt; never returns non-zero.

- [ ] **Step 1: Update and add tests**

Delete `test_phase3_says_which_interfaces_are_absent`, `test_phase3_never_advises_pwm4chan_on_a_non_pi5`, `test_phase3_skips_boot_config_on_a_non_pi`. Update `test_phase3_flags_pwm_chips_with_no_muxed_pins` to assert `"no header pin is muxed to PWM" in result.stdout` and `"docs/host-setup.md" in result.stdout`. Update `test_phase3_says_the_pin_mux_is_unverified_without_pinctrl` to assert `"pin mux not verified (pinctrl not installed)"`. Add:

```python
def test_phase3_reports_the_board(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 5 Model B Rev 1.1\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "board        Raspberry Pi 5 Model B Rev 1.1 (RP1 present)" in result.stdout, (
        result.stdout
    )
    assert result.returncode == 0


def test_phase3_probes_for_a_pca9685_when_i2c_tools_exist(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    log = tmp_path / "i2cget.log"
    write_stub(stubs, "i2cget", f'echo "$*" >> "{log}"; echo 0x11; exit 0')
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "dev").mkdir(parents=True)
    (root / "dev/i2c-1").write_text("")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "I2C          bus 1 present; PCA9685 at 0x40: answering" in result.stdout, result.stdout
    # One MODE1 read at 0x40. 0x70 is the chip's all-call address and is
    # never addressed (CLAUDE.md, verified host facts).
    assert log.read_text().strip() == "-y 1 0x40 0x00"


def test_phase3_says_not_probed_without_i2c_tools(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "dev").mkdir(parents=True)
    (root / "dev/i2c-1").write_text("")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "PCA9685 not probed (i2c-tools not installed)" in result.stdout, result.stdout


def test_phase3_counts_ds18b20_probes_and_names_a_floating_bus(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    devices = root / "sys/bus/w1/devices"
    for name in ("w1_bus_master1", "00-100000000000", "00-600000000000"):
        (devices / name).mkdir(parents=True)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert (
        "1-Wire       bus present; DS18B20 probes: 0 (bus up, nothing answering" in result.stdout
    ), result.stdout

    (devices / "28-000000bfe244").mkdir()
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "1-Wire       bus present; DS18B20 probes: 1" in result.stdout, result.stdout


def test_phase3_never_prescribes_boot_config_and_never_prompts(tmp_path: Path) -> None:
    # Nothing hardware-side is required to deploy the stack (ruled
    # 2026-08-30). No config.txt paste, no "reboot and re-run", no
    # "proceed with only these?" — a hub with no PWM and no probes is a
    # valid hub that happens to have nothing attached yet.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 5 Model B Rev 1.1\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    for forbidden in ("dtoverlay=", "dtparam=", "reboot", "proceed with only"):
        assert forbidden not in result.stdout, (forbidden, result.stdout)
    assert result.returncode == 0
```

Add `"i2cget"` to `HIDDEN_FROM_PATH` with the comment `# Phase 3's PCA9685 probe. Hidden so a bench machine with i2c-tools does not answer for a fixture that never had a bus.`

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_install_hub.py -q -k phase3`
Expected: the five new tests FAIL; old ones pass or fail on changed strings.

- [ ] **Step 3: Replace `ih_phase3_hardware`**

```bash
# Reported, never required. An owner may want temperature and no lights, or
# lights over a PCA9685 and no SoC PWM, or — on the day the hub is installed
# — nothing attached at all. This mirrors capabilities.py, which announces
# what it can prove and holds no view on what should be there. Nothing here
# gates the install, prints boot-config advice, or asks a question; the
# custom overlay procedure for RP1 PWM lives in docs/host-setup.md §9.
ih_phase3_hardware() {
    ih_step "3. hardware inventory (reported, never required)"

    local board model_text
    board="$(ih_detect_board)"
    model_text="$(tr -d '\0' < "${IH_ROOT}/proc/device-tree/model" 2>/dev/null || true)"
    case "$board" in
        pi5)   ih_pass "board        ${model_text} (RP1 present)" ;;
        pi)    ih_pass "board        ${model_text} (no RP1: SoC PWM unavailable in this stack; a PCA9685 over I2C works)" ;;
        *)     ih_warn "board        ${model_text:-unknown} — not a Raspberry Pi" ;;
    esac

    # /dev/i2c-1 specifically, not a glob over /dev/i2c-*: the Pi's HDMI DDC
    # buses are i2c-13/14 and are present with i2c_arm off. One MODE1 read at
    # 0x40 if i2c-tools is here; 0x70 (ALLCALL) is never addressed.
    if [[ -e "${IH_ROOT}/dev/i2c-1" ]]; then
        local pca
        if command -v i2cget >/dev/null 2>&1; then
            if i2cget -y 1 0x40 0x00 >/dev/null 2>&1; then
                pca="PCA9685 at 0x40: answering"
            else
                pca="PCA9685 at 0x40: not answering"
            fi
        else
            pca="PCA9685 not probed (i2c-tools not installed)"
        fi
        ih_pass "I2C          bus 1 present; ${pca}"
    else
        ih_warn "I2C          bus 1 absent"
    fi

    # 28-* is a DS18B20. 00-* entries are what a floating bus enumerates when
    # nothing (or nothing pulled up) is on it — reported as such, so "probes:
    # 0" on a bus that is clearly searching is not mistaken for a dead bus.
    local w1="${IH_ROOT}/sys/bus/w1/devices"
    if [[ -d "$w1" ]]; then
        local probes phantoms
        probes="$(find "$w1" -maxdepth 1 -name '28-*' 2>/dev/null | wc -l | tr -d ' ')"
        phantoms="$(find "$w1" -maxdepth 1 -name '00-*' 2>/dev/null | wc -l | tr -d ' ')"
        if (( probes > 0 )); then
            ih_pass "1-Wire       bus present; DS18B20 probes: ${probes}"
        elif (( phantoms > 0 )); then
            ih_pass "1-Wire       bus present; DS18B20 probes: 0 (bus up, nothing answering — no probe, or no pull-up)"
        else
            ih_pass "1-Wire       bus present; DS18B20 probes: 0"
        fi
    else
        ih_warn "1-Wire       no bus"
    fi

    # A pwmchip in sysfs says nothing about the header: the standing trap
    # (CLAUDE.md, verified host facts) is a chip that exports while every pin
    # reads `none`. pinctrl is the evidence, the same one hardware-io uses.
    if compgen -G "${IH_ROOT}/sys/class/pwm/pwmchip*" >/dev/null 2>&1; then
        if command -v pinctrl >/dev/null 2>&1; then
            local muxed
            muxed="$(pinctrl get 12,13,18,19 2>/dev/null | grep -c 'PWM')"
            [[ "$muxed" =~ ^[0-9]+$ ]] || muxed=0
            if (( muxed > 0 )); then
                ih_pass "SoC PWM      pwmchip present; ${muxed} of 4 header pins muxed to PWM"
            else
                ih_warn "SoC PWM      pwmchip present but no header pin is muxed to PWM (optional; docs/host-setup.md §9)"
            fi
        else
            ih_pass "SoC PWM      pwmchip present; pin mux not verified (pinctrl not installed)"
        fi
    else
        ih_warn "SoC PWM      no pwmchip (optional; RP1 PWM needs the overlay in docs/host-setup.md §9)"
    fi
    return 0
}
```
Delete the old `ih_phase3_hardware` entirely, including the `case "$board"` advice block and the `ih_confirm "proceed with only the interfaces above?"`.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_install_hub.py -q`
Expected: all pass. `test_check_only_reports_the_inventory_even_when_phase2_fails` still finds `I2C`; `test_phase3_ignores_the_hdmi_i2c_buses` asserts the absence line — update its expected string to `"I2C          bus 1 absent"`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(install): phase 3 is an inventory — board, PCA9685 probe, DS18B20 count, PWM mux; nothing hardware-side gates the stack"
```

### Task 10: Phase 5 creates the bind-mount directories

**Files:**
- Modify: `hub/scripts/install-hub.sh` (phase 5, between migrations and the boot unit), `tests/test_install_hub.py` (`install_stub`, order test, two new tests)

- [ ] **Step 1: Harness — `install -d`**

Replace `install_stub`'s body so `-d` creates a directory and logs `install-dir`:
```python
def install_stub(log: Path | None = None) -> str:
    record_unit = f'echo install-unit >> "{log}"\n' if log is not None else ""
    record_dir = f'echo install-dir >> "{log}"; ' if log is not None else ""
    return (
        f'if [[ "$1" == "-d" ]]; then {record_dir}mkdir -p "${{@: -1}}"; exit 0; fi\n'
        + record_unit
        + 'src="${@: -2:1}"; dst="${@: -1}"\n'
        'if [[ "$dst" == */ || -d "$dst" ]]; then dst="${dst%/}/$(basename "$src")"; fi\n'
        'mkdir -p "$(dirname "$dst")" || exit 1\n'
        'cat "$src" > "$dst" || exit 1\n'
        "exit 0"
    )
```
In `test_phase5_deploys_in_the_required_order`, the expected log becomes `["pull", "migrate", "install-dir", "install-dir", "install-unit", "systemctl", "daemon-reload", "systemctl", "enable", "up", "-d", "--wait"]`.

- [ ] **Step 2: Write the failing tests**

```python
def test_phase5_creates_the_bind_mount_directories_owned_by_the_image_uid(tmp_path: Path) -> None:
    # compose.yaml bind-mounts both; a missing host path is auto-created by
    # Docker as root:root, and the api container (uid 1000) then cannot write
    # a backup. The installer creates them owned by the container uid, which
    # makes the operating user's own uid irrelevant.
    log = tmp_path / "actions.log"
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    write_stub(stubs, "install", install_stub(log))
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env={**FAST_POLL, "HOME": "/home/tester"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "home/tester/backups").is_dir()
    assert (root / "etc/bellasreef").is_dir()
    assert "creating /home/tester/backups (owned by the container uid 1000)" in result.stdout, (
        result.stdout
    )


def test_phase5_leaves_existing_directories_alone(tmp_path: Path) -> None:
    log = tmp_path / "actions.log"
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    write_stub(stubs, "install", install_stub(log))
    root = phase5_root(tmp_path)
    (root / "home/tester/backups").mkdir(parents=True)
    (root / "etc/bellasreef").mkdir(parents=True)

    result = run_script("--yes", root=root, stubs=stubs, env={**FAST_POLL, "HOME": "/home/tester"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "install-dir" not in log.read_text()
    assert "directory /home/tester/backups exists; left as is" in result.stdout, result.stdout
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_install_hub.py -q -k "bind_mount or existing_directories or required_order"`
Expected: 3 FAIL.

- [ ] **Step 4: Implement**

In `ih_phase5_deploy`, after `ih_pass "migrations applied"`:
```bash
    # The two host paths compose.yaml bind-mounts into api. Docker would
    # auto-create a missing one as root:root, and the container runs as
    # 1000 — so `bellasreef backup` would fail on the day it was needed.
    # Created here owned by the image uid; an existing directory is left
    # exactly as found. `${IH_ROOT}` prefixes the host path; the value in
    # .env stays the bare path the container sees on a real machine.
    local backup_dir etc_dir d
    backup_dir="$(sed -n 's/^BELLASREEF_BACKUP_DIR=//p' "$IH_ENVFILE" | head -1)"
    etc_dir="$(sed -n 's/^BELLASREEF_ETC_DIR=//p' "$IH_ENVFILE" | head -1)"
    if [[ -z "$backup_dir" || -z "$etc_dir" ]]; then
        ih_fail "deploy/.env has no BELLASREEF_BACKUP_DIR / BELLASREEF_ETC_DIR; cannot create the bind-mount directories"
        return 1
    fi
    for d in "$backup_dir" "$etc_dir"; do
        if [[ -d "${IH_ROOT}${d}" ]]; then
            ih_pass "directory ${d} exists; left as is"
        else
            ih_run "creating ${d} (owned by the container uid 1000)" \
                sudo install -d -m 0755 -o 1000 -g 1000 "${IH_ROOT}${d}" || return 1
        fi
    done
```
Also add `ih_would "create the backup and /etc/bellasreef directories"` to the dry-run list at the top of phase 5.

- [ ] **Step 5: Run the suite**

Run: `uv run pytest tests/test_install_hub.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(install): create the backup and /etc/bellasreef bind-mount directories owned by the container uid before up"
```

### Task 11: Phase 6 hands off the device import and prints the hub identity

**Files:**
- Modify: `hub/scripts/install-hub.sh` (end of `ih_phase6_verify`), `tests/test_install_hub.py` (`HIDDEN_FROM_PATH` += `"hostname"`; `FULL_STUBS["hostname"]`; two tests)

- [ ] **Step 1: Harness**

Add to `HIDDEN_FROM_PATH`: `"hostname"` (comment: `# Phase 6's identity block. The runner's own hostname is not the hub's.`). Add to `FULL_STUBS`: `"hostname": 'case "${1:-}" in -I) echo "192.168.33.105 172.17.0.1 " ;; *) echo coco-test ;; esac'`.

- [ ] **Step 2: Write the failing tests**

```python
def test_phase6_hands_off_the_device_import_step(tmp_path: Path) -> None:
    # A fresh registry has no devices, so no telemetry, so nothing for the
    # app to show. The import is the next thing an owner does and the old
    # hand-off never mentioned it (install-hub-3bplus-readiness, 2026-08-17).
    stubs, _ = phase6_stubs(tmp_path)
    root = phase5_root(tmp_path)
    result = run_script("--yes", root=root, stubs=stubs, env={**FAST_POLL, "HOME": "/home/tester"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "/etc/bellasreef/devices.import.yaml" in result.stdout, result.stdout
    assert "bellasreef devices import /etc/bellasreef/devices.import.yaml" in result.stdout
    assert "deploy/config/devices.yaml.example" in result.stdout


def test_phase6_prints_the_hub_identity(tmp_path: Path) -> None:
    stubs, _ = phase6_stubs(tmp_path)
    root = phase5_root(tmp_path)
    (root / "proc/device-tree").mkdir(parents=True, exist_ok=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 5 Model B Rev 1.1\x00")
    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "hostname   coco-test" in out, out
    assert "board      Raspberry Pi 5 Model B Rev 1.1" in out, out
    assert "addresses  192.168.33.105 172.17.0.1" in out, out
    assert f"release    {FAKE_VERSION} ({FAKE_COMMIT[:12]})" in out, out
```

Read `phase6_stubs` first (line ~2027) to confirm it returns `(stubs, markers)` and clears phases 1–5; if it does not create `proc/device-tree`, the identity test's `mkdir(exist_ok=True)` covers it.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_install_hub.py -q -k "device_import_step or hub_identity"`
Expected: 2 FAIL.

- [ ] **Step 4: Implement**

Before the final `(( ${#IH_FAILURES[@]} == 0 … ))` line of `ih_phase6_verify`:
```bash
    ih_phase6_handoff
```
and add the function after `ih_phase6_verify`:
```bash
# The two things the setup code does not say: what to do next, and which
# machine this transcript is about.
ih_phase6_handoff() {
    local etc_dir version tag
    etc_dir="$(sed -n 's/^BELLASREEF_ETC_DIR=//p' "$IH_ENVFILE" | head -1)"
    etc_dir="${etc_dir:-/etc/bellasreef}"
    printf '\n'
    ih_step "Next: tell the hub what is attached"
    printf '      A fresh hub has no devices, so nothing to read and nothing to show.\n'
    printf '      Copy deploy/config/devices.yaml.example to %s/devices.import.yaml,\n' "$etc_dir"
    printf '      edit it for your hardware, then with a token from pairing:\n'
    printf '        docker compose -f %s --env-file %s \\\n' "$IH_COMPOSE" "${IH_ENVFILE#"$IH_ROOT"}"
    printf '          exec api bellasreef devices import /etc/bellasreef/devices.import.yaml\n'

    version="$(sed -n 's/^BELLASREEF_VERSION=//p' "$IH_RELEASE_ENV" 2>/dev/null | head -1)"
    tag="$(sed -n 's/^BELLASREEF_TAG=//p' "$IH_RELEASE_ENV" 2>/dev/null | head -1)"
    printf '\n'
    ih_step "This hub"
    printf '      hostname   %s\n' "$(hostname 2>/dev/null || echo unknown)"
    printf '      board      %s\n' "$(tr -d '\0' < "${IH_ROOT}/proc/device-tree/model" 2>/dev/null || echo unknown)"
    printf '      memory     %s MB\n' "$(awk '/^Mem/ {print int($2/1024); exit}' <(free -k 2>/dev/null) 2>/dev/null)"
    printf '      free disk  %s GB\n' "$(df -k --output=avail "${IH_ROOT}/" 2>/dev/null | tail -1 | awk '{print int($1/1000/1000)}')"
    printf '      addresses  %s\n' "$(hostname -I 2>/dev/null | sed 's/ *$//')"
    printf '      release    %s (%s)\n' "${version:-unknown}" "${tag:0:12}"
    printf '      checkout   %s\n' "$REPO_DIR"
}
```

- [ ] **Step 5: Run the suite and the gate**

Run: `uv run pytest tests/test_install_hub.py -q && BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh --quick`
Expected: all pass; shellcheck clean.

- [ ] **Step 6: Commit, PR 2**

```bash
git add -A
git commit -m "feat(install): hand off the device import and print the hub identity at the end of phase 6"
git push -u origin feat/install-hub-inventory-and-remediation
gh pr create --title "feat(install): release-manifest tag, avahi and log-rotation remediations, inventory-only phase 3, bind-mount dirs, hand-off" --body "Part 2 of 4 for docs/superpowers/specs/2026-08-30-hub-repo-split-and-install-design.md — §5 in full. Found on coco-bellasreef's maiden run (2026-08-30): phase 2 stopped on an allow-interfaces line the script had computed and refused to write.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01WEwWri4eyKH53yQkHrDbzK"
gh pr checks --watch && gh pr merge --squash --delete-branch && git checkout main && git pull
```

---

## PR 3 — lifecycle scripts

Branch: `git checkout -b feat/on-pi-lifecycle-scripts`.

### Task 12: Shared harness `tests/hub_script_harness.py`

**Files:**
- Create: `tests/hub_script_harness.py`
- Modify: `tests/test_install_hub.py` (imports)

**Interfaces:**
- Produces: `HIDDEN_FROM_PATH`, `real_bin_dir()`, `script_env(root, stubs, extra, *, release_env: Path | None)`, `run_any_script(script: Path, *args, root, stubs, env, input)`, `write_stub`. `test_install_hub.py` keeps its own `run_script` as a one-line wrapper: `run_any_script(SCRIPT, *args, …)`.

- [ ] **Step 1: Move the helpers**

Cut from `test_install_hub.py` into the new module, unchanged: `HIDDEN_FROM_PATH`, `_real_bin_dir`/`real_bin_dir`, `script_env`, `write_stub`, `default_release_env`, `write_release_env`. Add `"volume"`-free generic runner:
```python
def run_any_script(
    script: Path,
    *args: str,
    root: Path,
    stubs: Path | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        capture_output=True,
        text=True,
        env=script_env(root, stubs, env),
        input=input,
        check=False,
        timeout=120,
    )
```
Keep `HIDDEN_FROM_PATH` a `frozenset` but add the names Task 14 needs: `"docker"`, `"systemctl"`, `"sudo"`, `"curl"` are already there; add `"sleep"` is NOT hidden (the reset script's retries use seams instead).

In `test_install_hub.py`: `from tests.hub_script_harness import (...)` — check how `conftest.py` sets `rootdir`; if `tests` is not importable as a package, add an empty `tests/__init__.py` and confirm `uv run pytest` still collects `tests/test_env_boundary*.py` (or whatever the other file in `tests/` is — there are two files; read `ls tests`).

- [ ] **Step 2: Run the suite**

Run: `uv run pytest tests -q`
Expected: same pass count as before the move.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test: lift the install-hub subprocess harness into tests/hub_script_harness.py for the sibling scripts"
```

### Task 13: `hub/scripts/update-hub.sh` skeleton

**Files:**
- Create: `hub/scripts/update-hub.sh`
- Test: `tests/test_update_hub.py`
- Modify: `tests/test_build_hub_repo.py` (`EXPECTED` += `"scripts/update-hub.sh"`)

- [ ] **Step 1: Write the failing test**

```python
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""update-hub.sh is a skeleton by ruling (2026-08-30): usage and the recorded
design, and an honest not-implemented exit. These tests pin exactly that."""

from __future__ import annotations

from pathlib import Path

from tests.hub_script_harness import run_any_script

SCRIPT = Path(__file__).resolve().parents[1] / "hub/scripts/update-hub.sh"


def test_help_names_the_planned_phases(tmp_path: Path) -> None:
    result = run_any_script(SCRIPT, "--help", root=tmp_path)
    assert result.returncode == 0
    for phase in ("installed", "release", "backup", "pull", "verify"):
        assert phase in result.stdout.lower(), result.stdout


def test_a_plain_run_says_not_implemented_and_exits_70(tmp_path: Path) -> None:
    result = run_any_script(SCRIPT, root=tmp_path)
    assert result.returncode == 70
    assert "not implemented" in result.stderr.lower(), result.stderr
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_update_hub.py -q` → FAIL (not found).

- [ ] **Step 3: Write the skeleton**

```bash
#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
#
# Bella's Reef — move this hub to a newer release. Runs on the hub, from the
# bellasreef-hub clone. NOT YET IMPLEMENTED (skeleton by ruling, 2026-08-30:
# install first). The design as far as it is decided, so it is not re-derived:
#
#   1. installed?     refuse a machine with no bellasreef.service / deploy/.env
#   2. release        default: newest stable v* tag (no -suffix); --pre allows
#                     the newest pre-release; --ref <tag> pins one.
#                     OPEN (David): may a plain run ever move to main? (no)
#   3. checkout       git fetch --tags; git checkout <tag>; then RE-EXEC this
#                     script with --stage2, because the file now running may
#                     have changed under bash.
#   4. backup         mandatory, same mechanism as factory-reset-hub.sh
#   5. deploy         docker compose pull → run --rm api alembic upgrade head
#                     → up -d --wait (app services only change) → rewrite
#                     BELLASREEF_TAG in deploy/.env from deploy/release.env
#   6. verify         THREE outcomes: PASS (fresh telemetry on the wire),
#                     NO DEVICES (registry empty; update complete; prints the
#                     import command; exit 0), FAIL (devices registered and
#                     nothing on the wire within the deadline).
set -uo pipefail

usage() {
    cat <<'USAGE'
update-hub.sh — move this hub to a newer release   (not yet implemented)

  --pre         allow the newest pre-release (vX.Y.Z-rc.N)
  --ref <tag>   pin a specific release tag
  --help        this text

Planned phases: 1 installed?  2 choose release  3 checkout and re-exec
                4 backup  5 pull, migrate, up  6 verify (PASS | NO DEVICES | FAIL)
USAGE
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

printf 'update-hub: not implemented yet — see the header of this file for the design.\n' >&2
exit 70
```
`chmod +x`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_update_hub.py tests/test_build_hub_repo.py -q` → pass after adding `"scripts/update-hub.sh"` to `EXPECTED`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(hub): update-hub.sh skeleton — usage, recorded design, exit 70"
```

### Task 14: `hub/scripts/factory-reset-hub.sh` (on-Pi); delete the two workstation scripts

**Files:**
- Create: `hub/scripts/factory-reset-hub.sh`
- Delete: `scripts/factory-reset-pi.sh`, `scripts/deploy-pi.sh`
- Modify: `tests/test_build_hub_repo.py` (`EXPECTED` += `"scripts/factory-reset-hub.sh"`), `scripts/check.sh:118` (comment: "deploy-pi.sh renders the value" → "install-hub.sh installs this file verbatim"), `hub/deploy/avahi/bellasreef.service` (comment block: drop the deploy-pi.sh sentence), `hub/deploy/compose.yaml:19` (comment: `BELLASREEF_TAG is written into deploy/.env by install-hub.sh from deploy/release.env`)
- Test: `tests/test_factory_reset_hub.py`

**Interfaces:**
- Seams: `FR_API_DEADLINE_SECS` (default 30), `FR_STREAM_DEADLINE_SECS` (default 60), `FR_POLL_SECS` (default 3), `IH_ROOT` (same meaning as the installer's). Confirmation is read from **stdin** (`echo factory-reset | ./scripts/factory-reset-hub.sh` stays valid); every `docker … exec` gets `</dev/null`.

- [ ] **Step 1: Write the failing tests**

```python
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Black-box tests for hub/scripts/factory-reset-hub.sh — the on-Pi reset."""

from __future__ import annotations

from pathlib import Path

from tests.hub_script_harness import run_any_script, write_stub

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB_ROOT = REPO_ROOT / "hub"
SCRIPT = HUB_ROOT / "scripts" / "factory-reset-hub.sh"
FAST = {"FR_API_DEADLINE_SECS": "2", "FR_STREAM_DEADLINE_SECS": "2", "FR_POLL_SECS": "0"}

INFO_FRESH = '{"paired_client_count":0,"setup_mode":true}'
INFO_PAIRED = '{"paired_client_count":1,"setup_mode":false}'
HW_LOG = "\n".join(['{"msg":"stream created"}'] * 7 + ['{"msg":"capability announced"}'])


def installed_root(tmp_path: Path) -> Path:
    """A fixture root on which a hub is installed: the boot unit exists and
    deploy/.env is non-empty (the two things the script refuses without)."""
    root = tmp_path / "root"
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/systemd/system/bellasreef.service").write_text("[Unit]\n")
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True)
    envfile.write_text("POSTGRES_PASSWORD=x\nBELLASREEF_BACKUP_DIR=/home/tester/backups\n")
    return root


def docker_stub(
    log: Path,
    *,
    info: str = INFO_FRESH,
    psql: str = "0\n0",
    alembic: str = "0020 (head)",
    volumes: str = "bellasreef_nats-data\nbellasreef_postgres-data\nbellasreef_vm-data",
    hw_log: str = HW_LOG,
    backup_rc: int = 0,
) -> str:
    return f"""
echo "docker $*" >> "{log}"
case "$1" in
  compose)
    shift; while [[ "$1" == -* ]]; do shift 2; done
    case "$1" in
      exec)
        case "$*" in
          *"bellasreef backup"*) exit {backup_rc} ;;
          *psql*) printf '{psql}\\n'; exit 0 ;;
          *"alembic current"*) echo "{alembic}"; exit 0 ;;
          *"setup-code"*) echo "7KF2-9QMD"; exit 0 ;;
        esac ;;
      down|run) exit 0 ;;
    esac ;;
  volume)
    case "$2" in ls) printf '{volumes}\\n'; exit 0 ;; rm) exit 0 ;; esac ;;
  logs) printf '%s\\n' '{hw_log}'; exit 0 ;;
esac
exit 0
"""


def stubs_for(tmp_path: Path, log: Path, **docker_kwargs: object) -> Path:
    stubs = tmp_path / "bin"
    write_stub(stubs, "docker", docker_stub(log, **docker_kwargs))  # type: ignore[arg-type]
    write_stub(stubs, "sudo", f'echo "sudo $*" >> "{log}"; exit 0')
    write_stub(stubs, "systemctl", f'echo "systemctl $*" >> "{log}"; exit 0')
    write_stub(stubs, "curl", f"printf '%s' '{docker_kwargs.get('info', INFO_FRESH)}'; exit 0")
    return stubs


def run(tmp_path: Path, *args: str, confirm: str = "factory-reset\n", **kw: object):
    log = tmp_path / "actions.log"
    stubs = stubs_for(tmp_path, log, **kw)
    root = installed_root(tmp_path)
    result = run_any_script(SCRIPT, *args, root=root, stubs=stubs, env=FAST, input=confirm)
    return result, (log.read_text() if log.exists() else "")


def test_help_and_unknown_args_exit_before_anything_runs(tmp_path: Path) -> None:
    result, log = run(tmp_path, "--help")
    assert result.returncode == 0 and "DESTROYS" in result.stdout
    assert log == ""
    result, log = run(tmp_path, "--now")
    assert result.returncode == 1
    assert log == ""


def test_refuses_an_uninstalled_hub_before_the_backup(tmp_path: Path) -> None:
    log = tmp_path / "actions.log"
    stubs = stubs_for(tmp_path, log)
    root = tmp_path / "root"
    root.mkdir()
    result = run_any_script(SCRIPT, root=root, stubs=stubs, env=FAST, input="factory-reset\n")
    assert result.returncode == 1
    assert "nothing to reset" in result.stderr, result.stderr
    assert not log.exists()


def test_backup_runs_before_the_prompt_and_a_decline_touches_nothing_else(tmp_path: Path) -> None:
    result, log = run(tmp_path, confirm="no\n")
    assert result.returncode == 1
    assert "not confirmed" in result.stderr
    assert "bellasreef backup" in log
    assert "down" not in log and "volume rm" not in log and "systemctl stop" not in log


def test_a_failed_backup_aborts_with_nothing_touched(tmp_path: Path) -> None:
    result, log = run(tmp_path, backup_rc=1)
    assert result.returncode == 1
    assert "backup failed" in result.stderr
    assert "systemctl stop" not in log


def test_destroys_in_order_and_redeploys_through_the_boot_unit(tmp_path: Path) -> None:
    result, log = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [ln for ln in log.splitlines() if ln]
    stop = next(i for i, ln in enumerate(lines) if ln.startswith("sudo systemctl stop bellasreef"))
    down = next(i for i, ln in enumerate(lines) if " down" in ln)
    rm = next(i for i, ln in enumerate(lines) if "volume rm" in ln)
    migrate = next(i for i, ln in enumerate(lines) if "alembic upgrade head" in ln)
    start = next(
        i for i, ln in enumerate(lines) if ln.startswith("sudo systemctl start bellasreef")
    )
    assert stop < down < rm < migrate < start, lines
    assert "systemctl restart" not in log
    assert "7KF2-9QMD" in result.stdout


def test_a_missing_volume_is_not_found_rather_than_removed_nothing(tmp_path: Path) -> None:
    result, log = run(tmp_path, volumes="bellasreef_nats-data\nbellasreef_postgres-data")
    assert result.returncode == 1
    assert "bellasreef_vm-data" in result.stderr and "not found" in result.stderr
    assert "volume rm" not in log


def test_post_reset_assertions_fail_the_run(tmp_path: Path) -> None:
    result, _ = run(tmp_path, info=INFO_PAIRED)
    assert result.returncode == 1 and "paired_client_count=1" in result.stderr
    result, _ = run(tmp_path, psql="3\n0")
    assert result.returncode == 1 and "3 device(s)" in result.stderr
    result, _ = run(tmp_path, alembic="0019")
    assert result.returncode == 1 and "not at head" in result.stderr
    result, _ = run(tmp_path, hw_log='{"msg":"stream created"}')
    assert result.returncode == 1 and "1 of 7" in result.stderr
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_factory_reset_hub.py -q` → FAIL (script not found).

- [ ] **Step 3: Write the script**

```bash
#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
#
# Factory-reset this hub. Runs ON the hub, from the bellasreef-hub clone.
#
# Destroys: the docker volumes bellasreef_postgres-data, bellasreef_vm-data,
# bellasreef_nats-data — every pairing, every device, the audit log, all
# telemetry history. Keeps: the pre-reset backup (mandatory, taken first),
# this checkout, deploy/.env, the images, /boot/firmware.
#
# Sanctioned exception to "spine data services are never recreated" (see the
# hub docs): one deliberate, typed-confirmation wipe. Order matters: a
# stopped-but-not-removed container still pins its volume (measured
# 2026-08-14), so the unit stops and the stack comes down before the volumes
# will release. The redeploy goes through `systemctl start bellasreef.service`
# so a reset also proves the power-cut path.
#
# Usage: ./scripts/factory-reset-hub.sh            (prompts for the word)
#        echo factory-reset | ./scripts/factory-reset-hub.sh
#
# IH_ROOT / FR_*_SECS are test seams, empty/default in production.
set -uo pipefail

usage() {
    cat <<USAGE
Usage: ./scripts/factory-reset-hub.sh

Factory-resets THIS hub.

DESTROYS:
  - docker volumes: bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data
  - every pairing, every device, the audit log, ALL telemetry history

A pre-reset backup is mandatory and is taken first. Destruction additionally
requires typing 'factory-reset' at a prompt once the backup has completed.
Takes no other flags by design.

  -h, --help   print this message and exit
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done

IH_ROOT="${IH_ROOT:-}"
FR_API_DEADLINE_SECS="${FR_API_DEADLINE_SECS:-30}"
FR_STREAM_DEADLINE_SECS="${FR_STREAM_DEADLINE_SECS:-60}"
FR_POLL_SECS="${FR_POLL_SECS:-3}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_DIR}/deploy/compose.yaml"
ENV_FILE="${IH_ROOT}${REPO_DIR}/deploy/.env"
UNIT="${IH_ROOT}/etc/systemd/system/bellasreef.service"
API_INFO_URL="http://127.0.0.1:8000/api/v1/info"
VOLUMES=(bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data)

die()  { printf '\033[31mfactory-reset: %s\033[0m\n' "$1" >&2; exit 1; }
step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }

# </dev/null on every compose call: the confirmation prompt below is the only
# thing allowed to read stdin, and a docker exec that inherits it eats the
# word before the prompt sees it (the 2026-08-15 failure, over ssh then).
compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@" </dev/null; }

# ------------------------------------------------------------ 0. installed?
[[ -f "$UNIT" && -s "$ENV_FILE" ]] \
    || die "nothing to reset: no bellasreef.service or no deploy/.env on this machine — run scripts/install-hub.sh"
backup_dir="$(sed -n 's/^BELLASREEF_BACKUP_DIR=//p' "$ENV_FILE" | head -1)"

# ------------------------------------------------------------- 1. backup
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_NAME="bellasreef-pre-factory-${STAMP}.tar.gz"
step "taking pre-reset backup to ${backup_dir:-/backups}/${BACKUP_NAME}"
compose exec -T api bellasreef backup --out "/backups/${BACKUP_NAME}" \
    || die "backup failed; aborting with nothing touched"

# ------------------------------------------------------------- 2. consent
cat <<DOOM

About to DESTROY on this hub:
  - docker volumes: ${VOLUMES[*]}
  - every pairing, every device, the audit log, ALL telemetry history

Pre-reset backup: ${backup_dir:-/backups}/${BACKUP_NAME}
DOOM
read -r -p "Type 'factory-reset' to proceed: " confirm || confirm=""
[[ "$confirm" == "factory-reset" ]] || die "not confirmed; nothing touched"

# ------------------------------------------------------- 3. stop, down, wipe
step "stopping bellasreef.service"
sudo systemctl stop bellasreef.service || die "could not stop bellasreef.service; nothing removed"
step "bringing the stack down"
compose down || die "compose down failed; volumes not touched — check container state before retrying"
step "removing the three data volumes"
existing="$(docker volume ls -q 2>/dev/null)"
for v in "${VOLUMES[@]}"; do
    grep -qx "$v" <<<"$existing" || die "volume ${v} not found — a project-name mismatch, not a clean removal; check 'docker volume ls' before retrying"
done
docker volume rm "${VOLUMES[@]}" \
    || die "volume removal failed — the stack is down but one or more volumes may remain; check 'docker volume ls' before retrying"

# --------------------------------------------------------- 4. redeploy clean
step "migrating the empty database"
compose run --rm api sh -c 'cd /app/db && alembic upgrade head' \
    || die "migrations failed after the wipe — the hub has NO data volumes and is not running; do not treat this as a completed reset"
step "starting bellasreef.service (the boot unit brings the stack up)"
sudo systemctl start bellasreef.service \
    || die "bellasreef.service did not start after the wipe — the hub is not confirmed running"

# ------------------------------------------------------ 5. verify fresh state
step "verifying factory-fresh state"
deadline=$(( $(date +%s) + FR_API_DEADLINE_SECS ))
info=""
while :; do
    info="$(curl -fsS --max-time 10 "$API_INFO_URL" 2>/dev/null || true)"
    [[ -n "$info" ]] && break
    (( $(date +%s) >= deadline )) && break
    sleep "$FR_POLL_SECS"
done
[[ -n "$info" ]] || die "GET ${API_INFO_URL} did not answer within ${FR_API_DEADLINE_SECS}s — cannot confirm factory-fresh state"
paired="$(sed -n 's/.*"paired_client_count":\([0-9]*\).*/\1/p' <<<"$info")"
setup_mode="$(sed -n 's/.*"setup_mode":\([a-z]*\).*/\1/p' <<<"$info")"
[[ "$paired" == "0" ]] || die "post-reset /info reports paired_client_count=${paired:-<absent>} — expected 0; the reset did not clear pairings"
[[ "$setup_mode" == "true" ]] || die "post-reset /info reports setup_mode=${setup_mode:-<absent>} — expected true; setup mode did not reopen"
echo "  0 paired clients, setup mode open"

step "confirming devices and audit log are empty"
if psql_out="$(compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM devices; SELECT count(*) FROM audit_log;"' 2>&1)"; then
    device_count="$(sed -n '1p' <<<"$psql_out")"
    audit_count="$(sed -n '2p' <<<"$psql_out")"
    [[ "$device_count" == "0" ]] || die "the reset left ${device_count:-<unreadable>} device(s) behind"
    [[ "$audit_count" == "0" ]] || die "the reset left ${audit_count:-<unreadable>} audit_log row(s) behind"
    echo "  0 devices, 0 audit_log rows"
else
    warn "could not query devices/audit_log counts (${psql_out}); check manually with: docker compose exec -T postgres psql -U \$POSTGRES_USER -d \$POSTGRES_DB -c 'SELECT count(*) FROM devices;'"
fi

step "confirming alembic is at head"
if alembic_out="$(compose exec -T api sh -c 'cd /app/db && alembic current' 2>&1)"; then
    [[ "$alembic_out" == *"(head)"* ]] || die "alembic current reports '${alembic_out}' — not at head after the redeploy"
    echo "  alembic at head"
else
    warn "could not check the alembic revision (${alembic_out})"
fi

# Seven JetStream streams (BR_CMD, BR_STATE, BR_REGISTRY, BR_CAPABILITY,
# BR_CHIP, BR_ASSIGNMENT, BR_AUDIT) are provisioned by hardware-io at start;
# against a wiped nats-data volume every one logs "stream created".
step "confirming all seven JetStream streams were recreated"
deadline=$(( $(date +%s) + FR_STREAM_DEADLINE_SECS ))
stream_count=0
while :; do
    stream_count="$(docker logs bellasreef-hardware-io-1 2>&1 | grep -c '"msg":"stream created"' || true)"
    stream_count="${stream_count:-0}"
    (( stream_count >= 7 )) && break
    (( $(date +%s) >= deadline )) && break
    sleep "$FR_POLL_SECS"
done
(( stream_count >= 7 )) || die "hardware-io logged ${stream_count} of 7 expected 'stream created' lines within ${FR_STREAM_DEADLINE_SECS}s — JetStream provisioning did not complete"
echo "  all 7 JetStream streams recreated"

# Watch item (2026-08-30): a hub with no hardware attached may legitimately
# announce nothing under the inventory-only ruling. Kept strict until coco
# shows otherwise; if it does, this becomes "announced N (0 is fine with no
# hardware)" rather than a die.
step "checking hardware-io capability announcements"
announced="$(docker logs bellasreef-hardware-io-1 2>&1 | grep -c '"msg":"capability announced"' || true)"
(( ${announced:-0} >= 1 )) || die "hardware-io logged no capability announcements — discovery did not run"
echo "  hardware-io announced ${announced} capability line(s)"

# --------------------------------------------------- 6. mint the final code
# `bellasreef setup-code` ROTATES (stores only a hash); this is the one code
# this run prints and the last thing on screen.
step "minting the setup code"
compose exec -T api bellasreef setup-code \
    || warn "could not mint the setup code — mint one with: docker compose -f deploy/compose.yaml --env-file deploy/.env exec -T api bellasreef setup-code"
echo "Use the code directly above. Then re-import devices (see scripts/install-hub.sh's hand-off)."
```
`chmod +x`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_factory_reset_hub.py -q`
Expected: 7 passed. If the `compose()` argument-skipping loop in the docker stub misparses, fix the stub, not the script: the script's compose calls are always `docker compose -f <file> --env-file <file> <verb> …`.

- [ ] **Step 5: Delete the workstation scripts and fix the comments**

```bash
git rm scripts/factory-reset-pi.sh scripts/deploy-pi.sh
```
`scripts/check.sh` line ~118: replace the paragraph starting "deploy-pi.sh renders the value from the installed package" with:
```
# install-hub.sh installs this file verbatim, so the committed value is what
# every hub advertises. This check keeps it equal to the installed package.
```
`hub/deploy/avahi/bellasreef.service`: replace the sentence about `scripts/deploy-pi.sh rewrites it` with `scripts/install-hub.sh installs this file as it is; scripts/check.sh fails if the value stops matching the bellasreef-contracts package.`
`hub/deploy/compose.yaml` line ~19: `BELLASREEF_TAG is written into deploy/.env by install-hub.sh from deploy/release.env`.
Add `"scripts/factory-reset-hub.sh"` to `EXPECTED` in `tests/test_build_hub_repo.py`.

- [ ] **Step 6: Gate, commit, PR 3**

```bash
uv run pytest tests -q && BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh --quick
git add -A
git commit -m "feat(hub): on-Pi factory-reset-hub.sh; delete deploy-pi.sh and factory-reset-pi.sh (the hub is the only machine)"
git push -u origin feat/on-pi-lifecycle-scripts
gh pr create --title "feat(hub): on-Pi lifecycle scripts — factory-reset-hub.sh, update-hub.sh skeleton; workstation tools deleted" --body "Part 3 of 4 for docs/superpowers/specs/2026-08-30-hub-repo-split-and-install-design.md — §6, §7, §8. deploy-pi.sh 'should never have existed' (ruled 2026-08-30).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01WEwWri4eyKH53yQkHrDbzK"
gh pr checks --watch && gh pr merge --squash --delete-branch && git checkout main && git pull
```

---

## PR 4 — docs, then the release

Branch: `git checkout -b docs/hub-repo-and-deploy-discipline`.

### Task 15: `hub/README.md` — the install guide; fold in `hub-prereqs.md`

**Files:**
- Create: `hub/README.md`
- Delete: `docs/hub-prereqs.md`
- Modify: `tests/test_build_hub_repo.py` (`EXPECTED` += `"README.md"`)

- [ ] **Step 1: Write the README**

Content, in this order, plain prose (use the `voice` skill for the prose):

1. Title `# Bella's Reef hub` and one paragraph: this repository is what a hub runs from; it is generated from the development repository on each release and is never edited by hand; the current release is in `deploy/release.env`.
2. `## What you need` — a Raspberry Pi 5 (or another arm64/amd64 Linux 6.x board; see `docs/hub-platform-requirements.md`), 64-bit OS, 16 GB storage practical minimum, network, a phone with the app. One line: memory — the installer warns below 2 GB; measurements are being collected.
3. `## Install` — the four commands verbatim: `sudo apt install -y git` (if missing), `git clone https://github.com/viperdavethesnake/bellasreef-hub.git ~/bellasreef`, `cd ~/bellasreef`, `./scripts/install-hub.sh`. Then what the phases do, one line each, from `install-hub.sh --help`. Say plainly: the installer offers Docker, the docker group (log out and back in when told), avahi's service record and interface allowlist, and docker log rotation; it asks before each.
4. `## While the images are private` — `docker login ghcr.io -u <github-username>` with a `read:packages` PAT, before the last command. "This section is deleted when the packages go public."
5. `## After the install` — pair the phone with the setup code; copy `deploy/config/devices.yaml.example` to `/etc/bellasreef/devices.import.yaml`, edit, import with the command the installer printed.
6. `## Later` — `scripts/update-hub.sh` (not yet implemented; says so when run), `scripts/factory-reset-hub.sh` (what it destroys, that it backs up first, that it asks for the word).
7. `## If you get locked out` — the `bellasreef pair` / `revoke` block from the dev README, with `cd ~/bellasreef` and the `br` alias using `deploy/compose.yaml --env-file deploy/.env`.
8. `## Docs` — links to the three files under `docs/`.
9. `## Licence` — AGPL-3.0-only, `LICENSE`; source at `https://github.com/viperdavethesnake/bellas-reef`.

Log rotation, backups dir, `/etc/bellasreef`, the repo clone path — all previously hand steps in `hub-prereqs.md` — are now done by the installer and are described as such, not as steps.

- [ ] **Step 2: Delete `docs/hub-prereqs.md`; fix inbound links**

`git rm docs/hub-prereqs.md`; `grep -rn "hub-prereqs" --include=*.md . | grep -v superpowers` → re-point each to `hub/README.md`.

- [ ] **Step 3: Test, commit**

```bash
uv run pytest tests/test_build_hub_repo.py -q
git add -A
git commit -m "docs(hub): README is the install guide; hub-prereqs folded in"
```

### Task 16: CLAUDE.md, dev README, moved docs

**Files:**
- Modify: `CLAUDE.md` ("Deployment discipline", "Dev environment", the `w1-gpio-pi5` paragraph), `README.md` (layout table, lockout block path, status), `hub/docs/host-setup.md` and `hub/docs/backup-restore.md` (every `deploy-pi.sh`, `factory-reset-pi.sh`, `deploy/compose.yaml`-relative-to-dev-root, `hub-prereqs.md` reference), `deploy/compose.drill.yaml:5` comment.

- [ ] **Step 1: CLAUDE.md "Deployment discipline"**

Replace the bullet beginning "Deploy with `scripts/deploy-pi.sh`." and the bullet "**A backend pass is not done at CI green.**" with:

```markdown
- **The hub is the only machine.** A user clones `bellasreef-hub` on the Pi
  and runs `scripts/install-hub.sh` there; updates are
  `scripts/update-hub.sh` on the Pi; a reset is `scripts/factory-reset-hub.sh`
  on the Pi. There is no workstation-side deploy tool and there must never be
  one again: `deploy-pi.sh` (2026-08-12 → 2026-08-30) encoded the Mac→dev-Pi
  loop as if it were the product and was deleted by ruling ("it should never
  have existed"). `bellasreef-hub` is generated from this repo's `hub/` by
  `.github/workflows/release.yaml` on every `v*` tag and is never edited by
  hand; `scripts/build-hub-repo.sh` is the one assembler.
- **A backend pass is not done at CI green.** The stop condition is
  **CI green → `v*` tag → release workflow green → `update-hub.sh` on the hub
  → telemetry verified on the wire.** All of it, every time. Until
  `update-hub.sh` is implemented, the last two are a fresh
  `install-hub.sh` from the new release on a machine that is not yet a hub,
  or the documented manual steps — never a hand-applied fix around a script
  failure ("if the script fails, we fail", 2026-08-30).
```
In the same section, the factory-reset sentence: `scripts/factory-reset-pi.sh` → `scripts/factory-reset-hub.sh`, "run on the hub". In "Dev environment": "Deploy with `scripts/deploy-pi.sh`." → "Releases: tag `v*` on main; the hub installs/updates itself from `bellasreef-hub`."

- [ ] **Step 2: CLAUDE.md `w1-gpio-pi5` paragraph**

Append to the "**Pi 5 overlay name:**" paragraph:
```markdown
Observed on the dev Pi only. On coco-bellasreef (2026-08-30, same kernel
string) the plain `w1-gpio,gpiopin=4` brought up `w1_bus_master1` on RP1
GPIO4 (dmesg `gpio-573 (onewire@4)`); whether a probe reads through it is
unmeasured. Ruled: the installer reports what the kernel exposes and never
prescribes an overlay name; probes are optional.
```

- [ ] **Step 3: Dev README**

Layout table: add `| hub/ | Exactly what a hub runs from — generated into bellasreef-hub on each release |`; `deploy/` row → "Dockerfiles, bench drill override, custom overlay source". Lockout block: `cd ~/bellasreef` and `-f deploy/compose.yaml --env-file deploy/.env` (that is the hub-repo layout; note "on the hub, in the bellasreef-hub clone"). Status paragraph: "Pre-release. `v0.2.0-rc.*` tags; hubs install from `bellasreef-hub`."

- [ ] **Step 4: Moved docs**

In `hub/docs/host-setup.md` and `hub/docs/backup-restore.md`: `grep -n "deploy-pi\|factory-reset-pi\|hub-prereqs\|/home/david/bellasreef" hub/docs/*.md` and fix each: script names → the on-Pi ones; `/home/david/bellasreef` → `~/bellasreef`; `hub-prereqs.md` → `../README.md`. Historical dated paragraphs that *describe* deploy-pi.sh as a past event keep the name with "(deleted 2026-08-30)". `deploy/compose.drill.yaml:5`: `scripts/drill-restart.sh names hub/deploy/compose.yaml`.

- [ ] **Step 5: Gate, commit, PR 4**

```bash
BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh --quick
git add -A
git commit -m "docs: deployment discipline around on-Pi scripts and bellasreef-hub; w1 overlay note; moved operator docs re-pointed"
git push -u origin docs/hub-repo-and-deploy-discipline
gh pr create --title "docs: hub repo, on-Pi deploy discipline, install guide" --body "Part 4 of 4 for docs/superpowers/specs/2026-08-30-hub-repo-split-and-install-design.md — §9 docs, the spec and this plan committed.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01WEwWri4eyKH53yQkHrDbzK"
gh pr checks --watch && gh pr merge --squash --delete-branch && git checkout main && git pull
```
(The spec and this plan are untracked until this PR; `git add -A` in Task 1 would have picked them up — either is fine, they are docs. If Task 1 committed them, this step just carries edits.)

### Task 17: Release `v0.2.0-rc.2`, publish the hub repo, install on coco

**Interfaces:**
- Consumes: everything above merged to `main`; `ci.yaml` has published images for the merge commit.

- [ ] **Step 1: Create the hub repo (private)**

```bash
gh repo create viperdavethesnake/bellasreef-hub --private --description "Bella's Reef hub — what a hub runs from. Generated on each release; do not edit." --disable-wiki
```

- [ ] **Step 2: Confirm images exist for main's HEAD, then tag**

```bash
git checkout main && git pull
SHA="$(git rev-parse HEAD)"
gh api "/users/viperdavethesnake/packages/container/bellasreef-api/versions?per_page=5" --jq '.[].metadata.container.tags[]' | grep -q "$SHA" || echo "WAIT: CI has not published $SHA yet"
git tag -a v0.2.0-rc.2 -m "Bella's Reef 0.2.0-rc.2 — bellasreef-hub, on-Pi lifecycle scripts, installer fixes from coco's maiden run"
git push origin v0.2.0-rc.2
gh run watch "$(gh run list --workflow release.yaml --limit 1 --json databaseId --jq '.[0].databaseId')"
```
Expected: retag, assemble, attach succeed; **publish fails** naming `HUB_REPO_TOKEN` (David has not created it). That failure is expected and reported as such.

- [ ] **Step 3: Publish the hub repo from the Mac (same assembler)**

```bash
OUT="$(mktemp -d)/hub"
scripts/build-hub-repo.sh "$OUT" v0.2.0-rc.2 "$SHA"
git clone https://github.com/viperdavethesnake/bellasreef-hub.git "$OUT-repo"
cd "$OUT-repo" && cp -R "$OUT/." . && git add -A
git commit -m "release v0.2.0-rc.2 from bellas-reef@${SHA}"
git tag v0.2.0-rc.2 && git push origin HEAD:main v0.2.0-rc.2
cd - >/dev/null
```

- [ ] **Step 4: On coco — clone and install**

David does `docker login ghcr.io` on coco first (his ruling; his credential). Then:
```bash
ssh -o BatchMode=yes david@192.168.33.105 'test -e /home/david/bellasreef && echo "STOP: old clone still present" || (git clone -q https://github.com/viperdavethesnake/bellasreef-hub.git /home/david/bellasreef && cd /home/david/bellasreef && git log --oneline -1 && cat deploy/release.env)'
```
(The hub repo is private: cloning needs coco's `gh auth setup-git` or an HTTPS credential — if the clone is refused, that is the finding; David's `gh` on coco is logged in and `gh auth setup-git` makes git use it.)
```bash
ssh -o BatchMode=yes david@192.168.33.105 'cd /home/david/bellasreef && ./scripts/install-hub.sh --yes 2>&1 | sed "s/\x1b\[[0-9;]*m//g"; echo "exit=${PIPESTATUS[0]}"'
```
Report the transcript verbatim. If it fails: stop, report, no hand fixes. If it passes: the identity block, the setup code (do not paste the code into any memory), and the two watch items — `docker logs bellasreef-hardware-io-1 | grep -c "capability announced"` and whether `discover_pwm` degraded cleanly on a Pi 5 with no pwmchip.

- [ ] **Step 5: Memory and report**

Update `coco-post-deploy-review-items` with the outcome and the remaining David items (`HUB_REPO_TOKEN`, dev-repo visibility, `update-hub.sh` main-tracking); update `bellasreef-current-state`; report to David.
