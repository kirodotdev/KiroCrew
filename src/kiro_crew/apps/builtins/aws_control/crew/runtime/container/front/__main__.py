"""Runnable entrypoint for the front process: ``python -m container.front``.

Reads the environment once through ``common.load()`` and serves ``build_app`` on
the configured front port and interface. The backend it forwards to is
loopback-only and is never bound here.

The bind is ``settings.front_bind`` rather than a constant, and the two lanes
want different answers:

* **Fargate** binds ``0.0.0.0``. A container's own port has to be reachable from
  outside the container to be reachable at all, and a security group decides who
  may reach it. Reaching the port is an authorised call in the owner's own
  account, decided before the request arrives.
* **Lambda MicroVM** binds ``127.0.0.1``. There is no security group, the VM's
  HTTPS endpoint is reachable from the internet, and an endpoint credential names
  a PORT -- so a caller who can mint one can ask for whichever port in the guest
  they like. Measured: a token minted for the hook port reaches that port from a
  machine outside the account's network entirely. The owner reaches this process
  through an SSM port-forward, which dials loopback inside the guest's own
  network namespace, so loopback costs the owner nothing and takes the port off
  the endpoint.

Neither bind is the authorisation. ``app.py`` requires the deployment's secret on
every route, which is what decides whether a caller is served; the bind only
decides whether the port can be reached. The turn route additionally refuses to
serve at all unless the deployment has declared a single principal.
"""

from __future__ import annotations

import uvicorn
from container import common

from .app import build_app


def main() -> None:
    settings = common.load()
    app = build_app(settings)
    uvicorn.run(app, host=settings.front_bind, port=settings.front_port)


if __name__ == "__main__":
    main()
