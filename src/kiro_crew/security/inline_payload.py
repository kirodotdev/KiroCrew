"""Static mint-name checks for inline Python; payloads are never executed.

The argv floor chooses the payload. This module folds literal concatenation,
reads call boundaries with Python's tokenizer, and matches explicit mint names.
It is a heuristic, not an interpreter or an OS credential boundary. In particular,
.local_secret is readable inside the sandbox; its explicit readers stay protected.
"""

from __future__ import annotations

import ast
import base64
import binascii
import io
import re
import token
import tokenize

from .shell_normalizer import (
    _NESTED_SHELL_PROGRAMS,
    _PYTHON_PROGRAM_RE,
    _program_basename,
    _shell_tokens,
)
from .vocabulary import _SELF_NAME_RE

# A cheap trigger, not a verdict. Include stdlib base64 aliases before the floor
# short-circuits, without treating arbitrary eval/getattr calls as minting.
_INLINE_DYNAMIC_EXEC_RE = re.compile(
    r"\b__import__\s*\(|\bimportlib\b|\bimport_module\b|\brunpy\b|\brun_module\b|"
    r"\brun_path\b|\bexec\s*\(|\beval\s*\(|\bcompile\s*\(|"
    r"\b(?:(?:standard_|urlsafe_)?b64decode|decodebytes)\b|\bmarshal\b|\bgetattr\s*\("
)
# Keep explicit dispatch and token-producer paths, including stdin file carriers.
# Secret is not a path word: handlers/secrets.py is ordinary patch-script data.
# A package path is spelled with ``.`` as a module, ``/`` as a POSIX path, or
# ``\`` as a Windows path; the three separators are one class, not three rules.
_MINT_SURFACE_RE = re.compile(
    r"kiro_crew[./\\](?:cli|cli_server|__main__|_bootstrap)(?![a-z0-9_])"
    r"|kiro_crew[\w./\\]*token(?!iz)"
    r"|from\s+kiro_crew\s+import\b[^;]{0,120}?(?<![a-z0-9_.-])(?:cli|cli_server|__main__|_bootstrap)(?![a-z0-9_])"
)
# A simple statement begins at the start of input, after ``;``, after a newline,
# or after the ``:`` that closes a compound header (``if x:``, ``for``, ``try:``,
# ``def f():``). Python's grammar has exactly those four, so the class is closed.
_PRODUCT_IMPORT_RE = re.compile(
    r"(?:^|[;\n:])\s*(?:from\s+kiro_crew(?:\.[\w.]*)?\s+import\b"
    r"|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*kiro_crew\b)"
)
# Only consulted INSIDE a loader argument, never beside an unrelated loader.
# Nested compile string arguments need this anchor even without recursive scans.
_LOADED_IMPORT_RE = re.compile(
    r"""["']\s*(?:from\s+kiro_crew(?:\.[\w.]*)?\s+import\b"""
    r"|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*kiro_crew\b)"
)
#: The callables the gates read, by class. A call is judged by which of these it
#: RESOLVES to, not by the word written at the call, so a new way of spelling the
#: same callable is a binding rule rather than another special case per gate.
_DYNAMIC_RUNNER_NAMES = frozenset({"run_module", "run_path", "import_module", "__import__"})
_INLINE_CODE_LOADER_NAMES = frozenset({"exec", "eval", "compile", "run_path", "execfile"})
_B64_DECODER_NAMES = frozenset(
    {"b64decode", "standard_b64decode", "urlsafe_b64decode", "decodebytes"}
)
_RESOLVABLE_NAMES = _DYNAMIC_RUNNER_NAMES | _INLINE_CODE_LOADER_NAMES | _B64_DECODER_NAMES
#: Which names a ``from <module> import`` may bind, per module. Scoped so an
#: unrelated ``from mypkg import compile`` is not read as the builtin -- the
#: precision matters here, because every name resolved is a denial made possible.
_CALLABLE_MODULES = {
    "base64": _B64_DECODER_NAMES,
    "runpy": frozenset({"run_module", "run_path"}),
    "importlib": frozenset({"import_module"}),
    "builtins": _INLINE_CODE_LOADER_NAMES | frozenset({"__import__"}),
}
_GETATTR_CALL_RE = re.compile(r"(?<![a-z0-9_])getattr\s*\(")
_ATTRIBUTE_NAME_LITERAL_RE = re.compile(r"""(["'])([A-Za-z_]\w*)\1""")
#: ``m.__dict__["exec"]`` names a callable through a subscript, which is not a
#: call and so never reaches the call index. The literal shape is simple enough
#: to read directly; a subscript by a computed key resolves to nothing.
_ATTRIBUTE_SUBSCRIPT_RE = re.compile(r"""__dict__\s*\[\s*(["'])([A-Za-z_]\w*)\1\s*\]""")
#: A whole dotted head, so only its LAST component names the callable:
#: ``runpy.run_module`` resolves, ``run_module.thing`` does not.
_DOTTED_HEAD_RE = re.compile(r"\A(?:[A-Za-z_]\w*\s*\.\s*)*([A-Za-z_]\w*)\Z")
_ALIAS_BINDING_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*\Z")
#: Matched with an explicit position, so it carries no string-start anchor:
#: ``\\A`` means offset zero even when ``match`` is given one, which reads every
#: call after the first as nameless.
_CALL_NAME_RE = re.compile(r"([A-Za-z_]\w*)")
_PACKAGE_LITERAL_RE = re.compile(r"(?<![a-z0-9_./\\-])kiro_crew(?![a-z0-9_/\\-])")
_CONSOLE_SCRIPT_LITERAL_RE = re.compile(
    r"(?<![a-z0-9_.-])kiro[-.]?crew(?:\.exe)?(?![a-z0-9_./\\-])"
)
_LOADER_ARG_DELIMITERS_RE = re.compile(
    r"""^\s*(?:[rbfu]{1,2})?(?:\"\"\"|'''|[\"'])|(?:\"\"\"|'''|[\"'])\s*$"""
)
_MINT_VERB_RE = re.compile(r"(?<![a-z0-9])(?:token|secret)(?![a-z0-9])")
_PLAIN_LITERAL_RE = re.compile(r"""(["'])([^"'\\\n]*)\1""")
#: A decoder's input after folding. Surrounding parentheses are accepted because
#: a constant span excludes them: ``b64decode(('a' 'b'))`` folds to a literal
#: still inside the call's own parens, and the group is balanced by the parse.
_INLINE_B64_LITERAL_RE = re.compile(
    r"""\s*\(*\s*(?:[bBrRuU]{1,2})?(["'])([A-Za-z0-9+/=_-]{8,})\1\s*\)*\s*"""
)
#: The cheap precondition for any decode: a decoder has to be NAMED somewhere,
#: whether it is called directly or bound to an alias first. Gating on a base64
#: literal instead made the fast path depend on how the bytes were written, so a
#: payload spelled in short enough pieces was skipped before the fold could run.
_B64_DECODER_NAME_RE = re.compile(r"\b(?:standard_|urlsafe_)?b64decode\b|\bdecodebytes\b")
_B64_INPUT_KEYWORD_RE = re.compile(r"\s*([a-zA-Z_]\w*)\s*=\s*(?!=)")
_B64_MODULE_QUALIFIER_RE = re.compile(r"(?:[a-zA-Z_]\w*\.)+")
_PY_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n[ \t]*")
#: How many times a payload's own quotes can survive shell splitting: its own word,
#: and one nested shell command (``bash -c '… python -c …'``). Deeper nesting is a
#: residual, not a silent claim -- an unreachable carrier is noted, never covered.
_SHELL_QUOTE_LEVELS = 2


def _lex(view: str):
    """Yield Python tokens with absolute offsets, retaining a malformed prefix.

    Python's lexer owns escapes, triple quotes and comments; a shell quote walk
    cannot substitute for its different string rules. No AST or execution occurs.
    """
    offsets = [0]
    for line in view.split("\n"):
        offsets.append(offsets[-1] + len(line) + 1)
    try:
        for item in tokenize.generate_tokens(io.StringIO(view).readline):
            yield item, offsets[item.start[0] - 1] + item.start[1], offsets[
                item.end[0] - 1
            ] + item.end[1]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return


def _constant_text(node: ast.AST) -> str | None:
    """The value of a constant string/bytes expression, or ``None`` when computed.

    The case list here is the GRAMMAR's, not a list of spellings. Python's parser
    has already merged implicit concatenation and every literal prefix (``b``,
    ``r``, ``u``, ``rb``) into a single :class:`ast.Constant`, so exactly two node
    shapes can carry a constant string: that constant, and ``+`` over two of them.
    Parentheses are not nodes, so a grouped literal folds for free. Anything else
    -- an f-string, ``"".join(...)``, a slice, ``bytes.fromhex``, a name -- is
    COMPUTED and resolves to nothing, the same residual a callable named at run
    time gets. Closing the fold over node shapes is what keeps a new way of
    writing the same bytes from being a new rule.

    Bytes decode as latin-1: it maps every byte without raising, and the alphabets
    this fold feeds (base64, dotted module names) are ASCII either way.

    No piece count or chain-depth limit: total work is bounded by the payload's
    own length, and nesting deep enough to matter is refused by the parse in
    :func:`_constant_spans` rather than by a number here a payload could pad up
    to. A cap on the chain would hand a payload a way to stay unfolded by being
    long, which is the property the fold exists to remove.
    """
    stack: list[ast.AST] = [node]
    pieces: list[str] = []
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Constant):
            if isinstance(current.value, str):
                pieces.append(current.value)
                continue
            if isinstance(current.value, (bytes, bytearray)):
                pieces.append(bytes(current.value).decode("latin-1"))
                continue
            return None
        if isinstance(current, ast.BinOp) and isinstance(current.op, ast.Add):
            # Pushed right-then-left so the pop order is source order.
            stack.append(current.right)
            stack.append(current.left)
            continue
        return None
    return "".join(pieces)


def _line_starts(raw: bytes) -> list[int]:
    """Byte offset of each line start; ``ast`` columns are utf-8 byte offsets."""
    starts = [0]
    for line in raw.split(b"\n"):
        starts.append(starts[-1] + len(line) + 1)
    return starts


def _constant_spans(view: str) -> list[tuple[int, int, str]]:
    """``(byte start, byte end, value)`` for each OUTERMOST constant string.

    A folded span is not descended into, so a literal inside an already-folded
    ``+`` chain is never rewritten twice. Traversal is an explicit stack: a
    payload picks its own nesting depth, and recursion here would raise instead
    of judging.
    """
    try:
        tree = ast.parse(view)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    raw = view.encode("utf-8")
    starts = _line_starts(raw)
    spans: list[tuple[int, int, str]] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Constant, ast.BinOp)):
            text = _constant_text(node)
            end_line, end_column = node.end_lineno, node.end_col_offset
            if text is not None and end_line is not None and end_column is not None:
                spans.append(
                    (
                        starts[node.lineno - 1] + node.col_offset,
                        starts[end_line - 1] + end_column,
                        text,
                    )
                )
                continue
        stack.extend(ast.iter_child_nodes(node))
    spans.sort()
    return spans


def _fold_inline_literals(payload: str) -> str:
    """Replace every constant string expression with its own value.

    The fold runs on the AST when the payload parses, so the bytes a decoder or a
    runner is handed read the same however they are written. A payload the parser
    rejects -- a heredoc fragment, or a text the shell already stripped quotes
    from -- keeps the lexical fold below, which tolerates a malformed prefix.

    A value carrying a newline or both quote characters stays as written: the
    emitted literal has to remain one line and remain quotable, or the fold could
    manufacture a statement boundary the payload never had.
    """
    view = _PY_LINE_CONTINUATION_RE.sub(" ", payload)
    spans = _constant_spans(view)
    if not spans:
        return _fold_literals_lexically(view)
    raw = view.encode("utf-8")
    out: list[bytes] = []
    cursor = 0
    for left, right, text in spans:
        if left < cursor or "\n" in text or "\r" in text:
            continue
        if '"' in text and "'" in text:
            continue
        quote = "'" if "'" not in text else '"'
        out.append(raw[cursor:left])
        out.append(f"{quote}{text}{quote}".encode())
        cursor = right
    out.append(raw[cursor:])
    return b"".join(out).decode("utf-8", "replace")


def _fold_literals_lexically(payload: str) -> str:
    """Join plain adjacent literals without touching nested string delimiters.

    Only whitespace or a plus may join tokens. Joining each run in one operation
    avoids both repeated passes and quadratic concatenation of growing strings.
    """
    view = payload
    out: list[str] = []
    pieces: list[str] = []
    start = end = cursor = 0
    quote = "'"
    for item, left, right in _lex(view):
        literal = _PLAIN_LITERAL_RE.fullmatch(item.string) if item.type == token.STRING else None
        if literal is None:
            continue
        if pieces and view[end:left].strip() not in ("", "+"):
            out.extend((view[cursor:start], quote, "".join(pieces), quote))
            cursor = end
            pieces = []
        if not pieces:
            start = left
            quote = literal.group(1)
        pieces.append(literal.group(2))
        end = right
    if pieces:
        out.extend((view[cursor:start], quote, "".join(pieces), quote))
        cursor = end
    out.append(view[cursor:])
    return "".join(out)


def _call_spans(view: str) -> list[tuple[int, int, int]]:
    """(name start, argument start, argument end), in opening order.

    One lexer pass pairs parentheses without counting strings or comments.
    Unclosed calls end at EOF, never a truncated prefix. Store indices instead
    of nested suffix copies; no Python recursion or per-call boundary rescan.
    """
    calls: list[tuple[int, int, int]] = []
    stack: list[int | None] = []
    previous = None
    for item, start, end in _lex(view):
        if item.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
            continue
        if item.type == token.OP and item.string == "(":
            index = None
            if previous is not None and previous[0].type == token.NAME:
                index = len(calls)
                calls.append((previous[1], end, len(view)))
            stack.append(index)
        elif item.type == token.OP and item.string == ")" and stack:
            index = stack.pop()
            if index is not None:
                name, opening, _ = calls[index]
                calls[index] = (name, opening, start)
        previous = (item, start)
    return calls


def _preceded_by_attribute_access(view: str, position: int) -> bool:
    """True if the name at *position* is reached as an attribute of something else.

    An imported alias is a bare name, so ``obj.decode(...)`` is that object's own
    method and not the alias the payload bound.
    """
    cursor = position - 1
    while cursor >= 0 and view[cursor].isspace():
        cursor -= 1
    return cursor >= 0 and view[cursor] == "."


def _expression_start(view: str, position: int) -> int:
    """Where the dotted expression ending at *position* begins.

    A subscript head is reached through its receiver (``runpy.__dict__[...]``), so
    the name a binding gives it sits before that receiver, not before the
    subscript. Walking the dotted chain back is what lets
    :func:`_callable_aliases` see the ``r =`` in ``r = runpy.__dict__["exec"]``.
    """
    cursor = position
    while cursor > 0 and (view[cursor - 1].isalnum() or view[cursor - 1] in "_. \t"):
        cursor -= 1
    return cursor


def _unwrapped_head(text: str) -> str:
    """*text* with balanced outer parentheses removed: ``((f))`` reads as ``f``.

    Grouping parentheses are not an operation, so a head wrapped in them is the
    same callable. One layer per pass, bounded by the text it strips.
    """
    head = text.strip()
    while head.startswith("(") and head.endswith(")"):
        head = head[1:-1].strip()
    return head


def _indirect_heads(
    view: str,
    spans: list[tuple[int, int, int]],
    brackets: dict[int, list[tuple[int, int]]],
    aliases: "dict[str, str] | None" = None,
) -> list[tuple[int, int, str]]:
    """``(head start, position after the head, callable)`` for each name-free head.

    Three shapes name a callable without writing a NAME where the call can read
    it, so :func:`_call_spans` records no call for any of them:
    ``getattr(obj, "exec")`` and ``obj.__dict__["exec"]``, each resolved from a
    plain string literal, and a GROUPING -- ``(runpy.run_module)(…)`` -- whose
    parentheses are not an operation, so the head is the callable they wrap. All
    three are then either called immediately or bound to a name, which is what
    :func:`_callable_aliases` and :func:`_resolved_calls` ask of them.

    *aliases* resolves a grouping that wraps a bound name (``(r)(…)``). The
    binding pass omits it, because the table it is building is not complete yet,
    and a grouping around a dotted name resolves without it.
    """
    heads: list[tuple[int, int, str]] = []
    for name, start, end in spans:
        opener = _GETATTR_CALL_RE.match(view, name)
        if opener is None or opener.end() != start:
            continue
        arguments = brackets.get(start, [])
        if len(arguments) < 2:
            continue  # a one-argument getattr resolves to nothing
        left, right = arguments[1]
        literal = _ATTRIBUTE_NAME_LITERAL_RE.fullmatch(view[left:right].strip())
        if literal is not None and literal.group(2) in _RESOLVABLE_NAMES:
            heads.append((name, end + 1, literal.group(2)))
    for match in _ATTRIBUTE_SUBSCRIPT_RE.finditer(view):
        if match.group(2) in _RESOLVABLE_NAMES:
            heads.append((_expression_start(view, match.start()), match.end(), match.group(2)))
    for opening, arguments in brackets.items():
        if not arguments:
            continue
        # A bracket is a GROUPING only when nothing callable or subscriptable sits
        # in front of it; otherwise it is that call's or subscript's own argument
        # list, and ``foo(run_module)(x)`` hands over what foo RETURNS.
        cursor = opening - 2
        while cursor >= 0 and view[cursor].isspace():
            cursor -= 1
        if cursor >= 0 and (view[cursor].isalnum() or view[cursor] in "_)]"):
            continue
        closing = arguments[-1][1]
        if closing >= len(view) or view[closing] != ")":
            continue
        dotted = _DOTTED_HEAD_RE.match(_unwrapped_head(view[opening:closing]))
        if dotted is None:
            continue
        word = dotted.group(1)
        canonical = word if word in _RESOLVABLE_NAMES else (aliases or {}).get(word)
        if canonical is not None:
            heads.append((opening - 1, closing + 1, canonical))
    return heads


def _callable_aliases(
    view: str,
    spans: list[tuple[int, int, int]],
    brackets: dict[int, list[tuple[int, int]]],
) -> dict[str, str]:
    """Names this payload binds to a callable the gates read: alias -> callable.

    Read from the payload's own STATIC bindings, in token order so quoted text
    binds nothing: ``from runpy import run_module as r``, ``r = runpy.run_module``,
    ``r = getattr(runpy, "run_module")`` and ``r = m.__dict__["run_module"]`` all
    reach one callable while writing a different word at the call.

    A name computed at run time (``getattr(m, input())``, a subscript by
    variable) binds nothing here. That residual is noted rather than claimed
    covered, and it is the same residual the sensitive-path floor answers for a
    file written and then run.
    """
    items = [
        (item, start)
        for item, start, _ in _lex(view)
        if item.type not in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING)
    ]
    aliases: dict[str, str] = {}
    index = 0
    while index < len(items):
        item, _ = items[index]
        if (
            item.type == token.NAME
            and item.string == "from"
            and index + 2 < len(items)
            and items[index + 2][0].string == "import"
        ):
            exported = _CALLABLE_MODULES.get(items[index + 1][0].string, frozenset())
            index += 3
            if index < len(items) and items[index][0].string == "(":
                index += 1
            while index < len(items) and items[index][0].type == token.NAME:
                original = items[index][0].string
                alias = original
                index += 1
                if index + 1 < len(items) and items[index][0].string == "as":
                    alias = items[index + 1][0].string
                    index += 2
                if original in exported:
                    aliases[alias] = original
                if index >= len(items) or items[index][0].string != ",":
                    break
                index += 1
            continue
        # ``r = run_module`` / ``r = runpy.run_module`` -- the callable itself, not
        # its result, so a trailing ``(`` disqualifies the binding.
        if item.type == token.NAME and index + 2 < len(items) and items[index + 1][0].string == "=":
            cursor = index + 2
            last = None
            while cursor < len(items) and items[cursor][0].type == token.NAME:
                last = items[cursor][0].string
                cursor += 1
                if cursor < len(items) and items[cursor][0].string == ".":
                    cursor += 1
                    continue
                break
            if last in _RESOLVABLE_NAMES and (
                cursor >= len(items) or items[cursor][0].string != "("
            ):
                aliases[item.string] = last
        index += 1
    for head, _, canonical in _indirect_heads(view, spans, brackets):
        binding = _ALIAS_BINDING_RE.search(view[:head])
        if binding is not None:
            aliases[binding.group(1)] = canonical
    return aliases


def _resolved_calls(
    view: str,
    spans: list[tuple[int, int, int]] | None = None,
    brackets: dict[int, list[tuple[int, int]]] | None = None,
) -> list[tuple[str, int, int, int]]:
    """``(callable, head start, argument start, argument end)`` per resolved call.

    The one place a call is identified, so the runner, loader and decoder gates
    all read the same answer and a spelling closed for one is closed for all.
    A call resolves when the word written at it IS one of the callables, when
    that word is a name the payload bound to one (:func:`_callable_aliases`), or
    when the call has no name at all because its head resolved the callable
    (``getattr(runpy, "run_module")(...)``).

    Opening order, so a caller reading nested calls inner-first can reverse it.
    """
    if spans is None:
        spans = _call_spans(view)
    if brackets is None:
        brackets = _argument_spans(view)
    aliases = _callable_aliases(view, spans, brackets)
    resolved: list[tuple[str, int, int, int]] = []
    for name, start, end in spans:
        written = _CALL_NAME_RE.match(view, name)
        if written is None:
            continue
        word = written.group(1)
        if word in _RESOLVABLE_NAMES:
            resolved.append((word, name, start, end))
            continue
        canonical = aliases.get(word)
        if canonical is not None and not _preceded_by_attribute_access(view, name):
            resolved.append((canonical, name, start, end))
    for head, after, canonical in _indirect_heads(view, spans, brackets, aliases):
        cursor = after
        while cursor < len(view) and view[cursor].isspace():
            cursor += 1
        if cursor >= len(view) or view[cursor] != "(":
            continue  # bound to a name, or never called here
        arguments = brackets.get(cursor + 1, [])
        end = arguments[-1][1] if arguments else cursor + 1
        resolved.append((canonical, head, cursor + 1, end))
    resolved.sort(key=lambda entry: entry[1])
    return resolved


def _disjoint_arguments(
    view: str, resolved: list[tuple[str, int, int, int]], wanted: frozenset[str]
) -> tuple[str, ...]:
    """Outermost arguments of the *wanted* calls; nested text is inspected once.

    Keeping all nested suffixes costs quadratic space even with a linear lexer.
    Outermost intervals include their nested names without copying those tails.
    """
    args: list[str] = []
    covered = -1
    for canonical, head, start, end in resolved:
        if canonical not in wanted or head < covered:
            continue
        args.append(view[start:end])
        covered = end
    return tuple(args)


def _code_loader_arguments(view: str) -> tuple[str, ...]:
    """Arguments belonging to code loaders, excluding later statements."""
    return _disjoint_arguments(view, _resolved_calls(view), _INLINE_CODE_LOADER_NAMES)


def _dynamic_runner_handed_the_package(view: str) -> bool:
    return any(
        _PACKAGE_LITERAL_RE.search(arg)
        for arg in _disjoint_arguments(view, _resolved_calls(view), _DYNAMIC_RUNNER_NAMES)
    )


def _decoder_call_names(view: str) -> list[tuple[int, int, int]]:
    """``(head start, argument start, argument end)`` per base64 decoder call.

    The decoder gate's view of :func:`_resolved_calls`, so an alias, a ``getattr``
    and a ``__dict__`` subscript reach the decoder exactly as they reach a runner
    or a loader. Opening order; the caller reverses it to resolve nesting
    innermost first.
    """
    return [
        (head, start, end)
        for canonical, head, start, end in _resolved_calls(view)
        if canonical in _B64_DECODER_NAMES
    ]


def _argument_spans(view: str) -> dict[int, list[tuple[int, int]]]:
    """Index comma-separated arguments once, without copying nested expressions.

    All brackets participate so commas in lists, dictionaries, nested calls,
    strings and comments cannot split their containing decoder's arguments.
    """
    arguments: dict[int, list[tuple[int, int]]] = {}
    stack: list[tuple[int, int | None]] = []
    for item, start, end in _lex(view):
        if item.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
            continue
        if item.type == token.OP and item.string in (")", "]", "}") and stack:
            opening, left = stack.pop()
            if left is not None:
                arguments[opening].append((left, start))
            continue
        if item.type == token.OP and item.string == "," and stack:
            opening, left = stack[-1]
            if left is not None:
                arguments[opening].append((left, start))
            stack[-1] = (opening, None)
            continue
        if stack and stack[-1][1] is None:
            stack[-1] = (stack[-1][0], start)
        if item.type == token.OP and item.string in ("(", "[", "{"):
            stack.append((end, None))
            arguments[end] = []
    for opening, left in stack:
        if left is not None:
            arguments[opening].append((left, len(view)))
    return arguments


def _decoder_input_spans(view: str) -> list[tuple[int, int, int, int]]:
    """``(name start, argument end, value start, value end)`` per decoder call, inner first.

    Which argument carries the bytes is one decision, and both the decode and the
    ownership check below need it, so it is made in one place. A keyword ``s=``
    wins wherever it sits, else the first positional -- the stdlib signature --
    and an unrecognised keyword resolves to nothing rather than to a guess.
    Indices, not slices: a nested call's argument text is never copied.
    """
    arguments = _argument_spans(view)
    spans: list[tuple[int, int, int, int]] = []
    for name, start, end in reversed(_decoder_call_names(view)):
        value_start = start
        value_end = end
        for index, (left, right) in enumerate(arguments.get(start, [])):
            keyword = _B64_INPUT_KEYWORD_RE.match(view, left, right)
            if keyword is not None and keyword.group(1) == "s":
                value_start, value_end = keyword.end(), right
                break
            if index == 0 and keyword is None:
                value_start, value_end = left, right
                break
        spans.append((name, end, value_start, value_end))
    return spans


def _decode_call_literal_sources(view: str) -> tuple[tuple[str, str], ...]:
    """``(source literal, decoded text)`` for each decoder call whose input resolves.

    Each decoder occurrence is evaluated once. Nested decoding shrinks the bytes;
    no loop repeatedly decodes arbitrary data until it happens to name a mint.
    Nonliteral arguments are left unresolved, never executed.

    The SOURCE is the base64 that appears in *view*, lower-cased so a caller
    reading the floor's lower-cased view can match it. For a nested chain it is
    the OUTERMOST literal, inherited from the resolved child: the intermediate
    bytes exist only inside this function, so tagging a decoding with them would
    leave it unattributable to the text it came from.
    """
    view = _fold_inline_literals(view)
    resolved: dict[int, tuple[int, str, str]] = {}
    decoded: list[tuple[str, str]] = []
    for name, end, value_start, value_end in _decoder_input_spans(view):
        literal = _INLINE_B64_LITERAL_RE.fullmatch(view, value_start, value_end)
        if literal is not None:
            value = literal.group(2)
            source = value.lower()
        else:
            # Only a directly nested decoder is resolved; no arbitrary expression runs.
            child_start = value_start
            while child_start < value_end and view[child_start].isspace():
                child_start += 1
            qualifier = _B64_MODULE_QUALIFIER_RE.match(view, child_start, value_end)
            if qualifier is not None:
                child_start = qualifier.end()
            child = resolved.get(child_start)
            if child is None or view[child[0] : value_end].strip():
                continue
            value, source = child[1], child[2]
        try:
            text = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            ).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeError):
            continue
        resolved[name] = (end + 1, text, source)
        decoded.append((source, text.lower()))
    return tuple(decoded)


def _decode_call_literals(view: str) -> tuple[str, ...]:
    """The decodings of *view*'s own decoder calls, without their source literals."""
    return tuple(decoded for _, decoded in _decode_call_literal_sources(view))


def _names_an_inline_interpreter(words: list[str]) -> bool:
    """True if *words* read as a command that could run an inline PROGRAM.

    The gate on splitting a text to expose a payload. Splitting removes one
    layer of quotes, which is exactly what uncovers a quoted carrier -- and, on
    a text that is already Python, would remove PYTHON's own quotes and promote
    printed text to code: ``print("b64decode('…')")`` split once reads as a
    real decoder call, fabricating a decoding no program performs. A command
    that can run an inline payload names an interpreter or a shell in one of its
    words (the pipe producer's own program is neither, so the whole command is
    what is asked, not its first word); printed Python names nothing.
    """
    for word in words:
        base = _program_basename(word)
        if base in _NESTED_SHELL_PROGRAMS or _PYTHON_PROGRAM_RE.match(base):
            return True
    return False


def _program_text_candidates(raw_text: str) -> list[str]:
    """Every text the shell could hand an interpreter as a PROGRAM, over-yielded.

    Three levels, because a payload's Python quotes survive exactly as many
    shell splits as the spelling wrapped it in: the whole command reads as
    Python for an UNQUOTED carrier (a heredoc body), its shell WORDS expose a
    quoted carrier with its base64 case intact (a ``-c`` operand, a here-string
    word, a pipe producer's operand), and one further split exposes a payload
    nested inside another shell command, where an inner ``python -c`` payload
    sits inside the outer shell's own quoted operand. The raw text alone reaches
    only the unquoted carrier: a quoted one holds its decoder call inside a single
    string token, where the tokenizer cannot see it.

    Over-yielding is safe here and a bounded cost. Safe because the caller
    attributes a decoding only to the payload that carries its own source
    literal, so a neighbouring command's decoding cannot travel to this one --
    the whole reason the source is tracked. Bounded because the levels are fixed
    and each level is one pass over the text it splits.
    """
    candidates = [raw_text]
    frontier = [raw_text]
    for _ in range(_SHELL_QUOTE_LEVELS):
        deeper: list[str] = []
        for text in frontier:
            words = _shell_tokens(text)
            if not _names_an_inline_interpreter(words):
                continue
            for word in words:
                if word == text:
                    continue  # nothing left to unwrap
                candidates.append(word)
                deeper.append(word)
        frontier = deeper
    return candidates


def _decoded_b64_literal_sources(raw_text: str) -> tuple[tuple[str, str], ...]:
    """``(source literal, decoded text)`` for the decoder calls *raw_text* carries.

    Read from the command AS SUBMITTED because base64 is case-sensitive and does
    not survive the floor's lower-cased view. Only an argument a decoder call is
    actually handed is decoded -- an encoded literal a program merely carries as
    data stays data -- and each ``(source, decoded)`` pair is reported once, so a
    spelling that reaches the same payload at two levels does not double it.
    """
    seen: set[tuple[str, str]] = set()
    sources: list[tuple[str, str]] = []
    for text in _program_text_candidates(raw_text):
        # A decoder has to be named to resolve at all, so this keeps the tokenizer
        # off every ordinary command word without reading the literal's spelling.
        if not _B64_DECODER_NAME_RE.search(text):
            continue
        for pair in _decode_call_literal_sources(text):
            if pair not in seen:
                seen.add(pair)
                sources.append(pair)
    return tuple(sources)


def _decoded_b64_literals(raw_text: str) -> tuple[str, ...]:
    """The decodings *raw_text* carries, without their source literals."""
    return tuple(decoded for _, decoded in _decoded_b64_literal_sources(raw_text))


def _inline_payload_reaches_cli(
    payload: str, decoded_literals: tuple[tuple[str, str], ...] = ()
) -> bool:
    """True if this payload names the credential mint, plainly or once decoded.

    *decoded_literals* is the whole command's decode pool
    (:func:`_decoded_b64_literal_sources`), because only the raw text still has
    the case base64 needs -- so each entry arrives with the source literal it
    was decoded from, and an entry counts for THIS payload only when the payload
    carries that literal. Unscoped, the pool is shared: a decoder call in one
    command lends its decoding to every other payload in the frame, so
    ``python -c 'print(b64decode("aGVsbG8="))'`` beside an unrelated ``printf``
    of encoded text reads as a mint.

    Matched as a substring rather than as a quoted literal because the floor's
    tokens have had their shell quotes removed: a heredoc body arrives as
    ``exec(b64decode(s=aw1…))``, with the Python quotes already gone. Carrying
    the bytes AND running a decoder over them is the reach; a payload that only
    carries them is data (the pool never decoded them), and a payload with no
    decoder call at all borrows nothing.
    """
    view = _fold_inline_literals(payload)
    if _names_the_mint(view):
        return True
    # Do not attach another command's decoding to a payload with no decoder call.
    if not _decoder_call_names(view):
        return False
    carried = view.lower()
    for source, decoded in decoded_literals:
        if source not in carried:
            continue
        if _SELF_NAME_RE.search(decoded) and _MINT_VERB_RE.search(decoded):
            return True
        if _names_the_mint(decoded):
            return True
    return False


def _names_the_mint(view: str) -> bool:
    """Check each disjoint loader region without recursive descendant rescans.

    Recursive all-loader rescans grow exponentially with nesting. Keeping only
    outer intervals bounds copied/scanned argument text by the source length.
    The scoped import anchor retains the quote-peeling distinction: a statement
    at a string's start is not necessarily a match at the whole view's start.
    """
    if _MINT_SURFACE_RE.search(view):
        return True
    if _PRODUCT_IMPORT_RE.search(view) and _MINT_VERB_RE.search(view):
        return True
    resolved = _resolved_calls(view, _call_spans(view), _argument_spans(view))
    runners = _disjoint_arguments(view, resolved, _DYNAMIC_RUNNER_NAMES)
    loaders = _disjoint_arguments(view, resolved, _INLINE_CODE_LOADER_NAMES)
    if any(_PACKAGE_LITERAL_RE.search(arg) for arg in runners):
        return True
    for raw_arg in loaders:
        arg = _LOADER_ARG_DELIMITERS_RE.sub("", raw_arg)
        if _CONSOLE_SCRIPT_LITERAL_RE.search(arg) or _MINT_SURFACE_RE.search(arg):
            return True
        if (
            _PRODUCT_IMPORT_RE.search(arg) or _LOADED_IMPORT_RE.search(raw_arg)
        ) and _MINT_VERB_RE.search(arg):
            return True
        if _dynamic_runner_handed_the_package(arg):
            return True
    return False
