"""Fixtures and helpers shared by the ``test_deploy_web_handlers*.py`` modules.

The three autouse fixtures isolate every deploy handler test: the handlers' Windows
platform gate is neutralised, config resolves under ``tmp_path`` and the base-stack
lookup is answered. Each module imports them by name, so pytest applies them there.
``_run`` drives a handler coroutine, ``_set_profile`` saves a deploy profile and
``_fake_store`` is an artifact store holding one artifact of the given kind and content.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.deploy import engine, handlers


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse isolation of test_deploy_web_handlers.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _force_posix_shell(monkeypatch):
    """Neutralise the handlers' ``os.name == "nt"`` platform gate.

    The deploy handlers early-return 400/unsupported on Windows because the
    deploy scripts require a POSIX shell. That gate hides the handler logic the
    tests actually cover (scan gate, ttl validation, confirm gate, restricted
    session deny), so on a Windows CI runner these tests get the platform
    early-return instead of the assertion they expect.

    Patch a handlers-module-local ``os`` proxy instead of the global
    ``os.name``: pathlib and other stdlib consumers select behaviour from
    ``os.name`` at call time, so a process-wide patch would break ``Path``
    construction inside the handlers on a real Windows runner. The proxy
    changes only what ``handlers.py`` itself sees. On a POSIX host this
    changes nothing. The dedicated Windows-gate test overrides the same
    module-local attribute in its own body.
    """

    class _PosixNameOs:
        """Delegate everything to the real ``os`` except ``name``."""

        name = "posix"

        def __getattr__(self, attr):  # pragma: no cover - trivial delegation
            return getattr(os, attr)

    monkeypatch.setattr(handlers, "os", _PosixNameOs())


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse isolation of test_deploy_web_handlers.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _isolate_config(tmp_path: Path, monkeypatch):
    # Patch config_dir on all deploy submodules so paths resolve under tmp_path.
    import kiro_crew.deploy as _deploy_pkg
    from kiro_crew.deploy import pending as _pending_mod
    from kiro_crew.deploy import profiles as _profiles_mod

    monkeypatch.setattr(handlers, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_profiles_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_pending_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(_deploy_pkg, "config_dir", lambda: tmp_path)
    cfg = tmp_path / "config.json"
    return cfg


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse isolation of test_deploy_web_handlers.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _mock_base_stack(monkeypatch):
    """Default mock for engine.run_aws so all confirm=True tests pass the base-stack check.

    Tests that specifically want to test missing-base-stack behavior can
    override engine.run_aws in their body (monkeypatch is last-write-wins).
    """
    import json as _json

    _base_outputs = _json.dumps(
        [{"OutputKey": "BucketName", "OutputValue": "kirocrew-base-bucket"}]
    )
    monkeypatch.setattr(engine, "run_aws", lambda *a, **kw: (0, _base_outputs, ""))


def _run(coro):
    return asyncio.run(coro)


def _set_profile(monkeypatch, profile="p", region="us-west-2"):
    handlers._save_config(profile, region)


def _fake_store(kind="widget", content="<div>hi</div>", name="My Art"):
    art = SimpleNamespace(kind=kind, content=content, name=name)
    return SimpleNamespace(get=lambda slug: art)
