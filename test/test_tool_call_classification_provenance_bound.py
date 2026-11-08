"""The per-turn provenance caches share one bound: a count cap over the union of their
keys and a size bound on the one externally sized field a row carries, with a refused
frame written to none of them and its overflow reported.
``test_tool_call_classification.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

from typing import Any

import pytest
from stray_line_helpers import cyclic_gc_quiesced  # noqa: F401 - fixture

from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
from kiro_crew.acp.types import JsonRpcMessage


class TestTheProvenanceCachesShareOneBound:
    """The per-turn provenance caches are keyed on the same scoped id and written
    for the same frames, so they are ONE population with ONE admission: a count
    bound over the union of their keys and a size bound on the one externally
    sized field a row carries. A refused frame is written to none of them, its
    scope is recorded, and the handle refuses the permission request for it."""

    @staticmethod
    def _caches() -> dict[str, Any]:
        return {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
            "diff_path_cache": {},
            "tool_kind_cache": {},
            "tool_input_redacted_cache": {},
            "identity_unreadable_cache": {},
            "harness_tool_name_cache": {},
            "cache_overflow": set(),
        }

    @staticmethod
    def _over(*scopes: str) -> set[str]:
        """The overflow record for *scopes*: fixed-size digests, never the text."""
        from kiro_crew.acp._dispatch import overflow_scope_key

        return {overflow_scope_key(sc) for sc in scopes}

    @staticmethod
    def _call(i: int, **extra: Any) -> dict[str, Any]:
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": f"tc-{i}",
            "title": "Write File",
            "kind": "edit",
            "status": "pending",
            "rawInput": {"path": f"/tmp/f{i}", "text": "x"},
            "_meta": {"kiro": {"toolName": "fs_write", "mcpServerName": ""}},
            **extra,
        }

    @staticmethod
    def _dicts(caches: dict[str, Any]) -> list[dict]:
        return [c for c in caches.values() if isinstance(c, dict)]

    def test_the_count_cap_is_one_constant_over_the_union_of_sibling_keys(self, caplog) -> None:
        import logging

        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES):
            parse_session_update(self._call(i), cache_scope="s", **caches)
        keys: set[str] = set()
        for c in self._dicts(caches):
            keys.update(c.keys())
        assert len(keys) == TOOL_CALL_CACHE_MAX_ENTRIES
        assert caches["cache_overflow"] == set()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            parse_session_update(self._call(TOOL_CALL_CACHE_MAX_ENTRIES), cache_scope="s", **caches)
            parse_session_update(
                self._call(TOOL_CALL_CACHE_MAX_ENTRIES + 1), cache_scope="s", **caches
            )
        over = f"s|tc-{TOOL_CALL_CACHE_MAX_ENTRIES}"
        assert all(over not in c for c in self._dicts(caches)), "a refused id got a row"
        assert caches["cache_overflow"] == self._over("s")
        # Overflow is said once per scope, not once per refused frame.
        assert sum("refused a frame" in r.getMessage() for r in caplog.records) == 1

    def test_the_harness_name_store_is_a_sibling_under_the_same_admission(self) -> None:
        """The ``toolCallId -> harness_tool_name`` store is written on the same
        frames and keyed on the same scoped id as the other provenance caches, so
        it is one more member of the population the admission bounds: a frame the
        gate refuses -- here for an oversized id -- gets no row in it either, an
        admitted frame's row is there, and the handle releases it at the terminal
        with its siblings. Without this the store grew with turn length on KAS
        outside the count bound, under keys the key bound had refused."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_KEY_CHARS

        caches = self._caches()
        parse_session_update(self._call(0), cache_scope="s", **caches)
        # Written on every admitted tool_call, "" included (the fixture's title
        # is prose, not an identifier), so the row is the fact to pin.
        assert caches["harness_tool_name_cache"] == {"s|tc-0": ""}
        parse_session_update(
            self._call(1, toolCallId="x" * (TOOL_CALL_CACHE_MAX_KEY_CHARS + 1)),
            cache_scope="s",
            **caches,
        )
        assert len(caches["harness_tool_name_cache"]) == 1, "a refused id got a harness-name row"
        assert caches["cache_overflow"] == self._over("s")
        handle, _ = self._handle()
        handle._handle_update(self._frame(self._call(0, title="bash")))
        key = "s-1|tc-0"
        assert handle._tool_call_harness_tool_name[key] == "bash"
        handle._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": "completed"},
            "s-1",
        )
        assert key not in handle._tool_call_harness_tool_name

    def test_overflow_is_said_again_on_the_next_snapshot(self, caplog) -> None:
        """The warning is deduplicated against the caller-owned overflow record,
        which the turn reset clears with the caches -- NOT against anything that
        outlives the turn. A scope that overflows on two consecutive snapshots
        is said out loud on both; within one snapshot it is said once."""
        import logging

        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        def overflow_once(caches: dict[str, Any]) -> int:
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
                for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 2):
                    parse_session_update(self._call(i), cache_scope="s", **caches)
            return sum("refused a frame" in r.getMessage() for r in caplog.records)

        caches = self._caches()
        assert overflow_once(caches) == 1
        assert caches["cache_overflow"] == self._over("s")
        # Same snapshot, same scope, more refused frames: still said once.
        assert overflow_once(caches) == 0
        # The per-turn reset clears the record with the caches; the next
        # snapshot's overflow is a new fact and is reported again.
        for c in caches.values():
            if isinstance(c, (dict, set)):
                c.clear()
        assert overflow_once(caches) == 1
        assert caches["cache_overflow"] == self._over("s")

    def test_an_oversized_payload_is_refused_whether_the_id_is_new_or_held(self) -> None:
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        caches = self._caches()
        big = "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)
        parse_session_update(self._call(1, rawInput={"path": "/tmp/f1", "text": big}), **caches)
        assert all("tc-1" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")
        # A small first frame does not reserve a row a huge refinement then fills.
        caches = self._caches()
        parse_session_update(self._call(2), **caches)
        assert caches["raw_params_cache"]["tc-2"]["text"] == "x"
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "rawInput": {"path": "/tmp/f2", "text": big},
            },
            **caches,
        )
        # The refused refinement evicts the held row: a stale target must not
        # stay trusted for the permission request that follows.
        assert all("tc-2" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")

    # The 5,000-deep payload walks the encoder to its recursion limit, so the cyclic
    # collector is held off for the test (testing-conventions.md, RecursionError).
    @pytest.mark.usefixtures("cyclic_gc_quiesced")
    def test_a_payload_the_encoder_could_not_render_is_refused_not_retained_unmeasured(
        self,
    ) -> None:
        """A ``rawInput`` nested past the encoder's ceiling renders as the fixed
        placeholder, so measuring the rendering would admit the dict behind it at
        49 characters however large it is -- the one way past the payload bound.
        A degraded rendering is no measurement: the frame is refused on both
        builders, the scope is recorded, and the event still DISPLAYS the
        placeholder (the degrade is for the reader; only the retained provenance
        is withheld). A payload that is deep but still renders is measured on its
        real size and admitted."""
        from kiro_crew.acp._dispatch import (
            TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
            UNSERIALISABLE_SIBLING_VALUE,
        )

        def nest(depth: int, leaf: object) -> dict:
            node: object = leaf
            for _ in range(depth):
                node = {"k": node}
            return {"path": "/tmp/f1", "deep": node}

        too_deep = nest(5000, "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1))
        caches = self._caches()
        events = parse_session_update(self._call(1, rawInput=too_deep), **caches)
        assert all("tc-1" not in c for c in self._dicts(caches)), "unmeasured params retained"
        assert caches["cache_overflow"] == self._over("")
        assert any(UNSERIALISABLE_SIBLING_VALUE in (e.tool_input or "") for e in events)
        # The refinement builder measures the same way: a held id refined with
        # an unrenderable payload is evicted, not kept under the placeholder.
        caches = self._caches()
        parse_session_update(self._call(2), **caches)
        assert "tc-2" in caches["raw_params_cache"]
        parse_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-2", "rawInput": too_deep},
            **caches,
        )
        assert all("tc-2" not in c for c in self._dicts(caches))
        # Control: deep but renderable is measured on its real size and admitted.
        caches = self._caches()
        parse_session_update(self._call(3, rawInput=nest(50, "x")), **caches)
        assert "tc-3" in caches["raw_params_cache"]
        assert caches["cache_overflow"] == set()

    def test_a_large_params_object_beside_a_small_final_rendering_is_measured_on_the_params(
        self,
    ) -> None:
        """The converse hole: a diff block REPLACES the params rendering with a
        usually smaller string, but the row retains the ``raw_params`` dict too.
        A near-limit ``rawInput`` beside a tiny diff must be refused on the
        params' size, on the initial frame and on a refinement of a held id;
        otherwise 256 such frames retain gigabytes the gate never measured."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        big = {"path": "/tmp/f1", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)}
        tiny = [{"type": "diff", "path": "/tmp/f1", "oldText": "a", "newText": "b"}]
        caches = self._caches()
        parse_session_update(self._call(1, rawInput=big, content=tiny), **caches)
        assert all("tc-1" not in c for c in self._dicts(caches)), "unmeasured params retained"
        assert caches["cache_overflow"] == self._over("")
        # Refinement of a held id: the earlier row is evicted, not widened.
        refined = self._caches()
        parse_session_update(self._call(2, content=tiny), **refined)
        assert refined["raw_params_cache"]["tc-2"]["path"] == "/tmp/f2"
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "title": "Write File",
                "rawInput": big,
                "content": tiny,
            },
            **refined,
        )
        assert all("tc-2" not in c for c in self._dicts(refined)), "unmeasured params retained"
        # The same params beside a small diff, one under the bound, are admitted,
        # so the refusal is the params' size and not the diff block's presence.
        ok = self._caches()
        fits = {"path": "/tmp/f3", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS // 2)}
        parse_session_update(self._call(3, rawInput=fits, content=tiny), **ok)
        assert ok["raw_params_cache"]["tc-3"]["path"] == "/tmp/f3"
        assert ok["cache_overflow"] == set()

    def test_the_size_bound_measures_the_final_rendering_not_the_params(self) -> None:
        """A small ``rawInput`` beside a large diff -- the content block, or the
        edit diff derived from an ``insert`` body -- must not pass admission on
        the params' size and then retain the diff unmeasured: the bound is
        applied to the string the cache retains, after diff rendering and
        redaction, in both builders."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        big = "x\n" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS // 2 + 1)
        # Derived diff: an ``insert`` renders its body line-by-line without the
        # unified-diff length cap, so the retained string is the body's size.
        caches = self._caches()
        parse_session_update(
            self._call(
                1,
                rawInput={"path": "/tmp/f1", "command": "insert", "insertLine": 0, "fileText": big},
            ),
            **caches,
        )
        assert all("tc-1" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")
        # Diff content block on the initial frame.
        caches = self._caches()
        parse_session_update(
            self._call(
                2,
                rawInput={"path": "/tmp/f2"},
                content=[{"type": "diff", "path": "/tmp/f2", "oldText": "", "newText": big}],
            ),
            **caches,
        )
        # The unified-diff renderer caps its output, so this frame is admitted
        # -- and what is retained is the capped rendering, never the block.
        assert len(caches["tool_input_cache"]["tc-2"]) <= TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS
        # Refinement carrying an oversized derived rendering of a held id.
        caches = self._caches()
        parse_session_update(self._call(3), **caches)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "rawInput": {"path": "/tmp/f3", "text": big},
            },
            **caches,
        )
        assert all("tc-3" not in c for c in self._dicts(caches)), "stale row survived"

    def test_a_small_params_object_beside_a_larger_final_rendering_is_measured_on_the_rendering(
        self, monkeypatch
    ) -> None:
        """The discriminating case: params well under the bound, the diff
        rendered from the content block over it. With the bound lowered below
        the unified-diff renderer's own cap, the only way the frame is refused
        is if the gate measured the FINAL string; a gate that measured the
        params rendering admits it and retains the diff unmeasured."""
        from kiro_crew.acp import _dispatch

        monkeypatch.setattr(_dispatch, "TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS", 2_000)
        new_text = "line\n" * 1_000  # renders to ~6 KiB of diff, under the 64 KiB cap
        # Initial frame: rawInput of ~20 chars, diff block of ~5 KiB.
        caches = self._caches()
        parse_session_update(
            self._call(
                1,
                rawInput={"path": "/tmp/f1"},
                content=[{"type": "diff", "path": "/tmp/f1", "oldText": "", "newText": new_text}],
            ),
            **caches,
        )
        assert all("tc-1" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")
        # Refinement: the held id's small first frame, then a refinement whose
        # rawInput is tiny but whose diff block renders over the bound.
        caches = self._caches()
        parse_session_update(self._call(2, rawInput={"path": "/tmp/f2"}), **caches)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "rawInput": {"path": "/tmp/f2"},
                "content": [
                    {"type": "diff", "path": "/tmp/f2", "oldText": "", "newText": new_text}
                ],
            },
            **caches,
        )
        assert all("tc-2" not in c for c in self._dicts(caches)), "stale row survived"
        assert caches["cache_overflow"] == self._over("")

    def test_every_retained_rendering_is_within_the_size_bound(self) -> None:
        """Property over the retained display strings: nothing in
        ``tool_input_cache`` ever exceeds the bound, whatever the frame shape."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        big = "y" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 5)
        caches = self._caches()
        frames = [
            self._call(1, rawInput={"path": "/tmp/a", "command": "create", "fileText": big}),
            self._call(
                2,
                rawInput={"path": "/tmp/b", "command": "strReplace", "oldStr": "a", "newStr": big},
            ),
            self._call(
                3,
                rawInput={"path": "/tmp/c", "command": "insert", "insertLine": 1, "fileText": big},
            ),
            self._call(4, rawInput={"path": "/tmp/d"}),
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "rawInput": {
                    "path": "/tmp/d",
                    "command": "insert",
                    "insertLine": 1,
                    "fileText": big,
                },
            },
        ]
        for frame in frames:
            parse_session_update(frame, **caches)
        assert all(
            len(v) <= TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS for v in caches["tool_input_cache"].values()
        )

    def test_a_refinement_of_a_held_id_is_admitted_past_the_count_cap(self) -> None:
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES):
            parse_session_update(self._call(i), **caches)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-0",
                "rawInput": {"path": "/tmp/f0-refined", "text": "y"},
            },
            **caches,
        )
        assert caches["raw_params_cache"]["tc-0"]["path"] == "/tmp/f0-refined"
        assert caches["cache_overflow"] == set()

    def test_an_oversized_tool_call_id_is_refused_as_overflow(self) -> None:
        """The scoped key is a retained string too: a ``toolCallId`` past the key
        bound is written nowhere and its scope is recorded, while one exactly at
        the bound is admitted. Otherwise a stream of near-limit ids spends the
        count cap on key bytes the size bound never measured."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_KEY_CHARS

        caches = self._caches()
        at_bound = "k" * TOOL_CALL_CACHE_MAX_KEY_CHARS
        over = "k" * (TOOL_CALL_CACHE_MAX_KEY_CHARS + 1)
        parse_session_update(self._call(1, toolCallId=at_bound), **caches)
        assert at_bound in caches["raw_params_cache"]
        parse_session_update(self._call(2, toolCallId=over), **caches)
        assert all(over not in c for c in self._dicts(caches)), "an oversized id got a row"
        assert caches["cache_overflow"] == self._over("")
        # The scope is part of the key: a scope that pushes an in-bound id over
        # the bound is refused the same way (the key is what is stored).
        scoped = self._caches()
        parse_session_update(self._call(3, toolCallId=at_bound), cache_scope="s", **scoped)
        assert all(f"s|{at_bound}" not in c for c in self._dicts(scoped))
        assert scoped["cache_overflow"] == self._over("s")
        # A refinement naming an oversized id is refused too, not just the first frame.
        refined = self._caches()
        parse_session_update(self._call(4), **refined)
        parse_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": over, "rawInput": {"a": 1}},
            **refined,
        )
        assert all(over not in c for c in self._dicts(refined))
        assert refined["cache_overflow"] == self._over("")

    def test_a_non_string_tool_call_id_is_not_a_key(self) -> None:
        """A ``toolCallId`` that is not a string (a list, a dict) never becomes a
        cache key by stringification: the initial frame retains nothing under it
        and the refinement is dropped, as a missing id is."""
        caches = self._caches()
        parse_session_update(self._call(1, toolCallId=["tc", "1"]), **caches)
        assert all(not c for c in self._dicts(caches))
        parse_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": {"id": 1}, "rawInput": {"a": 1}},
            **caches,
        )
        assert all(not c for c in self._dicts(caches))
        assert caches["cache_overflow"] == set()

    def test_an_oversized_diff_path_is_refused_as_overflow(self) -> None:
        """``diff_path_cache`` retains the frame's diff-block ``path`` verbatim,
        so a path past the path bound refuses the frame -- initial and
        refinement alike -- rather than being retained or cut; a path at the
        bound is retained whole, and a non-string path is no path."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PATH_CHARS

        def _diff(path: object) -> list[dict]:
            return [{"type": "diff", "path": path, "oldText": "a", "newText": "b"}]

        caches = self._caches()
        at_bound = "/" + "p" * (TOOL_CALL_CACHE_MAX_PATH_CHARS - 1)
        over = "/" + "p" * TOOL_CALL_CACHE_MAX_PATH_CHARS
        parse_session_update(self._call(1, content=_diff(at_bound)), **caches)
        assert caches["diff_path_cache"]["tc-1"] == at_bound
        parse_session_update(self._call(2, content=_diff(over)), **caches)
        assert all("tc-2" not in c for c in self._dicts(caches)), "an oversized path got a row"
        assert caches["cache_overflow"] == self._over("")
        # A held id's refinement carrying an oversized path is refused, and the
        # row it already had is evicted rather than kept as a stale target.
        refined = self._caches()
        parse_session_update(self._call(3, content=_diff("/tmp/f3")), **refined)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "title": "Write File",
                "content": _diff(over),
            },
            **refined,
        )
        assert all("tc-3" not in c for c in self._dicts(refined)), "stale row survived"
        assert refined["cache_overflow"] == self._over("")
        # A non-string path names no file: nothing is retained as the target.
        odd = self._caches()
        parse_session_update(self._call(4, content=_diff(["/tmp", "f4"])), **odd)
        assert "tc-4" not in odd["diff_path_cache"]
        assert "tc-4" in odd["raw_params_cache"]

    def test_every_retained_key_and_path_is_within_its_bound(self) -> None:
        """Property over the retained keys and diff paths, beside the payload
        property: whatever the frames carried, no key exceeds the key bound and
        no retained path exceeds the path bound."""
        from kiro_crew.acp._dispatch import (
            TOOL_CALL_CACHE_MAX_KEY_CHARS,
            TOOL_CALL_CACHE_MAX_PATH_CHARS,
        )

        caches = self._caches()
        long_id = "i" * (TOOL_CALL_CACHE_MAX_KEY_CHARS * 2)
        long_path = "/" + "q" * (TOOL_CALL_CACHE_MAX_PATH_CHARS * 2)
        frames = [
            self._call(1),
            self._call(2, toolCallId=long_id),
            self._call(
                3, content=[{"type": "diff", "path": long_path, "oldText": "", "newText": "z"}]
            ),
            self._call(4, toolCallId="ok-" + long_id[:8]),
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "title": "Write File",
                "content": [{"type": "diff", "path": long_path, "oldText": "", "newText": "z"}],
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": long_id,
                "rawInput": {"path": "/tmp/x"},
            },
        ]
        for frame in frames:
            parse_session_update(frame, cache_scope="scope", **caches)
        for c in self._dicts(caches):
            assert all(len(k) <= TOOL_CALL_CACHE_MAX_KEY_CHARS for k in c)
        assert all(
            len(p) <= TOOL_CALL_CACHE_MAX_PATH_CHARS for p in caches["diff_path_cache"].values()
        )
        assert caches["cache_overflow"] == self._over("scope")

    def test_a_refused_id_s_permission_event_carries_no_provenance(self) -> None:
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 1):
            parse_session_update(self._call(i), **caches)
        over = f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES}"
        msg = JsonRpcMessage(
            id=9,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": over, "status": "pending", "title": "Write File"},
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        perm_caches = {k: v for k, v in caches.items() if k != "cache_overflow"}
        event, _ = build_permission_event(msg, **perm_caches)
        assert event.raw_params_trusted is False
        assert event.shell_classified is False
        assert event.mcp_identity_trusted is False
        assert event.tool_kind == ""

    @staticmethod
    def _handle():
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_KAS
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        sent: list[tuple] = []

        async def _send_response(request_id, payload):
            sent.append((request_id, payload))

        rt.send_response = _send_response
        return AcpSessionHandle("s-1", asyncio.Queue(), rt), sent

    @staticmethod
    def _frame(update: dict[str, Any]) -> JsonRpcMessage:
        return JsonRpcMessage(
            method="session/update", params={"sessionId": "s-1", "update": update}
        )

    @staticmethod
    def _perm(rid: int, tool_call_id: str) -> JsonRpcMessage:
        return JsonRpcMessage(
            id=rid,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "status": "pending",
                    "title": "Delete File",
                },
                "options": [
                    {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                ],
                "_meta": {"kiro": {"toolId": "delete_file"}},
            },
        )

    @pytest.mark.asyncio
    async def test_the_handle_refuses_a_permission_request_the_caches_never_retained(
        self, caplog
    ) -> None:
        """GPT's scenario: a mounted KAS ``delete_file`` after the cap is reached.
        Its frame is written nowhere, so the permission event has no kind, no
        identity and no target; instead of yielding that to a consumer (whose
        AUTO_APPROVE path would judge the title alone) the handle refuses it.
        The refusal names both bounds, so an operator reading it alone knows
        which limit the call can meet."""
        import logging

        from kiro_crew.acp._dispatch import (
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
        )

        handle, sent = self._handle()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 1):
            handle._handle_update(self._frame(self._call(i)))
        over = f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES}"
        msg = self._perm(21, over)
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is False
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
            assert await handle._refuse_unretained_provenance(msg, event) is True
        assert sent == [(21, {"outcome": {"outcome": "selected", "optionId": "reject"}})]
        refusal = [r.getMessage() for r in caplog.records if "did not retain" in r.getMessage()]
        assert len(refusal) == 1
        assert str(TOOL_CALL_CACHE_MAX_ENTRIES) in refusal[0]
        assert str(TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS) in refusal[0]

    @pytest.mark.asyncio
    async def test_a_retained_id_and_a_scope_that_never_overflowed_are_untouched(self) -> None:
        handle, sent = self._handle()
        handle._handle_update(self._frame(self._call(0)))
        msg = self._perm(22, "tc-0")
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is True
        assert await handle._refuse_unretained_provenance(msg, event) is False
        # An unknown id on a scope the bound never refused is the pre-existing
        # cache-miss shape and goes to the consumer as before.
        msg2 = self._perm(23, "never-seen")
        event2 = handle._build_permission_event(msg2)
        assert event2 is not None and event2.raw_params_trusted is False
        assert await handle._refuse_unretained_provenance(msg2, event2) is False
        assert sent == []

    def test_a_terminal_result_releases_the_call_s_provenance_from_every_store(self) -> None:
        """A settled call's rows are dead weight: the permission request preceded
        execution and nothing reads them afterwards, so the caches hold calls IN
        FLIGHT and the count bound stops being a bound on turn length."""
        handle, _ = self._handle()
        handle._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
        key = "s-1|tc-0"
        stores = (
            handle._tool_call_inputs,
            handle._tool_call_input_redacted,
            handle._tool_call_is_shell,
            handle._tool_call_raw_params,
            handle._tool_call_kind,
            handle._tool_call_tool_name,
        )
        assert all(key in st for st in stores), [key in st for st in stores]
        # A non-terminal refinement releases nothing.
        handle._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": "in_progress"},
            "s-1",
        )
        assert all(key in st for st in stores)
        # A terminal frame under ANOTHER scope releases nothing -- a child's
        # terminal must not release a parent's row.
        handle._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": "completed"},
            "child-scope",
        )
        assert all(key in st for st in stores)
        for status in ("completed", "failed", "cancelled", "canceled", "refused"):
            handle._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
            handle._release_settled_tool_call(
                {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": status},
                "s-1",
            )
            assert not any(key in st for st in stores), status
            assert (
                key not in handle._tool_call_mcp_server and key not in handle._tool_call_diff_path
            )
        # A stray terminal for an id no store holds is a no-op.
        handle._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "never", "status": "completed"},
            "s-1",
        )
        # A first-class harness's handle holds its rows until the turn's clear(),
        # exactly as on main -- the release is the KAS admission's (H13).
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = MagicMock()
        rt.acp_backend = "kiro"
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        kiro = AcpSessionHandle("s-1", asyncio.Queue(), rt)
        kiro._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
        assert key in kiro._tool_call_raw_params
        kiro._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": "completed"},
            "s-1",
        )
        assert key in kiro._tool_call_raw_params and key in kiro._tool_call_is_shell

    @pytest.mark.asyncio
    async def test_a_kas_turn_past_the_cap_keeps_working_when_its_calls_settle(self) -> None:
        """Opus's scenario, the other way round: three hundred calls in one turn,
        each settled by its terminal frame before the next, never overflow -- the
        population is one call in flight -- so the 300th call's permission
        request is admitted with full provenance, not refused."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        handle, sent = self._handle()
        rt = handle._runtime
        frames: list[JsonRpcMessage] = []
        n = TOOL_CALL_CACHE_MAX_ENTRIES + 44
        for i in range(n):
            frames.append(self._frame(self._call(i, rawInput={"targetFile": f"/tmp/f{i}"})))
            frames.append(
                self._frame(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": f"tc-{i}",
                        "status": "completed",
                    }
                )
            )
        queue = handle._queue

        async def _send_request(method, params):
            for f in frames:
                queue.put_nowait(f)
            queue.put_nowait(JsonRpcMessage(id=7, result={"stopReason": "end_turn"}))
            return 7

        rt.send_request = _send_request
        rt.mark_turn_active = lambda *_a, **_k: None
        events = [ev async for ev in handle.stream_command("/noop", timeout=5.0)]
        assert events, "the scripted turn produced events"
        assert handle._tool_call_cache_overflow == set()
        assert handle._tool_call_raw_params == {}
        # The next call is admitted with full provenance, as the first was.
        handle._handle_update(self._frame(self._call(n, rawInput={"targetFile": "/tmp/last"})))
        msg = self._perm(31, f"tc-{n}")
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is True
        assert event.tool_kind == "edit", "the tool_call's write-plane kind was carried"
        assert await handle._refuse_unretained_provenance(msg, event) is False
        assert sent == []

    def test_the_release_runs_after_the_tripwire_and_at_every_parse_site(self) -> None:
        """Pin by source: the main route releases inside the same loop as the
        spec-disabled tripwire and AFTER it (the tripwire reads ``raw_params``
        at the terminal), and each side-effects-only parse site releases right
        after its ``parse_session_update``."""
        import inspect
        import sys

        from kiro_crew.acp import session_handle

        src = inspect.getsource(session_handle.AcpSessionHandle._dispatch_events)
        trip = src.index("self._tripwire_spec_disabled_tool(ev, msg)")
        rel = src.index("self._release_settled_tool_call(")
        assert trip < rel < src.index("yield ev", trip)
        # The facade and every owner its code moved to, as one text.
        whole = "\n".join(
            inspect.getsource(sys.modules[name])
            for name in (session_handle.__name__, *session_handle._SESSION_HANDLE_OWNERS)
        )
        assert whole.count("self._release_settled_tool_call(") == 4
        for scope in ("upd, ssid)", "update, self._session_id)", "update, frame_sid)"):
            assert f"self._release_settled_tool_call({scope}" in whole, scope

    def test_a_native_child_s_terminal_releases_its_own_rows(self) -> None:
        """A child-routed frame writes the shared stores under the child's scope
        and re-tags only calls and text, so its terminal never reaches the main
        route's release: the child arm releases itself, and only its own row."""
        from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY

        handle, _ = self._handle()
        handle._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
        child = JsonRpcMessage(
            method="session/update",
            params={"sessionId": "child-1", "update": self._call(7, rawInput={"path": "/tmp/c"})},
        )
        out = handle._handle_update(child)
        assert [ev.kind for ev in out] == [EVENT_SUBAGENT_ACTIVITY]
        assert "child-1|tc-7" in handle._tool_call_raw_params
        assert "s-1|tc-0" in handle._tool_call_raw_params
        settled = JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "child-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-7",
                    "status": "completed",
                },
            },
        )
        assert handle._handle_update(settled) == []
        assert "child-1|tc-7" not in handle._tool_call_raw_params
        assert "child-1|tc-7" not in handle._tool_call_is_shell
        assert "child-1|tc-7" not in handle._tool_call_kind
        # The parent's in-flight row is untouched by the child's terminal.
        assert "s-1|tc-0" in handle._tool_call_raw_params

    def test_the_overflow_record_is_cleared_with_the_sibling_caches(self) -> None:
        """The per-turn clear is inline in ``_run_turn``; pin by source: wherever
        ``_tool_call_kind`` is cleared, ``_tool_call_cache_overflow`` is too."""
        import ast
        import inspect
        import sys

        from kiro_crew.acp import session_handle

        # The facade and every owner its code moved to, parsed as one module.
        tree = ast.parse(
            "\n".join(
                inspect.getsource(sys.modules[name])
                for name in (session_handle.__name__, *session_handle._SESSION_HANDLE_OWNERS)
            )
        )
        cleared_in: dict[str, set[str]] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "clear"
                    and isinstance(node.func.value, ast.Attribute)
                ):
                    cleared_in.setdefault(fn.name, set()).add(node.func.value.attr)
        owners = {fn for fn, names in cleared_in.items() if "_tool_call_kind" in names}
        assert owners, "the kind cache is cleared somewhere"
        for fn in owners:
            assert "_tool_call_cache_overflow" in cleared_in[fn], fn

    def test_the_overflow_record_holds_fixed_size_digests_never_the_scope(self, caplog) -> None:
        """The scope is the frame's own ``sessionId``, unbounded and backend
        authored -- and the record exists to remember scopes whose frames were
        refused for size. Storing the text would retain what the admission
        refused, so the record stores a 64-character digest whatever the scope
        was, and the once-per-snapshot warning carries the digest's head only."""
        import logging

        from kiro_crew.acp import _dispatch
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_KEY_CHARS, overflow_scope_key

        huge_scope = "s" * (10 * 1024 * 1024)
        caches = self._caches()
        # A scope that pushes any key over the key bound is refused; the scope is
        # what the key was refused FOR, and it must not be what the record keeps.
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            parse_session_update(self._call(1), cache_scope=huge_scope, **caches)
        assert all(not c for c in self._dicts(caches))
        assert caches["cache_overflow"] == {overflow_scope_key(huge_scope)}
        (stored,) = caches["cache_overflow"]
        assert len(stored) == 64 and huge_scope[:TOOL_CALL_CACHE_MAX_KEY_CHARS] not in stored
        assert not hasattr(_dispatch, "_cache_overflow_reported"), "no process-global record"
        warned = [r.getMessage() for r in caplog.records if "refused a frame" in r.getMessage()]
        assert len(warned) == 1 and huge_scope[:TOOL_CALL_CACHE_MAX_KEY_CHARS] not in warned[0]
        # The digest is what the reader consults: the same scope reads as
        # overflowed, a different one does not.
        assert _dispatch.scope_overflowed(caches["cache_overflow"], huge_scope) is True
        assert _dispatch.scope_overflowed(caches["cache_overflow"], "other") is False

    def test_the_overflow_record_is_bounded_by_the_every_scope_marker(self) -> None:
        """A backend that refuses frames under a fresh sessionId each time would
        grow the record one digest per scope; past the shared entry cap the
        record takes the ``OVERFLOW_EVERY_SCOPE`` marker instead, so it never
        exceeds ``MAX_ENTRIES + 1`` entries, and every scope -- recorded or not
        -- then reads as overflowed (the fail-safe direction)."""
        from kiro_crew.acp._dispatch import (
            OVERFLOW_EVERY_SCOPE,
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_KEY_CHARS,
            scope_overflowed,
        )

        # Each frame is refused on the KEY bound (its scope alone exceeds it),
        # which costs nothing to render -- the payload bound is pinned by the
        # sibling tests, and refusing 259 frames on a multi-megabyte rendering
        # each was what made this test exceed a loaded shard's per-test budget.
        def scope(i: int) -> str:
            return f"scope-{i}-" + "s" * TOOL_CALL_CACHE_MAX_KEY_CHARS

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 3):
            parse_session_update(self._call(i), cache_scope=scope(i), **caches)
        record = caches["cache_overflow"]
        assert len(record) == TOOL_CALL_CACHE_MAX_ENTRIES + 1
        assert OVERFLOW_EVERY_SCOPE in record
        assert scope_overflowed(record, scope(0)) is True
        assert scope_overflowed(record, scope(TOOL_CALL_CACHE_MAX_ENTRIES + 2)) is True
        assert scope_overflowed(record, "never-refused") is True
        # Below the cap an unrecorded scope is NOT overflowed.
        small = self._caches()
        parse_session_update(self._call(0), cache_scope=scope(0), **small)
        assert scope_overflowed(small["cache_overflow"], scope(0)) is True
        assert scope_overflowed(small["cache_overflow"], "b") is False

    def test_a_refused_refinement_evicts_the_id_from_every_cache(self) -> None:
        """A held id's earlier frame wrote params, a diff path, an identity and
        a kind; a refinement of that id refused for size must not leave them in
        place -- the permission request would be judged against the SUPERSEDED
        target and marked trusted. The refusal evicts the id from every sibling
        store, the ones a refinement writes and the ones it does not."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        caches = self._caches()
        first = self._call(
            1,
            rawInput={"path": "/tmp/allowed.txt", "text": "x"},
            _meta={"kiro": {"toolId": "str_replace"}},
            content=[{"type": "diff", "path": "/tmp/allowed.txt", "oldText": "a", "newText": "b"}],
        )
        parse_session_update(first, **caches)
        assert caches["raw_params_cache"]["tc-1"]["path"] == "/tmp/allowed.txt"
        assert caches["diff_path_cache"]["tc-1"] == "/tmp/allowed.txt"
        assert caches["tool_kind_cache"]["tc-1"] == "edit"
        held_before = {k: dict(v) for k, v in caches.items() if isinstance(v, dict)}
        assert any("tc-1" in v for v in held_before.values())
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "title": "Write File",
                "rawInput": {
                    "path": "/etc/passwd",
                    "text": "z" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1),
                },
            },
            **caches,
        )
        assert all("tc-1" not in c for c in self._dicts(caches)), "a stale row survived"
        assert caches["cache_overflow"] == self._over("")
        # The permission event for the id now carries no trusted provenance.
        msg = JsonRpcMessage(
            id=31,
            method="session/request_permission",
            params={
                "sessionId": "",
                "toolCall": {"toolCallId": "tc-1", "status": "pending", "title": "Write File"},
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        perm_caches = {k: v for k, v in caches.items() if k != "cache_overflow"}
        event, _ = build_permission_event(msg, **perm_caches)
        assert event.raw_params_trusted is False
        assert event.tool_kind == ""

    @pytest.mark.asyncio
    async def test_the_handle_refuses_a_request_whose_refinement_was_refused(self) -> None:
        """GPT's scenario end to end: a benign cached KAS write, then an
        oversized same-id refinement that changes the target. The handle must
        refuse the permission request rather than trust the stale target."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        handle, sent = self._handle()
        handle._handle_update(
            self._frame(
                self._call(
                    5,
                    rawInput={"path": "/tmp/allowed.txt", "text": "x"},
                    _meta={"kiro": {"toolId": "str_replace"}},
                )
            )
        )
        pre = handle._build_permission_event(self._perm(40, "tc-5"))
        assert pre is not None and pre.raw_params_trusted is True
        handle._handle_update(
            self._frame(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-5",
                    "title": "Write File",
                    "rawInput": {
                        "path": "/etc/passwd",
                        "text": "z" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1),
                    },
                }
            )
        )
        msg = self._perm(41, "tc-5")
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is False
        assert await handle._refuse_unretained_provenance(msg, event) is True
        assert sent == [(41, {"outcome": {"outcome": "selected", "optionId": "reject"}})]
