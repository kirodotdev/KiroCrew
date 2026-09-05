#!/usr/bin/env python3
"""Mutation sweep for the crew-work-migration feature (issue #7577).

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

REPO = pathlib.Path("/home/ec2-user/kirocrew")
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
    # ── Circle 2: preflight must fail CLOSED ───────────────────────────────
    Mutation(
        gap="c2 fail-open",
        what="no probe means every requirement is admitted (the original defect)",
        path="src/kiro_crew/migration/receiver.py",
        old="        if self._probe is None:\n            # Nothing to verify means nothing to refuse.",
        new="        if self._probe is None:\n            return P.PreflightReport(findings=[])",
        test=(
            "test/test_migration_receiver.py::"
            "test_preflight_with_no_probe_refuses_a_bundle_that_has_requirements"
        ),
    ),
    Mutation(
        gap="c2 fail-open",
        what="an unprobed requirement kind is silently skipped again",
        path="src/kiro_crew/migration/receiver.py",
        old="            if check is None:",
        new="            if check is None:\n                continue",
        test=(
            "test/test_migration_receiver.py::"
            "test_preflight_refuses_a_requirement_kind_it_cannot_check"
        ),
    ),
    Mutation(
        gap="c2 fail-open",
        what="closing the hole over-corrects into fail-CLOSED for units needing nothing",
        path="src/kiro_crew/migration/receiver.py",
        old="        findings: list[P.Finding] = []\n\n        if self._probe is None:",
        new='        findings: list[P.Finding] = [\n            P.Finding(kind="agent", detail="x", severity="blocking", detail_key="x")\n        ]\n\n        if self._probe is None:',
        test=(
            "test/test_migration_receiver.py::"
            "test_preflight_with_no_probe_still_admits_a_bundle_needing_nothing"
        ),
    ),
    # ── Circle 1: the crash window a RESTART must resolve ──────────────────
    Mutation(
        gap="c1 journal",
        what="the in-flight handoff is never journalled, so a reboot cannot find it",
        path="src/kiro_crew/migration/protocol.py",
        old="        if self._journal is not None:\n            self._journal.open(",
        new="        if False:\n            self._journal.open(",
        test=(
            "test/test_migration_protocol.py::"
            "test_an_outstanding_handoff_is_discoverable_after_a_restart"
        ),
    ),
    Mutation(
        gap="c1 journal",
        what="a settled handoff is left in the journal, so every boot replays history",
        path="src/kiro_crew/migration/journal.py",
        old="        if data.pop(handoff_id, None) is not None:\n            self._write_all(data)",
        new="        pass  # MUTATION: never settle an entry",
        test=(
            "test/test_migration_protocol.py::"
            "test_a_completed_migration_leaves_nothing_to_reconcile"
        ),
    ),
    Mutation(
        gap="c1 journal",
        what="an unanswerable receiver has its window DROPPED instead of kept",
        path="src/kiro_crew/migration/protocol.py",
        old='                self._log("reconcile.unresolved", outcome="unknown", reason=str(exc))\n                continue',
        new='                self._log("reconcile.unresolved", outcome="unknown", reason=str(exc))\n                self._journal.close(entry.handoff_id)\n                continue',
        test=(
            "test/test_migration_protocol.py::"
            "test_an_unanswerable_receiver_keeps_its_window_open"
        ),
    ),
    # ── Gap 1: slice-1 protocol tests never observed failing ──────────────
    Mutation(
        gap="1 protocol",
        what="allow-list stops filtering (ships every field)",
        path="src/kiro_crew/migration/protocol.py",
        old="return {k: source[k] for k in allowed if k in source}",
        new="return dict(source)",
        test="test/test_migration_protocol.py::test_allow_list_serialize_drops_unnamed_fields",
    ),
    Mutation(
        gap="1 protocol",
        what="PreflightReport.blocked never blocks",
        path="src/kiro_crew/migration/protocol.py",
        old='return any(f.severity == "blocking" for f in self.findings)',
        new="return False",
        test="test/test_migration_protocol.py::test_preflight_report_blocked_property",
    ),
    Mutation(
        gap="1 protocol",
        what="a pre-ack failure no longer un-quiesces (source loses ownership)",
        path="src/kiro_crew/migration/protocol.py",
        old="            await self.adapter.unquiesce(unit_id, token)",
        new="            pass  # MUTATION: skip rollback",
        test="test/test_migration_protocol.py::test_transmit_failure_after_quiesce_unquiesces_and_retains_ownership",
    ),
    Mutation(
        gap="1 protocol",
        what="a blocking preflight reports 'failed' instead of 'refused'",
        path="src/kiro_crew/migration/protocol.py",
        old='outcome="refused", report=report, reason="preflight reported a blocking finding"',
        new='outcome="failed", report=report, reason="preflight reported a blocking finding"',
        test="test/test_migration_protocol.py::test_preflight_blocking_refuses_without_quiescing",
    ),
    Mutation(
        gap="1 protocol",
        what="the secret scan finds nothing",
        path="src/kiro_crew/migration/protocol.py",
        old="    findings: list[Finding] = []\n    for text in _walk_strings(payload):",
        new="    findings: list[Finding] = []\n    for text in []:",
        test="test/test_migration_protocol.py::test_credential_scan_flags_blocking_and_no_secret_in_bundle",
    ),
    Mutation(
        gap="1 protocol",
        what="reconciliation always hands the unit back to the source",
        path="src/kiro_crew/migration/protocol.py",
        old="        if held is not None:",
        new="        if False:",
        test="test/test_migration_protocol.py::test_reconciliation_after_ack_before_tombstone_converges_to_target",
    ),
    # ── Gap 2: green-on-first-run characterisation tests ──────────────────
    Mutation(
        gap="2 characterisation",
        what="ownership is never released (no tombstone after ack)",
        path="src/kiro_crew/migration/protocol.py",
        old="        await self.adapter.tombstone(unit_id, self.target_crew, ack.unit_id)",
        new="        pass  # MUTATION: never release",
        test="test/test_migration_reversibility.py::test_single_owner_holds_at_every_hop_of_the_round_trip",
    ),
    Mutation(
        gap="2 characterisation",
        what="the target does not re-bind the owning scope",
        path="src/kiro_crew/migration/cron_adapter.py",
        old='fields["session_key"] = self._target_session_key',
        new="pass  # MUTATION: keep the source scope",
        test="test/test_migration_integration.py::test_cron_migrates_end_to_end_source_released_target_owns",
    ),
    # ── Gap 3: frontend implemented before its tests ──────────────────────
    Mutation(
        gap="3 frontend",
        what="the Schedule row stops offering 'Move to crew…'",
        path="website/src/components/CronRowActions.tsx",
        old="{onMoveToCrew && (",
        new="{false && onMoveToCrew && (",
        test="src/components/CronRowActions.moveToCrew.test.tsx",
        runner="vitest",
    ),
    Mutation(
        gap="3 frontend",
        what="the dialog stops requiring a target crew",
        path="website/src/components/MoveToCrewDialog.tsx",
        # Anchored on the CONTROL FLOW, not on the message text. The previous
        # anchor quoted the literal 'A target crew is required.', which the i18n
        # pass replaced with an i18nT() key -- silently disabling this mutation.
        # The guard's early return is what the test actually asserts, and it
        # survives any rewording or re-translation.
        old="    if (!target) {\n      setError(i18nT('components.moveToCrew.error_target_required'))\n      return\n    }",
        new="    if (!target) {\n      setError(i18nT('components.moveToCrew.error_target_required'))\n    }",
        test="src/components/MoveToCrewDialog.cov80.test.tsx",
        runner="vitest",
    ),
    Mutation(
        gap="3 frontend",
        what="SchedulePage stops wiring the handler (button becomes dead code)",
        path="website/src/pages/SchedulePage.tsx",
        old="onMoveToCrew={() => setMovingJobId(j.id)}",
        new="",
        test="src/components/CronRowActions.moveToCrew.test.tsx",
        runner="vitest",
    ),
    # ── Tech-debt fixes: the new invariants must have teeth too ───────────
    Mutation(
        gap="debt 3.1 audit",
        what="the audit sink is never called (only the in-process list)",
        path="src/kiro_crew/migration/protocol.py",
        old="            sink(entry)",
        new="            pass  # MUTATION: never audit",
        test="test/test_migration_protocol.py::test_a_completed_migration_is_audited_with_duration",
    ),
    Mutation(
        gap="debt 3.4 versions",
        what="the receiver stops checking bundle_version",
        path="src/kiro_crew/migration/receiver.py",
        old="if bundle.bundle_version not in known:",
        new="if False:",
        test="test/test_migration_receiver.py::test_accept_refuses_a_future_bundle_version",
    ),
    Mutation(
        gap="debt 3.5 secrets",
        what="the receiver stops re-scanning for credential material",
        path="src/kiro_crew/migration/receiver.py",
        old="secrets = P.scan_for_secrets(bundle.payload)",
        new="secrets = []  # MUTATION: trust the sender",
        test="test/test_migration_receiver.py::test_accept_refuses_a_payload_carrying_credential_material",
    ),
    Mutation(
        gap="debt 3.3 session reqs",
        what="session requirements stop naming the MCP servers",
        path="src/kiro_crew/migration/session_adapter.py",
        # Anchored on the LOOP rather than the append: black rewrapped the
        # append across three lines, which silently un-anchored this mutation.
        # A one-line loop header survives any wrapping of its body.
        old='for server in raw.get("mcp_servers") or []:',
        new="for server in []:  # MUTATION: stop naming the MCP servers",
        test="test/test_migration_session_adapter.py::test_session_requirements_name_each_mcp_server",
    ),
    # ── Task 5.2: the tombstone must stay discoverable ────────────────────
    Mutation(
        gap="5.2 durability",
        what="the registry never writes, so a restart forgets the move",
        path="src/kiro_crew/migration/tombstones.py",
        old="        data.setdefault(kind, {})[unit_id] = self._to_json(tombstone)\n        self._write_all(data)",
        new="        data.setdefault(kind, {})[unit_id] = self._to_json(tombstone)",
        test="test/test_migration_tombstones.py::test_a_tombstone_survives_a_restart",
    ),
    Mutation(
        gap="5.2 move-back",
        what="clear() is a no-op, so a returned unit still reads as moved away",
        path="src/kiro_crew/migration/tombstones.py",
        old="        del bucket[unit_id]",
        new="        pass  # MUTATION: never clear",
        test="test/test_migration_tombstones.py::test_a_unit_that_came_back_is_no_longer_tombstoned",
    ),
    Mutation(
        gap="5.2 adapter wiring",
        what="the cron adapter stops recording into the registry",
        path="src/kiro_crew/migration/cron_adapter.py",
        old="            self._registry.record(self.bundle_kind, unit_id,",
        new="            _skip = (self.bundle_kind, unit_id) and (lambda *a: None)(",
        test="test/test_migration_tombstones.py::test_the_cron_adapter_records_into_the_registry",
    ),
    Mutation(
        gap="5.2 listing surface",
        what="cron list stops printing where a migrated job went",
        path="src/kiro_crew/cli_commands.py",
        # Anchored on the literal, not on `print(`: black moved the call onto
        # its own line. Emptying the first fragment removes the "migrated to
        # <crew>" text the test looks for, while the implicit concatenation
        # stays syntactically valid.
        old='f"      ↪ migrated to {where} "',
        new='f""',
        test="test/test_migration_tombstones.py::test_cron_list_tells_the_user_where_a_migrated_job_went",
    ),
    # ── A3/A4: discoverability for the other kinds and the dashboard ──────
    Mutation(
        gap="A3 session",
        what="the session adapter stops recording its destination durably",
        path="src/kiro_crew/migration/session_adapter.py",
        old="            self._registry.record(self.bundle_kind, unit_id,",
        new="            _skip = (self.bundle_kind, unit_id) and (lambda *a: None)(",
        test="test/test_migration_tombstones.py::test_a_migrated_session_is_discoverable_after_a_restart",
    ),
    Mutation(
        gap="A3 taskrun",
        what="the task-run adapter stops recording its destination durably",
        path="src/kiro_crew/migration/taskrun_adapter.py",
        old="            self._registry.record(self.bundle_kind, unit_id,",
        new="            _skip = (self.bundle_kind, unit_id) and (lambda *a: None)(",
        test="test/test_migration_tombstones.py::test_a_migrated_task_run_is_discoverable_after_a_restart",
    ),
    Mutation(
        gap="A4 dashboard",
        what="the cron list endpoint stops reporting the redirect",
        path="src/kiro_crew/dashboard/handlers/cron.py",
        old='        row["migrated_to"] = None if tomb is None else {',
        new='        row["migrated_to"] = None if True else {',
        test="test/test_api_migration_move.py::test_the_cron_list_endpoint_reports_a_migrated_job",
    ),
    Mutation(
        gap="A4 frontend",
        what="the badge renders nothing even for a migrated job",
        path="website/src/components/MigratedBadge.tsx",
        old="  if (!migratedTo) return null",
        new="  if (true) return null",
        test="src/components/MigratedBadge.test.tsx",
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
