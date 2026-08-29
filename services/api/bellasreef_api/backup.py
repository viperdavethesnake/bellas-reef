# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Backup and restore (PRD R14).

One command produces one file. Inside it: a ``pg_dump`` of the whole database
and a manifest describing what the dump is, what hub it came from, and — the
part that matters most — **what is deliberately not in here**.

That last list is the reason this module is shaped the way it is. A backup that
quietly excludes telemetry is indistinguishable, six months later on new
hardware, from a backup that lost it. The operator restores, sees no history,
and cannot tell whether the data was never captured or was captured and
dropped. So absence is written down: every omission carries what, why, and how
to get it anyway. An archive that cannot say what it left out is not a backup,
it is a hope.

Restore is built around a single ordering rule:

    **The database is not touched until the archive has been proven whole.**

Digest, size, manifest shape, manifest version and schema revision are all
checked while the target is still untouched, and every failure raises
:class:`RestoreRefusedError` with a stable, named reason. There is no path through
this module that half-restores and reports success — the actual load runs
inside ``--single-transaction --exit-on-error``, so Postgres itself makes the
final step all-or-nothing.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from urllib.parse import quote
from uuid import UUID

import httpx
from bellasreef_db.revisions import KNOWN_REVISIONS
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

__all__ = [
    "ARCHIVE_MODE",
    "DUMP_NAME",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "PG_BIN_ENV",
    "BackupError",
    "BackupResult",
    "Contains",
    "DumpInfo",
    "HubIdentity",
    "Manifest",
    "Omission",
    "RestoreRefusedError",
    "TelemetrySnapshot",
    "VerifiedArchive",
    "create_backup",
    "open_archive",
    "pg_tools_available",
    "restore_backup",
    "write_archive",
]

#: Directory holding ``pg_dump``/``pg_restore`` when they are not on ``PATH``.
#: This is not ceremony: Homebrew's ``libpq`` is keg-only, so on a macOS dev
#: machine the tools exist and ``PATH`` does not have them.
PG_BIN_ENV: Final = "BELLASREEF_PG_BIN"

MANIFEST_NAME: Final = "manifest.json"
DUMP_NAME: Final = "postgres.dump"

#: The archive is owner-read/write and nothing else, from the instant it exists.
#:
#: The dump covers the whole database, which includes ``signing_keys.secret``
#: and every paired client id — so the file is not a copy of your tank's
#: settings, it is a credential that mints a valid JWT for any client. On a
#: shared box a 0644 archive hands that to every account on it. Encryption was
#: considered and deliberately not built (see the design's accepted-risk table:
#: this is a file on the operator's own machine, handled like a password-manager
#: export), which makes the mode the only thing standing between the archive and
#: anyone else with a shell.
ARCHIVE_MODE: Final = 0o600

#: Bump only when the archive layout changes in a way an older restore cannot
#: read. Adding a field is not that — unknown manifest fields are ignored, so a
#: newer hub's extra metadata does not make its archive unreadable here. What
#: does make it unreadable is a schema revision this binary has never heard of,
#: and that is reported as exactly that rather than as a parse failure.
MANIFEST_VERSION: Final = 1

#: Read in 1 MiB chunks so a large dump is hashed and written without ever
#: being held in memory whole.
_CHUNK = 1024 * 1024


class BackupError(Exception):
    """A backup could not be produced. No archive is written."""


class RestoreRefusedError(Exception):
    """Restore stopped before touching the database.

    ``reason`` is a stable slug and part of the operator-facing contract — it
    is what scripts match on and what an operator recognises the second time
    they see it. ``detail`` is the human sentence and may change freely.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


# ------------------------------------------------------------------- manifest


class _Strict(BaseModel):
    # Unknown fields are ignored rather than rejected: an archive written by a
    # newer hub should fail on the schema-revision gate, which can explain
    # itself, not on a field name this build has not learned yet.
    model_config = ConfigDict(extra="ignore")


class HubIdentity(_Strict):
    """Which tank this came from — recorded from two angles, because neither
    one is sufficient on its own.

    ``database_host`` is the Postgres server exactly as the DSN addressed it.
    A loopback name (``localhost``, ``127.0.0.1``, a tunnel) identifies
    nothing; a network name like ``bellasreef.local`` identifies everything.
    Which one the on-hub flow produces is a fact about the api container's
    configured ``BELLASREEF_DATABASE_URL``, not about this code.

    ``taken_on`` is the machine that ran the tool, and has the mirror-image
    problem: on the hub it is the hub, from a laptop it is the laptop.

    ``hub_id`` is the one that settles it: a UUID written once at first boot
    and restored along with everything else, so an archive names the hub rather
    than the circumstances of its own creation. Optional only for reading
    archives written before the identity table existed — a manifest without one
    is old, not wrong.
    """

    #: The hub's own identity row, written at first boot and carried through a
    #: restore with the rest of the data. This is the identifier that actually
    #: distinguishes two hubs; the three below are corroboration.
    hub_id: UUID | None = None

    database_host: str
    database: str
    postgres_version: str
    taken_on: str


class DumpInfo(_Strict):
    file: str
    format: str
    sha256: str
    bytes: int
    pg_dump_version: str


class TelemetrySnapshot(_Strict):
    """The VictoriaMetrics side, which is a pointer and not a payload.

    ``/snapshot/create`` gives a consistent, hardlinked view *inside the VM data
    volume*. It is not a portable file and this process has no access to that
    volume, so the archive records where the snapshot is rather than pretending
    to contain it. See the matching entry in :attr:`Manifest.omissions`.
    """

    taken: bool
    snapshot: str | None = None
    vm_url: str | None = None
    note: str


class Omission(_Strict):
    """One thing this archive does not contain, and how to get it anyway."""

    what: str
    why: str
    recover: str


class Contains(_Strict):
    """One thing this archive *does* contain that makes the file itself sensitive.

    The mirror of :class:`Omission`, and the more important of the two. The
    omissions list has always been here because absence is invisible; presence
    turned out to be invisible in the other direction. An operator who reads a
    manifest that carefully enumerates what is missing will reasonably conclude
    that what is present is the boring part, and copy the file to a laptop, a
    USB stick, or a cloud drive.
    """

    what: str
    why: str
    handling: str


class Manifest(_Strict):
    manifest_version: int
    created_at: datetime
    hub: HubIdentity
    schema_revision: str
    contracts_version: str
    tool_version: str
    postgres: DumpInfo
    telemetry: TelemetrySnapshot
    omissions: list[Omission]
    #: Defaulted rather than required so archives written before this field
    #: existed still read. They are old, not wrong — the same reasoning as
    #: ``hub_id``, and the reason adding a field is not a MANIFEST_VERSION bump.
    contains: list[Contains] = Field(default_factory=list)


# -------------------------------------------------------------------- writing


def write_archive(path: Path, *, manifest_json: bytes, dump_path: Path) -> None:
    """Assemble the archive. Manifest first, so a reader meets it first.

    The file is created at :data:`ARCHIVE_MODE` and never exists at any other
    mode. ``tarfile.open(path, ...)`` would create it through the process umask,
    which on a stock Debian or macOS account is 0644 — and chmod-after-write
    leaves a window, for the whole duration of a ``pg_dump`` of the entire
    database, in which the most sensitive file this project produces is readable
    by every account on the box. So the descriptor is opened here with the mode
    it must have, and the tar is written into it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ARCHIVE_MODE)
    try:
        # O_CREAT's mode applies only when the open actually creates the file.
        # Overwriting last week's 0644 archive would otherwise keep its mode,
        # which is the case most likely to happen on a real hub, where backups
        # are written to the same path on a schedule.
        os.fchmod(descriptor, ARCHIVE_MODE)
        handle = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise

    with handle, tarfile.open(fileobj=handle, mode="w:gz") as tar:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_json)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(manifest_json))
        tar.add(dump_path, arcname=DUMP_NAME)


# -------------------------------------------------------------------- reading


@dataclass(frozen=True, slots=True)
class VerifiedArchive:
    """An archive that has passed every check restore makes before writing."""

    manifest: Manifest
    dump_path: Path


def open_archive(archive: Path, workdir: Path) -> VerifiedArchive:
    """Verify an archive and unpack its dump into ``workdir``.

    Raises :class:`RestoreRefusedError` for anything wrong, and touches no database
    either way. Members are read by exact name — never ``extractall`` — so a
    crafted path in the tar cannot write outside ``workdir``; it simply is not
    one of the two names looked up.
    """
    if not archive.is_file():
        raise RestoreRefusedError("archive-missing", f"no archive at {archive}")

    workdir.mkdir(parents=True, exist_ok=True)
    dump_path = workdir / DUMP_NAME

    try:
        with tarfile.open(archive, "r:gz") as tar:
            manifest = _read_manifest(tar)
            _extract_dump(tar, manifest, dump_path)
    except RestoreRefusedError:
        raise
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as exc:
        # Truncation lands here: gzip raises reading past the end of a stream
        # that never got its end-of-stream marker.
        raise RestoreRefusedError(
            "archive-unreadable", f"{archive} is not a readable backup archive: {exc}"
        ) from exc

    _check_schema_revision(manifest)
    return VerifiedArchive(manifest=manifest, dump_path=dump_path)


def _read_manifest(tar: tarfile.TarFile) -> Manifest:
    try:
        member = tar.getmember(MANIFEST_NAME)
    except KeyError as exc:
        raise RestoreRefusedError(
            "manifest-missing", f"the archive contains no {MANIFEST_NAME}"
        ) from exc

    handle = tar.extractfile(member)
    if handle is None:
        raise RestoreRefusedError("manifest-missing", f"{MANIFEST_NAME} is not a regular file")

    try:
        raw = json.loads(handle.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RestoreRefusedError(
            "manifest-unreadable", f"{MANIFEST_NAME} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise RestoreRefusedError("manifest-unreadable", f"{MANIFEST_NAME} is not a JSON object")

    # Version before shape. A future layout would fail validation too, but
    # "written by a newer version" is a far more useful thing to be told than
    # a list of fields that moved.
    version = raw.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise RestoreRefusedError(
            "manifest-version-unsupported",
            f"archive manifest is version {version!r}; this build reads version {MANIFEST_VERSION}",
        )

    try:
        return Manifest.model_validate(raw)
    except ValidationError as exc:
        raise RestoreRefusedError(
            "manifest-incomplete", f"{MANIFEST_NAME} is missing or malformed: {exc}"
        ) from exc


def _extract_dump(tar: tarfile.TarFile, manifest: Manifest, dump_path: Path) -> None:
    try:
        member = tar.getmember(DUMP_NAME)
    except KeyError as exc:
        raise RestoreRefusedError(
            "payload-missing", f"the archive contains no {DUMP_NAME}"
        ) from exc

    handle = tar.extractfile(member)
    if handle is None:
        raise RestoreRefusedError("payload-missing", f"{DUMP_NAME} is not a regular file")

    digest = hashlib.sha256()
    written = 0
    with dump_path.open("wb") as out:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            written += len(chunk)
            out.write(chunk)

    if written != manifest.postgres.bytes:
        raise RestoreRefusedError(
            "payload-corrupt",
            f"{DUMP_NAME} is {written} bytes; the manifest recorded {manifest.postgres.bytes}",
        )
    if digest.hexdigest() != manifest.postgres.sha256:
        raise RestoreRefusedError(
            "payload-corrupt",
            f"{DUMP_NAME} does not match the sha256 the manifest recorded — the "
            "archive is damaged and must not be restored",
        )


# ------------------------------------------------------ the PostgreSQL tools


def _pg_binary(name: str, pg_bin: Path | None = None) -> Path:
    """Locate ``pg_dump``/``pg_restore``. An explicit directory wins over PATH."""
    override = pg_bin
    if override is None and (env := os.environ.get(PG_BIN_ENV)):
        override = Path(env)

    candidates: list[Path] = []
    if override is not None:
        candidates.append(override / name)
    if (found := shutil.which(name)) is not None:
        candidates.append(Path(found))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    raise BackupError(
        f"{name} not found. Install the PostgreSQL client tools, or point "
        f"{PG_BIN_ENV} at the directory holding them. Their major version must be "
        "at least the server's — an older pg_dump refuses to dump a newer server."
    )


def pg_tools_available(pg_bin: Path | None = None) -> bool:
    """Whether both tools can be located. Used to gate tests, not to fall back."""
    try:
        _pg_binary("pg_dump", pg_bin)
        _pg_binary("pg_restore", pg_bin)
    except BackupError:
        return False
    return True


def _libpq_target(dsn: str) -> tuple[str, dict[str, str]]:
    """Turn a SQLAlchemy DSN into a libpq URI plus the environment for it.

    The password goes in ``PGPASSWORD`` rather than into the URI, because the
    URI becomes an argument vector and argument vectors are world-readable in
    ``ps``. Backing up a hub should not broadcast its database password to
    every process on the box.
    """
    url = make_url(dsn)
    user = quote(url.username or "", safe="")
    host = url.host or "localhost"
    port = url.port or 5432
    database = url.database or ""
    credential = f"{user}@" if user else ""
    uri = f"postgresql://{credential}{host}:{port}/{database}"
    env = {"PGPASSWORD": url.password} if url.password else {}
    return uri, env


async def _run_pg_tool(
    binary: Path, args: list[str], env_extra: dict[str, str]
) -> tuple[int, str, str]:
    """Run a client tool. Returns ``(exit_code, stdout, stderr)``."""
    process = await asyncio.create_subprocess_exec(
        str(binary),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env_extra},
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


# ------------------------------------------------------------- what is present


def _contains() -> list[Contains]:
    """The things in this archive that make the archive itself a credential.

    Short on purpose. This list is not an inventory of the database — it is the
    set of facts that change how the file must be handled, and a list that grows
    to twenty entries is a list nobody reads.
    """
    return [
        Contains(
            what="the JWT signing secret and every paired client id",
            why=(
                "the dump covers the whole database, and signing_keys.secret is in it in "
                "plaintext. Anyone holding this file can mint a valid access token for any "
                "client of the hub it came from. It is not encrypted: for a home hub this is "
                "a file on your own machine, so it is mode-restricted and labelled rather "
                "than wrapped in a key you would also have to back up somewhere."
            ),
            handling=(
                "treat it like a password-manager export. Written 0600 and kept that way; "
                "do not put it on shared storage or in a chat. There is no signing-key "
                "rotation, so a leaked archive cannot be remediated after the fact — "
                "restoring elsewhere and revoking clients does not invalidate it."
            ),
        ),
    ]


# -------------------------------------------------------------- what is missing


def _omissions(snapshot: str | None) -> list[Omission]:
    """Everything this archive knowingly does not contain.

    Written out in full rather than summarised, because the operator reading it
    is by definition looking at a hub that no longer exists. "Telemetry not
    included" is a sentence they can act on; silence is not.
    """
    telemetry_recover = (
        "the snapshot is already taken — copy it out of the volume with:\n"
        f'      docker run --rm -v bellasreef_vm-data:/storage -v "$PWD":/out alpine \\\n'
        f"        tar czf /out/vm-{snapshot}.tar.gz -C /storage/snapshots {snapshot}"
        if snapshot
        else "no snapshot was taken for this archive; telemetry history is not recoverable from it"
    )
    return [
        Omission(
            what="telemetry samples (VictoriaMetrics)",
            why=(
                "/snapshot/create gives a consistent, hardlinked view inside the vm-data "
                "volume. It is not a portable file, and this process has no access to that "
                "volume — so the archive records where the snapshot is rather than "
                "pretending to carry it."
            ),
            recover=telemetry_recover,
        ),
        Omission(
            what="NATS JetStream state (streams, durable consumers, queued commands)",
            why=(
                "Deliberate, and a safety decision rather than a convenience one. BR_CMD "
                "holds actuator commands; restoring a stale one would replay actuation "
                "against a tank whose state nobody has looked at yet. Registrations are "
                "re-announced by hardware-io on boot, so the spine rebuilds itself."
            ),
            recover=(
                "nothing to do — the services provision their streams and re-announce "
                "their devices on startup"
            ),
        ),
        Omission(
            what="deployment secrets (deploy/.env)",
            why=(
                "The database password and host group IDs are deployment inputs, not hub "
                "state. An archive gets copied to laptops and USB sticks; a credential "
                "inside it is a credential leak waiting for one careless copy."
            ),
            recover="recreate from deploy/.env.example — see docs/host-setup.md",
        ),
        Omission(
            what="host configuration (config.txt overlays, chrony, avahi, systemd units)",
            why=(
                "Host mutation is documented rather than captured, and it is the ONE "
                "host-touching surface this project allows itself."
            ),
            recover="follow docs/host-setup.md on the new hardware before restoring",
        ),
        Omission(
            what="container images",
            why="pinned by digest in deploy/compose.yaml, which is in the git repository",
            recover="docker compose pull",
        ),
    ]


# ---------------------------------------------------------------------- backup


@dataclass(frozen=True, slots=True)
class BackupResult:
    archive: Path
    manifest: Manifest


async def _hub_facts(dsn: str) -> tuple[str, str, UUID | None]:
    """``(schema_revision, postgres_version, hub_id)`` straight from the database."""
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            version = str((await conn.execute(text("SHOW server_version"))).scalar_one()).split()[0]
            try:
                stamped = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).all()
                hub_row = (await conn.execute(text("SELECT id FROM hub_identity"))).first()
            except SQLAlchemyError as exc:
                raise BackupError(
                    "the database has no alembic_version table, so it has never been "
                    f"migrated and there is nothing coherent to back up: {exc}"
                ) from exc
    finally:
        await engine.dispose()

    if len(stamped) != 1:
        raise BackupError(
            f"alembic_version holds {len(stamped)} rows; a hub at a single, known schema "
            "revision is a precondition for a restorable backup"
        )
    # None only for a hub that has not started a service since migrating. Not
    # an error: the backup is still restorable, it just cannot name its origin
    # as precisely.
    hub_id = UUID(str(hub_row[0])) if hub_row is not None else None
    return str(stamped[0][0]), version, hub_id


async def _create_vm_snapshot(vm_url: str) -> str:
    """POST /snapshot/create. Verified against VictoriaMetrics v1.149."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(f"{vm_url}/snapshot/create")
    except httpx.HTTPError as exc:
        raise BackupError(f"VictoriaMetrics at {vm_url} could not be reached: {exc}") from exc

    if response.status_code != 200:
        raise BackupError(
            f"VictoriaMetrics refused the snapshot: HTTP {response.status_code} {response.text}"
        )
    body = response.json()
    if body.get("status") != "ok" or not body.get("snapshot"):
        raise BackupError(f"VictoriaMetrics returned an unusable snapshot response: {body!r}")
    return str(body["snapshot"])


async def create_backup(
    *,
    dsn: str,
    out: Path,
    vm_url: str | None,
    tool_version: str,
    contracts_version: str,
    pg_bin: Path | None = None,
) -> BackupResult:
    """Produce one restorable archive.

    ``vm_url=None`` means the telemetry snapshot is deliberately skipped, and
    the manifest says so in as many words. It is never inferred from a missing
    environment variable — the caller has to decide, so that "no telemetry" is
    always a choice somebody made rather than a variable somebody forgot.
    """
    pg_dump = _pg_binary("pg_dump", pg_bin)
    schema_revision, postgres_version, hub_id = await _hub_facts(dsn)
    snapshot = await _create_vm_snapshot(vm_url) if vm_url else None

    uri, env = _libpq_target(dsn)
    # The intermediate dump is as sensitive as the archive — same bytes, same
    # signing secret — and it exists for as long as pg_dump takes. It is safe
    # for the same reason the archive is: TemporaryDirectory creates the
    # directory 0700, so nothing under it is reachable by another account
    # regardless of what mode pg_dump gives its own output file.
    with tempfile.TemporaryDirectory(prefix="bellasreef-backup-") as scratch:
        dump_path = Path(scratch) / DUMP_NAME
        code, _, stderr = await _run_pg_tool(
            pg_dump,
            [
                "--format=custom",
                # Ownership and grants are deployment facts, not hub state, and
                # a restore onto fresh hardware may well run under a different
                # role name. Carrying them would make the dump refuse to load
                # for a reason that has nothing to do with the tank.
                "--no-owner",
                "--no-privileges",
                f"--file={dump_path}",
                f"--dbname={uri}",
            ],
            env,
        )
        if code != 0:
            raise BackupError(f"pg_dump failed (exit {code}): {stderr}")

        digest = hashlib.sha256()
        with dump_path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)

        _, dump_version, _ = await _run_pg_tool(pg_dump, ["--version"], {})
        manifest = Manifest(
            manifest_version=MANIFEST_VERSION,
            created_at=datetime.now(UTC),
            hub=HubIdentity(
                hub_id=hub_id,
                database_host=make_url(dsn).host or "",
                database=make_url(dsn).database or "",
                postgres_version=postgres_version,
                taken_on=socket.gethostname(),
            ),
            schema_revision=schema_revision,
            contracts_version=contracts_version,
            tool_version=tool_version,
            postgres=DumpInfo(
                file=DUMP_NAME,
                format="custom",
                sha256=digest.hexdigest(),
                bytes=dump_path.stat().st_size,
                pg_dump_version=dump_version or "unknown",
            ),
            telemetry=TelemetrySnapshot(
                taken=snapshot is not None,
                snapshot=snapshot,
                vm_url=vm_url,
                note=(
                    "consistent snapshot created inside the VictoriaMetrics data volume; "
                    "its bytes are NOT in this archive — see omissions"
                    if snapshot
                    else "no telemetry snapshot was taken for this archive"
                ),
            ),
            omissions=_omissions(snapshot),
            contains=_contains(),
        )

        write_archive(
            out,
            manifest_json=manifest.model_dump_json(indent=2).encode(),
            dump_path=dump_path,
        )

    return BackupResult(archive=out, manifest=manifest)


# --------------------------------------------------------------------- restore


async def _public_table_count(dsn: str) -> int:
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    )
                ).scalar_one()
            )
    finally:
        await engine.dispose()


async def restore_backup(
    *,
    dsn: str,
    archive: Path,
    workdir: Path,
    pg_bin: Path | None = None,
    force: bool = False,
) -> Manifest:
    """Load an archive into ``dsn``, or refuse without touching it.

    Order is the safety property. The archive is verified whole, the manifest's
    schema revision is checked against this build, and the target is checked
    empty — all before a single byte is written. The load itself then runs
    inside ``--single-transaction --exit-on-error``, so Postgres makes the last
    step all-or-nothing too. There is no arrangement of failures that leaves a
    half-populated database reporting success.
    """
    pg_restore = _pg_binary("pg_restore", pg_bin)
    verified = open_archive(archive, workdir)

    existing = await _public_table_count(dsn)
    if existing and not force:
        raise RestoreRefusedError(
            "target-not-empty",
            f"the target database already holds {existing} table(s). Restore expects an "
            "empty database — on fresh hardware, create the database and do NOT run "
            "migrations, because the archive carries the schema. Pass --force to drop "
            "and replace what is there.",
        )

    uri, env = _libpq_target(dsn)
    args = [
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--single-transaction",
        f"--dbname={uri}",
    ]
    if force:
        args += ["--clean", "--if-exists"]
    args.append(str(verified.dump_path))

    code, _, stderr = await _run_pg_tool(pg_restore, args, env)
    if code != 0:
        raise RestoreRefusedError(
            "pg-restore-failed",
            f"pg_restore exited {code} and the transaction was rolled back, so the target "
            f"is unchanged: {stderr}",
        )
    return verified.manifest


def _check_schema_revision(manifest: Manifest) -> None:
    """Refuse an archive from a hub newer than this binary.

    A revision this build has never heard of came from code that knows tables,
    columns and constraints this code does not. Loading that dump would produce
    a database the running services cannot describe — and, worse, one that
    looks restored.
    """
    if manifest.schema_revision not in KNOWN_REVISIONS:
        raise RestoreRefusedError(
            "schema-revision-unknown",
            f"archive was taken at schema revision {manifest.schema_revision!r}, which this "
            f"build does not know (it knows up to {KNOWN_REVISIONS[-1]!r}). Restore with a "
            "hub build at least as new as the one that wrote the backup.",
        )
