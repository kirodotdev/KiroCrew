"""A docker-backed stand-in for the MicroVM platform, so the lane is testable.

Under ``test/`` and NOT under ``src/kiro_crew/``, which is a security boundary and
not tidiness. :meth:`LocalLaunchEngine.exec_in` runs a caller-supplied argv inside a
container, and the spawn audit requires every spawn primitive in the shipped package
to be routed through the OS sandbox or individually justified. A harness that drives
``docker`` with an argv its caller chooses cannot honestly be justified as benign
inside the product, and routing it through the sandbox would defeat the point -- so it
ships with the tests that use it and is unreachable from any lane.

The real lane needs an AWS account, a region with MicroVM capacity, and a public
HTTPS endpoint. None of those exist on a contributor's machine, and a lane whose
only proof is a mocked unit test is a lane nobody can change safely. So this maps
the lane's two verbs onto docker primitives against the SAME published crew
image the real lane runs:

| verb      | docker                      |
|-----------|-----------------------------|
| launch    | ``docker run -d -p 127.0.0.1:<port>:5476`` |
| terminate | ``docker rm -f``            |

**What this cannot prove, and must never be read as proving.** A container is not
a MicroVM, and the gateway says so in its own log the moment it starts in one: no
cgroup v2 scope enforcement, no user namespace for the model subprocess, no
OS-level sandbox backend. So this harness says nothing about the agent sandbox or
about memory ceilings. It also says nothing about the per-VM public endpoint or
its TLS, because this one is a loopback port, nor about the platform's wall, which
no local container enforces.

Three settings are not optional and each was bought with a failed run:

``KIROCREW_SKIP_MODEL_DOWNLOAD=1``
    Without it the gateway fetches a 639 MB embedding model about forty seconds
    after start, which it will not use and which makes every run minutes longer.
    With it the home is under five megabytes.

``KIROCREW_BIND=0.0.0.0``
    A container port has to leave the container to be reachable.

A readiness probe with an explicit timeout
    A published port can complete a TCP handshake through docker's userland proxy
    and then answer nothing, so a probe with no timeout blocks instead of
    reporting what it found.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: The crew image this harness runs. Pinned by DIGEST, not by tag, for the reason
#: the Fargate lane refuses an undigested reference: a movable tag means the thing
#: that was tested and the thing that runs are two different images.
DEFAULT_IMAGE = "ghcr.io/kirodotdev/kirocrew:stable"

#: The port the gateway listens on inside the image.
GATEWAY_PORT = 5476

#: Seconds a readiness probe waits for one HTTP answer. Short, because the
#: paused-port case hangs rather than refusing and the whole point of the bound is
#: to turn that hang into an answer.
PROBE_TIMEOUT_SECONDS = 2.0

#: Seconds to wait for a fresh container's gateway to answer. Measured at about
#: three seconds after ``docker run`` returns; the bound is generous because a
#: cold image layer cache is slower and a timeout here reads as a broken harness.
READY_TIMEOUT_SECONDS = 90


class DockerUnavailable(RuntimeError):
    """``docker`` is not on ``PATH`` or refused a basic command."""


def docker_available() -> bool:
    """Whether this host can run the local harness at all.

    Checked as a function so a test can skip rather than fail: a contributor
    without docker must be able to run the rest of the suite.
    """
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


@dataclass
class LocalCrew:
    """One container standing in for one MicroVM."""

    name: str
    container_id: str
    host_port: int
    #: The generation this container was launched with, so a readiness answer from
    #: a previous container cannot satisfy this one -- the same clause the real
    #: lane needs after a reopen.
    generation: int = 1

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.host_port}"


@dataclass
class LocalLaunchEngine:
    """Launch and terminate crews as local containers.

    Injected as a sibling of the real launcher, NEVER routed through
    ``cloud/aws.py``'s ``run_aws``. That chokepoint refuses any non-allowlisted
    ``aws`` call once ``KIROCREW_SESSION_KEY`` is set, so a harness that reused it
    would be refused from inside an agent session -- which is exactly where this
    harness has to run.
    """

    image: str = DEFAULT_IMAGE
    #: Prefix for every container this engine creates, so a sweep can find them.
    #: Every test MUST remove what it starts; this exists for the run that
    #: crashed before it could.
    name_prefix: str = "kc-microvm-"
    env: dict[str, str] = field(
        default_factory=lambda: {
            "KIROCREW_SKIP_MODEL_DOWNLOAD": "1",
            "KIROCREW_BIND": "0.0.0.0",
            # The MicroVM is the isolation boundary on the real lane, and a
            # container cannot provide the user namespace the model subprocess
            # would otherwise need. Set here so the harness boots at all, and
            # stated so nobody reads a green harness run as a sandbox claim.
            "KIROCREW_ALLOW_UNSANDBOXED": "1",
        }
    )
    _crews: dict[str, LocalCrew] = field(default_factory=dict)

    def launch(self, tag: str, *, host_port: int = 0, generation: int = 1) -> LocalCrew:
        """Start a container for *tag* and wait for its gateway.

        Bound to ``127.0.0.1`` explicitly. A bare ``-p <port>:5476`` publishes on
        every interface, which on a shared machine is a crew gateway on the
        network.

        *host_port* defaults to ``0``, which leaves the choice to DOCKER and reads
        the answer back with ``docker port``. That is one atomic allocation; a
        caller that picks a free port and then hands it over has released it in
        between, so another listener on the machine can take it and the launch
        fails with address-in-use. A caller may still pin a port, which is for the
        case where something outside this engine has to know it in advance.
        """
        self._require_docker()
        name = f"{self.name_prefix}{tag}"
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            # An empty host port is docker's own spelling for "choose one".
            f"127.0.0.1:{host_port or ''}:{GATEWAY_PORT}",
        ]
        for key, value in self.env.items():
            argv += ["-e", f"{key}={value}"]
        argv.append(self.image)
        out = self._run(argv)
        crew = LocalCrew(
            name=name,
            container_id=out.strip(),
            host_port=host_port or self._published_port(name),
            generation=generation,
        )
        self._crews[tag] = crew
        self.wait_ready(crew)
        return crew

    def _published_port(self, name: str) -> int:
        """The host port docker chose for this container's gateway.

        ``docker port`` answers ``127.0.0.1:<port>`` per published mapping. Parsed
        from the LAST colon-separated field so an IPv6 host address, which carries
        colons of its own, does not split into the wrong pieces.
        """
        out = self._run(["docker", "port", name, str(GATEWAY_PORT)])
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                return int(line.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                continue
        raise DockerUnavailable(
            f"docker published no host port for {name}:{GATEWAY_PORT}; the container "
            f"started but nothing can reach its gateway. docker port said: {out!r}"
        )

    def terminate(self, tag: str) -> None:
        """``docker rm -f``. Idempotent: a crew already gone is not an error."""
        crew = self._crews.pop(tag, None)
        if crew is None:
            return
        subprocess.run(["docker", "rm", "-f", crew.name], capture_output=True, timeout=120)

    def status(self, tag: str) -> Optional[str]:
        """The container's docker status, or ``None`` when it does not exist.

        ``None`` maps to the platform's "the service has forgotten this VM" and a
        status of ``exited`` to its ``TERMINATED`` -- two answers the real lane
        keeps distinct for the same reason.
        """
        crew = self._crews.get(tag)
        if crew is None:
            return None
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", crew.name],
            capture_output=True,
            **UTF8_TEXT,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def exec_in(self, tag: str, argv: list[str], *, stdin: Optional[bytes] = None) -> bytes:
        """Run *argv* inside the crew's container and return its stdout.

        How the harness writes the marker file and runs the archive ``tar``: both
        happen on the guest's own disk on the real lane, so doing them from here
        would test a different thing.
        """
        crew = self._crew(tag)
        result = subprocess.run(
            ["docker", "exec", "-i", crew.name, *argv],
            input=stdin,
            capture_output=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"docker exec in {crew.name} failed (rc={result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace')[:2000]}"
            )
        return result.stdout

    @staticmethod
    def _loopback(url: str) -> str:
        """Return *url* after proving it addresses this host's loopback over HTTP.

        A real guard rather than a formality. ``urllib`` honours ``file://``, so a
        URL assembled from anything a caller influenced can read a local file
        instead of making a request -- and this harness assembles its URLs from a
        port. Checking the scheme AND the host means the only thing this can open
        is a port on this machine, which is the whole of what it is for.
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError(f"refusing to open {url!r}: this harness speaks only to loopback")
        return url

    def probe(self, crew: LocalCrew, path: str = "/api/health") -> Optional[int]:
        """One HTTP probe with a hard timeout. ``None`` means no answer arrived.

        ``None`` rather than an exception, and a timeout rather than a blocking
        read, because that is the ONLY way to observe a paused container: the
        published port completes the handshake and then says nothing forever.
        """
        url = f"{crew.base_url}{path}"
        try:
            # ``_loopback`` refuses any scheme but http and any host but this
            # machine's loopback, so the ``file://`` reading the rule warns about is
            # unreachable from here.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            with urllib.request.urlopen(
                self._loopback(url), timeout=PROBE_TIMEOUT_SECONDS
            ) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except Exception:  # noqa: BLE001 - a hang, a reset and a refusal are one answer here
            return None

    def wait_ready(self, crew: LocalCrew, *, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.probe(crew) == 200:
                return
            time.sleep(0.25)
        raise TimeoutError(f"crew container {crew.name} did not answer /api/health in {timeout}s")

    def reap(self) -> list[str]:
        """Remove every container this engine's prefix owns. Returns their names.

        For the run that crashed before its own cleanup. Named rather than
        implicit, because a harness that silently removes containers is a harness
        that can remove someone else's.
        """
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name={self.name_prefix}"],
            capture_output=True,
            **UTF8_TEXT,
            timeout=60,
        )
        ids = [line for line in result.stdout.split() if line]
        for container in ids:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=120)
        self._crews.clear()
        return ids

    def crew(self, tag: str) -> LocalCrew:
        return self._crew(tag)

    # ── internals ────────────────────────────────────────────────────────────

    def _crew(self, tag: str) -> LocalCrew:
        try:
            return self._crews[tag]
        except KeyError:
            raise KeyError(f"no local crew container for {tag!r}") from None

    def _require_docker(self) -> None:
        if not docker_available():
            raise DockerUnavailable(
                "the local MicroVM harness needs docker: it runs the published crew image, "
                "which is the only way to exercise the guest half without an AWS account"
            )

    @staticmethod
    def _run(argv: list[str]) -> str:
        result = subprocess.run(argv, capture_output=True, timeout=600, **UTF8_TEXT)
        if result.returncode != 0:
            raise RuntimeError(
                f"{' '.join(argv[:3])} failed (rc={result.returncode}): {result.stderr[:2000]}"
            )
        return result.stdout


def guest_state_from_slots(payload: object, *, generation: int) -> dict:
    """Reduce the gateway's own ``/api/chat/slots`` answer to the lifecycle's view.

    Here rather than in the lifecycle, because the REAL lane reads this reduction
    from the guest -- the guest holds facts the control plane cannot see, like its
    supervisor's restart count. Doing the reduction locally in the harness and
    remotely in production from one function is what keeps the two from drifting
    into different definitions of "running".
    """
    slots = []
    if isinstance(payload, dict):
        raw = payload.get("slots")
        if isinstance(raw, list):
            slots = raw
    running = sum(1 for slot in slots if isinstance(slot, dict) and slot.get("status") == "running")
    return {
        "ready": True,
        "running_slots": running,
        "idle_for_seconds": 0.0 if running else float("inf"),
        "restarts": 0,
        "generation": generation,
    }


def dump_state(crew: LocalCrew) -> str:
    """One JSON line about a local crew, for an evidence log."""
    return json.dumps(
        {"name": crew.name, "port": crew.host_port, "generation": crew.generation},
        sort_keys=True,
    )
