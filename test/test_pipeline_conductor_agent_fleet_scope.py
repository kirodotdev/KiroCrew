"""Pipeline-conductor fleet probe: the cwd-scoped banned-process scan.

A topic class of ``TestFleetProbe`` in ``test_pipeline_conductor_agent.py``: which
banned processes belong to the fleet, judged by their working directory or program
path, and what the probe prints about the rest. It runs the real script over a faked
``/proc`` through ``FleetProbeHarness``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pipeline_conductor_agent_helpers import FleetProbeHarness

from conftest import make_dir_link
from kiro_crew import platform_compat


class TestFleetProbeCwdScope(FleetProbeHarness):
    # ── 2d: cwd-scoped banned scan ────────────────────────────────────────────
    def test_a_banned_match_outside_the_fleet_is_foreign_not_banned(
        self, tmp_path, capsys, monkeypatch
    ):
        """A banned command SHAPE is only a banned OPERATION when the fleet owns
        it. The same unbounded pytest in an unrelated checkout is this machine's
        business, and counting it made the conductor stop a worker that was not
        the offender."""
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        theirs = tmp_path / "somebody-else" / "repo"
        proc = self._proc(tmp_path, "5001", b"python\x00-m\x00pytest\x00-n4\x00test/x.py\x00", mine)
        self._proc(tmp_path, "5002", b"python\x00-m\x00pytest\x00-n4\x00test/y.py\x00", theirs)
        cfg = self._config(
            tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet" / "wt-a")]
        )
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=5001 rule=" in out and "cwd=fleet" in out
        assert "pid=5002" not in out, "an unrelated checkout is not reported"
        assert "banned 1 | foreign 1" in out

    def test_a_delivered_worker_goes_quiet_rather_than_idle(self, tmp_path, capsys, monkeypatch):
        """A delivered, dispositioned worker fires NOTHING once it ages.

        Raised by a review lane as "`TERMINAL` never fires when unprefixed text is
        appended". The first half is true and the harm is not, which is only
        visible by running it. A real transcript APPENDS, so the `GREEN:` row stays
        inside the window: the tag remains `GREEN`, the digest is unchanged, and the
        signal is suppressed. The lane's inference was that the session then ages
        into `IDLE` and the conductor re-dispatches a worker that has already
        delivered. It does not -- the suppressed `GREEN` keeps `IDLE` from being the
        reading too, so the line is silent, which is the correct output for a worker
        whose completion was already reported and acted on.

        `TERMINAL` is therefore the reading for a finished worker whose report is no
        longer VISIBLE while the state file still remembers it. Both paths reach the
        property that matters -- never read as merely idle, never re-dispatched --
        and the prescribed fix (demote any trailing unprefixed row to `-`) would
        make a plainly stated `GREEN:` depend on the state file to be understood.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "GREEN: https://example.invalid/pull/1 abc1234"},
        ]
        self._transcript(sessions, "s-done", rows)
        out = self._run(mod, cfg, capsys)
        assert "GREEN" in out, "the report itself is the reading while it is visible"
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "GREEN", self._digest_of(out, "s-done")
        )
        # Appends, as a real transcript does: the GREEN row does not go away.
        self._transcript(
            sessions,
            "s-done",
            rows + [{"role": "assistant", "content": "One more note, no prefix."}],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "IDLE" not in out, "a delivered worker must never be read as merely idle"
        assert "0 fired" in out, "and it must not be re-presented either"

    def test_terminal_covers_the_case_the_transcript_no_longer_states(
        self, tmp_path, capsys, monkeypatch
    ):
        """The other half of the same boundary: once the terminal report scrolls
        out of the window the transcript alone cannot say the worker finished, and
        that is where the remembered tag earns its keep -- `TERMINAL` rather than
        `IDLE`, so an aged finished worker is not nudged."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-gone"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-gone", "GREEN: delivered abc1234")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-gone", "GREEN", self._digest_of(out, "s-gone")
        )
        # The report is not in the window; only the state file knows.
        self._session(sessions, "s-gone", "trailing chatter with no prefix", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out
        assert "IDLE" not in out

    def test_a_reseeded_worker_does_not_read_as_finished(self, tmp_path, capsys, monkeypatch):
        """The compliant re-seed case the protocol requires: a worker handed new
        work states a prefix, and that newer report supersedes the terminal one.

        The transcript is aged so `TERMINAL` WOULD be the reading if the terminal
        report still won; the assertion is that it does not. `WORKING` is a
        heartbeat and deliberately fires nothing, so the session goes quiet rather
        than reporting -- quiet being the correct output for a worker that is
        working.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-again"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-again",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "GREEN: delivered abc1234"},
                {"role": "nudge", "content": "new item assigned"},
                {"role": "assistant", "content": "WORKING: picked up the new item"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" not in out, "a re-seeded worker is not finished"

    def test_any_later_disposition_preserves_the_answered_payload(
        self, tmp_path, capsys, monkeypatch
    ):
        """Not just condition tags: ANY later mark must preserve the payload.

        The first version of this fix keyed on ``IDLE``/``NOPROGRESS`` only, which
        left the same hole open one door down. A session whose ``BLOCKED`` was
        answered can hit an error row, get its ``ERR`` dispositioned, and then post
        a heartbeat -- at which point the error branch stops matching, the sticky
        ``BLOCKED`` is the reading again, and the ``ERR`` mark has overwritten the
        record that the ruling was delivered. The answered ruling re-presents and
        the conductor re-adjudicates a decision it already made.

        A ``GONE`` mark cannot reach this: marking refuses a digest no probe
        reported, and a GONE session has no transcript left to re-read a report
        from. ``ERR`` is the reachable one, which is why it is the one tested.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-err"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-err", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-err", "BLOCKED", self._digest_of(out, "s-err")
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys)
        rows = rows + [{"role": "error", "content": "tool crashed"}]
        self._transcript(sessions, "s-err", rows)
        out = self._run(mod, cfg, capsys)
        assert "ERR" in out
        self._run(mod, cfg, capsys, "--mark-handled", "s-err", "ERR", self._digest_of(out, "s-err"))
        # The heartbeat stops the error row being last, so the sticky BLOCKED is
        # the reading again -- and it is still answered.
        self._transcript(
            sessions, "s-err", rows + [{"role": "assistant", "content": "WORKING: back on it"}]
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys), "answered rulings stay answered"

    def test_the_shipped_state_shape_upgrades_without_losing_terminal(
        self, tmp_path, capsys, monkeypatch
    ):
        """The upgrade path that actually exists.

        The shipped writer records ``{tag, digest, ts}``, so a session whose last
        mark was a terminal payload still reports ``TERMINAL`` after an upgrade,
        recovered from the entry's own tag -- no nudge for a worker that already
        delivered. An earlier draft also read a ``proto`` field; measurement showed
        no released version ever wrote one (it existed only in intermediate commits
        of this branch), so that path and its test were removed rather than left
        asserting behaviour for an input that cannot occur.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-old"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-old", "trailing chatter with no prefix", age_secs=500)
        state_path = Path(f"{cfg}.state.json")
        state_path.write_text(
            json.dumps({"handled": {"s-old": {"tag": "GREEN", "digest": "d0", "ts": 1}}}),
            encoding="utf-8",
        )
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out, "the shipped shape still names a delivered worker"
        assert "IDLE" not in out

    def test_a_legacy_answered_payload_survives_a_later_mark(self, tmp_path, capsys, monkeypatch):
        """The upgrade path must not lose the DIGEST, only the field name changed.

        A state file written before ``settled`` existed records an answered payload
        as the entry's own ``tag``/``digest``. Carrying only the legacy ``proto``
        tag forward keeps the terminal reading but drops the digest, and
        suppression needs both halves -- so a pre-upgrade answered ``BLOCKED``
        followed by any later mark presented the ruling again on the first cycle
        after the upgrade. Raised by a review lane against the compatibility path
        added to prevent exactly this.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-up"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-up", rows)
        out = self._run(mod, cfg, capsys)
        digest = self._digest_of(out, "s-up")
        # The pre-upgrade shape: the answered payload IS the entry, no `settled`.
        Path(f"{cfg}.state.json").write_text(
            json.dumps(
                {
                    "handled": {
                        "s-up": {
                            "tag": "BLOCKED",
                            "digest": digest,
                            "ts": int(time.time()),
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys), "the legacy record still suppresses"
        rows = rows + [{"role": "error", "content": "tool crashed"}]
        self._transcript(sessions, "s-up", rows)
        out = self._run(mod, cfg, capsys)
        self._run(mod, cfg, capsys, "--mark-handled", "s-up", "ERR", self._digest_of(out, "s-up"))
        assert (
            self._handled(cfg)["s-up"]["settled"]["digest"] == digest
        ), "digest carried, not dropped"
        self._transcript(
            sessions, "s-up", rows + [{"role": "assistant", "content": "WORKING: back on it"}]
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys), "answered rulings survive the upgrade"

    def test_a_delivered_worker_is_not_reclassified_as_stalled(self, tmp_path, capsys, monkeypatch):
        """A finished worker produces nothing BY DEFINITION, so absence of output
        is not a stall.

        The common shape: a worker files `GREEN:`, the conductor marks it, and the
        transcript then never changes again. The tag stays `GREEN` -- a firing tag,
        so the terminal/idle ladder is never reached -- the report is suppressed by
        digest, and the index is unchanged because nothing more was written. Without
        a guard that makes it `NOPROGRESS`, which re-fires every cycle because it
        expires, sending the conductor to nudge a session that already delivered.

        The neighbouring test escapes this only because its fixture APPENDS a line,
        which moves the index. That is exactly the kind of accident a common-path
        fixture hides, so this one changes nothing after the mark.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-shipped"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-shipped", "GREEN: https://example.invalid/pull/1 abc1234")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-shipped",
            "GREEN",
            self._digest_of(out, "s-shipped"),
        )
        # Nothing further is written; only the clock moves.
        self._age_mark(cfg, "s-shipped", 500)
        (sessions / "s-shipped.jsonl").touch()
        os.utime(sessions / "s-shipped.jsonl", (time.time() - 500, time.time() - 500))
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" not in out, "a delivered worker is finished, not stalled"
        assert "0 fired" in out

    def test_a_surfaced_ruling_can_actually_be_dispositioned(self, tmp_path, capsys, monkeypatch):
        """A signal the conductor cannot mark is worse than no signal.

        Surfacing the sticky ``BLOCKED`` from under a handled ``ERR`` prints a
        digest computed over the RULING. ``--mark-handled`` recomputes the payload
        from the transcript, where the error row is still last, so it digests the
        ERR text instead and refuses the mark as a changed payload. The ruling then
        fires every cycle forever, undismissable -- a signal that cannot be
        dispositioned is a worse failure than the one surfacing it fixed.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-mark"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
            {"role": "error", "content": "tool crashed"},
        ]
        self._transcript(sessions, "s-mark", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-mark", "ERR", self._digest_of(out, "s-mark")
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        # The mark must be ACCEPTED, and the ruling must then go quiet.
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-mark", "BLOCKED", self._digest_of(out, "s-mark")
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys)

    def test_a_symlinked_fleet_root_cannot_smuggle_a_wider_scope(self, tmp_path, monkeypatch):
        """Validation must judge what the root RESOLVES to, not how it is spelled.

        The classifier compares `realpath` as well as the literal path -- that
        second chance exists so a symlinked worktree still matches -- so a root
        spelled as a symlink to a filesystem root passes a literal-only check and
        then matches every cwd on the host. The widening returns by the same door
        the convenience opened.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [])
        store = mod._sessions_dir()
        for target in (Path(os.sep), store.parent):
            link = tmp_path / f"link-{abs(hash(str(target))) % 1000}"
            if link.is_symlink() or link.exists():
                platform_compat.unlink_link_or_junction(link)
            make_dir_link(link, target)
            cfg.write_text(
                json.dumps({"sessions": [], "fleet_worktrees": [str(link)]}), encoding="utf-8"
            )
            assert mod.main(["--config", str(cfg)]) == 2, target

    def test_a_filesystem_root_as_a_fleet_root_is_refused(self, tmp_path, monkeypatch):
        """A root of ``/`` makes every cwd on the host read as fleet-owned.

        ``fleet_worktrees`` comes from a config the conductor authors, so this is a
        trust boundary and not merely a typo: ``cwd=fleet`` is the one class that
        STOPS a session, so a root that matches everything turns the ownership
        guard into a false-stop generator against unrelated processes -- the exact
        failure scoping the scan was introduced to prevent. Refused at load time
        with a message, the same discipline as the NUL byte.

        Which RULE refuses each of these differs, and mutation testing is what
        showed it: on POSIX ``/`` is always an ancestor of the session store, so
        the store rule catches it and the filesystem-root rule is redundant here;
        ``C:\\`` is not absolute on POSIX, so the absolute-path rule catches that.
        The root rule earns its place on Windows, where the store can sit on
        another drive and a drive root is both absolute and not an ancestor of it.
        This test asserts the OUTCOME -- every one of these is refused on every
        platform -- rather than pretending to isolate one branch.
        """
        mod = self._mod()
        for bad in ("/", "//", "/.", "C:\\", "C:/"):
            cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[bad])
            assert mod.main(["--config", str(cfg)]) == 2, bad

    def test_an_ancestor_of_the_session_store_is_refused_as_a_fleet_root(
        self, tmp_path, monkeypatch
    ):
        """A root that CONTAINS the session store is the same widening one level up.

        It cannot be a worktree -- the store is the conductor's own data directory --
        and accepting it makes every process working anywhere beneath it, including
        the conductor itself, read as a fleet worker eligible to be stopped.
        """
        mod = self._mod()
        # The env that decides where the store lives is set by _config, so the
        # store has to be resolved AFTER it -- reading it first answers for the
        # real home directory and the test silently checks the wrong path.
        cfg = self._config(tmp_path, monkeypatch, [])
        store = mod._sessions_dir()
        for ancestor in (store.parent, store.parent.parent):
            cfg.write_text(
                json.dumps({"sessions": [], "fleet_worktrees": [str(ancestor)]}), encoding="utf-8"
            )
            assert mod.main(["--config", str(cfg)]) == 2, ancestor

    def test_ownership_fails_toward_the_non_stopping_class(self, tmp_path, capsys, monkeypatch):
        """When ownership cannot be established, answer ``unknown``, never ``fleet``.

        ``unknown`` re-injects the directive without stopping anyone; ``fleet``
        stops a worker's turn. The safe direction for a wrong answer is therefore
        toward NOT enforcing, so a root that somehow reaches the classifier without
        being usable must not promote a process to the stopping class.
        """
        mod = self._mod()
        proc = self._proc(tmp_path, "8001", b"pytest\x00-n\x004\x00", tmp_path / "elsewhere")
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet")])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        # A root that cannot be spelled the way any cwd is spelled: the comparison
        # cannot establish ownership, so the answer must be the non-stopping one.
        assert mod._owner_class(proc / "8001", ["\0/broken"], "") != "fleet"
        assert mod._program_class("", [str(tmp_path / "fleet")]) == "unknown"
        out = self._run(mod, cfg, capsys)
        assert "cwd=fleet" not in out

    def test_a_nul_in_a_fleet_root_is_malformed_config_not_a_crash(self, tmp_path, monkeypatch):
        """A NUL byte in a path is malformed config, and the contract for this
        script is that typed misconfiguration exits 2 with a message rather than
        raising. ``os.path`` and ``os.readlink`` both raise ``ValueError`` -- not
        ``OSError`` -- on an embedded NUL, so it escapes the scan's error handling
        and takes the whole cycle down, losing every other session's reading with
        it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=["/fleet/wt-\0a"])
        assert mod.main(["--config", str(cfg)]) == 2

    def test_a_dispositioned_err_does_not_bury_an_unanswered_ruling(
        self, tmp_path, capsys, monkeypatch
    ):
        """An answered ERR must not mask a ruling nobody has answered.

        The error branch is checked before the sticky walk, so a session that
        reports ``BLOCKED:`` and then hits a persistent error row reads as ``ERR``.
        Once that ``ERR`` is dispositioned it goes quiet by digest -- and while the
        error row stays last, the unanswered ``BLOCKED`` underneath it never
        surfaces. Sticky ``BLOCKED`` exists precisely so a ruling cannot be lost to
        whatever the session said next.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-buried"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
            {"role": "error", "content": "tool crashed"},
        ]
        self._transcript(sessions, "s-buried", rows)
        out = self._run(mod, cfg, capsys)
        assert "ERR" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-buried", "ERR", self._digest_of(out, "s-buried")
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out, "the unanswered ruling must surface once the ERR is answered"

    def test_a_report_after_a_delivery_notice_clears_the_counter(
        self, tmp_path, capsys, monkeypatch
    ):
        """The delivery counters count UNRECOVERED failures only.

        A session that filed a protocol report after an init-timeout notice
        evidently got a turn through, so the walk stops at that report. Otherwise a
        recovery report that QUOTES the notice keeps its own session in the
        undelivered column, and the counter becomes a permanent accusation instead
        of an admission.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-recovered"])
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-recovered",
            [
                {"role": "user", "content": "seed"},
                {"role": "error", "content": "initialize timed out after 120s"},
                {"role": "assistant", "content": "WORKING: recovered from initialize timed out"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "init-timeout 0" in out, "a report after the notice means it was recovered"

    def _venv(self, root: Path) -> Path:
        """A venv interpreter path with the ``pyvenv.cfg`` marker beside it, which
        is what makes an interpreter attributable to one checkout."""
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        interpreter = root / ".venv" / "bin" / "python"
        interpreter.write_text("", encoding="utf-8")
        return interpreter

    def test_an_unreadable_cwd_falls_back_to_the_program_path(self, tmp_path, capsys, monkeypatch):
        """Both attributable BANNED lines seen in the field had an unreadable cwd
        and a readable cmdline, so cwd alone is not a sufficient ownership signal.

        ``/proc/<pid>/cwd`` needs the access a debugger would have; the cmdline
        does not. A venv interpreter belongs to the checkout that created it, so
        it attributes the process even when the cwd link cannot be followed.
        """
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        theirs = tmp_path / "somebody-else" / "repo"
        theirs.mkdir(parents=True, exist_ok=True)
        their_python = self._venv(theirs)
        # cwd=None is the unreadable case: no symlink for the probe to follow.
        proc = self._proc(
            tmp_path,
            "7001",
            f"{their_python}\0-m\0pytest\0-n4\0test_sandbox_x.py\0".encode(),
            None,
        )
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=7001" not in out, "provably not the fleet's, so not the fleet's problem"
        assert "banned 0 | foreign 1" in out

    def test_a_fleet_interpreter_is_owned_even_with_an_unreadable_cwd(
        self, tmp_path, capsys, monkeypatch
    ):
        """The other direction: a program path UNDER a fleet worktree is
        conclusive, because nothing outside that checkout runs its interpreter."""
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        mine.mkdir(parents=True, exist_ok=True)
        my_python = self._venv(mine)
        proc = self._proc(
            tmp_path, "7002", f"{my_python}\0-m\0pytest\0-n4\0test/x.py\0".encode(), None
        )
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=7002 rule=" in out and "cwd=fleet" in out
        assert "banned 1 | foreign 0" in out

    def test_a_system_interpreter_attributes_nothing_and_stays_unknown(
        self, tmp_path, capsys, monkeypatch
    ):
        """The correction that keeps this refinement from muting the signal it
        exists to sharpen.

        Treating "program path not under a fleet worktree" as ``foreign`` looks
        symmetric and is not. A fleet worktree here has NO ``.venv`` and its
        workers run a global ``python3`` shim, so a real banned run INSIDE the
        fleet has a program path outside every worktree. Calling that ``foreign``
        would drop it silently -- the exact harm 2d exists to prevent -- so a
        shared interpreter yields ``unknown`` and the match is still reported.
        """
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        shim = tmp_path / "shims" / "python3"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text("", encoding="utf-8")
        proc = self._proc(tmp_path, "7003", f"{shim}\0-m\0pytest\0-n4\0test/x.py\0".encode(), None)
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=7003 rule=" in out and "cwd=unknown" in out
        assert "banned 1 | foreign 0" in out, "no venv means no evidence, not evidence of absence"

    def test_the_program_path_is_read_for_a_decision_and_never_printed(
        self, tmp_path, capsys, monkeypatch
    ):
        """Reading argv is not printing it. The fallback compares the program
        path; a secret riding in an ARGUMENT must still never reach the output."""
        mod = self._mod()
        theirs = tmp_path / "somebody-else" / "repo"
        theirs.mkdir(parents=True, exist_ok=True)
        their_python = self._venv(theirs)
        secret = "hunter2-do-not-print"
        proc = self._proc(
            tmp_path,
            "7004",
            f"{their_python}\0-m\0pytest\0--token={secret}\0-n4\0".encode(),
            None,
        )
        cfg = self._config(
            tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet" / "wt-a")]
        )
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert secret not in out
        assert str(their_python) not in out, "the program path decides; it is not emitted"
        assert "foreign 1" in out

    def test_a_subdirectory_of_a_fleet_worktree_is_fleet_owned(self, tmp_path, capsys, monkeypatch):
        """Workers run tests from inside the tree, not at its root, so the
        comparison is prefix-wise -- and on a path BOUNDARY, so a sibling named
        ``wt-a-old`` is not swallowed by ``wt-a``."""
        mod = self._mod()
        root = tmp_path / "fleet" / "wt-a"
        proc = self._proc(
            tmp_path, "6001", b"pytest\x00-n4\x00test/x.py\x00", root / "test" / "deep"
        )
        self._proc(
            tmp_path, "6002", b"pytest\x00-n4\x00test/y.py\x00", tmp_path / "fleet" / "wt-a-old"
        )
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(root)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=6001" in out and "cwd=fleet" in out
        assert "pid=6002" not in out
        assert "banned 1 | foreign 1" in out

    def test_a_windows_path_spelling_still_matches_its_fleet_root(self, tmp_path, monkeypatch):
        """Two spellings of one directory must not read as two directories.

        This class of bug is invisible on Linux and misfiled EVERY match on the
        Windows lane: ``os.readlink`` there can answer an extended-length path
        (``\\\\?\\D:\\...``) that no configured root will ever spell. Misfiling
        is not cosmetic -- a fleet-owned banned run reported as ``foreign`` is
        not printed at all, so the conductor never learns of it.

        Only the platform-independent halves are asserted here. Stripping the
        extended prefix is this script's own work, so it is pinned directly. Case
        and separator folding is delegated to ``os.path.normcase``, which is a
        no-op on Linux by design, so what is pinned is the DELEGATION rather than
        Windows semantics this runner cannot exhibit.
        """
        mod = self._mod()
        root = r"D:\a\KiroCrew\fleet\wt-a"
        assert mod._norm_path("\\\\?\\" + root) == mod._norm_path(root)
        assert mod._norm_path("\\\\?\\" + root) == os.path.normcase(os.path.normpath(root))
        # Trailing separators and redundant segments fold on every platform.
        assert mod._norm_path("/fleet/wt-a/") == mod._norm_path("/fleet/wt-a")
        assert mod._norm_path("/fleet/./wt-a") == mod._norm_path("/fleet/wt-a")
        assert mod._norm_path("/fleet/x/../wt-a") == mod._norm_path("/fleet/wt-a")

    def test_a_sibling_root_is_not_swallowed_by_a_prefix_match(self, tmp_path, monkeypatch):
        """The boundary test is a separator, not a bare prefix: ``wt-a-old`` is
        not inside ``wt-a``, and attributing its runs to ``wt-a`` would stop the
        wrong worker."""
        mod = self._mod()
        root = mod._norm_path("/fleet/wt-a")
        assert mod._under(mod._norm_path("/fleet/wt-a"), root)
        assert mod._under(mod._norm_path("/fleet/wt-a/test/deep"), root)
        assert not mod._under(mod._norm_path("/fleet/wt-a-old"), root)
        assert not mod._under(mod._norm_path("/fleet/wt-ab"), root)

    def test_a_symlinked_fleet_root_still_matches(self, tmp_path, capsys, monkeypatch):
        """A worktree reached through a symlink is the same worktree. The literal
        comparison cannot see that, so the classifier gets a second chance
        through ``realpath`` -- a match missed here means a banned run inside the
        fleet is filed as somebody else's and never printed."""
        mod = self._mod()
        real = tmp_path / "real-fleet" / "wt-a"
        real.mkdir(parents=True)
        link = tmp_path / "via-link"
        make_dir_link(link, tmp_path / "real-fleet")
        # The process reports the REAL path; the config names the symlinked one.
        proc = self._proc(tmp_path, "5100", b"pytest\x00-n4\x00test/x.py\x00", real)
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(link / "wt-a")])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=5100" in out and "cwd=fleet" in out
        assert "banned 1 | foreign 0" in out
