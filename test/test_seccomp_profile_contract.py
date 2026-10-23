"""The shipped seccomp profile must allow every syscall the private window needs.

Containers run WITHOUT ``CAP_SYS_ADMIN``, so a rule gated on
``includes.caps`` never fires there: the call falls through to the profile's
``SCMP_ACT_ERRNO`` default and the launcher refuses fail-closed. A syscall the
launcher's private-window path needs that is allowed only inside the
CAP_SYS_ADMIN group therefore turns every sandboxed spawn carrying a private
window into "could not retire the staging mount", while the startup probe
(which opens no private window) stays green. This pins the invariant: each
syscall that path needs is reachable by an UNCONDITIONAL allow rule in the
shipped profile, and the profile grants nothing unconditional beyond them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SECCOMP_DIR = Path(__file__).resolve().parent.parent / "docker" / "seccomp"

#: The shipped profile, exactly what a container downloads per Option A in
#: docs/guides/docker.md.
PROFILE_PATH = _SECCOMP_DIR / "kirocrew-seccomp.json"

#: The private-window syscall set: clone the child, unshare
#: CLONE_NEWUSER|CLONE_NEWNS, mount --bind the staging dir, then
#: umount2(MNT_DETACH) to retire it (``_retire_stage_or_die`` in
#: sandbox_launcher_parts/pinned_mounts.py).
PRIVATE_WINDOW_SYSCALLS = ("clone", "unshare", "mount", "umount2")


def _load_profile() -> dict:
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "kirocrew_seccomp_gen_profile", _SECCOMP_DIR / "gen_profile.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ungated_allow_names(profile: dict) -> set[str]:
    """Syscall names a capability-less container can reach with any arguments.

    Rules gated on ``includes.caps`` are skipped: Docker evaluates them
    against the container's capability set, and a plain ``docker run`` has
    no CAP_SYS_ADMIN, so those rules never match and the call lands on
    ``defaultAction``. Rules carrying ``args`` filters are skipped too: they
    allow the call only for some flag values, which is not what the launcher
    can rely on.
    """
    allowed: set[str] = set()
    for rule in profile.get("syscalls", []):
        if rule.get("action") != "SCMP_ACT_ALLOW":
            continue
        if (rule.get("includes") or {}).get("caps"):
            continue
        if rule.get("args"):
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


def test_profile_adds_nothing_beyond_the_private_window_set() -> None:
    """The Kiro Crew additions are exactly the private-window syscalls.

    The legacy one-argument ``umount`` stays behind CAP_SYS_ADMIN: the
    launcher never calls it, so granting it would widen the profile for no
    caller.
    """
    gen = _load_generator()
    added = [name for rule in gen.EXTRA_RULES for name in rule["names"]]
    assert sorted(added) == sorted(PRIVATE_WINDOW_SYSCALLS)
    assert all(rule["action"] == "SCMP_ACT_ALLOW" for rule in gen.EXTRA_RULES)
    assert "umount" not in _ungated_allow_names(_load_profile())


def test_shipped_profile_ends_with_the_generator_rules() -> None:
    """Editing the generator without regenerating the JSON is caught here."""
    gen = _load_generator()
    syscalls = _load_profile()["syscalls"]
    tail = syscalls[len(syscalls) - len(gen.EXTRA_RULES) :]
    assert tail == gen.EXTRA_RULES
    assert _load_profile()["_comment"] == gen.COMMENT
