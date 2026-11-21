"""Shared runtime exceptions with no transport dependencies."""

class ClientRequestError(RuntimeError):
    """An agent-to-client ACP request failed, timed out, or was cancelled."""


class SessionBusyError(RuntimeError):
    """A session cannot be replaced while active or stale."""
