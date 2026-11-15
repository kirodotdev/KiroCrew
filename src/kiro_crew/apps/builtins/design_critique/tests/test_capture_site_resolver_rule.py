"""``capture-site.mjs`` pins the vetted host to an address Chromium can parse.

The backend passes ``--resolve=host:ip1,ip2`` and the script turns the first
address into ``--host-resolver-rules=MAP host ip``. Chromium reads an IPv6
replacement only in brackets, so a bare one leaves the pin unparsed and an
IPv6-first site captures nothing. ``hostResolverRule`` is the pure builder; the
script imports cleanly as a module (its entry point runs only when it is the
program), so the test calls the shipped function directly under ``node``.

Running ``node`` is a subprocess spawn, and ``test_spawn_audit`` holds every spawn
under ``src/kiro_crew`` to the sandbox chokepoint or its allowlist, so ``_rules`` is a
row of ``test/spawn_audit_app_spawns.py``. It runs once for the module, over every
input the tests below use, on one worker: one cold Node start per run, not one per
test on as many workers (testing-conventions § Determinism 6).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from kiro_crew.testing.wait import default_timeout

pytestmark = pytest.mark.xdist_group(name="node_probe_capture_site_resolver_rule")

# tests/<this file> -> parents[1] is the app's own directory.
CAPTURE_SITE = (
    Path(__file__).resolve().parents[1]
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
        timeout=default_timeout(),
    )
    assert done.returncode == 0, done.stderr
    return list(json.loads(done.stdout.strip().splitlines()[-1]))


#: Every input a test below asks about, computed in one node child.
_INPUTS: tuple[str | None, ...] = (
    "example.com:93.184.215.14",
    "example.com:2606:2800:21f:cb07:6820:80da:af6b:8b2c",
    "example.com:[2001:db8::1]",
    "example.com:2001:db8::1,93.184.215.14",
    "example.com:93.184.215.14,2001:db8::1",
    None,
    "",
    "example.com",
    ":1.2.3.4",
    "example.com:",
)


@pytest.fixture(scope="module")
def rule() -> Callable[..., list[str | None]]:
    """``hostResolverRule`` for each input, from the module's one node child."""
    table = dict(zip(_INPUTS, _rules(*_INPUTS)))
    return lambda *resolves: [table[r] for r in resolves]


def test_an_ipv4_address_is_written_as_it_is(rule) -> None:
    assert rule("example.com:93.184.215.14") == [
        "--host-resolver-rules=MAP example.com 93.184.215.14"
    ]


def test_an_ipv6_address_is_bracketed(rule) -> None:
    assert rule("example.com:2606:2800:21f:cb07:6820:80da:af6b:8b2c") == [
        "--host-resolver-rules=MAP example.com [2606:2800:21f:cb07:6820:80da:af6b:8b2c]"
    ]


def test_an_already_bracketed_address_keeps_one_pair(rule) -> None:
    assert rule("example.com:[2001:db8::1]") == [
        "--host-resolver-rules=MAP example.com [2001:db8::1]"
    ]


def test_only_the_first_vetted_address_is_pinned(rule) -> None:
    assert rule(
        "example.com:2001:db8::1,93.184.215.14", "example.com:93.184.215.14,2001:db8::1"
    ) == [
        "--host-resolver-rules=MAP example.com [2001:db8::1]",
        "--host-resolver-rules=MAP example.com 93.184.215.14",
    ]


def test_nothing_to_pin_gives_no_flag(rule) -> None:
    assert rule(None, "", "example.com", ":1.2.3.4", "example.com:") == [
        None,
        None,
        None,
        None,
        None,
    ]
