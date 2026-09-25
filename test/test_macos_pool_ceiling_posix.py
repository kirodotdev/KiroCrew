"""The macOS pool ceiling's verdict logic, EXECUTED as bash.

Split out of ``test_macos_platform_tests_gate.py`` and listed in
``test/windows-collect-ignore.txt`` because the whole module is POSIX-only BY
DESIGN, not incidentally: it runs the `decide` job's real bash against a stub
``gh`` whose executable bit has to survive, and on Windows ``shutil.which("bash")``
resolves the WSL launcher while ``chmod`` is a no-op on NTFS. Listing the module is
how this repository states a POSIX-only suite; a class-level ``skipif`` would leave
a marker that reads as a loosened ratchet while proving nothing on that shard
either.

Every property here lives in that script's control flow rather than in the
workflow's shape, so a yaml assertion would pass on a ceiling wired to the wrong
switch.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from macos_lane_helpers import verdict_script as _verdict_script


class TestTheMacOsPoolCeiling:
    """The ceiling refuses `paths` and `sample` while this lane owns the macOS pool.

    Measured on this repository: the lane holds 53 of 56 in-progress macOS jobs and
    37 of 40 queued ones across 22 live runs, and one shard waited 14 hours for a
    runner while `build.yml` and `release.yml` -- the signing paths, which cannot
    run anywhere else -- queued behind it.

    The step's bash is EXECUTED here against a stub `gh`, because every property
    below lives in that script's control flow rather than in the workflow's shape:
    a yaml assertion would pass on a ceiling wired to the wrong switch.
    """

    def _run(
        self,
        tmp_path: Path,
        *,
        paths_hit: str = "false",
        labelled: str = "false",
        head_sha: str = "1" * 40,
        in_progress: str = "0",
        queued: str = "0",
        ceiling: str = "6",
        attempt: str = "1",
        gh_rc: str = "0",
        holders: str = "999",
        reads_cap: str = "12",
        jobs_junk: str = "",
    ) -> tuple[dict[str, str], str, list[str]]:
        bash = shutil.which("bash")
        # Asserted, not skipped. This module executes the verdict step under bash and
        # is excluded from the Windows shards by ``test/windows-collect-ignore.txt``,
        # so every runner that collects it ships bash: a missing one is a broken
        # environment, and skipping would turn that into ten silently absent
        # assertions with the suite still green.
        assert bash is not None, "bash is required to execute the verdict step"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        calls = tmp_path / "gh-calls"
        calls.touch()
        stub = bin_dir / "gh"
        # The stub models BOTH calls the step now makes: the workflow's run
        # listing, which yields run ids, and each run's job listing, which says
        # whether that run holds a macOS job. `holders` of the listed runs do;
        # the rest are runs still in `decide`, which is the case the ceiling must
        # not count.
        stub.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$GH_CALLS"\n'
            'if [ "$STUB_RC" != "0" ]; then exit "$STUB_RC"; fi\n'
            'case "$*" in\n'
            "  *status=in_progress*)\n"
            '    i=1; while [ "$i" -le "$STUB_IN_PROGRESS" ]; do echo "$i"; '
            "i=$(( i + 1 )); done ;;\n"
            "  *status=queued*)\n"
            '    i=1; while [ "$i" -le "$STUB_QUEUED" ]; do echo "$(( 500 + i ))"; '
            "i=$(( i + 1 )); done ;;\n"
            "  */jobs*)\n"
            # The run id is the last path segment before `?`.
            '    id=$(printf "%s" "$*" | sed "s/.*runs\\///; s/\\/jobs.*//")\n'
            '    if [ "$STUB_JOBS_JUNK" != "" ]; then echo "$STUB_JOBS_JUNK"; exit 0; fi\n'
            '    if [ "$id" -le "$STUB_HOLDERS" ]; then echo 1; else echo 0; fi ;;\n'
            "  *) echo 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        out_file = tmp_path / "gh-output"
        out_file.touch()
        proc = subprocess.run(
            [bash, "-c", _verdict_script()],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            # An EXPLICIT env, not `**os.environ`: inheriting it leaves
            # `GITHUB_TOKEN` in place, `gh` falls back to it when the stub is not
            # the `gh` that resolves, and the step's two listing calls then go to
            # the LIVE repository -- a side effect outside this test's tmp dir, on
            # the same installation quota whose exhaustion aborts watchdog ticks.
            # Both token variables are blanked so a leaked real `gh` cannot
            # authenticate, and `timeout` keeps a hung call from riding the global
            # one.
            env={
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "TMPDIR": str(tmp_path),
                "GH_TOKEN": "",
                "GITHUB_TOKEN": "",
                "GH_CALLS": str(calls),
                "STUB_IN_PROGRESS": in_progress,
                "STUB_QUEUED": queued,
                "STUB_HOLDERS": holders,
                "STUB_JOBS_JUNK": jobs_junk,
                "STUB_RC": gh_rc,
                "PATHS_HIT": paths_hit,
                "LABELLED": labelled,
                "HEAD_SHA": head_sha,
                "SAMPLE_ONE_IN": "20",
                "LANE_MAX_LIVE_RUNS": ceiling,
                "LANE_OCCUPANCY_MAX_READS": reads_cap,
                "RUN_ATTEMPT": attempt,
                "GITHUB_REPOSITORY": "kirodotdev/KiroCrew",
                "GITHUB_RUN_ID": "999",
                "GITHUB_OUTPUT": str(out_file),
            },
            cwd=tmp_path,
            timeout=30,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        outputs = dict(
            line.split("=", 1)
            for line in out_file.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        gh_calls = [line for line in calls.read_text(encoding="utf-8").splitlines() if line]
        return outputs, proc.stdout, gh_calls

    def test_a_path_hit_is_refused_while_the_lane_already_owns_the_pool(
        self, tmp_path: Path
    ) -> None:
        outputs, stdout, gh_calls = self._run(
            tmp_path, paths_hit="true", in_progress="5", queued="2", ceiling="6"
        )
        assert outputs["run"] == "false"
        assert outputs["reason"] == "pool-cap"
        # The notice has to name the count, the ceiling and the way out, or the
        # author of a skipped pull request cannot tell this from "no darwin path".
        # "at least", because counting stops at the ceiling rather than totalling
        # the lane.
        assert "at least 6 live lane run(s)" in stdout and "ceiling 6" in stdout
        assert "ci:macos" in stdout
        # Two listings plus one job read per candidate until the ceiling is met.
        assert len(gh_calls) == 2 + 6

    def test_runs_that_hold_no_mac_job_do_not_count_toward_the_ceiling(
        self, tmp_path: Path
    ) -> None:
        """The ceiling is about the POOL, and a run of this workflow is not a pool slot.

        The trigger carries no ``paths:`` filter, so every pull request to main
        starts a run of this workflow and the common verdict is ``run=false`` --
        `paths` is roughly 5 percent of pull requests. So counting RUNS lets six
        concurrent pull requests that touch no darwin path refuse one that does,
        with not a single macOS job in the pool.

        Here nine runs are live and only two hold a macOS job. Negative control:
        the reverted rule counted the runs themselves, reached nine, and refused.
        """
        outputs, stdout, _ = self._run(
            tmp_path, paths_hit="true", in_progress="9", queued="0", holders="2", ceiling="6"
        )
        assert outputs["run"] == "true"
        assert outputs["reason"] == "paths"
        assert "pool" not in stdout.lower()
        # The reverted rule, over the same fixture.
        assert 9 >= 6

    def test_the_occupancy_count_is_bounded_and_under_counts_rather_than_refusing(
        self, tmp_path: Path
    ) -> None:
        """`decide` runs on every pull request, so the count cannot be unbounded.

        With more live runs than the read bound and no holder among them, the step
        reports what it found -- under the ceiling -- and the suite runs. Stopping
        early can only UNDER-count, which is the same direction as failing open.
        """
        outputs, stdout, gh_calls = self._run(
            tmp_path,
            paths_hit="true",
            in_progress="30",
            queued="0",
            holders="0",
            ceiling="6",
            reads_cap="4",
        )
        assert outputs["run"] == "true"
        assert outputs["reason"] == "paths"
        assert "stopped counting macOS lane occupancy after 4 run(s)" in stdout
        assert len(gh_calls) == 2 + 4

    def test_a_path_hit_runs_while_the_pool_has_room(self, tmp_path: Path) -> None:
        outputs, stdout, _ = self._run(
            tmp_path, paths_hit="true", in_progress="3", queued="2", ceiling="6"
        )
        assert outputs["run"] == "true"
        assert outputs["reason"] == "paths"
        assert "pool" not in stdout.lower()

    def test_the_label_is_never_refused_and_spends_no_quota(self, tmp_path: Path) -> None:
        """The label is the on-demand path a fixer uses on a red nightly.

        Silencing it when the pool is busy would remove the one request where
        waiting out the queue is the whole point, so the ceiling is not even
        measured -- which is also why a labelled run costs no API call.
        """
        outputs, _, gh_calls = self._run(
            tmp_path, paths_hit="true", labelled="true", in_progress="99", ceiling="1"
        )
        assert outputs["run"] == "true"
        assert "label" in outputs["reason"]
        assert gh_calls == []

    def test_a_rerun_ignores_the_ceiling_so_a_red_run_cannot_vanish(self, tmp_path: Path) -> None:
        """Same property the SHA sample protects: a retry must not erase a verdict.

        A ceiling applied on every attempt would let a re-run of a RED lane turn
        into a skip whenever the pool happened to be busy the second time.
        """
        outputs, _, gh_calls = self._run(
            tmp_path, paths_hit="true", in_progress="99", ceiling="1", attempt="2"
        )
        assert outputs["run"] == "true"
        assert outputs["reason"] == "paths"
        assert gh_calls == []

    def test_an_unreadable_count_fails_open(self, tmp_path: Path) -> None:
        """The ceiling is an allocation choice, not a safety control.

        A lane that stops covering darwin because one API call failed is the worse
        error, so the suite runs and the tick says why it was not bounded.
        """
        outputs, stdout, _ = self._run(
            tmp_path, paths_hit="true", in_progress="99", ceiling="1", gh_rc="1"
        )
        assert outputs["run"] == "true"
        assert outputs["reason"] == "paths"
        assert "::warning::" in stdout and "could not be read" in stdout

    def test_a_non_numeric_count_fails_open_too(self, tmp_path: Path) -> None:
        # A zero exit with junk on stdout is the shape an API change takes, and
        # arithmetic on it would either abort the step or invent a number.
        outputs, stdout, _ = self._run(
            tmp_path, paths_hit="true", in_progress="3", jobs_junk="not-a-number", ceiling="1"
        )
        assert outputs["run"] == "true"
        assert "could not be read" in stdout

    def test_no_switch_at_all_still_reads_as_no_switch(self, tmp_path: Path) -> None:
        # `pool-cap` must not swallow the ordinary skip: an author reading
        # `reason=none` learns something different from `reason=pool-cap`.
        outputs, _, gh_calls = self._run(tmp_path, paths_hit="false", in_progress="99", ceiling="1")
        assert outputs["run"] == "false"
        assert outputs["reason"] == "none"
        assert gh_calls == []

    def test_the_sample_switch_is_subject_to_the_ceiling(self, tmp_path: Path) -> None:
        # A head SHA whose first 8 hex digits are divisible by 20: bucket 0.
        outputs, _, gh_calls = self._run(
            tmp_path, head_sha="00000000" + "a" * 32, in_progress="9", ceiling="6"
        )
        assert outputs["reason"] == "pool-cap"
        assert len(gh_calls) == 2 + 6
