"""The session-start refusal for an opencode session that fell back to opencode's hosted model.

With no provider of the operator's own, opencode 1.19.1 reports no error:
``session/new`` puts the session on ``opencode/big-pickle``, advertises only
``opencode/`` models, and the first prompt goes to that hosted model (live run
``run-opencode-signedout``). :func:`is_opencode_hosted_fallback` reads that
from the start-time model state; the model-selection code then raises
:class:`AcpNoModelProvider` before the first prompt, so nothing the user wrote has
left the machine.
"""

from __future__ import annotations

from collections.abc import Sequence

from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk import host_auth

#: The provider prefix of opencode's OWN hosted models.
OPENCODE_HOSTED_PREFIX = "opencode/"


class AcpNoModelProvider(AcpError):  # noqa: N818
    """The harness has no model provider of the operator's own, refused before a prompt.

    Non-retryable: a respawn reads the same missing provider. ``transient`` is fixed
    False, and the message is Crew's own wording.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args, transient=False, user_worded=True)


def is_opencode_hosted_fallback(current: str, advertised: Sequence[str], config_model: str) -> bool:
    """True when an inheriting opencode session fell back to opencode's hosted model.

    Read from three facts together: the session's model *current* is an
    ``opencode/`` one, every *advertised* model is too (no provider of the operator's
    own is reachable), and the harness's own config names no model (*config_model*,
    from the ``opencode debug config`` read-back). An empty advertised list is no
    evidence. An explicit pick is never refused: a Crew pin that lands never reaches
    this check, and a ``model`` in opencode.json sets *config_model*.

    shortcut: a host signed in ONLY to opencode's own hosted service also advertises
    only ``opencode/`` models, so it is refused until it picks a model or names one in
    opencode.json; telling the two apart would mean the host reading opencode's
    credential file.
    """
    if not current.startswith(OPENCODE_HOSTED_PREFIX) or not advertised:
        return False
    if not all(m.startswith(OPENCODE_HOSTED_PREFIX) for m in advertised):
        return False
    return not config_model


def opencode_hosted_fallback_message(shown_model: str) -> str:
    """The refusal text, naming *shown_model* (already redacted by the caller)."""
    return (
        "Kiro Crew stopped this chat before sending it: OpenCode would have answered "
        f"from its own hosted model ({shown_model}). "
        f"{host_auth.signed_out_message(ACP_BACKEND_OPENCODE)} To use OpenCode's hosted "
        "models on purpose, pick one in the model picker or set it as `model` in "
        "opencode.json."
    )
