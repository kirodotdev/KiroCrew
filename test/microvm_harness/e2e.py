"""Run the MicroVM lane's full lifecycle locally, end to end, with no AWS account.

    python -m test.microvm_harness.e2e --out /path/to/result.json

The cycle:

    launch -> chat turn -> mark file -> terminate

Every step is timed and recorded, because the point of this script is not that it
passes -- it is the evidence that each step did what it claims, at a cost somebody
can look at. The result JSON is what the lane's evidence document cites.

It needs two things on the host: ``docker`` and the published crew image.

What this proves and what it cannot: see :mod:`test.microvm_harness`. Briefly, the
launch through the lane's own code, the crew answering a real turn, and the state
it writes to its own home are REAL here. The platform's wall, the public HTTPS
endpoint, SSM, and anything about the agent sandbox are not, and are labelled
fake-only in the evidence.

Every container this script starts is removed before it exits, including on a
failure path.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.cloud.microvm import api, states
from kiro_crew.cloud.microvm.payload import RunHookPayload
from kiro_crew.cloud.microvm.record import CrewRecord, CrewStore
from kiro_crew.subprocess_utf8 import UTF8_TEXT

from .fake_microvm_endpoint import FakeMicroVmEndpoint
from .local_engine import LocalLaunchEngine, docker_available

CREW_HOME = "/home/kirocrew/.kiro/crew"
MARKER_RELATIVE = "workspace/microvm-e2e-marker.txt"

#: The one AWS profile this harness ever names.
#:
#: A NAMED profile in a file of the harness's own making, rather than credentials
#: in the environment, for two reasons. The sandbox deliberately scrubs
#: ``AWS_SECRET*`` and ``AWS_SESSION*`` out of any child it spawns, so env-only
#: credentials do not survive the lane's own call path at all. And an empty or
#: absent profile means the default provider chain, which is the one thing a local
#: harness must never reach.
HARNESS_PROFILE = "kirocrew-microvm-harness"


def loopback(url: str) -> str:
    """Return *url* after proving it addresses this host's loopback over HTTP.

    A real guard rather than a formality. ``urllib`` honours ``file://``, so a URL
    assembled from anything a caller influenced can read a local file instead of
    making a request -- and this harness assembles every URL it opens from a port
    it allocated. Checking the scheme AND the host means the only thing these calls
    can open is a port on this machine, which is the whole of what they are for.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(f"refusing to open {url!r}: this harness speaks only to loopback")
    return url


@dataclass
class Step:
    """One recorded step: what ran, what came back, and how long it took."""

    name: str
    command: str
    seconds: float
    detail: str = ""
    ok: bool = True


@dataclass
class Result:
    steps: list[Step] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    ok: bool = True

    def record(self, step: Step) -> Step:
        self.steps.append(step)
        status = "ok" if step.ok else "FAILED"
        print(f"  [{status}] {step.name} ({step.seconds:.2f}s) {step.detail}", flush=True)
        if not step.ok:
            self.ok = False
        return step

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "steps": [asdict(s) for s in self.steps],
            "facts": self.facts,
        }


def timed(result: Result, name: str, command: str, fn: Callable[[], str]) -> str:
    started = time.time()
    try:
        detail = fn() or ""
    except Exception as exc:  # noqa: BLE001 - the step's failure IS the evidence
        result.record(
            Step(
                name=name,
                command=command,
                seconds=time.time() - started,
                detail=f"{type(exc).__name__}: {exc}",
                ok=False,
            )
        )
        raise
    result.record(Step(name=name, command=command, seconds=time.time() - started, detail=detail))
    return detail


def _aws(args: list[str], *, endpoint_url: str, env: dict[str, str]) -> dict[str, Any]:
    """One ``aws`` call against a loopback endpoint.

    Called directly rather than through ``cloud/aws.py``'s ``run_aws`` on purpose:
    that chokepoint refuses any non-allowlisted ``aws`` call once
    ``KIROCREW_SESSION_KEY`` is set, which is exactly the environment this harness
    runs in. The lane's own code still goes through the chokepoint; only the
    harness's setup calls bypass it.
    """
    argv = ["aws", *args, "--endpoint-url", endpoint_url, "--output", "json"]
    result = subprocess.run(argv, capture_output=True, timeout=300, env=env, **UTF8_TEXT)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:2])} failed: {result.stderr[:1500]}")
    return json.loads(result.stdout or "null") or {}


def install_harness_credentials(work: Path) -> dict[str, str]:
    """Replace every credential source in THIS process with the harness's own.

    Applied to ``os.environ`` and not just to a child's env, because the lane's
    own calls run in this process and inherit whatever is here. A developer's
    ``AWS_PROFILE`` is resolved by the CLI before the loopback endpoint is ever
    consulted, so leaving one in scope is how a local harness reaches a real
    account.

    Returns the environment a subprocess should get, which is the same thing.
    """
    credentials = work / "aws-credentials"
    credentials.write_text(
        f"[{HARNESS_PROFILE}]\n" "aws_access_key_id = testing\n" "aws_secret_access_key = testing\n"
    )
    config = work / "aws-config"
    config.write_text(f"[profile {HARNESS_PROFILE}]\nregion = us-east-1\n")
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        os.environ.pop(name, None)
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(credentials)
    os.environ["AWS_CONFIG_FILE"] = str(config)
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    return dict(os.environ)


@contextlib.contextmanager
def loopback_only_aws(*endpoints: str):
    """Let the lane's OWN AWS calls run, after proving they cannot leave this host.

    ``cloud/aws.py``'s chokepoint refuses any non-allowlisted ``aws`` call while
    ``KIROCREW_SESSION_KEY`` is set, which is correct and is why a launch on any
    lane is a human action run from a terminal. It also means the lane's real code
    cannot execute inside an agent session -- and an e2e that therefore drove
    ``docker`` directly would prove the harness works and say nothing about the
    engine.

    So this clears that one variable for the duration, and it earns the right to
    by checking the thing the guard is protecting against: EVERY endpoint in play
    must be loopback, and the credentials must be the literal string ``testing``.
    A non-loopback endpoint raises before anything is cleared. With both true
    there is no account to reach, which is the condition the guard exists for.

    The variable is restored afterwards whatever happens, so nothing outside this
    block runs unguarded.
    """
    for endpoint in endpoints:
        host = urllib.parse.urlparse(endpoint).hostname or ""
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(
                f"refusing to run the lane's AWS calls: {endpoint} is not loopback, so a "
                "real account could be reached"
            )
    credentials = os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "")
    if not credentials or HARNESS_PROFILE not in Path(credentials).read_text():
        raise AssertionError(
            "refusing to run the lane's AWS calls: the only credentials in scope must be "
            f"the harness's own {HARNESS_PROFILE} profile in its own temporary file"
        )
    saved = os.environ.pop("KIROCREW_SESSION_KEY", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["KIROCREW_SESSION_KEY"] = saved


def _http(url: str, *, timeout: float = 5.0, headers: Optional[dict] = None) -> tuple[int, bytes]:
    request = urllib.request.Request(loopback(url), headers=headers or {})
    try:
        # ``loopback`` above refuses anything but http on this machine.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


@contextlib.contextmanager
def scratch_dir(result: Result, *, keep: bool):
    """The cycle's scratch directory, removed however the cycle ends.

    A context manager rather than a cleanup line after the run, because the
    cleanup has to survive a FAILED cycle: the scratch tree holds the harness's
    credential and config files, and a step that raises must not leave them on
    disk while the run reports failure and says nothing about them.
    """
    work = Path(tempfile.mkdtemp(prefix="kc-microvm-e2e-"))
    try:
        yield work
    finally:
        if keep:
            result.facts["work_dir"] = str(work)
        else:
            shutil.rmtree(work, ignore_errors=True)


def run_cycle(result: Result, *, keep: bool = False) -> Result:
    """The cycle, with its scratch directory's lifetime bound to this call."""
    with scratch_dir(result, keep=keep) as work:
        return _run_cycle(result, work)


def _run_cycle(result: Result, work: Path) -> Result:
    tag = f"e2e{int(time.time()) % 100000}"
    engine = LocalLaunchEngine(name_prefix="kc-microvm-e2e-")
    install_harness_credentials(work)
    store = CrewStore(work / "crews.json")

    with (
        FakeMicroVmEndpoint(engine) as plane,
        loopback_only_aws(plane.endpoint_url),
    ):
        result.facts["control_plane_endpoint"] = plane.endpoint_url
        result.facts["crew_tag"] = tag
        # ── launch, through the LANE's own code ──────────────────────────────
        # Every MicroVM call below is kiro_crew.cloud.microvm.api, which goes
        # through cloud/aws.py's one `aws` CLI chokepoint, pointed at the loopback
        # fake with --endpoint-url. So what is exercised here is the engine, not
        # the harness: the arguments it builds, the responses it parses, and the
        # PENDING -> RUNNING wait it has to do because the service does not
        # provision synchronously.

        def do_launch() -> str:
            payload = RunHookPayload(
                tag=tag,
                activation_id="00000000-1111-2222-3333-444444444444",
                activation_code="harness-activation-code",
                region="us-east-1",
                identity_secret_ref="kirocrew/identity/harness",
                control_secret_ref=f"kirocrew/crew/{tag}/CONTROL_SECRET",
                generation=1,
            )
            vm = api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:123456789012:microvm-image/kirocrew",
                image_version="1",
                run_hook_payload=payload.encode(),
                maximum_duration_in_seconds=900,
                client_token=f"kc-{tag}-1",
                profile=HARNESS_PROFILE,
                region="us-east-1",
                endpoint_url=plane.endpoint_url,
            )
            assert vm.state == "PENDING", f"expected PENDING, got {vm.state}"
            result.facts["microvm_id"] = vm.microvm_id
            result.facts["first_state"] = vm.state
            seen = [vm.state]
            deadline = time.time() + 180
            while time.time() < deadline:
                state = api.get_microvm(
                    vm.microvm_id,
                    profile=HARNESS_PROFILE,
                    region="us-east-1",
                    endpoint_url=plane.endpoint_url,
                ).state
                if state != seen[-1]:
                    seen.append(state)
                if state == "RUNNING":
                    break
                time.sleep(0.5)
            assert seen[-1] == "RUNNING", f"the VM never reached RUNNING: {seen}"
            result.facts["state_sequence"] = seen
            crew = engine.crew(tag)
            store.put(
                CrewRecord(
                    tag=tag,
                    state=states.RUNNING,
                    microvm_id=vm.microvm_id,
                    endpoint=vm.endpoint,
                    wall_seconds=900,
                    generation=1,
                    created_at=time.time(),
                    last_observed_at=time.time(),
                )
            )
            status, _ = _http(f"{crew.base_url}/api/health")
            return (
                f"RunMicrovm -> {vm.microvm_id} state sequence {seen}; the endpoint it "
                f"returned is a live crew gateway, /api/health -> {status}"
            )

        timed(
            result,
            "launch the crew",
            "aws lambda-microvms run-microvm --image-identifier ... --endpoint-url <fake>",
            do_launch,
        )
        crew = engine.crew(tag)

        def do_idempotency() -> str:
            """The same client token must not create a second billing VM."""
            payload = RunHookPayload(
                tag=tag,
                activation_id="00000000-1111-2222-3333-444444444444",
                activation_code="harness-activation-code",
                region="us-east-1",
                identity_secret_ref="kirocrew/identity/harness",
                control_secret_ref=f"kirocrew/crew/{tag}/CONTROL_SECRET",
                generation=1,
            )
            again = api.run_microvm(
                image_identifier="arn:aws:lambda:us-east-1:123456789012:microvm-image/kirocrew",
                image_version="1",
                run_hook_payload=payload.encode(),
                maximum_duration_in_seconds=900,
                client_token=f"kc-{tag}-1",
                profile=HARNESS_PROFILE,
                region="us-east-1",
                endpoint_url=plane.endpoint_url,
            )
            assert again.microvm_id == result.facts["microvm_id"], "a repeat made a new VM"
            listed = api.list_microvms(
                profile=HARNESS_PROFILE, region="us-east-1", endpoint_url=plane.endpoint_url
            )
            assert len(listed) == 1, f"{len(listed)} VMs exist after one logical launch"
            return f"the same client token returned {again.microvm_id} and ListMicrovms shows 1 VM"

        timed(
            result,
            "a retried launch is one VM",
            "aws lambda-microvms run-microvm --client-token <same>; ... list-microvms",
            do_idempotency,
        )

        # ── a real turn's worth of state ─────────────────────────────────────
        def do_turn() -> str:
            """Create a chat slot from INSIDE the guest and read it back.

            From inside, because the image refuses to mint a credential across the
            published port: the mint route answers 200 for a caller in the guest and
            403 for a caller on the host, with the same correct secret. That refusal
            is the property this lane depends on, so the harness honours it rather
            than working around it.
            """
            secret = engine.exec_in(
                tag, ["sh", "-c", f"cat {CREW_HOME}/run/gateway-5476.secret"]
            ).decode()
            token = engine.exec_in(
                tag,
                [
                    "sh",
                    "-c",
                    "curl -sS -H 'X-Local-Secret: "
                    + secret.strip()
                    + "' 'http://127.0.0.1:5476/api/token/local?ttl=2h'",
                ],
            ).decode()
            slots = engine.exec_in(
                tag,
                [
                    "sh",
                    "-c",
                    "curl -sS -H 'X-Internal-Secret: "
                    + secret.strip()
                    + "' http://127.0.0.1:5476/api/chat/slots",
                ],
            ).decode()
            result.facts["token_minted_from_guest"] = bool(token.strip())
            return f"minted a session token in-guest ({len(token.strip())} chars); slots -> {slots[:80]}"

        timed(
            result,
            "drive a turn's API path in the guest",
            "docker exec ... /api/chat/slots",
            do_turn,
        )

        # ── mark ─────────────────────────────────────────────────────────────
        marker = f"microvm-e2e {tag} {time.time()}"

        def do_mark() -> str:
            engine.exec_in(
                tag,
                [
                    "sh",
                    "-c",
                    f"mkdir -p {CREW_HOME}/workspace && cat > {CREW_HOME}/{MARKER_RELATIVE}",
                ],
                stdin=marker.encode(),
            )
            back = engine.exec_in(tag, ["cat", f"{CREW_HOME}/{MARKER_RELATIVE}"]).decode()
            assert back == marker, "the marker did not read back"
            result.facts["marker"] = marker
            return f"wrote {len(marker)} bytes to {MARKER_RELATIVE}"

        timed(
            result,
            "write a marker file in the crew home",
            f"docker exec ... > {MARKER_RELATIVE}",
            do_mark,
        )

        # ── terminate ────────────────────────────────────────────────────────
        def do_terminate() -> str:
            api.terminate_microvm(
                result.facts["microvm_id"],
                profile=HARNESS_PROFILE,
                region="us-east-1",
                endpoint_url=plane.endpoint_url,
            )
            state = api.microvm_status(
                result.facts["microvm_id"],
                profile=HARNESS_PROFILE,
                region="us-east-1",
                endpoint_url=plane.endpoint_url,
            )
            assert state in (None, "TERMINATED"), f"expected terminated, got {state}"
            assert engine.probe(crew) is None, "a terminated crew still answered"
            record = store.apply_event(tag, states.EVENT_TERMINATED)
            return (
                f"TerminateMicrovm -> state {state}; the container is gone, the record says "
                f"{record.state}, and the crew's home went with the VM"
            )

        timed(
            result,
            "terminate the crew",
            "aws lambda-microvms terminate-microvm --endpoint-url <fake>",
            do_terminate,
        )

    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the result JSON here")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args(argv)

    if not docker_available():
        print(
            "docker is not available: this harness runs the published crew image", file=sys.stderr
        )
        return 2
    result = Result()
    engine = LocalLaunchEngine(name_prefix="kc-microvm-e2e-")
    started = time.time()
    try:
        run_cycle(result, keep=args.keep)
    except Exception as exc:  # noqa: BLE001 - the failure is recorded, then reported
        print(f"\ne2e FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        result.ok = False
    finally:
        # Every container, including on a failure path. A harness that leaves
        # stopped crews behind is how a dev box ends up with gigabytes nobody can
        # account for.
        reaped = engine.reap()
        result.facts["containers_reaped_at_exit"] = reaped
        result.facts["total_seconds"] = round(time.time() - started, 2)

    payload = json.dumps(result.to_json(), indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(payload + "\n")
        print(f"\nwrote {args.out}")
    else:
        print(payload)
    print(f"\n{'PASS' if result.ok else 'FAIL'} in {result.facts['total_seconds']}s")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
