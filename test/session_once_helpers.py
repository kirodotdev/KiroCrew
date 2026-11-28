"""The body a session tool posts once per call, without its replay fields.

``mcp_session_once._post_once`` adds a fresh ``request_id`` and the post's
``request_attempt`` to every body it sends. A test that pins the rest of a body
checks those two here and compares what is left.
"""

from __future__ import annotations

import re
from typing import Any


def without_once_fields(body: dict[str, Any]) -> dict[str, Any]:
    """*body* minus a well-formed ``request_id`` and a first-post ``request_attempt``."""
    rest = dict(body)
    assert re.fullmatch(r"[0-9a-f]{32}", rest.pop("request_id"))
    assert rest.pop("request_attempt") == 0
    return rest
