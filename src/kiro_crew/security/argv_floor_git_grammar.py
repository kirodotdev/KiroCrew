"""The git grammar the git/tar self-target floor reads a ``git`` argv with.

Which subcommands never open a transport, and the second-level reading of
``archive``, ``submodule`` and ``remote`` that decides it for those three; where
the subcommand sits behind git's global options and redirections, with the
``-c`` / ``--config-env`` overrides found on the way; which words after it are
transport operands or program overrides; the command an ``ext::`` remote runs;
the remote test (a URL or scp-like ``host:path`` whose host is a literal self
address); and the config entries one invocation carries (``-c``,
``--config-env``, ``GIT_CONFIG_PARAMETERS``), the alias definitions git can run
from them, and the keys whose value is a transport URL.
``argv_floor_git_tar`` judges what a git command read with these dials and owns
everything else.

A remote host is read LITERALLY: only a loopback name or address or one of this
host's own cached names and addresses is self, with no DNS or hosts-file alias
lookup, so a first-seen dotted remote is never pending-refused (the
``argv_floor_git_tar`` docstring gives the reason and the residual).

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch
through it reaches this module (:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import shlex
from typing import Any

from .argv_floor_git_tar_parallel import _unmasked
from .argv_floor_ssh import (
    _decode_scp_host,
    _decode_url_authority,
    _git_config_override,
    _operand_targets_self,
    _unmask_separators,
)
from .argv_floor_ssh_operand import _GIT_WORD
from .argv_floor_tar import _READ_MEMO, _glob_host_is_self
from .shell_normalizer import _ends_argv, _strip_redirect

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


def _git_remote_targets_self(operand: str) -> bool:
    """True if a git remote *operand* dials THIS host (``_git_remote_word_dials_self``).

    The word is read as a git word (``_GIT_WORD``): a Windows own-name counts
    only in a URL authority or an scp-like ``user@host:path``, since a git word
    may be a refspec or a remote name.
    """
    git_word = _GIT_WORD.set(True)
    try:
        return _git_remote_word_dials_self(operand)
    finally:
        _GIT_WORD.reset(git_word)


def _git_remote_word_dials_self(operand: str) -> bool:
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
    return _operand_targets_self(
        t, host_position=True, dns=False, match_globs=False, file_operand=True
    )


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


def _git_may_dial(args: "list[str]") -> bool:
    """Whether git with *args* can open a transport or record a remote URL
    (``_git_transport_modes``); a verb that is itself a placeholder is not local."""
    i, verb_word, _ = _git_verb_at(args)
    return i < len(args) and any(_git_transport_modes(_unmasked(verb_word).lower(), args[i + 1 :]))
