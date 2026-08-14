"""The real :class:`~kiro_crew.cloud.launch_job.LaunchEngine` — binds the launch
job's abstract steps to the tested ``cloud/`` engine functions.

Kept separate from the HTTP handlers so it can be unit tested by monkeypatching
the ``cloud`` modules, with no aiohttp and no live AWS. It adds **no** new AWS
logic: ``preflight`` reuses ``iam.reachability_check``, ``provision`` reuses
``ec2.deploy``, sign-in reuses ``login.*``, and ``register`` reuses
``connect.register_instance`` — the same calls the CLI wizard already makes.
"""

from __future__ import annotations

import logging
import threading

from kiro_crew.cloud import connect as connect_mod
from kiro_crew.cloud import ec2, iam, login, sizes
from kiro_crew.cloud import source as source_mod
from kiro_crew.cloud.aws import AWSError
from kiro_crew.instances.port_allocator import PortAllocator
from kiro_crew.instances.registry import InstancesRegistry

logger = logging.getLogger(__name__)

# Matches login.wait_until_logged_in's own default budget (30 × ~5s ≈ 150s), but
# spent one attempt at a time so cancellation is observed between attempts.
_SIGNIN_ATTEMPTS = 30


class _RealSigninHandle:
    """Wraps ``login.start_device_login`` as a launch-job SigninHandle."""

    def __init__(self, instance_id: str, profile: str, region: str) -> None:
        self._iid = instance_id
        self._profile = profile
        self._region = region
        try:
            prompt = login.start_device_login(
                instance_id, profile, region, open_browser=False
            )
        except Exception:  # noqa: BLE001 - see below
            # start_device_login shells out to SSM. A transient failure here used to
            # raise straight out of this constructor -> begin_signin -> the launch
            # worker, failing the job BEFORE register() ran — leaving a provisioned,
            # billing instance that was never registered and so never appeared in the
            # crew list. That is the same stranding wait() was already hardened
            # against; this constructor was the one remaining path that could still
            # cause it. Continue with an empty, unconfirmed prompt so the launch still
            # reaches register(): the crew becomes visible and the user finishes
            # sign-in from the dashboard (or deletes it) rather than paying for an
            # invisible instance. Broad on purpose — an exec/sandbox failure arrives
            # as an unrelated exception type, and every mode means the same thing here.
            logger.info(
                "could not start Kiro sign-in for %s; continuing unconfirmed",
                instance_id, exc_info=True,
            )
            prompt = None
        self._prompt = prompt
        self.already_logged_in = bool(getattr(prompt, "already_logged_in", False))
        self.url = str(getattr(prompt, "url", "") or "")
        self.code = str(getattr(prompt, "code", "") or "")
        self.ports = list(getattr(prompt, "ports", []) or [])

    def wait(self, cancel: threading.Event) -> bool:
        if cancel.is_set():
            return False
        try:
            # Resuming the background login is INSIDE this handler on purpose. A
            # transient SSM failure here used to propagate out of wait(), fail the
            # whole job, and return before STEP_CONNECT — leaving a provisioned,
            # billing instance that was never registered and so never appeared in the
            # crew list. "We could not confirm sign-in" is the honest outcome, and it
            # lets the launch finish registering so the user can see the crew and
            # complete sign-in from the dashboard (the device code is preserved).
            login.resume_login_daemon(self._iid, self._profile, self._region)
            # Poll one attempt at a time so a cancel lands within ~5s. Calling
            # wait_until_logged_in() with its default 30 attempts would ignore
            # cancellation for up to ~150s, leaving the UI showing "cancelling"
            # while the job keeps waiting on a device code nobody will approve.
            for _ in range(_SIGNIN_ATTEMPTS):
                if cancel.is_set():
                    return False
                if login.wait_until_logged_in(
                    self._iid, self._profile, self._region, attempts=1
                ):
                    return True
            return False
        except Exception:  # noqa: BLE001 - see below
            # Deliberately broad, not just AWSError: these helpers shell out to the
            # AWS CLI, so an exec/sandbox failure arrives as an unrelated exception
            # type. Every failure mode means the same thing to the caller — sign-in
            # is unconfirmed — and none of them justifies stranding a paid instance
            # outside the crew list.
            logger.info("could not confirm Kiro sign-in for %s", self._iid, exc_info=True)
            return False

    def close(self) -> None:
        if self._prompt is None:
            return
        try:
            self._prompt.close()
        except Exception:  # pragma: no cover - best effort
            logger.info("device-login prompt close failed (non-fatal)", exc_info=True)


class RealLaunchEngine:
    """Drives a launch against the user's AWS account via the ``cloud/`` engine."""

    def __init__(self) -> None:
        # Settled once per launch, then reused for BOTH ends: the crew's gateway
        # starts on it (KIROCREW_PORT via the stack) and the registry records it as
        # remote_port, which the tunnel mirrors locally. See _allocate_port.
        self._port = 0

    def _allocate_port(self) -> int:
        """Pick a gateway port this crew can actually be reached on.

        The tunnel forces ``local_port == remote_port`` so the embedded dashboard's
        Origin matches what the remote gateway trusts, and it hard-fails rather than
        falling back when that port is busy. Registering every crew on the default
        5476 therefore produced a crew that could never be connected: the operator's
        own gateway usually owns 5476, and a second crew would collide with the
        first. Allocate deterministically upward from the tunnel base, skipping every
        port the registry already hands out.
        """
        if self._port:
            return self._port
        used: set[int] = set()
        try:
            for inst in InstancesRegistry().list():
                used.add(inst.remote_port)
                if inst.local_port:
                    used.add(inst.local_port)
        except Exception as exc:  # pragma: no cover - registry absent is not fatal
            logger.info("could not read the instances registry for port reuse: %s", exc)
        self._port = PortAllocator().allocate(exclude=used)
        logger.info("allocated gateway port %d for this crew", self._port)
        return self._port

    def preflight(self, profile: str, region: str) -> None:
        reach = iam.reachability_check(profile, region)
        if not reach.get("reachable"):
            detail = reach.get("detail") or reach.get("note") or "AWS credentials did not resolve"
            raise AWSError(str(detail))

    def provision(self, *, tag: str, size_key: str, profile: str, region: str) -> str:
        tier = sizes.get_tier(size_key)
        # Ship the local source only when this really is a checkout. The dashboard
        # runs from a wheel/app install for most users, where `source.repo_root()`
        # fails closed by design (it must not tar up site-packages) — and with
        # ship_source left at its default that raised mid-provision, making
        # one-click setup impossible for anyone who did not install from git. With
        # no checkout the instance installs from the template's public-repo clone
        # instead, which is the fallback the template already carries.
        ship_source = source_mod.find_repo_root() is not None
        if not ship_source:
            logger.info(
                "no local checkout found; the instance will install by cloning the "
                "public repo instead of shipping local source"
            )
        result = ec2.deploy(
            tag=tag, tier=tier, profile=profile, region=region, ship_source=ship_source,
            dashboard_port=self._allocate_port(),
        )
        return result.instance_id

    def begin_signin(self, *, instance_id: str, profile: str, region: str) -> _RealSigninHandle:
        return _RealSigninHandle(instance_id, profile, region)

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        registered = connect_mod.register_instance(
            instance_id, name=f"Kiro Crew Cloud ({tag})", profile=profile, region=region,
            # Must match the port the stack started the gateway on, or the mirrored
            # tunnel would forward to a port nothing is listening on.
            remote_port=self._allocate_port(),
        )
        if registered is None:
            # register_instance is best-effort BY CONTRACT: it returns None both when the
            # Instances feature is unavailable and when the registry write raises, logging
            # instead of propagating. Ignoring that return marks the launch `done` while
            # the crew is absent from the dashboard — the user is told setup succeeded and
            # is left paying for an instance that never appears in their crew list. Fail
            # loudly, and name the instance so it can still be recovered by hand.
            raise RuntimeError(
                f"The crew was created (instance {instance_id}, stack {tag}) but could not "
                "be added to your crews. It is running and billing — add it under Remote "
                "crew, or delete it, so it does not sit idle."
            )

    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        """Delete a stack this launch created — the rollback for a cancelled setup.

        Returns True only when AWS CONFIRMS the stack is gone. Requesting the delete
        is not the same as it succeeding: a stack can land in DELETE_FAILED, and
        reporting "Removed" off the back of an accepted request would tell the user
        their billing stopped when an instance may still be running.

        The request is issued with ``wait=False`` and confirmed separately so the
        caller can persist "cancelled" immediately and only then block on the
        (minutes-long) confirmation.
        """
        ec2.destroy(tag, profile, region, wait=False)
        return bool(ec2.wait_for_delete(tag, profile, region))
