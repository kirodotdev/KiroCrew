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
# ``kirocrew file-delivery``'s dispatchable verbs -- the whole of that subcommand's
# argparse ``choices``, which a test in the denied-command suite derives from the
# parser so the two cannot drift. The floor keys on the VERB rather than on the
# subcommand word because ``action`` is required, so the bare and ``--help`` forms
# dispatch nothing and refusing them would refuse a read-only golden path.
_SELF_FILE_DELIVERY_VERBS = frozenset({"approve"})
# The dev-mode escape hatch flag ``kirocrew`` accepts to run OUT of its install
# root. It is a FLAG, not a program: the self-protection floor keys on the token
# alone, so the shell reader's over-cap fold must treat a word resolving to it as
# dangerous even though no program name is present.
_DEV_MODE_CONFIRM_FLAG = "--confirm-out-of-install-root"

# The over-cap guarded-reading fold's protected TOKEN vocabulary, in ONE place: the
# ssh-like programs that can open a channel back to this host, and the protected
# FLAGS a flag-based self-protection floor keys on with no program word present. A
# new flag-based floor is covered by adding its flag here, so a fold gap of that
# class cannot recur without touching this vocabulary (found in review). Nested-shell
# programs/verbs keep their shared definition in the reader and are handed to the
# fold beside these; the cli and protected-path names are dynamic predicates.
_SSH_LIKE_PROGRAMS = frozenset({"ssh", "scp", "sftp"})
_FOLD_PROTECTED_FLAGS = frozenset({_DEV_MODE_CONFIRM_FLAG})
