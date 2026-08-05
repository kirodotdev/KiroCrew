"""RED test: _build_stream_chunk must not KeyError on a message lacking 'role'.

The helper treats role as possibly-absent at line 77 (``msg.get("role")``) but
hard-indexes ``msg["role"]`` when building the returned chunk. A slot message
dict without a 'role' key therefore raises KeyError instead of producing a JSON
chunk. This test feeds such a dict and asserts a valid chunk comes back.
"""

import json

from kiro_crew.dashboard.chat_utils import _build_stream_chunk


def test_agent_defect():
    # A message dict with no 'role' key — must build a chunk, not raise KeyError.
    result = _build_stream_chunk({"content": "hi"})
    parsed = json.loads(result)
    assert parsed["content"] == "hi"
    # Absent role degrades gracefully to an empty type (consistent with the
    # msg.get(...) defaults used everywhere else in the function).
    assert parsed["type"] == ""
