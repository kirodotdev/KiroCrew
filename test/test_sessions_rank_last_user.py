"""The recent-sessions list ranks by the last HUMAN turn, not by file mtime.

``st_mtime`` records the last WRITE, and a write is machine activity: a cron wake,
a monitor loop, a subagent turn, an auto-title refresh and any bulk maintenance
pass over the sessions directory all advance it although nobody read the session.
With ten slots that fills the list with sessions nobody is reading -- forks of one
exploratory thread, a cron session, a five-message dead session -- and pushes a
long workstream past the cut-off where it can never render.

A bulk pass makes the key's fragility exact. Rewriting the metadata line of every
transcript advances every mtime, and a pass that visits them
newest-activity-first hands the freshest session the OLDEST new stamp, which
inverts the list outright rather than merely perturbing it. Sub-second mtimes are
distinct, so this is no tie-break artifact: the sort behaves exactly as written
over a signal that has been overwritten with "when the pass reached this file".

These tests pin both halves of the remedy: the collector ranks on a recorded
human turn and falls back to mtime only for a transcript that has none, and the
dashboard slot save is what records that turn.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from chat_test_helpers import _make_state

from kiro_crew.config.loader import _build_slack_config
from kiro_crew.config.sections import SlackConfig
from kiro_crew.dashboard.chat_persistence import (
    _META_LAST_USER_AT,
    _newest_human_turn_ts,
    _save_slot_to_history,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.history import HUMAN_TURN_META_KEY, SLOT_OWNED_META_KEYS
from kiro_crew.messaging.sessions_view import (
    _SESSIONS_DEFAULT_LIMIT,
    _collect_recent_sessions,
)
from kiro_crew.slack.sessions_view import (
    _SLACK_MESSAGE_BLOCK_LIMIT,
    MAX_MESSAGE_SESSION_ROWS,
    _build_sessions_blocks,
    _message_surface_limit,
)

#: Repository root, derived from this file so the doc-parity tests read the
#: committed copies rather than a path assembled from the working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _iso(epoch: float) -> str:
    """An offset-aware transcript stamp for *epoch*, the format writers emit."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


def _write_transcript(
    path: Path,
    *,
    title: str,
    last_user_at: str | None = None,
    rows: tuple[tuple[str, str], ...] = (("user", "hi"),),
) -> None:
    """A session transcript with a metadata line, optionally carrying the stamp."""
    meta: dict = {"_type": "metadata", "title": title}
    if last_user_at is not None:
        meta[_META_LAST_USER_AT] = last_user_at
    lines = [json.dumps(meta)]
    for role, content in rows:
        lines.append(json.dumps({"role": role, "content": content}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


def _sessions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "sessions"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# The ranking key
# ---------------------------------------------------------------------------


class TestRanksByHumanActivity:
    def test_a_recorded_human_turn_outranks_a_newer_file_write(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        stale = d / "dashboard_stale.jsonl"
        fresh = d / "dashboard_fresh.jsonl"
        _write_transcript(stale, title="stale", last_user_at=_iso(now - 7200))
        _write_transcript(fresh, title="fresh", last_user_at=_iso(now - 60))
        # The machine touched the session nobody is reading most recently.
        _set_mtime(stale, now)
        _set_mtime(fresh, now - 3600)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["fresh", "stale"]

    def test_a_rewrite_pass_in_activity_order_does_not_invert_the_list(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        # Real activity order: newest first.
        order = ["workstream", "branch", "may-thread"]
        for offset, title in enumerate(order):
            _write_transcript(
                d / f"dashboard_{title}.jsonl",
                title=title,
                last_user_at=_iso(now - 60 - offset * 3600),
            )
        # The cleanup pass visits them newest-activity-first, so the freshest
        # session is written FIRST and ends up with the oldest mtime.
        for visited, title in enumerate(order):
            _set_mtime(d / f"dashboard_{title}.jsonl", now + visited)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == order

    def test_an_unstamped_transcript_still_ranks_by_mtime(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        old = d / "dashboard_old.jsonl"
        new = d / "dashboard_new.jsonl"
        _write_transcript(old, title="old")
        _write_transcript(new, title="new")
        _set_mtime(old, now - 3600)
        _set_mtime(new, now)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["new", "old"]

    def test_a_stamped_and_an_unstamped_session_share_one_timeline(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        stamped = d / "dashboard_stamped.jsonl"
        legacy = d / "dashboard_legacy.jsonl"
        _write_transcript(stamped, title="stamped", last_user_at=_iso(now - 7200))
        _write_transcript(legacy, title="legacy")
        _set_mtime(stamped, now)
        _set_mtime(legacy, now - 60)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        # The legacy file's mtime is newer than the stamped file's human turn, so
        # it wins. The two populations interleave rather than one preceding the
        # other wholesale -- an install mid-migration is not sorted into blocks.
        assert [r["title"] for r in rows] == ["legacy", "stamped"]

    def test_an_unparseable_stamp_falls_back_to_mtime(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        corrupt = d / "dashboard_corrupt.jsonl"
        real = d / "dashboard_real.jsonl"
        _write_transcript(corrupt, title="corrupt", last_user_at="whenever")
        _write_transcript(real, title="real", last_user_at=_iso(now - 3600))
        # The corrupt file's mtime is the NEWER signal, so a working fallback
        # puts it first. Ranking on transcript_sort_key's seconds without
        # checking its bucket would instead pin it to the 0.0 those seconds carry
        # for an unparseable value and bury it last -- which is why the mtime of
        # this file has to beat the other file's real stamp for the test to tell
        # the two behaviours apart at all.
        _set_mtime(corrupt, now)
        _set_mtime(real, now - 7200)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["corrupt", "real"]

    def test_an_unparseable_stamp_does_not_outrank_a_later_real_turn(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        corrupt = d / "dashboard_corrupt.jsonl"
        real = d / "dashboard_real.jsonl"
        _write_transcript(corrupt, title="corrupt", last_user_at="whenever")
        _write_transcript(real, title="real", last_user_at=_iso(now - 60))
        _set_mtime(corrupt, now - 7200)
        _set_mtime(real, now - 3600)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["real", "corrupt"]

    def test_the_row_shape_is_unchanged(self, tmp_path):
        d = _sessions_dir(tmp_path)
        _write_transcript(d / "dashboard_one.jsonl", title="one", last_user_at=_iso(time.time()))

        rows = _collect_recent_sessions(None, sessions_dir=d)

        # Ranking is internal. A row carries what it always carried, so no
        # renderer has to learn anything and `mtime` keeps meaning the file's own
        # timestamp rather than quietly becoming the rank.
        assert set(rows[0]) == {"key", "title", "agent", "mtime", "active", "kind", "msgs"}

    def test_a_transcript_that_is_not_utf8_does_not_disable_the_list(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        # The rank read touches line 0 of EVERY candidate before the limit
        # break, so one file of invalid bytes anywhere in the directory could
        # raise a UnicodeDecodeError out through the collector. Each surface
        # wraps the call in a try that turns any exception into "Sessions
        # unavailable" plus an error audit, and the state persists on every
        # scan until someone deletes the file, so a single corrupt transcript
        # would take out the list on every Slack surface at once.
        (d / "dashboard_corrupt.jsonl").write_bytes(b'{"_type": "metadata", "title": "\xff\xfe"}\n')
        _write_transcript(d / "dashboard_real.jsonl", title="real", last_user_at=_iso(now - 60))
        # Ranked FIRST by mtime, so it is inside the read window too, not just
        # the pre-scan.
        _set_mtime(d / "dashboard_corrupt.jsonl", now + 60)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["real"]

    def test_a_corrupt_transcript_costs_only_its_own_row(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        (d / "dashboard_bad.jsonl").write_bytes(b"\xff\xfe not text at all\n")
        for i in range(3):
            _write_transcript(
                d / f"dashboard_ok-{i}.jsonl", title=f"ok {i}", last_user_at=_iso(now - i)
            )
        _set_mtime(d / "dashboard_bad.jsonl", now - 30)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["ok 0", "ok 1", "ok 2"]

    def test_a_valid_header_over_a_corrupt_body_costs_only_its_own_row(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        # Line 0 decodes, so the rank read succeeds and this file is ranked into
        # the read window -- where the whole-transcript read then hits the bad
        # bytes. That is a SECOND decode site, and the pre-scan's guard cannot
        # cover it.
        (d / "dashboard_half.jsonl").write_bytes(
            b'{"_type": "metadata", "title": "half"}\n{"role": "user", "content": "\xff\xfe"}\n'
        )
        _write_transcript(d / "dashboard_real.jsonl", title="real", last_user_at=_iso(now - 60))
        _set_mtime(d / "dashboard_half.jsonl", now + 60)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["real"]

    def test_a_boundary_stamp_that_parses_falls_back_instead_of_raising(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        # These PARSE, so the unparseable path never sees them, and resolving a
        # naive value with astimezone() then raises: measured as
        # "ValueError: year 0 is out of range" and "year 10000 is out of range".
        # Unguarded, one such file aborts the whole scan.
        for name, stamp in (("low", "0001-01-01T00:00:00"), ("high", "9999-12-31T23:59:59")):
            _write_transcript(d / f"dashboard_{name}.jsonl", title=name, last_user_at=stamp)
            _set_mtime(d / f"dashboard_{name}.jsonl", now - 7200)
        _write_transcript(d / "dashboard_real.jsonl", title="real", last_user_at=_iso(now - 60))

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert rows[0]["title"] == "real"
        assert {r["title"] for r in rows} == {"real", "low", "high"}

    def test_a_stamp_on_a_non_metadata_line_is_not_read_as_the_rank(self, tmp_path):
        d = _sessions_dir(tmp_path)
        now = time.time()
        # A message row that merely CONTAINS the field name must not be mistaken
        # for the metadata line: line 0 is the only line the pre-scan reads, and
        # it is a metadata line by construction.
        (d / "dashboard_spoof.jsonl").write_text(
            json.dumps({"role": "user", "content": "hi", _META_LAST_USER_AT: _iso(now)}) + "\n",
            encoding="utf-8",
        )
        _write_transcript(d / "dashboard_real.jsonl", title="real", last_user_at=_iso(now - 60))
        _set_mtime(d / "dashboard_spoof.jsonl", now - 7200)
        _set_mtime(d / "dashboard_real.jsonl", now - 3600)

        rows = _collect_recent_sessions(None, sessions_dir=d)

        assert [r["title"] for r in rows] == ["real", "Dashboard spoof"]

    def test_a_limit_below_one_falls_back_to_the_default(self, tmp_path):
        d = _sessions_dir(tmp_path)
        for i in range(3):
            _write_transcript(d / f"dashboard_chat-{i}.jsonl", title=f"chat {i}")

        # A hand-edited slack.sessions_limit of 0 or -1 would otherwise break the
        # read loop on its first iteration and render an empty list forever.
        assert len(_collect_recent_sessions(None, sessions_dir=d, limit=0)) == 3
        assert len(_collect_recent_sessions(None, sessions_dir=d, limit=-5)) == 3

    def test_a_non_integer_limit_does_not_raise_into_the_surface(self, tmp_path):
        d = _sessions_dir(tmp_path)
        _write_transcript(d / "dashboard_chat-1.jsonl", title="one")

        # Each Slack surface calls the collector inside a try that turns any
        # exception into "Sessions unavailable" plus an error audit, so a bad
        # config value must degrade to the default rather than raise.
        assert len(_collect_recent_sessions(None, sessions_dir=d, limit=None)) == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


def _human(ts: str, content: str = "typed") -> dict:
    """A row shaped like the one a person's send produces."""
    return {"role": "user", "content": content, "ts": ts, "meta": {HUMAN_TURN_META_KEY: True}}


class TestNewestHumanTurn:
    def test_picks_the_latest_human_row(self):
        rows = [
            _human(_iso(1_000_000), "first"),
            {"role": "assistant", "content": "reply", "ts": _iso(1_000_500)},
            _human(_iso(1_000_200), "second"),
        ]
        assert _newest_human_turn_ts(rows) == _iso(1_000_200)

    def test_ignores_a_machine_injection(self):
        rows = [
            _human(_iso(1_000_000), "mine"),
            {"role": "inject", "content": "cron result", "ts": _iso(1_000_900)},
        ]
        assert _newest_human_turn_ts(rows) == _iso(1_000_000)

    def test_ignores_a_gateway_driven_prompt(self):
        # _ChatSlot.enqueue_or_run_prompt appends ("user", prompt, "msg msg-u")
        # for an Issue Radar wake -- identical in role AND presentation class to a
        # typed message, so only the absent marker tells them apart. Counting it
        # would let a background session displace the human-active ones, which is
        # the exact failure this change exists to remove.
        rows = [
            _human(_iso(1_000_000), "mine"),
            {"role": "user", "content": "wake", "cls": "msg msg-u", "ts": _iso(1_000_900)},
        ]
        assert _newest_human_turn_ts(rows) == _iso(1_000_000)

    def test_ignores_a_marker_that_is_not_true(self):
        rows = [
            _human(_iso(1_000_000), "mine"),
            {
                "role": "user",
                "content": "spoofed",
                "ts": _iso(1_000_900),
                "meta": {HUMAN_TURN_META_KEY: "yes"},
            },
        ]
        assert _newest_human_turn_ts(rows) == _iso(1_000_000)

    def test_no_human_row_yields_empty(self):
        rows = [{"role": "assistant", "content": "hi", "ts": _iso(1_000_000)}]
        assert _newest_human_turn_ts(rows) == ""

    def test_an_unparseable_row_stamp_is_not_ranked(self):
        rows = [_human(_iso(1_000_000), "real"), _human("whenever", "corrupt")]
        assert _newest_human_turn_ts(rows) == _iso(1_000_000)


class TestSlotSaveRecordsHumanActivity:
    def test_the_save_stamps_the_users_own_turn(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._titled = True
        slot.append("user", "kick it off", "msg msg-u", meta={HUMAN_TURN_META_KEY: True})
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)

        key = slot_history_key(slot)
        meta = state.conversation_log._read_metadata(key)
        rows = state.conversation_log.read_messages(key)
        user_ts = [r["ts"] for r in rows if r.get("role") == "user"]

        assert meta[_META_LAST_USER_AT] == user_ts[-1]

    def test_an_assistant_only_session_is_not_stamped(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-2")
        slot._titled = True
        slot.append("assistant", "unprompted", "msg msg-a")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)

        meta = state.conversation_log._read_metadata(slot_history_key(slot))
        assert _META_LAST_USER_AT not in meta

    def test_the_stamp_only_moves_forward(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-3")
        slot._titled = True
        slot.append("user", "an older turn", "msg msg-u", meta={HUMAN_TURN_META_KEY: True})
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)
        key = slot_history_key(slot)

        # Something already recorded a LATER turn than this window holds -- a
        # rewind, a fork, or a window that has scrolled past it. The save must
        # not walk the stamp backwards.
        ahead = _iso(time.time() + 3600)
        state.conversation_log.update_metadata(key, {_META_LAST_USER_AT: ahead})
        _save_slot_to_history(state, slot, closed=False)

        assert state.conversation_log._read_metadata(key)[_META_LAST_USER_AT] == ahead

    def test_a_gateway_driven_prompt_does_not_stamp(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-4")
        slot._titled = True
        # Exactly what _ChatSlot.enqueue_or_run_prompt appends for a wake.
        slot.append("user", "wake prompt", "msg msg-u")
        slot.drain()
        _save_slot_to_history(state, slot, closed=False)

        meta = state.conversation_log._read_metadata(slot_history_key(slot))
        assert _META_LAST_USER_AT not in meta

    def test_the_field_is_not_slot_owned(self):
        # An OWNED key's absence erases. The window this save serializes is
        # bounded, so a save whose window has scrolled past the last user row
        # derives nothing -- and if the field were owned, that save would wipe a
        # real turn and drop the session off the list. Unowned means
        # carry_unowned_metadata keeps the stored value instead.
        assert _META_LAST_USER_AT not in SLOT_OWNED_META_KEYS

    # ---------------------------------------------------------------------------
    # The configurable limit
    # ---------------------------------------------------------------------------

    def test_a_boundary_row_stamp_costs_only_itself(self):
        # Same hazard on the write side, and worse: an unguarded raise here aborts
        # the SLOT SAVE, not just one row's stamp. The good row must still win.
        rows = [_human(_iso(1_000_000), "real"), _human("9999-12-31T23:59:59", "boundary")]
        assert _newest_human_turn_ts(rows) == _iso(1_000_000)

    def test_only_boundary_row_stamps_yields_empty(self):
        assert _newest_human_turn_ts([_human("0001-01-01T00:00:00")]) == ""


class TestSessionsLimitConfig:
    def test_the_default_matches_the_collectors_own(self):
        assert SlackConfig().sessions_limit == _SESSIONS_DEFAULT_LIMIT

    def test_the_loader_reads_the_key(self):
        assert _build_slack_config({"sessions_limit": 40}).sessions_limit == 40

    def test_the_loader_coerces_a_bad_value(self):
        assert _build_slack_config({"sessions_limit": "lots"}).sessions_limit == 10

    def test_the_spec_quotes_the_production_default(self):
        # The spec states the default in prose. Derived from the dataclass here,
        # not typed twice: a hand-written number would go stale the moment the
        # default moved and the drift would be invisible.
        spec = _REPO_ROOT / "docs" / "system-specs" / "modules" / "slack-gateway.md"
        text = spec.read_text(encoding="utf-8")
        assert f"default {SlackConfig().sessions_limit}" in text

    def test_the_committed_baseline_carries_the_production_default(self):
        baseline = json.loads((_REPO_ROOT / "config-baseline.json").read_text(encoding="utf-8"))
        slack = [e for e in baseline["entries"] if e["path"] == "slack"]
        assert slack, "the baseline has no slack section"
        assert slack[0]["defaultValue"]["sessions_limit"] == SlackConfig().sessions_limit


class TestSlackMessageBlockCeiling:
    """A configured limit cannot exceed what one Slack message can carry."""

    @staticmethod
    def _rows(n: int) -> list[dict]:
        return [
            {
                "key": f"dashboard:chat-{i}",
                "title": f"session {i}",
                "agent": "kirocrew",
                "mtime": 0.0,
                "active": False,
                "kind": "dashboard",
                "msgs": [{"role": "user", "content": "hi"}],
            }
            for i in range(n)
        ]

    def test_the_cap_is_the_largest_row_count_that_fits(self):
        # Measured against the real builder rather than asserted from arithmetic,
        # so the constant cannot drift away from the layout it is meant to track.
        at_cap = len(_build_sessions_blocks(self._rows(MAX_MESSAGE_SESSION_ROWS)))
        over_cap = len(_build_sessions_blocks(self._rows(MAX_MESSAGE_SESSION_ROWS + 1)))
        assert at_cap <= _SLACK_MESSAGE_BLOCK_LIMIT
        assert over_cap > _SLACK_MESSAGE_BLOCK_LIMIT

    def test_a_configured_limit_above_the_cap_is_clamped(self):
        # Slack rejects an over-budget payload WHOLE, so an unclamped 18 renders
        # nothing at all -- which reads as the feature being broken.
        assert _message_surface_limit(18) == MAX_MESSAGE_SESSION_ROWS
        assert _message_surface_limit(500) == MAX_MESSAGE_SESSION_ROWS

    def test_a_configured_limit_below_the_cap_is_untouched(self):
        assert _message_surface_limit(10) == 10
        assert _message_surface_limit(1) == 1

    def test_a_non_integer_limit_does_not_raise_into_the_surface(self):
        assert _message_surface_limit(None) == _SESSIONS_DEFAULT_LIMIT  # type: ignore[arg-type]
