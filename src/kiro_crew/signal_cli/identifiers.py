"""Signal identities as strings: the one reading of a number or an ACI.

Dependency-free on purpose (``re`` and nothing else). Config loading
(``config.section_builders``) and the dashboard's settings handler both read
``signal.account`` and ``signal.allowed_numbers`` with these, so a module that
imported the channel's runtime (the client, the transport, the JSON-RPC peer)
would load all of it with config, and on the gateway boot path, even while the
channel is disabled. ``signal_cli.client`` re-exports every name here, so the
channel's own modules keep importing them from there.
"""

from __future__ import annotations

import re

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_PHONE_NOISE = re.compile(r"[\s()\-.]")
_E164_RE = re.compile(r"^\+[0-9]{6,15}$")


def normalize_identifier(value: object) -> str:
    """Canonicalize a phone number or ACI for allow-list comparison.

    An ACI (a UUID) folds to lowercase; a number loses formatting so
    ``+44 7700 900123`` and ``+447700900123`` are one identity.
    """
    if not isinstance(value, str):
        return ""
    stripped = value.strip()
    if not stripped:
        return ""
    if _UUID_RE.match(stripped):
        return stripped.lower()
    number = _PHONE_NOISE.sub("", stripped)
    if number.startswith("00") and number[2:].isdigit():
        # The international call prefix many countries dial: 0044... is +44...
        number = "+" + number[2:]
    return number


def coerce_identifier(value: object) -> str:
    """A config value as an identifier string: a JSON integer gains its ``+``.

    ``447700900001`` written without quotes in ``config.json`` is a number to
    the JSON parser; it can only have meant the E.164 ``+447700900001``. A
    bool, a float or anything else coerces to ``""``. The full shape check
    (``+E.164`` or an ACI UUID) is :func:`identifier_problem`.
    """
    if isinstance(value, bool):
        return ""
    if isinstance(value, int) and value > 0:
        return f"+{value}"
    return value.strip() if isinstance(value, str) else ""


def identifier_problem(value: object) -> str:
    """Why *value* is not a usable identity, or ``""`` when it is one.

    Usable means a ``+`` E.164 number (6-15 digits, after formatting and a
    leading ``00`` are normalized) or an ACI UUID. The answer never echoes the
    value whole -- callers log it next to a redacted form.
    """
    normalized = normalize_identifier(coerce_identifier(value))
    if not normalized:
        return "empty"
    if _UUID_RE.match(normalized) or _E164_RE.match(normalized):
        return ""
    return "neither a +E.164 number (6-15 digits) nor an ACI UUID"


def account_identifier(value: object) -> str:
    """``signal.account`` as signal-cli's ``-a`` needs it, or ``""``.

    The ONE reading of the field, shared by the client (argv and self-identity
    matching), the boot gate and doctor, so a ``+44 7700 900123``, a
    ``0044...`` or an unquoted JSON integer reaches signal-cli as the same
    ``+E.164`` the doctor reports healthy.
    """
    return normalize_identifier(coerce_identifier(value))


def redact_identifier(value: str) -> str:
    """A number or ACI is personal data -- never log it whole."""
    return f"{value[:4]}***" if value else "?"
