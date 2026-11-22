"""The ``tunnel.relay_enabled`` field default: unset, so the Crew's credential decides.

A leaf module with no imports, so the config schema can read it without pulling in
the rest of the package.

Unset means nobody chose. Then a Crew on a personal sign-in hosts, and so does one
with a team key or a workload identity
(:func:`kiro_crew.remote_control.hosting.hosts_by_default`). ``on`` writes ``true``
and ``off`` writes ``false``, and a stored value always wins, so turning it off
sticks and an install that already stores ``false`` keeps it.
"""

from __future__ import annotations

#: ``tunnel.relay_enabled`` when ``config.json`` does not set it: unset.
REMOTE_CONTROL_ON_BY_DEFAULT: bool | None = None
