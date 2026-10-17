"""The alias-refusing ``yaml.SafeLoader`` that readers of untrusted YAML build on.

``yaml.safe_load`` constructs no arbitrary objects, but it still expands every
``*alias`` into a shared reference, so a file of a few hundred bytes can compose
a graph exponentially larger than itself (the "billion laughs" shape), and any
consumer that walks or serializes the result pays for the expansion.
:class:`NoAliasSafeLoader` refuses the alias instead. Every loader in this
package that refuses aliases subclasses it, so the refusal has one
implementation to audit and harden.

A leaf on purpose: it imports nothing from ``kiro_crew``, so any module can
build on it without an import cycle.
"""

from __future__ import annotations

from typing import Any

import yaml  # type: ignore[import-untyped]


class NoAliasSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses a YAML alias at compose time.

    An alias (``*name``) is refused when the composer reaches it, before any
    node is shared, so the loaded value is a tree whose size is bounded by the
    file's. An anchor (``&name``) that nothing references has nothing to
    amplify and loads as its plain value.

    The refusal is a ``yaml.composer.ComposerError`` (a ``yaml.YAMLError``)
    marked at the alias. A subclass words it for its own readers by overriding
    :meth:`alias_problem`.
    """

    def alias_problem(self, anchor: str) -> str:
        """The problem text the refusal of an alias to *anchor* carries."""
        return "found alias, which is not allowed"

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            event = self.get_event()
            raise yaml.composer.ComposerError(
                None, None, self.alias_problem(event.anchor), event.start_mark
            )
        return super().compose_node(parent, index)
