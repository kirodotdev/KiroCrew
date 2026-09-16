"""Markdown agent definitions (``~/.kiro/agents/<name>.md``) across every consumer.

kiro-cli v3 (KAS) and Kiro IDE define an agent as ONE markdown file: YAML
frontmatter for the fields, the body as the system prompt. A scan of an agents
directory that sees ``*.json`` alone leaves such an agent out of the roster and
out of the KAS projection. These tests pin the shared reader
(:mod:`kiro_crew.agent_spec_format`) and each consumer whose JSON-only
behaviour would otherwise be a hole:

* the roster and its cache signature (a markdown edit must invalidate);
* the KAS projection (``<id>.md`` fallback, body as prompt, JSON wins over an md twin);
* the resolvers that WRITE (they refuse a markdown target rather than
  serializing JSON over it);
* the MCP gateway rewriter (a markdown spec's servers are stubbed into a
  ``<stem>.json`` overlay, so they cannot spawn direct past the tool gate);
* the Connections census (a markdown sharer blocks a revoke);
* the kiro-cli harness (a markdown-only agent is refused before spawn with a
  message naming KAS, never started to fault with ``Mode not found``).

Every test writes under ``tmp_path`` and pins the agents directory it uses.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery
from kiro_crew.acp import kas_agents
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.kas_agents import KasAgentTranslationError, build_kas_custom_agents
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.agent_discovery import (
    AmbiguousAgentSpecError,
    agent_model_map,
    agent_spec_stems,
    clear_list_agents_cache,
    list_agents,
    project_agent_files,
    project_agent_names,
    spec_by_declared_name,
)
from kiro_crew.agent_spec_format import (
    agent_spec_candidates,
    is_agent_spec_name,
    is_markdown_spec,
    iter_agent_spec_files,
    parse_agent_spec_text,
    parse_markdown_spec,
    shadowed_markdown_specs,
    spec_stem,
    spec_suffix,
    split_markdown_spec,
)

# The reporter's own probe file, verbatim: it loads under ``kiro-cli --v3`` and
# was invisible to Kiro Crew.
PROBE = """---
name: kas-md-probe
description: >
  A minimal markdown-format probe agent created to verify that kiro-cli v3 (KAS)
  loads agent definitions authored in markdown, not just JSON. Answers questions
  tersely and identifies itself as the markdown probe agent.
tools: ["read"]
---

# KAS Markdown Probe

You are `kas-md-probe`, a test agent defined entirely in markdown format under
`~/.kiro/agents/`. Your only job is to confirm that a markdown agent loads and
activates under the KAS (v3) engine.

## Behavior

- When asked who you are, reply exactly: "I am kas-md-probe, loaded from a
  markdown agent file." Then state that markdown agent loading works.
- Keep every answer to one or two sentences.
- Do not use any tool unless explicitly asked to read a file.
"""


def _md(name: str, body: str = "# Prompt\n\nDo the thing.\n", **fields: Any) -> str:
    front = {"name": name, **fields}
    lines = ["---"]
    for key, value in front.items():
        lines.append(f"{key}: {json.dumps(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


@pytest.fixture
def agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pinned agents dir: every resolver under test reads THIS directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: d)
    clear_list_agents_cache()
    yield d
    clear_list_agents_cache()


# ── the parser ──────────────────────────────────────────────────────────────


class TestParser:
    def test_the_probe_parses_to_the_json_shape(self) -> None:
        spec = parse_markdown_spec(PROBE)
        assert spec["name"] == "kas-md-probe"
        assert spec["tools"] == ["read"]
        assert spec["description"].startswith("A minimal markdown-format probe agent")
        assert spec["prompt"].startswith("# KAS Markdown Probe")
        assert "Do not use any tool" in spec["prompt"]

    def test_body_is_the_prompt_even_when_frontmatter_declares_one(self) -> None:
        spec = parse_markdown_spec("---\nname: a\nprompt: from-frontmatter\n---\nbody wins\n")
        assert spec["prompt"] == "body wins\n"

    def test_empty_body_keeps_the_frontmatter_prompt(self) -> None:
        spec = parse_markdown_spec("---\nname: a\nprompt: file://prompts/a.md\n---\n\n")
        assert spec["prompt"] == "file://prompts/a.md"

    def test_nested_yaml_survives(self) -> None:
        text = (
            '---\nname: a\nmcpServers:\n  srv:\n    command: x\n    args: ["-a"]\n'
            'permissions:\n  rules:\n    - { capability: fs_read, match: ["**"], effect: allow }\n---\nhi\n'
        )
        spec = parse_markdown_spec(text)
        assert spec["mcpServers"] == {"srv": {"command": "x", "args": ["-a"]}}
        assert spec["permissions"]["rules"][0]["capability"] == "fs_read"

    def test_bom_and_crlf_are_tolerated(self) -> None:
        spec = parse_markdown_spec("\ufeff---\r\nname: a\r\n---\r\nline one\r\n")
        assert spec["name"] == "a"
        assert spec["prompt"] == "line one\r\n"

    def test_a_dashed_junk_line_does_not_close_the_fence(self) -> None:
        """``---junk`` is body text; an unclosed fence is not a spec at all."""
        assert split_markdown_spec("---\nname: a\n---junk\nbody\n") is None
        with pytest.raises(ValueError):
            parse_markdown_spec("---\nname: a\n---junk\nbody\n")

    def test_plain_markdown_is_not_a_spec(self) -> None:
        assert split_markdown_spec("# README\n\nnotes\n") is None
        with pytest.raises(ValueError):
            parse_markdown_spec("# README\n\nnotes\n")

    def test_non_mapping_frontmatter_is_refused(self) -> None:
        with pytest.raises(ValueError):
            parse_markdown_spec("---\n- a\n- b\n---\nbody\n")

    def test_invalid_yaml_is_a_value_error_like_bad_json(self) -> None:
        with pytest.raises(ValueError):
            parse_markdown_spec("---\nname: [unclosed\n---\nbody\n")

    def test_empty_frontmatter_is_an_empty_mapping(self) -> None:
        assert parse_markdown_spec("---\n---\nbody\n") == {"prompt": "body\n"}

    def test_comma_separated_tools_string_becomes_the_list_shape(self) -> None:
        """The v3 loader accepts ``tools: read, write``; the JSON shape is a list."""
        assert parse_markdown_spec("---\ntools: read, write ,\n---\nhi\n")["tools"] == [
            "read",
            "write",
        ]
        assert parse_markdown_spec("---\ntools: '*'\n---\nhi\n")["tools"] == "*"
        assert "tools" not in parse_markdown_spec("---\ntools: ''\n---\nhi\n")
        assert parse_markdown_spec("---\ntools: [read]\n---\nhi\n")["tools"] == ["read"]

    def test_frontmatter_always_loads_to_json_types(self) -> None:
        """Every consumer re-serializes the parsed spec as JSON (the KAS
        projection over JSON-RPC, the overlay, the roster), so a YAML value
        with no JSON form would be a crash there rather than a quirk here. An
        unquoted date stays the text the author typed -- kiro-cli's own YAML
        reader has no date type and reads it the same way -- and the rest
        (binary, sets, non-string keys, alias cycles, infinities) is refused
        with the key path named."""
        spec = parse_markdown_spec(
            "---\nname: a\nwhen: 2026-09-16\nargs: [2026-09-16, 2026-09-16T01:02:03Z]\n---\nhi\n"
        )
        assert spec["when"] == "2026-09-16"
        assert spec["args"] == ["2026-09-16", "2026-09-16T01:02:03Z"]
        json.dumps(spec)  # the projection's serialization must not raise

        # Nesting deep enough to exhaust the interpreter stack is bad content
        # like any other, not a ``RecursionError`` the caller has to know about.
        deep = "x: " + "[" * 5000 + "]" * 5000
        with pytest.raises(ValueError, match="nested too deeply"):
            parse_markdown_spec(f"---\n{deep}\n---\nhi\n")

        for frontmatter, where in (
            ("blob: !!binary aGk=", "frontmatter.blob"),
            ("s: !!set {x, y}", "frontmatter.s"),
            ("1: x", "key 1"),
            ("self: &a [*a]", "frontmatter.self[0]"),
            ("f: .inf", "frontmatter.f"),
            ("mcpServers:\n  srv:\n    timeout: .nan", "frontmatter.mcpServers.srv.timeout"),
        ):
            with pytest.raises(ValueError, match=where.replace("[", "\\[").replace("]", "\\]")):
                parse_markdown_spec(f"---\nname: a\n{frontmatter}\n---\nhi\n")

    def test_dispatch_by_suffix(self, tmp_path: Path) -> None:
        assert parse_agent_spec_text('{"name": "j"}', tmp_path / "j.json") == {"name": "j"}
        assert parse_agent_spec_text('{"name": "j"}', tmp_path / "j.JSON") == {"name": "j"}
        assert parse_agent_spec_text(_md("m"), tmp_path / "m.md")["name"] == "m"
        assert parse_agent_spec_text(_md("m"), tmp_path / "m.MD")["name"] == "m"

    def test_suffix_helpers(self) -> None:
        assert spec_suffix("a.json") == ".json"
        assert spec_suffix("A.MD") == ".md"
        assert spec_suffix("notes.txt") is None
        assert is_agent_spec_name("x.md") and not is_agent_spec_name("x.md.bak")
        assert is_markdown_spec(Path("/x/y.md")) and not is_markdown_spec("y.json")
        assert spec_stem("pkg-agent.json") == "pkg-agent"
        assert spec_stem("agent.md") == "agent"
        assert spec_stem("other.txt") == "other.txt"

    def test_iteration_covers_both_forms_and_only_those(self, tmp_path: Path) -> None:
        for name in ("b.md", "a.json", "README.txt", "c.json.bak", "d.md"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        assert [p.name for p in iter_agent_spec_files(tmp_path)] == ["a.json", "b.md", "d.md"]
        assert {p.name for p in iter_agent_spec_files(tmp_path, ordered=False)} == {
            "a.json",
            "b.md",
            "d.md",
        }
        assert [p.name for p in agent_spec_candidates(tmp_path, "z")] == ["z.json", "z.md"]
        assert shadowed_markdown_specs(tmp_path) == []

    def test_a_json_twin_shadows_the_markdown_file(self, tmp_path: Path) -> None:
        """The JSON twin is the workaround users kept while only JSON was read;
        it stays the live file, and the scan names what it hides."""
        for name in ("a.json", "a.md", "b.md"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        assert [p.name for p in iter_agent_spec_files(tmp_path)] == ["a.json", "b.md"]
        assert [p.name for p in shadowed_markdown_specs(tmp_path)] == ["a.md"]
        assert shadowed_markdown_specs(tmp_path / "absent") == []


# ── the roster and its cache ────────────────────────────────────────────────


class TestRoster:
    def test_the_probe_is_listed_with_its_fields(self, agents_dir: Path) -> None:
        (agents_dir / "kas-md-probe.md").write_text(PROBE, encoding="utf-8")
        (agents_dir / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")

        rows = {a.name: a for a in list_agents(agents_dir=agents_dir)}

        assert set(rows) == {"kas-md-probe", "plain"}
        probe = rows["kas-md-probe"]
        assert probe.filename == "kas-md-probe.md"
        assert probe.description.startswith("A minimal markdown-format probe agent")
        assert probe.scope == "global"

    def test_a_readme_in_the_agents_dir_is_not_an_agent(self, agents_dir: Path) -> None:
        (agents_dir / "README.md").write_text("# not an agent\n", encoding="utf-8")
        (agents_dir / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")

        assert [a.name for a in list_agents(agents_dir=agents_dir)] == ["plain"]
        assert agent_spec_stems(agents_dir, operation="t", source="unknown") == ["plain"]

    def test_a_markdown_edit_invalidates_the_roster_cache(self, agents_dir: Path) -> None:
        """Trap: a signature that fingerprints ``*.json`` only would serve the
        old description forever after the markdown file changed."""
        path = agents_dir / "bot.md"
        path.write_text(_md("bot", description="v1"), encoding="utf-8")
        assert list_agents(agents_dir=agents_dir)[0].description == "v1"

        path.write_text(_md("bot", description="v2"), encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))

        assert list_agents(agents_dir=agents_dir)[0].description == "v2"

    def test_a_new_markdown_file_alone_invalidates_the_roster_cache(self, agents_dir: Path) -> None:
        (agents_dir / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")
        assert [a.name for a in list_agents(agents_dir=agents_dir)] == ["plain"]

        (agents_dir / "bot.md").write_text(_md("bot"), encoding="utf-8")

        assert {a.name for a in list_agents(agents_dir=agents_dir)} == {"plain", "bot"}

    def test_project_scope_reads_markdown_too(self, agents_dir: Path, tmp_path: Path) -> None:
        proj = tmp_path / "repo"
        d = proj / ".kiro" / "agents"
        d.mkdir(parents=True)
        (d / "repobot.md").write_text(_md("repobot"), encoding="utf-8")

        assert [p.name for p in project_agent_files(proj)] == ["repobot.md"]
        assert "repobot" in project_agent_names(proj)
        rows = list_agents(agents_dir=agents_dir, project_dir=str(proj))
        assert [(a.name, a.scope) for a in rows] == [("repobot", "project")]

    def test_model_map_and_declared_name_scan_read_markdown(self, agents_dir: Path) -> None:
        (agents_dir / "Pkg-bot.md").write_text(_md("bot", model="m-md"), encoding="utf-8")

        assert agent_model_map(agents_dir, operation="t", source="unknown")["bot"] == "m-md"
        found = spec_by_declared_name(agents_dir, "bot", operation="t", source="unknown")
        assert found is not None and found["model"] == "m-md"

    def test_a_json_twin_wins_and_the_roster_warns(
        self, agents_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The pre-fix workaround (a ``.json`` copy beside the ``.md``) keeps
        working after upgrade: one agent, the JSON one, plus a warning naming
        the file that is not read."""
        (agents_dir / "bot.json").write_text(
            json.dumps({"name": "bot", "description": "json twin"}), encoding="utf-8"
        )
        (agents_dir / "bot.md").write_text(_md("bot", description="md twin"), encoding="utf-8")

        with caplog.at_level("WARNING", logger="kiro_crew.agent_discovery"):
            rows = list_agents(agents_dir=agents_dir)

        assert [(a.name, a.description) for a in rows] == [("bot", "json twin")]
        assert any("bot.md is shadowed" in r.getMessage() for r in caplog.records)

    def test_two_markdown_specs_declaring_one_name_are_ambiguous(self, agents_dir: Path) -> None:
        (agents_dir / "a.md").write_text(_md("bot"), encoding="utf-8")
        (agents_dir / "b.md").write_text(_md("bot"), encoding="utf-8")
        with pytest.raises(AmbiguousAgentSpecError):
            spec_by_declared_name(agents_dir, "bot", operation="t", source="unknown")


# ── the KAS projection ──────────────────────────────────────────────────────


class TestKasProjection:
    def test_the_direct_markdown_file_is_the_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "kas-md-probe.md").write_text(PROBE, encoding="utf-8")
        spec = kas_agents.load_agent_spec(tmp_path, "kas-md-probe")
        assert spec["tools"] == ["read"]

        [agent] = build_kas_custom_agents(tmp_path, "kas-md-probe", spec)
        assert agent["id"] == "kas-md-probe"
        assert agent["tools"] == ["read"]
        assert agent["prompt"].startswith("# KAS Markdown Probe")

    def test_the_json_twin_is_projected_over_the_markdown_one(self, tmp_path: Path) -> None:
        """Both declare the id: the JSON twin is the one KAS receives, so a
        workaround user's session keeps the spec it ran on before."""
        (tmp_path / "bot.json").write_text(
            json.dumps({"name": "bot", "description": "json"}), encoding="utf-8"
        )
        (tmp_path / "bot.md").write_text(_md("bot", description="md"), encoding="utf-8")
        assert kas_agents.load_agent_spec(tmp_path, "bot")["description"] == "json"

        # Neither declares the id: the direct-filename fallback picks JSON too.
        (tmp_path / "other.json").write_text(json.dumps({"name": "x"}), encoding="utf-8")
        (tmp_path / "other.md").write_text(_md("y"), encoding="utf-8")
        assert kas_agents.load_agent_spec(tmp_path, "other")["name"] == "x"

    def test_a_broken_markdown_file_is_a_translation_error(self, tmp_path: Path) -> None:
        (tmp_path / "bot.md").write_text("# no fence\n", encoding="utf-8")
        with pytest.raises(KasAgentTranslationError, match="not a valid spec"):
            kas_agents.load_agent_spec(tmp_path, "bot")

    def test_a_symlink_to_a_sensitive_file_is_refused_not_projected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The direct-filename fallback reads through the hardened gate: a
        symlink in the user-writable agents directory is resolved and its
        target vetted, so a fenced document living somewhere sensitive is
        refused instead of becoming a KAS agent's prompt."""
        secret = tmp_path / "vault" / "notes.md"
        secret.parent.mkdir()
        secret.write_text(_md("bot", body="the secret\n"), encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        try:
            os.symlink(secret, agents / "bot.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_path", lambda p: str(p) == str(secret.resolve())
        )

        with pytest.raises(KasAgentTranslationError, match="sensitive"):
            kas_agents.load_agent_spec(agents, "bot")


# ── resolvers that write ────────────────────────────────────────────────────


class TestWriters:
    def test_agent_spec_path_resolves_a_markdown_spec(self, agents_dir: Path) -> None:
        (agents_dir / "bot.md").write_text(_md("bot"), encoding="utf-8")
        assert agent_mod.agent_spec_path("bot") == agents_dir / "bot.md"

    def test_agent_spec_path_prefers_the_json_twin(self, agents_dir: Path) -> None:
        (agents_dir / "bot.json").write_text(json.dumps({"name": "bot"}), encoding="utf-8")
        (agents_dir / "bot.md").write_text(_md("bot"), encoding="utf-8")
        assert agent_mod.agent_spec_path("bot") == agents_dir / "bot.json"
        # And the writer built on it still writes, since the JSON is the live file.
        path, previous = agent_mod.reset_agent_model("bot")
        assert path == agents_dir / "bot.json" and previous == ""

    def test_reset_agent_model_refuses_a_markdown_spec(self, agents_dir: Path) -> None:
        """Serializing a JSON object over the file would destroy the prompt body."""
        path = agents_dir / "bot.md"
        text = _md("bot", model="pinned")
        path.write_text(text, encoding="utf-8")

        with pytest.raises(ValueError, match="defined in markdown"):
            agent_mod.reset_agent_model("bot")

        assert path.read_text(encoding="utf-8") == text

    def test_migrate_agent_specs_leaves_markdown_alone(self, agents_dir: Path) -> None:
        path = agents_dir / "bot.md"
        text = _md("bot", model_managed=True)
        path.write_text(text, encoding="utf-8")

        assert agent_mod.migrate_agent_specs() == 0
        assert path.read_text(encoding="utf-8") == text

    def test_markdown_spec_for_agent_names_the_file(self, agents_dir: Path, tmp_path: Path) -> None:
        (agents_dir / "bot.md").write_text(_md("bot"), encoding="utf-8")
        (agents_dir / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")
        assert agent_mod.markdown_spec_for_agent("bot") == agents_dir / "bot.md"
        assert agent_mod.markdown_spec_for_agent("plain") is None
        assert agent_mod.markdown_spec_for_agent("absent") is None

        proj = tmp_path / "repo"
        d = proj / ".kiro" / "agents"
        d.mkdir(parents=True)
        # A project ``plain.md`` beside the user-level ``plain.json``: kiro-cli
        # does not see markdown, so it runs the JSON spec, and the harness must
        # not refuse a valid agent. Markdown-only in BOTH scopes is refused, and
        # the project file is the one named, as it is for kiro-cli's --agent.
        (d / "plain.md").write_text(_md("plain"), encoding="utf-8")
        assert agent_mod.markdown_spec_for_agent("plain", proj) is None
        (d / "bot.md").write_text(_md("bot"), encoding="utf-8")
        assert agent_mod.markdown_spec_for_agent("bot", proj) == d / "bot.md"
        # And the other way round: a project JSON spec makes a user-level
        # markdown-only agent runnable, so nothing is refused.
        (d / "bot.json").write_text(json.dumps({"name": "bot"}), encoding="utf-8")
        assert agent_mod.markdown_spec_for_agent("bot", proj) is None


# ── the MCP gateway rewriter ────────────────────────────────────────────────


@pytest.fixture
def _rewriter_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.mcp_gateway import rewriter

    monkeypatch.setattr(rewriter, "forward_declared_env_enabled", lambda: True)
    monkeypatch.setattr(rewriter, "pool_identity_env_keys", lambda: frozenset())


def _rewrite(root: Path) -> tuple[dict[str, int], dict[str, str]]:
    from kiro_crew.mcp_gateway.rewriter import rewrite_agents

    settings = root / "settings" / "mcp.json"
    if not settings.exists():
        # Written once: the settings file is a fingerprint input, so rewriting
        # it on every pass would itself invalidate the cache under test.
        settings.parent.mkdir(exist_ok=True)
        settings.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return rewrite_agents(
        source_dir=root / "agents",
        overlay_dir=root / "mcp-gateway" / "agents",
        socket_path=root / "gw.sock",
        work_dir=root / "wd",
        stub_servers=frozenset({"srv"}),
    )


class TestRewriter:
    def test_a_markdown_spec_gets_a_json_overlay_with_its_servers_stubbed(
        self, tmp_path: Path, _rewriter_flags: None
    ) -> None:
        """Trap: unstubbed, a markdown agent's servers would spawn direct and
        bypass the tool gate. The overlay is ``<stem>.json`` because the
        session-level lookup resolves ``<agent>.json``."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER
        from kiro_crew.mcp_gateway.session_servers import injection_server_names

        src = tmp_path / "agents"
        src.mkdir()
        (src / "mdbot.md").write_text(
            "---\nname: mdbot\nmcpServers:\n  srv:\n    command: "
            + json.dumps(sys.executable)
            + '\n    args: ["-x"]\n---\nprompt\n',
            encoding="utf-8",
        )

        results, _env = _rewrite(tmp_path)

        overlay = tmp_path / "mcp-gateway" / "agents" / "mdbot.json"
        assert overlay.is_file()
        assert results == {"mdbot.json": 1}
        spec = json.loads(overlay.read_text(encoding="utf-8"))
        assert spec["mcpServers"]["srv"][_WRAPPER_MARKER] is True
        assert injection_server_names(overlay.parent, "mdbot") == frozenset({"srv"})

    def test_a_markdown_edit_invalidates_the_rewrite_cache(
        self, tmp_path: Path, _rewriter_flags: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.mcp_gateway import rewriter

        src = tmp_path / "agents"
        src.mkdir()
        path = src / "mdbot.md"
        path.write_text(
            "---\nname: mdbot\nmcpServers:\n  srv:\n    command: "
            + json.dumps(sys.executable)
            + "\n---\nv1\n",
            encoding="utf-8",
        )
        calls = {"n": 0}
        real = rewriter._rewrite_single_spec

        def spy(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(rewriter, "_rewrite_single_spec", spy)

        _rewrite(tmp_path)
        assert calls["n"] == 1
        _rewrite(tmp_path)
        assert calls["n"] == 1, "unchanged inputs must serve the cached overlay"

        path.write_text(path.read_text(encoding="utf-8").replace("v1", "v2"), encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
        _rewrite(tmp_path)
        assert calls["n"] == 2, "a markdown edit must invalidate the fingerprint"

    def test_a_symlinked_sensitive_source_is_skipped_deterministically(
        self, tmp_path: Path, _rewriter_flags: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A symlink in the agents directory is resolved and vetted before the
        read, so a sensitive target never lands in an overlay -- and the skip is
        the deterministic kind (the pass stays cacheable), not a transient keep."""
        from kiro_crew.mcp_gateway.rewriter import _FINGERPRINT_NAME

        secret = tmp_path / "vault" / "notes.md"
        secret.parent.mkdir()
        secret.write_text(
            "---\nname: evil\nmcpServers:\n  srv:\n    command: x\n---\nprompt\n",
            encoding="utf-8",
        )
        src = tmp_path / "agents"
        src.mkdir()
        try:
            os.symlink(secret, src / "evil.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_path", lambda p: str(p) == str(secret.resolve())
        )

        results, _env = _rewrite(tmp_path)

        assert results == {}
        assert not (tmp_path / "mcp-gateway" / "agents" / "evil.json").exists()
        assert (tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME).is_file()

    def test_a_readme_produces_no_overlay(self, tmp_path: Path, _rewriter_flags: None) -> None:
        src = tmp_path / "agents"
        src.mkdir()
        (src / "README.md").write_text("# notes\n", encoding="utf-8")
        _rewrite(tmp_path)
        assert not (tmp_path / "mcp-gateway" / "agents" / "README.json").exists()


# ── the Connections census ──────────────────────────────────────────────────


def test_a_markdown_sharer_is_counted_by_the_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trap: a census that lists ``*.json`` only reads a markdown sharer as
    absent and lets a Disconnect revoke a grant it is still using."""
    from kiro_crew import mcp_discovery
    from kiro_crew.config import paths as connections_paths
    from kiro_crew.connections import ownership
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(mcp_discovery, "_MCP_SOURCES", ((tmp_path / "mcp.json", "kirocrew"),))
    monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", list)
    monkeypatch.setattr(connections_paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(mcp_handlers, "_extra_mcp_scopes", list)
    (agents / "research.md").write_text(
        "---\nname: research\nmcpServers:\n  notion:\n    url: https://mcp.example.test/mcp\n---\nhi\n",
        encoding="utf-8",
    )
    (agents / "README.md").write_text("# notes\n", encoding="utf-8")

    specs, unreadable = ownership.spec_census()

    assert specs["agent:research.md"] == {"notion": {"url": "https://mcp.example.test/mcp"}}
    # A README is not a spec: it declares nothing, so it neither blocks a
    # revoke as a sharer nor makes the census incomplete.
    assert unreadable == ()
    assert specs.get("agent:README.md", {}) == {}


# ── the kiro-cli harness ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kiro_cli_backend_refuses_a_markdown_only_agent(
    agents_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trap: kiro-cli discovers ``*.json`` only, so a markdown-only agent would
    spawn and then fault every turn with ``Mode not found``. Refuse before the
    spawn, naming the file and the backend that can run it."""
    from kiro_crew import sandbox as sandbox_mod
    from kiro_crew.acp import client as client_mod
    from kiro_crew.acp.session_handle import AcpRuntimeError

    async def _bin(*, environ, home):  # type: ignore[no-untyped-def]
        return "/pinned/kiro-cli"

    monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(sandbox_mod, "delegated_workspace_exposes_agents_dir", lambda work_dir: "")
    (agents_dir / "kas-md-probe.md").write_text(PROBE, encoding="utf-8")
    (agents_dir / "plain.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    def ctx(agent: str) -> SpawnContext:
        return SpawnContext(agent=agent, work_dir=str(work), model=None, environ={}, home=tmp_path)

    with pytest.raises(AcpRuntimeError) as exc:
        await harness_for(ACP_BACKEND_KIRO).resolve_spawn(ctx("kas-md-probe"))
    assert "kas-md-probe.md" in str(exc.value)
    assert "'kas'" in str(exc.value)

    plan = await harness_for(ACP_BACKEND_KIRO).resolve_spawn(ctx("plain"))
    assert plan.argv[:4] == ["/pinned/kiro-cli", "acp", "--agent", "plain"]


# ── the other readers ───────────────────────────────────────────────────────


def test_doctor_dead_path_walk_reads_markdown(tmp_path: Path) -> None:
    from kiro_crew import doctor_deadpath as dp

    spec = tmp_path / "bot.md"
    spec.write_text(
        "---\nname: bot\nmcpServers:\n  gone:\n    command: /definitely/not/here/bin\n---\nhi\n",
        encoding="utf-8",
    )
    dead, unreadable = dp._walk_spec(spec)
    assert unreadable is None
    assert [d.server for d in dead] == ["gone"]

    (tmp_path / "README.md").write_text("# notes\n", encoding="utf-8")
    dead, unreadable = dp._walk_spec(tmp_path / "README.md")
    assert dead == [] and unreadable is not None and "frontmatter" in unreadable


def test_crew_context_opt_out_is_read_from_markdown(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import context

    monkeypatch.setattr(context, "kiro_agents_dir", lambda: agents_dir)
    (agents_dir / "quiet.md").write_text(_md("quiet", includeCrewContext=False), encoding="utf-8")
    (agents_dir / "loud.md").write_text(_md("loud"), encoding="utf-8")

    assert context._read_include_crew_context("quiet") is False
    assert context._read_include_crew_context("loud") is True


def test_materialized_names_and_model_resolver_read_markdown(agents_dir: Path) -> None:
    from kiro_crew.config import loader

    (agents_dir / "Pkg-bot.md").write_text(_md("bot", model="m-md"), encoding="utf-8")
    assert "bot" in loader._scan_materialized_agents(agents_dir)
    assert loader.KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=agents_dir) == "m-md"


def test_slack_agent_names_read_markdown_and_skip_a_readme(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack import events, handler

    monkeypatch.setattr(events, "kiro_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(handler, "kiro_agents_dir", lambda: agents_dir)
    (agents_dir / "Pkg-bot.md").write_text(_md("bot"), encoding="utf-8")
    (agents_dir / "README.md").write_text("# notes\n", encoding="utf-8")
    (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")

    assert sorted(events._get_agent_names()) == ["bot", "broken"]
    assert events._selector_agent_names() == ["Pkg-bot", "broken"]
    # The name resolver reads the declared name out of a markdown match whose
    # stem differs from it, instead of tripping over a non-JSON body.
    assert handler._resolve_agent_name("bot") == "bot"
    # A broken JSON spec still occupies its name, as it always has; a markdown
    # file that is not a spec resolves to nothing, the listing's rule, so a
    # README cannot be persisted as a thread's agent.
    assert handler._resolve_agent_name("broken") == "broken"
    assert handler._resolve_agent_name("README") is None
