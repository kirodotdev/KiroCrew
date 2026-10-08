"""The macOS Seatbelt profile a sandboxed agent spawn runs under.

:func:`render_seatbelt_profile` renders a :class:`~kiro_crew.sandbox_plan.ConfinementPlan`
as ``sandbox-exec`` profile text: a read deny over each masked tree (with the private
windows and the exposed files carved back out), write and hardlink denies over the trees
whose writes matter, and the approved write carve-outs last, because Seatbelt is
last-match-wins between an allow and a deny. What is masked, sealed or re-opened is the
plan's decision; this module only spells it as rules. ``kiro_crew.sandbox`` plans the
spawn, writes the text to ``<config_dir>/run`` and wraps the command in
``sandbox-exec -f <profile>`` (``sandbox_exec_argv``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.sandbox_plan import ConfinementPlan


_SEATBELT_PROFILE = """\
(version 1)
(allow default)
{deny_rules}
"""


def _quoted(path: str) -> str:
    """*path* with its double quotes escaped for a ``"..."`` profile string."""
    return path.replace('"', '\\"')


def render_seatbelt_profile(plan: ConfinementPlan) -> str:
    """Render *plan* as a Seatbelt profile.

    Rule order is part of the contract: Seatbelt is deny-wins across deny rules and
    last-match-wins between an allow and a deny, so the write carve-outs come last and
    override only the runtime parent's write seal.
    """
    rules: list[str] = []
    for mask in plan.masks:
        if mask.origin != "tier":
            continue
        if mask.windows:
            # A private window (the spawn's own scratch) inside a masked tree: deny the
            # tree except the window, in every direction, so siblings stay hidden while
            # the process keeps read-write on its own directory.
            exceptions = " ".join(f"(require-not (subpath {json.dumps(w)}))" for w in mask.windows)
            predicate = f"(require-all (subpath {json.dumps(mask.path)}) {exceptions})"
            for operation in ("file-read*", "file-write*", "file-link"):
                rules.append(f"(deny {operation} {predicate})")
            # ...but stat on the masked directories ABOVE each window stays allowed:
            # ``realpath`` of the window lstat()s every component, so a harness that
            # canonicalizes its $TMPDIR would otherwise fail on its own window. Metadata
            # only, literal paths only: no sibling becomes listable or readable.
            for ancestor in mask.window_ancestors:
                rules.append(f"(allow file-read-metadata (literal {json.dumps(ancestor)}))")
            continue
        if mask.cancelled:
            # A lifted governance cache stays READ-only: the write and hardlink denies
            # hold and only the read deny is dropped. Mirrors readonly_dirs on Linux.
            if mask.read_only_when_cancelled:
                sealed = _quoted(mask.path)
                rules.append(f'(deny file-write* (subpath "{sealed}"))')
                rules.append(f'(deny file-link (subpath "{sealed}"))')
            continue
        escaped = _quoted(mask.path)
        if mask.exposed:
            exceptions = " ".join(f'(require-not (literal "{_quoted(f)}"))' for f in mask.exposed)
            rules.append(f'(deny file-read* (require-all (subpath "{escaped}") {exceptions}))')
        else:
            rules.append(f'(deny file-read* (subpath "{escaped}"))')
        if mask.write_sealed:
            # Linux bind-masks these roots away, which blocks both directions; macOS
            # needs an explicit write deny as well. Governance metadata is a trust root,
            # a writable voice-runtime image would race the gateway's decoder spawn, and
            # a crew-home secret that is read-denied but writable can still be
            # OVERWRITTEN. Not every mask: ``.aws`` is rewritten legitimately when a tool
            # refreshes a cached token.
            rules.append(f'(deny file-write* (subpath "{escaped}"))')
            if mask.literal_write_sealed:
                # A leaf may be a plain file; whether a subpath rule alone reaches a
                # plain-file anchor is not checked against the kernel, so emit the literal
                # too -- redundant if subpath covers files, load-bearing if it does not.
                rules.append(f'(deny file-write* (literal "{escaped}"))')
        # Deny creating a HARDLINK whose target is under this dir: ``file-read*`` is
        # path-based, so a hardlink at a non-denied path would read the same inode past
        # the deny. ``file-link`` fires on the link TARGET.
        rules.append(f'(deny file-link (subpath "{escaped}"))')
        # A file-shaped leaf also takes the LITERAL read and hardlink denies, matching the
        # literal write deny above and the ``file-read*``/``file-link`` literal forms the
        # caller-origin masks below already emit. Whether a subpath rule alone covers a
        # plain-file anchor has never been checked against the kernel, so the three
        # directions emit the same pair of spellings rather than trusting subpath to reach
        # the leaf for read and link but not write: the literal is redundant if subpath
        # covers files and load-bearing if it does not. Only when there is no read
        # carve-out (a crew-secret leaf has none), so the literal cannot re-open an exposed
        # file the subpath require-all above deliberately spared.
        if mask.literal_write_sealed and not mask.exposed:
            rules.append(f'(deny file-read* (literal "{escaped}"))')
            rules.append(f'(deny file-link (literal "{escaped}"))')

    # The voice image lives below ``run``. That parent stays readable (the launcher is
    # stored there), but every write through both spellings is denied, and literal
    # ancestor rules stop an agent renaming a parent around the subtree deny.
    for target in plan.runtime_parents:
        escaped = _quoted(target)
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    for target in plan.runtime_ancestor_guards:
        rules.append(f'(deny file-write* (literal "{_quoted(target)}"))')
    # The crew data home's ceilings: readable but never writable. A ceiling may be a
    # file (``literal``) or a directory (``subpath``), and ``file-link`` stops the agent
    # minting a writable alias to the same inode.
    for target in plan.readonly:
        escaped = _quoted(target)
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
    for target in plan.files:
        escaped = _quoted(target)
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        rules.append(f'(deny file-link (literal "{escaped}"))')

    for mask in plan.masks:
        if mask.origin != "caller":
            continue
        if mask.windows:
            # Deny the tree except the window, in every direction: the window is the
            # process's own state, so it stays read-WRITE, while every sibling -- and
            # anything installed into the tree after the profile was built -- stays
            # denied. An exposed file keeps its READ carve-out only.
            window_exceptions = " ".join(
                f"(require-not (subpath {json.dumps(w)}))" for w in mask.windows
            )
            read_exceptions = window_exceptions + "".join(
                f" (require-not (literal {json.dumps(f)}))" for f in mask.exposed
            )
            subpath = f"(subpath {json.dumps(mask.path)})"
            rules.append(f"(deny file-read* (require-all {subpath} {read_exceptions}))")
            for operation in ("file-write*", "file-link"):
                rules.append(f"(deny {operation} (require-all {subpath} {window_exceptions}))")
            for ancestor in mask.window_ancestors:
                rules.append(f"(allow file-read-metadata (literal {json.dumps(ancestor)}))")
            continue
        if mask.cancelled:
            continue
        escaped = _quoted(mask.path)
        if mask.exposed:
            exceptions = " ".join(f'(require-not (literal "{_quoted(f)}"))' for f in mask.exposed)
            rules.append(f'(deny file-read* (require-all (subpath "{escaped}") {exceptions}))')
        else:
            rules.append(f'(deny file-read* (subpath "{escaped}"))')
        rules.append(f'(deny file-write* (subpath "{escaped}"))')
        rules.append(f'(deny file-link (subpath "{escaped}"))')
        # BOTH shapes, because most caller masks are plain FILES: whether a subpath rule
        # alone covers a plain file has never been checked against the kernel, and this
        # mask is the only compensating control for a harness whose passive reads never
        # reach the gate. The literal is redundant if subpath covers files, and
        # load-bearing if it does not.
        rules.append(f'(deny file-read* (literal "{escaped}"))')
        rules.append(f'(deny file-write* (literal "{escaped}"))')
        rules.append(f'(deny file-link (literal "{escaped}"))')

    if plan.hide_ssh:
        # Deny all access except reading known_hosts; hardlinking any key out of the
        # subtree is denied too, with no known_hosts exception.
        ssh_escaped = _quoted(plan.ssh_dir)
        ssh_kh_escaped = _quoted(plan.ssh_known_hosts)
        rules.append(
            f'(deny file-read* (require-all (subpath "{ssh_escaped}")'
            f' (require-not (literal "{ssh_kh_escaped}"))))'
        )
        rules.append(f'(deny file-write* (subpath "{ssh_escaped}"))')
        rules.append(f'(deny file-link (subpath "{ssh_escaped}"))')

    # Write carve-outs, validated against every seal above and emitted LAST. The
    # subtree's ``file-link`` deny stays in force: a probe scratch dir never needs to
    # mint hardlinks, and the deny stops aliasing a sealed inode into the window.
    for spelling in plan.writable:
        rules.append(f'(allow file-write* (subpath "{_quoted(spelling)}"))')

    return _SEATBELT_PROFILE.format(deny_rules="\n".join(rules))
