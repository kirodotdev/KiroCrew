"""Load/write bound parity for `_EDITABLE_CONFIG`'s numeric fields.

The dashboard API rejects an out-of-range value at WRITE time. A hand-edited
`config.json` never reaches that API, so whatever bounds the load path fails to apply
are simply not enforced -- the asymmetry #4688 and #4734 closed for the
security-relevant knobs, still open for eleven others.

The primary test here is a RATCHET over `_EDITABLE_CONFIG` rather than eleven
hand-written cases. Eleven cases prove eleven fields; the ratchet proves the invariant
and fails when the twelfth field is added with a write bound and no load bound, which is
how this drifted in the first place.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG


def _loaded(tmp_path: Path, data: dict) -> KiroCrewConfig:
    """Load *data* as config.json through the real load path.

    Goes through `KiroCrewConfig.load()` rather than constructing dataclasses: every
    clamp under test lives on the load path, so a hand-built dataclass skips all of them
    and proves nothing about what a hand-edited file produces.
    """
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
        return KiroCrewConfig.load()


def _bounded_numeric_fields() -> list[tuple[str, str, str, float, float]]:
    """Every `_EDITABLE_CONFIG` entry that declares a numeric range at write time."""
    out: list[tuple[str, str, str, float, float]] = []
    for path_key, spec in sorted(_EDITABLE_CONFIG.items()):
        if spec.get("type") not in ("int", "float"):
            continue
        lo, hi = spec.get("min"), spec.get("max")
        if lo is None or hi is None:
            continue
        section, _, leaf = path_key.partition(".")
        if not section or not leaf or "." in leaf:
            continue  # nested keys address a sub-dict, not a scalar on a config section
        out.append((path_key, section, leaf, lo, hi))
    return out


BOUNDED_FIELDS = _bounded_numeric_fields()


def test_the_field_inventory_is_not_silently_empty() -> None:
    """Guards the ratchet itself.

    Every assertion below is parametrised over `_EDITABLE_CONFIG`. If a refactor renamed
    the table or changed a spec key, the parametrisation would collapse to zero cases and
    the whole suite would pass while checking nothing.
    """
    assert len(BOUNDED_FIELDS) >= 15, f"only {len(BOUNDED_FIELDS)} bounded fields discovered"


@pytest.mark.parametrize("path_key,section,leaf,lo,hi", BOUNDED_FIELDS, ids=lambda v: str(v))
def test_a_value_above_the_write_ceiling_is_bounded_on_load(
    path_key: str, section: str, leaf: str, lo: float, hi: float, tmp_path: Path
) -> None:
    """The ceiling the API enforces must also hold for a hand-edited file.

    The ceiling is the half that matters: an absurd budget, timeout or pool size loaded
    verbatim becomes real work, real memory or a real stall.
    """
    absurd = int(hi) * 1000 + 12345
    cfg = _loaded(tmp_path, {section: {leaf: absurd}})
    got = getattr(getattr(cfg, section), leaf)
    assert got <= hi, (
        f"{path_key} loaded {got!r} from a hand-edited {absurd!r}; the write path caps it "
        f"at {hi}. Pass the bound at the coercion site (_safe_int's lo/hi, or "
        f"_safe_nonnegative_int's hi)."
    )


@pytest.mark.parametrize("path_key,section,leaf,lo,hi", BOUNDED_FIELDS, ids=lambda v: str(v))
def test_a_value_inside_the_range_is_stored_verbatim(
    path_key: str, section: str, leaf: str, lo: float, hi: float, tmp_path: Path
) -> None:
    """The clamp must not be a blanket overwrite.

    Without this the ceiling test above could be satisfied by pinning every field to its
    default and ignoring the file, which would be a worse bug than the one being fixed.
    """
    inside = int(lo) + 1 if int(hi) > int(lo) + 1 else int(lo)
    if not (lo <= inside <= hi):
        pytest.skip(f"{path_key} has no representable interior value")
    cfg = _loaded(tmp_path, {section: {leaf: inside}})
    got = getattr(getattr(cfg, section), leaf)
    assert got == inside, f"{path_key} rewrote a deliberate in-range {inside!r} to {got!r}"


def test_type_handling_stays_upstream_and_this_change_only_adds_range(tmp_path: Path) -> None:
    """Pins the division of labour, and corrects a claim this PR first got wrong.

    `session.timeout_secs` was the only one of the eleven whose load site had no
    `_safe_int` at all, and the first draft of this change asserted that a hand-edited
    `"abc"` therefore reached the dataclass verbatim. That is FALSE, and measuring it is
    what caught it: on the base revision `"abc"` and `true` already loaded as the 3600
    default, because `_validate_config_data` runs over the raw dict before section
    extraction and owns type handling.

    So this is a CONTRACT test, not a regression test -- it passes on base too, which is
    exactly the point. Types belong upstream; the only thing this change adds is the
    range. Kept so nobody later "fixes" a type hole here that does not exist, or removes
    the upstream pass believing the coercion at this site covers it.
    """
    assert _loaded(tmp_path, {"session": {"timeout_secs": "abc"}}).session.timeout_secs == 3600
    assert _loaded(tmp_path, {"session": {"timeout_secs": True}}).session.timeout_secs == 3600
    # A numeric STRING is still honoured (older writers stored these as strings), which is
    # why the clamp has to live at the coercion site rather than only in the raw-dict pass.
    assert _loaded(tmp_path, {"session": {"timeout_secs": "600"}}).session.timeout_secs == 600


def test_a_negative_knowledge_budget_keeps_its_default_instead_of_clamping_to_zero(
    tmp_path: Path,
) -> None:
    """Pins a deliberate asymmetry so it is not "tidied" into a clamp.

    These budgets declare a write minimum of 0, but 0 is MEANINGFUL: a zero chunk budget
    turns that sweep off. Clamping -1 up to 0 would silently disable a sweep the operator
    never asked to disable, so `_safe_nonnegative_int` keeps returning the default for a
    negative value and only the ceiling is applied.
    """
    cfg = _loaded(tmp_path, {"knowledge": {"sweep_chunk_budget": -1}})
    assert cfg.knowledge.sweep_chunk_budget == 500


def test_the_completion_keep_ceiling_matches_its_owner() -> None:
    """The one bound that genuinely has to be spelled twice, pinned equal.

    `context_management` does `from kiro_crew.config.loader import config_dir`, so the
    loader cannot import `RESULT_FILE_MAX_BYTES` back without a circular import. The value
    therefore lives in both modules, and this test is the only place the two spellings can
    be held together -- a test imports both without the cycle.
    """
    from kiro_crew.config import loader
    from kiro_crew.context_management import RESULT_FILE_MAX_BYTES

    assert loader.COMPLETION_KEEP_CHARS_MAX == RESULT_FILE_MAX_BYTES, (
        "the loader's completion-keep ceiling has drifted from context_management's "
        "RESULT_FILE_MAX_BYTES; they must stay one number"
    )


def test_the_write_table_reads_its_bounds_from_the_loader_constants() -> None:
    """Drift ratchet for the two-literal pairs.

    The bounds were spelled twice -- once in the loader's clamp, once in
    `_EDITABLE_CONFIG` -- so the two could disagree and nothing would notice. They now
    share named constants. This is NOT tautological: it fails the moment someone
    re-hardcodes a literal in either place.
    """
    from kiro_crew.config import loader

    expected = {
        "agent.soft_stop_budget_secs": (loader.SOFT_STOP_BUDGET_MIN, loader.SOFT_STOP_BUDGET_MAX),
        "knowledge.extraction_pool_size": (
            loader.EXTRACTION_POOL_SIZE_MIN,
            loader.EXTRACTION_POOL_SIZE_MAX,
        ),
        "session.timeout_secs": (loader.SESSION_TIMEOUT_MIN, loader.SESSION_TIMEOUT_MAX),
        "session.pool_ttl_secs": (loader.POOL_TTL_SECS_MIN, loader.POOL_TTL_SECS_MAX),
        "dashboard.mcp_probe_timeout_secs": (
            loader.MCP_PROBE_TIMEOUT_MIN,
            loader.MCP_PROBE_TIMEOUT_MAX,
        ),
        "dashboard.recent_tint_count": (
            loader.RECENT_TINT_COUNT_MIN,
            loader.RECENT_TINT_COUNT_MAX,
        ),
    }
    for path_key, (lo, hi) in expected.items():
        spec = _EDITABLE_CONFIG[path_key]
        assert (spec["min"], spec["max"]) == (lo, hi), (
            f"{path_key} write bounds {(spec['min'], spec['max'])} no longer match the "
            f"loader constants {(lo, hi)} -- the two spellings have drifted apart again"
        )
