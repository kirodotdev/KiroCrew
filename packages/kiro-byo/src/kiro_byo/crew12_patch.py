"""Source-guarded KiroCrew 0.12 ACP migration candidate.

Reuse 0.11's proven, *semantic* seam transforms wherever the shape survives.
The 0.12 session/load import boundary changed to a grouped import, so the
single changed owner has its own checked transform here. A version never
makes a seam patchable; unknown/partially patched sources are rejected.

The supplied 0.12.0 stable source subset is a source-contract *candidate*,
not an E2E-verified full host. The bundle lacks compiled Dashboard assets. Actual installed source must pass every generated seam's fixed-point
preflight before any Crew file can be written.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass

from . import crew_current_patch as base
from .crew_patch_common import PatchError

TARGET_FILES = base.TARGET_FILES
_SESSION_LOAD = "src/kiro_crew/acp/runtime_session_load.py"


def _load_session(text: str) -> str:
    tree = ast.parse(text)
    func = next((n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "load_session"), None)
    if func is None or func.end_lineno is None:
        raise PatchError("Crew 0.12 load_session owner moved")
    lines = text.splitlines(keepends=True)
    body = "".join(lines[func.lineno - 1:func.end_lineno])
    # rc.3 must preserve the SAME permission, projection and MCP setup on
    # session/load as on session/new. A matching KAS if-guard alone is not
    # proof of a safe resume path; abort before touching Crew files when any
    # of these independent lifecycle edges moves.
    required_resume_edges = (
        "await handle.apply_session_permission_routing()",
        "self._harness.record_session_projection(",
        "await self.terminate_session(resume_sid)",
        "self._start_mount_check(",
    )
    missing = [edge for edge in required_resume_edges if edge not in body]
    if missing:
        raise PatchError("Crew 0.12 session/load safety edges moved: " + ", ".join(missing))
    original = "    if self._acp_backend == ACP_BACKEND_KAS:\n"
    replacement = "    if self._acp_backend in {ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO}:\n"
    if body.count(replacement) == 1:
        if body.count(original):
            raise PatchError("mixed 0.12 session/load activation guards")
    elif body.count(original) == 1 and "await self._kas_custom_agents(" in body:
        body = body.replace(original, replacement, 1)
        text = "".join(lines[:func.lineno - 1]) + body + "".join(lines[func.end_lineno:])
    else:
        raise PatchError("Crew 0.12 KAS custom-agent load guard moved")
    tree = ast.parse(text)
    has_backend_import = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "kiro_crew.agent_sdk.backends"
        and any(a.name == "ACP_BACKEND_KIRO_BYO" for a in n.names)
        for n in tree.body
    )
    if not has_backend_import:
        # Preserve the tagged 0.12 grouped ACP types import without rewriting it.
        # The source's actual imported KAS symbol must still be present.
        acp_imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
                       and n.module == "kiro_crew.acp.types"]
        if len(acp_imports) != 1 or not any(
            a.name == "ACP_BACKEND_KAS" for a in acp_imports[0].names
        ):
            raise PatchError("Crew 0.12 ACP types import contract moved")
        at = acp_imports[0].end_lineno
        assert at is not None
        updated = text.splitlines(keepends=True)
        updated.insert(at, "from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO_BYO\n")
        text = "".join(updated)
    ast.parse(text)
    return text


def patch_text(relative: str, text: str) -> str:
    relative = relative.replace("\\", "/")
    if relative == _SESSION_LOAD:
        return _load_session(text)
    if relative not in TARGET_FILES:
        raise PatchError(f"Crew 0.12 has no audited transform for {relative}")
    # Do not clone 0.11 patch code; the reused semantic method itself fails
    # if upstream changes its anchor or any BYO-owned precondition.
    return base.patch_text(relative, text)


@dataclass(frozen=True)
class TransformInspection:
    state: str
    detail: str = ""


def inspect(relative: str, text: str) -> TransformInspection:
    try:
        updated = patch_text(relative, text)
        if updated == text:
            return TransformInspection("patched", "0.12 exact owned fixed point")
        if "kiro_byo" in text.lower() or "kirobyo" in text.lower():
            # Only independently AST-proven BYO-owned migrations are accepted.
            # Each must independently prove that Crew-owned AST is untouched.
            if relative.replace("\\", "/") in {
                "src/kiro_crew/acp/types.py",
                "src/kiro_crew/dashboard/handlers/agents.py",
                "src/kiro_crew/dashboard/chat_api/slot_effort.py",
                "src/kiro_crew/providers/mirrors/registry.py",
                "src/kiro_crew/config/loader_builders.py",
            }:
                prior = base.inspect(relative, text)
                if prior.state == "upgradeable" and patch_text(relative, updated) == updated:
                    return TransformInspection("upgradeable", prior.detail)
            return TransformInspection("incompatible", "unrecognized partial BYO 0.12 patch")
        if patch_text(relative, updated) != updated:
            return TransformInspection("incompatible", "0.12 transform did not converge")
        compile(updated, relative, "exec")
        return TransformInspection("clean", "0.12 source transform converges")
    except (PatchError, SyntaxError, ValueError) as exc:
        return TransformInspection("incompatible", str(exc))
