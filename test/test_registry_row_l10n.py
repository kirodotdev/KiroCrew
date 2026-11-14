"""A store row carries the app's ``l10n`` block, so a reader sees its translation before install.

The dashboard reads ``manifest.l10n`` on both an installed record and a store row
(``components/appstore/appManifest.ts``), so a row that dropped the block showed
English to a reader whose language the app translated.
"""

from __future__ import annotations

from kiro_crew.apps import registry

ENTRY = {"name": "notes", "repo": "https://github.com/acme/notes"}


def test_the_store_row_carries_the_parsed_l10n_block() -> None:
    manifest = {"displayName": "Notes", "l10n": {"de": {"displayName": "Notizen"}}}
    out = registry._merge_manifest(ENTRY, manifest)
    assert out["manifest"]["l10n"] == {"de": {"displayName": "Notizen"}}


def test_a_malformed_locale_is_dropped_as_on_install() -> None:
    manifest = {
        "l10n": {
            "de": {"displayName": "Notizen"},
            "not a tag": {"displayName": "x"},
            "fr": {"displayName": "y" * 500},
        }
    }
    out = registry._merge_manifest(ENTRY, manifest)
    assert out["manifest"]["l10n"] == {"de": {"displayName": "Notizen"}}


def test_no_l10n_block_adds_none() -> None:
    out = registry._merge_manifest(ENTRY, {"displayName": "Notes", "l10n": "oops"})
    assert "manifest" not in out or "l10n" not in out["manifest"]
