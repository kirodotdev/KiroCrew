#!/usr/bin/env python3
"""Mutation sweep for the crew-work-migration feature.

Closes the three places the TDD discipline was NOT clean:

  1. The slice-1 protocol tests were written first but the implementation landed
     before shell execution was available, so those 12 tests went straight to
     green and were never observed failing. Assertions that have never failed
     are unproven.
  2. The reversibility and two-crew integration tests passed on their first run
     by design (they characterise EXISTING behaviour rather than driving new
     code), so they were never red either.
  3. The frontend menu items and page wiring were implemented before their
     tests, reversing the order.

The remedy for all three is the same and is stronger than back-dating a red:
break the behaviour each test claims to protect and require the test to FAIL. A
mutation that survives means the test has no teeth -- exactly the weak-eval
failure mode where a green light is worse than no light.

Usage:  python3 mutation_sweep.py          (runs every mutation)
Exit 0 = every mutation was caught. Exit 1 = at least one SURVIVED.
"""

from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[4]
PYTEST = REPO / ".venv/bin/pytest"


@dataclasses.dataclass
class Mutation:
    gap: str  # which of the three gaps this covers
    what: str  # the invariant being broken, in words
    path: str  # file, relative to REPO
    old: str
    new: str
    test: str  # test selector expected to FAIL
    runner: str = "pytest"


MUTATIONS: list[Mutation] = [
    # Every mutation below targets code a USER can reach. The transfer half's
    # mutations left with the coordinator: a mutation that proves a test has
    # teeth is worthless when nothing in production drives the code it mutates,
    # and keeping them would have re-anchored a growing set of dead entries.
    # ── the allow-list and the secret scan are what a plan is built from ──
    Mutation(
        gap="plan core",
        what="allow-list stops filtering (a plan claims every field ships)",
        path="src/kiro_crew/migration/protocol.py",
        old="return {k: source[k] for k in allowed if k in source}",
        new="return dict(source)",
        test="test/test_migration_protocol.py::test_allow_list_serialize_drops_unnamed_fields",
    ),
    # ── the subtraction itself must stay subtracted ───────────────────────
    Mutation(
        gap="subtraction",
        what="a transfer step returns to the cron adapter without its wiring",
        path="src/kiro_crew/migration/cron_adapter.py",
        old="    async def serialize(self, unit_id: str) -> dict:",
        new="    async def tombstone(self, u, t, r) -> None: ...\n\n    async def serialize(self, unit_id: str) -> dict:",
        test=(
            "test/test_migration_cron_adapter.py::"
            "test_the_transfer_steps_are_absent_from_the_plan_adapter"
        ),
    ),
    # ── vault grants and credentials must not travel or be printed ────────
    Mutation(
        gap="secrecy",
        what="an owner-approved vault grant is shipped to the target",
        path="src/kiro_crew/migration/cron_adapter.py",
        old='    "timeout",\n)',
        new='    "timeout",\n    "secret_env",\n)',
        test=(
            "test/test_migration_cron_adapter.py::"
            "test_owner_approved_vault_grant_never_travels_to_another_crew"
        ),
    ),
    Mutation(
        gap="secrecy",
        what="a requirement identity is printed to the terminal unsanitized",
        path="src/kiro_crew/migration/move_plan.py",
        old="    safe, _ = redact_exfiltration_urls(identity)\n    safe, _ = redact_credentials(safe)",
        new="    safe = identity",
        test=(
            "test/test_migration_move_cli.py::"
            "test_taskrun_move_plan_sanitizes_requirement_identities"
        ),
    ),
    Mutation(
        gap="secrecy",
        what="the dashboard plan serializes requirement identities unredacted",
        path="src/kiro_crew/dashboard/handlers/migration.py",
        old='"identity": _redact_requirement_identity(r.identity),',
        new='"identity": r.identity,',
        test=(
            "test/test_api_migration_move.py::"
            "test_plan_redacts_credentials_and_exfiltration_urls_from_requirements"
        ),
    ),
    Mutation(
        gap="secrecy",
        what="a caller-named runs file bypasses the sensitive-path gate",
        path="src/kiro_crew/cli_commands.py",
        old="records = _json.loads(safe_read_file(str(runs_path)))",
        new='records = _json.loads(runs_path.read_text(encoding="utf-8"))',
        test="test/test_migration_move_cli.py::test_taskrun_move_refuses_a_sensitive_runs_file",
    ),
    # ── the audit must actually fire, not merely be called ────────────────
    Mutation(
        gap="audit",
        what="the move audit resolves a module that does not exist",
        path="src/kiro_crew/dashboard/handlers/migration.py",
        old="    from kiro_crew.sel import sel",
        new="    from kiro_crew.security_event_log import sel",
        test=(
            "test/test_api_migration_move.py::"
            "test_the_move_audit_actually_reaches_the_security_event_log"
        ),
    ),
    # ── input validation and the mid-run refusal ──────────────────────────
    Mutation(
        gap="validation",
        what="a non-string target crashes instead of returning 400",
        path="src/kiro_crew/dashboard/handlers/migration.py",
        old="    if raw_target is not None and not isinstance(raw_target, str):",
        new="    if False:",
        test=(
            "test/test_api_migration_move.py::"
            "test_move_rejects_a_non_string_target_instead_of_crashing"
        ),
    ),
    Mutation(
        gap="validation",
        what="a run with a task mid-execution is planned anyway",
        path="src/kiro_crew/migration/taskrun_adapter.py",
        old='        mid = {"in_progress", "reviewing"}',
        new="        mid = set()",
        test=(
            "test/test_migration_move_cli.py::"
            "test_taskrun_move_refuses_a_run_with_a_task_mid_execution"
        ),
    ),
    # ── session plan: non-portable references must be reported ────────────
    Mutation(
        gap="session plan",
        what="non-portable references are shipped instead of reported",
        path="src/kiro_crew/migration/session_adapter.py",
        old="    for key in SESSION_NONPORTABLE:",
        new="    for key in []:",
        test=(
            "test/test_migration_session_adapter.py::"
            "test_dropped_references_are_reported_not_swallowed"
        ),
    ),
    # ── frontend: the reachable surfaces ──────────────────────────────────
    Mutation(
        gap="frontend",
        what="the Schedule row stops offering 'Move to crew…'",
        path="website/src/components/CronRowActions.tsx",
        old="{onMoveToCrew && (",
        new="{false && onMoveToCrew && (",
        test="src/components/CronRowActions.moveToCrew.test.tsx",
        runner="vitest",
    ),
    Mutation(
        gap="frontend",
        what="the dialog stops requiring a target crew",
        path="website/src/components/MoveToCrewDialog.tsx",
        old="    if (!target) {\n      setValidationHint(i18nT('components.moveToCrew.error_target_required'))\n      return\n    }",
        new="    if (!target) {\n      setValidationHint(i18nT('components.moveToCrew.error_target_required'))\n    }",
        test="src/components/MoveToCrewDialog.cov80.test.tsx",
        runner="vitest",
    ),
    Mutation(
        gap="frontend",
        what="validation and request failures share one surface again",
        path="website/src/components/MoveToCrewDialog.tsx",
        old="      setValidationHint(i18nT('components.moveToCrew.error_target_required'))",
        new="      setError(i18nT('components.moveToCrew.error_target_required'))",
        test="src/components/MoveToCrewDialog.cov80.test.tsx",
        runner="vitest",
    ),
    Mutation(
        gap="frontend",
        what="the plan grid goes back to two columns at every width",
        path="website/src/components/MoveToCrewDialog.tsx",
        old='className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[max-content_1fr]',
        new='className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 sm:x-[max-content_1fr]',
        test="src/components/MoveToCrewDialog.cov80.test.tsx",
        runner="vitest",
    ),
    Mutation(
        gap="secrecy",
        what="an app token can probe for foreign slots again",
        path="src/kiro_crew/dashboard/handlers/migration.py",
        old='    denied = _check_slot_ownership(request, slot, "migration.session_move")',
        new="    denied = None",
        test=(
            "test/test_api_migration_move.py::"
            "test_an_app_token_cannot_learn_a_foreign_slot_exists"
        ),
    ),
    Mutation(
        gap="validation",
        what="an unstable snapshot becomes a 500 again",
        path="src/kiro_crew/dashboard/handlers/migration.py",
        old="    except SnapshotUnstable as exc:",
        new="    except _NeverRaised as exc:  # noqa: F821",
        test=(
            "test/test_api_migration_move.py::"
            "test_an_unstable_session_snapshot_is_a_retryable_503_not_a_500"
        ),
    ),
    Mutation(
        gap="validation",
        what="a scalar runs registry is iterated and crashes",
        path="src/kiro_crew/cli_commands.py",
        old="    if not isinstance(records, list):",
        new="    if False:",
        test=(
            "test/test_migration_move_cli.py::"
            "test_taskrun_move_refuses_a_runs_registry_that_is_not_a_list"
        ),
    ),
    Mutation(
        gap="frontend",
        what="the plan-only correction goes back to muted small print",
        path="website/src/components/MoveToCrewDialog.tsx",
        old='            className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 font-medium text-warn"',
        new='            className="text-[12px] text-muted"',
        test="src/components/MoveToCrewDialog.cov80.test.tsx",
        runner="vitest",
    ),
    Mutation(
        gap="frontend",
        what="SchedulePage stops wiring the handler (button becomes dead code)",
        path="website/src/pages/SchedulePage.tsx",
        old="onMoveToCrew={() => setMovingJobId(j.id)}",
        new="",
        test="src/components/CronRowActions.moveToCrew.test.tsx",
        runner="vitest",
    ),
]


def run_test(m: Mutation) -> bool:
    """True when the test PASSES."""
    if m.runner == "pytest":
        cmd = [str(PYTEST), "-q", "-p", "no:cacheprovider", "-n0", "--timeout=120", m.test]
        cwd = REPO
    else:
        cmd = ["npx", "vitest", "run", m.test]
        cwd = REPO / "website"
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    return proc.returncode == 0


def main() -> int:
    results: list[tuple[Mutation, str]] = []
    for m in MUTATIONS:
        path = REPO / m.path
        original = path.read_text(encoding="utf-8")
        if m.old not in original:
            results.append((m, "SKIPPED (anchor not found)"))
            continue
        try:
            path.write_text(original.replace(m.old, m.new, 1), encoding="utf-8")
            passed = run_test(m)
            results.append((m, "SURVIVED" if passed else "caught"))
        finally:
            path.write_text(original, encoding="utf-8")  # always restore

    survived = [r for r in results if r[1] != "caught"]
    print("\n=== mutation sweep: crew-work-migration (#7577) ===\n")
    for m, outcome in results:
        mark = "✓" if outcome == "caught" else "✗"
        print(f" {mark} [gap {m.gap}] {m.what}")
        print(f"     -> {outcome}  ({m.test.split('::')[-1]})")
    print(f"\n{len(results) - len(survived)}/{len(results)} mutations caught")
    if survived:
        print("\nSURVIVING mutations mean those tests have no teeth:")
        for m, outcome in survived:
            print(f"  - [gap {m.gap}] {m.what} :: {outcome}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
