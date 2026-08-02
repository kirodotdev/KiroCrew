"""Per-application AppArmor profile granting the gateway unprivileged userns.

Ubuntu >= 23.10 ships ``kernel.apparmor_restrict_unprivileged_userns=1``, which
moves any process that creates a user namespace into a restricted AppArmor
profile carrying no ``CAP_SYS_ADMIN``. KiroCrew's Linux sandbox needs a user
namespace *and then* a mount namespace, so the second ``unshare`` gets EPERM and
``wrap_argv()`` fail-closes — correctly refusing to run the agent unisolated.

The distro-sanctioned remedy is a **per-application AppArmor profile** granting
``userns``, not a kernel-wide sysctl rollback. ``/etc/apparmor.d/`` on a stock
Ubuntu already ships exactly this for every application in the same position
(``bwrap-userns-restrict``, ``chrome``, ``chromium``, ``brave``, ``buildah``,
``ch-run``, ``QtWebEngineProcess``, ``1password``, ``Discord``). KiroCrew was the
outlier.

**Why a NAMED profile plus systemd, not a path attachment.** The obvious design —
attach the profile to the gateway's interpreter — does not work here, and worse,
the naive fix over-grants. ``~/.kiro/crew-venv/bin/python3`` is a *symlink* to the
system interpreter (verified on Ubuntu 26.04: it resolves to ``/usr/bin/python3``),
and AppArmor matches an attachment against the path the kernel resolves, not the
symlink. So a profile attached to the venv path would never match, while one
attached to ``/usr/bin/python3`` would grant ``userns`` to **every Python process
on the machine**. Instead this module installs a profile with **no attachment
path** and has systemd transition the service into it via ``AppArmorProfile=``,
which confines exactly one unit and is independent of how the interpreter is
resolved.

The gate is deliberately **mechanism-based, never distro-based**: Ubuntu
derivatives (Pop!_OS, Mint, Zorin, elementary) inherit the restriction and would
be missed by an ID check, and Debian 13 ships AppArmor *without* the restriction
so it must be left alone. Every check below must pass or the profile is skipped.

Nothing here may ever fail an install: a gateway that runs without the profile is
the status quo, whereas an install that aborts because a hardening step failed is
a regression. All entry points return a result and warn; they do not raise.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# The profile is named, with no attachment path, and applied by systemd. Keep the
# filename and the profile name identical so `apparmor_parser -R` on the file
# removes the same profile the unit names.
PROFILE_NAME = "kirocrew-userns"
PROFILE_PATH = Path("/etc/apparmor.d") / PROFILE_NAME

# Read the knob through /proc rather than shelling out to `sysctl`: it avoids a
# PATH-dependent subprocess and returns the same value.
_SYSCTL_PATH = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
_LSM_PATH = Path("/sys/kernel/security/lsm")
# The abi the profile declares must name a file this host actually ships. It
# tracks the POLICY files, not the parser version: Ubuntu 25.10 has
# apparmor_parser 5.x but only abi/3.0 and abi/4.0 on disk, so pinning the abi
# to the parser major makes the profile fail to load with "Could not open
# 'abi/5.0'". Highest available wins; with none, the abi line is omitted and the
# parser falls back to its own default.
_ABI_DIR = Path("/etc/apparmor.d/abi")

# The `userns,` rule this profile depends on needs AppArmor 4.x or newer. On an
# older parser the profile would fail to compile, so skip rather than warn later.
_MIN_PARSER_MAJOR = 4

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


@dataclass(frozen=True)
class ProfileOutcome:
    """What an install/uninstall attempt did, for the CLI to report."""

    changed: bool
    message: str
    # False when a step failed. The caller still continues — this only decides
    # whether the message is printed as a warning.
    ok: bool = True


# Anything this module hands to sudo MUST be resolved from a trusted system
# directory, never through $PATH. `shutil.which` honours the invoking user's
# PATH, so a writable directory early in it would let an unprivileged user get a
# fake `apparmor_parser` executed as root by `kirocrew service install` — a local
# privilege escalation introduced by, of all things, a hardening step. Mirrors the
# existing provider-CLI rule in security.md: canonical, root-owned, and not
# writable by anyone but root.
_TRUSTED_BIN_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin")


def _resolve_trusted(name: str) -> str | None:
    """Absolute path to *name* in a trusted system dir, or None.

    Rejects a symlink chain that leaves the trusted set, anything not owned by
    root, and anything group- or world-writable. Returning None makes the caller
    skip its step rather than run something it cannot vouch for.
    """
    for directory in _TRUSTED_BIN_DIRS:
        candidate = Path(directory) / name
        try:
            resolved = candidate.resolve(strict=True)
            if not any(
                resolved == Path(d) / resolved.name or str(resolved).startswith(d + "/")
                for d in _TRUSTED_BIN_DIRS
            ):
                continue  # symlink escaped the trusted set
            info = resolved.stat()
        except OSError:
            continue
        if info.st_uid != 0:
            continue  # not root-owned
        if info.st_mode & 0o022:
            continue  # group- or world-writable
        return str(resolved)
    return None


def userns_restricted() -> bool:
    """True when this kernel restricts unprivileged user namespaces via AppArmor.

    The sysctl **existing and equalling 1** is the discriminator for the whole
    feature. Absent (Debian 13, older kernels) or 0 (operator already relaxed it)
    means there is nothing for this profile to fix.
    """
    try:
        return _SYSCTL_PATH.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def apparmor_is_active() -> bool:
    """True when AppArmor is one of the kernel's active LSMs.

    Presence of AppArmor is necessary but NOT sufficient — Debian 13 has AppArmor
    loaded and is unaffected — so this is only ever one half of the gate.
    """
    try:
        return "apparmor" in _LSM_PATH.read_text(encoding="utf-8").split(",")
    except OSError:
        return False


def parser_path() -> str | None:
    """Trusted absolute path to ``apparmor_parser``, or None.

    Resolved from a fixed system-directory list, NOT $PATH — this path is handed
    to sudo, so a PATH-controlled binary here would run as root.
    """
    return _resolve_trusted("apparmor_parser")


def parser_version(parser: str) -> tuple[int, int] | None:
    """``(major, minor)`` of ``apparmor_parser``, or None if it cannot be read."""
    try:
        proc = subprocess.run(
            [parser, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(proc.stdout or proc.stderr or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def should_install() -> tuple[bool, str]:
    """Whether this host needs the profile, plus a reason to print either way.

    Ordered cheapest-first so a non-Ubuntu host does no subprocess work at all.
    """
    if not apparmor_is_active():
        return (False, "AppArmor is not an active LSM on this kernel")
    if not userns_restricted():
        return (
            False,
            "kernel.apparmor_restrict_unprivileged_userns is not 1, "
            "so unprivileged user namespaces are not restricted",
        )
    parser = parser_path()
    if parser is None:
        return (False, "apparmor_parser is not installed")
    version = parser_version(parser)
    if version is None:
        return (False, f"could not read the version of {parser}")
    if version[0] < _MIN_PARSER_MAJOR:
        return (
            False,
            f"apparmor_parser {version[0]}.{version[1]} is older than "
            f"{_MIN_PARSER_MAJOR}.x, which the 'userns' rule requires",
        )
    return (True, f"AppArmor {version[0]}.{version[1]} with userns restricted")


def detect_abi() -> str | None:
    """Highest numeric abi file present, e.g. ``"4.0"``; None when none exist."""
    try:
        names = [entry.name for entry in _ABI_DIR.iterdir()]
    except OSError:
        return None
    versions: list[tuple[int, int]] = []
    for name in names:
        match = re.fullmatch(r"(\d+)\.(\d+)", name)
        if match:
            versions.append((int(match.group(1)), int(match.group(2))))
    if not versions:
        return None
    best = max(versions)
    return f"{best[0]}.{best[1]}"


def render_profile(abi: str | None) -> str:
    """Render the named profile.

    ``flags=(unconfined)`` means the profile restricts nothing — it exists solely
    to carry the ``userns`` grant, which is the same shape stock Ubuntu uses for
    ``chrome`` / ``brave``. There is deliberately **no attachment path**: systemd
    applies it by name, so it cannot leak onto an unrelated interpreter.

    ``abi`` names a policy abi file this host actually ships (see
    :func:`detect_abi`) — declaring one that is absent makes the profile fail to
    load. When None the line is omitted and the parser uses its default.
    """
    abi_line = f"abi <abi/{abi}>,\n\n" if abi else ""
    return f"""# Managed by KiroCrew — regenerated by `kirocrew service install`.
#
# This profile grants ONE permission: creating an unprivileged user namespace.
# KiroCrew's Linux sandbox isolates the agent by entering a user namespace and
# then a mount namespace, over-mounting credential paths such as ~/.aws and
# ~/.ssh. On Ubuntu >= 23.10, kernel.apparmor_restrict_unprivileged_userns=1
# blocks that second step, so KiroCrew fail-closes and refuses to run the agent
# rather than run it unisolated.
#
# It is a NAMED profile with no attachment path: systemd applies it to the
# kirocrew service via AppArmorProfile=. It therefore cannot apply to any other
# process. Do NOT add an attachment path for the venv interpreter — that is a
# symlink to the system python, and attaching there would grant this permission
# to every Python process on the host.
#
# flags=(unconfined) means this profile restricts nothing else; it is the same
# shape /etc/apparmor.d/chrome and /etc/apparmor.d/brave use.
#
# Removing this file re-breaks KiroCrew's sandbox on this host: the gateway will
# refuse to spawn the agent instead of running it without isolation.
# `kirocrew service uninstall` removes it for you.
{abi_line}include <tunables/global>

profile {PROFILE_NAME} flags=(unconfined) {{
  userns,

  # Site-specific additions and overrides. See local/README for details.
  include if exists <local/{PROFILE_NAME}>
}}
"""


def validate(parser: str, text: str) -> tuple[bool, str]:
    """Parse the profile WITHOUT loading it. Returns ``(ok, detail)``.

    Mirrors the generate-then-validate shape ``sandbox.py`` already uses for the
    macOS Seatbelt profile: write to a temp file, ask the real tool, check the rc.
    ``--skip-cache`` is required because writing ``/var/cache/apparmor`` needs
    root and this runs before any privileged step.
    """
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".aa", prefix="kirocrew_profile_", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(text)
            tmp = handle.name
        proc = subprocess.run(
            [parser, "-Q", "--skip-cache", tmp],
            capture_output=True,
            text=True,
            timeout=30,
        )
        detail = (proc.stderr or proc.stdout or "").strip()
        return (proc.returncode == 0, detail)
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, str(exc))
    finally:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass


# A probe that mirrors what the gateway actually needs: the launcher's split
# unshare sequence. Run INSIDE the profile via aa-exec, because the process doing
# the install is not itself confined by it (systemd applies it to the service), so
# probing here would report the unpatched host and look like a failure.
# A SELF-CONTAINED replica of the launcher's split unshare sequence. It must not
# import kiro_crew: this runs under sudo, and importing our own package would
# execute user-writable site-packages as root. Only the stdlib and a constant
# string, so nothing user-controlled is evaluated with privilege.
_VERIFY_SNIPPET = (
    "import ctypes,ctypes.util,os,sys\n"
    "l=ctypes.CDLL(ctypes.util.find_library('c'),use_errno=True)\n"
    "l.unshare.argtypes=[ctypes.c_int]\n"
    "u,g=os.getuid(),os.getgid()\n"
    "r,w=os.pipe()\n"
    "p=os.fork()\n"
    "if p==0:\n"
    " os.close(r)\n"
    " ctypes.set_errno(0)\n"
    " if l.unshare(0x10000000)!=0: os._exit(90)\n"
    " os.write(w,b'x'); os.close(w)\n"
    " import time; time.sleep(2)\n"
    " os._exit(91)\n"
    "os.close(w)\n"
    "ok=os.read(r,1)==b'x'\n"
    "if ok:\n"
    " try:\n"
    "  open('/proc/%d/setgroups'%p,'w').write('deny')\n"
    "  open('/proc/%d/uid_map'%p,'w').write('%d %d 1'%(u,u))\n"
    "  open('/proc/%d/gid_map'%p,'w').write('%d %d 1'%(g,g))\n"
    " except OSError: ok=False\n"
    "os.kill(p,9); os.waitpid(p,0)\n"
    "if not ok: sys.exit(1)\n"
    "c=os.fork()\n"
    "if c==0:\n"
    " ctypes.set_errno(0)\n"
    " if l.unshare(0x10000000)!=0: os._exit(92)\n"
    " ctypes.set_errno(0)\n"
    " os._exit(0 if l.unshare(0x00020000)==0 else 93)\n"
    "sys.exit(0 if os.waitstatus_to_exitcode(os.waitpid(c,0)[1])==0 else 1)\n"
)


def verify_enforcement(sudo_capture, uid: int, gid: int) -> tuple[bool, str | None]:
    """Confirm the loaded profile GRANTS userns. ``(ok, problem)``; problem None if ok.

    Three constraints shape this, and getting any of them wrong is a real bug:

    1. **It needs privilege to ENTER the profile.** ``aa_change_onexec()`` into a
       named profile is not permitted for an ordinary unconfined user, and
       ``aa-exec`` does not fail loudly when it cannot transition — it execs the
       command unconfined, so an unprivileged attempt returns a false NEGATIVE
       (verified on Ubuntu 26.04).
    2. **It must NOT run anything user-writable as root.** So the interpreter is a
       trusted system ``python3``, never ``sys.executable`` (the venv python is
       user-writable), and the payload is a constant stdlib snippet that does not
       import kiro_crew — otherwise ``sudo`` would execute user-controlled
       site-packages.
    3. **The probe itself must run UNPRIVILEGED**, or it proves nothing: root may
       be permitted to create namespaces regardless of the restriction, so a
       root-side success would not tell us the gateway's user can. ``setpriv``
       drops back to the invoking uid/gid inside the profile before the probe.

    A missing tool yields ``(False, "could not verify: ...")`` — inconclusive, not
    a claim of failure, and never fatal.
    """
    aa_exec = _resolve_trusted("aa-exec")
    setpriv = _resolve_trusted("setpriv")
    python3 = _resolve_trusted("python3")
    missing = [
        name
        for name, path in (("aa-exec", aa_exec), ("setpriv", setpriv), ("python3", python3))
        if path is None
    ]
    if missing:
        return (False, f"could not verify enforcement: {', '.join(missing)} not found in a trusted system directory")
    try:
        rc, output = sudo_capture(
            aa_exec,
            "-p",
            PROFILE_NAME,
            "--",
            setpriv,
            f"--reuid={uid}",
            f"--regid={gid}",
            "--clear-groups",
            "--",
            python3,
            "-c",
            _VERIFY_SNIPPET,
        )
    except Exception as exc:  # noqa: BLE001 - verification must never raise
        return (False, f"could not verify enforcement: {exc}")
    if rc == 0:
        return (True, None)
    return (False, output.strip() or "the namespace probe still fails inside the profile")


def install(sudo_install_file, sudo_run, sudo_capture, uid: int, gid: int) -> ProfileOutcome:
    """Generate, validate, load and verify the profile. NEVER raises.

    ``sudo_install_file(text, dest)`` and ``sudo_run(*argv)`` are injected so this
    reuses the caller's existing privileged helpers — the same ones that already
    write the systemd unit — rather than inventing a second escalation path, and
    so tests can drive the whole flow without touching the real host.
    """
    needed, reason = should_install()
    if not needed:
        return ProfileOutcome(False, f"AppArmor profile not needed: {reason}")
    parser = parser_path()
    version = parser_version(parser) if parser else None
    if parser is None or version is None:  # pragma: no cover - should_install covers
        return ProfileOutcome(False, "AppArmor profile skipped: parser unavailable")

    text = render_profile(detect_abi())
    valid, detail = validate(parser, text)
    if not valid:
        # Refuse to install something that does not compile: loading a broken
        # profile is how you get a service that will not start.
        return ProfileOutcome(
            False, f"AppArmor profile did NOT compile, so it was not installed: {detail}", ok=False
        )
    try:
        sudo_install_file(text, PROFILE_PATH)
        sudo_run(parser, "-r", "-W", str(PROFILE_PATH))
    except Exception as exc:  # noqa: BLE001 - any failure here is non-fatal
        return ProfileOutcome(
            False,
            f"AppArmor profile could not be installed ({exc}). The gateway will "
            "still start, but the agent sandbox cannot be built on this host, so "
            "spawns will fail closed until this is resolved.",
            ok=False,
        )
    enforced, problem = verify_enforcement(sudo_capture, uid, gid)
    if not enforced:
        return ProfileOutcome(
            True,
            f"AppArmor profile written to {PROFILE_PATH} and loaded, but "
            f"enforcement was not confirmed ({problem}). Not claiming success.",
            ok=False,
        )
    return ProfileOutcome(
        True,
        f"AppArmor profile installed at {PROFILE_PATH} — grants unprivileged "
        "userns to the kirocrew service only, and enforcement is confirmed",
    )


def uninstall(sudo_run) -> ProfileOutcome:
    """Unload and remove the profile. Idempotent, and never raises.

    Whatever removes the service removes the profile, so a host is left exactly as
    it was found rather than carrying an orphaned grant.
    """
    if not PROFILE_PATH.exists():
        return ProfileOutcome(False, "")
    parser = parser_path()
    try:
        if parser is not None:
            # -R unloads; tolerate failure since the file removal is what matters.
            sudo_run(parser, "-R", str(PROFILE_PATH))
    except Exception:  # noqa: BLE001
        logger.warning("Could not unload the AppArmor profile", exc_info=True)
    try:
        sudo_run("rm", "-f", str(PROFILE_PATH))
    except Exception as exc:  # noqa: BLE001
        return ProfileOutcome(False, f"could not remove {PROFILE_PATH}: {exc}", ok=False)
    return ProfileOutcome(True, f"AppArmor profile removed from {PROFILE_PATH}")
