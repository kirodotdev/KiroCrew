# ACP frame replay corpus

Recorded agent-to-client JSON-RPC frames, one directory per backend, replayed by
`test/test_acp_frame_replay.py` against a committed snapshot of the events they
turn into.

## What a fixture is

One `.jsonl` file is one frame sequence: the raw lines a backend wrote to stdout
during a slice of a session, in order, one JSON object per line. Nothing is
reordered and nothing is summarized — a fixture is what the wire carried.

The first line is not a frame. It is a provenance header the test requires:

```json
{"_meta": {"backend": "kas", "recorded": "live", "date": "2026-09-06", "agent_version": "0.54.3"}}
```

- `backend` is the canonical backend id from `src/kiro_crew/acp_backends.py`. It
  is the id, not the directory name — kiro-cli's id is the empty string, which is
  not a filename, so the directory is named through `POLICY_ID_BY_BACKEND` by
  `fixture_dir_name` in `test/acp_frame_replay_harness.py`. The test checks the
  two agree.
- `recorded` is `live` (captured off a real backend) or `synthesized` (written
  from the shapes this repo parses). There is no third value, because a corpus
  whose provenance is unstated is a corpus nobody can weigh.
- `agent_version` is the backend's own version, or `unmeasured` when it was never
  run.
- `agent_name` (live captures) is the `agentInfo.name` the backend answered
  `initialize` with. Both it and `agent_version` must agree with the capture's own
  `initialize` answer when it carries one. KAS sends no `agentInfo`, so its
  captures carry no name. For an adapter pinned in `test/real_adapters/package.json`,
  `test/test_real_adapter_fixture_pins.py` fails when `agent_version` differs from
  the pin: a bump means re-capturing, not editing the header. On a red bump PR,
  either re-capture each listed fixture at the new release and delete its
  `_STALE` entry, or accept it as stale by editing its pin in both `_STALE` and
  `_STALE_CEILING` in that test, which a reviewer then sees.

Each `<name>.jsonl` has a `<name>.expected.json` beside it holding the event
stream it replays into. That file is the actual gate: it is what fails when a
refactor changes the shape of a turn.

## Recording a fixture

`src/kiro_crew/acp/_frame_record.py` is the in-product recorder: set
`KIROCREW_ACP_RECORD_FRAMES=<dir>` and every inbound frame from both transports
is appended to `<dir>/<backend>.jsonl` with owner-only permissions. It records
what a running gateway sees, so it is the right tool when the scenario needs
Crew's own session wiring — its MCP projection, its permission policy, its auth
callback. A frame the recorder cannot turn into a line (one nested deeper than
half the interpreter's recursion limit, for example) is skipped while recording
carries on, with at most one `skipped N frame(s)` warning a minute in the
gateway log, so a capture whose run logged one has a gap there. A count still
held when the minute is up, recording stands down or the writer stops is logged
then, so the warnings add up to every frame that is missing.

It is not the only route, and for most classes it is not the shortest one. A
backend that speaks ACP over stdio can be driven directly: spawn it, send
`initialize`, `session/new` and `session/prompt`, answer
`session/request_permission`, and keep every agent-to-client line. That reaches
the seven required classes in one turn without a gateway, and it is how the
`kiro`, `kas`, `claude` and `opencode` captures here were taken.

Either way, split the capture into scenario files, add the `_meta` header, review
it (below), and generate the snapshot:

```
python3 scripts/update_acp_frame_snapshots.py
```

That script is the only writer, and it takes no options -- it always rewrites a
stale snapshot. `test/test_acp_frame_replay.py` is strictly read-only and has no
update mode, because a test must not create files in the repo that outlive the run
-- the rule is `no-test-side-effects` in `AUTOSDE.yaml`, and its own history is a
file a test left at the repository root and shipped to main. To CHECK without
writing, run the test: it fails on a stale snapshot, so a second checker in the
script would be a flag with no caller.

Commit the `.jsonl` and the `.expected.json` together. A snapshot rewritten in a
commit that also changes the dispatch layer is the point at which a reviewer gets
to see the event diff, so never regenerate one to make a red test green without
saying in the review why the events changed.

Keep a fixture under 50 frames. The corpus is read by people.

## Redaction

The recorder runs each frame through `redact_text` — the same credential and
exfiltration-URL scrub the dashboard path runs — and replaces the recording user's
home directory with `~`. That is a floor, not a guarantee. It does not know an
account id, an internal hostname, a customer name or a private file path when it
sees one, and a capture driven by a script of your own has had only the scrub that
script applied.

So a recording is reviewed by hand before it is committed. Read every line and
remove:

- tokens, keys and session credentials, including anything the scrub tagged but
  left recognizable;
- absolute paths, usernames and machine names;
- account ids, ARNs and internal endpoints;
- prompt and tool-output text that was not written for this corpus;
- anything that describes what the recording host or account HAS rather than what
  the product IS: installed agents, skills, MCP servers, steering documents, and
  model entitlements. This is the class that reads as ordinary product data on a
  quick look, so read it twice. A model catalog is the trap — it looks like a
  published price list and is actually a per-account entitlement list carrying
  internal-only markers and unreleased codenames.

Search for the marker words an internal build uses (`[Internal]`, a fleet name, a
codename you do not recognise) before committing, and do not rely on
`internal-content-scan`: it cannot know a codename it has never seen.

`scripts/check_acp_frame_host_data.py` holds the part of that review a machine can:
twelve marker patterns over every fixture, shrink-only, and baselined per FILE and per
MARKER CLASS so a new fixture cannot inherit an exemption and an old one cannot acquire
a second. It is a floor under the hand review, not a replacement for it — it knows the
markers it was given and nothing about a codename it has never seen either.

Prefer re-recording against throwaway data over editing a capture down: an
edited frame is no longer evidence of what the wire carried, and the `_meta`
header claims it is.

Two of the marker classes cannot be re-recorded away, because the value is minted
by the agent rather than read from the host: a session or request uuid, and a
scratch run id. The sanctioned remedy for those is normalization AT CAPTURE, in the
capture script's own reduction step -- the id is replaced with a fixed synthetic
value (`goose-session-1`, `perm-1`) before the frame is written, the way the goose
corpus does it, and the `_meta.note` records that the ids are synthetic. A frame
reduced that way is still evidence: the id was never the fact the fixture pins.
Until a pre-gate corpus is re-captured through such a step, its uuid and run-id
entries are an accepted debt, held in the baseline and pinned entry by entry in
`test_acp_frame_host_data.py`, and paying one down means re-capturing the file,
not editing the id in place.

## What the corpus does not pin

`replay_frames` in `test/acp_frame_replay_harness.py` mirrors the routing the two
reader loops perform; it does not call the loops. The parsers are the real ones,
so a change inside a `_dispatch` parser fails the snapshot. A change that moves
translation INTO `AcpRuntime._reader_loop` or `AcpClient` and out of a parser does
not. Read a green snapshot as "the parsers still behave", not as "the product
stream is unchanged".

That gap closes when the Agent SDK driver lands: retarget the harness at the
driver's public entry point and delete the mirrored routing.

## A new backend must add a directory

`test_every_known_backend_has_fixtures` fails — it does not skip — when an id in
`ACP_BACKENDS_KNOWN` has no directory here, and
`test_every_backend_covers_the_required_frame_kinds` fails when a directory does
not reach all of: an initialize response, a `session/new` response, an
`agent_message_chunk` turn, a `tool_call`, a `tool_call_update`, a
`session/request_permission` frame, and a response carrying a `stopReason`.

An initialize response is recognised by its `protocolVersion`, and the agent
version is a SECOND assertion over that same frame. The two are separate because
`agentInfo` is optional in ACP and one shipped backend omits it: KAS 0.63.3
answers with `protocolVersion`, `agentCapabilities` and `authMethods` only. A
backend that sends no version is named in `_BACKENDS_WITHOUT_AGENT_VERSION` in
the test, with the evidence, so an omission is a decision someone wrote down
rather than a signal that quietly decayed for everyone.

This is the second requirement in the host contract enforced by behaviour rather
than by prose; see `docs/system-specs/modules/agent-host-contract.md`.

## Provenance of what is committed today

All eight backends carry a live capture reaching all seven required classes.
`codex/` also keeps a synthesized `session.jsonl`. Stated plainly because it
bounds what the corpus proves: a synthesized fixture locks the dispatch layer's
behaviour against refactoring, which is what it was built for, and it does
**not** prove that the backend really emits those shapes. Only a live file
carries that second proof.

`kiro/notifications.jsonl` is synthesized too, but it is not a required-class
carrier -- the directory's live file already reaches all seven, and this one pins
`_kiro.dev/*` members no capture here reaches. Its row says which.

### The rule

**Any host-derived enumeration is unshippable unless every entry in it is
product-defined or publicly announced.** Everything a capture is allowed to have
removed follows from that one sentence, and every removal is named in the file's
own `_meta.note`, so a reader knows the corpus was pruned and on what grounds.

Being real is not the same as being publishable. A live frame is host data until
you have shown otherwise, and the direction of the trade is worth stating: a
capture is strictly more faithful than a hand-written fixture AND strictly worse
on disclosure. Sanitization is how that is resolved. "It is what the wire carried"
is not.

Prune to the MINIMUM the seven required frame classes need. A corpus exists to pin
frame TRANSLATION, and translation does not read an inventory — it reads the shape
around one. Keep the product's own routing entry so the field still parses as a
list of the right thing, drop the rest, and a reviewer loses nothing: they learn
no more from 28 model names than from one. A smaller fixture is a smaller
disclosure surface for as long as the repository exists.

### What that comes to today

The recording home directory reads as `~`.

The recording host's installed-agent inventory is cut back to product-defined
entries. Not cosmetic: `kiro`'s `session/new` response lists 44 host agents by
name and description.

The recording account's model entitlements must be reduced to the minimum shape
the parser needs, with descriptions emptied when they reveal the model a routing
entry resolves to. `models.availableModels` and the `model` `configOption` are the
sharpest payloads: entries can carry internal-only markers, unreleased codenames,
internal fleet names, and rate multipliers. A served-model name elsewhere in a
frame must be removed too, including a local model supplied for the capture.

Some Codex and OpenCode captures predate that floor and still carry local
provider or model identifiers and catalog entries. They are remediation debt,
not precedent: re-capture those files through a reduction step rather than
editing a live frame in place.

Neither inventory can be avoided by re-recording against a throwaway home,
because relocating `HOME` moves kiro-cli's credential store with it, so a
throwaway home cannot authenticate. A bulk notification frame carrying the same
inventory (`_kiro/mcp/status`, `_kiro/tools/didChange`,
`_kiro/steering/documents_changed`, `available_commands_update`) is left out of
the slice instead.

This is not an ordinary tidy-up. These files land in a public repository, and a
revert cleans HEAD, not history. `internal-content-scan` cannot know a codename it
has never seen, so it is not a safety net — the review step below is.

| Directory | Backend id | Provenance | Why |
|---|---|---|---|
| `kiro/` | `""` | **live** | `session.jsonl`: a slice of one `kiro-cli acp` 2.21.4 turn reaching all seven classes, plus the `_kiro.dev/session/update` `tool_call_chunk` only kiro-cli sends. `compaction-live.jsonl`: a kiro-cli 2.32.0 `/compact` turn and the `_kiro.dev/compaction/status` `started` / `completed` pair that follows its `end_turn`, between `_kiro.dev/metadata` readings. `notifications.jsonl` stays synthesized: it pins the rest of the `_kiro.dev/*` family, and a turn that runs one shell command reaches five of its members (`metadata`, `mcp/server_initialized`, `subagent/list_update`, `commands/available`, `session/update`) but never `clear/status`, `agent/switched`, `mcp/server_init_failure` or `mcp/oauth_request` — each needs its own scenario, and the OAuth one needs an MCP server mid-authorization. Three more kiro-cli 2.32.0 captures are membership evidence: `steering-live.jsonl` (a `_session/steer` mid-turn, `{queued: true}`, and the `AgentExecutionUserMessageQueued` / `AgentExecutionSteeringInjected` updates), `saved-spec-live.jsonl` (`--agent probe` running a saved spec's MCP tool) and `commands-execute-live.jsonl` (`_kiro.dev/commands/execute` `effort` answered `{success: true}`). `side-read-only-live.jsonl`, on kiro-cli 2.34.0: a side session on the derived `--readonly` spec, four turns (`fs_read`, `grep`, `execute_bash`, `fs_write`); the last two raised `session/request_permission` and were rejected. |
| `kas/` | `kas` | **live** | `session.jsonl`: a slice of one KAS 0.63.3 turn through `kiro-cli acp --agent-engine v3 --auth-method cli`, the production relay spawn, reaching all seven classes. Its initialize response carries no `agentInfo`, which is why KAS is named in `_BACKENDS_WITHOUT_AGENT_VERSION`. Four more captures off KAS 0.66.43 (kiro-cli 2.32.0), the evidence for three memberships and one exclusion. `steering-live.jsonl`: a `_session/steer` sent mid-turn, its `{queued: true}` answer, and the `steering_queued` / `steering_injected` / `steering_cleared` kinds, which arrive as `session_info_update` with the kind under `_meta.kiro`. `compact-live.jsonl`: the `_kiro/session/compact` verb after three turns, its `summarization_completed` and `{success: true}`, and a lower `context_usage` on the next turn. `side-read-only-live.jsonl`: a side session on the derived `--readonly` spec where every tool call raised `session/request_permission`. `commands-refused-live.jsonl`, same build: three `_kiro.dev/commands/execute` requests (`tools`, `context`, `effort`), each answered -32603 because the engine has no persistence classification for the method, which is why KAS is outside `ACP_BACKENDS_KIRO_SLASH_COMMANDS`. Two more on KAS 0.66.43: `saved-spec-live.jsonl` (a saved spec projected to `customAgents` and activated with `session/set_mode`, its MCP tool called) and `member-dispatch-live.jsonl` (a member DM session calling `@kirocrew-dashboard/session_status`). `bundle-tool-path.jsonl` is a second **live** capture, off the bundle route (`agent.kas_bundle`: KAS 0.66.33 spawned from Kiro Crew's own pinned engine with no kiro-cli, recorded through a live gateway with `KIROCREW_ACP_RECORD_FRAMES`): the tool path only -- `tool_call`, `session/request_permission`, the `tool_call_update` frames through `completed`, the chunks, the turn-end `session_info_update` and the `stopReason` response -- so `test_kas_bundle_hooks_parity.py` can lay it beside `session.jsonl` frame class by frame class. Ids and the workspace path were normalized at capture (its `_meta.note` says which), so it carries no baseline entry in `check_acp_frame_host_data.py`. |
| `claude/` | `claude` | **live** | All on `@agentclientprotocol/claude-agent-acp` 0.94.0, the adapter delegating to the host `claude` executable. `session.jsonl`: a slice of one turn reaching all seven classes. It advertises `loadSession: true`, and its `session/request_permission` names the tool under `name`. `compact-live.jsonl`: a `/compact` turn finished inside the turn, `preTokens` 31250 -> `postTokens` 4148. `mcp-stdio-mount-live.jsonl`: an `_acp_server_entry`-shaped element mounted, its tool asked and completed. `saved-spec-projection-live.jsonl`: the server the claude mirror projected out of a saved spec, the same round trip. `side-read-ask-live.jsonl` and `side-read-no-ask-live.jsonl`: a side session under Crew's side settings, with and without the Read, Grep and Glob ask rules; with them every read asks, without them in-cwd reads run unasked, and calls under a denied path fail without asking. |
| `codex/` | `codex` | **live** + synthesized | Three captures off `codex-acp` 2.2.2, the Real Adapter Contract lane's pin, driven against a loopback model stub. `session-live.jsonl`: one turn reaching all seven classes, its stub asking for an escalated `exec_command` so the adapter sends a `tool_call`, a `session/request_permission` and a `tool_call_update` with the terminal output, plus the `_auth/status_update` sent before `session/new` and the `session_info_update` frames carrying `_meta.codex`. `steering-live.jsonl`: the lane's `_session/steering` run, its initialize answering `_meta.steering.supported: true` and a mid-turn steer answered `injected`. `compact-live-2.2.2.jsonl`: a manual `/compact` pair carrying no `_meta`. `session.jsonl` is synthesized from the parsers in `src/kiro_crew/acp/_dispatch_parts/`, from before a live tool turn existed. |
| `opencode/` | `opencode` | **live** | Seven captures off `opencode acp` 1.19.1 (five driving a local Ollama model, `effort-live.jsonl` and `mcp-directive-call-live.jsonl` a local stub), all seven required classes reached live, plus a `session/load` result (`session-load-live.jsonl`: replayed conversation, `configOptions`, no `modes`). `session-live.jsonl`: the initialize response, the `session/new` response, an `agent_message_chunk` turn, a `usage_update` and the `stopReason` response, in order. `tool-call-live.jsonl`: a `tool_call` and two `tool_call_update` frames from a call the harness rejected against its own argument schema. `permission-request-live.jsonl`: `tool_call`, the `session/request_permission` frame OpenCode sent with `permission: ask` in force, and the `tool_call_update` frames through `completed` with the command's real output. `mcp-directive-call-live.jsonl`: an MCP tool call -- a `kirocrew-core` stdio element on the `session/new` array exposing `monitor_start` -- carrying opencode's own tool naming (`title` = `kirocrew-core_monitor_start`, ONE underscore, no `_meta.kiro`), `rawInput` empty on the `tool_call` and complete on the refinement, and the tool's result text with its directive marker intact. `compact-live.jsonl`: an ordinary turn followed by a manual `/compact` turn whose usage falls and whose terminal `stopReason` is the only completion signal; no compaction-status frame follows. Reduced at capture (synthetic ids, home `~`, working directory `<cwd>`, selects cut); see `opencode/README.md`. |
| `pi/` | `pi` | **live** | Every capture is off `pi-acp` 0.0.34 spawning `pi` 1.3.0, the Real Adapter lane's pin, driving a scripted loopback model, with all seven required classes reached live; ids and the working directory are normalized at capture (see `pi/README.md`). `session-live.jsonl`: initialize, `session/new` (a `model` select of `provider/model` ids, a `thought_level` select, `modes`), `available_commands_update`, a `tool_call` + two `tool_call_update` frames for a read pi rejected against its own schema, the chunk and the `stopReason` result. `permission-request-live.jsonl`: the `session/request_permission` pi-acp forwards from Kiro Crew's gate extension (pi has no gate of its own; the frame's `toolCall` describes the confirm DIALOG and the real call rides in its message as a JSON envelope), then the updates through `completed`. `session-load-live.jsonl`: a `session/load` from a second process, replayed conversation, and a result that carries `modes`. `member-panel-mount-live.jsonl`: a crew member session's resolver-composed panel element registered through the gate extension's handoff, its `panel_publish` call gated by `session/request_permission`, and the panel server's own answer -- the evidence `ACP_BACKENDS_MEMBER_PANEL` holds pi to. |
| `goose/` | `goose` | **live** | Five captures off `goose acp` 1.50.1, with all seven required classes reached live. `handshake-live.jsonl` records initialization, session creation, absent-session load, and listing; `turn-live.jsonl` records a builtin tool permission round trip; `mcp-stdio-mount-live.jsonl` proves a stdio MCP round trip; `mcp-stdio-dropped-live.jsonl` records an unstartable element being dropped without failing session creation; `session-load-live.jsonl` records mode persistence across processes. |
| `deepseek/` | `deepseek` | **live** | Six captures off `dsh --profile acp` (bridge version 0.0.1; harness package 0.1.5-rc.2 for the id-reuse capture) driving a locally served model. `handshake-live.jsonl`: the initialize response, a `session/new` response, the `session/load` **rejection** (`-32601`, the observation `ACP_BACKENDS_RESUME_WITHOUT_LOAD` rests on) and the `session/list` result. `turn-live.jsonl`: one real turn end to end -- `usage_update`, `tool_call`, `tool_call_update`, `agent_message_chunk` and the `stopReason` response. `mcp-stdio-mount-live.jsonl`: a stdio MCP mount completing a ROUND TRIP -- a broker-stub-shaped element pointing at a real server, then the `tool_call` and result for `mcp__crew-probe__crew_probe_echo` -- which is the observation `ACP_BACKENDS_SESSION_MCP_ARRAY` membership rests on. `mcp-stdio-rollback-live.jsonl`: the same element with an unstartable command, which fails `session/new` WHOLE rather than being dropped. `permission-request-live.jsonl`: the `session/request_permission` frame the harness sends once Crew's gate plugin is composed into it, with the `tool_call` before it and the `completed` update after -- the class #10373 had to synthesize, because without that plugin this harness's sandbox decides a tool call itself and asks for nothing. `tool-call-id-reuse-live.jsonl`: the SAME `toolCallId` (`call_0`) across two consecutive turns of one session, each with its own permission frame and its own terminal update -- the premise the shared gate tripwire's per-call state consumption rests on, captured rather than asserted. Host data pruned under a sweep that refuses to finish if any survives; see `deepseek/README.md`. |

Replacing any row with a live capture is a strict improvement and needs no
change to the test. Record it, set `recorded` to `live`, fill in the real
`agent_version`, regenerate the snapshot, and update this table.

Read the event diff when you do. Replacing a synthesized fixture is the one
moment a wrong assumption about a backend becomes visible, and it earns its keep:
the `kiro` capture showed `_kiro.dev/session/update` carrying the PARENT turn's
own `tool_call_chunk` under the parent's own `sessionId`, on the same method a
child's update arrives on. Both dispatch paths read that as a sub-agent, so an
ordinary turn raised a sub-agent whose id was the session the user was already
watching, once per tool call. The guard now scopes the activity events to a frame
naming a different session.
