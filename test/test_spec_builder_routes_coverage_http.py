"""Spec Builder's HTTP handlers up to create: request plumbing, repo info, settings, list.

The request plumbing first (the auth gate, the JSON-object body reader, the
enabled-app gate and the client-claim reader); then the repo-info and browse
handlers, the recent projects, settings, the spec list, and spec creation with and
without a worktree.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest
import spec_builder_routes_coverage_helpers
from aiohttp import web
from spec_builder_routes_coverage_helpers import (
    CRED_NAME,
    _body,
    _entry,
    _mk,
    _no_rehydrate,
    _Slot,
    _spec_tree,
    _State,
    _write_index,
)

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes as r

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_no_autonudge = spec_builder_routes_coverage_helpers._no_autonudge
_no_dispatch = spec_builder_routes_coverage_helpers._no_dispatch
_pin_state_dir = spec_builder_routes_coverage_helpers._pin_state_dir
_quiet_sel = spec_builder_routes_coverage_helpers._quiet_sel


# -- HTTP: request plumbing ---------------------------------------------------


class TestRequireAuth:
    def test_a_request_the_auth_middleware_never_touched_is_401(self):
        denied = r._require_auth(_mk("GET", "specs", authed=False))
        assert denied is not None and denied.status == 401
        assert _body(denied)["code"] == "unauthorized"

    def test_an_authenticated_request_passes(self):
        assert r._require_auth(_mk("GET", "specs")) is None

    def test_an_app_token_interactive_denial_is_audited(self, _quiet_sel):
        request = _mk("POST", "specs/demo/message")
        request["app"] = "trusted-app"

        denied = r._require_interactive_user(request)

        assert denied is not None and denied.status == 403
        assert _body(denied)["code"] == "interactive_user_required"
        assert _quiet_sel.log_api_access.call_args.kwargs == {
            "caller": r.APP_NAME,
            "operation": "spec_interactive_user_denied",
            "outcome": "denied",
            "resources": "",
        }


class TestReadJson:
    @pytest.mark.asyncio
    async def test_a_payload_that_does_not_decode_is_400(self):
        out = await r._read_json(_mk("POST", "specs", body=None))
        assert isinstance(out, web.Response) and out.status == 400
        assert _body(out)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_a_json_value_that_is_not_an_object_is_400(self):
        out = await r._read_json(_mk("POST", "specs", body=[1, 2]))
        assert isinstance(out, web.Response) and out.status == 400
        assert _body(out)["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_an_object_is_returned_as_a_dict(self):
        assert await r._read_json(_mk("POST", "specs", body={"a": 1})) == {"a": 1}


class TestRequireEnabled:
    @pytest.mark.asyncio
    async def test_a_disabled_app_is_denied_even_though_the_route_is_registered(self):
        # Routes are wired once at gateway startup, so a default-disabled app
        # would otherwise stay callable.
        inner = mock.AsyncMock(return_value=web.json_response({"ok": True}))
        wrapped = r._require_enabled(inner)
        recovery = mock.AsyncMock()
        with (
            mock.patch.object(r, "is_app_enabled", return_value=False),
            mock.patch.object(r, "_ensure_duplicate_recovery", recovery),
        ):
            out = await wrapped(_mk("GET", "specs"))
        assert out.status == 403 and _body(out)["code"] == "app_disabled"
        inner.assert_not_awaited()
        recovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_enabled_app_reaches_the_handler(self):
        inner = mock.AsyncMock(return_value=web.json_response({"ok": True}))
        wrapped = r._require_enabled(inner)
        request = _mk("GET", "specs")
        recovery = mock.AsyncMock()
        with (
            mock.patch.object(r, "is_app_enabled", return_value=True),
            mock.patch.object(r, "_ensure_duplicate_recovery", recovery),
        ):
            out = await wrapped(request)
        assert _body(out) == {"ok": True}
        recovery.assert_awaited_once_with(request.app)

    def test_the_wrapper_keeps_the_handlers_identity(self):
        assert r._require_enabled(r._handle_list).__name__ == "_handle_list"


class TestClientClaim:
    @pytest.mark.asyncio
    async def test_a_delete_carries_its_claim_in_the_query_string(self):
        req = _mk("DELETE", "specs/demo", query="spec_dir=/s&slot_key=spec-builder-demo-0123abcd")
        assert await r._client_claim(req) == r._ClientClaim("/s", "spec-builder-demo-0123abcd")

    @pytest.mark.asyncio
    async def test_a_post_can_carry_it_in_the_body(self):
        req = _mk(
            "POST",
            "specs/demo/stop",
            body={"spec_dir": " /s ", "slot_key": " spec-builder-demo-0123abcd "},
        )
        assert await r._client_claim(req) == r._ClientClaim("/s", "spec-builder-demo-0123abcd")

    @pytest.mark.asyncio
    async def test_a_request_with_neither_is_unpinned_rather_than_refused(self):
        # An older tab predates these fields, so "" must keep working.
        assert await r._client_claim(_mk("POST", "specs/demo/stop")) == r._ClientClaim("", "")

    @pytest.mark.asyncio
    async def test_a_body_that_does_not_decode_leaves_the_claim_empty(self):
        assert await r._client_claim(_mk("POST", "specs/demo/stop", body=None)) == r._ClientClaim(
            "", ""
        )

    @pytest.mark.asyncio
    async def test_a_non_object_body_leaves_the_claim_empty(self):
        assert await r._client_claim(
            _mk("POST", "specs/demo/stop", body=["nope"])
        ) == r._ClientClaim("", "")

    @pytest.mark.asyncio
    async def test_the_query_string_is_not_overridden_by_the_body(self):
        req = _mk(
            "POST",
            "specs/demo/stop",
            query="spec_dir=/from-query&slot_key=spec-builder-demo-0123abcd",
            body={"spec_dir": "/from-body", "slot_key": "spec-builder-demo-ffffffff"},
        )
        assert (await r._client_claim(req)).spec_dir == "/from-query"


# -- HTTP: repo info, browse, settings ---------------------------------------


class TestHandleRepoInfo:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_repo_info(_mk("GET", "repo-info", authed=False))
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_no_path_answers_not_a_repo_without_running_git(self):
        git = mock.AsyncMock()
        with mock.patch.object(r, "_repo_info", git):
            out = await r._handle_repo_info(_mk("GET", "repo-info"))
        assert _body(out) == {"is_git": False}
        git.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_path_that_fails_the_chokepoint_answers_not_a_repo(self, tmp_path):
        # The hand-rolled is_absolute()/is_dir() pair skipped the sensitive-path
        # denial _safe_dir applies, and statted on the event loop.
        out = await r._handle_repo_info(_mk("GET", "repo-info", query=f"path={tmp_path}/missing"))
        assert _body(out) == {"is_git": False}

    @pytest.mark.asyncio
    async def test_a_real_directory_is_probed(self, tmp_path):
        with mock.patch.object(
            r, "_repo_info", mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)})
        ):
            out = await r._handle_repo_info(_mk("GET", "repo-info", query=f"path={tmp_path}"))
        assert _body(out)["is_git"] is True


class TestHandleBrowse:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        out = await r._handle_browse(_mk("GET", "browse", authed=False))
        assert out.status == 401

    @pytest.mark.asyncio
    async def test_a_denied_path_is_403_and_audited(self, tmp_path, _quiet_sel):
        out = await r._handle_browse(_mk("GET", "browse", query=f"path={tmp_path}/gone"))
        assert out.status == 403 and _body(out)["code"] == "access_denied"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_browse_denied" in ops

    @pytest.mark.asyncio
    async def test_a_named_path_lists_its_subdirectories_and_its_parent(self, tmp_path):
        (tmp_path / "src").mkdir()
        with mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={"is_git": True})):
            out = await r._handle_browse(_mk("GET", "browse", query=f"path={tmp_path}"))
        payload = _body(out)
        assert payload["path"] == str(Path(os.path.realpath(tmp_path)))
        assert payload["parent"] == os.path.dirname(os.path.realpath(tmp_path))
        assert [d["name"] for d in payload["dirs"]] == ["src"]
        assert payload["is_git"] is True
        # recents are only attached to the INITIAL empty-path call.
        assert "recents" not in payload

    @pytest.mark.asyncio
    async def test_the_initial_call_defaults_to_home_and_attaches_recents(self, tmp_path):
        with (
            mock.patch.object(Path, "home", return_value=tmp_path),
            mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={})),
            mock.patch.object(r, "_read_recent_projects", return_value=["/p1"]),
        ):
            out = await r._handle_browse(_mk("GET", "browse"))
        payload = _body(out)
        assert payload["recents"] == ["/p1"]
        assert payload["is_git"] is False


class TestReadRecentProjects:
    def test_a_missing_file_is_an_empty_list(self):
        assert r._read_recent_projects() == []

    def test_only_existing_directories_survive_and_the_list_is_bounded(self, tmp_path):
        from kiro_crew.config.paths import config_dir

        real = [str(tmp_path / f"p{i}") for i in range(12)]
        for path in real:
            Path(path).mkdir()
        config_dir().mkdir(parents=True, exist_ok=True)
        (config_dir() / "recent_projects.json").write_text(
            json.dumps([*real, str(tmp_path / "gone"), 7, None])
        )
        assert r._read_recent_projects() == real[:10]

    def test_a_malformed_or_wrongly_shaped_file_is_an_empty_list(self):
        from kiro_crew.config.paths import config_dir

        config_dir().mkdir(parents=True, exist_ok=True)
        (config_dir() / "recent_projects.json").write_text('{"not": "a list"}')
        assert r._read_recent_projects() == []


class TestHandleSettings:
    @pytest.mark.asyncio
    async def test_unauthenticated_on_both_verbs(self):
        assert (await r._handle_get_settings(_mk("GET", "settings", authed=False))).status == 401
        assert (await r._handle_put_settings(_mk("PUT", "settings", authed=False))).status == 401

    @pytest.mark.asyncio
    async def test_the_default_is_an_empty_base_path(self):
        assert _body(await r._handle_get_settings(_mk("GET", "settings"))) == {
            "base_path": "",
            "model": "",
        }

    @pytest.mark.asyncio
    async def test_a_stored_base_path_is_redacted_on_its_way_out(self):
        # settings.json is agent-writable, so a credential parked in base_path
        # would otherwise be rendered verbatim in the dashboard.
        r._save_settings({"base_path": f"/srv/{CRED_NAME}"})
        assert (
            CRED_NAME
            not in _body(await r._handle_get_settings(_mk("GET", "settings")))["base_path"]
        )

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_400(self):
        out = await r._handle_put_settings(_mk("PUT", "settings", body=None))
        assert out.status == 400

    @pytest.mark.asyncio
    async def test_a_relative_base_path_is_refused(self):
        out = await r._handle_put_settings(_mk("PUT", "settings", body={"base_path": "specs"}))
        assert out.status == 400 and _body(out)["code"] == "base_path_not_absolute"

    @pytest.mark.asyncio
    async def test_a_base_path_that_fails_the_chokepoint_is_refused(self, tmp_path):
        # Without this, spec storage could be repointed at a credential directory
        # and every subsequent spec would write into it.
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            out = await r._handle_put_settings(
                _mk("PUT", "settings", body={"base_path": str(tmp_path)})
            )
        assert out.status == 400 and _body(out)["code"] == "base_path_not_a_directory"

    @pytest.mark.asyncio
    async def test_a_usable_base_path_is_stored_fully_resolved(self, tmp_path):
        out = await r._handle_put_settings(
            _mk("PUT", "settings", body={"base_path": f" {tmp_path} "})
        )
        assert _body(out) == {
            "ok": True,
            "base_path": str(Path(os.path.realpath(tmp_path))),
            "model": "",
        }
        assert r._load_settings()["base_path"] == str(Path(os.path.realpath(tmp_path)))

    @pytest.mark.asyncio
    async def test_an_empty_base_path_clears_the_override(self, tmp_path):
        r._save_settings({"base_path": str(tmp_path)})
        out = await r._handle_put_settings(_mk("PUT", "settings", body={"base_path": ""}))
        assert _body(out) == {"ok": True, "base_path": "", "model": ""}
        assert r._load_settings()["base_path"] == ""


# -- HTTP: list ---------------------------------------------------------------


class TestHandleList:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        assert (await r._handle_list(_mk("GET", "specs", authed=False))).status == 401

    @pytest.mark.asyncio
    async def test_an_empty_index_still_reports_the_default_location(self):
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert payload == {"specs": [], "default_base": ".kiro/specs"}

    @pytest.mark.asyncio
    async def test_every_string_from_the_index_is_redacted(self, tmp_path):
        _write_index(
            {
                "demo": _entry(
                    tmp_path / f"specs/{CRED_NAME}",
                    working_dir=str(tmp_path),
                    spec_type=CRED_NAME,
                )
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert CRED_NAME not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_a_delete_in_flight_is_not_a_spec_the_user_still_has(self, tmp_path):
        _write_index(
            {
                "doomed": _entry(
                    # flake-ok: live-clock stamp on a reservation this process owns; ownership, not age, decides
                    tmp_path / "doomed",
                    deleting={"owner": r._PROCESS_ID, "at": time.time()},
                ),
                "kept": _entry(tmp_path / "kept"),
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert [s["name"] for s in payload["specs"]] == ["kept"]

    @pytest.mark.asyncio
    async def test_the_status_is_reconciled_rather_than_echoed(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo", status="executing")})
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        # A capped nudge loop that ran out of cycles leaves "executing" forever.
        assert payload["specs"][0]["status"] == "planning"

    @pytest.mark.asyncio
    async def test_the_live_running_flag_comes_from_the_slot(self, tmp_path):
        state = _State(
            **{"spec-builder-demo-0123abcd": _Slot("spec-builder-demo-0123abcd", running=True)}
        )
        _write_index({"demo": _entry(tmp_path / "demo")})
        payload = _body(await r._handle_list(_mk("GET", "specs", state=state)))
        assert payload["specs"][0]["running"] is True

    @pytest.mark.asyncio
    async def test_a_gateway_with_no_state_still_lists(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        payload = _body(await r._handle_list(_mk("GET", "specs")))
        assert payload["specs"][0]["running"] is False

    @pytest.mark.asyncio
    async def test_the_derived_phase_is_reported(self, tmp_path):
        spec = tmp_path / "demo"
        spec.mkdir()
        (spec / "requirements.md").write_text("x")
        _write_index({"demo": _entry(spec)})
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert payload["specs"][0]["phase"] == "requirements"

    @pytest.mark.asyncio
    async def test_agent_written_timestamps_cannot_break_the_sort(self, tmp_path):
        # Mixing a str and a float in one sort key raises TypeError, which turned a
        # single malformed entry into a 500 on EVERY list request.
        _write_index(
            {
                "newest": _entry(tmp_path / "a", updated_at=900.0),
                "broken": _entry(tmp_path / "b", updated_at="soon", created_at="also-soon"),
                "middle": _entry(tmp_path / "c", updated_at=500.0),
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert [s["name"] for s in payload["specs"]] == ["newest", "middle", "broken"]
        assert payload["specs"][-1]["updated_at"] == 0.0

    @pytest.mark.asyncio
    async def test_an_entry_with_no_updated_at_orders_by_created_at(self, tmp_path):
        _write_index(
            {
                "older": _entry(tmp_path / "a", updated_at=0, created_at=100.0),
                "newer": _entry(tmp_path / "b", updated_at=0, created_at=800.0),
            }
        )
        payload = _body(await r._handle_list(_mk("GET", "specs", state=_State())))
        assert [s["name"] for s in payload["specs"]] == ["newer", "older"]


# -- HTTP: create -------------------------------------------------------------


class TestHandleCreate:
    @pytest.mark.asyncio
    async def test_unauthenticated(self):
        assert (await r._handle_create(_mk("POST", "specs", authed=False))).status == 401

    @pytest.mark.asyncio
    async def test_a_malformed_body_is_400(self):
        assert (await r._handle_create(_mk("POST", "specs", body=None))).status == 400

    @pytest.mark.asyncio
    async def test_a_name_that_fails_the_grammar_is_400(self):
        out = await r._handle_create(_mk("POST", "specs", body={"name": "has space"}))
        assert out.status == 400 and _body(out)["code"] == "invalid_name"

    @pytest.mark.asyncio
    async def test_a_credential_shaped_name_is_400_not_a_spec_the_loader_will_drop(self):
        out = await r._handle_create(_mk("POST", "specs", body={"name": CRED_NAME}))
        assert out.status == 400 and _body(out)["code"] == "invalid_name"

    @pytest.mark.asyncio
    async def test_an_unknown_spec_type_is_400(self, tmp_path):
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "spec_type": "epic"})
        )
        assert out.status == 400 and _body(out)["code"] == "invalid_spec_type"

    @pytest.mark.asyncio
    async def test_a_relative_working_dir_is_400(self):
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": "project"})
        )
        assert out.status == 400 and _body(out)["code"] == "working_dir_not_absolute"

    @pytest.mark.asyncio
    async def test_a_missing_or_sensitive_working_dir_gets_one_indistinguishable_400(
        self, tmp_path
    ):
        # One response for "missing", "not a directory" and "sensitive" so the
        # endpoint cannot probe the filesystem.
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path / "gone")})
        )
        assert out.status == 400 and _body(out)["code"] == "working_dir_not_a_directory"
        with mock.patch.object(r, "is_sensitive_path", return_value=True):
            out = await r._handle_create(
                _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
            )
        assert _body(out)["code"] == "working_dir_not_a_directory"

    @pytest.mark.asyncio
    async def test_a_duplicate_name_is_409(self, tmp_path):
        _write_index({"demo": _entry(tmp_path / "demo")})
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
        )
        assert out.status == 409 and _body(out)["code"] == "spec_exists"

    @pytest.mark.asyncio
    async def test_a_successful_create_returns_201_and_seeds_the_agent(
        self, tmp_path, _no_dispatch, _quiet_sel
    ):
        state = _State()
        with _no_rehydrate():
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=state,
                    body={
                        "name": "demo",
                        "working_dir": str(tmp_path),
                        "spec_type": "quick",
                        "description": "add a picker",
                    },
                )
            )
        payload = _body(out)
        assert out.status == 201
        assert payload["name"] == "demo" and payload["spec_type"] == "quick"
        assert payload["status"] == "planning" and payload["worktree_branch"] == ""
        spec_dir = Path(payload["spec_dir"])
        assert spec_dir.is_dir()
        # A fresh per-creation key, so a reused name never appends to the previous
        # spec's transcript.
        stored = r._load_index()["demo"]
        assert r._owns_slot_key("demo", stored["slot_key"])
        assert stored["slot_key"] != "spec-builder-demo"
        seed = _no_dispatch.call_args.args[2]
        assert "add a picker" in seed and "QUICK spec" in seed
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_create" in ops

    @pytest.mark.asyncio
    async def test_creating_clears_an_earlier_tombstone_for_the_same_directory(
        self, tmp_path, _no_dispatch
    ):
        spec_dir = (tmp_path / ".kiro" / "specs" / "demo").resolve()
        r._remember_deleted(str(spec_dir))
        with _no_rehydrate():
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 201
        # Creating is an explicit decision that outranks an earlier delete.
        assert r._load_deleted() == []

    @pytest.mark.asyncio
    async def test_existing_kiro_documents_are_409_with_the_opt_in_named(self, tmp_path):
        _spec_tree(tmp_path, "demo", "requirements.md")
        out = await r._handle_create(
            _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
        )
        assert out.status == 409
        payload = _body(out)
        assert payload["code"] == "spec_files_exist"
        assert "import_existing" in payload["error"]

    @pytest.mark.asyncio
    async def test_a_resolved_path_outside_its_root_is_400_and_audited(self, tmp_path, _quiet_sel):
        with mock.patch.object(
            r, "_prepare_spec_dir", return_value=(tmp_path / "elsewhere", "escape")
        ):
            out = await r._handle_create(
                _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
            )
        assert out.status == 400 and _body(out)["code"] == "spec_path_outside_root"
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_path_escape_denied" in ops

    @pytest.mark.asyncio
    async def test_a_directory_that_cannot_be_created_is_400(self, tmp_path):
        with mock.patch.object(
            r, "_prepare_spec_dir", return_value=(tmp_path / "x", "mkdir:read-only fs")
        ):
            out = await r._handle_create(
                _mk("POST", "specs", body={"name": "demo", "working_dir": str(tmp_path)})
            )
        assert out.status == 400 and _body(out)["code"] == "spec_dir_creation_failed"
        assert "read-only fs" in _body(out)["error"]

    @pytest.mark.asyncio
    async def test_a_concurrent_create_that_wins_the_index_makes_this_one_409(self, tmp_path):
        # The duplicate check at the top is stale by the time the awaits are done,
        # so the insert is the arbitration and the loser touches no slot state.
        with mock.patch.object(r, "_mutate_index", mock.AsyncMock(return_value=False)):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "spec_exists"

    @pytest.mark.asyncio
    async def test_a_slot_owned_by_another_app_unwinds_the_insert(self, tmp_path):
        state = _State(**{"spec-builder-demo": _Slot("spec-builder-demo", app="issue-radar")})
        with (
            _no_rehydrate(),
            mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=None)),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=state,
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "slot_owned_by_another_app"
        assert r._load_index() == {}

    @pytest.mark.asyncio
    async def test_a_spec_replaced_during_slot_setup_is_409_and_unwound(
        self, tmp_path, _no_dispatch, _quiet_sel
    ):
        state = _State()

        async def _steal(*_a, **_kw):
            # A concurrent delete + re-import lands in the slot-setup window; the
            # seed prompt must not drive the replacement's agent.
            index = r._load_index()
            index["demo"]["slot_key"] = "spec-builder-demo-ffffffff"
            r._save_index(index)
            return _Slot("spec-builder-demo")

        with mock.patch.object(r, "_ensure_worker_slot", _steal):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=state,
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409 and _body(out)["code"] == "spec_changed_during_create"
        _no_dispatch.assert_not_called()
        ops = [c.kwargs["operation"] for c in _quiet_sel.log_api_access.call_args_list]
        assert "spec_create_aborted" in ops

    @pytest.mark.asyncio
    async def test_the_unwind_leaves_a_replacements_entry_alone(self, tmp_path, _no_dispatch):
        """The pop keys off the NAME, so an unpinned unwind would delete a
        same-name spec created while we were validating."""
        replacement = _entry(tmp_path / "other", slot_key="spec-builder-demo-ffffffff")

        async def _replace(*_a, **_kw):
            r._save_index({"demo": dict(replacement)})
            return None

        with mock.patch.object(r, "_ensure_worker_slot", _replace):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 409
        assert r._load_index()["demo"]["slot_key"] == "spec-builder-demo-ffffffff"

    @pytest.mark.asyncio
    async def test_a_title_that_cannot_be_set_does_not_fail_the_create(
        self, tmp_path, _no_dispatch
    ):
        class _NoTitle(_Slot):
            def __setattr__(self, name, value):
                if name == "title" and getattr(self, "_ready", False):
                    raise RuntimeError("frozen")
                super().__setattr__(name, value)

        slot = _NoTitle("spec-builder-demo")
        slot._ready = True
        with mock.patch.object(r, "_ensure_worker_slot", mock.AsyncMock(return_value=slot)):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path)},
                )
            )
        assert out.status == 201


class TestHandleCreateWithWorktree:
    @pytest.mark.asyncio
    async def test_a_non_repository_is_400(self, tmp_path):
        with mock.patch.object(r, "_repo_info", mock.AsyncMock(return_value={"is_git": False})):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={
                        "name": "demo",
                        "working_dir": str(tmp_path),
                        "use_worktree": True,
                    },
                )
            )
        assert out.status == 400 and _body(out)["code"] == "worktree_requires_git"

    @pytest.mark.asyncio
    async def test_a_failed_creation_is_400(self, tmp_path):
        with (
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r, "_create_worktree", mock.AsyncMock(return_value="path already exists")
            ),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        assert out.status == 400 and _body(out)["code"] == "worktree_creation_failed"

    @pytest.mark.asyncio
    async def test_a_worktree_that_is_not_a_usable_directory_is_rolled_back(self, tmp_path):
        remove = mock.AsyncMock()
        with (
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r,
                "_create_worktree",
                mock.AsyncMock(return_value=(str(tmp_path / "wt-gone"), "spec/demo")),
            ),
            mock.patch.object(r, "_remove_worktree", remove),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        assert out.status == 400 and _body(out)["code"] == "worktree_unusable"
        remove.assert_awaited_once_with(str(tmp_path), str(tmp_path / "wt-gone"), "spec/demo")

    @pytest.mark.asyncio
    async def test_the_worktree_becomes_the_containment_root_for_the_spec_files(
        self, tmp_path, _no_dispatch
    ):
        # Without re-validating the worktree, containment is still measured against
        # the ORIGINAL checkout and every worktree-mode create fails.
        worktree = tmp_path / "repo-wt-demo"
        worktree.mkdir()
        with (
            _no_rehydrate(),
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r, "_create_worktree", mock.AsyncMock(return_value=(str(worktree), "spec/demo"))
            ),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    state=_State(),
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        payload = _body(out)
        assert out.status == 201
        assert payload["worktree_branch"] == "spec/demo"
        assert payload["working_dir"] == str(Path(os.path.realpath(worktree)))
        assert payload["spec_dir"].startswith(str(Path(os.path.realpath(worktree))))

    @pytest.mark.asyncio
    async def test_a_refusal_after_the_worktree_exists_removes_it(self, tmp_path):
        worktree = tmp_path / "repo-wt-demo"
        worktree.mkdir()
        remove = mock.AsyncMock()
        with (
            mock.patch.object(
                r,
                "_repo_info",
                mock.AsyncMock(return_value={"is_git": True, "root": str(tmp_path)}),
            ),
            mock.patch.object(
                r, "_create_worktree", mock.AsyncMock(return_value=(str(worktree), "spec/demo"))
            ),
            mock.patch.object(r, "_remove_worktree", remove),
            mock.patch.object(r, "_prepare_spec_dir", return_value=(worktree, "escape")),
        ):
            out = await r._handle_create(
                _mk(
                    "POST",
                    "specs",
                    body={"name": "demo", "working_dir": str(tmp_path), "use_worktree": True},
                )
            )
        assert out.status == 400
        remove.assert_awaited_once()
