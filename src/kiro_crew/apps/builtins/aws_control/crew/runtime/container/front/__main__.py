"""Runnable entrypoint for the front process: ``python -m container.front``.

Reads the environment once through ``common.load()`` and serves ``build_app`` on
the configured front port. The bind is ``0.0.0.0`` on purpose: a container's own
port has to be reachable from outside the container to be reachable at all, and
this process is the only listener that is. The backend it forwards to is
loopback-only and is never bound here.

Reaching this port is an authorised call in the owner's own account, decided
before the request arrives; the task is not published to the internet and has no
external DNS name. That authorisation is not this process's to enforce and this
process cannot see its result, which is why the turn route refuses to serve at all
unless the deployment has declared a single principal (``app.py``).
"""

from __future__ import annotations

import uvicorn
from container import common

from .app import build_app


def main() -> None:
    settings = common.load()
    app = build_app(settings)
    uvicorn.run(app, host="0.0.0.0", port=settings.front_port)


if __name__ == "__main__":
    main()
