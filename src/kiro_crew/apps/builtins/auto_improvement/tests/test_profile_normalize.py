"""Profiler artifacts → the one frame-tree shape the flame/sunburst views render.

The output schema is a CONTRACT: one set of frontend components draws either a
Python ``cProfile`` dump or a V8 ``.cpuprofile``, so a field that changes name or a
time that lands on the wrong frame is a rendering bug with no error to point at.
These tests pin the schema, the two per-format time attributions, and the hot-frame
rule — including the root-exclusion carve-out, which is the part that silently
degrades into "always highlight the entry point" if it regresses.
"""

from __future__ import annotations

import cProfile
import gc
import json
import pstats
import unittest.mock as mock
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import profile_normalize as pn
from kiro_crew.apps.builtins.auto_improvement.backend import store


@pytest.fixture()
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app's data root at a tmp dir."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr(store, "data_dir", lambda: tmp_path / "data")
    # Pin workspace_dir == data_dir so profiles_dir() and flat test paths coincide
    # (see the note in test_finding_detail's fixture).
    monkeypatch.setattr(store, "workspace_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store.ensure_layout()
    return tmp_path / "data"


# ── fixtures: real profiler artifacts, not hand-written ones ──────────────────


def _busy() -> None:
    total = 0
    for i in range(20000):
        total += i * i


def _idle() -> None:
    pass


def _entry() -> None:
    """A caller with negligible self time and two callees of unequal cost."""
    _busy()
    _idle()


def _real_pstats(path: Path) -> Path:
    """Produce a genuine ``.pstats`` dump with ``cProfile``.

    A real dump rather than a fabricated ``Stats`` object, because the tuple layout
    and the callers index are exactly what the normalizer walks — a hand-built stand-in
    would let a misread of that layout pass.
    """
    prof = cProfile.Profile()
    prof.enable()
    _entry()
    prof.disable()
    prof.dump_stats(str(path))
    return path


def _cpuprofile(**overrides: Any) -> dict[str, Any]:
    """A minimal V8 CPU profile: root → hot, with root also calling cold.

    Sample stream is deliberately uneven so a mis-indexed ``timeDeltas`` read lands
    the time on the wrong frame instead of merely rounding differently.
    """
    profile = {
        "nodes": [
            {
                "id": 1,
                "callFrame": {"functionName": "(root)", "url": ""},
                "hitCount": 0,
                "children": [2, 3],
            },
            {
                "id": 2,
                "callFrame": {"functionName": "hot", "url": "file:///app/src/hot.js"},
                "hitCount": 3,
                "children": [],
            },
            {
                "id": 3,
                "callFrame": {"functionName": "", "url": "file:///app/src/cold.js"},
                "hitCount": 1,
                "children": [],
            },
        ],
        # deltas[0] is the pre-first-sample startup offset and belongs to no frame.
        "samples": [2, 2, 2, 3],
        "timeDeltas": [5000, 1000, 2000, 3000, 4000],
        "startTime": 0,
        "endTime": 15000,
    }
    profile.update(overrides)
    return profile


class TestPstatsNormalization:
    def test_schema_is_the_frontend_contract(self, tmp_path: Path) -> None:
        """Every key the viewer reads must be present, on the tree and on each frame."""
        tree = pn.normalize_pstats(_real_pstats(tmp_path / "run.pstats"))
        assert tree is not None
        assert tree["unit"] == "ms"
        assert tree["kind"] == "cpu"
        assert tree["scenario"] == "run"
        assert isinstance(tree["total"], float)
        assert isinstance(tree["highlight"], list)

        seen: list[dict[str, Any]] = []

        def walk(node: dict[str, Any]) -> None:
            seen.append(node)
            for child in node["children"]:
                walk(child)

        walk(tree["root"])
        for node in seen:
            assert set(node) == {"name", "module", "self", "total", "calls", "children", "hot"}
            assert isinstance(node["name"], str)
            assert isinstance(node["hot"], bool)

    def test_times_are_milliseconds_matching_the_raw_stats(self, tmp_path: Path) -> None:
        """``pstats`` reports seconds; the tree declares ms. A missing conversion
        renders a 40 ms profile as a 0.04 ms one, which looks like a fast profile
        rather than a bug."""
        path = _real_pstats(tmp_path / "run.pstats")
        raw = pstats.Stats(str(path)).stats  # type: ignore[attr-defined]
        busy = next(f for f in raw if f[2] == "_busy")
        tree = pn.normalize_pstats(path)
        assert tree is not None

        found = _find(tree["root"], "_busy")
        assert found is not None
        assert found["self"] == pytest.approx(raw[busy][2] * 1000.0, abs=0.01)
        assert found["total"] == pytest.approx(raw[busy][3] * 1000.0, abs=0.01)

    def test_callee_edges_become_children(self, tmp_path: Path) -> None:
        """The dump is a call GRAPH keyed by callers; the tree must be walked out of
        it, or every frame renders as a sibling of the root."""
        tree = pn.normalize_pstats(_real_pstats(tmp_path / "run.pstats"))
        assert tree is not None
        entry = _find(tree["root"], "_entry")
        assert entry is not None
        assert "_busy" in [c["name"] for c in entry["children"]]

    def test_multiple_roots_are_wrapped_in_one_synthetic_root(self, tmp_path: Path) -> None:
        """The schema has ONE root, but a profile can have several independent entry
        points. Dropping the extras would silently hide whole subtrees."""
        prof = cProfile.Profile()
        prof.enable()
        _busy()
        prof.disable()
        prof2 = cProfile.Profile()
        prof2.enable()
        _idle()
        prof2.disable()
        path = tmp_path / "multi.pstats"
        merged = pstats.Stats(prof)
        merged.add(prof2)
        merged.dump_stats(str(path))

        tree = pn.normalize_pstats(path)
        assert tree is not None
        root = tree["root"]
        if root["name"] == "(roots)":
            assert len(root["children"]) > 1
            assert root["module"] == ""
            # The aggregate must account for its children, not report zero.
            assert root["total"] == pytest.approx(
                sum(c["total"] for c in root["children"]), abs=0.1
            )
        else:  # a single-root profile is passed through unwrapped
            assert root["children"] is not None

    def test_roots_are_ordered_by_cumulative_time(self, tmp_path: Path) -> None:
        """The dominant subtree must render first; the viewer opens on child zero."""
        path = _real_pstats(tmp_path / "run.pstats")
        tree = pn.normalize_pstats(path)
        assert tree is not None
        root = tree["root"]
        if root["name"] == "(roots)":
            totals = [c["total"] for c in root["children"]]
            assert totals == sorted(totals, reverse=True)

    def test_recursion_terminates(self, tmp_path: Path) -> None:
        """A recursive function is its OWN caller in the dump, so the call graph has a
        self-edge and the walk never returns without the cycle break.

        Called through a wrapper on purpose: a directly-profiled recursive function has
        no in-profile caller other than itself, so it is not a root and never gets
        walked at all — the cyclic edge would go untested."""

        def fib(n: int) -> int:
            return n if n < 2 else fib(n - 1) + fib(n - 2)

        def entry() -> None:
            fib(12)

        prof = cProfile.Profile()
        # GC is held off for the profiled region, because a collection that fires
        # while `fib` is on the stack attributes the collected objects' weakref
        # /  __del__ callbacks to `fib` as CALLEES. Those frames are real -- the
        # profiler saw them -- but they have nothing to do with recursion, and
        # their appearance is timing- and memory-pressure-dependent: this test
        # passed alone and failed inside a 4-way xdist shard, where the other
        # workers' allocation churn made a mid-`fib` collection likely (a
        # `weakref`-module frame showed up as a child of `fib` with calls=4).
        #
        # Disabling GC removes the nondeterminism at its source rather than
        # teaching the assertion to tolerate it.
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            prof.enable()
            entry()
            prof.disable()
        finally:
            if gc_was_enabled:
                gc.enable()
        path = tmp_path / "rec.pstats"
        prof.dump_stats(str(path))

        raw = pstats.Stats(str(path)).stats  # type: ignore[attr-defined]
        fib_func = next(f for f in raw if f[2] == "fib")
        assert fib_func in raw[fib_func][4], "fib must be its own caller for this to test anything"

        tree = pn.normalize_pstats(path)
        assert tree is not None
        walked = _find(tree["root"], "fib")
        assert walked is not None
        # The property under test is that the SELF-EDGE is cut, not that `fib`
        # happens to have no callees at all. Asserting `children == []` conflated
        # the two, so any incidental frame the profiler attributed to `fib` failed
        # a test about recursion.
        assert "fib" not in [c["name"] for c in walked["children"]]
        assert _depth(tree["root"]) < 50  # bounded, not merely finite

    def test_corrupt_and_missing_files_return_none(self, tmp_path: Path) -> None:
        """A torn artifact must degrade one panel, not raise into the request."""
        bad = tmp_path / "bad.pstats"
        bad.write_bytes(b"not a marshalled stats object")
        assert pn.normalize_pstats(bad) is None
        assert pn.normalize_pstats(tmp_path / "absent.pstats") is None

    def test_a_single_root_profile_is_not_wrapped(self, tmp_path: Path) -> None:
        """One entry point means one root, and the synthetic ``(roots)`` aggregate must
        NOT appear — an extra frame at the top shifts every depth in the flame graph.

        A synthetic stats mapping because ``cProfile`` always leaves its own
        ``Profiler.disable`` as a second root, so a real dump cannot produce this case.
        """
        entry = ("/app/src/main.py", 10, "entry")
        leaf = ("/app/src/work.py", 20, "work")
        stats = {
            entry: (1, 1, 0.001, 0.100, {}),
            leaf: (1, 1, 0.099, 0.099, {entry: (1, 1, 0.099, 0.099)}),
        }

        class _Fake:
            pass

        fake = _Fake()
        fake.stats = stats  # type: ignore[attr-defined]
        path = tmp_path / "single.pstats"
        path.write_bytes(b"")
        with mock.patch.object(pstats, "Stats", lambda *_a, **_k: fake):
            tree = pn.normalize_pstats(path)
        assert tree is not None
        assert tree["root"]["name"] == "entry"
        assert tree["root"]["module"] == "main.py"
        assert [c["name"] for c in tree["root"]["children"]] == ["work"]
        assert tree["total"] == pytest.approx(100.0)
        assert tree["highlight"] == ["work"]

    def test_a_dump_with_no_entries_returns_none(self, tmp_path: Path) -> None:
        """A profile that recorded nothing has no frames to draw, so there is no tree —
        an empty one would render as a blank flame graph with no explanation."""
        path = tmp_path / "empty.pstats"
        path.write_bytes(b"")

        class _Empty:
            stats: dict[Any, Any] = {}

        with mock.patch.object(pstats, "Stats", lambda *_a, **_k: _Empty()):
            assert pn.normalize_pstats(path) is None


class TestCpuprofileNormalization:
    def test_self_time_uses_the_following_delta(self, tmp_path: Path) -> None:
        """V8's ``timeDeltas[i]`` is the interval ENDING at ``samples[i]``, so a
        sample's own time is ``deltas[i + 1]`` and ``deltas[0]`` is a startup offset
        belonging to no frame. Reading ``deltas[i]`` instead shifts every frame's cost
        onto whatever ran before it — here it would credit ``hot`` with 8 ms and
        ``cold`` with 2 ms instead of 6 ms and 4 ms."""
        path = tmp_path / "p.cpuprofile"
        path.write_text(json.dumps(_cpuprofile()), encoding="utf-8")
        tree = pn.normalize_cpuprofile(path)
        assert tree is not None

        hot = _find(tree["root"], "hot")
        cold = _find(tree["root"], "(anonymous)")
        assert hot is not None and cold is not None
        # samples[0..2] = node 2 -> deltas[1..3] = 1000 + 2000 + 3000 us
        assert hot["self"] == pytest.approx(6.0)
        # samples[3] = node 3 -> deltas[4] = 4000 us
        assert cold["self"] == pytest.approx(4.0)
        # The 5000 us startup offset is attributed to nothing.
        assert tree["total"] == pytest.approx(10.0)

    def test_total_is_self_plus_children(self, tmp_path: Path) -> None:
        path = tmp_path / "p.cpuprofile"
        path.write_text(json.dumps(_cpuprofile()), encoding="utf-8")
        tree = pn.normalize_cpuprofile(path)
        assert tree is not None
        root = tree["root"]
        assert root["self"] == pytest.approx(0.0)
        assert root["total"] == pytest.approx(sum(c["total"] for c in root["children"]))

    def test_anonymous_functions_and_module_basenames(self, tmp_path: Path) -> None:
        """An empty ``functionName`` must still label; a full script URL must reduce to
        a basename or the flame-graph label is unreadable."""
        path = tmp_path / "p.cpuprofile"
        path.write_text(json.dumps(_cpuprofile()), encoding="utf-8")
        tree = pn.normalize_cpuprofile(path)
        assert tree is not None
        hot = _find(tree["root"], "hot")
        assert hot is not None and hot["module"] == "hot.js"
        assert _find(tree["root"], "(anonymous)") is not None

    def test_hit_count_becomes_calls(self, tmp_path: Path) -> None:
        path = tmp_path / "p.cpuprofile"
        path.write_text(json.dumps(_cpuprofile()), encoding="utf-8")
        tree = pn.normalize_cpuprofile(path)
        assert tree is not None
        hot = _find(tree["root"], "hot")
        assert hot is not None and hot["calls"] == 3

    def test_cyclic_child_references_terminate(self) -> None:
        """A child pointing back at an ancestor is malformed but reachable from a
        truncated capture; it must not recurse forever."""
        profile = _cpuprofile()
        profile["nodes"][1]["children"] = [1]  # hot -> (root)
        tree = pn._normalize_cpuprofile_obj(profile, scenario="cyclic")
        assert tree is not None
        assert _depth(tree["root"]) < 50

    def test_dangling_child_ids_are_skipped(self) -> None:
        """A partial capture can reference a node it never wrote."""
        profile = _cpuprofile()
        profile["nodes"][0]["children"] = [2, 3, 99]
        tree = pn._normalize_cpuprofile_obj(profile, scenario="partial")
        assert tree is not None
        assert len(tree["root"]["children"]) == 2

    def test_empty_and_unparseable_profiles_return_none(self, tmp_path: Path) -> None:
        assert pn._normalize_cpuprofile_obj({"nodes": []}, scenario="x") is None
        assert pn._normalize_cpuprofile_obj({}, scenario="x") is None
        torn = tmp_path / "torn.cpuprofile"
        torn.write_text('{"nodes": [', encoding="utf-8")
        assert pn.normalize_cpuprofile(torn) is None
        assert pn.normalize_cpuprofile(tmp_path / "absent.cpuprofile") is None

    def test_a_node_list_with_no_usable_root_returns_none(self) -> None:
        """The root is ``nodes[0]``; a capture whose first node has no id cannot be
        walked, and an empty tree would render as a blank flame graph with no error."""
        profile = _cpuprofile()
        del profile["nodes"][0]["id"]
        assert pn._normalize_cpuprofile_obj(profile, scenario="rootless") is None

    def test_a_sample_referencing_an_unknown_node_is_ignored(self) -> None:
        """A truncated capture can sample a node it never wrote out. Its time must not
        be credited to a frame that does exist."""
        profile = _cpuprofile(samples=[2, 99], timeDeltas=[5000, 1000, 2000])
        tree = pn._normalize_cpuprofile_obj(profile, scenario="dangling")
        assert tree is not None
        hot = _find(tree["root"], "hot")
        assert hot is not None and hot["self"] == pytest.approx(1.0)
        assert tree["total"] == pytest.approx(1.0)  # the unknown node's 2 ms is dropped

    def test_a_node_without_an_id_is_skipped(self) -> None:
        """Keying the index on ``n["id"]`` directly raised a KeyError on a truncated
        capture — before the no-root guard could fire, making it unreachable."""
        profile = _cpuprofile()
        profile["nodes"].append({"callFrame": {"functionName": "orphan"}, "children": []})
        tree = pn._normalize_cpuprofile_obj(profile, scenario="orphan")
        assert tree is not None
        assert _find(tree["root"], "orphan") is None

    def test_no_samples_still_yields_a_tree(self) -> None:
        """A capture that started and stopped without sampling has a real node list;
        an all-zero tree is a valid answer and must not become None."""
        profile = _cpuprofile(samples=[], timeDeltas=[])
        tree = pn._normalize_cpuprofile_obj(profile, scenario="empty")
        assert tree is not None
        assert tree["total"] == pytest.approx(0.0)
        assert tree["highlight"] == []


class TestHotFrames:
    def _tree(self, selves: list[float], total: float) -> dict[str, Any]:
        """A root plus one child per self time in ``selves``."""
        return {
            "unit": "ms",
            "total": total,
            "kind": "cpu",
            "scenario": "s",
            "root": {
                "name": "root",
                "module": "",
                "self": selves[0],
                "total": total,
                "calls": 1,
                "hot": False,
                "children": [
                    {
                        "name": f"f{i}",
                        "module": "",
                        "self": s,
                        "total": s,
                        "calls": 1,
                        "hot": False,
                        "children": [],
                    }
                    for i, s in enumerate(selves[1:])
                ],
            },
            "highlight": [],
        }

    def test_hottest_frame_is_always_hot_even_below_threshold(self) -> None:
        """A flat profile has no frame over 20%, but the viewer still needs somewhere
        to open — otherwise ``highlight`` is empty and nothing is pre-focused."""
        tree = self._tree([0.0, 3.0, 2.5, 2.5, 2.0], total=100.0)
        pn._populate_hot(tree)
        assert tree["highlight"] == ["f0"]

    def test_frames_over_the_fraction_are_all_hot(self) -> None:
        tree = self._tree([0.0, 50.0, 30.0, 5.0], total=100.0)
        pn._populate_hot(tree)
        assert tree["highlight"] == ["f0", "f1"]
        assert _find(tree["root"], "f2")["hot"] is False  # type: ignore[index]

    def test_root_never_wins_the_always_hot_pick(self) -> None:
        """ROOT EXCLUSION: the root is the scenario entry point. When its self time
        merely edges out the real hot leaf, picking the overall maximum pre-focuses a
        frame that explains nothing — so the always-hot pick skips the root. The root
        is 6.0 here and the leaf 5.0, both under the 20 ms threshold, so only the
        always-hot rule can fire."""
        tree = self._tree([6.0, 5.0, 1.0], total=100.0)
        pn._populate_hot(tree)
        assert tree["highlight"] == ["f0"]
        assert tree["root"]["hot"] is False

    def test_root_over_the_threshold_is_still_co_highlighted(self) -> None:
        """Root exclusion applies only to the always-hot pick. A root that
        independently clears the fraction is genuinely expensive and must show."""
        tree = self._tree([40.0, 30.0, 1.0], total=100.0)
        pn._populate_hot(tree)
        assert tree["highlight"] == ["root", "f0"]

    def test_root_only_profile_falls_back_to_the_root(self) -> None:
        """The one case where the root legitimately is the hottest frame: nothing
        below it spent any time."""
        tree = self._tree([7.0, 0.0, 0.0], total=100.0)
        pn._populate_hot(tree)
        assert tree["highlight"] == ["root"]
        assert tree["root"]["hot"] is True

    def test_all_zero_profile_has_no_hot_frame(self) -> None:
        """Nothing measured means nothing to highlight — the root must not win the
        zero tie and present itself as the hot spot."""
        tree = self._tree([0.0, 0.0, 0.0], total=0.0)
        pn._populate_hot(tree)
        assert tree["highlight"] == []
        assert all(not f["hot"] for f in [tree["root"], *tree["root"]["children"]])

    def test_duplicate_frame_names_appear_once_in_highlight(self) -> None:
        tree = self._tree([0.0, 50.0, 30.0], total=100.0)
        tree["root"]["children"][1]["name"] = "f0"
        pn._populate_hot(tree)
        assert tree["highlight"] == ["f0"]

    def test_hot_is_populated_by_both_normalizers(self, tmp_path: Path) -> None:
        """Callers read ``highlight`` regardless of the source format."""
        tree = pn.normalize_pstats(_real_pstats(tmp_path / "run.pstats"))
        assert tree is not None and tree["highlight"]
        path = tmp_path / "p.cpuprofile"
        path.write_text(json.dumps(_cpuprofile()), encoding="utf-8")
        cpu = pn.normalize_cpuprofile(path)
        assert cpu is not None and cpu["highlight"] == ["hot", "(anonymous)"]


class TestCapture:
    def test_capture_pstats_writes_the_tree(self, tmp_path: Path) -> None:
        out = tmp_path / "out" / "fp.json"
        tree = pn.capture_pstats(_real_pstats(tmp_path / "run.pstats"), out)
        assert tree is not None
        assert json.loads(out.read_text(encoding="utf-8"))["root"]["name"] == tree["root"]["name"]

    def test_capture_overrides_the_scenario_label(self, tmp_path: Path) -> None:
        """Without the override every profile is titled with a fingerprint hash."""
        out = tmp_path / "fp.json"
        tree = pn.capture_pstats(
            _real_pstats(tmp_path / "run.pstats"), out, scenario="search depth 6"
        )
        assert tree is not None and tree["scenario"] == "search depth 6"
        assert json.loads(out.read_text(encoding="utf-8"))["scenario"] == "search depth 6"

    def test_capture_cpuprofile_accepts_an_object_or_a_path(self, tmp_path: Path) -> None:
        """A harness holding the parsed profile should not have to write a file the
        normalizer would immediately re-read."""
        out_obj = tmp_path / "obj.json"
        assert pn.capture_cpuprofile(_cpuprofile(), out_obj, scenario="from-object") is not None
        assert json.loads(out_obj.read_text(encoding="utf-8"))["scenario"] == "from-object"

        src = tmp_path / "p.cpuprofile"
        src.write_text(json.dumps(_cpuprofile()), encoding="utf-8")
        out_path = tmp_path / "path.json"
        assert pn.capture_cpuprofile(src, out_path, scenario="from-path") is not None
        assert json.loads(out_path.read_text(encoding="utf-8"))["scenario"] == "from-path"

    def test_capture_failure_writes_nothing(self, tmp_path: Path) -> None:
        """A failed normalize must not leave a half-written tree the reader would
        prefer over the raw artifact."""
        bad = tmp_path / "bad.pstats"
        bad.write_bytes(b"garbage")
        out = tmp_path / "fp.json"
        assert pn.capture_pstats(bad, out) is None
        assert not out.exists()
        assert pn.capture_cpuprofile({"nodes": []}, out) is None
        assert not out.exists()

    def test_capture_profile_dispatches_and_lands_in_profiles_dir(self, data_home: Path) -> None:
        pstats_path = _real_pstats(data_home / "run.pstats")
        assert pn.capture_profile("a" * 16, pstats_path) is not None
        assert (store.profiles_dir() / f"{'a' * 16}.json").is_file()

        assert pn.capture_profile("b" * 16, _cpuprofile()) is not None
        written = json.loads((store.profiles_dir() / f"{'b' * 16}.json").read_text("utf-8"))
        assert written["scenario"] == "b" * 16

        cpu_path = data_home / "p.cpuprofile"
        cpu_path.write_text(json.dumps(_cpuprofile()), encoding="utf-8")
        assert pn.capture_profile("c" * 16, cpu_path) is not None
        assert (store.profiles_dir() / f"{'c' * 16}.json").is_file()

    def test_capture_profile_rejects_unknown_suffixes(self, data_home: Path) -> None:
        """An unrecognized artifact is not silently written as an empty tree."""
        art = data_home / "trace.nflx"
        art.write_text("{}", encoding="utf-8")
        assert pn.capture_profile("d" * 16, art) is None
        assert not (store.profiles_dir() / f"{'d' * 16}.json").exists()

    def test_capture_profile_refuses_a_traversing_fingerprint(self, data_home: Path) -> None:
        """``fp`` reaches this from a URL path segment and names a file that is
        written; it must never be able to escape ``profiles/``."""
        pstats_path = _real_pstats(data_home / "run.pstats")
        assert pn.capture_profile("../../escaped", pstats_path) is None
        assert not (data_home.parent / "escaped.json").exists()
        assert list(store.profiles_dir().glob("*.json")) == []

    def test_capture_cpuprofile_survives_a_malformed_object(self, tmp_path: Path) -> None:
        """A harness can hand over something that is not a profile object at all — a
        bare JSON array, a half-built dict. That must fail the capture, not raise into
        the run that was being profiled."""
        out = tmp_path / "fp.json"
        assert pn.capture_cpuprofile({"nodes": [{"no_id": True}]}, out) is None
        assert pn.capture_cpuprofile([1, 2, 3], out) is None  # type: ignore[arg-type]
        assert not out.exists()


class TestReads:
    def test_read_profile_serves_the_captured_tree(self, data_home: Path) -> None:
        fp = "e" * 16
        pn.capture_profile(fp, _real_pstats(data_home / "run.pstats"), scenario="warm")
        tree = pn.read_profile(fp)
        assert tree is not None and tree["scenario"] == "warm"

    def test_read_profile_falls_back_to_raw_artifacts(self, data_home: Path) -> None:
        """A capture taken before normalization existed, or one whose normalize
        failed, must still render — through the same normalizer, so ``hot`` is
        populated on the fallback path too."""
        fp = "f" * 16
        _real_pstats(store.profiles_dir() / f"{fp}.pstats")
        tree = pn.read_profile(fp)
        assert tree is not None and tree["highlight"]

        fp2 = "0" * 16
        (store.profiles_dir() / f"{fp2}.cpuprofile").write_text(
            json.dumps(_cpuprofile()), encoding="utf-8"
        )
        tree2 = pn.read_profile(fp2)
        assert tree2 is not None and tree2["highlight"] == ["hot", "(anonymous)"]

    def test_read_profile_prefers_the_json_over_the_raw_artifact(self, data_home: Path) -> None:
        """The captured tree is authoritative — it carries the scenario label the
        harness set, which the raw artifact does not know."""
        fp = "1" * 16
        raw = store.profiles_dir() / f"{fp}.pstats"
        _real_pstats(raw)
        pn.capture_pstats(raw, store.profiles_dir() / f"{fp}.json", scenario="labelled")
        tree = pn.read_profile(fp)
        assert tree is not None and tree["scenario"] == "labelled"

    def test_read_profile_returns_none_for_unknown_or_unsafe(self, data_home: Path) -> None:
        assert pn.read_profile("2" * 16) is None
        assert pn.read_profile("../../../etc/passwd") is None
        assert pn.read_profile("") is None

    def test_read_time_normalize_failure_returns_none(self, data_home: Path) -> None:
        """A raw artifact deep or degenerate enough to blow the recursion limit must
        degrade the panel, not 500 the request that asked for it."""
        fp = "7" * 16
        raw = store.profiles_dir() / f"{fp}.pstats"
        _real_pstats(raw)

        def _boom(_path: Path) -> None:
            raise RecursionError("maximum recursion depth exceeded")

        with mock.patch.object(pn, "normalize_pstats", _boom):
            assert pn.read_profile(fp) is None

    def test_read_profile_rejects_a_json_file_that_is_not_a_tree(self, data_home: Path) -> None:
        """A hand-edited file can parse as JSON and still not be a tree; serving it
        breaks the viewer downstream of the read."""
        fp = "3" * 16
        (store.profiles_dir() / f"{fp}.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert pn.read_profile(fp) is None

    def test_list_profiles_reports_scenario_and_kind(self, data_home: Path) -> None:
        pn.capture_profile("4" * 16, _real_pstats(data_home / "run.pstats"), scenario="deep")
        pn.capture_profile("5" * 16, _cpuprofile(), scenario="wide")
        listed = {row["fp"]: row for row in pn.list_profiles()}
        assert listed["4" * 16]["scenario"] == "deep"
        assert listed["5" * 16]["scenario"] == "wide"
        assert all(row["kind"] == "cpu" for row in listed.values())

    def test_list_profiles_still_lists_an_unparseable_tree(self, data_home: Path) -> None:
        """A profile the UI cannot label is better than one the UI cannot see."""
        (store.profiles_dir() / "6666666666666666.json").write_text("{oops", encoding="utf-8")
        rows = pn.list_profiles()
        assert rows == [{"fp": "6666666666666666", "scenario": "6666666666666666", "kind": "cpu"}]

    def test_list_profiles_is_empty_when_nothing_captured(self, data_home: Path) -> None:
        assert pn.list_profiles() == []


class TestRealData:
    def test_the_real_profiles_directory_has_no_artifacts_yet(self) -> None:
        """Documents why every other test here is synthetic: the live data directory
        ships no profiler artifact, so a real ``.pstats`` is generated in-process with
        stdlib ``cProfile`` instead. If real captures ever appear, this asserts the
        reader can parse them rather than quietly skipping."""
        real = Path.home() / ".kiro" / "crew" / "apps" / "auto-improvement" / "data" / "profiles"
        if not real.is_dir():
            pytest.skip("no live data directory on this machine")
        artifacts = sorted(p for p in real.glob("*") if p.suffix in {".json", ".pstats"})
        for path in artifacts:
            if path.suffix == ".pstats":
                assert pn.normalize_pstats(path) is not None
            else:
                assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


# ── helpers ───────────────────────────────────────────────────────────────────


def _find(node: dict[str, Any], name: str) -> dict[str, Any] | None:
    if node["name"] == name:
        return node
    for child in node["children"]:
        found = _find(child, name)
        if found is not None:
            return found
    return None


def _depth(node: dict[str, Any]) -> int:
    return 1 + max((_depth(c) for c in node["children"]), default=0)
