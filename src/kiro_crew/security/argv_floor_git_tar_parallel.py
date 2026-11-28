"""GNU parallel replacement readings for the git/tar self-target floor."""

from __future__ import annotations

import re

from .argv_floor_ssh import _dequoted, _unmask_separators

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

# The most words the floor reads from one command value or one git argv.  A
# command past it is refused unread, the way ``MAX_SCANNABLE_COMMAND_CHARS``
# refuses an oversized one: the gate runs on the event loop, and a 20 KiB
# command can still hold about 10,000 one-letter words.  No real git or tar
# argv, ssh command or ``ext::`` payload comes near it.
_GIT_TAR_WORD_CAP = 4096


def _unmasked(token: str) -> str:
    """*token* with the floor's quoted-separator sentinels restored, de-quoted."""
    return _dequoted(_unmask_separators(token).strip())


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
