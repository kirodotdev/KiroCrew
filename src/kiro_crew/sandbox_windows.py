"""Opt-in native Windows backend using Microsoft's MXC SDK through a fixed runner.

The complete Crew policy is pinned, hashed and passed as one JSON document.
No Codex configuration, administrator setup, ACL mutation or fallback is used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from kiro_crew import platform_compat, sandbox_argv, sandbox_plan
from kiro_crew import sandbox_windows_launcher as _windows_launcher
from kiro_crew.sandbox_seals import SandboxCeilingUnsealable
from kiro_crew.sandbox_unavailable import SandboxUnavailableError
from kiro_crew.sandbox_windows_policy import (
    WindowsPolicyUnsupported,
    permission_entries,
    render_permissions,
    windows_literal_path,
)

WINDOWS_SANDBOX_EXE_ENV = "KIROCREW_WINDOWS_SANDBOX_EXE"
WINDOWS_BACKEND = "windows-mxc"
_PROBE_TIMEOUT_SECONDS = 20
_MAX_COMMAND_UTF16 = 30000
logger = logging.getLogger("kiro_crew.sandbox")

# Synthetic data only. Every check runs inside the native sandbox; a launcher
# that silently ignores permissions cannot produce the success marker.
_PROBE_PROGRAM = r"""
import pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
def denied(operation):
    try:
        operation()
    except (PermissionError, FileNotFoundError):
        return
    raise RuntimeError("permission not enforced")
hidden = root / "hidden"
denied(lambda: (root / "future-denied").mkdir())
denied(lambda: (hidden / "secret").read_text())
denied(lambda: (hidden / "secret").write_text("changed"))
assert (hidden / "config").read_text() == "visible"
denied(lambda: (hidden / "config").write_text("changed"))
window = hidden / "window"
assert (window / "visible").read_text() == "visible"
(window / "created").write_text("ok")
denied(lambda: (window / "remasked" / "secret").read_text())
governance = root / "governance"
assert (governance / "policy").read_text() == "ceiling"
denied(lambda: (governance / "policy").write_text("changed"))
denied(lambda: governance.rename(root / "moved-governance"))
(root / "allowed").write_text("ok")
child = "import pathlib,sys; p=pathlib.Path(sys.argv[1]);\ntry: p.read_text()\nexcept (PermissionError,FileNotFoundError): sys.exit(0)\nsys.exit(91)"
assert subprocess.run([sys.executable, "-I", "-S", "-c", child, str(hidden / "secret")]).returncode == 0
print("KIROCREW_MXC_CONFINEMENT_OK", flush=True)
"""


def windows_sandbox_executable() -> str | None:
    """Require an explicit, absolute native MXC runner, never a shell shim."""
    if sys.platform != "win32":
        return None
    value = os.environ.get(WINDOWS_SANDBOX_EXE_ENV, "")
    if not value or not os.path.isabs(value) or Path(value).suffix.lower() != ".exe":
        return None
    return os.path.abspath(value) if os.path.isfile(value) else None


def _windows_trusted_roots(executable: str) -> tuple[str, ...]:
    """Protect code that runs before the workload's native security boundary."""
    return tuple(
        dict.fromkeys(
            (
                str(Path(executable).absolute().parent),
                str(Path(sys.executable).absolute().parent),
                *(
                    str(Path(prefix).absolute())
                    for prefix in (
                        sys.prefix,
                        sys.base_prefix,
                        sys.exec_prefix,
                        sys.base_exec_prefix,
                    )
                ),
                str(Path(__file__).absolute().parent),
            )
        )
    )


def _windows_native_command(
    executable: str, profile: Path, digest: str, cwd: str, argv: list[str]
) -> list[str]:
    return _windows_launcher.windows_native_spawn_argv(executable, str(profile), digest, cwd, argv)


def _windows_policy_dir() -> Path:
    return Path(sandbox_argv._ensure_run_dir())


def _windows_policy_path_heads(entries: dict[str, str]) -> dict[str, str]:
    """Observe the first absent component without creating provider directories."""
    heads = {}
    for value in entries:
        if value == ":root":
            continue
        target = Path(value)
        cursor = target
        first_missing = None
        while True:
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                first_missing = cursor
                parent = cursor.parent
                if parent == cursor:
                    raise WindowsPolicyUnsupported("Windows policy drive is unavailable")
                cursor = parent
                continue
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise WindowsPolicyUnsupported("Windows policy ancestor is a reparse point")
            if cursor != target and not stat.S_ISDIR(info.st_mode):
                raise WindowsPolicyUnsupported("Windows policy ancestor is not a directory")
            break
        if first_missing is not None:
            heads[value] = first_missing.as_posix()
    return heads


def _stage_windows_profile(entries: dict[str, str], *, network: bool) -> tuple[Path, str]:
    descriptor, filename = tempfile.mkstemp(
        prefix=f"{sandbox_argv._WINDOWS_SANDBOX_ARTIFACT_PREFIX}{os.getpid()}_",
        suffix=".json",
        dir=_windows_policy_dir(),
    )
    profile = Path(filename)
    try:
        rules = dict(entries)
        rules[windows_literal_path(profile.as_posix())] = "read"
        contents = render_permissions(rules, network=network).encode("utf-8")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(contents)
        return profile, hashlib.sha256(contents).hexdigest()
    except BaseException:
        if descriptor != -1:
            os.close(descriptor)
        profile.unlink(missing_ok=True)
        raise


def _windows_launcher_argv(
    executable: str,
    profile: Path,
    digest: str,
    cwd: str,
    argv: list[str],
    *,
    prefixes: tuple[str, ...] = (),
    identities: tuple[tuple, ...] = (),
    protected_paths: tuple[str, ...] = (),
    immutable_roots: tuple[str, ...] = (),
    writable_paths: tuple[str, ...] = (),
    native_mcp: dict[str, str] | None = None,
    settings_gate: str | None = None,
    settings_snapshot: dict[str, str | None] | None = None,
) -> list[str]:
    payload = {
        "executable": executable,
        "argv": argv,
        "cwd": cwd,
        "inherit_cwd": True,
        "scrub_prefixes": prefixes,
        "scrub_names": [],
        "identities": identities,
        "protected_paths": protected_paths,
        "immutable_roots": immutable_roots,
        "writable_paths": writable_paths,
        "native_mcp": native_mcp,
        "settings_gate": settings_gate,
        "settings_snapshot": settings_snapshot,
    }
    native_size = (
        len(
            subprocess.list2cmdline(
                _windows_native_command(executable, profile, digest, cwd, argv)
            ).encode("utf-16-le")
        )
        // 2
    )
    if native_size >= _MAX_COMMAND_UTF16:
        raise WindowsPolicyUnsupported("Windows sandbox command exceeds the native command limit")
    policy_bytes = profile.read_bytes()
    if hashlib.sha256(policy_bytes).hexdigest() != digest:
        raise WindowsPolicyUnsupported("Windows policy changed before launch metadata publication")
    document = json.loads(policy_bytes)
    if native_mcp is not None:
        document["process_descriptor"] = {
            "path": native_mcp["descriptor"],
            "instance": native_mcp["instance"],
        }
    document["launch"] = payload
    contents = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(contents) > 1_048_576:
        raise WindowsPolicyUnsupported("Windows sandbox launch metadata exceeds its size limit")
    wrapped = [
        sys.executable,
        "-I",
        "-S",
        str(Path(__file__).with_name("sandbox_windows_launcher.py")),
        "--request-file",
        str(profile),
        hashlib.sha256(contents).hexdigest(),
    ]
    if len(subprocess.list2cmdline(wrapped).encode("utf-16-le")) // 2 >= _MAX_COMMAND_UTF16:
        raise WindowsPolicyUnsupported("Windows sandbox command exceeds the native command limit")
    # The caller owns exactly this one artifact, including before a child exists.
    # No sidecar can outlive cancellation of the off-loop preparation.
    profile.write_bytes(contents)
    return wrapped


def _cleanup_windows_profile(profile: Path) -> None:
    profile.unlink(missing_ok=True)


def _native_mcp_metadata(
    hidden: tuple[str, ...], private: tuple[str, ...]
) -> dict[str, str] | None:
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.mcp_gateway.native_scope import DESCRIPTOR_NAME
    from kiro_crew.mcp_gateway.rewriter import default_socket_path
    from kiro_crew.mcp_gateway.transport import resolve_address

    if not private or data_home() / "trust" not in {Path(value) for value in hidden}:
        return None
    gateway = KiroCrewConfig.load().mcp_gateway
    # Final session overlays can add managed stubs after launch planning. The
    # channel is inert without a host-signed scope, so prepare it for every
    # private runtime whose trust material is masked, independent of the roster.
    descriptor = Path(private[0]) / DESCRIPTOR_NAME
    if descriptor.exists():
        raise WindowsPolicyUnsupported("Previous native MCP runtime has not been retired")
    return {
        "descriptor": windows_literal_path(descriptor.as_posix()),
        "address": resolve_address(gateway.socket_path or default_socket_path()),
        "instance": secrets.token_hex(32),
    }


def probe_windows_sandbox(*, diagnostics: list[str] | None = None) -> bool:
    """Prove actual denies, seals, nested windows and descendant confinement."""
    executable = windows_sandbox_executable()
    if executable is None:
        if diagnostics is not None:
            diagnostics.append("runner-not-configured")
        return False
    profile = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="kirocrew-native-probe-", dir=sandbox_argv._ensure_run_dir()
        ) as directory:
            root = Path(directory)
            hidden = root / "hidden"
            window = hidden / "window"
            remasked = window / "remasked"
            governance = root / "governance"
            for folder in (remasked, governance):
                folder.mkdir(parents=True)
            for file, value in (
                (hidden / "secret", "synthetic"),
                (hidden / "config", "visible"),
                (window / "visible", "visible"),
                (remasked / "secret", "synthetic"),
                (governance / "policy", "ceiling"),
            ):
                file.write_text(value, encoding="utf-8")
            rules = {
                ":root": "write",
                hidden.as_posix(): "deny",
                (root / "future-denied" / "nested" / "secret").as_posix(): "deny",
                (hidden / "config").as_posix(): "read",
                window.as_posix(): "write",
                remasked.as_posix(): "deny",
                governance.as_posix(): "read",
            }
            rules.update(
                {windows_literal_path(path): "read" for path in _windows_trusted_roots(executable)}
            )
            rules = sandbox_plan.project_windows_permissions(
                rules, _windows_policy_path_heads(rules)
            )
            profile, digest = _stage_windows_profile(rules, network=False)
            command = _windows_launcher_argv(
                executable,
                profile,
                digest,
                str(root),
                [sys.executable, "-I", "-S", "-c", _PROBE_PROGRAM, str(root)],
                protected_paths=tuple(p for p, access in rules.items() if p != ":root"),
                immutable_roots=tuple(p for p, access in rules.items() if access == "read"),
                writable_paths=(str(window),),
            )
            with subprocess.Popen(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                try:
                    stdout, _stderr = process.communicate(timeout=_PROBE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    if diagnostics is not None:
                        diagnostics.append("probe-timeout")
                    platform_compat.kill_popen_tree(process)
                    process.communicate(timeout=5)
                    return False
                passed = process.returncode == 0 and (
                    stdout.strip() == b"KIROCREW_MXC_CONFINEMENT_OK"
                )
                if not passed:
                    if diagnostics is not None:
                        if b"native MXC is unavailable" in _stderr:
                            reason = "native-mxc-unavailable-in-this-executor"
                        elif b"native filesystem policy unavailable" in _stderr:
                            reason = "native-filesystem-policy-unavailable"
                        elif b"native Windows policy refused" in _stderr:
                            reason = "native-policy-input-refused"
                        else:
                            reason = "native-confinement-probe-failed"
                        diagnostics.append(reason)
                    logger.warning(
                        "Native Windows sandbox did not pass confinement probe (exit=%s)",
                        process.returncode,
                    )
                return passed
    except (OSError, ValueError, subprocess.SubprocessError):
        if diagnostics is not None:
            diagnostics.append("probe-launch-failed")
        logger.warning("Native Windows sandbox confinement probe was unavailable")
        return False
    finally:
        if profile is not None:
            _cleanup_windows_profile(profile)


def _windows_file_exposures(
    plan: sandbox_plan.ConfinementPlan,
) -> tuple[sandbox_plan.ConfinementPlan, list[tuple[str, int, int, bool]]]:
    """A literal read grant must never reopen a directory instead of a file."""
    exposed = []
    identities = []

    def validate(path: str) -> bool:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_nlink != 1
        ):
            raise WindowsPolicyUnsupported("Only unlinked regular files may be re-exposed")
        identities.append((path, info.st_dev, info.st_ino, False))
        return True

    for path, name in plan.expose:
        if validate(path):
            exposed.append((path, name))
    known_hosts = plan.ssh_known_hosts
    if plan.hide_ssh and (not known_hosts or not validate(known_hosts)):
        known_hosts = ""
    return replace(plan, expose=tuple(exposed), ssh_known_hosts=known_hosts), identities


def _windows_window_identities(plan: sandbox_plan.ConfinementPlan):
    _windows_launcher.verify_writable_ancestry(
        plan.immutable_roots, (*plan.windows, *plan.writable)
    )
    identities = []
    for path in dict.fromkeys((*plan.windows, *plan.writable)):
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise WindowsPolicyUnsupported("A private or writable window must be a real directory")
        identities.append((path, info.st_dev, info.st_ino, True))
    return identities


def windows_sandbox_argv(
    argv: list[str],
    sandbox_level: str = "strict",
    *,
    strip_python_env: bool = False,
    forward_ssh_auth_sock: bool = False,
    extra_hidden_dirs: tuple[str, ...] = (),
    seal_hidden_dirs: bool = False,
    extra_hidden_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_alias_credential_ids: tuple[tuple[int, int], ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_private_dir_ids: tuple[tuple[str, int, int], ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
    exec_cwd: str = "",
) -> tuple[list[str], str]:
    """Build the common plan and return a pinned native launch plus its cleanup file."""
    executable = windows_sandbox_executable()
    if executable is None:
        raise SandboxUnavailableError(
            "Native Windows MXC runner is unavailable.",
            kind="no_backend",
            detail="windows-mxc-not-configured",
        )
    profile = None
    preparation = contextlib.ExitStack()
    try:
        if not argv:
            raise WindowsPolicyUnsupported("Cannot sandbox an empty Windows command")
        if extra_alias_credential_ids:
            raise WindowsPolicyUnsupported(
                "Inode-only credential aliases need explicit Windows paths"
            )
        if exec_cwd:
            # This parameter promises a sealed, empty launch directory, not a
            # generic cwd change. MXC has no equivalent descriptor handshake.
            raise WindowsPolicyUnsupported("Sealed empty execution directories are unsupported")
        required: list[str] = []
        sandbox_argv._materialize_sealable_ceilings(required)
        sandbox_argv._materialize_maskable_dirs(required)
        sandbox_argv._refuse_linked_models_leaf()
        sandbox_argv._sweep_legacy_md_notebook_temps()
        sandbox_argv._migrate_md_notebook_legacy_state()
        sandbox_argv._materialize_md_notebook_mask_targets(required)
        sandbox_argv._materialize_live_target_mask_target(required)
        sandbox_argv._sweep_legacy_auth_store_temps()
        sandbox_argv._reconcile_auth_store_staging_links()
        aliases = sandbox_argv._refuse_multilinked_credential_leaves(masks_are_path_only=True)
        occupants: dict[str, tuple[int, ...]] = {}
        masked_aliases = sandbox_argv._refuse_aliased_masked_leaves(occupants)
        if aliases or masked_aliases:
            raise WindowsPolicyUnsupported(
                "Credential hard-link aliases cannot be silently omitted"
            )
        from kiro_crew import windows_settings

        # Config loading may migrate before a read lease is held. Bind those
        # derived metadata to the same generation as the subsequent plan; a
        # writer (or migration) in this gap requires a fresh launch.
        for _ in range(2):
            settings_snapshot = windows_settings.snapshot()
            native_mcp = _native_mcp_metadata(extra_hidden_dirs, extra_private_dirs)
            if windows_settings.snapshot() == settings_snapshot:
                break
        else:
            raise windows_settings.NativeSettingsRestartRequired("stale_start")
        settings_gate = windows_settings.prepare_gate()
        preparation.enter_context(windows_settings.read_gate(settings_gate))
        windows_settings.verify_snapshot(settings_gate, settings_snapshot)
        plan = sandbox_argv._spawn_plan(
            sandbox_plan.BACKEND_WINDOWS,
            sandbox_level,
            strip_python_env=strip_python_env,
            forward_ssh_auth_sock=forward_ssh_auth_sock,
            extra_hidden_dirs=extra_hidden_dirs,
            seal_hidden_dirs=seal_hidden_dirs,
            extra_hidden_dir_ids=extra_hidden_dir_ids,
            extra_visible_dirs=extra_visible_dirs,
            extra_private_dirs=extra_private_dirs,
            extra_private_dir_ids=extra_private_dir_ids,
            extra_writable_dirs=extra_writable_dirs,
            extra_expose_files=extra_expose_files,
            runtime_install_roots=_windows_trusted_roots(executable),
        )
        if plan.refusals:
            raise WindowsPolicyUnsupported("The Windows confinement plan contains refused requests")
        plan, file_identities = _windows_file_exposures(plan)
        original_rules = permission_entries(plan)
        if native_mcp is not None:
            original_rules[native_mcp["descriptor"]] = "read"
        rules = sandbox_plan.project_windows_permissions(
            original_rules, _windows_policy_path_heads(original_rules)
        )
        profile, digest = _stage_windows_profile(rules, network=True)
        identities = [(*item, True) for item in (*extra_hidden_dir_ids, *extra_private_dir_ids)]
        identities.extend(file_identities)
        identities.extend(_windows_window_identities(plan))
        for target in required:
            info = os.lstat(target)
            identities.append((target, info.st_dev, info.st_ino, os.path.isdir(target)))
        for target, occupant in occupants.items():
            if occupant[2]:
                raise WindowsPolicyUnsupported("Protected Windows aliases must be unlinked")
            identities.append((target, occupant[0], occupant[1], os.path.isdir(target)))
        wrapped = _windows_launcher_argv(
            executable,
            profile,
            digest,
            os.getcwd(),
            list(argv),
            prefixes=plan.env_scrub_prefixes,
            identities=tuple(identities),
            protected_paths=tuple(
                dict.fromkeys(p for p in (*original_rules, *rules) if p != ":root")
            ),
            immutable_roots=plan.immutable_roots,
            writable_paths=tuple(dict.fromkeys((*plan.windows, *plan.writable))),
            native_mcp=native_mcp,
            settings_gate=str(settings_gate),
            settings_snapshot=settings_snapshot,
        )
        return wrapped, str(profile)
    except (OSError, ValueError, SandboxCeilingUnsealable) as exc:
        if profile is not None:
            _cleanup_windows_profile(profile)
        raise SandboxUnavailableError(
            "Native Windows sandbox cannot enforce this launch; no process was started.",
            kind="no_backend",
            detail="windows-mxc-policy-unsupported",
        ) from exc
    finally:
        preparation.close()
