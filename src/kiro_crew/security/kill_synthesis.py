"""The bare-kill window's runtime-synthesis search.

``_is_self_kill`` in ``argv_floor`` asks whether a bare-kill substitution body
builds the protected name at run time -- from a single-literal ``printf``/``echo``
substitution or a brace expansion -- after its static views found nothing. The
floor hands in its constant-output resolver, its brace expander and the body's
character cap, so this module imports only ``shell_normalizer`` and
``vocabulary`` and ``argv_floor`` stays under the package's per-module cap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import shell_normalizer as _shell_normalizer
from .shell_normalizer import _debracket
from .vocabulary import _SELF_NAME_RE

if TYPE_CHECKING:
    from collections.abc import Callable


def _kill_body_synthesizes_self(
    body: str,
    resolve: "Callable[[str], str]",
    expand: "Callable[..., list[str] | None]",
    char_cap: int,
) -> bool:
    """True if a bare-kill substitution *body* builds the protected name at runtime.

    Brace-expands each folded word under ONE character budget for the body,
    failing closed once it or the assignment bound is spent.  Neither an
    unresolvable operand NOR a brace product past the fan-out cap is failed
    closed: both are the documented residual (the issue's reported forms all fit
    within the cap, and a wider product cannot name a single live process).
    """
    words = _shell_normalizer._synthesis_words(body, resolve, char_cap)
    if words is None:
        return True
    budget = [char_cap]

    for word in words:
        # Mask ${...} braces so an ENCLOSING brace group still expands.
        alternatives = expand(_shell_normalizer._mask_param_word_braces(word), budget=budget)
        if budget[0] < 0:
            return True
        if alternatives is None:
            continue  # over-cap fan-out: a residual, not failed closed
        for alt in alternatives:
            alt = _shell_normalizer._restore_quoted_braces(alt)
            if _SELF_NAME_RE.search(_debracket(alt)) or _SELF_NAME_RE.search(
                _shell_normalizer._resolved_word_view(alt)
            ):
                return True
    return False
