"""Remote session transport: a resilient, authenticated stdio link to a guest worker.

``wire`` (sans-IO protocol) and ``worker`` (guest side) are stdlib-only so they
copy into a session environment unchanged; ``link`` is the gateway side.
"""
