"""The wire does not carry a file-change CONTENT PAIR when a diff will do.

Measured on a real 9,611-message session: 1.0% of rows carried 69% of the corpus
bytes, and those bytes were entirely `meta.file_changes` before/after pairs
(33-200 KB per side) while the rows' own `content` was 0-2 KB. Server-side CPU for
one page was ~305 ms of a 2.6 s wait, so the remainder was bytes on the wire --
which makes payload the only lever on the term that dominates. Sending the diff
instead of the pair measured 130x smaller on the 40 heaviest pairs for 3.6 ms of
CPU each.

It is geometrically free: the card these entries feed renders COLLAPSED, and a
collapsed row's height is its file-list header, which does not depend on the
snapshots.
"""

from __future__ import annotations

import json

from kiro_crew.dashboard.chat_utils import (
    _PATCH_INSTEAD_OF_PAIR_BYTES,
    _prepare_messages,
    _shrink_file_changes_for_wire,
)


def _entry(path: str, before: str, after: str) -> dict:
    return {"path": path, "before": before, "after": after}


class TestOversizedPairBecomesAPatch:
    def test_a_large_pair_travels_as_a_patch_and_the_pair_is_dropped(self) -> None:
        body = "".join(f"line {i}\n" for i in range(4000))
        changed = body.replace("line 2000\n", "line 2000 EDITED\n")
        meta = {"file_changes": [_entry("a/b.py", body, changed)]}

        out = _shrink_file_changes_for_wire(meta)
        row = out["file_changes"][0]

        # The pair is what made the row heavy; it must not reach the client.
        assert "before" not in row
        assert "after" not in row
        assert row["path"] == "a/b.py"
        # The patch has to carry the change, not merely exist.
        assert "line 2000 EDITED" in row["patch"]
        # And it has to be the point: far smaller than what it replaced.
        assert len(row["patch"]) * 10 < len(body) + len(changed)

    def test_the_true_line_numbers_survive(self) -> None:
        # A windowed content pair could not carry these: both windows would start
        # at line 1. The hunk header is the only place the real position lives.
        body = "".join(f"line {i}\n" for i in range(4000))
        changed = body.replace("line 3500\n", "line 3500 EDITED\n")
        out = _shrink_file_changes_for_wire({"file_changes": [_entry("x.py", body, changed)]})
        assert "@@ -34" in out["file_changes"][0]["patch"]

    def test_a_small_pair_is_left_alone(self) -> None:
        # Below the threshold the pair is already cheap, and converting would
        # spend CPU to save nothing while losing full context on expand.
        meta = {"file_changes": [_entry("s.py", "a\n", "b\n")]}
        out = _shrink_file_changes_for_wire(meta)
        assert out is meta, "an ordinary message must not even be copied"
        assert out["file_changes"][0]["before"] == "a\n"

    def test_the_threshold_is_crossed_by_the_SUM_of_both_sides(self) -> None:
        half = "x\n" * (_PATCH_INSTEAD_OF_PAIR_BYTES // 4)
        # Each side alone is under the threshold; together they are over it, and
        # it is the pair that travels, so the pair is what must be measured.
        assert len(half) < _PATCH_INSTEAD_OF_PAIR_BYTES
        out = _shrink_file_changes_for_wire({"file_changes": [_entry("h.py", half, half + "y\n")]})
        assert "before" not in out["file_changes"][0]

    def test_an_existing_patch_is_not_recomputed(self) -> None:
        # The writer attaches a patch exactly when it judged the pair unusable
        # (both sides cut from the file's start, so byte-identical). Recomputing
        # from that pair would replace a good diff with the empty diff of two
        # identical prefixes.
        same = "z\n" * 5000
        entry = _entry("cap.py", same, same)
        entry["patch"] = "@@ -1,1 +1,1 @@\n-real\n+change\n"
        out = _shrink_file_changes_for_wire({"file_changes": [entry]})
        assert out["file_changes"][0]["patch"] == "@@ -1,1 +1,1 @@\n-real\n+change\n"
        assert "before" not in out["file_changes"][0]

    def test_the_input_meta_is_not_mutated(self) -> None:
        # The stored dict is shared with the slot's in-memory history; shrinking
        # for one response must not strip the snapshots the session still holds.
        body = "q\n" * 6000
        meta = {"file_changes": [_entry("m.py", body, body + "r\n")]}
        _shrink_file_changes_for_wire(meta)
        assert meta["file_changes"][0]["before"] == body

    def test_non_dict_entries_and_absent_key_pass_through(self) -> None:
        assert _shrink_file_changes_for_wire({}) == {}
        odd = {"file_changes": ["not-a-dict", {"path": "p"}]}
        assert _shrink_file_changes_for_wire(odd) is odd


class TestTheWirePreparationActuallyAppliesIt:
    """The helper being correct is not the same as it being CALLED.

    `_prepare_messages` is the one place a slot-detail response is built, and it
    has two meta branches (a parsed `cls` meta and a stored one). A shrink wired
    into only one of them would halve the saving on a corpus nobody would think to
    re-measure, so the guard goes through the public entry point rather than the
    helper.
    """

    def test_a_stored_oversized_pair_reaches_the_wire_as_a_patch(self) -> None:
        body = "".join(f"line {i}\n" for i in range(4000))
        changed = body.replace("line 1500\n", "line 1500 EDITED\n")
        msgs = [
            {
                "role": "assistant",
                "content": "did some work",
                "cls": "msg msg-a",
                "meta": {"mid": "m1", "file_changes": [_entry("big.py", body, changed)]},
            }
        ]

        out = _prepare_messages(msgs, False, live_child="")

        rows = [m for m in out if isinstance(m.get("meta"), dict)]
        assert rows, "the assistant row must survive preparation"
        entry = rows[-1]["meta"]["file_changes"][0]
        assert "before" not in entry and "after" not in entry
        assert "line 1500 EDITED" in entry["patch"]

    def test_the_patch_inherits_the_credential_scrub(self) -> None:
        # Redaction runs BEFORE the shrink, so the patch is computed from already
        # scrubbed text and needs no pass of its own. Pinning it here is what makes
        # that ordering load-bearing rather than incidental: reversing the two would
        # ship an unredacted diff of redacted files.
        secret = "AKIAIOSFODNN7EXAMPLE"
        body = "".join(f"line {i}\n" for i in range(4000))
        changed = body.replace("line 900\n", f"line 900 {secret}\n")
        msgs = [
            {
                "role": "assistant",
                "content": "x",
                "cls": "msg msg-a",
                "meta": {"mid": "m2", "file_changes": [_entry("leak.py", body, changed)]},
            }
        ]

        out = _prepare_messages(msgs, False, live_child="")
        blob = json.dumps(out)
        assert secret not in blob

    def test_an_ordinary_message_is_unaffected(self) -> None:
        msgs = [{"role": "user", "content": "hello", "cls": "msg msg-u"}]
        out = _prepare_messages(msgs, False, live_child="")
        assert out[-1]["content"] == "hello"
