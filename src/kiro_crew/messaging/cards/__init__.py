"""Channel card catalog: one typed card spec, one redacting, cap-checking render.

See ``docs/request-for-change/rfc-channel-card-catalog.md``. A gateway builder
describes a card with :mod:`.spec`, and :func:`.render.render` produces the Slack
Block Kit payload with every display string redacted and defused and every cap in
:mod:`.limits` checked.
"""

from kiro_crew.messaging.cards.limits import SLACK_LIMITS, Limits
from kiro_crew.messaging.cards.render import Platform, Rendered, render
from kiro_crew.messaging.cards.spec import (
    Actions,
    Block,
    Button,
    Card,
    CardError,
    Context,
    Fit,
    Issue,
    Section,
    Text,
)

__all__ = [
    "Actions",
    "Block",
    "Button",
    "Card",
    "CardError",
    "Context",
    "Fit",
    "Issue",
    "Limits",
    "Platform",
    "Rendered",
    "SLACK_LIMITS",
    "Section",
    "Text",
    "render",
]
