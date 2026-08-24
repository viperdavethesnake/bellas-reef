# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Clock trust: the shared predicate, and the kernel oracle promoted into its
decision chain.

No RTC battery on the target board — a power cut leaves the clock wrong
until chrony catches up, so ``clock_is_trusted()`` is what every deadline in
the system is allowed to believe.

Decision chain (promoted out of shadow mode 2026-08-23, David's ruling to
flip):

1. ``timedatectl`` answers -> its answer wins (host runs, CI runners with
   systemd).
2. ``timedatectl`` unreachable, the kernel oracle (``adjtimex(2)``) answers
   -> the oracle decides (every container — this is the production path,
   proven live in-container on the Pi 2026-08-23).
3. Neither answers -> the ``BELLASREEF_ASSUME_CLOCK_TRUSTED`` env override
   (the dev-machine fallback: macOS, where libc has no adjtimex).
4. Nothing answers at all -> False.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

import pytest
from bellasreef_service import clock


def _no_timedatectl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``_timedatectl_says_trusted()`` to ``None`` (unreachable),
    deterministically, regardless of what box the suite runs on."""

    def _raise(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("no timedatectl on this box")

    # Patches the same ``subprocess`` module object clock.py imported —
    # modules are singletons in sys.modules, so this reaches it without
    # accessing the non-exported ``clock.subprocess`` attribute mypy
    # --strict flags on a module whose __all__ is explicit.
    monkeypatch.setattr(subprocess, "run", _raise)


def _timedatectl_answers(monkeypatch: pytest.MonkeyPatch, *, synchronised: bool) -> None:
    """Force ``_timedatectl_says_trusted()`` to a real answer."""

    class _FakeCompleted:
        returncode = 0
        stdout = "yes\n" if synchronised else "no\n"

    def _run(*_a: object, **_k: object) -> Any:
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _run)


class TestKernelOracle:
    def test_returns_none_where_the_syscall_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On the dev Mac there is no adjtimex; the oracle must say
        'unknown', never guess."""
        monkeypatch.setattr(clock, "_libc", None)
        assert clock.kernel_clock_synchronised() is None

    def test_returns_none_when_libc_has_no_adjtimex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS libc exists but has no adjtimex — a truthy ``_libc`` must
        not be mistaken for a usable one."""

        class _FakeLibc:
            pass

        monkeypatch.setattr(clock, "_libc", _FakeLibc())
        assert clock.kernel_clock_synchronised() is None

    def test_returns_true_when_the_kernel_reports_synchronised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeLibc:
            def adjtimex(self, _buf: object) -> int:
                return 0  # TIME_OK

        monkeypatch.setattr(clock, "_libc", _FakeLibc())
        assert clock.kernel_clock_synchronised() is True

    def test_returns_false_on_time_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeLibc:
            def adjtimex(self, _buf: object) -> int:
                return clock._TIME_ERROR

        monkeypatch.setattr(clock, "_libc", _FakeLibc())
        assert clock.kernel_clock_synchronised() is False

    def test_returns_none_on_negative_return(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A negative return is the syscall failing (e.g. permission) —
        unknown, not a guessed answer."""

        class _FakeLibc:
            def adjtimex(self, _buf: object) -> int:
                return -1

        monkeypatch.setattr(clock, "_libc", _FakeLibc())
        assert clock.kernel_clock_synchronised() is None

    def test_returns_none_when_the_call_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeLibc:
            def adjtimex(self, _buf: object) -> int:
                raise OSError("boom")

        monkeypatch.setattr(clock, "_libc", _FakeLibc())
        assert clock.kernel_clock_synchronised() is None


class TestTimedatectlSaysTrusted:
    """Unit tests for the split predicate: ``bool | None``, distinguishing
    "timedatectl answered" from "timedatectl is unreachable" — the split the
    decision chain in ``clock_is_trusted()`` depends on."""

    def test_returns_true_when_synchronised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _timedatectl_answers(monkeypatch, synchronised=True)
        assert clock._timedatectl_says_trusted() is True

    def test_returns_false_when_not_synchronised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _timedatectl_answers(monkeypatch, synchronised=False)
        assert clock._timedatectl_says_trusted() is False

    def test_returns_none_when_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_timedatectl(monkeypatch)
        assert clock._timedatectl_says_trusted() is None

    def test_returns_none_on_nonzero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FakeCompleted:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _FakeCompleted())
        assert clock._timedatectl_says_trusted() is None

    def test_returns_none_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_k: object) -> None:
            raise subprocess.TimeoutExpired(cmd="timedatectl", timeout=5)

        monkeypatch.setattr(subprocess, "run", _raise)
        assert clock._timedatectl_says_trusted() is None


class TestClockIsTrustedDecisionChain:
    """The order matters and each step must be able to override the ones
    after it — proven by making the *later* steps disagree and checking the
    earlier one still wins."""

    def setup_method(self) -> None:
        clock._last_shadow = None

    def test_timedatectl_answer_wins_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _timedatectl_answers(monkeypatch, synchronised=True)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "0")
        assert clock.clock_is_trusted() is True

    def test_timedatectl_false_answer_wins_even_over_a_true_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _timedatectl_answers(monkeypatch, synchronised=False)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: True)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
        assert clock.clock_is_trusted() is False

    def test_oracle_decides_when_timedatectl_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_timedatectl(monkeypatch)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: True)
        monkeypatch.delenv(clock.ASSUME_TRUSTED_ENV, raising=False)
        assert clock.clock_is_trusted() is True

    def test_oracle_false_is_honoured_even_with_env_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This is the production case: a container where the oracle says
        the clock is not synchronised must refuse, even though the env
        fallback (still set from before the flip, or by habit) says yes."""
        _no_timedatectl(monkeypatch)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
        assert clock.clock_is_trusted() is False

    def test_env_fallback_when_both_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_timedatectl(monkeypatch)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: None)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
        assert clock.clock_is_trusted() is True

    def test_false_when_nothing_answers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_timedatectl(monkeypatch)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: None)
        monkeypatch.delenv(clock.ASSUME_TRUSTED_ENV, raising=False)
        assert clock.clock_is_trusted() is False


class TestShadowDisagreementLogging:
    """``clock_is_trusted()`` logs when the kernel oracle would have
    disagreed — but only on the timedatectl-decides branch. The
    oracle-decides branch (timedatectl unavailable) has nothing left to
    shadow: the oracle *is* the decision there, not a second opinion on it."""

    def setup_method(self) -> None:
        clock._last_shadow = None

    def test_logs_a_warning_on_disagreement(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _timedatectl_answers(monkeypatch, synchronised=True)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            result = clock.clock_is_trusted()

        assert result is True
        assert any(getattr(r, "event", None) == "clock_shadow_disagreement" for r in caplog.records)

    def test_does_not_log_on_agreement(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _timedatectl_answers(monkeypatch, synchronised=True)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: True)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            clock.clock_is_trusted()

        assert not any(
            getattr(r, "event", None) == "clock_shadow_disagreement" for r in caplog.records
        )

    def test_does_not_log_when_the_oracle_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _timedatectl_answers(monkeypatch, synchronised=True)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: None)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            clock.clock_is_trusted()

        assert not any(
            getattr(r, "event", None) == "clock_shadow_disagreement" for r in caplog.records
        )

    def test_does_not_log_on_the_oracle_decides_branch(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """timedatectl unavailable -> the oracle IS the decision, so there is
        nothing to shadow-log even though nothing here "agrees" with it."""
        _no_timedatectl(monkeypatch)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            clock.clock_is_trusted()

        assert not any(
            getattr(r, "event", None) == "clock_shadow_disagreement" for r in caplog.records
        )

    def test_latches_so_a_steady_disagreement_logs_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _timedatectl_answers(monkeypatch, synchronised=True)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            clock.clock_is_trusted()
            clock.clock_is_trusted()
            clock.clock_is_trusted()

        hits = [
            r for r in caplog.records if getattr(r, "event", None) == "clock_shadow_disagreement"
        ]
        assert len(hits) == 1

    def test_logs_again_on_a_flip_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _timedatectl_answers(monkeypatch, synchronised=True)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)
            clock.clock_is_trusted()  # disagree -> logs
            monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: True)
            clock.clock_is_trusted()  # agree -> quiet
            monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)
            clock.clock_is_trusted()  # disagree again -> logs

        hits = [
            r for r in caplog.records if getattr(r, "event", None) == "clock_shadow_disagreement"
        ]
        assert len(hits) == 2


class TestClockIsTrustedEnvFallbackContract:
    """The env-fallback contract for when NEITHER oracle answers (the dev
    Mac: no timedatectl, and libc has no adjtimex). This used to be
    "the only fallback in a container" (shadow-mode era) — now it is
    specifically the no-oracle-available case, since a real container gets
    an oracle answer at step 2 instead."""

    def test_env_unset_falls_back_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_timedatectl(monkeypatch)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: None)
        monkeypatch.delenv(clock.ASSUME_TRUSTED_ENV, raising=False)
        assert clock.clock_is_trusted() is False

    def test_env_set_to_1_is_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_timedatectl(monkeypatch)
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: None)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
        assert clock.clock_is_trusted() is True
