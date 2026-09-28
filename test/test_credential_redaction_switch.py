"""The owner's credential-redaction switch (``security.redaction_switch``).

Credential-shaped material is SYNTHESIZED at runtime, for the reason
``test_file_delivery_consent.py`` gives: a diff carrying a working token string
reads as an exfiltration recipe to a review provider, while the scanner sees the
same bytes either way.
"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import security
from kiro_crew.security import redaction_switch


def _synth_aws_key() -> str:
    prefix = "A" + "KIA"
    return prefix + hashlib.sha256(b"kc-redaction-switch").hexdigest().upper()[:16]


def _synth_token_url() -> str:
    # A ``?token=`` URL: the pass-4 shape that motivated the switch.
    return "https://approve.example.test/auth?token=" + hashlib.sha256(b"kc-rs").hexdigest()


def _synth_exfil_url() -> str:
    # A long high-entropy query to a non-exempt host trips the exfil pass.
    blob = hashlib.sha256(b"kc-exfil").hexdigest() * 4
    return f"https://collector.example.test/x?d={blob}"


@pytest.fixture(autouse=True)
def _isolated_switch(tmp_path, monkeypatch):
    """Point the switch at a tmp keystone and drop any cached verdict."""
    store = tmp_path / "credential_redaction.json"
    monkeypatch.setattr(redaction_switch, "_path", lambda: store, raising=True)
    yield store


# ------------------------------------------------------------- the material trips


class TestSynthesizedMaterialActuallyTrips:
    def test_aws_key_is_redacted_by_default(self):
        key = _synth_aws_key()
        assert security.redact_credentials(key)[0] != key

    def test_token_url_value_is_redacted_by_default(self):
        url = _synth_token_url()
        out = security.redact_credentials(url)[0]
        assert out != url
        assert "token=" in out


# ------------------------------------------------------------------ the default


class TestDefaultIsOn:
    def test_absent_record_reads_enabled(self, _isolated_switch):
        assert not _isolated_switch.exists()
        assert redaction_switch.read_state().enabled is True
        assert redaction_switch.read_state().enabled is True

    def test_non_utf8_bytes_read_enabled(self, _isolated_switch):
        _isolated_switch.write_bytes(b'{"enabled": false, "x": "\xff\xfe"}')
        assert redaction_switch.read_state().enabled is True
        assert redaction_switch.read_state().enabled is True

    @pytest.mark.parametrize(
        "raw",
        ["", "not json", "[false]", "null", '{"enabled": "false"}', '{"enabled": 0}', "{}"],
    )
    def test_only_a_literal_false_disables(self, _isolated_switch, raw):
        _isolated_switch.write_text(raw, encoding="utf-8")
        assert redaction_switch.read_state().enabled is True
        assert redaction_switch.read_state().enabled is True

    def test_an_unreadable_record_reads_enabled(self, _isolated_switch):
        _isolated_switch.write_text('{"enabled": false}', encoding="utf-8")
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs a POSIX permission denial")
        _isolated_switch.chmod(0)
        try:
            assert redaction_switch.read_state().enabled is True
        finally:
            _isolated_switch.chmod(0o600)


# -------------------------------------------------------------- what OFF does


def _off() -> None:
    redaction_switch.set_enabled(False, changed_at="2026-09-24T23:00:00+00:00")


def _owner_view():
    """The scope as the seam enters it: only when the keystone says OFF."""
    from contextlib import nullcontext

    return nullcontext() if redaction_switch.read_state().enabled else redaction_switch.owner_view()


class TestSwitchedOffInsideOwnerView:
    """The switch acts ONLY inside an explicit ``owner_view`` scope."""

    def test_set_enabled_false_records_the_position(self, _isolated_switch):
        state = redaction_switch.set_enabled(False, changed_at="2026-09-24T23:00:00+00:00")
        assert state.enabled is False
        assert json.loads(_isolated_switch.read_text())["enabled"] is False

    def test_owner_view_passes_credentials_through_when_off(self):
        _off()
        key, url = _synth_aws_key(), _synth_token_url()
        with _owner_view():
            assert security.redact_credentials(key) == (key, [])
            assert security.redact_credentials(url) == (url, [])

    def test_owner_view_still_redacts_when_on(self):
        key = _synth_aws_key()
        with _owner_view():
            assert security.redact_credentials(key)[0] != key

    def test_set_enabled_true_restores_redaction_in_owner_view(self):
        _off()
        key = _synth_aws_key()
        with _owner_view():
            assert security.redact_credentials(key)[0] == key
        redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
        with _owner_view():
            assert security.redact_credentials(key)[0] != key

    def test_exfiltration_url_redaction_is_untouched_inside_owner_view(self):
        _off()
        url = _synth_exfil_url()
        with _owner_view():
            assert security.redact_exfiltration_urls(url)[0] != url
            assert security.redact(url) != url

    def test_the_verdict_is_the_one_the_scope_was_entered_with(self):
        """One render, one verdict: a flip mid-render is seen by the NEXT request."""
        key = _synth_aws_key()
        with _owner_view():  # entered ON
            assert redaction_switch.credential_pass_bypassed() is False
            _off()  # the owner flips the switch while this render is in flight
            assert redaction_switch.credential_pass_bypassed() is False
            assert security.redact_credentials(key)[0] != key
        with _owner_view():  # entered OFF
            assert redaction_switch.credential_pass_bypassed() is True
            redaction_switch.set_enabled(True, changed_at="2026-09-24T23:01:00+00:00")
            assert security.redact_credentials(key)[0] == key
        with _owner_view():
            assert redaction_switch.credential_pass_bypassed() is False

    def test_scope_is_task_local_and_restored_on_exit(self):
        _off()
        key = _synth_aws_key()
        assert redaction_switch.credential_pass_bypassed() is False
        with _owner_view():
            assert redaction_switch.credential_pass_bypassed() is True
        assert redaction_switch.credential_pass_bypassed() is False
        assert security.redact_credentials(key)[0] != key

    @pytest.mark.asyncio
    async def test_scope_does_not_leak_across_tasks(self):
        """An owner-view render on one task must not switch off a sibling task's post."""
        import asyncio

        _off()
        key = _synth_aws_key()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def owner_task():
            with _owner_view():
                entered.set()
                await release.wait()
                return security.redact_credentials(key)[0]

        async def sibling_task():
            await entered.wait()
            out = security.redact_credentials(key)[0]
            release.set()
            return out

        owner_out, sibling_out = await asyncio.gather(owner_task(), sibling_task())
        assert owner_out == key
        assert sibling_out != key


class TestSwitchedOffOutsideOwnerView:
    """Every surface that never opens the scope keeps the full pass."""

    def test_bare_redact_credentials_is_unconditional(self):
        _off()
        key = _synth_aws_key()
        assert security.redact_credentials(key)[0] != key
        assert security.redact(key) != key
        assert security.redact_with_findings(key)[0] != key

    def test_stream_redactor_default_is_unconditional(self):
        _off()
        key = _synth_aws_key()
        sr = security.StreamRedactor()
        out = sr.feed(key + " ") + sr.flush()
        assert key not in out

    def test_request_blocking_predicate_is_untouched(self):
        _off()
        assert security._contains_fixed_credential(_synth_aws_key()) is True

    def test_diagnostics_bundle_scrubs_regardless(self):
        """The bundle goes to a public issue; it never opens the owner scope."""
        from kiro_crew import diagnostics

        _off()
        clean, count = diagnostics._scrub(_synth_aws_key())
        assert clean != _synth_aws_key()
        assert count >= 1

    def test_channel_renderers_never_open_the_scope(self):
        """Slack, Webex and the shared messaging renderer redact at their sinks with
        the unconditional pass; none of them may import the owner-view scope."""
        import inspect

        from kiro_crew.messaging import renderer as messaging_renderer
        from kiro_crew.slack import renderer as slack_renderer
        from kiro_crew.webex import renderer as webex_renderer

        for mod in (slack_renderer, webex_renderer, messaging_renderer):
            src = inspect.getsource(mod)
            assert "owner_view" not in src, mod.__name__

    def test_only_the_named_owner_seams_open_the_scope(self):
        """A grep-pinned allowlist: adding an owner-view seam is a review decision."""
        import re
        from pathlib import Path

        pkg = Path(security.__file__).resolve().parents[1]  # src/kiro_crew
        needle = re.compile(r"owner_view(?:_scope)?\(|redact_owner_view_via_context")
        files = {
            "src/kiro_crew/" + f.relative_to(pkg).as_posix()
            for f in pkg.rglob("*.py")
            if "/tests/" not in f.as_posix() and needle.search(f.read_text(encoding="utf-8"))
        }
        assert files == {
            "src/kiro_crew/dashboard/handlers/files.py",  # api_file_read + api_file_diff, owner only
            "src/kiro_crew/platform/context.py",
            "src/kiro_crew/security/redaction.py",
            "src/kiro_crew/security/redaction_switch.py",
        }, files

    def test_the_chat_surface_never_opens_the_scope(self):
        """Chat is OUT of scope, and deliberately so: ``_flush_segment`` redacts the
        assistant text BEFORE ``slot.append``, so the transcript holds the redacted
        bytes and no display-time scope could restore them; the live SSE/WS streams
        fan one chunk to every connected client besides. An owner-view seam there
        would advertise a raw transcript it cannot deliver."""
        import inspect

        from kiro_crew.dashboard import chat_runner, chat_utils

        assert "owner_view" not in inspect.getsource(chat_runner)
        assert "owner_view" not in inspect.getsource(chat_utils)

    def test_file_viewer_opens_the_scope_only_for_the_owner(self):
        """The file viewer is the ONE surface, fed by two handlers that MUST agree:
        ``api_file_read`` (the buffer) and ``api_file_diff`` (the ``original`` the
        buffer is compared against). Both take the verdict from one helper: owner
        check first, verdict read off-loop, per request. ``api_file_watch`` is not
        a seam: it also serves artifact live reload and neither consumer renders
        its frame."""
        import inspect

        from kiro_crew.dashboard.handlers import files as files_handlers

        helper = inspect.getsource(files_handlers._owner_view_bypasses_credential_pass)
        assert "owner_view_for_request(request)" in helper
        assert "asyncio.to_thread(redaction_switch.read_state)" in helper
        for fn in (files_handlers.api_file_read, files_handlers.api_file_diff):
            src = inspect.getsource(fn)
            assert "_owner_view_bypasses_credential_pass(request)" in src
            assert "redact_owner_view_via_context(" in src
        watch = inspect.getsource(files_handlers.api_file_watch)
        assert "redact_owner_view_via_context" not in watch
        assert "owner_view" not in watch

    def test_file_admission_gates_use_the_unconditional_pass(self):
        """The outbox flagged-file check and the upload gates decide whether a
        file may LEAVE; they are not owner-view renders."""
        import inspect

        from kiro_crew.dashboard.handlers import files as files_handlers

        assert "redact_owner_view_via_context" not in inspect.getsource(
            files_handlers.api_outbox_download
        )
        assert "redact_owner_view_via_context" not in inspect.getsource(
            files_handlers._gate_upload_file
        )
        assert "redact_owner_view_via_context" in inspect.getsource(files_handlers.api_file_read)
        assert "redact_owner_view_via_context" in inspect.getsource(files_handlers.api_file_diff)

    @pytest.mark.asyncio
    async def test_read_and_diff_take_the_same_verdict(self, monkeypatch, _isolated_switch):
        """A non-owner never bypasses; the owner bypasses exactly when the switch is
        OFF -- the same answer for both handlers, from the one helper."""
        from kiro_crew.dashboard.handlers import files as files_handlers
        from kiro_crew.dashboard.handlers import source_providers
        from kiro_crew.security import redaction_switch

        monkeypatch.setattr(source_providers, "owner_view_for_request", lambda req: req["owner"])
        assert await files_handlers._owner_view_bypasses_credential_pass({"owner": False}) is False
        assert await files_handlers._owner_view_bypasses_credential_pass({"owner": True}) is False
        redaction_switch.set_enabled(False, changed_at="2026-01-01T00:00:00+00:00")
        assert await files_handlers._owner_view_bypasses_credential_pass({"owner": True}) is True
        assert await files_handlers._owner_view_bypasses_credential_pass({"owner": False}) is False


# ---------------------------------------------------------------- the cache


# ------------------------------------------------ the motivating link, end to end


def _synth_approval_link() -> str:
    """The reported shape: a base64url JSON blob as a single-segment ``?token=``.

    Synthesized rather than pasted: a decoded ``{"versionId":1,"userId":"x"}``
    re-encoded at runtime, so the bytes are token-shaped without being a token.
    """
    import base64

    blob = base64.urlsafe_b64encode(b'{"versionId":1,"userId":"someone"}').decode().rstrip("=")
    return f"https://auth.approvals.example.test/auth?token={blob}"


class TestTheMotivatingLinkThroughTheFileViewerPath:
    """What the switch does, and does not, for the report that motivated it.

    ``redact_owner_view_via_context`` is what ``api_file_read`` calls, so this is
    the file viewer's exact path minus the HTTP layer.
    """

    def test_with_the_switch_on_the_token_value_is_redacted(self):
        from kiro_crew.platform.context import redact_via_context

        link = _synth_approval_link()
        out = redact_via_context(link)
        assert "token=" in out or "REDACTED" in out
        assert link.split("token=")[1] not in out

    def test_in_the_public_build_the_exfil_pass_still_hides_the_link_when_off(self):
        """Stated limit: the switch reaches the credential pass only. A long
        base64 query to a non-exempt host is independently classified as
        exfiltration and replaced whole, switch or no switch."""
        from kiro_crew.platform.context import redact_owner_view_via_context

        link = _synth_approval_link()
        out = redact_owner_view_via_context(link)
        assert link.split("token=")[1] not in out
        assert "REDACTED" in out

    def test_with_a_companion_host_exemption_the_link_renders_raw_when_off(self, monkeypatch):
        """The internal companion exempts its approval hosts from the exfil
        heuristics; there, switching off renders the reported link as written."""
        from kiro_crew.platform.context import redact_owner_view_via_context, redact_via_context
        from kiro_crew.security import exfil

        link = _synth_approval_link()
        host = link.split("/")[2]
        monkeypatch.setattr(exfil, "_exfil_exempt_hosts", lambda: frozenset({host}))
        # ON: exempt host clears the exfil pass, the credential pass still redacts.
        assert link.split("token=")[1] not in redact_via_context(link)
        assert redact_owner_view_via_context(link) == link
        # And the same bytes on a NON-owner surface stay redacted.
        assert security.redact(link) != link


# ------------------------------------------------------------- the keystone


class TestTheLeafIsAKeystone:
    def test_the_path_is_the_named_leaf_under_the_data_home(self, monkeypatch, tmp_path):
        from kiro_crew.config import loader

        monkeypatch.setattr(loader, "config_dir", lambda: tmp_path)
        assert loader.credential_redaction_path() == tmp_path / "credential_redaction.json"

    def test_fenced_on_the_agent_file_tool_path(self):
        from kiro_crew.security.paths import _CREW_SECRET_LEAVES, is_sensitive_path

        assert "credential_redaction.json" in _CREW_SECRET_LEAVES
        assert is_sensitive_path("~/.kiro/crew/credential_redaction.json") is True

    def test_mounted_read_only_and_pre_created_in_the_sandbox(self):
        from kiro_crew import sandbox

        assert "credential_redaction.json" in sandbox._CREW_READONLY_LEAVES
        assert "credential_redaction.json" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
        assert "credential_redaction.json" in sandbox._CREW_CHILD_WITHHELD_LEAVES

    def test_config_json_carries_no_switch(self):
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert not any("redaction" in key for key in _EDITABLE_CONFIG)

    def test_not_re_exported_on_the_security_facade(self):
        """The scope is reached by its owning module only: no consumer needs the facade."""
        from kiro_crew.security._exports import EXPORTED_NAMES

        assert "owner_view" not in EXPORTED_NAMES
        assert "credential_redaction_enabled" not in EXPORTED_NAMES


# --------------------------------------------------------------- the handler


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None):
    """A request shaped like a real DASHBOARD OWNER call (see test_decisions_consent.py)."""
    req = MagicMock()
    req.path = "/api/security/credential-redaction"
    store = {"app": app, "user": user}
    req.get = lambda key, default=None: store.get(key, default)
    req.__contains__ = lambda _self, key: key in store
    req.__getitem__ = lambda _self, key: store[key]
    state = MagicMock()
    state.owner_id = owner
    req.app = {"state": state}
    if isinstance(body, Exception):
        req.json = AsyncMock(side_effect=body)
    else:
        req.json = AsyncMock(return_value=body if body is not None else {})
    return req


@pytest.fixture
def _quiet_audit(monkeypatch):
    from kiro_crew.dashboard.handlers import credential_redaction as handlers

    class _Events(list):
        ops: list[str] = []

    events = _Events()
    events.ops = []

    def _record(*, outcome, caller, detail="", critical=False, operation=None):
        events.append((outcome, caller, critical))
        events.ops.append(operation or outcome)
        return True

    monkeypatch.setattr(handlers, "_audit", _record, raising=True)
    return events


class TestHandlers:
    @pytest.mark.asyncio
    async def test_owner_get_is_audited_as_an_allowed_read(self, _quiet_audit):
        """Refusals AND owner reads are in the SEL: the history says who read the
        switch. Non-critical -- a read never fails on a briefly unavailable log."""
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        resp = await handlers.api_credential_redaction_get(_request())
        assert resp.status == 200
        assert ("allowed", "owner-1", False) in _quiet_audit
        assert "read" in _quiet_audit.ops

    @pytest.mark.asyncio
    async def test_get_reports_the_default(self, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_get

        resp = await api_credential_redaction_get(_request())
        assert resp.status == 200
        assert json.loads(resp.text) == {"enabled": True, "changed_at": ""}

    @pytest.mark.asyncio
    async def test_put_false_records_and_audits(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(body={"enabled": False}))
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert payload["enabled"] is False and payload["changed_at"]
        assert json.loads(_isolated_switch.read_text())["enabled"] is False
        assert (
            "disabled",
            "owner-1",
            True,
        ) in _quiet_audit  # synchronous, fail-loud write; REAL subject
        with _owner_view():
            assert security.redact_credentials(_synth_aws_key())[0] == _synth_aws_key()
        assert security.redact_credentials(_synth_aws_key())[0] != _synth_aws_key()

    @pytest.mark.asyncio
    async def test_put_pushes_the_change_to_every_owner_dashboard(
        self, _isolated_switch, _quiet_audit
    ):
        """A second browser tab holds the same raw bodies; only a push reaches it.
        Owner sockets only (``broadcast_ws_owners``), never the app-scoped fan-out."""
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        req = _request(body={"enabled": False})
        resp = await handlers.api_credential_redaction_put(req)
        assert resp.status == 200
        state = req.app["state"]
        state.broadcast_ws_owners.assert_called_once()
        msg_type, payload = state.broadcast_ws_owners.call_args.args
        assert msg_type == "credential_redaction_changed"
        assert payload["enabled"] is False and payload["changed_at"]
        state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_lost_push_does_not_fail_the_put(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        req = _request(body={"enabled": False})
        req.app["state"].broadcast_ws_owners.side_effect = RuntimeError("hub gone")
        resp = await handlers.api_credential_redaction_put(req)
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_put_true_restores(self, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        await api_credential_redaction_put(_request(body={"enabled": False}))
        resp = await api_credential_redaction_put(_request(body={"enabled": True}))
        assert json.loads(resp.text)["enabled"] is True
        assert ("enabled", "owner-1", True) in _quiet_audit
        with _owner_view():
            assert security.redact_credentials(_synth_aws_key())[0] != _synth_aws_key()

    def test_write_and_audit_are_one_unit_on_the_worker_thread(self):
        """A cancelled handler cannot skip the audit: both run in the same closure,
        the record is written BEFORE the switch, and it is a critical (synchronous)
        write, because the default SEL path only enqueues."""
        import inspect

        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        src = inspect.getsource(api_credential_redaction_put)
        closure = src[src.index("def _write_and_audit") : src.index("try:\n        state = await")]
        assert closure.index('outcome="enabled" if enabled else "disabled"') < closure.index(
            "set_enabled("
        )
        assert closure.count("critical=True") == 2
        assert "asyncio.to_thread(_write_and_audit)" in src
        # The whole pair runs under the store lock, so two interleaving PUTs
        # cannot leave the audit trail contradicting the switch.
        assert closure.index("with redaction_switch.transaction():") < closure.index(
            "recorded = _audit("
        )

    @pytest.mark.asyncio
    async def test_audit_and_write_hold_the_store_lock_together(
        self, monkeypatch, _isolated_switch
    ):
        """While the audit record is being written the store lock is HELD, so a
        concurrent set_enabled must wait: the SEL order equals the write order."""
        import threading

        from kiro_crew.dashboard.handlers import credential_redaction as handlers
        from kiro_crew.security import redaction_switch

        held_during_audit: list[bool] = []

        def spy_audit(**kw):
            # A non-blocking acquire from ANOTHER thread fails iff the handler
            # thread holds the (re-entrant) lock right now.
            probe: list[bool] = []

            def try_lock():
                got = redaction_switch._STORE_LOCK.acquire(blocking=False)
                probe.append(got)
                if got:
                    redaction_switch._STORE_LOCK.release()

            t = threading.Thread(target=try_lock)
            t.start()
            t.join()
            held_during_audit.append(not probe[0])
            return True

        monkeypatch.setattr(handlers, "_audit", spy_audit, raising=True)
        resp = await handlers.api_credential_redaction_put(_request(body={"enabled": False}))
        assert resp.status == 200
        assert held_during_audit == [True]
        assert redaction_switch.read_state().enabled is False

    @pytest.mark.asyncio
    async def test_a_disable_whose_audit_cannot_be_written_does_not_happen(
        self, _isolated_switch, monkeypatch
    ):
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        seen: list[dict] = []

        def failing(**kw):
            seen.append(kw)
            return False

        monkeypatch.setattr(handlers, "_audit", failing, raising=True)
        resp = await handlers.api_credential_redaction_put(_request(body={"enabled": False}))
        assert resp.status == 503
        # The record the refusal hinged on was requested as a SYNCHRONOUS write:
        # an enqueued record reports success before the writer has tried.
        assert seen and seen[0]["critical"] is True
        assert json.loads(resp.text)["code"] == "credential_redaction_audit_unavailable"
        assert not _isolated_switch.exists()
        assert redaction_switch.read_state().enabled is True

    @pytest.mark.asyncio
    async def test_an_enable_proceeds_even_when_the_audit_cannot_be_written(
        self, _isolated_switch, monkeypatch
    ):
        """Enabling is the fail-safe direction; an unavailable log must not pin OFF."""
        from kiro_crew.dashboard.handlers import credential_redaction as handlers

        _off()
        monkeypatch.setattr(handlers, "_audit", lambda **kw: False, raising=True)
        resp = await handlers.api_credential_redaction_put(_request(body={"enabled": True}))
        assert resp.status == 200
        assert redaction_switch.read_state().enabled is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{"enabled": "false"}, {"enabled": 0}, {}, [False], None])
    async def test_put_refuses_a_non_boolean(self, _isolated_switch, _quiet_audit, body):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(body=body))
        assert resp.status == 400
        assert not _isolated_switch.exists()

    @pytest.mark.asyncio
    async def test_put_refuses_invalid_json(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(body=ValueError("bad json")))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_non_owner_is_refused_on_both_verbs(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import (
            api_credential_redaction_get,
            api_credential_redaction_put,
        )

        get = await api_credential_redaction_get(_request(user="someone-else"))
        put = await api_credential_redaction_put(
            _request(user="someone-else", body={"enabled": False})
        )
        assert get.status == 403 and put.status == 403
        assert not _isolated_switch.exists()
        # The refused SUBJECT is recorded, never a constant.
        assert ("denied", "someone-else", False) in _quiet_audit

    @pytest.mark.asyncio
    async def test_an_app_token_is_refused(self, _isolated_switch, _quiet_audit):
        from kiro_crew.dashboard.handlers.credential_redaction import api_credential_redaction_put

        resp = await api_credential_redaction_put(_request(app="some-app", body={"enabled": False}))
        assert resp.status == 403
        assert not _isolated_switch.exists()

    def test_routes_are_registered(self):
        import inspect

        from kiro_crew.dashboard.routes import system

        src = inspect.getsource(system)
        assert (
            'add_get("/api/security/credential-redaction", handlers.api_credential_redaction_get)'
            in src
        )
        assert (
            'add_put("/api/security/credential-redaction", handlers.api_credential_redaction_put)'
            in src
        )
