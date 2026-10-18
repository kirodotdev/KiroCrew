"""Fakes and fixtures shared by the ``test_sidecar_durability_gaps*`` modules.

The slot key and the transcript stem the production mapping relates, settings with both
authority files present, the content-addressed and authority-pair keys a test asserts with, a
store that models S3's conditional writes, a bucket that serves only the objects a test plants,
and a ``time`` stand-in whose ``monotonic`` the test controls. Each module imports what it uses
by name.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from container.common import keys
from container.front import transcript as front_transcript
from container.sidecar import backup as backup_mod
from container.sidecar import generation as generation_mod
from container.sidecar.store import ObjectAbsent

from ._settings_helper import make_settings

STEM = "dashboard_cust-91"

#: The SLOT KEY the backend's index names this conversation by. The transcript file carries
#: a ``dashboard_`` prefix on top of it, and the two are asserted equal through the function
#: that actually names the file rather than by spelling the prefix twice -- comparing the two
#: namespaces directly is a defect this suite once agreed with.
SLOT_KEY = "cust-91"
assert (
    front_transcript.transcript_stem(SLOT_KEY) == STEM
), "the slot key and the transcript stem must be related by the production mapping"


class _ClockProxy:
    """Stand in for the ``time`` module with a controlled ``monotonic`` only (GPT F2).

    ``backup.py`` does ``import time`` and calls ``time.monotonic()``, so patching
    ``backup_mod.time.monotonic`` would mutate the stdlib ``time`` SINGLETON -- every other
    xdist worker thread reading ``time.monotonic()`` would then receive this test's finite
    synthetic sequence (and run off its end) instead of real time. Patching the module-local
    binding ``backup_mod.time`` with this proxy instead confines the override to the module
    under test: ``monotonic`` returns the test's value and every other attribute delegates to
    the real module, so ``time.sleep`` and friends keep working for everyone else.

    Pass a zero-arg callable (``lambda: next(clock)`` for a scripted sequence, or
    ``lambda: now`` for a frozen reading); it is called on each ``monotonic()``.
    """

    def __init__(self, monotonic: "Callable[[], float]") -> None:
        self._monotonic = monotonic

    def monotonic(self) -> float:
        return self._monotonic()

    def __getattr__(self, name: str):
        return getattr(time, name)


def _settings(tmp_path: Path, *, interval: int = 60):
    s = make_settings(tmp_path, crew="crew-91", prefix="crews")
    for name in keys.AUTHORITY_NAMES:
        (s.config_dir / name).write_bytes(b"{}")
    return s.__class__(**{**s.__dict__, "backup_interval_secs": interval})


def _transcript(settings, payload: bytes, stem: str = STEM) -> Path:
    path = settings.sessions_dir / f"{stem}{keys.TRANSCRIPT_SUFFIX}"
    path.write_bytes(payload)
    return path


def _blob_key(settings, payload: bytes) -> str:
    """The content-addressed key the writer stores *payload* under: ``data/blob/<sha256>``."""
    import hashlib as _hashlib

    return keys.blob_key(settings, _hashlib.sha256(payload).hexdigest())


class _Recorder:
    """Accepts every put, remembers bytes, and counts cycles by their first key.

    Models S3's conditional writes well enough for the generation CAS: each stored key
    carries a monotonic ETag, ``get_etag`` returns it (or ``None`` when absent), and ``put``
    honours ``if_match``/``if_none_match`` by raising :class:`PreconditionFailed` when the
    precondition does not hold -- so a test can stage a concurrent writer and see the stale
    commit rejected.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.etags: dict[str, str] = {}
        self._etag_seq = 0

    def put(
        self,
        key: str,
        body,
        size: int,
        *,
        budget: float | None = None,
        cancel: Callable[[], bool] | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str | None:
        if if_none_match == "*" and key in self.objects:
            raise backup_mod.PreconditionFailed(f"{key} already exists")
        if if_match is not None and self.etags.get(key) != if_match:
            raise backup_mod.PreconditionFailed(f"{key} ETag is not {if_match}")
        self.objects[key] = body.read(size)
        self.puts.append(key)
        self._etag_seq += 1
        self.etags[key] = f'"etag-{self._etag_seq}"'
        # Return the ETag the write LEFT, as S3's PutObject response does: the ownership
        # claim takes its release CAS validator from here, not from a follow-up GET.
        return self.etags[key]

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        # Serves what was put, so a pointer this store committed on an earlier cycle is
        # visible when the next cycle re-reads it -- the CAS validator round-trip the
        # writer-unique generation protocol depends on.
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectAbsent(key) from None

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        # Delegates to self.get so a subclass overriding get (to serve or to raise an
        # unreadable pointer) is honoured; the ETag rides alongside, one logical GET.
        return self.get(key, limit=limit), self.etags.get(key)


def _authority_keys_in(settings, keyset) -> set[str]:
    """The authority-pair keys present in *keyset*, matched by their ``gen/<id>/<name>`` shape.

    A cycle mints a WRITER-UNIQUE generation id, so the pair's keys are not known in advance
    -- they are ``<prefix>gen/<id>/<name>``. This picks them out of whatever the store holds
    or the cycle withheld, so a test asserts about the pair without predicting the id.
    """
    prefix = keys.generations_prefix(settings)
    names = set(keys.AUTHORITY_NAMES)
    found: set[str] = set()
    for key in keyset:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        gen_id, _, name = rest.partition("/")
        if name in names and keys.is_generation_id(gen_id):
            found.add(key)
    return found


class _Bucket:
    """A bucket holding exactly the objects given, by their key's trailing name.

    A stored key CONTAINING a slash is matched as a suffix, which is how a test plants
    ``gen/<slot>/<name>``. A stored BARE name means the legacy key and must not also answer
    for a generation slot -- otherwise planting ``session_map.json`` silently populates both
    slots as well, and a survey of the slots reads a legacy-only bucket as two complete
    generations.
    """

    def __init__(self, present: dict[str, bytes]) -> None:
        self._present = present
        self.gets: list[str] = []

    def get(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        self.gets.append(key)
        for name, raw in self._present.items():
            if "/" in name:
                if key.endswith(name):
                    return raw
            elif key.endswith(f"/{name}") and keys.GENERATION_PREFIX not in key:
                return raw
        raise ObjectAbsent(key)

    def get_with_etag(
        self,
        key: str,
        *,
        limit: int,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> tuple[bytes, str | None]:
        return self.get(key, limit=limit), None

    def put(
        self,
        key: str,
        body,
        size: int,
        *,
        budget: float | None = None,
        cancel: Callable[[], bool] | None = None,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:  # pragma: no cover - unused
        raise AssertionError("the restore does not write to the bucket")


#: A fixed, well-formed generation id for the restore tests, so a planted pointer and the
#: keys it names agree without minting a fresh one each time.
_GEN = "00000000000000000001-0123456789abcdef"


def _pointer(generation_id: str = _GEN) -> bytes:
    return generation_mod.pointer_body(generation_id)
