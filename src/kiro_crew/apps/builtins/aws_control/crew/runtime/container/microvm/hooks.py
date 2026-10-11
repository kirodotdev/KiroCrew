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
  get a 200; it was inert only because of the marker this module writes.
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
RUN_MARKER = f"{STATE_DIR}/run.done"

#: Held for the whole single-shot ``/run`` bootstrap, so the marker check and the
#: marker write are one step rather than two with the bootstrap in between.
_RUN_LOCK = threading.Lock()
PAYLOAD_SEEN = f"{STATE_DIR}/payload-seen.json"
BOOT_STATE = f"{STATE_DIR}/boot.json"

AGENT_BIN = _guest_path(USR, "bin", "amazon-ssm-agent")
#: Where the SSM agent publishes the managed node's role credentials. It writes
#: them under the HOME of the process that runs it, and this image sets
#: ``HOME=/var/lib/crew`` -- so the obvious ``/root/.aws/credentials`` is the
#: WRONG path here and would have stalled the boot at the credential wait once
#: registration began working. Both are checked, because the agent's own HOME is
#: the image's to change.
SHARED_CREDS_CANDIDATES = (
    os.path.join(os.environ.get("HOME", "/root"), ".aws", "credentials"),
    "/root/.aws/credentials",
    _guest_path(VAR, "lib", "crew", ".aws", "credentials"),
)
#: The login user the image's crew processes run as (``Dockerfile``: ``USER crew``).
CREW_USER = "crew"

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
    """
    env = dict(os.environ)
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
        # Not redundant: this environment starts from ``os.environ``, so the
        # image's value already arrives -- but a launch that passed its own value
        # would be in there too, and this line is what makes the guest's answer
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
    identity is the one the SSM agent publishes. The identity last of the two
    secrets, so a failure to read it is reported after the control secret that
    every other call needs.
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


def handle_run(body: bytes) -> "dict[str, Any]":
    """The real bootstrap, and the one hook that may happen only once.

    A repeat answers 200 and does nothing, in the same shape a first call
    answers: this hook cannot authenticate its caller, so telling a stranger
    their call was ignored would tell them a crew is already up.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    # Serialised, because the marker is written at the END of the bootstrap and
    # the check for it is at the start. Two requests arriving before it exists
    # both find it absent and both bootstrap: they reset the machine id, both
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

    ssm_block = payload.get("ssm") or {}
    region = str(payload.get("region") or _region)
    # What arrived, as shapes and lengths. "The payload arrived intact" is a
    # claim someone will want to check, and the activation code's length checks it
    # without recording it.
    write_state(
        PAYLOAD_SEEN,
        {
            "encoding": encoding,
            "tag": payload.get("tag"),
            "gen": payload.get("gen"),
            "region": region,
            "ssm_id": ssm_block.get("id"),
            "ssm_code_len": len(str(ssm_block.get("code") or "")),
            "secret_ref_present": bool(payload.get("secretRef")),
        },
    )

    log("machine_id.in_effect", machine_id=reset_machine_id())
    register_agent(str(ssm_block.get("id") or ""), str(ssm_block.get("code") or ""), region)
    spawn("ssm-agent", [AGENT_BIN])
    _managed_instance_id = read_managed_instance_id()
    log("run.registered", managed_instance_id=_managed_instance_id, payload_encoding=encoding)

    # The single-shot marker is written HERE, after the registration succeeded,
    # and not on the way in.
    #
    # Written first, a run that failed at the registration could never be retried
    # -- not by the platform and not by a relaunch against the same VM -- so a
    # transient failure became a permanently dark crew, which is exactly what
    # happened live. Written here, the only thing the marker refuses is a
    # SECOND SUCCESSFUL run, which is the property it exists for: resetting the
    # machine id and re-registering the agent is what would sever the owner's own
    # channel.
    #
    # Writing it late does not open a replay: a caller who reaches this route
    # without a version-1 payload is turned away above, and a valid payload
    # carries a live single-use activation code that only the platform was given.
    with open(RUN_MARKER, "w", encoding="ascii") as fh:
        fh.write(str(time.time()))

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

    def send_response(self, code: int, message: Optional[str] = None) -> None:  # noqa: A003
        self.send_response_only(code, message)

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
        # The payload budget is 4 KiB; this cap is generous against it and bounds
        # what a stranger can make this process allocate.
        return self.rfile.read(min(length, 65536)) if length > 0 else b""

    def _route(self) -> str:
        path = (self.path or "").split("?", 1)[0].rstrip("/")
        return path[len(HOOK_PREFIX) :].lstrip("/") if path.startswith(HOOK_PREFIX) else ""

    def do_GET(self) -> None:  # noqa: N802
        # No hook is a GET, so every GET -- including the front and dashboard
        # paths a probe will try -- gets the one constant answer.
        self._reply(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        name = self._route()
        body = self._read_body()
        if name == "run":
            self._reply(200, handle_run(body))
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
        self._reply(404, {"ok": False})

    def do_PUT(self) -> None:  # noqa: N802
        self._reply(404, {"ok": False})

    def do_DELETE(self) -> None:  # noqa: N802
        self._reply(404, {"ok": False})

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
    os.makedirs(STATE_DIR, exist_ok=True)
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
