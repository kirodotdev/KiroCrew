"""OpenCode executable discovery and its non-interactive model catalog.

The ACP spawn and SDK install probe share this resolver. The cold catalog stays
behind the SDK driver, so no dashboard handler needs a new ACP import.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Mapping

from kiro_crew import platform_compat
from kiro_crew.env import augmented_path

logger = logging.getLogger(__name__)

OPENCODE_BIN = "opencode"
OPENCODE_BIN_ENV = "OPENCODE_BIN"
OPENCODE_NPM_PKG = "opencode-ai"
MODEL_LIST_TIMEOUT_SECS = 10.0
MODEL_LIST_MAX_BYTES = 1_000_000
_MODEL_LIST_STDERR_TAIL_BYTES = 1000
_MODEL_ID_RE = re.compile(r"^[^\s/\x00-\x1f\x7f]+/[^\s\x00-\x1f\x7f]+$")
_READ_CHUNK_BYTES = 8192
_AUTH_DATA_HOME_ENV = "XDG_DATA_HOME"


def validate_opencode_env(env: Mapping[str, str]) -> None:
    """Keep native credential paths aligned with the host's declared anchors.

    OpenCode reads XDG_DATA_HOME verbatim, relative to its child cwd, whereas
    the host's credential floor expands home syntax and anchors on its own cwd.
    Refuse those ambiguous inputs instead of silently moving a credential store.
    An absent or empty override keeps OpenCode's home-relative default. Absolute
    paths, including meaningful whitespace, are left byte-for-byte unchanged.
    """
    root = env.get(_AUTH_DATA_HOME_ENV, "")
    if root and ("\x00" in root or not os.path.isabs(root)):
        raise ValueError("OpenCode XDG_DATA_HOME must be an absolute path without NUL bytes")


def resolve_opencode_bin() -> str | None:
    """Resolve the CLI from an override, mise, its install home, or daemon PATH.

    Preserve the discovered launch path: resolving symlinks can break a shim
    that dispatches by its own name or reads sibling resources.
    """
    from kiro_crew.acp.client import _mise_which, _normalize_exe_casing

    override = os.environ.get(OPENCODE_BIN_ENV)
    if override and platform_compat.is_executable_file(override):
        return _normalize_exe_casing(str(Path(override).absolute()))
    mise_resolved = _mise_which(OPENCODE_BIN)
    if mise_resolved and platform_compat.is_executable_file(mise_resolved):
        return _normalize_exe_casing(mise_resolved)
    binary_name = f"{OPENCODE_BIN}.exe" if platform_compat.IS_WINDOWS else OPENCODE_BIN
    installed = Path.home() / ".opencode" / "bin" / binary_name
    if platform_compat.is_executable_file(installed):
        return _normalize_exe_casing(str(installed))
    search_path = augmented_path(os.environ.get("PATH", ""))
    return _normalize_exe_casing(shutil.which(OPENCODE_BIN, path=search_path))


def model_rows(stdout: bytes) -> list[dict[str, str]]:
    """Parse the CLI's provider-qualified ids, rejecting diagnostic output."""
    if len(stdout) > MODEL_LIST_MAX_BYTES:
        raise ValueError("opencode model list exceeded the output limit")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in stdout.decode("utf-8").splitlines():
        model_id = line.strip()
        if not model_id:
            continue
        if not _MODEL_ID_RE.fullmatch(model_id):
            raise ValueError("opencode model list contained an invalid model id")
        if model_id not in seen:
            seen.add(model_id)
            rows.append({"model_name": model_id, "display_name": model_id, "description": ""})
    if not rows:
        raise ValueError("opencode model list was empty")
    return rows


def _discard_sandbox(path: str | None) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            logger.debug("Could not remove OpenCode model-list sandbox file", exc_info=True)


def _model_list_env() -> dict[str, str]:
    from kiro_crew.acp.client import _resolve_ssh_auth_sock
    from kiro_crew.config.loader import strip_kiro_cli_api_key
    from kiro_crew.sandbox import scrub_agent_subprocess_env

    env = dict(os.environ)
    env["PATH"] = augmented_path(env.get("PATH", ""))
    _resolve_ssh_auth_sock(env)
    strip_kiro_cli_api_key(env)
    return scrub_agent_subprocess_env(env)


async def _model_list_output(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Drain both pipes concurrently, retaining bounded data even on failures."""

    async def _drain(stream: asyncio.StreamReader | None, *, tail: bool) -> bytes:
        if stream is None:
            return b""
        buf = bytearray()
        cap = _MODEL_LIST_STDERR_TAIL_BYTES if tail else MODEL_LIST_MAX_BYTES + 1
        while True:
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return bytes(buf)
            if tail:
                buf.extend(chunk)
                del buf[:-cap]
            elif len(buf) < cap:
                buf.extend(chunk[: cap - len(buf)])

    drains = [
        asyncio.create_task(_drain(proc.stdout, tail=False)),
        asyncio.create_task(_drain(proc.stderr, tail=True)),
    ]
    try:
        out, err = await asyncio.gather(*drains)
        await proc.wait()
        return out, err
    finally:
        for task in drains:
            if not task.done():
                task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


async def query_available_opencode_models(*, work_dir: str | None = None) -> list[dict[str, str]]:
    """Read the configured catalog without an ACP session or a model prompt.

    Installation is not admission: this read neither changes selectability nor
    claims that OpenCode's tool-permission channel is safe to execute.
    """
    from kiro_crew.executors import subprocess_executor
    from kiro_crew.sandbox import (
        cgroup_scope_argv,
        configured_sandbox_mode,
        create_subprocess_limited,
        shielded_prepare_off_loop,
        wrap_argv,
    )

    opencode_bin = await asyncio.to_thread(resolve_opencode_bin)
    if not opencode_bin:
        raise FileNotFoundError("opencode executable not resolved")
    loop = asyncio.get_running_loop()

    def prepare() -> tuple[list[str], dict[str, str], str | None]:
        argv, cleanup = wrap_argv(
            [opencode_bin, "models"],
            mode=configured_sandbox_mode(),
            strip_python_env=True,
            is_kiro_cli=False,
        )
        return argv, {}, cleanup

    # A cancelled caller must settle the worker and retire the profile it creates.
    argv, _unused_env, cleanup = await shielded_prepare_off_loop(
        prepare, executor=subprocess_executor()
    )
    try:
        argv = await loop.run_in_executor(subprocess_executor(), cgroup_scope_argv, argv)
        env = await asyncio.to_thread(_model_list_env)
        validate_opencode_env(env)
        proc = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP | platform_compat._SUBPROCESS_NO_WINDOW
            ),
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                _model_list_output(proc), timeout=MODEL_LIST_TIMEOUT_SECS
            )
        except BaseException:
            await platform_compat.kill_and_reap(proc)
            raise
    finally:
        await loop.run_in_executor(subprocess_executor(), _discard_sandbox, cleanup)
    if proc.returncode != 0:
        from kiro_crew.platform import redact_via_context

        logger.warning(
            "OpenCode model list exited %s: %s",
            proc.returncode,
            redact_via_context(stderr.decode("utf-8", errors="replace")).strip(),
        )
        raise RuntimeError("opencode model list command failed")
    return model_rows(stdout)
