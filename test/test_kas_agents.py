"""Crew agent spec -> KAS ``ClientCustomAgent`` projection.

Each assertion here pins a constraint read off KAS's own zod schema
(``resolve-client-agents.ts``), not a preference: getting ``tools`` or ``prompt``
wrong produces an agent that registers successfully and then behaves nothing like
the one the operator configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import config as kiro_crew_config
from kiro_crew.acp.kas_agents import (
    _KAS_FALLBACK_PROMPT,
    KAS_MAX_CUSTOM_AGENTS,
    KasAgentTranslationError,
    build_kas_custom_agents,
    resolve_prompt,
    to_client_custom_agent,
)


def _spec(**over):
    base = {
        "name": "kirocrew",
        "description": "the crew agent",
        "prompt": "You are Kiro.",
        "tools": ["fs_read", "fs_write", "@kirocrew-core"],
        "mcpServers": {"kirocrew-core": {"command": "x"}},
        "model": "auto",
        "includeMcpJson": False,
    }
    base.update(over)
    return base


class TestRequiredFields:
    """``id`` and ``prompt`` are the schema's only required members."""

    def test_id_and_prompt_are_emitted(self):
        out = to_client_custom_agent("kirocrew", _spec(), "You are Kiro.")
        assert out["id"] == "kirocrew"
        assert out["prompt"] == "You are Kiro."

    def test_empty_id_is_refused(self):
        with pytest.raises(KasAgentTranslationError):
            to_client_custom_agent("", _spec(), "p")

    def test_empty_prompt_is_refused(self):
        with pytest.raises(KasAgentTranslationError):
            to_client_custom_agent("kirocrew", _spec(), "   ")


class TestToolsFailClosed:
    """``tools`` absent means NO tools on KAS (``agent.tools ?? []``).

    So the list must always be emitted, and a spec that does not state one must
    not be widened into an allowlist nobody wrote.
    """

    def test_list_is_passed_through(self):
        out = to_client_custom_agent("a", _spec(), "p")
        assert out["tools"] == ["fs_read", "fs_write", "@kirocrew-core"]

    def test_mcp_server_shorthand_survives(self):
        """KAS tags every MCP tool ``@<server>``, so Crew's existing syntax works."""
        out = to_client_custom_agent("a", _spec(tools=["@kirocrew-cron"]), "p")
        assert out["tools"] == ["@kirocrew-cron"]

    def test_star_becomes_the_all_tools_literal(self):
        """``"*"`` is a distinct type in the schema, not a list member."""
        assert to_client_custom_agent("a", _spec(tools=["*"]), "p")["tools"] == "*"
        assert to_client_custom_agent("a", _spec(tools="*"), "p")["tools"] == "*"

    @pytest.mark.parametrize("bad", [None, {}, 7, "fs_read"])
    def test_absent_or_malformed_yields_an_empty_allowlist(self, bad):
        spec = _spec()
        spec["tools"] = bad
        if bad is None:
            del spec["tools"]
        assert to_client_custom_agent("a", spec, "p")["tools"] == []

    def test_non_string_entries_are_discarded(self):
        out = to_client_custom_agent("a", _spec(tools=["fs_read", 3, "", None]), "p")
        assert out["tools"] == ["fs_read"]


class TestDeliberateOmissions:
    """Fields left out on purpose; each would misbehave if projected.

    ``mcpServers`` would double-register against the session-level injection,
    ``model`` would compete with the dedicated model verb, and ``permissions``
    would require guessing KAS's capability identifiers.
    """

    @pytest.mark.parametrize("key", ["mcpServers", "model", "permissions", "welcomeMessage"])
    def test_key_is_not_projected(self, key):
        assert key not in to_client_custom_agent("a", _spec(), "p")


class TestOptionalPassThrough:
    def test_description_when_present(self):
        assert to_client_custom_agent("a", _spec(), "p")["description"] == "the crew agent"

    def test_description_omitted_when_blank(self):
        assert "description" not in to_client_custom_agent("a", _spec(description=""), "p")

    def test_include_mcp_json_is_a_bool_passthrough(self):
        assert to_client_custom_agent("a", _spec(), "p")["includeMcpJson"] is False
        assert "includeMcpJson" not in to_client_custom_agent(
            "a", _spec(includeMcpJson="no"), "p"
        )

    def test_resources_and_excluded_tools_when_non_empty(self):
        out = to_client_custom_agent(
            "a", _spec(resources=["file:///x.md"], excludedTools=["execute_bash"]), "p"
        )
        assert out["resources"] == ["file:///x.md"]
        assert out["excludedTools"] == ["execute_bash"]

    def test_empty_lists_are_omitted_rather_than_sent(self):
        out = to_client_custom_agent("a", _spec(resources=[], excludedTools=[]), "p")
        assert "resources" not in out
        assert "excludedTools" not in out


class TestUnsupportedKeysAreLoud:
    """A dropped capability must be visible in the log, not silent."""

    def test_dropped_keys_are_named(self, caplog):
        spec = _spec(allowedTools=["fs_read"], toolsSettings={"x": 1})
        with caplog.at_level("WARNING"):
            to_client_custom_agent("kirocrew", spec, "p")
        msg = caplog.text
        assert "allowedTools" in msg and "toolsSettings" in msg

    def test_nothing_logged_when_the_spec_has_none(self, caplog):
        with caplog.at_level("WARNING"):
            to_client_custom_agent("kirocrew", _spec(), "p")
        assert "no KAS equivalent" not in caplog.text


class TestPromptResolution:
    """KAS requires resolved content; a ``file://`` prompt is ours to read."""

    def test_inline_prompt_is_returned_as_is(self, tmp_path):
        assert resolve_prompt({"prompt": "hello"}, agent_id="a", agents_dir=tmp_path) == "hello"

    def test_file_uri_is_inlined(self, tmp_path):
        p = tmp_path / "prompt.md"
        p.write_text("from disk", encoding="utf-8")
        assert (
            resolve_prompt({"prompt": f"file://{p}"}, agent_id="a", agents_dir=tmp_path)
            == "from disk"
        )

    def test_missing_file_is_an_error_not_a_silent_empty_prompt(self, tmp_path):
        with pytest.raises(KasAgentTranslationError):
            resolve_prompt(
                {"prompt": f"file://{tmp_path / 'nope.md'}"}, agent_id="a", agents_dir=tmp_path
            )

    def test_empty_file_is_refused(self, tmp_path):
        p = tmp_path / "empty.md"
        p.write_text("   ", encoding="utf-8")
        with pytest.raises(KasAgentTranslationError):
            resolve_prompt({"prompt": f"file://{p}"}, agent_id="a", agents_dir=tmp_path)

    def test_sensitive_prompt_path_is_refused_before_any_read(self, tmp_path):
        # A spec whose prompt points at a credential file must NOT be inlined and
        # shipped to KAS. The guard fires on the path, before read_text, so it
        # holds even if the file does not exist.
        for target in ("file://~/.aws/credentials", "file://~/.ssh/id_rsa"):
            with pytest.raises(KasAgentTranslationError, match="not an allowed location"):
                resolve_prompt({"prompt": target}, agent_id="a", agents_dir=tmp_path)

    def test_proc_environ_prompt_is_refused(self, tmp_path):
        # /proc/self/environ would leak the gateway's own environment. It must be
        # refused either way: on POSIX it is absolute and caught by the pseudo-fs
        # denylist ("not an allowed location"); on Windows it is not absolute (no
        # drive letter), so it is treated as a relative prompt and rejected for
        # escaping the agent directory. Both are fail-closed rejections.
        with pytest.raises(
            KasAgentTranslationError, match="not an allowed location|escapes the agent directory"
        ):
            resolve_prompt(
                {"prompt": "file:///proc/self/environ"}, agent_id="a", agents_dir=tmp_path
            )

    def test_relative_prompt_anchors_to_agents_dir_not_cwd(self, tmp_path):
        sub = tmp_path / "prompts"
        sub.mkdir()
        (sub / "expert.md").write_text("expert prompt", encoding="utf-8")
        out = resolve_prompt(
            {"prompt": "file://./prompts/expert.md"}, agent_id="a", agents_dir=tmp_path
        )
        assert out == "expert prompt"

    def test_relative_prompt_escaping_the_agents_dir_is_refused(self, tmp_path):
        with pytest.raises(KasAgentTranslationError, match="escapes"):
            resolve_prompt(
                {"prompt": "file://../../etc/passwd"}, agent_id="a", agents_dir=tmp_path
            )

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_empty_prompt_falls_back_to_the_kas_constant(self, bad, tmp_path, caplog):
        # KAS requires a non-empty prompt; a missing or blank string is an
        # intentionally prompt-less agent (e.g. kirocrew-lite ships "prompt": ""),
        # so the projection substitutes the small inline fallback constant
        # instead of crashing the session.
        out = resolve_prompt({"prompt": bad}, agent_id="kirocrew-lite", agents_dir=tmp_path)
        assert out == _KAS_FALLBACK_PROMPT
        assert "falling back to the lightweight KAS prompt" in caplog.text

    @pytest.mark.parametrize("bad", [7, 3.14, True, [], {}, ["x"]])
    def test_non_string_prompt_is_refused_not_defaulted(self, bad, tmp_path):
        # A non-string prompt is a malformed spec, not a prompt-less one — it
        # must fail loud rather than silently run with the fallback text.
        with pytest.raises(KasAgentTranslationError, match="must be a string"):
            resolve_prompt({"prompt": bad}, agent_id="a", agents_dir=tmp_path)

    def test_a_real_prompt_wins_over_the_fallback(self, tmp_path):
        # The fallback only fires for an empty spec.
        assert resolve_prompt({"prompt": "own"}, agent_id="a", agents_dir=tmp_path) == "own"

    def test_non_utf8_file_prompt_is_refused_not_crashing(self, tmp_path):
        # A non-UTF-8 agent-supplied file:// prompt must fail loud as
        # "unreadable", never raise a raw UnicodeDecodeError out of KAS session
        # creation.
        p = tmp_path / "prompt.md"
        p.write_bytes(b"\xff\xfe not utf-8")
        with pytest.raises(KasAgentTranslationError, match="unreadable"):
            resolve_prompt({"prompt": f"file://{p}"}, agent_id="a", agents_dir=tmp_path)

    def test_build_projects_a_prompt_less_spec_with_the_fallback(self, tmp_path):
        (tmp_path / "kirocrew-lite.json").write_text(
            json.dumps({"name": "kirocrew-lite", "tools": [], "prompt": ""}), encoding="utf-8"
        )
        agents = build_kas_custom_agents(tmp_path, "kirocrew-lite")
        assert agents[0]["prompt"] == _KAS_FALLBACK_PROMPT
        # Tool restriction is preserved — the fallback only supplies a prompt.
        assert agents[0]["tools"] == []


def test_the_batch_cap_matches_the_schema():
    """KAS declares ``customAgents: z.array(z.unknown()).max(50)``."""
    assert KAS_MAX_CUSTOM_AGENTS == 50


class TestAgainstTheRealBundledSpec:
    """Translate the spec Crew actually ships, not a hand-written stand-in.

    The fixtures above encode what the schema allows; this one catches the case
    where the real spec's shape has drifted away from them.
    """

    @staticmethod
    def _bundled() -> dict:
        path = Path(kiro_crew_config.__file__).resolve().parent / "defaults.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_crew_agent_projects_with_its_tools_intact(self):
        spec = self._bundled()
        out = to_client_custom_agent(spec["name"], spec, "resolved prompt text")

        assert out["id"] == "kirocrew"
        assert out["prompt"] == "resolved prompt text"
        # The MCP shorthand is most of Crew's tool surface; losing it would leave
        # the agent nominally configured but unable to reach its own tools.
        assert any(t.startswith("@") for t in out["tools"])
        assert "fs_read" in out["tools"]

    def test_the_real_spec_carries_keys_KAS_cannot_take(self):
        """Guards the drop path against the actual spec, not a synthetic one."""
        spec = self._bundled()
        assert spec.get("allowedTools"), "expected the real spec to still carry allowedTools"
        out = to_client_custom_agent(spec["name"], spec, "p")
        assert "allowedTools" not in out
        assert "mcpServers" not in out
