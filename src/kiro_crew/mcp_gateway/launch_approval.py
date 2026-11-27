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
the resolved command and args, and ``hash_launch_env`` over the declared env
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

Sharing has no approval step: the gateway start makes that decision itself.
Its rewrite pass (``LaunchApprovals.auto``, always set by ``_init_mcp_gateway``)
admits every non-managed launch the first time it sees it and again whenever
it changes, the way a server whose changes the operator trusts is admitted,
and records it here with its own audit reason. So a shared backend's launch is
one no person looked at; on Linux and macOS gatewayd runs it inside the global
sandbox (``backend._sandboxed_backend_argv``), and outside it on Windows. The store
stays the record of what ran. Never-pooled servers are still never admitted,
and a launch set past the per-server cap is still refused. A pass without
``auto`` (the dashboard's resolution, the app-refresh probe) applies every rule
below.

An approval gets its fingerprints at the moment of the operator's decision.
Turning a stub on resolves the launch behind the name right then, through the
rewriter's own resolution (:mod:`kiro_crew.mcp_gateway.launch_resolve`), and
records exactly that launch -- so an agent that edits a spec after the click,
before the next gateway start rewrites the overlays, gets its launch refused.
A store that does not exist or is empty approves no launch. Existing stubs
require an operator approval through MCP Management (MCP Servers → Sharing)
before the daemon may run their backends outside the sandbox.

Standard library only (plus the shared hashing leaf and ``config.paths``), for
the same reason as :mod:`kiro_crew.mcp_gateway.hashing`: the rewriter sits on
``config.loader``'s import path.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping, TypeGuard

from kiro_crew.config.paths import config_dir
from kiro_crew.mcp_gateway import launch_approval_heal as _heal
from kiro_crew.mcp_gateway.hashing import (
    gateway_path,
    hash_command,
    hash_launch_env,
    launch_env_value_bytes,
)
from kiro_crew.mcp_gateway.launch_approval_heal import CASING_AUDIT_REASON  # noqa: F401
from kiro_crew.mcp_gateway.launch_approval_normalize import (  # noqa: F401 - re-exported
    NORMALIZED_AUDIT_REASON,
    normalized_command_hash,
    normalized_repair_source,
    retire_normalized_sources,
)

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
#: A launch is approvable only from a display that holds it in full, so the
#: count sits past what a readable launch passes (255 ``--include-tools <tool>``
#: pairs fit) and still bounds the quoting each entry adds to a stored display.
_MAX_DISPLAY_ARGS = 512
#: Maximum characters retained across one whole display list -- an argv or a
#: declared environment -- including its partial marker. The budget is the
#: list's, not each entry's: a launch may pass one long value and still be
#: readable in full, and only a list this large is cut. It is what keeps one
#: stored display small.
_MAX_DISPLAY_TOTAL_CHARS = 16384
#: Characters held back from :data:`_MAX_DISPLAY_TOTAL_CHARS` for the marker that
#: says a display was cut, and the ceiling on that marker.
_MAX_DISPLAY_MARKER_CHARS = 64
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

#: Purpose label of the key the per-key env digests are taken under
#: (:func:`_env_digests`), derived from the dashboard token-signing secret.
_ENV_DIGEST_KEY_DOMAIN = b"kiro-crew:mcp-launch-env:key:v1"
_HEX_DIGITS = frozenset("0123456789abcdef")

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

    Secret-prefixed entries count too: private backends receive them. The
    gateway's own part of ``PATH`` does not (``hashing.hash_launch_env``).
    """
    return hash_launch_env(_coerced_env(env_pairs))


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


def pin_gateway_part() -> None:
    """Fix this process's gateway ``PATH`` part for the life of the MCP broker.

    The broker calls it before its rewrite pass, so the pass, every later
    agent-spec rebuild and the gatewayd it hands the part to fold one
    composition. BLOCKING: the first read scans the well-known directories.
    """
    from kiro_crew.env import pin_gateway_spec_path

    pin_gateway_spec_path(gateway_path())


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
    #: stem -> approved pair -> ``(key check, env key -> digest)`` of the approved
    #: launch's declared env (:func:`_env_digests`), stored with its display so a
    #: refusal can name a key whose value changed where the displays read the same.
    #: Never consulted to admit a launch.
    approved_env_digests: dict[str, dict[str, tuple[str, dict[str, str]]]] = field(
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
    #: Fingerprints admitted as an approved launch whose command only went
    #: through ``env.saved_command_casing`` (stem -> fingerprint -> the
    #: approved source fingerprint and pair it was admitted under).
    casing_healed: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    #: Likewise for a launch whose approval predates the ``PATH`` fold.
    prefold_healed: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    #: Stems the operator chose "Always trust changes to this server" for, in the dashboard.
    trusted: set[str] = field(default_factory=set)
    #: Set for the gateway start's pass (sharing has no approval step): every non-managed launch
    #: is admitted and recorded the first time it is seen and whenever it changes,
    #: as a trusted server's changes are. Never persisted.
    auto: bool = False
    #: Trusted stems: fingerprints admitted only by trust, and every fingerprint seen live.
    trust_admitted: dict[str, dict[str, str]] = field(default_factory=dict)
    trust_live: dict[str, set[str]] = field(default_factory=dict)
    #: Every server stem declared, as this pass read them; incomplete if one source was skipped.
    declared: set[str] = field(default_factory=set)
    declared_incomplete: bool = False
    #: stem -> approved pair -> its normalized command digest
    #: (:func:`normalized_command_hash`), when an edition normalizer folds it.
    approved_normalized: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Fingerprints admitted as an approved launch whose only change is a token
    #: the edition normalizer folds (stem -> fingerprint -> source fingerprint
    #: and pair), saved under the same guard as :attr:`casing_healed`.
    normalized_healed: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    #: Normalized digests this pass learned for live approved pairs (stem -> pair
    #: -> digest); a save writes each only while its pair is still approved.
    normalized_seen: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Additional redacted display argv found by the final target filter.
    refused_commands: dict[str, list[list[str]]] = field(default_factory=dict)
    #: Every approval identity and its redacted command/env displays seen this pass.
    #: ``None`` keeps an identity whose launch content cannot safely be displayed.
    launch_identities: dict[str, dict[str, tuple[list[str] | None, list[str] | None]]] = field(
        default_factory=dict
    )
    #: The same digests for each identity in :attr:`launch_identities`, for the
    #: per-key comparison of a changed refusal. Never persisted.
    launch_env_digests: dict[str, dict[str, tuple[str, dict[str, str]]]] = field(
        default_factory=dict
    )
    #: Approved pairs this pass admitted once :attr:`launch_identities` held the
    #: per-server cap for their stem (stem -> pairs). They are live, so a save
    #: retires none of them as unseen. Each is in :attr:`approved_pairs`, which
    #: the same cap bounds.
    live_overflow: dict[str, set[str]] = field(default_factory=dict)
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
            self.trust_live,
            self.declared,
        )

    def trusts(self, stem: str) -> bool:
        """Whether this pass admits a changed (or first) launch of *stem* by trust."""
        return stem in self.trusted or (self.auto and stem not in _unpoolable_stems())

    def note_declared(self, name: str) -> None:
        """Record a declared server within the snapshot's name and record bounds.

        A slash-named server is recorded under its raw stem and its slash-free
        alias stem: an agent spec's own ``mcpServers`` admits its launch under the
        raw name and other paths under the alias, so its trust may sit under either.
        """
        from kiro_crew.mcp_utils import mcp_server_alias

        for stem in dict.fromkeys((target_stem(name), target_stem(mcp_server_alias(name)))):
            if not self._stem_admitted(stem, name):
                self.declared_incomplete = True
                continue
            self.declared.add(stem)

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
                        "mcp launch approvals: refusing %s; per-server launch cap "
                        "_MAX_SERVER_LAUNCHES=%d reached",
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

    def _note_identity(
        self,
        stem: str,
        identity: str,
        launch: tuple[str, list[str]] | None,
        env: Mapping[str, Any] | None,
    ) -> tuple[list[str] | None, list[str] | None, bool]:
        """Retain one launch identity seen this pass with its redacted displays.

        Returns the command and env displays and whether both are complete.
        """
        command_display, env_display, display_complete = _launch_displays(launch, env)
        if not display_complete:
            self.incomplete_identities.setdefault(stem, set()).add(identity)
        # Only an approved server's launch can be refused as changed.
        digests = _env_digests(stem, env, env_display) if self.approved.get(stem) else None
        if digests is not None:
            self.launch_env_digests.setdefault(stem, {})[identity] = digests
        identities = self.launch_identities.setdefault(stem, {})
        prior_command, prior_env = identities.get(identity, (None, None))
        identities[identity] = (
            command_display if command_display is not None else prior_command,
            env_display if env_display is not None else prior_env,
        )
        return command_display, env_display, display_complete

    def _note_live(
        self,
        stem: str,
        identity: str,
        launch: tuple[str, list[str]] | None,
        env: Mapping[str, Any] | None,
    ) -> tuple[list[str] | None, list[str] | None, bool]:
        """Retain an approved launch this pass admits, whatever the per-server cap.

        Other agents declaring the same name with launches of their own fill
        :attr:`launch_identities` in file order. A pair with no room left there
        goes to :attr:`live_overflow` instead, so a save still counts it live.
        """
        identities = self.launch_identities.get(stem, {})
        if identity in identities or len(identities) < _MAX_SERVER_LAUNCHES:
            return self._note_identity(stem, identity, launch, env)
        self.live_overflow.setdefault(stem, set()).add(identity)
        return _launch_displays(launch, env)

    def _learn_normalized(
        self, stem: str, identity: str, launch: tuple[str, list[str]] | None
    ) -> None:
        """Remember *identity*'s normalized digest for the save, when it has one."""
        if launch is None:
            return
        digest = normalized_command_hash(str(launch[0]), [str(a) for a in launch[1]])
        if digest is not None and self.approved_normalized.get(stem, {}).get(identity) != digest:
            self.normalized_seen.setdefault(stem, {})[identity] = digest

    def admit(
        self,
        name: str,
        fingerprint: str,
        *,
        managed: bool = False,
        launch: tuple[str, list[str]] | None = None,
        env: Mapping[str, Any] | None = None,
        derived_env_hash: str,
        prefold: tuple[str, str] | None = None,
    ) -> bool:
        """Whether *name* may launch *fingerprint* outside the sandbox.

        ``managed`` marks a launch the code derived itself (a reserved
        ``kirocrew-*`` server repaired to the managed invocation): it is
        recorded without needing an operator approval, because nothing
        agent-writable chose it. ``launch`` is the ``(command, args)`` behind
        the fingerprint, kept for a probe pass to report. ``prefold`` is its
        ``launch_approval_heal.prefold_digests``, for a store older than the fold.
        """
        stem = target_stem(name)
        command_hash, _declared_env_hash = _split(fingerprint)
        identity = launch_pair(command_hash, derived_env_hash)
        _heal.note_trust_live(self, stem, fingerprint)
        # Decided before the per-server cap, which bounds what this pass retains
        # for display: other agents declaring the same name with launches of
        # their own fill it in file order, and must not refuse the approved one.
        if (
            not self.probe
            and fingerprint in self.approved.get(stem, ())
            and self.admits_launch(stem, command_hash, derived_env_hash)
        ):
            self._note_live(stem, identity, launch, env)
            self._learn_normalized(stem, identity, launch)
            return True
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
        healed = prefolded = normalized = None
        if not (self.probe or managed or rebind):
            healed = _heal.casing_repair_of_approved(
                self, stem, fingerprint, derived_env_hash, launch
            )
            prefolded = (
                None
                if healed
                else _heal.prefold_approval_of(self, stem, fingerprint, prefold, launch)
            )
            if healed is None and prefolded is None:
                normalized = normalized_repair_source(
                    self, stem, fingerprint, derived_env_hash, launch
                )
        trusted = not (
            self.probe or rebind or healed or prefolded or normalized
        ) and _heal.trusts_change(self, stem, managed)
        rebind = (
            rebind
            or healed is not None
            or prefolded is not None
            or normalized is not None
            or trusted
        )
        additions: list[tuple[Mapping[str, Collection[Any]], Collection[Any]]] = []
        # A rebind is an approved launch, so like the exact one above it is not
        # counted against the display set; what it adds to the store stays capped.
        if not self.probe and not rebind:
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
        additions = _heal.without_sources(self, stem, prefolded, additions, trusted)
        if not self._admission_allowed(stem, name, additions):
            return False
        if self.probe:
            self._record(stem, name, fingerprint, derived_env_hash, launch, env)
            return True
        command_display, env_display, display_complete = (
            self._note_live(stem, identity, launch, env)
            if rebind
            else self._note_identity(stem, identity, launch, env)
        )
        if managed:
            self._record(stem, name, fingerprint, derived_env_hash, launch, env)
            return True
        if rebind:
            self._learn_normalized(stem, identity, launch)
            if healed is not None:
                self.casing_healed.setdefault(stem, {})[fingerprint] = healed
            elif prefolded is not None:
                self.prefold_healed.setdefault(stem, {})[fingerprint] = prefolded
            elif normalized is not None:
                self.normalized_healed.setdefault(stem, {})[fingerprint] = normalized
            elif trusted:
                self.trust_admitted.setdefault(stem, {})[fingerprint] = identity
            else:
                self.rebind_base.setdefault(stem, frozenset(self.approved_pairs.get(stem, ())))
                self.rebound.setdefault(stem, {})[identity] = fingerprint
            self._record(stem, name, fingerprint, derived_env_hash, launch, env)
            if display_complete and command_display is not None and env_display is not None:
                self.approved_displays.setdefault(stem, {})[identity] = (
                    command_display,
                    env_display,
                )
                digests = _env_digests(stem, env, env_display)
                if digests is not None:
                    self.approved_env_digests.setdefault(stem, {})[identity] = digests
            logger.info(
                "mcp launch approvals: %s keeps its approval; only %s changed",
                _bounded_name(name),
                (
                    "its command's extension case"
                    if healed
                    else (
                        "how its digest folds the gateway's PATH"
                        if prefolded
                        else (
                            "a version folder the edition folds"
                            if normalized
                            else (
                                "its launch, and the operator trusts its changes"
                                if trusted
                                else "a ${VAR} value in its declared env"
                            )
                        )
                    )
                ),
            )
            return True
        self.names.setdefault(stem, _bounded_name(name))
        # A launch already retained before the cap was hit must not hide the cap.
        if self.refused.get(stem) != REFUSED_TOO_MANY:
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
        """Whether one exact command/effective-environment pair was approved.

        A never-pooled server (a credential vendor) is refused whatever its
        recorded approval: this is the check ``gatewayd`` runs before it spawns
        a shared backend, so an approval written before the name was listed
        cannot let it run outside every chat sandbox.
        """
        if stem in _unpoolable_stems():
            return False
        return launch_pair(command_hash, derived_env_hash) in self.approved_pairs.get(stem, ())

    def admits_command(self, stem: str, command_hash: str) -> bool:
        """Whether *command_hash* participates in an approved pair."""
        prefix = f"{command_hash}:"
        return any(pair.startswith(prefix) for pair in self.approved_pairs.get(stem, ()))

    def saw_command(self, stem: str, command_hash: str) -> bool:
        """Whether this pass recorded a launch of *command_hash* for *stem*."""
        prefix = f"{command_hash}:"
        return any(identity.startswith(prefix) for identity in self.launch_identities.get(stem, {}))

    def digest(self) -> list[Any]:
        """Stable summary folded into the rewriter's input fingerprint.

        JSON-native (lists, not tuples): the rewriter compares it with the copy
        it reloads from the stored fingerprint, and a tuple never equals the
        list that copy decodes to.
        """
        rows: list[Any] = [
            [stem, sorted(fps), sorted(self.approved_pairs.get(stem, ()))]
            for stem, fps in sorted(self.approved.items())
        ]
        # A pass that approves automatically must not be served a cached
        # overlay whose refusals were decided without it.
        return [*rows, ["auto"]] if self.auto else rows


def _bounded_name(value: Any) -> str:
    """Return one bounded operator-facing server name."""
    text = str(value)
    if len(text) <= _MAX_NAME_CHARS:
        return text
    return text[: _MAX_NAME_CHARS - 1] + "…"


def _bounded_display_argv_with_completeness(
    launch: tuple[Any, Iterable[Any]],
) -> tuple[list[str], bool]:
    """Redact and bound one argv, returning whether the display is complete.

    The budget is the whole list's characters, not each entry's. A per-entry cap
    cuts the display of a launch that passes one long value -- every skill
    directory in a single ``--skill-paths``, say -- even when its whole argv is
    a fraction of what the display may hold. The cut display is then
    unapprovable, so that server's backend can never leave the sandbox and each
    session starts its own copy. A total budget shows such a launch whole and
    still refuses one genuinely too large to read. The entry count works the
    same way: a launch that passes one flag and value per tool is read in full
    up to :data:`_MAX_DISPLAY_ARGS`, and only a longer one is cut.

    A cut display stays unapprovable because an approval covers only a launch
    the operator can read: a row's Details and the Review dialog show this
    display beside the Approve that pins it, and a change past the cut would be
    approved unseen.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    raw = [launch[0], *launch[1]]
    safe: list[str] = []
    # The marker's room is held back, so the list that fills its budget can
    # still say it was cut.
    remaining = _MAX_DISPLAY_TOTAL_CHARS - _MAX_DISPLAY_MARKER_CHARS
    cut = False
    for value in raw[:_MAX_DISPLAY_ARGS]:
        text, _ = redact_exfiltration_urls(str(value))
        text, _ = redact_credentials(text)
        if len(text) > remaining:
            safe.append(text[: max(0, remaining - 1)] + "…")
            cut = True
            break
        remaining -= len(text)
        safe.append(text)
    dropped = max(0, len(raw) - len(safe))
    partial = cut or bool(dropped)
    if partial:
        if len(safe) >= _MAX_DISPLAY_ARGS:
            safe.pop()
            dropped += 1
        marker = "…(partial)" if not dropped else f"…(+{dropped} args; partial)"
        safe.append(marker[:_MAX_DISPLAY_MARKER_CHARS])
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


def _env_digests(
    stem: str, env: Mapping[str, Any] | None, env_display: list[str] | None
) -> tuple[str, dict[str, str]] | None:
    """A keyed digest of each displayed env value, with a check of the key, or ``None``.

    Only a key the bounded *env_display* names gets one, so the digests stay
    within the display's bounds however large the declared env is. The key is
    derived from the dashboard token-signing secret, which the agent sandbox
    cannot read: the store is readable there, and an unkeyed hash of a
    hidden value would let a guess be checked against it. A value hashes as the
    approval's env fingerprint reads it (:func:`hash_launch_env`), so the
    gateway's own ``PATH`` part moving changes no digest. ``None`` without the
    persisted secret: a temporary one signs nothing a later process can compare.
    """
    if env is None or env_display is None:
        return None
    # Deferred: this leaf stays standard library at import time.
    from kiro_crew.dashboard.token_secret import get_persisted_secret

    try:
        root = get_persisted_secret()
    except (OSError, RuntimeError):
        return None
    key = hmac.new(root, _ENV_DIGEST_KEY_DOMAIN, hashlib.sha256).digest()
    prefix = stem.encode("utf-8", "surrogatepass") + b"\0"
    shown = _displayed_keys([env_display])
    return (
        hmac.new(key, b"check", hashlib.sha256).hexdigest(),
        {
            name: hmac.new(
                key,
                prefix + name.encode("utf-8") + b"\0" + launch_env_value_bytes(name, value),
                hashlib.sha256,
            ).hexdigest()
            for name, value in _coerced_env(env).items()
            if name in shown
        },
    )


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


def _launch_displays(
    launch: tuple[str, list[str]] | None, env: Mapping[str, Any] | None
) -> tuple[list[str] | None, list[str] | None, bool]:
    """Bounded command and env displays of one launch, and whether both are complete."""
    command_display: list[str] | None = None
    env_display: list[str] | None = None
    complete = False
    if launch is not None:
        command_display, command_complete = _bounded_display_argv_with_completeness(launch)
        if env is not None:
            env_display, env_complete = _bounded_display_env_with_completeness(env)
            complete = command_complete and env_complete
    return command_display, env_display, complete


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


def _displayed_keys(envs: Iterable[Iterable[str]]) -> set[str]:
    """The env key names a list of env displays shows."""
    return {line.split("=", 1)[0] for env in envs for line in env if "=" in line}


def _is_digest(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX_DIGITS


def _parse_env_digests(
    value: Any, displays: Mapping[str, tuple[list[str], list[str]]]
) -> dict[str, tuple[str, dict[str, str]]]:
    """Stored env digests of each parsed display, for the keys that display names."""
    if not isinstance(value, dict):
        return {}
    digests: dict[str, tuple[str, dict[str, str]]] = {}
    for pair, (_command, env) in displays.items():
        entry = value.get(pair)
        stored = entry.get("env_digests") if isinstance(entry, dict) else None
        if not isinstance(stored, dict):
            continue
        check, values = stored.get("key"), stored.get("values")
        if not _is_digest(check) or not isinstance(values, dict):
            continue
        digests[pair] = (
            check,
            {name: values[name] for name in _displayed_keys([env]) if _is_digest(values.get(name))},
        )
    return digests


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
            env_digests = _parse_env_digests(record.get("displays"), displays)
            if env_digests:
                approvals.approved_env_digests[stem] = env_digests
            stored_normalized = record.get("normalized")
            if isinstance(stored_normalized, dict):
                normalized = {
                    pair: digest
                    for pair, digest in stored_normalized.items()
                    if pair in pairs and _is_digest(digest)
                }
                if normalized:
                    approvals.approved_normalized[stem] = normalized
            name = record.get("name")
            approvals.names[stem] = _bounded_name(name if isinstance(name, str) else stem)
            if record.get("trust_changes") is True:
                approvals.trusted.add(stem)
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
            stored_keys = rec.get("changed_env_keys")
            changed_env_keys = (
                sorted(
                    {key for key in stored_keys if isinstance(key, str)}
                    & _displayed_keys(envs)
                    & _displayed_keys(approved_envs)
                )
                if approved_commands and isinstance(stored_keys, list)
                else []
            )
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
                **({"changed_env_keys": changed_env_keys} if changed_env_keys else {}),
            }
    return approvals


class ApprovalStoreUnreadable(OSError):
    """The approval store cannot be the base of a rewrite, so a writer left it as it is.

    The writers' contract: :func:`approve`, :func:`revoke`, :func:`restore` and
    :func:`save_pass` raise this rather than rewrite a store they could not read.
    It is an ``OSError``, so a caller that answers a failed store write answers
    this the same way.
    """


def _read_store(target: Path, *, strict_decode: bool = False) -> tuple[str, str | None]:
    """The store's text, or ``""`` and why it cannot be read.

    The one read every approval reader shares, so a failure is classified once.
    An absent store reads as ``""`` with no reason: it records nothing yet. With
    *strict_decode*, bytes that are not UTF-8 raise :class:`ApprovalStoreUnreadable`.
    """
    try:
        return target.read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return "", None
    except UnicodeDecodeError as exc:
        if strict_decode:
            raise ApprovalStoreUnreadable(f"{target.name} is not valid UTF-8") from exc
        return "", "not valid UTF-8"
    except OSError as exc:
        return "", f"unreadable ({type(exc).__name__})"


def _load(target: Path, *, strict_decode: bool) -> LaunchApprovals:
    text, problem = _read_store(target, strict_decode=strict_decode)
    if problem:
        logger.warning("mcp launch approvals: store %s; approving no launch", problem)
        return LaunchApprovals()
    if not text.strip():
        return LaunchApprovals()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("mcp launch approvals: store is not valid JSON; approving no launch")
        return LaunchApprovals()
    return _parse(raw)


def load_approvals(path: Path | None = None) -> LaunchApprovals:
    """Read the store. Absent, empty, unreadable, or malformed approves nothing."""
    return _load(path or approvals_path(), strict_decode=False)


def _load_for_update(target: Path) -> LaunchApprovals:
    """The base of a store rewrite: :func:`load_approvals`, except bytes that do
    not decode raise :class:`ApprovalStoreUnreadable`, so the rewrite is abandoned
    and the file is left as it is.

    shortcut: an unreadable or non-JSON store still reads as empty here, the
    lenient-read-then-rewrite shape; make it raise when that shape is tightened.
    """
    return _load(target, strict_decode=True)


def check_store_rewritable(path: Path | None = None) -> None:
    """Raise :class:`ApprovalStoreUnreadable` when a writer would refuse the store.

    For a caller that must not change anything else before a store write fails.
    """
    _load_for_update(path or approvals_path())


def approvals_store_problem(path: Path | None = None) -> str | None:
    """Why the store cannot be read, or ``None`` when it reads.

    For diagnostics only. :func:`load_approvals` reads every one of these
    outcomes as "approves nothing", which is right for launch authorization and
    wrong for a report: an unreadable store and an empty one look the same there.
    An absent or blank store is not a problem; it records nothing yet.
    """
    text, problem = _read_store(path or approvals_path())
    if problem:
        return problem
    if not text.strip():
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return "not valid JSON"
    if not isinstance(raw, dict):
        return "not a JSON object"
    if raw.get("version") != _VERSION:
        return "written in a format this version does not read"
    return None


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
            **({"trust_changes": True} if stem in approvals.trusted else {}),
        }
        env_digests = approvals.approved_env_digests.get(stem, {})
        displays = {
            pair: {
                "command": list(command),
                "env": list(env),
                **(
                    {"env_digests": {"key": env_digests[pair][0], "values": env_digests[pair][1]}}
                    if pair in env_digests
                    else {}
                ),
            }
            for pair, (command, env) in sorted(approvals.approved_displays.get(stem, {}).items())
            if pair in pairs
        }
        if displays:
            servers[stem]["displays"] = dict(list(displays.items())[:_MAX_SERVER_LAUNCHES])
        normalized = {
            pair: digest
            for pair, digest in sorted(approvals.approved_normalized.get(stem, {}).items())
            if pair in pairs
        }
        if normalized:
            servers[stem]["normalized"] = normalized
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
            if raw_record.get("changed_env_keys"):
                record["changed_env_keys"] = list(raw_record["changed_env_keys"])
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
    # Deferred: ``atomic_write`` and ``owner_only_files`` pull in platform
    # helpers this leaf does not need at import time.
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.owner_only_files import mkdirs_owner_only

    # The store's directory is born ``0700``, whatever the process umask: a bare
    # ``mkdir`` takes the umask default (``0755`` under the usual ``022``), and
    # this directory is the SEAL for the store (the file-tool gate and the OS
    # sandbox both fence it by name), so an other-readable one would publish
    # which launches the operator approved to every account on the host. The
    # sandbox precreates it ``0700`` on a Linux spawn; this is the first writer
    # on a host that has not spawned yet, and it must not depend on that order.
    mkdirs_owner_only(path.parent)
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


def _digests_by_key(
    entries: list[tuple[str, dict[str, str]] | None],
) -> tuple[set[str], dict[str, set[str]]] | None:
    """The key checks *entries* carry and each env key's digests; ``None`` if one is missing."""
    if not entries:
        return None
    checks: set[str] = set()
    by_key: dict[str, set[str]] = {}
    for entry in entries:
        if entry is None:
            return None
        checks.add(entry[0])
        for key, digest in entry[1].items():
            by_key.setdefault(key, set()).add(digest)
    return checks, by_key


def _changed_env_keys(
    approvals: LaunchApprovals, stem: str, envs: list[list[str]], approved_envs: list[list[str]]
) -> list[str]:
    """Env keys set on both sides whose values differ by keyed digest. Names only.

    Each key's digests are compared as sets over each side's launches, since
    the gateway pairs no launch with another. Empty unless every launch on both
    sides has digests under the same key -- an approval recorded before digests
    were kept, or a signing secret that changed since, leaves the page comparing
    the displays as before. Only a key both displays name is listed.
    """
    stored = approvals.approved_env_digests.get(stem, {})
    live = approvals.launch_env_digests.get(stem, {})
    before = _digests_by_key([stored.get(pair) for pair in approvals.approved_pairs.get(stem, ())])
    after = _digests_by_key(
        [live.get(identity) for identity in approvals.launch_identities.get(stem, {})]
    )
    if before is None or after is None or len(before[0] | after[0]) != 1:
        return []
    shown = _displayed_keys(envs) & _displayed_keys(approved_envs)
    return sorted(
        key for key in before[1].keys() & after[1].keys() & shown if before[1][key] != after[1][key]
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
            changed = _changed_env_keys(approvals, stem, all_envs, approved_envs)
            if changed:
                record["changed_env_keys"] = changed
    return record


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
        current = _load_for_update(target)
        changed = False
        withheld: dict[str, set[str]] = {}
        stems = approvals.captured.keys() | (
            current.approved_normalized.keys() if approvals.full_pass else set()
        )
        for stem in stems:
            fps = approvals.captured.get(stem, set())
            before = set(current.approved.get(stem, ()))
            before_pairs = set(current.approved_pairs.get(stem, ()))
            written, retired, untouched = _heal.settle_rebinds(
                approvals, stem, before_pairs, before
            )
            dropped = set(approvals.rebound.get(stem, {})) - written
            withdrawn = {
                approvals.rebound[stem][pair]
                for pair in dropped
                if approvals.rebound[stem][pair] not in before
            }
            # A casing, pre-fold or normalized repair is written only while the
            # store still approves its source.
            healed = {
                **approvals.casing_healed.get(stem, {}),
                **approvals.prefold_healed.get(stem, {}),
                **approvals.normalized_healed.get(stem, {}),
            }
            for fp, (source, source_pair) in healed.items():
                if source not in before or source_pair not in before_pairs:
                    withdrawn.add(fp)
                    dropped.add(launch_pair(_split(fp)[0], _split(source_pair)[1]))
            if not (approvals.auto or stem in current.trusted):
                withdrawn |= set(approvals.trust_admitted.get(stem, {}))
                dropped |= set(approvals.trust_admitted.get(stem, {}).values())
            withheld[stem] = withdrawn
            superseded = _heal.superseded_sources(approvals, stem, withdrawn)
            fps = {fp for fp in fps if fp not in withdrawn}
            if not fps and not before_pairs:
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
            final_fps = (before | fps) - superseded
            final_pairs = (before_pairs | captured_pairs) - retired - superseded
            retire_normalized_sources(
                approvals,
                stem,
                current.approved_normalized.get(stem, {}),
                before,
                before_pairs,
                final_fps,
                final_pairs,
            )
            if not fps and final_fps == before and final_pairs == before_pairs:
                continue
            final_fps, final_pairs = _heal.retire_trusted(
                approvals, current, stem, final_fps, final_pairs
            )
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
            pass_digests = approvals.approved_env_digests.get(stem, {})
            new_digests = {
                pair: pass_digests[pair] for pair in new_displays if pair in pass_digests
            }
            if new_digests:
                current.approved_env_digests.setdefault(stem, {}).update(new_digests)
            changed = (
                changed or final_fps != before or final_pairs != before_pairs or bool(new_displays)
            )

        # A learned normalized digest is written only beside a pair the merged
        # store still approves, so a revoke that landed during the pass wins.
        for stem, seen in approvals.normalized_seen.items():
            live_pairs = current.approved_pairs.get(stem, set())
            for pair, digest in seen.items():
                if (
                    pair in live_pairs
                    and current.approved_normalized.get(stem, {}).get(pair) != digest
                ):
                    current.approved_normalized.setdefault(stem, {})[pair] = digest
                    changed = True

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
        changed = (
            _heal.forget_unseen_trust(approvals, current) or refused != original_refused or changed
        )
        if not changed:
            return False
        _write(target, _document(current, refused))
    for reason, fingerprints in _heal.captured_by_reason(approvals, withheld).items():
        logger.warning(
            "mcp launch approvals: recorded %d launch fingerprint(s) for %s (%s)",
            sum(len(v) for v in fingerprints.values()),
            ", ".join(sorted(approvals.names.get(s, s) for s in fingerprints)),
            reason,
        )
    return True


def approve(
    launches: Mapping[str, Iterable[ResolvedLaunch]],
    path: Path | None = None,
    *,
    trusted: Collection[str] = (),
) -> None:
    """Record exactly *launches* as the approved launches of their server names.

    *launches* maps a server name to the launches it resolved to when the
    operator decided (see :func:`kiro_crew.mcp_gateway.launch_resolve.resolve_launches`).
    Replaces every fingerprint already approved for those names, so turning a
    stub on approves what it runs NOW rather than keeping an older launch alive
    beside it. The env the daemon may forward is the env those launches
    derived when they were resolved, so a backend the running daemon spawns
    before the next rewrite still gets its declared env; that rewrite derives
    it again. Gateway-side writer only: the dashboard stub toggle. A name in
    *trusted* keeps later launch changes shared ("Always trust changes to this
    server"); every other approved name loses that choice, so each approval is
    a fresh decision.
    """
    target = path or approvals_path()
    decided: dict[str, tuple[str, set[str], set[str]]] = {}
    with _STORE_LOCK:
        current = _load_for_update(target)
        for name, items in launches.items():
            if not isinstance(name, str) or not name:
                continue
            fps: set[str] = set()
            pairs: set[str] = set()
            displays: dict[str, tuple[list[str], list[str]]] = {}
            digests: dict[str, tuple[str, dict[str, str]]] = {}
            normalized: dict[str, str] = {}
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
                launch_digests = _env_digests(
                    target_stem(name), dict(launch.declared_env), display[1]
                )
                if launch_digests is not None:
                    digests.update({pair: launch_digests for pair in launch_pairs})
                folded = normalized_command_hash(launch.command, list(launch.args))
                if folded is not None:
                    normalized.update({pair: folded for pair in launch_pairs})
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
            current.approved_env_digests[stem] = digests
            current.approved_normalized[stem] = normalized
            current.names[stem] = _bounded_name(name)
            (current.trusted.add if name in trusted else current.trusted.discard)(stem)
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
    env_digests: dict[str, dict[str, tuple[str, dict[str, str]]]] = field(default_factory=dict)
    trusted: set[str] = field(default_factory=set)
    normalized: dict[str, dict[str, str]] = field(default_factory=dict)


def revoke(names: Iterable[str], path: Path | None = None) -> ApprovalSnapshot:
    """Forget approvals for *names* and return their immediately prior rows."""
    stems = {target_stem(n) for n in names if isinstance(n, str) and n}
    snapshot = ApprovalSnapshot(stems, {}, {}, {}, {})
    if not stems:
        return snapshot
    target = path or approvals_path()
    with _STORE_LOCK:
        current = _load_for_update(target)
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
        snapshot.env_digests = {
            stem: copy.deepcopy(current.approved_env_digests[stem])
            for stem in stems
            if stem in current.approved_env_digests
        }
        snapshot.normalized = {
            stem: dict(current.approved_normalized[stem])
            for stem in stems
            if stem in current.approved_normalized
        }
        snapshot.refused = {
            stem: copy.deepcopy(current.stored_refused[stem])
            for stem in stems
            if stem in current.stored_refused
        }
        snapshot.trusted = stems & current.trusted
        current.trusted -= stems
        for stem in stems:
            current.approved.pop(stem, None)
            current.approved_pairs.pop(stem, None)
            current.approved_displays.pop(stem, None)
            current.approved_env_digests.pop(stem, None)
            current.approved_normalized.pop(stem, None)
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
        current = _load_for_update(target)
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
        current.approved_env_digests.update(
            {
                stem: copy.deepcopy(snapshot.env_digests[stem])
                for stem in restore_stems
                if stem in snapshot.env_digests
            }
        )
        current.approved_normalized.update(
            {
                stem: dict(snapshot.normalized[stem])
                for stem in restore_stems
                if stem in snapshot.normalized
            }
        )
        current.stored_refused.update(
            {
                stem: copy.deepcopy(snapshot.refused[stem])
                for stem in restore_stems
                if stem in snapshot.refused
            }
        )
        current.trusted |= snapshot.trusted & restore_stems
        _write(target, _document(current, current.stored_refused))


def refused_servers(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Display-safe refusal records keyed by server name.

    A ``too_many_launches`` record is display only: it shows the launches the
    cap kept and the cap itself, and never an ``expected_launch``, because no
    approval can cover a server past the cap.
    """
    approvals = load_approvals(path)
    result: dict[str, dict[str, Any]] = {}
    for rec in approvals.stored_refused.values():
        over_cap = rec.get("reason") == REFUSED_TOO_MANY
        commands, _cut_commands, _malformed_commands = _bounded_commands(rec.get("commands"))
        envs, _cut_envs, _malformed_envs = _bounded_envs(rec.get("envs"))
        result[str(rec["name"])] = {
            "reason": str(rec["reason"]),
            "commands": commands,
            "envs": envs,
            "complete": rec.get("complete") is not False and not over_cap,
            **({"launch_cap": _MAX_SERVER_LAUNCHES} if over_cap else {}),
            **(
                {"approved_commands": rec["approved_commands"]}
                if rec.get("approved_commands")
                else {}
            ),
            **({"approved_envs": rec["approved_envs"]} if rec.get("approved_envs") else {}),
            **(
                {"changed_env_keys": rec["changed_env_keys"]} if rec.get("changed_env_keys") else {}
            ),
            **(
                {"expected_launch": rec["expected_launch"]}
                if not over_cap
                and isinstance(rec.get("expected_launch"), str)
                and rec["expected_launch"]
                else {}
            ),
        }
    return result


def refused_share_records(
    asked: Iterable[str], refused: Mapping[str, Mapping[str, Any]]
) -> dict[str, Mapping[str, Any]]:
    """Refusal record per share-list name whose launch is refused, keyed as configured.

    ``asked`` is ``mcp_gateway.stub_servers`` as config states it; ``refused`` is
    :func:`refused_servers`. The rewriter admits a slash-named server under its
    slash-free alias (``npm:@playwright/mcp`` is recorded as ``playwright-mcp``),
    so each configured name is looked up under itself and then under its alias.
    A refusal for a server that is not on the share list matches nothing here.
    """
    from kiro_crew.mcp_utils import mcp_server_alias

    result: dict[str, Mapping[str, Any]] = {}
    for name in asked:
        rec = refused.get(name)
        if rec is None:
            rec = refused.get(mcp_server_alias(name))
        if rec is not None:
            result[name] = rec
    return result


def refused_shares(
    asked: Iterable[str], refused: Mapping[str, Mapping[str, Any]]
) -> dict[str, str]:
    """Refusal reason per share-list name, matched as :func:`refused_share_records` does.

    A ``too_many_launches`` name is left out: this feeds the notice that asks for an
    approval, no approval clears a server over the cap, and its own MCP Management
    row explains it.
    """
    return {
        name: str(rec.get("reason", ""))
        for name, rec in refused_share_records(asked, refused).items()
        if rec.get("reason") != REFUSED_TOO_MANY
    }


def unapproved_share_names(asked: Iterable[Any], path: Path | None = None) -> list[str]:
    """Share-list names, as configured, the store holds no approved launch for.

    Writing a name to ``mcp_gateway.stub_servers`` approves nothing: the next
    gateway start refuses its launch as ``added_outside_dashboard`` until the
    operator approves it in MCP Management, so a writer outside the dashboard
    names these. Each name is looked up under itself and under its slash-free
    alias, as :func:`refused_share_records` does. A reserved ``kirocrew-*`` server is
    left out, since its managed launch needs no approval, and so is a
    never-pooled one, since no approval shares it.
    """
    from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
    from kiro_crew.mcp_utils import mcp_server_alias

    approvals = load_approvals(path)
    unpoolable = _unpoolable_stems()
    result: list[str] = []
    for name in dict.fromkeys(n for n in asked if isinstance(n, str) and n):
        stems = {target_stem(name), target_stem(mcp_server_alias(name))}
        if name in KIROCREW_BIN_MCP_SERVERS or stems & unpoolable:
            continue
        if not any(approvals.approved_pairs.get(stem) for stem in stems):
            result.append(name)
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
    # A never-pooled server is dropped whatever its approval says: an approval
    # or sidecar written before the name was listed must not let gatewayd spawn
    # it outside every chat sandbox. Not recorded as a refusal: nothing is
    # waiting on the person to approve it.
    denied = _unpoolable_stems()
    never: set[str] = set()
    for key, spec in target_env.items():
        stem = target_env_stem(key)
        if stem is None:
            kept[key] = spec
            continue
        if stem in denied:
            never.add(stem)
            continue
        try:
            parts = shlex.split(spec)
        except ValueError:
            parts = []
        if parts and approvals.admits_command(stem, hash_command(parts[0], parts[1:])):
            kept[key] = spec
            continue
        dropped.add(stem)
        # A launch this pass already refused carries its own display in the
        # refusal record. Counting it again here would leave that record with
        # nothing approvable, though it shows the whole launch.
        if parts and not approvals.saw_command(stem, hash_command(parts[0], parts[1:])):
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
    return kept, sorted(dropped | never)


def _unpoolable_stems() -> frozenset[str]:
    """Target stems of the servers the rewriter never pools.

    Imported lazily: the rewriter imports this module, so a module-level import
    would be a cycle.
    """
    from kiro_crew.mcp_gateway import rewriter_spec
    from kiro_crew.mcp_utils import mcp_server_alias

    names = rewriter_spec.UNPOOLABLE_SERVERS
    return frozenset(target_stem(n) for n in names) | frozenset(
        target_stem(mcp_server_alias(n)) for n in names
    )


def launch_approved(server_name: str, command_hash: str, derived_env_hash: str) -> bool:
    """gatewayd check for one exact command/effective-environment pair."""
    approvals = load_approvals()
    return approvals.admits_launch(target_stem(server_name), command_hash, derived_env_hash)
