"""Requests the Kiro relay delivers to this Crew, and who Crew takes each one to be.

The relay terminates a person's connection, verifies their Kiro access token, and
names the verified person in its own protocol message on the Crew's host connection.
The Crew's relay client turns each relayed request into an ordinary HTTP request to
this gateway and stamps that name on it as ``x-kiro-relay-user``. This package is the
Crew's half of that contract, and it is OFF unless ``tunnel.relay_enabled`` is set:

* :mod:`.caller` -- the relayed caller: its provenance, its remote transport, and the
  owner-or-teammate decision. Pure values, no request access.
* :mod:`.ingress` -- the relay-path header rule. The seam the relay client calls to
  stamp a request, and the outermost middleware that honors the stamp only on a
  request the relay client handed over and hands every other one on unchanged to the
  client-sent strip.
* :mod:`.limits` -- the routes a relayed caller may never reach.
* :mod:`.admission` -- how token auth admits a relayed caller, and the refusal of
  any Crew credential presented over the relay.

Contract: ``docs/system-specs/modules/dashboard-token-auth.md``, section
"Relay-delivered requests".
"""
