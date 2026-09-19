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
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

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
        assert consent.save_enabled(True, endpoint=CUSTOM) == {"enabled": True, "endpoint": CUSTOM}
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
        }
        assert consent.permits(CUSTOM) is False

    def test_keeps_an_operator_key_it_does_not_know(self, keystone):
        keystone.write_text('{"note": "kept", "enabled": false}', encoding="utf-8")
        consent.save_enabled(True, endpoint=DEFAULT_ENDPOINT)
        assert json.loads(keystone.read_text()) == {
            "note": "kept",
            "enabled": True,
            "endpoint": DEFAULT_ENDPOINT,
        }

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


# ---------------------------------------------------------------------------
# The dashboard handler
# ---------------------------------------------------------------------------


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
