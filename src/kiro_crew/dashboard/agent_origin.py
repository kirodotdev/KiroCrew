"""The agent-origin marker on a ``POST /api/chat`` send (rfc-conductor-remote-dispatch).

A hub conductor drives a worker on this crew through this crew's own
``POST /api/chat``, over the owner-token proxy. To this gateway that request looks
like the owner typing, so without a mark its turn would hold every right only a
person's turn holds: starting a guide, proposing a card, the person's approval
window, a user-origin directive.

The sender marks such a send with ``turn_origin``. The mark only ever REMOVES
rights: a marked send is handled as a send that no person typed. It grants
nothing, so it needs no proof of who wrote it and no switch to turn it on. That is
also why it fails closed: ``"agent"`` is the one defined value, and any other
value under the key, malformed or not, reads as agent-origin too. A send without
the key behaves exactly as before.
"""

from __future__ import annotations

from typing import Any

#: The body key a hub conductor's send carries.
TURN_ORIGIN_KEY = "turn_origin"

#: The one defined value. Any other value under the key reads the same way.
TURN_ORIGIN_AGENT = "agent"


def send_is_agent_origin(body: Any) -> bool:
    """Whether a send's *body* marks its turn as agent-originated: the key is present."""
    return isinstance(body, dict) and TURN_ORIGIN_KEY in body
