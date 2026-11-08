"""The macOS gateway service decodes launchctl output as UTF-8.

``service.macos._launchctl`` is the one spawn behind every launchctl call the
gateway service makes (``bootstrap``, ``bootout``, ``kickstart``, ``print``).
launchctl writes labels, plist paths and its error text as UTF-8, so the call
pins the decode with ``UTF8_TEXT`` instead of the locale code page. A malformed
byte degrades to U+FFFD.

The spy test pins the kwargs. The locale test runs ``_launchctl`` in a child
interpreter whose locale encoding is ASCII and feeds it a real pipe of UTF-8
bytes, with a control run showing that locale decoding garbles the same bytes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew.service import macos

# A localized launchctl error naming a service label with a non-ASCII suffix.
_NON_ASCII = "Échec du chargement : service « dev.kirocrew.gateway-café » introuvable"

# The child the harness spawns in place of launchctl: raw UTF-8 bytes plus one
# byte that is not valid UTF-8, written straight to the pipe.
_PAYLOAD = _NON_ASCII.encode("utf-8") + b" \xff"
_CHILD = (
    "import sys; sys.stdout.buffer.write(bytes.fromhex(%r)); sys.stdout.flush()" % _PAYLOAD.hex()
)

# Runs inside an interpreter pinned to the C locale with UTF-8 mode and locale
# coercion off, so its locale encoding is ASCII. ``mode`` "service" calls the real
# ``_launchctl``; "locale" calls subprocess.run with a bare ``text=True``.
_HARNESS = """
import json, locale, subprocess, sys
from kiro_crew.service import macos

child, mode = sys.argv[1], sys.argv[2]
real_run = subprocess.run

def fake_run(argv, **kwargs):
    assert argv[0] == "launchctl", argv
    return real_run([sys.executable, "-c", child], **kwargs)

out = {"encoding": locale.getpreferredencoding(False)}
try:
    if mode == "service":
        macos.subprocess.run = fake_run
        out["stdout"] = macos._launchctl("print", "gui/501").stdout
    else:
        out["stdout"] = real_run(
            [sys.executable, "-c", child], capture_output=True, text=True
        ).stdout
except UnicodeDecodeError as exc:
    out["error"] = type(exc).__name__
print(json.dumps(out))
"""


def _run_under_ascii_locale(mode: str) -> dict:
    src_root = str(Path(kiro_crew.__file__).resolve().parent.parent)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("LC_") and k not in {"LANG", "LANGUAGE", "PYTHONIOENCODING"}
    }
    env.update(
        LC_ALL="C",
        LANG="C",
        PYTHONUTF8="0",
        PYTHONCOERCECLOCALE="0",
        PYTHONPATH=os.pathsep.join(filter(None, [src_root, env.get("PYTHONPATH")])),
    )
    res = subprocess.run(
        [sys.executable, "-c", _HARNESS, _CHILD, mode],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_launchctl_pins_utf8_decode(monkeypatch):
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        assert argv[0] == "launchctl"
        seen.append(kwargs)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=_NON_ASCII, stderr="")

    monkeypatch.setattr(macos.subprocess, "run", fake_run)
    assert macos._launchctl("print", "gui/501").stdout == _NON_ASCII
    assert len(seen) == 1
    decode = {k: seen[0].get(k) for k in ("text", "encoding", "errors")}
    assert decode == {"text": True, "encoding": "utf-8", "errors": "replace"}


def test_launchctl_decodes_utf8_under_an_ascii_locale():
    control = _run_under_ascii_locale("locale")
    if "utf" in control["encoding"].lower().replace("-", ""):
        pytest.skip(f"host cannot select a non-UTF-8 locale (got {control['encoding']})")
    # The control proves the locale is really non-UTF-8: bare text=True garbles
    # or rejects the same bytes.
    assert control.get("stdout") != _NON_ASCII + " \ufffd"

    result = _run_under_ascii_locale("service")
    assert "error" not in result, result
    assert result["stdout"] == _NON_ASCII + " \ufffd"
