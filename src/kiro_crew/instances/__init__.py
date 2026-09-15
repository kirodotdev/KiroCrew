"""Multi-instance management (the *Instances* feature).

Lets one KiroCrew gateway manage and switch between several remote KiroCrew
instances over SSH tunnels.

This package is the gateway-side home for the instances registry, the
``SshTunnelManager``, the port allocator, and the SSH token-mint helper. All
behaviour is gated behind the ``instances.enabled`` config flag (default off).
"""
