"""World-rwx ``chmod`` seatbelt behind the ``local-destructive-chmod-777`` row.

Refuses the common respellings of ``chmod 777`` the row misses: options before the
mode, ``--``, leading zeros and a special-bits digit. A seatbelt, not a parser:
other shell shapes stay with the row and the OS sandbox.
"""

from __future__ import annotations

import re

from ..trust_patterns import ENV_ASSIGNMENT_RE
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES
from .rm_floor import _segments

_CHMOD_FLOOR_RULE_ID = "local-destructive-chmod-777"
_CHMOD_FLOOR_RULE: "tuple[str, str] | None" = next(
    ((rule.id, rule.pattern) for rule in BUILTIN_DENIED_RULES if rule.id == _CHMOD_FLOOR_RULE_ID),
    None,
)
_CHMOD_FLOOR_NOTE = (
    "Matched on the chmod mode operand, not the pattern text: "
    "a numeric mode that gives every user read, write and execute."
)
_CHMOD_FLOOR_UNREAD_NOTE = (
    "Refused because the chmod check could not read this command. Simplify it."
)
_CAN_FIRE_RE = re.compile(r"c[^\w\s]*h[^\w\s]*m[^\w\s]*o[^\w\s]*d")  # quote splices allowed
# GNU chmod reads any run of leading zeros and one special-bits digit before 777.
_WORLD_RWX_MODE_RE = re.compile(r"0*[0-7]?777")


def _can_fire(command: str) -> bool:
    return bool(_CAN_FIRE_RE.search(command))


def _mode_operand(args: "list[str]") -> "str | None":
    """The mode word of a ``chmod`` argv, or ``None`` when ``--reference`` replaces it."""
    mode = None
    reference = done = False
    for arg in args:
        if not done and arg == "--":
            done = True
        elif not done and arg.startswith("--"):
            reference = reference or arg.startswith("--ref")
        elif not done and arg.startswith("-") and arg != "-":
            continue  # a short option cluster, wherever it sits
        elif mode is None:
            mode = arg
    return None if reference else mode


def _is_chmod_world_rwx(command: str) -> bool:
    """True when a ``chmod`` in *command* sets a numeric mode ending in ``777``."""
    for segment in _segments(command):
        words = _shell._shell_tokens(segment)
        while words and ENV_ASSIGNMENT_RE.match(words[0]):
            words = words[1:]
        if not words or _shell._program_basename(words[0]) != "chmod":
            continue
        mode = _mode_operand(words[1:])
        if mode is not None and _WORLD_RWX_MODE_RE.fullmatch(mode):
            return True
    return False
