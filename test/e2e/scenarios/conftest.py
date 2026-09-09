"""Scenario-level E2E over the existing ``kirocrew pod`` tooling.

One session-scoped pod, five scenarios that each drive it through a user-visible
flow: save a setting across a gateway restart, fire a cron, run an agent turn,
render the host service definition against the pod home, and install the built
wheel into a scratch venv.

Plain pytest, deliberately: the repo's isolation, timeout and sharding story is
pytest-shaped (``setup.cfg`` addopts, ``pytest-timeout``, xdist ``loadgroup``),
and a second framework would need a second isolation story to match. See
``docs/system-specs/common/testing-conventions.md``.

Gating mirrors ``docs/ci/e2e-gate.md``:

* ``KIROCREW_E2E_SCENARIOS`` unset -> every scenario skips. The suite boots a
  real service-managed pod, which is minutes and a service manager away from a
  bare ``pytest`` run.
* ``KIROCREW_E2E_REQUIRE=1`` -> every precondition skip becomes a
  FAILURE. A skip counts as a pass, so without this the nightly job would go
  green having run zero scenarios. The same switch the Playwright gate and the
  gateway boot matrix read: one name for one mechanism, since each job runs one
  module set and sets it for its own run.

Platform: the fixture asks the pod's OWN gate (``runtime.require_backend``)
whether this host can run pods at all, so Linux picks systemd and macOS picks
launchd with no test-side platform test. Scenario bodies carry no platform
branching whatsoever, which is what makes Windows a pure matrix add -- and the
Task Scheduler backend now exists, so ``require_backend`` finds one here too. What
is still missing is one validated run, not a backend;
``test/test_pod_scenario_matrix.py`` holds that reason as an assertion, so it
cannot go stale the way this paragraph would.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

from kiro_crew import platform_compat
from kiro_crew.pod import launchd

# The pod plane these scenarios run on is hermetic: its own roots, its own unit
# prefix and its own port band, so a run can never collide with (or reclaim) a
# developer's real pods. `pod/README.md` documents this as the supported way to
# get a test plane.
PLANE_PREFIX = "kirocrew-e2e-pod"
PLANE_BASE_PORT = "7410"

# Bounds. Every wait in this suite is a deadline plus a poll, never a fixed
# sleep: a fixed sleep is both slower than the fast case and a flake in the slow
# one (testing-conventions.md, the timing flake class).
POD_UP_TIMEOUT = 420.0
HEALTH_TIMEOUT = 180.0
# Separate from HEALTH_TIMEOUT on purpose: the private API socket is a LATER
# edge than the HTTP listener, so the two waits are two different questions.
API_TIMEOUT = 120.0
RESTART_TIMEOUT = 180.0
POLL_INTERVAL = 1.0

# A FLOOR under every platform's ``sockaddr_un.sun_path`` cap, NOT "the cap minus
# the NUL" -- do not "fix" it to 107. The caps differ: 108 bytes on Linux, 104 on
# darwin, so a single number has to clear the SMALLER one, and 107 would break
# macOS while looking like a tightening. 100 leaves 8 bytes of headroom under
# darwin's 104 (the NUL plus slack for a longer bound name than the one ``_worst``
# models). Measured in BYTES via ``.encode()``, so a non-ASCII plane root is
# charged for every byte it really costs rather than per character.
_AF_UNIX_MAX = 100
_PLANE_MARKER = ".kce2e-plane"


def _required() -> bool:
    return os.environ.get("KIROCREW_E2E_REQUIRE", "") == "1"


def unresolved(reason: str) -> None:
    """Skip, or FAIL when the caller declared the environment must be present.

    Copied from ``test_playwright_e2e.py::_unresolved`` on purpose: one gating
    contract across both E2E suites means a CI job that sets the REQUIRE marker
    can never report green on a suite that never ran.
    """
    if _required():
        pytest.fail(f"KIROCREW_E2E_REQUIRE=1 but {reason}")
    pytest.skip(reason)


@dataclass(frozen=True)
class PodClient:
    """What a scenario is handed: where the pod is, and how to call it."""

    name: str
    base_url: str
    port: int
    home: Path
    checkout: Path
    cli: Path
    env: dict[str, str]

    # -- transport ---------------------------------------------------------
    def api(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        expect_ok: bool = True,
        timeout: float = 120.0,
    ) -> Any:
        """One authenticated request through ``kirocrew pod api``.

        Deliberately NOT httpx against the TCP port. ``pod api`` mints its own
        token and sends it over the pod's private dashboard unix socket, and it
        refuses when the socket is absent rather than falling back to
        ``127.0.0.1:<port>`` -- which is what stops a scenario from driving
        whatever else answered that port after the pod released it. Reusing it
        also means these scenarios exercise the shipped verb rather than a
        second, test-only client that could drift from it.

        Returns the envelope's ``body``. ``expect_ok=False`` returns the whole
        envelope so a scenario can assert on a deliberate non-2xx.
        """
        argv = [str(self.cli), "pod", "api", self.name, method.upper(), path]
        if method.upper() not in {"GET", "HEAD"}:
            argv.append("--allow-write")
        if payload is not None:
            argv += ["--data", json.dumps(payload)]
        cp = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=self.env,
            cwd=_plane_cwd(self.env),
        )
        envelope = _envelope_or_fail(cp, method, path)
        if not expect_ok:
            return envelope
        assert (
            envelope.get("ok") is True
        ), f"{method} {path} -> status {envelope.get('status')}: {envelope.get('body')!r}"
        return envelope.get("body")

    def health(self) -> int:
        """The pod's OWN health code, via ``pod status --json``.

        Never a bare probe of ``base_url``: every Kiro Crew gateway answers
        ``/api/health`` identically, so a 200 from that port proves only that
        something is there. ``pod status`` reports -2 when the responder is
        provably another instance.
        """
        cp = _run_cli(self.cli, ["pod", "status", self.name, "--json"], self.env)
        if cp.returncode != 0:
            return 0
        try:
            return int(json.loads(cp.stdout or "{}").get("health", 0))
        except (ValueError, AttributeError):
            return 0

    def logs(self, lines: int = 80) -> str:
        cp = _run_cli(self.cli, ["pod", "logs", self.name, "-n", str(lines)], self.env)
        return cp.stdout or cp.stderr or ""


def _envelope_or_fail(cp: subprocess.CompletedProcess[str], method: str, path: str) -> dict:
    """Parse ``pod api``'s fixed-key envelope, or fail naming what came back.

    ``pod api`` exits 1 on a non-2xx while still printing the envelope, so a
    non-zero return code is not on its own a reason to give up on the output.
    An UNPARSEABLE stdout is, and it is a different fault: the command did not
    reach the documented contract at all.
    """
    try:
        parsed = json.loads(cp.stdout or "")
    except ValueError:
        pytest.fail(
            f"`pod api {method} {path}` printed no JSON envelope "
            f"(exit {cp.returncode}).\nstdout: {cp.stdout!r}\nstderr: {cp.stderr!r}"
        )
    if not isinstance(parsed, dict):
        pytest.fail(f"`pod api {method} {path}` envelope was {type(parsed).__name__}, want object")
    return parsed


def _plane_cwd(env: dict[str, str]) -> str:
    """The plane's scratch root, which every pod child runs FROM.

    The pod CLI resolves the checkout through ``KIROCREW_POD_REPO`` (set in
    ``_plane_env``), never through its working directory, so nothing is lost by
    moving the child off the checkout; what is gained is that any path a child
    writes relative to its cwd lands under the plane root the fixture reclaims,
    never in the repository pytest happens to be running from.
    """
    return str(Path(env["KIROCREW_POD_ROOT"]).parent)


def _run_cli(
    cli: Path, argv: list[str], env: dict[str, str], timeout: float = 600.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(cli), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        cwd=_plane_cwd(env),
    )


def _repo_root() -> Path:
    """The checkout under test, resolved the way the pod plane resolves it."""
    return Path(__file__).resolve().parents[3]


def _sweep_stale_plane_roots(base: Path) -> None:
    """Remove suite-marked ``kce2e-<pid>`` roots whose owning pytest is gone.

    A hard kill (pytest-timeout, a cancelled CI job) skips the fixture's
    ``finally``, and a root outside pytest's own temp tree has no other sweeper.
    The pid in the leaf is the pytest process that made it, so a dead pid is a
    root nobody will reclaim. Best-effort and never raises.
    """
    try:
        candidates = list(base.glob("kce2e-*"))
    except OSError:
        return
    for stale in candidates:
        pid_text = stale.name.rsplit("-", 1)[-1]
        if (
            not stale.is_dir()
            or not (stale / _PLANE_MARKER).is_file()
            or not pid_text.isdigit()
            or int(pid_text) == os.getpid()
        ):
            continue
        # pid_exists, never os.kill(pid, 0): on Windows signal zero TERMINATES
        # the target, which here would be another live pytest process.
        if not platform_compat.pid_exists(int(pid_text)):
            shutil.rmtree(stale, ignore_errors=True)


def _plane_root(name: str, basetemp: Path | None) -> Path:
    """A plane root SHORT enough that the pod's unix socket path fits AF_UNIX.

    A pod's gateway binds its private dashboard socket at
    ``<pod home>/dashboard-<port>.sock``, and AF_UNIX caps a path at 108 bytes.
    Under a deep pytest temp root the full path blew that cap, the gateway logged
    ``dashboard unix socket unavailable (AF_UNIX path too long); internal API
    stays TCP-only``, and every ``pod api`` call then refused correctly and
    permanently -- `pod api` has no TCP fallback on purpose, because a released
    port can be rebound by anyone. The pod still answered health, so the failure
    read as a broken API rather than a too-long path.

    So the root is chosen for length and VERIFIED, not assumed: the worst-case
    socket path is measured and a root that cannot host one is refused up front
    with the number, rather than producing a pod whose API is unreachable.

    pytest's own basetemp is tried FIRST: pytest keeps only the last few
    basetemps and removes older ones on the next run, so a root there is
    reclaimed even when a hard kill skipped this fixture's ``finally``. The
    fallbacks (``TMPDIR``, ``/tmp``, the home directory) are for a basetemp too
    deep to host a socket, and each is swept of roots left by dead pytest
    processes before it is used, for the same reason.
    """
    leaf = f"kce2e-{os.getpid()}"

    def _worst(root: Path) -> int:
        # 5 digits of port plus the fixed name the gateway binds.
        return len(str(root / "h" / name / "dashboard-65535.sock").encode())

    bases: list[Path] = []
    if basetemp is not None:
        bases.append(basetemp)
    bases += [Path(tempfile.gettempdir()), Path("/tmp"), Path.home()]
    tried: list[tuple[Path, int]] = []
    for base in bases:
        root = base / leaf
        width = _worst(root)
        tried.append((root, width))
        if width <= _AF_UNIX_MAX:
            if base != basetemp:
                _sweep_stale_plane_roots(base)
            return root
    detail = ", ".join(f"{r} ({w} bytes)" for r, w in tried)
    unresolved(
        "no pod plane root on this host is short enough for a unix socket: the "
        f"pod's dashboard socket path would exceed the {_AF_UNIX_MAX}-byte "
        f"AF_UNIX limit under every candidate, so `pod api` could never reach "
        f"it. Tried {detail}"
    )
    raise AssertionError("unreachable: unresolved() always raises")


def _plane_env(root: Path, scratch: Path, fake_backend: Path | None) -> dict[str, str]:
    """Environment for every pod-plane command in this suite.

    ``KIROCREW_POD_KIRO_BIN`` is the offline seam: a booted pod starts from the
    service manager's clean environment, so exporting ``KIROCREW_KIRO_BIN`` here
    would never reach its gateway. The plane knob is pinned into the service
    definition instead (pod/README.md).
    """
    env = dict(os.environ)
    env.update(
        {
            # The CONTROL plane's own data home, pinned under scratch: every
            # `kirocrew pod` verb runs `ensure_data_home()` against KIROCREW_HOME
            # before anything else, and an inherited value (or none) would have
            # this suite create or migrate the operator's real `~/.kiro/crew`.
            # The pods' homes live under KIROCREW_POD_ROOT and are unaffected.
            "KIROCREW_HOME": str(scratch / "kh"),
            "KIROCREW_WORKSPACE": str(scratch / "kw"),
            # `h`, not `pod-homes`: every character here is charged against the
            # pod socket's AF_UNIX budget (see _plane_root).
            "KIROCREW_POD_ROOT": str(scratch / "h"),
            "KIROCREW_POD_ENV_DIR": str(scratch / "e"),
            "KIROCREW_POD_ARTIFACTS_DIR": str(scratch / "a"),
            "KIROCREW_POD_BASE_PORT": PLANE_BASE_PORT,
            "KIROCREW_POD_UNIT_PREFIX": PLANE_PREFIX,
            "KIROCREW_POD_REPO": str(root),
            "KIROCREW_POD_WORKTREES_ROOT": str(root.parent),
        }
    )
    if fake_backend is not None:
        env["KIROCREW_POD_KIRO_BIN"] = str(fake_backend)
    return env


def _resolve_backend() -> Path | None:
    """The agent backend a pod in this suite should spawn, or ``None`` for the real one.

    Mirrors ``pod-e2e.sh``'s honest refusal: a requested driver that is missing
    is never quietly downgraded to green. Here the two answers are both valid --
    the fake backend is the offline default, and ``KIROCREW_E2E_SCENARIOS_REAL_AGENT=1``
    opts into the host's signed-in ``kiro-cli`` -- but a request for the REAL
    agent on a host with no ``kiro-cli`` on PATH is refused rather than silently
    served by the fake.
    """
    if os.environ.get("KIROCREW_E2E_SCENARIOS_REAL_AGENT", "") == "1":
        if shutil.which("kiro-cli") is None:
            unresolved(
                "KIROCREW_E2E_SCENARIOS_REAL_AGENT=1 but no `kiro-cli` on PATH, so the "
                "requested driver is absent; unset it to run against the fake backend"
            )
        return None
    from kiro_crew.testing import fake_acp_backend

    backend = Path(fake_acp_backend.__file__)
    if not backend.is_file():
        unresolved(f"packaged fake ACP backend not found at {backend}")
    return backend


def _wait_for(deadline_secs: float, probe, want) -> Any:
    """Poll ``probe`` until it returns a value ``want`` accepts, or the deadline.

    Probe first, test the deadline after -- a do-while. A pre-test loop reads a
    clock that may already have passed a deadline computed a fork earlier and
    skips its only body, reporting a timeout on a question nobody asked.
    """
    deadline = time.monotonic() + deadline_secs
    last: Any = None
    while True:
        last = probe()
        if want(last):
            return last
        if time.monotonic() >= deadline:
            return last
        time.sleep(POLL_INTERVAL)


@pytest.fixture(scope="session")
def skip_unless_required():
    """Skip on a missing precondition, or FAIL when the REQUIRE marker is set."""
    return unresolved


@pytest.fixture(scope="session")
def render_service_definition():
    """Render THIS host's service definition, print-only, in a chosen environment."""
    return render_host_service_definition


@pytest.fixture(scope="session")
def wait_for():
    """The suite's bounded poll, handed over as a fixture.

    A fixture rather than a cross-file import: a scenario file must be runnable
    alone, and ``from conftest import ...`` depends on rootdir/sys.path shape
    that changes with how pytest was invoked.
    """
    return _wait_for


@pytest.fixture(scope="session")
def pod(tmp_path_factory: pytest.TempPathFactory):
    """One booted, healthy, seeded pod for the whole scenario suite.

    Sequence, all through shipped verbs:

    1. ``pod prune --all`` as a pre-step, then assert ``pod ls`` is empty. The
       plane is hermetic, so this reclaims only this suite's own leftovers from
       an earlier crashed run -- and asserting the listing is empty afterwards is
       what stops a scenario from adopting a stale pod and reporting on it.
    2. ``pod up --seed minimal --crons``, with the fake ACP backend pinned into
       the service definition.
    3. Bounded health polling through ``pod status --json``.
    4. ``pod down`` in teardown, then a leak check: the pod must be off the
       listing AND its isolated home gone, because ``down``'s zero-residue
       guarantee is the thing most worth regression-testing here.
    """
    if not os.environ.get("KIROCREW_E2E_SCENARIOS"):
        pytest.skip("pod scenario E2E. Set KIROCREW_E2E_SCENARIOS=1 to run.")

    from kiro_crew.pod import runtime as rt

    # The pod's own gate, not a platform test of our own: on Linux this is the
    # systemd + session-bus check, on macOS the launchd one, and on a host with a
    # future backend it is whatever that backend declares.
    try:
        rt.require_backend()
    except Exception as exc:  # PodError / PodBackendAbsent, both refusals
        unresolved(f"this host cannot run pods: {exc}")

    root = _repo_root()
    cli = root / ".venv" / "bin" / "kirocrew"
    if not (cli.is_file() and os.access(cli, os.X_OK)):
        unresolved(
            f"no kirocrew in the checkout venv at {cli}; the suite must drive the "
            "branch under test, not the host's installed build "
            "(build it: python3 -m venv .venv && .venv/bin/pip install -e .)"
        )
    if not (root / "src" / "kiro_crew" / "static" / "dist").is_dir():
        unresolved(
            f"no built SPA dist under {root / 'src' / 'kiro_crew' / 'static' / 'dist'}; "
            "a pod refuses to come up without one (cd website && npm ci && npm run build)"
        )

    backend = _resolve_backend()
    name = root.name
    scratch = _plane_root(name, tmp_path_factory.getbasetemp())
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / _PLANE_MARKER).write_text(str(os.getpid()), encoding="ascii")
    env = _plane_env(root, scratch, backend)

    # Cheapest possible probe that the CLI can BOOT before any assertion depends
    # on it. `kirocrew` fails closed on a host whose platform profile it cannot
    # compose (an enterprise marker with no companion package installed, which is
    # the state of an Amazon dev desktop), and that refusal is a property of the
    # host rather than of anything these scenarios assert. Reported as an
    # unresolved precondition with the CLI's own words, so a REQUIRE run still
    # fails loudly and a developer run says what to fix.
    # The probe is inside its own try, because `_run_cli` RAISES rather than
    # returning on a timeout (`subprocess.run(timeout=...)` -> TimeoutExpired) and
    # the plane root already exists by now. Letting that propagate would leave the
    # root behind, and `_plane_root` may put it OUTSIDE pytest's temp tree, so
    # nothing else would ever reclaim it — a test side effect on the host, which is
    # the one thing this suite must never leave.
    try:
        probe = _run_cli(cli, ["pod", "ls", "--json"], env, timeout=180.0)
    except (subprocess.TimeoutExpired, OSError) as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        unresolved(
            f"`kirocrew pod ls` did not complete on this host ({type(exc).__name__}): "
            "the CLI could not be probed, so no scenario below can be trusted"
        )
        raise  # unreachable: `unresolved` always raises. Kept so a future change to
        # its contract cannot silently fall through into the assertions below.
    if probe.returncode != 0:
        # The plane root already exists and may sit outside pytest's temp tree
        # (see _plane_root), so an ordinary precondition skip must not leave it
        # behind either.
        shutil.rmtree(scratch, ignore_errors=True)
        unresolved(
            f"`kirocrew pod ls` could not run on this host: "
            f"{(probe.stderr or probe.stdout or '').strip()[-1200:]}"
        )

    # Everything from here on mutates the HOST (a template unit, a booted
    # gateway), so it all runs inside one try whose finally tears down whatever
    # got as far as being created: a timeout waiting for health or for the API
    # socket must not leave a live gateway and its unit for the next run to
    # reclaim. The strict teardown assertions apply only once the pod booted;
    # before that, the boot failure itself is the verdict.
    client: PodClient | None = None
    try:
        # Lay the template service definition down for THIS plane before anything
        # boots. The template is written once per machine and BAKES the plane env it
        # was installed with (pod/config.py environment_vars), so a template left by
        # an earlier plane points a booted pod at the wrong env dir and it dies with
        # "has no pinned checkout" -- a failure that reads as a broken worktree
        # build. Idempotent, so re-running costs a file write.
        installed = _run_cli(cli, ["pod", "install"], env, timeout=180.0)
        assert installed.returncode == 0, f"pod install failed: {installed.stderr.strip()}"

        prune = _run_cli(cli, ["pod", "prune", "--all", "--json"], env)
        assert prune.returncode == 0, f"pod prune failed: {prune.stderr.strip()}"
        listed = _run_cli(cli, ["pod", "ls", "--json"], env)
        assert listed.returncode == 0, f"pod ls failed: {listed.stderr.strip()}"
        assert (
            json.loads(listed.stdout or "[]") == []
        ), f"the hermetic plane was not empty after prune: {listed.stdout!r}"

        up = _run_cli(
            cli,
            ["pod", "up", name, "--seed", "minimal", "--crons", "--json"],
            env,
            timeout=POD_UP_TIMEOUT,
        )
        if up.returncode != 0:
            pytest.fail(
                f"`pod up {name}` failed (exit {up.returncode}).\n"
                f"stdout: {up.stdout}\nstderr: {up.stderr}"
            )
        try:
            info = json.loads(up.stdout or "{}")
        except ValueError:
            pytest.fail(f"`pod up --json` printed no JSON: {up.stdout!r}")
        base_url = str(info.get("base_url") or "")
        port = int(info.get("port") or 0)
        assert base_url and port, f"`pod up --json` gave no base_url/port: {info!r}"

        client = PodClient(
            name=name,
            base_url=base_url,
            port=port,
            home=Path(env["KIROCREW_POD_ROOT"]) / name,
            checkout=root,
            cli=cli,
            env=env,
        )

        code = _wait_for(HEALTH_TIMEOUT, client.health, lambda c: c in (200, 401, 403))
        if code not in (200, 401, 403):
            pytest.fail(
                f"pod {name} never became healthy on :{port} (last health {code}).\n"
                f"{client.logs()}"
            )

        # Health is NOT the readiness this suite needs. `pod api` travels over the
        # pod's private dashboard unix socket and refuses through its own envelope
        # while that socket is absent, and the gateway binds it AFTER it starts
        # answering HTTP. A scenario fired on health alone therefore raced the socket
        # and read that documented refusal as an API failure. Wait for the transport
        # itself, bounded, and report the refusal text if it never arrives.
        envelope = _wait_for(
            API_TIMEOUT,
            lambda: client.api("GET", "health", expect_ok=False),
            lambda env: bool(env.get("ok")),
        )
        if not envelope.get("ok"):
            pytest.fail(
                f"pod {name} answers health on :{port} but its private API socket never "
                f"answered within {API_TIMEOUT:.0f}s: {envelope.get('body')!r}\n{client.logs()}"
            )

        yield client
    finally:
        # Every cleanup step is guarded SEPARATELY, because `_run_cli` raises on a
        # timeout (`subprocess.run(timeout=...)`) and the first step here is a
        # 300-second `pod down`. A single flat `finally` meant that one timeout
        # skipped the rest of it: the plane root stayed (and `_plane_root` may put it
        # outside pytest's temp tree, so nothing else would ever reclaim it), the
        # systemd template this suite installed stayed on the HOST, and the pod it
        # failed to stop stayed RUNNING.
        #
        # But guarding is not enough on its own: deletion is conditioned on the pod
        # being CONFIRMED GONE. `pod down` timing out means it may still be serving,
        # and reclaiming the plane root then would rmtree a live pod's state from
        # under it -- the same irreversible shape this suite exists to catch. So a
        # confirmed teardown reclaims everything, and an unconfirmed one deletes
        # NOTHING and says so with the paths, leaving a live pod stoppable by name.
        down: Optional[subprocess.CompletedProcess[str]] = None
        after: Optional[subprocess.CompletedProcess[str]] = None
        down_error = ""
        try:
            down = _run_cli(cli, ["pod", "down", name], env, timeout=300.0)
        except (subprocess.TimeoutExpired, OSError) as exc:
            down_error = f"{type(exc).__name__}: {exc}"
        try:
            after = _run_cli(cli, ["pod", "ls", "--json"], env)
        except (subprocess.TimeoutExpired, OSError):
            after = None
        leaked_home = client.home.exists() if client is not None else False
        rows: list[object] = []
        if after is not None and after.returncode == 0:
            try:
                rows = json.loads(after.stdout or "[]")
            except ValueError:
                rows = []
        # Confirmed absent = `pod down` completed cleanly AND the listing was READ
        # and is empty. An unreadable listing is NOT agreement: `rows` is empty in
        # that case too, so it is checked separately -- unknown counts as
        # possibly-alive, because the cost of being wrong is one-directional.
        listing_read = after is not None and after.returncode == 0
        pod_is_gone = (
            not down_error
            and down is not None
            and down.returncode == 0
            and listing_read
            and rows == []
        )
        if pod_is_gone:
            # Read every fact the assertions need FIRST, then reclaim the plane root
            # itself: it may sit outside pytest's temp tree (see _plane_root), so
            # nothing else would ever remove it.
            shutil.rmtree(scratch, ignore_errors=True)
            # The template unit `pod install` wrote is the one host-level artifact
            # that lives outside the plane root, so it is removed here by name and
            # the manager told to forget it. PLANE_PREFIX is this suite's own, so
            # the developer's real `kirocrew-pod@.service` is never touched.
            _remove_plane_unit_template()
        if client is not None and not pod_is_gone:
            detail = (
                f"down_error={down_error!r} "
                f"rc={None if down is None else down.returncode} "
                f"listing={'unreadable' if not listing_read else (after.stdout or '').strip()!r} "
                f"stderr={'' if down is None else (down.stderr or '').strip()!r}."
            )
            if sys.platform in {"linux", "darwin"}:
                cleaned_services, cleaned_pids = _force_clean_plane(scratch, name, env)
                service_kind = "units" if sys.platform == "linux" else "launchd_jobs"
                raise AssertionError(
                    f"`pod down {name}` did not confirm the pod is gone. {detail} "
                    f"Force-cleaned this fixture's {service_kind}={cleaned_services}, "
                    f"pids={cleaned_pids}, and plane={scratch}."
                )
            raise AssertionError(
                f"`pod down {name}` did not confirm the pod is gone. {detail} "
                f"Kept plane={scratch} because forced cleanup is unavailable "
                f"on platform={sys.platform}."
            )
        if client is not None:
            # Independent of the above: `down` can report success and still leave the
            # isolated home, which is the residue the scenario suite is here to catch.
            assert not leaked_home, f"pod down left the isolated home behind: {client.home}"


def _matching_plane_units() -> list[str]:
    """Loaded systemd units owned by this suite's dedicated prefix."""
    if sys.platform != "linux":
        return []
    try:
        cp = subprocess.run(
            [
                "systemctl",
                "--user",
                "list-units",
                f"{PLANE_PREFIX}@*.service",
                "--all",
                "--plain",
                "--no-legend",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if cp.returncode != 0:
        return []
    prefix = f"{PLANE_PREFIX}@"
    return sorted(
        {
            line.split()[0]
            for line in cp.stdout.splitlines()
            if line.split()
            and line.split()[0].startswith(prefix)
            and line.split()[0].endswith(".service")
        }
    )


def _force_clean_plane(
    scratch: Path, name: str, env: dict[str, str]
) -> tuple[list[str], list[int]]:
    """Remove this scenario suite's services, processes, and scratch root."""
    services: list[str]
    if sys.platform == "linux":
        units = sorted(set(_matching_plane_units()) | {f"{PLANE_PREFIX}@{name}.service"})
        for unit in units:
            for verb in ("stop", "reset-failed"):
                try:
                    subprocess.run(
                        ["systemctl", "--user", verb, unit],
                        check=False,
                        capture_output=True,
                        timeout=60,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
        services = units
        _remove_plane_unit_template()
    elif sys.platform == "darwin":
        unit_prefix = env["KIROCREW_POD_UNIT_PREFIX"]
        label_prefix = f"{launchd.LABEL_PREFIX}.{unit_prefix}."
        pods_dir = Path(env["KIROCREW_POD_ROOT"])
        try:
            plists = sorted(pods_dir.glob(f"{label_prefix}*.plist"))
        except OSError:
            plists = []
        labels = sorted(
            {f"{label_prefix}{name}"}
            | {plist.stem for plist in plists if plist.stem.startswith(label_prefix)}
        )
        for label in labels:
            try:
                subprocess.run(
                    ["launchctl", "bootout", f"{launchd.domain()}/{label}"],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        for plist in plists:
            with contextlib.suppress(OSError):
                plist.unlink()
        services = labels
    else:
        services = []

    killed = _kill_plane_processes(scratch)
    shutil.rmtree(scratch, ignore_errors=True)
    return services, killed


def _kill_plane_processes(scratch: Path) -> list[int]:
    """Kill pinned processes whose command line names this plane root."""
    killed: list[int] = []
    leaders = platform_compat.live_thread_group_leaders()
    if leaders is None:
        return killed
    marker = str(scratch.resolve())
    for pid in sorted(leaders):
        if marker not in platform_compat.process_command_line(pid):
            continue
        token = platform_compat.process_start_time(pid)
        if not token:
            continue
        with contextlib.suppress(OSError):
            if platform_compat.kill_pid_pinned(pid, token, platform_compat.SIGKILL):
                killed.append(pid)
    return killed


def _remove_plane_unit_template() -> None:
    """Delete this plane's systemd template unit and reload the user manager.

    Only Linux installs a template (macOS and Windows write per-pod definitions
    at ``up`` that ``down`` already removes). Best-effort: the unit may never
    have been written when the fixture failed early, and a reload failure at
    teardown is not a scenario verdict.
    """
    if sys.platform != "linux":
        return
    unit = Path.home() / ".config" / "systemd" / "user" / f"{PLANE_PREFIX}@.service"
    dropins = unit.with_name(f"{PLANE_PREFIX}@.service.d")
    unit.unlink(missing_ok=True)
    shutil.rmtree(dropins, ignore_errors=True)
    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


@pytest.fixture
def restart_pod_gateway(pod: PodClient):
    """Restart the pod's gateway in place and wait, bounded, for it to serve again.

    ``pod`` has no restart VERB. Its own remediation table maps "restart" to
    ``pod down && pod up``, and ``down`` deletes the isolated home -- which
    destroys the very state a persistence scenario is asserting on, so it cannot
    be the restart under test. The gateway's own ``POST /api/restart`` is the
    in-place path: it ``os.execv``s, so the unit stays active and its MainPID is
    unchanged, and it is the same route the dashboard's restart control drives.
    Reached here through ``pod api``, so no test-only transport is introduced.
    """

    def _restart() -> None:
        body = pod.api("POST", "restart", {})
        assert isinstance(body, dict) and body.get("ok") is True, f"restart refused: {body!r}"
        # The exec replaces the process image, so the port goes away and comes
        # back. Wait for BOTH edges: without the down edge a fast poll reads the
        # pre-exec listener and calls the restart complete before it happened.
        _wait_for(RESTART_TIMEOUT, pod.health, lambda c: c not in (200, 401, 403))
        code = _wait_for(RESTART_TIMEOUT, pod.health, lambda c: c in (200, 401, 403))
        if code not in (200, 401, 403):
            pytest.fail(f"pod gateway did not come back after restart (last {code})\n{pod.logs()}")

    return _restart


def render_host_service_definition(env_overrides: dict[str, str] | None = None) -> str:
    """Render THIS host's service definition, print-only, and return the text.

    The dispatch lives here rather than in a scenario body: ``service install``
    has no ``--dry-run``, but both backends expose the pure renderer the
    installer itself calls (``linux.render_unit`` / ``macos.render_plist``), so
    rendering it in a child process IS the print-only path and it touches
    nothing -- no file written, no service manager consulted.

    Out-of-process for two reasons: the renderers read the process environment
    and ``Path.home()``, which must not be mutated inside a session holding a
    live pod, and a child is the only way to put a pod's own environment in scope
    without putting it in scope for everything else.
    """
    script = (
        "import sys\n"
        "from kiro_crew.service.common import Platform, current_platform\n"
        "plat = current_platform()\n"
        "if plat is Platform.SYSTEMD:\n"
        "    from kiro_crew.service import linux\n"
        "    print(linux.render_unit(user_scope=True))\n"
        "elif plat is Platform.LAUNCHD:\n"
        "    from kiro_crew.service import macos\n"
        "    print(macos.render_plist())\n"
        "else:\n"
        "    sys.exit(f'no service backend for {plat}')\n"
    )
    env = dict(os.environ)
    env.update(env_overrides or {})
    cp = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    if cp.returncode != 0:
        unresolved(f"no service backend to render on this host: {cp.stderr.strip()}")
    return cp.stdout
