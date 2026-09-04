"""Runnable entrypoint for the front process: ``python -m container.front``.

Reads the environment once through ``common.load()`` and serves ``build_app`` on
the configured front port. The bind is ``0.0.0.0`` on purpose: this process is
the only listener the network reaches, sitting behind the API Gateway IAM
authorizer and the internal ALB. The backend it forwards to is loopback-only and
is never bound here.
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
