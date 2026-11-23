"""An import vets a settings document saved "UTF-8 with BOM" as it vets one without.

The live stores read ``config.json``, ``config.local.json`` and
``notification_settings.json`` past one leading byte-order mark, so the archive
vetting does too; a document that does not parse, or is not an object, is still
skipped.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import portability

_BOM = chr(0xFEFF)

_DOCUMENTS = [
    ("config.json", "config", {"agent": {"model": "auto"}}),
    ("config.local.json", "config.local", {"agent": {"model": "auto"}}),
    (
        "notification_settings.json",
        "notification-settings",
        {"channel_settings": {"cron.done": {"muted": True}}},
    ),
]


@pytest.fixture(autouse=True)
def _roots(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws-root"))


def _vet(tmp_path, name: str, text: str):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / name).write_text(text, encoding="utf-8")
    summary: dict = {"items": []}
    return snap, portability._vet_archive_settings(snap, summary), summary


@pytest.mark.parametrize(("name", "label", "doc"), _DOCUMENTS)
def test_a_marked_document_vets_as_the_unmarked_one(tmp_path, name, label, doc):
    snap, vetted, summary = _vet(tmp_path, name, _BOM + json.dumps(doc))
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / name).write_text(json.dumps(doc), encoding="utf-8")
    expected = portability._vet_archive_settings(plain, {"items": []})
    assert vetted == expected and name in vetted
    assert (snap / name).exists()
    assert not any(i.startswith(f"{label} (skipped") for i in summary["items"]), summary


@pytest.mark.parametrize(("name", "label", "_doc"), _DOCUMENTS)
@pytest.mark.parametrize("body", ["[1]", "{not json", _BOM + "{}"])
def test_a_marked_document_that_is_not_an_object_is_still_skipped(
    tmp_path, name, label, _doc, body
):
    snap, vetted, summary = _vet(tmp_path, name, _BOM + body)
    assert name not in vetted
    assert not (snap / name).exists()
    assert any(i.startswith(f"{label} (skipped: ") for i in summary["items"]), summary
