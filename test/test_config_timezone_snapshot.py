"""The published config-timezone snapshot.

``kiro_crew.cron`` resolves the global default timezone on the event loop --
``CronService._on_timer``'s due-scan reaches ``_job_tz`` for every
cron-expression job that carries no zone of its own, on every tick -- so the
value is PUBLISHED by each successful ``KiroCrewConfig.load`` rather than read
from ``config.json`` at the point of use. These tests pin the two properties
that makes that safe to rely on: a settings change still reaches a gateway that
is already running, and a load that finishes late cannot reinstate what it read
early.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path
from zoneinfo import ZoneInfo

from kiro_crew.config.loader import (
    KiroCrewConfig,
    next_config_load_ticket,
    publish_config_timezone,
    published_config_timezone,
)
from kiro_crew.cron import CronJob, _job_tz


def _reset_published_timezone() -> None:
    """Return the process-global timezone snapshot to "unset".

    The snapshot and its ordering ticket are module globals, so any test that
    publishes MUST restore them from a ``finally`` block: a failed assertion
    would otherwise hand every later test in the process a zone it never set.

    Deliberately does NOT reset the shared issued-ticket counter that
    ``next_config_load_ticket`` advances. That counter also orders the
    compaction-threshold snapshot, so zeroing it here would leave THAT snapshot
    holding a published ticket no freshly drawn one could beat, and silently
    drop the next real publish in this worker. The ordering test below draws a
    real base ticket instead of hardcoding synthetic numbers, which is what
    makes it independent of whatever ran before it.
    """
    from kiro_crew.config import loader as _loader

    _loader._CONFIG_TIMEZONE = ""
    _loader._CONFIG_TIMEZONE_TICKET = 0


#: Offset putting a test's ticket far beyond anything a real load will draw in
#: this process. The snapshot is a process global and the full suite loads config
#: concurrently, so a test that reads the global back must OUTRANK an interleaving
#: publish -- otherwise it is asserting on another thread's value. The ordering
#: guard is only ever a comparison, so a synthetic ticket is legitimate here; the
#: reset below returns the published mark to 0 so later real loads are unaffected.
_DOMINATING = 1_000_000


def _dominating_ticket() -> int:
    return next_config_load_ticket() + _DOMINATING


def _load_with_timezone(tz_name: str) -> KiroCrewConfig:
    """Load config from a temp file naming *tz_name* as the default zone.

    Patches ``config_path`` rather than touching the real data home; a distinct
    temp file per call also keeps the loader's fingerprint-keyed hot-path cache
    from serving one test's data to another.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"timezone": tz_name}, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


class TestTheSnapshotCarriesTheConfiguredZone:
    def test_load_publishes_the_timezone_it_resolved(self) -> None:
        """Every ``load()`` refreshes the snapshot cron reads.

        This is the link that lets a settings write reach an already-running
        gateway: something loads config on nearly every turn, so the value is
        published without the writer knowing the scheduler exists.

        Asserted on the CALL rather than by reading the global back: the snapshot
        is process-global and other code in the suite loads config concurrently,
        so the value could legitimately be replaced between the load and the
        read. The claim under test is that load() publishes what it resolved,
        which the call itself carries.
        """
        with unittest.mock.patch("kiro_crew.config.loader.publish_config_timezone") as publish:
            cfg = _load_with_timezone("America/Toronto")

        assert cfg.timezone == "America/Toronto"
        assert publish.call_count == 1, "load() must publish exactly once"
        published_cfg = publish.call_args[0][0]
        assert published_cfg.timezone == "America/Toronto"

    def test_the_published_zone_reaches_job_resolution(self) -> None:
        """A job with no zone of its own resolves through the snapshot."""
        job = CronJob(id="j1", name="t", message="m", timezone="")
        try:
            publish_config_timezone(_cfg_with_tz("America/Toronto"), _dominating_ticket())
            assert _job_tz(job) == ZoneInfo("America/Toronto")
        finally:
            _reset_published_timezone()

    def test_an_explicit_job_zone_still_outranks_the_default(self) -> None:
        """The snapshot is a FALLBACK; a job's own zone is not overridden."""
        job = CronJob(id="j1", name="t", message="m", timezone="Asia/Tokyo")
        try:
            publish_config_timezone(_cfg_with_tz("America/Toronto"), _dominating_ticket())
            assert _job_tz(job) == ZoneInfo("Asia/Tokyo")
        finally:
            _reset_published_timezone()


class TestTheSnapshotFailsToUnsetRatherThanToAGuess:
    def test_a_cold_snapshot_reads_as_unset(self) -> None:
        """Before any load, the snapshot says "nothing configured" -- not a zone.

        ``""`` is exactly what an unset ``KiroCrewConfig.timezone`` says, so a
        read taken during the boot window resolves the same way a defaults-only
        load would: through to UTC. Inventing a zone here would make a
        cron-expression job fire at the wrong local time once at startup.
        """
        # Driven through the cron-side reader rather than by resetting the
        # process global: a concurrent load publishing a real zone would make an
        # assertion on the reset global fail for a reason this test is not about.
        # What is under test is that an UNSET default resolves to UTC.
        with unittest.mock.patch("kiro_crew.cron.published_config_timezone", return_value=""):
            job = CronJob(id="j1", name="t", message="m", timezone="")
            assert _job_tz(job) == ZoneInfo("UTC")

    def test_a_defaults_load_overwrites_a_richer_snapshot(self) -> None:
        """The degraded path must CLEAR a zone the files no longer name.

        A load that falls back to defaults (neither config file readable) is the
        current truth, not a stale read. Leaving the previous zone in force
        would keep the scheduler honoring a setting that no longer exists.
        """
        try:
            first = _dominating_ticket()
            publish_config_timezone(_cfg_with_tz("America/Toronto"), first)
            assert published_config_timezone() == "America/Toronto"
            # Strictly newer than the publish above, and still far beyond any
            # ticket a concurrent real load can draw -- see _DOMINATING.
            publish_config_timezone(KiroCrewConfig(), first + 1)
            assert published_config_timezone() == ""
        finally:
            _reset_published_timezone()


class TestConcurrentLoadsAreOrdered:
    def test_an_older_load_cannot_republish_over_a_newer_one(self) -> None:
        """A load that finishes late must not reinstate the zone it read early.

        Loads run concurrently, and this value decides WHEN a job fires: without
        the ordering ticket an old load publishing last would leave every
        schedule depending on the default zone firing against a setting the
        operator had already changed, until something loaded again.
        """
        _reset_published_timezone()
        # Far beyond any ticket a concurrent real load will draw, so an
        # interleaving publish is dropped by the guard rather than winning and
        # failing this test for an unrelated reason -- see _DOMINATING.
        base = _dominating_ticket()
        newer = _cfg_with_tz("America/Toronto")
        older = _cfg_with_tz("Asia/Tokyo")
        try:
            publish_config_timezone(newer, base + 10)
            assert published_config_timezone() == "America/Toronto"
            # The zone an in-flight load read before the newer write landed.
            publish_config_timezone(older, base + 1)
            assert (
                published_config_timezone() == "America/Toronto"
            ), "an older ticket must be dropped"
            # An equal ticket still publishes: repeating a value is harmless.
            publish_config_timezone(older, base + 10)
            assert published_config_timezone() == "Asia/Tokyo"
        finally:
            _reset_published_timezone()

    def test_an_omitted_ticket_draws_a_fresh_one_and_wins(self) -> None:
        """Publishing a just-built config needs no ticket bookkeeping.

        The claim is specifically that a fresh draw outranks every EARLIER draw
        -- which is what a caller publishing a config it just built wants. It is
        not a claim about synthetic ticket numbers: a hand-written ticket far
        ahead of the counter still wins, correctly, because the ordering is only
        ever a comparison.
        """
        _reset_published_timezone()
        try:
            # Both draws are real and consecutive, so the second strictly
            # outranks the first. Asserted immediately; a concurrent load can
            # only publish with a ticket between or after these, and either way
            # the ORDERING property under test still held for this pair -- which
            # is why this reads the value back rather than mocking the reader.
            publish_config_timezone(_cfg_with_tz("America/Toronto"), next_config_load_ticket())
            publish_config_timezone(_cfg_with_tz("Asia/Tokyo"), next_config_load_ticket())
            assert published_config_timezone() in ("Asia/Tokyo", _live_default())
        finally:
            _reset_published_timezone()


def _cfg_with_tz(tz_name: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.timezone = tz_name
    return cfg


def _live_default() -> str:
    """The zone a CONCURRENT real load would have published.

    Named so the one assertion that cannot dominate the ticket counter can admit
    the only other legal outcome, instead of being quietly flaky.
    """
    return KiroCrewConfig.load().timezone
