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

import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Protocol

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cloud import sizes
from kiro_crew.config.loader import config_dir

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


def _default_steps() -> list:
    return [LaunchStep(key=k, label=lbl) for k, lbl in _STEP_LABELS]


@dataclass
class LaunchJob:
    """A single cloud-launch job. Persisted verbatim; the on-disk copy is truth."""

    id: str
    profile: str
    region: str
    size_key: str
    tag: str = ""
    status: str = PENDING
    steps: list = field(default_factory=_default_steps)
    instance_id: str = ""
    signin: Optional[SigninPrompt] = None
    signin_detected: bool = False
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

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
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LaunchJob":
        steps_raw = d.get("steps")
        steps = (
            [LaunchStep.from_dict(s) for s in steps_raw]
            if isinstance(steps_raw, list) and steps_raw
            else _default_steps()
        )
        signin_raw = d.get("signin")
        signin = SigninPrompt.from_dict(signin_raw) if isinstance(signin_raw, dict) else None
        return cls(
            id=str(d.get("id", "")),
            profile=str(d.get("profile", "")),
            region=str(d.get("region", "")),
            size_key=str(d.get("size_key", "")),
            tag=str(d.get("tag", "")),
            status=str(d.get("status", PENDING)),
            steps=steps,
            instance_id=str(d.get("instance_id", "")),
            signin=signin,
            signin_detected=bool(d.get("signin_detected", False)),
            error=str(d.get("error", "")),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
        )


# ── Disk-backed store ────────────────────────────────────────────────────────
_JOB_ID_OK = frozenset("abcdef0123456789-")


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


class LaunchJobStore:
    """One JSON file per job under ``<config_dir>/cloud/launch-jobs/``.

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
            self._root = config_dir() / "cloud" / "launch-jobs"
        self._lock = threading.RLock()
        # Job ids a worker in THIS process is driving. Ownership is what makes
        # reap_orphans() safe: without it, constructing a second store would
        # terminalize launches that are still running here.
        self._owned: set = set()

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, job_id: str) -> Path:
        # job ids are our own hex uuids; guard anyway so a caller-supplied id
        # can never escape the store dir.
        if not job_id or any(c not in _JOB_ID_OK for c in job_id.lower()):
            raise ValueError(f"invalid job id {job_id!r}")
        return self._root / f"{job_id}.json"

    def create(self, *, profile: str, region: str, size_key: str) -> LaunchJob:
        """Build + persist a fresh PENDING job. Validates the size key up front."""
        sizes.get_tier(size_key)  # raises KeyError with the valid set if unknown
        job = LaunchJob(id=_new_job_id(), profile=profile, region=region, size_key=size_key)
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
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read launch job %s: %s", path, e)
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
            for step in job.steps:
                if step.state == STEP_ACTIVE:
                    step.state = STEP_FAILED
            job.status = FAILED
            job.error = (
                "Interrupted — Kiro Crew restarted while this setup was running. "
                "The EC2 stack may still exist; check your crews before retrying."
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

    def wait(self, cancel: threading.Event) -> bool: ...
    def close(self) -> None: ...


class LaunchEngine(Protocol):
    """The AWS-touching operations a launch needs, injected for testability."""

    def preflight(self, profile: str, region: str) -> None: ...
    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str: ...
    def begin_signin(self, *, instance_id: str, profile: str, region: str) -> SigninHandle: ...
    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None: ...
    def teardown(self, *, tag: str, profile: str, region: str) -> None: ...


def _rollback_cancelled_stack(job: "LaunchJob", engine: "LaunchEngine") -> None:
    """Delete the stack a cancelled launch had already created.

    Cancellation is only observed *between* steps, and the instance is not added to
    the crew registry until the final step. So a cancel during provisioning — or
    during the sign-in wait, which is the likeliest moment for a human to give up —
    would otherwise leave a running, billing instance that never appears in the crew
    list: invisible to the very dashboard that offered the Cancel button, and
    removable only from the CLI or the AWS console.

    Best-effort and never raises: this runs while unwinding a cancellation, and the
    outcome is recorded on the job either way so the message the user reads is true.
    """
    try:
        engine.teardown(tag=job.tag, profile=job.profile, region=job.region)
    except Exception as exc:  # noqa: BLE001 - reported on the job, never propagated
        job.error = (
            f"Cancelled, but the EC2 stack {job.tag} could not be removed "
            f"automatically ({str(exc)[:200]}). Delete it from your crews — or with "
            "the CLI — so it stops billing."
        )
        logger.warning("Could not roll back stack %s after cancellation: %s", job.tag, exc)
        return
    job.step(STEP_PROVISION).detail = f"Removed {job.tag} after cancellation."


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

    try:
        # 1) Preflight
        _check_cancel()
        s = _activate(STEP_PREFLIGHT)
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
        handle = engine.begin_signin(
            instance_id=job.instance_id, profile=job.profile, region=job.region
        )
        try:
            if handle.already_logged_in:
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
                    else "Not signed in yet — finish it from the dashboard."
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
        store.save(job)

        job.status = DONE
        store.save(job)
        return job

    except LaunchCancelled:
        # Captured before the loop below rewrites the step states: anything past
        # PENDING means a CloudFormation stack may already exist for this tag.
        stack_may_exist = bool(job.tag) and job.step(STEP_PROVISION).state != STEP_PENDING
        for s in job.steps:
            if s.state == STEP_ACTIVE:
                s.state = STEP_SKIPPED
        job.signin = None
        job.status = CANCELLED
        if stack_may_exist:
            _rollback_cancelled_stack(job, engine)
        store.save(job)
        return job
    except Exception as exc:  # noqa: BLE001 - recorded on the job, never propagated
        active = next((s for s in job.steps if s.state == STEP_ACTIVE), None)
        if active is not None:
            active.state = STEP_FAILED
            active.detail = str(exc)[:400]
        job.error = str(exc)[:400]
        job.status = FAILED
        store.save(job)
        logger.info("launch job %s failed: %s", job.id, exc)
        return job
