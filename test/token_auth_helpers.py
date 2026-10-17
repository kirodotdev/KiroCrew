"""The autouse fixture and request helpers shared by the ``test_token_auth`` family.

``clear_nonces`` is the autouse fixture every file of the family imports: it points the
config dir at the test's tmp path and resets the token state, the revocation
generation, the revoked-nonce store and the app-permission cache around each test.
``_make_request`` builds the mock aiohttp request the middleware tests send, and
``_ok_handler`` is the handler they route it to; every file but
``test_token_auth_signing_secret.py`` imports those two as well. The family is
``test_token_auth.py`` and its siblings ``test_token_auth_signing_secret.py``,
``test_token_auth_session_lifecycle.py``, ``test_token_auth_spa_shell.py``,
``test_token_auth_credential_scope.py`` and ``test_token_auth_tailnet_pin.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; three pre-existing tests of the family call monkeypatch.undo(), tracked for a de-flake pass
def clear_nonces(tmp_path, monkeypatch):
    """Isolate token state per test.

    Points config_dir at a tmp dir so the persisted revocation-generation file
    is not written to the real ~/.kirocrew, resets the in-process gen to 0, and
    clears the nonce store. Uses _state.clear_all() (not revoke_all_sessions)
    so the gen isn't bumped between unrelated tests.
    """
    import kiro_crew.dashboard.revocation_gen as _rg
    import kiro_crew.dashboard.token_auth as _ta

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    # Pin the memoized revocation generation to 0 (skips the lazy disk load)
    # so no persisted counter from a prior test or the real home leaks in.
    monkeypatch.setattr(_rg, "_gen", 0)
    # Reset the per-session revoked-nonce store so each test gets a fresh store
    # bound to its own tmp_path config_dir (the singleton would otherwise pin
    # the first test's path and leak revocations across tests).
    monkeypatch.setattr(_ta, "_revoked_store_singleton", None)
    _ta._state.clear_all()
    _ta._app_perms_cache.clear()
    yield
    monkeypatch.setattr(_rg, "_gen", 0)
    monkeypatch.setattr(_ta, "_revoked_store_singleton", None)
    _ta._state.clear_all()
    _ta._app_perms_cache.clear()


# -- Middleware helpers --


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def _make_request(
    path: str = "/",
    query: dict | None = None,
    cookies: dict | None = None,
    remote: str = "127.0.0.1",
    headers: dict | None = None,
    method: str = "GET",
) -> MagicMock:
    """Build a mock aiohttp request."""
    req = MagicMock(spec=web.Request)
    req.path = path
    req.query = query or {}
    req.cookies = cookies or {}
    req.remote = remote
    req.headers = headers or {}
    req.method = method
    return req
