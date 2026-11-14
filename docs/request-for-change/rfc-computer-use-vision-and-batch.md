---
title: Computer Use -- screenshots the model can see, and several actions in one call
status: draft
author: inariku, with Kiro Crew
created: 2026-10-26
last-audited: 2026-10-26
audited-at: 260141d49
doc-pr: 24210
implementation-prs: [24316]
tracking-issues: [24212]
supersedes: []
superseded-by: []
---

# RFC: Computer Use -- screenshots the model can see, and several actions in one call

- Status: `draft`. Nothing here is built. Measured against main `260141d49`.
- Related: [`rfc-jev-task-executor`](rfc-jev-task-executor.md) (accepted), whose
  rollout step 4 splits the same dispatch chokepoint this design needs;
  [`rfc-remote-computer-use`](rfc-remote-computer-use.md) (draft), whose wire carries
  the same screenshot bytes; window targeting in
  [#24203](https://github.com/kirodotdev/KiroCrew/pull/24203) for
  [#18543](https://github.com/kirodotdev/KiroCrew/issues/18543), which this document
  assumes but does not depend on.

## 1. Summary

Computer Use gives the chat model a text tree and, at most, the path of a JPEG on
disk. The model never sees the window unless it spends an extra tool call reading
the file, and on a large browser window it gets no screenshot at all. Each action is
one tool call followed by a full re-walk, so a ten-field form is ten model turns.

This document proposes two changes and leaves the security-relevant choices to the
maintainers:

1. **Inline screenshots.** `computer_get_state` returns its screenshot as an MCP
   image content block, on harnesses measured to pass such a block to the model. A
   probe recorded in section 2.3 shows kiro-cli 2.34.0's v3 engine (KAS) does on a
   real Computer Use screenshot, and its v2 engine (Kiro) does not. Every other harness keeps
   today's path-only reply.
2. **`computer_batch`.** One call runs a short list of actions against one window.
   Each step is checked by name against the governance decision before any step
   runs, then goes through the existing dispatch chokepoint; the batch stops at the
   first refusal or UI drift and returns one refreshed tree (and screenshot) at the
   end.

The whole-window screenshot floor for a window holding a password field is unchanged.
Whether a truncated tree may be photographed is an open question (section 10), not a
decision this document makes.

## 2. Motivation

### 2.1 Current state

- **Text only, by construction.** `validation.build_tool_response` frames every
  result as one `{"type": "text"}` block, and the stdio loop in
  `mcp_shared_dispatch._run_stdio_dispatch_loop` (its `_tool_response` helper) is the
  only exit. Computer Use adds two more text-only links: the shim's
  `mcp_computer._call_tool_inner` returns the `text` field of the gateway's reply, and
  `dashboard/handlers/computer_use.py` `api_computer_use_invoke` answers
  `{"text": ...}`. `docs/system-specs/modules/computer-use.md` ("Result shape: text
  only, by construction") states that an image block "is not expressible on this
  transport". Section 2.3 shows that is a property of these three links, not of the
  harness.
- **The screenshot is a path.** `service._persist_image` spools the JPEG and
  `render._render_image_note` tells the model to read it "with the fs_read tool only
  if the tree is insufficient". The agent-facing prompt in
  `context_assembly/sections.py` and the `computer-use` skill say the same.
- **A truncated tree is never photographed.** `capture_macos.capture_snapshot_image`
  and `capture_windows.capture_snapshot_image` return no image when
  `snap.truncated or snap.depth_truncated`. On macOS, `snapshot_macos._walk` sets
  `truncated` as soon as the REPORT reaches `max_tree_nodes` (default 1200), even
  though it keeps classifying every remaining node for `saw_secure` up to
  `MAX_TREE_NODES_LIMIT` (5000). It also sets `truncated` when the secure scan really
  stops early: at that 5000-node cutoff, at the `MAX_WALK_SECS` deadline, or when a
  node has more than `MAX_CHILDREN_PER_NODE` (512) children. A `Snapshot` cannot tell
  the first case from the other three, so all of them suppress the image. The
  `computer-use` skill calls this suppression "routine for a browser or an Electron
  app at the default budget".
- **One action per call, one walk per action.** Every mutating tool in
  `computer_use/tools.py` `_run` verifies the target's fingerprint against a fresh
  walk (`service.verify_fingerprint`), acts, settles for `POST_ACTION_SETTLE_SECS`,
  re-walks and renders the whole tree, with `want_image=False`.
- **Nothing structured leaves the chokepoint.** `tools._dispatch` returns a string.
  `rfc-jev-task-executor` section 3.5 proposes splitting it into one executor and two
  output adapters; `git grep dispatch_structured -- src` is empty at `260141d49`, so
  that step has not landed.

### 2.2 What this costs

The model drives a GUI from a text outline it cannot check against the pixels. When
the outline is wrong or thin (Electron stubs, unlabeled buttons, canvases), it has to
decide on its own to call `fs_read` on a path, at the cost of one more tool call. A
form with ten fields is ten round trips through the model, each re-reading a tree of
up to 1200 nodes. Section 2.4 compares Kiro Crew with Codex on these points.

### 2.3 Probe: kiro-cli passes a tool-result image to the model

Run on 2026-10-26 with kiro-cli 2.34.0 on macOS:

1. A stdlib stdio MCP server with one tool whose result is ONLY
   `{"type": "image", "data": <base64 PNG>, "mimeType": "image/png"}`. The PNG shows
   six random capital letters that appear nowhere in text.
2. A workspace-local agent config loading only that server.
3. `kiro-cli chat --no-interactive --agent <probe> --trust-tools=<the tool>` with the
   prompt: call the tool once and reply with only the word drawn on the image, or
   NONE.

Two runs with two fresh words (`LWZIVA`, `BKGISO`): the model answered each word
exactly, and a third run on the v3 engine (`--agent-engine v3`, the engine KAS
uses) read `JNWMMK`.

The prototype in [#24316](https://github.com/kirodotdev/KiroCrew/pull/24316) then
repeated the probe with a real screenshot. Preview showed a white shape (random
circle, triangle or square) on a random background colour; the branch's dispatcher
captured it, the gateway's `_inline_image` fitted it, and `build_tool_response` built
the text-plus-image frame, with the answer words checked to be absent from the text.
On the v3 engine the model named colour and shape correctly each time and answered
`NONE` when no image was attached. On the v2 engine (Kiro) it never named both in
nine runs, across JPEG and PNG, 480 to 1280 px, with and without the text block. A
small text-free PNG is therefore not enough evidence for a harness: the probe that
admits one uses a real screenshot. This real-screenshot result was produced on the
prototype branch only; main has no image path to run it against.

Kiro Crew's own ACP parser
(`acp/_dispatch_parts/tool_result_events.py` `tool_call_content_text`) renders only
text blocks and stays silent on `image`, so the dashboard transcript shows no image
bytes. No harness outside kiro-cli has been probed.

### 2.4 Comparison with Codex

OpenAI's Codex ships a Computer Use feature for the same job: an agent driving the
user's desktop apps. The Codex column comes from public sources: OpenAI's
[computer use API guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
and [Codex use-case page](https://developers.openai.com/codex/use-cases/use-your-computer-with-codex),
issues on [openai/codex](https://github.com/openai/codex), and two third-party
analyses of the Codex plugin
([codex-computer-use-cli](https://github.com/paralym/codex-computer-use-cli),
[opencode-codex-computer-use](https://github.com/malhashemi/opencode-codex-computer-use)).
Rows marked (third-party) rest on those analyses alone. The Kiro Crew column is
measured at `260141d49`.

| | Codex | Kiro Crew today | This document |
|---|---|---|---|
| What an observation gives the model | The accessibility tree and a screenshot, the image returned to the model (third-party) | The tree, and a screenshot only as a spooled file path | Inline image on measured harnesses (5.2) |
| A large browser window | Photographed (third-party) | Not photographed once the report exceeds `max_tree_nodes` (2.1) | Open question 3 |
| Actions per model call | Several: the API guide recommends a code-execution mode that runs many actions, loops and branches in one call, and Codex exposes Computer Use as a JavaScript runtime, `cua_repl` ([openai/codex#42454](https://github.com/openai/codex/issues/42454)) | One, then a full re-walk | Several, as a fixed list of tool steps, not code (5.3, section 9) |
| Choosing among an app's windows | Targets by app name; per-agent window ownership is requested in [openai/codex#20852](https://github.com/openai/codex/issues/20852) | One window per app on main; `window_id` in [#24203](https://github.com/kirodotdev/KiroCrew/pull/24203) | Assumed, not required |
| Web pages and tabs | Handled inside the same runtime through a Chrome extension, with tab handles and locators (third-party) | Separate tools (the Browser panel, `playwright-cli`), not Computer Use | Out of scope (section 4); requested in [#23095](https://github.com/kirodotdev/KiroCrew/issues/23095) and [#10974](https://github.com/kirodotdev/KiroCrew/issues/10974) |
| Background input on macOS | SkyLight per-process event posting ([Cua write-up](https://github.com/trycua/cua/blob/main/blog/inside-macos-window-internals.md)) | The same SkyLight calls (`macos_skylight.py`) | No change |
| Permission | Per app, with "Always allow" (use-case page) | One keystone enable for the whole feature | No change |

The foundations match: both read the accessibility tree and post input to a
background process the same way. The gaps this document addresses are the first
three rows, which decide how often the model can check its work against the pixels
and how many turns a multi-step task costs.

## 3. Goals

- G1. On a harness measured to pass tool-result images, the model sees the
  screenshot of every `computer_get_state` it asks one for, in the same reply as the
  tree, without an extra tool call.
- G2. A harness not measured keeps exactly today's reply: same text, same path.
- G3. A short run of actions on one window costs one model turn, not one per action,
  and still passes every per-action check the single tools pass.
- G4. No floor moves without a maintainer decision: a window holding a secure field
  is never photographed, and the truncated-tree rule changes only through open
  question 3.
- G5. One image can never wedge a session's history: every inline image is held to
  the budget `kiro_crew/imaging.py` defines (`MAX_IMAGE_EDGE_PX`,
  `MAX_IMAGE_B64_BYTES`) before it leaves the gateway.

## 4. Non-goals

- Browser tab control (Codex's `getBrowser()` / tabs). A gateway-owned browser
  driver is already gated on its own design in `rfc-jev-task-executor` section 3.6.
  The requests are tracked in
  [#23095](https://github.com/kirodotdev/KiroCrew/issues/23095) (Browser panel
  actions, tabs and viewports) and
  [#10974](https://github.com/kirodotdev/KiroCrew/issues/10974) (an agent browser in
  the user's signed-in session).
- Running model-written code (Codex's `cua_repl`). See section 9.
- Changing the Jev executor, its consent model or its "no pixels to Jev" rule. That
  rule is about the Jev loop; this document is about the chat model.
- Multi-display selection.
- Returning a screenshot from every mutating tool. Only `computer_get_state` and the
  end of a `computer_batch` attach one.

## 5. Design

### 5.1 One structured result from the chokepoint

`tools._dispatch` and `_run` return a private `DispatchResult` instead of a string:
the rendered text exactly as today, plus `image: bytes | None` and `image_mime`. The
existing `dispatch_tool` returns `result.text`, so every current caller and test sees
byte-identical output. This is the split `rfc-jev-task-executor` section 3.5
describes, minus its `dispatch_structured` adapter; open question 5 asks whether it
lands here or with Jev step 4.

The image bytes come from `Snapshot.image_jpeg`, which `_render_snapshot` already
holds, after the same observation shaping (`gate.apply_observation_ceiling`) that
decides whether the path is shown. An image is carried only when the path would be.

### 5.2 Inline delivery, by harness capability

- A positive capability set, `ACP_BACKENDS_TOOL_RESULT_IMAGES`, next to the other
  `ACP_BACKENDS_*` sets in `agent_sdk/backends_turns.py`. It starts as
  `{ACP_BACKEND_KAS}`, the one harness section 2.3 measured on a real screenshot. A
  harness joins by
  passing the same probe, recorded in this document. Membership is never inferred
  from the absence of another harness.
- The agent-spec writer adds an `--inline-images` argument to the `kirocrew-computer`
  invocation (`agent._MANAGED_MCP_SERVERS`) only for a member harness. The shim sends
  `accept_images: true` on the invoke request when it was started with the flag.
- `api_computer_use_invoke` adds an `image` member (`{"mimeType", "data"}`) to its
  `{"text": ...}` reply only when the request asked for one, the keystone's
  `inline_screenshots` setting is on (section 8), and the result carries bytes. Before sending, it runs `imaging.downscale_image_block`; a block whose
  compliance cannot be established is dropped and the text keeps today's path note.
  The budget applies whether or not the server is brokered by the MCP gateway, whose
  relay (`mcp_gateway/image_budget.py`) applies the same budget only to brokered
  servers.
- The shim returns a text block and, when present, an image block. The stdio frame
  gains an optional second content block; `build_tool_response` keeps its signature
  for every other server.
- The text half still names the spooled path, so the live view, the user and a
  harness that later drops the block are unaffected, and says the image is attached.
  `render._render_image_note`, the prompt line in `context_assembly/sections.py` and
  the skill are updated together.
- Mutating tools stay `want_image=False`. `computer_batch` attaches one image at its
  end (section 5.3).

### 5.3 `computer_batch`

```
computer_batch(app, window_id?, steps, screenshot?)
steps: [{"tool": "computer_click" | "computer_type_text" | "computer_press_key" |
                 "computer_set_value" | "computer_scroll" | "computer_perform_action" |
                 "computer_drag",
         "args": {...}}]   # 1..MAX_BATCH_STEPS, args as the single tool takes them
```

- **One window.** `app` and `window_id` belong to the batch; a step's `args` may not
  name another. A batch cannot hop between applications.
- **Every step is governed under its own name, before any step runs.** The
  governance gate (`hook_runtime/governance_gate._governance_denial`, through
  `governance_parts/evaluator.gate_decision` and `tool_calls.classify_tool_title`)
  sees only the title of the call it approves, so for a batch it sees
  `@kirocrew-computer/computer_batch`, never the step tools. Inside the dispatcher,
  `gate.require_computer_use` writes an audit record and makes no decision. The batch
  therefore evaluates each step's `@kirocrew-computer/<step tool>` ref against the
  same `POLICY ∩ PROFILE` decision the gate would make for that tool called alone,
  and refuses the whole batch, before running anything, if any step is denied. An
  administrator who denies `computer_type_text` by name gets that tool refused inside
  a batch exactly as outside it.
- **Every step passes the chokepoint.** Each step is then dispatched under its own
  tool name through the same ordered `_dispatch` (enable, identity resolution, audit,
  target policy, secure-target refusal, fingerprint check) and audited as that tool.
- **Indices bind to the snapshot the model saw.** Each element step resolves its
  index against the cached snapshot from the `computer_get_state` that preceded the
  batch, and `verify_fingerprint` compares it with a fresh walk before acting. The
  per-step refresh walk is skipped and the cache is not replaced mid-batch, so a step
  that changes the window (opens a dialog, reorders a list) makes the next element
  step fail its fingerprint check instead of acting on an element the model never
  saw. The check compares role, subrole and title at the index, so it carries the
  same limit the single tools do ("Index lifecycle (and its honest limit)" in
  `computer-use.md`): a changed window whose element at that index happens to match
  is not caught.
- **Stops at the first problem.** A refusal, a driver error or drift ends the batch.
  The reply lists every step as `done`, `refused: <reason>` or `not run`, then one
  refreshed tree, cached for the next call, plus the screenshot when 5.2 applies.
  Steps that ran are not undone; the reply says so.
- **Excluded:** `computer_launch_app`, observation tools, `computer_end_turn`, and
  `click_method: "global"` (the one path that moves the user's real pointer), pending
  open question 6.
- **Classification.** `batch` gets a row in the existing code-owned
  `_CU_ACTION_CLASSES` table (`platform/governance_parts/tool_calls.py`) carrying
  `mutate`, so `hooks._cu_read_only_auto_approve` never auto-approves it as an
  observation and `gate`'s is-it-mutating check reads it correctly. The row adds no
  `computer_use.*` scope and no approval ordinal. It is not a governance control:
  no evaluator reads a class pattern such as `@mutate` as a deny today, so the
  by-name step check above is what enforces a ceiling.

## 6. Migration plan

Each phase is one PR.

1. **Structured result** (5.1). No behaviour change.
   Exit: every existing computer-use and MCP-computer test passes unchanged; a new
   test asserts `dispatch_tool` returns `DispatchResult.text` byte-for-byte for every
   tool on the fake backend. Blocked on open question 5.
2. **Inline screenshots** (5.2) for `computer_get_state`.
   Exit: a member harness's shim returns a text block and an image block; a
   non-member's reply is byte-identical to main; an oversized fake image is
   downscaled and an undecodable one is dropped with the path note kept; a window
   holding a secure field and a truncated walk return no image block; the
   dashboard transcript stores no base64; the section 2.3 probe, re-run against a
   real window, reads a word shown in it; with the keystone's `inline_screenshots`
   off, a call passing `screenshot: true` returns no image block; a missing, corrupt
   or non-boolean `inline_screenshots` reads as off; toggling the primary enable in
   Settings keeps `inline_screenshots`. Blocked on open question 2.
3. **`computer_batch`** (5.3).
   Exit: a batch of N steps produces N per-step SEL records under the step tool
   names; a step that opens a dialog makes the next element step fail with drift and
   the reply marks the rest `not run`; a step naming another `app` or `window_id`, a
   launch step and a `global` click are refused before any step runs; with an `mcp`
   deny on `@kirocrew-computer/computer_type_text`, a batch containing a
   `computer_type_text` step is refused before its first step runs, and the same
   batch without that step runs.
   Blocked on open questions 1 and 6.
4. **Truncated-tree capture**, only if open question 3 chooses to change it.

## 7. Backward compatibility

Compatible. Non-member harnesses get byte-identical replies, `build_tool_response`
keeps its signature, and `computer_batch` is a new tool. The spec's "text only, by
construction" section is rewritten in phase 2 to describe the capability rule.

## 8. Security considerations

- **More pixels reach the model provider.** On a member harness every
  `computer_get_state` with a screenshot sends the image, where today the model sends
  it only when it chooses to read the file. The pixels are the ones the model can read
  today. No setting stops a screenshot the model asks for:
  `computer_use.attach_screenshot` is the default used when a call omits `screenshot`
  (`service.snapshot_request` takes the caller's value first), and `config.md`
  documents it as that default. Phase 2 therefore adds
  the keystone's `inline_screenshots` (a key in `computer_use.json`, beside the primary enable), an operator setting the model cannot override because the agent cannot write the keystone:
  while it is off, no image block leaves the gateway whatever the call asks for. Its
  default is open question 2.
- **Pixels are not redacted.** The text tree passes `policy.redact_result`; an image
  cannot. That is already true of the spooled file. The secure-field floor (no capture
  when any node is secure) is what keeps a password box out of the frame, and it is
  unchanged.
- **Where the image is stored.** kiro-cli keeps it in its session history and replays
  it on later turns (as `mcp_gateway/image_budget.py` describes). Kiro Crew's
  transcript does not: `tool_call_content_text` renders text blocks
  only (phase 2 pins this with a test). The spooled JPEG keeps today's `0o600` file
  and ring trim.
- **No history wedge.** The gateway route downscales or drops every image before it
  leaves (G5), independent of the MCP gateway relay.
- **Batch.** Every step passes the full chokepoint and is audited separately. What
  changes is that one tool call, and therefore one interactive approval in a
  non-autopilot session, covers several actions. The spec's `call` section says a
  tool that runs other tools "would let a model launder one per-call gate decision
  into many"; open question 1 asks the maintainers to rule on that for this tool.
- **One keystone key, no other sensitive-path change.** Phase 2 adds one boolean,
  `inline_screenshots`, to the keystone `computer_use.json`, beside `enabled`. Its
  reader in `computer_use/enable_state.py` fails closed like `enabled`: only a real
  JSON `true` turns it on, and a missing, corrupt or non-boolean value reads as off.
  The agent cannot write the keystone in any sandbox mode, and the Settings handler
  (`_write_state` in `dashboard/handlers/computer_use.py`) does read-modify-write, so
  toggling the primary enable keeps the key. `computer_use/gate.py` is not touched,
  and phase 3's class row lives in `platform/governance_parts/tool_calls.py`.
  AGENTS.md keeps Computer Use to one operator opt-in on the keystone and forbids
  `computer_use.*` scopes, capability rows, approval ordinals and pointer permits.
  This key is none of those: it is an operator setting on the same keystone, like
  `allowed_apps` and `extra_denied_apps`, and adds nothing to `SCOPE_CATALOG`.
  Whether a second boolean beside `enabled` still counts as one opt-in is the
  maintainers' call; option (d) of open question 2 is the shape without the key.

## 9. Alternatives considered

- **Keep the path and prompt the model to read it.** Already the instruction; it
  costs one extra call per look, and the model has to decide to make it.
- **Embed the image as base64 text.** The model would receive a string, not an
  image, at several thousand characters per screenshot.
- **Codex-style code execution** (a JavaScript REPL driving the desktop). Rejected:
  model-written code would run inside the gateway process that holds desktop access,
  and every action inside a script would bypass the per-call chokepoint and audit.
- **Images on every mutating tool.** Rejected for now: each image is replayed with
  the history on every later turn. The batch's single final image covers the
  "act, then look" case.
- **Leave multi-step work to Jev.** Jev serves closed subtasks and does not read
  images (`rfc-jev-task-executor` section 6). Both can exist: Jev for cheap, closed
  errands; the batch for steps the chat model plans itself.

## 10. Open questions

1. **One approval for a batch.** Do maintainers accept that one `computer_batch`
   call covers up to `MAX_BATCH_STEPS` actions under one approval, given every step
   is checked by name, dispatched and audited on its own? Options: (a) accept;
   (b) offer the tool only in sessions whose approval mode already pre-approves
   Computer Use; (c) drop the batch and ship only inline screenshots.
2. **Default of the keystone's `inline_screenshots` on a member harness.** (a) On;
   (b) off, so an operator opts in; (c) on for `computer_get_state`, off for the
   batch; (d) no key: images follow the primary enable alone on a harness in
   `ACP_BACKENDS_TOOL_RESULT_IMAGES`, which keeps Computer Use to one opt-in but
   leaves an operator no way to stop each screenshot reaching the model provider
   while Computer Use is on.
3. **Truncated trees.** (a) Keep today's rule: a window whose report exceeds
   `max_tree_nodes` is not photographed unless the model raises the budget;
   (b) photograph when the cheap secure scan reached the end of the window, which
   needs `Snapshot` to carry "scan complete" apart from "report truncated", with the
   node, deadline and per-node children cutoffs all counted as incomplete; (c) another
   rule. This is the floor that decides whether pixels leave the process, so it is the
   maintainers' call.
4. **Other harnesses.** Who runs the section 2.3 probe for Kiro on a later kiro-cli,
   Claude Code, Codex, OpenCode and DeepSeek, and is a recorded probe enough to join
   the set?
5. **Order with Jev.** Does the phase 1 split land here, shaped as
   `rfc-jev-task-executor` section 3.5 describes so Jev's `dispatch_structured`
   adapter can sit on it, or does this wait for Jev's step 4?
6. **Batch limits.** `MAX_BATCH_STEPS` (20 is a placeholder), the allowed step
   tools, and whether `click_method: "global"` may appear in a batch at all.
