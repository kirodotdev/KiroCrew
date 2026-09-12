"""New-user friction channel: entries, ledger, rendering, issue plan, harness routing -- all offline."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from gui_user import friction, harness, report, scenarios

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def _raw(**over) -> dict:
    base = {
        "surface": "Settings page",
        "element": "Developer section header",
        "what_confused": "I did not expect feature previews to live under Developer.",
        "expected": "A section named Previews near the top",
        "actual": "It was the last item, under Developer",
        "severity": "slows-down",
    }
    base.update(over)
    return base


def _entry(**over) -> dict:
    feature = over.pop("feature", "members")
    return friction.validate_entry(
        _raw(**over),
        feature=feature,
        scenario="members-dm-hello",
        screenshot="members-dm-hello/attempt-1/02-left_click.png",
        step=2,
    )


# --------------------------------------------------------------------------
# Persona on the scenario
# --------------------------------------------------------------------------


class TestPersona:
    def test_default_is_new_user_and_arms_the_channel(self) -> None:
        for sc in scenarios.load_all(SCENARIOS_DIR):
            assert sc.persona == scenarios.DEFAULT_PERSONA == "new-user"
            assert sc.reports_friction
            assert "report_friction" in sc.persona_prompt

    def test_none_persona_disarms_the_channel(self) -> None:
        sc = dataclasses.replace(scenarios.load_all(SCENARIOS_DIR)[0], persona="none")
        assert not sc.reports_friction and sc.persona_prompt == ""

    def test_unknown_persona_is_rejected(self, tmp_path: Path) -> None:
        import yaml

        doc = {
            "name": "demo",
            "feature": "settings",
            "user_story": "As a user, I want a thing, so that it is done.",
            "summary": "do a thing",
            "steps": ["click"],
            "expectations": ["clicked"],
            "persona": "power-user",
        }
        p = tmp_path / "demo.yaml"
        p.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(scenarios.ScenarioError, match="'persona' must be one of"):
            scenarios.load_scenario(p)

    def test_system_prompt_splices_the_persona_before_how_to_work(self) -> None:
        sc = scenarios.load_all(SCENARIOS_DIR)[0]
        prompt = harness.system_prompt(sc)
        assert prompt.startswith(harness.SYSTEM_PROMPT.split("\n", 1)[0])
        assert prompt.index("WHO YOU ARE") < prompt.index("HOW TO WORK:")
        assert prompt.index("RULES (non-negotiable") < prompt.index("WHO YOU ARE")
        bare = dataclasses.replace(sc, persona="none")
        assert harness.system_prompt(bare) == harness.SYSTEM_PROMPT

    def test_tools_carry_report_friction_only_for_a_reporting_persona(self) -> None:
        from gui_user import x11

        sc = scenarios.load_all(SCENARIOS_DIR)[0]
        runner = harness.Runner.__new__(harness.Runner)
        runner.tool_mode = "custom"
        runner.display = type("D", (), {"geo": x11.Geometry(1600, 1000, 1280)})()
        names = [t["name"] for t in runner._tools(sc)]
        assert names[-1] == "report_friction" and names.count("report_friction") == 1
        assert "report_friction" not in [
            t["name"] for t in runner._tools(dataclasses.replace(sc, persona="none"))
        ]
        runner.tool_mode = "native"
        assert [t["name"] for t in runner._tools(sc)] == ["computer", "report_friction"]

    def test_custom_action_vocabulary_is_untouched(self) -> None:
        from gui_user import x11

        assert {t["name"] for t in harness.custom_tools()} <= x11.ACTIONS
        assert friction.FRICTION_TOOL["name"] not in x11.ACTIONS


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------


class TestEntries:
    def test_valid_entry_carries_scenario_context_and_a_key(self) -> None:
        e = _entry()
        assert e["feature"] == "members" and e["scenario"] == "members-dm-hello"
        assert e["screenshot"].endswith("02-left_click.png") and e["step"] == 2
        assert e["key"] == friction.entry_key("members", e["element"], e["what_confused"])
        assert set(e) == {
            "feature",
            "scenario",
            "surface",
            "element",
            "what_confused",
            "expected",
            "actual",
            "severity",
            "screenshot",
            "step",
            "key",
        }

    def test_key_ignores_case_whitespace_and_punctuation(self) -> None:
        a = friction.entry_key("chat", "The  Send button!", "I could not find it.")
        b = friction.entry_key("chat", "the send button", "i could NOT find   it")
        assert a == b
        assert friction.entry_key("sidebar", "the send button", "i could not find it") != a

    def test_fields_are_trimmed_and_capped_not_rejected(self) -> None:
        e = _entry(what_confused="  x " * 200)
        assert len(e["what_confused"]) == friction.FIELD_MAX and e["what_confused"].endswith("…")
        assert "\n" not in _entry(surface="line one\nline two")["surface"]

    @pytest.mark.parametrize(
        "over,match",
        [
            ({"severity": "huge"}, "severity"),
            ({"surface": ""}, "must not be empty"),
            ({"element": 7}, "must be a string"),
            ({"bonus": "x"}, "unknown friction fields"),
        ],
    )
    def test_rejects_malformed_input(self, over, match) -> None:
        with pytest.raises(friction.FrictionError, match=match):
            friction.validate_entry(
                _raw(**over), feature="members", scenario="s", screenshot="", step=0
            )

    def test_rejects_non_mapping_and_unknown_feature(self) -> None:
        with pytest.raises(friction.FrictionError, match="mapping"):
            friction.validate_entry("nope", feature="members", scenario="s", screenshot="", step=0)
        with pytest.raises(friction.FrictionError, match="registry slug"):
            friction.validate_entry(
                _raw(), feature="teleporter", scenario="s", screenshot="", step=0
            )

    def test_collect_dedupes_by_key_and_skips_garbage(self) -> None:
        e = _entry()
        summary = {
            "scenarios": [
                {"attempts": [{"friction": [e, e, {"severity": "blocker"}, "junk"]}]},
                {"attempts": [{"friction": [{**e, "key": "other"}]}]},
                "junk",
            ]
        }
        got = friction.collect(summary)
        assert [g["key"] for g in got] == [e["key"], "other"]

    def test_collect_folds_a_repeated_key_to_worst_severity_and_newest_evidence(self) -> None:
        # Attempt 1 called it cosmetic; the retry hit the same confusion and called
        # it a blocker with a fresher screenshot. Cosmetic never files an issue, so
        # first-sighting-wins would have hidden the blocker for a night.
        first = {**_entry(), "severity": "cosmetic", "screenshot": "a1/step-2.png"}
        retry = {**_entry(), "severity": "blocker", "screenshot": "a2/step-1.png"}
        summary = {"scenarios": [{"attempts": [{"friction": [first]}, {"friction": [retry]}]}]}
        (got,) = friction.collect(summary)
        assert got["severity"] == "blocker" and got["screenshot"] == "a2/step-1.png"
        # The other way round the retry is milder: the worst severity is kept, the
        # newest evidence still wins.
        summary = {"scenarios": [{"attempts": [{"friction": [retry]}, {"friction": [first]}]}]}
        (got,) = friction.collect(summary)
        assert got["severity"] == "blocker" and got["screenshot"] == "a1/step-2.png"


# --------------------------------------------------------------------------
# Harness routing
# --------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def write(self, kind: str, **fields) -> int:
        self.records.append((kind, fields))
        return len(self.records)


class TestFrictionCountIsOnlyClaimedWhenTheChannelRan:
    def test_dry_run_or_persona_none_never_claims_the_channel(self) -> None:
        cat = scenarios.load_all(SCENARIOS_DIR)
        assert harness.friction_channel_ran(cat, dry_run=False)
        assert not harness.friction_channel_ran(cat, dry_run=True)
        bare = [dataclasses.replace(sc, persona="none") for sc in cat]
        assert not harness.friction_channel_ran(bare, dry_run=False)
        assert harness.friction_channel_ran([*bare, cat[0]], dry_run=False)
        assert not harness.friction_channel_ran([], dry_run=False)


class TestHarnessRecordsFriction:
    def _runner(self) -> harness.Runner:
        return harness.Runner.__new__(harness.Runner)

    def test_valid_call_is_stored_with_the_last_screenshot(self) -> None:
        sc = scenarios.load_all(SCENARIOS_DIR)[0]
        entries: list[dict] = []
        log = _Log()
        text = self._runner()._record_friction(sc, _raw(), entries, "s/attempt-1/03-x.png", 3, log)
        assert text.startswith("Noted")
        assert len(entries) == 1 and entries[0]["screenshot"] == "s/attempt-1/03-x.png"
        assert entries[0]["feature"] == sc.feature and entries[0]["step"] == 3
        assert log.records[0][0] == "friction"

    def test_duplicate_rejected_and_capped_calls_do_not_fail_the_model(self) -> None:
        sc = scenarios.load_all(SCENARIOS_DIR)[0]
        entries: list[dict] = []
        log = _Log()
        r = self._runner()
        r._record_friction(sc, _raw(), entries, "", 1, log)
        assert r._record_friction(sc, _raw(), entries, "", 2, log).startswith("Already noted")
        assert r._record_friction(sc, _raw(severity="huge"), entries, "", 2, log).startswith(
            "Not recorded"
        )
        for i in range(friction.ATTEMPT_CAP + 2):
            r._record_friction(sc, _raw(what_confused=f"confusion {i}"), entries, "", 3, log)
        assert len(entries) == friction.ATTEMPT_CAP
        assert any(k == "friction_dropped" for k, _ in log.records)

    def test_attempt_result_carries_friction_into_summary(self) -> None:
        att = harness.AttemptResult(
            "PASS", 3, 10.0, 1, 1, 0.01, "VERDICT: PASS", friction=[_entry()]
        )
        doc = json.loads(json.dumps(dataclasses.asdict(att)))
        assert doc["friction"][0]["severity"] == "slows-down"
        assert harness.AttemptResult("ERROR", 0, 0.0, 0, 0, 0.0, "").friction == []


class _FakeDisplay:
    """Enough of ``x11.Display`` for ``Runner.attempt``: actions succeed, screenshots are numbered."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.shots_dir = root
        self.n = 0
        self.actions: list[str] = []

    def reset(self, shots_dir: Path) -> None:
        self.shots_dir = shots_dir
        shots_dir.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def perform(self, action: str, params: dict) -> str:
        self.actions.append(action)
        return f"did {action}"

    def settle(self) -> None:
        pass

    def screenshot(self, label: str = "shot"):
        self.n += 1
        path = self.shots_dir / f"{self.n:02d}-{label}.png"
        path.write_bytes(b"png")
        return b"png", path


class _ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.bodies: list[dict] = []

    def messages(self, body: dict) -> dict:
        self.bodies.append(body)
        return self.responses.pop(0)


def _tool_use(tid: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


class TestBatchedFrictionCitesTheScreenTheModelSaw:
    def test_action_then_friction_in_one_response(self, tmp_path: Path) -> None:
        sc = scenarios.load_all(SCENARIOS_DIR)[0]
        client = _ScriptedClient(
            [
                {
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [
                        _tool_use("a", "left_click", {"coordinate": [1, 2]}),
                        _tool_use("b", "report_friction", _raw()),
                    ],
                },
                {
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [
                        {"type": "text", "text": "VERDICT: PASS\nEXPECTATIONS:\n- x : MET"}
                    ],
                },
            ]
        )
        display = _FakeDisplay(tmp_path)
        runner = harness.Runner(
            client,  # type: ignore[arg-type]
            display,  # type: ignore[arg-type]
            base_url="http://t",
            tool_mode="custom",
            price_in=1.0,
            price_out=1.0,
            budget_usd=5.0,
            out=tmp_path,
        )
        att = runner.attempt(sc, 1)
        assert att.status == "PASS" and att.steps == 1
        assert len(att.friction) == 1
        # The batch was decided from the start screenshot (01-start.png) at step 0; the
        # left_click that precedes the friction call in the batch must not move either.
        assert att.friction[0]["screenshot"].endswith("/01-start.png")
        assert att.friction[0]["step"] == 0
        assert display.actions.count("left_click") == 1
        # The tool result handed back for the friction call is text only (no image).
        # (the harness appends to the same message list, so search rather than index)
        friction_result = next(
            block
            for msg in client.bodies[1]["messages"]
            for block in msg["content"]
            if block.get("type") == "tool_result" and block.get("tool_use_id") == "b"
        )
        assert friction_result["content"] == [
            {"type": "text", "text": "Noted. Continue with the steps."}
        ]
        assert client.bodies[0]["tools"][-1]["name"] == "report_friction"
        assert "WHO YOU ARE" in client.bodies[0]["system"]


# --------------------------------------------------------------------------
# Ledger and issues
# --------------------------------------------------------------------------


def _ledger_two_nights():
    e1 = _entry()
    e2 = _entry(
        feature="chat",
        surface="Chat page",
        element="plus icon",
        what_confused="The plus icon has no label.",
        severity="cosmetic",
    )
    e3 = _entry(
        feature="settings",
        surface="Display",
        element="Theme dropdown",
        what_confused="Nothing changed for a second.",
        severity="blocker",
    )
    led, new1 = friction.merge_ledger(
        friction.empty_ledger(), [e1, e2], date="2026-09-12", run_url="r1", head_sha="aaa"
    )
    led, new2 = friction.merge_ledger(
        led, [e1, e3], date="2026-09-13", run_url="r2", head_sha="bbb"
    )
    return led, (e1, e2, e3), (new1, new2)


class TestLedger:
    def test_merge_counts_recurrences_and_keeps_the_worst_severity(self) -> None:
        led, (e1, e2, e3), (new1, new2) = _ledger_two_nights()
        assert set(new1) == {e1["key"], e2["key"]} and new2 == [e3["key"]]
        row = led["entries"][e1["key"]]
        assert row["count"] == 2 and row["first_seen"] == "2026-09-12"
        assert (
            row["last_seen"] == "2026-09-13" and row["run_url"] == "r2" and row["head_sha"] == "bbb"
        )
        led, _ = friction.merge_ledger(
            led, [{**e1, "severity": "blocker"}], date="2026-09-14", run_url="r3"
        )
        assert led["entries"][e1["key"]]["severity"] == "blocker"
        led, _ = friction.merge_ledger(
            led, [{**e1, "severity": "cosmetic"}], date="2026-09-15", run_url="r4"
        )
        assert led["entries"][e1["key"]]["severity"] == "blocker"
        # A recurrence is taken whole: a renamed surface or re-spelt wording (same key)
        # never sits beside the previous night's screenshot.
        moved = {
            **e1,
            "surface": "Settings › Developer",
            "element": "DEVELOPER section header",
            "what_confused": "I did not expect feature previews to live under Developer!",
            "screenshot": "x/attempt-1/09-scroll.png",
        }
        assert moved["key"] == e1["key"]
        led, _ = friction.merge_ledger(led, [moved], date="2026-09-16")
        row = led["entries"][e1["key"]]
        assert (
            row["surface"] == "Settings › Developer"
            and row["element"] == "DEVELOPER section header"
        )
        assert (
            row["what_confused"].endswith("!") and row["screenshot"] == "x/attempt-1/09-scroll.png"
        )

    def test_same_run_merged_twice_is_idempotent(self) -> None:
        e = _entry()
        led, _ = friction.merge_ledger(
            friction.empty_ledger(), [e], date="2026-09-12", run_url="r1"
        )
        led, new = friction.merge_ledger(led, [e], date="2026-09-12", run_url="r1")
        assert new == [] and led["entries"][e["key"]]["count"] == 1

    def test_another_run_on_the_same_day_refreshes_evidence_without_recounting(self) -> None:
        # Nightly first, then a dispatch the same day that re-hits the key: the row must
        # carry the dispatch's screenshot / artifact / head (what its report links to),
        # but the day is still counted once.
        e = _entry()
        led, _ = friction.merge_ledger(
            friction.empty_ledger(),
            [e],
            date="2026-09-12",
            run_url="r-night",
            artifact_url="a-night",
            head_sha="aaa",
        )
        again = {**e, "screenshot": "s/attempt-1/07-left_click.png", "actual": "seen again"}
        led, new = friction.merge_ledger(
            led,
            [again],
            date="2026-09-12",
            run_url="r-dispatch",
            artifact_url="a-dispatch",
            head_sha="bbb",
        )
        row = led["entries"][e["key"]]
        assert new == [] and row["count"] == 1 and row["last_seen"] == "2026-09-12"
        assert (
            row["screenshot"] == "s/attempt-1/07-left_click.png" and row["actual"] == "seen again"
        )
        assert row["run_url"] == "r-dispatch" and row["artifact_url"] == "a-dispatch"
        assert row["head_sha"] == "bbb"

    def test_tonight_lists_only_rows_seen_on_that_date_in_registry_then_severity_order(
        self,
    ) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        rows = friction.tonight(led, "2026-09-13")
        assert [r["key"] for r in rows] == [e1["key"], e3["key"]]  # members before settings
        assert friction.tonight(led, "2026-09-12")[-1]["key"] == e2["key"]

    def test_rows_for_scopes_to_one_runs_keys_not_the_date(self) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        # Same date as e1/e3 (the nightly), but this run only saw e3.
        assert [r["key"] for r in friction.rows_for(led, [e3["key"]])] == [e3["key"]]
        assert friction.rows_for(led, []) == []
        # Catalog order (chat before members), whatever order the keys were given in.
        assert [r["key"] for r in friction.rows_for(led, [e1["key"], e2["key"]])] == [
            e2["key"],
            e1["key"],
        ]

    def test_load_ledger_shapes(self, tmp_path: Path) -> None:
        assert friction.load_ledger(None) == friction.empty_ledger()
        assert friction.load_ledger(tmp_path / "missing.json") == friction.empty_ledger()
        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        with pytest.raises(friction.FrictionError, match="unexpected shape"):
            friction.load_ledger(bad)
        bad.write_text(json.dumps({"version": 1, "entries": {"k": {"severity": "huge"}}}))
        with pytest.raises(friction.FrictionError, match="malformed"):
            friction.load_ledger(bad)
        bad.write_text("{not json")
        with pytest.raises(friction.FrictionError):
            friction.load_ledger(bad)


class TestIssues:
    def test_plan_skips_cosmetic_caps_new_and_comments_recurrences(self) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        # Every un-issued non-cosmetic row is a candidate whatever night it was last seen
        # (a row an earlier cap held over is not lost); cosmetic e2 never leaves the summary.
        assert friction.plan_issues(led, date="2026-09-12") == {
            "create": [e1["key"], e3["key"]],
            "comment": [],
        }
        # Tonight's sightings come first, each group in registry-then-severity order.
        plan = friction.plan_issues(led, date="2026-09-13")
        assert plan == {"create": [e1["key"], e3["key"]], "comment": []}
        led["entries"][e3["key"]]["last_seen"] = "2026-09-10"  # held over, not seen tonight
        assert friction.plan_issues(led, date="2026-09-13")["create"] == [e1["key"], e3["key"]]
        led["entries"][e3["key"]]["last_seen"] = "2026-09-13"
        assert friction.plan_issues(led, date="2026-09-13", cap=1)["create"] == [e1["key"]]
        led["entries"][e1["key"]]["issue"] = 42
        plan = friction.plan_issues(led, date="2026-09-13")
        assert plan == {"create": [e3["key"]], "comment": [e1["key"]]}
        # A same-day rerun after the comment was posted must not post it again.
        led["entries"][e1["key"]]["last_commented"] = "2026-09-13"
        assert friction.plan_issues(led, date="2026-09-13")["comment"] == []
        led["entries"][e1["key"]]["last_commented"] = "2026-09-12"
        assert friction.plan_issues(led, date="2026-09-13")["comment"] == [e1["key"]]

    def test_file_issues_uses_gh_and_records_numbers(self) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        led["entries"][e1["key"]]["issue"] = 42
        calls: list[list[str]] = []

        def fake(args: list[str]) -> str:
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                return '{"state": "OPEN"}'
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                return "https://github.com/o/r/issues/91\n"
            return ""

        saved: list[int] = []
        out = friction.file_issues(
            led, repo="o/r", date="2026-09-13", run=fake, persist=lambda led: saved.append(1)
        )
        assert out == {"opened": [91], "adopted": [], "commented": [42], "failed": []}
        assert len(saved) == 2  # once per successful gh mutation
        assert led["entries"][e1["key"]]["last_commented"] == "2026-09-13"
        assert led["entries"][e3["key"]]["issue"] == 91
        create = next(c for c in calls if c[:2] == ["issue", "create"])
        assert (
            "--label" in create and friction.LABEL_UX in create and friction.LABEL_CHANNEL in create
        )
        assert "area: dashboard" in create  # settings -> default area
        assert create[create.index("--title") + 1].startswith("ux(settings): Nothing changed")
        body = create[create.index("--body") + 1]
        assert body.startswith(f"{friction.ISSUE_MARKER}{e3['key']} -->")
        comment = next(c for c in calls if c[:2] == ["issue", "comment"])
        assert comment[2] == "42" and "Again on 2026-09-13" in comment[comment.index("--body") + 1]
        # Label creation failing (no permission / exists) must not stop the filing.
        labels = [c for c in calls if c[:2] == ["label", "create"]]
        assert len(labels) == 2

    def test_label_create_failure_is_tolerated(self) -> None:
        led, (e1, _, _), _ = _ledger_two_nights()

        def fake(args: list[str]) -> str:
            if args[:2] == ["label", "create"]:
                raise subprocess.CalledProcessError(1, args)
            if args[:2] == ["issue", "list"]:
                return "[]"
            return "https://github.com/o/r/issues/5\n"

        out = friction.file_issues(led, repo="o/r", date="2026-09-13", run=fake)
        assert out["opened"] == [5, 5]

    def test_failure_mid_batch_keeps_the_mappings_already_made(self) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        snapshots: list[dict] = []
        calls = {"create": 0}

        def flaky(args: list[str]) -> str:
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "create"]:
                calls["create"] += 1
                if calls["create"] == 2:
                    raise subprocess.CalledProcessError(1, args)
                return "https://github.com/o/r/issues/7\n"
            return ""

        out = friction.file_issues(
            led,
            repo="o/r",
            date="2026-09-13",
            run=flaky,
            persist=lambda led: snapshots.append(json.loads(json.dumps(led))),
        )
        assert out["opened"] == [7] and [f["op"] for f in out["failed"]] == ["create"]
        # The first mapping was persisted BEFORE the second create failed ...
        assert snapshots and snapshots[0]["entries"][e1["key"]]["issue"] == 7
        # ... so a rerun files only the one that failed.
        assert friction.plan_issues(led, date="2026-09-13")["create"] == [e3["key"]]

    def test_recurrence_on_a_closed_issue_opens_a_fresh_one(self) -> None:
        led, (e1, _, e3), _ = _ledger_two_nights()
        led["entries"][e1["key"]]["issue"] = 42
        led["entries"][e3["key"]]["issue"] = 43  # filed by tonight's earlier attempt, still open
        calls: list[list[str]] = []

        def fake(args: list[str]) -> str:
            if args[:2] == ["issue", "list"]:
                return "[]"
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                return '{"state": "CLOSED"}' if args[2] == "42" else '{"state": "OPEN"}'
            if args[:2] == ["issue", "create"]:
                return "https://github.com/o/r/issues/99\n"
            return ""

        out = friction.file_issues(led, repo="o/r", date="2026-09-13", run=fake)
        assert out == {"opened": [99], "adopted": [], "commented": [], "failed": []}
        row = led["entries"][e1["key"]]
        assert row["issue"] == 99 and row["closed_issues"] == [42]
        # Every row seen tonight that has an issue is checked -- a same-day rerun's
        # fresh sighting must not stay mapped to a thread a human closed meanwhile.
        assert sorted(c[2] for c in calls if c[:2] == ["issue", "view"]) == ["42", "43"]
        assert led["entries"][e3["key"]]["issue"] == 43
        create = next(c for c in calls if c[:2] == ["issue", "create"])
        assert "earlier issue(s): #42" in create[create.index("--body") + 1]

    def test_unknown_issue_state_keeps_the_comment_path(self) -> None:
        led, (e1, _, _), _ = _ledger_two_nights()
        led["entries"][e1["key"]]["issue"] = 42

        def fake(args: list[str]) -> str:
            if args[:2] == ["issue", "list"]:
                return "[]"
            if args[:2] == ["issue", "view"]:
                raise subprocess.CalledProcessError(1, args)
            if args[:2] == ["issue", "create"]:
                return "https://github.com/o/r/issues/5\n"
            return ""

        out = friction.file_issues(led, repo="o/r", date="2026-09-13", run=fake)
        assert out["commented"] == [42] and led["entries"][e1["key"]]["issue"] == 42

    def test_area_label_mapping_names_only_registry_slugs(self) -> None:
        assert friction.AREA_LABELS["schedule"] == "area: cron"
        assert friction.AREA_LABELS["apps"] == "area: apps"
        assert friction.DEFAULT_AREA_LABEL == "area: dashboard"
        # A key the registry lacks could never fire; the registry is the one taxonomy.
        assert set(friction.AREA_LABELS) <= set(scenarios.FEATURES)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


class TestRendering:
    def test_section_groups_by_feature_and_orders_by_severity(self) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        led["entries"][e1["key"]]["issue"] = 42
        md = friction.render_section(friction.tonight(led, "2026-09-13"), artifact_url="https://a")
        assert md.startswith("## New-user friction\n")
        assert "1 blocker · 1 slows-down · 0 cosmetic" in md
        assert md.index("### Crew Members (`members`)") < md.index("### Settings (`settings`)")
        assert "| slows-down |" in md and "| blocker |" in md
        assert "2× · last 2026-09-13 · #42" in md and "| new |" in md
        assert "[`members-dm-hello/attempt-1/02-left_click.png`](https://a)" in md

    def test_empty_section_says_so(self) -> None:
        assert "reported nothing confusing" in friction.render_section([])

    def test_cells_are_inert(self) -> None:
        e = _entry(what_confused="see `code` | <b>bold</b> [link](x) @maintainer ```")
        md = friction.render_section([e])
        row = next(ln for ln in md.splitlines() if ln.startswith("| slows-down |"))
        cell = row.split("|")[2]
        assert "`" not in cell and "<b>" not in cell and "[link](x)" not in cell
        assert "@\u200bmaintainer" in cell and "```" not in cell

    def test_cells_defang_urls_the_tester_read_off_the_screen(self) -> None:
        e = _entry(
            what_confused="A banner said go to https://evil.example/x or www.evil.example now"
        )
        md = friction.render_section([e])
        assert "https://" not in md and "www." not in md
        assert "https:\u200b//evil.example/x" in md and "www\u200b.evil.example" in md
        body = friction.issue_body(e["key"], {**e, "count": 1, "first_seen": "d"})
        assert "https://evil" not in body and "https:\u200b//evil.example/x" in body

    def test_report_markdown_appends_the_section_when_the_channel_ran(self) -> None:
        e = _entry()
        summary = {
            "scenarios": [
                {
                    "name": "members-dm-hello",
                    "feature": "members",
                    "status": "PASS",
                    "attempts": [
                        {"status": "PASS", "final_text": "VERDICT: PASS", "friction": [e]}
                    ],
                }
            ],
            "usage": {},
            "friction_count": 1,
        }
        md = report.render_markdown(summary, artifact_url="https://a")
        assert "## New-user friction" in md and e["what_confused"] in md
        # Ledger-aware rows replace the run's own when supplied.
        md2 = report.render_markdown(
            summary, friction_entries=[{**e, "count": 3, "last_seen": "d"}]
        )
        assert "3× · last d" in md2
        # A pre-channel summary renders as before.
        summary.pop("friction_count")
        assert "New-user friction" not in report.render_markdown(summary)
        assert "new-user friction: 1 slows-down" in report.render_console(
            {**summary, "friction_count": 1, "usd": 0}
        )

    def test_cli_merge_render_issues_dry_run(self, tmp_path: Path, capsys) -> None:
        e = _entry()
        summary = {
            "scenarios": [
                {
                    "name": "members-dm-hello",
                    "feature": "members",
                    "status": "PASS",
                    "attempts": [{"status": "PASS", "friction": [e]}],
                }
            ],
            "friction_count": 1,
        }
        s = tmp_path / "summary.json"
        s.write_text(json.dumps(summary), encoding="utf-8")
        out = tmp_path / "friction.json"
        led = tmp_path / "ledger.json"
        assert (
            friction.main(
                [
                    "merge",
                    "--summary",
                    str(s),
                    "--out",
                    str(out),
                    "--out-ledger",
                    str(led),
                    "--date",
                    "2026-09-12",
                    "--run-url",
                    "r",
                    "--head-sha",
                    "aaa",
                ]
            )
            == 0
        )
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["new_keys"] == [e["key"]] and doc["entries"][0]["count"] == 1
        assert doc["run_keys"] == [e["key"]]
        assert "1 friction entry, 1 new" in capsys.readouterr().out
        assert (
            friction.main(
                [
                    "issues",
                    "--ledger",
                    str(led),
                    "--repo",
                    "o/r",
                    "--date",
                    "2026-09-12",
                    "--artifact-url",
                    "https://a",
                    "--friction",
                    str(out),
                    "--dry-run",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out) == {"create": [e["key"]], "comment": []}
        # snapshot rewrites tonight's rows from the ledger and keeps the run's provenance.
        led_doc = json.loads(led.read_text(encoding="utf-8"))
        led_doc["entries"][e["key"]]["issue"] = 77
        # A row another run saw on the same date must not leak into this run's snapshot.
        other = {**led_doc["entries"][e["key"]], "issue": None, "scenario": "elsewhere"}
        led_doc["entries"]["deadbeefdeadbeef"] = other
        led.write_text(json.dumps(led_doc), encoding="utf-8")
        assert (
            friction.main(
                ["snapshot", "--ledger", str(led), "--out", str(out), "--date", "2026-09-12"]
            )
            == 0
        )
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert [r["key"] for r in doc["entries"]] == [e["key"]]
        assert (
            doc["entries"][0]["issue"] == 77 and doc["run_url"] == "r" and doc["head_sha"] == "aaa"
        )
        assert "snapshotted" in capsys.readouterr().out
        assert (
            friction.main(
                [
                    "merge",
                    "--summary",
                    str(tmp_path / "nope.json"),
                    "--out",
                    str(out),
                    "--out-ledger",
                    str(led),
                    "--date",
                    "d",
                ]
            )
            == 2
        )


REPO = "kirodotdev/KiroCrew"


def _run(run_id: int, **over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "id": run_id,
        "path": friction.WORKFLOW_PATH,
        "event": "schedule",
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
        "created_at": f"2026-09-{run_id % 28 + 1:02d}T09:30:00Z",
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
    }
    doc.update(over)
    return doc


def _artifact(run_id: int, art_id: int = 900, **over: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "id": art_id,
        "name": friction.LEDGER_ARTIFACT,
        "expired": False,
        "workflow_run": {"id": run_id, "head_branch": "main"},
    }
    doc.update(over)
    return doc


class TestLedgerProvenance:
    """The previous ledger comes ONLY from a scheduled run of this workflow on the
    trusted branch. Every other origin -- another workflow, a pull_request or
    dispatch run, a fork, a run that did not complete -- is rejected, however
    its artifact is named."""

    def test_source_runs_are_newest_first_and_exclude_self(self) -> None:
        runs = [_run(3), _run(5, created_at="2026-09-20T09:30:00Z"), _run(4)]
        assert friction.ledger_source_runs(runs, repo=REPO, branch="main") == [5, 4, 3]
        assert friction.ledger_source_runs(runs, repo=REPO, branch="main", exclude_run_id=5) == [
            4,
            3,
        ]

    @pytest.mark.parametrize(
        "over",
        [
            {"path": ".github/workflows/ci.yml"},  # another workflow (a PR lane)
            {"event": "pull_request"},  # wrong event: fork-reachable
            {"event": "workflow_dispatch"},  # a maintainer's ad-hoc run is not the nightly
            {"head_branch": "feat/anything"},  # untrusted branch
            {"head_repository": {"full_name": "someone/KiroCrew"}},  # fork head
            {"repository": {"full_name": "someone/KiroCrew"}},  # foreign base
            {"head_repository": None},  # provenance missing is provenance wrong
            {"status": "in_progress", "conclusion": None},  # not finished
            {"conclusion": "skipped"},  # the job never ran
            {"status": "completed", "conclusion": None},
            {"id": "12"},  # malformed id
        ],
    )
    def test_every_provenance_field_must_agree(self, over: dict[str, object]) -> None:
        bad = _run(9, **over)
        assert friction.ledger_source_runs([bad, _run(2)], repo=REPO, branch="main") == [2]

    def test_junk_in_the_listing_is_dropped_not_guessed_at(self) -> None:
        assert friction.ledger_source_runs(
            ["not-a-dict", None, {}, _run(2)], repo=REPO, branch="main"
        ) == [2]

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
    def test_a_red_or_cut_short_nightly_still_counts_as_a_source(self, conclusion: str) -> None:
        # A FAIL verdict, a cancellation or a timeout all land AFTER the ledger was
        # written and uploaded; refusing such a run would re-file its issues.
        assert friction.ledger_source_runs(
            [_run(7, conclusion=conclusion)], repo=REPO, branch="main"
        ) == [7]

    def test_artifact_must_belong_to_that_run_and_branch(self) -> None:
        assert friction.ledger_artifact_id([_artifact(7)], run_id=7, branch="main") == 900
        for bad in (
            _artifact(8),  # another run's artifact in the listing
            _artifact(7, workflow_run={"id": 7, "head_branch": "feat/x"}),
            _artifact(7, name="gui-user-test-123"),  # the screenshots artifact
            _artifact(7, expired=True),
            _artifact(7, workflow_run=None),
            _artifact(7, id="900"),
        ):
            assert friction.ledger_artifact_id([bad], run_id=7, branch="main") is None

    def test_pick_walks_newest_first_and_skips_runs_without_a_ledger(self) -> None:
        calls: list[str] = []

        def fetch(path: str) -> object:
            calls.append(path)
            if path == "actions/runs/99":  # tonight's first attempt: no ledger yet
                return _run(99, status="in_progress", conclusion=None)
            if path == f"actions/runs/99/artifacts?name={friction.LEDGER_ARTIFACT}":
                return {"artifacts": []}
            if path.startswith("actions/workflows/gui-user-test.yml/runs?"):
                assert "event=schedule" in path and "branch=main" in path
                return {"workflow_runs": [_run(4), _run(6), _run(5, event="pull_request")]}
            if path == f"actions/runs/6/artifacts?name={friction.LEDGER_ARTIFACT}":
                return {"artifacts": []}  # that night's merge step failed: nothing uploaded
            if path == f"actions/runs/4/artifacts?name={friction.LEDGER_ARTIFACT}":
                return {"artifacts": [_artifact(4, 41)]}
            raise AssertionError(path)

        assert friction.pick_ledger(repo=REPO, branch="main", self_run_id=99, fetch=fetch) == {
            "run_id": 4,
            "artifact_id": 41,
        }
        assert not any("/runs/5/" in c for c in calls)  # the PR run's artifacts are never listed

    def test_a_rerun_reads_its_own_earlier_attempt_before_last_night(self) -> None:
        # Attempt 1 of tonight's scheduled run filed issues and uploaded the ledger
        # that records them; attempt 2 must start from THAT, not from last night's
        # ledger, or it files every one of them again.
        calls: list[str] = []

        def fetch(path: str) -> object:
            calls.append(path)
            if path == "actions/runs/99":
                return _run(99, status="in_progress", conclusion=None)
            if path == f"actions/runs/99/artifacts?name={friction.LEDGER_ARTIFACT}":
                return {"artifacts": [_artifact(99, 991)]}
            raise AssertionError(path)

        assert friction.pick_ledger(repo=REPO, branch="main", self_run_id=99, fetch=fetch) == {
            "run_id": 99,
            "artifact_id": 991,
        }
        assert not any("workflows/" in c for c in calls)

    @pytest.mark.parametrize(
        "me",
        [
            _run(99, event="workflow_dispatch", status="in_progress", conclusion=None),
            _run(99, head_branch="feat/x", status="in_progress", conclusion=None),
            _run(99, head_repository={"full_name": "someone/KiroCrew"}),
            {"id": 99},
            None,
        ],
    )
    def test_an_ineligible_current_run_never_supplies_its_own_ledger(self, me: object) -> None:
        # A dispatch or PR run never wrote a ledger; even if an artifact of that
        # name sat on it, provenance says no and the walk continues to the nightlies.
        def fetch(path: str) -> object:
            if path == "actions/runs/99":
                return me
            if path == f"actions/runs/99/artifacts?name={friction.LEDGER_ARTIFACT}":
                raise AssertionError("must not list an untrusted run's artifacts")
            if path.startswith("actions/workflows/"):
                return {"workflow_runs": [_run(4)]}
            if path == f"actions/runs/4/artifacts?name={friction.LEDGER_ARTIFACT}":
                return {"artifacts": [_artifact(4, 41)]}
            raise AssertionError(path)

        assert friction.pick_ledger(repo=REPO, branch="main", self_run_id=99, fetch=fetch) == {
            "run_id": 4,
            "artifact_id": 41,
        }

    def test_pick_with_no_eligible_run_is_a_first_night_not_an_error(self) -> None:
        assert friction.pick_ledger(
            repo=REPO,
            branch="main",
            self_run_id=None,
            fetch=lambda path: {"workflow_runs": [_run(1, event="pull_request")]},
        ) == {"run_id": None, "artifact_id": None}

    def test_pick_fails_closed_on_api_errors_and_malformed_answers(self) -> None:
        def boom(path: str) -> object:
            raise subprocess.CalledProcessError(1, ["gh", "api", path])

        with pytest.raises(subprocess.CalledProcessError):
            friction.pick_ledger(repo=REPO, branch="main", self_run_id=None, fetch=boom)
        with pytest.raises(friction.FrictionError):
            friction.pick_ledger(
                repo=REPO, branch="main", self_run_id=None, fetch=lambda p: {"message": "oops"}
            )
        with pytest.raises(friction.FrictionError):
            friction.pick_ledger(
                repo=REPO,
                branch="main",
                self_run_id=None,
                fetch=lambda p: {"workflow_runs": [_run(1)]} if "workflows/" in p else {},
            )

    def test_cli_writes_the_pick_and_fails_closed(
        self, monkeypatch, capsys, tmp_path: Path
    ) -> None:
        answers = {
            f"repos/{REPO}/actions/runs/99": _run(99, status="in_progress", conclusion=None),
            f"repos/{REPO}/actions/runs/99/artifacts?name={friction.LEDGER_ARTIFACT}": {
                "artifacts": []
            },
            f"repos/{REPO}/actions/workflows/gui-user-test.yml/runs"
            "?event=schedule&branch=main&status=completed&per_page=100&page=1": {
                "workflow_runs": [_run(4)]
            },
            f"repos/{REPO}/actions/runs/4/artifacts?name={friction.LEDGER_ARTIFACT}": {
                "artifacts": [_artifact(4, 41)]
            },
        }
        monkeypatch.setattr(friction, "_gh", lambda args: json.dumps(answers[args[1]]))
        out = tmp_path / "pick.json"
        argv = ["pick-ledger", "--repo", REPO, "--branch", "main", "--out", str(out)]
        assert friction.main([*argv, "--self-run", "99"]) == 0
        assert json.loads(out.read_text(encoding="utf-8")) == {"run_id": 4, "artifact_id": 41}
        # Nothing the API answered is echoed into the job log.
        assert capsys.readouterr().out.strip() == "previous ledger found"

        def boom(args: list[str]) -> str:
            raise subprocess.CalledProcessError(1, ["gh", *args])

        monkeypatch.setattr(friction, "_gh", boom)
        assert friction.main(argv) == 2
        # The failed run left the earlier answer untouched.
        assert json.loads(out.read_text(encoding="utf-8")) == {"run_id": 4, "artifact_id": 41}


class TestAtomicWrite:
    def test_write_replaces_the_target_only_when_complete(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        target = tmp_path / "friction_ledger.json"
        friction._write(target, {"v": 1})
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
        assert [p.name for p in tmp_path.iterdir()] == ["friction_ledger.json"]  # no .tmp left

        # A write that dies mid-way (disk full) leaves the previous document intact.
        def cut_short(_tmp: object, _target: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(friction.os, "replace", cut_short)
        with pytest.raises(OSError):
            friction._write(target, {"v": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
        assert [p.name for p in tmp_path.iterdir()] == ["friction_ledger.json"]


class TestReconcileAndBounds:
    def test_an_open_issue_carrying_the_marker_is_adopted_not_duplicated(self) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        calls: list[list[str]] = []

        def fake(args: list[str]) -> str:
            calls.append(args)
            if args[:2] == ["issue", "view"]:
                return '{"state": "OPEN"}'
            if args[:2] == ["issue", "list"]:
                key = args[args.index("--search") + 1].split(" ")[0]
                if key == e3["key"]:
                    # A stray mention of the digest in prose is NOT the marker.
                    return json.dumps(
                        [
                            {"number": 8, "body": f"talks about {key} somewhere"},
                            {"number": 9, "body": f"x {friction.ISSUE_MARKER}{key} --> y"},
                        ]
                    )
                return "[]"
            if args[:2] == ["issue", "create"]:
                return "https://github.com/o/r/issues/91\n"
            return ""

        out = friction.file_issues(led, repo="o/r", date="2026-09-13", run=fake)
        assert out["adopted"] == [9] and led["entries"][e3["key"]]["issue"] == 9
        assert 91 in out["opened"]
        created = [c for c in calls if c[:2] == ["issue", "create"]]
        assert all(e3["key"] not in c[c.index("--body") + 1] for c in created)

    def test_a_failed_reconcile_skips_the_key_rather_than_risking_a_duplicate(self) -> None:
        led, (e1, _, e3), _ = _ledger_two_nights()

        def fake(args: list[str]) -> str:
            if args[:2] == ["issue", "list"]:
                raise subprocess.CalledProcessError(1, args)
            if args[:2] == ["issue", "create"]:
                raise AssertionError("must not create after a failed reconcile")
            return ""

        out = friction.file_issues(led, repo="o/r", date="2026-09-13", run=fake)
        assert out["opened"] == [] and {f["op"] for f in out["failed"]} == {"reconcile"}
        assert led["entries"][e3["key"]].get("issue") is None

    def test_section_stays_within_budget_and_counts_the_rest(self) -> None:
        big = "x" * friction.FIELD_MAX
        rows = [
            {
                **_entry(),
                "key": f"k{i:03d}",
                "what_confused": f"{big[:-4]}{i:04d}",
                "surface": big,
                "element": big,
                "expected": big,
                "actual": big,
                "severity": "blocker" if i % 2 else "slows-down",
            }
            for i in range(80)
        ]
        section = friction.render_section(rows)
        assert len(section) <= friction.SECTION_MAX_CHARS
        assert "more row(s) not shown" in section
        # The worst rows are the ones shown: the 40 blockers alone overrun the
        # budget, so every shown row is a blocker and no slows-down made it in.
        assert "| blocker |" in section and "| slows-down |" not in section
        # A small set renders whole, no trailer.
        assert "more row(s)" not in friction.render_section(rows[:3])


class TestIssueReferenceDefang:
    def test_hash_number_never_autolinks(self) -> None:
        cell = friction._cell("saw #123 and kirodotdev/KiroCrew#45 but #hashtag stays")
        assert "#\u200b123" in cell and "#\u200b45" in cell
        assert "#123" not in cell and "#45" not in cell
        assert "#hashtag" in cell


class TestLedgerLookupPagination:
    def test_walks_past_a_full_page_of_ledgerless_nights(self) -> None:
        # 100 nightlies whose merge step failed sit in front of the one that
        # still holds a ledger: a single page would have called this a first night.
        pages = {
            1: [_run(1000 - i) for i in range(100)],
            2: [_run(900), _run(899)],
        }
        calls: list[str] = []

        def fetch(path: str) -> object:
            calls.append(path)
            if path.startswith("actions/workflows/"):
                page = int(path.rsplit("page=", 1)[1])
                return {"workflow_runs": pages.get(page, [])}
            if path.endswith(f"/artifacts?name={friction.LEDGER_ARTIFACT}"):
                run_id = int(path.split("/")[2])
                return {"artifacts": [_artifact(899, 8990)] if run_id == 899 else []}
            raise AssertionError(path)

        assert friction.pick_ledger(repo=REPO, branch="main", self_run_id=None, fetch=fetch) == {
            "run_id": 899,
            "artifact_id": 8990,
        }
        assert sum(1 for c in calls if "page=" in c) == 2

    def test_a_short_page_ends_the_walk_and_the_walk_is_bounded(self) -> None:
        seen: list[int] = []

        def fetch(path: str) -> object:
            if path.startswith("actions/workflows/"):
                seen.append(int(path.rsplit("page=", 1)[1]))
                return {"workflow_runs": [_run(1, event="pull_request")]}  # short, nothing eligible
            raise AssertionError(path)

        assert friction.pick_ledger(repo=REPO, branch="main", self_run_id=None, fetch=fetch) == {
            "run_id": None,
            "artifact_id": None,
        }
        assert seen == [1]

        def endless(path: str) -> object:
            if path.startswith("actions/workflows/"):
                return {"workflow_runs": [_run(1, event="pull_request")] * 100}
            raise AssertionError(path)

        friction.pick_ledger(repo=REPO, branch="main", self_run_id=None, fetch=endless)
        # bounded: MAX_RUN_PAGES pages at most, never an unbounded crawl
        assert friction.MAX_RUN_PAGES == 5


class TestArtifactUrlStamp:
    def test_only_the_runs_own_keys_are_stamped(self, tmp_path: Path, monkeypatch, capsys) -> None:
        led, (e1, e2, e3), _ = _ledger_two_nights()
        for e in (e1, e2, e3):
            led["entries"][e["key"]]["last_seen"] = "2026-09-13"
        ledger = tmp_path / "friction_ledger.json"
        ledger.write_text(json.dumps(led), encoding="utf-8")
        own = tmp_path / "friction.json"
        own.write_text(json.dumps({"run_keys": [e1["key"]]}), encoding="utf-8")
        rc = friction.main(
            [
                "issues",
                "--ledger",
                str(ledger),
                "--repo",
                "o/r",
                "--date",
                "2026-09-13",
                "--artifact-url",
                "https://a/b",
                "--friction",
                str(own),
                "--dry-run",
            ]
        )
        assert rc == 0
        capsys.readouterr()
        # --dry-run returns before the ledger is rewritten, so read the in-memory
        # effect through a second, non-dry invocation with a fake gh.
        monkeypatch.setattr(
            friction, "_gh", lambda args: "[]" if args[:2] == ["issue", "list"] else ""
        )
        friction.main(
            [
                "issues",
                "--ledger",
                str(ledger),
                "--repo",
                "o/r",
                "--date",
                "2026-09-13",
                "--artifact-url",
                "https://a/b",
                "--friction",
                str(own),
            ]
        )
        saved = json.loads(ledger.read_text(encoding="utf-8"))
        assert saved["entries"][e1["key"]]["artifact_url"] == "https://a/b"
        assert saved["entries"][e2["key"]].get("artifact_url") != "https://a/b"
        assert saved["entries"][e3["key"]].get("artifact_url") != "https://a/b"
        # Without the run's own keys nothing is stamped: refused, not guessed.
        assert (
            friction.main(
                [
                    "issues",
                    "--ledger",
                    str(ledger),
                    "--repo",
                    "o/r",
                    "--date",
                    "2026-09-13",
                    "--artifact-url",
                    "https://a/b",
                ]
            )
            == 2
        )


class TestRerunAcrossMidnight:
    def test_the_same_run_is_never_a_new_night_even_after_the_date_changed(self) -> None:
        e = _entry()
        led, _ = friction.merge_ledger(
            friction.empty_ledger(), [e], date="2026-09-12", run_url="https://r/1"
        )
        row = led["entries"][e["key"]]
        assert row["count"] == 1 and row["last_seen"] == "2026-09-12"
        # Attempt 2 of the same run fires after midnight UTC: same run_url, new date.
        led, new_keys = friction.merge_ledger(
            led,
            [{**e, "screenshot": "attempt-2/x.png"}],
            date="2026-09-13",
            run_url="https://r/1",
        )
        row = led["entries"][e["key"]]
        assert new_keys == []
        assert row["count"] == 1 and row["last_seen"] == "2026-09-12"
        assert row["screenshot"] == e["screenshot"]  # attempt 1's evidence stands
        # ... so the re-run has nothing "tonight" to file or comment on.
        assert friction.tonight(led, "2026-09-13") == []
        # A DIFFERENT run the next day is a recurrence as before.
        led, _ = friction.merge_ledger(led, [e], date="2026-09-13", run_url="https://r/2")
        assert led["entries"][e["key"]]["count"] == 2
