"""Gateway-owned push verdicts: evidence the push guard ran, held where the agent cannot reach it.

WHAT THIS IS
------------
The prepare-pr skill's pre-push stale-base guard answers one question -- is this branch
being published on a base that has moved -- and a verdict that is only printed persists
nothing, so a publish whose gate was skipped is byte-identical to one whose gate passed.

The evidence lives HERE, in the gateway process, and nothing outside this process writes
it. The agent asks the gateway to run the guard; the gateway runs it, records what IT
observed, and the ``git push`` allow path in the shell-command floor reads that record.

WHY THE GATEWAY IS THE WRITER
-----------------------------
Any store the agent can write is evidence the gated party controls, and signing does not
help when the key is as readable as the file. So the writer is the one party the agent
cannot impersonate: the gateway itself. ``record`` is called from the gateway's own route
handler, which runs in this process; the MCP tool the agent calls is a separate process
that reaches that route over loopback and carries a request, never a result.

WHY THE GATE TOUCHES NO FILESYSTEM
----------------------------------
The publish floor runs inside the permission gate, where a slow mount is a stall of every
task in the process. So the floor reads THIS module's in-process mapping and nothing else:
no receipt file, no policy file, no ref read, no subprocess, no network.

That is affordable because the gateway already sees every command it authorizes. It does
not need to re-read ``HEAD`` to know the tree moved -- it can watch for the commands that
move it. ``invalidate_on`` is called for every authorized command, drops the session's
verdict when that command can change ``HEAD`` or the base, and ``mutates_head`` is the same
predicate the floor uses to refuse a publish that carries a mutation in its own command
line. A compound ``git commit --amend && git push`` is therefore refused rather than judged
on the pre-amend state, which a pre-execution gate could never judge correctly.

WHAT A VERDICT IS KEYED ON
--------------------------
The CALLING SESSION, never a worktree named in a request body -- the contract
``dashboard/handlers/session_ledger.py`` states for its own routes. A session cannot ask
for a verdict about one worktree and then publish from another, because it never names the
worktree: its own identity selects the record. There is no enrolled list, so there is also
no over-refusal of a repository nobody asked about.

ACTIVATION IS THE OPERATOR'S, AND IT IS TRUSTED
-----------------------------------------------
``activation_enabled`` reads a leaf on the protected keystone -- the one placement where the
agent can neither read nor write it, the same floor the computer-use primary enable sits on
and for the same reason. ``config.json`` may REQUEST activation; only the keystone
AUTHORIZES it, so an agent with file-write access cannot enable or disable this gate. It is
off until an operator turns it on out of band, so an installation that never activates it
is judged exactly as it is on main.

Once activated, the check runs in the UNCONDITIONAL git-publish path rather than behind the
opt-out tiers, because an opt-out key would hand the gated party its own bypass.

Activation is on disk and survives a restart. Verdicts are the opposite, in-process only: a
restart costs one guard re-run rather than leaving a stale pass behind.

A git ``pre-push`` hook is deliberately NOT the mechanism: the gateway pins
``core.hooksPath=/dev/null`` so a repository-planted hook cannot execute host-side.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from .shell_normalizer import _CMD_SPLIT_RE

logger = logging.getLogger(__name__)

#: Label the refusal reports, mirroring the other publish-floor tags.
DENY_LABEL = "prepare-pr push verdict"

#: Named in every refusal so an operator reads what to do, not merely what failed. The
#: tool name rather than a command line: the interpreter and the skill's location differ
#: per install, and a command that does not run is worse than a name.
GATE_TOOL = "the prepare-pr push guard tool"

#: How long a verdict stays usable. A bound rather than a freshness proxy: the verdict is
#: invalidated by OBSERVED mutation, so this only caps how long an untouched session may
#: sit between its guard run and its push.
MAX_AGE_SECONDS = 60 * 60

#: Git subcommands that can move ``HEAD`` or the recorded base. A verdict is about a pair
#: of commits, so any of these leaves the recorded pair describing a tree that has moved.
#: Deliberately a SMALL positive list of verbs known to mutate, not a denylist of
#: everything else: a verb absent here costs one stale-verdict window, whereas treating an
#: unrecognised verb as inert would silently widen the gate.
_MUTATING_SUBCOMMANDS = frozenset(
    {
        "am",
        "apply",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "config",
        "fetch",
        "merge",
        "pull",
        "rebase",
        "remote",
        "reset",
        "restore",
        "revert",
        "stash",
        "switch",
    }
)

#: Options that point ``git`` at a repository other than the one the caller stands in. A
#: verdict describes ONE tree, so a publish carrying any of these is not the publish that was
#: judged. ``-c k=v`` is deliberately absent: it sets configuration, it does not redirect.
_REDIRECTING_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})

#: Shell verbs that move the working directory. A publish fused with one of these is
#: redirected as surely as ``-C`` redirects it, and the floor cannot resolve where it lands:
#: it has no filesystem access by design and no view of the shell's working directory.
_DIRECTORY_VERBS = frozenset({"cd", "pushd", "popd", "chdir"})

#: Spellings of the git program itself. Matched on the token's LEAF so ``/usr/bin/git`` and
#: ``git.exe`` are recognised, because a walk that only knows the bare word would read
#: ``/usr/bin/git reset`` as carrying no git subcommand at all.
_GIT_PROGRAM_NAMES = frozenset({"git", "git.exe"})

#: What separates one command from the next on a shell line. Splitting here is what lets a
#: fused compound be judged ONE INVOCATION AT A TIME: the mutation in
#: ``git reset --hard && git push`` is found because the reset is its own segment, while the
#: publish segment is judged on its own subcommand rather than on the whole line's words.
#: Command substitution opens a segment too, so a git nested inside one is still seen.
#:
#: This is ``shell_normalizer``'s splitter rather than one of our own. A local copy lived here
#: and was a THIRD spelling of the same grammar -- and the narrower one: it missed the lone
#: background ``&``, so ``git push & git reset --hard`` read as a single segment and the
#: mutation in it went unseen. Reusing the sibling both removes the duplicate and closes that
#: hole. A bare ``(`` is not in it and does not need to be: ``_command_words`` strips shell
#: grouping from each word, which is what lets a wrapped ``cd`` be seen.
_SEGMENT_SEPARATORS = _CMD_SPLIT_RE

#: Path separators, both spellings in one pattern. A payload's spelling is the agent's choice
#: and not the host's, so a Windows path reaches a POSIX gateway and the reverse. One pattern
#: rather than normalising to ``/`` and splitting on it, because that form reads as manual
#: ``/`` path assembly to the portability scanner and to anyone skimming for it.
_PATH_SEPARATORS = re.compile(r"[\\/]")

#: ``git``'s OWN options, those valid before the subcommand, that take their value as a
#: separate token. The walk must step over the option AND its value or the value is mistaken
#: for the subcommand: in ``git -C build push`` the subcommand is ``push``, not ``build``.
#: The ``--option=value`` spellings need no entry, being one token already.
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)

#: Subverbs that READ a mutating verb's storage without changing it. A verb alone cannot
#: always decide: ``git stash`` saves and so mutates, while ``git stash list`` only prints,
#: and both carry the verb ``stash``. Treating the reads as mutations invalidated a verdict
#: on a command that changed nothing.
#:
#: Deliberately limited to the SUBVERB position. An option-level dry run -- ``git apply
#: --check``, ``git clean --dry-run`` -- is still read as its verb, because the cost of that
#: is one extra guard run for the session while the cost of a wrong exception is a verdict
#: outliving the commit it describes.
_READONLY_SUBVERBS: dict[str, frozenset[str]] = {
    "stash": frozenset({"list", "show"}),
    "remote": frozenset({"get-url", "show"}),
}

#: Verbs whose READS are spelled as options rather than as a subverb. ``git config --get x``
#: prints one value; every other spelling of ``config`` can write one, and a written
#: ``remote.<name>.pushurl`` moves where a publish LANDS without touching a single commit --
#: which is why ``config`` and ``remote`` are mutating verbs at all. A verdict describes one
#: destination as much as one pair of commits.
_READONLY_OPTIONS: dict[str, frozenset[str]] = {
    "config": frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}),
}

#: Verbs whose BARE form is a read. ``git remote`` lists and ``git config`` prints usage, while
#: ``git stash`` SAVES -- so absence of a subverb cannot mean the same thing for all of them.
_READONLY_WHEN_BARE = frozenset({"config", "remote"})


@dataclass(frozen=True)
class Verdict:
    """What the gateway observed when it ran the guard for one session.

    Every field is part of WHAT WAS JUDGED, not decoration. A verdict that recorded only its
    own existence authorized any publish from the session that earned it: a different branch,
    a different remote, a different destination ref. Each field below is something a publish
    can name, so each one is something the floor can hold the publish to.
    """

    gitdir: str
    worktree: str
    head: str
    base: str
    base_sha: str
    remote: str
    source_ref: str
    recorded_at: float


_LOCK = threading.Lock()
_VERDICTS: dict[str, Verdict] = {}


def record(
    session_key: str,
    *,
    gitdir: str,
    worktree: str,
    head: str,
    base: str,
    base_sha: str,
    remote: str,
    source_ref: str,
) -> None:
    """Record the guard's pass for *session_key*. Called ONLY by the gateway's own route.

    There is deliberately no path from an agent-supplied payload to this function: the
    route computes every field from the run it performed itself.

    Every field is REQUIRED with no default. A default here would be the quiet way this gate
    loses its binding: a caller that forgot the remote would record a verdict that matches
    every remote, and nothing would fail.
    """
    if not session_key:
        raise ValueError("a verdict must belong to a session")
    with _LOCK:
        _VERDICTS[session_key] = Verdict(
            gitdir=gitdir,
            worktree=worktree,
            head=head,
            base=base,
            base_sha=base_sha,
            remote=remote,
            source_ref=source_ref,
            recorded_at=time.time(),
        )


def verdict_for(session_key: str) -> Verdict | None:
    """The live verdict for *session_key*, or ``None`` when there is none to read.

    Expiry is applied here rather than by a sweeper so a reader can never see a verdict
    the writer would consider stale.
    """
    if not session_key:
        return None
    with _LOCK:
        found = _VERDICTS.get(session_key)
        if found is None:
            return None
        if time.time() - found.recorded_at > MAX_AGE_SECONDS:
            del _VERDICTS[session_key]
            return None
        return found


def invalidate(session_key: str) -> None:
    """Drop *session_key*'s verdict. Idempotent: absence is the desired end state."""
    if not session_key:
        return
    with _LOCK:
        _VERDICTS.pop(session_key, None)


def invalidate_for_worktree(worktree: str) -> int:
    """Drop every verdict recorded for *worktree*, and answer how many went.

    A companion to the by-key form rather than a replacement, and the reason is that a session
    can be repointed at a DIFFERENT repository while keeping its key. The by-key call needs the
    key the floor will read, which a caller that only holds a slot may not be able to name; this
    one needs only the tree, which the verdict records. Over-invalidation is the safe direction
    here -- it costs one guard re-run, where a verdict outliving the tree it describes is a pass
    for a repository nobody judged.
    """
    if not worktree:
        return 0
    with _LOCK:
        stale = [key for key, found in _VERDICTS.items() if found.worktree == worktree]
        for key in stale:
            del _VERDICTS[key]
    return len(stale)


def git_subcommand(segment: str) -> tuple[str, tuple[str, ...]]:
    """The git subcommand *segment* invokes, plus the tokens that follow it.

    Answers ``("", ())`` when the segment invokes no git at all. This is the token walk every
    other predicate here is built on, and the reason it exists is that matching a verb
    ANYWHERE in the line reads arguments as verbs: ``git push origin
    revert-12643-fix-crash`` carries the word ``revert`` and ``fix/reset-password-flow``
    carries ``reset``, and a line-wide match refuses both -- ordinary branch names, refused
    for containing a verb in a position where git does not read one.

    So the walk finds the git program token, steps over git's own options and their values,
    and takes the FIRST bare token after them. That is the one position git itself treats as
    the subcommand.

    The program token is looked for rather than required at position zero, because a publish
    is routinely prefixed -- ``timeout 60 git push``, ``env GIT_SSH=... git push``, ``sudo git
    push`` -- and demanding position zero would miss the mutation in every one of them. The
    cost is that a git-looking WORD in some other command's arguments can be read as an
    invocation; that direction refuses a publish an operator can then re-run, where the other
    direction would let a fused mutation through unseen.
    """
    tokens = _command_words(segment)
    start = -1
    for position, token in enumerate(tokens):
        leaf = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if leaf in _GIT_PROGRAM_NAMES:
            start = position
            break
    if start < 0:
        return "", ()

    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return token.lower(), tuple(tokens[index + 1 :])
        # Case-sensitive membership on purpose: ``-C`` and ``-c`` are different options that
        # happen to differ by case alone, and both take a separate value, so either spelling
        # must step two. Lowercasing here would be harmless today and a trap the first time
        # one of them stops taking a value.
        index += 2 if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE else 1
    return "", ()


def _reads_only(verb: str, arguments: tuple[str, ...]) -> bool:
    """Whether *verb* is being used in one of its read-only forms.

    Three shapes, because git does not spell "read" one way. An OPTION can say it
    (``git config --get x``); a SUBVERB can say it (``git stash list``), read from the first
    bare token so options are stepped over; and for some verbs the BARE form is itself a read
    (``git remote`` lists, while ``git stash`` saves).
    """
    readonly_options = _READONLY_OPTIONS.get(verb)
    if readonly_options and any(
        argument.split("=", 1)[0] in readonly_options for argument in arguments
    ):
        return True
    readonly_subverbs = _READONLY_SUBVERBS.get(verb)
    for argument in arguments:
        if argument.startswith("-"):
            continue
        return readonly_subverbs is not None and argument.lower() in readonly_subverbs
    return verb in _READONLY_WHEN_BARE


def git_mutating_subcommand(command: str) -> str:
    """The mutating git verb *command* carries, or ``""`` when it carries none.

    Returns the VERB rather than a bool so a refusal can name what it saw, which is the
    difference between a message an operator can act on and one they have to guess at.

    Judged per shell segment through ``git_subcommand``, so a compound that fuses a mutation
    with a publish still answers the mutation's verb -- the mutation is simply found in its
    own segment rather than by scanning the whole line for the word.
    """
    for segment in _SEGMENT_SEPARATORS.split(command):
        verb, arguments = git_subcommand(segment)
        if verb in _MUTATING_SUBCOMMANDS and not _reads_only(verb, arguments):
            return verb
    return ""


def redirects_repository(command: str) -> str:
    """The repository redirection *command* carries, or ``""`` when it carries none.

    Two forms, both read as TEXT because the floor may not touch the filesystem:

    ``git -C other push`` and the ``--git-dir`` / ``--work-tree`` / ``--namespace``
    spellings, which point git at another repository outright; and a directory change fused
    with the publish (``cd other && git push``), which redirects it just as effectively.

    A directory verb counts only in COMMAND position, so a branch literally named ``cd``
    is not mistaken for one. The returned token is the one that was seen, so the refusal can
    name it instead of leaving an operator to guess which word was the problem.

    Option matching is CASE-SENSITIVE and must stay that way: ``git -C`` redirects the
    repository and ``git -c`` sets one configuration value, they differ by case alone, and
    lowercasing the command line collapses the redirection into the thing this predicate
    deliberately ignores. Only the directory verb is matched case-insensitively.

    Segments come from ``_SEGMENT_SEPARATORS``, the same splitter the sibling predicates use.
    A second hand-rolled splitter lived here and had already diverged from it on ``$(``,
    backticks and parentheses, which is how a subshell-wrapped ``cd`` walked past this
    refusal.
    """
    for segment in _SEGMENT_SEPARATORS.split(command):
        for position, token in enumerate(_command_words(segment)):
            if position == 0 and token.lower() in _DIRECTORY_VERBS:
                return token.lower()
            option = token.split("=", 1)[0]
            if option in _REDIRECTING_OPTIONS:
                return option
    return ""


def _command_words(segment: str) -> list[str]:
    """*segment*'s words with shell WRAPPERS removed, so command position is command position.

    A grouping construct does not start a new command, it wraps one, and the wrapper can sit
    glued to the verb or stand alone: bash runs the same thing for ``cd x && git push``,
    ``(cd x && git push)`` and ``{ cd x; git push; }``. Read literally, the middle one puts
    ``(cd`` at position zero and the last one puts ``{`` there, so a position-zero test for a
    directory verb answered no to both -- and the publish that followed was then measured
    against a verdict for a different tree, with nothing else able to catch it, since this gate
    reads no filesystem and cannot see the shell's working directory.

    So leading grouping characters are stripped from each word and words that are nothing but
    grouping are dropped. Stripping rather than splitting on them, because ``(`` is already a
    segment separator for the sibling predicates and what remains after the split is the glue
    on the verb itself.
    """
    words: list[str] = []
    for word in segment.split():
        stripped = word.strip("(){}`!")
        if stripped:
            words.append(stripped)
    return words


#: ``git push``'s own options that take their value as a SEPARATE token. Same reason as the
#: global set: without stepping over the value, ``git push --repo other HEAD`` reads ``other``
#: as the remote.
_PUSH_OPTIONS_WITH_VALUE = frozenset({"--exec", "--push-option", "--receive-pack", "--repo", "-o"})

#: The verbs that publish. Kept beside the parser rather than derived from the floor's own
#: publish detection, because that detection answers "is this a publish at all" across
#: wrappers and brace expansions while this one needs the verb's ARGUMENT POSITIONS.
_PUBLISH_SUBCOMMANDS = frozenset({"push"})


def publish_target(command: str) -> tuple[str, tuple[str, ...]]:
    """The remote and EVERY refspec the publish in *command* names.

    Both elements are empty when the command does not name them, which is the ordinary case:
    a bare ``git push`` names neither and means "this branch, to its upstream", which is
    exactly what the gateway judged.

    Read positionally, the way git reads it: after the verb and its options, the first bare
    token is the remote and EVERY token after it is a refspec. Returning all of them rather
    than the second positional is the point -- ``git push origin one two`` publishes both, and
    reading only the first left the second unjudged.
    """
    for segment in _SEGMENT_SEPARATORS.split(command):
        verb, arguments = git_subcommand(segment)
        if verb not in _PUBLISH_SUBCOMMANDS:
            continue
        positionals: list[str] = []
        repo_option = ""
        index = 0
        while index < len(arguments):
            token = arguments[index]
            if token == "--":
                positionals.extend(arguments[index + 1 :])
                break
            if token.startswith("--repo="):
                repo_option = token.split("=", 1)[1]
                index += 1
                continue
            if token == "--repo" and index + 1 < len(arguments):
                repo_option = arguments[index + 1]
                index += 2
                continue
            if token.startswith("-"):
                index += 2 if token in _PUSH_OPTIONS_WITH_VALUE else 1
                continue
            positionals.append(token)
            index += 1
        # git's own precedence: ``--repo`` supplies the repository only when no positional one
        # does. Reading it the other way round would refuse an ordinary publish whose positional
        # remote is the judged one.
        remote = positionals[0] if positionals else repo_option
        return remote, tuple(positionals[1:])
    return "", ()


#: Prefixes a ref may carry for the SAME object a bare branch name reaches. Only the branch
#: namespace: ``refs/tags/x`` and ``refs/heads/x`` are different refs and must not compare
#: equal, which is why this is a known-prefix list rather than a last-component split.
_KNOWN_REF_PREFIXES = ("refs/heads/",)


def _normalize_ref(ref: str) -> str:
    """*ref* with a leading branch-namespace prefix removed, so the two spellings of one
    branch compare equal while two different refs still do not.

    Comparing only the LAST component was the defect this replaces: it made ``bug/foo`` equal
    ``feat/foo``, so a verdict for one authorized a publish of the other.
    """
    ref = ref[1:] if ref.startswith("+") else ref
    for prefix in _KNOWN_REF_PREFIXES:
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def publish_mismatch(verdict: Verdict, command: str) -> str:
    """Why *command* is not the publish *verdict* describes, or ``""`` when it is.

    The verdict is about ONE pair of commits reached from ONE branch toward ONE remote. A gate
    that only asked whether a verdict EXISTS authorized far more than it checked: having been
    judged on this branch against ``origin``, the session could push a different branch, push
    to a fork, or write its commit onto a ref that was never judged, and the recorded pass
    would cover all three.

    So each thing the command NAMES is held to what was judged, and each thing it leaves
    unnamed is left alone, because unnamed means git's own default, which is what was judged.

    Returns the whole operator-facing sentence rather than a flag, so the refusal says which
    of the three differed and what it was judged as.
    """
    remote, refspecs = publish_target(command)

    if remote and remote != verdict.remote:
        return (
            f"this publish names the remote `{remote}`, and the recorded verdict was earned "
            f"against `{verdict.remote}`. A verdict describes one remote's view of this "
            f"branch, so it cannot speak for another. Run {GATE_TOOL} and publish to "
            f"`{verdict.remote}`, or publish to `{remote}` in a session judged against it."
        )

    if len(refspecs) > 1:
        return (
            f"this publish names {len(refspecs)} refspecs and a verdict describes one branch "
            f"measured against one base (`{verdict.source_ref or 'a detached HEAD'}` against "
            f"`{verdict.base}`). Publish them one command at a time, running {GATE_TOOL} for "
            "each, so every ref that is published is a ref that was judged."
        )

    if not refspecs:
        # A publish naming NO refspec is decided by git's own ``push.default``, and with
        # ``matching`` a bare push publishes EVERY branch whose name exists on both sides. The
        # loop below then held nothing to anything, so one branch's pass covered all of them.
        #
        # Establishing what the expansion actually is means reading ``push.default`` from git
        # config, and this predicate is consumed inside the permission gate, which reads no
        # filesystem by design -- a slow mount there stalls every task in the process. So the
        # honest answer is to refuse what cannot be established rather than to assume the
        # narrow reading.
        #
        # This costs nothing an operator wants: on an activated installation the gateway
        # performs the publish itself and releases the receipt with the operation, so a bare
        # ``git push`` by the agent already has no receipt to spend. What this closes is the
        # window WHILE a receipt exists.
        return (
            "this publish names no refspec, so what it publishes is whatever git's "
            f"`push.default` expands to -- with `matching` that is every branch both sides "
            f"share, not only `{verdict.source_ref or 'a detached HEAD'}`. This gate reads no "
            "git config, so it cannot establish that the expansion is the branch that was "
            "judged. Name the ref explicitly (`git push <remote> <branch>`) so what is "
            "published is what was judged."
        )

    judged = _normalize_ref(verdict.source_ref)
    for refspec in refspecs:
        source, separator, destination = refspec.partition(":")
        # A BARE refspec names one ref and lets git resolve the far side itself, so there is no
        # destination to hold anything to: ``git push origin HEAD`` is ordinary and its far side
        # is whatever git decides the current branch maps to.
        named = (
            ((source, "source"),)
            if not separator
            else ((source, "source"), (destination, "destination"))
        )
        for value, role in named:
            if not value:
                continue
            ref = _normalize_ref(value)
            if ref == "HEAD" and role == "source":
                # ``HEAD`` is what the gateway fetched and judged, by definition.
                continue
            if len(ref) == 40 and all(character in "0123456789abcdef" for character in ref.lower()):
                if ref.lower() == verdict.head.lower():
                    continue
                return (
                    f"this publish names the commit `{ref[:12]}` as its {role}, and the "
                    f"recorded verdict describes `{verdict.head[:12]}`. Re-run {GATE_TOOL} so "
                    "the commit being published is the one that was judged."
                )
            if judged and ref == judged:
                continue
            earned = (
                f"the recorded verdict was earned on `{verdict.source_ref}` against "
                f"`{verdict.base}`"
                if judged
                else "the recorded verdict was earned on a detached HEAD, which names no branch"
            )
            return (
                f"this publish names `{value}` as its {role}, and {earned}. A ref the guard did "
                f"not examine is not covered by its pass. Run {GATE_TOOL} on the branch you mean "
                "to publish."
            )
    return ""


def mutates_head(command: str) -> bool:
    """True when *command* can move ``HEAD`` or the base a verdict was judged against.

    Delegates to ``git_mutating_subcommand`` so there is ONE matcher: two copies of this
    grammar would drift, and a verb one of them missed would be a silent gap in whichever
    caller used the stale copy.
    """
    return bool(git_mutating_subcommand(command))


def invalidate_on(session_key: str, command: str) -> None:
    """Drop the session's verdict when *command* can move what the verdict describes.

    Called for every command the floor authorizes, which is what lets the gate know the
    tree moved without reading the tree.
    """
    if mutates_head(command):
        invalidate(session_key)


def writes_git_metadata(paths: Iterable[str]) -> str:
    """The first of *paths* that writes inside a git directory, or ``""`` when none does.

    A git command is not the only way to move a ref. Writing ``.git/refs/heads/<branch>``
    with an ordinary file tool moves it with no verb for ``git_mutating_subcommand`` to see,
    and the floor cannot notice because it reads no filesystem -- so a verdict describing the
    old commit stayed usable while the branch pointed somewhere else. Writing the ``.git``
    FILE of a linked worktree is the same class one step out: it re-points the worktree at
    another repository entirely.

    Matched on the path COMPONENT, so ``/repo/.git/refs/heads/x`` and ``/repo/.git`` both
    answer while a file merely named ``x.git`` does not. Both separators are read, because a
    payload's spelling is the agent's choice and not the host's.

    Residual, stated rather than hidden: a git directory NOT called ``.git`` -- a bare
    repository, or one relocated with ``--git-dir`` -- is not recognised by name, and a name
    is all this predicate has. It does not read the filesystem, for the same reason the floor
    does not.
    """
    for path in paths:
        if not path:
            continue
        components = _PATH_SEPARATORS.split(path)
        # Case-folded, because the filesystem is: APFS and NTFS both land a write to
        # ``/repo/.GIT/refs/heads/x`` in the real ``.git`` directory and move the ref, while
        # a literal lowercase comparison read ``.GIT`` as an unrelated name and kept the
        # pass. A re-casing is one keystroke, so the exact-spelling form was an evasion of
        # the predicate rather than a gap in it.
        if any(component.lower() == ".git" for component in components):
            return path
    return ""


def invalidate_on_write(session_key: str, paths: Iterable[str]) -> str:
    """Drop the session's verdict when any of *paths* writes inside a git directory.

    Returns the path that triggered it so a caller can log what it saw. The verdict is
    dropped rather than the write refused: the write may be entirely legitimate, and the only
    thing that stops being true is the pass, which costs one guard re-run.
    """
    if not session_key:
        return ""
    found = writes_git_metadata(paths)
    if found:
        invalidate(session_key)
    return found


#: The keystone leaf holding activation: a FLAT name at the crew home's root, with the same
#: placement and the same protection as ``computer_use.json``. It is listed in
#: ``security.paths._CREW_SECRET_LEAVES`` for read+write protection on the tool path, and in
#: ``sandbox._CREW_READONLY_LEAVES``, ``sandbox._CREW_CHILD_WITHHELD_LEAVES`` and
#: ``sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES`` so the OS sandbox mounts it read-only for
#: the shell. THOSE ENTRIES are what make this a trusted opt-in rather than a preference: the
#: claim "the agent cannot write it" is a claim about those four tables, and without them the
#: gated party writes its own off switch.
#:
#: Flat rather than a ``security/`` subpath because every one of those tables keys on a
#: crew-home LEAF; a subpath would need a new protected directory instead of an existing,
#: tested shape.
class ActivationUnreadable(RuntimeError):
    """The activation leaf exists but could not be read or parsed.

    Distinct from absence on purpose. Absence means nobody activated gating and is answered
    with a plain false; this means an operator's activation state is UNKNOWN, and answering
    false there would let one corrupted file silently disable the gate on an installation
    that had turned it on. The floor turns this into a publish refusal.
    """


ACTIVATION_LEAF = "push-verdict-activation.json"

#: Where the gateway keeps its OWN copy of the commits it judges: one bare repository per
#: repository identity, under the crew home.
#:
#: It exists because the worktree must never be written. The gateway fetches the base from the
#: remote and the candidate out of the worktree into this repository, and runs the guard HERE,
#: so the tree being judged is only ever read. A fetch into the worktree would both modify the
#: state being judged and fail outright once that tree is read-only.
#:
#: It gets the SAME protection as ``ACTIVATION_LEAF`` and for a sharper reason: a mirror the
#: agent can write is a mirror in which the agent plants its own base commit, and then the
#: ancestry check passes against a base of the agent's choosing. That is the agent-writable
#: evidence problem again, one layer further out, so the mirror is listed read+write protected
#: on the tool path and read-only for the shell in every sandbox disposition. The gateway
#: writes it directly, not through the sandbox, exactly as the settings PUT writes the
#: computer-use enable.
#:
#: Being a DIRECTORY it takes the directory dispositions, which is not a detail: a fence seals
#: an EXISTING path only, and on a fresh activated install no judgement has run yet, so the
#: mirror root is absent, the read-only mount is skipped, and the name the agent would create
#: is exactly the one the listing exists to deny. ``sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES``
#: materialises it before any sandbox starts and ``_CREW_NOFOLLOW_READONLY_DIR_LEAVES`` keeps its
#: name unreplaceable. It is deliberately absent from the FILE precreate list, which would
#: materialise an empty file at the mirror root's own path and make the first ``mkdir`` raise.
MIRROR_DIR = "push-verdict-mirrors"


@dataclass(frozen=True)
class Activation:
    """What the keystone says about this installation's gating.

    Two facts, and they are pinned in the SAME operator-written file on purpose. The digest
    is what anchors the guard's bytes outside the repository: an installation tree can be the
    very checkout the agent edits, so no digest computed from that tree can vouch for it. Only
    a value an operator wrote where the agent cannot write can.
    """

    enabled: bool
    guard_sha256: str


def activation() -> Activation:
    """What an operator has activated on this installation, read from the keystone.

    On disk rather than in memory, so activation SURVIVES A RESTART: a gateway that comes
    back up is still gating, and an operator does not silently lose a control they turned
    on. The verdicts themselves are deliberately the opposite -- in-process only, so a
    restart costs one guard re-run and never leaves a stale pass behind.

    Read by opening the keystone path directly in the gateway, which is the keystone-reader
    pattern the other protected leaves use. ``config.json`` is not consulted here at all:
    an operator may REQUEST activation from config, but only the keystone AUTHORIZES it, and
    the same split applies to the digest -- a digest offered in config is not read, so an
    agent that writes config can neither turn this gate on nor change which guard bytes it
    will accept.

    Absence and unreadability are DIFFERENT facts and get different answers. Absence is
    the honest answer for an installation nobody activated: off, with no digest. A leaf that
    exists but cannot be read or parsed raises ``ActivationUnreadable``, because off there
    would be a fail-OPEN -- corrupting this one file would silently disable the gate
    on an installation whose operator had turned it on. Returning off is only safe for
    the never-activated case, and that case is exactly the one that raises nothing: an
    unreadable leaf can only exist where something wrote a leaf.
    """
    # Imported here rather than at module scope because this module is reachable from
    # ``kiro_crew.security``, and OUTSIDE the try because an ImportError is a defect in
    # this file, not an unactivated installation. Swallowing it would leave the gate
    # silently off forever -- the failure mode where the feature ships dead and every
    # test that mocks the reader still passes.
    from kiro_crew.config.paths import data_home

    path = data_home() / ACTIVATION_LEAF
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return Activation(enabled=False, guard_sha256="")
    except (OSError, ValueError) as exc:
        logger.warning("push verdict activation unreadable; refusing publishes", exc_info=True)
        raise ActivationUnreadable(str(exc)) from exc
    if not isinstance(payload, dict):
        # A document that PARSES but is not an object is a corrupted leaf, not an operator
        # writing a disable, so it raises for the same reason unparseable bytes do. Returning
        # off here was a fail-OPEN with a narrower entrance than the parse error above:
        # truncating this file to ``[]`` or to a bare number silently disabled the gate on an
        # installation whose operator had turned it on, and neither shape is anything a
        # writer of this leaf produces.
        logger.warning("push verdict activation is not an object; refusing publishes")
        raise ActivationUnreadable(f"activation document is {type(payload).__name__}, not object")
    enabled_value = payload.get("enabled")
    if enabled_value is not None and not isinstance(enabled_value, bool):
        # The sibling of the case above, and left unfixed it is how a closed finding returns:
        # ``{"enabled": 1}`` and ``{"enabled": "true"}`` are corrupted enables, and reading
        # either as off is the same fail-open one level in. ABSENT stays off, because that is
        # the never-activated installation; a real JSON ``false`` stays off too, because that
        # is an operator's disable.
        logger.warning("push verdict activation flag is not a boolean; refusing publishes")
        raise ActivationUnreadable(
            f"activation 'enabled' is {type(enabled_value).__name__}, not boolean"
        )
    # ``is True`` rather than ``bool(...)``: the JSON string "false" and the number 1 are
    # both truthy, and neither is an operator writing an enable. Only a real JSON ``true``
    # activates, which also makes a corrupted or half-written leaf read as OFF.
    enabled = payload.get("enabled") is True
    return Activation(enabled=enabled, guard_sha256=_pinned_digest(payload.get("guard_sha256")))


def _pinned_digest(value: object) -> str:
    """*value* as a pinned sha256, or ``""`` when it is not one.

    Anything that is not a full lowercase hex digest is treated as ABSENT rather than as a
    digest that will never match. The two behave the same at the comparison, but only absence
    produces the refusal that tells an operator to pin one, which is the message that gets the
    installation working again.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if len(candidate) != 64:
        return ""
    return candidate if all(character in "0123456789abcdef" for character in candidate) else ""


def activation_enabled() -> bool:
    """Whether an operator has activated push-verdict gating on this installation.

    The bool projection of ``activation()``, which is all the publish floor needs: the floor
    decides whether to gate, while the route is what has to care which guard bytes are
    pinned. One reader underneath, so the two cannot disagree about the same file.
    """
    return activation().enabled


def absent_detail() -> str:
    return (
        "no push guard verdict for this session. The gateway records one when it runs "
        f"{GATE_TOOL}; a publish is refused until it has. Run it and publish again."
    )


def activation_unreadable_detail(error: str) -> str:
    """Why a publish is refused while the activation state cannot be read."""
    return (
        "push-verdict gating is activated on this installation but its activation record "
        f"could not be read ({error}), so whether this publish must be gated is unknown. "
        "Refused rather than allowed: an unreadable record is not the same as gating being "
        "off. Repair or remove "
        + ACTIVATION_LEAF
        + " in the crew data directory, which needs an operator because the agent cannot "
        "write that path."
    )


def redirection_detail(token: str) -> str:
    return (
        f"this command publishes with `{token}`, so the repository it publishes need not be "
        "the one any verdict describes, and a verdict describes exactly one. Publish from "
        f"this session's own worktree without redirecting, having run {GATE_TOOL} there."
    )


def mutation_detail(subcommand: str) -> str:
    return (
        f"this command both publishes and runs `git {subcommand}`, so the commit it "
        "publishes is not the one any verdict describes -- the mutation happens after "
        "this decision is made. Split them into separate commands and re-run "
        f"{GATE_TOOL} after the last one."
    )
