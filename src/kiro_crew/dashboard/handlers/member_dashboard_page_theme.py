"""A dashboard package's declared names and the colour tokens the chat may follow.

Split out of :mod:`member_dashboard` to keep that file under the 2,000-line cap, and
cohesive on its own: both pieces are about a package's agent-authored NAMES and token
VALUES and keeping a credential out of either.

- :func:`page_theme` — the bounded set of colour tokens the Crewmates-page chat may
  follow (``--bg``/``--accent``), redacted; the chat derives every text colour itself
  on the client, behind the ``chat_page_colors`` feature preview.
- :func:`package_declared_name_spots` — where a package declares a name a redactor
  would mask, which ``dashboard_save`` refuses on.

``member_dashboard`` imports :func:`page_theme` for its read. The one caller of
:func:`package_declared_name_spots` (``agent_panel_dashboard``) imports it from here.
"""

from __future__ import annotations

from typing import Any, Mapping

from kiro_crew.platform.context import redact_via_context

#: The page theme tokens the CHAT follows: the two whose role is
#: unambiguous. A page's tokens are free-form custom-property names its author
#: picks, so the chat reads exactly these two by name -- the page background and
#: the page accent, the same names the app's own theme uses -- and derives every
#: text colour itself on the client. The crewmate-chat-page-colours preview flag
#: gates whether the client applies them; the server just names them.
CHAT_PAGE_THEME_TOKENS = ("--bg", "--accent")


def page_theme(package: Mapping[str, Any]) -> dict[str, str]:
    """The colour tokens the chat may follow from *package*'s theme, or ``{}``.

    Only :data:`CHAT_PAGE_THEME_TOKENS`, and only when the package declares them, so
    the body names a bounded, predictable set rather than a page's whole palette. The
    values are already the validator's own: a theme token value is a single printable
    line with no ``;``/``{``/``}``/``<``/``>`` (``_THEME_VALUE_RE``), so a colour
    literal passes and a style statement cannot. The chat derives readable text from
    these on the client; the page's own text tokens are not read, since readability
    cannot be left to the page author.

    REDACTED on the way out, the same pass ``_page_safe`` gives every other value the
    dashboard route returns. ``_validate_theme`` only bounds a token value's SHAPE, not
    its content, so a credential-shaped string an agent (or a prompt-injected write) put
    in ``--bg``/``--accent`` would otherwise reach the client verbatim; the canonical
    egress redactor rewrites it first, and a value the redactor rewrites is dropped
    rather than shipped half-masked (a rewritten colour is not a colour the chat can
    use, so the chat keeps its own theme for that token).
    """
    raw_theme = package.get("theme")
    theme: Mapping[str, Any] = raw_theme if isinstance(raw_theme, Mapping) else {}
    raw_tokens = theme.get("tokens")
    tokens: Mapping[str, Any] = raw_tokens if isinstance(raw_tokens, Mapping) else {}
    out: dict[str, str] = {}
    for name in CHAT_PAGE_THEME_TOKENS:
        value = tokens.get(name)
        if isinstance(value, str) and value and redact_via_context(value) == value:
            out[name] = value
    return out


def package_declared_name_spots(package: Mapping[str, Any]) -> list[str]:
    """Where *package* declares a name a redactor would mask. Positions, never names.

    THE ONE SCAN over the two name sets a dashboard package carries -- its model's
    field names and its theme's token keys -- and the one place
    the rule lives. ``dashboard_save`` asks it and refuses a save that trips it. Two
    copies of this predicate would be two rules, and the first thing the second one
    would do is drift from this one.

    The save keeps a credential out of a page an agent asked for honestly, with a
    refusal it can act on while the name can still be changed.

    REFUSED RATHER THAN MASKED, at both ends, because every one of these names is a
    JOIN KEY: a field name ties the model to the page cells that read it, and a theme
    token is the CSS variable the style references. A redacted key hides nothing -- a declaration is a name, not a value
    -- and leaves the reference pointing at a key the package does not contain.

    The POSITION comes back and the name does not. A name that needs redacting is not
    a name to write to a log, and these sentences travel through the gateway's logs;
    the agent can resolve a position against the package in its own hand.

    The field and token order is the model's sorted key order, which is what the
    package validator canonicalises to, so an index here is the index the stored
    package has.
    """
    raw_model = package.get("model")
    raw_theme = package.get("theme")
    model: Mapping[str, Any] = raw_model if isinstance(raw_model, Mapping) else {}
    theme: Mapping[str, Any] = raw_theme if isinstance(raw_theme, Mapping) else {}
    raw_types = model.get("types")
    raw_tokens = theme.get("tokens")
    types: Mapping[str, Any] = raw_types if isinstance(raw_types, Mapping) else {}
    tokens: Mapping[str, Any] = raw_tokens if isinstance(raw_tokens, Mapping) else {}
    spots: list[str] = []
    for index, name in enumerate(sorted(types), start=1):
        if isinstance(name, str) and redact_via_context(name) != name:
            spots.append(f"declared field #{index} of {len(types)}")
    for index, token in enumerate(sorted(tokens), start=1):
        if isinstance(token, str) and redact_via_context(token) != token:
            spots.append(f"theme token #{index} of {len(tokens)}")
    return spots
