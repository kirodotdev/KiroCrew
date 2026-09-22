"""The Agent templates tab's backend: roster, create, delete, and the widened PATCH.

A template is a shared definition; the tab manages the shared file itself, as
distinct from the crew pane's private copies. The rules under test are the ones a
management page must not get wrong: a package or runtime spec is read-only
(its installer rewrites it), a template still referenced by a crew, the default
agent, a schedule, a chat folder, a webhook or a private copy cannot be deleted,
and a create never lands on a name a crew binding or an installed spec already
resolves.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import agent_state
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers import agent_templates
from kiro_crew.dashboard.handlers.agent_templates import (
    api_agent_template_create,
    api_agent_template_delete,
    api_agent_templates,
)
from kiro_crew.dashboard.handlers.agents import api_agent_detail
from kiro_crew.webhooks import token_store


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Past the owner boundary; owner-auth has its own enumerated coverage."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.fixture
def agents_dir(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", d):
        yield d


def _request(
    method: str,
    name: str | None = None,
    body=None,
    *,
    bad_json: bool = False,
    folders: list[dict] | None = None,
):
    request = MagicMock(spec=web.Request)
    request.method = method
    request.match_info = {"name": name} if name else {}
    state = MagicMock()
    # The folder store is read on the loop through ``read_folders(reader)``;
    # the handlers see whatever *folders* this request's dashboard holds.
    state.read_folders = AsyncMock(side_effect=lambda read: read(folders or []))

    # ...and held across a section through ``hold_folders(section)``.
    async def _hold(section):
        return await section(folders or [])

    state.hold_folders = AsyncMock(side_effect=_hold)
    request.app = {"state": state}

    async def _json():
        if bad_json:
            raise ValueError("not json")
        return body

    request.json = _json
    return request


def _write(agents_dir, filename: str, **spec) -> None:
    data = {"name": filename.rsplit(".", 1)[0], "tools": ["fs_read"], **spec}
    (agents_dir / filename).write_text(json.dumps(data), encoding="utf-8")


def _seed_config(agents: dict[str, str] | None = None, default: str = "") -> None:
    cfg = KiroCrewConfig()
    cfg.agents = {crew: KiroCrewAgentConfig(kiro_agent=t) for crew, t in (agents or {}).items()}
    if default:
        cfg.default_agent = default
    cfg.save()


def _EXECUTION_FOR(template_id: str) -> dict:
    """A captured execution record the way ``bind_cron_memory`` writes one."""
    return {
        "member_id": None,
        "store": {"store_id": "default", "member_id": None},
        "selection_kind": "template",
        "template_id": template_id,
        "memory_mode": "persistent",
        "app": "",
    }


def _seed_cron(agent_id: str, name: str = "nightly triage", **extra) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "crons.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-1",
                        "name": name,
                        "message": "go",
                        "schedule": {"kind": "every", "every_secs": 3600},
                        "agent_id": agent_id,
                        **extra,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


async def _body(resp):
    return json.loads(resp.text)


# ── roster ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["", "elsewhere/catalog.json", "not-on-disk.json"])
async def test_roster_marks_a_row_without_a_spec_file_read_only(agents_dir, monkeypatch, filename):
    """An edition catalog row is ``AgentInfo`` built from a provider dict: its
    ``source`` defaults to ``builtin`` and its ``filename`` is whatever the
    provider wrote -- empty, foreign or absent from disk. No action on the tab
    has a file to write for it, so it must not be offered as editable only for
    every edit or delete to answer 404: it is read-only for the runtime's reason."""
    from kiro_crew.agent_discovery import AgentInfo
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "reviewer.json", description="mine")
    _seed_config()
    real = module.list_agents
    catalog = AgentInfo(name="edition-helper", filename=filename, description="", model="")
    monkeypatch.setattr(module, "list_agents", lambda **kw: [*real(**kw), catalog])
    resp = await api_agent_templates(_request("GET"))
    assert resp.status == 200, resp.text
    rows = {r["name"]: r for r in (await _body(resp))["templates"]}
    assert rows["edition-helper"]["read_only"] == "runtime"
    assert rows["reviewer"]["read_only"] is None


@pytest.mark.asyncio
async def test_roster_marks_editability_and_references(agents_dir):
    _write(agents_dir, "reviewer.json", description="mine")
    _write(agents_dir, "SomePkg-atlas.json", name="atlas")
    _write(agents_dir, "kirocrew-worker.json", name="kirocrew-worker")
    _seed_config({"pr-bot": "reviewer"}, default="pr-bot")
    _seed_cron("reviewer")
    agent_state.set_fork_info("pr-bot-copy", forked_from="reviewer", private_to="pr-bot")

    resp = await api_agent_templates(_request("GET"))
    assert resp.status == 200
    rows = {r["name"]: r for r in (await _body(resp))["templates"]}

    assert rows["reviewer"]["read_only"] is None
    kinds = [(u["kind"], u["id"]) for u in rows["reviewer"]["used_by"]]
    assert ("crew", "pr-bot") in kinds
    assert ("schedule", "job-1") in kinds
    assert ("private_copy", "pr-bot-copy") in kinds
    # The package file is read-only for the package's reason, the runtime file
    # for the runtime's -- two different remedies, so two different labels.
    assert rows["atlas"]["read_only"] == "package"
    assert rows["kirocrew-worker"]["read_only"] == "runtime"
    assert rows["atlas"]["used_by"] == []


@pytest.mark.asyncio
async def test_roster_counts_a_chat_folder_pin_as_a_reference(agents_dir):
    """A folder's ``default_agent`` is what every session filed there starts on."""
    _write(agents_dir, "reviewer.json")
    _write(agents_dir, "scratch.json")
    _seed_config()
    folders = [
        {"id": "f-1", "name": "Reviews", "default_agent": "reviewer"},
        {"id": "f-2", "name": "Inherits", "default_agent": ""},
    ]
    resp = await api_agent_templates(_request("GET", folders=folders))
    rows = {r["name"]: r for r in (await _body(resp))["templates"]}
    assert rows["reviewer"]["used_by"] == [{"kind": "folder", "id": "f-1", "label": "Reviews"}]
    assert rows["scratch"]["used_by"] == []


# ── create ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_roster_masks_package_controlled_strings_like_the_sibling_rosters(agents_dir):
    """A package writes ``description`` (and the rest of a spec); the roster
    renders it through ``_roster_mask`` like ``GET /api/agents`` and the chat
    catalog do, so a credential-shaped value arrives as the sentinel. A row whose
    identity itself would be masked is left out rather than half-shown."""
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

    probe = "AKIAIOSFODNN7EXAMPLE"
    _write(agents_dir, "SomePkg-atlas.json", name="atlas", description=f"key {probe}", model=probe)
    _write(agents_dir, "reviewer.json", description="benign")
    _write(agents_dir, f"{probe}.json", name=probe)
    _seed_config({probe: "reviewer"})
    resp = await api_agent_templates(_request("GET"))
    rows = {r["name"]: r for r in (await _body(resp))["templates"]}
    assert probe not in rows
    assert rows["atlas"]["description"] == _SENSITIVE_MASK
    assert rows["atlas"]["model"] == _SENSITIVE_MASK
    assert rows["reviewer"]["description"] == "benign"
    # Reference labels come from crew, folder, schedule and token records the
    # agent can write too.
    assert rows["reviewer"]["used_by"] == [
        {"kind": "crew", "id": _SENSITIVE_MASK, "label": _SENSITIVE_MASK}
    ]


@pytest.mark.asyncio
async def test_roster_counts_a_webhook_agent_pin_as_a_reference(agents_dir):
    """A webhook token's ``agent`` is what its calls run; gone, they are refused."""
    _write(agents_dir, "reviewer.json")
    _seed_config()
    _raw, _secret, entry = token_store().create(
        "ci-hook", require_signature=False, agent="reviewer"
    )
    resp = await api_agent_templates(_request("GET"))
    rows = {r["name"]: r for r in (await _body(resp))["templates"]}
    assert rows["reviewer"]["used_by"] == [
        {"kind": "webhook", "id": entry["id"], "label": "ci-hook"}
    ]


# ── create ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_publishes_the_name_to_the_dispatch_snapshot(agents_dir, monkeypatch):
    """ "Chat with this template" resolves through the in-memory dispatch snapshot
    (``_materialized_kiro_agent``), not the roster cache. A slot created before the
    off-loop rescan lands would be normalized -- and durably stored -- onto the
    default agent, so the create publishes the new name at once and only then
    schedules the authoritative rescan."""
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        agent_templates,
        "publish_materialized_agents",
        lambda names: calls.append(("publish", list(names))),
    )
    monkeypatch.setattr(
        agent_templates,
        "schedule_materialized_agents_refresh",
        lambda: calls.append(("schedule", None)),
    )
    _seed_config()
    resp = await api_agent_template_create(_request("POST", body={"name": "fresh"}))
    assert resp.status == 201, resp.text
    assert calls == [("publish", ["fresh"]), ("schedule", None)]


@pytest.mark.asyncio
async def test_delete_refreshes_the_dispatch_snapshot_before_answering(agents_dir, monkeypatch):
    """A removal has no publish shortcut: the snapshot is rebuilt by a rescan that
    the delete AWAITS (off the loop) before it answers, so the deleted name can no
    longer be bound to a new slot the moment the response lands. A refusal never
    touches the snapshot -- nothing changed on disk."""
    calls: list[str] = []
    monkeypatch.setattr(
        agent_templates, "refresh_materialized_agents", lambda: calls.append("refresh")
    )
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "reviewer"}, default="pr-bot")
    refused = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert refused.status == 409
    assert calls == []
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 200, resp.text
    assert calls == ["refresh"]


@pytest.mark.asyncio
async def test_create_blank_writes_a_minimal_runnable_spec(agents_dir):
    _seed_config()
    resp = await api_agent_template_create(
        _request("POST", body={"name": "pr-summarizer", "description": "Sums up PRs"})
    )
    assert resp.status == 201, resp.text
    assert (await _body(resp)) == {
        "ok": True,
        "name": "pr-summarizer",
        "filename": "pr-summarizer.json",
    }
    spec = json.loads((agents_dir / "pr-summarizer.json").read_text(encoding="utf-8"))
    assert spec["name"] == "pr-summarizer"
    assert spec["description"] == "Sums up PRs"
    assert spec["tools"] and spec["prompt"] == ""
    # A created template is shared, not anyone's private copy.
    assert agent_state.get_fork_info("pr-summarizer") is None


@pytest.mark.asyncio
async def test_create_accepts_the_case_variant_of_an_engine_reserved_id(agents_dir):
    """``Default`` registers on KAS as an ordinary client agent (measured), so the
    engine check is exact-case: a fold to ``.lower()`` would refuse it here."""
    _seed_config()
    resp = await api_agent_template_create(_request("POST", body={"name": "Default"}))
    assert resp.status == 201, resp.text
    assert (await _body(resp))["name"] == "Default"
    assert (agents_dir / "Default.json").exists()


@pytest.mark.asyncio
async def test_create_from_copies_a_package_template_without_lineage(agents_dir):
    _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="Be grounded.", tools=["x"])
    _seed_config()
    resp = await api_agent_template_create(
        _request("POST", body={"name": "my-atlas", "from": "atlas"})
    )
    assert resp.status == 201, resp.text
    spec = json.loads((agents_dir / "my-atlas.json").read_text(encoding="utf-8"))
    assert spec["name"] == "my-atlas"
    assert spec["prompt"] == "Be grounded."
    assert spec["tools"] == ["x"]
    assert agent_state.get_fork_info("my-atlas") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"name": "has space"}, 400, "invalid_template_name"),
        ({"name": "kirocrew"}, 400, "template_name_reserved"),
        # Kept by the KAS engine for itself when injected over the wire
        # (agent_files.KAS_RESERVED_AGENT_IDS); exact match, so `Default` is fine.
        ({"name": "default"}, 400, "template_name_reserved_by_engine"),
        ({"name": "vibe"}, 400, "template_name_reserved_by_engine"),
        ({"name": "plan"}, 400, "template_name_reserved_by_engine"),
        ({"name": "x", "from": "nope"}, 404, "template_not_found"),
        ({"name": "reviewer"}, 409, "name_taken"),
        ({"name": "pr-bot"}, 409, "name_bound"),
    ],
)
async def test_create_refusals(agents_dir, body, status, code):
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "reviewer"})
    resp = await api_agent_template_create(_request("POST", body=body))
    assert resp.status == status, resp.text
    assert (await _body(resp))["code"] == code
    assert not (agents_dir / f"{body['name']}.json").exists() or body["name"] == "reviewer"


@pytest.mark.asyncio
async def test_create_copies_the_source_as_it_is_inside_the_lock(agents_dir, monkeypatch):
    """The pre-lock read only resolves the source's path; its body is read again
    under the spec lock, so an edit that lands in between is what gets copied."""
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="v1")
    _seed_config()
    real = agents_handlers._spec_stem_on_disk

    def _edit_source_then_check(agents_dir_, name):
        # Runs inside ``agents_spec_lock`` right before the source is re-read:
        # the latest point a concurrent save could have committed.
        (agents_dir / "SomePkg-atlas.json").write_text(
            json.dumps({"name": "atlas", "tools": ["fs_read"], "prompt": "v2"}), encoding="utf-8"
        )
        return real(agents_dir_, name)

    monkeypatch.setattr(agents_handlers, "_spec_stem_on_disk", _edit_source_then_check)
    resp = await api_agent_template_create(_request("POST", body={"name": "mine", "from": "atlas"}))
    assert resp.status == 201, resp.text
    assert json.loads((agents_dir / "mine.json").read_text(encoding="utf-8"))["prompt"] == "v2"


@pytest.mark.asyncio
async def test_create_and_delete_mutations_run_through_the_drained_seam(agents_dir, monkeypatch):
    """A cancelled request must not leave the worker committing a spec write or
    unlink while the lock release, cache refresh and audit are abandoned: both
    mutations go through ``drained_to_thread``, which drains the thread first."""
    import asyncio

    from kiro_crew.dashboard.handlers import agent_templates as module

    drained: list[str] = []

    async def _drained(fn, /, *args):
        drained.append(fn.__name__)
        return await asyncio.to_thread(fn, *args)

    monkeypatch.setattr(module, "drained_to_thread", _drained)
    _seed_config()
    assert (
        await api_agent_template_create(_request("POST", body={"name": "scratch"}))
    ).status == 201
    assert (await api_agent_template_delete(_request("DELETE", "scratch"))).status == 200
    assert drained == ["_create", "_guard_then_unlink"]


@pytest.mark.asyncio
async def test_successful_create_and_delete_emit_operation_labelled_sel_events(
    agents_dir, monkeypatch
):
    """The owner gate logs only denials; a successful write of a machine-global
    spec gets its own labelled line beside the middleware's request-level one."""
    from kiro_crew.dashboard.handlers import agent_templates as module

    events: list[dict] = []
    monkeypatch.setattr(
        module,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    _seed_config()
    assert (
        await api_agent_template_create(_request("POST", body={"name": "scratch"}))
    ).status == 201
    assert (await api_agent_template_delete(_request("DELETE", "scratch"))).status == 200
    assert [(e["operation"], e["outcome"]) for e in events] == [
        ("agent_templates.create", "ok"),
        ("agent_templates.delete", "ok"),
    ]
    assert events[0]["resources"] == "template:scratch"
    # The delete's row names where the file went, so the audit trail is also
    # the recovery instruction.
    assert re.fullmatch(
        r"template:scratch file:scratch\.json tombstone:scratch\.json\.bak\.\d+",
        events[1]["resources"],
    )
    # A refused write logs nothing here: the gate and the middleware own that trail.
    assert (
        await api_agent_template_create(_request("POST", body={"name": "has space"}))
    ).status == 400
    assert len(events) == 2


@pytest.mark.asyncio
async def test_create_revalidates_the_source_identity_under_the_lock(agents_dir, monkeypatch):
    """The pre-lock probe chose ``SomePkg-atlas.json`` for ``atlas``; a second
    file reaching that name landing before the lock (``atlas.json``, a user
    save) makes the source ambiguous, so the copy must refuse rather than copy
    whichever file the probe happened to see. Nothing is written."""
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="package")
    _seed_config()
    real = agents_handlers._spec_stem_on_disk

    def _second_claimant_then_check(agents_dir_, name):
        _write(agents_dir, "atlas.json", name="atlas", prompt="user")  # lands after the probe
        return real(agents_dir_, name)

    monkeypatch.setattr(agents_handlers, "_spec_stem_on_disk", _second_claimant_then_check)
    resp = await api_agent_template_create(_request("POST", body={"name": "mine", "from": "atlas"}))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "ambiguous_template_name"
    assert not (agents_dir / "mine.json").exists()


@pytest.mark.asyncio
async def test_create_refuses_a_source_replaced_by_another_file_under_the_lock(
    agents_dir, monkeypatch
):
    """The probe's path is ``SomePkg-atlas.json``; if that file is gone and a
    different file now answers to ``atlas`` when the lock is taken, the probe's
    path is stale -- the locked re-resolution must see exactly the probed file,
    so the request refuses instead of copying a definition it never validated."""
    from kiro_crew.dashboard.handlers import agents as agents_handlers

    _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="package")
    _seed_config()
    real = agents_handlers._spec_stem_on_disk

    def _replace_source_then_check(agents_dir_, name):
        (agents_dir / "SomePkg-atlas.json").unlink()
        _write(agents_dir, "other.json", name="atlas", prompt="other")
        return real(agents_dir_, name)

    monkeypatch.setattr(agents_handlers, "_spec_stem_on_disk", _replace_source_then_check)
    resp = await api_agent_template_create(_request("POST", body={"name": "mine", "from": "atlas"}))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "ambiguous_template_name"
    assert not (agents_dir / "mine.json").exists()


@pytest.mark.asyncio
async def test_create_rescans_by_declared_name_under_the_lock(agents_dir, monkeypatch):
    """A package install landing ``Pkg-foo.json`` (declaring ``foo``) between the
    pre-lock probe and the write must refuse, not create ``foo.json`` beside it
    as an ambiguous name; the locked check scans by declared name, not stem alone."""
    from kiro_crew.dashboard.handlers import agents as agents_module

    _seed_config()
    real_stem_check = agents_module._spec_stem_on_disk

    def _install_then_check(agents_dir_, name):
        _write(agents_dir, "Pkg-foo.json", name="foo")  # lands after the probe
        return real_stem_check(agents_dir_, name)

    # The locked check imports the helper at call time, so patch its home.
    monkeypatch.setattr(agents_module, "_spec_stem_on_disk", _install_then_check)
    resp = await api_agent_template_create(_request("POST", body={"name": "foo"}))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "name_taken"
    assert not (agents_dir / "foo.json").exists()


@pytest.mark.asyncio
async def test_create_refuses_a_name_bound_only_in_the_overlay(agents_dir):
    """A binding present only in ``config.local.json`` still makes the new file
    resolve for a dangling reference the moment it lands."""
    from kiro_crew.config.loader import config_local_path

    _seed_config()
    config_local_path().write_text(
        json.dumps({"agents": {"pr-bot": {"kiro_agent": "ghost-writer"}}}), encoding="utf-8"
    )
    resp = await api_agent_template_create(_request("POST", body={"name": "ghost-writer"}))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "name_bound"
    assert not (agents_dir / "ghost-writer.json").exists()


@pytest.mark.asyncio
async def test_create_from_an_ambiguous_name_is_a_409_not_a_500(agents_dir):
    """Two files declaring one name is the user's to untangle, and is said so."""
    _write(agents_dir, "atlas.json", name="atlas")
    _write(agents_dir, "SomePkg-atlas.json", name="atlas")
    _seed_config()
    resp = await api_agent_template_create(_request("POST", body={"name": "mine", "from": "atlas"}))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "ambiguous_template_name"
    assert not (agents_dir / "mine.json").exists()


@pytest.mark.asyncio
async def test_create_rejects_malformed_bodies(agents_dir):
    _seed_config()
    assert (await api_agent_template_create(_request("POST", bad_json=True))).status == 400
    assert (await api_agent_template_create(_request("POST", body=["x"]))).status == 400


# ── delete ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_refuses_while_referenced_and_lists_the_references(agents_dir):
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "reviewer"}, default="pr-bot")
    _seed_cron("reviewer")
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 409
    body = await _body(resp)
    assert body["code"] == "template_referenced"
    assert {(r["kind"], r["id"]) for r in body["references"]} == {
        ("crew", "pr-bot"),
        ("schedule", "job-1"),
    }
    assert (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
async def test_delete_refuses_while_a_chat_folder_pins_the_template(agents_dir):
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    folders = [{"id": "f-1", "name": "Reviews", "default_agent": "reviewer"}]
    resp = await api_agent_template_delete(_request("DELETE", "reviewer", folders=folders))
    assert resp.status == 409
    body = await _body(resp)
    assert body["code"] == "template_referenced"
    assert body["references"] == [{"kind": "folder", "id": "f-1", "label": "Reviews"}]
    assert (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
async def test_delete_checks_and_unlinks_inside_the_folder_store_hold(agents_dir):
    """A pin that lands while the guard runs is seen: the check and the unlink
    are one section run while the folder store lock is held, on the committed
    list, so there is no snapshot for a concurrent folder update to go stale
    against -- and the section's file work happens off the loop."""
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    request = _request("DELETE", "reviewer")
    live: list[dict] = []

    async def _hold(section):
        # The store as the guard sees it under the lock -- a folder pinned after
        # the request started but before the lock was taken.
        live.append({"id": "f-late", "name": "Late", "default_agent": "reviewer"})
        return await section(live)

    request.app["state"].hold_folders = AsyncMock(side_effect=_hold)
    resp = await api_agent_template_delete(request)
    assert resp.status == 409
    assert (await _body(resp))["references"] == [
        {"kind": "folder", "id": "f-late", "label": "Late"}
    ]
    assert (agents_dir / "reviewer.json").exists()
    # One hold for the whole section; no separate snapshot read.
    request.app["state"].hold_folders.assert_awaited_once()
    request.app["state"].read_folders.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_names_a_schedule_that_landed_in_the_unlocked_window(
    agents_dir, monkeypatch, caplog
):
    """The cron store is not under the guard's locks; a job written between the
    check and the unlink is re-read once the file is gone and named out loud --
    a warning and an audit row -- instead of failing first at its next fire."""
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    events: list[dict] = []
    monkeypatch.setattr(
        module,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    real_cron_ids = module._cron_agent_ids
    calls = {"n": 0}

    def _cron_ids_with_a_late_writer():
        calls["n"] += 1
        if calls["n"] == 1:
            return real_cron_ids()  # the guard's read: nothing yet
        _seed_cron("reviewer", name="late job")  # landed after the guard read
        return real_cron_ids()

    monkeypatch.setattr(module, "_cron_agent_ids", _cron_ids_with_a_late_writer)
    with caplog.at_level("WARNING", logger=module.logger.name):
        resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "reviewer.json").exists()
    assert any("still names it" in r.getMessage() for r in caplog.records)
    assert [(e["outcome"], e["resources"]) for e in events][-1] == (
        "dangling_reference",
        "template:reviewer holders:schedule:job-1",
    )


@pytest.mark.asyncio
async def test_delete_holds_the_cron_store_lock_from_the_check_through_the_rename(
    agents_dir, monkeypatch
):
    """The schedule store's own cross-process lock is held from the walk that
    finds no dispatching job until the file is renamed, so a scheduler write
    cannot land in between: a writer that contends for ``.crons.lock`` while the
    guard reads is still waiting when the template goes, and the guard's read
    and the rename both happen INSIDE the hold."""
    from kiro_crew import platform_compat
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    seen: list[str] = []
    lock_path = config_dir() / ".crons.lock"

    def _held() -> bool:
        # A second, non-blocking acquire on a separate fd fails while the guard
        # holds the lock (flock excludes across open descriptions).
        with platform_compat.open_lock_file(lock_path) as fd:
            if platform_compat.try_acquire_lock(fd, exclusive=True):
                platform_compat.release_lock(fd)
                return False
            return True

    real_cron_ids = module._cron_agent_ids
    real_tombstone = module._tombstone

    def _cron_ids_probe():
        seen.append("read:" + ("locked" if _held() else "free"))
        return real_cron_ids()

    def _tombstone_probe(spec_path):
        seen.append("rename:" + ("locked" if _held() else "free"))
        return real_tombstone(spec_path)

    monkeypatch.setattr(module, "_cron_agent_ids", _cron_ids_probe)
    monkeypatch.setattr(module, "_tombstone", _tombstone_probe)
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 200, resp.text
    # The guard's read and the rename ran under the store lock; the post-delete
    # re-read (the second read) runs after it is released.
    assert seen[:2] == ["read:locked", "rename:locked"]
    assert not _held()


@pytest.mark.asyncio
async def test_delete_refuses_while_the_cron_store_lock_is_held_elsewhere(agents_dir, monkeypatch):
    """A scheduler write holding ``.crons.lock`` past the bounded wait is a 503
    ``schedule_store_busy`` -- retryable -- with the file intact: the guard never
    claims "no schedule dispatches this" without the store pinned."""
    from kiro_crew import platform_compat
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    config_dir().mkdir(parents=True, exist_ok=True)
    # Wait only briefly: the point is the refusal shape, not the ten-second budget.
    real_lock = module.cron_store_lock
    monkeypatch.setattr(
        module, "cron_store_lock", lambda d, **kw: real_lock(d, timeout=0.2, poll=0.02)
    )
    with platform_compat.open_lock_file(config_dir() / ".crons.lock") as fd:
        assert platform_compat.try_acquire_lock(fd, exclusive=True)
        try:
            resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
        finally:
            platform_compat.release_lock(fd)
    assert resp.status == 503, resp.text
    assert (await _body(resp))["code"] == "schedule_store_busy"
    assert (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
async def test_delete_fails_closed_when_the_cron_store_is_unreadable(agents_dir):
    """A present-but-unreadable ``crons.json`` is not "no schedules": a repaired
    store brings its jobs back naming whatever they named, so the guard refuses
    (503) and unlinks nothing rather than reading the store as empty."""
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "crons.json").write_text("{not json", encoding="utf-8")
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 503, resp.text
    assert (await _body(resp))["code"] == "schedule_store_unreadable"
    assert (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
async def test_delete_counts_a_binding_that_lives_only_in_the_overlay(agents_dir):
    """``config.local.json`` deep-merges over the base and can carry a crew's
    effective ``kiro_agent`` on its own; the guard reads the merged config
    under both layers' locks."""
    from kiro_crew.config.loader import config_local_path

    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    config_local_path().write_text(
        json.dumps({"agents": {"pr-bot": {"kiro_agent": "reviewer"}}}), encoding="utf-8"
    )
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 409
    assert (await _body(resp))["references"] == [
        {"kind": "crew", "id": "pr-bot", "label": "pr-bot"}
    ]
    assert (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
async def test_delete_refuses_while_a_webhook_names_the_template(agents_dir):
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    _raw, _secret, entry = token_store().create(
        "ci-hook", require_signature=False, agent="reviewer"
    )
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 409
    assert (await _body(resp))["references"] == [
        {"kind": "webhook", "id": entry["id"], "label": "ci-hook"}
    ]
    assert (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job, expected",
    [
        # A multi-entry sequence is what dispatches; agent_id is dormant.
        ({"agent_id": "kirocrew", "agent_sequence": ["reviewer", "kirocrew"]}, 1),
        # The template named twice in one sequence is one holder.
        ({"agent_id": "", "agent_sequence": ["reviewer", "reviewer"]}, 1),
        # A single-entry sequence is dormant; agent_id runs, and it is not ours.
        ({"agent_id": "kirocrew", "agent_sequence": ["reviewer"]}, 0),
        # A script job runs no agent at all.
        ({"agent_id": "reviewer", "script": "~/.kiro/crew/crons/x.py:run"}, 0),
        # A schedule created from a template chat with no ``agent`` names its
        # template ONLY in the captured execution; ``agent_id`` stays empty.
        ({"agent_id": "", "execution_context": _EXECUTION_FOR("reviewer")}, 1),
        # The captured execution is what runs, over a stale ``agent_id``.
        ({"agent_id": "kirocrew", "execution_context": _EXECUTION_FOR("reviewer")}, 1),
        # ...and a dispatching sequence still wins over both.
        (
            {
                "agent_id": "",
                "agent_sequence": ["kirocrew", "kirocrew"],
                "execution_context": _EXECUTION_FOR("reviewer"),
            },
            0,
        ),
    ],
)
async def test_delete_guard_mirrors_cron_dispatch_not_storage(agents_dir, job, expected):
    """The guard counts what a schedule RUNS, the way ``job_agent_names_from_disk``
    reads it: a dispatching ``agent_sequence`` over a dormant ``agent_id``, and a
    script job over neither."""
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    _seed_cron(job.pop("agent_id"), **job)
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    if expected:
        assert resp.status == 409
        assert (await _body(resp))["references"] == [
            {"kind": "schedule", "id": "job-1", "label": "nightly triage"}
        ]
        assert (agents_dir / "reviewer.json").exists()
    else:
        assert resp.status == 200, resp.text
        assert not (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
async def test_delete_guard_ignores_a_cron_record_the_scheduler_cannot_load(agents_dir):
    """A record without a ``schedule`` never runs; it holds nothing."""
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "crons.json").write_text(
        json.dumps(
            {"jobs": [{"id": "job-x", "name": "broken", "message": "go", "agent_id": "reviewer"}]}
        ),
        encoding="utf-8",
    )
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
async def test_delete_refuses_the_default_agent_by_stem_too(agents_dir):
    _write(agents_dir, "scratch.json", name="Scratch Pad")
    cfg = KiroCrewConfig()
    cfg.agent.default_agent = "scratch"
    cfg.save()
    resp = await api_agent_template_delete(_request("DELETE", "scratch"))
    assert resp.status == 409
    # The fresh config's own `default` crew resolves the same template, so the
    # crew row rides along; the fallback row is what this test pins.
    assert "default" in {r["kind"] for r in (await _body(resp))["references"]}


@pytest.mark.asyncio
async def test_default_crew_alias_is_not_a_template_reference(agents_dir):
    """``cfg.default_agent`` names a CREW; a template that merely shares that
    crew's alias, while the crew runs another template, is unreferenced."""
    _write(agents_dir, "reviewer.json")
    _seed_config({"reviewer": "kirocrew"}, default="reviewer")
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "reviewer.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename, reason",
    [("SomePkg-atlas.json", "package"), ("kirocrew-worker.json", "runtime")],
)
async def test_delete_refuses_read_only_specs(agents_dir, filename, reason):
    _write(
        agents_dir,
        filename,
        name=(
            filename.split("-", 1)[1].rsplit(".", 1)[0]
            if reason == "package"
            else "kirocrew-worker"
        ),
    )
    _seed_config()
    name = "atlas" if reason == "package" else "kirocrew-worker"
    resp = await api_agent_template_delete(_request("DELETE", name))
    assert resp.status == 409
    body = await _body(resp)
    assert body["code"] == "template_read_only" and body["reason"] == reason
    assert (agents_dir / filename).exists()


def test_tombstone_keeps_the_older_grave_when_the_rename_is_refused(agents_dir, monkeypatch):
    """The rename to the fresh grave comes first and the older tombstone goes
    only after it succeeded: a refused rename (a sharing lock on Windows) must
    leave the live file AND the previous recovery copy exactly as they were."""
    spec = agents_dir / "reviewer.json"
    spec.write_text("{}", encoding="utf-8")
    older = agents_dir / "reviewer.json.bak.1"
    older.write_text('{"prompt": "older"}', encoding="utf-8")

    def refuse(self, target):  # noqa: ARG001 - Path.rename signature
        raise PermissionError("sharing violation")

    monkeypatch.setattr(Path, "rename", refuse)
    with pytest.raises(PermissionError):
        agent_templates._tombstone(spec)
    assert spec.exists()
    assert older.read_text(encoding="utf-8") == '{"prompt": "older"}'
    monkeypatch.undo()
    # Two deletes of the same name within one second still get distinct graves.
    grave = agent_templates._tombstone(spec)
    spec.write_text("{}", encoding="utf-8")
    second = agent_templates._tombstone(spec)
    assert grave != second and not older.exists()
    assert sorted(p.name for p in agents_dir.glob("reviewer.json.bak.*")) == [second]


def test_tombstone_sweep_spares_a_live_template_whose_name_looks_like_a_grave(agents_dir):
    """A glob's ``*`` crosses dots, so ``foo.json.bak.*`` also matches a live
    template legally named ``foo.json.bak.5`` (written to ``foo.json.bak.5.json``).
    The sweep must unlink only a genuine grave of THIS file -- ``<name>.bak.<epoch>``
    or its same-second twin -- never a spec, whatever it is called."""
    spec = agents_dir / "foo.json"
    spec.write_text("{}", encoding="utf-8")
    lookalike = agents_dir / "foo.json.bak.5.json"
    lookalike.write_text('{"name": "foo.json.bak.5"}', encoding="utf-8")
    markdown = agents_dir / "foo.json.bak.7.md"
    markdown.write_text("# md", encoding="utf-8")
    older = agents_dir / "foo.json.bak.1"
    older.write_text("{}", encoding="utf-8")
    twin = agents_dir / "foo.json.bak.1.1"
    twin.write_text("{}", encoding="utf-8")

    grave = agent_templates._tombstone(spec)

    assert lookalike.exists() and markdown.exists()
    assert not older.exists() and not twin.exists()
    assert sorted(p.name for p in agents_dir.glob("foo.json.bak.*")) == sorted(
        [grave, lookalike.name, markdown.name]
    )


@pytest.mark.asyncio
async def test_delete_removes_an_unreferenced_template(agents_dir):
    """The spec leaves the roster (its ``.json`` is gone, so discovery never
    sees it again) but is retired as a one-deep tombstone beside itself --
    ``<name>.json.bak.<epoch>``, the janitor's own backup shape -- so the one
    irreversible action on the tab is a rename a person can undo."""
    _write(agents_dir, "reviewer.json", prompt="hand-written")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    agent_state.set_model_managed("reviewer", True)
    (agents_dir / "reviewer.json.bak.1").write_text("{}", encoding="utf-8")  # an older grave
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "reviewer.json").exists()
    assert agent_state.get_model_managed("reviewer") is None
    graves = sorted(agents_dir.glob("reviewer.json.bak.*"))
    assert len(graves) == 1 and graves[0].name != "reviewer.json.bak.1"
    assert json.loads(graves[0].read_text(encoding="utf-8"))["prompt"] == "hand-written"
    # The tombstone is invisible to the roster.
    assert "reviewer" not in {
        r["name"] for r in (await _body(await api_agent_templates(_request("GET"))))["templates"]
    }


@pytest.mark.asyncio
async def test_post_delete_observation_cannot_turn_the_delete_into_a_failure(
    agents_dir, monkeypatch, caplog
):
    """Once the file is gone, a store that fails to read during the post-delete
    reference check is a WARNING, not a refusal: the client must not hear
    "failed" about a template that has been removed."""
    from kiro_crew.cron import CronStoreUnreadable
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    real = module._cron_agent_ids
    calls = {"n": 0}

    def _readable_then_not():
        calls["n"] += 1
        if calls["n"] == 2:  # the post-delete re-read: the store broke meanwhile
            raise CronStoreUnreadable("crons.json")
        return real()

    monkeypatch.setattr(module, "_cron_agent_ids", _readable_then_not)
    with caplog.at_level("WARNING", logger=module.logger.name):
        resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 200, resp.text
    assert not (agents_dir / "reviewer.json").exists()
    assert any("post-delete reference check" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("escape", ["absolute", "traversal", "nested"])
async def test_delete_never_unlinks_a_row_whose_filename_is_not_a_spec_beneath_agents_dir(
    agents_dir, tmp_path, monkeypatch, escape
):
    """A discovery row's ``filename`` is a string some provider wrote (an edition
    catalog row carries whatever its provider put there); the delete path never
    treats it as a path. Only a plain basename that resolves to a regular file
    directly under the agents directory names a deletable template."""
    from kiro_crew.agent_discovery import AgentInfo
    from kiro_crew.dashboard.handlers import agent_templates as module

    victim = tmp_path / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    nested = agents_dir / "sub"
    nested.mkdir()
    (nested / "inner.json").write_text("{}", encoding="utf-8")
    filename = {
        "absolute": str(victim),
        "traversal": "../victim.json",
        "nested": "sub/inner.json",
    }[escape]
    _seed_config()
    rows = [AgentInfo(name="rogue", filename=filename, description="", model="")]
    monkeypatch.setattr(module, "_find_infos", lambda _name: rows)
    resp = await api_agent_template_delete(_request("DELETE", "rogue"))
    assert resp.status == 404, resp.text
    assert victim.exists() and (nested / "inner.json").exists()


@pytest.mark.asyncio
async def test_delete_never_unlinks_a_file_that_does_not_answer_to_the_requested_name(
    agents_dir, monkeypatch
):
    """A row pairs a name with a filename, and both are provider strings: an
    edition row saying ``rogue`` over ``victim.json`` passes the path guard, so
    the FILE is re-read under the spec lock and must itself answer to the
    requested name (declared name or stem) before anything is unlinked."""
    from kiro_crew.agent_discovery import AgentInfo
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "victim.json", name="victim")
    _seed_config()
    rows = [AgentInfo(name="rogue", filename="victim.json", description="", model="")]
    monkeypatch.setattr(module, "_find_infos", lambda _name: rows)
    resp = await api_agent_template_delete(_request("DELETE", "rogue"))
    assert resp.status == 404, resp.text
    assert (await _body(resp))["code"] == "template_not_found"
    assert (agents_dir / "victim.json").exists()


@pytest.mark.asyncio
async def test_delete_classifies_the_file_on_disk_not_the_row(agents_dir, monkeypatch):
    """A row that calls a package file plain does not make it deletable: the
    read-only rule is re-applied to the file's own contents under the lock."""
    from kiro_crew.agent_discovery import AgentInfo
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "SomePkg-atlas.json", name="atlas")
    _seed_config()
    rows = [AgentInfo(name="atlas", filename="SomePkg-atlas.json", description="", model="")]
    monkeypatch.setattr(module, "_find_infos", lambda _name: rows)
    resp = await api_agent_template_delete(_request("DELETE", "atlas"))
    assert resp.status == 409, resp.text
    body = await _body(resp)
    assert body["code"] == "template_read_only" and body["reason"] == "package"
    assert (agents_dir / "SomePkg-atlas.json").exists()


@pytest.mark.asyncio
async def test_delete_refusal_masks_reference_labels_like_the_roster(agents_dir):
    from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK

    probe = "AKIAIOSFODNN7EXAMPLE"
    _write(agents_dir, "reviewer.json")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    _seed_cron("reviewer", name=f"nightly {probe}")
    resp = await api_agent_template_delete(_request("DELETE", "reviewer"))
    assert resp.status == 409
    assert (await _body(resp))["references"] == [
        {"kind": "schedule", "id": "job-1", "label": _SENSITIVE_MASK}
    ]


@pytest.mark.asyncio
async def test_delete_refuses_a_name_that_reaches_two_files(agents_dir):
    """``bar.json`` declares ``baz`` while ``foo.json`` declares ``bar``: the name
    ``bar`` reaches one file by stem and another by declared name. Neither is
    unlinked; the user renames one first."""
    _write(agents_dir, "bar.json", name="baz")
    _write(agents_dir, "foo.json", name="bar")
    _seed_config({"pr-bot": "kirocrew"}, default="pr-bot")
    resp = await api_agent_template_delete(_request("DELETE", "bar"))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "ambiguous_template_name"
    assert (agents_dir / "bar.json").exists() and (agents_dir / "foo.json").exists()


@pytest.mark.asyncio
async def test_delete_refuses_a_name_two_files_both_declare(agents_dir):
    """The roster keeps one row per declared name, so a twin is invisible
    there; the delete resolves from the raw file scan and refuses."""
    _write(agents_dir, "a.json", name="bar")
    _write(agents_dir, "b.json", name="bar")
    _seed_config()
    resp = await api_agent_template_delete(_request("DELETE", "bar"))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "ambiguous_template_name"
    assert (agents_dir / "a.json").exists() and (agents_dir / "b.json").exists()


@pytest.mark.asyncio
async def test_delete_revalidates_ambiguity_under_the_spec_lock(agents_dir, monkeypatch):
    """A second file reaching the name that lands AFTER the pre-lock probe (a
    package install) must refuse under the lock, not let the stale single match
    unlink the user's own file."""
    from kiro_crew.dashboard.handlers import agent_templates as module

    _write(agents_dir, "bar.json", name="bar")
    _seed_config()
    real = module._find_infos
    calls = {"n": 0}

    def _probe_then_install(name_):
        calls["n"] += 1
        if calls["n"] == 2:  # the locked re-check: a package file landed meanwhile
            _write(agents_dir, "Pkg-bar.json", name="bar")
        return real(name_)

    monkeypatch.setattr(module, "_find_infos", _probe_then_install)
    resp = await api_agent_template_delete(_request("DELETE", "bar"))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "ambiguous_template_name"
    assert (agents_dir / "bar.json").exists() and (agents_dir / "Pkg-bar.json").exists()


@pytest.mark.asyncio
async def test_delete_unknown_is_404(agents_dir):
    _seed_config()
    assert (await api_agent_template_delete(_request("DELETE", "ghost"))).status == 404


# ── PATCH: the definition keys ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_writes_the_definition_keys_on_an_owned_template(agents_dir):
    _write(agents_dir, "reviewer.json", prompt="old", description="old")
    _seed_config()
    resp = await api_agent_detail(
        _request(
            "PATCH",
            "reviewer",
            {
                "prompt": "Review carefully.",
                "description": "Careful reviewer",
                "tools": ["fs_read", "grep", "@docs/search"],
                # An MCP ref survives; a builtin (`fs_read`) would be withheld by
                # the governance sanitizer, whose floor the ceiling may speak to.
                "allowedTools": ["@docs/search", "fs_read"],
            },
        )
    )
    assert resp.status == 200, resp.text
    spec = json.loads((agents_dir / "reviewer.json").read_text(encoding="utf-8"))
    assert spec["prompt"] == "Review carefully."
    assert spec["description"] == "Careful reviewer"
    assert spec["tools"] == ["fs_read", "grep", "@docs/search"]
    assert spec["allowedTools"] == ["@docs/search"]


@pytest.mark.asyncio
async def test_patch_definition_is_refused_on_a_package_template(agents_dir):
    _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="shipped")
    _seed_config()
    resp = await api_agent_detail(_request("PATCH", "atlas", {"prompt": "mine now"}))
    assert resp.status == 409
    assert (await _body(resp))["code"] == "template_read_only"
    spec = json.loads((agents_dir / "SomePkg-atlas.json").read_text(encoding="utf-8"))
    assert spec["prompt"] == "shipped"


@pytest.mark.asyncio
async def test_patch_definition_classifies_the_targeted_file_not_its_name(agents_dir):
    """``atlas.json`` beside ``SomePkg-atlas.json``: the roster keeps only the
    package twin, so a lookup by declared name would answer for the wrong file.
    The plain file is the user's and stays editable; the package file stays
    refused, whichever the name-first lookup happens to land on."""
    _write(agents_dir, "atlas.json", name="atlas", prompt="mine")
    _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="shipped")
    _seed_config()
    from kiro_crew.dashboard.handlers.agent_templates import read_only_reason_for_path

    assert read_only_reason_for_path(agents_dir / "atlas.json") is None
    assert read_only_reason_for_path(agents_dir / "SomePkg-atlas.json") == "package"


@pytest.mark.asyncio
async def test_patch_definition_refuses_an_ambiguous_name(agents_dir):
    """Two files claiming one name would be resolved by scan order, so the file
    the roster showed and the file rewritten could differ: refused for EVERY
    key a PATCH can write -- a model write picks one file just as a prompt
    write does -- with neither file touched."""
    _write(agents_dir, "atlas.json", name="atlas", prompt="mine")
    _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="shipped")
    _seed_config()
    for body in ({"prompt": "rewritten"}, {"model": "claude-x"}):
        resp = await api_agent_detail(_request("PATCH", "atlas", body))
        assert resp.status == 409, resp.text
        assert (await _body(resp))["code"] == "ambiguous_template_name"
    for f in ("atlas.json", "SomePkg-atlas.json"):
        spec = json.loads((agents_dir / f).read_text(encoding="utf-8"))
        assert spec["prompt"] != "rewritten" and "model" not in spec


@pytest.mark.asyncio
async def test_patch_revalidates_ambiguity_under_the_write_lock(agents_dir, monkeypatch):
    """A second claimant landing between the scan and the write (a package
    install) refuses under the lock instead of overwriting the stale match."""
    from kiro_crew.dashboard.handlers import agents as agents_module

    _write(agents_dir, "atlas.json", name="atlas", prompt="mine")
    _seed_config()
    real = agents_module._agent_detail_candidates
    calls = {"n": 0}

    def _scan_then_install(name_):
        calls["n"] += 1
        if calls["n"] == 2:  # the locked re-check: a package file landed meanwhile
            _write(agents_dir, "SomePkg-atlas.json", name="atlas", prompt="shipped")
        return real(name_)

    monkeypatch.setattr(agents_module, "_agent_detail_candidates", _scan_then_install)
    resp = await api_agent_detail(_request("PATCH", "atlas", {"prompt": "rewritten"}))
    assert resp.status == 409, resp.text
    assert (await _body(resp))["code"] == "ambiguous_template_name"
    assert json.loads((agents_dir / "atlas.json").read_text(encoding="utf-8"))["prompt"] == "mine"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"prompt": 12},
        {"tools": "fs_read"},
        {"tools": ["", "x"]},
        {"tools": ["x" * 257]},
        {"description": "x" * 2001},
    ],
)
async def test_patch_definition_shape_is_validated(agents_dir, body):
    _write(agents_dir, "reviewer.json", prompt="old")
    _seed_config()
    resp = await api_agent_detail(_request("PATCH", "reviewer", body))
    assert resp.status == 400
    assert (await _body(resp))["code"] == "invalid_definition"
