"""With the ``kas_subagents`` preview on, three ``deniedCommands`` entries reach KAS as kiro-cli sends them.

kiro-cli 3.5.0 upgraded three agent files and wrote these shell deny rules:
``rm \\| .*`` as ``*``, ``rm [|] .*`` as ``rm * *``, and ``rm .*|shred +x`` as
``rm *`` and ``shred *x``. On, the KAS translation sends exactly those for exactly
those entries, and the session-start notice says what each refuses. Off or never
set, and for every other entry, the translation is what it was. Kiro Crew's own
gate never refuses less either way.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import (
    CREW_SHELL_READONLY_KEY,
    shell_denies_carried,
    spec_shell_allowlist,
    tools_settings_to_permissions,
)
from kiro_crew.acp.kas_spec_denies import denied_command_items, uncarried_denied_commands
from kiro_crew.agent_sdk.spec_hooks import spec_keys_notice
from kiro_crew.config.loader import KiroCrewConfig

_ESCAPED_PIPE = "rm \\| .*"
_CLASS_PIPE = "rm [|] .*"
_PLUS_BRANCH = "rm .*|shred +x"

#: The entry -> the ``match`` list of the shell deny kiro-cli wrote for it.
_KIRO_CLI_WROTE: dict[str, list[str]] = {
    _ESCAPED_PIPE: ["*"],
    _CLASS_PIPE: ["rm * *"],
    _PLUS_BRANCH: ["rm *", "shred *x"],
}

#: The entry -> the ``match`` list of the shell deny sent with the preview off
#: (``None``: no rule), and whether the key is named as not carried.
_OFF: dict[str, tuple[list[str] | None, bool]] = {
    _ESCAPED_PIPE: (["rm | *"], False),
    _CLASS_PIPE: (None, True),
    _PLUS_BRANCH: (["rm *"], True),
}

#: Entries of the same kinds whose text is not one of the three.
_NOT_THE_THREE = [
    "git \\| .*",
    "rm \\| .*x",
    "rm [;] .*",
    "rm [|] .* -f",
    "rm .*|shred +y",
    "shred +x",
    "rm .*|shred .*",
    "(touch|mkdir) .*",
    "(sudo|) rm .*",
]


@pytest.fixture
def preview(monkeypatch):
    """Store the ``kas_subagents`` preview: ``True``, ``False`` or ``None`` (never set)."""

    def _set(value: bool | None) -> None:
        cfg = KiroCrewConfig()
        cfg.dashboard.feature_previews = {} if value is None else {"kas_subagents": value}
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))

    return _set


def _settings(*entries: str, key: str = "shell", **more) -> dict:
    return {key: {"deniedCommands": list(entries), **more}}


def _shell_rules(block: dict | None) -> list[dict]:
    return [r for r in (block or {}).get("rules", []) if r["capability"] == "shell"]


def _spec(settings: dict, **more) -> dict:
    return {"name": "mine", "prompt": "p", "tools": ["*"], "toolsSettings": settings, **more}


# ── on: the rule kiro-cli wrote ──────────────────────────────────────────────


@pytest.mark.parametrize("key", ["shell", "execute_bash"])
@pytest.mark.parametrize("entry", sorted(_KIRO_CLI_WROTE))
def test_the_entry_sends_the_rule_kiro_cli_wrote(preview, entry, key):
    preview(True)

    block, not_carried = tools_settings_to_permissions(_settings(entry, key=key))

    assert _shell_rules(block) == [
        {"capability": "shell", "match": _KIRO_CLI_WROTE[entry], "effect": "deny"}
    ]
    # Sent, but not as the regex the entry is: the key stays named.
    assert not_carried == [f"{key}.deniedCommands"]


@pytest.mark.parametrize("entry", sorted(_KIRO_CLI_WROTE))
def test_the_projected_entry_carries_that_rule(preview, entry):
    preview(True)

    projected = kas_agents.to_client_custom_agent("mine", _spec(_settings(entry)), "p")

    assert _shell_rules(projected.get("permissions")) == [
        {"capability": "shell", "match": _KIRO_CLI_WROTE[entry], "effect": "deny"}
    ]


def test_an_entry_beside_the_three_is_translated_as_ever(preview):
    preview(True)

    block, not_carried = tools_settings_to_permissions(
        _settings("git push .*", _CLASS_PIPE, "(touch|mkdir) .*", "foo(?=x)")
    )

    [rule] = _shell_rules(block)
    assert rule["match"] == ["git push *", "touch *", "mkdir *", "rm * *"]
    assert not_carried == ["shell.deniedCommands"]


@pytest.mark.parametrize("entry", _NOT_THE_THREE)
def test_an_entry_that_is_not_one_of_the_three_is_not_followed(preview, entry):
    preview(None)
    before = tools_settings_to_permissions(_settings(entry))
    preview(True)

    assert tools_settings_to_permissions(_settings(entry)) == before


# ── off or never set, and a config that cannot be read ───────────────────────


@pytest.mark.parametrize("value", [None, False], ids=["never-set", "off"])
@pytest.mark.parametrize("entry", sorted(_OFF))
def test_off_or_never_set_the_translation_is_what_it_was(preview, entry, value):
    preview(value)
    match, named = _OFF[entry]

    block, not_carried = tools_settings_to_permissions(_settings(entry))

    assert [r["match"] for r in _shell_rules(block)] == ([match] if match else [])
    assert not_carried == (["shell.deniedCommands"] if named else [])


@pytest.mark.parametrize("entry", sorted(_OFF))
def test_a_config_that_cannot_be_read_keeps_the_translation(monkeypatch, entry):
    def _unreadable(_cls, *a, **k):
        raise OSError("config.json unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_unreadable))
    match, _named = _OFF[entry]

    block, _not_carried = tools_settings_to_permissions(_settings(entry))

    assert [r["match"] for r in _shell_rules(block)] == ([match] if match else [])


def test_no_other_entry_reads_the_config(monkeypatch):
    def _never(_cls, *a, **k):
        raise AssertionError("the config was read for an entry that is not one of the three")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_never))

    for entry in _NOT_THE_THREE:
        tools_settings_to_permissions(_settings(entry))


# ── Kiro Crew's own gate never refuses less ──────────────────────────────────

_GATE_SPECS: dict[str, dict] = {
    **{f"alone-{i}": _spec(_settings(entry)) for i, entry in enumerate(sorted(_OFF))},
    "all-three": _spec(_settings(*sorted(_OFF))),
    "beside-a-plain-deny": _spec(_settings(_CLASS_PIPE, "git push .*")),
    "beside-an-unreadable-deny": _spec(_settings(_ESCAPED_PIPE, "foo(?=x)")),
    **{
        f"deny-by-default-{i}": _spec(
            _settings(entry, denyByDefault=True, allowedCommands=["git status.*"]),
            allowedTools=["execute_bash"],
        )
        for i, entry in enumerate(sorted(_OFF))
    },
    "auto-allow-readonly": _spec(
        _settings(_ESCAPED_PIPE, autoAllowReadonly=True), allowedTools=["execute_bash"]
    ),
}


def _gate_reads(spec: dict) -> dict:
    """What Kiro Crew's gate is handed for *spec*, each as a set that only refuses."""
    settings = spec["toolsSettings"]
    block, _ = tools_settings_to_permissions(settings)
    projected = kas_agents.to_client_custom_agent("mine", spec, "p")
    allowlist = spec_shell_allowlist(settings, spec.get("allowedTools"), block)
    return {
        "carried": shell_denies_carried(settings, block),
        "refused-by-the-gate": set(uncarried_denied_commands(settings, block)),
        "held-for-a-worker": {
            i["entry"] for i in denied_command_items(settings, block, top_level_split_carries=False)
        },
        "allowlist-kept": allowlist is not None,
        "allowlist": allowlist,
        "readonly-answered": bool(projected.get(CREW_SHELL_READONLY_KEY)),
    }


@pytest.mark.parametrize("name", sorted(_GATE_SPECS))
def test_the_gate_never_refuses_less_with_the_preview_on(preview, name):
    spec = _GATE_SPECS[name]
    preview(None)
    off = _gate_reads(spec)
    preview(True)
    on = _gate_reads(spec)

    # A deny the gate counted as not carried is still not carried.
    assert not (on["carried"] and not off["carried"])
    assert on["refused-by-the-gate"] >= off["refused-by-the-gate"]
    assert on["held-for-a-worker"] >= off["held-for-a-worker"]
    # A ``denyByDefault`` allowlist the gate kept is kept, entry for entry.
    if off["allowlist-kept"]:
        assert on["allowlist"] == off["allowlist"]
    # The read-only row never starts answering.
    assert not (on["readonly-answered"] and not off["readonly-answered"])


# ── the notice says what the entry refuses ───────────────────────────────────


def _notice(spec: dict, agent: str = "mine") -> str:
    keys = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    return spec_keys_notice(agent, list(keys)) if keys else ""


def test_the_escaped_pipe_entry_says_every_command_is_refused(preview):
    preview(True)

    notice = _notice(_spec(_settings(_ESCAPED_PIPE)))

    assert f"toolsSettings.shell.deniedCommands: {_ESCAPED_PIPE}" in notice
    assert "receives as kiro-cli sends it, so every shell command is refused" in notice
    # The engine takes the shell tool away under a deny on every command.
    assert "is refused and the agent may have no shell tool. That is more" in notice
    assert "no effect" not in notice
    assert "is not refused on this backend" not in notice


@pytest.mark.parametrize(
    "entry,refused",
    [(_CLASS_PIPE, "matching rm * * is refused"), (_PLUS_BRANCH, "matching rm * or shred *x is")],
)
def test_the_other_two_say_which_commands_are_refused(preview, entry, refused):
    preview(True)

    notice = _notice(_spec(_settings(entry)))

    assert refused in notice
    assert "That is more than the entry names." in notice
    assert "no effect" not in notice


def test_an_entry_nothing_carries_is_still_said_to_have_no_effect(preview):
    preview(True)

    notice = _notice(_spec(_settings(_CLASS_PIPE, "foo(?=x)")))

    assert "deniedCommands: foo(?=x), which this backend does not receive" in notice
    assert f"deniedCommands: {_CLASS_PIPE}, which this backend receives as kiro-cli" in notice


def test_a_bots_worker_is_told_its_shell_is_still_held(preview):
    preview(True)

    notice = _notice(_spec(_settings(_CLASS_PIPE)), agent="kirocrew-mate-worker")

    assert "matching rm * * is refused" in notice
    assert "this Bot's worker also refuses every shell command that is not read-only" in notice


@pytest.mark.parametrize("value", [None, False], ids=["never-set", "off"])
def test_off_or_never_set_the_notice_is_what_it_was(preview, value):
    preview(value)

    assert _notice(_spec(_settings(_ESCAPED_PIPE))) == ""
    notice = _notice(_spec(_settings(_CLASS_PIPE)))
    assert f"deniedCommands: {_CLASS_PIPE}, which this backend does not receive" in notice
    assert "kiro-cli sends it" not in notice
