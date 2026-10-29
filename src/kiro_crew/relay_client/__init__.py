"""The Kiro relay client: the host leg of Remote Control, in Crew core.

The Crew dials out to the Kiro relay over one WebSocket on 443 and holds it open.
The relay carries each viewer's HTTP request and WebSocket over that connection as
frames (protocol version 1), and this package hands each one to the Crew's own web
server and streams the answer back.

Modules, from the wire up:

* :mod:`~kiro_crew.relay_client.protocol` -- every number and frame shape of the
  protocol, and the validator for frames the relay sends.
* :mod:`~kiro_crew.relay_client.backoff` -- reconnect backoff and the host-leg
  close-code table.
* :mod:`~kiro_crew.relay_client.token_source` -- the seam the host token comes
  through.
* :mod:`~kiro_crew.relay_client.ingress` -- the seam a relayed request reaches the
  Crew's web server through.
* :mod:`~kiro_crew.relay_client.streams` -- one relayed request or WebSocket.
* :mod:`~kiro_crew.relay_client.connection` -- one host connection.
* :mod:`~kiro_crew.relay_client.client` -- the loop that keeps a connection live.
* :mod:`~kiro_crew.relay_client.provider` -- the ``TunnelProvider`` and the rule
  that selects it.

It is off by default (``tunnel.relay_enabled``). The package imports nothing from
``kiro_crew.tunnel``: the platform composition root imports it, and ``platform/``
keeps zero imports of the tunnel package.
"""
