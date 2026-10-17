#!/usr/bin/env python3
"""PID 1 on the Lambda MicroVM lane: answer the platform's hooks, start the crew.

``python -m container.microvm.hooks``. On Fargate ECS starts
``container.supervisor`` and this module does not run at all; the image is the
same and the entrypoint is what differs, which is the whole shape of the lane.

WHAT THE PLATFORM GIVES AND TAKES
---------------------------------
A MicroVM has exactly one channel in before it has registered itself anywhere: an
HTTP call from the platform to one port in the guest, at each lifecycle event.
Six paths under ``/aws/lambda-microvms/runtime/v1``: ``ready`` and ``validate``
during the image build, then ``run`` / ``resume`` / ``suspend`` / ``terminate``
while a VM is alive. ``run`` carries the launch payload, which is the only way
the guest learns its SSM activation and its secret references.

**The run hook's budget is 60 seconds.** Documented as 600 and refused above 60 by
the service -- measured live, ``ValidationException: Value '300' at
'hooks.microvmHooks.runTimeoutInSeconds' failed to satisfy constraint: Member must
have value less than or equal to 60``. So this module does the minimum the launch
is waiting on INSIDE the hook (give the VM its own machine identity, register the
SSM node) and everything else on a worker thread. A crew's first boot -- secrets,
backend, model -- does not fit in a minute and nothing is waiting for it.

WHAT IT MUST NOT DISCLOSE
-------------------------
This listener's port is reachable from the internet by anyone holding
``lambda:CreateMicrovmAuthToken`` in the owner's own account: the VM's HTTPS
endpoint is always up, the endpoint credential names a PORT rather than a path,
and the ``NO_INGRESS`` connector does not govern it. Measured live, from
a machine outside the account's network. Three consequences are code here, not
advice:

* **Every reply is a constant.** Any path that is not one of the six answers one
  fixed body, and no reply says whether a crew is running, which crew it is, or
  whether a credential arrived. The stranger asking is indistinguishable from the
  platform, because the hook call carries no credential this process can check.
* **``run`` is single-shot.** A second ``run`` would reset ``/etc/machine-id`` and
  re-register the SSM agent under a new identity, severing the owner's only route
  to their own crew. A probe really did reach ``POST .../run`` with a token and
  get a 200; it was inert only because of the marker this module writes. The
  marker is written once a payload passes the region/account check and carries a
  usable activation, so a payload this guest refuses leaves ``run`` available for
  the platform's real call; a failure before the agent registers clears it again.
* **The account the VM joins is checked, after it joins.** The activation in the
  payload decides which account the node enrolls in, and nothing in the payload
  says which account that is. So an image created with an account pin reads the
  account back from the node's own published credential before the crew starts,
  and a mismatch deregisters the node and leaves the crew unstarted. An image
  created without the pin does not check the account at all.

  What that check leaves open is stated rather than hidden: the root SSM agent
  runs from registration until the check answers, so a caller who won ``run``
  with another account's activation could send that agent commands in the
  window. Winning ``run`` takes ``lambda:CreateMicrovmAuthToken`` in the OWNER's
  account, so on this lane's single-owner deployment that caller already holds
  the owner's own AWS access. Closing the window for a shared account is tracked
  as a follow-up.
* **No ``Server`` header.** The default names the interpreter version.

The crew itself is NOT on this port. The front process binds loopback on this
lane (``SMC_FRONT_BIND``), so the only thing the endpoint can reach is the fixed
replies above, and the owner reaches the front through an SSM port-forward that
dials loopback inside the guest's own network namespace.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from .payload_shape import decode_payload, payload_encoding

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
HOOK_PORT = int(os.environ.get("SMC_HOOK_PORT", "8080"))

#: Guest-only state, deliberately OUTSIDE the data home so none of it can be
#: read back as crew state.
# Absolute paths INSIDE THE GUEST, which is a Linux MicroVM and never a
# developer's machine. Composed from segments rather than written as literals so
# that nothing here reads as a path this repository expects to exist on the host
# it is built on: this module only ever runs as PID 1 in the image.
_ROOT = "/"


def _guest_path(*segments: str) -> str:
    """One absolute guest path, as POSIX."""
    return _ROOT + "/".join(segments)


ETC = "etc"
VAR = "var"
USR = "usr"

#: Where the hook listener keeps its own state, outside the data home.
STATE_DIR = _guest_path(VAR, "lib", "microvm-guest")

#: The node identity the SSM agent fingerprints. Rewritten per VM, because every
#: MicroVM resumes from one snapshot and a shared id collides on a single node.
MACHINE_ID = _guest_path(ETC, "machine-id")

#: dbus's copy of the same id, kept in step when the file exists.
DBUS_MACHINE_ID = _guest_path(VAR, "lib", "dbus", "machine-id")

#: The agent's registration record. Its ABSENCE in the image is what proves the
#: build did not register a node identity into the snapshot.
SSM_REGISTRATION = _guest_path(VAR, "lib", "amazon", "ssm", "registration")

#: Where the crew's data home sits when the environment names none.
DEFAULT_DATA_HOME = _guest_path(VAR, "lib", "kirocrew")

#: The marker that makes ``/run`` single-shot. Named for when it is written, which
#: is as soon as a ``/run`` body DECODES -- before the payload is judged, so a
#: payload this guest refuses consumes the one ``/run`` it answers and the caller
#: cannot send a second, different one. It is removed again only on the one path
#: that must stay retryable: a registration that failed (see
#: :func:`_handle_run_once`). A body that does not decode is not an attempt and
#: writes nothing.
RUN_MARKER = f"{STATE_DIR}/run.attempted"

#: Held for the whole single-shot ``/run`` bootstrap, so the marker check and the
#: marker write are one step rather than two with the bootstrap in between.
_RUN_LOCK = threading.Lock()
PAYLOAD_SEEN = f"{STATE_DIR}/payload-seen.json"
BOOT_STATE = f"{STATE_DIR}/boot.json"

AGENT_BIN = _guest_path(USR, "bin", "amazon-ssm-agent")

#: The login user the image's crew processes run as (``Dockerfile``: ``USER crew``).
CREW_USER = "crew"

#: The crew user's own home, which the base image creates and chowns to that uid.
#: Named here so one place can say what must NOT be under it.
CREW_HOME = _guest_path(VAR, "lib", CREW_USER)

#: HOME for the SSM agent, and only for it. ``Dockerfile.microvm`` creates it
#: ``0700 root:root``, which is the whole point: with ``Profile.ShareCreds`` the
#: agent publishes the managed node's role credentials under its own HOME, and the
#: image's process-wide ``HOME`` is the crew user's own directory -- a uid the crew
#: owns, so it could read that file and replace the ``.aws`` directory before a
#: rotation. That role reads every crew's secret under the lane's prefix, so the
#: credential file must sit where the crew uid can neither traverse nor write.
#:
#: Outside ``/var/lib/amazon/ssm`` deliberately. Run Command stages a script under
#: the agent's orchestration root there and runs it as ``sudo -u crew``, so that
#: tree has to stay traversable by the crew uid and cannot hold the credential.
AGENT_HOME = _guest_path(VAR, "lib", "microvm-agent-home")

#: Where the SSM agent publishes the managed node's role credentials, in the order
#: the guest looks. Both are root-only directories: :data:`AGENT_HOME` is what
#: :func:`agent_env` hands the agent, and ``/root`` is where it writes if HOME ever
#: reaches it unset -- a program that takes its home from the passwd entry rather
#: than from the environment. Nothing under :data:`CREW_HOME` belongs here, and
#: ``test_microvm_guest_hardening`` is what keeps one from being added: it also
#: requires the image to gate every directory named here.
SHARED_CREDS_CANDIDATES = (
    os.path.join(AGENT_HOME, ".aws", "credentials"),
    "/root/.aws/credentials",
)

_lock = threading.Lock()
_children: "dict[str, subprocess.Popen]" = {}
_boot: "dict[str, Any]" = {"stage": "waiting for run"}
_region = os.environ.get("AWS_REGION", "us-east-1")
_managed_instance_id = ""


def log(event: str, **fields: Any) -> None:
    """One JSON object per line on stdout, which the platform collects.

    Value-free by construction: every call site passes identifiers, states,
    counts and lengths. There is no call that takes an activation code, a control
    secret or an identity document, so one cannot be added by a call site that
    merely looks harmless.
    """
    record = {"ts": round(time.time(), 3), "event": event}
    record.update(fields)
    try:
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - a log must never be why a boot fails
        pass


def ensure_state_dir() -> None:
    """Create the guest's state directory and make it root-only.

    The mode is set on every call rather than only at creation, because
    ``os.makedirs(..., exist_ok=True)`` leaves an existing directory's mode alone
    and the image creates this one with a plain ``mkdir -p``.

    Root-only because of what is in it: :data:`RUN_MARKER` is what makes ``/run``
    single-shot, and removing a file needs write on its DIRECTORY rather than on
    the file. The crew runs as another uid in this same kernel, so a directory
    that uid could write is a marker it could unlink.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip


def write_run_marker() -> None:
    """Write :data:`RUN_MARKER` so only root can read, replace or remove it."""
    ensure_state_dir()
    fd = os.open(RUN_MARKER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(str(time.time()))


def clear_run_marker() -> None:
    """Remove :data:`RUN_MARKER`, making ``/run`` answerable once more.

    One caller, one reason: a registration that failed. Logged rather than raised,
    because that caller re-raises the registration's own error straight after and
    a second exception from this cleanup would replace the diagnosis with the
    cleanup's.
    """
    try:
        os.unlink(RUN_MARKER)
    except OSError as exc:
        log("run.marker_not_cleared", error=repr(exc))


def write_state(path: str, data: "dict[str, Any]") -> None:
    # The directory of the PATH given, not of ``STATE_DIR``. Every caller in the
    # guest writes under that directory, so the two are the same there -- but a
    # function that takes a path and then creates a different directory can only
    # be called with one argument, and this one is also called from the tick's
    # ops module and from tests that redirect it.
    os.makedirs(os.path.dirname(path) or STATE_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
    os.replace(tmp, path)


# ── this VM's own identity ───────────────────────────────────────────────────


def reset_machine_id() -> str:
    """Give this VM a machine id of its own before the SSM agent fingerprints it.

    Every MicroVM from one image resumes from the same snapshot, so
    ``/etc/machine-id`` is byte-identical across all of them, and the agent builds
    its hardware fingerprint from it. Without this, every VM in a fleet claims one
    managed node and each registration invalidates the last.

    **This is best effort, and the distinction matters.** The platform may
    bind-mount the file; a bind mount cannot be truncated in place, and dropping
    it needs a capability the container may not hold. So the write can fail for
    reasons that are the platform's and not this guest's.

    What a failure costs is FLEET correctness, not this VM: one VM with the
    snapshot's baked id registers perfectly well. So the failure is recorded and
    the boot continues, rather than taking a crew down over a collision that
    needs a second VM to happen. The earlier shape of this function raised, which
    meant a platform that bind-mounts the file made every launch fail at the step
    BEFORE the SSM registration -- the crew never came online and the reason was
    invisible, because nothing in the guest had a log destination yet.

    Returns the id in effect afterwards, which is the old one when the write
    failed. The caller logs it either way, so "which id did this VM register
    under" is answerable.
    """
    new_id = uuid.uuid4().hex
    umount = subprocess.run(
        ["umount", MACHINE_ID], capture_output=True, text=True, encoding="utf-8", check=False
    )
    try:
        with open(MACHINE_ID, "w", encoding="ascii") as fh:
            fh.write(new_id + "\n")
    except OSError as exc:
        current = ""
        try:
            with open(MACHINE_ID, encoding="ascii") as fh:
                current = fh.read().strip()
        except OSError:
            pass
        log(
            "machine_id.not_reset",
            error=repr(exc),
            umount_rc=umount.returncode,
            umount_stderr=(umount.stderr or "").strip()[:200],
            kept=current,
        )
        return current
    if os.path.exists(DBUS_MACHINE_ID):
        try:
            shutil.copyfile(MACHINE_ID, DBUS_MACHINE_ID)
        except OSError as exc:
            log("machine_id.dbus_copy_failed", error=repr(exc))
    log("machine_id.reset", machine_id=new_id, umount_rc=umount.returncode)
    return new_id


def register_agent(activation_id: str, activation_code: str, region: str) -> None:
    """Enroll this VM as the hybrid managed node the launch payload names.

    The activation is minted per launch with a registration limit of one, so a
    code that leaked cannot enroll a second machine -- which is why it travels in
    the payload rather than being baked into the image.

    The code does reach this process's argv: ``amazon-ssm-agent -register`` has no
    other input for it. Bounded rather than clean -- the only reader of this VM's
    ``/proc`` is this VM, the code is single-use, it expires within the hour, and
    the registration consumes it immediately. Recorded so it is a known cost.
    """
    started = time.time()
    try:
        proc = subprocess.run(
            [
                AGENT_BIN,
                "-register",
                "-code",
                activation_code,
                "-id",
                activation_id,
                "-region",
                region,
                "-y",
            ],
            # Inside the hook's 60-second budget with room to report. A register
            # that has not answered in fifty seconds will not answer in sixty, and
            # a TimeoutExpired that escapes this function is indistinguishable
            # from a crash -- so it is caught and named.
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=50,
            # The agent's own root-only HOME here too, so no step of the
            # registration can put a credential under the crew user's home.
            env=agent_env(),
        )
    except subprocess.TimeoutExpired:
        log("ssm.register", rc="timeout", seconds=round(time.time() - started, 2))
        raise RuntimeError("amazon-ssm-agent -register did not answer within 50s") from None
    # stderr is logged whatever happens; stdout only on success. The agent's own
    # usage text, which it prints when it rejects an argument, can echo the
    # registration code back -- so the failure path logs the half that cannot
    # carry it.
    log(
        "ssm.register",
        rc=proc.returncode,
        seconds=round(time.time() - started, 2),
        stderr=(proc.stderr or "").strip()[:400],
        stdout=((proc.stdout or "").strip()[:200] if proc.returncode == 0 else "(withheld)"),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"amazon-ssm-agent -register exited {proc.returncode}")


def read_managed_instance_id() -> str:
    try:
        with open(SSM_REGISTRATION, encoding="utf-8") as fh:
            return str(json.load(fh).get("ManagedInstanceID") or "")
    except Exception:  # noqa: BLE001
        return ""


# ── children ─────────────────────────────────────────────────────────────────


def spawn(
    name: str,
    argv: "list[str]",
    env: "Optional[dict[str, str]]" = None,
    *,
    as_user: str = "",
) -> None:
    """Start a long-lived child and remember it, so shutdown can be orderly.

    Not a restart-on-exit supervisor. The crew is one process tree whose failure
    the owner must be able to see, and a loop that silently restarts a crew that
    cannot start is how a VM looks alive for its whole billable lifetime while
    answering nothing.

    ``as_user`` exists because this process is root and the crew must not be. The
    image's own posture is ``USER crew``; the MicroVM layer has to take root back
    for PID 1 (``/etc/machine-id``, the SSM registration), so the drop that
    ``USER crew`` would have done has to happen HERE for the one child that is
    the crew. Dropping both uid and gid, and not relying on sudo: ``sudo -i``
    would also reset the environment this function was given.
    """
    log("child.start", name=name, argv0=argv[0], as_user=as_user or "root")
    kwargs: "dict[str, Any]" = {}
    if as_user:
        kwargs["user"] = as_user
        kwargs["group"] = as_user
    proc = subprocess.Popen(argv, env=env, stdout=sys.stdout, stderr=sys.stderr, **kwargs)
    with _lock:
        _children[name] = proc


def stop_child(name: str, *, timeout: float = 10.0) -> None:
    """Stop the child started as *name* and wait for it, bounded.

    Ends the SSM agent on an account-pin refusal: the agent is what runs
    commands for the account the node joined, so stopping it here ends that
    account's access on this VM without depending on a remote deregistration
    that the refused account may decline.
    """
    with _lock:
        proc = _children.pop(name, None)
    if proc is None or proc.poll() is not None:
        return
    log("child.stop", name=name)
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=timeout)


def agent_env() -> "dict[str, str]":
    """The environment the SSM agent runs with, whose only job is ``HOME``.

    The agent is root and publishes the managed node's role credentials under its
    HOME. The image's process-wide ``HOME`` is the crew user's own directory, so an
    agent that inherits it writes the lane's strongest credential into a directory
    the crew uid owns. :data:`AGENT_HOME` is root-only, and this function is the
    one place that decides which HOME the agent gets.

    ``AWS_SHARED_CREDENTIALS_FILE`` is dropped: :func:`wait_for_shared_credentials`
    sets it on this process so the guest's own boto3 reads the file the agent
    wrote, and handing that name back to the agent would let a value this process
    chose decide where the agent writes.
    """
    env = dict(os.environ)
    env["HOME"] = AGENT_HOME
    env.pop("AWS_SHARED_CREDENTIALS_FILE", None)
    return env


# ── secrets ──────────────────────────────────────────────────────────────────


def wait_for_shared_credentials(timeout: float = 120.0) -> bool:
    """Wait for the SSM agent to publish the managed node's role credentials.

    ``Profile.ShareCreds`` in the agent's config is what makes it write them, and
    they are the guest's ONLY AWS identity: a MicroVM has no instance profile and
    this image carries no baked credential. So no secret can be read before this
    file exists.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in SHARED_CREDS_CANDIDATES:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                log("ssm.credentials_ready", path=path)
                # boto3 resolves the shared file from AWS_SHARED_CREDENTIALS_FILE
                # or from its own HOME, which is not necessarily the agent's. Point
                # it at the file that actually exists rather than hoping the two
                # agree.
                os.environ["AWS_SHARED_CREDENTIALS_FILE"] = path
                return True
        time.sleep(2)
    log("ssm.credentials_absent", checked=list(SHARED_CREDS_CANDIDATES))
    return False


def read_secret(secret_id: str, region: str) -> str:
    """Fetch one secret's value by the reference the payload carried.

    A reference and never a value is the payload's rule: ``run-hook-payload`` is
    an argument to ``RunMicrovm``, and AWS does not document it as sensitive, so a
    value there could sit in that account's CloudTrail request history. One extra
    call in the guest costs the owner nothing.
    """
    import boto3  # imported late: only this path needs an AWS client
    from botocore.config import Config

    client = boto3.client(
        "secretsmanager",
        region_name=region,
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 3}),
    )
    return str(client.get_secret_value(SecretId=secret_id)["SecretString"])


# ── the crew ─────────────────────────────────────────────────────────────────


#: The read-only crew payload the image carries, as ``Dockerfile.crew`` lays it
#: out. The manifest in it is what says which crew this image IS.
BUNDLE_DIR = "/app/crew-bundle"  # noqa: S108 - an image path, not a temp dir

#: Names the crew's own process tree inherits from PID 1, one by one.
#:
#: PID 1 here is root, it holds the registration code in its argv for the length
#: of one call, and :func:`wait_for_shared_credentials` points
#: ``AWS_SHARED_CREDENTIALS_FILE`` at the managed node's role credential -- a role
#: that reads every crew's secret under the lane's prefix. The crew is the one
#: child that runs as another uid, so what it inherits is a decision rather than
#: a default, and an allowlist is the only form of that decision which stays
#: correct when a name nobody here anticipated appears in root's environment.
#:
#: The locale, path and Python names are what any child needs to run at all. The
#: certificate names are read by the model subprocess's own HTTP stack, so a crew
#: in an environment with a private trust store needs them to reach the model.
SUPERVISOR_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "KIROCREW_HOME",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)

#: Prefix carried in whole: the container's own settings contract, which
#: ``container/common/config.py`` reads and ``Dockerfile``/``Dockerfile.microvm``
#: bake. Every name under it is the image's or this function's to set, and none of
#: them is a credential -- ``SMC_CONTROL_SECRET`` is passed as an argument to
#: :func:`supervisor_env` and set explicitly there.
SUPERVISOR_ENV_PREFIX = "SMC_"

#: Names refused even though their prefix is carried, because they name the AWS
#: identity the SSM agent published for the ROOT side of this guest. The crew has
#: no call to make with it: on this lane nothing in the crew's tree reads a
#: secret, a bucket or a key, and the whole point of the agent's root-only HOME is
#: that this credential is not the crew's to use.
SUPERVISOR_ENV_DENYLIST = (
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
)


def inherited_env() -> "dict[str, str]":
    """The allowlisted slice of this process's environment, and nothing else.

    Names absent from :data:`os.environ` are absent from the result rather than
    empty, so a child cannot tell "set to nothing" from "not set" differently from
    how it would without this function.
    """
    picked = {
        name: value
        for name, value in os.environ.items()
        if (name in SUPERVISOR_ENV_ALLOWLIST or name.startswith(SUPERVISOR_ENV_PREFIX))
        and name not in SUPERVISOR_ENV_DENYLIST
    }
    return picked


def bundle_crew_name() -> str:
    """The crew name the image's own manifest declares, or ``""``.

    Read rather than taken from the launch payload, because the supervisor
    compares the name it is given against this manifest and refuses a mismatch.
    The two answer different questions: the manifest says what was BUILT, and
    the payload's tag says what this launch calls it.

    Empty on any failure, so a bundle whose manifest cannot be read falls back to
    the tag rather than refusing to boot -- a crew that starts with the wrong name
    is recoverable and one that never starts is not visible at all.
    """
    try:
        with open(f"{BUNDLE_DIR}/manifest.json", encoding="utf-8") as handle:
            return str(json.load(handle).get("crew_name") or "")
    except (OSError, ValueError):
        return ""


def supervisor_env(
    payload: "dict[str, Any]", control_secret: str, identity: str
) -> "dict[str, str]":
    """The environment the supervisor would have been given by a task definition.

    This is the lane's whole adaptation: on Fargate these values are a task
    definition's environment and secrets; here they are assembled from the run
    payload and two Secrets Manager reads. The NAMES are the Fargate contract
    unchanged, so one supervisor serves both lanes and neither has a branch.

    ``SMC_FRONT_BIND`` is the one value whose answer differs by lane, and it is
    set rather than defaulted: see ``front/__main__``.

    The base is :func:`inherited_env`, an allowlist, rather than this process's
    whole environment: PID 1 is root on a guest whose root identity reads every
    crew's secret under the lane's prefix, and the crew runs as another uid.
    """
    env = inherited_env()
    env.update(
        # The BUNDLE's crew name, not the launch tag. The supervisor checks the
        # name it is given against the bundle's manifest and refuses when they
        # differ, so taking the tag here crashes the crew whenever an operator's
        # launch tag is not also the crew's name -- which is ordinary, since the
        # tag is this launch's id and the name belongs to the bundle. The tag is
        # still what the control plane calls this crew; it is just not what the
        # IMAGE is.
        SMC_CREW_NAME=bundle_crew_name() or str(payload.get("tag") or ""),
        # Strict auth, asserted here as well as baked into the image's own ``ENV``.
        #
        # Not redundant: the ``SMC_`` prefix is carried in whole, so the image's
        # value already arrives -- and so would a value the image was built with
        # at an operator's choosing. This line is what makes the guest's answer
        # the final one. A lane whose compute carries an internet-reachable
        # endpoint and no security group cannot let a caller choose the posture.
        SMC_REQUIRE_AUTH_ALL_ROUTES="1",
        SMC_CONTROL_SECRET=control_secret,
        KIRO_IDENTITY=identity,
        # The VM is the isolation boundary and the crews are the operator's own.
        # Both flags name a trust boundary the deployment is asserting, which is
        # why neither has a default that assumes it.
        SMC_SINGLE_PRINCIPAL="1",
        SMC_INTERNAL_ONLY="1",
        SMC_FRONT_BIND="127.0.0.1",
    )
    return env


def boot_crew(payload: "dict[str, Any]") -> None:
    """Secrets, then conversations, then the crew -- on a worker thread.

    Off the hook's own thread because the hook has 60 seconds and this does not
    fit in them, and because nothing is waiting for it: the launch waits for the
    SSM node, which the inline half of ``run`` has already arranged.

    The order is forced. Credentials before secrets, because the guest's only AWS
    identity is the one the SSM agent publishes. The account check between them,
    because it reads that same credential and because a node in an account this
    image does not pin must not be handed the owner's control secret. The identity
    last of the two secrets, so a failure to read it is reported after the control
    secret that every other call needs.
    """
    stage = "start"
    try:
        region = str(payload.get("region") or _region)
        control_ref = str(payload.get("secretRef") or "")

        stage = "shared-credentials"
        _boot.update(stage=stage)
        if not wait_for_shared_credentials():
            raise RuntimeError(
                "the SSM agent never published node credentials, so no secret can be read"
            )

        # The account the node ACTUALLY joined, against the pin -- the one check
        # that binds the activation rather than the payload's own strings. Here
        # and not inside the ``/run`` hook because the credential this reads is
        # published by the agent some seconds after it registers, and the hook's
        # whole budget is 60 seconds.
        #
        # Before the two secret reads and before the supervisor, which is what
        # makes a refusal worth anything: a node in a stranger's account never
        # gets the owner's control secret, and the crew bundle never serves.
        stage = "account-pin"
        _boot.update(stage=stage)
        pinned, pin_verdict = account_pin_verdict()
        if not pinned:
            # Stop the local SSM agent FIRST: it is what executes commands for the
            # account the node joined, so this ends that account's access here
            # whether or not the deregistration below is allowed. Then hand the
            # node back.
            #
            # What this does not cover: the agent has to run before it publishes
            # the credential this check reads, so between the agent starting and
            # this line (the credential wait and one STS call) the joined account
            # can already reach the node.
            log("run.account_refused", verdict=pin_verdict)
            stop_child("ssm-agent")
            deregister_self(f"account-pin-{pin_verdict}")
            raise RuntimeError(f"the node joined an account this image does not pin: {pin_verdict}")

        stage = "secrets"
        _boot.update(stage=stage)
        if not control_ref:
            raise RuntimeError("the payload carried no secret reference")
        control_secret = read_secret(control_ref, region)
        # The payload's OWN reference, not one derived from the control secret's
        # name. Both were once built from the launch tag, and that tag is minted
        # by the launcher rather than chosen by the operator -- so a derived name
        # names a secret nobody could have created ahead of the launch, and this
        # read ended every boot at its secrets stage while the VM billed to its
        # wall. A launcher too old to send the field is refused here rather than
        # falling back to the derived name, because that fallback IS the failure.
        identity_ref = str(payload.get("identityRef") or "")
        if not identity_ref:
            raise RuntimeError(
                "the payload carried no model-credential reference. Set "
                "microvm.identity_secret_ref in cloud.json to the secret the "
                "operator created; it cannot be derived from the launch tag, "
                "which the launcher mints"
            )
        identity = read_secret(identity_ref, region)
        # Lengths, never values. Two numbers are enough to tell "the secret was
        # read" from "the secret was empty", which is the only question a log can
        # usefully answer about a credential.
        log("secrets.read", control_chars=len(control_secret), identity_chars=len(identity))

        stage = "supervisor"
        _boot.update(stage=stage)
        spawn(
            "supervisor",
            [sys.executable, "-m", "container.supervisor"],
            env=supervisor_env(payload, control_secret, identity),
            as_user=CREW_USER,
        )
        del control_secret, identity
        _boot.update(stage="started", started_at=time.time())
    except Exception as exc:  # noqa: BLE001 - the stage IS the diagnosis
        log("boot.failed", stage=stage, error=repr(exc))
        _boot.update(stage=f"failed:{stage}", error=repr(exc))
    finally:
        write_state(BOOT_STATE, dict(_boot))


# ── is this payload for THIS VM? ─────────────────────────────────────────────

#: Image-level names that say which account and region a launch of this image may
#: name. They are IMAGE ENVIRONMENT variables, set by
#: ``cloud/microvm/image.py``'s ``guest_environment`` when the lane creates the
#: image -- not ``Dockerfile.microvm`` build args, which the Dockerfile does not
#: declare. An operator whose image was created with both gets the checks below;
#: an image created without them keeps only the checks that need no pin.
EXPECTED_ACCOUNT_ENV = "SMC_MICROVM_ACCOUNT"
EXPECTED_REGION_ENV = "SMC_MICROVM_REGION"


def _arn_parts(value: str) -> "tuple[str, str]":
    """``(region, account)`` of an ARN, and ``("", "")`` for anything that is not one.

    A bare secret NAME is not an ARN and yields nothing, which is a pass rather
    than a refusal: the lane's own launcher may send either form, and a check that
    refused names would refuse every launch that uses them.
    """
    fields = value.split(":")
    if len(fields) < 6 or fields[0] != "arn":
        return "", ""
    return fields[3], fields[4]


def run_payload_verdict(payload: "dict[str, Any]") -> "tuple[bool, str]":
    """Whether what this ``/run`` payload SAYS is consistent, and why not.

    Exactly four comparisons, over two fields and nothing else -- the payload's
    ``region``, and the region and account of ``secretRef``/``identityRef`` when
    those are ARNs:

    * A pinned region refuses a payload, or an ARN inside it, that names another.
    * A pinned account refuses a payload whose secret ARNs name another.
    * With or without a pin, a payload naming two accounts or two regions is
      refused: the lane writes both secrets in one account and one region, so a
      mixture is a payload assembled from more than one launch.

    **What this function does NOT check is the activation, and the activation is
    what decides which account the VM joins.** ``amazon-ssm-agent -register``
    enrolls the node in whichever account minted the activation id and code under
    ``ssm``; nothing about those two strings says which account that is, and this
    function never looks at them. So a payload carrying a stranger's activation
    and no ARNs at all -- ``{"v":1,"ssm":{"id":...,"code":...}}`` -- passes here,
    and the lane's own launcher sends bare secret NAMES, which carry no account
    either. On those payloads the account pin compares nothing.

    What binds the account is :func:`account_pin_verdict`, read AFTER the
    registration, which asks the node's own published credential which account it
    is in. This function is the cheap half that runs before anything is written.

    The verdict is a fixed code from a closed set, never a value out of the
    payload: it reaches a log, and this module's logs carry no caller-chosen
    content.
    """
    expected_account = os.environ.get(EXPECTED_ACCOUNT_ENV, "").strip()
    expected_region = os.environ.get(EXPECTED_REGION_ENV, "").strip()

    regions = set()
    accounts = set()
    claimed_region = str(payload.get("region") or "").strip()
    if claimed_region:
        regions.add(claimed_region)
    for key in ("secretRef", "identityRef"):
        region, account = _arn_parts(str(payload.get(key) or ""))
        if region:
            regions.add(region)
        if account:
            accounts.add(account)

    if expected_region and any(region != expected_region for region in regions):
        return False, "region-is-not-this-vm"
    if expected_account and any(account != expected_account for account in accounts):
        return False, "account-is-not-this-vm"
    if len(regions) > 1:
        return False, "payload-names-two-regions"
    if len(accounts) > 1:
        return False, "payload-names-two-accounts"
    return True, "ok"


def activation_of(payload: "dict[str, Any]") -> "tuple[str, str] | None":
    """The payload's SSM activation id and code, or ``None`` if it has none.

    ``None`` unless ``ssm`` is an object whose ``id`` and ``code`` are both
    non-empty strings: those are the two values ``amazon-ssm-agent -register``
    needs, and a payload without them is one this guest cannot register with.
    """
    block = payload.get("ssm")
    if not isinstance(block, dict):
        return None
    activation_id = block.get("id")
    code = block.get("code")
    if not isinstance(activation_id, str) or not activation_id.strip():
        return None
    if not isinstance(code, str) or not code.strip():
        return None
    return activation_id, code


def joined_account() -> str:
    """The account this node actually registered into, or ``""`` if unreadable.

    Asked of STS with the credential the SSM agent published for the managed
    node, which is the guest's only AWS identity. That credential came from
    whichever account minted the activation, so the answer is the account the
    node JOINED rather than any account a payload named.

    No new grant: ``sts:GetCallerIdentity`` is documented as requiring no
    permissions, so the lane's roles are unchanged and an operator's existing
    image needs nothing added. No live AWS call happens on a developer machine --
    this function only ever runs as PID 1 in the guest, after
    :func:`wait_for_shared_credentials`.

    ``""`` on any failure, and the caller treats that as a refusal rather than a
    pass: see :func:`account_pin_verdict`.
    """
    try:
        import boto3
        from botocore.config import Config

        identity = boto3.client(
            "sts",
            region_name=_region,
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
        ).get_caller_identity()
        return str(identity.get("Account") or "")
    except Exception as exc:  # noqa: BLE001 - unreadable is a verdict, not a crash
        log("account.unreadable", error=repr(exc))
        return ""


def account_pin_verdict() -> "tuple[bool, str]":
    """Whether the account this node joined is the one the image pins, and why not.

    The half :func:`run_payload_verdict` cannot do. That one reads strings out of
    the payload; this one reads the credential the registration produced, so it
    binds the activation -- the thing that actually chose the account.

    Three answers:

    * No pin in the image environment: ``True``, and THE ACCOUNT IS NOT CHECKED.
      An image created without :data:`EXPECTED_ACCOUNT_ENV` keeps exactly the
      behaviour it had before this check existed, which is a deliberate choice
      ``image.guest_environment`` documents on its own side.
    * The account read back differs from the pin: ``False``. The caller
      deregisters the node and never starts the crew.
    * The account could not be read at all: ``False``, the same as a mismatch.
      Fail-closed costs this boot nothing it was not already going to lose: the
      credential is already known to exist, and the next stage reads two secrets
      over the same network path, so a guest that cannot reach STS cannot reach
      Secrets Manager either. Fail-open would let one unreachable endpoint turn
      the check off.
    """
    expected = os.environ.get(EXPECTED_ACCOUNT_ENV, "").strip()
    if not expected:
        return True, "no-account-pin"
    actual = joined_account()
    if not actual:
        return False, "joined-account-unreadable"
    if actual != expected:
        return False, "joined-account-is-not-this-vms"
    return True, "ok"


def handle_run(body: bytes) -> "dict[str, Any]":
    """The real bootstrap, and the one hook that may happen only once.

    A repeat answers 200 and does nothing, in the same shape a first call
    answers: this hook cannot authenticate its caller, so telling a stranger
    their call was ignored would tell them a crew is already up. A payload this
    guest refuses before registering (a region/account mismatch, or no usable
    activation) leaves no marker, so ``/run`` stays available for the platform's
    real call. "repeat" here means "a payload was accepted and registration
    began on this VM", not "a crew is already up": a node the account pin
    refuses after registering keeps its marker.
    """
    ensure_state_dir()
    # Serialised, because the marker is written inside the bootstrap and the
    # check for it is at the start. Two requests arriving before it exists both
    # find it absent and both bootstrap: they reset the machine id, both
    # write ``payload-seen.json`` through the same temp path so one rename
    # removes the other's file, and both spawn a supervisor.
    #
    # A thread lock is the whole requirement here: the listener is one
    # ``ThreadingHTTPServer`` in the guest's PID 1, so concurrent requests are
    # threads of this process and there is no second process to exclude.
    #
    # Re-reading the marker instead would fix nothing -- both callers read a true
    # value. What was missing is that only one of them may act on it.
    with _RUN_LOCK:
        if os.path.exists(RUN_MARKER):
            log("run.repeat_ignored")
            return {"ok": True, "repeat": True}
        return _handle_run_once(body)


def _handle_run_once(body: bytes) -> "dict[str, Any]":
    """The bootstrap itself, run with :data:`_RUN_LOCK` held and no marker on disk."""
    global _managed_instance_id

    # The VM's own start, which is what the payload's wall edges are offsets
    # from. Taken HERE because this hook is the platform's first call into a
    # started VM: taking it after the crew boots would move both edges later by
    # however long boot took, and the one edge that must not move is the one in
    # front of a lifetime the platform will not extend.
    _boot["vm_started_at"] = time.time()

    # Through the LANE's own decoder, not a local json.loads. The platform
    # delivers the launcher's payload BASE64-ENCODED -- undocumented, measured
    # measured live -- and a guest that reads only raw JSON fails on every real
    # launch with no hint of why. One decoder, pinned by the lane's own test,
    # means the launcher and the guest cannot disagree about the shape.
    try:
        payload = decode_payload(body)
        encoding = payload_encoding(body)
    except Exception as exc:  # noqa: BLE001
        # Length and the exception TYPE only. The body carries a single-use
        # activation code, so its content must never reach a log -- which is
        # also why the earlier version of this line, logging only the length,
        # left three failed launches with no cause at all. The fix is a better
        # decoder, not a louder log.
        log("run.payload_unreadable", bytes=len(body), error=type(exc).__name__)
        return {"ok": True, "repeat": False}

    # Before anything is written and before the agent is registered: a payload
    # that is not this VM's own launch gets the same constant answer a repeat
    # gets and leaves no state behind, the single-shot marker included. This is
    # the payload's own strings only -- the account the node JOINS is checked
    # after registration, in ``boot_crew``.
    #
    # A refusal therefore leaves ``/run`` available, so the platform's real call
    # still registers after a stranger's mismatched one. Retrying past this check
    # gains a caller nothing: it compares a region and an account, neither of
    # them secret, so a caller who wants a passing payload can write one on the
    # first try. The check that binds the account is ``account_pin_verdict``,
    # and the answer here is the same whichever way it goes.
    for_this_vm, verdict = run_payload_verdict(payload)
    if not for_this_vm:
        log("run.payload_refused", verdict=verdict, payload_encoding=encoding)
        return {"ok": True, "repeat": False}

    # The activation's SHAPE, checked here for the same reason: a payload this
    # guest cannot register with must not take the one ``/run``. ``{"ssm": 1}``
    # decodes and passes the check above, and without this it would write the
    # marker and then fail on the first ``.get``, leaving the VM unable to
    # register for the platform's real call.
    activation = activation_of(payload)
    if activation is None:
        log("run.payload_refused", verdict="activation-malformed", payload_encoding=encoding)
        return {"ok": True, "repeat": False}
    activation_id, activation_code = activation

    # The single-shot marker, written once the payload is accepted and before
    # anything acts on it. Root-only, in a root-only directory, because the crew
    # runs as another uid in this kernel and unlinking a file needs write on its
    # directory.
    #
    # The cost is stated rather than hidden: a caller who reaches this route
    # before the platform does, with a payload that passes the check above, takes
    # this VM's one ``/run``, and the owner's real launch is answered as a repeat.
    # The account pin then refuses that node and the crew never starts, so the
    # outcome is a VM the lane relaunches.
    write_run_marker()

    region = str(payload.get("region") or _region)
    try:
        # What arrived, as shapes and lengths. "The payload arrived intact" is a
        # claim someone will want to check, and the activation code's length
        # checks it without recording it.
        write_state(
            PAYLOAD_SEEN,
            {
                "encoding": encoding,
                "tag": payload.get("tag"),
                "gen": payload.get("gen"),
                "region": region,
                "ssm_id": activation_id,
                "ssm_code_len": len(activation_code),
                "secret_ref_present": bool(payload.get("secretRef")),
            },
        )
        log("machine_id.in_effect", machine_id=reset_machine_id())
        register_agent(activation_id, activation_code, region)
    except Exception:
        # The one path the marker is cleared on, so a guest that failed before
        # the agent registered stays retryable. It covers every step between the
        # marker and the registration, so none of them can leave the marker
        # standing over a node that never registered. A standing marker there
        # is a permanently dark crew -- measured live -- because neither the
        # platform nor a relaunch against the same VM can get a second ``/run``.
        #
        # A node the account pin refuses after registering keeps its marker and
        # gets no second try. What is retryable here is the case where this guest
        # already agreed the payload was its own and the agent then did not
        # answer.
        clear_run_marker()
        raise
    # With the agent's own HOME, never this process's. See :func:`agent_env`.
    spawn("ssm-agent", [AGENT_BIN], env=agent_env())
    _managed_instance_id = read_managed_instance_id()
    log("run.registered", managed_instance_id=_managed_instance_id, payload_encoding=encoding)

    threading.Thread(target=boot_crew, args=(payload,), name="boot", daemon=True).start()
    return {"ok": True, "repeat": False}


def crew_is_serving() -> bool:
    """Whether the crew's own processes are still up, by this guest's own look.

    The only liveness question a guest can answer about itself without trusting
    its caller. ``pkill -0`` signals nothing and only reports whether a match
    exists, so this is a question rather than a second terminate.

    Returns ``True`` on any failure to tell. The caller uses this to decide
    whether to keep the crew reachable, and the safe answer when the guest cannot
    see is "it is still serving" -- which leaves the node registered rather than
    cutting a crew off on no information.
    """
    try:
        found = subprocess.run(
            ["pkill", "-0", "-f", "container.supervisor"],
            check=False,
            timeout=10,
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 - cannot tell means do not cut it off
        return True
    return found.returncode == 0


def supervisor_has_run() -> bool:
    """Whether the supervisor ever got as far as starting, or failed trying.

    The question :func:`crew_is_serving` cannot answer. That one looks for a live
    process, so it says "not serving" both for a crew that has STOPPED and for one
    that has not started YET -- and the bootstrap window between the node
    registering and the supervisor coming up is wide: two Secrets Manager reads,
    the shared-credential wait, and the bundle install.

    Read from the boot state this module writes as it goes, which is the only
    record of what the guest has already done.

    ``False`` when the file is absent or unreadable, which is the safe direction:
    the caller uses this to decide whether to hand the node back, and a guest that
    cannot tell what it has done must not cut its own crew off.
    """
    try:
        with open(BOOT_STATE, encoding="utf-8") as fh:
            stage = str((json.load(fh) or {}).get("stage") or "")
    except Exception:  # noqa: BLE001 - cannot tell means do not cut it off
        return False
    # ``started`` is written after the supervisor is spawned. A ``failed:`` stage
    # is also an answer: that boot is over and will not serve, so the node it
    # registered is dead weight and handing it back is right.
    return stage == "started" or stage.startswith("failed:")


def terminate_verdict() -> "tuple[bool, str]":
    """Whether an unauthenticated ``/terminate`` may hand this node back, and why.

    Two questions, because one is not enough. :func:`crew_is_serving` finds a live
    process, so on its own it says "not serving" for a crew that has STOPPED and
    for one that has not STARTED yet -- and this listener answers every interface
    and cannot authenticate its caller, so a stranger's terminate arriving in the
    bootstrap window would be the second case read as the first. That window is
    wide: two Secrets Manager reads, the shared-credential wait, and the bundle
    install all sit between the node registering and the supervisor coming up.

    So the node goes back only when the supervisor ran AND is not running now.
    Being wrong this way leaves a node registered for a VM that did go away, which
    the sweeper reports and an operator can delete. Being wrong the other way is a
    live crew nobody can reach again. Those are not comparable.

    Returns the verdict with the reason, so the caller logs what it decided rather
    than restating the condition.
    """
    if crew_is_serving():
        return False, "the crew is still serving"
    if not supervisor_has_run():
        return False, "the crew has not finished starting"
    return True, "the crew ran and has stopped"


def deregister_self(trigger: str) -> None:
    """Hand the managed node back on the way out.

    Best effort and a backstop: the ``terminate`` hook has no documented timeout.
    It is here because a terminated VM otherwise leaves its ``mi-`` node
    registered and reporting ``Online`` forever -- one leaked per launch, measured live -- and that node is the one resource neither the lane's teardown
    nor its sweeper reclaims today.
    """
    mi = _managed_instance_id or read_managed_instance_id()
    if not mi:
        return
    try:
        import boto3
        from botocore.config import Config

        boto3.client(
            "ssm",
            region_name=_region,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
        ).deregister_managed_instance(InstanceId=mi)
        log("deregister.ok", managed_instance_id=mi, trigger=trigger)
    except Exception as exc:  # noqa: BLE001
        log("deregister.failed", managed_instance_id=mi, trigger=trigger, error=repr(exc))


# ── HTTP ─────────────────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):
    """Six hook paths. Everything else gets one constant answer.

    ``server_version`` and ``sys_version`` are blanked and ``send_response`` is
    narrowed to the status line, so no reply carries a ``Server`` header: the
    default reads ``BaseHTTP/0.6 Python/3.12.x`` and hands anyone who can reach
    this port the guest's interpreter version for free.
    """

    server_version = ""
    sys_version = ""
    protocol_version = "HTTP/1.1"

    #: Seconds any single read or write on one hook connection may block.
    #:
    #: ``socketserver.StreamRequestHandler.setup`` reads this name and calls
    #: ``settimeout`` with it, and without it the default is no timeout at all: a
    #: client that sends ``Content-Length: 65536`` and then nothing held one
    #: thread of this ``ThreadingHTTPServer`` for as long as it kept the socket
    #: open, and this port is reachable from the internet.
    #:
    #: Twenty seconds, against a payload budget of 4 KiB that the platform's own
    #: caller sends in one segment. It bounds a request's reads and its
    #: keep-alive idle time, not the bootstrap: the work ``/run`` triggers runs on
    #: a thread of its own, so a slow registration cannot be cut off by this.
    timeout = 20

    #: The one body every answer that is not a hook's own carries. A dict rather
    #: than bytes so :meth:`_reply` serialises it the same way it serialises a
    #: hook's answer, and the two are indistinguishable on the wire.
    CONSTANT_REFUSAL = {"ok": False}

    def send_response(self, code: int, message: Optional[str] = None) -> None:  # noqa: A003
        self.send_response_only(code, message)

    def send_error(
        self, code: int, message: Optional[str] = None, explain: Optional[str] = None
    ) -> None:
        """The constant answer for everything the stdlib would describe.

        ``http.server`` reaches here for a method no ``do_*`` handles -- OPTIONS,
        PATCH, TRACE and anything else a scanner sends -- and for a request line or
        version it cannot parse, and its own reply is an HTML page naming the
        method, the path and the reason. That page is a description of the guest:
        it tells a caller which methods this listener implements and distinguishes
        a malformed request from an unimplemented one.

        So every one of them gets the same status and the same body as an unknown
        path. ``message`` and ``explain`` are read and dropped deliberately: they
        are the stdlib's description of the caller's own request, and nothing on
        this port describes anything back.

        A reply the stdlib cannot send at all -- a request line so broken that the
        version is unknown -- falls back to its own handling, which closes the
        connection. Nothing is disclosed by a connection that ends.
        """
        del code, message, explain
        try:
            self._reply(404, dict(self.CONSTANT_REFUSAL))
        except Exception:  # noqa: BLE001 - a half-written reply must still close
            self.close_connection = True

    def log_message(self, fmt: str, *args: Any) -> None:
        # Hook events are logged by their handlers through ``log``. An access log
        # here would let anyone who can reach the endpoint fill the guest's log
        # destination by choosing paths.
        return

    def _reply(self, code: int, body: "dict[str, Any]") -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return b""
        if length <= 0:
            return b""
        try:
            # The payload budget is 4 KiB; this cap is generous against it and
            # bounds what a stranger can make this process allocate.
            return self.rfile.read(min(length, 65536))
        except OSError:
            # A client that promised bytes and sent fewer, or none. :data:`timeout`
            # is what ends the wait; this is what turns the end of it into an
            # empty body rather than a traceback out of the handler thread. An
            # empty body does not decode, so no hook acts on it -- and ``/run``
            # does not count it as the one attempt it allows.
            self.close_connection = True
            return b""

    def _route(self) -> str:
        path = (self.path or "").split("?", 1)[0].rstrip("/")
        return path[len(HOOK_PREFIX) :].lstrip("/") if path.startswith(HOOK_PREFIX) else ""

    def do_GET(self) -> None:  # noqa: N802
        # No hook is a GET, so every GET -- including the front and dashboard
        # paths a probe will try -- gets the one constant answer.
        self._reply(404, dict(self.CONSTANT_REFUSAL))

    def do_POST(self) -> None:  # noqa: N802
        name = self._route()
        body = self._read_body()
        if name == "run":
            # The verdict is the guest's; the WIRE answer is the same constant
            # every other hook gets. A body carrying ``repeat`` would tell a
            # caller this listener cannot authenticate whether a crew is already
            # up -- the one fact the whole module is arranged not to disclose --
            # and a refused payload would be distinguishable from an accepted
            # one. What happened is in the log, which only the owner reads.
            handle_run(body)
            self._reply(200, {"ok": True})
            return
        if name in ("ready", "validate", "resume", "suspend"):
            # Fixed replies. ``ready`` answering 200 is what tells the build
            # service to snapshot, so a successful image build is itself proof
            # that this listener starts. ``resume`` and ``suspend`` have nothing
            # to do: the platform snapshots and restores the whole VM, and the
            # lane's own idle verdict is computed on the owner's gateway from the
            # crew's chat slots -- not here, where a guest cannot see whether the
            # owner is waiting on a turn.
            log(f"hook.{name}")
            self._reply(200, {"ok": True})
            return
        if name == "terminate":
            log("hook.terminate")
            # The hook listener cannot authenticate its caller -- that is why
            # every reply here is a constant -- so a terminate must not be able to
            # sever a RUNNING crew's only route in. Deregistering the SSM node is
            # exactly that: the owner reaches this crew through a port-forward over
            # that node and nothing else, so a spurious call would leave a crew
            # that serves, bills, and cannot be reached or torn down from the
            # dashboard.
            #
            # So the guest checks the one thing it can see for itself: whether the
            # crew is still serving. A VM the platform is really taking down has
            # had its processes stopped; one that is still answering turns is not
            # going anywhere.
            #
            # The cost of being wrong in this direction is a managed node left
            # registered for a VM that did go away -- which ``sweeper.py`` reports
            # and an operator can delete. The cost in the other direction is a
            # live crew nobody can reach again. Those are not comparable.
            #
            # "Not serving" is not enough on its own. This listener binds every
            # interface and cannot authenticate its caller, and between the node
            # registering and the supervisor coming up there is a wide window --
            # two Secrets Manager reads, the shared-credential wait, the bundle
            # install -- in which no supervisor process exists yet. A terminate
            # arriving in that window would find "not serving" true and hand back
            # the node of a crew that is about to serve, which is the live crew
            # nobody can reach again. So the guest also requires evidence that
            # the supervisor already ran.
            hand_back, reason = terminate_verdict()
            if hand_back:
                deregister_self("terminate-hook")
            else:
                log("hook.terminate.ignored", reason=reason)
            # 200 either way. The platform reads a non-200 as a hook that failed,
            # and this hook's job is to be answered; what it DID is the guest's
            # business and is in the log above.
            self._reply(200, {"ok": True})
            return
        self._reply(404, dict(self.CONSTANT_REFUSAL))

    def do_PUT(self) -> None:  # noqa: N802
        self._reply(404, dict(self.CONSTANT_REFUSAL))

    def do_DELETE(self) -> None:  # noqa: N802
        self._reply(404, dict(self.CONSTANT_REFUSAL))

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def on_signal(signum: int, _frame: Any) -> None:
    """Deregister, stop the children, and leave.

    The signal path and not the ``terminate`` hook is what makes deregistration
    reliable: a signal cannot be sent from the internet, and the hook's budget is
    undocumented. Both firing is harmless -- the deregister is idempotent.
    """
    log("signal", signum=signum)
    deregister_self(f"signal-{signum}")
    with _lock:
        for name, proc in _children.items():
            if proc.poll() is None:
                log("child.terminate", name=name)
                proc.terminate()
    time.sleep(2)
    os._exit(0)


class HookServer(ThreadingHTTPServer):
    """The hook listener's server, with its address-reuse choice STATED.

    ``SO_REUSEADDR`` is wanted here, and that is why it is written down rather
    than inherited. This process is PID 1 on a fixed port the image declared to
    ``CreateMicrovmImage``, so a restart has to rebind that exact port
    immediately: without reuse, a socket still in ``TIME_WAIT`` from the previous
    listener makes the bind fail and the VM answers no hook at all, which the
    platform reads as a guest that never came up.

    Inheriting the stdlib's ``True`` would reach the same behaviour by accident.
    A fixed-port listener that never names the flag is one nobody can tell apart
    from a listener that did not think about it, which is what the repo's own
    address-reuse rule is for -- and it is the opposite choice from the app
    backends, which bind loopback and must NOT silently take over a port.
    """

    allow_reuse_address = True


def main() -> None:
    ensure_state_dir()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    # 0.0.0.0 is required rather than chosen: the platform's hook caller is not
    # inside this container's loopback. It is also exactly why every reply above
    # is a constant. The crew's own listener binds 127.0.0.1.
    server = HookServer(("0.0.0.0", HOOK_PORT), Handler)  # noqa: S104
    log("hooks.listening", port=HOOK_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
