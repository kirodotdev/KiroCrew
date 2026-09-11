# OpenCode frame corpus

Four files, all live. Read `../README.md` first for what a fixture is and what
the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `session-live.jsonl` | live | initialize response with `agentInfo.version`, `session/new` response with a `sessionId`, `agent_message_chunk`, `usage_update`, and the `session/prompt` result carrying `stopReason` |
| `tool-call-live.jsonl` | live | `tool_call`, and two `tool_call_update` frames (`in_progress`, then a terminal `failed`) -- a call the harness rejected against its own argument schema |
| `permission-request-live.jsonl` | live | `tool_call`, `session/request_permission`, and the `tool_call_update` frames through to `completed` with the command's real output |
| `session-load-live.jsonl` | live | a `session/load` from a second process: the replayed `user_message_chunk` / `agent_message_chunk` updates and the load result, which carries `configOptions` and no `modes` |

Captured off `opencode acp` 1.18.30 driving a local Ollama model, agent-to-client
lines verbatim, with the recording user's home directory replaced by `~`.
`tool-call-live.jsonl` is a **slice**: the 430 `agent_message_chunk` frames the
model emitted around the tool call are omitted for length, and the fixture's own
`_meta.note` says so.

## What the permission capture establishes

The permission frame is the one the enforcement story rests on. Kiro Crew's tool
gate runs only when a harness sends `session/request_permission` for a tool call,
and OpenCode sends it only while its own `permission` setting says to ask. The
session's read-back (`AcpClient._verify_opencode_routing`) proves that setting is in
force; it cannot prove the harness then asks. `permission-request-live.jsonl` is that
second half, observed: with `permission: ask` resolved, OpenCode emitted
`session/request_permission` before running `bash`, waited for the client's
`allow_once`, and only then ran the command.

Two things it does *not* pin. It was observed on OpenCode 1.18.30, and nothing in
this repository pins per-call emission across upgrades the way the read-back pins
config precedence -- `agent-host-contract.md` records the version. And the
recording model garbled the echo text (`kirocrew-op encode-live`); that is the model,
and it is left verbatim because an edited frame is no longer evidence of what the
wire carried.

The earlier capture in `tool-call-live.jsonl` shows the other path: a call whose
arguments fail OpenCode's own schema is rejected BEFORE any permission check, so no
permission frame exists for it. Both fixtures are kept because they are different
facts about the same harness.
