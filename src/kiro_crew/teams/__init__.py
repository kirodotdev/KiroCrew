"""Microsoft Teams channel for KiroCrew via the Bot Framework REST API.

Self-hosted / DM-focused. Inbound activities are POSTed by the Bot Framework to
a single HTTPS webhook route (``/api/messaging/teams``) registered on the
gateway's existing HTTP server; each request is JWT-validated (Bot Framework
issuer + App-ID audience) before processing. Outbound replies are plain REST
``POST {serviceUrl}/v3/conversations/{id}/activities`` sends authenticated with
an app-credential bearer token.

Reuses the existing session machinery (SessionManager / provider.stream); this
package only does Teams I/O and maps it onto the shared
``MessagingTransport -> TurnDriver -> Renderer`` pipeline.

Dependency direction is ``teams -> messaging`` (allowed); the neutral
``messaging`` package never imports ``teams``.
"""

from __future__ import annotations
