"""Webex Messaging channel for KiroCrew via the Webex REST API + WebSocket.

DM-focused. Inbound events arrive over a device-registration WebSocket
(no public URL or webhook needed; KiroCrew reaches out to Webex), then the
full message is fetched via the documented REST API. Outbound replies are
plain REST ``POST /v1/messages`` sends, with a budgeted ``PUT`` edit used
for the in-place status placeholder (Webex caps a message at 10 edits).

Reuses the existing session machinery (SessionManager / provider.stream);
this package only does Webex I/O and maps it onto the shared
``MessagingTransport -> TurnDriver -> Renderer`` pipeline.
"""

from __future__ import annotations
