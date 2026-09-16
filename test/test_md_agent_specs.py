"""Tests for ``kiro_crew.md_agent_specs`` -- markdown agent definitions.

Two halves, and the second is the one that protects a user's files: the pure
derivation (what a document compiles to, and what it refuses), and the ownership
transaction (what the compiler is allowed to overwrite or delete). Every
ownership test asserts on the FILE, not just the returned outcome -- the whole
point of the record is that a refusal leaves bytes untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import md_agent_specs as mas

FULL_DOC = """---
name: code-reviewer
description: Reviews code changes
model: claude-opus
tools: [fs_read, grep, glob, "@kirocrew-core"]
allowedTools: [fs_read, grep]
mcpServers:
  fetch:
    command: uvx
    args: [mcp-server-fetch]
---
You are a meticulous code reviewer.

Focus on correctness.
"""


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _isolated_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ownership record at a tmp data home, never the real one."""
    monkeypatch.setattr(mas, "data_home", lambda: tmp_path / "data")


def _write(agents_dir: Path, name: str, text: str) -> Path:
    path = agents_dir / name
    path.write_text(text, encoding="utf-8")
    return path


# ── derivation ────────────────────────────────────────────────────────────────


def test_full_document_compiles_every_carried_field() -> None:
    compiled = mas.derive_spec(FULL_DOC, fallback_name="ignored")
    assert compiled.name == "code-reviewer"
    assert compiled.spec["model"] == "claude-opus"
    # Typed structures survive: a flat string-only frontmatter reader could not
    # express either of these, which is why this format parses real YAML.
    assert compiled.spec["tools"] == ["fs_read", "grep", "glob", "@kirocrew-core"]
    assert compiled.spec["mcpServers"]["fetch"]["args"] == ["mcp-server-fetch"]
    assert compiled.spec["prompt"] == (
        "You are a meticulous code reviewer.\n\nFocus on correctness."
    )


def test_name_falls_back_to_the_file_stem() -> None:
    compiled = mas.derive_spec("---\ndescription: d\n---\nbody\n", fallback_name="from-stem")
    assert compiled.name == "from-stem"


def test_unknown_frontmatter_key_is_dropped_not_refused() -> None:
    # kiro-cli rejects a spec wholesale on an unknown key, so carrying one
    # through would yield no agent at all. Dropping keeps a ported file usable.
    compiled = mas.derive_spec("---\nname: a\nexcludedTools: [x]\n---\nb\n", fallback_name="a")
    assert "excludedTools" not in compiled.spec
    assert compiled.name == "a"


def test_bom_and_crlf_document_compiles() -> None:
    raw = "\ufeff---\r\nname: winagent\r\ndescription: d\r\n---\r\nBody here.\r\n".encode("utf-8")
    compiled = mas.derive_spec(mas._decode(raw), fallback_name="x")
    assert compiled.name == "winagent"
    assert compiled.spec["prompt"] == "Body here."


def test_empty_frontmatter_block_is_a_fence_not_a_miss() -> None:
    compiled = mas.derive_spec("---\n---\nOnly a prompt.\n", fallback_name="stem")
    assert compiled.name == "stem"
    assert compiled.spec["prompt"] == "Only a prompt."


def test_body_only_whitespace_emits_no_prompt_key() -> None:
    compiled = mas.derive_spec("---\nname: a\n---\n\n  \n", fallback_name="a")
    assert "prompt" not in compiled.spec


@pytest.mark.parametrize(
    ("code", "doc"),
    [
        ("no_frontmatter", "just prose\n"),
        # A '---junk' line is NOT a closer: treating it as one would move the
        # remaining frontmatter into the prompt silently.
        ("no_frontmatter", "---\nname: a\n---junk\nbody\n"),
        ("no_frontmatter", "---\nname: a\nnever closed\n"),
        ("frontmatter_not_mapping", "---\n- a\n- b\n---\nbody\n"),
        ("reserved_key", "---\nname: a\nprompt: hi\n---\nbody\n"),
        ("unsafe_name", "---\nname: 'bad name!'\n---\nbody\n"),
        ("unsafe_name", "---\nname: 42\n---\nbody\n"),
        ("reserved_name", "---\nname: kirocrew\n---\nbody\n"),
        ("bad_yaml", "---\nname: [unclosed\n---\nbody\n"),
    ],
)
def test_refusals(code: str, doc: str) -> None:
    with pytest.raises(mas.MdAgentSpecError) as excinfo:
        mas.derive_spec(doc, fallback_name="fallback")
    assert excinfo.value.code == code


def test_payload_matches_the_atomic_writer_byte_for_byte() -> None:
    """Ownership compares digests, so these two serializations must not drift.

    If ``_spec_payload`` and ``agent._atomic_json_write`` ever disagree, every
    compiled spec reads as hand-edited on the next pass and the compiler stops
    touching its own output -- a silent total failure. This pins them together.
    """
    from kiro_crew.agent import _atomic_json_write

    compiled = mas.derive_spec(FULL_DOC, fallback_name="x")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "out.json"
        _atomic_json_write(target, compiled.spec)
        assert target.read_bytes() == compiled.payload


# ── compile / ownership ───────────────────────────────────────────────────────


def test_compile_writes_the_spec_and_is_idempotent(agents_dir: Path) -> None:
    _write(agents_dir, "code-reviewer.md", FULL_DOC)

    first = mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "code-reviewer.json"
    assert first.written == ["code-reviewer"]
    assert json.loads(target.read_text())["name"] == "code-reviewer"

    stamp = target.stat().st_mtime_ns
    second = mas.compile_markdown_agents(agents_dir)
    assert second.written == []
    assert second.unchanged == ["code-reviewer"]
    # Not merely "same content": an unnecessary rewrite bumps the directory
    # signature every roster cache is keyed on.
    assert target.stat().st_mtime_ns == stamp


def test_the_record_never_lands_in_the_agents_directory(agents_dir: Path) -> None:
    _write(agents_dir, "a.md", "---\nname: a\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    # Several sweeps enumerate this directory and glob('*.json') matches
    # dotfiles, so a record kept here would be prunable by another pass. The
    # only extra file tolerated is agents_spec_lock's own sidecar, which this
    # module does not create and every other spec writer also leaves behind.
    produced = {p.name for p in agents_dir.iterdir() if not p.name.startswith(".")}
    assert produced == {"a.md", "a.json"}
    assert mas._record_path().is_file()
    assert mas._record_path().parent != agents_dir


def test_a_hand_written_spec_of_the_same_name_is_never_overwritten(agents_dir: Path) -> None:
    mine = {"name": "code-reviewer", "description": "hand written"}
    (agents_dir / "code-reviewer.json").write_text(json.dumps(mine), encoding="utf-8")
    _write(agents_dir, "code-reviewer.md", FULL_DOC)

    outcome = mas.compile_markdown_agents(agents_dir)

    assert [c for _, c, _ in outcome.refused] == ["name_taken"]
    assert outcome.written == []
    assert json.loads((agents_dir / "code-reviewer.json").read_text()) == mine


def test_an_edited_generated_spec_is_preserved_not_reverted(agents_dir: Path) -> None:
    _write(agents_dir, "a.md", "---\nname: a\ndescription: original\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "a.json"

    edited = {"name": "a", "description": "the user edited this"}
    target.write_text(json.dumps(edited), encoding="utf-8")
    # Change the source too, so the compiler genuinely wants to rewrite.
    _write(agents_dir, "a.md", "---\nname: a\ndescription: changed\n---\nbody\n")

    outcome = mas.compile_markdown_agents(agents_dir)

    assert [c for _, c, _ in outcome.refused] == ["derived_modified"]
    assert json.loads(target.read_text()) == edited


def test_a_removed_source_prunes_its_generated_spec(agents_dir: Path) -> None:
    source = _write(agents_dir, "a.md", "---\nname: a\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    assert (agents_dir / "a.json").is_file()

    source.unlink()
    outcome = mas.compile_markdown_agents(agents_dir)

    assert outcome.pruned == ["a"]
    assert not (agents_dir / "a.json").exists()
    assert mas._load_record() == {}


def test_a_removed_source_does_not_delete_an_edited_spec(agents_dir: Path) -> None:
    source = _write(agents_dir, "a.md", "---\nname: a\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "a.json"

    edited = {"name": "a", "description": "mine now"}
    target.write_text(json.dumps(edited), encoding="utf-8")
    source.unlink()

    outcome = mas.compile_markdown_agents(agents_dir)

    assert outcome.pruned == []
    assert json.loads(target.read_text()) == edited
    # The claim is retired, so a later pass treats the file as the user's.
    assert "a" not in mas._load_record()


def test_an_unreadable_existing_output_is_never_overwritten(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file we cannot read is not a file we may replace.

    "No bytes to compare" must not collapse into "nothing is there": the digest
    is the only ownership evidence, so an unreadable output has none and the
    compiler has to refuse. Without this the absent-file and unreadable-file
    cases share a code path and the compiler overwrites bytes it never inspected.
    """
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v1\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "a.json"
    before = target.read_bytes()

    monkeypatch.setattr(mas, "safe_read_file_bytes", lambda _p: None)
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v2\n---\nbody\n")

    outcome = mas.compile_markdown_agents(agents_dir)

    assert [c for _, c, _ in outcome.refused] == ["unreadable_output"]
    assert target.read_bytes() == before


def test_an_unreadable_output_is_not_pruned_and_keeps_its_claim(
    agents_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient read failure must not abandon our own output permanently."""
    source = _write(agents_dir, "a.md", "---\nname: a\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "a.json"
    source.unlink()

    monkeypatch.setattr(mas, "safe_read_file_bytes", lambda _p: None)
    outcome = mas.compile_markdown_agents(agents_dir)

    assert outcome.pruned == []
    assert target.is_file()
    # Still claimed, so a later pass with a readable file can finish the prune.
    assert "a" in mas._load_record()


def test_two_sources_claiming_one_name_refuse_the_second(agents_dir: Path) -> None:
    _write(agents_dir, "aaa.md", "---\nname: dup\ndescription: first\n---\nb\n")
    _write(agents_dir, "zzz.md", "---\nname: dup\ndescription: second\n---\nb\n")

    outcome = mas.compile_markdown_agents(agents_dir)

    assert outcome.written == ["dup"]
    assert [(s, c) for s, c, _ in outcome.refused] == [("zzz.md", "duplicate_name")]
    assert json.loads((agents_dir / "dup.json").read_text())["description"] == "first"


def test_one_bad_document_does_not_stop_the_others(agents_dir: Path) -> None:
    _write(agents_dir, "good.md", "---\nname: good\n---\nbody\n")
    _write(agents_dir, "bad.md", "no frontmatter at all\n")

    outcome = mas.compile_markdown_agents(agents_dir)

    assert outcome.written == ["good"]
    assert [(s, c) for s, c, _ in outcome.refused] == [("bad.md", "no_frontmatter")]


def test_applediouble_sidecars_are_not_sources(agents_dir: Path) -> None:
    _write(agents_dir, "._a.md", FULL_DOC)
    outcome = mas.compile_markdown_agents(agents_dir)
    assert outcome.written == []
    assert not (agents_dir / "code-reviewer.json").exists()


# ── crash boundaries (the rows in the module docstring) ───────────────────────


def test_row_4_an_interrupted_write_is_still_recognised_as_ours(agents_dir: Path) -> None:
    """Killed after the spec write, before the commit: the pass must recover.

    Without the target digest in the pending entry the file would match nothing,
    the compiler would read its own output as hand-written, and the source could
    never compile again without manual cleanup.
    """
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v1\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "a.json"

    # Simulate the interrupted transaction: the spec on disk is generation 2,
    # the record is still PENDING and names both generations.
    doc_v2 = "---\nname: a\ndescription: v2\n---\nbody\n"
    v2 = mas.derive_spec(doc_v2, fallback_name="a")
    previous = mas._load_record()["a"]["output_sha256"]
    target.write_bytes(v2.payload)
    mas._store_record(
        {
            "a": {
                "source": "a.md",
                "state": "pending",
                "previous_sha256": previous,
                "target_sha256": v2.digest,
            }
        }
    )
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v3\n---\nbody\n")

    outcome = mas.compile_markdown_agents(agents_dir)

    assert outcome.refused == []
    assert outcome.written == ["a"]
    assert json.loads(target.read_text())["description"] == "v3"
    assert mas._load_record()["a"]["state"] == "committed"


def test_row_3_a_pending_entry_over_the_previous_generation_is_ours(agents_dir: Path) -> None:
    """Killed after the pending write but before the spec write."""
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v1\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "a.json"
    on_disk = mas._load_record()["a"]["output_sha256"]

    mas._store_record(
        {
            "a": {
                "source": "a.md",
                "state": "pending",
                "previous_sha256": on_disk,
                "target_sha256": "0" * 64,
            }
        }
    )
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v2\n---\nbody\n")

    outcome = mas.compile_markdown_agents(agents_dir)

    assert outcome.refused == []
    assert json.loads(target.read_text())["description"] == "v2"


def test_row_7_an_unusable_record_claims_nothing(agents_dir: Path) -> None:
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v1\n---\nbody\n")
    mas.compile_markdown_agents(agents_dir)
    target = agents_dir / "a.json"
    before = target.read_bytes()

    record = mas._record_path()
    record.write_text("{ not json", encoding="utf-8")
    _write(agents_dir, "a.md", "---\nname: a\ndescription: v2\n---\nbody\n")

    outcome = mas.compile_markdown_agents(agents_dir)

    # Losing the record must degrade to leaving files alone, never to a delete
    # or an overwrite authorized by a record we could not read.
    assert [c for _, c, _ in outcome.refused] == ["name_taken"]
    assert target.read_bytes() == before


def test_an_unrecognised_record_schema_is_not_migrated(agents_dir: Path) -> None:
    mas._record_path().parent.mkdir(parents=True, exist_ok=True)
    mas._record_path().write_text(
        json.dumps({"schema": mas.RECORD_SCHEMA + 1, "entries": {"a": {"state": "committed"}}}),
        encoding="utf-8",
    )
    assert mas._load_record() == {}


def test_a_missing_agents_directory_retires_stale_claims(tmp_path: Path) -> None:
    gone = tmp_path / "not-there"
    mas._store_record({"a": {"source": "a.md", "state": "committed", "output_sha256": "x"}})

    outcome = mas.compile_markdown_agents(gone)

    assert outcome.refused == []
    assert mas._load_record() == {}


# ── boot ordering ─────────────────────────────────────────────────────────────


def test_compilation_is_wired_ahead_of_the_mcp_gateway() -> None:
    """The compile step MUST precede ``_init_mcp_gateway`` in the boot sequence.

    ``mcp_gateway/rewriter.rewrite_agents`` is what rewrites a spec's
    ``mcpServers`` into broker stubs, and it scans the agents directory once. A
    spec compiled AFTER it therefore keeps its raw server entries, so those
    servers spawn direct and bypass the tool gate for the life of the process.
    That makes the ordering a security property rather than a style choice, and a
    comment cannot enforce it -- an unrelated edit that moves either call would
    silently reopen the hole, so it is pinned here.
    """
    import inspect

    from kiro_crew.slack import gateway as gw

    source = inspect.getsource(gw)
    compiled_at = source.find("await self._compile_markdown_agents()")
    gateway_at = source.find("await self._init_mcp_gateway()")
    assert compiled_at != -1, "the boot sequence no longer compiles markdown agents"
    assert gateway_at != -1, "the MCP gateway boot call moved or was renamed"
    assert compiled_at < gateway_at
