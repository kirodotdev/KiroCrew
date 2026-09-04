"""The product's own name, as the matchers spell it, plus the by-name kill verbs.

The lowest layer of the package: pure vocabulary, no predicate and no decision.
Two tiers read it and neither owns it. The shell reader resolves a substitution
or an alias back to a program name and has to recognise the product's own name to
know it is looking at the product; the argv-structural self-protection floor
above the reader matches the same name in command position. Holding the spellings
here is what keeps that a one-way dependency instead of the reader depending on
the floor and the floor on the reader.

Imports nothing from the package, so every layer above may load-import it.

Two spellings of the name, because the tiers ask two different questions: one
matches the name ANYWHERE in a token, the other only as a whole program name --
bare or as the tail of a path -- which is what separates an invocation of the
product from a directory that merely carries its name. The comment above
``_is_self_program`` in the shell reader, the sole consumer of the whole-program
form, records that distinction where the matching happens.
"""

from __future__ import annotations

import re

_SELF_NAME_RE = re.compile(r"kiro[-.]?crew")


_SELF_PROGRAM_RE = re.compile(r"\Akiro[-.]?crew(?:\.(?:exe|cmd|bat|sh|py))?\Z")
_SELF_PROGRAM_SPELLINGS = ("kirocrew", "kiro-crew", "kiro.crew")
# The kill programs that select their target BY NAME.  Bare ``kill`` takes PIDs
# and is handled separately (it can only reach the product through a command
# substitution that resolves the name), and both verbs are matched on TOKENS via
# ``_program_basename`` so a path-qualified or expansion-produced spelling counts.
_KILL_BY_NAME_PROGRAMS = frozenset({"pkill", "killall"})
