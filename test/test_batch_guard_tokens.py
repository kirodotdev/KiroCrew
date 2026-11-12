"""The batch write guard refuses on a token the write could not have seen.

A write batched with a read is refused only when its arguments carry a token
(a sha, an item, session or artifact id, a PR or run number) that the session has
not seen yet: then the value can only have come from a call of the same batch, or
from a guess. A write whose tokens were all seen before is independent of the
read and passes. The seen set fails open: when the transport did not see the
session from its start, or had to drop tokens at its bound, a refusal needs proof
(a returned peer whose output carries the token).
"""

from __future__ import annotations

import importlib
import json

import pytest

from kiro_crew.acp.types import EVENT_TOOL_CALL, AcpEvent

_SHA = "1f69e7c13c1bb4c1b0bb1fa5b1e2f75c73a25ae0"


def _guard():
    return importlib.import_module("kiro_crew.acp.batch_guard")


def _report(summary: str) -> str:
    return json.dumps({"status": "progress", "summary": summary})


def _window():
    """A window on a test clock; ``next_step()`` moves it on, as the model's next
    step does."""
    now = [1000.0]
    window = _guard().BatchWindow(clock=lambda: now[0])
    window.next_step = lambda: now.__setitem__(0, now[0] + 1.0)
    return window


# -- tokens --


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"head {_SHA}", {_SHA}),
        ("item it_9fc6cc99 bound", {"it_9fc6cc99"}),
        ("to chat-4046-1793158739", {"chat-4046-1793158739"}),
        ("artifact art_b81c953fa16a", {"art_b81c953fa16a"}),
        ("PR #1630 is open", {"1630"}),
        ("see pull/1630 and runs/40590558983", {"1630", "40590558983"}),
        ("review id 5727396521", {"5727396521"}),
        ('{"pr": 1742}', {"1742"}),
        # Uppercase hex is the same id as its lowercase form.
        (f"head {_SHA.upper()}", {_SHA}),
        # A sha256 digest is one token, not a miss.
        ("image sha256:" + "ab12" * 16, {"ab12" * 16}),
        ("gh pr merge 1155 --squash", {"1155"}),
    ],
)
def test_a_token_is_an_id_a_write_could_quote(text, expected):
    assert _guard().tokens_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "at 06:15:22Z, back 09:30Z",
        "accepted the decade",
        "deadbeef only letters",
        "1234567 only digits",
        "2026-11-06",
        "line 42 of 7 files",
    ],
)
def test_a_time_a_word_or_a_small_number_is_not_a_token(text):
    assert _guard().tokens_of(text) == set()


# -- the rule --


def test_a_write_with_a_read_peer_and_no_unseen_token_passes():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "gh pr checks 7")
    window.dispatched("w", guard.WRITE, text=_report("checks are running"))
    assert window.refusal_for("w", guard.WRITE) is None


@pytest.mark.parametrize(
    ("read", "write"),
    [
        ("gh pr checks 1630", "gh pr comment 1630 --body 'checks are running'"),
        ("git status", "git commit -m 'tidy the notes'"),
        ("git status", json.dumps({"target": "the reviewer", "message": "checks are running"})),
    ],
    ids=["gh-comment", "git-commit", "session-send"],
)
def test_a_main_write_batched_with_a_read_and_no_unseen_token_passes(read, write):
    # The loosening: on main every write batched with a read was refused. A
    # write of main's write set that quotes nothing unseen now runs.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, read)
    window.dispatched("w", guard.WRITE, text=write)
    window.settled("r", "all checks pending")
    assert window.refusal_for("w", guard.WRITE) is None


def test_the_token_scanner_uses_no_regular_expressions():
    module = importlib.import_module("kiro_crew.acp.batch_guard_tokens")
    assert not hasattr(module, "re")


def test_a_report_naming_a_sha_only_a_same_batch_read_returned_is_refused():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "gh pr view 7 --json headRefOid")
    window.dispatched("w", guard.WRITE, text=_report(f"PR 7 head {_SHA}"))
    window.settled("r", f'{{"headRefOid": "{_SHA}"}}')
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    assert _SHA[:12] in reason


def test_a_token_the_write_shares_with_its_peers_arguments_is_not_unseen():
    # The model wrote the PR number into the read itself, so citing it is no guess.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "gh pr view 1630 --json state")
    window.dispatched("w", guard.WRITE, text=_report("reading #1630 now"))
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_creators_minted_keys_leave_out_a_key_of_its_own_argument():
    # The argument is a short sha; the result prints the full sha. The full sha is
    # a different token, but its 7-character key is the argument: that key is the
    # input, not a minted value. A sha the call never took still mints its keys.
    guard = _guard()
    other = "9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e3f2a1b0c"
    window = guard.BatchWindow()
    window.dispatched("c", guard.WRITE, text=json.dumps({"ref": _SHA[:7]}), creator=True)
    window.settled("c", f"created {_SHA} and {other}")
    minted = guard._keys_of(window._members["c"])
    assert _SHA[:7] not in minted
    assert other[:7] in minted
    assert other in minted


def test_create_then_bind_with_the_new_id_in_one_batch_is_refused():
    guard = _guard()
    window = guard.BatchWindow()
    create = json.dumps({"action": "create", "title": "t"})
    bind = json.dumps({"action": "bind", "item_id": "it_9fc6cc99"})
    window.dispatched("c", guard.WRITE, text=create, creator=True)
    window.dispatched("b", guard.WRITE, text=bind)
    assert window.refusal_for("b", guard.WRITE) is not None


def test_a_write_peer_that_mints_nothing_is_not_a_producer():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("a", guard.WRITE, text=_report("first"))
    window.dispatched("b", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("b", guard.WRITE) is None


def test_the_same_bind_after_the_create_returned_passes():
    guard = _guard()
    window = _window()
    window.dispatched("c", guard.WRITE, text=json.dumps({"action": "create"}), creator=True)
    window.settled("c", "Recorded create. item=it_9fc6cc99")
    window.next_step()
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("b", guard.WRITE, text=json.dumps({"item_id": "it_9fc6cc99"}))
    assert window.refusal_for("b", guard.WRITE) is None


def test_a_sha_seen_earlier_and_shortened_passes():
    guard = _guard()
    window = _window()
    window.dispatched("r0", guard.READ, "git log -1")
    window.settled("r0", f"commit {_SHA}")
    window.next_step()
    window.dispatched("r", guard.READ, "gh pr checks 7")
    window.dispatched("w", guard.WRITE, text=_report(f"still at {_SHA[:8]}"))
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_full_sha_extended_from_a_seen_short_prefix_is_refused():
    # Matching runs one way: a write may shorten a sha it saw, never lengthen one.
    # Only the 8-char prefix was seen, so the other 32 characters are a guess.
    guard = _guard()
    window = _window()
    window.dispatched("r0", guard.READ, "git log -1 --oneline")
    window.settled("r0", f"{_SHA[:8]} fix things")
    window.next_step()
    window.dispatched("r", guard.READ, "gh pr view 7 --json headRefOid")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    assert _SHA[:12] in reason


def test_a_short_sha_of_a_seen_sha_passes_up_to_12_characters():
    # A seen sha is kept with its 7-12 character prefixes (the lengths git
    # abbreviates to) and in full; a 13-39 character cut is not matched.
    guard = _guard()
    window = _window()
    window.dispatched("r0", guard.READ, "git log -1")
    window.settled("r0", f"commit {_SHA}")
    window.next_step()
    window.dispatched("r", guard.READ, "gh pr checks 7")
    window.dispatched("w", guard.WRITE, text=_report(f"still at {_SHA[:12]}"))
    assert window.refusal_for("w", guard.WRITE) is None
    assert guard.token_keys(_SHA) == [_SHA] + [_SHA[:n] for n in range(7, 13)]


def test_one_result_with_thousands_of_shas_keeps_the_rule_working():
    # 6,000 full shas (a long `git log`) must not overflow the seen set: the
    # set stays complete, so a guessed sha next to a read is still refused.
    guard = _guard()
    window = _window()
    log = "\n".join(f"{i:08x}" * 5 for i in range(1, 6001))
    assert len(log) < 256 * 1024
    window.dispatched("r0", guard.READ, "git log")
    window.settled("r0", log)
    window.next_step()
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.seen.complete is True
    assert window.refusal_for("w", guard.WRITE) is not None


def test_a_refused_guess_is_not_learned_and_is_refused_again():
    # A refused write never ran, so its guessed value must not become "seen".
    guard = _guard()
    window = _window()
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is not None
    window.settled("r", "state OPEN")
    window.settled("w")
    window.next_step()
    window.dispatched("r2", guard.READ, "gh pr checks 7")
    window.dispatched("w2", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w2", guard.WRITE) is not None


@pytest.mark.parametrize(
    "text",
    [
        "bind to chat-4046-1793158739",
        '{"action": "bind", "item_id": "it_9fc6cc99"}',
    ],
    ids=["session-key", "ledger-id"],
)
def test_a_resumed_session_still_refuses_a_minted_id_it_never_saw(text):
    # After a resume only proof refuses, except for the id families Kiro Crew
    # mints (session keys, ledger item ids): a create's new key or item id quoted
    # in its own batch is still refused when the resumed text never showed it.
    guard = _guard()
    window = guard.BatchWindow()
    guard.mark_resumed(window)
    window.dispatched("c", guard.WRITE, text=json.dumps({"action": "create"}), creator=True)
    window.dispatched("b", guard.WRITE, text=text)
    assert window.refusal_for("b", guard.WRITE) is not None


@pytest.mark.parametrize(
    "text",
    [
        "send to chat-4046-1793158739",
        '{"action": "decide", "item_id": "it_9fc6cc99"}',
    ],
    ids=["session-key", "ledger-id"],
)
def test_a_resumed_session_passes_a_minted_id_from_history_next_to_a_read(text):
    # The restored history (which this window never saw) can carry a worker's
    # key or an item id; next to a plain read, with no creator in the batch,
    # quoting it is no guess. A returned peer printing it is still proof.
    guard = _guard()
    window = guard.BatchWindow()
    guard.mark_resumed(window)
    window.dispatched("r", guard.READ, "date -u")
    window.dispatched("s", guard.WRITE, text=text)
    assert window.refusal_for("s", guard.WRITE) is None
    window.settled("r", text)
    assert window.refusal_for("s", guard.WRITE) is not None


def test_a_resumed_session_passes_a_minted_id_its_resumed_text_showed():
    guard = _guard()
    window = guard.BatchWindow()
    guard.mark_resumed(window)
    guard.note_prompt(window, "Your worker is chat-4046-1793158739.")
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("s", guard.WRITE, text="send to chat-4046-1793158739")
    assert window.refusal_for("s", guard.WRITE) is None


@pytest.mark.parametrize("tool", ["session_create", "session_fork", "work_dispatch"])
def test_a_resumed_session_refuses_a_session_key_guessed_next_to_a_session_creator(tool):
    # The same rule as for a ledger create: after a resume the restored history
    # may hold any key, but a key the resumed text never showed, named in the
    # batch of a tool that returns a new one, is still a guess.
    guard = _guard()
    create = _event(
        tool_call_id="c",
        tool_name=tool,
        mcp_server_name="kirocrew-dashboard",
        mcp_identity_trusted=True,
        tool_input=json.dumps({"title": "reviewer"}),
    )
    window = guard.BatchWindow()
    guard.mark_resumed(window)
    guard.note_dispatched(window, create)
    window.dispatched("s", guard.WRITE, text="send to chat-4046-1793158739")
    assert window.refusal_for("s", guard.WRITE) is not None

    shown = guard.BatchWindow()
    guard.mark_resumed(shown)
    guard.note_prompt(shown, "Your worker is chat-4046-1793158739.")
    guard.note_dispatched(shown, create)
    shown.dispatched("s", guard.WRITE, text="send to chat-4046-1793158739")
    assert shown.refusal_for("s", guard.WRITE) is None


def test_a_resumed_session_still_needs_proof_for_a_sha():
    guard = _guard()
    window = guard.BatchWindow()
    guard.mark_resumed(window)
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_token_in_a_prompt_is_seen():
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, f"Your item is it_9fc6cc99; merge at {_SHA}.")
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE, text=_report(f"it_9fc6cc99 at {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is None


def test_model_text_counts_only_from_the_next_batch():
    # A sha the model writes while it composes this batch is its own guess.
    guard = _guard()
    window = _window()
    window.dispatched("r", guard.READ, "gh pr view 7")
    guard.note_text(window, f"the head will be {_SHA}")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is not None

    # Text the model wrote in a step whose writes were not refused is seen from
    # the next batch on. (A refused guess is not learned; see the test below.)
    window = _window()
    window.dispatched("r", guard.READ, "gh pr view 7")
    guard.note_text(window, f"the head is {_SHA}")
    window.dispatched("w", guard.WRITE, text=_report("reading the PR"))
    assert window.refusal_for("w", guard.WRITE) is None
    window.settled("r")
    window.settled("w")
    window.next_step()
    window.dispatched("r2", guard.READ, "gh pr checks 7")
    window.dispatched("w2", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w2", guard.WRITE) is None


def test_a_new_turn_keeps_what_the_session_has_seen():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r0", guard.READ, "git log -1")
    window.settled("r0", f"commit {_SHA}")
    window.reset()
    window.dispatched("r", guard.READ, "gh pr checks 7")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is None


# -- failing open --


def test_a_resumed_session_refuses_only_on_proof():
    guard = _guard()
    window = guard.BatchWindow()
    guard.mark_resumed(window)
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    # The sha may come from history this transport never saw.
    assert window.refusal_for("w", guard.WRITE) is None
    window.settled("r", f"headRefOid {_SHA}")
    assert window.refusal_for("w", guard.WRITE) is not None


def test_dropping_tokens_at_the_bound_marks_the_set_incomplete(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "MAX_SEEN_TOKENS", 4)
    window = guard.BatchWindow()
    guard.note_prompt(window, " ".join(f"it_{i:08x}" for i in range(10)))
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE, text=_report("bound it_00000001"))
    # it_00000001 was dropped: it is not proof of a guess.
    assert window.refusal_for("w", guard.WRITE) is None


def test_an_unreadable_member_text_never_refuses():
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "gh pr view 7")
    window.dispatched("w", guard.WRITE, text=None)  # type: ignore[arg-type]
    assert window.refusal_for("w", guard.WRITE) is None


# -- creators and tables --


def _event(**fields) -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, **fields)


def _shell(command: str) -> AcpEvent:
    return _event(is_shell=True, tool_name="shell", tool_input=json.dumps({"command": command}))


@pytest.mark.parametrize(
    "event",
    [
        _shell("gh pr create --title t --body-file b.md"),
        _shell("git commit -m x && git push origin HEAD"),
        _shell("aws logs start-query --log-group-name g --query-string q"),
        _event(
            tool_name="work_ledger_record",
            mcp_server_name="kirocrew-work",
            mcp_identity_trusted=True,
            tool_input=json.dumps({"action": "create", "title": "t"}),
        ),
    ],
)
def test_a_call_that_mints_ids_is_a_creator(event):
    assert _guard().is_creator(event) is True


@pytest.mark.parametrize(
    "envelope",
    [
        {
            "server": "kirocrew-work",
            "tool": "work_ledger_record",
            "arguments": {"action": "create"},
        },
        {"tool_id": "kirocrew-work::work_ledger_record", "arguments": {"action": "create"}},
    ],
    ids=["server-tool-arguments", "tool-id-arguments"],
)
def test_a_wrapped_ledger_create_is_a_creator(envelope):
    # Harnesses that wrap MCP arguments in an envelope still mint the item id.
    event = _event(
        tool_name="work_ledger_record",
        mcp_server_name="kirocrew-work",
        mcp_identity_trusted=True,
        tool_input=json.dumps(envelope),
        raw_tool_params=envelope,
    )
    assert _guard().is_creator(event) is True


def test_an_envelope_naming_another_tool_is_not_unwrapped():
    envelope = {"server": "kirocrew-work", "tool": "work_report", "arguments": {"action": "create"}}
    event = _event(
        tool_name="work_ledger_record",
        mcp_server_name="kirocrew-work",
        mcp_identity_trusted=True,
        tool_input=json.dumps(envelope),
        raw_tool_params=envelope,
    )
    assert _guard().is_creator(event) is False


@pytest.mark.parametrize(
    "event",
    [
        _shell("gh pr view 7"),
        _event(
            tool_name="work_ledger_record",
            mcp_server_name="kirocrew-work",
            mcp_identity_trusted=True,
            tool_input=json.dumps({"action": "bind", "item_id": "it_9fc6cc99"}),
        ),
        _event(tool_name="work_report", mcp_server_name="kirocrew-work", mcp_identity_trusted=True),
        # A session tool that returns no new key.
        _event(
            tool_name="session_status",
            mcp_server_name="kirocrew-dashboard",
            mcp_identity_trusted=True,
        ),
        _event(tool_name="spawn_run", mcp_server_name="kirocrew-core", mcp_identity_trusted=True),
    ],
)
def test_a_call_that_mints_nothing_is_not_a_creator(event):
    assert _guard().is_creator(event) is False


@pytest.mark.parametrize("tool", ["session_create", "session_fork", "work_dispatch"])
def test_a_session_tool_that_returns_a_new_key_is_a_creator_and_stays_unjudged(tool):
    event = _event(tool_name=tool, mcp_server_name="kirocrew-dashboard", mcp_identity_trusted=True)
    assert _guard().is_creator(event) is True
    # Not write-class: the guard never refuses the create itself.
    assert _guard().classify(event) == ""


@pytest.mark.parametrize(
    ("server", "tool", "expected"),
    [
        ("kirocrew-work", "work_ledger_record", "write"),
        ("kirocrew-dashboard", "session_send", "write"),
        ("kirocrew-core", "send_message", "write"),
        # Main's write set only: these stay unclassified, so never judged.
        ("kirocrew-dashboard", "session_create", ""),
        ("kirocrew-core", "spawn_run", ""),
        ("kirocrew-core", "learn_add", ""),
        ("kirocrew-cron", "cron_add", ""),
        ("kirocrew-cron", "cron_list", ""),
        ("kirocrew-core", "wait", ""),
    ],
)
def test_kiro_crew_tools_keep_mains_classes(server, tool, expected):
    event = _event(tool_name=tool, mcp_server_name=server, mcp_identity_trusted=True)
    assert _guard().classify(event) == expected


def test_an_unshipped_servers_mcp_tool_is_never_classified():
    # Operator entries name shell prefixes only; an @server/tool entry does not
    # turn another server's MCP tool into a write.
    guard = _guard()
    event = _event(
        tool_name="publish", mcp_server_name="example-reviews", mcp_identity_trusted=True
    )
    assert guard.classify(event) == ""
    assert guard.classify(event, ("@example-reviews/publish",)) == ""


def test_only_mains_write_tools_are_withheld_from_auto_approval(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    policy = {"rules": [{"capability": "mcp", "match": ["kirocrew-core/*"], "effect": "allow"}]}
    _policy, refs = guard.withhold_write_auto_approval(policy)
    assert "@kirocrew-core/send_message" in refs
    assert "@kirocrew-core/spawn_run" not in refs
    assert "@kirocrew-core/memory_recall" not in refs


# -- reading what the model saw --


def test_ids_on_separate_lines_of_a_json_encoded_result_are_all_seen():
    # A transport hands the result over as JSON: a newline becomes the two
    # characters backslash-n, which must not glue two ids into one word.
    guard = _guard()
    window = _window()
    output = json.dumps([{"type": "text", "text": "abc1234 one\ndef5678 two"}])
    window.dispatched("r0", guard.READ, "git log --oneline")
    window.settled("r0", output)
    window.next_step()
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, "git push origin def5678:refs/heads/x")
    assert window.refusal_for("w", guard.WRITE) is None


@pytest.mark.parametrize(
    "update",
    [
        {"content": [{"type": "content", "content": {"type": "text", "text": '{"pr": 1742}'}}]},
        {"content": [{"type": "text", "text": '{"items": [{"pr": 1742}]}'}]},
        {"rawOutput": {"stdout": '{"pr": 1742}'}},
    ],
    ids=["wrapped-block", "bare-block", "raw-output"],
)
def test_a_pr_number_in_a_json_encoded_result_is_seen(update):
    # A ledger read returns `"pr": 1742` inside a text block; the frame is
    # JSON-encoded on the way, which escapes those quotes. The number is seen.
    guard = _guard()
    window = _window()
    window.dispatched("r0", guard.READ, "work_ledger_read")
    window.settled("r0", guard.update_text(update))
    window.next_step()
    window.dispatched("r", guard.READ, "date -u")
    window.dispatched("w", guard.WRITE, text=json.dumps({"kind": "pr_checks", "pr": 1742}))
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_sha_split_across_two_streamed_text_chunks_is_seen():
    guard = _guard()
    window = _window()
    guard.note_text(window, f"the head is {_SHA[:20]}")
    guard.note_text(window, f"{_SHA[20:]} now")
    window.dispatched("r0", guard.READ, "git status")
    window.settled("r0")
    window.next_step()
    window.dispatched("r", guard.READ, "gh pr checks 7")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_result_past_the_batch_bound_is_cut_and_marks_the_set_incomplete(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "MAX_BATCH_TOKENS", 4)
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "git log")
    window.settled("r", " ".join(f"it_{i:08x}" for i in range(10)))
    assert len(window._members[next(iter(window._members))].output) <= 4
    assert window.seen.complete is False


def test_a_sha_past_the_scan_bound_in_a_writes_arguments_never_causes_a_refusal():
    # A write's arguments are cut at the scan bound like any seen text, so a sha
    # past the cut is not learned; the set must say so, or a later write quoting
    # that sha is refused as a guess.
    guard = _guard()
    window = _window()
    filler = "x " * (guard.MAX_SCANNED_TEXT_CHARS // 2)
    window.dispatched("w0", guard.WRITE, text=f"{filler} head {_SHA}")
    window.settled("w0")
    window.next_step()
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is None


def test_model_text_past_the_batch_bound_is_cut_and_marks_the_set_incomplete(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "MAX_BATCH_TOKENS", 4)
    window = guard.BatchWindow()
    for i in range(10):
        guard.note_text(window, f" it_{i:08x} ")
    assert len(window._pending) <= 4
    assert window.seen.complete is False


def test_a_prompt_echoing_a_refused_value_does_not_make_it_seen():
    # A recovery prompt quotes the refused call; that is not a source of the value.
    guard = _guard()
    window = _window()
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is not None
    window.settled("r", "clean")
    window.settled("w")
    window.next_step()
    guard.note_prompt(window, f"Your call `head {_SHA}` was refused by the batch guard.")
    window.dispatched("r2", guard.READ, "git status")
    window.dispatched("w2", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w2", guard.WRITE) is not None


def test_a_refused_value_a_result_later_prints_is_seen_again():
    guard = _guard()
    window = _window()
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is not None
    window.settled("r", "clean")
    window.settled("w")
    window.next_step()
    window.dispatched("r1", guard.READ, "git log -1")
    window.settled("r1", f"commit {_SHA}")
    window.next_step()
    window.dispatched("r2", guard.READ, "git status")
    window.dispatched("w2", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w2", guard.WRITE) is None


def test_a_value_narrated_before_the_batch_does_not_count_for_it():
    # The model streams a guessed sha, then announces a read and a report
    # quoting it: the narration composes this batch, so it is not a source.
    guard = _guard()
    window = _window()
    guard.note_text(window, f"the head is {_SHA}, let me check")
    window.dispatched("r", guard.READ, "git rev-parse HEAD")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is not None


def test_refused_guesses_are_bounded(monkeypatch):
    # A backend re-announcing one write with new guesses cannot grow the window.
    guard = _guard()
    monkeypatch.setattr(guard, "MAX_BATCH_TOKENS", 4)
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "git status")
    for i in range(10):
        window.dispatched("w", guard.WRITE, text=" ".join(f"it_{i:04x}{j:04x}" for j in range(3)))
        window.refusal_for("w", guard.WRITE)
    assert len(window._guessed) <= 4
    assert window.seen.complete is False


def test_a_refused_guess_resent_after_a_turn_reset_is_refused_again():
    # The refused batch is the turn's last; the next turn's reset must not learn
    # the refused write's arguments.
    guard = _guard()
    window = _window()
    window.dispatched("r", guard.READ, "gh pr view 42")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is not None
    window.settled("r", "state OPEN")
    window.settled("w")
    window.reset()
    assert _SHA not in window.seen
    window.dispatched("r2", guard.READ, "gh pr view 42")
    window.dispatched("w2", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w2", guard.WRITE) is not None


def test_a_prompt_echoing_a_refused_shas_prefix_does_not_make_it_seen():
    # The refusal notice quotes a 12-character prefix; steering it back as a
    # prompt must not make that prefix seen.
    guard = _guard()
    window = _window()
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    window.settled("r", "clean")
    window.settled("w")
    window.next_step()
    guard.note_prompt(window, reason)
    window.dispatched("r2", guard.READ, "git status")
    window.dispatched("w2", guard.WRITE, text=_report(f"head {_SHA[:12]}"))
    assert window.refusal_for("w2", guard.WRITE) is not None


def test_a_long_cut_of_a_refused_sha_echoed_back_is_not_learned():
    # A refused sha cut to 13-39 characters and echoed (a prompt quoting 20
    # characters, model text quoting 16) is still the refused guess: neither the
    # long cut nor the short sha it contains becomes seen.
    guard = _guard()
    window = _window()
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    assert window.refusal_for("w", guard.WRITE) is not None
    window.settled("r", "clean")
    window.settled("w")
    window.next_step()
    guard.note_prompt(window, f"Your call quoting {_SHA[:20]} was refused.")
    guard.note_text(window, f"I guessed {_SHA[:16]} earlier.")
    window.reset()
    for cut in (_SHA[:12], _SHA[:16], _SHA[:20]):
        assert cut not in window.seen
    for n, cut in enumerate((_SHA[:12], _SHA[:20])):
        window.next_step()
        window.dispatched(f"r{n}", guard.READ, "git status")
        window.dispatched(f"w{n}", guard.WRITE, text=_report(f"head {cut}"))
        assert window.refusal_for(f"w{n}", guard.WRITE) is not None


def test_a_write_peer_echoing_its_own_argument_is_not_proof():
    # A merge prints back the PR number it was given; that is no source for a
    # comment on the same PR in the same batch. Neither call is a read, so a
    # batch of writes only is never refused.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("m", guard.WRITE, "gh pr merge 1234 --squash")
    window.dispatched("c", guard.WRITE, "gh pr comment 1234 --body done")
    window.settled("m", "Merged pull request o/r#1234")
    assert window.refusal_for("c", guard.WRITE) is None


def test_the_refused_filter_looks_up_each_token_once(monkeypatch):
    # Thousands of refused shas and a prompt full of hex must not cost
    # tokens x refused comparisons on the event loop.
    guard = _guard()
    calls = [0]

    class _Counted(str):
        def startswith(self, *args, **kwargs):
            calls[0] += 1
            return super().startswith(*args, **kwargs)

    window = guard.BatchWindow()
    for i in range(1, 501):
        window._refused[_Counted(f"{i:08x}" * 5)] = None
    prompt = " ".join(f"{i:08x}{i:08x}ab" for i in range(10001, 10201))
    guard.note_prompt(window, prompt)
    assert calls[0] <= 200
    assert f"{10001:08x}{10001:08x}ab" in window.seen
    # A cut of a refused sha is still caught.
    guard.note_prompt(window, f"echo {1:08x}{1:08x}")
    assert f"{1:08x}{1:08x}" not in window.seen


def test_text_written_after_the_last_batch_is_seen_after_a_reset():
    guard = _guard()
    window = _window()
    window.dispatched("r0", guard.READ, "git status")
    window.settled("r0", "clean")
    guard.note_text(window, f"I will cite it_9fc6cc99 and {_SHA}")
    window.reset()
    assert "it_9fc6cc99" in window.seen
    assert _SHA in window.seen


def test_filling_the_seen_set_past_its_bound_never_rescans_it(monkeypatch):
    # Thousands of full shas in one result must not stall the event loop: each
    # eviction drops the oldest key in constant time. Starting an iterator over
    # the key map per eviction rescans its deleted slots, which is quadratic, so
    # the key map counts every iterator it hands out.
    from collections import OrderedDict

    class _CountingKeys(OrderedDict):
        iterators = 0

        def __iter__(self):
            _CountingKeys.iterators += 1
            return super().__iter__()

    guard = _guard()
    monkeypatch.setattr(guard, "MAX_SEEN_TOKENS", 100)
    seen = guard.SeenTokens()
    seen._keys = _CountingKeys()
    shas = [f"{i:08x}" * 5 for i in range(1, 200)]
    seen.add(shas)
    assert _CountingKeys.iterators == 0
    assert len(seen) == 100
    assert seen.complete is False
    assert shas[-1] in seen
    assert shas[0] not in seen


def test_an_id_past_the_scan_bound_of_a_prompt_does_not_cause_a_refusal():
    # A 300,000-character prompt is scanned to its first 256 KiB; an id after
    # character 262,144 is not seen, so the set must stop claiming completeness.
    guard = _guard()
    window = guard.BatchWindow()
    prompt = "x " * 140000 + " it_0badc0de now"
    assert len(prompt) > 262144 and prompt.index("it_0badc0de") > 262144
    guard.note_prompt(window, prompt)
    assert window.seen.complete is False
    window.dispatched("r", guard.READ, "git status")
    window.dispatched("w", guard.WRITE, text=_report("bound it_0badc0de"))
    assert window.refusal_for("w", guard.WRITE) is None


def test_a_result_past_the_scan_bound_marks_the_set_incomplete():
    guard = _guard()
    window = guard.BatchWindow()
    text = guard.update_text({"content": "y " * 140000})
    window.dispatched("r", guard.READ, "git log")
    window.settled("r", text)
    assert window.seen.complete is False


def test_a_proof_refusal_counts_the_peer_that_printed_the_value():
    # An unclassified peer whose result printed the sha is the one able to
    # return it; the notice must not say none of the peers could.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("x", "", text="review-tool show 7")
    window.dispatched("w", guard.WRITE, text=_report(f"head {_SHA}"))
    window.settled("x", f"head {_SHA}")
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    assert "1 of them able to return" in reason


# -- a session with the guard on from its start keeps its seen set complete --


def _gated_window_on_from_start(monkeypatch):
    """A transport's gated window for a session that was never resumed, with
    ``hooks.batch_write_guard`` on when its first prompt is sent."""
    import asyncio

    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    window = guard.BatchWindow(gated=True)
    asyncio.run(guard.refresh_feeding(window))
    return guard, window


def _run_gated_batch(guard, window, n: int) -> None:
    """One normal batch: three reads and a write, each with a small result."""
    for i in range(3):
        call = f"read-{n}-{i}"
        window.dispatched(call, guard.READ, f"gh pr view {1000 + n * 10 + i}")
        window.settled(call, f"PR #{1000 + n * 10 + i} head {n:02x}{i:02x}" + "ab" * 19)
    window.dispatched(f"write-{n}", guard.WRITE, text=_report(f"progress on item it_{n:08x}"))
    window.settled(f"write-{n}", "ok")


def test_a_guard_on_from_the_session_start_keeps_the_set_complete_after_one_batch(monkeypatch):
    guard, window = _gated_window_on_from_start(monkeypatch)
    guard.note_prompt(window, "please look at PR #1500 and report")
    _run_gated_batch(guard, window, 1)
    window.reset()  # the batch ends: its arguments and results join the seen set
    assert window.seen.dropped is False
    assert window.seen.resumed is False
    assert window.seen.complete is True
    assert len(window.seen) > 0


def test_a_guard_on_from_the_session_start_keeps_the_set_complete_over_three_batches(monkeypatch):
    guard, window = _gated_window_on_from_start(monkeypatch)
    guard.note_prompt(window, "please look at PR #1500 and report")
    for n in range(1, 4):
        _run_gated_batch(guard, window, n)
        # The next batch's first call ends this one.
        assert window.seen.dropped is False
        assert window.seen.complete is True
    window.reset()
    assert window.seen.dropped is False
    assert window.seen.complete is True
    assert "1010" in window.seen and "1030" in window.seen


def test_a_batch_past_the_seen_bound_drops_tokens_and_marks_the_set_incomplete(monkeypatch):
    guard, window = _gated_window_on_from_start(monkeypatch)
    monkeypatch.setattr(guard, "MAX_SEEN_TOKENS", 4)
    guard.note_prompt(window, "please look at PR #1500 and report")
    _run_gated_batch(guard, window, 1)
    window.reset()
    assert window.seen.dropped is True
    assert window.seen.complete is False
