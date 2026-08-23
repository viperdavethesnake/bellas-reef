# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Clock trust: the shared predicate, plus the kernel oracle running in
shadow mode alongside it.

No RTC battery on the target board — a power cut leaves the clock wrong
until chrony catches up, so ``clock_is_trusted()`` is what every deadline in
the system is allowed to believe. The oracle here (``adjtimex(2)``) doesn't
replace that predicate yet; it only logs when it would have disagreed, so
its own correctness can be checked against weeks of real logs before David
rules on flipping enforcement (2026-08-23 finding 4's PR-body note).
"""

from __future__ import annotations

import logging
import subprocess

import pytest
from bellasreef_service import clock


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


class TestShadowDisagreementLogging:
    """clock_is_trusted() keeps its own contract (timedatectl, then the env
    fallback) but now also logs when the kernel oracle would have answered
    differently — latched so a steady disagreement doesn't spam the log."""

    def setup_method(self) -> None:
        clock._last_shadow = None

    def _no_timedatectl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Force clock_is_trusted() onto the env-fallback path, deterministically."""

        def _raise(*_a: object, **_k: object) -> None:
            raise FileNotFoundError("no timedatectl on this box")

        # Patches the same ``subprocess`` module object clock.py imported —
        # modules are singletons in sys.modules, so this reaches it without
        # accessing the non-exported ``clock.subprocess`` attribute mypy
        # --strict flags on a module whose __all__ is explicit.
        monkeypatch.setattr(subprocess, "run", _raise)

    def test_logs_a_warning_on_disagreement(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._no_timedatectl(monkeypatch)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")  # clock_is_trusted() -> True
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: False)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            result = clock.clock_is_trusted()

        assert result is True
        assert any(getattr(r, "event", None) == "clock_shadow_disagreement" for r in caplog.records)

    def test_does_not_log_on_agreement(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._no_timedatectl(monkeypatch)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: True)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            clock.clock_is_trusted()

        assert not any(
            getattr(r, "event", None) == "clock_shadow_disagreement" for r in caplog.records
        )

    def test_does_not_log_when_the_oracle_is_unknown(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._no_timedatectl(monkeypatch)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
        monkeypatch.setattr(clock, "kernel_clock_synchronised", lambda: None)

        with caplog.at_level(logging.WARNING, logger=clock.__name__):
            clock.clock_is_trusted()

        assert not any(
            getattr(r, "event", None) == "clock_shadow_disagreement" for r in caplog.records
        )

    def test_latches_so_a_steady_disagreement_logs_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._no_timedatectl(monkeypatch)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
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
        self._no_timedatectl(monkeypatch)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")

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


class TestClockIsTrustedContractUnchanged:
    """The env-fallback contract that existed before this task must not move."""

    def test_env_unset_falls_back_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_k: object) -> None:
            raise FileNotFoundError()

        # Patches the same ``subprocess`` module object clock.py imported —
        # modules are singletons in sys.modules, so this reaches it without
        # accessing the non-exported ``clock.subprocess`` attribute mypy
        # --strict flags on a module whose __all__ is explicit.
        monkeypatch.setattr(subprocess, "run", _raise)
        monkeypatch.delenv(clock.ASSUME_TRUSTED_ENV, raising=False)
        assert clock.clock_is_trusted() is False

    def test_env_set_to_1_is_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_a: object, **_k: object) -> None:
            raise FileNotFoundError()

        # Patches the same ``subprocess`` module object clock.py imported —
        # modules are singletons in sys.modules, so this reaches it without
        # accessing the non-exported ``clock.subprocess`` attribute mypy
        # --strict flags on a module whose __all__ is explicit.
        monkeypatch.setattr(subprocess, "run", _raise)
        monkeypatch.setenv(clock.ASSUME_TRUSTED_ENV, "1")
        assert clock.clock_is_trusted() is True
