"""The owner's output-seams redaction switch (``kiro_crew.redaction_seams``).

Each seam is exercised with the switch on and off; the record's migration rule, a
refusal gate and the always-on passes are pinned beside them. Credential-shaped
material is synthesized at runtime, as in ``test_credential_redaction_switch.py``.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from redaction_structured_cases import github_classic

from kiro_crew import redaction_seams, security
from kiro_crew.log_redaction import SecretRedactionFilter
from kiro_crew.security import redaction_switch

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _synth_labelled_secret() -> str:
    # A labelled AWS secret value: redacted by the credential plan on this branch.
    return "aws_secret_access_key = X" + hashlib.sha256(b"kc-seams").hexdigest()[:39]


def _synth_token_url() -> str:
    return "https://approve.example.test/auth?token=" + hashlib.sha256(b"kc-seams-t").hexdigest()


def _synth_bearer() -> str:
    return "Authorization: Bearer " + hashlib.sha256(b"kc-seams-b").hexdigest()


def _synth_exfil_url() -> str:
    blob = hashlib.sha256(b"kc-seams-x").hexdigest() * 4
    return f"https://collector.example.test/x?d={blob}"


def _synth_vault_value() -> str:
    return "vault-" + hashlib.sha256(b"kc-seams-v").hexdigest()[:24]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    """A tmp keystone, and the process verdict restored after every test."""
    store = tmp_path / "credential_redaction.json"
    _floor_monkeypatch.setattr(redaction_switch, "_path", lambda: store, raising=True)
    _floor_monkeypatch.setattr(redaction_seams, "_seams_off", False)
    yield store


def _seams(off: bool, monkeypatch) -> None:
    monkeypatch.setattr(redaction_seams, "_seams_off", off)


# ------------------------------------------------------------ the record and migration


class TestRecord:
    def test_absent_record_is_on_for_both_switches(self):
        state = redaction_switch.read_state()
        assert state.enabled is True and state.seams_enabled is True
        redaction_switch.publish_seams(state)
        assert redaction_seams.seams_off() is False

    def test_an_old_off_record_keeps_its_files_view_only_meaning(self, _isolated):
        """A record written before ``seams_enabled`` existed: the seams read ON."""
        _isolated.write_text(
            json.dumps({"enabled": False, "changed_at": "2026-09-24T23:00:00+00:00"}),
            encoding="utf-8",
        )
        state = redaction_switch.read_state()
        assert state.enabled is False
        assert state.seams_enabled is True
        redaction_switch.publish_seams(state)
        assert redaction_seams.seams_off() is False
        secret = _synth_labelled_secret()
        with redaction_seams.output_seam(redaction_seams.ACP_PROMPT):
            assert security.redact_credentials(secret)[0] != secret

    @pytest.mark.parametrize("raw", ['"false"', "0", "null", "[]"])
    def test_only_a_literal_false_turns_the_seams_off(self, _isolated, raw):
        _isolated.write_text('{"seams_enabled": %s}' % raw, encoding="utf-8")
        assert redaction_switch.read_state().seams_enabled is True

    def test_a_seams_off_round_trips_and_leaves_the_files_switch_alone(self, _isolated):
        redaction_switch.set_enabled(False, changed_at="2026-11-01T00:00:00+00:00")
        redaction_switch.set_seams_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        stored = json.loads(_isolated.read_text(encoding="utf-8"))
        assert stored == {
            "enabled": False,
            "changed_at": "2026-11-01T00:00:00+00:00",
            "seams_enabled": False,
            "seams_changed_at": "2026-11-02T00:00:00+00:00",
            "engine": "structured",
            "engine_changed_at": "",
        }
        state = redaction_switch.read_state()
        assert (state.enabled, state.seams_enabled) == (False, False)
        # Moving the Files-view switch keeps the seams switch as stored.
        redaction_switch.set_enabled(True, changed_at="2026-11-03T00:00:00+00:00")
        assert redaction_switch.read_state().seams_enabled is False
        redaction_switch.set_seams_enabled(True, changed_at="2026-11-04T00:00:00+00:00")
        assert redaction_switch.read_state().seams_enabled is True

    def test_an_unknown_seam_name_is_refused(self):
        with pytest.raises(ValueError):
            with redaction_seams.output_seam("chat"):
                pass


# ------------------------------------------------------------------ the ACP prompt seam


class _Log:
    """Conversation-log stand-in for ``build_session_replay``."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def read_messages_chained(self, session_key: str) -> list[dict]:
        return list(self._rows)

    def read_messages(self, session_key: str) -> list[dict]:
        return list(self._rows)


class TestAcpPromptSeam:
    def _replay(self, text: str) -> str:
        from kiro_crew.context import build_session_replay

        return str(build_session_replay(_Log([{"role": "user", "content": text}]), "k"))

    def test_on_the_replay_redacts_the_credential(self):
        secret = _synth_labelled_secret()
        assert secret not in self._replay(secret)

    def test_off_the_replay_keeps_the_credential(self, monkeypatch):
        _seams(True, monkeypatch)
        secret = _synth_labelled_secret()
        assert secret in self._replay(secret)

    @pytest.mark.parametrize("off", [False, True])
    def test_the_exfil_url_pass_runs_either_way(self, monkeypatch, off):
        _seams(off, monkeypatch)
        url = _synth_exfil_url()
        assert url not in self._replay(f"see {url} now")

    def test_every_prompt_replay_credential_pass_opens_the_seam(self):
        """The session-start history blocks: each ``redact_credentials`` call sits in
        ``output_seam(ACP_PROMPT)``, and the exfil pass beside it does not."""
        for rel in ("context.py", "context_assembly/session_start.py"):
            tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
            inside, outside = _credential_calls(tree)
            assert inside, rel
            assert not outside, (rel, outside)


def _credential_calls(tree: ast.AST) -> tuple[list[int], list[int]]:
    """Lines of ``redact_credentials`` calls in ``build_session_replay`` and
    ``session_start`` history blocks, split by whether an ACP seam encloses them."""
    inside: list[int] = []
    outside: list[int] = []

    def seam_with(node: ast.AST) -> bool:
        if not isinstance(node, ast.With):
            return False
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "output_seam"
                and call.args
                and isinstance(call.args[0], ast.Attribute)
                and call.args[0].attr == "ACP_PROMPT"
            ):
                return True
        return False

    def walk(node: ast.AST, in_seam: bool, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scope = child.name
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, (ast.Name, ast.Attribute))
                and getattr(child.func, "attr", getattr(child.func, "id", ""))
                == "redact_credentials"
                and scope in ("build_session_replay", "build_session_context")
            ):
                (inside if in_seam else outside).append(child.lineno)
            walk(child, in_seam or seam_with(child), child_scope)

    walk(tree, False, "")
    return inside, outside


# -------------------------------------------------------------------------- the log seam


class TestLogSeam:
    def test_on_every_log_pass_runs(self):
        vault = _synth_vault_value()
        filt = SecretRedactionFilter([vault])
        out = filt.redact(f"{vault} {_synth_bearer()} {_synth_token_url()}")
        assert vault not in out
        assert _synth_bearer() not in out
        assert _synth_token_url() not in out

    def test_off_only_the_vault_secret_pass_runs(self, monkeypatch):
        _seams(True, monkeypatch)
        vault = _synth_vault_value()
        filt = SecretRedactionFilter([vault])
        out = filt.redact(f"{vault} {_synth_bearer()} {_synth_token_url()}")
        assert vault not in out and "[REDACTED]" in out
        assert _synth_bearer() in out
        assert _synth_token_url() in out

    def test_off_the_installed_record_factory_still_drops_vault_values(self, monkeypatch):
        """End to end through a real record: the factory's filter is the seam."""
        _seams(True, monkeypatch)
        vault = _synth_vault_value()
        filt = SecretRedactionFilter([vault])
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "v=%s", (vault,), None)
        assert vault not in filt.redact(record.getMessage())

    def test_log_shim_on_redacts_the_credential(self):
        from kiro_crew.platform.context import redact_log_via_context

        secret = _synth_labelled_secret()
        assert secret not in redact_log_via_context(secret)

    def test_log_shim_off_keeps_the_credential_and_still_drops_exfil_urls(self, monkeypatch):
        from kiro_crew.platform.context import redact_log_via_context

        _seams(True, monkeypatch)
        secret = _synth_labelled_secret()
        url = _synth_exfil_url()
        out = redact_log_via_context(f"{secret} {url}")
        assert secret in out
        assert url not in out


# --------------------------------------------------------------- what the off never reaches


class TestOutsideTheSeams:
    def test_a_refusal_gate_still_refuses_with_the_seams_off(self, monkeypatch):
        """``redact(x) != x`` gates open no seam, so they keep scanning."""
        from kiro_crew.autonudge_service.store_loader import _addressing_value_unsafe_why

        _seams(True, monkeypatch)
        assert _addressing_value_unsafe_why(_synth_labelled_secret()) == "is credential-shaped"

    def test_plain_redaction_outside_a_seam_is_unchanged_with_the_seams_off(self, monkeypatch):
        _seams(True, monkeypatch)
        secret = _synth_labelled_secret()
        assert security.redact(secret) != secret
        assert security.redact_credentials(secret)[0] != secret

    def test_the_files_view_switch_alone_opens_no_seam(self, _isolated, monkeypatch):
        redaction_switch.set_enabled(False, changed_at="2026-11-01T00:00:00+00:00")
        redaction_switch.publish_seams(redaction_switch.read_state())
        secret = _synth_labelled_secret()
        with redaction_seams.output_seam(redaction_seams.LOGS):
            assert security.redact_credentials(secret)[0] != secret


# ------------------------------------------------------------------------------ handlers


def _request(body=None):
    """A request shaped like a real DASHBOARD OWNER call (as test_credential_redaction_switch.py)."""
    req = MagicMock()
    req.path = "/api/security/credential-redaction"
    store = {"app": "", "user": "owner-1"}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = "owner-1"
    req.app = {"state": state}
    req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def _quiet_audit(monkeypatch):
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    calls: list[str] = []

    def fake(*, outcome, caller, detail="", critical=False, operation=None):
        calls.append(outcome)
        return True

    monkeypatch.setattr(handlers, "_audit", fake)
    return calls


class TestHandlers:
    @pytest.mark.asyncio
    async def test_put_seams_off_writes_publishes_and_audits(self, _isolated, _quiet_audit):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        req = _request(body={"seams_enabled": False})
        resp = await handlers.api_credential_redaction_put(req)
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload["seams_enabled"] is False and payload["seams_changed_at"]
        assert payload["enabled"] is True
        assert redaction_seams.seams_off() is True
        assert "seams_disabled" in _quiet_audit
        # The seams switch reaches no file body, so nothing is purged; owner tabs
        # still learn the new position, so none keeps showing the old one.
        (msg_type, payload), _ = req.app["state"].broadcast_ws_owners.call_args
        assert msg_type == "credential_redaction_settings_changed"
        assert payload["seams_enabled"] is False
        assert req.app["state"].broadcast_ws_owners.call_count == 1

        resp = await handlers.api_credential_redaction_put(_request(body={"seams_enabled": True}))
        assert resp.status == 200
        assert redaction_seams.seams_off() is False
        assert "seams_enabled" in _quiet_audit

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{}, {"enabled": False, "seams_enabled": False}, {"seams_enabled": "false"}, []],
    )
    async def test_put_refuses_anything_but_one_boolean_switch(self, _isolated, _quiet_audit, body):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        resp = await handlers.api_credential_redaction_put(_request(body=body))
        assert resp.status == 400
        assert not _isolated.exists()

    def test_boot_seed_publishes_the_stored_switch(self, _isolated, monkeypatch):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_seams_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        assert redaction_seams.seams_off() is False
        handlers.seed_pushed_switch()
        assert redaction_seams.seams_off() is True

    def test_a_policy_that_withdraws_the_opt_in_turns_the_seams_back_on(
        self, _isolated, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_seams_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        handlers.seed_pushed_switch()
        assert redaction_seams.seams_off() is True
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: False)
        loop = MagicMock()
        handlers.push_switch_moved_by_policy({"state": MagicMock()}, loop)
        assert redaction_seams.seams_off() is False

    @pytest.mark.asyncio
    async def test_a_files_view_on_after_a_withdrawn_opt_in_keeps_the_seams_on(
        self, _isolated, _quiet_audit, monkeypatch
    ):
        """A stored seams OFF the policy does not allow stays ON when the Files view is turned on.

        The write keeps the stored seams field, but what it publishes and returns is
        the switch in force, with the governance rule applied.
        """
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_seams_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: False)
        handlers.seed_pushed_switch()
        assert redaction_seams.seams_off() is False

        resp = await handlers.api_credential_redaction_put(_request(body={"enabled": True}))
        assert resp.status == 200
        assert redaction_seams.seams_off() is False
        assert json.loads(resp.text)["seams_enabled"] is True

    @pytest.mark.parametrize("write", ["enabled", "engine"])
    def test_every_setter_returns_the_switch_in_force(self, _isolated, monkeypatch, write):
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_seams_enabled(False, changed_at="t0")
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: False)
        if write == "enabled":
            state = redaction_switch.set_enabled(True, changed_at="t1")
        else:
            state = redaction_switch.set_engine(redaction_switch.ENGINE_LEGACY, changed_at="t1")
        assert state == redaction_switch.read_state()
        assert state.seams_enabled is True
        # The stored owner choice is kept: a later opt-in brings it back.
        assert json.loads(_isolated.read_text(encoding="utf-8"))["seams_enabled"] is False

    def test_a_policy_move_of_the_seams_alone_reaches_owner_tabs(self, _isolated, monkeypatch):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_seams_enabled(False, changed_at="2026-11-02T00:00:00+00:00")
        handlers.seed_pushed_switch()
        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: False)
        loop = MagicMock()
        handlers.push_switch_moved_by_policy({"state": MagicMock()}, loop)
        loop.call_soon_threadsafe.assert_called_once()


class TestLiveLogStream:
    """The dashboard's live log stream reaches non-owner sockets, so it is not a seam."""

    @staticmethod
    def _record(msg: str) -> logging.LogRecord:
        return logging.LogRecord("kiro_crew.test", logging.INFO, __file__, 1, msg, None, None)

    def test_the_ring_and_live_frames_are_redacted_with_the_seams_off(self, monkeypatch):
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=141)
        ring: collections.deque[str] = collections.deque(maxlen=5)
        handler = updates_streams._RingLogHandler(ring, 5)
        handler.emit(self._record(f"pushing with {token} and Authorization: Bearer {token}"))
        (entry,) = ring
        assert token not in entry

    def test_the_sse_queue_is_redacted_with_the_seams_off(self, monkeypatch):
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=142)
        queue: asyncio.Queue[str] = asyncio.Queue()
        updates_streams._QueueLogHandler(queue).emit(self._record(f"clone {token}"))
        assert token not in queue.get_nowait()

    def test_a_record_built_inside_a_bypassed_seam_is_still_redacted(self, monkeypatch):
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=143)
        ring: collections.deque[str] = collections.deque(maxlen=5)
        handler = updates_streams._RingLogHandler(ring, 5)
        with redaction_seams.output_seam(redaction_seams.ACP_PROMPT):
            handler.emit(self._record(f"replay {token}"))
        assert token not in ring[0]

    def test_a_record_made_while_the_seams_were_off_is_redacted_after_they_come_back(
        self, monkeypatch
    ):
        """The setting at emit time says nothing of how a record was built, so the
        stream redacts every line: here the record was made with the seams off (its
        shape passes skipped) and reaches the handlers once they are back on."""
        from kiro_crew.dashboard.handlers import updates_streams

        _seams(True, monkeypatch)
        token = github_classic(seed=144)
        record = self._record(f"push with Authorization: Bearer {token}")
        _seams(False, monkeypatch)
        ring: collections.deque[str] = collections.deque(maxlen=5)
        updates_streams._RingLogHandler(ring, 5).emit(record)
        queue: asyncio.Queue[str] = asyncio.Queue()
        updates_streams._QueueLogHandler(queue).emit(record)
        assert token not in ring[0]
        assert token not in queue.get_nowait()

    def test_a_line_the_redactor_logs_while_redacting_does_not_recurse(self, monkeypatch):
        """The fail-closed path logs a warning; the stream redacting that warning must
        not run the redactor again (on Windows' smaller stack the recursion crashed the
        process). The outer line still reaches the ring, with the credential gone."""
        from kiro_crew.dashboard.handlers import updates_streams
        from kiro_crew.security.redaction_structured import compat

        def broken(*_a, **_kw):
            raise ValueError("detectors broke")

        monkeypatch.setattr(compat, "_USE_RULE_TABLE", True)
        monkeypatch.setattr(compat, "_read", broken)
        monkeypatch.setattr(compat, "_count_fail_closed", lambda: None)
        calls: list[int] = []
        real = security.redact

        def counting(text):
            calls.append(1)
            return real(text)

        monkeypatch.setattr(security, "redact", counting)
        token = github_classic(seed=145)
        ring: collections.deque[str] = collections.deque(maxlen=50)
        handler = updates_streams._RingLogHandler(ring, 50)
        crew_logger = logging.getLogger("kiro_crew")
        crew_logger.addHandler(handler)
        try:
            crew_logger.warning("push with %s", token)
        finally:
            crew_logger.removeHandler(handler)
        assert len(calls) <= 3, len(calls)
        assert ring, "the outer line was dropped"
        assert all(token not in entry for entry in ring)

    def test_a_failed_redaction_is_reported_once_with_the_stream_attached(self, monkeypatch):
        """The fail-closed warning reaches the stream, whose redaction of that line fails
        too; the one failure is still logged and counted once."""
        from kiro_crew.dashboard.handlers import updates_streams
        from kiro_crew.security.redaction_structured import compat

        def broken(*_a, **_kw):
            raise ValueError("detectors broke")

        counted: list[int] = []
        monkeypatch.setattr(compat, "_USE_RULE_TABLE", True)
        monkeypatch.setattr(compat, "_read", broken)
        monkeypatch.setattr(compat, "_count_fail_closed", lambda: counted.append(1))
        queue: asyncio.Queue[str] = asyncio.Queue()
        handler = updates_streams._QueueLogHandler(queue)
        crew_logger = logging.getLogger("kiro_crew")
        crew_logger.addHandler(handler)
        try:
            security.redaction.redact_credentials("token " + github_classic(seed=146))
        finally:
            crew_logger.removeHandler(handler)
        assert counted == [1]
        assert queue.qsize() == 1


class TestPublishUnderTheStoreLock:
    """A refresher's read and publish cannot interleave with an owner write."""

    @pytest.mark.parametrize("entry", ["seed", "policy"])
    def test_the_seams_verdict_is_read_and_published_under_the_lock(
        self, _isolated, monkeypatch, entry
    ):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        held: list[bool] = []
        real = redaction_switch.publish_seams

        def spy(state):
            held.append(redaction_switch._STORE_LOCK._is_owned())
            real(state)

        monkeypatch.setattr(redaction_switch, "publish_seams", spy)
        if entry == "seed":
            handlers.seed_pushed_switch()
        else:
            handlers.push_switch_moved_by_policy({"state": MagicMock()}, MagicMock())
        assert held == [True]


class TestRefresherOwnerWriteRace:
    """An owner's ON landing while a refresh holds an older OFF read is the one published."""

    def test_an_owner_on_during_a_refresh_is_not_overwritten(self, _isolated, monkeypatch):
        import threading

        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        monkeypatch.setattr(redaction_switch, "owner_may_disable", lambda: True)
        redaction_switch.set_seams_enabled(False, changed_at="t0")
        handlers.seed_pushed_switch()
        assert redaction_seams.seams_off() is True

        read_done, go = threading.Event(), threading.Event()
        real_read = redaction_switch.read_state

        def slow_read():
            state = real_read()
            read_done.set()
            go.wait(2.0)
            return state

        monkeypatch.setattr(redaction_switch, "read_state", slow_read)

        def owner_put_on():
            # The PUT handler's write-and-publish, under the store's transaction lock.
            with redaction_switch.transaction():
                state = redaction_switch.set_seams_enabled(True, changed_at="t1")
                redaction_switch.publish_seams(state)

        refresher = threading.Thread(
            target=handlers.push_switch_moved_by_policy, args=({"state": MagicMock()}, MagicMock())
        )
        refresher.start()
        assert read_done.wait(2.0)
        owner = threading.Thread(target=owner_put_on)
        owner.start()
        # With the lock the owner blocks here, so the window's length cannot fail the test.
        owner.join(0.3)  # flake-ok: bounded window for an unlocked write to land first
        go.set()
        refresher.join(2.0)
        owner.join(2.0)
        assert not refresher.is_alive() and not owner.is_alive()
        assert redaction_seams.seams_off() is False
