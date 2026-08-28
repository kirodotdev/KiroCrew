# Fixtures for semgrep/contextvar-token-reset.yaml, exercised by
# `semgrep --test` in the SAST job. `ruleid:` asserts the NEXT line MUST
# match; `ok:` asserts it must NOT.

from contextvars import ContextVar

_MY_VAR: ContextVar[float | None] = ContextVar("my_var", default=None)


def bad_token_reset():
    """Token-based reset is context-bound and leaks under xdist."""
    # ruleid: kirocrew.contextvar-token-reset
    token = _MY_VAR.set(42.0)
    # do work ...
    _MY_VAR.reset(token)


def good_value_restore():
    """Value-based restore works across context boundaries."""
    previous = _MY_VAR.get()
    _MY_VAR.set(42.0)
    # do work ...
    # ok: kirocrew.contextvar-token-reset
    _MY_VAR.set(previous)


class SessionManager:
    """Unrelated .reset() calls must not be flagged."""

    def reset(self, key: str) -> None:
        pass


def good_session_manager_reset():
    """A non-ContextVar .reset() call should not match."""
    mgr = SessionManager()
    # ok: kirocrew.contextvar-token-reset
    mgr.reset("session_id")
