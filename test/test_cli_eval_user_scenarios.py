"""``kirocrew eval --dir/--file/--agent/--model/--repeat`` runs a project's own scenarios.

The real ``_run_eval`` and the real ``EvalRunner`` run here. Only the model is
replaced: ``build_provider_factory`` returns a factory that records the keyword
arguments each session was built with and hands back a scripted provider, so no
backend starts. Results land in the chdir'd ``tmp_path``.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import cli_parser_admin
from kiro_crew.acp.types import AcpEvent as LLMEvent
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.eval import runner as eval_runner
from kiro_crew.eval.runner import ScenarioResult, SessionResult, TurnResult
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_crew.security.paths import UNVERIFIABLE_PATH_PREFIX
from kiro_crew.testing.links import make_dir_link


class _Scripted:
    """A provider that answers every turn with the next reply from *replies*."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies

    cwd = ""

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def set_workspace(self, path: Any) -> None:
        pass

    async def stream(self, message: str, **_kwargs: Any):
        text = self._replies.pop(0) if self._replies else "no more replies"
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id: Any) -> None:
        pass

    async def reject_tool(self, request_id: Any) -> None:
        pass

    def context_usage_pct(self) -> float:
        return 0.0


class _Asking(_Scripted):
    """Asks permission for one tool call, then answers like ``_Scripted``."""

    async def stream(self, message: str, **kwargs: Any):
        yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="write_file", request_id="r1")
        async for event in super().stream(message, **kwargs):
            yield event


def _scenario_doc(name: str, expect: str = "ok") -> dict[str, Any]:
    return {
        "name": name,
        "dimensions": ["mine"],
        "sessions": [
            {
                "name": "only",
                "turns": [
                    {"user": "say it", "assertions": [{"type": "contains", "value": expect}]}
                ],
            }
        ],
    }


def _args(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "scenarios": [],
        "all_scenarios": False,
        "judge": False,
        "eval_dir": None,
        "eval_file": None,
        "agent": None,
        "model": None,
        "repeat": 1,
    }
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def factory_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run in ``tmp_path`` with a recording factory; yield ``(calls, replies)``.

    ``replies`` is the shared script every provider reads from, in call order.
    """

    monkeypatch.chdir(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []
    replies: list[str] = []

    def factory(session_key: str, **kwargs: Any) -> _Scripted:
        calls.append((session_key, kwargs))
        # Only a scenario session reads the script; memory consolidation gets filler.
        return _Scripted(replies if session_key.startswith("eval_only_") else [])

    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.build_provider_factory", return_value=factory),
        patch("kiro_crew.agent_sdk.backends.ensure_kiro_cli_kas_verdict"),
    ):
        yield calls, replies


def _write(folder: Path, filename: str, doc: dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _saved_json(tmp_path: Path) -> dict[str, Any]:
    (path,) = (tmp_path / "eval_results").glob("*.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _scenario_calls(calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The factory calls that built a scenario session (not consolidation ones)."""

    return [kw for key, kw in calls if key.startswith("eval_only_")]


class TestParser:
    @staticmethod
    def _parse(argv: list[str]) -> argparse.Namespace:
        root = argparse.ArgumentParser(prog="kirocrew")
        sub = root.add_subparsers(dest="command")
        cli_parser_admin.register_eval_parser(sub, argparse.ArgumentParser(add_help=False))
        return root.parse_args(["eval", *argv])

    def test_new_flags_default_off(self) -> None:
        args = self._parse([])
        assert (args.eval_dir, args.eval_file, args.agent, args.model, args.repeat) == (
            None,
            None,
            None,
            None,
            1,
        )

    def test_new_flags_parse(self) -> None:
        args = self._parse(
            ["--dir", "evals", "--agent", "reviewer", "--model", "m-1", "--repeat", "3"]
        )
        assert (args.eval_dir, args.agent, args.model, args.repeat) == (
            "evals",
            "reviewer",
            "m-1",
            3,
        )

    @pytest.mark.parametrize(
        ("bad", "reason"),
        [
            ("0", "must be 1 or more"),
            ("-2", "must be 1 or more"),
            ("two", "must be a whole number"),
        ],
    )
    def test_repeat_refuses_less_than_one(
        self, bad: str, reason: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            self._parse(["--repeat", bad])
        assert exc.value.code == 2
        assert reason in capsys.readouterr().err


class TestUserScenarios:
    @pytest.mark.asyncio
    async def test_dir_runs_every_scenario_file_in_it(self, tmp_path: Path, factory_calls) -> None:
        calls, replies = factory_calls
        replies.extend(["ok", "ok"])
        folder = tmp_path / "evals"
        _write(folder, "a.json", _scenario_doc("alpha"))
        _write(folder, "b.json", _scenario_doc("beta"))
        (folder / "notes.txt").write_text("not a scenario", encoding="utf-8")

        await cc._run_eval(_args(eval_dir=str(folder)))

        data = _saved_json(tmp_path)
        assert [s["name"] for s in data["scenarios"]] == ["alpha", "beta"]
        assert data["overall_passed"] == 2 and data["overall_total"] == 2

    @pytest.mark.asyncio
    async def test_file_runs_only_that_file(self, tmp_path: Path, factory_calls) -> None:
        calls, replies = factory_calls
        replies.append("ok")
        _write(tmp_path / "evals", "other.json", _scenario_doc("other"))
        mine = _write(tmp_path / "evals", "mine.json", _scenario_doc("mine"))

        await cc._run_eval(_args(eval_file=str(mine)))

        assert [s["name"] for s in _saved_json(tmp_path)["scenarios"]] == ["mine"]

    @pytest.mark.asyncio
    async def test_a_failing_scenario_exits_1_after_saving(
        self, tmp_path: Path, factory_calls
    ) -> None:
        calls, replies = factory_calls
        replies.append("wrong answer")
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine", expect="ok"))

        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(eval_file=str(mine)))

        assert exc.value.code == 1
        assert _saved_json(tmp_path)["overall_passed"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("opts", "needle"),
        [
            pytest.param({"eval_dir": "missing"}, "no scenario files", id="missing-dir"),
            pytest.param({"eval_file": "missing.json"}, "not found", id="missing-file"),
            pytest.param({"eval_file": "x.json", "eval_dir": "y"}, "not both", id="dir-and-file"),
            pytest.param(
                {"eval_file": "x.json", "scenarios": ["smoke_test"]},
                "cannot be combined",
                id="with-names",
            ),
            pytest.param(
                {"eval_dir": "y", "all_scenarios": True}, "cannot be combined", id="with-all"
            ),
        ],
    )
    async def test_a_bad_source_runs_nothing_and_exits_1(
        self,
        opts: dict[str, Any],
        needle: str,
        factory_calls,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        calls, _ = factory_calls

        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(**opts))

        assert exc.value.code == 1
        assert needle in capsys.readouterr().err
        assert calls == []
        assert not (tmp_path / "eval_results").exists()

    @pytest.mark.asyncio
    async def test_an_unreadable_file_is_named_and_stops_the_run(
        self, tmp_path: Path, factory_calls, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls, _ = factory_calls
        folder = tmp_path / "evals"
        _write(folder, "a.json", _scenario_doc("alpha"))
        (folder / "broken.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(eval_dir=str(folder)))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "cannot load scenario broken.json" in err
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_scenario_linked_to_a_credential_store_is_refused_unread(
        self,
        tmp_path: Path,
        factory_calls,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The read gate resolves the link first, so a link cannot launder the target.

        The link is a directory link (a junction on Windows), which needs no privilege,
        so this runs on every platform.
        """
        calls, _ = factory_calls
        # The gate anchors on the home directory; point it here so the real gate runs.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        aws = tmp_path / ".aws"
        aws.mkdir()
        (aws / "credentials").write_text("[default]\nkey = NEVER-PRINTED\n", encoding="utf-8")
        folder = tmp_path / "evals"
        folder.mkdir()
        make_dir_link(folder / "shared", aws)

        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(eval_file=str(folder / "shared" / "credentials")))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "refusing to read scenario credentials" in err and "sensitive location" in err
        assert "NEVER-PRINTED" not in err and str(aws) not in err
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_dangling_link_in_the_folder_stops_the_run(
        self, tmp_path: Path, factory_calls, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A scenario entry that cannot be read is named, never silently left out."""
        calls, _ = factory_calls
        folder = tmp_path / "evals"
        _write(folder, "a.json", _scenario_doc("alpha"))
        gone = tmp_path / "moved-away"
        gone.mkdir()
        make_dir_link(folder / "gone.json", gone)
        gone.rmdir()

        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(eval_dir=str(folder)))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "scenario gone.json" in err and str(gone) not in err
        assert calls == []

    @pytest.mark.asyncio
    async def test_terminal_controls_in_scenario_text_are_not_printed(
        self,
        tmp_path: Path,
        factory_calls,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A scenario file someone else wrote cannot drive the terminal (OSC 52 here).

        The runner's log lines count too: with ``-v`` they reach the console.
        """
        calls, replies = factory_calls
        replies.append("ok")
        doc = _scenario_doc("clip\x1b]52;c;aGk=\x07board")
        doc["dimensions"] = ["dim\x1b[2J"]
        mine = _write(tmp_path, "mine.json", doc)

        with caplog.at_level(logging.INFO, logger="kiro_crew.eval.runner"):
            await cc._run_eval(_args(eval_file=str(mine)))

        out = capsys.readouterr().out
        assert "\x1b" not in out and "\x07" not in out
        assert "Running: clipboard (1 turns)" in out
        logged = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.eval.runner"]
        assert any("clipboard" in line for line in logged)
        assert not any("\x1b" in line or "\x07" in line for line in logged)
        # The run itself saw the name as written.
        assert _saved_json(tmp_path)["scenarios"][0]["name"] == "clip\x1b]52;c;aGk=\x07board"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            pytest.param(
                PermissionError(13, "Permission denied"),
                "cannot load scenario case.json: PermissionError: Permission denied",
                id="no-read-permission",
            ),
            pytest.param(
                PermissionError(f"{UNVERIFIABLE_PATH_PREFIX} (late) '/elsewhere/x'"),
                "cannot load scenario case.json: the sensitive-path check did not finish",
                id="check-did-not-finish",
            ),
            pytest.param(
                PermissionError("Blocked: opened file no longer matches safe path: '/elsewhere/x'"),
                "refusing to read scenario case.json: the file changed while it was being opened",
                id="swapped-while-opening",
            ),
        ],
    )
    async def test_a_read_failure_is_reported_as_its_own_cause(
        self,
        tmp_path: Path,
        factory_calls,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        raised: PermissionError,
        expected: str,
    ) -> None:
        """Only a sensitive-path match is reported as one; a gate refusal's target is never shown."""
        calls, _ = factory_calls
        mine = _write(tmp_path, "case.json", _scenario_doc("mine"))

        def refuse(path: str, **_kwargs: Any) -> str:
            raise raised

        monkeypatch.setattr("kiro_crew.hooks.safe_read_file", refuse)
        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(eval_file=str(mine)))

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert expected in err
        assert "sensitive location" not in err and "/elsewhere" not in err
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_name_used_twice_stops_the_run(
        self, tmp_path: Path, factory_calls, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls, _ = factory_calls
        folder = tmp_path / "evals"
        _write(folder, "a.json", _scenario_doc("same"))
        _write(folder, "b.json", _scenario_doc("same"))

        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(eval_dir=str(folder)))

        assert exc.value.code == 1
        assert "'same' is used by more than one file" in capsys.readouterr().err
        assert calls == []


class TestAgentAndModel:
    @pytest.mark.asyncio
    async def test_without_the_flags_the_factory_gets_only_a_session_key(
        self, tmp_path: Path, factory_calls
    ) -> None:
        calls, replies = factory_calls
        replies.append("ok")
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine"))

        await cc._run_eval(_args(eval_file=str(mine)))

        assert _scenario_calls(calls) == [{}]
        assert set(_saved_json(tmp_path)) == {
            "timestamp",
            "scenarios",
            "dimensions",
            "overall_passed",
            "overall_total",
        }

    @pytest.mark.asyncio
    async def test_agent_and_model_reach_every_scenario_session(
        self, tmp_path: Path, factory_calls
    ) -> None:
        calls, replies = factory_calls
        replies.append("ok")
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine"))

        await cc._run_eval(_args(eval_file=str(mine), agent="reviewer", model="model-x"))

        assert _scenario_calls(calls) == [{"agent": "reviewer", "model_override": "model-x"}]
        data = _saved_json(tmp_path)
        assert (data["agent"], data["model"]) == ("reviewer", "model-x")
        # Memory consolidation is not the agent under test: it keeps the default.
        others = [kw for key, kw in calls if not key.startswith("eval_only_")]
        assert others and all(kw.get("agent") != "reviewer" for kw in others)
        assert all(kw.get("model_override") != "model-x" for kw in others)

    @pytest.mark.asyncio
    async def test_a_crew_member_runs_under_its_own_identity_and_pin(
        self, tmp_path: Path, factory_calls
    ) -> None:
        calls, replies = factory_calls
        replies.extend(["ok", "ok"])
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine"))
        resolved = ("kiro-agent", "reviewer", "pinned-model", "default")

        with patch("kiro_crew.cli_chat._resolve_cli_agent", return_value=resolved):
            await cc._run_eval(_args(eval_file=str(mine), agent="reviewer"))
            await cc._run_eval(_args(eval_file=str(mine), agent="reviewer", model="model-x"))

        assert _scenario_calls(calls) == [
            {"agent": "kiro-agent", "crew_agent": "reviewer", "model_override": "pinned-model"},
            {"agent": "kiro-agent", "crew_agent": "reviewer", "model_override": "model-x"},
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("flag", "resolved", "judged_as"),
        [
            pytest.param(None, None, "", id="no-agent"),
            pytest.param("helper", ("helper", None, "", "default"), "helper", id="kiro-agent"),
            pytest.param(
                "reviewer", ("kiro-agent", "reviewer", "", "default"), "reviewer", id="member"
            ),
        ],
    )
    async def test_the_gate_judges_each_call_as_the_agent_the_session_runs_as(
        self,
        tmp_path: Path,
        factory_calls,
        monkeypatch: pytest.MonkeyPatch,
        flag: str | None,
        resolved: tuple | None,
        judged_as: str,
    ) -> None:
        """The agent's own governance profile applies, as it does in ``kirocrew chat``."""
        calls, replies = factory_calls
        replies.append("ok")
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine"))
        judged: list[str] = []

        def gate(event: Any, **kwargs: Any) -> str:
            judged.append(kwargs["agent"])
            return "Blocked by a test rule"

        def factory(session_key: str, **kwargs: Any) -> _Scripted:
            calls.append((session_key, kwargs))
            return _Asking(replies) if session_key.startswith("eval_only_") else _Scripted([])

        monkeypatch.setattr(eval_runner, "refusal_for", gate)
        with (
            patch("kiro_crew.cli_commands.build_provider_factory", return_value=factory),
            patch("kiro_crew.cli_chat._resolve_cli_agent", return_value=resolved),
        ):
            await cc._run_eval(_args(eval_file=str(mine), agent=flag))

        assert judged == [judged_as]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("flag", [None, "helper"])
    async def test_a_named_agent_warms_the_spec_snapshot_before_any_session(
        self, tmp_path: Path, factory_calls, monkeypatch: pytest.MonkeyPatch, flag: str | None
    ) -> None:
        """The factory reads the agent's model pin from the snapshot; a cold one answers none."""
        calls, replies = factory_calls
        replies.append("ok")
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine"))
        events: list[object] = []

        async def warm(**kwargs: Any) -> None:
            events.append(("warm", kwargs))

        def factory(session_key: str, **kwargs: Any) -> _Scripted:
            events.append(session_key)
            return _Scripted(replies if session_key.startswith("eval_only_") else [])

        monkeypatch.setattr("kiro_crew.agent_discovery.warm_agent_specs", warm)
        with (
            patch("kiro_crew.cli_commands.build_provider_factory", return_value=factory),
            patch("kiro_crew.cli_chat._resolve_cli_agent", return_value=("helper", None, "", "d")),
        ):
            await cc._run_eval(_args(eval_file=str(mine), agent=flag))

        if flag is None:
            assert not any(isinstance(e, tuple) for e in events)
        else:
            assert events[0] == ("warm", {"operation": "eval_agent", "source": "cli"})
            assert any(isinstance(e, str) and e.startswith("eval_only_") for e in events[1:])


class TestRepeat:
    @pytest.mark.asyncio
    async def test_a_scenario_passes_only_when_every_run_passes(
        self, tmp_path: Path, factory_calls, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls, replies = factory_calls
        replies.extend(["ok", "nope", "ok"])
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine"))

        with pytest.raises(SystemExit) as exc:
            await cc._run_eval(_args(eval_file=str(mine), repeat=3))

        assert exc.value.code == 1
        assert len(_scenario_calls(calls)) == 3
        (entry,) = _saved_json(tmp_path)["scenarios"]
        assert (entry["passed"], entry["runs"], entry["runs_passed"]) == (False, 3, 2)
        assert entry["sessions"] == 1
        out = capsys.readouterr().out
        assert "Run 2/3" in out and "Runs passed: 2/3" in out
        assert "Overall: 0/1 scenarios passed" in out

    @pytest.mark.asyncio
    async def test_all_runs_passing_exits_0(self, tmp_path: Path, factory_calls) -> None:
        calls, replies = factory_calls
        replies.extend(["ok", "ok"])
        mine = _write(tmp_path, "mine.json", _scenario_doc("mine"))

        await cc._run_eval(_args(eval_file=str(mine), repeat=2))

        data = _saved_json(tmp_path)
        assert data["repeat"] == 2 and data["overall_passed"] == 1


class TestMergeRepeats:
    @staticmethod
    def _result(ok: bool) -> ScenarioResult:
        from kiro_crew.eval.scenario import Assertion, AssertionType

        turn = TurnResult(
            user_message="q",
            agent_response="a",
            assertion_results=[(Assertion(AssertionType.CONTAINS, "a"), ok)],
        )
        return ScenarioResult(
            name="s", dimensions=["d"], sessions=[SessionResult(name="one", turns=[turn])]
        )

    def test_folds_runs_per_scenario_and_renames_sessions(self) -> None:
        from kiro_crew.eval.runner import merge_repeats

        (merged,) = merge_repeats([[self._result(True)], [self._result(False)]])
        assert merged.passed is False
        assert (merged.runs, merged.runs_passed) == (2, 1)
        assert [s.name for s in merged.sessions] == ["run 1: one", "run 2: one"]
        assert merged.summary()["sessions"] == 1

    def test_all_runs_passing_passes(self) -> None:
        from kiro_crew.eval.runner import merge_repeats

        (merged,) = merge_repeats([[self._result(True)], [self._result(True)]])
        assert merged.passed is True and merged.runs_passed == 2
