# OpenCode frame corpus

Eight files on the v1 line plus five v2 captures, all live. Read `../README.md`
first for what a fixture is and what the corpus does and does not prove.

| File | Provenance | Frame classes it carries |
|---|---|---|
| `session-live.jsonl` | live, 1.18.30 | initialize response with `agentInfo.version`, `session/new` response with a `sessionId`, `agent_message_chunk`, `usage_update`, and the `session/prompt` result carrying `stopReason` |
| `tool-call-live.jsonl` | live | `tool_call`, and two `tool_call_update` frames (`in_progress`, then a terminal `failed`) -- a call the harness rejected against its own argument schema |
| `permission-request-live.jsonl` | live | `tool_call`, `session/request_permission`, and the `tool_call_update` frames through to `completed` with the command's real output |
| `session-load-live.jsonl` | live | a `session/load` from a second process: the replayed `user_message_chunk` / `agent_message_chunk` updates and the load result, which carries `configOptions` and no `modes` |
| `mcp-directive-call-live.jsonl` | live | a Crew MCP `tool_call`, its `in_progress` refinement and its terminal `tool_call_update` -- the single-underscore `kirocrew-core_<tool>` title, an empty-then-refined `rawInput`, and a result carrying a directive marker |
| `compact-live.jsonl` | live | a MANUAL `/compact`: an ordinary turn's `usage_update` and `stopReason`, then the `/compact` turn's summary `agent_message_chunk`, its far smaller `usage_update`, and its `stopReason: end_turn` with NO compaction status frame |
| `effort-live.jsonl` | live | `session/set_config_option` results for the per-model `effort` select (low, medium, high and default on a reasoning model, absent on a plain one), a `-32602` refusal of an unoffered level, and the reset to the first level after a model switch |
| `session-v2-live.jsonl` | live, 2.0.29 | initialize response with `agentInfo.version`, `session/new` response with a `sessionId` and model `configOptions`, `agent_message_chunk`, `usage_update`, and the `session/prompt` result carrying `stopReason` |
| `permission-request-v2-live.jsonl` | live, 2.0.29 | `tool_call` for `shell`, `session/request_permission` with once/always/reject options and complete `rawInput`, the client's allow, and the `tool_call_update` frames through to `completed` with the command's real output |
| `compact-v2-live.jsonl` | live, 2.0.29 | filler turn, then a manual `/compact` turn emitting `session_info_update` markers with `_meta.opencode/compaction` `started`/`completed` (`reason: manual`) and the prompt result |
| `load-v2-live.jsonl` | live, 2.0.29 | `session/load` of a just-completed session: replayed `user_message_chunk` / `agent_thought_chunk` / `agent_message_chunk` and the load result carrying `configOptions` |
| `mcp-directive-v2-live.jsonl` | live, 2.0.29 | a Crew-style MCP stdio server (`kirocrew-core`, one tool) mounted on the `session/new` array and driven to a completion carrying the directive marker |

Re-captured off `opencode acp` 1.19.1, the release the Real Adapter Contract lane pins, with each file's `initialize` frame kept so its version is read off the wire. Five drive a local Ollama model (llama3.2 3b, 16k context); `effort-live.jsonl` and `mcp-directive-call-live.jsonl` drive a local OpenAI-compatible stub instead. Every capture ran in a throwaway home and a scratch project, and was reduced at capture through `scripts/capture_acp_compaction.py`'s own reduction: the working directory reads `<cwd>`, the home directory `~`, session and message ids are synthetic, the local model id is the synthetic `local/model-1`, the command inventory is dropped and each `configOptions` select is cut to its current option -- except in `effort-live.jsonl`, whose selects ARE the evidence and keep their stub models and levels. `tool-call-live.jsonl` and `compact-live.jsonl` are **slices**, and each fixture's own `_meta.note` says what was omitted.

The five `-v2-` files are captured off `opencode acp` 2.0.29 driving a subscription model through the Crew broker instead (same redaction, same verbatim rule); their `_meta.note` records the seed and the reply.

## What the permission capture establishes

The permission frame is the one the enforcement story rests on. Kiro Crew's tool
gate runs only when a harness sends `session/request_permission` for a tool call,
and OpenCode sends it only while its own `permission` setting says to ask. The
session's read-back (`acp.harness.opencode._verify_opencode_routing`) proves that setting is in
force; it cannot prove the harness then asks. `permission-request-live.jsonl` is that
second half, observed: with `permission: ask` resolved, OpenCode emitted
`session/request_permission` before running `bash`, waited for the client's
`allow_once`, and only then ran the command.

What it does *not* pin: it was observed on OpenCode 1.19.1 (and before that on 1.18.30), and nothing in
this repository pins per-call emission across upgrades the way the read-back pins
config precedence -- `agent-host-contract.md` records the version.

The capture in `tool-call-live.jsonl` shows the other path: a call whose
arguments fail OpenCode's own schema is rejected BEFORE any permission check, so no
permission frame exists for it. Both fixtures are kept because they are different
facts about the same harness.

`permission-request-v2-live.jsonl` re-proves the same second half on 2.0.29
under the v2 ask-all seed: the frame carries once/always/reject options
(v1: allow_once-style ids, echoed back verbatim by the client either way),
complete `rawInput`, and the turn completes after the allow. The v1 capture
above stays the evidence for the v1 line; nothing here re-pins v1 behavior.

## What the v2 MCP capture establishes -- and breaks

`mcp-directive-v2-live.jsonl` is the same experiment as the v1 MCP capture --
one Crew MCP directive tool, called once -- with a different outcome. On v2
the tool is NOT flattened to a top-level `kirocrew-core_monitor_start`: the
tool index advertises it as `tools["kirocrew-core"].monitor_start`, the model
invokes it through Code Mode JS, and the permission frame arrives as action
`execute` (kind `other`) with the JS source in `rawInput.code`. A literal
fused-name call fails with "No tool named ... is currently available". The
result still carries the directive marker intact.

Three consequences, decided by the accompanying code change (all measured live on
2.0.29/2.0.30):

- Crew's per-tool MCP deny rules keyed on `<server>_<tool>` do not match v2
  calls, which all arrive as `execute` (a `tool.use` rule with the fused id
  matches nothing). Deny granularity for MCP tools is therefore reimplemented
  as server withholding: narrowed servers are withheld whole via
  `integration.use` `mcp:<server>` policy denies
  (`_opencode_v2_routing_config(..., mcp_deny_servers=...)`), and
  `_opencode_v2_unenforced_denies` marks the unenforceable per-tool rules so
  settle reports them. Native-action denies (`shell`, `read`, ...) are honored
  by the harness itself (a `shell` deny fails the call with "Blocked by
  configuration policy", no prompt).
- `directive_tool_from_call`'s exact server-qualified title match does not
  fire (the title is `execute`). The out-of-band digest path lands the
  directive off `rawInput.code`, which carries the full invocation:
  `_extract_code_mode_invocation` parses the `tools["<server>"].<tool>()`
  Code-Mode call (fail-closed), and the selector resolves only for
  `kirocrew-core` directive tools. Forgery buys nothing: the code only selects
  which parked record to claim, and the record was parked by the real tool
  call's own digest.
- The `execute` tool calls in this capture were framed for permission, but
  sibling runs under the same seed showed Code-Mode calls completing with no
  frame at all -- correlated with sessions whose default tool plane was not
  pinned to `build` via `session/set_mode`. The agent pin (shipped in the
  accompanying code change) is what makes the framed behavior the behavior;
  unpinned sessions are not evidence of anything.

`compact-v2-live.jsonl` differs from its v1 sibling in one load-bearing way:
v2 DOES emit an explicit compaction signal -- `session_info_update` frames
whose `_meta.opencode/compaction` runs `started` then `completed`
(`reason: manual`) -- where the v1 capture's whole point is that no such
frame follows and the terminal frame is the only done signal. No dedicated
`session/compact`-style method exists on v2 (`session/compact`,
`session/summarize` and `session/compaction` all answer `-32601`); the
`/compact` prompt text is the only path on both generations. Whether the
compaction waiter should key off the v2 marker instead of the terminal frame
is open -- recorded here, not decided.

## What the MCP capture establishes

`mcp-directive-call-live.jsonl` is a turn whose ONLY tool is a Crew MCP directive
tool. The server rode the `session/new` `mcpServers` array as a stdio element named
`kirocrew-core` exposing `monitor_start`; the model was a local OpenAI-compatible
stub with no credential and no network, driven to call that tool once. Three facts
come out of it, none of them inferred:

- the tool reaches the model as `kirocrew-core_monitor_start` -- server and tool
  joined by ONE underscore, where kiro-cli reports `<server>___<tool>` and the
  canonical MCP prefix form is `mcp__<server>__<tool>`;
- no `_meta.kiro` block appears anywhere in the turn, so `title` is the only
  channel that names the tool at all;
- `rawInput` is `{}` on the `tool_call` and complete on the following
  `in_progress` update, so a consumer keyed on the call's arguments can only see
  them on the refinement.

That spelling resolved to no directive tool at all before the fix:
`session_directive.directive_tool_from_call` knew `@<server>/<tool>` (KAS) and
`mcp__<server>__<tool>` (Claude), and `match_tool` splits only on a run of two or
more underscores -- so every #755 tool answered and none applied.

The capture pins how OpenCode names a mounted Crew server. Shipping code now
sends this backend its MCP array through `ACP_BACKENDS_SESSION_MCP_ARRAY`, using
the same element shape; `agent-host-contract.md` §5 describes that production
path. The remaining boundary is version-specific: this naming rule was observed
on 1.18.30 and again on 1.19.1, and nothing in this repository pins it across adapter upgrades.

## What the compaction capture establishes

This one is load-bearing for a MEMBERSHIP, so it is worth saying what it proves and
what it does not.

`ACP_BACKENDS_COMPACT` and `ACP_BACKENDS_INLINE_COMPACTION` both rest on one
question: does this harness finish a `/compact` inside the `session/prompt` turn,
emitting no separate status? `compact-live.jsonl` answers both halves in one slice:

```
usage_update       used=12528   (the seventh ordinary turn)
id=9 result        stopReason=end_turn
agent_message_chunk            (the /compact turn's summary)
usage_update       used=3720
id=10 result       stopReason=end_turn      <- and no status frame after it
usage_update       used=10135   (the next ordinary turn)
id=11 result       stopReason=end_turn
```

The ABSENCE is the evidence. No `compaction_status` frame of any kind follows the
`/compact` turn, so the turn's own terminal frame is the only done signal this
harness gives — which is why `AcpProvider.wait_for_compaction` answers from the
capability rather than waiting on the queue, and why waiting on the queue instead
would spend the whole `COMPACT_WAIT_TIMEOUT_SECS` and then recycle a session that
had just compacted correctly.

The slice also shows the SESSION shrank, not just one request: over seven growing turns `used` climbed 7566 → 12528, and the first ordinary turn after `/compact` read 10135, below the pre-compact peak. The six earlier turns are omitted for length; their series is in the fixture's `_meta.note`. A drive this short only falls if its history outgrows what opencode keeps verbatim through a compaction (`preserve_recent_tokens`, by default a quarter of the usable context), which is why the turns are filler notes rather than one-liners.

goose joined both sets on its own capture (`../goose/compact-live.jsonl`). pi has no
such capture and is therefore in neither set, even though its adapter source says it
compacts inline. See
`ACP_BACKENDS_COMPACT` for why source is not the bar this set holds its members to.
