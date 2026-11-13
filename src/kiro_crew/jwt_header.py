"""The structural half of the shape-only JWT spelling, shared by the scrubber and the log floor.

``credential_patterns`` imports nothing and ``log_redaction`` installs before the
``security`` package loads, so this stdlib-only module is the one both can import.
"""

from __future__ import annotations

import base64
import json


def is_jwt_header(header: str) -> bool:
    """Whether *header*, the first segment of a JWT-shaped hit, makes the hit a credential.

    Every JWS and JWE header, and the payload of an itsdangerous or Flask session
    token, base64url-decodes to a JSON object; a host label such as ``honeyJar`` does
    not. A header holding a second ``eyJ`` counts too, so a caller that rescans a
    rejected hit stays linear. A JSON object Python cannot parse (nested too deep,
    or an integer past its digit limit, kept a string by ``parse_int``) still counts:
    the check fails closed.
    """
    if header.find("eyJ", 1) != -1:
        return True
    try:
        value = json.loads(
            base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)), parse_int=str
        )
    except RecursionError:
        return True
    except ValueError:
        return False
    return isinstance(value, dict)
