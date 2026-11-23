"""``kirocrew compliance report``: one point-in-time evidence file for a security review.

Reads the policy, retention, audit-chain, denied-rule and app-trust facts through their own APIs
into one JSON document with a summary and an unkeyed SHA-256 manifest, which shows an accidental
change, not a deliberate one. Read-only: no setting, audit event or network call; the one file
written is ``--out`` (owner-only, never overwriting), and like ``kirocrew security verify`` opening
the audit log creates its key on a home that never had one. An unreadable fact is ``unavailable``
with the error's class (``config-defaults`` when defaults replaced its config section).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1


def register_parser(sub: Any) -> None:
    from kiro_crew import cli_help

    actions = cli_help.add_command(sub, "compliance").add_subparsers(dest="compliance_action")
    report = actions.add_parser("report", help="Write the security evidence file (read-only)")
    help_text = "Write the report to FILE (owner-only; never overwrites), else print it"
    report.add_argument("--out", metavar="FILE", help=help_text)


class _ConfigDefaults(Exception):
    """The loader put defaults in place of a config section this fact reads."""


def _config(*sections: str) -> Any:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG, observed_degraded_sections

    cfg = KiroCrewConfig.load()
    if (cfg.degraded_sections | observed_degraded_sections()) & {DEGRADED_WHOLE_CONFIG, *sections}:
        raise _ConfigDefaults
    return cfg


def _policy() -> dict[str, Any]:
    from kiro_crew.platform.context import current_context
    from kiro_crew.platform.governance import TIER_CENTRAL
    from kiro_crew.platform.policy_distribution import distribution_posture

    ceiling = getattr(current_context(), "governance", None)
    distribution = distribution_posture()
    fact: dict[str, Any] = {"active": ceiling is not None, "source": "none"}
    fact["provenance"] = "no policy active (editable secure-defaults)"
    if ceiling is not None:
        fact.update(
            # The installed tier: under "degrade" a failed fetch leaves a local one governing.
            source="central" if ceiling.tier == TIER_CENTRAL else "local",
            version=ceiling.version,
            signature_state=ceiling.signature_state,
            provenance=ceiling.signature_summary(),
            governed_scopes=len(ceiling.controls),
            require_sandbox=ceiling.boot.require_sandbox,
            fail_closed=ceiling.boot.fail_closed,
        )
    # The scheme only, never the endpoint: this file is handed to other people.
    keys = ("configured", "source_scheme", "on_unavailable", "error_code")
    fact["distribution"] = {key: distribution.get(key) for key in keys}
    return fact


def _retention() -> dict[str, Any]:
    cfg = _config("session", "telemetry", "memory")
    sessions = cfg.session.archive_retention_days
    # The off switches (-1 for sessions, 0 for telemetry) are null: no limit, never a count.
    return {
        "session_archive_days": sessions if sessions >= 0 else None,
        "telemetry_days": cfg.telemetry.retention_days or None,
        "telemetry_max_mb": cfg.telemetry.max_total_mb or None,
        "memory_backups_enabled": cfg.memory.backup_enabled,
        "memory_backups_kept": cfg.memory.backup_keep,
    }


def _audit_chain() -> dict[str, Any]:
    from kiro_crew.sel import sel

    result = sel().verify_integrity(detailed=True)
    total, valid, verifiable = result.total, result.valid, result.history_verifiable
    state = "empty" if not total else "intact" if total == valid else "tampered"
    fact: dict[str, Any] = {"state": state if verifiable else "unverifiable", "valid": valid}
    fact.update(entries=total, history_verifiable=verifiable)
    if not verifiable:
        fact["reason"] = str(result.reason)[:200]
    return fact


def _denied_rules() -> dict[str, Any]:
    from kiro_crew.dashboard.handlers import security as panel
    from kiro_crew.security import pinned_builtin_command_ids

    snapshot = panel.build_denied_commands_snapshot()
    rules, user_rules = snapshot["builtins"], snapshot["user_added"]
    # Settings locks a rule any profile pins; the gate keeps it on only for sessions bound to it.
    state, ceiling = panel._denied_state(panel._read_denied_data()), pinned_builtin_command_ids()
    off = {r["id"] for r in rules} if state["disable_all"] else set(state["disabled_ids"])
    profile_only = [r for r in rules if r["lock_reason"] == "policy" and r["id"] in off - ceiling]
    return {
        "builtin_total": sum(1 for r in rules if r["source"] == "builtin"),
        "edition_total": sum(1 for r in rules if r["source"] != "builtin"),
        "enabled": sum(1 for r in rules if r["enabled"]) - len(profile_only),
        "profile_pinned": len(profile_only),
        "locked_by_floor": sum(1 for r in rules if r["lock_reason"] == "floor"),
        "locked_by_policy": sum(1 for r in rules if r["lock_reason"] == "policy"),
        "user_added_total": len(user_rules),
        "user_added_enabled": sum(1 for r in user_rules if r["enabled"]),
        "disable_all": snapshot["disable_all"],
        "by_category": dict(sorted(Counter(r["category"] for r in rules).items())),
    }


def _app_trust() -> dict[str, Any]:
    from kiro_crew.apps import execution

    _config("agent")  # a config that cannot load must read as unavailable, not as "no grants"
    trusted = sorted(execution.trusted_app_names())
    return {
        "third_party_allowed_for_all": execution.third_party_execution_allowed(),
        "trusted_count": len(trusted),
        "trusted_local_count": len(execution.trusted_local_app_names()),
        "trusted_apps": trusted,
    }


_FACTS: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = tuple(
    (read.__name__[1:], read)
    for read in (_policy, _retention, _audit_chain, _denied_rules, _app_trust)
)


def _collect() -> dict[str, dict[str, Any]]:
    facts: dict[str, dict[str, Any]] = {}
    for name, read in _FACTS:
        try:
            facts[name] = {"status": "ok", **read()}
        except Exception as exc:  # noqa: BLE001 - one unreadable fact must not hide the rest
            # The class only: the message of an OS or parse error quotes the path.
            why = "config-defaults" if isinstance(exc, _ConfigDefaults) else type(exc).__name__
            facts[name] = {"status": "unavailable", "reason": why}
    return facts


#: One readable line per fact, filled from the fact's own fields (null reads "no limit").
_LINES = {
    "policy": "{provenance}; source {source}",
    "retention": "days kept: sessions {session_archive_days}, telemetry {telemetry_days}"
    " (size cap MB: {telemetry_max_mb}); memory backups on: {memory_backups_enabled}",
    "audit_chain": "{state}, {valid}/{entries} valid",
    "denied_rules": "{enabled} of {builtin_total} built-in and {edition_total} edition rules on,"
    " {profile_pinned} more on only in sessions bound to a profile pinning them,"
    " {user_added_enabled} of {user_added_total} user-added on",
    "app_trust": "{trusted_count} third-party apps trusted by name, all trusted: {third_party_allowed_for_all}",
}


def _line(name: str, fact: dict[str, Any]) -> str:
    from kiro_crew.terminal_safe import safe_terminal_line

    if fact["status"] != "ok":
        return safe_terminal_line(f"{name.replace('_', ' ')}: unavailable ({fact['reason']})")
    shown = {k: "no limit" if v is None else v for k, v in fact.items()}
    return safe_terminal_line(f"{name.replace('_', ' ')}: {_LINES[name].format_map(shown)}")


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_report() -> dict[str, Any]:
    """The whole evidence document. Reads only; see the module docstring."""
    from kiro_crew import __version__

    facts = _collect()
    summary = [_line(name, fact) for name, fact in facts.items()]
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kiro_crew_version": __version__,
        "facts": facts,
        "summary": summary,
    }
    parts = {**{name: _digest(fact) for name, fact in facts.items()}, "summary": _digest(summary)}
    # "whole" is everything but the manifest, so the time and version cannot change unseen.
    report["manifest"] = {"algorithm": "sha256", "parts": parts, "whole": _digest(report)}
    return report


def _write_new(out: str, text: str) -> bool:
    """Create *out* owner-only and write *text*; refuse a name that already exists."""
    from kiro_crew import platform_compat
    from kiro_crew.owner_only_files import owner_only_opener

    name = Path(out).name
    created: Path | None = None
    try:
        path = Path(out).expanduser()
        with open(path, "x", encoding="utf-8", opener=owner_only_opener) as handle:
            created = path
            platform_compat.restrict_to_owner(path)  # lockdown-ok: new empty 0600 file, no text yet
            handle.write(text)
    except FileExistsError:
        print(f"Refusing to overwrite {name}: the file already exists.", file=sys.stderr)
        return False
    except (OSError, RuntimeError) as exc:  # never print str(exc): it quotes the resolved target
        if created is not None:
            created.unlink(missing_ok=True)  # never leave a half-written evidence file
        why = f" ({exc.strerror})" if isinstance(exc, OSError) and exc.strerror else ""
        print(f"Could not write {name}: {type(exc).__name__}{why}.", file=sys.stderr)
        return False
    return True


def compliance_cmd(args: argparse.Namespace) -> int:
    if getattr(args, "compliance_action", None) != "report":
        print("Usage: kirocrew compliance report [--out FILE]", file=sys.stderr)
        return 2
    report = build_report()
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if not (out := getattr(args, "out", None)):
        print(text, end="")
        return 0
    if not _write_new(out, text):
        return 1
    print(f"Wrote the compliance report to {out}")
    print("\n".join(f"  {line}" for line in report["summary"]))
    return 0
