"""Platform caps a rendered card is measured against, after redaction.

Each Slack value is the platform's own documented cap, never a tighter house
number. That is what lets a migrated builder declare ``Fit.REFUSE`` on text it
never cut: a payload Slack accepts today is under every cap here, so it still
renders. A caller can pass tighter :class:`Limits` to ``render``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    blocks: int
    section_text: int
    context_items: int
    buttons: int
    button_label: int
    button_value: int
    action_id: int
    block_id: int


#: Block Kit, from https://docs.slack.dev/reference/block-kit/ : blocks per message,
#: the section block's text (also the cap on a context element's text), the
#: context block's elements, the actions block's elements, and the button
#: element's text, value and action id. ``block_id`` is every block's id.
SLACK_LIMITS = Limits(
    blocks=50,
    section_text=3000,
    context_items=10,
    buttons=25,
    button_label=75,
    button_value=2000,
    action_id=255,
    block_id=255,
)
