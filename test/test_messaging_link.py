"""Tests for the shared link helpers in ``messaging.link``.

Covers the two-level DM session-key builder — the canonical channel-first shape,
generation rotation, dmScope isolation vs unification, safe fallback on an unknown
scope, and the reserved ``group`` chat-type slot — plus the automatic origin-mirror
bind the DM dispatchers share.
"""

from __future__ import annotations

import time

from kiro_crew.messaging.link import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    DEFAULT_DM_SCOPE,
    DM_SCOPE_PER_CHANNEL_PEER,
    DM_SCOPE_UNIFIED,
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    should_rotate_generation,
)
from kiro_crew.session_map import ConversationOwnershipConflict


class TestBuildDmSessionKey:
    def test_per_channel_peer_is_channel_first(self) -> None:
        assert (
            build_dm_session_key("telegram", "kirocrew", "123")
            == "telegram:kirocrew:direct:123"
        )

    def test_default_scope_is_per_channel_peer(self) -> None:
        assert DEFAULT_DM_SCOPE == DM_SCOPE_PER_CHANNEL_PEER
        assert build_dm_session_key(
            "telegram", "kirocrew", "123"
        ) == build_dm_session_key(
            "telegram", "kirocrew", "123", dm_scope=DM_SCOPE_PER_CHANNEL_PEER
        )

    def test_generation_zero_is_bare_bucket(self) -> None:
        assert build_dm_session_key("telegram", "a", "1", gen=0) == "telegram:a:direct:1"

    def test_generation_rotates_and_keeps_bucket_prefix(self) -> None:
        bucket = build_dm_session_key("telegram", "a", "1")
        g2 = build_dm_session_key("telegram", "a", "1", gen=2)
        assert g2 == "telegram:a:direct:1:gen2"
        assert g2.startswith(bucket)

    def test_per_channel_peer_isolates_channels(self) -> None:
        assert build_dm_session_key("telegram", "a", "1") != build_dm_session_key(
            "wecom", "a", "1"
        )

    def test_unified_collapses_channel_and_user(self) -> None:
        a = build_dm_session_key("telegram", "kirocrew", "1", dm_scope=DM_SCOPE_UNIFIED)
        b = build_dm_session_key("wecom", "kirocrew", "999", dm_scope=DM_SCOPE_UNIFIED)
        assert a == b == "unified:kirocrew"

    def test_unified_with_generation(self) -> None:
        assert (
            build_dm_session_key("telegram", "a", "1", gen=3, dm_scope=DM_SCOPE_UNIFIED)
            == "unified:a:gen3"
        )

    def test_unknown_scope_falls_back_to_per_channel_peer(self) -> None:
        assert build_dm_session_key(
            "telegram", "a", "1", dm_scope="bogus"
        ) == build_dm_session_key("telegram", "a", "1")

    def test_group_chat_type_uses_reserved_slot(self) -> None:
        assert (
            build_dm_session_key("telegram", "a", "1", chat_type="group")
            == "telegram:a:group:1"
        )

    def test_unified_scopes_only_direct_dms_not_forum(self) -> None:
        # SECURITY (issue #211, PR #219 Codex HIGH): dm_scope=unified must NOT
        # collapse a forum Topic into the shared DM bucket — that would leak
        # private DM content into a group Topic (and vice versa). A forum route
        # keeps its FULL bucket regardless of dm_scope.
        key = build_dm_session_key(
            "telegram",
            "kirocrew",
            "-1001234567890:5",
            dm_scope=DM_SCOPE_UNIFIED,
            chat_type=CHAT_TYPE_FORUM,
        )
        assert key == "telegram:kirocrew:forum:-1001234567890:5"
        assert not key.startswith(f"{DM_SCOPE_UNIFIED}:")

    def test_unified_still_collapses_direct_dm(self) -> None:
        # Regression: a direct (1:1) DM under unified still collapses to the
        # single shared bucket (cross-surface continuity preserved).
        assert (
            build_dm_session_key(
                "telegram",
                "kirocrew",
                "123",
                dm_scope=DM_SCOPE_UNIFIED,
                chat_type=CHAT_TYPE_DIRECT,
            )
            == "unified:kirocrew"
        )


def _local_epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    """Epoch seconds for a local wall-clock time (DST auto-resolved)."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


class TestShouldRotateGeneration:
    def test_first_message_never_rotates(self) -> None:
        assert should_rotate_generation(0.0, 1_000_000.0, idle_minutes=30) is False

    def test_idle_fires_at_threshold(self) -> None:
        assert should_rotate_generation(1000.0, 1000.0 + 30 * 60, idle_minutes=30) is True

    def test_idle_not_reached(self) -> None:
        assert should_rotate_generation(1000.0, 1000.0 + 29 * 60, idle_minutes=30) is False

    def test_idle_disabled(self) -> None:
        assert should_rotate_generation(1000.0, 1000.0 + 10_000, idle_minutes=0) is False

    def test_daily_boundary_crossed(self) -> None:
        last = _local_epoch(2026, 7, 9, 3, 0)
        now = _local_epoch(2026, 7, 10, 5, 0)
        assert should_rotate_generation(last, now, daily_reset_hour=4) is True

    def test_daily_same_day_after_boundary_no_rotate(self) -> None:
        last = _local_epoch(2026, 7, 10, 5, 0)
        now = _local_epoch(2026, 7, 10, 7, 0)
        assert should_rotate_generation(last, now, daily_reset_hour=4) is False

    def test_daily_disabled(self) -> None:
        last = _local_epoch(2026, 7, 9, 3, 0)
        now = _local_epoch(2026, 7, 10, 5, 0)
        assert should_rotate_generation(last, now, daily_reset_hour=-1) is False


class _Sessions:
    """The narrow session-manager surface :func:`bind_origin_mirror` touches."""

    def __init__(self, *, raise_on_set: BaseException | None = None) -> None:
        self.mirror_links: dict[str, ChannelLink] = {}
        self.opt_outs: set[str] = set()
        self.writes = 0
        self._raise_on_set = raise_on_set

    def mirror_opt_out(self, key: str) -> bool:
        return key in self.opt_outs

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        return self.mirror_links.get(key)

    def set_mirror_link(self, key: str, link: ChannelLink) -> None:
        if self._raise_on_set is not None:
            raise self._raise_on_set
        self.writes += 1
        self.mirror_links[key] = link


_HERE = ChannelLink("discord", channel_id="c1")


class TestBindOriginMirror:
    def test_an_unbound_conversation_is_bound_to_itself(self) -> None:
        sess = _Sessions()
        assert bind_origin_mirror(sess, key="discord:kirocrew:direct:u1", location=_HERE) is True
        assert sess.mirror_links == {"discord:kirocrew:direct:u1": _HERE}

    def test_the_steady_state_is_a_read(self) -> None:
        """The re-assert runs per turn, so the repeating path must not write.

        A mutation rewrites the whole session map on the event loop; a per-turn
        write would put that stall on every message.
        """
        sess = _Sessions()
        for _ in range(5):
            bind_origin_mirror(sess, key="k", location=_HERE)
        assert sess.writes == 1

    def test_an_explicit_bind_to_another_location_is_not_repointed(self) -> None:
        """Nothing repoints a binding — a swept or rival-claimed one is REMOVED.

        So a binding naming another conversation on this channel is deliberate
        (the dashboard can bind a surfaced session anywhere), and re-pointing it
        at the origin would undo an explicit action with no signal.
        """
        sess = _Sessions()
        chosen = ChannelLink("discord", channel_id="c-elsewhere")
        sess.mirror_links["k"] = chosen
        assert bind_origin_mirror(sess, key="k", location=_HERE) is False
        assert sess.mirror_links["k"] == chosen

    def test_an_explicit_bind_on_ANOTHER_CHANNEL_is_preserved(self) -> None:
        """The dashboard can aim a session's mirror at any surface.

        A Discord conversation whose owner pointed its dashboard mirror at a
        Telegram chat must keep that target: overwriting it on the next Discord
        message would silently redirect their replies into this chat.
        """
        sess = _Sessions()
        chosen = ChannelLink("telegram", channel_id="7", thread_id=None)
        sess.mirror_links["k"] = chosen
        assert bind_origin_mirror(sess, key="k", location=_HERE) is False
        assert sess.mirror_links["k"] == chosen

    def test_a_real_slack_mirror_is_preserved(self) -> None:
        """The exception is the missing THREAD, not the channel type.

        A Slack mirror that names its thread is routable and deliberate.
        """
        sess = _Sessions()
        chosen = ChannelLink("slack", channel_id="C123", thread_id="1785370133.085469")
        sess.mirror_links["k"] = chosen
        assert bind_origin_mirror(sess, key="k", location=_HERE) is False
        assert sess.mirror_links["k"] == chosen

    def test_the_unrouted_slack_placeholder_does_not_block_the_bind(self) -> None:
        """``set_channel`` writes the namespaced bucket into ``slack_channel_id``.

        ``get_mirror_link`` synthesizes a Slack link from that field whenever no
        explicit mirror row exists, so the first turn of every new channel session
        reads one back. An empty thread is Slack's own clear sentinel and never
        enters the reverse index, so nothing can be delivered through it — it is
        bookkeeping, not a binding.
        """
        sess = _Sessions()
        sess.mirror_links["k"] = ChannelLink("slack", channel_id="discord:c1")
        assert bind_origin_mirror(sess, key="k", location=_HERE) is True
        assert sess.mirror_links["k"] == _HERE

    def test_a_unified_bucket_is_never_bound(self) -> None:
        """``dm_scope="unified"`` collapses every user's DMs into one bucket.

        The channel and the user drop out of the key, so "the origin conversation"
        has no single answer and a binding there would deliver one user's
        dashboard replies into another user's chat.
        """
        sess = _Sessions()
        assert bind_origin_mirror(sess, key="unified:kirocrew", location=_HERE) is False
        assert bind_origin_mirror(sess, key="unified:kirocrew:gen4", location=_HERE) is False
        assert sess.mirror_links == {}

    def test_a_per_channel_bucket_under_the_same_config_is_bound(self) -> None:
        """The guard is read off the KEY, so only the collapsed shape is excluded.

        A forum/thread route keeps its full bucket under any scope.
        """
        sess = _Sessions()
        assert (
            bind_origin_mirror(sess, key="discord:kirocrew:group:t9", location=_HERE) is True
        )

    def test_an_opted_out_conversation_is_not_bound(self) -> None:
        sess = _Sessions()
        sess.opt_outs.add("k")
        assert bind_origin_mirror(sess, key="k", location=_HERE) is False
        assert sess.mirror_links == {}

    def test_the_opt_out_never_clears_a_binding_it_finds(self) -> None:
        """Declining is ALL it does: an explicit dashboard link must survive."""
        sess = _Sessions()
        sess.opt_outs.add("k")
        chosen = ChannelLink("discord", channel_id="c-elsewhere")
        sess.mirror_links["k"] = chosen
        assert bind_origin_mirror(sess, key="k", location=_HERE) is False
        assert sess.mirror_links["k"] == chosen

    def test_a_refused_claim_does_not_drop_the_turn(self) -> None:
        """This runs on the turn path: an uncaught raise answers the user nothing.

        Reachable for a transport declaring ``supports_session_resume`` — a
        dashboard session resumed into this conversation holds the location while
        the transport's in-memory resume registry is cold after a restart.
        """
        sess = _Sessions(raise_on_set=ConversationOwnershipConflict("held"))
        assert bind_origin_mirror(sess, key="k", location=_HERE) is False
        assert sess.mirror_links == {}

    def test_no_exception_from_the_claim_escapes(self) -> None:
        sess = _Sessions(raise_on_set=RuntimeError("session map on fire"))
        assert bind_origin_mirror(sess, key="k", location=_HERE) is False


class TestUnbindReasonVocabulary:
    """The audited reason strings are a closed set with one home.

    A call site that invents a spelling fragments the audit trail into groups
    nothing can join, and a duplicate value makes two causes indistinguishable in
    it. Both are cheap to pin and impossible to notice by reading a diff.
    """

    def test_the_vocabulary_is_closed_and_unambiguous(self) -> None:
        from kiro_crew.messaging import link as link_mod

        declared = [
            value
            for name, value in vars(link_mod).items()
            if name.startswith("UNBIND_REASON_") and isinstance(value, str)
        ]
        # Closed set, and no two causes share a spelling — a duplicate would make
        # them indistinguishable in the audit.
        assert set(declared) == set(link_mod.UNBIND_REASONS)
        assert len(declared) == len(set(declared))

    def test_no_call_site_passes_a_bare_reason_literal(self) -> None:
        """A literal at a binding call site bypasses the vocabulary entirely.

        Scoped by AST to the methods that actually carry an unbind reason —
        ``reason=`` is an ordinary kwarg name elsewhere in the package (autonudge
        stop reasons, history archive reasons), and a text scan would flag those.
        The planted sample proves the scanner fires at all.
        """
        import ast
        from pathlib import Path

        import kiro_crew

        binding_calls = {
            "clear_mirror_link",
            "clear_mirror_links_at",
            "set_mirror_link",
            "delete",
        }

        def _bare_reasons(tree: ast.AST) -> list[int]:
            hits = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in binding_calls:
                    continue
                for kw in node.keywords:
                    if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                        hits.append(kw.value.lineno)
            return hits

        planted = ast.parse('s.clear_mirror_link(key, reason="made_up")\n')
        assert len(_bare_reasons(planted)) == 1, "the scanner does not fire"

        pkg = Path(kiro_crew.__file__).resolve().parent
        offenders = [
            f"{path.relative_to(pkg)}:{line}"
            for path in pkg.rglob("*.py")
            if "_vendor" not in path.parts
            for line in _bare_reasons(ast.parse(path.read_text(encoding="utf-8")))
        ]
        assert not offenders, (
            "unbind reasons must come from messaging.link's UNBIND_REASON_* "
            f"constants, not bare literals: {offenders}"
        )
