"""AcpProvider.shutdown forwards the inner session provider's KAS unconfirmed-close
state to the outer wrapper.

``discard_conversation`` (``session_lifecycle_parts/teardown.py``) reads
``session.provider._kas_shutdown_unconfirmed`` -- ``session.provider`` IS the outer
``AcpProvider``, not the inner ``AcpSessionProvider``.  Before the fix the outer
wrapper did not carry those fields, so the getattr always returned False and an
unconfirmed KAS close was silently reported as success.

Remove the forwarding block at the end of ``acp_lifecycle.shutdown`` and both
assertions below fail: the outer provider still has ``_kas_shutdown_unconfirmed=False``
and ``_kas_unconfirmed_session_id=None``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.providers import acp_lifecycle
from kiro_crew.providers.acp import AcpProvider


def _make_fake_provider(kas_unconfirmed: bool) -> MagicMock:
    """Return a MagicMock that looks like an AcpProvider to acp_lifecycle.shutdown.

    Uses a MagicMock so all attribute reads return sensible defaults and we
    don't have to bypass every property in AcpProvider just to test the
    forwarding logic.
    """
    provider = MagicMock(spec=AcpProvider)
    # Defaults that keep shutdown from branching into pi/kas cleanup.
    provider.session_id = "test-session-id"
    provider.memory_mode = "ephemeral"
    provider.is_pi_backend = False
    provider.is_kas_backend = False
    provider._disposable_work_dir = None
    # The three declared class-level defaults (False / None / None).
    provider._kas_shutdown_unconfirmed = False
    provider._kas_unconfirmed_runtime = None
    provider._kas_unconfirmed_session_id = None

    # Build an inner client SimpleNamespace with the confirmed/unconfirmed state.
    inner = SimpleNamespace(
        _kas_shutdown_unconfirmed=kas_unconfirmed,
        _kas_unconfirmed_runtime=(
            SimpleNamespace(evict_session=AsyncMock(return_value=False))
            if kas_unconfirmed
            else None
        ),
        _kas_unconfirmed_session_id="kas-sid-unconfirmed" if kas_unconfirmed else None,
    )
    inner.shutdown = AsyncMock()
    provider._client = inner

    # MagicMock stores attribute assignments on the object so side-effects of
    # the forwarding block (setattr) land on the mock and are readable back.
    return provider


@pytest.mark.asyncio
async def test_outer_provider_forwards_kas_unconfirmed_after_inner_shutdown():
    """After acp_lifecycle.shutdown(), the forwarding block must copy the inner
    session provider's _kas_shutdown_unconfirmed flag (and companions) onto the
    outer AcpProvider wrapper.

    discard_conversation (teardown.py:972-974) reads session.provider._kas_shutdown_unconfirmed
    -- session.provider IS this outer wrapper.  Before the fix that getattr always
    returned False, so an unconfirmed KAS close was silently reported as success.

    Remove the forwarding block from acp_lifecycle.shutdown and both assertions
    fail: _kas_shutdown_unconfirmed stays False and _kas_unconfirmed_session_id
    stays None on the outer provider.
    """
    outer = _make_fake_provider(kas_unconfirmed=True)
    await acp_lifecycle.shutdown(outer)

    assert outer._kas_shutdown_unconfirmed is True, (
        "_kas_shutdown_unconfirmed must be forwarded from inner to outer after "
        "shutdown(); remove the forwarding block from acp_lifecycle.shutdown and "
        "this assertion fails"
    )
    assert outer._kas_unconfirmed_session_id == "kas-sid-unconfirmed", (
        "_kas_unconfirmed_session_id must be forwarded from inner to outer; "
        "without it discard_conversation cannot recover the session handle for a retry"
    )


@pytest.mark.asyncio
async def test_outer_provider_unconfirmed_flag_false_when_inner_confirmed():
    """When the inner shutdown confirms normally, the outer provider must NOT gain
    the unconfirmed flag.

    Without the ``if getattr(inner, '_kas_shutdown_unconfirmed', False)`` guard
    the forwarding block would unconditionally copy the inner's default value and
    this test would still pass -- but removing the guard breaks the semantics so
    this test documents the expectation explicitly.
    """
    outer = _make_fake_provider(kas_unconfirmed=False)
    await acp_lifecycle.shutdown(outer)

    # The mock never had _kas_shutdown_unconfirmed set to True; it must stay False.
    assert outer._kas_shutdown_unconfirmed is False
