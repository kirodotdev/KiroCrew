"""``capture-site.mjs`` pins the vetted host to an address Chromium can parse.

The backend passes ``--resolve=host:ip1,ip2`` and the script turns the first
address into ``--host-resolver-rules=MAP host ip``. Chromium reads an IPv6
replacement only in brackets, so a bare one leaves the pin unparsed and an
IPv6-first site captures nothing. ``hostResolverRule`` is the pure builder; the
script imports cleanly as a module (its entry point runs only when it is the
program), so the test calls the shipped function directly under ``node``.

It lives in ``test/`` rather than the app's own ``tests/``: running ``node`` is a
subprocess spawn, and ``test_spawn_audit`` holds every spawn under
``src/kiro_crew`` to the sandbox chokepoint or its allowlist.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

# test/<this file> -> parents[1] is the repo root.
CAPTURE_SITE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "design_critique"
    / "skills"
    / "design-critique"
    / "scripts"
    / "capture-site.mjs"
)


def _rules(*resolves: str | None) -> list[str | None]:
    """``hostResolverRule`` for each input, as the shipped module computes it."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - CI installs node for the website build
        pytest.skip("node is needed to import capture-site.mjs")
    program = (
        f"const m = await import({json.dumps(CAPTURE_SITE.as_uri())});\n"
        f"const inputs = {json.dumps(list(resolves))};\n"
        "console.log(JSON.stringify(inputs.map((r) => m.hostResolverRule(r))));\n"
    )
    done = subprocess.run(
        [node, "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return list(json.loads(done.stdout.strip().splitlines()[-1]))


def test_an_ipv4_address_is_written_as_it_is() -> None:
    assert _rules("example.com:93.184.215.14") == [
        "--host-resolver-rules=MAP example.com 93.184.215.14"
    ]


def test_an_ipv6_address_is_bracketed() -> None:
    assert _rules("example.com:2606:2800:21f:cb07:6820:80da:af6b:8b2c") == [
        "--host-resolver-rules=MAP example.com [2606:2800:21f:cb07:6820:80da:af6b:8b2c]"
    ]


def test_an_already_bracketed_address_keeps_one_pair() -> None:
    assert _rules("example.com:[2001:db8::1]") == [
        "--host-resolver-rules=MAP example.com [2001:db8::1]"
    ]


def test_only_the_first_vetted_address_is_pinned() -> None:
    assert _rules(
        "example.com:2001:db8::1,93.184.215.14", "example.com:93.184.215.14,2001:db8::1"
    ) == [
        "--host-resolver-rules=MAP example.com [2001:db8::1]",
        "--host-resolver-rules=MAP example.com 93.184.215.14",
    ]


def test_nothing_to_pin_gives_no_flag() -> None:
    assert _rules(None, "", "example.com", ":1.2.3.4", "example.com:") == [
        None,
        None,
        None,
        None,
        None,
    ]
