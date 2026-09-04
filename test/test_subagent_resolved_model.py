"""The model a spawned sub-agent actually ran on is read back and surfaced.

A model-pinned review whose real model is unverifiable is not much of a pin
(#3582). ``SubagentManager`` reads the live session's PUBLIC ``served_model``
accessor and carries the resolved id on ``SubagentInfo.resolved_model``, which
rides the ``subagent_spawn`` / ``subagent_done`` frames and the completion meta.

These tests pin the reader's contract: it uses ``served_model`` (never private
client internals), filters the ``"auto"`` request-sentinel to ``""``
(unknown/inconclusive, never a wildcard), and never raises on a duck-typed or
model-less client.
"""

from __future__ import annotations

from kiro_crew.subagent import _resolved_model_of


class _Client:
    """Minimal provider double exposing only the public ``served_model``."""

    def __init__(self, served: str) -> None:
        self.served_model = served


def test_reads_the_served_model_off_the_public_accessor() -> None:
    assert _resolved_model_of(_Client("claude-opus-4.8")) == "claude-opus-4.8"


def test_auto_sentinel_is_normalized_to_unknown() -> None:
    # "auto" is the REQUEST sentinel ("let the backend pick"); read back it means
    # the served id is not known yet, so it must never render as the model.
    assert _resolved_model_of(_Client("auto")) == ""


def test_empty_served_model_is_unknown() -> None:
    assert _resolved_model_of(_Client("")) == ""


def test_whitespace_is_stripped() -> None:
    assert _resolved_model_of(_Client("  gpt-5.6-sol  ")) == "gpt-5.6-sol"


def test_never_reads_private_client_internals() -> None:
    # A raw client that exposes only the PRIVATE _resolved_model_id (no public
    # served_model) yields "" — the reader deliberately does not reach through
    # internals, matching AcpProvider.served_model. (The raw-client path is
    # bridged by the provider's own served_model, not by this helper poking at
    # _resolved_model_id.)
    class _RawOnly:
        _resolved_model_id = "gpt-5.6-sol"

    assert _resolved_model_of(_RawOnly()) == ""


def test_model_less_or_duck_typed_client_never_raises() -> None:
    class _NoModel:
        pass

    assert _resolved_model_of(_NoModel()) == ""
    assert _resolved_model_of(None) == ""
    assert _resolved_model_of(object()) == ""


def test_non_string_served_model_is_coerced_safely() -> None:
    # A provider double that returns a non-string (e.g. a MagicMock attribute)
    # must not crash the spawn path; anything non-empty-string-ish that is not
    # the sentinel is stringified, and a falsy value collapses to "".
    assert _resolved_model_of(_Client(0)) == ""  # type: ignore[arg-type]
