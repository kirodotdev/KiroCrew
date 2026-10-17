"""The shipped seccomp profile must allow every syscall the private window needs.

Containers run WITHOUT ``CAP_SYS_ADMIN``, so a rule gated on
``includes.caps`` never fires there: the call falls through to the profile's
``SCMP_ACT_ERRNO`` default and the launcher refuses fail-closed. That is how
#19392 played out — ``umount2`` existed in the profile only inside the
CAP_SYS_ADMIN group, so ``_retire_stage_or_die``'s ``umount2(MNT_DETACH)``
failed with EPERM and every sandboxed spawn carrying a private window died
with "could not retire the staging mount", while the startup probe (which
opens no private window) stayed green. This pins the invariant: each syscall
the launcher's private-window path needs must be reachable by an
UNCONDITIONAL allow rule in the shipped profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: The shipped profile, exactly what a container downloads per Option A in
#: docs/guides/docker.md.
PROFILE_PATH = (
    Path(__file__).resolve().parent.parent / "docker" / "seccomp" / "kirocrew-seccomp.json"
)

#: The private-window syscall set: clone the child, unshare
#: CLONE_NEWUSER|CLONE_NEWNS, mount --bind the staging dir, then
#: umount2(MNT_DETACH) to retire it (sandbox_launcher_program.py:279).
PRIVATE_WINDOW_SYSCALLS = ("clone", "unshare", "mount", "umount2")


def _load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _ungated_allow_names(profile: dict) -> set[str]:
    """Syscall names a capability-less container can actually reach.

    Rules gated on ``includes.caps`` are skipped: Docker evaluates them
    against the container's capability set, and a plain ``docker run`` has
    no CAP_SYS_ADMIN, so those rules never match and the call lands on
    ``defaultAction``.
    """
    allowed: set[str] = set()
    for rule in profile.get("syscalls", []):
        if rule.get("action") != "SCMP_ACT_ALLOW":
            continue
        if (rule.get("includes") or {}).get("caps"):
            continue
        allowed.update(rule.get("names", []))
    return allowed


def test_profile_fails_closed_by_default() -> None:
    """The context for the invariant: anything ungated-and-unlisted is EPERM."""
    assert _load_profile()["defaultAction"] == "SCMP_ACT_ERRNO"


@pytest.mark.parametrize("syscall", PRIVATE_WINDOW_SYSCALLS)
def test_private_window_syscall_is_allowed_without_capabilities(syscall: str) -> None:
    allowed = _ungated_allow_names(_load_profile())
    assert syscall in allowed, (
        "%s is missing from every unconditional SCMP_ACT_ALLOW rule; a container "
        "without CAP_SYS_ADMIN falls through to defaultAction and every "
        "private-window spawn fails closed" % syscall
    )
