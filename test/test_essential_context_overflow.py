"""An over-budget V2 essential envelope degrades instead of refusing the turn.

Contract (docs/request-for-change/rfc-essential-context-overflow-degrades.md):
the member's core -- identity, persona prompt, SOUL.md, context settings,
preferences.md, projects.md, the recall note -- is never left out; guides are
left out WHOLE from the tail of declaration order and named in one in-band
notice whose cost is reserved first; a core that alone does not fit still
refuses, naming ``memory.essential_max_chars``. The envelope size is that
setting capped by the model window.
"""

from __future__ import annotations

import json
import logging

import pytest
from test_member_essential_context import env as _member_env

from kiro_crew import folder_steering
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.section_builders import _build_memory_config
from kiro_crew.member_essential_context import (
    ESSENTIAL_MAX_CHARS,
    ESSENTIAL_OMISSION_SOURCE,
    MemberEssentialContextError,
    effective_essential_max_chars,
    fit_essential_documents,
    render_essentials,
)

env = _member_env

NOTICE_LABEL = f"[Essential source: {ESSENTIAL_OMISSION_SOURCE}]"
IDENTITY = "[MEMBER IDENTITY]\nname: writer\n[END MEMBER IDENTITY]\n"


def _set_limit(value: int) -> None:
    cfg = KiroCrewConfig.load()
    cfg.memory.essential_max_chars = value
    cfg.save()


def _write_template(env, resources: list[str]) -> None:
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    spec.write_text(
        json.dumps(
            {
                "name": "writer-template",
                "prompt": "Bound Soul: preserve the user's voice.",
                "resources": resources,
            }
        ),
        encoding="utf-8",
    )


def _essentials(env, **kwargs) -> str:
    return env.builder._build_v2_essentials(
        env.store, member=env.member, project=str(env.project), **kwargs
    )


# --- the fit itself ------------------------------------------------------------


def test_a_fitting_envelope_is_returned_unchanged_and_renders_byte_identical():
    documents = [("/core/prompt", "persona"), ("/guide/a.md", "A"), ("/guide/b.md", "B")]
    kept, left_out = fit_essential_documents(
        documents,
        identity=IDENTITY,
        max_chars=ESSENTIAL_MAX_CHARS,
        droppable={"/guide/a.md", "/guide/b.md"},
    )
    assert kept == documents and left_out == []
    rendered = render_essentials(kept, identity=IDENTITY)
    # The exact pre-change rendering, spelled out, so the fit cannot have
    # altered the fitting case's bytes.
    assert rendered == (
        "[V2 ESSENTIAL CONTEXT — current member identity and admitted project guides. "
        "This snapshot replaces ALL prior V2 essential snapshots, including guides "
        "absent from this source list. Do not keep applying removed sources. "
        "User permanent rules remain authoritative; project documents are task "
        "guidance, not permission to read another member's memory.]\n"
        + IDENTITY
        + "[Essential source: /core/prompt]\npersona\n"
        + "[Essential source: /guide/a.md]\nA\n"
        + "[Essential source: /guide/b.md]\nB\n"
        + "[END V2 ESSENTIAL CONTEXT]\n\n"
    )


def test_guides_drop_whole_from_the_tail_and_the_notice_names_exactly_those():
    cap = 20_000
    documents = [
        ("/guide/first.md", "f" * 6_000),
        ("/core/prompt", "persona"),
        ("/guide/second.md", "s" * 6_000),
        ("/guide/third.md", "t" * 9_000),
        ("/core/preferences.md", "prefs"),
    ]
    droppable = {"/guide/first.md", "/guide/second.md", "/guide/third.md"}
    kept, left_out = fit_essential_documents(
        documents, identity=IDENTITY, max_chars=cap, droppable=droppable
    )
    assert left_out == [("/guide/third.md", "t" * 9_000)]
    assert [s for s, _ in kept] == [
        "/guide/first.md",
        "/core/prompt",
        "/guide/second.md",
        "/core/preferences.md",
        ESSENTIAL_OMISSION_SOURCE,
    ]
    notice = kept[-1][1]
    assert "1 guide document(s)" in notice
    assert "/guide/third.md (9,000 characters)" in notice
    assert "/guide/first.md" not in notice and "/guide/second.md" not in notice
    assert "Do not assume their contents" in notice
    assert "memory.essential_max_chars" in notice
    assert len(render_essentials(kept, identity=IDENTITY, max_chars=cap)) <= cap


def test_a_left_out_guide_the_harness_carries_is_named_as_still_in_force():
    cap = 8_000
    documents = [
        ("/core/prompt", "persona"),
        ("/guide/native.md", "n" * 9_000),
        ("/guide/absent.md", "a" * 9_000),
    ]
    droppable = {"/guide/native.md", "/guide/absent.md"}
    kept, left_out = fit_essential_documents(
        documents,
        identity=IDENTITY,
        max_chars=cap,
        droppable=droppable,
        harness_loaded=frozenset({"/guide/native.md"}),
    )
    assert [s for s, _ in left_out] == ["/guide/native.md", "/guide/absent.md"]
    notice = kept[-1][1]
    absent, carried = notice.split("not repeated in this snapshot", 1)
    # The absent guide is "not loaded"; the carried one is never called that.
    assert "/guide/absent.md (9,000 characters)" in absent
    assert "/guide/native.md" not in absent
    assert "1 guide document(s) for this agent were not loaded" in absent
    assert "/guide/native.md (9,000 characters)" in carried
    assert "keep applying them" in carried and "Do not assume" not in carried


def test_only_harness_carried_guides_left_out_never_reads_as_incomplete():
    documents = [("/core/prompt", "persona"), ("/guide/native.md", "n" * 30_000)]
    kept, _ = fit_essential_documents(
        documents,
        identity=IDENTITY,
        max_chars=20_000,
        droppable={"/guide/native.md"},
        harness_loaded=frozenset({"/guide/native.md"}),
    )
    notice = kept[-1][1]
    assert notice.startswith("ESSENTIAL CONTEXT SHORTENED to 20000 characters.")
    assert "INCOMPLETE" not in notice and "not loaded" not in notice
    assert "keep applying them" in notice


def test_the_notice_cost_is_reserved_before_the_last_guide_is_admitted():
    """A guide that fits only WITHOUT the notice is left out too."""
    probe_cap = 100_000
    core = [("/core/prompt", "persona")]
    frame = len(render_essentials(core, identity=IDENTITY))
    guide_one = ("/guide/one.md", "o" * 3_000)
    guide_two = ("/guide/two.md", "w" * 3_000)
    one_cost = len(render_essentials([guide_one], identity="")) - len(
        render_essentials([], identity="")
    )
    # Exactly room for core + guide one, with nothing left for the notice that
    # dropping guide two requires.
    cap = frame + one_cost
    assert cap < probe_cap
    kept, left_out = fit_essential_documents(
        [*core, guide_one, guide_two],
        identity=IDENTITY,
        max_chars=cap,
        droppable={"/guide/one.md", "/guide/two.md"},
    )
    assert left_out == [guide_one, guide_two]
    assert [s for s, _ in kept] == ["/core/prompt", ESSENTIAL_OMISSION_SOURCE]
    assert "/guide/one.md (3,000 characters)" in kept[-1][1]
    assert len(render_essentials(kept, identity=IDENTITY, max_chars=cap)) <= cap


def test_a_minimal_notice_stands_in_when_the_full_list_does_not_fit():
    names = [f"/guide/{'n' * 120}-{i}.md" for i in range(20)]
    documents = [("/core/prompt", "c" * 15_000), *((name, "g" * 400) for name in names)]
    kept, left_out = fit_essential_documents(
        documents, identity=IDENTITY, max_chars=16_000, droppable=set(names)
    )
    assert [s for s, _ in kept] == ["/core/prompt", ESSENTIAL_OMISSION_SOURCE]
    assert len(left_out) == 20
    assert "20 guide document(s)" in kept[-1][1] and names[0] not in kept[-1][1]


def test_a_core_over_the_limit_still_refuses_and_names_the_setting():
    documents = [("/core/prompt", "c" * 70_000), ("/guide/a.md", "a" * 100)]
    with pytest.raises(MemberEssentialContextError) as raised:
        fit_essential_documents(
            documents,
            identity=IDENTITY,
            max_chars=ESSENTIAL_MAX_CHARS,
            droppable={"/guide/a.md"},
        )
    message = str(raised.value)
    assert "/core/prompt (70000 characters)" in message
    assert "/guide/a.md" not in message  # only what is still in the envelope
    assert "memory.essential_max_chars" in message


def test_a_drop_is_logged_once_with_every_source(caplog):
    documents = [("/core/prompt", "persona"), ("/guide/a.md", "a" * 70_000)]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.member_essential_context"):
        fit_essential_documents(
            documents,
            identity=IDENTITY,
            max_chars=ESSENTIAL_MAX_CHARS,
            droppable={"/guide/a.md"},
            owner="writer",
        )
    records = [r for r in caplog.records if "guide(s) not loaded" in r.getMessage()]
    assert len(records) == 1
    assert "writer" in records[0].getMessage()
    assert "/guide/a.md (70000 characters)" in records[0].getMessage()


# --- the limit ------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, loaded",
    [
        (None, 64_000),
        (1, 16_000),
        (128_000, 128_000),
        (10_000_000, 500_000),
        ("garbage", 64_000),
        (True, 64_000),
    ],
)
def test_the_configured_limit_is_clamped_on_load(raw, loaded):
    data = {} if raw is None else {"essential_max_chars": raw}
    assert _build_memory_config(data).essential_max_chars == loaded


@pytest.mark.parametrize(
    "configured, window, effective",
    [
        (200_000, None, 200_000),  # unknown window: the 1M reference, like budget.py
        (200_000, 0, 200_000),
        (900_000, None, 500_000),  # unknown window ceiling is the 1M one
        (32_000, None, 32_000),  # lowering always applies
        (200_000, 1_000_000, 200_000),  # 1M window ceiling is 500,000
        (900_000, 1_000_000, 500_000),  # clamped to the declared range first
        (200_000, 200_000, 100_000),  # 200K window: 200_000 * 4 / 8
        (200_000, 32_000, 99_000),  # small window: the three-budget floor
        (64_000, 32_000, 64_000),
    ],
)
def test_the_effective_limit_is_the_setting_capped_by_the_model_window(
    configured, window, effective
):
    assert effective_essential_max_chars(window, configured=configured) == effective


def test_the_effective_limit_reads_the_setting_by_default(env):
    _set_limit(20_000)
    assert effective_essential_max_chars() == 20_000
    assert effective_essential_max_chars(1_000_000) == 20_000


# --- a real member turn -----------------------------------------------------------


def test_the_readers_report_exactly_the_core_documents(env):
    from kiro_crew.member_essential_context import documents_for_member

    core: set[str] = set()
    documents = documents_for_member(
        "writer-template", str(env.project), context_settings=True, core_sources_out=core
    )
    spec = env.project / ".kiro" / "agents" / "writer-template.json"
    assert core == {
        str(env.project / "SOUL.md"),
        f"{spec}#prompt",
        f"{spec}#context-settings",
    }
    guides = {source for source, _ in documents} - core
    assert guides == {
        str(env.project / "AGENTS.md"),
        str(env.project / ".kiro" / "steering" / "always.md"),
        str(env.project / "declared-guide.md"),
    }


def test_member_turn_leaves_tail_guides_out_and_keeps_its_core(env):
    guides = env.project / "guides"
    guides.mkdir()
    (guides / "a-small.md").write_text("SMALL-GUIDE-KEPT", encoding="utf-8")
    (guides / "b-big.md").write_text("BIG-GUIDE-START" + "b" * 40_000, encoding="utf-8")
    (guides / "c-big.md").write_text("LAST-GUIDE-START" + "c" * 30_000, encoding="utf-8")
    _write_template(env, ["file://guides/*.md"])
    native: list[str] = []
    envelope = _essentials(env, native_envelope_out=native)
    assert "SMALL-GUIDE-KEPT" in envelope
    assert "BIG-GUIDE-START" in envelope
    assert "LAST-GUIDE-START" not in envelope
    assert f"{guides / 'c-big.md'} (30,016 characters)" in envelope
    assert f"{guides / 'b-big.md'} (" not in envelope.split(NOTICE_LABEL, 1)[1]
    for core in (
        "You are writer.",
        "Bound Soul: preserve the user's voice.",
        "Project Soul: write with empathy.",
        "Preference anchor: 请保留中文原文。",
        "Project anchor: the launch guide is authoritative.",
        "memory_recall",
    ):
        assert core in envelope
    assert len(envelope) <= ESSENTIAL_MAX_CHARS
    # The delivered native envelope carries the same notice inside the same bound.
    assert native and NOTICE_LABEL in native[0] and len(native[0]) <= ESSENTIAL_MAX_CHARS


def test_a_member_guide_kiro_cli_already_loaded_is_not_called_missing(env):
    guides = env.project / "guides"
    guides.mkdir()
    (guides / "b-big.md").write_text("BIG-GUIDE-START" + "b" * 40_000, encoding="utf-8")
    last = guides / "c-big.md"
    last.write_text("LAST-GUIDE-START" + "c" * 30_000, encoding="utf-8")
    _write_template(env, ["file://guides/*.md"])
    native: list[str] = []
    envelope = _essentials(
        env,
        native_documents={str(last): last.read_text(encoding="utf-8")},
        native_envelope_out=native,
    )
    notice = envelope.split(NOTICE_LABEL, 1)[1]
    assert "LAST-GUIDE-START" not in envelope
    assert f"{last} (30,016 characters)" in notice
    assert "not loaded" not in notice and "keep applying them" in notice
    assert native and "keep applying them" in native[0]


def test_raising_the_setting_admits_a_guide_that_was_left_out(env):
    big = env.project / "big-guide.md"
    big.write_text("BIG-GUIDE-START" + "b" * 110_000, encoding="utf-8")
    _write_template(env, ["file://big-guide.md"])
    assert "BIG-GUIDE-START" not in _essentials(env, model_window=1_000_000)
    _set_limit(200_000)
    admitted = _essentials(env, model_window=1_000_000)
    assert "BIG-GUIDE-START" in admitted and NOTICE_LABEL not in admitted
    # An unknown window (the default ``model="auto"`` deployment at a fresh
    # session) is the 1M reference window, so raising the setting works there too.
    assert "BIG-GUIDE-START" in _essentials(env)
    # A small window still caps it at the protected-context floor.
    assert "BIG-GUIDE-START" not in _essentials(env, model_window=32_000)


@pytest.mark.skipif(
    not folder_steering.pinned_fs.supports_pinned_tree_walk(),
    reason="folder steering refuses to walk by name on this platform",
)
def test_folder_steering_is_still_fitted_after_the_guides(env, tmp_path):
    (env.project / "big-guide.md").write_text("BIG-GUIDE-START" + "b" * 70_000, encoding="utf-8")
    _write_template(env, ["file://big-guide.md"])
    standards = tmp_path / "standards"
    standards.mkdir()
    (standards / "coding.md").write_text("FOLDER-RULE-KEPT", encoding="utf-8")
    envelope = _essentials(env, steering_dirs=(str(standards),))
    assert "BIG-GUIDE-START" not in envelope
    assert NOTICE_LABEL in envelope
    assert "FOLDER-RULE-KEPT" in envelope
    # Folder steering sits before the profile anchors, as it always has.
    assert envelope.index("FOLDER-RULE-KEPT") < envelope.index("Preference anchor")


def test_a_core_that_alone_overflows_still_refuses_the_turn(env):
    env.memory.write_preferences("p" * 70_000)
    with pytest.raises(MemberEssentialContextError, match="memory.essential_max_chars") as raised:
        _essentials(env)
    assert "preferences.md" in str(raised.value)


def test_a_refused_source_still_refuses_even_when_the_envelope_is_over_budget(env, tmp_path):
    (tmp_path / "outside.md").write_text("OTHER_PROJECT_SECRET", encoding="utf-8")
    (env.project / "AGENTS.md").write_text("x" * 70_000, encoding="utf-8")
    _write_template(env, ["file://../outside.md"])
    with pytest.raises(MemberEssentialContextError, match="outside.md"):
        _essentials(env)


def test_profile_validation_uses_the_effective_limit(env):
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.memory import _validate_private_profile_update

    _set_limit(20_000)
    with pytest.raises(MemberEssentialContextError, match="20000-character"):
        _validate_private_profile_update(
            SimpleNamespace(context_builder=env.builder), env.store, "projects.md", "q" * 25_000
        )


def test_profile_validation_uses_the_smallest_envelope_any_session_renders(env):
    """A raised setting does not admit a profile a small-window session refuses."""
    from types import SimpleNamespace

    from kiro_crew.config.sections import WorkspaceConfig
    from kiro_crew.dashboard.handlers.memory import _validate_private_profile_update
    from kiro_crew.member_essential_context import smallest_essential_max_chars

    _set_limit(200_000)
    cfg = KiroCrewConfig.load()
    cfg.workspaces["writer-project"] = WorkspaceConfig(dir=str(env.project))
    cfg.agents["writer"].workspace = "writer-project"
    cfg.save()
    assert smallest_essential_max_chars() == 99_000
    state = SimpleNamespace(context_builder=env.builder)
    with pytest.raises(MemberEssentialContextError, match="99000-character"):
        _validate_private_profile_update(state, env.store, "projects.md", "q" * 100_000)
    # Under the length precheck, but the core around it does not fit 99,000:
    # the envelope build itself must render against the smallest size too.
    with pytest.raises(MemberEssentialContextError, match="exceeds 99000 characters"):
        _validate_private_profile_update(state, env.store, "projects.md", "q" * 98_900)


def _help_copies() -> dict[str, str]:
    """Every shipped copy of the setting's help text, cut down to that key."""
    import dataclasses
    from pathlib import Path

    from kiro_crew.config.memory_sections import MemoryConfig

    root = Path(__file__).resolve().parent.parent
    field = next(f for f in dataclasses.fields(MemoryConfig) if f.name == "essential_max_chars")
    baseline = json.loads((root / "config-baseline.json").read_text(encoding="utf-8"))
    entries = baseline["entries"] if isinstance(baseline, dict) and "entries" in baseline else None
    if entries is None:
        entries = next(v for v in baseline.values() if isinstance(v, list))
    baseline_help = next(
        e["help"] for e in entries if e.get("path") == "memory.essential_max_chars"
    )
    doc = (root / "src/kiro_crew/docs/configuration.md").read_text(encoding="utf-8")
    doc_row = next(line for line in doc.splitlines() if "`memory.essential_max_chars`" in line)
    spec = (root / "docs/system-specs/modules/memory-skills-hooks.md").read_text(encoding="utf-8")
    para = next(
        p for p in spec.split("\n\n") if "`memory.essential_max_chars` characters (default" in p
    )
    return {
        "schema": str(field.metadata.get("help", "")),
        "baseline": baseline_help,
        "configuration.md": doc_row,
        "memory-skills-hooks.md": para,
    }


@pytest.mark.parametrize(
    "name", ["schema", "baseline", "configuration.md", "memory-skills-hooks.md"]
)
def test_every_help_copy_states_the_caps_the_code_measures(name):
    text = " ".join(_help_copies()[name].split())
    # The floor every known window keeps, measured through production code.
    assert effective_essential_max_chars(32_000, configured=500_000) == 99_000
    assert "99,000" in text or "99_000" in text
    # An unknown window is capped as the 1M window, measured the same way.
    assert effective_essential_max_chars(None, configured=500_000) == 500_000
    assert effective_essential_max_chars(1_000_000, configured=500_000) == 500_000
    assert "500,000" in text
    # The falsified spellings that shipped before are gone.
    assert "stays at 64,000" not in text
    assert "renders against at most 64,000" not in text
