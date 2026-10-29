"""What each sandbox tier masks, what it must still expose, and which tier applies.

The ``strict`` / ``standard`` / ``cc`` mask lists, the relocated and pod-home targets,
the carve-outs in-sandbox code must reach, the environment prefixes a spawn scrubs, and
the configured mode clamped to the governance floor. One of the owners
``kiro_crew.sandbox`` is carved into: every name here stays readable and patchable as
``kiro_crew.sandbox.<name>``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import stat
from pathlib import Path
from types import ModuleType

import kiro_crew as _kiro_crew_pkg
from kiro_crew import sandbox_plan
from kiro_crew.atomic_write import refuse_linked_parent
from kiro_crew.config.paths import config_dir, kiro_agents_dir
from kiro_crew.platform import current_context
from kiro_crew.sandbox_leaves import (
    _CREW_HIDDEN_DIRS,
    _CREW_HIDDEN_LEAVES,
    _CREW_HOME_DEFAULT,
    _CREW_HOME_PREFIXES,
    _MD_NOTEBOOK_STAGING_LEAF,
    _POLICY_CACHE_LEAF,
    _RELOCATED_CREW_HIDDEN_LEAVES,
    MD_NOTEBOOK_APP_NAME,
    _crew_home_entries,
)

# Logged under the sandbox's name, which operator log filters, level settings
# and the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.sandbox")


def _resolved_kiro_agents_targets() -> list[str]:
    """The RESOLVED kiro agents tree — fork/template specs AND their advisory
    lock — sealed read-only as a directory.

    The specs are what fork governance sanitizes (allowedTools ceiling,
    autoApprove strip), so a sandboxed process that can rewrite one hands its
    next spawn forged grants — the seal is the enforcement the governance
    passes rely on. Read stays open (kiro-cli resolves its own spec here);
    every legitimate WRITER (fork/publish/reset handlers, the fork refresh,
    CLI setup) runs unsandboxed.

    Resolved per spawn through :func:`kiro_agents_dir` — never a hard-coded
    home-relative literal — so a relocated data home is covered by the same
    entry. Same contract as :func:`_relocated_crew_targets`: ``normpath``
    never ``realpath`` (event-loop safety), and never raises.
    """
    try:
        return [os.path.normpath(str(kiro_agents_dir()))]
    except Exception:
        return []


#: Directory names an installer puts a packaged distribution under. A package that
#: imports from beneath one of these was copied there by an installer (a wheel, the
#: desktop bundle's embedded interpreter); one that does not is an editable or
#: from-source checkout.
_PACKAGED_INSTALL_DIR_NAMES: frozenset[str] = frozenset({"site-packages", "dist-packages"})


def install_root_targets_for(package_dir: str) -> list[str]:
    """The packaged install tree to seal read-only for a package at *package_dir*.

    A packaged install is one whose package directory sits directly in a
    ``site-packages`` (or ``dist-packages``) directory: an installer copied it there (a wheel, the desktop
    bundle's embedded interpreter). The seal is the package directory itself, which
    holds the gateway's own sources and its served static assets (``static/dist`` with
    the precompressed ``.br``/``.gz`` siblings). The surrounding ``site-packages`` and
    the rest of the environment (its ``bin`` scripts, ``pyvenv.cfg``, other
    distributions, ``.pth`` files) stay writable to the child: sandboxed provisioning
    steps (an optional extra, an app's Python build) install into that same
    ``site-packages``, so sealing it would break them. A child can therefore still
    change what the interpreter loads at start-up through those paths; this seal covers
    the package directory only.

    An editable or from-source checkout (``pip install -e``, ``install.sh``, a Dev Fleet
    worktree) imports from a source tree, never directly from such a directory, and returns
    ``[]``: its sources are the developer's own working copy and must stay writable.

    Lexical only, so it never stats; :func:`_resolved_install_root_targets` adds the
    canonical spelling.
    """
    return _install_root_targets_with(package_dir, os.path)


def _install_root_targets_with(package_dir: str, paths: ModuleType) -> list[str]:
    """:func:`install_root_targets_for` against one platform's path module."""
    normalized = paths.normpath(paths.abspath(package_dir))
    parent = paths.basename(paths.dirname(normalized))
    if paths.normcase(parent) in _PACKAGED_INSTALL_DIR_NAMES:
        return [normalized]
    return []


@functools.lru_cache(maxsize=1)
def _install_root_targets_cached(package_dir: str) -> tuple[str, ...]:
    lexical = install_root_targets_for(package_dir)
    spellings: list[str] = []
    for target in lexical:
        spellings.append(target)
        try:
            spellings.append(os.path.realpath(target))
        except (OSError, ValueError):
            pass
    return tuple(dict.fromkeys(spellings))


@functools.lru_cache(maxsize=1)
def _bundled_kas_targets_cached(bundled_kas_dir: str) -> tuple[str, ...]:
    """Both spellings of the desktop app's staged KAS engine root, or empty."""
    if not bundled_kas_dir:
        return ()
    lexical = os.path.normpath(os.path.abspath(bundled_kas_dir))
    spellings = [lexical]
    try:
        spellings.append(os.path.realpath(lexical))
    except (OSError, ValueError):
        pass
    return tuple(dict.fromkeys(spellings))


def _resolved_install_root_targets() -> list[str]:
    """The running gateway's packaged install tree, sealed read-only for every
    sandboxed child -- plus the desktop app's staged KAS engine root, when there is one.

    Every write that changes the install tree changes what the gateway executes on its
    next start, with the gateway's identity. The legitimate writer is the updater, which
    runs in the gateway process itself and is never wrapped, so the seal costs it
    nothing. Empty for an editable or from-source checkout (see
    :func:`install_root_targets_for`).

    The staged engine root (:data:`constants.BUNDLED_KAS_DIR_ENV`, a sibling of the
    package directory inside the app's resources) is the same class of target: its
    ``install.json`` names the Node binary the gateway spawns, outside any sandbox,
    for every ``agent.kas_bundle`` session, and the binary itself sits beside it. The shell
    sets the variable only when the build staged the tree, so an unbundled build adds
    nothing here. The tree is read-only by construction (the build wrote it; the data
    home's own ``engines/kas`` is where ``kirocrew kas install`` writes), so no
    legitimate writer is fenced.

    Computed once per process: neither location moves under a running gateway, and
    caching keeps the ``realpath`` calls off every later spawn. Never raises.
    """
    # Read here rather than imported at module scope: every module-level name of this
    # module is served through the ``sandbox`` facade, and the env name is the
    # ``constants`` module's to export, not this one's.
    from kiro_crew.constants import BUNDLED_KAS_DIR_ENV

    targets: list[str] = []
    try:
        targets.extend(_install_root_targets_cached(_gateway_package_dir()))
    except Exception:
        pass
    try:
        targets.extend(_bundled_kas_targets_cached(os.environ.get(BUNDLED_KAS_DIR_ENV, "").strip()))
    except Exception:
        pass
    return list(dict.fromkeys(targets))


def _gateway_package_dir() -> str:
    """The directory the running gateway imports ``kiro_crew`` from."""
    return os.path.dirname(os.path.abspath(_kiro_crew_pkg.__file__ or ""))


#: Hidden crew-home leaves one app's OWN backend must read and write.
#:
#: These leaves sit in ``_CREW_HIDDEN_LEAVES`` to fence AGENT subprocesses (a
#: prompt-injected shell must not repoint a vault's push target or lift the PAT), but
#: the named app's backend is each leaf's only legitimate reader/writer — and that
#: backend is itself a sandboxed spawn, so the blanket mask breaks the app.
#: ``apps/backend.py`` passes the resolved paths back as ``extra_visible_dirs`` when
#: spawning exactly that app's backend; every other sandboxed process keeps the mask.
#:
#: Keyed by app name as ``apps/backend.py`` spawns it. Only apps with a SPAWNED
#: backend belong here — an in-process builtin (routes/hooks) runs unsandboxed in the
#: gateway and needs no exemption.
_APP_BACKEND_OWNED_LEAVES: dict[str, tuple[str, ...]] = {
    # NOT here: dev-fleet's live-target pointer. Its backend is that leaf's writer, but a
    # carve-out would reach every descendant of that long-lived spawn — a nested sandbox
    # is denied by design, so ``wrap_argv`` runs its ``npm ci`` / build children inside
    # the SAME namespace — and a worktree's lifecycle script could then pick the code
    # the gateway execs next. The pointer stays masked; Dev Fleet moves the cutover to a
    # gateway-process route instead (``dev_fleet/gateway_routes.py``).
    MD_NOTEBOOK_APP_NAME: (
        # The whole state directory, handed back as one unit: the state files, their staged
        # temps and the migration marker all live inside it, so the publish rename stays on
        # one mount. The retired bare spellings are NOT here; the backend never reads them.
        _MD_NOTEBOOK_STAGING_LEAF,
    ),
}


def app_backend_visible_targets(app_name: str, mode: str = "standard") -> tuple[str, ...]:
    """Absolute paths of the hidden leaves *app_name*'s own backend owns.

    Resolved the same way the launcher and seatbelt builders spell their hidden
    targets — ``$HOME``-joined across both data-home spellings, plus the relocated
    paths when ``KIROCREW_HOME`` moves the data home — so each returned path matches
    its mask entry exactly and ``_hidden_path_contains_visible_path`` lifts it.

    A spelling that sits BENEATH an independently masked directory is REFUSED,
    not carved (see :func:`carveout_shadowed_by_foreign_mask`): carving it out
    would take that whole foreign mask down with it. The backend keeps its
    EPERM on such a host, which is strictly safer than unmasking a foreign
    tree.

    Returns ``()`` for an app with no owned leaves, leaving its spawn unchanged.
    *mode* is the tier the caller passes to :func:`wrap_argv` for the same
    spawn — the shadow guard judges against that tier (post-clamp), so a
    future caller wrapping at ``cc``/``strict`` must say so here too.
    """
    leaves = _APP_BACKEND_OWNED_LEAVES.get(app_name)
    if not leaves:
        return ()
    home = str(Path.home())
    targets = [os.path.join(home, entry) for entry in _crew_home_entries(leaves)]
    targets.extend(_relocated_crew_targets(leaves))
    return tuple(
        target
        for target in targets
        if not carveout_shadowed_by_foreign_mask(target, mode=mode)
        and not carveout_chain_has_planted_link(target)
    )


def enterprise_policy_cache_view(
    mode: str = "standard", extra_hidden_dirs: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """The governance cache to lift READ-ONLY for a sandboxed spawn, in an enterprise build.

    An enterprise build always enters the central tier (RFC D2), including a Kiro Crew
    MCP server kiro-cli starts inside the agent sandbox. That process reaches neither the
    sign-in nor the control plane, so it resolves the ceiling from the cache in cache-only
    mode (``policy_distribution_source.cache_only``) and fails closed without it. The cache
    holds only the document the agent is already governed by, a digest of the build's
    fixed source and the fetch validators; no credential. The vault and every other
    credential path stay hidden.

    Lifted the way ``apps/backend.py`` lifts it for a cache-only backend: one spelling,
    refused when it sits beneath a foreign mask (:func:`carveout_shadowed_by_foreign_mask`),
    and kept read-only by the plan for every spelling (``sandbox_plan.is_policy_cache_dir``).
    ``()`` in an oss build, so an oss sandbox is unchanged.

    Also ``()``, so the cache stays masked and a cache-only child fails closed, when:

    * one of the caller's own *extra_hidden_dirs* is the cache or an ancestor of it. A
      lift cancels every mask that contains it, so lifting would take down a mask the
      caller asked for (a whole crew home hidden from agent-authored test code);
    * the metadata is in the format older builds wrote, which recorded the source URL
      in plain text, and a pre-signed URL is a credential. The gateway's next fetch or
      304 rewrites it without the source.
    """
    try:
        from kiro_crew.edition import is_enterprise

        if not is_enterprise():
            return ()
        target = os.path.join(str(config_dir()), _POLICY_CACHE_LEAF)
    except Exception:
        logger.debug("could not resolve the governance cache for the sandbox", exc_info=True)
        return ()
    if extra_hidden_dirs and _caller_mask_contains(target, extra_hidden_dirs):
        return ()
    if carveout_shadowed_by_foreign_mask(target, mode=mode):
        return ()
    if _policy_cache_meta_names_its_source(target):
        logger.warning(
            "the governance cache still carries its source in plain text; keeping it hidden "
            "from the sandbox until the gateway's next fetch rewrites it"
        )
        return ()
    return (target,)


def _caller_mask_contains(target: str, hidden: tuple[str, ...]) -> bool:
    """Would a lift of *target* cancel one of the caller's *hidden* masks?

    Checked both as written (the Seatbelt plan compares spellings) and with the crew-home
    alias fold the namespace plan applies (``sandbox_plan.fold_crew_home_alias``), so a
    mask spelled under ``$HOME`` still counts when the data home resolves elsewhere. An
    alias table that cannot be built reads as "contains": the mask is kept.
    """
    try:
        aliases = _crew_home_alias_roots()
    except Exception:
        logger.debug("could not resolve the crew-home aliases", exc_info=True)
        return True
    folded_target = sandbox_plan.fold_crew_home_alias(os.path.abspath(target), aliases)
    for path in hidden:
        if sandbox_plan.hidden_path_contains_visible_path(path, (target,)):
            return True
        folded = sandbox_plan.fold_crew_home_alias(os.path.abspath(path), aliases)
        if sandbox_plan.hidden_path_contains_visible_path(folded, (folded_target,)):
            return True
    return False


#: The cache's metadata leaf, mirroring ``policy_distribution_cache._CACHE_META_LEAF``.
_POLICY_CACHE_META_LEAF = "policy.meta.json"


def _policy_cache_meta_names_its_source(cache: str) -> bool:
    """Does the cache metadata carry a plaintext ``source``? Unreadable reads as yes."""
    try:
        with open(os.path.join(cache, _POLICY_CACHE_META_LEAF), "rb") as handle:
            raw = handle.read(64 * 1024)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return True  # the reader refuses it anyway, and its bytes are not known to be clean
    return not isinstance(meta, dict) or "source" in meta


def crew_home_visible_spellings(path: str) -> tuple[str, ...]:
    """*path*, spelled every way the masks spell the crew data home.

    ``extra_visible_dirs`` lifts a mask entry only when
    :func:`_hidden_path_contains_visible_path` sees that entry CONTAIN one of the
    spellings it was handed, and that predicate is purely lexical (``commonpath``
    over ``abspath``, never ``realpath`` — the builders run on the event loop).
    The masks meanwhile carry THREE spellings of one crew-home directory: the two
    ``$HOME``-joined prefixes (:data:`_CREW_HOME_PREFIXES`, from
    ``Path.home()``, which does not resolve links) and the resolved
    :func:`config_dir` path :func:`_relocated_crew_targets` adds.

    A carve-out naming only one of them lifts only that entry. Where a second
    spelling ALIASES the same directory — a symlinked ``$HOME``, so
    ``config_dir()`` returns the link-resolved path and ``Path.home()`` returns
    the link — its mask survives and bind-mounts an empty directory back over the
    tree the carve-out just exposed. The child then gets ``ENOENT`` on a file the
    gateway can stat, which reads as a missing file rather than as a mask.

    A home-joined prefix may instead name a different crew data home. Adding its
    spelling would lift that foreign tree's mask, so each prefix root is admitted
    only when ``stat`` reports the same directory identity as :func:`config_dir`.
    Missing, non-directory, and unstatable roots stay excluded. This filesystem
    resolution must run off the event loop; its caller runs in a worker thread.
    The launcher and Seatbelt builders remain purely lexical on the event loop.

    So a crew-home carve-out is passed as the whole proven-alias spelling set.
    :func:`app_backend_visible_targets` already does this for its FIXED leaves;
    this function does it for a path computed per call — a transfer staging
    directory, named only once the transfer starts — which no static list can
    enumerate.

    *path* is returned alone when it is not under the data home: an ordinary
    workspace or repo carve-out has no crew-home spelling to add. Never raises —
    an unresolvable home yields the one spelling the caller already had, which is
    the behaviour before this function existed.
    """
    target = os.path.abspath(path)
    spellings = [target]
    try:
        root = os.path.normpath(str(config_dir()))
        home = str(Path.home())
        rel = os.path.relpath(target, root)
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not re-spell the crew-home carve-out %s", path, exc_info=True)
        return tuple(spellings)
    # Outside the data home (``..`` in the relative path, or a different drive on
    # Windows, which makes ``relpath`` raise above): nothing to re-spell.
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return tuple(spellings)
    try:
        root_info = os.stat(root)
    except Exception:  # pragma: no cover - defensive; exclusion is the safe answer
        logger.debug("could not identify crew-home root %s", root, exc_info=True)
        return tuple(spellings)
    if not stat.S_ISDIR(root_info.st_mode):
        return tuple(spellings)
    root_identity = (root_info.st_dev, root_info.st_ino)
    for prefix in _CREW_HOME_PREFIXES:
        candidate_root = os.path.normpath(os.path.join(home, prefix))
        try:
            candidate_info = os.stat(candidate_root)
        except Exception:
            continue
        if not stat.S_ISDIR(candidate_info.st_mode):
            continue
        if (candidate_info.st_dev, candidate_info.st_ino) != root_identity:
            continue
        spelling = os.path.normpath(os.path.join(candidate_root, rel))
        if spelling not in spellings:
            spellings.append(spelling)
    return tuple(spellings)


def carveout_chain_has_planted_link(path: str) -> bool:
    """Whether *path*'s parent chain passes through a link, so it must not be carved out.

    The boolean form of :func:`atomic_write.refuse_linked_parent`, for
    the two producers that must DEGRADE rather than raise. An app's state leaves sit under
    an agent-writable tree, so a link planted at an intermediate component means this
    process cannot tell which directory a write to that name will land in — and handing a
    spawn a carve-out for such a name unmasks whatever the link currently points at.

    Refusing the CARVE-OUT is the proportionate response, not refusing the spawn. Withhold
    it and the owning backend fails on its own masked state, which is the app's problem;
    refuse the spawn and one optional app's on-disk layout takes every sandboxed process on
    the host down with it. The two are safe together because they are keyed off this one
    predicate: while it holds, the backend cannot write the state, so a leaf left
    unmaterialised — and therefore unmasked — has nothing to expose.

    Fails toward "unsafe": an unresolvable chain is reported as planted, so the carve-out is
    withheld rather than granted on a path this process could not verify.
    """
    try:
        refuse_linked_parent(path)
    except OSError as exc:
        logger.warning(
            "SECURITY: not carving %s out of the sandbox masks — a parent component is a "
            "link (%s), so this process cannot tell which directory a write to that name "
            "would reach. The owning app backend will fail on its own state until the link "
            "is replaced with a real directory; every other spawn is unaffected.",
            path,
            exc,
        )
        return True
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not check the carve-out chain for %s", path, exc_info=True)
        return True
    return False


def carveout_shadowed_by_foreign_mask(path: str, mode: str = "standard") -> bool:
    """Whether carving *path* out of the sandbox masks would unmask a foreign tree.

    ``_hidden_path_contains_visible_path`` cancels any hidden mask entry that
    CONTAINS a visible path, so an ``extra_visible_dirs`` spelling that sits
    beneath an independently masked directory takes that whole foreign mask
    down with it — a data home relocated beneath a credential directory (e.g.
    ``KIROCREW_HOME`` under ``~/.aws``) would hand every spawn that asks for a
    crew-home carve-out the entire credential tree. Every EQUALITY-shaped
    crew-home carve-out producer calls this before adding a spelling —
    :func:`app_backend_visible_targets`, the policy-cache site in
    ``apps/backend.py``, and the pod Azure provider CLI spawn in
    ``monitoring/provider_cli.py``. The aws-control per-call staging carve-out is the
    ANCESTOR-LIFT shape instead: its temp dir is a proper descendant of the mask
    entry the lift cancels, so asking about that dir refuses on every layout,
    the default one included. It asks about the staging ROOT instead — the mask
    entry itself, which the equality rule below exempts while every OTHER masked
    ancestor still refuses. Same verdict, no parameter: no mask entry can sit
    between a root and the per-call dir minted directly inside it, and that dir
    is a fresh random name no list carries. On refusal the mask stays
    and the carve-out's consumer fails closed (EPERM for an app backend's own
    state, an unreadable cache for a cache-only backend), which is strictly
    safer than unmasking a foreign tree. The refusal is logged with the
    offending ancestor so the misconfiguration is actionable.

    The mask universe is the EFFECTIVE tier's dir list — *mode* run through the
    same ``sandbox.min_level`` clamp :func:`wrap_argv` applies (see
    :func:`effective_sandbox_mode`) — NOT the union of every tier: ``standard``
    deliberately leaves ``~/.aws`` visible, so a data home relocated beneath it
    shadows nothing in an ungoverned standard-mode spawn, and refusing there
    would break the carve-out's consumer for zero security gain. A governed
    floor that clamps the spawn up is honoured by construction, because the
    clamp result is what selects the list. Two deliberate asymmetries, both in
    the refusal direction: ``cc`` uses the launcher's list on every platform
    (the macOS profile builder drops ``.aws`` from it in favour of file-level
    rules, so macOS ``cc`` can only over-refuse), and a tier that cannot be
    resolved falls back to the union of every tier. The relocated mask entries
    the builders add in every mode are included on BOTH paths, so a carve-out
    leaf beneath a relocated wholesale mask is judged against it too. Two
    builder additions are deliberately absent: ``_voice_runtime_sandbox_paths``
    (fixed runtime sockets no crew-home carve-out can sit beneath) and a
    caller's ``extra_hidden_dirs`` (neither guarded producer's spawn passes
    any).

    The floor is re-read by ``wrap_argv`` at wrap time; a floor RAISED in the
    interval between this check and the wrap is honoured for the masks but not
    for this refusal. Exploiting that window requires an operator to both
    tighten governance and have already relocated the data home beneath a
    newly-masked tree — operator actions, not agent-reachable ones.

    Equality is not shadowing — carve-out spellings ARE masked entries, and
    unhiding exactly themselves is the carve-out's whole job; only a PROPER
    ancestor is foreign. An ancestor-lift producer therefore asks about the mask
    entry it lifts, never about the descendant it hands to the spawn.

    Fails toward refusal, never raises: when home (or the path itself) cannot
    be resolved the mask universe cannot be checked, so the spelling is
    reported shadowed and the spawn proceeds with the mask intact.
    """
    try:
        home = str(Path.home())
        candidate = os.path.abspath(path)
    except Exception:
        logger.debug("could not resolve home for the carve-out shadow check", exc_info=True)
        return True
    try:
        effective = effective_sandbox_mode(mode)
        policy = _sandbox_policy()
        if effective == "strict":
            tier_dirs = list(policy.strict_dirs())
        elif effective == "cc":
            tier_dirs = list(policy.cc_dirs())
        else:
            # "standard" — and "off", where no mask exists and ``wrap_argv``
            # ignores ``extra_visible_dirs`` entirely, so the verdict is inert.
            tier_dirs = list(_STANDARD_DIRS)
    except Exception:
        # Deliberately swallows PlatformCompositionError, which
        # ``_governance_sandbox_floor`` otherwise propagates so a floor never
        # silently downgrades DENY to ALLOW: here the union fallback is a
        # SUPERSET of every tier (refusal-leaning, the opposite of a
        # downgrade), and ``wrap_argv`` re-reads the floor at wrap time and
        # still raises for real.
        tier_dirs = list(dict.fromkeys([*_STRICT_DIRS, *_CC_DIRS, *_STANDARD_DIRS]))
    ancestors = [os.path.abspath(os.path.join(home, rel)) for rel in dict.fromkeys(tier_dirs)]
    # The builders extend every mode's hidden set with the relocated crew
    # entries; mirror that (on both the resolved-tier and fallback paths) so a
    # relocated wholesale mask still counts. Both helpers never raise.
    ancestors.extend(
        os.path.abspath(entry)
        for entry in (
            *_relocated_crew_targets((*_CREW_HIDDEN_LEAVES, *_RELOCATED_CREW_HIDDEN_LEAVES)),
            *_relocated_policy_cache_dirs(),
        )
    )
    for ancestor in dict.fromkeys(ancestors):
        if candidate == ancestor:
            continue
        try:
            contained = os.path.commonpath((ancestor, candidate)) == ancestor
        except ValueError:
            continue
        if contained:
            logger.warning(
                "SECURITY: refusing the sandbox carve-out for %s — it sits beneath "
                "the independently masked directory %s, and carving it out would "
                "unmask that whole tree. The carve-out's consumer will keep failing "
                "on the masked path until the data home moves out from under that "
                "directory.",
                path,
                ancestor,
            )
            return True
    return False


# Sensitive directories to hide from the agent subprocess tree.
# "strict" mode hides all; "standard" mode only hides non-workflow dirs.
_STRICT_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".config/gh",
    ".azure",
    ".docker",
    ".kube",
    # Encrypted secret vault. The ``.vault`` dir is also a
    # keystone leaf in ``security._CREW_SECRET_LEAVES`` (which blocks the
    # agent's in-process tool-call file access), but a spawned ``python -c``
    # subprocess does an OS ``open()`` that never routes through that gate — so
    # the vault dir must ALSO be bind-mount-hidden here, exactly as ``.env`` is
    # in ``_CC_FILES``. Without this a same-UID agent subprocess could read
    # ``.vault/.vault_key`` and decrypt the store.
    ".kiro/crew/.vault",
    ".kirocrew/.vault",
    # The centrally-distributed governance ceiling's cache
    # (``platform/policy_distribution_cache.py``). Bind-mount-hidden in every mode for the
    # reason the vault above is: ``is_sensitive_path`` is the shared read+write gate for
    # the agent's in-process tool calls, but a spawned ``python -c`` does an OS
    # ``open()`` that never routes through it. That matters more here than for the
    # policy FILE, which this cache copies: on a fleet using the environment channel
    # there is no ``security_policy.json`` on disk at all, so the cache is the only
    # on-disk copy of the ceiling — and its metadata records the SOURCE, which the
    # loader trusts when deciding whether the cache is this host's last-known-good.
    ".kiro/crew/policy_cache",
    ".kirocrew/policy_cache",
    ".kiro/crew/run/voice-runtime",
    ".kirocrew/run/voice-runtime",
]
_STRICT_DIRS += _CREW_HIDDEN_DIRS
_STRICT_DIRS += [".midway"]

_STANDARD_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
    # Secret vault — hidden in every mode (see _STRICT_DIRS note above).
    ".kiro/crew/.vault",
    ".kirocrew/.vault",
    # The centrally-distributed governance ceiling's cache
    # (``platform/policy_distribution_cache.py``). Bind-mount-hidden in every mode for the
    # reason the vault above is: ``is_sensitive_path`` is the shared read+write gate for
    # the agent's in-process tool calls, but a spawned ``python -c`` does an OS
    # ``open()`` that never routes through it. That matters more here than for the
    # policy FILE, which this cache copies: on a fleet using the environment channel
    # there is no ``security_policy.json`` on disk at all, so the cache is the only
    # on-disk copy of the ceiling — and its metadata records the SOURCE, which the
    # loader trusts when deciding whether the cache is this host's last-known-good.
    ".kiro/crew/policy_cache",
    ".kirocrew/policy_cache",
    ".kiro/crew/run/voice-runtime",
    ".kirocrew/run/voice-runtime",
]
_STANDARD_DIRS += _CREW_HIDDEN_DIRS

# CC mode: hides all credential dirs including .aws, but selectively exposes
# .aws/config (needed for credential_process → Bedrock auth). All other .aws
# files (credentials, sso cache, etc.) are filesystem-hidden via bind mount.
_CC_DIRS: list[str] = [
    ".kiro/crew-auth-staging",
    ".aws",
    ".gnupg",
    ".gpg",
    ".config/gcloud",
    ".azure",
    ".docker",
    ".kube",
    # Secret vault — hidden in every mode (see _STRICT_DIRS note above).
    ".kiro/crew/.vault",
    ".kirocrew/.vault",
    # The centrally-distributed governance ceiling's cache
    # (``platform/policy_distribution_cache.py``). Bind-mount-hidden in every mode for the
    # reason the vault above is: ``is_sensitive_path`` is the shared read+write gate for
    # the agent's in-process tool calls, but a spawned ``python -c`` does an OS
    # ``open()`` that never routes through it. That matters more here than for the
    # policy FILE, which this cache copies: on a fleet using the environment channel
    # there is no ``security_policy.json`` on disk at all, so the cache is the only
    # on-disk copy of the ceiling — and its metadata records the SOURCE, which the
    # loader trusts when deciding whether the cache is this host's last-known-good.
    ".kiro/crew/policy_cache",
    ".kirocrew/policy_cache",
    ".kiro/crew/run/voice-runtime",
    ".kirocrew/run/voice-runtime",
]
_CC_DIRS += _CREW_HIDDEN_DIRS
_CC_DIRS += [".midway"]


def _resolves_to(alias: str, canonical: str) -> bool:
    """Whether *alias* is another NAME for *canonical*: its components resolve to it.

    Resolution, not identity. A ``(st_dev, st_ino)`` match would also hold for a
    bind mount of the data home at the alias, and a bind mount is a second MOUNT,
    not a second name: a mask placed on the canonical path's entry does not appear
    under the bind, so folding the alias onto the canonical would leave every leaf
    under the alias unmasked. Only a path whose link components lead to the
    canonical directory shares that directory's entries, and only such a path is
    folded. ``realpath`` is strict, so an alias that cannot be resolved is a
    different name and the caller keeps its entry.
    """
    if alias == canonical:
        return True
    try:
        return os.path.realpath(alias, strict=True) == os.path.realpath(canonical, strict=True)
    except OSError:
        return False


def _crew_home_alias_roots() -> tuple[tuple[str, str, int, int], ...]:
    """``(alias, canonical, st_dev, st_ino)``: each ``$HOME``-joined crew root that IS the data home.

    The tier lists spell every crew leaf under ``$HOME/<prefix>`` and the pre-spawn
    passes spell it under ``config_dir()``, whose components may themselves be links
    (a resolved ``KIROCREW_HOME``, or the default ``~/.kiro/crew`` linked to
    ``~/.kirocrew``). On a host whose home is a link (``/home/u -> /mnt/home/u``)
    those are two names for one directory, and
    the launcher keeps every record per name -- the occupant a pass saw, the stand-in
    a loop placed -- and compares them against the filesystem by identity, so two
    names make the records disagree with what either name reaches. One path
    resolution per crew root decides it here, in the pass that already stats these
    roots, and the builder folds the alias spelling onto the canonical one so the
    launcher is handed one name per directory. The test is resolution, not
    identity: a bind mount of the data home at ``$HOME/<prefix>`` shares its
    ``(st_dev, st_ino)`` but is a second mount, on which a mask placed at the
    canonical path does not appear, so it keeps its own entry. A root
    string-equal to the data home is no alias; an absent or unreadable root is
    left as it is, since the launcher already handles an absent name.

    The identity the decision rested on travels with the pair. The fold is a
    check made here and acted on in the child, and the component that makes the
    two spellings one directory -- the home link -- is a name a writer could
    re-aim in between; the launcher re-reads each alias before it places a mask
    and refuses unless it still reaches the recorded directory, so the folded
    rules never stand for an alias that has moved.

    Never raises: a home that cannot be resolved yields no pair.
    """
    try:
        canonical = str(config_dir())
        home = Path.home()
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for the alias fold", exc_info=True)
        return ()
    pairs: list[tuple[str, str, int, int]] = []
    for prefix in _CREW_HOME_PREFIXES:
        alias = str(home / Path(prefix))
        if alias == canonical:
            continue
        try:
            info = os.stat(canonical)
        except OSError:
            continue
        if _resolves_to(alias, canonical):
            pairs.append((alias, canonical, info.st_dev, info.st_ino))
    return tuple(pairs)


def _relocated_crew_targets(leaves: tuple[str, ...]) -> list[str]:
    """The RESOLVED crew-home paths for *leaves*, when the data home is not under ``$HOME``.

    Every entry in the dir lists is ``$HOME``-relative and joined with ``Path.home()``, so
    ``KIROCREW_HOME=/srv/crew`` moves the data home out from under all of them and no rule
    matches the real governance tree. :func:`_relocated_policy_cache_dirs` already closes
    that hole for the one directory it was written for; the ceiling and the secret leaves
    need it for the same reason, so the resolution is shared here instead of restated.

    Returns only the paths that DIFFER from the ``$HOME``-relative spelling the lists
    already carry, so the default layout gains no duplicate rule. Under a symlinked
    home the resolved spelling differs as a STRING from the ``$HOME`` one while
    naming the same directory; the namespace plan folds the ``$HOME``
    spellings onto the resolved one (see ``_crew_home_alias_roots``) so the launcher
    is handed one name per directory.

    ``normpath``, never ``realpath``: lexical on purpose, so this helper itself does
    no filesystem call; the one identity decision per crew root is made in the
    pre-spawn pass that already stats those roots.

    Never raises: a data home that cannot be resolved yields nothing and the
    ``$HOME``-relative entries still apply.
    """
    try:
        home_root = os.path.join(str(Path.home()), _CREW_HOME_DEFAULT)
        resolved_root = str(config_dir())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the crew data home for sandbox masking", exc_info=True)
        return []
    out: list[str] = []
    for leaf in leaves:
        try:
            resolved = os.path.normpath(os.path.join(resolved_root, leaf))
            default = os.path.normpath(os.path.join(home_root, leaf))
        except Exception:  # pragma: no cover - defensive
            continue
        if resolved != default:
            out.append(resolved)
    return out


#: Tier leaves NOT re-anchored under a pod child's remapped home, because they ARE
#: the pod's own MCP OAuth grant store: the child WRITES its grants under this tree
#: and ``mcp_grant`` stats them there, so bind-masking it empty would discard every
#: grant the pod mints. Nothing host-derived lives here -- the seeder creates
#: ``.aws/sso/cache`` and copies nothing into it (see
#: ``pod.runtime._seed_pod_os_home``), and sign-in comes from the runtime's own data
#: store, so the carve-out exposes only pod-minted material. That is what answers
#: the security review that blocked the earlier shape, in which the corridor also
#: exposed COPIED HOST bearer tokens.
_POD_OS_HOME_GRANT_STORE_LEAVES: frozenset[str] = frozenset({".aws"})

#: Re-anchored IN ADDITION to the selected tier, so carving out the grant store
#: above does not also expose the file-credential leg. These are the profile files
#: whose ``AWS_CONFIG_FILE`` / ``AWS_SHARED_CREDENTIALS_FILE`` pass-through
#: ``acp.client._apply_pod_home_remap`` deliberately deleted; a remapped ``HOME``
#: makes both resolve inside the pod home, so they are masked by name there. Kept
#: rather than folded into a narrower ``.aws/sso/cache``-only carve-out because the
#: launcher's bind-mask has no directory-level exemption: a mask on ``.aws`` covers
#: everything beneath it, and the only re-expose primitive is per-FILE, while grant
#: filenames are sha256 keys that do not exist until the child mints them.
_POD_OS_HOME_MASKED_SUBLEAVES: tuple[str, ...] = (
    ".aws/config",
    ".aws/credentials",
    ".aws/cli",
)


def _pod_os_home_targets(dirs: tuple[str, ...]) -> list[str]:
    """The sensitive-dir list re-anchored under a pod child's REMAPPED home.

    Sibling of :func:`_relocated_crew_targets`, for the other root that moves.
    ``acp.client._apply_pod_home_remap`` gives a pod's kiro-cli child a pod-owned
    ``HOME`` (``KIROCREW_OS_HOME``) so its OAuth grants die with the pod, and
    ``pod.runtime._seed_pod_os_home`` mirrors the runtime's identity store into it
    (no host SSO cache contents are copied). Those are credentials, and every entry
    in the tier lists is ``$HOME``-relative joined
    against ``Path.home()`` -- the GATEWAY's home -- so none of them named the
    remapped tree and the seeded token sat in an UNMASKED location that the child's
    own ``$HOME`` resolves to.

    **Why this lives here rather than in the two ACP transports.** Both of them
    build their sandbox BEFORE applying the remap, so feeding the paths in through
    ``extra_hidden_dirs`` would need the call order changed in two places and the
    path set restated in both -- two independent copies of one rule, which is the
    duplication rounds 8 and 9 removed from the pinned-write path. Computing it
    inside the mask builder instead makes the mask correct for EVERY caller
    regardless of when the remap runs, and re-anchors whichever tier list the
    caller's mode selected rather than a hand-copied subset of it.

    Gated on ``KIROCREW_POD == "1"`` exactly as ``config.paths`` gates the
    resolver, so a non-pod session's mask is byte-identical to before. Returns only
    paths that DIFFER from the ``$HOME``-relative spelling, so the default layout
    gains no duplicate rule.

    ``normpath``, never ``realpath``, for the reason :func:`_relocated_crew_targets`
    records: this runs on the event loop for every async spawn. Never raises -- an
    unresolvable value yields nothing and the ``$HOME``-relative entries still apply.

    **One leaf is deliberately NOT re-anchored: the pod's own grant store.**
    ``<os-home>/.aws`` is where the pod's kiro-cli child writes its OWN MCP OAuth
    grants, which ``mcp_grant`` then stats through
    ``config.paths.kiro_oauth_cache_home``. No host SSO cache contents are staged
    into it -- ``_seed_pod_os_home`` creates ``.aws/sso/cache`` EMPTY, and the
    earlier revision that copied the operator's tokens there was deleted -- so what
    the tree holds is grants that pod itself minted. Bind-masking it empty still
    breaks the feature: the child's grant writes land in the overlay instead of the
    pod tree, so ``grant_presence`` answers "no grant" forever. A read-only per-file
    re-expose (the ``expose_files`` primitive) cannot substitute, because the child
    needs WRITE access and the grant filenames are sha256 keys that do not exist
    until the child mints them -- there is nothing to enumerate at launcher-build
    time.

    What that costs, stated rather than implied: an agent tool call inside the pod
    can read the grants that pod minted. That is accepted here because it is
    strictly narrower than both baselines it replaces -- a pod child resolving the
    REAL ``~/.aws/sso/cache``, and a non-pod kiro-cli child is
    exposed the real credential homes by the standard tier for exactly this sign-in
    reason. The tree is created by the pod and reclaimed by ``pod down``.

    **A SECOND tree carries the same residual, and it is host-derived rather than
    pod-minted: the staged identity store.** ``_seed_pod_os_home`` snapshots the
    runtime identity store (``.local/share/{kiro-cli,amazon-q}`` and the platform
    siblings, from ``identity_stores.store_mappings``) into the pod home, because
    that is where the harness resolves its access token and a pod with no copy boots
    signed-out. Those rows are absent from every masking tier ON PURPOSE --
    ``test_the_agent_runtime_auth_stores_stay_visible`` pins them out, because the
    harness is itself spawned inside this sandbox -- so this function emits nothing
    for them and an agent shell descendant can read the staged bearer token.

    That is NOT closed here, and the reason is structural rather than an oversight:
    a bind-mask acts on a MOUNT NAMESPACE, and the harness and the tool subprocesses
    it spawns share one. Hiding the store from the agent's shell hides it from
    kiro-cli in the same act, which is the auth break the tier exclusion exists to
    avoid. Crew owns no seam between the two audiences -- it wraps the harness, and
    the harness spawns its own tools inside that wrapper. The layer that DOES
    separate them is the tool gate: ``security.is_sensitive_path`` refuses every
    staged store path (``identity_stores.fenced_home_dirs`` is spliced into
    ``_SENSITIVE_HOME_DIRS`` and re-anchored under ``KIROCREW_OS_HOME``), so a
    tool call naming the path is denied and only a raw ``open()`` inside a spawned
    shell reaches it.

    **The command matcher is not a second opinion on this, for ANY path.**
    ``is_sensitive_bash_command`` carries a size ceiling, an IMDS check and an
    environment-credential exfiltration check, and no path matcher at all.
    Measured, it answers ALLOW for every path spelling -- ``~/.aws/credentials``
    and ``~/.ssh/id_rsa`` included -- so the pod tree is not a special case
    there. What bounds a shell is this mask and nothing else, which is
    exactly why the carve-outs above are the interesting part of this function.

    The re-anchoring rule itself is :func:`kiro_crew.sandbox_plan.pod_home_targets`,
    which the plan applies to every spawn; this reads the pod environment for it.

    Pinned in both directions by
    ``test_pod_runtime_auth_store.py::TestTheStagedStoresResidualIsPinned``; closing
    the staged store's shell leg needs a pod-scoped sign-in that never places a host
    bearer in the pod tree, which is a design change rather than a mask entry.

    The file-credential leg stays closed: ``.aws/config`` and ``.aws/credentials``
    are re-anchored EXPLICITLY, so the profile files whose pass-through
    ``acp.client._apply_pod_home_remap`` deleted cannot resolve inside the pod home
    either. Every other leaf in the tier is re-anchored unchanged.
    """
    if os.environ.get("KIROCREW_POD") != "1":
        return []
    os_home = os.environ.get("KIROCREW_OS_HOME")
    if not os_home:
        return []
    try:
        home_root = str(Path.home())
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the home root for pod os-home masking", exc_info=True)
        return []
    return sandbox_plan.pod_home_targets(
        dirs,
        home_root,
        os_home,
        _POD_OS_HOME_GRANT_STORE_LEAVES,
        _POD_OS_HOME_MASKED_SUBLEAVES,
    )


def _relocated_policy_cache_dirs() -> list[str]:
    """The governance cache's RESOLVED path, when it is not under ``$HOME``.

    Every entry in the dir lists above is ``$HOME``-relative and joined with
    ``Path.home()``, so ``KIROCREW_HOME=/srv/crew`` moves the data home out from under
    all of them. ``_RELOCATED_CREW_HIDDEN_LEAVES`` lifts that limit for the vault, and
    this one directory must not inherit it: on a fleet using the environment channel
    there is no ``security_policy.json`` on disk at all, so the cache is the ONLY on-disk
    copy of the ceiling, and its metadata records the source the next boot trusts. An
    agent subprocess able to rewrite it on a relocated home could hand itself a ceiling.

    Returns the path only when it differs from the ``$HOME``-relative form the lists
    already cover, so the default layout gains no duplicate rule.

    **Compared with ``normpath``, not ``realpath``, and that is deliberate.** This runs
    inside ``_live_plan_host``, which both builders call on the spawn path
    for every async spawn — the same reason the launcher pushes its ``isdir`` checks into
    the child (see the note there): on a stalled NFS home a link-resolving syscall here
    freezes the gateway and its liveness heartbeat. ``normpath`` is pure string work.

    The cost is precise and one-directional: where the home is a symlink (``/home/u`` →
    ``/local/home/u`` is ordinary on managed hosts) the two spellings do not compare
    equal, so a DEFAULT layout is reported as relocated and the resolved path is masked in
    addition to the ``$HOME``-relative one. That is a redundant rule for a directory that
    should be masked either way, never a missing one — the comparison was only ever
    de-duplication. Never raises: a data home that cannot be resolved yields nothing and
    the ``$HOME``-relative entry still applies.
    """
    try:
        resolved = os.path.normpath(os.path.join(str(config_dir()), _POLICY_CACHE_LEAF))
        default = os.path.normpath(
            os.path.join(str(Path.home()), _CREW_HOME_DEFAULT, _POLICY_CACHE_LEAF)
        )
    except Exception:  # pragma: no cover - defensive; a spawn must not fail on this
        logger.debug("could not resolve the policy-cache path for sandbox masking", exc_info=True)
        return []
    return [] if resolved == default else [resolved]


# CC mode: files to expose read-only inside otherwise-hidden dirs.
# After hiding the parent dir, these are recreated with original content.
_CC_EXPOSE_FILES: list[str] = [
    ".aws/config",
]

# CC mode: individual sensitive files that aren't inside the hidden dirs above.
# These require file-level (not directory-level) sandbox enforcement.
_CC_FILES: list[str] = [
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    # Kiro Crew's channel-credential file. The data home moved to ~/.kiro/crew,
    # so the live .env is now ~/.kiro/crew/.env; the legacy ~/.kirocrew/.env is
    # kept covered too (a not-yet-migrated box still holds real secret bytes).
    ".kiro/crew/.env",
    ".kirocrew/.env",
]


# Sensitive env var prefixes to scrub from the child environment.
# Scrubbed in ALL modes (standard + strict) — credential_process reads
# from ~/.aws/config, not env vars, so scrubbing is always safe.
#
# NOTE (applies to this list AND ``_AGENT_DENIED_ENV_KEYS`` below): inherited
# ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` variables must keep flowing to
# claude-harness children — the gateway's env-passthrough contract in
# docs/system-specs/modules/claude-code-provider.md (its env-passthrough
# section). Adding either namespace here silently breaks the custom-endpoint
# auth documented in docs/guides/custom-llm-backend.md.
_SENSITIVE_ENV_PREFIXES: list[str] = [
    "AWS_SECRET",
    "AWS_SESSION",
    "SSH_AUTH_SOCK",
    "GNUPGHOME",
    "GIT_ASKPASS",
]

# Python interpreter env that must NOT leak into a *foreign* Python subprocess
# launched under the sandbox (e.g. the MCP servers kiro-cli spawns, such as
# ord-mcp, which bundle their own interpreter + deps, or any Python the agent's
# shell runs).
#  - PYTHONPATH / PYTHONHOME: Kiro Crew's runtime may export PYTHONPATH
#    pointing at its own site-packages; a foreign server that inherits it
#    prepends Kiro Crew's site-packages to sys.path and imports Kiro Crew's
#    fastmcp/cryptography instead of its own -> ABI collision + init hang.
#  - PYTHONPYCACHEPREFIX: the packaged desktop app exports it at
#    ``<data home>/cache/pycache`` so the embedded interpreter keeps bytecode
#    out of the signed bundle. Inherited into the agent subtree, every foreign
#    interpreter (uv-managed pythons, ephemeral venvs the agent's bash spawns)
#    mirrors its whole stdlib + site-packages under the crew home instead of
#    writing ``__pycache__`` beside its own sources; each ephemeral root mints
#    a fresh path-keyed mirror, so the cache grows without bound (multi-GB per
#    day under heavy subagent use). ``pycache_gc.prune_pycache`` bounds what
#    the gateway's own tree still writes there.
#  - PYTHONDONTWRITEBYTECODE: the packaged macOS app exports it instead of the
#    prefix (its tree ships fully precompiled, see gateway-env.js), so the
#    bundled interpreter never writes into the sealed .app. It is a statement
#    about OUR bundle, not about the user's Python: inherited by a foreign
#    interpreter it silently disables bytecode caching for the user's own
#    projects -- pytest's assertion rewriter falls back to rewriting in memory
#    on every run, for one -- which is a slowdown nobody asked for and would
#    struggle to attribute to the desktop app.
# Stripped ONLY when the caller passes ``strip_python_env=True`` (the
# kiro-cli / agent spawn path). It is deliberately NOT part of
# ``_SENSITIVE_ENV_PREFIXES`` because Kiro Crew's OWN sandboxed Python
# subprocesses (cron scripts, app backends, code-review workers) import
# ``kiro_crew`` via PYTHONPATH -- and on the packaged app run the BUNDLED
# interpreter, which must keep the bytecode settings so it never writes into
# the signed bundle -- so both would break if stripped.
_PYTHON_ENV_PREFIXES: list[str] = [
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONPYCACHEPREFIX",
    "PYTHONDONTWRITEBYTECODE",
]

# Gateway-owned credentials must never reach agent-influenced subprocesses.
# This list feeds the cc/strict launcher scrub, the always-on ``scrub_env``
# parent scrub, and the narrower ``scrub_agent_denied_env`` compatibility helper.
# ACP spawn paths use ``scrub_agent_subprocess_env`` so Windows Kiro delegation
# has the same parent-side scrub as the POSIX sandbox launchers. Loader coverage
# is pinned by regression test.
_AGENT_DENIED_ENV_KEYS: list[str] = [
    # The ACP frame recorder's switch. A child agent that inherited it (a
    # nested Kiro Crew, or any tool that honours the variable) would record
    # its own frames into the SAME per-backend file, interleaving another
    # process's transcript with this gateway's. The recorder is a gateway-side
    # development aid; the children it observes must not see the switch.
    "KIROCREW_ACP_RECORD_FRAMES",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_USER_TOKEN",
    "WECOM_BOT_ID",
    "WECOM_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "WEBEX_BOT_TOKEN",
    "MICROSOFT_APP_ID",
    "MICROSOFT_APP_PASSWORD",
    "MICROSOFT_APP_TENANT_ID",
    "WEIXIN_TOKEN",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "JIRA_API_TOKEN",
    "JIRA_TOKEN_",
    "AZURE_DEVOPS_EXT_PAT",
    "BITBUCKET_EMAIL",
    "BITBUCKET_API_TOKEN",
    "KIROCREW_OWNER_ID",
    # The central-governance fetch configuration — see
    # ``platform/policy_distribution_source.py``. The URL is listed as well as the header,
    # deliberately:
    #
    # * ``KIROCREW_POLICY_HEADERS`` is a live bearer credential for the fleet's own
    #   control plane, and with it an agent could read the ceiling document that the
    #   ``is_sensitive_path`` keystone exists to keep it from reading on disk;
    # * ``KIROCREW_POLICY_URL`` is credential-bearing in its own right whenever the
    #   fleet uses a pre-signed object URL, where the signature rides in the query
    #   string — and even unsigned it names the control plane, which the SEL, the
    #   policy viewer and ``RefreshOutcome.detail`` all deliberately withhold.
    #
    # Spelled as CONCRETE NAMES rather than a ``KIROCREW_POLICY_`` prefix, because this
    # list has consumers with two different matching rules: the spawn scrubs here use
    # ``startswith``, but ``cron_script._CRON_ENV_DENY`` tests exact membership and
    # ``mcp_cron`` builds ``\b``-anchored regexes from it, and a prefix entry silently
    # matches nothing in either. ``test_governance_distribution`` pins these against
    # ``POLICY_DISTRIBUTION_ENV_VARS``, which owns the set, so a variable added there
    # cannot quietly stay agent-readable.
    "KIROCREW_POLICY_URL",
    "KIROCREW_POLICY_HEADERS",
    "KIROCREW_POLICY_REFRESH_SECS",
    "KIROCREW_POLICY_TIMEOUT_SECS",
    "KIROCREW_POLICY_MAX_CACHE_AGE_SECS",
    "KIROCREW_POLICY_ON_UNAVAILABLE",
    "KIROCREW_POLICY_CACHE_ONLY",
]


# ── Platform context accessor ──


def _sandbox_policy():
    """Return the active context's SandboxPolicy adapter.

    The Default adapter delegates to ``_STRICT_DIRS`` / ``_CC_DIRS`` above, so a
    standalone process gets today's exact lists; the internal companion extends
    them.
    """
    return current_context().sandbox


# Fallback tier for configured_sandbox_mode() when the config cannot be read.
# "auto" (= standard), matching wrap_argv's own default: an unreadable config
# must not be a way to obtain a LOOSER sandbox than the operator configured.
_SANDBOX_MODE_FALLBACK = "auto"


def configured_sandbox_mode() -> str:
    """The operator's ``agent.sandbox`` tier, for one-shot kiro-cli spawns.

    ``wrap_argv``'s ``mode`` parameter defaults to ``"auto"``, which coincides
    with the shipped ``agent.sandbox`` default but ignores what the operator
    actually configured. Where ``agent.sandbox`` is an explicit ``"off"`` —
    isolation deferred to kiro-cli's own internal sandbox, which cannot nest
    inside Kiro Crew's (macOS Seatbelt returns EPERM) — a spawn that takes the
    parameter default asks for a STRICTER tier than the operator configured. On
    a backend-less host an unclassified spawn then fail-closes while a delegated
    Kiro chat path can run; the reviewed Windows Kiro sites carry explicit
    classification, but keeping the configured tier remains the cross-platform rule.

    Passing the configured value is what keeps a one-shot read from being
    stricter than the long-lived session it accompanies; it can never make it
    looser, because both resolve the same key.

    The interactive ACP spawns already thread the configured mode through their
    ``sandbox_mode`` constructor argument. The one-shot ``kiro-cli`` reads
    (``--list-models``, ``whoami``, the ``/usage`` scrape) have no such plumbing,
    so they call this instead of relying on the parameter default. Use it for a
    spawn of the SAME binary under the SAME posture as chat; it is deliberately
    not for spawns that pin their own tier on purpose (the prerequisite probes'
    ``strict``, the credential-free registry clones).

    Read lazily, like the two opt-in predicates above, to avoid an import cycle
    with the config loader. Falls back to :data:`_SANDBOX_MODE_FALLBACK` so an
    unreadable config cannot silently loosen isolation. Governance still clamps
    the result UP inside ``wrap_argv`` (:func:`_clamp_sandbox_mode`), so an
    enterprise ``sandbox.min_level`` floor overrides this value as it does any
    other caller-supplied mode.
    """
    try:
        from kiro_crew.config.loader import (
            KiroCrewConfig,  # circular import: sandbox is a low-level dep of config.loader
        )

        return str(getattr(KiroCrewConfig.load().agent, "sandbox", _SANDBOX_MODE_FALLBACK))
    except Exception:
        logger.warning(
            "Could not read agent.sandbox; using %r for this spawn", _SANDBOX_MODE_FALLBACK
        )
        return _SANDBOX_MODE_FALLBACK


# The single environment marker that proves this process is already INSIDE a
# Kiro Crew namespace sandbox. Deny-by-default: the gate keys ONLY on the
# explicit, single-purpose ``KIROCREW_SANDBOX_ACTIVE``, which is exported at
# exactly one site — the namespace launcher's ``scrub_env`` stage
# (``sandbox_launcher_parts/exec_lockdown.py``, which ``_build_launcher_script`` renders). We
# deliberately do NOT key on ``KIROCREW_HOST_PID``:
# it is dual-purpose session-identity plumbing, and gating a security-relevant
# passthrough on a variable set for other reasons is a latent bypass. Since the
# launcher sets ``KIROCREW_SANDBOX_ACTIVE`` at the same site, no fallback marker
# is needed. No unsandboxed code path sets this marker.
#
# Two sites set it, each immediately after applying that platform's credential-env
# scrub: the Linux namespace launcher's ``scrub_env`` stage (after its ``env_prefixes``
# loop) and the macOS ``env`` prefix built by :func:`sandbox_exec_argv` (after its
# ``env -u`` flags, derived from the SAME prefix lists — see
# :func:`_sandbox_env_unset_args`). A marked process therefore always has an
# environment Kiro Crew already sanitised, which is what makes the passthrough
# below safe for callers that use ``wrap_argv`` directly rather than
# ``sandboxed_spawn_argv``.
_IN_SANDBOX_MARKER = "KIROCREW_SANDBOX_ACTIVE"

# Companion to ``_IN_SANDBOX_MARKER``: the single name for the systemd slice every
# agent scope nests under (``kirocrew.slice/kirocrew-agents.slice`` inside the user
# manager), and the aggregate enforcement boundary (``ensure_agents_slice_limits``).
# It lives here, in the lowest sandbox layer, so BOTH the confinement-evidence probe
# in ``sandbox_unavailable`` and the scope machinery in ``sandbox_cgroup`` read one
# constant rather than repeating the literal — a renamed slice moves in one place.
_CGROUP_AGENTS_SLICE = "kirocrew-agents.slice"

# Companion to ``_IN_SANDBOX_MARKER``: records WHICH tier the outer sandbox was
# built at (``standard``/``cc``/``strict``), exported at the same two launcher
# sites and with the same non-droppable placement (after each platform's env
# scrub / ``-u`` flags). The marker alone proves "a Kiro Crew sandbox is active"
# but not its tier; without this record the nested passthrough is tier-blind —
# an in-sandbox caller requesting ``strict`` under a ``standard`` outer sandbox
# silently runs at ``standard``. The passthrough compares this against the
# requested tier so a downgrade is audited and warned about rather than
# invisible. Absent for trees launched by an older build — readers treat that
# as ``unknown``.
_IN_SANDBOX_LEVEL_VAR = "KIROCREW_SANDBOX_LEVEL"

# Confinement ordering for downgrade detection: a request is a downgrade only
# when its ordinal exceeds the active tier's. ``unknown`` (absent/unrecognized
# level var) is deliberately NOT in this table: it carries no ordinal claim, so
# no downgrade can be *proven* against it.
_TIER_ORDINALS: dict[str, int] = {"standard": 1, "cc": 2, "strict": 3}


def _mode_to_level(mode: str) -> str:
    """Map a ``wrap_argv`` mode to the sandbox tier it resolves to.

    ``"auto"``/``"standard"`` (and anything unrecognized) resolve to
    ``standard``; ``"cc"`` and ``"strict"`` map to themselves. Shared by the
    nested-passthrough tier comparison and the backend-wrap level resolution so
    the two sites can never diverge.
    """
    if mode == "strict":
        return "strict"
    if mode == "cc":
        return "cc"
    return "standard"


# wrap_argv's ``mode`` vocabulary is a superset of the governance ``sandbox``
# ordinal scale: ``auto`` is an alias that resolves to ``standard`` below.  Only
# this alias mapping lives here; the strictness ORDER is owned solely by
# governance._ORDINAL_SCALES["sandbox"] (the single source of truth) — we never
# re-encode the order, so a new tier added there is honoured here without edit.
_SANDBOX_MODE_ALIASES = {"auto": "standard"}


def effective_sandbox_mode(mode: str) -> str:
    """The tier :func:`wrap_argv` would ACTUALLY apply for *mode* on this host.

    Applies the governed ``sandbox.min_level`` clamp, so a caller whose security
    argument depends on the sandbox being on can ask whether it will be instead of
    trusting the raw config value -- which a governance floor may silently raise.
    Read-only: same clamp, no spawn, no side effects.
    """
    return _clamp_sandbox_mode_to_floor(mode, _governance_sandbox_floor())


def _governance_sandbox_floor() -> str | None:
    """Read the governed ``sandbox.min_level`` floor, or ``None`` when ungoverned.

    ``wrap_argv`` performs this read ONCE per call and reuses the value for
    both the mode clamp and the first-party carve-out condition, so the two can
    never disagree about whether the same host is governed and the (potentially
    profile-walking) resolve is never duplicated.

    Error posture (every caller inherits it): a ``PlatformCompositionError`` (a
    non-standalone host that could not compose) propagates — the sandbox floor
    must never silently downgrade from DENY to ALLOW on the very host that is
    supposed to be governed.  Any OTHER (transient) error reads as "floor
    absent" (a missing tighten is backstopped by the always-on controls).
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import governance_floor_ordinal

        return governance_floor_ordinal("sandbox.min_level")
    except PlatformCompositionError:
        raise
    except Exception:
        return None


def _clamp_sandbox_mode(mode: str) -> str:
    """Read the governance floor and clamp *mode* up to it.

    Convenience wrapper preserving the read-then-clamp contract for callers and
    tests; :func:`wrap_argv` reads the floor itself (once) and calls
    :func:`_clamp_sandbox_mode_to_floor` directly.
    """
    return _clamp_sandbox_mode_to_floor(mode, _governance_sandbox_floor())


def _floor_mandates_sandbox(floor: str | None) -> bool:
    """True when an already-read ``sandbox.min_level`` *floor* requires isolation.

    ``None`` means ungoverned.  A governed floor at the LOOSEST tier is a policy
    that explicitly requires nothing, so testing the raw string for truthiness
    would read "no isolation required" as "isolation mandatory" and refuse a
    spawn the operator legitimately opted into — while telling them a floor of
    ``off`` forbids unsandboxed execution.

    The loosest tier is derived from the enforcer-owned ordinal registry rather
    than hardcoded, matching :func:`_clamp_sandbox_mode_to_floor`: a renamed or
    re-ordered scale must not silently invert this test.
    """
    if not floor:
        return False
    from kiro_crew.platform.governance import _ORDINAL_SCALES

    return floor != _ORDINAL_SCALES["sandbox"][0]


def _clamp_sandbox_mode_to_floor(mode: str, floor: str | None) -> str:
    """Clamp *mode* UP to an already-read ``sandbox.min_level`` *floor*, if any.

    Derives strictness ranking from the enforcer-owned ordinal registry
    (``OrdinalControl`` over ``_ORDINAL_SCALES['sandbox']``) — NOT a private
    duplicate table — so the floor cannot silently no-op if a tier is added to
    the scale.  Returns *mode* unchanged when there is no governance opinion or
    the floor is already satisfied.

    Fail-closed posture lives in the READ (:func:`_governance_sandbox_floor`):
    a ``PlatformCompositionError`` propagates, any other (transient) error
    reads as "floor absent".  Here, an unknown floor/mode value raises rather
    than ranking it as 0 (which would fail open).
    """
    from kiro_crew.edition_locks import sandbox_mode_floor
    from kiro_crew.platform.governance import _ORDINAL_SCALES, OrdinalControl

    # Enterprise: ``off`` reads as the shipped default tier, so no caller-supplied
    # mode goes below Crew's own sandbox (RFC enterprise edition, Red "Minimum
    # sandbox level"). The governance floor below still clamps it further up.
    mode = sandbox_mode_floor(mode, _SANDBOX_MODE_FALLBACK)
    if not floor:
        return mode
    scale = _ORDINAL_SCALES["sandbox"]
    # The floor already validated through OrdinalControl inside
    # governance_floor_ordinal, so it is in-scale; an unrecognised caller mode is
    # treated as the loosest tier so the floor still clamps it UP (fail-closed —
    # never let an unknown mode skip the tighten).
    cur_value = _SANDBOX_MODE_ALIASES.get(mode, mode)
    floor_rank = OrdinalControl("sandbox", floor).rank()
    cur_rank = scale.index(cur_value) if cur_value in scale else -1
    if floor_rank <= cur_rank:
        return mode
    # The floor's scale value IS a valid wrap_argv mode (off/standard/cc/strict).
    return floor
