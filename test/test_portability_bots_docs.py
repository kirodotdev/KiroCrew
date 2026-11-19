"""The owning specs document how an instance export and import keep each Bot whole.

Each module spec that owns a part of the Bot round trip must name the identifiers a
reader needs to find that part in the code: the manifest key and roster merge in the
config spec, the member-thread rule and binding rebuild in the history spec, the
regenerate-never-carry rules in the security spec, the minimal crewmate file in the
crew-mode spec, the page snapshot rebuild in the dashboard-instances spec and the
add-only package install in the artifacts spec. Each identifier is asserted as a
code span, the way the specs cite code, so a word in passing prose does not pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MODULES = Path(__file__).resolve().parent.parent / "docs" / "system-specs" / "modules"

#: Doc file name -> the identifiers its Bot export/import text must cite.
REQUIRED_IDENTIFIERS: dict[str, tuple[str, ...]] = {
    "config.md": (
        "bot_stores",
        "merge_roster",
        "INSTANCE_REPLACE_COMPONENTS",
        "summary.bots",
        "same_bot",
        "kept",
        "merged_entry",
        "RESERVED_DEFAULT_MEMBER",
        "config_installed",
        "briefing",
        "bot_dashboards_not_carried",
        "crewmate_file_notices",
        "held_by_other_bot",
        "owned_slugs",
        "before_import",
        "merged_store_entry",
        "export_bots",
        "bots_failed",
        "raw_entry_identity",
        "release_for_create",
        "read_fd_at_most",
        "was_here",
        "restore_crewmate_files",
        "crewmate_name_held",
        "teams_installed",
        "MAX_RECORD_BYTES",
        "BOT_ARTIFACTS_DIR",
        "bot-artifacts/<P>/",
        "fresh_home",
        "workspace_taken",
        "publish_member_workspace",
        "dynamic_capabilities",
        "worker_reasoning_effort",
        "foreign_owner",
        "crewmate_file_state",
        "report_items",
        "report_lines",
        "MAX_BOT_ROWS",
        "is_reserved_bot_name",
        "CARRIED_MEMBER_SINGLE_FILES",
        "mate_agents_dir",
        "another_package_of_this_bot",
        "import_bot_not_set_up",
        "part_reasons",
        "in_roster",
        "binding_ladder",
        "reserved_slugs",
        "_destination_reserved_slugs",
        "import_bot_not_set_up_no_reason",
        "_minimal_file_bots",
    ),
    "history.md": (
        "member_stem_allowed",
        "sessions_member_withheld",
        "bot_transcripts_orphaned",
        "rebuild_bindings",
        "kept",
        "binding_exists_unusable",
        "not_needed",
        "not_rebuilt",
        "sessions_skipped_existing",
        "member_thread_withheld",
        "REBUILD",
        "is_reserved_bot_name",
        "reserved_slugs",
        "binding_ladder",
        "sessions_listing",
        "held_by_other_bot",
        "owned_slugs",
        "_destination_reserved_slugs",
    ),
    "security.md": (
        "archive_binding_disagrees",
        "carried_member_file",
        "activity.jsonl",
        "merged_entry",
        "approval_mode",
        "acp_backend",
        "merged_store_entry",
        "held_by_other_bot",
        "owned_slugs",
        "before_import",
        "raw_entry_identity",
        "read_fd_at_most",
        "slug_is_well_formed",
        "publish_dir_noreplace",
        "crewmate_name_held",
        "BOT_ARTIFACTS_DIR",
        "bot-artifacts/<P>/",
        "fresh_home",
        "dynamic_capabilities",
        "copy_file_pinned",
        "REFUSED_META_FIELDS",
        "STRIPPED_META_FIELDS",
        ".bot-stage-",
        "foreign_owner",
        "crewmate_file_foreign",
        "worker_reasoning_effort",
        "another_package_of_this_bot",
        "_owned_package",
        "REFUSED_META_EMPTY",
        "UNSTAGED_ARTIFACT_FILES",
        "write_new_file_pinned",
        "atomic_write_at",
        "binding_ladder",
        "is_reserved_bot_name",
        "is_minimal_crewmate_file",
    ),
    "crew-mode.md": (
        "only_if_absent",
        "starting_abilities_only",
        "rename_noreplace",
        "safe_path_component",
        "restore_crewmate_files",
        "crewmate_file_on_record",
        "fresh_home",
        "os.rename",
        "foreign_owner",
        "crewmate_file_foreign",
        "crewmate_file_state",
        "write_new_file_pinned",
        "import_bot_agent_file_held",
        "MAX_OWNERS_BYTES",
        "_read_owner_bytes",
        "is_minimal_crewmate_file",
        "crewmate_file_notices",
        "held_by_other_bot",
        "owned_slugs",
    ),
    "dashboard-instances.md": (
        "_write_page_snapshot",
        "page_not_rebuilt",
        "held_by_other_bot",
        "REFUSED_META_FIELDS",
        "STRIPPED_META_FIELDS",
        "REFUSED_META_EMPTY",
        "comments.json",
        "ROLLED_BACK_RECORD",
        "BoundIndex",
        "scan_bound_dashboards",
        "another_package_of_this_bot",
        "_owned_package",
        "is_live_pointer",
        "live_binding",
    ),
    "artifacts.md": (
        "portability_bot_stores",
        "publish_dir_noreplace",
        "BOT_ARTIFACTS_DIR",
        "bot-artifacts/<P>/",
        "copy_file_pinned",
        "REFUSED_META_FIELDS",
        "STRIPPED_META_FIELDS",
        "_check_staged_artifact",
        ".bot-stage-",
        "REFUSED_META_EMPTY",
        "UNSTAGED_ARTIFACT_FILES",
        "comments.json",
        "artifact_missing",
        "ROLLED_BACK_RECORD",
        "ROLLED_BACK_PACKAGE",
        "BoundIndex",
        "scan_bound_dashboards",
        "BoundScan",
        "another_package_of_this_bot",
        "_owned_package",
        "is_live_pointer",
        "live_binding",
        "_archive_carries",
    ),
}

_CASES = [
    (doc, identifier)
    for doc, identifiers in REQUIRED_IDENTIFIERS.items()
    for identifier in identifiers
]


def _cites(text: str, identifier: str) -> bool:
    """True when *identifier* appears inside a backtick code span of *text*."""
    for span in text.split("`")[1::2]:
        if identifier in span:
            return True
    return False


#: Identifiers that name nothing in the code (each rule has one name): no owning doc cites them.
REMOVED_IDENTIFIERS: tuple[str, ...] = (
    "crewmate_file_is_own",
    "binding_verdict",
    "portability_bot_types.read_crewmate_file",
)


@pytest.mark.parametrize("identifier", REMOVED_IDENTIFIERS)
def test_no_doc_cites_a_removed_identifier(identifier: str) -> None:
    citing = [
        doc
        for doc in REQUIRED_IDENTIFIERS
        if _cites((_MODULES / doc).read_text(encoding="utf-8"), identifier)
    ]
    assert not citing, f"{citing} still cite `{identifier}`"


def test_every_owning_doc_is_present_and_nonempty() -> None:
    for doc in REQUIRED_IDENTIFIERS:
        path = _MODULES / doc
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8").strip(), path


@pytest.mark.parametrize(("doc", "identifier"), _CASES)
def test_doc_cites_identifier(doc: str, identifier: str) -> None:
    text = (_MODULES / doc).read_text(encoding="utf-8")
    assert _cites(text, identifier), f"{doc} does not cite `{identifier}`"


def test_cites_ignores_bare_prose() -> None:
    assert _cites("uses `merge_roster(config, mc)` here", "merge_roster")
    assert not _cites("uses merge_roster here", "merge_roster")
    assert not _cites("`a` merge_roster `b`", "merge_roster")


def test_security_doc_states_no_fence_changed_and_why_crewmates_stay_behind() -> None:
    text = (_MODULES / "security.md").read_text(encoding="utf-8")
    heading = "### Bots in an instance export and import"
    assert heading in text
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    assert "without changing any fence" in section
    assert "Bindings are regenerated, never carried" in section
    assert "Page snapshots (`crew-panels/`) are regenerated, never carried" in section
    assert "Crewmate files are never carried, because of their grant keys" in section
    for grant_key in ("allowedTools", "autoApprove", "permissions", "mcpServers"):
        assert _cites(section, grant_key), grant_key
    assert _cites(section, "carried_member_file")
    assert _cites(section, "activity.jsonl")


def test_history_doc_orders_the_binding_checks() -> None:
    text = (_MODULES / "history.md").read_text(encoding="utf-8")
    order = [
        "`held_by_other_bot`. Otherwise",
        "is\n   `kept` (whole)",
        "`binding_exists_unusable`, even when",
        "is `not_needed`",
        "is `transcript_predates_import`",
        "`archive_binding_disagrees`",
    ]
    positions = [text.find(marker) for marker in order]
    assert all(pos >= 0 for pos in positions), dict(zip(order, positions))
    assert positions == sorted(positions)
