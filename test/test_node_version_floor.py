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
from pathlib import Path

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
def test_below_the_floor_fails_with_the_exact_version(monkeypatch, version: str) -> None:
    parsed = parse_node_version(version)
    assert parsed is not None
    assert not node_version_meets_floor(parsed)
    # Pin a modern glibc so the default (nodejs.org / nvm) remedy is asserted
    # deterministically regardless of the glibc the test host actually ships.
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: (2, 35))
    assert node_too_old_message(parsed) == (
        f"Node.js {version} is too old: Kiro Crew needs {FLOOR_TEXT} or newer. "
        "Update Node.js: install 24 LTS from https://nodejs.org, or run "
        "`nvm install 24` / `mise use -g node@24`."
    )


@pytest.mark.parametrize("glibc", [(2, 26), (2, 17), (2, 27), (1, 99)])
def test_old_glibc_x86_64_with_script_names_the_ensure_node_path(monkeypatch, glibc) -> None:
    # Amazon Linux 2 ships glibc 2.26: official Node >= 18 binaries fail to LOAD
    # ("GLIBC_2.28 not found"), so the default nodejs.org / nvm advice is a dead
    # end. On x86_64 where the bundled script ships, the message names `bash
    # <path>` directly (ensure-node.sh unpacks the glibc-2.17 build), with the
    # action FIRST, NOT the updater (a no-op when the checkout is up to date).
    script_path = Path("/opt/kc/ensure-node.sh")
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: glibc)
    monkeypatch.setattr(constants.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr("kiro_crew.env._ensure_node_script", lambda: script_path)
    msg = node_too_old_message((17, 9, 1))
    assert f"Kiro Crew needs {FLOOR_TEXT} or newer" in msg
    # str(Path) is OS-shaped, so assert the form the code actually emits.
    assert f"bash {script_path}" in msg
    assert "glibc-2.17 Node" in msg
    assert f"glibc ({glibc[0]}.{glibc[1]})" in msg
    # The dead-end advice must NOT be offered.
    assert "nvm install 24" not in msg
    assert "install 24 LTS" not in msg
    assert "kirocrew update" not in msg
    # Action leads, explanation trails (reason is parenthetical at the end).
    assert msg.index(f"bash {script_path}") < msg.index("older than the official")


@pytest.mark.parametrize("glibc", [(2, 26), (2, 17), (2, 27), (1, 99)])
def test_old_glibc_x86_64_wheel_points_at_the_unofficial_build(monkeypatch, glibc) -> None:
    # pip/wheel install on x86_64: no bundled script (_ensure_node_script ->
    # None), but the x64 glibc-2.17 build STILL runs here -- so point the user at
    # it directly, never at a costly machine migration or build-from-source.
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: glibc)
    monkeypatch.setattr(constants.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr("kiro_crew.env._ensure_node_script", lambda: None)
    msg = node_too_old_message((17, 9, 1))
    # Assert the full remedy phrase (with scheme + surrounding words) rather than
    # a bare host substring -- a bare "host in url" check trips CodeQL's
    # py/incomplete-url-substring-sanitization, and this is expected OUTPUT text,
    # not URL validation.
    assert "Install the glibc-2.17 Node build from https://unofficial-builds.nodejs.org" in msg
    assert f"glibc ({glibc[0]}.{glibc[1]})" in msg
    assert "build Node from source" not in msg  # the build exists; don't say that
    assert "No glibc-2.17 Node build is published" not in msg
    assert "nvm install 24" not in msg
    assert "install 24 LTS" not in msg


@pytest.mark.parametrize("machine", ["aarch64", "arm64"])
def test_old_glibc_aarch64_says_no_arm_build_exists(monkeypatch, machine) -> None:
    # aarch64 / Graviton: upstream publishes no arm64 glibc-2.17 build, so the
    # message must NOT offer ensure-node.sh / unofficial-builds and must tell the
    # truth: no ARM build exists; move to a newer base or build from source.
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: (2, 26))
    monkeypatch.setattr(constants.platform, "machine", lambda: machine)
    msg = node_too_old_message((17, 9, 1))
    assert "No glibc-2.17 Node build is published for this CPU" in msg
    assert "Amazon Linux 2023" in msg
    assert "build Node from source" in msg
    assert "ensure-node.sh" not in msg
    assert "Install the glibc-2.17 Node build from https://unofficial-builds.nodejs.org" not in msg
    assert "kirocrew update" not in msg
    assert "nvm install 24" not in msg
    assert "install 24 LTS" not in msg


@pytest.mark.parametrize("glibc", [(2, 28), (2, 35), (3, 0)])
def test_modern_glibc_keeps_the_default_remedy(monkeypatch, glibc) -> None:
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: glibc)
    msg = node_too_old_message((20, 19, 0))
    assert "install 24 LTS" in msg
    assert "nvm install 24" in msg
    assert "ensure-node.sh" not in msg


def test_non_glibc_host_keeps_the_default_remedy(monkeypatch) -> None:
    # macOS / Windows / musl / unreadable: libc_ver gives no glibc, so the
    # reader returns None and the default remedy stands.
    monkeypatch.setattr(constants, "_host_glibc_version", lambda: None)
    msg = node_too_old_message((20, 19, 0))
    assert "install 24 LTS" in msg
    assert "ensure-node.sh" not in msg


def test_host_glibc_version_is_none_off_linux(monkeypatch) -> None:
    import platform as _platform

    monkeypatch.setattr(_platform, "system", lambda: "Darwin")
    assert constants._host_glibc_version() is None


def test_host_glibc_version_reads_libc_ver(monkeypatch) -> None:
    import platform as _platform

    monkeypatch.setattr(_platform, "system", lambda: "Linux")
    monkeypatch.setattr(_platform, "libc_ver", lambda *a, **k: ("glibc", "2.26"))
    assert constants._host_glibc_version() == (2, 26)
    # A non-glibc libc (musl) or an empty/unparseable version yields None.
    monkeypatch.setattr(_platform, "libc_ver", lambda *a, **k: ("libc", ""))
    assert constants._host_glibc_version() is None
    monkeypatch.setattr(_platform, "libc_ver", lambda *a, **k: ("glibc", "weird"))
    assert constants._host_glibc_version() is None
    # musl with a PARSEABLE version must still be rejected on the name, not the
    # version-parse path -- removing the `name != "glibc"` guard reddens here.
    monkeypatch.setattr(_platform, "libc_ver", lambda *a, **k: ("musl", "1.2"))
    assert constants._host_glibc_version() is None


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
