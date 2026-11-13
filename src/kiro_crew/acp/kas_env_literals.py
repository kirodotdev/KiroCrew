"""Which of a third-party MCP server's ``env`` literals may cross the KAS wire, and
how a withheld key is named in a log line or notice.

Split out of :mod:`kiro_crew.acp.kas_agents` for its line cap; the projection's
credential filter there is the caller. Pure: no I/O.
"""

from __future__ import annotations

import re
from typing import Any

#: The only literal values a third-party server's env may send over the KAS
#: wire: a boolean, ``true`` or ``false``, matched case-insensitively after
#: trimming. No secret is a boolean, so this holds by construction, with no
#: guess about which key names or value shapes carry one. Everything else,
#: numbers included (a PIN is digits), is withheld and named in the log; the
#: rest of the env reaches the server through a local launcher that keeps it
#: off the wire.
_RELAYABLE_BOOLEANS = frozenset({"true", "false"})

#: A key NAME printed in the log as written. Only a plain word the redactor
#: leaves alone is printed; any other name is counted.
_PLAIN_WORD = re.compile(r"[A-Za-z0-9._-]{1,256}")

#: A camelCase boundary (``accessKey`` -> ``access_Key``), so a credential word
#: joined by case alone still ends a segment for the shared class.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


def _credential_shaped_env_key(key: str) -> bool:
    """Whether the env KEY *key* names a credential.

    The one shared answer, ``config.sections_agent_env.AGENT_ENV_CREDENTIAL_CLASS``,
    on the name split at punctuation and camelCase (``encryptionKey`` reads as
    ``ENCRYPTION_KEY``), plus the gateway's own credential prefixes.
    """
    from kiro_crew.config.sections_agent_env import AGENT_ENV_CREDENTIAL_CLASS
    from kiro_crew.mcp_gateway.hashing import is_secret_env_key
    from kiro_crew.mcp_gateway.manager import is_credential_env_key

    normal = _NON_ALNUM.sub("_", _CAMEL_BOUNDARY.sub("_", key)).upper()
    if is_secret_env_key(normal) or is_credential_env_key(normal):
        return True
    return bool(AGENT_ENV_CREDENTIAL_CLASS.search(normal))


def _relayable_literal(key: str, value: Any) -> bool:
    """Whether a third-party server's literal env *value* under *key* may cross the wire.

    Only a boolean (:data:`_RELAYABLE_BOOLEANS`), and only when the KEY is not
    credential-shaped (:func:`_credential_shaped_env_key`) and the credential
    redactor would leave the value unchanged alone and spelled beside its key,
    through :func:`kiro_crew.external_text.redact_external_text` so an edition's
    extra patterns apply. The value is relayed as written.
    """
    if not isinstance(value, str) or not isinstance(key, str):
        return False
    if value.strip().lower() not in _RELAYABLE_BOOLEANS:
        return False
    if _credential_shaped_env_key(key):
        return False
    from kiro_crew.external_text import redact_external_text

    return all(
        redact_external_text(text) == text for text in (value, f"{key}={value}", f"{key}: {value}")
    )


def _loggable_key_names(keys: list[str]) -> str:
    """*keys* for a log line: a name shown only when it is itself a plain word the
    redactor leaves alone, so a credential-shaped NAME is counted, never printed."""
    from kiro_crew.external_text import redact_external_text

    shown = sorted(k for k in keys if _PLAIN_WORD.fullmatch(k) and redact_external_text(k) == k)
    hidden = len(keys) - len(shown)
    if hidden:
        shown.append(f"{hidden} unnamed")
    return ", ".join(shown)
