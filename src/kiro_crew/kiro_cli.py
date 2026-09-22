"""Side-effect-free Kiro CLI discovery shared by setup and ACP launch paths."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from kiro_crew import identity_stores, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.env import augmented_path
from kiro_crew.subprocess_utf8 import UTF8_TEXT

KIRO_CLI_NAME = "kiro-cli"

# kiro-cli's own local state database. Holds identity-describing rows next to
# credential rows, so every reader here is read-only and key-scoped. Alias of
# the single canonical filename constant so the six former copies cannot drift.
KIRO_CLI_STATE_DB = identity_stores.AUTH_SQLITE_DB

# Non-secret rows kiro-cli writes when the signed-in identity came from IAM
# Identity Center. Presence is the whole signal: the values (a start URL and a
# region) are never returned, and no token key is selected.
_IDC_STATE_KEYS = ("auth.idc.start-url", "auth.idc.region")

# Name only. Governance covers API-key sign-ins too, so its presence decides
# whether an admin registry can apply; the value is never read.
_API_KEY_ENV = "KIRO_API_KEY"

# SEL audit id for the identity probe below. Registered in
# ``hooks._AUDIT_ONLY_READ_IDS``; an unregistered id fails closed.
_IDC_PROBE_READ_ID = "kiro_cli.idc_identity_probe"

_STATE_DB_TIMEOUT_SECS = 5.0

#: Lowest kiro-cli release observed to ACCEPT a top-level ``permissions`` block in
#: an agent spec.
#:
#: kiro-cli validates agent specs with serde ``deny_unknown_fields``, so a release
#: whose schema lacks the field does not ignore it -- it refuses the WHOLE file,
#: drops the agent from its table, and every Kiro Crew MCP server is silently
#: absent from the session while ``--agent kirocrew`` resolves to the default
#: agent. Observed refusing on 2.10.0 and accepting on 2.23.0.
#:
#: The floor is the release actually PROBED, for the same reason
#: :data:`~kiro_crew.mcp_hot_reload.MCP_HOT_RELOAD_MIN_KIRO_CLI_VERSION` states:
#: lowering it once an older release is verified is a one-line change, while
#: granting it to a release that refuses the field costs the user every tool with
#: nothing red to say why.
SPEC_PERMISSIONS_MIN_VERSION: tuple[int, int, int] = (2, 23, 0)

#: ``--version`` is a local read of an already-resolved binary, so it gets the
#: same short leash the readiness probe puts on its own first execution.
_VERSION_PROBE_TIMEOUT_SECS = 5

#: One answer per binary IDENTITY, not per process: keyed by the pinned path and
#: its mtime, so ``kiro-cli update`` swapping the binary invalidates the entry
#: instead of leaving a whole gateway lifetime on a stale verdict.
_version_cache: dict[tuple[str, int], tuple[int, int, int] | None] = {}


def spec_permissions_supported(version: tuple[int, int, int] | None) -> bool:
    """Pure gate: does a kiro-cli at *version* accept a spec ``permissions`` block?

    An unknown version is not "probably new enough": it is False. The two losses
    are not symmetric. Writing the field on a release that refuses it costs the
    whole spec -- no MCP servers, no tools, no Kiro Crew agent at all.
    Withholding it costs the KAS mode listing for that agent. So the unknown case
    takes the smaller loss.
    """
    if version is None:
        return False
    return version >= SPEC_PERMISSIONS_MIN_VERSION


def installed_kiro_cli_version() -> tuple[int, int, int] | None:
    """The pinned kiro-cli's version, or ``None`` when it cannot be established.

    Blocking: one bounded ``--version`` spawn on the first call per binary
    identity, cached by path and mtime thereafter. The binary comes from
    :func:`pin_kiro_cli` -- an absolute path from the known install directories
    with the inherited ``PATH`` excluded -- because a bare argv0 would be
    re-resolved inside ``exec`` against a ``PATH`` that can lead with an
    agent-writable directory. No pin means no spawn and no answer.

    Never raises. Every failure (absent binary, refused spawn, timeout,
    unparseable output) gives the same answer as an old CLI: unknown, which the
    gate reads as "do not write the field".
    """
    # The ``--version`` line and the handshake's ``agentInfo.version`` are the
    # same spelling, so the parser is shared rather than copied. Imported here
    # because ``mcp_hot_reload`` reaches ``acp_backends``, and this module is a
    # leaf every setup and launch path imports at boot.
    from kiro_crew.mcp_hot_reload import parse_kiro_cli_version  # noqa: PLC0415

    try:
        binary, _unpinned = pin_kiro_cli()
    except Exception:  # noqa: BLE001 - an unanswerable probe is not an error here
        return None
    if binary is None:
        return None
    try:
        key = (binary, os.stat(binary).st_mtime_ns)
    except OSError:
        return None
    if key in _version_cache:
        return _version_cache[key]
    version: tuple[int, int, int] | None = None
    completed: subprocess.CompletedProcess[str] | None
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECS,
            check=False,
            # Pinned UTF-8 rather than bare text=True: a platform-locale decode
            # could mangle the version token and report a supported kiro-cli as
            # unparseable, which this gate reads as refusing.
            **UTF8_TEXT,
        )
    except Exception:  # noqa: BLE001 - see the docstring: unknown, never raising
        completed = None
    if completed is not None and completed.returncode == 0:
        version = parse_kiro_cli_version(completed.stdout or completed.stderr or "")
    _version_cache[key] = version
    return version


def kiro_cli_state_dbs(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    """Return candidate paths to kiro-cli's state database, most likely first.

    Mirrors the per-platform data directories the readiness probe stages from,
    including the ``XDG_DATA_HOME`` / ``LOCALAPPDATA`` redirections, so a host
    with a relocated data dir is not silently treated as having no store. Thin
    wrapper over :func:`identity_stores.state_db_candidates`, which owns the
    canonical per-platform table and the dedupe.
    """
    return identity_stores.state_db_candidates(platform_name, home, environ)


def api_key_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether an API-key credential is configured for kiro-cli.

    Only presence is inspected, never the value. Enterprise MCP governance
    applies to API-key sign-ins as well as IAM Identity Center, so a caller
    deciding whether governance *can* apply has to consider this too — treating
    an API-key account as ungoverned produces advice that breaks a correctly
    configured host.

    Reads the process environment, which by this point also carries anything the
    credential loader lifted out of Kiro Crew's ``.env``.
    """
    env = environ if environ is not None else os.environ
    return bool((env.get(_API_KEY_ENV) or "").strip())


def mcp_governance_may_apply(
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether an administrator's MCP registry can be in force for this identity.

    True for IAM Identity Center and for API-key sign-ins, the two the governance
    surface covers. False for Builder ID and social sign-ins, which
    organization-level MCP controls do not reach.

    Deliberately an OR of two independent signals rather than a single lookup: a
    host can be governed through either, and answering "ungoverned" for the one
    it does not check is what turns a diagnostic into bad advice.
    """
    return signed_in_via_idc(platform_name, home, environ) or api_key_configured(environ)


def signed_in_via_idc(
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether kiro-cli's local state says the identity came from IDC.

    Returns False on any failure. An unreadable or absent store means "cannot
    tell", and inferring an enterprise account from a missing file would put a
    governance warning in front of every personal install.

    The store holds live credential material, so every read of it owes an SEL
    audit event even though this function only ever selects the COUNT of two
    non-secret ``auth.idc.*`` marker rows. Auditing is FAIL-CLOSED: if the audit
    cannot be emitted the probe reports "cannot tell" rather than reading
    unaudited, which costs a diagnostic hint and never a credential.
    """
    # Imported here rather than at module scope: ``hooks`` pulls in the security
    # and governance planes, and this module is imported by lightweight
    # path-resolution callers (including the MCP server entry points) that must
    # not pay for that, nor risk an import cycle through them.
    from kiro_crew.hooks import emit_internal_read_audit

    candidates = kiro_cli_state_dbs(
        platform_name or sys.platform,
        home if home is not None else Path.home(),
        environ if environ is not None else os.environ,
    )
    placeholders = ",".join("?" * len(_IDC_STATE_KEYS))
    for db in candidates:
        connection = _open_state_db_readonly(db)
        if connection is None:
            continue
        try:
            if not emit_internal_read_audit(_IDC_PROBE_READ_ID, "success"):
                # Audit surface unavailable: do not read the store.
                return False
            with connection:
                row = connection.execute(
                    f"SELECT count(*) FROM state WHERE key IN ({placeholders})",
                    _IDC_STATE_KEYS,
                ).fetchone()
            if row and int(row[0]) > 0:
                return True
        except (sqlite3.Error, ValueError):
            continue
        finally:
            connection.close()
    return False


def _open_state_db_readonly(path: Path) -> sqlite3.Connection | None:
    """Open kiro-cli's state store read-only, or return None if it cannot be read.

    Mirrors the readiness probe's gates on the same file: reject a symlink and
    require a regular file, then hand SQLite a read-only URI.

    ``mode=ro`` WITHOUT ``immutable=1``. The immutable flag avoids touching a
    sidecar, but it also tells SQLite the file cannot change, so the WAL is
    IGNORED — and against a store in WAL mode whose newest commits are still in
    ``data.sqlite3-wal`` the identity rows read as absent. A fresh Identity
    Center sign-in would then look like a personal account and silence the very
    governance diagnosis this function exists to trigger. Plain ``mode=ro``
    applies the WAL, so the answer matches what kiro-cli itself would read; the
    cost is that SQLite may create or refresh the ``-shm`` index beside the live
    database exactly as any other reader does, and it holds no identity data.
    """
    try:
        if path.is_symlink():
            return None
        if not stat.S_ISREG(os.lstat(str(path)).st_mode):
            return None
    except OSError:
        return None
    uri = f"file:{urllib.request.pathname2url(str(path))}?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=_STATE_DB_TIMEOUT_SECS)
    except sqlite3.Error:
        return None


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _windows_program_files(environ: Mapping[str, str]) -> str:
    return environ.get("ProgramFiles") or environ.get("PROGRAMFILES") or r"C:\Program Files"


#: The macOS install locations that are FIXED system-wide, in search order, with
#: the user's own bundle inserted after the first entry by
#: :func:`known_kiro_cli_dirs`.
#:
#: Named rather than inlined because these are the ONE part of the candidate set
#: no argument can point elsewhere: every other entry is derived from ``home`` or
#: ``environ`` (the mise shim entry with the exception this function's own
#: docstring records -- ``mise_data_dir`` still honours the PROCESS-level
#: ``MISE_DATA_DIR`` / ``XDG_DATA_HOME``, so it is home-pinned only in their
#: absence), which is what lets a caller pin the set and then report it as the
#: directories that were searched. A test that fakes a host by pinning
#: ``(platform_name, home, environ)`` still gets these three, so on a developer
#: machine with a real install it is asserting about that machine's
#: ``/Applications`` rather than about its own fixture. Emptying this tuple is how
#: such a test fences them; the values themselves are pinned by
#: ``test_kiro_cli_pin.py`` so an empty default can never ship.
_MACOS_SYSTEM_DIRS: tuple[str, ...] = (
    "/Applications/Kiro CLI.app/Contents/MacOS",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


def known_kiro_cli_dirs(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
    *,
    include_inherited_path: bool = True,
) -> list[str]:
    """Return fixed and inherited directories where Kiro CLI may be installed.

    Every home-derived path comes from the ``home`` argument, never from a live
    ``os.path.expanduser("~")``, so a caller that pins ``(platform_name, home,
    environ)`` gets the same account's directories from this function and from
    :func:`find_kiro_cli_candidates`, and may report them as the directories
    that were searched. (:func:`~kiro_crew.env.mise_data_dir` still honours the
    process-level ``MISE_DATA_DIR``/``XDG_DATA_HOME`` overrides, so the mise
    shim entry is home-pinned only in their absence.)
    """

    if platform_name == "win32":
        local_app_data = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        dirs = [
            str(local_app_data / "Kiro-Cli"),
            str(Path(_windows_program_files(environ)) / "Kiro-Cli"),
        ]
    else:
        dirs = [
            str(home / ".local" / "bin"),
            str(home / ".cargo" / "bin"),
        ]
    if platform_name == "darwin":
        # Slicing rather than unpacking: a test fences the fixed locations by
        # emptying ``_MACOS_SYSTEM_DIRS``, and slices are safe at any length while
        # ``head, *rest = ()`` raises. Order is preserved -- the system bundle, the
        # user's own bundle, then the shared bin dirs.
        fixed = list(_MACOS_SYSTEM_DIRS)
        user_app = str(home / "Applications" / "Kiro CLI.app" / "Contents" / "MacOS")
        dirs.extend(fixed[:1] + [user_app] + fixed[1:])
    if include_inherited_path and platform_name == "win32":
        dirs.extend(part for part in environ.get("PATH", "").split(";") if part)
        # A GUI-launched Windows gateway can retain an old PATH after a user
        # installs a CLI. Keep the inherited order, then add the shared set of
        # standard user tool directories and the venv Scripts fallback.
        dirs.extend(part for part in augmented_path("", home=str(home)).split(os.pathsep) if part)
    elif include_inherited_path:
        # `home=` is forwarded for the same reason the win32 branch above does it:
        # `augmented_path` falls back to a LIVE `os.path.expanduser("~")` when the
        # keyword is omitted, so the `{home}`-templated extras and the Node/mise bin
        # dirs would come from the process's account while the `.local/bin` and
        # `.cargo/bin` entries above come from the caller's `home`. That makes this
        # function's result depend on state outside its arguments, which is exactly
        # what the ACP resolver's "the directories named in a not-found message are
        # the directories that were actually searched" contract relies on it NOT
        # doing (see acp/client.py's `_resolve_kiro_cli_for_spawn` docstring).
        dirs.extend(
            part
            for part in augmented_path(environ.get("PATH", ""), home=str(home)).split(os.pathsep)
            if part
        )
    return _unique(dirs)


def find_kiro_cli_candidates(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
    *,
    include_inherited_path: bool = True,
) -> list[str]:
    """Enumerate executable Kiro CLI candidates without mutating the environment."""

    name = f"{KIRO_CLI_NAME}.exe" if platform_name == "win32" else KIRO_CLI_NAME
    candidates: list[str] = []
    override = environ.get("KIROCREW_KIRO_BIN", "")
    if override:
        candidates.append(override)
    candidates.extend(
        str(Path(directory) / name)
        for directory in known_kiro_cli_dirs(
            platform_name,
            home,
            environ,
            include_inherited_path=include_inherited_path,
        )
    )
    result: list[str] = []
    for candidate in _unique(candidates):
        if platform_compat.is_executable_file(candidate, platform_name=platform_name):
            if platform_name == "win32":
                try:
                    if os.path.getsize(candidate) == 0:
                        continue
                except OSError:
                    continue
            result.append(os.path.realpath(candidate) if platform_name == "win32" else candidate)
    return result


def resolve_kiro_cli(
    *,
    platform_name: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    include_inherited_path: bool = True,
) -> str | None:
    """Return the first executable Kiro CLI candidate, if one exists.

    ``include_inherited_path=False`` forwards to
    :func:`find_kiro_cli_candidates` and drops the inherited ``PATH`` from the
    candidate set. What remains is the fixed known install directories plus the
    explicit ``KIROCREW_KIRO_BIN`` override, which is deliberately still
    honoured: it is set by the operator who starts the gateway, not named by a
    directory an agent can plant a file in. Unattended callers pass the keyword
    so a ``PATH`` leading with an agent-writable directory cannot choose what
    they execute; interactive ones keep the default, where a nonstandard install
    on ``PATH`` is a convenience rather than an exposure.
    """

    resolved_platform = platform_name or sys.platform
    resolved_home = home or Path.home()
    resolved_environ = environ if environ is not None else os.environ
    candidates = find_kiro_cli_candidates(
        resolved_platform,
        resolved_home,
        resolved_environ,
        include_inherited_path=include_inherited_path,
    )
    return candidates[0] if candidates else None


#: Fixed wording for the one refusal worth reporting: an install that exists but
#: only through ``PATH``. Named here so ``kirocrew update`` and the diagnostics
#: bundle tell the operator the same thing, including the override that fixes it.
PATH_ONLY_INSTALL_NOTE = (
    "kiro-cli resolves only through PATH, which this spawn does not trust; "
    "point KIROCREW_KIRO_BIN at the binary's absolute path to have it used here"
)


def pin_kiro_cli() -> tuple[str | None, bool]:
    """``(pinned absolute path or None, an unpinned install exists)``.

    The sync pin for a spawn that must not exec a bare argv0. ``argv0`` is
    re-resolved off the inherited ``PATH`` inside ``exec``, and a gateway's
    ``PATH`` can lead with an agent-writable directory (a worktree venv's
    ``bin``), so the candidate set is :func:`resolve_kiro_cli` with
    ``include_inherited_path=False``: the fixed known install directories plus
    the operator's own ``KIROCREW_KIRO_BIN``. ``None`` means refuse — callers
    skip the step rather than fall back to the bare name.

    The second element separates the two ways the pin comes back empty, which
    a caller reports differently: kiro-cli is not installed at all (nothing to
    say — the backend is optional), or it IS installed somewhere the pin does
    not accept, which an operator needs told about, together with
    :data:`PATH_ONLY_INSTALL_NOTE`. The ``PATH``-inclusive lookup that answers
    it only ever decides the wording; it never names what gets spawned.

    Absolute or nothing. The one candidate that can come back relative is the
    override itself (``KIROCREW_KIRO_BIN=kiro-cli``): the existence check would
    pass against the current directory while ``exec`` re-resolved the bare
    argv0 off ``PATH`` — the exact divergence this pin exists to remove. A
    relative pin is therefore refused and reported like a ``PATH``-only
    install, since the note already names the fix.

    Sync and unbounded: it stats directories under the home directory. The
    gateway's unattended paths run this in a thread under a timeout
    (``slack.gateway._pinned_kiro_cli``); a CLI command or a request handler
    already blocking on the spawn itself has nothing to gain from that.
    """

    pinned = resolve_kiro_cli(include_inherited_path=False)
    if pinned is not None and os.path.isabs(pinned):
        return pinned, False
    return None, resolve_kiro_cli() is not None
