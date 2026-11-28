"""An agent child gets none of the gateway's AWS key variables, and the log says so by name.

The scrub once dropped the secret key and the session token and kept the key id.
With the id alone every ``aws`` call in the child failed with "Partial credentials
found in env", which also broke the file routes that would have worked. The whole
set goes now, and one log line names what was dropped so the owner can tell.

Every value here is made up.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from kiro_crew import sandbox_spawn
from kiro_crew.sandbox_argv import _sandbox_env_scrub_keys
from kiro_crew.sandbox_spawn import scrub_agent_subprocess_env

_KEYS = {
    "AWS_ACCESS_KEY_ID": "made-up-key-id",
    "AWS_SECRET_ACCESS_KEY": "made-up-secret",
    "AWS_SESSION_TOKEN": "made-up-session-token",
    "AWS_SECURITY_TOKEN": "made-up-old-token",
}
#: Selectors and pointers, not keys: the child keeps them.
_KEPT = {
    "AWS_PROFILE": "made-up-profile",
    "AWS_DEFAULT_PROFILE": "made-up-profile",
    "AWS_CONFIG_FILE": "/made/up/config",
    "AWS_SHARED_CREDENTIALS_FILE": "/made/up/credentials",
    "AWS_REGION": "us-east-1",
    "PATH": "/usr/bin",
}


class _Lines(logging.Handler):
    """Keeps each rendered warning the sandbox logger emits."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def logged() -> Iterator[list[str]]:
    """The warnings the sandbox modules write during the test.

    Read from a handler on the sandbox logger itself, with that logger switched on
    for the test: what an earlier test on the worker left on the logger tree (a
    logger that does not propagate, a raised level) then cannot hide the line.
    """
    logger = logging.getLogger("kiro_crew.sandbox")
    handler = _Lines()
    level, disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.disabled = disabled


def test_the_whole_key_set_is_dropped_and_nothing_else():
    child = scrub_agent_subprocess_env({**_KEYS, **_KEPT})

    assert child == _KEPT


@pytest.mark.parametrize("level", ["standard", "cc", "strict"])
def test_the_launcher_drops_the_same_set_on_every_tier(level, monkeypatch):
    for name, value in {**_KEYS, **_KEPT}.items():
        monkeypatch.setenv(name, value)

    dropped = set(_sandbox_env_scrub_keys(level, strip_python_env=True))

    assert set(_KEYS) <= dropped
    assert not dropped & set(_KEPT)


def test_one_log_line_names_the_variables_and_no_value(logged):
    scrub_agent_subprocess_env({**_KEYS, **_KEPT})
    scrub_agent_subprocess_env({**_KEYS, **_KEPT})

    (line,) = logged
    for name, value in _KEYS.items():
        assert name in line
        assert value not in line
    for name in _KEPT:
        assert name not in line


@pytest.mark.parametrize("present", sorted(_KEYS))
def test_a_gateway_with_only_one_of_them_names_only_that_one(present, logged):
    child = scrub_agent_subprocess_env({present: _KEYS[present], **_KEPT})

    assert child == _KEPT
    (line,) = logged
    assert present in line
    for other in set(_KEYS) - {present}:
        assert other not in line


def test_a_gateway_with_none_of_them_logs_nothing(logged):
    scrub_agent_subprocess_env(dict(_KEPT))

    assert logged == []
    assert not sandbox_spawn._aws_key_drop_logged.is_set()
