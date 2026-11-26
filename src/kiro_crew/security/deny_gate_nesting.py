"""The substitution-nesting cap the command deny gate checks before any floor runs.

:func:`kiro_crew.security.deny_gate.is_denied` refuses a command that nests more than
``_MAX_SUBSTITUTION_DEPTH`` substitutions deep (:func:`_nested_past_cap`), with the refusal
key ``_DEPTH_REFUSAL`` and the note ``_DEPTH_NOTE``, so no floor re-walks bodies that deep.
The deny gate imports those three names from here; this module reads no other module.
"""

from __future__ import annotations

# Matches the ssh floor's _MAX_SSH_READING_DEPTH; unify the two once both are on main.
_MAX_SUBSTITUTION_DEPTH = 32
# Where a shell word starts, for a ``#`` comment and the ``case`` / ``esac`` words.
_WORD_BREAK = frozenset(" \t\n;&|()<>`")
_DEPTH_REFUSAL = f"substitution nesting deeper than {_MAX_SUBSTITUTION_DEPTH}"
_DEPTH_NOTE = (
    f"a command nested more than {_MAX_SUBSTITUTION_DEPTH} substitutions deep is refused;"
    " flatten it"
)


def _nested_past_cap(text: str) -> bool:
    """True once *text* nests command text more than ``_MAX_SUBSTITUTION_DEPTH`` deep.

    One pass, linear in *text*. The floors re-walk every substitution body as its own
    string, so their cost grows with depth times length; past the cap the command is
    refused before any floor runs. Bash opens a FRESH quoting context inside each
    ``$(``, so ``"$(echo "$(``... nests even though the quotes alternate: the scan keeps
    a stack of contexts rather than one flat quote state. Only ``$(``, ``<(`` and
    ``>(`` count. A bare ``(`` is tracked, so its ``)`` closes it, and so is a backtick
    span, so a ``)`` or quote inside it reads right, but neither counts. An unescaped
    backtick cannot open inside a backtick span (it closes it, even from inside a
    double-quoted string there), so between two counted levels sits at most one
    backtick level and the floors' work stays within about twice the cap. Not counting
    them also keeps strings that are not posix shell from tripping the cap: PowerShell
    escapes with a backtick (a backtick before a quote), and a non-shell tool string may nest parens.
    A ``#`` comment runs to its newline, and between ``case`` and ``esac`` a ``)`` ends
    a pattern rather than a frame, as the floors' own walk reads them.
    """
    stack = ["c"]  # c: substitution body, p: bare ( group, d: double quotes, b: backtick body
    cases = [0]  # open ``case`` words per frame: a pattern's ``)`` closes nothing there
    depth = 0  # open $( <( >( frames above the outermost command
    escaped = -1  # index of the last character a backslash escaped
    i, n = 0, len(text)
    while i < n:
        ch, top = text[i], stack[-1]
        if ch == "\\":
            escaped = i + 1
            i += 2
            continue
        if top == "d":
            if ch == '"':
                stack.pop()
                cases.pop()
            elif ch == "$" and text.startswith("(", i + 1):
                stack.append("c")
                cases.append(0)
                depth += 1
                i += 1
            elif ch == "`":
                if stack[-2] == "b":  # bash closes the enclosing backtick span here
                    del stack[-2:]
                    del cases[-2:]
                else:
                    stack.append("b")
                    cases.append(0)
        elif ch == "'":
            ansi = i > 0 and text[i - 1] == "$"
            j = i + 1
            while j < n and text[j] != "'":
                j += 2 if ansi and text[j] == "\\" else 1
            i = j + 1
            continue
        elif ch == "#" and (i == 0 or text[i - 1] in _WORD_BREAK):
            newline = text.find("\n", i)
            if newline < 0:
                break
            i = newline  # the comment's own quotes and parens are text
            continue
        elif ch in "ce" and (i == 0 or text[i - 1] in _WORD_BREAK):
            word = text[i : i + 4]
            j = i - 1  # each blank run is walked only by the word after it: linear
            while j >= 0 and text[j] in " \t":
                j -= 1
            before = text[j] if j >= 0 else ""  # shortcut: reads no reserved-word chain
            if (
                word in ("case", "esac")
                and text[i + 4 : i + 5] in _WORD_BREAK
                and before in ("", "\n", ";", "&", "|", "(", "`")
            ):
                cases[-1] = cases[-1] + 1 if word == "case" else max(0, cases[-1] - 1)
                i += 4
                continue
        elif ch == '"':
            stack.append("d")
            cases.append(0)
        elif ch == "(":
            if i > 0 and text[i - 1] in "$<>" and escaped != i - 1:
                stack.append("c")
                depth += 1
            else:
                stack.append("p")
            cases.append(0)
        elif ch == ")" and top in "cp" and len(stack) > 1 and not cases[-1]:
            depth -= stack.pop() == "c"
            cases.pop()
        elif ch == "`":
            if top == "b":
                stack.pop()
                cases.pop()
            else:
                stack.append("b")
                cases.append(0)
        if depth > _MAX_SUBSTITUTION_DEPTH:
            return True
        i += 1
    return False
