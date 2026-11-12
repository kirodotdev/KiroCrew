"""kiro-cli runs a Crew-owned skill view, and the view carries the kept ``autoApprove``.

kiro-cli has no ``autoApprove`` field, so a verb governance keeps runs without an
approval card only as an ``allowedTools`` entry. The native skill projection adds
that entry to the view of every agent, a user's own custom agent included, at every
spawn (``acp.kas_auto_approve.grant_in_skill_view``). No spec file is written, so the
grant follows the verb: an owner who narrows or deletes the list, or the opt-in
turned off, takes it out of the next view.
"""

from __future__ import annotations

import json
from typing import Any

import native_skill_projection_helpers
import pytest
from native_skill_projection_helpers import _alias_file

from kiro_crew.acp import skill_projection as projection
from kiro_crew.platform import governance

# The shared fixtures, bound here by name so pytest registers them for this module.
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
native_tree = native_skill_projection_helpers.native_tree
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)

_GRANT = "@builder-mcp/ReadInternalWebsites"
_OTHER = "@builder-mcp/SearchThings"


@pytest.fixture
def opt_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """An ungoverned host, and ``mcp.honour_auto_approve`` as the returned switch says."""
    honour = {"on": True}
    monkeypatch.setattr(governance, "may_skip_gate_now", lambda ref: True)
    monkeypatch.setattr(governance, "_auto_approve_is_honoured", lambda: honour["on"])
    return honour


def _write_custom(agents, verbs: list[str] | None) -> bytes:
    """The user's own agent, as the reporter wrote it: their server and its list."""
    server: dict[str, Any] = {"command": "builder-mcp"}
    if verbs is not None:
        server["autoApprove"] = verbs
    spec = {
        "name": "custom",
        "tools": ["fs_read", "@builder-mcp"],
        "allowedTools": ["fs_read"],
        "mcpServers": {"builder-mcp": server},
    }
    path = agents / "custom.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path.read_bytes()


def _granted(agents, project) -> list[str]:
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads(_alias_file(agents, prepared).read_text(encoding="utf-8"))
    return view["allowedTools"]


def test_a_custom_agent_s_view_grants_its_kept_auto_approve(native_tree, opt_in) -> None:
    """The reported case: a dashboard chat on a user's own agent runs this view."""
    _home, agents, project = native_tree
    authored = _write_custom(agents, ["ReadInternalWebsites"])
    assert _granted(agents, project) == ["fs_read", _GRANT]
    assert (agents / "custom.json").read_bytes() == authored


def test_the_opt_in_off_takes_the_grant_out_of_the_next_view(native_tree, opt_in) -> None:
    """The file still lists the verb, since Kiro Crew never rewrites it; the view asks
    governance again at every spawn."""
    _home, agents, project = native_tree
    _write_custom(agents, ["ReadInternalWebsites"])
    assert _GRANT in _granted(agents, project)
    opt_in["on"] = False
    assert _GRANT not in _granted(agents, project)


@pytest.mark.parametrize("verbs", [["SearchThings"], None], ids=["narrowed", "deleted"])
def test_an_owner_edit_takes_the_grant_out_of_the_next_view(
    native_tree, opt_in, verbs: list[str] | None
) -> None:
    _home, agents, project = native_tree
    _write_custom(agents, ["ReadInternalWebsites", "SearchThings"])
    assert _granted(agents, project) == ["fs_read", _GRANT, _OTHER]
    _write_custom(agents, verbs)
    assert _granted(agents, project) == (["fs_read", _OTHER] if verbs else ["fs_read"])


def test_a_ceiling_on_the_server_keeps_its_tools_prompting(
    native_tree, opt_in, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, agents, project = native_tree
    _write_custom(agents, ["ReadInternalWebsites"])
    monkeypatch.setattr(governance, "may_skip_gate_now", lambda ref: ref != "@builder-mcp")
    assert _granted(agents, project) == ["fs_read"]
