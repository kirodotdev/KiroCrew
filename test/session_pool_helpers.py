"""The isolation fixture and stand-ins the warm session pool tests share.

``test_session_pool.py`` and its ``_reaping`` and ``_refill`` siblings import these by
name: ``_isolate_config_dir``, the autouse fixture that points ``KIROCREW_HOME`` at a
per-test directory (imported, it applies to every test of the importing module);
``_make_cfg``, a config stub carrying the pool knobs; ``_make_provider``, a provider
stand-in whose start, shutdown and liveness are mocks; and ``_make_manager``, a
``SessionManager`` built on both with the workspace directory stubbed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; it only sets KIROCREW_HOME, and no test in a module that imports it calls monkeypatch.undo()
def _isolate_config_dir(tmp_path, monkeypatch):
    """Point config_dir() at a throwaway dir so SessionManager's SessionMap
    writes to a per-test ``session_map.json`` instead of the real
    ``~/.kirocrew/session_map.json``.

    Without this, every test reuses key ``"test-key"``: a pool-claim test
    persists a ``claude_code`` session_map entry (which ``SessionMap.get``
    returns without a kiro-file existence check), and a later test reading
    the same key then sees a truthy ``resume_sid`` and bypasses the warm
    pool — making ``assert provider is pooled`` fail nondeterministically
    under xdist. Isolating config_dir also stops the suite from polluting
    the developer's real ``~/.kirocrew``.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew_home"))


def _make_cfg(
    pool_size: int = 2, pool_agent: str = "kirocrew", pool_ttl_secs: int = 1800
) -> MagicMock:
    cfg = MagicMock()
    cfg.session.pool_size = pool_size
    cfg.session.pool_agent = pool_agent
    cfg.session.pool_ttl_secs = pool_ttl_secs
    cfg.session.timeout_secs = 3600
    cfg.agent.default_agent = ""
    cfg.agent.model = "auto"  # match real KiroCrewConfig default
    return cfg


def _make_provider() -> MagicMock:
    p = MagicMock()
    p.start = AsyncMock()
    p.shutdown = AsyncMock()
    p.is_process_alive = MagicMock(return_value=True)
    p.exit_code = None
    # session_map persistence reads provider.cwd (the LLMProvider ABC accessor);
    # a bare MagicMock returns a non-serializable Mock, so pin it to a string
    # like a real provider with no work dir.
    p.cwd = ""
    return p


def _make_manager(pool_size: int = 2, pool_agent: str = "kirocrew", pool_ttl_secs: int = 1800):
    from kiro_crew.session import SessionManager

    cfg = _make_cfg(pool_size, pool_agent, pool_ttl_secs)
    factory = MagicMock(side_effect=lambda *a, **kw: _make_provider())
    with patch(
        "kiro_crew.session.default_project_dir", return_value="/home/user/.kirocrew/workspace"
    ):
        mgr = SessionManager(cfg, provider_factory=factory)
    return mgr, factory
