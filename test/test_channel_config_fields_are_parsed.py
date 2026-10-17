"""Every channel-config field an operator can set must actually be READ.

A field on one of the channel dataclasses is only half a setting. The other half
is a line in ``KiroCrewConfig.load()`` pulling it out of the JSON section. Miss
that line and the field silently keeps its dataclass default forever: the schema
advertises it, the dashboard writes it to ``config.json``, ``GET`` reads it back
from disk and shows the operator their own choice — and the running channel never
sees it. Nothing else in the build reports it. `telegram.show_thinking` shipped in
exactly that state; a settings toggle, a schema entry, and screenshots of it
working, over a value the loader never looked at.

So this checks the round trip rather than the declaration: write a NON-DEFAULT
value into a section, load, and require the loaded config to carry it. That is the
only formulation that can fail for the right reason — asserting the field exists
passes with the parse line missing, and asserting the parse line exists by reading
the source is a grep test that a differently-spelled but correct parse would break.

Scope is the channel configs, because that is where the shape recurs: nine
sections, each parsed by its own hand-written block of ``section.get(...)`` calls,
none of which is derived from the dataclass.
"""

from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.config.loader import KiroCrewConfig

#: Config section name -> the attribute on ``KiroCrewConfig`` holding it. The
#: channels only; a section whose parse block is generated rather than hand-written
#: does not have this failure mode.
_CHANNEL_SECTIONS = (
    "telegram",
    "discord",
    "slack",
    "webex",
    "wecom",
    "weixin",
    "teams",
    "imessage",
)

#: Fields deliberately not read from the section, with the reason. Anything not
#: listed here MUST round-trip, so a newly added field is covered by default
#: rather than by someone remembering to extend this test.
_NOT_FROM_JSON: dict[tuple[str, str], str] = {
    # Nested lists of dataclasses have their own parsers and their own tests; the
    # scalar round trip below cannot synthesize a valid entry for them.
    ("telegram", "accounts"): "parsed by _parse_telegram_accounts",
    ("slack", "allowed_users"): "list of dicts with its own validator",
    ("slack", "tracking_channels"): "parsed by _validate_tracking_channels",
    # Deliberately NOT sourced from config.json: the Azure Bot secret is env-only
    # (MICROSOFT_APP_PASSWORD) so the credential stays out of a file the agent can
    # read. Verified against the parse site, which hardcodes "" with that rationale.
    ("teams", "app_password"): "env-only credential, never read from config.json",
}

#: Enum-validated fields, with a VALID non-default probe. Listed rather than
#: exempted: an exemption removes coverage, and these are exactly the fields where
#: a rejected arbitrary probe is indistinguishable from a field the loader never
#: read — which is the bug this file exists to catch.
_ENUM_PROBES: dict[tuple[str, str], str] = {
    ("imessage", "service"): "sms",
    ("telegram", "forum_activation"): "mention",
}

#: Fields whose loader CLAMPS to a range, so the probe has to land inside it. A
#: probe of default+7 on a percentage would be clamped back to the default and
#: read as "never parsed".
_PERCENT_SUFFIX = "_pct"


def _probe_value(section: str, name: str, current: Any) -> Any:
    """A value of *current*'s type that differs from it, or ``None`` to skip.

    Must differ, or a round trip proves nothing: a parse line that reads the wrong
    key still "survives" a value equal to the default. Must also be VALID, or a
    clamp or a validator turns it back into the default and the test reports a
    parsed field as unparsed.
    """
    enum_probe = _ENUM_PROBES.get((section, name))
    if enum_probe is not None:
        assert enum_probe != current, f"{section}.{name} enum probe equals the default"
        return enum_probe
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        if name.endswith(_PERCENT_SUFFIX):
            # Inside 1..100, and different from the default either way.
            return 42 if current != 42 else 43
        return int(current) + 7
    if isinstance(current, str):
        return "probe-value" if current != "probe-value" else "probe-other"
    return None


def _scalar_fields(section: str) -> list[tuple[str, Any]]:
    """``(name, probe_value)`` for each scalar field of *section*'s dataclass."""
    cfg = KiroCrewConfig()
    obj = getattr(cfg, section)
    assert is_dataclass(obj), section
    out: list[tuple[str, Any]] = []
    for f in fields(obj):
        if (section, f.name) in _NOT_FROM_JSON:
            continue
        probe = _probe_value(section, f.name, getattr(obj, f.name))
        if probe is None:
            continue
        out.append((f.name, probe))
    return out


@pytest.mark.parametrize("section", _CHANNEL_SECTIONS)
def test_every_scalar_channel_field_survives_a_load(
    section: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = _scalar_fields(section)
    # A section that yielded nothing would make this test vacuous — the exact
    # failure mode it exists to catch, one level up.
    assert probes, f"no scalar fields discovered for {section!r}"

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({section: {name: value for name, value in probes}}), encoding="utf-8"
    )

    cfg = KiroCrewConfig.load()
    loaded = getattr(cfg, section)
    unread = [
        f"{section}.{name} (wrote {value!r}, loaded {getattr(loaded, name)!r})"
        for name, value in probes
        if getattr(loaded, name) != value
    ]
    assert not unread, (
        "declared but never read out of config.json — add the missing "
        f"`{section}_data.get(...)` line to KiroCrewConfig.load(), or list the "
        f"field in _NOT_FROM_JSON with the reason: {unread}"
    )


def test_the_side_tables_name_only_real_fields() -> None:
    """A stale entry in either table silently un-covers a field."""
    cfg = KiroCrewConfig()
    stale = [
        f"{table}: {section}.{name}"
        for table, keys in (("_NOT_FROM_JSON", _NOT_FROM_JSON), ("_ENUM_PROBES", _ENUM_PROBES))
        for section, name in keys
        if name not in {f.name for f in fields(getattr(cfg, section))}
    ]
    assert not stale, f"names fields that no longer exist: {stale}"


def test_an_unreadable_activation_degrades_to_the_narrower_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not widen who the bot answers.

    ``always`` starts a turn for every message in an allow-listed Topic, and a
    Topic is a SHARED space, so resolving a malformed value to it would let a typo
    grant participation nobody asked for. Same posture as
    ``WeixinTransport.authorize`` on an unrecognized ``dm_policy``.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"telegram": {"forum_activation": "menshun"}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().telegram.forum_activation == "mention"


def test_an_absent_activation_still_takes_the_documented_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not setting the key is not the same act as setting it wrong.

    Forum topics are already opt-in twice (``allow_forum`` plus a chat allow-list),
    so an operator who got that far and left this key alone accepts the default.
    Narrowing that case too would be a silent behaviour change for every existing
    forum operator, which is what the typo path is guarding against in reverse.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"telegram": {"allow_forum": True}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().telegram.forum_activation == "always"

    # An explicitly EMPTY value is the absent case, not a typo: it names nothing.
    (tmp_path / "config.json").write_text(
        json.dumps({"telegram": {"forum_activation": ""}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().telegram.forum_activation == "always"


def test_telegram_does_not_advertise_an_activation_it_cannot_express() -> None:
    """``observe`` and ``review`` are excluded on purpose, not by omission.

    ``observe`` needs the channel-history buffer only Slack populates, and feeding
    it would put non-owner prose into the prompt unfenced. ``review`` is a second
    rendering mode built on Slack Block Kit ephemerals. Advertising either would
    give the operator a mode that silently behaves like a different one.
    """
    from kiro_crew.config.loader import _VALID_ACTIVATIONS, TELEGRAM_ACTIVATIONS

    assert TELEGRAM_ACTIVATIONS == {"always", "mention", "off"}
    assert TELEGRAM_ACTIVATIONS < _VALID_ACTIVATIONS


def test_telegram_show_thinking_specifically_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field that shipped unparsed, pinned by name.

    The parametrized test above covers it generically. This one names it so the
    regression is legible in a failure list, and because it is the one field with a
    settings toggle and a screenshot asserting it works.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"telegram": {"show_thinking": True}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().telegram.show_thinking is True
