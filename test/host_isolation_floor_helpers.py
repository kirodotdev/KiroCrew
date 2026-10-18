"""Shared pieces for the ``test_host_isolation_floor*.py`` modules.

``_root`` is the rootdir conftest imported by path under its own module name (pytest
loads it as a plugin, but reaching it through the plugin manager depends on the name
pytest registered); ``_REPO_ROOT`` and ``_ROOT_CONFTEST`` locate it. Loaded once per
process, when the first of these modules imports it.
"""

from __future__ import annotations

import importlib.util
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ROOT_CONFTEST = _REPO_ROOT / "conftest.py"


def _load_root_conftest():
    """Import the rootdir conftest under its own module name.

    pytest already loads it as a plugin, but reaching it through the plugin manager
    depends on the name pytest happened to register. Loading it by path is
    deterministic, and the fixtures it defines are inert in this namespace (a
    ``@pytest.fixture`` decorator only marks a function; nothing collects them here).
    """
    spec = importlib.util.spec_from_file_location("_kirocrew_isolation_conftest", _ROOT_CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_root = _load_root_conftest()
