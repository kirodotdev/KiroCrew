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
* the runtime's activation guard (a markdown-only agent on the kiro-cli
  backend is refused at session start with a message naming the file and KAS,
  never left running the host's default agent in its place).

Every test writes under ``tmp_path`` and pins the agents directory it uses.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery
from kiro_crew.acp import kas_agents
from kiro_crew.acp.harness import harness_for
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
        (binary, sets, non-string keys, infinities) is refused with the key
        path named. Aliases are refused at the loader: a shared reference is
        what would make the document a graph (a cycle recurses forever, a DAG
        of shared containers expands exponentially on serialization), so
        without them the value is a tree and the walk is linear."""
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

        # A 40-level alias DAG expands to 2**40 items if shared containers are
        # re-walked or serialized; it is refused at the first alias instead.
        dag = "a0: &a0 [x]\n" + "".join(
            f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]\n" for i in range(1, 40)
        )
        with pytest.raises(ValueError, match=r"aliases \(\*a0\) are not supported"):
            parse_markdown_spec(f"---\n{dag}---\nhi\n")

        for frontmatter, where in (
            ("blob: !!binary aGk=", "frontmatter.blob"),
            ("s: !!set {x, y}", "frontmatter.s"),
            ("1: x", "key 1"),
            ("self: &a [*a]", r"aliases \(\*a\) are not supported"),
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

    def test_twins_differing_only_by_case_are_shadowed_where_the_filesystem_folds_case(
        self, tmp_path: Path
    ) -> None:
        """Trap: ``Foo.json`` + ``foo.md`` are one agent on Windows and default
        macOS -- their ``<stem>.json`` overlays are ONE file there, so a scan
        that let both live would have the second overlay hand one agent the
        other's MCP servers. The twin question is put to the filesystem, not to
        an exact-case stem set, so the answer follows the directory's rule."""
        for name in ("Foo.json", "foo.md", "bar.md"):
            (tmp_path / name).write_text("x", encoding="utf-8")

        class _CaseFoldingDir(type(Path())):  # type: ignore[misc]
            """A directory whose ``exists`` folds case, as NTFS and APFS do."""

            def exists(self, *args: Any, **kwargs: Any) -> bool:
                if super().exists(*args, **kwargs):
                    return True
                parent = Path(str(self.parent))
                return parent.is_dir() and any(
                    e.name.casefold() == self.name.casefold() for e in parent.iterdir()
                )

        folding = _CaseFoldingDir(tmp_path)
        assert sorted(p.name for p in iter_agent_spec_files(folding)) == ["Foo.json", "bar.md"]
        assert [p.name for p in shadowed_markdown_specs(folding)] == ["foo.md"]

    def test_twins_differing_only_by_case_are_distinct_agents_on_a_case_sensitive_filesystem(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Probe").write_text("", encoding="utf-8")
        if (tmp_path / "probe").exists():
            pytest.skip("case-insensitive filesystem: the folding rule applies here")
        for name in ("Foo.json", "foo.md"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        assert sorted(p.name for p in iter_agent_spec_files(tmp_path)) == ["Foo.json", "foo.md"]
        assert shadowed_markdown_specs(tmp_path) == []


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
            agent_discovery,
            "is_sensitive_canonical_path",
            lambda p: str(p) == str(secret.resolve()),
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

    def test_two_sources_whose_overlays_are_one_file_write_only_the_first(
        self,
        tmp_path: Path,
        _rewriter_flags: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Defence at the destination: a case-sensitive source directory keeps
        ``Foo.json`` and ``foo.md`` as two agents, but an overlay directory
        that folds case has one file for their two overlays. The second write
        is refused and named, instead of replacing the first agent's servers
        with the second's."""
        from kiro_crew.mcp_gateway import rewriter

        (tmp_path / "Probe").write_text("", encoding="utf-8")
        if (tmp_path / "probe").exists():
            pytest.skip("case-insensitive filesystem: the listing shadows the twin first")
        src = tmp_path / "agents"
        src.mkdir()
        (src / "Foo.json").write_text(
            json.dumps(
                {"name": "Foo", "mcpServers": {"srv": {"command": sys.executable, "args": ["-x"]}}}
            ),
            encoding="utf-8",
        )
        (src / "foo.md").write_text(
            "---\nname: foo\nmcpServers:\n  other:\n    command: "
            + json.dumps(sys.executable)
            + "\n---\nprompt\n",
            encoding="utf-8",
        )
        # Stand in for a case-folding overlay directory: the two spellings are
        # reported as one file.
        monkeypatch.setattr(rewriter, "_overlay_names_collide", lambda _d, a, b: True)

        with caplog.at_level("WARNING", logger=rewriter.logger.name):
            results, _env = _rewrite(tmp_path)

        overlay_dir = tmp_path / "mcp-gateway" / "agents"
        assert (overlay_dir / "Foo.json").is_file()
        first = json.loads((overlay_dir / "Foo.json").read_text(encoding="utf-8"))
        assert set(first["mcpServers"]) == {"srv"}, "the first agent keeps its own servers"
        assert results == {"Foo.json": 1}
        assert not (overlay_dir / "foo.json").exists() or os.path.samefile(
            overlay_dir / "foo.json", overlay_dir / "Foo.json"
        )
        assert any("foo.md" in r.message and "Foo.json" in r.message for r in caplog.records)

    def test_a_skipped_twin_does_not_claim_its_valid_sibling_s_overlay(
        self,
        tmp_path: Path,
        _rewriter_flags: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Trap: a malformed ``Foo.json`` sorts first, a stale ``foo.json``
        overlay exists, and the overlay directory folds case. Claiming the
        destination before reading the source would skip the valid ``foo.md``
        as a collision and then prune its overlay -- KAS would launch that
        agent's MCP servers outside the tool gate. Only a kept or parsed
        source claims a destination."""
        from kiro_crew.mcp_gateway import rewriter
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        (tmp_path / "Probe").write_text("", encoding="utf-8")
        if (tmp_path / "probe").exists():
            pytest.skip("case-insensitive filesystem: the listing shadows the twin first")
        src = tmp_path / "agents"
        src.mkdir()
        (src / "Foo.json").write_text("{not json", encoding="utf-8")
        (src / "foo.md").write_text(
            "---\nname: foo\nmcpServers:\n  srv:\n    command: "
            + json.dumps(sys.executable)
            + '\n    args: ["-x"]\n---\nprompt\n',
            encoding="utf-8",
        )
        overlay_dir = tmp_path / "mcp-gateway" / "agents"
        overlay_dir.mkdir(parents=True)
        (overlay_dir / "foo.json").write_text(
            json.dumps({"name": "foo", "mcpServers": {}}), encoding="utf-8"
        )
        # Stand in for a case-folding overlay directory.
        monkeypatch.setattr(rewriter, "_overlay_names_collide", lambda _d, a, b: True)

        with caplog.at_level("WARNING", logger=rewriter.logger.name):
            results, _env = _rewrite(tmp_path)

        assert results == {"foo.json": 1}
        spec = json.loads((overlay_dir / "foo.json").read_text(encoding="utf-8"))
        assert spec["mcpServers"]["srv"][_WRAPPER_MARKER] is True, "the valid twin is stubbed"
        assert not any(
            "same file as overlay" in r.message for r in caplog.records
        ), "a skipped source is not a collision partner"
        assert any("Foo.json" in r.message for r in caplog.records), "the bad source is named"

    def test_the_collision_probe_asks_the_overlay_directory_itself(self, tmp_path: Path) -> None:
        """Two distinct files under the two spellings (a stale overlay on a
        case-sensitive directory) are NOT a collision; the same file reached by
        both spellings is."""
        from kiro_crew.mcp_gateway.rewriter import _overlay_names_collide

        (tmp_path / "Probe").write_text("", encoding="utf-8")
        if (tmp_path / "probe").exists():
            pytest.skip("case-insensitive filesystem")
        (tmp_path / "Foo.json").write_text("{}", encoding="utf-8")
        assert _overlay_names_collide(tmp_path, "Foo.json", "foo.json") is False
        (tmp_path / "foo.json").write_text("{}", encoding="utf-8")
        assert _overlay_names_collide(tmp_path, "Foo.json", "foo.json") is False
        assert _overlay_names_collide(tmp_path, "Foo.json", "Foo.json") is True
        os.symlink(tmp_path / "Foo.json", tmp_path / "Same.json")
        assert _overlay_names_collide(tmp_path, "Foo.json", "Same.json") is True

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
        from kiro_crew.mcp_gateway import rewriter

        # Both the fingerprint (rewriter._source_sig) and the rewrite loop's
        # strict read (agent_discovery) vet the resolved target; the strict read
        # asks is_sensitive_canonical_path, the fingerprint is_sensitive_path.
        for module, gate in (
            (agent_discovery, "is_sensitive_canonical_path"),
            (rewriter, "is_sensitive_path"),
        ):
            monkeypatch.setattr(module, gate, lambda p: str(p) == str(secret.resolve()))

        results, _env = _rewrite(tmp_path)

        assert results == {}
        assert not (tmp_path / "mcp-gateway" / "agents" / "evil.json").exists()
        fingerprint = tmp_path / "mcp-gateway" / "agents" / _FINGERPRINT_NAME
        assert fingerprint.is_file()
        # The fingerprint signs sources by digest; a sensitive target is never
        # read, not even to hash it, so its entry is the unreadable marker.
        sources = json.loads(fingerprint.read_text(encoding="utf-8"))["inputs"]["sources"]
        assert sources["evil.md"] is None

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


def test_reading_markdown_specs_is_a_membership_answer() -> None:
    """The harness answers from ``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``, never
    from its own identity, so a host added later joins the set. KAS reads the
    form (Crew parses the spec and hands it over the wire); kiro-cli does not."""
    from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKENDS_MARKDOWN_AGENT_SPECS

    assert ACP_BACKENDS_MARKDOWN_AGENT_SPECS == frozenset({ACP_BACKEND_KAS})
    assert harness_for(ACP_BACKEND_KAS).reads_markdown_agent_specs is True
    assert harness_for(ACP_BACKEND_KIRO).reads_markdown_agent_specs is False


@pytest.mark.asyncio
async def test_the_activation_guard_explains_a_markdown_only_agent(
    agents_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kiro-cli discovers ``*.json`` only, so a markdown-only agent selected on
    that backend spawns, is not the active mode after ``session/new``, and trips
    the existing activation guard -- the SAME refusal a missing JSON spec trips,
    so the spawn path gains no gate (H13). What changes is the explanation: on a
    host that answers False to ``reads_markdown_agent_specs`` the guard names the
    markdown file and the backends that can run it instead of sending the
    operator to rewrite a JSON file that was never the problem."""
    from unittest.mock import AsyncMock

    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.acp.session_handle import AcpRuntimeError

    (agents_dir / "kas-md-probe.md").write_text(PROBE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    resp = {"modes": {"currentModeId": "vibe", "availableModes": [{"id": "vibe"}]}}

    rt = AcpRuntime(work_dir=str(work), agent="kas-md-probe", acp_backend=ACP_BACKEND_KIRO)
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(AcpRuntimeError) as exc:
        await rt._verify_spawn_agent_active("s1", resp, override=None)
    message = str(exc.value)
    assert "kas-md-probe.md" in message
    assert "'kas'" in message
    assert "kirocrew setup" not in message  # the JSON repair is not the remedy here
    rt.terminate_session.assert_awaited_once_with("s1")

    # A JSON agent that fails to load keeps the generic diagnosis unchanged.
    rt = AcpRuntime(work_dir=str(work), agent="plain", acp_backend=ACP_BACKEND_KIRO)
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(AcpRuntimeError) as exc:
        await rt._verify_spawn_agent_active("s1", resp, override=None)
    assert "plain.json is missing" in str(exc.value)
    assert "kirocrew setup --agent-only" in str(exc.value)


# ── the ratchet: every agents-dir scan goes through the shared iterator ──────

_ONE_FORM_SCAN_RE = re.compile(
    r"(agents_dir|kiro_agents_dir(?:_path)?\(\)|project_agents_dir\([^)]*\))\s*\.glob\(\s*[\"']\*\.json[\"']"
)

# The writers that are JSON-only BY DESIGN: a markdown spec is never rewritten by
# Kiro Crew, so a bookkeeping migration that reads-then-writes must not see it.
_ONE_FORM_SCANS_ALLOWED: dict[str, int] = {"kiro_crew/agent.py": 1}


def test_no_agents_dir_scan_sees_one_form_only() -> None:
    """The bug class this module fixes is a scan that globs ``*.json`` alone, so
    an agent kiro-cli v3 or Kiro IDE would run is invisible to it. Every scan of
    an agents directory goes through ``agent_spec_format.iter_agent_spec_files``;
    a bare ``agents_dir.glob("*.json")`` is allowed only where listed above,
    with the reason, so reintroducing one is a red test and not a docstring."""
    src = Path(__file__).resolve().parent.parent / "src"
    found: dict[str, int] = {}
    for path in sorted(src.rglob("*.py")):
        hits = sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if _ONE_FORM_SCAN_RE.search(line)
        )
        if hits:
            found[path.relative_to(src).as_posix()] = hits
    assert found == _ONE_FORM_SCANS_ALLOWED, (
        "an agents-dir scan globs one form only; route it through "
        "agent_spec_format.iter_agent_spec_files or list the writer here with its reason"
    )


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


def test_doctor_dead_path_walk_reads_through_the_hardened_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink in the agents dir is resolved and vetted before the doctor
    reads it: a sensitive target is reported as unreadable, never parsed."""
    from kiro_crew import doctor_deadpath as dp

    secret = tmp_path / "vault" / "notes.md"
    secret.parent.mkdir()
    secret.write_text(_md("bot", mcpServers={"gone": {"command": "/nope"}}), encoding="utf-8")
    try:
        os.symlink(secret, tmp_path / "bot.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    monkeypatch.setattr(
        agent_discovery,
        "is_sensitive_canonical_path",
        lambda p: str(p) == str(secret.resolve()),
    )
    dead, unreadable = dp._walk_spec(tmp_path / "bot.md")
    assert dead == []
    assert unreadable is not None and "sensitive" in unreadable


def test_active_mcp_endpoint_lookup_tolerates_a_null_server_map(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mcpServers: null`` declares no servers: an empty list, not a 500."""
    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    monkeypatch.setattr(mcp_handlers, "kiro_agents_dir_path", lambda: agents_dir)
    (agents_dir / "nullbot.md").write_text("---\nname: nullbot\nmcpServers: null\n---\nhi\n")
    (agents_dir / "bot.md").write_text(_md("bot", mcpServers={"srv": {"command": "x"}}))
    assert mcp_handlers._agent_mcp_server_names("nullbot") == []
    assert mcp_handlers._agent_mcp_server_names("bot") == ["srv"]
    assert mcp_handlers._agent_mcp_server_names("absent") is None


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


# ── the surfaces the other ACP harnesses receive an agent through ───────────
#
# claude-agent-acp, codex, opencode and goose never read ``~/.kiro/agents``
# themselves. What they get of an agent is what Crew reads out of its spec and
# carries: the prompt as ``[AGENT SYSTEM PROMPT]`` text, and the MCP servers and
# tool allowlist through each backend's mirror (``session_mcp``). Both readers
# go through ``agent_spec_path`` + ``_read_agent_spec``, so a markdown agent is
# the same agent to those hosts as a JSON one; these two pins keep that true.


def test_session_mcp_reads_a_markdown_spec_for_the_mirrored_hosts(
    agents_dir: Path, tmp_path: Path
) -> None:
    from kiro_crew.acp import session_mcp

    (agents_dir / "mdbot.md").write_text(
        _md("mdbot", mcpServers={"srv": {"command": sys.executable}}, tools=["@srv"]),
        encoding="utf-8",
    )
    spec = session_mcp._agent_spec_for("mdbot")
    assert spec is not None
    assert set(spec["mcpServers"]) == {"srv"}
    assert spec["tools"] == ["@srv"]
    # A project checkout's markdown spec outranks the user-level one, the same
    # order the JSON form follows.
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (project / ".kiro" / "agents" / "mdbot.md").write_text(
        _md("mdbot", mcpServers={"proj": {"command": sys.executable}}), encoding="utf-8"
    )
    project_spec = session_mcp._agent_spec_for("mdbot", project)
    assert project_spec is not None
    assert set(project_spec["mcpServers"]) == {"proj"}


def test_the_agent_system_prompt_is_the_markdown_body(agents_dir: Path) -> None:
    from kiro_crew.context import ContextBuilder

    # Bytes, not text: the reader keeps line endings as written, and a text
    # write on Windows would turn every ``\n`` below into ``\r\n``.
    (agents_dir / "mdbot.md").write_bytes(
        _md("mdbot", body="You are mdbot.\n\nAnswer tersely.\n").encode("utf-8")
    )
    assert ContextBuilder._load_agent_prompt("mdbot") == "You are mdbot.\n\nAnswer tersely.\n"
