"""Name a ``KEYRING:`` Kerberos cache that sandboxed agent children cannot see.

The Linux sandbox starts every agent child in a new user namespace, and the
kernel keeps persistent and user keyrings per user namespace, so a child left
on ``KEYRING:persistent:<uid>`` sees an empty keyring. When
:func:`kiro_crew.env.resolve_krb5_ccname` finds no ``FILE:`` cache to redirect
to, it calls :func:`_warn_keyring_ccache_once` here; ``kirocrew doctor`` asks
:func:`krb5_keyring_ccache` for its Credentials row. Both are re-exported from
:mod:`kiro_crew.env`, which stays the import path callers use.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from kiro_crew import platform_compat
from kiro_crew.env_krb5_conf import (
    _KRB5_PER_USERNS_BARE,
    _KRB5_PER_USERNS_KEYRINGS,
    _krb5_conf_default_ccache,
)

logger = logging.getLogger(__name__)

# Set once the KEYRING check has run, so a gateway that spawns many children
# reads krb5.conf and names the problem once rather than on every spawn.
_KRB5_KEYRING_CHECKED = False

# Remedy text shared by the spawn-time warning and the doctor row.
KRB5_KEYRING_REMEDY = (
    "run `kinit -c FILE:/tmp/krb5cc_$(id -u)` (Kiro Crew picks that file up "
    "automatically; it is not renewed for you, so re-run it when the ticket "
    "expires), or, host-wide for every user, set default_ccache_name = "
    "FILE:/tmp/krb5cc_%{uid} in the [libdefaults] section of krb5.conf"
)


def _warn_keyring_ccache_once(env: Mapping[str, str]) -> None:
    """Log one warning per process when agent children are left on a KEYRING cache.

    Diagnostic only: it runs on the spawn path, so any failure reading the krb5
    config is logged at debug and never reaches the caller.
    """
    global _KRB5_KEYRING_CHECKED
    if _KRB5_KEYRING_CHECKED:
        return
    _KRB5_KEYRING_CHECKED = True
    try:
        keyring = krb5_keyring_ccache(env)
    except Exception as exc:  # a diagnostic must never fail a spawn
        logger.debug("krb5 KEYRING check skipped: %s", type(exc).__name__)
        return
    if keyring is None:
        return
    logger.warning(
        "Kerberos ticket cache is %r, which sandboxed agent processes cannot see; "
        "Kerberos-gated MCP servers they start will report no ticket. To fix: %s.",
        keyring,
        KRB5_KEYRING_REMEDY,
    )


def krb5_keyring_ccache(env: Mapping[str, str]) -> str | None:
    """Return the per-namespace ``KEYRING:`` ccache a child of *env* would use, else ``None``.

    ``KRB5CCNAME`` decides when set; otherwise the ``default_ccache_name`` from
    ``[libdefaults]`` in the krb5 config (``KRB5_CONFIG``, else
    ``/etc/krb5.conf``, plus its ``include`` / ``includedir`` files).  Only a
    ``KEYRING:persistent:`` or ``KEYRING:user:`` name is returned: those are
    the kinds the kernel keeps per user namespace, so the sandbox's namespace
    sees a different, empty keyring.  Linux only; read-only; only regular files
    are read.
    """
    if not platform_compat.IS_LINUX:
        return None
    current = env.get("KRB5CCNAME", "")
    name = current if current else _krb5_conf_default_ccache(env)
    if not name:
        return None
    # krb5.conf spells the uid as a token; show the name libkrb5 resolves.
    uid = str(os.getuid())
    name = name.replace("%{uid}", uid).replace("%{USERID}", uid)
    name = name.replace("%{euid}", str(os.geteuid()))
    if name.startswith(_KRB5_PER_USERNS_KEYRINGS) or name in _KRB5_PER_USERNS_BARE:
        return name
    return None
