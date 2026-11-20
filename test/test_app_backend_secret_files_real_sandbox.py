"""An app backend started at gateway boot cannot read the credential written after it.

A backend that starts at boot is spawned BEFORE the gateway writes this generation's
``run/gateway-<port>*.secret`` files and ``.local_secret``. Each test here spawns a child
under the real namespace sandbox with the mask list ``apps/backend.py`` passes, while the
test process plays the gateway: it writes the credentials with the gateway's own writer
after the child is running, and judges what the child reads from what it prints.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
    pytest.mark.xdist_group("app_backend_secret_real_sandbox"),
]

_PORT = 8765
_SECRET = "gateway-secret-written-after-the-spawn"

# Reports it is confined, waits for the gateway's write, then reads every path.
_BACKEND = r"""
import json, os, sys
print("READY " + os.readlink("/proc/self/ns/mnt"), flush=True)
sys.stdin.readline()
seen = {}
for path in sys.argv[1:]:
    try:
        seen[path] = {"read": open(path, encoding="utf-8").read()}
    except OSError as exc:
        seen[path] = {"errno": exc.errno}
print(json.dumps(seen), flush=True)
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    monkeypatch.setenv("KIROCREW_BOUND_PORT", str(_PORT))
    monkeypatch.delenv("KIROCREW_BOUND_HOST", raising=False)
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _credential_paths(home: Path) -> list[Path]:
    run = home / "run"
    return [
        home / ".local_secret",
        run / f"gateway-{_PORT}.secret",
        run / f"gateway-{_PORT}-127.0.0.1.secret",
        run / f"gateway-{_PORT}-__1.secret",
    ]


def _gateway_writes_its_credentials(home: Path) -> None:
    """The boot step that follows ``_start_app_backends``, with the gateway's own writer."""
    from kiro_crew.dashboard import server

    server._write_instance_credentials(
        home / ".local_secret", _PORT, "127.0.0.1", _SECRET, ("::1",)
    )
    for path in _credential_paths(home):
        assert path.read_text(encoding="utf-8") == _SECRET, f"the gateway did not write {path}"


def _read_as_the_backend(home: Path, hidden: tuple[str, ...]) -> dict:
    visible_file = home / "run" / "other.txt"
    paths = [*_credential_paths(home), visible_file]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.Popen(
        sandbox.namespace_argv(
            [sys.executable, "-c", _BACKEND, *map(str, paths)],
            "standard",
            extra_hidden_dirs=hidden,
        ),
        cwd=home,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        line = proc.stdout.readline()
        assert line.startswith("READY "), line
        assert line.split()[1] != os.readlink("/proc/self/ns/mnt"), "it never entered the sandbox"
        # The child is running; now the gateway publishes, as it does after the boot spawn.
        (home / "run").mkdir(exist_ok=True)
        visible_file.write_text("not a secret", encoding="utf-8")
        _gateway_writes_its_credentials(home)
        proc.stdin.write("go\n")
        proc.stdin.flush()
        report = json.loads(proc.stdout.readline())
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=60)
    assert report[str(visible_file)] == {"read": "not a secret"}, "run/ itself is not masked"
    return {Path(k): v for k, v in report.items()}


def test_a_backend_started_before_the_gateway_writes_cannot_read_the_credential(home):
    # What apps/backend.py computes for a boot spawn: nothing is on disk yet.
    assert not (home / "run").exists()
    hidden = sandbox.app_backend_secret_mask_targets()

    report = _read_as_the_backend(home, hidden)

    for path in _credential_paths(home):
        assert report[path] == {"read": ""}, f"{path} showed the credential to the backend"


def test_a_backend_spawned_with_no_names_does_read_it(home):
    """The control: the same run with only what existed at spawn shows the leak.

    Without it the test above could pass because the child failed to read anything.
    """
    report = _read_as_the_backend(home, ())

    for path in _credential_paths(home):
        assert report[path] == {"read": _SECRET}, path
