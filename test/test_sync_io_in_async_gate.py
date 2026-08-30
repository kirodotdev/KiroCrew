"""Tests for the sync-IO-in-async ratchet (scripts/check_sync_io_in_async.py).

#3057: nothing in the repository failed when blocking IO was written inside an
``async def``, so the count grew back after every individual fix -- ~70 on-loop
``store.db.execute()`` calls in ``dashboard/handlers/knowledge.py`` against zero
in ``dashboard/handlers/memory.py`` in the same directory. These tests pin the
halves that must stay true together: CI actually runs the gate (a gate that
exists only on disk is not a gate), the AST rules flag what they claim to flag
AND stay quiet on the sanctioned offload idioms, and the baseline can only
shrink -- no operation may add a path or raise a count.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_sync_io_in_async.py"
BASELINE = ROOT / ".github" / "sync-io-in-async-baseline.txt"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PROFILE = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "prepare-pr"
    / "profiles"
    / "kirocrew.json"
)

SPEC = importlib.util.spec_from_file_location("check_sync_io_in_async", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _lint_steps() -> list[dict]:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        steps = job.get("steps") or []
        if any("isort --check-only" in str(step.get("run", "")) for step in steps):
            return steps
    raise AssertionError("ci.yml has no job running isort --check-only")


class TestCiWiring:
    def test_ci_actually_runs_the_gate(self) -> None:
        runs = [str(step.get("run", "")) for step in _lint_steps()]
        assert any(
            "scripts/check_sync_io_in_async.py" in run for run in runs
        ), "ci.yml's lint job no longer runs the sync-io-in-async gate"

    def test_ci_runs_the_self_test_first(self) -> None:
        # The self-test plants one probe per rule family, so a typo that
        # silently disables a rule fails in CI instead of shipping green.
        for run in (str(step.get("run", "")) for step in _lint_steps()):
            if "check_sync_io_in_async.py" not in run:
                continue
            assert "--test" in run, "the gate step must run the --test self-test"
            return
        raise AssertionError("gate step not found")

    def test_prepare_pr_floor_carries_both_invocations(self) -> None:
        # The floor ships frozen into every install, so a gate CI gains but the
        # floor never learns about is one a contributor cannot run locally.
        floor = PROFILE.read_text(encoding="utf-8")
        assert "python3 scripts/check_sync_io_in_async.py --test" in floor
        assert '"python3 scripts/check_sync_io_in_async.py"' in floor

    def test_scope_helpers_coupling_is_alive(self) -> None:
        # The gate loads scripts/ratchet_scope.py, which OWNS both answers. A
        # rename there must fail HERE, not as an AttributeError inside a CI run.
        scope = gate._scope()
        assert callable(scope.changed_paths)
        assert callable(scope.added_lines)

    def test_all_ratchets_share_the_one_resolver(self) -> None:
        # The whole point of the shared module: if a gate grows a private copy
        # again, the same added line can be red under one gate and green under
        # another. Each of the merge-ref ratchets must reach the resolver through
        # it -- and must NOT reach into a sibling GATE's private helpers, which is
        # how check_agent_sdk_boundary.py broke when the resolver moved.
        for name in (
            "check_black_formatting.py",
            "check_subprocess_encoding.py",
            "check_agent_sdk_boundary.py",
            "check_sync_io_in_async.py",
        ):
            source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
            assert "ratchet_scope.py" in source, f"{name} no longer uses the shared resolver"
            assert (
                "def _changed_paths" not in source
            ), f"{name} has grown a private copy of the resolver again"
            for sibling in ("check_black_formatting", "check_subprocess_encoding"):
                if name.startswith(sibling):
                    continue
                assert (
                    f'spec_from_file_location("{sibling}"' not in source
                ), f"{name} loads {sibling}'s private helpers instead of ratchet_scope"


class TestRuleFamilies:
    """The rules, driven through the real detector.

    The per-spelling PROBE CORPUS lives in the script (`_FLAGGED` / `_CLEAN`), so
    CI's `--test` self-test and this suite exercise ONE set of sources rather than
    two byte-identical copies that would drift apart. These tests parametrize over
    that corpus, then add the assertions the corpus cannot make on its own: exact
    line numbers, exact family classification, provenance, and raising.
    """

    def _families(self, source: str) -> list[str]:
        return [family for _line, family, _prov in gate._violations_in_source(source)]

    @pytest.mark.parametrize("label", sorted(gate._FLAGGED))
    def test_every_flagged_probe_is_flagged(self, label: str) -> None:
        assert gate._violations_in_source(gate._FLAGGED[label]), f"not flagged: {label}"

    @pytest.mark.parametrize("label", sorted(gate._CLEAN))
    def test_every_clean_probe_stays_clean(self, label: str) -> None:
        found = gate._violations_in_source(gate._CLEAN[label])
        assert found == [], f"flagged but should be clean: {label} -> {found}"

    def test_the_corpus_exercises_every_family(self) -> None:
        # A family whose probe disappears would leave its remedy text and its
        # matching table untested, and the parametrized pass above could not tell.
        seen = set()
        for source in gate._FLAGGED.values():
            seen.update(self._families(source))
        assert seen == set(
            gate.FAMILY_REMEDY
        ), f"families without a probe: {set(gate.FAMILY_REMEDY) - seen}"

    def test_each_family_classifies_to_its_own_name(self) -> None:
        # The corpus proves "flagged"; these four pin WHICH family, which is what
        # selects the remedy text a contributor is shown.
        assert self._families("async def f(store):\n    store.db.execute('SELECT 1')\n") == ["db"]
        assert self._families(
            "import subprocess\nasync def f():\n    subprocess.run(['git'])\n"
        ) == ["subprocess"]
        assert self._families(
            "import requests\nasync def f():\n    requests.get('http://x')\n"
        ) == ["http"]
        assert self._families("import time\nasync def f():\n    time.sleep(5)\n") == ["sleep"]

    def test_flags_multiline_calls_once(self) -> None:
        source = (
            "import subprocess\n"
            "async def f():\n"
            "    subprocess.run(\n"
            "        ['git'],\n"
            "        check=True,\n"
            "    )\n"
        )
        assert gate._violations_in_source(source) == [(3, "subprocess", None)]

    def test_marker_does_not_leak_to_another_line(self) -> None:
        source = (
            "import time\n"
            "async def f():\n"
            "    time.sleep(1)\n"
            f"    time.sleep(2)  # {gate.MARKER}: bounded\n"
        )
        assert gate._violations_in_source(source) == [(3, "sleep", None)]

    def test_a_client_verdict_records_its_binding_line(self) -> None:
        # The provenance line is what lets the added-line rule catch a change that
        # adds the binding while leaving the call it flips untouched.
        source = (
            "import requests\n"
            "async def f(url):\n"
            "    s = requests.Session()\n"
            "    s.get(url)\n"
        )
        assert gate._violations_in_source(source) == [(4, "http", 3)]

    def test_a_module_level_verdict_has_no_provenance_line(self) -> None:
        source = "import requests\nasync def f():\n    requests.get('http://x')\n"
        assert gate._violations_in_source(source) == [(3, "http", None)]

    def test_a_sibling_coroutines_client_name_does_not_leak(self) -> None:
        # GPT 5.6 caught this: client names were collected file-wide, so a sync
        # `session` in one coroutine made a same-named httpx.AsyncClient in
        # another look blocking. A false positive is worse than a miss for a gate
        # -- the only way out would be a marker asserting something untrue.
        source = (
            "import requests\n"
            "import httpx\n"
            "async def a(url):\n"
            "    session = requests.Session()\n"
            "    session.get(url)\n"
            "async def b(url):\n"
            "    session = httpx.AsyncClient()\n"
            "    return await session.get(url)\n"
        )
        # Only coroutine a's call is blocking; b's awaited AsyncClient is not.
        assert gate._violations_in_source(source) == [(5, "http", 4)]

    def test_an_async_rebinding_disambiguates_its_own_scope(self) -> None:
        source = (
            "import requests\n"
            "import httpx\n"
            "async def f(url):\n"
            "    c = requests.Session()\n"
            "    c = httpx.AsyncClient()\n"
            "    return await c.get(url)\n"
        )
        assert self._families(source) == []

    def test_a_module_global_client_is_visible_inside_a_coroutine(self) -> None:
        # Scoping must not go too far the other way: a global IS readable.
        source = (
            "import requests\n"
            "SESSION = requests.Session()\n"
            "async def f(url):\n"
            "    SESSION.get(url)\n"
        )
        assert gate._violations_in_source(source) == [(4, "http", 2)]

    def test_an_aliased_sync_constructor_still_counts(self) -> None:
        source = (
            "from requests import Session as Sess\n"
            "async def f(url):\n"
            "    s = Sess()\n"
            "    s.get(url)\n"
        )
        assert self._families(source) == ["http"]

    def test_another_classs_async_attribute_does_not_erase_this_ones_client(self) -> None:
        # GPT 5.6's follow-up: attributes were file-wide, so class B's AsyncClient
        # erased class A's synchronous binding of the same attribute name and A's
        # blocking call went unreported. A miss rather than a false positive, but
        # a miss in the one family this PR claims starts at zero.
        source = (
            "import requests\n"
            "import httpx\n"
            "class A:\n"
            "    def __init__(self):\n"
            "        self._client = requests.Session()\n"
            "    async def f(self, url):\n"
            "        self._client.get(url)\n"
            "class B:\n"
            "    def __init__(self):\n"
            "        self._client = httpx.AsyncClient()\n"
            "    async def g(self, url):\n"
            "        return await self._client.get(url)\n"
        )
        # A's call is blocking (bound at line 5); B's awaited AsyncClient is not.
        assert gate._violations_in_source(source) == [(7, "http", 5)]

    def test_unparseable_source_raises_instead_of_reading_clean(self) -> None:
        with pytest.raises(SyntaxError):
            gate._violations_in_source("async def f(:\n")

    def test_self_test_passes(self) -> None:
        assert gate._self_test() == 0


class TestVerdicts:
    """The ratchet's four verdicts, driven directly."""

    def test_unbaselined_file_in_scope_is_a_new_offender(self) -> None:
        new, grown, added, shrunk = gate._verdicts(
            {"a.py": [(1, "db", None)]}, {}, {"a.py"}, {"a.py": {1}}
        )
        assert (new, grown, shrunk) == (["a.py"], [], [])
        # A brand-new offender is reported once, as a new offender -- not also
        # under the added-line rule, which exists for baselined files.
        assert added == {}

    def test_out_of_scope_files_are_not_judged(self) -> None:
        # CI evaluates a merge ref, so a file the base branch merged after the
        # baseline was recorded must not redden this change.
        new, grown, added, shrunk = gate._verdicts(
            {"other.py": [(1, "db", None)]}, {}, {"mine.py"}, {"mine.py": set()}
        )
        assert (new, grown, added, shrunk) == ([], [], {}, [])

    def test_grown_count_fails(self) -> None:
        new, grown, _added, shrunk = gate._verdicts(
            {"a.py": [(1, "db", None), (2, "db", None)]}, {"a.py": 1}, {"a.py"}, None
        )
        assert (new, grown, shrunk) == ([], ["a.py"], [])

    def test_swapping_one_violation_for_another_is_caught_by_added_lines(self) -> None:
        # Count level at 1, but the surviving call sits on a line this change
        # added: fixing an old call while adding a new one must not slip through.
        new, grown, added, shrunk = gate._verdicts(
            {"a.py": [(9, "http", None)]}, {"a.py": 1}, {"a.py"}, {"a.py": {9}}
        )
        assert (new, grown, shrunk) == ([], [], [])
        assert added == {"a.py": [(9, "http", None)]}

    def test_level_count_on_untouched_lines_passes(self) -> None:
        new, grown, added, shrunk = gate._verdicts(
            {"a.py": [(9, "http", None)]}, {"a.py": 1}, {"a.py"}, {"a.py": {40}}
        )
        assert (new, grown, added, shrunk) == ([], [], {}, [])

    def test_an_added_client_binding_is_caught_even_when_the_call_is_untouched(self) -> None:
        # The composite GPT 5.6 identified: adding `s = requests.Session()` (line
        # 40) turns an untouched `s.get(url)` (line 9) into blocking I/O, and
        # removing another violation in the same file keeps the count level, so
        # neither the count rule nor a call-line-only check would report it. The
        # violation's provenance line is what makes it visible.
        new, grown, added, shrunk = gate._verdicts(
            {"a.py": [(9, "http", 40)]}, {"a.py": 1}, {"a.py"}, {"a.py": {40}}
        )
        assert (new, grown, shrunk) == ([], [], [])
        assert added == {"a.py": [(9, "http", 40)]}

    def test_missing_added_line_info_skips_only_that_rule(self) -> None:
        new, grown, added, shrunk = gate._verdicts(
            {"a.py": [(9, "http", None)]}, {"a.py": 1}, {"a.py"}, None
        )
        assert (new, grown, added, shrunk) == ([], [], {}, [])

    def test_shrunk_count_demands_a_prune(self) -> None:
        new, grown, _added, shrunk = gate._verdicts({}, {"a.py": 3}, {"a.py"}, None)
        assert (new, grown, shrunk) == ([], [], ["a.py"])

    def test_undeterminable_scope_judges_the_whole_tree(self) -> None:
        # None scope must fail OPEN to whole-tree, never to "judge nothing":
        # a gate that disables itself when its inputs are unusual is not a gate.
        new, _grown, _added, _shrunk = gate._verdicts({"a.py": [(1, "db", None)]}, {}, None, None)
        assert new == ["a.py"]


class TestBaselineRatchet:
    def test_committed_baseline_parses_and_files_exist(self) -> None:
        entries = gate._read_baseline(BASELINE)
        assert entries, "the committed baseline is empty"
        for rel, count in entries.items():
            assert count > 0, f"{rel} is baselined at zero; it should be pruned"
            assert (ROOT / rel).is_file(), f"baselined path {rel} no longer exists"

    def test_missing_baseline_refuses_rather_than_absorbs(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            gate._read_baseline(tmp_path / "nope.txt")

    def test_malformed_baseline_line_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "b.txt"
        path.write_text("not-a-count src/x.py\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            gate._read_baseline(path)

    def test_duplicate_baseline_entry_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "b.txt"
        path.write_text("1 src/x.py\n2 src/x.py\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            gate._read_baseline(path)

    def test_refresh_never_adds_a_path(self) -> None:
        out = gate._shrunken_baseline({"a.py": 1}, {"a.py": 1, "b.py": 5})
        assert out == {"a.py": 1}

    def test_refresh_never_raises_a_count(self) -> None:
        assert gate._shrunken_baseline({"a.py": 2}, {"a.py": 9}) == {"a.py": 2}

    def test_refresh_lowers_and_prunes(self) -> None:
        out = gate._shrunken_baseline({"a.py": 5, "b.py": 2}, {"a.py": 3})
        assert out == {"a.py": 3}

    def test_write_read_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "b.txt"
        gate._write_baseline(path, {"src/x.py": 2})
        assert gate._read_baseline(path) == {"src/x.py": 2}

    def test_committed_baseline_is_byte_identical_to_a_refresh(self, tmp_path: Path) -> None:
        # The header is hand-written in the file and generated by the script; a
        # drift between them would make the next --update-baseline churn the
        # whole file and bury the one line that actually changed.
        path = tmp_path / "b.txt"
        gate._write_baseline(path, gate._read_baseline(BASELINE))
        assert path.read_text(encoding="utf-8") == BASELINE.read_text(encoding="utf-8")

    def test_baseline_points_at_the_report_mode(self) -> None:
        # --report is the work queue for clearing this list, so the file a
        # contributor reads when the gate fires has to name it.
        header = BASELINE.read_text(encoding="utf-8")
        assert "--report" in header
        assert "--update-baseline" in header


class TestReportMode:
    def test_report_runs_and_covers_the_baselined_files(self, capsys) -> None:
        assert gate.report() == 0
        out = capsys.readouterr().out
        assert "on-loop blocking call(s)" in out
        for rel in gate._read_baseline(BASELINE):
            assert rel in out, f"--report omits the baselined file {rel}"


class TestExemplarStaysClean:
    def test_the_fully_offloaded_handler_stays_clean(self) -> None:
        # dashboard/handlers/memory.py is #3057's control case: it wraps every
        # store call in asyncio.to_thread while handlers/knowledge.py next door
        # does not. It must never appear in the baseline.
        rel = "src/kiro_crew/dashboard/handlers/memory.py"
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert gate._violations_in_source(source) == []
        assert rel not in gate._read_baseline(BASELINE)
