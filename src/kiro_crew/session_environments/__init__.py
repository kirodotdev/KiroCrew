"""Provider-neutral pieces of Session Environments.

``coder/`` keeps what only a Coder workspace needs. What more than one provider
shares lives here: the persistent-VM registry and lifecycle (``persistent_vm``),
the table that routes a profile id to its provider and the guard that keeps a
session on the provider it started on (``routing``), and the VM branches of
browse and retention (``browse``, ``retention``).
"""
