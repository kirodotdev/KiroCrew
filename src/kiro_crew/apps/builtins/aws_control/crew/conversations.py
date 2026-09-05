"""The S3 conversation reader for a persistent-memory crew. NOT PORTED YET.

The S3 half of the old ``control/observe.py`` belongs here, and is deliberately
left unwritten. The reason is what the default deploy produces.

``--memory-mode chatbot`` is the DEFAULT mode, and its whole claim is that nothing is
persisted: the driver's gate 7 in that mode asserts ``state=disabled``, the task
role is granted no S3 action at all, and gate 13 proves that too. So a chatbot
crew has no S3 conversations to read -- not few, none, structurally. Only
``--memory-mode persistent`` writes transcripts under the crew's own prefix.

Shipping the reader now would therefore add a surface whose answer is empty for
every default deployment, which is the shape that already cost this project once:
the old UI read a ``fetch-status.json`` that nothing wrote, so a stale snapshot
and a broken fetcher looked identical. An empty list is indistinguishable from a
crew that has not been asked a question yet, and neither is distinguishable from
a reader that is broken.

What the port will need when a persistent crew is worth reading, all of it
already written and none of it here:

* ``observe.py``'s ``_gather_objects`` / ``classify_layout`` -- listing a crew's
  prefix and judging whether the layout is the correct one or the doubled-prefix
  variant a fixed bug used to write.
* ``_derive_title`` -- a conversation's title from the first content line of its
  transcript, not from the metadata record.
* ``resolve_open_slots`` -- mapping open slot ids onto the session ids that
  actually have a transcript, including the thread-prefixed spelling
  (``smc-verify`` -> ``dashboard_smc-verify.jsonl``). Comparing them directly
  leaves every prefixed conversation looking idle, and matching loosely marks the
  wrong one live.
* the prefixes in ``crew/runtime/container/backup/layout.py``, which are the
  single source of truth for where the sidecar puts an object. The reader must
  read them from there rather than restate them, which is the invariant
  ``test_transcript_key_agrees_with_sidecar.py`` holds.

Whoever picks this up: read that test first. It is the one that says the front
process and the sidecar agree on a key, and a reader is the third party to that
agreement.

This module stays importable and deliberately declares nothing, so an import of
it fails at the NAME -- loudly, at the call site that wanted a reader -- rather
than returning an empty list that reads like a crew with no history.
"""

from __future__ import annotations
