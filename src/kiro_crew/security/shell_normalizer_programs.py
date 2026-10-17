"""Shell text reader: the program vocabulary that attribution reads.

The sets of program names :mod:`kiro_crew.security.shell_normalizer` treats
specially: nested shells and their verbs, environment splitters, data consumers whose
argv is a mention rather than an invocation, and the Python interpreter spellings and
the flags that take an operand or an inline program. Data only, kept in one module so
a program is added in one place; ``shell_normalizer`` reads it and re-exports every
name.
"""

from __future__ import annotations

import re

#: Interpreter names that accept ``-m <module>``. Versioned spellings (``python3``,
#: ``python3.12``) and the Windows launcher included; ``.exe`` is stripped by
#: ``_program_basename`` before this is applied.
_PYTHON_PROGRAM_RE = re.compile(r"\Apy(?:thon)?[0-9.]*(?:\.exe)?\Z")


#: Interpreter flags that consume the NEXT token as their operand. Their operand must be
#: skipped when scanning for ``-m <module>``, or it terminates the scan and the mint slips
#: through (``python -X dev -m kiro_crew token``). ``-c`` and ``-m`` are deliberately absent:
#: both END the option list, and `-m` is what this scan is looking for.
#:
#: LOWERCASE, because the floor runs over an already-lowercased command (`_is_credential_mint`
#: takes `text_lower`), so a `-X` in the operator's shell reaches this set as `-x`. Storing the
#: uppercase spelling made every separate-operand form match nothing — the bypass stayed open
#: while the ATTACHED spellings (`-Xdev`) passed, which is the shape of a fix that looks tested.
#: Python's real flags are case-sensitive (`-x` skips the first line, `-X` sets an
#: implementation option), so this over-matches `-x` slightly: `python -x -m kiro_crew token`
#: would skip `-m` as an operand and MISS. Guarded by also treating a bare `-m` as the marker
#: on the next iteration — see the loop.
_PYTHON_OPERAND_FLAGS = frozenset({"-x", "-w", "-q", "--check-hash-based-pycs"})


#: Interpreter flags that take an INLINE PROGRAM as their operand: ``-c`` a statement string,
#: ``-`` / no flag a stdin script. ``python -c "from kiro_crew.cli import main; main()" token``
#: mints the identical token as ``python -m kiro_crew token`` — the payload is one argv word
#: carrying the import name, so the ``-m`` marker scan never fires and the "not a flag ⇒ not
#: the module shape" bail treated the payload as a script name and returned False. Same escape,
#: one flag over.
_PYTHON_INLINE_PROGRAM_FLAGS = ("-c",)


# Programs whose ``-c`` argument is a shell script: its text is a COMMAND, so a
# self-protection check has to look inside it rather than treat it as an operand.
_NESTED_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"})


_NESTED_SHELL_VERBS = frozenset({"eval", "source", "."})


_ENV_SPLIT_PROGRAMS = frozenset({"env"})


# product name appearing in their argv is a mention, not an invocation:
# ``echo kirocrew token`` prints two words.
#
# This list is deliberately a DENYLIST of data consumers rather than an ALLOWLIST
# of executors, because the two fail in opposite directions.  Many commands pass
# their remaining argv to an executor -- ``ssh host …``, ``docker exec c …``,
# ``sudo``, ``env``, ``nohup``, ``timeout``, ``runuser``, ``chroot``, ``pkexec``,
# ``systemd-run``, ``nice``, ``xargs`` -- and enumerating THOSE means a forgotten
# entry is a silent BYPASS.  Enumerating data consumers instead means a forgotten
# entry is a false positive: annoying, visible, and safe.  So the default for an
# unrecognised program is "this could execute the name".
# When adding a member, also decide whether its options or operands can run a
# helper; if so, add it to
# ``perm_verb_mention._PERM_VERB_MENTION_EXCLUDED_PROGRAMS``.
_DATA_CONSUMER_PROGRAMS = frozenset(
    {
        # filesystem inspectors: every argument is a path to DESCRIBE, so a glob
        # or brace word among them (``ls -d dir/*``) is a filename, not a program.
        "ls",
        "stat",
        "file",
        "du",
        "readlink",
        "realpath",
        "dirname",
        "basename",
        # filesystem movers: every argument is a path to COPY, MOVE, LINK, REMOVE or
        # re-mode, never a program to run (``cp $dir/*; ...``).  ``tar`` is absent on
        # purpose: ``-I <prog>`` / ``--use-compress-program`` executes its argument.
        "cp",
        "mv",
        "ln",
        "rm",
        "mkdir",
        "rmdir",
        "touch",
        "chmod",
        "chown",
        "echo",
        "printf",
        "print",
        "cat",
        "tac",
        "tee",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "sed",
        "awk",
        "cut",
        "tr",
        "sort",
        "uniq",
        "wc",
        "nl",
        "fold",
        "column",
        "comm",
        "diff",
        "strings",
        "jq",
        "yq",
        "base64",
        "md5sum",
        "sha256sum",
        "xxd",
        "od",
    }
)
