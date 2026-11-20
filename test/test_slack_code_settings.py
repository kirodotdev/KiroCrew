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
import re
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

_AS = frozenset({slack_manifest.AGENT_SESSIONS})

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
        assert body["agent_sessions"] is False
        assert body["code_channel_defaults"] == {
            "folder": None,
            "agent": None,
            "model": None,
            "reasoning_effort": None,
            "approval_mode": None,
            "conductor_mode": False,
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
        _call(env, "PUT", {"not_a_switch": True, "code_channels": True})
        stored = _stored(env)
        assert stored["code_channels"] is True
        assert "not_a_switch" not in stored

    @pytest.mark.parametrize("on", [True, False])
    def test_the_agent_sessions_switch_saves_and_reads_back(self, env: dict, on: bool) -> None:
        status, res = _call(env, "PUT", {"agent_sessions": on})
        assert status == 200
        assert res["restart_required"] is False
        _, body = _call(env, "GET")
        assert body["agent_sessions"] is on

    @pytest.mark.parametrize("on", [True, False])
    def test_the_slack_mcp_switch_saves_and_reads_back(self, env: dict, on: bool) -> None:
        status, res = _call(env, "PUT", {"slack_mcp": on})
        assert status == 200
        assert res["restart_required"] is False
        if on:  # off is the default, so an off save writes nothing
            assert _stored(env)["slack_mcp"] is True
        _, body = _call(env, "GET")
        assert body["slack_mcp"] is on
        # Nothing runs for it, so there is no capability to report.
        assert "slack_mcp_capability" not in body

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


class TestDispatchSubToggle:
    """``slack.code_channel_dispatch``: the sub-toggle under Code channels."""

    def test_newly_turning_code_channels_on_turns_helpers_on(self, env: dict) -> None:
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch"] is False
        assert body["code_channel_dispatch_on_enable"] is True
        status, res = _call(env, "PUT", {"code_channels": True})
        assert status == 200
        assert _stored(env)["code_channel_dispatch"] is True
        ok = [c for c in env["sel"].calls if c["outcome"] == "ok"]
        assert "code_channel_dispatch" in ok[-1]["resources"].split(",")
        _, body = _call(env, "GET")
        assert body["code_channels"] is True and body["code_channel_dispatch"] is True
        assert body["code_channel_dispatch_on_enable"] is False

    @pytest.mark.parametrize("landed", [False, True])
    def test_a_value_stored_after_the_snapshot_is_kept_on_enable(
        self, env: dict, monkeypatch: pytest.MonkeyPatch, landed: bool
    ) -> None:
        """A ``kirocrew config set`` that lands between the save's read and its
        locked write is kept: helpers are re-decided on the document written."""
        real = loader.update_config_locked
        raced: list[bool] = []

        def _cli_set_first(path: Path, *a: Any, **kw: Any) -> Any:
            if not raced:
                raced.append(True)
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                data.setdefault("slack", {})["code_channel_dispatch"] = landed
                path.write_text(json.dumps(data), encoding="utf-8")
            return real(path, *a, **kw)

        monkeypatch.setattr(loader, "update_config_locked", _cli_set_first)
        assert _call(env, "PUT", {"code_channels": True})[0] == 200
        assert raced
        assert _stored(env)["code_channels"] is True
        assert _stored(env)["code_channel_dispatch"] is landed
        ok = [c for c in env["sel"].calls if c["outcome"] == "ok"]
        assert "code_channel_dispatch" not in ok[-1]["resources"].split(",")

    def test_a_failed_env_write_undoes_helpers_turned_on_by_the_enable(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _accept(key: str, token: str) -> None:
            return None

        async def _env_fails(updates: dict) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(mod, "_validate_slack_token", _accept)
        monkeypatch.setattr(mod, "_write_env_off_loop", _env_fails)

        async def _put() -> int:
            app = web.Application()
            app["state"] = env["state"]
            app.router.add_put("/api/slack/config", mod.api_slack_config_save)
            async with TestClient(TestServer(app)) as client:
                resp = await client.put(
                    "/api/slack/config", json={"code_channels": True, "bot_token": "xoxb-test"}
                )
                return resp.status

        assert asyncio.run(_put()) >= 400
        stored = json.loads(env["cfg"].read_text(encoding="utf-8")) if env["cfg"].exists() else {}
        assert "code_channel_dispatch" not in stored.get("slack", {})
        assert stored.get("slack", {}).get("code_channels") is not True

    def test_an_existing_install_keeps_its_stored_value(self, env: dict) -> None:
        """Code channels already on with nothing stored: an upgrade flips nothing."""
        env["cfg"].write_text(json.dumps({"slack": {"code_channels": True}}), encoding="utf-8")
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch"] is False
        assert body["code_channel_dispatch_on_enable"] is False
        assert _call(env, "PUT", {"code_channels": True, "show_thinking": True})[0] == 200
        assert "code_channel_dispatch" not in _stored(env)
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch"] is False

    def test_an_explicit_off_is_kept_when_code_channels_are_turned_on(self, env: dict) -> None:
        env["cfg"].write_text(
            json.dumps({"slack": {"code_channels": False, "code_channel_dispatch": False}}),
            encoding="utf-8",
        )
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch_on_enable"] is False
        assert _call(env, "PUT", {"code_channels": True})[0] == 200
        assert _stored(env)["code_channel_dispatch"] is False
        _, body = _call(env, "GET")
        assert body["code_channels"] is True and body["code_channel_dispatch"] is False

    def test_an_off_saved_with_the_enabling_save_is_stored_as_explicit(self, env: dict) -> None:
        """Turning helpers off in the same save is recorded, so a later enable keeps it."""
        status, _ = _call(env, "PUT", {"code_channels": True, "code_channel_dispatch": False})
        assert status == 200
        assert _stored(env)["code_channel_dispatch"] is False
        _call(env, "PUT", {"code_channels": False})
        _call(env, "PUT", {"code_channels": True})
        assert _stored(env)["code_channel_dispatch"] is False

    @pytest.mark.parametrize(
        "text", ["{ torn", "[]", '{"slack": {"code_channel_dispatch": "yes"}}']
    )
    def test_an_unreadable_or_malformed_config_reads_off(self, env: dict, text: str) -> None:
        env["cfg"].write_text(text, encoding="utf-8")
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch"] is False
        assert body["code_channel_dispatch_on_enable"] is False

    def test_a_non_object_slack_section_is_a_first_enable_on_card_and_save(self, env: dict) -> None:
        """The save rebuilds a non-object section as ``{}``; the card says the same."""
        env["cfg"].write_text('{"slack": null}', encoding="utf-8")
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch"] is False
        assert body["code_channel_dispatch_on_enable"] is True
        assert _call(env, "PUT", {"code_channels": True})[0] == 200
        assert _stored(env)["code_channel_dispatch"] is True

    def test_an_overlay_that_cannot_be_read_leaves_helpers_off_on_enable(self, env: dict) -> None:
        """config.local.json may hold the value that wins: enabling Code channels
        still saves, and does not write a base value it cannot vouch for."""
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("{ torn", encoding="utf-8")
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_dispatch_on_enable"] is False
            assert _call(env, "PUT", {"code_channels": True})[0] == 200
            assert _stored(env)["code_channels"] is True
            assert "code_channel_dispatch" not in _stored(env)
        finally:
            overlay.unlink()

    @pytest.mark.parametrize("on", [True, False])
    def test_saves_hot_and_reads_back(self, env: dict, on: bool) -> None:
        _call(env, "PUT", {"code_channel_dispatch": not on})
        status, res = _call(env, "PUT", {"code_channel_dispatch": on})
        assert status == 200 and res["restart_required"] is False
        assert _stored(env)["code_channel_dispatch"] is on
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch"] is on

    def test_turning_code_channels_off_leaves_it_as_saved(self, env: dict) -> None:
        _call(env, "PUT", {"code_channels": True, "code_channel_dispatch": True})
        _call(env, "PUT", {"code_channels": False})
        assert _stored(env)["code_channel_dispatch"] is True

    @pytest.mark.parametrize("bad", ["true", 1, None])
    def test_must_be_a_boolean(self, env: dict, bad: object) -> None:
        status, res = _call(env, "PUT", {"code_channel_dispatch": bad})
        assert status == 400 and "boolean" in res["error"]
        assert not env["cfg"].exists()

    def test_an_overlay_pin_refuses_the_save_instead_of_acknowledging_it(self, env: dict) -> None:
        """config.local.json wins over the base file the save writes, so a withdrawal
        saved there would read 'saved' while dispatch stayed granted (security review)."""
        # The isolated test home's own overlay path, unpatched: the loader and
        # ``overlay_pins`` both resolve it there.
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"slack": {"code_channel_dispatch": True}}), encoding="utf-8")
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_dispatch"] is True
            assert body["code_channel_dispatch_overlay"] is True
            assert body["code_channel_dispatch_overlay_state"] == "pinned"
            assert body["code_channel_dispatch_overlay_path"].endswith("config.local.json")
            status, res = _call(env, "PUT", {"code_channel_dispatch": False})
            assert status == 409 and "config.local.json" in res["error"]
            assert res["error"].startswith("Helper agents are set in config.local.json (slack.")
            base = json.loads(env["cfg"].read_text(encoding="utf-8")) if env["cfg"].exists() else {}
            assert "code_channel_dispatch" not in (base.get("slack") or {}), "nothing staged"
            # Other Slack Code fields still save.
            assert _call(env, "PUT", {"code_channels": True})[0] == 200
            # The overlay wins, so enabling writes no base value under it.
            assert "code_channel_dispatch" not in _stored(env)
            overlay.write_text("{ torn", encoding="utf-8")
            _, body = _call(env, "GET")
            assert body["code_channel_dispatch_overlay_state"] == "unreadable"
        finally:
            overlay.unlink(missing_ok=True)
        _, body = _call(env, "GET")
        assert body["code_channel_dispatch_overlay"] is False
        assert body["code_channel_dispatch_overlay_state"] == "absent"

    @pytest.mark.parametrize("text", ["{ torn", "[1]", '{"slack": "on"}', '{"slack": null}'])
    def test_an_unreadable_overlay_refuses_the_save(self, env: dict, text: str) -> None:
        """An overlay that cannot be read may hold the winning value; the save is not
        taken as uncontested (verifier: overlay_pins fails open)."""
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(text, encoding="utf-8")
        try:
            status, res = _call(env, "PUT", {"code_channel_dispatch": False})
        finally:
            overlay.unlink(missing_ok=True)
        assert status == 409 and "cannot be read" in res["error"]

    @pytest.mark.parametrize("on_disk", ['{"slack": {}}', None], ids=["hand-edited", "deleted"])
    def test_a_sealed_overlay_pin_still_refuses_while_an_edit_is_pending(
        self, env: dict, monkeypatch: pytest.MonkeyPatch, on_disk: str | None
    ) -> None:
        """Where the sandbox seals config.local.json, the loader keeps serving the
        endorsed copy over a pending hand-edit or delete; the overlay check reads that
        copy too, so a withdrawal is refused rather than acknowledged while the load
        keeps hand-off on (Opus review)."""
        from kiro_crew.config import loosening_provenance as lp

        monkeypatch.setattr(lp, "enforcement_active", lambda: True)
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        lp.record(
            lp.LEAF_CONFIG_LOCAL,
            (json.dumps({"slack": {"code_channel_dispatch": True}}) + "\n").encode(),
            strict=True,
        )
        if on_disk is None:
            overlay.unlink(missing_ok=True)
        else:
            overlay.write_text(on_disk, encoding="utf-8")
        loader._invalidate_config_cache()
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_dispatch"] is True
            assert body["code_channel_dispatch_overlay_state"] == "pinned"
            status, res = _call(env, "PUT", {"code_channel_dispatch": False})
            assert status == 409 and res["error"].startswith(
                "Helper agents are set in config.local.json"
            )
        finally:
            overlay.unlink(missing_ok=True)
            loader._invalidate_config_cache()


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
        # What runs instead, as the runtime resolves it: the gone pick is skipped.
        eff = body["code_channel_effective"]
        assert eff["agent"] != "reviewer" and eff["agent_source"] != "default"
        assert asyncio.run(scs.default_agent_available("reviewer", None)) is False
        assert asyncio.run(scs.default_agent_available("builder", None)) is True


class TestAutoFolder:
    def test_unset_folder_reports_the_auto_folder_and_no_folder_agent(self, env: dict) -> None:
        _, body = _call(env, "GET")
        assert body["code_channel_auto_folder"] == {
            "name": "Code channels",
            "legacy_agent": "kirocrew-conductor",
            "id": None,
            "ambiguous": False,
        }
        eff = body["code_channel_effective"]
        assert (eff["folder_path"], eff["folder_auto_create"]) == ("Code channels", True)
        # No folder agent and no agent.default_agent: the built-in default runs.
        assert (eff["agent"], eff["agent_source"]) == (None, "")
        assert eff["folder"] is None
        assert body["code_channel_agents"] == ["builder", "reviewer"]

    def test_the_legacy_conductor_on_the_auto_folder_is_not_reported_as_its_agent(
        self, env: dict
    ) -> None:
        env["state"]._folders = [
            *_FOLDERS,
            {
                "id": "eeeeeeeeeeee",
                "name": "Code channels",
                "parent_id": "",
                "default_agent": "kirocrew-conductor",
            },
        ]
        _, body = _call(env, "GET")
        eff = body["code_channel_effective"]
        assert eff["folder"]["id"] == "eeeeeeeeeeee"
        assert eff["agent_source"] != "folder" and eff["agent"] != "kirocrew-conductor"

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


def _with_slack(env: dict, slack: dict) -> None:
    env["cfg"].write_text(json.dumps({"slack": slack}), encoding="utf-8")


class TestConductorMode:
    """``code_channel_defaults.conductor_mode``: "Always use conductor mode"."""

    @pytest.fixture(autouse=True)
    def _keep_remembered_agent(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        # The GET reads ``crew_mode.crew_mode_agent(cfg)``, which remembers it.
        from kiro_crew import crew_mode

        _floor_monkeypatch.setattr(crew_mode, "_configured", crew_mode._configured)

    def test_on_saves_true_and_off_clears_it(self, env: dict) -> None:
        status, res = _call(env, "PUT", {"code_channel_defaults": {"conductor_mode": True}})
        assert status == 200, res
        assert _stored(env)["code_channel_defaults"]["conductor_mode"] is True
        status, res = _call(env, "PUT", {"code_channel_defaults": {"conductor_mode": False}})
        assert status == 200, res
        assert _stored(env)["code_channel_defaults"].get("conductor_mode") is None
        _, body = _call(env, "GET")
        assert body["code_channel_defaults"]["conductor_mode"] is False

    @pytest.mark.parametrize("raw", ["true", 1, [True], {}])
    def test_anything_but_a_boolean_is_refused(self, env: dict, raw: object) -> None:
        status, res = _call(env, "PUT", {"code_channel_defaults": {"conductor_mode": raw}})
        assert status == 400 and "conductor_mode must be a boolean" in res.get("error", "")

    def test_on_wins_over_the_default_and_the_folder_agent(self, env: dict) -> None:
        _with_slack(
            env,
            {
                "code_channel_defaults": {
                    "folder": "bbbbbbbbbbbb",
                    "agent": "reviewer",
                    "conductor_mode": True,
                }
            },
        )
        _, body = _call(env, "GET")
        eff = body["code_channel_effective"]
        assert (eff["agent"], eff["agent_source"]) == ("kirocrew-conductor", "conductor")
        assert body["code_channel_conductor_agent"] == "kirocrew-conductor"
        # The stored default agent is kept for when the switch goes off.
        assert body["code_channel_defaults"]["agent"] == "reviewer"

    def test_a_folder_change_does_not_judge_the_dormant_default_agent(self, env: dict) -> None:
        # "project-only" exists only in the Dev project: picking Twin (no project)
        # would refuse it, but while conductor mode decides the agent it is dormant.
        stored = {"folder": "bbbbbbbbbbbb", "agent": "project-only", "conductor_mode": True}
        _with_slack(env, {"code_channel_defaults": stored})
        status, res = _call(env, "PUT", {"code_channel_defaults": {"folder": "cccccccccccc"}})
        assert status == 200, res
        saved = _stored(env)["code_channel_defaults"]
        assert (saved["folder"], saved["agent"]) == ("cccccccccccc", "project-only")

    def test_an_explicit_agent_edit_is_still_checked_while_it_is_on(self, env: dict) -> None:
        _with_slack(env, {"code_channel_defaults": {"conductor_mode": True}})
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"agent": "no-such-agent"}})
        assert status == 400

    def test_with_it_off_a_folder_change_still_judges_the_default_agent(self, env: dict) -> None:
        stored = {"folder": "bbbbbbbbbbbb", "agent": "project-only"}
        _with_slack(env, {"code_channel_defaults": stored})
        status, _ = _call(env, "PUT", {"code_channel_defaults": {"folder": "cccccccccccc"}})
        assert status == 400

    def test_a_folder_only_save_judges_the_stored_agent_as_main_does(self, env: dict) -> None:
        # The overlay-merged judgment is for a save that turns the switch off. A
        # plain folder change with the switch off judges the stored agent against
        # the new folder, as before, even while config.local.json sets an agent.
        stored = {"folder": "bbbbbbbbbbbb", "agent": "project-only"}
        _with_slack(env, {"code_channel_defaults": stored})
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(
            json.dumps({"slack": {"code_channel_defaults": {"agent": "reviewer"}}}),
            encoding="utf-8",
        )
        try:
            body = {"code_channel_defaults": {"folder": "cccccccccccc"}}
            status, res = _call(env, "PUT", body)
            assert status == 400 and "'project-only' not found" in res["error"]
        finally:
            overlay.unlink(missing_ok=True)

    def test_turning_it_off_with_a_folder_change_judges_the_default_agent(self, env: dict) -> None:
        stored = {"folder": "bbbbbbbbbbbb", "agent": "project-only", "conductor_mode": True}
        _with_slack(env, {"code_channel_defaults": stored})
        body = {"code_channel_defaults": {"folder": "cccccccccccc", "conductor_mode": False}}
        assert _call(env, "PUT", body)[0] == 400

    def test_turning_it_off_alone_judges_a_missing_stored_agent(self, env: dict) -> None:
        # The stored agent runs again once the switch is off: a gone one is refused,
        # and picking another (or clearing it) in the same save is accepted.
        _with_slack(env, {"code_channel_defaults": {"agent": "ghost", "conductor_mode": True}})
        off = {"conductor_mode": False}
        status, res = _call(env, "PUT", {"code_channel_defaults": off})
        assert status == 400 and "'ghost' not found" in res["error"]
        assert _stored(env)["code_channel_defaults"]["conductor_mode"] is True
        both = {"conductor_mode": False, "agent": "reviewer"}
        assert _call(env, "PUT", {"code_channel_defaults": both})[0] == 200
        # Turning it on never judges the agent it overrides.
        _with_slack(env, {"code_channel_defaults": {"agent": "ghost"}})
        on = {"conductor_mode": True}
        assert _call(env, "PUT", {"code_channel_defaults": on})[0] == 200
        # Resending the switch unchanged (off, as stored) turns nothing off: the
        # stored agent is not judged.
        _with_slack(env, {"code_channel_defaults": {"agent": "ghost"}})
        assert _call(env, "PUT", {"code_channel_defaults": {"conductor_mode": None}})[0] == 200

    def test_turning_it_off_judges_the_agent_the_overlay_leaves_to_it(self, env: dict) -> None:
        # Once the switch is off the agent that runs is config.json's with the overlay
        # merged over it. An agent the overlay sets is not this page's to change, so
        # it never blocks the save; the base one is judged only where it still runs.
        _with_slack(env, {"code_channel_defaults": {"agent": "ghost", "conductor_mode": True}})
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        off = {"code_channel_defaults": {"conductor_mode": False}}
        try:
            overlay.write_text(
                json.dumps({"slack": {"code_channel_defaults": {"agent": "reviewer"}}}),
                encoding="utf-8",
            )
            assert _call(env, "PUT", off)[0] == 200
            # Not even a missing one: the switch can always be turned off from here
            # (the runtime skips a missing agent and the GET warns about it).
            _with_slack(
                env, {"code_channel_defaults": {"agent": "reviewer", "conductor_mode": True}}
            )
            overlay.write_text(
                json.dumps({"slack": {"code_channel_defaults": {"agent": "ghost"}}}),
                encoding="utf-8",
            )
            assert _call(env, "PUT", off)[0] == 200
            # An empty overlay section merges: the base agent is still what runs.
            _with_slack(env, {"code_channel_defaults": {"agent": "ghost", "conductor_mode": True}})
            overlay.write_text(
                json.dumps({"slack": {"code_channel_defaults": {}}}), encoding="utf-8"
            )
            assert _call(env, "PUT", off)[0] == 400
        finally:
            overlay.unlink(missing_ok=True)

    def test_a_missing_dormant_default_agent_is_still_reported(self, env: dict) -> None:
        # Reported while the switch is on too, so the card can show it the moment
        # its draft turns the switch off, without a second save.
        _with_slack(env, {"code_channel_defaults": {"agent": "ghost", "conductor_mode": True}})
        _, body = _call(env, "GET")
        assert "agent_missing" in body["code_channel_warnings"]
        assert body["code_channel_effective"]["agent_source"] == "conductor"

    def test_a_non_object_defaults_overlay_pins_the_switch_off(self, env: dict) -> None:
        # The merge replaces code_channel_defaults whole with a non-object, and the
        # reader takes it as all-unset: the overlay pins the switch off.
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"slack": {"code_channel_defaults": None}}), encoding="utf-8")
        _with_slack(env, {"code_channel_defaults": {"conductor_mode": True}})
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_conductor_mode_overlay_state"] == "pinned"
            assert body["code_channel_defaults"]["conductor_mode"] is False
            put = {"code_channel_defaults": {"conductor_mode": True}}
            status, res = _call(env, "PUT", put)
            assert status == 409 and "is set in config.local.json" in res["error"]
        finally:
            overlay.unlink(missing_ok=True)

    def test_an_overlay_pin_refuses_a_change_instead_of_acknowledging_it(self, env: dict) -> None:
        # config.local.json wins over the base file a save writes: a withdrawal
        # saved there would read "saved" while code channels kept the conductor.
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        pinned = {"slack": {"code_channel_defaults": {"conductor_mode": True}}}
        overlay.write_text(json.dumps(pinned), encoding="utf-8")
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_defaults"]["conductor_mode"] is True
            assert body["code_channel_conductor_mode_overlay_state"] == "pinned"
            body_put = {"code_channel_defaults": {"conductor_mode": None}}
            status, res = _call(env, "PUT", body_put)
            assert status == 409 and "config.local.json" in res["error"]
            base = json.loads(env["cfg"].read_text(encoding="utf-8")) if env["cfg"].exists() else {}
            assert "conductor_mode" not in (
                (base.get("slack") or {}).get("code_channel_defaults") or {}
            )
            # Other defaults still save.
            ok = _call(env, "PUT", {"code_channel_defaults": {"reasoning_effort": "high"}})
            assert ok[0] == 200
            overlay.write_text("{ torn", encoding="utf-8")
            _, body = _call(env, "GET")
            assert body["code_channel_conductor_mode_overlay_state"] == "unreadable"
            assert _call(env, "PUT", body_put)[0] == 409
        finally:
            overlay.unlink(missing_ok=True)
        _, body = _call(env, "GET")
        assert body["code_channel_conductor_mode_overlay_state"] == "absent"

    def test_an_overlay_pinned_on_keeps_the_dormant_agent_out_of_a_folder_change(
        self, env: dict
    ) -> None:
        # The base file has no switch; config.local.json turns it on, so the stored
        # project-only agent is dormant and a folder change saves.
        _with_slack(
            env, {"code_channel_defaults": {"folder": "bbbbbbbbbbbb", "agent": "project-only"}}
        )
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        pinned = {"slack": {"code_channel_defaults": {"conductor_mode": True}}}
        overlay.write_text(json.dumps(pinned), encoding="utf-8")
        try:
            put = {"code_channel_defaults": {"folder": "cccccccccccc"}}
            status, res = _call(env, "PUT", put)
            assert status == 200, res
            # An unreadable overlay: whether it turns the switch off is unknown, so
            # it counts as off and the agent is judged.
            overlay.write_text("{ torn", encoding="utf-8")
            back = {"code_channel_defaults": {"folder": "bbbbbbbbbbbb"}}
            assert _call(env, "PUT", back)[0] == 200
            assert _call(env, "PUT", put)[0] == 400
        finally:
            overlay.unlink(missing_ok=True)

    @pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="needs symlinks")
    def test_a_dangling_overlay_symlink_reads_as_no_overlay(
        self, env: dict, tmp_path: Path
    ) -> None:
        # The name exists but its target is gone: the loader reads that as no
        # config.local.json, so every reader of the switch does too. The switch is
        # not locked, the stored value is in force, and a change saves.
        _with_slack(env, {"code_channel_defaults": {"conductor_mode": True}})
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        try:
            overlay.symlink_to(tmp_path / "gone.json")
        except OSError:
            pytest.skip("this host cannot create symlinks")
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_conductor_mode_overlay_state"] == "absent"
            assert body["code_channel_effective"]["agent_source"] == "conductor"
            assert body["code_channel_dispatch_overlay_state"] == "absent"
            off = {"code_channel_defaults": {"conductor_mode": False}}
            assert _call(env, "PUT", off)[0] == 200
            assert _call(env, "PUT", {"code_channel_dispatch": True})[0] == 200
        finally:
            overlay.unlink(missing_ok=True)

    def test_an_unreadable_overlay_reads_the_switch_as_off(self, env: dict) -> None:
        # config.json turns it on and config.local.json is torn: the GET and the save
        # read the switch as off (as the bind does), so the stored agent is live.
        stored = {"folder": "bbbbbbbbbbbb", "agent": "project-only", "conductor_mode": True}
        _with_slack(env, {"code_channel_defaults": stored})
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("{ torn", encoding="utf-8")
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_conductor_mode_overlay_state"] == "unreadable"
            assert body["code_channel_effective"]["agent_source"] != "conductor"
            put = {"code_channel_defaults": {"folder": "cccccccccccc"}}
            assert _call(env, "PUT", put)[0] == 400
        finally:
            overlay.unlink(missing_ok=True)

    def test_an_overlay_that_sets_only_dispatch_does_not_pin_it(self, env: dict) -> None:
        overlay = loader.config_local_path()
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"slack": {"code_channel_dispatch": True}}), encoding="utf-8")
        try:
            _, body = _call(env, "GET")
            assert body["code_channel_conductor_mode_overlay_state"] == "absent"
            put = {"code_channel_defaults": {"conductor_mode": True}}
            assert _call(env, "PUT", put)[0] == 200
        finally:
            overlay.unlink(missing_ok=True)

    def test_where_conductor_mode_is_not_offered_the_usual_order_applies(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew import edition

        monkeypatch.setattr(edition, "is_lts", lambda: True)
        # The lock logs its first use process-wide; restore that record.
        from kiro_crew import edition_locks

        monkeypatch.setattr(edition_locks, "_logged", set())
        _with_slack(env, {"code_channel_defaults": {"agent": "reviewer", "conductor_mode": True}})
        _, body = _call(env, "GET")
        eff = body["code_channel_effective"]
        assert (eff["agent"], eff["agent_source"]) == ("reviewer", "default")

    def test_on_lts_a_stored_on_never_makes_the_default_agent_dormant(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # LTS has no conductor mode, so a stored on is inert: a folder-only change
        # to a project that lacks the stored agent is refused, as on main.
        from kiro_crew import edition, edition_locks

        monkeypatch.setattr(edition, "is_lts", lambda: True)
        monkeypatch.setattr(edition_locks, "_logged", set())
        stored = {"folder": "bbbbbbbbbbbb", "agent": "project-only", "conductor_mode": True}
        _with_slack(env, {"code_channel_defaults": stored})
        status, res = _call(env, "PUT", {"code_channel_defaults": {"folder": "cccccccccccc"}})
        assert status == 400 and "'project-only' not found" in res["error"]


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
            "pins:write",
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
            "pins:write",
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

    def test_check_again_lifts_a_stale_workspace_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An admin enabled the feature after the runtime latched it: Check again
        # (refresh) must re-probe like a reconnect, not keep the old refusal.
        import slack_sdk.web.async_client as sdk

        monkeypatch.setattr(sdk, "AsyncWebClient", lambda **kw: _FakeClient(_scopes(_AS)))
        cfg = loader.KiroCrewConfig()
        cfg.slack.agent_sessions = True
        features.latch_unavailable("agent_sessions", "feature_disabled")
        first = asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        assert first["install"]["status"] == scope_probe.STATUS_FEATURE_DISABLED
        epoch = features.latch_epoch()
        again = asyncio.run(scs.manifest_status(cfg, "xoxb-test", refresh=True))
        assert again["install"]["status"] == scope_probe.STATUS_UP_TO_DATE
        assert features.latched() == {}
        assert features.available("agent_sessions")
        # A latch kept privately (code-channel status writes) is lifted too.
        assert features.latch_epoch() > epoch

    def test_a_cached_read_keeps_the_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import slack_sdk.web.async_client as sdk

        monkeypatch.setattr(sdk, "AsyncWebClient", lambda **kw: _FakeClient(_scopes(_AS)))
        cfg = loader.KiroCrewConfig()
        cfg.slack.agent_sessions = True
        features.latch_unavailable("agent_sessions", "feature_disabled")
        got = asyncio.run(scs.manifest_status(cfg, "xoxb-test"))
        assert got["install"]["status"] == scope_probe.STATUS_FEATURE_DISABLED
        assert features.latched() == {"agent_sessions": "feature_disabled"}

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

    @pytest.mark.parametrize("on", [frozenset(), _CC], ids=["no-switch", "code-channels"])
    @pytest.mark.parametrize("holds", [False, True], ids=["without-it", "with-it"])
    def test_an_optional_scope_is_neither_missing_nor_a_leftover(
        self, monkeypatch: pytest.MonkeyPatch, on: frozenset[str], holds: bool
    ) -> None:
        # Only Share Slack access's add-by-email needs users:read.email, and its
        # refusal says so. An install from before the manifest asked for it is not
        # out of date, and one that holds it is never told it may remove it.
        import slack_sdk.web.async_client as sdk

        granted = slack_manifest.requirements(on).bot_scopes - slack_manifest.OPTIONAL_BOT_SCOPES
        assert "users:read.email" not in granted
        if holds:
            granted = granted | {"users:read.email"}
        monkeypatch.setattr(
            sdk, "AsyncWebClient", lambda **kw: _FakeClient(",".join(sorted(granted)))
        )
        cfg = loader.KiroCrewConfig()
        cfg.slack.code_channels = bool(on)
        got = asyncio.run(scs.manifest_status(cfg, "xoxb-test", refresh=True))
        assert got["install"]["status"] == scope_probe.STATUS_UP_TO_DATE
        assert "users:read.email" not in got["required_scopes"]
        assert got["install"]["missing_scopes"] == [] and got["install"]["extra_scopes"] == []


class TestStatusReadKeepsARemovedToken:
    def test_the_status_read_does_not_put_back_a_token_the_watcher_removed(
        self, env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live watcher has applied a removal of the bot token (it is gone from
        the environment) while the status read still sees it in ``.env``. The read
        uses the token for its one Slack call, and leaves the removal standing."""
        import os

        loader.env_path().write_text("SLACK_BOT_TOKEN=xoxb-removed\n", encoding="utf-8")
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        seen: list[str] = []

        async def _status(cfg: Any, token: str, refresh: bool = False) -> dict:
            seen.append(token)
            return {"install": {"status": scope_probe.STATUS_UP_TO_DATE}}

        monkeypatch.setattr(scs, "manifest_status", _status)

        async def _run() -> int:
            app = web.Application()
            app.router.add_get("/api/slack/manifest/status", mod.api_slack_manifest_status)
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/slack/manifest/status")
                return resp.status

        assert asyncio.run(_run()) == 200
        assert seen == ["xoxb-removed"]
        assert "SLACK_BOT_TOKEN" not in os.environ


_LOCALES = Path(__file__).resolve().parents[1] / "website/src/i18n/locales"
_HAND_WRITTEN = sorted(
    p for p in _LOCALES.glob("*.json") if p.name not in {"en.json", "en-XA.json"}
)


@pytest.mark.parametrize("catalog", _HAND_WRITTEN, ids=lambda p: p.name)
def test_the_switch_description_counts_the_scopes_it_adds(catalog: Path) -> None:
    # The Code channels row tells the user how many permissions turning it on
    # adds. The number is written into each catalog, so it must follow the
    # manifest: a switch that gains or loses a scope fails here until every
    # locale says the new count.
    count = len(scs.feature_changes()["code_channels"]["added_scopes"])
    desc = json.loads(catalog.read_text(encoding="utf-8"))["pages"]["settings"]["slackPanel"][
        "slackCode"
    ]["code_channels_desc"]
    # Whole numbers, not digits: a count of 10 or more is one token, not two.
    assert re.findall(r"\d+", desc) == [
        str(count)
    ], f"{catalog.name}: {desc!r} should say {count} and no other number"


@pytest.mark.parametrize("catalog", _HAND_WRITTEN, ids=lambda p: p.name)
def test_the_slack_mcp_details_count_the_user_scopes_it_asks_for(catalog: Path) -> None:
    # Same rule as Code channels: the number in the copy follows the manifest.
    # It is the user scopes the manifest lists with the switch on (they replace
    # the older list), and it lives in the row's "What this adds" disclosure;
    # the description itself stays in plain words and states no number. The
    # second number is how many of them are new to an app without the switch,
    # the same count the update panel's Slack tools box titles itself with.
    on = slack_manifest.requirements(frozenset({slack_manifest.SLACK_MCP})).user_scopes
    off = slack_manifest.requirements(frozenset()).user_scopes
    count, new = len(on), len(on - off)
    copy = json.loads(catalog.read_text(encoding="utf-8"))["pages"]["settings"]["slackPanel"][
        "slackCode"
    ]
    details = copy["slack_mcp_details"]
    assert re.findall(r"\d+", details) == [
        str(count),
        str(new),
    ], f"{catalog.name}: {details!r} should say {count} ({new} new) and no other number"
    assert re.findall(r"\d+", copy["slack_mcp_desc"]) == [], catalog.name
