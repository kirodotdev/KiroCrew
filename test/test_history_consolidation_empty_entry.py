"""An empty history answer writes no dated history line.

A cron, subagent or automation-only session with nothing worth recording must
not get a dated "No substantive conversation occurred" entry. The prompt lets
the model answer ``""``, and the V1 writer appends only a non-blank string, as
the member store's ``apply_consolidation`` does. The span is still marked
consolidated, so the same messages are not prompted again.
"""

from unittest.mock import AsyncMock

import pytest
from history_consolidation_retry_helpers import KEY, _make_consolidator, _seed_log, _total

from kiro_crew.testing.wait import finished


def _marker(log) -> int:
    return int(log.get_metadata(KEY).get("last_consolidated", 0) or 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["", "   \n\t", None, ["not", "a", "string"]])
async def test_an_empty_history_entry_writes_nothing_and_still_advances(tmp_path, entry):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._call_llm = AsyncMock(return_value={"history_entry": entry, "lessons": []})

    await finished(c._consolidate(KEY, include_history=True), "consolidation")

    c._memory.append_history.assert_not_called()
    assert _marker(log) == _total(log)


@pytest.mark.asyncio
async def test_a_real_history_entry_is_still_written(tmp_path):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    entry = "[2026-11-03 02:00] Nightly backup copied 3 files to the archive."
    c._call_llm = AsyncMock(return_value={"history_entry": entry, "lessons": []})

    await finished(c._consolidate(KEY, include_history=True), "consolidation")

    c._memory.append_history.assert_called_once_with(entry)
    assert _marker(log) == _total(log)


@pytest.mark.asyncio
async def test_the_history_prompt_allows_an_empty_entry(tmp_path):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._call_llm = AsyncMock(return_value={"history_entry": "", "lessons": []})

    await finished(c._consolidate(KEY, include_history=True), "consolidation")

    prompt = " ".join(c._call_llm.await_args.args[0].split())
    assert 'return an empty string ""' in prompt
    assert "never a placeholder" in prompt
    assert "A scheduled or automated run that did real work still gets an entry" in prompt
