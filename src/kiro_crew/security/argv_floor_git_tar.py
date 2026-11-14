"""The git/tar self-target floor: a git or tar command that dials this host is refused.

The ssh self-target floor (``argv_floor_ssh``) reads the ssh-family verbs ``ssh``,
``scp``, ``sftp`` and ``rsync``.  Programs that start an ssh transport on their own are
not those verbs, so an agent can re-enter this same host, outside the sandbox mounts,
through them: ``git clone localhost:x`` scp-dials the local sshd, ``git fetch
ssh://127.0.0.1/x`` the same over a URL, ``git clone ext::ssh localhost id`` runs a
command that dials it, ``git -c core.sshCommand='ssh localhost' clone far:x`` redirects
every ssh to this host, and ``tar -cf localhost:out.tar dir`` rsh-dials it.  This floor
reads a git or tar command line -- behind env/wrapper unwrapping, quoting, ``user@``
prefixes, URI schemes, globs and the same shell expansions the ssh floor resolves -- and
refuses it when a transport operand names this host (the same ``argv_floor_hosts``
self-host test the ssh floor uses) or when a command the transport runs DIALS this host
(``git ext::``, a ``core.sshCommand`` / ``GIT_SSH`` / ``GIT_SSH_COMMAND`` override, ``tar
--rsh-command``, an ``--upload-pack`` / ``--receive-pack`` program, an invoked alias);
``TAR_OPTIONS`` and ``TAPE`` are read as tar arguments.  Every such command must name a
literal self host and is then read by the same argv-floor dispatcher ``is_denied`` runs
on a whole command (``_reenter_floors``), so ``ssh localhost`` is refused by the ssh
floor and ``git ls-remote localhost:x`` by this one; an invoked alias is rebuilt into
the invocation git runs and re-entered the same way.
Such a command runs in the agent's own sandbox, so one that does not dial this host is
no escape and stays allowed.  It FAILS CLOSED on a transport argument it cannot parse.

A git/tar remote host, and a host named in such a command value, is refused only when it
is a LITERAL self address (loopback names and addresses, this host's own cached
names/addresses), with no DNS or hosts-file alias lookup, so a first-seen dotted remote
such as ``github.com`` or a ProxyJump bastion is never pending-refused.  A remote host
holding an unquoted glob is refused when the glob can match a self name, since the shell
may expand the word to one (``local*:x``); a quoted glob, and a glob that keeps no literal
character (``*:*``), names no self host.  Globs are also read in the PROGRAM word (``g*t``)
and inside a command value, which a shell runs and expands (``GIT_SSH_COMMAND='ssh
local*'``).  A dotted name that resolves to THIS host through an alias is the documented
residual for git/tar: the ssh floor still covers ssh/scp, and the OS network fence is the
fix of record.

Deliberately NOT refused: normal local use -- ``git add``/``commit``/``status``/``log``/
``diff``, ``git clone /local/path``, ``git clone https://…`` (http(s)/git/ftp is not an
ssh-or-exec escape), ``git fetch origin`` with a remote that is not this host, ``tar
-czf x.tgz dir``, ``tar -xf x.tar``, and file-operand globs (``git add s* *.*``, ``tar
-czf handoff.tgz s* *.*``).

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`).  It reads a command line through
the ssh floor's own front end -- its source pre-walk (``_ssh_floor_source_text``), its
nested-payload frames and its environment tracker (``_FrameEnvironment``) -- and uses
the ssh floor's operand resolver, the host verdicts in ``argv_floor_hosts``, the tar
grammar in ``argv_floor_tar``, and the shell reader.  A new shell normalization goes in
the ssh floor, which both floors read; a new tar option or bundle rule goes in
``argv_floor_tar``; a new git/tar transport spelling goes here.
"""

from __future__ import annotations

import re
import shlex
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from . import shell_normalizer as _shell_normalizer
from . import shell_normalizer_walk as _walk
from .argv_floor_self import _SELF_FLOOR_MACHINERY_RE, _SELF_FLOOR_QUOTE_JUNK_RE
from .argv_floor_ssh import (
    _FUNCTION_BIND_CAP,
    _LITERAL_HOSTS,
    _bind_positional_parameters,
    _command_value_names_self_endpoint,
    _decode_scp_host,
    _decode_url_authority,
    _dequoted,
    _env_assignment,
    _executed_value_forms,
    _exported_environment,
    _FrameEnvironment,
    _git_config_override,
    _glob_word_spellings,
    _is_env_name,
    _mask_quoted_separators,
    _mask_sq_apostrophes,
    _open_frame_could_run_git_tar,
    _operand_targets_self,
    _ssh_floor_source_text,
    _ssh_floor_unmasked_text,
    _unmask_separators,
)
from .argv_floor_tar import (
    _GLOB_WORDS,
    _RAW_COMMAND,
    _READ_MEMO,
    _TAR_READ_VALUE_OPTS,
    _glob_host_is_self,
    _tar_archive_targets_self,
    _tar_bundle_archive_slot,
    _tar_operation_readings,
    _tar_option_takes_next_word,
)
from .shell_normalizer import (
    _decode_shell_quoted_literals,
    _ends_argv,
    _glob_could_expand_to,
    _nested_shell_payloads,
    _push_option_matches,
    _shell_join_continuations,
    _shell_payload_walk,
    _strip_redirect,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# ── git/tar sandbox-escape floor ──
# A git or tar command can open an ssh transport (or run an arbitrary command)
# that re-enters this host outside the sandbox, without ever naming an
# ssh-family verb the ssh floor reads.  The forms, all resolved below:
#   * scp-like ``host:path`` and the ssh/``git+ssh``/``ssh+git`` URI schemes
#     (git) and ``host:file`` (tar) whose host is THIS machine;
#   * a command git or tar runs in place of the transport: a decoded
#     ``git clone ext::<command>`` payload, a ``core.sshCommand`` config
#     (``-c``) or ``GIT_SSH`` / ``GIT_SSH_COMMAND`` / ``GIT_PROXY_COMMAND``
#     environment override, ``tar --rsh-command=<command>``, an
#     ``--upload-pack`` / ``--receive-pack`` / ``--exec`` (fetch-family ``-u``)
#     program and an invoked ``!`` alias.  Every one goes through
#     ``_command_value_dials_self``: a literal self host with no DNS, then the
#     whole argv-floor dispatcher re-entered on the command
#     (``_reenter_floors``).  An invoked ordinary alias is rebuilt into the
#     git invocation it becomes and re-entered the same way.  Any such command
#     runs inside the agent's own sandbox, so one that does not dial this host
#     is not an escape.
# Everything else -- local paths, bare words (git reads a bare operand as a
# local path, never ssh), http(s)/git/ftp URLs, remotes that are not this
# host, archive member globs -- stays allowed.  The git/tar verb is located behind
# ``VAR=x`` and wrappers (``sudo -u root``, ``timeout 5``) by ``argv_rules``'s reader.


# Subcommands (git 2.50 ``git help -a`` and ``git --exec-path``) that never open
# a network transport themselves.  Every other verb -- a transport verb, an
# unknown one, an alias from a config file, ``request-pull``, ``maintenance`` --
# has its operands, config and transport environment read (fail closed).  ``archive``,
# ``submodule`` and ``remote`` need a second-level classification below.
# shortcut: a partial clone can lazily fetch a missing object under a local verb;
# such a verb is still not read here.
_GIT_LOCAL_ONLY_VERBS: frozenset[str] = frozenset(
    {
        *("add", "am", "annotate", "apply", "bisect", "blame", "branch", "bugreport"),
        *("cat-file", "check-attr", "check-ignore", "check-mailmap", "check-ref-format"),
        *("checkout", "checkout-index", "cherry", "cherry-pick", "clean", "column"),
        *("commit", "commit-graph", "commit-tree", "config", "count-objects", "describe"),
        *("diagnose", "diff", "diff-files", "diff-index", "diff-pairs", "diff-tree"),
        *("fast-export", "fast-import", "fmt-merge-msg", "for-each-ref", "format-patch"),
        *("fsck", "fsck-objects", "gc", "get-tar-commit-id", "grep", "hash-object", "help"),
        *("index-pack", "init", "init-db", "interpret-trailers", "log", "ls-files"),
        *("ls-tree", "mailinfo", "mailsplit", "merge", "merge-base", "merge-file"),
        *("merge-index", "merge-one-file", "merge-tree", "mktag", "mktree"),
        *("multi-pack-index", "mv", "name-rev", "notes", "pack-objects", "pack-redundant"),
        *("pack-refs", "patch-id", "prune", "prune-packed", "range-diff", "read-tree"),
        *("rebase", "reflog", "refs", "repack", "replace", "replay", "rerere", "reset"),
        *("restore", "rev-list", "rev-parse", "revert", "rm", "shortlog", "show"),
        *("show-branch", "show-index", "show-ref", "stage", "stash", "status"),
        *("stripspace", "switch", "symbolic-ref", "tag", "unpack-file", "unpack-objects"),
        *("update-index", "update-ref", "update-server-info", "var", "verify-commit"),
        *("verify-pack", "verify-tag", "version", "whatchanged", "worktree", "write-tree"),
    }
)
# The other commands git ships (and ``lfs``), which can open a transport.  git
# ignores an alias that would hide one of them, so its own argv is read even
# when the invocation defines that alias; an unknown verb with an
# invocation-local alias is read both as the alias it becomes and as typed.
# shortcut: a third-party ``git-<verb>`` on PATH also hides an alias of its
# name; add one here when it opens a transport.
_GIT_NON_LOCAL_COMMANDS: frozenset[str] = frozenset(
    {
        *("archive", "backfill", "bundle", "checkout--worker", "citool", "clone"),
        *("contacts", "credential", "credential-cache", "credential-cache--daemon"),
        *("credential-netrc", "credential-store", "cvsexportcommit", "cvsimport"),
        *("cvsserver", "daemon", "difftool", "difftool--helper", "fetch", "fetch-pack"),
        *("filter-branch", "for-each-repo", "fsmonitor--daemon", "gitk", "gitweb", "gui"),
        *("hook", "http-backend", "http-fetch", "http-push", "imap-send", "instaweb"),
        *("lfs", "ls-remote", "maintenance", "merge-octopus", "merge-ours"),
        *("merge-recursive", "merge-resolve", "merge-subtree", "mergetool"),
        *("mergetool--lib", "p4", "pull", "push", "quiltimport", "receive-pack", "remote"),
        *("remote-ext", "remote-fd", "remote-ftp", "remote-ftps", "remote-http"),
        *("remote-https", "request-pull", "scalar", "send-email", "send-pack"),
        *("sh-i18n", "sh-i18n--envsubst", "sh-setup", "shell", "sparse-checkout"),
        *("submodule", "submodule--helper", "svn", "upload-archive", "upload-pack"),
        *("web--browse",),
    }
)
# Facade manifest names: transport discovery does not read either set to
# choose which tokens are visible.
_GIT_REMOTE_FIRST_OPERAND_VERBS: frozenset[str] = frozenset(
    {"fetch", "pull", "ls-remote", "push", "bundle", "send-pack", "fetch-pack"}
)
_GIT_REMOTE_ANY_OPERAND_VERBS: frozenset[str] = frozenset({"clone", "submodule", "remote"})


# ``git remote`` subcommands that query a remote now, rather than only editing
# the local remote configuration.
_GIT_REMOTE_CONNECTING_SUBCOMMANDS: frozenset[str] = frozenset({"show", "prune", "update"})


# The config key (lower-cased) that hands git an arbitrary ssh-replacement
# command, and the environment variables that choose a transport command.
_GIT_SSHCOMMAND_CONFIG_PREFIX = "core.sshcommand="
_GIT_SSH_ENV_NAMES: frozenset[str] = frozenset({"git_proxy_command", "git_ssh", "git_ssh_command"})


# git schemes that are NOT an ssh-or-exec escape: the connection is a plain
# socket to a server, not a login shell on this host.
_GIT_NON_ESCAPE_SCHEMES: tuple[str, ...] = (
    "http://",
    "https://",
    "git://",
    "ftp://",
    "ftps://",
    "file://",
)


# git ssh-transport scheme aliases normalized to ``ssh://`` so the ssh floor's
# operand resolver reads their authority (it models ``ssh://`` but not these).
_GIT_SSH_SCHEME_ALIASES: tuple[str, ...] = ("git+ssh://", "ssh+git://")
_SEPARATORS = frozenset({":::", "::::", ":::+", "::::+"})  # GNU ``parallel`` input separators
# A replacement string ``parallel`` puts each input in place of: ``{}``, ``{.}``, ``{1/}``...
_PLACEHOLDER_RE = re.compile(r"\{\d*(?:\.|/|//|/\.|#|%)?\}")
# GNU ``parallel`` options that name a custom replacement string (the default each
# replaces is in the comment): ``-I`` / ``--replace`` / ``-i`` for ``{}``, and
# ``--extensionreplace`` for ``{.}`` and so on.  Value by ``=`` or the next word.
_PARALLEL_REPLACE_LONG = frozenset(
    {
        "--replace",  # {}
        "--extensionreplace",  # {.}
        "--er",
        "--basenamereplace",  # {/}
        "--bnr",
        "--dirnamereplace",  # {//}
        "--dnr",
        "--basenameextensionreplace",  # {/.}
        "--bner",
        "--seqreplace",  # {#}
        "--slotreplace",  # {%}
    }
)
# A custom replacement string with one of these is not read literally (fail closed).
_PARALLEL_REPLACE_UNSAFE = "$`*?["


def _unmasked(token: str) -> str:
    """*token* with the floor's quoted-separator sentinels restored, de-quoted."""
    return _dequoted(_unmask_separators(token).strip())


def _frame_commands(source: str) -> "tuple[list[str], bool]":
    """*source*'s tokens with separators real at THIS frame's nesting level, and
    whether the frame's text ends inside a quote.

    A nested shell payload's own ``;`` / ``&&`` was masked as DATA by the
    top-level source pre-walk, because it was quoted one frame up; at the
    payload's own level an unquoted separator is a real command boundary.
    Git's sq-quote apostrophe escape is masked while the shell reader removes
    the outer assignment quotes, then restored for the config decoder; only an
    escape inside an open single quote is masked (``_mask_sq_apostrophes``).
    Restoring separators (``_unmask_separators``) and re-running the ssh floor's
    source pre-walk (``_ssh_floor_source_text``) re-masks only the separators
    quoted in THIS frame, so ``_split_commands`` sees the same boundaries the
    ssh floor's own token scan sees.  Idempotent for the top-level frame, whose
    quoted separators stay masked.  A text that ends inside a quote has every
    separator after the quote masked as data (``_git_tar_read`` reads it again).
    """
    source = _mask_sq_apostrophes(_unmask_separators(source))[0]
    text = _ssh_floor_source_text(source)
    # Read through the reader module, not bound here: a test patches ``_self_tokens``
    # on ``argv_floor_ssh_bodies``, which a second binder of the name would forbid.
    words = _shell_normalizer._self_tokens(text)
    tokens = [token.replace("\ue100", "'\\''") for token in words]
    return tokens, _mask_sq_apostrophes(text)[1]


def _shell_read_views(source: str) -> "list[str] | None":
    """The text of each command the shared shell reader finds in *source*, or None.

    ``_mask_quoted_separators`` reads quotes only: a quote in a comment
    (``# it's``), in a here-document body, or after an ANSI-C ``\\'`` (``$'a\\'b'``)
    leaves it open to the end, so the separators after it are read as data.  The
    shell reader (``read_commands``) models those, so the commands it finds are
    read too.  None when it also ends inside a quote or cannot read *source*: the
    caller refuses (fail closed).
    """
    from .shell_commands import read_commands

    text = _unmask_separators(source)
    try:
        commands = read_commands(text, heredocs=True)
    except Exception:
        return None
    if any(command.steps and command.steps[-1].state for command in commands):
        return None
    return [
        text[command.steps[0].start : command.steps[-1].end]
        for command in commands
        if command.steps
    ]


def _git_remote_targets_self(operand: str) -> bool:
    """True if a git remote *operand* dials THIS host over an ssh transport.

    Only a transport-shaped operand can dial: a URL (``scheme://``) or a
    scp-like ``host:path`` (a colon before the first slash, non-empty host).
    http(s)/git/ftp/file URLs are not an ssh-or-exec escape and return False;
    the ``git+ssh``/``ssh+git`` aliases are normalized to ``ssh://`` so the ssh
    floor's resolver reads their authority.  A BARE word -- a branch name, a
    number, ``origin``, a local path, even ``localhost`` -- is a local path to
    git, never ssh, so it returns False without any host or DNS lookup.  The
    host is read LITERALLY: ``dns=False`` (literal self addresses only; see the
    module docstring), so ``git@github.com:x`` is never pending-refused.  A glob
    host counts only as bash expands it (``_glob_host_is_self``): unquoted
    ``local*:x`` can name this host; ``'local*:x'``, ``*:*``, ``github*:x`` cannot.
    """
    t = operand.strip().strip("\"'")
    if not t:
        return False
    low = t.lower()
    if "://" in t:
        for scheme in _GIT_NON_ESCAPE_SCHEMES:
            if low.startswith(scheme):
                return False
        for alias in _GIT_SSH_SCHEME_ALIASES:
            if low.startswith(alias):
                t = "ssh://" + t[len(alias) :]
                break
        t = _decode_url_authority(t)
        return _operand_targets_self(t, host_position=True, dns=False, match_globs=False)
    head = t.split("/", 1)[0]
    if ":" not in head or not head.split(":", 1)[0]:
        return False  # a bare word or local path: git never dials it
    if _glob_host_is_self(t, head.split(":", 1)[0].rsplit("@", 1)[-1]):
        return True
    t = _decode_scp_host(t)
    if t.startswith("["):
        close = t.find("]")
        if close > 0 and t[close + 1 :].startswith(":"):
            host = t[1:close]
            if "@" in host:
                host = host.rsplit("@", 1)[1]
            before_port, sep, _port = host.rpartition(":")
            if sep and before_port and ":" not in before_port:
                host = before_port
            if _operand_targets_self(host, host_position=True, dns=False, match_globs=False):
                return True
    return _operand_targets_self(t, host_position=True, dns=False, match_globs=False)


# A value word longer than this is not split into every glued-option suffix:
# the literal reader answers "may be self" and the ssh floor's own reading of
# the value decides.  No real ssh option bundle comes near it.
_VALUE_WORD_SUFFIX_CAP = 256

# The most words the floor reads from one command value or one git argv.  A
# command past it is refused unread, the way ``MAX_SCANNABLE_COMMAND_CHARS``
# refuses an oversized one: the gate runs on the event loop, and a 20 KiB
# command can still hold about 10,000 one-letter words.  No real git or tar
# argv, ssh command or ``ext::`` payload comes near it.
_GIT_TAR_WORD_CAP = 4096

# The most characters of carried commands one read reads, summed over every
# command value it reads literally (``_value_names_literal_self``) and every
# command it re-enters the dispatcher on (``_reenter_floors``), nested reads
# included: eight times ``MAX_SCANNABLE_COMMAND_CHARS``.  A rebuilt alias command
# carries the whole environment it runs in, so a command line of many git
# commands that share a large exported environment and each invoke a different
# alias would otherwise re-read that environment once per command.  A read past
# it is refused (fail closed).  A value or command read again on the same chain
# is answered from ``_READ_MEMO`` and counts once.
_CARRIED_READ_CHAR_BUDGET = 8 * 20 * 1024


def _value_names_literal_self(value: str) -> "bool | None":
    """True if a command *value* names a LITERAL self host anywhere in it.

    A WIDE literal read with no DNS: the value is read through the ssh floor's
    source pre-walk (``_ssh_floor_source_text``: static substitutions, same-line
    assignments), its ANSI-C quoting is resolved and its quotes removed, then
    every word -- and, for an option word, every glued suffix (``-Jlocalhost``,
    ``-4vJlocalhost``) -- is checked by the ssh floor's literal endpoint reader
    (``_operand_targets_self(..., dns=False)`` plus its ``:`` / ``=`` / ``,``
    fragments).  A word, or an option word's ``=`` value, is also read as a git
    remote (``_git_remote_targets_self``, literal too), so a carried
    ``git+ssh://``, bracketed or ``%``-encoded self remote is seen.  It only
    narrows: ``_command_value_dials_self`` still needs a floor to refuse the
    value.  A value of more than ``_GIT_TAR_WORD_CAP`` words is not read: the
    answer is None, and ``_command_value_dials_self`` refuses it.  Within a read
    the answer is kept per value (``_READ_MEMO``), so a value inherited by many
    commands is read once, and a value read past ``_CARRIED_READ_CHAR_BUDGET``
    is not read either (None).
    """
    memo = _READ_MEMO.get()
    key = ("literal", value)
    if memo is not None:
        if key in memo:
            kept: "bool | None" = memo[key]
            return kept
        spent = memo.get(("carried-chars",), 0) + len(value)
        memo[("carried-chars",)] = spent
        if spent > _CARRIED_READ_CHAR_BUDGET:
            return None
    text = _unmask_separators(_ssh_floor_source_text(value))
    text = _decode_shell_quoted_literals(text)
    text = text.replace('"', "").replace("'", "").replace("\\", "")
    # An ``ext::`` payload carried inside the value embeds a space as ``% ``;
    # reading it as a plain space keeps the host before it a word of its own.
    text = text.replace("% ", " ")
    words = text.split()
    verdict: "bool | None" = None
    if len(words) <= _GIT_TAR_WORD_CAP:
        verdict = any(_value_word_names_literal_self(word) for word in words)
    if memo is not None:
        memo[key] = verdict
    return verdict


def _value_word_names_literal_self(word: str) -> bool:
    """True if one de-quoted *word* of a command value names a literal self host.

    ``_value_names_literal_self``'s reading of each word: a plain word as an
    endpoint or a git remote; an option word by its ``=`` value as a git remote
    and by every glued suffix as an endpoint, and one longer than
    ``_VALUE_WORD_SUFFIX_CAP`` as "may be self".  Reading every suffix of an
    option word costs the square of its length, so within a read that answer is
    kept per word (``_READ_MEMO``): an option word many values carry is read once.
    """
    if not word.startswith("-"):
        return _command_value_names_self_endpoint(word) or _git_remote_targets_self(word)
    bare = word.lstrip("-")
    if len(bare) > _VALUE_WORD_SUFFIX_CAP:
        return True
    memo = _READ_MEMO.get()
    key = ("option-word", bare)
    if memo is not None and key in memo:
        kept: bool = memo[key]
        return kept
    _, sep, attached = bare.partition("=")
    verdict = bool(sep and _git_remote_targets_self(attached)) or any(
        _command_value_names_self_endpoint(bare[start:]) for start in range(len(bare))
    )
    if memo is not None:
        memo[key] = verdict
    return verdict


#: Where the floor read in progress sits inside commands carried by other commands:
#: ``(depth, alias names expanded, command texts re-entered)`` along the current
#: chain only.  ``_reenter_floors`` threads it, since the dispatcher takes a string.
_REENTRY_CHAIN: "ContextVar[tuple[int, frozenset[str], frozenset[str]]]" = ContextVar(
    "git_tar_floor_reentry_chain", default=(0, frozenset(), frozenset())
)


def _reenter_floors(command: str, *, alias: "str | None" = None, literal: bool = False) -> bool:
    """True if the argv-floor dispatcher refuses *command*, which this git/tar command runs.

    *command* is a whole command line a git or tar invocation hands to a shell or
    runs as its transport (a command value), or the git invocation an invoked
    ordinary alias becomes.  It is read by the SAME dispatcher ``is_denied`` runs
    on an agent's whole command (``deny_gate._reentered_floor_hit``: every
    self-protection argv floor, row-backed ones only while the operator's row set
    enables them, the hit reported under the inner row's own id), so a floor added
    there reads carried commands too.  Re-entry is bounded by the ssh
    floor's named-body expansion limit, ``_FUNCTION_BIND_CAP``: a chain deeper
    than that, an alias name already expanded on this chain (*alias*) or a
    command text already re-entered on it is a loop git itself would reject, and
    is refused (fail closed), as is a dispatcher that raises on the command.
    Within a read (``_READ_MEMO``) a command no floor refused is not re-entered
    again on the same chain, and a read that has read more than
    ``_CARRIED_READ_CHAR_BUDGET`` characters of carried commands is refused
    (fail closed).  Only a refusal-free answer is kept: a refusal reports the
    inner row's hit (``_reentered_floor_hit``), so it is always read afresh.
    A *literal* read, and every read nested in it, reads hosts with no DNS
    (``argv_floor_ssh._LITERAL_HOSTS``).
    """
    state = _REENTRY_CHAIN.get()
    depth, aliases, commands = state
    literal = literal or _LITERAL_HOSTS.get()
    if depth >= _FUNCTION_BIND_CAP or command in commands:
        return True
    if alias is not None:
        if alias in aliases:
            return True
        aliases = aliases | {alias}
    memo = _READ_MEMO.get()
    key = ("reentry", command, alias, state, literal)
    if memo is not None:
        if key in memo:
            return False
        spent = memo.get(("carried-chars",), 0) + len(command)
        memo[("carried-chars",)] = spent
        if spent > _CARRIED_READ_CHAR_BUDGET:
            return True
    from .deny_gate import _reentered_floor_hit

    chain = _REENTRY_CHAIN.set((depth + 1, aliases, commands | {command}))
    hosts = _LITERAL_HOSTS.set(literal)
    try:
        refused = _reentered_floor_hit(command) is not None
    except Exception:  # incl. RecursionError: an unreadable carried command
        return True
    finally:
        _REENTRY_CHAIN.reset(chain)
        _LITERAL_HOSTS.reset(hosts)
    if not refused and memo is not None:
        memo[key] = False
    return refused


def _command_value_dials_self(value: str, *, alias: "str | None" = None) -> bool:
    """True if a command *value* git or tar runs dials THIS host.

    Every command-carrying value comes here: ``GIT_SSH`` / ``GIT_SSH_COMMAND`` /
    ``GIT_PROXY_COMMAND``, any invocation-local config value (``core.sshCommand``,
    ``core.gitProxy`` and the rest), a decoded ``ext::`` payload, an
    ``--upload-pack`` / ``--receive-pack`` / ``--exec`` / ``-u`` program, an
    invoked ``!`` alias (*alias* names it) and ``tar --rsh-command``.  It is
    refused when it names a literal self host (``_value_names_literal_self``, no
    DNS) AND the whole argv-floor dispatcher refuses it (``_reenter_floors``), so
    ``git ls-remote localhost:x`` is read by this floor and ``ssh localhost`` by
    the ssh floor.  The literal read runs first, so a value naming only remote
    hosts -- ``ssh -o ProxyJump=bastion.example.com`` -- never reaches the ssh
    floor's DNS layer and is never pending-refused; one re-entered only because it
    runs git or tar (``ssh -l git -J bastion.example.com``) is read with no DNS.  A
    value too long for the literal read (``_GIT_TAR_WORD_CAP``) is refused unread
    (fail closed).

    The value is read in every form git runs it (``_executed_value_forms``):
    as given, and -- when it begins with ``!`` -- with that character removed,
    the way git strips the ``!`` of a ``credential.helper`` or an ``alias``
    body before handing the rest to the shell.  Either form dialing self is a
    refusal (fail closed), so ``credential.helper='!ssh localhost id'`` is read
    as ``ssh localhost id`` the ssh floor refuses.
    """
    for candidate in _executed_value_forms(value):
        names_self = _value_names_literal_self(candidate)
        if names_self is None:
            return True
        # A carried git or tar command is read even when it names no literal self
        # host: ``git -c url.<base>.insteadOf=`` can rewrite a remote to one.
        if (names_self or _value_runs_git_or_tar(candidate)) and (
            _reenter_floors(candidate, alias=alias, literal=not names_self)
        ):
            return True
    return False


def _value_runs_git_or_tar(value: str) -> bool:
    """True if any word of the command *value* could run git or tar.

    Any position counts, so a wrapper (``env git``) or a shell (``sh -c "git ..."``)
    in front is read too.  A value with no such word
    (``ssh -o ProxyJump=bastion.example.com``) keeps the literal prefilter, so it
    never reaches the ssh floor's DNS layer.  The value is read through the ssh
    floor's source pre-walk first, so ``G=git; $G ...`` is the ``git`` it runs; a
    word still ``$``-built (``${G:-git}``) or a glob ``_git_tar_verbs`` reads as one
    (``/usr/bin/g?t``) may be git or tar."""
    text = _ssh_floor_source_text(_unmask_separators(value).lower())
    for word in text.translate(str.maketrans(";|&()`", "      ")).split():
        bare = _unmasked(word.replace("'", "").replace('"', "").replace("\\", ""))
        if bare.startswith("$") or any(_git_tar_verbs(bare)):
            return True
    return False


def _git_ext_command(operand: str, following: "list[str]") -> "str | None":
    """The command an ``ext::<command>`` remote *operand* runs, else None.

    An unquoted ``ext::ssh localhost id`` reaches git as several operands, so
    the *following* operands are appended -- reading more words only widens
    what ``_command_value_dials_self`` sees; ``_git_tokens_target_self`` passes
    them only up to the next ``ext::`` payload.  Git separates arguments on
    unescaped spaces; ``% `` embeds a space in one argument and ``%%`` embeds a
    percent sign.
    """
    t = operand.strip().strip("\"'")
    if not t.lower().startswith("ext::"):
        return None
    encoded = " ".join([t[len("ext::") :], *following])
    arguments = [""]
    index = 0
    while index < len(encoded):
        if encoded.startswith("% ", index):
            arguments[-1] += " "
            index += 2
        elif encoded.startswith("%%", index):
            arguments[-1] += "%"
            index += 2
        elif encoded[index] == " ":
            arguments.append("")
            index += 1
        else:
            arguments[-1] += encoded[index]
            index += 1
    return " ".join(arguments)


def _git_remote_candidates(token: str) -> "tuple[str, ...]":
    """Transport operands or executable overrides carried by one post-verb *token*.

    The whole token covers ordinary operands and detached option values: every
    post-verb token is a candidate, so ``--opt value`` has its value read
    whatever the option.  The attached ``--opt=value`` therefore has its value
    read for every long option too (``--repo=``, ``--remote=``,
    ``--upload-pack=``), and fetch's glued short ``-u<program>``.  Config
    options are also classified separately with their key intact.
    """
    t = token.strip().strip("\"'")
    candidates = [t]
    if t.startswith("--"):
        _, sep, attached = t.partition("=")
        if sep:
            candidates.append(attached)
    if t.startswith("-u") and len(t) > 2:
        short_tail = t[2:]
        candidates.append(short_tail[1:] if short_tail.startswith("=") else short_tail)
    return tuple(candidate for candidate in candidates if candidate)


def _git_nested_subcommand(tokens: "list[str]") -> "str | None":
    """First non-option word below ``git remote`` or ``git submodule``."""
    options_ended = False
    for tok in tokens:
        if _ends_argv(tok) and tok in (";", "&", "|", "\n", "&&", "||"):
            break
        if not options_ended and tok == "--":
            options_ended = True
            continue
        if options_ended or not tok.startswith("-"):
            return tok.lower()
    return None


def _git_options_before_terminator(tokens: "list[str]") -> "tuple[str, ...]":
    """Option tokens before this git subcommand's genuine ``--`` terminator."""
    options: "list[str]" = []
    for tok in tokens:
        if _ends_argv(tok) and tok in (";", "&", "|", "\n", "&&", "||"):
            break
        if tok == "--":
            break
        if tok.startswith("-"):
            options.append(tok)
    return tuple(options)


def _git_transport_modes(verb: str, tokens: "list[str]") -> "tuple[bool, bool]":
    """Return ``(opens_transport, inspect_remote_tokens)`` for this git verb.

    A verb not known to be local-only is read in full, so a missing verb fails
    closed rather than open.
    """
    if verb in _GIT_LOCAL_ONLY_VERBS:
        return False, False
    options = _git_options_before_terminator(tokens)
    if verb == "archive":
        opens = any(tok == "--remote" or tok.startswith("--remote=") for tok in options)
        return opens, opens
    if verb == "submodule":
        opens = _git_nested_subcommand(tokens) in ("add", "update")
        return opens, opens
    if verb != "remote":
        return True, True

    subcommand = _git_nested_subcommand(tokens)
    if subcommand == "add":
        opens = any(tok in ("-f", "--fetch") for tok in options)
        return opens, True  # records a URL even without ``--fetch``
    if subcommand == "set-url":
        records = "--delete" not in options
        return False, records
    if subcommand in _GIT_REMOTE_CONNECTING_SUBCOMMANDS:
        opens = subcommand != "show" or not any(tok in ("-n", "--no-query") for tok in options)
        return opens, opens
    if subcommand == "set-head":
        opens = any(tok in ("-a", "--auto") for tok in options)
        return opens, opens
    return False, False


def _git_tokens_target_self(tokens: "list[str]") -> bool:
    """True if any post-verb transport operand or executable targets self.

    An ``ext::`` operand is read with the operands after it up to the next
    ``ext::`` operand, and an option's ``ext::`` value (``--opt=ext::x``,
    ``-uext::x``) up to the next token carrying either, so each token is decoded
    into at most two payloads and the read stays linear.  An argv of more than
    ``_GIT_TAR_WORD_CAP`` words is refused unread (fail closed).
    """
    words: "list[str]" = []
    for tok in tokens:
        if _ends_argv(tok) and tok in (";", "&", "|", "\n", "&&", "||"):
            break
        words.append(tok)
    if len(words) > _GIT_TAR_WORD_CAP:
        return True
    candidates = [_git_remote_candidates(tok) for tok in words]
    payload_ends: "list[tuple[int, int]]" = []
    operand_end = value_end = len(words)
    for idx in reversed(range(len(words))):
        payload_ends.append((operand_end, value_end))
        if _git_ext_command(words[idx], []) is not None:
            operand_end = value_end = idx
        elif any(_git_ext_command(candidate, []) is not None for candidate in candidates[idx]):
            value_end = idx
    payload_ends.reverse()
    for idx, token_candidates in enumerate(candidates):
        for position, masked_candidate in enumerate(token_candidates):
            candidate = _unmasked(masked_candidate)
            if _git_remote_targets_self(candidate) or _command_value_dials_self(candidate):
                return True
            # Position 0 is the whole token; the rest are an option's attached value.
            following = words[idx + 1 : payload_ends[idx][min(position, 1)]]
            ext_command = _git_ext_command(masked_candidate, following)
            if ext_command is not None and _command_value_dials_self(_unmasked(ext_command)):
                return True
    return False


def _observe_words(frame_env: "_FrameEnvironment", words: "list[str]", prog: bool = False) -> None:
    """Feed each of *words* (de-quoted) to *frame_env*; the first is the program if *prog*."""
    for index, word in enumerate(words):
        frame_env.observe(word, word, prog and not index)


def _git_verb_at(tokens: "list[str]") -> "tuple[int, str, list[tuple[str, str]]]":
    """``(index, verb word, global config overrides)`` of git's subcommand in *tokens*."""
    i = 0
    n = len(tokens)
    global_overrides: "list[tuple[str, str]]" = []
    # Locate the subcommand first.  Global ssh-command overrides are collected
    # here but checked only after the subcommand is known to open a transport.
    # A redirection is not a word of git's argv (bash removes it before exec),
    # so it is stepped over the way the ssh floor steps over it: a bare or
    # fd-prefixed operator (``2>/dev/null``, ``>&2``, ``<file``, ``&>x``) is
    # skipped, and a detached operator (``>`` ``file``, ``2>`` ``file``) also
    # skips its target.  ``clone>/dev/null`` keeps its word part as the verb.
    redirect_target_pending = False
    verb_word = ""
    while i < n:
        tok = tokens[i]
        if redirect_target_pending:
            redirect_target_pending = False
            i += 1
            continue
        if ">" in tok or "<" in tok:
            remainder = _strip_redirect(tok)
            if not remainder or remainder.isdigit() or remainder == "&":
                redirect_target_pending = tok.endswith((">", "<"))
                i += 1
                continue
            if not remainder.startswith("-"):
                verb_word = remainder
                break
        if not tok.startswith("-"):
            verb_word = tok
            break
        config_override = _git_config_override(tokens, i)
        if config_override is not None:
            option, config, consumed = config_override
            global_overrides.append((option, config))
            i += consumed
            continue
        if tok in (
            "-C",
            "--git-dir",
            "--work-tree",
            "--namespace",
            "--super-prefix",
            "--attr-source",
        ):
            i += 2  # option + its value
            continue
        i += 1
    return i, verb_word, global_overrides


def _git_command_targets_self(tokens: "list[str]", env: "dict[str, list[str]]") -> bool:
    """True if a ``git`` command's *tokens* (its args) dial THIS host, directly or not."""
    i, verb_word, global_overrides = _git_verb_at(tokens)
    if i >= len(tokens):
        return False

    verb = _unmasked(verb_word).lower()
    command_tokens = tokens[i + 1 :]
    local_overrides: "list[tuple[str, str]]" = []
    idx = 0
    while idx < len(command_tokens):
        tok = command_tokens[idx]
        if tok == "--":
            break
        config_override = _git_config_override(command_tokens, idx)
        if config_override is None:
            idx += 1
            continue
        option, config, consumed = config_override
        local_overrides.append((option, config))
        idx += consumed

    # The numbered environment config (``GIT_CONFIG_COUNT`` with
    # ``GIT_CONFIG_KEY_<n>`` / ``GIT_CONFIG_VALUE_<n>``) is the same config
    # as ``-c <key>=<value>``.  Every key present is read, a superset of
    # the ones the count enables.  Each key takes every value kept under its
    # ``GIT_CONFIG_VALUE_<n>``, the way ``--config-env=<key>=<name>`` takes every
    # value kept under ``<name>``, so it is read as that option: the values are
    # read once per name, not once per key.
    numbered_overrides = [
        ("--config-env", key + "=git_config_value_" + name[len("git_config_key_") :])
        for name, keys in env.items()
        if name.startswith("git_config_key_")
        for key in keys
    ]
    parameters = env.get("git_config_parameters", [])
    cache: "dict[tuple[object, ...], Any]" = {}

    opens_transport, inspect_remotes = _git_transport_modes(verb, command_tokens)
    if not inspect_remotes or verb not in _GIT_NON_LOCAL_COMMANDS:
        bodies = _git_alias_bodies(
            verb, global_overrides, local_overrides, numbered_overrides, parameters, env, cache
        )
        if bodies or not inspect_remotes:
            alias_dials = _git_alias_dials_self(
                verb, tokens[:i], command_tokens, bodies, global_overrides, env
            )
            # git runs a ``git-<verb>`` program (``git-subtree``) before an alias
            # of its name, so a verb outside both tables is ALSO read as typed.
            if alias_dials or not inspect_remotes:
                # This path returns before the config scan below, so still
                # refuse an invocation-local config value that git RUNS even on
                # a local verb: ``core.fsmonitor`` runs during ``git status``,
                # and a ``-c core.sshCommand`` is carried through
                # GIT_CONFIG_PARAMETERS into a nested transport.  ``alias.<name>``
                # keys are skipped: a merely DEFINED alias is never run, and an
                # invoked one is read by the alias block above.
                if not alias_dials and (
                    any(
                        _sshcommand_config_dials_self(config, env, option, cache, skip_alias=True)
                        for option, config in (
                            *global_overrides,
                            *local_overrides,
                            *numbered_overrides,
                        )
                    )
                    or any(
                        _config_parameters_dial_self(value, skip_alias=True) for value in parameters
                    )
                ):
                    return True
                return alias_dials

    if opens_transport:
        # A transport-command environment override matters only when this
        # invocation can run a transport.  Local verbs merely store/read data.
        for name in _GIT_SSH_ENV_NAMES:
            if any(_command_value_dials_self(_unmasked(value)) for value in env.get(name, ())):
                return True

    # A transport invocation inspects every invocation-local config value as a
    # command git could run (``_sshcommand_config_dials_self`` is key-agnostic),
    # including an unused ``alias.<name>`` value (an accepted over-refusal).
    for option, config in (*global_overrides, *local_overrides, *numbered_overrides):
        if _sshcommand_config_dials_self(config, env, option, cache):
            return True
    if any(_config_parameters_dial_self(value) for value in parameters):
        return True

    # Remote and command discovery is token-agnostic: no option grammar
    # decision can hide a self-targeting operand or executable override.
    if _git_tokens_target_self(command_tokens):
        return True
    return _git_rewritten_remotes_dial_self(
        verb == "push",
        command_tokens,
        (*global_overrides, *local_overrides, *numbered_overrides),
        parameters,
        env,
        cache,
    )


def _git_rewritten_remotes_dial_self(
    push: bool,
    tokens: "list[str]",
    overrides: "Iterable[tuple[str, str]]",
    parameters: "list[str]",
    env: "dict[str, list[str]]",
    cache: "dict[tuple[object, ...], Any]",
) -> bool:
    """True if a remote, once git applies this invocation's URL rewrites, dials THIS host.

    git rewrites a remote by the LONGEST ``url.<base>.insteadOf`` prefix it
    starts with (``pushInsteadOf`` first when it pushes), so
    ``url.ssh://local.insteadOf=foo`` turns ``foohost/repo`` into
    ``ssh://localhost/repo`` though neither the base nor the operand names this
    host.  Rewrites come from every config source the floor reads (*overrides*
    and the *parameters* values).  Each post-verb word (``_git_remote_candidates``)
    is rewritten the way git does and read whole (``_url_rewrite_base_dials_self``);
    a ``remote.<name>.url`` / ``pushurl`` or ``submodule.<name>.url`` value is
    rewritten through every rule set, since which one git picks depends on the
    remote's other keys.  Equal longest prefixes are all read (fail closed).
    """
    fetch_rules: "dict[str, list[str]]" = {}
    push_rules: "dict[str, list[str]]" = {}
    remote_urls: "list[str]" = []

    def collect(key: str, values: "list[str]") -> None:
        low_key = key.lower()
        if low_key.startswith(("remote.", "submodule.")) and low_key.endswith((".url", ".pushurl")):
            remote_urls.extend(values)
            return
        for suffix, rules in ((".insteadof", fetch_rules), (".pushinsteadof", push_rules)):
            if low_key.startswith("url.") and low_key.endswith(suffix):
                base = key[len("url.") : -len(suffix)]
                for prefix in values:
                    rules.setdefault(prefix, []).append(base)

    for option, config in overrides:
        entry = _config_entries(option, config, env, cache)
        if entry is not None:
            collect(entry[0], entry[1])
    memo = _READ_MEMO.get()
    for value in parameters:
        # A value's rewrite entries are picked out once per read, so a value
        # exported to many git commands is not re-scanned by each.
        picked: "list[tuple[str, str]] | None" = (
            memo.get(("parameters-rewrites", value)) if memo is not None else None
        )
        if picked is None:
            picked = []
            for parameter in _config_parameter_entries(value):
                key, sep, carried = _unmasked(parameter).partition("=")
                if sep and key.lower().startswith(("url.", "remote.", "submodule.")):
                    picked.append((key, carried))
            if memo is not None:
                memo[("parameters-rewrites", value)] = picked
        for key, carried in picked:
            collect(key, [carried])
    if not fetch_rules and not push_rules:
        return False

    # Only the prefix lengths some rule has are tried, longest first; they are
    # sorted once, not once per remote.
    fetch_set = (fetch_rules, sorted({len(prefix) for prefix in fetch_rules}, reverse=True))
    push_set = (push_rules, sorted({len(prefix) for prefix in push_rules}, reverse=True))

    def dials(remote: str, rule_sets: "tuple[tuple[dict[str, list[str]], list[int]], ...]") -> bool:
        for rules, lengths in rule_sets:
            for length in lengths:
                bases = rules.get(remote[:length]) if length <= len(remote) else None
                if bases is not None:
                    tail = remote[length:]
                    return any(_url_rewrite_base_dials_self(base + tail) for base in bases)
        return False

    operand_sets = (push_set, fetch_set) if push else (fetch_set,)
    for tok in tokens:
        if _ends_argv(tok) and tok in (";", "&", "|", "\n", "&&", "||"):
            break
        for candidate in _git_remote_candidates(tok):
            if dials(_unmasked(candidate), operand_sets):
                return True
    return any(dials(url, (rules,)) for url in remote_urls for rules in (push_set, fetch_set))


def _config_entries(
    option: str,
    config: str,
    env: "dict[str, list[str]]",
    cache: "dict[tuple[object, ...], Any] | None" = None,
) -> "tuple[str, list[str], str | None] | None":
    """``(key, values, name)`` of the entry one invocation-local config *option* supplies.

    None when *config* has no ``=``.  For ``--config-env`` the value names a
    variable in the command environment (*name*), and every value kept under it
    is a value of the entry; *cache* keeps the de-quoted values per name, so a
    name many options point at is read once.  A ``-c`` / ``--config`` entry has
    its one value and no name.
    """
    key, sep, value = _unmasked(config).partition("=")
    if not sep:
        return None
    if option != "--config-env":
        return key, [value], None
    name = value.lower()
    values = cache.get(("values", name)) if cache is not None else None
    if values is None:
        values = [_unmasked(carried) for carried in env.get(name) or [""]]
        if cache is not None:
            cache[("values", name)] = values
    return key, values, name


def _sshcommand_config_dials_self(
    config: str,
    env: "dict[str, list[str]]",
    option: str,
    cache: "dict[tuple[object, ...], Any] | None" = None,
    skip_alias: bool = False,
) -> bool:
    """True if an invocation-local git config entry dials this host.

    Every value is checked as a command git could execute
    (``_command_value_dials_self``).  Only transport-URL keys are also checked as
    remotes, including the rewrite base carried in a ``url.<base>.insteadOf`` or
    ``url.<base>.pushInsteadOf`` key.  *cache* keeps the answer for the values
    of a ``--config-env`` name, so options pointing at the same name do not
    re-read its values.  With *skip_alias* set, an ``alias.<name>`` key reads as
    no self-dial: the caller is on the local-only path, where git never runs a
    merely DEFINED alias (an invoked one is read by the alias block).
    """
    entry = _config_entries(option, config, env, cache)
    if entry is None:
        return False
    key, values, name = entry
    low_key = key.lower()
    if skip_alias and low_key.startswith("alias."):
        return False
    # The remote/URL check is scoped to a TRANSPORT-URL key only.  An
    # http-proxy value (``http.proxy=localhost:3128``) is a plain
    # ``host:port`` for a proxy, not a dialed transport, so a key-agnostic
    # remote check over-refused it.
    remote = _is_transport_url_key(low_key)
    verdict = cache.get(("dials", name, remote)) if cache is not None and name else None
    if verdict is None:
        # The command-value check is KEY-AGNOSTIC: any config value git runs as
        # a command (``core.sshCommand``, ``core.gitProxy`` and the rest) is read
        # as one.
        verdict = any(_command_value_dials_self(value) for value in values) or (
            remote and any(_git_remote_targets_self(value) for value in values)
        )
        if cache is not None and name:
            cache[("dials", name, remote)] = verdict
    if verdict:
        return True
    if not low_key.startswith("url."):
        return False
    return any(
        low_key.endswith(suffix) and _url_rewrite_base_dials_self(key[len("url.") : -len(suffix)])
        for suffix in (".insteadof", ".pushinsteadof")
    )


def _url_rewrite_base_dials_self(base: str) -> bool:
    """True if a ``url.<base>.insteadOf`` / ``pushInsteadOf`` rewrite *base* dials THIS host.

    git rewrites a remote URL to BEGIN with *base*, so a base that names a
    transport to this host reaches it.  An ssh/scp base (``ssh://localhost/``,
    ``localhost:``) is read as a remote (``_git_remote_targets_self``); an
    ``ext::`` base is decoded to the command it runs and read as one
    (``_command_value_dials_self``), the same reading an ``ext::`` operand gets,
    so ``url.ext::ssh localhost id.insteadOf`` is refused while an ``ext::`` base
    to a remote host stays allowed.  A base that is neither names no transport.
    """
    if _git_remote_targets_self(base):
        return True
    ext_command = _git_ext_command(base, [])
    return ext_command is not None and _command_value_dials_self(ext_command)


def _config_parameter_entries(parameters: str) -> "list[str]":
    """The ``<key>=<value>`` entries of one ``GIT_CONFIG_PARAMETERS`` value, parsed once per read."""
    memo = _READ_MEMO.get()
    key = ("parameters", parameters)
    if memo is not None and key in memo:
        kept: "list[str]" = memo[key]
        return kept
    try:
        entries = shlex.split(_unmask_separators(parameters), comments=False, posix=True)
    except ValueError:
        # shortcut: malformed sq-quoting falls back to whitespace before the classifier.
        entries = _unmask_separators(parameters).split()
    if memo is not None:
        memo[key] = entries
    return entries


def _config_parameters_dial_self(parameters: str, skip_alias: bool = False) -> bool:
    """True if a ``GIT_CONFIG_PARAMETERS`` value carries a config entry that dials this host.

    Each entry is read as ``-c <entry>`` (``_sshcommand_config_dials_self``).
    Within a read a refusal-free answer is kept per value and re-entry chain
    (``_READ_MEMO``), so a value exported to many git commands is read once; a
    refusal is read afresh, like ``_reenter_floors``'s.  *skip_alias* is passed
    through to ``_sshcommand_config_dials_self`` for the local-only path.
    """
    memo = _READ_MEMO.get()
    key = ("parameters-dial", parameters, _REENTRY_CHAIN.get(), skip_alias)
    if memo is not None and key in memo:
        return False
    entries = _config_parameter_entries(parameters)
    if any(
        _sshcommand_config_dials_self(entry, {}, "-c", skip_alias=skip_alias) for entry in entries
    ):
        return True
    if memo is not None:
        memo[key] = False
    return False


def _git_alias_bodies(
    verb: str,
    global_overrides: "list[tuple[str, str]]",
    local_overrides: "list[tuple[str, str]]",
    numbered_overrides: "list[tuple[str, str]]",
    parameters: "list[str]",
    env: "dict[str, list[str]]",
    cache: "dict[tuple[object, ...], Any] | None" = None,
) -> "list[str]":
    """The definitions git can run for alias *verb*, each once, in reading order.

    git reads the numbered environment config, then ``GIT_CONFIG_PARAMETERS``:
    the inherited value followed by this invocation's own ``-c`` /
    ``--config-env`` options (*global_overrides*) in order, and the LAST
    ``alias.<verb>`` it reads wins.  So the last of those options that defines
    the alias decides (with every value kept under a ``--config-env`` name);
    without one, the last definition in each kept ``GIT_CONFIG_PARAMETERS``
    value and every numbered one (the floor reads every numbered key, so which
    is last is not known) are kept.  The last definition after the verb
    (*local_overrides*), which git does not read as config, is kept too, as the
    floor always has.  Each source is read once: no definition rescans the rest.
    """
    alias_key = "alias." + verb

    def last_definition(overrides: "list[tuple[str, str]]") -> "list[str]":
        found: "tuple[str, str] | None" = None
        for option, config in overrides:
            key, sep, _ = _unmasked(config).partition("=")
            if sep and key.lower() == alias_key:
                found = (option, config)
        if found is None:
            return []
        entry = _config_entries(found[0], found[1], env, cache)
        return list(entry[1]) if entry is not None else []

    memo = _READ_MEMO.get()
    bodies = last_definition(global_overrides)
    if not bodies:
        for value in parameters:
            # The alias map of a value is built once per read: name -> last entry.
            aliases: "dict[str, str] | None" = (
                memo.get(("parameters-aliases", value)) if memo is not None else None
            )
            if aliases is None:
                aliases = {}
                for parameter in _config_parameter_entries(value):
                    key, sep, _ = _unmasked(parameter).partition("=")
                    if sep and key.lower().startswith("alias."):
                        aliases[key.lower()] = parameter
                if memo is not None:
                    memo[("parameters-aliases", value)] = aliases
            if alias_key in aliases:
                bodies.extend(last_definition([("-c", aliases[alias_key])]))
        for option, config in numbered_overrides:
            entry = _config_entries(option, config, env, cache)
            if entry is not None and entry[0].lower() == alias_key:
                bodies.extend(entry[1])
    bodies.extend(last_definition(local_overrides))
    return list(dict.fromkeys(bodies))


def _git_alias_dials_self(
    verb: str,
    prefix: "list[str]",
    operands: "list[str]",
    bodies: "list[str]",
    global_overrides: "list[tuple[str, str]]",
    env: "dict[str, list[str]]",
) -> bool:
    """True if *verb* invokes an invocation-local alias whose command dials this host.

    *bodies* are the alias's definitions git can run (``_git_alias_bodies``).
    git replaces the alias word with its definition and keeps the rest of the
    invocation, so the command is rebuilt whole and re-entered
    (``_reenter_floors``): its environment, then for an ordinary alias
    ``git``, every word before the verb (*prefix*: ``-c``, ``--config-env``,
    ``-C`` …), the alias's git arguments and the invocation's *operands*; for a
    ``!`` alias, its shell command followed by the *operands*, run with git's
    ``-c`` config in ``GIT_CONFIG_PARAMETERS`` the way git hands it to the
    child.  git runs a ``!`` alias as ``sh -c '<body> "$@"' '<body>' <operands>``,
    so the body's ``$1`` / ``${N}`` / ``$@`` / ``$*`` are bound to the operands
    (``_bind_positional_parameters``) before the operands are appended.  A
    nested alias is expanded by the re-entered read itself, and
    re-expanding one already on the chain is refused as a loop.  The rebuilt
    environment and words are built once for every definition.
    """
    if not bodies:
        return False
    words = [shlex.quote(_unmask_separators(tok)) for tok in operands]
    prefix_text = " ".join(prefix)
    operands_text = " ".join(operands)
    override_text = " ".join(config for _option, config in global_overrides)

    def _env_for_reentry(*referencing: str) -> "dict[str, list[str]]":
        """*env* cut to the names a rebuilt command can actually read.

        A rebuilt alias command re-enters the whole dispatcher as text, so the
        environment leading it is re-parsed on every re-entry.  Only a variable
        that STEERS a transport -- the ``GIT_SSH*`` commands, the numbered and
        ``GIT_CONFIG_PARAMETERS`` git config, tar's ``TAR_OPTIONS``/``TAPE`` --
        or a variable the rebuilt command NAMES (``$VAR`` or a ``--config-env``
        target, found by its lower-cased name in *referencing*) can change a
        self-dial verdict.  An unreferenced ordinary export (a ``PATH``-like
        value) cannot, so dropping it keeps every real self-dial while bounding
        the work: a large unrelated ``export`` is not re-parsed once per
        alias (the ~27 s nine-alias worst case).
        """
        haystack = " ".join(referencing).lower()
        return {
            name: values
            for name, values in env.items()
            if name in _GIT_SSH_ENV_NAMES
            or name in ("git_config_parameters", "git_config_count", "tar_options", "tape")
            or name.startswith(("git_config_key_", "git_config_value_"))
            or name in haystack
        }

    environment = _exported_environment(
        _env_for_reentry(prefix_text, override_text, operands_text, " ".join(bodies))
    )
    for body in bodies:
        if body.startswith("!"):
            entries = [_config_entries(option, config, env) for option, config in global_overrides]
            child_parameters = " ".join(
                shlex.quote(entry[0] + "=" + value)
                for entry in entries
                if entry is not None
                for value in entry[1]
            )
            extra = (
                ["git_config_parameters=" + shlex.quote(child_parameters)]
                if child_parameters
                else []
            )
            shell_environment = _exported_environment(
                _env_for_reentry(prefix_text, override_text, operands_text, body, child_parameters),
                extra,
            )
            command = shell_environment + " ".join(
                [_bind_positional_parameters(body[1:], words), *words]
            )
            if _command_value_dials_self(command, alias=verb):
                return True
            continue
        try:
            alias_args = shlex.split(body, comments=False, posix=True)
        except ValueError:
            alias_args = body.split()
        if not alias_args:
            continue
        rebuilt = environment + " ".join(
            [
                "git",
                *(shlex.quote(_unmask_separators(tok)) for tok in prefix),
                *(shlex.quote(arg) for arg in alias_args),
                *words,
            ]
        )
        if _reenter_floors(rebuilt, alias=verb):
            return True
    return False


def _is_transport_url_key(low_key: str) -> bool:
    """True if a lower-cased git config *low_key* carries a transport URL value.

    The keys whose VALUE is a remote git can dial: ``remote.<name>.url``,
    ``remote.<name>.pushurl`` and the ``url.<base>.insteadOf`` /
    ``url.<base>.pushInsteadOf`` rewrites (checked for their value here; the
    rewrite's base is checked by the key-suffix branch in
    ``_sshcommand_config_dials_self``), and ``submodule.<name>.url`` (``submodule update``).
    """
    if low_key.startswith("remote.") and (low_key.endswith(".url") or low_key.endswith(".pushurl")):
        return True
    if low_key.startswith("submodule.") and low_key.endswith(".url"):
        return True
    return low_key.startswith("url.") and (
        low_key.endswith(".insteadof") or low_key.endswith(".pushinsteadof")
    )


def _tar_command_targets_self(tokens: "list[str]", env: "dict[str, list[str]]") -> bool:
    """True if a ``tar`` command's effective argv rsh-dials THIS host.

    The effective argv is a ``TAR_OPTIONS`` value's words followed by *tokens*,
    for every value kept under the name.  Each value is read once per read
    (``_tar_words_target_self``, kept in ``_READ_MEMO``), and *tokens* once for
    each state a value can leave the option reading in (past a ``--`` or not),
    so many values, or one value exported to many tar commands, are not
    re-read with the whole argv.
    """
    memo = _READ_MEMO.get()
    states: "set[bool]" = set()
    # Whether the ``TAR_OPTIONS`` value tar reads names an operation, over every kept value.
    defaults: "set[bool]" = set()
    tar_options_values: "list[str | None]" = [*env.get("tar_options", [])]
    for tar_options in tar_options_values or [None]:
        key = ("tar-options", tar_options, _REENTRY_CHAIN.get())
        kept = memo.get(key) if memo is not None else None
        if kept is None:
            try:
                default_tokens = (
                    shlex.split(_unmask_separators(tar_options), comments=False, posix=True)
                    if tar_options is not None
                    else []
                )
            except ValueError:
                # shortcut: malformed quoting falls back to whitespace before the same classifier.
                default_tokens = _unmask_separators(tar_options or "").split()
            kept = (
                _tar_words_target_self(default_tokens, False, separators_end=False),
                _tar_operation_readings(default_tokens),
            )
            if memo is not None and not kept[0][0]:
                memo[key] = kept  # a refusal is read afresh, like ``_reenter_floors``'s
        (dials, options_ended), readings = kept
        if dials:
            return True
        defaults |= readings
        if options_ended is not None:
            states.add(options_ended)
    if any(
        _tar_words_target_self(tokens, options_ended, defaults=frozenset(defaults))[0]
        for options_ended in sorted(states)
    ):
        return True
    # The ``TAPE`` default archive is read whenever it is present: a self-shaped
    # TAPE is refused even next to an explicit ``-f`` archive, since the narrow
    # host:file predicate -- not tar's option grammar -- decides, and a non-self
    # TAPE (``/dev/st0``) is simply not refused.
    return any(_tar_archive_targets_self(_unmasked(archive)) for archive in env.get("tape", ()))


def _tar_words_target_self(
    words: "list[str]",
    options_ended: bool,
    *,
    separators_end: bool = True,
    defaults: "frozenset[bool]" = frozenset({False}),
) -> "tuple[bool, bool | None]":
    """``(dials, options_ended)`` for a run of tar argv *words*, read from *options_ended*.

    *dials* is True when a word carries a remote-shell command that dials this
    host or names a ``host:file`` archive on it.  Only the archive operand is a
    ``host:file`` candidate: a ``-f``/``--file`` value, or an old-style bundle's
    ``f`` slot.  Any other operand is a member name tar never dials, except past
    a ``--`` that a ``TAR_OPTIONS`` value carried in, where every operand is read.
    The second item is whether the run ended past a ``--`` (the state the next
    words are read in), or None when a command separator ended the argv inside
    the run.  With *separators_end*
    False (the words of a ``TAR_OPTIONS`` value) every word is data: tar reads
    ``;``, ``&&`` or ``|`` there as an option word, so it never ends the run.
    *defaults* holds each answer to whether the ``TAR_OPTIONS`` value names an
    operation (``_tar_bundle_archive_slot``).
    """
    archive_candidates: "list[str]" = []
    ended: "bool | None" = None
    value_pending = False
    every_operand = options_ended
    bundle_archive_at: "int | None" = None
    # The slots the bundle's ``f`` archive can hold, one per reading: a word before
    # the first is a value in every reading; a word past it is also read as usual.
    bundle_slots: "frozenset[int]" = frozenset()
    first_bundle_slot = -1
    for index, tok in enumerate(words):
        if separators_end and _ends_argv(tok) and tok in (";", "&", "|", "\n", "&&", "||"):
            break
        # A remote-shell command may be a detached token or an attached long
        # option value.  Check both without deciding which option owns it.
        command_candidates = [tok]
        if "=" in tok:
            command_candidates.append(tok.split("=", 1)[1])
        # ``--checkpoint-action=exec=<cmd>`` keeps tar's ``exec=`` prefix on the
        # attached value; peel it so it reads like the detached spelling.
        if command_candidates[-1].startswith("exec="):
            command_candidates.append(command_candidates[-1][len("exec=") :])
        if any(_command_value_dials_self(_unmasked(candidate)) for candidate in command_candidates):
            return True, None
        # An old-style bundle's ``f`` archive sits at a fixed slot: the words
        # before it are option values and the members after it are filenames, so
        # only the one archive word is a candidate.
        if bundle_archive_at is not None:
            if bundle_archive_at == 0:
                archive_candidates.append(tok)
                bundle_archive_at = None
            else:
                bundle_archive_at -= 1
            continue
        if bundle_slots and index:
            if index - 1 in bundle_slots:
                archive_candidates.append(tok)
            if index - 1 <= first_bundle_slot:
                continue
        if index == 0 and separators_end and not tok.startswith("-"):
            bundle_slots = _tar_bundle_archive_slot(tok, words[1:], defaults)
            first_bundle_slot = min(bundle_slots, default=-1)
            continue
        # The detached value of a ``_TAR_VALUE_OPTS`` option is no archive.  A
        # word that looks like an option is still read as one: after a folded
        # valueless twin (``-c --file=host:x``) it is the next option.  A ``--``
        # there is read as the value, so a ``-f`` after it stays read.
        if value_pending and (tok == "--" or not tok.startswith("-")):
            value_pending = False
            continue
        value_pending = False
        low = tok.lower()
        if not options_ended and tok == "--":
            options_ended = True
            continue
        if not options_ended and tok.startswith("-"):
            value_pending = _tar_option_takes_next_word(tok)
        if not options_ended and tok.startswith("--"):
            if low.startswith("--file="):
                archive_candidates.append(tok.split("=", 1)[1])
            elif "=" not in tok and _push_option_matches(tok, _TAR_READ_VALUE_OPTS):
                bundle_archive_at = 0
            continue
        if not options_ended and tok.startswith("-"):
            letters = tok[1:]
            # The only tar short options whose value is a program the shell runs
            # are ``-I`` (``--use-compress-program``) and ``-F``
            # (``--info-script``/``--new-volume-script``); case-folded they are
            # ``i`` and ``f``.  Their value may be GLUED to the letter
            # (``-I'ssh ...'``, ``-cMF'ssh ...'``), so route that glued value to
            # the same command check the separated (``-I 'ssh ...'``) and
            # ``--use-compress-program='ssh ...'`` spellings already get.  ``f``
            # takes the rest of the cluster as its value, so a letter after the
            # first ``f`` is inside that value, not a separate option
            # (``-cfI'ssh ...'`` names the archive ``Issh ...``, no exec).
            first_f = letters.find("f")
            exec_scan = letters if first_f == -1 else letters[: first_f + 1]
            for pos, letter in enumerate(exec_scan):
                if letter not in "if":
                    continue
                value = letters[pos + 1 :]
                value = value[1:] if value.startswith("=") else value
                if value and _command_value_dials_self(_unmasked(value)):
                    return True, None
            if "f" in letters:
                # A glued ``-f<archive>`` names the archive in this same word.
                after = letters.split("f", 1)[1]
                if after:
                    archive_candidates.append(after[1:] if after.startswith("=") else after)
                else:
                    bundle_archive_at = 0
            continue
        # Any other operand is a member name, read only in the carried-``--`` case.
        if every_operand:
            archive_candidates.append(tok)
    else:
        ended = options_ended
    dials = any(_tar_archive_targets_self(_unmasked(archive)) for archive in archive_candidates)
    return dials, ended


def _is_git_tar_to_self(text_lower: str, *, raw_text: "str | None" = None) -> bool:
    """True if *text_lower* starts a git/tar network transport to THIS host.

    The ssh pre-walk and payload reader expose hidden transports and inherited environments.
    FAIL-CLOSED: an unparseable transport is refused. If payload discovery raises,
    analysis degrades to the top-level frame; a carried command that names a literal
    self host is still re-entered (``_command_value_dials_self``) and refused when
    a floor refuses it or cannot read it.  The outermost read opens the memo every
    read nested in it shares (``_READ_MEMO``) and keeps *raw_text*, the command as
    submitted (*text_lower* when not given), for them all (``_RAW_COMMAND``).
    """
    if _READ_MEMO.get() is not None:
        return _git_tar_read(text_lower)
    memo = _READ_MEMO.set({})
    raw = _RAW_COMMAND.set(text_lower if raw_text is None else raw_text)
    try:
        return _git_tar_read(text_lower, typed=True)
    finally:
        _RAW_COMMAND.reset(raw)
        _READ_MEMO.reset(memo)


def _git_tar_read(text_lower: str, *, typed: bool = False) -> bool:
    """``_is_git_tar_to_self``'s read of *text_lower*, inside an open ``_READ_MEMO``.

    *typed* is True for the outermost read only, whose text is the command as typed.
    """
    from .argv_rules import _split_commands
    from .shell_commands import _double_quoted_substitution_bodies, read_commands
    from .shell_normalizer_walk import _program_index

    raw = _RAW_COMMAND.get()
    unmasked = _ssh_floor_unmasked_text(text_lower)
    text_lower = _mask_quoted_separators(unmasked)
    # A quick gate that is a SUPERSET of every spelling the walk below can read
    # as git/tar: a literal ``git``/``tar`` once quote and backslash splices are
    # removed (``gi''t``, ``"git"``, ``g\it``), or any expansion machinery that
    # lets the shell build the verb from other text -- globs and braces
    # (``g*t``, ``/usr/bin/g?t``), ``$`` and backticks.  A quote alone names no
    # verb, so prose with an apostrophe stops here.  The character classes are
    # the self floor's own (``argv_floor_self``), so nothing new is matched here.
    unquoted = _SELF_FLOOR_QUOTE_JUNK_RE.sub("", _shell_join_continuations(text_lower))
    if (
        "git" not in unquoted
        and "tar" not in unquoted
        and not _SELF_FLOOR_MACHINERY_RE.search(text_lower)
    ):
        return False
    # The mask above keeps double-quoted text as data, but bash runs a ``$(...)``
    # there, so each such body is judged as a command line of its own, as the
    # ssh floor judges it.
    if any(map(_is_git_tar_to_self, _double_quoted_substitution_bodies(unmasked))):
        return True
    # A shell function is not followed into its body: call arguments are not
    # bound, as on main.  A literal self dial in a body is read like a plain command.

    def _payload_key(source: str) -> "tuple[tuple[str, ...], ...]":
        return tuple(tuple(command) for command in _split_commands(_frame_commands(source)[0]))

    def _carries_self_dial_signal(expansions: bool = True) -> bool:
        """True if the whole input carries something that could dial THIS host.

        It tells ordinary prose from a real potential self-dial for the two
        callers that fail closed on an input they cannot fully read: the capped
        main loop and the open-quote backstop below.  The signal is any of:
        (1) a literal self host the floor already knows -- loopback, this host's
        own cached names and the self addresses, and the ``$(hostname`` /
        `` `hostname `` / ``$hostname`` self-hostname forms
        (``_value_names_literal_self``); (2) an ``ext::`` / ``ssh://`` /
        ``core.sshCommand`` / ``GIT_SSH`` override shape that can redirect the
        transport to this host; (3) a ``$`` expansion the shell could build a
        host from at run time, read from the command as submitted
        (``_RAW_COMMAND``), since the normalizer can drop the ``$`` marker.

        The open-quote backstop passes *expansions* False.  An unclosed quote
        cannot make a self host appear that the text does not already carry, and
        kinds 1 and 2 already read every self host and override the floor can
        recognise (the ``$(hostname`` / `` `hostname `` self forms among them),
        so a bare ``$`` or a lone backtick that names no recognisable self host
        is ordinary prose (a markdown ``git status`` example, ``$HOME``), not a
        self-dial.  Dropping kind 3 there keeps the real self-dials refused and
        leaves the prose alone.
        """
        if _value_names_literal_self(text_lower) is not False:
            return True  # kind 1: a literal self host (incl. $(hostname)/`hostname`)
        # kind 2: an override / transport shape that can redirect to self.
        unquoted = _SELF_FLOOR_QUOTE_JUNK_RE.sub("", _shell_join_continuations(text_lower))
        if "ext::" in unquoted or _GIT_SSHCOMMAND_CONFIG_PREFIX in unquoted or "ssh://" in unquoted:
            return True
        if any(alias in unquoted for alias in _GIT_SSH_SCHEME_ALIASES):
            return True
        if any(name in unquoted for name in _GIT_SSH_ENV_NAMES):
            return True
        # kind 3: a ``$`` expansion the shell could build a host from, read as
        # submitted.  The open-quote backstop passes expansions False, so a bare
        # ``$`` beside an unclosed quote (``$HOME`` in prose) is not a self-dial.
        if not expansions:
            return False
        raw = _RAW_COMMAND.get()
        submitted = text_lower if raw is None else raw
        return "$" in text_lower or any(
            mark == "$" and (after in "({_" or after.isalpha())
            for mark, after in zip(submitted, submitted[1:])
        )

    def _view_commands(source: str) -> "tuple[list[list[str]], list[str]]":
        """The commands of one frame, and each view whose open quote nothing closes."""
        # Redirection-free argv comes from the shared quote-aware shell reader,
        # so only live redirects and their detached targets disappear. Scan it
        # before the original frame; the latter preserves every existing
        # expansion, separator and nested-payload reading.
        redirect_free_sources = [
            " ".join(source[word.start : word.end] for word in shell_command.argv)
            for shell_command in read_commands(source, nested=False)
            if shell_command.redirects
            and shell_command.argv
            and shell_command.argv[-1].start > shell_command.redirects[0][0].end
        ]
        commands: "list[list[str]]" = []
        unclosed: "list[str]" = []
        for view in (*redirect_free_sources, source):
            tokens, left_open = _frame_commands(view)
            commands.extend(_split_commands(tokens))
            if not left_open:
                continue
            # The frame ended inside a quote, so every separator after that
            # quote was read as data.  Read the commands the shell reader finds
            # (``_shell_read_views``) as well.
            shell_views = _shell_read_views(view)
            if shell_views is None:
                unclosed.append(view)
                continue
            for shell_view in shell_views:
                commands.extend(_split_commands(_frame_commands(shell_view)[0]))
        return commands, unclosed

    def _words(raw_command: "list[str]") -> "list[str]":
        return [
            t.strip() if _env_assignment(t.strip()) is not None else _dequoted(t.strip())
            for t in raw_command
        ]

    # A payload frame runs with the environment of the parent command that
    # launched it.  Keying by the payload's argv lets duplicate payload text
    # retain every launch environment without letting a later command rewrite
    # an earlier launch.
    inherited_by_payload: "dict[tuple[tuple[str, ...], ...], list[dict[str, list[str]]]]" = {}
    try:
        payload_frames = _shell_payload_walk(text_lower)
    except Exception:
        payload_frames = [(text_lower, [])]
    # A wrapper's option words are read in the case they were written in
    # (``sudo -H`` takes no value, ``sudo -h`` does; ``env -C`` takes one), as
    # the ssh floor reads them: the typed line goes through the same walk, and a
    # frame or command that does not line up word for word with the lowered one
    # keeps the lowered reading, as does every read reached by recursion.
    cased_frames: "list[str]" = []
    if typed and raw is not None and raw == raw.lower():
        cased_frames = [s for s, _ in payload_frames]
    elif typed and raw is not None:
        try:
            cased_frames = [s for s, _ in _shell_payload_walk(_ssh_floor_source_text(raw))]
        except Exception:
            pass  # the lowered reading only
    signals: "list[bool]" = []

    def _signal() -> bool:
        """``_carries_self_dial_signal()`` (kinds 1-3), read once per input."""
        if not signals:
            signals.append(_carries_self_dial_signal())
        return signals[0]

    for frame_index, (source, _tokens) in enumerate(payload_frames):
        if frame_index and _frame_cannot_run_git_tar(source):
            continue  # a payload frame with no git/tar word dials nothing
        # The walk also returns the rest of the line after each escaped backtick
        # (``--body "run \`git log\`"``), so reading each costs the square of the
        # body.  Such a frame runs only under a shell that unescapes it (``sh -c``);
        # with no self-dial signal anywhere in the input it can reach this host only
        # through a name DNS maps here, which is not read (shortcut).
        if frame_index and text_lower.endswith(source):
            before = text_lower[: len(text_lower) - len(source)]
            if _ESCAPED_TICK_END_RE.search(before) and not _signal():
                continue
        inherited_envs = (
            [{}] if frame_index == 0 else inherited_by_payload.get(_payload_key(source), [{}])
        )
        frame_env = _FrameEnvironment(_env_assignment, _is_env_name)
        raw_commands, unclosed = _view_commands(source)
        # When the shell reader cannot close a quote either, the command as
        # typed is refused (fail closed) if it could run git or tar
        # (``_open_frame_could_run_git_tar``).  A payload frame or a carried
        # command is rebuilt from its parent's words, so its quotes are not
        # always the shell's; the read of its carrier decides it.
        if (
            typed
            and frame_index == 0
            and any(map(_open_frame_could_run_git_tar, unclosed))
            and _carries_self_dial_signal(expansions=False)
        ):
            return True
        cased_commands: "list[list[str]]" = []
        if len(cased_frames) == len(payload_frames):
            cased_source = cased_frames[frame_index]
            cased_commands = (
                raw_commands if cased_source == source else _view_commands(cased_source)[0]
            )
        # ``echo git clone localhost:x | sh`` runs what ``echo`` prints, but the
        # pipe is in the next command: judge the data-reader exemption on the
        # whole frame's words, as the ssh floor does.
        frame_disqualified = _shell_normalizer._data_consumer_command_disqualified(
            _frame_commands(source)[0]
        )
        for index, raw_command in enumerate(raw_commands):
            if index:
                # ``_split_commands`` drops the separator ``_FrameEnvironment``
                # clears a command's pending assignments on, so feed it back.
                frame_env.observe(";", ";")
            # A separator QUOTED at this frame's level stays a sentinel (data),
            # so the walks below do not stop at it; only the value actually
            # inspected is unmasked (``_unmasked``), as the ssh floor unmasks
            # only the operand it checks.
            command = _words(raw_command)
            options = None
            if len(cased_commands) == len(raw_commands):
                cased = _words(cased_commands[index])
                if len(cased) == len(command):
                    options = [c if c.lower() == w else w for c, w in zip(cased, command)]
            found = _program_index(command, options)
            if found is None:
                _observe_words(frame_env, command)
                continue
            verb, args = _walk._program_basename(command[found]), command[found + 1 :]
            prefix = command[: len(command) - len(args) - 1]
            _observe_words(frame_env, prefix)
            # Every value kept under a name is carried, inherited ones included:
            # which case the shell honors is not known (``_FrameEnvironment``).
            envs: "list[dict[str, list[str]]]" = []
            for inherited in inherited_envs:
                env = {name: list(values) for name, values in inherited.items()}
                for layer in (frame_env.exported, frame_env.pending):
                    for name, values in layer.items():
                        env.setdefault(name, []).extend(values)
                envs.append(env)
            try:
                nested_payloads = _nested_shell_payloads(command)
            except Exception:
                nested_payloads = []
            for payload in nested_payloads:
                key = _payload_key(payload)
                if key:
                    inherited_by_payload.setdefault(key, []).extend(envs)
            _observe_words(frame_env, command[len(prefix) :], prog=True)
            # ``script -c`` runs its value with this command's environment, so the
            # value is read led by it, as a re-entered command is.  An unknown launcher
            # (``unbuffer``) runs a later git/tar word, so each is a verb too, as in the
            # ssh floor, unless a data consumer has it (``echo git ...``); ``parallel``
            # appends each ``:::`` argument to it, so only the separator words drop, and
            # with a replacement string (``{}``) it is read once per argument put in.
            if verb == "script" and any(
                _git_tar_read(_exported_environment(env) + payload)
                for payload in _script_commands(args)
                for env in envs
            ):
                return True
            programs = _shell_normalizer._argv_programs(command)
            disqualified = frame_disqualified or (
                _shell_normalizer._data_consumer_command_disqualified(command)
            )
            starts = [found] + [
                j
                for j in range(found + 1, len(command))
                if any(_git_tar_verbs(command[j]))
                and not _shell_normalizer._data_consumer_exempt(
                    j, command[j], programs, command, command_disqualified=disqualified
                )
            ]
            # Each later start is read to the command's end, so ``git git ... status``
            # costs the square of its length.  Past the word cap a later start is read
            # only up to the next one, which can drop an operand (``--origin git``), so
            # it is refused unread when the input carries a self-dial signal.  Prose
            # (``--body "run \`git log\`"``) carries none; a backtick run is not one,
            # since the full read does not expand a substitution either.
            capped = sum(len(command) - s for s in starts[1:]) * len(envs) > _GIT_TAR_WORD_CAP
            if capped and _signal():
                return True
            ends = dict(zip(starts, starts[1:] + [len(command)])) if capped else {}
            memo, glob_key = _READ_MEMO.get(), ("glob-words", source)  # read once per frame
            if memo is not None and glob_key not in memo:
                memo[glob_key] = _glob_word_spellings(source)
            glob_frame = _GLOB_WORDS.set(memo[glob_key] if memo else _glob_word_spellings(source))
            try:
                # The first ``parallel`` word at or after ``found``, found once so the
                # per-start guard below is O(1): a later ``start`` is in the parallel
                # branch exactly when such a word sits before it.
                first_parallel = next(
                    (i for i in range(found, len(command)) if _is_parallel(command[i])), None
                )
                for start, env in ((start, env) for start in starts for env in envs):
                    git, tar = _git_tar_verbs(command[start])
                    tail = command[start + 1 : ends.get(start) if start > found else None]
                    rest = [w for w in tail if start == found or w not in _SEPARATORS]
                    readings = [rest]
                    # Only ``parallel`` puts inputs in place of ``{}``.  ``--rpl`` /
                    # ``--plus``, an unreadable option value, or a replacement string
                    # in or before the verb (which it can rewrite) refuse outright;
                    # a brace string (``{=``) refuses only where the verb can dial.
                    if first_parallel is not None and first_parallel < start:
                        extras, unsafe, braces = _parallel_replacements(
                            command[found + 1 : start], tail
                        )
                        lead = command[start : start + 1]
                        if git:
                            verb_at = _git_verb_at(tail)[0]
                            lead = _git_lead_without_paths(
                                command[start : start + 2 + verb_at],
                                options[start:] if options else None,
                            )
                        # One ``:::`` list of literal inputs is read by the runs below;
                        # from stdin, a ``::::`` file or linked lists the verb is unknown.
                        seps = [w for w in tail if w in _SEPARATORS]
                        hidden = seps != [":::"]
                        if unsafe or (hidden and _rewrites_any(lead, extras)):
                            return True
                        if braces and (tar or _git_may_dial(tail)):
                            return True
                        runs = _parallel_runs(tail, extras)
                        if runs is None:
                            return True
                        readings += runs
                    for rest in readings:
                        if git and _git_command_targets_self(rest, env):
                            return True
                        if tar and _tar_command_targets_self(rest, env):
                            return True
            finally:
                _GLOB_WORDS.reset(glob_frame)
    return False


def _parallel_replacements(opts: "list[str]", argv: "list[str]") -> "tuple[list[str], bool, bool]":
    """The custom replacement strings ``parallel``'s options in *opts* set (the words
    between ``parallel`` and the launched verb), whether any option is unsafe, and
    whether *argv* holds a perl/plus string (``{=``, ``{+``, ``{..}``).  Unsafe is a
    missing, empty, or ``$``/backtick/glob value (not a literal word), or ``--rpl`` /
    ``--plus`` (and the ``--rp`` / ``--plu`` abbreviations parallel accepts), whose
    tags can be any word.  The option name is read case-insensitively (the floor
    lowers its input); ``--replace``/``-i`` with no value keep ``{}``."""
    extras: "list[str]" = []
    unsafe = any(o.lower().split("=", 1)[0] in ("--rp", "--rpl", "--plu", "--plus") for o in opts)
    braces = any(s in _unmasked(w) for w in argv for s in ("{=", "{+", "{..}", "{...}"))
    i = 0
    while i < len(opts):
        tok = opts[i]
        low = tok.lower()
        value: "str | None" = None
        optional = low in ("--replace", "-i")  # these keep ``{}`` with no value
        if low in _PARALLEL_REPLACE_LONG or low == "-i":
            if i + 1 < len(opts):
                value = opts[i + 1]
                i += 1
            elif not optional:
                unsafe = True
        elif low.startswith("--") and low.split("=", 1)[0] in _PARALLEL_REPLACE_LONG:
            value = tok.split("=", 1)[1]
        elif low.startswith("-i"):  # glued ``-I@@`` / ``-iXX``
            value = tok[2:]
        else:
            i += 1
            continue
        if value is not None:
            v = _unmasked(value)
            if not v or any(c in v for c in _PARALLEL_REPLACE_UNSAFE):
                unsafe = True
            else:
                extras.append(v)
        i += 1
    return extras, unsafe, braces


def _git_lead_without_paths(words: "list[str]", cased: "list[str] | None") -> "list[str]":
    """*words* (``git`` up to its verb) without the value of ``-C``, ``--git-dir`` or
    ``--work-tree``: ``parallel`` shell-quotes each input, so a replacement string
    there names a directory, never a verb or config.  ``-C`` is told from ``-c`` by
    its typed case (*cased*, aligned with *words*); without it, ``-c`` keeps its
    value (fail closed)."""
    kept: "list[str]" = []
    i = 0
    while i < len(words):
        typed = cased[i] if cased and i < len(cased) else words[i]
        if typed.startswith(("--git-dir=", "--work-tree=")):
            kept.append(typed.split("=", 1)[0])
        else:
            kept.append(words[i])
        i += 2 if typed in ("-C", "--git-dir", "--work-tree") and i + 2 < len(words) else 1
    return kept


def _rewrites_any(words: "list[str]", extras: "list[str]") -> bool:
    """Whether a replacement string (``{}``... or one of *extras*) is in *words*."""
    reps = [e.lower() for e in extras]
    texts = [_unmasked(w).lower() for w in words]
    return any(_PLACEHOLDER_RE.search(t) or any(e in t for e in reps) for t in texts)


def _parallel_runs(
    words: "list[str]", extras: "list[str] | None" = None
) -> "list[list[str]] | None":
    """The argv ``parallel`` runs for each input after the first separator in *words*,
    each replacement string (``{}`` and the *extras* from ``-I``/``--replace``...) put
    in by that input.  Case is folded so a direct caller's mixed case reads like the
    floor's lowered input.  None (refuse unread) past ``_GIT_TAR_WORD_CAP`` words."""
    reps = [e.lower() for e in (extras or [])]
    words = [w.lower() for w in words]
    cut = next((i for i, w in enumerate(words) if w in _SEPARATORS), len(words))
    head, inputs = words[:cut], dict.fromkeys(w for w in words[cut:] if w not in _SEPARATORS)
    if not any(_PLACEHOLDER_RE.search(w) or any(e in w for e in reps) for w in head):
        return []
    if len(head) * len(inputs) > _GIT_TAR_WORD_CAP:
        return None

    def _sub(word: str, arg: str) -> str:
        word = _PLACEHOLDER_RE.sub(lambda _: arg, word)
        for extra in reps:
            word = word.replace(extra, arg)
        return word

    return [[_sub(w, arg) for w in head] for arg in inputs]


#: Text that ends in an escaped backtick: an odd run of backslashes, then ```.
_ESCAPED_TICK_END_RE = re.compile(r"(?<!\\)(?:\\\\)*\\`\Z")
#: A backslash escape; an escaped backtick (``\```) is text, not a substitution.
_FRAME_ESCAPE_RE = re.compile(r"(\\[^`])|\\`", re.DOTALL)
#: What can build a verb from other text: ``$``, a live backtick, a brace, an escape code.
_FRAME_EXPANSION_RE = re.compile(r"[$`{}]|\\x[0-9a-f]|\\0?[0-7]{1,3}")
#: A word, cut at anything not a path or glob character, that could name git/tar.
_FRAME_VERB_WORD_RE = re.compile(r"[a-z0-9_./*?\[\]+-]*(?:git|tar|[*?\[])[a-z0-9_./*?\[\]+-]*")


def _frame_cannot_run_git_tar(source: str) -> bool:
    """True if payload frame *source* holds no word that can run git or tar.

    The payload walk returns a frame after every escaped backtick of a quoted body
    (``--body "use \\`x\\`"``), so reading each one costs the square of the body.
    Fail closed: any expansion keeps the frame, and a word is cut finer than the
    shell cuts it, quote and backslash splices removed (``gi''t``, ``g\\it``)."""
    text = _shell_join_continuations(source)
    text = _FRAME_ESCAPE_RE.sub(r"\1", text)
    if _FRAME_EXPANSION_RE.search(text):
        return False
    words = _FRAME_VERB_WORD_RE.findall(_SELF_FLOOR_QUOTE_JUNK_RE.sub("", text))
    return not any(any(_git_tar_verbs(word)) for word in words)


def _git_tar_verbs(word: str) -> "tuple[bool, bool]":
    """Whether program *word* runs ``git`` and ``tar`` (``gtar`` is GNU tar); a glob
    (``g*t``) is read as every verb it CAN name, as the self floor reads ``pkill``."""
    verb = _unmasked(_walk._program_basename(word)).removesuffix(".exe")
    return (
        verb == "git" or _glob_could_expand_to(verb, ("git",)),
        verb in ("tar", "gtar") or _glob_could_expand_to(verb, ("tar", "gtar")),
    )


def _is_parallel(word: str) -> bool:
    """Whether program *word* runs GNU ``parallel`` (a glob is read as every name it CAN be)."""
    verb = _unmasked(_walk._program_basename(word))
    return verb == "parallel" or _glob_could_expand_to(verb, ("parallel",))


def _git_may_dial(args: "list[str]") -> bool:
    """Whether git with *args* can open a transport or record a remote URL
    (``_git_transport_modes``); a verb that is itself a placeholder is not local."""
    i, verb_word, _ = _git_verb_at(args)
    return i < len(args) and any(_git_transport_modes(_unmasked(verb_word).lower(), args[i + 1 :]))


def _script_commands(args: "list[str]") -> "list[str]":
    """The ``-c`` / ``--command`` values of a ``script`` argv: command lines it runs."""
    values = [_unmasked(a.partition("=")[2]) for a in args if a.startswith("--command=")]
    for flag, value in zip(args, args[1:]):
        if flag == "--command" or (flag[:1] == "-" and flag[1:2] != "-" and flag.endswith("c")):
            values.append(_unmasked(value))
    return values
