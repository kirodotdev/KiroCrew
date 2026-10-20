"""The design_tweak preview check and the deploy scan run the credential scan in linear time.

The PEM branch of the credential pattern ends in a lazy full-block tail that reads to the end
of the text when no END marker follows. A text that repeats the header paid that read once per
header: quadratic. Both callers now take their matches from ``redaction._credential_matches``,
which never lets such a header reach the tail. These tests count the scans the full-block
pattern runs, which does not depend on the clock, and pin that a real key is still refused
and a quoted marker with no key body is still let through by the preview.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import server
from kiro_crew.deploy import scan
from kiro_crew.security import redaction

# The markers are assembled from their dashes, so no line of this file holds a whole
# private-key header for a secret scanner to read.
_D = "-----"
_B = f"{_D}BEGIN PRIVATE KEY{_D}"
_BR = f"{_D}BEGIN RSA PRIVATE KEY{_D}"
_ER = f"{_D}END RSA PRIVATE KEY{_D}"
_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"


def _preview(text: str) -> bool:
    """The preview server's own check, on the real runtime module."""
    return server._contains_credential(text.encode())


def _scan(text: str) -> list[scan.Finding]:
    return scan.scan_content(text)


class _CountingPattern:
    """A compiled pattern that counts the scans (``match``, ``search``, ``finditer``) run through it."""

    def __init__(self, real: re.Pattern[str]) -> None:
        self._real = real
        self.matches = 0

    def match(self, text: str, pos: int = 0) -> re.Match[str] | None:
        self.matches += 1
        return self._real.match(text, pos)

    def search(self, text: str, pos: int = 0) -> re.Match[str] | None:
        self.matches += 1
        return self._real.search(text, pos)

    def finditer(self, text: str, pos: int = 0) -> Any:
        self.matches += 1
        return self._real.finditer(text, pos)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
def full_pattern(monkeypatch: pytest.MonkeyPatch) -> _CountingPattern:
    """The full alternation, with the PEM full-block tail, counted."""
    full = _CountingPattern(redaction._CREDENTIAL_PATTERNS)
    monkeypatch.setattr(redaction, "_CREDENTIAL_PATTERNS", full)
    return full


@pytest.mark.parametrize("headers", [500, 2_000, 8_000])
def test_preview_never_runs_a_repeated_header_through_the_full_block_tail(
    full_pattern: _CountingPattern, headers: int
) -> None:
    """No header is run through the pattern that reads on to an END marker, which is what
    made the raw scan quadratic. The raw ``finditer`` over the whole text counts as a run."""
    _preview((_B + "\n") * headers)
    assert full_pattern.matches == 0


@pytest.mark.parametrize("headers", [500, 2_000, 8_000])
def test_deploy_scan_never_runs_a_repeated_header_through_the_full_block_tail(
    monkeypatch: pytest.MonkeyPatch, full_pattern: _CountingPattern, headers: int
) -> None:
    """The scan takes its credential matches from the shared scanner, once per scan, and the
    full-block pattern never sees a repeated header. The raw pattern it replaced is a
    module-level list the scan walked with ``finditer``, so this fails on that code."""
    calls: list[str] = []
    real = redaction._credential_matches

    def spy(text: str) -> Any:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(scan, "_credential_matches", spy)
    assert _scan((_B + "\n") * headers)
    assert len(calls) == 1
    assert full_pattern.matches == 0


@pytest.mark.timeout(60)
def test_both_callers_finish_a_large_text_of_repeated_headers() -> None:
    """Hang protection only: 3 MiB of unterminated headers did not finish under the raw
    pattern. The count tests above carry the linearity claim."""
    text = (_B + "\n") * (3 * 1024 * 1024 // (len(_B) + 1))
    assert _preview(text) is False
    assert _scan(text)


def test_both_callers_still_refuse_a_real_private_key_block() -> None:
    block = f"{_BR}\n{_BODY}\n{_BODY}\n{_ER}\n"
    assert _preview(block) is True
    assert _preview(f"cert notes\n{block}trailing text") is True
    assert any(f.severity == "credential" for f in _scan(block))


def test_both_callers_still_refuse_a_real_block_after_many_headers() -> None:
    text = (_B + "\n") * 1_000 + f"{_BR}\n{_BODY}\n{_ER}\n"
    assert _preview(text) is True
    assert any(f.severity == "credential" for f in _scan(text))


def test_preview_still_allows_a_quoted_marker_with_no_key_body() -> None:
    assert _preview(f"<p>Paste {_BR} here</p>") is False
    assert _preview(f"{_B}\n") is False
    assert _preview((_B + "\n") * 1_000) is False


def test_deploy_scan_still_flags_a_quoted_marker() -> None:
    """The deploy scan has its own header pattern, so a bare marker is a finding there too."""
    findings = _scan(f"<p>Paste {_BR} here</p>")
    assert findings
    assert all(f.kind == "credential" for f in findings)


def test_other_credentials_are_still_found_by_both_callers() -> None:
    text = "aws key AKIAIOSFODNN7EXAMPLE in a page"
    assert _preview(text) is True
    assert any(f.kind == "credential" for f in _scan(text))
