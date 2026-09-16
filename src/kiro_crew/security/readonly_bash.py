"""Read-only bash classifier: the last gate before a shell command auto-approves.

``is_read_only_bash`` / ``unsafe_bash_reason`` decide whether a shell command is
read-only, and therefore whether it may run with no human prompt under
``--approval reads`` / trust-reads and in ``hooks.on_tool_call``'s read-only
auto-approve branch. Deny-by-default: a command has to be RECOGNISED as read-only
by the tables here, and anything unrecognised prompts.

The rest of the module is the argument semantics that verdict rests on -- the
prefix allowlist, the per-verb write/exec/indirection flag tables, the git ref and
remote subcommand rules, the positive option accept-lists for tools with a small
enough surface (``sort``, ``date``, ``file``, ``hostname``), and the shell-expansion
readers that decide whether a token's real spelling is knowable before it runs.
Every table carries the measurement that put its entries there.

Self-contained on purpose: nothing here reads dashboard state, and the two
consumers (``dashboard.chat_runner`` for the approval flow, ``hooks`` for the
auto-approve branch) import it at module top.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from typing import NamedTuple

_READ_ONLY_BASH_PREFIXES: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "egrep",
    "fgrep",
    "wc",
    "which",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "diff",
    "pwd",
    "echo",
    "date",
    "whoami",
    "hostname",
    "uname",
    "readlink",
    "realpath",
    "basename",
    "dirname",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git branch",
    "git tag",
    "git remote",
    "git rev-parse",
    "git describe",
    "git ls-files",
    "git ls-tree",
    "git cat-file",
    "git blame",
    "brazil workspace show",
    "brazil workspace list",
    "brazil versionset print",
    "brazil versionset show",
    "brazil-path",
    "python --version",
    "python3 --version",
    "node --version",
    "java -version",
    "javac -version",
)

_READ_ONLY_PIPE_RE = re.compile(
    r"^\s*(grep|egrep|fgrep|head|tail|wc|sort|uniq|cut|less|more|cat)\b"
)

# Reject redirections and command substitutions, conservatively.
#
# `<` is matched only as `<(` here, NOT bare. Bare `<` and word-initial `#` are
# TOKEN ELISION rather than command execution, and they are handled per verb in
# `_side_effect_reason` instead: see `_ELISION_SENSITIVE` for why a global refusal
# was the wrong place for them.
_UNSAFE_SHELL_RE = re.compile(r">|`|\$\(|<\(|(?<!&)&(?!&)")

# Discard-only redirect idioms that are read-only despite containing '>'/'&':
# `2>/dev/null`, `>/dev/null`, `&>/dev/null`, `2>>/dev/null`, and `2>&1`.
# These sink or merge output, never writing a real file, so they must be
# stripped before _UNSAFE_SHELL_RE — otherwise every `find … 2>/dev/null`
# falls through to an interactive prompt. A redirect to any real path
# (e.g. `cmd > out.txt`) still trips _UNSAFE_SHELL_RE and stays unsafe.
# The `(?![\w./-])` guard pins the match to the literal device `/dev/null`:
# without it, `>/dev/nullx` or `>/dev/null/../etc/passwd` would be scrubbed as
# a sink, smuggling a real-file write past the unsafe-shell check.
_DEVNULL_REDIR_RE = re.compile(r"(?:\d*>>?|&>)\s*/dev/null(?![\w./-])|\d*>&\d+")

# ── Side effects reached through an allowlisted read-only verb ──
#
# The allowlist above names a verb and is matched as a prefix, so it vouches
# for every flag, subcommand and operand that verb accepts. Some of those
# write a file, change a ref or launch another program — and none of it goes
# through a shell redirect, so `_UNSAFE_SHELL_RE` never sees it.
#
# The tables are keyed by verb because the same spelling is harmless
# elsewhere: `ls -o` is a long listing format and `grep -o` prints only the
# match, while `sort -o FILE` truncates and writes FILE.

# Flags that make the program write a file named on its own command line.
_WRITE_FLAGS: dict[str, tuple[str, ...]] = {
    # `-R` writes without being handed a filename: tree re-runs itself in every
    # directory it descends into, adding `-o 00Tree.html` each time, so the file
    # is named by tree rather than by the command line. Same outcome as `-o`, one
    # step removed, which is why looking only for a filename-bearing flag missed
    # it.
    "tree": ("-o", "--output", "-R"),
    "uniq": ("-o",),
    "git diff": ("--output",),
    "git show": ("--output",),
    "git log": ("--output",),
    # A pager, and not on the prefix allowlist — but the PIPE-TARGET check runs
    # this table too, and `cat f | less -O FILE` writes FILE from a segment whose
    # leading verb is a read.
    "less": ("-o", "-O", "--log-file", "--LOG-FILE"),
}

# Flags that hand control to a program the repository names, not the caller:
# an external diff driver comes from the repo's config or .gitattributes.
_EXEC_FLAGS: dict[str, tuple[str, ...]] = {
    # `--textconv` is the same hand-off as `--ext-diff` through a different config
    # key, and it was missing here while `git cat-file` below already listed it —
    # the table gap, not the design, is what let `git diff --textconv` through.
    #
    # Scope, stated plainly: this stops the COMMAND LINE from selecting the
    # program. It does not stop a textconv driver the user configured from being
    # applied by default, because that name comes from git config, which is not
    # part of a repository and is not something a checkout can add. Requiring
    # `--no-textconv` would be the only way to cover that, and it would take plain
    # `git diff` off the read-only path — the most common read there is.
    "git diff": ("--ext-diff", "--textconv"),
    "git show": ("--ext-diff", "--textconv"),
    "git log": ("--ext-diff", "--textconv"),
    # `--filters` runs the repository's clean/smudge filter — a command from
    # `.gitattributes`, i.e. chosen by the checkout rather than by the caller.
    # `--textconv` is the same hand-off through a different config key.
    "git cat-file": ("--filters", "--textconv"),
    # A pager that runs a filter over its input, reachable as a pipe target.
    # `-k` is the sharper one and it is INDIRECT: it loads a lesskey file, and a
    # lesskey file can set environment variables — including `LESSOPEN`, which less
    # treats as an input PREPROCESSOR and runs. So a checkout-supplied lesskey is
    # arbitrary command execution, two steps removed from anything on the command
    # line. Verified against `less --help` on less 608: `-k [file]` /
    # `--lesskey-file=[file]`, plus `--lesskey-src` on newer builds.
    #
    # Case is load-bearing here, as it is for `file -C`: lowercase `-k` loads the
    # keyfile, while uppercase `-K` is `--quit-on-intr` and an ordinary read.
    "less": ("--filter", "-k", "--lesskey-file", "--lesskey-src"),
}

# Flags that name a file which in turn NAMES THE PATHS the program opens. This is
# an INDIRECTION, not a write, which is why a write-flag table could not hold it:
# the hook layer applies `is_sensitive_path` to the command text, so it sees the
# list file and nothing else while the program reads every path inside.
#
# Measured on coreutils, with a NUL-separated list containing `/etc/hostname`:
# `wc --files0-from=list0` and `du --files0-from=list0` both read `/etc/hostname`,
# a path that never appears in argv.
#
# Scoped to the heads that HAVE the flag and are not already covered, and
# deliberately spelled in full: `--file` is a prefix of `--files0-from`, so a
# shorter entry would be reached by the abbreviation walk in `_glob_reaches` and
# cost `grep --file=PATTERNS` and `stat --file` for no gain.
#
# `sort` HAS the flag (checked against `--help`) and is deliberately NOT here.
# It is already refused by `_OPTION_ACCEPT_LISTS`, which admits an option only if
# it is listed, and `--files0-from` is not in `_SORT_READONLY_LONG`. Listing it
# twice would change nothing but the reason string, while making a reader think
# this table is what closes it. The glob defence is unaffected: it derives
# `--files0-from` from the entries below, not from the key it appears under.
_INDIRECT_LIST_FLAGS: tuple[str, ...] = ("--files0-from",)

_INDIRECT_LIST_FLAGS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    prefix: _INDIRECT_LIST_FLAGS for prefix in ("wc", "du")
}

# Pagers take a `+` argument that is not an option but a string in the pager's OWN
# command language, and that language contains a shell escape. Measured under a
# real pty: `git log | less '+!touch FILE'` CREATED the file.
#
# Two things about that measurement decide the shape of this rule.
#
# It did NOT fire when stdout was a pipe -- less degrades to `cat` with no tty and
# never runs the startup command. So this is not unconditional code execution; it
# depends on whether whatever executes the command supplies a tty. The classifier
# cannot know that (the gate at `hooks.on_tool_call` hands the string to an agent
# runtime, it does not run it), so it must fail closed on the spelling.
#
# `more` did NOT fire on util-linux, which has no `+command`. It is listed anyway
# because on the BSDs `more` is not a separate program: FreeBSD's
# `usr.bin/less/Makefile` installs it as a link (`LINKS= ${BINDIR}/less
# ${BINDIR}/more`), and Apple's `less/main.c` detects the name at startup:
#
#     if (strcmp(last_component(progname), "more") == 0)
#             less_is_more = 1;
#
# `less_is_more` changes defaults, not the `+` startup-command path, so `more '+!cmd'`
# reaches the same shell escape there. This module ships to macOS, and the name a
# binary is invoked under does not tell the classifier which implementation answers,
# so both names are listed rather than branching on `sys.platform`.
#
# The whole `+` prefix is refused rather than the dangerous letters (`!` shell,
# `|` pipe-to-shell, `v` editor, `s` save-to-file), because enumerating them is the
# denylist this PR exists to argue against: the set is the pager's command
# language, and it grows without asking.
_PAGER_STARTUP_VERBS: frozenset[str] = frozenset(("less", "more"))

# Words bash DELETES before exec, which `shlex.split` keeps. The token list this
# module reads is therefore a strict SUPERSET of the real argv, and a phantom word
# can make a segment look like something it is not. Measured on a scratch repo,
# with an empty file named `--list` present for the redirect form:
#
#     git branch injected # --list     -> CREATED the branch
#     git tag forged # --list          -> CREATED the tag
#     git branch injected < --list     -> CREATED the branch
#     git branch injected <<< --list   -> CREATED the branch
#
# In each case `shlex` supplied a `--list` that put the segment in list mode, so
# the operand walk read `injected` as a pattern instead of a ref to create.
#
# WHY THIS IS PER VERB AND NOT A GLOBAL REFUSAL. A phantom word can only ADD to the
# token list. For a verb decided by its FLAGS, an added word is either inert or gets
# read as a flag it does not have, so the worst case is an extra prompt: safe. Only a
# verb decided by POSITION or MODE can be flipped, because there the count and the
# order of the words carry the decision. Refusing globally cost five ordinary reads
# that no phantom word could have made unsafe (`wc -l < f`, `grep TODO < f`,
# `cat < f`, `wc -l <f`, `head -20 < log.txt`), which is a worse trade than the
# narrower rule.
#
# WHY REFUSE RATHER THAN STRIP THE REDIRECT. Stripping the operator and its target
# looks equivalent and is not: `shlex` has already discarded the quoting, so a token
# beginning with `<` is indistinguishable from a QUOTED argument that begins with
# `<`. Stripping would drop a word bash keeps, and for exactly these verbs that
# opens a hole rather than closing one: `git branch '<new'` reaches `shlex` as
# `[git, branch, <new]`, and dropping `<new` leaves a bare `git branch` that reads
# as a listing while bash creates the ref. Refusing fails closed without
# reimplementing bash's quoting rules, which this module deliberately does not do.
_ELISION_SENSITIVE_RE = re.compile(r"(?:^|\s)#|<")

# `git branch` and `git tag` each carry a read mode and a write mode under one
# subcommand, so the prefix match admits the destructive spellings. Some of
# these also open `$EDITOR`, which runs a program of the environment's
# choosing: `git branch --edit-description` always does, and `git tag <name>`
# does whenever `tag.gpgSign` or `tag.forceSignAnnotated` is set.
_GIT_REF_WRITE_FLAGS: dict[str, tuple[str, ...]] = {
    "branch": (
        "-d",
        "-D",
        "--delete",
        "-m",
        "-M",
        "--move",
        "-c",
        "-C",
        "--copy",
        "-f",
        "--force",
        "-u",
        "--set-upstream",
        "--set-upstream-to",
        "--unset-upstream",
        "--edit-description",
    ),
    "tag": (
        "-d",
        "--delete",
        "-a",
        "--annotate",
        "-s",
        "--sign",
        "-m",
        "--message",
        "-F",
        "--file",
        "-f",
        "--force",
        "--cleanup",
        # `-u <keyid>` is `--local-user`: it makes the tag ANNOTATED and signed,
        # so it creates a ref exactly as `-a` does. `-u` was in the `branch` list
        # (set-upstream) and missing here, and the omission was reachable —
        # `git tag -ulin@kiro.co release` created a signed tag.
        "-u",
        "--local-user",
    ),
}

# Why a bare operand needs TWO tables rather than one.
#
# `git branch <name>` creates a ref, so a bare operand is the signal. Deciding
# when an operand is NOT that name was collapsed into a single "this flag eats
# the next token" set, and that conflated two different things git keeps apart:
#
#   * whether the flag CONSUMES the following word, which git's own option
#     parser decides by whether the argument is required or optional. A required
#     argument is taken from the next word; an OPTIONAL one must be attached with
#     `=`, and a separate word is left as an operand. `--color` is optional, so
#     `git branch --color newbranch` still creates `newbranch` — while the guard
#     read `newbranch` as the colour and passed the segment.
#   * whether the command is in LIST mode, where an operand is a pattern to match
#     rather than a ref to create. `git branch --list newbranch` lists, it does
#     not create, so treating that operand as a creation would be a false denial.
#
# Splitting them keeps both answers right: `--color` is in neither set, and
# `--list` is in the list-mode set only.

# Flags whose argument is REQUIRED, so git takes it from the following word.
_GIT_REF_VALUE_FLAGS: frozenset[str] = frozenset(("--points-at", "--format", "--sort"))


def _consumes_next_word(token: str) -> bool:
    """Whether *token* is a required-argument flag that takes the FOLLOWING word.

    Abbreviations count, because git's parser resolves them: `git branch --form`
    reaches `--format` and eats the next word, so reading `--form` as an ordinary
    option left `-l` in `git branch --form -l newbranch` looking like a list flag
    and licensed the bare operand that created the branch.

    This is the fourth site on this guard to need the abbreviation axis — after
    `_matched_flag`, `_option_accept_list_violation` and `_glob_reaches` — which is why
    named helper rather than a fourth inline prefix test.

    An ATTACHED value (`--format=x`) takes nothing from the next word, so it is
    not one of these. Over-matching an ambiguous abbreviation is safe: git rejects
    it rather than running it.
    """
    if "=" in token or not token.startswith("--") or len(token) <= 2:
        return False
    return any(flag == token or flag.startswith(token) for flag in _GIT_REF_VALUE_FLAGS)


# Flags that put `git branch` / `git tag` in list mode, where a bare operand is a
# pattern. `--points-at` appears in both: it consumes its value AND selects.
_GIT_REF_LIST_FLAGS: frozenset[str] = frozenset(
    (
        "-l",
        "--list",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
    )
)


def _glob_shifts_arguments(token: str) -> bool:
    """Whether a glob in *token* can change how many arguments the program gets.

    A separate question from `_glob_hides_word`, which asks what a pattern can
    expand INTO. This one is about the COUNT, and it cuts both ways:

    * several matches become several WORDS — with `in1` and `in2` present,
      `uniq in*` runs `uniq in1 in2`, whose second operand is an OUTPUT file;
    * no match under `nullglob` makes the word VANISH — `git branch --format
      nomatch* --list newbranch` loses the format's value, so `--format` eats
      `--list` instead and `newbranch` stops being a pattern.

    Neither outcome needs the pattern to resemble anything this module decides on,
    so it is only asked where the argument COUNT or POSITION carries the verdict:
    a `uniq` operand, and a required git option's value. An operand whose meaning
    does not depend on its position — `ls *.py`, `git branch --list 'feat/*'` —
    is not affected, which is what keeps ordinary globbing on the read path.
    """
    return bool(_GLOB_META_RE.search(token) or _EXTGLOB_RE.search(token))


# The SHORT list-mode flags, per subcommand, because they do not agree. `-l` is
# a listing for both, but `git tag -n[<num>]` prints annotation lines — a listing
# form `git branch` has no counterpart for, and reading it as anything else
# denied a common inspection (`git tag -n 'v1.*'`) its auto-approval. Kept as
# LETTERS, not flags, because they arrive bundled: `git tag -n2` and `git tag -ln`
# both select, and only a per-letter test sees that. A letter here must never be
# a write flag for the same subcommand — `-n` is not in `_GIT_REF_WRITE_FLAGS`
# ("tag"), which is what makes reading it as a listing safe.
_GIT_REF_LIST_SHORTS: dict[str, str] = {"branch": "l", "tag": "ln"}

# Flags that CANCEL list mode, per subcommand. git's parse-options auto-generates
# a `--no-<opt>` negation for a boolean, so `--list` has one and it undoes the
# listing — `git branch --list --no-list newbranch` CREATES the branch. Verified
# against git rather than inferred, which matters because the neighbouring
# `--no-` spellings do NOT behave this way:
#
#     git branch --list --no-list nl1                -> branch nl1 CREATED
#     git branch --list --no-lis nl2                 -> branch nl2 CREATED
#     git branch -l --no-list nl3                    -> branch nl3 CREATED
#     git branch --contains HEAD --no-contains nc1   -> no ref (git errors)
#     git branch --merged --no-merged nm1            -> no ref (git errors)
#     git tag -l --no-list t1                        -> no ref (unknown option)
#
# So `--no-contains` and `--no-merged` are real list FILTERS rather than
# negations, and treating them as cancelling would deny two ordinary reads;
# `git tag` has no `--no-list` at all. The table says only what was measured.
_GIT_REF_LIST_CANCEL_FLAGS: dict[str, tuple[str, ...]] = {"branch": ("--no-list",)}


def _cancels_list_mode(token: str, subcommand: str) -> bool:
    """Whether *token* turns list mode off for *subcommand*.

    Abbreviations count, as everywhere else on this guard: `--no-lis` reaches
    `--no-list`. Cancelling can only move a bare operand from "pattern" to
    "creates a ref", i.e. toward the prompt, so over-matching here is safe.
    """
    head = token.split("=", 1)[0]
    if not head.startswith("--") or len(head) <= 2:
        return False
    cancels = _GIT_REF_LIST_CANCEL_FLAGS.get(subcommand, ())
    return any(flag.startswith(head) for flag in cancels)


# `git remote` subcommands that rewrite remote configuration. `set-url` is the
# sharpest: it repoints the remote, so later fetches and pushes go elsewhere.
_GIT_REMOTE_WRITE_SUBCOMMANDS: frozenset[str] = frozenset(
    ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "prune", "update")
)

# Allowlist entries that name a VERSION PROBE, matched EXACTLY rather than as a
# prefix like every other entry, because a prefix match vouches for a trailing
# operand too. That is not academic: `javac` does not act on `-version` and exit,
# it prints the version and then compiles whatever else it was handed, so
#
#     javac -version -processorpath evil.jar -processor Evil Payload.java
#
# auto-approves and runs an annotation processor — ordinary compiled Java on a
# path the caller supplies, i.e. arbitrary code execution, and the
# highest-severity shape in this family.
#
# All five probes are listed, not only `javac`. Whether an interpreter ignores a
# trailing operand is a property of the installed release rather than of the
# flag, and JDK single-file source mode (`java Foo.java`) already moved that
# answer once. A version probe has no legitimate operand, so requiring the exact
# spelling costs nothing and does not depend on being right about each tool.
_EXACT_ONLY_BASH_PREFIXES: frozenset[str] = frozenset(
    (
        "python --version",
        "python3 --version",
        "node --version",
        "java -version",
        "javac -version",
    )
)

# `sort` is vetted POSITIVELY: every option token must be a recognised read-only
# flag, and anything unrecognised goes to the human prompt.
#
# The inversion is here because a denylist demonstrably does not converge on this
# one tool: six distinct spellings of the same escape reach it —
# `-o FILE`, `--output=FILE`, attached `-oFILE`, bundled `-uo FILE`, abbreviated
# `--o FILE`, and `--compress-program=PROG` -- which is not a write at all
# but arbitrary CODE EXECUTION. Verified: with the input large enough to spill to
# temporaries, `sort -S 1k --compress-program=./payload big.txt` ran the payload and
# exited 0. Enumerated from this box's own `sort --help`, so it is complete for that
# release rather than for a guess.
#
# Deliberately NOT read-only: `-o/--output` (writes), `-T/--temporary-directory`
# (writes temporaries into a caller-named directory), `--compress-program`
# (executes), and `--random-source` / `--files0-from` (open a caller-named path).
# An unlisted flag costs a prompt, so omission is the safe direction.
# `k`, `t` and `S` are value-taking AND read-only (key, field separator, buffer
# size); `o` and `T` are value-taking and NOT read-only, which is what makes the
# value branch below refuse them while `-k2n` and `-S1k` pass.
_SORT_READONLY_SHORT: frozenset[str] = frozenset("bdfgiMhnRrVcCmsuzktS")
# Short options that consume the rest of the token (or the next one) as a VALUE.
# Needed so `-k2n` and `-S1k` read as flag-plus-value instead of a letter cluster
# where `2` and `1` look like unknown options.
_SORT_VALUE_SHORT: frozenset[str] = frozenset("ktSTo")
_SORT_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--ignore-leading-blanks",
        "--dictionary-order",
        "--ignore-case",
        "--general-numeric-sort",
        "--ignore-nonprinting",
        "--month-sort",
        "--human-numeric-sort",
        "--numeric-sort",
        "--random-sort",
        "--reverse",
        "--sort",
        "--version-sort",
        "--batch-size",
        "--check",
        "--debug",
        "--key",
        "--merge",
        "--stable",
        "--buffer-size",
        "--field-separator",
        "--parallel",
        "--unique",
        "--zero-terminated",
        "--help",
        "--version",
    )
)


# `date`'s read-only surface. Enumerated from GNU coreutils `date --help`, then
# checked for a second axis the other accept-lists did not have to face: whether the
# same LETTER means different things in different `date` implementations.
#
# `-d` IS on the list, and the reason it is here is worth recording because an earlier
# revision of this change left it OFF on the belief that BSD/macOS `date -d` sets the
# kernel's daylight-saving value. That belief came from documentation, not from the
# implementations, and checking the implementations showed it is false on every
# platform that ships today. Read from each project's own `getopt(3)` string:
#
#   GNU coreutils      `-d STRING`  parses and PRINTS  (verified by execution, 8.22)
#   FreeBSD  bin/date  "f:I::jnRr:uv:z:"   no `d` at all -> invalid option
#   Apple    shell_cmds/date  "f:I::jnRr:uv:z:"   no `d` at all -> invalid option
#   OpenBSD  bin/date  "af:jr:uz:"         no `d` at all -> invalid option
#   NetBSD   bin/date  "ad:f:jnRr:Uuz:"    `-d` sets rflag and parsedate()s optarg,
#                                          i.e. the GNU meaning: a reference time to
#                                          PRINT. `setthetime()` is reached only from
#                                          a bare operand, never from `-d`.
#
# So `-d` either reads or errors, never writes. The historical `-d dst` that set the
# kernel daylight-saving flag is gone from every current BSD.
#
# There was also an internal tell that should have caught this without the source
# dive: `--date=` was already on the read-only long list, and `-d` is the same option
# under a shorter spelling on every implementation that has it. Admitting one and
# refusing the other could not both be right.
#
# `-s`/`--set` IS the setter GNU shares, verified accepted and failing only on
# privilege ("date: cannot set date: Operation not permitted"). `-f`, `-r`, `-I` and
# `-d` take values, which is what makes `-Iseconds`, `-r FILE` and `-d yesterday` read
# cleanly while `-s` is refused -- the dilemma that kept `-s` out of the old
# write-flag table.
#
# The other three accept-lists were swept for divergence and need no change: BSD
# `sort`'s writers are `-o`/`-T` and BSD `file`'s is `-C`, all already excluded, and
# BSD `hostname` offers only `-f`/`-s` plus a name OPERAND, which `operands="none"`
# already refuses. BSD `date`'s other setters (`-t` minutes west, `-j`, `-n`, `-v`)
# are likewise absent from this list, so they fail closed already.
_DATE_READONLY_SHORT: frozenset[str] = frozenset("dfIrRu")
_DATE_VALUE_SHORT: frozenset[str] = frozenset("dfIrs")
_DATE_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--date",
        "--file",
        "--iso-8601",
        "--reference",
        "--rfc-2822",
        "--rfc-3339",
        "--universal",
        "--utc",
        "--help",
        "--version",
    )
)
# Long and short forms that consume the NEXT token, so an operand count is not
# fooled by a flag's value. `-I` is absent on purpose: its TIMESPEC is optional and
# must be attached (`-Iseconds`), so `-I` never eats the following word.
_DATE_VALUE_FLAGS: frozenset[str] = frozenset(
    ("-d", "-f", "-r", "-s", "--date", "--file", "--reference", "--set")
)

# `hostname`'s surface, from its own `--help`. Tiny and fully enumerable, which is
# why a positive list is cheap here. `-b/--boot` and `-F/--file` SET the name with
# no operand (both verified: privilege-only failure, with `-F` re-tested against a
# file that EXISTS -- against a missing path it fails at open() and looks read-only).
_HOSTNAME_READONLY_SHORT: frozenset[str] = frozenset("aAdfiIsyVh")
_HOSTNAME_VALUE_SHORT: frozenset[str] = frozenset("F")
_HOSTNAME_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--alias",
        "--all-fqdns",
        "--all-ip-addresses",
        "--domain",
        "--fqdn",
        "--ip-address",
        "--long",
        "--nis",
        "--short",
        "--yp",
        "--help",
        "--version",
    )
)


# `file`'s surface, from its own `--help`. It reached an accept-list rather than a
# `-C` denylist entry because that is the shape this change keeps converging on: an
# unlisted option prompts instead of passing, so a flag missing from the help text
# costs a prompt rather than a write. `git blame --textconv` is the reason that
# distinction is not academic.
#
# `-C/--compile` is the setter: with `-m FILE` it compiles that magic file and writes
# `FILE.mgc` beside it (verified, 464 bytes). `-z/--uncompress` is ALSO excluded, on
# the omission-is-cheap principle rather than a measured escape -- libmagic can shell
# out to an external decompressor for formats it does not handle internally, and the
# flag is rare enough that a prompt costs nothing. Everything else prints.
# `f`/`--files-from` is absent, and for a different reason than `-C`: it does not
# write, it INDIRECTS. `file -f LIST` opens every path named inside LIST, and those
# paths never appear in the command, so the hook layer's path gates
# (`is_sensitive_path` / `is_sensitive_bash_command`, applied to the command text) see
# only LIST and cannot see what is actually read. A guard that inspects argv is blind
# to one more level of indirection, so the option has to go rather than the guard get
# cleverer. `sort --files0-from` was already excluded for the same shape; `hostname -F`
# is already refused as a setter.
#
# Kept: `-m/--magic-file`, `-e/--exclude`, `-F/--separator` all take a value, but the
# value IS the path or string being used, visible in argv, so the guards can act on it.
# There is no indirection to hide behind.
#
# `p`/`--preserve-date` is absent too, and it is the subtlest of the three exclusions.
# It LOOKS read-only because it RESTORES the access time rather than setting a caller
# chosen one -- which is how it was originally, and wrongly, admitted here. Restoring
# still requires a `utimes()` call on the named path, and the `ctime` that call bumps is
# NOT restorable. So the option erases the evidence that a file was read while leaving a
# permanent metadata modification behind: the wrong side of read-only in both directions.
#
# MEASURED, because `noatime` on this box hides the atime effect entirely and made the
# obvious test inconclusive. `ctime` advances on any inode metadata write and is visible
# whatever the mount options are:
#
#   file t.txt            -> ctime unchanged   (control)
#   file -b t.txt         -> ctime unchanged   (control)
#   file -p t.txt         -> ctime ADVANCED
#   file --preserve-date  -> ctime ADVANCED
#
# The same probe was then run over every other accept-list flag that opens a named file
# -- `file -m/-k/-L/-s/-r`, `sort`, `sort -u`, `sort -k1`, `date -r`, `date -f`, plus
# `cat` and `wc -l` as controls -- and all twelve are clean. `-p` is the only one.
#
# `-z`/`--uncompress` is also absent, and it belongs to a class this module already
# names elsewhere rather than to the write-flag class. From `file`'s own
# `src/compress.c`, the decompressor is SPAWNED:
#
#     status = posix_spawnp(&pid, compr[method].argv[0], &fa, NULL, ...)
#
# with `compr[]` holding `"gzip"`, `"bzip2"`, `"lzip"`, `"xz"`, `"lrzip"`, `"zstd"` and
# `method` selected from the examined file's magic bytes. So `-z` runs a program whose
# NAME is chosen by the content being inspected, which is the same hand-off as
# `git diff --ext-diff`. Stated because it is a behaviour change: on the write-flag
# table `file -z` auto-approved, and under this list it prompts.
_FILE_READONLY_SHORT: frozenset[str] = frozenset("vmbceFiklLhnN0rsd")
# `f` stays here so `-f LIST` and `-fLIST` are both recognised as flag-plus-value and
# refused, rather than `LIST` being mistaken for an operand.
_FILE_VALUE_SHORT: frozenset[str] = frozenset("mefF")
_FILE_READONLY_LONG: frozenset[str] = frozenset(
    (
        "--apple",
        "--brief",
        "--checking-printout",
        "--debug",
        "--dereference",
        "--exclude",
        "--keep-going",
        "--list",
        "--magic-file",
        "--mime",
        "--mime-encoding",
        "--mime-type",
        "--no-buffer",
        "--no-dereference",
        "--no-pad",
        "--print0",
        "--raw",
        "--separator",
        "--special-files",
        "--help",
        "--version",
    )
)
# `file` needs no VALUE-FLAG set: `spec.value_flags` is read only by `_operands`,
# which is behind an early return for `operands == "any"`, and `file`'s operands are
# the files it identifies. A set here would look load-bearing and never be read.


class _AcceptSpec(NamedTuple):
    """A tool's read-only surface, stated positively.

    One registry rather than three bespoke checks, because the algorithm turned out
    identical for every tool that needed it. `sort` had this shape first; `date` and
    `hostname` arrived at it for the same reason -- a per-tool DENYLIST had already
    leaked on each of them, and an accept-list is closed by construction instead.
    """

    reason_fmt: str  # carries `{tok}`
    readonly_short: frozenset[str]
    value_short: frozenset[str]
    readonly_long: frozenset[str]
    operands: str  # "any" (they are inputs) | "none" | "plus" (only +FORMAT)
    operand_reason: str
    value_flags: frozenset[str]  # for operand counting


_OPTION_ACCEPT_LISTS: dict[str, _AcceptSpec] = {
    "sort": _AcceptSpec(
        reason_fmt="pipe target 'sort {tok}' is not a recognised read-only option",
        readonly_short=_SORT_READONLY_SHORT,
        value_short=_SORT_VALUE_SHORT,
        readonly_long=_SORT_READONLY_LONG,
        # sort's operands are input FILES, which it reads.
        operands="any",
        operand_reason="",
        value_flags=frozenset(),
    ),
    "date": _AcceptSpec(
        # The reason names the accepted spelling because `date -d` is a form agents
        # emit constantly, and a refusal that only says no turns every one of them
        # into a human prompt instead of a self-serve retry.
        reason_fmt=(
            "'date {tok}' is not a recognised read-only option; "
            "'--date=<expr>' is the read-only spelling"
        ),
        readonly_short=_DATE_READONLY_SHORT,
        value_short=_DATE_VALUE_SHORT,
        readonly_long=_DATE_READONLY_LONG,
        # `date 08221200` sets the clock (verified: privilege-only failure). A `+`
        # operand is the output FORMAT and only prints.
        operands="plus",
        operand_reason=("'date <operand>' sets the system clock unless it is a +FORMAT string"),
        value_flags=_DATE_VALUE_FLAGS,
    ),
    "file": _AcceptSpec(
        reason_fmt="'file {tok}' is not a recognised read-only option",
        readonly_short=_FILE_READONLY_SHORT,
        value_short=_FILE_VALUE_SHORT,
        readonly_long=_FILE_READONLY_LONG,
        # `file`'s operands are the FILES it identifies, which it only reads.
        operands="any",
        operand_reason="",
        value_flags=frozenset(),
    ),
    "hostname": _AcceptSpec(
        reason_fmt="'hostname {tok}' is not a recognised read-only option",
        readonly_short=_HOSTNAME_READONLY_SHORT,
        value_short=_HOSTNAME_VALUE_SHORT,
        readonly_long=_HOSTNAME_READONLY_LONG,
        operands="none",
        operand_reason=("'hostname <operand>' sets the hostname; every read form is flag-only"),
        value_flags=frozenset(("-F", "--file")),
    ),
}

#: Keys whose verdict depends on the COUNT or ORDER of words rather than on which
#: flags are present, so a word bash deletes can flip it. See `_ELISION_SENSITIVE_RE`
#: for the measurements and for why the refusal is scoped here instead of applied to
#: every command.
#:
#: Derived from the tables that carry those decisions, so a tool added to the accept
#: list with an operand rule joins this set without a second edit. `git remote` and
#: `uniq` are named: their decision is positional in the walk itself (first
#: non-option word is the subcommand; second operand is the output file) rather than
#: expressed in a table this can read.
_ELISION_SENSITIVE_KEYS: frozenset[str] = (
    frozenset(f"git {subcommand}" for subcommand in _GIT_REF_WRITE_FLAGS)
    | frozenset(("git remote", "uniq"))
    | frozenset(verb for verb, spec in _OPTION_ACCEPT_LISTS.items() if spec.operands != "any")
)


def _option_accept_list_violation(prefix: str, tokens: list[str]) -> str:
    """Reason *tokens* leave *prefix*'s positively-vetted read-only surface, else "".

    Deny-by-default per tool: an option has to be RECOGNISED as read-only, so an
    unlisted one prompts instead of passing. That is what makes this closed by
    construction where a write-flag denylist was not -- a spelling nobody thought of
    is refused rather than admitted.

    A long flag must match EXACTLY, which disposes of getopt_long abbreviation for
    free: `--out` is an abbreviation of `--output` and simply is not in the read-only
    set. The cost is that an abbreviation of a read-only flag (`--rev` for
    `--reverse`) also prompts.
    """
    spec = _OPTION_ACCEPT_LISTS[prefix]
    # `--` does not stop this loop either. HARDENING rather than a fix here: measured,
    # every value-taking read flag of this box's `sort` REJECTS `--` as its value and
    # aborts (`-k` "invalid number", `-S` "invalid -S argument '--'", `-t`
    # "multi-character tab"), so `sort -k -- -o OUT` writes nothing today. That is
    # sort's argument validation saving us, not this classifier, and it is not a
    # property worth depending on -- the git path above proved the same shape does
    # write when the tool is more permissive. Cost is a prompt on an input FILE named
    # like an option (`sort -- -o`).
    for token in tokens:
        if not token.startswith("-") or token == "-":
            continue  # operand, or `-` for stdin
        if _GLOB_META_RE.search(token):
            # An option-shaped token whose real spelling the shell has not produced
            # yet. No legitimate option contains a glob metacharacter, so this costs
            # nothing, and an operand glob is untouched: it has no leading dash.
            return spec.reason_fmt.format(tok=token)
        if token.startswith("--"):
            if token.partition("=")[0] not in spec.readonly_long:
                return spec.reason_fmt.format(tok=token)
            continue
        for letter in token[1:]:
            if letter in spec.value_short:
                # This option takes a value, so the remainder of the token is that
                # value and carries no further option letters.
                if letter not in spec.readonly_short:
                    return spec.reason_fmt.format(tok=f"-{letter}")
                break
            if letter not in spec.readonly_short:
                return spec.reason_fmt.format(tok=f"-{letter}")
    if spec.operands == "any":
        return ""
    operands = _operands(tokens, spec.value_flags)
    if spec.operands == "none" and operands:
        return spec.operand_reason
    if spec.operands == "plus" and any(not o.startswith("+") for o in operands):
        return spec.operand_reason
    return ""


def _operands(args: list[str], value_flags: frozenset[str] = frozenset()) -> list[str]:
    """Operand tokens in *args*, honouring the `--` terminator.

    Before the terminator a leading-dash word is an option; after it EVERY word
    is an operand however it is spelled. That second half is what
    `uniq -- input -pwned` turned on: counting only the non-dash words saw one
    operand and passed a segment that writes `-pwned`.
    """
    if "--" in args:
        at = args.index("--")
        before, after = args[:at], args[at + 1 :]
    else:
        before, after = args, []
    out: list[str] = []
    previous = ""
    for tok in before:
        if tok.startswith("-"):
            # A short option consumes the NEXT word only when the token is the bare
            # flag; `-Iseconds` carries its own value, so treating it as `-I` plus a
            # separate operand would deny an ordinary read.
            previous = tok if tok in value_flags else ""
            continue
        if previous:
            previous = ""
            continue
        out.append(tok)
    return out + after


#: Shell expansions whose RESULT is the argument, while ``shlex`` hands this
#: module the unexpanded text. Every check here is keyed on the token, so where
#: the two disagree the guard inspects one string and the program receives
#: another:
#:
#:     git diff $'--output=/tmp/pwned'       shlex: `$--output=…`     bash: `--output=…`
#:     git diff $"--output=/tmp/pwned"       shlex: `$--output=…`     bash: `--output=…`
#:     git diff ${HOME:+--output=/tmp/pwned} shlex: literal           bash: `--output=…`
#:     git remote se${x}t-url …              shlex: `se${x}t-url`     bash: `set-url`
#:     git diff --{out,out}put=/tmp/pwned    shlex: `--{out,out}put=` bash: `--output=…`
#:
#: Matched as ONE class rather than one spelling at a time. Closing ``$'`` alone
#: leaves ``$"`` (locale translation) and ``${…}`` (parameter expansion) open on
#: the identical path, and the remaining forms are bounded only by bash's grammar.
#: Un-expanding them here would mean reimplementing that grammar, so a segment
#: carrying one is refused instead: a read-only command has no need of any of
#: them, and a rejected segment falls through to the human approval prompt.
#:
#: Brace expansion belongs to the same class even though it carries no ``$``: it
#: is performed FIRST, before any other expansion, and it can assemble a flag out
#: of fragments that match nothing here. Only the forms bash actually expands are
#: matched — a comma list or a ``..`` range — so a lone ``{`` (a JSON argument, a
#: Go template) is left alone.
#:
#: ``$(…)`` and backticks are already refused upstream by ``_UNSAFE_SHELL_RE``;
#: this covers what that pattern does not reach.
#:
#: Positional and special parameters (``$1``, ``$@``, ``$*``, ``$?``, ``$$``,
#: ``$!``, ``$#``, ``$-``) belong to the same class and are matched by their own
#: alternative. Their NAME is not an identifier, so the ``$[A-Za-z_]`` branch
#: above does not match them, and in a `bash -c` string with no positional
#: arguments
#: ``$@`` and ``$*`` expand to NOTHING — which is what makes them the sharpest
#: spelling here rather than a curiosity:
#:
#:     git remote $@set-url origin …   shlex: `$@set-url`      bash: `set-url`
#:     git diff $1--output=/tmp/pwned  shlex: `$1--output=…`   bash: `--output=…`
#:
#: Matched on the raw segment, so a QUOTED occurrence is refused too even though
#: bash would not expand it (``grep '*.{js,ts}' f``). That is the same trade the
#: ``$`` forms already make, and it errs toward the prompt.
#:
#: Applied only to a GUARDED verb (see ``_side_effect_reason``). A verb this
#: module has no table for cannot have a decision subverted by a hidden word,
#: so ``cat $HOME/.bashrc`` and ``head -20 $LOG`` — the ordinary reads — stay on
#: the auto-approve path.
_SHELL_EXPANSION_RE = re.compile(
    r"\$['\"{]"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\$[0-9@*#?$!\-]"
    r"|\{[^{}\s]*,[^{}\s]*\}"
    r"|\{[^{}\s]*\.\.[^{}\s]*\}"
)
#: Pathname-expansion metacharacters. NOT part of the class above, because a glob
#: is usually the argument itself in a read-only command (`ls *.py`) — it is
#: refused only in the positions where the spelling is what gets classified. See
#: the note in `_side_effect_reason`.
#:
#: A leading `~` is deliberately NOT here: tilde expansion yields a path starting
#: with `/`, so it cannot synthesize a flag or a subcommand.
_GLOB_META_RE = re.compile(r"[*?\[]")

#: Bash EXTGLOB operators, which synthesize a token the same way an ordinary glob
#: does — `git diff @(--output=pwned)` matches a file of that name and git writes
#: it.
#:
#: These get their own regex and their own verdict because `fnmatch` — the test
#: that makes the plain-glob case precise — does not implement extglob: it reads
#: `@(` as two literal characters, so `fnmatch("--output", "@(--output")` is False
#: and the pattern that reaches the flag looks inert. Nothing can be proven about
#: an extglob token here, so a guarded verb refuses it outright. That is the same
#: trade the `$`-led forms make, and it costs nothing: unlike a plain glob, an
#: extglob has no ordinary use in a read-only command.
#:
#: Extglob is off by default in a non-interactive `bash -c`, so reaching this needs
#: `shopt -s extglob` (or a `BASHOPTS` carrying it) AND a matching file — narrower
#: than the plain-glob case, closed here because it is the same cause.
_EXTGLOB_RE = re.compile(r"[?*+@!]\(")

#: Every word this module decides on: the flags of all four tables, the
#: ``git remote`` write subcommands, and the option terminator. A glob is
#: dangerous exactly when the filesystem can hand the program one of THESE in
#: place of the pattern, so the test is ``fnmatch`` against this set rather than
#: "the token contains a metacharacter" — which would have taken `ls *.py` and
#: `git diff *.py` off the read-only path for no gain.
_GLOB_SENSITIVE_WORDS: frozenset[str] = (
    frozenset(
        flag
        for table in (
            _WRITE_FLAGS,
            _EXEC_FLAGS,
            _GIT_REF_WRITE_FLAGS,
            # Derived, not restated: this is the whole reason `wc --file*` is
            # refused. A checkout containing a file named `--files0-from=payload`
            # turns that pattern into the flag, and measured, `wc --file*` then read
            # a path that appears nowhere in the command. Listing the flag in one
            # table and having the glob defence read that table is what keeps the
            # two from drifting -- the same coupling that broke when `sort` moved
            # off the denylist.
            _INDIRECT_LIST_FLAGS_BY_PREFIX,
        )
        for flags in table.values()
        for flag in flags
    )
    | _GIT_REMOTE_WRITE_SUBCOMMANDS
    # A glob that expands to `--` shifts every following word into operand
    # position, which is how the terminator changes what the walk below decides.
    | frozenset(("--",))
    # The accept-listed tools have no denylist to derive from, but they do not need
    # one: a letter that TAKES A VALUE and is not READ-ONLY is refused by the
    # registry by construction, so it is precisely a word a glob must not reach.
    # For `sort` that yields `-o` and `-T`. Without this, moving a tool to a positive
    # list dropped it out of this set -- measured, `cat f | sort ?uo victim` was
    # auto-approved because `-o` had stopped being a sensitive word.
    | frozenset(
        f"-{letter}"
        for spec in _OPTION_ACCEPT_LISTS.values()
        for letter in spec.value_short - spec.readonly_short
    )
)

#: Verbs whose OWN tables carry a short flag, so a glob can expand into a bundled
#: cluster for them (``?uo`` -> ``-uo``, which supplies ``-o``). A cluster is not
#: a word in the set above, so it takes the extra test in `_glob_hides_word` —
#: and only here, which is what keeps `git diff *.py` (long flags only) passing.
#: Derived from the tables so the two cannot drift apart.
_SHORT_FLAG_VERBS: frozenset[str] = frozenset(
    key
    for table in (_WRITE_FLAGS, _EXEC_FLAGS)
    for key, flags in table.items()
    if any(len(flag) == 2 and flag[0] == "-" for flag in flags)
) | frozenset(
    verb for verb, spec in _OPTION_ACCEPT_LISTS.items() if spec.value_short - spec.readonly_short
)


def _glob_hides_word(token: str, has_short_flags: bool) -> bool:
    """Whether *token*'s glob can expand into a word this module decides on.

    Two shapes, because a pattern reaches a flag two different ways:

    * it matches a decided word outright — ``s?t-url`` matches ``set-url``,
      ``--outp?t`` matches ``--output``, ``?o`` matches ``-o``, and a bare ``*``
      matches every one of them. ``fnmatchcase`` answers this exactly, so a
      pattern that CANNOT reach one (``*.py``) is left alone;
    * its metacharacter is the FIRST character, so the filesystem chooses the
      leading character too and the expansion can be a short-option CLUSTER
      (``?uo`` -> ``-uo``, which :func:`_matched_flag` reads as supplying ``-o``).
      A cluster is not a word in the set above, so it needs its own test — but
      only where the verb HAS a short flag to be bundled into, which keeps
      ``git diff *.py`` (long flags only) passing.

    An EXTGLOB token short-circuits to True: ``fnmatch`` cannot model extglob, so
    neither shape below can rule on one. See `_EXTGLOB_RE`.
    """
    if _EXTGLOB_RE.search(token):
        return True
    if not _GLOB_META_RE.search(token):
        return False
    head = token.split("=", 1)[0]
    # A token that already LOOKS like an option is refused on the metacharacter
    # alone, without asking what it can match. `fnmatch` answers "can this reach a
    # decided word", and a short-option CLUSTER is not one of those words, so
    # `sort -u? victim` slipped: no candidate is three characters long, the
    # metacharacter is not first so the cluster test below does not fire, and bash
    # resolves `-u?` against a file named `-uo` — which `_matched_flag` would have
    # rejected had it ever seen it. Nothing legitimate is lost, because the head is
    # the flag NAME: a glob in a flag's VALUE is split off above, which is what
    # keeps `git log --grep=[abc]` a read.
    if token.startswith("-") and _GLOB_META_RE.search(head):
        return True
    # Every word this module decides on, PLUS every abbreviation of a long one,
    # because `_matched_flag` resolves an abbreviation and so does the parser it
    # guards. Testing only the full spellings left `git diff ??out=victim`
    # auto-approved: `fnmatch("--output", "??out")` is False on the length alone,
    # `git diff`'s table is long-only so the cluster arm below does not fire, and
    # bash resolves `??out` against a file named `--out` that git then reads as
    # `--output`. The full spelling `??output` was already refused, which is what
    # made the gap look closed.
    if any(_glob_reaches(head, word) for word in _GLOB_SENSITIVE_WORDS):
        return True
    return has_short_flags and _GLOB_META_RE.match(token) is not None


def _glob_reaches(head: str, word: str) -> bool:
    """Whether glob *head* can expand to *word* or to an abbreviation of it.

    A long option is abbreviable to any unambiguous prefix, and an ambiguous one
    is rejected by the tool rather than run — so every prefix of `--` plus one
    character is tested, and over-matching can only add a prompt.

    Compared CASE-INSENSITIVELY, because `nocaseglob` decouples the pattern's case
    from the filename's: with it set, `git diff ??OUT=victim` expands to
    `--out=victim` and git writes the file, while a case-sensitive test saw a
    pattern matching nothing. Measured — `bash -O nocaseglob -c 'echo git diff
    ??OUT=victim'` prints `git diff --out=victim`, and plain `bash -c` does not.

    The case sensitivity this module DOES rely on is elsewhere and unaffected:
    `_matched_flag` still distinguishes `file -C` (compiles a magic file) from
    `file -c` (prints one), because that reads a literal token rather than asking
    what a pattern could become.
    """
    folded = head.lower()
    lowered = word.lower()
    if fnmatch.fnmatchcase(lowered, folded):
        return True
    if not word.startswith("--") or len(word) <= 3:
        return False
    return any(fnmatch.fnmatchcase(lowered[:cut], folded) for cut in range(3, len(word)))


def _matched_flag(tokens: list[str], flags: tuple[str, ...]) -> str:
    """Return the first token in *tokens* that supplies one of *flags*.

    Matches the flag on its own (``-o``), joined to its value (``--output=x``)
    and bundled into a short-option cluster (``-uo`` supplies ``-o``), so the
    check cannot be stepped around by respelling the same flag.
    """
    shorts = {f[1] for f in flags if len(f) == 2 and f[0] == "-"}
    longs = [f for f in flags if f.startswith("--") and len(f) > 2]
    for tok in tokens:
        if tok in flags:
            return tok
        for flag in flags:
            if tok.startswith(flag + "="):
                return flag
        # A GNU long option may be ABBREVIATED to any unambiguous prefix, so
        # `--out=FILE` and `--outp=FILE` reach the same `--output` that exact
        # matching missed. Accept any prefix of a known flag that is at least
        # `--` plus one character: the parser this guards resolves it, so the
        # guard has to as well. Over-matching here can only add a prompt.
        head = tok.split("=", 1)[0]
        if head.startswith("--") and len(head) > 2:
            for flag in longs:
                if flag.startswith(head):
                    return flag
        if shorts and len(tok) > 1 and tok[0] == "-" and tok[1] != "-":
            for ch in tok[1:]:
                if ch in shorts:
                    return "-" + ch
    return ""


def _side_effect_reason(segment: str) -> str:
    """Reason *segment* has a side effect, despite naming a read-only verb.

    Returns "" when the segment is genuinely read-only. Called after the verb
    has cleared the allowlist, because the allowlist only decides *which
    program* runs — not what the rest of the command line asks it to do.
    """
    try:
        # Discard-only redirects are scrubbed first, mirroring the unsafe-shell
        # check upstream, because the exact-match rule below would otherwise read
        # one as a trailing operand. `java -version 2>&1` is the canonical probe —
        # java prints its version to stderr — so counting `2>&1` as an operand
        # would deny the single most common read on this path.
        tokens = shlex.split(_DEVNULL_REDIR_RE.sub(" ", segment))
    except ValueError:
        # Cannot recover argv, so cannot vouch for the operands.
        return "quoting cannot be resolved"
    if not tokens:
        return ""
    # A version probe acts on an operand, so its entry matches EXACTLY: the
    # allowlist named `javac -version`, the prefix match vouched for everything
    # after it, and javac compiled it. See `_EXACT_ONLY_BASH_PREFIXES`.
    spelled = " ".join(tokens).lower()
    for probe in _EXACT_ONLY_BASH_PREFIXES:
        if spelled.startswith(probe + " "):
            return f"'{probe}' takes no operand, and acts on one when given it"
    # The verb is matched case-insensitively, like the allowlist does, so an
    # unusual spelling cannot step past the table. Flags keep their case,
    # because for these programs the case carries the meaning.
    verb = tokens[0].rsplit("/", 1)[-1].lower()
    args = tokens[1:]

    # Checked on the RAW segment, before any table lookup, because the thing being
    # guarded against is a word that reached `shlex` but will not reach the program.
    elision_key = f"git {args[0].lower()}" if verb == "git" and args else verb
    if elision_key in _ELISION_SENSITIVE_KEYS and _ELISION_SENSITIVE_RE.search(segment):
        return f"a word bash removes could change what '{elision_key}' does"

    # Whether an unexpanded word can subvert THIS segment's classification.
    #
    # Every check below is keyed on a table, so a verb with no table has no
    # decision to subvert: whatever `cat $HOME/.bashrc` expands to, this
    # function was always going to return "". Refusing an expansion there buys
    # nothing and costs the most ordinary read on the auto-approve path, so the
    # refusal is scoped to the verbs whose arguments this module reads.
    #
    # `git` is guarded whatever the subcommand, because the subcommand itself is
    # a decided word: `git $x` reaches bash as `git branch -D release`.
    #
    # `hostname` and `date` are guarded through `_OPTION_ACCEPT_LISTS` rather than a
    # write-flag table, because their rule is an OPERAND rule: an unexpanded word IS
    # the decision there, so `hostname $EVIL` renames the host under a spelling this
    # module read as harmless.
    guarded = (
        verb in ("git", "uniq")
        or verb in _WRITE_FLAGS
        or verb in _EXEC_FLAGS
        or verb in _OPTION_ACCEPT_LISTS
        # A verb whose arguments this module reads for an INDIRECTION or a pager
        # startup command has a decision to subvert just as much as one with a
        # write-flag table, so it belongs in the same guard.
        or verb in _INDIRECT_LIST_FLAGS_BY_PREFIX
        or verb in _PAGER_STARTUP_VERBS
    )

    # ANSI-C quoting is stripped by `shlex` but honoured by bash, so the token
    # this check inspects is not the token the shell runs: `git diff $'-o'` reaches
    # `shlex` as `$-o` — matching no flag — while bash passes `-o`. The same trick
    # hides a subcommand (`git remote $'set-url'`), and a positional or special
    # parameter does it with no quoting at all — `git remote $@set-url …`, where
    # `$@` expands to nothing in a `bash -c` string. It is a spelling with no
    # legitimate use in a read-only command, so the segment is refused outright
    # rather than un-quoted here, which would mean reimplementing bash's rules.
    if guarded and _SHELL_EXPANSION_RE.search(segment):
        return "a shell expansion hides the real argument"

    # Pathname expansion cannot be refused wholesale: a glob usually IS the
    # argument — `ls *.py`, `grep -rn TODO src/*` — so the question is whether
    # THIS pattern can reach a word this module decides on. `_glob_hides_word`
    # answers it with `fnmatch`, which is what keeps the ordinary forms passing:
    #
    #     git remote s?t-url origin https://evil   (a file named `set-url` nearby)
    #     git diff --outp?t=/tmp/pwned
    #     cat f | sort ?o victim                   (a file named `-o` nearby)
    #
    # The last one is why a leading-dash test is not enough. `?o` does not start
    # with `-`, so a test keyed on the spelling skipped it, bash resolved it to
    # `-o`, and `sort` truncated `victim` under an auto-approval.
    if guarded:
        has_short_flags = verb in _SHORT_FLAG_VERBS or (
            verb == "git" and bool(args) and args[0].lower() in _GIT_REF_WRITE_FLAGS
        )
        for token in args:
            if _glob_hides_word(token, has_short_flags):
                return "a glob could expand into a flag or subcommand"

    # The allowlist names `git <subcommand>`, so that is the unit to key on.
    key = verb
    if verb == "git" and args:
        subcommand = args[0].lower()
        key = f"git {subcommand}"
        args = args[1:]

        if subcommand in _GIT_REF_WRITE_FLAGS:
            hit = _matched_flag(args, _GIT_REF_WRITE_FLAGS[subcommand])
            if hit:
                return f"'git {subcommand} {hit}' changes a ref"
            # A bare operand names a ref to create, unless the command is in list
            # mode (where it is a pattern) or the operand is a required flag value.
            #
            # List mode is decided over the whole argument list, because the
            # selecting flag can come after the operand it makes into a pattern:
            # `git branch newbranch --list` is still a list. It must stop at `--`,
            # though: after the terminator a word spelled like a flag is an
            # operand, so `git tag -- --list` CREATES the ref `--list` while
            # reading that `--list` as list mode passed it off as a read.
            options = args[: args.index("--")] if "--" in args else args
            list_shorts = _GIT_REF_LIST_SHORTS.get(subcommand, "")
            # A required flag's VALUE is not an option, however it is spelled. git
            # takes it from the following word, so `git branch --format -l newbranch`
            # hands `-l` to `--format` and never sees a list flag — while scanning
            # every token read that `-l` as one, and the bare operand it then
            # licensed created the branch. The walk below already tracks this for
            # operands; list mode has to track it too, over the same tokens.
            selectors: list[str] = []
            consumed = ""
            for tok in options:
                if _consumes_next_word(consumed):
                    # A glob HERE decides by COUNT, not by what it becomes: under
                    # `nullglob` an unmatched pattern vanishes, so the flag eats the
                    # NEXT word instead and every later position shifts by one.
                    # `git branch --format nomatch* --list newbranch` loses the
                    # format's value, `--format` takes `--list`, and `newbranch`
                    # stops being a pattern. See `_glob_shifts_arguments`.
                    if _glob_shifts_arguments(tok):
                        return "a glob in a required option's value shifts the arguments"
                    consumed = ""
                    continue
                if tok.startswith("-"):
                    # An ATTACHED value takes nothing from the next word.
                    consumed = "" if "=" in tok else tok
                    selectors.append(tok)
                    continue
                consumed = ""
            list_mode = any(
                tok.split("=", 1)[0] in _GIT_REF_LIST_FLAGS
                or (
                    len(tok) > 1
                    and tok[0] == "-"
                    and tok[1] != "-"
                    # EVERY character of the cluster must be a list letter or a
                    # digit, not merely one of them. `any` reads an attached VALUE
                    # as part of the cluster, which is the same trap the note on
                    # the accept-list registry records for `date -Iseconds`: the `l` in
                    # `git tag -ulin@kiro.co` selects list mode and the bare
                    # operand it then licenses creates a signed tag. A digit is
                    # allowed because `-n` carries an optional count (`-n5`).
                    #
                    # A MIXED cluster (`-lv`) is NOT a listing here and falls
                    # through to the prompt. That is the intended trade: the letter
                    # this cannot distinguish from a value is exactly the letter a
                    # write flag arrives on, and the ordinary spellings — a separate
                    # `-l`, `--list`, or `-n5` — are unaffected.
                    and all(ch in list_shorts or ch.isdigit() for ch in tok[1:])
                )
                for tok in selectors
            )
            # A `--no-list` anywhere in the span undoes it, and the operand it was
            # protecting becomes a ref to create. Applied AFTER the scan and
            # unconditionally, rather than as git's last-wins: cancelling can only
            # move an operand toward the prompt, so being coarse here is the safe
            # direction. See `_GIT_REF_LIST_CANCEL_FLAGS` for what was measured.
            if list_mode and any(_cancels_list_mode(tok, subcommand) for tok in selectors):
                list_mode = False
            previous = ""
            operand_only = False
            for tok in args:
                # `--` ends the options. Everything after it is an operand, however
                # it is spelled: `git tag -- -z` creates the tag `-z`, while a
                # leading-dash test read it as one more option and passed. A SECOND
                # `--` is itself an operand, so the terminator is consumed once.
                if tok == "--" and not operand_only:
                    operand_only = True
                    previous = ""
                    continue
                if not operand_only and tok.startswith("-"):
                    # An ATTACHED value (`--sort=x`) takes nothing from the next
                    # word, so it must not mark the following operand as consumed.
                    previous = "" if "=" in tok else tok
                    continue
                if _consumes_next_word(previous):
                    previous = ""
                    continue
                if list_mode:
                    continue
                return f"'git {subcommand} {tok}' creates a ref"

        if subcommand == "remote":
            # `git remote -v set-url …` puts an option BEFORE the subcommand, and
            # git accepts it there. Keying on `args[0]` therefore saw `-v` and let
            # the mutation through, so the leading options are skipped and the
            # first non-option word is the subcommand — the same token git uses.
            for tok in args:
                if tok.startswith("-"):
                    continue
                if tok in _GIT_REMOTE_WRITE_SUBCOMMANDS:
                    return f"'git remote {tok}' rewrites remote configuration"
                # A glob HERE, even one that reaches no decided word, because this
                # loop stops at the first non-option token and `nullglob` can make
                # a token VANISH: with it exported, `git remote nomatch* set-url
                # origin …` loses `nomatch*` entirely and git receives `set-url` —
                # while this loop broke on the pattern and never looked further.
                #
                # `_glob_hides_word` above cannot cover it: that test asks whether
                # the pattern can EXPAND INTO a decided word, and `nomatch*` cannot
                # — the mutation comes from the token disappearing, not from what it
                # becomes. Removing this check on the grounds that the general test
                # subsumed it is what opened the hole.
                if _GLOB_META_RE.search(tok) or _EXTGLOB_RE.search(tok):
                    return "a glob in the subcommand hides the real argument"
                # Likewise an expansion: `guarded` refuses those for `git` before
                # this point, so reaching here with one is impossible — but the
                # subcommand position is load-bearing enough to state rather than
                # infer.
                if _SHELL_EXPANSION_RE.search(tok):
                    return "a shell expansion hides the real argument"
                break

    hit = _matched_flag(args, _WRITE_FLAGS.get(key, ()))
    if hit:
        return f"'{key} {hit}' writes a file"
    hit = _matched_flag(args, _EXEC_FLAGS.get(key, ()))
    if hit:
        return f"'{key} {hit}' runs a program named by the repository"

    hit = _matched_flag(args, _INDIRECT_LIST_FLAGS_BY_PREFIX.get(key, ()))
    if hit:
        return f"'{key} {hit}' reads paths named inside a file, which this check cannot see"

    # A `+` argument to a pager is a string in the pager's own command language,
    # not an option, and that language reaches a shell. A glob is refused here too:
    # the shell has not produced the real spelling yet, so `less +*` could resolve
    # against a file named `+!cmd` and nothing downstream would see the `+`.
    if verb in _PAGER_STARTUP_VERBS:
        for token in args:
            if token.startswith("+"):
                return f"'{verb} {token}' runs a pager startup command, which reaches a shell"
            if _GLOB_META_RE.search(token) or _EXTGLOB_RE.search(token):
                return f"a glob in a '{verb}' argument could expand into a startup command"

    # `uniq INPUT OUTPUT` writes its second operand. `--` ends the options here
    # too, so a word after it is an operand however it is spelled:
    # `uniq -- input -pwned` writes `-pwned`, while a leading-dash test counted
    # one operand and passed the segment as a read.
    if verb == "uniq":
        operands = _operands(args)
        # Counting the tokens is only sound if each one stays ONE word. A glob
        # here decides by count: with `in1` and `in2` present, `uniq in*` runs
        # `uniq in1 in2`, and the second operand is the OUTPUT file — so a single
        # pattern passed a segment that truncates a file. `uniq`'s operands are
        # positional, which is what makes this different from `ls *.py`.
        if any(_glob_shifts_arguments(tok) for tok in operands):
            return "a glob in a 'uniq' operand can expand into a second operand, which it writes"
        if len(operands) > 1:
            return "'uniq INPUT OUTPUT' writes its second operand"

    # Tools whose read-only option surface is enumerated POSITIVELY. Deny-by-default:
    # an option has to be recognised as a read before it passes, so a spelling nobody
    # thought of prompts instead of being admitted. This is what a per-tool write-flag
    # denylist could not give us on these four -- see the note above the registry for
    # the six distinct `sort` spellings a denylist has to enumerate.
    if verb in _OPTION_ACCEPT_LISTS:
        violation = _option_accept_list_violation(verb, args)
        if violation:
            return violation

    return ""


def _classify_bash(cmd: str) -> str:
    """Single source of truth for read-only bash classification.

    Returns "" when the command is read-only, otherwise a human-readable
    reason it was rejected. :func:`is_read_only_bash` and
    :func:`unsafe_bash_reason` both delegate here so the two can never
    diverge — the invariant "reason is non-empty iff not read-only" holds
    by construction rather than by parallel maintenance. Deny-by-default.
    """
    if not cmd.strip():
        return "empty command"
    # Strip discard-only redirects (output sinks / stderr-merge) before the
    # unsafe-shell check; they are read-only but contain '>' / '&'.
    scrubbed = _DEVNULL_REDIR_RE.sub(" ", cmd)
    if _UNSAFE_SHELL_RE.search(scrubbed):
        return "unsafe shell pattern (redirect, command/process substitution, or backgrounding)"
    parts = re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd.strip())
    for part in parts:
        if not part.strip():
            continue
        pipe_parts = [p.strip() for p in part.split("|") if p.strip()]
        if not pipe_parts:
            return "unsafe shell pattern"
        # The verb is compared case-insensitively, but the side-effect check
        # below needs the original spelling: flags are case-sensitive, and the
        # two cases can mean opposite things (`file -C` compiles a magic file,
        # `file -c` only prints one).
        head = pipe_parts[0].strip()
        first = head.lower()
        if not any(first == p or first.startswith(p + " ") for p in _READ_ONLY_BASH_PREFIXES):
            base = first.split()[0] if first.split() else first
            return f"command '{base}' is not on the read-only allowlist"
        # Clearing the allowlist only settles which program runs. The rest of
        # the command line can still write a file, change a ref or start
        # another program.
        side_effect = _side_effect_reason(head)
        if side_effect:
            return f"not read-only: {side_effect}"
        for target in pipe_parts[1:]:
            matched = _READ_ONLY_PIPE_RE.match(target)
            if not matched:
                tgt = target.split()[0] if target.split() else target
                return f"pipe target '{tgt}' is not a read-only filter"
            # The name the allowlist matched must be the program bash actually
            # runs. `_READ_ONLY_PIPE_RE` ends its filter name at a `\b`, and `$`
            # satisfies that, so `sort$IFS-o victim` matched the entry `sort` while
            # bash split `$IFS` into whitespace and ran `sort -o victim`. Nothing
            # downstream recovered: `_side_effect_reason` reads the verb as
            # `sort$ifs-o`, finds no table for it, and returns "".
            #
            # The leading segment of a pipeline was never exposed to this, because
            # its allowlist test pins the boundary to a literal space
            # (`first == p or first.startswith(p + " ")`). This makes the pipe
            # allowlist say the same thing: the first argv word, exactly.
            try:
                target_tokens = shlex.split(target)
            except ValueError:
                return "pipe target quoting cannot be resolved"
            if not target_tokens or target_tokens[0] != matched.group(1):
                tgt = target_tokens[0] if target_tokens else target
                return (
                    f"pipe target '{tgt}' is not the read-only filter "
                    f"'{matched.group(1)}' it matched"
                )
            # The pipe allowlist matches only the leading verb, so a filter's
            # own output flag (`sort -o FILE`) needs the same check.
            side_effect = _side_effect_reason(target)
            if side_effect:
                return f"pipe target is not read-only: {side_effect}"
    return ""


def is_read_only_bash(cmd: str) -> bool:
    """Check if a bash command is read-only. Deny-by-default."""
    return _classify_bash(cmd) == ""


def unsafe_bash_reason(cmd: str) -> str:
    """Human-readable reason a bash command failed read-only classification.

    Makes rejection messages specific ("unsafe shell pattern …") instead of
    the generic adapter default ("User refused permission to run tool").
    Returns "" when the command IS read-only (no reason to reject on
    safety grounds).
    """
    return _classify_bash(cmd)
