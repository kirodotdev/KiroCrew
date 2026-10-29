"""The dashboard's tailnet sub-section: ``TailscaleConfig`` and the parser that builds it.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kiro_crew.config.fields import _meta, _safe_bool, _safe_dict
from kiro_crew.config.resolution import _OBSERVED_DEGRADED_SECTIONS, DEGRADED_TAILSCALE
from kiro_crew.config.sections_defaults import SectionReader

logger = logging.getLogger("kiro_crew.config.loader")


@dataclass
class TailscaleConfig:
    """Tailnet access for the dashboard (RFC: rfc-tailnet-dashboard-access)."""

    enabled: bool = field(
        default=False,
        metadata=_meta(
            "Tailnet Access",
            "Accept this machine's own MagicDNS name as a dashboard origin, so "
            "`tailscale serve` works without hand-writing dashboard.url. Reads "
            "the local Tailscale daemon once at startup; contributes nothing if "
            "Tailscale is absent, stopped, or MagicDNS is off. Does NOT widen the "
            "network bind and does NOT change authentication — every request "
            "still needs a dashboard session.",
            restart=True,
        ),
    )
    trust_identity: bool = field(
        default=False,
        metadata=_meta(
            "Trust Tailnet Identity",
            "Pin dashboard sessions arriving via `tailscale serve` to the "
            "daemon-verified tailnet peer instead of the tunnel's shared "
            "loopback address, and record that identity in the audit trail. "
            "Explicit opt-in, never inferred, and requires a non-empty "
            "allowed_logins — enabling it with an empty allowlist forces it off "
            "at load and logs an error (the rest of the config still loads). "
            "Every failure to verify a peer falls back to the ordinary "
            "token path. Takes effect on the next gateway start (the trust "
            "settings are read once at startup).",
            restart=True,
        ),
    )
    allowed_logins: list[str] = field(
        default_factory=list,
        metadata=_meta(
            "Allowed Tailnet Logins",
            "Not where you add people. Shared Crew works out who may open the "
            "Crew over Tailscale (you, accepted Crew invites, people you approve) "
            "and reads this list at the first start as its starting point. Adding "
            "a login later admits nobody; removing one removes that person at the "
            "next start. It must still "
            "be non-empty for trust_identity: a shared tailnet can have hundreds "
            "of members, so identity trust without one would hand each of them "
            "the dashboard.",
            restart=True,
        ),
    )
    pin_scope: str = field(
        default="node",
        metadata=_meta(
            "Pin Scope",
            "What an identity-pinned session binds to: 'node' (default — a "
            "leaked cookie is usable only from the original device) or 'login' "
            "(usable from any device carrying that Tailscale identity). An "
            "unrecognised value falls back to 'node'. An ACL-tagged node is "
            "always pinned at node scope regardless of this setting. Takes "
            "effect on the next gateway start.",
            restart=True,
        ),
    )
    bind_refresh_chains: bool = field(
        default=True,
        metadata=_meta(
            "Bind Refresh Chains To The Device",
            "Bind a session's 30-day refresh chain to the tailnet peer that "
            "opened it, so a stolen refresh cookie cannot be replayed from a "
            "different allowed device. On by default. Only turn it off if you "
            "need one session to roam between your DEVICES while pin_scope is "
            "'node' — with pin_scope 'login' the pin is your identity, not the "
            "device, so roaming already works. Turning it off means a stolen "
            "refresh cookie renews from any allowed node. Existing chains keep "
            "the binding they were opened with; the change applies to sessions "
            "started after the next gateway start.",
            restart=True,
        ),
    )
    keep_awake: bool = field(
        default=True,
        metadata=_meta(
            "Keep Awake While Published",
            "Keep this machine's SYSTEM awake while the dashboard is published "
            "on the tailnet, so a phone does not lose the dashboard when the "
            "laptop idles. The display is still allowed to sleep. Publishing is "
            "the opt-in — this exists to opt back OUT of the awake half without "
            "unpublishing. Independent of dashboard.prevent_sleep, which keeps "
            "the host awake only while a turn is in flight.",
        ),
    )


def effective_pin_scope(raw: object) -> str:
    """The pin scope the loader applies for a stored *raw* value.

    Case and surrounding space are ignored, an empty value is the default
    ``node``, and anything else unrecognised falls back to ``node`` (the
    narrower scope).
    """
    value = str(raw or "node").strip().lower()
    return value if value in ("node", "login") else "node"


def _tailscale_config_from(
    raw: object,
    degraded: set[str] | None = None,
    *,
    key_present: bool = False,
) -> TailscaleConfig:
    """Build the validated :class:`TailscaleConfig` (RFC §3/§3.1 load rules).

    Two rules, both narrowing-only so a typo can never widen access:

    * ``trust_identity: true`` with an empty ``allowed_logins`` is a
      configuration error — refused with a logged reason, identity trust stays
      OFF. Never a silently-permissive default: "any tailnet member" on a
      shared corporate tailnet would hand the dashboard to all of them.
    * An unrecognised ``pin_scope`` falls back to ``"node"`` (the narrower
      scope) with a logged warning — never to ``"login"``.

    Both rules resolve to a *narrower* value, which is right for an operator
    typo and wrong for a value that was LOST: ``allowed_logins`` is the only
    restriction on which tailnet peer may authenticate, so losing it resolves
    to "identity trust off", i.e. no login restriction at all. Absent is
    genuinely unconfigured; MALFORMED is the operator having asked for a
    restriction this load cannot read, and it is recorded in *degraded* under
    :data:`DEGRADED_TAILSCALE` so the gate can deny instead of admitting every
    tailnet peer (the shape that reopens the publish allowlist).

    ``key_present`` separates the two states a bare value cannot: a MISSING
    ``tailscale`` key and one written as JSON ``null`` both arrive here as
    ``None``. Only the second is the operator having written something, so only
    it degrades -- reading ``None`` alone as malformed would deny every install
    that simply has no tailscale section. Callers that do not know pass nothing
    and get the absent reading, which is what the direct-value tests rely on.
    """
    if (key_present and raw is None) or (raw is not None and not isinstance(raw, dict)):
        # Reached only because "dashboard.tailscale" is a fail-closed path in
        # config/validation.py; without that entry the malformed value is
        # repaired to the default before this runs and there is nothing to see.
        # An explicit null counts: the operator had to write the key to produce
        # it, which is the absent-versus-malformed line this whole fix turns on.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale' is not a JSON object (got %s) — the "
            "tailnet login allowlist is unknown, so tailnet peers are DENIED "
            "until the file is fixed and the gateway restarted",
            type(raw).__name__,
        )
    data = _safe_dict(raw)
    section = SectionReader(TailscaleConfig, data)
    enabled = section.read("enabled", _safe_bool)
    trust_identity = section.read("trust_identity", _safe_bool)
    if "trust_identity" in data and not isinstance(data.get("trust_identity"), bool):
        # The same class as the allowlist itself, one field over, and the field
        # is the restriction's own ON switch -- so it is the most permissive
        # default in the section. ``_safe_bool`` returns the default for
        # anything non-boolean, and that default is False, so `"true"` (a
        # quoted boolean, the commonest hand-edit slip) or `1` reads as "the
        # operator never asked for identity trust" and the perfectly valid
        # allowlist beside it stops being enforced.
        #
        # Recording it enforces the allowlist AS WRITTEN rather than denying
        # everyone: the entries parsed from a readable file are kept, so the
        # operator's own login still works and every peer they did not name is
        # refused. That is the closest honest reading of a config whose intent
        # to enable was garbled but whose list of who to admit was not.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale.trust_identity' is not a boolean (got "
            "%s) — it is the switch for the tailnet login allowlist, so the "
            "allowlist is enforced as written and every peer it does not name "
            "is DENIED until the file is fixed and the gateway restarted",
            type(data.get("trust_identity")).__name__,
        )
    raw_logins = data.get("allowed_logins")
    # The allowlist is only ever CONSULTED when identity trust is on, so a
    # malformed value in it loses nothing when the operator cleanly said off (or
    # never said on). Recording a degradation there would turn a typo in an
    # inert field into a forwarded-tailnet lockout, against a config that -- read
    # correctly -- permits those peers. A malformed FLAG is different: intent is
    # unknown, so the allowlist has to be treated as live.
    #
    # Presence is tested with ``in`` rather than ``is not None`` throughout: a
    # key written as JSON null is the operator having written something
    # unusable, not having left it out, and only the second is consent.
    _allowlist_is_live = trust_identity or (
        "trust_identity" in data and not isinstance(data.get("trust_identity"), bool)
    )
    if _allowlist_is_live and "allowed_logins" in data and not isinstance(raw_logins, list):
        # Same class one level down, and reachable WITHOUT a registry entry:
        # a three-segment path is past _apply_field_default's depth cap, so the
        # malformed value survives validation already. Recorded rather than
        # merely logged, because the log line below only fires when
        # trust_identity happens to be readable AND true — a config whose
        # trust_identity was lost in the same edit would say nothing at all.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale.allowed_logins' is not a list (got "
            "%s) — the tailnet login allowlist is unknown, so tailnet peers "
            "are DENIED until the file is fixed and the gateway restarted",
            type(raw_logins).__name__,
        )
    elif (
        _allowlist_is_live
        and isinstance(raw_logins, list)
        and any(not (isinstance(entry, str) and entry.strip()) for entry in raw_logins)
    ):
        # A LIST whose entries are not usable logins, e.g. [1] or ["a@b", None].
        # The comprehension below silently drops them, so an all-invalid
        # narrowing parses to [] — indistinguishable from "no restriction
        # configured", which is the exact silent widening this branch exists to
        # stop, and the same entry-level shape publish.allowed_destinations
        # already handles. An EMPTY list is NOT this case: that is a readable,
        # if mistaken, statement, and the trust_identity rule below already
        # refuses it with its own error.
        #
        # Unlike publish, the surviving entries are KEPT rather than zeroed.
        # The publish gate denies one whole action, so a partial allowlist
        # there has nowhere safe to land; this gate decides per peer, so
        # keeping the parseable logins narrows access to exactly what the
        # operator demonstrably wrote, while the degradation record still
        # denies every peer they did not name. Zeroing would instead lock out
        # the administrator whose own login parsed fine — a self-inflicted
        # outage in the middle of a config repair.
        if degraded is not None:
            degraded.add(DEGRADED_TAILSCALE)
        _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_TAILSCALE)
        logger.warning(
            "config: 'dashboard.tailscale.allowed_logins' carries entr(y/ies) "
            "that are not non-empty strings — the tailnet login allowlist is "
            "not what was written, so any peer it does not name is DENIED "
            "until the file is fixed and the gateway restarted",
        )
    allowed_logins = (
        [entry.strip() for entry in raw_logins if isinstance(entry, str) and entry.strip()]
        if isinstance(raw_logins, list)
        else section.default("allowed_logins")
    )
    raw_pin_scope = str(data.get("pin_scope") or section.default("pin_scope")).strip().lower()
    pin_scope = effective_pin_scope(raw_pin_scope)
    if pin_scope != raw_pin_scope:
        logger.warning(
            "dashboard.tailscale.pin_scope %r is not recognised; falling back to "
            "'node' (the narrower scope)",
            raw_pin_scope,
        )
    if trust_identity and not allowed_logins:
        logger.error(
            "dashboard.tailscale.trust_identity is on but allowed_logins is "
            "empty — identity trust requires an explicit login allowlist and "
            "stays OFF. Add the Tailscale logins you want to admit."
        )
        trust_identity = False
    return TailscaleConfig(
        enabled=enabled,
        trust_identity=trust_identity,
        allowed_logins=allowed_logins,
        pin_scope=pin_scope,
        # Defaults TRUE, and a non-boolean resolves to TRUE as well: this is a
        # narrowing-only field like the two rules above, so an operator typo may
        # only ever leave the binding ON, never silently reopen the replay path
        # the binding closes.
        bind_refresh_chains=_safe_bool(section.get("bind_refresh_chains"), True),
        keep_awake=section.read("keep_awake", _safe_bool),
    )
