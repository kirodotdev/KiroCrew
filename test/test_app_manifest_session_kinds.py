"""``contributes.sessionKinds``: what a manifest may declare, and what its signature freezes.

A kind parses, round-trips and is served in the manifest the dashboard lists; it
refuses missing fields, a bad id, a bad ``defaultAgent``, over-cap labels, a
duplicate id and more kinds than the cap; its ``entry`` is contained in the app root
and, because the dashboard runs it in its own origin, signed. The minimal valid
manifest lives in ``app_manifest_helpers.py``.
"""

from __future__ import annotations

import pytest
from app_manifest_helpers import _valid_manifest

from kiro_crew.apps.manifest import (
    MAX_SESSION_KINDS_PER_APP,
    AppManifest,
    Contributes,
    SessionKindConfig,
)


def _kind(**overrides) -> dict:
    kind = {
        "id": "project",
        "title": "Project app",
        "menuLabel": "New project app",
        "menuDescription": "A React app built for this piece of work",
        "icon": "AppWindow",
        "entry": "dist/pane.mjs",
        "defaultAgent": "kirocrew-conductor",
    }
    kind.update(overrides)
    return kind


def _errors(*kinds: object) -> list[str]:
    return AppManifest.from_dict(
        _valid_manifest(contributes={"sessionKinds": list(kinds)})
    ).validate()


def test_a_kind_parses_validates_and_round_trips():
    m = AppManifest.from_dict(_valid_manifest(contributes={"sessionKinds": [_kind()]}))
    assert m.validate() == []
    (kind,) = m.contributes.sessionKinds
    assert kind == SessionKindConfig(**_kind())
    # Served as declared: this is the manifest `GET /api/apps` hands the create menu.
    assert m.to_dict()["contributes"] == {"sessionKinds": [_kind()]}
    assert AppManifest.from_dict(m.to_dict()).contributes == m.contributes


def test_the_optional_fields_may_be_left_out():
    bare = {"id": "project", "title": "P", "menuLabel": "New P", "entry": "pane.mjs"}
    assert _errors(bare) == []
    assert Contributes.from_dict({"sessionKinds": [bare]}).to_dict() == {"sessionKinds": [bare]}


def test_no_kinds_serializes_no_key():
    assert "contributes" not in AppManifest.from_dict(_valid_manifest()).to_dict()


def test_each_required_field_is_reported_missing():
    errors = _errors(_kind(id="", title="", menuLabel="", entry=""))
    assert "contributes.sessionKinds: entry missing id" in errors
    for name in ("title", "menuLabel", "entry"):
        assert any(f"missing {name}" in e for e in errors), (name, errors)


@pytest.mark.parametrize("bad_id", ["Project", "1project", "pro ject", "project\n", "a" * 65])
def test_an_id_that_is_not_a_storage_safe_slug_is_refused(bad_id):
    assert any("storage-safe slug" in e for e in _errors(_kind(id=bad_id)))


@pytest.mark.parametrize("field", ["title", "menuLabel", "menuDescription", "icon"])
def test_every_rendered_label_is_capped_in_utf16_units(field):
    assert not any("exceeds" in e for e in _errors(_kind(**{field: "x" * 120})))
    assert any(f"{field} exceeds 120" in e for e in _errors(_kind(**{field: "x" * 121})))
    # 61 astral characters are 61 code points and 122 UTF-16 units, the browser's count.
    assert any(f"{field} exceeds 120" in e for e in _errors(_kind(**{field: "\U0001f600" * 61})))


@pytest.mark.parametrize("agent", ["../evil", "a/b", "a b", "x" * 121])
def test_a_default_agent_that_is_not_a_bare_name_is_refused(agent):
    assert any(
        "defaultAgent must be a bare agent name" in e for e in _errors(_kind(defaultAgent=agent))
    )


def test_a_duplicate_id_is_refused():
    assert any("duplicate id 'project'" in e for e in _errors(_kind(), _kind()))


def test_more_kinds_than_the_cap_are_refused():
    at_cap = [_kind(id=f"k{i}") for i in range(MAX_SESSION_KINDS_PER_APP)]
    assert _errors(*at_cap) == []
    over = at_cap + [_kind(id="one-more")]
    assert any("kinds exceeds the limit of 4" in e for e in _errors(*over))


def test_a_non_array_block_parses_empty_but_is_reported():
    m = AppManifest.from_dict(_valid_manifest(contributes={"sessionKinds": {"id": "project"}}))
    assert m.contributes.sessionKinds == []
    assert m.contributes.bad_session_kinds is True
    assert any("contributes.sessionKinds must be an array" in e for e in m.validate())


def test_non_object_entries_are_counted_and_reported():
    m = AppManifest.from_dict(_valid_manifest(contributes={"sessionKinds": [_kind(), "x", 7]}))
    assert len(m.contributes.sessionKinds) == 1
    assert m.contributes.dropped_session_kinds == 2
    assert any("contributes.sessionKinds: 2 entries must be an object" in e for e in m.validate())


@pytest.mark.parametrize("entry", ["../../etc/passwd", "/abs/pane.mjs", "dist/../../x.mjs"])
def test_an_entry_outside_the_app_root_is_refused(entry):
    assert any("contributes.sessionKinds entry" in e for e in _errors(_kind(entry=entry)))


def test_a_kinds_only_manifest_is_signed_and_its_entry_and_labels_with_it():
    """The entry is an ESM module the dashboard mounts as a session's whole pane in its
    own origin: outside the signed bytes it could be repointed on a signed app."""
    m = AppManifest.from_dict(_valid_manifest(contributes={"sessionKinds": [_kind()]}))
    payload = m.signing_payload()
    assert b"sessionKinds" in payload and b"dist/pane.mjs" in payload
    for tamper in ({"entry": "dist/evil.mjs"}, {"menuLabel": "System Settings"}):
        tampered = _valid_manifest(contributes={"sessionKinds": [_kind(**tamper)]})
        assert AppManifest.from_dict(tampered).signing_payload() != payload, tamper


def test_a_manifest_without_kinds_signs_the_bytes_it_signed_before():
    m = AppManifest.from_dict(_valid_manifest())
    assert b"contributes" not in m.signing_payload()
