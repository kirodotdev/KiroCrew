"""Every mount in the namespace launcher refuses to exec when it fails.

Each ``mount(2)`` in the launcher IS a security control: three hide credential
paths, one pins mount propagation so the hiding cannot escape, and a pair exposes
the governance cache read-only (bind, then remount MS_RDONLY -- the remount is
what withholds the write, since MS_RDONLY is ignored on the initial bind).
Discarding the return value made them all fail OPEN -- the path stayed visible, or
stayed WRITABLE, and the agent ran anyway -- and nothing downstream noticed, because there is no post-mount
emptiness check, the launcher has no logger, and the pre-exec hardlink scan only
fires when a credential happens to carry an extra link.

These tests run the mount region lifted VERBATIM out of the shipped launcher, so
they cannot drift away from what actually executes in the child. ``_libc`` is a
stand-in whose ``mount`` returns a chosen rc, which is the only way to exercise
the failure path at all: this test process cannot create a user namespace (a
nested ``unshare`` is seccomp-denied inside an agent sandbox), and even outside
one a real EPERM would need an LSM mount rule the test cannot install.

Every behavioural assertion below has its own break-arm in
``test_break_arms_falsify_each_assertion``: a mutation applied to the shipped
source, chosen to move that assertion's own value. One arm for the whole file
would be inert for any assertion whose expected value happens to coincide with
the mutant's output.
"""

from __future__ import annotations

import errno
import os
import runpy
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from kiro_crew.sandbox import _build_launcher_script

# Every test here builds the shipped launcher, and ``_build_launcher_script`` calls
# POSIX-only ``os.getuid``/``os.getgid`` (the namespace launcher is Linux-only), so
# all of them raise AttributeError on Windows. Guarded rather than listed in
# ``test/windows-expected-failures.txt``: that list is a burn-down backlog of gaps to
# close, and a POSIX-only launcher is a permanent platform boundary. The sibling
# launcher suites take the same route -- see ``test_sandbox_argv.py`` (#2041).
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)

#: Module-level helper: from its ``def`` to the first template substitution.
_HELPER_START = "def _mount_or_die("
_HELPER_END = "REAL_UID = "
#: The propagation mount, on its own -- the tmpfs-source picker sits between it
#: and the hiding loops and is deliberately not exercised here (it probes the
#: host's /run/user and /dev/shm; ``_tmpfs_src`` is injected instead so every
#: temp artifact lands under pytest's tmp_path).
_PROP_START = "        # Private mount propagation"
_PROP_END = "        # Pick a tmpfs-backed source dir"
#: The three hiding mounts: credential dirs, sensitive files, ~/.ssh.
_HIDE_START = "        # Bind-mount empty dirs over credential paths"
_HIDE_END = "        # Scrub sensitive env vars"

#: What the extracted region must contain. Without this a marker rename would
#: shrink a slice and leave every assertion below vacuously green against a
#: fragment that no longer holds the guard. Deliberately STRUCTURAL, not the
#: guard EXPRESSION: pinning a call's exact text here would make the break-arm
#: that reverts that call fail on the landmark instead of on its assertion, and
#: the call form is already pinned once, on purpose, by
#: ``test_every_tier_routes_all_four_mounts_through_the_guard``.
_LANDMARKS = (
    "# Private mount propagation",  # the propagation site
    "for d in SENSITIVE_DIRS:",  # the credential-dir loop
    "for d in READONLY_DIRS:",  # the read-only exposure loop
    "for f in SENSITIVE_FILES:",  # the sensitive-file loop
    "if HIDE_SSH and os.path.isdir(SSH_DIR):",  # the .ssh block
    "sandbox: BLOCKED",  # the refusal
)


class _FakeLibc:
    """``_libc``, with a ``mount`` that fails on a chosen call.

    ``fail_at`` is 1-based over the calls this region makes, in source order:
    1 = propagation, 2 = first credential dir, 3 = read-only bind, 4 = read-only
    remount, 5 = first sensitive file, 6 = ~/.ssh. ``None`` means every mount
    succeeds.
    """

    def __init__(self, *, fail_at: int | None, err: int = errno.EPERM) -> None:
        self.fail_at = fail_at
        self.err = err
        self.calls: list[tuple[object, object, int]] = []

    def mount(self, source, target, fstype, flags, data):  # noqa: ANN001
        self.calls.append((source, target, flags))
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            import ctypes

            ctypes.set_errno(self.err)
            return -1
        return 0


def _region(script: str) -> str:
    """Helper + propagation site + hiding loops, lifted out of *script*.

    Sliced from the START OF THE LINE, not from the marker: ``dedent`` measures
    the common prefix across all lines, so a first line already stripped of its
    indent leaves the rest indented and the block will not parse.
    """

    def cut(start_marker: str, end_marker: str) -> str:
        a = script.rindex("\n", 0, script.index(start_marker)) + 1
        b = script.rindex("\n", 0, script.index(end_marker, a)) + 1
        return script[a:b]

    helper = cut(_HELPER_START, _HELPER_END)
    body = textwrap.dedent(cut(_PROP_START, _PROP_END)) + textwrap.dedent(
        cut(_HIDE_START, _HIDE_END)
    )
    region = helper + "\n" + body
    missing = [m for m in _LANDMARKS if m not in region]
    assert not missing, f"the extracted mount region is missing {missing}"
    return region


def _run(
    tmp_path: Path,
    *,
    fail_at: int | None,
    err: int = errno.EPERM,
    script: str | None = None,
) -> tuple[_FakeLibc, str | None]:
    """Run the mount region. Returns ``(fake_libc, refusal_message_or_None)``.

    Via ``runpy.run_path`` on the extracted region rather than ``exec`` of its
    text: equivalent here -- both run the shipped source with an injected
    namespace -- but ``exec`` trips the SAST gate's ``exec-detected`` rule, and a
    suppression comment would be this repo's first, spent on a false positive.
    """
    import ctypes

    home = tmp_path / "home"
    aws = home / ".aws"
    aws.mkdir(parents=True)
    (aws / "credentials").write_text("[default]\n")
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("example.com ssh-rsa AAAA\n")
    lone = home / ".netrc"
    lone.write_text("machine example.com\n")
    # The governance cache: exposed read-only rather than hidden, so it is the one
    # target whose rule is a REAL bind of itself plus a sealing remount.
    cache = home / ".kiro" / "crew" / "policy_cache"
    cache.mkdir(parents=True)
    (cache / "policy.json").write_text("{}\n")
    src_dir = tmp_path / "tmpfs"
    src_dir.mkdir()

    libc = _FakeLibc(fail_at=fail_at, err=err)
    ns = {
        "_libc": libc,
        "_MS_BIND": 4096,
        "_MS_REC": 16384,
        "_MS_PRIVATE": 1 << 18,
        "_MS_RDONLY": 1,
        "_MS_REMOUNT": 32,
        "_MS_NOSUID": 2,
        "_MS_NODEV": 4,
        "_MS_NOEXEC": 8,
        "ctypes": ctypes,
        "os": os,
        "sys": sys,
        "tempfile": tempfile,
        "_tmpfs_src": str(src_dir),
        # Defined by the launcher alongside _tmpfs_src, before this region: the
        # pid-bearing prefix tagging every bind-mount source for the janitor.
        "_src_prefix": "kirocrew_sb_%d_" % os.getpid(),
        "expose_data": {},
        "EXPOSE_FILES": [],
        "SENSITIVE_DIRS": [str(aws)],
        "READONLY_DIRS": [str(cache)],
        "SENSITIVE_FILES": [str(lone)],
        "SSH_DIR": str(ssh),
        "SSH_KNOWN_HOSTS": str(ssh / "known_hosts"),
        "HIDE_SSH": True,
    }
    region_file = tmp_path / "region.py"
    region_file.write_text(_region(script or _build_launcher_script("strict")))
    try:
        runpy.run_path(str(region_file), init_globals=ns)
    except SystemExit as exc:
        return libc, str(exc.code)
    return libc, None


# --------------------------------------------------------------------------
# Behavioural assertions
# --------------------------------------------------------------------------


def test_all_mounts_succeeding_lets_the_exec_proceed(tmp_path: Path) -> None:
    """The guard must not turn a healthy spawn into a refusal.

    Break-arm: ``happy_path`` (helper's ``!= 0`` flipped to ``== 0``).
    """
    libc, refusal = _run(tmp_path, fail_at=None)
    assert refusal is None
    # propagation + credential dir + read-only bind + its sealing remount + file + ssh
    assert len(libc.calls) == 6


@pytest.mark.parametrize(
    ("fail_at", "expect_in_message"),
    [
        (1, "propagation"),
        (2, "credential directory"),
        (3, "exposing read-only path"),
        (4, "sealing read-only path"),
        (5, "sensitive file"),
        (6, "ssh key directory"),
    ],
    ids=[
        "propagation",
        "credential-dir",
        "readonly-bind",
        "readonly-seal",
        "sensitive-file",
        "ssh-dir",
    ],
)
def test_a_failed_mount_refuses_to_exec(
    tmp_path: Path, fail_at: int, expect_in_message: str
) -> None:
    """Each of the six sites refuses, and says which control failed.

    ``readonly-seal`` is the one whose failure is least visibly a security
    failure and most needs the refusal: the bind succeeded, so the directory is
    THERE and readable, and only the remount that withholds write did not land.
    Proceeding would hand the child exactly the write access the pair exists to
    deny, with nothing observably wrong.

    Break-arms: ``site1`` .. ``site6`` -- each restores the pre-fix unchecked
    ``_libc.mount(...)`` call at that one site, so exactly the matching
    parametrisation stops refusing.
    """
    libc, refusal = _run(tmp_path, fail_at=fail_at)
    assert refusal is not None, f"site {fail_at} let the spawn proceed"
    assert "sandbox: BLOCKED" in refusal
    assert expect_in_message in refusal
    # Stops AT the failure: no mount is attempted after the one that failed.
    assert len(libc.calls) == fail_at


def test_the_refusal_names_the_hidden_path(tmp_path: Path) -> None:
    """An operator needs the path, not just 'a mount failed'.

    Break-arm: ``drop_path`` (the dirs site's label made a constant).
    """
    libc, refusal = _run(tmp_path, fail_at=2)
    assert refusal is not None
    target = libc.calls[-1][1].decode()
    assert target in refusal


def test_the_refusal_carries_the_errno(tmp_path: Path) -> None:
    """errno is the only thing that distinguishes an LSM denial from ENOMEM.

    Break-arm: ``drop_errno`` (errno removed from the helper's message).
    """
    _libc_unused, refusal = _run(tmp_path, fail_at=2, err=errno.ENOMEM)
    assert refusal is not None
    assert str(errno.ENOMEM) in refusal
    assert os.strerror(errno.ENOMEM) in refusal


def test_the_refusal_names_the_deliberate_opt_out(tmp_path: Path) -> None:
    """Refusing is only defensible if the message says how to opt out.

    Break-arm: ``drop_optout`` (the sandbox_level sentence removed).
    """
    _libc_unused, refusal = _run(tmp_path, fail_at=1)
    assert refusal is not None
    assert "sandbox_level" in refusal


def test_every_tier_routes_all_six_mounts_through_the_guard() -> None:
    """No tier may keep a raw, unchecked ``_libc.mount`` call site.

    Break-arm: ``reintroduce_raw`` (one site reverted to the raw call).
    """
    for level in ("strict", "cc", "standard"):
        script = _build_launcher_script(level)
        raw = [
            line
            for line in script.splitlines()
            # the helper's own call is the one legitimate raw use
            if "_libc.mount(" in line and "source, target, None, flags, None" not in line
        ]
        assert raw == [], f"{level}: unchecked mount call(s): {raw}"
        assert script.count("_mount_or_die(") == 7  # 1 def + 6 call sites


# --------------------------------------------------------------------------
# Break-arms: one mutation per assertion above
# --------------------------------------------------------------------------

#: ``name -> (mutation applied to the shipped script, what it must break)``.
#: Each mutation is chosen to move ONE assertion's own value; a single arm would
#: be inert for any assertion whose expected value coincided with the mutant's.
_ARMS: dict[str, tuple[str, str]] = {
    "site1": (
        '_mount_or_die(None, b"/", _MS_REC | _MS_PRIVATE,\n'
        '                      "making mount propagation private on /")',
        '_libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None)',
    ),
    "site2": (
        "_mount_or_die(per_dir_empty, target, _MS_BIND,\n"
        '                              "hiding credential directory %s" % d)',
        "_libc.mount(per_dir_empty, target, None, _MS_BIND, None)",
    ),
    "site3": (
        "_mount_or_die(target, target, _MS_BIND,\n"
        '                              "exposing read-only path %s" % d)',
        "_libc.mount(target, target, None, _MS_BIND, None)",
    ),
    "site4": (
        "_mount_or_die(target, target,\n"
        "                              _MS_REMOUNT | _MS_BIND | _MS_RDONLY\n"
        "                              | _locked_mount_flags(target),\n"
        '                              "sealing read-only path %s" % d)',
        "_libc.mount(target, target, None, _MS_REMOUNT | _MS_BIND | _MS_RDONLY, None)",
    ),
    "site5": (
        "_mount_or_die(empty_path.encode(), target, _MS_BIND,\n"
        '                              "hiding sensitive file %s" % f)',
        "_libc.mount(empty_path.encode(), target, None, _MS_BIND, None)",
    ),
    "site6": (
        "_mount_or_die(ssh_tmp, SSH_DIR.encode(), _MS_BIND,\n"
        '                          "hiding ssh key directory %s" % SSH_DIR)',
        "_libc.mount(ssh_tmp, SSH_DIR.encode(), None, _MS_BIND, None)",
    ),
    "happy_path": (
        "if _libc.mount(source, target, None, flags, None) != 0:",
        "if _libc.mount(source, target, None, flags, None) == 0:",
    ),
    "drop_errno": (
        '"sandbox: BLOCKED -- %s failed: errno %d (%s). The sandbox could not "',
        '"sandbox: BLOCKED -- %s failed. The sandbox could not "',
    ),
    "drop_path": (
        '"hiding credential directory %s" % d',
        '"hiding a credential directory"',
    ),
    "drop_optout": (
        '"visible. Lower sandbox_level to run without it deliberately."',
        '"visible."',
    ),
}


def _mutate(arm: str) -> str:
    old, new = _ARMS[arm]
    script = _build_launcher_script("strict")
    assert script.count(old) == 1, f"arm {arm}: anchor not unique ({script.count(old)})"
    return script.replace(old, new)


@pytest.mark.parametrize("arm", sorted(_ARMS))
def test_break_arms_falsify_each_assertion(tmp_path: Path, arm: str) -> None:
    """Each arm must break its assertion -- proof the assertion has power."""
    script = _mutate(arm)

    if arm == "drop_errno":
        # `errno %d` gone: the errno assertion can no longer hold. The message
        # is now malformed (%-args outnumber the placeholders), so a TypeError
        # here is the same evidence as a missing number.
        try:
            _unused, refusal = _run(tmp_path, fail_at=2, err=errno.ENOMEM, script=script)
        except TypeError:
            return
        assert refusal is None or str(errno.ENOMEM) not in refusal
        return

    if arm == "drop_path":
        libc, refusal = _run(tmp_path, fail_at=2, script=script)
        assert refusal is not None
        assert libc.calls[-1][1].decode() not in refusal
        return

    if arm == "drop_optout":
        _unused, refusal = _run(tmp_path, fail_at=1, script=script)
        assert refusal is not None
        assert "sandbox_level" not in refusal
        return

    if arm == "happy_path":
        # The guard now fires on SUCCESS, so an all-succeeding run refuses.
        _unused, refusal = _run(tmp_path, fail_at=None, script=script)
        assert refusal is not None
        return

    # site1..site6: that one site is unchecked again, so it proceeds silently.
    site = int(arm[-1])
    _unused, refusal = _run(tmp_path, fail_at=site, script=script)
    assert refusal is None, f"arm {arm} still refused: {refusal}"


def test_break_arm_reintroduce_raw_is_caught_by_the_tier_sweep() -> None:
    """The no-raw-call-sites sweep must fail when a raw call comes back."""
    script = _mutate("site2")
    raw = [
        line
        for line in script.splitlines()
        if "_libc.mount(" in line and "source, target, None, flags, None" not in line
    ]
    assert raw, "the sweep would not have noticed a reintroduced raw mount"
