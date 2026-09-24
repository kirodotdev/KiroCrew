"""The queue's provenance proof: minted by the gateway, verified at the restore and the drain.

A queued prompt persisted to the session's metadata line is an ordinary writable
file. Whatever provenance the line carries is worth what the file is worth, so a
RESTORED entry has none by default: no command authority (it drains as channel
text), no admission snapshot (it is re-checked against every constraint that
holds now) and no channel address (a released binding neither drops nor reports
it) -- unless the gateway can prove it accepted the entry with exactly that
provenance. This module is that proof, in two shapes under two keys, each
derived (domain-separated) from the fenced ``token_signing.key`` secret that
also signs dashboard tokens:

* the ATTESTATION (:func:`queue_provenance_proof`) is the in-process record that
  the gateway accepted an entry with exactly its stamps: an HMAC-SHA256 over the
  slot key, the queue id, the content, the channel conversation the entry came
  from (none for dashboard text) and the containment snapshot recorded at
  admission. It lives in the slot's sidecar and is what the drain consults;
* the RECORD SEAL (:func:`queue_record_seal`) is what the durable writer stamps
  on ONE record of ONE durable write: the same fields plus the write's
  GENERATION, a nonce the slot mints whenever the durable value changes. A seal
  therefore names the entry it is on (id and content), the slot, the stamps it
  certifies and the write it belongs to. Nothing an editor can write to the file
  produces a valid one: rewritten words, a rewritten or hand-added address or
  snapshot, a seal moved onto another record or another slot, a hand-written
  tag -- and a record lifted whole from an OLDER write, whose generation is not
  the one the current line carries, verifies as nothing too. That last case is
  the replay the generation exists to refuse: a hand-off consumed before the
  restart cannot be put back on the line and run again with its authority.

The attestation lives BESIDE the queue, never on the entry (see
``slot_queue_repository``): the live entry dict is exactly what the caller
enqueued and the board never sees a tag. The seal rides the durable record as
its own field, with the generation beside it, for the restore path to verify
and read back -- minting a fresh attestation for the entry it proves.

This module's path carries ``token`` on purpose: the argv floor's credential-mint
rule denies an inline program that imports a product module named for the token
mint, which is the convention ``test_argv_floor_inline_and_brace_scope`` derives
from the tree -- every function that can produce a credential from the signing
key must sit behind a module path or a name that rule reads. Consumers import
this MODULE and call through it rather than re-exporting the producer names
under a path the gate does not cover.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

# The module, not its loader: importing it writes nothing (``_get_secret`` is
# lazy by contract), and the call stays deferred to :func:`_derived_key`.
from kiro_crew.dashboard import token_secret

#: Domain separation for the two proof keys: the signing secret also signs
#: dashboard access and refresh tokens, so each proof is computed under a key
#: DERIVED from it for that purpose alone -- a queue proof is never also a valid
#: auth token, and an attestation is never also a valid seal. Each tag is
#: versioned with the signed record's shape.
_ORIGIN_PROOF_DOMAIN = b"kirocrew.dashboard.queue-provenance-proof.v2"
_RECORD_SEAL_DOMAIN = b"kirocrew.dashboard.queue-record-seal.v1"
_DERIVED_KEYS: dict[bytes, bytes] = {}


def _derived_key(domain: bytes) -> bytes:
    """The proof key for *domain*, computed once per process from the fenced secret.

    The first derivation loads the secret, which is file I/O (read or create
    ``token_signing.key``), so it must not happen on the event loop -- and in a
    gateway it does not: :func:`warm_proof_keys` derives both keys in a worker
    thread from ``token_auth.warm_auth_singletons`` before the server accepts a
    connection or restores a slot, and every loop-bound caller after that (the
    enqueue stamp, the durable seal, the restore) reads the memoized key. The
    lazy branch here serves a process with no warm-up -- the tests, a tool --
    where the caller's thread is its own.

    ``token_secret._get_secret`` never raises: a home it cannot write falls back
    to a per-process random secret, under which proofs still verify within the
    process and simply fail after a restart -- the fail-closed direction, since an
    unverifiable restored entry is prose with no admission and no address.
    """
    key = _DERIVED_KEYS.get(domain)
    if key is None:
        key = _DERIVED_KEYS[domain] = hmac.new(
            token_secret._get_secret(), domain, hashlib.sha256
        ).digest()
    return key


def warm_proof_keys() -> None:
    """Derive both proof keys now, loading the secret if it is not loaded yet.

    The one-time load before the loop-bound callers: ``token_auth.
    warm_auth_singletons`` runs this in a worker thread at startup, beside the
    secret and revoked-nonce warm-ups it already does, so
    :func:`_derived_key` never touches the key file on the event loop. Idempotent.
    """
    _derived_key(_ORIGIN_PROOF_DOMAIN)
    _derived_key(_RECORD_SEAL_DOMAIN)


def _origin_proof_key() -> bytes:
    """The attestation key (see :func:`_derived_key`)."""
    return _derived_key(_ORIGIN_PROOF_DOMAIN)


def _canonical(part: dict[str, Any] | None) -> str:
    """One signed mapping as text: sorted keys, no whitespace, ``""`` for none.

    The same bytes on both sides of a JSON round trip, which is what the durable
    record puts the address and the snapshot through: the writer signs the live
    dicts and the reader verifies the ones ``json.loads`` handed back. An empty
    string for an absent part is distinct from ``"{}"`` for an empty one, so
    "no address" and "an address with nothing in it" are different records.
    Raises ``TypeError``/``ValueError`` for a mapping json cannot emit, which the
    callers read as "no proof".
    """
    if part is None:
        return ""
    return json.dumps(part, sort_keys=True, separators=(",", ":"))


def queue_provenance_proof(
    slot_key: str,
    queue_id: str,
    content: str,
    *,
    channel_address: dict[str, Any] | None,
    admission: dict[str, Any] | None,
) -> str:
    """The proof the gateway stamps for an entry of *slot_key* it accepted as
    *content* from *channel_address* (``None`` for dashboard text) under the
    admission-time containment *admission* (``None`` when the producer stamped
    none).

    Length-prefixed fields, so no choice of key, id, content, address or snapshot
    can be read as another split of the same bytes.
    """
    parts = (slot_key, queue_id, content, _canonical(channel_address), _canonical(admission))
    message = "".join(f"{len(part)}:{part}" for part in parts).encode("utf-8", "surrogatepass")
    return hmac.new(_origin_proof_key(), message, hashlib.sha256).hexdigest()


def queue_provenance_matches(
    slot_key: str,
    queue_id: str,
    content: object,
    channel_address: object,
    admission: object,
    tag: object,
) -> bool:
    """Whether *tag* is exactly the proof over the five other arguments.

    False for a missing or non-string tag or content, an address or snapshot that
    is neither a mapping nor absent, a mapping json cannot re-emit, and for any
    mismatch -- rewritten content, a rewritten or hand-added address or snapshot,
    a transplanted proof, another slot. Constant-time comparison; never raises.
    The untyped parameters come off an entry or a line nothing here trusts.
    """
    if not isinstance(tag, str) or not isinstance(queue_id, str) or not isinstance(content, str):
        return False
    if channel_address is not None and not isinstance(channel_address, dict):
        return False
    if admission is not None and not isinstance(admission, dict):
        return False
    try:
        expected = queue_provenance_proof(
            slot_key, queue_id, content, channel_address=channel_address, admission=admission
        )
    except (TypeError, ValueError):
        # A mapping json cannot canonicalise carries no proof the writer could
        # have minted either.
        return False
    return hmac.compare_digest(expected, tag)


def queue_record_seal(
    slot_key: str,
    generation: str,
    queue_id: str,
    content: str,
    *,
    channel_address: dict[str, Any] | None,
    admission: dict[str, Any] | None,
) -> str:
    """The seal the durable writer stamps on the record of ONE entry in ONE write.

    The attestation's fields -- slot key, queue id, content, channel address
    (``None`` for dashboard text) and admission snapshot -- plus *generation*, the
    nonce the slot minted for the durable write this record belongs to. Under its
    own derived key, so a seal is never also an attestation. Length-prefixed, so no
    choice of fields can be read as another split of the same bytes.

    Binding the generation is what makes a seal good for one durable record only:
    every record of one write carries the same generation, the next write that
    changes the value carries a fresh one, and a record kept from the older write
    fails to verify under the newer -- so an entry the gateway consumed between
    the two writes cannot be put back on the line and drained again.
    """
    parts = (
        slot_key,
        generation,
        queue_id,
        content,
        _canonical(channel_address),
        _canonical(admission),
    )
    message = "".join(f"{len(part)}:{part}" for part in parts).encode("utf-8", "surrogatepass")
    return hmac.new(_derived_key(_RECORD_SEAL_DOMAIN), message, hashlib.sha256).hexdigest()


def queue_record_seal_matches(
    slot_key: str,
    generation: object,
    queue_id: object,
    content: object,
    channel_address: object,
    admission: object,
    tag: object,
) -> bool:
    """Whether *tag* is exactly the seal over the six other arguments.

    Recomputed over the ENTRY the seal is attached to and the write it claims: False
    for a missing or non-string tag, generation, id or content, an address or
    snapshot that is neither a mapping nor absent, a mapping json cannot re-emit,
    and for any mismatch -- another entry's id or words, a rewritten or hand-added
    address or snapshot, another slot, another write's generation. Constant-time
    comparison; never raises. The untyped parameters come off a line nothing here
    trusts.
    """
    if not isinstance(tag, str) or not isinstance(generation, str) or not generation:
        return False
    if not isinstance(queue_id, str) or not isinstance(content, str):
        return False
    if channel_address is not None and not isinstance(channel_address, dict):
        return False
    if admission is not None and not isinstance(admission, dict):
        return False
    try:
        expected = queue_record_seal(
            slot_key,
            generation,
            queue_id,
            content,
            channel_address=channel_address,
            admission=admission,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, tag)
