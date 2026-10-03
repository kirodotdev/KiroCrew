"""Tests for ``kiro_crew.jwt_header``: the structural half of the shared JWT spelling.

The regex ``JWT_MULTI_SEGMENT`` matches on shape alone, so a hostname that merely
contains ``eyJ`` satisfies it. This module is the ONE place that decides
whether such a hit is a credential, and three consumers import it: the scrubber,
the log floor and the decisions gate. The tests here pin the verdict itself, the
linear scan and the single-home property; each consumer's own tests pin its
behaviour.
"""

from __future__ import annotations

import base64
import importlib
import json
import re
import sys
import time

import pytest

from kiro_crew import jwt_header
from kiro_crew.credential_patterns import JWT_MULTI_SEGMENT
from kiro_crew.jwt_header import JWT_RE, is_json_object_segment, is_jwt_lookalike, jwt_matches


def _b64u(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


JWS_HEADER = _b64u({"alg": "HS256", "typ": "JWT"})
JWE_HEADER = _b64u({"alg": "dir", "enc": "A256GCM"})
PAYLOAD = _b64u({"sub": "1234567890"})
SIGNATURE = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
JWS = f"{JWS_HEADER}.{PAYLOAD}.{SIGNATURE}"
JWE_DIR = f"{JWE_HEADER}..48V1_ALb6US04U3b.5eym8TW_c8SuK0ltJ3rpYIzOeDQz7TALvtu6UG9oMo4vpzs9tX_EFShS8iB7j6ji.XFBoMYUZodetZdvTiFvSkQ"

#: The reproduction rows from the issue, plus the lowercase control that isolates
#: the trigger: one letter of case is the only difference.
ISSUE_HOSTS = (
    "https://honeyJar.atlassian.net/wiki/spaces/ABC/pages/1234567890/Design+Doc",
    "https://moneyJar.atlassian.net/browse/ABC-1",
    "https://disneyJapan.atlassian.net/wiki/x",
    "https://blueyJam.example.com/a/b",
)
LOWERCASE_CONTROL = "https://honeyjar.atlassian.net/wiki/x"


def _spans(text: str) -> list[str]:
    return [m.group() for m in jwt_matches(text)]


class TestTheCompiledSpelling:
    def test_it_is_the_shared_spelling(self) -> None:
        assert JWT_RE.pattern == JWT_MULTI_SEGMENT
        assert JWT_RE.pattern is JWT_MULTI_SEGMENT  # the same string object, not a copy


class TestIsJsonObjectSegment:
    @pytest.mark.parametrize(
        "segment",
        [JWS_HEADER, JWE_HEADER, _b64u({"user": 1}), _b64u({})],
        ids=["jws-alg", "jwe-enc", "itsdangerous-payload", "empty-object"],
    )
    def test_a_json_object_is_a_header(self, segment: str) -> None:
        assert is_json_object_segment(segment) is True

    @pytest.mark.parametrize(
        "segment",
        ["eyJar", "eyJapan", "eyJam", _b64u([1, 2]), _b64u("eyJ"), _b64u(7), "eyJ!!", ""],
        ids=[
            "honeyJar-label",
            "disneyJapan-label",
            "blueyJam-label",
            "json-array",
            "json-string",
            "json-number",
            "not-base64url",
            "empty",
        ],
    )
    def test_anything_else_is_not(self, segment: str) -> None:
        assert is_json_object_segment(segment) is False


class TestIsJwtLookalike:
    def test_a_hostname_hit_is_a_lookalike(self) -> None:
        for text in ISSUE_HOSTS:
            match = JWT_RE.search(text)
            assert match is not None, text  # the shape still matches: the regex is unchanged
            assert is_jwt_lookalike(match) is True, text

    @pytest.mark.parametrize("token", [JWS, JWE_DIR], ids=["jws", "jwe-dir"])
    def test_a_real_token_is_not(self, token: str) -> None:
        match = JWT_RE.search(f"token={token}")
        assert match is not None
        assert match.group() == token
        assert is_jwt_lookalike(match) is False

    def test_a_one_dot_hit_is_never_on_this_branch(self) -> None:
        """The two-segment link token is a different alternative with its own bounds."""
        match = re.match(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "eyJar.payload")
        assert match is not None
        assert is_jwt_lookalike(match) is False

    def test_a_header_holding_a_second_eyj_stays_a_credential(self) -> None:
        """Fail closed: rejecting it would rescan the header once per ``eyJ`` it holds."""
        match = JWT_RE.search("eyJxxeyJyy.aaa.bbb")
        assert match is not None
        assert is_jwt_lookalike(match) is False


class TestJwtMatches:
    def test_the_issue_rows_yield_nothing(self) -> None:
        for text in ISSUE_HOSTS:
            assert _spans(text) == [], text
        assert _spans(LOWERCASE_CONTROL) == []

    @pytest.mark.parametrize(
        "text",
        [
            JWS,
            f"Authorization: Bearer {JWS}",
            f"https://api.example.com/cb?token={JWS}&x=1",
            f"compact=jwt{JWS}",
            json.dumps({"access_token": JWS}),
        ],
        ids=["bare", "bearer", "query", "glued-label", "json"],
    )
    def test_a_real_jws_is_yielded_whole_wherever_it_sits(self, text: str) -> None:
        assert _spans(text) == [JWS]

    def test_a_dir_jwe_with_an_empty_key_segment_is_yielded_whole(self) -> None:
        assert _spans(JWE_DIR) == [JWE_DIR]

    def test_two_tokens_in_one_text_are_both_yielded(self) -> None:
        assert _spans(f"a {JWS} b {JWE_DIR} c") == [JWS, JWE_DIR]

    def test_a_real_token_glued_after_a_lookalike_is_still_found(self) -> None:
        """The regex swallows ``eyJar.<jws>`` as ONE hit whose header is ``eyJar``.

        A scan that resumed at the END of the rejected hit would skip the token.
        Resuming one character on finds it, which is the line the test pins.
        """
        text = f"honeyJar.{JWS}"
        raw = JWT_RE.search(text)
        assert raw is not None and raw.group() == f"eyJar.{JWS}"  # the trap is real
        assert _spans(text) == [JWS]

    def test_a_lookalike_beside_a_real_token_leaves_only_the_token(self) -> None:
        text = f"host honeyJar.example.com token {JWS} done"
        assert [m.span() for m in jwt_matches(text)] == [
            (text.index(JWS), text.index(JWS) + len(JWS))
        ]

    def test_a_lookalike_span_yields_nothing_even_when_it_holds_another_spelling(self) -> None:
        """An AWS key inside a rejected span is another pattern's job, and the
        consumers run that pattern too; this scan yields JWTs only."""
        assert _spans("eyJx.AKIAIOSFODNN7EXAMPLE.y") == []

    def test_dense_eyj_header_is_yielded_whole(self) -> None:
        """A header holding more ``eyJ`` is kept, so the scan never re-walks it."""
        text = ("eyJ" * 2000) + ".a.b"
        assert _spans(text) == [text]

    def test_a_dotless_eyj_run_is_scanned_in_linear_time(self) -> None:
        """``JWT_RE.search`` is quadratic here (measured 6.3 s at 48 KB); this scan is not.

        One anchored attempt per base64url run, then skip the run. CPU time rather
        than wall clock, so a descheduled worker cannot red it; the margin is two
        orders of magnitude below the quadratic cost at this size (~10 s).
        """
        text = "eyJ" * 20_000  # 60 KB, no dot anywhere: nothing can match
        started = time.thread_time()
        assert _spans(text) == []
        assert time.thread_time() - started < 1.0

    def test_a_dotless_run_is_skipped_as_one_step(self) -> None:
        """The invariant behind the linear scan: a failed attempt at the first ``eyJ``
        of a run decides every later ``eyJ`` in it, because the first segment cannot
        stop before the run's end. A token right after the run is still found."""
        text = "eyJ" * 50 + " " + JWS
        assert _spans(text) == [JWS]
        text = "eyJ" * 50 + "." + JWS  # the run ends at a dot: one hit, header is the run
        assert _spans(text) == [f"{'eyJ' * 50}.{JWS}"]  # dense header fails closed


class TestSingleHome:
    """Every consumer reads the verdict from here; a second copy is a drift pair."""

    def test_the_scrubber_and_the_stream_holdback_bind_the_shared_verdict(self) -> None:
        from kiro_crew import security
        from kiro_crew.security import redaction

        assert redaction.is_jwt_lookalike is jwt_header.is_jwt_lookalike
        assert security.is_json_object_segment is jwt_header.is_json_object_segment

    def test_the_log_floor_and_the_gate_bind_the_shared_scan(self) -> None:
        from kiro_crew import log_redaction
        from kiro_crew.decisions import gate

        assert log_redaction.jwt_matches is jwt_header.jwt_matches
        assert log_redaction._JWT_RE is jwt_header.JWT_RE
        assert gate.jwt_matches is jwt_header.jwt_matches
        assert JWT_MULTI_SEGMENT not in gate._CREDENTIAL_RE.pattern  # scanned once, not twice

    def test_a_fresh_import_pulls_in_only_the_spelling_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The log floor imports this at CLI bootstrap, so it must stay a leaf."""
        import kiro_crew

        mod_name = "kiro_crew.jwt_header"
        monkeypatch.setattr(kiro_crew, "jwt_header", sys.modules[mod_name])
        monkeypatch.delitem(sys.modules, mod_name)

        before = set(sys.modules)
        importlib.import_module(mod_name)
        pulled = {name for name in set(sys.modules) - before if name.startswith("kiro_crew")}
        assert pulled <= {
            mod_name,
            "kiro_crew.credential_patterns",
        }, f"jwt_header pulled in: {sorted(pulled - {mod_name})}"
