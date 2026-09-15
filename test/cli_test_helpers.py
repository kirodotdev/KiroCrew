"""Explicit, test-only ownership of the CLI's process-global sandbox markers."""

import os

import pytest


@pytest.fixture(autouse=True)
def cli_sandbox_environment():
    """Restore entry values after each test, without hiding them from cli.main.

    Import this fixture in modules that call cli.main in-process. Its teardown
    is independent of the test's monkeypatch, including an explicit undo().
    Production main must still clear both keys before dispatch.
    """
    keys = ("KIROCREW_SANDBOX_ACTIVE", "KIROCREW_SANDBOX_LEVEL")
    before = {key: os.environ.get(key) for key in keys}
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
