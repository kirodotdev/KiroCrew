"""A profile registry holding valid JSON of the WRONG SHAPE must degrade, not raise.

``profiles.json`` is agent-writable, and ``load_registry`` parses it with
``raw.get("profiles", ...)``. Before this pin, a file containing ``[]`` (a list),
``"x"`` (a string) or ``{"profiles": 5}`` (a non-iterable member) escaped as an
AttributeError/TypeError -- past the caller and out as an HTTP 500 on EVERY route
that reads the registry, including the deploy surface that predates aws-control.
"""

import json

import pytest
from stray_line_helpers import cyclic_gc_quiesced  # noqa: F401 - fixture
from stray_line_helpers import too_deep_to_decode

from kiro_crew.deploy import profiles as profiles_mod

EMPTY = {"version": 2, "profiles": [], "default": ""}


def _over_digit_limit() -> str:
    """A registry whose ``default`` is a number past the integer digit limit.

    ``json.loads`` refuses it with a plain ``ValueError`` (not a ``JSONDecodeError``)
    from ``int()``, which is a different exception from the one truncated JSON raises.
    """
    return '{"profiles": [], "default": ' + "9" * 5000 + "}"


#: Files ``json.loads`` refuses with something other than a ``JSONDecodeError``.
REFUSED = {
    "nested-past-the-decoder": too_deep_to_decode,
    "over-digit-limit": _over_digit_limit,
}


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


def test_undecodable_registry_files_read_as_empty(tmp_path, monkeypatch):
    """A non-UTF-8 file degrades like a malformed one at every step of the chain.

    ``read_text(encoding="utf-8")`` raises ``UnicodeDecodeError`` (a ``ValueError``,
    not a ``JSONDecodeError``) on a file saved in a legacy code page. Each of the
    three reads must fall through, so all three files being latin-1 still ends
    at the empty default rather than raising out of ``load_registry``.
    """
    primary = tmp_path / "profiles.json"
    primary.write_bytes('{"profiles": [{"name": "café"}]}'.encode("latin-1"))
    legacy = tmp_path / "legacy.json"
    legacy.write_bytes('{"profiles": [{"name": "café"}]}'.encode("latin-1"))
    config = tmp_path / "config.json"
    config.write_bytes('{"profile": "café", "region": "us-east-1"}'.encode("latin-1"))
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: primary)
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: legacy)
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: config)

    try:
        out = profiles_mod.load_registry()
    except UnicodeDecodeError as exc:
        pytest.fail(f"an undecodable registry escaped load_registry: {exc!r}")
    assert out == EMPTY


@pytest.mark.usefixtures("cyclic_gc_quiesced")
@pytest.mark.parametrize("make", list(REFUSED.values()), ids=list(REFUSED))
def test_a_registry_json_loads_refuses_reads_as_empty_at_every_step(tmp_path, monkeypatch, make):
    """A file ``json.loads`` refuses outright degrades like one it cannot decode.

    A file nested past the interpreter's recursion limit raises ``RecursionError``,
    and one holding a number past the integer digit limit raises a plain
    ``ValueError``; neither is a ``JSONDecodeError``. Each of the three reads must
    fall through, so all three files being such still ends at the empty default
    rather than raising out of ``load_registry`` and 500ing every route.
    """
    content = make()
    primary = tmp_path / "profiles.json"
    primary.write_text(content, encoding="utf-8")
    legacy = tmp_path / "legacy.json"
    legacy.write_text(content, encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(content, encoding="utf-8")
    monkeypatch.setattr(profiles_mod, "_registry_path", lambda: primary)
    monkeypatch.setattr(profiles_mod, "_legacy_registry_path", lambda: legacy)
    monkeypatch.setattr(profiles_mod, "_legacy_config_path", lambda: config)

    try:
        out = profiles_mod.load_registry()
    except (ValueError, RecursionError) as exc:
        pytest.fail(f"a registry json.loads refuses escaped load_registry: {type(exc).__name__}")
    assert out == EMPTY


@pytest.mark.usefixtures("cyclic_gc_quiesced")
@pytest.mark.parametrize("make", list(REFUSED.values()), ids=list(REFUSED))
def test_a_refused_primary_still_falls_through_to_the_legacy_registry(tmp_path, monkeypatch, make):
    """Same fallback chain as a mis-shaped primary: the legacy registry is consulted."""
    primary = tmp_path / "profiles.json"
    primary.write_text(make(), encoding="utf-8")
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
