"""How many ``toolsSettings.shell.allowedCommands`` entries Kiro Crew applies on KAS,
and what it says about the ones it does not.

Kiro Crew answers a KAS shell request from the spec's list itself, so the list never
reaches the engine and the bound on it is Kiro Crew's own. A list within the bound
works as written. Past it the first entries still apply, the session-start notice
names each entry that does not by its position and text, and ``denyByDefault``
reads the same applied list. An entry that is cut, or that cannot be read, allows
nothing. The authored ``permissions`` shell globs are bounded by their own block
and are never cut by this bound. Each entry's text is read once and kept
(``allowed_commands._read_text``), so the gate does not read the list again on
every shell call.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_kas_allowed_commands import _judge, _no_profiles, _shell  # noqa: F401
from test_kas_deny_by_default import _judge as _judge_own
from test_kas_deny_by_default import _own_request

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import (
    _MAX_ALLOWED_COMMANDS,
    CREW_ANSWERED_COMMANDS_KEY,
    crew_answered_commands,
    spec_shell_allowlist,
)
from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice
from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY
from kiro_crew.platform import allowed_commands

LIMIT = _MAX_ALLOWED_COMMANDS
_KEY = "toolsSettings.shell.allowedCommands"
_GRANT = "spec-builtin-grant"


def _entries(count: int) -> list[str]:
    """``tool1 .*`` to ``tool<count> .*``: entry *n* sits at written position *n*."""
    return [f"tool{n} .*" for n in range(1, count + 1)]


def _spec(**extra: Any) -> dict[str, Any]:
    return {"name": "a", "prompt": "p", "tools": ["*"], **extra}


def _shell_settings(entries: list[Any], **shell: Any) -> dict[str, Any]:
    return {"shell": {"allowedCommands": entries, **shell}}


def _answered(entries: list[Any], **extra: Any) -> list[str]:
    """What the projection of a spec listing *entries* records for the gate."""
    spec = _spec(toolsSettings=_shell_settings(entries), **extra)
    return kas_agents.to_client_custom_agent("a", spec, "p").get(CREW_ANSWERED_COMMANDS_KEY, [])


def _lost(entries: list[Any], **extra: Any) -> list[str]:
    spec = _spec(toolsSettings=_shell_settings(entries), **extra)
    return kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)


def _granted(command: str, patterns: list[str]) -> bool:
    return _judge(_shell(command), patterns).tier == _GRANT


# ── a list within the bound works as written ─────────────────────────────────


def test_the_bound_is_the_documented_one() -> None:
    assert LIMIT == 1024


@pytest.mark.parametrize("count", [64, 65, 200, LIMIT])
def test_every_entry_of_a_list_within_the_bound_applies(count: int) -> None:
    entries = _entries(count)
    answered = _answered(entries)
    assert answered == entries
    assert kas_agents.projected_crew_answered_commands(
        [
            kas_agents.to_client_custom_agent(
                "a", _spec(toolsSettings=_shell_settings(entries)), "p"
            )
        ],
        "a",
    ) == tuple(entries)
    assert _lost(entries) == []
    assert _granted("tool1 --version", answered)
    assert _granted(f"tool{count} --version", answered)
    # A longer list allows nothing its entries do not name.
    unlisted = _judge(_shell(f"tool{count + 1} --version"), answered)
    assert (unlisted.action, unlisted.tier) == (TOOL_ALLOW, "")


def test_the_reported_list_answers_its_late_entries() -> None:
    """139 entries: ``realpath .*`` was the first one cut, ``dig .*`` sat near 117."""
    entries = _entries(139)
    entries[64] = "realpath .*"
    entries[116] = "dig .*"
    answered = _answered(entries)
    assert len(answered) == 139 and _lost(entries) == []
    assert _granted("dig +short example.com", answered)
    assert _granted("realpath .", answered)


def test_a_chain_is_granted_when_entries_far_apart_match_its_parts() -> None:
    answered = _answered(_entries(200))
    assert _granted("tool1 build && tool200 test", answered)
    assert not _granted("tool1 build && tool201 test", answered)


# ── past the bound: the first entries apply and the rest are named ────────────


def test_one_entry_past_the_bound_is_named_and_the_rest_apply() -> None:
    entries = _entries(LIMIT + 1)
    answered = _answered(entries)
    assert answered == entries[:LIMIT]
    assert _granted(f"tool{LIMIT} x", answered)
    cut = _judge(_shell(f"tool{LIMIT + 1} x"), answered)
    assert (cut.action, cut.tier) == (TOOL_ALLOW, "")
    lost = _lost(entries)
    assert lost == [f"{_KEY} past the limit: entry {LIMIT + 1}: tool{LIMIT + 1} .*"]
    notice = _spec_keys_notice("a", lost)
    assert f"sets more {_KEY} entries than the {LIMIT} Kiro Crew applies" in notice
    assert f"Its first {LIMIT} readable entries apply" in notice
    assert f"These do not: entry {LIMIT + 1}: tool{LIMIT + 1} .*." in notice
    assert "no effect" not in notice and "does not receive" not in notice
    assert "kirocrew doctor" not in notice


def test_a_long_tail_is_named_by_its_first_entries_and_counted() -> None:
    entries = _entries(LIMIT + 50)
    long_entry = "tool-long " + "x" * 990
    entries[LIMIT + 1] = long_entry
    (item,) = _lost(entries)
    for position in range(LIMIT + 1, LIMIT + 11):
        assert f"entry {position}: " in item
    assert f"entry {LIMIT + 11}: " not in item
    assert item.endswith(f"; and 40 more after entry {LIMIT + 10}")
    assert f"entry {LIMIT + 2}: {long_entry[:80]}...; " in item
    assert long_entry[:81] not in item
    assert len(_spec_keys_notice("a", [item])) < 1500


def test_a_repeated_entry_is_counted_once_and_keeps_its_first_place() -> None:
    entries = _entries(LIMIT)
    repeats = [*entries[:40], *entries, *entries[-40:]]
    assert _answered(repeats) == entries
    assert _lost(repeats) == []
    # Two more copies of the first entry move every later entry two places down.
    shifted = [entries[0], entries[0], *_entries(LIMIT + 1)]
    assert _answered(shifted) == entries
    assert _lost(shifted) == [f"{_KEY} past the limit: entry {LIMIT + 3}: tool{LIMIT + 1} .*"]
    # A cut entry written again later is still named at its first place.
    again = [*_entries(LIMIT + 1), f"tool{LIMIT + 1} .*"]
    assert _lost(again) == [f"{_KEY} past the limit: entry {LIMIT + 1}: tool{LIMIT + 1} .*"]


def test_an_unreadable_entry_takes_no_place_and_allows_nothing() -> None:
    entries = ["(?x)bad", 7, *_entries(LIMIT)]
    answered = _answered(entries)
    assert answered == _entries(LIMIT)
    lost = _lost(entries)
    assert [key.partition(": ")[0] for key in lost] == [_KEY, _KEY]
    notice = _spec_keys_notice("a", lost)
    assert "the agent's other entries still apply" in notice and "past the limit" not in notice
    assert not _granted("bad", answered) and not _granted("7", answered)
    assert not allowed_commands.matches_any(["(?x)bad", 7], "bad")


def test_unreadable_and_cut_entries_are_each_named_in_one_notice() -> None:
    entries = ["(?x)bad", *_entries(LIMIT + 1)]
    notice = _spec_keys_notice("a", _lost(entries))
    assert f"{_KEY}: (?x)bad (" in notice
    assert f"These do not: entry {LIMIT + 2}: tool{LIMIT + 1} .*." in notice
    # The cut entries do not apply, so the first sentence does not say the others do.
    assert "other entries still apply" not in notice
    assert "no effect" not in notice


def test_a_list_the_session_answers_none_of_is_not_said_to_apply_in_part() -> None:
    """An authored ``ask`` withholds the whole list: nothing applies, and the notice
    says so without the sentence about the first entries."""
    ask = {"rules": [{"capability": "shell", "effect": "ask"}]}
    entries = _entries(LIMIT + 1)
    assert _answered(entries, permissions=ask) == []
    lost = _lost(entries, permissions=ask)
    assert _KEY in lost
    notice = _spec_keys_notice("a", lost)
    assert f"sets {_KEY}, which this backend does not receive, so they have no effect" in notice
    assert "readable entries apply" not in notice and "past the limit" not in notice


def test_a_withheld_list_the_gate_still_enforces_has_its_cut_entries_named() -> None:
    """With ``denyByDefault`` the gate enforces the first entries as the allowlist
    even when the session answers none of them, so the cut ones are still named:
    a command only they match is refused."""
    ask = {"rules": [{"capability": "shell", "effect": "ask"}]}
    entries = _entries(LIMIT + 1)
    settings = _shell_settings(entries, denyByDefault=True)
    spec = _spec(toolsSettings=settings, permissions=ask)
    assert CREW_ANSWERED_COMMANDS_KEY not in kas_agents.to_client_custom_agent("a", spec, "p")
    lost = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    assert f"{_KEY} (read by Crew's gate)" in lost and _KEY not in lost
    notice = _spec_keys_notice("a", lost)
    assert "Kiro Crew's own approval gate still enforces them" in notice
    assert f"These do not: entry {LIMIT + 1}: tool{LIMIT + 1} .*." in notice
    assert "no effect" not in notice
    assert _judge_own(_own_request(f"tool{LIMIT + 1} x", settings)).action == TOOL_DENY


def test_the_fix_the_notice_suggests_is_one_kiro_crew_reads() -> None:
    assert "(dig|host) .*" in _spec_keys_notice("a", _lost(_entries(LIMIT + 1)))
    assert allowed_commands.matches_any(["(dig|host) .*"], "dig +short example.com")
    assert allowed_commands.matches_any(["(dig|host) .*"], "host example.com")
    assert not allowed_commands.matches_any(["(dig|host) .*"], "nslookup example.com")


# ── denyByDefault reads the same applied list ────────────────────────────────


@pytest.mark.parametrize("count", [65, 200])
def test_deny_by_default_does_not_refuse_a_late_listed_command(count: int) -> None:
    settings = _shell_settings(_entries(count), denyByDefault=True)
    assert spec_shell_allowlist(settings) == _entries(count)
    assert _judge_own(_own_request(f"tool{count} x", settings)).action != TOOL_DENY
    refused = _judge_own(_own_request("mkfifo /tmp/probe", settings))
    assert (refused.action, refused.tier) == (TOOL_DENY, "spec-shell-allowlist")


def test_deny_by_default_refuses_what_only_a_cut_entry_names() -> None:
    entries = _entries(LIMIT + 1)
    settings = _shell_settings(entries, denyByDefault=True)
    assert spec_shell_allowlist(settings) == entries[:LIMIT]
    assert _judge_own(_own_request(f"tool{LIMIT} x", settings)).action != TOOL_DENY
    assert _judge_own(_own_request(f"tool{LIMIT + 1} x", settings)).action == TOOL_DENY
    lost = kas_agents.spec_keys_without_carrier(
        _spec(toolsSettings=settings), translates_tools_settings=True
    )
    notice = _spec_keys_notice("a", lost)
    assert f"These do not: entry {LIMIT + 1}: tool{LIMIT + 1} .*." in notice
    assert "no effect" not in notice


# ── the authored `permissions` globs keep their own bound ────────────────────


def _allow(match: list[str]) -> dict[str, Any]:
    return {"capability": "shell", "effect": "allow", "match": match}


def test_authored_globs_are_not_cut_by_the_allowed_commands_bound() -> None:
    globs = [f"glob{n} *" for n in range(10)]
    patterns = [f"glob{n} .*" for n in range(10)]
    block = {"rules": [_allow(globs)]}
    full = _entries(LIMIT + 5)
    answered = crew_answered_commands(_shell_settings(full), block)
    assert answered == [*full[:LIMIT], *patterns]
    assert _granted("glob9 run", answered)
    assert _answered(_entries(60), permissions=block) == [*_entries(60), *patterns]


def test_a_rule_with_more_globs_than_one_kas_rule_holds_keeps_every_glob() -> None:
    globs = [f"glob{n} *" for n in range(70)]
    answered = _answered([], permissions={"rules": [_allow(globs)]})
    assert answered == [f"glob{n} .*" for n in range(70)]
    assert _granted("glob69 run", answered)


def test_a_permissions_block_past_its_own_bound_is_named_not_cut() -> None:
    """Over 200 rules the block is refused whole: it grants nothing, and the notice
    names ``permissions`` and says why."""
    block = {"rules": [_allow([f"glob{n} *"]) for n in range(201)]}
    spec = _spec(permissions=block)
    assert crew_answered_commands({}, block) == []
    assert CREW_ANSWERED_COMMANDS_KEY not in kas_agents.to_client_custom_agent("a", spec, "p")
    lost = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    assert "permissions" in lost
    notice = _spec_keys_notice("a", lost)
    assert "A permissions block is not sent at all" in notice and "too large" in notice


# ── each entry's text is read once ───────────────────────────────────────────


@pytest.fixture
def _empty_read_cache():
    allowed_commands._read_text.cache_clear()
    yield
    allowed_commands._read_text.cache_clear()


def test_an_entry_is_compiled_once_however_often_it_is_asked_about(
    _empty_read_cache: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled: list[tuple] = []
    compile_tree = allowed_commands._compile

    def counting(tree: tuple):
        compiled.append(tree)
        return compile_tree(tree)

    monkeypatch.setattr(allowed_commands, "_compile", counting)
    entries = ["git (status|diff)( .*)?", r"ls [\w./-]+"]
    for _ in range(5):
        assert allowed_commands.chain_matches_all(entries, "git status && ls src")
        assert allowed_commands.matches_any(entries, "git diff --stat")
        assert allowed_commands.refusal_reason(entries[0]) is None
    assert len(compiled) == 2
    assert allowed_commands.parse(entries[0]) is allowed_commands.parse(entries[0])


def test_the_cache_holds_a_full_list_and_is_bounded(_empty_read_cache: None) -> None:
    size = allowed_commands._read_text.cache_info().maxsize
    assert size is not None and size >= LIMIT


_TEXTS = [
    "git status",
    "git status ",
    "Git status",
    "git status .*",
    "git statu(s)",
    "git (status|diff)",
    "(?i)git status",
    "1",
    "1.0",
    "(?x)bad",
    "(bad",
    "\tgit status",
]


@pytest.mark.parametrize("order", [_TEXTS, _TEXTS[::-1]], ids=["as-listed", "reversed"])
def test_two_different_texts_never_share_an_answer(
    _empty_read_cache: None, order: list[str]
) -> None:
    """Each text read after the others still gets the answer a first read gives it."""
    fresh = {text: allowed_commands._read_text.__wrapped__(text) for text in _TEXTS}
    assert len({repr(answer) for answer in fresh.values()}) > len(_TEXTS) // 2
    for _ in range(2):
        for text in order:
            assert allowed_commands._read(text) == fresh[text]
    assert allowed_commands.matches_any(["git status"], "git status")
    assert not allowed_commands.matches_any(["Git status"], "git status")
    assert not allowed_commands.matches_any(["git status "], "git status")


def test_a_changed_list_is_read_fresh(_empty_read_cache: None) -> None:
    before, after = ["make build", "git status"], ["make build", "git stash"]
    for _ in range(2):
        assert allowed_commands.matches_any(before, "git status")
        assert not allowed_commands.matches_any(after, "git status")
        assert allowed_commands.matches_any(after, "git stash")
        assert not allowed_commands.matches_any(before, "git stash")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (7, "not a string"),
        (None, "not a string"),
        (["git status"], "not a string"),
        ({"git status": 1}, "not a string"),
        ("", "empty"),
        ("x" * (allowed_commands.MAX_PATTERN_LENGTH + 1), "longer than 1024 characters"),
    ],
    ids=["int", "none", "list", "dict", "empty", "too-long"],
)
def test_an_entry_refused_before_the_cache_is_refused_on_every_call(
    _empty_read_cache: None, raw: Any, reason: str
) -> None:
    # A readable text is kept first, so a kept answer is there to be handed out.
    assert allowed_commands.parse("git status") is not None
    for _ in range(3):
        assert allowed_commands.parse(raw) is None
        assert allowed_commands.refusal_reason(raw) == reason
        assert not allowed_commands.matches_any([raw], "git status")
    assert allowed_commands._read_text.cache_info().currsize == 1


def test_a_number_is_not_read_as_its_spelling(_empty_read_cache: None) -> None:
    assert allowed_commands.parse("7") is not None
    assert allowed_commands.parse(7) is None and allowed_commands.parse(7.0) is None
    assert allowed_commands.parse("7") is not None
