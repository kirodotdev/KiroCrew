"""Per-tool MCP deny on opencode.

A switched-off tool becomes a ``deny`` rule in the ``permission`` config Crew seeds on
``OPENCODE_CONFIG_CONTENT``, keyed on the harness's own tool id, and the narrowed
server stays mounted. The routing read-back evaluates the resolved rules the way the
harness does (last match wins), and a rule that is not in force withholds its server.

goose is pinned beside it: it still withholds a narrowed server whole.

The unit half drives the projection and the client's read-back. The live half drives
the real harness against a local fake model, so no credential and no remote model
are involved: a denied tool must be hidden while its sibling on the same server
still runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from real_adapter_gate import MEASURED_OPENCODE_VERSION, require_real_adapter

from kiro_crew import agent as agent_mod
from kiro_crew import platform_compat
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import opencode as oc_harness
from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE
from kiro_crew.providers.mirrors import opencode as opencode_mod
from kiro_crew.providers.mirrors.goose import goose_projection

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory (same seam as the
    opencode and codex session-MCP tests)."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    managed = {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: dict(managed[name]) if name in managed else None,
    )
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, servers: dict, tools: list) -> None:
    spec = {"name": "kirocrew", "mcpServers": servers, "tools": tools}
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _names(projection) -> list[str]:
    return [str(e.get("name")) for e in projection.params["mcpServers"]]


class TestOpencodeRuleEvaluation:
    def test_the_wildcard_is_the_harnesss(self):
        match = oc_harness._opencode_wildcard_match
        assert match("probe_secret", "*")
        assert match("probe_secret", "probe_*")
        assert match("probe_secret", "probe_secre?")
        assert not match("probe_secret", "probe")
        assert not match("probe.secret", "probe_secret")
        assert match("git", "git *") and match("git push", "git *")

    @staticmethod
    def _harness_regex(value: str, pattern: str) -> bool:
        """opencode 1.18.30's own wildcard regex, the oracle for short inputs only."""
        import re

        specials = set(".+^${}()|[]\\")
        value = value.replace("\\", "/")
        pattern = pattern.replace("\\", "/")
        body = "".join(
            ".*" if c == "*" else "." if c == "?" else "\\" + c if c in specials else c
            for c in pattern
        )
        if body.endswith(" .*"):
            body = body[:-3] + "( .*)?"
        return re.fullmatch(body, value, re.DOTALL) is not None

    def test_the_glob_agrees_with_the_harness_regex(self):
        """Every pattern over a small alphabet, against every short value."""
        import itertools
        import random

        rng = random.Random(7)
        alphabet = "ab*? \\.\n"
        values = ["".join(t) for n in range(5) for t in itertools.product("ab .\n", repeat=n)]
        patterns = [
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 7))) for _ in range(600)
        ]
        patterns += ["git *", "a *", " *", "*", "?", "a?b*", "\\*", "a\\b"]
        match = oc_harness._opencode_wildcard_match
        for pattern in patterns:
            for value in values:
                assert match(value, pattern) == self._harness_regex(value, pattern), (
                    value,
                    pattern,
                )

    @staticmethod
    def _glob_steps(call) -> tuple[object, int]:
        """Run *call* and count the lines ``_glob_fullmatch`` executes.

        A step count, not a clock: it is the same on a busy CI runner as on an idle
        laptop. Zero steps means the match never went through the glob walk at all.
        """
        import sys

        code = oc_harness._glob_fullmatch.__code__
        steps = 0

        def tracer(frame, event, _arg):
            nonlocal steps
            if frame.f_code is not code:
                return None
            if event == "line":
                steps += 1
            return tracer

        previous = sys.gettrace()
        sys.settrace(tracer)
        try:
            result = call()
        finally:
            sys.settrace(previous)
        return result, steps

    @staticmethod
    def _bound(value: str, pattern: str) -> int:
        """Lines a backtrack-free walk may take: a small constant per (value, pattern)
        cell. A backtracking match of ``*?``x40 needs exponentially more."""
        return 12 * (len(value) + 1) * (len(pattern) + 1)

    def test_a_pathological_key_is_matched_in_bounded_steps(self):
        """``*?*?...Z`` makes a backtracking regex take seconds on a 30-char tool id,
        holding the GIL. The glob walk stays within its step bound and still answers
        correctly."""
        pattern = "*?" * 40 + "Z"
        match = oc_harness._opencode_wildcard_match
        for value, expected in (("a" * 30, False), ("a" * 45 + "Z", True), ("a" * 30 + "Z", False)):
            result, steps = self._glob_steps(lambda v=value: match(v, pattern))
            assert result is expected, value
            assert 0 < steps <= self._bound(value, pattern), (value, steps)

    def test_the_step_count_grows_with_the_input_not_exponentially(self):
        """Doubling both sides at most quadruples the work, plus slack."""
        match = oc_harness._opencode_wildcard_match
        _, small = self._glob_steps(lambda: match("a" * 15, "*?" * 20 + "Z"))
        _, large = self._glob_steps(lambda: match("a" * 30, "*?" * 40 + "Z"))
        assert 0 < small < large <= 6 * small, (small, large)

    def test_the_read_back_survives_a_pathological_operator_key(self):
        key = "*?" * 40 + "Z"
        resolved = {"permission": {key: "allow", "*": "ask", "probe_secret": "deny"}}
        result, steps = self._glob_steps(
            lambda: oc_harness._opencode_unenforced_denies(resolved, "permission", ["probe_secret"])
        )
        assert result == frozenset()
        # One glob per rule tried, each within its own bound.
        assert 0 < steps <= 4 * self._bound("probe_secret", key)

    def test_a_trailing_deny_is_in_force(self):
        resolved = {"permission": {"bash": "allow", "*": "ask", "probe_secret": "deny"}}
        assert (
            oc_harness._opencode_unenforced_denies(resolved, "permission", ["probe_secret"])
            == frozenset()
        )

    def test_a_deny_a_lower_source_put_before_the_star_is_not_in_force(self):
        """Measured on opencode 1.18.30: a global ``"probe_secret": "allow"`` keeps
        its place, the seed overrides only its value, and the seed's ``"*"`` lands
        after it, so the deny reads as ask."""
        resolved = {"permission": {"probe_secret": "deny", "*": "ask"}}
        assert oc_harness._opencode_unenforced_denies(
            resolved, "permission", ["probe_secret"]
        ) == frozenset({"probe_secret"})

    def test_an_agent_override_is_appended_and_can_outrank(self):
        resolved = {
            "permission": {"*": "ask", "probe_secret": "deny", "a_b": "deny"},
            "agent": {"build": {"permission": {"*": "ask"}}, "plan": {"model": "x"}},
        }
        assert oc_harness._opencode_unenforced_denies(
            resolved, "permission", ["probe_secret", "a_b"]
        ) == frozenset({"probe_secret", "a_b"})

    def test_a_later_pattern_rule_on_the_same_tool_unhides_it(self):
        """The harness hides a tool only when the last rule naming it denies EVERY
        pattern."""
        resolved = {"permission": {"*": "ask", "probe_secret": "deny", "probe_*": {"x": "allow"}}}
        assert oc_harness._opencode_unenforced_denies(
            resolved, "permission", ["probe_secret"]
        ) == frozenset({"probe_secret"})

    def test_the_seeded_rules_are_read_from_the_seed(self):
        seed = json.dumps({"permission": {"*": "ask", "a_b": "deny", "c_d": "deny"}})
        assert oc_harness._opencode_seeded_deny_rules(seed, "permission") == ("a_b", "c_d")
        assert oc_harness._opencode_seeded_deny_rules('{"permission": "ask"}', "permission") == ()


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


class TestOpencodeReadBack:
    @pytest.fixture
    def client(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        c._session_harness_deny_rules = ("probe_secret",)
        return c

    def test_the_seed_puts_each_deny_after_the_star(self, client, monkeypatch):
        monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
        seed = json.loads(
            oc_harness._opencode_routing_config(
                ACP_BACKEND_OPENCODE, client._session_harness_deny_rules
            )
        )
        assert list(seed["permission"].items()) == [("*", "ask"), ("probe_secret", "deny")]

    def test_no_rules_keeps_the_plain_ask(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        assert (
            json.loads(
                oc_harness._opencode_routing_config(
                    ACP_BACKEND_OPENCODE, c._session_harness_deny_rules
                )
            )["permission"]
            == "ask"
        )

    def _read_back(self, client, monkeypatch, resolved: dict) -> tuple[str, str]:
        monkeypatch.setattr(
            oc_harness.subprocess_mod, "run", lambda *a, **k: _completed(json.dumps(resolved))
        )
        seed = json.dumps({"permission": {"*": "ask", "probe_secret": "deny"}})
        return oc_harness._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, ["opencode", "debug", "config"], seed
        )

    def test_the_seeded_shape_passes_with_every_deny_in_force(self, client, monkeypatch):
        issue = self._read_back(
            client, monkeypatch, {"permission": {"*": "ask", "probe_secret": "deny"}}
        )
        assert issue == ("", "")
        assert client._opencode_denies_unenforced == frozenset()

    def test_an_outranked_deny_is_recorded_not_refused(self, client, monkeypatch):
        issue = self._read_back(
            client, monkeypatch, {"permission": {"probe_secret": "deny", "*": "ask"}}
        )
        assert issue == ("", "")
        assert client._opencode_denies_unenforced == frozenset({"probe_secret"})

    def test_an_outranked_deny_on_a_native_mount_refuses_the_session(self, client, monkeypatch):
        """opencode's own config mounts ``probe`` and names ``probe_secret``, so the
        seed's deny lands behind ``"*"``. Withholding Crew's array element cannot
        reach that mount, so the session is refused."""
        issue, remedy = self._read_back(
            client,
            monkeypatch,
            {"permission": {"probe_secret": "deny", "*": "ask"}, "mcp": {"probe": {}}},
        )
        assert issue and "probe" in issue and "own config" in issue
        assert "opencode config" in remedy

    def test_a_native_mount_whose_deny_holds_starts(self, client, monkeypatch):
        """A plain native mount: the seeded deny lands after ``"*"`` and covers it."""
        issue = self._read_back(
            client,
            monkeypatch,
            {"permission": {"*": "ask", "probe_secret": "deny"}, "mcp": {"probe": {}}},
        )
        assert issue == ("", "")

    def test_a_remapped_tool_on_a_native_mount_refuses_the_session(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """``apply``/``patch`` fuses to ``apply_patch``, which opencode judges by its
        builtin ``edit`` rule, so no deny rule can be written for it. The projection
        withholds the array copy; a native mount of ``apply`` would still serve it."""
        _write_spec(
            agents_dir,
            {"apply": {"command": "/bin/x", "disabledTools": ["patch"]}},
            ["@apply"],
        )
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        assert "apply" not in [e["name"] for e in c._resolve_session_mcp_servers()]
        assert c._session_harness_deny_rules == ()
        assert c._session_mcp_unhonoured == frozenset({"apply"})
        issue, _remedy = self._read_back(
            c, monkeypatch, {"permission": {"*": "ask"}, "mcp": {"apply": {}}}
        )
        assert issue and "apply" in issue and "own config" in issue

    def test_a_native_mount_whose_tools_are_untouched_starts(
        self, agents_dir, tmp_path, monkeypatch
    ):
        _write_spec(agents_dir, {"apply": {"command": "/bin/x"}}, ["@apply"])
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        c._resolve_session_mcp_servers()
        assert c._session_mcp_unhonoured == frozenset()
        issue = self._read_back(c, monkeypatch, {"permission": {"*": "ask"}, "mcp": {"apply": {}}})
        assert issue == ("", "")

    def test_an_unhonoured_server_not_mounted_natively_starts(
        self, agents_dir, tmp_path, monkeypatch
    ):
        """Withheld from the array, not mounted anywhere else: nothing to refuse."""
        _write_spec(
            agents_dir,
            {"apply": {"command": "/bin/x", "disabledTools": ["patch"]}},
            ["@apply"],
        )
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        assert "apply" not in [e["name"] for e in c._resolve_session_mcp_servers()]
        issue = self._read_back(c, monkeypatch, {"permission": {"*": "ask"}, "mcp": {"other": {}}})
        assert issue == ("", "")

    def test_an_outranked_deny_on_another_native_server_starts(self, client, monkeypatch):
        issue = self._read_back(
            client,
            monkeypatch,
            {"permission": {"probe_secret": "deny", "*": "ask"}, "mcp": {"other": {}}},
        )
        assert issue == ("", "")
        assert client._opencode_denies_unenforced == frozenset({"probe_secret"})

    def test_an_operators_own_outranked_deny_is_still_refused(self, client, monkeypatch):
        issue, _remedy = self._read_back(
            client,
            monkeypatch,
            {"permission": {"bash": {"pwd": "deny"}, "*": "ask", "probe_secret": "deny"}},
        )
        assert issue and "deny" in issue


class TestOpencodeDeniesInForce:
    """Which deny rules are in force comes from the seed, never from intent."""

    @pytest.fixture
    def client(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        c._session_harness_deny_rules = ("probe_secret",)
        return c

    def test_a_seed_that_carries_the_rule_keeps_it(self, client):
        client._opencode_config_content = json.dumps(
            {"permission": {"*": "ask", "probe_secret": "deny"}}
        )
        assert (
            oc_harness.settle_opencode_denies(client, client._opencode_config_content)
            == frozenset()
        )
        assert client._opencode_denies_in_force == {"probe_secret"}

    def test_a_seed_that_left_the_rule_out_loses_it(self, client):
        """The routing value is not a rule map, so no deny was written."""
        client._opencode_config_content = json.dumps({"permission": "ask"})
        assert oc_harness.settle_opencode_denies(client, client._opencode_config_content) == {
            "probe_secret"
        }
        assert client._opencode_denies_in_force == frozenset()

    def test_an_outranked_rule_is_lost_too(self, client):
        client._opencode_config_content = json.dumps(
            {"permission": {"*": "ask", "probe_secret": "deny"}}
        )
        client._opencode_denies_unenforced = frozenset({"probe_secret"})
        assert oc_harness.settle_opencode_denies(client, client._opencode_config_content) == {
            "probe_secret"
        }

    def test_a_lost_rule_withholds_its_server(self, agents_dir, tmp_path):
        _write_spec(
            agents_dir,
            {"narrowed": {"command": "/bin/x", "disabledTools": ["danger"]}},
            ["@narrowed"],
        )
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        assert "narrowed" in [e["name"] for e in c._resolve_session_mcp_servers()]
        c._opencode_config_content = json.dumps({"permission": "ask"})
        assert oc_harness.settle_opencode_denies(c, c._opencode_config_content) == {
            "narrowed_danger"
        }
        assert "narrowed" not in [e["name"] for e in c._resolve_session_mcp_servers()]

    def test_a_re_projection_that_narrows_a_native_mount_refuses(self, tmp_path):
        """The read-back judged one projection; a later re-read narrowed a server
        opencode mounts natively. Withholding the array copy cannot reach it."""
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        c._opencode_native_mounts = frozenset({"second"})
        c._session_mcp_unhonoured = frozenset({"second"})
        with pytest.raises(oc_harness.AcpToolGateUnroutable, match="second"):
            oc_harness.refuse_native_mount_of_unhonoured(c)

    def test_a_re_projection_that_narrows_only_array_servers_starts(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        c._opencode_native_mounts = frozenset({"other"})
        c._session_mcp_unhonoured = frozenset({"second"})
        oc_harness.refuse_native_mount_of_unhonoured(c)

    def test_the_read_back_keeps_the_native_mounts(self, tmp_path, monkeypatch):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        resolved = {"permission": {"*": "ask"}, "mcp": {"second": {}, "third": {}}}
        monkeypatch.setattr(
            oc_harness.subprocess_mod, "run", lambda *a, **k: _completed(json.dumps(resolved))
        )
        issue = oc_harness._verify_opencode_routing(
            c, ACP_BACKEND_OPENCODE, ["opencode", "debug", "config"], '{"permission": "ask"}'
        )
        assert issue == ("", "")
        assert c._opencode_native_mounts == {"second", "third"}

    def test_the_spawn_arm_rechecks_after_the_re_projection(self):
        import inspect

        source = inspect.getsource(oc_harness.OpencodeLaunch.resolve_spawn)
        arm = source[source.index("lost = settle_opencode_denies(") :]
        assert arm.index("session._resolve_session_mcp_servers") < arm.index(
            "refuse_native_mount_of_unhonoured(session)"
        )

    def test_the_spawn_arm_settles_after_the_read_back(self):
        """The arm calls the settle step, and nothing else sets the in-force set."""
        import inspect

        source = inspect.getsource(oc_harness)
        assert "lost = settle_opencode_denies(session, self._config_content)" in source
        assert source.count("session._opencode_denies_in_force = ") == 1

    def test_a_narrowed_server_and_the_control_plane_are_withheld(self, agents_dir):
        _write_spec(
            agents_dir,
            {
                "narrowed": {"command": "/bin/x", "disabledTools": ["danger"]},
                "kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]},
                "plain": {"command": "/bin/y"},
            },
            ["@narrowed", "@kirocrew-core", "@kirocrew-cron", "@plain"],
        )
        projection = goose_projection("kirocrew")
        assert set(_names(projection)) == {"plain", "kirocrew-cron"}
        assert projection.denied_tools == frozenset()
        assert projection.unhonoured_servers == {"narrowed", "kirocrew-core"}

    def test_a_zero_tools_spec_carries_the_total_ban(self, agents_dir):
        """``"tools": []`` must reach the client on goose too, as it does on opencode."""
        _write_spec(agents_dir, {"plain": {"command": "/bin/y"}}, [])
        assert session_mcp.session_mcp_projection("kirocrew").zero_tools is True
        assert goose_projection("kirocrew").zero_tools is True

    def test_a_stub_of_a_narrowed_server_is_not_re_added(self, agents_dir):
        _write_spec(
            agents_dir,
            {"narrowed": {"command": "/bin/x", "disabledTools": ["danger"]}},
            ["@narrowed"],
        )
        stub = {"name": "narrowed", "command": "/opt/stub", "args": [], "env": []}
        projection = goose_projection(
            "kirocrew", stub_server_names=("narrowed",), stub_elements=[stub]
        )
        assert _names(projection) == []


class TestTheClientWiring:
    def test_a_rule_not_in_force_withholds_its_server_on_a_reprojection(self, agents_dir, tmp_path):
        _write_spec(
            agents_dir,
            {"narrowed": {"command": "/bin/x", "disabledTools": ["danger"]}},
            ["@narrowed"],
        )
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        mounted = c._resolve_session_mcp_servers()
        assert "narrowed" in [e["name"] for e in mounted]
        assert c._session_harness_deny_rules == ("narrowed_danger",)
        c._opencode_denies_in_force = frozenset()
        withheld = c._resolve_session_mcp_servers()
        assert "narrowed" not in [e["name"] for e in withheld]
        assert c._session_mcp_unhonoured == frozenset({"narrowed"})

    def test_a_member_mount_never_re_adds_an_unhonoured_server(self, tmp_path):
        c = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        c._session_mcp_unhonoured = frozenset({"kirocrew-dashboard"})
        assert c._member_mount_withheld(
            "kirocrew-dashboard", "dispatch", frozenset({"kirocrew-dashboard"}), frozenset()
        )


# ── live: the real harnesses, a fake model, one probe server ────────────────

_PROBE_MCP = r"""
import json, os, sys
LOG = os.environ["CALL_LOG"]
TOOLS = [{"name": n, "description": "Probe tool " + n,
          "inputSchema": {"type": "object", "properties": {}}} for n in ("secret", "public")]
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    m, rid = req.get("method"), req.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req["params"].get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "probe", "version": "0"}}})
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif m == "tools/call":
        with open(LOG, "a") as f:
            f.write(req["params"]["name"] + "\n")
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": "word-from-" + req["params"]["name"]}]}})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
"""


class _FakeModel:
    """An OpenAI-compatible chat endpoint that calls *calls* in order, when offered.

    Each request records the tool names it offered. The model asks for the next tool
    in *calls* that the request offers, one per turn, then answers ``done``.
    """

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.offered: list[list[str]] = []
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                self._json({"object": "list", "data": [{"id": "m", "object": "model"}]})

            def _json(self, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                tools = [t.get("function", {}).get("name") for t in req.get("tools") or []]
                if tools:
                    model.offered.append(tools)
                done = sum(1 for m in req.get("messages", []) if m.get("role") == "tool")
                pending = [t for t in model.calls if t in tools][done:]
                if pending:
                    msg = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{done}",
                                "type": "function",
                                "function": {"name": pending[0], "arguments": "{}"},
                            }
                        ],
                    }
                    finish = "tool_calls"
                else:
                    msg, finish = {"role": "assistant", "content": "done"}, "stop"
                if req.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    delta = {"role": "assistant"}
                    if msg.get("content"):
                        delta["content"] = msg["content"]
                    if msg.get("tool_calls"):
                        delta["tool_calls"] = [dict(tc, index=0) for tc in msg["tool_calls"]]
                    base = {
                        "id": "x",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "m",
                    }
                    chunks = [
                        dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                        dict(
                            base,
                            choices=[{"index": 0, "delta": {}, "finish_reason": finish}],
                            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                        ),
                    ]
                    for c in chunks:
                        self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self._json(
                    {
                        "id": "x",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "m",
                        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def _isolated_home(root: Path) -> dict[str, str]:
    home = root / "home"
    (home / ".config").mkdir(parents=True)
    # The harnesses unpack native modules into the temp dir; keep them in tmp_path.
    tmp = root / "tmp"
    tmp.mkdir()
    return {
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
    }


def _drive_acp(
    argv: list[str], env: dict[str, str], cwd: Path, servers: list[dict], decide
) -> list[dict]:
    """One ``session/new`` with *servers* and one prompt; returns every frame.

    *decide(update_by_call, tool_call)* picks the option kind for a permission
    request: ``allow_once`` or ``reject_once``.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
    )
    assert proc.stdin is not None and proc.stdout is not None
    frames: list[dict] = []
    calls: dict[str, dict] = {}
    waiting: dict[int, list] = {}
    lock = threading.Lock()

    def write(msg: dict) -> None:
        with lock:
            proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
            proc.stdin.flush()  # type: ignore[union-attr]

    def reader() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            frames.append(msg)
            if msg.get("method") == "session/update":
                update = msg["params"]["update"]
                if update.get("sessionUpdate") == "tool_call":
                    calls[update.get("toolCallId")] = update
            elif msg.get("method") == "session/request_permission":
                tool_call = msg["params"].get("toolCall") or {}
                want = decide(calls.get(tool_call.get("toolCallId")), tool_call)
                options = msg["params"]["options"]
                pick = next((o for o in options if o["kind"] == want), None)
                assert pick is not None, options
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "result": {
                            "outcome": {"outcome": "selected", "optionId": pick["optionId"]}
                        },
                    }
                )
            elif "id" in msg and "method" in msg:
                write(
                    {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}}
                )
            elif msg.get("id") in waiting:
                waiting[msg["id"]][1] = msg
                waiting[msg["id"]][0].set()

    def call(rid: int, method: str, params: dict, timeout: float) -> dict:
        waiting[rid] = [threading.Event(), None]
        write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        assert waiting[rid][0].wait(timeout), f"{method} timed out"
        return waiting[rid][1]

    threading.Thread(target=reader, daemon=True).start()
    try:
        call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 120)
        new = call(2, "session/new", {"cwd": str(cwd), "mcpServers": servers}, 120)
        assert "result" in new, new
        prompt = [{"type": "text", "text": "Call the probe tools, then say done."}]
        done = call(
            3, "session/prompt", {"sessionId": new["result"]["sessionId"], "prompt": prompt}, 300
        )
        assert "result" in done, done
    finally:
        # The whole tree: the harness spawns the MCP child, which a plain kill of the
        # harness would leave running. Cross-platform, and the wait always runs.
        try:
            platform_compat.kill_process_tree(proc.pid, platform_compat.SIGKILL)
        except Exception:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
    return frames


def _probe_spec(agents_dir: Path, probe: Path, log: Path) -> None:
    _write_spec(
        agents_dir,
        {
            "probe": {
                "command": sys.executable,
                "args": [str(probe)],
                "env": {"CALL_LOG": str(log)},
                "disabledTools": ["secret"],
            }
        },
        ["@probe"],
    )


def _self_served_bin(backend: str) -> str | None:
    resolved, _search = client_mod._resolve_self_served_bin(backend)
    return resolved or None


@pytest.mark.real_adapter
def test_real_opencode_hides_a_denied_tool_and_keeps_its_sibling(agents_dir, tmp_path, monkeypatch):
    """The opencode projection, end to end through Crew's own seed and read-back.

    The spec switches off ``secret`` on ``probe``. Crew's projection mounts ``probe``
    and seeds ``probe_secret: deny`` after ``"*": "ask"``; the real read-back finds it
    in force; and in the live session the model is never offered ``probe_secret``,
    while ``probe_public`` is offered, asks, and runs.
    """
    binary = _self_served_bin(ACP_BACKEND_OPENCODE)
    require_real_adapter(
        binary, what="opencode", install=f"npm i -g opencode-ai@{MEASURED_OPENCODE_VERSION}"
    )
    assert binary is not None
    probe = tmp_path / "probe_mcp.py"
    probe.write_text(_PROBE_MCP, encoding="utf-8")
    log = tmp_path / "calls.log"
    _probe_spec(agents_dir, probe, log)
    model = _FakeModel(["probe_secret", "probe_public"])
    try:
        provider = {
            "model": "fake/m",
            "provider": {
                "fake": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": model.url + "/v1", "apiKey": "x"},
                    "models": {"m": {"tool_call": True}},
                }
            },
        }
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(provider))
        isolated = _isolated_home(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        client = AcpClient(work_dir=work, acp_backend=ACP_BACKEND_OPENCODE, extra_env=isolated)
        servers = client._resolve_session_mcp_servers()
        assert [e["name"] for e in servers] == ["probe"]
        seed = oc_harness._opencode_routing_config(
            ACP_BACKEND_OPENCODE, client._session_harness_deny_rules
        )
        assert oc_harness._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, [binary, "debug", "config"], seed
        ) == ("", "")
        assert client._opencode_denies_unenforced == frozenset()

        env = {**os.environ, **isolated, "OPENCODE_CONFIG_CONTENT": seed}
        asked: list[str] = []

        def decide(_update, tool_call):
            asked.append(str(tool_call.get("title")))
            return "allow_once"

        _drive_acp([binary, "acp"], env, work, servers, decide)
    finally:
        model.close()
    offered = {name for request in model.offered for name in request}
    assert "probe_public" in offered, model.offered
    assert "probe_secret" not in offered, model.offered
    assert log.read_text(encoding="utf-8").split() == ["public"]
    assert asked == ["probe_public"], "the sibling tool must still ask per call"


@pytest.mark.real_adapter
def test_real_opencode_an_outranking_global_key_is_detected(agents_dir, tmp_path):
    """The last-match-wins hazard, against the real config resolution.

    A global ``"probe_secret": "allow"`` keeps its earlier place when the seed is
    merged, so the seed's ``"*": "ask"`` lands after it and outranks the deny. The
    read-back must find that rule not in force rather than report the session as
    narrowed.
    """
    binary = _self_served_bin(ACP_BACKEND_OPENCODE)
    require_real_adapter(
        binary, what="opencode", install=f"npm i -g opencode-ai@{MEASURED_OPENCODE_VERSION}"
    )
    assert binary is not None
    isolated = _isolated_home(tmp_path)
    (Path(isolated["XDG_CONFIG_HOME"]) / "opencode").mkdir()
    (Path(isolated["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json").write_text(
        json.dumps({"permission": {"probe_secret": "allow"}}), encoding="utf-8"
    )
    work = tmp_path / "work"
    work.mkdir()
    client = AcpClient(work_dir=work, acp_backend=ACP_BACKEND_OPENCODE, extra_env=isolated)
    client._session_harness_deny_rules = ("probe_secret",)
    seed = oc_harness._opencode_routing_config(
        ACP_BACKEND_OPENCODE, client._session_harness_deny_rules
    )
    assert oc_harness._verify_opencode_routing(
        client, ACP_BACKEND_OPENCODE, [binary, "debug", "config"], seed
    ) == ("", "")
    assert client._opencode_denies_unenforced == frozenset({"probe_secret"})


@pytest.mark.real_adapter
def test_real_opencode_a_native_mount_with_an_outranked_deny_refuses(agents_dir, tmp_path):
    """opencode's own project config mounts ``probe`` and names ``probe_secret``.

    The seed's deny keeps that earlier place, so ``"*": "ask"`` outranks it; and the
    native mount is out of reach of Crew's array withhold. The real read-back must
    refuse the session.
    """
    binary = _self_served_bin(ACP_BACKEND_OPENCODE)
    require_real_adapter(
        binary, what="opencode", install=f"npm i -g opencode-ai@{MEASURED_OPENCODE_VERSION}"
    )
    assert binary is not None
    isolated = _isolated_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "opencode.json").write_text(
        json.dumps(
            {
                "mcp": {"probe": {"type": "local", "command": [sys.executable, "-c", "pass"]}},
                "permission": {"probe_secret": "deny"},
            }
        ),
        encoding="utf-8",
    )
    client = AcpClient(work_dir=work, acp_backend=ACP_BACKEND_OPENCODE, extra_env=isolated)
    client._session_harness_deny_rules = ("probe_secret",)
    seed = oc_harness._opencode_routing_config(
        ACP_BACKEND_OPENCODE, client._session_harness_deny_rules
    )
    issue, _remedy = oc_harness._verify_opencode_routing(
        client, ACP_BACKEND_OPENCODE, [binary, "debug", "config"], seed
    )
    assert issue and "probe" in issue and "own config" in issue


@pytest.mark.real_adapter
def test_real_opencode_the_seeded_deny_covers_a_native_mount(agents_dir, tmp_path, monkeypatch):
    """A plain native mount of ``probe`` (no key naming the tool) starts, and the
    seeded deny hides ``probe_secret`` there too, while ``probe_public`` still runs.
    The whole-server withhold on main left this native ``probe_secret`` callable."""
    binary = _self_served_bin(ACP_BACKEND_OPENCODE)
    require_real_adapter(
        binary, what="opencode", install=f"npm i -g opencode-ai@{MEASURED_OPENCODE_VERSION}"
    )
    assert binary is not None
    probe = tmp_path / "probe_mcp.py"
    probe.write_text(_PROBE_MCP, encoding="utf-8")
    log = tmp_path / "calls.log"
    _probe_spec(agents_dir, probe, log)
    model = _FakeModel(["probe_secret", "probe_public"])
    try:
        provider = {
            "model": "fake/m",
            "provider": {
                "fake": {
                    "npm": "@ai-sdk/openai-compatible",
                    "options": {"baseURL": model.url + "/v1", "apiKey": "x"},
                    "models": {"m": {"tool_call": True}},
                }
            },
        }
        monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps(provider))
        isolated = _isolated_home(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        (work / "opencode.json").write_text(
            json.dumps(
                {
                    "mcp": {
                        "probe": {
                            "type": "local",
                            "command": [sys.executable, str(probe)],
                            "environment": {"CALL_LOG": str(log)},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        client = AcpClient(work_dir=work, acp_backend=ACP_BACKEND_OPENCODE, extra_env=isolated)
        client._resolve_session_mcp_servers()
        seed = oc_harness._opencode_routing_config(
            ACP_BACKEND_OPENCODE, client._session_harness_deny_rules
        )
        assert oc_harness._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, [binary, "debug", "config"], seed
        ) == ("", "")
        env = {**os.environ, **isolated, "OPENCODE_CONFIG_CONTENT": seed}
        # The native mount only: no array element, so nothing but opencode's own
        # config puts probe in front of the model.
        _drive_acp([binary, "acp"], env, work, [], lambda _u, _t: "allow_once")
    finally:
        model.close()
    offered = {name for request in model.offered for name in request}
    assert "probe_public" in offered, model.offered
    assert "probe_secret" not in offered, model.offered
    assert log.read_text(encoding="utf-8").split() == ["public"]


def test_the_live_probe_is_valid_python():
    """The live tests skip where the harness is absent; keep their probe parseable."""
    import ast

    ast.parse(_PROBE_MCP)
    assert opencode_mod.opencode_tool_id("probe", "secret") == "probe_secret"
