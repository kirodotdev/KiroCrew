"""Checks the split-credential-boundary test files share, written once.

Each check lives here as a mixin. A test class in
``test_split_credential_boundary_*.py`` takes the mixin as a base, so the node id stays
``<file>::<Class>::<test>``, the class keeps its own setup, and pytest resolves the
test's fixtures from that file.
"""

from __future__ import annotations

from split_credential_boundary_helpers import KEY, _on_screen, _rejoins

from kiro_crew.messaging.split import split_markdown_safe


class TheBudgetAloneHandsTheReaderTheKey:
    def test_the_budget_alone_hands_the_reader_the_key(self) -> None:
        """The control: without the redactor this is exactly what ships."""
        chunks = split_markdown_safe(self._body(), self.BUDGET)
        assert len(chunks) > 1
        assert _rejoins(chunks)
        assert KEY in _on_screen(chunks)
