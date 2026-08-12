"""Tests for authoring agent templates through the dashboard.

Covers ``POST /api/agents/installed`` and ``PUT /api/agents/installed/{name}``.

The load-bearing constraint behind most of these: kiro-cli validates
``~/.kiro/agents/*.json`` with serde ``deny_unknown_fields`` and rejects the
ENTIRE spec on any unknown key, then silently falls back to the default agent.
A spec written with a field kiro-cli does not know is not a degraded template —
it is a template that does not exist, while the session looks like it is running
the user's agent. Several tests below exist only to pin that.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.agent_discovery import clear_list_agents_cache
from kiro_crew.dashboard.handlers import agents as agents_mod
from kiro_crew.dashboard.handlers.agents import (
    _template_is_writable,
    api_agents_installed_create,
    api_agents_installed_update,
)

#: Keys kiro-cli accepts in an agent spec. Anything outside this set makes the
#: whole file unloadable, so a written spec must never contain one.
VALID_SPEC_KEYS = {
    "name",
    "description",
    "model",
    "prompt",
    "tools",
    "allowedTools",
    "mcpServers",
    "resources",
    "includeMcpJson",
    "hooks",
    "toolsSettings",
}


@pytest.fixture(autouse=True)
def _no_agent_cache():
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


def _request(method: str, body: object, name: str = "", *, owner: bool = True) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.method = method
    request.match_info = {"name": name}
    state = MagicMock()
    # No configured owner id -> `is_owner_dashboard_request` falls back to the
    # implicit local-dashboard subjects, so "local-app" is an owner here.
    state.owner_id = ""
    request.app = {"state": state}

    # The owner gate reads `request["user"]` and requires `request["app"] == ""`
    # (an app token is never the owner), both set by the token-auth middleware.
    keys: dict[str, object] = {"app": ""} if owner else {"app": "some-app"}
    if owner:
        keys["user"] = "local-app"

    request.get = lambda key, default=None: keys.get(key, default)
    request.__contains__ = lambda self, key: key in keys
    request.__getitem__ = lambda self, key: keys[key]

    async def _json():
        if body is None:
            raise json.JSONDecodeError("no body", "", 0)
        return body

    request.json = _json
    return request


async def _create(agents_dir: Path, body: object) -> web.Response:
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        return await api_agents_installed_create(_request("POST", body))


async def _update(agents_dir: Path, name: str, body: object) -> web.Response:
    with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
        return await api_agents_installed_update(_request("PUT", body, name=name))


def _body(resp: web.Response) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def _written(agents_dir: Path, name: str) -> dict:
    return json.loads((agents_dir / f"{name}.json").read_text(encoding="utf-8"))


# ── The spec must only ever contain fields kiro-cli accepts ──


class TestSpecSchema:
    @pytest.mark.asyncio
    async def test_prompt_is_written_to_the_prompt_field(self, agents_dir):
        """`customInstructions` is not a kiro spec field. Writing it there makes
        deny_unknown_fields reject the whole spec, so a template with a prompt
        would silently not load at all."""
        await _create(agents_dir, {"name": "researcher", "prompt": "Be rigorous."})

        written = _written(agents_dir, "researcher")
        assert written["prompt"] == "Be rigorous."
        assert "customInstructions" not in written

    @pytest.mark.asyncio
    async def test_no_unknown_key_reaches_the_spec(self, agents_dir):
        """A body full of plausible-but-invalid keys must not smuggle any of them
        into the file. One unknown key costs the entire template."""
        await _create(
            agents_dir,
            {
                "name": "researcher",
                "prompt": "hi",
                "customInstructions": "hi",
                "systemPrompt": "hi",
                "instructions": "hi",
                "temperature": 0.4,
                "maxTokens": 100,
                "model_managed": True,
                "cc_model": "x",
            },
        )

        assert set(_written(agents_dir, "researcher")) <= VALID_SPEC_KEYS

    @pytest.mark.asyncio
    async def test_denied_commands_is_refused(self, agents_dir):
        """Top-level it is an unknown key (whole spec rejected), and relocating it
        under toolsSettings would revive a retired mechanism that
        _strip_legacy_denied_commands deletes. Refused loudly, not dropped."""
        resp = await _create(agents_dir, {"name": "researcher", "deniedCommands": ["rm -rf /*"]})

        assert resp.status == 400
        assert not (agents_dir / "researcher.json").exists()

    @pytest.mark.asyncio
    async def test_valid_privilege_fields_still_round_trip(self, agents_dir):
        """The scope this endpoint deliberately DOES grant must keep working.

        ``allowedTools`` survives only when the governance ceiling permits the
        ref, so the decision is pinned here rather than inherited from whatever
        ceiling and profiles the host running the suite happens to have —
        ``may_skip_gate_now`` fails closed on a host where any configured profile
        could govern ``fs_read``, which made this assertion environment-dependent.
        """
        with patch("kiro_crew.platform.governance.may_skip_gate_now", return_value=True):
            await _create(
                agents_dir,
                {
                    "name": "researcher",
                    "tools": ["fs_read", "execute_bash"],
                    "allowedTools": ["fs_read"],
                    "resources": ["file://.kiro/steering/**/*.md"],
                },
            )

        written = _written(agents_dir, "researcher")
        assert written["tools"] == ["fs_read", "execute_bash"]
        assert written["allowedTools"] == ["fs_read"]
        assert written["resources"] == ["file://.kiro/steering/**/*.md"]

    @pytest.mark.asyncio
    async def test_ceiling_governed_grant_is_withheld(self, agents_dir):
        """The other direction: a governed ref is dropped from ``allowedTools``
        but stays MOUNTED via ``tools``, so the tool remains usable and its calls
        go through the approval gate instead of skipping it."""
        with patch("kiro_crew.platform.governance.may_skip_gate_now", return_value=False):
            await _create(
                agents_dir,
                {
                    "name": "researcher",
                    "tools": ["fs_read", "execute_bash"],
                    "allowedTools": ["execute_bash"],
                },
            )

        written = _written(agents_dir, "researcher")
        assert written["allowedTools"] == []
        assert written["tools"] == ["fs_read", "execute_bash"]


# ── Names ──


class TestNames:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["../escape", "sub/dir", "back\\slash", "..", ".hidden", "has space", "-lead"]
    )
    async def test_unsafe_names_are_rejected(self, agents_dir, name):
        resp = await _create(agents_dir, {"name": name})

        assert resp.status == 400
        assert list(agents_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_traversal_writes_nothing_outside_the_agents_dir(self, agents_dir, tmp_path):
        await _create(agents_dir, {"name": "../../pwned"})

        assert not (tmp_path / "pwned.json").exists()
        assert not (tmp_path.parent / "pwned.json").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["kirocrew", "kirocrew-lite", "kirocrew-knowledge", "default"])
    async def test_managed_and_builtin_names_are_reserved(self, agents_dir, name):
        resp = await _create(agents_dir, {"name": name})

        assert resp.status == 400
        assert not (agents_dir / f"{name}.json").exists()

    @pytest.mark.asyncio
    async def test_a_name_claimed_by_a_package_spec_conflicts(self, agents_dir):
        """kiro-cli resolves by the `name` FIELD, so a free filename is not
        enough — two specs answering to one name is a coin flip."""
        (agents_dir / "somepkg-reviewer.json").write_text('{"name": "reviewer"}', encoding="utf-8")

        resp = await _create(agents_dir, {"name": "reviewer"})

        assert resp.status == 409
        assert not (agents_dir / "reviewer.json").exists()

    @pytest.mark.asyncio
    async def test_an_unparseable_neighbour_does_not_block_creation(self, agents_dir):
        (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")

        resp = await _create(agents_dir, {"name": "researcher"})
        assert resp.status == 201


# ── Create is exclusive ──


class TestExclusiveCreate:
    @pytest.mark.asyncio
    async def test_existing_template_is_not_overwritten(self, agents_dir):
        (agents_dir / "researcher.json").write_text(
            '{"name": "researcher", "prompt": "original"}', encoding="utf-8"
        )

        resp = await _create(agents_dir, {"name": "researcher", "prompt": "replacement"})

        assert resp.status == 409
        assert _written(agents_dir, "researcher")["prompt"] == "original"

    @pytest.mark.asyncio
    async def test_a_file_appearing_after_the_scan_is_not_clobbered(self, agents_dir):
        """The TOCTOU window O_EXCL closes. Stubbing the scan to report a free
        name is exactly what the loser of a concurrent-create race sees, so this
        reaches the write with the file already present."""
        victim = agents_dir / "researcher.json"
        victim.write_text('{"name": "researcher", "prompt": "original"}', encoding="utf-8")

        with patch("kiro_crew.dashboard.handlers.agents._name_already_claimed", return_value=False):
            resp = await _create(agents_dir, {"name": "researcher", "prompt": "replacement"})

        assert resp.status == 409
        assert _written(agents_dir, "researcher")["prompt"] == "original"


# ── Update ──


class TestUpdate:
    @pytest.fixture
    def template(self, agents_dir) -> Path:
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps({"name": "researcher", "description": "old", "prompt": "old"}),
            encoding="utf-8",
        )
        return path

    @pytest.mark.asyncio
    async def test_fields_are_replaced(self, agents_dir, template):
        resp = await _update(
            agents_dir, "researcher", {"description": "new", "prompt": "new prompt"}
        )

        assert resp.status == 200
        written = _written(agents_dir, "researcher")
        assert written["description"] == "new"
        assert written["prompt"] == "new prompt"

    @pytest.mark.asyncio
    async def test_unmodelled_keys_are_carried_forward(self, agents_dir):
        """The form builds a FRESH spec, so a plain full-replace would delete
        hand-authored keys it does not model — editing a description would drop
        the user's audit hook."""
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps(
                {
                    "name": "researcher",
                    "description": "old",
                    "hooks": {"postToolUse": [{"matcher": "execute_bash", "command": "log"}]},
                    "includeMcpJson": False,
                    "toolsSettings": {"fs_write": {"someSetting": True}},
                }
            ),
            encoding="utf-8",
        )

        await _update(agents_dir, "researcher", {"description": "new"})

        written = _written(agents_dir, "researcher")
        assert written["description"] == "new"
        assert written["hooks"]["postToolUse"][0]["matcher"] == "execute_bash"
        assert written["includeMcpJson"] is False
        assert written["toolsSettings"] == {"fs_write": {"someSetting": True}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "filename",
        [
            "kirocrew.json",
            "kirocrew-lite.json",
            "kirocrew-knowledge.json",
            "kirocrew-research.json",
            "kirocrew-heartbeat.json",
        ],
    )
    async def test_kiro_crew_managed_specs_are_refused(self, agents_dir, filename):
        """None of these contains a double dash, so a `--`-only guard lets a PUT
        full-replace Kiro Crew's own agent — dropping hooks, includeMcpJson and
        the managed MCP block until the next install rebuild."""
        path = agents_dir / filename
        original = {
            "name": Path(filename).stem,
            "prompt": "file://managed.md",
            "hooks": {"postToolUse": [{"matcher": "execute_bash", "command": "audit"}]},
        }
        path.write_text(json.dumps(original), encoding="utf-8")

        resp = await _update(agents_dir, Path(filename).stem, {"description": "hijacked"})

        assert resp.status == 403
        assert json.loads(path.read_text(encoding="utf-8")) == original

    @pytest.mark.asyncio
    async def test_app_managed_specs_are_still_refused(self, agents_dir):
        path = agents_dir / "someapp--helper.json"
        path.write_text('{"name": "helper"}', encoding="utf-8")

        resp = await _update(agents_dir, "someapp--helper", {"description": "x"})
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_missing_template_is_a_404(self, agents_dir):
        resp = await _update(agents_dir, "nope", {"description": "x"})
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_a_mismatched_body_name_is_rejected(self, agents_dir, template):
        resp = await _update(
            agents_dir, "researcher", {"name": "somethingelse", "description": "x"}
        )

        assert resp.status == 400
        assert _written(agents_dir, "researcher")["description"] == "old"

    @pytest.mark.asyncio
    async def test_denied_commands_is_refused_on_update_too(self, agents_dir, template):
        resp = await _update(agents_dir, "researcher", {"deniedCommands": ["rm -rf /*"]})

        assert resp.status == 400
        assert "deniedCommands" not in _written(agents_dir, "researcher")


# ── An edit must not delete configuration the dialog cannot express ──


class TestEditPreservesUnsubmittedKeys:
    """Ownership is decided by what the request carried, not a static key list.

    ``resources`` and ``mcpServers`` are nominally editor-owned, but the dialog
    omits ``resources`` entirely and can submit args-only servers. Treating them
    as owned regardless deleted hand-authored steering globs and stripped
    ``command`` / ``env`` off stdio entries on an edit that only touched the
    description.
    """

    @pytest.fixture
    def rich_template(self, agents_dir) -> Path:
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps(
                {
                    "name": "researcher",
                    "description": "old",
                    "resources": ["file://.kiro/steering/**/*.md"],
                    "mcpServers": {
                        "fetch": {
                            "command": "uvx",
                            "args": ["mcp-server-fetch"],
                            "env": {"FETCH_UA": "kiro"},
                        }
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    @pytest.mark.asyncio
    async def test_description_only_edit_keeps_resources(self, agents_dir, rich_template):
        resp = await _update(agents_dir, "researcher", {"description": "new"})

        assert resp.status == 200
        written = _written(agents_dir, "researcher")
        assert written["description"] == "new"
        assert written["resources"] == ["file://.kiro/steering/**/*.md"]

    @pytest.mark.asyncio
    async def test_description_only_edit_keeps_full_mcp_entry(self, agents_dir, rich_template):
        resp = await _update(agents_dir, "researcher", {"description": "new"})

        assert resp.status == 200
        server = _written(agents_dir, "researcher")["mcpServers"]["fetch"]
        # command/env are what make the entry launchable; args alone is unusable.
        assert server["command"] == "uvx"
        assert server["env"] == {"FETCH_UA": "kiro"}
        assert server["args"] == ["mcp-server-fetch"]

    @pytest.mark.asyncio
    async def test_submitted_resources_still_replace(self, agents_dir, rich_template):
        """Preservation must not become an inability to edit: a body that DOES
        carry the key replaces it, including clearing it to empty."""
        resp = await _update(agents_dir, "researcher", {"resources": []})

        assert resp.status == 200
        assert _written(agents_dir, "researcher")["resources"] == []

    @pytest.mark.asyncio
    async def test_submitted_mcp_servers_still_replace(self, agents_dir, rich_template):
        resp = await _update(
            agents_dir,
            "researcher",
            # A launchable entry: an args-only server is refused now, since
            # kiro-cli cannot start one and rejects the whole spec on it.
            {"mcpServers": {"other": {"command": "uvx", "args": ["x"]}}},
        )

        assert resp.status == 200
        written = _written(agents_dir, "researcher")["mcpServers"]
        assert "fetch" not in written
        assert written == {"other": {"command": "uvx", "args": ["x"]}}


# ── A JSON body that is not an object must not reach .get() ──


class TestNonObjectBody:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [[], [1, 2], "text", 5, True])
    async def test_create_rejects_non_object(self, agents_dir, body):
        resp = await _create(agents_dir, body)

        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_body"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [[], "text", 5])
    async def test_update_rejects_non_object(self, agents_dir, body):
        path = agents_dir / "researcher.json"
        path.write_text(json.dumps({"name": "researcher"}), encoding="utf-8")

        resp = await _update(agents_dir, "researcher", body)

        assert resp.status == 400
        assert _body(resp)["code"] == "invalid_body"
        # The stored spec is untouched by a rejected request.
        assert _written(agents_dir, "researcher") == {"name": "researcher"}


# ── Every whole-config write goes through the governance ceiling ──


class TestGovernanceSanitize:
    """``allowedTools`` is assigned wholesale from the request. Without the
    sanitize call a governed tool selected for auto-approval is persisted, and
    kiro-cli then skips the PreToolUse gate so the policy denial never runs."""

    @pytest.mark.asyncio
    async def test_create_runs_the_sanitizer(self, agents_dir):
        with patch(
            "kiro_crew.dashboard.handlers.agents.sanitize_agent_config_governance"
        ) as sanitize:
            resp = await _create(
                agents_dir,
                {"name": "auditor", "allowedTools": ["execute_bash"]},
            )

        assert resp.status == 201
        assert sanitize.call_count == 1
        # Called with the spec that is about to be written, not a copy.
        assert sanitize.call_args[0][0]["name"] == "auditor"

    @pytest.mark.asyncio
    async def test_update_runs_the_sanitizer(self, agents_dir):
        path = agents_dir / "auditor.json"
        path.write_text(json.dumps({"name": "auditor"}), encoding="utf-8")

        with patch(
            "kiro_crew.dashboard.handlers.agents.sanitize_agent_config_governance"
        ) as sanitize:
            resp = await _update(agents_dir, "auditor", {"allowedTools": ["execute_bash"]})

        assert resp.status == 200
        assert sanitize.call_count == 1

    @pytest.mark.asyncio
    async def test_ceiling_governed_entry_does_not_reach_the_file(self, agents_dir):
        """End-to-end through the real sanitizer: whatever it strips must be
        absent from the persisted spec, so the assertion holds even if the
        ceiling's contents change."""

        def _strip(config: dict) -> None:
            config["allowedTools"] = [
                t for t in config.get("allowedTools", []) if t != "execute_bash"
            ]

        with patch(
            "kiro_crew.dashboard.handlers.agents.sanitize_agent_config_governance",
            side_effect=_strip,
        ):
            resp = await _create(
                agents_dir,
                {"name": "auditor", "allowedTools": ["execute_bash", "fs_read"]},
            )

        assert resp.status == 201
        assert _written(agents_dir, "auditor")["allowedTools"] == ["fs_read"]


# ── Resource URIs must not reach credential files ──


class TestSensitiveResources:
    """kiro-cli READS every resource match into the agent's context, so an
    unscreened file:// entry turns a template into a credential disclosure path."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "uri",
        [
            "file://~/.ssh/id_rsa",
            "file://~/.aws/credentials",
            "file://~/.ssh/**/*",
            "file://~/**",
        ],
    )
    async def test_create_refuses_sensitive_resource(self, agents_dir, uri):
        resp = await _create(agents_dir, {"name": "leaky", "resources": [uri]})

        assert resp.status == 400
        assert not (agents_dir / "leaky.json").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "uri",
        [
            "file://.kiro/steering/**/*.md",
            "file://docs/README.md",
            "skill://browser-auth",
        ],
    )
    async def test_create_still_allows_ordinary_resources(self, agents_dir, uri):
        resp = await _create(agents_dir, {"name": "reader", "resources": [uri]})

        assert resp.status == 201, _body(resp)
        assert _written(agents_dir, "reader")["resources"] == [uri]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("uri", ["file://**/*.md", "file://*", "file://.*/*"])
    async def test_create_refuses_unanchored_globs(self, agents_dir, uri):
        """A glob naming no concrete directory is resolved against the AGENT's
        working directory, which this validator cannot know -- under a home
        workspace these sweep dotfile directories including .ssh."""
        resp = await _create(agents_dir, {"name": "sweeper", "resources": [uri]})

        assert resp.status == 400
        assert not (agents_dir / "sweeper.json").exists()

    @pytest.mark.asyncio
    async def test_update_refuses_sensitive_resource(self, agents_dir):
        path = agents_dir / "reader.json"
        path.write_text(json.dumps({"name": "reader"}), encoding="utf-8")

        resp = await _update(agents_dir, "reader", {"resources": ["file://~/.ssh/id_rsa"]})

        assert resp.status == 400
        assert "resources" not in _written(agents_dir, "reader")


# ── An edit must not rename an agent out from under its callers ──


class TestDeclaredNameMismatch:
    @pytest.mark.asyncio
    async def test_update_refuses_when_declared_name_differs_from_stem(self, agents_dir):
        """The editor forces name to the URL stem, so saving a spec whose declared
        name differs would unregister it under the name callers actually use."""
        path = agents_dir / "pkg-reviewer.json"
        path.write_text(
            json.dumps({"name": "reviewer", "description": "package owned"}),
            encoding="utf-8",
        )

        resp = await _update(agents_dir, "pkg-reviewer", {"description": "hijacked"})

        assert resp.status == 409
        assert _body(resp)["code"] == "agent_template_name_mismatch"
        # The stored spec is untouched: still named reviewer, original description.
        written = _written(agents_dir, "pkg-reviewer")
        assert written["name"] == "reviewer"
        assert written["description"] == "package owned"

    @pytest.mark.asyncio
    async def test_update_allows_matching_declared_name(self, agents_dir):
        path = agents_dir / "reviewer.json"
        path.write_text(json.dumps({"name": "reviewer"}), encoding="utf-8")

        resp = await _update(agents_dir, "reviewer", {"description": "fine"})

        assert resp.status == 200
        assert _written(agents_dir, "reviewer")["description"] == "fine"

    @pytest.mark.asyncio
    async def test_update_allows_a_spec_with_no_declared_name(self, agents_dir):
        """A spec missing ``name`` entirely has nothing to unregister."""
        path = agents_dir / "reviewer.json"
        path.write_text(json.dumps({"description": "old"}), encoding="utf-8")

        resp = await _update(agents_dir, "reviewer", {"description": "new"})

        assert resp.status == 200
        assert _written(agents_dir, "reviewer")["name"] == "reviewer"


# ── The env screen must use the full validator, not the pattern alone ──


class TestMcpEnvScreenWiring:
    """The handler must call the full value validator. A base64-wrapped
    credential is invisible to the pattern but caught by the decode pass, so it
    pins the wiring: revert the handler to searching the raw pattern and this
    fails while every pattern-matched shape still passes.
    """

    @staticmethod
    def _wrapped_credential() -> str:
        # Assembled rather than written out, and long enough to form a single
        # base64 chunk (the decoder only inspects runs of 40+ chars).
        inner = "ghp_" + "C" * 36
        return base64.b64encode(inner.encode()).decode()

    @pytest.mark.asyncio
    async def test_create_refuses_a_base64_wrapped_credential(self, agents_dir):
        wrapped = self._wrapped_credential()
        resp = await _create(
            agents_dir,
            {
                "name": "leaky",
                # Launchable on purpose: without a command the transport rule
                # would refuse this before the env screen ran, and the test would
                # pass for the wrong reason.
                "mcpServers": {
                    "svc": {"command": "uvx", "args": ["x"], "env": {"OPTION": wrapped}}
                },
            },
        )

        assert resp.status == 400
        assert not (agents_dir / "leaky.json").exists()

    @pytest.mark.asyncio
    async def test_ordinary_env_values_still_pass(self, agents_dir):
        resp = await _create(
            agents_dir,
            {
                "name": "fine",
                "mcpServers": {
                    "svc": {
                        "command": "uvx",
                        "args": ["x"],
                        "env": {"PORT": "8080", "ENDPOINT": "https://api.example.com/v1"},
                    }
                },
            },
        )

        assert resp.status == 201, _body(resp)


# ── Auto-approval must name each tool exactly ──


class TestWildcardAutoApproval:
    """``may_skip_gate_now`` answers True for a glob and False for the concrete
    tools that glob covers, so a wildcard is a broader grant that passes the gate
    the specific names fail -- shell and filesystem tools would auto-approve with
    the sensitive-path and denied-command checks never running.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entry", ["*", "@*", "@builtin/*", "fs_*", "execute_bas?"])
    async def test_create_refuses_wildcard_auto_approval(self, agents_dir, entry):
        resp = await _create(
            agents_dir,
            {"name": "broad", "tools": ["fs_read"], "allowedTools": [entry]},
        )

        assert resp.status == 400
        assert not (agents_dir / "broad.json").exists()

    @pytest.mark.asyncio
    async def test_update_refuses_wildcard_auto_approval(self, agents_dir):
        path = agents_dir / "broad.json"
        path.write_text(json.dumps({"name": "broad"}), encoding="utf-8")

        resp = await _update(agents_dir, "broad", {"allowedTools": ["*"]})

        assert resp.status == 400
        assert "allowedTools" not in _written(agents_dir, "broad")

    @pytest.mark.asyncio
    async def test_exact_tool_names_are_still_accepted(self, agents_dir):
        with patch("kiro_crew.platform.governance.may_skip_gate_now", return_value=True):
            resp = await _create(
                agents_dir,
                {"name": "narrow", "tools": ["fs_read"], "allowedTools": ["fs_read"]},
            )

        assert resp.status == 201, _body(resp)
        assert _written(agents_dir, "narrow")["allowedTools"] == ["fs_read"]


# ── A relative resource must not climb out of its own tree ──


class TestResourceTraversal:
    """A relative path is resolved by the AGENT at prompt time, against the
    agent's directory -- not by this validator, which runs in the gateway. So
    ``file://../.ssh/id_rsa`` resolves somewhere harmless here and somewhere
    sensitive there, and traversal is refused rather than resolved.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "uri",
        [
            "file://../.ssh/id_rsa",
            "file://../../.aws/credentials",
            "file://docs/../../.ssh/id_rsa",
            "file://../**/*.md",
            # Backslash separators too: the screen parses with PureWindowsPath, so
            # a Windows-style traversal is caught on every platform rather than
            # only where os.sep happens to be a backslash.
            "file://..\\.ssh\\id_rsa",
            "file://docs\\..\\..\\.aws\\credentials",
        ],
    )
    async def test_create_refuses_parent_traversal(self, agents_dir, uri):
        resp = await _create(agents_dir, {"name": "climber", "resources": [uri]})

        assert resp.status == 400
        assert not (agents_dir / "climber.json").exists()

    @pytest.mark.asyncio
    async def test_ordinary_relative_resources_still_pass(self, agents_dir):
        resp = await _create(
            agents_dir,
            {"name": "reader", "resources": ["file://.kiro/steering/**/*.md"]},
        )

        assert resp.status == 201, _body(resp)


# ── An unreadable spec must not be silently replaced ──


class TestUnreadableSpec:
    """Treating an unparseable spec as empty turns carry-forward into a full
    replace: nothing is preserved because nothing was understood. That is the same
    silent erase this handler exists to prevent, reached through a parse failure
    instead of a partial request.
    """

    @pytest.mark.asyncio
    async def test_update_refuses_malformed_json(self, agents_dir):
        path = agents_dir / "broken.json"
        path.write_text("{not valid json", encoding="utf-8")

        resp = await _update(agents_dir, "broken", {"description": "new"})

        assert resp.status == 409
        assert _body(resp)["code"] == "agent_template_unreadable"
        # The unreadable file is left exactly as it was.
        assert path.read_text(encoding="utf-8") == "{not valid json"

    @pytest.mark.asyncio
    async def test_update_refuses_a_non_object_spec(self, agents_dir):
        path = agents_dir / "listy.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")

        resp = await _update(agents_dir, "listy", {"description": "new"})

        assert resp.status == 409
        assert path.read_text(encoding="utf-8") == "[1, 2, 3]"

    @pytest.mark.asyncio
    async def test_update_refuses_a_symlink(self, agents_dir, tmp_path):
        """Following it would copy whatever it points at into a world-readable
        agent spec, and would replace the link rather than the template."""
        secret = tmp_path / "outside.json"
        secret.write_text(json.dumps({"name": "outside"}), encoding="utf-8")
        link = agents_dir / "linked.json"
        link.symlink_to(secret)

        resp = await _update(agents_dir, "linked", {"description": "new"})

        assert resp.status == 409
        assert _body(resp)["code"] == "agent_template_unreadable"
        assert link.is_symlink()


# ── Concurrent updates must not lose a saved field ──


class TestConcurrentUpdateSerialization:
    @pytest.mark.asyncio
    async def test_two_disjoint_updates_both_survive(self, agents_dir):
        """Read-modify-write: without serialization both PUTs read the same
        version, and whichever writes second carries forward the other's stale
        value -- losing an update that already reported success.
        """
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps({"name": "researcher", "description": "old", "prompt": "old"}),
            encoding="utf-8",
        )

        # Disjoint edits: one touches description, the other only prompt.
        first, second = await asyncio.gather(
            _update(agents_dir, "researcher", {"description": "NEW-DESC"}),
            _update(agents_dir, "researcher", {"prompt": "NEW-PROMPT"}),
        )

        assert first.status == 200
        assert second.status == 200
        written = _written(agents_dir, "researcher")
        # Neither write may resurrect the other's pre-edit value.
        assert written["description"] == "NEW-DESC"
        assert written["prompt"] == "NEW-PROMPT"


# ── prompt accepts file:// and kiro-cli reads it, so it needs the same screen ──


class TestSensitivePrompt:
    """The repo documents `"prompt": "file:///path/to/prompt.md"` and Kiro Crew
    writes that form for its own managed agents, so an unscreened prompt is the
    same credential-disclosure path as an unscreened resource.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value",
        [
            "file://~/.ssh/id_rsa",
            "file://../../.ssh/id_rsa",
            "file://~/.aws/credentials",
        ],
    )
    async def test_create_refuses_a_sensitive_prompt(self, agents_dir, value):
        resp = await _create(agents_dir, {"name": "leaky", "prompt": value})

        assert resp.status == 400
        assert not (agents_dir / "leaky.json").exists()

    @pytest.mark.asyncio
    async def test_ordinary_prompts_still_pass(self, agents_dir):
        resp = await _create(agents_dir, {"name": "fine", "prompt": "You are a careful reviewer."})

        assert resp.status == 201, _body(resp)
        assert _written(agents_dir, "fine")["prompt"] == "You are a careful reviewer."

    @pytest.mark.asyncio
    async def test_an_anchored_file_prompt_still_passes(self, agents_dir):
        resp = await _create(
            agents_dir, {"name": "filed", "prompt": "file://.kiro/prompts/review.md"}
        )

        assert resp.status == 201, _body(resp)


# ── A falsy non-string must not erase a stored scalar ──


class TestFalsyScalarsDoNotErase:
    """A falsy non-string skipped both validation and assignment, so the key was
    present in the request -- which carry-forward reads as authored -- while
    absent from the built spec. The stored value was erased with HTTP 200.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [False, 0, [], {}])
    async def test_update_refuses_a_falsy_non_string_description(self, agents_dir, bad):
        path = agents_dir / "researcher.json"
        path.write_text(
            json.dumps({"name": "researcher", "description": "keep me"}),
            encoding="utf-8",
        )

        resp = await _update(agents_dir, "researcher", {"description": bad})

        assert resp.status == 400
        assert _written(agents_dir, "researcher")["description"] == "keep me"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["model", "prompt"])
    async def test_update_refuses_a_falsy_non_string_scalar(self, agents_dir, field):
        path = agents_dir / "researcher.json"
        path.write_text(json.dumps({"name": "researcher", field: "keep me"}), encoding="utf-8")

        resp = await _update(agents_dir, "researcher", {field: False})

        assert resp.status == 400
        assert _written(agents_dir, "researcher")[field] == "keep me"

    @pytest.mark.asyncio
    async def test_an_empty_string_still_clears_the_field(self, agents_dir):
        """The legitimate clear must keep working: "" is a string, so it validates
        and the field is simply omitted from the rebuilt spec."""
        path = agents_dir / "researcher.json"
        path.write_text(json.dumps({"name": "researcher", "description": "old"}), encoding="utf-8")

        resp = await _update(agents_dir, "researcher", {"description": ""})

        assert resp.status == 200


# ── A malformed MCP entry must not be persisted ──


class TestMcpEntryShape:
    """kiro-cli rejects the ENTIRE spec on a malformed server and falls the session
    back to the default agent, so a bad entry breaks the template silently."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "srv",
        [
            {"command": 123},
            {"command": ["uvx"]},
            {"url": {"host": "x"}},
            {"command": "uvx", "args": "not-a-list"},
            {"command": "uvx", "args": [1, 2]},
        ],
    )
    async def test_create_refuses_a_malformed_entry(self, agents_dir, srv):
        resp = await _create(agents_dir, {"name": "broken", "mcpServers": {"svc": srv}})

        assert resp.status == 400
        assert not (agents_dir / "broken.json").exists()

    @pytest.mark.asyncio
    async def test_a_well_formed_entry_still_passes(self, agents_dir):
        resp = await _create(
            agents_dir,
            {
                "name": "good",
                "mcpServers": {"svc": {"command": "uvx", "args": ["mcp-server-fetch"]}},
            },
        )

        assert resp.status == 201, _body(resp)

    @pytest.mark.asyncio
    async def test_an_http_entry_with_only_a_url_passes(self, agents_dir):
        """``url`` is the http transport, so it is a launchable entry on its own."""
        resp = await _create(
            agents_dir,
            {"name": "remote", "mcpServers": {"svc": {"url": "https://mcp.example.com"}}},
        )

        assert resp.status == 201, _body(resp)

    @pytest.mark.asyncio
    async def test_an_entry_with_neither_command_nor_url_is_refused(self, agents_dir):
        """kiro-cli cannot start it and rejects the whole spec, which silently
        falls the session back to the default agent."""
        resp = await _create(agents_dir, {"name": "husk", "mcpServers": {"svc": {"args": ["x"]}}})

        assert resp.status == 400
        assert not (agents_dir / "husk.json").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_env", [[], 0, "", "not-a-dict"])
    async def test_a_present_but_non_dict_env_is_refused(self, agents_dir, bad_env):
        """Screened on PRESENCE: a falsy non-dict would otherwise skip the type
        check and be persisted as an invalid block."""
        resp = await _create(
            agents_dir,
            {
                "name": "badenv",
                "mcpServers": {"svc": {"command": "uvx", "env": bad_env}},
            },
        )

        assert resp.status == 400
        assert not (agents_dir / "badenv.json").exists()


# ── The listing's managed flag must match what the PUT actually refuses ──


class TestManagedFlagMatchesRefusal:
    """The editor gates Edit/Clone on this flag. If it disagreed with
    _template_is_writable, the UI would offer an action the endpoint refuses with
    403 -- which is what a source-only check did for the auxiliary managed specs
    (knowledge, research, lite), since those do not report source 'kirocrew'.
    """

    @pytest.mark.parametrize(
        "filename",
        [
            "kirocrew.json",
            "kirocrew-knowledge.json",
            "kirocrew-research.json",
            "kirocrew-lite.json",
        ],
    )
    def test_managed_specs_are_flagged_and_refused(self, filename):
        from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

        if filename not in OWNED_KIRO_AGENT_FILES:
            pytest.skip(f"{filename} is not in the managed set on this version")
        # Both sides derive from the same predicate, so they cannot disagree.
        assert _template_is_writable(filename) is not None

    @pytest.mark.parametrize("filename", ["researcher.json", "my-agent.json"])
    def test_user_specs_are_neither_flagged_nor_refused(self, filename):
        assert _template_is_writable(filename) is None

    def test_app_namespaced_specs_are_refused(self):
        assert _template_is_writable("myapp--reviewer.json") is not None


# ── The collision scan must go through the hardened shared reader ──


class TestCollisionScanUsesSharedReader:
    """The scan reads every spec in a user-writable directory shared with other
    tools, so it must not use a raw read_text: a symlink to a character device or
    an arbitrarily large file would become an unbounded allocation in the gateway.

    The discriminator here is the AppleDouble skip, which only the shared reader
    has: a raw read would parse `._claimed.json` and see a name collision, while
    the shared reader skips the sidecar entirely. That makes the test fail if the
    reader is ever swapped back for a bare read_text.
    """

    @pytest.mark.asyncio
    async def test_an_appledouble_sidecar_does_not_claim_a_name(self, agents_dir):
        # macOS writes these next to real files on non-native filesystems; they are
        # resource forks, not specs, and must not participate in name resolution.
        (agents_dir / "._claimed.json").write_text(
            json.dumps({"name": "researcher"}), encoding="utf-8"
        )

        resp = await _create(agents_dir, {"name": "researcher"})

        assert resp.status == 201, _body(resp)
        assert _written(agents_dir, "researcher")["name"] == "researcher"

    @pytest.mark.asyncio
    async def test_a_real_spec_still_claims_its_declared_name(self, agents_dir):
        """The guard must still fire for a genuine spec, so the sidecar skip has
        not simply disabled collision detection."""
        (agents_dir / "pkg-reviewer.json").write_text(
            json.dumps({"name": "researcher"}), encoding="utf-8"
        )

        resp = await _create(agents_dir, {"name": "researcher"})

        assert resp.status == 409
        assert _body(resp)["code"] == "agent_template_exists"


# ── A relative dotfile resource is screened against a HOME workspace ──


class TestRelativeDotfileResource:
    """`is_sensitive_path` with no base resolves a relative path against the
    GATEWAY's cwd, so a bare `.ssh/id_rsa` matched nothing and was accepted. The
    path is resolved by the AGENT against its workspace, and a home-directory
    workspace is the scenario this screen defends against -- so the relative form
    is screened a second time with HOME as the base.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "uri",
        [
            "file://.ssh/id_rsa",
            "file://.aws/credentials",
            "file://.gnupg/secring.gpg",
        ],
    )
    async def test_create_refuses_a_relative_dotfile_resource(self, agents_dir, uri):
        resp = await _create(agents_dir, {"name": "leaky", "resources": [uri]})

        assert resp.status == 400
        assert not (agents_dir / "leaky.json").exists()

    @pytest.mark.asyncio
    async def test_the_documented_steering_shape_still_passes(self, agents_dir):
        """`.kiro/steering/**` is dot-leading too, so the screen must not simply
        refuse dotted segments -- it is not sensitive under home either."""
        resp = await _create(
            agents_dir,
            {"name": "reader", "resources": ["file://.kiro/steering/**/*.md"]},
        )

        assert resp.status == 201, _body(resp)

    @pytest.mark.asyncio
    async def test_a_relative_dotfile_prompt_is_refused_too(self, agents_dir):
        resp = await _create(agents_dir, {"name": "leaky", "prompt": "file://.ssh/id_rsa"})

        assert resp.status == 400


# ── Every 400 carries a machine-readable code ──


class TestErrorCodesArePresent:
    @pytest.mark.asyncio
    async def test_too_many_skills_carries_a_code(self, agents_dir):
        from kiro_crew.dashboard.handlers.agents import MAX_AGENT_SKILLS

        resp = await _create(
            agents_dir,
            {"name": "many", "skills": [f"s{i}" for i in range(MAX_AGENT_SKILLS + 1)]},
        )

        assert resp.status == 400
        assert _body(resp)["code"] == "too_many_skills"

    @pytest.mark.asyncio
    async def test_name_mismatch_carries_a_code(self, agents_dir):
        path = agents_dir / "researcher.json"
        path.write_text(json.dumps({"name": "researcher"}), encoding="utf-8")

        resp = await _update(agents_dir, "researcher", {"name": "somethingelse"})

        assert resp.status == 400
        assert _body(resp)["code"] == "name_mismatch"


# ── A security refusal must reach the audit log ──


class TestValidationDenialIsAudited:
    """Most refusals from the spec builder are SECURITY decisions -- a sensitive
    resource path, a literal credential in an MCP env block, a wildcard
    auto-approve grant. Those are exactly what the audit log exists to record, and
    they were previously invisible to it.
    """

    @pytest.mark.asyncio
    async def test_a_sensitive_resource_refusal_is_recorded(self, agents_dir):
        with patch("kiro_crew.dashboard.handlers.agents._sel") as sel:
            resp = await _create(
                agents_dir, {"name": "leaky", "resources": ["file://~/.ssh/id_rsa"]}
            )

        assert resp.status == 400
        calls = [c for c in sel.return_value.log_api_access.call_args_list]
        assert calls, "no SEL event emitted for a security refusal"
        kwargs = calls[-1].kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["operation"] == "agent_template.validate"

    @pytest.mark.asyncio
    async def test_a_wildcard_grant_refusal_is_recorded(self, agents_dir):
        with patch("kiro_crew.dashboard.handlers.agents._sel") as sel:
            resp = await _create(agents_dir, {"name": "broad", "allowedTools": ["*"]})

        assert resp.status == 400
        assert sel.return_value.log_api_access.call_args_list
        assert sel.return_value.log_api_access.call_args_list[-1].kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_a_successful_create_is_not_recorded_as_denied(self, agents_dir):
        with patch("kiro_crew.dashboard.handlers.agents._sel") as sel:
            resp = await _create(agents_dir, {"name": "fine"})

        assert resp.status == 201
        outcomes = [c.kwargs.get("outcome") for c in sel.return_value.log_api_access.call_args_list]
        assert "denied" not in outcomes


# ── A template must not pin a model the account cannot run ──


class TestUnavailableModelRefused:
    """A spec naming an unavailable model is not harmless: session startup
    withholds it and the agent silently runs the backend default, so the user
    believes the template pinned a model when it did not.
    """

    @pytest.mark.asyncio
    async def test_create_refuses_a_model_the_session_does_not_advertise(self, agents_dir):
        with patch(
            "kiro_crew.dashboard.handlers.agents._live_advertised_model_ids",
            return_value=["claude-opus-4-8", "auto"],
        ):
            resp = await _create(agents_dir, {"name": "pinned", "model": "some-unentitled-model"})

        assert resp.status == 400
        assert not (agents_dir / "pinned.json").exists()

    @pytest.mark.asyncio
    async def test_create_accepts_an_advertised_model(self, agents_dir):
        with patch(
            "kiro_crew.dashboard.handlers.agents._live_advertised_model_ids",
            return_value=["claude-opus-4-8", "auto"],
        ):
            resp = await _create(agents_dir, {"name": "pinned", "model": "claude-opus-4-8"})

        assert resp.status == 201, _body(resp)
        assert _written(agents_dir, "pinned")["model"] == "claude-opus-4-8"

    @pytest.mark.asyncio
    async def test_an_unknown_advertised_set_allows_any_model(self, agents_dir):
        """Fails OPEN: an empty advertised set means entitlement is unknowable (no
        live session, or a backend that omits `models`), and reading it as "nothing
        is allowed" would refuse every model on such a host."""
        with patch(
            "kiro_crew.dashboard.handlers.agents._live_advertised_model_ids",
            return_value=[],
        ):
            resp = await _create(agents_dir, {"name": "pinned", "model": "anything-at-all"})

        assert resp.status == 201, _body(resp)


# ── A managed-template refusal is a permission denial and must be audited ──


class TestManagedDenialIsAudited:
    @pytest.mark.asyncio
    async def test_managed_template_refusal_is_recorded(self, agents_dir):
        from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

        managed = OWNED_KIRO_AGENT_FILES[0]
        stem = managed[:-5] if managed.endswith(".json") else managed
        (agents_dir / managed).write_text(json.dumps({"name": stem}), encoding="utf-8")

        with patch("kiro_crew.dashboard.handlers.agents._sel") as sel:
            resp = await _update(agents_dir, stem, {"description": "hijack"})

        assert resp.status == 403
        calls = sel.return_value.log_api_access.call_args_list
        assert calls, "no SEL event emitted for a managed-template refusal"
        kwargs = calls[-1].kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["operation"] == "agent_template.update"


# ── Relative globs must be screened against HOME, not the gateway cwd ──


class TestRelativeGlobResourceScreen:
    """A relative resource path is resolved by the AGENT against its workspace,
    while this validator runs in the gateway. Screening it against the gateway's
    cwd matches nothing, so both directions -- is-sensitive and
    contains-sensitive -- must also screen against HOME as the worst case.

    The absolute form of each glob below was already refused, which is what makes
    the relative form a bypass of the same rule rather than a new policy.
    """

    @pytest.mark.parametrize(
        "glob",
        [
            # sensitive leaf is a CHILD of the named dir, so only the
            # contains-direction can catch these
            "file://.kube/**",
            "file://.config/**",
            "file://.docker/**",
            "file://.local/share/**",
        ],
    )
    @pytest.mark.asyncio
    async def test_relative_credential_dir_glob_is_refused(self, agents_dir, glob):
        resp = await _create(agents_dir, {"name": "sweeper", "resources": [glob]})
        assert resp.status == 400, f"{glob} was accepted"
        assert not (agents_dir / "sweeper.json").exists()

    @pytest.mark.parametrize(
        "glob",
        ["file://~/.kube/**", "file://~/.config/**", "file://~/.docker/**"],
    )
    @pytest.mark.asyncio
    async def test_absolute_form_stays_refused(self, agents_dir, glob):
        resp = await _create(agents_dir, {"name": "sweeper", "resources": [glob]})
        assert resp.status == 400

    @pytest.mark.parametrize(
        "glob",
        [
            "file://.kiro/steering/**/*.md",
            "file://.kiro/steering/**",
            "file://docs/**/*.md",
            "file://src/**/*.py",
        ],
    )
    @pytest.mark.asyncio
    async def test_legitimate_globs_are_not_over_blocked(self, agents_dir, glob):
        """The HOME-base screen must not refuse the shapes templates legitimately
        use -- otherwise the fix trades a bypass for a broken feature."""
        resp = await _create(agents_dir, {"name": "docs", "resources": [glob]})
        assert resp.status == 201, _body(resp)


# ── The app-agent namespace separator is reserved ──


class TestAppNamespaceCollision:
    """`_safe_link_name` maps `app/agent` to `app--agent.json`, and
    `_deregister_agents` deletes by that prefix alone without checking ownership,
    so a user template inside that namespace is unlinked when the app is disabled.
    """

    @pytest.mark.asyncio
    async def test_double_hyphen_name_is_refused(self, agents_dir):
        resp = await _create(agents_dir, {"name": "calendar--assistant"})
        assert resp.status == 400
        assert not (agents_dir / "calendar--assistant.json").exists()

    @pytest.mark.asyncio
    async def test_single_hyphen_name_is_still_allowed(self, agents_dir):
        resp = await _create(agents_dir, {"name": "my-helper-agent"})
        assert resp.status == 201, _body(resp)


# ── headers is a credential channel and gets the same screen as env ──


class TestMcpHeadersScreen:
    @pytest.mark.asyncio
    async def test_literal_authorization_header_is_refused(self, agents_dir):
        resp = await _create(
            agents_dir,
            {
                "name": "remote",
                "mcpServers": {
                    "api": {
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer sk-live-abc123def456"},
                    }
                },
            },
        )
        assert resp.status == 400
        assert not (agents_dir / "remote.json").exists()

    @pytest.mark.asyncio
    async def test_cookie_header_is_refused(self, agents_dir):
        resp = await _create(
            agents_dir,
            {
                "name": "remote",
                "mcpServers": {
                    "api": {
                        "url": "https://example.invalid/mcp",
                        "headers": {"Cookie": "session=8f3b2a1c9d4e5f60"},
                    }
                },
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_dict_headers_is_refused(self, agents_dir):
        resp = await _create(
            agents_dir,
            {
                "name": "remote",
                "mcpServers": {"api": {"url": "https://example.invalid/mcp", "headers": []}},
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_env_reference_and_benign_headers_are_accepted(self, agents_dir):
        resp = await _create(
            agents_dir,
            {
                "name": "remote",
                "mcpServers": {
                    "api": {
                        "url": "https://example.invalid/mcp",
                        "headers": {
                            "Authorization": "${MY_API_TOKEN}",
                            "Accept": "application/json",
                        },
                    }
                },
            },
        )
        assert resp.status == 201, _body(resp)
        written = _written(agents_dir, "remote")
        assert written["mcpServers"]["api"]["headers"]["Accept"] == "application/json"


# ── Every value channel on an MCP entry is screened, not a named list of them ──


class TestMcpEntryValueSweep:
    """`env` was screened, then `headers` had to be added, then `url` and `args`:
    four rounds of the same defect. The sweep walks every string in the entry so a
    field nobody enumerated is covered on arrival.

    Credential fixtures are assembled from parts: a joined literal in the source
    trips the repository's secret-scanning gate.
    """

    @pytest.mark.asyncio
    async def test_url_userinfo_is_refused(self, agents_dir):
        """A secret embedded in URL structure rather than passed as a value. Caught
        by the shared credential patterns via the sweep -- a dedicated userinfo
        regex was added here and then removed once a revert-verify showed it was
        unreachable behind that predicate.
        """
        resp = await _create(
            agents_dir,
            {
                "name": "remote",
                "mcpServers": {"api": {"url": "https://admin:correcthorse@mcp.invalid/sse"}},
            },
        )
        assert resp.status == 400
        assert not (agents_dir / "remote.json").exists()

    @pytest.mark.asyncio
    async def test_token_in_args_is_refused(self, agents_dir):
        token = "ghp" + "_" + "aB3xY7zQ9wE2rT5yU8iO1pA4sD6fG0hJ2kL5"
        resp = await _create(
            agents_dir,
            {
                "name": "stdio",
                "mcpServers": {"api": {"command": "npx", "args": ["-y", "srv", "--token", token]}},
            },
        )
        assert resp.status == 400
        assert not (agents_dir / "stdio.json").exists()

    @pytest.mark.asyncio
    async def test_credential_in_an_unenumerated_field_is_refused(self, agents_dir):
        """The point of the sweep: a field this validator has no rule for."""
        token = "ghp" + "_" + "aB3xY7zQ9wE2rT5yU8iO1pA4sD6fG0hJ2kL5"
        resp = await _create(
            agents_dir,
            {
                "name": "remote",
                "mcpServers": {
                    "api": {"url": "https://mcp.invalid/sse", "auth": {"bearer": token}}
                },
            },
        )
        assert resp.status == 400

    @pytest.mark.parametrize(
        "server",
        [
            {"command": "npx", "args": ["-y", "@scope/mcp-server"]},
            {"command": "uvx", "args": ["--from", "/opt/tools/pkg", "--verbose"]},
            {"url": "https://mcp.invalid/sse"},
            {"url": "https://mcp.invalid:8443/v1/sse"},
            # the sanctioned form must stay allowed wherever it appears
            {"command": "npx", "args": ["--token", "${GITHUB_TOKEN}"]},
        ],
    )
    @pytest.mark.asyncio
    async def test_ordinary_servers_are_not_over_blocked(self, agents_dir, server):
        """A generic value sweep must not refuse normal entries: `args` carries
        paths, flags and package names, and a false refusal breaks real servers."""
        resp = await _create(agents_dir, {"name": "ok", "mcpServers": {"api": server}})
        assert resp.status == 201, _body(resp)


# ── The update path's existence stat belongs off the event loop ──


class TestUpdatePrecheckIsOffloaded:
    @pytest.mark.asyncio
    async def test_existence_and_writability_run_off_the_loop(self, agents_dir):
        """`is_file()` on a network-backed home blocks every gateway task. Pinned by
        thread identity: the loop runs in the main thread, so work reaching a worker
        thread is provably not on the loop. A wall-clock assertion would be a flake.
        """
        import threading

        (agents_dir / "editable.json").write_text(
            json.dumps({"name": "editable", "description": "before"}), encoding="utf-8"
        )
        main_thread = threading.current_thread().name
        seen: list[str] = []
        real = agents_mod._template_is_writable

        def _tracking(filename):
            seen.append(threading.current_thread().name)
            return real(filename)

        with patch.object(agents_mod, "_template_is_writable", _tracking):
            resp = await _update(agents_dir, "editable", {"description": "after"})

        assert resp.status == 200, _body(resp)
        assert seen, "writability check never ran"
        assert (
            main_thread not in seen
        ), f"precheck ran on the event loop thread ({main_thread}); threads={seen}"


# ── A creatable name must be a usable agent name ──


class TestNameMatchesSharedAgentGrammar:
    """The name written to disk here is the same string later supplied as an
    `agent` value and validated against the shared `_AGENT_NAME_RE`. A name this
    endpoint accepts but that grammar rejects yields a template that exists and
    cannot be selected.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "release.bot",  # dots are legal in the file grammar, not the shared one
            "my.agent",
            "my.agent.v2",
            "trailing-",  # shared grammar requires an alphanumeric final char
            "trailing_",
            "trailing.",
        ],
    )
    @pytest.mark.asyncio
    async def test_names_the_shared_grammar_rejects_are_refused(self, agents_dir, name):
        resp = await _create(agents_dir, {"name": name})
        assert resp.status == 400, f"{name} was accepted"
        assert not (agents_dir / f"{name}.json").exists()

    @pytest.mark.parametrize(
        "name",
        ["my-agent", "agent1", "a", "code-reviewer", "under_score", "a-b_c"],
    )
    @pytest.mark.asyncio
    async def test_names_valid_under_both_grammars_are_accepted(self, agents_dir, name):
        """The added check must not narrow the ordinary cases: anything both
        grammars accept still works."""
        resp = await _create(agents_dir, {"name": name})
        assert resp.status == 201, _body(resp)
        assert (agents_dir / f"{name}.json").exists()

    @pytest.mark.asyncio
    async def test_uppercase_is_still_refused_by_the_file_grammar(self, agents_dir):
        """The shared grammar allows uppercase; this endpoint must not, because the
        spec filename is derived from the name."""
        resp = await _create(agents_dir, {"name": "MyAgent"})
        assert resp.status == 400


# ── Every spec writer must strip Kiro Crew's bookkeeping keys ──


class TestBookkeepingKeysNeverReachTheSpec:
    """`lift_and_strip_bookkeeping`'s docstring requires EVERY writer of a kiro
    agent spec to run it (#2570 named the other three). Carry-forward copies
    unowned keys verbatim, so a `model_managed`/`cc_model` acquired after boot
    would survive an edit -- and kiro-cli rejects the ENTIRE spec on an unknown
    field, silently falling the session back to the default agent.
    """

    @pytest.mark.asyncio
    async def test_update_strips_bookkeeping_carried_from_the_stored_spec(self, agents_dir):
        (agents_dir / "keeper.json").write_text(
            json.dumps(
                {
                    "name": "keeper",
                    "description": "before",
                    "model_managed": True,
                    "cc_model": "sonnet",
                }
            ),
            encoding="utf-8",
        )

        resp = await _update(agents_dir, "keeper", {"description": "after"})

        assert resp.status == 200, _body(resp)
        written = _written(agents_dir, "keeper")
        assert written["description"] == "after"
        assert "model_managed" not in written
        assert "cc_model" not in written

    @pytest.mark.asyncio
    async def test_create_strips_bookkeeping_sent_in_the_body(self, agents_dir):
        resp = await _create(
            agents_dir,
            {"name": "fresh", "description": "d", "model_managed": True, "cc_model": "x"},
        )

        assert resp.status == 201, _body(resp)
        written = _written(agents_dir, "fresh")
        assert "model_managed" not in written
        assert "cc_model" not in written


# ── The credential screen judges what the request CHANGES, not what is stored ──


class TestCredentialScreenScopedToChanges:
    """The dialog re-submits loaded `mcpServers` verbatim, so re-judging stored
    values made a template that already carries a literal token uneditable --
    a description-only edit 400d with no in-dialog way to fix it. The screen's
    purpose is to stop THIS endpoint introducing a credential, so an unchanged
    value is exempt and anything new or changed is still refused.
    """

    @staticmethod
    def _seed(agents_dir, secret: str) -> None:
        (agents_dir / "legacy.json").write_text(
            json.dumps(
                {
                    "name": "legacy",
                    "description": "before",
                    "mcpServers": {"api": {"command": "uvx", "env": {"GITHUB_TOKEN": secret}}},
                }
            ),
            encoding="utf-8",
        )

    @pytest.mark.asyncio
    async def test_unrelated_edit_survives_a_stored_literal_credential(self, agents_dir):
        secret = "ghp" + "_" + "aB3xY7zQ9wE2rT5yU8iO1pA4sD6fG0hJ2kL5"
        self._seed(agents_dir, secret)

        resp = await _update(
            agents_dir,
            "legacy",
            {
                "description": "after",
                "mcpServers": {"api": {"command": "uvx", "env": {"GITHUB_TOKEN": secret}}},
            },
        )

        assert resp.status == 200, _body(resp)
        written = _written(agents_dir, "legacy")
        assert written["description"] == "after"
        assert written["mcpServers"]["api"]["env"]["GITHUB_TOKEN"] == secret

    @pytest.mark.asyncio
    async def test_a_changed_credential_is_still_refused(self, agents_dir):
        """The exemption is byte-identity with the stored value, not the key's
        mere presence -- rotating the literal in is still introducing one."""
        self._seed(agents_dir, "ghp" + "_" + "aB3xY7zQ9wE2rT5yU8iO1pA4sD6fG0hJ2kL5")
        different = "ghp" + "_" + "zZ9yX8wV7uT6sR5qP4oN3mL2kJ1hG0fE9dC8"

        resp = await _update(
            agents_dir,
            "legacy",
            {"mcpServers": {"api": {"command": "uvx", "env": {"GITHUB_TOKEN": different}}}},
        )

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_a_credential_on_a_new_server_is_still_refused(self, agents_dir):
        """A server absent from the stored spec has no exempt values at all."""
        self._seed(agents_dir, "ghp" + "_" + "aB3xY7zQ9wE2rT5yU8iO1pA4sD6fG0hJ2kL5")
        token = "ghp" + "_" + "qQ1wW2eE3rR4tT5yY6uU7iI8oO9pP0aA1sS2"

        resp = await _update(
            agents_dir,
            "legacy",
            {"mcpServers": {"added": {"command": "npx", "env": {"API_KEY": token}}}},
        )

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_screens_everything(self, agents_dir):
        """Create has no stored spec, so nothing is exempt."""
        token = "ghp" + "_" + "aB3xY7zQ9wE2rT5yU8iO1pA4sD6fG0hJ2kL5"
        resp = await _create(
            agents_dir,
            {
                "name": "brandnew",
                "mcpServers": {"api": {"command": "uvx", "env": {"GITHUB_TOKEN": token}}},
            },
        )

        assert resp.status == 400
        assert not (agents_dir / "brandnew.json").exists()


# ── An external writer's save must not be silently overwritten ──


class TestExternalEditIsNotOverwritten:
    """The config lock serializes Kiro Crew's own writers, but kiro-cli, an editor
    or any other tool writes the same file outside it. Without a version check the
    dashboard's read-modify-write replaces that save wholesale and loses it.
    """

    @pytest.mark.asyncio
    async def test_a_write_landing_after_the_read_is_refused(self, agents_dir):
        path = agents_dir / "shared.json"
        path.write_text(json.dumps({"name": "shared", "description": "original"}), encoding="utf-8")

        real_read = agents_mod._read_agent_spec

        def _read_then_external_write(target):
            # Simulate an external tool saving in the window between our read and
            # our write -- the exact interleaving the version stamp exists to catch.
            spec = real_read(target)
            if target == path:
                path.write_text(
                    json.dumps({"name": "shared", "description": "external edit"}),
                    encoding="utf-8",
                )
                # Force a distinct mtime even on a coarse-granularity clock.
                os.utime(path, (time.time() + 2, time.time() + 2))
            return spec

        with patch.object(agents_mod, "_read_agent_spec", _read_then_external_write):
            resp = await _update(agents_dir, "shared", {"description": "dashboard edit"})

        assert resp.status == 409, _body(resp)
        assert _body(resp)["code"] == "agent_template_conflict"
        # The external writer's value survived; ours was refused, not merged.
        assert _written(agents_dir, "shared")["description"] == "external edit"

    @pytest.mark.asyncio
    async def test_an_undisturbed_edit_still_writes(self, agents_dir):
        """The check must not refuse the ordinary case."""
        (agents_dir / "calm.json").write_text(
            json.dumps({"name": "calm", "description": "before"}), encoding="utf-8"
        )

        resp = await _update(agents_dir, "calm", {"description": "after"})

        assert resp.status == 200, _body(resp)
        assert _written(agents_dir, "calm")["description"] == "after"


class TestConflictCoversTheEarliestRead:
    """What the pre-lock screening read is actually RELIED UPON for is the credential
    exemption ("this value is already stored"). That is re-validated against the
    authoritative in-lock read, so a value which changed in between cannot be written
    unscreened.

    A disjoint external edit in the same window is NOT refused, and deliberately so:
    rejecting any change since the screening read also makes two lock-serialized
    dashboard PUTs conflict, when the lock plus the in-lock re-read is precisely what
    lets both survive (see TestConcurrentUpdateSerialization). Distinguishing "the
    client authored against the version it loaded" needs a client-supplied version
    (If-Match), which this endpoint does not yet accept.
    """

    @pytest.mark.asyncio
    async def test_an_exempted_credential_changed_in_the_window_is_refused(self, agents_dir):
        path = agents_dir / "shared.json"
        old_secret = "ghp" + "_" + "aB3xY7zQ9wE2rT5yU8iO1pA4sD6fG0hJ2kL5"
        rotated = "ghp" + "_" + "zZ9yX8wV7uT6sR5qP4oN3mL2kJ1hG0fE9dC8"
        path.write_text(
            json.dumps(
                {
                    "name": "shared",
                    "mcpServers": {"api": {"command": "uvx", "env": {"GITHUB_TOKEN": old_secret}}},
                }
            ),
            encoding="utf-8",
        )

        real_read = agents_mod._read_agent_spec
        calls = {"n": 0}

        def _rotate_after_the_screening_read(target):
            spec = real_read(target)
            calls["n"] += 1
            if calls["n"] == 1 and target == path:
                # An external writer rotates the secret AFTER this request's screen
                # judged the client's value "already stored".
                path.write_text(
                    json.dumps(
                        {
                            "name": "shared",
                            "mcpServers": {
                                "api": {"command": "uvx", "env": {"GITHUB_TOKEN": rotated}}
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                os.utime(path, (time.time() + 2, time.time() + 2))
            return spec

        with patch.object(agents_mod, "_read_agent_spec", _rotate_after_the_screening_read):
            resp = await _update(
                agents_dir,
                "shared",
                {"mcpServers": {"api": {"command": "uvx", "env": {"GITHUB_TOKEN": old_secret}}}},
            )

        assert resp.status == 409, _body(resp)
        assert _body(resp)["code"] == "agent_template_conflict"
        # The rotated secret survived; the stale value was not written back.
        assert _written(agents_dir, "shared")["mcpServers"]["api"]["env"]["GITHUB_TOKEN"] == rotated

    @pytest.mark.asyncio
    async def test_an_edit_that_exempts_nothing_does_not_conflict(self, agents_dir):
        """A description-only edit records no exemption, so it has nothing to
        re-validate and must not be turned into a spurious conflict."""
        (agents_dir / "plain.json").write_text(
            json.dumps({"name": "plain", "description": "before"}), encoding="utf-8"
        )

        resp = await _update(agents_dir, "plain", {"description": "after"})

        assert resp.status == 200, _body(resp)
        assert _written(agents_dir, "plain")["description"] == "after"


class TestAdvertisedModelsResolvedOnTheLoop:
    """`_live_advertised_model_ids` walks the live-session map, which the event loop
    owns and mutates. Resolving it inside the executor let a worker thread iterate a
    dict the loop was mutating -- a RuntimeError surfacing as a 500.
    """

    @pytest.mark.asyncio
    async def test_advertised_lookup_does_not_run_in_a_worker_thread(self, agents_dir):
        import threading

        (agents_dir / "editable.json").write_text(
            json.dumps({"name": "editable", "description": "before"}), encoding="utf-8"
        )
        main_thread = threading.current_thread().name
        seen: list[str] = []
        real = agents_mod._live_advertised_model_ids

        def _tracking(request):
            seen.append(threading.current_thread().name)
            return real(request)

        with patch.object(agents_mod, "_live_advertised_model_ids", _tracking):
            resp = await _update(agents_dir, "editable", {"description": "after"})

        assert resp.status == 200, _body(resp)
        assert seen, "advertised lookup never ran"
        assert all(
            t == main_thread for t in seen
        ), f"ran off the loop thread ({main_thread}); threads={seen}"


class TestExclusiveCreateIsAtomic:
    """Opening the target with O_EXCL and then writing publishes the path before the
    bytes are there, so a crash mid-write leaves truncated JSON under a name kiro-cli
    will load -- and an unparsable spec takes the whole agent down. The content is
    staged first and published with a single atomic link.
    """

    @pytest.mark.asyncio
    async def test_the_target_does_not_exist_until_content_is_complete(self, agents_dir):
        # The vulnerability is a gateway CRASH mid-write, where no Python cleanup
        # runs -- so a test that raises OSError proves nothing (the old direct-write
        # path unlinked on that). The observable property that actually distinguishes
        # the two implementations: while the bytes are being written, does the
        # published name already exist? Staged-then-linked says no; opening the target
        # with O_EXCL and writing into it says yes, and a crash there is what leaves
        # truncated JSON for kiro-cli to choke on.
        target = agents_dir / "staged.json"
        real_fdopen = os.fdopen
        observed: list[bool] = []

        def _observe(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)
            original_write = handle.write

            def _checked(text):
                observed.append(target.exists())
                return original_write(text)

            handle.write = _checked  # type: ignore[method-assign]
            return handle

        with patch.object(agents_mod.os, "fdopen", _observe):
            resp = await _create(agents_dir, {"name": "staged", "description": "d"})

        assert resp.status == 201, _body(resp)
        assert observed, "the spec write never ran"
        assert not any(observed), "the published name existed while content was still being written"

    @pytest.mark.asyncio
    async def test_create_still_refuses_an_existing_name(self, agents_dir):
        """The exclusivity guarantee must survive the restructure."""
        (agents_dir / "taken.json").write_text(json.dumps({"name": "taken"}), encoding="utf-8")

        resp = await _create(agents_dir, {"name": "taken", "description": "d"})

        assert resp.status == 409, _body(resp)
        # The original file is untouched.
        assert _written(agents_dir, "taken") == {"name": "taken"}

    @pytest.mark.asyncio
    async def test_a_normal_create_publishes_complete_content(self, agents_dir):
        resp = await _create(agents_dir, {"name": "whole", "description": "complete"})

        assert resp.status == 201, _body(resp)
        assert _written(agents_dir, "whole")["description"] == "complete"
        assert not list(agents_dir.glob("*.json.tmp"))


class TestOwnerAuthorizationRequired:
    """Both handlers install agent tools and MCP server COMMANDS into ~/.kiro/agents,
    which every session on the machine resolves against, so a non-owner dashboard
    credential must not reach them. Refused before the body is parsed.
    """

    @pytest.mark.asyncio
    async def test_create_refuses_a_non_owner(self, agents_dir):
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            resp = await api_agents_installed_create(
                _request("POST", {"name": "sneaky"}, owner=False)
            )

        assert resp.status == 403
        assert _body(resp)["code"] == "owner_required"
        assert not (agents_dir / "sneaky.json").exists()

    @pytest.mark.asyncio
    async def test_update_refuses_a_non_owner(self, agents_dir):
        (agents_dir / "target.json").write_text(
            json.dumps({"name": "target", "description": "before"}), encoding="utf-8"
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            resp = await api_agents_installed_update(
                _request("PUT", {"description": "after"}, name="target", owner=False)
            )

        assert resp.status == 403
        assert _body(resp)["code"] == "owner_required"
        assert _written(agents_dir, "target")["description"] == "before"


class TestSpecFilesAreOwnerOnly:
    """A spec can name a credential-bearing MCP env, so it must not be readable by
    another principal — on create OR after an edit, since the replacement inherits the
    staged file's permissions.
    """

    @pytest.mark.asyncio
    async def test_create_restricts_the_published_spec(self, agents_dir):
        with patch.object(agents_mod, "restrict_to_owner") as restrict:
            resp = await _create(agents_dir, {"name": "secretive"})

        assert resp.status == 201, _body(resp)
        assert restrict.called, "the staged file was published unrestricted"

    @pytest.mark.asyncio
    async def test_update_restricts_the_replacement(self, agents_dir):
        (agents_dir / "kept.json").write_text(
            json.dumps({"name": "kept", "description": "before"}), encoding="utf-8"
        )

        with patch.object(agents_mod, "restrict_to_owner") as restrict:
            resp = await _update(agents_dir, "kept", {"description": "after"})

        assert resp.status == 200, _body(resp)
        assert restrict.called, "an edit republished the spec unrestricted"


class TestVersionTokenIncludesADigest:
    """`st_mtime_ns` reports nanoseconds but many filesystems only STORE coarse
    timestamps, so a same-size save inside one tick compares equal on mtime+size and
    the conflict check waves the overwrite through. The digest makes the token
    content-sensitive.
    """

    def test_a_same_size_same_mtime_change_is_still_detected(self, agents_dir):
        path = agents_dir / "coarse.json"
        path.write_text('{"name": "coarse", "description": "aaa"}', encoding="utf-8")
        before = agents_mod._spec_version(path)
        st = path.stat()

        # Same byte length, then the timestamp forced back to what it was: exactly the
        # collision a coarse-granularity filesystem produces on its own.
        path.write_text('{"name": "coarse", "description": "bbb"}', encoding="utf-8")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        after = agents_mod._spec_version(path)

        assert after is not None and before is not None
        assert after[0] == before[0], "mtime was not actually held equal"
        assert after[1] == before[1], "size was not actually held equal"
        assert after != before, "the version token missed a same-size same-mtime change"

    def test_absent_file_has_no_version(self, agents_dir):
        assert agents_mod._spec_version(agents_dir / "nope.json") is None


class TestExactlyOneTransport:
    """stdio and http are alternatives, not a pair. Both set is a shape the MCP schema
    rejects, and kiro-cli refuses the whole spec on an unusable server.
    """

    @pytest.mark.asyncio
    async def test_both_command_and_url_is_refused(self, agents_dir):
        resp = await _create(
            agents_dir,
            {
                "name": "mixed",
                "mcpServers": {"api": {"command": "uvx", "url": "https://mcp.invalid/sse"}},
            },
        )

        assert resp.status == 400
        assert not (agents_dir / "mixed.json").exists()

    @pytest.mark.parametrize(
        "server",
        [{"command": "uvx", "args": ["x"]}, {"url": "https://mcp.invalid/sse"}],
    )
    @pytest.mark.asyncio
    async def test_exactly_one_transport_is_accepted(self, agents_dir, server):
        resp = await _create(agents_dir, {"name": "single", "mcpServers": {"api": server}})

        assert resp.status == 201, _body(resp)
