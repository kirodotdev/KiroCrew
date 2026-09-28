"""Which registries are in force, and what each one is trusted with.

The bundled seed plus edition rows (``_load_registry_file``); the edition-pinned and
operator-configured external registries, merged into the one list every consumer
reads (``_effective_registries``); each registry's trust tier; the clone-host trust
set and clone sandbox mode; and the owner-designated same-repository credential
carve-out with its SEL audit records.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.caches import _external_registry_cache_path
from kiro_crew.apps.registry_pipeline.git_targets import (
    _PUBLIC_GIT_HOSTS,
    _clone_sandbox_mode,
    _entry_git_url,
    _git_target_is_unsupported,
    _git_url_host,
    _is_ssh_git_url,
    _public_registry_name,
    _redact_url_userinfo,
    _strip_git_target_userinfo,
)
from kiro_crew.platform import PlatformCompositionError, current_context

try:
    from kiro_crew.sel import sel as _sel_fn
except ImportError:
    _sel_fn = None  # type: ignore[assignment]

logger = logging.getLogger(_FACADE)


#: The bundled seed, shipped beside ``registry.py`` in ``kiro_crew/apps/``.
_REGISTRY_FILE = Path(__file__).parent.parent / "app-registry.json"


#: Trust tiers an ``ExternalRegistryConfig.trust`` value may name.
#
# ``index`` is the historical (and default) posture: the registry's index is
# untrusted content, so every app it lists clones credential-free. ``owner``
# is the operator's assertion that the index itself is under change control
# they own, which lets its apps clone with the machine's git identity.
#
# Anything not in this set resolves to ``_TRUST_INDEX`` — a typo, or a tier a
# future core adds and this one does not know, must fail toward the restrictive
# posture rather than the credentialed one.
_TRUST_INDEX = "index"


_TRUST_OWNER = "owner"


_REGISTRY_TRUST_TIERS: frozenset[str] = frozenset({_TRUST_INDEX, _TRUST_OWNER})


#: Review tiers an ``ExternalRegistryConfig.review`` value may name.
#
# This says how thoroughly a registry's LISTINGS were reviewed before being
# published, which is a statement to the user — not a security control. ``trust``
# alone selects the credential posture for cloning, so a ``curated`` registry at
# the ``index`` tier still clones credential-free and a ``community`` one at the
# ``owner`` tier still clones with this machine's git identity. Keeping the two
# axes separate is deliberate: collapsing them would make "we read the listings"
# silently hand out credentials.
#
# ``""`` is the default and means the registry makes no claim, so a build that
# never sets the field renders exactly as it did before it existed. An
# unrecognised value degrades to ``""`` rather than dropping the row: the field is
# display metadata, and the list it lands in feeds index fetch, the trusted-host
# allowlist and install, so a typo must not be able to take a registry offline.
_REVIEW_UNSET = ""


_REVIEW_CURATED = "curated"


_REVIEW_COMMUNITY = "community"


_REGISTRY_REVIEW_TIERS: frozenset[str] = frozenset(
    {_REVIEW_UNSET, _REVIEW_CURATED, _REVIEW_COMMUNITY}
)


def _registry_identity_key(name_or_repo: str) -> str:
    """Canonical comparison key for a registry's public identifier.

    Registry names are normalized to their filename-safe cache-path form, then
    case-folded so ``Official`` and ``official`` cannot be treated as separate
    names on Linux but as one name on default macOS or Windows filesystems. This key governs build-pinned/config name ownership;
    it is NOT the live index-cache identity, which additionally includes the
    normalized repository and branch.
    """
    return _external_registry_cache_path(name_or_repo).name.casefold()


def _is_supported_registry_transport(repo: str) -> bool:
    """Whether *repo* is a form a registry index may legitimately be fetched from.

    Accepts an https URL, an ssh/scp remote, or a bare legacy name — and nothing
    else, so plaintext ``http://``, ``git://`` and ``ext::`` never reach a clone.
    The index this fetches becomes install coordinates, so an unauthenticated
    transport lets anything on the network path substitute app code.

    **A credential embedded in the URL is refused outright**, not redacted. A
    pinned repo travels further than a log line: the fetch uses it, ``GET
    /api/apps/registries`` returns it to dashboard clients, and the SEL trail
    records it. Redaction covers the sinks this module controls and would leave
    the others, so the value simply must not carry a secret — an edition should
    rely on the ambient git credentials the clone already has. ``https://`` refuses
    ANY userinfo, since a bare token is commonly the whole of it; ssh/scp refuse a
    ``user:password@`` form while allowing the conventional ``git@host``, which is
    a username, not a secret.

    Deliberately a mirror of ``routes._is_safe_repo_identifier`` rather than an
    import: ``routes`` imports this module, so the dependency can only run one
    way. Keep the two in step — both gate the same decision from opposite ends
    (operator-typed rows there, edition-pinned rows here).
    """
    repo = (repo or "").strip()
    if not repo:
        return False
    if ".." in repo or any(c in repo for c in " \t\n\r;|&$`<>()*?!\\\"'"):
        return False
    if re.match(r"^[A-Za-z0-9_\-]+$", repo):  # bare legacy name
        return True
    if repo.startswith("https://"):
        authority = repo[len("https://") :].split("/", 1)[0]
        return "@" not in authority
    if _is_ssh_git_url(repo):
        # Reject only a password-bearing userinfo; ``git@host`` is a username.
        # Userinfo is split off BEFORE the port: stripping at the first colon
        # would cut inside ``user:token@host`` and hide the very thing being
        # looked for.
        scheme, sep, rest = repo.partition("://")
        hostpart = (rest if sep else repo).split("/", 1)[0]
        userinfo = hostpart.rsplit("@", 1)[0] if "@" in hostpart else ""
        return ":" not in userinfo
    return False


def _pinned_registries() -> list[Any]:
    """The edition's default registries, materialised and validated.

    Rows the edition supplies are dicts (see ``AppsLoader.default_registries``);
    they are materialised into ``ExternalRegistryConfig`` here so every call site
    sees one attribute shape. A malformed row is dropped with a warning rather
    than raised on: this list feeds security gates
    (:func:`is_clone_host_trusted`), and those must keep answering.

    ``label`` and ``review`` are display metadata carried through unchanged. An
    unrecognised ``review`` value degrades to ``""`` (no claim) and is logged;
    it never drops the row, because a display field must not be able to remove a
    registry from install and the security gates — see
    :data:`_REGISTRY_REVIEW_TIERS`.
    """
    try:
        edition_rows = current_context().apps_loader.default_registries()
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("edition default_registries() unavailable", exc_info=True)
        return []

    if not edition_rows:
        return []
    if isinstance(edition_rows, (str, bytes, dict)) or not hasattr(edition_rows, "__iter__"):
        # A companion returning a scalar (or a mapping, or a bare string) is a
        # companion bug, but this list feeds `is_clone_host_trusted` — the SSRF
        # gate must answer, not raise, so the malformed value is dropped whole.
        logger.warning(
            "Ignoring a malformed default_registries() return of type %s",
            type(edition_rows).__name__,
        )
        return []

    from kiro_crew.config.loader import ExternalRegistryConfig

    pinned: list[Any] = []
    for row in edition_rows:
        if not isinstance(row, dict):
            logger.warning("Ignoring a non-object edition registry row: %s", type(row).__name__)
            continue
        repo = row.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            logger.warning("Ignoring an edition registry row with no repo URL: %r", row.get("name"))
            continue
        repo = repo.strip()
        # An edition is trusted code, but a MISCONFIGURED one must not be able to
        # downgrade the transport that carries installable app code: this index is
        # cloned and its rows become install coordinates, so a plaintext fetch lets
        # anything on the path replace them. `PUT /api/apps/registries` already
        # refuses a non-https/ssh repo, and a pinned row must not be the weaker
        # door. Mirrored rather than imported because `routes` imports this module.
        if not _is_supported_registry_transport(repo):
            logger.error(
                "Ignoring edition registry %r: %s is not an https/ssh git URL or a bare name",
                row.get("name"),
                _redact_url_userinfo(repo),
            )
            continue
        name = row.get("name")
        branch = row.get("branch")
        trust = row.get("trust")
        label = row.get("label")
        raw_review = row.get("review")
        review = raw_review.strip() if isinstance(raw_review, str) else _REVIEW_UNSET
        # An unknown review tier DEGRADES to "no claim"; it does not drop the row.
        # `review` is display metadata, and this list feeds index fetch, the
        # trusted-host allowlist and install — so dropping the row would let a
        # typo, or a tier a future core adds that this one does not know, take a
        # whole registry offline: its apps vanish from the store, its installs
        # fail, and its host leaves the clone-trust set. A display field must not
        # be able to do that.
        #
        # Degrading is not the "falsely reassuring" outcome it first looks like:
        # `""` is NO claim, which is exactly what a build that never set the field
        # renders, so a mistyped `community` shows an unbadged row rather than a
        # trusted-looking one. Logged at error level so the misconfiguration is
        # visible to whoever shipped it instead of being silently normalised.
        if review not in _REGISTRY_REVIEW_TIERS:
            logger.error(
                "Registry %r declares an unknown review tier %r (known: %s) — "
                "showing it with no review claim.",
                name,
                review,
                ", ".join(repr(t) for t in sorted(_REGISTRY_REVIEW_TIERS)),
            )
            review = _REVIEW_UNSET
        pinned.append(
            ExternalRegistryConfig(
                name=name.strip() if isinstance(name, str) else "",
                repo=repo,
                branch=branch if isinstance(branch, str) and branch else "main",
                # Display only, so an absent or non-string label is simply empty
                # and the id is shown instead. It is never substituted INTO
                # `name`: the id is what cache paths and every installed app's
                # `_registry` tag are keyed by.
                label=label.strip() if isinstance(label, str) else "",
                review=review,
                trust=trust if isinstance(trust, str) and trust else _TRUST_INDEX,
            )
        )
    # Two pinned rows sharing one public identifier are an edition bug. Their
    # live caches are source-coordinate keyed, but every returned app still
    # carries the same ``_registry`` attribution and trust lookup key. Drop ALL
    # duplicated rows rather than picking a winner and hiding the ambiguity.
    counts: dict[str, int] = {}
    for reg in pinned:
        key = _registry_identity_key(reg.name or reg.repo)
        counts[key] = counts.get(key, 0) + 1
    duplicated = {key for key, n in counts.items() if n > 1}
    if duplicated:
        for key in sorted(duplicated):
            logger.error(
                "Ignoring %d edition registries that share public identifier %r — "
                "their app attribution and trust lookup would be ambiguous.",
                counts[key],
                key,
            )
        pinned = [
            reg for reg in pinned if _registry_identity_key(reg.name or reg.repo) not in duplicated
        ]
    return pinned


def _effective_registries() -> list[Any]:
    """The external registries in force: edition defaults + operator config.

    Every consumer of the registry list goes through here, so an edition-pinned
    registry is visible to index fetch/refresh, the trusted-host allowlist, row
    lookup, install, and the blob-proxy allowlist alike. A seam wired into only
    some of those would surface an app the install path then refuses — the
    half-implemented-mechanism failure mode.

    Merge rule: an **edition default wins** when an operator row has the same
    public ``name``, repo, and branch. When the source coordinates differ,
    **neither is served**. Their index caches are isolated by full source
    identity, but both rows still claim one public ``_registry`` attribution
    and trust lookup key: silently choosing either would hide the other
    claimant and make the control-plane owner of that name ambiguous. Refusing
    both makes the conflict visible and keeps build-owned trust/review metadata
    from being associated with an operator's different source.

    Operators can add registries freely; they just cannot silently repoint one
    the edition pinned. ``PUT /api/apps/registries`` refuses to create such a
    collision, so the case that survives here is a ``config.json`` that already
    used the name before the build pinned it.

    Edition rows come first, which is also the lookup precedence
    :func:`_registry_app_candidates` documents, so a pinned registry is the first
    row consulted for a same-named app. A config-load failure degrades to the
    pinned rows alone rather than raising — the security gates must keep
    answering.
    """
    pinned = _pinned_registries()
    # Resolved at call time from the loader module (not the module-level import)
    # so this stays the single seam callers and tests already patch for the
    # config boundary — see test_catalog_inventory's registry-candidate tests.
    from kiro_crew.config.loader import (
        KiroCrewConfig,  # circular import: loader.py imports from apps/ at module level; deferring avoids ImportError
    )

    try:
        configured = list(KiroCrewConfig.load().registries or [])
    except Exception as exc:  # config load is best-effort for the security gates
        logger.debug("Could not load config for the registry list: %s", exc)
        configured = []

    if not pinned:
        return configured

    pinned_by_key = {_registry_identity_key(reg.name or reg.repo): reg for reg in pinned}
    contested: set[str] = set()
    kept_configured = []
    for reg in configured:
        key = _registry_identity_key(reg.name or reg.repo)
        rival = pinned_by_key.get(key)
        if rival is None:
            kept_configured.append(reg)
            continue
        if (rival.repo, rival.branch) != (reg.repo, reg.branch):
            contested.add(key)
            logger.warning(
                "Registry name %r is claimed by this build (%s@%s) and by your config (%s@%s); "
                "serving neither until the names differ, because their public app attribution "
                "and trust lookup identity would otherwise be ambiguous.",
                key,
                _redact_url_userinfo(rival.repo),
                rival.branch,
                _redact_url_userinfo(reg.repo),
                reg.branch,
            )
        # Same repo AND same branch: the pinned row supersedes it, nothing is lost.

    return [
        reg for reg in pinned if _registry_identity_key(reg.name or reg.repo) not in contested
    ] + kept_configured


def _registry_trust_tier(registry_name: str) -> str:
    """The trust tier in force for the registry identified by *registry_name*.

    **Only a BUILD-PINNED registry can carry ``owner``.** A row in
    ``config.json`` is read as ``index`` no matter what it declares, because
    ``config.json`` is agent-writable — ``security.py`` says so in as many words,
    with the check inline: ``is_sensitive_bash_command("echo x > …/config.json")``
    is ``None``. A tier read from there would therefore not be an operator's
    assertion at all; a prompt-injected shell could mint ``owner``, and the same
    write also adds its chosen host to ``_configured_registry_hosts()`` and lets
    it control the index that :func:`_owner_tier_confirmed` re-fetches. Every
    layer that decision passes through would be one the same write had already
    satisfied. ``default_registries()`` ships in the wheel instead, so an
    ``owner`` tier is a claim the build makes and the agent cannot forge.

    *registry_name* is the ``_registry`` tag an index entry carries, which is the
    registry's ``name`` or (when unnamed) its ``repo``. Returns ``_TRUST_INDEX``
    for an unknown registry, an unrecognised tier, or any lookup failure — the
    caller uses this to decide whether to offer credentials, so every ambiguous
    answer must be the credential-free one.
    """
    if not registry_name:
        return _TRUST_INDEX
    try:
        # Pinned AND in force. `_pinned_registries()` alone is not enough: a name
        # contested between a pinned row and a config row is served by NEITHER
        # (see `_effective_registries`), and reading the tier off the pinned list
        # would keep granting `owner` for a registry whose apps are not being
        # listed at all. So the row must survive the merge and be one the build
        # pinned — config rows are read as `index` regardless.
        pinned_keys = {_registry_identity_key(reg.name or reg.repo) for reg in _pinned_registries()}
        wanted = _registry_identity_key(registry_name)
        if wanted not in pinned_keys:
            return _TRUST_INDEX
        for reg in _effective_registries():
            if _registry_identity_key(reg.name or reg.repo) == wanted:
                tier = getattr(reg, "trust", _TRUST_INDEX)
                if isinstance(tier, str) and tier in _REGISTRY_TRUST_TIERS:
                    return tier
                if tier != _TRUST_INDEX:
                    logger.warning(
                        "Registry %r declares unknown trust %r — reading it as %r",
                        _strip_git_target_userinfo(registry_name),
                        tier,
                        _TRUST_INDEX,
                    )
                return _TRUST_INDEX
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug(
            "trust-tier lookup failed for %r",
            _strip_git_target_userinfo(registry_name),
            exc_info=True,
        )
    return _TRUST_INDEX


def _sel_credential_decision(
    operation: str, git_url: str, *, granted: bool, reason: str = ""
) -> None:
    """SEL-audit a credential decision on a registry clone (best-effort).

    Records the REFUSAL as well as the grant. A refusal is the more interesting
    record of the two: `_owner_tier_confirmed` returns False when a fresh read of
    the registry's index does not list the coordinates the local row claims, which
    is exactly the signal that something tried to escalate and was stopped. Left
    to a rotating ``logger.warning`` alone, the one event an incident responder
    would want is the one that ages out.

    Only a decision on an ATTEMPTED escalation is recorded. The ordinary
    non-escalation answers — a registry at the default tier, a bundled entry, an
    entry with no URL — are not decisions about credentials and would bury the
    real ones under a record per browse.
    """
    if _sel_fn is None:
        return
    detail = f"owner_designated_clone url={_redact_url_userinfo(git_url)}"
    if reason:
        detail = f"{detail} reason={reason}"
    try:
        _sel_fn().log_api_access(
            caller="registry",
            operation=operation,
            outcome="granted" if granted else "denied",
            resources=detail,
        )
    except Exception as exc:
        logger.debug("SEL audit log failed for %s: %s", operation, exc)


def _sel_credential_grant(operation: str, git_url: str) -> None:
    """SEL-audit an owner-designated credential GRANT (best-effort).

    The same-repo carve-out and the owner tier both escalate a clone from
    anonymous+strict to owner credentials + context sandbox. That is a
    security-relevant permission decision and must leave an audit record,
    mirroring the existing ``fetch_external_registry`` SEL events.
    """
    _sel_credential_decision(operation, git_url, granted=True)


def _owner_designated_repo_target(entry: dict[str, Any]) -> str:
    """Return the configured transport target for an exact same-repo row.

    External-registry rows and their on-disk cache are credential-free. When a
    legacy configured registry URL still carries HTTP userinfo, recover that raw
    value only from current config and only for the network call that needs it.
    Repository identity remains byte-exact and credential-free on both sides.
    """
    registry_name = entry.get("_registry")
    if not isinstance(registry_name, str) or not registry_name:
        return ""
    effective_url = _entry_git_url(entry)
    if not effective_url:
        return ""
    for reg in _effective_registries():
        public_repo = _strip_git_target_userinfo(reg.repo)
        if _public_registry_name(reg) == registry_name and effective_url == public_repo:
            return reg.repo
    return ""


def _is_owner_designated_repo(entry: dict[str, Any]) -> bool:
    """True when an index entry's clone URL is the owner-configured registry repo.

    Same-repo credential carve-out: the confused-deputy defense (anonymous env +
    strict sandbox) exists because an *untrusted index* can point at a private
    sibling repo on the owner's trusted forge. When the entry's effective clone
    URL is **byte-identical** to the owner-typed ``ExternalRegistryConfig.repo``,
    the confused-deputy argument does not apply — the owner explicitly designated
    exactly that URL by adding the registry. Such entries may use owner
    credentials (``minimal_env`` + context sandbox mode) instead of the
    anonymous+strict posture.

    This predicate is safe on the AUTOMATIC (browse/refresh) paths, which is why
    it is the only escalation they get: it compares against a URL the operator
    typed, so an entry read from the agent-writable index cache cannot widen it.
    The registry ``trust`` tier is deliberately NOT consulted here — see
    :func:`_owner_tier_confirmed`, which is install-only and re-confirms against a
    fresh index.

    Security boundary:
      - Compares against the **config-stored** repo URL, never against
        index-supplied fields — the index can ``setdefault`` the repo field,
        but an explicit override by the index will NOT match the config URL.
      - Exact string equality only; no normalization, no host-level matching
        (host-granular trust is exactly the confused-deputy hole this defense
        exists for).
      - ``subdirectory`` remains untrusted: ``_contained_join`` containment
        checks are unaffected by this predicate.
    """
    return bool(_owner_designated_repo_target(entry))


def _install_coordinates(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    """The four values that decide WHAT an install clones and runs.

    Name, clone URL, branch and subdirectory together select the bytes and the
    setup script. They are compared as one tuple by :func:`_owner_tier_confirmed`
    so a credential escalation requires the fresh index to agree on all of them,
    not merely on the repository.

    Byte-identical string comparison, no normalization — the same rule as the
    same-repo carve-out, for the same reason: any normalization here is a place
    two spellings could be made to collide.
    """
    return (
        str(entry.get("name", "") or ""),
        _entry_git_url(entry),
        str(entry.get("branch", "") or ""),
        str(entry.get("subdirectory", "") or ""),
    )


def _configured_registry_hosts() -> frozenset[str]:
    """Hosts of the external registries in force (trusted for SSH).

    A registry the owner deliberately added to their config — or one the edition
    pins as a default (:func:`_effective_registries`) — is a host they intend to
    authenticate to, so its SSH clones are allowed ~/.ssh access even if it is not
    a well-known public forge (e.g. a self-hosted Gitea/GitLab).
    """
    hosts = {_git_url_host(reg.repo) for reg in _effective_registries() if _git_url_host(reg.repo)}
    return frozenset(hosts)


def _context_clone_sandbox_mode(git_url: str) -> str:
    """Pick the clone sandbox mode for *git_url* via the active PlatformContext.

    Routes the trusted-host + clone-sandbox-mode decision through
    ``current_context().registry``.  The Default ``AppRegistryPolicy`` delegates
    to ``git_targets._clone_sandbox_mode`` / ``_PUBLIC_GIT_HOSTS`` (reached
    through the registry facade), so standalone is byte-for-byte today's decision
    (public forges + user-configured registry hosts allowed for SSH, everything
    else strict).  A companion can add
    further internal git hosts to the trusted set.  Any failure falls back to the
    bare module decision so the security gate never disappears.
    """
    if _git_target_is_unsupported(git_url):
        return "strict"
    try:
        policy = current_context().registry
        trusted = frozenset(policy.public_git_hosts()) | _configured_registry_hosts()
        return policy.clone_sandbox_mode(git_url, trusted)
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("registry clone-sandbox-mode via context failed; using default", exc_info=True)
        return _clone_sandbox_mode(git_url, _configured_registry_hosts())


def is_clone_host_trusted(git_url: str) -> bool:
    """SSRF gate: is *git_url*'s host one the owner explicitly trusts to clone?

    The trust set is the well-known public forges (``_PUBLIC_GIT_HOSTS``, plus
    any a companion contributes) UNION the hosts of the owner's
    explicitly-configured external registries (``_configured_registry_hosts``).

    Why this exists: registry ``repo`` fields are now full git URLs, and a
    configured external (federated) registry's ``app-registry.json`` is
    UNTRUSTED content — it can list an app whose ``repo`` points at an internal
    address (e.g. ``https://127.0.0.1:8443/x``) or any attacker-controlled host.
    Such a value passes ``_is_safe_repo_identifier`` and enters the blob-proxy
    allowlist (``known_registry_repos``), so without this gate merely browsing
    the App Store would drive ``git clone`` against the loopback/internal
    network — an authenticated backend SSRF. Constraining every URL clone to an
    explicitly-trusted HOST closes that vector and is immune to DNS rebinding:
    the hostname itself must be trusted, not its (re-resolvable) IP. An
    owner-configured internal forge (e.g. self-hosted GitLab at a private IP)
    stays allowed precisely because the owner added it; an index-injected host
    never is.

    Bare-name legacy repos (no URL host) return ``False`` here and are handled
    by the bundled-registry allowlist — they never reach a URL clone. Fails
    CLOSED: an unparseable/hostless URL is untrusted.
    """
    if _git_target_is_unsupported(git_url):
        return False
    host = _git_url_host(git_url)
    if not host:
        return False
    try:
        policy = current_context().registry
        trusted = frozenset(policy.public_git_hosts()) | _configured_registry_hosts()
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("clone-host trust set via context failed; using default", exc_info=True)
        trusted = _PUBLIC_GIT_HOSTS | _configured_registry_hosts()
    return host in trusted


def _edition_registry_rows() -> list[dict[str, Any]]:
    """Edition-contributed App-Store rows (CPP seam), fail-closed to []."""
    from kiro_crew.platform.context import safe_context_call

    def _read() -> list[dict[str, Any]]:
        rows = current_context().apps_loader.registry_rows()
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("name"), str)]

    return safe_context_call(
        _read,
        fallback_factory=list,
        log_message="edition registry_rows lookup failed; using bundled only",
    )


def _load_registry_file() -> list[dict[str, Any]]:
    """Load and parse the bundled app-registry.json, then merge edition rows.

    Edition rows (from the CPP ``AppsLoader.registry_rows`` seam) are appended
    ADD-only: a bundled core row wins over a same-``name`` edition row, so a
    companion can only add catalog entries, never repoint a core one. The public
    edition contributes none, so the merged list equals the bundled file.
    """
    rows: list[dict[str, Any]] = []
    if not _REGISTRY_FILE.is_file():
        logger.warning("Registry file not found: %s", _REGISTRY_FILE)
    else:
        try:
            data = json.loads(_REGISTRY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = data
            else:
                logger.warning("Registry file is not a JSON array")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load registry: %s", exc)

    seen = {r.get("name") for r in rows if isinstance(r, dict)}
    for row in _edition_registry_rows():
        if row.get("name") in seen:
            continue
        rows.append(row)
        seen.add(row.get("name"))
    return rows
