"""Kiro managed environments for Remote Crew, behind the ``crew_client`` feature preview.

A managed environment is a persistent cloud machine the Kiro web service creates for
the signed-in user. This package holds the service client (:mod:`.client`), its
memory of what it may touch (:mod:`.guard`) and the ``instances.managed_env_*``
settings (:mod:`.settings`). Nothing here is reachable yet: no route or dashboard
entry calls it.
"""
