---
title: Redaction that explains itself
status: partial
author: rayrayxu
created: 2026-09-21
last-audited: 2026-09-25
audited-at: 8d47ac90d
doc-pr:
implementation-prs: [13685]
tracking-issues: [13741]
supersedes: []
superseded-by: []
---

# RFC: redaction that explains itself

Explain why text was removed, where the original usually remains, and how to
view it. Keep suspicious links visible, but prevent them from opening
automatically.

Related: [#8426](https://github.com/kirodotdev/KiroCrew/issues/8426),
[#3056](https://github.com/kirodotdev/KiroCrew/issues/3056),
[#674](https://github.com/kirodotdev/KiroCrew/issues/674).
An interactive prototype of every UI surface this document proposes is at
[assets/redaction-explain-reveal-prototype.html](assets/redaction-explain-reveal-prototype.html)
(a self-contained page; open it in a browser).

## Problem

When an agent's reply contains a secret (a private value such as an access
key) or a link that may send out conversation data, redaction (replacing risky
text with a placeholder before the message is saved) changes what appears in
chat. The user sees `[REDACTED: credential]`,
`[REDACTED: suspicious URL to wiki.corp.example]`, and this notice:

> Security notice: 4 credentials in this message were replaced with a
> redaction placeholder before it reached this page. Any command shown above
> will not work if you paste it as-is; supply the secret yourself on the
> machine where you run it.

- **It does not explain why.** The message says what was removed, but not why
  the system removed it, so users may think the product failed or look for a
  setting that does not exist.
- **It does not explain the next step.** The original value is no longer in
  the saved conversation, but the message does not say where it may still
  exist or how the user can view it.
- **The placeholder does nothing.** Users cannot select it to learn what
  happened, where the value came from, or what to do next.
- **The whole link disappears.** The system removes a link when its query (the
  part after the `?`) is over 200 characters. It can also mistake normal
  Chinese titles for suspicious text, because seven Chinese characters can
  become 20 or more consecutive `%XX` groups when a URL encodes them.
- **It looks too serious.** Red warnings appear before the answer, even though
  this protection is working normally, so the explanation should look calm and
  follow the answer instead.

Users repeatedly ask in Slack #kiro-crew-debug why text or links disappeared,
which shows that the current message is confusing even when the protection
worked correctly.

## Three facts to accept before changing anything

1. **The value is not in the saved conversation.** The system replaces it
   before saving any message that was not written by the user
   (`chat_persistence._build_message_entry_uncached`). This means no saved
   copy of the conversation ever contains the value, so even the session owner
   cannot recover it from the conversation. The replacement also keeps the
   value out of Slack, artifacts, memory, sub-agents, and backups — although
   the source file, tool logs, model provider, or screenshots may still
   contain it. There is no setting to turn this off. (The one owner switch
   that exists, described under *What does not change*, does not reach the
   saved conversation: it applies only to files the owner opens in their own
   dashboard.)
2. **The value usually remains at its source.** Removing it from the saved
   conversation does not usually remove it from the file, command output, or
   environment variable where it came from. Because the user can already read
   that source, the UI should explain where it is and how to view it. The UI
   must also say when the source is no longer available, such as when
   temporary output has disappeared or a token has expired.
3. **The built-in Terminal does not remove live output.** Live command output
   goes straight to the browser, so users can view the source without leaving
   the dashboard. Redaction applies only when a user inserts selected Terminal
   text into chat (`api_terminal_redact`); the live output otherwise remains
   visible only in the Terminal view.

## What we actually defend against

The design must handle three different ways data can leave the conversation,
because each one requires a different protection:

| Risk | How data leaves | What stops it |
|---|---|---|
| A secret appears in a saved or shared place (Slack channel, artifact, log) | The secret itself is included in content that other people or systems can read. | Replace the secret before saving, so no saved conversation copy contains it. Never weaken this rule. |
| Harmful instructions make an agent put conversation data in a link | The agent creates a URL such as `https://evil/?d=<your conversation>`. Opening it, or Slack fetching a link preview automatically (an unfurl), sends the data to that site. | Show the URL as blocked text that is unclickable and impossible to preview. The URL does not need to be deleted. |
| A request happens without anyone clicking (a zero-click request): markdown images, Slack previews, widget frames | The code that displays the message requests the URL automatically for the user. | Never fetch an agent-written URL automatically — hiding the URL without stopping the request adds no protection. |

Deleting a URL from the visible chat does not by itself stop data from
leaving, because an agent may also use network access, file tools, or a shell.
The real protection comes from limits on network access and from display code
that never requests agent-written URLs automatically. The UI can therefore
show a blocked link with an explanation and require confirmation before
opening it, while secrets must still be replaced before saving.

**Known residual zero-click path, accepted for now.** The dashboard's own
link-preview feature (`link_previews`; the frontend calls `GET
/api/link-meta` for anchors in a message) fetches metadata for agent-written
hrefs when the user has turned that preview setting on. It is opt-in,
off by default, and the fetch goes through the server-side `link_unfurl`
vetting module rather than the browser — but it is still a request nobody
clicked for. Step 1 deliberately leaves it: folding it into the
click-to-load posture is a decision for the blocked-link work in rollout
step 3, where link rendering is redesigned anyway.

**Second known residual: a redirect after approval.** The chip binds the
request the renderer *initiates* — it names the host the URL points at, and one
click fetches exactly that URL. It cannot bind where a server sends the browser
next: an approved `images.example/chart.png` may answer `302
attacker.example/ping`, and the browser follows it to a host the chip never
showed. Everything knowable before the fetch is now derived from one collector
(§4), so this is not another gap in that disclosure — it is a fact that does not
exist until the network answers, and no client-side check can see it.

Two things follow. What step 1 *can* close, it does: an approved remote IMAGE
carries `referrerPolicy="no-referrer"`, so a redirect target does not also learn
which page the image sat in. That attribute exists only on `a`/`area`/`img`/
`iframe`/`link`/`script`, so an approved `video`/`audio` has no per-element
equivalent — its fetch follows the document policy — and the residual is
correspondingly wider for media than for images. Binding the final host needs
the fetch under our own control, and both ways of getting that are step-3
decisions rather than step-1 mechanics:

- **A same-origin relay** (`GET /api/remote-media?url=…` with redirects
  refused) would bind it exactly, and would incidentally stop the remote host
  from seeing the user's IP at all. But it also makes the gateway issue outbound
  requests to agent-written URLs on the user's behalf — pointed at the user's own
  network, which is the SSRF shape of the very class this document is about.
  Shipping that unproven, beside the renderer rules, would trade a disclosure gap
  for a worse one.
- **`fetch(url, {redirect: 'error'})` rendered from a blob** keeps everything in
  the browser and needs no new route, but requires CORS on the remote host, which
  ordinary image hosts do not send — so it would break legitimate images rather
  than gate them.

Until one of those is decided, the honest statement is the one the chip makes:
it discloses the host being contacted, not a promise about every host in the
redirect chain.

**Third known residual: Slack's edit path carries no unfurl parameter.**
`chat.postMessage` takes `unfurl_links`/`unfurl_media` and step 1 always
sends them false. `chat.update` and `chat.postEphemeral` take neither — an
unknown argument earns `invalid_arg_name` — so on those two calls there is no
flag to set, and adding one would break every send rather than protect it. That
is not a corner: the Slack renderer delivers streamed agent text by
edit-throttled `update_message`, so a URL that first appears in a later chunk is
edited into a message that was posted without it. Whether Slack's fetchers
unfurl a link introduced by an edit is a fact about their servers, so this
change verifies it instead of asserting it — the edit path and the ephemeral
path are both on the live-check list in #12699, and a mitigation lands only if
an unfurl actually appears. The mitigations available at this layer all cost
something real (re-posting instead of editing turns one message into several and
re-notifies the channel; withholding a URL until the stream ends changes what
the reader sees mid-answer), which is exactly why none of them should be
pre-emptively shipped against a behaviour nobody has observed yet. What the edit
path DOES enforce today is the Block Kit media refusal, because that boundary
belongs to this code rather than to Slack's fetchers.

## The fix

### 0 · Task first, protection second

Redaction information must appear after the agent's answer. Show small neutral
markers inside the answer, then any summary strip or coach card. The markers
replace today's grey notice line — once they ship, the notice is removed,
because it only repeated what the markers already say. Protection that worked
normally uses the info color, never danger red; red is reserved for a failure.
Use a lock for a removed value, a shield for a summary, and a broken-link icon
for a link that cannot open.

### 1 · The placeholder becomes a clickable tag with one shared card

Replace `[REDACTED: credential]` with a small, neutral lock tag that users can
select. The tag opens one shared type of card, which leads with three facts:

- **Not saved** — the system removed the value before saving, so this page
  cannot show it and no setting can change that.
- **Source** — the card names the file and section, or the recorded command,
  where the value came from.
- **Recommended action** — *Open in Terminal* adds the exact command without
  running it, so the user can review it first and then view the unredacted
  live output. For a token that will expire soon, *Use the profile instead*
  avoids showing the token at all.

Keep less common information closed until the user asks for it. *Technical
details* explains which rule matched and exactly what the system protected.
*More ways* offers *Copy path* and a pre-filled chat request that saves the
value in a mode-0600 file, which only the owner's account can read, and
the owner can then open that file in the dashboard with the file-view switch
off (#13685); because this creates another secret copy on disk, the card must
say so. A
false-positive report sends only the rule name and session id, and the card
clearly says when the source is gone. Each placeholder stores the rule and
source for that specific removed value.

**Where the source comes from.** Redaction runs before the message is saved,
while the original value and the tool results from that turn are still in
memory. The system searches those results for the value and saves only a
description of its source, never the value itself. If it finds the value in a
file, it records the path and nearby section; if it finds the value in command
output, it records the exact command that already ran, which the user can run
again in the built-in Terminal to see the live output. Some known rules
provide a fixed command instead; for example, an AWS secret-key match
(`bare_aws_secret`) in `~/.aws/credentials [dev]` uses
`aws configure get aws_secret_access_key --profile dev`. A fixed command can
use only non-secret details such as a profile name. If no source matches, the
card says that the source was not recorded. Every command added to Terminal
must be either a command that already ran or a fixed template — never free
text written by the agent, because pre-filled text sits one Enter away from
execution and a prompt-injected agent could pre-fill an attack disguised as a
"view command".

### 2 · A one-time coach card on the first redaction in a session

Show this guidance once, after the first answer that contains a removed value.
It explains that the values were replaced before saving, so no saved
conversation copy contains them, the conversation cannot show them, and no
setting changes this for the conversation. It also explains that each lock tag names the likely
source and tells the user how to view it. End with: "Keep secrets at their
source. Your messages are saved exactly as typed." Keep extra details under
*Learn more*; after the user selects *Got it*, show only the small lock tag.

### 3 · Suspicious links: blocked, but visible

Keep a suspicious URL visible as a **Link blocked** chip, but never make it
an anchor. Show the site name and path immediately, and the length of the
query. *See link* opens a details card that explains why the link was blocked
and shows the full URL as text, with *Open once* and *Copy link* beside it.
That card is the confirmation: nothing opens until the reader has the whole
destination in front of them and clicks *Open once*, which opens it in a new
tab with no referrer. Nothing is remembered, so the link is blocked again on
the next render. When one message blocks several different links on the same
site, the card lists every one with its own address and actions rather than
guessing which placeholder is which. *Allow long
queries for this host in this workspace* creates a rule for only that
workspace. The dashboard writes this rule; an agent can never write it. The
rule relaxes only the checks for long queries and base64 text (a common
encoding that turns data into letters and numbers); all credential checks
remain active. Users can undo the choice immediately or revoke it later in
Settings. Never describe the link as safe. On shared pages, hide only the
suspicious query value and do not provide an Open button.

**This chip cannot be built from what is saved today.** The whole link is
thrown away before the message is written: the saved text keeps the site name
and nothing else (`[REDACTED: suspicious URL to example.com]`). The path and
the query never reach disk. So the site name is the only thing a chip can show
from the saved text — the path and the length of the query that the design
shows are facts this step destroys. Showing them means keeping them, which is
a security decision and not a rendering one, so it is recorded here rather than
settled in the UI work.

What keeping them does *not* cost: the link does not come back to the model.
The replay the model receives is built from the role and the message text only,
and that text is put through the same two removers again before it is injected,
so a detail kept beside a message — its *meta* — is never part of what the
model reads. What it does cost: a link the system judged suspicious then sits
in the saved conversation instead of being destroyed, and anyone who can read
that conversation can read the link.

What is shown on the chip itself, before anyone asks, follows a stricter rule:
**the chip shows only what the removers themselves would pass.** It would be
wrong to assume the query is the dangerous part and the path is safe — a link
can carry smuggled data in its path just as easily — so the path appears on
the chip only when it passes the same checks that blocked the link; otherwise
the chip shows the site name and the length of the query. The full address is
shown only inside the details card, after the reader asks for it. There is one
precedent for keeping a URL in meta: the consent link for a connected server
(`oauth_url`) is preserved only when it passes two checks. Anything kept is
still text an agent wrote, so the chip escapes it and never makes it an
anchor.

**Decision: keep the whole link, so a wrongly blocked link can be opened.**
Most blocked links are false positives — a wiki page with a Chinese title, a
review-filter URL with a long query — and the reader's actual need is to open
them. A chip that only explains why a link is gone does not meet that need, so
keeping structure alone (the site, the path, the query's length) is not
enough. Step 3 keeps the full URL in the message meta and offers *Open once*
and *Copy link*.

This is safe to do because of what the gate actually defends against. The
harm is a request nobody chose: an agent writes
`https://evil/?d=<your conversation>` and a renderer or a preview fetcher
requests it. A human who reads the full URL in the details card and then
clicks *Open once* has made that choice with the destination in front of
them, which is exactly what the confirmation exists to establish. Keeping the
URL in meta does not return it to the model (see above), and the chip still
never makes it an anchor.

Four limits keep this from becoming a way around the gate:

- **A link carrying a credential is not kept.** When the credential remover
  would change the URL as written or in any bounded percent-decoded form, or
  the URL carries userinfo, it is not retained and the chip offers no *Open* —
  opening it would send the secret, and that is the case the gate exists to
  stop. The card says why the address was not kept.
- **The URL is checked again on every serve.** A retained address is re-run
  through the credential remover each time the message is sent to the
  browser, rather than trusted from the saved line, so a rule that changed
  since the message was written applies before anyone can open it; the click
  itself re-checks that the address is http(s) on exactly the host the chip
  names.
- **Only the dashboard gets the address.** Channel posts, shared cards and the
  model's replay carry the placeholder text only; the address lives in the
  message meta the dashboard reads.
- **Everything retained is bounded.** The record count per message and every
  retained string have named limits applied where the record is stored — an
  address longer than its limit is withheld rather than truncated, because a
  truncated address opens a different page — and a limit restricts what is
  described, never what is removed from the text.

### 4 · Every action answers back

Every action must tell the user what happened: *Copy* shows "Copied"; *Open in
Terminal* says the command was added but not run, with a *Clear* action beside
it; *Pre-fill request* reminds the user to review the text before sending;
reports say exactly what information was sent; *Allow* explains where the new
rule applies before offering *Undo*. *Open once* opens nothing until it is
clicked. On
phones, cards open from the bottom of the screen with Close always visible;
tapping outside the card or pressing Esc closes it, then restores the user's
previous focus and scroll position. Every control must provide a touch target
of at least 44×44 points.

### 5 · Three renderer rules, independent of any UI

1. Do not load an image from agent-written markdown automatically; wait until
   the user selects it.
2. The browser policy for an embedded widget frame (a Content Security
   Policy) blocks images and frames from other sites.
3. Slack bot posts must never fetch link or media previews automatically
   (`unfurl_links=false`, `unfurl_media=false`).

These three rules provide the real protection, by stopping URL requests that
would otherwise happen without a click — so they must be in place before the
UI changes that explain blocked links.

**Approval is deliberately per-impression.** Clicking a chip loads that one
image in that one rendered message; a reload shows the chip again. This is a
design decision, not an oversight: a persisted approval is an allow-list, and
an allow-list for agent-written URLs is Settings-grade security state that
must be dashboard-written, human-revocable and auditable — exactly the
machinery rollout step 3 builds for blocked links. Until that exists,
re-clicking is the honest cost; a durable per-host allow rule is step 3's
job, not a side effect of viewing an image.

### 6 · Two false-positive sources fixed on the way

1. Decode percent-encoded text (each encoded character appears as one or more
   `%XX` groups) before deciding whether it is suspicious. Normal CJK text and
   emoji should remain readable instead of being blocked; today, seven Chinese
   characters are enough to trigger this rule, making it the largest known
   false positive for Chinese-speaking users.
2. In a company-internal deployment, exempt that deployment's own well-known
   exact hosts (its wiki, code-review and docs domains — the examples in this
   document use `wiki.corp.example`, `code.corp.example`,
   `docs.corp.example`) from only the long-query and
   base64 checks (`exempt_exact_hosts`). Credential checks still apply, and
   the existing extension point means this change requires no upstream code
   change.

## UI prototype

The interactive prototype at
[assets/redaction-explain-reveal-prototype.html](assets/redaction-explain-reveal-prototype.html)
renders one conversation in the current chat layout, with a switch between
three variants:

- **A — inline chip.** Each removed value is a neutral lock tag inside the
  code; selecting it opens the details card.
- **B — shield strip + drawer.** One summary line after the answer groups
  several removed values; a drawer lists each with its source and actions.
- **C — first-run coach.** A one-time card teaches the three facts, then later
  replies show only the lock tags.

Recommendation: **C → A → B.** Start with C once, after the first answer that
contains removed values, so a new user learns what happened. After that, use A
when one value was removed, and B when one answer contains several — keeping a
small marker at each exact location. Every version places a marker inside
copied code blocks, and copying should turn each marker into `<REDACTED>`. Use
the same blocked-link chip in every version. Keep the existing placeholder tag
and store that value's rule and source in its metadata. Only the session owner
sees actions; Slack and other shared pages show explanatory text without
actions. On phones and desktop alike, a card opens inline, below the block it
explains.

## What does not change

- **Never paste secrets into chat.** Messages written by the user are saved
  exactly as typed and remain with the conversation, so the coach ends with
  this warning.
- Secrets found in agent replies are never saved in the conversation, which
  means there cannot be a "show original" button.
- Known credential formats such as `AKIA`, PEM, `xoxb-`, and `ghp_` remain
  hidden everywhere the agent's output reaches another person or service —
  chat, every messaging channel, artifacts, logs, backups — including inside
  links. Remove only the credential from a URL rather than deleting the whole
  URL, and never let the "Allow" list weaken credential rules.
- **One exception, decided with [#13685](https://github.com/kirodotdev/KiroCrew/pull/13685):
  the dashboard owner can turn the credential pass off for the files their own
  dashboard opens for them** (the chat side panel's Files tab, and the Library
  and Artifacts previews that read through the same route). This is the
  completion of the *save it to a mode-0600 file* remedy above: without it the
  Files view ran the same pass over the saved file and hid the value again.
  Its fences are the reason it is allowed to exist. It is **on by default**;
  only the owner can turn it off (a non-owner or an app token is refused); it
  lives on the keystone floor beside `file_delivery_consent.json`, which the
  agent's file tools refuse and its sandbox mounts read-only, so a
  prompt-injected agent cannot flip it through the filesystem (the owner gate
  is an identity check, so an agent driving the owner's already-authenticated
  browser through computer use could reach the PUT; what that buys is bounded
  to the owner's own file views, and the flip leaves an audit record); every
  change is written to the security event log *before* the switch moves, and
  a *disable* that cannot be audited does not happen — turning redaction back
  on is the fail-safe direction and proceeds even when the log is unavailable;
  it reaches the credential pass only — the
  exfiltration-URL pass, every request-blocking gate, the chat and every
  channel keep the unconditional pass — and the two file handlers that feed
  the viewer take one verdict per request, so neither side of a diff is raw
  while the other is masked. When the owner turns it back on, every open
  dashboard document drops the file bodies it holds — cached reads, diffs and
  clean open tabs; a tab with unsaved edits keeps the owner's own typing — and
  re-reads them under the pass now in force. Settings → Security shows the
  state with a warning
  and the time it was switched off. An in-viewer indicator and an auto-revert
  window were considered and are open decisions
  ([#13741](https://github.com/kirodotdev/KiroCrew/issues/13741) tracks the
  exfiltration-URL carve-out for the same owner view on a companionless
  build).
- Logs, audits, and backups continue to use full redaction, and they do not
  receive any tools for viewing the source.

## Rollout

1. Add the three display rules first, because they stop web requests that
   would otherwise happen without a click.
2. Store each match's rule name and source with its placeholder, then add the
   neutral lock tag, details card, action confirmations, and one-time coach
   after the answer. Remove the old grey notice line — the markers replace it.
3. Replace deleted URLs with a Link blocked chip and a *See link* control.
   Keep the full URL in meta (except a URL carrying a credential) and offer
   *Open once* and *Copy link* in the details card. A per-workspace Allow rule
   with confirmation and Undo follows later; only the dashboard can write that
   list, and Settings → Security → Redaction shows each entry and lets the user
   revoke it.
4. Release the phone layout with the main card, including touch targets that
   are at least 44×44 points.
5. Decode percent-encoded text before checking it, then extend the internal
   list of exact hosts that can bypass only the long-query and base64 checks.
6. Add a Redaction section under Settings → Security that shows groups of
   rules, recent match counts, and allowed hosts, and states which surfaces
   redaction cannot be turned off for (everything the agent's output reaches
   another person or service through). The one control it carries is the
   owner's credential-redaction switch for their own file views, shipped in
   [#13685](https://github.com/kirodotdev/KiroCrew/pull/13685) as *Credential
   redaction in file views* with the fences listed under *What does not
   change*; the rules and hosts view remains read-only.

Each step can be released or rolled back without requiring the later steps.
Steps 1 and 3 ship together in one implementation PR: the renderer rules
stop the request nobody chose, and the chip gives back the link the reader
did choose, so either half alone leaves users with fewer working links than
they have today.

### Step 1 design

What step 1 ships, and the invariants review should hold it to (nothing
here is on main yet except the widget-frame CSP):

- **Widget frame CSP** was already on main: `buildSrcdoc`
  (`website/src/lib/widgetSrcdoc.ts`, `cspFor`) sets `default-src 'none'` with
  `img-src data: blob:` and no `frame-src`, so a widget cannot load an image
  or frame from another site.
- **Deferred remote media** is UNCONDITIONAL on every
  `MarkdownRenderer` surface: agent-authored markdown is the common case, so
  the gate is inherited by construction, and there is no caller prop or
  context that turns it off, so no renderer can forget
  OR disable it — user-message rows defer too, because channel-relayed
  messages share that path and carry attacker-influenceable markdown. A
  remote http(s) image renders as a click-to-load chip whose label always
  shows every destination host (model-authored alt text renders as a quoted
  subordinate line and cannot conceal them), the approval click never falls
  through to a link wrapping the image, an approval never survives a URL
  swap at the same render position, and raw-HTML `video`/`audio`/`source`
  elements — whose `poster`/`src` the browser would fetch on mount — are
  gated by the same chip. Local `/api/file-raw` images are unaffected.
- **Slack unfurls are always off**: `RealSlackClient.post_message`
  and `post_blocks` send `unfurl_links=false, unfurl_media=false`
  unconditionally, and the interface deliberately carries no opt-in
  parameter — every caller is reachable from agent-authored content, so a
  flag would hand a prompt-injected agent the one bit it needs to re-enable
  the fetch. The `send_message` endpoint refuses an explicit
  `unfurl_*=true` (HTTP 400, code `unfurl_disabled`) rather than silently
  dropping it; `false`/absent stay accepted. A Block Kit tree carrying
  `image`/`video` blocks or `image_url`/`thumbnail_url`/`video_url` fields
  anywhere in it is refused, because Slack fetches that media server-side
  regardless of the unfurl flags. The refusal lives at the CLIENT seam
  (`slack/client.py`, on both the send and the edit path), not only at the
  agent-facing entry: the gateway has roughly twenty direct `post_blocks`
  callers, and a check one layer above them is a check each of them can
  forget. The agent-facing `send_message` / `update_message` routes keep their
  own HTTP 400 with the machine-readable `blocks_remote_media_disabled` code,
  and ask that one predicate rather than restating the rule.
- **Sibling channels are closed in the same step, not left as point-patch
  gaps**: every outbound Discord message carries the `SUPPRESS_EMBEDS` flag
  (the client constructs no intentional rich embeds), and every Telegram
  `sendMessage` passes `link_preview_options={"is_disabled": true}` — the
  same server-side zero-click preview class as Slack unfurls, closed with
  the same no-opt-in posture. The seven remaining channel clients were
  audited and none has a bot-triggerable server-side preview to close:
  WhatsApp sends plain `conversation` text (previews require an
  `ExtendedTextMessage` the client never builds), Teams Bot Framework text
  activities do not auto-unfurl (link unfurling is a separate app-manifest
  registration), Webex renders markdown images client-side per recipient,
  Feishu/WeCom/Weixin send text or bot-constructed cards with no remote
  fetch of message-embedded URLs, and iMessage previews are generated
  locally by Messages.app with no send-API knob. No code is added where no
  zero-click fetch exists.
- **One collector feeds the media gate** (invariant recorded so a future
  regression is caught at review time): the renderer derives the deferral
  decision, the approval signature, and the visible host list from the
  same `collectRemotes()` output — the label a user reads and the URL set
  one click unlocks cannot diverge. Everything needed to approve (the host
  set, the consequence sentence, the model-authored description) renders in
  the button's visible flow; the `title` tooltip is a strict duplicate of
  the URL list and never load-bearing, because touch and keyboard users
  have no hover path.

## How we know it worked

- For two weeks, count false-positive reports by rule name without collecting
  the removed value, then use those counts to find rules that need changes.
- Check whether #kiro-crew-debug receives fewer questions about missing links
  and missing secrets.
- Measure how quickly users dismiss the coach with *Got it* — very fast
  dismissal may mean the card is too long to read.

## Open questions

- When users copy text, should every marker become the same fixed token, or
  should the copied text keep the visible marker?
- On shared pages, should we hide the entire suspicious query value? The
  preferred answer is yes; the other option hides only the part that matched
  the rule.
- If a host appears in both places, should the workspace Allow list or the
  built-in exact-host exemption take priority (`exempt_exact_hosts`)?
- **One redaction surface, not two.**
  [#10965](https://github.com/kirodotdev/KiroCrew/pull/10965) scrubs every
  persisted row and marks a rewritten one in the transcript. That marker and
  step 2's lock tag describe the same event to the same reader, so they should
  be one component with one vocabulary; which change carries it is open.
- **Which false-positive fixes this document absorbs.**
  [#9811](https://github.com/kirodotdev/KiroCrew/pull/9811) (shared document
  links), [#10192](https://github.com/kirodotdev/KiroCrew/pull/10192) (pako
  diagram links) and [#9937](https://github.com/kirodotdev/KiroCrew/pull/9937)
  (inline media URIs) each stop one class of wrongly removed link, which is
  §6's goal. They can land on their own; this document only asks that each
  one's rule id reach the chip's reason text so a reader can tell them apart.
- **Media delivery for MCP apps.**
  [RFC #10038](https://github.com/kirodotdev/KiroCrew/pull/10038) settles
  media inside MCP app frames as a trust question. Step 1's click-to-load
  chip covers agent-authored markdown only; the two should agree on whether an
  app frame counts as agent-authored.
- **Merge order with the redaction-pass refactor.**
  [#11501](https://github.com/kirodotdev/KiroCrew/pull/11501) composes the
  two removers through one helper, and step 3 records blocked links at the
  same call in `_flush_segment`. Whichever lands second takes the other's
  shape.

## A live example

While the prototype was being written, the redaction check saw an AWS key
label followed by an equals sign and an HTML tag. The chat said "4 credentials
in this message were replaced", even though the value after the equals sign
was `<span>`, not a secret. Because the label alone caused this incorrect
match, *Report a false positive* must always be available.
