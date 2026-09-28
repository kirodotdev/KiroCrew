"""The persona behind a ``file://`` prompt reference, resolved, fenced and read as one pinned read.

Ported from ``crew_export/spec.py`` and the reader guard in ``serving/smc/bundle.py``
(``validate_prompt``). The UNC and redirect screens run before any resolution, the
sensitive-path fences run on the one resolution, and the bytes are authorised against the
directory those checks pinned, so nothing is re-resolved between a verdict and the read.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path

from . import pinned as _pinned
from . import scan as _scan
from . import sensitive as _sensitive
from .contract import _MAX_PROMPT_BYTES, ExportRefused

_MAX_REDIRECT_HOPS = 8


def _within(path: Path, root: Path) -> bool:
    """Is *path* inside *root*, judged without resolving either side's links."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_prompt_path(raw: str, agents_dir: Path, *, resolved_root: Path | None = None) -> Path:
    target = raw[len("file://") :]
    # A NUL first, ahead of the UNC gate and every path construction below. The target comes
    # from the crew's agent spec, so its bytes are someone else's choice, and Python raises a
    # bare ValueError from the C boundary the moment a NUL-bearing string reaches a syscall:
    # measured, a spec carrying "file://per\x00sona.md" left ValueError uncaught on all three
    # branches -- relative, absolute, and a NUL alone -- and it reached the CLI as a traceback
    # rather than a refusal naming the spec.
    #
    # Checked on the STRING because that is the only place it can be checked. ``Path`` itself
    # accepts the NUL and defers the error to the first syscall, so there is no later point
    # that is both reachable and still able to name the reference.
    if "\x00" in target:
        raise ExportRefused(
            f"the prompt reference {raw!r} contains a NUL byte, which cannot name a file on "
            f"any platform. Fix the reference in the agent spec."
        )
    # Resolved ONCE, here, and reused by every containment question below. Each extra
    # ``.resolve()`` is another chance to follow a link planted since the last one.
    # Guarded like the resolution further down. A cycle in the AGENTS directory itself is
    # reached before either branch below runs: measured, a two-link cycle at ``agents/``
    # raised RuntimeError out of the CLI for a relative target and an absolute one alike.
    # ``resolve()`` reports a loop as OSError(ELOOP) on some libcs and RuntimeError on
    # others, so both are caught.
    # ONE reading of the tree, and the caller may own it. Resolving here as well as in the
    # caller gave the two of them separate answers, and a writable agents directory replaced
    # between the two made both answers self-consistent about DIFFERENT trees: the
    # replacement's anchor and the replacement's persona each passed their own check, and the
    # attacker's bytes were signed into ``agent.json``. A caller that has already resolved the
    # root hands it in, so there is one answer for both of them to be judged against.
    if resolved_root is not None:
        agents_root = resolved_root
    else:
        try:
            agents_root = agents_dir.resolve()
        except (OSError, RuntimeError) as exc:
            raise ExportRefused(
                f"the agents directory {agents_dir} cannot be resolved ({exc}), so a prompt "
                f"reference cannot be judged against it. Check the crew directory for a link "
                f"loop."
            ) from None
    # BEFORE `Path(target)` and before any resolution, because on Windows resolving a
    # UNC path IS the outbound SMB probe -- `hooks.validate_file_path` says exactly that
    # in its own docstring: "the Windows UNC trusted-root gate (BEFORE any resolution --
    # realpath on a UNC path is itself the outbound SMB probe)". An agent spec carrying
    # `file:////attacker/share/persona.md` therefore reached the attacker's host through
    # `path.resolve()` below, ahead of every fence in this function, and a Windows SMB
    # touch hands over an NTLM exchange.
    #
    # The gate is IMPORTED rather than restated. This repo already owns the rule, and a
    # second spelling of it is the mistake this branch has now paid for seven times. The
    # trusted-root allowance comes along with it, so a persona that legitimately lives on
    # a share the operator configured still resolves.
    #
    # nt-scoped to match hooks: on POSIX a leading `//` names no network location, and
    # refusing it here would reject a legitimate absolute path written with a doubled
    # slash while protecting nothing.
    if os.name == "nt":
        # Fail CLOSED when the import is unavailable, which is the standalone venv on
        # Windows. The opposite of the agent-spec fence, and for the opposite reason: there
        # the read is the tool's whole purpose and a coarse local list can answer the
        # question, while here the question is whether resolving this path reaches a host
        # over SMB -- and an unanswerable version of that question is not a reason to
        # resolve it anyway. Refusing costs the operator one copy of the persona; a bare
        # ModuleNotFoundError costs them an uncaught crash mid-build.
        try:
            from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
        except ImportError as exc:
            raise ExportRefused(
                f"cannot judge whether the prompt URI {raw!r} names a UNC path, because "
                f"kiro_crew.hooks is not importable here ({exc}). Resolving it could reach "
                f"a host over SMB before any check runs, so it is refused rather than "
                f"resolved unchecked. Copy the persona next to the agent spec and reference "
                f"it by name, or run this build where kiro_crew is installed."
            ) from exc

        if is_unc_shape(target) and not unc_probe_allowed(target):
            raise ExportRefused(
                f"prompt URI {raw!r} is a UNC path outside the trusted roots. Resolving "
                f"it would reach that host over SMB before this build could check "
                f"anything about it, and a Windows SMB touch carries an NTLM exchange. "
                f"Copy the persona next to the agent spec and reference it by name."
            )
    path = Path(target)
    if not path.is_absolute():
        # The UNRESOLVED chain is checked BEFORE ``resolve()``, because resolve is itself the
        # traversal. Two things were wrong with checking afterwards.
        #
        # First, resolve() on Windows follows a reparse point, and following one that points
        # at a share IS the outbound SMB probe with its NTLM exchange. The UNC gate above
        # only sees a UNC path written literally in the target string, so a junction reaching
        # the same host was not covered by it and the probe happened before any fence ran.
        #
        # Second, resolve() COLLAPSES the links, so a check placed after it inspects the
        # targets and cannot see that a link was ever there. An implementation of
        # this branch walked the components of the resolved path looking for reparse points
        # and could never have found one; it passed its own tests only because those called
        # it directly with an unresolved path, which is not what this call site hands it.
        _pinned._refuse_redirects_in_chain(agents_dir, target)
        try:
            path = (agents_dir / target).resolve()
        except (OSError, RuntimeError) as exc:
            raise ExportRefused(
                f"prompt reference {raw!r} cannot be resolved ({exc}). Point it at the "
                f"persona file itself rather than through a link loop."
            ) from None
        try:
            path.relative_to(agents_root)
        except ValueError:
            raise ExportRefused(f"prompt URI {raw!r} escapes the agents directory") from None
    elif os.name == "nt":
        # On the absolute branch the fence is NOT a ban on links.
        #
        # A symlink at the prompt path is a SUPPORTED case: the design permits a persona
        # outside the agents directory and protects it by checking the RESOLVED target against
        # this repository's sensitive-path fence, which
        # ``test_a_symlink_to_a_legitimate_persona_still_works`` pins. Walking the absolute
        # path and refusing every redirect was tried and it reddened that test plus four more
        # -- it protected the supported case out of existence.
        #
        # What the relative branch's walk buys that the target check cannot is narrower than it
        # looks: on Windows, ``resolve()`` following a reparse point that names a SHARE is
        # itself the outbound SMB probe, carrying an NTLM exchange before any fence has read
        # anything. The UNC gate above only sees a share written literally in the target
        # string, so a reparse point reaching one is the gap -- and it is the only gap, because
        # everything else a redirect can do is caught by the target check after resolution.
        #
        # So the components are read with ``readlink``, which does NOT traverse, and only a
        # redirect whose target has UNC shape is refused. nt-scoped because there is no such
        # probe elsewhere: on POSIX a leading ``//`` names no network location, which is the
        # same reason the UNC gate above is nt-scoped.
        # Imported bare, and that is deliberate. The nt branch at the top of this function
        # imports the same module unconditionally and refuses when it is unavailable, so any
        # call that reaches HERE has already proven the import succeeds. A second try/except
        # would be a guard no input can trigger: an ImportError case that cannot happen reads
        # as protection while testing nothing, and one was written here and removed after a
        # mutation showed every test still passed with it gone.
        from kiro_crew.hooks import is_unc_shape as _unc

        probe = Path(path.anchor)
        for part in path.relative_to(path.anchor).parts:
            probe = probe / part
            if not _pinned._is_redirecting_entry(probe):
                continue
            # The whole CHAIN, not just the first hop. Checking only the immediate target
            # left link -> link -> share open: the first readlink returns a local path, the
            # UNC test says no, and ``resolve()`` then follows the rest of the chain to the
            # share anyway. One hop is not a fence when hops compose.
            #
            # ``readlink`` is used rather than ``resolve()`` on purpose: it reads the link's
            # own contents and traverses nothing, so walking the chain by hand never performs
            # the probe this exists to prevent. Bounded at _MAX_REDIRECT_HOPS because a link
            # cycle would otherwise spin here; a chain that long is refused rather than
            # followed further, since anything needing that many hops is not a persona path.
            hop = probe
            for _ in range(_MAX_REDIRECT_HOPS):
                try:
                    dest = os.readlink(hop)
                except OSError as exc:
                    if exc.errno in (errno.EINVAL, errno.ENOENT):
                        # Not a link, or nothing there: the ordinary end of the walk.
                        break
                    # Anything else means this hop EXISTS and could not be inspected, which
                    # is not the same fact. Breaking on it would end the redirect walk early
                    # and let the resolution below follow a hop nothing had judged.
                    raise ExportRefused(
                        f"{hop} on the path to the prompt file could not be inspected "
                        f"({exc}), so whether it redirects is unknown. Fix its permissions "
                        f"or copy the persona next to the agent spec."
                    ) from None
                if _unc(str(dest)):
                    raise ExportRefused(
                        f"{probe} on the path to the prompt file redirects to {dest!r}, which "
                        f"names a network share. Resolving this path would reach that host "
                        f"over SMB before anything could be checked, and a Windows SMB touch "
                        f"carries an NTLM exchange. Copy the persona next to the agent spec."
                    )
                nxt = Path(dest)
                hop = nxt if nxt.is_absolute() else hop.parent / nxt
                # This hop came out of a link's CONTENTS, so nothing has walked the path
                # that reaches it. ``lstat`` on it crosses whatever its ancestors are, and
                # a junction among them naming a share is the outbound SMB touch with its
                # NTLM exchange -- the thing this whole walk exists to avoid, reached by a
                # path the walk never judged. Screen the ancestors first, from the hop's own
                # anchor down, where each ``lstat`` only crosses components already cleared.
                _refuse_share_reached_through_ancestors(hop)
                if not _pinned._is_redirecting_entry(hop):
                    break
            else:
                raise ExportRefused(
                    f"{probe} on the path to the prompt file starts a chain of more than "
                    f"{_MAX_REDIRECT_HOPS} redirects. Where it ends cannot be established "
                    f"without following it, which is the thing this check exists to avoid. "
                    f"Copy the persona next to the agent spec."
                )
    # ONE resolution, and every check below runs on its result. An earlier version
    # resolved the target for the credential fences but left this pseudo-filesystem
    # loop testing the path as written, so a symlink to /proc/self/environ passed
    # all three: the link is not under /proc, and /proc is not a credential
    # location. The read then followed the link and inlined the deploy process's
    # environment into the shipped prompt, where scan_text catches only
    # credential-SHAPED text and a secret in another format survives.
    #
    # Containment under agents_dir is deliberately NOT required: an absolute
    # persona path outside that directory is a supported case with its own test.
    #
    # ``resolved`` is a DISTINCT name rather than a reassignment of ``target``.
    # The two are different things -- the URI as written versus what it points at
    # -- and collapsing them into one name is how the symlink bug above was
    # written in the first place: every check read ``target`` and it was not
    # obvious which of the two any given line meant. mypy rejects the reassignment
    # outright (``target`` is the ``str`` sliced off ``raw``), which is the type
    # checker naming the same problem.
    # A symlink cycle DOES reach this line, and only on one of the two paths in. The chain
    # walk that catches a -> b -> a runs in the RELATIVE branch above; an absolute
    # ``file://`` target skips it and arrives here with the cycle intact, where ``resolve()``
    # raises ``RuntimeError`` (glibc ELOOP) straight out of the CLI as a traceback. Measured
    # -- an absolute two-link cycle produced ``RuntimeError: Symlink loop from ...``.
    #
    # An earlier guard here WAS removed as unreachable, and that judgement was right about
    # the case it was tested on and wrong about this one: the cycle test it came with used a
    # relative target, so the chain walk answered first and the guard looked dead.
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        raise ExportRefused(
            f"prompt URI {raw!r} cannot be resolved: its path leads through a symlink "
            f"loop. Point the prompt at the persona file itself."
        ) from None
    posix = resolved.as_posix()
    for root in ("/proc", "/sys", "/dev"):
        if posix == root or posix.startswith(root + "/"):
            raise ExportRefused(
                f"prompt URI {raw!r} resolves to {resolved}, inside a "
                f"pseudo-filesystem. Those files are process and kernel state, not "
                f"a persona, and one of them is this deploy process's own "
                f"environment."
            )
    # The repo's own fence, when this module can reach it. The local predicates
    # below are a deliberate self-contained subset, and three review passes in a
    # row found one more thing that subset does not name (a kubeconfig, then a
    # symlink, then a git credential store). A denylist needing a new entry per
    # review pass is the wrong shape here, so prefer the shared implementation
    # and keep the local pair as the fallback that preserves this module's ability
    # to run without kiro_crew importable.
    try:
        from kiro_crew.security import is_sensitive_path

        _shared_fence: Callable[[str], bool] | None = is_sensitive_path
    except Exception:
        _shared_fence = None
    # FAIL CLOSED when the shared fence is unreachable, rather than continuing on the local
    # subset. The fallback was written to preserve this module's ability to run without
    # ``kiro_crew`` importable, and that intent is fine -- but the thing it falls back to is
    # a denylist that three consecutive review passes each found one more hole in (a
    # kubeconfig, a symlink, a git credential store). Continuing on it means an environment
    # where the import fails is an environment where ``file://~/.git-credentials`` is read
    # and bundled, and nothing in the output says the weaker check was the one that ran.
    #
    # An EXTERNAL prompt reference is the only thing this gates, so the refusal costs a
    # feature that reaches outside the crew directory, not the ordinary case. A crew whose
    # prompt is inline, or a file beside the spec, is unaffected.
    if _shared_fence is None:
        raise ExportRefused(
            f"cannot check whether prompt URI {raw!r} points at sensitive material: this "
            f"repository's own path fence (kiro_crew.security.is_sensitive_path) is not "
            f"importable here. The local checks below are a deliberate subset and have "
            f"been found short three times, so an external prompt reference is refused "
            f"rather than judged by them. Inline the prompt, or run where kiro_crew "
            f"is importable."
        )
    if _shared_fence(posix):
        raise ExportRefused(
            f"prompt URI {raw!r} resolves to {resolved}, which this repository "
            f"treats as a sensitive path. A prompt may reference an agent persona, "
            f"not credential or key material."
        )
    if _sensitive.refused_by_name(resolved) or _sensitive.refused_by_name(path):
        raise ExportRefused(f"prompt URI {raw!r} points at a credential location")
    if _sensitive.refused_by_location(resolved) or _sensitive.refused_by_location(path):
        raise ExportRefused(
            f"prompt URI {raw!r} resolves to {resolved}, inside a credential "
            f"directory; the file is not read. Its contents cannot be trusted to "
            f"be scannable (a kubeconfig's certificate is base64 and may match no "
            f"credential pattern), so it is refused before any read rather than "
            f"read and then scanned."
        )
    return path


def _refuse_share_reached_through_ancestors(hop: Path) -> None:
    """Refuse when an ANCESTOR of ``hop`` redirects to a network share.

    ``O_NOFOLLOW`` and ``lstat`` both answer about the entry they are given, so neither says
    anything about the components on the way to it. A hop read out of a link's contents is a
    path no walk has judged: statting it crosses its ancestors, and on Windows crossing a
    reparse point that names a share performs the outbound SMB probe with its NTLM exchange.

    Walked from the hop's own anchor downwards, one component at a time, so every ``lstat``
    here only crosses components this walk has already cleared. The target of a redirecting
    ancestor is read with ``readlink``, which reads the link's contents and traverses nothing.
    """
    # Imported bare, and that is deliberate: the one caller is the redirect walk, which
    # imports the same symbol from the same module before it reaches this loop, so an
    # ImportError here cannot happen without that caller having already failed closed on it.
    # A guard would be one no input can trigger, which reads as protection while testing
    # nothing.
    from kiro_crew.hooks import is_unc_shape as _unc_shape

    # Shape FIRST, on the string alone, before anything asks the filesystem. A guard that
    # has to touch its subject to judge it cannot be the outermost one here, because on
    # Windows touching is the probe: ``lstat`` on a path whose own anchor is a share reaches
    # that host, so a walk starting at ``hop.anchor`` would perform the exchange while
    # looking for it. This test reads characters and reaches nothing, so it can run in front.
    if _unc_shape(str(hop)) or any(_unc_shape(str(a)) for a in hop.parents):
        raise ExportRefused(
            f"{hop} on the path to the prompt file names a network share. Reaching it would "
            f"cross that host over SMB before anything could be checked, and a Windows SMB "
            f"touch carries an NTLM exchange. Copy the persona next to the agent spec."
        )

    parts = hop.relative_to(hop.anchor).parts[:-1] if hop.parts else ()
    cur = Path(hop.anchor)
    for part in parts:
        cur = cur / part
        if not _pinned._is_redirecting_entry(cur):
            continue
        try:
            dest = os.readlink(cur)
        except OSError as exc:
            if exc.errno in (errno.EINVAL, errno.ENOENT):
                continue
            raise ExportRefused(
                f"{cur} on the path to the prompt file could not be inspected ({exc}), so "
                f"whether it reaches a network share is unknown. Fix its permissions or copy "
                f"the persona next to the agent spec."
            ) from None
        if _unc_shape(str(dest)):
            raise ExportRefused(
                f"{cur} on the path to the prompt file redirects to {dest!r}, which names a "
                f"network share. Reaching the prompt would cross that host over SMB before "
                f"anything could be checked, and a Windows SMB touch carries an NTLM "
                f"exchange. Copy the persona next to the agent spec."
            )


def _inline_prompt(spec: dict, crew_name: str, agents_dir: Path, notes: list[str]) -> None:
    """Inline a ``file://`` prompt as literal text; refuse a missing persona.

    Kiro Crew writes an installed agent's prompt as ``file://<absolute host
    path>`` (``kiro_crew/agent.py:2166``). That path does not exist in the
    container, so a naively copied spec produces a crew that answers as nobody --
    and kiro-cli tolerates an empty prompt, so the failure is silent. A
    ``file://`` reference is read here and the persona inlined as literal text, so the
    bundle carries the prompt rather than a host path; anything still unresolvable is
    refused, and ``serving/smc/bundle.py:validate_prompt`` refuses it at startup too.
    """
    raw = spec.get("prompt")
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise ExportRefused(
            f"agent.json for {crew_name!r} has no prompt. The prompt is the crew's "
            f"persona and kiro-cli tolerates an empty one, so a crew shipped this way "
            f"answers as nobody. Inline the persona as literal text."
        )
    if not raw.strip().lower().startswith("file://"):
        leaks = _scan.scan_text(raw, "prompt")
        if leaks:
            raise ExportRefused("the crew's prompt contains a credential: " + leaks[0].render())
        return
    # Resolved BEFORE validation, and the same value is handed to the validator, so the tree
    # is read once for the whole operation. Two resolutions -- one inside the validator, one
    # here -- were separately self-consistent and could describe DIFFERENT trees: a writable
    # agents directory replaced between them let the replacement's anchor clear containment
    # and the replacement's persona clear the read, and the attacker's bytes were signed into
    # ``agent.json``. A cycle in the agents directory itself is reached before either branch
    # below, and ``resolve()`` reports a loop as OSError(ELOOP) on some libcs and
    # RuntimeError on others, so both are caught here.
    try:
        agents_root = agents_dir.resolve()
    except (OSError, RuntimeError) as exc:
        raise ExportRefused(
            f"the agents directory {agents_dir} cannot be resolved ({exc}), so a prompt "
            f"reference cannot be judged against it. Check the crew directory for a link loop."
        ) from None
    path = _resolve_prompt_path(raw.strip(), agents_dir, resolved_root=agents_root)
    # Anchor the descendant-wise read at the root this path was actually validated
    # under, which is NOT always agents_dir. `_resolve_prompt_path` documents that
    # "containment under agents_dir is deliberately NOT required: an absolute persona
    # path outside that directory is a supported case with its own test." Passing
    # agents_dir unconditionally therefore refused that supported case outright --
    # reproduced: an absolute persona under a sibling directory aborted the whole
    # bundle with "is not under the agents directory".
    #
    # The two anchors buy different things, and the difference is the point:
    #
    #   * A prompt INSIDE agents_dir gets per-component O_NOFOLLOW from agents_dir down.
    #     That directory is writable by the agent, so a swapped PARENT is a live attack
    #     and every component below the anchor has to be checked.
    #   * An absolute prompt OUTSIDE it gets the final-component check only, by anchoring
    #     at its own parent. Walking from `/` with O_NOFOLLOW would refuse any legitimate
    #     path whose ancestors include a symlink, which is most real installs -- so
    #     claiming that protection would cost the supported case and deliver nothing.
    #     This is the protection the code had before the parent-swap fix, unchanged.
    # Resolved ONCE into a local, and both the containment test and the reader's
    # ``within_root`` use that value. Three separate ``.resolve()`` calls stood here and each
    # one re-walks the name, so a link planted between two of them is followed by the later
    # call: the reader can be handed a containment root inside the attacker's tree, where the
    # escaping file IS contained and the check passes. Measured -- a re-resolved anchor
    # returned ``ATTACKER BYTES`` where a value resolved once returned None.
    #
    # Resolving is also what makes the comparison correct at all. ``path`` comes back from
    # ``_resolve_prompt_path`` absolute while ``agents_dir`` keeps whatever shape ``--source``
    # was typed in, so comparing them unresolved always raised ValueError under a relative
    # ``--source`` and sent an IN-TREE persona down the outside-the-crew branch, trading the
    # anchored walk for a final-component check.
    #
    if _within(path, agents_root):
        anchor = agents_root
    else:
        anchor = path.parent
    # Read through a descriptor opened WITHOUT following a link at ANY component, and
    # do not re-open. _resolve_prompt_path applies every fence -- pseudo-filesystem,
    # the repo's sensitive-path predicate, the credential name and location checks --
    # and then returns a PATH. Re-opening that path here made the fences advisory: the
    # agents directory is writable, so between the last check and this read the entry
    # can become a link to ~/.aws/credentials, and the bundle would carry the target's
    # bytes with every fence having passed. Same defect the sidecar's backup read had,
    # in the opposite direction (that one exfiltrates by upload, this one by shipping
    # the bytes inside the artifact).
    #
    # agents_dir is the anchor: a single O_NOFOLLOW only refuses a FINAL-component
    # link, so without it an agent leaves the leaf alone and swaps a PARENT instead.
    # Measured -- that read private key material into the prompt.
    # ONE authority for this read. ``hooks.safe_read_file_bytes_nolink`` is where the rules
    # live: the centralized sensitive-path gate, O_NOFOLLOW followed by ``fstat`` on the
    # DESCRIPTOR so the inode validated is the inode read, ``st_nlink > 1`` refused, and the
    # opened descriptor's real path required to sit inside ``within_root`` -- read back
    # through ``/proc/self/fd`` rather than by re-walking the name, so a component swapped
    # after the fences cannot redirect it.
    #
    # A local re-implementation of the same rules was here and is gone. It answered all
    # three cases correctly when measured, which is exactly why it was worth deleting: a
    # second copy that agrees today is a second copy that drifts tomorrow, and this one
    # already differed in kind by asking ``lstat`` about a NAME where the shared reader asks
    # ``fstat`` about the open file.
    #
    # Refuses when hooks is unimportable, matching the UNC gate above at this same site and
    # for the same reason: an unanswerable question about an author-supplied path is not a
    # reason to read it anyway, and the operator has an alternative the agent-spec read does
    # not -- inline the persona as literal text, which is what the base branch requires of
    # every crew today.
    # The LINK question is asked here, before the path is handed over, because the shared
    # reader cannot answer it: ``validate_file_path`` canonicalizes first, so by the time its
    # ``O_NOFOLLOW`` open runs the name it opens is already the link's TARGET. Measured --
    # a symlinked persona read straight through it and returned the target's bytes.
    #
    # Same trap this module recorded once before in the other direction: ``resolve()``
    # collapses links, so a check placed after it inspects targets and cannot see that a link
    # was ever there. One authority per rule still holds -- the shared reader owns the
    # sensitive-path verdict, the descriptor's identity and containment; the link's existence
    # is a question only an un-canonicalized view can answer.
    # Two questions, two answers, one authority for each.
    #
    # ``safe_read_file_bytes_nolink`` stays the VERDICT: it owns the sensitive-path rules, the
    # fstat on the descriptor it opened, the hard-link refusal and containment against the
    # anchor. Re-deriving any of those here would be a second implementation of a security
    # primitive, which is worse than none.
    #
    # What it cannot answer is whether the anchor STRING still names the directory this build
    # checked. It resolves that string itself, so a swap between the chain walk and the read
    # makes every containment answer true of the replacement: measured, an ``agents/`` replaced
    # by a symlink after the walk inlined the attacker's bytes. Comparing the anchor's identity
    # before and after was tried and is defeatable -- swap, let the read happen, swap back, and
    # both observations match.
    #
    # So the bytes are AUTHORISED separately, by ``safe_read_file_bytes_with_identity``, which
    # opens once with ``O_NOFOLLOW`` and refuses unless the fstat identity of that very
    # descriptor is the one allowed. The identity handed to it is taken THROUGH a descriptor for
    # the anchor, so it names the file inside the directory that was checked whatever the path
    # means by then. A disagreement between the two reads is itself the answer: something
    # changed underneath, and neither set of bytes is trustworthy.
    try:
        anchor_fd = _pinned._open_dir_nofollow_pinned(anchor, already_resolved=True)
    except OSError as exc:
        raise ExportRefused(
            f"the prompt anchor {anchor} could not be opened ({exc}), so the directory the "
            f"prompt is read from cannot be pinned. Copy the persona next to the agent spec."
        ) from None

    try:
        _pinned._refuse_redirects_in_chain(
            anchor, str(path.relative_to(anchor)) if _within(path, anchor) else path.name
        )

        try:
            from kiro_crew.hooks import (
                FileTooLargeError,
                safe_read_file_bytes_nolink,
                safe_read_file_bytes_with_identity,
            )
        except ImportError as exc:
            raise ExportRefused(
                f"cannot read the prompt file {path} safely, because kiro_crew.hooks is not "
                f"importable here ({exc}). That module holds the sensitive-path rules this "
                f"read has to satisfy, and a local approximation of them is not the same "
                f"check. Inline the persona as literal text in the agent spec instead."
            ) from exc

        # The shared reader has TWO refusal channels and they mean different things: None is
        # "the guard rejected this", while the size cap RAISES. Catching only one lets a
        # FileTooLargeError out of a function whose contract is ExportRefused -- measured, it
        # reached the CLI as a traceback.
        try:
            data = safe_read_file_bytes_nolink(str(path), str(anchor), max_bytes=_MAX_PROMPT_BYTES)
        except FileTooLargeError as exc:
            raise ExportRefused(
                f"prompt file {path} exceeds the {_MAX_PROMPT_BYTES} byte ceiling for an "
                f"inlined persona ({exc}). A persona that large is a document, not a prompt; "
                f"trim it or point the agent at a skill instead."
            ) from None
        if data is None:
            raise ExportRefused(
                f"prompt file {path} was refused by the repository's file-read guard. It is "
                f"sensitive, a link, hard-linked to another name, not a regular file, outside "
                f"{anchor}, or unreadable. Copy the persona next to the agent spec and "
                f"reference it by name."
            )

        rel = path.relative_to(anchor) if _within(path, anchor) else Path(path.name)
        try:
            through_anchor = os.stat(str(rel), dir_fd=anchor_fd, follow_symlinks=False)
        except OSError as exc:
            raise ExportRefused(
                f"prompt file {path} could not be inspected inside the pinned anchor "
                f"({exc}), so the bytes cannot be authorised against the directory this "
                f"build checked. Copy the persona next to the agent spec."
            ) from None

        # ``through_anchor`` is a SECOND observation and needs its own verdict. The shared
        # reader does refuse a directory and a hard-linked name, but it refuses what ITS OWN
        # resolution found, which is the reason this stat exists at all. What is authorised
        # here is an INODE, so a persona replaced by a directory between the two reads gets a
        # directory's inode allowlisted and the failure then lands inside the reader as an
        # uncaught IsADirectoryError, out of a function whose contract is ExportRefused:
        # measured. Nothing after the allowlist can refuse it, so both questions are answered
        # before the identity is handed over.
        if not stat.S_ISREG(through_anchor.st_mode):
            raise ExportRefused(
                f"prompt file {path} is not a regular file inside the anchor this build "
                f"pinned, so there are no persona bytes to inline. Point the prompt "
                f"reference at a file."
            )
        if through_anchor.st_nlink > 1:
            raise ExportRefused(
                f"prompt file {path} has {through_anchor.st_nlink} names inside the anchor "
                f"this build pinned. A second name can change the bytes after this read, so "
                f"what lands in the bundle would not be what was checked. Copy the persona "
                f"instead of hard-linking it."
            )

        try:
            authorised = safe_read_file_bytes_with_identity(
                str(path), {(through_anchor.st_dev, through_anchor.st_ino)}
            )
        except FileTooLargeError as exc:
            raise ExportRefused(
                f"prompt file {path} exceeds the reader's size cap ({exc})."
            ) from None
        except PermissionError as exc:
            # FIRST, because it is a subclass of the OSError below and Python takes the first
            # matching handler: ordered the other way this arm is unreachable and an identity
            # mismatch reports itself as a truncated read. The two are different facts -- this
            # one says the bytes are not from the file that was pinned.
            raise ExportRefused(
                f"prompt file {path} is not the file inside the directory this build checked "
                f"({exc}). The anchor or the file changed while the bundle was being built, "
                f"so these bytes are not the ones any check ran against."
            ) from None
        except OSError as exc:
            # The right file, read part way and then failed: a disconnected NFS or FUSE mount
            # is the measured case. The reader raises it from inside the descriptor read, so
            # without this arm it leaves a function contracted to raise ExportRefused as a
            # bare traceback.
            raise ExportRefused(
                f"prompt file {path} could not be read through to the end ({exc}), so the "
                f"persona that would be inlined is incomplete. Refusing rather than "
                f"bundling a truncated prompt."
            ) from None
    finally:
        os.close(anchor_fd)

    if authorised is None or authorised != data:
        raise ExportRefused(
            f"prompt file {path} changed while it was being read: the bytes the guard cleared "
            f"are not the bytes reachable inside the anchor this build pinned. Refusing rather "
            f"than inlining either."
        )

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ExportRefused(f"prompt file {path} is not UTF-8 text") from None
    if not text.strip():
        raise ExportRefused(f"prompt file {path} is empty")
    leaks = _scan.scan_text(text, f"prompt({path.name})")
    if leaks:
        raise ExportRefused("the crew's prompt contains a credential: " + leaks[0].render())
    spec["prompt"] = text
    notes.append(f"inlined prompt from {path} ({len(text)} chars)")
