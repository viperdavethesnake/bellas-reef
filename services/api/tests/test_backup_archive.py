# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Every way an archive can be wrong, and the name restore gives it.

These need no database and no ``pg_dump``: they are about the archive as a
file. That is deliberate. The whole safety property of restore — *nothing
touches the database until the archive has been proven whole* — lives in this
layer, so this layer has to be checkable on its own.

The reason slugs asserted here are an operator-facing contract. An operator
reading ``payload-corrupt`` at 2am needs it to mean the same thing it meant the
last time they saw it, so they are asserted by value, not by message text.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from bellasreef_api.backup import (
    DUMP_NAME,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    Manifest,
    RestoreRefusedError,
    _contains,
    open_archive,
    write_archive,
)
from bellasreef_api.cli import _print_contains
from bellasreef_db.revisions import HEAD_REVISION

DUMP = b"PGDMP-not-really-but-bytes-are-bytes" * 64


def _manifest_dict(**overrides: object) -> dict[str, object]:
    """A manifest that would verify, before the test breaks one field of it."""
    base: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "created_at": "2026-08-10T12:00:00Z",
        "hub": {
            "database_host": "bellasreef.local",
            "taken_on": "bellasreef",
            "database": "bellasreef",
            "postgres_version": "17.5",
        },
        "schema_revision": HEAD_REVISION,
        "contracts_version": "3.0.0",
        "tool_version": "0.1.0",
        "postgres": {
            "file": DUMP_NAME,
            "format": "custom",
            "sha256": hashlib.sha256(DUMP).hexdigest(),
            "bytes": len(DUMP),
            "pg_dump_version": "17.5",
        },
        "telemetry": {
            "taken": True,
            "snapshot": "20260810120000-0000000000000001",
            "vm_url": "http://victoria-metrics:8428",
            "note": "snapshot lives in the VictoriaMetrics volume; not in this archive",
        },
        "omissions": [
            {"what": "telemetry samples", "why": "they live in VM", "recover": "copy the snapshot"}
        ],
    }
    base.update(overrides)
    return base


def _good_archive(tmp_path: Path) -> Path:
    dump_path = tmp_path / "source.dump"
    dump_path.write_bytes(DUMP)
    path = tmp_path / "backup.tar.gz"
    write_archive(path, manifest_json=json.dumps(_manifest_dict()).encode(), dump_path=dump_path)
    return path


def _handmade_archive(path: Path, members: dict[str, bytes]) -> Path:
    """A tar.gz with exactly the members given — including none at all."""
    with tarfile.open(path, "w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return path


# --------------------------------------------------------------------- happy


def test_a_whole_archive_verifies_and_yields_its_manifest(tmp_path: Path) -> None:
    verified = open_archive(_good_archive(tmp_path), tmp_path / "work")

    assert verified.manifest.schema_revision == HEAD_REVISION
    assert verified.manifest.hub.database_host == "bellasreef.local"
    assert verified.dump_path.read_bytes() == DUMP


# ------------------------------------------------------------------ refusals


def test_a_truncated_archive_is_refused_as_unreadable(tmp_path: Path) -> None:
    archive = _good_archive(tmp_path)
    whole = archive.read_bytes()
    archive.write_bytes(whole[: len(whole) // 2])

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "archive-unreadable"


def test_an_archive_that_is_not_there_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(tmp_path / "nothing-here.tar.gz", tmp_path / "work")
    assert caught.value.reason == "archive-missing"


def test_a_file_that_is_not_an_archive_at_all_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"this is a text file someone renamed")

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "archive-unreadable"


def test_a_dump_that_does_not_match_its_digest_is_refused(tmp_path: Path) -> None:
    """Intact tar, intact manifest, wrong bytes — only the digest catches this."""
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz",
        {
            MANIFEST_NAME: json.dumps(_manifest_dict()).encode(),
            DUMP_NAME: DUMP[:-1] + b"X",
        },
    )

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "payload-corrupt"


def test_a_dump_shorter_than_the_manifest_claims_is_refused(tmp_path: Path) -> None:
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz",
        {MANIFEST_NAME: json.dumps(_manifest_dict()).encode(), DUMP_NAME: DUMP[:100]},
    )

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "payload-corrupt"


def test_an_archive_with_no_manifest_is_refused(tmp_path: Path) -> None:
    archive = _handmade_archive(tmp_path / "backup.tar.gz", {DUMP_NAME: DUMP})

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "manifest-missing"


def test_an_archive_with_no_dump_is_refused(tmp_path: Path) -> None:
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz", {MANIFEST_NAME: json.dumps(_manifest_dict()).encode()}
    )

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "payload-missing"


def test_a_manifest_that_is_not_json_is_refused(tmp_path: Path) -> None:
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz", {MANIFEST_NAME: b"{not json", DUMP_NAME: DUMP}
    )

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "manifest-unreadable"


def test_a_manifest_missing_a_required_field_is_refused(tmp_path: Path) -> None:
    incomplete = _manifest_dict()
    del incomplete["schema_revision"]
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz",
        {MANIFEST_NAME: json.dumps(incomplete).encode(), DUMP_NAME: DUMP},
    )

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "manifest-incomplete"


def test_a_newer_manifest_format_is_refused(tmp_path: Path) -> None:
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz",
        {
            MANIFEST_NAME: json.dumps(
                _manifest_dict(manifest_version=MANIFEST_VERSION + 1)
            ).encode(),
            DUMP_NAME: DUMP,
        },
    )

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "manifest-version-unsupported"


def test_a_schema_revision_this_binary_has_never_heard_of_is_refused(tmp_path: Path) -> None:
    """The archive is from a newer hub. Its dump describes tables we do not know."""
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz",
        {
            MANIFEST_NAME: json.dumps(_manifest_dict(schema_revision="0042")).encode(),
            DUMP_NAME: DUMP,
        },
    )

    with pytest.raises(RestoreRefusedError) as caught:
        open_archive(archive, tmp_path / "work")
    assert caught.value.reason == "schema-revision-unknown"
    assert "0042" in caught.value.detail


def test_an_older_schema_revision_is_accepted(tmp_path: Path) -> None:
    """Behind is fine — those migrations are in this binary and can be re-run."""
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz",
        {
            MANIFEST_NAME: json.dumps(_manifest_dict(schema_revision="0001")).encode(),
            DUMP_NAME: DUMP,
        },
    )

    assert open_archive(archive, tmp_path / "work").manifest.schema_revision == "0001"


def test_a_member_trying_to_escape_the_workdir_is_refused(tmp_path: Path) -> None:
    """Members are read by exact name, so a traversal path is simply not found."""
    archive = _handmade_archive(
        tmp_path / "backup.tar.gz",
        {
            MANIFEST_NAME: json.dumps(_manifest_dict()).encode(),
            "../../etc/passwd": b"root:x:0:0",
            DUMP_NAME: DUMP,
        },
    )

    verified = open_archive(archive, tmp_path / "work")
    assert sorted(p.name for p in verified.dump_path.parent.iterdir()) == [DUMP_NAME]


# ------------------------------------------------------- what the operator sees


def test_the_operator_is_told_what_the_archive_contains(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `contains` list reached the manifest and the docs, and stopped there.

    Which is the one place it was no use: the terminal is where the file has
    just been written and where the operator is deciding whether to drop it in a
    cloud folder. Printed at backup and at restore, above the omissions, and
    shaped as a warning rather than a bullet.
    """
    manifest = Manifest.model_validate(
        _manifest_dict(contains=[item.model_dump() for item in _contains()])
    )

    _print_contains(manifest)

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "signing secret" in out
    assert "password-manager export" in out, "the handling advice, not only the label"


def test_an_archive_from_before_the_contains_list_still_prints(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Old, not wrong. A manifest without the field reads, and says nothing."""
    _print_contains(Manifest.model_validate(_manifest_dict()))
    assert capsys.readouterr().out == ""
