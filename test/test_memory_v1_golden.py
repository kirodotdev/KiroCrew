"""Golden characterization of the v1 default memory path.

This module is the regression net for "the default agent's assembled first turn is
unchanged". Everything else about memory is covered by behavioural tests that each
assert one property; nothing else pins the WHOLE payload — the set of injected
blocks, their order, their byte extents, the recall order inside each memory block,
and the embed accounting behind them. Without that, "unchanged" is an assertion
rather than a fact, because a block can be added, reordered, or silently doubled in
size without any single-property test noticing.

The contract this establishes is stated in
``docs/system-specs/common/testing-conventions.md`` § Golden payload tests: after a
change to the memory path, this file is re-run UNEDITED. Needing to edit it is the
definition of a regression, and the edit is the thing a reviewer reads.

Determinism, and what is deliberately stubbed:

* The seed is a fully controlled data home under ``tmp_path`` — its own workspace,
  its own ``memory.db``, its own two-skill catalog, its own agent prompt. The
  bundled prompt file and the shipped skill catalog are NOT used, because editing
  either is a legitimate non-memory change and pinning their bytes here would force
  an edit to this file for a reason it does not cover. Their presence on the real
  default path is asserted separately (``test_production_agent_prompt_exists``).
* Absolute paths leak into the payload (workspace identity, the docs pointer, each
  memory block's ``_[source: ...]_`` line), and their LENGTH varies with the
  checkout and tmp dir. Extents are therefore measured over a path-normalized
  payload; the normalization is exact-string substitution, so it cannot hide a
  content change.
* Wall-clock: the ``[CURRENT DATE]`` line is frozen. Episodic decay scores against
  ``datetime.now``, so episodic rows are pinned by AGE (a fixed number of days
  before the row is written), never by an absolute timestamp — an absolute
  ``created_at`` would make ``days_old`` grow every day and drift the score.
* The host: ``agent.resource_pressure_gb = 0`` is the production off switch for the
  ``[RESOURCES]`` advisory, so no host memory reading can reach the payload.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew import context as ctx
from kiro_crew import embeddings, resource_status, session_surface, vector_memory
from kiro_crew.config import paths as config_paths
from kiro_crew.config.loader import KiroCrewConfig, workspace_dir_for
from kiro_crew.config.paths import config_dir
from kiro_crew.embeddings import PRIORITY_NORMAL
from kiro_crew.hooks import HookManager
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.vector_memory import VectorMemoryStore

# ``context._memory_stores`` / ``context._lesson_stores`` are module globals behind
# ``context._stores_lock``. Two workers racing them is a flake, so the whole module
# runs in one xdist group.
pytestmark = pytest.mark.xdist_group("memory_v1_golden")


# ── The deterministic embedder ───────────────────────────────────────────────
#
# One axis per topic word, so a query's similarity to a row is a number this file
# can derive by hand rather than a property of a 610MB model. Substring matching
# (not tokenizing) keeps it independent of the stemmer.
_DIM = 8
_AXES = {"deploy": 0, "tabs": 1, "review": 2, "coffee": 3}

#: The query every recall assertion ranks against. Hits axes 0, 1 and 2 once each.
QUERY = "deploy review tabs"


def _topic_vector(text: str) -> list[float]:
    vec = [0.0] * _DIM
    low = text.lower()
    for word, axis in _AXES.items():
        if word in low:
            vec[axis] += 1.0
    if not any(vec):
        # A row matching no topic must still get a comparable unit vector, on an
        # axis the query never occupies, so it scores a true 0.0 rather than being
        # treated as "no vector stored" (which scores on the keyword floor).
        vec[_DIM - 1] = 1.0
    return vec


class _TopicBackend(embeddings.EmbeddingBackend):
    """An ``EmbeddingBackend`` over ``_topic_vector`` that counts real inferences.

    Counting HERE — below ``make_sync_embed_fn``'s ``lru_cache`` — is what makes the
    cache-collapse assertion honest: the layer above counts calls, this layer counts
    the inferences those calls actually cost.
    """

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    @property
    def model_id(self) -> str:
        return "kirocrew-test-topic-axes-v1"

    @property
    def dim(self) -> int:
        return _DIM

    def is_ready(self) -> bool:
        return True

    def embed(self, text: str, *, priority: int = PRIORITY_NORMAL) -> list[float]:
        self._calls.append(text)
        return _topic_vector(text)

    def embed_batch(
        self, texts: list[str], *, priority: int = PRIORITY_NORMAL
    ) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def close(self) -> None:
        """No resource to release. Nothing here to spy on or delegate to."""


# ── The seed ─────────────────────────────────────────────────────────────────

_PREFERENCES = "# Preferences\n\n- Prefers tabs over spaces\n"
_PROJECTS = "# Projects\n\n- kirocrew: the crew itself\n"
_HISTORY_DAY = "#### 10:00 deploy chat\nAgreed to review the deploy checklist.\n"

#: Semantic rows, written in this order. ``SEM_TOP`` outranks ``SEM_MID`` on both
#: terms of the hybrid score (its key AND value overlap the query, and its stored
#: vector shares two of the query's three axes); ``SEM_OFF`` shares nothing, scores
#: 0, and is dropped by ``get_semantic_context``'s ``score > 0`` gate. Written
#: most-relevant FIRST so the ranked order is the REVERSE of the recency order the
#: no-query branch would emit — a test that cannot tell ranking from insertion order
#: proves nothing.
SEM_TOP = ("pref.deploy_style", "review before deploy")
SEM_MID = ("pref.indent_style", "tabs not spaces")
SEM_OFF = ("user.drink", "coffee")

#: Episodic rows: (text, age in days at scoring time). ``EP_TOP`` matches all three
#: query axes (cosine 1.0), ``EP_MID`` two of three (0.816), ``EP_OFF`` none (0.0,
#: below ``_EPISODIC_RELEVANCE_THRESHOLD``, so the relevance gate drops it).
#: ``EP_TOP`` is the OLDER of the two admitted rows, so ranking it first cannot be
#: explained by recency.
EP_TOP = ("We deploy on Fridays and review the tabs setting first.", 2)
EP_MID = ("The deploy runbook says to widen tabs to four spaces.", 1)
EP_OFF = ("Coffee tastes better at a one to sixteen brew ratio.", 3)

#: Lessons, written in this order. Ranked order is the exact REVERSE of the
#: newest-first order ``get_lessons()`` returns, so insertion order cannot pass this.
#: ``LESSON_OFF`` scores 0 and is still rendered — lessons are ranked, never
#: relevance-gated, and that asymmetry with episodic recall is part of the contract.
LESSON_TOP = "Run the deploy smoke test and review the diff before merging."
LESSON_MID = "Use tabs for indentation in this repository."
LESSON_OFF = "Brew coffee at a one to sixteen ratio."

_FROZEN_NOW = datetime(2026, 3, 4, 5, 6, tzinfo=timezone.utc)


@dataclass
class Seeded:
    """Everything a golden assertion needs about one seeded home."""

    home: Path
    workspace: Path
    builder: ctx.ContextBuilder
    memory: MemoryStore
    vectors: VectorMemoryStore
    docs_dir: Path
    #: The daily-history file the seed wrote. Carried rather than recomputed: its name
    #: is today's date, and ``read_recent_history`` globs by date, so a second
    #: ``datetime.now()`` in a test would widen the midnight-rollover window.
    history_file: Path
    #: Text handed to ``embed_fn``, in call order (the boundary vector_memory calls).
    embed_fn_calls: list[str]
    #: Text that reached the backend, i.e. one entry per REAL inference.
    inference_calls: list[str]

    def normalize(self, payload: str) -> str:
        """Replace machine-specific absolute paths with stable placeholders.

        Byte extents are meaningless otherwise: the workspace path, the skills
        path and the bundled-docs path all appear verbatim in the payload and all
        vary in length with the checkout and the tmp dir. Exact-string
        substitution only — nothing content-bearing can hide behind it.
        """
        out = payload.replace(str(self.docs_dir), "<DOCS>")
        for root in {str(self.home), str(self.home.resolve())}:
            out = out.replace(root, "<HOME>")
        return out


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Seeded:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIROCREW_SKIP_MODEL_DOWNLOAD", "1")
    # ``config_dir()`` memoises the resolved home in this module global for the
    # process lifetime, and one xdist worker runs thousands of tests in one process.
    monkeypatch.setattr(config_paths, "_resolved_home", None, raising=False)

    # ``resource_pressure_gb: 0`` is the documented off switch for the [RESOURCES]
    # advisory. Without it the payload depends on how much free memory the host
    # happens to have, which is not a property of the memory path.
    (home / "config.json").write_text(
        json.dumps({"agent": {"resource_pressure_gb": 0, "resource_critical_gb": 0}}),
        encoding="utf-8",
    )
    assert KiroCrewConfig.load().agent.resource_pressure_gb == 0
    assert resource_status.probe().context_line() == ""

    # Module globals: reset through monkeypatch so a build in this test cannot serve
    # another test's store, and vice versa.
    monkeypatch.setattr(ctx, "_memory_stores", {}, raising=True)
    monkeypatch.setattr(ctx, "_lesson_stores", {}, raising=True)
    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset(), raising=True)
    monkeypatch.setattr(ctx, "_INCLUDE_CREW_CONTEXT_CACHE", {}, raising=True)

    # Freeze the clock the [CURRENT DATE] line reads. ``context`` holds exactly two
    # ``datetime.now(tz)`` call sites and no other use of the name.
    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return _FROZEN_NOW if tz is None else _FROZEN_NOW.astimezone(tz)

    monkeypatch.setattr(ctx, "datetime", _FrozenDatetime, raising=True)
    monkeypatch.setattr(ctx, "get_local_tz", lambda: ("UTC", timezone.utc), raising=True)

    # The embedder: a backend (counted below the cache) reached through the production
    # ``make_sync_embed_fn`` (counted above it). The factory slot and the shared-instance
    # slot are set through ``monkeypatch`` rather than
    # ``register_embedding_backend`` + ``reset_shared_embedder``, because the public pair
    # would have to be undone by a second ``reset_shared_embedder()`` in teardown — and
    # that RETIRES whatever instance the slot holds, which on a worker that already built
    # a real embedder means unloading another test's model. monkeypatch restores both
    # globals to the values they had, and this backend owns nothing to close.
    inference_calls: list[str] = []
    monkeypatch.setattr(
        embeddings, "_backend_factory", lambda: _TopicBackend(inference_calls), raising=False
    )
    monkeypatch.setattr(embeddings, "_shared_embedder", None, raising=False)
    sync_embed = embeddings.make_sync_embed_fn()

    embed_fn_calls: list[str] = []

    def embed_fn(text: str, *, priority: int = PRIORITY_NORMAL) -> list[float] | None:
        embed_fn_calls.append(text)
        return sync_embed(text, priority=priority)

    embed_fn.accepts_priority = True  # type: ignore[attr-defined]

    workspace = workspace_dir_for("default")
    memory = MemoryStore(workspace=workspace)
    memory.init()
    memory_dir = workspace / "memory"
    (memory_dir / "preferences.md").write_text(_PREFERENCES, encoding="utf-8")
    (memory_dir / "projects.md").write_text(_PROJECTS, encoding="utf-8")
    history_dir = memory_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    # ``read_recent_history`` globs by date, so today's name is the only one the
    # full-content tier reads. ``datetime.now()`` in ``memory`` is not frozen —
    # the FILE NAME must match whatever today is on the running host.
    today = datetime.now().date().strftime("%Y-%m-%d")
    history_file = history_dir / f"{today}.md"
    history_file.write_text(_HISTORY_DAY, encoding="utf-8")

    vectors = VectorMemoryStore(db_path=config_dir() / "memory.db", embedding_dim=_DIM)
    vectors.init()
    vectors.embed_fn = embed_fn
    for key, value in (SEM_TOP, SEM_MID, SEM_OFF):
        assert vectors.set_semantic(key, value, 1.0, "user_explicit") is None, key
    for text, _age in (EP_TOP, EP_MID, EP_OFF):
        assert vectors.write_episodic(text, importance=0.5, source="consolidation"), text
    for rule in (LESSON_TOP, LESSON_MID, LESSON_OFF):
        assert vectors.write_lesson(rule).outcome.value == "inserted", rule

    # Pin each episodic row's AGE, not an absolute timestamp: the decay term is
    # ``exp(-rate * (now - created_at).days)`` against the real clock, so an
    # absolute date would change the score every day the suite runs.
    ages = {text: age for text, age in (EP_TOP, EP_MID, EP_OFF)}
    now = datetime.now(timezone.utc)
    for row in vectors.get_episodic_list(limit=50):
        vectors.db.execute(
            "UPDATE episodic_memories SET created_at = ? WHERE id = ?",
            ((now - timedelta(days=ages[row["text"]])).isoformat(), row["id"]),
        )
    vectors.db.commit()
    memory.vector_store = vectors

    skills_root = home / "skills"
    for name, description in (("alpha", "Alpha skill"), ("beta", "Beta skill")):
        (skills_root / name).mkdir(parents=True)
        (skills_root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\ntriggers: zzz-{name}\n---\n\nBody.\n",
            encoding="utf-8",
        )

    prompt_file = home / "agent-prompt.md"
    prompt_file.write_text("You are {bot_name}. Be terse.\n", encoding="utf-8")
    monkeypatch.setattr(ctx, "_prompt_path", lambda mode="": prompt_file, raising=True)

    builder = ctx.ContextBuilder(
        memory=memory,
        skills=SkillsLoader(skills_path=skills_root, install_builtins=False),
        hooks=HookManager(),
        lessons=LessonStore(base_dir=workspace),
        conversation_log=None,
        channel_history=None,
    )
    # ContextBuilder.__init__ registers its store under "default"; a build with no
    # workspace/memory_store resolves back to exactly this object.
    assert ctx._memory_stores["default"] is memory

    embed_fn_calls.clear()
    inference_calls.clear()
    return Seeded(
        home=home,
        workspace=workspace,
        builder=builder,
        memory=memory,
        vectors=vectors,
        docs_dir=ctx._BUNDLED_DOCS_DIR,
        history_file=history_file,
        embed_fn_calls=embed_fn_calls,
        inference_calls=inference_calls,
    )


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Seeded:
    return _seed(tmp_path, monkeypatch)


#: The session key every build uses. Not a ``dashboard:`` key, so the payload takes
#: the channel critical-rules variant and no dashboard-tool nudges.
SESSION_KEY = "slack:C0GOLDEN:1700000000.000100"


def _session_context(seeded: Seeded, **kwargs: object) -> str:
    return seeded.builder.build_session_context(
        session_key=SESSION_KEY, query_text=QUERY, **kwargs  # type: ignore[arg-type]
    )


# ── 1. The set and the order of injected blocks ──────────────────────────────

#: Every marker the assembled first turn carries, in the order it carries it. Each
#: entry is the literal opening line of one block. This is the whole list — the
#: absence test below refuses any other known marker.
EXPECTED_BLOCK_ORDER = [
    "[AGENT SYSTEM PROMPT]",
    "[SESSION CONTEXT -- background reference only, NOT a task to act on.",
    "[CRITICAL RULES -- always follow these]",
    "[CURRENT DATE] ",
    "[CURRENT AGENT] kirocrew",
    "[WORKSPACE IDENTITY]",
    "[DOCUMENTATION]",
    "[Memory -- persistent user profile and recent activity log.",
    "## User Preferences",
    "## Active Projects",
    "## Recent History",
    "[Semantic Memory -- factual key-value pairs.",
    "[Episodic Memory -- relevant past conversation fragments.]",
    "[End of memory]",
    "[Skills:]",
    "[Learned corrections -- user-taught rules from past mistakes.",
    "[END OF SESSION CONTEXT]",
    "[CURRENT USER REQUEST -- respond to this]",
]

#: Blocks a default first turn must NOT carry, each with the reason. A change that
#: starts injecting one of these is a change to the default path even when every
#: block above is still byte-identical.
EXPECTED_ABSENT = {
    "[CONTEXT SCOPE]": "nothing was withheld (context_groups=None)",
    "[USER PROFILE]": "onboarding questions unanswered",
    "[UI LANGUAGE]": "dashboard.language is the follow-the-browser sentinel",
    "[THREAD CONVERSATION HISTORY": "no conversation log on a first turn",
    "## Recent Session Context": "provenance needs a conversation log",
    "[CONVERSATION HISTORY": "no compressed replay was supplied",
    "[REINJECTED AFTER COMPACTION": "first turn, not a post-compaction turn",
    "[SLACK THREAD CONTEXT": "no thread_ts",
    "[PROJECT] ": "no active project",
    "[FOLDER] ": "no folder path",
    "[RESOURCES]": "the pressure advisory is switched off in config",
    "[Hook context:]": "no hooks configured",
    "[Skill: ": "no skill trigger matches the query",
    "[CRITICAL ERROR - LESSONS FILE TOO LARGE]": "lessons fit their cap",
}
# The steering block carries no marker of its own — it is raw file content — so it
# cannot be listed above. It is covered by ``test_session_context_char_extents``
# instead: steering lands between the docs pointer and the memory block, so any
# content there inflates the ``[DOCUMENTATION]`` extent and the total.


def test_first_turn_block_set_and_order(seeded: Seeded) -> None:
    """The assembled first turn carries exactly these blocks, in exactly this order."""
    message, hook = seeded.builder.build_message(QUERY, True, session_key=SESSION_KEY)
    assert hook.action == "passthrough"

    found = [
        (message.index(marker), marker) for marker in EXPECTED_BLOCK_ORDER if marker in message
    ]
    missing = [m for m in EXPECTED_BLOCK_ORDER if m not in message]
    assert not missing, f"expected blocks absent from the default first turn: {missing}"
    assert [m for _, m in sorted(found)] == EXPECTED_BLOCK_ORDER

    # Every block appears exactly once. A doubled memory block is the specific
    # regression a set-only assertion cannot see.
    for marker in EXPECTED_BLOCK_ORDER:
        assert message.count(marker) == 1, f"{marker!r} appears {message.count(marker)} times"

    for marker, why in EXPECTED_ABSENT.items():
        assert marker not in message, f"{marker!r} must not be injected: {why}"


def test_session_context_block_order_matches_the_wrapped_turn(seeded: Seeded) -> None:
    """``build_session_context`` emits the same blocks, minus ``build_message``'s wrapper."""
    payload = _session_context(seeded)
    wrapper = {
        "[AGENT SYSTEM PROMPT]",
        "[SESSION CONTEXT -- background reference only, NOT a task to act on.",
        "[END OF SESSION CONTEXT]",
        "[CURRENT USER REQUEST -- respond to this]",
    }
    # ``build_message`` folds em dashes to "--" via _MULTIBYTE_TABLE; the raw
    # session context has not been through that fold yet.
    inner = [m.replace("--", "—") for m in EXPECTED_BLOCK_ORDER if m not in wrapper]
    assert [m for m in inner if m in payload] == inner
    assert sorted((payload.index(m), m) for m in inner) == [(payload.index(m), m) for m in inner]


def test_production_agent_prompt_exists() -> None:
    """The stubbed prompt stands in for a real file on the default path.

    Pinning the shipped prompt's bytes here would force an edit to this file every
    time the prompt is reworded — a change this golden does not cover. Asserting the
    file exists keeps the stub honest.
    """
    from kiro_crew.agent import _prompt_path

    assert _prompt_path().is_file()
    assert _prompt_path().read_text(encoding="utf-8").strip()


# ── 2. Character extents, derived from the production caps ───────────────────


def _blocks(payload: str) -> dict[str, str]:
    """Split a normalized session context into ``{marker: block text}``.

    Each block runs from its own opening marker to the next one, so the extents
    below sum to the payload length with nothing unaccounted for — which is also how
    a block carrying no marker of its own (steering) is covered: its content would
    land inside the preceding block's extent.
    """
    markers = [
        "[CRITICAL RULES",
        "[CURRENT DATE]",
        "[CURRENT AGENT]",
        "[WORKSPACE IDENTITY]",
        "[DOCUMENTATION]",
        "[Memory —",
        "[Skills:]",
        "[Learned corrections",
    ]
    missing = [m for m in markers if m not in payload]
    # Named rather than left to ``str.index``'s bare "substring not found": a header
    # edit is the most likely reason to land here, and the marker is the diagnosis.
    assert not missing, f"block header(s) absent from the session context: {missing}"
    starts = sorted((payload.index(marker), marker) for marker in markers)
    out: dict[str, str] = {}
    for i, (idx, marker) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(payload)
        out[marker] = payload[idx:end]
    return out


#: Byte extent of every block in the normalized default session context, and the
#: total. Regenerate ONLY as part of a reviewed change to what a block contains.
EXPECTED_EXTENTS = {
    "[CRITICAL RULES": 2811,
    "[CURRENT DATE]": 48,
    "[CURRENT AGENT]": 196,
    "[WORKSPACE IDENTITY]": 376,
    "[DOCUMENTATION]": 235,
    "[Memory —": 1029,
    "[Skills:]": 353,
    "[Learned corrections": 296,
}
EXPECTED_TOTAL = 5344


def test_session_context_char_extents(seeded: Seeded) -> None:
    """Every block's char extent, and the total, are what they were."""
    payload = seeded.normalize(_session_context(seeded))
    extents = {marker: len(text) for marker, text in _blocks(payload).items()}
    assert extents == EXPECTED_EXTENTS
    assert sum(extents.values()) == len(payload) == EXPECTED_TOTAL


def test_total_stays_under_the_production_ceiling(seeded: Seeded) -> None:
    """The default build's global ceiling is the un-scaled budget base.

    ``skills.lazy_load`` is off by default, so ``max_context_chars`` is
    ``caps.base``, not the Σ-of-sections ``caps.max_context``. Both are read from
    ``_resolve_caps`` rather than restated.
    """
    caps = ctx._resolve_caps(None)
    assert KiroCrewConfig.load().skills.lazy_load is False
    payload = _session_context(seeded)
    assert len(payload) <= caps.base < caps.max_context


def test_memory_sub_block_bodies_fit_their_own_caps(seeded: Seeded) -> None:
    """Each memory sub-block's body is inside the cap ``build_session_context`` passed."""
    caps = ctx._resolve_caps(None)
    payload = _session_context(seeded)
    memory_block = _blocks(seeded.normalize(payload))["[Memory —"]

    assert len(_PREFERENCES) <= caps.prefs
    assert len(_PROJECTS) <= caps.projects
    assert len(_HISTORY_DAY) <= caps.memory_history
    assert "…[truncated]" not in memory_block

    semantic = memory_block[memory_block.index("[Semantic Memory") :]
    semantic = semantic[: semantic.index("[End of semantic memory]")]
    assert len(semantic) <= caps.semantic
    episodic = memory_block[memory_block.index("[Episodic Memory") :]
    episodic = episodic[: episodic.index("[End of episodic memory]")]
    assert len(episodic) <= min(ctx._EPISODIC_INJECT_CAP, caps.episodic)


@pytest.mark.parametrize("section", ["prefs", "projects", "memory_history"])
def test_oversized_memory_file_truncates_exactly_at_its_cap(seeded: Seeded, section: str) -> None:
    """An overflowing file is cut to its own cap, and the cap is the production one.

    This is what ties the extents above to ``_resolve_caps`` rather than to the size
    of the seed: a cap that changed would move this boundary even though the golden
    seed still fits comfortably.
    """
    caps = ctx._resolve_caps(None)
    cap = getattr(caps, section)
    target = {
        "prefs": seeded.workspace / "memory" / "preferences.md",
        "projects": seeded.workspace / "memory" / "projects.md",
        "memory_history": seeded.history_file,
    }[section]
    filler = "- x" + "y" * 60 + "\n"
    target.write_text(filler * (cap // len(filler) + 20), encoding="utf-8")
    seeded.memory._invalidate_history_cache()

    payload = _session_context(seeded)
    marker = {
        "prefs": "## User Preferences",
        "projects": "## Active Projects",
        "memory_history": "## Recent History",
    }[section]
    body = payload[payload.index(marker) :]
    # Body = header line + source line + capped content + the truncation marker.
    head, _, rest = body.partition("\n")
    source, _, content = rest.partition("\n")
    content = content[: content.index("\n…[truncated]") + len("\n…[truncated]")]
    assert head == marker
    assert source.startswith("_[source: ")
    assert len(content) == cap + len("\n…[truncated]")


# ── 3. Recall order inside each memory block ─────────────────────────────────


def test_semantic_recall_order(seeded: Seeded) -> None:
    """Hybrid ranking, most relevant first; a zero-scoring row is dropped."""
    payload = _session_context(seeded)
    block = payload[payload.index("[Semantic Memory") : payload.index("[End of semantic memory]")]
    rows = [line for line in block.splitlines() if line.startswith(("pref.", "user."))]
    assert rows == [
        f"{SEM_TOP[0]}: {SEM_TOP[1]}",
        f"{SEM_MID[0]}: {SEM_MID[1]}",
    ]
    assert SEM_OFF[0] not in payload, "a row scoring 0 must not be injected"


def test_episodic_recall_order(seeded: Seeded) -> None:
    """Relevance gate then decay ranking; the older, more relevant row leads."""
    payload = _session_context(seeded)
    block = payload[payload.index("[Episodic Memory") : payload.index("[End of episodic memory]")]
    rows = [line for line in block.splitlines() if re.match(r"^\d+\. ", line)]
    assert rows == [f"1. {EP_TOP[0]}", f"2. {EP_MID[0]}"]
    assert EP_OFF[0] not in payload, "a row below the relevance gate must not be injected"


def test_lesson_recall_order(seeded: Seeded) -> None:
    """Lessons are ranked, never relevance-gated: the 0-scoring rule still ships."""
    payload = _session_context(seeded)
    block = payload[
        payload.index("[Learned corrections") : payload.index("[End of learned corrections]")
    ]
    rows = [line for line in block.splitlines() if line.startswith("- ")]
    assert rows == [f"- {LESSON_TOP}", f"- {LESSON_MID}", f"- {LESSON_OFF}"]
    # ``get_lessons()`` hands the ranker its rows newest-first. Reading the store's
    # own order is what makes the assertion above about RANKING rather than about
    # iteration: the rendered order is the exact reverse of what the ranker was given.
    store_order = [
        vector_memory._lesson_display_text(json.loads(row["value_json"]))
        for row in seeded.vectors.get_lessons()
    ]
    assert store_order == [LESSON_OFF, LESSON_MID, LESSON_TOP]


def test_recall_order_is_stable_across_repeated_builds(seeded: Seeded) -> None:
    """Two builds of the same seed produce byte-identical memory blocks."""
    first = seeded.normalize(_session_context(seeded))
    second = seeded.normalize(_session_context(seeded))
    assert _blocks(first)["[Memory —"] == _blocks(second)["[Memory —"]
    assert _blocks(first)["[Learned corrections"] == _blocks(second)["[Learned corrections"]


# ── 4. Memory is injected on the first message only ──────────────────────────

#: Markers whose presence means memory was injected.
_MEMORY_MARKERS = (
    "[Memory --",
    "## User Preferences",
    "## Active Projects",
    "## Recent History",
    "[Semantic Memory --",
    "[Episodic Memory --",
    "[Learned corrections --",
    "[Skills:]",
    "[CRITICAL RULES --",
)


def test_memory_is_injected_on_the_first_message_only(seeded: Seeded) -> None:
    first, _ = seeded.builder.build_message(QUERY, True, session_key=SESSION_KEY)
    for marker in _MEMORY_MARKERS:
        assert marker in first, marker

    follow_up, _ = seeded.builder.build_message(
        "and what about the rollback?", False, session_key=SESSION_KEY
    )
    for marker in _MEMORY_MARKERS:
        assert marker not in follow_up, f"{marker!r} re-injected on a follow-up turn"
    for row in (SEM_TOP[1], EP_TOP[0], LESSON_TOP, "Prefers tabs over spaces"):
        assert row not in follow_up

    # Main's warm-session request boundary keeps the current user text last;
    # the reply-format reminder does not trigger another memory injection.
    assert follow_up.startswith("[REPLY FORMAT RULES]\n")
    assert "(If presenting choices," in follow_up
    assert follow_up.endswith(
        "[CURRENT USER REQUEST -- respond to this]\nand what about the rollback?"
    )


def test_follow_up_turn_costs_no_embedding(seeded: Seeded) -> None:
    """A follow-up reaches no retrieval path, so it must not embed anything."""
    seeded.builder.build_message("and what about the rollback?", False, session_key=SESSION_KEY)
    assert seeded.embed_fn_calls == []
    assert seeded.inference_calls == []


# ── 5. The files a build touches under KIROCREW_HOME ─────────────────────────


def _home_files(home: Path) -> set[str]:
    return {p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()}


def test_build_writes_no_new_file_under_the_data_home(seeded: Seeded) -> None:
    """A build reads memory; it must not grow the data home a new file.

    A later change that starts writing a sidecar (an index, a cache, a per-store
    manifest) is invisible in the payload and shows up only here.
    """
    before = _home_files(seeded.home)
    seeded.builder.build_message(QUERY, True, session_key=SESSION_KEY)
    after = _home_files(seeded.home)
    assert after - before == set(), f"build created {sorted(after - before)}"
    assert before - after == set(), f"build removed {sorted(before - after)}"


def test_seeded_home_file_set_is_pinned(seeded: Seeded) -> None:
    """The seed's own footprint, so an added file is attributed to the seed or the build.

    ``config.json.bak`` is here because ``KiroCrewConfig.load()`` normalizes and
    writes the file back, keeping a backup — a side effect of the seed, not of the
    build, which is exactly the distinction this pin exists to keep visible.

    ``config.json.lock`` is the same shape: ``config.loader``'s locked write-back
    takes an advisory lock beside the file it writes. It is named here rather than
    filtered out because the whole value of this pin is that a NEW file has to be
    accounted for — and the accounting for this one is that it belongs to the config
    writer, not to memory. What the pin is guarding is that no store file appears
    outside ``memory.db*`` and ``workspace/memory/``: a silo leaking into the
    default home would show up here as a ``memory_stores/`` entry.

    ``connections_ui_migrated.json`` is the loader's one-time migration marker.
    It records that legacy connection visibility has already been translated;
    it is config state, not a memory-store artifact.
    """
    history = seeded.history_file.relative_to(seeded.home).as_posix()
    assert _home_files(seeded.home) == {
        "agent-prompt.md",
        "config.json",
        "config.json.bak",
        "config.json.lock",
        "connections_ui_migrated.json",
        "memory.db",
        "memory.db-shm",
        "memory.db-wal",
        "skills/alpha/SKILL.md",
        "skills/beta/SKILL.md",
        history,
        "workspace/memory/preferences.md",
        "workspace/memory/projects.md",
    }


# ── 6. Embed accounting: 3 calls, 1 inference ────────────────────────────────


def test_one_build_makes_three_embed_calls_and_one_inference(seeded: Seeded) -> None:
    """Three retrieval paths embed the query; the lru_cache collapses them to one.

    The three are semantic ranking, episodic recall and lesson ranking, in that
    order — each embeds the QUERY only, never a row (rows carry write-time
    vectors). ``make_sync_embed_fn``'s ``lru_cache`` is keyed on
    ``(text, model_id)``, so the identical query text costs exactly one inference
    and the other two are hits. Both numbers are asserted: 3 alone would not notice
    the cache being lost, and 1 alone would not notice a fourth retrieval path
    riding the cache for free.
    """
    _session_context(seeded)

    assert seeded.embed_fn_calls == [QUERY, QUERY, QUERY]
    assert seeded.inference_calls == [QUERY]


def test_a_second_build_is_served_entirely_from_the_embedding_cache(seeded: Seeded) -> None:
    """The cache spans builds, so a second identical build pays no inference."""
    _session_context(seeded)
    seeded.embed_fn_calls.clear()
    seeded.inference_calls.clear()
    _session_context(seeded)
    assert seeded.embed_fn_calls == [QUERY, QUERY, QUERY]
    assert seeded.inference_calls == []


def test_an_empty_query_skips_episodic_recall_and_lesson_ranking(seeded: Seeded) -> None:
    """No query means recency order and no embedding at all — the eval-runner path."""
    payload = seeded.builder.build_session_context(session_key=SESSION_KEY, query_text="")
    assert seeded.embed_fn_calls == []
    assert seeded.inference_calls == []
    assert "[Episodic Memory" not in payload
    # Recency order, so the last-written semantic row leads and no row is dropped.
    block = payload[payload.index("[Semantic Memory") : payload.index("[End of semantic memory]")]
    rows = [line for line in block.splitlines() if line.startswith(("pref.", "user."))]
    assert rows[0] == f"{SEM_OFF[0]}: {SEM_OFF[1]}"
    assert len(rows) == 3


# ── 7. The default store resolves to the legacy path ─────────────────────────


def test_default_store_resolves_to_config_dir_memory_db(seeded: Seeded) -> None:
    """The default named store IS ``config_dir()/"memory.db"`` — the legacy path.

    v1 is a permanent resolution result of one store name, not a phase the system
    leaves, so this is a contract and not a migration detail.
    """
    assert vector_memory._DB_FILE == "memory.db"
    expected = config_dir() / "memory.db"
    assert VectorMemoryStore()._db_path == expected
    assert seeded.vectors._db_path == expected
    # Resolved on both sides: a platform whose temp root is itself a link (macOS
    # /tmp -> /private/tmp) hands ``config_dir()`` the resolved spelling.
    assert expected.parent.resolve() == seeded.home.resolve()
    assert expected.is_file()
    # And it is the store the assembled payload actually read from.
    assert ctx._memory_stores["default"].vector_store is seeded.vectors
