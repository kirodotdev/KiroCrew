"""Isolated stdlib launcher for the native Windows sandbox.

Profiles and their ancestors stay pinned until the native launcher exits.
Failures never fall back to an unwrapped process.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import runpy
import stat
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_LAUNCHER_ERROR = 125


@contextlib.contextmanager
def pinned_profile(path: str, expected_sha256: str):
    """Deny write/replacement of the profile and replacement of its ancestors."""
    if os.name != "nt":
        raise OSError("Native Windows sandbox requires Windows")
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)
    kernel = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = []
    target = Path(path)
    try:
        for parent in reversed(target.parents):
            handle = kernel.CreateFileW(
                str(parent),
                _GENERIC_READ,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            if handle == _INVALID_HANDLE_VALUE:
                raise windows_ctypes.WinError(windows_ctypes.get_last_error())
            handles.append(handle)
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise OSError("Sandbox profile ancestor is a reparse point")
        handle = kernel.CreateFileW(
            path,
            _GENERIC_READ,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise windows_ctypes.WinError(windows_ctypes.get_last_error())
        handles.append(handle)
        info = target.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or (getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
            or info.st_nlink != 1
        ):
            raise OSError("Sandbox profile is not an unlinked regular file")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256:
            raise OSError("Sandbox profile changed before launch")
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


@contextlib.contextmanager
def pinned_boundaries(paths: list[str]):
    """Keep existing protected objects and ancestors from being renamed away."""
    if not paths:
        yield
        return
    if os.name != "nt":
        raise OSError("Native Windows sandbox requires Windows")
    from ctypes import wintypes

    windows_ctypes = cast(Any, ctypes)
    kernel = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handles = []
    seen = set()
    try:
        for raw in paths:
            target = Path(raw)
            for path in [*reversed(target.parents), target]:
                key = str(path).casefold()
                if key in seen:
                    continue
                handle = kernel.CreateFileW(
                    str(path),
                    _GENERIC_READ,
                    _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    None,
                    _OPEN_EXISTING,
                    _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
                    None,
                )
                if handle == _INVALID_HANDLE_VALUE:
                    error = windows_ctypes.get_last_error()
                    if error in {2, 3}:
                        break
                    raise windows_ctypes.WinError(error)
                handles.append(handle)
                info = path.lstat()
                if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT:
                    raise OSError("Protected Windows sandbox path contains a reparse point")
                seen.add(key)
        yield
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def verify_identities(identities: list[list]) -> None:
    """Check carried mask/private-directory identities immediately before launch."""
    for identity in identities:
        path, device, inode = identity[:3]
        directory = identity[3] if len(identity) > 3 else True
        info = os.lstat(path)
        correct_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not correct_kind or (
            getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError("Sandbox directory was replaced by a reparse point")
        if (info.st_dev, info.st_ino) != (device, inode):
            raise OSError("Sandbox directory changed after approval")


def verify_writable_ancestry(immutable_roots: Iterable[str], writable_paths: Iterable[str]) -> None:
    """Compare objects, so short names cannot carve writes through immutable roots."""
    paths = tuple(writable_paths)
    if not paths:
        return
    sealed: set[tuple[int, int]] = set()
    for root in immutable_roots:
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            continue
        sealed.add((info.st_dev, info.st_ino))
    if not sealed:
        return
    for raw in paths:
        path = Path(raw)
        for ancestor in (path, *path.parents):
            info = os.lstat(ancestor)
            if (info.st_dev, info.st_ino) in sealed:
                raise OSError("Writable Windows window enters an immutable root")


def child_environment(prefixes: list[str], denied_names: list[str]) -> dict[str, str]:
    """Remove secret variables before starting the trusted native helper."""
    upper_prefixes = tuple(prefix.upper() for prefix in prefixes)
    upper_names = {name.upper() for name in denied_names}
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in upper_names and not key.upper().startswith(upper_prefixes)
    }


def windows_native_spawn_argv(
    executable: str, profile: str, digest: str, cwd: str, argv: list[str]
) -> list[str]:
    """Native confinement chokepoint: one complete policy, never configuration layers."""
    return [
        executable,
        "--policy-file",
        profile,
        "--policy-sha256",
        digest,
        "--cwd",
        cwd,
        "--",
        *argv,
    ]


def _run_native(command: list[str], cwd: str, env: dict[str, str], native: dict | None) -> int:
    if not native:
        return subprocess.run(command, cwd=cwd, env=env, check=False).returncode
    bootstrap = (
        "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
        "runpy.run_module('kiro_crew.mcp_gateway.native_handles',run_name='__main__')"
    )
    helper = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            bootstrap,
            str(Path(__file__).resolve().parent.parent),
            "--descriptor",
            native["descriptor"],
            "--address",
            native["address"],
            "--instance",
            native["instance"],
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    workload = None
    try:
        workload = subprocess.Popen(command, cwd=cwd, env=env)
        while workload.poll() is None:
            if helper.poll() is not None:
                raise OSError("Native MCP handle service exited")
            time.sleep(0.02)
        return workload.wait()
    finally:
        # Both are our own children. The native runner's job owns its entire
        # descendant tree; closing that runner also closes the kernel job.
        if workload is not None and workload.poll() is None:
            workload.terminate()
            workload.wait()
        if helper.stdin is not None:
            helper.stdin.close()
        try:
            helper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            helper.terminate()
            helper.wait()


def main(payload: dict) -> int:
    """Run one native command, inheriting pipes and preserving its exit status."""
    cleanup_profile = None
    try:
        with contextlib.ExitStack() as stack:
            if payload.get("settings_gate"):
                settings = runpy.run_path(str(Path(__file__).with_name("windows_settings.py")))
                gate = Path(payload["settings_gate"])
                stack.enter_context(settings["read_gate"](gate))
                settings["verify_snapshot"](gate, payload["settings_snapshot"])
            stack.enter_context(pinned_boundaries(payload.get("protected_paths", [])))
            stack.enter_context(pinned_profile(payload["profile_path"], payload["profile_sha256"]))
            profile = Path(payload["profile_path"])
            if profile.name.startswith("kirocrew_native_") and profile.name.endswith(".json"):
                cleanup_profile = profile
            verify_identities(payload["identities"])
            verify_writable_ancestry(
                payload.get("immutable_roots", []), payload.get("writable_paths", [])
            )
            command = payload["command"]
            workload = command[command.index("--") + 1 :]
            expected = windows_native_spawn_argv(
                command[0],
                payload["profile_path"],
                payload["profile_sha256"],
                payload["cwd"],
                workload,
            )
            if command != expected:
                raise ValueError("Native sandbox flags were changed")
            cwd = os.getcwd() if payload.get("inherit_cwd") else payload["cwd"]
            if payload.get("inherit_cwd"):
                expected = windows_native_spawn_argv(
                    command[0], payload["profile_path"], payload["profile_sha256"], cwd, workload
                )
            return _run_native(
                expected,
                cwd,
                child_environment(payload["scrub_prefixes"], payload["scrub_names"]),
                payload.get("native_mcp"),
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Do not echo argv, environment values or protected filenames.
        code = getattr(exc, "winerror", None)
        print(
            f"Kiro Crew Windows sandbox refused launch ({type(exc).__name__}, winerror={code})",
            file=sys.stderr,
        )
        return _LAUNCHER_ERROR
    finally:
        native = payload.get("native_mcp")
        if native:
            descriptor = Path(native["descriptor"])
            try:
                raw = descriptor.read_text("ascii")
                if not raw or json.loads(raw).get("instance") == native["instance"]:
                    descriptor.unlink()
            except (OSError, ValueError, KeyError):
                # The runtime's scratch owner retries after confirmed tree exit.
                pass
        if cleanup_profile is not None:
            try:
                cleanup_profile.unlink(missing_ok=True)
            except OSError:
                # The caller owns a second cleanup attempt after reaping us.
                pass


def entrypoint(arguments: list[str]) -> int:
    """Read a pinned launch request, keeping large policies off the command line."""
    cleanup = None
    try:
        if len(arguments) == 3 and arguments[0] == "--request-file":
            request = Path(arguments[1])
            if request.stat().st_size > 1_048_576:
                raise ValueError("Launch metadata exceeds its size limit")
            with pinned_profile(str(request), arguments[2]):
                if request.name.startswith("kirocrew_native_") and request.suffix == ".json":
                    cleanup = request
                payload = json.loads(request.read_bytes())["launch"]
                payload["profile_path"] = str(request)
                payload["profile_sha256"] = arguments[2]
                payload["command"] = windows_native_spawn_argv(
                    payload.pop("executable"),
                    str(request),
                    arguments[2],
                    payload["cwd"],
                    payload.pop("argv"),
                )
                return main(payload)
        if len(arguments) == 1:
            return main(json.loads(arguments[0]))
        return _LAUNCHER_ERROR
    except (OSError, ValueError, TypeError, KeyError):
        print("Kiro Crew Windows sandbox refused launch metadata", file=sys.stderr)
        return _LAUNCHER_ERROR
    finally:
        if cleanup is not None:
            try:
                cleanup.unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(entrypoint(sys.argv[1:]))
