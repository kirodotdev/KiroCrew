"""The session-start refusal for an opencode session that fell back to opencode's hosted model.

With no provider of the operator's own, opencode 1.19.1 reports no error:
``session/new`` puts the session on ``opencode/big-pickle``, advertises only
``opencode/`` models, and the first prompt goes to that hosted model (live run
``run-opencode-signedout``). :func:`is_opencode_hosted_fallback` reads that
from the start-time model state; the model-selection code then raises
:class:`AcpNoModelProvider` before the first prompt, so nothing the user wrote has
left the machine.

MiMo Code 0.1.17, built on opencode, does the same with Xiaomi's hosted models:
with no sign-in, no key and no ``model`` in its config, ``session/new`` puts the
session on ``xiaomi/mimo-v2.6-pro-ultraspeed`` and the first prompt goes to
Xiaomi's API with no credential. A signed-in host advertises the very same models,
so :func:`is_mimocode_hosted_fallback` also needs the read-back's credential fact.
"""

from __future__ import annotations

from collections.abc import Sequence

from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import ACP_BACKEND_MIMOCODE, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk import host_auth

#: The provider prefix of opencode's OWN hosted models.
OPENCODE_HOSTED_PREFIX = "opencode/"

#: The provider prefix of the Xiaomi hosted models MiMo Code starts on.
MIMOCODE_HOSTED_PREFIX = "xiaomi/"


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
    return _only_hosted(OPENCODE_HOSTED_PREFIX, current, advertised) and not config_model


def is_mimocode_hosted_fallback(
    current: str, advertised: Sequence[str], config_model: str, credential_present: bool
) -> bool:
    """True when an inheriting MiMo Code session would answer from Xiaomi's hosted model unsigned.

    The opencode test with ``xiaomi/`` as the prefix, plus *credential_present*: the
    read-back's presence check for the harness's own sign-in file or a Xiaomi key in
    the child's environment. Without it a signed-in host, which advertises the same
    ``xiaomi/`` models, would be refused too.
    """
    if credential_present or config_model:
        return False
    return _only_hosted(MIMOCODE_HOSTED_PREFIX, current, advertised)


def _only_hosted(prefix: str, current: str, advertised: Sequence[str]) -> bool:
    """*current* and every *advertised* model carry *prefix*; an empty list is no evidence."""
    if not current.startswith(prefix) or not advertised:
        return False
    return all(m.startswith(prefix) for m in advertised)


def opencode_hosted_fallback_message(shown_model: str) -> str:
    """The refusal text, naming *shown_model* (already redacted by the caller)."""
    return (
        "Kiro Crew stopped this chat before sending it: OpenCode would have answered "
        f"from its own hosted model ({shown_model}). "
        f"{host_auth.signed_out_message(ACP_BACKEND_OPENCODE)} To use OpenCode's hosted "
        "models on purpose, pick one in the model picker or set it as `model` in "
        "opencode.json."
    )


def mimocode_hosted_fallback_message(shown_model: str) -> str:
    """The MiMo Code refusal text, naming *shown_model* (already redacted by the caller)."""
    return (
        "Kiro Crew stopped this chat before sending it: MiMo Code would have answered "
        f"from Xiaomi's hosted model ({shown_model}) without a sign-in. "
        f"{host_auth.signed_out_message(ACP_BACKEND_MIMOCODE)} To use it without signing "
        "in on purpose, pick a model in the model picker or set it as `model` in "
        "mimocode.json."
    )
