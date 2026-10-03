"""Source-offset spans for the self-protection-kill refusal diagnostic.

A reader-facing companion to the argv-structural floor in :mod:`argv_floor`.
Nothing here changes a verdict: the floor has already decided a by-name
self-kill when these run. They only name WHICH tokens the floor keyed on --
the kill program and its target -- at their real offsets in the submitted
command, so the ``self-protection-kill`` refusal can bracket them without
echoing a byte of the command.

They live beside the floor rather than in it because the floor module is at
its per-module line cap: this is the cohesive, self-contained unit (a raw
word-span splitter and the index alignment that reads the floor's own token
walk) that splits out cleanly. The alignment leans on a handful of the floor's
structural predicates, reached through the :mod:`argv_floor` module object so a
test seam that patches one of them on that module is still observed here.
"""

from __future__ import annotations

from . import shell_normalizer as _shell_normalizer

#: Word-break characters for the RAW span splitter: unquoted whitespace is
#: handled separately, these are the operator/separator characters bash ends a
#: word on. Mirrors ``_shell_normalizer._SHELL_WORD_BREAK`` minus the space set.
_SHELL_WORD_BREAK_RAW = frozenset(";&|()<>`")


def _raw_word_spans(source: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each shell WORD in *source*, at its real offset.

    Splits on UNQUOTED whitespace and the operator/separator characters bash
    breaks a word on, walking the shared quote/escape state machine so a quoted
    or escaped separator stays inside its word (``pkill -f '[;]*kirocrew'`` is
    three words, not four). An unquoted run of operator characters (``;``,
    ``&&``, ``|``) coalesces into one span, mirroring how the token walk keeps a
    separator as one token -- so the span list aligns index-for-index with the
    top-level frame for the common command shapes.

    An ACTIVE newline gets its own one-character span, because the token walk
    turns a top-level newline into a ``;`` separator TOKEN
    (:func:`argv_floor._self_tokens`): without a span for it the two lists would
    be shifted by one on any multi-line command, and -- paired with a redirect
    that the walk keeps as one token but the raw split breaks in two -- the two
    shifts could cancel so the counts still matched while the lists were
    misaligned. Emitting the newline span removes one half of that cancellation;
    :func:`_self_kill_token_spans` re-checks each paired slice to catch the rest.

    It does NOT resolve quoting, ``$VAR`` or ``~``: these are RAW source offsets,
    so the region a reader sees is the bytes they actually hold, even when the
    token walk later resolved that word to something else (``"$C"/gateway/*``).
    """
    spans: list[tuple[int, int]] = []
    start = -1
    prev_was_operator = False
    for step in _shell_normalizer._iter_shell_chars(source):
        off = step.offset
        breaks = step.active and (step.char.isspace() or step.char in _SHELL_WORD_BREAK_RAW)
        if not breaks:
            if start < 0:
                start = off
            continue
        if start >= 0:
            spans.append((start, off))
            start = -1
            prev_was_operator = False
        # An active newline is a separator the token walk turns into a ``;``
        # token, so it needs its own span to keep the two lists aligned by
        # index. It is whitespace to the operator-coalescing rule below (a run
        # of ``;``/``\n`` must not fuse into one span the way ``&&`` does), so it
        # is handled here, before that rule, and resets the operator run.
        if step.active and step.char == "\n":
            spans.append((off, off + len(step.text)))
            prev_was_operator = False
            continue
        # An unquoted operator character is a separator word of its own; a run of
        # them (``&&``, ``||``, ``;;``) coalesces into one span, but an operator
        # that merely abuts a preceding WORD (``export;``) stays its own span.
        if not step.char.isspace():
            if prev_was_operator and spans and spans[-1][1] == off:
                spans[-1] = (spans[-1][0], off + len(step.text))
            else:
                spans.append((off, off + len(step.text)))
            prev_was_operator = True
        else:
            prev_was_operator = False
    if start >= 0:
        spans.append((start, len(source)))
    return spans


def _self_kill_token_spans(text_lower: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The ``(program, target)`` source offsets a by-name self-kill keyed on.

    Returns the offsets of the ``pkill``/``killall`` program token and of the
    argument carrying the product name, both as ``(start, end)`` indices into
    *text_lower* -- so the ``self-protection-kill`` refusal diagnostic can name
    which token was read as the kill program and which as the target, without
    echoing any byte of the command.

    Scope is the FIRST (by-name) leg of :func:`argv_floor._is_self_kill`,
    mirrored token for token so the two never disagree about what fired, and only
    the TOP-LEVEL frame (``source == text_lower``, i.e. the frame the outer
    offsets belong to): a nested-payload frame's tokens live inside one token of
    the outer command, so their offsets are not the outer command's, and those
    keep the whole-command span. Raw word spans are lined up with the frame's
    tokens BY INDEX, but equal word COUNTS are NOT a proof that the two lists
    align: the raw splitter and the floor's token walk break words by different
    rules (an active newline is a ``;`` token in the walk but was dropped by the
    raw split until the newline span was added; a redirect such as ``>out`` is
    one token in the walk but two raw words), and two such differences can CANCEL
    so the counts can match while the slices are shifted. On
    ``true`` + newline + ``pkill -f <name> >out`` such a shift would name ``-f``
    the program and ``>`` the target -- the exact misdiagnosis this module
    exists to prevent. The newline span closes one half of that cancellation
    (the counts for those two shapes differ, so they fall through here). Equal
    counts can still hide a shift from some other pair of divergences, so
    before a pair is returned each picked slice is RE-TOKENISED through the same
    word split (:func:`shell_normalizer._self_tokens`) and must come back as a
    SINGLE shell word, with the program slice's word itself a by-name kill
    program just as ``tokens[i]`` is. A shifted pair fails that -- the slice at
    the program index is a flag like ``-f``, not a kill program. The check reads
    the resolved WORD, not its raw bytes, so a ``$VAR``-resolved target still
    passes: ``"$C"/gateway/*`` is the same word the floor keyed on, only the
    expansion differs. On any disagreement this returns ``None`` and the caller
    keeps the plain whole-command span -- a mismatch costs precision (a
    fallback), never correctness (a misnamed token). When the word counts
    disagree outright (an empty-quote collapse merged a word) the index alignment
    is not even attempted, for the same reason. The bare-``kill`` leg aims
    through a substitution BODY rather than one argv token, so it too has no pair
    to point
    at.

    The floor's structural predicates are reached through the :mod:`argv_floor`
    module object (imported lazily to keep the dependency one-way) so a test that
    patches one of them on that module is observed here too.
    """
    from . import argv_floor as _argv_floor

    if not _argv_floor._self_floor_can_fire(text_lower):
        return None
    raw_spans = _raw_word_spans(text_lower)
    for source, tokens in _argv_floor._shell_payload_walk(text_lower):
        # Only the top-level frame's word offsets index the submitted command,
        # and only when the raw split and the resolved frame agree on word count.
        if source != text_lower or len(raw_spans) != len(tokens):
            continue
        programs = _argv_floor._argv_programs(tokens)
        disqualified: bool | None = None
        for i, token in enumerate(tokens):
            if not _argv_floor._is_kill_by_name_program(token):
                continue
            if disqualified is None:
                disqualified = _shell_normalizer._data_consumer_command_disqualified(tokens)
            if _argv_floor._data_consumer_exempt(
                i, token, programs, tokens, command_disqualified=disqualified
            ):
                continue
            depth = 0
            for j, arg in enumerate(tokens[i + 1 :], start=i + 1):
                if _argv_floor._SELF_NAME_RE.search(
                    _argv_floor._debracket(arg)
                ) or _argv_floor._SELF_NAME_RE.search(_shell_normalizer._normalize_operand(arg)):
                    # Equal counts are not alignment (an active-newline ``;`` and
                    # a split redirect can cancel in the count while shifting the
                    # lists), so verify the picked slices before trusting them.
                    # Each raw slice must re-tokenise -- through the same word
                    # split the walk used -- to a SINGLE shell word, and the
                    # program slice's word must itself be a by-name kill program,
                    # exactly as ``tokens[i]`` is. A shifted pair fails this: the
                    # slice landing on the program index is a flag like ``-f``,
                    # not a kill program. The check reads the word, not its bytes,
                    # so a ``$VAR``-resolved target (raw ``"$C"/gateway/*``,
                    # resolved ``/home/.../gateway/*``) still passes -- same word,
                    # only the expansion differs. On any disagreement this returns
                    # ``None`` and the caller keeps the whole-command span.
                    prog_words = _shell_normalizer._self_tokens(
                        text_lower[raw_spans[i][0] : raw_spans[i][1]]
                    )
                    target_words = _shell_normalizer._self_tokens(
                        text_lower[raw_spans[j][0] : raw_spans[j][1]]
                    )
                    if (
                        len(prog_words) != 1
                        or len(target_words) != 1
                        or not _argv_floor._is_kill_by_name_program(prog_words[0])
                    ):
                        return None
                    return raw_spans[i], raw_spans[j]
                depth += _argv_floor._substitution_depth_delta(arg)
                if depth <= 0 and _argv_floor._ends_argv(arg):
                    break
                depth = max(depth, 0)
    return None
