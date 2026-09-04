"""Side-effect-free Kiro CLI discovery shared by setup and ACP launch paths."""

from __future__ import annotations

import os
import stat
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from kiro_crew import identity_stores, platform_compat
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.env import augmented_path

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


def known_kiro_cli_dirs(
    platform_name: str,
    home: Path,
    environ: Mapping[str, str],
    *,
    include_inherited_path: bool = True,
) -> list[str]:
    """Return fixed and inherited directories where Kiro CLI may be installed."""

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
        dirs.extend(
            [
                "/Applications/Kiro CLI.app/Contents/MacOS",
                str(home / "Applications" / "Kiro CLI.app" / "Contents" / "MacOS"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
            ]
        )
    if include_inherited_path and platform_name == "win32":
        dirs.extend(part for part in environ.get("PATH", "").split(";") if part)
        # A GUI-launched Windows gateway can retain an old PATH after a user
        # installs a CLI. Keep the inherited order, then add the shared set of
        # standard user tool directories and the venv Scripts fallback.
        dirs.extend(part for part in augmented_path("", home=str(home)).split(os.pathsep) if part)
    elif include_inherited_path:
        dirs.extend(
            part for part in augmented_path(environ.get("PATH", "")).split(os.pathsep) if part
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
