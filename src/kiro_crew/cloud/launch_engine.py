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
        prompt = login.start_device_login(instance_id, profile, region, open_browser=False)
        self._prompt = prompt
        self.already_logged_in = bool(getattr(prompt, "already_logged_in", False))
        self.url = str(getattr(prompt, "url", "") or "")
        self.code = str(getattr(prompt, "code", "") or "")
        self.ports = list(getattr(prompt, "ports", []) or [])

    def wait(self, cancel: threading.Event) -> bool:
        # Ensure a background login is polling, then block on detection. The
        # underlying wait has its own timeout; ``cancel`` is best-effort (it is
        # re-checked by the orchestrator immediately after this returns).
        login.resume_login_daemon(self._iid, self._profile, self._region)
        if cancel.is_set():
            return False
        try:
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
        except AWSError:
            return False

    def close(self) -> None:
        try:
            self._prompt.close()
        except Exception:  # pragma: no cover - best effort
            logger.info("device-login prompt close failed (non-fatal)", exc_info=True)


class RealLaunchEngine:
    """Drives a launch against the user's AWS account via the ``cloud/`` engine."""

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
            tag=tag, tier=tier, profile=profile, region=region, ship_source=ship_source
        )
        return result.instance_id

    def begin_signin(self, *, instance_id: str, profile: str, region: str) -> _RealSigninHandle:
        return _RealSigninHandle(instance_id, profile, region)

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        registered = connect_mod.register_instance(
            instance_id, name=f"Kiro Crew Cloud ({tag})", profile=profile, region=region
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

    def teardown(self, *, tag: str, profile: str, region: str) -> None:
        """Delete a stack this launch created — the rollback for a cancelled setup.

        ``wait=False``: the caller is unwinding a cancellation on a worker thread and
        must not block for the minutes a DELETE takes. Requesting the delete is what
        stops the billing clock.
        """
        ec2.destroy(tag, profile, region, wait=False)
