"""Additional coverage for ``kiro_crew.slack.handler``.

Focuses on branches the existing ``test_slack_handler*.py`` files leave
untouched:

* the rich-streaming ``EVENT_TOOL_CALL`` path (task cards, elapsed-time timer,
  the ``wait``-tool stream finalisation) and stream rotation after a failed
  ``append_stream``
* the ACP failure ladder inside ``handle_message``
  (timeout / process-died / prompt-busy)
* hook verdicts on tool calls and permission requests, plus per-session trust
* the defence-in-depth authorisation re-checks in ``handle_interaction``
* small module-level helpers: ``_add_phase_reaction``, ``set_orch_cfg``
  provider validation, ``_handle_cron_command`` list rendering,
  ``_handle_run_command`` idle status, ``_resolve_agent_name`` project lookup.

Everything runs against in-process doubles: no subprocess, no git, no network,
no sandbox. ``MockSlackClient`` comes from ``test/conftest.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import kiro_crew.slack.handler as h
from conftest import MockSlackClient
from kiro_crew.acp.client import AcpProcessDied, AcpPromptBusy, AcpTimeoutError
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
)
from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY, ToolHookResult
from kiro_crew.slack.handler import handle_interaction, handle_message
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.task_reporter import build_status

# ──────────────────────────────────────────────────────────────────────
# doubles
# ──────────────────────────────────────────────────────────────────────


class FakeProvider:
    """Provider double yielding a scripted event list, then ``complete``."""

    def __init__(self, events=None, raises: BaseException | None = None):
        self._events = events or []
        self._raises = raises
        self.approved: list = []
        self.rejected: list = []

    async def stream(self, message, timeout=120.0):
        for event in self._events:
            yield event
        if self._raises is not None:
            raise self._raises
        yield AcpEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id, option_id="allow_once"):
        self.approved.append(request_id)

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def start(self):
        pass

    async def shutdown(self):
        pass

    def context_usage_pct(self):
        return 0.0


class FakeSessions:
    """Minimal SessionManager surface used by ``handle_message``."""

    def __init__(self, provider: FakeProvider | None = None):
        self._provider = provider or FakeProvider()
        self.keys_seen: list[str] = []
        self._is_new = True
        self.reset_calls: list[str] = []
        self.failures: list[str] = []
        self.policies: dict[str, str] = {}
        self._session_map = None

    async def get_or_create(self, key, agent=None, channel_id=None):
        self.keys_seen.append(key)
        was_new = self._is_new
        self._is_new = False
        return self._provider, was_new, False

    def check_context_usage(self, key, provider):
        return 0.0

    def record_success(self, key):
        pass

    async def record_failure(self, key):
        self.failures.append(key)
        return False

    async def try_acquire(self, key):
        return self.has_session(key)

    def release(self, key):
        pass

    def is_busy(self, key):
        return False

    def begin_turn(self, key):
        pass

    async def set_channel(self, key, channel_id):
        pass

    def get_channel(self, key):
        return None

    def set_slack_link(self, key, thread_ts, channel_id):
        pass

    def get_slack_link(self, key):
        return None, None

    def get_session_for_thread(self, thread_ts):
        return None

    def set_approval_policy(self, key, policy):
        self.policies[key] = policy

    async def close_all(self):
        pass

    async def remove(self, key):
        pass

    async def destroy(self, key):
        pass

    def has_session(self, key):
        return key in self.keys_seen

    def get_provider(self, key):
        return None

    async def reset(self, key):
        self.reset_calls.append(key)

    def get_pid(self, key):
        return None

    def enqueue(self, key, msg_ts, text, **kwargs):
        return False

    def is_cancelled(self, key, msg_ts):
        return False

    def dequeue(self, key):
        return None

    def clear_queue(self, key):
        pass

    async def stop_turn(self, key, *, force=False, on_soft=None, on_hard=None):
        return "soft"


class _StreamingSlack(MockSlackClient):
    """MockSlackClient with the Slack rich-streaming API enabled."""

    def __init__(self):
        super().__init__()
        self._stream_enabled = True


class _RotatingSlack(_StreamingSlack):
    """``append_stream`` fails once, forcing exactly one stream rotation."""

    def __init__(self):
        super().__init__()
        self._appends = 0

    async def append_stream(self, channel, ts, text):
        self._appends += 1
        await super().append_stream(channel, ts, text)
        return self._appends != 1


def _has_approval_prompt(slack) -> bool:
    """True when an interactive tool-approval Block Kit prompt was posted."""
    for kind, kw in slack.actions:
        if kind != "blocks":
            continue
        for block in kw.get("blocks") or []:
            for el in block.get("elements") or []:
                if el.get("action_id") == h._ACTION_APPROVE:
                    return True
    return False


def _kinds(slack) -> list[str]:
    return [a[0] for a in slack.actions]


def _all_text(slack) -> str:
    return " ".join(
        str(a[1].get("text") or "")
        for a in slack.actions
        if a[0] in ("post", "update", "stop_stream", "append_stream")
    )


@pytest.fixture()
def owner(monkeypatch):
    monkeypatch.setattr(h, "_owner_id", "U1")
    return "U1"


@pytest.fixture(autouse=True)
def _clean_approval_state():
    h._pending_approvals.clear()
    h._linked_approvals.clear()
    h._trusted_sessions.clear()
    yield
    h._pending_approvals.clear()
    h._linked_approvals.clear()
    h._trusted_sessions.clear()


# ──────────────────────────────────────────────────────────────────────
# _add_phase_reaction
# ──────────────────────────────────────────────────────────────────────
class TestAddPhaseReaction:
    @pytest.mark.asyncio
    async def test_suppressed_phase_adds_nothing(self, monkeypatch):
        """A ``null`` sentinel in ``slack.reactions`` means "no emoji at all"."""
        monkeypatch.setitem(h._PHASE_EMOJIS, "done", None)
        slack = MockSlackClient()
        await h._add_phase_reaction(slack, "C1", "m1", "done")
        assert slack.actions == []

    @pytest.mark.asyncio
    async def test_configured_phase_is_added(self, monkeypatch):
        monkeypatch.setitem(h._PHASE_EMOJIS, "done", "tada")
        slack = MockSlackClient()
        await h._add_phase_reaction(slack, "C1", "m1", "done")
        assert slack.actions == [("react", {"channel": "C1", "ts": "m1", "emoji": "tada"})]

    @pytest.mark.asyncio
    async def test_unknown_phase_is_ignored(self):
        """A phase with no entry at all resolves to None -> no API call."""
        slack = MockSlackClient()
        await h._add_phase_reaction(slack, "C1", "m1", "not-a-phase")
        assert slack.actions == []


# ──────────────────────────────────────────────────────────────────────
# privacy modifiers
# ──────────────────────────────────────────────────────────────────────
class TestIncognitoModifierIdempotence:
    @pytest.mark.asyncio
    async def test_second_apply_is_a_no_op(self):
        """``!incognito`` twice in one thread must not re-notify the user."""
        h._mark_incognito("slack:t1")
        slack = MockSlackClient()
        await h._apply_incognito_modifier("slack:t1", "U1", "C1", slack, FakeSessions(), "t1")
        assert slack.actions == []


# ──────────────────────────────────────────────────────────────────────
# set_orch_cfg — voice_reply provider validation
# ──────────────────────────────────────────────────────────────────────
class _Cfg:
    def __init__(self, voice_reply: dict):
        self.raw = {"voice_reply": voice_reply}


class TestSetOrchCfgProviderValidation:
    def test_unknown_provider_falls_back_to_local(self, monkeypatch, caplog):
        # Flipped from a "polly" fallback: an unrecognised provider value must
        # not land on a PAID cloud service. Local costs nothing and degrades
        # visibly; a cloud fallback bills an account nobody chose.
        monkeypatch.setattr(h, "_vc", h._VoiceConfig())
        monkeypatch.setattr(h, "_orch_cfg", None, raising=False)
        with caplog.at_level("WARNING"):
            h.set_orch_cfg(_Cfg({"enabled": True, "provider": "ploly"}))
        assert h._vc.provider == "piper"
        assert "ploly" in caplog.text

    def test_valid_provider_is_kept_and_enabled_implies_auto_reply(self, monkeypatch):
        monkeypatch.setattr(h, "_vc", h._VoiceConfig())
        monkeypatch.setattr(h, "_orch_cfg", None, raising=False)
        h.set_orch_cfg(_Cfg({"enabled": True, "provider": "piper", "voice_id": "Amy"}))
        assert h._vc.provider == "piper"
        assert h._vc.global_enabled is True
        # auto_reply_to_voice defaults to the value of `enabled`.
        assert h._vc.auto_reply_to_voice is True
        assert h._vc.default_voice == "Amy"


# ──────────────────────────────────────────────────────────────────────
# _resolve_agent_name — project-local agents win
# ──────────────────────────────────────────────────────────────────────
class TestResolveAgentNameProject:
    def test_project_agent_declared_name_wins_over_stem(self, tmp_path):
        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.json").write_text(
            json.dumps({"name": "team-reviewer"}), encoding="utf-8"
        )
        assert h._resolve_agent_name("reviewer", str(tmp_path)) == "team-reviewer"

    def test_non_matching_project_agent_does_not_short_circuit(self, tmp_path):
        agents = tmp_path / ".kiro" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
        # No project match and (in an isolated KIROCREW_HOME) no user agent either.
        assert h._resolve_agent_name("nope", str(tmp_path)) is None


# ──────────────────────────────────────────────────────────────────────
# _handle_cron_command — `cron list` rendering
# ──────────────────────────────────────────────────────────────────────
class _FakeCronService:
    def __init__(self, jobs):
        self._jobs = jobs

    def list_jobs(self, include_disabled=False):
        return list(self._jobs)


def _at_job(job_id: str, offset: float, **kw) -> CronJob:
    return CronJob(
        id=job_id,
        name=job_id,
        message=f"do {job_id}",
        schedule=CronSchedule(kind="at", at_ts=time.time() + offset),
        **kw,
    )


class TestCronListRendering:
    @pytest.mark.asyncio
    async def test_relative_next_run_buckets(self):
        jobs = [
            _at_job("days", 2 * 86400 + 3 * 3600 + 1800),
            _at_job("hours", 5 * 3600 + 30 * 60 + 30),
            _at_job("mins", 5 * 60 + 30),
            _at_job("soon", 30),
        ]
        out = await _handle_cron("cron list", jobs)
        assert "⏭ in 2d 3h" in out
        assert "⏭ in 5h 30m" in out
        assert "⏭ in 5m" in out
        assert "⏭ in <1m" in out

    @pytest.mark.asyncio
    async def test_last_status_markers_and_disabled_prefix(self):
        jobs = [
            _at_job("okjob", 600, last_status="ok"),
            _at_job("badjob", 600, last_status="error"),
            CronJob(
                id="offjob",
                name="offjob",
                message="paused work",
                schedule=CronSchedule(kind="every", every_secs=60),
                enabled=False,
            ),
        ]
        out = await _handle_cron("cron list", jobs)
        lines = {line.split("`")[1]: line for line in out.splitlines() if "`" in line}
        assert lines["okjob"].endswith(("✓", "m")) and " ✓" in lines["okjob"]
        assert " ❌" in lines["badjob"]
        assert lines["offjob"].startswith("⏸️")
        # A disabled job has no next run at all.
        assert "⏭" not in lines["offjob"]

    @pytest.mark.asyncio
    async def test_due_now_renders_now(self):
        job = CronJob(
            id="due",
            name="due",
            message="tick",
            schedule=CronSchedule(kind="every", every_secs=60),
            created_ts=1.0,
        )
        out = await _handle_cron("cron list", [job])
        assert "⏭ now" in out

    @pytest.mark.asyncio
    async def test_message_is_truncated_to_50_chars(self):
        job = _at_job("long", 600)
        job.message = "x" * 200
        out = await _handle_cron("cron list", [job])
        assert "x" * 50 in out
        assert "x" * 51 not in out

    @pytest.mark.asyncio
    async def test_empty_and_unrecognised_forms(self):
        assert await _handle_cron("cron list", []) == "No cron jobs scheduled."
        # not a cron command at all
        assert await _handle_cron("hello there", []) is None
        # known action, missing job id
        assert await _handle_cron("cron pause", []) is None
        # unknown action with a job id
        assert await _handle_cron("cron frobnicate j1", []) is None


async def _handle_cron(text: str, jobs) -> str | None:
    return await h._handle_cron_command(text, _FakeCronService(jobs), "C1", "t1")


# ──────────────────────────────────────────────────────────────────────
# _handle_run_command
# ──────────────────────────────────────────────────────────────────────
class _FakeRunner:
    def __init__(self, running=False, status=None):
        self.running = running
        self._status = status or {}
        self.cancelled = False
        self.started: list[Path] = []

    def status(self):
        return dict(self._status)

    def cancel(self):
        self.cancelled = True

    async def start_background(self, spec_path, source=""):
        self.started.append(spec_path)


class _LiveTask:
    """Stand-in for an in-flight asyncio task; ``build_status`` only calls done()."""

    @staticmethod
    def done() -> bool:
        return False


def _running_status(*, completed: int = 2, total: int = 5, current: int = 3) -> dict:
    """A real ``build_status()`` payload for one in-flight run.

    Progress lives per run inside ``runs``; there is no top-level
    ``completed``/``steps``/``current_step`` for a renderer to read.
    """
    statuses = [TaskStatus.PASSED] * completed + [TaskStatus.PENDING] * (total - completed)
    run = Project(
        spec_path="/tmp/spec.md",
        spec_content="",
        task_id="live",
        name="Live Task",
        status="executing",
        current_task=current,
        tasks=[
            Task(index=i + 1, title=f"t{i + 1}", description="", status=s)
            for i, s in enumerate(statuses)
        ],
    )
    return build_status({"live": run}, {"live": _LiveTask()}, "kirocrew")


class TestRunCommand:
    @pytest.mark.asyncio
    async def test_status_when_idle(self):
        out = await h._handle_run_command(
            "task run status", _FakeRunner(), MockSlackClient(), "C1", "t1"
        )
        assert out == "No task running."

    @pytest.mark.asyncio
    async def test_status_when_running(self):
        runner = _FakeRunner(running=True, status=_running_status())
        out = await h._handle_run_command("task run status", runner, MockSlackClient(), "C1", "t1")
        assert "Steps: 2/5" in out
        assert "Current: step 3" in out

    @pytest.mark.asyncio
    async def test_cancel_paths(self):
        idle = _FakeRunner()
        assert (
            await h._handle_run_command("task run cancel", idle, MockSlackClient(), "C1", "t1")
            == "No task running."
        )
        assert idle.cancelled is False
        busy = _FakeRunner(running=True)
        assert "cancelled" in await h._handle_run_command(
            "task run cancel", busy, MockSlackClient(), "C1", "t1"
        )
        assert busy.cancelled is True

    @pytest.mark.asyncio
    async def test_project_run_alias_and_missing_spec(self, tmp_path):
        missing = tmp_path / "nope.md"
        out = await h._handle_run_command(
            f"project run {missing}", _FakeRunner(), MockSlackClient(), "C1", "t1"
        )
        assert "Spec file not found" in out

    @pytest.mark.asyncio
    async def test_start_success_and_failure(self, tmp_path):
        spec = tmp_path / "task.md"
        spec.write_text("# task\n", encoding="utf-8", newline="\n")

        runner = _FakeRunner()
        out = await h._handle_run_command(f"task run {spec}", runner, MockSlackClient(), "C1", "t1")
        assert "Task started" in out and runner.started == [spec]

        class _Boom(_FakeRunner):
            async def start_background(self, spec_path, source=""):
                raise RuntimeError("spawn refused")

        out = await h._handle_run_command(
            f"task run {spec}", _Boom(), MockSlackClient(), "C1", "t1"
        )
        assert "Failed to start: spawn refused" in out

    @pytest.mark.asyncio
    async def test_non_run_text_is_not_intercepted(self):
        runner = _FakeRunner()
        slack = MockSlackClient()
        assert await h._handle_run_command("hello", runner, slack, "C1", "t1") is None
        assert await h._handle_run_command("task run ", runner, slack, "C1", "t1") is None


# ──────────────────────────────────────────────────────────────────────
# _handle_slash_command — !agent off with an unwritable config
# ──────────────────────────────────────────────────────────────────────
class TestSlashAgentResetFailure:
    @pytest.mark.asyncio
    async def test_config_write_failure_is_reported_not_raised(self, monkeypatch, owner):
        def _boom(name):
            raise ValueError("Failed to write config: disk full")

        monkeypatch.setattr(h, "_set_default_agent", _boom)
        slack = MockSlackClient()
        out = await h._handle_slash_command(
            "!agent off",
            slack,
            FakeSessions(),
            "C1",
            "t1",
            "m1",
            "slack:t1",
            owner,
        )
        assert out == ""
        assert "disk full" in _all_text(slack)


# ──────────────────────────────────────────────────────────────────────
# _maybe_auto_title_slack — a tool request during titling must be rejected
# ──────────────────────────────────────────────────────────────────────
class _TitleSessions(FakeSessions):
    def __init__(self, provider):
        super().__init__(provider)
        self.released: list[str] = []

    def release(self, key):
        self.released.append(key)


class TestAutoTitleToolRejection:
    @pytest.mark.asyncio
    async def test_permission_request_is_rejected_and_title_still_set(self):
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", title="Bash"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Deploy plan review"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = _TitleSessions(provider)
        slack = MockSlackClient()
        await h._maybe_auto_title_slack(
            slack, sessions, "C1", "slack:t1", None, "user text", "assistant text"
        )
        # The titling session is never allowed to run tools.
        assert provider.rejected == ["rq1"]
        titles = [a for a in slack.actions if a[0] == "set_thread_title"]
        assert titles and titles[0][1]["title"] == "Deploy plan review"
        assert sessions.released  # BACKGROUND_KEY released in the finally

    @pytest.mark.asyncio
    async def test_skip_reply_clears_the_claim_for_retry(self):
        h._titled_threads["slack:t2"] = "auto"
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="SKIP"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        slack = MockSlackClient()
        await h._maybe_auto_title_slack(
            slack, _TitleSessions(provider), "C1", "slack:t2", None, "hi", "hello"
        )
        assert "slack:t2" not in h._titled_threads
        assert not [a for a in slack.actions if a[0] == "set_thread_title"]

    def test_lock_is_rebound_when_the_event_loop_changes(self):
        """Regression for #4789 (mechanism now shared via #4800's LoopBoundLock):
        the module-global auto-title lock must keep working when the running
        event loop changes.

        ``pytest-asyncio`` gives every async test a fresh loop, and on
        Python 3.10+ acquiring an ``asyncio.Lock`` from a loop other than the
        one it was first used on raises ``RuntimeError`` — which the bare
        ``except Exception:`` in ``_maybe_auto_title_slack`` then swallowed,
        silently skipping the permission-rejection branch. Prove the contract
        deterministically with two distinct loops instead of replaying the
        order-dependent CI flake.
        """

        def _run_once(session_key: str):
            provider = FakeProvider(
                [
                    AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", title="Bash"),
                    AcpEvent(kind=EVENT_TEXT_CHUNK, text="Deploy plan review"),
                    AcpEvent(kind=EVENT_COMPLETE),
                ]
            )
            slack = MockSlackClient()

            async def _go():
                # Touch the lock on this loop first, then run the real path.
                lock = h._get_auto_title_lock()
                await lock.acquire()
                lock.release()
                inner = lock._bound()  # this loop's underlying asyncio.Lock
                await h._maybe_auto_title_slack(
                    slack, _TitleSessions(provider), "C1", session_key, None, "u", "a"
                )
                return lock, inner

            lock, inner = asyncio.run(_go())
            return lock, inner, provider, slack

        lock1, inner1, provider1, _ = _run_once("slack:loop1")
        lock2, inner2, provider2, slack2 = _run_once("slack:loop2")

        # One shared chokepoint object, but each loop must get its OWN inner
        # lock — this is the rebinding invariant that #4789's fix introduced
        # and #4800's LoopBoundLock now carries.
        assert lock2 is lock1
        assert inner2 is not inner1
        # …and the real path must still work there: the rejection is recorded
        # (this was the exact assertion the flake broke) and the title lands.
        assert provider1.rejected == ["rq1"]
        assert provider2.rejected == ["rq1"]
        titles = [a for a in slack2.actions if a[0] == "set_thread_title"]
        assert titles and titles[0][1]["title"] == "Deploy plan review"


# ──────────────────────────────────────────────────────────────────────
# handle_interaction — defence-in-depth re-checks
# ──────────────────────────────────────────────────────────────────────
def _revoking_allow_check(monkeypatch):
    """``is_allowed_user`` that passes the entry gate then revokes.

    Models an authorisation revoked between the outer gate and the inner
    re-check — the reason those inner checks exist.
    """
    calls = {"n": 0}

    def _fake(user_id):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(h, "is_allowed_user", _fake)
    return calls


class TestHandleInteractionAuthReChecks:
    @pytest.mark.asyncio
    async def test_late_trust_click_rejected_when_authorisation_revoked(self, monkeypatch):
        calls = _revoking_allow_check(monkeypatch)
        slack = MockSlackClient()
        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, "U1", thread_ts="t1", slack=slack
        )
        assert out is None
        assert calls["n"] == 2
        assert not h._trusted_sessions
        # Rejected before any Slack call is made.
        assert slack.actions == []

    @pytest.mark.asyncio
    async def test_late_trust_click_needs_a_slack_client_to_verify_ownership(self, owner):
        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, owner, thread_ts="t1", slack=None
        )
        assert out is None
        assert not h._trusted_sessions

    @pytest.mark.asyncio
    async def test_late_trust_click_denied_when_not_thread_owner(self, owner):
        slack = MockSlackClient()
        slack._fetch_thread_replies_result = [{"user": "U_OTHER"}]
        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, owner, thread_ts="t1", slack=slack
        )
        assert out is None
        assert not h._trusted_sessions

    @pytest.mark.asyncio
    async def test_late_trust_refused_when_session_map_lookup_fails(self, monkeypatch, owner):
        import kiro_crew.session as session_mod

        class _BoomMap:
            def __init__(self):
                raise RuntimeError("session map unreadable")

        monkeypatch.setattr(session_mod, "SessionMap", _BoomMap)
        slack = MockSlackClient()
        slack._fetch_thread_replies_result = [{"user": owner}]
        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, owner, thread_ts="t1", slack=slack
        )
        # Fail closed: no trust granted when the thread->session mapping is unknown.
        assert out is None
        assert not h._trusted_sessions

    @pytest.mark.asyncio
    async def test_late_trust_refused_when_ownership_fetch_raises(self, owner):
        class _Boom(MockSlackClient):
            async def fetch_thread_replies(self, channel, thread_ts, limit=200, **kw):
                raise RuntimeError("slack down")

        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, owner, thread_ts="t1", slack=_Boom()
        )
        assert out is None
        assert not h._trusted_sessions

    @pytest.mark.asyncio
    async def test_late_trust_binds_to_the_linked_dashboard_session(self, monkeypatch, owner):
        """A thread linked to a dashboard slot must grant trust on the LINKED
        session key, not the bare thread ts."""
        import kiro_crew.session as session_mod

        class _Map:
            def get_session_for_thread(self, thread_ts):
                return "dash:slot-1"

        monkeypatch.setattr(session_mod, "SessionMap", _Map)
        slack = MockSlackClient()
        slack._fetch_thread_replies_result = [{"user": owner}]
        sessions = FakeSessions()
        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, owner, thread_ts="t1", slack=slack, sessions=sessions
        )
        assert out == h._ACTION_TRUST
        assert h.is_session_trusted("dash:slot-1")
        assert sessions.policies == {"dash:slot-1": "auto"}

    @pytest.mark.asyncio
    async def test_trust_escalation_rejected_when_authorisation_revoked(self, monkeypatch):
        calls = _revoking_allow_check(monkeypatch)
        provider = FakeProvider()
        pending = h._PendingApproval(provider, "rq7", session_key="slack:t1")
        h._pending_approvals["C1:m1"] = pending
        sessions = FakeSessions()

        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, "U1", thread_ts="t1", sessions=sessions
        )
        assert out == h._ACTION_REJECT
        assert calls["n"] == 2
        # The turn is unblocked with a rejection, trust is NOT granted, and the
        # entry is consumed so a retry cannot reuse it.
        assert pending.future.result() == h._OUTCOME_REJECTED
        assert not h._trusted_sessions
        assert sessions.policies == {}
        assert "C1:m1" not in h._pending_approvals

    @pytest.mark.asyncio
    async def test_trust_without_session_key_still_approves(self, owner):
        provider = FakeProvider()
        pending = h._PendingApproval(provider, "rq8", session_key="")
        h._pending_approvals["C1:m1"] = pending

        out = await handle_interaction("C1", "m1", h._ACTION_TRUST, owner)
        assert out == h._ACTION_TRUST
        assert provider.approved == ["rq8"]
        assert pending.future.result() == h._OUTCOME_APPROVED
        # Nothing to trust — the set stays empty rather than gaining "".
        assert not h._trusted_sessions

    @pytest.mark.asyncio
    async def test_trust_with_session_key_propagates_policy_to_subagents(self, owner):
        provider = FakeProvider()
        h._pending_approvals["C1:m1"] = h._PendingApproval(provider, "rq9", session_key="slack:t1")
        sessions = FakeSessions()
        out = await handle_interaction(
            "C1", "m1", h._ACTION_TRUST, owner, thread_ts="t1", sessions=sessions
        )
        assert out == h._ACTION_TRUST
        assert "slack:t1" in h._trusted_sessions
        assert sessions.policies["slack:t1"] == "auto"


# ──────────────────────────────────────────────────────────────────────
# handle_message — rich streaming tool cards
# ──────────────────────────────────────────────────────────────────────
class TestStreamingToolCards:
    @pytest.mark.asyncio
    async def test_consecutive_tools_complete_the_previous_card(self, monkeypatch):
        monkeypatch.setattr(h, "_EDIT_INTERVAL", 0.0)
        slack = _StreamingSlack()
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Let me look."),
                AcpEvent(
                    kind=EVENT_TOOL_CALL,
                    title="Read File",
                    tool_kind="read",
                    tool_purpose="Read config",
                ),
                AcpEvent(kind=EVENT_TOOL_CALL, title="Grep", tool_kind="grep"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
            ]
        )
        await handle_message(slack, FakeSessions(provider), "C1", "go", None, "m1", "U1")

        cards = [a[1] for a in slack.actions if a[0] == "append_task"]
        statuses = [(c["task_id"], c["status"]) for c in cards]
        # First tool opens tool_1, second tool completes tool_1 then opens tool_2.
        assert ("tool_1", "in_progress") in statuses
        assert ("tool_1", "complete") in statuses
        assert ("tool_2", "in_progress") in statuses
        # The purpose (not the raw tool name) is the card title when present.
        assert any(c["title"] == "Read config" for c in cards)
        # Thread status reflects the running tool.
        assert any(
            a[1].get("status") == "is using Grep"
            for a in slack.actions
            if a[0] == "set_thread_status"
        )
        assert "Done." in _all_text(slack)

    @pytest.mark.asyncio
    async def test_wait_tool_finalises_the_stream(self, monkeypatch):
        """The ``wait`` MCP tool blocks for up to 30min, so the stream must be
        closed rather than left open until Slack errors it out."""
        monkeypatch.setattr(h, "_EDIT_INTERVAL", 0.0)
        slack = _StreamingSlack()
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="waiting for CI"),
                AcpEvent(kind=EVENT_TOOL_CALL, title="wait", tool_kind="mcp"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="CI is green"),
            ]
        )
        await handle_message(slack, FakeSessions(provider), "C1", "go", None, "m1", "U1")

        kinds = _kinds(slack)
        # Stream closed mid-turn, then reopened for the post-wait text.
        assert kinds.count("start_stream") >= 2
        assert kinds.count("stop_stream") >= 2
        # The pre-wait text was already published, so only post-wait text is
        # carried in the final message.
        final = [a[1]["text"] for a in slack.actions if a[0] == "stop_stream"][-1]
        assert "CI is green" in (final or "")

    @pytest.mark.asyncio
    async def test_failed_append_rotates_the_stream(self, monkeypatch):
        monkeypatch.setattr(h, "_EDIT_INTERVAL", 0.0)
        slack = _RotatingSlack()
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="first chunk "),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="second chunk"),
            ]
        )
        await handle_message(slack, FakeSessions(provider), "C1", "go", None, "m1", "U1")

        # One rotation: the dead stream is stopped and a fresh one started.
        assert _kinds(slack).count("start_stream") >= 2
        assert "second chunk" in _all_text(slack)


# ──────────────────────────────────────────────────────────────────────
# handle_message — ACP failure ladder
# ──────────────────────────────────────────────────────────────────────
class TestAcpFailureLadder:
    @pytest.mark.asyncio
    async def test_timeout_publishes_partial_output(self):
        slack = MockSlackClient()
        sessions = FakeSessions(FakeProvider(raises=AcpTimeoutError("half an answer")))
        await handle_message(slack, sessions, "C1", "go", None, "m1", "U1")
        assert "half an answer" in _all_text(slack)
        assert sessions.failures == ["m1"]

    @pytest.mark.asyncio
    async def test_timeout_without_partial_output_falls_back_to_notice(self):
        slack = MockSlackClient()
        sessions = FakeSessions(FakeProvider(raises=AcpTimeoutError()))
        await handle_message(slack, sessions, "C1", "go", None, "m1", "U1")
        assert "timed out" in _all_text(slack).lower()

    @pytest.mark.asyncio
    async def test_process_died_is_reported(self):
        slack = MockSlackClient()
        sessions = FakeSessions(FakeProvider(raises=AcpProcessDied("boom")))
        await handle_message(slack, sessions, "C1", "go", None, "m1", "U1")
        assert "process died" in _all_text(slack).lower()
        assert sessions.failures == ["m1"]

    @pytest.mark.asyncio
    async def test_prompt_busy_resets_the_session(self):
        slack = MockSlackClient()
        sessions = FakeSessions(FakeProvider(raises=AcpPromptBusy("prompt in flight")))
        await handle_message(slack, sessions, "C1", "go", None, "m1", "U1")
        # A wedged session must be reset so the NEXT message cold-starts.
        assert sessions.reset_calls == ["m1"]
        assert "prompt in flight" in _all_text(slack)

    @pytest.mark.asyncio
    async def test_prompt_busy_reset_failure_is_swallowed(self):
        class _NoReset(FakeSessions):
            async def reset(self, key):
                raise RuntimeError("reset refused")

        slack = MockSlackClient()
        sessions = _NoReset(FakeProvider(raises=AcpPromptBusy("prompt in flight")))
        await handle_message(slack, sessions, "C1", "go", None, "m1", "U1")
        # The reply still reaches the user even though the reset failed.
        assert "prompt in flight" in _all_text(slack)


# ──────────────────────────────────────────────────────────────────────
# handle_message — hook verdicts and per-session trust
# ──────────────────────────────────────────────────────────────────────
class _Builder:
    """ContextBuilder double: real-shaped hooks, no memory/embedding work."""

    def __init__(self, tool_result: ToolHookResult):
        self.conversation_log = None
        self.hooks = _Hooks(tool_result)

    def build_message(self, text, is_new, session_key, **kw):
        return text, None


class _Hooks:
    auto_approve_subagent_spawn = False

    def __init__(self, tool_result: ToolHookResult):
        self._tool_result = tool_result
        self.tool_calls: list[str] = []

    def on_message(self, *a, **kw):
        from kiro_crew.hooks import HookResult

        return HookResult(action="none")

    def on_tool_call(self, title, **kw):
        self.tool_calls.append(title)
        return self._tool_result


class TestToolHookVerdicts:
    @pytest.mark.asyncio
    async def test_tool_call_deny_is_surfaced_as_unenforceable_warning(self):
        """EVENT_TOOL_CALL is informational — the tool is ALREADY running, so a
        hook deny warns instead of claiming it was blocked."""
        slack = MockSlackClient()
        builder = _Builder(ToolHookResult(action=TOOL_DENY, reason="denylisted"))
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TOOL_CALL, title="rm -rf /tmp/x", tool_kind="execute"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="tail"),
            ]
        )
        await handle_message(
            slack,
            FakeSessions(provider),
            "C1",
            "go",
            None,
            "m1",
            "U1",
            context_builder=builder,
        )
        text = _all_text(slack)
        assert "flagged by security" in text
        assert "cannot be stopped here" in text
        assert builder.hooks.tool_calls == ["rm -rf /tmp/x"]

    @pytest.mark.asyncio
    async def test_permission_request_hook_auto_approve(self):
        slack = MockSlackClient()
        builder = _Builder(ToolHookResult(action=TOOL_AUTO_APPROVE))
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq1", title="Read"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="read it"),
            ]
        )
        await handle_message(
            slack,
            FakeSessions(provider),
            "C1",
            "go",
            None,
            "m1",
            "U1",
            approval_mode=h.APPROVAL_INTERACTIVE,
            context_builder=builder,
        )
        assert provider.approved == ["rq1"]
        assert provider.rejected == []
        # No approval prompt was posted — the hook answered it.
        assert not _has_approval_prompt(slack)

    @pytest.mark.asyncio
    async def test_permission_request_hook_deny(self):
        slack = MockSlackClient()
        builder = _Builder(ToolHookResult(action=TOOL_DENY, reason="sensitive path"))
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq2", title="Write"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="tail"),
            ]
        )
        await handle_message(
            slack,
            FakeSessions(provider),
            "C1",
            "go",
            None,
            "m1",
            "U1",
            approval_mode=h.APPROVAL_INTERACTIVE,
            context_builder=builder,
        )
        assert provider.rejected == ["rq2"]
        assert provider.approved == []
        assert "blocked by hooks" in _all_text(slack)

    @pytest.mark.asyncio
    async def test_trusted_session_auto_approves_in_interactive_mode(self):
        slack = MockSlackClient()
        h.add_trusted_session("m1")
        builder = _Builder(ToolHookResult(action=TOOL_ALLOW))
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id="rq3", title="Bash"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            ]
        )
        await handle_message(
            slack,
            FakeSessions(provider),
            "C1",
            "go",
            None,
            "m1",
            "U1",
            approval_mode=h.APPROVAL_INTERACTIVE,
            context_builder=builder,
        )
        assert provider.approved == ["rq3"]
        # Trust means no button prompt at all.
        assert not _has_approval_prompt(slack)


# ──────────────────────────────────────────────────────────────────────
# handle_message — review-mode ephemeral draft
# ──────────────────────────────────────────────────────────────────────
class TestReviewMode:
    @pytest.mark.asyncio
    async def test_answer_is_held_back_as_an_ephemeral_draft(self, monkeypatch):
        monkeypatch.setattr(h, "_EDIT_INTERVAL", 0.0)
        slack = _StreamingSlack()
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Proposed answer "),
                AcpEvent(kind=EVENT_TOOL_CALL, title="Read File", tool_kind="read"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="for review"),
            ]
        )
        await handle_message(
            slack,
            FakeSessions(provider),
            "C1",
            "go",
            None,
            "m1",
            "U1",
            channel_activation=h.ACTIVATION_REVIEW,
            user_display_name="Zed",
        )

        # Nothing was streamed or posted publicly.
        assert not [a for a in slack.actions if a[0] in ("append_stream", "append_task")]
        assert not [a for a in slack.actions if a[0] == "post"]
        # The draft went out as an ephemeral with review buttons...
        eph = [a[1] for a in slack.actions if a[0] == "ephemeral"]
        assert len(eph) == 1
        assert "Proposed answer" in eph[0]["text"] and "for review" in eph[0]["text"]
        assert eph[0]["user_id"] == "U1"
        assert eph[0]["blocks"]
        # ...and the thread indicator says a review is outstanding.
        assert any(
            a[1].get("status") == "Awaiting review…"
            for a in slack.actions
            if a[0] == "set_thread_status"
        )
        # The draft is retrievable by the requester for the button handlers.
        keys = [k for k in h._review_drafts if k.startswith("C1|m1|")]
        assert len(keys) == 1
        draft, requester = h._review_drafts_get(keys[0])
        assert requester == "U1" and "Proposed answer" in draft
        h._review_drafts.clear()


# ──────────────────────────────────────────────────────────────────────
# handle_message — voice reply opt-in
# ──────────────────────────────────────────────────────────────────────
_LONG_ANSWER = "This reply is comfortably longer than the fifty-character voice threshold."


def _voice_on(monkeypatch, **fields):
    vc = h._VoiceConfig(**fields)
    monkeypatch.setattr(h, "_vc", vc)
    return vc


class TestVoiceReply:
    @pytest.mark.asyncio
    async def test_missing_tts_backend_warns_the_opted_in_user(self, monkeypatch):
        _voice_on(monkeypatch, global_enabled=True, provider="piper")
        monkeypatch.setattr(h, "_tts_available", lambda **kw: False)
        slack = MockSlackClient()
        provider = FakeProvider([AcpEvent(kind=EVENT_TEXT_CHUNK, text=_LONG_ANSWER)])
        await handle_message(slack, FakeSessions(provider), "C1", "go", None, "m1", "U1")

        eph = [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]
        assert len(eph) == 1
        assert "provider=piper" in eph[0]
        # piper-specific remediation, not the Polly one.
        assert "piper_model" in eph[0]
        assert "ada credentials" not in eph[0]

    @pytest.mark.asyncio
    async def test_voice_memo_gets_the_voice_in_voice_out_notice(self, monkeypatch):
        _voice_on(monkeypatch, auto_reply_to_voice=True, provider="polly")
        monkeypatch.setattr(h, "_tts_available", lambda **kw: False)
        slack = MockSlackClient()
        provider = FakeProvider([AcpEvent(kind=EVENT_TEXT_CHUNK, text=_LONG_ANSWER)])
        await handle_message(
            slack, FakeSessions(provider), "C1", "go", None, "m1", "U1", had_voice_input=True
        )
        eph = [a[1]["text"] for a in slack.actions if a[0] == "ephemeral"]
        assert len(eph) == 1
        assert "Received your voice memo" in eph[0]
        assert "ada credentials" in eph[0]

    @pytest.mark.asyncio
    async def test_available_backend_synthesises_with_per_thread_overrides(self, monkeypatch):
        vc = _voice_on(monkeypatch, provider="polly")
        vc.sessions.add("m1")
        vc.voices["m1"] = "Joanna"
        vc.engines["m1"] = "neural"
        monkeypatch.setattr(h, "_tts_available", lambda **kw: True)
        seen: list[dict] = []

        async def _fake_voice(slack_, channel, reply_ts, text, **kw):
            seen.append({"text": text, **kw})

        monkeypatch.setattr(h, "_safe_voice_reply", _fake_voice)
        slack = MockSlackClient()
        provider = FakeProvider([AcpEvent(kind=EVENT_TEXT_CHUNK, text=_LONG_ANSWER)])
        await handle_message(slack, FakeSessions(provider), "C1", "go", None, "m1", "U1")
        await asyncio.sleep(0)  # let the fire-and-forget task run

        assert len(seen) == 1
        assert seen[0]["voice_id"] == "Joanna"
        assert seen[0]["engine"] == "neural"
        assert not [a for a in slack.actions if a[0] == "ephemeral"]

    @pytest.mark.asyncio
    async def test_short_answer_is_not_spoken(self, monkeypatch):
        _voice_on(monkeypatch, global_enabled=True)
        monkeypatch.setattr(h, "_tts_available", lambda **kw: False)
        slack = MockSlackClient()
        provider = FakeProvider([AcpEvent(kind=EVENT_TEXT_CHUNK, text="short")])
        await handle_message(slack, FakeSessions(provider), "C1", "go", None, "m1", "U1")
        assert not [a for a in slack.actions if a[0] == "ephemeral"]


# ──────────────────────────────────────────────────────────────────────
# maybe_route_linked_thread — hand-off into a linked dashboard slot
# ──────────────────────────────────────────────────────────────────────
class _Slot:
    def __init__(self, running=False):
        self.key = "slot-1"
        self.running = running
        self.task = None
        self.appended: list[tuple[str, str]] = []
        self.queued: list[str] = []

    def append(self, role, text, cls="", *, broadcast_user=False, meta=None):
        # Mirror the real ``_ChatSlot.append`` contract enough for
        # append_and_surface: accept the delivery kwargs and return the
        # appended row with a minted ``meta.mid``.
        self.appended.append((role, text))
        return {
            "role": role,
            "content": text,
            "ts": "",
            "meta": {**(meta or {}), "mid": f"m-slot-{len(self.appended)}"},
        }

    def queue_append(self, text, *, meta=None, directive_user_origin):
        assert directive_user_origin is True
        # The linked-thread enqueue stamps the admission-time containment
        # snapshot (#5911) so the drain can re-assert it at delivery.
        assert isinstance(meta, dict)
        self.queued.append(text)


class _DashState:
    def __init__(self, slot):
        self._slot = slot
        self._background_tasks: set = set()
        self.broadcasts: list[tuple[str, dict]] = []
        self.slot_pushes = 0

    def get_linked_slot(self, thread_ts):
        return self._slot

    def broadcast_ws(self, event, payload):
        self.broadcasts.append((event, payload))

    def push_slots_update(self):
        self.slot_pushes += 1


class TestLinkedThreadRouting:
    @pytest.mark.asyncio
    async def test_idle_slot_starts_a_dashboard_turn(self, monkeypatch, owner):
        import kiro_crew.dashboard.chat as chat_mod

        ran: list[str] = []

        async def _fake_run_chat(state, slot, text, *, _directive_user_origin):
            assert _directive_user_origin is True
            ran.append(text)

        monkeypatch.setattr(chat_mod, "_run_chat", _fake_run_chat)
        slot = _Slot(running=False)
        state = _DashState(slot)
        monkeypatch.setattr(h, "_dashboard_state", state, raising=False)

        handled = await h.maybe_route_linked_thread(
            "check the build", "slack:t1", owner, "C1", MockSlackClient(), "t1"
        )
        assert handled is True
        # The task is created, registered for cancellation, and tracked on the slot.
        assert slot.task is not None
        assert slot.task in state._background_tasks
        await slot.task
        assert ran == ["check the build"]
        assert slot.queued == []
        assert state.slot_pushes == 1
        assert slot.appended == [("user", "check the build")]

    @pytest.mark.asyncio
    async def test_busy_slot_queues_instead_of_racing(self, monkeypatch, owner):
        slot = _Slot(running=True)
        state = _DashState(slot)
        monkeypatch.setattr(h, "_dashboard_state", state, raising=False)

        handled = await h.maybe_route_linked_thread(
            "second message", "slack:t1", owner, "C1", MockSlackClient(), "t1"
        )
        assert handled is True
        assert slot.queued == ["second message"]
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_bang_command_falls_through_to_normal_handling(self, monkeypatch, owner):
        slot = _Slot()
        monkeypatch.setattr(h, "_dashboard_state", _DashState(slot), raising=False)
        # `!stop` must reach the Slack handler even in a linked thread — it is
        # how the user halts the turn.
        handled = await h.maybe_route_linked_thread(
            "!stop", "slack:t1", owner, "C1", MockSlackClient(), "t1"
        )
        assert handled is False
        assert slot.appended == []

    @pytest.mark.asyncio
    async def test_unauthorised_user_is_denied_not_routed(self, monkeypatch):
        monkeypatch.setattr(h, "_owner_id", "U_OWNER")
        slot = _Slot()
        monkeypatch.setattr(h, "_dashboard_state", _DashState(slot), raising=False)
        slack = MockSlackClient()
        handled = await h.maybe_route_linked_thread(
            "leak the secrets", "slack:t1", "U_INTRUDER", "C1", slack, "t1"
        )
        assert handled is True
        assert slot.appended == [] and slot.queued == []
        assert "Not authorized." in _all_text(slack)


# ──────────────────────────────────────────────────────────────────────
# StatusReactionController — stall watchdog upgrade path
# ──────────────────────────────────────────────────────────────────────
class TestStallWatchdog:
    @pytest.mark.asyncio
    async def test_soft_then_hard_stall_emoji_replaces_not_stacks(self, monkeypatch):
        monkeypatch.setattr(h, "_STALL_SOFT_SECS", 0.001)
        monkeypatch.setattr(h, "_STALL_HARD_SECS", 0.005)
        slack = MockSlackClient()
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.on_progress()
        await asyncio.sleep(0.05)
        reacts = [a[1]["emoji"] for a in slack.actions if a[0] == "react"]
        unreacts = [a[1]["emoji"] for a in slack.actions if a[0] == "unreact"]
        assert h._STALL_EMOJI_SOFT in reacts
        assert h._STALL_EMOJI_HARD in reacts
        # Upgrading to the hard emoji removes the soft one.
        assert h._STALL_EMOJI_SOFT in unreacts
        ctrl.finalize()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_paused_watchdog_adds_nothing(self, monkeypatch):
        monkeypatch.setattr(h, "_STALL_SOFT_SECS", 0.001)
        monkeypatch.setattr(h, "_STALL_HARD_SECS", 0.005)
        slack = MockSlackClient()
        ctrl = h.StatusReactionController(slack, "C1", "m1")
        ctrl.on_progress()
        ctrl.pause_stall_watchdog()
        await asyncio.sleep(0.05)
        reacts = [a[1]["emoji"] for a in slack.actions if a[0] == "react"]
        assert h._STALL_EMOJI_SOFT not in reacts
        assert h._STALL_EMOJI_HARD not in reacts
        ctrl.finalize()
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_disabled_controller_never_calls_slack(self):
        slack = MockSlackClient()
        ctrl = h.StatusReactionController(slack, "C1", "m1", enabled=False)
        ctrl.set_phase("queued")
        ctrl.on_progress()
        ctrl.resume_stall_watchdog()
        ctrl.finalize()
        await asyncio.sleep(0.01)
        assert slack.actions == []
