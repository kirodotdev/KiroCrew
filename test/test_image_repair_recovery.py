"""The provider's many-image 2000px dimension rejection, and its recovery.

When a conversation holds many images, every image must be <= 2000px on its
longest edge, and one oversized image already in the native history rejects the
whole turn with

    messages.30.content.1.image.source.base64.data: At least one of the image
    dimensions exceed max allowed size for many-image requests: 2000 pixels

Resume replays the identical history and fails identically. Unlike a context
overflow this is recoverable WITHOUT losing turns: the oversized images can be
downscaled in place in the transcript. These tests pin the three things that
follow:

- the frame is tagged ``image_dimension_wedge`` and ``structural_terminal`` at
  raise time and is never transient, so neither the same-model retry ladder nor
  a self-driving loop re-sends it;
- the user-facing text says why and names the repair fix, not a fresh session;
- the dashboard's terminal row carries the ``image_repair`` kind, which is what
  the error card reads to offer "Repair images and resume" instead of Resume,
  and that kind outranks ``context_overflow`` (the two are exclusive, but the
  repair fix is the specific one).
"""

from __future__ import annotations

import pytest

from kiro_crew.acp.client import (
    AcpError,
    _format_acp_error,
    _is_transient_raw_error,
    _raise_acp_error,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO
from kiro_crew.dashboard.chat_utils import IMAGE_REPAIR_KIND

_MANY_IMAGE = {
    "code": -32603,
    "message": "internal error",
    "data": (
        "messages.30.content.1.image.source.base64.data: At least one of the image "
        "dimensions exceed max allowed size for many-image requests: 2000 pixels "
        "(request_id: b209a047-432f-431c-866f-fe0dfc66f147)"
    ),
}

# The same verdict with a single image (the "dimension" singular the provider
# also emits) must match too.
_MANY_IMAGE_SINGULAR = {
    "code": -32603,
    "message": "internal error",
    "data": ("image dimension exceed max allowed size for many-image requests: 2000 pixels"),
}


def _raised(error: dict) -> AcpError:
    with pytest.raises(AcpError) as excinfo:
        _raise_acp_error(error)
    return excinfo.value


@pytest.mark.parametrize("frame", [_MANY_IMAGE, _MANY_IMAGE_SINGULAR])
class TestTheWedgeIsTagged:
    def test_it_carries_the_image_dimension_wedge_tag(self, frame):
        assert _raised(frame).image_dimension_wedge is True

    def test_it_is_structural_so_a_self_driving_loop_stops(self, frame):
        assert _raised(frame).structural_terminal is True

    def test_it_is_never_transient(self, frame):
        assert _is_transient_raw_error(frame) is False
        assert _raised(frame).transient is False

    def test_it_is_not_also_a_plain_image_format_rejection(self, frame):
        # Distinct tag from the unsupported-format sibling, so the card offers
        # repair rather than the format sibling's "start a new conversation".
        assert getattr(_raised(frame), "image_format_unsupported", False) is False


class TestTheWedgeIsScopedToTheDataField:
    def test_a_phrase_echo_in_message_alone_does_not_tag(self):
        """Scoped to ``data`` like its image siblings: a tool's stderr echoing
        the words in ``message`` must not retag an unrelated error."""
        frame = {
            "code": -32603,
            "message": (
                "tool failed: image dimensions exceed max allowed size for " "many-image requests"
            ),
            "data": {"errorKind": "unknown"},
        }
        assert getattr(_raised(frame), "image_dimension_wedge", False) is False

    def test_a_co_occurring_retry_hint_cannot_rescue_it(self):
        frame = dict(_MANY_IMAGE, data=_MANY_IMAGE["data"] + " Please try again.")
        assert _is_transient_raw_error(frame) is False


class TestTheUserIsToldTheRepairFix:
    def test_the_text_names_the_cause_and_the_repair(self):
        out = _format_acp_error(_MANY_IMAGE)
        assert "2000" in out
        # Names the remedy -- repairing/downscaling the images in place -- but
        # NO specific control or command: the formatter is surface-blind and
        # reaches the CLI, Slack and the dashboard alike, so it must not tell a
        # surface to press a button or run a command it does not have.
        assert "repair" in out.lower()
        assert "repair-images" not in out
        assert "error card" not in out.lower()
        assert "kirocrew session" not in out
        assert "Retrying will not help" in out
        assert "switching models" not in out
        # The request_id is preserved for support correlation.
        assert "b209a047" in out


class TestTheRepairTagIsKiroOnly:
    """The REPAIR rewrites kiro-cli's own transcript, so the dashboard-repair
    tag is set only for the kiro-cli backend. Under another harness the error
    stays terminal and structural but carries no repair affordance (AGENTS.md
    Harness parity: a remedy written for Kiro is not offered to a harness it
    cannot help). ``ACP_BACKEND_KIRO`` is the empty string, so an unset backend
    is kiro."""

    def test_the_kiro_backend_gets_the_repair_tag(self):
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(_MANY_IMAGE, backend=ACP_BACKEND_KIRO)
        assert excinfo.value.image_dimension_wedge is True

    def test_the_default_backend_is_kiro_and_gets_the_tag(self):
        assert _raised(_MANY_IMAGE).image_dimension_wedge is True

    def test_a_non_kiro_backend_gets_no_repair_tag_but_stays_terminal(self):
        with pytest.raises(AcpError) as excinfo:
            _raise_acp_error(_MANY_IMAGE, backend=ACP_BACKEND_CLAUDE)
        err = excinfo.value
        assert getattr(err, "image_dimension_wedge", False) is False
        # Still terminal and structural -- re-sending the same history fails on
        # any provider, so the self-wake loop must still stop.
        assert err.structural_terminal is True
        assert err.transient is False

    def test_the_kiro_wording_names_the_in_place_repair(self):
        # On Kiro the dashboard offers the Repair card, so the prose names the
        # in-place repair as the recovery.
        out = _format_acp_error(_MANY_IMAGE, backend=ACP_BACKEND_KIRO)
        assert "repairing the oversized images in place" in out
        assert "2000" in out

    def test_a_non_kiro_harness_is_told_to_start_a_new_conversation(self):
        # A non-Kiro harness gets no Repair button, so telling it to repair in
        # place would name a recovery it cannot run. It is told to start a new
        # conversation -- the recovery it does have -- instead.
        out = _format_acp_error(_MANY_IMAGE, backend=ACP_BACKEND_CLAUDE)
        assert "repairing the oversized images in place" not in out
        assert "start a new conversation" in out.lower()
        assert "2000" in out


class TestTheDashboardRowCarriesTheKind:
    def test_terminal_meta_is_image_repair(self):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta

        assert _terminal_error_meta(_raised(_MANY_IMAGE)) == {"kind": IMAGE_REPAIR_KIND}

    def test_it_outranks_context_overflow_when_both_are_somehow_set(self):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta

        err = _raised(_MANY_IMAGE)
        err.context_overflow = True  # should not happen at raise time; pin the order
        assert _terminal_error_meta(err) == {"kind": IMAGE_REPAIR_KIND}

    def test_the_sign_in_fix_outranks_it(self):
        from kiro_crew.dashboard.chat_runner import _terminal_error_meta
        from kiro_crew.dashboard.chat_utils import AUTH_REQUIRED_KIND

        err = _raised(_MANY_IMAGE)
        err.auth_required = True
        assert _terminal_error_meta(err) == {"kind": AUTH_REQUIRED_KIND}
