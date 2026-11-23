"""The two readings of the KAS sub-agents preview that one session start takes agree.

A sub-agent run's session start reads the preview twice: once to decide whether the
run takes its founding chat's grants (``kas_parent_grants.preview_on``), and once
where the run's entry is projected, to decide whether a shell grant lifts
``denyByDefault`` (``kas_shell_grant_lift``). Both ask the one reader of the switch
with the same config: the one the gateway last applied, else ``config.json``. A
config that cannot be read is off to both.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_kas_parent_grants import (  # noqa: F401 -- the fixtures register by import
    _GENEROUS,
    _NARROW,
    _OWNS_AND_GRANTS,
    _child,
    _isolated,
    _verdicts,
    agents,
    sessions,
)
from test_kas_parent_limits import _write

from kiro_crew.acp import kas_engine_subagents, kas_parent_grants, kas_shell_grant_lift
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_path


def _applied(monkeypatch: pytest.MonkeyPatch, value: bool | None) -> None:
    """The config the gateway last applied stores *value*; ``None`` is none applied."""
    cfg = None
    if value is not None:
        cfg = KiroCrewConfig()
        cfg.dashboard.feature_previews = {kas_engine_subagents.KAS_SUBAGENTS_PREVIEW: value}
    monkeypatch.setattr(live, "snapshot", lambda: cfg)


def _on_disk(value: bool) -> None:
    """The test's own ``config.json`` stores *value*."""
    path = Path(config_path())
    previews = {kas_engine_subagents.KAS_SUBAGENTS_PREVIEW: value}
    fresh = path.with_name("config.json.new")
    fresh.write_text(json.dumps({"dashboard": {"feature_previews": previews}}), encoding="utf-8")
    fresh.replace(path)


def _readings() -> tuple[bool, bool]:
    """``(the grants reader, the lift reader)``, each as "the preview is on"."""
    return (
        kas_parent_grants.preview_on(),
        not kas_shell_grant_lift.shell_grant_lifts_deny_by_default(),
    )


@pytest.mark.parametrize(
    "applied, on_disk, on",
    [
        (True, False, True),
        (False, True, False),
        (None, True, True),
        (None, False, False),
        (True, True, True),
        (False, False, False),
    ],
    ids=[
        "applied-on-file-off",
        "applied-off-file-on",
        "none-applied-file-on",
        "none-applied-file-off",
        "both-on",
        "both-off",
    ],
)
def test_both_readers_take_the_applied_config_and_then_the_file(monkeypatch, applied, on_disk, on):
    _on_disk(on_disk)
    _applied(monkeypatch, applied)

    assert _readings() == (on, on)


@pytest.mark.parametrize(
    "applied, on", [(None, False), (True, True), (False, False)], ids=["none", "on", "off"]
)
def test_a_config_file_that_cannot_be_read_is_the_same_to_both(monkeypatch, applied, on):
    """With no applied config both read off; with one, neither reads the file."""

    def unreadable() -> None:
        raise OSError("config unreadable")

    _on_disk(True)
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(unreadable))
    _applied(monkeypatch, applied)

    assert _readings() == (on, on)


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", [True, False], ids=["applied-on", "applied-off"])
@pytest.mark.parametrize("founder", [_NARROW["name"], _GENEROUS["name"]])
async def test_one_session_start_is_built_on_one_reading(
    agents, sessions, monkeypatch, applied, founder  # noqa: F811
):
    """A run whose own file lists the shell beside ``denyByDefault``, under a founder
    that grants nothing and under one that grants the shell: with the file saying
    the opposite of the applied config, its entry and its verdicts are the ones of
    the applied value alone."""
    for spec in (_GENEROUS, _OWNS_AND_GRANTS):
        _write(agents, spec)
    child = _OWNS_AND_GRANTS["name"]

    async def built(on_disk: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        _on_disk(on_disk)
        _applied(monkeypatch, applied)
        batch = await _child(sessions, founder, child)
        return batch, _verdicts(batch, child)

    whole = await built(applied)
    split = await built(not applied)

    assert split == whole
