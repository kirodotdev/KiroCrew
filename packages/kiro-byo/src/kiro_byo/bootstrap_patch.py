"""Crew Gateway boot environment and activation marker (no source self-repair).

The retired 0.10 startup writer was unsafe to reuse with Crew 0.12. Only the
App's transactional source-contracted installer may change Crew files.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_STATE_NAME = "_bootstrap_state.json"
_PREFIX = "src/kiro_crew/"


def _bind_runtime_environment() -> None:
    """Bind the ACP child to Crew's interpreter *and* the App-owned data home.

    The App copies ``kiro_byo`` beside the running Kiro Crew package.  A bare
    ``python`` on PATH is therefore not a safe launch target on Windows: it may
    be another installation that cannot import this runtime.  Likewise the ACP
    child must not guess where provider metadata lives; the Kiro Crew App data
    directory is authoritative and is recorded in ``_bootstrap_state.json``.

    The .pth hook is imported by Crew's own interpreter on every fresh gateway
    start.  Publish both facts into the gateway environment so Kiro Crew's normal
    subprocess environment copy carries them into the self-served ACP process.
    Explicit valid operator overrides are preserved.
    """
    current = os.environ.get("KIRO_BYO_BIN", "").strip()
    if not (current and os.path.isfile(current)):
        executable = str(Path(sys.executable).resolve()) if sys.executable else ""
        if executable and os.path.isfile(executable):
            os.environ["KIRO_BYO_BIN"] = executable

    if os.environ.get("KIRO_BYO_HOME", "").strip():
        return
    state_path = Path(__file__).resolve().parent / _STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    raw_home = state.get("kiro_byo_home", "") if isinstance(state, dict) else ""
    if isinstance(raw_home, str) and raw_home.strip():
        home = Path(raw_home.strip()).expanduser()
        if home.exists():
            os.environ["KIRO_BYO_HOME"] = str(home)


def _atomic_write_text(text: str, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".kiro-byo-", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def repair() -> int:
    """No bootstrap source edits for 0.12.

    All 0.12 seam validation and writes are owned by the transactional App
    installer. The old 0.10 startup repair cannot be reused safely for Crew
    worktrees or updated source owners. A Gateway restart only records the
    activation marker; it never attempts to re-patch upstream Python files.
    """
    return 0


def _is_crew_gateway_process(argv: list[str] | None = None) -> bool:
    """Only the Gateway may record activation; see ``process_role``."""
    from .process_role import is_crew_gateway_process

    return is_crew_gateway_process(argv)


def _record_activation() -> None:
    """Record that this exact copied runtime has started in a fresh Gateway.

    Integration writes modify Crew source and the built dashboard on disk, but
    KiroCrew's running Python modules and renderer keep their old code until a
    complete restart.  This marker lets the App distinguish "written on disk"
    from "actually active in this Gateway process" even across same-version App
    rebuilds. Non-Gateway interpreters never write it.
    """
    if not _is_crew_gateway_process():
        return
    state_path = Path(__file__).resolve().parent / _STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        if not isinstance(state, dict):
            return
        raw_home = str(state.get("kiro_byo_home") or "").strip()
        if not raw_home:
            return
        path = Path(raw_home) / "data" / "activation.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "activated_at": time.time(),
            "kiro_byo_version": str(state.get("kiro_byo_version") or ""),
            "kiro_byo_build": str(state.get("kiro_byo_build") or ""),
            "detected_version": str(state.get("detected_version") or ""),
        }
        _atomic_write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", path)
    except Exception:
        # Activation diagnostics must never stop KiroCrew startup.
        return


_bind_runtime_environment()
# The .pth hook is imported by every Crew interpreter. Only the Gateway may
# report activation; startup never edits any Crew source file.
if _is_crew_gateway_process():
    _record_activation()
