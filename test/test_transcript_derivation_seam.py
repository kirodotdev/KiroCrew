"""The transcript DERIVATION seam, and the fence that keeps every consumer on it.

A restricted (incognito / temporary) transcript is kept on disk for the user to
reopen, and nothing may be DERIVED from it: no summary, no export or transfer
bundle, no suggestions prompt, no MCP history read, no consolidation, no skill.
Every one of those readers takes its rows from disk, and the file's own
``memory_mode`` line is the privacy contract -- a ratchet any writer may tighten
between a reader's check and its read (a same-key hand-over landing a closed
restricted tab's rows; a second gateway on the same data home). Guarding each
consumer separately produced a new finding per consumer. The fix is one seam:
``ConversationLog.derive_messages`` / ``derive_messages_chained`` /
``derive_recent`` (and ``snapshot_for_consolidation(withhold_restricted=True)``)
validate the line and read the rows under ONE ``_locked`` hold and raise
``TranscriptWithheld`` instead of yielding rows a restricted or unreadable line
governs.

The plain reads stay for transcript PLUMBING (resume, save, rewind, fork,
mirror, the History browser, migrations, injections), which must see a
restricted transcript. The fence below enumerates every plain-read reference in
the source tree: a new consumer written against a plain read fails this test and
must either move to the seam or name itself here as plumbing, with the reviewer
seeing that choice in the diff.
"""

from __future__ import annotations

import ast
import pathlib
from collections import Counter

import pytest

from kiro_crew import history as history_mod
from kiro_crew.history import (
    ConversationLog,
    HistoryLockTimeout,
    TranscriptBusy,
    TranscriptWithheld,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

# Every attribute name that reads transcript ROWS from disk without the seam.
PLAIN_READS = frozenset(
    {
        "read_messages",
        "read_messages_chained",
        "read_messages_chained_full",
        "recent",
        "recent_chained",
        "_read_messages",
        "_read_messages_locked",
        "_recent_via_tail",
        "sliding_window",
        "read_file_change_messages",
        "get_unconsolidated",
    }
)

# (module path under src/, plain read) pairs that are TRANSCRIPT PLUMBING --
# they resume, save, rewind, fork, mirror, render, migrate or inject the
# transcript itself and must see a restricted one. Anything that hands rows to a
# model, a peer, a downloadable file or a memory store does NOT belong here: it
# goes through the derivation seam. Keep this sorted; a stale entry fails too.
PLUMBING: frozenset[tuple[str, str]] = frozenset(
    {
        # An app reading its own slot's conversation for the user.
        ("kiro_crew/apps/builtins/spec_builder/backend/runtime.py", "read_messages"),
        # Channel file migration: moves rows between stems, derives nothing.
        ("kiro_crew/channel_transcript_migration.py", "read_messages"),
        # SEL audit log `recent`, not a transcript.
        ("kiro_crew/cli_commands.py", "recent"),
        # The live session's OWN rows into its OWN prompt / compaction.
        ("kiro_crew/context.py", "read_messages"),
        ("kiro_crew/context.py", "read_messages_chained"),
        ("kiro_crew/context.py", "recent"),
        ("kiro_crew/dashboard/channel_slots.py", "read_messages"),
        ("kiro_crew/dashboard/chat_backfill.py", "read_messages_chained"),
        ("kiro_crew/dashboard/chat_backfill.py", "recent"),
        ("kiro_crew/dashboard/chat_fork.py", "read_messages_chained"),
        ("kiro_crew/dashboard/chat_fork.py", "read_messages_chained_full"),
        ("kiro_crew/dashboard/chat_handlers.py", "read_messages_chained"),
        ("kiro_crew/dashboard/chat_handlers.py", "read_messages_chained_full"),
        # ``selection.recent`` -- a mirror selection field, not a transcript read.
        ("kiro_crew/dashboard/chat_mirror.py", "recent"),
        ("kiro_crew/dashboard/chat_persistence.py", "read_messages_chained"),
        ("kiro_crew/dashboard/chat_rewind.py", "read_messages_chained"),
        ("kiro_crew/dashboard/chat_runner.py", "recent"),
        ("kiro_crew/dashboard/chat_slack.py", "recent"),
        ("kiro_crew/dashboard/chat_threads.py", "read_messages_chained"),
        ("kiro_crew/dashboard/cron_inject.py", "read_messages"),
        # Rendering the transcript's own artifacts / rows to its user.
        ("kiro_crew/dashboard/handlers/artifacts.py", "read_messages"),
        # SEL audit log `recent`, not a transcript.
        ("kiro_crew/dashboard/handlers/core.py", "recent"),
        ("kiro_crew/dashboard/handlers/cron.py", "read_messages"),
        ("kiro_crew/dashboard/handlers/session_control.py", "read_messages"),
        # The History browser showing the user their own transcript. Its
        # list_sessions(summarize=true) leg goes through the seam (derive_recent).
        ("kiro_crew/dashboard/handlers/sessions.py", "read_messages"),
        ("kiro_crew/decisions/points/compaction_keep.py", "read_messages"),
        # Thread diagnostics probe `recent`, not a transcript.
        ("kiro_crew/diag/threads.py", "recent"),
        ("kiro_crew/discord/session_resume.py", "recent"),
        # SEL `recent`, not a transcript.
        ("kiro_crew/feature_videos.py", "recent"),
        # The log and its read projection ARE the plain reads.
        ("kiro_crew/history.py", "_read_messages"),
        ("kiro_crew/history.py", "_read_messages_locked"),
        ("kiro_crew/history.py", "_recent_via_tail"),
        ("kiro_crew/history.py", "read_file_change_messages"),
        ("kiro_crew/history.py", "read_messages"),
        ("kiro_crew/history.py", "read_messages_chained"),
        ("kiro_crew/history.py", "read_messages_chained_full"),
        ("kiro_crew/history.py", "recent"),
        ("kiro_crew/history.py", "recent_chained"),
        ("kiro_crew/history.py", "sliding_window"),
        # Scheduling counts only (`len(...)`); the rows it consolidates come from
        # the gated snapshot and skill detection goes through derive_messages.
        ("kiro_crew/history_consolidation.py", "_read_messages"),
        ("kiro_crew/history_projection.py", "_read_messages"),
        ("kiro_crew/history_projection.py", "_read_messages_locked"),
        ("kiro_crew/history_projection.py", "_recent_via_tail"),
        ("kiro_crew/history_projection.py", "read_messages_chained"),
        ("kiro_crew/slack/gateway.py", "read_messages"),
        ("kiro_crew/teams/session_resume.py", "recent"),
    }
)


def _plain_read_sites() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in PLAIN_READS:
                found.add((rel, node.attr))
    return found


class TestTheFence:
    def test_every_plain_read_in_the_tree_is_named_as_plumbing(self):
        """A consumer written against a plain read must declare itself.

        If this fails on a site you just added: hands the rows to a model, a
        peer, a downloadable file or a memory store? Then use
        ``derive_messages`` / ``derive_messages_chained`` / ``derive_recent``
        (or ``snapshot_for_consolidation(withhold_restricted=True)``) and catch
        ``TranscriptWithheld``. Resuming, saving, rendering or migrating the
        transcript itself? Add the ``(module, read)`` pair to ``PLUMBING`` with a
        one-line reason.
        """
        found = _plain_read_sites()
        unnamed = sorted(found - PLUMBING)
        assert not unnamed, f"plain transcript reads not named as plumbing: {unnamed}"

    def test_the_plumbing_list_carries_no_stale_entry(self):
        stale = sorted(PLUMBING - _plain_read_sites())
        assert not stale, f"PLUMBING names reads that no longer exist: {stale}"

    def test_the_known_derivers_do_not_use_plain_reads(self):
        """The consumers this seam exists for are not merely fenced -- they are off it."""
        derivers = {
            "kiro_crew/dashboard/chat_summary.py",
            "kiro_crew/dashboard/session_transfer.py",
            "kiro_crew/dashboard/session_export.py",
            "kiro_crew/suggestions.py",
            "kiro_crew/mcp_tools/sessions.py",
        }
        offenders = sorted(site for site in _plain_read_sites() if site[0] in derivers)
        assert not offenders, offenders


# Transcript-derived writes owned by this seam. The consolidation entries are
# intentionally scoped to that module: the same store methods also serve direct
# owner writes and imports whose source is not a transcript result.
_PUBLISH_NAMES = frozenset(
    {
        "set_cached_intent_summary",
        "set_cached_summary",
        "stage_skill_candidate",
        "create_auto_skill",
        "update_auto_skill",
        "apply_consolidation",
        "append_history",
        "set_semantic",
        "propose_semantic_delete",
        "delete_semantic",
        "write_lesson",
        "write_episodic",
        "write_preferences",
        "write_projects",
        "build_transfer_bundle_async",
    }
)
_PUBLISH_MODULES = frozenset(
    {
        "kiro_crew/dashboard/chat_summary.py",
        "kiro_crew/dashboard/handlers/sessions.py",
        "kiro_crew/dashboard/session_export.py",
        "kiro_crew/dashboard/handlers_instances.py",
        "kiro_crew/history_consolidation.py",
    }
)
_EXPECTED_PUBLISHERS = Counter(
    {
        (
            "kiro_crew/dashboard/chat_summary.py",
            "_publish_if_derivation_is_allowed",
            "set_cached_intent_summary",
        ): 1,
        (
            "kiro_crew/dashboard/handlers/sessions.py",
            "_publish_if_derivation_is_allowed",
            "set_cached_summary",
        ): 1,
        ("kiro_crew/history_consolidation.py", "_write_preferences", "write_preferences"): 1,
        ("kiro_crew/history_consolidation.py", "_write_projects", "write_projects"): 1,
        (
            "kiro_crew/history_consolidation.py",
            "_apply_member_consolidation",
            "apply_consolidation",
        ): 1,
        (
            "kiro_crew/history_consolidation.py",
            "_append_history_under_hold",
            "append_history",
        ): 1,
        (
            "kiro_crew/history_consolidation.py",
            "_write_structured_memory",
            "propose_semantic_delete",
        ): 1,
        (
            "kiro_crew/history_consolidation.py",
            "_write_structured_memory",
            "delete_semantic",
        ): 1,
        ("kiro_crew/history_consolidation.py", "_write_structured_memory", "set_semantic"): 1,
        ("kiro_crew/history_consolidation.py", "_save_lessons", "write_lesson"): 1,
        ("kiro_crew/history_consolidation.py", "_write_structured_memory", "write_episodic"): 1,
        ("kiro_crew/history_consolidation.py", "_stage_skill_update", "stage_skill_candidate"): 1,
        ("kiro_crew/history_consolidation.py", "_process_auto_skills", "stage_skill_candidate"): 1,
        ("kiro_crew/history_consolidation.py", "_process_auto_skills", "create_auto_skill"): 1,
        ("kiro_crew/history_consolidation.py", "_process_auto_skills", "update_auto_skill"): 1,
        (
            "kiro_crew/dashboard/session_export.py",
            "api_chat_slot_export",
            "build_transfer_bundle_async",
        ): 1,
        (
            "kiro_crew/dashboard/handlers_instances.py",
            "api_instances_send_session",
            "build_transfer_bundle_async",
        ): 1,
    }
)
# These builders must run outside the hold because each awaits file/model-state
# assembly. Their consumer revalidates immediately before its synchronous
# response commit or awaited tunnel send; the ordering is pinned below.
_PUBLICATION_ALLOWLIST = {
    (
        "kiro_crew/dashboard/session_export.py",
        "api_chat_slot_export",
        "build_transfer_bundle_async",
    ): "bundle assembly awaits; the response commit revalidates afterwards",
    (
        "kiro_crew/dashboard/handlers_instances.py",
        "api_instances_send_session",
        "build_transfer_bundle_async",
    ): "bundle assembly awaits; the tunnel send revalidates afterwards",
}


def _guard_name(expr: ast.expr) -> str:
    call = expr if isinstance(expr, ast.Call) else None
    target = call.func if call is not None else expr
    return target.attr if isinstance(target, ast.Attribute) else ""


def _publication_sites() -> list[tuple[str, str, str, int, bool]]:
    found: list[tuple[str, str, str, int, bool]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self, rel: str) -> None:
            self.rel = rel
            self.functions: list[str] = []
            self.guarded = 0

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_FunctionDef = _visit_function  # noqa: N815 - ast.NodeVisitor protocol
        visit_AsyncFunctionDef = _visit_function  # noqa: N815 - ast.NodeVisitor protocol

        def visit_With(self, node: ast.With) -> None:  # noqa: N802 - ast protocol
            guarded = any(
                _guard_name(item.context_expr)
                in {
                    "publication_hold",
                    "_publication_hold_checked",
                    "_skill_publication_guard",
                }
                for item in node.items
            )
            self.guarded += int(guarded)
            self.generic_visit(node)
            self.guarded -= int(guarded)

        visit_AsyncWith = visit_With  # noqa: N815 - ast.NodeVisitor protocol

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast protocol
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                name = ""
            if name in _PUBLISH_NAMES and self.functions:
                found.append((self.rel, self.functions[-1], name, node.lineno, self.guarded > 0))
            self.generic_visit(node)

    for rel in sorted(_PUBLISH_MODULES):
        path = SRC / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _Visitor(rel).visit(tree)
    return found


def _calls_in_function(rel: str, function: str) -> dict[str, list[int]]:
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"), filename=rel)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function
    )
    calls: dict[str, list[int]] = {}
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        if isinstance(item.func, ast.Attribute):
            name = item.func.attr
        elif isinstance(item.func, ast.Name):
            name = item.func.id
        else:
            continue
        calls.setdefault(name, []).append(item.lineno)
    return calls


class TestThePublicationFence:
    def test_every_transcript_derived_publish_site_is_enumerated_and_guarded(self):
        sites = _publication_sites()
        counts = Counter((rel, function, name) for rel, function, name, _line, _guarded in sites)
        assert counts == _EXPECTED_PUBLISHERS
        unguarded = [
            (rel, function, name, line)
            for rel, function, name, line, guarded in sites
            if not guarded and (rel, function, name) not in _PUBLICATION_ALLOWLIST
        ]
        assert (
            not unguarded
        ), f"transcript-derived publications bypass publication_hold: {unguarded}"

    def test_the_publication_allowlist_is_reasoned_and_not_stale(self):
        site_keys = {
            (rel, function, name) for rel, function, name, _line, _guarded in _publication_sites()
        }
        assert set(_PUBLICATION_ALLOWLIST) <= site_keys
        assert all(reason.strip() for reason in _PUBLICATION_ALLOWLIST.values())

    def test_the_skill_guard_is_the_publication_seam(self):
        calls = _calls_in_function("kiro_crew/history_consolidation.py", "_skill_publication_guard")
        assert len(calls.get("_publication_hold_checked", [])) == 1
        assert not calls.get("publication_hold")
        assert not calls.get("derivation_hold")
        checked = _calls_in_function(
            "kiro_crew/history_consolidation.py", "_publication_hold_checked"
        )
        assert len(checked.get("publication_hold", [])) == 1
        assert len(checked.get("_persistence_disabled", [])) == 1

    def test_no_publication_hold_spans_an_await_or_model_call(self):
        forbidden = {
            "_call_llm",
            "run_bg_oneliner",
            "embed_lesson",
            "embed_episodic",
            "embed_semantic",
            "embed_semantic_retirement",
            "_try_embed",
        }
        offenders: list[tuple[str, int, str]] = []
        for rel in sorted(_PUBLISH_MODULES | {"kiro_crew/history.py"}):
            tree = ast.parse((SRC / rel).read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.With, ast.AsyncWith)):
                    continue
                if not any(
                    _guard_name(item.context_expr)
                    in {"publication_hold", "_publication_hold_checked"}
                    for item in node.items
                ):
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.Await):
                        offenders.append((rel, node.lineno, "await"))
                    elif isinstance(child, ast.Call):
                        name = (
                            child.func.attr
                            if isinstance(child.func, ast.Attribute)
                            else child.func.id if isinstance(child.func, ast.Name) else ""
                        )
                        if name in forbidden:
                            offenders.append((rel, node.lineno, name))
        assert not offenders, f"publication_hold spans slow work: {offenders}"

    @pytest.mark.parametrize(
        ("rel", "function", "commit"),
        [
            (
                "kiro_crew/dashboard/session_export.py",
                "api_chat_slot_export",
                "Response",
            ),
            (
                "kiro_crew/dashboard/handlers_instances.py",
                "api_instances_send_session",
                "send_session_bundle",
            ),
        ],
    )
    def test_egress_revalidates_after_assembly_immediately_before_commit(
        self, rel, function, commit
    ):
        calls = _calls_in_function(rel, function)
        build = calls["build_transfer_bundle_async"]
        hold = calls["publication_hold"]
        committed = calls[commit]
        assert len(build) == len(hold) == 1
        assert committed
        assert build[0] < hold[0] < max(committed)

    @pytest.mark.parametrize(
        ("function", "embed_call", "write_call"),
        [
            ("_save_lessons", "embed_lesson", "write_lesson"),
            ("_write_structured_memory", "embed_episodic", "write_episodic"),
            ("_write_structured_memory", "embed_semantic", "set_semantic"),
            (
                "_write_structured_memory",
                "embed_semantic_retirement",
                "set_semantic",
            ),
        ],
    )
    def test_consolidation_embedding_precedes_the_publication_hold(
        self, function, embed_call, write_call
    ):
        calls = _calls_in_function("kiro_crew/history_consolidation.py", function)
        assert len(calls[embed_call]) == len(calls[write_call]) == 1
        assert calls["_publication_hold_checked"]
        holds_before_write = [
            line for line in calls["_publication_hold_checked"] if line < calls[write_call][0]
        ]
        assert holds_before_write
        assert calls[embed_call][0] < max(holds_before_write) < calls[write_call][0]

    @pytest.mark.parametrize(
        ("rel", "function", "write_call"),
        [
            (
                "kiro_crew/dashboard/chat_handlers.py",
                "_persist_handover_tail",
                "save_slot_off_loop",
            ),
            (
                "kiro_crew/dashboard/chat_runner.py",
                "_persist_tool_result_rows",
                "save_slot_off_loop",
            ),
            (
                "kiro_crew/dashboard/chat_title.py",
                "_persist_title",
                "to_thread",
            ),
        ],
    )
    def test_every_tightening_cas_refusal_continues_to_its_durable_write(
        self, rel, function, write_call
    ):
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"), filename=rel)
        node = next(
            item
            for item in ast.walk(tree)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function
        )
        handlers = {
            handler.type.id
            for handler in ast.walk(node)
            if isinstance(handler, ast.ExceptHandler) and isinstance(handler.type, ast.Name)
        }
        calls = _calls_in_function(rel, function)
        tightening = calls.get("tighten_replacement_to_restricted_original", []) + calls.get(
            "_tighten_replacement_to_restricted_original", []
        )
        assert "UnknownMemoryStore" in handlers
        assert len(tightening) == 1
        assert tightening[0] < min(calls[write_call])


KEY = "dashboard:seam"


def _log(tmp_path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        log.append(KEY, "user", "PRIVATE-1")
        log.append(KEY, "assistant", "PRIVATE-2")
    return log


class TestTheSeam:
    def test_a_persistent_line_yields_the_same_rows_as_the_plain_read(self, tmp_path):
        log = _log(tmp_path)
        assert log.derive_messages(KEY) == log.read_messages(KEY)
        plain_chained = log.read_messages_chained(KEY)
        assert log.derive_messages_chained(KEY) is plain_chained
        assert log.derive_recent(KEY, 1) == log.recent(KEY, 1)
        assert [m["content"] for m in log.derive_recent(KEY, 5, roles={"user"})] == ["PRIVATE-1"]

    def test_an_absent_file_is_not_a_refusal(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path / "sessions")
        log.init()
        assert log.derive_messages("dashboard:nothing-here") == []
        assert log.derive_recent("dashboard:nothing-here") == []

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "Incognito"])
    def test_a_restricted_line_withholds_every_shape(self, tmp_path, mode):
        log = _log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"memory_mode": mode})
        for read in (
            lambda: log.derive_messages(KEY),
            lambda: log.derive_messages_chained(KEY),
            lambda: log.derive_recent(KEY, 5),
            lambda: log.snapshot_for_consolidation(KEY, withhold_restricted=True),
        ):
            with pytest.raises(TranscriptWithheld):
                read()
        # The plain reads still serve the transcript's own plumbing.
        assert [m["content"] for m in log.read_messages(KEY)] == ["PRIVATE-1", "PRIVATE-2"]

    def test_publication_hold_allows_one_persistent_write(self, tmp_path):
        log = _log(tmp_path)
        published: list[str] = []
        with log.publication_hold(KEY):
            published.append("written")
        assert published == ["written"]

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_publication_hold_refuses_a_restricted_line(self, tmp_path, mode):
        log = _log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"memory_mode": mode})
        with pytest.raises(TranscriptWithheld):
            with log.publication_hold(KEY):
                raise AssertionError("the publication body ran")

    def test_an_unreadable_line_withholds(self, tmp_path, monkeypatch):
        log = _log(tmp_path)
        monkeypatch.setattr(type(log), "_read_metadata_status", lambda self, key: ({}, False))
        with pytest.raises(TranscriptWithheld):
            log.derive_messages(KEY)

    def test_the_line_is_validated_under_the_same_lock_as_the_rows(self, tmp_path, monkeypatch):
        """A tightening that lands while the seam holds the lock cannot slip a row out.

        The writer that tightens a line (the hand-over save) takes ``_locked`` too,
        so under the seam's hold it waits; a tightening that lands first is seen by
        the seam's own check. Modelled here by tightening from INSIDE the lock,
        after the check: the rows the seam returns are still the ones the check
        vouched for, because both happened in one hold -- and the next derivation
        is refused.
        """
        log = _log(tmp_path)
        real_read = type(log).read_messages
        state = {"tightened": False}

        def _read_then_tighten(self, key):
            rows = real_read(self, key)
            if not state["tightened"]:
                state["tightened"] = True
                with history_mod.allow_on_loop_persist():
                    self.update_metadata(key, {"memory_mode": "incognito"})
            return rows

        monkeypatch.setattr(type(log), "read_messages", _read_then_tighten)
        first = log.derive_messages(KEY)
        assert [m["content"] for m in first] == ["PRIVATE-1", "PRIVATE-2"]
        with pytest.raises(TranscriptWithheld):
            log.derive_messages(KEY)


class TestTheSeamCoversTheWholeChain:
    """A chained read concatenates every transcript sharing the tab id.

    The contract that governs the result is the strictest line among ALL of them:
    a legacy tab whose earlier file was tightened to ``temporary`` must not ride
    its rows out under a persistent sibling's line. Every chained transcript is
    locked and validated before a row is read.
    """

    @staticmethod
    def _chain(tmp_path) -> ConversationLog:
        log = ConversationLog(base_dir=tmp_path / "sessions")
        log.init()
        with history_mod.allow_on_loop_persist():
            log.append("dashboard:chat-0", "user", "OLDER-PRIVATE", tab_id="tab-legacy")
            log.append("dashboard:chat-1", "user", "NEWER-PUBLIC", tab_id="tab-legacy")
        assert log.chained_keys("dashboard:chat-1") == ["dashboard:chat-0", "dashboard:chat-1"]
        return log

    def test_a_persistent_chain_reads_every_file(self, tmp_path):
        log = self._chain(tmp_path)
        rows = log.derive_messages_chained("dashboard:chat-1")
        assert [m["content"] for m in rows] == ["OLDER-PRIVATE", "NEWER-PUBLIC"]
        assert rows == log.read_messages_chained("dashboard:chat-1")

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "Temporary"])
    def test_a_restricted_sibling_withholds_the_whole_chain(self, tmp_path, mode):
        log = self._chain(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata("dashboard:chat-0", {"memory_mode": mode})
        # The requested key's own line is still persistent...
        assert "memory_mode" not in log.get_metadata("dashboard:chat-1")
        # ...and the seam still refuses, because the sibling's line governs its rows.
        with pytest.raises(TranscriptWithheld):
            log.derive_messages_chained("dashboard:chat-1")
        with pytest.raises(TranscriptWithheld):
            with log.publication_hold("dashboard:chat-1"):
                raise AssertionError("the publication body ran")
        # Plumbing still sees the chain.
        assert len(log.read_messages_chained("dashboard:chat-1")) == 2

    def test_an_unreadable_sibling_line_withholds_the_whole_chain(self, tmp_path, monkeypatch):
        log = self._chain(tmp_path)
        real_status = type(log)._read_metadata_status

        def _status(self, key):
            if key == "dashboard:chat-0":
                return {}, False
            return real_status(self, key)

        monkeypatch.setattr(type(log), "_read_metadata_status", _status)
        with pytest.raises(TranscriptWithheld):
            log.derive_messages_chained("dashboard:chat-1")

    def test_a_chain_that_grows_while_being_locked_is_refused_as_busy(self, tmp_path, monkeypatch):
        """A file joining the chain between the resolve and the hold is unlocked and
        unvalidated, so the read is refused rather than served -- as TranscriptBusy,
        the same answer ``publication_hold`` gives a changed chain, so an export of a
        persistent chat maps it to its retryable 503 and not to a privacy 400."""
        log = self._chain(tmp_path)
        real_chained = type(log).chained_keys
        calls = {"n": 0}

        def _grow_on_second_resolve(self, key):
            keys = real_chained(self, key)
            calls["n"] += 1
            if calls["n"] == 2:
                return keys + ["dashboard:chat-late"]
            return keys

        monkeypatch.setattr(type(log), "chained_keys", _grow_on_second_resolve)
        with pytest.raises(TranscriptBusy):
            log.derive_messages_chained("dashboard:chat-1")
        calls["n"] = 0
        with pytest.raises(TranscriptBusy):
            log.derive_messages_chained_with_keys("dashboard:chat-1")

    def test_a_member_joining_after_validation_is_not_read(self, tmp_path, monkeypatch):
        """The settled, validated keys are the complete read set.

        A third chain resolution can observe a member whose lock was never held and
        whose line was never validated. The derivation read must not resolve again.
        """
        log = self._chain(tmp_path)
        late = "dashboard:chat-late"
        with history_mod.allow_on_loop_persist():
            log.append(late, "user", "LATE-PRIVATE", tab_id="tab-legacy")
            log.update_metadata(late, {"memory_mode": "temporary"})
        projection = log._read_projection
        settled = ["dashboard:chat-0", "dashboard:chat-1"]
        calls = {"n": 0}

        def _join_on_third_resolve(self, key):
            calls["n"] += 1
            assert key == "dashboard:chat-1"
            return settled + ([late] if calls["n"] >= 3 else [])

        monkeypatch.setattr(type(projection), "chained_keys", _join_on_third_resolve)
        rows = log.derive_messages_chained("dashboard:chat-1")

        assert calls["n"] == 2, "the derivation read resolved its validated chain again"
        assert [row["content"] for row in rows] == ["OLDER-PRIVATE", "NEWER-PUBLIC"]


class TestABusyTranscriptIsARefusalNotAnError:
    """A lock the seam cannot take in time answers TranscriptBusy, a TranscriptWithheld.

    The seam's hold is a patient, cross-process acquire with a ceiling. A reader
    that cannot obtain it cannot vouch for the contract, so it gets no rows -- and
    because the refusal is a TranscriptWithheld every caller's best-effort skip
    already handles it, while a person-facing caller can still tell busy apart.
    """

    @staticmethod
    def _timeout_on_lock(log, monkeypatch):
        import contextlib

        @contextlib.contextmanager
        def _raise(self, stems):
            raise HistoryLockTimeout("held by another writer")
            yield  # pragma: no cover

        monkeypatch.setattr(type(log), "locked_stems", _raise)

    def test_every_seam_shape_answers_busy(self, tmp_path, monkeypatch):
        log = _log(tmp_path)
        self._timeout_on_lock(log, monkeypatch)

        def _publish() -> None:
            with log.publication_hold(KEY):
                raise AssertionError("the publication body ran")

        for read in (
            lambda: log.derive_messages(KEY),
            lambda: log.derive_messages_chained(KEY),
            lambda: log.derive_recent(KEY, 5),
            lambda: log.snapshot_for_consolidation(KEY, withhold_restricted=True),
            _publish,
        ):
            with pytest.raises(TranscriptBusy) as excinfo:
                read()
            assert isinstance(excinfo.value, TranscriptWithheld)
            assert isinstance(excinfo.value.__cause__, HistoryLockTimeout)

    def test_the_ungated_snapshot_still_raises_the_raw_timeout(self, tmp_path, monkeypatch):
        """Plumbing keeps the plain exception; only the seam translates it."""
        log = _log(tmp_path)
        self._timeout_on_lock(log, monkeypatch)
        with pytest.raises(HistoryLockTimeout):
            log.snapshot_for_consolidation(KEY)
