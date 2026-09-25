"""The event loop must not queue behind bulk path resolution on ``mc-pathres``.

Field report (Amazon Linux 2, 0.6.0.16): eight of eight loop-stall crash dumps
showed the loop thread parked in ``Future.result`` inside
``is_sensitive_path`` -> ``_run_resolution_bounded``, reached from
``recycle_background -> acp_effective_model -> _resolve_named_agent_model ->
_read_agent_spec`` -- while two to four worker threads sat in
``skills._iter_skill_files`` submitting one resolution per directory and per
``SKILL.md`` to the same two-worker FIFO pool. No single wait crossed the 2 s
budget, so the stall breaker never tripped; the loop's waits simply queued
behind the scanner's backlog, and their sum crossed the watchdog. Queueing, not
a slow disk: ~1.4k sub-2 ms ``realpath`` calls per scan.

These tests pin the two halves of the fix:

- the skill walk asks the fence LEXICALLY against the ``realpath`` it already
  computed (``is_sensitive_resolved_path``), with the anchors resolved inline on
  its own thread, so it submits nothing to the pool on either half while the
  fence's decision -- same targets, same cache -- is unchanged;
- the named-agent model resolver reads the ``parsed_agent_specs`` snapshot, so a
  warm call on the loop re-parses (and re-resolves) nothing.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

import kiro_crew.agent_discovery as agent_discovery
import kiro_crew.skills as skills_mod
from kiro_crew import security
from kiro_crew.agent_discovery import clear_list_agents_cache
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skills import _iter_skill_files


def _refuse_candidate_resolution(expanded: str) -> set[str]:
    raise AssertionError(
        f"candidate resolution reached the mc-pathres pool for {expanded!r}; "
        "the pre-resolved gate must match lexically"
    )


class TestIsSensitiveResolvedPath:
    """The pre-resolved gate: same verdicts as ``is_sensitive_path``, no pool hop."""

    def test_duplicate_override_leaves_resolve_once_per_fresh_build(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
        roots = security.paths._resolve_root_anchors(str(tmp_path))
        calls = []
        generation = ["one"]

        def resolve(path):
            calls.append(path)
            return path + generation[0]

        monkeypatch.setattr(security.paths, "_realpath_or_none", resolve)
        leaves = [".kiro/crew/token_signing.key", ".kirocrew/token_signing.key"]
        first = security.paths._home_dir_targets_uncached(leaves, roots)
        target = os.path.join(roots.crew_home, "token_signing.key")
        assert calls.count(target) == 1
        generation[0] = "two"
        second = security.paths._home_dir_targets_uncached(leaves, roots)
        assert calls.count(target) == 2
        assert (target + "one").casefold() in first
        assert (target + "two").casefold() in second
        assert (target + "one").casefold() not in second

    def test_a_canonical_credential_path_is_still_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "_resolved_forms_bounded", _refuse_candidate_resolution)
        real = os.path.realpath(os.path.expanduser("~/.aws/credentials"))
        assert security.is_sensitive_resolved_path(real) is True

    def test_a_canonical_benign_path_passes(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(security, "_resolved_forms_bounded", _refuse_candidate_resolution)
        benign = tmp_path / "skills" / "tiny" / "SKILL.md"
        benign.parent.mkdir(parents=True)
        benign.write_text("---\nname: tiny\n---\n")
        assert security.is_sensitive_resolved_path(os.path.realpath(str(benign))) is False
        assert security.is_sensitive_resolved_path("") is False

    def test_the_keystone_publish_artifact_rule_still_applies(self, monkeypatch) -> None:
        # The second matcher of the fence is not skipped by the lexical path: the
        # temp beside a keystone leaf holds the leaf's payload.
        monkeypatch.setattr(security, "_resolved_forms_bounded", _refuse_candidate_resolution)
        parents = security.paths._home_dir_targets(security.paths._KEYSTONE_ARTIFACT_PARENTS)
        assert parents, "no keystone artifact parent resolved; the fixture home is wrong"
        artifact = os.path.join(sorted(parents)[0], "x.tmp")
        assert security.is_sensitive_resolved_path(artifact) is True

    def test_plain_skill_checks_fresh_sensitive_targets_without_artifact_lookup(
        self, tmp_path, monkeypatch
    ) -> None:
        calls = []
        original = security.paths._home_dir_targets

        def counted(home_dirs, **kwargs):
            calls.append(tuple(home_dirs))
            return original(home_dirs, **kwargs)

        monkeypatch.setattr(security.paths, "_home_dir_targets", counted)
        assert not security.is_sensitive_resolved_path(os.path.realpath(tmp_path / "SKILL.md"))
        assert calls == [tuple(security.paths._SENSITIVE_HOME_DIRS)]

    def test_the_plain_gate_still_resolves_its_candidate(self, tmp_path) -> None:
        # Control: the ordinary gate keeps submitting the candidate. If this
        # stopped holding, the test above would pass for the wrong reason.
        calls: list[str] = []
        real = security.paths._resolved_forms_bounded

        def recording(expanded: str) -> set[str]:
            calls.append(expanded)
            return real(expanded)

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(security, "_resolved_forms_bounded", recording)
            security.is_sensitive_path(str(tmp_path / "notes.md"))
        assert calls, "is_sensitive_path no longer resolves its candidate"

    def test_neither_half_reaches_the_pool(self, monkeypatch) -> None:
        # Not only the candidate: the anchors are resolved inline too, on a
        # cold target cache. One submission per call from a thousand-entry
        # walk is the queue the loop starved behind.
        def refuse(expanded, worker, **kwargs):
            raise AssertionError(
                f"_run_resolution_bounded reached the pool for {expanded!r} via {worker}"
            )

        monkeypatch.setattr(security.paths, "_run_resolution_bounded", refuse)
        monkeypatch.setattr(security.paths, "_home_targets_cache", {})
        real = os.path.realpath(os.path.expanduser("~/.aws/credentials"))
        assert security.is_sensitive_resolved_path(real) is True
        assert security.is_sensitive_resolved_path(os.path.realpath(os.sep)) is False

    def test_the_inline_anchors_are_the_bounded_anchors(self, monkeypatch) -> None:
        # Same targets from both routes, and the same cache key, so the
        # decision cannot drift between the loop's gate and the walker's.
        monkeypatch.setattr(security.paths, "_home_targets_cache", {})
        bounded = security.paths._home_dir_targets(security.paths._SENSITIVE_HOME_DIRS)
        monkeypatch.setattr(security.paths, "_home_targets_cache", {})
        inline = security.paths._home_dir_targets(security.paths._SENSITIVE_HOME_DIRS, inline=True)
        assert inline == bounded
        assert len(security.paths._home_targets_cache) == 1

    def test_the_gate_is_exported_on_the_facade(self) -> None:
        from kiro_crew.security import _exports

        assert "is_sensitive_resolved_path" in _exports.EXPORTED_NAMES
        assert security.is_sensitive_resolved_path is security.paths.is_sensitive_resolved_path

    def test_every_caller_of_the_gate_is_enumerated(self) -> None:
        # The gate's safety is a precondition on its argument (a ``realpath``
        # result, produced on the caller's own worker thread) that no code
        # enforces. So its callers are a reviewed list: a new call site fails
        # here until it is read for both halves of that contract and added.
        assert _gate_call_sites() == _EXPECTED_GATE_CALL_SITES


# path relative to ``src`` -> number of ``is_sensitive_resolved_path`` calls.
_EXPECTED_GATE_CALL_SITES: dict[str, int] = {
    # ``_iter_skill_files``: one check per directory (prune) and per SKILL.md
    # (skip), each against the ``realpath`` the walk computed for loop detection.
    "kiro_crew/skills.py": 2,
    # ``_complete_path_listing`` / ``_scan_completion_dir``: the project root, the
    # directory a `./` completion token named, and every entry that directory
    # offers. Both halves of the contract hold. The argument is canonical without a
    # resolve: the root is a ``realpath`` result, the token is walked one component
    # at a time with a no-follow open (so no component is a link), the directory is
    # then identified by ``pinned_fs.fd_real_path`` on the held descriptor rather
    # than by its typed name, and a link entry is never offered. And the calls run
    # on the dashboard's path-probe pool worker, never the event loop -- resolving
    # the candidate there is what this endpoint must not do at all, since on Windows
    # it would follow a junction aimed at a share.
    "kiro_crew/dashboard/handlers/files.py": 3,
    # ``security.is_sensitive_canonical_path``: the shared entry point for a
    # reader that canonicalised its path itself. It picks the gate by thread --
    # this pre-resolved gate off the event loop, the bounded gate on it -- so
    # only an offloaded caller reaches this call, and the caller's say-so is
    # never consulted. Its callers today: the artifact store's four file helpers
    # (``_read_text`` / ``_write_text`` / ``_read_bytes`` / ``_write_bytes``,
    # each handing it the ``os.path.realpath`` computed on the line above;
    # ``GET /api/artifacts`` runs ``store.list()`` on a worker for that reason,
    # since the listing reads one ``meta.json`` per artifact and the bounded
    # gate's two pool hops per call filled the pool from a single listing and
    # dropped healthy artifacts on the stall) and the two agent-spec readers
    # (``_read_agent_spec`` / ``read_agent_spec_strict``, each handing it the
    # ``Path.resolve(strict=True)`` result; the native skill projection reads
    # every spec under ``asyncio.to_thread``, and a stalled pool there dropped
    # agents silently and surfaced as ``no prepared skill discovery view``).
    # ``test_artifacts_pathres.py`` and ``test_agent_discovery_pathres.py`` pin
    # the canonical spelling and the thread split for each caller. The store's
    # root check and ``source_path`` pointers, and the reader module's project
    # root and cache key checks, stay on ``is_sensitive_path``: none of those
    # values is canonicalised first.
    "kiro_crew/security/paths.py": 1,
    # ``hooks._screen_and_resolve_held``: the BOUNDED arm of the Windows tail of
    # ``validate_file_path``, where the held walk covered only a prefix of the path.
    # Both halves of the contract hold. The argument is canonical without a resolve:
    # ``_canonicalize_within_hold`` runs ``realpath`` on the proven prefix and joins
    # the remainder as text, and that remainder holds no link for a resolution to
    # follow -- the walk classified every component it PROVED off that component's own
    # descriptor and would have reported a reparse point instead of this outcome, and a
    # name that holds nothing redirects nothing. And the bounded gate is the one thing
    # this arm must not use: it resolves its candidate again, which would send
    # ``realpath`` through the single component nothing is holding and re-open the
    # junction-swap window the bound exists to close. The call never runs on the event
    # loop, because ``validate_file_path`` is synchronous and every coroutine reaching
    # it hands it to a worker (``logo`` and ``api_skill_detail`` through
    # ``discovery_executor``, ``start_background`` through ``asyncio.to_thread``, and
    # ``_run_chat``'s two turn-flush exits through ``drained_to_thread``). The SETTLED
    # arm of the same function stays on ``is_sensitive_path``: the whole chain is held
    # there, so that gate's own resolution traverses nothing that can be swapped.
    "kiro_crew/hooks.py": 1,
}


def _gate_call_sites() -> dict[str, int]:
    import ast

    src = Path(skills_mod.__file__).resolve().parents[1]
    sites: dict[str, int] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else ""
            )
            handed_off = any(
                isinstance(arg, ast.Name) and arg.id == "is_sensitive_resolved_path"
                for arg in node.args
            )
            if name == "is_sensitive_resolved_path" or handed_off:
                rel = path.relative_to(src).as_posix()
                sites[rel] = sites.get(rel, 0) + 1
    return sites


class TestSkillWalkStaysOffThePool:
    """``_iter_skill_files`` submits nothing to ``mc-pathres``."""

    @staticmethod
    def _tree(root: Path, count: int) -> None:
        for i in range(count):
            d = root / f"pkg-{i}" / "skill"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: s{i}\n---\nbody\n")

    def test_the_walk_never_resolves_through_the_pool(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "skills"
        self._tree(root, 5)

        def refuse(expanded, worker, **kwargs):
            raise AssertionError(f"the skill walk reached mc-pathres for {expanded!r}")

        monkeypatch.setattr(security.paths, "_run_resolution_bounded", refuse)
        monkeypatch.setattr(security.paths, "_home_targets_cache", {})
        # The unresolved gate must not be reached from the walk at all either.
        monkeypatch.setattr(
            skills_mod,
            "is_sensitive_path",
            lambda *a, **k: pytest.fail("the skill walk called is_sensitive_path"),
        )
        found = _iter_skill_files(root)
        assert sorted(name for name, _ in found) == [f"pkg-{i}/skill" for i in range(5)]

    def test_the_walk_hands_the_gate_only_canonical_spellings(self, tmp_path, monkeypatch) -> None:
        # The pre-resolved gate's contract is that its input IS a realpath. Pin
        # that the walk honours it for every directory and every SKILL.md.
        root = tmp_path / "skills"
        self._tree(root, 3)
        seen: list[str] = []
        real_gate = skills_mod.is_sensitive_resolved_path

        def recording(resolved: str) -> bool:
            seen.append(resolved)
            return real_gate(resolved)

        monkeypatch.setattr(skills_mod, "is_sensitive_resolved_path", recording)
        _iter_skill_files(root)
        assert seen, "the walk asked the fence about nothing"
        not_canonical = [p for p in seen if p != os.path.realpath(p)]
        assert not_canonical == []
        # Directories AND files were vetted.
        assert any(p.endswith("SKILL.md") for p in seen)
        assert any(not p.endswith("SKILL.md") for p in seen)

    def test_a_fenced_directory_is_still_pruned_and_a_fenced_file_skipped(
        self, tmp_path, monkeypatch
    ) -> None:
        # The decision moved gates, not verdicts: a refusal from the pre-resolved
        # gate prunes the directory (nothing beneath it is enumerated) and drops
        # the file, exactly as the pool-backed gate did.
        root = tmp_path / "skills"
        self._tree(root, 3)
        fenced_dir = os.path.realpath(str(root / "pkg-1"))
        fenced_file = os.path.realpath(str(root / "pkg-2" / "skill" / "SKILL.md"))

        def gate(resolved: str) -> bool:
            return resolved in (fenced_dir, fenced_file)

        monkeypatch.setattr(skills_mod, "is_sensitive_resolved_path", gate)
        found = sorted(name for name, _ in _iter_skill_files(root))
        assert found == ["pkg-0/skill"]


@pytest.fixture
def _fresh_parsed_specs():
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


@pytest.fixture
def _discovery_pool(monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = []
        real_submit = pool.submit

        def submit(fn, *args, **kwargs):
            future = real_submit(fn, *args, **kwargs)
            futures.append(future)
            return future

        monkeypatch.setattr(pool, "submit", submit)
        monkeypatch.setattr(agent_discovery, "discovery_executor", lambda: pool)
        yield pool, futures


class _ReadCounter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.n = 0
        real = agent_discovery._read_agent_spec

        def counting(path, **kwargs):
            self.n += 1
            return real(path, **kwargs)

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", counting)


@pytest.mark.usefixtures("_fresh_parsed_specs")
class TestNamedAgentModelReadsTheSnapshot:
    """``_resolve_named_agent_model`` costs one ``scandir`` when warm."""

    def test_a_warm_call_parses_nothing(self, tmp_path, monkeypatch) -> None:
        for i in range(4):
            (tmp_path / f"a{i}.json").write_text(
                json.dumps({"name": f"agent-{i}", "model": f"m{i}"})
            )
        counter = _ReadCounter(monkeypatch)
        assert KiroCrewConfig._resolve_named_agent_model("agent-2", agents_dir=tmp_path) == "m2"
        cold = counter.n
        assert cold == 4  # one parse per spec, once
        assert KiroCrewConfig._resolve_named_agent_model("agent-3", agents_dir=tmp_path) == "m3"
        assert KiroCrewConfig._resolve_named_agent_model("agent-0", agents_dir=tmp_path) == "m0"
        assert counter.n == cold, "a warm resolve re-read the agents directory"

    def test_a_warm_call_submits_nothing_to_the_pool(self, tmp_path, monkeypatch) -> None:
        # The dumps' exact frame: the loop parked in the candidate resolution
        # under _read_agent_spec. Warm, that frame is never entered.
        (tmp_path / "bot.json").write_text(json.dumps({"name": "bot", "model": "m"}))
        assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == "m"
        monkeypatch.setattr(security, "_resolved_forms_bounded", _refuse_candidate_resolution)
        assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == "m"
        assert KiroCrewConfig._resolve_named_agent_model("nobody", agents_dir=tmp_path) == ""

    def test_a_changed_directory_is_seen(self, tmp_path) -> None:
        (tmp_path / "bot.json").write_text(json.dumps({"name": "bot", "model": "m"}))
        assert KiroCrewConfig._resolve_named_agent_model("late", agents_dir=tmp_path) == ""
        (tmp_path / "late.json").write_text(json.dumps({"name": "late", "model": "m-late"}))
        assert KiroCrewConfig._resolve_named_agent_model("late", agents_dir=tmp_path) == "m-late"

    @pytest.mark.parametrize("state", ["cold", "stale", "cleared"])
    def test_on_loop_refresh_parses_only_on_the_worker(
        self, tmp_path, monkeypatch, _discovery_pool, state
    ) -> None:
        spec = tmp_path / "bot.json"
        spec.write_text(json.dumps({"name": "bot", "model": "old"}))
        if state != "cold":
            assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == "old"
        previous_mtime = spec.stat().st_mtime_ns
        spec.write_text(json.dumps({"name": "bot", "model": "new"}))
        # No sleep or filesystem timestamp-resolution assumption.
        os.utime(spec, ns=(previous_mtime, previous_mtime + 1_000_000_000))
        if state == "cleared":
            clear_list_agents_cache()

        loop_thread = threading.current_thread()
        reads = []
        real_read = agent_discovery._read_agent_spec

        def recording_read(path, **kwargs):
            reads.append((threading.current_thread(), kwargs))
            assert threading.current_thread() is not loop_thread
            return real_read(path, **kwargs)

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", recording_read)

        async def resolve():
            return KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path)

        assert asyncio.run(resolve()) == ("old" if state == "stale" else "")
        _, futures = _discovery_pool
        assert len(futures) == 1
        futures[0].result(timeout=5)
        assert len(reads) == 1
        assert reads[0][0] is not loop_thread
        assert reads[0][1] == {"operation": "load_config", "source": "unknown"}
        assert asyncio.run(resolve()) == "new"
        assert len(futures) == 2
        futures[1].result(timeout=5)
        assert len(reads) == 1, "a warm revalidation parsed"
        assert str(tmp_path) not in agent_discovery._PARSED_SPECS_REFRESHING

    def test_on_loop_call_touches_no_filesystem(
        self, tmp_path, monkeypatch, _discovery_pool
    ) -> None:
        (tmp_path / "bot.json").write_text(json.dumps({"name": "bot", "model": "m"}))
        loop_thread = threading.current_thread()
        signatures = []
        reads = []
        real_signature = agent_discovery._dir_signature
        real_read = agent_discovery._read_agent_spec

        def recording_signature(directory):
            signatures.append(threading.current_thread())
            assert threading.current_thread() is not loop_thread
            return real_signature(directory)

        def recording_read(path, **kwargs):
            reads.append(threading.current_thread())
            assert threading.current_thread() is not loop_thread
            return real_read(path, **kwargs)

        monkeypatch.setattr(agent_discovery, "_dir_signature", recording_signature)
        monkeypatch.setattr(agent_discovery, "_read_agent_spec", recording_read)

        async def resolve():
            return KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path)

        _, futures = _discovery_pool
        cold = asyncio.run(resolve())
        assert len(futures) == 1
        futures[0].result(timeout=5)
        warm = asyncio.run(resolve())
        assert len(futures) == 2
        futures[1].result(timeout=5)
        assert cold == ""
        assert warm == "m"
        assert signatures, "no directory signature was revalidated"
        assert len(reads) == 1, "a warm revalidation parsed"
        assert all(thread is not loop_thread for thread in signatures + reads)
        assert str(tmp_path) not in agent_discovery._PARSED_SPECS_REFRESHING

    def test_on_loop_cold_calls_deduplicate_the_refresh(
        self, tmp_path, monkeypatch, _discovery_pool
    ) -> None:
        (tmp_path / "bot.json").write_text(json.dumps({"name": "bot", "model": "m"}))
        queued = Mock()
        monkeypatch.setattr(agent_discovery, "discovery_executor", lambda: queued)
        counter = _ReadCounter(monkeypatch)

        async def resolve_twice():
            assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == ""
            assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == ""
            clear_list_agents_cache()
            assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == ""

        try:
            asyncio.run(resolve_twice())
            assert queued.submit.call_count == 1
            assert counter.n == 0
        finally:
            # Drain even if an assertion fails; never leave an in-flight mark.
            pool, _ = _discovery_pool
            for call in queued.submit.call_args_list:
                pool.submit(*call.args, **call.kwargs).result(timeout=5)
        assert str(tmp_path) not in agent_discovery._PARSED_SPECS_REFRESHING
        assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == "m"

    def test_failed_refresh_releases_the_inflight_mark(
        self, tmp_path, monkeypatch, caplog, _discovery_pool
    ) -> None:
        def fail(*args, **kwargs):
            raise RuntimeError("snapshot unavailable")

        monkeypatch.setattr(agent_discovery, "parsed_agent_specs", fail)

        async def lookup():
            return agent_discovery.cached_agent_specs(
                tmp_path, operation="load_config", source="unknown"
            )

        _, futures = _discovery_pool
        for attempt in range(2):
            with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_discovery"):
                assert asyncio.run(lookup()) == []
                assert len(futures) == attempt + 1
                # The worker swallows the failure so the fire-and-forget future
                # never carries an unretrieved exception -- and logs it, since
                # nothing else would surface a persistent parse failure.
                assert futures[-1].result(timeout=5) is None
            assert "snapshot refresh failed" in caplog.text
            assert "snapshot unavailable" in caplog.text
            assert str(tmp_path) not in agent_discovery._PARSED_SPECS_REFRESHING

    def test_shutdown_executor_releases_the_inflight_mark(self, tmp_path, monkeypatch) -> None:
        stopped = Mock()
        stopped.submit.side_effect = RuntimeError("cannot schedule after shutdown")
        monkeypatch.setattr(agent_discovery, "discovery_executor", lambda: stopped)

        async def lookup():
            return agent_discovery.cached_agent_specs(
                tmp_path, operation="load_config", source="unknown"
            )

        for _ in range(2):
            assert asyncio.run(lookup()) == []
            assert str(tmp_path) not in agent_discovery._PARSED_SPECS_REFRESHING
        assert stopped.submit.call_count == 2

    def test_json_still_outranks_a_markdown_spec_of_another_stem(self, tmp_path) -> None:
        # The unordered first-match scan listed JSON entries first; the snapshot
        # is filename-sorted, so the precedence is restated explicitly.
        (tmp_path / "aaa.md").write_text("---\nname: shared\nmodel: from-md\n---\nprompt\n")
        (tmp_path / "zzz.json").write_text(json.dumps({"name": "shared", "model": "from-json"}))
        assert (
            KiroCrewConfig._resolve_named_agent_model("shared", agents_dir=tmp_path) == "from-json"
        )

    def test_a_snapshot_failure_is_no_pin_not_an_exception(self, tmp_path, monkeypatch) -> None:
        def boom(*a, **k):
            raise RuntimeError("snapshot unavailable")

        monkeypatch.setattr(agent_discovery, "parsed_agent_specs", boom)
        assert KiroCrewConfig._resolve_named_agent_model("bot", agents_dir=tmp_path) == ""


# ---------------------------------------------------------------------------
# The folder project-dir validator stays off the event loop
# ---------------------------------------------------------------------------

#: Names that reach ``_validate_project_dir`` without an offload of their own, so
#: an ``async def`` frame calling any of them inline puts ``realpath`` + ``isdir``
#: + the sensitive-path scan on the loop. The two helpers are listed BY NAME
#: rather than left to the validator's own name: each calls the validator inline
#: in a synchronous frame, which is correct there because every caller hands the
#: helper to a worker, and a gate that watched only the validator would miss an
#: async frame that reached it through one of them instead.
_ON_LOOP_BANNED_PROJECT_DIR_NAMES = frozenset(
    {
        "_validate_project_dir",
        "_resolve_folder_project_dir",
        "_resolve_root",
    }
)

#: Modules owning an ``async def`` that may touch folder project directories.
_PROJECT_DIR_LOOP_MODULES = (
    "kiro_crew/dashboard/chat_folders.py",
    "kiro_crew/dashboard/chat_folder_scaffold.py",
    "kiro_crew/dashboard/chat_handlers.py",
)

_PROJECT_DIR_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _inline_project_dir_calls_in_async_frames(source: str, label: str) -> list[str]:
    """Offenders: a banned name CALLED inside an ``async def``'s own frame.

    Handing the name to ``asyncio.to_thread`` passes it as a bare ``Name``, not
    a ``Call``, so the offloaded form is invisible here and needs no exemption.
    A nested ``def`` or ``lambda`` is a separate frame -- that is the offloaded
    callable itself -- and is not scanned, matching the scoping the apps-dir and
    registry ratchets already use.
    """
    offenders: list[str] = []

    def _scan(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _PROJECT_DIR_NESTED_SCOPES):
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in _ON_LOOP_BANNED_PROJECT_DIR_NAMES:
                    offenders.append(f"{label}:{owner}:{child.lineno} calls {name}()")
            _scan(child, owner)

    for node in ast.walk(ast.parse(source, label)):
        if isinstance(node, ast.AsyncFunctionDef):
            for stmt in node.body:
                _scan(stmt, node.name)
    return offenders


class TestProjectDirValidationStaysOffTheLoop:
    """``_validate_project_dir`` is never called synchronously on the loop.

    The validator's own contract requires it: ``realpath`` + ``isdir`` + the
    sensitive-path scan touch the filesystem, so an event-loop caller must hand
    it to ``asyncio.to_thread``. This pins the INVARIANT over every ``async def``
    in the owning modules rather than the wrapping of one call site, because the
    offending call and the compliant one sit in the same file a few hundred lines
    apart and a per-site assertion cannot see the next one.
    """

    def test_no_inline_project_dir_validation_in_async_frames(self) -> None:
        src = Path(skills_mod.__file__).resolve().parents[1]
        offenders: list[str] = []
        scanned = 0
        for rel in _PROJECT_DIR_LOOP_MODULES:
            path = src / rel
            assert path.is_file(), f"ratchet names a module that does not exist: {rel}"
            scanned += 1
            offenders += _inline_project_dir_calls_in_async_frames(
                path.read_text(encoding="utf-8"), rel
            )
        assert scanned == len(_PROJECT_DIR_LOOP_MODULES)
        assert not offenders, "folder project-dir validation on the event loop:\n" + "\n".join(
            offenders
        )

    def test_the_ratchet_flags_an_inline_call(self) -> None:
        """Non-vacuity: the detector reports the shape the fix removed."""
        offenders = _inline_project_dir_calls_in_async_frames(
            "async def api_chat_folder_update(request):\n"
            "    pd, err = _validate_project_dir(raw)\n",
            "probe.py",
        )
        assert len(offenders) == 1
        assert "calls _validate_project_dir()" in offenders[0]

    def test_the_ratchet_flags_a_helper_that_reaches_the_validator(self) -> None:
        """A banned helper called inline is an offender too, not just the validator."""
        offenders = _inline_project_dir_calls_in_async_frames(
            "async def handler(request):\n    return _resolve_folder_project_dir(snap, fid)\n",
            "probe.py",
        )
        assert len(offenders) == 1
        assert "calls _resolve_folder_project_dir()" in offenders[0]

    def test_the_ratchet_allows_the_offloaded_form(self) -> None:
        """Control: the compliant spelling is not an offender."""
        assert (
            _inline_project_dir_calls_in_async_frames(
                "async def api_chat_folder_update(request):\n"
                "    pd, err = await asyncio.to_thread(_validate_project_dir, raw)\n",
                "probe.py",
            )
            == []
        )

    def test_the_ratchet_allows_a_synchronous_frame(self) -> None:
        """Control: the two helpers' own inline calls are correct where they are."""
        assert (
            _inline_project_dir_calls_in_async_frames(
                "def _resolve_root(body):\n    return _validate_project_dir(raw)\n",
                "probe.py",
            )
            == []
        )
