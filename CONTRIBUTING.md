# Contributing

## A CLA is required, or contributions cannot be accepted

Bella's Reef LLC offers commercial licences that release bundlers and OEMs from
the AGPL's disclosure obligations. Offering that licence requires the LLC to
hold relicensing rights over the whole codebase — and it cannot hold those
rights over code contributed under AGPL by someone else without an explicit
grant.

So: **pull requests cannot be merged without a signed Contributor Licence
Agreement.** This is stated up front rather than discovered after you have
written a patch, which is the outcome this paragraph exists to prevent.

If you would rather not sign a CLA, that is entirely reasonable. Fork it — the
AGPL guarantees you that right, and nothing here is intended to discourage it.
Bug reports, reproductions, hardware findings and documentation corrections are
welcome regardless and need no agreement.

The CLA is not yet published. Until it is, code contributions are not being
accepted. Issues are.

## If you are running this on a tank

Please report what you find, especially failures. The most useful bug report is
one with the exit code, the relevant `journalctl -u bellasreef-hardware-io`
lines, and what the hardware physically did — not what the UI said it did.

Safety-relevant reports take priority over everything else. If you have found a
way for the controller to leave an actuator energised when it should not, say
so plainly in the title; that is not a normal bug.

## Working on the code

```bash
./scripts/install-hooks.sh    # pre-push runs the full gate and blocks on failure
uv sync
./scripts/check.sh            # ruff, ruff format, mypy --strict, pytest, alembic render
```

CI runs exactly `scripts/check.sh`, so a green local gate is a green CI.

Tests that need real hardware are marked `@pytest.mark.hardware` and never run
in CI. Tests needing NATS or Postgres skip unless `BELLASREEF_TEST_NATS_URL` /
`BELLASREEF_TEST_DATABASE_URL` are set.

Read [`CLAUDE.md`](CLAUDE.md) before proposing an architectural change. The
stack is locked and the reasoning is recorded; if you think a locked decision is
wrong, raise it as an issue with the requirement it traces to rather than a PR.

## Licence of contributions

By submitting a contribution you agree it is licensed under the licence of the
component it touches:

- `contracts/` — Apache-2.0
- everything else — AGPL-3.0-only

and, subject to the CLA, that Bella's Reef LLC may relicense it commercially.
