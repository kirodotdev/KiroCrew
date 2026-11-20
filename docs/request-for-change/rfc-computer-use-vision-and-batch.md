---
title: Computer Use -- screenshots the model can see, and several actions in one call
status: draft
author: inariku, with Kiro Crew
created: 2026-10-26
last-audited: 2026-11-20
audited-at: 3a651796a0
doc-pr: 24210
implementation-prs: []
tracking-issues: [24212]
supersedes: []
superseded-by: []
---

# RFC: Computer Use -- screenshots the model can see, and several actions in one call

- Status: `draft`. Nothing here is built. Measured against main `3a651796a0`. The
  draft [#24316](https://github.com/kirodotdev/KiroCrew/pull/24316) prototypes an
  earlier shape of this design (section 6).
- Related: [`rfc-jev-task-executor`](rfc-jev-task-executor.md) (accepted), whose
  rollout step 4 splits the same dispatch chokepoint this design needs;
  [`rfc-remote-computer-use`](rfc-remote-computer-use.md) (draft), whose wire carries
  the same screenshot bytes; window targeting in
  [#24203](https://github.com/kirodotdev/KiroCrew/pull/24203) for
  [#18543](https://github.com/kirodotdev/KiroCrew/issues/18543), which this document
  assumes but does not depend on; and the decision
  [a Kiro CLI choice runs on KAS](../decisions/2026-10-28-a-kiro-cli-choice-runs-on-kas.md),
  under which a session that chose Kiro CLI runs on KAS's path whenever the installed
  kiro-cli offers its v3 engine, so the capability set in section 5.3 applies to it
  too.

## 1. Summary

Computer Use gives the chat model a text tree and, at most, the path of a JPEG on
disk. To see the window, the model has to spend a second tool call reading that file,
and whether that read works depends on the harness and its sandbox (section 2.2). On
a large browser window it gets no screenshot at all. Each action is one tool call
followed by a full re-walk, so a ten-field form is ten model turns.

This document proposes two changes:

1. **A screenshot in the reply, when the call asks for one.** `computer_get_state`
   gains an optional boolean argument, `inline_image`. When a call sets it, the
   calling session's harness is in a positive set measured to pass a tool-result
   image to the model (`ACP_BACKENDS_TOOL_RESULT_IMAGES`, starting as `{KAS}`), the
   screenshot passes today's capture floors, and a file read of the spooled JPEG by
   that session would run today without a prompt, the reply carries the screenshot
   as an MCP image block beside today's text. A call that omits the argument gets
   today's reply byte for byte. Nothing is added to the keystone or to `config.json`,
   and no default changes.
2. **`computer_batch`.** One call runs a short list of actions against one window.
   Each step is checked by name against the governance decision before any step
   runs, then goes through the existing dispatch chokepoint; the batch stops at the
   first refusal or UI drift and returns one refreshed tree at the end.

For the first change, the decision asked of the maintainers is section 10.1: may
`computer_get_state` return an image when the call asks for one? The whole-window
floor for a window holding a password field and the rule that a truncated tree is not
photographed are unchanged.

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
  transport". Section 2.4 shows that is a property of these three links, not of the
  harness.
- **The screenshot is a path.** `service._persist_image` writes the JPEG under the
  gateway's temp directory (`service._shot_dir`, `<tmp>/kirocrew-computer-shots`), and
  the note `render._render_image_note` appends (`types.SCREENSHOT_NOTE`) ends "Read
  the file with the fs_read tool only if the tree is insufficient." The tool
  description in `mcp_computer._tool_definitions`, the server instructions in
  `mcp_instructions.py` and the `computer-use` skill say the same.
- **Capture is the caller's choice.** The call's `screenshot` argument decides whether
  a screenshot is taken. `computer_use.attach_screenshot` is only the default for a
  call that omits it: `service.snapshot_request` takes the caller's value first, and
  `docs/system-specs/modules/config.md` documents the key as that default. No setting
  stops a screenshot a call asks for.
- **A truncated tree is never photographed.** `capture_macos.capture_snapshot_image`
  and `capture_windows.capture_snapshot_image` return no image when
  `snap.truncated or snap.depth_truncated`. On macOS, `snapshot_macos._walk` sets
  `truncated` as soon as the REPORT reaches `max_tree_nodes` (default 1200), even
  though it keeps classifying every remaining node for `saw_secure` up to
  `MAX_TREE_NODES_LIMIT` (5000). It also sets `truncated` when the secure scan really
  stops early: at that 5000-node cutoff, at the `MAX_WALK_SECS` deadline, or when a
  node reaches `MAX_CHILDREN_PER_NODE` (512) children. A `Snapshot` cannot tell
  the first case from the other three, so all of them suppress the image. The
  `computer-use` skill calls this suppression "Routine for a browser or an Electron
  app at the default budget".
- **One action per call, one walk per action.** Every mutating tool in
  `computer_use/tools.py` `_run` verifies the target's fingerprint against a fresh
  walk (`service.verify_fingerprint`), acts, settles for `POST_ACTION_SETTLE_SECS`,
  re-walks and renders the whole tree, with `want_image=False`.
- **Nothing structured leaves the chokepoint.** `tools._dispatch` returns a string.
  `rfc-jev-task-executor` section 3.5 proposes splitting it into one executor and two
  output adapters; `git grep dispatch_structured -- src` is empty at `3a651796a0`, so
  that step has not landed.

### 2.2 What the model can read today

The model can already see a screenshot by reading the spooled JPEG with its
file-read tool. Where that works was checked on 2026-11-20:

| Configuration | Can the session read the spooled JPEG? | How it was checked |
|---|---|---|
| macOS, KAS (`kiro-cli acp --agent-engine v3`), Kiro Crew's sandbox off | Yes, and the model receives it as an image | A KAS session's `read_file` of a synthetic 256x256 JPEG reached the model as an image |
| macOS, Kiro Crew's own sandbox in `standard` (`auto`), `cc` and `strict` | Yes, at the file level | The seatbelt profile main's `sandbox.sandbox_exec_argv` builds let a child read a JPEG in `kirocrew-computer-shots` in all three modes, while `strict` refused `~/.ssh`. The masks are relative to `$HOME` and the crew home (`sandbox_plan._mask_sources`), and none names the gateway's temp directory. Run by calling `sandbox_exec_argv` directly, not through the ACP launch |
| macOS, file confinement delegated to kiro-cli's own sandbox (`agent_sdk.backends.spawn_delegates_to_internal_sandbox`) | Unknown | `security.md` says kiro-cli's profile refuses reads of files outside the session workspace that existed before the session, while files the session created stay readable. The gateway writes the screenshot outside the workspace after the session starts, so which of the two applies is not known. kiro-cli's profile is not in this repository |
| Windows, any harness | Not run | Kiro Crew has no OS sandbox on Windows. A Kiro CLI (v2) session delegates to Kiro's own sandbox there, whose scope was not checked |

`docs/system-specs/modules/security.md` records the readable directory as an
accepted residual ("the screenshot directory stays agent-readable"). So today a look
costs the model a second tool call, and the operator's read rules can stop that read
(section 8). The first change removes the second call on a harness that can carry the
image in the tool result, and keeps those read rules deciding (section 5.3).

### 2.3 What this costs

The model drives a GUI from a text outline it cannot check against the pixels. When
the outline is wrong or thin (Electron stubs, unlabeled buttons, canvases), it has to
decide on its own to read a file, at the cost of one more tool call, and only where
that read works (2.2). A form with ten fields is ten round trips through the model,
each re-reading a tree of up to 1200 nodes. Section 2.5 compares Kiro Crew with Codex
on these points.

### 2.4 Probe: which harnesses pass a tool-result image to the model

The synthetic and real-screenshot runs used kiro-cli 2.34.0 on macOS; later kiro-cli
releases were not probed.

**Synthetic image, 2026-10-26.**

1. A stdlib stdio MCP server with one tool whose result is ONLY
   `{"type": "image", "data": <base64 PNG>, "mimeType": "image/png"}`. The PNG shows
   six random capital letters that appear nowhere in text.
2. A workspace-local agent config loading only that server.
3. `kiro-cli chat --no-interactive --agent <probe> --trust-tools=<the tool>` with the
   prompt: call the tool once and reply with only the word drawn on the image, or
   NONE.

The first two runs (`LWZIVA`, `BKGISO`) passed no `--agent-engine`, so they ran on
kiro-cli's default engine of that date, most likely v2 (Kiro); the engine was not
recorded. A third run with `--agent-engine v3`, the engine KAS uses, read `JNWMMK`.
All three answered the word exactly.

**Real screenshot, on the prototype branch
[#24316](https://github.com/kirodotdev/KiroCrew/pull/24316).** Preview showed a white
shape (random circle, triangle or square) on a random background colour. The branch's
dispatcher captured it, the gateway's `_inline_image` fitted it, and
`build_tool_response` built the text-plus-image frame, with the answer words checked
to be absent from the text. A stdio MCP server returned that frame to
`kiro-cli chat --agent-engine <v2|v3>`. No run used Kiro Crew's own KAS launch, and
the shim's HTTP hop to a running gateway was not exercised live.

- v3 (KAS): the model named colour and shape correctly every time. The prototype PR
  quotes two of those answers (`red square`, `orange square`) and does not record the
  total count. With no image attached it answered `NONE`.
- v2 (Kiro): it never named both colour and shape in nine runs, across JPEG and PNG,
  480 to 1280 px, with and without the text block.

A small text-free PNG is therefore not enough evidence for a harness: the probe that
admits one uses a real screenshot. Main has no image path to run that probe against.

**File read, 2026-11-20.** On a KAS session (`kiro-cli acp --agent-engine v3`),
`read_file` of a synthetic 256x256 JPEG reached the model as an image (section 2.2).

Kiro Crew's own ACP parser
(`acp/_dispatch_parts/tool_result_events.py` `tool_call_content_text`) renders only
text blocks and stays silent on `image`, so the dashboard transcript shows no image
bytes. No harness outside kiro-cli has been probed.

### 2.5 Comparison with Codex

OpenAI's Codex ships a Computer Use feature for the same job: an agent driving the
user's desktop apps. The Codex column comes from public sources: OpenAI's
[computer use API guide](https://developers.openai.com/api/docs/guides/tools-computer-use)
and [Codex use-case page](https://developers.openai.com/codex/use-cases/use-your-computer-with-codex),
issues on [openai/codex](https://github.com/openai/codex), and two third-party
analyses of the Codex plugin
([codex-computer-use-cli](https://github.com/paralym/codex-computer-use-cli),
[opencode-codex-computer-use](https://github.com/malhashemi/opencode-codex-computer-use)).
Rows marked (third-party) rest on those analyses alone. The Kiro Crew column is
measured at `3a651796a0`.

| | Codex | Kiro Crew today | This document |
|---|---|---|---|
| What an observation gives the model | The accessibility tree and a screenshot, the image returned to the model (third-party) | The tree, and a screenshot only as a spooled file path | The image in the same reply when the call asks, on measured harnesses (5.2, 5.3) |
| A large browser window | Photographed (third-party) | Not photographed once the report exceeds `max_tree_nodes` (2.1) | Unchanged (section 10.2) |
| Actions per model call | Several: the API guide recommends a code-execution mode that runs many actions, loops and branches in one call, and Codex exposes Computer Use as a JavaScript runtime, `cua_repl` ([openai/codex#42454](https://github.com/openai/codex/issues/42454)) | One, then a full re-walk | Several, as a fixed list of tool steps, not code (5.4, section 9) |
| Choosing among an app's windows | Targets by app name; per-agent window ownership is requested in [openai/codex#20852](https://github.com/openai/codex/issues/20852) | One window per app on main; `window_id` in [#24203](https://github.com/kirodotdev/KiroCrew/pull/24203) | Assumed, not required |
| Web pages and tabs | Handled inside the same runtime through a Chrome extension, with tab handles and locators (third-party) | Separate tools (the Browser panel, `playwright-cli`), not Computer Use | Out of scope (section 4); requested in [#23095](https://github.com/kirodotdev/KiroCrew/issues/23095) and [#10974](https://github.com/kirodotdev/KiroCrew/issues/10974) |
| Background input on macOS | SkyLight per-process event posting ([Cua write-up](https://github.com/trycua/cua/blob/main/blog/inside-macos-window-internals.md)) | The same SkyLight calls (`macos_skylight.py`) | No change |
| Permission | Per app, with "Always allow" (use-case page) | One operator opt-in on the keystone `computer_use.json`, ANDed with `capabilities.computer_use` | No change |

The foundations match: both read the accessibility tree and post input to a
background process the same way. This document addresses the first and third rows,
which decide how often the model can check its work against the pixels and how many
turns a multi-step task costs.

## 3. Goals

- G1. A `computer_get_state` call that asks for the image gets it in the same reply as
  the tree, on a harness measured to pass tool-result images, without a second tool
  call.
- G2. A call that does not ask gets exactly today's reply. A call that asks on a
  harness not measured gets today's `screenshot: true` text plus one line saying no
  image is attached.
- G3. The image leaves only when a file read of the spooled JPEG by the same session
  would run today without a prompt. Where Kiro Crew cannot establish that, no image
  leaves.
- G4. A short run of actions on one window costs one model turn, not one per action,
  and still passes every per-action check the single tools pass.
- G5. No floor moves: a window holding a secure field is never photographed, and the
  truncated-tree rule changes only through section 10.2.
- G6. One image can never wedge a session's history: every inline image is held to
  the budget `kiro_crew/imaging.py` defines (`MAX_IMAGE_EDGE_PX`,
  `MAX_IMAGE_B64_BYTES`) before it leaves the gateway.
- G7. No new setting: no keystone key, no `config.json` key, no `computer_use.*`
  scope, no capability row, and no changed default.

## 4. Non-goals

- Photographing a truncated tree. Section 10.2's recommended answer keeps today's
  rule, so a Chrome or Electron window at the default `max_tree_nodes` still gets no
  image, and this document alone does not solve the Chrome case in
  [#31198](https://github.com/kirodotdev/KiroCrew/issues/31198). Changing that rule
  would be its own RFC.
- Attaching an image the call did not ask for. Section 9 says why.
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
  end of a `computer_batch` can attach one, and only when the call asks.

## 5. Design

### 5.1 One structured result from the chokepoint

This is the split `rfc-jev-task-executor` section 3.5 describes: `_dispatch`, with
the `_run` it enters, becomes one internal executor that returns a private
`DispatchResult`, and every in-line refusal becomes a typed refusal. `dispatch_tool`
is the text adapter and renders exactly today's text, so every current caller and
test sees byte-identical output. Jev's `dispatch_structured` adapter is not part of
this document (section 10.4 is about the order).

This document adds one field to `DispatchResult`: the screenshot to attach, `image`
(bytes and MIME type) or nothing. The executor fills it only after the attach
decision in 5.3 passes, so a caller of the executor never receives bytes the
decision did not allow. `dispatch_structured` omits screenshot bytes and paths, as
Jev section 3.5 already requires, and a phase 2 test pins that no adapter other than
the invoke path's carries them.

### 5.2 The request: `inline_image` on `computer_get_state`

- `computer_get_state` gains `inline_image`, a boolean with no default; absent means
  `false`. It is added to `TOOL_GET_STATE` in
  `validation_parts/computer_use_schemas.py` as `FieldSpec("inline_image", bool)` and
  to the advertised `inputSchema` in `mcp_computer._tool_definitions`;
  `test_advertised_schema_matches_the_validated_fields` keeps the two in step.
- `inline_image: true` takes a screenshot the way `screenshot: true` does.
  `screenshot: false` together with `inline_image: true` is refused as contradictory,
  the way `computer_click` refuses an index and coordinates together.
- With the argument absent or `false`, the reply is today's, byte for byte.
  `test_no_result_ever_carries_an_image_content_block` and
  `test_get_state_relays_a_path_not_bytes` call without it, so their assertions stay
  and pin that.
- The argument is advertised on every harness, since one server's tool list is
  shared by its callers. Its description says to ask only when the pixels are
  needed, and that the reply states whether the image was attached.
- The request is part of the call's own arguments, but the default path does not
  judge them: `hook_runtime/tool_identity._cu_read_only_auto_approve` approves
  `computer_get_state` by name whenever Computer Use is on. The read check in 5.3 is
  therefore the per-call control on that path. Where an interactive prompt,
  `confirm_tools` on `computer_get_state`, a session's approver or a governance `mcp`
  rule applies, it decides on a call whose arguments ask for an image.
- A new argument rather than a new value of `screenshot`: `screenshot` is a `bool`
  field whose omission defers to `attach_screenshot`. A string value such as
  `"inline"` would make it a union type in both the validator and the JSON Schema and
  change a documented parameter.

### 5.3 When the image is attached

The executor attaches the image only when all of these hold. Otherwise the reply is
today's `screenshot: true` text plus one line saying the image is not attached, why,
and that the path above can be read.

1. The call set `inline_image: true`.
2. The calling session's harness is in `ACP_BACKENDS_TOOL_RESULT_IMAGES`.
3. The snapshot carries image bytes after the capture floors (no secure field, no
   truncated tree) and the observation check `gate.permitted_observation_channels`
   (which withholds nothing on main).
4. A file read of the spooled path by this session would run without a prompt (the
   read check below).
5. `imaging.downscale_image_block` fits the bytes to the `kiro_crew/imaging.py`
   budget. A block whose compliance cannot be established is dropped.

**The harness is the calling session's.** `ACP_BACKENDS_TOOL_RESULT_IMAGES` is a
positive set beside the other `ACP_BACKENDS_*` sets in `agent_sdk/backends_turns.py`
(`ACP_BACKENDS_STEER`, `ACP_BACKENDS_COMPACT`), and membership is never inferred
from the absence of another harness. It starts as `{ACP_BACKEND_KAS}`, provisionally:
section 2.4 measured KAS on a real screenshot through `kiro-cli chat`, and phase 2's
probe through Kiro Crew's own launch is what admits it (section 10.3). One agent
spec feeds every harness (kiro-cli reads it, KAS gets a projection), and
`kirocrew-computer` may run as one pooled process, so neither the spec nor the shim
can say which harness a call came from. The executor resolves it from the invoke
request's `session_key`, through the same per-session choice the session started on
(`members_parts/member_sessions.select_provider_backend`, fed by
`session_backend_pick` and the crewmate pin) or from the live session. A session the
gateway cannot name (the shim's unresolved-session key) gets no image. This also
replaces the prototype's reading of the configured `agent.acp_backend`, which cannot
tell apart a session started on another harness by a per-member or per-spawn
override.

**The read check.** Today the pixels reach the model through a separate file-read
call, which Kiro Crew's PreToolUse gate judges. The inline image skips that call, so
the executor asks the gate's question for it: would a file read of the spooled path,
by this session, with this session's own file-read tool, run without a prompt? It
builds that call and runs it through the gate's decision for the session, and
attaches the image only on a run-without-asking answer. A deny, a would-prompt answer
or any evaluation error means no image. On main that decision includes:

- the always-on deny floor and the operator's deny rules: `hooks.auto_deny_tools`,
  the user-added patterns in the keystone `denied_commands.json` and the companion
  overlay (`hook_runtime/gate_tiers._tier_deny_rules`, `_tier_mcp_auto_deny`);
- governance `POLICY ∩ PROFILE` for the session's `session_key`, `agent` and `app`
  (all three are on the invoke request): a `filesystem.read` deny on the path or a
  `tools` deny of the file-read tool (`resolve_active_scope` and `gate_decision`, the
  pair `hook_runtime/governance_gate._governance_denial` uses);
- `hooks.confirm_tools` naming the file-read tool, or a list that could not be read
  (`confirm_tools_unknown`);
- the gate's own approval of an ordinary read: a read a spec's `allowedPaths` grant
  (`_spec_read_granted`), or one the read-only tier auto-approves
  (`_tier_read_only`).

The executor also checks that the agent could make the read at all: an agent whose
spec (`tools`, `disabledTools`, `toolsSettings`) leaves it no file-read tool for
that path cannot read the file today, so it gets no image.

Two more cases attach nothing because Kiro Crew cannot ask them:

- **A delegated harness sandbox.** When kiro-cli's own sandbox confines the session
  (`agent_sdk.backends.spawn_delegates_to_internal_sandbox`), Kiro Crew cannot tell
  whether that sandbox would let the read through (2.2).
- **Sessions whose reads go to an approver.** A heartbeat session's scoped hooks set
  `defer_reads_to_approver`, so its approver judges every read, and the invoke
  request does not carry that flag. On main the case does not arise: heartbeat's
  approver (`slack/gateway_runtime/tool_policy._is_heartbeat_safe_tool`) refuses
  every `kirocrew-computer` tool unless an edition pins one in
  `heartbeat_safe_tools()`. Phase 2 carries the flag to the executor or attaches no
  image for such a session.

`mcp_gateway/app_call._governance_denial` is a precedent for answering a governance
question for a session outside the PreToolUse gate and failing closed.

Not verified: that the gate's decision can be evaluated for a built call from the
executor with this session's facts and the same ceiling the PreToolUse gate holds;
which of the two ways of resolving the session's harness the executor can reach; how
the executor reads the agent's file-read grant and learns per session that
kiro-cli's sandbox is in charge; and which of
the two approver options is possible. Phase 2 settles these first. If the read check
cannot be built as a whole, phase 2 attaches no images and this document goes back
to the maintainers.

**Audit.** Every request writes an SEL record through `sel().log_tool_invocation`, as
`gate._audit_allowed` does for each call, with `inline_image=attached` or
`inline_image=withheld:<reason>` in `resources` (`harness`, `floor`, `read_check`,
`delegated_sandbox`, `session` or `budget`). Today the separate file read leaves its
own record; this record takes its place.

**Where the decision runs.** Inside the executor, in band like the other Computer Use
refusals (AGENTS.md: "in band on `tools._dispatch`"), never at the fail-open `hooks`
gate, so a pre-authorized `computer_get_state` cannot skip it.

**What leaves the gateway.** `api_computer_use_invoke` adds an `image` member
(`{"mimeType", "data"}`) to its `{"text": ...}` reply when `DispatchResult.image` is
set. The shim returns a text block and the image block. `build_tool_response` gains
an optional image parameter and the stdio frame an optional second content block;
existing callers are unchanged. The budget applies whether or not the server is
brokered by the MCP gateway, whose relay (`mcp_gateway/image_budget.py`) applies the
same budget only to brokered servers. The text half still names the spooled path and
says the image is attached, so the live view, the user and a harness that later
drops the block are unaffected.

Mutating tools stay `want_image=False`. `computer_batch` can attach one image at its
end (5.4).

### 5.4 `computer_batch`

```
computer_batch(app, window_id?, steps, screenshot?, inline_image?)
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
  refreshed tree, cached for the next call, plus the screenshot when the batch set
  `inline_image` and 5.3 applies. Steps that ran are not undone; the reply says so.
- **Excluded:** `computer_launch_app`, observation tools, `computer_end_turn`, and
  `click_method: "global"` (the one path that moves the user's real pointer), pending
  section 10.6.
- **Classification.** `batch` gets a row in the existing code-owned
  `_CU_ACTION_CLASSES` table (`platform/governance_parts/tool_calls.py`) carrying
  `mutate`, so `hooks._cu_read_only_auto_approve` never auto-approves it as an
  observation and `gate`'s is-it-mutating check reads it correctly. The row adds no
  `computer_use.*` scope and no approval ordinal. It is not a governance control:
  no evaluator reads a class pattern such as `@mutate` as a deny today, so the
  by-name step check above is what enforces a ceiling.

## 6. Migration plan

Each phase is one PR. The prototype
[#24316](https://github.com/kirodotdev/KiroCrew/pull/24316) implements phases 1 and
2 in the earlier shape this document has dropped (a keystone key, section 9). Once
this document is decided it is rebased once and changed to the design above: the
keystone key and its reader go, and the argument and the read check come in.

1. **Structured result** (5.1). No behaviour change.
   Exit: every existing computer-use and MCP-computer test passes unchanged; a new
   test asserts `dispatch_tool` returns `DispatchResult.text` byte-for-byte for every
   tool on the fake backend. Uses the shape section 10.4 recommends.
2. **Image on request** (5.2, 5.3) for `computer_get_state`.
   Exit:
   - a call without `inline_image` returns a reply identical to main on every harness;
   - on a member harness a call with it returns a text block naming the path and an
     image block;
   - on a non-member harness, including a Kiro CLI (v2) session of the same agent,
     the same call returns today's `screenshot: true` text plus the one line, and no
     image block;
   - a session the gateway cannot name gets no image block;
   - a window holding a secure field and a truncated walk return no image block when
     asked;
   - `screenshot: false` with `inline_image: true` is refused;
   - each of these gives no image block and an SEL `withheld` record with its
     reason: an operator `auto_deny_tools` rule matching the read, a governance
     `filesystem.read` deny on the spooled path, a `tools` deny of the file-read tool,
     a `confirm_tools` entry naming it, an unreadable `confirm_tools` list, an agent
     whose spec grants no file-read tool, and kiro-cli's own sandbox in charge;
   - an attached image writes an SEL `attached` record;
   - an oversized fake image is downscaled, and an undecodable one is dropped with the
     path note kept;
   - no adapter other than the invoke path's carries image bytes;
   - the dashboard transcript stores no base64;
   - the specs, docs, scripts and test docstrings in section 7.1 change in the same
     PR.
   A real-machine probe through Kiro Crew's own KAS launch, on both the relay route
   (`kiro-cli acp --agent-engine v3`) and the bundle route (`agent.kas_bundle`), not
   through `kiro-cli chat`, names content shown only in a real window. It is recorded
   as manual evidence on the PR, not as a test. A route that fails it does not get
   images, and if both fail the set ships empty. Blocked on section 10.1.
3. **`computer_batch`** (5.4).
   Exit: a batch of N steps produces N per-step SEL records under the step tool
   names; a step that opens a dialog makes the next element step fail with drift and
   the reply marks the rest `not run`; a step naming another `app` or `window_id`, a
   launch step and a `global` click are refused before any step runs; with an `mcp`
   deny on `@kirocrew-computer/computer_type_text`, a batch containing a
   `computer_type_text` step is refused before its first step runs, and the same
   batch without that step runs.
   Blocked on sections 10.5 and 10.6.
4. **Truncated-tree capture**: not planned. Section 10.2's recommended answer keeps
   today's rule; a change would be its own RFC.

## 7. Backward compatibility

Compatible. A call that omits `inline_image` gets a byte-identical reply on every
harness, the argument is new and optional, `build_tool_response` gains only an
optional parameter, nothing in the keystone or `config.json` changes, and
`computer_batch` is a new tool.

### 7.1 What else phase 2 changes

The implementation PR updates these in the same commit as the code:

- `docs/system-specs/modules/computer-use.md`, "Result shape: text only, by
  construction": becomes the section 5.3 rule (an image block only when the call
  asks, the harness is a member, and the capture floors and the read check pass;
  text only otherwise).
- The same spec's screenshot-spool paragraph, which says "Only the **path** is ever
  relayed; the bytes never enter a result."
- `docs/system-specs/modules/security.md`, "Accepted residual -- the screenshot
  directory stays agent-readable": add the result path and the read check that keeps
  it no wider than the file read.
- `src/kiro_crew/computer_use/render.py`: the module docstring ("cannot express an
  image block"), and `types.SCREENSHOT_NOTE`.
- `src/kiro_crew/mcp_computer.py` `_tool_definitions` (its docstring and the
  `computer_get_state` description), `src/kiro_crew/mcp_instructions.py` ("you get a
  **file path**, not an image") and `src/kiro_crew/builtin_skills/computer-use/SKILL.md`
  ("You receive a file path, not an image" and "you get a **path**, not an image"):
  ask with `inline_image: true` only when the pixels are needed, and read the path
  when the reply says no image is attached. The skill's "Request a screenshot on the
  first snapshot of each turn" stays about `screenshot`, so the live view keeps its
  frame without every snapshot sending an image.
- `test/test_mcp_computer.py` `test_no_result_ever_carries_an_image_content_block`
  and `test_get_state_relays_a_path_not_bytes`: the assertions stay, since both call
  without the argument and now pin the default; their "Text only, by construction"
  docstrings say why that still holds for such a call.
- `test/test_validation.py` `test_content_is_list_of_one_text` stays as it is:
  `build_tool_response` still emits one text block when it is given no image.
- `src/kiro_crew/mcp_shared_dispatch.py` `_tool_response(text)`, the shared stdio
  exit every managed server uses, and the `Error:` prefix check in
  `mcp_shared.call_tool_with_logging`, which classifies a text result as failed: both
  gain the optional image half without changing what a text-only result does.
- `scripts/check_harness_parity.py`: the new `ACP_BACKENDS_TOOL_RESULT_IMAGES` set is
  classified (`check_coverage` fails on an unclassified `ACP_BACKENDS_*` set), with
  the per-set decision `docs/system-specs/modules/harness-parity.md` asks for when an
  invariant is added or changed.
- Nothing is added to `_SECURITY_KEYS` (`config/loosening_provenance.py`) or
  `SECURITY_RULES` (`website/src/pages/settings/SecurityPanelConfigPending.tsx`):
  with no new key there is nothing for the pending-change review to list.

## 8. Security considerations

- **When pixels reach the model provider.** Only when a call asks for the image and
  a file read of the spooled JPEG by that session would have run without a prompt
  (5.3). Today the same pixels reach the provider when the model reads that file.
  What changes is that a look costs one call instead of two, and the executor asks
  the gate's question about the read (5.3) instead of the gate judging a separate
  call. `computer_get_state` itself is auto-approved by name whenever Computer Use is
  on (5.2), so the read check is the per-call control on the default path.
- **What stops it.** Capture: no setting on main stops a screenshot a call asks for
  (2.1), and this document does not change that. Reading: an earlier revision of
  this document said "No setting stops a screenshot the model asks for", which holds
  for capture and is wrong for reading. Today's file read can be stopped by the
  operator's deny rules, a governance `filesystem.read` or `tools` deny, a
  `confirm_tools` entry, an agent spec without a file-read tool, a session's approver
  and, where Kiro Crew delegates confinement, kiro-cli's own sandbox. The read check
  in 5.3 carries each of these to the inline image, or attaches nothing where it
  cannot ask. The switches over the whole feature also stop it: the keystone opt-in,
  `capabilities.computer_use`, and an `mcp` rule on
  `@kirocrew-computer/computer_get_state`.
- **Pixels are not redacted.** The text tree passes `policy.redact_result`; an image
  cannot. That is already true of the spooled file. The secure-field floor (no capture
  when any node is secure) is what keeps a password box out of the frame, and it is
  unchanged.
- **Where the image is stored.** kiro-cli keeps it in its session history and replays
  it on later turns (as `mcp_gateway/image_budget.py` describes), so each image costs
  tokens on every later turn. That cost is paid only for images a call asked for.
  Kiro Crew's transcript does not store it: `tool_call_content_text` renders text
  blocks only (phase 2 pins this with a test). The spooled JPEG keeps today's `0o600`
  file and ring trim.
- **No history wedge.** The gateway route downscales or drops every image before it
  leaves (G6), independent of the MCP gateway relay.
- **Audit.** Every request records whether the image was attached, and why not (5.3).
- **No new Computer Use row.** AGENTS.md governs Computer Use by one on/off row, the
  operator opt-in on the keystone `computer_use.json` ANDed with
  `capabilities.computer_use` ([`rfc-crew-lts-edition`](rfc-crew-lts-edition.md),
  appendix "Desktop control"), and forbids finer `computer_use.*` scopes, more
  capability rows, approval ordinals and pointer permits. The first change adds none
  of these and no key. The on/off row still decides whether `computer_get_state` runs
  at all; the read check asks only the questions the gate already asks of a file
  read (deny rules, `filesystem.read`, `tools`, `hooks.confirm_tools`, the spec's
  grant).
  `ACP_BACKENDS_TOOL_RESULT_IMAGES` states what a harness can carry in a tool result,
  like the other `ACP_BACKENDS_*` sets, and is not a governance row. The attach
  decision runs in band (5.3).
- **Sensitive paths.** The keystone, `computer_use.json` and `computer_use/gate.py`
  are not touched; phase 3's class row lives in
  `platform/governance_parts/tool_calls.py`.
- **Batch.** Every step passes the full chokepoint and is audited separately. What
  changes is that one tool call, and therefore one interactive approval in a
  non-autopilot session, covers several actions. The spec's `call` section says a
  tool that runs other tools "would let a model launder one per-call gate decision
  into many"; section 10.5 asks the maintainers to rule on that for this tool.

## 9. Alternatives considered

- **A keystone key, `inline_screenshots`, that attaches every screenshot.** The
  previous revision of this document proposed it and left its default open. With it
  on, every captured screenshot is sent and replayed whether or not the model needed
  it. It adds a second operator boolean beside `enabled` on the keystone, a Settings
  write path and an entry in `_SECURITY_KEYS`, and it raises whether that boolean is
  a second Computer Use row under AGENTS.md. If the maintainers prefer this shape to
  the per-call request, the author's answer for its default is off, the same as
  bolichen97's non-binding view on 2026-10-26.
- **No key: attach whenever Computer Use is on and the harness is a member.** This
  changes the default for every Computer Use user on KAS, which under the 2026-10-28
  decision includes a Kiro CLI choice whenever the installed kiro-cli offers v3, and
  leaves an operator no way to keep pixels out while Computer Use is on.
- **Reuse `computer_use.attach_screenshot`.** Its documented meaning is the default
  for `screenshot`; making it decide inline images changes a documented key.
- **A new value of `screenshot`.** See 5.2.
- **Keep the path and prompt the model to read it.** Already the instruction; it
  costs one extra call per look, and where the harness or its sandbox cannot read the
  file (2.2) the model cannot see it at all.
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

## 10. Open questions and recommended answers

Sections 10.1, 10.5 and 10.6 are open. Sections 10.2 to 10.4 carry the author's
recommended answers; the maintainers decide, and a sign-off on 10.1 accepts them
unless the maintainer says otherwise.

### 10.1 May `computer_get_state` return an image when the call asks for one?

This replaces the spec's "text only, by construction" with the section 5.3 rule: an
image block only for a call that sets `inline_image`, from a session whose harness is
in `ACP_BACKENDS_TOOL_RESULT_IMAGES`, after the capture floors and the read check.
Author's recommendation: yes, as 5.2 and 5.3 describe. Section 9 lists the shapes
considered instead.

### 10.2 Truncated trees

(a) Keep today's rule: a window whose report exceeds `max_tree_nodes` is not
photographed unless the model raises the budget; (b) photograph when the cheap secure
scan reached the end of the window, which needs `Snapshot` to carry "scan complete"
apart from "report truncated", with the node, deadline and per-node children cutoffs
all counted as incomplete; (c) another rule. Author's recommendation: (a). This is the
floor that decides whether pixels leave the process, and the image request does not
need it changed. As a result this document alone does not solve the Chrome case in
[#31198](https://github.com/kirodotdev/KiroCrew/issues/31198); (b) would be its own
RFC.

### 10.3 Other harnesses

Author's recommendation: a harness joins `ACP_BACKENDS_TOOL_RESULT_IMAGES` on a
recorded real-machine probe, section 2.4's real-screenshot probe run through Kiro
Crew's own launch for that harness and written into this document. The set starts as
`{KAS}`, provisional until phase 2's probe (section 6); Kiro on a later kiro-cli,
Claude Code, Codex, OpenCode and DeepSeek join only that way.

### 10.4 Order with Jev

Author's recommendation: phase 1 lands here in the shape `rfc-jev-task-executor`
section 3.5 describes (one executor returning a private `DispatchResult` with typed
refusals, and `dispatch_tool` as its text adapter), so Jev's `dispatch_structured`
adapter can sit on the same split later instead of a second one. Jev's author is
asked before phase 1 opens.

### 10.5 One approval for a batch

Do maintainers accept that one `computer_batch` call covers up to `MAX_BATCH_STEPS`
actions under one approval, given every step is checked by name, dispatched and
audited on its own? Options: (a) accept; (b) offer the tool only in sessions whose
approval mode already pre-approves Computer Use; (c) drop the batch and ship only the
screenshot change.

### 10.6 Batch limits

`MAX_BATCH_STEPS` (20 is a placeholder), the allowed step tools, and whether
`click_method: "global"` may appear in a batch at all.
