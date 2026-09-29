"""Operator approval of the exact launch a stubbed MCP server runs as.

A stubbed server's backend is spawned by gatewayd, outside the session sandbox,
as the user and with the daemon's environment. Which servers are stubbed is the
operator's decision, recorded in write-protected config. WHAT each of them runs
is not: the command, args and declared env come from agent-writable inputs
(``~/.kiro/crew/mcp.json``, the agent specs, the overlay and env sidecars the
rewriter produces). So the server NAME cannot be the approval -- an agent that
rewrites the spec behind an approved name would choose the program the gateway
execs outside its own sandbox. An absent or empty approval store approves
nothing; each non-managed launch requires an explicit operator decision.

This module binds the approval to CONTENT instead. For every approved server it
records the launch fingerprints the operator accepted -- ``hash_command`` over
the resolved command and args, and ``hash_declared_env`` over the declared env
-- in :data:`APPROVALS_LEAF` under the crew data home. That file is written only
by gateway-side code (the dashboard stub toggle and rewrite-pass refusal
recording), is write-protected from agent tools, and is read-only inside
the sandbox. Every launch that is about to leave the sandbox is
checked against it:

* the rewriter wraps a stubbed entry only when its fingerprint is approved, and
  otherwise leaves it unwrapped so the session launches it inside the sandbox;
* the gateway drops any target env entry whose command is not approved before
  handing the map to gatewayd;
* gatewayd re-checks the command at resolve time and the declared env before
  forwarding it, and fails closed on both.

An approval gets its fingerprints at the moment of the operator's decision.
Turning a stub on resolves the launch behind the name right then, through the
rewriter's own resolution (:mod:`kiro_crew.mcp_gateway.launch_resolve`), and
records exactly that launch -- so an agent that edits a spec after the click,
before the next gateway start rewrites the overlays, gets its launch refused.
A store that does not exist or is empty approves no launch. Existing stubs
require an operator approval through Settings → MCP Management before the daemon
may run their backends outside the sandbox.

Standard library only (plus the shared hashing leaf and ``config.paths``), for
the same reason as :mod:`kiro_crew.mcp_gateway.hashing`: the rewriter sits on
``config.loader``'s import path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping

from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_gateway.hashing import hash_command, hash_declared_env

logger = logging.getLogger(__name__)

#: Crew-home directory holding the approved launch fingerprints. The directory,
#: rather than its rename-published file, is the read-only sandbox seal.
APPROVALS_DIR = "mcp-launch-approvals"
APPROVALS_LEAF = f"{APPROVALS_DIR}/approvals.json"

#: Schema marker. A document without it approves no launch.
_VERSION = 1

#: Maximum records retained in either server population of one snapshot.
_MAX_SERVER_RECORDS = 512
#: Maximum total argv entries retained for operator display, including its marker.
_MAX_DISPLAY_ARGS = 32
#: Maximum characters retained in one display argv entry.
_MAX_DISPLAY_ARG_CHARS = 512
#: Maximum characters retained in one operator-facing server name. A server
#: whose stem is longer is not stored at all, so its launch stays unapproved.
_MAX_NAME_CHARS = 256
#: Maximum fingerprints, approved pairs or refused identities retained per server.
_MAX_SERVER_LAUNCHES = 64
#: Length of one ``<sha256 hex>:<sha256 hex>`` fingerprint or approval identity.
_MAX_IDENTITY_CHARS = 129
_TARGET_PREFIXES = ("KIROCREW_MCP_TARGET_", "MC_MCP_TARGET_")

_DISPLAY_ENV_VALUE_KEYS = frozenset(
    {
        "PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
    }
)
_DECLARED_ENV_REFERENCE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

#: Serializes read-modify-write of the store. Every writer (the dashboard
#: handlers, the gateway's post-rewrite save) runs in the gateway process.
_STORE_LOCK = threading.Lock()

#: Why a launch was refused, as surfaced to the dashboard.
REFUSED_UNAPPROVED = "added_outside_dashboard"
REFUSED_CHANGED = "changed_needs_reapproval"
REFUSED_TOO_MANY = "too_many_launches"
_REFUSAL_REASONS = frozenset({REFUSED_UNAPPROVED, REFUSED_CHANGED, REFUSED_TOO_MANY})


def approvals_path(home: str | os.PathLike[str] | None = None) -> Path:
    """Return ``<crew home>/`` :data:`APPROVALS_LEAF`, following ``KIROCREW_HOME``."""
    if home is not None:
        return Path(home) / APPROVALS_LEAF
    env_home = os.environ.get("KIROCREW_HOME")
    return (Path(env_home) if env_home else config_dir()) / APPROVALS_LEAF


def target_stem(name: str) -> str:
    """The normalized key gatewayd resolves a server by (``KIROCREW_MCP_TARGET_<stem>``)."""
    return name.upper().replace("-", "_")


def _coerced_env(env_pairs: Mapping[Any, Any] | None) -> dict[str, str]:
    """The declared env the way gatewayd reads it back from a sidecar."""
    return {str(k): str(v) for k, v in (env_pairs or {}).items() if k}


def env_fingerprint(env_pairs: Mapping[Any, Any] | None) -> str:
    """Hash of the complete declared env block gatewayd may apply.

    Secret-prefixed entries count too: private backends receive them.
    """
    return hash_declared_env(_coerced_env(env_pairs))


def launch_pair(command_hash: str, derived_env_hash: str) -> str:
    """Stable serialization of one approved command/environment pair."""
    return f"{command_hash}:{derived_env_hash}"


def serialize_identities(identities: Iterable[str]) -> str:
    """The deterministic ``expected_launch`` form of a set of approval identities."""
    return ",".join(sorted(set(identities)))


def launch_fingerprint(
    command: str, args: Iterable[str], env_pairs: Mapping[Any, Any] | None = None
) -> str:
    """``<command hash>:<env hash>`` for one launch."""
    return f"{hash_command(command, list(args))}:{env_fingerprint(env_pairs)}"


def _split(fingerprint: str) -> tuple[str, str]:
    cmd, _, env = fingerprint.partition(":")
    return cmd, env


@dataclass(frozen=True)
class ResolvedLaunch:
    """One launch a server name resolves to: its fingerprint, command and args.

    The env is part of the fingerprint but not carried here, so the value can be
    shown to the operator without echoing tokens. ``derived_env`` holds only the
    hashes of the env this server hands gatewayd once expanded, which the
    approval records so a backend spawned before the next rewrite still gets it.
    """

    fingerprint: str
    command: str
    args: tuple[str, ...]
    derived_env: frozenset[str] = frozenset()
    declared_env: tuple[tuple[str, str], ...] = ()

    @property
    def approval_identities(self) -> frozenset[str]:
        """The exact command/effective-environment pairs this launch may approve."""
        command_hash, _declared_env_hash = _split(self.fingerprint)
        return frozenset(launch_pair(command_hash, env_hash) for env_hash in self.derived_env)


@dataclass
class LaunchApprovals:
    """One snapshot of the store, plus what a rewrite pass did with it."""

    #: stem -> approved launch fingerprints.
    approved: dict[str, set[str]] = field(default_factory=dict)
    #: stem -> approved ``(command_hash, derived_env_hash)`` pairs.
    approved_pairs: dict[str, set[str]] = field(default_factory=dict)
    #: stem -> display name, for the audit trail and the dashboard.
    names: dict[str, str] = field(default_factory=dict)
    #: stem -> approved pair -> redacted, bounded ``(command, env)`` display of the
    #: launch the operator approved. Display only: never consulted to admit a launch.
    approved_displays: dict[str, dict[str, tuple[list[str], list[str]]]] = field(
        default_factory=dict
    )
    #: A resolution pass (:func:`kiro_crew.mcp_gateway.launch_resolve.resolve_launches`):
    #: every launch is admitted and recorded so the caller can read what each
    #: name resolves to. Never persisted.
    probe: bool = False
    #: Fingerprints recorded during this pass (stem -> set).
    captured: dict[str, set[str]] = field(default_factory=dict)
    #: Exact command/environment pairs recorded during this pass.
    captured_pairs: dict[str, set[str]] = field(default_factory=dict)
    #: The launches behind :attr:`captured` (stem -> fingerprint -> launch).
    captured_launches: dict[str, dict[str, ResolvedLaunch]] = field(default_factory=dict)
    #: Launches refused during this pass (stem -> reason).
    refused: dict[str, str] = field(default_factory=dict)
    #: Pairs this pass admitted because the operator approved the declared
    #: launch and only its ``${VAR}`` expansion differs (stem -> pair ->
    #: the approved fingerprint it was admitted under).
    rebound: dict[str, dict[str, str]] = field(default_factory=dict)
    #: The approved pairs a stem held before this pass rebound any of them, so
    #: a save can tell whether the store moved underneath the pass.
    rebind_base: dict[str, frozenset[str]] = field(default_factory=dict)
    #: Additional redacted display argv found by the final target filter.
    refused_commands: dict[str, list[list[str]]] = field(default_factory=dict)
    #: Every approval identity and its redacted command/env displays seen this pass.
    #: ``None`` keeps an identity whose launch content cannot safely be displayed.
    launch_identities: dict[str, dict[str, tuple[list[str] | None, list[str] | None]]] = field(
        default_factory=dict
    )
    #: Identities whose command or environment display was bounded. They remain
    #: refused but cannot be approved from an incomplete operator display.
    incomplete_identities: dict[str, set[str]] = field(default_factory=dict)
    #: Refusals carried from the stored document, replaced by a full pass.
    stored_refused: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Set by a full rewrite pass; a cache-served pass keeps the stored
    #: refusals and derived env.
    full_pass: bool = False
    #: Set when a pass kept at least one agent's previous overlay without
    #: admitting the launches behind it, so :attr:`launch_identities` is not
    #: the full set of launches live after this pass. Retirement of a stale
    #: pair needs the full set, so it waits for a pass that has it.
    live_incomplete: bool = False
    #: Set when an environment sidecar failed to publish during this pass.
    #: Rebound pairs depend on that content, so a save withholds all of them.
    rebind_incomplete: bool = False
    #: Whether this snapshot already reported reaching ``_MAX_SERVER_RECORDS``.
    _record_cap_reported: bool = field(default=False, repr=False, compare=False)

    def _retained_stems(self) -> set[str]:
        return set().union(
            self.approved,
            self.approved_pairs,
            self.names,
            self.captured,
            self.captured_launches,
            self.refused,
            self.refused_commands,
            self.launch_identities,
            self.incomplete_identities,
            self.stored_refused,
        )

    def _stem_admitted(self, stem: str, name: str) -> bool:
        """Whether *stem* may hold a row anywhere in this snapshot."""
        display_name = _bounded_name(name)
        if len(stem) > _MAX_NAME_CHARS:
            logger.warning(
                "mcp launch approvals: refusing %s; server name exceeds %d characters",
                display_name,
                _MAX_NAME_CHARS,
            )
            return False
        retained_stems = self._retained_stems()
        if stem not in retained_stems and len(retained_stems) >= _MAX_SERVER_RECORDS:
            if not self._record_cap_reported:
                self._record_cap_reported = True
                logger.warning(
                    "mcp launch approvals: server record cap _MAX_SERVER_RECORDS=%d "
                    "reached; refusing %s and every further new server",
                    _MAX_SERVER_RECORDS,
                    display_name,
                )
            return False
        return True

    def over_cap(self, stem: str) -> bool:
        """Whether *stem* was refused, or would be, by a record or launch cap."""
        if self.refused.get(stem) == REFUSED_TOO_MANY:
            return True
        retained_stems = self._retained_stems()
        return stem not in retained_stems and len(retained_stems) >= _MAX_SERVER_RECORDS

    def _admission_allowed(
        self,
        stem: str,
        name: str,
        additions: Iterable[tuple[Mapping[str, Collection[Any]], Collection[Any]]],
        *,
        replace: bool = False,
    ) -> bool:
        """Refuse a stem or launch before any structure retains its content."""
        if not self._stem_admitted(stem, name):
            return False
        display_name = _bounded_name(name)
        for population, candidates in additions:
            retained = [] if replace else list(population.get(stem, ()))
            for candidate in candidates:
                if candidate in retained:
                    continue
                retained.append(candidate)
                if len(retained) <= _MAX_SERVER_LAUNCHES:
                    continue
                if self.refused.get(stem) != REFUSED_TOO_MANY:
                    logger.warning(
                        "mcp launch approvals: refusing %s; per-server launch cap %d reached",
                        display_name,
                        _MAX_SERVER_LAUNCHES,
                    )
                self.refused[stem] = REFUSED_TOO_MANY
                return False
        return True

    def _record(
        self,
        stem: str,
        name: str,
        fingerprint: str,
        derived_env_hash: str,
        launch: tuple[str, list[str]] | None,
        env: Mapping[str, Any] | None,
    ) -> None:
        command_hash, _declared_env_hash = _split(fingerprint)
        pair = launch_pair(command_hash, derived_env_hash)
        self.approved.setdefault(stem, set()).add(fingerprint)
        self.approved_pairs.setdefault(stem, set()).add(pair)
        self.captured.setdefault(stem, set()).add(fingerprint)
        self.captured_pairs.setdefault(stem, set()).add(pair)
        self.names.setdefault(stem, _bounded_name(name))
        if launch is not None:
            self.captured_launches.setdefault(stem, {})[fingerprint] = ResolvedLaunch(
                fingerprint,
                str(launch[0]),
                tuple(str(a) for a in launch[1]),
                frozenset({derived_env_hash}),
                tuple(sorted(_coerced_env(env).items())),
            )

    def admit(
        self,
        name: str,
        fingerprint: str,
        *,
        managed: bool = False,
        launch: tuple[str, list[str]] | None = None,
        env: Mapping[str, Any] | None = None,
        derived_env_hash: str,
    ) -> bool:
        """Whether *name* may launch *fingerprint* outside the sandbox.

        ``managed`` marks a launch the code derived itself (a reserved
        ``kirocrew-*`` server repaired to the managed invocation): it is
        recorded without needing an operator approval, because nothing
        agent-writable chose it. ``launch`` is the ``(command, args)`` behind
        the fingerprint, kept for a probe pass to report.
        """
        stem = target_stem(name)
        command_hash, _declared_env_hash = _split(fingerprint)
        identity = launch_pair(command_hash, derived_env_hash)
        # The operator approved this exact declared launch -- resolved command,
        # args and the env text with its ``${VAR}`` references unexpanded -- and
        # only the expansion moved. Those values come from the gateway process
        # environment (the operator's shell and the crew ``.env``), which no
        # agent can write, so the new expansion is the approved launch.
        rebind = (
            not self.probe
            and not managed
            and fingerprint in self.approved.get(stem, ())
            and not self.admits_launch(stem, command_hash, derived_env_hash)
        )
        additions: list[tuple[Mapping[str, Collection[Any]], Collection[Any]]] = []
        if not self.probe:
            additions.append((self.launch_identities, (identity,)))
        if self.probe or managed or rebind:
            additions.extend(
                (
                    (self.approved, (fingerprint,)),
                    (self.approved_pairs, (identity,)),
                    (self.captured, (fingerprint,)),
                )
            )
            if launch is not None:
                additions.append((self.captured_launches, (fingerprint,)))
        if not self._admission_allowed(stem, name, additions):
            return False
        if self.probe:
            self._record(stem, name, fingerprint, derived_env_hash, launch, env)
            return True
        identities = self.launch_identities.setdefault(stem, {})
        command_display: list[str] | None = None
        env_display: list[str] | None = None
        display_complete = False
        if launch is not None:
            command_display, command_complete = _bounded_display_argv_with_completeness(launch)
            if env is not None:
                env_display, env_complete = _bounded_display_env_with_completeness(env)
                display_complete = command_complete and env_complete
        if not display_complete:
            self.incomplete_identities.setdefault(stem, set()).add(identity)
        prior_command, prior_env = identities.get(identity, (None, None))
        identities[identity] = (
            command_display if command_display is not None else prior_command,
            env_display if env_display is not None else prior_env,
        )
        if fingerprint in self.approved.get(stem, ()) and self.admits_launch(
            stem, command_hash, derived_env_hash
        ):
            return True
        if managed:
            self._record(stem, name, fingerprint, derived_env_hash, launch, env)
            return True
        if rebind:
            self.rebind_base.setdefault(stem, frozenset(self.approved_pairs.get(stem, ())))
            self._record(stem, name, fingerprint, derived_env_hash, launch, env)
            self.rebound.setdefault(stem, {})[identity] = fingerprint
            if display_complete and command_display is not None and env_display is not None:
                self.approved_displays.setdefault(stem, {})[identity] = (
                    command_display,
                    env_display,
                )
            logger.info(
                "mcp launch approvals: %s keeps its approval; only a ${VAR} value "
                "in its declared env changed",
                _bounded_name(name),
            )
            return True
        self.names.setdefault(stem, _bounded_name(name))
        self.refused[stem] = REFUSED_CHANGED if self.approved.get(stem) else REFUSED_UNAPPROVED
        return False

    def refusal_displays(self, stem: str) -> tuple[list[list[str]], list[list[str]]]:
        """Paired command/env displays behind a refusal, in stable identity order."""
        commands: list[list[str]] = []
        envs: list[list[str]] = []
        for _identity, (command, env) in sorted(self.launch_identities.get(stem, {}).items()):
            if command is not None and env is not None:
                commands.append(list(command))
                envs.append(list(env))
        return commands, envs

    def refused_identities(self, stem: str) -> set[str] | None:
        """Every fully displayed approval identity behind a refusal, or ``None``."""
        identities = self.launch_identities.get(stem, {})
        if (
            not identities
            or self.refused_commands.get(stem)
            or self.incomplete_identities.get(stem)
            or any(command is None or env is None for command, env in identities.values())
        ):
            return None
        commands, envs = self.refusal_displays(stem)
        if len(commands) != len(identities) or len(envs) != len(identities):
            return None
        return set(identities)

    def admits_launch(self, stem: str, command_hash: str, derived_env_hash: str) -> bool:
        """Whether one exact command/effective-environment pair was approved."""
        return launch_pair(command_hash, derived_env_hash) in self.approved_pairs.get(stem, ())

    def admits_command(self, stem: str, command_hash: str) -> bool:
        """Whether *command_hash* participates in an approved pair."""
        prefix = f"{command_hash}:"
        return any(pair.startswith(prefix) for pair in self.approved_pairs.get(stem, ()))

    def digest(self) -> list[Any]:
        """Stable summary folded into the rewriter's input fingerprint.

        JSON-native (lists, not tuples): the rewriter compares it with the copy
        it reloads from the stored fingerprint, and a tuple never equals the
        list that copy decodes to.
        """
        return [
            [stem, sorted(fps), sorted(self.approved_pairs.get(stem, ()))]
            for stem, fps in sorted(self.approved.items())
        ]


def _bounded_name(value: Any) -> str:
    """Return one bounded operator-facing server name."""
    text = str(value)
    if len(text) <= _MAX_NAME_CHARS:
        return text
    return text[: _MAX_NAME_CHARS - 1] + "…"


def _bounded_display_argv_with_completeness(
    launch: tuple[Any, Iterable[Any]],
) -> tuple[list[str], bool]:
    """Redact and bound one argv, returning whether the display is complete."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    raw = [launch[0], *launch[1]]
    safe: list[str] = []
    cut = False
    for value in raw[:_MAX_DISPLAY_ARGS]:
        text, _ = redact_exfiltration_urls(str(value))
        text, _ = redact_credentials(text)
        if len(text) > _MAX_DISPLAY_ARG_CHARS:
            text = text[: _MAX_DISPLAY_ARG_CHARS - 1] + "…"
            cut = True
        safe.append(text)
    dropped = max(0, len(raw) - _MAX_DISPLAY_ARGS)
    partial = cut or bool(dropped)
    if partial:
        if len(safe) >= _MAX_DISPLAY_ARGS:
            safe.pop()
            dropped += 1
        marker = "…(partial)" if not dropped else f"…(+{dropped} args; partial)"
        safe.append(marker[:_MAX_DISPLAY_ARG_CHARS])
    return safe, not partial


def _bounded_display_argv(launch: tuple[Any, Iterable[Any]]) -> list[str]:
    """Return the bounded display portion of one argv."""
    return _bounded_display_argv_with_completeness(launch)[0]


def _display_env_pair(key: str, value: str) -> str:
    """Return one display-safe declared environment pair."""
    visible = (
        key in _DISPLAY_ENV_VALUE_KEYS
        or key.startswith(("LD_", "DYLD_"))
        or _DECLARED_ENV_REFERENCE.fullmatch(value) is not None
    )
    if visible:
        return f"{key}={value}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{key}=<hidden sha256:{digest}>"


def _bounded_display_env_with_completeness(
    env: Mapping[str, Any],
) -> tuple[list[str], bool]:
    """Redact and bound a declared environment in stable key order."""
    pairs = [
        _display_env_pair(str(key), str(value))
        for key, value in sorted(env.items(), key=lambda item: str(item[0]))
    ]
    if not pairs:
        return [], True
    return _bounded_display_argv_with_completeness((pairs[0], pairs[1:]))


def _bounded_display_env(env: Mapping[str, Any]) -> list[str]:
    """Return the bounded display portion of a declared environment."""
    return _bounded_display_env_with_completeness(env)[0]


def display_launches(name: str, launches: Iterable[ResolvedLaunch]) -> dict[str, Any]:
    """Build the display and compare-and-set token for one resolved server."""
    commands: list[list[str]] = []
    envs: list[list[str]] = []
    identities: set[str] = set()
    complete = True
    for launch in launches:
        command, command_complete = _bounded_display_argv_with_completeness(
            (launch.command, launch.args)
        )
        env, env_complete = _bounded_display_env_with_completeness(dict(launch.declared_env))
        launch_identities = launch.approval_identities
        if not launch_identities:
            complete = False
            continue
        complete = complete and command_complete and env_complete
        for identity in sorted(launch_identities):
            identities.add(identity)
            commands.append(command)
            envs.append(env)
    complete = complete and bool(identities) and len(commands) == len(identities)
    return {
        "name": _bounded_name(name),
        "commands": commands,
        "envs": envs,
        "complete": complete,
        **({"expected_launch": serialize_identities(identities)} if complete else {}),
    }


def _bounded_identities(values: Any) -> tuple[list[str], bool]:
    """Return the bounded subset of stored identities and whether any were cut."""
    if not isinstance(values, list):
        return [], False
    wellformed = sorted(
        {v for v in values if isinstance(v, str) and 0 < len(v) <= _MAX_IDENTITY_CHARS}
    )
    return wellformed[:_MAX_SERVER_LAUNCHES], len(wellformed) > _MAX_SERVER_LAUNCHES


def _bounded_commands(values: Any) -> tuple[list[list[str]], bool, bool]:
    """Bound stored command displays and report cuts or malformed entries."""
    if not isinstance(values, list):
        return [], False, values is not None
    commands: list[list[str]] = []
    malformed = False
    partial = False
    for value in values:
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(part, str) for part in value)
        ):
            malformed = True
            continue
        display, complete = _bounded_display_argv_with_completeness((value[0], value[1:]))
        commands.append(display)
        partial = partial or not complete
    return (
        commands[:_MAX_SERVER_LAUNCHES],
        partial or len(commands) > _MAX_SERVER_LAUNCHES,
        malformed,
    )


def _bounded_envs(values: Any) -> tuple[list[list[str]], bool, bool]:
    """Bound stored env displays and report cuts or malformed entries."""
    if not isinstance(values, list):
        return [], False, values is not None
    envs: list[list[str]] = []
    malformed = False
    partial = False
    for value in values:
        if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
            malformed = True
            continue
        if value:
            display, complete = _bounded_display_argv_with_completeness((value[0], value[1:]))
            envs.append(display)
            partial = partial or not complete
        else:
            envs.append([])
    return envs[:_MAX_SERVER_LAUNCHES], partial or len(envs) > _MAX_SERVER_LAUNCHES, malformed


def _stored_expected_launch(value: Any) -> str | None:
    """A stored ``expected_launch`` that is a well-formed bounded identity set, else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(",")
    if len(parts) > _MAX_SERVER_LAUNCHES or not all(
        0 < len(part) <= _MAX_IDENTITY_CHARS for part in parts
    ):
        return None
    return value


def _bounded_display_line(value: Any, *, allow_empty: bool) -> list[str] | None:
    """Re-bound one stored display line, or ``None`` when it is malformed."""
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        return None
    if not value:
        return [] if allow_empty else None
    return _bounded_display_argv((value[0], value[1:]))


def _parse_approved_displays(value: Any, pairs: set[str]) -> dict[str, tuple[list[str], list[str]]]:
    """Stored approved-launch displays for approved *pairs*, bounded like refusals."""
    if not isinstance(value, dict):
        return {}
    displays: dict[str, tuple[list[str], list[str]]] = {}
    for pair in sorted(pairs)[:_MAX_SERVER_LAUNCHES]:
        entry = value.get(pair)
        if not isinstance(entry, dict):
            continue
        command = _bounded_display_line(entry.get("command"), allow_empty=False)
        env = _bounded_display_line(entry.get("env"), allow_empty=True)
        if command is not None and env is not None:
            displays[pair] = (command, env)
    return displays


def _parse(raw: Any) -> LaunchApprovals:
    if not isinstance(raw, dict) or raw.get("version") != _VERSION:
        return LaunchApprovals()
    approvals = LaunchApprovals()
    servers = raw.get("servers")
    if isinstance(servers, dict):
        valid_servers = sorted(
            (stem, record)
            for stem, record in servers.items()
            if isinstance(stem, str) and len(stem) <= _MAX_NAME_CHARS and isinstance(record, dict)
        )
        for stem, record in valid_servers[:_MAX_SERVER_RECORDS]:
            fps, _cut_fps = _bounded_identities(record.get("fingerprints"))
            pairs, _cut_pairs = _bounded_identities(record.get("pairs"))
            if isinstance(record.get("fingerprints"), list):
                approvals.approved[stem] = set(fps)
            if isinstance(record.get("pairs"), list):
                approvals.approved_pairs[stem] = set(pairs)
            displays = _parse_approved_displays(record.get("displays"), set(pairs))
            if displays:
                approvals.approved_displays[stem] = displays
            name = record.get("name")
            approvals.names[stem] = _bounded_name(name if isinstance(name, str) else stem)
    refused = raw.get("refused")
    if isinstance(refused, dict):
        valid_refused = sorted(
            (stem, rec)
            for stem, rec in refused.items()
            if isinstance(stem, str) and len(stem) <= _MAX_NAME_CHARS and isinstance(rec, dict)
        )
        for stem, rec in valid_refused[:_MAX_SERVER_RECORDS]:
            commands, cut_commands, malformed_commands = _bounded_commands(rec.get("commands"))
            envs, cut_envs, malformed_envs = _bounded_envs(rec.get("envs"))
            displays_invalid = malformed_commands or malformed_envs or len(commands) != len(envs)
            if displays_invalid:
                # Never shift one side of a command/env pair after malformed input.
                commands, envs = [], []
            complete = rec.get("complete") is not False and not (
                cut_commands or cut_envs or displays_invalid
            )
            expected_launch = _stored_expected_launch(rec.get("expected_launch"))
            if expected_launch and (
                not complete or len(commands) != len(expected_launch.split(","))
            ):
                expected_launch = None
            approved_commands, _, malformed_approved_commands = _bounded_commands(
                rec.get("approved_commands")
            )
            approved_envs, _, malformed_approved_envs = _bounded_envs(rec.get("approved_envs"))
            approved_invalid = (
                malformed_approved_commands
                or malformed_approved_envs
                or len(approved_commands) != len(approved_envs)
            )
            if approved_invalid:
                approved_commands, approved_envs = [], []
            reason = rec.get("reason")
            approvals.stored_refused[stem] = {
                "name": _bounded_name(rec.get("name", stem)),
                "reason": reason if reason in _REFUSAL_REASONS else "",
                "commands": commands,
                "envs": envs,
                **({"complete": False} if not complete else {}),
                **({"expected_launch": expected_launch} if expected_launch else {}),
                **({"approved_commands": approved_commands} if approved_commands else {}),
                **({"approved_envs": approved_envs} if approved_envs else {}),
            }
    return approvals


def load_approvals(path: Path | None = None) -> LaunchApprovals:
    """Read the store. Absent, empty, unreadable, or malformed approves nothing."""
    target = path or approvals_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LaunchApprovals()
    except OSError:
        logger.warning("mcp launch approvals: store unreadable; approving no launch")
        return LaunchApprovals()
    if not text.strip():
        return LaunchApprovals()
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("mcp launch approvals: store is not valid JSON; approving no launch")
        return LaunchApprovals()
    return _parse(raw)


def _document(approvals: LaunchApprovals, refused: Mapping[str, Mapping[str, Any]]) -> str:
    server_records = [
        (stem, fps)
        for stem, fps in sorted(approvals.approved.items())
        if fps and len(stem) <= _MAX_NAME_CHARS
    ]
    servers: dict[str, dict[str, Any]] = {}
    for stem, fps in server_records:
        pairs = approvals.approved_pairs.get(stem, set())
        servers[stem] = {
            "name": _bounded_name(approvals.names.get(stem, stem)),
            "fingerprints": sorted(fps),
            "pairs": sorted(pairs),
        }
        displays = {
            pair: {"command": list(command), "env": list(env)}
            for pair, (command, env) in sorted(approvals.approved_displays.get(stem, {}).items())
            if pair in pairs
        }
        if displays:
            servers[stem]["displays"] = dict(list(displays.items())[:_MAX_SERVER_LAUNCHES])
    kept_refused: dict[str, dict[str, Any]] = {}
    eligible_refused = [
        (stem, record) for stem, record in sorted(refused.items()) if len(stem) <= _MAX_NAME_CHARS
    ]
    for stem, raw_record in eligible_refused:
        commands = [list(command) for command in raw_record.get("commands", [])]
        envs = [list(env) for env in raw_record.get("envs", [])]
        displays_invalid = len(commands) != len(envs)
        if displays_invalid:
            # A partial pair cannot truthfully describe launch content.
            commands, envs = [], []
        record: dict[str, Any] = {
            "name": _bounded_name(raw_record.get("name", stem)),
            "reason": raw_record.get("reason", ""),
            "commands": commands,
            "envs": envs,
        }
        if raw_record.get("complete") is False:
            record["complete"] = False
        approved_commands = [list(command) for command in raw_record.get("approved_commands", [])]
        approved_envs = [list(env) for env in raw_record.get("approved_envs", [])]
        if approved_commands and len(approved_commands) == len(approved_envs):
            record["approved_commands"] = approved_commands
            record["approved_envs"] = approved_envs
        expected_launch = _stored_expected_launch(raw_record.get("expected_launch"))
        if (
            expected_launch
            and raw_record.get("complete") is not False
            and not displays_invalid
            and len(commands) == len(expected_launch.split(","))
        ):
            record["expected_launch"] = expected_launch
        kept_refused[stem] = record
    doc = {"version": _VERSION, "servers": servers, "refused": kept_refused}
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _write(path: Path, text: str) -> None:
    # Deferred: ``atomic_write`` pulls in platform helpers this leaf does not
    # need at import time.
    from kiro_crew.atomic_write import atomic_write

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, text, mode=0o600)


def _approved_displays(
    approvals: LaunchApprovals, stem: str
) -> tuple[list[list[str]], list[list[str]]]:
    """Readable displays of the approved launches, or empty when any is unknown.

    An approval recorded without a display yields nothing rather than a partial
    or hash-only view, so an old-versus-new comparison is never misleading.
    """
    pairs = sorted(approvals.approved_pairs.get(stem, ()))
    displays = approvals.approved_displays.get(stem, {})
    if not pairs or any(pair not in displays for pair in pairs):
        return [], []
    return (
        [list(displays[pair][0]) for pair in pairs],
        [list(displays[pair][1]) for pair in pairs],
    )


def _refusal_record(approvals: LaunchApprovals, stem: str, reason: str) -> dict[str, Any]:
    """Stored refusal paired with the identity of its displayed launch content."""
    all_commands, all_envs = approvals.refusal_displays(stem)
    record: dict[str, Any] = {
        "name": _bounded_name(approvals.names.get(stem, stem)),
        "reason": reason,
        "commands": all_commands,
        "envs": all_envs,
    }
    identities = approvals.refused_identities(stem)
    complete = bool(
        identities and len(all_commands) == len(identities) and len(all_envs) == len(identities)
    )
    if complete and identities is not None:
        record["expected_launch"] = serialize_identities(identities)
    else:
        record["complete"] = False
    if reason == REFUSED_CHANGED:
        approved_commands, approved_envs = _approved_displays(approvals, stem)
        if approved_commands:
            record["approved_commands"] = approved_commands
            record["approved_envs"] = approved_envs
    return record


def _pairs_under(pairs: Iterable[str], command_hash: str) -> set[str]:
    return {pair for pair in pairs if _split(pair)[0] == command_hash}


#: Audit reason for a pair admitted because only a ``${VAR}`` value changed.
REBIND_AUDIT_REASON = "approved launch, ${VAR} value changed"


def _settle_rebinds(
    approvals: LaunchApprovals, stem: str, current_pairs: set[str], before: set[str]
) -> tuple[set[str], set[str], set[str]]:
    """Which rebound pairs a save may write, retire, or leave untouched.

    A rebind is written only while the store under its command hash is exactly
    what the pass started from: an operator approval or another pass that
    landed meanwhile is newer, and wins. A rebind whose fingerprint is absent
    from the current store is dropped too. Where it is written, the pairs
    under that command hash become the ones this full pass saw live, so a
    ``${VAR}`` that keeps changing replaces its older expansions instead of
    filling the per-server launch cap, whichever agents declare the launch.

    Retiring what the pass did not see needs the pass to have seen everything:
    a pass that kept an agent's overlay without reading its source
    (``live_incomplete``) never admitted that agent's launches, so its own
    still-approved pair is absent from ``launch_identities``. Such a pass
    writes the rebind and retires nothing; the stale expansions wait for a
    pass that admitted every live launch. A pass whose environment sidecar
    publication failed writes and retires no rebound pair.
    """
    if approvals.rebind_incomplete:
        return set(), set(), set()
    base = approvals.rebind_base.get(stem, frozenset())
    live = set(approvals.launch_identities.get(stem, {}))
    by_command: dict[str, set[str]] = {}
    for pair, fingerprint in approvals.rebound.get(stem, {}).items():
        if fingerprint in before:
            by_command.setdefault(_split(pair)[0], set()).add(pair)
    written: set[str] = set()
    retired: set[str] = set()
    untouched: set[str] = set()
    for command_hash, pairs in by_command.items():
        stored = _pairs_under(current_pairs, command_hash)
        if stored != _pairs_under(base, command_hash):
            untouched.add(command_hash)
            continue
        written |= pairs
        if approvals.full_pass and not approvals.live_incomplete:
            retired |= stored - (live & (stored | pairs))
    return written, retired, untouched


def save_pass(approvals: LaunchApprovals, path: Path | None = None) -> bool:
    """Persist what a full rewrite pass captured and refused. True if written.

    Merged into the CURRENT store under the lock rather than overwriting it
    with the pass's snapshot, so a toggle that landed while the pass ran is
    not lost. A probe pass is never persisted.
    """
    if approvals.probe:
        raise ValueError("a launch-resolution probe is not an approval store")
    target = path or approvals_path()
    with _STORE_LOCK:
        current = load_approvals(target)
        changed = False
        for stem, fps in approvals.captured.items():
            before = set(current.approved.get(stem, ()))
            before_pairs = set(current.approved_pairs.get(stem, ()))
            written, retired, untouched = _settle_rebinds(approvals, stem, before_pairs, before)
            dropped = set(approvals.rebound.get(stem, {})) - written
            withdrawn = {
                approvals.rebound[stem][pair]
                for pair in dropped
                if approvals.rebound[stem][pair] not in before
            }
            fps = {fp for fp in fps if fp not in withdrawn}
            if not fps:
                continue
            captured_pairs = {
                pair
                for pair in approvals.captured_pairs.get(stem, ())
                if (
                    any(pair.startswith(f"{_split(fp)[0]}:") for fp in fps)
                    and _split(pair)[0] not in untouched
                    and pair not in dropped
                )
            }
            name = approvals.names.get(stem, stem)
            final_fps = before | fps
            final_pairs = (before_pairs | captured_pairs) - retired
            if not current._admission_allowed(
                stem,
                name,
                ((current.approved, final_fps), (current.approved_pairs, final_pairs)),
                replace=True,
            ):
                if current.refused.get(stem) == REFUSED_TOO_MANY:
                    approvals.refused[stem] = REFUSED_TOO_MANY
                continue
            current.approved[stem] = final_fps
            current.approved_pairs[stem] = final_pairs
            current.names.setdefault(stem, _bounded_name(name))
            pass_displays = approvals.approved_displays.get(stem, {})
            new_displays = {
                pair: pass_displays[pair]
                for pair in captured_pairs
                if pair in pass_displays and pair not in current.approved_displays.get(stem, {})
            }
            if new_displays:
                current.approved_displays.setdefault(stem, {}).update(new_displays)
            changed = (
                changed or final_fps != before or final_pairs != before_pairs or bool(new_displays)
            )

        def _approved_since(stem: str) -> bool:
            identities = approvals.refused_identities(stem)
            if not identities:
                return False
            return identities <= current.approved_pairs.get(stem, set())

        original_refused = dict(current.stored_refused)
        if approvals.full_pass:
            candidates = {
                stem: _refusal_record(approvals, stem, reason)
                for stem, reason in approvals.refused.items()
                if reason == REFUSED_TOO_MANY or not _approved_since(stem)
            }
        else:
            candidates = dict(original_refused)
            for stem, reason in approvals.refused.items():
                if reason != REFUSED_TOO_MANY and _approved_since(stem):
                    continue
                candidates[stem] = _refusal_record(approvals, stem, reason)
        # Re-admit every refusal against the merged store, so the record cap
        # bounds what is written rather than only what one pass saw.
        current.stored_refused = {}
        for stem, record in candidates.items():
            if current._stem_admitted(stem, str(record.get("name", stem))):
                current.stored_refused[stem] = record
        refused = current.stored_refused
        if refused != original_refused:
            changed = True
        if not changed:
            return False
        _write(target, _document(current, refused))
    for reason, fingerprints in _captured_by_reason(approvals).items():
        logger.warning(
            "mcp launch approvals: recorded %d launch fingerprint(s) for %s (%s)",
            sum(len(v) for v in fingerprints.values()),
            ", ".join(sorted(approvals.names.get(s, s) for s in fingerprints)),
            reason,
        )
    return True


def _captured_by_reason(approvals: LaunchApprovals) -> dict[str, dict[str, set[str]]]:
    """Split what a pass captured by why it was admitted, for the audit line.

    A rebound fingerprint is an operator-approved declaration whose ``${VAR}``
    expansion changed; every other captured fingerprint is a managed launch.
    """
    by_reason: dict[str, dict[str, set[str]]] = {}
    for stem, fps in approvals.captured.items():
        rebound = set(approvals.rebound.get(stem, {}).values())
        for fingerprint in fps:
            reason = REBIND_AUDIT_REASON if fingerprint in rebound else "managed launch"
            by_reason.setdefault(reason, {}).setdefault(stem, set()).add(fingerprint)
    return by_reason


def approve(launches: Mapping[str, Iterable[ResolvedLaunch]], path: Path | None = None) -> None:
    """Record exactly *launches* as the approved launches of their server names.

    *launches* maps a server name to the launches it resolved to when the
    operator decided (see :func:`kiro_crew.mcp_gateway.launch_resolve.resolve_launches`).
    Replaces every fingerprint already approved for those names, so turning a
    stub on approves what it runs NOW rather than keeping an older launch alive
    beside it. The env the daemon may forward is the env those launches
    derived when they were resolved, so a backend the running daemon spawns
    before the next rewrite still gets its declared env; that rewrite derives
    it again. Gateway-side writer only: the dashboard stub toggle.
    """
    target = path or approvals_path()
    decided: dict[str, tuple[str, set[str], set[str]]] = {}
    with _STORE_LOCK:
        current = load_approvals(target)
        for name, items in launches.items():
            if not isinstance(name, str) or not name:
                continue
            fps: set[str] = set()
            pairs: set[str] = set()
            displays: dict[str, tuple[list[str], list[str]]] = {}
            for launch in items:
                fps.add(launch.fingerprint)
                command_hash, _declared_env_hash = _split(launch.fingerprint)
                launch_pairs = {
                    launch_pair(command_hash, env_hash) for env_hash in launch.derived_env
                }
                pairs.update(launch_pairs)
                display = (
                    _bounded_display_argv((launch.command, launch.args)),
                    _bounded_display_env(dict(launch.declared_env)),
                )
                displays.update({pair: display for pair in launch_pairs})
                if len(fps) > _MAX_SERVER_LAUNCHES or len(pairs) > _MAX_SERVER_LAUNCHES:
                    break
            if not fps:
                continue
            stem = target_stem(name)
            if not current._admission_allowed(
                stem,
                name,
                ((current.approved, fps), (current.approved_pairs, pairs)),
                replace=True,
            ):
                continue
            current.approved[stem] = set(fps)
            current.approved_pairs[stem] = set(pairs)
            current.approved_displays[stem] = displays
            current.names[stem] = _bounded_name(name)
            decided[stem] = (name, fps, pairs)
        refused = {s: r for s, r in current.stored_refused.items() if s not in decided}
        for stem, reason in current.refused.items():
            refused[stem] = _refusal_record(current, stem, reason)
        if decided or refused != current.stored_refused:
            _write(target, _document(current, refused))
    if decided:
        logger.warning(
            "mcp launch approvals: operator approved %d launch fingerprint(s) for %s",
            sum(len(rec[1]) for rec in decided.values()),
            ", ".join(sorted(rec[0] for rec in decided.values())),
        )


@dataclass
class ApprovalSnapshot:
    """The affected approval rows captured immediately before a revoke."""

    stems: set[str]
    approved: dict[str, set[str]]
    approved_pairs: dict[str, set[str]]
    names: dict[str, str]
    refused: dict[str, dict[str, Any]]
    displays: dict[str, dict[str, tuple[list[str], list[str]]]] = field(default_factory=dict)


def revoke(names: Iterable[str], path: Path | None = None) -> ApprovalSnapshot:
    """Forget approvals for *names* and return their immediately prior rows."""
    stems = {target_stem(n) for n in names if isinstance(n, str) and n}
    snapshot = ApprovalSnapshot(stems, {}, {}, {}, {})
    if not stems:
        return snapshot
    target = path or approvals_path()
    with _STORE_LOCK:
        current = load_approvals(target)
        snapshot.approved = {
            stem: set(current.approved[stem]) for stem in stems if stem in current.approved
        }
        snapshot.approved_pairs = {
            stem: set(current.approved_pairs[stem])
            for stem in stems
            if stem in current.approved_pairs
        }
        snapshot.names = {stem: current.names[stem] for stem in stems if stem in current.names}
        snapshot.displays = {
            stem: copy.deepcopy(current.approved_displays[stem])
            for stem in stems
            if stem in current.approved_displays
        }
        snapshot.refused = {
            stem: copy.deepcopy(current.stored_refused[stem])
            for stem in stems
            if stem in current.stored_refused
        }
        for stem in stems:
            current.approved.pop(stem, None)
            current.approved_pairs.pop(stem, None)
            current.approved_displays.pop(stem, None)
            current.names.pop(stem, None)
        refused = {s: r for s, r in current.stored_refused.items() if s not in stems}
        _write(target, _document(current, refused))
    return snapshot


def restore(snapshot: ApprovalSnapshot, path: Path | None = None) -> None:
    """Restore only the rows captured by :func:`revoke`."""
    if not snapshot.stems:
        return
    target = path or approvals_path()
    with _STORE_LOCK:
        current = load_approvals(target)
        occupied = current._retained_stems()
        restore_stems = snapshot.stems - occupied
        current.approved.update(
            {
                stem: set(snapshot.approved[stem])
                for stem in restore_stems
                if stem in snapshot.approved
            }
        )
        current.approved_pairs.update(
            {
                stem: set(snapshot.approved_pairs[stem])
                for stem in restore_stems
                if stem in snapshot.approved_pairs
            }
        )
        current.names.update(
            {stem: snapshot.names[stem] for stem in restore_stems if stem in snapshot.names}
        )
        current.approved_displays.update(
            {
                stem: copy.deepcopy(snapshot.displays[stem])
                for stem in restore_stems
                if stem in snapshot.displays
            }
        )
        current.stored_refused.update(
            {
                stem: copy.deepcopy(snapshot.refused[stem])
                for stem in restore_stems
                if stem in snapshot.refused
            }
        )
        _write(target, _document(current, current.stored_refused))


def refused_servers(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Display-safe refusal records keyed by server name."""
    approvals = load_approvals(path)
    result: dict[str, dict[str, Any]] = {}
    for rec in approvals.stored_refused.values():
        if rec.get("reason") == REFUSED_TOO_MANY:
            continue
        commands, _cut_commands, _malformed_commands = _bounded_commands(rec.get("commands"))
        envs, _cut_envs, _malformed_envs = _bounded_envs(rec.get("envs"))
        result[str(rec["name"])] = {
            "reason": str(rec["reason"]),
            "commands": commands,
            "envs": envs,
            "complete": rec.get("complete") is not False,
            **(
                {"approved_commands": rec["approved_commands"]}
                if rec.get("approved_commands")
                else {}
            ),
            **({"approved_envs": rec["approved_envs"]} if rec.get("approved_envs") else {}),
            **(
                {"expected_launch": rec["expected_launch"]}
                if isinstance(rec.get("expected_launch"), str) and rec["expected_launch"]
                else {}
            ),
        }
    return result


def target_env_stem(key: str) -> str | None:
    """The target stem an env key names, or ``None`` if it names no target.

    Strips the ``KIROCREW_MCP_TARGET_`` / ``MC_MCP_TARGET_`` prefix and any
    ``__<command_args_hash>`` suffix. Gatewayd callers share this definition via
    :func:`kiro_crew.mcp_gateway.gatewayd.resolvable_target_stems`.
    """
    for prefix in _TARGET_PREFIXES:
        if key.startswith(prefix):
            stem = key[len(prefix) :].split("__", 1)[0]
            return stem or None
    return None


def filter_target_env(
    target_env: Mapping[str, str], approvals: LaunchApprovals
) -> tuple[dict[str, str], list[str]]:
    """Drop every target whose command+args is not approved for its server.

    Returns ``(kept, dropped_stems)``. Runs at the last gateway-side stop
    before the map becomes gatewayd's process env, so an overlay, env sidecar
    or cache entry that reached ``target_env`` without passing the rewriter's
    own check still cannot choose what the daemon execs.
    """
    kept: dict[str, str] = {}
    dropped: set[str] = set()
    for key, spec in target_env.items():
        stem = target_env_stem(key)
        if stem is None:
            kept[key] = spec
            continue
        try:
            parts = shlex.split(spec)
        except ValueError:
            parts = []
        if parts and approvals.admits_command(stem, hash_command(parts[0], parts[1:])):
            kept[key] = spec
            continue
        dropped.add(stem)
        if parts:
            display = _bounded_display_argv((parts[0], parts[1:]))
            commands = approvals.refused_commands.get(stem, [])
            if display not in commands and approvals._admission_allowed(
                stem,
                approvals.names.get(stem, stem),
                ((approvals.refused_commands, (display,)),),
            ):
                approvals.refused_commands.setdefault(stem, []).append(display)
    for stem in dropped:
        if stem in approvals.refused or approvals._stem_admitted(
            stem, approvals.names.get(stem, stem)
        ):
            approvals.refused.setdefault(stem, REFUSED_UNAPPROVED)
    return kept, sorted(dropped)


def launch_approved(server_name: str, command_hash: str, derived_env_hash: str) -> bool:
    """gatewayd check for one exact command/effective-environment pair."""
    approvals = load_approvals()
    return approvals.admits_launch(target_stem(server_name), command_hash, derived_env_hash)
