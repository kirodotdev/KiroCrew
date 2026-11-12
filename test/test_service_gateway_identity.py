"""Whether the running kirocrew service is this gateway, from the service manager's answer.

A unit installed before the managed-service marker existed has no marker, so
the restart idle check reads the main pid of each running unit a service restart
reaches instead (``kiro_crew.service.gateway_identity``). Any of them that is not
this gateway refuses; nothing read at all is unknown.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew import gateway_restart
from kiro_crew.pod import launchd as pod_launchd
from kiro_crew.service import common, gateway_identity, linux, macos

MINE = {4242, 4241}


@pytest.mark.parametrize(
    ("pids", "verdict"),
    [
        ([4242], "service"),
        ([4241], "service"),  # the launcher that exec-ed or spawned it
        ([999], "other"),
        ([4242, 999], "other"),  # the restart would also stop an unchecked gateway
        ([], "unknown"),
    ],
)
def test_the_verdict(pids, verdict):
    assert gateway_identity.judge(pids, MINE) == verdict


def _systemd(monkeypatch, answers: dict[bool, object], *, user_reachable: bool = True):
    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.SYSTEMD)
    monkeypatch.setattr(
        linux, "_user_scope_unreachable_reason", lambda: None if user_reachable else "no bus"
    )
    monkeypatch.setattr(gateway_identity.os, "getpid", lambda: 4242)
    monkeypatch.setattr(gateway_identity.os, "getppid", lambda: 1)
    asked: list[bool] = []

    def _show(user):
        asked.append(user)
        answer = answers[user]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(gateway_restart, "_systemd_show", _show)
    return asked


def _shown(pid: str, *, unit: str = "kirocrew.service", load: str = "loaded") -> dict:
    return {"Id": [unit], "LoadState": [load], "MainPID": [pid]}


def test_a_system_unit_whose_main_pid_is_this_gateway_is_the_service(monkeypatch):
    _systemd(monkeypatch, {False: _shown("4242"), True: _shown("0", load="not-found")})
    assert gateway_identity.service_identity() == "service"


def test_a_unit_running_another_process_is_other(monkeypatch):
    _systemd(monkeypatch, {False: _shown("999"), True: _shown("0", load="not-found")})
    assert gateway_identity.service_identity() == "other"


def test_another_running_scope_makes_a_match_other(monkeypatch):
    """The service restart restarts both scopes; one of them is not this gateway."""
    _systemd(monkeypatch, {False: _shown("999"), True: _shown("4242")})
    assert gateway_identity.service_identity() == "other"


def test_an_unreachable_user_scope_is_skipped_as_the_restart_skips_it(monkeypatch):
    """Another gateway answering beside a busy system unit is still caught."""
    asked = _systemd(monkeypatch, {False: _shown("999")}, user_reachable=False)
    assert gateway_identity.service_identity() == "other"
    assert asked == [False]


def test_an_alias_is_not_the_service(monkeypatch):
    """``show kirocrew.service`` on an alias answers for another unit, which restart never selects."""
    _systemd(
        monkeypatch,
        {False: _shown("4242", unit="other-app.service"), True: _shown("999")},
    )
    assert gateway_identity.service_identity() == "other"


def test_an_alias_alone_is_unknown(monkeypatch):
    _systemd(
        monkeypatch,
        {False: _shown("4242", unit="other-app.service"), True: _shown("0", load="not-found")},
    )
    assert gateway_identity.service_identity() == "unknown"


def test_no_trusted_systemctl_is_unknown(monkeypatch):
    _systemd(
        monkeypatch,
        {False: gateway_restart._Inconclusive("no systemctl"), True: _shown("999")},
    )
    assert gateway_identity.service_identity() == "unknown"


def test_a_timed_out_scope_adds_nothing(monkeypatch):
    _systemd(
        monkeypatch,
        {False: subprocess.TimeoutExpired(["systemctl"], 3.0), True: _shown("0", load="not-found")},
    )
    assert gateway_identity.service_identity() == "unknown"


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired(["systemctl"], 3.0), OSError("gone")]
)
def test_a_later_scope_failure_keeps_the_mismatch_already_read(monkeypatch, failure):
    """A busy system unit's other pid still refuses when the user scope then fails."""
    _systemd(monkeypatch, {False: _shown("999"), True: failure})
    assert gateway_identity.service_identity() == "other"


def test_an_earlier_scope_failure_still_reads_the_next_scope(monkeypatch):
    _systemd(
        monkeypatch,
        {False: subprocess.TimeoutExpired(["systemctl"], 3.0), True: _shown("999")},
    )
    assert gateway_identity.service_identity() == "other"


def test_the_shared_reader_asks_for_the_canonical_id():
    """One ``systemctl show`` reader for this unit, and it reads the alias guard's ``Id``."""
    import inspect

    assert '"Id"' in inspect.getsource(gateway_restart._systemd_show)


def _launchd(monkeypatch, answers: dict[str, SimpleNamespace], tmp_path):
    plist = tmp_path / "agent.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.LAUNCHD)
    monkeypatch.setattr(macos, "PLIST_PATH", plist)
    monkeypatch.setattr(macos, "_legacy_domains", lambda: ("gui/501", "user/501"))
    monkeypatch.setattr(
        gateway_identity.platform_compat, "trusted_system_bin", lambda name: f"/bin/{name}"
    )
    argvs: list[list[str]] = []

    def _run(argv, **_kwargs):
        argvs.append(list(argv))
        answer = answers[argv[2].split("/")[0]]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(gateway_identity.subprocess, "run", _run)
    monkeypatch.setattr(pod_launchd, "_service_absent", lambda res: res.returncode == 113)
    monkeypatch.setattr(macos, "_domain_unavailable", lambda res: res.returncode == 125)
    monkeypatch.setattr(gateway_identity.os, "getpid", lambda: 4242)
    monkeypatch.setattr(gateway_identity.os, "getppid", lambda: 1)
    return argvs


def _printed(pid: str | None, rc: int = 0) -> SimpleNamespace:
    body = "state = running\n" + (f"\tpid = {pid}\n" if pid is not None else "")
    return SimpleNamespace(returncode=rc, stdout=body)


def test_a_launchd_agent_whose_pid_is_this_gateway_is_the_service(monkeypatch, tmp_path):
    _launchd(monkeypatch, {"gui": _printed("4242"), "user": _printed(None, 113)}, tmp_path)
    assert gateway_identity.service_identity() == "service"


def test_a_launchd_agent_running_another_process_is_other(monkeypatch, tmp_path):
    _launchd(monkeypatch, {"gui": _printed("999"), "user": _printed(None, 113)}, tmp_path)
    assert gateway_identity.service_identity() == "other"


def test_only_the_domain_the_restart_kickstarts_counts(monkeypatch, tmp_path):
    """Jobs in both domains: ``macos.restart()`` kickstarts gui/<uid> only, so user/<uid> is not read."""
    _launchd(monkeypatch, {"gui": _printed("4242"), "user": _printed("999")}, tmp_path)
    assert gateway_identity.service_identity() == "service"


def test_a_missing_first_domain_falls_through_to_the_next(monkeypatch, tmp_path):
    _launchd(monkeypatch, {"gui": _printed(None, 125), "user": _printed("999")}, tmp_path)
    assert gateway_identity.service_identity() == "other"


def test_an_absent_first_domain_falls_through_to_the_next(monkeypatch, tmp_path):
    """113 (no such job) in gui/<uid> moves on to user/<uid>, as ``macos.restart()`` does."""
    _launchd(monkeypatch, {"gui": _printed(None, 113), "user": _printed("999")}, tmp_path)
    assert gateway_identity.service_identity() == "other"


def test_a_launchd_print_that_fails_reads_nothing(monkeypatch, tmp_path):
    """``restart()`` stops at that domain too, so the next one is not judged."""
    _launchd(monkeypatch, {"gui": _printed(None, 5), "user": _printed("999")}, tmp_path)
    assert gateway_identity.service_identity() == "unknown"


def test_a_launchd_domain_that_times_out_reads_nothing(monkeypatch, tmp_path):
    _launchd(
        monkeypatch,
        {"gui": subprocess.TimeoutExpired(["launchctl"], 3.0), "user": _printed("999")},
        tmp_path,
    )
    assert gateway_identity.service_identity() == "unknown"


def test_launchctl_runs_from_its_trusted_absolute_path(monkeypatch, tmp_path):
    """The gateway's PATH can lead with an agent-writable directory; a bare name is never run."""
    argvs = _launchd(monkeypatch, {"gui": _printed("4242")}, tmp_path)
    assert gateway_identity.service_identity() == "service"
    assert argvs == [["/bin/launchctl", "print", f"gui/501/{macos.LAUNCHD_LABEL}"]]


def test_no_trusted_launchctl_is_unknown_and_runs_nothing(monkeypatch, tmp_path):
    argvs = _launchd(monkeypatch, {"gui": _printed("999")}, tmp_path)
    monkeypatch.setattr(gateway_identity.platform_compat, "trusted_system_bin", lambda name: None)
    assert gateway_identity.service_identity() == "unknown"
    assert argvs == []


def test_a_platform_without_a_service_manager_is_unknown(monkeypatch):
    monkeypatch.setattr(common, "current_platform", lambda: common.Platform.UNSUPPORTED)
    assert gateway_identity.service_identity() == "unknown"


def _show_with(monkeypatch, stdout: str | None, *, systemctl: str | None = "/usr/bin/systemctl"):
    monkeypatch.setattr(
        gateway_restart.platform_compat,
        "trusted_system_bin",
        lambda name: systemctl if name == "systemctl" else None,
    )
    monkeypatch.setattr(common, "systemctl_user_env", lambda: {"XDG_RUNTIME_DIR": "/run/user/501"})
    runs: list[dict] = []

    def _run(argv, **kwargs):
        runs.append({"argv": list(argv), **kwargs})
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(gateway_restart.subprocess, "run", _run)
    return runs


def test_the_unit_reader_asks_one_scope_for_the_properties_the_identity_check_reads(
    monkeypatch,
):
    runs = _show_with(monkeypatch, "")

    gateway_restart._systemd_show(user=True)
    gateway_restart._systemd_show(user=False)

    props = []
    for name in (
        "Id",
        "LoadState",
        "MainPID",
        "NeedDaemonReload",
        "ExecStart",
        "Environment",
        "EnvironmentFiles",
    ):
        props += ["-p", name]
    unit = f"{common.SERVICE_NAME}.service"
    assert runs[0]["argv"] == ["/usr/bin/systemctl", "--user", "show", *props, unit]
    assert runs[0]["env"] == {"XDG_RUNTIME_DIR": "/run/user/501"}
    assert runs[1]["argv"] == ["/usr/bin/systemctl", "show", *props, unit]
    assert runs[1]["env"] is None
    assert all(run["timeout"] == gateway_restart._SYSTEMCTL_TIMEOUT_SECS for run in runs)
    assert all(run["check"] is False for run in runs)


def test_the_unit_reader_keeps_every_value_of_a_repeated_property(monkeypatch):
    _show_with(
        monkeypatch,
        "Id=kirocrew.service\n"
        "MainPID= 4242 \n"
        "Environment=A=1 B=2\n"
        "Environment=C=3\n"
        "a line with no separator\n",
    )

    props = gateway_restart._systemd_show(user=False)

    assert props == {
        "Id": ["kirocrew.service"],
        "MainPID": ["4242"],
        "Environment": ["A=1 B=2", "C=3"],
    }


def test_a_unit_reader_with_no_output_reads_no_properties(monkeypatch):
    _show_with(monkeypatch, None)

    assert gateway_restart._systemd_show(user=True) == {}


def test_no_trusted_systemctl_is_inconclusive_and_runs_nothing(monkeypatch):
    runs = _show_with(monkeypatch, "Id=kirocrew.service\n", systemctl=None)

    with pytest.raises(gateway_restart._Inconclusive, match="trusted system directory"):
        gateway_restart._systemd_show(user=False)

    assert runs == []


def test_the_launcher_target_is_what_its_last_exec_line_runs(tmp_path):
    launcher = tmp_path / "launch.sh"
    launcher.write_text(
        "#!/bin/sh\n"
        "exec /old/python -m kiro_crew\n"
        "export KIROCREW_X=1\n"
        "exec '/Applications/Kiro Crew.app/python3' -m kiro_crew \"$@\"\n",
        encoding="utf-8",
    )

    assert gateway_restart._launcher_target(launcher) == "/Applications/Kiro Crew.app/python3"


@pytest.mark.parametrize("body", ["#!/bin/sh\necho no exec here\n", "#!/bin/sh\nexec \n"])
def test_a_launcher_with_no_exec_target_names_itself(tmp_path, body):
    launcher = tmp_path / "launch.sh"
    launcher.write_text(body, encoding="utf-8")

    assert gateway_restart._launcher_target(launcher) == str(launcher)


def test_a_missing_launcher_names_itself(tmp_path):
    launcher = tmp_path / "gone.sh"

    assert gateway_restart._launcher_target(launcher) == str(launcher)


def test_an_unreadable_launcher_is_inconclusive(tmp_path):
    launcher = tmp_path / "launch.sh"
    launcher.write_bytes(b"#!/bin/sh\nexec /usr/bin/\xff\xfe\n")

    with pytest.raises(gateway_restart._Inconclusive, match="could not be read"):
        gateway_restart._launcher_target(launcher)
