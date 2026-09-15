"""Sidecar store for KiroCrew's per-agent bookkeeping.

kiro-cli validates ``~/.kiro/agents/*.json`` with serde ``deny_unknown_fields``
and rejects the *entire* spec on any unknown key, then silently falls back to
the default agent (``--agent <name>`` resolves to default with only a stderr
"no agent with name X found" line). KiroCrew therefore keeps its private
per-agent bookkeeping OUT of the kiro spec and in this sidecar, so every spec
stays schema-valid for kiro-cli.

Two values are tracked per agent, plus fork lineage, all kept in this sidecar
rather than the kiro spec:

- ``model_managed`` (bool): whether an agent's ``model`` should track the
  shipped ``defaults.json`` (so a default bump propagates) or is an explicit
  user pick frozen against future bumps.
- ``cc_model`` (str): a per-agent model for the ``claude_code`` provider (that
  backend can't pick a per-agent model from ``--agent`` the way kiro-cli does).
- ``forked_from`` / ``private_to`` (str): recorded on a template that is one
  crew's private copy of another template (blueprint semantics — editing a
  crew's definition forks a copy instead of mutating the shared file).

State file (``~/.kiro/crew/agent_model_state.json``, honoring ``KIROCREW_HOME``)::

    {
      "kirocrew":           {"model_managed": true},
      "kirocrew-heartbeat": {"cc_model": "auto"}
    }

This is a near-leaf module: it imports only the stdlib plus the leaf
``config.paths`` and ``atomic_write`` helpers, so it never participates in the
``agent`` <-> ``config.loader`` import cycle.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import stat
import threading
from pathlib import Path
from typing import Iterator, Mapping, MutableMapping

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

_STATE_FILENAME = "agent_model_state.json"
_MODEL_MANAGED = "model_managed"
_CC_MODEL = "cc_model"
# Fork lineage: a private copy created so a crew's definition edits stop
# landing on the shared template ("blueprint" semantics, copy-on-first-edit).
_MIRRORED_FROM = "mirrored_from"
_MIRRORED_STAT = "mirrored_stat"
_FORKED_FROM = "forked_from"
_PRIVATE_TO = "private_to"
#: The bytes a SHARED template file last held when a trusted writer put
#: them there (``sha256:<hex>``). Recorded by the app bridge when it
#: materializes ``<app>--<agent>.json`` and refreshed by every trusted
#: rewrite of that file (``agent._atomic_json_write``); read by the KAS
#: projection, which refuses to inject a file whose content does not
#: match. Absent for a file no bridge materialized (a hand-written spec,
#: a private copy), which is then not fingerprinted.
_SHARED_SHA256 = "shared_sha256"

# Guards in-process read-modify-write races (e.g. dashboard PATCH vs gateway
# refresh). ``atomic_write`` makes each WRITE atomic, but two processes can
# still interleave read-modify-write and the later stale snapshot wins —
# ``_locked()`` below adds the cross-process half.
_lock = threading.RLock()


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """Hold the in-process lock AND a cross-process advisory file lock.

    A dashboard fork (gateway process) racing a CLI model-state write is two
    processes doing read-modify-write on the same file; without this, the
    later whole-file replacement silently erases the other's entry (e.g. fork
    lineage — the private copy then surfaces as shared). Sidecar lockfile, not
    the state file's own fd, because ``atomic_write`` replaces the inode.
    """
    with _lock:
        # Lazy: platform_compat pulls in executors, and this module's
        # near-leaf import contract is what keeps it out of the
        # agent <-> config.loader cycle.
        from kiro_crew.platform_compat import file_lock

        lock_path = _state_path().with_suffix(".json.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("agent_state_lock_invalid")
            with file_lock(fd, exclusive=True, wait=True):
                yield
        finally:
            os.close(fd)


def _state_path() -> Path:
    """Return the sidecar path (resolved fresh so KIROCREW_HOME is honored)."""
    return config_dir() / _STATE_FILENAME


def _read(*, strict: bool = False) -> dict:
    """Load the sidecar. ``strict`` distinguishes ABSENT from UNREADABLE.

    Getters read lenient: a corrupt sidecar degrading to "no info" keeps the
    roster and provenance displays alive. MUTATORS must read strict — a
    read-modify-write that collapsed an unreadable-but-present file to ``{}``
    would then ``_write`` the empty dict back, silently erasing every agent's
    model and lineage state with no recovery. Only a genuinely missing file is
    empty; an existing file that cannot be read or parsed propagates.
    """
    try:
        path = _state_path()
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("agent_state_file_invalid")
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > STATE_MAX_BYTES
            ):
                raise ValueError("agent_state_file_invalid")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(STATE_MAX_BYTES + 1)
            if len(raw) > STATE_MAX_BYTES:
                raise ValueError("agent_state_too_large")
            data = json.loads(raw)
        finally:
            os.close(fd)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"{_state_path()} does not hold a JSON object")
        return {}
    return data


STATE_MAX_BYTES = 8 * 1024 * 1024


def _write(data: dict) -> None:
    payload = json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > STATE_MAX_BYTES:
        raise ValueError("agent_state_too_large")
    atomic_write(_state_path(), payload, restrict_to_owner=True)


# Schema-v1 section names live with their strict reader, below the resolver in
# the import graph. The writer/resolver use this same tuple.
CAPABILITY_SECTIONS = (
    "mcpServers",
    "tools",
    "allowedTools",
    "autoApprove",
    "skills",
    "prompt",
    "model",
    "resources",
)


def capability_transport_fields_valid(value: object) -> bool:
    """Check known wire fields without imposing the editor's request allowlist.

    Whole source transports may carry metadata or be policy-only entries.
    In particular, native OAuth permits nested oauthScopes, not just strings.
    """
    if not isinstance(value, dict):
        return False
    for field in ("command", "url", "type"):
        if field in value and (not isinstance(value[field], str) or not value[field]):
            return False
    for field in ("args", "disabledTools", "oauthScopes"):
        if field in value and (
            not isinstance(value[field], list)
            or not all(isinstance(item, str) for item in value[field])
        ):
            return False
    for field in ("env", "headers"):
        if field in value and (
            not isinstance(value[field], dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in value[field].items())
        ):
            return False
    if "disabled" in value and type(value["disabled"]) is not bool:
        return False
    if "timeout" in value:
        timeout = value["timeout"]
        if type(timeout) not in (int, float) or timeout <= 0:
            return False
        if isinstance(timeout, float) and not math.isfinite(timeout):
            return False
    if "oauth" in value:
        oauth = value["oauth"]
        if not isinstance(oauth, dict):
            return False
        for field in ("clientId", "clientSecret", "redirectUri", "clientMetadataUrl"):
            if field in oauth and not isinstance(oauth[field], str):
                return False
        if "oauthScopes" in oauth and (
            not isinstance(oauth["oauthScopes"], list)
            or not all(isinstance(scope, str) for scope in oauth["oauthScopes"])
        ):
            return False
    return True


def _capability_value_valid(section: str, key: str, value: object) -> bool:
    """Validate persisted rows, not the editor's pre-resolution request format."""
    if section == "mcpServers":
        # Source transports are preserved whole; approvals live separately.
        return (
            isinstance(value, dict)
            and "autoApprove" not in value
            and capability_transport_fields_valid(value)
        )
    if section in ("tools", "allowedTools", "autoApprove"):
        return value is True
    if section in ("prompt", "model"):
        return key == section and isinstance(value, str)
    if section == "resources":
        return isinstance(value, str) and value == key
    # Skill requests use True, but the writer persists the resolved catalog URI.
    return isinstance(value, str)


def _validate_capability_rows(value: dict) -> None:
    for field in ("accepted", "overrides"):
        sections = value[field]
        if set(sections) != set(CAPABILITY_SECTIONS):
            raise ValueError("capability_state_invalid")
        for section, rows in sections.items():
            if not isinstance(rows, dict):
                raise ValueError("capability_state_invalid")
            for key, row in rows.items():
                if not isinstance(key, str) or (section in ("prompt", "model") and key != section):
                    raise ValueError("capability_state_invalid")
                if field == "overrides":
                    if not isinstance(row, dict):
                        raise ValueError("capability_state_invalid")
                    if row.get("action") == "remove" and set(row) == {"action"}:
                        continue
                    if row.get("action") != "set" or set(row) != {"action", "value"}:
                        raise ValueError("capability_state_invalid")
                    row = row["value"]
                if not _capability_value_valid(section, key, row):
                    raise ValueError("capability_state_invalid")


def _capability_parent_valid(value: object) -> bool:
    """Validate the pinned descriptor consumed by resolution and publication."""
    return (
        isinstance(value, dict)
        and all(
            isinstance(value.get(key), str) and value[key] for key in ("name", "source", "path")
        )
        and value.get("scope") in ("global", "project")
        # An empty project is the supported global-only resolution context.
        and isinstance(value.get("project"), str)
    )


def _validate_capability_metadata(value: dict) -> None:
    """Optional bookkeeping stays optional, but present values must be usable."""
    for field in ("materialized", "revision"):
        if field in value and not isinstance(value[field], str):
            raise ValueError("capability_state_invalid")
    if "governance_generation" in value and (
        type(value["governance_generation"]) is not int or value["governance_generation"] < 0
    ):
        raise ValueError("capability_state_invalid")
    for field in ("catalog", "ordinary", "ordinary_local"):
        if field not in value:
            continue
        mapping = value[field]
        if not isinstance(mapping, dict) or not all(isinstance(key, str) for key in mapping):
            raise ValueError("capability_state_invalid")
        if field == "catalog" and not all(isinstance(uri, str) for uri in mapping.values()):
            raise ValueError("capability_state_invalid")
        if field == "ordinary_local" and not all(type(flag) is bool for flag in mapping.values()):
            raise ValueError("capability_state_invalid")


def get_capabilities(name: str) -> dict | None:
    """Read inheritance intent strictly; corruption must not become legacy mode."""
    with _lock:
        entry = _read(strict=True).get(name, {})
    if not isinstance(entry, dict):
        raise ValueError("capability_state_invalid")
    if "capabilities" not in entry:
        return None
    value = entry["capabilities"]
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("capability_state_invalid")
    if (
        not _capability_parent_valid(value.get("parent"))
        or not isinstance(value.get("accepted"), dict)
        or not isinstance(value.get("overrides"), dict)
        or value.get("status") not in ("saved", "pending")
    ):
        raise ValueError("capability_state_invalid")
    _validate_capability_rows(value)
    _validate_capability_metadata(value)
    return value


def get_publish_info(name: str) -> dict | None:
    """Read a publish receipt without treating corrupt intent as absence."""
    with _lock:
        entry = _read(strict=True).get(name, {})
    if not isinstance(entry, dict):
        raise ValueError("publish_state_invalid")
    if "publish" not in entry:
        return None
    value = entry["publish"]
    if (
        not isinstance(value, dict)
        or not all(
            isinstance(value.get(key), str) and value[key]
            for key in ("member", "source", "source_digest", "digest")
        )
        or not _capability_parent_valid(value.get("parent"))
    ):
        raise ValueError("publish_state_invalid")
    return value


def _entry(data: dict, name: str) -> dict:
    entry = data.get(name)
    return entry if isinstance(entry, dict) else {}


def get_model_managed(name: str, *, strict: bool = False) -> bool | None:
    """Return the agent's managed flag, or ``None`` when unset (legacy status).

    ``strict`` propagates the unreadable-sidecar error instead of degrading it to
    ``None``, for the reason :func:`_read` gives about mutators: a caller whose
    answer feeds a WRITE cannot treat "the file will not parse" as "no opinion
    recorded". Both map to ``None`` here, and one of them means the spec may be
    rewritten while the other means ownership is unknown. A display caller still
    wants the lenient default -- a corrupt sidecar should grey out a roster badge,
    not raise through a page render.
    """
    with _lock:
        value = _entry(_read(strict=strict), name).get(_MODEL_MANAGED)
    return bool(value) if isinstance(value, bool) else None


def set_model_managed(name: str, value: bool) -> None:
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry[_MODEL_MANAGED] = bool(value)
        data[name] = entry
        _write(data)


def get_cc_model(name: str) -> str | None:
    """Return the agent's claude_code-provider model, or ``None`` when unset."""
    with _lock:
        value = _entry(_read(), name).get(_CC_MODEL)
    return value if isinstance(value, str) and value else None


def set_cc_model(name: str, value: str | None) -> None:
    """Set (or clear, when ``value`` is falsy) the agent's claude_code model."""
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        if value:
            entry[_CC_MODEL] = str(value)
        else:
            entry.pop(_CC_MODEL, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def get_mirrored_from(name: str) -> str | None:
    """Return the fingerprint of the default spec this agent was mirrored from.

    A DERIVED agent (today only ``kirocrew-worker``) is a function of
    ``kirocrew.json``, and this is the only durable record of WHICH generation of
    that file it was derived from. It lives in the sidecar rather than in the spec
    for the reason the whole sidecar exists: kiro-cli validates a spec with
    ``deny_unknown_fields`` and DROPS one carrying a key it does not know, so a
    bookkeeping field written into the spec would cost the agent its existence.
    """
    with _lock:
        value = _entry(_read(), name).get(_MIRRORED_FROM)
    return value if isinstance(value, str) and value else None


def set_mirrored_from(name: str, value: str | None) -> None:
    """Record (or clear) the default-spec fingerprint an agent was derived from."""
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        if value:
            entry[_MIRRORED_FROM] = str(value)
        else:
            entry.pop(_MIRRORED_FROM, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def get_mirrored_stat(name: str) -> str | None:
    """Return the file IDENTITY of the default spec this agent was mirrored from.

    Paired with :func:`get_mirrored_from`: the fingerprint says WHAT was mirrored, this
    says which file instance it was read from. A caller compares it against the file's
    current identity to decide whether hashing is needed at all -- an equality test on
    one file, never an ordering test between two, because "newer" is not a property a
    restored backup or a clock that steps backwards respects.
    """
    with _lock:
        value = _entry(_read(), name).get(_MIRRORED_STAT)
    return value if isinstance(value, str) and value else None


def set_mirrored_stat(name: str, value: str | None) -> None:
    """Record (or clear) the default-spec file identity an agent was derived from."""
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        if value:
            entry[_MIRRORED_STAT] = str(value)
        else:
            entry.pop(_MIRRORED_STAT, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def get_fork_info(name: str, *, strict: bool = False) -> dict | None:
    """Return ``{"forked_from": str, "private_to": str}`` for a forked copy, else None.

    A template spec cannot carry this itself (kiro-cli rejects unknown fields),
    so lineage lives here: ``forked_from`` names the template the copy was made
    from, ``private_to`` names the ONE crew whose edits land on this copy.

    ``strict`` propagates an unreadable sidecar instead of degrading it to
    "not a fork" — the spawn gate needs the distinction (an agent it cannot
    VERIFY as a non-fork must not pass), while display callers stay lenient.
    """
    with _lock:
        entry = _entry(_read(strict=strict), name)
    origin = entry.get(_FORKED_FROM)
    owner = entry.get(_PRIVATE_TO)
    if isinstance(origin, str) and origin and isinstance(owner, str) and owner:
        return {_FORKED_FROM: origin, _PRIVATE_TO: owner}
    return None


def set_fork_info(name: str, forked_from: str, private_to: str) -> None:
    """Record that template *name* is *private_to*'s copy of *forked_from*."""
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry[_FORKED_FROM] = str(forked_from)
        entry[_PRIVATE_TO] = str(private_to)
        data[name] = entry
        _write(data)


def get_shared_template_digest(name: str) -> str | None:
    """The ``sha256:<hex>`` a trusted writer last recorded for shared
    template *name*, or None when the file is not a fingerprinted one.

    Strict on an unreadable sidecar: the caller is a spawn-time gate, and
    "cannot verify" must not degrade to "not fingerprinted".
    """
    with _lock:
        entry = _entry(_read(strict=True), name)
    digest = entry.get(_SHARED_SHA256)
    return digest if isinstance(digest, str) and digest.startswith("sha256:") else None


def set_shared_template_digest(name: str, digest: str) -> None:
    """Record the fingerprint of the bytes a trusted writer just put in
    shared template *name*'s file (``sha256:<hex>``)."""
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry[_SHARED_SHA256] = str(digest)
        data[name] = entry
        _write(data)


def clear_shared_template_digest(name: str) -> None:
    """Forget *name*'s fingerprint (the materialized file was removed)."""
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict) or _SHARED_SHA256 not in entry:
            return
        entry.pop(_SHARED_SHA256, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def clear_fork_info(name: str) -> None:
    """Drop *name*'s lineage so it lists as a shared template again.

    Only the fork fields go; ``model_managed`` / ``cc_model`` stay, which is
    what distinguishes this from :func:`prune`. A publish records lineage
    before its destination file exists and calls this once the crew's
    binding has moved onto the new name.
    """
    with _locked():
        data = _read(strict=True)
        entry = data.get(name)
        if not isinstance(entry, dict):
            return
        entry.pop(_FORKED_FROM, None)
        entry.pop(_PRIVATE_TO, None)
        if entry:
            data[name] = entry
        else:
            data.pop(name, None)
        _write(data)


def rename_private_owner(old: str, new: str) -> int:
    """Re-attribute every private copy owned by crew *old* to *new*; return the count.

    The member-id migration (``config.loader.MIGRATE_MEMBER_IDS``) re-keys an
    ``agents`` row; a private copy whose lineage still names the OLD key would
    then read as another crew's copy and every write to it -- a save, a
    publish, a reset -- would be refused. Runs under the sidecar lock, reads
    strict (a corrupt sidecar propagates rather than being erased) and is
    idempotent: an entry already naming *new* is left as it is, so the locked
    migration pass can retry it.
    """
    if not old or not new or old == new:
        return 0
    with _locked():
        data = _read(strict=True)
        moved = 0
        for entry in data.values():
            if isinstance(entry, dict) and entry.get(_PRIVATE_TO) == old:
                entry[_PRIVATE_TO] = new
                moved += 1
        if moved:
            _write(data)
        return moved


#: Sidecar key of the member-id migration's pending moves. Outside the agent-name
#: grammar (a ``::`` prefix), so it can never collide with an agent's entry; the
#: mapping sits under ``moves`` so the entry carries no fork field and every
#: per-agent reader skips it. Lives HERE, in the sealed sidecar, because the
#: moves authorize ownership renames: a marker an agent could write would let a
#: forged pair re-own another member's private state.
_PENDING_MEMBER_MOVES_KEY = "::pending_member_id_moves"


def get_pending_member_moves() -> dict[str, str]:
    """The member-id migration's pending ``{old key: minted id}`` pairs, or ``{}``.

    Strict: the caller is a migration pass deciding ownership renames, and
    "cannot read" must surface rather than read as "nothing pending".
    """
    with _lock:
        entry = _read(strict=True).get(_PENDING_MEMBER_MOVES_KEY)
    moves = entry.get("moves") if isinstance(entry, dict) else None
    if not isinstance(moves, dict):
        return {}
    return {str(k): v for k, v in moves.items() if isinstance(k, str) and isinstance(v, str) and v}


def set_pending_member_moves(moves: dict[str, str]) -> None:
    """Record pending moves (merged into any pairs an earlier pass left)."""
    with _locked():
        data = _read(strict=True)
        entry = data.get(_PENDING_MEMBER_MOVES_KEY)
        current = entry.get("moves") if isinstance(entry, dict) else None
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update({str(k): str(v) for k, v in moves.items()})
        data[_PENDING_MEMBER_MOVES_KEY] = {"moves": merged}
        _write(data)


def discard_pending_member_moves(moves: Mapping[str, str]) -> int:
    """Drop exactly the given ``{old key: minted id}`` pairs; return how many went.

    Pair-specific on purpose: two migration passes can overlap (two gateways,
    or a pass in one process while another replays), and a pass that finished
    ITS renames must not erase a pair another pass recorded and has not
    finished -- a dropped pair is a rename nobody retries, and the store keeps
    the legacy owner for good. A pair is removed only when the stored id still
    matches the one the caller renamed to; the entry goes when it empties.
    """
    with _locked():
        data = _read(strict=True)
        entry = data.get(_PENDING_MEMBER_MOVES_KEY)
        current = entry.get("moves") if isinstance(entry, dict) else None
        if not isinstance(current, dict):
            return 0
        dropped = 0
        for old, new in moves.items():
            if current.get(old) == new:
                del current[old]
                dropped += 1
        if not dropped:
            return 0
        if current:
            data[_PENDING_MEMBER_MOVES_KEY] = {"moves": current}
        else:
            data.pop(_PENDING_MEMBER_MOVES_KEY, None)
        _write(data)
        return dropped


#: Sidecar key of the crewmate ENROLLMENT record: ``{"members": {<id>: {...}}}``.
#: A crewmate exists only after the owner picked a template, named it and
#: confirmed; that confirmation is what writes an entry here, inside the hire's
#: locked publication, and the fire is what removes it. Roster membership is
#: decided by THIS record -- never by a row's presence in ``config.agents``, its
#: source, its display name, a member directory or a session that used it --
#: so a template synced from a file, a built-in, an app's materialized agent or
#: a plain crew stays a session agent until it is hired. Outside the agent-name
#: grammar (``::`` prefix) like the pending-moves record, and in the SEALED
#: sidecar for the same reason: a record an agent could write would let a
#: config edit enroll a member the owner never hired.
_CREWMATES_KEY = "::crewmates"


def _crewmate_entries(data: dict) -> dict[str, dict]:
    entry = data.get(_CREWMATES_KEY)
    members = entry.get("members") if isinstance(entry, dict) else None
    if not isinstance(members, dict):
        return {}
    return {
        str(k): v
        for k, v in members.items()
        if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("generation"), str)
    }


#: The member-id grammar, byte-equal to ``validation._AGENT_NAME_RE`` (pinned by
#: the roster tests); spelled here because ``validation`` imports config sections
#: and this module sits below them.
_MEMBER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


def enrolled_member_ids(agents: Mapping[str, object], *, strict: bool = False) -> list[str]:
    """The ids on the roster: the *agents* rows (``config.agents``, name -> row
    with a ``memory_store``) that carry a crewmate ENROLLMENT record whose
    generation is the row's private store, in the mapping's order.

    Membership is decided by the record alone (``all_crewmate_records``, written
    by a confirmed hire inside its locked publication, removed by the fire) --
    never by a row's presence in ``config.agents``, its source, its display
    name, a member directory or a session that used it. So ``default``, a
    built-in, an app's materialized agent, a file the agent sync registered and
    a plain crew-manager crew all stay session agents until the owner hires them
    by name. A record that does not cover the row's store
    (:func:`record_covers_store`: its generation, or the generation a store
    change through the API is moving it to) is a record of a member that was
    deleted and recreated under the same id: not this row.

    Nothing is enrolled on upgrade. No build before this one wrote a mark that
    only a hire could have written -- an earlier hire verb's private copy and
    ``member-`` store are exactly what the crew editor's fork and private-memory
    provisioning give a plain crew -- so every pre-existing row, that class
    included, stays a session agent until the owner hires it by name; nothing
    is deleted, retired or rebound.

    ``strict`` raises on an unreadable sidecar (a route about to WRITE for a
    member must not read "cannot verify" as "not a member"); a lenient read
    logs and answers no members, which fails closed for the roster. Lives here,
    beside the record it reads, so the gallery, the roster route and any later
    non-dashboard reader share one predicate without reaching into the HTTP layer.
    """
    try:
        records = all_crewmate_records(strict=strict)
    except (OSError, ValueError):
        if strict:
            raise
        logger.warning("crewmate record unreadable; the roster lists no members", exc_info=True)
        return []
    out: list[str] = []
    for name, row in agents.items():
        if not isinstance(name, str) or not _MEMBER_ID_RE.match(name):
            continue
        store = getattr(row, "memory_store", None)
        if not isinstance(store, str):
            store = row.get("memory_store", "") if isinstance(row, dict) else ""
        if record_covers_store(records.get(name), store):
            out.append(name)
    return out


def record_covers_store(record: dict | None, store: str) -> bool:
    """Whether enrollment *record* is the record OF a row whose private store
    is *store*: its ``generation`` names it, or its ``pending_generation`` does
    -- the store a legitimate change through the API is moving the row to
    (see :func:`stage_crewmate_generation`). One predicate for every reader,
    so the row is covered at every point of that move, crash included."""
    if not isinstance(record, dict) or not store:
        return False
    return record.get("generation") == store or record.get("pending_generation") == store


def get_crewmate_record(member_id: str, *, strict: bool = False) -> dict | None:
    """The enrollment record of *member_id*: ``{"generation", "template",
    "hired_at"}``, or ``None`` when no hire ever enrolled it. ``strict`` raises
    on an unreadable sidecar (a caller deciding membership for a WRITE must not
    read "cannot verify" as "not a member")."""
    with _lock:
        data = _read(strict=strict)
    rec = _crewmate_entries(data).get(member_id)
    return dict(rec) if rec else None


def all_crewmate_records(*, strict: bool = False) -> dict[str, dict]:
    """Every enrollment record, ``{id: record}`` (one read; the roster's form)."""
    with _lock:
        data = _read(strict=strict)
    return {k: dict(v) for k, v in _crewmate_entries(data).items()}


def set_crewmate_record(member_id: str, *, generation: str, template: str, hired_at: str) -> None:
    """Enroll *member_id*: the hire's confirmed publication records the private
    store *generation* the row was minted with (a row recreated under the same
    id with another store is NOT this crewmate), the *template* it was hired
    from and when. Strict on the record: an unreadable sidecar refuses the
    enrollment rather than overwriting whatever it held."""
    if not member_id or not generation:
        raise ValueError("a crewmate record needs the member id and its generation")
    with _locked():
        data = _read(strict=True)
        members = _crewmate_entries(data)
        members[member_id] = {
            "generation": generation,
            "template": str(template or ""),
            "hired_at": str(hired_at or ""),
        }
        data[_CREWMATES_KEY] = {"members": members}
        _write(data)


def clear_crewmate_record(member_id: str, *, generation: str | None = None) -> bool:
    """Un-enroll *member_id* (the fire, the delete); True when a record went.

    With *generation*, only a record of THAT generation goes: a hire unwinding
    its own failed attempt must not remove the record of a concurrent hire
    that won the same id in another gateway (its row is the one on disk, its
    generation is its own store)."""
    with _locked():
        data = _read(strict=True)
        members = _crewmate_entries(data)
        record = members.get(member_id)
        if record is None:
            return False
        if generation is not None and not record_covers_store(record, generation):
            return False
        del members[member_id]
        if members:
            data[_CREWMATES_KEY] = {"members": members}
        else:
            data.pop(_CREWMATES_KEY, None)
        _write(data)
        return True


def rename_crewmate_record(old: str, new: str) -> bool:
    """Follow the member-id migration: a record keyed by a re-keyed row's old
    id moves to the minted id (idempotent; a record already under *new* wins)."""
    if not old or not new or old == new:
        return False
    with _locked():
        data = _read(strict=True)
        members = _crewmate_entries(data)
        if old not in members or new in members:
            return False
        members[new] = members.pop(old)
        data[_CREWMATES_KEY] = {"members": members}
        _write(data)
        return True


def stage_crewmate_generation(member_id: str, *, old: str, new: str) -> bool:
    """Phase one of following a crewmate's private store when the ROW's store
    legitimately changes through the API (the crew editor provisioning private
    memory for a member on the shared store, or rebinding it). Called BEFORE
    the config write: the record keeps ``generation == old`` and gains
    ``pending_generation == new``, so whichever store the row is on when the
    process dies -- the write never happened, or happened and phase two did
    not -- :func:`record_covers_store` still covers it and the crewmate stays
    on the roster. A pending generation an earlier move never finalized is
    finalized here first when it is the store the row is now on. ONLY a record
    that covers *old* is touched: a record of another generation belongs to a
    row that was deleted and recreated. True when staged."""
    if not member_id or not old or not new or old == new:
        return False
    with _locked():
        data = _read(strict=True)
        members = _crewmate_entries(data)
        record = members.get(member_id)
        if not isinstance(record, dict) or not record_covers_store(record, old):
            return False
        staged = dict(record)
        if staged.get("generation") != old:
            staged["generation"] = old  # the earlier move's write landed; own it
        staged["pending_generation"] = new
        members[member_id] = staged
        data[_CREWMATES_KEY] = {"members": members}
        _write(data)
        return True


def move_crewmate_generation(member_id: str, *, old: str, new: str) -> bool:
    """Phase two, after the config write landed: the record's generation
    becomes *new* and the pending mark goes. Accepts a record still at *old*
    and one whose pending generation is *new* (the staged form); anything
    else is another row's record and stays. True when it moved."""
    if not member_id or not old or not new or old == new:
        return False
    with _locked():
        data = _read(strict=True)
        members = _crewmate_entries(data)
        record = members.get(member_id)
        if not isinstance(record, dict):
            return False
        if record.get("generation") != old and record.get("pending_generation") != new:
            return False
        moved = {k: v for k, v in record.items() if k != "pending_generation"}
        moved["generation"] = new
        members[member_id] = moved
        data[_CREWMATES_KEY] = {"members": members}
        _write(data)
        return True


def unstage_crewmate_generation(member_id: str, *, new: str) -> bool:
    """Drop a staged move whose config write did not land (the row is still on
    its prior store, which ``generation`` still names). True when a pending
    mark of *new* went."""
    if not member_id or not new:
        return False
    with _locked():
        data = _read(strict=True)
        members = _crewmate_entries(data)
        record = members.get(member_id)
        if not isinstance(record, dict) or record.get("pending_generation") != new:
            return False
        members[member_id] = {k: v for k, v in record.items() if k != "pending_generation"}
        data[_CREWMATES_KEY] = {"members": members}
        _write(data)
        return True


def all_fork_info() -> dict[str, dict]:
    """Map of template name -> fork info for every recorded fork (one read).

    Bulk form for scans (``list_agents`` enriches every row); per-name callers
    use :func:`get_fork_info`.
    """
    with _lock:
        data = _read(strict=True)
    out: dict[str, dict] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        origin = entry.get(_FORKED_FROM)
        owner = entry.get(_PRIVATE_TO)
        if isinstance(origin, str) and origin and isinstance(owner, str) and owner:
            out[name] = {_FORKED_FROM: origin, _PRIVATE_TO: owner}
    return out


def prune(name: str) -> None:
    """Drop an agent's entry entirely (call when the agent is deleted)."""
    with _locked():
        data = _read(strict=True)
        if name in data:
            data.pop(name, None)
            _write(data)


def lift_and_strip_bookkeeping(config: MutableMapping[str, object], name: str) -> bool:
    """Lift ``model_managed`` / ``cc_model`` into the sidecar when unset; strip both.

    kiro-cli rejects unknown fields on the whole agent spec. Every writer that
    persists a kiro agent JSON must run this so those keys, owned only by
    Kiro Crew, never land on disk. When the sidecar already holds a value, a
    stale key in *config* is discarded rather than clobbering the
    authoritative sidecar (same rule as ``migrate_agent_specs`` /
    ``_refresh_dynamic_fields`` / the per-agent PATCH handler).

    A key is only LIFTED when it has the right type — ``bool`` for
    ``model_managed``, non-empty ``str`` for ``cc_model`` — mirroring the read
    guards in :func:`get_model_managed` / :func:`get_cc_model`. A hand-edited
    PUT body (the dashboard's free-text Agent Config textarea) can carry e.g.
    ``"model_managed": "false"``, and ``bool("false")`` is ``True``: silently
    coercing that would flip the flag's meaning rather than just ignore the
    bad value. The key is ALWAYS stripped from *config* regardless of type,
    since it must never reach the kiro spec either way.

    Returns True if either key was present on *config* (and removed).
    """
    changed = False
    if _MODEL_MANAGED in config:
        value = config[_MODEL_MANAGED]
        if isinstance(value, bool):
            # Hold the lock across the get-and-set so a concurrent writer
            # (e.g. an explicit-model PATCH racing a stale config PUT)
            # can't sneak a value in between the unset check and the lift —
            # the lifted stale value would clobber the fresher one.
            # ``_lock`` is an RLock, so the nested get/set acquisitions are
            # re-entrant and safe.
            with _lock:
                if get_model_managed(name) is None:
                    set_model_managed(name, value)
        else:
            logger.warning("Discarding non-bool model_managed=%r for agent %r", value, name)
        config.pop(_MODEL_MANAGED, None)
        changed = True
    if _CC_MODEL in config:
        value = config[_CC_MODEL]
        if isinstance(value, str):
            with _lock:
                if value and get_cc_model(name) is None:
                    set_cc_model(name, value)
        elif value:
            logger.warning("Discarding non-string cc_model=%r for agent %r", value, name)
        config.pop(_CC_MODEL, None)
        changed = True
    return changed
