"""Read the direction of an ``aws s3 cp|mv|sync`` command, or refuse to read it.

The three ``credential-exfil-s3-*`` rows match ``<verb> .* s3://`` on the raw text,
so a download with a leading flag (``cp --recursive s3://b/p ./d``) reads as an
upload. A regex cannot split flags from operands the way bash and the aws CLI do:
a backslash-escaped space and a glued ``$IFS`` each make bash see words the regex
does not. So this module does not see through shell syntax at all. It
reads only a command of plain literal words, splits them as bash would, reads the
words with a fixed table of aws CLI flags, and answers True only when the one
source is ``s3://`` and the one destination is not. Anything else -- an expansion,
a glob, an escape, a separator, an unknown flag -- answers False and the row's
deny stands, so this can only narrow a row on a command it fully read.

It reads ``aws s3`` and nothing else: ``python -c``, ``curl`` and every other way
to move a file are out of its reach and stay with the rows and the OS sandbox.
"""

from __future__ import annotations

import shlex

from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES

_VERBS = frozenset({"cp", "mv", "sync"})
_S3_TRANSFER_PATTERNS: frozenset[str] = frozenset(
    rule.pattern
    for rule in BUILTIN_DENIED_RULES
    if rule.id in {f"credential-exfil-s3-{v}" for v in _VERBS}
)

#: Characters a word may hold outside quotes. No ``$``, backtick, ``\``, glob
#: (``*?[``), brace, ``#``, ``!``, operator or separator: each one is syntax.
#: ``~`` stays: bash expands it only to a local home path, never to ``s3://`` or a flag.
_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_./:@%+=,~-")
#: Refused anywhere, quoted or not. Inside double quotes ``$``, backtick and ``\``
#: are live; refusing them in single quotes too only over-refuses.
_NEVER = frozenset("$`\\!")
_BOOL, _VALUE, _EQ_ONLY = "bool", "value", "eq-only"
#: aws CLI flags this reader understands. Global options are accepted anywhere,
#: as the aws CLI does. A flag one verb rejects makes the aws CLI exit before any
#: transfer, so one table serves all three. ``_EQ_ONLY`` flags take an OPTIONAL
#: value (argparse ``nargs='?'``) and so would swallow the next word: only the
#: ``--flag=value`` spelling is read. Every flag not here refuses.
_FLAGS: dict[str, str] = {
    **dict.fromkeys(
        "--debug --no-verify-ssl --no-paginate --no-sign-request --no-cli-pager "
        "--recursive --quiet --dryrun --only-show-errors --no-progress --follow-symlinks "
        "--no-follow-symlinks --no-guess-mime-type --ignore-glacier-warnings "
        "--force-glacier-transfer --delete --exact-timestamps --size-only --no-overwrite".split(),
        _BOOL,
    ),
    **dict.fromkeys(
        "--profile --region --endpoint-url --output --query --ca-bundle --cli-read-timeout "
        "--cli-connect-timeout --color --cli-binary-format --include --exclude --page-size "
        "--source-region --checksum-mode --sse-c-key --expected-size".split(),
        _VALUE,
    ),
    **dict.fromkeys(("--sse-c", "--request-payer"), _EQ_ONLY),
}


def _plain_words(command: str) -> "list[str] | None":
    """The argv bash builds from *command*, or None when it is not plain literal words."""
    text = _shell._fold_line_continuations(command).strip(" \t\n")
    if not text or _NEVER.intersection(text):
        return None
    state = 0
    for step in _shell._iter_shell_chars(text):
        state = step.state
        if not " " <= step.char <= "~" and step.char != "\t":
            return None  # newline, control or non-ASCII: a separator or a lookalike
        if step.active and step.char not in _PLAIN and step.char not in " \t'\"":
            return None
    if state != 0:
        return None
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return None


def _skip_flag(words: "list[str]", i: int) -> "int | None":
    """Index after the flag at ``words[i]`` and its value, or None when it is not read."""
    name, eq, _value = words[i].partition("=")
    arity = _FLAGS.get(name)
    if arity is None or (eq and arity == _BOOL) or (not eq and arity == _EQ_ONLY):
        return None
    if eq or arity == _BOOL:
        return i + 1
    if i + 1 >= len(words) or words[i + 1].startswith("-"):
        return None
    return i + 2


def _s3_download_only(command: str, raw: str = "") -> bool:
    """Whether *command* is exactly one plain ``aws s3 cp|mv|sync s3://… <local>``.

    *raw* is the same command before case folding: KELVIN SIGN folds to ASCII ``k``.
    """
    if not (command.isascii() and raw.isascii()):
        return False
    words = _plain_words(command.lower())
    if words and words[0] == "running:":  # kiro-cli's title for a shell call
        words = words[1:]
    # A ``file://`` / ``fileb://`` value makes the aws CLI READ a local file and
    # send it (``--sse-c-key fileb://...`` goes out in a request header).
    if not words or words[0] != "aws" or any(("file://" in w or "fileb://" in w) for w in words):
        return False
    stage, operands, i = 0, [], 1  # stage 0 wants ``s3``, 1 the verb, 2 operands
    while i < len(words):
        word = words[i]
        if word.startswith("-") and word != "-":  # a bare ``-`` is stdin/stdout
            nxt = _skip_flag(words, i)
            if nxt is None:
                return False
            i = nxt
            continue
        if stage == 2:
            operands.append(word)
        elif (stage == 0 and word == "s3") or (stage == 1 and word in _VERBS):
            stage += 1
        else:
            return False
        i += 1
    return (
        stage == 2
        and len(operands) == 2
        and operands[0].startswith("s3://")
        and not operands[1].startswith("s3://")
    )
