"""``kirocrew resume``, and the hint ``kirocrew restart`` prints instead of resuming.

The gateway is stood in for at ``cli_resume.GatewayClient``: the owner sign-in is
``cli_chat_headless``'s and has its own tests. What these pin is the route and body
each form sends, and that the last-stop record is only ever read.
"""

from __future__ import annotations

import argparse
import os
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import cli_resume
from kiro_crew.cli_chat_headless import NoGateway
from kiro_crew.dashboard import restart_resume


class _Gateway:
    """Records every press; answers each with *answer*, or raises *fails*.

    *ready* is what ``/api/ready`` answers, one status per probe; the last repeats.
    ``events`` is the order of probes, sign-ins and presses.
    """

    def __init__(self, answer=None, fails: Exception | None = None, ready=(200,), fails_for=None):
        self.answer = answer if answer is not None else {"ok": True, "total": 0, "run_id": ""}
        self.fails = fails
        self.fails_for: dict[str, Exception] = fails_for or {}
        self.ready = list(ready)
        self.pressed: list[tuple[str, str, dict]] = []
        self.events: list[str] = []

    def ready_status(self, port):
        self.events.append(f"ready:{self.ready[0]}")
        return self.ready.pop(0) if len(self.ready) > 1 else self.ready[0]

    def client(self, port):
        gateway = self

        class _Client:
            def sign_in(self):
                gateway.events.append("sign-in")
                if isinstance(gateway.fails, NoGateway):
                    raise gateway.fails

            def call(self, method, path, body=None, timeout=60):
                gateway.events.append("press")
                gateway.pressed.append((method, path, body))
                if gateway.fails is not None:
                    raise gateway.fails
                if path in gateway.fails_for:
                    raise gateway.fails_for[path]
                return gateway.answer

        return _Client()


class _Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def sleep(self, secs):
        self.sleeps += 1
        assert self.sleeps < 1000, "the wait never ended"
        self.now += secs


@pytest.fixture
def stop_record(tmp_path, monkeypatch):
    path = tmp_path / "resume-after-stop.json"
    monkeypatch.setattr(restart_resume, "last_stop_path", lambda: path)
    monkeypatch.setattr(cli_resume, "resolve_client_port", lambda port: 4321)
    clock = _Clock()
    monkeypatch.setattr(cli_resume, "_clock", lambda: clock.now)
    monkeypatch.setattr(cli_resume, "_sleep", clock.sleep)
    return path


def _gateway(monkeypatch, **kwargs) -> _Gateway:
    gateway = _Gateway(**kwargs)
    monkeypatch.setattr(cli_resume, "GatewayClient", gateway.client)
    monkeypatch.setattr(cli_resume, "_ready_status", gateway.ready_status)
    return gateway


def _args(*sessions: str, all: bool = False) -> argparse.Namespace:
    return argparse.Namespace(sessions=list(sessions), all=all, port=None)


@pytest.mark.parametrize("started", [2, 1, 0], ids=["all", "some", "none"])
def test_resume_sends_exactly_the_chats_the_last_stop_cut_and_leaves_the_record(
    started, stop_record, monkeypatch, capsys
):
    """The route skips a chat that is not interrupted, so a second run is safe; the next stop rewrites it."""
    restart_resume.write_last_stop(["cut-a", "cut-b"])
    gateway = _gateway(monkeypatch, answer={"ok": True, "total": started, "run_id": "r1"})

    cli_resume.resume_cmd(_args())

    assert gateway.pressed == [
        ("POST", cli_resume.RESUME_ALL_PATH, {"crew_mode": True, "slots": ["cut-a", "cut-b"]})
    ]
    assert restart_resume.read_last_stop() == ["cut-a", "cut-b"]
    out = capsys.readouterr().out
    if started:
        assert f"Resuming {started} chat(s)" in out
    if started < 2:
        assert f"{2 - started} of the 2 chat(s)" in out


def test_resume_waits_for_a_booting_gateway_to_report_ready_before_pressing(
    stop_record, monkeypatch
):
    restart_resume.write_last_stop(["cut-a"])
    gateway = _gateway(monkeypatch, ready=(503, 503, 200), answer={"ok": True, "total": 1})

    cli_resume.resume_cmd(_args())

    assert gateway.events == ["ready:503", "ready:503", "ready:200", "sign-in", "press"]


def test_resume_against_a_gateway_that_never_becomes_ready_exits_1_without_pressing(
    stop_record, monkeypatch, capsys
):
    restart_resume.write_last_stop(["cut-a"])
    gateway = _gateway(monkeypatch, ready=(503,))

    with pytest.raises(SystemExit) as exc:
        cli_resume.resume_cmd(_args())

    assert exc.value.code == 1
    assert gateway.pressed == []
    assert restart_resume.read_last_stop() == ["cut-a"]
    assert "not ready" in capsys.readouterr().err


def test_resume_with_no_gateway_listening_fails_at_once(stop_record, monkeypatch):
    restart_resume.write_last_stop(["cut-a"])
    gateway = _gateway(monkeypatch, ready=(0,))

    with pytest.raises(SystemExit) as exc:
        cli_resume.resume_cmd(_args())

    assert exc.value.code == 1
    assert gateway.events == ["ready:0"]


def test_resume_with_nothing_recorded_says_so_and_asks_no_gateway(stop_record, monkeypatch, capsys):
    gateway = _gateway(monkeypatch)

    cli_resume.resume_cmd(_args())

    assert gateway.pressed == []
    assert "nothing to resume" in capsys.readouterr().out


def test_all_presses_resume_all_with_crew_mode(stop_record, monkeypatch):
    restart_resume.write_last_stop(["cut-a"])
    gateway = _gateway(monkeypatch, answer={"ok": True, "total": 2, "run_id": "r"})

    cli_resume.resume_cmd(_args(all=True))

    assert gateway.pressed == [("POST", cli_resume.RESUME_ALL_PATH, {"crew_mode": True})]


def test_named_sessions_press_each_tabs_own_resume_and_never_resume_all(
    stop_record, monkeypatch, capsys
):
    """Resume-all leaves an old interruption to the tab's button, so a named chat uses its route."""
    restart_resume.write_last_stop(["cut-a"])
    gateway = _gateway(monkeypatch, answer={"ok": True, "slot": "x"})

    cli_resume.resume_cmd(_args("s1", "chat/2", "s1"))

    assert gateway.pressed == [
        ("POST", "/api/chat/slots/s1/continue", None),
        ("POST", "/api/chat/slots/chat%2F2/continue", None),
    ]
    assert gateway.events == ["ready:200", "sign-in", "press", "press"]
    out = capsys.readouterr().out
    assert "s1: resuming" in out and "chat/2: resuming" in out


def test_a_named_session_the_route_refuses_is_reported_and_the_rest_still_resume(
    stop_record, monkeypatch, capsys
):
    refused = RuntimeError(
        'POST /api/chat/slots/s1/continue: HTTP 409 {"error": "busy now", "code": "slot_running"}'
    )
    gateway = _gateway(monkeypatch, fails_for={"/api/chat/slots/s1/continue": refused})

    cli_resume.resume_cmd(_args("s1", "s2"))

    assert [path for _m, path, _b in gateway.pressed] == [
        "/api/chat/slots/s1/continue",
        "/api/chat/slots/s2/continue",
    ]
    out = capsys.readouterr().out
    assert "s1: left alone (HTTP 409 slot_running: busy now)" in out
    assert "s2: resuming" in out


def test_a_named_session_the_gateway_never_answers_exits_1_after_trying_the_rest(
    stop_record, monkeypatch, capsys
):
    lost = NoGateway("lost the gateway on port 4321: reset")
    gateway = _gateway(monkeypatch, fails_for={"/api/chat/slots/s1/continue": lost})

    with pytest.raises(SystemExit) as exc:
        cli_resume.resume_cmd(_args("s1", "s2"))

    assert exc.value.code == 1
    assert len(gateway.pressed) == 2
    assert "s1: no answer from the gateway" in capsys.readouterr().err


def test_a_named_session_whose_sign_in_lapses_exits_1_not_left_alone(
    stop_record, monkeypatch, capsys
):
    lapsed = RuntimeError("the dashboard session could not be renewed")
    _gateway(monkeypatch, fails_for={"/api/chat/slots/s1/continue": lapsed})

    with pytest.raises(SystemExit) as exc:
        cli_resume.resume_cmd(_args("s1"))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "s1: request failed" in captured.err
    assert "left alone" not in captured.out


@pytest.mark.parametrize(
    "fails",
    [NoGateway("no gateway secret for port 4321"), RuntimeError("POST x: HTTP 409 busy")],
    ids=["no-gateway", "refused"],
)
def test_a_failed_resume_exits_1(stop_record, monkeypatch, fails):
    restart_resume.write_last_stop(["cut-a"])
    _gateway(monkeypatch, fails=fails)

    with pytest.raises(SystemExit) as exc:
        cli_resume.resume_cmd(_args())

    assert exc.value.code == 1


def test_session_ids_and_all_together_are_refused(stop_record, monkeypatch):
    gateway = _gateway(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli_resume.resume_cmd(_args("s1", all=True))

    assert exc.value.code == 2 and gateway.pressed == []


def test_an_unreadable_record_exits_1_with_a_message_and_asks_no_gateway(
    stop_record, monkeypatch, capsys
):
    stop_record.mkdir()
    gateway = _gateway(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        cli_resume.resume_cmd(_args())

    assert exc.value.code == 1
    assert "Could not read the last stop's record" in capsys.readouterr().err
    assert gateway.events == []


def test_a_deeply_nested_record_reads_as_empty():
    assert restart_resume._names(b"[" * 100_000 + b"]" * 100_000) == []


HINT = "2 chat(s) were cut by the stop. To resume them, run: kirocrew resume"
#: What ``cli_server_restart._now`` reads as the moment the restart began.
RESTART_BEGAN = 1_000_000.0


def _restart_writing(stop_record, monkeypatch, recorded, mtime):
    """Run ``_restart`` on the service path; its stop writes *recorded* at *mtime* (``None``: no write)."""
    from kiro_crew import cli_server_restart

    monkeypatch.setattr(cli_server_restart, "_now", lambda: RESTART_BEGAN)

    def service_restart():
        if mtime is not None:
            restart_resume.write_last_stop(recorded)
            if recorded:
                os.utime(stop_record, (mtime, mtime))
        return True

    return patch(
        "kiro_crew.cli_server.service_controller.restart_service", side_effect=service_restart
    )


def _run_restart(cli_port):
    from kiro_crew.cli_server import _restart

    with (
        patch("kiro_crew.cli_server.sel", return_value=MagicMock()),
        patch("kiro_crew.cli_server._print_token_url"),
        patch("kiro_crew.cli_server._print_running_cron_runs"),
        patch("kiro_crew.cli_server.resolve_client_port", return_value=4321),
    ):
        _restart(cli_port)


@pytest.mark.parametrize("recorded", [["cut-a", "cut-b"], []], ids=["cut", "idle"])
def test_kirocrew_restart_names_kirocrew_resume_and_never_presses_it(
    recorded, stop_record, monkeypatch, capsys
):
    """A stop may have been how the operator ended those turns, so a restart only points at resume."""
    gateway = _gateway(monkeypatch)
    with _restart_writing(stop_record, monkeypatch, recorded, RESTART_BEGAN):
        _run_restart(None)

    out = capsys.readouterr().out
    assert "Restarted kirocrew service" in out
    if recorded:
        assert HINT + "\n" in out
    else:
        assert "were cut by the stop" not in out
    assert gateway.events == []
    assert restart_resume.read_last_stop() == recorded


def test_kirocrew_restart_prints_no_hint_for_a_record_an_earlier_stop_wrote(
    stop_record, monkeypatch, capsys
):
    """A crash, or no gateway running, runs no graceful stop: the old record names another stop's chats."""
    restart_resume.write_last_stop(["old-a", "old-b"])
    os.utime(stop_record, (RESTART_BEGAN - 1, RESTART_BEGAN - 1))
    with _restart_writing(stop_record, monkeypatch, [], None):
        _run_restart(None)

    out = capsys.readouterr().out
    assert "Restarted kirocrew service" in out
    assert "were cut by the stop" not in out


def test_kirocrew_restart_on_a_named_port_names_that_port_in_the_hint(
    stop_record, monkeypatch, capsys
):
    from kiro_crew import cli_server_restart

    monkeypatch.setattr(cli_server_restart, "_now", lambda: RESTART_BEGAN)
    restart_resume.write_last_stop(["cut-a", "cut-b"])
    os.utime(stop_record, (RESTART_BEGAN, RESTART_BEGAN))

    cli_server_restart._print_resume_hint(RESTART_BEGAN, 9999, 9999)
    cli_server_restart._print_resume_hint(RESTART_BEGAN, None, 4321)

    assert capsys.readouterr().out.splitlines() == [
        f"   {HINT} --port 9999",
        f"   {HINT}",
    ]


@pytest.mark.parametrize(
    "argv, sessions, everything",
    [
        pytest.param([], [], False, id="bare"),
        pytest.param(["chat-1", "chat-2"], ["chat-1", "chat-2"], False, id="named"),
        pytest.param(["--all"], [], True, id="all"),
    ],
)
def test_the_cli_routes_kirocrew_resume_to_the_resume_command(
    monkeypatch, argv, sessions, everything
):
    from kiro_crew import cli

    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(cli_resume, "resume_cmd", seen.append)
    monkeypatch.setattr("sys.argv", ["kirocrew", "resume", *argv, "--port", "5476"])

    cli.main()

    ((args,),) = [(a,) for a in seen]
    assert (args.sessions, args.all, args.port) == (sessions, everything, 5476)
