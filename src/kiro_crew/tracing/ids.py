"""Deterministic trace and span ids: one formula every producer can compute.

Core, a hook consumer and an external ACP tap must derive the SAME id for the
same span, so each id is a hash of things all of them can see, never a random
number one of them minted::

    H(kind, p1, ..., pn) = sha256("kirocrew.v1|" + ns + "|" + kind
                                  + "|" + esc(p1) + ... + "|" + esc(pn))
    span_id  = first 16 hex of H   (hex 16..31 when those are all zero)
    trace_id = first 32 hex of H("trace." + kind, ...)   (hex 32..63 when zero)
    esc(p)   = p with "%" -> "%25" and "|" -> "%7C"

``ns`` is the per-install trace namespace (:func:`load_namespace`): several
inputs are unique only within one install, and two installs exporting to one
backend must not collide. A turn's trace id skips the hash, because a turn id
is already a random 128-bit hex value (:func:`turn_trace_id`).

``test/fixtures/tracing/id_conformance.json`` holds input -> id vectors for
every kind below; an external producer runs the same vectors. Stdlib only.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from kiro_crew.owner_only_files import owner_only_opener_for

#: Version tag at the front of every hashed input. A change to the formula
#: changes this tag, so ids from two formulas never compare equal by accident.
VERSION = "kirocrew.v1"

#: Span kinds and their inputs (docs/system-specs/modules/metrics.md, "Traces").
KIND_TURN = "turn"  # turn_id, attempt
KIND_CHAT = "chat"  # turn_id, attempt
KIND_USER_PROMPT = "user.prompt"  # turn_id, attempt
KIND_TURN_COMPLETE = "turn.complete"  # turn_id, attempt
KIND_TOOL = "tool"  # acp_session_id, tool_call_id

_HEX_DIGITS = frozenset("0123456789abcdef")
_ZERO16 = "0" * 16
_ZERO32 = "0" * 32


def is_hex32(value: str) -> bool:
    """Whether *value* is exactly 32 lowercase hex digits."""
    return len(value) == 32 and set(value) <= _HEX_DIGITS


def esc(part: object) -> str:
    """Escape one input so a ``|`` inside it cannot shift the tuple boundary."""
    return str(part).replace("%", "%25").replace("|", "%7C")


def digest(ns: str, kind: str, *parts: object) -> str:
    """The 64-hex ``H(kind, *parts)`` under namespace *ns*."""
    material = "|".join([VERSION, ns, kind, *(esc(p) for p in parts)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def span_id(ns: str, kind: str, *parts: object) -> str:
    """16-hex span id for a span of *kind* with *parts* as its inputs."""
    full = digest(ns, kind, *parts)
    return full[:16] if full[:16] != _ZERO16 else full[16:32]


def trace_id(ns: str, kind: str, *parts: object) -> str:
    """32-hex trace id for a run root of *kind*: ``H("trace." + kind, ...)``."""
    full = digest(ns, "trace." + kind, *parts)
    return full[:32] if full[:32] != _ZERO32 else full[32:]


def turn_trace_id(ns: str, turn_id: str) -> str:
    """A turn's trace id: the turn id itself, lowercased, when it is 32 hex digits
    and not all zero.

    The dashboard mints ``uuid4().hex``, which is already a valid, random trace
    id, and hook records carry the same value raw. Any other value is hashed
    into one.
    """
    value = str(turn_id).lower()
    if is_hex32(value) and value != _ZERO32:
        return value
    return trace_id(ns, KIND_TURN, turn_id)


#: File name of the install's trace namespace under the traces directory.
NAMESPACE_FILE = "namespace"


def load_namespace(directory: Path) -> str:
    """Read, or mint once, this install's random 128-bit trace namespace.

    Written in full to a private temporary file and then published with
    ``os.link``, which never replaces an existing name: two processes racing the
    first start agree on one value (the loser reads the winner's), and a reader
    never sees a half-written file. A file that does not hold 32 lowercase hex
    is not trusted and raises, so tracing stays off rather than hashing under a
    namespace another install could share.
    """
    path = Path(directory) / NAMESPACE_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        minted = os.urandom(16).hex()
        staged = path.with_name(f".{NAMESPACE_FILE}-{os.getpid()}-{minted[:8]}")
        try:
            with open(staged, "x", encoding="utf-8", opener=owner_only_opener_for(staged)) as fh:
                fh.write(minted + "\n")
            os.link(staged, path)
            return minted
        except FileExistsError:
            value = path.read_text(encoding="utf-8").strip()
        finally:
            staged.unlink(missing_ok=True)
    if not is_hex32(value):
        raise ValueError("trace namespace file is malformed")
    return value
