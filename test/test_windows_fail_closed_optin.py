"""The Windows fail-closed sandbox opt-in contract, on a real win32 host.

WHY THIS FILE EXISTS, AND WHY IT IS NOT test_sandbox_argv.py

A Windows startup break added one conjunct
to ``wrap_argv``'s Windows delegation predicate::

    ) or (sys.platform == "win32" and is_kiro_cli is True and kiro_internal_sandbox_enabled())

``kiro_internal_sandbox_enabled()`` reads ``~/.kiro/settings/amazon-internal.json``
and returns False when that file is absent. A fresh Windows install does not have
it. So delegation stopped, control fell through to backend detection, Windows has
no Kiro Crew OS sandbox backend, and the no-backend policy fail-closed: the ACP
backend never started and the gateway never became usable.

``test/test_sandbox_argv.py`` already pinned that exact branch and did not catch
it, for two compounding reasons. It is line 2 of ``test/windows-collect-ignore.txt``,
so it is not collected on ``windows-latest`` at all. And where it does run, in the
Linux shards, it monkeypatches ``kiro_crew.sandbox.sys.platform`` to ``"win32"``
and patches ``kiro_internal_sandbox_enabled`` to ``return_value=True`` -- which
hardcodes the one answer a fresh Windows host cannot give. Real filesystem state
was never involved on any OS.

This file is therefore built on the opposite principle, and every rule below is
load-bearing rather than stylistic:

* It runs ONLY on win32 and is skipped elsewhere with a reason. It is deliberately
  absent from ``windows-collect-ignore.txt`` so the Windows shards collect it, and
  a dedicated ``windows-latest`` canary job in ``ci.yml`` runs it by node id and
  greps the pass count, because ``pytest -q`` does not name passing tests and a
  skip exits 0 -- a canary that quietly stopped running would leave the job green
  while the contract it proves went unverified.
* It patches NEITHER ``sys.platform`` NOR the settings probe. The platform is real
  and the probe reads the real absent file. ``_settings_probe_answers_false``
  asserts that precondition explicitly and SKIPS a host that happens to carry the
  file, so a developer machine cannot produce a misleading result either way.
* The policy is flipped by WRITING ``config.json`` to disk, not by patching
  ``_allow_unsandboxed_exec``. The config cache is fingerprint-keyed on a stat, so
  the write is what invalidates it -- the same path an operator takes. Note which
  write means what on THIS platform: an undeclared
  ``agent.sandbox_allow_unsandboxed_exec`` already resolves to ``True`` on win32
  (``unsandboxed_exec_platform_default``), so the operator gesture that refuses is
  a declared ``false``, and a config that merely omits the key is a host that runs.

Two halves, matching the two things a fresh Windows host must do:

``TestFreshWindowsHostPrecondition`` / ``TestWrapArgvContract`` -- in-process
``wrap_argv`` against real filesystem state. This is where the fail-closed pair
lives, because driving an unclassified spawn all the way through a booted gateway
would prove the same predicate through several hundred lines of unrelated
machinery and fail for many more reasons than the one under test.

``TestFreshWindowsGatewayDelegates`` -- a real gateway process, one session and
one prompt turn against the packaged fake ACP backend. ``/api/health`` alone is
NOT sufficient: it answers while the ACP spawn is being refused, which is exactly
how that break stayed invisible. The prompt turn is the load-bearing assertion.

It does NOT use ``kiro_crew.testing.harness.spawn_feature_gateway``, which cannot
run on Windows today: ``_wait_for_ready_line`` registers a PIPE with
``selectors.DefaultSelector()`` (the Windows selector accepts sockets only) and
teardown goes through ``terminate_pgid``, which calls ``os.killpg`` / ``os.getpgid``
(absent on Windows). The minimal reader thread below is a deliberate stand-in and
should fold into the harness once it is cross-platform. It still imports
``parse_ready_line`` from the harness rather than re-parsing the READY line, so
the wire contract has exactly one owner.
"""

from __future__ import annotations

import contextlib
import http.cookiejar
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.testing.harness import _terminate_process_group

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "the Windows fail-closed sandbox opt-in contract is a win32 host property: "
        "it depends on there being no Kiro Crew OS sandbox backend and on the real "
        "absence of ~/.kiro/settings/amazon-internal.json, neither of which a "
        "POSIX host can represent without the platform mocks that let the break through"
    ),
)

# The gateway boot, the readiness gate and the ACP handshake are all measurably
# slow on windows-latest. Bounded by a deadline rather than a sleep (flake class
# 2), and generous because a cold runner has been observed well past 60s.
READY_TIMEOUT_SECS = 240.0
TURN_TIMEOUT_SECS = 180.0
HEALTH_TIMEOUT_SECS = 180.0


def _kiro_internal_settings_path() -> Path:
    """The real path the settings probe reads, derived from the constant.

    Read from ``sandbox`` rather than restated so a move of the file cannot leave
    this test asserting the absence of a path nothing consults any more.
    """
    from kiro_crew import sandbox

    return Path(os.path.expanduser(sandbox._KIRO_INTERNAL_SETTINGS_PATH))


@pytest.fixture
def fresh_windows_home(monkeypatch, tmp_path):
    """A scratch data home with NO config.json, on a host with no settings file.

    Both halves of "fresh" are asserted rather than assumed. ``KIROCREW_HOME`` is
    already pinned per test by the rootdir conftest isolation floor; this fixture
    re-pins it to its own subdirectory so the opt-in write below cannot be
    confused with state another test left, and resets the sandbox backend cache so
    a probe result cached by an earlier test in the same xdist worker cannot
    decide this one.
    """
    from kiro_crew import sandbox

    settings = _kiro_internal_settings_path()
    if settings.exists():
        pytest.skip(
            f"host is not a fresh Windows install: {settings} exists, so "
            "kiro_internal_sandbox_enabled() may answer True and the "
            "no-settings-file precondition this suite pins cannot be observed"
        )

    home = tmp_path / "crew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    paths = sys.modules.get("kiro_crew.config.paths")
    if paths is not None:
        monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _write_config(home: Path, payload: dict) -> None:
    """Write ``config.json`` into the scratch home the way an operator would.

    The loader's hot-path cache is keyed on a stat fingerprint of this file, so
    writing it is what makes the new value visible. Patching
    ``_allow_unsandboxed_exec`` instead would assert that the gate reads *a*
    function rather than that it reads the operator's file, which is the class of
    shortcut this whole suite exists to avoid.
    """
    from kiro_crew.config.paths import config_dir

    target = config_dir() / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


class TestFreshWindowsHostPrecondition:
    """The two host facts every assertion below rests on, asserted not assumed."""

    def test_settings_probe_answers_false_on_this_host(self, fresh_windows_home):
        """``kiro_internal_sandbox_enabled()`` is False here, from real state.

        This is the value the break conjoined into the delegation predicate. Pinning
        it separately is what makes the delegation test below meaningful: without
        it, a host that happened to carry the settings file would pass that test
        for the wrong reason and the regression would be invisible again.
        """
        from kiro_crew import sandbox

        assert not _kiro_internal_settings_path().exists()
        assert sandbox.kiro_internal_sandbox_enabled() is False

    def test_windows_offers_no_sandbox_backend(self, fresh_windows_home):
        """Backend detection reports ``none``, so the fail-closed policy applies.

        The other half of the precondition. If Windows ever grows a backend this
        test fails first and says so, rather than every assertion below silently
        changing meaning.
        """
        from kiro_crew import sandbox

        assert sandbox.detect_backend(config_mode="auto") == "none"


class TestWrapArgvContract:
    """The fail-closed pair, and the delegation branch the settings probe broke.

    In-process against a real win32 host and real files: no ``sys.platform``
    patch, no settings-probe patch, no ``_allow_unsandboxed_exec`` patch.
    """

    # A real, always-present Windows binary. Concrete rather than invented so the
    # refusal cannot be attributed to a missing file, and NOT named kiro-cli so
    # basename inference has nothing to latch onto.
    UNCLASSIFIED_ARGV = [
        os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "system32",
            "whoami.exe",
        )
    ]

    def test_unclassified_spawn_fails_closed(self, fresh_windows_home):
        """With NO config to read at all, the spawn is refused and names the remedy.

        The first half of the pair. What it pins is the unreadable-config rule: the
        effective policy is resolved INTO ``agent.sandbox_allow_unsandboxed_exec``
        at config load, so a config that cannot be read yields ``False`` rather
        than a platform default -- a broken or missing config must never be a way
        to obtain a LOOSER sandbox than the operator configured. It is NOT a
        deny-by-default-until-opt-in posture; on win32 an undeclared key resolves
        to ``True``, which the other half of the pair pins.

        Asserting the message names the setting matters as much as the raise: a
        refusal whose remedy is undiscoverable is the failure mode
        ``_setup_sandbox_consent`` was added to fix.
        """
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        with pytest.raises(SandboxUnavailableError) as excinfo:
            wrap_argv(list(self.UNCLASSIFIED_ARGV), mode="auto")

        assert excinfo.value.kind == "no_backend"
        assert "sandbox_allow_unsandboxed_exec" in str(excinfo.value)

    def test_a_declared_false_is_what_refuses_the_spawn_on_windows(self, fresh_windows_home):
        """A DECLARED lockdown refuses; leaving the key undeclared does not.

        The second half of the pair, and the direction that is easy to get wrong.
        On win32 an UNDECLARED ``agent.sandbox_allow_unsandboxed_exec`` resolves to
        ``True`` at load time -- see
        :func:`kiro_crew.config.loader.unsandboxed_exec_platform_default`, which is
        ``sys.platform == "win32"`` because there is no native wrapper to apply and
        nothing installable that would produce one, so fail-closing here would
        refuse every script cron, hook, app backend, MCP probe and provider CLI on
        the platform with no operator action able to satisfy the check. A config
        that merely omits the key is therefore NOT a host that refuses, and
        asserting it were would pin the opposite of the shipped policy.

        What an operator can still do is declare ``false``, and that must be
        honoured on this platform exactly as it is everywhere else. Nothing else
        pins that, which is why it is pinned here: the platform default is a
        default, not an override.
        """
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        _write_config(
            fresh_windows_home,
            {"agent": {"sandbox": "auto", "sandbox_allow_unsandboxed_exec": False}},
        )
        with pytest.raises(SandboxUnavailableError) as excinfo:
            wrap_argv(list(self.UNCLASSIFIED_ARGV), mode="auto")
        assert excinfo.value.kind == "no_backend"

        # Undeclared: the platform default carries the policy, so the identical
        # spawn runs -- unwrapped, because on this platform there is nothing to
        # wrap with.
        _write_config(fresh_windows_home, {"agent": {"sandbox": "auto"}})
        wrapped, launcher = wrap_argv(list(self.UNCLASSIFIED_ARGV), mode="auto")

        assert wrapped == list(self.UNCLASSIFIED_ARGV)
        assert launcher is None

    def test_classified_kiro_spawn_delegates_without_the_settings_file(self, fresh_windows_home):
        """THE DELEGATION PIN: delegation does not consult the settings probe.

        No ``config.json``, no ``~/.kiro/settings/amazon-internal.json``, and the
        reviewed ACP caller's ``is_kiro_cli=True``. This must NOT raise: the
        official Kiro backend owns isolation through its own internal sandbox, so
        the spawn proceeds env-scrubbed and SEL-audited.

        With the settings-probe conjunct in the predicate this call raises ``SandboxUnavailableError``
        on this host, which is precisely the fresh-install breakage no test could
        observe while the platform and the probe were both mocked.
        """
        from kiro_crew.sandbox import wrap_argv

        argv = [str(Path(fresh_windows_home) / "kiro-cli.exe"), "acp"]
        wrapped, launcher = wrap_argv(argv, mode="auto", is_kiro_cli=True)

        assert wrapped == argv
        assert launcher is None

    def test_basename_inference_alone_does_not_grant_delegation(self, fresh_windows_home):
        """``is_kiro_cli=None`` still fails closed even for a kiro-cli basename.

        The Windows carve-out is a positive capability grant from the reviewed
        caller, deliberately not an inference from argv[0]. Anything an agent can
        influence must not be able to name itself into the exception, so the
        predicate tests ``is_kiro_cli is True`` rather than ``_spawns_kiro_cli``.
        Pinned here because widening it to the basename would look like a
        harmless simplification and would hand any spawn a path out of the
        sandbox by filename.
        """
        from kiro_crew.sandbox import SandboxUnavailableError, wrap_argv

        with pytest.raises(SandboxUnavailableError):
            wrap_argv([str(Path(fresh_windows_home) / "kiro-cli"), "acp"], mode="auto")


# --- The real-process half ------------------------------------------------


class _GatewayProc:
    """A booted gateway plus the cookie-primed opener its API needs."""

    def __init__(self, proc: subprocess.Popen, port: int, token: str, home: Path) -> None:
        self.proc = proc
        self.port = port
        self.token = token
        self.home = home
        self._opener: urllib.request.OpenerDirector | None = None

    def opener(self) -> urllib.request.OpenerDirector:
        """Mint the durable session cookie once, then reuse it.

        Same reason as ``test_e2e_smoke.py``: the READY line carries a one-time
        LINK token, and re-presenting it as a query parameter 403s on
        ``mixed_internal`` routes once its nonce is revoked. One normal-path GET
        exchanges it for a session cookie whose nonce is fresh.
        """
        if self._opener is None:
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            prime = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/status?token={self.token}"
            )
            with opener.open(prime, timeout=30):
                pass
            self._opener = opener
        return self._opener

    def get(self, path: str, timeout: float = 30.0) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        return self._open(req, timeout=timeout)

    def post(self, path: str, body: dict, timeout: float = 60.0) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        return self._open(req, timeout=timeout)

    def _open(self, req: urllib.request.Request, *, timeout: float) -> dict:
        """One request; an HTTP error carries the body the gateway answered with.

        A bare ``HTTPError: 409 Conflict`` names no cause, while the gateway's JSON
        body does (``code`` and ``error``). This job runs only on Windows CI, where
        there is no debugger to attach, so a refusal that arrives without its body
        turns a one-glance diagnosis into a hunt through every 409 in the handler --
        which is exactly what one cost on the sibling boot-matrix module.
        """
        try:
            with self.opener().open(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = ""
            with contextlib.suppress(Exception):
                body = exc.read().decode("utf-8", "replace")[:2000]
            raise AssertionError(
                f"{req.get_method()} {req.selector} -> HTTP {exc.code}: "
                f"{body or '(empty body)'}"
            ) from exc


def _read_until_ready(
    proc: subprocess.Popen,
    *,
    deadline: float,
    stderr_tail: deque[bytes],
) -> dict:
    """Read stdout on this thread until the READY line arrives or time runs out.

    ``readline()`` on a pipe rather than a selector: the Windows selector cannot
    register a pipe, which is the specific reason ``harness._wait_for_ready_line``
    does not run here. The blocking read is made safe by a WATCHDOG thread that
    kills the process at the deadline, so a gateway that wedges without writing
    and without exiting cannot hang the test -- the read then returns EOF.

    The watchdog is STOPPED before this returns. Its job ends the moment the read
    does, and a watchdog still counting down after READY would kill the healthy
    gateway the caller is about to use, at a deadline the caller has no reason to
    know about -- surfacing as an unrelated connection error later in the test.
    """
    from kiro_crew.testing.harness import READY_PREFIX, parse_ready_line

    stop = threading.Event()

    def _watchdog() -> None:
        while not stop.wait(0.25):
            if proc.poll() is not None:
                return
            if time.monotonic() >= deadline:
                if proc.poll() is None:
                    proc.kill()
                return

    threading.Thread(target=_watchdog, daemon=True).start()

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith(READY_PREFIX):
                return parse_ready_line(line)
    finally:
        # Every exit: READY, EOF, or an exception out of the decode/parse. The
        # kill window has to close with the read, not with the deadline.
        stop.set()

    tail = b"".join(stderr_tail).decode("utf-8", errors="replace")
    raise AssertionError(
        f"gateway never emitted {READY_PREFIX} (exit code {proc.poll()}). "
        f"--- stderr tail ---\n{tail[-6000:]}"
    )


def _drain(stream, sink: deque[bytes]) -> None:
    """Keep a pipe empty so the child never blocks writing to it."""
    for chunk in iter(lambda: stream.read(4096), b""):
        sink.append(chunk)


def _write_fake_backend_launcher(directory: Path) -> Path:
    """The ``.cmd`` shim that runs the packaged fake ACP backend.

    Shared with ``test/e2e/test_gateway_boot_matrix.py`` through
    ``kiro_crew.testing.harness.fake_acp_backend_launcher``, which carries the
    reasoning: no shebang on Windows, ``.cmd`` is runnable, and the name is
    deliberately not ``kiro-cli`` so delegation must come from the reviewed
    ``is_kiro_cli=True`` call site rather than the basename.
    """
    from kiro_crew.testing.harness import fake_acp_backend_launcher

    return fake_acp_backend_launcher(directory)


@pytest.fixture
def fresh_windows_gateway(fresh_windows_home, tmp_path):
    """Boot a real gateway on a fresh Windows home and tear it down.

    Deliberately NOT seeded with an opt-in and deliberately run on a host with no
    settings file: the whole point is that the default chat path works on a fresh
    Windows install with no config edit, which is what
    ``docs/guides/windows-install.md`` promises and what the settings probe falsified.
    """
    launcher = _write_fake_backend_launcher(tmp_path)
    env = {
        **os.environ,
        "KIROCREW_HOME": str(fresh_windows_home),
        "KIRO_HOME": str(fresh_windows_home / "kiro"),
        "KIROCREW_WORKSPACE": str(tmp_path / "workspace"),
        "KIROCREW_KIRO_BIN": str(launcher),
        # Marks this as a test rig; grants no launch privilege. The launcher above
        # is exec'd by the ordinary in-place path like any other runnable file.
        "KIROCREW_FAKE_ACP_TEST_MODE": "1",
        # --json-ready flushes its own line, but earlier prints block-buffer into
        # a pipe and would mask an early failure behind them.
        "PYTHONUNBUFFERED": "1",
        # Embeddings are default-on; never let a test kick the ~610MB download.
        "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
        "PYTHONUTF8": "1",
    }
    cmd = [
        sys.executable,
        "-m",
        "kiro_crew",
        "gateway",
        "--test-mode",
        "--approval",
        "reads",
        # A stray scheduled job firing mid-test is a classic invisible flake.
        "--no-crons",
    ]
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
    )
    stderr_tail: deque[bytes] = deque(maxlen=512)
    stdout_tail: deque[bytes] = deque(maxlen=256)
    assert proc.stderr is not None
    threading.Thread(target=_drain, args=(proc.stderr, stderr_tail), daemon=True).start()
    try:
        ready = _read_until_ready(
            proc,
            deadline=time.monotonic() + READY_TIMEOUT_SECS,
            stderr_tail=stderr_tail,
        )
        # Keep draining stdout for the rest of the run: the loop above stops
        # reading at the sentinel, and per-turn logging would otherwise fill the
        # pipe and stall the gateway's event loop partway through the turn.
        assert proc.stdout is not None
        threading.Thread(target=_drain, args=(proc.stdout, stdout_tail), daemon=True).start()
        yield _GatewayProc(proc, int(ready["port"]), str(ready["token"]), fresh_windows_home)
    finally:
        candidates = [proc.pid, *platform_compat.process_descendants(proc.pid)]
        exited = _terminate_process_group(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        if not exited:
            survivors = [pid for pid in candidates if platform_compat.pid_exists(pid)]
            raise AssertionError(f"gateway teardown left live process pids: {survivors}")


def _await_assistant_reply(gateway: _GatewayProc, slot: str, timeout: float) -> str:
    """Poll the slot's persisted history for a non-empty assistant message.

    Polls the durable history rather than reading the WebSocket: the reply is
    persisted as well as pushed, and asserting on the persisted copy proves the
    turn actually completed instead of merely having been streamed.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            detail = gateway.get(f"/api/chat/slots/{slot}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = exc
            time.sleep(1.0)
            continue
        for message in detail.get("messages", []):
            if message.get("role") == "assistant" and str(message.get("content", "")).strip():
                return json.dumps(message)
        time.sleep(1.0)
    raise AssertionError(
        f"no assistant reply within {timeout:.0f}s "
        f"(last transport error: {last_error!r}) -- on a fresh Windows host this is "
        "what a fail-closed ACP spawn looks like from the outside"
    )


class TestFreshWindowsGatewayDelegates:
    """A fresh Windows install must chat with no config edit."""

    @pytest.mark.timeout(900)
    def test_health_then_one_session_and_one_prompt_succeed(self, fresh_windows_gateway):
        """The end-to-end proof that delegation does not need the settings file.

        Health is checked first only to separate "the gateway never bound" from
        "the ACP spawn was refused". It is explicitly NOT the assertion: on a host
        with the settings-probe conjunct in place health answers 200 while every ACP spawn
        raises, so a health-only test goes green on the exact break it exists to
        catch. The prompt turn is the assertion.
        """
        gateway = fresh_windows_gateway
        from kiro_crew.testing.fake_acp_backend import REPLY_TEXT

        deadline = time.monotonic() + HEALTH_TIMEOUT_SECS
        health: dict | None = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                health = gateway.get("/api/health", timeout=10)
                break
            except (urllib.error.URLError, OSError) as exc:
                last_error = exc
                time.sleep(1.0)
        if health is None:
            raise AssertionError(
                f"/api/health never answered within {HEALTH_TIMEOUT_SECS:.0f}s: " f"{last_error!r}"
            )

        # Restate both preconditions here so a future reader does not have to
        # infer them from the fixture. The gateway WRITES a default config.json at
        # boot, so the assertion is that the opt-in is not enabled in it -- not
        # that the file is absent, which would be false and would make this test
        # fail for a reason unrelated to the contract.
        config_path = Path(gateway.home) / "config.json"
        if config_path.exists():
            written = json.loads(config_path.read_text(encoding="utf-8"))
            agent_section = written.get("agent") or {}
            assert not agent_section.get("sandbox_allow_unsandboxed_exec", False)
            # The KIROCREW_KIRO_BIN seam only fires when the provider resolves to
            # acp. Fail fast and say so, rather than surfacing it minutes later as
            # an indistinguishable "no reply" timeout.
            assert agent_section.get("provider", "acp") == "acp"
        assert not _kiro_internal_settings_path().exists()

        slot = gateway.post("/api/chat/slots", {})["key"]
        assert slot

        # No ``agent`` is named. The slot dispatches on the binding it was created
        # with, and naming one the seeded HOME does not define is a 409 "slot agent
        # mismatch" -- the same refusal the boot matrix hit, in the same shape, in
        # this file's own copy of the pattern.
        gateway.post("/api/chat?ws=1", {"message": "ping", "slot": slot})

        reply = _await_assistant_reply(gateway, slot, TURN_TIMEOUT_SECS)
        assert REPLY_TEXT in reply
