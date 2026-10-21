"""Fixes derived from analyzing the app's own dogfood run (31 findings, 1 landed).

The funnel showed the loop's problem was DELIVERY and COVERAGE, not judgement:

* 6 candidates passed the complete gate (RED x2 -> GREEN -> STAYGREEN), but only 1
  reached the branch. Three died at the final ``git push`` with ``non-fast-forward``
  because the branch legitimately advanced during the ~1h run.
* 17 of 31 findings (55%) were never attempted at all — the CYCLE cap bound long before
  the time budget (the run stopped with 0.72h of 1.5h unused).
* The keeper's noise-band test was hardcoded to ``minimize``, so the perf track would
  score a ``maximize`` metric with an inverted comparison. The loop found this itself;
  its fix added a ``direction`` parameter, but nothing PASSED one — so it was inert
  until the driver was wired to the ruler.

This module keeps the tests of those three findings and of the driver's post-agent retire
path beside them. The tests of the later fixes, from the run and from review, live in the
sibling ``test_dogfood_learnings_*.py`` modules, one per topic, and the fixtures they share
live in ``dogfood_learnings_helpers.py``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.apps.builtins.auto_improvement.backend import runner as R
from kiro_crew.apps.builtins.auto_improvement.spine.driver import BudgetCaps
from kiro_crew.apps.builtins.auto_improvement.tests.dogfood_learnings_helpers import (  # noqa: F401
    _cwd_is_the_scratch_dir,
)


class TestMetricDirectionIsPlumbed:
    """The keeper accepts ``direction`` — but a parameter nobody passes is dead code."""

    def test_the_driver_passes_a_direction_to_the_keeper(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        src = Path(D.__file__).read_text(encoding="utf-8")
        assert "direction=self._metric_direction()" in src

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("maximize", "maximize"),
            ("MAXIMIZE", "maximize"),
            ("  maximize  ", "maximize"),
            ("minimize", "minimize"),
            ("", "minimize"),
            (None, "minimize"),
            ("sideways", "minimize"),  # unrecognized -> the safe default
        ],
    )
    def test_direction_is_read_off_the_ruler_and_normalized(self, raw, expected) -> None:
        """Fail-safe: anything unrecognized reads as minimize, which can only make the
        band test stricter for a maximize metric — never wrongly accept a regression."""
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        class _Ruler:
            direction = raw

        class _Profile:
            ruler = _Ruler()

        d = object.__new__(Driver)  # no __init__: this reads only self.profile
        # _metric_direction is duck-typed (getattr chain), so a stub is the point here.
        d.profile = _Profile()  # type: ignore[assignment]
        assert Driver._metric_direction(d) == expected

    def test_a_profile_without_a_ruler_still_resolves(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        d = object.__new__(Driver)
        d.profile = object()  # type: ignore[assignment]
        assert Driver._metric_direction(d) == "minimize"

    def test_the_keeper_inverts_the_band_test_for_maximize(self) -> None:
        """The actual defect: a +delta is an improvement when maximizing."""
        from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
            GateResult,
            Measurement,
        )
        from kiro_crew.apps.builtins.auto_improvement.spine.keeper import Keeper

        k = Keeper()
        gate = GateResult(passed=True, commit_sha="abc")
        win_when_maximizing = Measurement(ok=True, primary_delta=+5.0, noise_band=1.0)
        # ``proposal`` is not consulted by the band/guardrail predicate under test.
        keep, _ = k.evaluate_one(
            proposal=None,  # type: ignore[arg-type]
            gate=gate,
            measurement=win_when_maximizing,
            direction="maximize",
        )
        assert keep is True
        # The same delta is a REGRESSION when minimizing.
        keep_min, _ = k.evaluate_one(
            proposal=None,  # type: ignore[arg-type]
            gate=gate,
            measurement=win_when_maximizing,
            direction="minimize",
        )
        assert keep_min is False


#: The object id every retry-path double reports as the fetched tip. The retry captures that id
#: from `git fetch --porcelain` STDOUT instead of reading the mutable `FETCH_HEAD` ref:
#: that ref is a FILE the pre-push reviewer's shell can rewrite between the fetch and the
#: rebase, which would replay onto a substituted parent whose content nothing scanned. So every
#: double on this path has to answer the fetch, and one that does not is correctly refused.
FETCHED_BASE = "ba5e" + "0" * 36


def _porcelain_fetch(argv: list[str]) -> "subprocess.CompletedProcess | None":
    """The `git fetch --porcelain` reply for *argv*, or ``None`` if it is not a fetch.

    Shape measured against real git 2.50: one line per ref, ``<flag> <old-oid> <new-oid>
    <local-ref>``, and for a `FETCH_HEAD`-style fetch the local-ref column reads ``FETCH_HEAD``
    while the new-oid column carries the tip. Shared so the doubles state it once.
    """
    if argv[:1] != ["fetch"]:
        return None
    return subprocess.CompletedProcess(
        args=argv,
        returncode=0,
        stdout=f"* {'0' * 40} {FETCHED_BASE} FETCH_HEAD\n",
        stderr="",
    )


def _replay_identity(argv: list[str]) -> "subprocess.CompletedProcess | None":
    """The expected-tree reply for *argv*, or ``None`` if it is not one of those reads.

    Before publishing, the retry checks that the replay carries the tree a replay of the
    AUTHORIZED source produces, because the id it works from is captured from ambient `HEAD`
    and a process that moves HEAD before that read has every later check bind to its
    substitute. Identity of object ids cannot express this -- a replay is a new object -- and
    a patch identity would ignore hunk positions, so the binding is the tree: the expected one
    from `merge-tree --write-tree`, the actual one from `log -1 --format=%T`.

    ONE CONSTANT FOR BOTH READS, which is what "the replay carries the authorized content"
    looks like from here. A double that answered one but not the other would report an empty
    tree and refuse, so both are stated. Shared so the doubles state it once.

    ATTRIBUTION IS ANSWERED HERE TOO, for the same reason and with the same shape. The retry
    pins the replay's committer to the identity the authorized object already holds, then
    compares the replayed commit's author, committer and message against that object's -- so a
    double that answered neither read would refuse before reaching what its test is about. One
    constant again: both revisions get the same answer, which is the equal case every test in
    this class except the attribution one depends on.
    """
    if argv[:2] == ["merge-tree", "--write-tree"] or argv[:3] == ["log", "-1", "--format=%T"]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="7ea5" + "0" * 36 + "\n", stderr=""
        )
    if argv[:3] == ["log", "-1", "--format=%cn%x00%ce"]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="pipeline\x00pipeline@example.invalid\n", stderr=""
        )
    if len(argv) >= 3 and argv[:2] == ["log", "-1"] and argv[2].startswith("--format=%an"):
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=(
                "author\x00author@example.invalid\x00pipeline"
                "\x00pipeline@example.invalid\x00the authorized change\n"
            ),
            stderr="",
        )
    return None


class TestPushRetriesOnRace:
    """3 of 6 gate survivors were lost to a branch that moved mid-run."""

    @pytest.fixture(autouse=True)
    def _safe_repository(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: True)
        monkeypatch.setattr(clone_setup, "_push_disabled", lambda _clone: True)

    @staticmethod
    def _driver(clone: Path, log, *, reverify: bool = True, raises: bool = False):
        """A Driver stub whose build gate reports ``reverify`` (or explodes).

        The gate is wired in because the rebase-and-retry path RE-VERIFIES the replayed
        tree before pushing; a stub without one would only ever exercise the refusal.
        """
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        class _Gate:
            def build_and_test(self, *, worktree, src):
                if raises:
                    raise RuntimeError("gate exploded")
                return SimpleNamespace(passed=reverify, detail="stub", commit_sha="")

        d = object.__new__(Driver)
        d.clone = clone  # type: ignore[attr-defined]
        d.log = log  # type: ignore[attr-defined]
        d.profile = SimpleNamespace(build_gate=_Gate())  # type: ignore[attr-defined]
        return d

    def test_a_non_fast_forward_triggers_exactly_one_rebase_and_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            rc = 1 if len(pushes) == 1 else 0
            err = "! [rejected] HEAD -> b (non-fast-forward)" if rc else ""
            return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr=err)

        gits: list[list[str]] = []

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            # The retry resolves the REPLAYED commit to a full object id and pushes THAT
            # rather than `HEAD`, so the double has to answer that question; an empty
            # answer is correctly treated as "cannot resolve" and refuses to push.
            # `rev-list --count FETCH_HEAD..HEAD` proves the rebase replayed exactly ONE
            # commit: an equivalent upstream patch makes git drop ours, and an
            # unanswered count is correctly read as "not one" and refuses.
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha0000000000000000000000000000\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)
        import logging

        d = self._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(
            d, "https://x/y.git", "b", "tgt", src="rebasedsha0000000000000000000000000000"
        )
        assert out is not None
        assert out.returncode == 0
        assert len(pushes) == 2, "should push, rebase, push again"
        assert ["fetch", "--porcelain", "https://x/y.git", "b"] in gits, (
            "the fetch must ask for --porcelain: that is where the tip's object id comes from, "
            "instead of the mutable FETCH_HEAD ref"
        )
        # BOTH ends of the rebase are object ids. `rebase FETCH_HEAD` with no revision replays
        # whatever the BRANCH points at, and that bare form is rejected (`src` is
        # required); naming FETCH_HEAD as the BASE is a defect, since that ref is a
        # mutable file the reviewer can repoint at an unscanned commit.
        assert [
            "rebase",
            FETCHED_BASE,
            "rebasedsha0000000000000000000000000000",
        ] in gits, gits
        assert not any(
            g[:1] == ["rebase"] and "FETCH_HEAD" in g for g in gits
        ), "the rebase still keys on the mutable FETCH_HEAD ref"
        # And the retry publishes the REPLAYED OBJECT BY ID, not `HEAD`: a symbolic ref is
        # resolved by the push itself, i.e. after the re-verify and after the scan.
        assert "rebasedsha0000000000000000000000000000:refs/heads/b" in pushes[1], pushes[1]
        assert "HEAD:refs/heads/b" not in pushes[1], pushes[1]
        # It also REPORTS that object, which is what the caller records in the ledger -- a
        # post-push `rev-parse HEAD` would name whatever HEAD points at by then.
        assert d._pushed_object == "rebasedsha0000000000000000000000000000"

    def test_the_retry_refuses_a_replayed_object_that_does_not_scan_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rebase REPLAYS the commit as a new object, and the caller's credential scan
        covered the PRE-rebase one -- so without a scan here the retry published an object
        nothing had ever scanned. A non-fast-forward retry is not an edge case on this path:
        this class exists because 3 of 6 gate survivors were lost to that race.

        Raised by the GPT review of this branch, which called it the one place the
        object/ref binding was still broken."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []
        gits: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            rc = 1 if len(pushes) == 1 else 0
            err = "! [rejected] HEAD -> b (non-fast-forward)" if rc else ""
            return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr=err)

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            # The scan reads the pushable OBJECTS, not a rendered diff: `cat-file commit`
            # for the header bytes, `diff-tree` to list the changed blobs, then
            # `cat-file blob` for each blob's RAW bytes. The credential is planted in the
            # blob the listing points at, so it reaches the scanner exactly as the push
            # would transfer it.
            blob_sha = "b10b" + "0" * 36
            if a[:2] == ["cat-file", "commit"]:
                # Benign, non-empty commit metadata: an empty read would fail closed.
                return subprocess.CompletedProcess(
                    args=a,
                    returncode=0,
                    stdout="tree 7ea5\nauthor a <a@x> 0 +0000\ncommitter a <a@x> 0 +0000\n\nfix\n",
                    stderr="",
                )
            if a[:1] == ["diff-tree"]:
                # `:<src mode> <dst mode> <src sha> <dst sha> <status>\t<path>` -- one
                # modified blob pointing at the credential-bearing object below.
                return subprocess.CompletedProcess(
                    args=a,
                    returncode=0,
                    stdout=f":100644 100644 {'a' * 40} {blob_sha} M\tapp.py\n",
                    stderr="",
                )
            if a[:2] == ["cat-file", "blob"] and a[2:3] == [blob_sha]:
                # The REPLAYED object carries a credential the first scan never saw. This
                # exact form was checked against the real `scan_content_for_secrets`: the
                # uppercase `AWS_SECRET_ACCESS_KEY = '...'` spelling is NOT flagged, so a
                # test using it would pass for the wrong reason.
                return subprocess.CompletedProcess(
                    args=a, returncode=0, stdout="AWS_KEY = 'glpat-IOSFODNN7EXAMPLE'\n", stderr=""
                )
            # `rev-list --count FETCH_HEAD..HEAD` proves the rebase replayed exactly ONE
            # commit: an equivalent upstream patch makes git drop ours, and an
            # unanswered count is correctly read as "not one" and refuses.
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha0000000000000000000000000000\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(
            d, "https://x/y.git", "b", "tgt", src="rebasedsha0000000000000000000000000000"
        )

        assert len(pushes) == 1, "the replayed object was published without scanning clean"
        assert out is not None and out.returncode == 1, "the original rejection must be returned"
        # And the branch is NOT promoted onto a replay that failed the scan: promotion belongs
        # only on the path where every check passed.
        assert not any(
            g[:2] == ["branch", "-f"] for g in gits
        ), "the branch was force-moved onto a replay that did not scan clean"

    def test_the_retry_refuses_when_the_replayed_commit_cannot_be_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
    ) -> None:
        """Fail closed: not being able to name the replacement is the ABSENCE of the check,
        so it must not fall back to pushing the symbolic ref.

        The MESSAGE is asserted, not just the refusal, because the later HEAD-equality check
        would also refuse an empty id -- so without pinning which one fired, deleting this
        check would leave no test red. Same reason the first-push tests assert which of their
        two identity checks refused."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            rc = 1 if len(pushes) == 1 else 0
            err = "(fetch first)" if rc else ""
            return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr=err)

        # The pre-fetch tamper read is answered (`src` is required now, so it always runs);
        # every LATER answer is empty, including the `rev-list` that resolves the replacement.
        # Both reads are the same argv, so they are told apart by ORDER -- which is also what
        # makes the message assertion below necessary rather than decorative.
        reads: list[list[str]] = []

        def _fake_git(a, cwd, **_kw):
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            out = ""
            if a[:1] == ["rev-list"]:
                reads.append(a)
                out = "authorizedsrc00\n" if len(reads) == 1 else ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        log = logging.getLogger("retry-unresolvable")
        d = TestPushRetriesOnRace._driver(tmp_path, log)
        with caplog.at_level(logging.ERROR, logger="retry-unresolvable"):
            out = D.Driver._push_with_rebase(
                d, "https://x/y.git", "b", "tgt", src="authorizedsrc00"
            )

        assert len(pushes) == 1, "an unresolvable replacement was published anyway"
        assert out is not None and out.returncode == 1
        assert any("cannot resolve the rebased commit" in r.getMessage() for r in caplog.records), [
            r.getMessage() for r in caplog.records
        ]

    def test_the_retry_refuses_when_head_moves_while_the_build_gate_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_reverify_head` runs the repository-under-improvement's OWN test suite, so arbitrary
        code from the tree being published executes inside that call. Resolving the replacement
        commit AFTER it let a test teardown move HEAD and have the moved object captured,
        scanned and pushed as though the gate had measured it -- credentials still covered, but
        nothing had built or tested the thing that landed.

        The replacement is therefore resolved BEFORE the gate and HEAD must still be that
        object afterwards. Here the gate itself moves HEAD, which is exactly the teardown's
        capability. Raised by the GPT review of this branch."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []
        gate_ran: list[str] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            rc = 1 if len(pushes) == 1 else 0
            err = "! [rejected] HEAD -> b (non-fast-forward)" if rc else ""
            return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr=err)

        def _fake_git(a, cwd, **_kw):
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            out = ""
            if a[:1] == ["rev-list"]:
                # Before the gate: the replayed commit. After it: a DIFFERENT object, which is
                # what a teardown moving HEAD looks like from here.
                out = "movedsha00000000000000000000000000000\n" if gate_ran else "rebased0000\n"
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))

        def _gate_that_moves_head(*, worktree, src):
            gate_ran.append("yes")
            return SimpleNamespace(passed=True, detail="green", commit_sha="")

        d.profile.build_gate.build_and_test = _gate_that_moves_head  # type: ignore[attr-defined]

        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="rebased0000")

        assert gate_ran, "the build gate never ran, so nothing was injected"
        assert len(pushes) == 1, "a tree the gate never measured was published"
        assert out is not None and out.returncode == 1

    def test_the_retry_refuses_to_rebase_a_head_that_is_no_longer_the_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`git rebase` replays the CURRENT BRANCH, not the revision handed to this method. So
        if HEAD moved after the caller's checks, the retry would replay the moved commit and
        then bind its own capture, verify and scan to THAT -- every check passing while the
        published content never descended from the authorized object. The rebase input is
        therefore checked first. Raised by the GPT review of this branch."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []
        gits: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            rc = 1 if len(pushes) == 1 else 0
            err = "(fetch first)" if rc else ""
            return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr=err)

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            # HEAD is something OTHER than the authorized source.
            # `rev-list --count FETCH_HEAD..HEAD` proves the rebase replayed exactly ONE
            # commit: an equivalent upstream patch makes git drop ours, and an
            # unanswered count is correctly read as "not one" and refuses.
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "movedhead000\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="authorized00")

        assert len(pushes) == 1, "a moved HEAD was rebased and republished"
        assert not any(g[:1] == ["rebase"] for g in gits), "the rebase ran on an unauthorized HEAD"
        assert not any(g[:1] == ["fetch"] for g in gits), "it should refuse before fetching"
        assert out is not None and out.returncode == 1

    def test_a_rewritten_fetch_head_cannot_substitute_the_rebase_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The base comes from the FETCH's OWN STDOUT, so repointing `FETCH_HEAD` does nothing.

        `FETCH_HEAD` is a mutable FILE in the clone, not a value, and the pre-push reviewer's
        shell can rewrite it between the fetch and the rebase. Measured on a throwaway repo with
        a bare remote: with `.git/FETCH_HEAD` rewritten to a prepared child,
        `rev-list --count FETCH_HEAD..HEAD` still reported 1 while the truth against the real tip
        was 2, the scanned range held only our own file, and the remote ACCEPTED the push -- the
        foreign file landed through a scanner that believed it had looked. Both the rebase input
        and the count read that same name, so they agreed with the substitution instead of
        catching it. This asserts the id the fetch REPORTED is what both of them use, and that
        the ref name appears in neither."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        gits: list[list[str]] = []
        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            rc = 1 if len(pushes) == 1 else 0
            return subprocess.CompletedProcess(
                args=argv, returncode=rc, stdout="", stderr="(fetch first)" if rc else ""
            )

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            out = ""
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha00\n"
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="rebasedsha00")

        assert out is not None and out.returncode == 0
        assert ["rebase", FETCHED_BASE, "rebasedsha00"] in gits, gits
        assert [
            "rev-list",
            "--count",
            f"{FETCHED_BASE}..rebasedsha00",
        ] in gits, (
            "the replayed-commit count must name BOTH captured ids: a `..HEAD` far end resolves "
            "at read time, after the build gate ran the target repo's own tests, and it is not "
            "the range the push publishes"
        )
        assert not any(
            g[:2] == ["rev-list", "--count"] and any("HEAD" in f for f in g[2:]) for g in gits
        ), "the replayed-commit count still has an ambient endpoint"
        assert not any("FETCH_HEAD" in "".join(g) for g in gits if g[:1] != ["fetch"]), (
            "something on the retry path still names FETCH_HEAD, so a rewrite of that file "
            "can still substitute an unscanned parent"
        )

    def test_the_retry_refuses_when_the_fetch_reports_no_object_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: an id it could not obtain is the absence of the check, not a pass.

        Also covers an older git that does not understand `--porcelain` -- there the fetch fails
        outright -- and a ref DELETION, which reports the all-zero null id."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        gits: list[list[str]] = []
        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="(fetch first)"
            )

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            # The fetch SUCCEEDS but says nothing usable -- the null id of a deleted ref.
            if a[:1] == ["fetch"]:
                zero = "0" * 40
                return subprocess.CompletedProcess(
                    args=a, returncode=0, stdout=f"- {zero} {zero} FETCH_HEAD\n", stderr=""
                )
            out = "rebasedsha00\n" if a[:1] == ["rev-list"] else ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="rebasedsha00")

        assert out is not None and out.returncode == 1
        assert len(pushes) == 1, "it published without knowing what it rebased onto"
        assert not any(g[:1] == ["rebase"] for g in gits), "it rebased onto an unknown base"

    def test_the_retry_replays_the_retained_object_and_only_then_moves_the_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`git rebase <base-oid> <rev>` replays an immutable object id ONTO an immutable object
        id, so nothing that rewrites a ref during the `fetch` can substitute either end. A bare
        `git rebase FETCH_HEAD` could substitute the replayed side, because it replays whatever
        the branch points at; naming `FETCH_HEAD` as the BASE could substitute the other side,
        because that ref is a mutable file the pre-push reviewer can repoint at a commit nothing
        scanned. This form detaches HEAD, so the branch is promoted onto the result afterwards,
        and ONLY after re-verification, the HEAD-identity check and the credential scan have
        passed. Raised by the GPT review of this branch and ruled on by the conductor."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []
        gits: list[list[str]] = []
        order: list[str] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            order.append("push")
            rc = 1 if len(pushes) == 1 else 0
            err = "(fetch first)" if rc else ""
            return subprocess.CompletedProcess(args=argv, returncode=rc, stdout="", stderr=err)

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            order.append(" ".join(a[:2]))
            # `rev-list --count FETCH_HEAD..HEAD` proves the rebase replayed exactly ONE
            # commit: an equivalent upstream patch makes git drop ours, and an
            # unanswered count is correctly read as "not one" and refuses.
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha00\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))

        def _gate(*, worktree, src):
            order.append("verify")
            return SimpleNamespace(passed=True, detail="green", commit_sha="")

        d.profile.build_gate.build_and_test = _gate  # type: ignore[attr-defined]
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="rebasedsha00")

        assert out is not None and out.returncode == 0
        # The replay names the object, not the ref.
        assert ["rebase", FETCHED_BASE, "rebasedsha00"] in gits, gits
        assert not any(
            g[:1] == ["rebase"] and "FETCH_HEAD" in g for g in gits
        ), "the rebase base is a mutable ref name, not the id the fetch reported"
        # The branch is promoted onto the replay, then re-attached.
        assert ["branch", "-f", "b", "rebasedsha00"] in gits, gits
        assert ["checkout", "b"] in gits, gits
        # Ordering is the point: promote only after the gate ran, and before the second push.
        assert order.index("verify") < order.index("branch -f"), order
        assert order.index("branch -f") < len(order) - 1, order
        assert "rebasedsha00:refs/heads/b" in pushes[1], pushes[1]

    def test_a_failed_rebase_leaves_the_branch_alone_and_publishes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`git branch -f` is only correct when HEAD IS the replayed result. After a rebase
        that fails, conflicts or aborts, HEAD is something else -- so forcing the branch onto
        it would publish the wrong tree while the branch looked healthy. The promotion is
        therefore reached only on the success path; a failure re-attaches without moving
        anything. Required by the conductor's ruling on this change."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []
        gits: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="(fetch first)"
            )

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            if a[:1] == ["rebase"] and a[1:2] != ["--abort"]:
                return subprocess.CompletedProcess(
                    args=a, returncode=1, stdout="", stderr="CONFLICT"
                )
            # `rev-list --count FETCH_HEAD..HEAD` proves the rebase replayed exactly ONE
            # commit: an equivalent upstream patch makes git drop ours, and an
            # unanswered count is correctly read as "not one" and refuses.
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha00\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        # `src` matches what the double resolves HEAD to, so the pre-rebase tamper check passes
        # and the flow actually reaches the rebase this test is about.
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="rebasedsha00")

        assert len(pushes) == 1, "a failed rebase still published"
        assert ["rebase", "--abort"] in gits, "the failed rebase was not aborted"
        assert not any(
            g[:2] == ["branch", "-f"] for g in gits
        ), "the branch was force-moved after a rebase that failed"
        assert ["checkout", "b"] in gits, "the clone was left detached after the failed rebase"
        assert out is not None and out.returncode == 1

    def test_the_retry_refuses_when_the_rebase_dropped_the_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the remote already carries an equivalent patch, `git rebase` drops ours as
        already-applied and leaves HEAD at the remote tip. Every check downstream would then
        bind to THAT, consistently, and the ledger would record an unrelated commit as the one
        this pipeline landed. So the retry proves exactly one commit was replayed. Raised by
        the GPT review of this branch."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []
        gits: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="(fetch first)"
            )

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            if a[:2] == ["rev-list", "--count"]:
                out = "0\n"  # the rebase dropped our commit
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha00\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="rebasedsha00")

        assert len(pushes) == 1, "a dropped commit was published as though it were ours"
        assert not any(
            g[:2] == ["branch", "-f"] for g in gits
        ), "the branch was promoted onto a commit the rebase did not replay"
        assert out is not None and out.returncode == 1

    def test_a_failed_branch_restore_aborts_the_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ref lock or a checkout failure means the repository is not in the state the push
        assumes -- publishing then leaves the clone detached or its durable branch stale while
        the remote moves on. The restore's status is therefore checked, not logged. Raised by
        the GPT review of this branch."""
        import logging

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="(fetch first)"
            )

        def _fake_git(a, cwd, **_kw):
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            if a[:2] == ["branch", "-f"]:
                return subprocess.CompletedProcess(
                    args=a, returncode=1, stdout="", stderr="cannot lock ref"
                )
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha00\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="rebasedsha00")

        assert len(pushes) == 1, "the push went ahead despite a failed branch restore"
        assert out is not None and out.returncode == 1

    def test_every_git_read_here_ignores_replacement_refs(self) -> None:
        """`git replace` is not on the pre-push reviewer's denylist, and git substitutes
        replaced objects in READS while the push sends the ORIGINAL -- so any read that a
        replacement can redirect is a place where what this code inspects and what it
        publishes come apart. Two instances were found one at a time (the credential scan,
        then the rebase), so the setting belongs at the single chokepoint every read goes
        through rather than on the next call someone remembers. Raised by the GPT review."""
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        src = inspect.getsource(D._git)
        assert (
            '"core.useReplaceRefs=false"' in src
        ), "driver._git no longer disables replacement refs, so a `git replace` can redirect it"

    def test_a_non_race_failure_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retrying an auth or protected-branch error just hides the real message."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="remote: Permission denied"
            )

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        import logging

        d = self._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="authorizedsrc00")
        assert out is not None
        assert out.returncode == 1
        assert len(pushes) == 1, "a non-race failure must not be retried"

    def test_a_conflicting_rebase_aborts_and_never_force_pushes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []
        gits: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="(fetch first)"
            )

        def _fake_git(a, cwd, **_kw):
            gits.append(a)
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            rc = 1 if a[:1] == ["rebase"] and a != ["rebase", "--abort"] else 0
            # `src` is required now, so the pre-fetch tamper check ALWAYS runs; answer its read
            # so the check this test exists to measure is still the one that refuses.
            out = "authorizedsrc00\n" if a[:1] == ["rev-list"] else ""
            return subprocess.CompletedProcess(args=a, returncode=rc, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)
        import logging

        d = self._driver(tmp_path, logging.getLogger("t"))
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="authorizedsrc00")
        assert out is not None
        assert out.returncode == 1
        assert ["rebase", "--abort"] in gits, "a conflicted rebase must be aborted"
        assert len(pushes) == 1, "must not push a half-merged tree"
        assert not any("--force" in a for a in pushes[0]), "never force-push"


class TestARebasedTreeIsReVerifiedBeforePublishing:
    """A clean rebase is a statement about TEXT, not about behaviour.

    The gate result the driver holds was measured against the PRE-rebase base. Replaying
    our verified commit onto a moved branch yields a tree nothing has ever built: measured
    on a real repo — our commit added ``g() -> 2``, the branch meanwhile gained a NEW FILE
    asserting ``g() == 3``, the rebase exited **0** (disjoint paths, no conflict) and the
    combined tree was **RED**. Pre-fix that red tree was pushed to the shared branch, which
    is precisely what a measurement-first pipeline exists to prevent.

    Raised by the GPT review of this branch.
    """

    @pytest.fixture(autouse=True)
    def _safe_repository(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup

        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: True)
        monkeypatch.setattr(clone_setup, "_push_disabled", lambda _clone: True)

    def test_the_replayed_tree_is_re_verified_before_the_second_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        order: list[str] = []

        def _fake_run(argv, **_kw):
            order.append("push")
            rc = 1 if order.count("push") == 1 else 0
            return subprocess.CompletedProcess(
                args=argv, returncode=rc, stdout="", stderr="(fetch first)" if rc else ""
            )

        def _fake_git(a, cwd, **_kw):
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            order.append(a[0])
            # See the sibling class: the retry resolves the replayed commit to a full object
            # id and pushes THAT, so the double must answer `rev-list`.
            # `rev-list --count FETCH_HEAD..HEAD` proves the rebase replayed exactly ONE
            # commit: an equivalent upstream patch makes git drop ours, and an
            # unanswered count is correctly read as "not one" and refuses.
            if a[:2] == ["rev-list", "--count"]:
                out = "1\n"
            elif a[:1] == ["rev-list"]:
                out = "rebasedsha0000000000000000000000000000\n"
            else:
                out = ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"))
        verified: list[str] = []

        def _counting_gate(*, worktree, src):
            verified.append("gate")
            order.append("verify")
            return SimpleNamespace(passed=True, detail="green", commit_sha="")

        d.profile.build_gate.build_and_test = _counting_gate  # type: ignore[attr-defined]

        out = D.Driver._push_with_rebase(
            d, "https://x/y.git", "b", "tgt", src="rebasedsha0000000000000000000000000000"
        )
        assert out is not None
        assert out.returncode == 0
        assert verified == ["gate"], "the rebased tree must be re-verified exactly once"
        # Ordering is the whole point: the tree is verified AFTER the rebase replays it and
        # BEFORE the retry push, so what gets published is what was measured.
        assert order.index("rebase") < order.index("verify") < order.index("push", 1)

    def test_post_rebase_safety_failure_retires_without_further_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        states = iter((True, False))
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: next(states))
        retired: list[Path] = []
        retired_path = tmp_path / ".clone.unsafe-test"

        def _retire(clone: Path) -> Path:
            retired.append(Path(clone))
            return retired_path

        monkeypatch.setattr(clone_setup, "_retire_unsafe_clone", _retire)
        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="(fetch first)"
            )

        gits: list[list[str]] = []

        def _fake_git(args, _cwd, **_kw):
            gits.append(args)
            porcelain = _porcelain_fetch(args)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(args)
            if identity is not None:
                return identity
            # `src` is required now, so the pre-fetch tamper check ALWAYS runs; answer its read
            # so the check this test exists to measure is still the one that refuses.
            out = "authorizedsrc00\n" if args[:1] == ["rev-list"] else ""
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)
        d = TestPushRetriesOnRace._driver(tmp_path / "clone", logging.getLogger("t"))
        d._stop = False
        d._repository_retired = False
        d._progress = lambda **_event: None

        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="authorizedsrc00")

        assert out is None
        assert retired == [tmp_path / "clone"]
        assert d._repository_retired is True and d._stop is True
        assert len(pushes) == 1, "unsafe rebased tree must not reach the retry push"
        assert not any(args[:1] == ["reset"] for args in gits)

    @pytest.mark.parametrize("reverify,raises", [(False, False), (True, True)])
    def test_failed_reverify_still_retires_before_failure_handling(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        reverify: bool,
        raises: bool,
    ) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        states = iter((True, False))
        monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda _clone: next(states))
        monkeypatch.setattr(
            clone_setup, "_retire_unsafe_clone", lambda _clone: tmp_path / ".unsafe"
        )
        pushes: list[list[str]] = []

        def _failed_push(argv, **_kw):  # noqa: ANN001
            pushes.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "(fetch first)")

        monkeypatch.setattr(D.subprocess, "run", _failed_push)
        monkeypatch.setattr(
            D,
            "_git",
            # Answers the pre-fetch tamper read, plus the replay-identity and attribution
            # reads, so the re-verification failure is still what this test measures
            # (`src` is required now).
            lambda args, _cwd, **_kw: _porcelain_fetch(args)
            or _replay_identity(args)
            or subprocess.CompletedProcess(
                args, 0, "authorizedsrc00\n" if args[:1] == ["rev-list"] else "", ""
            ),
        )
        d = TestPushRetriesOnRace._driver(
            tmp_path / "clone", logging.getLogger("t"), reverify=reverify, raises=raises
        )
        d._stop = False
        d._repository_retired = False
        d._progress = lambda **_event: None

        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="authorizedsrc00")

        assert out is None
        assert d._repository_retired is True
        assert len(pushes) == 1

    def test_a_rebased_tree_that_fails_re_verification_is_not_pushed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The measured case: clean rebase, red combined tree. Must NOT publish."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="(fetch first)"
            )

        def _fake_git(a, cwd, **_kw):
            porcelain = _porcelain_fetch(a)
            if porcelain is not None:
                return porcelain
            identity = _replay_identity(a)
            if identity is not None:
                return identity
            # `src` is required now, so the pre-fetch tamper check ALWAYS runs; answer its read
            # so the check this test exists to measure is still the one that refuses.
            out = "authorizedsrc00\n" if a[:1] == ["rev-list"] else ""
            return subprocess.CompletedProcess(args=a, returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(D, "_git", _fake_git)

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"), reverify=False)
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="authorizedsrc00")
        assert out is not None
        assert out.returncode == 1, "the original rejection is returned"
        assert len(pushes) == 1, "an unverified rebased tree must never be pushed"

    def test_a_broken_build_gate_refuses_rather_than_publishes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-CLOSED: an unverifiable tree is exactly what this gate is for."""
        from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

        pushes: list[list[str]] = []

        def _fake_run(argv, **_kw):
            pushes.append(argv)
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="non-fast-forward"
            )

        monkeypatch.setattr(D.subprocess, "run", _fake_run)
        monkeypatch.setattr(
            D,
            "_git",
            # Answers the pre-fetch tamper read so the RAISING gate is still what this
            # test measures (`src` is required now).
            lambda a, cwd, **_kw: _porcelain_fetch(a)
            or subprocess.CompletedProcess(
                args=a,
                returncode=0,
                stdout="authorizedsrc00\n" if a[:1] == ["rev-list"] else "",
                stderr="",
            ),
        )

        d = TestPushRetriesOnRace._driver(tmp_path, logging.getLogger("t"), raises=True)
        out = D.Driver._push_with_rebase(d, "https://x/y.git", "b", "tgt", src="authorizedsrc00")
        assert out is not None
        assert out.returncode == 1
        assert len(pushes) == 1, "a gate that raised must not be read as a pass"

    def test_the_ledger_records_the_sha_that_actually_landed(self) -> None:
        """A rebase rewrites HEAD, so the pre-push snapshot names an absent commit.

        Measured on a real repo: ``eb828444`` before the rebase, ``11aff54a`` after. The
        pre-fix ledger row said ``cr=eb828444`` — a sha nowhere in the remote's history,
        so anyone auditing "what did the bot land?" chases a commit that does not exist.

        The FIX for that was originally a post-push ``rev-parse HEAD``, which this asserted.
        That read was itself the same defect one step later: HEAD can move between the push
        and the read, so the ledger could name an unrelated commit for a change that really
        did land. The publish helper now REPORTS the object it sent, and that is what gets
        recorded -- an id, not a re-read ref.
        """
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        push_src = inspect.getsource(Driver._direct_push)
        assert "pushed_sha" in push_src, "_direct_push must publish the landed sha"
        assert (
            "_pushed_object" in push_src
        ), "the landed sha must be the object the push REPORTED sending, not a re-read ref"
        assert (
            "rev-parse" not in push_src.split("pushed_sha")[-1]
        ), "a ref is re-read after the push again -- HEAD can move in between"
        # And no fallback to the caller's pre-push snapshot. That fallback was unreachable --
        # the only return preceding `_pushed_object = src` routes to `_direct_push`'s own
        # `return None` -- and its stated reason ("callers that do not pass an explicit source")
        # described callers that do not exist once `src` became required.
        assert (
            "self.pushed_sha = sent\n" in push_src
        ), "the recorded sha has a fallback again; it must be the object the push reported"

        body = inspect.getsource(Driver)
        for site in ("direct-pushed to {self.branch}", "direct-pushed bug fix to {self.branch}"):
            assert f'f"{site} ({{landed}})"' in body, f"{site} must record the landed sha"


class TestPostAgentRepositorySafety:
    def test_proposer_exception_retires_before_reraise(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        d = object.__new__(Driver)
        d.proposer = SimpleNamespace(  # type: ignore[assignment]
            fan_out=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("capture failed"))
        )
        d.profile = SimpleNamespace()  # type: ignore[assignment]
        d._stop = False
        retired: list[str] = []

        def _retire(stage: str) -> bool:
            retired.append(stage)
            return True

        d._retire_if_unsafe = _retire  # type: ignore[method-assign]

        result = Driver._fan_out_checked(d, fresh_candidates=[], base_sha="abc", cycle=1)

        assert result is None
        assert retired == ["proposal"]

    def test_candidate_exception_retires_before_ledger_bookkeeping(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver, Stats

        d = object.__new__(Driver)

        def _raise(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            raise RuntimeError("gate failed")

        def _retire(stage: str) -> bool:
            del stage
            return True

        def _fail_record(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
            pytest.fail("ledger bookkeeping ran")

        d._work_one_proposal = _raise  # type: ignore[method-assign]
        d._retire_if_unsafe = _retire  # type: ignore[method-assign]
        d._record = _fail_record  # type: ignore[method-assign]
        d.log = logging.getLogger("t")
        d.stats = Stats()
        prop = SimpleNamespace(cand_id="candidate")

        result = Driver._work_one_proposal_checked(
            d,
            prop,  # type: ignore[arg-type]
            base_sha="abc",
            cycle=1,
            proposals=[],
            perf_survivors=[],
            bug_winners=[],
            gated_sha={},
        )

        assert result is False
        assert d.stats.errors == 0

    def test_each_agent_boundary_precedes_the_next_host_git_phase(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.auto_improvement.spine.driver import Driver

        cycle = inspect.getsource(Driver.run_cycle)
        assert cycle.index("self.profile.discover(") < cycle.index(
            'self._retire_if_unsafe("discovery")'
        ) < cycle.index("self._fan_out_checked(")

        fan_out = inspect.getsource(Driver._fan_out_checked)
        assert fan_out.index("self.proposer.fan_out(") < fan_out.index(
            'self._retire_if_unsafe("proposal")'
        ) < fan_out.index("if error is not None:")

        candidate = inspect.getsource(Driver._work_one_proposal_checked)
        assert candidate.index("self._work_one_proposal(") < candidate.index(
            'self._retire_if_unsafe("candidate gate/measure")'
        ) < candidate.index("if error is not None:")
        assert candidate.index('self._retire_if_unsafe("candidate gate/measure")') < candidate.index(
            "self._record(prop, L.STATUS_ERROR"
        )

        push = inspect.getsource(Driver._direct_push)
        assert push.index("self._prepush_review_clean(") < push.index(
            'self._retire_if_unsafe("pre-push review")'
        ) < push.index('if not sha or sha == "-"')
        assert "elif pushed is False" in inspect.getsource(Driver._apply_verdict)
        assert "elif pushed is False" in inspect.getsource(Driver._apply_bug_winner)

        from kiro_crew.apps.builtins.auto_improvement.spine.pr_pipeline import CrPipeline

        reproduce = inspect.getsource(CrPipeline.emit_perf)
        assert reproduce.count('self._retire_if_unsafe("reproduce")') == 2
        assert reproduce.index('self._retire_if_unsafe("reproduce")') < reproduce.index(
            "if not self._reproduces(verify, reproduce):"
        )


class TestFanOutIsDisjoint:
    """wide and deep both sliced from index 0, so with wide=1/deep=1 the two proposers
    authored the SAME candidate — two agent passes and two gate ladders for one answer."""

    def test_deep_does_not_overlap_wide(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.spine import proposer as P

        src = Path(P.__file__).read_text(encoding="utf-8")
        assert "candidates[self.wide : self.wide + self.deep]" in src
        assert "deep_cands = candidates[: self.deep]" not in src


class TestBudgetDefaultsDoNotStarveCoverage:
    """55% of findings were never attempted because the CYCLE cap bound first."""

    def test_the_cycle_cap_is_generous_relative_to_time(self) -> None:
        assert R.DEFAULT_MAX_CYCLES >= 20, (
            "a low cycle cap leaves discovered findings at 'seen' forever — they are "
            "never rejected, just never tried"
        )

    def test_quiescence_still_ends_a_mined_out_run(self) -> None:
        """Raising the cycle cap is only safe because quiescence is the real stop."""
        assert BudgetCaps.quiesce_after > 0

    def test_quiesce_after_is_configurable(self) -> None:
        from kiro_crew.apps.builtins.auto_improvement.backend import routes

        assert "quiesceAfter" in routes._CONFIG_WRITABLE
