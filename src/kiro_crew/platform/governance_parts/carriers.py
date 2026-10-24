"""What a parsed security policy and profile hold.

``GovernanceCeiling`` (level 1) and ``Profile`` (level 2), with the policy-only blocks a
ceiling carries: ``BootControls``, ``UpdatePins`` (``active_update_pins``),
``PolicyDistribution`` (``active_policy_distribution``) and the ``capabilities.agentcore``
posture, plus the signature states the loader proves, ``deny_all_profile`` and
``compose_profiles`` (a child profile that ``extends`` a parent). A new field of the
ceiling or of a profile goes here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import os
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_parts.archetypes import (
    MODE_ALLOW,
    CapabilityGate,
    RulesetLike,
    ScopedMap,
    ScopedRuleset,
    _compose_controls,
    _dedup,
    _reject_unknown_keys,
)
from kiro_crew.platform.governance_parts.catalog import (
    _SCOPE_ALIASES,
    CAPABILITY,
    COMMANDS_SCOPE,
    RULESET,
    SCOPE_CATALOG,
)

logger = logging.getLogger("kiro_crew.platform.governance")


# Duplicated from ``policy_distribution.POLICY_URL_ENV`` on purpose.  This module
# is the trust root and must not import the fetch engine at module load — that
# would pull urllib onto every import of the governance evaluator, and the engine
# imports back from here.  Naming the variable is not a behaviour, so the copy has
# nothing to drift into; ``test_governance_distribution.py`` pins them equal.
#: Ceiling on any ``distribution`` duration, in seconds. ``threading.TIMEOUT_MAX`` is what the
#: platform can wait for, and both consumers of these values hit it: ``Event.wait`` in the
#: refresher and the socket timeout in a fetch each raise OverflowError above it. ~292 years, so
#: it bounds typos rather than intentions.
MAX_DURATION_SECS = threading.TIMEOUT_MAX

_POLICY_DISTRIBUTION_URL_ENV = "KIROCREW_POLICY_URL"

#: Named here only so the declared-source refusal can point at it; the engine owns it.
_POLICY_DISTRIBUTION_HEADERS_ENV = "KIROCREW_POLICY_HEADERS"


# Provenance of the loaded ceiling — what the loader actually PROVED, not what the
# document claims.  Reported verbatim by ``kirocrew policy show`` so an operator
# can tell an established issuer from a decorative one.
SIGNATURE_VERIFIED = "verified"  # signature present AND checked against a trust key

SIGNATURE_UNVERIFIED = "unverified"  # signature present but not proven (no key / bad sig)

SIGNATURE_UNSIGNED = "unsigned"  # no identity.signature in the document

SIGNATURE_UNCHECKED = "unchecked"  # parsed outside the loader (no trust root consulted)


# ──────────────────────────────────────────────────────────────────────────
# Ceiling + Profile carriers
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BootControls:
    """Policy-only boot constraints (not a governed scope; checked at startup)."""

    require_sandbox: bool = True
    allow_terminal: bool = False
    fail_closed: bool = True


def _version_tuple(value: str) -> Tuple[int, ...]:
    """Parse a version's numeric core into a comparable tuple.

    The pre-release/build suffix is split off the WHOLE string before splitting
    on dots, because this project's CI stamps a dot INSIDE the pre-release
    (``0.2.0-nightly.20260728t184500``, ``0.3.0-insider.2``) — splitting
    per-component would leave ``nightly.20260728t184500`` as its own component
    and read every nightly as version ``0``.
    """
    core = re.split(r"[-+]", str(value).strip(), maxsplit=1)[0]
    try:
        return tuple(int(chunk) for chunk in core.split("."))
    except ValueError:
        return ()


@dataclass(frozen=True)
class UpdatePins:
    """Policy-only update pins: WHERE new code comes from, and the MINIMUM version.

    **Not a governed scope, deliberately.** Every archetype answers "is X
    permitted?"; a remote URL and a version number are *values the core
    consumes*, not permission decisions. So they ride outside ``controls`` — no
    ``SCOPE_CATALOG`` row, no matcher, no evaluator change, no new archetype.

    Read from the trust-root ``security_policy.json``, which sits in
    ``security._SENSITIVE_HOME_DIRS`` — that keystone (the agent can neither read
    nor write its own ceiling) is what makes these enterprise-*pinnable* where a
    ``config.json`` field or an env var would only be a suggestion.

    **Policy-only: rejected in a Level-2 profile** (see :func:`parse_profile`).
    Profiles are narrow-only, and there is no "narrower" version of pointing
    somewhere else — a per-app profile that could redirect the update source
    would be privilege escalation.
    """

    # The git remote new code may come from, as an fnmatch glob so one pin can
    # cover a mirror set. Empty = unpinned (the standalone default: any remote).
    source: str = ""
    # The minimum version this fleet may run. Empty = no floor.
    min_version: str = ""
    # The operator-authored shell commands for the command update provider. Their
    # PRESENCE is what enables the provider (there is no mechanism selector): when
    # either is set, resolve_provider() returns a CommandProvider. They live HERE
    # (in the keystone-protected security_policy.json the agent cannot write),
    # never in config.json, because a command provider executes unsandboxed code
    # as the gateway — placing its commands in an agent-writable file would let a
    # prompt-injected agent run arbitrary code. config.json has no path into this
    # seam at all.
    check_command: str = ""
    apply_command: str = ""
    # Per-platform command overrides, keyed by "{sys.platform}-{machine}"
    # (e.g. "linux-x86_64", "darwin-arm64"). Falls back to the top-level
    # check_command/apply_command when the current platform has no override.
    platform_commands: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def permits_source(self, url: str) -> bool:
        """Does *url* satisfy the source pin? An empty pin permits anything.

        An empty *url* (the checkout has no resolvable remote) DENIES when a pin
        exists: an admin's pin must not be satisfied by "we could not tell". So
        does a ``.``/``..`` component — ``*`` spans separators, so
        ``/srv/approved/../evil.git`` would otherwise match ``/srv/approved/*``.

        ``fnmatchcase``, not ``fnmatch``: the latter normcases via
        ``os.path.normcase``, a no-op on POSIX but lowercasing on Windows — the
        same pin must not be case-sensitive on one OS and not the other.
        """
        if not self.source:
            return True
        candidate = url.strip()
        if not candidate or any(
            part in (".", "..")
            for part in candidate.replace("\\", "/").split(
                "/",
            )
        ):
            return False
        return fnmatch.fnmatchcase(candidate, self.source.strip())

    def meets_min_version(self, version: str) -> bool:
        """Is *version* at or above the floor? An empty floor is always met.

        An unparseable floor imposes none (a typo must not brick a fleet); an
        unparseable *version* is treated as below the floor, which makes the host
        take the update rather than sit on a build it cannot identify.
        """
        floor = _version_tuple(self.min_version) if self.min_version else ()
        if not floor:
            return True
        current = _version_tuple(version)
        if not current:
            return False
        width = max(len(current), len(floor))
        return current + (0,) * (width - len(current)) >= floor + (0,) * (width - len(floor))

    @staticmethod
    def from_dict(d: Mapping[str, object]) -> "UpdatePins":
        _reject_unknown_keys(
            d,
            {
                "source",
                "min_version",
                "check_command",
                "apply_command",
                "platform_commands",
            },
            "updates",
        )
        for key in ("source", "min_version", "check_command", "apply_command"):
            # `str(raw or "")` would coerce `"source": false` to "" — silently
            # UNPINNING a policy that plainly meant to pin. Fail closed instead.
            if d.get(key) is not None and not isinstance(d[key], str):
                raise PlatformCompositionError(f"updates.{key} must be a string")
        raw_platform = d.get("platform_commands")
        platform_commands: dict[str, dict[str, str]] = {}
        if raw_platform is not None:
            if not isinstance(raw_platform, Mapping):
                raise PlatformCompositionError("updates.platform_commands must be a mapping")
            for plat_key, plat_val in raw_platform.items():
                if not isinstance(plat_key, str) or not isinstance(plat_val, Mapping):
                    raise PlatformCompositionError(
                        "updates.platform_commands entries must map a platform string "
                        "to a command mapping"
                    )
                entry: dict[str, str] = {}
                for cmd_key, cmd_val in plat_val.items():
                    if cmd_key not in ("check_command", "apply_command"):
                        raise PlatformCompositionError(
                            f"updates.platform_commands.{plat_key} has unknown key {cmd_key!r}"
                        )
                    if not isinstance(cmd_val, str):
                        raise PlatformCompositionError(
                            f"updates.platform_commands.{plat_key}.{cmd_key} must be a string"
                        )
                    # Stripped like source/min_version above: a whitespace-only
                    # command is truthy, so it would pass the presence check that
                    # SELECTS the provider, and `sh -c "   "` exits 0. That reads
                    # as a successful update, so the gateway restarts, the version
                    # has not changed, the check still reports an update, and the
                    # unattended path loops. Normalising here means absent and
                    # blank are the same thing everywhere downstream.
                    entry[cmd_key] = cmd_val.strip()
                platform_commands[plat_key] = entry
        return UpdatePins(
            source=str(d.get("source") or "").strip(),
            min_version=str(d.get("min_version") or "").strip(),
            check_command=str(d.get("check_command") or "").strip(),
            apply_command=str(d.get("apply_command") or "").strip(),
            platform_commands=platform_commands,
        )


def active_update_pins() -> UpdatePins:
    """The boot-frozen ceiling's update pins — empty (unpinned) when ungoverned.

    The single read point for the three update call sites, so they cannot drift.
    Returns unpinned on any error: a governance glitch must not block an update,
    because refusing one strands a host on a build that may need a patch.
    """
    try:
        from kiro_crew.platform.context import current_context

        pins = getattr(current_context().governance, "updates", None)
    except Exception:
        logger.debug("update pins unavailable; treating as unpinned", exc_info=True)
        return UpdatePins()
    return pins if isinstance(pins, UpdatePins) else UpdatePins()


# ``distribution.on_unavailable`` dispositions.  Two values, because there are only
# two questions: may this host run when the central ceiling cannot be established,
# and may it run on a cache older than the fleet's staleness bound.
UNAVAILABLE_FAIL_CLOSED = "fail_closed"  # abort boot / refuse the stale cache

UNAVAILABLE_DEGRADE = "degrade"  # fall through to the next tier / serve stale + audit

_UNAVAILABLE_DISPOSITIONS = frozenset({UNAVAILABLE_FAIL_CLOSED, UNAVAILABLE_DEGRADE})

#: Floor on ``refresh_interval_secs``.  A central endpoint serves the whole fleet,
#: so a typo'd `1` would turn every host into a polling loop against the admin's
#: own control plane.  A value below this is raised to it (with a warning) rather
#: than rejected: refusing would brick a fleet over a number that has a safe
#: reading, which is the opposite of what a distribution channel is for.
MIN_REFRESH_INTERVAL_SECS = 60

#: Default per-request timeout when the policy names none.  Bounded because this
#: runs on the boot path on a cold cache: an endpoint that accepts a connection
#: and never answers must not hang startup indefinitely.
DEFAULT_FETCH_TIMEOUT_SECS = 10.0

#: Hard ceiling on a fetched document.  A hostile or misconfigured endpoint must
#: not be able to OOM boot by answering with an unbounded body.
MAX_POLICY_BYTES = 1 << 20  # 1 MiB


def _is_enterprise() -> bool:
    """``kiro_crew.edition.is_enterprise()``, read at call time.

    Imported here rather than at module scope so the facade family binds no new project
    module name (``test_governance_parts_facade`` keeps that set closed).
    """
    from kiro_crew.edition import is_enterprise

    return is_enterprise()


@dataclass(frozen=True)
class PolicyDistribution:
    """Policy-only central-distribution pins: WHERE the ceiling itself comes from.

    This is how an enterprise IT admin owns one document and has every machine in
    the fleet follow it: the admin publishes ``security_policy.json`` to a central
    location, and each host fetches it, caches the last-known-good copy, and
    re-fetches on an interval so a pushed change lands without a restart or a
    redeploy.  ``kiro_crew.platform.policy_distribution`` is the engine; this
    dataclass is only the parsed declaration.

    **Not a governed scope**, for the same reason :class:`UpdatePins` is not: every
    archetype answers "is X permitted?", while a URL and an interval are *values
    the core consumes*.  So it rides outside ``controls`` — no ``SCOPE_CATALOG``
    row, no matcher, no evaluator change.

    **Policy-only: rejected in a Level-2 profile** (see :func:`parse_profile`).  A
    profile is narrow-only and there is no narrower version of pointing somewhere
    else.  A per-app profile that could redirect where the ceiling is fetched from
    would not be a narrowing — it would be a total replacement of the enforcement
    document, which is the widest escalation in this model.

    **Provenance is not configured here either.**  There is no
    ``require_signature`` key: a document must not be the authority on whether it has
    to be authentic, which is the reason ``_policy_trust_settings`` already gives for
    keeping the flag out of ``security_policy.json`` — "an attacker rewriting the
    policy would simply clear it".  Mandating a verified signature is
    ``require_policy_signature`` in the operator-controlled admission policy, which is
    on the keystone and which a fetched document cannot reach; this tier honours it
    like every other tier.

    **No credentials live here.**  ``headers`` is deliberately absent: a document
    published to the whole fleet must not carry a per-machine secret, and this one
    is additionally copied into a local cache and reported on by the read-only
    policy viewer.  A request credential is per-machine configuration and comes
    from the ``KIROCREW_POLICY_HEADERS`` environment variable instead — the same
    channel that already carries ``KIROCREW_SECURITY_POLICY``.
    """

    #: The URL the ceiling is fetched from.  Empty = no central distribution (the
    #: default, so an existing policy is byte-identical in behaviour).  The scheme
    #: must be one a fetcher is registered for; the built-ins are ``https``,
    #: ``file``, and ``http`` restricted to loopback hosts.
    source: str = ""
    #: Seconds between background re-fetches.  0 = fetch at boot only, which is
    #: still centrally-managed but not "on the fly".  Raised to
    #: :data:`MIN_REFRESH_INTERVAL_SECS` when set lower.
    refresh_interval_secs: int = 0
    #: Per-request timeout.  0 = :data:`DEFAULT_FETCH_TIMEOUT_SECS`.
    timeout_secs: float = 0.0
    #: How old the cached copy may be before it stops being an acceptable answer.
    #: 0 = no bound (a reachable-once host keeps running forever on that copy).
    #: A positive value is the fleet's staleness ceiling: past it, the disposition
    #: below decides whether the host refuses to run or runs and reports.
    max_cache_age_secs: int = 0
    #: What to do when the central ceiling cannot be established — a cold cache and
    #: an unreachable source, or a cache past ``max_cache_age_secs``.
    #: :data:`UNAVAILABLE_FAIL_CLOSED` (the default) refuses; a fleet that pointed
    #: a host at a central ceiling meant that ceiling to bind, so "we could not
    #: tell" must not read as "run unbounded".  :data:`UNAVAILABLE_DEGRADE` falls
    #: through to the next precedence tier instead, recording a governance
    #: incident — for a fleet that would rather have a working host it can see is
    #: degraded than a host that will not start.
    on_unavailable: str = UNAVAILABLE_FAIL_CLOSED
    #: Was a ``distribution`` block present in the document at all?  Set by
    #: :meth:`from_dict`; excluded from equality so a block that spells out the
    #: defaults still compares equal to the defaults and a ceiling that composes
    #: alone is still returned as the same object.  Read through :attr:`declared`.
    explicit: bool = field(default=False, compare=False, repr=False)

    @property
    def enabled(self) -> bool:
        """Is central distribution configured at all?"""
        return bool(self.source)

    @property
    def declared(self) -> bool:
        """Did the tier that produced this value express distribution AT ALL?

        Distinct from :attr:`enabled`, which asks only whether a source is set.  A
        block that pins the cadence and names no address still declares that the tier
        which carried it owns the channel, and :func:`compose_tier_ladder` needs that
        question: a lower tier may supply the pins only when NO tier above it declared
        any, so "declared something" and "declared a source" are different questions.

        A block that is PRESENT counts as declared even when every value it names is
        the default: a central document that spells out ``on_unavailable: fail_closed``
        has expressed a choice, and reading it as "no choice" would let a lower tier's
        ``degrade`` replace it -- the loosening this ladder exists to refuse.  The
        value comparison stays as a second route so a field added to this dataclass
        later is covered without editing a hand-maintained list.
        """
        return self.explicit or self != PolicyDistribution()

    def effective_timeout(self) -> float:
        return self.timeout_secs if self.timeout_secs > 0 else DEFAULT_FETCH_TIMEOUT_SECS

    def effective_refresh_interval(self) -> int:
        """The refresh interval actually used, clamped to the polling floor."""
        if self.refresh_interval_secs <= 0:
            return 0
        return max(self.refresh_interval_secs, MIN_REFRESH_INTERVAL_SECS)

    def cache_too_old(self, age_secs: float) -> bool:
        """Has a cached copy of *age_secs* exceeded the fleet's staleness bound?

        A negative age (a clock that moved backwards between the write and the
        read) reads as fresh rather than as infinitely stale: a host must not
        refuse to boot because NTP stepped its clock.
        """
        if self.max_cache_age_secs <= 0:
            return False
        return age_secs > self.max_cache_age_secs

    @staticmethod
    def from_dict(d: Mapping[str, object]) -> "PolicyDistribution":
        _reject_unknown_keys(
            d,
            {
                "source",
                "refresh_interval_secs",
                "timeout_secs",
                "max_cache_age_secs",
                "on_unavailable",
            },
            "distribution",
        )
        raw_source = d.get("source")
        if raw_source is not None and not isinstance(raw_source, str):
            # `str(raw or "")` would coerce `"source": false` to "", silently
            # DISABLING central distribution on a policy that plainly meant to
            # configure it. Fail closed instead, exactly as ``updates`` does.
            raise PlatformCompositionError("distribution.source must be a string")
        source = str(raw_source or "").strip()
        # A DECLARED source may not carry a credential, and the two shapes that do are
        # userinfo and a query string. This block ends up in the policy cache VERBATIM --
        # the document has to be byte-identical for its signature to verify -- and that
        # file is readable by an app backend, which is arbitrary third-party code. So a
        # `https://user:pass@host/p.json` or a pre-signed `?X-Amz-Signature=...` placed
        # here would be published to every host and then handed to every app.
        #
        # This is the rule the module docstring already states -- the per-machine request
        # credential travels in `KIROCREW_POLICY_HEADERS`, which "a published document must
        # not" carry -- made enforceable rather than advisory. The ENVIRONMENT channel is
        # deliberately unrestricted: that is where a pre-signed URL belongs, it is set by
        # whatever provisions the host, and it never lands in the document.
        if source:
            try:
                parsed = urllib.parse.urlsplit(source)
            except ValueError as exc:
                # ``urlsplit`` raises on a malformed bracketed host ("https://[::1"), and a
                # policy is CONFIGURATION: an operator who mistyped the address must get a
                # composition error naming the key, not an uncaught ValueError traceback out
                # of boot. Refusing here also keeps a source that cannot be parsed from
                # reaching the engine at all.
                raise PlatformCompositionError(
                    f"distribution.source is not a parseable URL: {exc}"
                ) from exc
            try:
                source.encode("utf-8")
            except UnicodeEncodeError as exc:
                # JSON can represent an unpaired surrogate even though UTF-8 cannot. The
                # cache identity hashes this text during boot, so reject it as configuration
                # here instead of leaking a UnicodeEncodeError from the distribution engine.
                raise PlatformCompositionError(
                    "distribution.source is not valid UTF-8 text"
                ) from exc
            # ``.port`` is a LAZILY parsed property, so the ``urlsplit`` guard above does not
            # cover it: it raises for a non-numeric or out-of-range port. A policy is
            # configuration, so that is a composition error naming the key, not a traceback.
            try:
                parsed.port
            except ValueError as exc:
                raise PlatformCompositionError(
                    f"distribution.source has an unusable port: {exc}"
                ) from exc
            offending = "userinfo" if (parsed.username or parsed.password) else ""
            if not offending and parsed.query:
                offending = "a query string"
            if offending:
                raise PlatformCompositionError(
                    f"distribution.source carries {offending}, which a published policy "
                    "must not: this document is cached verbatim and is readable by app "
                    f"backends. Put the address in {_POLICY_DISTRIBUTION_URL_ENV} and any "
                    f"credential in {_POLICY_DISTRIBUTION_HEADERS_ENV}, which stay "
                    "per-machine."
                )

        numbers: dict[str, float] = {}
        for key, allow_float in (
            ("refresh_interval_secs", False),
            ("timeout_secs", True),
            ("max_cache_age_secs", False),
        ):
            raw = d.get(key)
            if raw is None:
                numbers[key] = 0.0
                continue
            # bool is an int subclass, and `"refresh_interval_secs": true` would
            # otherwise parse as 1 second — a fleet-wide poll storm from a typo.
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise PlatformCompositionError(f"distribution.{key} must be a number")
            # NaN and infinity parse as JSON numbers in Python's decoder, and every
            # comparison below is FALSE for NaN — so it would slip past the range and
            # whole-number checks and then raise an uncaught ValueError at int().
            #
            # Only a FLOAT is asked: ``math.isfinite`` converts its argument to a float
            # first, so a JSON integer of 310 digits — which is a perfectly ordinary thing
            # for a typo to produce — raises OverflowError inside the very check meant to
            # reject it. An int has no non-finite values to screen for, and the range check
            # below handles it exactly.
            if isinstance(raw, float) and not math.isfinite(raw):
                raise PlatformCompositionError(f"distribution.{key} must be a finite number")
            if raw < 0:
                raise PlatformCompositionError(f"distribution.{key} must not be negative")
            if not allow_float and raw != int(raw):
                raise PlatformCompositionError(f"distribution.{key} must be a whole number")
            # Bounded by ``threading.TIMEOUT_MAX``, which is what the platform can actually
            # WAIT for: the refresher passes the interval to ``Event.wait`` and the fetch
            # passes the timeout to a socket, and both raise OverflowError above it —
            # silently killing the poller thread, so the host simply stops receiving policy
            # updates. Not an invented policy limit: it is ~292 years, so no duration anyone
            # means comes near it, and every value that does is a typo.
            if raw > MAX_DURATION_SECS:
                raise PlatformCompositionError(
                    f"distribution.{key} must not exceed {int(MAX_DURATION_SECS)} seconds "
                    "(the longest interval this platform can wait for)"
                )
            # Stored WITHOUT a float round-trip, for the same reason ``isfinite`` is not
            # asked of an int: ``float()`` on a 310-digit integer raises OverflowError, and
            # doing that here would move the crash three lines down rather than fix it.
            # Every consumer either takes ``int()`` of this or accepts a float, and an int
            # satisfies both.
            numbers[key] = raw

        raw_disposition = d.get("on_unavailable")
        if raw_disposition is None:
            disposition = UNAVAILABLE_FAIL_CLOSED
        elif not isinstance(raw_disposition, str):
            raise PlatformCompositionError("distribution.on_unavailable must be a string")
        else:
            disposition = raw_disposition.strip()
            if disposition not in _UNAVAILABLE_DISPOSITIONS:
                raise PlatformCompositionError(
                    f"distribution.on_unavailable {raw_disposition!r} must be one of "
                    f"{sorted(_UNAVAILABLE_DISPOSITIONS)}"
                )

        if (
            not source
            and not os.environ.get(_POLICY_DISTRIBUTION_URL_ENV, "").strip()
            # An enterprise build supplies the source itself (RFC D2), so a block that
            # only tunes the cadence or staleness bound is not inert there.
            and not _is_enterprise()
            and any(
                (
                    numbers["refresh_interval_secs"],
                    numbers["timeout_secs"],
                    numbers["max_cache_age_secs"],
                    raw_disposition is not None,
                )
            )
        ):
            # A block that tunes a fetch it never configures is a policy whose
            # author believed central distribution was on. Silently ignoring it is
            # how a fleet ends up ungoverned while its policy file reads as managed.
            #
            # Unless the ENVIRONMENT supplies the source, which is the ordinary split:
            # whatever provisions the host owns the address (and any credential in it),
            # while the fleet publishes the cadence and the staleness bound in the
            # document. Such a block is not inert at all, and refusing it here aborted
            # boot on exactly the configuration the two-channel design intends.
            raise PlatformCompositionError(
                "distribution declares settings but no 'source'; central policy "
                f"distribution would be inert (fail-closed). Set 'source', or "
                f"{_POLICY_DISTRIBUTION_URL_ENV} if the address is per-machine."
            )

        return PolicyDistribution(
            source=source,
            refresh_interval_secs=int(numbers["refresh_interval_secs"]),
            timeout_secs=numbers["timeout_secs"],
            max_cache_age_secs=int(numbers["max_cache_age_secs"]),
            on_unavailable=disposition,
            # Any key at all is a declaration, default-valued or not; the caller
            # passes ``{}`` for an absent block, which stays undeclared.
            explicit=bool(d),
        )


def active_policy_distribution() -> PolicyDistribution:
    """The installed ceiling's distribution pins — empty when ungoverned.

    The single read point so the refresher, the CLI and the policy viewer cannot
    drift.  Returns an unconfigured value on any error, and deliberately reads the
    INSTALLED context rather than resolving one: this is called from a background
    refresh thread, where composing a context as a side effect of asking "where do
    I fetch from" would be a boot-order surprise.
    """
    try:
        from kiro_crew.platform.context import installed_context

        ctx = installed_context()
        pins = getattr(ctx.governance, "distribution", None) if ctx is not None else None
    except Exception:
        logger.debug("policy distribution pins unavailable", exc_info=True)
        return PolicyDistribution()
    return pins if isinstance(pins, PolicyDistribution) else PolicyDistribution()


@dataclass(frozen=True)
class GovernanceCeiling:
    """Level 1 — the enterprise security ceiling, frozen at boot.

    ``controls`` maps a catalog scope name to its parsed archetype value.  A
    scope absent from the map is *not governed* by policy (truth-table
    "not-governed" rows).  This generic mapping is what lets the evaluator stay
    scope-name-agnostic.
    """

    version: int
    boot: BootControls
    controls: Mapping[str, object]
    identity_issuer: str = ""
    identity_signature: str = ""
    # Outcome of the load-time signature check (:func:`load_security_policy`).
    # ``parse_policy`` alone cannot fill this in — it has the document but not the
    # trust root — so a directly-parsed ceiling carries the honest
    # ``SIGNATURE_UNCHECKED``, never a flattering default.
    signature_state: str = "unchecked"
    # Policy-only update pins (outside ``controls`` — see UpdatePins).
    updates: UpdatePins = field(default_factory=UpdatePins)
    # Policy-only central-distribution pins (outside ``controls`` — see
    # PolicyDistribution). Where THIS document is fetched from, so an enterprise
    # admin owns one file and the fleet follows it.
    distribution: PolicyDistribution = field(default_factory=PolicyDistribution)
    # Optional operator-declared fallback profile (policy top-level ``fallback``).
    # When a per-surface profile FILE is unusable (unreadable, unparseable, or a
    # broken ``extends``), the loader substitutes THIS profile instead of the
    # most-restrictive deny-all. Absent (the default) keeps the deny-all fallback,
    # so an operator who declares nothing is unchanged (fail-closed). A declared
    # fallback is still intersected with this ceiling, so it can only ever narrow
    # it — it trades strict fail-closed for keeping the unlisted planes available.
    fallback_profile: "Optional[Profile]" = None
    # Policy-only composed posture for ``capabilities.agentcore``
    # (``workload`` | ``login``). Not a CapabilityGate field — that type stays
    # ``enabled`` + ``scopes``. Read through :func:`agentcore_posture`, which
    # returns ``None`` when the capability is off, omitted, or fail-closed
    # disabled. A profile cannot compose a different posture onto this field
    # (policy-wins).
    agentcore_identity_posture: Optional[str] = None
    # Policy-only Gateway MCP URL (``capabilities.agentcore.gateway_url``).
    # Empty when omitted or the capability is off. Not a CapabilityGate field.
    # A profile cannot carry this key. Read through
    # :func:`agentcore_gateway_url`.
    agentcore_gateway_url: str = ""
    # Policy-only workload identity name (``capabilities.agentcore.workload_name``).
    # Empty when omitted — runtime then uses env or a later default. A profile
    # cannot carry this key. Read through :func:`agentcore_workload_name`.
    agentcore_workload_name: str = ""
    # Which precedence tier produced this ceiling (one of the ``TIER_*`` names, or
    # "" for a ceiling parsed outside the loader).  Its one consumer is the tier
    # audit in ``compose_tier_ladder``, which names authority and subordinate in the
    # SEL when two tiers compose; nothing displays it to an operator yet.
    tier: str = ""

    def get(self, scope: str) -> Optional[object]:
        return self.controls.get(scope)

    def signature_summary(self) -> str:
        """One-line, operator-facing description of the ceiling's provenance.

        Exists so every surface (CLI, and any future viewer) renders the SAME
        vocabulary instead of each re-deriving "is this issuer meaningful?" from
        the raw fields — the mistake this state machine replaces was printing an
        issuer that no check had ever established.
        """
        if self.signature_state == SIGNATURE_VERIFIED:
            return f"signed and verified (issuer: {self.identity_issuer})"
        if self.signature_state == SIGNATURE_UNVERIFIED:
            issuer = self.identity_issuer or "unnamed issuer"
            return f"signed but UNVERIFIED — issuer {issuer!r} is unproven (advisory)"
        if self.signature_state == SIGNATURE_UNSIGNED:
            return "unsigned (integrity rests on filesystem permissions)"
        return "signature not checked (parsed without a trust root)"

    def pinned_command_patterns(self) -> Tuple[str, ...]:
        """Force-deny command patterns pinned by this ceiling's ``commands`` scope.

        Enterprise lock: these patterns are un-opt-out-able. hooks/security union
        them into the effective denied set so a user cannot disable a built-in
        rule the fleet policy pinned. Returns ``()`` when the policy does not
        govern ``commands`` or governs it in allow (allowlist) mode.
        """
        return _command_deny_patterns(self.controls.get(COMMANDS_SCOPE))


@dataclass(frozen=True)
class Bind:
    """What a profile applies to (discriminated)."""

    type: str  # surface | app | task
    id: str = ""


@dataclass(frozen=True)
class Profile:
    """Level 2 — a per-surface / per-app / per-task narrowing scope."""

    name: str
    bind: Optional[Bind] = None
    extends: str = ""
    controls: Mapping[str, object] = field(default_factory=dict)
    #: Capability-family keys this profile declared that THIS build does not know
    #: (see the key-open tolerance note in ``_parse_controls``). Diagnostic only —
    #: an unknown key governs nothing here, so it is recorded rather than enforced.
    #: Defaults to ``()`` so every existing constructor and ``replace()`` call keeps
    #: working unchanged.
    unknown_scopes: Tuple[str, ...] = ()

    def get(self, scope: str) -> Optional[object]:
        return self.controls.get(scope)

    def pinned_command_patterns(self) -> Tuple[str, ...]:
        """Force-deny command patterns pinned by this profile's ``commands`` scope."""
        return _command_deny_patterns(self.controls.get(COMMANDS_SCOPE))


def deny_all_profile(name: str = "_deny_all") -> Profile:
    """The most-restrictive built-in profile.

    An invalid profile falls back to this (Validation rule 5) — NOT to the
    policy ceiling.  Deny-all everywhere it can: every ruleset scope is an empty
    allow-set; every capability is disabled.
    """
    controls: Dict[str, object] = {
        scope: ScopedRuleset(mode=MODE_ALLOW, allow=(), matcher=spec.matcher)
        for scope, spec in SCOPE_CATALOG.items()
        # Skip alias scopes (folders.* → filesystem.*): the gate queries the
        # canonical target, so emitting the alias key too is dead config.
        if spec.kind == RULESET and scope not in _SCOPE_ALIASES
    }
    controls["channels"] = ScopedMap(members=ScopedRuleset(mode=MODE_ALLOW, allow=()))
    for scope, spec in SCOPE_CATALOG.items():
        if spec.kind == CAPABILITY:
            controls[scope] = CapabilityGate(enabled=False)
    return Profile(name=name, controls=controls)


def _command_deny_patterns(control: object) -> Tuple[str, ...]:
    """Extract force-deny command patterns from a parsed ``commands`` control.

    A DENY-mode set's ``deny`` tuple IS the set of force-pins: patterns that must
    remain denied regardless of user opt-out. An ALLOW-mode set is an allowlist
    (deny-by-default) enforced by ``gate_decision`` and projects no pattern to
    union. Ask the control (``force_deny_patterns``) rather than reading its
    attributes: a fold of two tiers is an ``_AndRuleset``, which holds its
    patterns in its halves, and an authority's pins bind through such a fold.

    The test is :class:`RulesetLike` conformance, not a list of the two shapes
    that satisfy it today: naming them would make any later archetype in this
    scope project no pin at all, which is the same empty projection this asks
    about. A control of another archetype (a capability gate, a scoped map)
    carries no pins to union and yields ``()`` with a debug log.
    """
    if isinstance(control, RulesetLike):
        pins = control.force_deny_patterns()
        if pins:
            return pins
    if control is not None:
        logger.debug("commands control %r yields no force-deny pins", type(control))
    return ()


_AGENTCORE_SCOPE = "capabilities.agentcore"

_AGENTCORE_POSTURES = frozenset({"workload", "login"})

_AGENTCORE_POLICY_ONLY = frozenset({"posture", "gateway_url", "workload_name"})


def _capability_raw_for_gate(
    scope: str, raw: Mapping[str, object], *, is_policy: bool
) -> Mapping[str, object]:
    """Drop inner policy data that is not a CapabilityGate field.

    ``capabilities.agentcore.posture``, ``gateway_url``, and
    ``workload_name`` are policy data, not a second scope and not an
    evaluator input. Strip them on a policy document so
    ``CapabilityGate.from_dict`` stays ``additionalProperties: false``.
    A profile (or policy fallback body) that carries either key is
    rejected — Rule 6, same fail-closed raise as ``ScopedMap.posture``.
    """
    if scope != _AGENTCORE_SCOPE:
        return raw
    extra = _AGENTCORE_POLICY_ONLY.intersection(raw)
    if not extra:
        return raw
    if not is_policy:
        name = "posture" if "posture" in extra else next(iter(extra))
        raise PlatformCompositionError(
            f"capabilities.agentcore.{name} is policy-only; not allowed on a profile"
        )
    return {key: value for key, value in raw.items() if key not in _AGENTCORE_POLICY_ONLY}


def _apply_agentcore_posture(
    data: Mapping[str, object],
    controls: Dict[str, object],
    boot: "BootControls",
) -> Optional[str]:
    """Validate agentcore posture and return the value to persist on the ceiling.

    Missing or unknown ``posture`` with ``enabled: true`` aborts when
    ``boot.fail_closed``; otherwise the row is treated as disabled. Disabled
    (or unnamed) rows do not require a posture and yield ``None``.
    """
    raw_caps = data.get("capabilities")
    if not isinstance(raw_caps, dict):
        return None
    raw = raw_caps.get("agentcore")
    if not isinstance(raw, dict):
        return None
    control = controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return None
    posture = raw.get("posture")
    if isinstance(posture, str) and posture in _AGENTCORE_POSTURES:
        return posture
    reason = (
        f"capabilities.agentcore is enabled but posture is {posture!r}; "
        "expected 'workload' or 'login'"
    )
    if boot.fail_closed:
        raise PlatformCompositionError(reason)
    controls[_AGENTCORE_SCOPE] = CapabilityGate(enabled=False, scopes=control.scopes)
    return None


def agentcore_posture(ceiling: Optional[GovernanceCeiling]) -> Optional[str]:
    """Return the policy posture if ``capabilities.agentcore`` is enabled.

    ``\"workload\"`` or ``\"login\"`` when the ceiling enables the capability
    with a known posture. ``None`` when there is no ceiling, the capability is
    omitted, disabled, or fail-closed-disabled. The single reader later work
    must use — do not re-parse raw policy JSON for this field.

    Always ``None`` in an enterprise build, which also empties the Gateway URL and
    workload name read through it (RFC enterprise edition, Red "Workload identity"
    and "Identity settings").
    """
    from kiro_crew.edition_locks import locked_off

    if ceiling is None or locked_off():
        return None
    stored = ceiling.agentcore_identity_posture
    if stored not in _AGENTCORE_POSTURES:
        return None
    control = ceiling.controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return None
    return stored


def _apply_agentcore_gateway_url(
    data: Mapping[str, object],
    controls: Dict[str, object],
    boot: "BootControls",
) -> str:
    """Validate and return the policy Gateway URL, or empty.

    Missing / empty is legal (the crew may still name a posture and add
    the URL later). A present but unusable value aborts when
    ``boot.fail_closed``; otherwise it is ignored.
    """
    from kiro_crew.platform.agentcore_schema import normalize_agentcore_gateway_url

    raw_caps = data.get("capabilities")
    if not isinstance(raw_caps, dict):
        return ""
    raw = raw_caps.get("agentcore")
    if not isinstance(raw, dict):
        return ""
    control = controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return ""
    value = raw.get("gateway_url")
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        reason = "capabilities.agentcore.gateway_url must be a string"
        if boot.fail_closed:
            raise PlatformCompositionError(reason)
        return ""
    try:
        return normalize_agentcore_gateway_url(value)
    except ValueError as exc:
        if boot.fail_closed:
            raise PlatformCompositionError(str(exc)) from exc
        return ""


def agentcore_gateway_url(ceiling: Optional[GovernanceCeiling]) -> str:
    """Return the policy Gateway URL if ``capabilities.agentcore`` is enabled."""
    if ceiling is None or agentcore_posture(ceiling) is None:
        return ""
    return str(ceiling.agentcore_gateway_url or "")


def _apply_agentcore_workload_name(
    data: Mapping[str, object],
    controls: Dict[str, object],
    boot: "BootControls",
) -> str:
    """Validate and return the policy workload name, or empty."""
    from kiro_crew.platform.agentcore_schema import normalize_agentcore_workload_name

    raw_caps = data.get("capabilities")
    if not isinstance(raw_caps, dict):
        return ""
    raw = raw_caps.get("agentcore")
    if not isinstance(raw, dict):
        return ""
    control = controls.get(_AGENTCORE_SCOPE)
    if not isinstance(control, CapabilityGate) or not control.enabled:
        return ""
    value = raw.get("workload_name")
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        reason = "capabilities.agentcore.workload_name must be a string"
        if boot.fail_closed:
            raise PlatformCompositionError(reason)
        return ""
    try:
        return normalize_agentcore_workload_name(value)
    except ValueError as exc:
        if boot.fail_closed:
            raise PlatformCompositionError(str(exc)) from exc
        return ""


def agentcore_workload_name(ceiling: Optional[GovernanceCeiling]) -> str:
    """Return the policy workload name if ``capabilities.agentcore`` is enabled."""
    if ceiling is None or agentcore_posture(ceiling) is None:
        return ""
    return str(ceiling.agentcore_workload_name or "")


def compose_profiles(parent: Profile, child: Profile) -> Profile:
    """Inheritance — a child profile that ``extends`` *parent* may only narrow.

    Produces a merged ``Profile`` whose every control is ``parent ∘ child``
    (allow∩, deny∪, ordinal=stricter, gate=AND).  Used when a profile declares
    ``extends`` (Validation rule 4, monotonic narrowing).  The child's bind/name
    win; controls present in only one parent carry through unchanged.
    """
    merged: Dict[str, object] = dict(parent.controls)
    for scope, c_control in child.controls.items():
        p_control = merged.get(scope)
        if p_control is None:
            merged[scope] = c_control
            continue
        merged[scope] = _compose_controls(p_control, c_control)
    # Union the diagnostic records so an ``extends`` chain does not lose the fact
    # that a link in it named a capability this build cannot govern.
    return Profile(
        name=child.name,
        bind=child.bind or parent.bind,
        extends="",
        controls=merged,
        unknown_scopes=_dedup(parent.unknown_scopes + child.unknown_scopes),
    )
