"""Remote session transport: a resilient, authenticated stdio link to a guest worker.

``wire`` is the sans-IO protocol, ``worker`` runs in the guest beside the agent
process, and ``link`` is the gateway side. ``wire`` and ``worker`` are
stdlib-only so they can be copied into a session environment unchanged.
"""
