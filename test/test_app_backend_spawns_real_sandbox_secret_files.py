"""An app backend and its install spawns cannot read the gateway's secret files.

``.local_secret`` and ``run/gateway-<port>.secret`` hold the credential of the gateway's
internal routes. The spawns of an app backend (the server itself, ``npm install``, ``pip
install``) run third-party code, so none of them may read it.

The sibling tests only check the arguments handed to ``wrap_argv``. This one goes through
the real launcher: the production spawn code runs unchanged up to the sandbox wrap, where
the test swaps the command for a probe and hands the wrap the arguments production built
(the real hide list included). The probe runs under the real namespace sandbox and
reports what reading each secret file gave. The test process plays the gateway.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401
from apps_backend_coverage_helpers import _capture_popen, _manifest, _record_runs, _StopSpawn

import kiro_crew.apps.backend as bmod
from kiro_crew import sandbox
from kiro_crew.apps.backend_runtime import provisioning
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
    pytest.mark.xdist_group("app_backend_spawns_real_sandbox"),
]

# Reads every path given, then a control file that is NOT secret. A hidden file reads as
# an error or empty; the control must still read, or the probe proves nothing.
_PROBE = r"""
import errno, json, os, sys

*secrets, control = sys.argv[1:]

def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return {"ok": True, "value": fh.read()}
    except OSError as exc:
        return {"ok": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "secrets": {path: read(path) for path in secrets},
    "control": read(control),
}
print("REPORT " + json.dumps(report), flush=True)
"""

_SECRET_VALUE = "gateway-internal-secret-value"
_CONTROL_VALUE = "not-a-secret"


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    # The mask list is planned from the resolved data home and ``$HOME``, so both are
    # pinned: no step of the launch may touch the developer's real data home.
    home = (tmp_path / "home").resolve()
    (home / "run").mkdir(parents=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    secrets = [home / ".local_secret", home / "run" / "gateway-8765.secret"]
    for path in secrets:
        path.write_text(_SECRET_VALUE, encoding="utf-8")
    control = home / "run" / "control.txt"
    control.write_text(_CONTROL_VALUE, encoding="utf-8")
    sandbox.reset_backend()
    yield {"secrets": [str(p) for p in secrets], "control": str(control)}
    sandbox.reset_backend()


def _install_real_probe(
    monkeypatch: pytest.MonkeyPatch, seeded: dict[str, Any], match: Any
) -> list[dict[str, Any]]:
    """Run the probe through the real wrap whenever production wraps a command *match* accepts.

    The wrap gets the keyword arguments production built, so a missing hide list shows up
    as a readable secret. Every other wrap call passes through untouched.
    """
    reports: list[dict[str, Any]] = []
    real_wrap = sandbox.wrap_argv

    def _wrap(argv: Any, **kwargs: Any) -> Any:
        if not match(list(argv)):
            return (list(argv), None)
        probe = [sys.executable, "-c", _PROBE, *seeded["secrets"], seeded["control"]]
        wrapped, cleanup = real_wrap(probe, **kwargs)
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
        try:
            proc = subprocess.run(
                wrapped,
                cwd=os.environ["KIROCREW_HOME"],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,  # flake-ok: kill deadline of a one-shot probe child, not a wait
            )
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)
        output = proc.stdout + proc.stderr
        assert proc.returncode == 0, output
        (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
        reports.append(json.loads(line[len("REPORT ") :]))
        return (list(argv), None)

    monkeypatch.setattr(bmod, "wrap_argv", _wrap)
    monkeypatch.setattr(provisioning, "wrap_argv", _wrap)
    return reports


def _assert_secrets_hidden(reports: list[dict[str, Any]], seeded: dict[str, Any]) -> None:
    assert len(reports) == 1, reports
    report = reports[0]
    # It really ran confined: the launcher marked it, in a mount namespace of its own.
    assert report["marker"] == "1", report
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), report
    # The control reads, so a hidden file is hidden by the plan and not by a broken probe.
    assert report["control"] == {"ok": True, "value": _CONTROL_VALUE}, report
    for path in seeded["secrets"]:
        got = report["secrets"][path]
        assert got != {"ok": True, "value": _SECRET_VALUE}, f"{path} is readable: {report}"
        assert got.get("value", "") == "", f"{path} gave content: {report}"


class TestARealSandboxHidesTheSecretFiles:
    def test_the_backend_spawn_cannot_read_them(
        self, spawn_root: Any, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        reports = _install_real_probe(monkeypatch, seeded, lambda argv: "server.py" in argv[-1])
        _capture_popen(monkeypatch)

        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("realspawn", _manifest("server.py"))

        _assert_secrets_hidden(reports, seeded)

    def test_the_npm_install_spawn_cannot_read_them(
        self, spawn_root: Any, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.js").write_text("// noop\n")
        (spawn_root / "package.json").write_text(json.dumps({"name": "x"}))
        monkeypatch.setattr(bmod, "_find_node_binary", lambda: "/usr/bin/node")
        monkeypatch.setattr(bmod, "_find_npm_binary", lambda: "/usr/bin/npm")
        reports = _install_real_probe(
            monkeypatch, seeded, lambda argv: argv[:2] == ["/usr/bin/npm", "install"]
        )
        _record_runs(monkeypatch)
        _capture_popen(monkeypatch)

        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("realnpm", _manifest("server.js"))

        _assert_secrets_hidden(reports, seeded)

    def test_the_pip_install_spawn_cannot_read_them(
        self, spawn_root: Any, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (spawn_root / "server.py").write_text("x = 1\n")
        (spawn_root / "requirements.txt").write_bytes(b"requests\n")
        reports = _install_real_probe(
            monkeypatch, seeded, lambda argv: "pip" in argv and "install" in argv
        )
        _record_runs(monkeypatch)
        _capture_popen(monkeypatch)

        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body("realpip", _manifest("server.py"))

        _assert_secrets_hidden(reports, seeded)
