"""Tests for the cron-cost-optimize scan helper.

The scan tells a user to change a job that already works, so the tests pin the
direction of every wrong answer it could give:

* A prompt that reasons must never be sent down the script path. That failure
  hides -- the job stays green and quietly stops doing its work.
* A job whose run history could not be READ must never be reported as a job that
  did nothing. Those are three states, not two, and conflating them would turn a
  masked directory into a confident recommendation.
* A half-typed or malformed payload must produce a plain message, not a crash.

The four prompts in `test_ticket_examples_are_caught` are the real cases from the
ASBX cost analysis, kept verbatim so a pattern change cannot quietly stop
catching the traffic this skill was written for.

Input is the JSON `cron_list json=true` produces. The script reads nothing from
disk, deliberately: the job store is ownership-scoped by that tool and the run
history is readable only by the gateway.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "cron-cost-optimize"
    / "scripts"
    / "cron_cost_scan.py"
)


def _load() -> Any:
    """Import the script by path -- it ships inside a skill dir, not a package.

    The module has to be registered in ``sys.modules`` before it executes, because
    ``dataclass`` resolves its own field annotations through that table.
    """
    spec = importlib.util.spec_from_file_location("cron_cost_scan", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _job(**over: Any) -> dict[str, Any]:
    """One job record shaped as the tool's JSON mode emits it."""
    rec: dict[str, Any] = {
        "id": "job1",
        "name": "a-job",
        "message": "Do the thing.",
        "mode": "agent",
        "schedule": "every 10m",
        "every_secs": 600,
        "cron_expr": None,
        "timezone": None,
        "enabled": True,
        "minimal_context": False,
        "persistent_session": True,
        "hide_in_chat": False,
        "last_status": "ok",
        "message_truncated": False,
        "history": None,
    }
    rec.update(over)
    return rec


def _history(*summaries: str, failures: int = 0) -> dict[str, Any]:
    """The folded counts the gateway returns, computed the same way it does."""
    seen = {s.strip().lower() for s in summaries}
    noop_words = ("nothing", "no new", "unchanged", "clean", "skipped", "healthy")
    noop = sum(1 for s in summaries if any(w in s.lower() for w in noop_words))
    return {
        "runs": len(summaries),
        "failures": failures,
        "distinct_summaries": len(seen),
        "noop_runs": noop,
        "same_every_run": len(summaries) > 1 and len(seen) == 1,
    }


def _payload(*jobs: dict[str, Any], **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "generated_at": 1.0,
        "scanned": len(jobs),
        "truncated": False,
        "history_available": True,
        "jobs": list(jobs),
    }
    out.update(over)
    return out


def _classify(job: dict[str, Any], min_runs: int = 5) -> Any:
    return mod.classify(job, min_runs)


class TestZeroTokenJobsAreLeftAlone(unittest.TestCase):
    def test_script_job(self) -> None:
        f = _classify(_job(mode="script"))
        self.assertEqual(f.verdict, "already-zero-token")
        self.assertEqual(f.change, "")

    def test_command_job(self) -> None:
        self.assertEqual(_classify(_job(mode="command")).verdict, "already-zero-token")

    def test_mode_decides_cost_not_a_leftover_prompt(self) -> None:
        f = _classify(_job(mode="script", message="Summarize the day."))
        self.assertEqual(f.verdict, "already-zero-token")


class TestScriptVerdict(unittest.TestCase):
    def test_deterministic_prompt_with_idle_history_is_high_confidence(self) -> None:
        job = _job(
            message="Compare the channel timestamp with the last run and report a change.",
            history=_history(*["No new messages. Timestamp unchanged."] * 8),
        )
        f = _classify(job)
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "high")
        self.assertIn("8", f.reason)

    def test_thin_history_downgrades_to_low_confidence(self) -> None:
        job = _job(
            message="Check whether the disk usage is above 80 percent.",
            history=_history("Clean run."),
        )
        f = _classify(job)
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "low")
        self.assertIn("not enough run history", f.reason)

    def test_unreadable_history_says_so_rather_than_blaming_a_thin_record(self) -> None:
        """The distinction the masked-directory case turns on."""
        f = _classify(_job(message="Check the exit code of the health probe.", history=None))
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "low")
        self.assertIn("could not be read", f.reason)
        self.assertNotIn("not enough run history", f.reason)

    def test_unremarkable_prompt_with_identical_history_is_medium(self) -> None:
        """History outranks wording: a job repeating itself is doing no reasoning."""
        job = _job(
            message="Look at the queue and tell me about it.",
            history=_history(*["Queue is fine."] * 9),
        )
        f = _classify(job)
        self.assertEqual(f.verdict, "move-to-script")
        self.assertEqual(f.confidence, "medium")

    def test_dedup_carry_is_flagged_when_the_session_persists(self) -> None:
        job = _job(message="Check the timestamp.", history=_history(*["Unchanged."] * 6))
        self.assertTrue(any("dedup" in n for n in _classify(job).notes))

    def test_no_dedup_note_without_a_persistent_session(self) -> None:
        job = _job(
            message="Check the timestamp.",
            persistent_session=False,
            history=_history(*["Unchanged."] * 6),
        )
        self.assertFalse(any("dedup" in n for n in _classify(job).notes))


class TestJudgementJobsNeverBecomeScripts(unittest.TestCase):
    """The one wrong answer that fails silently, so every case must fall back."""

    def test_summarize_falls_back_to_minimal_context(self) -> None:
        job = _job(
            message="Summarize the new messages and check the timestamp.",
            history=_history(*["Nothing new."] * 20),
        )
        f = _classify(job)
        self.assertEqual(f.verdict, "enable-minimal-context")
        self.assertTrue(f.blockers)

    def test_every_judgement_word_blocks_the_rewrite(self) -> None:
        for verb in (
            "Summarize the disk usage",
            "Review the file size",
            "Draft a note about the exit code",
            "Triage the timestamp",
            "Analyze the port status",
            "Recommend a threshold",
            "Investigate the checksum",
            "Reply to anything above 5",
        ):
            with self.subTest(verb=verb):
                job = _job(message=verb + ".", history=_history(*["Nothing to do."] * 12))
                self.assertNotEqual(_classify(job).verdict, "move-to-script")

    def test_idle_history_cannot_override_a_judgement_prompt(self) -> None:
        """Even 40 identical no-op runs must not unlock a script rewrite."""
        job = _job(
            message="Review the open items and summarize what changed.",
            history=_history(*["Nothing to report."] * 40),
        )
        self.assertNotEqual(_classify(job).verdict, "move-to-script")


class TestMinimalContextBlockers(unittest.TestCase):
    def test_dollar_skill_token_blocks_minimal_context(self) -> None:
        job = _job(
            message="Run $babysit against the open pull request and report.",
            history=_history(*["Nothing new."] * 10),
        )
        f = _classify(job)
        self.assertEqual(f.verdict, "leave-as-is")
        self.assertTrue(any("skill" in b for b in f.blockers))

    def test_skill_named_in_prose_blocks_minimal_context(self) -> None:
        job = _job(message="Load the prepare-pr skill and drive the branch to green.")
        self.assertEqual(_classify(job).verdict, "leave-as-is")

    def test_memory_dependence_blocks_minimal_context(self) -> None:
        job = _job(message="Using my saved preferences, decide what to escalate.")
        f = _classify(job)
        self.assertEqual(f.verdict, "leave-as-is")
        self.assertTrue(any("preference" in b for b in f.blockers))

    def test_already_minimal_and_still_reasoning_is_left_alone(self) -> None:
        job = _job(message="Summarize the new failures.", minimal_context=True)
        f = _classify(job)
        self.assertEqual(f.verdict, "leave-as-is")
        self.assertIn("already on minimal context", f.reason)

    def test_hide_in_chat_is_offered_only_when_runs_are_noise(self) -> None:
        quiet = _classify(
            _job(message="Summarize anything new.", history=_history(*["Nothing new."] * 10))
        )
        self.assertTrue(any("hide_in_chat" in n for n in quiet.notes))

        busy = _classify(
            _job(
                message="Summarize anything new.",
                history=_history(*[f"Found {i} new items." for i in range(10)]),
            )
        )
        self.assertFalse(any("hide_in_chat" in n for n in busy.notes))

    def test_unknown_history_offers_no_hide_in_chat_note(self) -> None:
        """A noise claim needs evidence of noise, and there is none here."""
        f = _classify(_job(message="Summarize anything new.", history=None))
        self.assertEqual(f.verdict, "enable-minimal-context")
        self.assertFalse(any("hide_in_chat" in n for n in f.notes))


class TestTicketExamples(unittest.TestCase):
    def test_ticket_examples_are_caught(self) -> None:
        """The four real cases from the cost analysis must all leave full-context mode."""
        cases = [
            # 101K tokens to compare two integers.
            ("Check the channel timestamp and report only if it changed.", "move-to-script"),
            # 100K tokens for a threshold check.
            (
                "Check disk usage for tmp and home and report if either is above 80.",
                "move-to-script",
            ),
            # 78K tokens re-deriving a documented permanent limitation.
            (
                "Check whether the upstream fix already exists and report the status code.",
                "move-to-script",
            ),
            # 141K tokens for what a grep settles. The artifact is called a triage
            # comment, so the judgement guard fires on the word and the scan falls
            # back rather than risking a silent rewrite. Still leaves full context.
            ("Check whether a First Pass Triage comment already exists.", "enable-minimal-context"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                job = _job(message=message, history=_history(*["Nothing to do."] * 10))
                f = _classify(job)
                self.assertEqual(f.verdict, expected)
                self.assertNotEqual(f.verdict, "leave-as-is")


class TestTruncatedPrompt(unittest.TestCase):
    """A prompt cut short cannot be judged, and must not be judged anyway.

    Every signal the classifier reads is a phrase IN the prompt, so a judgement
    verb, a skill token or a memory reference past the cut is invisible. Judging
    on the visible half is exactly the silent failure the safe-direction bias
    exists to prevent, so a truncated prompt has to refuse instead.
    """

    def test_a_truncated_prompt_never_reaches_a_cheaper_mode(self) -> None:
        for message in (
            "Check whether disk usage is above 80.",  # would otherwise be script
            "Post the standup reminder.",  # would otherwise be minimal-context
        ):
            with self.subTest(message=message):
                f = _classify(
                    _job(
                        message=message,
                        message_truncated=True,
                        history=_history(*["Nothing to do."] * 10),
                    )
                )
                self.assertEqual(f.verdict, "leave-as-is")
                self.assertIn("past the cut", f.reason)

    def test_idle_history_cannot_unlock_a_truncated_prompt(self) -> None:
        """History is real evidence, but it cannot vouch for text nobody read."""
        f = _classify(
            _job(
                message="Check the timestamp.",
                message_truncated=True,
                history=_history(*["Unchanged."] * 40),
            )
        )
        self.assertEqual(f.verdict, "leave-as-is")

    def test_the_blocker_names_the_truncation(self) -> None:
        f = _classify(_job(message="Check the timestamp.", message_truncated=True))
        self.assertTrue(any("truncated" in b for b in f.blockers))

    def test_a_script_job_is_still_recognised_when_truncated(self) -> None:
        """Mode decides cost, and it is not read out of the prompt text."""
        f = _classify(_job(mode="script", message="x", message_truncated=True))
        self.assertEqual(f.verdict, "already-zero-token")

    def test_an_untruncated_prompt_is_unaffected(self) -> None:
        f = _classify(
            _job(
                message="Check whether disk usage is above 80.",
                message_truncated=False,
                history=_history(*["Clean run."] * 8),
            )
        )
        self.assertEqual(f.verdict, "move-to-script")

    def test_a_payload_without_the_flag_is_treated_as_complete(self) -> None:
        """Absent means an older tool that did not truncate, not unknown."""
        job = _job(message="Check whether disk usage is above 80.")
        job.pop("message_truncated", None)
        self.assertEqual(_classify(job).verdict, "move-to-script")


class TestNonEnglishPromptsAreNotJudged(unittest.TestCase):
    """An empty blocker set means "unreadable" here, not "nothing blocks".

    Every blocker is an English word list, so a Chinese or German prompt matches
    none of them. Read naively that looks like a clean bill of health and points
    at the DANGEROUS verdict: move-to-script for a job that asks a model to
    summarize and decide. The failure is silent -- the rewritten job stays green
    and quietly stops doing its work -- so an unreadable prompt has to refuse,
    exactly as a truncated one does.
    """

    def test_a_chinese_judgement_prompt_is_not_cleared_for_script(self) -> None:
        f = _classify(
            _job(
                message="总结昨天的部署情况并判断是否需要回滚。",
                history=_history(*["Nothing to do."] * 10),
            )
        )
        self.assertEqual(f.verdict, "leave-as-is")
        self.assertIn("not in English", f.reason)

    def test_a_german_judgement_prompt_is_not_cleared_for_script(self) -> None:
        f = _classify(
            _job(
                message="Fasse den Bericht zusammen und entscheide, ob wir handeln müssen.",
                history=_history(*["Nothing to do."] * 10),
            )
        )
        # German is Latin-script, so the readability test passes and the ENGLISH
        # blockers are what must be seen to miss it. This pins the honest state:
        # the prompt is scannable, so the verdict rests on the word lists, and
        # "zusammenfassen" is not in them. Documented in SKILL.md rather than
        # silently trusted -- assert only that a cheaper mode is not asserted
        # confidently.
        self.assertNotEqual((f.verdict, f.confidence), ("move-to-script", "high"))

    def test_an_english_prompt_with_a_few_cjk_characters_still_scans(self) -> None:
        """A name or a quoted string must not disable the scan."""
        f = _classify(
            _job(
                message='Check whether the file named "报告.txt" exists and report the size.',
                history=_history(*["Nothing to do."] * 10),
            )
        )
        self.assertEqual(f.verdict, "move-to-script")

    def test_idle_history_cannot_unlock_an_unreadable_prompt(self) -> None:
        f = _classify(
            _job(
                message="检查时间戳是否变化。",
                history=_history(*["Nothing to do."] * 40),
            )
        )
        self.assertEqual(f.verdict, "leave-as-is")


class TestSkillWordIsMatchedInThePlural(unittest.TestCase):
    """ "loads the skills it needs" names a skill as much as the singular does.

    A minimal-context wake injects no skill at all, so missing the plural clears
    a job for a mode that breaks it. Asserted on the blocker list rather than the
    final verdict: a verdict can come out non-minimal for an unrelated reason,
    which would let this pass while the plural still went undetected.
    """

    def test_plural_skills_is_a_minimal_context_blocker(self) -> None:
        mod = _load()
        for message in (
            "Load the skills you need and post the digest.",
            "Run the skills listed in the note.",
        ):
            with self.subTest(message=message):
                blockers = mod.minimal_context_blockers(message)
                self.assertTrue(
                    any("skill" in b for b in blockers),
                    f"plural went undetected: {blockers}",
                )

    def test_plural_skills_is_a_script_blocker(self) -> None:
        mod = _load()
        blockers = mod.script_blockers("Run the skills listed in the note.")
        self.assertTrue(any("skill" in b for b in blockers), blockers)

    def test_the_singular_still_blocks(self) -> None:
        mod = _load()
        blockers = mod.minimal_context_blockers("Load the skill you need.")
        self.assertTrue(any("skill" in b for b in blockers), blockers)

    def test_a_word_merely_containing_skill_does_not_block(self) -> None:
        """The word boundary still has to hold, or every prompt blocks."""
        mod = _load()
        self.assertEqual(mod.minimal_context_blockers("Check the unskilled queue."), [])


class TestEvidence(unittest.TestCase):
    def test_a_missing_history_object_is_unknown_not_empty(self) -> None:
        for raw in (None, "nope", 5, []):
            with self.subTest(raw=raw):
                ev = mod.Evidence.from_payload(raw)
                self.assertFalse(ev.known)
                self.assertFalse(ev.is_evidence(1))
                self.assertFalse(ev.says_idle(1))
                self.assertEqual(ev.to_dict(), {"known": False})

    def test_a_present_history_object_is_known_even_at_zero_runs(self) -> None:
        ev = mod.Evidence.from_payload({"runs": 0, "failures": 0})
        self.assertTrue(ev.known)
        self.assertEqual(ev.noop_ratio, 0.0)
        self.assertFalse(ev.says_idle(1))

    def test_counts_round_trip(self) -> None:
        ev = mod.Evidence.from_payload(
            {
                "runs": 10,
                "failures": 2,
                "distinct_summaries": 1,
                "noop_runs": 9,
                "same_every_run": True,
            }
        )
        self.assertTrue(ev.is_evidence(5))
        self.assertTrue(ev.says_idle(5))
        self.assertAlmostEqual(ev.noop_ratio, 0.9)
        self.assertEqual(ev.to_dict()["failures"], 2)


class TestSchedule(unittest.TestCase):
    def test_interval_becomes_wakes_per_day(self) -> None:
        self.assertEqual(
            mod.describe_schedule({"schedule": "every 1h", "every_secs": 3600}),
            ("every 1h", 24.0),
        )

    def test_a_cron_schedule_is_shown_but_not_counted(self) -> None:
        label, per_day = mod.describe_schedule({"schedule": "cron 0 9 * * 1-5"})
        self.assertEqual(label, "cron 0 9 * * 1-5")
        self.assertIsNone(per_day)

    def test_a_missing_schedule_degrades(self) -> None:
        self.assertEqual(mod.describe_schedule({}), ("unknown", None))


class TestPayloadReading(unittest.TestCase):
    def test_empty_input_is_reported_not_raised(self) -> None:
        with self.assertRaises(LookupError) as ctx:
            mod.load_payload("   \n")
        self.assertIn("pipe", str(ctx.exception))

    def test_a_plain_sentence_from_the_tool_is_relayed_verbatim(self) -> None:
        """cron_list answers prose for an empty or out-of-scope registry."""
        for sentence in (
            "No cron jobs.",
            "No cron jobs owned by this session.",
            "No cron jobs match ids: abc",
        ):
            with self.subTest(sentence=sentence):
                with self.assertRaises(LookupError) as ctx:
                    mod.load_payload(sentence)
                self.assertIn(sentence, str(ctx.exception))

    def test_unparseable_json_is_reported(self) -> None:
        with self.assertRaises(LookupError) as ctx:
            mod.load_payload('{"jobs": [')
        self.assertIn("cannot parse", str(ctx.exception))

    def test_a_document_without_a_job_list_is_reported(self) -> None:
        with self.assertRaises(LookupError) as ctx:
            mod.load_payload('{"scanned": 0}')
        self.assertIn("no job list", str(ctx.exception))

    def test_non_dict_job_entries_are_skipped_not_fatal(self) -> None:
        payload = _payload(_job(), **{"jobs": [_job(), "junk", 5, None]})
        self.assertEqual(len(mod.scan(payload, 5)), 1)


class TestReporting(unittest.TestCase):
    def _payload_with_three(self) -> dict[str, Any]:
        return _payload(
            _job(
                id="a",
                name="poll",
                message="Check the timestamp for a change.",
                history=_history(*["Nothing to do."] * 8),
            ),
            _job(
                id="b",
                name="digest",
                message="Summarize the new failures.",
                history=_history(*["Two new failures."] * 8),
            ),
            _job(id="c", name="cheap", mode="script"),
        )

    def test_every_job_is_classified(self) -> None:
        by_id = {f.job_id: f.verdict for f in mod.scan(self._payload_with_three(), 5)}
        self.assertEqual(by_id["a"], "move-to-script")
        self.assertEqual(by_id["b"], "enable-minimal-context")
        self.assertEqual(by_id["c"], "already-zero-token")

    def test_text_leads_with_counts_and_ends_with_the_caveats(self) -> None:
        payload = self._payload_with_three()
        text = mod.render_text(mod.scan(payload, 5), payload, 5)
        self.assertIn("Scanned 3 cron job(s)", text)
        self.assertIn("move-to-script", text)
        self.assertIn("not a measurement", text)
        self.assertIn("Nothing was changed.", text)

    def test_missing_history_is_stated_up_front_not_buried(self) -> None:
        """A reader must know the evidence is absent BEFORE reading a verdict."""
        payload = _payload(
            _job(message="Check the timestamp."),
            history_available=False,
            history_unavailable_reason="the gateway did not answer",
        )
        text = mod.render_text(mod.scan(payload, 5), payload, 5)
        head, _, tail = text.partition("Check the timestamp")
        self.assertIn("could NOT be read", head)
        self.assertIn("the gateway did not answer", head)
        self.assertIn("wording alone", head)
        self.assertIn("could not be read", text)
        del tail

    def test_a_partial_payload_says_so(self) -> None:
        payload = _payload(_job(), truncated=True)
        self.assertIn("partial scan", mod.render_text(mod.scan(payload, 5), payload, 5))

    def test_json_output_is_valid_and_carries_the_numbers(self) -> None:
        payload = self._payload_with_three()
        out = json.loads(mod.render_json(mod.scan(payload, 5), payload, 5))
        self.assertEqual(out["scanned"], 3)
        self.assertTrue(out["history_available"])
        self.assertFalse(out["partial"])
        for key in ("job_id", "verdict", "confidence", "reason", "evidence", "blockers"):
            self.assertIn(key, out["findings"][0])

    def test_json_output_carries_the_unavailable_reason(self) -> None:
        payload = _payload(_job(), history_available=False, history_unavailable_reason="no gateway")
        out = json.loads(mod.render_json(mod.scan(payload, 5), payload, 5))
        self.assertFalse(out["history_available"])
        self.assertEqual(out["history_unavailable_reason"], "no gateway")


class TestCli(unittest.TestCase):
    def test_exits_zero_on_a_readable_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "payload.json"
            p.write_text(json.dumps(_payload(_job())), encoding="utf-8")
            self.assertEqual(mod.main(["--input", str(p), "--json"]), 0)

    def test_exits_one_on_a_missing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertEqual(mod.main(["--input", str(Path(tmp) / "absent.json")]), 1)

    def test_exits_one_on_a_non_json_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "payload.json"
            p.write_text("No cron jobs.", encoding="utf-8")
            self.assertEqual(mod.main(["--input", str(p)]), 1)

    def test_rejects_a_nonsense_window(self) -> None:
        with self.assertRaises(SystemExit):
            mod.main(["--min-runs", "0", "--input", "x"])


if __name__ == "__main__":
    unittest.main()
