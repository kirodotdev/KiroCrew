"""A channel-born transcript row carries a durable delivery identity (#5981).

The dashboard's merge keys on ``meta.mid`` and nothing else: ``isRedeliveredMessage``
drops a redelivered row by it, ``olderHeadAbovePage`` cuts the retained scrollback
head at it, and ``rowIdentities``/``tailNotInPage`` decide by it which prior rows a
page already carries. Every one of those DECLINES rather than guesses when the id is
absent or has changed -- so a row whose identity is not stable degrades all three at
once, and the same reply can render more than once in the dashboard's view.

Channel dispatchers persisted their rows with no ``mid``, so the row reached disk
with no ``meta``; each surface that materialized it
(``channel_slots._rebuild_window`` / ``refresh_channel_window``) then minted a FRESH
id, giving one logical row a different identity on every pass. The dispatcher is the
first and only place the row exists -- a channel turn runs on its own session, so
unlike the dashboard dual-writers there is no ``_ChatSlot.append`` to mint the id and
hand it back -- so it now mints its own.

Discord's ``mirrored`` branch is the one exception and is pinned separately below: it
DOES have a slot, so it must thread that slot's id into the durable write rather than
mint a second one. ``append_if_absent`` skips only a body-equal row carrying the SAME
mid, so an unrelated id can never match the copy the slot save landed and the turn
would be persisted twice -- a durable duplicate, worse than the display one this
change fixes.

Structured after ``test_channel_persist_agent_metadata.py`` (#2890, the same "every
channel omitted a kwarg on its persist writes" shape): the unbound ``_persist_turn``
is called with a minimal stand-in so no channel client has to be constructed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard.state import row_mid
from kiro_crew.history import ConversationLog

#: Every dispatcher sharing the two-append ``_persist_turn`` shape. Slack is absent
#: deliberately: its persist is a different topology (three write branches, off-loop
#: stamping) and is not covered by this change.
_CHANNELS = [
    ("weixin", "kiro_crew.weixin.transport_dispatch"),
    ("telegram", "kiro_crew.telegram.transport_dispatch"),
    ("discord", "kiro_crew.discord.transport_dispatch"),
    ("feishu", "kiro_crew.feishu.transport_dispatch"),
    ("imessage", "kiro_crew.imessage.transport_dispatch"),
    ("teams", "kiro_crew.teams.transport_dispatch"),
    ("webex", "kiro_crew.webex.transport_dispatch"),
    ("wecom", "kiro_crew.wecom.transport_dispatch"),
    ("whatsapp", "kiro_crew.whatsapp.transport_dispatch"),
]


@pytest.fixture
def dashboard_state(tmp_path):
    """DashboardState with mocked services and a real (empty) ConversationLog.

    ``channel_key_for_stem`` is pinned to the empty-session-map answer, mirroring
    ``test_channel_slots.py``: ``sessions`` is a MagicMock whose auto-created
    attributes are truthy, so without the pin a slot could silently bind to a Mock.
    """
    state = _make_state(tmp_path)
    state.sessions.channel_key_for_stem = lambda stem: ""
    return state


def _dispatcher_class(module):
    """The class in *module* that defines ``_persist_turn``."""
    for obj in vars(module).values():
        if isinstance(obj, type) and "_persist_turn" in vars(obj):
            return obj
    raise AssertionError(f"no _persist_turn owner in {module.__name__}")


def _persist(cls, host, key, user_text, reply_text, is_new):
    """Call ``_persist_turn`` across the two signatures in play.

    WhatsApp's takes no ``agent`` (it predates #2890's fix), so keywords alone
    cannot cover both shapes.
    """
    try:
        cls._persist_turn(host, key, user_text, reply_text, is_new, agent="kirocrew-research")
    except TypeError:
        cls._persist_turn(host, key, user_text, reply_text, is_new)


def _rows_on_disk(log: ConversationLog, key: str) -> list[dict]:
    """The raw JSONL message rows for *key*.

    Read straight off the file rather than through a reader helper: what is being
    pinned is what is DURABLE, and a reader that re-derived an id would hide the
    very regression this file exists to catch.
    """
    rows: list[dict] = []
    for line in log._path(key).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get("role"):
            rows.append(row)
    return rows


class TestMintRowMid:
    """The id format has ONE definition, shared with ``_ChatSlot.append``.

    ``mint_row_mid`` is imported inside each test on purpose: the rest of this file
    must still COLLECT against a tree without the helper, so the behavioural tests
    below fail on their assertions (the real regression) rather than the whole
    module erroring out on an ImportError.
    """

    def test_mints_the_shape_every_reader_matches(self) -> None:
        from kiro_crew.history import mint_row_mid

        mid = mint_row_mid()
        assert mid.startswith("m-")
        # "m-" + 16 hex: the same width _ChatSlot.append stamps. Readers match the
        # VALUE, so a second spelling would be invisible to all of them.
        assert len(mid) == 18
        assert all(c in "0123456789abcdef" for c in mid[2:])

    def test_ids_are_distinct(self) -> None:
        from kiro_crew.history import mint_row_mid

        # A collision makes a client DROP a real message, so distinctness is the
        # property that matters, not merely the format.
        assert len({mint_row_mid() for _ in range(512)}) == 512


@pytest.mark.parametrize("channel,mod_name", _CHANNELS)
def test_persist_turn_stamps_a_durable_mid_on_both_rows(channel, mod_name, tmp_path) -> None:
    """RED before the fix: each dispatcher forwarded no ``mid``, so disk had no meta."""
    module = __import__(mod_name, fromlist=["_"])
    log = ConversationLog(base_dir=tmp_path)
    host = SimpleNamespace(conv_log=log)
    key = f"{channel}:row-identity-test"

    _persist(_dispatcher_class(module), host, key, "hello", "world", True)

    rows = _rows_on_disk(log, key)
    assert [r["role"] for r in rows] == [
        "user",
        "assistant",
    ], f"{channel}: _persist_turn should write exactly the turn's two rows"
    mids = [row_mid(r) for r in rows]
    assert all(isinstance(m, str) and m for m in mids), (
        f"{channel}: a channel row reached disk with no meta.mid -- every later "
        f"materialization mints a fresh one, so the row has no stable identity and "
        f"the dashboard cannot recognise a redelivery of it"
    )
    assert mids[0] != mids[1], f"{channel}: the two rows of one turn must not share an id"


@pytest.mark.parametrize("channel,mod_name", _CHANNELS)
def test_persist_turn_ids_are_unique_across_turns(channel, mod_name, tmp_path) -> None:
    """A reused id makes a client DROP a real row, the opposite failure to a duplicate."""
    module = __import__(mod_name, fromlist=["_"])
    cls = _dispatcher_class(module)
    log = ConversationLog(base_dir=tmp_path)
    host = SimpleNamespace(conv_log=log)
    key = f"{channel}:row-identity-unique"

    _persist(cls, host, key, "first", "one", True)
    _persist(cls, host, key, "second", "two", False)

    mids = [row_mid(r) for r in _rows_on_disk(log, key)]
    assert len(mids) == 4
    assert len(set(mids)) == 4, f"{channel}: every persisted row needs its own id"


@pytest.mark.parametrize("channel,mod_name", _CHANNELS)
def test_persist_turn_still_writes_an_empty_reply(channel, mod_name, tmp_path) -> None:
    """The reply guard is unchanged: an empty reply persists the user row only."""
    module = __import__(mod_name, fromlist=["_"])
    log = ConversationLog(base_dir=tmp_path)
    host = SimpleNamespace(conv_log=log)
    key = f"{channel}:row-identity-noreply"

    _persist(_dispatcher_class(module), host, key, "hello", "", False)

    rows = _rows_on_disk(log, key)
    assert [r["role"] for r in rows] == ["user"]
    assert row_mid(rows[0])


class TestDiscordMirroredBranchStaysIdempotent:
    """The mirrored write must NOT mint a second identity for a row it already has.

    ``append_if_absent`` skips only a body-equal row carrying the SAME ``meta.mid``
    (``history.py``'s own contract). The mirrored branch runs after
    ``_mirror_turn_to_live_slot`` put the row in a live slot, whose save writes that
    window row under the slot-minted id. Minting a fresh id for the durable copy
    means the check can never match, so the turn lands on disk TWICE under two
    unrelated ids -- a durable duplicate no mid-keyed sweep can collapse.

    ``mirror_mids`` carries both facts: the ids, and (by being present at all)
    whether there was a slot to mirror into. There is deliberately no second
    ``mirrored`` flag for the two to disagree about.
    """

    @staticmethod
    def _discord_cls():
        module = __import__("kiro_crew.discord.transport_dispatch", fromlist=["_"])
        return _dispatcher_class(module)

    def test_mirrored_write_reuses_the_slot_minted_ids(self, tmp_path) -> None:
        cls = self._discord_cls()
        log = ConversationLog(base_dir=tmp_path)
        host = SimpleNamespace(conv_log=log)
        key = "discord:mirrored-threaded"

        cls._persist_turn(
            host,
            key,
            "hello",
            "world",
            False,
            agent=None,
            mirror_mids=("m-1111111111111111", "m-2222222222222222"),
        )

        assert [row_mid(r) for r in _rows_on_disk(log, key)] == [
            "m-1111111111111111",
            "m-2222222222222222",
        ], "the durable copy must carry the id the slot already stamped on its window row"

    def test_mirrored_write_is_a_no_op_when_the_slot_save_landed_it(self, tmp_path) -> None:
        """The race the branch exists for: same row, same id, must not double-write."""
        cls = self._discord_cls()
        log = ConversationLog(base_dir=tmp_path)
        host = SimpleNamespace(conv_log=log)
        key = "discord:mirrored-after-save"

        # The slot save got there first, under the slot's own id.
        log.append(key, "user", "hello", mid="m-1111111111111111")
        log.append(key, "assistant", "world", mid="m-2222222222222222")

        cls._persist_turn(
            host,
            key,
            "hello",
            "world",
            False,
            agent=None,
            mirror_mids=("m-1111111111111111", "m-2222222222222222"),
        )

        rows = _rows_on_disk(log, key)
        assert len(rows) == 2, (
            "the mirrored write duplicated a row the slot save already persisted -- "
            "a fresh mid defeats append_if_absent's same-mid skip"
        )

    def test_no_live_slot_means_a_plain_append_under_a_fresh_id(self, tmp_path) -> None:
        """``mirror_mids=None`` is "there was no slot", so nothing holds the row yet."""
        cls = self._discord_cls()
        log = ConversationLog(base_dir=tmp_path)
        host = SimpleNamespace(conv_log=log)
        key = "discord:unmirrored"

        cls._persist_turn(host, key, "hello", "world", False, agent=None, mirror_mids=None)

        mids = [row_mid(r) for r in _rows_on_disk(log, key)]
        assert all(isinstance(m, str) and m for m in mids)
        assert len(set(mids)) == 2


class TestIdentitySurvivesMaterialization:
    """The load-bearing half: a persisted id is PRESERVED, not re-minted.

    This is the property the frontend merge depends on. A row whose id changes
    between two materializations is, to every consumer of ``meta.mid``, two rows.
    """

    @staticmethod
    def _surface(state, messages, *, user: str):
        return channel_slots.surface_channel_session(
            state,
            {"key": f"weixin_kirocrew-research_direct_{user}", "title": "t", "modified": 0.0},
            {},
            [dict(r) for r in messages],
            session_key=f"weixin:kirocrew-research:direct:{user}",
        )

    @staticmethod
    def _transcript(*, with_ids: bool) -> list[dict]:
        rows: list[dict] = [
            {"role": "user", "content": "hi", "ts": "2026-09-01T00:00:00+00:00"},
            {"role": "assistant", "content": "hello", "ts": "2026-09-01T00:00:01+00:00"},
        ]
        if with_ids:
            rows[0]["meta"] = {"mid": "m-aaaaaaaaaaaaaaaa"}
            rows[1]["meta"] = {"mid": "m-bbbbbbbbbbbbbbbb"}
        return rows

    def test_window_carries_the_persisted_ids(self, dashboard_state) -> None:
        """What the fix buys: the window's identity IS the transcript's identity."""
        slot = self._surface(dashboard_state, self._transcript(with_ids=True), user="u1")
        assert slot is not None
        assert [row_mid(m) for m in slot.messages] == [
            "m-aaaaaaaaaaaaaaaa",
            "m-bbbbbbbbbbbbbbbb",
        ], "a materialized channel row must keep its persisted id rather than re-mint"

    def test_two_materializations_of_a_persisted_transcript_agree(self, dashboard_state) -> None:
        """Two independent windows over one transcript resolve to the SAME ids."""
        messages = self._transcript(with_ids=True)
        first = self._surface(dashboard_state, messages, user="u1")
        second = self._surface(dashboard_state, messages, user="u2")
        assert first is not None and second is not None
        assert [row_mid(m) for m in first.messages] == [row_mid(m) for m in second.messages]

    def test_an_id_less_transcript_stays_id_less_across_materializations(
        self, dashboard_state
    ) -> None:
        """Legacy rows must not advertise an identity absent from disk.

        Rows written before durable IDs remain on the index-based fallback. If a
        materializer minted a fresh window-only id, response-level operations
        would send an anchor that full-history readers cannot resolve.
        """
        bare = self._transcript(with_ids=False)
        first = self._surface(dashboard_state, bare, user="u3")
        second = self._surface(dashboard_state, bare, user="u4")
        assert first is not None and second is not None
        assert [row_mid(m) for m in first.messages] == [None, None]
        assert [row_mid(m) for m in second.messages] == [None, None]
