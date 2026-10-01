"""The Node floor is a full major.minor.patch, shared by startup and doctor.

A major-only compare (``major >= 22``) admitted an early 22.x. A recent undici
fetch client calls ``worker_threads.markAsUncloneable``, which first shipped in
Node 22.10.0, so on such a Node it fails with
"webidl.util.markAsUncloneable is not a function". The frontend bundler's own
floor (``>=22.12.0``) is stricter still, so that is the floor.
"""

from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout

import pytest

from kiro_crew import cli as kc_cli
from kiro_crew import cli_doctor, constants
from kiro_crew.constants import (
    MIN_NODE_VERSION,
    node_too_old_message,
    node_version_meets_floor,
    parse_node_version,
)

FLOOR_TEXT = "v22.12.0"


def test_the_floor_is_the_first_22_with_both_needs() -> None:
    # markAsUncloneable: 22.10.0; vite/rolldown engines: >=22.12.0.
    assert MIN_NODE_VERSION == (22, 12, 0)


def test_doctor_and_startup_share_one_constant() -> None:
    assert kc_cli.MIN_NODE_VERSION is constants.MIN_NODE_VERSION
    assert cli_doctor.MIN_NODE_VERSION is constants.MIN_NODE_VERSION
    assert not hasattr(constants, "MIN_NODE_MAJOR")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v22.12.0\n", (22, 12, 0)),
        ("22.3.1", (22, 3, 1)),
        ("  v24.18.0", (24, 18, 0)),
        ("", None),
        ("v22", None),
        ("garbage", None),
        (None, None),
    ],
)
def test_parse_node_version(text, expected) -> None:
    assert parse_node_version(text) == expected


@pytest.mark.parametrize("version", ["v22.0.0", "v22.9.0", "v22.11.0", "v22.11.9", "v20.19.0"])
def test_below_the_floor_fails_with_the_exact_version(version: str) -> None:
    parsed = parse_node_version(version)
    assert parsed is not None
    assert not node_version_meets_floor(parsed)
    assert node_too_old_message(parsed) == (
        f"Node.js {version} is too old: Kiro Crew needs {FLOOR_TEXT} or newer. "
        "Update Node.js: install 24 LTS from https://nodejs.org, or run "
        "`nvm install 24` / `mise use -g node@24`."
    )


@pytest.mark.parametrize(
    "version", ["v22.12.0", "v22.12.1", "v22.20.0", "v23.0.0", "v24.0.0", "v24.18.0"]
)
def test_the_floor_and_later_pass(version: str) -> None:
    parsed = parse_node_version(version)
    assert parsed is not None
    assert node_version_meets_floor(parsed)


def test_doctor_spawns_the_resolved_node_path(monkeypatch) -> None:
    resolved = r"C:\Users\dev\AppData\Roaming\npm\node.CMD"
    monkeypatch.setattr(
        cli_doctor.shutil, "which", lambda name, **_kw: resolved if name == "node" else None
    )
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="v22.14.0\n")

    monkeypatch.setattr(cli_doctor.subprocess, "run", fake_run)
    with redirect_stdout(io.StringIO()):
        cli_doctor._report_node([])
    assert seen == [[resolved, "-v"]]


def _fake_node(monkeypatch, module, stdout: str) -> None:
    monkeypatch.setattr(
        module.shutil, "which", lambda name, **_kw: "/usr/bin/node" if name == "node" else None
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout),
    )


@pytest.mark.parametrize(
    ("stdout", "warns"),
    [
        ("v22.0.0\n", True),
        ("v22.11.0\n", True),
        ("v22.12.0\n", False),
        ("v22.14.0\n", False),
        ("v24.18.0\n", False),
    ],
)
def test_startup_warns_below_the_full_floor(monkeypatch, caplog, stdout: str, warns: bool) -> None:
    _fake_node(monkeypatch, kc_cli, stdout)
    with caplog.at_level("WARNING"):
        # The boot repair trigger stays major-only: every 22.x and later passes.
        assert kc_cli._node_ok() is True
    assert (f"needs {FLOOR_TEXT} or newer" in caplog.text) is warns


@pytest.mark.parametrize("stdout", ["v20.19.0\n", "v18.20.8\n"])
def test_startup_probe_still_fails_below_the_major(monkeypatch, stdout: str) -> None:
    _fake_node(monkeypatch, kc_cli, stdout)
    assert kc_cli._node_ok() is False


@pytest.mark.parametrize(
    ("stdout", "ok"),
    [
        ("v22.0.0\n", False),
        ("v22.11.0\n", False),
        ("v22.12.0\n", True),
        ("v22.14.0\n", True),
        ("v24.18.0\n", True),
    ],
)
def test_doctor_fails_below_the_full_floor(monkeypatch, stdout: str, ok: bool) -> None:
    _fake_node(monkeypatch, cli_doctor, stdout)
    issues: list[str] = []
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_doctor._report_node(issues)
    out = buf.getvalue()
    if ok:
        assert "✅" in out and "too old" not in out
        assert issues == []
    else:
        assert "❌" in out
        assert f"needs {FLOOR_TEXT} or newer" in out
        assert issues == ["node"]


def test_doctor_names_the_exact_floor_when_node_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(cli_doctor.shutil, "which", lambda name, **_kw: None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_doctor._report_node([])
    assert FLOOR_TEXT in buf.getvalue()
