"""The CLI's node / npm / electron-builder spawns decode their pipes as UTF-8.

``_node_ok`` (``node -v``) and ``_setup_electron`` (``npm install`` and
``npx electron-builder``) used ``text=True`` with no ``encoding=``, so the
child's output was decoded with ``locale.getpreferredencoding(False)``. Node
writes UTF-8 to a pipe on every OS whatever the locale, so under a non-UTF-8
locale npm's stderr -- printed back to the operator on a failed desktop build --
came back as mojibake, or raised ``UnicodeDecodeError`` out of
``subprocess.run`` before the failure message could print at all.

The spy tests pin the decode on each spawn; the real-pipe test proves the pinned
kwargs decode a node-shaped UTF-8 stream correctly on any host locale.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from kiro_crew import cli as kc_cli
from kiro_crew import cli_setup

# The real spawn primitive, captured before any test patches
# ``subprocess.run`` (the stubs patch the shared module attribute).
_REAL_RUN = subprocess.run

# What npm prints for a path under a non-ASCII home directory.
_NON_ASCII = "npm ERR! path /opt/projet-José/électron/node_modules/ü"


def _assert_utf8_decode(kwargs: dict) -> None:
    assert kwargs.get("encoding") == "utf-8", kwargs
    assert kwargs.get("errors") == "replace", kwargs


def test_node_version_probe_decodes_utf8(monkeypatch):
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="v22.14.0\n")

    monkeypatch.setattr(kc_cli.shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(kc_cli.subprocess, "run", fake_run)

    assert kc_cli._node_ok() is True
    assert len(seen) == 1
    _assert_utf8_decode(seen[0])


def _spawn_kwargs_of_setup_electron(monkeypatch) -> dict[str, dict]:
    """Run ``_setup_electron`` with stubbed spawns; return each spawn's kwargs by argv[0]."""
    monkeypatch.setattr(cli_setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli_setup.shutil, "which", lambda _name: "/usr/bin/node")
    monkeypatch.setattr(cli_setup, "_find_electron_dir", lambda: Path("/electron"))
    seen: dict[str, dict] = {}

    def fake_run(argv, **kwargs):
        seen[argv[0]] = kwargs
        # Fail the build step so the function returns before touching the
        # filesystem; the install step's kwargs are recorded either way.
        failing = argv[0] == "npx"
        return SimpleNamespace(returncode=1 if failing else 0, stdout="", stderr="boom")

    monkeypatch.setattr(cli_setup.subprocess, "run", fake_run)
    cli_setup._setup_electron()
    return seen


def test_desktop_npm_install_decodes_utf8(monkeypatch, capsys):
    seen = _spawn_kwargs_of_setup_electron(monkeypatch)
    _assert_utf8_decode(seen["npm"])


def test_desktop_electron_builder_decodes_utf8(monkeypatch, capsys):
    seen = _spawn_kwargs_of_setup_electron(monkeypatch)
    _assert_utf8_decode(seen["npx"])


def test_pinned_kwargs_decode_a_real_utf8_stderr(monkeypatch, capsys):
    """Replay the npm spawn's own kwargs against a real child writing UTF-8 bytes.

    The child writes raw bytes, so what reaches the pipe does not depend on its
    locale either; the decoded text depends only on the kwargs under test.
    """
    kwargs = dict(_spawn_kwargs_of_setup_electron(monkeypatch)["npm"])
    kwargs.pop("cwd", None)
    # One trailing byte that is not valid UTF-8 at all: it must degrade to
    # U+FFFD, not raise out of ``subprocess.run``.
    payload = _NON_ASCII.encode("utf-8") + b" \xff"
    child = (
        "import sys; sys.stderr.buffer.write(bytes.fromhex(%r)); sys.stderr.flush()" % payload.hex()
    )
    result = _REAL_RUN([sys.executable, "-c", child], check=False, **kwargs)
    assert result.stderr == _NON_ASCII + " \ufffd"
