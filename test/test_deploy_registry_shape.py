"""A profile registry holding valid JSON of the WRONG SHAPE must degrade, not raise.

``profiles.json`` is agent-writable, and ``load_registry`` parses it with
``raw.get("profiles", ...)``. Before this pin, a file containing ``[]`` (a list),
``"x"`` (a string) or ``{"profiles": 5}`` (a non-iterable member) escaped as an
AttributeError/TypeError -- past the caller and out as an HTTP 500 on EVERY route
that reads the registry, including the deploy surface that predates aws-control.
"""

import json

import pytest

from kiro_crew.deploy import profiles as profiles_mod

EMPTY = {"version": 2, "profiles": [], "default": ""}


@pytest.mark.parametrize(
    "content",
    [
        "[]",  # a list: raw.get does not exist
        '"just a string"',  # a str: same
        "123",  # a number: same
        "null",  # None: same
        '{"profiles": 5}',  # dict but the member is not iterable -> TypeError
        '{"profiles": "abc"}',  # iterable of str -> p.get does not exist
    ],
    ids=["list", "string", "number", "null", "non-iterable", "string-members"],
)
def test_wrong_shaped_registry_reads_as_empty(tmp_path, monkeypatch, content):
    reg = tmp_path / "profiles.json"
    reg.write_text(content, encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: reg)
    # No legacy files to fall through to, so the terminal fallback applies.
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: tmp_path / "nope.json")
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: tmp_path / "nope2.json")

    assert profiles_mod.load_registry() == EMPTY


def test_a_wrong_shaped_primary_still_falls_through_to_the_legacy_registry(tmp_path, monkeypatch):
    """The shape guard must reuse the EXISTING fallback chain, not short-circuit it.

    A mis-shaped primary file has to behave like an unreadable one: the legacy
    registry is still consulted. A guard that returned empty immediately would
    silently drop a real migration.
    """
    primary = tmp_path / "profiles.json"
    primary.write_text("[]", encoding="utf-8")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps({"version": 2, "profiles": [{"name": "p", "region": "us-west-2"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: primary)
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: legacy)
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: tmp_path / "nope.json")

    out = profiles_mod.load_registry()
    assert [p["name"] for p in out["profiles"]] == ["p"]


def test_a_well_formed_registry_is_unaffected(tmp_path, monkeypatch):
    reg = tmp_path / "profiles.json"
    reg.write_text(
        json.dumps(
            {"version": 2, "default": "a", "profiles": [{"name": "a", "region": "us-east-1"}]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: reg)

    out = profiles_mod.load_registry()
    assert out["default"] == "a"
    assert [p["name"] for p in out["profiles"]] == ["a"]
