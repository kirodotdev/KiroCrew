"""Which account the credential sources name, as one fingerprint.

:func:`identity_fingerprint` digests the stable claims of kiro-cli's store, audited and
failing closed; the API-key and Crew-vault components join it as separate components
(:func:`_combine_identity_fingerprints`) so a lost component drops out rather than
turning into a different value.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from kiro_crew import hooks, identity_stores
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.config.loader import CRED_KIRO_API_KEY, read_env_file_credential
from kiro_crew.kiro_prerequisite_identity_store import (
    _AUTH_STATE_KEY_PREFIXES,
    _AUTH_STATE_TABLE,
    _open_identity_db_readonly,
)

logger = logging.getLogger("kiro_crew.kiro_prerequisite")


# What :func:`identity_fingerprint` returns when the store names no identity --
# signed out, absent, or unreadable. Not a hash, so it can never collide with a
# real fingerprint.
_AUTH_FINGERPRINT_ABSENT = ""
# SEL audit label for the identity-fingerprint read. Registered in
# hooks._AUDIT_ONLY_READ_IDS; an unregistered id is refused there, and this reader
# fails closed on that refusal rather than reading unaudited.
_IDENTITY_FINGERPRINT_READ_ID = "kiro_prerequisite.identity_fingerprint"
# Blob fields that identify WHICH account is signed in and survive a token
# refresh. An ALLOWLIST on purpose: the same blobs carry `access_token`,
# `refresh_token`, `expires_at` and `client_secret`, and a denylist would admit
# every field a future kiro-cli adds -- letting a new secret into the digest, or a
# new rotating field cause an account change on every refresh. `client_id` is the
# OIDC registration, which is replaced when the client re-registers; that costs one
# cold start on re-registration and is what distinguishes two logins whose
# start_url is identical.
_IDENTITY_CLAIM_FIELDS = frozenset(
    {
        "client_id",
        "oauth_flow",
        "region",
        "scopes",
        "start_url",
    }
)
# Claims whose value is a label rather than an identifier, compared without case.
# kiro-cli has more than one token refresher, and they serialise the same flow with
# different casing (`PKCE` and `Pkce` observed minutes apart on one login). Hashing
# the raw spelling turns every such refresh into an "account change" and retires
# healthy sessions. Case-folding keeps a real flow change (PKCE to device
# code) visible while ignoring spelling.
_CASE_INSENSITIVE_CLAIM_FIELDS = frozenset({"oauth_flow"})


def identity_fingerprint(path: Path, *, definitive: list[bool] | None = None) -> str:
    """Return a digest naming WHICH account an identity store is signed in as.

    Two rules shape what participates.

    **Stable claims only.** The credential blobs mix fields that identify an
    account with fields that are replaced on every token refresh. Digesting a
    rotating field would report an account change roughly hourly and retire
    healthy sessions, so the rotating and secret ones -- ``access_token``,
    ``refresh_token``, ``expires_at``, ``client_secret`` -- are excluded and the
    identifying ones are kept: the SSO ``start_url``, ``region``, ``oauth_flow``
    (compared without case, see :data:`_CASE_INSENSITIVE_CLAIM_FIELDS`),
    ``scopes`` and the OIDC registration's ``client_id``. Key NAMES also
    participate, so a change of credential kind counts even when no value moved.

    **An allowlist, not a denylist** (:data:`_IDENTITY_CLAIM_FIELDS`). A field
    added to these blobs by a future kiro-cli cannot join the fingerprint by
    default -- which keeps both a new secret out of it and a new rotating field
    from causing spurious retirement.

    Values are hashed, never returned, so the fingerprint carries no credential.

    The read is SEL-audited and FAILS CLOSED: this file holds live credential
    material, so a read whose audit cannot be recorded returns "absent" rather
    than an unaudited answer. "Absent" is also what a logout leaves behind, and
    the caller treats it as "cannot confirm the running children" -- which errs
    toward retiring them, never toward trusting them.

    ``definitive``, when given, receives ONE bool: whether this answer fully
    describes the store. True only when the store was read and audited and
    every credential row in it was identified, or when there is no store file
    at all. Every failure returns the same ``""`` as a genuine sign-out, and a
    store holding a credential row with no stable claim (a social login) can
    also come out ``""`` or omit that row; a caller that must not mistake
    either for "nobody is signed in here" reads this flag.
    """

    def _done(is_definitive: bool, value: str = _AUTH_FINGERPRINT_ABSENT) -> str:
        if definitive is not None:
            definitive.append(is_definitive)
        return value

    audit_ok = hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "invoked")
    if not audit_ok:
        # A logger line is not an SEL audit. Refuse rather than read.
        logger.warning("Kiro identity fingerprint read denied: audit unavailable")
        return _done(False)
    connection = _open_identity_db_readonly(path)
    if connection is None:
        hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "unreadable")
        # A store that does not exist at all is a definitive "nobody signed in
        # here"; one that exists but will not open is merely unknowable.
        return _done(not os.path.lexists(str(path)))
    parts: list[str] = []
    # Whether some credential row carried no stable claim and was skipped: the
    # store then holds a login this reader cannot identify, so its answer is
    # never definitive, whatever else it contains.
    unidentified = False
    try:
        with contextlib.closing(connection):
            present = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"  # wokeignore:rule=master
                ).fetchall()
            }
            if "auth_kv" in present:
                for key, value in connection.execute("SELECT key, value FROM auth_kv").fetchall():
                    claims = _identity_claims(str(key), value)
                    if not claims:
                        # A row carrying NO stable claim is deliberately skipped
                        # ENTIRELY -- its key name is not recorded either. Recording
                        # the name alone would make two different accounts stored
                        # under the same key look identical, which is exactly the
                        # false confidence to avoid: a social login (GitHub, Google)
                        # has no SSO start_url, so account A and account B under
                        # `kirocli:social:token` would fingerprint the same and the
                        # child authenticated as A would never be retired.
                        #
                        # Contributing nothing means such a store can come out
                        # ABSENT, which is never reconciled (see the caller) and so
                        # re-sweeps every turn. "Cannot distinguish" is reported as
                        # "cannot confirm" rather than as "unchanged".
                        unidentified = True
                        continue
                    parts.append(f"k:{key}")
                    parts.extend(claims)
            if _AUTH_STATE_TABLE in present:
                predicate = " OR ".join(["key LIKE ?"] * len(_AUTH_STATE_KEY_PREFIXES))
                rows = connection.execute(
                    f'SELECT key, value FROM "{_AUTH_STATE_TABLE}" WHERE {predicate}',
                    tuple(f"{prefix}%" for prefix in _AUTH_STATE_KEY_PREFIXES),
                ).fetchall()
                for key, value in rows:
                    parts.append(f"s:{key}={_claim_digest(value)}")
    except sqlite3.Error:
        hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "error")
        return _done(False)
    if not parts:
        # Schema present but zero identity rows reads as signed out, not as an
        # identity whose fingerprint happens to be the digest of nothing.
        hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "signed_out")
        return _done(not unidentified)
    if not hooks.emit_internal_read_audit(_IDENTITY_FINGERPRINT_READ_ID, "success"):
        # The read HAPPENED and its terminal audit could not be written, so the
        # answer is unaudited. Discard it rather than let unaudited identity data
        # drive retirement. The failure-shaped outcomes above need no such guard:
        # they already return "absent" whatever their audit does.
        logger.warning("Kiro identity fingerprint discarded: terminal audit unavailable")
        return _done(False)
    return _done(not unidentified, hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest())


#: Separator between the kiro-cli store's fingerprint and the Crew vault's in the
#: combined identity string. Only present when the vault holds an identity, so a
#: host with no Crew sign-in fingerprints exactly as before.
_CREW_VAULT_FINGERPRINT_SEP = "+crew:"

#: Separator in front of Kiro CLI's own API-key component (see
#: :func:`_api_key_identity_claim`). Only present when a key is configured, so a
#: host without one fingerprints exactly as before.
_API_KEY_FINGERPRINT_SEP = "+key:"


def _crew_vault_fingerprint() -> str:
    """The Crew vault's own identity digest, or ``""`` when it holds nothing.

    Deferred import: ``kiro_crew.auth`` brings the ``cryptography`` wheel, which
    must stay off this module's import graph (it is on the gateway's boot path)
    and is loaded here on the first identity read instead. Never raises; the
    bridge helper reports an unreadable vault as empty. Blocking file IO -- the
    caller already runs on a worker thread.
    """
    from kiro_crew.auth.bridge import vault_identity_fingerprint

    return vault_identity_fingerprint()


def _store_choice_is_certain(platform_name: str, home: Path) -> bool:
    """Whether the store the identity read chose is the only kiro-cli store the host can have.

    Every anchor the store can live at must hold a file or be reported missing, and at
    most one may hold a file. Neither reader of the store's location can tell a missing
    file from one it may not look at: :func:`identity_fingerprint` counts a store with no
    file as definitive through ``os.path.lexists``, and on Windows
    :func:`identity_stores.selected_store` skips an anchor whose ``Path.exists`` is False,
    which CPython 3.14 answers for a path it may not look at. With both Windows stores
    present the choice rests on write times, of main files and WAL sidecars that may not
    be readable, so a read of one proves nothing about the other.
    """
    stores = 0
    for anchor in identity_stores.kiro_cli_store_candidates(platform_name, home):
        try:
            os.stat(anchor)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return False
        stores += 1
    return stores <= 1


def _crew_vault_holds_no_identity() -> bool:
    """Whether the Crew vault was read and stores no identity.

    :func:`_crew_vault_fingerprint` reports a vault it cannot read or decrypt as ``""``
    too, so this asks :meth:`TokenStore.holds_no_identity`, which raises on such a
    vault instead; a raise proves nothing. Deferred import and blocking file IO for the
    reasons that function gives.
    """
    from kiro_crew.auth.bridge import default_token_store

    try:
        return default_token_store().holds_no_identity()
    except Exception:  # noqa: BLE001 - a vault that will not read proves nothing
        return False


def _api_key_identity_claim(
    environ: Mapping[str, str], env_file: Path, *, definitive: list[bool] | None = None
) -> str:
    """The hashed identity claim of Kiro CLI's own API-key credential, or ``""``.

    A host authenticated by :data:`CRED_KIRO_API_KEY` keeps NOTHING about that
    identity in the kiro-cli store -- ``auth_kv`` holds only SSO/social blobs --
    so :func:`identity_fingerprint` reads such a store as signed out. Absent is
    never reconciled as a baseline, so without this component every turn on an
    API-key host re-sweeps and retires idle sessions along with their running
    children, although the CLI stays authenticated the whole time.

    The key is a STABLE claim (it does not rotate on a token refresh), so it
    participates like the store's claims do: hashed, never returned. The lookup
    mirrors the ``whoami`` probe's
    (:meth:`KiroPrerequisiteService._audited_identity_probe`): the process
    environment first, then the data home's ``.env`` -- where a post-scrub
    Docker entrypoint moves the credential -- so the fingerprint and the
    readiness probe agree on whether a key exists. ``definitive``, when given,
    receives the ``.env`` read's own answer (see :func:`read_env_file_credential`)
    when no key is set in the environment, so a caller can tell "no key" from "a
    ``.env`` that would not read". Blocking file IO; the caller runs on a worker
    thread.
    """

    value = str(environ.get(CRED_KIRO_API_KEY, "") or "").strip()
    if not value:
        value = read_env_file_credential(CRED_KIRO_API_KEY, env_file, definitive=definitive)
    if not value:
        return _AUTH_FINGERPRINT_ABSENT
    return _claim_digest(value)


def _combine_identity_fingerprints(cli: str, crew_vault: str, api_key: str = "") -> str:
    """One fingerprint over EVERY credential source a running child may have loaded.

    kiro-cli's store answers for the kiro backend and for a KAS relay spawned
    cli-owned; the Crew vault answers for a KAS relay spawned Crew-owned
    (:mod:`kiro_crew.acp.kas_host_auth`); Kiro CLI's own ``KIRO_API_KEY``
    answers for a child that is handed the key (a harness that strips it, like
    KAS, authenticates from the store instead). A change in ANY of them must
    read as an identity change, so the per-turn sweep retires -- and keeps
    re-sweeping until it completes -- children that loaded the previous
    identity, whichever source it came from.

    Each source is its OWN component, never folded into another's digest: a
    read can LOSE a component under failure (a locked store, an unwritable
    audit) and the latch and :func:`identity_stamp_mismatch` rely on a lost
    component dropping out rather than turning into a different nonempty value.
    Keeping the store and the key separate also means no precedence between
    them has to be assumed -- whichever one a child consumed, a change to it is
    seen. The key and vault components are appended only when present, so a
    host with neither keeps the store-only fingerprint byte-for-byte and ``""``
    still means "no identity anywhere".
    """

    combined = cli
    if api_key:
        combined = f"{combined}{_API_KEY_FINGERPRINT_SEP}{api_key}"
    if crew_vault:
        combined = f"{combined}{_CREW_VAULT_FINGERPRINT_SEP}{crew_vault}"
    return combined


def _identity_components(fingerprint: str) -> tuple[str, str, str]:
    """Split a combined fingerprint into its (store, api_key, vault) components."""

    head, _, vault = fingerprint.partition(_CREW_VAULT_FINGERPRINT_SEP)
    store, _, api_key = head.partition(_API_KEY_FINGERPRINT_SEP)
    return store, api_key, vault


def _claim_digest(value: object) -> str:
    """Hash one claim value so no credential material can leave the reader."""

    raw = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _identity_claims(key: str, value: object) -> list[str]:
    """Return hashed STABLE claims from one ``auth_kv`` blob.

    Anything that is not a JSON object contributes nothing: the row's key name is
    already recorded by the caller, and guessing at an opaque value risks pulling
    in a rotating one. Fields outside :data:`_IDENTITY_CLAIM_FIELDS` are skipped
    whatever their name.
    """

    try:
        blob = json.loads(value if isinstance(value, (str, bytes)) else str(value))
    except (ValueError, TypeError):
        return []
    if not isinstance(blob, dict):
        return []
    claims: list[str] = []
    for claim in sorted(_IDENTITY_CLAIM_FIELDS):
        if claim not in blob:
            continue
        raw = blob[claim]
        # Lists (``scopes``) are order-normalized so a reordered grant of the same
        # scopes is not mistaken for a different account.
        if isinstance(raw, list):
            rendered = ",".join(sorted(str(item) for item in raw))
        else:
            rendered = str(raw)
        if claim in _CASE_INSENSITIVE_CLAIM_FIELDS:
            rendered = rendered.casefold()
        claims.append(f"c:{key}:{claim}={_claim_digest(rendered)}")
    return claims
