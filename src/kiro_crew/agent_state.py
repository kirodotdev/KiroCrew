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
import stat
import threading
from pathlib import Path
from typing import Iterator, MutableMapping

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
