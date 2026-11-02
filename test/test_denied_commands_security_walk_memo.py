"""``is_denied`` shares one payload walk per distinct text within a decision.

Moved beside the other ``test_denied_commands_security_<topic>.py`` modules, which
share ``denied_commands_security_helpers.py``.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import argv_floor_self as _argv_floor_self
from kiro_crew.security import is_denied
from kiro_crew.security import shell_commands as _shell_commands

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _payload_heavy_heredoc(lines: int) -> str:
    """A heredoc-fed script dense in the shell machinery the payload walk reads.

    It reaches every floor that walks (``python3 -`` reads a script from stdin, so
    the self-floor gate opens) while no floor fires, which is the shape where an
    unshared walk is paid once per floor.
    """
    return "git add f && python3 - <<'PY'\n" + "print('a $b \"c\" `d` $(e) f')\n" * lines + "PY"


#: One command per structural floor plus benign and near-miss shapes, so the
#: differential below compares verdicts on the paths the shared walk feeds and on
#: the ones it must leave alone. Continuation-joined spellings are included because
#: the self-subcommand floors walk the joined text, a different key in the memo.
_WALK_SHARING_CORPUS: tuple[str, ...] = (
    "git status",
    "ls -la /tmp/foo",
    "git push origin main",
    "git push origin feat/walk-once",
    "bash -c 'git push origin main'",
    "eval 'git push origin main'",
    "kirocrew token",
    'bash -c "kirocrew token"',
    "echo $(kirocrew token)",
    "cat <(kirocrew token)",
    "pkill -f kirocrew",
    "kill $(pgrep -f kirocrew)",
    "kirocrew restart",
    "kiro\\\ncrew restart",
    "kirocrew update",
    "kirocrew gateway restart",
    "kirocrew cloud destroy",
    "kirocrew file-delivery approve",
    "rg -n 'chmod|chown' /etc/profile.d",
    "rm -rf /",
    'bash -c "rm -rf /"',
    _payload_heavy_heredoc(3),
    "python3 - <<'PY'\nimport os; os.system('kirocrew token')\nPY",
)


class TestOneDecisionSharesEachPayloadWalk:
    """``is_denied`` walks each distinct text once per decision, and only once.

    Every structural floor descends the command's nested payloads; unshared, each one
    re-tokenizes the whole command, so one decision on a long command pays the
    tokenizer's cost once per floor. The walk is shared inside
    the per-call cache ``shell_commands.sharing_views`` opens for one call.

    Ratcheted on the WORK the walk does -- how many times the uncached walk runs --
    never on timing, which flips under load: a regression that brings back a walk
    per floor fails the count on every host.
    """

    @staticmethod
    def _spy_walks(monkeypatch) -> tuple[Counter[str], Counter[str]]:
        """``(requests, walks)``: texts the floors asked for, and texts actually walked."""
        from kiro_crew.security import shell_normalizer

        requests: Counter[str] = Counter()
        walks: Counter[str] = Counter()
        memoized = shell_normalizer._shell_payload_walk
        uncached = shell_normalizer._shell_payload_walk_uncached

        def request_spy(text: str):
            requests[text] += 1
            return memoized(text)

        def walk_spy(text: str):
            walks[text] += 1
            return uncached(text)

        # ``argv_floor_self`` imports the walk by name, so its floors call that binding.
        monkeypatch.setattr(_argv_floor, "_shell_payload_walk", request_spy, raising=False)
        monkeypatch.setattr(_argv_floor_self, "_shell_payload_walk", request_spy)
        monkeypatch.setattr(shell_normalizer, "_shell_payload_walk_uncached", walk_spy)
        return requests, walks

    def test_one_decision_walks_each_text_once(self, monkeypatch):
        requests, walks = self._spy_walks(monkeypatch)

        assert is_denied(_payload_heavy_heredoc(20)) is None

        # Precondition: several floors asked for the same walk, so the assertion
        # below is about sharing and cannot pass merely because one floor walked.
        assert sum(requests.values()) > len(requests) >= 1, requests
        assert set(walks) == set(requests)
        assert all(count == 1 for count in walks.values()), walks

    def test_a_later_decision_walks_again(self, monkeypatch):
        """Nothing carries between decisions: the gate holds no state across calls."""
        _requests, walks = self._spy_walks(monkeypatch)
        command = _payload_heavy_heredoc(5)

        is_denied(command)
        is_denied(command)

        assert walks[command.lower()] == 2
        assert _shell_commands._CALL_VIEWS.get() is None

    def test_each_distinct_text_keeps_its_own_walk(self, monkeypatch):
        """A continuation-joined view is a second text, walked once in its own right.

        The self-subcommand floors walk the command with its line continuations
        joined, so one decision asks for two different texts. Each must be answered
        with ITS walk: a memo that answered by position rather than by text would
        hand the joined view the unjoined frames.
        """
        from kiro_crew.security import shell_normalizer

        requests, walks = self._spy_walks(monkeypatch)

        assert is_denied("git status \\\n&& kirocrew update")

        assert len(requests) == 2, requests
        assert walks == Counter({text: 1 for text in requests})

        first, second = "kirocrew restart", "bash -c 'git push origin main'"
        with _one_call_scope():
            for text in (first, second, first):
                assert shell_normalizer._shell_payload_walk(
                    text
                ) == shell_normalizer._shell_payload_walk_uncached(text)

    def test_a_shared_walk_hands_out_fresh_lists(self):
        """A consumer that edits the argv it was handed cannot reach the next one."""
        from kiro_crew.security import shell_normalizer

        command = 'bash -c "kirocrew token" && echo $(pgrep -f kirocrew)'
        with _one_call_scope():
            first = shell_normalizer._shell_payload_walk(command)
            first[0][1].append("tampered")
            first.append(("tampered", ["tampered"]))
            second = shell_normalizer._shell_payload_walk(command)

        assert second == shell_normalizer._shell_payload_walk_uncached(command)

    def test_a_walk_that_raises_is_not_remembered(self, monkeypatch):
        from kiro_crew.security import shell_normalizer

        real = shell_normalizer._shell_payload_walk_uncached
        attempts = {"n": 0}

        def flaky(text: str):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("walk failed")
            return real(text)

        monkeypatch.setattr(shell_normalizer, "_shell_payload_walk_uncached", flaky)
        with pytest.raises(RuntimeError):
            with _one_call_scope():
                with pytest.raises(RuntimeError):
                    shell_normalizer._shell_payload_walk("kirocrew token")
                assert shell_normalizer._shell_payload_walk("kirocrew token") == real(
                    "kirocrew token"
                )
                raise RuntimeError("the block itself fails")

        assert attempts["n"] == 2
        assert _shell_commands._CALL_VIEWS.get() is None

    def test_verdicts_match_an_unshared_walk_on_the_floor_corpus(self):
        """The memo changes what a decision costs, never what it decides."""
        verdicts = {command: is_denied(command) for command in _WALK_SHARING_CORPUS}
        with _without_walk_sharing():
            unshared = {command: is_denied(command) for command in _WALK_SHARING_CORPUS}

        assert verdicts == unshared
        # Precondition: the corpus exercises denials from several floors AND
        # allows, so equality is not the trivial agreement of two empty answers.
        denied = [reason for reason in verdicts.values() if reason]
        assert len(denied) >= 8, verdicts
        assert any(reason is None for reason in verdicts.values())

    @given(
        fragments=st.lists(
            st.sampled_from(_WALK_SHARING_CORPUS + ("&&", ";", "|", "'", '"', "$(", ")", "`")),
            min_size=1,
            max_size=6,
        ),
        glue=st.sampled_from((" ", "", "\n", " && ")),
    )
    def test_verdicts_match_an_unshared_walk_on_composed_commands(self, fragments, glue):
        command = glue.join(fragments)
        shared = is_denied(command)
        with _without_walk_sharing():
            unshared = is_denied(command)
        assert shared == unshared, command


@contextlib.contextmanager
def _one_call_scope():
    """The per-call scope ``sharing_views`` opens around one ``is_denied`` call, as a block."""
    previous = _shell_commands._CALL_VIEWS.get()
    _shell_commands._CALL_VIEWS.set({})
    try:
        yield
    finally:
        _shell_commands._CALL_VIEWS.set(previous)


@contextlib.contextmanager
def _without_walk_sharing():
    """Every walk runs uncached: the reference a shared-walk decision must agree with."""
    from kiro_crew.security import shell_normalizer

    with mock.patch.object(shell_normalizer, "_payload_walk_memo", lambda: None):
        yield
