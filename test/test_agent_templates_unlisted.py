"""The spec files the Custom agents roster does not show, and the names they keep taken.

A create refuses ``409 name_taken`` for the stem of EVERY spec file in the agents
directory, readable or not, while the roster lists only the files that read and
keeps one row per declared name. A file the roster drops therefore takes a name
the tab gives no way to free. Each such file is listed under ``unlisted`` with
its reason, the refusal names it, and the delete reaches it.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from test_agent_templates_endpoint import _body, _request, _seed_config, _write

from kiro_crew import agent_discovery, agent_state
from kiro_crew.agent_discovery import declared_spec_claim
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_spec_format import APP_AGENT_LAUNCH_PREFIX
from kiro_crew.agent_spec_pin_ops import check_pin_target, write_spec_pin
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers.agent_templates import (
    api_agent_template_create,
    api_agent_template_delete,
    api_agent_templates,
)


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    """Past the owner boundary; owner-auth has its own enumerated coverage."""
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.fixture(autouse=True)
def _row_builder_refuses_non_string_builder_args(_floor_monkeypatch):
    """A row builder that raises on one field type, as ``_write_unbuildable`` needs.

    ``list_agents`` skips a spec whose row construction raises, and the handlers
    here list that file as unreadable (``spec_row``). The shipped row builder
    coerces every JSON field type, so this stands in one that raises, through
    the skill extraction every row build calls, patched through the
    ``agent_discovery`` facade so every owner that binds it holds the stand-in:
    ``mcpServers.builder-mcp.args`` that is not a list of strings raises
    ``TypeError``. Every other spec builds as it does without the stand-in.
    """
    real = agent_discovery._extract_skills

    def _extract_skills(data):
        servers = data.get("mcpServers")
        builder = servers.get("builder-mcp") if isinstance(servers, dict) else None
        if isinstance(builder, dict) and "args" in builder:
            args = builder["args"]
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise TypeError("mcpServers.builder-mcp.args is not a list of strings")
        return real(data)

    _floor_monkeypatch.setattr(agent_discovery, "_extract_skills", _extract_skills)


@pytest.fixture
def agents_dir(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", d):
        yield d


async def _roster() -> dict:
    resp = await api_agent_templates(_request("GET"))
    assert resp.status == 200, resp.text
    return await _body(resp)


async def _create(name: str):
    resp = await api_agent_template_create(_request("POST", body={"name": name}))
    return resp.status, await _body(resp)


def _write_unbuildable(agents_dir, filename: str, name: str, args=5) -> None:
    """A spec that reads as an object but that no row can be built from:
    ``mcpServers.builder-mcp.args`` has a type the row builder does not take
    (``_row_builder_refuses_non_string_builder_args``)."""
    spec = {"name": name, "mcpServers": {"builder-mcp": {"args": args}}}
    (agents_dir / filename).write_text(json.dumps(spec), encoding="utf-8")


def _write_markdown(agents_dir, filename: str, name: str) -> None:
    """A markdown spec (frontmatter and a prompt body) declaring *name*."""
    (agents_dir / filename).write_text(
        f'---\nname: "{name}"\ndescription: "markdown form"\n---\n\n# Prompt\n\nDo the thing.\n',
        encoding="utf-8",
    )


def _store_harnesses(
    agent: dict[str, str] | None = None, pins: dict[str, str] | None = None
) -> None:
    """Store harness choices in ``config.json`` the way the operator writes
    them: *agent* into the ``agent`` section (``"kiro"`` is the Kiro CLI
    choice), *pins* as each crewmate's own ``acp_backend``."""
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("agent", {}).update(agent or {})
    for crew, harness in (pins or {}).items():
        data["agents"][crew]["acp_backend"] = harness
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.asyncio
async def test_an_unreadable_spec_is_listed_named_by_the_refusal_and_deletable(agents_dir):
    """Invalid JSON: the roster has no row for it, yet its stem is taken."""
    _write(agents_dir, "visible.json")
    (agents_dir / "broken.json").write_text('{"name": "broken",}\n', encoding="utf-8")
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"visible.json"}
    assert roster["unlisted"] == [
        {"filename": "broken.json", "stem": "broken", "reason": "unreadable", "deletable": True}
    ]

    status, payload = await _create("broken")
    assert status == 409 and payload["code"] == "name_taken"
    assert payload["unlisted"] == roster["unlisted"][0]
    assert "~/.kiro/agents/broken.json" in payload["error"]
    assert "cannot be read as an agent spec" in payload["error"]
    assert str(agents_dir) not in payload["error"]

    resp = await api_agent_template_delete(_request("DELETE", "broken"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "broken.json").exists()
    assert len(list(agents_dir.glob("broken.json.bak.*"))) == 1
    assert (await _roster())["unlisted"] == []
    assert (await _create("broken"))[0] == 201


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [5, ["--skill-name-filter", 5]])
async def test_a_spec_whose_row_cannot_be_built_is_listed_as_unreadable(agents_dir, args):
    """The file reads as an object, but a field has a type no row can be built
    from (``mcpServers.builder-mcp.args`` here; ``~/.kiro/agents`` is shared
    with other tools). Discovery skips it as it skips a file that does not
    read, so the roster lists it as unreadable rather than failing the whole
    tab, and neither a create of another name nor the delete trips on it."""
    _write(agents_dir, "visible.json")
    _write_unbuildable(agents_dir, "weird.json", "weird", args)
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"visible.json"}
    assert roster["unlisted"] == [
        {
            "filename": "weird.json",
            "stem": "weird",
            "reason": "unreadable",
            "declared_name": "weird",
            "deletable": True,
        }
    ]

    status, payload = await _create("weird")
    assert status == 409 and payload["code"] == "name_taken"
    assert payload["unlisted"] == roster["unlisted"][0]
    assert "~/.kiro/agents/weird.json" in payload["error"]
    assert (await _create("fresh"))[0] == 201

    resp = await api_agent_template_delete(_request("DELETE", "weird"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "weird.json").exists()
    assert (await _roster())["unlisted"] == []
    assert (await _create("weird"))[0] == 201


@pytest.mark.asyncio
async def test_the_name_a_spec_with_no_buildable_row_declares_is_named_by_its_refusal(agents_dir):
    """``odd.json`` reads and declares ``zeta``, but no row can be built from
    it. The create's taken check counts that declared name as well as the stem,
    so a create of ``zeta`` is refused too: the refusal names the file, whose
    entry carries the name it declares, and deleting it frees both names."""
    _write(agents_dir, "visible.json")
    _write_unbuildable(agents_dir, "odd.json", "zeta")
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"visible.json"}
    assert roster["unlisted"] == [
        {
            "filename": "odd.json",
            "stem": "odd",
            "reason": "unreadable",
            "declared_name": "zeta",
            "deletable": True,
        }
    ]

    refusals: dict[str, str] = {}
    for name in ("zeta", "Zeta", "odd"):
        status, payload = await _create(name)
        assert status == 409 and payload["code"] == "name_taken", (name, payload)
        assert payload["unlisted"] == roster["unlisted"][0], name
        assert "~/.kiro/agents/odd.json" in payload["error"], name
        refusals[name] = payload["error"]
    assert "declares 'zeta' but cannot be read as an agent spec" in refusals["zeta"]
    assert "declares" not in refusals["odd"]

    resp = await api_agent_template_delete(_request("DELETE", "odd"))
    assert resp.status == 200, resp.text
    assert (await _roster())["unlisted"] == []
    assert (await _create("zeta"))[0] == 201


@pytest.mark.asyncio
async def test_deleting_a_spec_with_no_buildable_row_is_guarded_by_its_declared_name(agents_dir):
    """The file reads, so the name it declares is known: a chat folder pinned
    to that name refuses the delete by the file's stem, as it refuses the
    delete of the same file with a well-formed field."""
    _write_unbuildable(agents_dir, "odd.json", "zeta")
    _seed_config()
    folders = [{"id": "f-1", "name": "Reviews", "default_agent": "zeta"}]
    resp = await api_agent_template_delete(_request("DELETE", "odd", folders=folders))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert payload["code"] == "template_referenced"
    assert payload["references"] == [{"kind": "folder", "id": "f-1", "label": "Reviews"}]
    assert (agents_dir / "odd.json").exists()


@pytest.mark.asyncio
async def test_a_package_spec_with_no_buildable_row_is_not_offered_for_delete(agents_dir):
    """``Pkg-helper.json`` declaring ``helper`` is a package's file by the
    filename rule discovery applies, which needs only the declared name. It
    stays read-only although no row can be built from it: listed, not
    deletable, and the delete by its stem is refused as a package's."""
    _write_unbuildable(agents_dir, "Pkg-helper.json", "helper")
    _seed_config()

    assert (await _roster())["unlisted"] == [
        {
            "filename": "Pkg-helper.json",
            "stem": "Pkg-helper",
            "reason": "unreadable",
            "declared_name": "helper",
            "deletable": False,
        }
    ]
    status, payload = await _create("helper")
    assert status == 409 and payload["unlisted"]["filename"] == "Pkg-helper.json"

    resp = await api_agent_template_delete(_request("DELETE", "Pkg-helper"))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert (payload["code"], payload["reason"]) == ("template_read_only", "package")
    assert (agents_dir / "Pkg-helper.json").exists()


@pytest.mark.asyncio
async def test_a_same_name_copy_is_listed_with_the_file_that_holds_the_name(agents_dir):
    """``atlas_v2.json`` is a copy whose ``name`` was never edited: discovery
    keeps ``atlas.json`` for the name and drops the copy, whose stem stays taken."""
    _write(agents_dir, "atlas.json", prompt="the original")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="my edited copy")
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"atlas.json"}
    assert roster["unlisted"] == [
        {
            "filename": "atlas_v2.json",
            "stem": "atlas_v2",
            "reason": "duplicate_name",
            "declared_name": "atlas",
            "listed_as": "atlas.json",
            "deletable": True,
        }
    ]

    status, payload = await _create("atlas_v2")
    assert status == 409 and payload["code"] == "name_taken"
    assert payload["unlisted"]["filename"] == "atlas_v2.json"
    assert "declares 'atlas', which atlas.json already holds" in payload["error"]

    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "atlas_v2.json").exists()
    assert (agents_dir / "atlas.json").exists()


def _same_name_copy(agents_dir) -> tuple[str, str]:
    """``atlas_v2.json``, a readable copy declaring ``atlas``, beside the listed
    ``atlas.json``: ``(hidden stem, declared name)``."""
    _write(agents_dir, "atlas.json", prompt="the original")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="my edited copy")
    return "atlas_v2", "atlas"


def _unbuildable_copy(agents_dir) -> tuple[str, str]:
    """``odd.json``, which reads and declares ``zeta`` but yields no row, beside
    the listed ``zeta.json``: ``(hidden stem, declared name)``."""
    _write(agents_dir, "zeta.json")
    _write_unbuildable(agents_dir, "odd.json", "zeta")
    return "odd", "zeta"


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [_same_name_copy, _unbuildable_copy])
async def test_deleting_a_hidden_file_keeps_the_state_of_the_listed_file_of_its_name(
    agents_dir, layout
):
    """The sidecar entry of the name a hidden file declares belongs to the
    listed file that still declares it. Deleting the hidden file, as the tab
    offers, must leave that entry: ``model_managed`` is the explicit-pick
    freeze, and once it is gone a defaults bump may overwrite the pinned model."""
    stem, declared = layout(agents_dir)
    _seed_config()
    agent_state.set_model_managed(declared, False)
    agent_state.set_cc_model(declared, "my-cc-model")
    [entry] = (await _roster())["unlisted"]
    assert (entry["stem"], entry["deletable"]) == (stem, True)

    resp = await api_agent_template_delete(_request("DELETE", stem))
    assert resp.status == 200, resp.text
    assert not (agents_dir / f"{stem}.json").exists()
    assert (agents_dir / f"{declared}.json").exists()
    assert agent_state.get_model_managed(declared) is False
    assert agent_state.get_cc_model(declared) == "my-cc-model"


def _capabilities_saved_for(monkeypatch, saved: str, on_ask=None) -> list[str]:
    """Make *saved* a name with saved capabilities (the capabilities editor's
    definition, which ``require_unmanaged_template`` refuses a legacy delete
    over). Returns every name asked about, in order; *on_ask*, when given, runs
    with each name before the answer."""
    real = agent_state.get_capabilities
    asked: list[str] = []

    def _get_capabilities(name: str):
        asked.append(name)
        if on_ask is not None:
            on_ask(name)
        return {"schema_version": 1, "status": "saved"} if name == saved else real(name)

    monkeypatch.setattr(agent_state, "get_capabilities", _get_capabilities)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [_same_name_copy, _unbuildable_copy])
async def test_saved_capabilities_of_the_name_another_file_keeps_do_not_refuse_the_offered_delete(
    agents_dir, layout, monkeypatch
):
    """Saved capabilities of the name a hidden file declares belong to the
    listed file that still declares it, as its other sidecar state does. The
    tab offers the delete, so the handler must not refuse it with
    ``capabilities_editor_required`` over a definition this delete never
    touches. The hidden file's own stem is still checked."""
    stem, declared = layout(agents_dir)
    _seed_config()
    asked = _capabilities_saved_for(monkeypatch, declared)
    [entry] = (await _roster())["unlisted"]
    assert (entry["stem"], entry["deletable"]) == (stem, True)

    resp = await api_agent_template_delete(_request("DELETE", stem))
    assert resp.status == 200, await _body(resp)
    assert not (agents_dir / f"{stem}.json").exists()
    assert (agents_dir / f"{declared}.json").exists()
    assert stem in asked


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [_same_name_copy, _unbuildable_copy])
async def test_saved_capabilities_refuse_the_delete_of_the_file_the_spec_pin_picks(
    agents_dir, layout, monkeypatch
):
    """The spec pin makes the hidden file the one every session of its
    declared name runs, so the name, and the definition saved for it, go with
    this file: the delete is refused ``409 capabilities_editor_required`` and
    the file stays."""
    stem, declared = layout(agents_dir)
    _seed_config()
    check_pin_target(declared, f"{stem}.json", source="cli", agents_dir=agents_dir)
    write_spec_pin(declared, f"{stem}.json")
    asked = _capabilities_saved_for(monkeypatch, declared)

    resp = await api_agent_template_delete(_request("DELETE", stem))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert payload == {
        "error": "capabilities_editor_required",
        "code": "capabilities_editor_required",
    }
    assert (agents_dir / f"{stem}.json").exists()
    assert declared in asked


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["solo", "atlas"])
async def test_saved_capabilities_refuse_the_delete_of_the_only_file_declaring_the_name(
    agents_dir, monkeypatch, target
):
    """``solo.json`` is the one file declaring ``atlas``: deleting it, by its
    stem or by that name, takes the name with it, so the definition saved for
    ``atlas`` refuses the delete and the file stays."""
    _write(agents_dir, "solo.json", name="atlas")
    _seed_config()
    _capabilities_saved_for(monkeypatch, "atlas")

    resp = await api_agent_template_delete(_request("DELETE", target))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "capabilities_editor_required"
    assert (agents_dir / "solo.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [_same_name_copy, _unbuildable_copy])
async def test_the_name_left_with_the_hidden_file_under_the_lock_keeps_its_capabilities_guard(
    agents_dir, layout, monkeypatch
):
    """The listed file holds the name when the tab offers the delete, then goes
    before the delete takes its locks (removed here as the stem is asked
    about, the last step before them). Under the locks the hidden file is the
    only one declaring the name, so the name would go with it: the saved
    definition refuses the delete, and nothing of the name is pruned."""
    stem, declared = layout(agents_dir)
    _seed_config()
    agent_state.set_model_managed(declared, False)
    [entry] = (await _roster())["unlisted"]
    assert (entry["stem"], entry["deletable"]) == (stem, True)

    def _listed_file_goes(name: str) -> None:
        if name == stem:
            (agents_dir / f"{declared}.json").unlink(missing_ok=True)

    asked = _capabilities_saved_for(monkeypatch, declared, on_ask=_listed_file_goes)

    resp = await api_agent_template_delete(_request("DELETE", stem))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "capabilities_editor_required"
    assert stem in asked and declared in asked
    assert not (agents_dir / f"{declared}.json").exists()
    assert (agents_dir / f"{stem}.json").exists()
    assert agent_state.get_model_managed(declared) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [_same_name_copy, _unbuildable_copy])
async def test_a_hidden_file_is_guarded_by_its_stem_not_by_a_name_another_file_keeps(
    agents_dir, layout
):
    """A crew bound to, or a folder pinned to, the name the listed file still
    declares keeps resolving once the hidden copy goes, so it does not refuse
    that delete. One pinned to the hidden file's own stem still does."""
    stem, declared = layout(agents_dir)
    _seed_config({"reviewer-bot": declared})
    pinned_to_stem = [{"id": "f-1", "name": "Copies", "default_agent": stem}]
    resp = await api_agent_template_delete(_request("DELETE", stem, folders=pinned_to_stem))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert payload["code"] == "template_referenced"
    assert payload["references"] == [{"kind": "folder", "id": "f-1", "label": "Copies"}]
    assert (agents_dir / f"{stem}.json").exists()

    pinned_to_name = [{"id": "f-2", "name": "Reviews", "default_agent": declared}]
    resp = await api_agent_template_delete(_request("DELETE", stem, folders=pinned_to_name))
    assert resp.status == 200, resp.text
    assert not (agents_dir / f"{stem}.json").exists()
    assert (agents_dir / f"{declared}.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", [_same_name_copy, _unbuildable_copy])
async def test_a_hidden_file_the_spec_pin_picks_keeps_the_guard_of_the_name_it_declares(
    agents_dir, layout
):
    """The operator pinned the hidden file (``agent.spec_pins``, as the chat's
    chooser or ``kirocrew agent pin`` writes it), so every session of the name
    it declares runs THAT file, not the listed one. The name is then this
    file's: the tab offers no delete, and a crew bound to the name refuses the
    delete, which would leave the pin naming no file and every session of the
    name refused."""
    stem, declared = layout(agents_dir)
    _seed_config({"reviewer": declared})
    check_pin_target(declared, f"{stem}.json", source="cli", agents_dir=agents_dir)
    write_spec_pin(declared, f"{stem}.json")

    [entry] = (await _roster())["unlisted"]
    assert (entry["stem"], entry["deletable"]) == (stem, False)

    resp = await api_agent_template_delete(_request("DELETE", stem))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert payload["code"] == "template_referenced"
    # The roster default's template is carried into ``agent.default_agent``.
    assert payload["references"] == [
        {"kind": "crew", "id": "reviewer", "label": "reviewer"},
        {"kind": "default", "id": "default", "label": "default agent"},
    ]
    assert (agents_dir / f"{stem}.json").exists()
    claim = declared_spec_claim(agents_dir, declared, operation="t", source="test")
    assert claim is not None and claim.path.name == f"{stem}.json"


@pytest.mark.asyncio
async def test_a_symlink_to_the_hidden_file_does_not_keep_the_name_it_declares(agents_dir):
    """``atlas.json`` is a symlink to ``atlas_v2.json``, so it reads as a second
    file declaring ``atlas``. It is no independent holder of the name: it reads
    the file being deleted and dangles once that file goes. A folder pinned to
    ``atlas`` therefore still refuses the delete of ``atlas_v2``. With nothing
    naming ``atlas`` the delete goes ahead, and the sidecar entry of ``atlas``
    goes with it: it was this file's, not the link's."""
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="the only copy")
    try:
        os.symlink("atlas_v2.json", agents_dir / "atlas.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _seed_config()
    agent_state.set_model_managed("atlas", False)

    pinned_to_name = [{"id": "f-1", "name": "Atlas work", "default_agent": "atlas"}]
    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2", folders=pinned_to_name))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert payload["code"] == "template_referenced"
    assert {"kind": "folder", "id": "f-1", "label": "Atlas work"} in payload["references"]
    assert (agents_dir / "atlas_v2.json").is_file()
    assert (agents_dir / "atlas.json").resolve() == (agents_dir / "atlas_v2.json").resolve()
    assert agent_state.get_model_managed("atlas") is False

    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2"))
    assert resp.status == 200, resp.text
    assert not os.path.lexists(agents_dir / "atlas_v2.json")
    assert (agents_dir / "atlas.json").is_symlink() and not (agents_dir / "atlas.json").exists()
    assert agent_state.get_model_managed("atlas") is None


@pytest.mark.asyncio
async def test_the_target_of_a_hidden_symlink_keeps_the_name_it_declares(agents_dir):
    """``atlas_v2.json`` is a symlink to the real ``atlas.json``. The delete
    renames the link itself, so ``atlas.json`` survives and still holds
    ``atlas``: a folder pinned to ``atlas`` does not refuse the delete, and the
    sidecar entry of ``atlas`` stays with ``atlas.json``."""
    _write(agents_dir, "atlas.json", prompt="the original")
    try:
        os.symlink("atlas.json", agents_dir / "atlas_v2.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _seed_config()
    agent_state.set_model_managed("atlas", False)

    pinned_to_name = [{"id": "f-1", "name": "Atlas work", "default_agent": "atlas"}]
    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2", folders=pinned_to_name))
    assert resp.status == 200, resp.text
    assert not os.path.lexists(agents_dir / "atlas_v2.json")
    assert not (agents_dir / "atlas.json").is_symlink()
    assert json.loads((agents_dir / "atlas.json").read_text(encoding="utf-8"))["name"] == "atlas"
    assert agent_state.get_model_managed("atlas") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("link", "target", "offered"),
    [("atlas.json", "atlas_v2.json", False), ("atlas_v2.json", "atlas.json", True)],
    ids=["listed_link_reads_hidden_file", "hidden_link_reads_listed_file"],
)
async def test_a_hidden_duplicate_is_offered_for_delete_only_when_the_listed_file_survives_it(
    agents_dir, link, target, offered
):
    """The roster lists ``atlas.json`` and leaves out ``atlas_v2.json``, both
    declaring ``atlas``, and the tab's confirm for that delete says
    ``atlas.json`` and the ``atlas`` agent stay as they are. When ``atlas.json``
    is a symlink reading ``atlas_v2.json``, deleting ``atlas_v2`` leaves the
    link dangling, ``atlas`` leaves the roster and its sidecar entry goes, so
    the delete is not offered. When ``atlas_v2.json`` is the link, the real
    ``atlas.json`` survives its delete, which is offered."""
    _write(agents_dir, target, name="atlas", prompt="the only copy")
    try:
        os.symlink(target, agents_dir / link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"atlas.json"}
    [entry] = roster["unlisted"]
    assert (entry["filename"], entry["reason"], entry["listed_as"]) == (
        "atlas_v2.json",
        "duplicate_name",
        "atlas.json",
    )
    assert entry["deletable"] is offered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harnesses", "offered"),
    [({"agent": {"acp_backend": "kiro"}}, False), ({}, True)],
    ids=["json_only_host", "every_host_reads_markdown"],
)
async def test_a_hidden_json_copy_is_offered_for_delete_only_where_the_listed_markdown_keeps_it(
    agents_dir, harnesses, offered
):
    """The roster lists ``atlas.md`` for ``atlas`` and leaves out
    ``atlas_v2.json``, which declares it too. On a host that loads JSON specs
    alone the markdown spec holds nothing once the JSON copy goes (the delete
    guards ``atlas`` and drops its sidecar entry), so the confirm saying the
    ``atlas`` agent stays as it is would be false and the delete is not
    offered. Where every configured host reads markdown the name stays with
    ``atlas.md``, and it is."""
    _write_markdown(agents_dir, "atlas.md", "atlas")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="my JSON copy")
    _seed_config()
    _store_harnesses(**harnesses)

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"atlas.md"}
    [entry] = roster["unlisted"]
    assert (entry["filename"], entry["reason"], entry["listed_as"]) == (
        "atlas_v2.json",
        "duplicate_name",
        "atlas.md",
    )
    assert entry["deletable"] is offered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pin", "stem", "refused"),
    [("atlas_pinned.json", "atlas_v2", True), ("atlas_v2.json", "atlas_pinned", False)],
    ids=["pin_names_a_link_reading_the_file", "pin_names_the_target_of_the_deleted_link"],
)
async def test_a_pin_through_a_link_to_a_hidden_file_keeps_the_guard_of_its_name(
    agents_dir, pin, stem, refused
):
    """``atlas.json``, ``atlas_v2.json`` and ``atlas_pinned.json``, a symlink
    reading ``atlas_v2.json``, all declare ``atlas``, and a crew is bound to
    ``atlas``. When the pin names the link, every session of ``atlas`` runs
    ``atlas_v2.json`` through it, and deleting ``atlas_v2`` leaves the pin on
    a dangling link with every session of the name refused: the tab offers no
    delete, and the crew refuses it. When the pin names ``atlas_v2.json`` and
    the link is deleted, the pinned file survives, so the name stays with it
    and the delete goes ahead."""
    _write(agents_dir, "atlas.json", prompt="the original")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="the pinned copy")
    try:
        os.symlink("atlas_v2.json", agents_dir / "atlas_pinned.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _seed_config({"reviewer": "atlas"})
    check_pin_target("atlas", pin, source="cli", agents_dir=agents_dir)
    write_spec_pin("atlas", pin)

    unlisted = {e["stem"]: e for e in (await _roster())["unlisted"]}
    assert unlisted[stem]["deletable"] is not refused

    resp = await api_agent_template_delete(_request("DELETE", stem))
    if refused:
        assert resp.status == 409, resp.text
        payload = await _body(resp)
        assert payload["code"] == "template_referenced"
        assert {"kind": "crew", "id": "reviewer", "label": "reviewer"} in payload["references"]
        assert (agents_dir / f"{stem}.json").is_file()
    else:
        assert resp.status == 200, resp.text
        assert not os.path.lexists(agents_dir / f"{stem}.json")
        assert (agents_dir / pin).is_file()


@pytest.mark.asyncio
async def test_a_markdown_spec_does_not_keep_the_name_on_a_host_that_loads_json_only(agents_dir):
    """``atlas.md`` declares ``atlas`` beside ``atlas_v2.json``, the only JSON
    spec of that name, and ``agent.acp_backend`` selects Kiro CLI, which loads
    JSON specs alone. Once ``atlas_v2.json`` goes, no spec that host can load is
    named ``atlas``, so a crew bound to it still refuses the delete of
    ``atlas_v2``."""
    _write_markdown(agents_dir, "atlas.md", "atlas")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="the only JSON copy")
    _seed_config({"reviewer": "atlas"})
    _store_harnesses(agent={"acp_backend": "kiro"})

    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2"))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert payload["code"] == "template_referenced"
    assert {"kind": "crew", "id": "reviewer", "label": "reviewer"} in payload["references"]
    assert (agents_dir / "atlas_v2.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "harnesses",
    [
        {"agent": {"member_acp_backend": "codex"}},
        {"pins": {"reviewer": "kiro"}},
        {"pins": {"reviewer": "no-such-harness"}},
    ],
    ids=["member_backend", "crewmate_pin", "unstartable_pin"],
)
async def test_a_markdown_spec_keeps_no_name_while_any_configured_host_loads_json_only(
    agents_dir, harnesses
):
    """``agent.acp_backend`` is KAS, which reads markdown, but another host
    the config starts sessions on does not: the Bot backend, a crewmate's own
    harness pin, or a pin this gateway cannot start (its host is unknown). The
    markdown spec then holds nothing, and the crew bound to ``atlas`` still
    refuses the delete."""
    _write_markdown(agents_dir, "atlas.md", "atlas")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="the only JSON copy")
    _seed_config({"reviewer": "atlas"})
    _store_harnesses(**harnesses)

    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2"))
    assert resp.status == 409, resp.text
    payload = await _body(resp)
    assert payload["code"] == "template_referenced"
    assert {"kind": "crew", "id": "reviewer", "label": "reviewer"} in payload["references"]
    assert (agents_dir / "atlas_v2.json").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("pins", [{}, {"reviewer": "kas"}], ids=["default", "kas_pin"])
async def test_a_markdown_spec_keeps_the_name_when_every_configured_host_reads_markdown(
    agents_dir, pins
):
    """Every host the config starts sessions on is KAS (the default, and a
    crewmate pinned to it), which loads ``atlas.md`` once ``atlas_v2.json``
    goes. The name stays with the markdown spec: the crew bound to ``atlas``
    does not refuse the delete, and the sidecar entry of ``atlas`` is kept."""
    _write_markdown(agents_dir, "atlas.md", "atlas")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="my JSON copy")
    _seed_config({"reviewer": "atlas"})
    _store_harnesses(pins=pins)
    agent_state.set_model_managed("atlas", False)

    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "atlas_v2.json").exists()
    assert (agents_dir / "atlas.md").is_file()
    assert agent_state.get_model_managed("atlas") is False


@pytest.mark.asyncio
async def test_a_json_spec_beside_a_markdown_one_keeps_the_name_on_a_json_only_host(agents_dir):
    """On a Kiro CLI host ``atlas-notes.md`` holds nothing, but the real
    ``atlas.json`` declares ``atlas`` too and survives the delete of
    ``atlas_v2``, so a folder pinned to ``atlas`` does not refuse it."""
    _write_markdown(agents_dir, "atlas-notes.md", "atlas")
    _write(agents_dir, "atlas.json", prompt="the original")
    _write(agents_dir, "atlas_v2.json", name="atlas", prompt="my edited copy")
    _seed_config()
    _store_harnesses(agent={"acp_backend": "kiro"})

    pinned_to_name = [{"id": "f-1", "name": "Atlas work", "default_agent": "atlas"}]
    resp = await api_agent_template_delete(_request("DELETE", "atlas_v2", folders=pinned_to_name))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "atlas_v2.json").exists()
    assert (agents_dir / "atlas.json").is_file()


@pytest.mark.asyncio
async def test_a_spec_nested_past_the_parser_depth_is_listed_and_deletable(agents_dir):
    """``~/.kiro/agents`` is shared with other tools, and a document nested
    deeper than the JSON parser recurses makes it raise ``RecursionError``
    rather than ``ValueError``. Discovery skips such a file; the roster's
    re-read of hidden files and the delete must too, so the tab loads, lists
    it as unreadable, and its Delete reaches it."""
    depth = 200_000
    text = "[" * depth + "]" * depth
    with pytest.raises(RecursionError):
        json.loads(text)
    _write(agents_dir, "visible.json")
    (agents_dir / "deep.json").write_text(text, encoding="utf-8")
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"visible.json"}
    assert roster["unlisted"] == [
        {"filename": "deep.json", "stem": "deep", "reason": "unreadable", "deletable": True}
    ]
    resp = await api_agent_template_delete(_request("DELETE", "deep"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "deep.json").exists()
    assert (await _roster())["unlisted"] == []


@pytest.mark.asyncio
async def test_a_symlink_loop_fails_neither_the_tab_nor_its_delete(agents_dir):
    """``loop.json`` links to itself. No read of it succeeds, and Python 3.12's
    ``Path.resolve`` reports the loop with ``RuntimeError`` rather than
    ``OSError``. Discovery skips it, and the create's taken check still counts
    its stem, so the roster lists it as unreadable and its refusal names it.
    There is no file beneath the agents directory to rename, so the tab offers
    no delete and the delete answers 404, as for any name no file reaches."""
    _write(agents_dir, "visible.json")
    try:
        os.symlink("loop.json", agents_dir / "loop.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"visible.json"}
    assert roster["unlisted"] == [
        {"filename": "loop.json", "stem": "loop", "reason": "unreadable", "deletable": False}
    ]
    status, payload = await _create("loop")
    assert status == 409 and payload["code"] == "name_taken"
    assert payload["unlisted"] == roster["unlisted"][0]
    assert "~/.kiro/agents/loop.json" in payload["error"]
    assert (await _create("fresh"))[0] == 201

    resp = await api_agent_template_delete(_request("DELETE", "loop"))
    assert resp.status == 404, resp.text
    assert (agents_dir / "loop.json").is_symlink()


@pytest.mark.asyncio
async def test_a_listed_name_keeps_the_plain_refusal(agents_dir):
    """The row is on screen, so the refusal needs no file named."""
    _write(agents_dir, "visible.json")
    _seed_config()
    status, payload = await _create("visible")
    assert status == 409
    assert payload == {"error": "A template named 'visible' already exists", "code": "name_taken"}


@pytest.mark.asyncio
async def test_no_delete_is_offered_for_a_hidden_file_whose_stem_reaches_another(agents_dir):
    """``atlas-custom.json`` declares ``atlas`` and sorts first, so discovery
    keeps it and drops ``atlas.json``. The name ``atlas`` then reaches both
    files, which the delete refuses as ambiguous, so the tab must not offer a
    delete that can only be refused."""
    _write(agents_dir, "atlas-custom.json", name="atlas")
    _write(agents_dir, "atlas.json")
    _seed_config()

    roster = await _roster()
    assert {r["filename"] for r in roster["templates"]} == {"atlas-custom.json"}
    [entry] = roster["unlisted"]
    assert (entry["filename"], entry["listed_as"], entry["deletable"]) == (
        "atlas.json",
        "atlas-custom.json",
        False,
    )
    resp = await api_agent_template_delete(_request("DELETE", "atlas"))
    assert resp.status == 409
    assert (await _body(resp))["code"] == "ambiguous_template_name"


@pytest.mark.asyncio
async def test_a_name_a_listed_row_carries_keeps_the_plain_refusal_beside_a_hidden_stem(
    agents_dir,
):
    """The row ``atlas`` is backed by ``atlas-custom.json``, and the hidden
    ``atlas.json`` has the stem ``atlas``. The exact name reaches both files
    and is refused as ambiguous; another case of it is refused as taken (the
    taken check is case-folded). Renaming or deleting ``atlas.json`` would not
    free the name the row still declares, so that refusal names no file: the
    row on screen is what holds it."""
    _write(agents_dir, "atlas-custom.json", name="atlas")
    _write(agents_dir, "atlas.json")
    _seed_config()

    status, payload = await _create("Atlas")
    assert status == 409
    assert payload == {"error": "A template named 'Atlas' already exists", "code": "name_taken"}


@pytest.mark.asyncio
async def test_a_copy_saved_as_name_custom_is_named_by_the_refusal_of_its_stem(agents_dir):
    """The reported layout: ``src-custom.json`` is a copy of ``src.json`` whose
    ``name`` still says ``src``. It sorts first, so the row ``src`` is backed
    by the copy and the original is hidden. ``src-custom`` is then taken by a
    file no row is labelled with, so the refusal names that file and its row."""
    _write(agents_dir, "src.json", prompt="the original")
    _write(agents_dir, "src-custom.json", name="src", prompt="my edited copy")
    _seed_config()

    roster = await _roster()
    assert [(r["name"], r["filename"]) for r in roster["templates"]] == [("src", "src-custom.json")]
    [entry] = roster["unlisted"]
    assert (entry["filename"], entry["listed_as"], entry["deletable"]) == (
        "src.json",
        "src-custom.json",
        False,
    )

    status, payload = await _create("src-custom")
    assert status == 409 and payload["code"] == "name_taken"
    assert payload["listed"] == {"filename": "src-custom.json", "name": "src"}
    assert "unlisted" not in payload
    assert "~/.kiro/agents/src-custom.json, which the list shows as 'src'" in payload["error"]


@pytest.mark.asyncio
async def test_a_same_package_twin_is_not_listed_as_a_problem(agents_dir):
    """A locally published package ships ``{package}-{name}.json`` and
    ``local-{package}-{name}.json``. Discovery drops the twin on purpose
    (logged at DEBUG), and the package manager owns both files, so the tab
    must not tell the user to rename or delete one."""
    _write(agents_dir, "Pkg-atlas.json", name="atlas")
    _write(agents_dir, "local-Pkg-atlas.json", name="atlas")
    _seed_config()

    roster = await _roster()
    # The directory sort picks which twin backs the row, and on Windows that
    # sort ignores case, so the row may be either file of the pair.
    [row] = roster["templates"]
    assert row["name"] == "atlas"
    assert row["filename"] in {"Pkg-atlas.json", "local-Pkg-atlas.json"}
    assert roster["unlisted"] == []


@pytest.mark.asyncio
async def test_a_plain_file_behind_a_package_of_its_name_is_still_listed(agents_dir):
    """Only one package shipping a name twice is expected. A user's plain file
    the package's row shadows still takes its stem, and stays listed."""
    _write(agents_dir, "Pkg-atlas.json", name="atlas")
    _write(agents_dir, "atlas.json")
    _seed_config()

    [entry] = (await _roster())["unlisted"]
    assert (entry["filename"], entry["reason"], entry["listed_as"]) == (
        "atlas.json",
        "duplicate_name",
        "Pkg-atlas.json",
    )


@pytest.mark.asyncio
async def test_unlisted_leaves_out_what_the_roster_leaves_out_by_design(agents_dir):
    """An AppleDouble sidecar, an app launch copy and the runtime's own spec
    are not templates, so none is listed as a problem. A markdown file with no
    frontmatter takes its stem like any spec file; the tab lists it but offers
    no delete, as it offers none for a markdown spec."""
    _write(agents_dir, "visible.json")
    (agents_dir / "._visible.json").write_bytes(b"\x00\x05\x16\x07")
    (agents_dir / f"{APP_AGENT_LAUNCH_PREFIX}demo.json").write_text("{", encoding="utf-8")
    (agents_dir / AGENT_FILENAME).write_text("{", encoding="utf-8")
    (agents_dir / "notes.md").write_text("Plain notes, no frontmatter.\n", encoding="utf-8")
    _seed_config()

    assert (await _roster())["unlisted"] == [
        {"filename": "notes.md", "stem": "notes", "reason": "unreadable", "deletable": False}
    ]
    resp = await api_agent_template_delete(_request("DELETE", "notes"))
    assert resp.status == 409
    assert (await _body(resp))["reason"] == "markdown"
    assert (agents_dir / "notes.md").exists()


@pytest.mark.asyncio
async def test_deleting_an_unreadable_spec_keeps_the_reference_guard(agents_dir):
    """A file that does not parse: its contents are unknown, so it is guarded
    by the one name it answers to, its stem, and a chat folder pinned to that
    name still refuses the delete. A file that reads but yields no row is
    guarded by the name it declares as well
    (``test_deleting_a_spec_with_no_buildable_row_is_guarded_by_its_declared_name``)."""
    (agents_dir / "broken.json").write_text("{", encoding="utf-8")
    _seed_config()
    folders = [{"id": "f-1", "name": "Reviews", "default_agent": "broken"}]
    resp = await api_agent_template_delete(_request("DELETE", "broken", folders=folders))
    assert resp.status == 409
    payload = await _body(resp)
    assert payload["code"] == "template_referenced"
    assert payload["references"] == [{"kind": "folder", "id": "f-1", "label": "Reviews"}]
    assert (agents_dir / "broken.json").exists()


@pytest.mark.asyncio
async def test_an_unreadable_spec_fixed_before_the_lock_is_not_deleted_as_unreadable(
    agents_dir, monkeypatch
):
    """The delete re-checks under the spec lock: a file that reads by then is
    not the one the request named, and nothing is unlinked."""
    from kiro_crew.dashboard.handlers import agent_templates as module

    (agents_dir / "broken.json").write_text("{", encoding="utf-8")
    _seed_config()
    real = module._unreadable_info
    calls = {"n": 0}

    def _probe_then_fix(name):
        calls["n"] += 1
        if calls["n"] == 2:  # the locked re-check: the user fixed the file meanwhile
            _write(agents_dir, "broken.json")
        return real(name)

    monkeypatch.setattr(module, "_unreadable_info", _probe_then_fix)
    resp = await api_agent_template_delete(_request("DELETE", "broken"))
    assert resp.status == 404, resp.text
    assert (agents_dir / "broken.json").exists()


@pytest.mark.asyncio
async def test_a_traversing_name_reaches_no_unreadable_file(agents_dir):
    (agents_dir / "broken.json").write_text("{", encoding="utf-8")
    _seed_config()
    resp = await api_agent_template_delete(_request("DELETE", "../agents/broken"))
    assert resp.status == 404
    assert (agents_dir / "broken.json").exists()


@pytest.mark.asyncio
async def test_unlisted_masks_like_the_rows(agents_dir):
    """A file whose name would be masked is left out, as its row would be."""
    _write(agents_dir, "visible.json")
    (agents_dir / "AKIAIOSFODNN7EXAMPLE.json").write_text("{", encoding="utf-8")
    _seed_config()
    assert (await _roster())["unlisted"] == []
