"""Token model + encrypted vault store with the External > Builder > Social priority.

KAS credentials live in a DEDICATED :class:`~kiro_crew.secrets.SecretVault`
instance rooted at ``<data_home>/kas`` (store: ``kas/.vault/secrets.enc``,
AES-256-GCM per entry). This is deliberately NOT the user-facing secrets vault
(``config_dir()``): login credentials are auto-refreshed session state managed by
the login/logout UI, not user-provided integration secrets — a separate store
path and key keeps them out of the ``/api/secrets`` panel and keeps the delete
semantics distinct (logout vs disconnect-integration).

Defense in depth: the whole ``kas`` directory is a keystone leaf in
``security._CREW_SECRET_LEAVES`` (the agent can neither read nor write it), and
the vault adds encryption at rest on top — a ciphertext-only leak (backup, sync,
accidental read) discloses nothing without ``kas/.vault/.vault_key``. Atomic
writes, cross-process locking, owner-only modes and Windows ACLs are the vault's
concern, not reimplemented here.
"""

from __future__ import annotations

import enum
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag

from kiro_crew.platform_compat import is_link_or_junction, make_owner_only_dir
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(__name__)

# KAS enters its refresh buffer at now + this margin; a delivered token must beat it.
REFRESH_MARGIN_SECS = 180

# Identity kinds, in the order KAS/kiro-cli resolve them when several are stored.
# External IdP wins, then Builder ID, then social (auth/mod.rs UnifiedBearerResolver).
_PRIORITY = ("external_idp", "builder_id", "identity_center", "social")


class SocialProvider(enum.Enum):
    """Social login provider, spelled as the Kiro auth service expects on the wire."""

    GOOGLE = "Google"
    GITHUB = "Github"


@dataclass
class KasToken:
    """A resolved Kiro credential, in the shape the KAS contract consumes.

    ``provider`` is the governance classification KAS keys off — one of
    ``BuilderId`` / ``Google`` / ``Github`` / ``Enterprise`` / ``ExternalIdp`` /
    ``Internal``. ``profile_arn`` is mandatory for enterprise/IdC identities and feeds
    the ``X-Kiro-Profile-Arn`` header; social/Builder ID may omit it.
    """

    access_token: str
    expires_at: datetime  # timezone-aware UTC
    provider: str
    identity: str  # one of _PRIORITY — which store entry this came from
    refresh_token: str | None = None
    profile_arn: str | None = None
    region: str | None = None
    auth_method: str | None = None  # e.g. 'external_idp'; drives KAS TokenType header
    # IdC refresh needs the dynamically-registered client credentials.
    client_id: str | None = None
    client_secret: str | None = None
    token_endpoint: str | None = None  # external_idp refresh
    extra: dict = field(default_factory=dict)

    def is_expired(self, *, margin_secs: int = REFRESH_MARGIN_SECS) -> bool:
        """True when the token is at or inside KAS's refresh buffer."""
        now = datetime.now(timezone.utc)
        return (now.timestamp() + margin_secs) >= self.expires_at.timestamp()

    def to_json(self) -> str:
        d = asdict(self)
        d["expires_at"] = self.expires_at.astimezone(timezone.utc).isoformat()
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> KasToken:
        d = json.loads(raw)
        d["expires_at"] = _parse_dt(d["expires_at"])
        # Drop unknown keys defensively so a forward-compatible entry still loads.
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TokenStoreError(Exception):
    """The vault store could not be read or written (distinct from a bad identity).

    Callers branch on this: an unknown identity kind raises ``ValueError`` (a 400
    at the API layer), while a storage failure raises ``TokenStoreError`` (a coded
    500) — without this split a corrupt vault envelope's ``ValueError`` would be
    misreported as an invalid-identity client error.
    """


class TokenStore:
    """Per-identity KAS tokens in a dedicated encrypted vault under ``<data_home>/kas``.

    Thin adapter over :class:`SecretVault`: identity kind -> vault entry name,
    ``KasToken.to_json()`` as the entry value. All storage hardening (atomic
    replace, cross-process flock, 0600/ACL, encryption, tamper detection via
    per-entry AAD) is the vault's.
    """

    def __init__(self, data_home: str | os.PathLike[str]) -> None:
        self._kas_dir = Path(data_home) / "kas"
        self._vault = SecretVault(self._kas_dir)

    def _assert_unlinked(self) -> None:
        """Refuse to operate through a linked ``kas`` or ``kas/.vault`` directory.

        The agent is denied writes INSIDE ``kas`` (keystone leaf), but before the
        directory first exists a link could be planted AT ``kas`` (or ``.vault``)
        pointing somewhere agent-readable — the vault would then write its key
        file and ciphertext through the link, making the bearer tokens
        decryptable. A cheap lstat at every entry point closes that.
        """
        for p in (self._kas_dir, self._kas_dir / ".vault"):
            if is_link_or_junction(p):
                raise TokenStoreError(f"refusing linked token-store directory: {p}")

    @staticmethod
    def _entry(identity: str) -> str:
        if identity not in _PRIORITY:
            raise ValueError(f"unknown identity kind: {identity!r}")
        return identity

    def lock_path(self, identity: str) -> Path:
        """Path to the per-identity refresh lock file (owner-only ``kas`` dir).

        The refresh single-flight flock is a coordination primitive, not a
        secret, so it stays a plain co-located file rather than a vault entry.
        """
        self._entry(identity)
        self._assert_unlinked()
        make_owner_only_dir(self._kas_dir)
        return self._kas_dir / f"refresh-{identity}.lock"

    def save(self, token: KasToken) -> None:
        """Write ``token`` for its identity into the vault (encrypted at rest).

        Raises ``ValueError`` for an unknown identity kind and ``TokenStoreError``
        when the vault itself cannot be written.
        """
        name = self._entry(token.identity)
        self._assert_unlinked()
        try:
            self._vault.set_sync(name, token.to_json())
        except (OSError, ValueError, TypeError, AttributeError) as err:
            raise TokenStoreError(f"could not persist KAS token {token.identity}") from err
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - logs the identity slug only, never the token value
        logger.debug("saved KAS token for identity=%s", token.identity)

    def load(self, identity: str) -> KasToken | None:
        name = self._entry(identity)
        self._assert_unlinked()
        try:
            secret = self._vault.get(name)
        except (
            OSError,
            ValueError,
            KeyError,
            UnicodeError,
            TypeError,
            AttributeError,
            InvalidTag,
        ) as err:
            # Unreadable store, malformed envelope (including valid JSON that is
            # not an object — .get on a list/str raises AttributeError/TypeError),
            # or a tampered/undecryptable entry: treat as absent rather than
            # crashing status with a 500. InvalidTag also covers a ciphertext
            # transplanted between entries (AAD mismatch).
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - logs identity + error class, never the token value
            logger.warning("failed to read KAS token %s: %s", identity, err)
            return None
        if secret is None:
            return None
        try:
            token = KasToken.from_json(secret.reveal())
        except (ValueError, KeyError, TypeError) as err:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - logs the identity + parse error, never the token value
            logger.warning("corrupt KAS token entry %s: %s", identity, err)
            return None
        # A social/IdC token without a profile ARN is invalid (KAS rejects it); drop it.
        if token.identity in ("social", "identity_center") and not token.profile_arn:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure - logs the identity slug only, never the token value
            logger.debug("token %s has no profile ARN, treating as invalid", identity)
            return None
        return token

    def delete(self, identity: str) -> None:
        """Delete one identity's stored token. Propagates failures.

        A logout that could not actually remove the credential must NOT report
        success — the caller (the logout handler) turns a raised ``TokenStoreError``
        into a coded error, rather than a false HTTP 200 while the bearer token
        still sits in the store. ``ValueError`` still means a bad identity kind.
        """
        name = self._entry(identity)
        self._assert_unlinked()
        try:
            self._vault.delete_sync(name)
        except (OSError, ValueError, TypeError, AttributeError) as err:
            raise TokenStoreError(f"could not delete KAS token {identity}") from err

    def resolve(self) -> KasToken | None:
        """Return the highest-priority stored token (External > Builder > Social).

        Does NOT refresh — callers that need a live token refresh the result
        themselves. Returns None when nothing is stored.
        """
        for identity in _PRIORITY:
            token = self.load(identity)
            if token is not None:
                return token
        return None
