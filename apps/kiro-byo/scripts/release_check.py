#!/usr/bin/env python3
"""Fail-closed release metadata checks for Kiro BYO.

The script intentionally uses only the standard library so it can run before dev
extras are installed. CI separately runs tests, coverage, lint, typecheck and wheel
builds; this check prevents version/documentation metadata from drifting between
packaging surfaces.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# KiroCrew App manifests require SemVer, not merely a Python/PEP 440 version.
# Examples accepted: 0.9.12, 0.9.12-dev.2, 1.0.0-rc.2+build.5
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _is_semver(value: str) -> bool:
    return _SEMVER_RE.fullmatch(value) is not None


def _version_facts() -> dict[str, str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    app = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
    init_text = (ROOT / "src/kiro_byo/__init__.py").read_text(encoding="utf-8")
    protocol_text = (ROOT / "src/kiro_byo/protocol.py").read_text(encoding="utf-8")
    init_match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_text, re.M)
    agent_match = re.search(r'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', protocol_text, re.M)
    if init_match is None or agent_match is None:
        raise RuntimeError("version constants are missing")
    return {
        "VERSION": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "pyproject": str(pyproject["project"]["version"]),
        "app": str(app["version"]),
        "package": init_match.group(1),
        "agent": agent_match.group(1),
    }


_FORBIDDEN_PRE_RELEASE_IDENTITIES = (
    "Kiro" + "API",
    "Kiro" + " API",
    "Kiro" + "-API",
    "kiro" + "-api",
    "kiro" + "_api",
    "KIRO" + "_API",
    "Kiro" + "Api",
)


def _stale_identity_leaks() -> list[str]:
    """Return source files that still carry the abandoned pre-release identity."""
    leaked: list[str] = []
    skipped_parts = {".git", ".pytest_cache", "__pycache__", "build", "dist"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in skipped_parts for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(token in text for token in _FORBIDDEN_PRE_RELEASE_IDENTITIES):
            leaked.append(path.relative_to(ROOT).as_posix())
    return sorted(leaked)


def _release_state_leaks() -> list[str]:
    """Return mutable/user-specific files that must never ship in a release tree."""
    leaked: list[str] = []
    for rel in ("data/sessions", "data/telemetry", "data/runtime_trace", "data/audit"):
        root = ROOT / rel
        if not root.exists():
            continue
        leaked.extend(
            path.relative_to(ROOT).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )
    for rel in ("config/providers.json", "config/native_tools.json"):
        path = ROOT / rel
        if path.is_file():
            leaked.append(rel)
    return sorted(leaked)




def _policy_pack_release_check() -> str | None:
    path = ROOT / "src/kiro_byo/policy_packs/tiga.json"
    if not path.is_file():
        return "built-in Tiga Policy Pack is missing"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"built-in Tiga Policy Pack is unreadable: {exc}"
    if raw.get("schema_version") != 1 or raw.get("id") != "tiga":
        return "built-in Tiga Policy Pack identity/schema drift"
    policy_ids = [row.get("id") for row in raw.get("policies", []) if isinstance(row, dict)]
    if policy_ids != ["sky", "multi", "power", "glitter"] or raw.get("default_policy") != "multi":
        return "built-in Tiga Policy Pack policy/default drift"
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {}).get("kiro_byo", [])
    if "policy_packs/*.json" not in package_data:
        return "wheel package-data no longer includes policy_packs/*.json"
    return None



def _mode_type_ui_release_check() -> str | None:
    protocol = (ROOT / "src/kiro_byo/protocol.py").read_text(encoding="utf-8")
    crew_patch = (ROOT / "src/kiro_byo/crew_patch_common.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui/index.mjs").read_text(encoding="utf-8")
    if "kiro-byo-mode" in protocol or "kiro-byo-mode" in crew_patch or "kiro-byo-mode" in ui:
        return "legacy virtual Mode sentinel leaked back into product/runtime surfaces"
    if 'TYPE_SELECTION_PREFIX = "kiro-byo-type:"' not in protocol:
        return "Type selection wire prefix is missing"
    if 'return f"{TYPE_SELECTION_PREFIX}{mode}:{type_value}"' not in protocol:
        return "Type selection wire id is no longer namespaced by Mode"
    for label in ("Sky Type", "Multi Type", "Power Type", "Glitter Type"):
        if label not in (ROOT / "src/kiro_byo/policy_packs/tiga.json").read_text(encoding="utf-8"):
            return f"Tiga Type missing from built-in pack: {label}"
    if "changePolicy" in ui:
        return "management UI still exposes a Type switch instead of read-only Type catalog"
    if "这里只展示，不在此切换" not in ui:
        return "management UI no longer explains that Type switching belongs to KiroCrew"
    if "for _policy in _pack.policies" not in crew_patch or "_kiro_byo_type_selection_id" not in crew_patch:
        return "Crew picker no longer projects active Mode Types before real models"
    return None

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-only", action="store_true")
    parser.parse_args()

    facts = _version_facts()
    unique = set(facts.values())
    for surface, value in facts.items():
        print(f"{surface:<12} {value}")
    if not _is_semver(facts["app"]):
        print(
            f"FAIL: app.json version must be SemVer, got {facts['app']!r}",
            file=sys.stderr,
        )
        return 2
    if len(unique) != 1:
        print("FAIL: release version metadata drift", file=sys.stderr)
        return 8

    compat = json.loads(
        (ROOT / "integration/kiro_crew/v0.9-adaptive/compatibility.json").read_text(encoding="utf-8")
    )
    if compat.get("transformer") != "adaptive09":
        print("FAIL: Crew 0.9 semantic transformer metadata drift", file=sys.stderr)
        return 3
    if not (ROOT / "ARCHITECTURE_BOUNDARY.md").is_file():
        print("FAIL: architecture ownership boundary is missing", file=sys.stderr)
        return 4

    stale_identities = _stale_identity_leaks()
    if stale_identities:
        print(
            "FAIL: abandoned pre-release identity remains in: "
            + ", ".join(stale_identities),
            file=sys.stderr,
        )
        return 11

    policy_pack_error = _policy_pack_release_check()
    if policy_pack_error:
        print(f"FAIL: {policy_pack_error}", file=sys.stderr)
        return 12

    mode_type_error = _mode_type_ui_release_check()
    if mode_type_error:
        print(f"FAIL: {mode_type_error}", file=sys.stderr)
        return 13

    leaks = _release_state_leaks()
    if leaks:
        print(
            "FAIL: mutable runtime/secret state is present in the release tree: "
            + ", ".join(leaks),
            file=sys.stderr,
        )
        return 10

    version = next(iter(unique))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if "dev" in version:
        marker = f"Current development version: **{version}**"
        heading = f"## {version} (development)"
        match = _SEMVER_RE.fullmatch(version)
        prerelease = match.group(4) if match is not None else ""
        dev_match = re.fullmatch(r"dev\.(\d+)", prerelease or "")
        if dev_match is None:
            print(f"FAIL: development SemVer must use -dev.N, got {version!r}", file=sys.stderr)
            return 9
        base = version.split("-", 1)[0]
        audit_name = f"AUDIT_{base}_DEV{dev_match.group(1)}.md"
        label = "development"
    elif "rc" in version:
        marker = f"Current release candidate: **{version}**"
        heading = f"## {version} (release candidate)"
        audit_name = "AUDIT_0.9.11_RC1.md"
        label = "release-candidate"
    else:
        marker = f"Current release: **{version}**"
        heading = f"## {version}"
        audit_name = "AUDIT_CURRENT.md"
        label = "release"
    audit = ROOT / audit_name
    if marker not in readme:
        print(f"FAIL: README {label} version marker drift", file=sys.stderr)
        return 5
    if heading not in changelog:
        print(f"FAIL: CHANGELOG {label} heading drift", file=sys.stderr)
        return 6
    if not audit.is_file():
        print(f"FAIL: current {label} audit document is missing: {audit_name}", file=sys.stderr)
        return 7

    print("PASS: release metadata and primary documentation are internally consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
