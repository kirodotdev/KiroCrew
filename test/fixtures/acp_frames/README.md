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

Each `<name>.jsonl` has a `<name>.expected.json` beside it holding the event
stream it replays into. That file is the actual gate: it is what fails when a
refactor changes the shape of a turn.

## Recording a fixture

`src/kiro_crew/acp/_frame_record.py` is the in-product recorder: set
`KIROCREW_ACP_RECORD_FRAMES=<dir>` and every inbound frame from both transports
is appended to `<dir>/<backend>.jsonl` with owner-only permissions. It records
what a running gateway sees, so it is the right tool when the scenario needs
Crew's own session wiring — its MCP projection, its permission policy, its auth
callback.

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

Prefer re-recording against throwaway data over editing a capture down: an
edited frame is no longer evidence of what the wire carried, and the `_meta`
header claims it is.

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

Four of the five backends carry a live capture reaching all seven required
classes. `codex/` carries a live capture of four of them beside a synthesized
file holding the other three, because a corpus is judged per directory and a
four-class file cannot satisfy the seven-class gate on its own; its row says what
stopped the rest. Stated plainly because it
bounds what the corpus proves: a synthesized fixture locks the dispatch layer's
behaviour against refactoring, which is what it was built for, and it does
**not** prove that the backend really emits those shapes. Only a live file
carries that second proof.

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

The recording ACCOUNT's model entitlements go the same way, keeping only the
product's routing entry (`auto`, `default`) with its description emptied, because
that description names the model the entry resolves to. `models.availableModels`
and the `model` `configOption` are that payload, and they are the sharper case:
the entries carry internal-only markers, unreleased codenames, internal fleet
names and rate multipliers. A served-model name anywhere else in a frame goes too
— including a local model you supplied yourself — so a capture names no model at
all.

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
| `kiro/` | `""` | **live** | `session.jsonl`: a slice of one `kiro-cli acp` 2.21.4 turn reaching all seven classes, plus the `_kiro.dev/session/update` `tool_call_chunk` only kiro-cli sends. `notifications.jsonl` stays synthesized: it pins the whole `_kiro.dev/*` family, and a turn that runs one shell command reaches five of its members (`metadata`, `mcp/server_initialized`, `subagent/list_update`, `commands/available`, `session/update`) but never `compaction/status`, `clear/status`, `agent/switched`, `mcp/server_init_failure` or `mcp/oauth_request` — each needs its own scenario, and the OAuth one needs an MCP server mid-authorization. |
| `kas/` | `kas` | **live** | `session.jsonl`: a slice of one KAS 0.63.3 turn through `kiro-cli acp --agent-engine v3 --auth-method cli`, the production relay spawn, reaching all seven classes. Its initialize response carries no `agentInfo`, which is why KAS is named in `_BACKENDS_WITHOUT_AGENT_VERSION`. `steering.jsonl` stays synthesized: a mid-turn steer needs a second prompt sent while the first is still running, which the one-shot capture does not do. |
| `claude/` | `claude` | **live** | `session.jsonl`: a slice of one `@agentclientprotocol/claude-agent-acp` 0.76.0 turn reaching all seven classes, the adapter delegating to the host `claude` executable. Two shapes here differ from what was assumed: it advertises `loadSession: true`, and its `session/request_permission` params carry no `sessionId` and name the tool under `name`. |
| `codex/` | `codex` | **live** + synthesized | `session-live.jsonl` is a capture off `codex-acp` 1.11.0 reaching four classes: the initialize response with `agentInfo.version`, the `session/new` response with a `sessionId` and its `configOptions`, `agent_message_chunk`, and the `stopReason` response, plus the `_auth/status_update` the adapter sends before `session/new` and the `session_info_update` frames carrying `_meta.codex`. The three tool classes (`tool_call`, `tool_call_update`, `session/request_permission`) stay in the synthesized `session.jsonl`, because reaching them needs a model that emits a tool call and this host has none it can drive. Its configured provider is `amazon-bedrock` reading a host AWS profile, and that turn ends `stream disconnected before completion: failed to load AWS credentials: the credentials provider was not properly configured` — the recording shell cannot read `~/.aws`, and the Codex wrapper cannot mint a config either (`failed to create temporary file for AWS config: Permission denied (os error 13)`). Repointed at the built-in `ollama` provider it authenticates and runs a full turn against a local 3B model, and that model answers a shell request in prose: it writes a `Preamble:` and a fabricated `Command Output:` block containing the expected text instead of calling a tool. So the three tool shapes follow the parsers in `src/kiro_crew/acp/_dispatch.py`. |
| `opencode/` | `opencode` | **live** | Five captures off `opencode acp` 1.18.30 driving a local Ollama model, all seven required classes reached live, plus a `session/load` result (`session-load-live.jsonl`: replayed conversation, `configOptions`, no `modes`). `session-live.jsonl`: the initialize response, the `session/new` response, an `agent_message_chunk` turn, a `usage_update` and the `stopReason` response, verbatim and in order. `tool-call-live.jsonl`: a `tool_call` and two `tool_call_update` frames from a call the harness rejected against its own argument schema. `permission-request-live.jsonl`: `tool_call`, the `session/request_permission` frame OpenCode sent with `permission: ask` in force, and the `tool_call_update` frames through `completed` with the command's real output. `mcp-directive-call-live.jsonl`: an MCP tool call -- a `kirocrew-core` stdio element on the `session/new` array exposing `monitor_start` -- carrying opencode's own tool naming (`title` = `kirocrew-core_monitor_start`, ONE underscore, no `_meta.kiro`), `rawInput` empty on the `tool_call` and complete on the refinement, and the tool's result text with its directive marker intact. Slices of longer turns, with the home directory redacted to `~`. |

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
