"""Shell text reader: command bodies carried inside ssh-family options.

An ssh-family program runs several of its argument values as LOCAL shell commands
on this host, not as host operands:

* ``ssh``/``scp``/``sftp`` run the value of any ``*Command`` option
  (``ProxyCommand``, ``LocalCommand``, ``KnownHostsCommand``) through
  ``$SHELL -c`` -- ProxyCommand and KnownHostsCommand even with no connection.
* ``rsync`` runs the value of ``-e``/``--rsh`` (and a leading ``RSYNC_RSH=`` /
  ``RSYNC_CONNECT_PROG=`` environment assignment) as its remote-shell command.
* ``git`` runs a leading ``GIT_SSH_COMMAND=`` assignment as the transport it
  shells out to.

Each of those values is a command line this host executes, exactly as a
``bash -c`` body is, so this reader hands each one to the nested-command walk
that already judges a ``-c`` body. The ssh floor's own self-dial check
(``argv_floor_ssh``) is unaffected: it reads the same values for a different
question (does the value DIAL this host) and stays in place.

``RemoteCommand`` is the one ``*Command`` option excluded: its value runs on the
REMOTE host, not here, so feeding it to the local-command walk would refuse a
legitimate remote invocation.

The verb is read only in COMMAND position and from the caller's ``programs``
attribution (already peeled of ``(``, quotes and paths), so a word that merely NAMES
an ssh-family program as an argument -- ``grep -e rsync -e 'x'`` -- does not get
ssh/rsync option parsing, while ``(ssh …`` and ``ssh.exe …`` do. Options end where
the program stops reading them: at ``--``, and for ``ssh`` at its remote command
(the second operand; ssh keeps reading options after the destination), whose words
run on the far side. A leading ``VAR=`` assignment is a command only for the
statement that runs its consumer (``git`` / ``rsync``); one before ``true ; rsync``
ends with its own statement. A verb reached only through a
wrapper (``env -u rsync ssh …``, ``sudo ssh …``) is attributed to the wrapper, not
to ssh, so its option values are not read here; that is the same wrapper residual
the rest of the ssh floor carries, not a gap this reader introduces.

Data and a single pass only; no tier decides anything here. The module imports
nothing from the floors above it, so the reader stays below them.
"""

from __future__ import annotations

import re

from .shell_normalizer_walk import _ends_argv

#: Where one word ends and the next begins when scanning a token for a consumer
#: name. Whitespace AND the shell separators a glued token can carry
#: (``;`` ``&`` ``|`` newline, parens): ``sh -c 'cd r&&git fetch'`` reaches this as the
#: one token ``cd r&&git fetch``, and a plain ``str.split`` would leave ``r&&git`` and
#: hide the ``git`` consumer. Over-reading a word is the safe direction.
_WORD_SEPARATORS_RE = re.compile(r"[\s;&|()\n]+")

# ssh-family program basenames. A path- or ``.exe``-qualified spelling is reduced
# to the bare verb by ``_family_verb`` before this is applied.
_SSH_FAMILY_VERBS: frozenset[str] = frozenset({"ssh", "scp", "sftp", "rsync"})


# Verbs that accept ssh ``-o Key=value`` options whose value is a local command.
_DASH_O_VERBS: frozenset[str] = frozenset({"ssh", "scp", "sftp"})


# Leading environment assignments whose value is a command line run FROM HERE,
# mapped to the program that runs it.
_ENV_COMMAND_VARS: "dict[str, str]" = {
    "git_ssh_command": "git",
    "rsync_rsh": "rsync",
    "rsync_connect_prog": "rsync",
}


# Case-folded option letters that CONSUME the next token as their value, per verb
# (see ``argv_floor_ssh``, which reads the same table). A letter whose two cases
# disagree on valueness is IN the set, so the ssh walk here can only reach the
# remote command LATE, never early: over-reading is the safe direction.
_VERB_VALUE_TAKING_OPT_LETTERS: "dict[str, frozenset[str]]" = {
    "ssh": frozenset("bcdefijlmopqrsw"),
    "sftp": frozenset("bcdfijloprsx"),
}


def _word_program(word: str) -> str:
    """The lowercased basename *word* names, peeled of quotes, parens and ``.exe``."""
    base = word.strip("\"'(){}`$").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    return base[: -len(".exe")] if base.endswith(".exe") else base


def _family_verb(program: str) -> "str | None":
    """The ssh-family verb an attributed *program* name is, or None.

    Lowercased, so a case-preserved caller's ``SSH`` and ``ssh.exe`` both resolve.
    """
    base = _word_program(program.strip())
    return base if base in _SSH_FAMILY_VERBS else None


def _ssh_value_consumes_next(token: str) -> bool:
    """True if a short-option cluster ends in an ssh flag that takes the NEXT token.

    getopt reads valueless letters until the first value-taking one; the rest of
    the cluster is its value, or, when it is the last letter, the next token is.
    """
    body = token[1:]
    for index, ch in enumerate(body):
        if ch.lower() in _VERB_VALUE_TAKING_OPT_LETTERS["ssh"]:
            return index == len(body) - 1
    return False


def _is_local_command_key(key: str) -> bool:
    """True if ssh option *key* runs its value as a LOCAL command.

    Every current OpenSSH option whose value this host executes ends in
    ``Command`` (``ProxyCommand``, ``LocalCommand``, ``KnownHostsCommand``), so a
    suffix test future-proofs the set. ``RemoteCommand`` is the sole ``*Command``
    option run on the far side, so it is excluded -- feeding it here would refuse
    a legitimate remote command.
    """
    # OpenSSH ``strdelim`` drops double quotes anywhere in the keyword token
    # (``"ProxyCommand"``, ``Proxy"Command"``, ``-o"ProxyCommand"=v``), so they are
    # removed before matching -- a quoted key would otherwise never end in ``command``.
    k = key.strip().lower().replace('"', "")
    return k.endswith("command") and k != "remotecommand"


#: How far back from the end of a short-flag cluster's leading flag-letter region
#: the fold-ambiguous value letter is searched for. The deny tiers case-fold the
#: command before this reader runs, so a valueless letter and its value-taking
#: twin (scp ``-O``/``-o``, rsync ``-E``/``-e``) arrive as the SAME byte and which
#: one took the value is unrecoverable; every plausible split is yielded, exactly
#: as the ``-c`` glued carrier does (see security.md). The true split's distance
#: from the region's end is the glued value's length -- a keyword (``ProxyCommand=…``)
#: or a command -- while valueless-flag PADDING only adds fake splits farther from
#: the end, so bounding the scan to the LAST window of the region catches the true
#: split and drops the padding: linear, with no padding bypass.
_CLUSTER_SPLIT_WINDOW = 64


def _cluster_value_splits(token: str, letter: str) -> "tuple[list[str], bool]":
    """Every glued value a short-flag cluster plausibly carries for *letter*, and
    whether the value is instead the NEXT token.

    *token* is a single-dash cluster (``-Oo…``, ``-aEe…``) the deny tiers have already
    case-folded. getopt/popt step over valueless letters until the first value-taking
    one, whose glued remainder -- or, when it is the cluster's last letter, the next
    word -- is the value. Folding makes the valueless letter (scp ``-O``, rsync ``-E``)
    and the value-taking one (``-o``, ``-e``) the same byte, so EACH *letter* position in
    the leading flag-letter region is a plausible split and all are returned; over-reading
    a split only adds a payload, this floor's documented safe direction. The leading
    flag-letter region ends at the first non-alphanumeric character, which begins a glued
    value (the ``=`` in ``-e=cmd``, the keyword in ``-oKey=v``). Returns
    ``(glued_remainders, takes_next_token)``: ``takes_next_token`` is true when the cluster
    is all flag letters and ends in *letter*, so the value is the following word.
    """
    if not token.startswith("-") or token.startswith("--"):
        return [], False
    body = token[1:]
    # Step over leading flag letters that are NOT the value letter -- valueless, or a
    # value-taking twin we over-read past, the floor's safe direction -- then take the
    # maximal RUN of the value letter. A run is what the fold produces: a valueless
    # flag (scp ``-O``, rsync ``-E``) bundled before its value-taking twin (``-o``,
    # ``-e``) arrives as the same byte repeated, and any position in the run could be
    # the one that took the value, so each is a plausible split. The keyword/value
    # begins AFTER the run, so no split is generated inside it (``-oProxyCommand`` is
    # one ``-o``, not an ``o`` per keyword letter).
    index = 0
    while index < len(body) and body[index].isalnum() and body[index] != letter:
        index += 1
    run_start = index
    while index < len(body) and body[index] == letter:
        index += 1
    run_end = index
    if run_start == run_end:
        return [], False  # the value letter does not lead the flag region
    # The cluster is all flag letters ending in the value letter: its value is the
    # NEXT word. ``takes_next`` is a suffix test, so padding before the run never
    # hides it; only the glued splits need the window.
    takes_next = run_end == len(body)
    start = max(run_start, run_end - _CLUSTER_SPLIT_WINDOW)
    splits = [body[pos + 1 :] for pos in range(start, run_end) if body[pos + 1 :]]
    return splits, takes_next


def _split_key_value(spec: str) -> "tuple[str, str]":
    """Split an ssh option spec into ``(key, value)`` as OpenSSH's reader does.

    OpenSSH ends the keyword at the first whitespace OR ``=``, whichever comes
    first, then ``readconf``'s ``parse_command`` skips the WHOLE following run of
    whitespace and ``=`` before the value. So ``ProxyCommand =cmd``,
    ``ProxyCommand = cmd``, ``ProxyCommand==cmd`` and ``ProxyCommand=<TAB>=cmd`` all
    have the value ``cmd``, while an ``=`` INSIDE the value
    (``ProxyCommand env A=1 cmd``) is preserved. A separator run left on the value
    would make the deny tiers read ``=cmd`` instead of the ``cmd`` OpenSSH runs.
    """
    stripped = spec.strip()
    for index, ch in enumerate(stripped):
        if ch == "=" or ch.isspace():
            key = stripped[:index].strip()
            rest = stripped[index:].lstrip(" \t=")
            return key, rest
    return stripped, ""


def _consumers_ahead(tokens: "list[str]") -> "list[frozenset[str]]":
    """For each index, the env-command consumers (``git``/``rsync``) named as a word
    from that token to the end of its statement. One backward pass, so the leading-
    assignment check stays linear however many assignments a statement carries.

    Any WORD counts, not only the program: ``RSYNC_RSH=… command rsync`` and
    ``GIT_SSH_COMMAND=… sh -c 'git fetch'`` hand the variable to the consumer through
    a wrapper, and over-reading a mention is this reader's safe direction.
    """
    consumers = frozenset(_ENV_COMMAND_VARS.values())
    ahead: "list[frozenset[str]]" = [frozenset()] * (len(tokens) + 1)
    for index in range(len(tokens) - 1, -1, -1):
        token = tokens[index]
        words = map(_word_program, _WORD_SEPARATORS_RE.split(token))
        found = frozenset(w for w in words if w in consumers)
        ahead[index] = found if _ends_argv(token) else found | ahead[index + 1]
    return ahead


def ssh_command_option_payloads(tokens: "list[str]", programs: "list[str]") -> "list[str]":
    """Local-command bodies carried inside an ssh-family invocation in *tokens*.

    *programs* is the per-token command-position attribution (``_argv_programs``):
    an ssh-family verb is parsed only where it is the program the argv runs, so a
    decoy word naming an ssh-family program as an argument is inert. Returns every
    value this host would execute: ssh ``*Command`` option values
    (``ssh``/``scp``/``sftp``), the rsync ``-e``/``--rsh`` remote-shell value, and
    a leading ``GIT_SSH_COMMAND=`` / ``RSYNC_RSH=`` / ``RSYNC_CONNECT_PROG=``
    assignment value. Each is a command line the nested-command walk then
    re-tokenizes and judges, so an ``-e "ssh -oProxyCommand=…"`` value descends on
    the next pass. Over-approximate by design: a value naming a self-dial or a
    harmless command is emitted too, and the deny tiers decide.
    """
    payloads: list[str] = []
    limit = min(len(tokens), len(programs))
    # A command starts where attribution names a program right after a leading
    # assignment, a statement boundary (``_ends_argv``) or the start of the line.
    starts = [
        bool(programs[k]) and (k == 0 or not programs[k - 1] or _ends_argv(tokens[k - 1]))
        for k in range(limit)
    ]

    # A leading ``NAME=value`` assignment (empty attribution, or the program slot
    # itself when a subshell paren is glued on: ``(GIT_SSH_COMMAND=…``) whose
    # consumer runs in the SAME statement. ``RSYNC_RSH=… true ; rsync …`` hands the
    # variable to ``true`` only, so it is no command of rsync's.
    ahead: "list[frozenset[str]] | None" = None
    for index in range(limit):
        if programs[index] and not starts[index]:
            continue
        spec = tokens[index].lstrip("({")
        name, eq, value = spec.partition("=")
        consumer = _ENV_COMMAND_VARS.get(name.lower()) if eq else None
        if consumer is None:
            continue
        if ahead is None:
            ahead = _consumers_ahead(tokens)
        if consumer in ahead[index + 1]:
            payloads.append(value)

    # One forward pass. A matched command's argv span is scanned once and the outer
    # index jumps PAST it, so the walk is linear inside this synchronous gate.
    i = 0
    while i < limit:
        verb = _family_verb(programs[i]) if starts[i] else None
        if verb is None:
            i += 1
            continue
        j = i + 1
        reading_options = True
        operands = 0
        # The span is this command's own argv: it ends after the token that ends it.
        while j < limit and not _ends_argv(tokens[j - 1]):
            token = tokens[j]
            has_value_next = j + 1 < limit and not tokens[j + 1].startswith("-")
            if not reading_options:
                pass
            elif token == "--":
                # getopt stops at ``--``: what follows is an operand (for ssh the
                # destination, then the remote command), never an option.
                reading_options = False
            elif not token.startswith("-") or token == "-":
                # ssh re-reads options after its destination, but its SECOND operand
                # starts the remote command, whose words run on the far side.
                if verb == "ssh":
                    operands += 1
                    reading_options = operands < 2
            elif verb in _DASH_O_VERBS:
                # ``-oKey=value`` / ``-o'Key value'`` glued, and the folded
                # ``-Oo``/``-vo`` clusters that bundle valueless flags before the
                # ``-o``: every plausible split is read (see ``_cluster_value_splits``).
                splits, takes_next = _cluster_value_splits(token, "o")
                for glued in splits:
                    key, value = _split_key_value(glued)
                    if _is_local_command_key(key) and value:
                        payloads.append(value)
                # ``sftp -D <prog>`` connects to a LOCAL sftp-server command rather than
                # over ssh, running it on this host. ``-D`` folds to ``-d`` and, like
                # ``-o``, takes a value, so its glued remainder is read as a command line
                # and extended directly (the deny walk re-tokenizes it). Computed
                # independently of the ``-o`` scan because an ``o`` inside the value
                # (``-Dkirocrew token``) would otherwise make ``-o`` claim the token.
                d_splits, d_takes_next = (
                    _cluster_value_splits(token, "d") if verb == "sftp" else ([], False)
                )
                payloads.extend(d_splits)
                # ``-o Key=value`` / ``-o 'Key value'``. A next token that itself
                # starts with ``-`` is another option, not this one's argument --
                # consuming it would skip the real ``-o`` spec that follows (the
                # case-folded deny view turns scp's valueless ``-O`` into a second
                # ``-o``, so the spurious one must not eat the real spec).
                if takes_next and has_value_next:
                    key, value = _split_key_value(tokens[j + 1])
                    if _is_local_command_key(key) and value:
                        payloads.append(value)
                    j += 1
                elif d_takes_next and has_value_next:
                    payloads.append(tokens[j + 1])  # ``sftp -D <prog>`` next-word value
                    j += 1
                elif (
                    not splits
                    and not takes_next
                    and verb == "ssh"
                    and has_value_next
                    and _ssh_value_consumes_next(token)
                ):
                    j += 1  # ``-p 22``: the value is not the destination operand
            elif verb == "rsync":
                # Same guard: a next token starting with ``-`` is a flag, not the
                # remote-shell value, so a case-folded ``-E`` (``--executability``,
                # valueless) cannot make a trailing bare ``-e`` swallow the real
                # ``-e`` option that follows.
                if token in ("-e", "--rsh") and has_value_next:
                    payloads.append(tokens[j + 1])
                    j += 1
                elif token.lower().startswith("--rsh="):
                    payloads.append(token[len("--rsh=") :])
                else:
                    # ``-Ee``/``-ae``/``-e=cmd`` clusters: every plausible fold split.
                    splits, takes_next = _cluster_value_splits(token, "e")
                    for glued in splits:
                        # popt strips ONE leading ``=`` from a glued short-option
                        # value (``-e=cmd``, ``-ae=cmd``), as the ``--rsh=`` branch does.
                        payloads.append(glued[1:] if glued.startswith("=") else glued)
                    if takes_next and has_value_next:
                        payloads.append(tokens[j + 1])
                        j += 1
            j += 1
        i = j

    return [p for p in payloads if p.strip()]
