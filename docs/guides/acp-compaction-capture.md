# Capturing a harness's `/compact` evidence

`ACP_BACKENDS_COMPACT` in `src/kiro_crew/agent_sdk/backends.py` decides whether Kiro
Crew offers a manual `/compact` on a backend at all. A harness joins that set on a
DRIVEN capture, never on its source: the bar opencode met is a live session whose
`usage_update.used` was seen to fall below its pre-compact peak.

pi and goose are outside the set today, and not because they lack the feature. Both
finish a compaction inline according to their own code — pi-acp intercepts the command
in `prompt()`, goose routes it through its `handle_compact_command` — and neither has
ever been driven: pi answered `Authentication required` and goose
`Failed to resolve provider: GOOSE_PROVIDER` on the host where the note was written.
Source says what the code WOULD do. A capture says what the harness DID, and for a
membership whose wrong answer makes `wait_for_compaction` report a completion that
never happened, only the second counts.

This guide is how anyone holding a credential closes that gap in one command.

```
python3 scripts/capture_acp_compaction.py --harness goose
```

`scripts/capture_acp_compaction.py` spawns the harness the way Crew spawns it, drives
four ordinary turns, sends `/compact`, drives one more ordinary turn, writes the frames
as a corpus fixture and prints a verdict.

## What you have to bring

Kiro Crew authenticates none of these harnesses. Each resolves its own provider from
its own store, which is why `src/kiro_crew/agent_sdk/backend_install.py` probes no
credential and why this script cannot either. So the one prerequisite the script cannot
give you is a harness that can reach a model.

**A locally served model is the cheapest way to satisfy that, and it needs no
credential at all.** Both existing corpora were captured that way: the goose fixtures
were driven against a model served locally with no provider key involved, and the pi
fixtures against an Ollama model named in pi's own `models.json`. A local model also
keeps a provider catalog out of the frames, which is one of the marker classes a
fixture is refused for.

| Harness | Install | What it needs to reach a model | Overrides |
|---|---|---|---|
| goose | `curl -fsSL https://raw.githubusercontent.com/block/goose/main/download_cli.sh \| bash` | A provider. `goose configure` in a terminal names one and stores its key; the key lands in the OS keyring, or in `~/.config/goose/secrets.yaml` when `GOOSE_DISABLE_KEYRING` selects file storage. A locally served model is named as the provider instead and needs no key. `GOOSE_PROVIDER` / `GOOSE_MODEL` in the environment select one without touching the config. | `GOOSE_BIN` points at the binary. `XDG_CONFIG_HOME` relocates the whole config tree, secrets included — useful for a scratch capture. |
| pi | `npm i -g pi-acp @earendil-works/pi-coding-agent` (both halves: the adapter Crew spawns and the agent it spawns) | A sign-in or a local model. `pi` in a terminal, then its `/login`, writes `~/.pi/agent/auth.json`; or name a locally served model in `~/.pi/agent/models.json` and skip sign-in entirely. | `PI_ACP_BIN` points at the adapter's entry script, `PI_ACP_PI_COMMAND` at the agent. `PI_CODING_AGENT_DIR` relocates pi's whole agent directory. |
| opencode | `npm i -g opencode-ai` | `opencode auth login`, or a local model in the project's `opencode.json`. | `OPENCODE_BIN` points at the binary. |

opencode is in that table on purpose: it is already a member, so driving it is the
control run. If the script reports `COMPACTED` for opencode on your host, the script
works, and a `NOT_COMPACTED` or `UNPROVEN` for goose or pi is then about the harness.

Nothing else is required. The script needs no gateway, no Crew session and no config: it
speaks ACP to the harness over stdio directly.

## Running it

```
# the default drive: 4 ordinary turns, /compact, 1 ordinary turn
python3 scripts/capture_acp_compaction.py --harness goose

# a longer growth series, and a longer patience for a slow local model
python3 scripts/capture_acp_compaction.py --harness pi --turns 6 --timeout 300

# the control run against a harness already in the set
python3 scripts/capture_acp_compaction.py --harness opencode
```

The capture lands in `build/acp-capture/<harness>/compact-live.jsonl` unless `--out`
says otherwise. That is deliberately NOT the corpus: `test/test_acp_frame_replay.py`
collects every `*.jsonl` under `test/fixtures/acp_frames/` and requires a snapshot
beside each one, so a candidate dropped straight in goes red on sight. Moving a reviewed
capture in is a separate step, below.

**The capture client says no to everything.** It spawns the harness raw — no sandbox, no
permission gate, your own environment — because a capture has to be evidence about the
harness rather than about Crew's wrapping. So every request the harness sends it is
refused, tool permissions included: a client that approved them would run whatever the
model asked for on your machine, and a compaction needs no tool call. A refused call is
still in the frames. A harness that runs its OWN builtin tools without asking never
reaches that refusal, which is the other reason the drive belongs in a scratch directory.

The session's working directory defaults to a fresh scratch directory. Pass `--cwd` only
if the harness needs a project to look at — and then expect the path to show up in the
frames, where it is host data the sweep will name. A filesystem root is refused: its text
is the separator every other path contains, so substituting it out would rewrite
`session/update` into `session<cwd>update` and corrupt the evidence.

Output reads like this (the numbers are opencode's own recorded drive):

```
harness        OpenCode (opencode)
drive          4 ordinary turn(s), /compact, 1 ordinary turn
  turn 1  ordinary used 14863                        stopReason end_turn
  turn 2  ordinary used 15727                        stopReason end_turn
  turn 3  ordinary used 16614                        stopReason end_turn
  turn 4  ordinary used 17478                        stopReason end_turn
  turn 5  compact  used 514                          stopReason end_turn
  turn 6  ordinary used 14577                        stopReason end_turn
peak before    17478
during compact 514
after compact  14577
verdict        COMPACTED -- used fell to 14577 after /compact, below the pre-compact peak of 17478
frames         9 written to build/acp-capture/opencode/compact-live.jsonl
```

Two readings matter and they are not the same. The number DURING the `/compact` turn is
the harness mid-summary, so it is reported and never used as the comparison. The number
on the ORDINARY turn after it is the evidence, because it shows the smaller context was
carried forward rather than announced once.

| Exit | Verdict | What it means |
|---|---|---|
| 0 | `COMPACTED` | `used` fell below the pre-compact peak. This capture is the evidence for a membership. |
| 1 | `NOT_COMPACTED` | The drive worked and `used` did not fall. Also evidence, and the more interesting kind: it contradicts the harness's own source. |
| 2 | — | Arguments, or the harness is not installed. The message carries the install command and the override variable. |
| 3 | — | The harness refused to talk to a model. The message carries the repository's own sign-in remedy for that harness. |
| 4 | `UNPROVEN` | The turns ran and no `usage_update` carried `used`. This harness does not report the number the bar is written in; say so rather than reading a pass out of silence. |
| 5 | — | The capture was written and is not commit-ready: a recording-host marker survived the sweep, or `--keep-ids` kept the harness's own ids. The frames are on disk either way. |

## Reviewing the capture

`test/fixtures/acp_frames/README.md` is the contract, and it asks for a hand review that
no sweep replaces. The script does the part a machine can, on every run:

- drops `available_commands_update` and `session_info_update` frames — the recording
  host's own command inventory and run id;
- drops `agent_thought_chunk` frames — model prose carrying no class any parser reads;
- replaces the scratch working directory with `<cwd>` and your home directory with `~`;
- replaces the harness's session id with a synthetic `<harness>-session-1` and each
  permission id with `perm-N` (pass `--keep-ids` to see the originals, which then makes
  the file un-committable);
- runs every string through `redact_text`, the same credential and exfiltration-URL
  scrub the in-product frame recorder applies — a frame is agent-written text, so a
  secret the model echoed into its own reply is a secret in the capture, and the marker
  patterns read host identity rather than secrets;
- sweeps the result with `scripts/check_acp_frame_host_data.py`'s own patterns and exits
  5 naming anything that survived.

Then read every line yourself. The class that gets through a sweep is an ENUMERATION —
a provider catalog, a model list, an installed-agent list — because it reads as ordinary
product data while describing what your machine HAS. A local model is the best defence:
there is no catalog to leak.

Every reduction the script made is recorded in the file's own `_meta.note`, together
with the `used` series and the verdict, because a reader cannot recompute the series from
a file whose growth turns were trimmed.

## Landing the evidence

A `COMPACTED` capture for goose or pi changes one membership and the several places that
today record its absence. Work through them in order; the tests are what stop a half-done
move.

**1. The fixture.** Move the reviewed capture in and generate its snapshot:

```
cp build/acp-capture/goose/compact-live.jsonl test/fixtures/acp_frames/goose/
python3 scripts/update_acp_frame_snapshots.py
python3 -m pytest test/test_acp_frame_replay.py test/test_acp_frame_host_data.py
```

Commit the `.jsonl` and the generated `.expected.json` together.

**2. The corpus README.** `test/fixtures/acp_frames/goose/README.md` (or `pi/`) carries a
row per file and a short section per thing a capture establishes. Add both: the row names
the frame classes, the section names what the `used` series shows.

**3. The capability sets.** In `src/kiro_crew/agent_sdk/backends.py`:

- add the id to `ACP_BACKENDS_COMPACT`;
- add it to `ACP_BACKENDS_INLINE_COMPACTION` as well **if** the compaction finished
  inside the `session/prompt` turn — which is what a terminal `stopReason` with no
  compaction status frame means, and what both harnesses' source predicts. Skip this and
  `wait_for_compaction` waits out its whole timeout on a compaction that already
  happened;
- rewrite the "pi and goose are NOT members" paragraph on `ACP_BACKENDS_COMPACT`. It is
  the record of WHY the set asks for a capture, so replace the waiting-for-a-drive part
  with what the drive found — the `used` series, the harness version, the fixture path —
  and leave whichever harness is still unmeasured saying what it still lacks;
- the same comment's closing paragraph explains that both take the
  `COMPACT_ARM_UNCLASSIFIED` refusal. Narrow it to whichever harness still takes it.

**4. The backend card's unmeasured cell.** The Developer > Agent Backend card renders
three marks per line — available, not available, and NOT MEASURED — and the third one is
the only per-harness table in `src/kiro_crew/agent_sdk/backend_cards.py`:
`DECLARED_UNMEASURED`. It holds exactly two entries, both the `manual_compact` cell, one
for pi and one for goose, each citing `ACP_BACKENDS_COMPACT`'s own words. A capture is
what that cell was waiting for, so delete the entry for the harness you drove.

Three tests in `test/test_backend_cards.py` hold this together, and each fails for a
different half-done move:

- an entry naming a MEMBER is stale, so leaving it in place after step 3 fails outright —
  the table may soften a negative, never overrule a membership;
- every entry must be supported by the deciding set's comment, which names the harness
  and says the gap is evidence. If step 3 rewrote that comment, the REMAINING entry's
  support has to survive the rewrite;
- one test asserts the unmeasured cells are exactly those two. Removing one means editing
  that assertion, and removing both means the card has no unmeasured cell at all.

`website/scripts/capture-agent-backend-unmeasured.mjs` captures the screenshots that show
the three marks side by side, and its header names pi and goose as the two unmeasured
cells. Update that prose; if both cells go, the capture has no subject left and the frames
it produces are the all-measured case.

The frontend needs nothing. `unmeasured_reason` is a machine code labelled in
`website/src/pages/developer/AgentBackendTab.tsx` and its test drives a stub payload, so
neither knows which harness carries the cell.

**5. The tests that pin today's answer.** Each one fails until it is moved, which is the
design:

- `test/test_manual_compact_gate.py` — an exact-set equality on `ACP_BACKENDS_COMPACT`
  plus explicit `not in` assertions for pi and goose;
- `test/test_acp_capability_sets_leaf.py` — a second exact-set equality on the same set;
- `test/test_compaction_other_backends.py` — `test_pi_and_goose_wait_for_a_capture` (the
  test that says what the bar is), the `UNCLASSIFIED` frozenset and its reason comment,
  `test_pi_and_goose_are_the_unclassified_case_today`, and the refusal-arm assertion that
  names pi as the unclassified example. If BOTH harnesses become members, `UNCLASSIFIED`
  empties out — the arm itself stays covered by the unknown-id case beside it;
- `test/test_acp_goose_backend.py` — a block asserting goose's absence from the
  compaction sets, with the reason in its docstring (goose only);
- `test/test_agent_sdk_capabilities.py` — no edit expected: it asserts
  `ACP_BACKENDS_INLINE_COMPACTION` is a subset of `ACP_BACKENDS_COMPACT` and of
  `ACP_BACKENDS_KNOWN`. Run it; a subset break there means step 3 added the inline
  membership without the manual one.

**6. The specs.** Two record the current exclusion in prose:

- `docs/system-specs/modules/agent-host-contract.md` — the Compaction row of the
  per-harness table. The pi cell calls its exclusion "conservative rather than
  evidenced"; a capture is exactly the evidence that sentence was waiting for.
- `docs/system-specs/modules/session.md` — the compaction-set section, which lists the
  members and states the capture bar.

**7. The full local gate**, because the sets are read across the boundary:

```
python3 -m pytest test/test_manual_compact_gate.py test/test_compaction_other_backends.py \
    test/test_acp_capability_sets_leaf.py test/test_agent_sdk_capabilities.py \
    test/test_harness_parity.py test/test_acp_frame_replay.py test/test_backend_cards.py
python3 scripts/docs_lint.py
```

A `NOT_COMPACTED` or `UNPROVEN` capture changes nothing in the sets — the harness stays
out — but it is still worth landing: it turns "nobody has driven this" into "driven, and
here is what it did", which is a different sentence for the next reader. Put the fixture
and the finding in the comment; leave the memberships alone.

## When the harness refuses

Exit 3 means the harness answered an auth or provider error rather than a turn, and the
script prints that harness's own remedy from `src/kiro_crew/agent_sdk/host_auth.py` —
the same text the dashboard shows. Nothing in Kiro Crew can sign a harness in for you.

The two errors this guide exists because of:

- `Failed to resolve provider: GOOSE_PROVIDER` — goose has no provider configured. Run
  `goose configure`, or set `GOOSE_PROVIDER` and `GOOSE_MODEL` for a locally served
  model.
- `Authentication required` — pi is not signed in. Run `pi` and complete `/login`, or
  name a local model in `~/.pi/agent/models.json`.

A harness that starts, handshakes and then hangs is usually a model that is slow rather
than a harness that is stuck: raise `--timeout`. The failure message carries the
harness's last stderr lines, which is where a local model server that is not running
says so.
