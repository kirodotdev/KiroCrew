"""Pipeline-conductor fleet probe: how a worker's tail is read.

Topic classes of ``TestFleetProbe`` in ``test_pipeline_conductor_agent.py``, one per
section of it: the tail index, TERMINAL and sticky BLOCKED, decoration on the protocol
prefix, the delivery counters, NOPROGRESS, and backward compatibility together with the
standing behaviours these must not break. They run the real script over hand-authored
fixtures through ``FleetProbeHarness``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from pipeline_conductor_agent_helpers import FleetProbeHarness


class TestFleetProbeTailIndex(FleetProbeHarness):
    # ── 2a: the tail index ────────────────────────────────────────────────────

    def test_every_fired_line_carries_the_index_before_the_digest(
        self, tmp_path, capsys, monkeypatch
    ):
        """Field ORDER is part of the contract: the conductor's action table
        parses these lines positionally, so ``i=`` goes before ``d=``."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "GREEN: https://x/pull/1 abc123")
        line = next(ln for ln in self._run(mod, cfg, capsys).splitlines() if "s-1" in ln)
        assert re.search(r"\bi=\d+\s+d=\S+", line), line

    def test_an_unchanged_index_across_two_probes_is_no_progress(
        self, tmp_path, capsys, monkeypatch
    ):
        """The index is the no-progress discriminator: it must hold still while
        the session is silent and move the moment it speaks. A wall-clock age
        cannot answer this -- anything that touches the file ages it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-1", "BLOCKED: waiting on a ruling")
        first = self._index_of(self._run(mod, cfg, capsys), "s-1")
        # Same transcript, second probe: no progress, so the index must not move.
        assert self._index_of(self._run(mod, cfg, capsys), "s-1") == first
        with (sessions / "s-1.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "assistant", "content": "BLOCKED: still"}) + "\n")
        assert self._index_of(self._run(mod, cfg, capsys), "s-1") == first + 1

    def test_the_index_stays_monotonic_past_the_parse_cap(self, tmp_path, capsys, monkeypatch):
        """Counted from the START of the file, not the start of the parse window.

        A window-relative count saturates once a transcript passes ``tail_bytes``
        and then stays frozen while the session talks -- reading, at exactly the
        sizes real worker sessions reach, as the deadlock the index exists to
        detect. Here the transcript is deliberately far larger than the cap, and
        the rows are the session's OWN so they count.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-big"], tail_bytes=2000)
        sessions = tmp_path / "sessions"
        rows = [{"role": "assistant", "content": "x" * 200} for _ in range(60)]
        rows.append({"role": "assistant", "content": "PROPOSAL: https://x/issues/1"})
        self._transcript(sessions, "s-big", rows)
        first = self._index_of(self._run(mod, cfg, capsys), "s-big")
        assert first == len(rows) - 1, "the index must count from the file start"
        assert first > 20, "a window-relative count would have saturated well below this"
        with (sessions / "s-big.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "assistant", "content": "PROPOSAL: v2"}) + "\n")
        assert self._index_of(self._run(mod, cfg, capsys), "s-big") == first + 1

    def test_mark_handled_records_the_index_and_keeps_its_signature(
        self, tmp_path, capsys, monkeypatch
    ):
        """``--mark-handled KEY TAG DIGEST`` stays exactly three positional
        arguments; the index rides along in the state entry."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "GREEN: https://x/pull/1 abc123")
        out = self._run(mod, cfg, capsys)
        self._run(mod, cfg, capsys, "--mark-handled", "s-1", "GREEN", self._digest_of(out, "s-1"))
        entry = self._handled(cfg)["s-1"]
        assert entry["index"] == self._index_of(out, "s-1")
        assert entry["tag"] == "GREEN"


class TestFleetProbeTerminal(FleetProbeHarness):
    # ── 2b: TERMINAL ──────────────────────────────────────────────────────────

    def test_a_terminal_report_then_unprefixed_text_is_terminal_not_idle(
        self, tmp_path, capsys, monkeypatch
    ):
        """A worker that filed STANDDOWN and then wrote one unprefixed line is
        FINISHED. Ageing it into IDLE says the opposite, and the two readings
        call for opposite actions -- close the item, or nudge and reclaim it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-done", "STANDDOWN: covered by an already-merged PR")
        out = self._run(mod, cfg, capsys)
        assert "STANDDOWN" in out
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-done",
            "STANDDOWN",
            self._digest_of(out, "s-done"),
        )
        # ... then one unprefixed line, and the clock runs past the idle threshold.
        self._session(sessions, "s-done", "thanks, nothing further from me", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out
        assert "IDLE" not in out

    def test_a_resumed_worker_reporting_working_is_not_terminal(
        self, tmp_path, capsys, monkeypatch
    ):
        """TERMINAL replaces IDLE only for a tail with NO protocol prefix.

        ``WORKING:`` is a protocol message and it means active work. A worker
        that stood down, was re-seeded, and is now reporting WORKING must not
        read as finished: TERMINAL closes the item, so this inversion would
        abandon live work. The non-firing set holds BOTH ``-`` and ``WORKING``,
        which is how they came to share a branch."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-resumed"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-resumed", "STANDDOWN: nothing further")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-resumed",
            "STANDDOWN",
            self._digest_of(out, "s-resumed"),
        )
        # Re-seeded: the worker is working again and says so. APPENDED, because a
        # real transcript only ever grows -- and the index is a count of produced
        # rows, so a rewrite that happened to keep the count would read as a stall.
        self._transcript(
            sessions,
            "s-resumed",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "STANDDOWN: covered by an already-merged PR"},
                {"role": "assistant", "content": "WORKING: re-seeded, reproducing"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" not in out, "an active WORKING report must not read as finished"
        assert "s-resumed" not in out  # WORKING is a heartbeat, not a signal
        # And the clock still governs a WORKING tail that then goes silent.
        self._transcript(
            sessions,
            "s-resumed",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "STANDDOWN: covered by an already-merged PR"},
                {"role": "assistant", "content": "WORKING: re-seeded, reproducing"},
                {"role": "assistant", "content": "WORKING: still reproducing"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out and "TERMINAL" not in out

    # ── 2b extension: sticky BLOCKED ──────────────────────────────────────────

    def test_blocked_survives_the_heartbeats_that_follow_it(self, tmp_path, capsys, monkeypatch):
        """A ruling owed must not be lost to SAMPLING.

        The probe samples; it does not subscribe. The protocol tells a blocked
        worker to keep reporting status, so the worker's own next ``WORKING:``
        overwrites the only message a newest-message classifier reads, and the
        debt becomes invisible on both sides: the worker holds position waiting
        for a ruling, the conductor never learns it owes one. Nothing is
        suppressed here and nothing is deferred -- the signal is simply never
        observed. So ``BLOCKED`` is sticky, and a heartbeat cannot clear it.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocked"])
        self._transcript(
            tmp_path / "sessions",
            "s-blocked",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "BLOCKED: need a ruling on the baseline entry"},
                {"role": "assistant", "content": "WORKING: holding position"},
                {"role": "assistant", "content": "WORKING: still holding"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out, "two heartbeats must not clear a ruling owed"
        # A probe that did NOT mark it handled leaves it owed, so it fires again.
        assert "BLOCKED" in self._run(mod, cfg, capsys)

    def test_a_sticky_blocked_keeps_one_digest_across_new_heartbeats(
        self, tmp_path, capsys, monkeypatch
    ):
        """Sticky must not mean noisy. The digest is keyed on the BLOCKED report's
        own text, so a worker that keeps filing heartbeats does not re-fire the
        same ruling every cycle: the ruling quiets it, and only a genuinely NEW
        blocker re-fires."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocked"])
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling on scope"},
        ]
        self._transcript(sessions, "s-blocked", rows)
        first = self._digest_of(self._run(mod, cfg, capsys), "s-blocked")
        # More heartbeats arrive. Same unanswered blocker, so the same digest.
        self._transcript(
            sessions,
            "s-blocked",
            rows + [{"role": "assistant", "content": "WORKING: holding position"}],
        )
        out = self._run(mod, cfg, capsys)
        assert self._digest_of(out, "s-blocked") == first
        # Delivering the ruling is what quiets it.
        self._run(mod, cfg, capsys, "--mark-handled", "s-blocked", "BLOCKED", first)
        assert "BLOCKED" not in self._run(mod, cfg, capsys)
        # A genuinely new blocker is a new signal.
        self._transcript(
            sessions,
            "s-blocked",
            rows + [{"role": "assistant", "content": "BLOCKED: a second, different ruling"}],
        )
        assert "BLOCKED" in self._run(mod, cfg, capsys)

    def test_a_real_report_clears_a_sticky_blocked(self, tmp_path, capsys, monkeypatch):
        """Only a heartbeat is transparent. A worker that was blocked and then
        files ``PR``/``GREEN``/``STANDDOWN`` has moved on, and continuing to
        demand a ruling for it would manufacture the opposite error.

        What is asserted is the CLEARING, not what fires in its place. A report
        followed by a heartbeat has always been quiet -- the heartbeat is the
        newest message -- and that is pre-existing behaviour this extension does
        not change; ``test_a_real_report_after_a_blocker_fires_on_its_own`` covers
        the case where the report IS the newest message.
        """
        mod = self._mod()
        for tag, line in (
            ("PR", "PR: https://x/pull/9"),
            ("GREEN", "GREEN: https://x/pull/9 abc123 all lanes clean"),
            ("STANDDOWN", "STANDDOWN: item withdrawn"),
        ):
            key = f"s-moved-{tag.lower()}"
            cfg = self._config(tmp_path, monkeypatch, [key])
            self._transcript(
                tmp_path / "sessions",
                key,
                [
                    {"role": "user", "content": "seed"},
                    {"role": "assistant", "content": "BLOCKED: need a ruling"},
                    {"role": "assistant", "content": line},
                    {"role": "assistant", "content": "WORKING: tidying up"},
                ],
            )
            out = self._run(mod, cfg, capsys)
            assert "BLOCKED" not in out, f"{tag} must clear the ruling owed"
            assert key not in out, f"{tag} then a heartbeat is quiet, as it always was"

    def test_a_real_report_after_a_blocker_fires_on_its_own(self, tmp_path, capsys, monkeypatch):
        """The same clearing, with the report as the newest message: the new tag
        fires and the ruling owed is gone."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-unblocked"])
        self._transcript(
            tmp_path / "sessions",
            "s-unblocked",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "BLOCKED: need a ruling"},
                {"role": "assistant", "content": "PR: https://x/pull/9"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "PR" in out and "s-unblocked" in out
        assert "BLOCKED" not in out

    def test_unprefixed_text_does_not_clear_a_sticky_blocked(self, tmp_path, capsys, monkeypatch):
        """Unprefixed text is not a report at all, so it clears nothing -- and it
        must not let a blocked worker age into IDLE, which reads as "nudge me"
        when the truth is "answer me"."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocked"], idle_alert_secs=100)
        self._transcript(
            tmp_path / "sessions",
            "s-blocked",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "BLOCKED: need a ruling"},
                {"role": "assistant", "content": "still waiting, nothing new from me"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        assert "IDLE" not in out and "TERMINAL" not in out

    def test_sticky_blocked_outranks_a_recorded_terminal_disposition(
        self, tmp_path, capsys, monkeypatch
    ):
        """TERMINAL and sticky BLOCKED are deliberately not the same tag: one says
        close me, the other says a ruling is owed. A worker that stood down and
        then found a blocker is owed the ruling, so BLOCKED wins."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-both"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-both", "STANDDOWN: nothing further")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-both",
            "STANDDOWN",
            self._digest_of(out, "s-both"),
        )
        self._transcript(
            sessions,
            "s-both",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "STANDDOWN: nothing further"},
                {"role": "assistant", "content": "BLOCKED: actually, one ruling is needed"},
                {"role": "assistant", "content": "WORKING: holding"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        assert "TERMINAL" not in out


class TestFleetProbeDecoration(FleetProbeHarness):
    # ── 2e extension: decoration on the prefix ────────────────────────────────

    def test_a_bolded_prefix_still_classifies(self, tmp_path, capsys, monkeypatch):
        """A tag matched at position zero is defeated by ANY leading decoration.

        ``**BLOCKED:**`` puts an asterisk where the tag has to be, so the match
        fails and the line falls through as no-prefix -- and on a fresh transcript
        IDLE does not fire either, so the report is not delayed, it is silent.
        Emphasis is ordinary formatting habit rather than a protocol violation, so
        the reader normalises instead of the writer remembering.
        """
        mod = self._mod()
        cases = {
            "s-bold": ("**BLOCKED:** need a ruling on scope", "BLOCKED"),
            "s-quote": ("> BLOCKED: quoted escalation", "BLOCKED"),
            "s-bullet": ("- **GREEN:** https://x/pull/1 abc123", "GREEN"),
            "s-heading": ("### PROPOSAL: https://x/issues/9", "PROPOSAL"),
            "s-numbered": ("1. STANDDOWN: item withdrawn", "STANDDOWN"),
            "s-underscore": ("__PR:__ https://x/pull/2", "PR"),
            "s-ticked": ("`BLOCKED:` need a ruling", "BLOCKED"),
        }
        for key, (line, tag) in cases.items():
            cfg = self._config(tmp_path, monkeypatch, [key])
            self._session(tmp_path / "sessions", key, line)
            out = self._run(mod, cfg, capsys)
            assert tag in out, f"{key}: {line!r} must classify as {tag}"
            assert key in out

    def test_a_bolded_blocked_still_survives_two_heartbeats(self, tmp_path, capsys, monkeypatch):
        """The two rules composing, which is the case that actually goes silent:
        the worker writes a decorated escalation, then obeys the protocol by
        continuing to report status. Emphasis must not lose the tag, and the
        heartbeats must not clear it."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-both"], idle_alert_secs=100)
        self._transcript(
            tmp_path / "sessions",
            "s-both",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "**BLOCKED:** need a ruling before I can push"},
                {"role": "assistant", "content": "WORKING: holding position"},
                {"role": "assistant", "content": "WORKING: still holding"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out
        assert "IDLE" not in out and "TERMINAL" not in out

    def test_decoration_stripping_is_not_a_substring_search(self, tmp_path, capsys, monkeypatch):
        """The anchor SURVIVES normalisation: decoration comes off the front only.
        A bolded protocol word mid-sentence is a worker talking ABOUT a report,
        and tagging it would fire on every message that mentioned one."""
        mod = self._mod()
        for key, line in (
            ("s-mid", "I think **BLOCKED:** is the right call here"),
            ("s-tail", "waiting on review, then **GREEN:** follows"),
            ("s-prose", "the **PR:** convention confuses me"),
        ):
            cfg = self._config(tmp_path, monkeypatch, [key])
            self._session(tmp_path / "sessions", key, line)
            out = self._run(mod, cfg, capsys)
            assert key not in out, f"{line!r} must not fire"
            assert "OK 1 watched, 0 fired" in out

    def test_a_tool_row_with_a_bolded_prefix_still_never_classifies(
        self, tmp_path, capsys, monkeypatch
    ):
        """2e's original requirement, unaffected by normalisation: a tool row is
        not a protocol message however it is formatted."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-tool"])
        self._transcript(
            tmp_path / "sessions",
            "s-tool",
            [
                {"role": "assistant", "content": "reading the diff"},
                {"role": "tool", "content": "🔧 send_message body=**PR:** https://x/pull/3"},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "s-tool" not in out and "PR" not in out
        assert "OK 1 watched, 0 fired" in out

    def test_the_digest_is_computed_over_what_the_worker_wrote(self, tmp_path, capsys, monkeypatch):
        """Normalisation is for MATCHING only. The digest still covers the raw
        text, so a decorated report and an undecorated one with the same words are
        distinct payloads -- and ``--mark-handled`` keeps working, since it
        digests the same raw text the probe printed."""
        mod = self._mod()
        sessions = tmp_path / "sessions"
        cfg_plain = self._config(tmp_path, monkeypatch, ["s-plain"])
        self._session(sessions, "s-plain", "BLOCKED: need a ruling")
        plain = self._digest_of(self._run(mod, cfg_plain, capsys), "s-plain")
        cfg_bold = self._config(tmp_path, monkeypatch, ["s-styled"])
        self._session(sessions, "s-styled", "**BLOCKED:** need a ruling")
        out = self._run(mod, cfg_bold, capsys)
        styled = self._digest_of(out, "s-styled")
        assert styled != plain, "the digest must reflect the text as written"
        # And the mark round-trips on the decorated payload.
        self._run(mod, cfg_bold, capsys, "--mark-handled", "s-styled", "BLOCKED", styled)
        assert "BLOCKED" not in self._run(mod, cfg_bold, capsys)

    def test_terminal_fires_once_then_suppresses_by_digest(self, tmp_path, capsys, monkeypatch):
        """TERMINAL is an ordinary tag in the firing set: dispositioned once, it
        goes quiet, and a NEW payload re-fires like any other."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-done", "PROPOSAL: https://x/issues/9")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "PROPOSAL", self._digest_of(out, "s-done")
        )
        self._session(sessions, "s-done", "handing back", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "TERMINAL", self._digest_of(out, "s-done")
        )
        assert "TERMINAL" not in self._run(mod, cfg, capsys)

    def test_a_non_protocol_disposition_does_not_erase_the_terminal_tag(
        self, tmp_path, capsys, monkeypatch
    ):
        """The handled set keeps ONE entry per key, so a later IDLE or GONE
        disposition must not overwrite the terminal report; otherwise the
        finished worker reads as wedged again on the next cycle."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-done"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-done", "STANDDOWN: item withdrawn")
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-done",
            "STANDDOWN",
            self._digest_of(out, "s-done"),
        )
        self._session(sessions, "s-done", "ok", age_secs=500)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-done", "TERMINAL", self._digest_of(out, "s-done")
        )
        assert self._handled(cfg)["s-done"]["settled"]["tag"] == "STANDDOWN"
        # A fresh unprefixed payload is still TERMINAL, not IDLE.
        self._session(sessions, "s-done", "signing off for real", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out and "IDLE" not in out

    def test_a_non_terminal_report_still_ages_into_idle(self, tmp_path, capsys, monkeypatch):
        """The negative half: WORKING is not terminal, so a silent worker that
        last said WORKING must still raise IDLE."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-wedged"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-wedged", "WORKING: reproducing")
        # WORKING never fires, so dispose of it the way the conductor would:
        # through a probe cycle that records nothing, then let the clock run.
        self._run(mod, cfg, capsys)
        self._session(sessions, "s-wedged", "hmm", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out and "TERMINAL" not in out


class TestFleetProbeDeliveryCounters(FleetProbeHarness):
    # ── 2c: delivery counters ─────────────────────────────────────────────────

    def test_delivery_counters_appear_on_the_ok_line(self, tmp_path, capsys, monkeypatch):
        """Load and memory can both read healthy while the fleet cannot deliver.
        These two counters are the honest admission instrument, so they are
        counted for every watched session -- fired or not."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-init", "s-stall", "s-fine"])
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-init",
            [
                {"role": "assistant", "content": "WORKING: starting"},
                {"role": "error", "content": "initialize timed out on respawn"},
            ],
        )
        self._transcript(
            sessions,
            "s-stall",
            [
                {"role": "assistant", "content": "WORKING: running the suite"},
                {"role": "inject", "content": "[Tool stall - automatic recovery] resume"},
            ],
        )
        self._session(sessions, "s-fine", "WORKING: healthy")
        out = self._run(mod, cfg, capsys)
        assert "deliver init-timeout 1, watchdog 1" in out
        assert "| foreign 0 |" in out  # 2d's counter sits before deliver

    def test_delivery_patterns_are_configurable(self, tmp_path, capsys, monkeypatch):
        mod = self._mod()
        cfg = self._config(
            tmp_path,
            monkeypatch,
            ["s-1"],
            init_timeout_res=["never-delivered-handshake"],
            watchdog_res=["gave-up-waiting"],
        )
        self._transcript(
            tmp_path / "sessions",
            "s-1",
            [
                {"role": "assistant", "content": "WORKING: fine so far"},
                {"role": "user", "content": "never-delivered-handshake"},
                {"role": "inject", "content": "gave-up-waiting"},
            ],
        )
        assert "deliver init-timeout 1, watchdog 1" in self._run(mod, cfg, capsys)

    def test_a_tool_row_never_feeds_the_delivery_counters(self, tmp_path, capsys, monkeypatch):
        """Same rule as classification: an error phrase quoted in a tool card is
        quoted text. Measured over 60 real transcripts, no timeout or
        stall-watchdog line ever landed on a tool row."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._transcript(
            tmp_path / "sessions",
            "s-1",
            [
                {"role": "assistant", "content": "WORKING: grepping"},
                {"role": "tool", "content": "🔧 grep initialize timed out"},
            ],
        )
        assert "deliver init-timeout 0, watchdog 0" in self._run(mod, cfg, capsys)

    def test_a_bad_delivery_regex_is_malformed_config(self, tmp_path, capsys, monkeypatch):
        """Same validation as ``err_res`` / ``banned_process_res``: a bad regex
        is malformed config (exit 2), never a crash mid-cycle."""
        mod = self._mod()
        for key in ("init_timeout_res", "watchdog_res"):
            cfg = self._config(tmp_path, monkeypatch, [], **{key: ["(unclosed"]})
            assert mod.main(["--config", str(cfg)]) == 2, key
            cfg = self._config(tmp_path, monkeypatch, [], **{key: [7]})
            assert mod.main(["--config", str(cfg)]) == 2, key


class TestFleetProbeNoProgress(FleetProbeHarness):
    # ── 2a extension: NOPROGRESS ───────────────────────────────────────────────

    def test_noprogress_fires_when_nothing_came_out_since_the_last_action(
        self, tmp_path, capsys, monkeypatch
    ):
        """The probe answers the no-progress question instead of posing it.

        Printing ``i=`` and expecting a human to diff two cycles is a decision
        living in prose: nothing enforces it, so it may never happen. Here the
        session is held WARM by inbound nudges it never answers -- so the clock
        never trips IDLE -- while producing nothing of its own.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-stalled"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling"},
        ]
        self._transcript(sessions, "s-stalled", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-stalled",
            "BLOCKED",
            self._digest_of(out, "s-stalled"),
        )
        # Nudged twice. The transcript is fresh, so IDLE cannot see this.
        self._transcript(
            sessions,
            "s-stalled",
            rows
            + [
                {"role": "nudge", "content": "resume; re-state your protocol prefix"},
                {"role": "nudge", "content": "still waiting"},
            ],
        )
        # Not yet: a disposition seconds old would fire this on every key every
        # cycle, since nothing has come out in those seconds by definition.
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)
        self._age_mark(cfg, "s-stalled", 500)
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" in out
        assert "IDLE" not in out

    def test_one_produced_row_clears_noprogress(self, tmp_path, capsys, monkeypatch):
        """The negative half: anything the session emits is progress, so a single
        message or one tool call clears it. Otherwise the tag would accuse a
        worker that is simply mid-task."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-moving"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling"},
        ]
        self._transcript(sessions, "s-moving", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-moving",
            "BLOCKED",
            self._digest_of(out, "s-moving"),
        )
        self._transcript(
            sessions,
            "s-moving",
            rows + [{"role": "nudge", "content": "resume"}, {"role": "tool", "content": "🔧 grep"}],
        )
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)

    def test_a_named_tag_outranks_noprogress(self, tmp_path, capsys, monkeypatch):
        """A tag that says WHY always wins: a live sticky ``BLOCKED`` names the
        reason, and reporting NOPROGRESS instead would hide a ruling owed behind
        a weaker observation."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-named"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "WORKING: starting"},
        ]
        self._transcript(sessions, "s-named", rows)
        self._run(mod, cfg, capsys)  # WORKING never fires, so nothing is marked
        # Give it a recorded observation via an IDLE disposition, then a blocker.
        self._transcript(sessions, "s-named", rows, age_secs=500)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-named", "IDLE", self._digest_of(out, "s-named")
        )
        self._transcript(
            sessions,
            "s-named",
            rows + [{"role": "assistant", "content": "BLOCKED: found a real blocker"}],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out and "NOPROGRESS" not in out

    def test_noprogress_is_suppressible_and_needs_a_prior_observation(
        self, tmp_path, capsys, monkeypatch
    ):
        """It is an ordinary tag: dispositioned once it goes quiet. And with no
        recorded observation there is nothing to compare against, so a session the
        conductor has never acted on cannot be stalled yet."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-fresh"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        # Never marked: no recorded index, so no NOPROGRESS however quiet it is.
        self._transcript(
            sessions,
            "s-fresh",
            [{"role": "user", "content": "seed"}, {"role": "nudge", "content": "resume"}],
        )
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)
        # Give it an observation, then stall it, then dispose of the tag.
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-fresh", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-fresh",
            "BLOCKED",
            self._digest_of(out, "s-fresh"),
        )
        self._transcript(sessions, "s-fresh", rows + [{"role": "nudge", "content": "resume"}])
        self._age_mark(cfg, "s-fresh", 500)
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" in out
        self._run(
            mod,
            cfg,
            capsys,
            "--mark-handled",
            "s-fresh",
            "NOPROGRESS",
            self._digest_of(out, "s-fresh"),
        )
        assert "NOPROGRESS" not in self._run(mod, cfg, capsys)

    def test_a_second_disposition_does_not_destroy_the_first(self, tmp_path, capsys, monkeypatch):
        """An answered report must not re-present because a later tag was marked.

        The handled set holds ONE entry per key, so marking a condition tag
        (``IDLE``/``NOPROGRESS``) over an answered payload tag must not overwrite
        the record that the payload was dealt with -- otherwise the answered ruling
        fires again, sending the conductor to re-adjudicate something it has
        already decided. The payload disposition is preserved beside the new
        entry, which is the shape ``proto`` already uses for the terminal reading.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-two"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        rows = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: ruling needed"},
        ]
        self._transcript(sessions, "s-two", rows)
        out = self._run(mod, cfg, capsys)
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-two", "BLOCKED", self._digest_of(out, "s-two")
        )
        assert "BLOCKED" not in self._run(mod, cfg, capsys)
        self._transcript(sessions, "s-two", rows + [{"role": "nudge", "content": "resume"}])
        self._age_mark(cfg, "s-two", 500)
        out = self._run(mod, cfg, capsys)
        assert "NOPROGRESS" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-two", "NOPROGRESS", self._digest_of(out, "s-two")
        )
        assert self._handled(cfg)["s-two"]["settled"]["tag"] == "BLOCKED"
        # The answered ruling stays answered, and the stall stays quiet too.
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" not in out and "NOPROGRESS" not in out
        # A NEW blocker is still a new signal: preservation is not deafness.
        self._transcript(
            sessions,
            "s-two",
            rows + [{"role": "assistant", "content": "BLOCKED: a second, different ruling"}],
        )
        assert "BLOCKED" in self._run(mod, cfg, capsys)

    def test_a_row_quoting_transcript_json_does_not_advance_the_index(
        self, tmp_path, capsys, monkeypatch
    ):
        """The needle matches a row's own opening, not text inside one.

        Nudges quote things, and a nudge that quoted a transcript row would
        otherwise advance the index -- defeating the detector in exactly the
        inbound-row case it guards. Valid JSONL already prevents the obvious
        version (``json.dumps`` escapes the inner quotes), so this pins the
        line-anchoring that makes the count correct WITHOUT relying on that.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-quoted"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        base = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: need a ruling"},
        ]
        self._transcript(sessions, "s-quoted", base)
        first = self._index_of(self._run(mod, cfg, capsys), "s-quoted")
        # A nudge whose CONTENT is a transcript row, quoted verbatim.
        self._transcript(
            sessions,
            "s-quoted",
            base
            + [
                {
                    "role": "nudge",
                    "content": 'your last row was {"role": "assistant", "content": "x"}',
                }
            ],
        )
        assert self._index_of(self._run(mod, cfg, capsys), "s-quoted") == first
        # Raw, unescaped: the needle must still only match a line opening.
        path = sessions / "s-quoted.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "x"}) + "\n")
        assert self._index_of(self._run(mod, cfg, capsys), "s-quoted") == first

    def test_an_inbound_nudge_does_not_advance_the_index(self, tmp_path, capsys, monkeypatch):
        """The index counts what the SESSION produced, never what arrived.

        A transcript holds nudges, injected recovery notices and user turns
        alongside the session's own rows. Counting all of them means the
        conductor's own nudge advances the index of the worker it just nudged, so
        a wedged session reads as progress at exactly the moment the conductor
        pokes it -- the false negative the index exists to prevent. Tool rows DO
        count: a session running tools is working even while it is silent.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-poked"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        base = [
            {"role": "user", "content": "seed"},
            {"role": "assistant", "content": "BLOCKED: waiting on a ruling"},
        ]
        self._transcript(sessions, "s-poked", base)
        first = self._index_of(self._run(mod, cfg, capsys), "s-poked")
        # The conductor nudges it, and a recovery notice is injected. Neither is
        # the worker speaking, so neither is progress.
        self._transcript(
            sessions,
            "s-poked",
            base
            + [
                {"role": "nudge", "content": "resume; re-state your protocol prefix"},
                {"role": "inject", "content": "[Stalled turn - automatic recovery] resume"},
                {"role": "user", "content": "any update?"},
            ],
        )
        assert self._index_of(self._run(mod, cfg, capsys), "s-poked") == first
        # A tool call IS the session working, even with nothing said.
        self._transcript(
            sessions,
            "s-poked",
            base + [{"role": "nudge", "content": "resume"}, {"role": "tool", "content": "🔧 grep"}],
        )
        assert self._index_of(self._run(mod, cfg, capsys), "s-poked") == first + 1

    def test_a_recovered_delivery_failure_stops_being_counted(self, tmp_path, capsys, monkeypatch):
        """The counters answer "is this session undelivered NOW".

        Scanning the window for any historical match keeps a healed session in
        the undelivered column until the notice scrolls out of 200 KB, which
        turns an admission instrument into a permanent accusation. A protocol
        report filed after the notice is proof a turn got through.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-healed"])
        sessions = tmp_path / "sessions"
        self._transcript(
            sessions,
            "s-healed",
            [
                {"role": "error", "content": "initialize timed out on respawn"},
                {"role": "assistant", "content": "still stuck"},
            ],
        )
        assert "deliver init-timeout 1, watchdog 0" in self._run(mod, cfg, capsys)
        # It recovered and said so. The old notice is history, not a live failure.
        self._transcript(
            sessions,
            "s-healed",
            [
                {"role": "error", "content": "initialize timed out on respawn"},
                {"role": "assistant", "content": "WORKING: resumed, re-running the tests"},
            ],
        )
        assert "deliver init-timeout 0, watchdog 0" in self._run(mod, cfg, capsys)

    def test_a_failure_after_the_last_report_is_still_counted(self, tmp_path, capsys, monkeypatch):
        """The negative half: a notice NEWER than the last report is outstanding,
        so recovery is judged by order rather than by mere presence."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-broke"])
        self._transcript(
            tmp_path / "sessions",
            "s-broke",
            [
                {"role": "assistant", "content": "WORKING: running the suite"},
                {"role": "inject", "content": "[Tool stall - automatic recovery] resume"},
            ],
        )
        assert "watchdog 1" in self._run(mod, cfg, capsys)

    def test_the_protocol_regex_is_derived_from_the_tag_set(self, tmp_path, monkeypatch):
        """One list, not two. Every word in ``PROTO_TAGS`` must match in its
        protocol form, and a word that is a PREFIX of another must not swallow it:
        ``PR`` and ``PROPOSAL`` share two characters, so a naive alternation can
        report the shorter one."""
        mod = self._mod()
        for tag in mod.PROTO_TAGS:
            match = mod.PROTO.match(f"{tag}: payload")
            assert match is not None, tag
            assert match.group(1) == tag, f"{tag} matched as {match.group(1)}"
        assert mod.PROTO.match("PROPOSAL: x").group(1) == "PROPOSAL"
        assert mod.PROTO.match("PR: x").group(1) == "PR"

    def test_an_unreadable_cwd_is_unknown_and_still_reported(self, tmp_path, capsys, monkeypatch):
        """A process that exited between the scan and the read, or one owned by
        another user, has no readable cwd. Dropping it would be how a real banned
        run inside the fleet goes unreported, so unknown is reported and
        counted."""
        mod = self._mod()
        proc = self._proc(tmp_path, "7001", b"pytest\x00-n4\x00test/x.py\x00", None)
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(tmp_path / "fleet")])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=7001" in out and "cwd=unknown" in out
        assert "banned 1 | foreign 0" in out

    def test_an_undeclared_fleet_scope_reports_every_match(self, tmp_path, capsys, monkeypatch):
        """No ``fleet_worktrees`` declares no scope. Scoping against an empty set
        would file every match as foreign and mute the banned signal entirely --
        a failure the conductor cannot see -- so unscoped keeps the pre-2d
        behaviour: every match reported, every match counted."""
        mod = self._mod()
        proc = self._proc(
            tmp_path, "8001", b"pytest\x00-n4\x00test/x.py\x00", tmp_path / "anywhere"
        )
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "BANNED pid=8001" in out and "cwd=unknown" in out
        assert "banned 1 | foreign 0" in out

    def test_the_banned_line_still_withholds_argv_under_scoping(
        self, tmp_path, capsys, monkeypatch
    ):
        """A command line can carry credentials or a presigned URL, and this line
        lands in the conductor's model context. Adding the cwd class must not
        smuggle the argv in with it."""
        mod = self._mod()
        mine = tmp_path / "fleet" / "wt-a"
        proc = self._proc(
            tmp_path, "9001", b"pytest\x00-n4\x00--token\x00secret-token\x00test/x.py\x00", mine
        )
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[str(mine)])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        out = self._run(mod, cfg, capsys)
        assert "pid=9001" in out
        assert "secret-token" not in out

    def test_a_relative_fleet_worktree_is_malformed_config(self, tmp_path, capsys, monkeypatch):
        """A relative root can never match an absolute ``/proc/<pid>/cwd``
        target, so every banned run inside the fleet would be filed as foreign.
        Say so at load time instead of silently muting the signal."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=["relative/path"])
        assert mod.main(["--config", str(cfg)]) == 2
        cfg = self._config(tmp_path, monkeypatch, [], fleet_worktrees=[42])
        assert mod.main(["--config", str(cfg)]) == 2


class TestFleetProbeCompatibility(FleetProbeHarness):
    # ── 2f: backward compatibility ────────────────────────────────────────────

    def test_a_state_file_from_the_old_version_still_suppresses(
        self, tmp_path, capsys, monkeypatch
    ):
        """The old handled entry has no ``index`` and no ``settled``. Both are
        additive metadata outside the digest, so an old state file must suppress
        exactly as it did -- missing fields read as absent, never a crash."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"], idle_alert_secs=100)
        self._session(tmp_path / "sessions", "s-1", "GREEN: https://x/pull/1 abc123")
        digest = self._digest_of(self._run(mod, cfg, capsys), "s-1")
        # Hand-write the PRE-2a entry shape, the way the shipped version writes it.
        Path(f"{cfg}.state.json").write_text(
            json.dumps({"handled": {"s-1": {"tag": "GREEN", "digest": digest, "ts": 1}}}),
            encoding="utf-8",
        )
        out = self._run(mod, cfg, capsys)
        assert "GREEN" not in out and "0 fired" in out
        # The old entry carries no `settled`, but its own tag IS the last
        # dispositioned report -- so a delivered worker reads TERMINAL rather than
        # ageing into IDLE and being nudged. The old shape is sufficient for this;
        # what it cannot recover is a terminal report the old writer had already
        # overwritten, which is the loss the field exists to stop going forward.
        self._session(tmp_path / "sessions", "s-1", "quiet now", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "TERMINAL" in out and "IDLE" not in out

    def test_an_old_config_with_no_new_keys_still_runs(self, tmp_path, capsys, monkeypatch):
        """``--config`` keeps its shape: a config written before 2c/2d has no
        delivery patterns and no fleet scope, and must still produce a full OK
        line off the in-code defaults."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        self._session(tmp_path / "sessions", "s-1", "WORKING: fine")
        out = self._run(mod, cfg, capsys)
        assert re.search(
            r"OK 1 watched, 0 fired \| .*\| banned 0 \| foreign 0 \| "
            r"deliver init-timeout 0, watchdog 0",
            out,
        ), out

    # ── standing behaviours these changes must not break ──────────────────────

    def test_a_handled_idle_mark_expires_and_re_alerts(self, tmp_path, capsys, monkeypatch):
        """An IDLE disposition expires: a nudged-but-still-silent worker has to
        re-alert rather than vanish behind its own disposition.

        The session emits one more line after the mark, so the index moves and
        this stays a test of the CLOCK. A session that produces nothing after a
        disposition is the NOPROGRESS case, which is a different tag with the same
        expiry.
        """
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-quiet"], idle_alert_secs=100)
        sessions = tmp_path / "sessions"
        self._session(sessions, "s-quiet", "hmm", age_secs=500)
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-quiet", "IDLE", self._digest_of(out, "s-quiet")
        )
        # One more line: the session HAS produced something, so this is the clock.
        self._transcript(
            sessions,
            "s-quiet",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": "hmm"},
                {"role": "assistant", "content": "still thinking"},
            ],
            age_secs=500,
        )
        out = self._run(mod, cfg, capsys)
        assert "IDLE" in out and "NOPROGRESS" not in out
        self._run(
            mod, cfg, capsys, "--mark-handled", "s-quiet", "IDLE", self._digest_of(out, "s-quiet")
        )
        assert "IDLE" not in self._run(mod, cfg, capsys)  # freshly marked: quiet
        # Age the MARK past the threshold; the worker is still silent.
        state_path = Path(f"{cfg}.state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["handled"]["s-quiet"]["ts"] = int(time.time()) - 500
        state_path.write_text(json.dumps(state), encoding="utf-8")
        assert "IDLE" in self._run(mod, cfg, capsys)

    def test_mark_handled_rejects_a_key_that_is_a_path(self, tmp_path, capsys, monkeypatch):
        """A session key is a filename STEM. The mark path validates it too, or
        the probe's one approved writer becomes an arbitrary-path writer."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-1"])
        assert mod.main(["--config", str(cfg), "--mark-handled", "../../etc/x", "GREEN", "d"]) == 2

    def test_the_ok_line_reports_available_memory(self, tmp_path, capsys, monkeypatch):
        """``mem`` is read from ``MemAvailable`` in the /proc seam, so the host
        posture half of the OK line is exercised rather than assumed."""
        mod = self._mod()
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(
            "MemTotal:       65805864 kB\nMemAvailable:   12582912 kB\n", encoding="ascii"
        )
        cfg = self._config(tmp_path, monkeypatch, [])
        monkeypatch.setenv("KIROCREW_PROBE_PROC_ROOT", str(proc))
        assert "mem 12G" in self._run(mod, cfg, capsys)

    def test_block_shaped_content_classifies_like_a_string(self, tmp_path, capsys, monkeypatch):
        """A row whose ``content`` is a list of text blocks is read by joining the
        blocks. Real transcripts on this host carry plain strings, so this is the
        defensive path -- pinned so a writer that starts emitting blocks does not
        silently read as a session with nothing to say."""
        mod = self._mod()
        cfg = self._config(tmp_path, monkeypatch, ["s-blocks"])
        self._transcript(
            tmp_path / "sessions",
            "s-blocks",
            [
                {"role": "user", "content": "seed"},
                {"role": "assistant", "content": [{"text": "BLOCKED: need a ruling"}]},
            ],
        )
        out = self._run(mod, cfg, capsys)
        assert "BLOCKED" in out and "s-blocks" in out
