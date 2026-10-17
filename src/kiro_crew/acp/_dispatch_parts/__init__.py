"""The owner modules :mod:`kiro_crew.acp._dispatch` is composed from.

``kiro_crew.acp._dispatch`` is the import path and the patch surface of the shared ACP
dispatch helpers: ``AcpClient``, ``AcpRuntime`` and the transports import them from it,
and the tests patch them there. The facade keeps the session handshake shapes, the
per-turn metadata, refusal and usage parsers and the notification classifier. Each
module here owns one other responsibility; the facade imports them lowest layer first,
and a module imports only the ones before it, never the facade:

* ``tool_call_identity`` -- which tool a ``tool_call`` frame names: the shell-kind
  test, the transport-aware ``ToolCallIdentity`` verdict, the bounded harness tool ids
  and the harness-authored ``_meta`` identity channels;
* ``surfaced_text`` -- text made safe and readable before it is surfaced: the one
  redaction policy, length-safe backend ids, the unified edit diff, the pill title and
  purpose line, and the JSON encode that degrades instead of raising;
* ``tool_call_provenance`` -- the per-turn tool-call provenance caches: their scoped
  key, caps, overflow record and admission, the release at a terminal status, and the
  liveness keys over the same in-flight calls;
* ``adapter_signals`` -- claude-agent-acp and codex-acp signals that ride ordinary
  frames: compaction notices, adapter-written errors, background-launch records and
  codex compaction updates;
* ``tool_result_events`` -- a ``tool_call_update``'s output as ``EVENT_TOOL_RESULT``:
  content and ``rawOutput`` text, the unrenderable-content diagnostic, the output
  measure and the ``todo_list`` snapshot;
* ``permission_requests`` -- a ``session/request_permission`` as
  ``EVENT_PERMISSION_REQUEST``: option kinds and the least-destructive reject, the
  gate-extension envelope, MCP identity and approval, KAS consent and the event
  builder itself;
* ``session_update_parser`` -- the ``session/update`` parser: chunk text and message
  ids, the ``tool_call`` and refinement builders, and the dispatch over them.

The facade serves every name these modules define, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries a patch of ``kiro_crew.acp._dispatch.<name>``
to every module that binds it, so the moved code reads its own globals unchanged.
"""
