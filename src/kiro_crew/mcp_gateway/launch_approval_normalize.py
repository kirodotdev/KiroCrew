"""An edition's launch normalizer, for the approval repair of a moved version folder.

Some package managers install each update under a new versioned folder and
rewrite the agent specs that point into it, so a shared server's argument such
as ``--sops <root>/<pkg>/<version>/sops`` changes on every update while the
program does not. An edition that knows such a layout supplies
``McpToolingProvider.launch_token_normalizer``: it maps one launch argument to
a canonical spelling, folding only the version segment it recognises. The
program path is never passed through it, so a launch whose program changed is
still refused. The public default returns the argument unchanged, so core folds
nothing.

The normalizer is consulted ONLY by the gateway's rewrite pass, inside
:meth:`kiro_crew.mcp_gateway.launch_approval.LaunchApprovals.admit`, and by the
operator's own approval writer. It never reaches ``hashing.hash_command``: the
broker stub and gatewayd do not compose the edition, so a fold there would make
the three processes disagree on a launch's digest. The pool key
and gatewayd's approval check stay literal; the repair records the live
launch's literal fingerprint, the way the PATHEXT casing repair does.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

#: Audit reason for a launch admitted because only a normalized token moved.
NORMALIZED_AUDIT_REASON = "approved launch, edition-folded version folder moved"

#: Domain tag, so a normalized digest can never equal a literal ``hash_command``.
_DOMAIN = b"kirocrew-normalized-launch\0"


def _folded_args(args: List[str]) -> Optional[List[Any]]:
    """*args* mapped through the edition's normalizer, or ``None`` when it fails.

    Read through ``getattr`` so an edition provider that lacks the method folds
    nothing. Deferred import: gatewayd imports this module's caller and never
    calls this.
    """
    from kiro_crew.platform.context import current_context, safe_context_call

    def _read() -> Optional[List[Any]]:
        fold = getattr(current_context().mcp_tooling, "launch_token_normalizer", None)
        if not callable(fold):
            return None
        try:
            return [fold(arg) for arg in args]
        except Exception:
            logger.warning("mcp launch approvals: the launch normalizer failed", exc_info=True)
            return None

    return safe_context_call(
        _read,
        fallback=None,
        log_message="mcp launch approvals: launch_token_normalizer lookup failed; folding nothing",
    )


def normalized_command_hash(command: str, args: Sequence[str]) -> Optional[str]:
    """Digest of the literal *command* and the normalized *args*, or ``None``.

    Only the arguments go through the edition's normalizer; the digest covers
    *command* as written, so a launch whose program changed never matches.
    ``None`` when the normalizer changes no argument (the public default never
    does, so nothing gets looser), or when it fails: a normalizer that raises
    or returns a non-string folds nothing.
    """
    literal = list(args)
    folded = _folded_args(literal)
    if folded is None or not all(isinstance(arg, str) for arg in folded) or folded == literal:
        return None
    h = hashlib.sha256(_DOMAIN)
    try:
        for token in (command, *folded):
            h.update(token.encode("utf-8"))
            h.update(b"\0")
    except UnicodeEncodeError:
        return None
    return h.hexdigest()


def _command_part(identity: str) -> str:
    """The command hash of a ``<command hash>:<env hash>`` fingerprint or pair."""
    return identity.partition(":")[0]


def normalized_repair_source(
    approvals: Any,
    stem: str,
    fingerprint: str,
    derived_env_hash: str,
    launch: Optional[Tuple[str, List[str]]],
) -> Optional[Tuple[str, str]]:
    """The approved fingerprint and pair *launch* normalizes onto, or ``None``.

    *approvals* is a ``LaunchApprovals``. The source is an approved pair with the
    same declared and derived env, a stored display, and a stored normalized
    digest equal to the live launch's. The normalizer is the edition's and runs
    in this process only; no agent chooses it.
    """
    if launch is None:
        return None
    digest = normalized_command_hash(str(launch[0]), [str(a) for a in launch[1]])
    if digest is None:
        return None
    env_hash = fingerprint.partition(":")[2]
    for pair, stored in sorted(approvals.approved_normalized.get(stem, {}).items()):
        approved_hash, _, pair_env = pair.partition(":")
        source = f"{approved_hash}:{env_hash}"
        if (
            stored == digest
            and pair_env == derived_env_hash
            and source in approvals.approved.get(stem, ())
            and approvals.admits_launch(stem, approved_hash, derived_env_hash)
            and pair in approvals.approved_displays.get(stem, {})
        ):
            return source, pair
    return None


def retire_normalized_sources(
    approvals: Any,
    stem: str,
    stored_normalized: Mapping[str, str],
    before: Set[str],
    before_pairs: Set[str],
    final_fps: Set[str],
    final_pairs: Set[str],
) -> None:
    """Retire unused pairs that normalize onto a live approved pair with the same env.

    Only a full pass that saw every live launch may retire a pair, and only
    while the store holds no approval the pass did not load or record. Stored
    and learned digests match obsolete versions even when their repairs happened
    in earlier passes. Unrelated approvals and pairs without a digest stay.
    """
    if not approvals.full_pass or approvals.live_incomplete:
        return
    if not (
        before <= approvals.approved.get(stem, set())
        and before_pairs <= approvals.approved_pairs.get(stem, set())
    ):
        return
    live = set(approvals.launch_identities.get(stem, {})) | approvals.live_overflow.get(stem, set())
    normalized = {**stored_normalized, **approvals.normalized_seen.get(stem, {})}
    declared_envs: dict[str, Set[str]] = {}
    for fp in final_fps:
        command, _, env = fp.partition(":")
        declared_envs.setdefault(command, set()).add(env)
    # A command with several declared fingerprints cannot attribute them to pairs.
    # Its derived env still covers the declared env of the expanded launch.
    live_pairs = live & final_pairs & normalized.keys()
    retired = {
        pair
        for pair in final_pairs - live
        if pair in normalized
        and any(
            normalized[pair] == normalized[replacement]
            and pair.partition(":")[2] == replacement.partition(":")[2]
            and (
                len(declared_envs.get(_command_part(pair), ())) != 1
                or len(declared_envs.get(_command_part(replacement), ())) != 1
                or declared_envs[_command_part(pair)] == declared_envs[_command_part(replacement)]
            )
            for replacement in live_pairs
        )
    }
    final_pairs.difference_update(retired)
    orphaned_commands = {_command_part(pair) for pair in retired} - {
        _command_part(pair) for pair in final_pairs
    }
    final_fps.difference_update(
        fp for fp in set(final_fps) if _command_part(fp) in orphaned_commands
    )
