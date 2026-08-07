"""Tests for authoring agent templates via ``POST /api/agents/installed``.

Three layers, matching the properties the endpoint must hold:

* VALIDATE — ``_build_template_spec`` accepts only the fields the kiro agent
  model supports, rejects unsafe/reserved/malformed names, and names the
  offending field so a client can keep the rest of the draft.
* WRITE — the handler refuses duplicate identities (by spec ``name`` or file
  stem, not just the target filename), maps catalog skill keys through the
  enumerated catalog, and lands the new spec atomically as ``{name}.json`` so
  discovery classifies it as user-owned.
* PROTECT — framework-owned stems and the built-in ``default`` agent can never
  be claimed, and a request with unknown skill keys writes nothing.

Every test uses a tmp_path fake agents dir so the real filesystem is untouched.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.agent_discovery import clear_list_agents_cache
from kiro_crew.dashboard.handlers.agents import (
    _build_template_spec,
    _reserved_template_names,
    api_agents_installed_create,
)


@pytest.fixture(autouse=True)
def _no_agent_cache():
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / ".kiro" / "agents"
    d.mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: d)
    # Some sandboxes block hardlinks entirely (os.link -> EPERM). The
    # publication step is link-only by design, so on such hosts emulate the
    # link's EXCLUSIVE semantics (O_EXCL create + byte copy) — CI and normal
    # dev machines keep exercising the real os.link path.
    probe_src = tmp_path / ".link-probe-src"
    probe_src.write_text("x")
    try:
        os.link(probe_src, tmp_path / ".link-probe-dst")
    except OSError:
        real_open, real_fdopen = os.open, os.fdopen

        def _fake_link(src_path, dst_path):
            fd = real_open(str(dst_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with real_fdopen(fd, "wb") as out:
                with open(src_path, "rb") as inp:
                    out.write(inp.read())

        monkeypatch.setattr(os, "link", _fake_link)
    return d


class _FakeState:
    def __init__(self):
        self.refreshed: list[str] = []

    def push_refresh(self, *kinds: str) -> None:
        self.refreshed.extend(kinds)


@pytest.fixture
def state():
    return _FakeState()


async def _post(state, payload, app=None):
    """POST helper. ``app`` non-None marks the request as app-token
    authenticated, mirroring the auth middleware's ``request["app"]``."""
    app_name = app
    app = web.Application()
    app["state"] = state
    if app_name is not None:

        @web.middleware
        async def _mark_app(request, handler):
            request["app"] = app_name
            return await handler(request)

        app.middlewares.append(_mark_app)
    app.router.add_post("/api/agents/installed", api_agents_installed_create)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post("/api/agents/installed", json=payload)
        body = await resp.json()
        return resp.status, body
    finally:
        await client.close()


# ── VALIDATE ──


class TestBuildTemplateSpec:
    def test_trailing_newline_name_rejected(self):
        """Python ``re`` ``$`` matches BEFORE a trailing newline — ``match``
        would accept ``"foo\n"`` and write a newline-bearing filename
        (GPT round-21). ``fullmatch`` closes it."""
        _, err = _build_template_spec({"name": "foo\n"})
        assert err is not None
        assert json.loads(err.text)["field"] == "name"

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"name": "ok", "tools": ["fs_read\n"]}, "tools"),
            ({"name": "ok", "mcpServers": {"srv\n": {"command": "x"}}}, "mcpServers"),
        ],
    )
    def test_trailing_newline_evasion_rejected_everywhere(self, payload, field):
        """Same ``$``-before-newline gap as the name field, for every other
        full-string validator in the template."""
        _, err = _build_template_spec(payload)
        assert err is not None
        assert json.loads(err.text)["field"] == field

    def test_env_reference_with_trailing_garbage_not_exempt(self):
        """``${VAR}\ngarbage`` must not ride the env-reference exemption."""
        _, err = _build_template_spec(
            {
                "name": "ok",
                "mcpServers": {
                    "s": {
                        "command": "x",
                        "env": {"API_TOKEN": "${API_TOKEN}\nAKIAIOSFODNN7EXAMPLE"},
                    }
                },
            }
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    @pytest.mark.parametrize(
        "payload,field",
        [
            ({"name": "ok", "prompt": "file://../.ssh/id_rsa"}, "prompt"),
            ({"name": "ok", "resources": ["file://../.aws/credentials"]}, "resources"),
            ({"name": "ok", "resources": ["file://docs/../../.ssh/id_rsa"]}, "resources"),
        ],
    )
    def test_dotdot_traversal_rejected(self, payload, field):
        """``..`` resolves against the agent's RUNTIME cwd — no authoring-time
        anchor can vet it (GPT round-25). Rejected outright."""
        _, err = _build_template_spec(payload)
        assert err is not None
        body = json.loads(err.text)
        assert body["code"] == "path_traversal"
        assert body["field"] == field

    def test_minimal_valid(self):
        spec, err = _build_template_spec({"name": "my-agent"})
        assert err is None
        assert spec == {"name": "my-agent"}

    def test_full_valid(self):
        spec, err = _build_template_spec(
            {
                "name": "review-bot",
                "description": " Reviews things ",
                "model": "claude-opus",
                "prompt": "You review code.",
                "tools": ["fs_read", "@kirocrew-core", "@sdpm/generate_pptx"],
                "allowedTools": ["fs_read"],
                "mcpServers": {
                    "sdpm": {
                        "command": "npx",
                        "args": ["-y", "sdpm"],
                        "env": {"A": "b"},
                        "timeout": 120000,
                    }
                },
                "resources": ["file://.kiro/steering/overview.md"],
                "deniedCommands": ["rm -rf *"],
            }
        )
        assert err is None
        assert spec["description"] == "Reviews things"
        assert spec["model"] == "claude-opus"
        assert spec["tools"] == ["fs_read", "@kirocrew-core", "@sdpm/generate_pptx"]
        assert spec["mcpServers"]["sdpm"]["timeout"] == 120000
        assert spec["toolsSettings"]["execute_bash"]["deniedCommands"] == ["rm -rf *"]

    def test_name_required(self):
        _, err = _build_template_spec({})
        assert err is not None and err.status == 400
        assert json.loads(err.text)["code"] == "name_required"

    @pytest.mark.parametrize(
        "bad",
        ["../evil", "a/b", "UPPER", "has space", "-leading", ".hidden", "a" * 65, ""],
    )
    def test_unsafe_names_rejected(self, bad):
        _, err = _build_template_spec({"name": bad})
        assert err is not None and err.status == 400
        body = json.loads(err.text)
        assert body["code"] in ("name_invalid", "name_required")
        assert body.get("field", "name") == "name"

    def test_reserved_names_rejected(self):
        for reserved in ("kirocrew", "kirocrew-lite", "default"):
            assert reserved in _reserved_template_names()
            _, err = _build_template_spec({"name": reserved})
            assert err is not None
            assert json.loads(err.text)["code"] == "name_reserved"

    def test_error_names_the_field(self):
        _, err = _build_template_spec({"name": "ok", "tools": [123]})
        body = json.loads(err.text)
        assert body["field"] == "tools"
        assert body["code"] == "field_invalid"

    def test_empty_tools_list_preserved(self):
        """``tools: []`` means an agent with NO tools — kiro-cli semantics."""
        spec, err = _build_template_spec({"name": "ok", "tools": []})
        assert err is None
        assert spec["tools"] == []

    def test_auto_model_omitted(self):
        spec, err = _build_template_spec({"name": "ok", "model": "auto"})
        assert err is None
        assert "model" not in spec

    def test_skill_uri_in_resources_rejected(self):
        """skill:// must flow through catalog keys — the enumeration boundary."""
        _, err = _build_template_spec(
            {"name": "ok", "resources": ["skill://~/.kiro/skills/x/SKILL.md"]}
        )
        body = json.loads(err.text)
        assert body["field"] == "resources"

    @pytest.mark.parametrize(
        "bad",
        ["file://~/.ssh/id_rsa", "file://~/.aws/credentials"],
    )
    def test_sensitive_file_resources_rejected(self, bad):
        """A template pointing kiro-cli at a credential directory would load
        those files into model context on every agent start."""
        _, err = _build_template_spec({"name": "ok", "resources": [bad]})
        assert err is not None
        body = json.loads(err.text)
        assert body["code"] == "sensitive_path"
        assert body["field"] == "resources"

    @pytest.mark.parametrize(
        "bad",
        [
            "file://~/**",
            "file://~/*",
            "file://~/.ssh/**/*",
            "file://~/.s[s]h/id_rsa",
            "file://~/.s*/id_rsa",
            "file://~/.ss?/id_rsa",
            "file://~/.[a]ws/credentials",
            "file://~/.kiro/**",
            "file://~/.kiro/crew/*",
            "file://.kiro/steering/**/*.md",
            "file://~/projects/*/README.md",
        ],
    )
    def test_wildcard_resources_rejected(self, bad):
        """Dialog-authored resources take LITERAL paths only. A glob's
        expansion set is decided at consumption time in the agent's
        filesystem, where a symlink planted after authoring
        (``~/projects/link -> ~``) pulls credential files into an
        innocent-looking pattern — no authoring-time check can close that,
        so wildcards are rejected outright (hand-authored JSON keeps them)."""
        _, err = _build_template_spec({"name": "ok", "resources": [bad]})
        assert err is not None
        body = json.loads(err.text)
        assert body["code"] == "glob_not_allowed"
        assert body["field"] == "resources"

    def test_ordinary_file_resources_accepted(self):
        spec, err = _build_template_spec(
            {"name": "ok", "resources": ["file://.kiro/steering/overview.md"]}
        )
        assert err is None
        assert spec["resources"] == ["file://.kiro/steering/overview.md"]

    @pytest.mark.parametrize(
        "bad",
        ["file://~/.ssh/id_rsa", "file://.aws/credentials", "file://~/docs/../.ssh/id_rsa"],
    )
    def test_sensitive_prompt_file_uris_rejected(self, bad):
        """kiro-cli reads a ``file://`` prompt into model context on agent
        start — a template naming a credential file here would exfiltrate it
        through the system prompt. Traversal forms hit the stricter ``..``
        gate first; both are rejections."""
        _, err = _build_template_spec({"name": "ok", "prompt": bad})
        assert err is not None
        body = json.loads(err.text)
        assert body["code"] in {"sensitive_path", "path_traversal"}
        assert body["field"] == "prompt"

    def test_ordinary_prompt_file_uri_accepted(self):
        spec, err = _build_template_spec({"name": "ok", "prompt": "file://~/prompts/reviewer.md"})
        assert err is None
        assert spec["prompt"] == "file://~/prompts/reviewer.md"

    def test_mcp_server_unknown_keys_rejected(self):
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "cwd": "/"}}}
        )
        body = json.loads(err.text)
        assert body["field"] == "mcpServers"

    def test_mcp_server_requires_command(self):
        _, err = _build_template_spec({"name": "ok", "mcpServers": {"s": {"args": []}}})
        assert err is not None

    def test_mcp_timeout_bool_rejected(self):
        """``True`` is an int subclass; a boolean timeout is a client bug."""
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "timeout": True}}}
        )
        assert err is not None

    @pytest.mark.parametrize(
        "key",
        ["API_TOKEN", "aws_secret_access_key", "DbPassword", "GITHUB_API_KEY", "GITHUB_TOKEN"],
    )
    def test_mcp_env_credential_keys_with_literal_values_rejected(self, key):
        """Template JSON is a plain config file, not a secret store — a
        credential value inlined via ``env`` would persist with the template
        and travel with any copy of the file."""
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "env": {key: "hunter2"}}}}
        )
        assert err is not None
        body = json.loads(err.text)
        assert body["code"] == "env_secret_rejected"
        assert body["field"] == "mcpServers"

    @pytest.mark.parametrize("key", ["GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"])
    def test_mcp_env_credential_keys_with_var_references_accepted(self, key):
        """A ``${VAR}`` reference defers to the server's process environment —
        nothing literal is persisted, so the flagship 'template with a
        token-bearing MCP server' shape works without hand-editing JSON."""
        spec, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "env": {key: "${" + key + "}"}}}}
        )
        assert err is None
        assert spec["mcpServers"]["s"]["env"][key] == "${" + key + "}"

    @pytest.mark.parametrize("key", ["OAUTH_CLIENT_ID", "AUTHOR_NAME", "SORT_KEY", "AUTH_MODE"])
    def test_mcp_env_benign_lookalike_keys_accepted(self, key):
        """Token-split matching: OAUTH/AUTHOR must not substring-match into a
        rejection, and a bare KEY token (SORT_KEY) stays usable."""
        spec, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "env": {key: "plain-value"}}}}
        )
        assert err is None
        assert spec["mcpServers"]["s"]["env"][key] == "plain-value"

    @pytest.mark.parametrize(
        "key", ["AUTH_TOKEN_URL", "OAUTH_TOKEN_ENDPOINT", "SECRET_FILE", "TOKEN_HEADER"]
    )
    def test_mcp_env_credential_metadata_keys_accepted(self, key):
        """A metadata suffix (_URL/_ENDPOINT/_FILE/_HEADER) names configuration
        ABOUT a credential — an OAuth endpoint URL is common MCP config, not a
        persisted secret. The value-shape backstop still catches a real secret
        pasted under such a key."""
        spec, err = _build_template_spec(
            {
                "name": "ok",
                "mcpServers": {"s": {"command": "x", "env": {key: "https://idp.example/token"}}},
            }
        )
        assert err is None
        assert spec["mcpServers"]["s"]["env"][key] == "https://idp.example/token"

    def test_mcp_env_secret_value_under_metadata_key_still_rejected(self):
        _, err = _build_template_spec(
            {
                "name": "ok",
                "mcpServers": {
                    "s": {"command": "x", "env": {"TOKEN_FILE": "AKIAIOSFODNN7EXAMPLE"}}
                },
            }
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    def test_mcp_env_url_with_embedded_password_rejected(self):
        """The metadata-suffix exemption must not become a tunnel: a
        ``DATABASE_URL`` carrying ``user:password@`` userinfo IS the
        credential, whatever the key is called."""
        _, err = _build_template_spec(
            {
                "name": "ok",
                "mcpServers": {
                    "s": {
                        "command": "x",
                        "env": {"DATABASE_URL": "postgres://svc:hunter2@db.internal:5432/app"},
                    }
                },
            }
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    def test_mcp_env_url_without_credentials_accepted(self):
        spec, err = _build_template_spec(
            {
                "name": "ok",
                "mcpServers": {
                    "s": {
                        "command": "x",
                        "env": {"DATABASE_URL": "postgres://db.internal:5432/app"},
                    }
                },
            }
        )
        assert err is None

    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_abcdefghijklmnopqrstuvwxyz012345",
            "xoxb-1234-abcdefghijklmnop",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
        ],
    )
    def test_mcp_env_credential_shaped_values_rejected_under_benign_keys(self, value):
        """A raw credential under a benign key (``REGION`` holding an AWS key)
        is still a persisted secret — the value shape is the backstop."""
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "env": {"REGION": value}}}}
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    @pytest.mark.parametrize(
        "key", ["AUTHORIZATION", "Authorization", "AUTH", "PROXY_AUTHORIZATION"]
    )
    def test_authorization_env_keys_rejected(self, key):
        """An Authorization header value IS the credential (GPT round-24)."""
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "env": {key: "whatever"}}}}
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    @pytest.mark.parametrize(
        "value",
        ["Bearer abcdef123456789", "Basic dXNlcjpwYXNzd29yZA=="],
    )
    def test_bearer_basic_values_rejected_under_benign_keys(self, value):
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "env": {"HEADER": value}}}}
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    @pytest.mark.parametrize(
        "value",
        [
            "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCD",
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789",
        ],
    )
    def test_shared_redactor_backstops_value_screening(self, value):
        """Formats the shared ``redact_credentials()`` knows (OpenAI keys
        etc.) must be rejected even when the local regex misses them
        (GPT round-26)."""
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "env": {"OPENAI_CFG": value}}}}
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    @pytest.mark.parametrize(
        "args",
        [
            ["--token", "ghp_abcdefghijklmnopqrstuvwxyz012345"],
            ["--token=ghp_abcdefghijklmnopqrstuvwxyz012345"],
            ["--api-key", "some-literal-secret"],
            ["--password=hunter2secret"],
        ],
    )
    def test_mcp_args_credentials_rejected(self, args):
        """Args land in the persisted JSON and the server argv — same
        no-credentials contract as env values (GPT round-28)."""
        _, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "args": args}}}
        )
        assert err is not None
        assert json.loads(err.text)["code"] == "env_secret_rejected"

    @pytest.mark.parametrize(
        "args",
        [
            ["--token", "${GITHUB_TOKEN}"],
            ["--token=$GITHUB_TOKEN"],
            ["--token-file", "/run/secrets/gh"],
            ["--verbose", "--port", "8080"],
        ],
    )
    def test_mcp_args_references_and_metadata_allowed(self, args):
        spec, err = _build_template_spec(
            {"name": "ok", "mcpServers": {"s": {"command": "x", "args": args}}}
        )
        assert err is None
        assert spec["mcpServers"]["s"]["args"] == args

    def test_mcp_env_benign_keys_accepted(self):
        spec, err = _build_template_spec(
            {
                "name": "ok",
                "mcpServers": {
                    "s": {"command": "x", "env": {"LOG_LEVEL": "debug", "REGION": "us-east-1"}}
                },
            }
        )
        assert err is None
        assert spec["mcpServers"]["s"]["env"] == {"LOG_LEVEL": "debug", "REGION": "us-east-1"}

    @pytest.mark.parametrize(
        "bad",
        ["file://.aws/credentials", "file://.ssh/id_rsa"],
    )
    def test_relative_resources_vetted_against_home_anchor(self, bad):
        """A relative resource resolves against the agent's RUNTIME working
        directory, unknowable at authoring time. ``file://.aws/credentials``
        looks harmless from the gateway CWD but reads the real credential file
        the moment the template starts with ``$HOME`` as its working
        directory — so relative forms are vetted against that worst-case
        anchor."""
        _, err = _build_template_spec({"name": "ok", "resources": [bad]})
        assert err is not None
        assert json.loads(err.text)["code"] == "sensitive_path"


# ── WRITE + PROTECT (HTTP level) ──


@pytest.mark.asyncio
class TestCreateEndpoint:
    async def test_create_writes_user_owned_spec(self, agents_dir, state):
        status, body = await _post(
            state,
            {
                "name": "my-agent",
                "description": "d",
                "model": "claude-opus",
                "prompt": "p",
                "tools": ["fs_read"],
            },
        )
        assert status == 201
        assert body["ok"] is True and body["filename"] == "my-agent.json"
        written = json.loads((agents_dir / "my-agent.json").read_text())
        assert written["name"] == "my-agent"
        assert written["model"] == "claude-opus"
        assert "agents" in state.refreshed

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    async def test_created_template_is_owner_only(self, agents_dir, state):
        """The template carries the author's system prompt and MCP wiring and
        feeds directly into agent behavior — written 0600 like the platform
        ``.env``, not umask-default config."""
        status, _ = await _post(state, {"name": "private-agent"})
        assert status == 201
        mode = (agents_dir / "private-agent.json").stat().st_mode & 0o777
        assert mode == 0o600

    async def test_duplicate_filename_conflicts(self, agents_dir, state):
        (agents_dir / "taken.json").write_text(json.dumps({"name": "taken"}))
        status, body = await _post(state, {"name": "taken"})
        assert status == 409
        assert body["code"] == "name_exists"
        assert body["field"] == "name"

    async def test_duplicate_spec_name_conflicts_even_with_free_filename(self, agents_dir, state):
        """A package file ``pkg--foo.json`` naming "foo" claims the identity."""
        (agents_dir / "pkg--foo.json").write_text(json.dumps({"name": "foo"}))
        status, body = await _post(state, {"name": "foo"})
        assert status == 409
        assert body["code"] == "name_exists"
        assert not (agents_dir / "foo.json").exists()

    async def test_invalid_name_writes_nothing(self, agents_dir, state):
        status, body = await _post(state, {"name": "../escape"})
        assert status == 400
        assert body["code"] == "name_invalid"
        assert list(agents_dir.glob("*")) == []

    async def test_reserved_name_conflicts(self, agents_dir, state):
        status, body = await _post(state, {"name": "kirocrew"})
        assert status == 400
        assert body["code"] == "name_reserved"

    async def test_unknown_skills_write_nothing(self, agents_dir, state):
        with mock.patch(
            "kiro_crew.dashboard.handlers.agents.apply_skill_mapping",
            return_value=([], ["no-such-skill"]),
        ):
            status, body = await _post(state, {"name": "skilled", "skills": ["no-such-skill"]})
        assert status == 400
        assert body["code"] == "unknown_skills"
        assert body["skills"] == ["no-such-skill"]
        assert not (agents_dir / "skilled.json").exists()

    async def test_skills_mapped_through_catalog(self, agents_dir, state):
        def fake_mapping(data, path, st, keys):
            data["resources"] = ["skill://~/.kiro/skills/prepare-pr/SKILL.md"]
            return list(keys), []

        with mock.patch(
            "kiro_crew.dashboard.handlers.agents.apply_skill_mapping",
            side_effect=fake_mapping,
        ):
            status, body = await _post(
                state, {"name": "skilled", "skills": ["kiro-user/prepare-pr"]}
            )
        assert status == 201
        assert body["skills"] == ["kiro-user/prepare-pr"]
        written = json.loads((agents_dir / "skilled.json").read_text())
        assert written["resources"] == ["skill://~/.kiro/skills/prepare-pr/SKILL.md"]

    async def test_non_object_body_rejected(self, agents_dir, state):
        status, body = await _post(state, ["not", "an", "object"])
        assert status == 400
        assert body["code"] == "body_not_object"

    async def test_denied_commands_written_as_tools_settings(self, agents_dir, state):
        status, _ = await _post(state, {"name": "guarded", "deniedCommands": ["git push --force"]})
        assert status == 201
        written = json.loads((agents_dir / "guarded.json").read_text())
        assert written["toolsSettings"]["execute_bash"]["deniedCommands"] == ["git push --force"]

    async def test_governance_sanitizer_runs_before_persist(self, agents_dir, state):
        """This handler is a whole-config writer: a ceiling-governed ref in
        allowedTools must be stripped before the spec lands on disk."""

        def fake_sanitize(config):
            config["allowedTools"] = [t for t in config.get("allowedTools", []) if t != "@governed"]

        with mock.patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            side_effect=fake_sanitize,
        ) as sanitize:
            status, _ = await _post(
                state,
                {"name": "governed-agent", "allowedTools": ["fs_read", "@governed"]},
            )
        assert status == 201
        sanitize.assert_called_once()
        written = json.loads((agents_dir / "governed-agent.json").read_text())
        assert written["allowedTools"] == ["fs_read"]

    async def test_governance_sanitizer_failure_fails_closed(self, agents_dir, state):
        """If the sanitizer errors we cannot know whether the spec carries a
        governed grant — the create must be refused, never persisted
        unsanitized (a written config would bypass the ceiling for the
        template's lifetime)."""
        with mock.patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            side_effect=RuntimeError("plumbing broke"),
        ):
            status, body = await _post(
                state,
                {"name": "gov-broken", "allowedTools": ["execute_bash"]},
            )
        assert status == 503
        assert body["code"] == "governance_unavailable"
        assert not (agents_dir / "gov-broken.json").exists()

    async def test_race_after_name_scan_conflicts_not_overwrites(self, agents_dir, state):
        """A file that appears after the ``_name_taken`` scan (another process
        winning the race) must produce a 409, not be silently overwritten —
        the write uses O_EXCL, so creation itself is the atomic check."""
        theirs = agents_dir / "raced.json"

        def racing_sanitize(config):
            # Runs after the uniqueness scan, before the write — the widest
            # in-lock window where another process could land the file.
            theirs.write_text('{"name": "raced", "model": "their-model"}\n', encoding="utf-8")

        with mock.patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            side_effect=racing_sanitize,
        ):
            status, body = await _post(state, {"name": "raced"})
        assert status == 409
        assert body["code"] == "name_exists"
        # The other process's template survives untouched.
        assert json.loads(theirs.read_text())["model"] == "their-model"

    async def test_allowed_tools_grant_is_sel_audited(self, agents_dir, state, monkeypatch):
        """A surviving allowedTools grant is a persisted auto-approval — it
        must emit a named SEL permission-grant event (GPT round-20)."""
        import kiro_crew.dashboard.handlers.agents as agents_mod

        calls = []
        monkeypatch.setattr(
            agents_mod,
            "_audit_capability",
            lambda op, outcome, res: calls.append((op, outcome, res)),
        )
        with mock.patch(
            "kiro_crew.platform.governance.sanitize_agent_config_governance",
            side_effect=lambda config: None,
        ):
            status, _ = await _post(
                state, {"name": "audited", "tools": ["fs_read"], "allowedTools": ["fs_read"]}
            )
        assert status == 201
        assert ("capability_agent_template_auto_approve", "granted", "audited:fs_read") in calls

    async def test_no_allowed_tools_no_grant_audit(self, agents_dir, state, monkeypatch):
        import kiro_crew.dashboard.handlers.agents as agents_mod

        calls = []
        monkeypatch.setattr(
            agents_mod,
            "_audit_capability",
            lambda op, outcome, res: calls.append((op, outcome, res)),
        )
        status, _ = await _post(state, {"name": "ungranted", "tools": ["fs_read"]})
        assert status == 201
        assert not [c for c in calls if c[0] == "capability_agent_template_auto_approve"]

    async def test_app_token_cannot_create(self, agents_dir, state):
        """A path-allowlisted app token must not gain the POST: creation
        persists MCP commands and auto-approved tools (GPT round-17)."""
        status, body = await _post(state, {"name": "appmade"}, app="some-app")
        assert status == 403
        assert body["code"] == "app_forbidden"
        assert not (agents_dir / "appmade.json").exists()

    async def test_missing_agents_dir_created_on_first_template(self, agents_dir, state):
        """Fresh install: ~/.kiro/agents may not exist — the empty-state
        create flow must succeed, not 500 on the lock open (GPT round-17)."""
        import shutil

        shutil.rmtree(agents_dir)
        status, _ = await _post(state, {"name": "first-ever"})
        assert status == 201
        assert (agents_dir / "first-ever.json").exists()

    async def test_non_utf8_spec_file_does_not_500(self, agents_dir, state):
        """A non-UTF-8 ``*.json`` in the agents dir raises UnicodeDecodeError
        (a ValueError) from ``read_text`` — the name scan must skip it, not
        500 the create."""
        (agents_dir / "bad.json").write_bytes(b"\xff\xfe\x00garbage")
        status, _ = await _post(state, {"name": "survives-bad-neighbor"})
        assert status == 201

    async def test_kirocrew_home_glob_bypass_rejected(
        self, agents_dir, state, monkeypatch, tmp_path
    ):
        """A custom KIROCREW_HOME re-anchors the governance keystones; a glob
        aimed at the re-anchored location must be rejected exactly like the
        default-home form (GPT round-16 finding)."""
        crew = tmp_path / "kirodata"
        crew.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(crew))
        status, body = await _post(
            state,
            {"name": "sneaky", "resources": [f"file://{crew}/security_policy.json"]},
        )
        assert status == 400
        assert body["code"] == "sensitive_path"

    async def test_name_scan_skips_sensitive_symlinks(self, agents_dir, state, tmp_path):
        """A symlink planted in the agents dir aiming at a protected file must
        not be READ by the duplicate scan (GPT round-26). The create still
        succeeds; the symlink is simply skipped."""
        secret = tmp_path / "creds"
        secret.write_text('{"name": "stolen"}')
        # Make the symlink target register as sensitive for this test.
        (agents_dir / "planted.json").symlink_to(secret)
        import kiro_crew.dashboard.handlers.agents as agents_mod

        orig = agents_mod.is_sensitive_path
        agents_mod.is_sensitive_path = lambda p, base_dir=None: (
            True if str(secret) in str(p) else orig(p, base_dir)
        )
        try:
            status, _ = await _post(state, {"name": "stolen"})
        finally:
            agents_mod.is_sensitive_path = orig
        # The planted symlink was skipped: its "stolen" name was never read,
        # so the create is NOT blocked by it.
        assert status == 201

    async def test_existing_empty_file_conflicts_never_deleted(self, agents_dir, state):
        """ANY existing target file 409s — even an empty one. It may be a
        concurrent writer that opened the file before writing bytes; deleting
        it could destroy their template (GPT round-24). Never unlink."""
        (agents_dir / "racing.json").touch()
        status, body = await _post(state, {"name": "racing"})
        assert status == 409
        assert body["code"] == "name_exists"
        assert (agents_dir / "racing.json").exists()
