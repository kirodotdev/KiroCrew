"""Each gateway status row composed outside the dashboard carries its notice token.

These rows build their text from another component's output -- an exception, a
containment-change phrase, a start error, a formatter's card -- so the dashboard
could not localize them from a bare English ``content``. Each now ships a
``meta["notice"]`` token (and, where its copy interpolates values,
``meta["notice_args"]``), so the frontend renders the fixed frame in the reader's
language and the variable part verbatim inside it.

One assertion per row KIND, each RED on origin/main (the token does not exist
there). ``test_transient_notice_parity.py`` separately pins every token against
the frontend table and every wording against the LEGACY pattern it maps to.
"""

from __future__ import annotations

from kiro_crew.dashboard import gateway_notice as gn


class _Slot:
    """Captures ``append`` calls; the row families here read nothing else off it."""

    def __init__(self, key: str = "s1", messages=None):
        self.key = key
        self.messages = list(messages or [])
        self.appended: list[dict] = []

    def append(self, role, content, cls, ts="", *, broadcast=True, meta=None):
        row = {"role": role, "content": content, "cls": cls, "meta": meta}
        self.appended.append(row)
        self.messages.append(row)

    # turn_marker reconcile touches these; the no-row branch never runs here.
    def __getattr__(self, _name):  # pragma: no cover - only hit by unused attrs
        return None


def _notice(row: dict) -> str | None:
    meta = row["meta"] or {}
    return meta.get(gn.GATEWAY_NOTICE_META_KEY)


def _args(row: dict) -> dict:
    return (row["meta"] or {}).get(gn.GATEWAY_NOTICE_ARGS_KEY, {})


def test_turn_timeout_notice_tokens_and_args() -> None:
    from kiro_crew.dashboard import turn_dispatch as td

    text, token, args = td.turn_timeout_notice(600, None, td.TURN_LIMIT_GLOBAL)
    assert token == gn.GATEWAY_NOTICE_TURN_TIMEOUT
    # Structured args: a NUMBER plus a unit key, so the frontend words the limit
    # with its own plural keys. The English content still reads "10-minute".
    assert args["limit_value"] == 10
    assert args["limit_unit"] == "minute"
    assert args["limit_scope"] == td.TURN_LIMIT_GLOBAL
    assert "10-minute" in text
    # No pre-worded English limit/hint rides in the args any more.
    assert "limit" not in args and "hint" not in args

    _t2, token2, args2 = td.turn_timeout_notice(5400, "timeout", td.TURN_LIMIT_SESSION)
    assert token2 == gn.GATEWAY_NOTICE_TURN_TIMEOUT_NATIVE_PENDING
    assert args2["limit_value"] == 1.5
    assert args2["limit_unit"] == "hour"
    assert args2["limit_scope"] == td.TURN_LIMIT_SESSION
    assert "1.5-hour" in _t2


def test_background_turn_abandoned_notice_token_and_args() -> None:
    from kiro_crew.dashboard import turn_dispatch as td

    text, token, args = td.background_turn_abandoned_notice(120)
    assert token == gn.GATEWAY_NOTICE_BACKGROUND_TURN_ABANDONED
    # A NUMBER, not the English "2 minutes": the frontend words the plural.
    assert args == {"waited_minutes": 2}
    assert "2 minutes" in text


def test_finish_turn_task_abandoned_call_site_carries_its_token() -> None:
    # Pins the CALL SITE, not just the helper: finish_turn_task must stamp the
    # abandoned-turn card's notice token + args on the appended row. Removing the
    # meta from this append (buluoray's mutation) makes this fail, where the
    # helper-return test above would stay green.
    from kiro_crew.dashboard import turn_dispatch as td

    class _DoneTask:
        def cancelled(self) -> bool:
            return False

        def exception(self):
            return td.BackgroundTurnAbandoned(120.0, cap=4)

    class _State:
        def __init__(self) -> None:
            self._background_tasks: set = set()
            self.pushed = 0

        def push_slots_update(self) -> None:
            self.pushed += 1

    slot = _Slot()
    td.finish_turn_task(_State(), slot, _DoneTask(), timeout_secs=600.0)

    (row,) = slot.appended
    assert _notice(row) == gn.GATEWAY_NOTICE_BACKGROUND_TURN_ABANDONED
    assert _args(row) == {"waited_minutes": 2}


def test_close_interruption_row_carries_its_token() -> None:
    from kiro_crew.dashboard.slot_persistence import turn_marker as tm

    slot = _Slot()
    assert tm.record_close_interruption(slot) is True
    (row,) = slot.appended
    assert _notice(row) == gn.GATEWAY_NOTICE_TURN_INTERRUPTED_CLOSE
    # The kind the reconcile already stamped is preserved beside the token.
    assert row["meta"]["kind"] == tm._CLOSE_INTERRUPTION_KIND


def test_crew_no_permit_card_carries_its_token() -> None:
    # The card text is a module constant; assert the token rides with it.
    from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime_session as crs

    slot = _Slot()
    slot.append(
        "error",
        crs.NO_PERMIT_CARD,
        "msg msg-err",
        meta=gn.gateway_notice_meta(gn.GATEWAY_NOTICE_CREW_TURN_NO_PERMIT),
    )
    # The site writes exactly this; the real path is exercised by the parity
    # test's literal-site scan. Here we pin the token/text pairing.
    (row,) = slot.appended
    assert _notice(row) == gn.GATEWAY_NOTICE_CREW_TURN_NO_PERMIT
    assert row["content"] == crs.NO_PERMIT_CARD


def test_meta_shape_for_composed_rows() -> None:
    # The composed families interpolate values; each rides under notice_args.
    dropped = gn.gateway_notice_meta(
        gn.GATEWAY_NOTICE_SEND_DROPPED, target="s2", change="a constraint changed", excerpt=""
    )
    assert dropped[gn.GATEWAY_NOTICE_META_KEY] == "send_dropped"
    assert dropped[gn.GATEWAY_NOTICE_ARGS_KEY] == {
        "target": "s2",
        "change": "a constraint changed",
        "excerpt": "",
    }
    eager = gn.gateway_notice_meta(
        gn.GATEWAY_NOTICE_EAGER_SPAWN_STOPPED, failures=3, detail="BootError"
    )
    assert eager[gn.GATEWAY_NOTICE_ARGS_KEY] == {"failures": 3, "detail": "BootError"}
    # A frame-only row carries the token alone, no args.
    assert gn.gateway_notice_meta(gn.GATEWAY_NOTICE_TURN_FAILED_BEFORE_START) == {
        gn.GATEWAY_NOTICE_META_KEY: "turn_failed_before_start"
    }


def test_note_failed_before_the_try_tokenizes_both_variants() -> None:
    from kiro_crew.dashboard.chat_turn.queue_cycle import (
        TURN_FAILED_META,
        _note_failed_before_the_try,
        _TurnExit,
    )

    plain = _Slot()
    exit_plain = _TurnExit()
    _note_failed_before_the_try(plain, exit_plain)
    (row,) = plain.appended
    assert _notice(row) == gn.GATEWAY_NOTICE_TURN_FAILED_BEFORE_START
    assert row["meta"][TURN_FAILED_META] is True

    cmd = _Slot()
    exit_cmd = _TurnExit()
    exit_cmd.local_command = "/status"
    _note_failed_before_the_try(cmd, exit_cmd)
    (row2,) = cmd.appended
    assert _notice(row2) == gn.GATEWAY_NOTICE_LOCAL_COMMAND_FAILED_BEFORE_START
    assert _args(row2) == {"command": "/status"}
    assert row2["meta"][TURN_FAILED_META] is True


# --- Every tokenized KIND maps its exact wording to its token, via the frontend
# --- LEGACY table (the same resolution the dashboard does for a pre-token row on
# --- disk). Red on main: these tokens are absent there. Covers the kinds whose
# --- append content the parity literal-site scan skips (a concatenation, a local
# --- variable), so each still has a dedicated per-kind assertion here.
def _legacy_token_of(text: str) -> str | None:
    import re
    from pathlib import Path

    fe = Path(__file__).resolve().parents[1] / "website/src/pages/chat/gatewayNotice.ts"
    body = re.search(
        r"^const LEGACY\b[^=]*=\s*\[(?P<body>.*?)^\]",
        fe.read_text(encoding="utf-8"),
        re.DOTALL | re.MULTILINE,
    ).group("body")
    for src, token in re.findall(r"re:\s*/(\^.*?\$)/u,\s*token:\s*'([a-z_]+)'", body):
        if re.compile(src.replace("(?<", "(?P<")).match(text):
            return token
    return None


def test_every_tokenized_kind_maps_its_wording_to_its_token() -> None:
    cases = {
        # steer_queue
        "⚠️ Queued message dropped: a mirror became unverified after it was queued, so the "
        "authorization that admitted it no longer holds.": "queued_message_dropped",
        # delivery_containment, both sender notices
        "⚠️ Message you sent to s2 was dropped before it ran: a constraint changed after it "
        "was queued, so the authorization that admitted it no longer holds. It was not "
        'delivered and will not run. Text: "hi"': "send_dropped",
        "⚠️ Message you sent to s2 was discarded before it ran: it was cancelled from that "
        "session's queue. It was not delivered and will not run.": "send_discarded",
        # eager_spawn
        "This chat's agent failed to start 3 times in a row, so it will not be started again "
        "in the background. Send a message to try again. Last error: BootError": "eager_spawn_stopped",
        # turn_marker, both reconcile rows
        "This turn was interrupted when the app restarted. Review the partial output above, "
        "then resume to continue.": "turn_interrupted_restart",
        "This turn was interrupted when its chat was closed. Review the partial output above, "
        "then resume to continue.": "turn_interrupted_close",
        # crew_runtime_session
        "This crew's turn never started: it waited for a free background-turn slot and gave "
        "up. Nothing ran and nothing was rolled back. The crew gets another turn on its next "
        "signal or idle cycle; raise `dashboard.max_background_turns` if the fleet is queueing "
        "at the cap.": "crew_turn_no_permit",
        # turn_dispatch, both cards
        "⏱️ This turn hit the 10-minute limit and was stopped. Work already written to disk is "
        "still there — nothing was rolled back. Send a message to continue from where it "
        "stopped. To allow longer turns, raise it in Settings.": "turn_timeout",
        "⏱️ This turn never started: it waited 2 minutes for a free background-turn slot and "
        "gave up. Nothing ran and nothing was rolled back; the next message or loop cycle "
        "starts a new turn. If turns keep queueing at the cap, raise "
        "`dashboard.max_background_turns`.": "background_turn_abandoned",
        # turn_failures capability card
        "capability_tool_limits_unsupported: This crew member's saved capabilities turn off "
        "single tools on an MCP server. The backend its chats run on (agent.member_acp_backend) "
        "cannot turn off single tools, so they would be on. On the member's Capabilities page, "
        "turn the whole server off or turn those tools back on, then start a new "
        "chat.": "capability_tool_limits_unsupported",
    }
    for text, token in cases.items():
        assert _legacy_token_of(text) == token, (token, text)


def test_stay_english_rows_are_not_tokenized() -> None:
    # The pure-exception / formatter-output rows keep their English with a
    # documented reason, so their wording maps to NO token.
    assert _legacy_token_of("Some provider SDK exception text") is None
    assert (
        _legacy_token_of("Clip can't run here: its agent runs only on the KAS or Kiro backend.")
        is None
    )
