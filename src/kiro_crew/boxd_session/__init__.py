"""boxd machines as a session environment: a persistent microVM per session.

The engine stays on the gateway, as for every session environment. A boxd
machine holds only the workspace and the guest worker. The gateway reaches it
with ``boxd machine exec``, which streams stdin and stdout both ways, so the
same install, start and dial commands Coder runs over ``coder ssh`` run here
unchanged, under the gateway machine's own ``boxd auth login``.
"""
