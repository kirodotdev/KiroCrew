"""Consolidation must never overwrite a memory file with a placeholder body.

The consolidation prompt asks the model for the COMPLETE updated
preferences/projects file. A model occasionally answers with a protocol word
instead (the literal string ``unchanged``, ``(empty)``, …). Writing that
placeholder destroys the file, and because the next consolidation prompt embeds
the file's current content verbatim, the placeholder then primes every later
pass to echo it into the other memory file too — a self-reinforcing loop that
keeps both files destroyed. ``_is_plausible_memory_file`` gates the write on the
mandated markdown header; these tests pin the gate and the write path around it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.history import HistoryConsolidator, _is_plausible_memory_file

_PREFS_HEADER = "# User Preferences"
_PROJECTS_HEADER = "# Active Projects"


class TestIsPlausibleMemoryFile:
    def test_real_file_passes(self):
        body = "# User Preferences\n\n- Prefers concise answers\n"
        assert _is_plausible_memory_file(body, _PREFS_HEADER)

    def test_leading_whitespace_still_passes(self):
        body = "\n\n# Active Projects\n\n## Something\n"
        assert _is_plausible_memory_file(body, _PROJECTS_HEADER)

    @pytest.mark.parametrize(
        "placeholder",
        [
            "unchanged",
            "Unchanged.",
            "(empty)",
            "no changes needed",
            "N/A",
            "",
            "   \n",
        ],
    )
    def test_placeholders_rejected(self, placeholder):
        assert not _is_plausible_memory_file(placeholder, _PREFS_HEADER)
        assert not _is_plausible_memory_file(placeholder, _PROJECTS_HEADER)

    def test_wrong_header_rejected(self):
        # A projects body must not be accepted as a preferences file (and vice
        # versa): a swapped write would silently destroy the target file.
        projects_body = "# Active Projects\n\n## Thing\n"
        assert not _is_plausible_memory_file(projects_body, _PREFS_HEADER)

    @pytest.mark.parametrize(
        "body",
        [
            "unchanged",
            "Unchanged.",
            "(unchanged)",
            "**unchanged**",
            "_unchanged_",
            "~~unchanged~~",
            "*no changes needed*",
            "no changes needed",
            "No changes required.",
            "N/A",
            "none",
            "same as before",
        ],
    )
    def test_header_plus_placeholder_body_rejected(self, body):
        # GPT 5.6 review finding: a header line followed by a placeholder body
        # must be rejected too — the header alone does not make it a file.
        # Markdown emphasis wrapping must not bypass the set.
        for header in (_PREFS_HEADER, _PROJECTS_HEADER):
            assert not _is_plausible_memory_file(f"{header}\n\n{body}", header)

    def test_header_prefix_not_exact_line_rejected(self):
        # The header must be the exact first line, not merely a prefix.
        assert not _is_plausible_memory_file(
            "# User Preferences (unchanged)\n\n- real content here\n",
            _PREFS_HEADER,
        )

    def test_header_with_empty_body_accepted(self):
        # An empty body after the exact header is the legitimate COMPLETE file
        # when the last entry is deleted (final project goes inactive);
        # rejecting it would pin the stale entry forever (GPT 5.6 finding).
        assert _is_plausible_memory_file("# User Preferences\n\n", _PREFS_HEADER)
        assert _is_plausible_memory_file("# Active Projects", _PROJECTS_HEADER)

    def test_header_with_tiny_substantive_body_passes(self):
        # No size floor: a single tiny bullet is a legitimate memory file, and
        # rejecting it would silently lose the learned preference while the
        # consolidation marker advances past it (GPT 5.6 review finding).
        assert _is_plausible_memory_file(
            "# User Preferences\n\n- Vim\n", _PREFS_HEADER
        )

    def test_header_with_substantive_body_passes(self):
        assert _is_plausible_memory_file(
            "# User Preferences\n\n- Prefers concise answers\n", _PREFS_HEADER
        )


def _make_consolidator(memory: MagicMock) -> HistoryConsolidator:
    log = MagicMock()
    log.snapshot_for_consolidation.return_value = (
        [{"role": "user", "content": "hi"}],
        1,
        0,
    )
    log.get_metadata.return_value = {}
    log.consolidation_retry_state.return_value = (0, 0.0)
    return HistoryConsolidator(
        log=log,
        memory=memory,
        sessions=None,
        vector_store=None,
        migrated=False,
    )


def _make_memory(prefs: str, projects: str) -> MagicMock:
    memory = MagicMock()
    memory.read_preferences.return_value = prefs
    memory.read_projects.return_value = projects
    return memory


class TestConsolidatePlaceholderGuard:
    @pytest.mark.asyncio
    async def test_placeholder_update_is_discarded(self):
        """The literal 'unchanged' must never reach write_preferences/projects."""
        memory = _make_memory(
            "# User Preferences\n\n- real content\n",
            "# Active Projects\n\n## Real project\n",
        )
        c = _make_consolidator(memory)
        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {
                "preferences_update": "unchanged",
                "projects_update": "unchanged",
            }
            await c._consolidate("k", include_history=False)

        memory.write_preferences.assert_not_called()
        memory.write_projects.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_update_is_written(self):
        """The gate must not block a genuine full-file update."""
        memory = _make_memory(
            "# User Preferences\n\n- old\n",
            "# Active Projects\n\n## Old\n",
        )
        c = _make_consolidator(memory)
        new_prefs = "# User Preferences\n\n- old\n- new fact\n"
        new_projects = "# Active Projects\n\n## New project\n"
        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {
                "preferences_update": new_prefs,
                "projects_update": new_projects,
            }
            await c._consolidate("k", include_history=False)

        memory.write_preferences.assert_called_once_with(new_prefs)
        memory.write_projects.assert_called_once_with(new_projects)

    @pytest.mark.asyncio
    async def test_shrunk_but_valid_update_is_written(self):
        """No size floor: a consolidation that halves a bloated file is legit."""
        bloated = "# Active Projects\n\n" + "\n".join(
            f"## Stale project {i}\n- detail\n" for i in range(40)
        )
        memory = _make_memory("# User Preferences\n", bloated)
        c = _make_consolidator(memory)
        trimmed = "# Active Projects\n\n## Only live project\n"
        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {"projects_update": trimmed}
            await c._consolidate("k", include_history=False)

        memory.write_projects.assert_called_once_with(trimmed)

    @pytest.mark.asyncio
    async def test_omitted_update_keys_write_nothing(self):
        """Omitting the key is the sanctioned no-change path — nothing written."""
        memory = _make_memory("# User Preferences\n", "# Active Projects\n")
        c = _make_consolidator(memory)
        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {"history_entry": "something happened"}
            await c._consolidate("k", include_history=False)

        memory.write_preferences.assert_not_called()
        memory.write_projects.assert_not_called()

    @pytest.mark.asyncio
    async def test_prompt_sanctions_omitting_and_forbids_placeholders(self):
        """The prompt must offer omission for no-change and forbid placeholders,
        so the model is never tempted to echo a file or answer with a protocol
        word."""
        memory = _make_memory("# User Preferences\n", "# Active Projects\n")
        c = _make_consolidator(memory)
        with patch.object(c, "_call_llm", new_callable=AsyncMock) as llm:
            llm.return_value = {}
            await c._consolidate("k", include_history=False)

        prompt = llm.call_args.args[0]
        assert prompt.count("OMIT this key entirely") == 2
        assert prompt.count("placeholder word like 'unchanged'") == 2
