"""Self-heal the Kiro Crew overlay only when the installed integration surface is compatible.

Kiro BYO uses adaptive semantic transformers for supported Crew builds. On startup,
known integration seams are re-inspected and re-applied only when their structure is
still recognized; changed boundaries fail closed. The integration supports only semantic-generated
seams plus the Kiro BYO-owned create-only harness module.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
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


def _sha256(path: Path) -> str:
    """Hash Python source semantically enough for adapter selection/self-heal.

    Normalize BOM/CRLF so harmless packaging differences do not look like
    integration-surface changes.
    """
    text = path.read_text(encoding="utf-8-sig")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def _remove_pyc(py_file: Path) -> None:
    cache = py_file.parent / "__pycache__"
    if not cache.is_dir():
        return
    for candidate in cache.glob(f"{py_file.stem}.*.pyc"):
        try:
            candidate.unlink()
        except OSError:
            pass


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.name + ".kiro-byo-", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


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
    package_dir = Path(__file__).resolve().parent
    state_path = package_dir / _STATE_NAME
    if not state_path.is_file():
        return 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        root = Path(state["kiro_byo_home"])
        crew_package = Path(state["crew_package"])
        overlay_version = str(state.get("overlay_version") or "")
        adapter_root = root / "integration" / "kiro_crew" / overlay_version
        overlay_root = Path(state.get("overlay_root") or (adapter_root / "overlay"))
        manifest_path = Path(state.get("manifest_path") or (adapter_root / "manifest.json"))
        compatibility_path = Path(
            state.get("compatibility_path") or (adapter_root / "compatibility.json")
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
        generated_files = frozenset(
            str(v).replace("\\", "/")
            for v in (compatibility.get("generated_files") or [])
        )
        created_files = frozenset(
            str(v).replace("\\", "/")
            for v in (compatibility.get("created_files") or [])
        )
        transformer = str(compatibility.get("transformer") or "").strip().lower()
        if transformer != "adaptive10" or overlay_version != "v0.10-adaptive":
            print("[kiro-byo] unsupported legacy Crew adapter; bootstrap repair skipped", file=sys.stderr)
            return 0
        # Old runtime snapshots may survive App upgrades. Never let the .pth
        # startup repair bypass the 0.10-only install policy.
        import re
        declared_crew_version = str(state.get("detected_version") or "")
        if re.match(r"^0\.10\.\d+(?:$|[-+]|rc)", declared_crew_version) is None:
            print("[kiro-byo] unsupported Crew snapshot; bootstrap repair skipped", file=sys.stderr)
            return 0
        try:
            import kiro_crew  # type: ignore
            live_crew_version = str(getattr(kiro_crew, "__version__", "") or "")
        except ImportError:
            live_crew_version = ""
        if live_crew_version and re.match(r"^0\.10\.\d+(?:$|[-+]|rc)", live_crew_version) is None:
            print("[kiro-byo] installed Crew is not 0.10; bootstrap repair skipped", file=sys.stderr)
            return 0
    except Exception as exc:
        print(f"[kiro-byo] bootstrap state unavailable: {exc}", file=sys.stderr)
        return 0

    # Validate every unmodified Crew API imported by the Kiro BYO-owned harness
    # before self-healing any patched seam.  The App installer performs the same
    # contract check; keeping it in the runtime snapshot prevents a Crew update
    # from being partially re-patched at interpreter startup when an overlay
    # dependency has actually moved.
    try:
        from .integration_manager import compatibility_contract_issues

        contract_issues = compatibility_contract_issues(compatibility, crew_package)
    except Exception as exc:
        contract_issues = [f"contract validation failed: {exc}"]
    if contract_issues:
        preview = ", ".join(contract_issues[:3])
        more = "..." if len(contract_issues) > 3 else ""
        print(
            f"[kiro-byo] Kiro Crew overlay contract changed; self-repair skipped: "
            f"{preview}{more}",
            file=sys.stderr,
        )
        return 0

    repaired = 0
    incompatible: list[str] = []
    for rel_raw in manifest.get("files", []):
        rel = str(rel_raw).replace("\\", "/")
        if not rel.startswith(_PREFIX):
            continue
        src = overlay_root / Path(rel)
        dst = crew_package / Path(rel[len(_PREFIX):])

        # A created file is owned by Kiro BYO rather than replacing an upstream
        # Crew source file.  If Crew's updater removes it, recreate the exact
        # packaged owner module.  If another file occupies the path, fail closed
        # instead of overwriting foreign code.
        if rel in created_files:
            if not src.is_file():
                incompatible.append(f"{rel} (created-file overlay missing)")
                continue
            try:
                overlay_hash = _sha256(src)
                if not dst.exists():
                    _atomic_copy(src, dst)
                    _remove_pyc(dst)
                    repaired += 1
                    continue
                if not dst.is_file():
                    incompatible.append(f"{rel} (created-file target is not a file)")
                    continue
                if _sha256(dst) == overlay_hash:
                    continue
                incompatible.append(f"{rel} (foreign content at Kiro BYO-owned path)")
                continue
            except OSError as exc:
                incompatible.append(f"{rel} ({exc})")
                continue

        # Adaptive semantic integrations can rebase themselves over a harmless
        # new insider build. The transformer inspects the *current* Crew source,
        # applies only known semantic additions, compiles the result, and refuses
        # any changed boundary it cannot prove. This removes the old requirement
        # for one Kiro BYO release per Crew insider suffix.
        if rel in generated_files and transformer == "adaptive10":
            if not dst.is_file():
                incompatible.append(rel)
                continue
            try:
                from . import crew10_patch as semantic_patch

                current_text = dst.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
                inspection = semantic_patch.inspect(rel, current_text)
                if inspection.state == "patched":
                    continue
                if inspection.state != "clean":
                    incompatible.append(f"{rel} ({inspection.detail})")
                    continue
                patched_text = semantic_patch.patch_text(rel, current_text)
                _atomic_write_text(patched_text, dst)
                _remove_pyc(dst)
                repaired += 1
                continue
            except Exception as exc:
                incompatible.append(f"{rel} ({exc})")
                continue

        incompatible.append(
            f"{rel} (unclassified Crew 0.10 seam; expected generated_files or created_files)"
        )

    if incompatible:
        preview = ", ".join(incompatible[:3])
        more = "..." if len(incompatible) > 3 else ""
        print(
            f"[kiro-byo] Kiro Crew integration surface changed; self-repair skipped for "
            f"{len(incompatible)} file(s): {preview}{more}",
            file=sys.stderr,
        )
    if repaired:
        print(f"[kiro-byo] restored Kiro Crew integration ({repaired} file(s))", file=sys.stderr)
    return repaired


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
# Importing the .pth happens in every interpreter that uses Crew's bundled
# Python. Only the Gateway owns host-integration self-heal; ACP/MCP/diagnostic
# child processes load the runtime but never mutate KiroCrew source files.
if _is_crew_gateway_process():
    repair()
    _record_activation()
