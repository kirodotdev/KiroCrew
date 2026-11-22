"""``kirocrew security deny-list``: the built-in half follows ``denied_commands.json``.

The gate drops a built-in rule the operator turned off (``disabled_ids`` or
``disable_all``) unless a floor or a governance pin keeps it on. The command must
list the same rules as enforced, so an operator auditing a host from a terminal
reads what the gate does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cli_commands_coverage_helpers import _ns

from kiro_crew import cli_commands as cc
from kiro_crew.security import BUILTIN_DENIED_RULES, floor_enforced_builtin_command_ids

_ENFORCED = "🔒 Built-in deny patterns (enforced):"
_OFF = "🔓 Built-in deny patterns turned off in denied_commands.json (not enforced):"


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: dict | None) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    if state is not None:
        (tmp_path / "denied_commands.json").write_text(json.dumps(state), encoding="utf-8")
    cc._security(_ns(sec_action="deny-list"))


def _sections(out: str) -> tuple[list[str], list[str]]:
    """The patterns listed as enforced and as turned off, notes stripped."""
    enforced_half, _, off_half = out.partition(_OFF)
    enforced_half = enforced_half.split(_ENFORCED, 1)[1].split("\n\n", 1)[0]
    enforced = [
        line[4:].split("  (", 1)[0]
        for line in enforced_half.splitlines()
        if line.startswith("  ✗ ")
    ]
    off_half = off_half.split("\n\n", 1)[0]
    off = [line[4:] for line in off_half.splitlines() if line.startswith("  - ")]
    return enforced, off


def _rule(rule_id: str) -> str:
    return next(r.pattern for r in BUILTIN_DENIED_RULES if r.id == rule_id)


def _gate_builtin_set() -> set[str]:
    from kiro_crew.hook_runtime.denied_commands import (
        hooks_config_from_config_dict,
        resolve_effective_denied_regexes,
    )

    effective = set(resolve_effective_denied_regexes(hooks_config_from_config_dict({})))
    return {r.pattern for r in BUILTIN_DENIED_RULES} & effective


def test_no_opt_out_file_lists_every_builtin_as_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, monkeypatch, None)
    out = capsys.readouterr().out
    enforced, off = _sections(out)
    assert enforced == [r.pattern for r in BUILTIN_DENIED_RULES]
    assert off == [] and _OFF not in out
    assert "always enforced" not in out
    # Nothing was opted out, so no rule needs a reason for staying on.
    assert "(always on: floor rule)" not in out


def test_disabled_id_is_listed_as_turned_off_not_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, monkeypatch, {"disabled_ids": ["credential-exfil-s3-cp"]})
    enforced, off = _sections(capsys.readouterr().out)
    pattern = _rule("credential-exfil-s3-cp")
    assert pattern not in enforced
    assert off == [pattern]


def test_disable_all_keeps_only_floor_rules_enforced_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, monkeypatch, {"disable_all": True})
    out = capsys.readouterr().out
    enforced, off = _sections(out)
    floor = {
        r.pattern for r in BUILTIN_DENIED_RULES if r.id in floor_enforced_builtin_command_ids()
    }
    assert set(enforced) == floor
    assert set(off) == {r.pattern for r in BUILTIN_DENIED_RULES} - floor
    for pattern in floor:
        assert f"  ✗ {pattern}  (always on: floor rule)" in out


@pytest.mark.parametrize(
    "state",
    [
        {"disabled_ids": ["credential-exfil-s3-cp"]},
        {"disable_all": True, "disabled_ids": ["credential-exfil-s3-cp"], "user_added": []},
    ],
)
def test_enforced_regex_rules_match_the_gate(
    state: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Floor rules are enforced by their own floor, not the regex tier the gate
    resolves, so the comparison is over the rest of the catalog."""
    _run(tmp_path, monkeypatch, state)
    enforced, _ = _sections(capsys.readouterr().out)
    floor = {
        r.pattern for r in BUILTIN_DENIED_RULES if r.id in floor_enforced_builtin_command_ids()
    }
    assert set(enforced) - floor == _gate_builtin_set() - floor


_PROFILE_ONLY = (
    "🔐 Built-in deny patterns turned off but kept on only for sessions "
    "bound to a governance profile that pins them:"
)


def test_profile_only_pin_is_not_listed_as_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rule pinned by one profile only is enforced for sessions bound to it, not
    for an unbound session, so it must not appear under the unscoped heading."""
    from kiro_crew.security import denied_rules

    rid = "credential-exfil-s3-cp"
    monkeypatch.setattr(denied_rules, "pinned_builtin_command_ids", lambda: set())
    monkeypatch.setattr(denied_rules, "pinned_builtin_command_ids_for_snapshot", lambda: {rid})
    _run(tmp_path, monkeypatch, {"disabled_ids": [rid]})
    out = capsys.readouterr().out
    enforced, off = _sections(out)
    pattern = _rule(rid)
    assert pattern not in enforced
    assert pattern not in off
    assert f"{_PROFILE_ONLY}\n  ~ {pattern}" in out
    assert pattern not in _gate_builtin_set()


def test_ceiling_pin_stays_enforced_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kiro_crew.security import denied_rules

    rid = "credential-exfil-s3-cp"
    monkeypatch.setattr(denied_rules, "pinned_builtin_command_ids", lambda: {rid})
    _run(tmp_path, monkeypatch, {"disabled_ids": [rid]})
    out = capsys.readouterr().out
    enforced, off = _sections(out)
    pattern = _rule(rid)
    assert pattern in enforced and pattern not in off
    assert f"  ✗ {pattern}  (kept on: governance pin)" in out
    assert _PROFILE_ONLY not in out
    assert pattern in _gate_builtin_set()


def test_lock_note_only_on_a_rule_the_operator_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pinned rule nobody turned off is on anyway, so it gets no lock note even
    when another rule is opted out."""
    from kiro_crew.security import denied_rules

    off_id = "credential-exfil-s3-cp"
    pinned_id = next(r.id for r in BUILTIN_DENIED_RULES if r.id != off_id)
    monkeypatch.setattr(denied_rules, "pinned_builtin_command_ids", lambda: set())
    monkeypatch.setattr(
        denied_rules, "pinned_builtin_command_ids_for_snapshot", lambda: {pinned_id}
    )
    _run(tmp_path, monkeypatch, {"disabled_ids": [off_id]})
    out = capsys.readouterr().out
    enforced, _ = _sections(out)
    pattern = _rule(pinned_id)
    assert pattern in enforced
    assert f"  ✗ {pattern}\n" in out
