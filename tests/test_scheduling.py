from dataclasses import replace

import pytest

from jobbot.models import MAX_PUBLICATION_AGE, Company
from jobbot.scheduling import (
    DEFAULT_TIERS,
    MAX_QUIET_INTERVAL,
    SchedulingPolicy,
    after,
    bounded_tiers,
    parse_priority_boards,
)


def test_quiet_tiers_climb_to_the_ceiling_and_reset():
    policy = SchedulingPolicy()
    tier = 0
    for expected, delay in enumerate(DEFAULT_TIERS[1:], 1):
        tier, interval = policy.success(tier, False)
        assert (tier, interval) == (expected, delay)
    assert policy.success(tier, False) == (len(DEFAULT_TIERS) - 1, DEFAULT_TIERS[-1])
    assert policy.success(tier, True) == (0, 1800)


def test_no_tier_outlives_the_publication_freshness_window():
    """A board slept past this window would classify its own new listings as stale,
    which never counts as qualifying, so its tier would never reset.  Regression guard."""
    window = MAX_PUBLICATION_AGE.total_seconds()
    for tiers in (DEFAULT_TIERS, (1800, 604800, 2592000), (86400, 999999999)):
        policy = SchedulingPolicy(tiers=tiers)
        assert policy.tiers, "at least one tier must survive the ceiling"
        assert max(policy.tiers) <= MAX_QUIET_INTERVAL < window
        tier = 0
        for _ in range(len(tiers) + 5):
            tier, interval = policy.success(tier, False)
            assert interval < window


def test_over_generous_tiers_are_dropped_not_rejected():
    # An unattended bot keeps scanning on a safe cadence rather than refusing to start.
    assert bounded_tiers((1800, 604800, 2592000)) == (1800,)
    assert bounded_tiers(DEFAULT_TIERS) == DEFAULT_TIERS
    assert bounded_tiers((2592000,)) == (2592000,), "never return an empty ladder"
    assert SchedulingPolicy(tiers=(1800, 7200, 2592000)).tiers == (1800, 7200)


@pytest.mark.parametrize("priority", [1800, 7200])
def test_priority_caps_quiet_and_active_cadence(priority):
    policy = SchedulingPolicy()
    for tier in range(10):
        assert policy.success(tier, False, priority)[1] <= priority
        assert policy.success(tier, True, priority) == (0, 1800)


def test_retry_uses_separate_capped_exponential_delay():
    policy = SchedulingPolicy()
    assert [policy.retry(n) for n in range(1, 9)] == [
        1800,
        3600,
        7200,
        14400,
        28800,
        57600,
        86400,
        86400,
    ]
    assert policy.retry(100000) == 86400
    assert replace(policy, failure_retry=30).retry(1) == 30


def test_priority_seeds_and_per_board_override():
    nvidia = Company("Nvidia", "jsonld", "https://example.com/careers")
    amazon = Company("Amazon", "jsonld", "https://example.com/jobs")
    policy = SchedulingPolicy(priority_boards={nvidia.key: 1800})
    assert policy.priority(nvidia) == 1800
    assert policy.priority(amazon) == 7200
    assert policy.priority(Company("Other", "ashby", "other")) == 0
    assert policy.priority(Company("NVIDIA", "ashby", "nvidia")) == 7200


def test_custom_tiers_and_priority_parser():
    policy = SchedulingPolicy(tiers=(60, 120, 300))
    assert policy.success(1, False) == (2, 300)
    assert policy.success(2, True) == (0, 60)
    assert parse_priority_boards("ashby:global:acme, lever:eu:example=1800", 7200) == {
        "ashby:global:acme": 7200,
        "lever:eu:example": 1800,
    }
    assert parse_priority_boards("jsonld:global:https://example.com/careers=1800", 7200) == {
        "jsonld:global:https://example.com/careers": 1800
    }
    assert after("2026-09-06T00:00:00+00:00", 1800) == "2026-09-06T00:30:00+00:00"


@pytest.mark.parametrize(
    "value",
    [
        "acme",
        "workday:global:acme",
        "lever:us:acme",
        "ashby:global:",
        "lever:eu:a/b",
        "ashby:global:acme=0",
        "ashby:global:acme=-1",
        "ashby:global:acme=86400",
        "ashby:global:acme=fast",
        "ashby:global:acme,",
        "ashby:global:a,ashby:global:a",
    ],
)
def test_invalid_priority_boards(value):
    with pytest.raises(ValueError, match="PRIORITY_BOARDS"):
        parse_priority_boards(value, 7200)
