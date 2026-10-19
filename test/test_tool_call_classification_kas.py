"""KAS tool-call frames: the tool_call's ``kind`` carried to its permission frame (and
only on the KAS backend), the spawn-time warning on engine vocabulary drift, the
built-in id on the permission frame, and the identity of a KAS MCP-served call.
``test_tool_call_classification.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.acp._dispatch import build_permission_event, classify_tool_call, parse_session_update
from kiro_crew.acp.types import AcpEvent, JsonRpcMessage
from kiro_crew.hooks import TOOL_DENY, HookManager

# ── KAS: the built-in's id is on the permission frame, not the tool_call ──────


def _kas_builtin_flow(
    tool_id: str,
    *,
    title: str = "Read File",
    kind: str = "read",
    tool_call_meta: dict | None = None,
    raw_input: dict | None = None,
    with_kind_cache: bool = True,
    on_kas: bool = True,
) -> AcpEvent:
    """A KAS built-in as the wire carries it (captured live, kiro-cli 2.24.0):
    the tool_call frame has a display title, a kind and ``_meta.kiro.toolOrigin``
    but NO ``toolName``; the permission request stamps ``_meta.kiro.toolId`` and
    carries NO ``kind``."""
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
    }
    if on_kas:
        # The KAS harness is handed this store (and only KAS is), which is also
        # what marks the permission event's bare built-in id as a KAS id.
        caches["identity_unreadable_cache"] = {}
    if with_kind_cache:
        caches["tool_kind_cache"] = {}
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-kas",
            "title": title,
            "kind": kind,
            "status": "pending",
            "rawInput": raw_input if raw_input is not None else {"path": "/tmp/probe-note.txt"},
            "_meta": (
                tool_call_meta
                if tool_call_meta is not None
                else {"kiro": {"toolOrigin": "default"}}
            ),
        },
        **caches,
    )
    msg = JsonRpcMessage(
        id=41,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {"toolCallId": "tc-kas", "status": "pending", "title": title},
            "options": [
                {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
            ],
            "_meta": {"kiro": {"toolId": tool_id}},
        },
    )
    event, _ = build_permission_event(msg, **caches)
    return event


class TestKasPermissionFrameTakesTheToolCallKind:
    """KAS's ``request_permission`` names no ``kind``; the tool_call did. Without
    the carry-over every KAS call reached the gate's kind-keyed tiers as ``""``,
    so the write-plane routing this PR adds for ``delete_file`` never fired on
    the one harness that has ``delete_file``. Only the WRITE-PLANE kinds are
    carried: the other kinds KAS stamps (recorded in
    ``test/fixtures/kas_builtin_tool_ids.json`` from the engine's kind map) are
    left ``""`` so a KAS search keeps the read-only proof it has by identity."""

    @pytest.mark.parametrize("kind", ["edit", "delete"])
    def test_a_write_plane_tool_call_kind_reaches_the_permission_event(self, kind: str) -> None:
        event = _kas_builtin_flow("delete_file", title="Delete File", kind=kind)
        assert event.tool_kind == kind

    @pytest.mark.parametrize("kind", ["read", "search", "execute", "fetch", "other"])
    def test_a_non_write_plane_kind_is_not_carried(self, kind: str) -> None:
        """Every kind KAS stamps outside the write plane stays off the permission
        event -- ``search`` would veto the host read-only proof, ``read`` would
        widen the interactive allow-list on a field the frame never stated."""
        event = _kas_builtin_flow("grep_search", title="Grep Search", kind=kind)
        assert event.tool_kind == ""

    def test_the_carried_set_is_exactly_the_write_plane_and_the_fixture_agrees(self) -> None:
        """The exclusion is measured, not assumed: the fixture records the kind
        the engine stamps on every registered built-in, the write-plane kinds
        appear there only on the write tools, and ``search`` is really what the
        engine stamps on its three search-shaped reads."""
        import json
        from pathlib import Path

        from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS

        reg = json.loads(
            (Path(__file__).parent / "fixtures" / "kas_builtin_tool_ids.json").read_text()
        )
        kinds = {k: v for k, v in reg["tool_call_kind"].items() if not k.startswith("_")}
        assert set(kinds) == set(reg["registered"])
        carried = {tool for tool, kind in kinds.items() if kind in WRITE_PLANE_KINDS}
        assert carried == {"delete_file", "fs_append", "fs_write", "str_replace"}
        assert {kinds[t] for t in ("grep_search", "file_search", "list_directory")} == {"search"}
        assert kinds["read_file"] == "read"

    def test_a_kas_search_still_auto_approves_under_the_read_only_proof(self) -> None:
        """The First-Principles condition, driven from the wire with the cache
        PRESENT: a KAS ``grep_search`` tool_call stamps ``search``; its kindless
        permission frame must still auto-approve under ``--approval reads`` by
        the identity fold (``grep_search`` -> ``grep``), which a carried
        ``search`` would veto."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, hook_gate_kwargs

        for kas_id, title in (
            ("grep_search", "Grep Search"),
            ("file_search", "File Search"),
            ("list_directory", "List Directory"),
        ):
            event = _kas_builtin_flow(
                kas_id,
                title=title,
                kind="search",
                raw_input={"pattern": "TODO", "path": "/tmp/repo"},
            )
            assert event.tool_kind == "", kas_id
            r = HookManager().on_tool_call(
                event.title, classifier_only=True, **hook_gate_kwargs(event)
            )
            assert r.action == TOOL_AUTO_APPROVE and r.read_only is True, kas_id

    def test_without_the_cache_the_kind_is_empty_as_before(self) -> None:
        """The control: this is the shape KAS produced at the gate before."""
        event = _kas_builtin_flow(
            "delete_file", title="Delete File", kind="delete", with_kind_cache=False
        )
        assert event.tool_kind == ""

    def test_a_kind_the_permission_frame_states_is_not_overridden(self) -> None:
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
            "tool_kind_cache": {},
        }
        parse_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-1",
                "title": "t",
                "kind": "edit",
                "status": "pending",
                "rawInput": {"path": "/tmp/x"},
            },
            **caches,
        )
        msg = JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {
                    "toolCallId": "tc-1",
                    "status": "pending",
                    "title": "t",
                    "kind": "read",
                },
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        event, _ = build_permission_event(msg, **caches)
        assert event.tool_kind == "read"

    @pytest.mark.parametrize("bad_kind", [[], {}, 7, None, ["edit"]])
    def test_a_non_string_frame_kind_is_read_as_absent(self, bad_kind) -> None:
        """The frame's ``kind`` is an unvalidated harness value. A non-string is
        not stamped on the event -- it would reach the write-plane membership
        test unhashable and abort the turn -- so the cached tool_call kind
        fills the blank exactly as it does for a frame that omits the field, and
        without a cache the event carries ``""``. The gate then classifies."""
        from kiro_crew.platform.tool_paths import is_edit_call

        def frame(with_cache: bool) -> AcpEvent:
            caches: dict[str, Any] = {
                "tool_input_cache": {},
                "shell_cache": {},
                "raw_params_cache": {},
                "mcp_server_name_cache": {},
                "tool_name_cache": {},
            }
            if with_cache:
                caches["tool_kind_cache"] = {}
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-bad",
                    "title": "t",
                    "kind": "delete",
                    "status": "pending",
                    "rawInput": {"path": "/tmp/x"},
                },
                **caches,
            )
            msg = JsonRpcMessage(
                id=2,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {
                        "toolCallId": "tc-bad",
                        "status": "pending",
                        "title": "t",
                        "kind": bad_kind,
                    },
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                },
            )
            event, _ = build_permission_event(msg, **caches)
            return event

        with_cache = frame(True)
        assert with_cache.tool_kind == "delete"
        without = frame(False)
        assert without.tool_kind == ""
        assert is_edit_call(without.tool_kind, "") is False

    def test_a_placeholder_kind_is_not_cached(self) -> None:
        caches: dict[str, Any] = {"tool_kind_cache": {}}
        for update in (
            {"kind": "unknown"},
            {"kind": ""},
            {},  # absent: the builder's own "unknown" default
        ):
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-p",
                    "title": "t",
                    "status": "pending",
                    **update,
                },
                **caches,
            )
        assert caches["tool_kind_cache"] == {}

    def test_the_cache_retains_only_the_closed_write_plane_vocabulary(self) -> None:
        """The frame's ``kind`` is an externally-sized string. Only membership in
        ``WRITE_PLANE_KINDS`` is retained, so a stored entry is one of those
        literals and never the frame's own bytes: a 10 MiB ``kind`` per unique
        toolCallId retains nothing, and ``read``/``search``/``execute``/
        ``fetch``/``other`` (kinds the reader never carries) are not written
        either -- growth with no reader."""
        from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS

        caches: dict[str, Any] = {"tool_kind_cache": {}}
        huge = "k" * (1024 * 1024)
        for i, kind in enumerate(
            [huge, "read", "search", "execute", "fetch", "other", "edit", "delete", "editx"]
        ):
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": f"tc-{i}",
                    "title": "t",
                    "status": "pending",
                    "kind": kind,
                },
                **caches,
            )
        stored = caches["tool_kind_cache"]
        assert set(stored.values()) <= WRITE_PLANE_KINDS
        assert sorted(stored.values()) == ["delete", "edit"]
        assert sum(len(v) for v in stored.values()) < 32


class TestTheKindCarryOverIsKasOnly:
    """kiro-cli's permission frames omit ``kind`` too, and its ``grep``/``glob``
    tool_calls carry ``kind: "search"`` -- not a read-only kind -- so carrying it
    over would let the kind veto the host read-only proof that auto-approves a
    kiro-cli search under ``--approval reads`` (harness parity H13). The cache
    therefore exists only on the KAS backend, by a positive backend test; the
    kiro-cli path is unchanged."""

    @staticmethod
    def _handle(acp_backend: str):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = MagicMock()
        rt.acp_backend = acp_backend
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        return AcpSessionHandle("sA", asyncio.Queue(), rt)

    def test_the_kas_handle_hands_the_cache_to_the_wire(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        h = self._handle(ACP_BACKEND_KAS)
        assert h._tool_kind_cache_for_wire() is h._tool_call_kind

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_every_other_handle_hands_none_to_the_wire(self, backend: str) -> None:
        """Construction is identical for every harness (the attribute exists on
        all of them); only what the wire parsers are HANDED differs, per frame."""
        h = self._handle(backend)
        assert h._tool_call_kind == {}
        assert h._tool_kind_cache_for_wire() is None

    def test_a_runtime_double_without_a_backend_is_not_a_member(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = MagicMock(spec=["pid", "is_alive", "send_notification", "supports_image_prompt"])
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        assert AcpSessionHandle("sA", asyncio.Queue(), rt)._tool_kind_cache_for_wire() is None

    def test_the_kas_handle_hands_the_overflow_record_to_the_wire(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        h = self._handle(ACP_BACKEND_KAS)
        assert h._tool_call_cache_overflow_for_wire() is h._tool_call_cache_overflow

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_every_other_handle_hands_no_overflow_record(self, backend: str) -> None:
        h = self._handle(backend)
        assert h._tool_call_cache_overflow == set()
        assert h._tool_call_cache_overflow_for_wire() is None

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_a_non_member_harness_retains_past_every_bound_and_is_never_refused(
        self, backend: str
    ) -> None:
        """The admission bound is the KAS projection's (H13): a kiro-cli edit over
        the payload bound, and a turn past the count cap, retain their provenance
        exactly as on the base, and the handle's unretained-provenance refusal
        never fires because the record is never written."""
        import asyncio

        from kiro_crew.acp._dispatch import (
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
        )

        h = self._handle(backend)

        def _frame(i: int, **extra: Any) -> JsonRpcMessage:
            upd = {
                "sessionUpdate": "tool_call",
                "toolCallId": f"tc-{i}",
                "title": "Write File",
                "kind": "edit",
                "status": "pending",
                "rawInput": {"path": f"/tmp/f{i}", "text": "x"},
                **extra,
            }
            return JsonRpcMessage(
                method="session/update", params={"sessionId": "sA", "update": upd}
            )

        from kiro_crew.acp._dispatch import scoped_tool_cache_key as _k

        big = {"path": "/tmp/big", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)}
        h._handle_update(_frame(0, rawInput=big))
        assert h._tool_call_raw_params[_k("sA", "tc-0")]["path"] == "/tmp/big"
        for i in range(1, TOOL_CALL_CACHE_MAX_ENTRIES + 2):
            h._handle_update(_frame(i))
        over = f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES + 1}"
        assert (
            h._tool_call_raw_params[_k("sA", over)]["path"]
            == f"/tmp/f{TOOL_CALL_CACHE_MAX_ENTRIES + 1}"
        )
        assert h._tool_call_cache_overflow == set()
        msg = JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "sessionId": "sA",
                "toolCall": {"toolCallId": "never-seen", "status": "pending", "title": "Write"},
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        event = h._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is False
        assert asyncio.run(h._refuse_unretained_provenance(msg, event)) is False

    def test_the_kas_handle_is_the_one_the_bound_governs(self) -> None:
        """Counterfactual for the test above: on the KAS handle the same frames
        are refused past the bound and the record is written."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS, overflow_scope_key
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        h = self._handle(ACP_BACKEND_KAS)
        big = {"path": "/tmp/big", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)}
        h._handle_update(
            JsonRpcMessage(
                method="session/update",
                params={
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-0",
                        "title": "Write File",
                        "kind": "edit",
                        "status": "pending",
                        "rawInput": big,
                    },
                },
            )
        )
        assert not any(k.endswith("tc-0") for k in h._tool_call_raw_params)
        assert h._tool_call_cache_overflow == {overflow_scope_key("sA")}

    def test_a_kiro_cli_search_stays_kindless_and_read_only_proven(self) -> None:
        """kiro-cli streams a ``search`` tool_call then a kindless permission
        frame. Two independent defenses keep the event's kind ``""`` so the
        trusted identity proves the read: the kiro path hands the parser no
        cache at all (the KAS-only membership), and even a parser handed one
        carries only write-plane kinds, never ``search``."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, hook_gate_kwargs

        def flow(with_cache: bool) -> AcpEvent:
            caches: dict[str, Any] = {
                "tool_input_cache": {},
                "shell_cache": {},
                "raw_params_cache": {},
                "mcp_server_name_cache": {},
                "tool_name_cache": {},
            }
            if with_cache:
                caches["tool_kind_cache"] = {}
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-grep",
                    "title": "grep",
                    "kind": "search",
                    "status": "pending",
                    "rawInput": {"pattern": "TODO", "path": "/tmp/repo"},
                    "_meta": {"kiro": {"toolName": "grep", "mcpServerName": ""}},
                },
                **caches,
            )
            msg = JsonRpcMessage(
                id=5,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {"toolCallId": "tc-grep", "status": "pending", "title": "grep"},
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                },
            )
            event, _ = build_permission_event(msg, **caches)
            return event

        kiro_path = flow(with_cache=False)
        assert kiro_path.tool_kind == ""
        assert kiro_path.tool_name == "grep" and kiro_path.mcp_identity_trusted is True
        r = HookManager().on_tool_call(
            kiro_path.title, classifier_only=True, **hook_gate_kwargs(kiro_path)
        )
        assert r.action == TOOL_AUTO_APPROVE and r.read_only is True

        # Second defense: a parser handed the cache still leaves ``search`` off
        # the event, so the proof holds even without the membership gate.
        with_cache = flow(with_cache=True)
        assert with_cache.tool_kind == ""
        r = HookManager().on_tool_call(
            with_cache.title, classifier_only=True, **hook_gate_kwargs(with_cache)
        )
        assert r.action == TOOL_AUTO_APPROVE and r.read_only is True

    def test_the_membership_gate_is_what_keeps_a_kiro_cli_edit_kindless(self) -> None:
        """Why the cache is still KAS-only: kiro-cli's ``fs_write`` tool_call
        stamps ``edit``, a write-plane kind the reader WOULD carry. kiro-cli
        routes its edits by the diff content block already, and the kindless
        governance classification is additive (read pairs beside the write
        pairs), so a carried ``edit`` would change what kiro-cli's gate sees
        (harness parity H13). The handle hands the kiro path no cache."""
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }

        def flow(with_cache: bool) -> AcpEvent:
            local = {k: {} for k in caches}
            if with_cache:
                local["tool_kind_cache"] = {}
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-edit",
                    "title": "fs_write",
                    "kind": "edit",
                    "status": "pending",
                    "rawInput": {"command": "create", "path": "/tmp/x.md", "fileText": "x"},
                    "_meta": {"kiro": {"toolName": "fs_write", "mcpServerName": ""}},
                },
                **local,
            )
            msg = JsonRpcMessage(
                id=6,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {"toolCallId": "tc-edit", "status": "pending", "title": "fs_write"},
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                },
            )
            event, _ = build_permission_event(msg, **local)
            return event

        assert flow(with_cache=False).tool_kind == ""
        assert flow(with_cache=True).tool_kind == "edit"


class TestKasSpawnWarnsOnVocabularyDrift:
    """The policy fold fails permissive on engine drift, so the KAS harness says
    once per kiro-cli release when it spawns the relay -- where an operator sees
    it without running ``kirocrew doctor`` -- that the installed kiro-cli is not
    the one the tables were measured on. Same comparison and same reading (the
    pinned binary's ``--version``) as the doctor row. It lives on the harness,
    not on the shared runtime (H13)."""

    def test_a_different_engine_release_is_logged_once_per_process(
        self, caplog, monkeypatch
    ) -> None:
        import logging

        from kiro_crew.acp.harness import kas as kas_mod
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        newer = (*KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[-1][:2], 99)
        monkeypatch.setattr(kas_mod, "_DRIFT_WARNED_VERSION", None)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.harness.kas"):
            kas_mod.warn_kas_vocabulary_drift(newer)
            kas_mod.warn_kas_vocabulary_drift(newer)
        hits = [r for r in caplog.records if "tool-name tables" in r.getMessage()]
        assert len(hits) == 1
        shown = ".".join(str(p) for p in newer)
        assert shown in hits[0].getMessage() and "newer" in hits[0].getMessage()

    def test_the_warned_release_is_one_slot_and_a_changed_release_warns_again(
        self, caplog, monkeypatch
    ) -> None:
        """The process keeps the one release it last warned for, nothing more: a
        repeat is silent, a different release (kiro-cli upgraded under a running
        gateway) warns, and what is retained is a fixed-shape tuple."""
        import logging

        from kiro_crew.acp.harness import kas as kas_mod

        monkeypatch.setattr(kas_mod, "_DRIFT_WARNED_VERSION", None)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.harness.kas"):
            kas_mod.warn_kas_vocabulary_drift((9, 1, 0))
            kas_mod.warn_kas_vocabulary_drift((9, 1, 0))
            assert kas_mod._DRIFT_WARNED_VERSION == (9, 1, 0)
            kas_mod.warn_kas_vocabulary_drift((9, 2, 0))
            assert kas_mod._DRIFT_WARNED_VERSION == (9, 2, 0)
            kas_mod.warn_kas_vocabulary_drift((9, 2, 0))
        drift = [r.getMessage() for r in caplog.records if "tool-name tables" in r.getMessage()]
        assert len(drift) == 2
        assert "9.1.0" in drift[0] and "9.2.0" in drift[1]

    def test_the_measured_release_and_an_unknown_version_are_silent(
        self, caplog, monkeypatch
    ) -> None:
        import logging

        from kiro_crew.acp.harness import kas as kas_mod
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        monkeypatch.setattr(kas_mod, "_DRIFT_WARNED_VERSION", None)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.harness.kas"):
            kas_mod.warn_kas_vocabulary_drift(KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[0])
            kas_mod.warn_kas_vocabulary_drift(None)
        assert not [r for r in caplog.records if "tool-name tables" in r.getMessage()]
        assert kas_mod._DRIFT_WARNED_VERSION is None

    def test_the_warning_lives_on_the_kas_harness_and_nowhere_shared(self) -> None:
        """Pin by source (H13): the KAS spawn feeds the warning the pinned
        binary's version off the loop, and the shared runtime carries no
        vocabulary comparison and no KAS-membership conditional for it."""
        import inspect

        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.harness.kas import KasHarness

        spawn = inspect.getsource(KasHarness.resolve_spawn)
        assert (
            "warn_kas_vocabulary_drift(await asyncio.to_thread(installed_kiro_cli_version))"
            in spawn
        )
        import sys

        # runtime.py's owner modules (runtime_*.py) hold code that was its own.
        runtime_src = "".join(
            inspect.getsource(sys.modules[name])
            for name in (runtime_mod.__name__, *runtime_mod._RUNTIME_OWNERS)
        )
        assert "vocabulary_drift" not in runtime_src
        assert "installed_kiro_cli_version" not in runtime_src

    @pytest.mark.asyncio
    async def test_the_kas_spawn_probes_the_pinned_binary_once_per_call(
        self, monkeypatch, tmp_path
    ) -> None:
        """The spawn hands the probe's answer to the warning; a probe that cannot
        answer hands ``None`` and the spawn still resolves."""
        from kiro_crew.acp.harness import kas as kas_mod
        from kiro_crew.acp.harness.base import SpawnContext

        seen: list[object] = []
        monkeypatch.setattr(kas_mod, "installed_kiro_cli_version", lambda: (2, 30, 1))
        monkeypatch.setattr(kas_mod, "warn_kas_vocabulary_drift", seen.append)

        async def _bin(**_kw):
            return str(tmp_path / "kiro-cli")

        async def _no_vault():
            return False

        from kiro_crew.acp import client as client_mod

        monkeypatch.setattr(client_mod, "_resolve_kiro_bin_for_spawn", _bin)
        monkeypatch.setattr(kas_mod, "vault_holds_identity_off_loop", _no_vault)
        ctx = SpawnContext(agent="a", work_dir=str(tmp_path), model=None, environ={}, home=tmp_path)
        plan = await kas_mod.KasHarness().resolve_spawn(ctx)
        assert plan.argv and seen == [(2, 30, 1)]
        monkeypatch.setattr(kas_mod, "installed_kiro_cli_version", lambda: None)
        await kas_mod.KasHarness().resolve_spawn(ctx)
        assert seen == [(2, 30, 1), None]


class TestKasPermissionFrameCarriesTheBuiltinId:
    def test_tool_id_on_the_permission_frame_becomes_the_tool_name(self) -> None:
        """Without this the only identity the gate sees for a KAS built-in is the
        prose title, and a name-keyed deny (``fs_read``) matches nothing."""
        event = _kas_builtin_flow("read_file")
        assert event.tool_name == "read_file"
        assert event.mcp_server_name == ""

    def test_the_frame_id_cannot_satisfy_a_trust_gated_grant(self) -> None:
        """Server-keyed grants stay closed. The trusted pair the cache attests is
        ``("", "")`` -- a KAS built-in writes a hit with empty names -- so every
        grant keyed on a trusted SERVER still sees none and does not fire. (The id
        does reach the read-only proof, by design, where only a read-only
        allowlisted id can gain; see ``TestClassifierOnlyHostTrustedProof``.)"""
        from kiro_crew.acp._dispatch import identified_mcp_call
        from kiro_crew.hooks import event_is_spawn_run

        event = _kas_builtin_flow("spawn_run", title="spawn_run")
        assert event.tool_name == "spawn_run"
        assert event.mcp_server_name == ""
        assert event_is_spawn_run(event) is False
        assert identified_mcp_call(event) is None

    def test_a_cached_tool_name_wins_over_the_frame_id(self) -> None:
        """kiro-cli's ``toolName`` on the tool_call is the primary channel; the
        frame id fills only an EMPTY name."""
        event = _kas_builtin_flow("read_file", tool_call_meta={"kiro": {"toolName": "fs_read"}})
        assert event.tool_name == "fs_read"

    @pytest.mark.parametrize(
        "bad",
        [7, "", "   ", None, ["read_file"], "k" * 129, "k" * (100 * 1024), "read_*", "a b"],
        ids=["int", "empty", "blank", "none", "list", "over-by-one", "100k", "glob", "space"],
    )
    def test_a_malformed_frame_id_is_ignored(self, bad) -> None:
        """Bounded at retention by the same rule ``_permission_tool_id`` applies:
        over ``_MAX_HARNESS_TOOL_ID_LEN``, a glob metacharacter or whitespace
        reads as absent, so the frame cannot grow the event or the audit log."""
        event = _kas_builtin_flow(bad)  # type: ignore[arg-type]
        assert event.tool_name == ""

    def test_the_frame_id_bound_is_the_permission_tool_id_bound(self) -> None:
        from kiro_crew.acp._dispatch import _MAX_HARNESS_TOOL_ID_LEN

        ok = "k" * _MAX_HARNESS_TOOL_ID_LEN
        assert _kas_builtin_flow(ok).tool_name == ok
        assert _kas_builtin_flow(ok + "k").tool_name == ""

    def test_a_frame_no_tool_call_preceded_is_not_named_by_its_raw_id(self) -> None:
        """The frame id FILLS a name the preceding tool_call left empty; it does not
        invent one. A request whose toolCallId misses the identity cache -- a
        sub-agent spawn, which KAS sends with no tool_call frame -- is classified
        by ``kas_consent_tool`` alone: a verified consent block yields the Crew
        name rules are written against, a disagreeing one yields nothing, and the
        raw spawn-family id never reaches the gate as a spelling no rule carries."""
        from kiro_crew.acp._dispatch import build_permission_event

        def frame(kiro: dict[str, Any]) -> AcpEvent:
            msg = JsonRpcMessage(
                id=42,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {
                        "toolCallId": "invoke_subagent_toolu_01",
                        "status": "pending",
                        "title": "Sub-agent: my-research",
                    },
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                    "_meta": {"kiro": kiro},
                },
            )
            event, _ = build_permission_event(
                msg,
                tool_input_cache={},
                shell_cache={},
                raw_params_cache={},
                mcp_server_name_cache={},
                tool_name_cache={},
                kas_consent_meta=True,
            )
            return event

        verified = frame(
            {
                "toolId": "invoke_sub_agent",
                "consent": {"capability": "subagent", "resource": "my-research"},
            }
        )
        assert verified.tool_name == "use_subagent"
        disagreeing = frame({"toolId": "invoke_sub_agent", "consent": {"capability": "shell"}})
        assert disagreeing.tool_name == ""
        # And a built-in id with no preceding tool_call is not named either: the
        # id is trusted as the tool_call's complement, not on its own.
        assert frame({"toolId": "read_file"}).tool_name == ""

    def test_a_kiro_cli_fs_read_deny_binds_to_a_kas_read_file_permission(self) -> None:
        """End to end through the gate: the rule an operator wrote for kiro-cli
        denies the KAS built-in that does the same work, from the frame alone."""
        event = _kas_builtin_flow("read_file")
        from kiro_crew.hooks import HooksConfig

        mgr = HookManager(HooksConfig(auto_deny_tools=["fs_read"]))
        decision = mgr.on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            kas_builtin_ids=event.kas_builtin_ids,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action == TOOL_DENY

    def test_the_same_frame_off_kas_is_marked_and_judged_as_a_bare_id(self) -> None:
        """A harness that is not handed the KAS store builds the same event
        with ``kas_builtin_ids`` False, so the kiro-cli rule does not fold onto
        its bare ``read_file`` and the read is not proven read-only."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, HooksConfig, hook_gate_kwargs

        on_kas = _kas_builtin_flow("read_file")
        off_kas = _kas_builtin_flow("read_file", on_kas=False)
        assert on_kas.kas_builtin_ids is True
        assert off_kas.kas_builtin_ids is False
        assert off_kas.tool_name == on_kas.tool_name == "read_file"
        deny_fs_read = HookManager(HooksConfig(auto_deny_tools=["fs_read"]))
        assert (
            deny_fs_read.on_tool_call(off_kas.title, **hook_gate_kwargs(off_kas)).action
            != TOOL_DENY
        )
        assert (
            deny_fs_read.on_tool_call(on_kas.title, **hook_gate_kwargs(on_kas)).action == TOOL_DENY
        )
        r = HookManager().on_tool_call(
            off_kas.title, classifier_only=True, **hook_gate_kwargs(off_kas)
        )
        assert r.action != TOOL_AUTO_APPROVE


class TestKasMcpServedIdentity:
    """A KAS MCP-served call as the wire carries it (captured live, kiro-cli 2.24.0,
    a stdio server whose one tool is named ``read_file`` on purpose): the
    tool_call frame stamps ``_meta.kiro.serverName`` and no tool name; the
    permission request stamps ``_meta.kiro.mcpTool.identity`` and a
    ``toolId`` of ``mcp_<server>_<tool>``. Both must read as an MCP call with a
    server -- never as the engine's own ``read_file``."""

    _TOOL_CALL = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-mcp",
        "title": "@probefs/read_file",
        "kind": "other",
        "status": "pending",
        "rawInput": {"path": "/tmp/probe-note.txt"},
        "_meta": {"kiro": {"serverName": "probefs", "toolOrigin": "client"}},
    }
    _PERMISSION_META = {
        "kiro": {
            "toolId": "mcp_probefs_read_file",
            "agentManagesTrust": True,
            "consent": {"capability": "mcp", "resource": "probefs/read_file"},
            "mcpTool": {
                "version": 1,
                "identity": {"serverName": "probefs", "toolName": "read_file"},
            },
        }
    }

    def _flow(self) -> AcpEvent:
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(dict(self._TOOL_CALL), **caches)
        msg = JsonRpcMessage(
            id=43,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {
                    "toolCallId": "tc-mcp",
                    "status": "pending",
                    "title": "@probefs/read_file",
                },
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": self._PERMISSION_META,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        return event

    def test_the_tool_call_frame_names_the_server(self) -> None:
        identity = classify_tool_call(dict(self._TOOL_CALL))
        assert identity.mcp_server_name == "probefs"
        assert identity.is_shell is False

    @pytest.mark.parametrize(
        "server",
        ["s" * 513, "s" * (8 * 1024 * 1024), "", None, ["probefs"]],
        ids=["over-bound", "8MiB", "empty", "none", "list"],
    )
    def test_an_oversized_or_malformed_server_name_on_the_frame_is_read_as_absent(
        self, server: object
    ) -> None:
        """The KAS ``serverName`` half is retained per call (``mcp_server_name_cache``)
        under the KAS-only admission, so it is bounded at the point it is read by
        what Crew's own MCP tool surface admits (``mcp_identity_name``, 512
        characters): a repeated 8 MiB ``serverName`` retains nothing. The kiro-cli
        ``mcpServerName`` channel is a first-class path and reads as it always did
        (a string verbatim, anything else absent), gaining no bound (H13)."""
        frame = dict(self._TOOL_CALL)
        frame["_meta"] = {"kiro": {"serverName": server, "toolOrigin": "client"}}
        identity = classify_tool_call(frame)
        assert identity.mcp_server_name == ""
        caches: dict[str, Any] = {"mcp_server_name_cache": {}, "tool_name_cache": {}}
        parse_session_update(frame, **caches)
        assert caches["mcp_server_name_cache"].get("tc-mcp", "") == ""
        kiro = dict(self._TOOL_CALL)
        kiro["_meta"] = {"kiro": {"mcpServerName": server, "toolName": "t"}}
        expected = server if isinstance(server, str) else ""
        assert classify_tool_call(kiro).mcp_server_name == expected
        # The bounded spelling still names the server.
        ok = classify_tool_call(dict(self._TOOL_CALL))
        assert ok.mcp_server_name == "probefs"

    @pytest.mark.parametrize(
        "server",
        ["s" * 512, "s" * 129, "probe*", "probe fs", "probe/fs:v2"],
        ids=["at-bound", "129", "glob", "space", "separators"],
    )
    def test_every_name_the_surface_admits_keeps_its_identity(self, server: str) -> None:
        """The bound is the SURFACE's, not the built-in id's: a server or tool name
        Crew's own MCP tool surface would list -- up to 512 characters, any
        spelling -- is retained, because an exact per-tool deny binds to nothing
        else and emptying it would hand the call to the title-keyed grant loop.
        The name is matched as a name, never as a pattern, so a metacharacter in
        it is inert."""
        from kiro_crew.mcp_gateway.tool_surface import admits_name

        assert admits_name(server)
        frame = dict(self._TOOL_CALL)
        frame["_meta"] = {"kiro": {"serverName": server, "toolOrigin": "client"}}
        assert classify_tool_call(frame).mcp_server_name == server
        caches: dict[str, Any] = {"mcp_server_name_cache": {}, "tool_name_cache": {}}
        parse_session_update(frame, **caches)
        assert caches["mcp_server_name_cache"]["tc-mcp"] == server

    def test_only_the_kas_channel_bounds_its_halves(self) -> None:
        """The bound is a property of the KAS channel row (``bounds_names``): its
        two halves are bounded before retention and goose/kiro-cli/codex -- the
        first-class channels -- retain a string verbatim as they always did, so no
        first-class path gains a bound or a refusal (H13)."""
        from kiro_crew.acp._dispatch import (
            _CODEX_MCP_TOOL_CALL_MARKER,
            _MCP_IDENTITY_META_CHANNELS,
            _meta_identity,
        )

        assert [c.bounds_names for c in _MCP_IDENTITY_META_CHANNELS] == [True, False, False]
        # A kiro-cli built-in (toolName, no server of either spelling) falls
        # through the KAS row to its own, unbounded.
        builtin = {"_meta": {"kiro": {"toolName": "t" * 600}}}
        assert _meta_identity(builtin) == ("", "t" * 600)
        huge = "t" * 4096
        kas = {"_meta": {"kiro": {"serverName": "srv", "toolName": huge}}}
        assert _meta_identity(kas) == ("srv", "")
        kiro = {"_meta": {"kiro": {"mcpServerName": "srv", "toolName": huge}}}
        assert _meta_identity(kiro) == ("srv", huge)
        goose = {"_meta": {"goose": {"toolCall": {"extensionName": "ext", "toolName": huge}}}}
        assert _meta_identity(goose) == ("ext", huge)
        goose_ok = {
            "_meta": {"goose": {"toolCall": {"extensionName": "ext", "toolName": "ext__tool"}}}
        }
        assert _meta_identity(goose_ok) == ("ext", "tool")
        codex = {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-cx",
            "kind": "execute",
            "rawInput": {"server": huge, "tool": "read"},
            "_meta": {_CODEX_MCP_TOOL_CALL_MARKER: True},
        }
        identity = classify_tool_call(codex)
        assert (identity.mcp_server_name, identity.tool_name) == (huge, "read")
        assert identity.identity_unreadable is False

    def test_the_permission_event_carries_the_canonical_pair(self) -> None:
        event = self._flow()
        assert (event.mcp_server_name, event.tool_name) == ("probefs", "read_file")

    @pytest.mark.parametrize(
        "bad",
        ["k" * 513, "k" * (100 * 1024), ""],
        ids=["over-by-one", "100k", "empty"],
    )
    def test_a_malformed_identity_pair_field_is_read_as_absent(self, bad) -> None:
        """The pair is bounded at retention by the surface's MCP-name bound: an
        oversized ``serverName`` names no server (the cached name stands), and an
        oversized ``toolName`` names no tool (the ``toolId`` stands). Neither is
        ever cut -- a truncated name is a different tool to an exact deny."""
        import copy

        meta = copy.deepcopy(self._PERMISSION_META)
        meta["kiro"]["mcpTool"]["identity"]["serverName"] = bad
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(dict(self._TOOL_CALL), **caches)
        msg = JsonRpcMessage(
            id=44,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "t"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": meta,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        # The tool_call's ``serverName`` channel already cached the server; the
        # pair with a malformed half names no tool, so the ``toolId`` stands.
        assert event.mcp_server_name == "probefs"
        assert event.tool_name == "mcp_probefs_read_file"

        meta2 = copy.deepcopy(self._PERMISSION_META)
        meta2["kiro"]["mcpTool"]["identity"]["toolName"] = bad
        caches2 = {k: {} for k in caches}
        parse_session_update(dict(self._TOOL_CALL), **caches2)
        msg2 = JsonRpcMessage(id=45, method="session/request_permission", params=dict(msg.params))
        msg2.params["_meta"] = meta2
        event2, _ = build_permission_event(msg2, **caches2)
        assert event2.tool_name == "mcp_probefs_read_file"

    @pytest.mark.parametrize("length", [129, 300, 512], ids=["129", "300", "at-bound"])
    def test_a_long_but_valid_mcp_tool_name_keeps_its_per_tool_deny(self, length: int) -> None:
        """A server may name a tool in up to 512 characters and Crew's own tool
        surface lists it; an operator's exact ``@server/<that name>`` deny must
        still bind. Under the built-in identifier bound the pair read as no
        identity, the deny target was dropped, and the call fell to the
        title-keyed grant loop -- so a 129-character name was auto-approved
        past its own deny."""
        import copy

        from kiro_crew.hooks import HooksConfig

        long_tool = "very_long_tool_name_" + "x" * (length - len("very_long_tool_name_"))
        assert len(long_tool) == length
        meta = copy.deepcopy(self._PERMISSION_META)
        meta["kiro"]["toolId"] = "mcp_probefs_" + long_tool
        meta["kiro"]["mcpTool"]["identity"]["toolName"] = long_tool
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(dict(self._TOOL_CALL), **caches)
        msg = JsonRpcMessage(
            id=46,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "harmless"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": meta,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        assert (event.mcp_server_name, event.tool_name) == ("probefs", long_tool)
        decision = HookManager(
            HooksConfig(auto_deny_tools=[f"@probefs/{long_tool}"], auto_approve_tools=["harmless"])
        ).on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action == TOOL_DENY

    @pytest.mark.parametrize(
        "channel",
        ["kiro-serverName", "kiro-toolName", "kiro-cli-toolName", "codex-tool", "permission-pair"],
    )
    def test_a_present_but_oversized_identity_half_is_unreadable_not_absent(
        self, channel: str
    ) -> None:
        """A 513-character half on the KAS channel is NOT a frame that names no
        tool: reading it as absent dropped the exact per-tool deny and handed the
        call to the title-keyed grant loop, so the KAS frame is marked unreadable
        and the permission event carries the mark; an absent half is not
        unreadable. The first-class channels (kiro-cli ``toolName``, the codex
        pair) are never marked -- they retain verbatim, as they always did (H13).
        """
        import copy

        from kiro_crew.acp._dispatch import _CODEX_MCP_TOOL_CALL_MARKER

        over = "n" * 513
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
            "identity_unreadable_cache": {},
        }
        frame = dict(self._TOOL_CALL)
        perm_meta = copy.deepcopy(self._PERMISSION_META)
        if channel == "kiro-serverName":
            frame["_meta"] = {"kiro": {"serverName": over, "toolOrigin": "client"}}
        elif channel == "kiro-toolName":
            frame["_meta"] = {"kiro": {"serverName": "probefs", "toolName": over}}
        elif channel == "kiro-cli-toolName":
            frame["_meta"] = {"kiro": {"mcpServerName": "probefs", "toolName": over}}
        elif channel == "codex-tool":
            frame["kind"] = "execute"
            frame["rawInput"] = {"server": "probefs", "tool": over}
            frame["_meta"] = {_CODEX_MCP_TOOL_CALL_MARKER: True}
        else:
            perm_meta["kiro"]["mcpTool"]["identity"]["toolName"] = over
        kas_marked = channel in ("kiro-serverName", "kiro-toolName")
        first_class = channel in ("kiro-cli-toolName", "codex-tool")
        identity = classify_tool_call(frame)
        assert identity.identity_unreadable is kas_marked
        if first_class:
            assert over in (identity.mcp_server_name, identity.tool_name)
        else:
            assert over not in (identity.mcp_server_name, identity.tool_name)
        parse_session_update(frame, **caches)
        assert caches["identity_unreadable_cache"].get("tc-mcp") is (True if kas_marked else None)
        if first_class:
            return
        msg = JsonRpcMessage(
            id=47,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "harmless"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": perm_meta,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        assert event.mcp_identity_unreadable is True
        assert over not in (event.mcp_server_name, event.tool_name)
        # The clean shape is untouched: nothing presented, nothing unreadable.
        clean: dict[str, Any] = {k: {} for k in caches}
        parse_session_update(dict(self._TOOL_CALL), **clean)
        assert clean["identity_unreadable_cache"] == {}
        ok_msg = JsonRpcMessage(id=48, method="session/request_permission", params=dict(msg.params))
        ok_msg.params["_meta"] = self._PERMISSION_META
        ok_event, _ = build_permission_event(ok_msg, **clean)
        assert ok_event.mcp_identity_unreadable is False

    @pytest.mark.parametrize(
        "cached_server, frame_server",
        [("", "probefs"), ("probefs", "other"), ("", "o" * 512)],
        ids=["builtin-then-server", "different-server", "builtin-then-long-server"],
    )
    def test_a_request_naming_a_server_the_tool_call_did_not_is_refused_not_adopted(
        self, cached_server: str, frame_server: str
    ) -> None:
        """The permission frame's ``mcpTool.identity.serverName`` never fills the
        cached server. A KAS built-in's tool_call writes an identity hit with an
        EMPTY server, so a server adopted from the request would carry the hit's
        trust into the server-keyed grants (the app-own MCP auto-approve) for a
        built-in. Agreement is a no-op; a server the tool_call did not name is
        refused on the KAS wire as an unreadable identity and never adopted; a
        first-class harness (no store handed) ignores the half."""
        import copy

        frame = dict(self._TOOL_CALL)
        if cached_server:
            frame["_meta"] = {"kiro": {"serverName": cached_server, "toolOrigin": "client"}}
        else:
            frame["_meta"] = {"kiro": {"toolOrigin": "client"}}
        perm_meta = copy.deepcopy(self._PERMISSION_META)
        perm_meta["kiro"]["mcpTool"]["identity"]["serverName"] = frame_server
        perm_meta["kiro"]["toolId"] = "read_file"
        for handed in (True, False):
            caches: dict[str, Any] = {
                "tool_input_cache": {},
                "shell_cache": {},
                "raw_params_cache": {},
                "mcp_server_name_cache": {},
                "tool_name_cache": {},
            }
            if handed:
                caches["identity_unreadable_cache"] = {}
            parse_session_update(frame, **caches)
            assert caches["mcp_server_name_cache"]["tc-mcp"] == cached_server
            msg = JsonRpcMessage(
                id=49,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "Read File"},
                    "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                    "_meta": perm_meta,
                },
            )
            event, _ = build_permission_event(msg, **caches)
            # The request's server is never adopted; the cache's stands.
            assert event.mcp_server_name == cached_server, handed
            assert frame_server not in (event.mcp_server_name, event.tool_name)
            assert event.mcp_identity_unreadable is handed
            if not handed:
                # The first-class shape reads as it always did: trusted per the
                # cache hit, serverless when the tool_call was.
                assert event.mcp_identity_trusted is True
        # Agreement stays a no-op and is never marked.
        ok = self._flow()
        assert (ok.mcp_server_name, ok.mcp_identity_unreadable) == ("probefs", False)

    @pytest.mark.asyncio
    async def test_the_handle_refuses_a_request_whose_identity_is_unreadable(self) -> None:
        """Refused before any consumer sees it, on the KAS wire shape and on a
        first-class harness alike (a fact about the frame, not a backend), and
        audited; a frame whose identity is readable is untouched."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        for backend in (ACP_BACKEND_KAS,):
            rt = MagicMock()
            rt.acp_backend = backend
            rt.pid = None
            rt.is_alive = MagicMock(return_value=True)
            rt.send_notification = AsyncMock()
            rt.supports_image_prompt = False
            sent: list[tuple] = []

            async def _send_response(request_id, payload, _sent=sent):
                _sent.append((request_id, payload))

            rt.send_response = _send_response
            handle = AcpSessionHandle("s-1", asyncio.Queue(), rt)
            audits: list[tuple] = []
            handle._audit_handle_reject = lambda *a, **k: audits.append((a, k))  # type: ignore[method-assign]
            frame = dict(self._TOOL_CALL)
            frame["_meta"] = {"kiro": {"serverName": "probefs", "toolName": "n" * 513}}
            handle._handle_update(
                JsonRpcMessage(
                    method="session/update", params={"sessionId": "s-1", "update": frame}
                )
            )
            msg = JsonRpcMessage(
                id=49,
                method="session/request_permission",
                params={
                    "sessionId": "s-1",
                    "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "harmless"},
                    "options": [
                        {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                    ],
                },
            )
            event = handle._build_permission_event(msg)
            assert event is not None and event.mcp_identity_unreadable is True
            assert await handle._refuse_unreadable_mcp_identity(event) is True, backend
            assert sent == [(49, {"outcome": {"outcome": "selected", "optionId": "reject"}})]
            assert audits and audits[0][0][1:3] == ("mcp__unreadable", "mcp_identity_unreadable")
            # Readable: not refused by this.
            handle._handle_update(
                JsonRpcMessage(
                    method="session/update",
                    params={"sessionId": "s-1", "update": dict(self._TOOL_CALL)},
                )
            )
            ok = handle._build_permission_event(
                JsonRpcMessage(id=50, method="session/request_permission", params=dict(msg.params))
            )
            assert ok is not None and ok.mcp_identity_unreadable is False
            assert await handle._refuse_unreadable_mcp_identity(ok) is False
        # A first-class harness's handle is handed no store: the KAS-spelled frame
        # would mark, but nothing records it and nothing refuses (H13).
        rt2 = MagicMock()
        rt2.acp_backend = "kiro"
        rt2.pid = None
        rt2.is_alive = MagicMock(return_value=True)
        rt2.send_notification = AsyncMock()
        rt2.supports_image_prompt = False
        kiro_handle = AcpSessionHandle("s-1", asyncio.Queue(), rt2)
        assert kiro_handle._identity_unreadable_cache_for_wire() is None
        kiro_handle._handle_update(
            JsonRpcMessage(method="session/update", params={"sessionId": "s-1", "update": frame})
        )
        assert kiro_handle._tool_call_identity_unreadable == {}
        kiro_event = kiro_handle._build_permission_event(
            JsonRpcMessage(id=51, method="session/request_permission", params=dict(msg.params))
        )
        assert kiro_event is not None and kiro_event.mcp_identity_unreadable is False
        assert await kiro_handle._refuse_unreadable_mcp_identity(kiro_event) is False
        # Wired before the consumer yield, after the unidentifiable-MCP refusal.
        import inspect

        from kiro_crew.acp import session_handle

        src = inspect.getsource(session_handle.AcpSessionHandle._dispatch_events)
        a = src.index("self._refuse_unidentifiable_mcp_approval(msg, _perm_event)")
        b = src.index("self._refuse_unreadable_mcp_identity(_perm_event)")
        c = src.index("self._refuse_unretained_provenance(msg, _perm_event)")
        assert a < b < c < src.index("yield _perm_event")

    def test_an_mcp_read_file_is_not_the_builtin_for_the_gate(self) -> None:
        """The reason the server must be read: a built-in ``fs_read`` deny must not
        bind to a server's ``read_file``, and the host read-only proof must not
        treat it as the engine's own."""
        from kiro_crew.hooks import HooksConfig, _is_host_read_only_builtin

        event = self._flow()
        assert (
            _is_host_read_only_builtin(
                event.tool_name, event.mcp_server_name, mcp_identity_trusted=True
            )
            is False
        )
        decision = HookManager(HooksConfig(auto_deny_tools=["fs_read"])).on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action != TOOL_DENY

    def test_a_per_server_rule_binds_through_the_pair(self) -> None:
        from kiro_crew.hooks import HooksConfig

        event = self._flow()
        decision = HookManager(HooksConfig(auto_deny_tools=["@probefs/read_file"])).on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action == TOOL_DENY

    def test_a_kiro_cli_frame_still_reads_through_the_first_channel(self) -> None:
        identity = classify_tool_call(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-k",
                "title": "Look up weather",
                "kind": "other",
                "_meta": {"kiro": {"mcpServerName": "weather", "toolName": "forecast"}},
            }
        )
        assert (identity.mcp_server_name, identity.tool_name) == ("weather", "forecast")
        assert identity.identity_trusted is True
