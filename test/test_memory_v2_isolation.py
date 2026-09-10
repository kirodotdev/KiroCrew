"""Private member isolation and unchanged Global Memory V1 behavior.

Owned V2 stores have distinct files and require explicit successful preparation.
Invalid identities fail closed, while legacy global and declared V1 stores keep
existing fallback behavior. Path and content assertions prove both isolation
and successful retrieval; fixtures deliberately collide workspace/store names.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import context as ctx
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import (
    KiroCrewConfig,
    config_dir,
    resolve_agent_bindings,
    workspace_dir_for,
)
from kiro_crew.dashboard.handlers import cron
from kiro_crew.history import ConversationLog
from kiro_crew.history_consolidation import HistoryConsolidator
from kiro_crew.hooks import HookManager
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.member_memory_auth import bind_private_session_store
from kiro_crew.memory import INDEX_DB_FILE, MemoryStore
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    MEMBER_MEMORY_MANIFEST,
    MEMORY_DB_FILE,
    MEMORY_STORES_DIR_NAME,
    UnknownMemoryStore,
    memory_index_path_for,
    memory_store_dir_for,
    memory_stores_root,
    resolve_store_path,
)
from kiro_crew.skills import SkillsLoader
from kiro_crew.vector_memory import VectorMemoryStore

#: The two silos, and the crew bound to each. Both are DECLARED in the fixture's
#: config, because declaring is what makes a store resolvable at all — an undeclared
#: name degrades onto the default, which is how an earlier precedence test managed to
#: pass with the store ignored outright.
CODING = "coding"
EMAIL = "email"
CODING_CREW = "coder"
EMAIL_CREW = "mailer"
DEFAULT_CREW = "default"

#: ``coding`` as a WORKSPACE name, pointed at a directory no store owns.
_COLLIDING_WS_DIR = "coding-workspace"

#: The v1 filenames, as literals. These assertions exist to say the bytes have not
#: moved, so deriving them from the production constants would make the pin circular.
#: The constants are asserted equal to the literals instead, so a rename shows up here
#: rather than silently passing.
_V1_WORKSPACE_DIR = "workspace"
_V1_VECTOR_FILE = "memory.db"
_V1_INDEX_FILE = "memory_index.db"
_LESSONS_FILE = "lessons.jsonl"

#: Fixed so no test reads the clock. ``Lesson.ts`` is never asserted on; it only has
#: to be a string the JSONL round-trip preserves.
_LESSON_TS = "2026-03-04T05:06:00+00:00"


def _config_payload() -> dict:
    """A config declaring three stores, three crews, and the name collision."""
    return {
        "agents": {
            CODING_CREW: {
                "kiro_agent": "kirocrew",
                "memory_store": CODING,
                # Bound to the WORKSPACE that shares the store's name, so this
                # crew alone would expose a resolver that reads one namespace.
                "workspace": CODING,
            },
            EMAIL_CREW: {"kiro_agent": "kirocrew", "memory_store": EMAIL},
            DEFAULT_CREW: {"kiro_agent": "kirocrew", "memory_store": DEFAULT_MEMORY_STORE},
        },
        "default_agent": DEFAULT_CREW,
        "workspaces": {
            "default": {"dir": _V1_WORKSPACE_DIR},
            CODING: {"dir": _COLLIDING_WS_DIR},
        },
        "default_workspace": "default",
        "memory_stores": {
            DEFAULT_MEMORY_STORE: {},
            CODING: {"owner_member": CODING_CREW, "memory_version": 2},
            EMAIL: {"owner_member": EMAIL_CREW, "memory_version": 2},
            "legacy": {},
        },
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }


@dataclass
class Silos:
    """The global v1 pair, plus the builder that registered it."""

    home: Path
    builder: ctx.ContextBuilder
    #: Markdown at ``config_dir()/"workspace"``, vectors at ``config_dir()/"memory.db"``
    #: — the memory an install already has, and the thing a silo must not touch.
    global_memory: MemoryStore
    global_vectors: VectorMemoryStore
    global_lessons: LessonStore


@pytest.fixture
def silos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # These tests use fake extraction/providers and exercise memory routing;
    # actual kernel enforcement has its own sandbox integration probe.
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.private_memory_execution_supported", lambda **kwargs: True
    )
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth._request_peer_pid", lambda request: os.getpid()
    )
    (config_dir() / "config.json").write_text(json.dumps(_config_payload()), encoding="utf-8")
    loader_mod._invalidate_config_cache()
    for name, member in ((CODING, CODING_CREW), (EMAIL, EMAIL_CREW)):
        root = memory_stores_root() / name
        root.mkdir(parents=True)
        (root / MEMBER_MEMORY_MANIFEST).write_text(
            json.dumps({"owner_member": member, "memory_version": 2}), encoding="utf-8"
        )
        vectors = VectorMemoryStore(db_path=root / MEMORY_DB_FILE)
        vectors.init()
        vectors.close()
    (memory_stores_root() / "legacy").mkdir()

    # The three process-global caches ``ContextBuilder`` resolves through. Reset per
    # test so nothing this test builds can serve another test on the same xdist
    # worker, and so nothing an earlier test left behind can answer here.
    monkeypatch.setattr(ctx, "_memory_stores", {}, raising=True)
    monkeypatch.setattr(ctx, "_lesson_stores", {}, raising=True)
    monkeypatch.setattr(ctx, "_vector_stores", {}, raising=True)

    global_ws = workspace_dir_for(DEFAULT_MEMORY_STORE)
    global_memory = MemoryStore(workspace=global_ws)
    global_memory.init()
    global_vectors = VectorMemoryStore(db_path=config_dir() / MEMORY_DB_FILE)
    global_vectors.init()
    global_memory.vector_store = global_vectors
    global_lessons = LessonStore(base_dir=global_ws)

    builder = ctx.ContextBuilder(
        memory=global_memory,
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        hooks=HookManager(),
        lessons=global_lessons,
        conversation_log=None,
        channel_history=None,
    )
    # ``__init__`` seeds the default slot; every default-spelling assertion below
    # resolves back to exactly this object.
    assert ctx._memory_stores[DEFAULT_MEMORY_STORE] is global_memory
    try:
        yield Silos(
            home=config_dir(),
            builder=builder,
            global_memory=global_memory,
            global_vectors=global_vectors,
            global_lessons=global_lessons,
        )
    finally:
        # Each VectorMemoryStore holds an open SQLite connection, and monkeypatch
        # restores the ORIGINAL cache dict — so anything ``ensure_store`` built is
        # unreachable by teardown and would hold its handle for the worker's life.
        for store in list(ctx._vector_stores.values()):
            store.close()
        global_vectors.close()


def _four_files(memory_store: str | None) -> tuple[Path, Path, Path, Path]:
    """The four files one memory target owns: markdown root, index, vectors, lessons.

    Read through the same seams production uses — the three resolvers, and
    ``get_lessons_for`` for the JSONL tier — so a path returned here is a path a write
    actually lands on rather than one this test composed by hand.
    """
    name = memory_store or DEFAULT_MEMORY_STORE
    return (
        memory_store_dir_for(name),
        memory_index_path_for(name),
        resolve_store_path(name),
        ctx.ContextBuilder.get_lessons_for(memory_store=memory_store)._path,
    )


@pytest.fixture
def prepared_silos(silos):
    """Model the V2 prepare step for synchronous context/FTS consumers."""
    for name in (CODING, EMAIL):
        vectors = VectorMemoryStore(db_path=resolve_store_path(name))
        vectors.init()
        ctx._vector_stores[name] = vectors
    return silos


# ── 1. Four disjoint files per named store, asserted by PATH ──────────────────


@pytest.mark.usefixtures("prepared_silos")
class TestFourFilesPerNamedStore:
    def test_each_named_store_owns_its_four_files_inside_its_own_directory(self, silos) -> None:
        """All four live under ``memory_stores/<name>/`` — one directory, one silo."""
        assert memory_stores_root() == config_dir() / MEMORY_STORES_DIR_NAME
        for name in (CODING, EMAIL):
            store_dir = memory_stores_root() / name
            markdown, index, vectors, lessons = _four_files(name)
            assert markdown == store_dir
            assert index == store_dir / INDEX_DB_FILE
            assert vectors == store_dir / MEMORY_DB_FILE
            assert lessons == store_dir / _LESSONS_FILE
            # Every one of them INSIDE the store's directory, which is what puts the
            # whole silo behind the ``memory_stores/`` keystone fence at once.
            for path in (index, vectors, lessons):
                assert path.parent == store_dir

    def test_the_twelve_files_of_three_targets_are_twelve_distinct_paths(self, silos) -> None:
        """No pair of the three targets shares any of its four files.

        By PATH, because object identity was never the broken property: the defect
        was two distinct ``MemoryStore`` objects reading two markdown trees while
        both wrote through ONE vector file.
        """
        targets = {
            DEFAULT_MEMORY_STORE: _four_files(None),
            CODING: _four_files(CODING),
            EMAIL: _four_files(EMAIL),
        }
        every = [path for four in targets.values() for path in four]
        assert len(set(every)) == len(every) == 12, f"paths collide: {sorted(map(str, every))}"

    def test_the_store_objects_open_the_paths_the_resolvers_named(self, silos) -> None:
        """``MemoryStore`` is store-agnostic, so the two must not answer differently."""
        for name in (CODING, EMAIL):
            markdown, index, _vectors, _lessons = _four_files(name)
            store = ctx.ContextBuilder.get_memory_for(memory_store=name)
            assert store._workspace == markdown
            assert store._index_db == index

    def test_the_binding_a_crews_config_resolves_is_the_silo_it_reads(self, silos) -> None:
        """The production chain end to end: config → bindings → store.

        The two ends must agree, and the crew that names ``"default"`` must come out
        holding the GLOBAL store — that is the invariant the whole unit is for.
        """
        cfg = KiroCrewConfig.load()
        for crew, expected in (
            (CODING_CREW, CODING),
            (EMAIL_CREW, EMAIL),
            (DEFAULT_CREW, DEFAULT_MEMORY_STORE),
        ):
            name = resolve_agent_bindings(cfg, agent_name=crew).memory_store_name
            assert name == expected, crew
        coding_name = resolve_agent_bindings(cfg, agent_name=CODING_CREW).memory_store_name
        default_name = resolve_agent_bindings(cfg, agent_name=DEFAULT_CREW).memory_store_name
        assert ctx.ContextBuilder.get_memory_for(memory_store=default_name) is silos.global_memory
        coding_store = ctx.ContextBuilder.get_memory_for(memory_store=coding_name)
        assert coding_store is not silos.global_memory
        assert coding_store._workspace == memory_stores_root() / CODING

    def test_the_index_is_per_store_by_behaviour_not_only_by_path(self, silos) -> None:
        """A search answering in one store and not the other is the index assertion.

        A path assertion alone would pass with both stores writing one index file, as
        long as the file names differed on paper.
        """
        coding = ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        email = ctx.ContextBuilder.get_memory_for(memory_store=EMAIL)
        coding.write_preferences("# User Preferences\n\n- reviews with a canary stage\n")
        assert coding.search("canary"), "the writing store cannot find its own row"
        assert email.search("canary") == []
        assert silos.global_memory.search("canary") == []


# ── 2. The default store's paths have not moved ───────────────────────────────


class TestDefaultStorePathsAreTheV1Literals:
    """The hard invariant: declaring silos must not relocate the global store.

    ``test_memory_stores.py`` pins these resolvers on a home with no config at all.
    What is asserted HERE is the same answers while two non-default stores are
    declared and a crew is bound to each — the state in which a resolver that keyed
    off "are there any named stores?" would move the default path.
    """

    def test_the_three_default_paths_are_the_literal_v1_paths(self, silos) -> None:
        home = config_dir()
        assert memory_store_dir_for(DEFAULT_MEMORY_STORE) == home / _V1_WORKSPACE_DIR
        assert resolve_store_path(DEFAULT_MEMORY_STORE) == home / _V1_VECTOR_FILE
        assert memory_index_path_for(DEFAULT_MEMORY_STORE) == home / _V1_INDEX_FILE
        # The constants must spell the same thing, so a rename cannot pass by
        # moving both the production path and a derived expectation together.
        assert MEMORY_DB_FILE == _V1_VECTOR_FILE
        assert INDEX_DB_FILE == _V1_INDEX_FILE
        assert workspace_dir_for(DEFAULT_MEMORY_STORE) == home / _V1_WORKSPACE_DIR

    def test_no_default_path_is_inside_the_stores_root(self, silos) -> None:
        """The default store is deliberately NOT a subdirectory of the new root."""
        root = memory_stores_root()
        for path in _four_files(None):
            assert root not in path.parents, path
            assert path != root

    def test_the_global_pair_is_the_one_the_default_spelling_answers_with(self, silos) -> None:
        assert silos.global_memory._workspace == config_dir() / _V1_WORKSPACE_DIR
        assert silos.global_vectors._db_path == config_dir() / _V1_VECTOR_FILE
        assert VectorMemoryStore()._db_path == silos.global_vectors._db_path
        assert silos.global_lessons._path == config_dir() / _V1_WORKSPACE_DIR / _LESSONS_FILE

    def test_new_private_stores_have_an_empty_database_and_ownership_manifest(self, silos) -> None:
        for name in (CODING, EMAIL):
            assert {p.name for p in (memory_stores_root() / name).iterdir()} == {
                MEMBER_MEMORY_MANIFEST,
                MEMORY_DB_FILE,
            }


# ── 3. The three default spellings are one object ─────────────────────────────


class TestDefaultSpellingsCollapseOntoOneStore:
    def test_bare_none_and_explicit_default_all_return_the_global_store(self, silos) -> None:
        implicit = ctx.ContextBuilder.get_memory_for()
        positional_none = ctx.ContextBuilder.get_memory_for(None)
        explicit = ctx.ContextBuilder.get_memory_for(memory_store=DEFAULT_MEMORY_STORE)
        assert implicit is positional_none is explicit is silos.global_memory
        assert ctx._memory_stores[DEFAULT_MEMORY_STORE] is implicit

    def test_no_spelling_mints_a_second_cache_slot(self, silos) -> None:
        """The default key's SPELLING is load-bearing, so a synonym is a second store.

        ``"store:default"`` would be a fresh slot with a fresh ``MemoryStore``, and
        both would then answer for the same files with two FTS handles.
        """
        for spelling in (None, "", DEFAULT_MEMORY_STORE):
            ctx.ContextBuilder.get_memory_for(memory_store=spelling)
        assert set(ctx._memory_stores) == {DEFAULT_MEMORY_STORE}

    def test_target_key_mints_the_default_key_for_every_default_spelling(self, silos) -> None:
        assert ctx._target_key(None, None) == (DEFAULT_MEMORY_STORE, "")
        assert ctx._target_key(None, "") == (DEFAULT_MEMORY_STORE, "")
        assert ctx._target_key(None, DEFAULT_MEMORY_STORE) == (DEFAULT_MEMORY_STORE, "")
        assert ctx._target_key("default", None) == (DEFAULT_MEMORY_STORE, "")

    def test_the_lessons_seam_collapses_the_same_way(self, silos) -> None:
        implicit = ctx.ContextBuilder.get_lessons_for()
        explicit = ctx.ContextBuilder.get_lessons_for(memory_store=DEFAULT_MEMORY_STORE)
        assert implicit is explicit
        assert implicit._path == silos.global_lessons._path
        assert set(ctx._lesson_stores) == {DEFAULT_MEMORY_STORE}


# ── 4. A named store's vectors are its own ────────────────────────────────────


class TestNamedStoreVectorsAreNeverTheGlobalOnes:
    @pytest.mark.asyncio
    async def test_ensure_store_returns_none_for_every_default_spelling(self, silos) -> None:
        """The default store's vectors are the global ones, wired at startup."""
        for spelling in (None, "", DEFAULT_MEMORY_STORE):
            assert await ctx.ContextBuilder.ensure_store(spelling) is None
        assert ctx._vector_stores == {}

    @pytest.mark.asyncio
    async def test_each_named_store_gets_its_own_vector_file(self, silos) -> None:
        coding = await ctx.ContextBuilder.ensure_store(CODING)
        email = await ctx.ContextBuilder.ensure_store(EMAIL)
        assert coding is not None and email is not None
        assert coding is not email
        assert coding is not silos.global_vectors
        assert email is not silos.global_vectors
        assert coding._db_path == memory_stores_root() / CODING / MEMORY_DB_FILE
        assert email._db_path == memory_stores_root() / EMAIL / MEMORY_DB_FILE
        # Three files, not three handles onto one: the handles were always
        # distinct objects, the FILE is what was shared.
        assert len({coding._db_path, email._db_path, silos.global_vectors._db_path}) == 3

    @pytest.mark.asyncio
    async def test_ensure_store_is_idempotent(self, silos) -> None:
        """One instance per db_path is an invariant: two do not share ``_db_lock``."""
        first = await ctx.ContextBuilder.ensure_store(CODING)
        second = await ctx.ContextBuilder.ensure_store(CODING)
        assert first is second
        assert ctx._vector_stores[CODING] is first

    @pytest.mark.asyncio
    async def test_the_memory_store_is_wired_to_its_own_vectors_in_either_order(
        self, silos
    ) -> None:
        """The consolidator resolves markdown BEFORE it stands the vectors up.

        So the ``get_memory_for`` → ``ensure_store`` order has to back-fill the
        already-cached markdown store, and the reverse order has to find the vectors
        already in the cache. Both must end wired to the store's OWN file.
        """
        with pytest.raises(UnknownMemoryStore, match="Prepare member"):
            ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        coding = await ctx.ContextBuilder.ensure_store(CODING)
        markdown_first = ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        assert markdown_first.vector_store is coding

        email_vectors = await ctx.ContextBuilder.ensure_store(EMAIL)
        email = ctx.ContextBuilder.get_memory_for(memory_store=EMAIL)
        assert email.vector_store is email_vectors

        for store, vectors in ((markdown_first, coding), (email, email_vectors)):
            assert store.vector_store is not silos.global_vectors
            assert store.vector_store is not None
            assert store.vector_store._db_path == vectors._db_path

    @pytest.mark.asyncio
    async def test_an_unprepared_private_store_refuses_context_until_prepared(self, silos) -> None:
        """Losing a semantic row is recoverable; reading another crew's rows is not."""
        with pytest.raises(UnknownMemoryStore, match="Prepare member"):
            ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        assert silos.global_memory.vector_store is silos.global_vectors


# ── 5. Write isolation ────────────────────────────────────────────────────────


@pytest.mark.usefixtures("prepared_silos")
class TestWriteIsolation:
    @pytest.mark.asyncio
    async def test_a_semantic_row_written_to_one_store_reaches_no_other(self, silos) -> None:
        coding = await ctx.ContextBuilder.ensure_store(CODING)
        email = await ctx.ContextBuilder.ensure_store(EMAIL)
        assert coding is not None and email is not None

        assert coding.set_semantic("pref.editor", "vim", 1.0, "user_explicit") is None
        assert email.set_semantic("pref.editor", "emacs", 1.0, "user_explicit") is None
        assert silos.global_vectors.set_semantic("pref.shell", "zsh", 1.0, "user_explicit") is None

        assert coding.get_semantic("pref.editor")["value_json"] == '"vim"'
        assert email.get_semantic("pref.editor")["value_json"] == '"emacs"'
        assert coding.get_semantic("pref.shell") is None
        assert email.get_semantic("pref.shell") is None

        # ``get_all_semantic`` is the riskiest read on the write path: its rows go
        # into the consolidation prompt, and that prompt instructs the model to
        # UPDATE and DELETE them. A global fetch here let one crew's consolidation
        # turn delete the operator's own memory.
        assert [r["key"] for r in coding.get_all_semantic()] == ["pref.editor"]
        assert [r["key"] for r in email.get_all_semantic()] == ["pref.editor"]
        assert [r["key"] for r in silos.global_vectors.get_all_semantic()] == ["pref.shell"]

    @pytest.mark.asyncio
    async def test_an_episodic_row_written_to_one_store_reaches_no_other(self, silos) -> None:
        coding = await ctx.ContextBuilder.ensure_store(CODING)
        assert coding is not None
        assert coding.write_episodic("The coding crew rotated the deploy key.", source="test")
        assert [r["text"] for r in coding.get_episodic_list(limit=10)] == [
            "The coding crew rotated the deploy key."
        ]
        assert silos.global_vectors.get_episodic_list(limit=10) == []

    def test_a_lesson_written_to_one_stores_jsonl_reaches_no_other(self, silos) -> None:
        coding = ctx.ContextBuilder.get_lessons_for(memory_store=CODING)
        email = ctx.ContextBuilder.get_lessons_for(memory_store=EMAIL)
        global_store = ctx.ContextBuilder.get_lessons_for()
        assert len({coding._path, email._path, global_store._path}) == 3

        coding.save(Lesson(ts=_LESSON_TS, rule="Rebase before merging.", category="preference"))
        assert [lesson.rule for lesson in coding.load_all()] == ["Rebase before merging."]
        assert email.load_all() == []
        assert global_store.load_all() == []
        assert coding._path == memory_stores_root() / CODING / _LESSONS_FILE
        assert not global_store._path.exists(), "the global lessons file must be untouched"

    @pytest.mark.asyncio
    async def test_a_lesson_written_to_one_stores_vector_table_reaches_no_other(
        self, silos
    ) -> None:
        coding = await ctx.ContextBuilder.ensure_store(CODING)
        assert coding is not None
        assert coding.write_lesson("Rebase before merging.", category="preference")
        assert len(coding.get_lessons()) == 1
        assert silos.global_vectors.get_lessons() == []

    def test_preferences_written_to_one_store_reach_no_other(self, silos) -> None:
        coding = ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        email = ctx.ContextBuilder.get_memory_for(memory_store=EMAIL)
        assert coding.write_preferences("# User Preferences\n\n- coding crew only\n")

        assert "coding crew only" in coding.read_preferences()
        assert "coding crew only" not in email.read_preferences()
        assert "coding crew only" not in silos.global_memory.read_preferences()
        # And by path, so a shared file that merely happened to be read twice
        # cannot pass: three preferences files, three parents.
        files = {
            coding._preferences_file,
            email._preferences_file,
            silos.global_memory._preferences_file,
        }
        assert len(files) == 3
        assert coding._preferences_file.parent.parent == memory_stores_root() / CODING


# ── 6. Degradation: anything that is not a declared store is the v1 path ──────


class TestInvalidIdentityFailsClosed:
    @pytest.mark.parametrize("name", [None, "", DEFAULT_MEMORY_STORE], ids=repr)
    def test_only_global_spellings_keep_v1(self, silos, name) -> None:
        assert ctx._target_key(None, name) == (DEFAULT_MEMORY_STORE, "")
        assert ctx.ContextBuilder.get_memory_for(memory_store=name) is silos.global_memory

    @pytest.mark.parametrize("name", ["   ", "no-such-store", "Bad_Name", "../escape", "con", 7])
    def test_read_seams_refuse_invalid_identity(self, silos, name) -> None:
        for read in (ctx.ContextBuilder.get_memory_for, ctx.ContextBuilder.get_lessons_for):
            with pytest.raises(UnknownMemoryStore):
                read(memory_store=name)
        with pytest.raises(UnknownMemoryStore):
            ctx._target_key(None, name)
        assert set(ctx._memory_stores) == {DEFAULT_MEMORY_STORE}
        assert ctx._vector_stores == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["   ", "no-such-store", "Bad_Name", "../escape"])
    async def test_invalid_identity_cannot_prepare_vectors(self, silos, name) -> None:
        with pytest.raises(UnknownMemoryStore):
            await ctx.ContextBuilder.ensure_store(name)
        assert ctx._vector_stores == {}

    def test_undeclared_directory_is_never_an_alias_for_global(self, silos) -> None:
        with pytest.raises(UnknownMemoryStore):
            memory_store_dir_for("no-such-store")


# ── 7. Two namespaces, two targets ────────────────────────────────────────────


@pytest.mark.usefixtures("prepared_silos")
class TestStoreAndWorkspaceAreSeparateNamespaces:
    """``mem_key = memory_store or workspace`` collapsed these into one slot.

    Whichever of the two was built first then decided where the other one read, so
    the crew bound to store ``coding`` could be served the ``coding`` WORKSPACE's
    markdown tree — or the reverse — depending only on arrival order.
    """

    def test_the_same_name_mints_two_different_cache_keys(self, silos) -> None:
        ws_key, ws_store = ctx._target_key(CODING, None)
        store_key, store_store = ctx._target_key(None, CODING)
        assert ws_key != store_key
        assert (ws_key, ws_store) == ("ws:" + CODING, "")
        assert (store_key, store_store) == ("store:" + CODING, CODING)

    def test_the_same_name_resolves_to_two_different_trees(self, silos) -> None:
        ws_store = ctx.ContextBuilder.get_memory_for(CODING)
        silo = ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        assert ws_store is not silo
        assert ws_store._workspace == config_dir() / _COLLIDING_WS_DIR
        assert silo._workspace == memory_stores_root() / CODING
        assert ws_store._workspace != silo._workspace
        assert ws_store._index_db != silo._index_db
        assert set(ctx._memory_stores) == {DEFAULT_MEMORY_STORE, "ws:" + CODING, "store:" + CODING}

    def test_a_named_store_wins_over_a_workspace_of_the_same_name(self, silos) -> None:
        """The silo is the tighter scope, so it decides — never arrival order."""
        both = ctx.ContextBuilder.get_memory_for(CODING, CODING)
        assert both is ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        assert both._workspace == memory_stores_root() / CODING

    def test_a_workspace_still_shares_the_global_vector_store(self, silos) -> None:
        """The workspace arm is the v1 path and must stay byte-identical to it.

        Markdown splits per workspace; the vector rows have always been global
        there, and narrowing that would change an existing install's behaviour.
        """
        ws_store = ctx.ContextBuilder.get_memory_for(CODING)
        assert ws_store.vector_store is silos.global_vectors
        silo = ctx.ContextBuilder.get_memory_for(memory_store=CODING)
        assert silo.vector_store is not silos.global_vectors

    def test_the_lessons_seam_splits_the_two_namespaces_too(self, silos) -> None:
        ws_lessons = ctx.ContextBuilder.get_lessons_for(CODING)
        silo_lessons = ctx.ContextBuilder.get_lessons_for(memory_store=CODING)
        assert ws_lessons._path != silo_lessons._path
        assert ws_lessons._path == config_dir() / _COLLIDING_WS_DIR / _LESSONS_FILE
        assert silo_lessons._path == memory_stores_root() / CODING / _LESSONS_FILE


# ── 8. The consolidator writes to the store it was handed ─────────────────────


class TestConsolidatorStoreResolution:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("unavailable", [False, True])
    async def test_consolidation_memory_admission_and_profile_reads_run_off_loop(
        self, silos, tmp_path, monkeypatch, unavailable
    ) -> None:
        from test_history_consolidation_retry import KEY, _make_consolidator, _seed_log

        from kiro_crew import memory_stores

        log = await asyncio.to_thread(_seed_log, tmp_path)
        await asyncio.to_thread(bind_private_session_store, KEY, CODING)
        await asyncio.to_thread(log.update_metadata, KEY, {"memory_store": CODING})
        consolidator = _make_consolidator(log, vector_store=silos.global_vectors)
        calls: list[str] = []

        def watch(owner, name):
            original = getattr(owner, name)

            def checked(*args, **kwargs):
                with pytest.raises(RuntimeError, match="no running event loop"):
                    asyncio.get_running_loop()
                calls.append(name)
                if unavailable and name == "store_of_session":
                    raise UnknownMemoryStore("member identity offline")
                return original(*args, **kwargs)

            monkeypatch.setattr(owner, name, checked)

        watch(ctx, "store_of_session")
        watch(memory_stores, "memory_store_version")
        watch(ctx.ContextBuilder, "get_memory_for")
        watch(ctx.ContextBuilder, "get_lessons_for")
        watch(MemoryStore, "read_preferences")
        watch(MemoryStore, "read_projects")
        model = AsyncMock(return_value={"history_entry": "The member finished its task."})
        monkeypatch.setattr(consolidator, "_call_llm", model)

        if unavailable:
            with pytest.raises(UnknownMemoryStore, match="member identity offline"):
                await consolidator._consolidate(KEY)
        else:
            await consolidator._consolidate(KEY)

        assert calls.count("store_of_session") == 1
        if unavailable:
            model.assert_not_awaited()
            assert calls == ["store_of_session"]
            assert log.unconsolidated_count(KEY) > 0
            assert ctx._vector_stores == {}
        else:
            model.assert_awaited_once()
            assert {
                "memory_store_version",
                "get_memory_for",
                "get_lessons_for",
                "read_preferences",
                "read_projects",
            } <= set(calls)
            assert log.unconsolidated_count(KEY) == 0
            assert ctx._vector_stores[CODING] is not silos.global_vectors
        assert silos.global_vectors.get_all_semantic() == []
        assert silos.global_vectors.get_episodic_list(limit=10) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("private", [False, True])
    async def test_private_consolidation_cannot_publish_or_refine_global_skills(
        self, silos, prepared_silos, tmp_path, private
    ) -> None:
        from unittest.mock import MagicMock

        from test_history_consolidation_retry import KEY, _make_consolidator, _seed_log

        log = _seed_log(tmp_path)
        if private:
            await asyncio.to_thread(bind_private_session_store, KEY, CODING)
            await asyncio.to_thread(log.update_metadata, KEY, {"memory_store": CODING})
        loader = MagicMock()
        consolidator = _make_consolidator(
            log,
            vector_store=silos.global_vectors,
            skills_loader=loader,
            auto_skills_enabled=True,
        )
        with (
            patch.object(
                consolidator, "_call_llm", AsyncMock(return_value={"history_entry": "done"})
            ),
            patch.object(consolidator, "_run_skill_detection", AsyncMock()) as detect,
        ):
            await consolidator._consolidate(KEY)
        assert detect.await_count == (0 if private else 1)
        assert loader.run_skill_lifecycle.call_count == (0 if private else 1)
        assert log.unconsolidated_count(KEY) == 0

    @pytest.mark.asyncio
    async def test_structured_memory_is_written_to_the_store_passed_in(
        self, silos, tmp_path
    ) -> None:
        """Constructed with the GLOBAL store, handed a silo: the silo must win.

        ``self._vector_store`` was written unconditionally, so a crew read its own
        markdown and filed its findings into the operator's table.
        """
        target = await ctx.ContextBuilder.ensure_store(CODING)
        assert target is not None
        consolidator = self._consolidator(silos, tmp_path)

        consolidator._write_structured_memory(
            {
                "semantic": [{"key": "pref.editor", "value": "vim", "confidence": 1.0}],
                "episodic": [{"text": "The coding crew rotated the deploy key."}],
            },
            "dashboard:chat-coding",
            target,
        )

        assert [r["key"] for r in target.get_all_semantic()] == ["pref.editor"]
        assert [r["text"] for r in target.get_episodic_list(limit=10)] == [
            "The coding crew rotated the deploy key."
        ]
        assert silos.global_vectors.get_all_semantic() == []
        assert silos.global_vectors.get_episodic_list(limit=10) == []

    @pytest.mark.asyncio
    async def test_omitting_the_store_keeps_the_global_handle(self, silos, tmp_path) -> None:
        """The workspace and default arms pass nothing, and must keep v1 behaviour."""
        consolidator = self._consolidator(silos, tmp_path)
        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.shell", "value": "zsh", "confidence": 1.0}]},
            "dashboard:chat-global",
        )
        assert [r["key"] for r in silos.global_vectors.get_all_semantic()] == ["pref.shell"]

    @pytest.mark.asyncio
    async def test_lessons_are_written_to_the_vector_store_passed_in(self, silos, tmp_path) -> None:
        target = await ctx.ContextBuilder.ensure_store(CODING)
        assert target is not None
        consolidator = self._consolidator(silos, tmp_path)

        consolidator._save_lessons(
            [{"rule": "Rebase before merging.", "category": "preference"}], target, None
        )

        assert len(target.get_lessons()) == 1
        assert silos.global_vectors.get_lessons() == []
        assert not silos.global_lessons._path.exists()

    def test_lessons_are_written_to_the_jsonl_store_passed_in(self, silos, tmp_path) -> None:
        """The JSONL tier is reached only when the silo has no vector store yet.

        Misfiling here is worse than losing a row: the fallback is a WRITE target, so
        every crew's corrections append to the one global ``lessons.jsonl``.

        ``vector_store=None`` is what a silo whose vector store could not be stood up
        passes, and it must mean "skip that tier" — NOT "inherit the global handle".
        Inheriting takes the dedup-aware vector branch and never reaches the silo's
        own file at all, which is the exact shape of the defect being closed.
        """
        silo_lessons = ctx.ContextBuilder.get_lessons_for(memory_store=CODING)
        consolidator = self._consolidator(silos, tmp_path)

        consolidator._save_lessons(
            [{"rule": "Rebase before merging.", "category": "preference"}], None, silo_lessons
        )

        assert [lesson.rule for lesson in silo_lessons.load_all()] == ["Rebase before merging."]
        assert silos.global_lessons.load_all() == []
        assert not silos.global_lessons._path.exists()
        assert silos.global_vectors.get_lessons() == [], "the crew's rows reached the global table"

    @pytest.mark.asyncio
    async def test_a_silo_with_no_vector_store_skips_the_tier_rather_than_inheriting(
        self, silos, tmp_path
    ) -> None:
        """The unprepared-silo path, driven the way ``_consolidate`` drives it.

        ``ensure_store`` answers ``None`` whenever it cannot hand back a silo's own
        vector store, and both write helpers then receive that ``None`` positionally.
        So an omitted argument and an explicit ``None`` cannot share a default: one
        means "the global handle", the other means "this silo has no such tier".
        """
        consolidator = self._consolidator(silos, tmp_path)
        silo_lessons = ctx.ContextBuilder.get_lessons_for(memory_store=CODING)
        with patch.object(ctx, "_build_store_vectors", AsyncMock(return_value=None)):
            unprepared = await ctx.ContextBuilder.ensure_store("legacy")
        assert unprepared is None, "an unavailable legacy tier does not borrow global vectors"

        consolidator._write_structured_memory(
            {"semantic": [{"key": "pref.editor", "value": "vim", "confidence": 1.0}]},
            "dashboard:chat-coding",
            unprepared,
        )
        consolidator._save_lessons(
            [{"rule": "Rebase before merging.", "category": "preference"}],
            unprepared,
            silo_lessons,
        )

        # The semantic tier is simply skipped — nothing anywhere.
        assert silos.global_vectors.get_all_semantic() == []
        assert silos.global_vectors.get_lessons() == []
        # The lessons tier still has a live handle, so the rule lands in the SILO's
        # own file, which is the branch inheriting the global store would skip.
        assert [lesson.rule for lesson in silo_lessons.load_all()] == ["Rebase before merging."]
        assert silos.global_lessons.load_all() == []

    @staticmethod
    def _consolidator(silos: Silos, tmp_path: Path) -> HistoryConsolidator:
        """A consolidator holding ONLY global handles, the way the gateway builds it."""
        log = ConversationLog(base_dir=tmp_path / "sessions")
        log.init()
        return HistoryConsolidator(
            log=log,
            memory=silos.global_memory,
            vector_store=silos.global_vectors,
            lesson_store=silos.global_lessons,
            migrated=True,
        )


# ── 9. The lessons routes follow the BINDING, never the population ────────────

#: Session keys for the two callers. Both are ``dashboard:`` keys with a live slot, so
#: ``_recognize_session`` accepts them and each route reaches the destination
#: resolution these tests are about rather than a refusal.
_SILO_SESSION_KEY = "dashboard:chat-coding"
_GLOBAL_SESSION_KEY = "dashboard:chat-plain"

#: A lesson only the operator taught, held in the GLOBAL store. Every silo-bound
#: assertion below says this string is neither read, written, nor deleted.
_OPERATOR_RULE = "Deploy on Fridays"
#: What the silo-bound crew teaches. It shares no word with the operator's rule, so a
#: substring delete aimed at one cannot reach the other by accident.
_CREW_RULE = "Rebase before merging"


async def _dashboard_state(
    silos: Silos, sessions_dir: Path, session_key: str, store: str
) -> MagicMock:
    """A ``DashboardState`` stand-in whose session RECORDS *store* as its binding.

    Only what the three lessons routes read is set, and the attributes that decide
    destination are the real objects a gateway builds: ``context_builder`` and
    ``lessons`` hold the GLOBAL handles, so a route that reaches for a global handle
    reaches this fixture's global store and the assertion sees the row.

    The binding is read back out of a real ``ConversationLog`` — the same metadata the
    consolidator resolves from, and the seam ``_session_memory_store`` reads — because
    a route honouring a value nothing records would prove nothing. Seeded off the loop
    so the writes take history's sanctioned acquire path rather than tripping its
    on-loop persistence discipline.
    """
    log = ConversationLog(base_dir=sessions_dir)

    def _seed() -> None:
        log.init()
        log.append(session_key, "user", "remember this")
        log.update_metadata(session_key, {"memory_store": store})

    await asyncio.to_thread(_seed)
    if store == CODING:
        await asyncio.to_thread(bind_private_session_store, session_key, store)

    state = MagicMock()
    state.context_builder = silos.builder
    state.lessons = silos.global_lessons
    state.conversation_log = log
    # ``_recognize_session`` strips the namespace and looks the slot up by name; the
    # slot object itself is read only by ``_get_active_workspace``.
    slot = MagicMock(total_messages=1, workspace="default")
    state._slots = {session_key.split(":", 1)[-1]: slot}
    state._restricted_keys = set()
    state._background_tasks = set()
    state.owner_id = ""
    return state


def _lessons_request(
    state: MagicMock,
    session_key: str,
    body: dict | None = None,
    *,
    claims: dict[str, object] | None = None,
) -> MagicMock:
    """One request shape for all three routes: a session key, plus a body for the two
    that read one. The empty query string is what the dashboard sends — a
    ``?workspace=`` override belongs to the v1 workspace tier, which a silo does not
    consult at all.

    The body is fed as BYTES on a real stream, not by stubbing ``request.json``. The
    lessons route reads through ``read_bounded_json``, whose capped path enforces the
    ceiling BEFORE decoding: it compares ``request.content_length`` against the cap, so
    a ``MagicMock`` there raises ``TypeError: '>' not supported`` instead of parsing.
    ``read_bounded_json``'s own docstring states this contract — a site on the cap must
    be fed ``content``/``content_length``. ``request.json`` is left stubbed as well so
    the shape also satisfies the uncapped (``max_bytes=None``) path.
    """
    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": session_key}
    request.query = {}
    identity = {"internal_auth": True} if claims is None else claims
    if identity.get("internal_auth") is True:
        from kiro_crew.member_memory_auth import (
            PROOF_HEADER,
            issue_member_session_proof,
            publish_member_session_pid,
        )

        # A real protected publication and signature model the trusted MCP
        # connection; the caller-supplied session header alone grants nothing.
        publish_member_session_pid(
            os.getpid(),
            session_key,
            memory_store=CODING if session_key == _SILO_SESSION_KEY else "",
        )
        proof = issue_member_session_proof(session_key, os.getpid())
        if session_key == _SILO_SESSION_KEY:
            assert proof
            request.headers[PROOF_HEADER] = proof
    request.get.side_effect = identity.get
    request.__contains__.side_effect = identity.__contains__
    request.__getitem__.side_effect = identity.__getitem__
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        request.content_length = len(raw)
        request.can_read_body = True
        request.charset = "utf-8"

        async def _iter_chunked(_size: int, _payload: bytes = raw):
            yield _payload

        request.content.iter_chunked = _iter_chunked
        request.json = AsyncMock(return_value=body)
    return request


@contextlib.contextmanager
def _session_mode_gates_open():
    """Hold the gates these tests are not about open.

    Session-mode refusals and the SEL sink have their own suites; patching them here
    keeps every assertion below about DESTINATION alone.
    """
    with (
        patch.object(cron, "_sel"),
        patch.object(cron, "_is_restricted_session", return_value=False),
        patch.object(cron, "_blocks_reads_session", return_value=False),
    ):
        yield


def _operator_lesson() -> Lesson:
    return Lesson(rule=_OPERATOR_RULE, category="preference", ts=_LESSON_TS)


def _crew_lesson() -> Lesson:
    return Lesson(rule=_CREW_RULE, category="preference", ts=_LESSON_TS)


@pytest.mark.asyncio
class TestLessonRoutesFollowTheBindingNotThePopulation:
    """``/api/lessons`` must resolve its destination from the caller's BINDING.

    A named store starts EMPTY and nothing is ever copied into it, so "this store
    holds no lesson rows" is the ordinary state of a freshly bound crew — never
    evidence that the operator's global store is the right answer. Keying the
    fallback on population instead of on binding is the shape that lets an empty silo
    read the operator's lessons, delete one of them, and file the crew's own
    correction into the global file every other crew reads on every turn.

    Two triggers reach it and both are covered per route: a silo whose vector store
    was never stood up (``ensure_store`` not awaited, or unable to answer), and a
    prepared silo that simply holds no rows yet.
    """

    @pytest.mark.parametrize("route", ["list", "create", "delete"])
    @pytest.mark.parametrize(
        "claims, allowed",
        [
            ({"user": "viewer", "app": ""}, False),
            ({"user": "operator", "app": "external-app"}, False),
            ({"user": "operator", "app": ""}, True),
            ({"internal_auth": True}, True),
        ],
        ids=["non-owner", "app-token", "owner", "internal"],
    )
    async def test_only_verified_internal_or_owner_requests_can_follow_a_silo_binding(
        self, silos, tmp_path, route, claims, allowed
    ) -> None:
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        state.owner_id = "operator"
        lessons = ctx.ContextBuilder.get_lessons_for(memory_store=CODING)
        lessons.save(_crew_lesson())
        body = {"rule": _CREW_RULE, "category": "preference"} if route != "list" else None
        request = _lessons_request(state, _SILO_SESSION_KEY, body, claims=claims)
        # A header by itself proves nothing: only middleware can publish the marker.
        request.headers["X-Internal-Secret"] = "unverified-secret"
        handler = {
            "list": cron.api_lessons,
            "create": cron.api_lessons_create,
            "delete": cron.api_lessons_delete,
        }[route]

        with _session_mode_gates_open():
            response = await handler(request)

        assert response.status == (200 if allowed else 403)
        if not allowed:
            assert _CREW_RULE not in response.text
            assert [lesson.rule for lesson in lessons.load_all()] == [_CREW_RULE]
            assert silos.global_lessons.load_all() == []
        elif route == "list":
            assert [row["rule"] for row in json.loads(response.text)["lessons"]] == [_CREW_RULE]
        elif route == "delete":
            assert lessons.load_all() == []

    async def test_an_empty_silo_lists_no_lessons_rather_than_the_operators(
        self, silos, tmp_path
    ) -> None:
        """A prepared silo holding zero rows answers "none", not the global file."""
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        assert await ctx.ContextBuilder.ensure_store(CODING) is not None
        silos.global_lessons.save(_operator_lesson())

        with _session_mode_gates_open():
            resp = await cron.api_lessons(_lessons_request(state, _SILO_SESSION_KEY))

        assert resp.status == 200
        assert json.loads(resp.text)["lessons"] == []

    async def test_an_unprepared_silo_lists_no_lessons_rather_than_the_operators(
        self, silos, tmp_path
    ) -> None:
        """The same answer when the silo has no vector store at all.

        ``ensure_store`` is never awaited here, which is what a crew's first turn
        looks like and what a store whose vectors could not be stood up looks like
        forever.
        """
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        assert CODING not in ctx._vector_stores
        silos.global_lessons.save(_operator_lesson())

        with _session_mode_gates_open():
            resp = await cron.api_lessons(_lessons_request(state, _SILO_SESSION_KEY))

        assert resp.status == 200
        assert json.loads(resp.text)["lessons"] == []

    async def test_a_silo_reads_its_own_jsonl_when_it_holds_no_vector_rows(
        self, silos, tmp_path
    ) -> None:
        """No global read is not no read: the silo's OWN JSONL tier still answers.

        This is the tier the consolidator writes when a silo has no vector store, so a
        fix that simply refused to fall back would strand every row it wrote there.
        """
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        ctx.ContextBuilder.get_lessons_for(memory_store=CODING).save(_crew_lesson())
        silos.global_lessons.save(_operator_lesson())

        with _session_mode_gates_open():
            resp = await cron.api_lessons(_lessons_request(state, _SILO_SESSION_KEY))

        assert [row["rule"] for row in json.loads(resp.text)["lessons"]] == [_CREW_RULE]

    async def test_a_silo_bound_write_prepares_and_uses_its_own_vectors(
        self, silos, tmp_path
    ) -> None:
        """An unprepared silo's lesson write must not append to the global file.

        Misfiling here is the worst of the three: the row is durable, it is injected
        into every OTHER crew's turns, and nothing reports that it went to the wrong
        store.
        """
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        assert CODING not in ctx._vector_stores

        with _session_mode_gates_open():
            resp = await cron.api_lessons_create(
                _lessons_request(
                    state, _SILO_SESSION_KEY, body={"rule": _CREW_RULE, "category": "preference"}
                )
            )

        assert json.loads(resp.text)["ok"] is True
        silo_lessons = ctx.ContextBuilder.get_lessons_for(memory_store=CODING)
        assert silo_lessons.load_all() == []
        assert [_CREW_RULE] == [
            json.loads(row["value_json"])["rule"]
            for row in ctx._vector_stores[CODING].get_lessons()
        ]
        assert silos.global_lessons.load_all() == []
        assert not silos.global_lessons._path.exists()
        assert silos.global_vectors.get_lessons() == []

    async def test_a_silo_bound_delete_cannot_remove_an_operators_lesson(
        self, silos, tmp_path
    ) -> None:
        """A prepared-but-empty silo deleting by substring must miss the global file."""
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        assert await ctx.ContextBuilder.ensure_store(CODING) is not None
        silos.global_lessons.save(_operator_lesson())

        with _session_mode_gates_open():
            resp = await cron.api_lessons_delete(
                _lessons_request(state, _SILO_SESSION_KEY, body={"rule": _OPERATOR_RULE})
            )

        assert json.loads(resp.text)["ok"] is False
        assert [lesson.rule for lesson in silos.global_lessons.load_all()] == [_OPERATOR_RULE]

    async def test_an_unprepared_silo_delete_cannot_remove_an_operators_lesson(
        self, silos, tmp_path
    ) -> None:
        """The absence trigger, on the destructive route."""
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        assert CODING not in ctx._vector_stores
        silos.global_lessons.save(_operator_lesson())

        with _session_mode_gates_open():
            resp = await cron.api_lessons_delete(
                _lessons_request(state, _SILO_SESSION_KEY, body={"rule": _OPERATOR_RULE})
            )

        assert json.loads(resp.text)["ok"] is False
        assert [lesson.rule for lesson in silos.global_lessons.load_all()] == [_OPERATOR_RULE]

    async def test_a_silo_bound_delete_still_removes_its_own_lesson(self, silos, tmp_path) -> None:
        """The crew keeps a working delete over the rows it actually owns."""
        state = await _dashboard_state(silos, tmp_path / "sessions", _SILO_SESSION_KEY, CODING)
        silo_lessons = ctx.ContextBuilder.get_lessons_for(memory_store=CODING)
        silo_lessons.save(_crew_lesson())
        silos.global_lessons.save(_operator_lesson())

        with _session_mode_gates_open():
            resp = await cron.api_lessons_delete(
                _lessons_request(state, _SILO_SESSION_KEY, body={"rule": _CREW_RULE})
            )

        assert json.loads(resp.text)["ok"] is True
        assert silo_lessons.load_all() == []
        assert [lesson.rule for lesson in silos.global_lessons.load_all()] == [_OPERATOR_RULE]

    async def test_the_global_binding_still_reads_and_writes_the_global_store(
        self, silos, tmp_path
    ) -> None:
        """The v1 path, unchanged: a caller naming no store gets the global store.

        The install's global vector store is active here, which is why the write lands
        in its lesson table rather than in ``lessons.jsonl`` — the same ladder every
        install without a silo runs. No silo may be created by a global caller's turn.
        """
        state = await _dashboard_state(silos, tmp_path / "sessions", _GLOBAL_SESSION_KEY, "")

        with _session_mode_gates_open():
            created = await cron.api_lessons_create(
                _lessons_request(
                    state,
                    _GLOBAL_SESSION_KEY,
                    body={"rule": _OPERATOR_RULE, "category": "preference"},
                )
            )
            listed = await cron.api_lessons(_lessons_request(state, _GLOBAL_SESSION_KEY))

        assert json.loads(created.text)["ok"] is True
        assert len(silos.global_vectors.get_lessons()) == 1
        assert [row["rule"] for row in json.loads(listed.text)["lessons"]] == [_OPERATOR_RULE]
        assert CODING not in ctx._vector_stores

    async def test_a_global_caller_with_no_vector_store_still_writes_the_global_jsonl(
        self, silos, tmp_path
    ) -> None:
        """The v1 JSONL rung: no vector store means ``lessons.jsonl``, as before."""
        state = await _dashboard_state(silos, tmp_path / "sessions", _GLOBAL_SESSION_KEY, "")
        silos.global_memory.vector_store = None

        with _session_mode_gates_open():
            resp = await cron.api_lessons_create(
                _lessons_request(
                    state,
                    _GLOBAL_SESSION_KEY,
                    body={"rule": _OPERATOR_RULE, "category": "preference"},
                )
            )

        assert json.loads(resp.text)["ok"] is True
        assert [lesson.rule for lesson in silos.global_lessons.load_all()] == [_OPERATOR_RULE]
        assert CODING not in ctx._vector_stores


# ── 10. One definition of "which silo does this session read" ─────────────────

#: A conversation the crew ``coder`` runs, on a channel. Namespaced like a real
#: channel key so nothing here passes only for a ``dashboard:`` shape.
_CHANNEL_SESSION_KEY = "weixin:coder:direct:userA"

#: What the crew's silo holds, and what the OPERATOR's global store holds. They share
#: no word, so a prompt asserting one cannot be satisfied by the other.
_CREW_PREFERENCE = "The coding crew rebases before merging."
_OPERATOR_PREFERENCE = "The operator deploys on Fridays."


def _seeded_log(sessions_dir: Path, session_key: str, store: str) -> ConversationLog:
    """A real log and, for V2, protected binding that agree on *store*.

    The transcript metadata is written and read back through the real log rather
    than stubbed because the consolidator resolves its WRITE side from that line.
    Private V2 additionally requires the protected session record that authorizes
    the READ side; seeding both proves the two sides agree without treating editable
    transcript metadata as authority.

    Fully synchronous, so an async test seeds through ``asyncio.to_thread``:
    ``append`` takes a cross-process flock that makes a single NON-BLOCKING acquire
    while a loop is running, and would silently drop the row it was called for.
    """
    log = ConversationLog(base_dir=sessions_dir)
    log.init()
    log.append(session_key, "user", "remember this")
    if store:
        log.update_metadata(session_key, {"memory_store": store})
    if store == CODING:
        bind_private_session_store(session_key, store)
    return log


class TestTheSessionsRecordedBindingIsWhatASurfaceReads:
    """``store_of_session`` is the ONE resolver every turn-running surface asks.

    A channel surface holds no crew alias — only an ``agent``, which is a kiro-cli
    template id and a namespace disjoint from ``cfg.agents``. The session's own
    protected binding is the authoritative crew identity in scope. The consolidator
    resolves its write side through the same call (``_consolidate`` is pinned to it
    above), so one conversation's reads and writes name one silo.
    """

    def test_a_recorded_silo_is_the_store_the_surface_resolves(self, silos, tmp_path) -> None:
        log = _seeded_log(tmp_path / "sessions", _CHANNEL_SESSION_KEY, CODING)
        assert ctx.store_of_session(log, _CHANNEL_SESSION_KEY) == CODING

    @pytest.mark.parametrize("value", [None, 17, [CODING], "   ", " coding ", "no-such-store"])
    def test_explicit_invalid_metadata_never_selects_global(self, silos, tmp_path, value):
        log = _seeded_log(tmp_path / "sessions", _CHANNEL_SESSION_KEY, "")
        log.update_metadata(_CHANNEL_SESSION_KEY, {"memory_store": value})
        with pytest.raises(UnknownMemoryStore):
            ctx.store_of_session(log, _CHANNEL_SESSION_KEY)

    def test_subagent_reads_its_protected_binding_without_transcript_metadata(self, silos):
        from kiro_crew.subagent_persistence import create_agent_folder

        create_agent_folder("memoryrun", memory_store=CODING)
        assert ctx.store_of_session(ConversationLog(), "subagent:memoryrun") == CODING

    @pytest.mark.asyncio
    async def test_legacy_named_store_keeps_keyword_fallback(self, silos, tmp_path):
        silos.builder.conversation_log = await asyncio.to_thread(
            _seeded_log, tmp_path / "sessions", _CHANNEL_SESSION_KEY, "legacy"
        )
        with patch.object(
            ctx.ContextBuilder, "ensure_store", AsyncMock(side_effect=OSError("disk"))
        ):
            assert await ctx.session_store_for_turn(silos.builder, _CHANNEL_SESSION_KEY) == "legacy"
        assert ctx.ContextBuilder.get_memory_for(memory_store="legacy").vector_store is None

    @pytest.mark.parametrize(
        "recorded",
        ["", DEFAULT_MEMORY_STORE],
        ids=["absent", "the-default-literal"],
    )
    def test_every_global_spelling_answers_the_global_store(
        self, silos, tmp_path, recorded
    ) -> None:
        """``""`` is what keeps a silo unreachable by accident.

        Each of these is a shape a real metadata line carries — a session written
        before silos existed has no key at all — and every one of them has to land on
        the v1 path an install already runs.
        """
        log = _seeded_log(tmp_path / "sessions", _CHANNEL_SESSION_KEY, "")
        if recorded != "":
            log.update_metadata(_CHANNEL_SESSION_KEY, {"memory_store": recorded})
        assert ctx.store_of_session(log, _CHANNEL_SESSION_KEY) == ""

    @pytest.mark.parametrize(
        "log,key",
        [(None, _CHANNEL_SESSION_KEY), (object(), _CHANNEL_SESSION_KEY), (object(), "")],
        ids=["no-log-at-all", "a-log-with-no-reader", "no-session-key"],
    )
    def test_nothing_it_cannot_read_raises(self, silos, log, key) -> None:
        """A gateway with no conversation log must not lose the turn.

        The global store is where a session naming no silo already was, so erring
        toward it costs a default install nothing — while a raise here would drop a
        message the user is waiting on.
        """
        assert ctx.store_of_session(log, key) == ""

    def test_unreadable_session_identity_fails_closed(self, silos) -> None:
        class Exploding:
            def get_metadata(self, _key: str) -> dict:
                raise OSError("transcript is gone")

        with pytest.raises(UnknownMemoryStore):
            ctx.store_of_session(Exploding(), _CHANNEL_SESSION_KEY)

    def test_the_dashboard_seam_reads_through_the_same_resolver(self, silos, tmp_path) -> None:
        """Two copies of "read the recorded binding" is how two surfaces drift.

        The dashboard's lesson routes and every channel turn must answer the same
        store for one session, so ``_session_memory_store`` is an adapter over this
        resolver rather than a second implementation of it.
        """
        from kiro_crew.dashboard.handlers._shared import _session_memory_store

        log = _seeded_log(tmp_path / "sessions", _CHANNEL_SESSION_KEY, CODING)
        state = MagicMock()
        state.conversation_log = log
        assert _session_memory_store(state, _CHANNEL_SESSION_KEY) == CODING
        assert _session_memory_store(state, _CHANNEL_SESSION_KEY) == ctx.store_of_session(
            log, _CHANNEL_SESSION_KEY
        )

    @pytest.mark.asyncio
    async def test_a_turn_stands_up_the_resolved_silos_vectors(self, silos, tmp_path) -> None:
        """The vector tier is prepared BEFORE the build is offloaded.

        ``VectorMemoryStore.init()`` is blocking file IO that ``get_memory_for``'s
        sync resolver deliberately does not perform, so a surface that resolves a
        silo without preparing it gets markdown and keyword scoring forever.
        """
        silos.builder.conversation_log = await asyncio.to_thread(
            _seeded_log, tmp_path / "sessions", _CHANNEL_SESSION_KEY, CODING
        )
        assert ctx._vector_stores.get(CODING) is None

        store = await ctx.session_store_for_turn(silos.builder, _CHANNEL_SESSION_KEY)

        assert store == CODING
        prepared = ctx._vector_stores.get(CODING)
        assert prepared is not None
        assert prepared is not silos.global_vectors
        assert prepared._db_path == resolve_store_path(CODING)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unavailable", [False, True])
    async def test_turn_identity_and_first_store_construction_run_off_loop(
        self, silos, tmp_path, monkeypatch, unavailable
    ) -> None:
        from kiro_crew import embeddings, member_memory_auth, memory_stores

        silos.builder.conversation_log = await asyncio.to_thread(
            _seeded_log, tmp_path / "sessions", _CHANNEL_SESSION_KEY, CODING
        )
        calls: list[str] = []

        def watch(owner, name):
            original = getattr(owner, name)

            def checked(*args, **kwargs):
                with pytest.raises(RuntimeError, match="no running event loop"):
                    asyncio.get_running_loop()
                calls.append(name)
                if unavailable and name == "read_private_session_store":
                    raise OSError("member identity offline")
                return original(*args, **kwargs)

            monkeypatch.setattr(owner, name, checked)

        watch(member_memory_auth, "read_private_session_store")
        watch(ctx, "_resolved_store_name")
        watch(memory_stores, "memory_store_version")
        watch(VectorMemoryStore, "__init__")
        watch(VectorMemoryStore, "init")
        watch(embeddings, "model_file_present")

        if unavailable:
            with pytest.raises(UnknownMemoryStore, match="member identity offline"):
                await ctx.session_store_for_turn(silos.builder, _CHANNEL_SESSION_KEY)
            assert calls == ["read_private_session_store"]
            assert ctx._vector_stores == {}
        else:
            assert await ctx.session_store_for_turn(silos.builder, _CHANNEL_SESSION_KEY) == CODING
            prepared = ctx._vector_stores[CODING]
            assert prepared is not silos.global_vectors
            assert prepared._db_path == await asyncio.to_thread(resolve_store_path, CODING)
            assert {
                "read_private_session_store",
                "_resolved_store_name",
                "memory_store_version",
                "__init__",
                "init",
                "model_file_present",
            } <= set(calls)
        assert silos.global_vectors.get_all_semantic() == []

    @pytest.mark.asyncio
    async def test_a_private_silo_that_cannot_be_stood_up_stops_the_turn(
        self, silos, tmp_path
    ) -> None:
        """Skipping the vector tier, never falling back to the global store.

        Losing a semantic row is recoverable; reading or overwriting another crew's
        rows is not. So a failed prepare must leave the turn pointed at the silo with
        no vectors rather than at the global store with all of them.
        """
        silos.builder.conversation_log = await asyncio.to_thread(
            _seeded_log, tmp_path / "sessions", _CHANNEL_SESSION_KEY, CODING
        )
        with patch.object(
            ctx.ContextBuilder, "ensure_store", AsyncMock(side_effect=RuntimeError("no disk"))
        ):
            with pytest.raises(UnknownMemoryStore, match="no disk"):
                await ctx.session_store_for_turn(silos.builder, _CHANNEL_SESSION_KEY)

        assert ctx._vector_stores == {}
        assert silos.global_vectors.get_all_semantic() == []

    @pytest.mark.asyncio
    async def test_a_builder_with_no_conversation_log_reads_the_global_store(self, silos) -> None:
        """The CLI builds a ``ContextBuilder`` with no log; it must still run."""
        assert silos.builder.conversation_log is None
        assert await ctx.session_store_for_turn(silos.builder, _CHANNEL_SESSION_KEY) == ""


# ── 11. The shared channel pipeline, end to end ───────────────────────────────


class _PipelineSessions:
    """Only what ``drive_turn`` calls. ``is_new=True`` so the memory block is built."""

    def __init__(self) -> None:
        self.created: list[str] = []

    async def get_or_create(self, key: str, agent: str = "", channel_id: str = "", **kw: object):
        self.created.append(key)
        return object(), True, False

    def begin_turn(self, key: str) -> None:
        return None

    async def set_channel(self, key: str, channel_id: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        return None

    async def record_failure(self, key: str) -> None:
        return None

    def release(self, key: str) -> None:
        return None

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return False


class _PipelineRenderer:
    capabilities = None
    channel_type = "weixin"

    async def on_turn_start(self) -> None:
        return None

    async def on_text_chunk(self, text: str) -> None:
        return None

    async def on_done(self, stop_reason: str = "") -> None:
        return None

    async def close(self) -> None:
        return None


async def _prompt_through_the_channel_pipeline(
    builder: ctx.ContextBuilder, session_key: str
) -> str:
    """The prompt ``messaging.dispatch.drive_turn`` hands the model for *session_key*.

    Driven through the REAL pipeline and the REAL ``ContextBuilder``, so what is
    asserted is the store the shipped code reads rather than the store this test
    passed in. Only the seams that would need a network or a live provider are
    substituted: the governance probe, identity publication, the embed pool's thread
    hop, and the turn driver.
    """
    import kiro_crew.messaging.dispatch as dispatch_mod

    captured: list[str] = []

    class _CapturingDriver:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def run(self, message: str) -> str:
            captured.append(message)
            return "answered"

    async def _permitted(_channel_type: str) -> bool:
        return True

    async def _publish(_sessions: object, _key: str) -> None:
        return None

    async def _embed(fn, *args: object, **kw: object):
        return fn(*args, **kw)

    turn = dispatch_mod.ChannelTurn(
        channel_type="weixin",
        session_key=session_key,
        conversation_id="weixin:userA",
        agent="kirocrew",
        user_text="what do you know about merging?",
        renderer=_PipelineRenderer(),
        approval_mode="auto",
    )
    with (
        patch.object(dispatch_mod, "inbound_permitted", _permitted),
        patch.object(dispatch_mod, "publish_turn_identity", _publish),
        patch.object(dispatch_mod, "run_in_embed_pool", _embed),
        patch.object(dispatch_mod, "TurnDriver", _CapturingDriver),
    ):
        await dispatch_mod.drive_turn(turn, sessions=_PipelineSessions(), ctx_builder=builder)

    assert captured, "the pipeline never reached the turn driver"
    return captured[0]


@pytest.mark.asyncio
@pytest.mark.usefixtures("prepared_silos")
class TestTheChannelPipelineReadsTheSessionsOwnSilo:
    """A crew bound to a silo must read ITS memory over a channel, not the operator's.

    This is the feature's headline, and the shape of its failure is that nothing goes
    red: the prompt is well-formed, the store exists, and the content is plausible —
    it is simply somebody else's. So the assertions are on the PROMPT's text, in both
    directions: the crew's line present AND the operator's line absent.

    The pipeline under test is the shared one, which seven shipped channels dispatch
    through, so this covers all of them at once.
    """

    async def test_a_crew_bound_conversation_reads_its_own_memory(self, silos, tmp_path) -> None:
        silos.builder.conversation_log = await asyncio.to_thread(
            _seeded_log, tmp_path / "sessions", _CHANNEL_SESSION_KEY, CODING
        )
        ctx.ContextBuilder.get_memory_for(memory_store=CODING).write_preferences(
            f"# User Preferences\n\n- {_CREW_PREFERENCE}\n"
        )
        silos.global_memory.write_preferences(f"# User Preferences\n\n- {_OPERATOR_PREFERENCE}\n")

        prompt = await _prompt_through_the_channel_pipeline(silos.builder, _CHANNEL_SESSION_KEY)

        assert _CREW_PREFERENCE in prompt
        assert _OPERATOR_PREFERENCE not in prompt

    async def test_a_conversation_with_no_binding_still_reads_the_global_store(
        self, silos, tmp_path
    ) -> None:
        """The v1 path over a channel, byte-for-byte the behaviour every install has.

        The other half of the pin: a fix that simply stopped reading the global store
        would pass the test above and break every conversation that never named a
        silo, which is nearly all of them.
        """
        silos.builder.conversation_log = await asyncio.to_thread(
            _seeded_log, tmp_path / "sessions", _CHANNEL_SESSION_KEY, ""
        )
        ctx.ContextBuilder.get_memory_for(memory_store=CODING).write_preferences(
            f"# User Preferences\n\n- {_CREW_PREFERENCE}\n"
        )
        silos.global_memory.write_preferences(f"# User Preferences\n\n- {_OPERATOR_PREFERENCE}\n")

        prompt = await _prompt_through_the_channel_pipeline(silos.builder, _CHANNEL_SESSION_KEY)

        assert _OPERATOR_PREFERENCE in prompt
        assert _CREW_PREFERENCE not in prompt


# ── 12. Every converted turn surface resolves through the one seam ────────────

#: The call sites that now name a store, as ``(path, the identity each resolves)``.
#: Held by PATH and asserted by AST, never by line number: a line pin goes stale on
#: any edit above the call, and this file's thesis is that matching is structural.
#: A site missing from this table is not silently fine — ``test_memory_store_seam``'s
#: ``EXPECTED_BACKLOG`` counts what is still unconverted, so the two halves together
#: leave no site unaccounted for.
_CONVERTED_TURN_SURFACES: tuple[tuple[str, str], ...] = (
    ("src/kiro_crew/slack/handler.py", "candidate_key"),
    ("src/kiro_crew/slack/transport_dispatch.py", "session_key"),
    ("src/kiro_crew/discord/transport_dispatch.py", "session_key"),
    ("src/kiro_crew/telegram/transport_dispatch.py", "session_key"),
    ("src/kiro_crew/messaging/dispatch.py", "session_key"),
    # Auto-nudge continues the nudged session; both injections continue the PARENT.
    ("src/kiro_crew/slack/gateway.py", "key"),
    ("src/kiro_crew/slack/gateway.py", "parent_key"),
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _surface_id(value: str) -> str:
    """``discord/transport_dispatch.py`` -> ``discord-transport_dispatch``.

    Three of the paths are named ``transport_dispatch.py``, so a basename id makes
    pytest disambiguate them with a trailing digit and a failure names no channel.
    """
    if "/" not in value:
        return value
    parent, _, name = value.rpartition("/")
    return f"{parent.rsplit('/', 1)[-1]}-{name.removesuffix('.py')}"


def _awaited_store_targets(path: str) -> set[str]:
    """The session keys ``session_store_for_turn`` is called with in *path*."""
    tree = ast.parse((_REPO_ROOT / path).read_text(encoding="utf-8"), filename=path)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "session_store_for_turn":
            continue
        for arg in node.args[1:]:
            if isinstance(arg, ast.Name):
                targets.add(arg.id)
    return targets


class TestEveryConvertedSurfaceResolvesThroughTheOneSeam:
    """The store each surface passes is the one the shared resolver answered.

    Two properties, and the seam gate covers neither. It checks that a
    ``memory_store=`` keyword is PRESENT (and that it is not derived from an
    ``agent``); it cannot check that the value came from the session's recorded
    binding rather than from a second, drifting copy of that lookup. Structural
    because the alternative — driving eight surfaces end to end — would pin the
    doubles far more tightly than the behaviour, and the behaviour of the resolver
    itself is pinned above.
    """

    @pytest.mark.parametrize("path,identity", _CONVERTED_TURN_SURFACES, ids=_surface_id)
    def test_the_surface_resolves_its_store_from_a_session_key(self, path, identity) -> None:
        assert identity in _awaited_store_targets(path), (
            f"{path} no longer resolves its memory store from {identity!r} through "
            f"context.session_store_for_turn; a surface that stops asking reads the "
            f"operator's global store for every crew bound to a silo, and nothing "
            f"goes red"
        )

    @pytest.mark.parametrize(
        "path", sorted({p for p, _ in _CONVERTED_TURN_SURFACES}), ids=_surface_id
    )
    def test_the_surface_hands_that_value_to_the_build(self, path) -> None:
        """A resolved store nobody passes is a resolution that changed nothing.

        The value has to reach the build's ``memory_store=``, so the assertion is
        that every such keyword in the file names a local the resolver assigned —
        which is also what stops a future edit from replacing one with an ``agent``
        expression while the resolver call stays behind as decoration.
        """
        source = (_REPO_ROOT / path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path)
        assigned: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
                continue
            func = value.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "session_store_for_turn":
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            assigned.update(t.id for t in targets if isinstance(t, ast.Name))
        assert assigned, f"{path} assigns no resolved store"

        passed = {
            kw.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "memory_store" and isinstance(kw.value, ast.Name)
        }
        assert passed, f"{path} passes no memory_store= naming a local"
        assert passed <= assigned, (
            f"{path} passes memory_store={sorted(passed - assigned)}, which no "
            f"session_store_for_turn call produced"
        )
