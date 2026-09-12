"""Owner-authorization policy for mediated secret requests.

A Custom secret may be *used* by the mediated-request tool only after the owner
authorizes it for an EXACT https origin and a specific credential placement.
This is least-privilege secret-to-destination binding: the agent names a secret
and a URL, and trusted code refuses unless the URL's origin exactly matches the
one the owner bound to that secret, injecting the value only in the owner-chosen
place.

Storage: ``<config_dir>/secret_request_policy.json``, an owner-authored file that
sits beside ``config.json`` and the ``.vault``. The OWNER authors it directly
(a manual edit under the owner's control), never through the agent tool path,
for the same reason the vault is protected: authorizing a credential's egress
destination is a trust-root decision, and an auto-approved agent shell must not
be able to grant itself a new destination for a stored secret. This module only
READS it.

The file shape (JSON)::

    {
      "version": 1,
      "authorizations": {
        "WEATHER_API_KEY": {
          "origin": "https://api.weather.example",
          "placement": {"type": "bearer"}
        },
        "JIRA_TOKEN": {
          "origin": "https://your-domain.atlassian.net",
          "placement": {"type": "header", "header": "X-Api-Key"}
        }
      }
    }

``origin`` is scheme + host + optional explicit port, normalized; a request URL
is authorized only when its own origin equals it exactly. ``placement`` is where
the resolved secret is injected: a bearer ``Authorization: Bearer <value>`` or a
single named request header. No other placement is honored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

#: Policy file name, beside ``config.json`` under the config dir.
POLICY_FILENAME = "secret_request_policy.json"

#: Header names a policy may NOT choose for injection: these are set by the
#: dispatcher itself or are hop-by-hop / would let a placement smuggle the value
#: into a field the sanitizer does not scrub. Lower-cased for comparison.
_FORBIDDEN_PLACEMENT_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "authorization",  # a bearer secret uses placement type "bearer", not a raw header
    }
)


class PolicyError(Exception):
    """A policy problem safe to surface to the agent (names the secret/origin,
    never the value)."""


@dataclass(frozen=True)
class CredentialPlacement:
    """Where the resolved secret is injected into the outbound request."""

    #: ``"bearer"`` -> ``Authorization: Bearer <value>``; ``"header"`` -> a named header.
    type: str
    #: Header name when ``type == "header"``; ``None`` for bearer.
    header: Optional[str] = None


@dataclass(frozen=True)
class SecretAuthorization:
    """The owner's binding of one secret to one destination + placement."""

    secret_name: str
    origin: str
    placement: CredentialPlacement


def normalize_origin(url_or_origin: str) -> str:
    """Return the canonical ``scheme://host[:port]`` origin of *url_or_origin*.

    Lower-cases scheme/host, drops a default 443 port, keeps a non-default port,
    and strips any path/query/fragment. Used both when reading the policy and
    when checking a request URL, so the two are compared on identical footing.
    Raises :class:`PolicyError` for a non-https or host-less input.
    """
    parts = urlsplit(url_or_origin.strip())
    if parts.scheme != "https":
        raise PolicyError("Only https origins are supported.")
    host = (parts.hostname or "").lower()
    if not host:
        raise PolicyError("Origin has no host.")
    try:
        port = parts.port
    except ValueError:
        raise PolicyError("Origin has an invalid port.")
    if port and port != 443:
        return f"https://{host}:{port}"
    return f"https://{host}"


def _parse_placement(raw: object, secret_name: str) -> CredentialPlacement:
    if not isinstance(raw, dict):
        raise PolicyError(f"Authorization for {secret_name!r} has a malformed placement.")
    ptype = raw.get("type")
    if ptype == "bearer":
        return CredentialPlacement(type="bearer")
    if ptype == "header":
        header = raw.get("header")
        if not isinstance(header, str) or not header.strip():
            raise PolicyError(
                f"Authorization for {secret_name!r} uses a header placement without a header name."
            )
        header = header.strip()
        if header.lower() in _FORBIDDEN_PLACEMENT_HEADERS:
            raise PolicyError(
                f"Authorization for {secret_name!r} names a header that cannot carry a secret "
                f"({header!r}); use placement type 'bearer' for Authorization."
            )
        # Reject a header name with control/framing characters up front.
        if any(ord(c) < 0x21 or ord(c) == 0x7F or c in ":\r\n" for c in header):
            raise PolicyError(f"Authorization for {secret_name!r} has an invalid header name.")
        return CredentialPlacement(type="header", header=header)
    raise PolicyError(
        f"Authorization for {secret_name!r} has an unknown placement type "
        f"(expected 'bearer' or 'header')."
    )


def load_authorization(config_dir: str | Path, secret_name: str) -> SecretAuthorization:
    """Load and validate the owner's authorization for *secret_name*.

    Raises :class:`PolicyError` (fail closed) when the policy file is missing,
    unreadable, malformed, or has no authorization for this secret. The returned
    object's ``origin`` is already normalized so the dispatcher can compare a
    request URL's origin against it with ``==``.
    """
    path = Path(config_dir) / POLICY_FILENAME
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PolicyError(
            "No mediated-request authorizations are configured. The owner must authorize this "
            "secret for an exact https origin in the owner-authored secret_request_policy.json "
            "before it can be used from chat."
        )
    except OSError:
        raise PolicyError("The mediated-request authorization policy could not be read.")

    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError):
        raise PolicyError("The mediated-request authorization policy is not valid JSON.")
    if not isinstance(data, dict):
        raise PolicyError("The mediated-request authorization policy has an unexpected shape.")

    authorizations = data.get("authorizations")
    if not isinstance(authorizations, dict):
        raise PolicyError("The mediated-request authorization policy lists no authorizations.")

    entry = authorizations.get(secret_name)
    if not isinstance(entry, dict):
        raise PolicyError(
            f"Secret {secret_name!r} is not authorized for any origin. The owner must authorize "
            f"it for an exact https origin in the owner-authored secret_request_policy.json "
            f"before it can be used from chat."
        )

    origin_raw = entry.get("origin")
    if not isinstance(origin_raw, str) or not origin_raw.strip():
        raise PolicyError(f"Authorization for {secret_name!r} has no origin.")
    origin = normalize_origin(origin_raw)
    placement = _parse_placement(entry.get("placement"), secret_name)
    return SecretAuthorization(secret_name=secret_name, origin=origin, placement=placement)
