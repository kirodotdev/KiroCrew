"""The credential plan with the structured detectors read first (``redaction_structured.compat``).

Covers: arbitrary text never raises; spans stay sorted and disjoint; Secret hits
are removed and the plan's passes still run after them; a detector failure
replaces the whole text with a value-free record and a metric; the internal
switch is off by default; table rule ids reach records under their old names.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from redaction_structured_cases import github_classic, session_token

from kiro_crew.security import redaction
from kiro_crew.security.redaction_structured import compat

any_text = st.text(max_size=10_000)


@pytest.fixture
def table_on() -> Iterator[None]:
    before = compat.use_rule_table()
    compat.publish_use_rule_table(True)
    try:
        yield
    finally:
        compat.publish_use_rule_table(before)


@pytest.fixture
def broken_table(monkeypatch: pytest.MonkeyPatch, table_on: None) -> list[str]:
    """The detectors raise on every read; returns the metric names counted."""
    counted: list[str] = []

    def boom(*_a: object, **_k: object) -> list[object]:
        raise ValueError("detectors broke")

    monkeypatch.setattr(compat, "secret_hits", boom)
    monkeypatch.setattr(compat, "_count_fail_closed", lambda: counted.append("fail_closed"))
    return counted


def _github_classic() -> str:
    """A ``ghp_`` token whose CRC32 suffix is valid, built at run time."""
    return github_classic(seed=3)


class TestArbitraryTextNeverRaises:
    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(text=any_text)
    def test_plan_never_raises(self, text: str) -> None:
        spans, warnings, rules = compat.credential_plan_via_table(text)
        assert isinstance(spans, list)
        assert isinstance(warnings, list)
        assert isinstance(rules, dict)
        assert all(start in rules for start, _end, _tag in spans)

    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(text=any_text)
    def test_spans_are_sorted_and_disjoint(self, text: str) -> None:
        spans, _, _ = compat.credential_plan_via_table(text)
        cursor = 0
        for start, end, _tag in spans:
            assert cursor <= start <= end <= len(text)
            cursor = end

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(before=any_text, after=any_text)
    def test_a_checksummed_token_is_always_removed(self, before: str, after: str) -> None:
        token = _github_classic()
        text = f"{before} {token} {after}"
        spans, _, _ = compat.credential_plan_via_table(text)
        start = text.index(token, len(before))
        assert any(s <= start and start + len(token) <= e for s, e, _t in spans)


class TestKnownSecrets:
    def test_checksummed_github_token_keeps_its_old_rule(self, table_on: None) -> None:
        token = _github_classic()
        text, warnings, matches = redaction.redact_credentials_with_records(f"t {token} x")
        assert token not in text
        assert warnings == [f"Redacted credential pattern ({len(token)} chars)"]
        assert [(m.rule, m.label, m.value) for m in matches] == [("github_token", "", token)]

    def test_aws_session_token_keeps_its_old_rule(self, table_on: None) -> None:
        value = session_token(seed=5)
        text, _, matches = redaction.redact_credentials_with_records(f"aws_session_token={value}")
        assert value not in text
        assert [(m.rule, m.value) for m in matches] == [("aws_session_token", value)]

    def test_the_passes_still_run_after_the_detectors(self, table_on: None) -> None:
        # A 40-character run with no key ID nearby is no detector's hit; the
        # bare secret pass that runs after the detectors still removes it.
        secret = "Kx3Q51tPusVbN8mZr2LwTq7HjYcD4fGaE9sRkU0p"
        text, warnings = redaction.redact_credentials(f"x {secret} y")
        assert secret not in text
        assert warnings == ["Redacted bare secret key (40 chars)"]

    def test_a_pass_span_over_a_tag_widens_to_the_whole_hit(self, table_on: None) -> None:
        token = _github_classic()
        text = f"Authorization: Bearer {token} done"
        spans, _, _ = compat.credential_plan_via_table(text)
        start = text.index(token)
        assert any(s <= start and start + len(token) <= e for s, e, _t in spans)
        cursor = 0
        for s, e, _t in spans:
            assert cursor <= s < e
            cursor = e

    def test_prose_is_unchanged(self, table_on: None) -> None:
        text = "Nothing secret here, just words: on a line."
        assert redaction.redact_credentials(text) == (text, [])


class TestFailClosed:
    TEXT = "token gh" + "p_" + "a" * 36 + " and prose"

    def test_whole_text_is_replaced(self, broken_table: list[str]) -> None:
        text, warnings = redaction.redact_credentials(self.TEXT)
        assert text == redaction.REDACTION_UNAVAILABLE_TAG
        assert warnings == ["Redaction unavailable: whole text replaced"]
        assert broken_table == ["fail_closed"]

    def test_record_carries_no_value(self, broken_table: list[str]) -> None:
        text, _, matches = redaction.redact_credentials_with_records(self.TEXT)
        assert text == redaction.REDACTION_UNAVAILABLE_TAG
        assert [tuple(m) for m in matches] == [(0, redaction.REDACTION_UNAVAILABLE_RULE, "", "")]

    def test_log_names_only_the_error_type(
        self, broken_table: list[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger=compat.__name__):
            redaction.redact_credentials(self.TEXT)
        assert "ValueError" in caplog.text
        assert "ghp_" not in caplog.text and "detectors broke" not in caplog.text

    def test_load_failure_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, table_on: None
    ) -> None:
        monkeypatch.setattr(compat, "_LOAD_FAILED", True)
        monkeypatch.setattr(compat, "_count_fail_closed", lambda: None)
        assert redaction.redact_credentials(self.TEXT)[0] == redaction.REDACTION_UNAVAILABLE_TAG

    def test_a_stream_holds_nothing_when_the_detectors_cannot_load(
        self, monkeypatch: pytest.MonkeyPatch, table_on: None
    ) -> None:
        monkeypatch.setattr(compat, "_LOAD_FAILED", True)
        assert compat.stream_read("", "some text") is None


class TestSetting:
    def test_off_by_default_and_off_runs_the_passes_alone(self) -> None:
        assert compat.use_rule_table() is False
        token = _github_classic()
        text = f"t {token} AKIAIOSFODNN7EXAMPLE"
        assert redaction._credential_redaction_plan(text) == redaction._main_plan(text, False, ())

    def test_every_old_rule_name_has_a_tier(self) -> None:
        from kiro_crew.credential_patterns import RULE_TIERS

        names = set(compat._OLD_RULE.values()) | {
            redaction.REDACTION_UNAVAILABLE_RULE,
            compat.old_rule("no.such.rule"),
        }
        assert names <= set(RULE_TIERS)

    def test_every_old_rule_key_is_a_table_secret_row(self) -> None:
        from kiro_crew.security.redaction_structured.model import SECRET
        from kiro_crew.security.redaction_structured.table import ROWS

        secret_ids = {row["id"] for row in ROWS if row["tier"] == SECRET}
        assert set(compat._OLD_RULE) <= secret_ids
