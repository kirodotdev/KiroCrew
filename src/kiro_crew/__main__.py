"""Entry point for ``python -m kiro_crew``.

``_ensure_ssl_certs()`` MUST run before ``from kiro_crew.cli import main``
because importing ``cli`` can still reach ``aiohttp`` transitively (e.g. via
``cli_doctor`` → ``dashboard.crash_dump_store`` / ``dashboard.origin``).
Issue #3504 deferred cli.py's heaviest aiohttp edges (``cli_server``,
``dashboard.state``) to call time, which makes this ordering EASIER to hold —
but any module-scope import that reaches aiohttp, now or later, must still
execute after ``_ensure_ssl_certs()``, so the prelude stays mandatory.

aiohttp caches its default SSL context at import time
(``aiohttp.connector._SSL_CONTEXT_VERIFIED``).  On AL2 / dev-desktops the
BrazilPython cafile is missing, so the cached context ends up with zero CA
certs and every HTTPS connection fails with CERTIFICATE_VERIFY_FAILED.
"""

from __future__ import annotations

from kiro_crew import platform_compat
from kiro_crew._ssl_compat import _ensure_ssl_certs

# Windows: force UTF-8 stdout/stderr before any non-ASCII output (no-op on POSIX).
platform_compat.ensure_utf8_console()
_ensure_ssl_certs()

if __name__ == "__main__":
    from kiro_crew.cli import main  # noqa: E402

    main()
