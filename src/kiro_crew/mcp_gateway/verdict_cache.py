"""Remember, per machine, what a shareability pre-flight concluded.

Nothing about which MCP servers a machine runs ships with Kiro Crew. Each host
derives its own verdicts and keeps them here, which is why there is no curated
list of server names in the repository and none has to leave the host.

One row per configured server
-----------------------------
The row is keyed by the server NAME, and the execution identity it was measured
under is a FIELD inside the row. Reading compares that field against the
server's current identity and treats a mismatch as no measurement at all, so an
upgraded binary or an edited command still forces a re-measurement — the same
invalidation an identity-keyed row would give.

Keying by identity instead looks equivalent and is not: it makes one server
produce a NEW row on every command edit and every binary upgrade, so the file
grows with config churn and needs a size cap, an eviction policy, and a
newest-wins rule for readers who only know a name. Overwriting one row per
server removes all three. Row count equals the number of configured servers,
which is bounded by the config rather than by history.

The identity is the same material ``PoolKey`` is built from — command+args,
effective env, resolved launch fingerprint — plus ``SCHEMA``, so a smarter
pre-flight does not inherit conclusions an older one reached with less evidence.

What is deliberately NOT stored here
------------------------------------
Observed hazards. Those live in ``hazards`` and outrank anything here: a cached
"safe" must never be able to rescue a server the gateway has watched misbehave.
Keeping the two stores separate is what makes that ordering impossible to get
wrong by editing one file.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.mcp_gateway.record_json import finite_float

logger = logging.getLogger(__name__)

VERDICT_CACHE_FILENAME = "shareability-verdicts.json"

#: Bump when the pre-flight learns a new check, so older verdicts are re-derived
#: rather than trusted. Part of the identity string, so bumping it makes every
#: stored row read as stale instead of wiping the file.
SCHEMA = 1


@dataclass(frozen=True)
class Identity:
    """What a measurement was taken under. Stored in the row, not used as its key."""

    command_args_hash: str
    env_hash: str
    binary_version: str
    schema: int = SCHEMA

    def as_str(self) -> str:
        return "\u0000".join(
            (self.command_args_hash, self.env_hash, self.binary_version, str(self.schema))
        )


@dataclass(frozen=True)
class CachedPreflight:
    """A stored pre-flight MEASUREMENT — not a final verdict.

    Only the expensive part is stored. The cheap evidence (declared env names,
    advertised capabilities, observed hazards) is re-read on every request and
    recombined by ``shareability.assess``, so a hazard observed a minute ago
    changes the answer immediately without invalidating anything here. Storing
    the composite instead would go stale the moment the ledger grew an entry.

    ``ran`` distinguishes "provoked and found nothing" from "could not provoke
    it", and callers must branch on it before trusting ``caller_sensitive``.

    ``identity`` is what the measurement was taken under. A reader that knows the
    server's current identity compares it; a reader that only knows the name (the
    dashboard row builder, which does no IO) reads the row as-is and accepts that
    a just-changed server shows its previous measurement until the next probe.
    """

    ran: bool
    caller_sensitive: bool
    reasons: tuple[str, ...]
    evaluated_at: float
    identity: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "callerSensitive": self.caller_sensitive,
            "reasons": list(self.reasons),
            "evaluatedAt": self.evaluated_at,
            "identity": self.identity,
        }


class VerdictCache:
    """One row per configured server, as a single JSON object."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, CachedPreflight] = {}
        # Servers whose recommendation has already been written into config.
        # Keyed by NAME for the same reason the rows are: an operator who
        # switches a server off must stay switched off across an upgrade of that
        # server, and an identity-keyed marker would re-flip it the moment the
        # binary version changed.
        self._applied: set[str] = set()
        self._dirty = False

    def load(self) -> None:
        """Read the file. Absence, corruption and a future shape all read empty.

        Reading empty means "nothing evaluated yet", which costs a re-evaluation
        and never a wrong answer. Guessing at an unreadable file's meaning is the
        only outcome that could produce one.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # UnicodeDecodeError is raised by read_text BEFORE json.loads sees the
            # bytes, and it is a ValueError rather than a JSONDecodeError, so the
            # two obvious clauses do not cover it. A single invalid byte in this
            # file is exactly the corruption this loader exists to absorb.
            logger.warning("shareability cache: ignoring unreadable %s: %s", self._path, exc)
            return
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return
        applied = raw.get("applied") if isinstance(raw, dict) else None
        if isinstance(applied, list):
            self._applied = {a for a in applied if isinstance(a, str) and a}
        for name, value in entries.items():
            if not isinstance(name, str) or not name or not isinstance(value, dict):
                continue
            ran = value.get("ran")
            if not isinstance(ran, bool):
                # A row that cannot say whether the pre-flight ran is unusable:
                # treating it as "ran" would fabricate evidence.
                continue
            reasons = value.get("reasons")
            identity = value.get("identity")
            self._entries[name] = CachedPreflight(
                ran=ran,
                caller_sensitive=value.get("callerSensitive") is True,
                reasons=tuple(r for r in reasons if isinstance(r, str))
                if isinstance(reasons, list)
                else (),
                evaluated_at=finite_float(value.get("evaluatedAt")),
                # A row written before identities were stored reads as identity
                # "", which matches nothing, so it is re-measured rather than
                # trusted. That is the safe direction for a format change.
                identity=identity if isinstance(identity, str) else "",
            )

    def get(self, server_name: str, identity: Identity) -> CachedPreflight | None:
        """The row for *server_name*, only if it was measured under *identity*.

        An identity mismatch reads as "not measured": the stored conclusion
        describes a program that is no longer what this name launches.
        """
        row = self._entries.get(server_name)
        if row is None or row.identity != identity.as_str():
            return None
        return row

    def get_by_name(self, server_name: str) -> CachedPreflight | None:
        """The row for *server_name* without checking identity.

        For the dashboard row builder, which is deliberately IO-free and so
        cannot resolve a binary fingerprint. A row whose identity has moved on
        is corrected by the next probe.
        """
        return self._entries.get(server_name)

    def put(self, server_name: str, identity: Identity, verdict: CachedPreflight) -> None:
        """Overwrite the row for *server_name*. One server, one row, always."""
        self._entries[server_name] = CachedPreflight(
            ran=verdict.ran,
            caller_sensitive=verdict.caller_sensitive,
            reasons=verdict.reasons,
            evaluated_at=verdict.evaluated_at,
            identity=identity.as_str(),
        )
        self._dirty = True

    def was_applied(self, server_name: str) -> bool:
        """True when this server's recommendation was already written to config.

        The gate that stops a later start from undoing an operator's choice: a
        server is seeded once, and after that the config is theirs.
        """
        return server_name in self._applied

    def mark_applied(self, server_name: str) -> None:
        if server_name and server_name not in self._applied:
            self._applied.add(server_name)
            self._dirty = True

    def server_names(self) -> set[str]:
        """Every server name with a stored measurement."""
        return set(self._entries)

    def flush(self) -> None:
        """Persist when dirty. Blocking IO — keep it off the event loop."""
        if not self._dirty:
            return
        payload = {
            "entries": {k: v.to_json() for k, v in sorted(self._entries.items())},
            "applied": sorted(self._applied),
        }
        try:
            atomic_write(self._path, json.dumps(payload, indent=2) + "\n")
        except OSError as exc:
            # A cache we cannot persist costs a re-evaluation next start, so
            # this is a warning and never fatal.
            logger.warning("shareability cache: could not write %s: %s", self._path, exc)
            return
        self._dirty = False

    def __len__(self) -> int:
        return len(self._entries)


def cache_path(runtime_dir: Path) -> Path:
    """Sibling of ``hot-keys.json`` and ``observed-hazards.json``."""
    return runtime_dir / VERDICT_CACHE_FILENAME


def load_cache(runtime_dir: Path) -> VerdictCache:
    cache = VerdictCache(cache_path(runtime_dir))
    cache.load()
    return cache


def now() -> float:
    """Wall clock for ``evaluated_at``. Seam so tests need no monkeypatching."""
    return time.time()
