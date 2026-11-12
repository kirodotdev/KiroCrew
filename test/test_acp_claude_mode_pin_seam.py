"""The starting-mode pin on a claude session that is neither excluded nor a Side Chat.

With nothing requested such a session's seed names no ``defaultMode``, so the adapter
starts it in the mode the user's own settings name, or a resumed one in its
transcript's. By default the session keeps that mode. With
``KIROCREW_CC_PIN_INHERITED_MODE`` on, ``_pin_claude_starting_mode`` reads it back: it
moves ``auto``, ``bypassPermissions`` or an unreported mode to ``default``, keeps a mode
that asks and the mode Crew requested, and stops the session when the move fails. The
exclusion path, Side Chat and the adapter-version floor are in
``test_acp_session_mcp_client_seam.py``; both modules replay the recorded
claude-agent-acp 0.84.0 answers through ``acp_session_mcp_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _no_ambient_pin_setting  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import (
    _PIN_SETTING_ENV,
    _adapter_transport,
    _by_name,
    _pin_harness,
    _write_spec,
)

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk import backends


class TestUnrequestedModePin:
    """The pin with ``KIROCREW_CC_PIN_INHERITED_MODE`` on."""

    _pin_harness = staticmethod(_pin_harness)
    _adapter_transport = staticmethod(_adapter_transport)

    @pytest.fixture(autouse=True)
    def _setting_on(self, _floor_monkeypatch, no_ambient_pin_setting):
        _floor_monkeypatch.setenv(_PIN_SETTING_ENV, "1")

    def _seeded(self, tmp_path, **kw):
        """A client whose settings seed has already run, as the spawn path does."""
        client = AcpClient(work_dir=tmp_path, **kw)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        return client

    def _record_sent_array(self, client):
        """Record Crew's array as the start sent it, for a test that calls the pin directly."""
        client._mcp_mount_servers = client._claude_session_mcp_servers()
        assert "foo" in _by_name(client._mcp_mount_servers)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["auto", "bypassPermissions"])
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_crew_authored_session_is_pinned_off_an_approving_mode(
        self, tmp_path, agents_dir, monkeypatch, started_mode, path
    ):
        """A session that requested no mode is read back like the exclusion path.

        Its seed names no ``defaultMode``, so the adapter starts a
        ``session/new`` in the mode the user's own settings name and a
        ``session/load`` in the mode its transcript was left in. The transport
        reports that mode here. One that approves on its own is moved to
        ``default`` before the first prompt, and the session keeps Crew's array.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        seeded = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert "permissions" not in seeded
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=started_mode)
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert client._resumed is (path == "load")
        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert "foo" in _by_name(params["mcpServers"])
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["default", "plan", "acceptEdits", "dontAsk"])
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_crew_authored_session_in_a_mode_that_asks_gains_no_call(
        self, tmp_path, agents_dir, monkeypatch, started_mode, path
    ):
        """Only an approving mode is moved: a stricter or narrower one is kept."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=started_mode)
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    async def test_a_crew_authored_session_that_reports_no_mode_is_pinned(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A response that names no mode is pinned, as on the exclusion path."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        self._record_sent_array(client)
        sent, killed = self._pin_harness(client, monkeypatch)
        await client._pin_claude_starting_mode({"sessionId": "s-1"})
        assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "default"})]
        assert killed == []

    @pytest.mark.asyncio
    async def test_a_failed_pin_stops_a_crew_authored_session(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The read-back fails closed on a session whose file Crew wrote too."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        self._record_sent_array(client)
        _sent, killed = self._pin_harness(client, monkeypatch, fail=True)
        with pytest.raises(client_mod.AcpError, match="pin the asking permission mode"):
            await client._pin_claude_starting_mode(
                {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
            )
        assert killed == [True]

    @pytest.mark.asyncio
    async def test_a_load_that_did_not_take_is_pinned_once_on_the_new_session(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A ``session/load`` with no ``modes`` falls through to ``session/new``.

        The load is not read back, because it did not take; the session that
        replaces it is, so the pin is sent once, on the new id.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        answer = client._wait_for_response

        async def _load_without_modes(rid, **kw):
            resp = await answer(rid, **kw)
            if sent[rid - 1][0] == "session/load":
                resp = {key: value for key, value in resp.items() if key != "modes"}
            return resp

        monkeypatch.setattr(client, "_wait_for_response", _load_without_modes)
        await client._initialize_session()
        assert client._resumed is False
        assert client._session_id == "s-new"
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": "s-new", "modeId": "default"}
        ]

    @pytest.mark.asyncio
    async def test_a_failed_pin_on_a_resumed_session_is_not_taken_for_a_failed_load(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The load-path pin sits outside the load's own error handling.

        A ``session/set_mode`` that fails after a successful ``session/load``
        stops the harness; it does not fall through to ``session/new``.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        answer = client._wait_for_response

        async def _refuse_set_mode(rid, **kw):
            if sent[rid - 1][0] == "session/set_mode":
                raise client_mod.AcpError("set_mode refused")
            return await answer(rid, **kw)

        monkeypatch.setattr(client, "_wait_for_response", _refuse_set_mode)
        with pytest.raises(client_mod.AcpError, match="pin the asking permission mode"):
            await client._initialize_session()
        assert killed == [True]
        assert "session/new" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    async def test_the_deny_re_read_stays_on_the_exclusion_path(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A checked-in project deny does not stop a session whose file Crew wrote.

        That session loads the project tier itself, so its deny rules apply
        natively. Only the exclusion path carries them inline and re-reads them
        before the first prompt.
        """
        project = tmp_path / ".claude"
        project.mkdir()
        (project / "settings.json").write_text(
            json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}), encoding="utf-8"
        )
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert client._claude_session_excludes_local() is False
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": "s-new", "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_an_opt_in_session_that_starts_or_resumes_in_bypass_is_pinned(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """The opt-in keeps ``auto`` only: a reported ``bypassPermissions`` is moved.

        ``auto`` is the only mode Crew requests, so this is also the case of a
        requested mode that differs from the approving mode the adapter reports.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, permission_mode="auto"
        )
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_crew_opt_in_mode_is_not_overridden(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """The documented ``auto`` opt-in keeps the mode Crew asked for."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, permission_mode="auto"
        )
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_the_pin_does_not_project_the_array_again(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """The pin decides from the array the start sent, not from a new projection.

        The re-seed after the start clears the projection cache, so a projection
        made inside the pin would read the spec and the settings files on the
        event loop.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        await client._prepare_session_mcp()
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        inside_pin: list[bool] = []
        cache_cold_at_pin: list[bool] = []
        projected_inside_pin: list[bool] = []
        pin = client._pin_claude_starting_mode
        project = client._resolve_session_mcp_servers

        async def _recording_pin(resp):
            cache_cold_at_pin.append(client._session_mcp_cache is None)
            inside_pin.append(True)
            try:
                await pin(resp)
            finally:
                inside_pin.pop()

        def _recording_project():
            projected_inside_pin.append(bool(inside_pin))
            return project()

        monkeypatch.setattr(client, "_pin_claude_starting_mode", _recording_pin)
        monkeypatch.setattr(client, "_resolve_session_mcp_servers", _recording_project)
        await client._initialize_session()
        assert cache_cold_at_pin == [True]
        assert True not in projected_inside_pin
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_sent_array_is_pinned_though_a_new_projection_withholds_it(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """A session that started with Crew's servers is read back whatever comes later.

        A projection made after the start can withhold the array, for one when a
        settings read is refused or the file was replaced since the spawn. The
        session already holds Crew's servers, so its mode is still pinned.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        await client._prepare_session_mcp()
        monkeypatch.setattr(client, "_resolve_session_mcp_servers", lambda: [])
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert "foo" in _by_name(params["mcpServers"])
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]


class TestUnrequestedModeKeptByDefault:
    """With the setting off, the default, such a session keeps the mode it started in."""

    _adapter_transport = staticmethod(_adapter_transport)
    _seeded = TestUnrequestedModePin._seeded

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_user_auto_start_with_crews_array_is_not_pinned(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """A user ``defaultMode: auto``, Crew's array sent, no request: no pin, no stop."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        work = tmp_path / "work"
        work.mkdir()
        client = self._seeded(work, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert client._permission_mode is None
        assert "permissions" not in json.loads(
            (work / ".claude" / "settings.local.json").read_text()
        )
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert "foo" in _by_name(params["mcpServers"])
        assert "foo" in _by_name(client._mcp_mount_servers)
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "pinned"),
        [
            ("1", True),
            ("true", True),
            pytest.param(" On ", True, id="spaced-On-True"),
            ("0", False),
            ("off", False),
            ("2", False),
        ],
    )
    async def test_the_setting_reaches_the_pin_from_its_env_var(
        self, tmp_path, agents_dir, monkeypatch, value, pinned
    ):
        """The pin reads the env var at the start: a truthy spelling turns it on."""
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        expected = [{"sessionId": "s-new", "modeId": "default"}] if pinned else []
        assert [p for m, p in sent if m == "session/set_mode"] == expected


class TestResolveCcPinInheritedMode:
    """The setting is off unless its env var holds a truthy spelling."""

    def test_the_setting_has_its_documented_name(self):
        assert backends.CC_PIN_INHERITED_MODE_ENV == _PIN_SETTING_ENV

    def test_unset_is_off_and_quiet(self, caplog):
        with caplog.at_level("WARNING"):
            assert backends.resolve_cc_pin_inherited_mode() is False
        assert caplog.text == ""

    @pytest.mark.parametrize(
        "value", ["1", "true", "TRUE", "yes", "on", pytest.param(" 1\n", id="spaced-1")]
    )
    def test_a_truthy_value_turns_it_on(self, monkeypatch, value):
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        assert backends.resolve_cc_pin_inherited_mode() is True

    @pytest.mark.parametrize("value", [pytest.param("", id="empty"), "0", "false", "no", "off"])
    def test_a_falsy_value_is_off_and_quiet(self, monkeypatch, caplog, value):
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        with caplog.at_level("WARNING"):
            assert backends.resolve_cc_pin_inherited_mode() is False
        assert caplog.text == ""

    @pytest.mark.parametrize("value", ["2", "enabled", "auto", "default"])
    def test_an_unrecognised_value_is_off_and_reported(self, monkeypatch, caplog, value):
        """A lever an operator believes is set is named, not silently dropped."""
        monkeypatch.setenv(_PIN_SETTING_ENV, value)
        with caplog.at_level("WARNING"):
            assert backends.resolve_cc_pin_inherited_mode() is False
        assert _PIN_SETTING_ENV in caplog.text
        assert "not recognised" in caplog.text
