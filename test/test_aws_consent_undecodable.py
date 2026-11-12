"""An AWS consent store whose bytes are not UTF-8 reads as "not confirmed".

It is handled like a store that does not parse: every service reads as
unconfirmed, so the gate refuses with its usual reason, and confirming again
from the dashboard copies the old bytes aside and writes a fresh store.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import pytest

from kiro_crew import aws_consent

# Not UTF-8: 0xff never starts a UTF-8 sequence.
_UNDECODABLE = b'\xff\xfe{"polly": {}}'

T = TypeVar("T")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Isolated data home holding a consent store that is not UTF-8."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    from kiro_crew.config.loader import aws_consent_path, config_dir

    config_dir().mkdir(parents=True, exist_ok=True)
    aws_consent._probe_cache.clear()
    path = aws_consent_path()
    path.write_bytes(_UNDECODABLE)
    yield path
    aws_consent._probe_cache.clear()


def _call(fn: Callable[[], T]) -> T:
    """Run *fn*, failing the test (not erroring it) when it raises on the bytes."""
    try:
        return fn()
    except UnicodeDecodeError as exc:
        pytest.fail(f"a store that is not UTF-8 raised {exc!r}")


def test_a_store_that_is_not_utf8_grants_nothing(store) -> None:
    assert _call(lambda: aws_consent.read_grant(aws_consent.SERVICE_POLLY)) is None
    granted, reason = _call(
        lambda: aws_consent.is_granted(aws_consent.SERVICE_POLLY, profile="", region="us-east-1")
    )
    assert granted is False
    assert "has not been confirmed" in reason


def test_confirming_again_replaces_a_store_that_is_not_utf8(store) -> None:
    _call(
        lambda: aws_consent.record_grant(
            aws_consent.SERVICE_POLLY,
            profile="",
            region="us-east-1",
            account="1234",
            arn="arn:aws:iam::1234:user/x",
            granted_at="2026-08-21T00:00:00+00:00",
        )
    )

    sidecars = list(store.parent.glob(f"{store.name}.corrupt-*"))
    assert len(sidecars) == 1, sidecars
    assert sidecars[0].read_bytes() == _UNDECODABLE
    assert aws_consent.read_grant(aws_consent.SERVICE_POLLY) is not None
