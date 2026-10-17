"""``aws.project_profiles``: an operator-owned project directory -> AWS profile map.

A session whose working directory lies in a mapped directory starts its agent
with ``AWS_PROFILE`` set to that profile. These tests pin what makes the binding
safe to ship as an opt-in:

* an empty map leaves every spawn environment exactly as the caller passed it;
* a profile the caller already chose (a cron job's ``env``, a workflow) wins;
* an invalid profile name or a relative key is ignored at load and refused at save;
* the deepest mapped directory wins, compared component by component, so a
  sibling sharing a name prefix (``proj`` / ``project2``) does not match;
* resolving the binding reads no file at all: no credential material, and
  nothing inside the project checkout;
* the scrub that strips AWS secrets from the agent environment still applies.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import logging
import os
import pathlib
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import aws_project_profiles as app
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.sections import AwsConfig
from kiro_crew.testing.wait import async_wait_until, await_until


def _project(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestResolveProjectProfile:
    def test_the_mapped_directory_itself_is_bound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        assert app.resolve_project_profile({str(proj): "dev"}, str(proj)) == "dev"

    def test_a_directory_under_the_mapped_one_is_bound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        nested = _project(proj, "src/pkg")
        assert app.resolve_project_profile({str(proj): "dev"}, str(nested)) == "dev"

    def test_an_unmapped_directory_is_unbound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")
        assert app.resolve_project_profile({str(proj): "dev"}, str(other)) is None

    def test_a_sibling_sharing_a_name_prefix_is_not_bound(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        sibling = _project(tmp_path, "project2")
        assert app.resolve_project_profile({str(proj): "dev"}, str(sibling)) is None

    def test_the_deepest_mapped_directory_wins(self, tmp_path: Path) -> None:
        outer = _project(tmp_path, "work")
        inner = _project(outer, "billing")
        cwd = _project(inner, "infra")
        mapping = {str(inner): "billing-prod", str(outer): "dev"}
        assert app.resolve_project_profile(mapping, str(cwd)) == "billing-prod"
        assert app.resolve_project_profile(mapping, str(outer)) == "dev"
        reordered = {str(outer): "dev", str(inner): "billing-prod"}
        assert app.resolve_project_profile(reordered, str(cwd)) == "billing-prod"

    def test_a_trailing_separator_on_the_key_still_matches(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        coerced = app.coerce_project_profiles({str(proj) + os.sep: "dev"})
        assert app.resolve_project_profile(coerced, str(proj / "src")) == "dev"

    def test_a_symlinked_cwd_resolves_to_its_target(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        link = tmp_path / "link-to-proj"
        try:
            link.symlink_to(proj, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")
        assert app.resolve_project_profile({str(proj): "dev"}, str(link)) == "dev"

    def test_a_symlinked_key_resolves_to_its_target(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        link = tmp_path / "key-link"
        try:
            link.symlink_to(proj, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")
        assert app.resolve_project_profile({str(link): "dev"}, str(proj / "src")) == "dev"

    def test_a_symlink_inside_a_bound_project_cannot_carry_another_dir_in(
        self, tmp_path: Path
    ) -> None:
        """The real path decides: a link under the project naming a dir outside is unbound."""
        proj = _project(tmp_path, "proj")
        outside = _project(tmp_path, "outside")
        link = proj / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available to this user")
        assert app.resolve_project_profile({str(proj): "dev"}, str(link)) is None

    def test_an_empty_map_or_cwd_is_unbound(self, tmp_path: Path) -> None:
        assert app.resolve_project_profile({}, str(tmp_path)) is None
        assert app.resolve_project_profile({str(tmp_path): "dev"}, "") is None


class TestBoundProfileEnv:
    def test_an_empty_map_returns_the_callers_env_object_itself(self, tmp_path: Path) -> None:
        caller = {"FOO": "bar"}
        assert app.bound_profile_env({}, str(tmp_path), caller) is caller
        assert app.bound_profile_env({}, str(tmp_path), None) is None

    def test_an_unbound_cwd_returns_the_callers_env_object_itself(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")
        caller = {"FOO": "bar"}
        assert app.bound_profile_env({str(proj): "dev"}, str(other), caller) is caller
        assert app.bound_profile_env({str(proj): "dev"}, str(other), None) is None
        assert app.bound_profile_env({str(proj): "dev"}, None, None) is None

    def test_a_bound_cwd_adds_the_profile_without_mutating_the_callers_env(
        self, tmp_path: Path
    ) -> None:
        proj = _project(tmp_path, "proj")
        caller = {"FOO": "bar"}
        result = app.bound_profile_env({str(proj): "dev"}, str(proj), caller)
        assert result == {"FOO": "bar", "AWS_PROFILE": "dev", "AWS_DEFAULT_PROFILE": "dev"}
        assert caller == {"FOO": "bar"}
        assert app.bound_profile_env({str(proj): "dev"}, str(proj), None) == {
            "AWS_PROFILE": "dev",
            "AWS_DEFAULT_PROFILE": "dev",
        }

    @pytest.mark.parametrize("name", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_a_selector_the_gateway_inherited_cannot_outrank_the_binding(
        self, tmp_path: Path, name: str
    ) -> None:
        """The launcher layers ``extra_env`` over the gateway's own environment."""
        proj = _project(tmp_path, "proj")
        overlay = app.bound_profile_env({str(proj): "dev"}, str(proj), None)
        child_env = {name: "prod", **(overlay or {})}
        assert child_env["AWS_PROFILE"] == "dev"
        assert child_env["AWS_DEFAULT_PROFILE"] == "dev"

    @pytest.mark.parametrize("name", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_a_profile_the_caller_chose_wins(self, tmp_path: Path, name: str) -> None:
        proj = _project(tmp_path, "proj")
        caller = {name: "from-cron"}
        assert app.bound_profile_env({str(proj): "dev"}, str(proj), caller) is caller


class TestNoFileIsRead:
    """Resolving the binding opens no file: no credential, nothing in the checkout."""

    def test_resolution_opens_no_file_even_with_project_settings_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _project(tmp_path, "home")
        aws_dir = _project(home, ".aws")
        (aws_dir / "credentials").write_text("[dev]\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        proj = _project(tmp_path, "proj")
        settings = _project(proj, ".kiro/settings")
        planted = {"aws": {"project_profiles": {str(proj): "repo-chosen"}}, "AWS_PROFILE": "x"}
        for name in ("settings.json", "aws.json", "cli.json"):
            (settings / name).write_text(json.dumps(planted), encoding="utf-8")
        (proj / ".kiro" / "settings.json").write_text(json.dumps(planted), encoding="utf-8")

        def refuse(*_a: object, **_k: object) -> None:
            raise AssertionError("resolving a project profile must not open a file")

        with (
            patch.object(builtins, "open", refuse),
            patch.object(io, "open", refuse),
            patch.object(os, "open", refuse),
            patch.object(pathlib.Path, "open", refuse),
            patch.object(pathlib.Path, "read_text", refuse),
            patch.object(pathlib.Path, "read_bytes", refuse),
        ):
            unbound = app.bound_profile_env({}, str(proj), None)
            elsewhere = app.bound_profile_env({str(home): "dev"}, str(proj), None)
            bound = app.bound_profile_env({str(proj): "dev"}, str(proj / "src"), None)

        assert unbound is None, "a repo file must never bind a profile"
        assert elsewhere is None
        assert bound == {
            "AWS_PROFILE": "dev",
            "AWS_DEFAULT_PROFILE": "dev",
        }, "only the operator's map binds"


class TestCoerce:
    def test_valid_entries_are_kept_and_keys_normalized(self, tmp_path: Path) -> None:
        proj = _project(tmp_path, "proj")
        raw = {str(proj) + os.sep: "dev", str(tmp_path / "b"): "sso.Admin+ro-1_x"}
        assert app.coerce_project_profiles(raw) == {
            str(proj): "dev",
            str(tmp_path / "b"): "sso.Admin+ro-1_x",
        }

    @pytest.mark.parametrize(
        "profile", ["", "-dash", "has space", "semi;colon", "a" * 129, "new\nline", 7]
    )
    def test_an_invalid_profile_name_is_dropped_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, profile: object
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.aws_project_profiles"):
            assert app.coerce_project_profiles({str(tmp_path): profile}) == {}
        assert any(str(tmp_path) in r.getMessage() for r in caplog.records)

    def test_a_relative_key_is_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="kiro_crew.aws_project_profiles"):
            assert app.coerce_project_profiles({"relative/dir": "dev", "": "dev"}) == {}
        assert len(caplog.records) == 2

    def test_a_non_mapping_is_ignored(self) -> None:
        assert app.coerce_project_profiles(None) == {}
        assert app.coerce_project_profiles(["/a"]) == {}
        assert app.coerce_project_profiles("dev") == {}

    def test_two_keys_for_one_directory_keep_the_first(self, tmp_path: Path) -> None:
        raw = {str(tmp_path): "first", str(tmp_path) + os.sep: "second"}
        assert app.coerce_project_profiles(raw) == {str(tmp_path): "first"}

    def test_refusals_never_carry_the_profile_value(self, tmp_path: Path) -> None:
        reasons = app.project_profile_refusals({str(tmp_path): "not a profile; AKIAEXAMPLE"})
        assert len(reasons) == 1
        assert str(tmp_path) in reasons[0]
        assert "AKIAEXAMPLE" not in reasons[0]


class TestConfigLoadAndSave:
    def test_the_default_map_is_empty(self) -> None:
        assert KiroCrewConfig().aws.project_profiles == {}
        assert KiroCrewConfig().to_dict()["aws"] == {"project_profiles": {}}

    def test_load_keeps_valid_entries_and_drops_invalid_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path, "proj")
        stored = tmp_path / "config.json"
        stored.write_text(
            json.dumps(
                {
                    "aws": {
                        "project_profiles": {
                            str(proj): "dev",
                            str(tmp_path / "bad"): "bad name",
                            "relative": "dev",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)
        assert KiroCrewConfig.load().aws.project_profiles == {str(proj): "dev"}

    def test_a_saved_binding_survives_a_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = _project(tmp_path, "proj")
        stored = tmp_path / "config.json"
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: stored)
        chosen = KiroCrewConfig(aws=AwsConfig(project_profiles={str(proj): "dev"}))
        stored.write_text(json.dumps(chosen.to_dict()), encoding="utf-8")
        assert KiroCrewConfig.load().aws.project_profiles == {str(proj): "dev"}

    def test_a_write_introducing_an_invalid_profile_is_refused(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused, update_config_locked

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"agent": {"yolo": True}}), encoding="utf-8")
        before = path.read_bytes()

        def plant(existing: dict) -> dict:
            existing["aws"] = {"project_profiles": {str(tmp_path): "bad name; AKIAEXAMPLE"}}
            return existing

        with pytest.raises(ConfigWriteRefused) as refused:
            update_config_locked(path, mutate=plant)
        assert str(tmp_path) in str(refused.value)
        assert "AKIAEXAMPLE" not in str(refused.value)
        assert path.read_bytes() == before

    def test_a_write_introducing_a_relative_key_is_refused(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import ConfigWriteRefused, update_config_locked

        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")

        def plant(existing: dict) -> dict:
            existing["aws"] = {"project_profiles": {"relative/dir": "dev"}}
            return existing

        with pytest.raises(ConfigWriteRefused):
            update_config_locked(path, mutate=plant)

    def test_a_valid_write_lands(self, tmp_path: Path) -> None:
        from kiro_crew.config.loader import update_config_locked

        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")

        def plant(existing: dict) -> dict:
            existing["aws"] = {"project_profiles": {str(tmp_path): "dev"}}
            return existing

        update_config_locked(path, mutate=plant)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["aws"]["project_profiles"] == {str(tmp_path): "dev"}

    def test_a_pre_existing_bad_entry_does_not_refuse_an_unrelated_write(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.config.loader import update_config_locked

        path = tmp_path / "config.json"
        stale = {"relative/dir": "dev"}
        path.write_text(json.dumps({"aws": {"project_profiles": stale}}), encoding="utf-8")

        def unrelated(existing: dict) -> dict:
            existing.setdefault("agent", {})["yolo"] = True
            return existing

        with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
            update_config_locked(path, mutate=unrelated)
        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["agent"]["yolo"] is True
        assert written["aws"]["project_profiles"] == stale
        assert any("aws.project_profiles" in r.getMessage() for r in caplog.records)


class TestScrubKeepsTheProfile:
    def test_aws_profile_survives_the_agent_scrub_and_secrets_do_not(self) -> None:
        from kiro_crew.sandbox import scrub_agent_subprocess_env

        env = {
            "PATH": "/usr/bin",
            "AWS_PROFILE": "dev",
            "AWS_SECRET_ACCESS_KEY": "s",
            "AWS_SESSION_TOKEN": "t",
        }
        scrubbed = scrub_agent_subprocess_env(env)
        assert scrubbed["AWS_PROFILE"] == "dev"
        assert "AWS_SECRET_ACCESS_KEY" not in scrubbed
        assert "AWS_SESSION_TOKEN" not in scrubbed


def _recording_factory(calls: list[dict]):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        calls.append(kwargs)
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.disown_work_dir = MagicMock()
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.spawn_identity = ""
        m._runtime = None
        m.cwd = kwargs.get("cwd") or ""
        return m

    return factory


def _cfg(mapping: dict[str, str], pool_size: int = 0) -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    c.session.pool_size = pool_size
    c.aws.project_profiles = dict(mapping)
    return c


class TestSessionSpawn:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller_env", [None, {"FOO": "bar"}])
    async def test_an_empty_map_passes_the_callers_env_through_unchanged(
        self, tmp_path: Path, caller_env: dict | None
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        settings = _project(proj, ".kiro/settings")
        (settings / "aws.json").write_text(
            json.dumps({"project_profiles": {str(proj): "repo-chosen"}}), encoding="utf-8"
        )
        calls: list[dict] = []
        mgr = SessionManager(_cfg({}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj), extra_env=caller_env)
        finally:
            await mgr.close_all()
        assert len(calls) == 1
        assert calls[0]["extra_env"] is caller_env

    @pytest.mark.asyncio
    async def test_an_unbound_cwd_passes_the_callers_env_through_unchanged(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        sibling = _project(tmp_path, "project2")
        calls: list[dict] = []
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(sibling), extra_env=None)
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] is None

    @pytest.mark.asyncio
    async def test_a_bound_cwd_starts_the_agent_with_the_profile(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        caller = {"FOO": "bar"}
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj / "src"), extra_env=caller)
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] == {
            "FOO": "bar",
            "AWS_PROFILE": "dev",
            "AWS_DEFAULT_PROFILE": "dev",
        }
        assert caller == {"FOO": "bar"}

    @pytest.mark.asyncio
    async def test_the_profile_reaches_only_the_env_and_is_logged_by_name(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        mgr = SessionManager(
            _cfg({str(proj): "zq-acct-7"}), provider_factory=_recording_factory(calls)
        )
        try:
            with caplog.at_level(logging.INFO):
                await mgr.get_or_create("chat-6332", cwd=str(proj))
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] == {
            "AWS_PROFILE": "zq-acct-7",
            "AWS_DEFAULT_PROFILE": "zq-acct-7",
        }
        elsewhere = [k for k, v in calls[0].items() if k != "extra_env" and "zq-acct-7" in repr(v)]
        assert elsewhere == [], "the profile name travels only as the AWS_PROFILE value"
        applied = [r.getMessage() for r in caplog.records if "zq-acct-7" in r.getMessage()]
        assert applied == ["session chat-6332: AWS profile 'zq-acct-7' from aws.project_profiles"]

    @pytest.mark.asyncio
    async def test_an_unbound_spawn_logs_no_profile(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")
        mgr = SessionManager(
            _cfg({str(proj): "zq-acct-7"}), provider_factory=_recording_factory([])
        )
        try:
            with caplog.at_level(logging.INFO):
                await mgr.get_or_create("chat-6332", cwd=str(other))
        finally:
            await mgr.close_all()
        assert not [r for r in caplog.records if "AWS profile" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_callers_explicit_profile_wins(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        caller = {"AWS_PROFILE": "from-cron"}
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj), extra_env=caller)
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] is caller

    @pytest.mark.asyncio
    async def test_a_successor_applies_the_binding_again(self, tmp_path: Path) -> None:
        """The session records the caller's env, not the bound one: rebuilt from the
        bound one, a successor would read the profile as the caller's own choice."""
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj), extra_env={"FOO": "bar"})
            key = mgr._fold_key("chat-6332")
            first = mgr._sessions[key]
            mgr.release(key)
            assert mgr.enqueue(key, "ts-1", "parked for the successor", force=True)
            assert await mgr.reset(key) is True
            await async_wait_until(
                lambda: (live := mgr._sessions.get(key)) is not None and live is not first,
                describe=lambda: f"no successor under the key: {list(mgr._sessions)}",
            )
            successor = mgr._sessions[key]
        finally:
            await mgr.close_all()
        assert calls[-1]["extra_env"] == {
            "FOO": "bar",
            "AWS_PROFILE": "dev",
            "AWS_DEFAULT_PROFILE": "dev",
        }
        assert successor.aws_project_profile == "dev"

    @pytest.mark.asyncio
    async def test_a_won_race_retry_applies_the_binding_again(self, tmp_path: Path) -> None:
        """A cold start that lost the race to a session gone stale retries with the
        caller's env, so the session it registers still records the binding."""
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        record = _recording_factory([])
        started: list[object] = []
        both_started = asyncio.Event()
        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            provider = record(session_key=session_key, agent=agent, channel_id=channel_id, **kwargs)

            async def _start() -> None:
                started.append(provider)
                if len(started) == 2:
                    both_started.set()
                await gate.wait()

            provider.start = _start
            return provider

        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=factory)
        key = mgr._fold_key("chat-6332")
        try:
            racers = [
                asyncio.ensure_future(
                    mgr.get_or_create(key, cwd=str(proj), extra_env={"FOO": "bar"})
                )
                for _ in range(2)
            ]
            await await_until(both_started, what="both cold starts parked in start()")
            gate.set()
            await async_wait_until(
                lambda: (live := mgr._sessions.get(key)) is not None and live.semaphore._waiters,
                describe=lambda: "the losing start is not waiting on the winner's lease",
            )
            winner = mgr._sessions[key]
            winner.provider.is_process_alive = lambda: False
            mgr.release(key)
            await await_until(asyncio.gather(*racers), what="the losing start's retry")
            retried = mgr._sessions[key]
            mgr.release(key)
        finally:
            await mgr.close_all()
        assert retried is not winner
        assert retried.aws_project_profile == "dev"
        assert retried.extra_env == {"FOO": "bar"}

    @pytest.mark.asyncio
    async def test_a_bound_session_at_the_pool_directory_skips_the_warm_pool(
        self, tmp_path: Path
    ) -> None:
        """A pooled process was spawned without the profile, so a bound session never takes one."""
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        mgr = SessionManager(
            _cfg({str(proj): "dev"}, pool_size=2), provider_factory=_recording_factory(calls)
        )
        mgr._pool_cwd = str(proj)
        mgr._drain_and_claim = AsyncMock(side_effect=AssertionError("pool must be bypassed"))
        mgr._schedule_replenish = MagicMock()
        decisions: list[str] = []
        mgr._record_pool_decision = lambda decision, _key: decisions.append(decision)
        try:
            await mgr.get_or_create("chat-6332", cwd=str(proj))
        finally:
            await mgr.close_all()
        mgr._drain_and_claim.assert_not_awaited()
        assert decisions == ["bypass_env"]
        assert calls[0]["extra_env"] == {"AWS_PROFILE": "dev", "AWS_DEFAULT_PROFILE": "dev"}


class TestTaskRunStep:
    @pytest.mark.asyncio
    async def test_a_bound_step_takes_the_dedicated_path(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager
        from kiro_crew.start_priority import StartPriority

        proj = _project(tmp_path, "proj")
        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory([]))
        dedicated = MagicMock()
        mgr.get_or_create = AsyncMock(return_value=(dedicated, True, False))
        mgr._get_or_bootstrap_run_runtime = AsyncMock(
            side_effect=AssertionError("a bound step must not share the run's runtime")
        )
        try:
            result = await mgr.open_task_session(
                "taskrunner:run6332:runtime",
                "taskrunner:run6332:task0",
                agent="kirocrew",
                cwd=str(proj),
            )
        finally:
            await mgr.close_all()
        assert result == (dedicated, True, False)
        mgr.get_or_create.assert_awaited_once_with(
            "taskrunner:run6332:task0",
            agent="kirocrew",
            approval_policy="",
            cwd=str(proj),
            start_priority=StartPriority.BACKGROUND,
        )

    @pytest.mark.asyncio
    async def test_an_unbound_step_keeps_the_shared_runtime(self, tmp_path: Path) -> None:
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        other = _project(tmp_path, "other")

        class _SharedPath(Exception):
            pass

        mgr = SessionManager(_cfg({str(proj): "dev"}), provider_factory=_recording_factory([]))
        mgr.get_or_create = AsyncMock(side_effect=AssertionError("must not divert"))
        mgr._get_or_bootstrap_run_runtime = AsyncMock(side_effect=_SharedPath())
        try:
            with pytest.raises(_SharedPath):
                await mgr.open_task_session(
                    "taskrunner:run6332b:runtime",
                    "taskrunner:run6332b:task0",
                    agent="kirocrew",
                    cwd=str(other),
                )
        finally:
            await mgr.close_all()


class TestHotReload:
    @pytest.mark.asyncio
    async def test_a_binding_change_reaches_the_next_spawn_without_a_restart(
        self, tmp_path: Path
    ) -> None:
        """The manager subscribes to ``aws``, so an edit alone is adopted into ``_cfg``."""
        from kiro_crew.config.live import ConfigChange
        from kiro_crew.session import SessionManager

        proj = _project(tmp_path, "proj")
        calls: list[dict] = []
        mgr = SessionManager(_cfg({}), provider_factory=_recording_factory(calls))
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        try:
            assert "aws" in mgr._config_sub.prefixes
            edited = _cfg({str(proj): "dev"})
            await mgr._on_config_change(
                ConfigChange(old=None, new=edited, changed=frozenset({"aws.project_profiles"}))
            )
            mgr.refresh_defaults.assert_not_awaited()
            await mgr.get_or_create("chat-6332", cwd=str(proj))
        finally:
            await mgr.close_all()
        assert calls[0]["extra_env"] == {"AWS_PROFILE": "dev", "AWS_DEFAULT_PROFILE": "dev"}


_PARENT_KEY = "dashboard:parent6332"
_CHILD_KEY = "subagent:sub6332"


async def _bound_parent(sm, cwd: Path, extra_env: dict | None = None) -> None:
    """Start the parent session in *cwd* on *sm*, on a backend that hosts shared sessions."""
    await sm.get_or_create(_PARENT_KEY, cwd=str(cwd), extra_env=extra_env)
    sm.get_provider(_PARENT_KEY).is_session_sharing_eligible = True


def _sharing_cfg() -> KiroCrewConfig:
    """The config the subagent manager loads: session sharing on."""
    cfg = _cfg({})
    cfg.agent.session_sharing = True
    return cfg


def _sharing_runner(sm):
    """A subagent manager on *sm*, its message builder a double."""
    from kiro_crew.subagent import SubagentManager

    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("msg", None))
    ctx.hooks.auto_approve_subagent_tools = False
    return SubagentManager(sessions=sm, ctx_builder=ctx)


def _child_info(cwd: str):
    """One template subagent of the parent, in *cwd*."""
    from kiro_crew.execution_context import execution_for_store
    from kiro_crew.subagent import SubagentInfo

    return SubagentInfo(
        execution_context=execution_for_store(""),
        id="sub6332",
        task="test",
        parent_session_key=_PARENT_KEY,
        cwd=cwd,
    )


async def _start_subagent(sm, cwd: str) -> AsyncMock:
    """Run one template subagent of the parent with *cwd*; returns the shared-arm double.

    The dedicated arm goes through ``sm.get_or_create`` for real, so the spawn's
    environment lands in the recording factory; the run itself then streams on
    a plain double, which keeps the turn out of these tests.
    """
    import kiro_crew.subagent as subagent_mod
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    client = MagicMock()

    async def _stream(_msg):
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = _stream
    real_get_or_create = sm.get_or_create

    async def _get_or_create(key, *args, **kwargs):
        provider, _, _ = await real_get_or_create(key, *args, **kwargs)
        # The turn's identity step calls this synchronously; the recording
        # factory's async double would leave an unawaited coroutine behind.
        provider.client.reclaim = MagicMock()
        return client, True, False

    async def _unchanged(shared_client, *_args):
        return shared_client

    cfg = _sharing_cfg()
    runner = _sharing_runner(sm)
    shared = AsyncMock(return_value=client)
    info = _child_info(cwd)
    runner._log_spawned(info)
    with (
        patch.object(sm, "get_or_create", _get_or_create),
        patch.object(runner, "_create_shared_session", shared),
        patch.object(subagent_mod, "replace_stale_shared_session", _unchanged),
        patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
    ):
        await runner._run_inner(info, _CHILD_KEY)
    return shared


class TestSubagentSharing:
    """A subagent shares its parent's process only when both bind the same AWS profile."""

    @pytest.mark.asyncio
    async def test_a_child_bound_to_another_profile_gets_its_own_process_and_profile(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        billing = _project(tmp_path, "billing")
        sandbox = _project(tmp_path, "sandbox")
        calls: list[dict] = []
        sm = SessionManager(
            _cfg({str(billing): "billing-prod", str(sandbox): "dev"}),
            provider_factory=_recording_factory(calls),
        )
        try:
            await _bound_parent(sm, billing)
            shared = await _start_subagent(sm, str(sandbox))
        finally:
            await sm.close_all()
        shared.assert_not_awaited()
        assert [c["extra_env"] for c in calls] == [
            {"AWS_PROFILE": "billing-prod", "AWS_DEFAULT_PROFILE": "billing-prod"},
            {"AWS_PROFILE": "dev", "AWS_DEFAULT_PROFILE": "dev"},
        ]

    @pytest.mark.asyncio
    async def test_an_unbound_child_of_a_bound_parent_does_not_inherit_its_profile(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        billing = _project(tmp_path, "billing")
        scratch = _project(tmp_path, "scratch")
        calls: list[dict] = []
        sm = SessionManager(
            _cfg({str(billing): "billing-prod"}), provider_factory=_recording_factory(calls)
        )
        try:
            await _bound_parent(sm, billing)
            shared = await _start_subagent(sm, str(scratch))
        finally:
            await sm.close_all()
        shared.assert_not_awaited()
        assert len(calls) == 2
        assert calls[1]["extra_env"] is None

    @pytest.mark.asyncio
    async def test_a_bound_child_of_an_unbound_parent_gets_its_own_profile(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        home = _project(tmp_path, "home")
        sandbox = _project(tmp_path, "sandbox")
        calls: list[dict] = []
        sm = SessionManager(_cfg({str(sandbox): "dev"}), provider_factory=_recording_factory(calls))
        try:
            await _bound_parent(sm, home)
            shared = await _start_subagent(sm, str(sandbox))
        finally:
            await sm.close_all()
        shared.assert_not_awaited()
        assert calls[0]["extra_env"] is None
        assert calls[1]["extra_env"] == {"AWS_PROFILE": "dev", "AWS_DEFAULT_PROFILE": "dev"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("child_dir", ["billing/src", "billing-mirror"])
    async def test_a_child_bound_to_the_same_profile_still_shares(
        self, tmp_path: Path, child_dir: str
    ) -> None:
        from kiro_crew.session import SessionManager

        billing = _project(tmp_path, "billing")
        mirror = _project(tmp_path, "billing-mirror")
        child = _project(tmp_path, child_dir)
        calls: list[dict] = []
        sm = SessionManager(
            _cfg({str(billing): "billing-prod", str(mirror): "billing-prod"}),
            provider_factory=_recording_factory(calls),
        )
        try:
            await _bound_parent(sm, billing)
            shared = await _start_subagent(sm, str(child))
        finally:
            await sm.close_all()
        shared.assert_awaited_once()
        assert len(calls) == 1, "the child starts no process of its own"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("child_cwd", ["", "sandbox"])
    async def test_an_empty_map_keeps_sharing_and_reads_no_path(
        self, tmp_path: Path, child_cwd: str
    ) -> None:
        from kiro_crew.session import SessionManager

        home = _project(tmp_path, "home")
        cwd = str(_project(tmp_path, child_cwd)) if child_cwd else ""
        calls: list[dict] = []
        sm = SessionManager(_cfg({}), provider_factory=_recording_factory(calls))
        try:
            await _bound_parent(sm, home)
            with patch.object(app, "_dir_parts", side_effect=AssertionError("no path resolved")):
                shared = await _start_subagent(sm, cwd)
        finally:
            await sm.close_all()
        shared.assert_awaited_once()
        assert len(calls) == 1, "the child starts no process of its own"

    @pytest.mark.asyncio
    async def test_an_unbound_child_keeps_sharing_a_profile_the_parents_caller_chose(
        self, tmp_path: Path
    ) -> None:
        from kiro_crew.session import SessionManager

        billing = _project(tmp_path, "billing")
        scratch = _project(tmp_path, "scratch")
        calls: list[dict] = []
        sm = SessionManager(
            _cfg({str(billing): "billing-prod"}), provider_factory=_recording_factory(calls)
        )
        try:
            await _bound_parent(sm, billing, extra_env={"AWS_PROFILE": "from-cron"})
            shared = await _start_subagent(sm, str(scratch))
        finally:
            await sm.close_all()
        shared.assert_awaited_once()
        assert [c["extra_env"] for c in calls] == [{"AWS_PROFILE": "from-cron"}]

    @pytest.mark.asyncio
    async def test_the_childs_profile_is_resolved_off_the_event_loop(self, tmp_path: Path) -> None:
        """A mapped directory on a mount that stops answering stalls the run, not the loop."""
        from kiro_crew import session_allocation
        from kiro_crew.session import SessionManager
        from kiro_crew.testing.wait import LoopHop, await_until, default_timeout

        billing = _project(tmp_path, "billing")
        sandbox = _project(tmp_path, "sandbox")
        calls: list[dict] = []
        sm = SessionManager(
            _cfg({str(billing): "billing-prod", str(sandbox): "dev"}),
            provider_factory=_recording_factory(calls),
        )
        loop_thread = threading.get_ident()
        hop = LoopHop(asyncio.get_running_loop())
        release = threading.Event()
        reached = asyncio.Event()
        # (resolved on the loop thread, answer), one per call of the resolver.
        resolved: list[tuple[bool, str | None]] = []
        real_resolve = session_allocation.resolve_project_profile

        async def _loop_turn() -> None:
            reached.set()

        def _blocking_resolve(mapping, cwd):
            if threading.get_ident() == loop_thread:
                # Blocking here would freeze this test along with the loop.
                resolved.append((True, real_resolve(mapping, cwd)))
                reached.set()
                return resolved[-1][1]
            # The loop runs a coroutine while this thread is still held.
            hop.run(_loop_turn(), "a loop turn while the resolver was blocked")
            release.wait(default_timeout())
            resolved.append((False, real_resolve(mapping, cwd)))
            return resolved[-1][1]

        try:
            await _bound_parent(sm, billing)
            with patch.object(session_allocation, "resolve_project_profile", _blocking_resolve):
                start = asyncio.ensure_future(_start_subagent(sm, str(sandbox)))
                try:
                    await await_until(reached, what="the child's profile lookup")
                    hop.check()
                    assert resolved == [], "the child's profile was resolved on the event loop"
                    assert not start.done(), "the run went on before its profile was resolved"
                    release.set()
                    shared = await await_until(start, what="the subagent start")
                finally:
                    release.set()
                    await await_until(asyncio.gather(start, return_exceptions=True))
        finally:
            await sm.close_all()
        assert resolved == [(False, "dev")]
        shared.assert_not_awaited()
        assert calls[1]["extra_env"] == {"AWS_PROFILE": "dev", "AWS_DEFAULT_PROFILE": "dev"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("binding", "shared"),
        [
            pytest.param("mapped", False, id="a-map-is-configured"),
            pytest.param("parent-only", False, id="only-the-parent-carries-one"),
            pytest.param("none", True, id="no-binding-anywhere"),
        ],
    )
    async def test_the_admission_probe_resolves_no_path(
        self, tmp_path: Path, binding: str, shared: bool
    ) -> None:
        """The synchronous probe prices a possible split dedicated without a ``realpath``."""
        from kiro_crew.session import SessionManager

        billing = _project(tmp_path, "billing")
        child = _project(billing, "src")
        mapping = {} if binding == "none" else {str(billing): "billing-prod"}
        sm = SessionManager(_cfg(mapping), provider_factory=_recording_factory([]))
        cfg = _sharing_cfg()
        try:
            await _bound_parent(sm, billing)
            if binding == "parent-only":
                # The binding was removed after the parent started with it.
                sm._cfg.aws.project_profiles = {}
            runner = _sharing_runner(sm)
            with (
                patch.object(app, "_dir_parts", side_effect=AssertionError("a path was resolved")),
                patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
            ):
                plan = runner._sharing_plan(_child_info(str(child)), cfg=cfg)
        finally:
            await sm.close_all()
        assert plan.shared is shared
