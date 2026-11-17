"""``kirocrew eval --baseline/--max-drop/--junit`` gates a run on a saved one.

The real ``_run_eval`` and ``EvalRunner`` run; only the model is replaced by a
scripted provider, as in ``test_cli_eval_user_scenarios``. Without the new flags
the command is exactly what it was, which the pin tests state in terms main
already satisfies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import cli_parser_admin
from kiro_crew.acp.types import AcpEvent as LLMEvent
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.eval import baseline
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.testing.links import make_dir_link

#: A reply that makes the scripted provider raise, so the turn fails with an error.
_BROKEN_TURN = "<raise>"


class _Scripted:
    """A provider that answers every turn with the next reply from *replies*."""

    cwd = ""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def set_workspace(self, path: Any) -> None:
        pass

    async def stream(self, message: str, **_kwargs: Any):
        reply = self._replies.pop(0) if self._replies else "nothing"
        if reply == _BROKEN_TURN:
            raise RuntimeError("the provider went away")
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=reply)
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id: Any) -> None:
        pass

    async def reject_tool(self, request_id: Any) -> None:
        pass

    def context_usage_pct(self) -> float:
        return 0.0


def _doc(name: str, expect: list[str]) -> dict[str, Any]:
    """A one-turn scenario that passes one assertion per word of *expect* the reply holds."""
    asserts = [{"type": "contains", "value": word} for word in expect]
    turns = [{"user": "say it", "assertions": asserts}]
    return {"name": name, "sessions": [{"name": "only", "turns": turns}]}


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
        "baseline": None,
        "max_drop": None,
        "junit": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def replies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run in ``tmp_path`` with a scripted provider; yield the shared reply script."""
    monkeypatch.chdir(tmp_path)
    script: list[str] = []

    def factory(session_key: str, **_kwargs: Any) -> _Scripted:
        return _Scripted(script if session_key.startswith("eval_only_") else [])

    with (
        patch.object(KiroCrewConfig, "load", return_value=KiroCrewConfig()),
        patch("kiro_crew.cli_commands.build_provider_factory", return_value=factory),
        patch("kiro_crew.agent_sdk.backends.ensure_kiro_cli_kas_verdict"),
    ):
        yield script


def _scenarios(tmp_path: Path, *docs: dict[str, Any]) -> Path:
    folder = tmp_path / "evals"
    folder.mkdir(exist_ok=True)
    for number, doc in enumerate(docs):
        # A file name cannot hold a control character on every platform; the name the
        # scenario reports can, because it is read from the file's text.
        stem = doc["name"] if doc["name"].isprintable() else f"scenario-{number}"
        (folder / f"{stem}.json").write_text(json.dumps(doc), encoding="utf-8")
    return folder


def _baseline(tmp_path: Path, **scores: str) -> Path:
    """A saved run in the shape the command writes: name -> "passed/total"."""
    rows = [
        {"name": n, "passed": a.split("/")[0] == a.split("/")[1], "assertions": a}
        for n, a in scores.items()
    ]
    path = tmp_path / "main.json"
    path.write_text(json.dumps({"scenarios": rows}), encoding="utf-8")
    return path


async def _run(**over: Any) -> None:
    await cc._run_eval(_args(**over))


class TestWithoutTheFlags:
    """What main already does, so the flags' absence is pinned to it."""

    @pytest.mark.asyncio
    async def test_a_plain_run_writes_its_two_files_and_prints_no_gate_text(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok")
        await _run(eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))))

        captured = capsys.readouterr()
        assert "Regression" not in captured.out and "Not compared" not in captured.out
        assert captured.err == ""
        assert sorted(p.suffix for p in (tmp_path / "eval_results").iterdir()) == [".json", ".md"]
        saved = json.loads(next((tmp_path / "eval_results").glob("*.json")).read_text("utf-8"))
        assert set(saved) == {
            "timestamp",
            "scenarios",
            "dimensions",
            "overall_passed",
            "overall_total",
        }

    @pytest.mark.asyncio
    async def test_a_failing_scenario_still_exits_1(
        self, tmp_path: Path, replies: list[str]
    ) -> None:
        replies.append("wrong")
        with pytest.raises(SystemExit) as exc:
            await _run(eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))))
        assert exc.value.code == 1


class TestTheGate:
    @pytest.mark.asyncio
    async def test_a_drop_past_the_allowance_is_named_and_exits_1(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # One of two assertions holds, so alpha scores 0.50 against a saved 1.00.
        replies.append("ok")
        with pytest.raises(SystemExit) as exc:
            await _run(
                eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok", "yes"]))),
                baseline=str(_baseline(tmp_path, alpha="2/2")),
                max_drop="0.05",
            )

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Regression: alpha: 1.00 -> 0.50 (allowed drop 0.05)" in out
        assert list((tmp_path / "eval_results").glob("*.json"))

    @pytest.mark.asyncio
    async def test_a_known_failure_still_exits_1_and_is_not_named_a_regression(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The gate only adds: a scenario that failed before and fails now still fails the run.
        replies.append("no")
        with pytest.raises(SystemExit) as exc:
            await _run(
                eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok", "yes"]))),
                baseline=str(_baseline(tmp_path, alpha="0/2")),
            )
        assert exc.value.code == 1
        assert "Regression" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_a_scenario_that_passed_before_and_fails_at_the_same_score_exits_1(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Turn one holds its assertion and turn two errors, which records no assertion
        # result, so the score stays 1/1 = 1.00 while the scenario fails.
        doc = {
            "name": "alpha",
            "sessions": [
                {
                    "name": "only",
                    "turns": [
                        {"user": "one", "assertions": [{"type": "contains", "value": "ok"}]},
                        {"user": "two", "assertions": [{"type": "contains", "value": "ok"}]},
                    ],
                }
            ],
        }
        replies.extend(["ok", _BROKEN_TURN])
        with pytest.raises(SystemExit) as exc:
            await _run(
                eval_dir=str(_scenarios(tmp_path, doc)),
                baseline=str(_baseline(tmp_path, alpha="2/2")),
            )
        assert exc.value.code == 1
        assert "no worse than the baseline" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_a_failure_the_baseline_has_no_score_for_still_exits_1(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("no")
        with pytest.raises(SystemExit) as exc:
            await _run(
                eval_dir=str(_scenarios(tmp_path, _doc("fresh", ["ok"]))),
                baseline=str(_baseline(tmp_path, other="1/1")),
            )
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Not compared: fresh (not in the baseline)" in out and "Regression" not in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "allowed", [pytest.param("0.5", id="exactly-the-drop"), pytest.param("1", id="all-of-it")]
    )
    async def test_a_drop_within_the_allowance_is_not_named_but_the_failure_still_exits_1(
        self,
        allowed: str,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        replies.append("ok")
        with pytest.raises(SystemExit) as exc:
            await _run(
                eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok", "yes"]))),
                baseline=str(_baseline(tmp_path, alpha="2/2")),
                max_drop=allowed,
            )
        assert exc.value.code == 1
        assert "Regression" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_an_equal_or_better_run_passes(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok yes")
        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok", "yes"]))),
            baseline=str(_baseline(tmp_path, alpha="1/2")),
        )
        assert "Regression" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_a_scenario_missing_from_either_run_is_named_not_failed(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok")
        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("fresh", ["ok"]))),
            baseline=str(_baseline(tmp_path, gone="1/1")),
        )
        out = capsys.readouterr().out
        assert "Not compared: fresh (not in the baseline)" in out
        assert "Not compared: gone (not in this run)" in out
        assert "Regression" not in out

    @pytest.mark.asyncio
    async def test_a_run_can_be_gated_on_the_file_the_command_wrote(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        folder = _scenarios(tmp_path, _doc("alpha", ["ok"]))
        replies.append("ok")
        await _run(eval_dir=str(folder))
        saved = next((tmp_path / "eval_results").glob("*.json"))
        replies.append("ok")
        await _run(eval_dir=str(folder), baseline=str(saved), max_drop="0")
        assert "Regression" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_a_name_from_a_file_cannot_drive_the_terminal(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("no")
        with pytest.raises(SystemExit):
            await _run(
                eval_dir=str(_scenarios(tmp_path, _doc("a\x1b[2Jb", ["ok"]))),
                baseline=str(_baseline(tmp_path, **{"a\x1b[2Jb": "1/1"})),
            )
        assert "\x1b" not in capsys.readouterr().out


class TestAnUnusableBaselineKeepsMainsOutcome:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            pytest.param(None, "the file was not found", id="missing"),
            pytest.param("{not json", "it is not an eval result file", id="malformed"),
            pytest.param('["a"]', "it is not an eval result file", id="wrong-shape"),
            pytest.param('{"scenarios": []}', "it is not an eval result file", id="no-scores"),
            pytest.param('{"scenarios": [{"name": "alpha"}]}', "it is not an eval", id="no-score"),
            pytest.param("[" * 5000, "it is not an eval result file", id="deeply-nested"),
        ],
    )
    async def test_one_warning_names_the_file_and_the_reason_class(
        self,
        content: str | None,
        reason: str,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        replies.append("ok")
        target = tmp_path / "main.json"
        if content is not None:
            target.write_text(content, encoding="utf-8")

        await _run(eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))), baseline=str(target))

        err = capsys.readouterr().err
        assert err.count("Warning:") == 1
        assert "--baseline main.json ignored: " in err and reason[:20] in err
        assert str(tmp_path) not in err

    @pytest.mark.asyncio
    async def test_a_failing_scenario_still_exits_1_under_a_bad_baseline(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("wrong")
        with pytest.raises(SystemExit) as exc:
            await _run(
                eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))),
                baseline=str(tmp_path / "absent.json"),
            )
        assert exc.value.code == 1
        assert "--baseline absent.json ignored" in capsys.readouterr().err

    @pytest.mark.asyncio
    async def test_a_refusal_from_the_read_gate_names_a_class_not_the_target(
        self,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        replies.append("ok")
        refusal = PermissionError("Blocked: access to sensitive path: '/elsewhere/secret'")

        from kiro_crew.hooks import safe_read_file as real_read

        def refuse(path: str, **kwargs: Any) -> str:
            if path.endswith("main.json"):
                raise refusal
            return real_read(path, **kwargs)

        monkeypatch.setattr("kiro_crew.hooks.safe_read_file", refuse)
        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))),
            baseline=str(tmp_path / "main.json"),
        )
        err = capsys.readouterr().err
        assert "the sensitive-path check refused it" in err and "/elsewhere" not in err

    @pytest.mark.asyncio
    async def test_a_folder_given_as_the_baseline_is_not_blamed_on_the_sensitive_path_check(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok")
        folder = tmp_path / "eval_results"
        folder.mkdir()

        await _run(eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))), baseline=str(folder))

        err = capsys.readouterr().err
        assert err.count("Warning:") == 1
        assert "--baseline eval_results ignored: " in err
        assert "sensitive-path" not in err

    @pytest.mark.parametrize(
        ("refusal", "reason"),
        [
            ("Blocked: access to sensitive path: '/x'", "the sensitive-path check refused it"),
            ("Blocked: opened sensitive path: '/x'", "the sensitive-path check refused it"),
            (
                "Blocked: opened file no longer matches safe path: '/x'",
                "the file could not be read",
            ),
            ("[Errno 13] Permission denied: '/x'", "the file could not be read"),
        ],
    )
    def test_only_a_sensitive_path_match_is_named_as_one(self, refusal: str, reason: str) -> None:
        assert baseline._read_failure_class(PermissionError(refusal)) == reason

    @pytest.mark.asyncio
    async def test_a_baseline_linked_to_a_credential_store_is_never_read(
        self,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        replies.append("ok")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        aws = tmp_path / ".aws"
        aws.mkdir()
        (aws / "credentials").write_text("[default]\nkey = NEVER-PRINTED\n", encoding="utf-8")
        folder = tmp_path / "saved"
        folder.mkdir()
        make_dir_link(folder / "shared", aws)

        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))),
            baseline=str(folder / "shared" / "credentials"),
        )
        captured = capsys.readouterr()
        assert "--baseline credentials ignored" in captured.err
        assert "NEVER-PRINTED" not in captured.out + captured.err
        assert str(aws) not in captured.err


class TestMaxDrop:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["abc", "2", "-0.1", "nan", "inf", ""])
    async def test_a_bad_value_drops_the_gate_with_one_warning(
        self,
        bad: str,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # alpha holds one of two assertions: main exits 1, and the gate would name a regression.
        replies.append("ok")
        with pytest.raises(SystemExit) as exc:
            await _run(
                eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok", "yes"]))),
                baseline=str(_baseline(tmp_path, alpha="2/2")),
                max_drop=bad,
            )
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.err.count("Warning:") == 1
        assert (
            "--baseline main.json ignored: --max-drop must be a number from 0 to 1" in captured.err
        )
        assert "Regression" not in captured.out

    @pytest.mark.asyncio
    async def test_a_max_drop_alone_is_ignored_with_one_warning(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok")
        await _run(eval_dir=str(_scenarios(tmp_path, _doc("alpha", ["ok"]))), max_drop="0.1")
        assert capsys.readouterr().err.count("--max-drop ignored: it needs --baseline") == 1


class TestJunit:
    @pytest.mark.asyncio
    async def test_a_passing_run_writes_one_case_per_scenario(
        self, tmp_path: Path, replies: list[str]
    ) -> None:
        replies.extend(["ok", "ok"])
        target = tmp_path / "reports" / "eval.xml"
        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]), _doc("b", ["ok"]))),
            junit=str(target),
        )
        text = target.read_text(encoding="utf-8")
        assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<testsuite ')
        assert 'tests="2" failures="0"' in text and text.count("<testcase ") == 2
        assert "<failure" not in text

    @pytest.mark.asyncio
    async def test_every_failed_scenario_is_a_failure_and_a_regression_says_so(
        self, tmp_path: Path, replies: list[str]
    ) -> None:
        replies.extend(["ok", "no", "no"])
        target = tmp_path / "eval.xml"
        docs = (_doc("a", ["ok", "yes"]), _doc("b", ["ok"]), _doc("c", ["ok"]))
        with pytest.raises(SystemExit):
            await _run(
                eval_dir=str(_scenarios(tmp_path, *docs)),
                baseline=str(_baseline(tmp_path, a="2/2", c="0/1")),
                junit=str(target),
            )
        text = target.read_text(encoding="utf-8")
        assert 'tests="3" failures="3"' in text
        assert 'message="score fell from 1.00 to 0.50"' in text
        assert text.count('<failure message="assertions 0/1"/>') == 2
        assert "<skipped" not in text

    @pytest.mark.asyncio
    async def test_without_a_baseline_a_failed_scenario_is_a_failure(
        self, tmp_path: Path, replies: list[str]
    ) -> None:
        replies.append("no")
        target = tmp_path / "eval.xml"
        with pytest.raises(SystemExit):
            await _run(eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]))), junit=str(target))
        assert '<failure message="assertions 0/1"/>' in target.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_file_that_cannot_be_written_warns_and_keeps_mains_outcome(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok")
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a folder", encoding="utf-8")
        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]))), junit=str(blocker / "x.xml")
        )
        err = capsys.readouterr().err
        assert "--junit x.xml was not written" in err and str(tmp_path) not in err

    @pytest.mark.asyncio
    async def test_a_home_the_host_cannot_find_warns_and_keeps_mains_outcome(
        self,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A quoted ``~user`` whose account does not exist: pathlib raises RuntimeError.
        class NoHome(type(Path())):  # type: ignore[misc]
            def expanduser(self) -> Path:
                raise RuntimeError("Could not determine home directory.")

        replies.append("ok")
        monkeypatch.setattr(baseline, "Path", NoHome)
        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]))),
            junit="~nobody-here/reports/eval.xml",
        )
        err = capsys.readouterr().err
        assert "--junit eval.xml was not written: the file could not be written." in err

    @pytest.mark.asyncio
    async def test_a_protected_destination_is_never_written(
        self,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # An agent can run any CLI command, so --junit must not overwrite a file the
        # sensitive-path check protects (a credential store, the governance trust root).
        import kiro_crew.security as security

        replies.append("ok")
        target = tmp_path / "policy" / "eval.xml"
        real = security.is_sensitive_path

        def flags_target(path: str, *a: Any, **kw: Any) -> bool:
            return Path(path).name == "eval.xml" or real(path, *a, **kw)

        monkeypatch.setattr(security, "is_sensitive_path", flags_target)
        await _run(eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]))), junit=str(target))

        assert not target.exists()
        err = capsys.readouterr().err
        assert err.count("Warning:") == 1
        assert "--junit eval.xml was not written: the sensitive-path check refused it." in err
        assert str(tmp_path) not in err

    @pytest.mark.asyncio
    @pytest.mark.skipif(sys.platform == "win32", reason="a link at a file name needs O_NOFOLLOW")
    async def test_a_link_at_the_name_is_named_a_link(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok")
        (tmp_path / "elsewhere").mkdir()
        target = tmp_path / "eval.xml"
        make_dir_link(target, tmp_path / "elsewhere")
        await _run(eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]))), junit=str(target))

        err = capsys.readouterr().err
        assert err.count("Warning:") == 1
        assert "--junit eval.xml was not written: the name is a link." in err
        assert "sensitive" not in err and str(tmp_path) not in err

    @pytest.mark.asyncio
    async def test_a_second_name_for_a_file_is_named_a_hard_link(
        self, tmp_path: Path, replies: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        replies.append("ok")
        (tmp_path / "other.txt").write_text("keep", encoding="utf-8")
        target = tmp_path / "eval.xml"
        os.link(tmp_path / "other.txt", target)
        await _run(eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]))), junit=str(target))

        err = capsys.readouterr().err
        assert "--junit eval.xml was not written: the name is a hard link." in err
        assert "sensitive" not in err and str(tmp_path) not in err
        assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "keep"

    @pytest.mark.skipif(sys.platform == "win32", reason="a named pipe needs os.mkfifo")
    def test_a_pipe_at_the_name_is_not_written_and_does_not_hang(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Opening a pipe nobody reads blocks for ever, so the write runs on a thread the
        # test gives ten seconds; a regression fails the test instead of hanging the suite.
        import threading

        target = tmp_path / "eval.xml"
        os.mkfifo(target)
        worker = threading.Thread(
            target=baseline.write_junit,
            args=(str(target), [{"name": "a", "passed": True}], None),
            daemon=True,
        )
        worker.start()
        worker.join(10)
        stuck = worker.is_alive()
        if stuck:
            # Let a writer blocked in open() through so it can end.
            fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(10)
        assert not stuck, "write_junit blocked on a pipe at the destination"
        err = capsys.readouterr().err
        assert err.count("Warning:") == 1
        assert "--junit eval.xml was not written: the name is not a regular file." in err
        assert str(tmp_path) not in err

    def test_a_folder_at_the_name_is_not_a_regular_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "eval.xml"
        target.mkdir()
        baseline.write_junit(str(target), [{"name": "a", "passed": True}], None)

        err = capsys.readouterr().err
        assert err.count("Warning:") == 1
        assert "--junit eval.xml was not written: the name is not a regular file." in err
        assert list(target.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_link_on_the_way_is_named_a_link_on_the_way(
        self,
        tmp_path: Path,
        replies: list[str],
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import kiro_crew.eval.bench.safepath as safepath

        # Where a directory cannot be pinned by descriptor, a link in the chain is refused.
        monkeypatch.setattr(safepath, "_supports_pinned_walk", lambda: False)
        replies.append("ok")
        (tmp_path / "real").mkdir()
        make_dir_link(tmp_path / "via", tmp_path / "real")
        await _run(
            eval_dir=str(_scenarios(tmp_path, _doc("a", ["ok"]))),
            junit=str(tmp_path / "via" / "eval.xml"),
        )

        err = capsys.readouterr().err
        assert "--junit eval.xml was not written: a folder on the way to it is a link." in err
        assert "sensitive" not in err and str(tmp_path) not in err

    @pytest.mark.parametrize(
        ("text", "named"),
        [
            ("refusing to write the JUnit report: 'a' is a symbolic link. x", "the name is a link"),
            (
                "refusing to use the JUnit report: 'a' has 2 hard links, so",
                "the name is a hard link",
            ),
            (
                "refusing to write the JUnit report to 'a': the destination resolves",
                "the sensitive-path check refused it",
            ),
            ("refusing to write the JUnit report: something new", "the write guard refused it"),
            (
                "refusing to write the JUnit report: 'is a symbolic link' has 2 hard links",
                "the name is a hard link",
            ),
            (
                "refusing to write the JUnit report to 'it\\'s a link': the destination resolves",
                "the sensitive-path check refused it",
            ),
        ],
    )
    def test_a_refusal_is_named_by_its_words_not_by_the_name_it_quotes(
        self, text: str, named: str
    ) -> None:
        assert baseline._write_refusal_class(text) == named

    def test_a_name_outside_the_basic_plane_is_kept(self) -> None:
        rows = [{"name": "ship \U0001f680", "passed": True, "elapsed_secs": 0}]
        assert 'name="ship \U0001f680"' in baseline.junit_xml(rows, {})

    def test_markup_and_control_characters_in_a_name_cannot_break_the_file(self) -> None:
        rows = [{"name": 'a<b>&"c\x00\x1bd\n', "passed": True, "elapsed_secs": 1.5}]
        text = baseline.junit_xml(rows, {})
        assert 'name="a&lt;b&gt;&amp;&quot;cd&#10;"' in text
        assert "\x00" not in text and "\x1b" not in text


class TestScores:
    @pytest.mark.parametrize(
        ("entry", "score"),
        [
            ({"passed": False, "assertions": "1/4"}, 0.25),
            ({"passed": True, "assertions": "0/0"}, 1.0),
            ({"passed": False, "assertions": "0/0"}, 0.0),
            ({"passed": True}, 1.0),
            ({"passed": True, "assertions": "9/4"}, 1.0),
            ({"passed": False, "assertions": "x/y"}, 0.0),
            ({"passed": False, "assertions": "1" * 40 + "/3"}, 0.0),
            ({"assertions": "1/2"}, 0.5),
            ({"name": "n"}, None),
            ("text", None),
        ],
    )
    def test_a_scenario_scores_its_share_of_passed_assertions(
        self, entry: Any, score: float | None
    ) -> None:
        assert baseline.scenario_score(entry) == score


class TestParser:
    @staticmethod
    def _parse(argv: list[str]) -> argparse.Namespace:
        root = argparse.ArgumentParser(prog="kirocrew")
        sub = root.add_subparsers(dest="command")
        cli_parser_admin.register_eval_parser(sub, argparse.ArgumentParser(add_help=False))
        return root.parse_args(["eval", *argv])

    def test_the_flags_default_off(self) -> None:
        args = self._parse([])
        assert (args.baseline, args.max_drop, args.junit) == (None, None, None)

    def test_a_bad_max_drop_is_not_refused_by_the_parser(self) -> None:
        args = self._parse(["--baseline", "b.json", "--max-drop", "lots", "--junit", "o.xml"])
        assert (args.baseline, args.max_drop, args.junit) == ("b.json", "lots", "o.xml")


def test_the_report_has_no_stdlib_xml_import() -> None:
    source = Path(baseline.__file__).read_text(encoding="utf-8")
    assert re.search(r"^\s*(from|import) xml", source, re.MULTILINE) is None
