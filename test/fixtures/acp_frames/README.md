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

There is no recorder in this repository yet. Every fixture here was written by
hand from the shapes the parsers accept (see the provenance table below). The
opt-in recorder — `KIROCREW_ACP_RECORD_FRAMES=<dir>`, appending every inbound
frame from both transports to `<dir>/<backend>.jsonl` with owner-only file
permissions — ships in the follow-up PR titled *opt-in ACP frame recorder
(owner-only perms)*. Until it lands, a live capture means running a backend
under a wire-level trace of your own and transcribing its stdout lines.

Then split the capture into scenario files, add the `_meta` header, review it
(below), and generate the snapshot:

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

The follow-up recorder will run each frame through `redact_text` — the same
credential and exfiltration-URL scrub the dashboard path runs — and replace the
recording user's home directory with `~`. That is a floor, not a guarantee. It
does not know an account id, an internal hostname, a customer name or a private
file path when it sees one, and a hand-transcribed capture has had no scrub at
all.

So a recording is reviewed by hand before it is committed. Read every line and
remove:

- tokens, keys and session credentials, including anything the scrub tagged but
  left recognizable;
- absolute paths, usernames and machine names;
- account ids, ARNs and internal endpoints;
- prompt and tool-output text that was not written for this corpus.

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

This is the second requirement in the host contract enforced by behaviour rather
than by prose; see `docs/system-specs/modules/agent-host-contract.md`.

## Provenance of what is committed today

Every fixture here is `synthesized` except the five under `opencode/`, which
are captures. Stated plainly because it bounds what the corpus proves: a
synthesized fixture locks the dispatch layer's behaviour against refactoring,
which is what it was built for, and it does **not** prove that the backend really
emits those shapes. Only the live files carry that second proof, and for opencode
they reach all seven required classes.

| Directory | Backend id | Provenance | Why |
|---|---|---|---|
| `kiro/` | `""` | synthesized | kiro-cli 2.21.1 is installed on the recording host but reaching it goes through an interactive sandbox launcher, so no capture was taken. Shapes follow the parsers in `src/kiro_crew/acp/_dispatch.py`. |
| `kas/` | `kas` | synthesized | Reached through the kiro-cli relay, so same as above. The `_meta.kiro` discriminants follow `src/kiro_crew/acp/kas_wire.py`. |
| `claude/` | `claude` | synthesized | `claude-agent-acp` was not installed on the recording host. |
| `codex/` | `codex` | synthesized | `codex-acp` was not installed on the recording host. |
| `opencode/` | `opencode` | **live** | Five captures off `opencode acp` 1.18.30 driving a local Ollama model, all seven required classes reached live, plus a `session/load` result (`session-load-live.jsonl`: replayed conversation, `configOptions`, no `modes`). `session-live.jsonl`: the initialize response, the `session/new` response, an `agent_message_chunk` turn, a `usage_update` and the `stopReason` response, verbatim and in order. `tool-call-live.jsonl`: a `tool_call` and two `tool_call_update` frames from a call the harness rejected against its own argument schema. `permission-request-live.jsonl`: `tool_call`, the `session/request_permission` frame OpenCode sent with `permission: ask` in force, and the `tool_call_update` frames through `completed` with the command's real output. `mcp-directive-call-live.jsonl`: an MCP tool call -- a `kirocrew-core` stdio element on the `session/new` array exposing `monitor_start` -- carrying opencode's own tool naming (`title` = `kirocrew-core_monitor_start`, ONE underscore, no `_meta.kiro`), `rawInput` empty on the `tool_call` and complete on the refinement, and the tool's result text with its directive marker intact. Slices of longer turns, with the home directory redacted to `~`. |

Replacing any row with a live capture is a strict improvement and needs no
change to the test. Record it, set `recorded` to `live`, fill in the real
`agent_version`, regenerate the snapshot, and update this table.
