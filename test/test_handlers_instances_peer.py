"""The exception classes ``handlers_instances_peer`` holds are named by their facade.

A raised class renders as ``module.qualname`` in a traceback and an error chain, so
each class keeps the name of the module callers import it from,
``kiro_crew.dashboard.handlers_instances``, and resolves back to itself through it.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew.dashboard import handlers_instances, handlers_instances_peer


@pytest.mark.parametrize("name", ["PeerSlotsUnavailable", "ProxyReplyUnredactable"])
def test_a_peer_exception_class_is_named_by_the_facade(name: str) -> None:
    cls = getattr(handlers_instances, name)
    assert cls is vars(handlers_instances_peer)[name]
    assert issubclass(cls, Exception)
    assert cls.__module__ == "kiro_crew.dashboard.handlers_instances"
    assert getattr(sys.modules[cls.__module__], cls.__qualname__) is cls
