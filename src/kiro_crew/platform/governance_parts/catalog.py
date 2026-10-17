"""Which governed scopes exist, and which archetype each one is.

``SCOPE_CATALOG`` is the single place a scope name binds to an archetype, a matcher and a
scale; ``register_scope`` extends it, ``_SCOPE_ALIASES`` maps the profile's ``folders.*``
spelling onto ``filesystem.*``. A new governed scope is a row here.

Served through :mod:`kiro_crew.platform.governance`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping

from kiro_crew.platform.governance_parts.matching import (
    _DEFAULT_MATCHER,
    _MATCHERS,
    _ORDINAL_SCALES,
)

# ──────────────────────────────────────────────────────────────────────────
# Scope catalog — names → archetype kind + matcher/scale binding
# ──────────────────────────────────────────────────────────────────────────
# This is the ONLY place a scope name is associated with an archetype.  The
# evaluator iterates this catalog; it never hard-codes "if scope == 'tools'".
# Extending the model = adding a row here + (maybe) a matcher/scale entry.
RULESET = "ruleset"

ORDINAL = "ordinal"

CAPABILITY = "capability"

SCOPEDMAP = "scopedmap"

# SCOPE_CATALOG key whose deny-mode deny list is force-pinned (un-opt-out-able).
COMMANDS_SCOPE = "commands"


@dataclass(frozen=True)
class ScopeSpec:
    """Catalog entry describing how a named governed scope is parsed + composed."""

    kind: str
    matcher: str = _DEFAULT_MATCHER
    ordinal_scale: str = ""
    capability_default: bool = False  # see the CAPABILITY-DEFAULT CONTRACT note below
    # for CapabilityGate: scope-name -> matcher for its inner ScopedRulesets
    scope_matchers: Mapping[str, str] = field(default_factory=dict)
    # Identifiers this scope may never forbid, checked at PARSE time so a policy
    # that removes the floor is refused instead of booting into a state with no
    # usable option. Data rather than a scope-name branch in the parser: the
    # loader's contract is that registering a scope needs no loader edit, and a
    # second scope with a floor should be a catalog entry, not another `if`.
    always_permitted: tuple[str, ...] = ()


# ── CAPABILITY-DEFAULT CONTRACT (read before touching any capability_default) ──
#
# ``capability_default`` supplies ``enabled`` when the capability's KEY IS PRESENT
# in the document but its ``enabled`` field is omitted, e.g.
#
#     "capabilities": {"publish": {"scopes": {"destinations": {...}}}}
#
# which configures publish's destinations without stating whether publish is on,
# and therefore resolves to this default.  That is the ONLY case it covers.
#
# It does NOT apply when the key is absent.  ``_parse_controls`` materializes only
# the children physically present in the document, so an unnamed capability is an
# absent scope, and ``resolve`` PERMITS an absent scope (``_PERMIT_NOT_GOVERNED``)
# exactly as it does for an unnamed ``mcp``, ``commands`` or ``network.egress``.
# Omission means ungoverned-and-permitted for every archetype; capabilities are
# not a special case, and making them one would put a namespace-specific rule in
# a loader whose whole contract is that a newly registered scope family parses
# without a loader edit.
#
# Consequence for authors, and the reason a governed fleet should enumerate all of
# them: a policy cannot deny a capability by leaving it out.  ``kirocrew policy
# validate`` reports a partially-governed ``capabilities`` block so the gap is
# visible instead of implied.
#
# The absent-key semantics below are verified by executing parse_policy + resolve,
# not inferred from the comments.

# Built-in catalog.
#
# APPEND-ONLY CONTRACT: add a row, or retire one — never RENAME a scope in place.
# Two things depend on it. A policy naming a retired scope fails closed at boot, so
# a rename is a visible migration on the ceiling; and a PROFILE naming an
# unregistered ``capabilities.*`` child that declares ``enabled: true`` is
# deliberately TOLERATED (skipped + recorded on ``Profile.unknown_scopes``, see
# ``_parse_controls``) so a data home shared with an edition that registers extra
# rows still loads. A rename would land in that tolerant path and silently stop
# governing, instead of surfacing. (A renamed row declared ``enabled: false`` still
# fails closed — the asymmetry is documented on ``_parse_controls``.)
SCOPE_CATALOG: Dict[str, ScopeSpec] = {
    "tools": ScopeSpec(RULESET, matcher="identifier"),
    # Dashboard tool-approval modes. Today this scope governs exactly ONE mode:
    # ``yolo`` (auto-approve every tool everywhere), e.g.
    # ``{"approval_modes": {"mode": "deny", "deny": ["yolo"]}}``. An absent scope
    # permits every mode (unchanged behavior).
    #
    # ``always_permitted`` carries the three modes this scope may not forbid, for two
    # DIFFERENT reasons, both enforced at parse time so a policy author is told
    # rather than left with a control that silently does not hold:
    #
    # * ``normal`` is the interactive floor. Denying it would leave no selectable
    #   mode and brick tool approval, and the trust-root ``security_policy.json`` is
    #   the one file the dashboard may not rewrite to repair itself.
    # * ``trust`` and ``trust_reads`` are NOT YET GOVERNED. Their grants are honoured
    #   by consumption predicates this scope does not reach — the in-memory trusted
    #   set, and the session ``approval_policy`` a spawned subagent inherits — so
    #   accepting a deny for them would advertise enforcement that does not exist.
    #   Governing those read paths is tracked separately.
    "approval_modes": ScopeSpec(
        RULESET,
        matcher="identifier",
        always_permitted=("normal", "trust", "trust_reads"),
    ),
    "mcp": ScopeSpec(RULESET, matcher="mcp"),
    "apps": ScopeSpec(RULESET, matcher="identifier"),
    "commands": ScopeSpec(RULESET, matcher="command"),
    "filesystem.read": ScopeSpec(RULESET, matcher="path"),
    "filesystem.write": ScopeSpec(RULESET, matcher="path"),
    "folders.read": ScopeSpec(RULESET, matcher="path"),
    "folders.write": ScopeSpec(RULESET, matcher="path"),
    "network.egress": ScopeSpec(RULESET, matcher="host"),
    "channels": ScopeSpec(SCOPEDMAP),
    "approval_mode": ScopeSpec(ORDINAL, ordinal_scale="approval"),
    "sandbox.min_level": ScopeSpec(ORDINAL, ordinal_scale="sandbox"),
    # Admin control over the auto-approve (YOLO) durations a user may select.
    # Members are duration selections. Two are security-relevant: "permanent"
    # (the never-expiring grant an operator DECLARES via
    # ``agent.dangerouslySkipPermissions``, re-established every startup) and
    # "until_shutdown" (an ad-hoc grant with no timed expiry, cleared on restart).
    # Deny-by-default is open (standalone permits it), and an enterprise POLICY
    # can remove it, e.g.
    #   {"yolo_duration": {"mode": "deny", "deny": ["permanent", "until_shutdown"]}}
    # which forces a declared grant back onto the ordinary ad-hoc TTL. Consulted
    # by kiro_crew.safety_override at the activation seam, against the HOST
    # profile and fail-closed.
    "yolo_duration": ScopeSpec(RULESET, matcher="identifier"),
    # Which ACP harness a deployment may select (``agent.acp_backend``).
    #
    # Distinct from the selectable-backend REGISTRY in ``kiro_crew.acp_backends``:
    # that answers "what can this BUILD serve", which is a capability fact and is
    # not governable. This row answers "what may THIS DEPLOYMENT select", so a
    # managed fleet can qualify one harness and bound the rest.
    #
    # ADDITIVE over a floor -- the semantics the selectable-backend design settled on:
    #   {"agent_backend": {"mode": "allow", "allow": ["claude"]}}
    # means "ALSO allow claude", not "only claude" — ``kiro`` stays selectable
    # because ``acp_backends.GOVERNANCE_FLOOR_BACKEND`` is never submitted to this
    # scope at all. The alternative reading (exclusive) can empty the set, and an
    # install with no startable harness cannot be recovered from the dashboard,
    # since the trust-root policy is the one file it may not write.
    #
    # Members are POLICY ids, not the code's spelling: ``kiro`` / ``kas`` /
    # ``claude`` (see ``acp_backends.POLICY_ID_BY_BACKEND``) — the kiro backend is
    # the empty string internally, which no identifier matcher can carry.
    #
    # Consulted by ``kiro_crew.agent_backend_governance`` at exactly ONE place: it
    # recomputes the ``acp_backends`` registry, from ``bootstrap_context`` at boot and
    # from ``policy_distribution.apply_ceiling`` on every runtime ceiling install.
    # Deliberately NOT consulted at provider construction — harness-parity H13 forbids
    # the Kiro construction path gaining a conditional in service of an adapter, and a
    # test asserts ``create_provider_factory`` contains no governance call. The
    # existing single gate ``resolve_selected_backend`` reads the narrowed registry, so
    # it degrades a denied persisted value with no second check.
    "agent_backend": ScopeSpec(RULESET, matcher="identifier"),
    # Capabilities (registered defaults — see the CAPABILITY-DEFAULT CONTRACT above):
    "capabilities.spawn": ScopeSpec(
        CAPABILITY, capability_default=True, scope_matchers={"agents": "identifier"}
    ),
    "capabilities.memory_writes": ScopeSpec(CAPABILITY, capability_default=True),
    # Web browsing (the ``browser`` MCP tool driving the native panel, and the
    # playwright-cli fallback it points at) is a governable egress surface: an
    # enterprise policy that denies it must stop the agent browsing on the
    # native path too, not only the shell ``commands`` path playwright-cli sits
    # behind. Default True (on) — like memory_writes, an ordinary capability
    # that is available unless a policy explicitly sets
    # ``{"capabilities": {"browse": {"enabled": false}}}``.
    "capabilities.browse": ScopeSpec(CAPABILITY, capability_default=True),
    "capabilities.script_hooks": ScopeSpec(CAPABILITY, capability_default=False),
    "capabilities.cron": ScopeSpec(CAPABILITY, capability_default=False),
    "capabilities.messaging": ScopeSpec(CAPABILITY, capability_default=False),
    # Agent workload identity + Gateway MCP (opt-in, like messaging/publish).
    # Inner ``posture`` is policy data (``workload`` | ``login``), not a second
    # scope and not an evaluator input. An ``enabled: true`` document with a
    # missing or unknown posture fails closed — treated as disabled, or
    # boot-abort when ``boot.fail_closed``. Data row only; CONTRACT_VERSION
    # and the evaluator are untouched.
    "capabilities.agentcore": ScopeSpec(CAPABILITY, capability_default=False),
    # Publishing an artifact's bytes to an external destination is an
    # exfil/external-side-effect surface (like messaging), so it is opt-in
    # (capability_default=False): a policy that names ``publish`` while omitting
    # ``enabled`` — typically to configure its destinations — resolves to denied.
    # Note this does NOT make omission of the whole key deny: an unnamed publish
    # is ungoverned and permitted (see the CAPABILITY-DEFAULT CONTRACT above).
    # The inner ``destinations`` ruleset
    # bounds WHICH publish-provider ids are allowed once the capability is on
    # (the direct analogue of capabilities.spawn's ``agents`` ruleset).  WHO
    # implements a destination is the orthogonal CPP PublishRegistry seam; this
    # gate only decides WHETHER + to WHERE.  Not a lever over ``git push`` (deny
    # floor) or ``network.egress`` (fetch host) — those are separate planes.
    "capabilities.publish": ScopeSpec(
        CAPABILITY, capability_default=False, scope_matchers={"destinations": "identifier"}
    ),
    # Installed theme packs may ship a persona.md whose (consent-gated) text is
    # injected into the agent's FIRST turn — third-party text entering the
    # model's context (mirrors capabilities.publish being a governable external
    # surface). Making it a capability row lets an enterprise POLICY
    # force-disable persona injection wholesale. Default True: naming the row
    # without ``enabled`` keeps personas working (this is a tone-only surface; the deny floor +
    # PreToolUse gate still bind every action the persona could ask for), while
    # a governing policy can pin it off. Data row only — CONTRACT_VERSION and
    # the evaluator are untouched (per spec).
    "capabilities.theme_persona": ScopeSpec(CAPABILITY, capability_default=True),
    # Installing a theme pack fetches third-party content server-side (a local
    # dir move or a ``git clone`` from a remote URL), stores it, and serves its
    # sandboxed JS + assets into the operator's dashboard — a content-ingestion
    # surface that, like capabilities.publish, an enterprise POLICY must be able
    # to close. Making install a capability row lets a managed-fleet ceiling
    # ban pack installation wholesale (consulted in ``api_themes_install``).
    # Default True: naming the row without ``enabled`` keeps install working for the
    # standalone single-user owner; a governing policy can pin it off. Data row only —
    # CONTRACT_VERSION and the evaluator are untouched (mirrors theme_persona).
    "capabilities.theme_install": ScopeSpec(CAPABILITY, capability_default=True),
    # The anonymous heartbeat (``beacon.py``) and official-app install receipt
    # (``apps/install_receipt.py``) are the repo's only default-on egress family.
    # Both use fixed anonymous payloads and the same effective consent gate. A
    # managed fleet frequently may not egress to a vendor endpoint at all — so
    # unlike the user-facing Settings toggle (which the agent and the operator can
    # both flip), an enterprise POLICY needs to pin it off in a way the running app
    # cannot undo. This row is what makes that possible: it is read from the
    # trust-root ``security_policy.json``, which sits in
    # ``security._SENSITIVE_HOME_DIRS``, so the agent can neither read nor rewrite
    # its own ceiling.
    #
    # Default True: naming the row without ``enabled`` keeps the documented default-on
    # behavior for the
    # standalone user, who still has the toggle, the CLI, and the env var. Consulted
    # at THREE chokepoints, because any one alone would be a half-control:
    #   * ``beacon.should_send`` — blocks the send itself (the actual egress);
    #   * the dashboard config PATCH allowlist — refuses a re-enable write with 403,
    #     so a pinned host cannot be left storing ``beacon_enabled: true`` and
    #     showing a toggle that does nothing;
    #   * ``kirocrew telemetry enable`` — exits 1 without writing, for the same
    #     reason as the PATCH refusal on a headless host.
    # Writing ``false`` stays allowed at both write chokepoints (tightest-wins).
    # POLICY LAYER ONLY: ``beacon.is_governance_pinned_off`` requires
    # ``layer == "policy"``, so a Level-2 profile pinning this row does NOT suppress
    # the beacon — the probe is process-wide and carries no session, so a
    # per-surface profile is not the question it answers.
    # Data row only — CONTRACT_VERSION and the evaluator are untouched (mirrors
    # theme_install).
    "capabilities.telemetry": ScopeSpec(CAPABILITY, capability_default=True),
    # Auto-deriving the dashboard's OWN tailnet origin (``dashboard/tailnet.py``,
    # RFC §4). When ``dashboard.tailscale.enabled`` is on, the gateway shells out
    # to the local ``tailscale`` daemon at startup and, on success, puts the
    # resulting MagicDNS name into the CSRF origin allowlist and the
    # DNS-rebinding ``Host`` barrier. A managed fleet frequently may not do
    # either half: some hosts must not invoke the tailnet CLI at all, and — the
    # sharper case — an origin set that only a fleet-controlled ``dashboard.url``
    # is allowed to widen must not be widenable by a name the app derived for
    # itself. The operator-facing switch is reachable from ``config.json``, the
    # ``config.local.json`` overlay and the CLI, so exactly like
    # ``capabilities.telemetry`` it is a control the running app (and its agent)
    # can lift; this row is the one it cannot, because it is read from the
    # trust-root ``security_policy.json``, which sits in
    # ``security._SENSITIVE_HOME_DIRS`` — the agent can neither read nor rewrite
    # its own ceiling.
    #
    # Default True: naming this row without ``enabled`` must keep TODAY's behaviour
    # byte-for-byte. The feature is already off by default in config, so a default
    # of False would deny tailnet derivation for a policy that mentioned the row
    # only to configure something about it. (An unnamed row is ungoverned and
    # permitted regardless of this default — see the CAPABILITY-DEFAULT CONTRACT
    # above.) An enterprise that wants it off says so.
    # Consulted at THREE chokepoints, because any one alone is a half-control:
    #   * ``tailnet.resolve_tailnet_host`` — returns "" so no origin is derived
    #     and the daemon is not even consulted (the action itself);
    #   * the dashboard config PATCH allowlist — refuses an ENABLING write with
    #     403, so a pinned host cannot be left storing ``enabled: true`` behind a
    #     control that does nothing;
    #   * ``kirocrew config set dashboard.tailscale.enabled true`` — exits 1
    #     without writing, for the same reason on a headless host.
    # Writing ``false`` stays allowed at both write chokepoints (tightest-wins).
    # POLICY LAYER ONLY: ``tailnet.is_governance_pinned_off`` requires
    # ``layer == "policy"``, so a Level-2 profile pinning this row does NOT
    # suppress the derivation — the probe runs once at gateway startup, is
    # process-wide and carries no session, so a per-surface profile is not the
    # question it answers.
    # Data row only — CONTRACT_VERSION and the evaluator are untouched (mirrors
    # telemetry).
    "capabilities.tailnet_origin": ScopeSpec(CAPABILITY, capability_default=True),
    # "Connect your phone": minting a live mobile session credential (tailnet QR
    # or one-time login link) is an auth-surface widening an enterprise POLICY
    # must be able to close wholesale or narrow per method. The ``methods``
    # ruleset binds on the MobileConnectMethod ids the CPP seam contributes
    # (mirrors capabilities.publish's ``destinations``); WHO implements a method
    # is the orthogonal MobileConnectProvider seam — this gate only decides
    # WHETHER + WHICH. Default True: naming the row without ``enabled`` keeps
    # the personal-install pair working; a governing policy can pin it off.
    # Enforced fail-closed at the methods listing AND at each mint endpoint
    # (the filtered list is presentation, never the control). Data row only —
    # CONTRACT_VERSION and the evaluator are untouched (mirrors publish).
    "capabilities.mobile_connect": ScopeSpec(
        CAPABILITY, capability_default=True, scope_matchers={"methods": "identifier"}
    ),
    # "Share as image": the dashboard turns an assistant reply into a branded
    # PNG card and offers a prefilled post to X / LinkedIn. The card itself is
    # rendered and exported in the browser (no upload — copy / download stay
    # local), but the intent buttons hand the reply's caption text to a
    # third-party site in a URL, so the feature is an egress path for agent
    # output that a managed fleet may forbid wholesale. There is no server-side
    # share action to refuse; the control is the dashboard entry, and the
    # dashboard learns whether to draw it from ``GET /api/dashboard/config``
    # (``social_share_enabled``), which resolves this row server-side so the
    # frontend never guesses. Default True: naming the row without ``enabled``
    # keeps the entry for the standalone user; a governing ceiling — policy or
    # a profile bound to the dashboard surface — withdraws it
    # (``dashboard/social_share.py``: evaluated on the pinned ``dashboard:ui``
    # surface through ``vet_and_audit``, fail-closed, mirroring the
    # mobile_connect listing). Data row only — CONTRACT_VERSION and the
    # evaluator are untouched.
    "capabilities.social_share": ScopeSpec(CAPABILITY, capability_default=True),
    # Hosted feature-video clips: the gateway fetches a signed manifest from a
    # vendor CDN and then downloads media from it into the data home
    # (``feature_videos_manifest`` / ``feature_videos_cache``). That is outbound
    # traffic to a vendor endpoint plus third-party bytes landing on disk, which a
    # managed fleet frequently may not do at all — so this row sits in the egress
    # family with ``capabilities.telemetry`` and ``capabilities.publish`` rather
    # than with the advisory probes, and is enforced FAIL-CLOSED: an unevaluable
    # ceiling denies.
    #
    # Default True: naming the row without ``enabled`` keeps the documented
    # behaviour for the standalone user, who additionally has the
    # ``dashboard.feature_videos_enabled`` kill switch and a URL override. (An
    # unnamed row is ungoverned and permitted regardless of this default — see the
    # CAPABILITY-DEFAULT CONTRACT above.) An enterprise that wants no vendor fetch
    # says so, and unlike the config switches this row is read from the trust-root
    # ``security_policy.json``, which the agent cannot REWRITE from any surface:
    # its file tools refuse the path (``security._SENSITIVE_HOME_DIRS``, the
    # read+write fence, so those tools cannot read it either) and the OS sandbox
    # mounts the keystone read-only in every mode. A shell READ of it is permitted
    # by design (see ``security/paths.py``) — the ceiling is not a secret, it is a
    # bound — and ``kirocrew policy show`` prints the same posture summary.
    # Consulted at THREE chokepoints, because any one alone is a half-control:
    #   * the manifest fetch — no request is made, so nothing is learned;
    #   * each clip request in the download pass — no media lands on disk;
    #   * ``POST /api/feature-videos/fetch-all`` — refused 403 rather than
    #     accepted into a task that would deny itself.
    # All three are server-side, and that is the whole surface: the browser is
    # never handed a CDN url (an uncached hosted clip is not offered at all), so
    # there is no client-side fetch for the ceiling to miss.
    # Already-cached clips keep playing under a denial: withdrawing bytes already
    # on disk is a separate decision this row does not make.
    # Data row only — CONTRACT_VERSION and the evaluator are untouched (mirrors
    # social_share).
    "capabilities.feature_videos_download": ScopeSpec(CAPABILITY, capability_default=True),
    # The Jev decision seam: an enabled, sampled session sends message excerpts and
    # skill descriptions to an external, PAID provider endpoint and spends the
    # operator's account to do it. The keystone ``decisions_consent.json`` is the
    # OWNER's switch -- it is not a ceiling, and an owner who consented on a managed
    # machine has still consented to something a fleet may not permit at all. So
    # this row is the fleet's side of the same question, and it sits in the egress
    # family with ``capabilities.telemetry`` / ``publish`` /
    # ``feature_videos_download`` rather than with the advisory probes.
    #
    # Default True: naming the row without ``enabled`` keeps the documented
    # behaviour for the standalone user, whose consent keystone is still the thing
    # that turns the seam on. A governing ceiling -- policy or a profile bound to the
    # dashboard surface -- withdraws it at TWO chokepoints
    # (``decisions/capability.py``: evaluated on the pinned ``dashboard:ui``
    # surface through ``vet_and_audit``, fail-closed, mirroring social_share): the
    # consent PUT refuses to enable, and the gate's own consent read reports NOT
    # consented, so a keystone that already says ``true`` is inert rather than
    # carried over. ``GET /api/dashboard/config`` reports the answer as
    # ``decisions_enabled`` so the Feature Previews card is not drawn at all.
    # Data row only -- CONTRACT_VERSION and the evaluator are untouched (mirrors
    # social_share).
    "capabilities.decisions": ScopeSpec(CAPABILITY, capability_default=True),
    # The same seam answered by a LOCAL PRESET model on this machine
    # (``decisions/local_models.py``) instead of hosted Jev. Nothing leaves the
    # machine and nothing is spent, so a fleet that permits hosted Jev can still
    # withdraw local models by pinning THIS row off. It only narrows: a pinned
    # ``capabilities.decisions`` deny still withdraws local models too, as it did
    # before this row existed. Same two chokepoints and the same fail-closed probe
    # (``decisions/capability.py``), selected by whether the configured provider is
    # a route-built preset address -- a hand-written loopback address can be a
    # tunnel to hosted Jev and stays under the row above. Default True: an omitted
    # row permits, as for every capability.
    # Data row only -- CONTRACT_VERSION and the evaluator are untouched.
    "capabilities.decisions_local": ScopeSpec(CAPABILITY, capability_default=True),
}


def register_scope(name: str, spec: ScopeSpec) -> None:
    """Register a new governed scope (extensibility seam).

    Raises on a conflicting redefinition so a typo cannot shadow a built-in.
    """
    existing = SCOPE_CATALOG.get(name)
    if existing is not None and existing != spec:
        raise ValueError(f"scope {name!r} already registered with a different spec")
    if spec.ordinal_scale and spec.ordinal_scale not in _ORDINAL_SCALES:
        raise ValueError(f"scope {name!r} references unknown ordinal scale {spec.ordinal_scale!r}")
    if spec.matcher not in _MATCHERS:
        raise ValueError(f"scope {name!r} references unknown matcher {spec.matcher!r}")
    SCOPE_CATALOG[name] = spec


# Scope aliases: the provider doc names the profile's path scopes ``folders.read``/
# ``folders.write`` but the policy names them ``filesystem.read``/``.write`` (App.
# A.3 note + the worked example). They are the SAME path ceiling — both resolve
# through the ``path`` matcher and the gate queries ``filesystem.*`` — so a
# ``folders.*`` key is normalized to its ``filesystem.*`` scope at parse time.
# Without this, a profile's ``folders.write`` would land in a separate control
# key and silently fail to narrow the ``filesystem.write`` the gate evaluates.
_SCOPE_ALIASES: Dict[str, str] = {
    "folders.read": "filesystem.read",
    "folders.write": "filesystem.write",
}
