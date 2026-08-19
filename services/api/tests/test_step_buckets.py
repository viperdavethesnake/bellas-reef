# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Step-hold bucketing for piecewise-constant series (actuator duty).

H1/H2, 2026-08-18: actuator state is published on change, not on a cadence.
Bucketing it like a sampled signal made a light held at 0 % all hour vanish
from a 1H window (no sample inside it → no series) and a one-minute 100 % hold
in a 45-minute bucket read as a sample-average of 47.8 % — or as nothing at
all when it fell in a bucket by itself. A duty is a step function: it holds
its last value until the next change, and a bucket's envelope is the
time-weighted min/avg/max of that function inside the bucket.

Pure function, no VictoriaMetrics: the export fetch is a thin wrapper.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bellasreef_api.history import step_buckets

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class TestHold:
    def test_a_value_set_before_the_window_holds_across_every_bucket(self) -> None:
        """The whole point: a light held at 0.3 since yesterday shows 0.3 for
        the entire window, not nothing."""
        buckets = step_buckets([(at(-600), 0.3)], start=at(0), end=at(60), step_s=900)
        assert [b.at for b in buckets] == [at(0), at(15), at(30), at(45)]
        assert all((b.minimum, b.average, b.maximum) == (0.3, 0.3, 0.3) for b in buckets)

    def test_no_sample_before_or_inside_the_window_means_no_buckets(self) -> None:
        assert step_buckets([], start=at(0), end=at(60), step_s=900) == []
        assert step_buckets([(at(70), 0.5)], start=at(0), end=at(60), step_s=900) == []

    def test_buckets_before_the_first_ever_sample_are_absent_not_zero(self) -> None:
        """A device that came into existence mid-window has no state before
        that; inventing 0 would claim it was off."""
        buckets = step_buckets([(at(30), 0.5)], start=at(0), end=at(60), step_s=900)
        assert [b.at for b in buckets] == [at(30), at(45)]
        assert buckets[0].average == 0.5


class TestEnvelope:
    def test_a_short_hold_inside_a_bucket_is_time_weighted(self) -> None:
        """One minute at 1.0 in a 15-minute bucket, 0 otherwise: the average
        is 1/15, the max is 1.0, the min is 0 — the sample-average would have
        said 0.5 and hidden how brief it was."""
        samples = [(at(-100), 0.0), (at(5), 1.0), (at(6), 0.0)]
        buckets = step_buckets(samples, start=at(0), end=at(15), step_s=900)
        assert len(buckets) == 1
        b = buckets[0]
        assert b.minimum == 0.0
        assert b.maximum == 1.0
        assert b.average == pytest.approx(1 / 15)

    def test_a_change_at_a_bucket_boundary_belongs_to_the_bucket_it_starts(self) -> None:
        samples = [(at(-100), 0.0), (at(15), 0.8)]
        buckets = step_buckets(samples, start=at(0), end=at(30), step_s=900)
        assert [(b.at, b.average) for b in buckets] == [(at(0), 0.0), (at(15), 0.8)]

    def test_same_timestamp_twice_keeps_the_last(self) -> None:
        """Three `reason` label variants land as separate VM series; a merged
        list can carry two values at one instant. Last wins, and the earlier
        one leaves no trace in the envelope."""
        samples = [(at(-100), 0.0), (at(5), 0.9), (at(5), 0.2)]
        buckets = step_buckets(samples, start=at(0), end=at(15), step_s=900)
        assert buckets[0].maximum == pytest.approx(0.2)


class TestInputs:
    def test_unsorted_input_is_sorted_first(self) -> None:
        samples = [(at(5), 1.0), (at(-100), 0.0), (at(6), 0.0)]
        a = step_buckets(samples, start=at(0), end=at(15), step_s=900)
        b = step_buckets(sorted(samples), start=at(0), end=at(15), step_s=900)
        assert a == b

    def test_a_window_shorter_than_a_step_still_yields_one_bucket(self) -> None:
        buckets = step_buckets([(at(-1), 0.4)], start=at(0), end=at(5), step_s=900)
        assert len(buckets) == 1
        assert buckets[0].average == pytest.approx(0.4)
