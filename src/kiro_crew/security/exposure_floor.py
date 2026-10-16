"""Argv-structural floor for the ``network-exposure`` (tailscale) catalog rows.

The rows name ``tailscale`` subcommands that widen this machine's network
exposure (``serve``, ``funnel``, ``up``, ...). A text pattern cannot enforce them:
matched anywhere it refuses a ``grep`` or a commit message that merely names a
subcommand, and anchored it is bypassed by shell grammar the anchor does not model.

This floor is a thin mirror of ``argv_floor._matches_self_subcommand`` and reads
the same shared walk unchanged (``_shell_payload_walk``): the command and every
``$( )``, backtick, process-substitution, ``sh -c`` and ``eval`` payload, each
tokenized with quoting resolved, same-line literal assignments substituted. Like
that floor it is position-blind and fails closed: EVERY token whose basename is
``tailscale`` is a candidate unless ``_data_consumer_exempt`` says it is an
argument of a program that treats its arguments as data (``echo``, ``grep``,
``cat``, ...). So a glued separator, a leading redirect, a reserved word, or a
heredoc fed to a shell cannot move the program out of view. The price is the one
the other floors already pay: an argument of a program that is not a data
consumer (``git grep -e tailscale -e up``) is read as an invocation.

It is fed the command AS SUBMITTED, not lowercased, so the walk's case-sensitive
assignment resolution keeps ``ts`` and ``TS`` apart. Only the program name and
the operand words are lowercased, where they are compared.
"""

from __future__ import annotations

import re

from .argv_floor import _brace_expansions, _operands_lead_with
from .shell_normalizer import (
    _argv_programs,
    _data_consumer_command_disqualified,
    _data_consumer_exempt,
    _ends_argv,
    _glob_could_expand_to,
    _normalize_operand,
    _program_basename,
    _redirect_consumes_next,
    _shell_join_continuations,
    _shell_payload_walk,
)

_PROGRAM_NAMES = ("tailscale", "tailscale.exe")

#: The one global tailscale flag that takes a SEPARATE value (``--socket /path``).
_VALUE_FLAGS = frozenset({"--socket", "-socket"})

#: Necessary condition for the walk: the name in the text once quotes, backslashes
#: and continuations come off, or machinery that can assemble it (an expansion, a
#: substitution, a glob or brace, a printf escape). Anything else cannot fire.
_MACHINERY_RE = re.compile(r"[?*\[\]{}$`]|\\x[0-9a-f]|\\0?[0-7]{1,3}", re.IGNORECASE)
_QUOTE_JUNK_RE = re.compile(r"[\"'\\]")

#: Backstop on nested brace expansion of the verb; real verbs need one level.
_BRACE_DEPTH_CAP = 4

#: Leading subcommand words -> row id. A read is carved out in ``_verdict``.
_VERB_RULES: "tuple[tuple[tuple[object, ...], str], ...]" = (
    (("serve",), "network-exposure-tailscale-serve"),
    (("funnel",), "network-exposure-tailscale-funnel"),
    (
        (frozenset({"up", "set", "login", "logout", "switch", "cert", "web"}),),
        "network-exposure-tailscale-node-mutate",
    ),
    (("drive",), "network-exposure-tailscale-drive"),
    (("file", "cp"), "network-exposure-tailscale-file-cp"),
)
_ALL_RULES: "tuple[str, ...]" = tuple(rule for _spec, rule in _VERB_RULES)

#: Reads: the word right after the verb that makes each subtree a report. A read
#: is the NEXT word, never a flag found anywhere later in the argv: a scan for one
#: crosses a glued ``&&`` into the next command (``…&&echo --help``).
_READS: dict[str, frozenset[str]] = {
    "serve": frozenset({"status", "get-config"}),
    "funnel": frozenset({"status"}),
    "drive": frozenset({"list"}),
    "switch": frozenset({"--list"}),
}


def _can_fire(text: str) -> bool:
    if _MACHINERY_RE.search(text):
        return True
    return "tailscale" in _QUOTE_JUNK_RE.sub("", _shell_join_continuations(text)).lower()


def _words(tokens: "list[str]", index: int) -> "tuple[list[str], str]":
    """(operands, the word right after the verb) for the program at *index*.

    Operands are the non-flag words, lowercased, to the argv's end. The word after
    the verb is taken RAW in position (a flag counts), because a read is that word
    and only that word.
    """
    operands: list[str] = []
    after_verb = ""
    verb_seen = False
    skip = False
    for later in tokens[index + 1 :]:
        is_redirect, expects_target = _redirect_consumes_next(later)
        # A redirect glued to a word (``up>/tmp/log``) still leaves the word.
        glued = re.split(r"[<>]", later, maxsplit=1)[0] if is_redirect else ""
        if glued.rstrip("&").isdigit():
            glued = ""
        if skip:
            skip = False
        elif is_redirect and not glued:
            skip = expects_target
        else:
            word = _normalize_operand(glued or later).lower()
            skip = expects_target if glued else False
            if verb_seen and not after_verb and word:
                after_verb = word
            if word.startswith("-"):
                skip = skip or (not verb_seen and word in _VALUE_FLAGS)
            elif word:
                operands.append(word)
                verb_seen = True
        if _ends_argv(later):
            break
    return operands, after_verb


def _verdict(operands: "list[str]", after_verb: str) -> "str | None":
    """The row id an argv's operands fall under, or None for a read.

    There is no ``--help`` carve-out: a mutating verb with a help flag is refused,
    as the self-protection floor refuses ``kirocrew restart --help``.
    """
    if not operands:
        return None
    if after_verb in _READS.get(operands[0], frozenset()):
        return None
    for spec, rule in _VERB_RULES:
        if _operands_lead_with(operands, spec):
            return rule
    return None


def _verdicts(operands: "list[str]", after_verb: str, depth: int = 0) -> "list[str]":
    """Every row id the operands can fall under, brace-expanding the first two.

    ``tailscale {funnel,} 3000`` and ``tailscale file {cp,} x peer:`` run the
    verb bash expands to (an empty alternative is dropped). A word the expansion
    leaves unchanged (``'{status}'``) is a literal operand. An expansion that
    cannot be computed (over the alternatives cap) or nests past the depth cap
    fails CLOSED: every row is a candidate.
    """
    for position in (0, 1):
        word = operands[position] if len(operands) > position else ""
        if "{" not in word:
            continue
        alternatives = _brace_expansions(word)
        if alternatives is None or depth >= _BRACE_DEPTH_CAP:
            return list(_ALL_RULES)
        if alternatives == [word]:
            continue
        out: list[str] = []
        for alt in alternatives:
            expanded = operands[:position] + ([alt] if alt else []) + operands[position + 1 :]
            out.extend(_verdicts(expanded, after_verb, depth + 1))
        return out
    rule = _verdict(operands, after_verb)
    return [rule] if rule else []


def _tailscale_exposure_rule(
    text: str, enabled: "frozenset[str] | set[str] | None" = None
) -> "str | None":
    """The first ENABLED ``network-exposure`` row id *text* (the RAW command) runs into.

    *enabled* is the set of row ids still in force (None: all). The scan continues
    past a match on a disabled row, so ``tailscale serve; tailscale funnel 3000``
    with only ``serve`` disabled is still refused for the funnel.
    """
    if not _can_fire(text):
        return None
    for _source, tokens in _shell_payload_walk(text):
        programs = _argv_programs(tokens)
        disqualified: "bool | None" = None
        for index, token in enumerate(tokens):
            # Split on both separators: a quoted Windows path keeps its backslashes.
            base = re.split(r"[\\/]", _program_basename(token).lower())[-1]
            if base not in _PROGRAM_NAMES and not _glob_could_expand_to(base, _PROGRAM_NAMES):
                continue
            if disqualified is None:
                disqualified = _data_consumer_command_disqualified(tokens)
            if _data_consumer_exempt(
                index, token, programs, tokens, command_disqualified=disqualified
            ):
                continue
            for rule in _verdicts(*_words(tokens, index)):
                if enabled is None or rule in enabled:
                    return rule
    return None
