"""The decision-seam consent keystone: the file, its fences, and its one writer.

Consent to send conversation state to Jev is an authorization, so it lives on
``decisions_consent.json`` -- a KEYSTONE leaf the agent can neither read nor
write -- and never in ``config.json``. Three things are pinned here:

* the leaf is fenced on every layer the other keystones are fenced on: the
  agent file-tool gate (``_CREW_SECRET_LEAVES``), the sandbox read-only mount,
  and the absent-ceiling pre-create list;
* every read fails soft to NOT CONSENTED and only a literal ``true`` consents --
  and only for the endpoint it was recorded for, because ``provider.endpoint`` is
  in the agent-writable ``config.json`` too;
* the dashboard handler is owner-only on read and write, validates strictly,
  audits, and never clobbers a corrupt file.

:class:`TestCapabilityCeiling` pins the FLEET's switch above the owner's --
``capabilities.decisions`` -- at both of its chokepoints and on the dashboard
config read that hides the card.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import StreamReader, web
from dashboard_owner_helpers import NoConfiguredOwner

from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT as DEFAULT_ENDPOINT
from kiro_crew.decisions import consent

CUSTOM = "https://proxy.example/v1/systemone"


@pytest.fixture
def keystone(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    return path


@pytest.fixture
def configured(monkeypatch):
    """Pin the endpoint the live config names; returns a setter."""
    from kiro_crew.decisions import gate

    def _set(endpoint):
        monkeypatch.setattr(gate, "configured_endpoint", lambda config=None: endpoint)

    _set(DEFAULT_ENDPOINT)
    return _set


# ---------------------------------------------------------------------------
# Fences
# ---------------------------------------------------------------------------


class TestTheLogIsSealedToo:
    """Sealing the GRANT and leaving the RECORD writable would be half a control.

    An agent that cannot switch the seam on can still append a ``kind="feedback"``
    line -- the exact shape ``log.build_feedback_row`` produces -- and the owner's
    summary would count a verdict nobody gave. The directory is read-only in every
    sandbox mode for that reason, and pre-created so an install that has never
    sampled a decision does not leave the name creatable.
    """

    def test_the_log_directory_is_mounted_read_only(self):
        from kiro_crew import sandbox

        assert "decisions" in sandbox._CREW_READONLY_LEAVES

    def test_the_log_directory_is_fenced_on_the_agent_file_tool_path_too(self):
        """The sandbox layer covers a shell; this one covers the file-edit tool.

        The sandbox is one enforcement point and not every host has it, while the file
        tool is present on all of them -- so the sandbox mount alone leaves the forged
        verdict reachable wherever the OS sandbox is absent or off. WRITE-protected
        rather than sensitive: reading the rows is the point of recording them, and the
        gateway's own writer opens the file directly without passing this gate.
        """
        from kiro_crew.security.paths import write_protected_home_paths

        entries = write_protected_home_paths()
        assert any(entry.endswith("/decisions") for entry in entries), entries

    def test_the_log_directory_stays_readable_through_the_tools(self):
        """A record nobody may read is a record nobody can be shown."""
        from kiro_crew.security.paths import is_sensitive_path

        assert is_sensitive_path("~/.kiro/crew/decisions") is False
        assert is_sensitive_path("~/.kiro/crew/decisions/decisions-20260919.jsonl") is False

    def test_it_is_pre_created_so_an_absent_directory_is_not_skipped(self):
        """A mount cannot target a path that does not exist, and an install that has
        never sampled a decision has no log directory -- which is precisely the
        state in which the name is creatable from inside the sandbox."""
        from kiro_crew import sandbox

        assert "decisions" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES

    def test_it_is_pre_created_as_a_DIRECTORY_not_a_file(self):
        """A regular file at that name is worse than an absent one: the gateway's
        own log directory could not then be created there."""
        from kiro_crew import sandbox

        assert "decisions" not in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES

    def test_the_seal_is_by_NAME_so_a_planted_symlink_cannot_stand_in(self):
        """A resolving symlink is followed by the mount, which would seal a target
        the governed party chose and leave the lexical name replaceable -- so the
        anti-forgery seal would be satisfied by a link rather than by the log."""
        from kiro_crew import sandbox

        assert "decisions" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES

    def test_the_grant_is_sealed_as_well(self):
        """Both halves, so neither can be read as sufficient on its own."""
        from kiro_crew import sandbox

        assert "decisions_consent.json" in sandbox._CREW_READONLY_LEAVES


class TestTheLeafIsAKeystone:
    def test_the_path_is_the_named_leaf_under_the_data_home(self, keystone):
        assert consent.consent_path() == keystone
        assert keystone.name == "decisions_consent.json"

    def test_fenced_on_the_agent_file_tool_path(self):
        from kiro_crew.security.paths import _CREW_SECRET_LEAVES, is_sensitive_path

        assert "decisions_consent.json" in _CREW_SECRET_LEAVES
        assert is_sensitive_path("~/.kiro/crew/decisions_consent.json") is True

    def test_mounted_read_only_and_pre_created_in_the_sandbox(self):
        from kiro_crew import sandbox

        assert "decisions_consent.json" in sandbox._CREW_READONLY_LEAVES
        assert "decisions_consent.json" in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES

    def test_config_json_carries_no_switch(self):
        """The other half of the design: nothing agent-writable stands in for it."""
        from dataclasses import fields

        from kiro_crew.config.sections import DecisionsConfig
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert "enabled" not in {f.name for f in fields(DecisionsConfig)}
        assert "decisions.enabled" not in _EDITABLE_CONFIG


# ---------------------------------------------------------------------------
# Reads fail soft; only a literal true consents
# ---------------------------------------------------------------------------


class TestRead:
    def test_absent_is_not_consented(self, keystone):
        assert consent.load_state() == {}
        assert consent.is_enabled() is False

    def test_a_literal_true_consents(self, keystone):
        keystone.write_text('{"enabled": true}', encoding="utf-8")
        assert consent.is_enabled() is True

    @pytest.mark.parametrize(
        "raw", ['{"enabled": "true"}', '{"enabled": 1}', '{"enabled": false}', "{}"]
    )
    def test_nothing_else_consents(self, keystone, raw):
        keystone.write_text(raw, encoding="utf-8")
        assert consent.is_enabled() is False

    @pytest.mark.parametrize("raw", ["", "not json", "[true]", "null", '"enabled"'])
    def test_a_corrupt_or_non_object_file_is_not_consented(self, keystone, raw):
        keystone.write_text(raw, encoding="utf-8")
        assert consent.load_state() == {}
        assert consent.is_enabled() is False

    def test_permits_only_the_recorded_endpoint(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        assert consent.permits(DEFAULT_ENDPOINT) is True
        assert consent.permits(f"  {DEFAULT_ENDPOINT} ") is True, "whitespace is not a new address"
        assert consent.permits(CUSTOM) is False
        assert consent.permits("") is False

    @pytest.mark.parametrize(
        "raw",
        [
            '{"enabled": true}',
            '{"enabled": true, "endpoint": ""}',
            '{"enabled": true, "endpoint": 7}',
        ],
    )
    def test_a_flag_without_a_destination_permits_nothing(self, keystone, raw):
        """The dashboard writer always records where; a keystone that does not say
        never came from it, and must not send anywhere."""
        keystone.write_text(raw, encoding="utf-8")
        assert consent.is_enabled() is True
        assert consent.permits(DEFAULT_ENDPOINT) is False

    def test_disabled_permits_nothing_even_for_the_recorded_endpoint(self, keystone):
        keystone.write_text(
            json.dumps({"enabled": False, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        assert consent.permits(DEFAULT_ENDPOINT) is False

    def test_an_unreadable_file_is_not_consented(self, keystone):
        keystone.write_text('{"enabled": true}', encoding="utf-8")
        if os.name != "posix" or os.geteuid() == 0:
            pytest.skip("needs a POSIX permission denial")
        keystone.chmod(0)
        try:
            assert consent.is_enabled() is False
        finally:
            keystone.chmod(0o600)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


class TestWrite:
    def test_writes_owner_only_and_reads_back_bound_to_the_endpoint(self, keystone):
        assert consent.save_enabled(True, endpoint=CUSTOM) == {
            "enabled": True,
            "endpoint": CUSTOM,
            "history_budget_chars": 0,
            # Enabling alone consents to no tool arguments: the default is the
            # narrowest scope, so a caller that does not mention them grants none.
            "tool_args": False,
            "compaction": False,
            "memory_text": False,
        }
        assert consent.permits(CUSTOM) is True
        assert consent.permits(DEFAULT_ENDPOINT) is False
        if os.name == "posix":
            assert stat.S_IMODE(keystone.stat().st_mode) == 0o600

    def test_disabling_clears_the_destination(self, keystone):
        """So a later re-enable cannot inherit a stale address."""
        consent.save_enabled(True, endpoint=CUSTOM)
        assert consent.save_enabled(False, endpoint=DEFAULT_ENDPOINT) == {
            "enabled": False,
            "endpoint": "",
            "history_budget_chars": 0,
            # Cleared on the same terms as the endpoint and the ceiling, so a later
            # re-enable cannot inherit a tool-argument scope nobody re-reviewed.
            "tool_args": False,
            "compaction": False,
            "memory_text": False,
        }
        assert consent.permits(CUSTOM) is False

    def test_keeps_an_operator_key_it_does_not_know(self, keystone):
        keystone.write_text('{"note": "kept", "enabled": false}', encoding="utf-8")
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        assert json.loads(keystone.read_text()) == {
            "note": "kept",
            "enabled": True,
            "endpoint": DEFAULT_ENDPOINT,
            "history_budget_chars": 0,
            "tool_args": False,
            "compaction": False,
            "memory_text": False,
        }

    def test_records_the_history_ceiling_it_was_given(self, keystone):
        """The prior-conversation budget the owner reviewed, on the sealed record."""
        state = consent.save_enabled(True, endpoint=CUSTOM, history_budget_chars=2000)
        assert state["history_budget_chars"] == 2000
        assert consent.consented_history_budget() == 2000

    def test_omitting_the_ceiling_consents_to_no_prior_turns(self, keystone):
        consent.save_enabled(True, endpoint=CUSTOM)
        assert consent.consented_history_budget() == 0

    def test_disabling_clears_the_ceiling_even_when_one_is_passed(self, keystone):
        """So a later re-enable cannot inherit a budget nobody re-reviewed.

        The budget is passed on the DISABLE call too, because that is the only
        version of this test that fails when the clearing is removed: disabling
        with the default of 0 would write 0 either way.
        """
        consent.save_enabled(True, endpoint=CUSTOM, history_budget_chars=2000)
        state = consent.save_enabled(False, endpoint=CUSTOM, history_budget_chars=2000)
        assert state["history_budget_chars"] == 0
        assert consent.consented_history_budget() == 0

    @pytest.mark.parametrize("bad", [True, False, "2000", 2000.5, None, -1, [2000]])
    def test_a_ceiling_that_is_not_a_whole_non_negative_number_is_refused(self, keystone, bad):
        """Refused rather than rounded: it is written into a security record."""
        with pytest.raises(ValueError):
            consent.save_enabled(True, endpoint=CUSTOM, history_budget_chars=bad)

    @pytest.mark.parametrize(
        "recorded,expected",
        [(2000, 2000), (0, 0), (-5, 0), (True, 0), ("2000", 0), (2000.5, 0), (None, 0)],
    )
    def test_only_a_whole_non_negative_number_reads_as_a_ceiling(
        self, keystone, recorded, expected
    ):
        """A hand-edited keystone cannot widen egress by being creative."""
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": CUSTOM, "history_budget_chars": recorded}),
            encoding="utf-8",
        )
        assert consent.consented_history_budget() == expected

    def test_an_absent_ceiling_is_no_prior_turns(self, keystone):
        """Every consent recorded before this ceiling existed keeps its meaning."""
        keystone.write_text(json.dumps({"enabled": True, "endpoint": CUSTOM}), encoding="utf-8")
        assert consent.consented_history_budget() == 0

    def test_refuses_to_clobber_a_corrupt_file(self, keystone):
        keystone.write_text("{not json", encoding="utf-8")
        with pytest.raises(consent.ConsentCorruptError):
            consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        assert keystone.read_text() == "{not json"

    def test_only_a_bool_with_a_destination_is_written(self, keystone):
        with pytest.raises(ValueError):
            consent.save_enabled("true", endpoint=DEFAULT_ENDPOINT)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            consent.save_enabled(True, endpoint="   ")
        assert not keystone.exists()

    def test_a_lowering_survives_an_enable_running_beside_it(self, keystone, monkeypatch):
        """Two owner PUTs on two threads, and the LOWER ceiling is the one left.

        The handler hands ``save_enabled`` to a thread, so the switch PUT (which
        omits the field and therefore keeps) and a PUT lowering the ceiling to 0
        really do overlap. Resolving the sentinel inside the function narrows the
        window but cannot order two reads against one write: the keeper reads 2000,
        the lowerer writes 0, and the keeper writes 2000 back -- an egress limit
        raised by losing a race.

        The interleaving is forced rather than raced for: the keeper's write is held
        open long enough for the lowerer to finish inside it. Under the lock the
        lowerer cannot start until the keeper is done, so the last write is its 0;
        without the lock the keeper's delayed 2000 lands last.
        """
        import threading
        import time

        from kiro_crew.decisions import consent as module

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=2000)
        real_write = module.atomic_write

        def _slow_keeper_write(path, text, **kwargs):
            if '"history_budget_chars": 2000' in text:
                time.sleep(0.3)
            return real_write(path, text, **kwargs)

        monkeypatch.setattr(module, "atomic_write", _slow_keeper_write)
        errors: list[BaseException] = []

        def _keep():
            try:
                consent.save_enabled(
                    True,
                    endpoint=DEFAULT_ENDPOINT,
                    history_budget_chars=consent.KEEP_HISTORY_BUDGET,
                )
            except BaseException as exc:  # pragma: no cover - reported, not swallowed
                errors.append(exc)

        def _lower():
            try:
                consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=0)
            except BaseException as exc:  # pragma: no cover - reported, not swallowed
                errors.append(exc)

        keeper = threading.Thread(target=_keep)
        lowerer = threading.Thread(target=_lower)
        keeper.start()
        time.sleep(0.05)
        lowerer.start()
        keeper.join(timeout=5)
        lowerer.join(timeout=5)

        assert not errors, errors
        assert not keeper.is_alive() and not lowerer.is_alive()
        assert consent.consented_history_budget() == 0, "the enable PUT restored a lowered ceiling"

    def test_the_write_is_held_under_one_lock(self, keystone):
        """The read and the write are inside the same acquisition, not two."""
        held: list[bool] = []
        real_read = consent.read_state_strict
        real_write = consent.atomic_write

        def _read_under_the_lock():
            held.append(consent._SAVE_LOCK.locked())
            return real_read()

        def _write_under_the_lock(path, text, **kwargs):
            held.append(consent._SAVE_LOCK.locked())
            return real_write(path, text, **kwargs)

        with (
            patch.object(consent, "read_state_strict", _read_under_the_lock),
            patch.object(consent, "atomic_write", _write_under_the_lock),
        ):
            consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=2000)

        assert held == [True, True]


# ---------------------------------------------------------------------------
# The dashboard handler
# ---------------------------------------------------------------------------


def _stream(data: bytes):
    """A minimal readable payload for ``make_mocked_request`` (no socket, no server)."""
    stream = StreamReader(MagicMock(), limit=2**16)
    stream.feed_data(data)
    stream.feed_eof()
    return stream


def _request(*, app: str = "", user: str = "owner-1", owner: str = "owner-1", body=None):
    """A request shaped like a real DASHBOARD OWNER call (see test_aws_consent.py)."""
    req = MagicMock()
    req.path = "/api/decisions/consent"
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
def audit(monkeypatch):
    """Capture SEL rows the handler writes."""
    import kiro_crew.dashboard.handlers as handlers_pkg

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_api_access = lambda **kw: rows.append(kw)
    monkeypatch.setattr(handlers_pkg, "sel", lambda: fake)
    return rows


class TestHandler:
    @pytest.mark.asyncio
    async def test_get_reports_the_keystone_against_the_configured_endpoint(
        self, keystone, audit, configured
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_get

        resp = await api_decisions_consent_get(_request())
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "enabled": False,
            "endpoint": "",
            "configured_endpoint": DEFAULT_ENDPOINT,
            "permits": False,
            "history_budget_chars": 0,
            # Reported so the card draws the scope actually recorded rather than
            # inferring it from ``enabled``; absent on this keystone reads false.
            "tool_args": False,
            "compaction": False,
            "memory_text": False,
        }
        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        assert json.loads((await api_decisions_consent_get(_request())).text)["permits"] is True
        # The config moved the destination: consent stands for the old one only.
        configured(CUSTOM)
        body = json.loads((await api_decisions_consent_get(_request())).text)
        assert body["enabled"] is True and body["permits"] is False
        assert body["endpoint"] == DEFAULT_ENDPOINT and body["configured_endpoint"] == CUSTOM
        # Every successful read is audited too, with the address it reported.
        assert [(r["operation"], r["outcome"]) for r in audit] == [
            ("decisions_consent_get", "allowed")
        ] * 3
        assert f"endpoint={CUSTOM}" in audit[-1]["resources"]

    @pytest.mark.asyncio
    async def test_the_audit_hops_off_the_loop_when_sel_is_cold(
        self, keystone, audit, configured, monkeypatch
    ):
        """A failed SEL warm makes ``sel()`` retry blocking init; the handler then
        writes its row from a worker thread, never on the event loop (the
        ``server._audit_middleware_denial`` gate)."""
        import threading

        import kiro_crew.sel as sel_mod
        from kiro_crew.dashboard.handlers import decisions as mod

        loop_thread = threading.get_ident()
        writer_threads: list[int] = []
        audit_fake = mod._sel()
        orig = audit_fake.log_api_access
        audit_fake.log_api_access = lambda **kw: (
            writer_threads.append(threading.get_ident()),
            orig(**kw),
        )

        monkeypatch.setattr(sel_mod, "sel_is_warm", lambda: False)
        await mod.api_decisions_consent_get(_request())
        assert writer_threads and all(t != loop_thread for t in writer_threads)

        writer_threads.clear()
        monkeypatch.setattr(sel_mod, "sel_is_warm", lambda: True)
        await mod.api_decisions_consent_get(_request())
        assert writer_threads == [loop_thread], "warm SEL keeps the direct enqueue"
        assert len(audit) == 2

    @pytest.mark.asyncio
    async def test_a_failing_audit_never_breaks_the_request(
        self, keystone, configured, monkeypatch
    ):
        import kiro_crew.dashboard.handlers as handlers_pkg
        from kiro_crew.dashboard.handlers import decisions as mod

        def _boom():
            raise RuntimeError("SEL down")

        monkeypatch.setattr(handlers_pkg, "sel", _boom)
        resp = await mod.api_decisions_consent_get(_request())
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_put_binds_consent_to_the_configured_endpoint_and_audits_it(
        self, keystone, audit, configured
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        configured(CUSTOM)
        resp = await api_decisions_consent_put(_request(body={"enabled": True, "endpoint": CUSTOM}))
        assert resp.status == 200
        assert json.loads(resp.text)["permits"] is True
        assert consent.permits(CUSTOM) is True and consent.permits(DEFAULT_ENDPOINT) is False
        resp = await api_decisions_consent_put(_request(body={"enabled": False}))
        assert json.loads(resp.text)["enabled"] is False
        assert consent.is_enabled() is False
        assert [(r["operation"], r["outcome"]) for r in audit] == [
            ("decisions_consent_put", "granted"),
            ("decisions_consent_put", "revoked"),
        ]
        assert f"endpoint={CUSTOM}" in audit[0]["resources"]

    @pytest.mark.asyncio
    async def test_enabling_must_echo_the_reviewed_endpoint(self, keystone, audit, configured):
        """The GET-to-PUT window is operator-paced and config is agent-writable: consent
        binds to the address the owner SAW, or it is refused."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        # No echo at all: refused as a malformed body, nothing written.
        resp = await api_decisions_consent_put(_request(body={"enabled": True}))
        assert resp.status == 400 and not keystone.exists()
        # The owner reviewed the default; the config now names another address.
        configured(CUSTOM)
        resp = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )
        assert resp.status == 409
        payload = json.loads(resp.text)
        assert payload["code"] == "decisions_consent_endpoint_changed"
        assert payload["configured_endpoint"] == CUSTOM
        assert not keystone.exists(), "a refused echo writes nothing"
        assert audit[-1]["outcome"] == "denied" and audit[-1]["error"] == "endpoint_changed"
        # Disabling needs no echo: withdrawing consent is never the risky direction.
        resp = await api_decisions_consent_put(_request(body={"enabled": False}))
        assert resp.status == 200 and consent.is_enabled() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{"enabled": "true"}, {"enabled": 1}, {}, [], "yes", {"enabled": None}],
        ids=["string", "int", "missing", "list", "scalar", "null"],
    )
    async def test_put_accepts_only_a_real_boolean(self, keystone, audit, configured, body):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(_request(body=body))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "decisions_consent_invalid_body"
        assert not keystone.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [True, False, "2000", 2000.5, -1, [2000], {"n": 1}])
    async def test_the_route_refuses_a_ceiling_that_is_not_a_whole_number(
        self, keystone, audit, configured, bad
    ):
        """Refused at the DOOR, not coerced: it is written into a security record.

        Driven through the handler rather than through ``save_enabled``, because the
        route carries its own validation and a test of the writer leaves it
        unexercised -- which is what revert-verify caught.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request(
                body={"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": bad}
            )
        )

        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "decisions_consent_invalid_body"
        assert consent.consented_history_budget() == 0

    @pytest.mark.asyncio
    async def test_the_route_records_the_ceiling_the_owner_echoed(
        self, keystone, audit, configured
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request(
                body={"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 2000}
            )
        )

        assert resp.status == 200
        assert json.loads(resp.text)["history_budget_chars"] == 2000
        assert consent.consented_history_budget() == 2000

    @pytest.mark.asyncio
    async def test_omitting_the_ceiling_with_none_recorded_consents_to_no_prior_turns(
        self, keystone, audit, configured
    ):
        """With nothing recorded, the switch consents to the message and the menu."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )

        assert resp.status == 200
        assert json.loads(resp.text)["history_budget_chars"] == 0

    @pytest.mark.asyncio
    async def test_omitting_the_ceiling_leaves_a_recorded_one_as_it_was(
        self, keystone, audit, configured
    ):
        """An enable PUT that never mentions the ceiling must not lower it.

        The consent card sends ``enabled`` and ``endpoint`` only, so defaulting the
        missing field to 0 makes an ordinary switch flip erase a ceiling recorded
        through this same route -- a security decision lowered by a request that
        said nothing about it, and silently, since the route answers 200.

        Driven through the handler, because the default that did the erasing lives
        on the route and a test of ``save_enabled`` leaves it unexercised.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        first = await api_decisions_consent_put(
            _request(
                body={"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 2000}
            )
        )
        assert first.status == 200
        assert consent.consented_history_budget() == 2000

        again = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )

        assert again.status == 200
        assert json.loads(again.text)["history_budget_chars"] == 2000
        assert consent.consented_history_budget() == 2000

    @pytest.mark.asyncio
    async def test_the_route_hands_the_keep_sentinel_down_rather_than_a_number(
        self, keystone, audit, configured, monkeypatch
    ):
        """Resolving the ceiling here would write a number read before the write.

        Two owner PUTs can overlap: one lowering the ceiling, one that only flips the
        switch. If this route resolved the omitted field itself, it would hold a
        ceiling read BEFORE the lowering landed and write that number back, raising an
        egress limit by losing a race. The writer resolves it from the same read its
        write is based on, so the route must hand the sentinel down untouched.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=2000)
        seen: list = []
        real = consent.save_enabled

        def _spy(
            enabled,
            *,
            endpoint,
            history_budget_chars=0,
            tool_args=False,
            compaction=False,
            memory_text=False,
        ):
            seen.append(history_budget_chars)
            return real(
                enabled,
                endpoint=endpoint,
                history_budget_chars=history_budget_chars,
                tool_args=tool_args,
                compaction=compaction,
                memory_text=memory_text,
            )

        monkeypatch.setattr(consent, "save_enabled", _spy)

        resp = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )

        assert resp.status == 200
        assert seen == [consent.KEEP_HISTORY_BUDGET], "the route resolved it instead"
        assert consent.consented_history_budget() == 2000

    @pytest.mark.asyncio
    async def test_keep_reads_the_ceiling_from_the_write_s_own_read(self, keystone):
        """A lowering that lands before the write wins, rather than being restored."""
        lowered = {"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 0}
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT, history_budget_chars=2000)

        with patch.object(consent, "read_state_strict", return_value=lowered):
            state = consent.save_enabled(
                True,
                endpoint=DEFAULT_ENDPOINT,
                history_budget_chars=consent.KEEP_HISTORY_BUDGET,
            )

        assert state["history_budget_chars"] == 0
        assert consent.consented_history_budget() == 0

    @pytest.mark.asyncio
    async def test_a_ceiling_of_zero_in_the_body_still_clears_a_recorded_one(
        self, keystone, audit, configured
    ):
        """0 is a ceiling the owner chose, and absent is a field nobody sent.

        Preserving on absence is only safe if an explicit 0 still lowers it, which is
        the owner's way to take prior turns back without turning the seam off.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        await api_decisions_consent_put(
            _request(
                body={"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 2000}
            )
        )

        resp = await api_decisions_consent_put(
            _request(
                body={"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 0}
            )
        )

        assert resp.status == 200
        assert json.loads(resp.text)["history_budget_chars"] == 0
        assert consent.consented_history_budget() == 0

    @pytest.mark.asyncio
    async def test_disabling_without_the_field_still_clears_the_ceiling(
        self, keystone, audit, configured
    ):
        """Preserving is for an enable; a revocation clears the ceiling with it."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        await api_decisions_consent_put(
            _request(
                body={"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 2000}
            )
        )

        resp = await api_decisions_consent_put(_request(body={"enabled": False}))

        assert resp.status == 200
        assert json.loads(resp.text)["history_budget_chars"] == 0
        assert consent.consented_history_budget() == 0

    @pytest.mark.asyncio
    async def test_put_refuses_a_body_that_is_not_json(self, keystone, audit, configured):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        resp = await api_decisions_consent_put(_request(body=ValueError("bad json")))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_put_leaves_a_corrupt_keystone_byte_identical(self, keystone, audit, configured):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        keystone.write_text("{corrupt", encoding="utf-8")
        resp = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "decisions_consent_corrupt"
        assert keystone.read_text() == "{corrupt"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [{"app": "some-app"}, {"user": "slack-friend"}],
        ids=["app-token", "non-owner-user"],
    )
    async def test_both_verbs_are_owner_only(self, keystone, audit, configured, kwargs):
        """An app token or an allow-listed non-owner is the agent's third key;
        both are refused on the read too, so nobody but the owner learns the state."""
        from kiro_crew.dashboard.handlers.decisions import (
            api_decisions_consent_get,
            api_decisions_consent_put,
        )

        resp = await api_decisions_consent_get(_request(**kwargs))
        assert resp.status == 403
        resp = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": DEFAULT_ENDPOINT}, **kwargs)
        )
        assert resp.status == 403
        assert not keystone.exists()
        assert {r["outcome"] for r in audit} == {"denied"}

    def test_the_handler_module_keeps_the_seam_off_the_boot_path(self):
        """``handlers/__init__`` imports this module at boot; the optional seam must not
        come with it (AUTOSDE ``no-new-work-on-gateway-boot-path``, clause 5)."""
        import subprocess
        import sys

        probe = (
            "import sys; import kiro_crew.dashboard.handlers; "
            "print(sorted(m for m in sys.modules if m.startswith('kiro_crew.decisions')))"
        )
        # The child must import the same checkout the test runs against.
        import kiro_crew

        env = dict(os.environ)
        src = str(Path(kiro_crew.__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
        out = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=True,
            env=env,
        )
        assert out.stdout.strip() == "[]", out.stdout

    def test_the_routes_are_registered_browser_side(self):
        """Cookie-authed like the AWS pair, and not on the strict-internal list."""
        import inspect

        from kiro_crew.dashboard import routes
        from kiro_crew.dashboard.routes import system as system_routes

        source = inspect.getsource(system_routes)
        assert 'add_get("/api/decisions/consent"' in source
        assert 'add_put("/api/decisions/consent"' in source
        assert routes is not None


# ---------------------------------------------------------------------------
# The fleet's ceiling above the owner's switch: ``capabilities.decisions``
# ---------------------------------------------------------------------------


#: A ceiling that pins the seam off.
_PIN_DOC: dict = {
    "version": 1,
    "boot": {"fail_closed": True},
    "capabilities": {"decisions": {"enabled": False}},
}


@pytest.fixture
def ceiling(monkeypatch):
    """Install a boot-frozen ceiling for the duration of a test; returns a setter."""
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    def _set(doc: "dict | None") -> None:
        parsed = parse_policy(doc) if doc is not None else None

        class _Ctx:
            governance = parsed

        monkeypatch.setattr(pc, "current_context", lambda: _Ctx())

    _set(None)
    return _set


@pytest.fixture
def governance_rows(monkeypatch):
    """Capture the ``governance_decision`` rows the probe writes through the seam."""
    import kiro_crew.sel as sel_mod

    rows: list[dict] = []
    fake = MagicMock()
    fake.log_governance_decision = lambda **kw: rows.append(kw)
    monkeypatch.setattr(sel_mod, "sel", lambda: fake)
    return rows


class TestCapabilityCeiling:
    """The fleet's side of the same question the keystone answers for the owner.

    An owner on a managed machine can consent in good faith to a paid external
    endpoint their fleet never approved, so the ceiling has to stand ABOVE the
    keystone rather than beside it: enabling is refused, and an existing
    ``"enabled": true`` is inert rather than carried over.
    """

    # ── the catalog row ───────────────────────────────────────────────

    def test_the_row_is_a_default_on_capability(self):
        """A DATA change: one row, no ``CONTRACT_VERSION`` or evaluator edit."""
        from kiro_crew.platform.governance import CAPABILITY, SCOPE_CATALOG

        spec = SCOPE_CATALOG["capabilities.decisions"]
        assert spec.kind == CAPABILITY
        # A policy governing some OTHER capabilities.* row must not silently
        # withdraw a feature it never mentioned.
        assert spec.capability_default is True

    def test_a_policy_can_actually_express_the_pin(self):
        from kiro_crew.platform.governance import CapabilityGate, parse_policy

        gate = parse_policy(_PIN_DOC).get("capabilities.decisions")
        assert isinstance(gate, CapabilityGate)
        assert gate.enabled is False

    # ── the probe ─────────────────────────────────────────────────────

    def test_the_probe_permits_an_ungoverned_host_and_denies_a_pin(self, ceiling, governance_rows):
        from kiro_crew.decisions.capability import is_decisions_denied

        assert is_decisions_denied() is False
        ceiling(_PIN_DOC)
        assert is_decisions_denied() is True
        assert [r["outcome"] for r in governance_rows] == ["allowed", "denied"]
        assert {r["scope"] for r in governance_rows} == {"capabilities.decisions"}
        assert {r["session_key"] for r in governance_rows} == {"dashboard:ui"}

    def test_the_probe_pins_the_dashboard_surface_and_fails_closed(self, monkeypatch, ceiling):
        """The surface key is what a profile binds on, and it is never a caller
        value -- a request carrying ``slack:x`` must not dodge a dashboard-bound
        profile. An unevaluable ceiling denies."""
        from kiro_crew.decisions import capability

        seen: list[dict] = []

        def _spy(scope, item, **kw):
            seen.append({"scope": scope, "item": item, **kw})
            return MagicMock(permitted=True)

        monkeypatch.setattr(capability, "vet_and_audit", _spy)
        assert capability.is_decisions_denied() is False
        assert seen == [
            {
                "scope": "capabilities.decisions",
                "item": "",
                "session_key": "dashboard:ui",
                "tool_name": capability.AUDIT_TOOL,
                "log_warning": False,
                "fail_closed": True,
            }
        ]

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(capability, "vet_and_audit", _boom)
        assert capability.is_decisions_denied() is True

    def test_an_unevaluable_ceiling_records_the_denial_the_seam_could_not(
        self, monkeypatch, governance_rows
    ):
        """``vet_and_audit`` never ran, so it wrote nothing: the probe must audit
        the denial it is about to act on itself."""
        from kiro_crew.decisions import capability

        monkeypatch.setattr(
            capability, "vet_and_audit", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError())
        )
        assert capability.is_decisions_denied() is True
        assert len(governance_rows) == 1
        assert governance_rows[0]["scope"] == "capabilities.decisions"
        assert governance_rows[0]["outcome"] == "denied"
        assert governance_rows[0]["tool_name"] == capability.AUDIT_TOOL
        assert "fail-closed" in governance_rows[0]["reason"]

    # ── chokepoint (a): the consent PUT ───────────────────────────────

    @pytest.mark.asyncio
    async def test_enabling_is_refused_and_nothing_is_written(
        self, keystone, audit, configured, ceiling, governance_rows
    ):
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        ceiling(_PIN_DOC)
        resp = await api_decisions_consent_put(
            _request(body={"enabled": True, "endpoint": DEFAULT_ENDPOINT})
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "decisions_capability_denied"
        assert not keystone.exists(), "a withdrawn seam must not acquire a keystone"
        assert audit[-1]["outcome"] == "denied" and audit[-1]["error"] == "capability_denied"

    @pytest.mark.asyncio
    async def test_disabling_still_succeeds_so_a_stale_consent_can_be_cleared(
        self, keystone, audit, configured, ceiling, governance_rows
    ):
        """The owner consented BEFORE the pin. Refusing the disabling write too
        would trap them with a keystone saying ``true`` they cannot clear."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        ceiling(_PIN_DOC)
        resp = await api_decisions_consent_put(_request(body={"enabled": False}))
        assert resp.status == 200
        assert json.loads(resp.text)["enabled"] is False
        assert consent.is_enabled() is False

    @pytest.mark.asyncio
    async def test_raising_the_history_ceiling_is_refused_like_any_other_grant(
        self, keystone, audit, configured, ceiling, governance_rows
    ):
        """A budget-only PUT carries no ``enabled`` and no scope, and still grants.

        The number bounds how much prior conversation the seam may carry, so recording a
        bigger one under the pin books an egress allowance for the moment the pin lifts
        -- and the pin is exactly when it is cheapest to book, because nothing sends
        while it holds. The write must be refused and the recorded ceiling untouched.

        The scope rides along at its REVOKING value, because the route refuses a body
        naming neither the switch nor a scope. So whatever this asserts can only have
        come from the ceiling.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        keystone.write_text(
            json.dumps(
                {"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 100}
            ),
            encoding="utf-8",
        )
        ceiling(_PIN_DOC)
        resp = await api_decisions_consent_put(
            _request(body={"memory_text": False, "history_budget_chars": 50_000})
        )
        assert resp.status == 403
        assert json.loads(resp.text)["code"] == "decisions_capability_denied"
        assert consent.consented_history_budget() == 100, "the ceiling must not move"
        assert audit[-1]["outcome"] == "denied" and audit[-1]["error"] == "capability_denied"

    @pytest.mark.asyncio
    async def test_clearing_the_history_ceiling_stays_available_under_the_pin(
        self, keystone, audit, configured, ceiling, governance_rows
    ):
        """``0`` takes prior turns back, so it is a revocation and must not be trapped."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        keystone.write_text(
            json.dumps(
                {"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 5_000}
            ),
            encoding="utf-8",
        )
        ceiling(_PIN_DOC)
        resp = await api_decisions_consent_put(
            _request(body={"memory_text": False, "history_budget_chars": 0})
        )
        assert resp.status == 200
        assert consent.consented_history_budget() == 0

    @pytest.mark.asyncio
    async def test_a_disabling_write_carrying_a_budget_is_not_trapped(
        self, keystone, audit, configured, ceiling, governance_rows
    ):
        """Disabling forces the ceiling to 0, so the number in the body grants nothing."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        keystone.write_text(
            json.dumps(
                {"enabled": True, "endpoint": DEFAULT_ENDPOINT, "history_budget_chars": 5_000}
            ),
            encoding="utf-8",
        )
        ceiling(_PIN_DOC)
        resp = await api_decisions_consent_put(
            _request(body={"enabled": False, "history_budget_chars": 9_000})
        )
        assert resp.status == 200
        assert consent.is_enabled() is False
        assert consent.consented_history_budget() == 0

    @pytest.mark.asyncio
    async def test_the_recorded_endpoint_is_never_taken_from_the_body(
        self, keystone, audit, configured, ceiling, governance_rows
    ):
        """The other half of the same question: a PUT cannot move the address either.

        The endpoint written is always ``gate.configured_endpoint()``, and a write that
        preserves consent preserves the RECORDED address from the writer's own read. The
        body's ``endpoint`` is only ever the echo an enabling write is checked against,
        and an enabling write is refused under the pin by the test above.
        """
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_put

        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        ceiling(_PIN_DOC)
        resp = await api_decisions_consent_put(
            _request(body={"endpoint": "https://attacker.example/v1", "memory_text": False})
        )
        assert resp.status == 200, "a revoking scope write stays available"
        assert consent.consented_endpoint() == DEFAULT_ENDPOINT

    @pytest.mark.asyncio
    async def test_the_get_reports_the_denial_and_stops_claiming_it_permits(
        self, keystone, audit, configured, ceiling, governance_rows
    ):
        """A keystone consenting for the configured address, under a pin: the card
        must not read ``permits: true`` and offer to send."""
        from kiro_crew.dashboard.handlers.decisions import api_decisions_consent_get

        keystone.write_text(
            json.dumps({"enabled": True, "endpoint": DEFAULT_ENDPOINT}), encoding="utf-8"
        )
        body = json.loads((await api_decisions_consent_get(_request())).text)
        assert body["permits"] is True

        ceiling(_PIN_DOC)
        body = json.loads((await api_decisions_consent_get(_request())).text)
        # ``enabled`` still reports the keystone as it stands -- the owner's record is
        # not rewritten -- but the EFFECTIVE answer flips, which is the one a caller
        # acts on. There is no separate reason field to read.
        assert body["enabled"] is True
        assert body["permits"] is False

    # ── the dashboard config read that hides the card ─────────────────

    @pytest.mark.asyncio
    async def test_the_dashboard_config_read_reports_the_answer(
        self, monkeypatch, ceiling, governance_rows
    ):
        """``GET /api/dashboard/config`` carries ``decisions_enabled`` beside
        ``social_share_enabled``; the frontend hides the card on anything but
        ``true``. Presentation, not the control -- the two chokepoints above are."""
        from aiohttp.test_utils import make_mocked_request

        import kiro_crew.dashboard.handlers as handlers_pkg
        from kiro_crew.dashboard.handlers.files import api_dashboard_config

        monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())
        resp = await api_dashboard_config(make_mocked_request("GET", "/api/dashboard/config"))
        assert json.loads(resp.text)["decisions_enabled"] is True

        ceiling(_PIN_DOC)
        resp = await api_dashboard_config(make_mocked_request("GET", "/api/dashboard/config"))
        assert json.loads(resp.text)["decisions_enabled"] is False

    @pytest.mark.asyncio
    async def test_the_config_put_drops_the_round_tripped_field(self, monkeypatch, ceiling):
        """Both settings surfaces PUT the spread GET body back, so the read-only
        field has to be DROPPED rather than rejected or every toggle save 400s."""
        from aiohttp.test_utils import make_mocked_request

        import kiro_crew.dashboard.handlers as handlers_pkg
        from kiro_crew.dashboard.handlers.files import api_dashboard_config

        monkeypatch.setattr(handlers_pkg, "sel", lambda: MagicMock())
        get = await api_dashboard_config(make_mocked_request("GET", "/api/dashboard/config"))
        body = json.loads(get.text)
        assert "decisions_enabled" in body
        payload = json.dumps({**body, "restore_sessions": True}).encode()
        # The write is owner-gated, and the gate reads ``request.app["state"]``
        # plus the claims the token middleware normally sets -- the same minimum
        # ``dashboard_owner_helpers.as_owner`` installs, without a server.
        app = web.Application()
        app["state"] = NoConfiguredOwner()
        put = make_mocked_request(
            "PUT",
            "/api/dashboard/config",
            payload=_stream(payload),
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
            app=app,
        )
        put["user"] = "local-app"
        put["app"] = ""
        resp = await api_dashboard_config(put)
        assert resp.status == 200, resp.text
