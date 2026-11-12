"""``kirocrew pod --help`` names only mechanisms the host actually runs.

Only Linux has the systemd ``--user`` template unit and a journal. launchd and
Task Scheduler route a pod's stdout/stderr to files and have nothing to install
(``runtime_lifecycle.install_backend``), and Windows refuses ``pod restart``
(``runtime_lifecycle.restart_pod``). The parser reads the platform when it is
built, so each test patches ``platform_compat`` and builds a fresh one.
"""

from __future__ import annotations

import argparse

import pytest

from kiro_crew import cli_parser_admin, platform_compat


def _pod_parser(
    monkeypatch: pytest.MonkeyPatch, *, windows: bool, macos: bool
) -> argparse.ArgumentParser:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)
    monkeypatch.setattr(platform_compat, "IS_MACOS", macos)
    root = argparse.ArgumentParser(prog="kirocrew")
    cli_parser_admin.register_pod_parser(root.add_subparsers(dest="command"))
    sub = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices["pod"]


def _verbs(pod: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(a for a in pod._actions if isinstance(a, argparse._SubParsersAction))


def _verb_help(pod: argparse.ArgumentParser) -> dict[str, str]:
    """The one-line help each visible verb prints in ``pod --help``."""
    return {a.dest: a.help or "" for a in _verbs(pod)._choices_actions}


def _every_help_string(pod: argparse.ArgumentParser) -> str:
    """``pod --help`` plus every verb's own ``--help``, as printed."""
    verbs = _verbs(pod)
    parts = [pod.format_help()]
    parts += [verbs.choices[v].format_help() for v in _verb_help(pod)]
    return "\n".join(parts)


_EXPECTED_VERBS = {
    "up", "restart", "down", "ls", "prune", "status", "token", "url",
    "scenarios", "api", "logs", "exec", "provision", "install",
}  # fmt: skip


@pytest.mark.parametrize(("windows", "macos"), [(True, False), (False, True)])
def test_no_linux_only_mechanism_is_claimed_off_linux(
    monkeypatch: pytest.MonkeyPatch, windows: bool, macos: bool
) -> None:
    pod = _pod_parser(monkeypatch, windows=windows, macos=macos)
    assert set(_verb_help(pod)) == _EXPECTED_VERBS
    text = _every_help_string(pod)
    # MemoryMax / CPUQuota appear only under an explicit "on Linux" qualifier in
    # the --memory / --cpu help, so the unqualified Linux mechanisms are these two.
    for line in text.splitlines():
        for linux_only in ("systemd", "journal"):
            assert linux_only not in line, f"{linux_only!r} in help off Linux: {line.strip()!r}"
    helps = _verb_help(pod)
    assert helps["logs"] == "Tail a pod's stdout/stderr log files"
    assert helps["install"].startswith("Linux only; a no-op here")


def test_windows_help_says_restart_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    pod = _pod_parser(monkeypatch, windows=True, macos=False)
    helps = _verb_help(pod)
    assert helps["restart"] == "Not supported on Windows; use `pod down` then `pod up`"
    restart = _verbs(pod).choices["restart"]
    assert "Not supported on Windows" in (restart.description or "")
    assert "start it again from its env file" not in restart.format_help()
    up_help = _verbs(pod).choices["up"].format_help()
    assert "service-manager restart" not in up_help


def test_macos_keeps_restart_and_approval_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    pod = _pod_parser(monkeypatch, windows=False, macos=True)
    assert _verb_help(pod)["restart"].startswith("Restart a pod in place")
    assert "service-manager restart" in " ".join(_verbs(pod).choices["up"].format_help().split())


def test_linux_wording_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    pod = _pod_parser(monkeypatch, windows=False, macos=False)
    helps = _verb_help(pod)
    assert helps["logs"] == "Tail a pod's journal"
    assert helps["install"] == "Lay down the systemd --user template unit (once)"
    assert helps["restart"] == (
        "Restart a pod in place, keeping its home (applies new --memory/--cpu)"
    )
    restart = _verbs(pod).choices["restart"]
    assert (restart.description or "").startswith("Stop the pod's gateway, keep its isolated home")
    up_help = " ".join(_verbs(pod).choices["up"].format_help().split())
    assert "Persisted per pod so it survives a service-manager restart." in up_help
