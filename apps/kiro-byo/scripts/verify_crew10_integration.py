#!/usr/bin/env python3
"""Dry-run the Kiro BYO adapter against an installed/extracted Crew 0.10 tree.

The verifier is intentionally host-authoritative: it transforms the real Crew
sources in memory, compiles every seam, then derives the backend-card rows from
Crew's own backend registries.  Kiro BYO does not carry a second 10-row matrix.
"""
from __future__ import annotations

import argparse
import json
import runpy
import sys
import tempfile
from pathlib import Path

from kiro_byo import crew10_patch
from kiro_byo.integration_manager import compatibility_contract_issues

PREFIX = "src/kiro_crew/"
CARD_ROWS = (
    ("crew_tools", lambda ns, b: b in ns["ACP_BACKENDS_SESSION_MCP_ARRAY"] or b in ns["ACP_BACKENDS_KIRO_SLASH_COMMANDS"]),
    ("member_thread_tools", lambda ns, b: b in ns["ACP_BACKENDS_MEMBER_DISPATCH"]),
    ("member_saved_agent", lambda ns, b: b in ns["ACP_BACKENDS_MEMBER_CAPABILITIES"]),
    ("side_chat_tools", lambda ns, b: b in ns["ACP_BACKENDS_SIDE_READONLY"]),
    ("subagent_continuation", lambda ns, b: b in ns["ACP_BACKENDS_SESSION_SHARING"]),
    ("mid_turn_steer", lambda ns, b: b in ns["ACP_BACKENDS_STEER"] or b in ns["ACP_BACKENDS_STEERING_REQUEST"]),
    ("manual_compact", lambda ns, b: b in ns["ACP_BACKENDS_COMPACT"]),
    ("reasoning_effort", lambda ns, b: b in ns["ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION"] or b in ns["ACP_BACKENDS_KIRO_SLASH_COMMANDS"]),
    ("model_switch", lambda ns, b: b in ns["ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION"] or b in ns["ACP_BACKENDS_KIRO_SLASH_COMMANDS"]),
    ("markdown_agents", lambda ns, b: b in ns["ACP_BACKENDS_MARKDOWN_AGENT_SPECS"]),
)


def _path(root: Path, rel: str) -> Path:
    if not rel.startswith(PREFIX):
        raise ValueError(rel)
    return root / rel[len(PREFIX):]


def _patched_sources(root: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    failed = False
    for rel in crew10_patch.TARGET_FILES:
        path = _path(root, rel)
        if not path.is_file():
            print(f"MISSING      {rel}")
            failed = True
            continue
        text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        report = crew10_patch.inspect(rel, text)
        print(f"{report.state.upper():<12} {rel}" + (f" — {report.detail}" if report.detail else ""))
        if report.state == "incompatible":
            failed = True
            continue
        output = text if report.state == "patched" else crew10_patch.patch_text(rel, text)
        compile(output, str(path), "exec")
        outputs[rel] = output
    if failed:
        raise SystemExit(2)
    return outputs


def _backend_namespace(source: str) -> dict[str, object]:
    # run_path registers a real module namespace, which dataclasses expects when
    # resolving postponed annotations on this Crew leaf module.
    with tempfile.TemporaryDirectory(prefix="kiro-byo-verify-") as td:
        probe = Path(td) / "backends_probe.py"
        probe.write_text(source, encoding="utf-8")
        return runpy.run_path(str(probe), run_name="_kiro_byo_crew10_verify")


def _verify_owned_harness(project_root: Path, outputs: dict[str, str]) -> None:
    adapter = project_root / "integration" / "kiro_crew" / "v0.10-adaptive"
    manifest = json.loads((adapter / "manifest.json").read_text(encoding="utf-8"))
    compat = json.loads((adapter / "compatibility.json").read_text(encoding="utf-8"))
    rel = "src/kiro_crew/acp/harness/kiro_byo.py"
    overlay = adapter / "overlay" / rel
    if rel not in manifest.get("files", []):
        raise SystemExit("FAIL: dedicated Kiro BYO harness is missing from manifest")
    if rel not in compat.get("created_files", []):
        raise SystemExit("FAIL: dedicated Kiro BYO harness is not declared create-only")
    source = overlay.read_text(encoding="utf-8")
    compile(source, str(overlay), "exec")
    init_rel = "src/kiro_crew/acp/harness/__init__.py"
    init_text = outputs[init_rel]
    if "from kiro_crew.acp.harness.kiro_byo import KiroByoHarness" not in init_text:
        raise SystemExit("FAIL: harness registry does not import the dedicated owner module")
    if "class KiroByoHarness" in init_text:
        raise SystemExit("FAIL: KiroByoHarness was inlined into harness/__init__.py")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crew-package", required=True, help="Path to installed/extracted kiro_crew package")
    args = ap.parse_args()
    root = Path(args.crew_package).expanduser().resolve()
    project_root = Path(__file__).resolve().parents[1]
    compat = json.loads(
        (project_root / "integration/kiro_crew/v0.10-adaptive/compatibility.json").read_text(encoding="utf-8")
    )
    contract_issues = compatibility_contract_issues(compat, root)
    if contract_issues:
        for issue in contract_issues:
            print(f"INCOMPATIBLE {issue}", file=sys.stderr)
        raise SystemExit(2)

    outputs = _patched_sources(root)
    _verify_owned_harness(project_root, outputs)

    rel = "src/kiro_crew/agent_sdk/backends.py"
    ns = _backend_namespace(outputs[rel])
    kiro = str(ns["ACP_BACKEND_KIRO"])
    api = str(ns["ACP_BACKEND_KIRO_BYO"])

    def card(backend: str) -> list[tuple[str, bool]]:
        return [(name, bool(check(ns, backend))) for name, check in CARD_ROWS]

    native = card(kiro)
    target = card(api)
    print("\nCrew-authoritative backend card:")
    for (name, n), (_, k) in zip(native, target):
        print(f"  {name:<24} Kiro={'✓' if n else '✗'}  KiroBYO={'✓' if k else '✗'}")
    n_score = sum(v for _, v in native)
    k_score = sum(v for _, v in target)
    print(f"\nCrew-derived score: native Kiro {n_score}/10; Kiro BYO {k_score}/10")

    # The legacy 10-row card is retained only as a compact Crew UI/control-surface
    # comparison. It is NOT an overall Kiro parity percentage. Kiro BYO now
    # participates in Crew's official per-session member-dispatch mount, while
    # still abstaining from the stronger full-saved-member-spec contract. Markdown
    # agent format support is genuine: Crew parses the spec and projects it to the
    # Kiro BYO harness over the same user-level-only seam used by KAS.
    expected_false = {"member_saved_agent"}
    false_api = {name for name, value in target if not value}

    dynamic_registry = callable(ns.get("registered_backends")) and callable(ns.get("selectable_backends"))
    members_text = outputs["src/kiro_crew/members.py"]
    runtime_text = outputs["src/kiro_crew/acp/runtime.py"]
    semantic_checks = {
        "known_backend": api in ns["ACP_BACKENDS_KNOWN"],
        "member_backend_inheritance_seam": (
            ("# Kiro BYO whole-Crew backend inheritance" in members_text
             and "configured_default == ACP_BACKEND_KIRO_BYO" in members_text)
            or "resolve_member_provider_backend(member_backend, configured_default)" in members_text
        ),
        "resume_agent_reprojection_seam": (
            "if self._acp_backend in {ACP_BACKEND_KAS, ACP_BACKEND_KIRO_BYO}:" in runtime_text
        ),
        "launch_record_present": api in ns["ACP_BACKEND_LAUNCH"],
        "permission_routing_verified": ns["routing_for"](api) is ns["Routing"].ACP_PERMISSION_GATE,
        "shared_runtime": api in ns["ACP_BACKENDS_ACP_RUNTIME"],
        "legacy_acpclient_spawn_absent": api not in ns["ACP_BACKENDS_ACP_CLIENT_SPAWNABLE"],
        "durable_session_sharing": api in ns["ACP_BACKENDS_SESSION_SHARING"],
        "member_dispatch_mount_declared": api in ns["ACP_BACKENDS_MEMBER_DISPATCH"],
        "member_panel_mount_declared": api in ns.get("ACP_BACKENDS_MEMBER_PANEL", frozenset()),
        "no_full_member_overclaim": api not in ns["ACP_BACKENDS_MEMBER_CAPABILITIES"],
        "markdown_agent_projection_declared": api in ns["ACP_BACKENDS_MARKDOWN_AGENT_SPECS"],
    }
    if dynamic_registry:
        semantic_checks.update({
            "registered_via_dynamic_registry": api in ns["registered_backends"](),
            "selectable_via_dynamic_registry": api in ns["selectable_backends"](),
            "public_baseline_not_polluted": api not in ns["BASELINE_SELECTABLE_BACKENDS"],
        })
    else:
        semantic_checks["legacy_baseline_selectability"] = api in ns["BASELINE_SELECTABLE_BACKENDS"]

    optional_checks: dict[str, bool | None] = {}
    if "ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY" in ns:
        optional_checks["markdown_user_scope_matches_harness"] = (
            api in ns["ACP_BACKENDS_USER_LEVEL_AGENT_SPECS_ONLY"]
        )
    else:
        # Earlier compatible host source shapes do not expose this scope marker.
        # The owned harness still reads only paths.kiro_agents_dir(); there is no
        # upstream scope registry to join on those builds.
        optional_checks["markdown_user_scope_matches_harness"] = None
    if "ACP_BACKENDS_SELF_SERVED_ACP" in ns:
        optional_checks["self_served_from_launch_contract"] = api in ns["ACP_BACKENDS_SELF_SERVED_ACP"]
    else:
        optional_checks["self_served_from_launch_contract"] = None
    if "ACP_BACKENDS_HONOR_ZERO_TOOL_BAN" in ns:
        optional_checks["zero_tool_ban_honored"] = api in ns["ACP_BACKENDS_HONOR_ZERO_TOOL_BAN"]
    else:
        optional_checks["zero_tool_ban_honored"] = None
    if "ACP_BACKEND_PROCESS_NAMES" in ns:
        launch_record = ns["ACP_BACKEND_LAUNCH"].get(api)
        optional_checks["process_reclaim_name_from_launch"] = (
            launch_record is not None
            and api in ns["ACP_BACKEND_PROCESS_NAMES"]
            and ns["ACP_BACKEND_PROCESS_NAMES"][api] == launch_record.binary
        )
    else:
        optional_checks["process_reclaim_name_from_launch"] = None

    print("\nCrew 0.10 semantic integration contract:")
    for name, ok in semantic_checks.items():
        print(f"  {name:<38} {'✓' if ok else '✗'}")
    for name, ok in optional_checks.items():
        rendered = 'n/a (capability absent in this Crew build)' if ok is None else ('✓' if ok else '✗')
        print(f"  {name:<38} {rendered}")

    if k_score != 9 or false_api != expected_false:
        print(
            "FAIL: legacy Crew capability card differs from Kiro BYO's deliberate boundary contract",
            file=sys.stderr,
        )
        return 3
    failed_contracts = [name for name, ok in semantic_checks.items() if not ok]
    failed_contracts.extend(name for name, ok in optional_checks.items() if ok is False)
    if failed_contracts:
        print(
            "FAIL: Crew 0.10 semantic integration contract failed: " + ", ".join(failed_contracts),
            file=sys.stderr,
        )
        return 4
    print(
        "PASS: semantic seams compile; source-shape-appropriate Crew selectability "
        "contract is active; derived capabilities and Kiro BYO's evidence-backed "
        "contract are consistent; legacy capability card is 9/10 with Crew-owned "
        "member dispatch + Markdown projection and without saved-spec orchestration overclaim"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
