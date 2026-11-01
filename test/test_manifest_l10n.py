"""The optional manifest ``l10n`` block: listing copy per locale, never a refusal.

A malformed locale is dropped with a warning; the rest of the block, and the
install, are unaffected.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.manifest import (
    MAX_L10N_LOCALES,
    AppManifest,
    L10nConfig,
    L10nEntry,
)
from kiro_crew.apps.manifest_sections import MAX_L10N_WARNINGS

_BASE: dict[str, Any] = {
    "name": "l10n-probe",
    "version": "1.0.0",
    "displayName": "Probe",
    "description": "English description",
}

_DE = {
    "displayName": "Sonde",
    "description": "Deutsche Beschreibung",
    "pageLabel": "Sonde",
    "highlights": ["eins", "zwei"],
}


def _manifest(l10n: Any) -> AppManifest:
    return AppManifest.from_dict({**_BASE, "l10n": l10n})


def test_a_valid_block_round_trips_through_to_dict() -> None:
    m = _manifest({"de": _DE, "pt-BR": {"displayName": "Sonda"}, "zh-CN": {"pageLabel": "探针"}})
    assert m.l10n.warnings == []
    assert m.to_dict()["l10n"] == {
        "de": _DE,
        "pt-BR": {"displayName": "Sonda"},
        "zh-CN": {"pageLabel": "探针"},
    }
    again = AppManifest.from_dict(m.to_dict())
    assert again.l10n == m.l10n
    assert "l10n" not in m.extra


def test_absent_block_emits_nothing() -> None:
    m = AppManifest.from_dict(dict(_BASE))
    assert m.l10n == L10nConfig()
    assert "l10n" not in m.to_dict()


def test_an_empty_entry_is_kept_out_of_to_dict() -> None:
    m = _manifest({"de": {}, "fr": {"displayName": ""}})
    assert m.l10n.warnings == []
    assert "l10n" not in m.to_dict()


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"displayName": "x" * 121}, "displayName is longer than 120"),
        ({"description": "x" * 1001}, "description is longer than 1000"),
        ({"pageLabel": "x" * 61}, "pageLabel is longer than 60"),
        ({"highlights": ["h"] * 13}, "more than 12 items"),
        ({"highlights": ["x" * 301]}, "longer than 300"),
        ({"highlights": "one"}, "highlights must be an array of strings"),
        ({"highlights": [1]}, "highlights must be an array of strings"),
        ({"displayName": 5}, "displayName must be a string"),
        ({"tagline": "x"}, "unknown key(s) tagline"),
        ("Sonde", "must be an object"),
    ],
    ids=[
        "name-over-cap",
        "description-over-cap",
        "label-over-cap",
        "too-many-highlights",
        "highlight-over-cap",
        "highlights-not-list",
        "highlight-not-str",
        "name-not-str",
        "unknown-key",
        "entry-not-object",
    ],
)
def test_a_bad_entry_drops_only_that_locale(entry: Any, reason: str) -> None:
    m = _manifest({"de": entry, "fr": {"displayName": "Sonde"}})
    assert list(m.l10n.entries) == ["fr"]
    assert len(m.l10n.warnings) == 1
    assert m.l10n.warnings[0].startswith("l10n.de: ")
    assert reason in m.l10n.warnings[0]
    assert m.validate() == []


def test_the_caps_are_inclusive() -> None:
    entry = {
        "displayName": "x" * 120,
        "description": "x" * 1000,
        "pageLabel": "x" * 60,
        "highlights": ["x" * 300] * 12,
    }
    assert _manifest({"de": entry}).l10n.entries["de"] == L10nEntry(**entry)


@pytest.mark.parametrize("key", ["DE", "d", "deutsch", "de_DE", "de-", "de-DE-x", "../x", ""])
def test_a_locale_key_outside_the_grammar_is_dropped(key: str) -> None:
    m = _manifest({key: {"displayName": "x"}, "it": {"displayName": "y"}})
    assert list(m.l10n.entries) == ["it"]
    assert "is not a locale tag" in m.l10n.warnings[0]


@pytest.mark.parametrize("key", ["de", "fil", "pt-BR", "zh-Hant", "es-419"])
def test_a_locale_key_inside_the_grammar_is_kept(key: str) -> None:
    assert list(_manifest({key: {"displayName": "x"}}).l10n.entries) == [key]


def test_locales_past_the_cap_are_dropped_in_manifest_order() -> None:
    langs = [f"{a}{b}" for a in "abcdefgh" for b in "abcdefgh"][: MAX_L10N_LOCALES + 2]
    m = _manifest({lang: {"displayName": lang} for lang in langs})
    assert list(m.l10n.entries) == langs[:MAX_L10N_LOCALES]
    assert len(m.l10n.warnings) == 2


def test_the_warnings_are_bounded_and_the_rest_are_counted() -> None:
    bad = {f"BAD-{i}": {"displayName": "x"} for i in range(MAX_L10N_WARNINGS + 5)}
    m = _manifest({**bad, "de": {"displayName": "Sonde"}})
    assert list(m.l10n.entries) == ["de"]
    assert len(m.l10n.warnings) == MAX_L10N_WARNINGS + 1
    assert m.l10n.warnings[-1] == "l10n: 5 more locale(s) dropped"


def test_a_warning_quotes_an_app_supplied_key_cut_short() -> None:
    m = _manifest({"x" * 500: {"displayName": "x"}, "de": {f"k{i}" * 50: "v" for i in range(9)}})
    assert all(len(w) < 200 for w in m.l10n.warnings)
    assert m.l10n.warnings[1].endswith(" and 6 more; dropped")


@pytest.mark.parametrize("raw", ["de", ["de"], 3, True])
def test_a_non_object_block_is_ignored_with_a_warning(raw: Any) -> None:
    m = _manifest(raw)
    assert m.l10n.entries == {}
    assert m.l10n.warnings == ["l10n: must be an object; ignored"]
    assert "l10n" not in m.to_dict()


def test_a_null_block_reads_as_absent() -> None:
    m = _manifest(None)
    assert m.l10n == L10nConfig() and m.l10n.warnings == []


def test_l10n_is_not_in_the_signed_payload() -> None:
    """Listing copy is content: translating an app must not invalidate its signature."""
    plain = AppManifest.from_dict(dict(_BASE)).signing_payload()
    assert _manifest({"de": _DE}).signing_payload() == plain


def test_install_validation_warns_and_does_not_refuse(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from kiro_crew.apps.manager_tree import _validate_source_path

    (tmp_path / "app.json").write_text(
        json.dumps({**_BASE, "l10n": {"de": {"displayName": 5}, "fr": {"displayName": "S"}}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="kiro_crew.apps.manager"):
        errors = _validate_source_path(tmp_path)
    assert errors == []
    assert any("l10n.de: displayName must be a string" in r.getMessage() for r in caplog.records)
