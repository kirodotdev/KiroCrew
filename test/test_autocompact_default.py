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
