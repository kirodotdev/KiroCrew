"""Session-array MCP wiring at the client seam: what ``AcpClient`` puts on ``session/new``.

A kiro backend passes no array; a claude backend translates the agent spec,
projects the admitted direct or pooled servers, resolves the array once and drops
it on reset. The array rides with the settings seed: the project tier's deny and
ask rules go inline, an unexaminable or linked project file withholds it, and a
session started in a mode that escapes Crew's gate is pinned (or stopped when the
pin fails). The claude adapter's version floor decides whether a session gets
tools at all, and a side read-only session starts confined and pinned, with
root-anchored deny rules. The element shape, mounting, deny rules, control plane
and settings seed are in ``test_acp_session_mcp.py``; both modules take their
fixtures from ``acp_session_mcp_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from unittest import mock

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _by_name, _write_project_spec, _write_spec

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.providers.mirrors import claude_code as claude_mirror
from kiro_crew.providers.mirrors import registry as mirrors_registry
from kiro_crew.providers.mirrors.claude_code import ClaudeCodeMirror


class TestClientSeam:
    def _seeded(self, tmp_path, **kw):
        """A client whose settings seed has already run, as the spawn path does.

        The array is withheld unless Crew authored ``settings.local.json`` -- that
        file is the backend's permission surface, and a tool Crew cannot gate is
        not handed over at all. So a seam test that skips the seed exercises the
        withhold path, not the translation. The spawn path runs the writer first
        for the same reason; see ``_resolve_session_mcp_servers``.
        """
        client = AcpClient(work_dir=tmp_path, **kw)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        return client

    def test_kiro_backend_passes_no_array(self, tmp_path, agents_dir):
        client = AcpClient(work_dir=tmp_path)
        # kiro-cli receives the same servers via --agent; a duplicate here would
        # shadow the spec's own entries.
        assert client._session_mcp_servers() == []

    def test_claude_backend_translates_the_spec(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())

    @pytest.mark.parametrize("pooled", [False, True])
    def test_claude_projects_the_admitted_direct_or_pooled_server(
        self,
        tmp_path,
        agents_dir,
        pooled,
    ):
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": ["serve"], "env": {"K": "V"}}},
            tools=["@foo"],
        )
        overlay = tmp_path / "broker-overlay"
        overlay.mkdir()
        (overlay / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "foo": {
                            _WRAPPER_MARKER: True,
                            "command": "broker-stub",
                            "args": [],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        client = self._seeded(
            tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            mcp_gateway_overlay=overlay if pooled else None,
        )
        original = _by_name(client._session_mcp_servers()).get("foo")
        # The mirror keeps the allowlisted entry in both supported transports.
        assert original is not None
        if not pooled:
            assert original["command"] == "/bin/foo"
            assert original["args"] == ["serve"]
            assert original["env"] == [{"name": "K", "value": "V"}]
        else:
            assert original["command"] == "broker-stub"
        # The shared append is inert for claude -- the mirror placed the stubs.
        assert client._pooled_mcp_servers() == []

    def test_neither_gate_is_an_identity_check(self, tmp_path, agents_dir, monkeypatch):
        """Two gates decide the seam, and neither reads the harness's identity.

        The capability set decides WHETHER the array is consulted (a property of the
        transport, not the vendor -- harness-parity H6), and the mirror registry
        decides WHAT fills it. Widening the set alone must NOT populate: a backend
        with no registered mirror has nothing to contribute and fails closed. Add
        both and it works with no edit at either call site, which is what proves no
        identity branch has crept back in.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew")
        assert client._session_mcp_servers() == []

        # Set widened, no mirror registered -> still nothing. Fail-closed.
        monkeypatch.setattr(
            client_mod, "ACP_BACKENDS_SESSION_MCP_ARRAY", frozenset({client.backend})
        )
        client._reset_state()
        client._write_claude_local_settings()  # reset ends the session's ownership
        assert client._session_mcp_servers() == []

        # Register a mirror for that backend too, and the array populates.
        monkeypatch.setitem(mirrors_registry.MIRRORS, client.backend, ClaudeCodeMirror)
        client._reset_state()
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_the_seam_hands_down_the_pooled_stub_names(self, tmp_path, agents_dir, monkeypatch):
        # The client owns the overlay, so it is the only layer that can answer
        # which servers will ALSO arrive as broker stubs.
        _write_spec(
            agents_dir,
            servers={"pooled": {"command": "/bin/raw"}, "direct": {"command": "/bin/direct"}},
            tools=["@pooled", "@direct"],
        )
        monkeypatch.setattr(
            # ``**_kw`` so the double keeps mirroring the real signature: the call
            # site passes the session's checkout as ``work_dir``, and a double that
            # refuses it turns this seam test into a test of the except branch.
            client_mod,
            "injection_server_names",
            lambda _o, _a, **_kw: frozenset({"pooled"}),
        )
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        names = _by_name(client._session_mcp_servers())
        assert "pooled" not in names
        assert "direct" in names

    def test_an_unreadable_overlay_does_not_cost_the_session_its_servers(
        self, tmp_path, agents_dir, monkeypatch
    ):
        # Empty is the safe direction: re-declaring a stubbed server lets the
        # injection outrank it, while withholding one nothing else supplies is a
        # session with missing tools.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])

        def _boom(_o, _a, **_kw):
            raise RuntimeError("overlay unreadable")

        monkeypatch.setattr(client_mod, "injection_server_names", _boom)
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_the_shared_call_site_reads_no_disk_for_kiro(self, tmp_path, agents_dir, monkeypatch):
        """harness-parity H13: the kiro construction path gains nothing.

        Both session-params call sites are shared with kiro-cli, so the accessor
        they call must be synchronous AND must not reach the translator for a
        backend outside the capability set. If it did, adapter work would have put
        a new scheduling and failure point on kiro's ``session/new``.
        """

        def _never(*_a, **_kw):
            raise AssertionError("the kiro path must not translate a spec")

        monkeypatch.setattr(claude_mirror, "session_mcp_projection", _never)
        monkeypatch.setattr(client_mod, "injection_server_names", _never)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew")
        result = client._session_mcp_servers()
        assert result == []
        # A coroutine here would force the shared call site to await.
        assert not hasattr(result, "__await__")

    def test_the_array_is_resolved_once_and_dropped_on_reset(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Cached per spawn, not per call site, and re-read on the next spawn.

        session/new and the session/load that resumes it both read the accessor;
        translating twice would double the disk work for one session. Clearing on
        reset is what keeps the "installing a server takes effect on the NEXT
        session" promise.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        calls: list[int] = []
        real = session_mcp.session_mcp_projection

        def _counted(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        monkeypatch.setattr(claude_mirror, "session_mcp_projection", _counted)
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 1
        client._reset_state()
        assert client._session_mcp_cache is None
        client._write_claude_local_settings()  # the next spawn re-seeds before resolving
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 2

    @staticmethod
    def _project_owned_settings(tmp_path, payload):
        """A project's OWN ``.claude/settings.local.json``, written before Crew runs."""
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @staticmethod
    async def _session_new_params(client, tmp_path, monkeypatch):
        """Drive the real ``session/new`` call site and return the params it sent."""
        sent: list[dict] = []

        async def _work_dir():
            return str(tmp_path)

        async def _send(_method, params):
            sent.append(params)
            return 1

        async def _wait(_rid, **_kw):
            return {"sessionId": "s-1"}

        monkeypatch.setattr(client, "_session_work_dir", _work_dir)
        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        await client._new_session_following_substitution()
        assert len(sent) == 1
        return sent[0]

    @pytest.mark.asyncio
    async def test_a_project_owned_settings_file_still_gets_crews_tools(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A project that owns settings.local.json keeps Crew's tools, gated.

        The file is left exactly as it is, and out of the session: the envelope
        loads only the ``user`` setting source, so neither the project's
        ``settings.local.json`` nor its checked-in ``.claude/settings.json`` reaches
        the CLI. No ``permissions.allow`` a repository carries can pre-approve a
        call before Crew's gate sees it. Crew's own settings ride inline instead.
        That is what makes it safe to deliver the whole array -- so a cron job's
        ``send_message`` reaches this session too.
        """
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "disabledTools": ["danger"]}},
            tools=["@foo"],
        )
        original = {"permissions": {"allow": ["mcp__foo__write"]}}
        path = self._project_owned_settings(tmp_path, original)
        # The checked-in project tier carries an allow rule of its own.
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["mcp__foo__read"]}}), encoding="utf-8"
        )

        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is False
        assert json.loads(path.read_text()) == original

        params = await self._session_new_params(client, tmp_path, monkeypatch)

        assert "foo" in _by_name(params["mcpServers"])
        options = params["_meta"]["claudeCode"]["options"]
        # Neither project tier loads: no local, and no checked-in project file.
        assert options["settingSources"] == ["user"]
        assert options["allowDangerouslySkipPermissions"] is False
        # Neither allow rule reaches the session through any channel.
        assert "mcp__foo__write" not in json.dumps(params)
        assert "mcp__foo__read" not in json.dumps(params)
        # Crew's own restriction does, through the inline settings tier.
        assert "mcp__foo__danger" in options["settings"]["permissions"]["deny"]
        # And the project's file is still exactly the project's.
        assert json.loads(path.read_text()) == original
        await client._discard_claude_settings_seed()
        client._reset_state()
        assert json.loads(path.read_text()) == original

    def test_the_project_tiers_deny_rules_ride_inline(self, tmp_path, agents_dir):
        """Leaving the project tiers out drops their allows, never their denies.

        Both project files' ``permissions.deny`` rules are carried in the inline
        settings beside Crew's own, so a repository's refusal still holds.
        """
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "disabledTools": ["danger"]}},
            tools=["@foo"],
        )
        self._project_owned_settings(
            tmp_path,
            {"permissions": {"allow": ["mcp__foo__write"], "deny": ["Bash(git push:*)"]}},
        )
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"deny": ["Bash(rm:*)", "Bash(git push:*)"]}}),
            encoding="utf-8",
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        options = client._claude_session_meta()["claudeCode"]["options"]
        deny = options["settings"]["permissions"]["deny"]
        assert "mcp__foo__danger" in deny
        assert "Bash(git push:*)" in deny
        assert "Bash(rm:*)" in deny
        assert deny.count("Bash(git push:*)") == 1
        assert "mcp__foo__write" not in json.dumps(options)

    @pytest.mark.parametrize(
        "shape", ["directory", "oversized", "fifo", "deep", "hooks", "sandbox"]
    )
    def test_unexaminable_project_denies_withhold_the_array(self, tmp_path, agents_dir, shape):
        """A deny file whose bytes cannot be examined never drops silently: no tools ship."""
        if shape == "fifo" and not hasattr(os, "mkfifo"):
            pytest.skip("needs os.mkfifo")
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        checked_in = tmp_path / ".claude" / "settings.json"
        if shape == "directory":
            checked_in.mkdir()
        elif shape == "fifo":
            os.mkfifo(checked_in)
        elif shape == "hooks":
            hook = {"type": "command", "command": "./block.sh"}
            checked_in.write_text(
                json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}}),
                encoding="utf-8",
            )
        elif shape == "sandbox":
            checked_in.write_text(json.dumps({"sandbox": {"enabled": True}}), encoding="utf-8")
        elif shape == "deep":
            depth = 200_000
            checked_in.write_text(
                '{"permissions": {"deny": ["Bash(rm:*)"]}, "x": ' + "[" * depth + "]" * depth + "}",
                encoding="utf-8",
            )
        else:
            checked_in.write_bytes(b" " * (client_mod._CLAUDE_PROJECT_SETTINGS_MAX_BYTES + 1))
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []

    def test_the_project_tiers_ask_rules_ride_inline(self, tmp_path, agents_dir):
        """An ``ask`` rule forces a prompt, so leaving its tier out must not drop it."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {"ask": ["Bash(git push:*)"]}})
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"ask": ["mcp__foo__write", "Bash(git push:*)"]}}),
            encoding="utf-8",
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        ask = client._claude_session_meta()["claudeCode"]["options"]["settings"]["permissions"][
            "ask"
        ]
        assert ask == ["Bash(git push:*)", "mcp__foo__write"]

    @pytest.mark.asyncio
    async def test_an_ask_rule_gained_before_the_first_prompt_stops_the_session(
        self, tmp_path, agents_dir, monkeypatch
    ):
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="default")
        real_send = client._send_request

        async def _send(method, params):
            rid = await real_send(method, params)
            if method == "session/new":
                (tmp_path / ".claude" / "settings.json").write_text(
                    json.dumps({"permissions": {"ask": ["Bash(git push:*)"]}}), encoding="utf-8"
                )
            return rid

        monkeypatch.setattr(client, "_send_request", _send)
        with pytest.raises(client_mod.AcpError, match="gained deny or ask rules"):
            await client._initialize_session()
        assert killed == [True]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.84.0", "0.83.0", None])
    async def test_the_handshake_never_resolves_the_array_on_the_loop(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The floor is decided before the array is warmed, so nothing rebuilds it.

        Once the spawn arm has warmed the projection, the handshake -- whatever
        version the adapter reports -- must never fall back to the inline
        resolve, which reads disk on the gateway loop.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})

        def _on_loop_resolve():
            raise AssertionError("the MCP array was re-resolved on the event loop")

        monkeypatch.setattr(client, "_resolve_session_mcp_servers", _on_loop_resolve)
        _sent, _killed = self._adapter_transport(
            client, monkeypatch, started_mode="default", version=version
        )
        try:
            await client._initialize_session()
        except client_mod.AcpError as exc:
            assert "not verified to honour settingSources" in str(exc)
            assert version != "0.84.0"

    @pytest.mark.parametrize(
        "checked_in",
        [
            "{not json",
            json.dumps(["not", "a", "dict"]),
            json.dumps({"permissions": "nope"}),
            json.dumps({"permissions": {"deny": "Bash(rm:*)"}}),
        ],
    )
    def test_a_file_claude_takes_no_denies_from_adds_none(self, tmp_path, agents_dir, checked_in):
        """Content claude could not take deny rules from contributes none, and blocks nothing."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        (tmp_path / ".claude" / "settings.json").write_text(checked_in, encoding="utf-8")
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        assert "Bash(rm:*)" not in json.dumps(client._claude_session_meta())

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
    def test_a_linked_checked_in_settings_file_withholds_the_array(self, tmp_path, agents_dir):
        """A checked-in ``settings.json`` that is a link is never read through."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}), encoding="utf-8")
        try:
            (tmp_path / ".claude" / "settings.json").symlink_to(target)
        except OSError:
            pytest.skip("symlink creation not permitted")
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._session_mcp_servers() == []

    def test_each_write_redecides_the_exclusion(self, tmp_path, agents_dir):
        """A later run where Crew authors the file drops the earlier exclusion."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        path = self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is True
        path.unlink()
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        assert client._claude_local_settings_excluded is False
        assert client._claude_inline_settings is None
        assert "foo" in _by_name(client._session_mcp_servers())
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    def test_the_envelope_follows_the_array_it_shipped_with(self, tmp_path, agents_dir):
        """An array projected under the exclusion carries it even once the flag clears."""
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "disabledTools": ["danger"]}},
            tools=["@foo"],
        )
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        client._claude_local_settings_excluded = False
        client._claude_inline_settings = None
        options = client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]
        assert options["allowDangerouslySkipPermissions"] is False
        assert "mcp__foo__danger" in options["settings"]["permissions"]["deny"]

    @pytest.mark.asyncio
    async def test_the_substitution_retry_rebuilds_the_envelope(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The retry's envelope comes from the retry's own re-seed.

        The first ``session/new`` is sent before the project file exists, under
        Crew's own seed. The file then appears, and the retry leaves it out: the
        array it re-derives must travel with ``settingSources`` minus ``local``
        and with the substitute model inline, never with the first attempt's
        envelope.
        """
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        from kiro_crew import model_registry

        substitute = "global.anthropic.claude-sonnet-4-6[1m]"
        monkeypatch.setattr(
            model_registry,
            "_ADVERTISED_MODELS",
            {"claude_code": ["global.anthropic.claude-opus-5[1m]", substitute]},
        )
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = AcpClient(
            work_dir=tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            model="global.anthropic.claude-opus-5[1m]",
        )
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        path = tmp_path / ".claude" / "settings.local.json"
        sent: list[dict] = []

        async def _work_dir():
            return str(tmp_path)

        async def _send(_method, params):
            sent.append(json.loads(json.dumps(params)))
            if len(sent) == 1:
                # Crew's seed goes away and the project's own file takes the path.
                client._claude_settings_authored = False
                client._claude_settings_written = None
                path.unlink()
                path.write_text(
                    json.dumps({"permissions": {"allow": ["mcp__foo__write"]}}), encoding="utf-8"
                )
            return len(sent)

        async def _wait(_rid, **_kw):
            if len(sent) == 1:
                client._last_substitution_model = substitute
                return {}
            return {"sessionId": "s-1"}

        monkeypatch.setattr(client, "_session_work_dir", _work_dir)
        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        await client._new_session_following_substitution()

        assert len(sent) == 2
        assert sent[0]["_meta"] == {"claudeCode": {"options": {}}}
        retry = sent[1]
        assert "foo" in _by_name(retry["mcpServers"])
        options = retry["_meta"]["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]
        assert options["settings"]["model"] == substitute
        assert "mcp__foo__write" not in json.dumps(retry)

    @pytest.mark.asyncio
    async def test_a_crew_authored_seed_keeps_every_setting_source(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The normal case is unchanged: Crew's own file IS the local tier."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        params = await self._session_new_params(client, tmp_path, monkeypatch)
        assert "foo" in _by_name(params["mcpServers"])
        assert params["_meta"] == {"claudeCode": {"options": {}}}

    @staticmethod
    def _pin_harness(client, monkeypatch, *, fail=False):
        """Record the requests the pin sends; optionally make ``set_mode`` fail."""
        sent: list[tuple[str, dict]] = []
        killed: list[bool] = []

        async def _send(method, params):
            sent.append((method, params))
            return len(sent)

        async def _wait(_rid, **_kw):
            if fail:
                raise client_mod.AcpError("set_mode refused")
            return {}

        async def _kill(*, force=False):
            killed.append(force)

        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        monkeypatch.setattr(client, "_kill_process", _kill)
        return sent, killed

    def _excluded_client(self, tmp_path, agents_dir, payload):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, payload)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["bypassPermissions", "auto", None])
    async def test_a_session_started_in_a_gate_escaping_mode_is_pinned(
        self, tmp_path, agents_dir, monkeypatch, mode
    ):
        """The mode is read back from the harness, never from the mutable file.

        claude-agent-acp picks the starting mode from every settings file itself.
        So whatever the session reports -- or fails to report -- it is pinned to
        the asking mode before the first prompt.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._pin_harness(client, monkeypatch)
        resp = {"sessionId": "s-1"}
        if mode is not None:
            resp["modes"] = {"currentModeId": mode}
        await client._pin_claude_starting_mode(resp)
        assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "default"})]
        assert killed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["default", "plan", "acceptEdits", "dontAsk"])
    async def test_a_mode_that_still_asks_about_crew_tools_is_kept(
        self, tmp_path, agents_dir, monkeypatch, mode
    ):
        """An operator's stricter or narrower mode is never widened to ``default``.

        ``plan`` is read-only and ``acceptEdits`` approves only file edits; each
        still asks the host about an MCP call. ``dontAsk`` refuses what is not
        pre-approved, so it runs nothing ``default`` would not. Each is left
        exactly as the session started it.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._pin_harness(client, monkeypatch)
        await client._pin_claude_starting_mode(
            {"sessionId": "s-1", "modes": {"currentModeId": mode}}
        )
        assert sent == []
        assert killed == []

    @pytest.mark.asyncio
    async def test_a_pin_that_fails_stops_the_session(self, tmp_path, agents_dir, monkeypatch):
        """Crew's tools never run under a mode that approves on its own."""
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        _sent, killed = self._pin_harness(client, monkeypatch, fail=True)
        with pytest.raises(client_mod.AcpError, match="pin the asking permission mode"):
            await client._pin_claude_starting_mode(
                {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
            )
        assert killed == [True]

    @pytest.mark.asyncio
    async def test_a_file_swapped_after_the_check_cannot_keep_an_approving_mode(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A safe file at check time, an approving one at session/new.

        The array ships, because nothing about it depends on the file. The mode
        the swapped file picked comes back on the response and is pinned; if the
        pin fails the session is stopped, so it never ends with Crew's tools
        mounted under an approving mode.
        """
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"defaultMode": "default"}}
        )
        path = tmp_path / ".claude" / "settings.local.json"
        params = await self._session_new_params(client, tmp_path, monkeypatch)
        path.write_text(json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8")
        assert "foo" in _by_name(params["mcpServers"])
        # Bypass cannot even be the starting mode.
        assert params["_meta"]["claudeCode"]["options"]["allowDangerouslySkipPermissions"] is False

        sent, killed = self._pin_harness(client, monkeypatch)
        await client._pin_claude_starting_mode(
            {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
        )
        assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "default"})]

        _sent, killed = self._pin_harness(client, monkeypatch, fail=True)
        with pytest.raises(client_mod.AcpError):
            await client._pin_claude_starting_mode(
                {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
            )
        assert killed == [True]

    @staticmethod
    def _recorded_results(scenario):
        """The adapter's own answers, in order, from a live claude-agent-acp 0.84.0 capture.

        ``test/fixtures/claude_mode_pin/<scenario>.jsonl``: agent-to-client lines
        off the wire, pruned of recording-host data (see each file's ``_meta``).
        Returns the JSON-RPC results keyed by the request they answered.
        """
        path = Path(__file__).parent / "fixtures" / "claude_mode_pin" / f"{scenario}.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        head = rows[0]["_meta"]
        assert head["agent_version"] == "0.84.0" and head["recorded"] == "live"
        sent = [label.split(" ")[0] for label in head["client_sent"]]
        results = [row["result"] for row in rows[1:] if "id" in row and "result" in row]
        assert len(results) == len(sent)
        by_method: dict[str, list[dict]] = {}
        for method, result in zip(sent, results):
            by_method.setdefault(method, []).append(result)
        return by_method

    @classmethod
    def _adapter_transport(
        cls,
        client,
        monkeypatch,
        *,
        started_mode,
        version="0.84.0",
        scenario="project-plan-then-auto-then-pin",
    ):
        """Answer the handshake with claude-agent-acp 0.84.0's recorded frames.

        ``initialize``, ``session/new``, ``session/load`` and ``session/set_mode``
        are answered from the live capture. Two fields are set per test and
        named here so nothing else is invented: ``sessionId`` (``s-new``, or
        the id a ``session/load`` asked for), and
        ``modes.currentModeId`` when ``started_mode`` differs from the recorded
        one -- the capture shows a project file cannot START a session in
        ``auto`` or ``bypassPermissions`` on this path (``project-auto-filtered``),
        so those starting modes reach the pin only from another tier. ``version``
        replaces ``agentInfo.version`` for the floor tests, and ``None`` drops it.
        """
        recorded = cls._recorded_results(scenario)
        sent: list[tuple[str, dict]] = []
        killed: list[bool] = []

        def _with_mode(result):
            result = json.loads(json.dumps(result))
            if started_mode is not None:
                result["modes"]["currentModeId"] = started_mode
            return result

        async def _send(method, params):
            sent.append((method, params))
            return len(sent)

        async def _wait(rid, **_kw):
            method = sent[rid - 1][0]
            if method == "initialize":
                resp = json.loads(json.dumps(recorded["initialize"][0]))
                if version is None:
                    resp.pop("agentInfo", None)
                else:
                    resp["agentInfo"]["version"] = version
                return resp
            if method == "session/new":
                return {**_with_mode(recorded["session/new"][0]), "sessionId": "s-new"}
            if method == "session/load":
                # The adapter answers a load for the session it was asked to load.
                return {
                    **_with_mode(recorded["session/load"][0]),
                    "sessionId": sent[rid - 1][1]["sessionId"],
                }
            if method == "session/set_mode":
                return recorded["session/set_mode"][-1]
            return {}

        async def _kill(*, force=False):
            killed.append(force)

        async def _work_dir():
            return str(client._work_dir)

        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        monkeypatch.setattr(client, "_kill_process", _kill)
        monkeypatch.setattr(client, "_session_work_dir", _work_dir)
        monkeypatch.setattr(client, "_drain_notifications", mock.AsyncMock())
        return sent, killed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["default", "plan", "bypassPermissions"])
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_claude_session_actually_starts_under_the_pin(
        self, tmp_path, agents_dir, monkeypatch, started_mode, path
    ):
        """The whole handshake completes, on both establishment paths.

        The pin is not a kill that always fires: the adapter handles
        ``session/set_mode``, so a session that started in ``bypassPermissions``
        is moved to ``default`` and keeps running, and one in ``default`` or
        ``plan`` sends nothing extra.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=started_mode)

        await client._initialize_session()

        methods = [m for m, _ in sent]
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert client._resumed is (path == "load")
        assert ("session/load" if path == "load" else "session/new") in methods
        assert killed == []
        pins = [p for m, p in sent if m == "session/set_mode"]
        if started_mode in ("default", "plan"):
            # Kept exactly as started: a read-only mode is never widened.
            assert pins == []
        else:
            assert pins == [{"sessionId": client._session_id, "modeId": "default"}]

    @pytest.mark.parametrize(
        "version", ["0.83.0", "0.24.2", "", pytest.param("1" * 5000 + ".0.0", id="huge")]
    )
    def test_an_installed_adapter_below_the_floor_gets_no_tools(
        self, tmp_path, agents_dir, version
    ):
        """An adapter not verified to honour ``settingSources`` never gets the array.

        Below 0.84.0, or with no readable version, the adapter could load the
        project's ``permissions.allow`` anyway. The writer reads the installed
        package's version before the array is first resolved, so the exclusion
        is never taken: no Crew tools and no exclusion envelope, as before.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._claude_adapter_disk_version = version
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    @pytest.mark.parametrize("version", ["0.84.0", "0.85.2-preview.3", "1.0.0"])
    def test_an_installed_adapter_at_or_above_the_floor_keeps_the_tools(
        self, tmp_path, agents_dir, version
    ):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._claude_adapter_disk_version = version
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        options = client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.83.0", "", None])
    async def test_a_handshake_below_the_floor_stops_an_excluded_session(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The installed package passed the floor, the running adapter did not."""
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        _sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="default", version=version
        )
        with pytest.raises(client_mod.AcpError, match="not verified to honour settingSources"):
            await client._initialize_session()
        assert killed == [True]

    def test_the_spawn_arm_reads_the_version_before_the_seed_and_the_warm(
        self, tmp_path, monkeypatch
    ):
        """Floor first, then the writer, then the warm: the array is never rebuilt.

        Driven through the claude adapter's own launch: the writer must already see
        the installed adapter version (it applies the ``settingSources`` floor with
        it), and the session's MCP array is resolved once, after the writer decided
        whether Crew authored the settings file.
        """
        from kiro_crew.acp.harness import SpawnContext
        from kiro_crew.acp.harness import claude as claude_mod
        from kiro_crew.acp.harness import process_adapter_for
        from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

        steps: list[tuple[str, str]] = []
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

        def _version(argv):
            steps.append(("read", ""))
            return "0.84.0"

        def _write(self):
            steps.append(("seed", self._claude_adapter_disk_version))

        def _resolve(self):
            steps.append(("warm", ""))
            return []

        forget_cached_resolution(ACP_BACKEND_CLAUDE)
        monkeypatch.setattr(
            claude_mod, "_resolve_claude_acp_bin", lambda: (["/opt/bin/claude-agent-acp"], "")
        )
        monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", _version)
        monkeypatch.setattr(AcpClient, "_write_claude_local_settings", _write)
        monkeypatch.setattr(AcpClient, "_resolve_session_mcp_servers", _resolve)
        try:
            asyncio.run(
                process_adapter_for(ACP_BACKEND_CLAUDE).resolve_spawn(
                    SpawnContext(
                        agent="kirocrew",
                        work_dir=tmp_path,
                        model=None,
                        environ={},
                        home=tmp_path,
                        session=client,
                    )
                )
            )
        finally:
            forget_cached_resolution(ACP_BACKEND_CLAUDE)

        assert steps == [("read", ""), ("seed", "0.84.0"), ("warm", "")]
        assert client._session_mcp_cache == []

    def test_the_installed_version_is_read_from_the_adapters_own_manifest(self, tmp_path):
        pkg = tmp_path / "node_modules" / "@agentclientprotocol" / "claude-agent-acp"
        (pkg / "dist").mkdir(parents=True)
        entry = pkg / "dist" / "index.js"
        entry.write_text("", encoding="utf-8")
        (pkg / "package.json").write_text(
            json.dumps({"name": client_mod.CLAUDE_ACP_NPM_PKG, "version": "0.84.0"}),
            encoding="utf-8",
        )
        assert client_mod._claude_adapter_installed_version(["node", str(entry)]) == "0.84.0"
        (pkg / "package.json").write_text(
            json.dumps({"name": "something-else", "version": "9.9.9"}), encoding="utf-8"
        )
        assert client_mod._claude_adapter_installed_version(["node", str(entry)]) == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["local", "checked_in", "unreadable"])
    async def test_a_deny_gained_before_the_first_prompt_stops_the_session(
        self, tmp_path, agents_dir, monkeypatch, change
    ):
        """A deny added between the writer's read and ``session/new`` is not lost.

        The envelope froze the denies it read; a project file that gained one, or
        became unexaminable, before the first prompt stops the harness.
        """
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"deny": ["Bash(rm:*)"]}}
        )
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="default")
        claude_dir = tmp_path / ".claude"
        real_send = client._send_request

        async def _send(method, params):
            rid = await real_send(method, params)
            if method == "session/new":
                if change == "local":
                    (claude_dir / "settings.local.json").write_text(
                        json.dumps({"permissions": {"deny": ["Bash(rm:*)", "Bash(curl:*)"]}}),
                        encoding="utf-8",
                    )
                elif change == "checked_in":
                    (claude_dir / "settings.json").write_text(
                        json.dumps({"permissions": {"deny": ["Bash(curl:*)"]}}), encoding="utf-8"
                    )
                else:
                    (claude_dir / "settings.json").mkdir()
            return rid

        monkeypatch.setattr(client, "_send_request", _send)
        with pytest.raises(client_mod.AcpError, match="gained deny or ask rules"):
            await client._initialize_session()
        assert "Bash(rm:*)" in json.dumps(dict(sent)["session/new"]["_meta"])
        assert killed == [True]

    @pytest.mark.asyncio
    async def test_an_unchanged_project_deny_set_lets_the_session_start(
        self, tmp_path, agents_dir, monkeypatch
    ):
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"deny": ["Bash(rm:*)"]}}
        )
        _sent, killed = self._adapter_transport(client, monkeypatch, started_mode="default")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.83.0", None])
    async def test_an_adapter_below_the_floor_is_not_pinned(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The pin is verified on 0.84.0 only, so an older adapter keeps today's behaviour."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions", version=version
        )
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert [m for m, _ in sent if m == "session/set_mode"] == []
        assert killed == []

    def test_a_write_after_an_old_adapters_handshake_refuses_the_exclusion(
        self, tmp_path, agents_dir
    ):
        """A re-seed after the handshake reads the version it already has."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._agent_version = "0.83.0"
        client._agent_version_read = True
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []

    @pytest.mark.asyncio
    async def test_the_recorded_plan_session_is_kept_as_it_started(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Pure replay: 0.84.0 started a project ``plan`` file's session in ``plan``.

        The project tier is out of ``settingSources``, yet the adapter still
        resolved the starting mode from it, so the pin reads ``plan`` back and
        sends nothing.
        """
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"defaultMode": "plan"}}
        )
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=None)
        await client._initialize_session()
        assert [m for m, _ in sent if m == "session/set_mode"] == []
        assert killed == []

    @pytest.mark.asyncio
    async def test_the_recorded_auto_file_starts_in_default(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Pure replay: 0.84.0 filtered a project ``auto`` and started in ``default``."""
        assert (
            self._recorded_results("project-auto-filtered")["session/new"][0]["modes"][
                "currentModeId"
            ]
            == "default"
        )
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"defaultMode": "auto"}}
        )
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode=None, scenario="project-auto-filtered"
        )
        await client._initialize_session()
        assert [m for m, _ in sent if m == "session/set_mode"] == []
        assert killed == []

    @pytest.mark.asyncio
    async def test_the_recorded_set_mode_answer_completes_the_pin(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The pin succeeds on the answer 0.84.0 gave to ``set_mode`` ``default`` from ``auto``."""
        recorded = self._recorded_results("project-plan-then-auto-then-pin")
        assert recorded["session/set_mode"] == [{}, {}]
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": "s-new", "modeId": "default"}
        ]
        assert killed == []

    @pytest.mark.asyncio
    async def test_a_crew_authored_session_is_not_pinned(self, tmp_path, agents_dir, monkeypatch):
        """The pin covers the exclusion path only.

        A user ``~/.claude`` mode reaching a session whose file Crew wrote is the
        inherited-config gap the spec names (W2-1), not something this change
        opens, so no ``session/set_mode`` is sent there.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        work = tmp_path / "work"
        work.mkdir()
        client = self._seeded(work, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "permissions" not in json.loads(
            (work / ".claude" / "settings.local.json").read_text()
        )
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert "foo" in _by_name(dict(sent)["session/new"]["mcpServers"])
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == []

    @pytest.mark.asyncio
    async def test_a_crew_opt_in_mode_is_not_overridden(self, tmp_path, agents_dir, monkeypatch):
        """The documented ``auto`` opt-in keeps the mode Crew asked for."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, permission_mode="auto"
        )
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    async def test_a_session_without_crews_array_gains_no_pin(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """No Crew tools, no pin: the adapter's own mode is left alone."""
        client = self._seeded(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        monkeypatch.setattr(client, "_resolve_session_mcp_servers", lambda: [])
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    def test_both_session_establishment_paths_reach_the_pin(self):
        """session/new and a successful session/load each read back and pin."""
        import inspect

        source = inspect.getsource(client_mod.AcpClient._initialize_session)
        assert source.count("await self._pin_claude_starting_mode(") == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_side_read_only_session_starts_confined_and_pinned(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """A Side Chat session gets only the read built-ins and no MCP server.

        The spec's servers are not mounted, every web call is forced to the host by
        an inline ``ask`` rule, settings hooks are off, bypass cannot be the
        starting mode, and an approving mode inherited from the user's settings is
        pinned to ``default`` before the first prompt -- on both establishment paths.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True
        )
        # As the launch does (ClaudeLaunch.resolve_spawn), before session/new.
        await client._prepare_claude_side_read_denies()
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )

        await client._initialize_session()

        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert params["mcpServers"] == []
        options = params["_meta"]["claudeCode"]["options"]
        assert options["tools"] == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
        assert options["strictMcpConfig"] is True
        assert options["allowDangerouslySkipPermissions"] is False
        assert "settingSources" not in options
        assert options["settings"]["permissions"]["ask"] == [
            "Read",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
        ]
        assert options["settings"]["disableAllHooks"] is True
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]
        assert killed == []

    def test_a_side_read_only_session_keeps_the_exclusions_restrictions(self, tmp_path, agents_dir):
        """On the exclusion path the side shape is added to, never swapped for,
        the inline settings: the project's deny rules still ride along and only
        the user tier loads."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        local = tmp_path / ".claude" / "settings.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(
            json.dumps({"permissions": {"deny": ["Read(./secrets/**)"], "ask": ["WebFetch"]}}),
            encoding="utf-8",
        )
        client = AcpClient(
            work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True
        )
        client._claude_adapter_disk_version = "0.84.0"
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is True
        client._claude_side_read_denies = client_mod._claude_side_read_deny_rules()

        options = client._claude_session_meta()["claudeCode"]["options"]

        assert options["settingSources"] == ["user"]
        permissions = options["settings"]["permissions"]
        assert "Read(./secrets/**)" in permissions["deny"]
        assert permissions["ask"] == ["WebFetch", "Read", "Grep", "Glob", "WebSearch"]
        assert options["tools"] == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]

    @pytest.mark.parametrize(("side_read_only", "builtin"), [(True, "fs_read"), (False, "")])
    def test_only_a_side_read_only_session_names_a_claude_read_builtin(
        self, tmp_path, side_read_only, builtin
    ):
        """The mapped name is how the READ_ONLY gate proves a claude read on a
        Side Chat session; on any other session it would bind kiro-cli-named deny
        rules to claude's own tools."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(
            work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, side_read_only=side_read_only
        )
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "_meta": {"claudeCode": {"toolName": "Read"}},
                        "sessionUpdate": "tool_call",
                        "toolCallId": "toolu_1",
                        "title": "Read /etc/hosts",
                        "kind": "read",
                        "rawInput": {"file_path": "/etc/hosts"},
                    }
                },
            )
        )
        event = client._build_permission_event(
            JsonRpcMessage(
                id=7,
                method="session/request_permission",
                params={
                    "toolCall": {
                        "toolCallId": "toolu_1",
                        "title": "Read /etc/hosts",
                        "kind": "read",
                    },
                    "options": [
                        {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
                    ],
                },
            )
        )
        assert event is not None
        assert event.harness_builtin_tool == builtin

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.83.0", None])
    async def test_a_handshake_below_the_floor_stops_a_side_read_only_session(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The read-only shape rides session options an unverified adapter may
        ignore, so the session never reaches its first prompt."""
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True
        )
        await client._prepare_claude_side_read_denies()
        _sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="default", version=version
        )
        with pytest.raises(client_mod.AcpError, match="not verified to honour settingSources"):
            await client._initialize_session()
        assert killed == [True]

    @staticmethod
    def _claude_reads_natively(path, *, deny, allow):
        """Whether Claude Code runs a Read of *path* without asking the host.

        Its documented order: a deny rule from any settings source refuses before
        an allow rule from any source can pre-approve. Only the rule shapes in play
        are modelled: a bare ``Read`` (every path), and a ``//`` pattern, exact or
        ending in ``/**``, with gitignore's backslash escapes. Paths are compared
        in the POSIX spelling Claude Code matches (``C:\\x`` is ``/c/x``).
        """
        spelled = Path(path).as_posix()
        drive = re.match(r"([A-Za-z]):", spelled)
        if drive:
            spelled = f"/{drive.group(1).lower()}{spelled[2:]}"

        def _matches(rule):
            if rule == "Read":
                return True
            if not (rule.startswith("Read(//") and rule.endswith(")")):
                return False
            pattern = re.sub(r"\\(.)", r"\1", rule[len("Read(/") : -1])
            if pattern.endswith("/**"):
                return spelled.startswith(pattern[: -len("**")])
            return spelled == pattern

        if any(_matches(rule) for rule in deny):
            return False
        return any(_matches(rule) for rule in allow)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_an_inherited_read_allow_does_not_open_a_sensitive_path(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """The user's settings allow every read; a side turn still cannot read a sensitive path.

        A Side Chat session keeps the user tier, so a ``Read`` allow rule there (or
        an ``additionalDirectories`` entry) lets claude run Read, Grep and Glob
        without asking the host, and the READ_ONLY gate never sees the call. What
        refuses it is the deny rule the session carries, which Claude Code checks
        first. Launched as the spawn path launches it, every sensitive target is
        denied as itself and as everything under it, while a work-dir file still
        reads natively.
        """
        from kiro_crew.acp.harness import claude as harness_claude
        from kiro_crew.acp.harness.base import SpawnContext
        from kiro_crew.security import sandbox_credential_targets

        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        user = {"permissions": {"allow": ["Read"], "additionalDirectories": [str(home)]}}
        (home / ".claude" / "settings.json").write_text(json.dumps(user), encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setattr(harness_claude, "_claude_acp_argv_cache", (["/x/claude-agent-acp"], ""))
        monkeypatch.setattr(
            harness_claude, "_claude_adapter_installed_version", lambda _argv: "0.84.0"
        )
        client = AcpClient(
            work_dir=work, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True
        )
        if path == "load":
            client._resume_session_id = "s-old"
        await harness_claude.ClaudeLaunch().resolve_spawn(
            SpawnContext(
                agent="kirocrew", work_dir=work, model=None, environ={}, home=home, session=client
            )
        )
        sent, _killed = self._adapter_transport(client, monkeypatch, started_mode="default")

        await client._initialize_session()

        params = dict(sent)["session/load" if path == "load" else "session/new"]
        deny = params["_meta"]["claudeCode"]["options"]["settings"]["permissions"].get("deny", [])
        allow = user["permissions"]["allow"]
        targets = sandbox_credential_targets()
        assert str(home / ".aws") in targets
        for target in targets:
            for sensitive in (Path(target), Path(target) / "x"):
                assert not self._claude_reads_natively(sensitive, deny=deny, allow=allow), sensitive
        assert self._claude_reads_natively(work / "README.md", deny=deny, allow=allow)

    @pytest.mark.asyncio
    async def test_a_side_session_without_its_deny_rules_never_starts(self, tmp_path, monkeypatch):
        """Fail closed: a launch that cannot build the deny rules stops, and an
        envelope with none to carry is refused, so no side session runs on the
        user's allow rules unfenced. Any other session is untouched."""

        def _unresolvable():
            raise RuntimeError("roots could not be canonicalised")

        monkeypatch.setattr(client_mod, "sandbox_credential_targets", _unresolvable)
        side = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True)
        with pytest.raises(client_mod.AcpError, match="could not be built"):
            await side._prepare_claude_side_read_denies()
        with pytest.raises(client_mod.AcpError, match="no sensitive-path deny rules"):
            side._claude_session_meta()

        main = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        await main._prepare_claude_side_read_denies()
        assert main._claude_side_read_denies == ()
        assert main._claude_session_meta() == {"claudeCode": {"options": {}}}

    @pytest.mark.parametrize(
        ("target", "windows", "pattern"),
        [
            ("/home/u/.aws", False, "//home/u/.aws"),
            ("/home/[u]/a*b?\\c", False, "//home/\\[u\\]/a\\*b\\?\\\\c"),
            ("C:\\Users\\u\\.aws", True, "//c/Users/u/.aws"),
            ("D:\\[x]\\.ssh", True, "//d/\\[x\\]/.ssh"),
        ],
    )
    def test_a_target_is_written_as_a_root_anchored_rule(self, target, windows, pattern):
        """``//`` anchors a Claude Code rule at the filesystem root, which it matches in
        POSIX spelling (a drive becomes ``/c``); a real path's glob characters are
        escaped, so the rule fences that path and nothing else."""
        assert client_mod._claude_root_pattern(target, windows=windows) == pattern

    @pytest.mark.parametrize(
        ("version", "expected"),
        [("0.84.0", True), ("0.86.0", True), ("0.81.2", False), ("", False)],
    )
    def test_side_chat_asks_the_installed_adapters_version(self, monkeypatch, version, expected):
        """Side Chat grants tools on claude only from an adapter at the floor."""
        from kiro_crew.agent_sdk.drivers.acp import claude_adapter_honours_session_options

        monkeypatch.setattr(client_mod, "_claude_acp_argv_cache", (["/x/claude-agent-acp"], ""))
        monkeypatch.setattr(client_mod, "_claude_adapter_installed_version", lambda argv: version)
        assert claude_adapter_honours_session_options() is expected

    def test_side_chat_sees_no_adapter_as_unverified(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers.acp import claude_adapter_honours_session_options

        monkeypatch.setattr(client_mod, "_claude_acp_argv_cache", (None, "PATH"))
        assert claude_adapter_honours_session_options() is False

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}),
        ],
    )
    def test_the_project_file_is_never_read_to_decide(self, tmp_path, agents_dir, raw):
        """Any regular project file is left out, whatever it holds."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(raw, encoding="utf-8")
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        assert client._claude_session_meta()["claudeCode"]["options"]["settingSources"] == ["user"]

    def test_a_requested_mode_still_withholds_the_array(self, tmp_path, agents_dir):
        """A mode Crew asked for cannot be pinned without the local file."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {"allow": ["mcp__foo__write"]}})
        client = AcpClient(
            work_dir=tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode="auto",
        )
        client._write_claude_local_settings()
        assert client._session_mcp_servers() == []
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    def test_a_symlinked_project_file_still_withholds_the_array(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._session_mcp_servers() == []
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    def test_the_cached_array_is_not_aliased_to_callers(self, tmp_path, agents_dir):
        # The two call sites splat this list into their params; handing out the
        # cache itself would let one session/load mutation reach the next.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        first = client._session_mcp_servers()
        first.clear()
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_a_pooled_stub_the_spec_never_references_does_not_mount(self, tmp_path, agents_dir):
        """The `tools` allowlist covers the pooled half of the array too.

        The gateway rewriter writes each agent's overlay from the GLOBAL settings
        file as well as the agent's own spec, so the overlay can carry a stub for
        a server this agent's ``tools`` never references -- and a stub is that
        server. Before the mirror placed the stubs, the shared append mounted it
        anyway, so a claude session received a pooled server the same spec would
        NOT mount on kiro-cli or codex. The unreferenced server is the widening
        direction, and this is the parity this test pins.
        """
        _write_spec(agents_dir, servers={"direct": {"command": "/bin/direct"}}, tools=["@direct"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._pooled_broker_stubs = lambda: [  # type: ignore[method-assign]
            {"name": "unreferenced", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]
        names = set(_by_name(client._session_mcp_servers()))
        assert "direct" in names
        assert "unreferenced" not in names
        # And the shared append is inert for claude, so nothing re-adds it later.
        assert client._pooled_mcp_servers() == []

    def test_the_shared_append_still_pools_for_kiro(self, tmp_path):
        """The mirror-less backend keeps the shared append.

        kiro-cli reads the agent spec itself via ``--agent``, so the injected
        array is the ONLY channel its broker stubs arrive on -- the injection
        outranking the same-named spec entry is what pools them. Making the
        append inert for every MIRRORED backend must not take kiro's away.
        """
        client = AcpClient(work_dir=tmp_path, agent="kirocrew")
        stub = {"name": "pooled", "command": "/stub", "args": [], "env": [], "type": "stdio"}
        client._pooled_broker_stubs = lambda: [dict(stub)]  # type: ignore[method-assign]
        assert client._pooled_mcp_servers() == [stub]

    def _overlay_with_a_stub(self, root: Path) -> Path:
        """A user-level overlay holding one broker stub for ``kirocrew``."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        overlay = root / "mcp-gateway" / "agents"
        overlay.mkdir(parents=True)
        (overlay / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "pooled": {
                            _WRAPPER_MARKER: True,
                            "command": "/stub",
                            "args": ["--target-command=user-level-cmd"],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return overlay

    def test_a_project_agent_takes_no_stub_from_the_user_level_overlay(self, tmp_path, agents_dir):
        """The client's own checkout reaches the overlay lookup.

        Drives the real call chain rather than the module function, so a call site
        that stops passing ``work_dir`` -- or is wrapped in a guard that is never
        true -- fails here instead of silently resolving the overlay by name.
        """
        overlay = self._overlay_with_a_stub(tmp_path / "gw")
        checkout = tmp_path / "checkout"
        _write_project_spec(checkout, servers={"proj": {"command": "/bin/proj"}}, tools=None)

        running_the_project_agent = AcpClient(work_dir=checkout, agent="kirocrew")
        running_the_project_agent._mcp_gateway_overlay = overlay
        assert running_the_project_agent._pooled_broker_stubs() == []

        # Control: the same overlay and agent name, a checkout that declares
        # neither, so the user-level stub is still this session's.
        elsewhere = AcpClient(work_dir=tmp_path / "plain", agent="kirocrew")
        elsewhere._mcp_gateway_overlay = overlay
        assert [e["name"] for e in elsewhere._pooled_broker_stubs()] == ["pooled"]
