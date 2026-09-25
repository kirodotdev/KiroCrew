"""Gateway-private store of per-chat backend pins.

A chat's ``acp_backend`` pin decides which harness PROCESS its prompts reach.
The chat transcript's metadata line is the wrong home for it: the transcript is
a file the agent's own tools can edit, so a prompt-injected agent that rewrote
``"acp_backend"`` in a transcript would have the next restart hand that chat's
prompts to a provider the user never picked, with the chat still reading as
pinned. The same reason ``jev_route`` is never read back from a transcript.

So the pin lives HERE instead: one gateway-written document under the crew home,
``chat-backend-pins.json``, mapping slot key -> ``{"backend", "owner"}``.
``backend`` is the pin (``""`` is a real Kiro pin; an ABSENT key is "inherit the
configured default"). ``owner`` is the creation identity of the slot that holds
the pin (``_ChatSlot.created_at``): a slot key can be reused by a chat created
under the same name after the previous one was deleted, and a delete that finishes
AFTER such a replacement recorded its pin must not remove the replacement's
record. Removal is therefore conditioned on the owner, compared under the store
lock in the same read-modify-write that deletes.

It is in the same sealed class as ``backend-routing-attestations.json`` --
write-protected against the file-edit tool (``security.paths._WRITE_PROTECTED_HOME_PATHS``),
sealed read-only and strict no-follow inside the OS sandbox
(``sandbox._CREW_READONLY_LEAVES``, ``_CREW_NOFOLLOW_READONLY_FILE_LEAVES``),
pre-created so the seal has a target on a fresh install -- and its reader refuses
an aliased name the way the attestation store does. Every writer is a gateway
route or the gateway's own slot lifecycle; nothing in an agent's reach produces it.

Transcript metadata still CARRIES the pin (``chat_persistence`` writes it, so a
transcript read on its own says what backend served it), but no restore path
reads it back: the three slot-restore paths -- the persistence rehydrate, the
persistence restore, and the channel-slot surface -- consult :func:`load_backend_pins`.

ABSENT is not UNREADABLE, for readers and writers alike. An absent store is the
one legitimate "no pins": a fresh install, or a deployment that never pinned a
chat, and every chat runs on the configured default -- the state a pin-less
chat is in anyway. An UNREADABLE store (malformed, not an object, oversized,
aliased, unreadable) is refused by :func:`_read_records_strict`, and neither
kind of caller may read it as empty:

* A WRITER (:func:`set_pin`, :func:`forget_pin`) that started its
  read-modify-write from ``{}`` would rewrite the document without every other
  chat's pin, so the write aborts with the error and the caller decides what a
  failed persist means for its in-memory pin.
* A READER (a slot restore) that read it as "no pins" would restore every
  explicitly pinned chat as INHERIT, and each one's next prompt would run on the
  global backend -- a silent retarget, which is the harm this store exists to
  prevent. So :func:`load_backend_pins` raises too, and a restore path uses
  :func:`load_backend_pins_snapshot`, which answers :data:`PINS_UNREADABLE` in
  that case, and :func:`apply_restored_pin`, which then leaves the slot's pin
  UNRESOLVED (``backend_pin_unresolved``): the chat is restored, its transcript
  is readable, but dispatch refuses to send until the store can be read again
  (it re-reads it at the next send and resolves the pin if it can), rather than
  send on a backend the chat may not have picked.

The OWNER is checked on the way back too. A record is applied to a restored
slot only when its ``owner`` is that slot's ``created_at``: a slot key can be
reused by a chat created under the same name after the previous one was
deleted, and if the delete's pin cleanup failed (the store was unwritable at
that moment) the stale record would otherwise be restored onto the NEW chat,
whose prompts would then reach a backend it never selected. A record whose
owner is another creation is ignored (the chat inherits, as a chat that never
pinned does) and logged; nothing about it is trusted.

Read-modify-write under one process lock, like the attestation store: a pin and
a clear on two slots racing unserialised would each write its own snapshot and
one would erase the other's change. Synchronous file I/O; callers on the event
loop use ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from types import MappingProxyType
from typing import Mapping

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

#: Leaf name under the crew home. Named in ``sandbox`` and ``security.paths`` too;
#: the protection tests pin the three spellings together.
BACKEND_PINS_LEAF = "chat-backend-pins.json"

#: Largest document the reader will parse. One entry is a slot key, a backend id
#: and a timestamp; a file past this is not a pin store and is refused whole.
BACKEND_PINS_MAX_BYTES = 1 * 1024 * 1024

_LOCK = threading.Lock()

_HARM = "retarget a chat's prompts to a backend its user never picked"


class BackendPinStoreUnreadable(OSError):
    """The pin store exists but could not be read as a pin store.

    Raised to a WRITER so its read-modify-write aborts instead of rewriting the
    document from an empty starting point, and to a READER so a restore does not
    read every explicit pin as "inherit" (see the module docstring).
    """


class BackendPinUnresolved(RuntimeError):
    """A chat's backend pin is UNRESOLVED and dispatch must not guess.

    Raised by the dispatch path for a slot restored while the pin store was
    unreadable, when the store still cannot be read at send time. The message is
    the card the chat shows; nothing is sent.
    """

    def __init__(self, slot_key: str) -> None:
        self.slot_key = slot_key
        super().__init__(
            "this chat's backend pin could not be read: the gateway's pin store "
            f"({BACKEND_PINS_LEAF}) is unreadable, so the chat's own backend choice "
            "is unknown and nothing was sent rather than run the prompt on the "
            "configured default. Repair or remove the store and send again, or "
            "pick a backend for this chat to record a fresh pin."
        )


#: What :func:`load_backend_pins_snapshot` hands a restore path when the store
#: could not be read. Compared by IDENTITY (``pins is PINS_UNREADABLE``): it is an
#: empty mapping, so a caller that forgot the check would read "no pins", which is
#: exactly the reading :func:`apply_restored_pin` exists to refuse.
PINS_UNREADABLE: Mapping[str, Mapping[str, str]] = MappingProxyType({})


def backend_pins_path() -> str:
    """Absolute path of the pin store under the crew home."""
    return os.path.join(str(config_dir()), BACKEND_PINS_LEAF)


def _read_records_strict(path: str) -> dict[str, dict[str, str]]:
    """Every record keyed by slot key; ``{}`` when ABSENT; raises when UNREADABLE.

    Absence is a legitimate starting point for a write (a fresh install has no
    store). Everything else that is not a well-formed store -- an aliased name, an
    unreadable file, malformed JSON, a document that is not an object, an
    oversized file -- raises :class:`BackendPinStoreUnreadable`, so a writer never
    treats a transient read failure as "no pins" and rewrites the document
    without every other chat's pin. Entries that are not well-formed records are
    dropped individually: they carry no pin a writer could preserve.
    """
    from kiro_crew.sandbox import SandboxCeilingUnsealable, require_unaliased_grant_file

    try:
        require_unaliased_grant_file(path, harm=_HARM)
        with open(path, "r", encoding="utf-8") as handle:
            require_unaliased_grant_file(path, harm=_HARM, fd=handle.fileno())
            if os.fstat(handle.fileno()).st_size > BACKEND_PINS_MAX_BYTES:
                raise BackendPinStoreUnreadable(f"backend pin store at {path} is oversized")
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except BackendPinStoreUnreadable:
        raise
    except SandboxCeilingUnsealable as exc:
        raise BackendPinStoreUnreadable(f"backend pin store refused: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise BackendPinStoreUnreadable(
            f"backend pin store at {path} is unreadable: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise BackendPinStoreUnreadable(f"backend pin store at {path} is not an object")
    out: dict[str, dict[str, str]] = {}
    for key, record in raw.items():
        if (
            isinstance(key, str)
            and isinstance(record, dict)
            and isinstance(record.get("backend"), str)
            and isinstance(record.get("owner"), str)
        ):
            out[key] = {"backend": record["backend"], "owner": record["owner"]}
    return out


def load_backend_pin_records(path: str | None = None) -> dict[str, dict[str, str]]:
    """Every record keyed by slot key; ``{}`` when ABSENT; raises when UNREADABLE.

    The READER's full view -- ``{"backend", "owner"}`` per slot key -- for a
    consumer that must check the owner before it trusts the pin (the restore
    paths, through :func:`apply_restored_pin`). Absent is "no pins". Anything
    else that is not a well-formed store raises :class:`BackendPinStoreUnreadable`
    rather than answering ``{}``: "no pins" read from an unreadable document
    would restore every explicitly pinned chat as inherit and run its next prompt
    on the global backend.
    """
    p = backend_pins_path() if path is None else path
    return _read_records_strict(p)


def load_backend_pins(path: str | None = None) -> dict[str, str]:
    """Every recorded pin keyed by slot key, OWNER DROPPED; same failure contract as above.

    A diagnostic view (which backend each key names), never a restore input: a
    restore must compare the owner, so it reads :func:`load_backend_pin_records`.
    """
    return {key: record["backend"] for key, record in load_backend_pin_records(path).items()}


def load_backend_pins_snapshot(path: str | None = None) -> Mapping[str, Mapping[str, str]]:
    """:func:`load_backend_pin_records` for a restore path: :data:`PINS_UNREADABLE` instead of raising.

    A restore must not abort because one document is unreadable -- the chats
    still exist and their transcripts are readable -- so it takes this snapshot
    and hands it to :func:`apply_restored_pin` per slot, which turns the marker
    into an UNRESOLVED pin on each slot rather than an inherited one.
    """
    try:
        return load_backend_pin_records(path)
    except BackendPinStoreUnreadable as exc:
        logger.error("%s; restored chats keep their backend pin UNRESOLVED until it reads", exc)
        return PINS_UNREADABLE


def apply_restored_pin(slot: object, records: Mapping[str, Mapping[str, str]]) -> None:
    """Stamp *slot*'s pin from a restore snapshot: owner-checked, failing closed on the marker.

    With a readable snapshot the slot's ``acp_backend`` is the recorded pin ONLY
    when the record's ``owner`` is this slot's ``created_at`` -- the creation
    identity the writer recorded. No record, or a record another creation owns
    (a stale pin left by a same-key chat whose delete-time cleanup failed), is
    "inherit": the chat never selected anything, and a pin it did not record must
    not route its prompts. With :data:`PINS_UNREADABLE` the pin is UNRESOLVED:
    ``acp_backend`` stays ``None`` so nothing reads a backend the store did not
    say, and ``backend_pin_unresolved`` is set so dispatch refuses to send until
    the store reads again (``chat_runner``) instead of running the prompt on the
    default.
    """
    if records is PINS_UNREADABLE:
        setattr(slot, "acp_backend", None)
        setattr(slot, "backend_pin_unresolved", True)
        return
    key = getattr(slot, "key")
    record = records.get(key)
    if record is None:
        backend = None
    elif record.get("owner") == getattr(slot, "created_at", None):
        backend = record.get("backend")
    else:
        logger.warning(
            "backend pin for slot %s is owned by another creation of that key; ignored",
            key,
        )
        backend = None
    setattr(slot, "acp_backend", backend)
    setattr(slot, "backend_pin_unresolved", False)


def pin_for(slot_key: str, path: str | None = None) -> str | None:
    """The pin recorded for *slot_key*: a backend id, ``""`` for Kiro, ``None`` for none.

    Raises :class:`BackendPinStoreUnreadable` like :func:`load_backend_pins`.
    """
    return load_backend_pins(path).get(slot_key)


def _write_records(path: str, records: Mapping[str, Mapping[str, str]]) -> None:
    from kiro_crew.atomic_write import atomic_write

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write(path, json.dumps(records, indent=2, sort_keys=True) + "\n")


def set_pin(slot_key: str, backend: str, *, owner: str, path: str | None = None) -> None:
    """Record *backend* as the pin of the slot *slot_key* whose creation identity is *owner*.

    Raises ``OSError`` (including :class:`BackendPinStoreUnreadable`) when the
    document cannot be read as a store or cannot be written; nothing is written
    in either case. The caller decides what a failed persist means for the
    in-memory pin (the backend route rolls it back; creation retracts the slot).
    """
    p = backend_pins_path() if path is None else path
    with _LOCK:
        current = _read_records_strict(p)
        record = {"backend": backend, "owner": owner}
        if current.get(slot_key) == record:
            return
        current[slot_key] = record
        _write_records(p, current)


def forget_pin(slot_key: str, *, owner: str, path: str | None = None) -> bool:
    """Drop *slot_key*'s pin if it is still held by the slot created as *owner*.

    Compared and deleted under the store lock, in one read-modify-write: a
    replacement chat created under the same key after the deleted one was popped
    carries a different ``created_at``, so its record survives a delete that
    finishes late. Returns True when a record was removed. Raises ``OSError``
    when the store is unreadable or cannot be written.
    """
    p = backend_pins_path() if path is None else path
    with _LOCK:
        current = _read_records_strict(p)
        record = current.get(slot_key)
        if record is None or record["owner"] != owner:
            return False
        del current[slot_key]
        _write_records(p, current)
    return True
