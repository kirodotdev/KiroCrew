"""Gateway boot smoke on Ubuntu, macOS and Windows.

This is the cross-OS end-to-end floor: a real ``kirocrew gateway`` subprocess, a
real HTTP round trip, a real ACP session and a real prompt turn, on every
platform Kiro Crew ships to. Everything is offline and credential-less -- the
agent binary is the packaged fake ACP backend, wired through
``KIROCREW_KIRO_BIN`` exactly as ``test/test_e2e_smoke.py`` does -- so no model,
no token and no network is involved.

WHY THIS FILE EXISTS. Before it, nothing on ``windows-latest`` or ``macos-15``
started a gateway at all: ``test_e2e_smoke.py`` is gated on ``KIROCREW_E2E``,
which only the Linux ``e2e`` job sets. That is one of the two holes
a Windows startup break shipped through. The change added
a settings-file probe to ``wrap_argv``'s Windows delegation branch, so on a fresh
Windows host -- where that file does not exist -- the Kiro ACP spawn stopped
delegating, hit the no-backend fail-closed path, and the gateway never became
usable. Its unit coverage lived in ``test_sandbox_argv.py``, which Windows does
not collect, and the PR itself changed that test's mock to hardcode the one
answer a fresh host cannot give. Only a real boot on the real platform closes
that. ``test_seeded_sandbox_mode_boots_and_runs_a_turn`` is the direct pin.

SHAPE. Every test boots its OWN gateway on its own scratch ``KIROCREW_HOME``
rather than sharing a module-scoped one. That costs a boot per test and buys
freedom from order dependence (flake class 4), which matters more here than
elsewhere because these tests differ in the CONFIG the gateway booted with.

Readiness is tuned per OS through ``KIROCREW_HARNESS_READY_TIMEOUT`` in the job
env, not in this file, so a slow runner is retunable without a code change.

Run it with ``-n0``: it spawns real processes, and under xdist a block would take
the worker down with it, which on Windows aborts the whole run (see
``docs/system-specs/common/testing-conventions.md``, flake class 6).

Gating, and why there are two markers:

* ``KIROCREW_E2E`` lifts the module skip. A bare ``pytest`` run does not pay
  seven gateway boots.
* ``KIROCREW_E2E_REQUIRE`` turns an unmet PRECONDITION from a graceful
  ``pytest.skip`` into a ``pytest.fail``. A skip counts as a pass, so without it
  a required job could go green having booted zero gateways. ONE switch shared
  with the Playwright gate (``test_playwright_e2e.py``) and the pod scenarios,
  rather than a per-module spelling of the same thing: each job runs one module
  set and sets it for its own run. See ``docs/ci/e2e-gate.md``.
"""

from __future__ import annotations

import contextlib
import http.client
import http.cookiejar
import json
import os
import socket
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn

import pytest

from kiro_crew import platform_compat

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="Cross-OS gateway boot matrix. Set KIROCREW_E2E=1 to run.",
)

# The sandbox tier each seed fixture puts in the config the gateway BOOTS with.
# ``agent.sandbox`` is read at boot (the gateway threads ``cfg.agent.sandbox``
# into the ACP client), so the tier cannot be changed after READY -- which is why
# the matrix is expressed as fixtures rather than as a post-boot config write.
# ``minimal`` states ``"off"``; ``rich`` omits the key, so it resolves to the
# shipped default, which is the tier a FRESH INSTALL runs and the one a settings-probe
# broke. ``_seeded_sandbox_mode`` asserts the fixture still says so, so an edit
# to either fixture fails here instead of quietly collapsing this matrix to one
# tier tested twice.
_SEED_SANDBOX_MODES: dict[str, str] = {"minimal": "off", "rich": "auto"}

# Kiro Crew's own default when ``agent.sandbox`` is absent
# (``sandbox._SANDBOX_MODE_FALLBACK``). Restated rather than imported so a change
# to that constant surfaces here as a named mismatch.
_DEFAULT_SANDBOX_MODE = "auto"

# The provider the ``KIROCREW_KIRO_BIN`` seam requires. The fake backend is only
# reached when the resolved provider is ``acp``; asserting it up front turns a
# provider change into a named failure instead of a no-reply timeout.
_EXPECTED_PROVIDER = "acp"

_REPLY_MARKER = "pong from the fake ACP backend"

# Per-turn ceiling for the reply poll. Generous rather than tight: it only
# matters when the turn is broken, and it must stay well under the job's
# ``--timeout`` so a stuck turn fails by name at the polling line instead of
# killing the run (testing-conventions.md, flake class 6).
_REPLY_TIMEOUT = 90.0

# How long teardown may take to release the port after the context manager
# exits. A refused connect is the observable proof that no process in the
# gateway's tree still holds the listener.
_PORT_RELEASE_TIMEOUT = 30.0


def _unresolved(msg: str) -> NoReturn:
    """Skip locally, FAIL on the required job.

    An environment-resolution miss on the matrix job is a hard failure: pytest
    counts a skip as a pass, so the job would otherwise report green having
    started no gateway at all -- the exact dead-suite blindness this module
    exists to remove. Ad-hoc local runs keep the graceful skip.
    """
    if os.environ.get("KIROCREW_E2E_REQUIRE"):
        pytest.fail(msg)
    pytest.skip(msg)


class _Client:
    """Cookie-jar HTTP client for one gateway.

    The harness hands out a one-time LINK token. Re-presenting it as a
    ``?token=`` query param works on ordinary routes but 403s on
    ``mixed_internal`` ones (``/api/chat/slots`` among them) once the link
    nonce is revoked after first use. So mint the durable ``mc_token_<port>``
    session cookie once through a normal-path GET, then send only the cookie.
    Mirrors ``test_e2e_smoke.py``'s opener.
    """

    def __init__(self, port: int, token: str) -> None:
        self._port = port
        #: Bound by ``_booted`` to the gateway handle's ``diagnostics``.
        self.diagnostics: Callable[[], str] = lambda: ""
        jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        prime = urllib.request.Request(f"http://localhost:{port}/api/status?token={token}")
        with self._opener.open(prime, timeout=30):
            pass

    def get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(f"http://localhost:{self._port}{path}")
        return self._open(req, timeout=30)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://localhost:{self._port}{path}", data=data, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        return self._open(req, timeout=60)

    def _open(self, req: urllib.request.Request, *, timeout: float) -> dict[str, Any]:
        """One request; an HTTP error carries the body the gateway answered with.

        A bare ``HTTPError: 409 Conflict`` names no cause, while the gateway's
        JSON body does (``code`` and ``error``), and the diagnostics say whether
        the gateway itself is still up. Both ride on the raised assertion.
        """
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:2000]
            raise AssertionError(
                f"{req.get_method()} {req.selector} -> HTTP {exc.code}: {body}\n"
                f"{self.diagnostics()}"
            ) from exc


@contextlib.contextmanager
def _booted(fixture: str) -> Iterator[tuple[Any, _Client]]:
    """Boot one gateway whose agent binary is the packaged fake ACP backend.

    ``KIROCREW_KIRO_BIN`` is set before the spawn (the child inherits
    ``os.environ``) and restored in ``finally``, so a fake backend can never leak
    into a later test in this interpreter. On Windows the value is a ``.cmd``
    shim (``fake_acp_backend_launcher``): ``CreateProcess`` cannot run a ``.py``
    path, which is exactly how the first Windows run of this module produced a
    healthy gateway that never answered a prompt.

    The first request is made INSIDE this context so a reset or refused
    connection reports the gateway's exit status and stderr tail instead of a
    bare socket error: on a platform nobody can reproduce locally, that tail is
    the whole diagnosis.
    """
    try:
        from kiro_crew.testing import fake_acp_backend
        from kiro_crew.testing.harness import fake_acp_backend_launcher, spawn_feature_gateway
    except ImportError as exc:  # pragma: no cover - packaging regression
        _unresolved(f"kiro_crew.testing is not importable: {exc}")

    backend_path = Path(str(fake_acp_backend.__file__))
    if not backend_path.is_file():
        _unresolved(f"packaged fake ACP backend is missing at {backend_path}")

    previous = os.environ.get("KIROCREW_KIRO_BIN")
    with tempfile.TemporaryDirectory(prefix="kc-fake-backend-") as launcher_dir:
        os.environ["KIROCREW_KIRO_BIN"] = str(fake_acp_backend_launcher(Path(launcher_dir)))
        try:
            with spawn_feature_gateway(fixture=fixture, approval="reads") as handle:
                try:
                    client = _Client(handle.port, handle.token)
                    client.diagnostics = handle.diagnostics
                except OSError as exc:
                    raise AssertionError(
                        f"first request to the booted gateway failed: {exc!r}\n"
                        f"{handle.diagnostics()}"
                    ) from exc
                yield handle, client
        finally:
            if previous is None:
                os.environ.pop("KIROCREW_KIRO_BIN", None)
            else:
                os.environ["KIROCREW_KIRO_BIN"] = previous


def _config(home: Path) -> dict[str, Any]:
    """The config the gateway booted with, as it exists in the seeded home."""
    path = home / "config.json"
    if not path.is_file():
        return {}
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _seeded_sandbox_mode(home: Path) -> str:
    """``agent.sandbox`` as the gateway resolved it, absent meaning the default."""
    agent = _config(home).get("agent") or {}
    return str(agent.get("sandbox", _DEFAULT_SANDBOX_MODE))


def _health(port: int) -> tuple[int, bytes]:
    """``GET /api/health`` -- the unauthenticated liveness probe, no token.

    ``http.client`` against a fixed loopback host rather than ``urllib``: the
    scheme and host are constants here, only the port varies, so there is no
    ``file://`` surface for a dynamic value to reach.
    """
    conn = http.client.HTTPConnection("localhost", port, timeout=30)
    try:
        conn.request("GET", "/api/health")
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _await_assistant_reply(client: _Client, slot: str, timeout: float = _REPLY_TIMEOUT) -> dict:
    """Poll the slot's history until a non-empty assistant reply is persisted.

    Polls observable state rather than sleeping toward a duration
    (testing-conventions.md, flake class 2), and raises by name at THIS line so a
    broken turn is a failed test rather than a lost run.
    """
    deadline = time.monotonic() + timeout
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        detail = client.get(f"/api/chat/slots/{slot}")
        for msg in detail.get("messages", []):
            if msg.get("role") == "assistant" and str(msg.get("content", "")).strip():
                return dict(msg)
        time.sleep(0.5)
    seen = [(m.get("role"), str(m.get("content", ""))[:120]) for m in detail.get("messages", [])]
    raise AssertionError(
        f"no non-empty assistant reply within {timeout:.0f}s; slot messages: {seen!r}\n"
        f"{client.diagnostics()}"
    )


def _run_one_turn(client: _Client, message: str) -> dict:
    """Create a slot, send one prompt, return the assistant reply."""
    slot = client.post("/api/chat/slots", {})["key"]
    assert slot, "gateway returned an empty chat-slot key"
    # ws=1: the POST returns immediately and the reply is both delivered over the
    # WebSocket and persisted to the slot history, which is what we poll. No
    # ``agent`` is named: the slot dispatches on the binding it was created with,
    # and a name the seeded HOME does not define is a 409 "slot agent mismatch"
    # (an unknown name resolves to the default and would otherwise pass as it).
    client.post("/api/chat?ws=1", {"message": message, "slot": slot})
    return _await_assistant_reply(client, slot)


def _port_is_refused(port: int, timeout: float = _PORT_RELEASE_TIMEOUT) -> bool:
    """True once a TCP connect to ``port`` is refused (no listener left)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.2)
    return False


# --- The matrix ---------------------------------------------------------------


def test_gateway_boots_and_answers_health() -> None:
    """READY, then ``GET /api/health`` returns 200.

    The harness returning a handle IS the READY assertion: it raises
    ``GatewaySpawnError`` on timeout or on an exit before the
    ``KIROCREW_READY:`` line. Health is probed unauthenticated, the same probe
    ``docker-smoke.yml`` uses, so a token-plumbing regression cannot be mistaken
    for a dead gateway.
    """
    with _booted("minimal") as (handle, _client):
        status, body = _health(handle.port)
        assert status == 200, f"/api/health returned {status}"
        assert body, "/api/health returned an empty body"


def test_resolved_provider_is_acp() -> None:
    """The gateway resolves the ``acp`` provider, so the fake-backend seam fires.

    ``KIROCREW_KIRO_BIN`` is only consulted on the acp path. Asserting the
    provider here turns a provider default change into a named failure rather
    than a reply that never arrives. That the acp path actually RAN is proved by
    the turn tests below, not by this one.
    """
    with _booted("minimal") as (handle, _client):
        provider = str(
            (_config(handle.home).get("agent") or {}).get("provider", _EXPECTED_PROVIDER)
        )
        assert provider == _EXPECTED_PROVIDER, (
            "the KIROCREW_KIRO_BIN seam needs the acp provider; the seeded home "
            f"resolved provider={provider!r}"
        )


def test_prompt_returns_the_fake_backend_reply() -> None:
    """One session create plus one prompt returns the fake backend's reply.

    This is the load-bearing assertion of the whole module. ``/api/health`` can
    answer while the ACP spawn is being refused, so only a completed turn proves
    the gateway to subprocess to ACP path works: spawn, initialize,
    ``session/new``, ``session/prompt``, streamed ``agent_message_chunk``, reply
    assembly and persistence.
    """
    with _booted("minimal") as (_handle, client):
        reply = _run_one_turn(client, "ping")
        # Match the serialized message so the assertion does not couple to the
        # content field name after display preparation and redaction.
        assert _REPLY_MARKER in json.dumps(reply)


def test_tool_marker_prompt_completes_the_turn() -> None:
    """A ``[[TOOL]]`` prompt still completes its turn.

    The fake emits a ``tool_call`` plus a ``tool_call_update`` before replying.
    The reply still arriving proves the gateway processed those updates without
    stalling the turn. The ``[[PERMISSION]]`` path needs a UI to resolve the
    modal, so Playwright owns it, not this headless module.
    """
    with _booted("minimal") as (_handle, client):
        reply = _run_one_turn(client, "please [[TOOL]] run the demo")
        assert _REPLY_MARKER in json.dumps(reply)


@pytest.mark.parametrize("fixture", sorted(_SEED_SANDBOX_MODES))
def test_seeded_sandbox_mode_boots_and_runs_a_turn(fixture: str) -> None:
    """A gateway boots and serves under each seeded ``agent.sandbox`` tier.

    THE DELEGATION PIN. On Windows the ``auto`` tier MUST complete a turn: Kiro Crew
    has no native Windows sandbox backend, so a positively classified Kiro ACP
    spawn delegates to Kiro CLI's own internal sandbox
    (``sandbox.wrap_argv``'s ``win32 and is_kiro_cli`` branch). Break that branch
    and every other spawn takes the no-backend fail-closed path, which is
    precisely the settings-probe break and what
    [56f67aa43](https://github.com/kirodotdev/KiroCrew/commit/56f67aa43f00f9484c346a8d1669b39102a63c78)
    reverted.

    Off Windows the expectation is derived, not assumed: a host with a real
    backend (macOS seatbelt, Linux user namespaces) must complete the turn under
    ``auto`` too, while a host that genuinely has none must FAIL CLOSED -- the
    turn must not silently run unsandboxed, and the gateway must stay healthy
    rather than die with it. ``ubuntu-latest`` is that second host: its
    unprivileged user namespaces are AppArmor-restricted, which is why
    ``ci.yml``'s ``backend-test-sandbox`` job has to clear a sysctl to get one.
    Both branches assert a specific named outcome; which one applies comes from
    the product's own backend probe, and on Windows it is unconditionally the
    first, so a regression cannot hide in the second.
    """
    from kiro_crew import sandbox

    expected_mode = _SEED_SANDBOX_MODES[fixture]

    with _booted(fixture) as (handle, client):
        seeded = _seeded_sandbox_mode(handle.home)
        assert seeded == expected_mode, (
            f"fixture {fixture!r} was expected to boot the gateway with "
            f"agent.sandbox={expected_mode!r} but the seeded home says {seeded!r}; "
            "update _SEED_SANDBOX_MODES so this matrix still covers both tiers"
        )

        status, _body = _health(handle.port)
        assert status == 200, f"/api/health returned {status} under sandbox={seeded}"

        # ``off`` defers isolation to the agent's own sandbox and never needs a
        # Kiro Crew backend, so the turn must complete everywhere.
        turn_expected = seeded == "off" or platform_compat.IS_WINDOWS
        if not turn_expected:
            turn_expected = sandbox.detect_backend(config_mode=seeded) != "none"

        if turn_expected:
            reply = _run_one_turn(client, "ping")
            assert _REPLY_MARKER in json.dumps(reply)
            return

        # No backend on this host: the spawn must be refused, by name, and the
        # gateway must survive the refusal.
        slot = client.post("/api/chat/slots", {})["key"]
        client.post("/api/chat?ws=1", {"message": "ping", "slot": slot})
        deadline = time.monotonic() + _REPLY_TIMEOUT
        transcript = ""
        while time.monotonic() < deadline:
            transcript = json.dumps(client.get(f"/api/chat/slots/{slot}"))
            if "sandbox" in transcript.lower():
                break
            time.sleep(0.5)
        assert _REPLY_MARKER not in transcript, (
            "a host with no sandbox backend ran the agent spawn anyway under "
            f"agent.sandbox={seeded!r}"
        )
        assert "sandbox" in transcript.lower(), (
            "expected a named sandbox-unavailable refusal in the slot transcript "
            f"under agent.sandbox={seeded!r}; got: {transcript[:2000]}"
        )
        status, _body = _health(handle.port)
        assert status == 200, "the gateway did not survive a fail-closed spawn"


def test_shutdown_leaves_no_gateway_child_alive() -> None:
    """Clean shutdown reaps the whole tree and releases the port.

    **The port check does NOT prove the children are gone, so it is not what proves
    it here.** A listening socket is not inherited by a descendant (PEP 446
    makes new fds non-inheritable, and the gateway passes none), so the listener
    disappears when the process that BOUND it dies -- the root. A refused connect is
    therefore evidence about the root and nothing else, and a leaked grandchild
    satisfies it: measured on a native Windows host, running the harness's own
    teardown against a gateway with an orphaned grandchild gives
    ``connect_ok=False`` and a dead direct child while the grandchild is still
    running. On a REQUIRED windows-latest check, that is green over precisely the
    residue class the pod canary reds on.

    So the gateway's descendants are SNAPSHOTTED while it is still serving, each
    with its creation identity, and re-probed after teardown. Before the kill,
    because a kill orphans survivors out of the parent map and a post-teardown walk
    cannot find them; attributed by ``platform_compat.created_after`` so a recycled
    pid's stray is not blamed on this gateway; and existence-first on the re-probe,
    because a creation token stays readable while any handle to the exited process
    is open and would report a corpse as residue.

    Liveness is probed through ``platform_compat.pid_exists``, never
    ``os.kill(pid, 0)``, which TERMINATES the target on Windows.
    """
    with _booted("minimal") as (handle, _client):
        pid = handle.proc.pid
        port = handle.port
        status, _body = _health(port)
        assert status == 200, "gateway was not serving before the shutdown check"
        root_token = platform_compat.process_start_time(pid) or ""
        assert root_token, "the gateway's creation identity must be readable to attribute its tree"
        descendants = {
            child: token
            for child in platform_compat.process_descendants(pid)
            if (token := platform_compat.process_start_time(child))
            and platform_compat.created_after(token, root_token)
        }

    assert handle.proc.poll() is not None, "the gateway process was not reaped"
    assert not platform_compat.pid_exists(pid), f"gateway pid {pid} is still alive after teardown"
    # Read at once, no grace: the harness certifies that the whole tree has
    # exited (group members and the descendants that left the group alike)
    # before it returns, so anything still alive here is real residue.
    leaked = sorted(
        child
        for child, token in descendants.items()
        if platform_compat.pid_exists(child) and platform_compat.process_start_time(child) == token
    )
    assert not leaked, (
        f"teardown left {len(leaked)} of the gateway's {len(descendants)} descendant(s) "
        f"running: {leaked}. The port check cannot see this, because a descendant never "
        "inherits the listener."
    )
    assert _port_is_refused(port), (
        f"port {port} still has a listener after teardown; the process that bound it " "did not die"
    )
    assert not handle.home.exists(), "the throwaway KIROCREW_HOME was not removed"
