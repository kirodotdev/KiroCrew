"""The credential give-up line redacts the provider text it carries.

``credential_give_up_text`` is the one place the dead-end line is built, and the
dashboard row and the channel post that show it do not redact on their own. So
the line is scrubbed here, for every surface, through the egress redaction path,
which finds link and credential spans on the original text and rewrites it once.
The frame's trailing ``data`` repr is machine data and is dropped.
"""

from __future__ import annotations

import pytest

from kiro_crew.llm_helpers import credential_give_up_text

#: The AWS documentation's own example access key id: credential-shaped, not real.
_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"
#: A link the link redactor removes: a presigned-style query on a storage host.
_LINK_HOST = "example-bucket.s3.us-east-1.amazonaws.com"
_LINK = (
    f"https://{_LINK_HOST}/report?X-Amz-Credential={_EXAMPLE_KEY}%2Fx"
    "&X-Amz-Signature=" + "b" * 64
)
_PROVIDER_TEXT = (
    "Internal error: API Error: Could not load AWS credentials · "
    f"see {_LINK} · key {_EXAMPLE_KEY} was refused. "
    "Check or refresh your AWS credentials and try again."
)
#: A link whose query carries a ``token`` parameter after a long one. The link
#: pass and the credential pass each remove part of it, so both run over the
#: original text and the line keeps neither part.
_TOKEN_VALUE = "opaque-value-xyz"
_TOKEN_LINK_TEXT = (
    "Internal error: API Error: Could not load AWS credentials · "
    f"see https://collector.example/?pad={'a' * 40}&token=){_TOKEN_VALUE} · "
    "Check or refresh your AWS credentials and try again."
)
#: Each provider text, with the values its give-up line must not show.
_CASES = {
    "link-and-key": (_PROVIDER_TEXT, (_EXAMPLE_KEY, "X-Amz-Signature")),
    "token-parameter": (_TOKEN_LINK_TEXT, (_TOKEN_VALUE,)),
}


def _raw_credential_error(case: str = "link-and-key") -> Exception:
    """A credential-reload ``AcpError`` whose text was never redacted."""
    from kiro_crew.acp.client import AcpError

    err = AcpError(_CASES[case][0], transient=False)
    err.credential_reload = True
    return err


def _assert_scrubbed(text: str, case: str = "link-and-key") -> None:
    for value in _CASES[case][1]:
        assert value not in text
    assert "[REDACTED" in text
    # The provider's remedy, the one actionable sentence, survives.
    assert "Check or refresh your AWS credentials" in text


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.parametrize("source", ["exception", "string"])
def test_give_up_text_redacts_links_and_credentials(source, case):
    exc = _raw_credential_error(case)
    text = credential_give_up_text(exc if source == "exception" else str(exc))

    _assert_scrubbed(text, case)


def test_give_up_text_drops_the_frame_data_repr():
    """The text the ACP formatter builds for a credential frame ends on the
    frame's ``data`` dict; the user-facing line ends on the provider's words."""
    from kiro_crew.acp.client import AcpError
    from kiro_crew.acp.transport_errors import _raise_acp_error

    with pytest.raises(AcpError) as ei:
        _raise_acp_error(
            {
                "code": -32603,
                "message": (
                    "Internal error: API Error: Could not load AWS credentials · "
                    "Check or refresh your AWS credentials and try again."
                ),
                "data": {"errorKind": "cloud_credential_error"},
            }
        )
    assert "errorKind" in str(ei.value)

    text = credential_give_up_text(ei.value)

    assert "errorKind" not in text
    assert text.endswith("Check or refresh your AWS credentials and try again.")


def test_give_up_text_keeps_a_mid_text_brace():
    """Only the trailing frame repr is dropped; provider prose stays whole."""
    text = credential_give_up_text("Profile {default} has no credentials. Run aws sso login.")

    assert text.endswith("Profile {default} has no credentials. Run aws sso login.")


@pytest.mark.timeout(60)
def test_give_up_text_is_linear_on_a_long_whitespace_run():
    """A long whitespace run in provider text does not make the frame-tail match
    restart at every space. The quadratic form needs minutes on this input, so
    it reaches the timeout; the linear one finishes in milliseconds."""
    text = "Could not load credentials" + " " * 400_000 + "and try again."

    assert credential_give_up_text(text).endswith("and try again.")

    tail = "try again." + " " * 50 + ": {'errorKind': 'cloud_credential_error'}"
    assert credential_give_up_text(tail).endswith("try again.")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_CASES))
async def test_dashboard_give_up_row_is_redacted(tmp_path, monkeypatch, case):
    """The dashboard chat ends a twice-failed credential load on a redacted row."""
    from test_chat_session_not_found_recovery import _client, _drain, _errors, _state, _wire

    from kiro_crew.dashboard.chat import _run_chat

    async def _fail(msg):
        raise _raw_credential_error(case)
        yield  # pragma: no cover

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_fail))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    await _run_chat(state, slot, "continue")
    await _drain(state)

    errors = _errors(slot)
    give_up = [e for e in errors if "could not load its cloud credentials after a restart" in e]
    assert give_up, errors
    _assert_scrubbed(give_up[-1], case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_CASES))
async def test_channel_give_up_post_is_redacted(case):
    """The channel agent ends a twice-failed credential load on a redacted post."""
    import asyncio

    from test_channel_prompt_busy import FakeSessions, _raising_client

    from kiro_crew import channel as channel_mod

    ch = channel_mod.Channel(id="c1", topic="review")
    agent = ch.add_agent(role="dev", agent_name="dev")
    assert agent is not None
    sessions = FakeSessions(
        [
            _raising_client(_raw_credential_error(case)),
            _raising_client(_raw_credential_error(case)),
        ]
    )

    task = asyncio.create_task(channel_mod.run_channel_agent(agent, ch, sessions))
    await ch.post("human", "start the review")
    await asyncio.wait_for(task, timeout=30)

    posted = [m.content for m in ch.messages if m.from_id == agent.id]
    give_up = [p for p in posted if "could not load its cloud credentials after a restart" in p]
    assert give_up, posted
    _assert_scrubbed(give_up[-1], case)
