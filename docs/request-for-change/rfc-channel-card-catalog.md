---
title: Channel card catalog — one validated, redacted card spec rendered to Slack, Teams, Discord and plain text
status: draft
author: kseam
created: 2026-11-10
last-audited: 2026-11-10
audited-at: b58eef1f9c
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Channel card catalog

- Status: draft. Nothing is built, and this document changes no code. Every
  claim about the code below was checked against main at `b58eef1f9c`, or
  against the open pull request it names, at that PR's head on 2026-11-10.
- Author: kseam
- Prior art: [`@json-render/chat`](https://github.com/vercel-labs/json-render/tree/main/packages/chat)
  from vercel-labs/json-render (Apache-2.0, TypeScript, built on Vercel's Chat
  SDK). It gave us the idea of an agent picking components from a catalog, the
  component list we start from, and the habit of sending validation issues back
  to the model. We borrow the ideas only. Nothing here depends on it.

## Summary

Kiro Crew builds every chat card by hand: Python functions that assemble Slack
Block Kit, Teams Adaptive Card or Discord component dicts, each with its own
length checks, its own redaction call and its own idea of what to do when text
is too long. This RFC proposes one small Python package, `kiro_crew.messaging.cards`:

1. Gateway code (and, later, an agent) describes a card as a typed **spec**:
   `Card`, `Section`, `Text`, `Fields`/`Field`, `Table`, `Context`, `Divider`,
   `Actions` and `Button`.
2. One function, `render(spec, platform, limits=None)`, validates the spec,
   runs the shared redactor and mention defuser on every text field, checks
   every platform cap, and returns the native payload plus a plain-text
   fallback. Over a cap it **refuses with structured issues**. It never cuts
   text silently.
3. Images, video, link previews and free-form URLs are not in the element set.
   A link exists only when one of a few gateway builders made it.

Surfaces move over one per PR, starting with a card that has no model-authored
text, each proven by golden tests that the JSON is unchanged. ("Channel card"
keeps it apart from `change_card_catalog.py`, an unrelated settings-change
card.)

## Motivation

### Current state

On main there are 157 hand-written Block Kit element literals (`"type":
"section"`, `"actions"`, `"context"`, `"header"`, `"divider"`, `"button"`,
`"static_select"`) across 25 modules, and 35 call sites that post or edit
blocks. The card builders this RFC covers:

| Surface | Where it is built |
|---|---|
| Tool approval card (transport) | `slack/renderer.py` `build_approval_blocks`, text from `slack/approval_card.py` `approval_detail_blocks` |
| Tool approval card (native handler) | `slack/handler_runtime/approvals.py` `_build_approval_blocks` |
| OPTIONS card | `slack/format.py` `build_options_blocks`, `build_options_selected_blocks`, `build_options_answered_blocks`, `replace_options_blocks` |
| Stop / working cards | `slack/blocks.py` `build_stopping_blocks`, `build_stopped_blocks`, `build_stop_failed_blocks`, `build_working_blocks` |
| Code-channel wrap-up | `slack/code_channels/wrapup.py` `wrap_up_blocks`, `_retired_blocks` |
| Code-channel context bar | `slack/code_channels/agent_bar.py`, `tools.py` `_bar_items`; bar actions in `actions.py` ([#22370](https://github.com/kirodotdev/KiroCrew/pull/22370)) |
| Agent-authored Block Kit views | `slack/code_channels/tools.py` `_blocks`, `_set_view` |
| Code-channel work board | `slack/code_channels/board/render.py` ([#21993](https://github.com/kirodotdev/KiroCrew/pull/21993)) |
| Code-channel tabs | `slack/code_channels/tabs.py` ([#22928](https://github.com/kirodotdev/KiroCrew/pull/22928)) |
| Agent picker and approval picker | `slack/code_channels/agent_picker.py`, `approval_picker.py` (merged in [#21991](https://github.com/kirodotdev/KiroCrew/pull/21991)) |
| Worker-approval relay | `slack/code_channels/worker_approvals.py` (merged in [#21991](https://github.com/kirodotdev/KiroCrew/pull/21991)) |
| Bridged notifications | `notifications/bridge_slack.py` `action_blocks` |
| Teams | `teams/cards.py` `approval_card`, `options_card`, `session_picker_card`, `resolved_card` |
| Discord | `discord/renderer_components.py` `build_option_components`, `build_model_components`; approval prompt in `discord/renderer.py` `DiscordRenderer.on_prompt_choice` |
| Webex (same Adaptive Card format as Teams) | `webex/cards.py` `approval_card`, `options_card` |

Each surface decides on its own how to redact, how to escape Slack's mrkdwn
and mention grammar, and what to do at a platform limit.

### Problems

The Slack Code pull requests keep hitting four kinds of bug, and the fixes keep
landing one surface at a time.

**1. Text cut mid-sentence at a platform limit.** Slack caps a button or option
label at 75 characters, a confirm dialog's text at 300 and a section at 3,000.
Each builder handles that its own way, and most slice:

- `wrapup.wrap_up_blocks` cuts the wrap-up summary at `MAX_SUMMARY_CHARS`
  (2,800) with no marker, and `wrapup._post_wrap_up` cuts the notification
  fallback at 200.
- `events_slash._handle_agent` cuts agent names to `[:75]`;
  `interactions_context.ack_button` cuts a section at 2,990.
- `row_text` in `board/render.py` (#21993) cuts the board at `[:2900]`.
- `renderer_components.build_option_components` cuts Discord option labels to
  80 characters.
- The #21991 UX review rounds found agent-picker descriptions and Trust
  confirmations cut mid-sentence. The fix was to rewrite the copy to fit
  (`agent_picker._SHIPPED`, "written to fit Slack's cap uncut"), add a
  word-boundary `agent_picker._fit` for other agents, and add a per-surface
  length test
  (`test_every_trust_text_says_how_far_trust_reaches_in_plain_words`, "at most
  300 characters"). The next surface gets none of that for free.

Some paths do better. `slack/format.py` splits a long reply into separate
posts with a continuation marker (`split_message`), and its `_lossless_blocks`
cuts text so that the pieces join back exactly. `slack/approval_card.py` marks
a cut with `_TRUNCATION_MARKER`. Those
behaviours live in their own modules, so a new builder does not inherit them.

**2. Fields dropped when a card is built again.** A card that is built in more
than one place, or rebuilt after a click, loses whatever the second builder
forgets. On #21991 the AI review found that the new worker-approval relay
(`worker_approvals.py`) built its Slack card without the event's trusted MCP
identity line. Owners could approve a worker's MCP call, or put the whole
channel on Trust, without seeing which server ran it. Every other approval
card, including the linked card in the same function, showed that line, and no
test checked it on worker cards. On main, the "rebuild" helpers work on posted
JSON: `wrapup._retired_blocks` filters blocks by type, and
`format.replace_options_blocks` splices blocks into a posted message. A field
that one builder adds is invisible to the others.

**3. Redaction and defusing missed on some paths.** The shared outbound sink is
`messaging/renderer.py` `display_safe_for`, but every surface has to remember
to call it on every field, with the right capabilities:

- #21429 (code-channel agent tools, merged 2026-11-09) went through several
  review rounds on `tools.py`. A credential hidden in HTML character
  references or in an attribute passed the text screen, which gave us
  `_redact_html`'s decode rounds and `_audit_view`. A credential split across
  two display elements passed per-element redaction, which gave us
  `_refuse_hidden_split`. Both checks apply only to agent-authored views. A
  gateway-built card that puts two model-influenced strings side by side does
  not get them.
- A local review round on #22001 (code-channel dispatch) found worker titles
  and tool names reaching Slack without the mention defuse; that fix is not
  pushed yet. `worker_approvals.py` defuses its own title by hand
  (`escape_mrkdwn(text).replace("*", "∗")`).
- `slack/approval_card.py` has its own `_BACKTICK_PAIR_RE`, so tool arguments
  cannot close a code fence early. No other builder has it.

**4. Stop and commit races at the send site.** After Slack's Stop, Kiro Crew
must post nothing more for that turn. #21429 added the commit point
`slack/code_channels/surface.py` `commit_if_not_stopped`: it is called synchronously,
with no await between it and the write. Getting it right took review rounds
("policy at commit points", `349ce1c118` on the PR branch), and
`test_slack_code_channel_commit_point.py` now holds each handler module to it.
Each new card that edits in place, such as the board, tabs or worker
progress, has to wire the fence again.

Each of these was fixed in the PR that found it. The fixes were correct, but
they live in the module where the bug was found, so the next card starts over.

## Goals

- One typed card spec for gateway-built cards, shared by Slack, Teams, Discord
  and a plain-text fallback.
- One render path that redacts and defuses every text field, enforces every
  platform cap and refuses on a breach, returning issues that name the field.
- Rebuilds (resolved, retired, answered) are new specs derived from the old
  spec, never edits of posted JSON.
- No new zero-click fetch path: the element set has no image, video, preview or
  free URL.
- Migration that changes no output: golden payloads prove each moved surface
  renders what it rendered before. The one exception is text that is cut
  silently today, and each phase names that change and tests it.

## Non-goals

- Changing any card's wording, layout or interaction handlers. Action ids and
  values stay as they are, so every click handler keeps working.
- Modals, Home tab views and canvases. They are separate Slack surfaces with
  different limits, and they can adopt the spec later.
- Inputs (text fields, date pickers). A posted card shows state and offers
  buttons. Nothing more.
- Charts. Block Kit has no chart element, so a chart would have to be an
  image, which the security floor refuses.
- Adopting json-render, the Chat SDK or any Node runtime.

## Design

### 1. The spec

Frozen dataclasses in `messaging/cards/spec.py`. The names follow json-render's
standard components so the prior art stays easy to read side by side.

```python
# Every class is @dataclass(frozen=True).
class Text:
    runs: tuple[str | Link, ...]          # plain strings and builder-made links
    style: Literal["plain", "markdown", "code"] = "plain"
    fit: Fit = Fit.REFUSE                 # see "Limits"

class Field:   label: str; value: Text
class Fields:  items: tuple[Field, ...]
class Table:   columns: tuple[str, ...]; rows: tuple[tuple[Text, ...], ...]
class Section: text: Text | None = None; fields: Fields | None = None
class Context: items: tuple[Text, ...]   # small, secondary lines
class Divider: ...
class Confirm: title: str; text: str; confirm: str; deny: str

class Button:
    label: str
    action_id: str                        # routes the click; gateway-owned
    value: str                            # a lookup key, never authority
    style: Literal["default", "primary", "danger"] = "default"
    confirm: Confirm | None = None

class Actions:
    buttons: tuple[Button, ...]
    id: str = ""                          # Slack block_id; Teams/Discord ignore it

Block = Section | Table | Context | Divider | Actions

class Card:
    body: tuple[Block, ...]
    title: str = ""
    fallback: str = ""                    # notification text; derived when empty
```

`markdown` is a small subset (bold, italic, inline code, code block, bullet
list). Each renderer translates it to its own dialect (Slack mrkdwn, Adaptive
Card TextBlock markdown, Discord markdown) and escapes everything else.

A `Button` carries only an action id and a value, the same rule `teams/cards.py`
already states: the payload is "only ever a LOOKUP key into state this process
already holds". A per-prompt nonce stays where it is minted today.

Typed builders sit on top for cards that more than one surface draws.
`approval_card(title, purpose, tool_input, identity: McpIdentity | Unverified,
...)` takes the MCP identity as a **required** argument. A relay or rebuild
that forgets it fails type-checking instead of reaching Slack without it,
which would have caught the #21991 relay bug.

Rebuilding means `dataclasses.replace` on the stored spec. Retiring a wrap-up,
for example, swaps its `Actions` for a `Context` note. The posted JSON is never
filtered.

### 2. Deliberately left out

| Not in the spec | Why |
|---|---|
| `Image`, image accessories, `image_url` / `thumbnail_url` | Slack fetches block media server-side whatever the unfurl flags say. `slack/client.py` `blocks_request_remote_media` refuses such trees on both the send and the edit path (rfc-redaction-explain-and-reveal §5, step 1). |
| Video | Same client refusal (`video` type, `video_url`). |
| Link-unfurl previews, Discord embeds | Unfurls are a zero-click fetch of a URL that may be agent-written. Slack sends `unfurl_links=false, unfurl_media=false` with no opt-in. Every Discord message carries `SUPPRESS_EMBEDS` (`discord/client.py` `_FLAG_SUPPRESS_EMBEDS`), which hides rich embeds too (see open question 3). |
| `LinkButton`, arbitrary URLs | A model-chosen URL is an exfiltration path: `https://evil/?d=<conversation>`. |

The sinks and rows this keeps whole: the client-seam refusal in
`slack/client.py`; the security-posture delivery rows "Slack code-channel
posts" (`slack/code_channels/wrapup.py`), "Teams rendered message text"
(`teams/renderer.py`), "Outbound channel messages — send_message post leg"
(`dashboard/messaging_api/proactive_send.py`) and "Discord / Telegram / WeCom
/ Webex" (`messaging/driver.py`); and the `slack/format.py` and
`slack/blocks.py` rows.

**Links come only from builders** in `messaging/cards/links.py`, and each one
takes a value the gateway already resolved: the workspace's pull-request URL,
a dashboard path joined to the gateway's own base URL, or a channel or message
permalink Slack returned. A builder refuses anything that is not `https://` or
that the payload redactor would change, which is the rule
`tools.py` `_bar_items` applies to context-bar URLs today. `Link` has no public
constructor, and the agent-facing schema (section 4) has no link type, so a
model has no way to name a URL.

### 3. `render(spec, platform, limits=None)`

```python
def render(spec: Card, platform: Platform, limits: Limits | None = None) -> Rendered:
    """Raises CardError(issues) and returns nothing when any check fails."""
```

`Rendered` holds the native payload (`blocks`, an Adaptive Card attachment, or
Discord `content` plus `components`) and `text`, the plain-text fallback. One
pass, in this order:

1. **Structure.** Types, required fields, unknown keys (agent input), counts
   (blocks per message, buttons per row, fields per section).
2. **Redact and defuse.** Every display string goes through `display_safe_for`
   with the platform's `TransportCapabilities` and `redact_via_context`, so the
   mention defuse applies exactly where the platform parses mentions. Link
   destinations get the payload form (no mention defang), as
   `wrapup._redact_payload` does. Slack mrkdwn fields are escaped with
   `escape_mrkdwn`, and code runs get the backtick-pair break from
   `approval_card.py`.
3. **Split secrets.** The shown text, in reading order, goes through
   `card_lifecycle.readings_hide_secret`, the screen `tools.py` runs on agent
   views today. It now runs on every card.
4. **Limits.** Measured **after** redaction, because a redaction placeholder
   can make text longer.
5. **Emit.** Build the native payload, then run the existing client-seam check
   (`blocks_request_remote_media`) as a backstop.

**Limits never truncate silently.** `Limits` is a frozen dataclass per platform
(Slack: 50 blocks, 3,000 section characters, 75 label characters, 2,000
value characters, 300 confirm-text characters; Discord: 2,000 content
characters, 5×5 buttons, 80-character labels; and so on), each value linked to
the platform's own docs. A caller can pass tighter limits, the way json-render
takes `limits: {tables: 1}`. A breach becomes an `Issue`:

```python
@dataclass(frozen=True)
class Issue:
    path: str        # "body[2].buttons[0].label"
    code: str        # "too_long", "too_many", "unknown_key", "link_refused", ...
    limit: int | None
    actual: int | None
    message: str     # one plain sentence a model or a log reader can act on
```

A field can opt into **one declared, visible** fit instead of refusing:

- `Fit.SPLIT`, for section text only: split losslessly across consecutive
  sections. The cut reuses `format._lossless_blocks`, so the sections join
  back to exactly the original text.
- `Fit.MARKED`: cut at a word boundary and append a visible marker (today's
  `_TRUNCATION_MARKER`). Allowed only where the full text is reachable another
  way: a dashboard link on the same card, or the full value carried in the
  button.

Each fit is chosen in code and covered by a test. Nothing is cut because a
builder forgot a cap.

Teams uses Adaptive Cards pinned to 1.4 (`teams/cards.py`), which has no
`Table` element, so `Table` renders as a `ColumnSet`. Webex uses the same
Adaptive Card renderer with its own `Limits`.

### 4. Agent path (optional, see open question 1)

An agent may hand back a card as JSON matching the spec's schema. The schema is
generated from the dataclasses with the platform's limits filled in, the way
json-render's `catalog.prompt()` describes its components. The gateway parses
it into the spec and renders it. On `CardError` the tool answers with the
issues, one line each, and posts nothing:

```text
Error: the card was not posted.
- body[1].fields.items[3].value: too_long (2,140 > 2,000 characters)
- body[4]: unknown_key 'image_url' (cards cannot load images)
```

The model fixes the spec and tries again, as json-render's `CardSpecError`
flow intends. In v1 an agent card is display-only, the rule
`code_channel_set_view` applies to Block Kit views today: no `Actions`, since
a model must not mint action ids that gateway handlers route on. The first
agent entry point would be a `card` view type on `code_channel_set_view`,
beside `block_kit`, which stays as it is.

Gateway-built cards use the same spec and the same `render`, so an agent card
and a gateway card cannot be screened two different ways.

### 5. Streaming and in-place edits

`post_card` and `edit_card` in `messaging/cards/send.py` take a client, a
target, a spec and a `commit` callable. They render first, then call
`commit()` synchronously right before the client call, with no await between.
For code channels `commit` is `surface.commit_if_not_stopped`, so a Stop fences
a card send exactly as it fences any other write, and
`test_slack_code_channel_commit_point.py` gains these two helpers.

A card whose spec grows (the board, worker progress, a long-running status) is
edited in place. Edits are coalesced to the channel's existing interval:
`slack/handler.py` `_EDIT_INTERVAL` (1.0 s), `discord/renderer.py`
`_EDIT_THROTTLE_S` (1.2 s), `teams/renderer.py` `_PROGRESS_THROTTLE_S`
(3.0 s). Each edit renders the whole current spec. A spec that fails
validation mid-stream leaves the last good render in place and logs the
issues. It never posts a partial payload.

This differs from json-render's `streamCard`, which parses a model's token
stream as the card grows. Our agent path gets MCP tool arguments whole, so
token-level streaming buys nothing. Our growing cards are gateway-built.

### 6. Plain-text fallback

`render(spec, Platform.TEXT)` produces the Slack notification `text`, the body for channels without cards
(WhatsApp, iMessage), and the message used when a card send fails. It writes
the title in bold where the channel supports it, sections as paragraphs,
fields as `Label: value` lines, and a table as one line per row. Buttons
appear only when the channel has a typed-reply route for that action id (the
numbered OPTIONS reply that `build_options_blocks` already offers for
overflow). Otherwise the text says where to answer ("Open the dashboard to
approve"). The same redaction pass runs.

## Migration plan

Each phase is one PR and can be abandoned on its own. "Golden" means a JSON
fixture captured from main before the move, asserted equal after it.

**Phase 1: package, Slack and text renderers, Stop/working cards.**
`messaging/cards/` with `spec`, `render`, `limits`, `links`, `send`; the Slack
and text renderers; `slack/blocks.py` `build_stopping_blocks`,
`build_stopped_blocks`, `build_stop_failed_blocks` and `build_working_blocks`
built from specs. These cards hold no model-authored text and at most two
buttons (the resolved Stop cards have none; the working card has Stop, plus
"Link to Dashboard" when `include_dashboard_link` is set). No flag. Exit:
goldens for all four builders, both working-card variants included, pass
unchanged; the
renderer test suite below passes; no caller of those four functions changes.

**Phase 2: code-channel wrap-up.** `wrap_up_blocks` and `_retired_blocks`
become a spec and a `replace`. The summary uses `Fit.SPLIT`. Exit: goldens
for every summary under the old 2,800 cap are unchanged. Over the cap, the
only intended difference is that the summary is split instead of cut, with a
test that fails on main. The Archive confirm keeps its exact text. The
commit-point test covers `post_card` and `edit_card`.

**Phase 3: OPTIONS card on Slack, Teams and Discord.** Adds the Adaptive Card
and Discord renderers. This is the first card drawn on three platforms. Exit:
goldens for `build_options_blocks`, `options_card` and
`build_option_components`; the Discord 80-character cut and the Slack 75-character
`options_button_label` become declared `Fit.MARKED` labels, since a click
already sends the full choice. Blocked on open question 4, because
the Slack card's "Choose several…" overflow menu is not in the v1 element set.

**Phase 4: approval cards.** One `approval_card` builder with a required
identity serves `slack/renderer.py`, `handler_runtime/approvals.py`, the
worker relay, `teams/cards.py` and `DiscordRenderer.on_prompt_choice`. Exit:
goldens per platform, including the trusted and unverified identity lines; a
test that every approval surface's output contains the identity line.

**Phase 5: code-channel board, tabs, bar and pickers.** Only after #21993,
#22928 and #22370 have merged, so this work never conflicts with an open
stack. The pickers need `Select` (open question 4). Exit: goldens per
surface; `board/render.py`'s `[:2900]` becomes `Fit.SPLIT` or `Fit.MARKED` with
a test.

**Phase 6 (optional): agent cards.** The `card` view type on
`code_channel_set_view`, display-only. Exit: the issue-list answer is tested,
and a refused spec posts nothing.

Later, unordered: `notifications/bridge_slack.py`, Ops Mission Control's
`slack_out.py`, `sessions_view.py`, and Webex.

## Backward compatibility

Compatible. Every migrated builder keeps its function signature and returns
the same payload, proven by goldens. Action ids, values and block ids do not
change, so posted cards from before an upgrade still route to the same
handlers. The only output that changes is text that today is cut silently;
each such change is named in its phase with a test. A click on a card posted
before the upgrade brings that card's blocks in Slack's interaction payload,
with no spec behind them; rebuilds of such cards keep today's JSON path until
those cards are no longer answerable.

## Security considerations

- **Posture rows.** Phase 1 adds one row to `_DELIVERY_SINKS` in
  `security_posture_sinks_delivery.py`, "Channel cards"
  (`messaging/cards/render.py`), because the renderer becomes an egress
  redaction site. That file is watched by Sensitive Change Review. Later
  phases edit only the wording of the rows whose redaction call moves into the
  renderer ("Slack code-channel posts", the `slack/blocks.py` and
  `slack/format.py` rows, "Teams rendered message text"). No row is removed,
  and no protection moves out of a watched file. The Teams renderer's
  render-form re-scan stays where it is.
- **No new fetch path.** The element set cannot express media, previews or
  free URLs. The client-seam refusal and `SUPPRESS_EMBEDS` stay unchanged, as
  a second layer.
- **Wider screening.** The split-secret and character-reference screens that
  #21429 built for agent views now run on every card. That can refuse a card
  that posts today. Each phase's goldens show whether any of its fixtures do.
- **Buttons carry no authority.** Values stay lookup keys; nonces stay
  per-prompt; agent cards carry no buttons in v1.
- **Stop fence.** Every card send passes through `commit()` with no await
  before the write.

## Testing

- **Goldens per platform** for every migrated builder (Block Kit JSON,
  Adaptive Card JSON, Discord payloads, plain text), captured on main before
  the move.
- **Redaction property test.** For every renderer and a corpus of fixtures
  (credentials, a credential split across two fields, `<!channel>`,
  `@everyone`, nested character references, backtick runs), every display
  string in the output is a fixed point of `display_safe_for` for that
  platform, and the split-secret screen finds nothing.
- **Limit tests.** For each limit, a field at the cap renders and a field one
  over refuses with the expected `path` and `code`; one case where redaction
  pushes text over the cap.
- **No-media test.** For a generated corpus of specs, no Slack output satisfies
  `blocks_request_remote_media`, and no Discord payload has `embeds`.
- **Commit point.** `post_card` and `edit_card` join
  `test_slack_code_channel_commit_point.py`.

## Alternatives considered

**Adopt json-render through a Node sidecar.** Rejected. The channel layer is
Python, so a sidecar adds a second runtime to install, update, sandbox and
supervise on every host. Its standard catalog includes `Image` and
`LinkButton`, which this repo's security floor refuses, so we would have to
fork the catalog anyway. Chat SDK adapters also degrade content past a
platform cap unless `limits` is passed, and our rule is to refuse.

**Keep hand-built blocks.** This is the status quo. The four bug classes above
recur because each fix lives in the module that found it. More review rounds
on each new surface is the cost we pay today.

**A Slack-only helper.** It would fix Slack's limits and redaction in one
place, but Teams, Discord and Webex would keep their own builders, and the
OPTIONS and approval cards that exist on all of them would still be written
three times.

**Use Block Kit as the shared format and translate it.** Rejected. Block Kit
carries Slack-only ideas (mrkdwn, `block_id`, accessories, 75-character
`plain_text`), so the other renderers would be parsing Slack's quirks back
out. A neutral spec is smaller than Block Kit and easier to validate.

**Clamp silently, as some renderers do.** For example,
[CopilotKit's `renderBlockKit`](https://docs.copilotkit.ai/reference/channels/slack/renderBlockKit)
drops an 11th field or a 26th button without a marker. Rejected: that is bug
class 1.

## Open questions

1. **Agent card tool in v1?** Recommendation: no. Ship gateway-built cards
   first (Phases 1–5), then the display-only `card` view type (Phase 6).
2. **Which surface goes first?** Recommendation: the Stop/working cards in
   `slack/blocks.py`. They have no model text and four small builders, so
   they prove the package and goldens at the lowest risk. The wrap-up card is
   the alternative if we want model-authored text and the Stop fence covered
   from day one.
3. **Discord: embeds or content plus components?** Discord's `SUPPRESS_EMBEDS`
   flag, which every outbound message carries, hides bot-built rich embeds as
   well as link previews. Recommendation: render Discord cards as markdown
   `content` plus button rows, as the Discord renderer does today, and keep
   the flag. Embeds would need a posture decision of their own.
4. **Add `Select` (and an overflow menu) to the element set?** The agent
   picker, approval picker and OPTIONS overflow need them. Recommendation: yes,
   in Phase 3, with the same rule as `Button`: gateway-owned action id, and
   option values that are lookup keys.
5. **One Sensitive Change Review edit in Phase 1.** Is one new row in
   `security_posture_sinks_delivery.py` acceptable, plus row wording updates
   as surfaces move? Without that row the renderer would be an unregistered
   egress site.
6. **Package location.** Recommendation: `kiro_crew/messaging/cards/`, next to
   the channel-neutral `messaging/renderer.py` and `messaging/driver.py`.
