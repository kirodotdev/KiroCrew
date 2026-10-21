"""The mate-owned-config RFC's load-bearing claims, proven offline.

Covers RFC *mate-owned-agent-config* §5.3/§5.4 where no live pod is needed:

* resolution reads BOTH ``~/.kiro/agents/`` and ``<kiro home>/mates/agents/``,
  user-level wins a name collision, and the crewmate's prompt anchors to its own
  folder (``resolve_projected_spec``);
* the session agent picker (``list_agents``) does NOT see a crewmate file;
* the eager copy projects to the SAME ``customAgents`` payload the source agent
  would (Phase-2 payload equivalence), with a ``file://`` prompt inlined so no
  link back to the source survives;
* the loader's materialized index recognises a crewmate id as a declared agent,
  so a binding ``kiro_agent=<id>`` survives the dangling self-heal and dispatches;
* the mate-tools grant neither forks nor writes the owned file;
* the Capabilities editor's in-place mate path (``mate_capabilities``) writes
  the owned file with no fork and refuses a stale revision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness.kas import resolve_projected_spec
from kiro_crew.acp.kas_agents import build_kas_custom_agents, expand_kiro_cli_tool_names
from kiro_crew.mate_config import (
    has_mate_config,
    mate_config_path,
    write_mate_config,
)


@pytest.fixture
def layout(monkeypatch, tmp_path):
    """A user-level agents dir, its sibling ``mates/agents`` dir, and a checkout.

    The mate folder is ``<kiro home>/mates/agents`` -- a SIBLING of
    ``<kiro home>/agents``, not under the data home (per
    rfc-mate-owned-agent-config: ``~/.kiro/mates/agents`` next to
    ``~/.kiro/agents``). So both dirs hang off one ``kiro home`` and the
    fixture patches ``kiro_home``.
    """
    kiro = tmp_path / "home"  # the kiro home (~/.kiro)
    user = kiro / "agents"
    user.mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    (checkout / ".kiro" / "agents").mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user, mates, checkout


def _write(directory: Path, name: str, spec: dict) -> None:
    (directory / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


# ── §5.3 resolution reads both folders ──────────────────────────────────────


def test_a_crewmate_file_resolves_when_the_user_level_does_not_declare_it(layout):
    _user, mates, checkout = layout
    _write(mates, "zxc", {"name": "zxc", "prompt": "mate prompt", "tools": ["fs_read"]})

    spec, prompt_dir = resolve_projected_spec(paths_mod.kiro_agents_dir(), "zxc", str(checkout))

    assert spec["prompt"] == "mate prompt"
    assert spec["tools"] == ["fs_read"]
    # The prompt anchors to the crewmate folder, so a ``file://`` prompt inlines
    # from there -- never the user level, never the checkout.
    assert prompt_dir == mates


def test_a_crewmate_prompt_inlines_from_its_own_folder(layout):
    _user, mates, checkout = layout
    (mates / "p.md").write_text("mate prompt body", encoding="utf-8")
    _write(mates, "zxc", {"name": "zxc", "prompt": "file://./p.md", "tools": ["*"]})

    entry = build_kas_custom_agents(*_resolved(mates, checkout, "zxc"))[0]

    assert entry["prompt"] == "mate prompt body"


def test_a_user_level_agent_wins_a_name_collision_with_a_crewmate(layout):
    user, mates, checkout = layout
    _write(user, "shared", {"name": "shared", "prompt": "user prompt", "tools": ["@kept"]})
    _write(mates, "shared", {"name": "shared", "prompt": "mate prompt", "tools": ["*"]})

    spec, prompt_dir = resolve_projected_spec(paths_mod.kiro_agents_dir(), "shared", str(checkout))

    # User-level is authored and read first; the crewmate folder is only reached
    # when the user level does not declare the name.
    assert spec["prompt"] == "user prompt"
    assert prompt_dir == paths_mod.kiro_agents_dir()


def test_an_ordinary_agent_with_no_crewmate_file_still_resolves_user_level(layout):
    user, _mates, checkout = layout
    _write(user, "solo", {"name": "solo", "prompt": "user prompt", "tools": ["fs_read"]})

    spec, prompt_dir = resolve_projected_spec(paths_mod.kiro_agents_dir(), "solo", str(checkout))

    assert spec["prompt"] == "user prompt"
    assert prompt_dir == paths_mod.kiro_agents_dir()


def test_a_missing_mates_dir_does_not_break_resolution(layout):
    user, mates, checkout = layout
    # Remove the crewmate dir entirely: an ordinary agent must still resolve, and
    # a non-existent folder is "absent", not "unreadable".
    for child in mates.iterdir():
        child.unlink()
    mates.rmdir()
    _write(user, "solo", {"name": "solo", "prompt": "user prompt", "tools": ["fs_read"]})

    spec, _ = resolve_projected_spec(paths_mod.kiro_agents_dir(), "solo", str(checkout))

    assert spec["prompt"] == "user prompt"


# ── §5.3 the picker does not see a crewmate ─────────────────────────────────


def test_the_session_agent_picker_does_not_list_a_crewmate(layout):
    from kiro_crew.agent_discovery import list_agents

    user, mates, _checkout = layout
    _write(user, "solo", {"name": "solo", "prompt": "user prompt"})
    _write(mates, "zxc", {"name": "zxc", "prompt": "mate prompt"})

    names = {a.name for a in list_agents(agents_dir=paths_mod.kiro_agents_dir())}

    assert "solo" in names
    assert "zxc" not in names  # the folder the picker reads is user-level only


# ── §5.4 eager copy + payload equivalence ───────────────────────────────────


def _resolved(agents_dir: Path, checkout: Path, agent: str):
    spec, prompt_dir = resolve_projected_spec(
        paths_mod.kiro_agents_dir() if agents_dir is None else agents_dir,
        agent,
        str(checkout),
    )
    return prompt_dir, agent, spec


def test_write_mate_config_copies_payload_and_declares_the_crewmate_id(layout):
    user, mates, _checkout = layout
    _write(
        user,
        "builder",
        {
            "name": "builder",
            "prompt": "builder prompt",
            "tools": ["fs_read", "@kirocrew-core"],
            "allowedTools": ["fs_read"],
            "mcpServers": {"demo": {"command": "/bin/echo"}},
        },
    )

    path = write_mate_config("zxc", "builder")

    assert path == mate_config_path("zxc")
    assert has_mate_config("zxc")
    written = json.loads(path.read_text(encoding="utf-8"))
    # Declares the crewmate id, not the source -- so it resolves under its own id.
    assert written["name"] == "zxc"
    # Every payload field copied verbatim.
    assert written["tools"] == ["fs_read", "@kirocrew-core"]
    assert written["allowedTools"] == ["fs_read"]
    assert written["mcpServers"] == {"demo": {"command": "/bin/echo"}}
    assert written["prompt"] == "builder prompt"


def test_write_mate_config_inlines_a_file_prompt_and_severs_the_link(layout):
    user, _mates, _checkout = layout
    (user / "prompts").mkdir()
    (user / "prompts" / "b.md").write_text("literal builder prompt", encoding="utf-8")
    _write(
        user,
        "builder",
        {"name": "builder", "prompt": "file://./prompts/b.md", "tools": ["*"]},
    )

    path = write_mate_config("zxc", "builder")
    written = json.loads(path.read_text(encoding="utf-8"))

    # The prompt is literal text now; no ``file://`` link back to the source.
    assert written["prompt"] == "literal builder prompt"
    assert "file://" not in written["prompt"]


def test_the_copy_projects_to_the_same_payload_as_the_source(layout):
    """RFC Phase-2 payload equivalence: before == after."""
    user, mates, checkout = layout
    _write(
        user,
        "builder",
        {
            "name": "builder",
            "prompt": "builder prompt",
            "tools": ["fs_read", "@kirocrew-core"],
            "allowedTools": ["fs_read"],
        },
    )

    # before: project the source agent as it stands today.
    before = build_kas_custom_agents(*_resolved(user, checkout, "builder"))[0]

    # after: eager-copy, repoint happens on the binding; project the crewmate file.
    write_mate_config("zxc", "builder")
    after = build_kas_custom_agents(*_resolved(mates, checkout, "zxc"))[0]

    # The id differs by construction (that IS the repoint); everything the
    # projection decides about behaviour is identical.
    assert before["prompt"] == after["prompt"]
    assert before["tools"] == after["tools"]
    assert before.get("permissions") == after.get("permissions")
    assert after["tools"] == expand_kiro_cli_tool_names(["fs_read", "@kirocrew-core"])


def test_write_mate_config_overwrites_an_existing_crewmate_file(layout):
    user, _mates, _checkout = layout
    _write(user, "builder", {"name": "builder", "prompt": "v1", "tools": ["*"]})
    write_mate_config("zxc", "builder")
    _write(user, "builder", {"name": "builder", "prompt": "v2", "tools": ["*"]})

    path = write_mate_config("zxc", "builder")

    assert json.loads(path.read_text(encoding="utf-8"))["prompt"] == "v2"


# ── the mate-tools grant never forks and never writes the owned file ─────────


def test_mate_tools_grant_skips_when_no_owned_file_exists(layout, monkeypatch):
    """The grant does not fork: with no owned mate file it returns mate_file_missing."""
    from types import SimpleNamespace

    from kiro_crew import member_mate_tools
    from kiro_crew.config import loader

    _user, _mates, _checkout = layout
    # A binding whose kiro_agent is NOT a mate id (eager copy never ran).
    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(
                agents={"A": SimpleNamespace(kiro_agent="some-template", workspace="")}
            )
        ),
    )
    monkeypatch.setattr(loader, "_invalidate_config_cache", lambda: None)
    assert (
        member_mate_tools.grant_mate_tools_to_new_member("A", catalog=lambda p: {})
        == member_mate_tools.MATE_FILE_MISSING
    )


def test_write_mate_config_refuses_an_absent_source(layout):
    from kiro_crew.mate_config import MateConfigError

    _user, _mates, _checkout = layout
    with pytest.raises(MateConfigError):
        write_mate_config("zxc", "does-not-exist")


# ── §5.3 the loader's materialized index recognises a crewmate id ───────────


def test_a_crewmate_id_is_a_declared_agent_in_the_materialized_index(layout, monkeypatch):
    from kiro_crew.config import loader

    user, mates, _checkout = layout
    _write(user, "solo", {"name": "solo", "prompt": "p"})
    _write(mates, "zxc", {"name": "zxc", "prompt": "mate prompt"})
    # The loader reads kiro_agents_dir / mate_agents_dir through its OWN imports.
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: mates)

    loader.refresh_materialized_agents()
    declared = loader._MATERIALIZED_AGENTS

    assert "solo" in declared
    assert "zxc" in declared  # survives the dangling self-heal, dispatches


# ── §5.4 Capabilities edit IN PLACE (no fork) -- mate_capabilities ───────────


def _mate_edit_env(layout, monkeypatch, member="qwe", spec=None):
    """A mate owned file on disk plus a stub binding, for the in-place edit path."""
    import kiro_crew.mate_capabilities as mc

    _user, mates, _checkout = layout
    spec = spec or {"name": member, "prompt": "hi", "tools": [], "allowedTools": []}
    (mates / f"{member}.json").write_text(json.dumps(spec), encoding="utf-8")
    # mate_config + mate_capabilities resolve the owned path through paths_mod
    # (kiro home is already patched by the layout fixture), so has_mate_config,
    # mate_config_path and mate_agents_dir all land in the layout's mates dir.
    monkeypatch.setattr(mc, "mate_agents_dir", lambda: mates)
    return mc, mates


def test_binding_is_mate_true_only_when_binding_points_at_own_id(layout, monkeypatch):
    mc, _mates = _mate_edit_env(layout, monkeypatch)

    class _Binding:
        def __init__(self, kiro_agent):
            self.kiro_agent = kiro_agent

    class _Cfg:
        def __init__(self, ka):
            self.agents = {"qwe": _Binding(ka)}

    # Binding at its own id + owned file exists -> mate.
    monkeypatch.setattr(mc.KiroCrewConfig, "load", staticmethod(lambda: _Cfg("qwe")))
    assert mc.binding_is_mate("qwe") is True
    # Binding on a shared template -> NOT a mate (ordinary fork path).
    monkeypatch.setattr(mc.KiroCrewConfig, "load", staticmethod(lambda: _Cfg("kirocrew")))
    assert mc.binding_is_mate("qwe") is False


def test_mate_edit_writes_the_owned_file_in_place_no_fork(layout, monkeypatch):
    """A skill-add lands in mates/agents/<id>.json; no crew- draft, binding untouched."""
    mc, mates = _mate_edit_env(layout, monkeypatch)
    catalog = {"kiro-user/agent-sop-author": "skill://agent-sop-author"}

    before = json.loads((mates / "qwe.json").read_text())
    revision = mc._revision(before)
    body = {
        "revision": revision,
        "operations": [
            {
                "section": "skills",
                "id": "kiro-user/agent-sop-author",
                "action": "set",
                "value": True,
            }
        ],
        "preview_token": revision,
        "accept_parent": [],
        "accept_members": [],
    }
    result = mc.put("qwe", body, catalog)

    after = json.loads((mates / "qwe.json").read_text())
    # The skill's resource landed in the OWNED file.
    assert "skill://agent-sop-author" in after.get("resources", [])
    # name stays pinned to the id; no fork draft file anywhere.
    assert after["name"] == "qwe"
    assert not list(mates.glob("crew-*.json"))
    assert result["mode"] == "mate"


def test_mate_edit_refuses_a_stale_revision(layout, monkeypatch):
    mc, mates = _mate_edit_env(layout, monkeypatch)
    from kiro_crew.agent_capabilities import CapabilityError

    body = {
        "revision": "not-the-current-digest",
        "operations": [],
        "preview_token": "x",
        "accept_parent": [],
        "accept_members": [],
    }
    with pytest.raises(CapabilityError) as exc:
        mc.put("qwe", body, {})
    assert exc.value.code == "stale_revision"


# ── guardrail: every kiro_agent -> spec file resolver searches the mate scope ─
#
# RFC §5.3 names FIVE reader sites, not two, because the two-folder table
# under-enumerated them. This test is the registry: a sixth site added later
# that forgets the mate scope fails HERE instead of shipping a crewmate that
# chats fine but breaks its "Built from" tab, its startup context, or its
# Capabilities editor. Each probe writes ONLY a mate file (no user-level
# twin), so a resolver that skips the mate scope entirely answers "absent"
# instead of finding it.


def _only_in_mates(layout, name: str, spec: dict | None = None) -> None:
    _user, mates, _checkout = layout
    _write(mates, name, spec or {"name": name, "prompt": "mate only"})


def _seam_kas_resolve_projected_spec(layout) -> None:
    _only_in_mates(layout, "seam_kas")
    _user, _mates, checkout = layout
    spec, _ = resolve_projected_spec(paths_mod.kiro_agents_dir(), "seam_kas", str(checkout))
    assert spec["prompt"] == "mate only"


def _seam_loader_refresh_materialized_agents(layout, monkeypatch) -> None:
    from kiro_crew.config import loader

    user, mates, _checkout = layout
    _only_in_mates(layout, "seam_loader")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: mates)
    loader.refresh_materialized_agents()
    assert "seam_loader" in loader._MATERIALIZED_AGENTS


def _seam_essential_context_resolve_template_path(layout) -> None:
    from kiro_crew.member_essential_context import resolve_template_path

    _only_in_mates(layout, "seam_essential")
    path = resolve_template_path("seam_essential")
    assert path is not None and path.read_text(encoding="utf-8")


def _seam_capability_source(layout) -> None:
    from kiro_crew.agent_capabilities import _source

    _only_in_mates(layout, "seam_capability")
    path, spec, _ = _source("seam_capability", "")
    assert spec["name"] == "seam_capability"
    assert path.parent == layout[1]  # resolved under the mate dir


def _seam_agent_detail_candidates(layout) -> None:
    from kiro_crew.dashboard.agent_admin.agent_detail import _agent_detail_candidates

    _only_in_mates(layout, "seam_detail")
    matches = _agent_detail_candidates("seam_detail")
    assert any(spec.get("name") == "seam_detail" for _f, spec in matches)


# Every resolver seam this RFC's §5.3 names, keyed for a clear failure message.
# A sixth seam discovered later is added as one more entry here, not a new
# test file -- that is what keeps this a registry instead of scattered probes.
_RESOLVER_SEAMS: dict[str, Any] = {
    "kas.resolve_projected_spec": _seam_kas_resolve_projected_spec,
    "loader.refresh_materialized_agents": _seam_loader_refresh_materialized_agents,
    "member_essential_context.resolve_template_path": _seam_essential_context_resolve_template_path,
    "agent_capabilities._source": _seam_capability_source,
    "agent_detail._agent_detail_candidates": _seam_agent_detail_candidates,
}


@pytest.mark.parametrize("seam_name", sorted(_RESOLVER_SEAMS))
def test_every_kiro_agent_resolver_seam_searches_the_mate_scope(layout, monkeypatch, seam_name):
    """Each of the RFC's five seams resolves a name that exists ONLY in mates/agents/."""
    probe = _RESOLVER_SEAMS[seam_name]
    import inspect

    if "monkeypatch" in inspect.signature(probe).parameters:
        probe(layout, monkeypatch)
    else:
        probe(layout)


def test_the_resolver_seam_registry_has_exactly_the_rfc_count():
    """A sixth seam is added here, not silently -- this pins the count at five."""
    assert len(_RESOLVER_SEAMS) == 5
