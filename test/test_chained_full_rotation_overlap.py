"""The rotated+live concatenation must not serve a row twice.

Rotation archives the dropped lines FIRST and rewrites the live file's head
SECOND, and that order is deliberate: archiving is a precondition, so it fails
closed rather than deleting the only copy of those rows
(``history_rewrite._maybe_rotate``). The cost of that choice is a window in which
both files hold the same rows, and a crash or a kill inside it makes the
duplication permanent on disk.

``read_messages_chained_full`` is the pagination corpus — the index space
``before``/``next_before`` cursors and fork indices resolve against — so a
duplicated row is not cosmetic: it shifts every index above it, and a fork taken
at a rendered row lands on a different message.
"""

from __future__ import annotations

from kiro_crew.history_projection import drop_persisted_tail_prefix


def _m(mid: str, content: str = "", ts: str = "2026-09-04T00:00:00Z") -> dict:
    return {"ts": ts, "role": "user", "content": content or mid, "meta": {"mid": mid}}


def test_a_crash_window_duplication_is_served_once() -> None:
    # The archive holds rows 1-3; the live file still holds 2-3 because the
    # rewrite never landed. The corpus must read 1,2,3 — not 1,2,3,2,3.
    rotated = [_m("1"), _m("2"), _m("3")]
    live = [_m("2"), _m("3"), _m("4")]
    assert drop_persisted_tail_prefix(rotated, live) == [_m("4")]


def test_a_clean_rotation_drops_nothing() -> None:
    # The normal case: the rewrite landed, so the live file starts after the
    # archive ends. Nothing may be removed here or the transcript loses rows.
    rotated = [_m("1"), _m("2")]
    live = [_m("3"), _m("4")]
    assert drop_persisted_tail_prefix(rotated, live) == live


def test_only_a_prefix_overlap_counts() -> None:
    # Both producers of this shape persist a PREFIX of the duplicated block, never
    # an interior slice, so an interior coincidence must NOT be treated as overlap.
    rotated = [_m("1"), _m("2"), _m("3")]
    live = [_m("9"), _m("2"), _m("3")]
    assert drop_persisted_tail_prefix(rotated, live) == live


def test_rows_without_a_stable_id_need_ts_role_and_content() -> None:
    # A match DELETES a row, so (ts, role) alone is not enough: two distinct rows
    # can share a second and a speaker.
    a = {"ts": "2026-09-04T00:00:00Z", "role": "user", "content": "first"}
    b = {"ts": "2026-09-04T00:00:00Z", "role": "user", "content": "second"}
    assert drop_persisted_tail_prefix([a], [b]) == [b]
    assert drop_persisted_tail_prefix([a], [dict(a)]) == []


def test_the_identity_rule_is_shared_with_the_fork_path() -> None:
    # The fork rebuild reaches for the same rule through its own module. If these
    # ever diverge, one of the two duplication windows loses its guard silently.
    from kiro_crew.dashboard import chat_fork

    rotated = [_m("1"), _m("2")]
    live = [_m("2"), _m("3")]
    assert chat_fork.drop_persisted_tail_prefix(rotated, live) == drop_persisted_tail_prefix(
        rotated, live
    )


def test_read_messages_chained_full_applies_the_rule() -> None:
    """The APPLICATION, not just the rule.

    Pinning the predicate alone leaves the concatenation site free to stop calling
    it — which is exactly the state this fix found. Both readers are stubbed to
    report the crash-window overlap, so the assertion is about what the corpus
    serves rather than about disk layout.
    """
    from kiro_crew.history_projection import TranscriptReadProjection

    rotated = [_m("1"), _m("2"), _m("3")]
    live = [_m("2"), _m("3"), _m("4")]

    class _Log:
        _lock = __import__("threading").RLock()
        _tab_id_index: dict | None = {}

        def get_metadata(self, key: str) -> dict:
            return {}

        def _read_messages(self, key: str) -> list[dict]:
            return list(live)

    proj = TranscriptReadProjection.__new__(TranscriptReadProjection)
    proj._log = _Log()  # type: ignore[attr-defined]
    proj.read_rotated_messages = lambda key: list(rotated)  # type: ignore[method-assign]

    got = proj.read_messages_chained_full("k")
    mids = [row["meta"]["mid"] for row in got]
    assert mids == ["1", "2", "3", "4"], mids
    assert len(mids) == len(set(mids)), f"a row is served twice: {mids}"


def test_the_fork_flat_prepend_branch_is_also_guarded() -> None:
    """The SECOND concatenation of the same shape, reached by a different branch.

    `read_messages_chained_full`'s own guard does not cover this one: the fork
    handler's flat prepend runs when that read was NOT used (`_rebuilt` false), so
    fixing only the function left the fork corpus exposed to the identical crash
    window. Reported by review after the first fix landed.

    Source-scanned because the branch sits inside an aiohttp handler whose
    surrounding code needs a slot, a snapshot loop and a request; the rule it must
    call is exercised behaviourally by the cases above.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "chat_fork.py"
    body = src.read_text(encoding="utf-8")
    assert "_rotated_head + drop_persisted_tail_prefix(" in body, (
        "the fork handler's flat prepend must dedupe the live prefix against the "
        "rotated head, or a crash-window duplication shifts every fork index"
    )
    assert "all_messages = _rotated_head + all_messages" not in body
