"""``kirocrew-computer`` and ``kirocrew-dashboard`` resolve the calling session
from the injected caller block.

The exact state #4622 described and fixed for ``kirocrew-cron``: both servers
already routed identity through :func:`mcp_core._resolve_session_key_strict`,
whose FIRST source is the gateway-injected per-call caller block — but neither
advertised ``kirocrew.caller-identity``, and ``mcp_gateway/backend.py`` strips
any stub-supplied block from every forwarded request and re-injects its own only
for a backend that advertised. Nothing declines to pool an unadvertised backend
(``rewriter.UNPOOLABLE_SERVERS`` is empty), so not advertising bought a shared
process that never received an identity: pooled and identity-blind.

The gateway half — that gatewayd injects the block into a backend that
advertises, and that co-tenant sessions arrive distinguishable — is proven
end-to-end by ``test_mcp_gateway_pool_integ.py``
(``test_two_stubs_on_one_backend_are_told_apart``), and the advertisement itself
is ratcheted against ``_MANAGED_SERVERS_CALLER_AWARE`` by
``test_mcp_managed_caller_identity.py``. What THIS file pins is the server half
for these two servers, mirroring ``test_mcp_cron_caller_identity.py``:

1. The caller block WINS over the process environment — asserted with the
   environment naming a DIFFERENT session, because a test that merely reads the
   block back would also pass if the block were being ignored in favour of an
   environment that happened to agree.
2. With no block, the environment still resolves, so a non-gateway launch is
   not regressed.
3. Each server's documented no-identity posture is preserved: computer-use
   proceeds under an ``unresolved:<pid>`` namespace (refusing was removed by
   product decision), while the dashboard's tree-shaping tools refuse
   (deny-by-default over shared structure).
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import mcp_computer, mcp_dashboard
from kiro_crew.mcp_caller import CallerContext, set_current_caller


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch):
    """No identity of any kind unless a test grants one.

    Identity is granted per test rather than by a shared fixture: part of this
    module is about what happens when there is none.
    """
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    set_current_caller(None)
    yield
    set_current_caller(None)


def _as_session(key: str) -> None:
    """Arrive the way a pooled forwarded call does: carrying a caller block."""
    set_current_caller(CallerContext(session_key=key, session_type="dashboard"))


# --- kirocrew-computer ------------------------------------------------------
#
# The one place identity leaves this server is the ``session_key`` argument of
# ``_invoke`` (it becomes the gateway request's session header), so that is
# where these tests observe it.


@pytest.fixture
def computer_invocations(monkeypatch) -> dict[str, Any]:
    """Enable the feature and capture what ``_call_tool_inner`` forwards."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(mcp_computer.enable_state, "is_enabled", lambda: True)

    def _fake_invoke(session_key: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        captured["session_key"] = session_key
        return {"text": "ok"}

    monkeypatch.setattr(mcp_computer, "_invoke", _fake_invoke)
    return captured


def test_computer_forwards_the_block_identity_over_the_environment(
    monkeypatch, computer_invocations
) -> None:
    """The whole bug in one assertion.

    The environment names a DIFFERENT session than the block. A pooled backend's
    environment can only ever name one session (or, in practice, none), so the
    block has to outrank it rather than merely be consulted.
    """
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")
    _as_session("dashboard:from-block")

    assert mcp_computer._call_tool_inner("computer_get_state", {}) == "ok"
    assert computer_invocations["session_key"] == "dashboard:from-block"


def test_computer_falls_back_to_the_environment_without_a_block(
    monkeypatch, computer_invocations
) -> None:
    """A non-gateway launch has no block to read and must not be regressed."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")

    assert mcp_computer._call_tool_inner("computer_get_state", {}) == "ok"
    assert computer_invocations["session_key"] == "dashboard:from-env"


def test_computer_still_proceeds_unidentified_under_a_process_namespace(
    computer_invocations,
) -> None:
    """No identity is not a refusal here — that posture must survive adoption.

    The unattended-surface rule was removed by product decision: "we could not
    name the session" must not become "you may not drive the desktop". The call
    proceeds under the ``unresolved:<pid>`` namespace, which is honest about
    being a separator rather than an authenticated identity.
    """
    assert mcp_computer._call_tool_inner("computer_get_state", {}) == "ok"
    assert computer_invocations["session_key"].startswith(mcp_computer.UNRESOLVED_SESSION_PREFIX)


# --- kirocrew-dashboard -----------------------------------------------------
#
# Identity decides SCOPE here: which sessions a caller may see, and whether it
# may reshape the shared folder tree at all. Both go through
# ``_resolve_session_key_strict``, so both read the block first.


def _rows(*rows: dict[str, Any]) -> Any:
    """A canned ``_get_rows`` returning *rows* for every endpoint."""
    return lambda _path: (list(rows), None)


def test_dashboard_scopes_the_session_list_by_the_block_identity(
    monkeypatch,
) -> None:
    """Each co-tenant sees its OWN app's sessions, not another's.

    The environment names a session owned by a different app than the block.
    If the environment won, the visible set would be app-b's — one session's
    scope applied to another's call, the co-tenancy failure pooling makes
    possible.
    """
    monkeypatch.setattr(
        mcp_dashboard,
        "_get_rows",
        _rows(
            {"key": "from-block", "app": "app-a"},
            {"key": "from-env", "app": "app-b"},
            {"key": "other-a", "app": "app-a"},
            {"key": "other-b", "app": "app-b"},
        ),
    )
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")
    _as_session("dashboard:from-block")

    visible, err = mcp_dashboard._visible_chat_slots()
    assert err is None
    assert sorted(r["key"] for r in visible) == ["from-block", "other-a"]


def test_dashboard_verifies_tree_writes_against_the_block_identity(
    monkeypatch,
) -> None:
    """The verified key handed to every tree write is the block's, not the env's."""
    monkeypatch.setattr(
        mcp_dashboard,
        "_get_rows",
        _rows({"key": "from-block"}, {"key": "from-env", "app": "app-b"}),
    )
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")
    _as_session("dashboard:from-block")

    caller_key, err = mcp_dashboard._refuse_tree_shaping_if_unverifiable("moving")
    assert err is None
    assert caller_key == "dashboard:from-block"


def test_dashboard_falls_back_to_the_environment_without_a_block(
    monkeypatch,
) -> None:
    """A non-gateway launch has no block to read and must not be regressed."""
    monkeypatch.setattr(mcp_dashboard, "_get_rows", _rows({"key": "from-env"}))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")

    caller_key, err = mcp_dashboard._refuse_tree_shaping_if_unverifiable("moving")
    assert err is None
    assert caller_key == "dashboard:from-env"


def test_dashboard_refuses_an_unidentified_caller(monkeypatch) -> None:
    """Deny-by-default over shared structure — that posture must survive adoption.

    An unidentified caller can still share a pooled backend with identified ones
    (gatewayd forwards ``caller=None`` when a stub registers without a key), so
    empty identity does not imply a 1:1 transport and must not carry authority
    over the shared folder tree.
    """
    monkeypatch.setattr(mcp_dashboard, "_get_rows", _rows())

    caller_key, err = mcp_dashboard._refuse_tree_shaping_if_unverifiable("moving")
    assert caller_key == ""
    assert err is not None and "cannot verify" in err

    visible, list_err = mcp_dashboard._visible_chat_slots()
    assert visible == []
    assert list_err is not None and "cannot verify" in list_err
