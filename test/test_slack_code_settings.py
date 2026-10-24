"""Settings → Slack: the Slack Code switches, code-channel defaults and drift status.

Covers the PUT validation (every field, and the nearby inputs a user actually
produces: a folder deleted after it was saved, an agent removed after it was
saved, a model the account cannot use, an effort off the ladder), the GET read
model with its stale-default warnings, the SEL trail, and the manifest-drift
probe against recorded Slack payloads (no live workspace).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.core as core_handlers
import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew import slack_manifest
from kiro_crew.dashboard import slack_code_settings as scs
from kiro_crew.slack import features, scope_probe

#: The real validator, taken before any fixture stubs it. ``handlers.core`` is a
#: mirrored module, so the fixture's stub also lands in its owner module and the
#: original can't be read back from either once a test runs.
_REAL_VALIDATE_ROLE_MODEL = core_handlers._validate_role_model

_FOLDERS = [
    {"id": "aaaaaaaaaaaa", "name": "Dev", "parent_id": "", "project_dir": "/tmp"},
    {
        "id": "bbbbbbbbbbbb",
        "name": "Slack Code",
        "parent_id": "aaaaaaaaaaaa",
        "default_agent": "builder",
    },
    {"id": "cccccccccccc", "name": "Twin", "parent_id": ""},
    {"id": "dddddddddddd", "name": "Twin", "parent_id": ""},
]


class _State:
    def __init__(self, folders: list[dict] | None = None) -> None:
        self._folders = list(_FOLDERS if folders is None else folders)
        self.slack_socket_connected = False
        self.slack_connect_error = ""


class _SelRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.calls.append(kw)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    envf = tmp_path / ".env"
    envf.write_text("", encoding="utf-8")
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(loader, "env_path", lambda: envf)
    monkeypatch.setattr(loader, "config_path", lambda: cfg)
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)
    sel = _SelRecorder()
    monkeypatch.setattr(mod, "_sel", lambda: sel)

    async def _agents(project_dir: str | None) -> list[str]:
        names = ["builder", "reviewer"]
        if project_dir == "/tmp":
            names.append("project-only")
        return names

    monkeypatch.setattr(scs, "_agent_names", _agents)
    monkeypatch.setattr(
        core_handlers,
        "_validate_role_model",
        lambda value, request, *a, **k: None if value.startswith("claude-") else "not available",
    )

    async def _no_revalidation(value: str, request: Any, *, backend: str | None = None) -> None:
        return None

    monkeypatch.setattr(core_handlers, "_revalidate_role_pin_evidence", _no_revalidation)
    monkeypatch.setattr(core_handlers, "_active_provider_and_pin_backend", lambda: ("", ""))
    return {"cfg": cfg, "sel": sel, "state": _State()}


def _call(env: dict[str, Any], method: str, body: dict | None = None) -> tuple[int, dict]:
    async def _run() -> tuple[int, dict]:
        app = web.Application()
        app["state"] = env["state"]
        app.router.add_put("/api/slack/config", mod.api_slack_config_save)
        app.router.add_get("/api/slack/config", mod.api_slack_config_get)
        async with TestClient(TestServer(app)) as client:
            if method == "PUT":
                resp = await client.put("/api/slack/config", json=body)
            else:
                resp = await client.get("/api/slack/config")
            return resp.status, await resp.json()

    return asyncio.run(_run())


def _stored(env: dict[str, Any]) -> dict:
    return json.loads(env["cfg"].read_text(encoding="utf-8"))["slack"]


class TestSwitches:
    def test_defaults_read_off(self, env: dict) -> None:
        status, body = _call(env, "GET")
        assert status == 200
        assert body["code_channels"] is False
        # Only registered switches are reported; agent sessions lands with its
        # runtime, so this build neither reports nor accepts it.
        assert "agent_sessions" not in body
        assert body["code_channel_defaults"] == {
            "folder": None,
            "agent": None,
            "model": None,
            "reasoning_effort": None,
        }
        assert body["code_channel_warnings"] == []

    @pytest.mark.parametrize("on", [True, False])
    def test_the_switch_saves_and_reads_back(self, env: dict, on: bool) -> None:
        status, res = _call(env, "PUT", {"code_channels": on})
        assert status == 200
        # Hot-applied: the runtime reads the toggle per event, behind the
        # capability guard, so no flip needs a restart.
        assert res["restart_required"] is False
        _, body = _call(env, "GET")
        assert body["code_channels"] is on

    def test_an_unregistered_switch_is_not_stored(self, env: dict) -> None:
        _call(env, "PUT", {"agent_sessions": True, "code_channels": True})
        stored = _stored(env)
        assert stored["code_channels"] is True
        assert "agent_sessions" not in stored

    def test_unchanged_switch_is_not_staged(self, env: dict) -> None:
        _call(env, "PUT", {"code_channels": True})
        status, _ = _call(env, "PUT", {"code_channels": True})
        assert status == 200
        ok = [c for c in env["sel"].calls if c["outcome"] == "ok"]
        assert "code_channels" in ok[0]["resources"]
        assert "code_channels" not in ok[1]["resources"]

    def test_toggle_on_reports_the_capability_it_still_lacks(self, env: dict) -> None:
        from kiro_crew.slack import features

        features.reset_capabilities()
        _call(env, "PUT", {"code_channels": True})
        _, body = _call(env, "GET")
        assert body["code_channels"] is True
        assert body["code_channels_capability"] == {
            "available": False,
            "reason": "scopes_unknown",
            "scope": "code_channels:manage",
        }
        features.record_granted_scopes({"chat:write"})
        _, body = _call(env, "GET")
        assert body["code_channels_capability"]["reason"] == "missing_scope"
        features.record_granted_scopes({"chat:write", "code_channels:manage"})
        _, body = _call(env, "GET")
        assert body["code_channels_capability"] == {
            "available": True,
            "reason": "",
            "scope": "code_channels:manage",
        }
        features.reset_capabilities()

    @pytest.mark.parametrize("bad", ["true", 1, None])
    def test_switch_must_be_a_boolean(self, env: dict, bad: object) -> None:
        status, res = _call(env, "PUT", {"code_channels": bad})
        assert status == 400 and "boolean" in res["error"]
        assert env["sel"].calls[-1]["outcome"] == "denied"
        assert not env["cfg"].exists()


class TestDefaults:
    def test_folder_path_is_stored_by_id(self, env: dict) -> None:
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"folder": "dev / slack code"}})
        assert status == 200
        assert _stored(env)["code_channel_defaults"]["folder"] == "bbbbbbbbbbbb"
        _, body = _call(env, "GET")
        eff = body["code_channel_effective"]
        assert eff["folder"] == {
            "id": "bbbbbbbbbbbb",
            "path": "Dev/Slack Code",
            "project_dir": "/tmp",
            "default_agent": "builder",
        }
        assert (eff["agent"], eff["agent_source"]) == ("builder", "folder")
        assert (eff["project_dir"], eff["project_source"]) == ("/tmp", "folder")

    @pytest.mark.parametrize("ref", ["Nope", "Twin", "999999999999"])
    def test_unknown_or_ambiguous_folder_is_refused(self, env: dict, ref: str) -> None:
        status, res = _call(env, "PUT", {"code_channel_defaults": {"folder": ref}})
        assert status == 400 and "folder" in res["error"]
        assert env["sel"].calls[-1]["outcome"] == "denied"

    def test_agent_must_exist(self, env: dict) -> None:
        status, res = _call(env, "PUT", {"code_channel_defaults": {"agent": "ghost"}})
        assert status == 400 and "ghost" in res["error"]

    def test_agent_is_checked_in_the_folders_project_scope(self, env: dict) -> None:
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"agent": "project-only"}})
        assert status == 400
        status, _ = _call(
            env,
            "PUT",
            {"code_channel_defaults": {"folder": "bbbbbbbbbbbb", "agent": "project-only"}},
        )
        assert status == 200

    def test_explicit_agent_wins_over_folder(self, env: dict) -> None:
        _call(
            env, "PUT", {"code_channel_defaults": {"folder": "bbbbbbbbbbbb", "agent": "reviewer"}}
        )
        _, body = _call(env, "GET")
        assert body["code_channel_effective"]["agent_source"] == "default"
        assert body["code_channel_effective"]["agent"] == "reviewer"

    def test_invalid_model_is_refused(self, env: dict) -> None:
        status, res = _call(env, "PUT", {"code_channel_defaults": {"model": "gpt-nope"}})
        assert status == 400 and "not available" in res["error"]

    def test_model_is_judged_on_the_default_harness(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, Any]] = []

        async def _revalidate(value: str, request: Any, *, backend: str | None = None) -> None:
            seen.append(("revalidate", backend))

        def _validate(value: str, request: Any, provider: Any = None, *, backend: Any = None):
            seen.append(("validate", (provider, backend)))
            return None

        monkeypatch.setattr(
            core_handlers, "_active_provider_and_pin_backend", lambda: ("kiro", "codex")
        )
        monkeypatch.setattr(core_handlers, "_revalidate_role_pin_evidence", _revalidate)
        monkeypatch.setattr(core_handlers, "_validate_role_model", _validate)
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"model": "gpt-5"}})
        assert status == 200
        assert seen == [("revalidate", "codex"), ("validate", ("kiro", "codex"))]

    @pytest.mark.parametrize(("harness", "accepted"), [("codex", True), ("", False)])
    def test_the_real_validator_judges_against_the_default_harness_catalog(
        self, env: dict, monkeypatch: pytest.MonkeyPatch, harness: str, accepted: bool
    ) -> None:
        # The real validator, not the fixture's stub: only the live catalogs are
        # faked, one per harness, so the result depends on the backend it is given.
        from kiro_crew.dashboard.handlers import core_config_models

        catalogs = {"codex": ["codex-only-model"], None: ["claude-opus-4.8"]}
        monkeypatch.setattr(
            core_config_models,
            "_active_advertised_ids",
            lambda request, backend=None: catalogs.get(backend or None),
        )
        monkeypatch.setattr(core_handlers, "_validate_role_model", _REAL_VALIDATE_ROLE_MODEL)
        monkeypatch.setattr(
            core_handlers, "_active_provider_and_pin_backend", lambda: ("", harness)
        )
        status, res = _call(env, "PUT", {"code_channel_defaults": {"model": "codex-only-model"}})
        assert (status == 200) is accepted, res

    def test_model_waiting_on_revalidation_is_refused(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _pending(value: str, request: Any, *, backend: str | None = None) -> str:
            return "the model list is still loading"

        monkeypatch.setattr(core_handlers, "_revalidate_role_pin_evidence", _pending)
        status, res = _call(env, "PUT", {"code_channel_defaults": {"model": "claude-x"}})
        assert status == 400 and "still loading" in res["error"]

    def test_inherit_model_is_stored_as_unset(self, env: dict) -> None:
        _call(env, "PUT", {"code_channel_defaults": {"model": "claude-opus-4.8"}})
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"model": "auto"}})
        assert status == 200
        assert _stored(env)["code_channel_defaults"]["model"] is None

    @pytest.mark.parametrize("effort", ["extreme", "HIGH", 3])
    def test_effort_off_the_ladder_is_refused(self, env: dict, effort: object) -> None:
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"reasoning_effort": effort}})
        assert status == 400

    def test_a_stored_off_ladder_effort_does_not_block_other_saves(self, env: dict) -> None:
        # A hand edit / `kirocrew config set` left an effort the ladder lacks.
        # The page reads it as unset, so a save of another default must not be
        # refused over it, and the stored value is left alone (not this save's).
        env["cfg"].write_text(
            json.dumps({"slack": {"code_channel_defaults": {"reasoning_effort": "High"}}}),
            encoding="utf-8",
        )
        _, body = _call(env, "GET")
        assert body["code_channel_defaults"]["reasoning_effort"] is None
        status, res = _call(env, "PUT", {"code_channel_defaults": {"folder": "bbbbbbbbbbbb"}})
        assert status == 200, res
        stored = _stored(env)["code_channel_defaults"]
        assert stored["folder"] == "bbbbbbbbbbbb"
        assert stored["reasoning_effort"] == "High"

    def test_full_set_saves_and_omitted_keys_are_kept(self, env: dict) -> None:
        full = {
            "folder": "bbbbbbbbbbbb",
            "agent": "reviewer",
            "model": "claude-opus-4.8",
            "reasoning_effort": "high",
        }
        assert _call(env, "PUT", {"code_channel_defaults": full})[0] == 200
        assert _call(env, "PUT", {"code_channel_defaults": {"reasoning_effort": "low"}})[0] == 200
        assert _stored(env)["code_channel_defaults"] == {**full, "reasoning_effort": "low"}
        ok = [c for c in env["sel"].calls if c["outcome"] == "ok"]
        assert "code_channel_defaults" in ok[-1]["resources"]

    def test_a_concurrent_edit_to_another_default_survives(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A save that also verifies a token gives another writer (the CLI) a
        # window between this handler's validation and its locked write.
        _call(env, "PUT", {"code_channel_defaults": {"agent": "reviewer", "model": "claude-x"}})

        async def _verify_while_cli_writes(key: str, token: str) -> None:
            data = json.loads(env["cfg"].read_text(encoding="utf-8"))
            data["slack"]["code_channel_defaults"]["agent"] = "builder"
            env["cfg"].write_text(json.dumps(data), encoding="utf-8")
            return None

        monkeypatch.setattr(mod, "_validate_slack_token", _verify_while_cli_writes)
        status, _ = _call(
            env,
            "PUT",
            {"bot_token": "xoxb-test", "code_channel_defaults": {"reasoning_effort": "high"}},
        )
        assert status == 200
        stored = _stored(env)["code_channel_defaults"]
        assert stored["agent"] == "builder"  # the CLI's edit is kept
        assert stored["reasoning_effort"] == "high"  # and so is this save's
        assert stored["model"] == "claude-x"

    def test_a_failed_save_undoes_its_default_but_keeps_a_concurrent_sibling(self) -> None:
        # Commit -> the CLI changes a sibling default -> the paired .env write
        # fails -> rollback: this save's field goes back, the CLI's stays.
        write = mod._LockedSectionWrite(
            Path("unused"),
            "slack",
            {"code_channel_defaults": {"reasoning_effort": "high"}},
            merge_nested=("code_channel_defaults",),
        )
        fresh = {
            "slack": {"code_channel_defaults": {"agent": "reviewer", "reasoning_effort": "low"}}
        }
        fresh = write.apply(fresh)
        assert fresh["slack"]["code_channel_defaults"] == {
            "agent": "reviewer",
            "reasoning_effort": "high",
        }
        fresh["slack"]["code_channel_defaults"]["agent"] = "builder"  # the CLI, in between
        fresh = write.restore(fresh)
        assert fresh["slack"]["code_channel_defaults"] == {
            "agent": "builder",
            "reasoning_effort": "low",
        }

    def test_a_failed_first_save_of_defaults_leaves_no_empty_object(self) -> None:
        write = mod._LockedSectionWrite(
            Path("unused"),
            "slack",
            {"code_channel_defaults": {"agent": "reviewer"}},
            merge_nested=("code_channel_defaults",),
        )
        fresh = write.restore(write.apply({"slack": {"enabled": True}}))
        assert fresh == {"slack": {"enabled": True}}

    def test_a_field_someone_else_changed_since_is_not_reverted(self) -> None:
        write = mod._LockedSectionWrite(
            Path("unused"),
            "slack",
            {"code_channel_defaults": {"agent": "reviewer"}},
            merge_nested=("code_channel_defaults",),
        )
        fresh = write.apply({"slack": {"code_channel_defaults": {"agent": "old"}}})
        fresh["slack"]["code_channel_defaults"]["agent"] = "theirs"
        assert write.restore(fresh)["slack"]["code_channel_defaults"] == {"agent": "theirs"}

    def test_null_and_blank_clear(self, env: dict) -> None:
        _call(env, "PUT", {"code_channel_defaults": {"agent": "reviewer", "model": "claude-x"}})
        _call(env, "PUT", {"code_channel_defaults": {"agent": None, "model": "  "}})
        stored = _stored(env)["code_channel_defaults"]
        assert stored["agent"] is None and stored["model"] is None

    def test_defaults_do_not_need_a_restart(self, env: dict) -> None:
        status, res = _call(env, "PUT", {"code_channel_defaults": {"agent": "reviewer"}})
        assert status == 200 and res["restart_required"] is False

    @pytest.mark.parametrize(
        "raw", ["Dev", ["x"], {"colour": "red"}, {"agent": 5}, {"agent": "a" * 300}]
    )
    def test_malformed_defaults_are_refused(self, env: dict, raw: object) -> None:
        status, _ = _call(env, "PUT", {"code_channel_defaults": raw})
        assert status == 400


class TestFolderScopedAgents:
    """``GET /api/slack/code-channel-agents``: the list a save accepts for an
    unsaved folder pick, so the picker follows the folder's project."""

    def _agents(self, env: dict, folder: str) -> tuple[int, dict]:
        from kiro_crew.dashboard.routes import messaging as messaging_routes

        async def _run() -> tuple[int, dict]:
            app = web.Application()
            app["state"] = env["state"]
            messaging_routes.register(app)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/slack/code-channel-agents", params={"folder": folder})
                return resp.status, await resp.json()

        return asyncio.run(_run())

    def test_unset_unresolvable_and_ambiguous_folders(self, env: dict) -> None:
        # No auto folder yet, a ref that names nothing: no project to look in.
        for folder in ("", "Nowhere/At all"):
            assert "project-only" not in self._agents(env, folder)[1]["agents"]
        auto = {"name": features.AUTO_FOLDER_NAME, "parent_id": "", "project_dir": "/tmp"}
        env["state"]._folders = [*_FOLDERS, {"id": "eeeeeeeeeeee", **auto}]
        assert "project-only" in self._agents(env, "")[1]["agents"]
        env["state"]._folders.append({"id": "ffffffffffff", **auto})
        assert "project-only" not in self._agents(env, "")[1]["agents"]

    def test_lists_the_picked_folders_project_agents(self, env: dict) -> None:
        # "Slack Code" inherits /tmp from its parent; "Twin" has no project.
        assert "project-only" in self._agents(env, "bbbbbbbbbbbb")[1]["agents"]
        assert "project-only" in self._agents(env, "Dev/Slack Code")[1]["agents"]
        assert "project-only" not in self._agents(env, "cccccccccccc")[1]["agents"]

    def test_the_list_is_what_a_save_with_that_folder_accepts(self, env: dict) -> None:
        for folder in ("bbbbbbbbbbbb", "cccccccccccc"):
            offered = self._agents(env, folder)[1]["agents"]
            status, _ = _call(
                env, "PUT", {"code_channel_defaults": {"folder": folder, "agent": "project-only"}}
            )
            assert (status == 200) == ("project-only" in offered)

    def test_unlistable_agents_read_as_null(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _broken(project_dir: str | None) -> list[str]:
            raise OSError("discovery failed")

        monkeypatch.setattr(scs, "_agent_names", _broken)
        assert self._agents(env, "bbbbbbbbbbbb") == (200, {"agents": None})


class TestStaleDefaults:
    def test_folder_deleted_after_save(self, env: dict) -> None:
        _call(
            env, "PUT", {"code_channel_defaults": {"folder": "bbbbbbbbbbbb", "model": "claude-x"}}
        )
        env["state"]._folders = [f for f in _FOLDERS if f["id"] != "bbbbbbbbbbbb"]
        status, body = _call(env, "GET")
        assert status == 200
        assert "folder_missing" in body["code_channel_warnings"]
        eff = body["code_channel_effective"]
        assert eff["folder"] is None and eff["project_dir"] is None
        assert eff["model"] == "claude-x"  # the other defaults still apply
        # Saving an unrelated field keeps the stale id rather than refusing.
        assert _call(env, "PUT", {"code_channel_defaults": {"reasoning_effort": "low"}})[0] == 200

    def test_agent_only_save_checks_a_path_stored_folders_project(self, env: dict) -> None:
        # A hand-edited config names the folder by path; changing only the agent
        # must check it against that folder's project, as the picker offered it.
        env["cfg"].write_text(
            json.dumps({"slack": {"code_channel_defaults": {"folder": "Dev/Slack Code"}}}),
            encoding="utf-8",
        )
        status, body = _call(env, "PUT", {"code_channel_defaults": {"agent": "project-only"}})
        assert status == 200, body
        saved = _stored(env)["code_channel_defaults"]
        assert saved["agent"] == "project-only"
        assert saved["folder"] == "Dev/Slack Code"  # left as stored

    def test_agent_removed_after_save(self, env: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        _call(env, "PUT", {"code_channel_defaults": {"agent": "reviewer"}})

        async def _fewer(project_dir: str | None) -> list[str]:
            return ["builder"]

        monkeypatch.setattr(scs, "_agent_names", _fewer)
        _, body = _call(env, "GET")
        assert "agent_missing" in body["code_channel_warnings"]


class TestAutoFolder:
    def test_unset_folder_reports_the_auto_folder_and_the_conductor(self, env: dict) -> None:
        _, body = _call(env, "GET")
        assert body["code_channel_auto_folder"] == {
            "name": "Code channels",
            "agent": "kirocrew-conductor",
            "id": None,
            "ambiguous": False,
        }
        eff = body["code_channel_effective"]
        assert (eff["folder_path"], eff["folder_auto_create"]) == ("Code channels", True)
        assert (eff["agent"], eff["agent_source"]) == ("kirocrew-conductor", "folder")
        assert eff["folder"] is None
        assert body["code_channel_agents"] == ["builder", "reviewer"]

    def test_existing_auto_folder_reports_its_own_settings(self, env: dict) -> None:
        env["state"]._folders = [
            *_FOLDERS,
            {
                "id": "eeeeeeeeeeee",
                "name": "Code channels",
                "parent_id": "",
                "default_agent": "reviewer",
            },
        ]
        _, body = _call(env, "GET")
        assert body["code_channel_auto_folder"]["id"] == "eeeeeeeeeeee"
        eff = body["code_channel_effective"]
        assert eff["folder_auto_create"] is False
        assert eff["folder"]["id"] == "eeeeeeeeeeee"
        assert (eff["agent"], eff["agent_source"]) == ("reviewer", "folder")

    def test_two_auto_folders_are_ambiguous_and_nothing_is_created(self, env: dict) -> None:
        env["state"]._folders = [
            *_FOLDERS,
            {"id": "eeeeeeeeeeee", "name": "Code channels", "parent_id": ""},
            {"id": "ffffffffffff", "name": "code channels", "parent_id": ""},
        ]
        _, body = _call(env, "GET")
        auto = body["code_channel_auto_folder"]
        assert (auto["id"], auto["ambiguous"]) == (None, True)
        eff = body["code_channel_effective"]
        assert (eff["folder"], eff["folder_path"], eff["folder_auto_create"]) == (None, None, False)
        assert eff["agent_source"] != "folder"

    def test_agent_list_failure_reads_as_unknown_not_empty(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _broken(project_dir: str | None) -> list[str]:
            raise OSError("agents dir unreadable")

        monkeypatch.setattr(scs, "_agent_names", _broken)
        _, body = _call(env, "GET")
        assert body["code_channel_agents"] is None


class TestAgentChoices:
    """The real filter behind ``_agent_names``: the chat picker's choices, minus
    Kiro Crew's generated specs and the background-only heartbeat agent."""

    def _row(self, name: str, *, owned: bool = True, desc: str = "") -> Any:
        from kiro_crew.agent_discovery import AgentInfo

        return AgentInfo(
            name=name, description=desc, model="", filename=f"{name}.json", kirocrew_owned=owned
        )

    def test_shipped_agents_are_offered_and_internal_ones_are_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import agent_discovery
        from kiro_crew.dashboard.handlers import agent_catalog

        rows = [
            self._row("kirocrew"),
            self._row("kirocrew-conductor"),
            self._row("kirocrew-team-lead"),
            self._row("kirocrew-research"),
            self._row("kirocrew-heartbeat"),
            self._row("kirocrew-lite"),
            self._row("kirocrew-guest"),
            self._row("kirocrew-skill-view-0123456789abcdef01234567"),
            self._row("my-agent", owned=False),
            # A user's own file with the heartbeat's name is an ordinary choice.
            self._row("kirocrew-heartbeat-copy", owned=False),
        ]
        monkeypatch.setattr(agent_discovery, "list_agents", lambda project_dir=None: rows)
        monkeypatch.setattr(agent_catalog.agent_state, "all_fork_info", lambda: {})
        monkeypatch.setattr(agent_catalog, "_name_would_be_masked", lambda name: False)
        got = scs._choosable_agent_names(None)
        assert "kirocrew-conductor" in got and "kirocrew-team-lead" in got
        assert {"kirocrew", "kirocrew-research", "my-agent", "kirocrew-heartbeat-copy"} <= set(got)
        hidden = {"kirocrew-heartbeat", "kirocrew-lite", "kirocrew-guest"}
        assert not hidden & set(got)
        assert not [n for n in got if n.startswith("kirocrew-skill-view-")]


# ── Manifest drift probe, against recorded Slack payloads ──


class _Resp(dict):
    def __init__(self, data: dict, headers: dict | None = None) -> None:
        super().__init__(data)
        self.headers = headers or {}


class _SlackErr(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.response = {"ok": False, "error": error}


class _FakeClient:
    def __init__(
        self,
        scopes: str | None,
        *,
        auth_error: str = "",
        list_views_error: str = "not_agent_channel",
    ) -> None:
        self.scopes = scopes
        self.auth_error = auth_error
        self.list_views_error = list_views_error
        self.calls: list[str] = []

    async def auth_test(self) -> _Resp:
        self.calls.append("auth.test")
        if self.auth_error:
            raise _SlackErr(self.auth_error)
        headers = {} if self.scopes is None else {"X-OAuth-Scopes": self.scopes}
        return _Resp({"ok": True, "team_id": "T123", "bot_id": "B123"}, headers)

    async def bots_info(self, bot: str) -> _Resp:
        self.calls.append("bots.info")
        return _Resp({"ok": True, "bot": {"id": bot, "app_id": "A0123ABCD"}})

    async def api_call(self, method: str, **kw: Any) -> _Resp:
        self.calls.append(method)
        if self.list_views_error:
            raise _SlackErr(self.list_views_error)
        return _Resp({"ok": True, "views": []})


def _probe(client: _FakeClient, features: frozenset[str]) -> scope_probe.ManifestStatus:
    req = slack_manifest.requirements(features)
    every = slack_manifest.requirements(frozenset(slack_manifest.FEATURES)).bot_scopes
    return asyncio.run(
        scope_probe.probe_manifest_status(
            client,
            req.bot_scopes,
            known_scopes=every,
            probe_code_channels=slack_manifest.CODE_CHANNELS in features,
        )
    )


def _scopes(features: frozenset[str]) -> str:
    return ",".join(sorted(slack_manifest.requirements(features).bot_scopes))


_CC = frozenset({slack_manifest.CODE_CHANNELS})


@pytest.fixture(autouse=True)
def _clean_probe_state() -> Any:
    from kiro_crew.slack import features

    features.reset_capabilities()
    scs.reset_status_cache()
    yield
    features.reset_capabilities()
    scs.reset_status_cache()


class TestDrift:
    @pytest.mark.parametrize("features", slack_manifest.feature_sets())
    def test_current_install_is_up_to_date(self, features: frozenset[str]) -> None:
        got = _probe(_FakeClient(_scopes(features)), features)
        assert got.status == scope_probe.STATUS_UP_TO_DATE
        assert got.missing_scopes == () and got.app_id == "A0123ABCD"

    def test_old_install_after_enabling_code_channels_needs_update(self) -> None:
        got = _probe(_FakeClient(_scopes(frozenset())), _CC)
        assert got.status == scope_probe.STATUS_NEEDS_UPDATE
        assert got.missing_scopes == (
            "canvases:write",
            "channels:write.invites",
            "chat:write.customize",
            "code_channels:manage",
            "groups:write.invites",
        )

    def test_switch_turned_off_reports_extra_scopes_only(self) -> None:
        got = _probe(_FakeClient(_scopes(_CC) + ",emoji:read"), frozenset())
        assert got.status == scope_probe.STATUS_UP_TO_DATE
        # emoji:read is the user's own addition, not drift.
        assert got.extra_scopes == (
            "canvases:write",
            "channels:write.invites",
            "chat:write.customize",
            "code_channels:manage",
            "groups:write.invites",
        )

    def test_unreadable_scopes(self) -> None:
        got = _probe(_FakeClient(None), _CC)
        assert (got.status, got.reason) == (scope_probe.STATUS_UNKNOWN, "scopes_unreadable")

    def test_network_failure_is_unknown(self) -> None:
        class _Down(_FakeClient):
            async def auth_test(self) -> _Resp:
                raise ConnectionError("offline")

        got = _probe(_Down(""), _CC)
        assert (got.status, got.reason) == (scope_probe.STATUS_UNKNOWN, "ConnectionError")

    def test_rejected_token(self) -> None:
        got = _probe(_FakeClient("", auth_error="invalid_auth"), _CC)
        assert got.status == scope_probe.STATUS_TOKEN_REJECTED

    def test_workspace_not_enabled_for_code_channels(self) -> None:
        client = _FakeClient(_scopes(_CC), list_views_error="feature_disabled")
        got = _probe(client, _CC)
        assert got.status == scope_probe.STATUS_FEATURE_DISABLED
        assert got.disabled_features == ("code_channels",)
        assert "agents.conversations.listViews" in client.calls

    def test_feature_probe_skipped_without_the_scope(self) -> None:
        client = _FakeClient(_scopes(frozenset()), list_views_error="feature_disabled")
        got = _probe(client, _CC)
        assert got.status == scope_probe.STATUS_NEEDS_UPDATE
        assert "agents.conversations.listViews" not in client.calls

    def test_runtime_refusal_is_reported_and_retired_by_a_passing_probe(self) -> None:
        features.latch_unavailable("code_channels", "feature_disabled")
        # The runtime's refusal shows on the page even before the page probes.
        got = _probe(_FakeClient(_scopes(frozenset())), frozenset())
        assert got.status == scope_probe.STATUS_FEATURE_DISABLED
        assert got.disabled_features == ("code_channels",)
        # A probe that now gets past the gate retires the stale record.
        got = _probe(_FakeClient(_scopes(_CC)), _CC)
        assert got.status == scope_probe.STATUS_UP_TO_DATE
        assert features.latched() == {}

    @pytest.mark.parametrize(
        "failure", ["ratelimited", "internal_error", "invalid_auth", "unknown_method", "connection"]
    )
    def test_inconclusive_recheck_keeps_a_confirmed_refusal(self, failure: str) -> None:
        class _Down(_FakeClient):
            async def api_call(self, method: str, **kw: Any) -> _Resp:
                self.calls.append(method)
                raise ConnectionError("network down")

        features.latch_unavailable("code_channels", "feature_disabled")
        client = (
            _Down(_scopes(_CC))
            if failure == "connection"
            else _FakeClient(_scopes(_CC), list_views_error=failure)
        )
        got = _probe(client, _CC)
        assert "agents.conversations.listViews" in client.calls
        assert got.status == scope_probe.STATUS_FEATURE_DISABLED
        assert got.disabled_features == ("code_channels",)
        assert features.latched() == {"code_channels": "feature_disabled"}

    def test_status_endpoint_without_token(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        body = asyncio.run(scs.manifest_status(loader.KiroCrewConfig(), ""))
        assert body["install"]["status"] == scope_probe.STATUS_NOT_CONFIGURED
        assert body["features"] == [] and body["app_manifest_url"] is None
        assert set(body["feature_changes"]) == set(slack_manifest.FEATURES)

    def test_status_is_cached_and_refresh_bypasses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clients: list[_FakeClient] = []

        def _make(**kw: Any) -> _FakeClient:
            c = _FakeClient(_scopes(frozenset()))
            clients.append(c)
            return c

        import slack_sdk.web.async_client as sdk

        monkeypatch.setattr(sdk, "AsyncWebClient", _make)
        cfg = loader.KiroCrewConfig()
        first = asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        assert len(clients) == 1
        asyncio.run(scs.manifest_status(cfg, "xoxb-test", refresh=True))
        assert len(clients) == 2
        assert first["install"]["status"] == scope_probe.STATUS_UP_TO_DATE
        assert first["app_manifest_url"] == "https://api.slack.com/apps/A0123ABCD/app-manifest"
        assert "xoxb-test" not in json.dumps(first)
