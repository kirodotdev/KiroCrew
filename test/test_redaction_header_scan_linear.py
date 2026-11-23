"""A text that repeats an API-key header or a ``curl -u`` flag costs the header scan linear
time, with the same output.

``detect_headers`` reads a value after every ``x-api-key`` (and the other API-key header
names) and every ``-u`` / ``--user``. ``=`` and ``-`` are token characters, so on
``x-api-key=x-api-key=…`` every anchor's value ran to the same far end and its placeholder
check read it whole; and ``curl -u`` sliced and split its whole line head once per flag.
Both are quadratic, and the dashboard redacts pages of up to 3 MiB. These readers now share
the cookie reader's walk: one finder per stop set and the bounded value check, and the
line's first three words found once. These tests pin that the output is unchanged and that
the work is linear.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from redaction_structured_cases import assert_linear_work

from kiro_crew.security.redaction_structured import headers, redact

_TAG = "[REDACTED: credential]"
_K = "A1b2C3d4E5f6G7h8"

# (input, redacted text), captured from the scan before the walk was shared.
SHAPES: dict[str, tuple[str, str]] = {
    "api key colon": (f"x-api-key: {_K}", f"x-api-key: {_TAG}"),
    "api key json": (f'"x-api-key": "{_K}"', f'"x-api-key": "{_TAG}"'),
    "api key assign": (f"X-API-Key={_K}", f"X-API-Key={_TAG}"),
    "api key placeholder": ("x-api-key: your-api-key-here", "x-api-key: your-api-key-here"),
    "api key mask": ("x-api-key: xxxxxxxxxxxxxxxx", "x-api-key: xxxxxxxxxxxxxxxx"),
    "two api key names": (
        f"api-key: {_K}; x-auth-token: {_K}Z",
        f"api-key: {_TAG}; x-auth-token: {_TAG}",
    ),
    "api key inside a value": (
        f"private-token={_K}=x-api-key={_K}",
        f"private-token={_TAG}",
    ),
    "repeated api key": ("x-api-key=" * 3 + _K, f"x-api-key={_TAG}"),
    "bearer": (f"Authorization: Bearer {_K}", f"Authorization: Bearer {_TAG}"),
    "basic placeholder": (
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
    ),
    "basic": (
        "Authorization: Basic dXNlcjpzM2NyM3RwNHNzVzByZA==",
        f"Authorization: Basic {_TAG}",
    ),
    "curl user": (f"curl -u admin:{_K} https://h", f"curl -u admin:{_TAG} https://h"),
    "curl quoted": (
        f'curl -s -u "admin:{_K} x" https://h',
        f'curl -s -u "admin:{_TAG}" https://h',
    ),
    "curl long flag": (
        f"curl --user=admin:{_K} https://h",
        f"curl --user=admin:{_TAG} https://h",
    ),
    "curl path": (f"/usr/bin/curl -u admin:{_K}", f"/usr/bin/curl -u admin:{_TAG}"),
    "curl after pipe": (f"echo hi | curl -u admin:{_K}", f"echo hi | curl -u admin:{_TAG}"),
    "curl third word": (f"sudo curl -u admin:{_K}", f"sudo curl -u admin:{_TAG}"),
    # ``curl`` must be one of the first three words of the line, as a whole word.
    "curl fourth word": (f"a b c curl -u admin:{_K}", f"a b c curl -u admin:{_K}"),
    "curl suffix": (f"xcurl -u admin:{_K}", f"xcurl -u admin:{_K}"),
    "curl glued flag": (f"curl-u admin:{_K}", f"curl-u admin:{_K}"),
    "curl next line": (
        f"curl -u admin:{_K}\nwget -u admin:{_K}",
        f"curl -u admin:{_TAG}\nwget -u admin:{_K}",
    ),
    "curl repeated flag": (f"curl -u=-u=admin:{_K}", f"curl -u=-u=admin:{_TAG}"),
    "curl placeholder": ("curl -u admin:password", "curl -u admin:password"),
    "curl already masked": (f"curl -u admin:{_TAG}", f"curl -u admin:{_TAG}"),
    "curl two quotes": (
        f"curl -u 'admin:{_K}' -u \"root:{_K}Z\"",
        f"curl -u 'admin:{_TAG}' -u \"root:{_TAG}\"",
    ),
}


@pytest.mark.parametrize("name", SHAPES)
def test_every_header_shape_redacts_as_before(name: str) -> None:
    text, redacted = SHAPES[name]
    assert redact(text)[0] == redacted


def _fill(unit: str, lead: str = "") -> Callable[[int], str]:
    return lambda n: lead + (unit * (n // len(unit) + 1))[:n]


# Whole-text shapes where one value (or one line head) runs the length of the text. The
# streamed form holds a value that has not ended yet (sibling R3, a separate item) and is not
# covered here.
_WHOLE_SHAPES: dict[str, Callable[[int], str]] = {
    "api_key_eq": _fill("x-api-key="),
    "api_key_mask": _fill("x-api-key=xxxxxxxx="),
    "api_key_your": _fill("x-api-key=your="),
    "private_token_eq": _fill("private-token="),
    "curl_u_blank": _fill("-u ", lead="curl "),
    "u_blank_no_curl": _fill("-u "),
    "curl_u_eq": _fill("-u=", lead="curl "),
    "curl_user_eq": _fill("--user=", lead="curl "),
    "curl_u_colon": _fill("-u=a:", lead="curl "),
    "curl_u_quote": _fill('-u "', lead="curl "),
}


@pytest.mark.parametrize("build", [pytest.param(b, id=n) for n, b in _WHOLE_SHAPES.items()])
def test_repeated_header_anchor_costs_linear_work_whole(build: Callable[[int], str]) -> None:
    assert_linear_work(build, redact)


@pytest.mark.parametrize("lead", ["curl ", ""])
def test_curl_line_head_is_split_once_per_line(lead: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The line-head read per ``-u`` was a C-level slice and split, which the line count
    above does not see. Count the head reads instead: each walk splits a line's head once,
    however many flags the line holds, and the output is unchanged (no flag is followed by
    ``user:password``)."""
    real = headers._Walk.runs_curl
    anchors = 0
    reads: list[tuple[int, int]] = []

    def spy(self: headers._Walk, at: int) -> bool:
        nonlocal anchors
        anchors += 1
        before = self._head_lo
        out = real(self, at)
        if self._head_lo != before:
            reads.append((id(self), self._head_lo))
        return out

    monkeypatch.setattr(headers._Walk, "runs_curl", spy)
    line = _fill("-u ", lead=lead)(1 << 16)
    text = line + "\n" + line
    assert redact(text)[0] == text
    assert anchors > 10_000
    assert len(reads) == len(set(reads)), "a line head was split again"
    assert {lo for _walk, lo in reads} == {0, len(line) + 1}
