"""``import_memory`` stores a semantic row's ``confidence`` inside ``0..1``.

An export file is the user's own data, so a bad confidence never costs a row its
import: an out-of-range number is clamped, NaN and a non-number take the import
default, and every value a file could import before keeps the confidence it was
stored at.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest
from vector_memory_coverage_helpers import _DIM

from kiro_crew.vector_memory import VectorMemoryStore

_KEY = "pref.editor"
# The confidence import_memory stores for a row without a usable one, as on main.
_DEFAULT = 0.85


def _imported_confidence(
    tmp_path: Path, entry: dict, *, threshold: float = 0.8
) -> tuple[dict[str, int], float | None]:
    store = VectorMemoryStore(
        db_path=tmp_path / "mem.db", embedding_dim=_DIM, confidence_threshold=threshold
    )
    store.init()
    counts = store.import_memory({"semantic": [{"key": _KEY, "value": "vim", **entry}]})
    row = store.get_semantic(_KEY)
    return counts, None if row is None else float(row["confidence"])


_BAD = "bad_confidence_is_bounded_and_the_row_still_imports"
_KEPT = "a_value_main_imported_keeps_its_confidence"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        pytest.param(99, 1.0, id=f"{_BAD}-99"),
        pytest.param(1.5, 1.0, id=f"{_BAD}-1.5"),
        pytest.param(10**400, 1.0, id=f"{_BAD}-400-digit-int"),
        pytest.param(math.inf, 1.0, id=f"{_BAD}-inf"),
        pytest.param("99", 1.0, id=f"{_BAD}-str-99"),
        pytest.param(float("nan"), _DEFAULT, id=f"{_BAD}-nan"),
        pytest.param("nan", _DEFAULT, id=f"{_BAD}-str-nan"),
        pytest.param("high", _DEFAULT, id=f"{_BAD}-str-high"),
        pytest.param(None, _DEFAULT, id=f"{_BAD}-None"),
        pytest.param([0.9], _DEFAULT, id=f"{_BAD}-list"),
        pytest.param({"v": 0.9}, _DEFAULT, id=f"{_BAD}-dict"),
        pytest.param(0.9, 0.9, id=f"{_KEPT}-0.9"),
        pytest.param(1, 1.0, id=f"{_KEPT}-1"),
        pytest.param(True, 1.0, id=f"{_KEPT}-True"),
        pytest.param("0.95", 0.95, id=f"{_KEPT}-str-0.95"),
        pytest.param(" 1 ", 1.0, id=f"{_KEPT}-str-padded-1"),
    ],
)
def test_any_confidence_imports_the_row_at_a_bounded_value(
    tmp_path: Path, raw: object, stored: float
) -> None:
    counts, confidence = _imported_confidence(tmp_path, {"confidence": raw})
    assert counts == {"semantic": 1, "episodic": 0, "skipped": 0}
    assert confidence == stored


def test_a_missing_confidence_takes_the_import_default(tmp_path: Path) -> None:
    counts, confidence = _imported_confidence(tmp_path, {})
    assert counts == {"semantic": 1, "episodic": 0, "skipped": 0}
    assert confidence == _DEFAULT


@pytest.mark.parametrize("raw", [-3, -0.5, False])
def test_a_low_confidence_is_refused_by_the_store_threshold_as_before(
    tmp_path: Path, raw: object
) -> None:
    # Clamped to 0, below the store's confidence threshold, as the raw value was.
    counts, confidence = _imported_confidence(tmp_path, {"confidence": raw})
    assert counts == {"semantic": 0, "episodic": 0, "skipped": 1}
    assert confidence is None


@pytest.mark.parametrize(
    "raw", [99, float("nan"), "high", None, pytest.param(10**400, id="400-digit-int")]
)
def test_a_replaced_confidence_logs_a_warning_without_the_value(
    tmp_path: Path, raw: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"):
        _imported_confidence(tmp_path, {"confidence": raw})
    warnings = [
        r.getMessage() for r in caplog.records if "memory import: confidence" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert str(raw) not in warnings[0]


def test_an_in_range_confidence_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.vector_memory"):
        _imported_confidence(tmp_path, {"confidence": 0.9})
    assert not [r for r in caplog.records if "memory import: confidence" in r.getMessage()]


@pytest.mark.parametrize("raw", [float("nan"), "nan", "high", None])
def test_a_replaced_confidence_still_meets_a_raised_threshold(tmp_path: Path, raw: object) -> None:
    # NaN passed any threshold on main (nan < t is False), so its replacement must too.
    counts, confidence = _imported_confidence(tmp_path, {"confidence": raw}, threshold=0.9)
    assert counts == {"semantic": 1, "episodic": 0, "skipped": 0}
    assert confidence == 0.9


def test_a_missing_confidence_under_a_raised_threshold_is_refused_as_before(
    tmp_path: Path,
) -> None:
    counts, confidence = _imported_confidence(tmp_path, {}, threshold=0.9)
    assert counts == {"semantic": 0, "episodic": 0, "skipped": 1}
    assert confidence is None
