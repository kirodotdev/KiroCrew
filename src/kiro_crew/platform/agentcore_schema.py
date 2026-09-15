"""AWS-free AgentCore policy-field validators.

These live beside governance, not the optional AWS extra, so a policy
document can be parsed on a host that never installed ``kirocrew[agentcore]``.
The extra (a later PR) reuses the same functions; it must not re-define them.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Policy / launch-env Gateway MCP URL ceiling. A longer value is almost
# certainly a paste error or a credential stuffed into the query.
AGENTCORE_GATEWAY_URL_MAX = 512

# AgentCore CreateWorkloadIdentity name bounds.
WORKLOAD_NAME_MIN = 3
WORKLOAD_NAME_MAX = 255
_WORKLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def normalize_agentcore_gateway_url(value: str | None) -> str:
    """Return a stripped https Gateway MCP URL, or empty.

    Rejects credentials-in-URL, non-https, fragments, and over-long values
    so a policy write or launch Environment= line cannot carry an arbitrary
    credentialed URL.
    """
    url = (value or "").strip()
    if not url:
        return ""
    if len(url) > AGENTCORE_GATEWAY_URL_MAX:
        raise ValueError(f"agentcore_gateway_url exceeds {AGENTCORE_GATEWAY_URL_MAX} characters")
    # Internal whitespace (including newlines) survives urlparse's scheme
    # check and would land unquoted in a systemd Environment= line.
    if any(ch.isspace() for ch in url):
        raise ValueError("agentcore_gateway_url must be an https URL without credentials")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("agentcore_gateway_url must be an https URL without credentials")
    return url


def normalize_agentcore_workload_name(value: str | None) -> str:
    """Return a stripped workload identity name, or empty.

    Empty is legal (env / RFC default still apply). A present value must
    match AgentCore CreateWorkloadIdentity: 3–255 of ``[A-Za-z0-9_.-]``.
    """
    name = (value or "").strip()
    if not name:
        return ""
    if (
        len(name) < WORKLOAD_NAME_MIN
        or len(name) > WORKLOAD_NAME_MAX
        or _WORKLOAD_NAME_RE.fullmatch(name) is None
    ):
        raise ValueError("agentcore_workload_name is not a usable workload identity name")
    return name
