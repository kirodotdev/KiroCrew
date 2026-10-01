"""The macOS Seatbelt profile a sandboxed agent spawn runs under.

:func:`_build_seatbelt_profile` renders the ``sandbox-exec`` profile text: a read
deny over each masked tree (with the private windows and the exposed files carved
back out), write and hardlink denies over the trees whose writes matter, and the
approved write carve-outs last, because Seatbelt is last-match-wins between an allow
and a deny. ``kiro_crew.sandbox`` writes the text to ``<config_dir>/run`` and wraps
the command in ``sandbox-exec -f <profile>`` (``sandbox_exec_argv``).

``kiro_crew.sandbox`` re-exports the builder and its template under their old names,
and a patch of either through ``kiro_crew.sandbox`` lands here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The builder reads the plan it renders -- the tier lists, the crew-home leaf tables,
# the relocated and pod targets, the voice-runtime paths and the carve-out validator --
# from ``kiro_crew.sandbox``, imported inside the function, so a test that rebinds one
# of those there reaches the builder at call time and this module holds no second copy
# of the plan. Circular import: ``kiro_crew.sandbox`` imports this module while it
# loads, so the builder cannot import it at the top of this file.


_SEATBELT_PROFILE = """\
(version 1)
(allow default)
{deny_rules}
"""


def _build_seatbelt_profile(
    sandbox_level: str = "strict",
    *,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    extra_private_dirs: tuple[str, ...] = (),
    extra_writable_dirs: tuple[str, ...] = (),
    extra_expose_files: tuple[str, ...] = (),
) -> str:
    """Build a Seatbelt .sb profile denying reads of sensitive dirs."""
    from kiro_crew.sandbox import (
        _CC_EXPOSE_FILES,
        _CC_FILES,
        _CREW_HIDDEN_LEAVES,
        _CREW_READONLY_LEAVES,
        _CREW_READONLY_TARGETS,
        _STANDARD_DIRS,
        _crew_hidden_sandbox_targets,
        _hidden_path_contains_visible_path,
        _is_policy_cache_dir,
        _is_voice_runtime_dir,
        _md_notebook_degraded_mask_dirs,
        _pod_os_home_targets,
        _private_window_spellings,
        _relocated_crew_targets,
        _relocated_policy_cache_dirs,
        _resolved_kiro_agents_targets,
        _sandbox_policy,
        _voice_runtime_ancestor_guards,
        _voice_runtime_parent_paths,
        _voice_runtime_sandbox_paths,
        _writable_carveout_spellings,
    )

    home = str(Path.home())
    # Source the sensitive-dir lists from the active PlatformContext (Default
    # adapter == today's module globals; internal companion adds .midway/.ada).
    if sandbox_level == "standard":
        dirs = _STANDARD_DIRS
    elif sandbox_level == "cc":
        # On macOS, don't hide .aws — credential_process and SSO token
        # caches live under .aws/ and Seatbelt can't do partial exposure
        # as cleanly as Linux bind mounts. ``is_sensitive_path`` still fences
        # the file tools; a spawned shell read is unrefused there, the same
        # trade the standard tier makes. The .aws-exclusion is applied to the
        # context-sourced list so a companion's extra cc dirs are still hidden.
        dirs = [d for d in _sandbox_policy().cc_dirs() if d != ".aws"]
    else:
        dirs = _sandbox_policy().strict_dirs()
    files = _CC_FILES if sandbox_level in ("cc", "strict") else []
    expose_files = _CC_EXPOSE_FILES if sandbox_level == "cc" else []
    expose_abs = {os.path.join(home, f) for f in expose_files}
    # Caller-supplied read-only carve-outs (the enforced adapter's
    # ``~/.aws/config``). Folded into the SAME set the tier loop reads, not only
    # the extra-hidden loop below: under ``strict`` the tier list already
    # carries ``.aws``, so the first loop emits a blanket read deny for it, and a
    # narrower deny emitted later cannot cancel an earlier one -- Seatbelt is
    # deny-wins across deny rules, last-match-wins only between allow and deny.
    # Without this the child authenticates under ``standard``/``cc`` and fails
    # under ``strict`` with the same opaque error the mask itself produced.
    extra_expose_abs = {os.path.abspath(p) for p in extra_expose_files}
    expose_abs |= extra_expose_abs
    crew_hidden = _crew_hidden_sandbox_targets()
    rules: list[str] = []
    # Every masked target below doubles as a guard for the write carve-outs
    # appended at the end: Seatbelt is last-match-wins, so an allow emitted
    # after these denies re-opens whatever it covers, and the carve-out
    # validator must therefore see the same target list the rules were built
    # from (conservatively including entries `extra_visible_dirs` re-exposed).
    masked_targets = (
        [os.path.join(home, d) for d in dirs]
        # Same reason as the launcher builder: a pod child's remapped home holds
        # the seeded SSO token, and no $HOME-relative entry names that tree.
        + _pod_os_home_targets(tuple(dirs))
        + _relocated_policy_cache_dirs()
        + _relocated_crew_targets(_CREW_HIDDEN_LEAVES)
        # Seatbelt needs this for the same reason the Linux launcher does: the sweep
        # cannot delete an orphan through a linked chain on either platform, so the
        # directory mask is what keeps that orphan out of the sandbox. Omitting it here
        # would close the exposure on Linux and leave it open on macOS.
        + _md_notebook_degraded_mask_dirs()
        + list(_voice_runtime_sandbox_paths())
    )
    private_windows = _private_window_spellings(extra_private_dirs, masked_targets)
    for target in masked_targets:
        windows = [w for w in private_windows if w.startswith(target.rstrip("/") + "/")]
        if windows:
            # A private window (the spawn's own scratch) inside a masked tree:
            # deny the tree except the window, in every direction, so siblings
            # stay hidden while the process keeps read-write on its own dir.
            exceptions = " ".join(f"(require-not (subpath {json.dumps(w)}))" for w in windows)
            predicate = f"(require-all (subpath {json.dumps(target)}) {exceptions})"
            for operation in ("file-read*", "file-write*", "file-link"):
                rules.append(f"(deny {operation} {predicate})")
            continue
        if _hidden_path_contains_visible_path(
            target, extra_visible_dirs
        ) and not _is_voice_runtime_dir(target):
            # An exposed governance cache stays READ-only: keep the write and hardlink
            # denies and drop only the read deny. `extra_visible_dirs` otherwise cancels
            # the target's whole rule set, which would hand the one caller that needs to
            # read the ceiling (`apps/backend.py` in cache-only mode) the ability to
            # rewrite it — and the metadata records the source the next boot trusts, so
            # that is the dangerous direction. Mirrors READONLY_DIRS on Linux.
            if _is_policy_cache_dir(target):
                sealed = target.replace('"', '\\"')
                rules.append(f'(deny file-write* (subpath "{sealed}"))')
                rules.append(f'(deny file-link (subpath "{sealed}"))')
            continue
        escaped = target.replace('"', '\\"')
        # Check if any exposed files live under this dir
        exposed_in_dir = [f for f in expose_abs if f.startswith(target + "/")]
        if exposed_in_dir:
            exceptions = " ".join(
                f'(require-not (literal "{f.replace(chr(34), chr(92) + chr(34))}"))'
                for f in exposed_in_dir
            )
            rules.append(f'(deny file-read* (require-all (subpath "{escaped}") {exceptions}))')
        else:
            rules.append(f'(deny file-read* (subpath "{escaped}"))')
        if _is_policy_cache_dir(target) or _is_voice_runtime_dir(target) or target in crew_hidden:
            # Linux bind-mounts these roots away, which blocks both directions.
            # macOS needs an explicit write deny as well as the read rule above:
            # governance metadata is a trust root, a writable voice-runtime image would
            # race the gateway's authenticated decoder spawn, and a crew-home secret that
            # is read-denied but writable can still be OVERWRITTEN -- forging
            # ``token_signing.key`` needs no read at all. Scoped to those three sets on
            # purpose: widening it to every hidden entry would also cover .aws, which a
            # tool rewrites legitimately when it refreshes a cached token.
            rules.append(f'(deny file-write* (subpath "{escaped}"))')
            if target in crew_hidden:
                # A leaf may be a plain file, which no subpath rule addresses.
                rules.append(f'(deny file-write* (literal "{escaped}"))')
        # Deny creating a HARDLINK whose target is under this dir.
        # Seatbelt's file-read* deny is path-based, so a hardlink at a
        # non-denied path (e.g. /tmp) reads the same inode past the deny rule.
        # ``file-link`` fires on the link TARGET, so this stops the sandboxed
        # agent from minting such a hardlink in the first place.  Blanket (no
        # exposed-file exception): the agent never needs to hardlink a
        # credential-dir file, and blocking it is harmless.
        rules.append(f'(deny file-link (subpath "{escaped}"))')

    # The voice image lives below ``run``. Keep that parent readable (the
    # sandbox launcher itself is stored there), but deny every write through
    # both lexical and canonical spellings. Literal ancestor rules prevent a
    # same-UID agent from renaming a parent around the path-based subtree deny.
    runtime_parents = list(_voice_runtime_parent_paths())
    for target in runtime_parents:
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    ancestor_guards = list(_voice_runtime_ancestor_guards())
    for target in ancestor_guards:
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
    # The crew data home's ceilings: readable (in-sandbox code resolves them) but never
    # writable, so a sandboxed process cannot hand itself a ceiling. Mirrors
    # READONLY_DIRS on Linux. Both spellings, because a ceiling may be a file
    # (``literal``) or a directory (``subpath``), and ``file-link`` stops the agent
    # minting a writable alias to the same inode.
    readonly_targets = (
        [os.path.join(home, rel) for rel in _CREW_READONLY_TARGETS]
        + _relocated_crew_targets(_CREW_READONLY_LEAVES)
        + _resolved_kiro_agents_targets()
    )
    for target in readonly_targets:
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    for f in files:
        target = os.path.join(home, f)
        escaped = target.replace('"', '\\"')
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        # Also deny hardlinking the protected file (see above).
        rules.append(f'(deny file-link (literal "{escaped}"))')
    extra_hidden_targets = list(dict.fromkeys(os.path.abspath(path) for path in extra_hidden_dirs))
    # Private windows inside a CALLER's own extra-hidden tree, same primitive and
    # same rule shape as the tier loop above. Both builders must agree about one
    # spawn: ``_build_launcher_script`` extends ``hidden_dirs`` with
    # ``extra_hidden_dirs`` BEFORE it computes ``_private_window_spellings``, so a
    # window inside a caller's own mask is staged and re-bound there, and this
    # builder computes its windows against the caller's targets as well as the
    # TIER ones so the same window survives the blanket denies below. Without the
    # caller's targets a window here is swallowed and the child loses read AND
    # write on its own directory -- fail-closed, so it breaks the spawn rather
    # than exposing anything, but it leaves the primitive enforced on one
    # platform only for the one shape that needs it: a tree masked as a whole
    # with the process's own state kept live inside it. That is the durable-data
    # view an app-bundle cron script needs -- mask ``apps/`` so no sibling app's
    # ``.app_secret`` is reachable, including one installed mid-run, and keep
    # ``apps/<app>/data`` on its real inode at its real path so provisioned
    # dependencies and logs survive the run.
    #
    # Window-first, like the tier loop: a window keeps the tree denied except the
    # one directory, whereas the ``extra_visible_dirs`` check below cancels the
    # tree's whole rule set. When a caller passes both for one tree the narrower
    # answer wins, which is the refusal-leaning direction.
    #
    # Equality is refused by ``_private_window_spellings`` itself (a window equal
    # to its mask would be a mask lift by another name), so every entry here is a
    # PROPER descendant and the ``(literal …)`` denies emitted for the target
    # cannot reach it.
    extra_private_windows = _private_window_spellings(extra_private_dirs, extra_hidden_targets)
    # Read-only carve-outs inside an extra-hidden dir (the enforced adapter's
    # ``~/.aws/config``). READ only: the write and hardlink denies below stay
    # blanket over the subpath, exactly as the ``.ssh/known_hosts`` carve-out
    # further down. A file that sits under no hidden target is ignored -- there
    # is nothing to carve it out of, and emitting an allow for it would be an
    # allow with no deny, which last-match-wins Seatbelt turns into a grant.
    # ``extra_expose_abs`` was built above so the tier loop applies the same
    # carve-out when the tier itself already hides the parent (strict + .aws).
    for target in extra_hidden_targets:
        windows = [w for w in extra_private_windows if w.startswith(target.rstrip("/") + "/")]
        if windows:
            # Deny the tree except the window, in every direction: the window is
            # the process's own state, so it stays read-WRITE (a read-only
            # window would fail the deps swap renames the view exists to keep
            # working), while every sibling -- and anything installed into the
            # tree after the profile was built -- stays denied.
            window_exceptions = " ".join(
                f"(require-not (subpath {json.dumps(w)}))" for w in windows
            )
            # An exposed file under the same tree keeps its READ carve-out; it
            # gets no write or link exception, matching the blanket branch below.
            carved_here = sorted(f for f in extra_expose_abs if f.startswith(target + os.sep))
            read_exceptions = window_exceptions + "".join(
                f" (require-not (literal {json.dumps(f)}))" for f in carved_here
            )
            subpath = f"(subpath {json.dumps(target)})"
            rules.append(f"(deny file-read* (require-all {subpath} {read_exceptions}))")
            for operation in ("file-write*", "file-link"):
                rules.append(f"(deny {operation} (require-all {subpath} {window_exceptions}))")
            continue
        if _hidden_path_contains_visible_path(target, extra_visible_dirs):
            continue
        escaped = target.replace('"', '\\"')
        carved = sorted(f for f in extra_expose_abs if f.startswith(target + os.sep))
        if carved:
            exceptions = " ".join(
                f'(require-not (literal "{f.replace(chr(34), chr(92) + chr(34))}"))' for f in carved
            )
            rules.append(f'(deny file-read* (require-all (subpath "{escaped}") {exceptions}))')
        else:
            rules.append(f'(deny file-read* (subpath "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
        # BOTH shapes, because most of this list is plain FILES, not directories:
        # sandbox_credential_targets() yields .codex/auth.json,
        # .claude/.credentials.json, .netrc, .git-credentials, .npmrc, .pypirc,
        # .docker/config.json, .kube/config, sel_hmac.key, token_signing.key.
        # Whether a subpath rule alone covers a plain file is asserted in three
        # comments in this tree and CONTRADICTED by the crew_hidden branch above
        # ("A leaf may be a plain file, which no subpath rule addresses"), and
        # nothing tests it -- no test in this repo executes sandbox-exec, so the
        # claim has never been checked against the kernel. This mask is the ONLY
        # compensating control for a harness whose passive reads never reach the
        # gate, so it must not rest on an unverified reading of Seatbelt: the
        # literal is redundant if subpath does cover files, and load-bearing if it
        # does not.
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-link (literal "{escaped}"))')

    # .ssh: deny all access except reading known_hosts (strict only)
    ssh_guards: list[str] = []
    if sandbox_level == "strict":
        ssh_dir = os.path.join(home, ".ssh")
        ssh_guards.append(ssh_dir)
        ssh_escaped = ssh_dir.replace('"', '\\"')
        ssh_kh = os.path.join(ssh_dir, "known_hosts")
        ssh_kh_escaped = ssh_kh.replace('"', '\\"')
        rules.append(
            f'(deny file-read* (require-all (subpath "{ssh_escaped}")'
            f' (require-not (literal "{ssh_kh_escaped}"))))'
        )
        rules.append(f'(deny file-write* (subpath "{ssh_escaped}"))')
        # Block hardlinking any .ssh file (private keys) out of the
        # denied subtree.  Blanket over the whole subpath — no known_hosts
        # exception, since a hardlink to known_hosts has no legitimate use.
        rules.append(f'(deny file-link (subpath "{ssh_escaped}"))')

    # Write carve-outs, validated against every seal above and emitted
    # LAST: Seatbelt is last-match-wins, so this allow overrides only the
    # runtime-parent write seal for exactly the approved directory (both
    # spellings — Seatbelt rules are path-based). The subtree's ``file-link``
    # deny deliberately stays in force: a probe scratch dir never needs to mint
    # hardlinks, and the deny is what stops aliasing a sealed inode into the
    # writable window.
    for spelling in _writable_carveout_spellings(
        extra_writable_dirs,
        subtree_guards=masked_targets + readonly_targets + extra_hidden_targets + ssh_guards,
        literal_guards=ancestor_guards + [os.path.join(home, f) for f in files],
        carveable_parents=runtime_parents,
    ):
        escaped = spelling.replace('"', '\\"')
        rules.append(f'(allow file-write* (subpath "{escaped}"))')

    return _SEATBELT_PROFILE.format(deny_rules="\n".join(rules))
