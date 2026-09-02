"""iMessage channel — a chat surface reached through the user's own Mac.

Unlike every other channel, iMessage needs no third-party bot registration and
no credential: the transport is the user's own Messages.app, and the identity
that talks to the agent is their own handle. That is the design constraint, not
a convenience — a hosted relay would put a third party in the *message path* of
a channel whose value is that the path itself involves nobody else.

Be precise about what that does and does not buy, because the loose version of
the claim is false. The TRANSPORT is local: no bot, no relay, no inbound network
surface, and the message never transits a vendor on its way between Messages.app
and the gateway. The CONVERSATION is not: a turn is driven through the same
``TurnDriver`` as every other channel, so the prompt goes to whichever model
provider the agent is configured with. "Nothing leaves this Mac" would be a
privacy promise this channel does not keep.

The bridge to Messages.app is the external ``imsg`` CLI in its long-lived
``rpc`` mode: a child process spoken to over newline-framed JSON-RPC 2.0 on
stdio, the same shape as a language server. No daemon, no port, no webhook, and
therefore no new inbound network surface.

v1 is DM-only, text-only, and requires the gateway to run on the Messages host.
"""

from __future__ import annotations
