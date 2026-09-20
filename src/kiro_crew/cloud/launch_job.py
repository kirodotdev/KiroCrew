"""Durable, non-interactive launch jobs for the cloud launcher in the dashboard.

The CLI wizard (:mod:`cloud.wizard`) is stdin-interactive and streams progress to
the terminal. The dashboard needs the *same* provisioning flow as a **background
job** whose progress — including the device-code sign-in prompt — is structured
state persisted to disk. That lets the UI render it, lets the user navigate away
and come back, and lets it survive a gateway restart (the on-disk state is the
source of truth).

This module owns three things and deliberately NOTHING else (no HTTP — that is
``handlers_cloud.py`` in the next stage — and no ``ui.*`` terminal printing):

* the job state model — :class:`LaunchJob`, :class:`LaunchStep`,
  :class:`SigninPrompt`, and the status/step-state constants;
* :class:`LaunchJobStore`, a disk-backed store (one JSON file per job, atomic
  writes, ``KIROCREW_HOME``-aware via ``config_dir()``);
* :func:`run_launch`, the orchestrator that drives the tested ``cloud/`` engine
  through the steps and persists state after every transition.

The engine is injected (:class:`LaunchEngine`) so the orchestration is unit
tested against a fake — no AWS calls in tests.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional, Protocol

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cloud import sizes
from kiro_crew.cloud.login_target import KiroLoginTarget, LoginTargetError
from kiro_crew.config.loader import config_dir
from kiro_crew.platform.interfaces import BUILTIN_PROVISIONER_ID

logger = logging.getLogger(__name__)

# ── Status + step-state constants ────────────────────────────────────────────
# Job status.
PENDING = "pending"
RUNNING = "running"
AWAITING_SIGNIN = "awaiting_signin"  # blocked on the human approving a device code
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL: frozenset = frozenset({DONE, FAILED, CANCELLED})

# Per-step state.
STEP_PENDING = "pending"
STEP_ACTIVE = "active"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

# The ordered steps a launch moves through. Kept small and user-facing; the
# provision step blocks until the box is healthy (the CloudFormation
# WaitCondition gates on the on-box install), so "create + install" is one step.
STEP_PREFLIGHT = "preflight"
STEP_PROVISION = "provision"
STEP_SIGNIN = "signin"
STEP_CONNECT = "connect"

_STEP_LABELS: tuple = (
    (STEP_PREFLIGHT, "Check your AWS setup"),
    (STEP_PROVISION, "Create the instance and install Kiro Crew"),
    (STEP_SIGNIN, "Sign in to Kiro"),
    (STEP_CONNECT, "Connect"),
)


def _resource_noun(job: "LaunchJob") -> str:
    """What a launch created, for the user-facing rollback and reap messages.

    The built-in lane creates a CloudFormation stack and the messages have always
    said so; a provisioner from the ``remote_provisioners`` seam creates whatever
    it creates (a DevSpace, a task), and calling that an "EC2 stack" would send the
    user to the wrong console to clean it up.
    """
    return "EC2 stack" if job.provider_id == BUILTIN_PROVISIONER_ID else "instance"


class LaunchCancelled(Exception):
    """Raised internally when a cancel is requested between steps, or inside the
    sign-in wait (the one step that polls the cancel flag). Provisioning does NOT
    observe it mid-flight: the CloudFormation deploy blocks until the stack settles,
    so a cancel during it is acted on when that returns — and the stack it created is
    then rolled back rather than abandoned."""


# ── State model ──────────────────────────────────────────────────────────────
@dataclass
class SigninPrompt:
    """The device-code sign-in the user must approve, exposed as state (not stdout)."""

    url: str = ""
    code: str = ""
    ports: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"url": self.url, "code": self.code, "ports": list(self.ports)}

    @classmethod
    def from_dict(cls, d: dict) -> "SigninPrompt":
        raw_ports = d.get("ports") or []
        ports = [int(p) for p in raw_ports if str(p).isdigit()]
        return cls(url=str(d.get("url", "")), code=str(d.get("code", "")), ports=ports)


@dataclass
class LaunchStep:
    """One user-visible step in the launch."""

    key: str
    label: str
    state: str = STEP_PENDING
    detail: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "state": self.state, "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict) -> "LaunchStep":
        return cls(
            key=str(d.get("key", "")),
            label=str(d.get("label", "")),
            state=str(d.get("state", STEP_PENDING)),
            detail=str(d.get("detail", "")),
        )


def default_steps(step_labels: Optional[Mapping[str, str]] = None) -> list:
    """The four fixed steps, with a provisioner's label overrides applied.

    The KEYS are the orchestration contract (``run_launch`` and the two rollback
    paths branch on them), so a provisioner may rename a step but not add or
    drop one. An override for an unknown key is ignored rather than raised: the
    descriptor is edition-supplied and a typo there must not make every launch
    fail before its first step.
    """
    labels = dict(step_labels or {})
    return [LaunchStep(key=k, label=labels.get(k) or lbl) for k, lbl in _STEP_LABELS]


@dataclass
class LaunchJob:
    """A single cloud-launch job. Persisted verbatim; the on-disk copy is truth."""

    id: str
    profile: str
    region: str
    size_key: str
    tag: str = ""
    status: str = PENDING
    steps: list = field(default_factory=default_steps)
    instance_id: str = ""
    signin: Optional[SigninPrompt] = None
    signin_detected: bool = False
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Which remote-instance provisioner drives this job (the CPP
    # ``remote_provisioners`` seam). A job file written before the seam existed
    # carries no key and loads as the built-in, which is what it was.
    provider_id: str = BUILTIN_PROVISIONER_ID
    # The Kiro identity the crew must sign in as. Persisted with the job so a
    # gateway restart while the device code is pending resumes the SAME sign-in
    # rather than a Builder ID one; a job file written before the field existed
    # loads as the default target, which is what it was. Never a credential.
    login_target: KiroLoginTarget = field(default_factory=KiroLoginTarget)
    #: True when :meth:`from_dict` could NOT parse the stored identity and
    #: substituted the default. Parse state, so it is deliberately NOT persisted
    #: by :meth:`to_dict`: what is on disk is the identity bytes, and whether they
    #: read is answered by reading them. Kept off ``error`` because the retry
    #: worker clears that field as routine state, which would drop the guard.
    target_unreadable: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def step(self, key: str) -> LaunchStep:
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(f"no step {key!r}")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "profile": self.profile,
            "region": self.region,
            "size_key": self.size_key,
            "tag": self.tag,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "instance_id": self.instance_id,
            "signin": self.signin.to_dict() if self.signin else None,
            "signin_detected": self.signin_detected,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "login_target": self.login_target.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LaunchJob":
        steps_raw = d.get("steps")
        steps = (
            [LaunchStep.from_dict(s) for s in steps_raw]
            if isinstance(steps_raw, list) and steps_raw
            else default_steps()
        )
        target_unreadable = False
        signin_raw = d.get("signin")
        signin = SigninPrompt.from_dict(signin_raw) if isinstance(signin_raw, dict) else None
        status = str(d.get("status", PENDING))
        error = str(d.get("error", ""))
        try:
            login_target = KiroLoginTarget.from_dict(d.get("login_target"))
        except LoginTargetError as exc:
            # The job named an identity this release cannot read. Resuming
            # it would sign the crew in as the default identity instead -- the
            # silent downgrade the target exists to prevent -- so the job fails
            # here, visibly, and a terminal job is never resumed.
            login_target = KiroLoginTarget()
            status = FAILED
            error = f"{UNREADABLE_TARGET_ERROR}: {exc}"
            target_unreadable = True
        return cls(
            id=str(d.get("id", "")),
            profile=str(d.get("profile", "")),
            region=str(d.get("region", "")),
            size_key=str(d.get("size_key", "")),
            tag=str(d.get("tag", "")),
            status=status,
            steps=steps,
            instance_id=str(d.get("instance_id", "")),
            signin=signin,
            signin_detected=bool(d.get("signin_detected", False)),
            error=error,
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
            provider_id=str(d.get("provider_id") or BUILTIN_PROVISIONER_ID),
            login_target=login_target,
            target_unreadable=target_unreadable,
        )


# ── Disk-backed store ────────────────────────────────────────────────────────
_JOB_ID_OK = frozenset("abcdef0123456789-")
# Job ids are generated as exactly this many hex chars; the store validates the
# exact length so a caller-supplied over-long id can't reach the filesystem.
_JOB_ID_LEN = 12


def _new_job_id() -> str:
    return uuid.uuid4().hex[:_JOB_ID_LEN]


class LaunchJobStore:
    """One JSON file per job under ``<config_dir>/run/cloud-launch-jobs/``.

    Under ``run/`` deliberately: that tree is on the sensitive-path floor
    (``security._SENSITIVE_HOME_DIRS``), so agent file tools cannot read it. A job
    that is AWAITING_SIGNIN persists the device-login URL and code — a credential
    that, from a plain ``config_dir()`` subtree, a prompt-injected same-UID agent
    could read and use to complete the sign-in. The gateway's own writers (this
    store) open the path directly and do NOT route through that gate, so persistence
    still works.

    Atomic writes (temp + ``os.replace``) so a crash mid-write never corrupts a
    job. A fresh ``LaunchJobStore`` reading the same root sees all persisted
    jobs — that is the durability the "navigate away / restart" requirement
    needs. ``root`` is injectable for tests.
    """

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is not None:
            self._root = root
        else:
            # config_dir() is called (not imported) late, so a KIROCREW_HOME
            # override — per-test isolation, a non-default home — is honoured.
            # Under run/ (a sensitive-path-floor tree) because a job awaiting
            # sign-in holds the device-login URL+code; see the class docstring.
            self._root = config_dir() / "run" / "cloud-launch-jobs"
        self._lock = threading.RLock()
        # Job ids a worker in THIS process is driving. Ownership is what makes
        # reap_orphans() safe: without it, constructing a second store would
        # terminalize launches that are still running here.
        self._owned: set = set()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, job_id: str) -> Path:
        # Job ids are our own fixed-length hex uuids; validate the EXACT shape so a
        # caller-supplied id (the {id} path param on launch GET/cancel/signin) can
        # neither escape the store dir NOR blow the filename-length limit. Without the
        # length bound a charset-valid but over-long id (e.g. 300 hex chars) reaches
        # Path.exists() and raises ENAMETOOLONG — an HTTP 500 instead of a clean 404
        # (get() maps this ValueError to None → the handler's not-found path).
        if (
            not job_id
            or len(job_id) != _JOB_ID_LEN
            or any(c not in _JOB_ID_OK for c in job_id.lower())
        ):
            raise ValueError(f"invalid job id {job_id!r}")
        return self._root / f"{job_id}.json"

    def create(
        self,
        *,
        profile: str,
        region: str,
        size_key: str,
        provider_id: str = BUILTIN_PROVISIONER_ID,
        step_labels: Optional[Mapping[str, str]] = None,
        login_target: Optional[KiroLoginTarget] = None,
    ) -> LaunchJob:
        """Build + persist a fresh PENDING job.

        The size key is validated up front ONLY for the built-in EC2 provisioner:
        ``sizes.py`` is the EC2 instance-type ladder, and another provisioner's
        ``size_key`` is that provisioner's own shape vocabulary (a DevSpace
        instance type, a Fargate cpu/memory pair), which its engine validates in
        ``provision``. Rejecting it here against the EC2 table would refuse every
        non-EC2 launch.

        ``login_target`` is the Kiro identity the crew signs in as; ``None`` is
        the default (Builder ID) target, exactly the pre-field behaviour.
        """
        if provider_id == BUILTIN_PROVISIONER_ID:
            sizes.get_tier(size_key)  # raises KeyError with the valid set if unknown
        job = LaunchJob(
            id=_new_job_id(),
            profile=profile,
            region=region,
            size_key=size_key,
            provider_id=provider_id,
            steps=default_steps(step_labels),
            login_target=login_target or KiroLoginTarget(),
        )
        # Claim ownership BEFORE the file exists. `reap_orphans` spares only jobs this
        # process owns, and it runs off the event loop: a reap already in flight can
        # list the job dir at any moment. Adopting in the worker instead leaves a
        # window between this save() and that adopt() where a concurrent reap sees a
        # non-terminal, unowned job and terminalizes a launch that is about to start —
        # which also clears the "already running" guard, so the user retries and pays
        # for a second stack.
        self.adopt(job.id)
        self.save(job)
        return job

    def save(self, job: LaunchJob) -> None:
        with self._lock:
            job.updated_at = time.time()
            # A parked job holds the device-code prompt (verification URL + user
            # code) until the human approves it. Under the default umask 022 that
            # would land as 0644 and any other local account could read the code
            # and redeem the sign-in, so the file is written 0600 and the
            # directory is owner-only.
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if platform_compat.IS_POSIX:
                # mode= only applies when mkdir creates the directory, so tighten
                # an existing one too (a store written by an earlier build is
                # 0755). POSIX-only: Windows ignores these bits, and the cloud
                # routes are POSIX-only anyway (see handlers_cloud._guard).
                #
                # The scanner's rule calls 0o700 "widely permissive" and suggests
                # 0o644, which is backwards for a DIRECTORY holding a short-lived
                # credential: 0o644 would drop owner-execute (making it
                # untraversable) and add world-read. 0o700 IS the restrictive mode,
                # which is why the finding is suppressed on the line below.
                try:
                    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
                    os.chmod(self._root, 0o700)
                except OSError:  # e.g. not the owner — the 0600 file mode still holds
                    pass
            atomic_write(self._path(job.id), json.dumps(job.to_dict(), indent=2), mode=0o600)

    def get(self, job_id: str) -> Optional[LaunchJob]:
        try:
            path = self._path(job_id)
        except ValueError:
            return None
        # Retry a transient read failure. A concurrent save() replaces this file via
        # os.replace; on Windows a read that collides with the in-progress replace
        # raises a sharing violation (an OSError), and without this list() would then
        # silently DROP an in-flight job — e.g. listing crews while a launch worker is
        # persisting its progress. POSIX replace is atomic and reads never error, so
        # this loop only ever fires on Windows. Bounded, then give up.
        raw = None
        for attempt in range(5):
            if not path.exists():
                return None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                break
            except OSError as e:
                if attempt == 4:
                    logger.warning("Failed to read launch job %s: %s", path, e)
                    return None
                time.sleep(0.02 * (attempt + 1))
            except json.JSONDecodeError as e:
                logger.warning("Failed to parse launch job %s: %s", path, e)
                return None
        if not isinstance(raw, dict):
            return None
        return LaunchJob.from_dict(raw)

    def list(self) -> list:
        if not self._root.exists():
            return []
        jobs: list = []
        for p in self._root.glob("*.json"):
            job = self.get(p.stem)
            if job is not None:
                jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def adopt(self, job_id: str) -> None:
        """Record that a worker in *this* process is driving ``job_id``.

        :meth:`reap_orphans` uses this to tell "running, and someone is on it"
        apart from "the file says running but its worker died with the process".
        """
        with self._lock:
            self._owned.add(job_id)

    def reap_orphans(self) -> List[str]:
        """Terminalize non-terminal jobs that no worker in this process owns.

        A launch runs on a daemon thread, so a gateway restart takes the worker
        with it while the on-disk job stays ``running`` forever: the UI shows a
        progress card that can never advance, and a cancel would find no thread
        to signal. Marking those jobs failed on load is what keeps the persisted
        state honest — the stack itself may well have finished in AWS, so the
        message points at the crew list rather than claiming nothing happened.

        Call once per process, after the store is constructed. Returns the ids
        reaped, for logging.
        """
        reaped: List[str] = []
        for job in self.list():
            if job.terminal or job.id in self._owned:
                continue
            if job.step(STEP_CONNECT).state == STEP_DONE:
                # Only a sign-in RETRY (:func:`run_signin_retry`) is non-terminal
                # after the connect step: the crew is created and registered, so
                # "the stack may still exist, check before retrying" would be wrong
                # and failing the whole job would hide a working crew behind a red
                # card. Park it back at done, unsigned, so the crew keeps its
                # "Sign in" action.
                signin_step = job.step(STEP_SIGNIN)
                if signin_step.state == STEP_FAILED:
                    # A VERIFIED refusal (the box holds a session for a different
                    # identity) was recorded on the step, and ``run_launch`` saves
                    # the connect step before it sets FAILED -- a restart in that
                    # window lands here. Parking DONE would hide the one failure
                    # whose recovery is an explicit logout, behind a generic
                    # "interrupted". Keep it failed, with its own words.
                    job.status = FAILED
                    job.error = signin_step.detail[:400] or job.error
                    self.save(job)
                    reaped.append(job.id)
                    continue
                for step in job.steps:
                    if step.state == STEP_ACTIVE:
                        step.state = STEP_SKIPPED
                job.status = DONE
                # The device code is KEPT. The remote login is `nohup`'d on the box
                # and outlives a gateway restart, so the code it is polling for is
                # still live -- and a live poller with no local record is one whose
                # approval signs the crew in with nothing tracking it. With the
                # record kept, this job lands in the stale-code shape: the card
                # offers "I approved it -- check now", the re-probe clears the badge
                # when the approval landed, and "Start over with a new code" replaces the login
                # (which kills the old poller) when it did not. A confirmed sign-in
                # has no code to keep; ``run_launch`` clears it before this point.
                #
                # Only mark unconfirmed a sign-in that was never confirmed.
                # ``run_launch`` saves the connect step BEFORE the terminal status,
                # so a restart landing in that window reaches here with
                # signin_detected already True -- and overwriting it would badge a
                # working crew "Needs sign-in" and offer a "new code" that signs it
                # out of the session it has.
                job.error = (
                    ""
                    if job.signin_detected
                    else (
                        "Interrupted — Kiro Crew restarted while the Kiro sign-in was "
                        "running. If you already approved the code, check now; "
                        "otherwise get a new one from the crew."
                    )
                )
                self.save(job)
                reaped.append(job.id)
                continue
            for step in job.steps:
                if step.state == STEP_ACTIVE:
                    step.state = STEP_FAILED
            job.status = FAILED
            job.error = (
                "Interrupted — Kiro Crew restarted while this setup was running. "
                f"The {_resource_noun(job)} may still exist; check your crews before retrying."
            )
            job.signin = None
            self.save(job)
            reaped.append(job.id)
        if reaped:
            logger.warning("Terminalized %d orphaned launch job(s): %s", len(reaped), reaped)
        return reaped

    def delete(self, job_id: str) -> bool:
        try:
            path = self._path(job_id)
        except ValueError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


# ── Engine injection ─────────────────────────────────────────────────────────
class SigninHandle(Protocol):
    """A started device-code sign-in (mirrors ``login.start_device_login``)."""

    already_logged_in: bool
    url: str
    code: str
    ports: list
    #: A VERIFIED refusal (e.g. the instance already holds a session for a
    #: different identity than the pinned target); empty when the sign-in
    #: started normally. Read with ``getattr`` so older handles still conform.
    error: str

    def wait(self, cancel: threading.Event) -> bool: ...
    def close(self) -> None: ...

    def abort(self) -> bool:
        """Stop the remote login outright, for a CANCELLED sign-in only.

        Distinct from :meth:`close`, which every path calls and which deliberately
        leaves an unconfirmed login polling so a preserved code stays finishable.

        REQUIRED, and each lane answers for itself. A lane that drives a real
        remote login returns whether it is CONFIRMED stopped. A lane with no login
        to stop (``FargateSigninHandle``: no browser, no device-code poller, no
        session) returns ``True`` and says why in its docstring. Nothing is
        inferred from a missing method: a handle that forgets to implement this
        raises into :func:`_abort_signin`, which records "not confirmed stopped"
        -- the safe answer -- rather than treating absence as a clean cancel.
        """
        ...


class LaunchEngine(Protocol):
    """The AWS-touching operations a launch needs, injected for testability."""

    def preflight(self, profile: str, region: str) -> None: ...
    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str: ...

    def begin_signin(
        self,
        *,
        instance_id: str,
        profile: str,
        region: str,
        login_target: "KiroLoginTarget | None" = None,
    ) -> SigninHandle: ...
    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None: ...
    def teardown(self, *, tag: str, profile: str, region: str) -> bool: ...


def _begin_signin_with_target(engine: "LaunchEngine", job: "LaunchJob") -> SigninHandle:
    """Call ``engine.begin_signin`` with the job's login target, versioned.

    ``login_target`` is a NEW keyword on the engine contract. The built-in
    engine takes it; a downstream provisioner written against the older
    three-argument shape does not, and must keep working for the default
    (Builder ID) target it always implemented. But when the job carries a
    NON-default target and the engine cannot receive it, silently calling the
    old shape would sign the crew in as the wrong identity — the exact defect
    this field exists to close — so that combination fails loud instead.

    The compatibility decision is :func:`_check_signin_target_supported`, which
    the runner applies at PREFLIGHT — before anything is provisioned or billed.
    By the time this is called the combination is known to be valid, so the
    ``RuntimeError`` below is a programming-error guard, never a runtime path.
    """
    if target_is_unreadable(job):
        # The stored identity could not be parsed, so `job.login_target` is the
        # substituted DEFAULT (Builder ID). Signing in with it would authenticate
        # an org-portal crew against the wrong account and overwrite the original
        # bytes on the next save. The routes refuse before they get here; this is
        # the funnel both workers pass through, so it refuses too rather than
        # trusting every future caller to remember.
        raise RuntimeError(
            "refusing to start a Kiro sign-in: this job's identity target could not be read, "
            "so the sign-in would use the default account instead"
        )
    if _engine_accepts_login_target(engine):
        return engine.begin_signin(
            instance_id=job.instance_id,
            profile=job.profile,
            region=job.region,
            login_target=job.login_target,
        )
    if not job.login_target.is_default:
        raise RuntimeError(_target_unsupported_message(engine, job))
    return engine.begin_signin(instance_id=job.instance_id, profile=job.profile, region=job.region)


def _engine_accepts_login_target(engine: "LaunchEngine") -> bool:
    try:
        return "login_target" in inspect.signature(engine.begin_signin).parameters
    except (TypeError, ValueError):
        return False


def _target_unsupported_message(engine: "LaunchEngine", job: "LaunchJob") -> str:
    return (
        f"provisioner {job.provider_id!r} cannot sign in as {job.login_target.describe()}: "
        "its LaunchEngine.begin_signin does not accept login_target. Launch with the "
        "default Builder ID identity, or use a provisioner that supports Identity Center."
    )


def _engine_login_target_refusal(engine: "LaunchEngine", target: "KiroLoginTarget") -> str:
    """The engine's own reason it cannot sign in as ``target``, or ``""``.

    Accepting the ``login_target`` keyword says an engine can RECEIVE a target,
    not that it can honour every one: a Fargate task is credentialed by the API
    key its container starts with and has no sign-in at all, so an Identity
    Center target has nothing to act on there. An engine that knows this about
    itself declares it through an optional ``login_target_refusal(target)``
    method returning a non-empty reason; the built-in EC2 engine, which honours
    every target, has none. Optional so that a downstream engine written before
    this hook keeps its keyword-based contract unchanged.
    """
    probe = getattr(engine, "login_target_refusal", None)
    if probe is None:
        return ""
    try:
        return str(probe(target) or "")
    except Exception:  # pragma: no cover - defensive; a broken probe must not hide the launch
        logger.warning(
            "login_target_refusal probe failed; treating target as accepted", exc_info=True
        )
        return ""


def _check_signin_target_supported(engine: "LaunchEngine", job: "LaunchJob") -> None:
    """Refuse, at preflight, a non-default target the engine cannot honour.

    Provisioning bills an instance the moment it succeeds; discovering only at
    the sign-in step that the engine cannot honour the requested identity would
    strand that instance — provisioned, running, unregistered, and outside
    every teardown arm (which fire only on a provision-step failure). Deciding
    here means the job fails before any resource exists.

    Two engine shapes fail here. One written against the three-keyword
    ``begin_signin`` cannot receive a target at all. One that receives the
    keyword but declares, through ``login_target_refusal``, that this target has
    nothing to act on (see :func:`_engine_login_target_refusal`) is refused with
    its own reason.
    """
    if job.login_target.is_default:
        return
    if not _engine_accepts_login_target(engine):
        raise RuntimeError(_target_unsupported_message(engine, job))
    reason = _engine_login_target_refusal(engine, job.login_target)
    if reason:
        raise RuntimeError(
            f"provisioner {job.provider_id!r} cannot sign in as "
            f"{job.login_target.describe()}: {reason}"
        )


#: Prefix of ``job.error`` when the persisted identity target could not be read.
#:
#: The substituted target is the DEFAULT (Builder ID), so anything that resumes
#: such a job would sign the crew in as the wrong identity and overwrite the
#: original bytes on the next save. The route that could do that
#: (:func:`~kiro_crew.dashboard.handlers_cloud.api_cloud_launch_signin_restart`)
#: refuses on this marker rather than on the bare ``FAILED`` status, because
#: FAILED is an ordinary outcome the restart is allowed to act on.
UNREADABLE_TARGET_ERROR = "persisted Kiro identity target is unreadable"


def target_is_unreadable(job: "LaunchJob") -> bool:
    """Whether *job*'s stored identity could not be parsed by this release.

    Reads the ``target_unreadable`` flag :meth:`LaunchJob.from_dict` sets at parse
    time. The error text is a fallback for a job whose flag was lost across a
    boundary that only carries the persisted fields: the flag is authoritative,
    and the text alone was not enough, because :func:`run_signin_retry` clears
    ``job.error`` as routine state before the sign-in starts.
    """
    return bool(getattr(job, "target_unreadable", False)) or job.error.startswith(
        UNREADABLE_TARGET_ERROR
    )


#: Recorded on a cancelled job whose remote login could not be confirmed
#: stopped. The cancel still happened — but a login left polling can complete
#: minutes later, so the only person who can check the box is told instead of
#: being shown a launch that looks fully torn down.
ABORT_UNCONFIRMED_NOTE = (
    "Cancelled, but the Kiro sign-in on the instance was NOT confirmed stopped — "
    "if that device code is approved it may still sign this crew in. Check the "
    "instance, or delete it."
)


def mark_signed_in(job: "LaunchJob", detail: str = "Signed in.") -> None:
    """Record a CONFIRMED sign-in consistently, wherever it was confirmed.

    Three paths confirm one: the retry's own wait, its already-signed-in answer,
    and the dashboard's re-probe of a preserved code. Setting ``signin_detected``
    alone leaves the rest of the job saying the opposite -- an "Interrupted"
    error from a restart, a connect step still reading "Finish the Kiro sign-in
    before connecting." A card that says signed
    in and not signed in at once is the state this feature exists to remove, so
    the normalisation lives in one place.
    """
    job.signin_detected = True
    job.signin = None
    # Every error EXCEPT the one that says we could not read the identity. That
    # marker is what keeps the sign-in paths off this job (see
    # :func:`target_is_unreadable`); clearing it here would let the next caller
    # act on the substituted default target, which is the downgrade the marker
    # exists to prevent. A confirmed sign-in does not make the stored bytes
    # readable.
    if not target_is_unreadable(job):
        job.error = ""
    s = job.step(STEP_SIGNIN)
    s.state = STEP_DONE
    s.detail = detail
    c = job.step(STEP_CONNECT)
    if c.state == STEP_DONE:
        c.detail = "Added to your instances."


def _abort_signin(handle: SigninHandle, job: "Optional[LaunchJob]" = None) -> bool:
    """Stop a CANCELLED sign-in's remote login, when the handle can.

    Distinct from ``handle.close()``, which every path calls: an *unconfirmed*
    sign-in deliberately leaves its remote login polling, because that is what
    makes the preserved device code still finishable from the dashboard. Only a
    cancel means the opposite — that no later approval may land.

    Two outcomes, and the lane decides which -- never this function by inference:

    * ``abort()`` returns ``True`` — confirmed stopped, or (``FargateSigninHandle``)
      confirmed there was never a login to stop. Nothing recorded.
    * Anything else, including a raise or a missing method — NOT confirmed. Given
      *job*, records :data:`ABORT_UNCONFIRMED_NOTE` on ``job.error`` (only when
      nothing more urgent is already there), because a login that may still be
      polling is the one outcome an operator has to act on.

    Never raises: the caller is already unwinding a cancellation.
    """
    try:
        # Only True confirms. `None` is not evidence the login died, and a handle
        # that answers nothing has told us nothing. A handle with no `abort` at
        # all raises AttributeError here and is recorded as not confirmed: absence
        # of a stopper is not evidence the login stopped.
        stopped = handle.abort() is True
    except Exception:  # noqa: BLE001 - cleanup on the cancel path
        logger.warning("could not abort the sign-in after cancellation", exc_info=True)
        stopped = False
    if not stopped:
        logger.warning("the remote Kiro login was not confirmed stopped after cancellation")
        if job is not None and not job.error:
            job.error = ABORT_UNCONFIRMED_NOTE
    return stopped


def _rollback_cancelled_stack(
    job: "LaunchJob", store: "LaunchJobStore", engine: "LaunchEngine"
) -> None:
    """Delete the stack a cancelled launch had already created.

    Cancellation is only observed *between* steps, and the instance is not added to
    the crew registry until the final step. So a cancel during provisioning — or
    during the sign-in wait, which is the likeliest moment for a human to give up —
    would otherwise leave a running, billing instance that never appears in the crew
    list: invisible to the very dashboard that offered the Cancel button, and
    removable only from the CLI or the AWS console.

    Ack, then confirm. The cancelled state is persisted BEFORE the delete is awaited
    so the card stops saying "running" immediately, and the outcome is only written
    as removed once AWS confirms — an accepted delete request that later reaches
    DELETE_FAILED must not be reported as "Removed", or the user believes the
    billing stopped when the instance is still up.

    Best-effort and never raises: this runs while unwinding a cancellation.
    """
    step = job.step(STEP_PROVISION)
    step.detail = f"Removing {job.tag}…"
    store.save(job)
    # Anything already recorded (e.g. an unconfirmed abort of the remote login)
    # is kept as a prefix rather than overwritten.
    prior = f"{job.error} " if job.error else ""
    try:
        confirmed = engine.teardown(tag=job.tag, profile=job.profile, region=job.region)
    except Exception as exc:  # noqa: BLE001 - reported on the job, never propagated
        job.error = (
            f"{prior}Cancelled, but the {_resource_noun(job)} {job.tag} could not be removed "
            f"automatically ({str(exc)[:200]}). Delete it from your crews — or with "
            "the CLI — so it stops billing."
        )
        logger.warning("Could not roll back stack %s after cancellation: %s", job.tag, exc)
        return
    if confirmed:
        step.detail = f"Removed {job.tag} after cancellation."
        if job.error == ABORT_UNCONFIRMED_NOTE:
            # The note warns that a login left polling could still sign the crew
            # in. The instance it would poll from is confirmed gone, so there is
            # no poller and nothing to check: keep the card from telling the user
            # to inspect a machine that is gone.
            job.error = ""
        return
    job.error = (
        f"{prior}Cancelled, and the delete of {_resource_noun(job)} {job.tag} was requested but "
        "did NOT confirm (it may be DELETE_FAILED). Check your crews — it may still be "
        "running and billing."
    )
    logger.warning("Rollback of %s did not confirm deletion", job.tag)


def _rollback_failed_provision(
    job: "LaunchJob", store: "LaunchJobStore", engine: "LaunchEngine"
) -> None:
    """Best-effort delete of a stack left behind by a failed provision step.

    ``ec2.deploy`` creates the CloudFormation stack and then blocks until it is
    healthy, so a transient failure *after* the stack exists (e.g. a post-create
    ``DescribeStacks`` error) raises out of provisioning and marks the job FAILED —
    but the instance is running and was never registered, so it bills invisibly,
    exactly like a cancelled launch would. Roll it back, mirroring
    :func:`_rollback_cancelled_stack`. The caller scopes this to a STEP_PROVISION
    failure only: a later-step failure means the crew IS created (register even
    names it for manual recovery), so it must not be torn down here.

    Best-effort and never raises; it augments the recorded failure message rather
    than replacing it, so the original error stays visible. Safe when no stack was
    created — deleting an absent stack is a no-op.
    """
    base = job.error or "Setup failed."
    try:
        confirmed = engine.teardown(tag=job.tag, profile=job.profile, region=job.region)
    except Exception as exc:  # noqa: BLE001 - reported on the job, never propagated
        job.error = (
            f"{base} The {_resource_noun(job)} {job.tag} could not be removed automatically "
            f"({str(exc)[:150]}). Delete it from your crews — or with the CLI — so it "
            "stops billing."
        )
        logger.warning("Could not roll back stack %s after provision failure: %s", job.tag, exc)
        return
    if confirmed:
        job.error = f"{base} The {_resource_noun(job)} {job.tag} was removed so it stops billing."
        return
    job.error = (
        f"{base} The delete of {_resource_noun(job)} {job.tag} was requested but did NOT confirm "
        "(it may be DELETE_FAILED). Check your crews — it may still be running and billing."
    )
    logger.warning("Rollback of %s after provision failure did not confirm", job.tag)


def _new_tag() -> str:
    return f"kc-{secrets.token_hex(3)}"


# ── Orchestrator ─────────────────────────────────────────────────────────────
def run_launch(
    job: LaunchJob,
    store: LaunchJobStore,
    engine: LaunchEngine,
    *,
    cancel: Optional[threading.Event] = None,
) -> LaunchJob:
    """Drive ``job`` through the launch steps, persisting after each transition.

    Blocking (runs on a worker thread in production). Every state change is saved
    before returning, so a reader — or a restart — always sees the current step,
    and the device-code prompt is visible while the job is ``AWAITING_SIGNIN``.
    Never raises for an expected failure: a failed step sets ``status=FAILED``
    with the error recorded on that step; a cancel sets ``status=CANCELLED``.
    """
    cancel = cancel or threading.Event()
    if job.terminal:
        return job

    def _check_cancel() -> None:
        if cancel.is_set():
            raise LaunchCancelled()

    def _activate(key: str) -> LaunchStep:
        s = job.step(key)
        s.state = STEP_ACTIVE
        job.status = RUNNING
        store.save(job)
        return s

    job.status = RUNNING
    if not job.tag:
        job.tag = _new_tag()
    store.save(job)

    # Held at function scope for the cancel path below: the sign-in's own block
    # closes the handle, which deliberately leaves the remote login polling so an
    # unconfirmed code stays finishable. A CANCEL means the opposite.
    started: Optional[SigninHandle] = None

    try:
        # 1) Preflight — includes the identity/engine compatibility check, so a
        #    target the engine cannot honour fails HERE, before any resource is
        #    provisioned or billed (a sign-in-step refusal would strand it).
        _check_cancel()
        s = _activate(STEP_PREFLIGHT)
        _check_signin_target_supported(engine, job)
        engine.preflight(job.profile, job.region)
        s.state = STEP_DONE
        store.save(job)

        # 2) Provision (create instance + install; blocks until healthy)
        _check_cancel()
        s = _activate(STEP_PROVISION)
        job.instance_id = engine.provision(
            tag=job.tag, size_key=job.size_key, profile=job.profile, region=job.region
        )
        s.detail = job.instance_id
        s.state = STEP_DONE
        store.save(job)

        # 3) Sign in to Kiro (device code exposed as state while awaiting)
        _check_cancel()
        s = _activate(STEP_SIGNIN)
        handle = _begin_signin_with_target(engine, job)
        started = handle
        # A sign-in that was REFUSED (verified identity mismatch on a reused
        # instance) is recorded here and raised only after register(): failing
        # before registration would strand a provisioned, billing instance that
        # never appears in the crew list, and the recovery the message names
        # (``cloud logout`` on the instance) needs the crew to be visible.
        signin_error = str(getattr(handle, "error", "") or "")
        try:
            if signin_error:
                job.signin_detected = False
                s.state = STEP_FAILED
                s.detail = signin_error
                store.save(job)
            elif handle.already_logged_in:
                job.signin_detected = True
                s.state = STEP_DONE
                s.detail = "Already signed in."
                store.save(job)
            elif handle.url:
                job.signin = SigninPrompt(
                    url=handle.url, code=handle.code, ports=list(handle.ports or [])
                )
                job.status = AWAITING_SIGNIN
                store.save(job)  # UI now shows the URL + code
                signed = handle.wait(cancel)
                _check_cancel()
                # Keep the prompt when the wait ran out: the code is still valid
                # for a while, the user may be mid-approval, and the message below
                # tells them to finish from the dashboard — which is only possible
                # if the dashboard still has the URL and code to show. Clearing it
                # here is what made "finish it from the dashboard" a dead end.
                if signed:
                    job.signin = None
                job.signin_detected = signed
                job.status = RUNNING
                s.state = STEP_DONE if signed else STEP_SKIPPED
                s.detail = (
                    "Signed in."
                    if signed
                    else "Not signed in yet — finish it in the sign-in box below."
                )
                store.save(job)
            else:
                # No device-code URL (e.g. social-login) — do not block; surface it.
                job.signin_detected = False
                s.state = STEP_SKIPPED
                s.detail = "Sign in from the dashboard once it opens."
                store.save(job)
        finally:
            try:
                handle.close()
            except Exception:  # pragma: no cover - best effort
                logger.info("sign-in handle close failed (non-fatal)", exc_info=True)

        # 4) Register in the Instances hub so it appears under "Your crews"
        _check_cancel()
        s = _activate(STEP_CONNECT)
        engine.register(
            instance_id=job.instance_id, tag=job.tag, profile=job.profile, region=job.region
        )
        s.state = STEP_DONE
        # The step ran -- the instance is registered -- but a green check with no
        # words beside "Connect" reads as ready to use. Say what is still owed
        # when the sign-in did not confirm; the card's icon shows a waiting key in
        # that case and this is the sentence under it.
        s.detail = (
            "Added to your instances."
            if job.signin_detected
            else "Added to your instances. Finish the Kiro sign-in before connecting."
        )
        store.save(job)

        if signin_error:
            # Registered (visible, recoverable) but NOT done: the crew is running
            # under an identity the launch did not ask for, and only an explicit
            # logout on the instance fixes that. DONE here would report success.
            job.error = signin_error[:400]
            job.status = FAILED
            store.save(job)
            return job

        job.status = DONE
        store.save(job)
        return job

    except LaunchCancelled:
        # Stop the remote login BEFORE the step rewrite and BEFORE the rollback,
        # and regardless of whether the rollback will confirm. The teardown below
        # can end in DELETE_FAILED — a state this code reports rather than rules
        # out — and an instance that survives with a login still polling would
        # authenticate the crew minutes after the owner cancelled. Ordered first
        # because the rollback takes minutes and the browser tab is already open.
        if started is not None:
            _abort_signin(started, job)
        # Captured before the loop below rewrites the step states: anything past
        # PENDING means a CloudFormation stack may already exist for this tag.
        stack_may_exist = bool(job.tag) and job.step(STEP_PROVISION).state != STEP_PENDING
        for s in job.steps:
            if s.state == STEP_ACTIVE:
                s.state = STEP_SKIPPED
        job.signin = None
        job.status = CANCELLED
        if stack_may_exist:
            _rollback_cancelled_stack(job, store, engine)
        store.save(job)
        return job
    except Exception as exc:  # noqa: BLE001 - recorded on the job, never propagated
        active = next((s for s in job.steps if s.state == STEP_ACTIVE), None)
        if active is not None:
            active.state = STEP_FAILED
            active.detail = str(exc)[:400]
        job.error = str(exc)[:400]
        job.status = FAILED
        # A failure DURING provisioning can still have created the CloudFormation
        # stack (deploy creates it, then blocks — a transient post-create error
        # raises with the instance already running), leaving a billing stack that was
        # never registered and so never appears in the crew list. Roll it back, like
        # the cancel path. Scoped to a STEP_PROVISION failure: a later-step failure
        # means the crew IS created (register names it for recovery), so it stays.
        if job.step(STEP_PROVISION).state == STEP_FAILED and job.tag:
            _rollback_failed_provision(job, store, engine)
        store.save(job)
        logger.info("launch job %s failed: %s", job.id, exc)
        return job


def run_signin_retry(
    job: LaunchJob,
    store: LaunchJobStore,
    engine: LaunchEngine,
    *,
    cancel: Optional[threading.Event] = None,
) -> LaunchJob:
    """Re-run ONLY the sign-in step on a launch whose crew exists but is unsigned.

    This is the dashboard's "Start sign-in" / "Start over with a new code" action. The
    launch itself finished (the instance is created and registered), so **no
    other step is touched and nothing is ever re-provisioned**; the job returns
    to ``DONE`` whatever happens here. What changes is ``signin`` (a fresh device
    code, or none) and ``signin_detected``. While the new code is pending the job
    is ``AWAITING_SIGNIN`` so the UI polls it live, exactly as during setup.

    The job's persisted ``login_target`` is reused, via the same
    :func:`_begin_signin_with_target` the launch uses: an Identity Center crew
    gets an Identity Center code, not the Builder ID prompt that stranded it in
    the first place.

    Never raises for an expected failure — a broken engine is recorded on the
    step — except for the programming error of calling it on a job that has no
    instance to sign in on (``ValueError``).
    """
    cancel = cancel or threading.Event()
    if not job.instance_id:
        raise ValueError("this launch has no instance to sign in on")
    s = job.step(STEP_SIGNIN)
    s.state = STEP_ACTIVE
    if target_is_unreadable(job):
        # Before any state is touched: this job's stored identity could not be
        # parsed, so `job.login_target` is the substituted default and a sign-in
        # would authenticate the crew as the wrong account. The routes refuse
        # first, and `_begin_signin_with_target` refuses last; refusing here as
        # well means the worker never clears `job.error` (the marker's text
        # fallback) on a job it is not allowed to drive.
        s.state = STEP_FAILED
        s.detail = "This crew's Kiro identity could not be read, so no sign-in was started."
        job.status = FAILED
        job.error = (
            f"{UNREADABLE_TARGET_ERROR}: refusing to sign in with the default identity. "
            "Use a release that can read it, or delete this crew and launch again."
        )
        store.save(job)
        return job
    s.detail = ""
    job.error = ""
    # The previous code is KEPT until the box has replaced the login. Only
    # `begin_signin` (`start_device_login(replace_existing=True)`) kills the old
    # poller; until that call lands, the old code is still live on the instance.
    # Dropping the local record first would leave a live poller nothing tracks --
    # an approval of that stale code would then sign the crew in silently. The
    # card shows a spinner while this step is active, so the old code is not on
    # screen beside the new one.
    job.signin_detected = False
    job.status = RUNNING
    store.save(job)
    # Held separately from ``handle`` so the cancel path below can reach it
    # without making every use inside the try optional.
    started: Optional[SigninHandle] = None
    try:
        handle = _begin_signin_with_target(engine, job)
        started = handle
        # A VERIFIED refusal (the box holds a session for a DIFFERENT identity
        # than the target). Recorded as a failed step and a job error, exactly as
        # ``run_launch`` does — filing it as the benign "no device code" case
        # would report the retry as finished while the crew serves chats under
        # the wrong account.
        signin_error = str(getattr(handle, "error", "") or "")
        # A returned handle is NOT proof the box replaced the login: the real
        # engine swallows an SSM failure into an EMPTY handle (no code, not signed
        # in, no error), and in that case the remote `pkill` never ran and the old
        # poller is still live. Drop the old code only on evidence the box
        # answered -- a new code, an already-signed-in answer, or a verified
        # refusal. An empty handle keeps the old record, so the code stays tracked.
        box_answered = bool(signin_error or handle.already_logged_in or handle.url)
        if box_answered:
            job.signin = None
        try:
            if signin_error:
                s.state = STEP_FAILED
                s.detail = signin_error
                job.error = signin_error[:400]
            elif handle.already_logged_in:
                mark_signed_in(job, "Already signed in.")
            elif handle.url:
                job.signin = SigninPrompt(
                    url=handle.url, code=handle.code, ports=list(handle.ports or [])
                )
                job.status = AWAITING_SIGNIN
                store.save(job)  # UI now shows the new URL + code
                signed = handle.wait(cancel)
                if cancel.is_set() and not signed:
                    raise LaunchCancelled()
                # `and not signed`: the wait's final poll is a seconds-wide SSM
                # round trip, so a click can land inside it. If that poll came back
                # SIGNED the box already holds the session -- there is no login left
                # to abort, and reporting CANCELLED would leave a signed-in crew
                # badged "Needs sign-in" with Connect held. The cancel is honoured
                # for every outcome where the sign-in did NOT complete.
                # Keep the prompt when the wait ran out: the code is still valid
                # for a while and the message below tells the user to finish it
                # from the dashboard, which needs the URL and code to still be there.
                job.signin_detected = signed
                if signed:
                    mark_signed_in(job)
                else:
                    s.state = STEP_SKIPPED
                    s.detail = "Not signed in yet — finish it in the sign-in box below."
            else:
                # No code, not signed in, no refusal: the box was not reached. The
                # previous code (if any) is still on the job and still live.
                if cancel.is_set():
                    # The owner cancelled while the box was being asked. Nothing new
                    # is known to run, but the OLD poller may still be: a cancel means
                    # no later approval may land, so the login on the box must be
                    # stopped, not merely left "still valid if you have it".
                    raise LaunchCancelled()
                s.state = STEP_SKIPPED
                s.detail = (
                    "Could not reach the instance to start the sign-in. "
                    "The previous code is still valid if you have it; otherwise try again."
                    if job.signin is not None
                    else "Sign in from the box below once it opens."
                )
        finally:
            try:
                handle.close()
            except Exception:  # pragma: no cover - best effort
                logger.info("sign-in handle close failed (non-fatal)", exc_info=True)
    except LaunchCancelled:
        if started is not None:
            # `begin_signin` returned: either a new login was started (the box
            # replaced the old one, so the new poller is the only one alive) or
            # the box did not answer (the OLD poller may still be alive). In both
            # cases the login on the box must be stopped -- dropping the code
            # locally is not cancelling, it is in a browser.
            stopped = _abort_signin(started, job)
            s.state = STEP_SKIPPED
            s.detail = "Sign-in cancelled." if stopped else ABORT_UNCONFIRMED_NOTE
            job.signin = None
        else:
            # Cancelled before the box was reached: nothing new was started, the
            # OLD poller is still live, and `job.signin` was never cleared (that
            # happens only once `begin_signin` returns) -- so the preserved code
            # stays tracked and re-probed exactly as before the retry.
            s.state = STEP_SKIPPED
            s.detail = "Sign-in cancelled."
    except Exception as exc:  # noqa: BLE001 - recorded on the job, never propagated
        # The crew exists and is registered, so a failure here must leave it
        # exactly as it was: DONE, visible, and still offering "Sign in". That
        # includes the preserved code: `job.signin` is cleared only after
        # `begin_signin` returns, so a failure before then leaves the record of a
        # poller that is still live on the box.
        s.state = STEP_SKIPPED
        s.detail = f"Could not start the Kiro sign-in: {str(exc)[:300]}"
        job.error = str(exc)[:400]
        logger.info("sign-in retry for launch job %s failed: %s", job.id, exc)
    job.status = DONE
    store.save(job)
    return job
