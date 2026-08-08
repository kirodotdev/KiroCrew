"""Subcommand and flag completion for the web terminal's inline menu.

The path tier (``handlers/terminal.py``) answers "which FILE did you mean". This
module answers "which SUBCOMMAND or FLAG did you mean" — the ``gh pr cre⎸`` case,
where the panel previously offered nothing and the user had to go read ``--help``.

Authority model
---------------
The path tier reads directories, and its justification is that it grants nothing
the session's own ``ls`` does not. This tier RUNS a program, so it needs its own
justification, and the one it has is narrow but exact: **it runs only what
pressing Tab in that same shell already runs.** The entire body of a
cobra-generated bash/zsh completion script is a call to
``<tool> __complete <argv…>``; git's is ``git --list-cmds=…``. Those two
protocols are all this module implements, and nothing else is ever executed.

That claim is load-bearing, so note where it once did NOT hold: a third protocol,
``git <sub> --git-completion-helper``, was implemented and then removed, because
git resolves an alias through config lookup rather than invoking it — Tab would
never have run a ``!``-shell alias body, and that probe would have. The lesson is
that "the tool's own completion script calls this" has to be checked against what
the script does with the ANSWER, not just which command it names.

Five properties keep that claim true rather than aspirational:

* **Allowlist, not discovery.** A command is probed only when it appears in
  ``_KNOWN`` or the operator's ``dashboard.terminal.completion.commands``.
  Speculatively running ``<whatever the user typed> __complete`` is precisely
  what would make this dangerous — ``make __complete`` builds a target called
  ``__complete``, and an ``rm``-shaped tool would act on it — so an unknown
  command yields no completions at all rather than a probe.
* **Bare names, resolved against a sanitized PATH.** The command word must carry
  no ``/``, and PATH is filtered to absolute entries before resolution, so a
  hostile ``gh`` dropped in the session's cwd (or reached through a relative or
  empty PATH entry) can never be the file that runs.
* **No shell, ever.** An argv LIST handed to ``execve``. The words are never
  joined into a string that something would re-split, so a metacharacter in a word
  the user is mid-way through typing has nothing to escape into.
* **Sandboxed like any agent-influenced spawn, at the STRICTEST tier.** Routed
  through ``sandbox.sandboxed_spawn_argv(..., "strict")`` — every credential
  directory hidden, not just the non-workflow ones the default ``standard`` tier
  covers — plus a credential-scrubbed environment, and spawned with
  ``create_subprocess_limited`` (resource limits). The allowlist bounds WHICH
  program runs but not what an argument can ask that program to do, and a terminal
  line is writable by the agent — the "run in terminal" affordance — as well as by
  the user, so the argv is treated as agent-influenced. A probe needs no
  credential to read a static subcommand table, so the strictest tier costs
  nothing here. ``test_spawn_audit.py`` is the gate that enforces the routing.
* **Bounded.** stdin is ``/dev/null`` (a probe that decides to prompt sees EOF
  instead of hanging), stderr is discarded (cobra writes a human-readable
  directive line there), stdout is capped, a wall-clock timeout kills the process
  TREE, and a module-wide semaphore caps concurrent probes so a room full of
  typists cannot fork-bomb the gateway.
* **Answered from cache, not from a process, per keystroke.** The candidate list
  is a pure function of (binary identity, argv path, subcommands-or-flags), so a
  probe runs once per argv PATH and every subsequent keystroke narrows the cached
  list in-process. ``gh pr c`` → ``cre`` → ``creat`` is one subprocess, not three.

What this deliberately does NOT do
----------------------------------
Complete the command NAME itself (``gh`` from ``g⎸``). That needs a PATH-wide
executable scan, which is a different data source with a different cost profile
and its own disclosure question; the menu stays shut while the command word is
still being typed, exactly as before.

Positional VALUES (a branch name, a container id) are not a target either, but the
honest statement is narrower than "excluded": at a leaf position a cobra tool
answers a bare ``__complete`` from its own ``ValidArgsFunction`` rather than from a
subcommand table, so whatever it returns arrives through the same wire format and
— if it is shape-valid — is shown as a subcommand row. Two things bound that.
``strict`` sandboxing hides the credentials such a completer would need, so the
cluster- and account-backed ones (``kubectl get pod ⎸``) resolve to nothing rather
than to a live API call from the gateway. And ``is_command_token`` drops anything
that is not shaped like a token, which most identifiers are not. What remains is a
tool whose leaf values happen to look like subcommands and need no credential;
those are offered, and they are useful, but they are not what this tier set out to
provide and are not labelled differently.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import shutil
import stat
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import validate_file_path
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    create_subprocess_limited,
    sandboxed_spawn_argv,
)

logger = logging.getLogger(__name__)

# ── Protocols ────────────────────────────────────────────────────────────────

#: ``<tool> __complete <argv…> <partial>`` — spf13/cobra's hidden completion
#: command. Emits ``value\tdescription`` lines then a ``:<directive>`` line.
PROTOCOL_COBRA = "cobra"
#: git speaks its own: ``git --list-cmds=<groups>`` for subcommands. This is what
#: git's own ``git-completion.bash`` calls, not a scrape of ``--help`` (whose
#: subcommand list is a curated subset and whose per-subcommand output is a man
#: page).
#:
#: Git subcommand FLAGS are deliberately not offered. The obvious probe,
#: ``git <sub> --git-completion-helper``, reaches a builtin's parse-options only
#: when ``<sub>`` is a builtin; for an alias git expands it first, and a ``!``
#: alias body is then EXECUTED. See ``_probe_argv``.
PROTOCOL_GIT = "git"

_PROTOCOLS = frozenset({PROTOCOL_COBRA, PROTOCOL_GIT})

#: Curated allowlist of commands known to speak one of the protocols above.
#:
#: Deliberately a hand-maintained list of tools verified to implement the
#: protocol, not a guess from the tool's name or a "try it and see". Being absent
#: from this map is not a bug report — it is the safe default, and an operator can
#: add a tool through ``dashboard.terminal.completion.commands`` without waiting
#: for a release.
_KNOWN: dict[str, str] = {
    "git": PROTOCOL_GIT,
    # Cobra CLIs. Every one of these ships a `completion bash` script whose body
    # is a call to `__complete`, which is what makes the probe equivalent to Tab.
    "gh": PROTOCOL_COBRA,
    "glab": PROTOCOL_COBRA,
    "docker": PROTOCOL_COBRA,
    "podman": PROTOCOL_COBRA,
    "nerdctl": PROTOCOL_COBRA,
    "kubectl": PROTOCOL_COBRA,
    "helm": PROTOCOL_COBRA,
    "kustomize": PROTOCOL_COBRA,
    "kind": PROTOCOL_COBRA,
    "minikube": PROTOCOL_COBRA,
    "k3d": PROTOCOL_COBRA,
    "argocd": PROTOCOL_COBRA,
    "flux": PROTOCOL_COBRA,
    "istioctl": PROTOCOL_COBRA,
    "linkerd": PROTOCOL_COBRA,
    "skaffold": PROTOCOL_COBRA,
    "velero": PROTOCOL_COBRA,
    "stern": PROTOCOL_COBRA,
    "hugo": PROTOCOL_COBRA,
    "goreleaser": PROTOCOL_COBRA,
    "cosign": PROTOCOL_COBRA,
    "trivy": PROTOCOL_COBRA,
    "rclone": PROTOCOL_COBRA,
    "operator-sdk": PROTOCOL_COBRA,
    "tkn": PROTOCOL_COBRA,
}

# ── Limits ───────────────────────────────────────────────────────────────────

#: Words of context accepted from the client. A real command line is a handful of
#: words; the cap exists so a pathological screen row cannot build a giant argv.
ARGV_MAX_WORDS = 24
#: Per-word length cap, applied before anything is executed.
ARGV_MAX_WORD_LEN = 256
#: Entries returned for one listing. Cobra tools top out around 60 subcommands;
#: `git --list-cmds` returns ~150.
ENTRIES_MAX = 200
#: Wall-clock ceiling for one probe. Generous next to the ~36 ms a warm cobra
#: probe takes, because a cold binary on a network mount pays page-in cost.
PROBE_TIMEOUT_S = 2.0
#: stdout ceiling. A well-behaved probe emits a few KiB; this bounds a tool that
#: has been replaced by something that streams.
PROBE_MAX_BYTES = 256 * 1024
#: Concurrent probes across ALL sessions. Small on purpose: the cache means a
#: probe is a per-argv-path event, not a per-keystroke one, so this only ever
#: throttles genuinely cold lookups.
PROBE_CONCURRENCY = 4

#: How long a listing stays usable. A tool's subcommand set changes when the
#: BINARY changes, which the cache key already detects, so this TTL is only a
#: backstop for a tool whose answers depend on outside state (a cobra completer
#: that lists live containers).
CACHE_TTL_S = 300.0
#: Negative results expire sooner: "this returned nothing" is often a transient
#: (a tool that needs auth, a probe that timed out under load), and re-learning
#: it costs one subprocess.
CACHE_NEGATIVE_TTL_S = 60.0
#: Distinct (binary, argv-path) listings retained, LRU.
CACHE_MAX_ENTRIES = 512

#: A command word this module is willing to resolve. No ``/`` (so never a path,
#: relative or absolute), no leading dash, and a conservative character set —
#: everything outside it is refused rather than escaped, because there is nothing
#: to escape INTO when the value becomes argv[0] of a real execve.
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

#: Any control character, DEL, C1, or lone surrogate in a context word. Such a
#: word cannot have come from a command line the user is really editing, and it
#: must never reach argv.
_UNSAFE_WORD_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")

#: Shape of a subcommand name (``pr``, ``dry-run``, ``run:build``, ``v2.0``).
#: ``:`` is allowed because a subcommand is argv[1] and never a path, so the
#: ``host:path`` ambiguity the client's path guard exists for cannot arise.
_SUBCOMMAND_RE = re.compile(r"^[^\W_][\w.+:@-]*$", re.UNICODE)

#: Shape of a flag (``-v``, ``--repo``, ``--dry-run``, ``--message=``).
_FLAG_RE = re.compile(r"^--?[^\W_][\w.-]*=?$", re.UNICODE)


def is_command_token(name: str, flag: bool) -> bool:
    """Whether a value is one the client may type into the shell VERBATIM.

    The path tier escapes what it offers, because a filename is arbitrary bytes.
    A subcommand or flag is a token the TOOL defined, from a closed vocabulary,
    and is already a plain shell word — or it is not a real one. So this tier
    validates instead of escaping: an unexpected value is refused at the parser
    rather than smuggled onward as an escaped literal, and the client can insert
    what it receives without transforming it (escaping would corrupt the tool's
    own ``--message=`` into ``--message\\=``).

    Enforced on BOTH sides. Here it also keeps a malformed value out of the cache,
    where it would otherwise be re-served for the whole TTL.
    """
    if _UNSAFE_WORD_RE.search(name):
        return False
    return bool((_FLAG_RE if flag else _SUBCOMMAND_RE).match(name))


# ── Cobra wire protocol ──────────────────────────────────────────────────────

#: Sentinel passed as the final argv word to ask cobra for FLAGS rather than
#: subcommands. Cobra switches to flag completion when the word being completed
#: starts with a dash; a bare ``--`` is the canonical "list the flags" request
#: its own generated scripts use.
_COBRA_FLAG_SENTINEL = "--"

#: ``ShellCompDirectiveError``. Cobra exits 0 even when it failed, so the
#: directive bit — not the exit status — is the only reliable error signal.
_DIRECTIVE_ERROR = 1
#: ``ShellCompDirectiveNoSpace``: the accepted value is a prefix of something
#: longer, so no separator should follow it.
_DIRECTIVE_NO_SPACE = 2


@dataclass(frozen=True)
class CmdEntry:
    """One offered subcommand or flag."""

    name: str
    #: One-line help text, when the protocol supplies one (cobra does, git
    #: does not). Shown beside the name and in the menu's description bar.
    desc: str
    #: True for a flag (``--repo``), False for a subcommand (``pr``). Drives the
    #: menu's glyph and, critically, tells the client NOT to apply the ``./``
    #: path guard — a flag is not a filename.
    flag: bool
    #: Suppress the separator normally typed after an accepted value.
    nospace: bool = False

    def to_json(self) -> dict:
        """Wire form. ``kind`` is what lets the client tell a command listing from
        a path listing without a new top-level response field (which would change
        the path tier's documented shape).

        ``at`` is always 0 because command matching is a PREFIX match, unlike the
        path tier's substring search. It is emitted rather than omitted so the
        client highlights the typed span on these rows too — the same affordance,
        driven by the same field, instead of a second code path."""
        out: dict = {
            "name": self.name,
            "desc": self.desc,
            "kind": "flag" if self.flag else "sub",
            "at": 0,
        }
        if self.nospace:
            out["nospace"] = True
        return out


# ── Validation ───────────────────────────────────────────────────────────────


def parse_argv(raw: object) -> list[str] | None:
    """The client's command-line context as a validated argv, or ``None``.

    ``None`` means "this is not something to complete against" and the caller
    must answer with no command entries — never "guess". Every rejection below
    is a shape a genuine command line cannot have, so refusing costs nothing
    real and keeps unvalidated text away from ``execve``.
    """
    if not isinstance(raw, list) or not raw:
        return None
    if len(raw) > ARGV_MAX_WORDS:
        return None
    words: list[str] = []
    for word in raw:
        if not isinstance(word, str) or len(word) > ARGV_MAX_WORD_LEN:
            return None
        if _UNSAFE_WORD_RE.search(word):
            return None
        words.append(word)
    if not _COMMAND_NAME_RE.match(words[0]):
        return None
    return words


def protocol_for(command: str, extra: object = None) -> str | None:
    """The protocol ``command`` speaks, or ``None`` when it is not allowlisted.

    ``extra`` is the operator's ``dashboard.terminal.completion.commands`` map. It
    may only RE-POINT a command that is already in ``_KNOWN`` — it cannot add one.
    Adding was the original design and it was unsafe: mapping, say, ``python3`` to
    ``cobra`` makes the probe run ``python3 __complete ""``, and ``python3`` treats
    its first argument as a FILE, so an agent-created ``__complete`` in the
    session's directory would be executed as a script the moment the user typed
    ``python3``. The allowlist is not a naming convention, it is the set of tools
    whose probe argv is known to be inert; config cannot widen that.

    A value naming an unimplemented protocol is ignored rather than treated as an
    opt-in to something arbitrary.
    """
    known = _KNOWN.get(command)
    if known is None:
        return None
    if isinstance(extra, dict):
        override = extra.get(command)
        if isinstance(override, str) and override in _PROTOCOLS:
            return override
    return known


# ── Binary identity + cache ──────────────────────────────────────────────────


#: Path segments that mark a directory as PROJECT-LOCAL tooling rather than an
#: installed program. A binary under one of these is writable by whatever can
#: write the project — which includes the agent — so resolving a command name to
#: it would let a planted file run with gateway privileges the moment the user
#: types that command's name.
_PROJECT_LOCAL_SEGMENTS = frozenset({
    ".venv", "venv", ".virtualenv", "virtualenv", "node_modules", ".tox", ".nox",
    "vendor", ".direnv", "target", "build", "dist", ".git",
})


def _is_project_local(entry: str) -> bool:
    """Whether a PATH entry belongs to a project tree rather than an install.

    Segment-wise, not substring: ``/opt/venv-tools/bin`` is an installed prefix
    that merely CONTAINS the text, while ``/home/u/proj/.venv/bin`` genuinely is
    project-local.

    Both separators are honoured regardless of host. ``os.sep`` alone would make
    this silently useless for POSIX-shaped input on Windows (and vice versa) —
    and a security filter that quietly stops matching is worse than one that is
    absent, because the tests that cover it keep passing on the host that wrote
    them. `_resolve` is reachable on either platform (only ``_run_probe`` is
    POSIX-gated), so the check must not depend on which one it runs from.
    """
    parts = entry.replace("\\", "/").split("/")
    return any(part in _PROJECT_LOCAL_SEGMENTS for part in parts)


def _is_trusted_dir(directory: str) -> bool:
    """Whether a directory is one only a system administrator can write.

    Every component of the CANONICAL path — not just the leaf — must be owned by
    uid 0 and not group- or world-writable. The whole chain matters because a
    root-owned ``bin`` inside a user-writable parent can simply be swapped for a
    different directory; this is the same reasoning ``sudo``'s secure-path
    handling applies, and the reason the check walks upward.

    Deliberately NOT ``os.access(d, os.W_OK)``. That question is "can THIS process
    write here", and it answers *yes to everything* when the gateway runs as root
    — which containers routinely do — so it would silently disable the whole tier
    on those hosts while looking like a security win. Ownership and mode ask the
    question that actually matters: could anything other than an administrator
    have put a binary here.

    Note the honest limit: when the gateway itself runs as root, the agent shares
    that uid and no filesystem property separates them. This check bounds who
    could have planted a binary, which is the useful half; it cannot bound a
    same-uid actor, and no permission bit can.
    """
    try:
        real = os.path.realpath(directory)
    except OSError:
        return False
    node = real
    while True:
        try:
            st = os.stat(node)
        except OSError:
            return False
        if st.st_uid != 0:
            return False
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        parent = os.path.dirname(node)
        if parent == node:
            return True
        node = parent


def _sanitized_path() -> str | None:
    """``$PATH`` reduced to entries a probe may resolve a command name against.

    Three classes are removed, all for the same reason the cwd poller executes
    ``lsof`` only from fixed absolute paths — a probe runs with gateway
    privileges, so whatever it resolves to had better not be attacker-writable:

    * **Not an absolute directory.** An empty entry (``PATH=/usr/bin:``) and a
      relative one (``PATH=.:…``) both mean "the current directory", and this
      process's current directory is not the user's.
    * **Project-local tool directories** (``.venv/bin``, ``node_modules/.bin``,
      …), which are writable by anything that can write the project — including
      the agent.
    * **Anything not administrator-owned**, unless the operator declared it.
      This is the general form of the point above: the probe fires while the line
      is still being TYPED, so a binary planted anywhere on PATH would run before
      the user could decide not to run it. Requiring a root-owned chain means a
      plant needs administrator access, which is a different threat entirely.

    The cost is real and deliberate: on a typical machine this drops
    ``~/.local/bin`` and a version manager's shims, so a USER-LOCAL ``gh`` or
    ``npm`` is never probed — only a system-packaged one (``/usr/bin/gh`` from
    apt/dnf) is. There is deliberately NO opt-in to widen this. An earlier revision
    offered an operator-declared trusted-directory list; review was right that it
    reinstates the whole vector for anyone who uses it, and "the operator
    consented" does not make an agent-writable directory safe to execute from
    mid-keystroke. A tier that completes fewer tools is the correct trade against a
    tier that can run a planted binary.
    """
    raw = os.environ.get("PATH")
    if not raw:
        return None
    kept = [
        p for p in raw.split(os.pathsep)
        if p and os.path.isabs(p) and not _is_project_local(p) and _is_trusted_dir(p)
    ]
    return os.pathsep.join(kept) or None


def _resolve(command: str) -> tuple[str, tuple] | None:
    """``(absolute path, identity)`` for an allowlisted command name.

    The identity is ``(realpath, st_mtime_ns, st_size)`` and is part of the cache
    key, so upgrading the tool — or a version manager repointing a shim — expires
    its cached listings instead of serving a previous version's subcommands.
    Blocking (PATH walk + stat): callers run it off the event loop.

    Fails closed when sanitization leaves no usable PATH. That branch is
    load-bearing rather than defensive tidiness: ``shutil.which(cmd, path=None)``
    falls back to ``os.environ["PATH"]``, so handing it the sanitizer's ``None``
    would silently restore the very entries the sanitizer removed.
    """
    search = _sanitized_path()
    if not search:
        return None
    path = shutil.which(command, path=search)
    if not path:
        return None
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except OSError:
        return None
    # The PATH filter covers the DIRECTORY the name was found in; this covers where
    # that name actually LEADS. A symlink sitting in an allowed prefix and pointing
    # somewhere writable would otherwise reintroduce exactly what the filter
    # removed, and the target is the file that executes.
    target_dir = os.path.dirname(real)
    if _is_project_local(real):
        return None
    if not _is_trusted_dir(target_dir):
        return None
    # And the FILE, not only its directory. A root-owned directory cannot receive a
    # new file from a non-root user — creating one needs write permission on the
    # directory — but a file already inside it can still be user-owned or
    # group/world-writable (a bad package, a hand-chmod, a root-created file handed
    # to a user). The containing directory being trusted does not transfer to its
    # contents, so the executable is checked on its own terms.
    if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return None
    return path, (real, st.st_mtime_ns, st.st_size)


_cache: "OrderedDict[tuple, tuple[float, list[CmdEntry]]]" = OrderedDict()
_probe_gate: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    """The probe semaphore, created lazily.

    Not a module-level constructor: a ``Semaphore`` built at import time binds to
    whatever loop happens to be current then, which under pytest-asyncio is a
    different loop per test.
    """
    global _probe_gate
    if _probe_gate is None:
        _probe_gate = asyncio.Semaphore(PROBE_CONCURRENCY)
    return _probe_gate


def _cache_get(key: tuple) -> list[CmdEntry] | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    stored_at, entries = hit
    ttl = CACHE_TTL_S if entries else CACHE_NEGATIVE_TTL_S
    if time.monotonic() - stored_at > ttl:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return entries


def _cache_put(key: tuple, entries: list[CmdEntry]) -> None:
    _cache[key] = (time.monotonic(), entries)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def reset_cache() -> None:
    """Drop every cached listing. For tests, and for a future config reload."""
    _cache.clear()


# ── Probe execution ──────────────────────────────────────────────────────────


def _probe_env() -> dict[str, str]:
    """Environment for a probe: a minimal ALLOWLIST, not the inherited environment.

    Built from nothing rather than filtered down from ``os.environ``. A denylist
    (``scrub_env``) covers the credential names it knows — AWS, SSH, GnuPG, the
    channel tokens — and by construction cannot cover the ones it does not:
    ``GH_TOKEN``, ``GITHUB_TOKEN``, ``KUBECONFIG``, ``NPM_TOKEN`` and every future
    tool's variable would have reached the child. For a speculative process
    spawned mid-keystroke that is the wrong default, so the direction is inverted.

    Verified empirically that this costs nothing: with no ``HOME`` at all,
    ``git --list-cmds`` still returns 65 commands and ``gh __complete pr ""``
    still returns every subcommand WITH its descriptions — because those tables
    are compiled into the binary, which is the same fact the whole tier rests on.

    ``PATH`` is the SANITIZED one, so a probe cannot find a planted helper on a
    path the parent already refused to resolve against. ``TERM=dumb`` stops
    anything drawing, ``NO_COLOR`` keeps SGR sequences out of values the client
    would type, and the pager variables stop a tool that would page from waiting
    forever on a pager with no tty.

    One deliberate consequence: without ``HOME`` git cannot read ``~/.gitconfig``,
    so user ALIASES no longer appear in the subcommand listing. That is coherent
    with removing the alias-executing flag probe rather than a separate loss.
    """
    env = {
        "TERM": "dumb",
        "NO_COLOR": "1",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
    }
    search = _sanitized_path()
    if search:
        env["PATH"] = search
    # Locale only, never the full inherited set: a tool that decodes its own output
    # needs an encoding, and nothing here carries a credential.
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _prepare_probe(argv: list[str]) -> tuple[list[str], dict[str, str], str | None]:
    """Everything blocking about preparing a probe, on ONE executor hop.

    Both halves touch the filesystem and neither may run inline. Building the
    sandbox wrapper writes a temp launcher (``mkstemp``/``os.write``/``os.close``),
    and ``_probe_env`` walks every PATH entry through ``realpath``+``stat`` to
    build the sanitized PATH — so evaluating the env eagerly at the call site (as
    ``functools.partial(sandboxed_spawn_argv, argv, "strict", env=_probe_env())``
    did) put that walk straight back on the event loop, at keystroke rate. The
    nesting is the whole point of this function: the env must be constructed
    INSIDE the callable the executor runs, not passed into it.
    """
    return sandboxed_spawn_argv(argv, "strict", env=_probe_env())


async def _run_probe(argv: list[str], cwd: str | None) -> str | None:
    """stdout of one probe, or ``None`` if it could not be run to completion.

    ``None`` is not an error to report: at keystroke rate a tool that is missing,
    slow, or unhappy simply has no completions.

    Routed through ``sandboxed_spawn_argv`` in ``strict`` mode — OS-level
    filesystem isolation with EVERY credential directory hidden, plus a scrubbed
    environment — and spawned via ``create_subprocess_limited`` so the child
    carries resource limits. The allowlist already bounds WHICH program can run,
    but not what an argument can ask that program to do, and a terminal line is
    writable by the agent (the "run in terminal" affordance) as well as by the
    user. Treating the argv as agent-influenced is therefore the honest reading,
    which is what ``test/test_spawn_audit.py`` exists to force.

    ``strict`` rather than the default ``standard``: standard deliberately leaves
    the dirs a workflow needs visible (git-over-SSH, the AWS CLI), which is the
    right trade for a build or a test run and the wrong one here. A completion
    probe never needs a credential — the subcommand and flag tables it reads are
    static in the binary — so leaving ``~/.kube``, ``~/.ssh`` and friends readable
    buys nothing and lets an argument aim the tool at them
    (``kubectl --kubeconfig ~/.kube/config config use-context ⎸`` would have the
    probe read the protected config). Hiding everything costs no correctness and
    closes that.
    """
    if not platform_compat.IS_POSIX:
        return None
    loop = asyncio.get_running_loop()
    try:
        # Off the event loop, and INSIDE the error handling — two distinct reasons:
        #
        # * blocking. Building the sandbox wrapper writes a temp launcher/profile
        #   (`mkstemp` + `os.write` + `os.close`), so on a slow or wedged
        #   filesystem it would stall the whole gateway loop at keystroke rate.
        #   `discovery_executor` rather than `subprocess_executor` for the same
        #   reason the path tier gives: the latter is shared with PTY teardown's
        #   `os.close`, which can wedge in the kernel, and a completion must never
        #   be able to starve session cleanup.
        # * it RAISES. `sandboxed_spawn_argv` fails closed with
        #   `SandboxUnavailableError` when the host has no usable backend (the
        #   AppArmor-restricted-userns case is the common one). Left uncaught that
        #   escapes the route as an HTTP 500 on a keystroke — for a tier whose
        #   entire contract is "no completions is a normal answer", the right
        #   degradation is no menu, not an error the client must special-case.
        wrapped, env, cleanup = await loop.run_in_executor(
            discovery_executor(), functools.partial(_prepare_probe, argv),
        )
    except (SandboxUnavailableError, OSError, RuntimeError):
        return None
    async with _gate():
        proc = None
        try:
            proc = await create_subprocess_limited(
                *wrapped,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                # Discarded, not captured: cobra writes a human-readable
                # "Completion ended with directive: …" line here, and a tool that
                # chatters on stderr must not be able to fill a pipe nobody
                # drains and deadlock the probe.
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
            out = await asyncio.wait_for(
                proc.stdout.read(PROBE_MAX_BYTES) if proc.stdout else _empty(),
                timeout=PROBE_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, OSError, ValueError):
            if proc is not None:
                await _terminate(proc)
            return None
        except asyncio.CancelledError:
            # The client moved on (React Query aborts a superseded request). Do
            # not leave the child behind holding the semaphore slot.
            if proc is not None:
                await _terminate(proc)
            raise
        finally:
            # The sandbox's temp launcher/profile is ours to remove, on every path
            # out — including the cancellation one, where a `return` never runs.
            # Offloaded for the same reason its creation was: an unlink on a wedged
            # filesystem must not stall the loop. Fire-and-forget rather than
            # awaited, so a slow unlink delays no keystroke; a failure to remove one
            # temp file is not worth failing a completion over.
            if cleanup:
                loop.run_in_executor(
                    discovery_executor(),
                    functools.partial(Path(cleanup).unlink, missing_ok=True),
                )
        # Reap so the child never lingers as a zombie holding a pid. It has
        # already written what we read; a tool still running past that is one we
        # do not wait for.
        await _terminate(proc)
        return out.decode("utf-8", errors="replace")


async def _empty() -> bytes:
    return b""


async def _terminate(proc: "asyncio.subprocess.Process") -> None:
    """Stop a probe and reap it, without ever blocking the loop on it.

    ``start_new_session`` put the probe in its own process group, so the whole
    group is signalled: a completion script that forked helpers (git's do) would
    otherwise leave them running.
    """
    if proc.returncode is not None:
        return
    pid = proc.pid
    try:
        await platform_compat.kill_process_tree_async(pid, platform_compat.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except (asyncio.TimeoutError, ProcessLookupError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            logger.debug("terminal completion: probe %s would not die", pid)


# ── Protocol parsers ─────────────────────────────────────────────────────────


def parse_cobra(out: str, want_flags: bool) -> list[CmdEntry]:
    """Cobra's ``__complete`` output as entries.

    Wire format: ``value\\tdescription`` per line, terminated by a
    ``:<directive>`` line. The directive is a bit field and is load-bearing —
    cobra exits 0 even when it failed, so ``ShellCompDirectiveError`` is the only
    signal that the values are meaningless, and honouring it is what keeps a
    typo'd subcommand (``gh notreal ⎸``) from producing a menu of nonsense.
    """
    entries: list[CmdEntry] = []
    directive = 0
    for line in out.splitlines():
        if line.startswith(":"):
            try:
                directive = int(line[1:].strip() or "0")
            except ValueError:
                directive = 0
            break
        if not line.strip():
            continue
        name, _, desc = line.partition("\t")
        name = name.strip()
        if not name:
            continue
        # Cobra answers a flag request with flags and a bare request with
        # subcommands, but a completer is free to return either; filtering by
        # shape means a stray value can never land in the wrong menu, where the
        # client's path guard would then treat it as the wrong kind of thing.
        if name.startswith("-") != want_flags:
            continue
        # Shape-checked here, not just on the client: a value that fails would
        # otherwise sit in the cache and be re-served for the whole TTL.
        if not is_command_token(name, want_flags):
            continue
        entries.append(
            CmdEntry(
                name=name,
                desc=desc.strip(),
                flag=want_flags,
                nospace=bool(directive & _DIRECTIVE_NO_SPACE),
            )
        )
    if directive & _DIRECTIVE_ERROR:
        return []
    # The directive is only known after the loop, so NoSpace is re-applied here
    # rather than trusting the value each entry was built with.
    if directive & _DIRECTIVE_NO_SPACE:
        entries = [
            CmdEntry(e.name, e.desc, e.flag, nospace=True) for e in entries
        ]
    return entries[:ENTRIES_MAX]


#: Groups asked of ``git --list-cmds``. The same set git's own completion script
#: uses: the porcelain commands a user types, plus the third-party ``git-foo``
#: executables on PATH and the user's own aliases — which is why this beats
#: parsing ``git --help`` (that lists only "common" commands, ~22 of ~150).
_GIT_LIST_GROUPS = "list-mainporcelain,others,list-complete,alias"


def parse_git_subcommands(out: str) -> list[CmdEntry]:
    """``git --list-cmds`` output: one bare name per line, no descriptions."""
    seen: set[str] = set()
    entries: list[CmdEntry] = []
    for line in out.splitlines():
        name = line.strip()
        # `--list-cmds` prints names only; anything dash-shaped would be a
        # future format change, not a subcommand.
        if not name or name in seen or not is_command_token(name, False):
            continue
        seen.add(name)
        entries.append(CmdEntry(name=name, desc="", flag=False))
    return entries[:ENTRIES_MAX]


# ── Orchestration ────────────────────────────────────────────────────────────


def _vetted_cwd(cwd: str | None) -> str | None:
    """The directory to run a probe in, or ``None`` to inherit the gateway's.

    A probe needs the session's cwd to be useful (``gh`` finds the repo from it,
    ``git`` needs to be inside one at all), and it goes through the SAME
    ``hooks.validate_file_path`` chokepoint the path tier uses. A cwd that fails
    that check is not silently downgraded to the gateway's own directory — a
    probe is refused outright by the caller — so this tier can never be aimed
    into the governance trust-root by ``cd``-ing there first.
    """
    if not cwd:
        return None
    try:
        return validate_file_path(cwd)
    except (OSError, ValueError):
        return None


#: Flag stems that redirect a completer at a DIFFERENT endpoint or identity.
#:
#: A cobra completer at a leaf position calls the tool's own `ValidArgsFunction`,
#: which for cluster and API tools means a live request — and these flags choose
#: WHERE that request goes and WHO it goes as. `kubectl --server=<url> get pod ⎸`
#: would otherwise have a keystroke send an unsolicited request to an arbitrary
#: host, with the gateway as the client. That is a request the user never made, to
#: a destination the line author chose, so the probe is refused outright instead.
#:
#: Matched on the stem before any `=`, and on both `-x` and `--xx` forms. The list
#: is deliberately broad: a false refusal costs one menu, while a miss costs an
#: outbound request from the gateway.
_REDIRECTING_FLAG_STEMS = frozenset({
    "server", "host", "hostname", "endpoint", "addr", "address", "url", "api",
    "api-server", "cluster", "context", "kubeconfig", "config", "namespace-file",
    "token", "password", "passwd", "user", "username", "as", "as-group",
    "certificate-authority", "client-certificate", "client-key", "cacert", "cert",
    "key", "tls", "insecure", "insecure-skip-tls-verify", "proxy", "proxy-url",
    "registry", "repo-url", "remote", "socket", "unix-socket", "H",
})


#: Single-letter forms of the same thing. Kept separate because a one-dash word is
#: not one flag: it may be ``-s``, ``-s=x``, an ATTACHED value (``-shttp://host``,
#: kubectl's short ``--server``), or a cluster (``-itH``). An exact stem match — the
#: first version of this guard — saw ``-shttp://host`` as the stem
#: ``shttp://host`` and let it through.
_REDIRECTING_SHORT_FLAGS = frozenset({"s", "H", "u", "p"})


def _redirects_the_tool(argv: list[str]) -> bool:
    """Whether any word would point the tool at another endpoint or identity."""
    for word in argv[1:]:
        if not word.startswith("-") or word == "-" or word == "--":
            continue
        if word.startswith("--"):
            if word[2:].split("=", 1)[0] in _REDIRECTING_FLAG_STEMS:
                return True
            continue
        body = word[1:].split("=", 1)[0]
        # A single dash can also carry a long name (``-server``), so try that whole.
        if body in _REDIRECTING_FLAG_STEMS:
            return True
        # Otherwise read the leading run of letters — that is the flag cluster, and
        # everything after the first non-letter is an attached value, not a flag.
        # ``-shttp://host`` yields ``shttp``, which contains ``s``: refused.
        for ch in body:
            if not ch.isalpha():
                break
            if ch in _REDIRECTING_SHORT_FLAGS:
                return True
    return False


def _probe_argv(
    binary: str, protocol: str, argv: list[str], want_flags: bool
) -> list[str] | None:
    """The exact command line a probe will execute, or ``None`` when the
    (protocol, position) pair has no source for what was asked.

    Isolated from execution so a test can assert the argv WITHOUT spawning
    anything — the security-relevant claim about this module is the shape of this
    list, and it is checkable on its own.
    """
    if _redirects_the_tool(argv):
        return None
    rest = argv[1:]
    if protocol == PROTOCOL_COBRA:
        # Cobra requires the word being completed as an explicit final argument;
        # the empty string asks for "everything here", which is what makes one
        # probe answer every keystroke of a prefix.
        tail = _COBRA_FLAG_SENTINEL if want_flags else ""
        return [binary, "__complete", *rest, tail]
    if protocol == PROTOCOL_GIT:
        if want_flags:
            # NO SAFE PROBE EXISTS for a git subcommand's flags, so none is made.
            #
            # `git <sub> --git-completion-helper` only reaches a builtin's
            # parse-options when `<sub>` IS a builtin. Otherwise git expands it as an
            # alias first — and a `!`-prefixed alias is a shell command, which git
            # runs as `sh -c '<body> "$@"' <body> --git-completion-helper`. So for a
            # user with `alias.wipe = "!git reset --hard && git clean -xfd"`, probing
            # `git wipe --` would EXECUTE that alias in the session's own working
            # tree. The strict sandbox does not save this: it hides credentials, not
            # the user's repository.
            #
            # This is also the one place the tier's authority argument did not hold.
            # "It runs only what pressing Tab already runs" is true of the cobra
            # protocol and of `--list-cmds`, but git's own completion script RESOLVES
            # an alias through config lookup rather than invoking it — so Tab would
            # never have run the body, and this probe would have.
            #
            # A guarded version is conceivable (probe only when `<sub>` appears in
            # `git --list-cmds=builtins`, since git ignores aliases that shadow
            # builtins), but that trades a destructive-command hazard against a
            # second probe and a subtle dependence on that guarantee. Not the right
            # bargain inside a security boundary, and not for flag completion on one
            # tool. Git subcommands (`--list-cmds`) and every cobra flag are
            # unaffected.
            return None
        if rest:
            # git's subcommands are flat: `git remote ⎸` has no protocol that
            # enumerates `add`/`remove`, and guessing would be worse than the
            # path tier's answer.
            return None
        return [binary, f"--list-cmds={_GIT_LIST_GROUPS}"]
    return None


def _parse(protocol: str, out: str, want_flags: bool) -> list[CmdEntry]:
    if protocol == PROTOCOL_COBRA:
        return parse_cobra(out, want_flags)
    # git reaches here only for subcommands: `_probe_argv` refuses its flag case
    # outright, because no safe probe for it exists (see the alias hazard there).
    return parse_git_subcommands(out)


def filter_entries(entries: list[CmdEntry], prefix: str) -> list[CmdEntry]:
    """The cached listing narrowed to what the user has typed.

    Case-insensitive PREFIX matching, not the path tier's substring search. A
    subcommand name is short and the user is typing it from the front; matching
    ``m`` inside ``comment`` would offer rows that look unrelated to what was
    typed. Order is preserved — cobra and git both emit a meaningful one.
    """
    if not prefix:
        return entries
    lowered = prefix.lower()
    return [e for e in entries if e.name.lower().startswith(lowered)]


async def complete(
    argv: list[str],
    prefix: str,
    cwd: str | None,
    extra_commands: object = None,
) -> tuple[list[CmdEntry], str]:
    """Subcommand or flag entries for a terminal word, plus an audit reason word.

    ``argv`` is the validated context (``["gh", "pr"]``), ``prefix`` is the word
    being completed (``"cre"``, or ``"--ti"`` for a flag). The reason is drawn
    from a fixed vocabulary and never contains a command, flag or path — the
    audit rule for this route forbids recording what the user typed.
    """
    want_flags = prefix.startswith("-")
    protocol = protocol_for(argv[0], extra_commands)
    if protocol is None:
        return [], "cmd_unknown"

    loop = asyncio.get_running_loop()
    # Resolution (PATH walk), identity (realpath + stat) and cwd vetting
    # (realpath, which can stall on an unresponsive mount) are all blocking, and
    # ride ONE executor hop together — the same bargain `_resolve_vet_and_list`
    # strikes on the path tier, for the same reason: a keystroke pays a single
    # thread round-trip.
    resolved, run_cwd = await loop.run_in_executor(
        discovery_executor(), _resolve_and_vet, argv[0], cwd
    )
    if resolved is None:
        return [], "cmd_unknown"
    if cwd and run_cwd is None:
        # The session sits somewhere this gateway will not read, so it will not
        # run a program there either. Same verdict word the path tier uses.
        return [], "sensitive_path"
    binary, identity = resolved

    probe_argv = _probe_argv(binary, protocol, argv, want_flags)
    if probe_argv is None:
        return [], "cmd_none"

    key = (identity, tuple(argv[1:]), want_flags)
    cached = _cache_get(key)
    if cached is None:
        out = await _run_probe(probe_argv, run_cwd)
        cached = _parse(protocol, out, want_flags) if out is not None else []
        _cache_put(key, cached)
    entries = filter_entries(cached, prefix)
    return entries, "cmd_listed" if entries else "cmd_none"


def _resolve_and_vet(
    command: str, cwd: str | None
) -> tuple[tuple[str, tuple] | None, str | None]:
    """``_resolve`` and ``_vetted_cwd`` on one executor hop."""
    return _resolve(command), _vetted_cwd(cwd)
