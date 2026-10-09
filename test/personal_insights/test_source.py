from __future__ import annotations

import json
import uuid

import pytest

from kiro_crew.personal_insights.insights_adapter import (
    AdapterError,
    AdapterGroupRejected,
    SourceRecord,
    build_projection_input,
)
from kiro_crew.personal_insights.insights_snapshot import (
    AllUnknownProofError,
    CompletenessDowngradeError,
    ExportPage,
    OversizedSourceError,
    SnapshotError,
    SnapshotInvalidatedError,
    SnapshotManifest,
    enforce_completeness_downgrade,
    export_session,
    require_decidable_corpus,
)
from kiro_crew.personal_insights.insights_source import (
    AuthorityChangeError,
    CatalogPin,
)
from kiro_crew.personal_insights.insights_source import OversizedSourceError as SourceOversizedError
from kiro_crew.personal_insights.insights_source import (
    RawEvent,
    SourceError,
    WorkspaceIdentityMap,
    admit_raw_events,
    canonical_project_from_slot,
    catalog_entry_from_host,
    catalog_page,
    content_revision_for_catalog,
    pin_scope,
    safe_title,
    select_catalog_entries,
)

PROJECTION_INPUT_SCHEMA = "agent-session-intelligence.projection-input/2.0"


class _Slot:
    def __init__(self, project, workspace):
        self.project = project
        self.workspace = workspace


def _pin(workspace="w"):
    return CatalogPin(
        authenticated_principal="p",
        workspace_id=workspace,
        execution_platform="linux",
        window_start="2026-09-09T00:00:00Z",
        window_end="2026-10-09T00:00:00Z",
    )


def _row(**over):
    base = {
        "session_key": "s",
        "owner_id": "p",
        "workspace_id": "w",
        "origin": "dashboard",
        "session_kind": "user",
        "title": "t",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "message_count": 3,
        "content_revision": None,
        "history_completeness_hint": "complete",
        "temporary": False,
        "incognito": False,
        "deleted": False,
        "accessible": True,
    }
    base.update(over)
    return base


# ── Workspace identity ──


def test_workspace_identity_from_slot_project_not_memory_label(tmp_path):
    identity = WorkspaceIdentityMap(home=tmp_path)
    slot = _Slot(project="/abs/canonical/project", workspace="memory-label")
    by_project = identity.workspace_id_for(canonical_project_from_slot(slot))
    assert canonical_project_from_slot(slot) == "/abs/canonical/project"
    with pytest.raises(SourceError):
        identity.workspace_id_for(slot.workspace)
    assert by_project == identity.workspace_id_for(slot.project)


def test_workspace_identity_stable_across_independent_processes(tmp_path):
    first = WorkspaceIdentityMap(home=tmp_path).workspace_id_for("/abs/p")
    second = WorkspaceIdentityMap(home=tmp_path).workspace_id_for("/abs/p")
    assert first == second


def test_moved_path_gets_new_identity(tmp_path):
    identity = WorkspaceIdentityMap(home=tmp_path)
    assert identity.workspace_id_for("/abs/old") != identity.workspace_id_for("/abs/new")


def test_slot_without_project_rejected():
    with pytest.raises(SourceError):
        canonical_project_from_slot(_Slot(project=None, workspace="label"))


# ── Pin and override rejection ──


def test_pin_rejects_client_account_project_workspace_platform(tmp_path):
    identity = WorkspaceIdentityMap(home=tmp_path)
    for forbidden in ("account", "project", "workspace_id", "platform", "execution_platform"):
        with pytest.raises(Exception):
            pin_scope(
                authenticated_principal="p",
                canonical_project_path="/abs/p",
                server_platform="linux",
                identity_map=identity,
                request_fields={forbidden: "x"},
                window_start="2026-09-09T00:00:00Z",
                window_end="2026-10-09T00:00:00Z",
            )


def test_pin_derives_server_values(tmp_path):
    identity = WorkspaceIdentityMap(home=tmp_path)
    pin = pin_scope(
        authenticated_principal="p",
        canonical_project_path="/abs/p",
        server_platform="linux",
        identity_map=identity,
        request_fields={},
        window_start="2026-09-09T00:00:00Z",
        window_end="2026-10-09T00:00:00Z",
    )
    assert pin.workspace_id == identity.workspace_id_for("/abs/p")
    assert pin.execution_platform == "linux"


# ── Catalog ──


def test_catalog_count_window_and_hard_exclusions():
    pin = _pin()
    rows = [
        _row(session_key="ok"),
        _row(session_key="cli", origin="cli"),
        _row(session_key="cron", session_kind="cron"),
        _row(session_key="temp", temporary=True),
        _row(session_key="incog", incognito=True),
        _row(session_key="del", deleted=True),
        _row(session_key="banana", origin="banana"),
    ]
    selection = select_catalog_entries(rows, pin=pin, excluded_keys=set())
    assert selection.count == 1
    assert {e.session_key for e in selection.included} == {"ok"}
    reasons = {x.session_key: x.reason for x in selection.excluded}
    assert reasons["temp"] == "temporary"
    assert reasons["incog"] == "incognito"
    assert reasons["del"] == "deleted"
    assert reasons["cli"] == "origin_not_dashboard"
    assert reasons["cron"] == "session_kind_not_user"
    assert reasons["banana"] == "origin_unknown"


def test_catalog_deterministic_cursor_order():
    pin = _pin()
    rows = [
        _row(session_key="a", updated_at="2026-10-01T00:00:00Z"),
        _row(session_key="b", updated_at="2026-10-03T00:00:00Z"),
        _row(session_key="c", updated_at="2026-10-02T00:00:00Z"),
    ]
    selection = select_catalog_entries(rows, pin=pin, excluded_keys=set())
    assert [e.session_key for e in selection.included] == ["b", "c", "a"]


def test_catalog_rejects_over_max():
    pin = _pin()
    rows = [_row(session_key=f"s{i}") for i in range(201)]
    with pytest.raises(SourceOversizedError):
        select_catalog_entries(rows, pin=pin, excluded_keys=set())


def test_user_excluded_marked():
    pin = _pin()
    selection = select_catalog_entries(
        [_row(session_key="keep"), _row(session_key="drop")], pin=pin, excluded_keys={"drop"}
    )
    assert any(x.session_key == "drop" and x.reason == "user_excluded" for x in selection.excluded)


def test_origin_and_kind_unknown_values_excluded():
    pin = _pin()
    selection = select_catalog_entries(
        [_row(session_key="u", origin="made-up")], pin=pin, excluded_keys=set()
    )
    assert selection.count == 0


def test_title_redacted_and_escaped_in_memory():
    assert safe_title("ssh-rsa AAAAsecret", "user", "abcdefgh12345") == "user:abcdefgh"
    assert safe_title("<script>", "user", "k") == "&lt;script&gt;"
    assert safe_title(None, "user", "abcdefgh12345") == "user:abcdefgh"


def test_content_revision_always_null():
    assert content_revision_for_catalog(_row(content_revision="rev")) is None


def test_authority_rederived_rejects_second_page_drift():
    pin = _pin()
    first = catalog_entry_from_host(_row(session_key="s1"), pin=pin)
    assert first.session_key == "s1"
    with pytest.raises(AuthorityChangeError):
        catalog_entry_from_host(_row(session_key="s2", owner_id="CHANGED"), pin=pin)
    with pytest.raises(AuthorityChangeError):
        catalog_entry_from_host(_row(session_key="s3", workspace_id="CHANGED"), pin=pin)


# ── Admission ──


def _owner(raw_id, order):
    return RawEvent(
        raw_id,
        order,
        "owner_user",
        "user_visible",
        "message",
        "available",
        "d" * 64,
        "hello text",
        {},
    )


def _tool(raw_id, order):
    return RawEvent(
        raw_id,
        order,
        "tool",
        "metadata_only",
        "tool_call",
        "available",
        "e" * 64,
        "RAW TOOL BODY",
        {},
    )


def test_admission_separates_text_from_bodies():
    raws = [
        _owner("raw-owner-1", 1),
        _tool("raw-call-1", 2),
        RawEvent(
            "raw-sys-1",
            3,
            "system_injected",
            "hidden",
            "other",
            "available",
            "a" * 64,
            "INJECTED",
            {},
        ),
        RawEvent(
            "raw-fake",
            4,
            "owner_user",
            "user_visible",
            "tool_outcome",
            "available",
            "b" * 64,
            "DISGUISED",
            {},
        ),
    ]
    result = admit_raw_events(raws)
    assert result.sidecar_text["raw-owner-1"] == "hello text"
    assert "raw-call-1" not in result.sidecar_text
    assert "raw-sys-1" not in result.sidecar_text
    assert "raw-fake" not in result.sidecar_text


def test_admission_minimizes_secrets():
    raws = [
        RawEvent(
            "r1",
            1,
            "owner_user",
            "user_visible",
            "message",
            "available",
            "d" * 64,
            "key ssh-rsa AAAA",
            {},
        )
    ]
    result = admit_raw_events(raws)
    assert "ssh-rsa" not in result.sidecar_text["r1"]


def test_admission_oversized_excluded():
    with pytest.raises(SourceOversizedError):
        admit_raw_events([_owner(f"r{i}", i + 1) for i in range(5)], max_events=3)


# ── Adapter: text-free DTO, enums, incompatibility, groups, pairing, subagents, timing, floor ──


def _rec(raw_id, actor, event, order, **over):
    return SourceRecord(
        source_event_id=raw_id,
        actor_class=actor,
        event_class=event,
        visibility_class="user_visible",
        sequence=order,
        **over,
    )


def _tool_payload(tool_class="file_read", target=None):
    return {
        "tool_class": tool_class,
        "operation_class": "read",
        "status": "success",
        "error_class": None,
        "operation_shape_id": "shape-1",
        "target_equivalence_id": target,
    }


def test_dto_text_free_and_schema_pinned():
    records = [
        _rec("raw-owner-1", "owner_user", "message", 1),
        _rec("raw-call-1", "tool", "tool_call", 2, responds_to="raw-owner-1", tool=_tool_payload()),
    ]
    result = build_projection_input("source-1", records, mapping_floor=0.5)
    encoded = result.projection_input.to_json_bytes()
    payload = json.loads(encoded)
    assert payload["schema_version"] == PROJECTION_INPUT_SCHEMA
    assert b"hello" not in encoded and b"RAW TOOL BODY" not in encoded
    for event in payload["events"]:
        assert event["source_order"] >= 1
        assert "content" not in event
        assert event["tool"] is None or event["tool"]["duration_ms"] is None


def test_dto_single_line_json():
    result = build_projection_input(
        "source-1", [_rec("r1", "owner_user", "message", 1)], mapping_floor=0.5
    )
    assert b"\n" not in result.projection_input.to_json_bytes()


def test_adapter_rejects_group_fields():
    records = [_rec("r1", "owner_user", "message", 1, extra={"event_group": "g1"})]
    with pytest.raises(AdapterGroupRejected):
        build_projection_input("source-1", records, mapping_floor=0.5)


def test_adapter_rejects_bad_enum():
    records = [_rec("r1", "tool", "tool_call", 1, tool=_tool_payload(tool_class="not-a-class"))]
    with pytest.raises(AdapterError):
        build_projection_input("source-1", records, mapping_floor=0.5)


def test_actor_event_incompatibility_omits_event_locally():
    records = [
        _rec("ok", "owner_user", "message", 1),
        _rec("bad", "owner_user", "tool_outcome", 2, tool=_tool_payload()),
    ]
    result = build_projection_input("source-1", records, mapping_floor=0.5)
    ids = {e.raw_event_id for e in result.projection_input.events}
    assert ids == {"ok"}
    assert ("bad", "actor_event_incompatible") in result.omitted


def test_cross_page_tool_pairing_preserved():
    records = [
        _rec("raw-owner", "owner_user", "message", 1),
        _rec("raw-call", "tool", "tool_call", 2, responds_to="raw-owner", tool=_tool_payload()),
        _rec(
            "raw-outcome", "tool", "tool_outcome", 3, tool_call_ref="raw-call", tool=_tool_payload()
        ),
    ]
    result = build_projection_input("source-1", records, mapping_floor=0.5)
    outcome = [e for e in result.projection_input.events if e.raw_event_id == "raw-outcome"][0]
    assert outcome.tool_call_raw_id == "raw-call"


def test_subagent_unpaired_dispatch_abstains():
    records = [
        _rec("owner", "owner_user", "message", 1),
        _rec("disp", "direct_assistant", "subagent_dispatch", 2),
    ]
    result = build_projection_input("source-1", records, mapping_floor=0.5)
    assert "uses_parallel_delegation" in result.abstained_predicates


def test_subagent_paired_dispatch_does_not_abstain():
    records = [
        _rec("owner", "owner_user", "message", 1),
        _rec("disp", "direct_assistant", "subagent_dispatch", 2),
        _rec("comp", "tool", "subagent_completion", 3, spawn_of="disp"),
    ]
    result = build_projection_input("source-1", records, mapping_floor=0.5)
    assert "uses_parallel_delegation" not in result.abstained_predicates


def test_tool_mapping_floor_and_fractions():
    records = [
        _rec(f"c{i}", "tool", "tool_call", i + 1, tool=_tool_payload(tool_class="file_read"))
        for i in range(3)
    ] + [_rec("u", "tool", "tool_call", 10, tool=_tool_payload(tool_class="unknown"))]
    above = build_projection_input("source-1", records, mapping_floor=0.5)
    assert above.coverage.above_floor is True
    assert abs(above.coverage.concrete_fraction - 0.75) < 1e-9
    below = build_projection_input("source-1", records, mapping_floor=0.9)
    assert below.coverage.above_floor is False


# ── Snapshot ──


def _page(snapshot_id, ceiling, completeness, events, cursor, end):
    return ExportPage(snapshot_id, ceiling, completeness, tuple(events), cursor, end)


def test_snapshot_multi_page_append_excluded():
    manifest = SnapshotManifest(max_events=100, max_bytes=100000)
    pages = iter(
        [
            _page("snap-1", 2, "complete", [{"sequence": 1}], "cur", False),
            _page("snap-1", 2, "complete", [{"sequence": 2}, {"sequence": 3}], None, True),
        ]
    )
    buffer = export_session("s", lambda cursor: next(pages), manifest)
    orders = sorted(int(e["sequence"]) for e in buffer.events)
    assert orders == [1, 2]
    assert buffer.complete is True


def test_snapshot_oversized_whole_session():
    manifest = SnapshotManifest(max_events=1, max_bytes=100000)
    pages = iter([_page("snap-1", 5, "complete", [{"sequence": 1}, {"sequence": 2}], None, True)])
    with pytest.raises(OversizedSourceError):
        export_session("s", lambda cursor: next(pages), manifest)


def test_snapshot_one_retry_then_second_invalidation_excludes():
    manifest = SnapshotManifest(max_events=100, max_bytes=100000)
    state = {"attempt": 0}

    def read(cursor):
        state["attempt"] += 1
        raise SnapshotInvalidatedError("s")

    with pytest.raises(SnapshotInvalidatedError):
        export_session("s", read, manifest)
    assert state["attempt"] == 2


def test_completeness_downgrade_invalidates():
    with pytest.raises(CompletenessDowngradeError):
        enforce_completeness_downgrade("complete", "prefix_unavailable")
    enforce_completeness_downgrade("prefix_unavailable", "prefix_unavailable")


def test_all_unknown_corpus_fails():
    with pytest.raises(AllUnknownProofError):
        require_decidable_corpus(["unknown", "unknown"])
    require_decidable_corpus(["unknown", "complete"])


def test_parent_workspace_id_is_random_v4_and_persisted(tmp_path):
    identity = WorkspaceIdentityMap(tmp_path)
    workspace_id = identity.workspace_id_for(str(tmp_path / "project"))
    assert uuid.UUID(workspace_id).version == 4
    assert (
        WorkspaceIdentityMap(tmp_path).workspace_id_for(str(tmp_path / "project")) == workspace_id
    )


def test_parent_safe_title_redacts_email_and_controls():
    assert (
        safe_title("owner@example.com\x00<script>", "user", "session-key")
        == "[email]&lt;script&gt;"
    )


def test_parent_catalog_page_applies_window_and_deterministic_cursor(tmp_path):
    from kiro_crew.personal_insights.insights_source import catalog_page

    pin = _pin()
    rows = [
        _row(session_key="new", updated_at="2026-10-08T00:00:00Z"),
        _row(session_key="old", updated_at="2025-01-01T00:00:00Z"),
        _row(session_key="mid", updated_at="2026-10-07T00:00:00Z"),
    ]
    first = catalog_page(rows, pin, set(), None, 1)
    again = catalog_page(rows, pin, set(), None, 1)
    assert first == again
    assert first.exact_count == 2
    assert [entry.session_key for entry in first.entries] == ["new"]
    assert first.next_cursor is not None
    second = catalog_page(rows, pin, set(), first.next_cursor, 1)
    assert [entry.session_key for entry in second.entries] == ["mid"]
    assert second.next_cursor is None


def test_parent_null_revision_forces_export_and_digest_comparison():
    from kiro_crew.personal_insights.insights_source import warm_reuse_decision

    assert warm_reuse_decision(None, "same", 3, 3, 8, 8) == "export_and_compare_digest"


def test_parent_adapter_requires_manifest_mapping_floor():
    import inspect

    parameter = inspect.signature(build_projection_input).parameters["mapping_floor"]
    assert parameter.default is inspect.Parameter.empty


def test_parent_adapter_rejects_partial_target_digest():
    record = _rec(
        "tool",
        "tool",
        "tool_call",
        1,
        tool=_tool_payload(target="te-1-short"),
    )
    with pytest.raises(AdapterError):
        build_projection_input("source", [record], mapping_floor=0.5)


def test_parent_adapter_rejects_duplicate_source_order():
    with pytest.raises(AdapterError):
        build_projection_input(
            "source",
            [
                _rec("a", "owner_user", "message", 1),
                _rec("b", "owner_user", "message", 1),
            ],
            mapping_floor=0.5,
        )


def test_parent_snapshot_rejects_repeated_cursor_and_authority_drift():
    from kiro_crew.personal_insights.insights_snapshot import ExportAuthority, read_snapshot

    pages = iter(
        [
            ExportPage(
                "snap",
                2,
                "complete",
                ({"source_event_id": "one", "sequence": 1},),
                "same",
                False,
                "owner",
                "workspace",
            ),
            ExportPage(
                "snap",
                2,
                "complete",
                ({"source_event_id": "two", "sequence": 2},),
                "same",
                False,
                "owner",
                "workspace",
            ),
        ]
    )
    with pytest.raises(SnapshotError):
        read_snapshot(
            "session",
            ExportAuthority("owner", "workspace"),
            lambda *_args: next(pages),
            SnapshotManifest(4, 4096),
        )


def test_workspace_identity_matches_across_two_subprocesses(tmp_path):
    import subprocess
    import sys

    program = (
        "from pathlib import Path;"
        "from kiro_crew.personal_insights.insights_source import WorkspaceIdentityMap;"
        "import sys;"
        "print(WorkspaceIdentityMap(Path(sys.argv[1])).workspace_id_for(sys.argv[2]))"
    )
    project = str(tmp_path / "project")
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path), project],
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout.strip())
    assert outputs[0] == outputs[1]
    assert uuid.UUID(outputs[0]).version == 4


def test_catalog_preflight_never_reads_content():
    class MetadataRow(dict):
        def get(self, key, default=None):
            if key in {"content", "messages", "events"}:
                raise AssertionError("catalog preflight read content")
            return super().get(key, default)

        def __getitem__(self, key):
            if key in {"content", "messages", "events"}:
                raise AssertionError("catalog preflight read content")
            return super().__getitem__(key)

    selection = select_catalog_entries([MetadataRow(_row())], _pin(), set())
    assert selection.count == 1


def test_strict_snapshot_rederives_authority_on_every_page():
    from kiro_crew.personal_insights.insights_snapshot import (
        ExportAuthority,
        ExportAuthorityError,
        read_snapshot,
    )

    pages = iter(
        [
            ExportPage(
                "snap",
                2,
                "complete",
                ({"source_event_id": "one", "sequence": 1},),
                "cursor",
                False,
                "owner",
                "workspace",
            ),
            ExportPage(
                "snap",
                2,
                "complete",
                ({"source_event_id": "two", "sequence": 2},),
                None,
                True,
                "changed-owner",
                "workspace",
            ),
        ]
    )
    with pytest.raises(ExportAuthorityError):
        read_snapshot(
            "session",
            ExportAuthority("owner", "workspace"),
            lambda *_: next(pages),
            SnapshotManifest(2, 4096),
        )


def test_strict_snapshot_retries_once_with_fresh_reader():
    from kiro_crew.personal_insights.insights_snapshot import (
        ExportAuthority,
        capture_snapshot,
    )

    attempts = {"count": 0}

    def factory():
        attempts["count"] += 1
        if attempts["count"] == 1:

            def invalidated(*_args):
                raise SnapshotInvalidatedError("session")

            return invalidated

        def complete(*_args):
            return ExportPage(
                "fresh",
                1,
                "complete",
                ({"source_event_id": "one", "sequence": 1},),
                None,
                True,
                "owner",
                "workspace",
            )

        return complete

    result = capture_snapshot(
        "session",
        ExportAuthority("owner", "workspace"),
        factory,
        SnapshotManifest(1, 4096),
    )
    assert attempts["count"] == 2
    assert result.snapshot_id == "fresh"


def test_nullable_timing_is_not_invented_and_observed_zero_is_preserved():
    absent = build_projection_input(
        "source",
        [_rec("call", "tool", "tool_call", 1, tool=_tool_payload())],
        mapping_floor=0.5,
    )
    absent_tool = absent.projection_input.events[0].tool
    assert absent_tool is not None
    assert absent_tool["duration_ms"] is None
    payload = _tool_payload()
    payload["duration_ms"] = 0
    payload["output_size_bytes"] = 0
    observed = build_projection_input(
        "source",
        [_rec("call", "tool", "tool_call", 1, tool=payload)],
        mapping_floor=0.5,
    )
    observed_tool = observed.projection_input.events[0].tool
    assert observed_tool is not None
    assert observed_tool["duration_ms"] == 0
    assert observed_tool["output_size_bytes"] == 0


def test_behavior_evidence_requires_deterministic_signal():
    from kiro_crew.personal_insights.insights_adapter import behavior_signal_present

    assert behavior_signal_present({"event-a"}, {"event-a"}) is True
    assert behavior_signal_present({"event-a"}, {"event-b"}) is False


def test_catalog_cursor_rejects_result_set_drift():
    from kiro_crew.personal_insights.insights_source import CatalogCursorError

    rows = [
        _row(session_key="one", updated_at="2026-10-08T00:00:00Z"),
        _row(session_key="two", updated_at="2026-10-07T00:00:00Z"),
    ]
    first = catalog_page(rows, _pin(), set(), None, 1)
    changed = rows + [_row(session_key="three", updated_at="2026-10-06T00:00:00Z")]
    with pytest.raises(CatalogCursorError):
        catalog_page(changed, _pin(), set(), first.next_cursor, 1)


def test_admission_excludes_attachment_imported_and_child_prose():
    raws = [
        RawEvent(
            f"raw-{body_class}",
            index,
            "direct_assistant",
            "user_visible",
            "message",
            "available",
            "d" * 64,
            "excluded body",
            {"body_class": body_class},
        )
        for index, body_class in enumerate(("attachment", "imported", "child_prose"), start=1)
    ]
    assert admit_raw_events(raws).sidecar_text == {}


def test_hidden_message_is_omitted_from_neutral_events():
    result = build_projection_input(
        "source",
        [
            SourceRecord(
                source_event_id="hidden",
                actor_class="owner_user",
                event_class="message",
                visibility_class="hidden",
                sequence=1,
            )
        ],
        mapping_floor=0.5,
    )
    assert result.projection_input.events == ()
    assert result.omitted == (("hidden", "message_visibility_incompatible"),)


def test_certified_spawn_tool_pair_derives_no_tool_body():
    tool = {
        "tool_class": "task_management",
        "operation_class": "dispatch",
        "status": "success",
        "error_class": None,
        "duration_ms": None,
        "output_size_bytes": None,
        "operation_shape_id": "spawn-shape",
        "target_equivalence_id": None,
    }
    result = build_projection_input(
        "source",
        [
            SourceRecord(
                "dispatch",
                "tool",
                "tool_call",
                "metadata_only",
                1,
                tool=tool,
                extra={"certified_spawn": True},
            ),
            SourceRecord(
                "completion",
                "tool",
                "tool_outcome",
                "metadata_only",
                2,
                tool=tool,
                tool_call_ref="dispatch",
                extra={"certified_spawn": True},
            ),
        ],
        mapping_floor=0.5,
    )
    assert [event.event_class for event in result.projection_input.events] == [
        "spawn",
        "lifecycle",
    ]
    assert all(event.tool is None for event in result.projection_input.events)
    assert result.projection_input.events[1].spawn_of_raw_id == "dispatch"
