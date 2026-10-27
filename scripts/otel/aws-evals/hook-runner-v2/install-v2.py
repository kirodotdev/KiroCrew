#!/usr/bin/env python3
"""Explicitly enable or disable the optional KiroCrew OTLP hook adapter.

  install-v2.py enable  [--hooks FILE] [--service NAME]
  install-v2.py disable [--hooks FILE]

enable : backs up the file, disables the old otel-* runner entries (kept, not deleted),
         adds five otelv2-* entries whose command ends with '; exit 0'. Repeated enable is a no-op.
disable: backs up the file, removes otelv2-* entries, and restores legacy enabled values from
         the latest enable backup. Without an enable backup, legacy values are left unchanged.
Other hooks (for example the payload recorder) are left alone.
"""

import argparse
import json
import re
import shlex
import shutil
import time
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=["enable", "disable"])
    result.add_argument("--hooks", default=str(Path.home() / ".kiro/crew/hooks.json"))
    result.add_argument("--service", default="kiro-linux-v2")
    result.add_argument(
        "--acp",
        action="store_true",
        help="also run the ACP chat/workflow converter after each Stop",
    )
    result.add_argument("--endpoint", default="http://127.0.0.1:14318")
    result.add_argument(
        "--runs-dir",
        default="/var/lib/otelcol/workflow-runs",
        help="KiroCrew's configured workflow run store, read by the ACP converter",
    )
    return result


def backup_path(path: Path, action: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.before-v2-{action}-{stamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.before-v2-{action}-{stamp}-{suffix}.bak")
        suffix += 1
    return candidate


def latest_enable_backup(path: Path) -> Path | None:
    prefix = f"{path.name}.before-v2-enable-"
    backups = []
    for candidate in path.parent.glob(f"{prefix}*.bak"):
        suffix = candidate.name[len(prefix) :]
        match = re.fullmatch(r"(\d{8}-\d{6})(?:-(\d+))?\.bak", suffix)
        if match:
            backups.append(((match.group(1), int(match.group(2) or 0)), candidate))
    return max(backups, default=(None, None), key=lambda item: item[0])[1]


def restore_legacy_enabled_values(hooks: list[dict], backup: Path | None) -> None:
    if backup is None:
        return
    original = json.loads(backup.read_text(encoding="utf-8"))
    original_hooks = original.get("hooks", []) if isinstance(original, dict) else []
    original_by_id = {hook.get("id"): hook for hook in original_hooks if isinstance(hook, dict)}
    for hook in hooks:
        hook_id = str(hook.get("id", ""))
        if not hook_id.startswith("otel-") or hook_id not in original_by_id:
            continue
        prior = original_by_id[hook_id]
        if "enabled" in prior:
            hook["enabled"] = prior["enabled"]
        else:
            hook.pop("enabled", None)


def build_command(args: argparse.Namespace, runner: Path) -> str:
    assignments = [
        f"KIRO_OTEL_ENDPOINT={shlex.quote(args.endpoint)}",
        f"KIRO_OTEL_SERVICE_NAME={shlex.quote(args.service)}",
    ]
    if args.acp:
        converter = runner.parent.parent / "acp-tap" / "acp-to-spans.py"
        assignments.extend(
            [
                "KIRO_OTEL_HOOK_TOOLS=off",
                f"KIRO_OTEL_ACP_CONVERTER={shlex.quote(str(converter))}",
                f"KIRO_OTEL_WORKFLOW_RUNS_DIR={shlex.quote(args.runs_dir)}",
            ]
        )
    return " ".join(assignments) + f" python3 {shlex.quote(str(runner))}; exit 0"


def build_v2_hooks(command: str) -> list[dict]:
    hooks = []
    for event, matcher in [
        ("AgentSpawn", ""),
        ("UserPromptSubmit", ""),
        ("PreToolUse", "*"),
        ("PostToolUse", "*"),
        ("Stop", ""),
    ]:
        hooks.append(
            {
                "id": f"otelv2-{event.lower()}",
                "name": f"OTEL v2 {event}",
                "event": event,
                "matcher": matcher,
                "matcher_mode": "glob",
                "command": command,
                "skills": [],
                "timeout": 30,
                "enabled": True,
            }
        )
    return hooks


def has_complete_v2_installation(hooks: list[dict], expected: list[dict]) -> bool:
    v2_hooks = [hook for hook in hooks if str(hook.get("id", "")).startswith("otelv2-")]
    if len(v2_hooks) != len(expected):
        return False
    expected_by_id = {hook["id"]: hook for hook in expected}
    if {hook.get("id") for hook in v2_hooks} != set(expected_by_id):
        return False
    required_fields = (
        "event",
        "matcher",
        "matcher_mode",
        "command",
        "skills",
        "timeout",
        "enabled",
    )
    for hook in v2_hooks:
        want = expected_by_id[hook["id"]]
        if any(hook.get(field) != want[field] for field in required_fields):
            return False
    return all(
        hook.get("enabled") is False
        for hook in hooks
        if str(hook.get("id", "")).startswith("otel-")
    )


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    path = Path(args.hooks)
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, list):
        raise ValueError(f"hooks.json must contain a hooks list: {path}")

    original_enable_backup = latest_enable_backup(path)
    desired_v2_hooks = None
    backup_action = args.action
    if args.action == "enable":
        runner = Path(__file__).resolve().with_name("otel-crew-hook-v2.py")
        desired_v2_hooks = build_v2_hooks(build_command(args, runner))
        if has_complete_v2_installation(hooks, desired_v2_hooks):
            print("enable: already configured; no changes")
            return 0
        has_v2_entries = any(str(hook.get("id", "")).startswith("otelv2-") for hook in hooks)
        if has_v2_entries and original_enable_backup is not None:
            backup_action = "repair"

    backup = backup_path(path, backup_action)
    shutil.copy2(path, backup)
    print("backup:", backup)

    hooks[:] = [hook for hook in hooks if not str(hook.get("id", "")).startswith("otelv2-")]
    if args.action == "enable":
        for hook in hooks:
            if str(hook.get("id", "")).startswith("otel-"):
                hook["enabled"] = False
        hooks.extend(desired_v2_hooks or [])
    else:
        restore_legacy_enabled_values(hooks, original_enable_backup)

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(
        f"{args.action}: "
        + ", ".join(
            f"{hook['id']}={'on' if hook.get('enabled', True) else 'off'}" for hook in hooks
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
