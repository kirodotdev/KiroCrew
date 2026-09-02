"""Pins the auto-compaction threshold default and the relationships that make
it safe to change.

The defect this guards against is narrow and was live: ``autocompact_pct``
shipped defaulting to 90.0 while 90.0 was also the maximum its own validator
would accept, so the shipped default was the most expensive value an operator
could have chosen.

Three things are pinned, because the number alone is not the invariant:

- the value reached by the path a real install takes, which is ``load()``
  reading a config file, NOT ``SessionConfig()``;
- that the default stays strictly inside its validated range, which is the
  actual defect class;
- that the warning arm fires strictly before the compaction arm, since the two
  are consecutive arms of one if/elif chain and an equal warn level makes the
  warning unreachable.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

from kiro_crew.config.loader import (
    CONTEXT_WARN_MARGIN_PCT,
    DEFAULT_AUTOCOMPACT_PCT,
    KiroCrewConfig,
    SessionConfig,
)
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG


def _load_with_session(session_block: dict) -> KiroCrewConfig:
    """Load config from a temp file holding *session_block*.

    Mirrors ``test_config_loader._load_from_dict``: patch ``config_path`` rather
    than touching the real data home. A distinct temp file per call also keeps
    the loader's fingerprint-keyed hot-path cache from serving one test's data
    to another.
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"session": session_block}, f)
        tmp = Path(f.name)

    try:
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path",
            return_value=tmp,
        ):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


def test_the_default_compacts_below_the_window_ceiling() -> None:
    """The shipped default is 70, not the 90 validation ceiling."""
    assert DEFAULT_AUTOCOMPACT_PCT == 70.0
    assert SessionConfig().autocompact_pct == 70.0


def test_the_default_is_not_the_validation_ceiling() -> None:
    """The defect class: a default equal to its own maximum.

    A default sitting ON the ceiling means the shipped behaviour is the most
    expensive admissible behaviour and no operator can be worse off than
    stock. Keep the default strictly inside the range.
    """
    spec = _EDITABLE_CONFIG["session.autocompact_pct"]
    assert spec["min"] < DEFAULT_AUTOCOMPACT_PCT < spec["max"], (
        f"default {DEFAULT_AUTOCOMPACT_PCT} must sit strictly inside "
        f"({spec['min']}, {spec['max']})"
    )


def _reset_published_threshold() -> None:
    """Return the process-global threshold snapshot to its shipped default.

    The snapshot and its ordering ticket are module globals, so any test that
    publishes MUST restore them from a ``finally`` block: a failed assertion
    would otherwise hand every later test in the process a threshold it never
    set. Resetting the ticket counters too is what keeps the next publish from
    being dropped as older than the one this test installed -- including tests
    that pass synthetic ticket numbers, which a real drawn ticket would outrank
    once enough loads had run in the same worker.
    """
    from kiro_crew.config import loader as _loader

    _loader._CONFIG_AUTOCOMPACT_PCT = DEFAULT_AUTOCOMPACT_PCT
    _loader._CONFIG_AUTOCOMPACT_TICKET = 0
    _loader._CONFIG_AUTOCOMPACT_ISSUED = 0


def test_a_threshold_change_reaches_an_already_live_manager() -> None:
    """A published threshold moves the gate for sessions already open.

    The threshold is captured on the manager's ``_cfg`` when the gateway starts,
    so before this a config write reached disk and stopped there. Publishing on
    every ``load()`` is what lets ANY writer -- the dashboard PATCH handler or
    ``kirocrew config set``, which never contacts the gateway -- take effect
    without a restart.
    """
    from kiro_crew.config.loader import publish_autocompact_pct
    from kiro_crew.session import SessionManager

    # Reset first so this does not depend on what an earlier test in this
    # process published, and pass explicit tickets for the same reason: the
    # ordering guard drops a publish holding a ticket older than the current one.
    _reset_published_threshold()

    cfg = KiroCrewConfig()
    mgr = SessionManager(cfg, provider_factory=lambda *a, **k: object())
    provider = object()

    # 65 sits below the shipped 70, so the gate declines.
    assert cfg.session.autocompact_pct == 70.0
    assert mgr._compaction_gate_decision("k", provider, 65.0) == "below_threshold"

    lowered = KiroCrewConfig()
    lowered.session.autocompact_pct = 60.0
    try:
        publish_autocompact_pct(lowered, 1000)

        # Same reading, same manager, no restart: 65 is now over the bar.
        # Asserting "not below_threshold" keeps this pinned to the threshold and
        # indifferent to the later gates in the ladder.
        assert mgr._compaction_gate_decision("k", provider, 65.0) != "below_threshold"
        assert mgr._cfg.session.autocompact_pct == 60.0
    finally:
        # In a finally block because the snapshot is process-global: a failed
        # assertion above would otherwise leave every later test in this process
        # inheriting the 60.0 threshold.
        _reset_published_threshold()


def test_a_caller_supplied_threshold_survives_until_a_load_publishes() -> None:
    """A manager built with its own threshold keeps it.

    Adoption is on CHANGE, not unconditional, so constructing a manager with a
    config that carries its own value cannot be silently overwritten by whatever
    the last load in this process happened to publish. Without this, every test
    (and any embedder) handing in a tuned config would race the global.
    """
    from kiro_crew.session import SessionManager

    cfg = KiroCrewConfig()
    cfg.session.autocompact_pct = 20.0
    mgr = SessionManager(cfg, provider_factory=lambda *a, **k: object())

    # 25 is above the caller's 20 but below the published default of 70; the
    # caller's value is the one that must decide.
    assert mgr._compaction_gate_decision("k", object(), 25.0) != "below_threshold"
    assert mgr._cfg.session.autocompact_pct == 20.0


def test_a_config_file_omitting_the_key_gets_the_new_default() -> None:
    """The load() path, not the dataclass path, is what installs use.

    ``load()`` passes ``autocompact_pct=`` explicitly, so the dataclass field
    default is consulted only when there is no config file at all. A test that
    only builds ``SessionConfig()`` cannot see this path, and a stale literal
    here would silently keep every config-bearing install on the old value —
    which is how ``pool_size`` came to have a field default of 0 and a load
    fallback of 2.
    """
    cfg = _load_with_session({})

    assert cfg.session.autocompact_pct == DEFAULT_AUTOCOMPACT_PCT


def test_load_preserves_an_operators_configured_value() -> None:
    """Changing a default must not disturb a value someone chose.

    Asserted through ``load()`` reading a real file: constructing
    ``SessionConfig(autocompact_pct=88.0)`` would only exercise the dataclass
    constructor and would still pass if ``load()`` discarded the stored value.
    """
    cfg = _load_with_session({"autocompact_pct": 88.0})

    assert cfg.session.autocompact_pct == 88.0


def test_a_persisted_ceiling_value_is_left_alone() -> None:
    """An install already storing 90.0 keeps it — this change is not a migration.

    Documents the deliberate limit rather than a desired outcome: because
    ``to_dict`` serializes the whole session block with ``asdict``, every
    install that has ever saved its config carries an explicit
    ``autocompact_pct``, so lowering the default does not reach it. If a
    migration is added later this test is the one that must change, and
    changing it should be a conscious act.
    """
    cfg = _load_with_session({"autocompact_pct": 90.0})

    assert cfg.session.autocompact_pct == 90.0


def test_the_warning_fires_strictly_before_compaction() -> None:
    """The warning arm must stay reachable at the shipped default.

    Both consumers test the compaction threshold FIRST and the warning second in
    one if/elif chain, so a warn level at or above the action level makes the
    warning dead code — the early signal vanishes for every operator who did not
    change the default. The margin must be positive and must leave the warn
    level above zero.
    """
    assert CONTEXT_WARN_MARGIN_PCT > 0
    warn_at = DEFAULT_AUTOCOMPACT_PCT - CONTEXT_WARN_MARGIN_PCT
    assert 0 < warn_at < DEFAULT_AUTOCOMPACT_PCT, (
        f"warn level {warn_at} must sit strictly between 0 and the action "
        f"level {DEFAULT_AUTOCOMPACT_PCT}"
    )


def test_the_warning_stays_a_minority_of_the_usable_range() -> None:
    """The warn band must not swallow most of the range it warns about.

    The reachability guard above is satisfied by any positive margin, including
    one wide enough to fire on nearly every turn — and a warning that is always
    on carries no information, which is the failure mode an early-warning line
    actually dies of. On the shipped default the warn band is the top
    ``CONTEXT_WARN_MARGIN_PCT`` of ``DEFAULT_AUTOCOMPACT_PCT`` usable points.

    A quarter is the ceiling because that is where the band stops being an
    approach signal: a 20-point margin on this default covers 29% of the range
    and opens the warning at half the context window, so it fires on ordinary
    mid-session turns rather than on the approach to compaction.
    """
    band_fraction = CONTEXT_WARN_MARGIN_PCT / DEFAULT_AUTOCOMPACT_PCT
    assert band_fraction < 1 / 4, (
        f"the warning covers {band_fraction:.0%} of the usable range "
        f"({CONTEXT_WARN_MARGIN_PCT} of {DEFAULT_AUTOCOMPACT_PCT} points) — "
        f"an always-on warning is not an early warning"
    )


def test_no_consumer_hardcodes_its_own_warn_threshold() -> None:
    """Every warn arm must derive from the shared margin, not a literal.

    This is the gap that let a real defect ship: the session path was converted
    to a relative margin while ``cli_chat`` kept an absolute ``pct >= 75.0``,
    which the lowered default made unreachable. Asserting on the source keeps a
    third consumer from reintroducing the same dead arm, since a hardcoded
    threshold is invisible to a value-level assertion.
    """
    import re
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    for rel in ("session.py", "cli_chat.py"):
        src = (root / rel).read_text(encoding="utf-8")
        # The warn arm must name the shared margin.
        assert "CONTEXT_WARN_MARGIN_PCT" in src, (
            f"{rel} no longer references the shared warn margin — a warn arm "
            f"with its own literal goes dead when the default moves"
        )
        # And must not compare context pct against a bare float literal.
        stray = re.findall(r"pct\s*>=\s*\d+(?:\.\d+)?", src)
        assert not stray, f"{rel} compares context pct to a literal: {stray}"


def test_load_publishes_the_threshold_it_resolved() -> None:
    """Every ``load()`` refreshes the snapshot the gate reads.

    This is the link that makes a CLI write reach a running gateway: prompt
    assembly loads config once per turn, so a value written by any writer is
    published without that writer knowing the gateway exists.
    """
    from kiro_crew.config.loader import published_autocompact_pct

    try:
        _load_with_session({"autocompact_pct": 42.0})
        assert published_autocompact_pct() == 42.0
    finally:
        _reset_published_threshold()


def test_an_older_load_cannot_republish_over_a_newer_one() -> None:
    """A load that finishes late must not reinstate the value it read early.

    Loads run concurrently, so without an ordering ticket an old load publishing
    last would leave live sessions compacting at a threshold the operator had
    already changed.
    """
    from kiro_crew.config.loader import publish_autocompact_pct, published_autocompact_pct

    # Reset first: the tickets below are synthetic, and a higher one published by
    # an earlier test in this worker would drop every publish here.
    _reset_published_threshold()

    newer = KiroCrewConfig()
    newer.session.autocompact_pct = 55.0
    older = KiroCrewConfig()
    older.session.autocompact_pct = 85.0
    try:
        publish_autocompact_pct(newer, 2000)
        assert published_autocompact_pct() == 55.0
        # Same value an in-flight load read before the newer write landed.
        publish_autocompact_pct(older, 1000)
        assert published_autocompact_pct() == 55.0, "an older ticket must be dropped"
        # An equal ticket still publishes: repeating an unchanged value is harmless.
        publish_autocompact_pct(older, 2000)
        assert published_autocompact_pct() == 85.0
    finally:
        _reset_published_threshold()


def test_deleting_the_config_restores_the_default_threshold() -> None:
    """A load that finds no config files must not lose to an earlier publish.

    Ordering exists to stop an older READ overwriting a newer one, but "no config
    file exists" is the current truth rather than a stale read -- and it is the
    degraded-defaults path publish is documented to let through. It needs no
    special ticket value: the defaults load draws a fresh one like any other load
    and wins by ordinary comparison, which is the property an ordering read off
    the files' mtime cannot provide.
    """
    from kiro_crew.config.loader import (
        next_config_load_ticket,
        publish_autocompact_pct,
        published_autocompact_pct,
    )

    _reset_published_threshold()
    tuned = KiroCrewConfig()
    tuned.session.autocompact_pct = 45.0
    try:
        publish_autocompact_pct(tuned, next_config_load_ticket())
        assert published_autocompact_pct() == 45.0

        # Both files deleted: the loader takes its defaults path, holding a ticket
        # drawn after the one above.
        publish_autocompact_pct(KiroCrewConfig(), next_config_load_ticket())
        assert published_autocompact_pct() == DEFAULT_AUTOCOMPACT_PCT

        # A file recreated afterwards orders normally against the reset.
        recreated = KiroCrewConfig()
        recreated.session.autocompact_pct = 50.0
        publish_autocompact_pct(recreated, next_config_load_ticket())
        assert published_autocompact_pct() == 50.0
    finally:
        _reset_published_threshold()


def test_deleting_a_newer_overlay_does_not_reinstate_its_threshold(tmp_path, monkeypatch) -> None:
    """Removing the newer of the two config files must still publish the survivor.

    End-to-end through the real ``load()``, because this is the case an ordering
    key read off the files cannot express: deleting the newer file LOWERS the
    newest mtime across the set, so the load that should win looks older than the
    one that read the file now gone, and the live gate keeps the DELETED overlay's
    threshold. A ticket drawn per load always advances, so the survivor wins.

    The first ``load()`` is load-bearing, not setup: the write-back migration
    rewrites ``config.json`` during it, and doing that AFTER the overlay is
    created would push the base's mtime past the overlay's and mask the very
    rollback under test.
    """
    from kiro_crew.config import loader as _loader

    base = tmp_path / "config.json"
    overlay = tmp_path / "config.local.json"
    monkeypatch.setattr(_loader, "config_path", lambda: base)
    monkeypatch.setattr(_loader, "config_local_path", lambda: overlay)

    base.write_text(json.dumps({"session": {"autocompact_pct": 70.0}}))

    _reset_published_threshold()
    try:
        # Settle the base: this load runs the write-back migration, so afterwards
        # the base's mtime no longer moves under us.
        _loader._invalidate_config_cache()
        _loader.KiroCrewConfig.load()
        assert _loader.published_autocompact_pct() == 70.0

        # Overlay created second, so it is the NEWER file of the two.
        overlay.write_text(json.dumps({"session": {"autocompact_pct": 50.0}}))
        _loader._invalidate_config_cache()
        _loader.KiroCrewConfig.load()
        assert _loader.published_autocompact_pct() == 50.0, "the overlay must apply"

        # Remove it. The newest mtime across the set now goes BACKWARDS to the
        # base's, which is what an mtime-derived ordering key mistakes for a
        # stale read.
        overlay.unlink()
        _loader._invalidate_config_cache()
        _loader.KiroCrewConfig.load()
        assert _loader.published_autocompact_pct() == 70.0, (
            "deleting the overlay must publish the base's threshold, not leave "
            "the deleted file's value in force"
        )
    finally:
        _loader._invalidate_config_cache()
        _reset_published_threshold()


def test_concurrent_publishes_never_let_an_older_ticket_win() -> None:
    """The compare-and-set must be atomic across threads.

    ``load()`` runs on worker threads as well as the loop, so a compare followed
    by a separate assignment lets two loads both pass the comparison and race the
    writes -- leaving the older read installed and the ticket rolled backwards.
    Interleaving is forced here rather than hoped for: a patched comparison blocks
    the low-ticket writer between its check and its write while the high-ticket
    writer completes.
    """
    import threading

    from kiro_crew.config import loader as _loader
    from kiro_crew.config.loader import publish_autocompact_pct, published_autocompact_pct

    _reset_published_threshold()
    older = KiroCrewConfig()
    older.session.autocompact_pct = 30.0
    newer = KiroCrewConfig()
    newer.session.autocompact_pct = 80.0

    entered = threading.Event()
    release = threading.Event()
    real_lock = _loader._CONFIG_AUTOCOMPACT_LOCK

    class _StallOnce:
        """Lock stand-in that parks the FIRST holder inside the critical section."""

        def __init__(self) -> None:
            self._first = True

        def __enter__(self):
            real_lock.acquire()
            if self._first:
                self._first = False
                entered.set()
                release.wait(timeout=5)
            return self

        def __exit__(self, *exc) -> None:
            real_lock.release()

    try:
        _loader._CONFIG_AUTOCOMPACT_LOCK = _StallOnce()
        low = threading.Thread(target=publish_autocompact_pct, args=(older, 1000))
        low.start()
        assert entered.wait(timeout=5), "the first publisher never entered"

        # The high-ticket publisher cannot interleave: it blocks on the same lock.
        high = threading.Thread(target=publish_autocompact_pct, args=(newer, 9000))
        high.start()
        release.set()
        low.join(timeout=5)
        high.join(timeout=5)

        assert published_autocompact_pct() == 80.0, "the newer ticket must win"
        assert _loader._CONFIG_AUTOCOMPACT_TICKET == 9000
    finally:
        _loader._CONFIG_AUTOCOMPACT_LOCK = real_lock
        _reset_published_threshold()


def test_load_publishes_without_a_second_stat_pass(tmp_path, monkeypatch) -> None:
    """Publishing the threshold must add no filesystem I/O to ``load()``.

    ``load()`` is reached from the event loop, so a stat pass taken just to
    order the publish would be blocking I/O for information the cache lookup
    already fetched. One fingerprint pass per load is the contract: it serves
    the cache lookup and the pre-read TOCTOU capture.
    """
    from kiro_crew.config import loader as _loader

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"session": {"autocompact_pct": 41.0}}))
    monkeypatch.setattr(_loader, "config_path", lambda: cfg_file)
    monkeypatch.setattr(_loader, "config_local_path", lambda: tmp_path / "config.local.json")

    passes = {"n": 0}
    real_fingerprint = _loader._config_fingerprint

    def counting_fingerprint():
        passes["n"] += 1
        return real_fingerprint()

    monkeypatch.setattr(_loader, "_config_fingerprint", counting_fingerprint)

    _reset_published_threshold()
    try:
        _loader._invalidate_config_cache()

        passes["n"] = 0
        _loader.KiroCrewConfig.load()
        assert passes["n"] == 1, f"cache-miss load took {passes['n']} stat passes, want 1"
        assert _loader.published_autocompact_pct() == 41.0

        passes["n"] = 0
        _loader.KiroCrewConfig.load()
        assert passes["n"] == 1, f"cache-hit load took {passes['n']} stat passes, want 1"
    finally:
        _loader._invalidate_config_cache()
        _reset_published_threshold()
