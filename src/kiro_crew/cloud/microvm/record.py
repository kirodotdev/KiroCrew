"""One record per MicroVM crew, in the gateway's own product-owned state store.

The reference implementation for this lane keeps the equivalent record in
DynamoDB with optimistic concurrency on an integer version. This lane's control
plane is the owner's own local gateway, so what is left is a small JSON document
beside ``cloud_launch_state.json``, written with the same atomic-publish
discipline the rest of ``cloud`` uses and under a writer lock, because the
gateway is not the only process that may hold it.

It is a FILE OF ITS OWN rather than a key inside ``cloud_launch_state.json``
because the two hold different shapes: that file is one record of three fields
whose reader refuses anything else, and this is a collection that grows by crew.
Both are product-owned state under ``config_dir()`` and neither is the
operator-owned ``cloud.json``, which this lane reads and never writes.

What the record must carry is decided by what a recovery needs, not by what is
convenient to store. ``activation_id`` is kept because a launch that failed after
minting an activation leaked one, and a record is the only way to tell a leak
from a live crew. ``control_secret_ref`` and ``crew_name`` are kept because a
turn needs both and neither is recoverable from anything else the gateway holds:
the reference names a secret the launch minted, and the name is the bundle's, not
the launch tag's. ``wall_at`` is kept because the platform's maximum lifetime is
not adjustable and not extendable, so the owner has to be able to see when this
crew's VM will be taken.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Iterator, Optional

from kiro_crew import platform_lock_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cloud.microvm import states
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

_FILENAME = "microvm_crews.json"

#: Ceiling on the whole file, checked BEFORE it is parsed, for the reason
#: ``cloud/config.py`` gives for its own: ``json.loads`` builds the document in
#: memory, so a bound applied to the parsed result runs after the damage. This
#: store is read on every lifecycle tick, so an oversized file is a repeated cost
#: and not a one-off.
_MAX_FILE_BYTES = 1 << 21

#: Ceiling on how many crews one install may track. Reached only by a leak: the
#: regional memory quota stops a real fleet long before this, and the cap exists
#: so a runaway writer cannot grow an unbounded document the tick then re-reads.
_MAX_RECORDS = 256

#: Ceiling on any one string read back from the file.
_MAX_STRING_LEN = 2048


@dataclass(frozen=True)
class CrewRecord:
    """Everything the control plane knows about one MicroVM crew.

    Frozen, so a caller holding a record cannot mutate the store's copy by
    accident; :meth:`evolve` returns a new one. Every field defaults, so a
    document written by an older build reads back with the new fields absent
    rather than refusing to load -- the store is the owner's own state and a
    forward-incompatible read would cost them a crew.
    """

    #: The launch tag. The record's identity, and the same tag ``run_launch``
    #: makes before preflight and passes to ``provision`` and ``teardown`` -- so a
    #: rollback can find this crew without knowing the MicroVM id.
    tag: str = ""
    state: str = states.PENDING
    #: The platform's id for the VM, empty until ``RunMicrovm`` answers.
    microvm_id: str = ""
    #: The SSM managed-node id the guest registered as, empty until it does.
    mi_id: str = ""
    #: The hybrid activation minted for this VM. Kept after the VM is gone,
    #: because an activation with no registrations is what the sweeper reaps.
    activation_id: str = ""
    #: The VM's own HTTPS endpoint, as the platform returned it.
    endpoint: str = ""
    #: An ARN or local reference to the per-crew control secret. NEVER the value:
    #: this file is read on every turn and a secret in it would be a secret in
    #: every backup of the owner's config directory.
    control_secret_ref: str = ""
    #: Epoch seconds. When the platform will terminate the VM whatever its state.
    wall_at: float = 0.0
    #: The lifetime this VM was launched with, in seconds, as asked for.
    wall_seconds: int = 0
    #: Epoch seconds of the last observation that reached the guest. Zero means
    #: never, which :func:`states.effective_state` reads as unknown.
    last_observed_at: float = 0.0
    #: Incremented on every launch. A readiness observation from an earlier
    #: generation must not satisfy a later one, or the roster offers "Open crew"
    #: seconds after a relaunch using the previous VM's answer.
    generation: int = 0
    profile: str = ""
    region: str = ""
    #: The crew name this VM SERVES, which is not :attr:`tag`. The tag is this
    #: launch's id, minted by the launcher; the name comes from the bundle's
    #: manifest and is what the guest sets ``SMC_CREW_NAME`` to. A turn addresses
    #: the crew by name, so the two are needed for different things and a turn
    #: that sent the tag is answered with the guest's own "not served here".
    #:
    #: Recorded at launch rather than derived at turn time, for the reason
    #: :attr:`control_secret_ref` is: the launch is the only party that holds the
    #: bundle the image was built from.
    crew_name: str = ""
    created_at: float = field(default_factory=time.time)

    def evolve(self, **changes: Any) -> "CrewRecord":
        """A copy with *changes* applied. The only way to change a record."""
        return replace(self, **changes)

    def effective_state(self, *, now: Optional[float] = None) -> str:
        """What to report for this crew, degrading a stale live state to unknown."""
        moment = time.time() if now is None else now
        age = None if not self.last_observed_at else moment - self.last_observed_at
        return states.effective_state(self.state, age_seconds=age)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> Optional["CrewRecord"]:
        """One record, or ``None`` for anything that is not one.

        Unknown keys are DROPPED rather than refused, so a document written by a
        newer build still loads on an older one. A known key of the wrong type
        drops the whole record: a ``wall_at`` that is a string would otherwise
        reach the wall arithmetic and raise there, on a schedule, with no owner
        watching.
        """
        if not isinstance(data, dict):
            return None
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            annotation = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
            if annotation == "str":
                if not isinstance(value, str) or len(value) > _MAX_STRING_LEN:
                    return None
            elif annotation == "int":
                # ``bool`` is an ``int`` subclass, so ``true`` would otherwise read
                # as a count of one.
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
            elif annotation == "float":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                value = float(value)
            else:
                return None
            kwargs[f.name] = value
        if not kwargs.get("tag"):
            return None
        if kwargs.get("state", states.PENDING) not in states.STORED_STATES:
            return None
        return cls(**kwargs)


def store_path() -> Path:
    """Where the records live. Under the data home, beside the launch record."""
    return config_dir() / _FILENAME


class CrewStoreUnreadable(RuntimeError):
    """A write was refused because the store could not be read first.

    Its own type rather than a bare ``RuntimeError`` so a caller can tell this
    apart from a failed write: nothing was written, and the file on disk is
    whatever it already was. The fix is to repair or move the file, not to retry.
    """


class CrewStore:
    """Read and publish the whole record set.

    Whole-document writes, not per-record ones. The set is small, one gateway
    writes it, and a partial write of a collection is the failure mode a reopen
    cannot survive -- so the file is replaced atomically or not at all.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or store_path()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, CrewRecord]:
        """Every record by tag. An unusable file reads as no records.

        Tolerant for the reason ``CloudConfig.load`` is: this runs on a timer and
        from every request that lists crews, so raising would turn one bad byte
        into a dashboard that shows nothing. A record that fails to parse is
        dropped and logged; the rest still load, because one unreadable crew must
        not hide the others.
        """
        try:
            with open(self._path, "rb") as fh:
                raw = fh.read(_MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("microvm crew store %s could not be read: %s", self._path, exc)
            return {}
        if len(raw) > _MAX_FILE_BYTES:
            logger.warning(
                "microvm crew store %s is larger than %d bytes", self._path, _MAX_FILE_BYTES
            )
            return {}
        try:
            document = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            logger.warning("microvm crew store %s is not readable JSON: %s", self._path, exc)
            return {}
        if not isinstance(document, dict):
            return {}
        entries = document.get("crews")
        if not isinstance(entries, list) or len(entries) > _MAX_RECORDS:
            return {}
        out: dict[str, CrewRecord] = {}
        for entry in entries:
            record = CrewRecord.from_json(entry)
            if record is None:
                logger.warning("microvm crew store %s holds an unreadable record", self._path)
                continue
            out[record.tag] = record
        return out

    def get(self, tag: str) -> Optional[CrewRecord]:
        return self.load().get(tag)

    def publish(self, records: dict[str, CrewRecord]) -> None:
        """Replace the file with *records*, atomically.

        Refuses to write more than :data:`_MAX_RECORDS`, so a leak cannot grow a
        document the tick then re-reads on every pass.
        """
        if len(records) > _MAX_RECORDS:
            raise ValueError(
                f"refusing to store {len(records)} MicroVM crews: the cap is {_MAX_RECORDS}, "
                "and reaching it means crews are being created and not reaped"
            )
        payload = {
            "version": 1,
            "crews": [records[tag].to_json() for tag in sorted(records)],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def load_for_write(self) -> dict[str, CrewRecord]:
        """Every record, or a REFUSAL if the file could not be read.

        The strict twin of :meth:`load`, and the difference is which mistake each
        one is allowed to make. A READER that raises turns one bad byte into a
        dashboard showing nothing, so :meth:`load` is tolerant. A WRITER that
        reads tolerantly gets an empty dict from an unreadable file, adds its one
        record to it, and publishes that -- erasing every crew the file held, and
        with them the secret reference and the crew name a turn to each of those
        crews needs. The VMs keep running and billing, and nothing can reach them.

        Distinguished by RE-READING rather than by a flag, because the tolerant
        path cannot tell "no records" from "unreadable": both are ``{}``. Here the
        same failures are raised instead.
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            # A store that has never existed genuinely holds no crews, which is
            # the one empty answer that is true.
            return {}
        except OSError as exc:
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} could not be read ({exc}), so a write "
                "would publish a store with every existing crew missing. Refusing: those "
                "records are what makes those crews reachable."
            ) from exc
        if len(raw) > _MAX_FILE_BYTES:
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} is larger than {_MAX_FILE_BYTES} bytes, "
                "so it was not parsed; a write now would drop every record in it"
            )
        try:
            document = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} is not readable JSON ({exc}), so a write "
                "would replace it with one record and lose the rest"
            ) from exc
        if document is None:
            return {}
        entries = document.get("crews")
        if not isinstance(document, dict) or not isinstance(entries, list):
            raise CrewStoreUnreadable(
                f"the microvm crew store {self._path} is not the shape this lane writes, so a "
                "write would discard whatever it does hold"
            )
        # PER-RECORD too, not only per-file. The tolerant read DROPS an entry it
        # cannot parse and keeps the rest, which is right for a dashboard: one
        # unreadable crew must not hide the others. But a write publishes exactly
        # what loaded, so the dropped entry does not come back -- and what it held
        # was that crew's secret reference and name, which is what a turn to it
        # needs. The VM keeps running and nothing can reach it.
        unreadable = sum(1 for entry in entries if CrewRecord.from_json(entry) is None)
        if unreadable:
            raise CrewStoreUnreadable(
                f"{unreadable} record(s) in the microvm crew store {self._path} could not be "
                "parsed. A write would publish the file without them, and a crew whose "
                "record is gone cannot be reached or torn down. Repair or move the file first."
            )
        return self.load()

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        """Serialise one read-modify-write against every other process's.

        :meth:`publish` replaces the whole document, so a write is only safe if
        the read it was computed from is still current when it lands. Without
        this lock it is not: the lane has TWO writers by design -- the gateway
        serving the owner's clicks, and the separately scheduled ``tick`` cron in
        its own interpreter -- and an atomic rename makes each write indivisible
        without making the pair of them ordered. Two concurrent passes both read
        the same document, each adds its own change, and the second rename
        discards the first. What it discards is a crew's row, and with it the
        secret reference and crew name a turn to it needs: the VM keeps running
        and billing, and nothing can address it.

        ``platform_lock_compat.file_lock``, for the same reason
        :func:`launch_state._writer_lock` uses it: ``kirocrew cloud`` runs on the
        owner's own machine, Windows included, and the ``flock_compat`` shim is a
        no-op there. The lock lives in a sibling ``.lock`` file rather than on the
        document, because the Windows path locks a byte of the file it is given
        and the document is replaced by rename underneath it.

        A lock that cannot be taken RAISES, which is what ``file_lock`` already
        does and is the right direction here: every caller of a write is a
        provisioning step or a tick that reports its own failure, so a refusal
        costs one pass and an unserialised write costs a crew.
        """
        lock_path = self._path.with_name(self._path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.ExitStack() as stack:
            handle = stack.enter_context(open(lock_path, "a+"))
            stack.enter_context(platform_lock_compat.file_lock(handle.fileno(), exclusive=True))
            yield

    def put(self, record: CrewRecord) -> CrewRecord:
        """Write one record, leaving every other alone. Returns what was stored."""
        with self._writer_lock():
            records = self.load_for_write()
            records[record.tag] = record
            self.publish(records)
        return record

    def delete(self, tag: str) -> bool:
        """Remove one record. ``False`` when there was none."""
        with self._writer_lock():
            records = self.load_for_write()
            if tag not in records:
                return False
            del records[tag]
            self.publish(records)
        return True

    def patch_live(self, tag: str, *, generation: int, **changes: Any) -> Optional[CrewRecord]:
        """Apply *changes* only while *tag* is still the live crew of *generation*.

        ``None`` when the fence refuses, which is the answer for all three ways the
        crew a caller is acting for can stop being the crew on disk: the row is
        gone, it reached a terminal state, or a newer launch took the tag. The
        caller holds resources at that point -- a VM, a registered node -- and
        refusing is what tells it to clean them up instead of recording them.

        Why this exists beside :meth:`put`. A provisioning step reads a record,
        then WAITS: ``RunMicrovm`` is answered long before the guest registers, and
        the online poll runs for minutes. ``put`` writes the record whole, so a
        caller that evolves the object it read before the wait writes every field
        back as it was -- including a ``state`` a teardown moved to ``terminated``
        while it waited. The VM is gone and the row says running, which is a crew
        the sweeper keeps reporting and nothing can reach.

        So the changes are applied to the record as it is ON DISK, inside the
        writer lock, exactly as :meth:`apply_event` computes its transition there.
        The caller's own copy supplies the new facts and never the old ones.

        The generation is the discriminator a state check alone does not give:
        tear a crew down and launch the same tag again, and the row is live and
        non-terminal while belonging to a different VM.
        """
        with self._writer_lock():
            records = self.load_for_write()
            current = records.get(tag)
            if current is None or current.state in states.TERMINAL_STATES:
                return None
            if current.generation != generation:
                return None
            records[tag] = current.evolve(**changes)
            self.publish(records)
            return records[tag]

    def apply_event(self, tag: str, event: str, **changes: Any) -> CrewRecord:
        """Move one crew through :func:`states.transition` and store the result.

        The state and the facts that came with it are written in ONE publish.
        Writing the state first and the facts second leaves a window where a
        gateway restart keeps the new state and loses what came with it, and a
        state whose own facts are missing is a crew the next reader cannot act on.

        Raises :class:`states.IllegalTransition` for an event the table has no row
        for, which is a caller bug and must not be stored as a state.

        The transition is computed INSIDE the writer lock, not only written there.
        The event's legality depends on the state the record is in, so a read
        outside the lock decides against a state another writer may already have
        moved on from -- and the publish would then store a state no edge of the
        table ever allowed.
        """
        with self._writer_lock():
            records = self.load_for_write()
            current = records.get(tag)
            next_state = states.transition(current.state if current else None, event)
            if current is None:
                current = CrewRecord(tag=tag)
            records[tag] = current.evolve(state=next_state, **changes)
            self.publish(records)
            return records[tag]

    def iter_records(self) -> Iterator[CrewRecord]:
        """Every record, for a sweep that only reads."""
        yield from self.load().values()
