---
title: Proactive Slack Egress Consolidation — one hardened path instead of three tiers
status: draft
revision: v1
author: rnoack, with Kiro
created: 2026-09-05
last-audited: 2026-09-05
audited-at: 6d1b51704
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Proactive Slack Egress Consolidation — one hardened path instead of three tiers

- Status: draft — nothing proposed here has shipped. The three tiers it describes
  all exist on main today; what does not exist is the consolidation.

## The problem

A *proactive* Slack send has no inbound message to answer, so it names its own
recipient and nothing in the request proves that recipient is still authorized. The
repo currently answers that in three different strengths, and the strongest one
guards the least consequential sender.

**Tier 1, the full chain.** `dashboard/slack_egress._deliver_slack_governed`:
recipient authorization against four named authorities, a governance gate, a
synchronous re-assertion adjacent to the send, per-chunk revalidation, a governance
generation re-read, and a SEL row on every outcome. Exactly ONE consumer, the
`/note` channel mirror's Slack leg.

**Tier 2, gate only.** `dashboard/chat_compaction_notice` calls
`channel_egress_permitted` and then posts directly. It gets the governance check and
none of the per-chunk revalidation.

**Tier 3, plain client, live TOCTOU between resolving a recipient and sending.**
Three modules, each marked at its own send site and pinned by a census test:
`handlers/messaging.py`'s `api_send_message` Slack leg (the LLM-facing tool, so the
highest-consequence of these), `server.py`'s owner DM, and `handlers/hooks.py`'s
hook notification DM.

The inversion is the point: a best-effort background note is the protected one, and
the tool an agent drives is not.

## Where the tier map lives, and where it deliberately does not

This document is the ONE prose enumeration of which sender is in which tier. That is
a rule, not an accident: the change this RFC follows argues that a duplicated fact
drifts, so restating the map would reproduce the defect it describes.

Two other surfaces carry the map, and both are executable rather than prose:

1. A census test pins the tier sets by module, so a new direct Slack sender in
   `dashboard` fails until its author classifies it.
2. Each plain-client send site carries a one-line marker naming its own tier, so a
   reader at the site learns its posture without finding this file.

For anyone grepping later, two phrases elsewhere in the tree look like this map and
are a different concern. `docs/system-specs/modules/messaging.md` says "three tiers
decide when a transition reaches the channel", which is the phase reaction ladder
(terminal, immediate, debounced). `docs/app-kit/api-reference.md` says a flag is
"operator-only on the agent side too, three tiers", which is that flag's enforcement.
Neither is about Slack egress hardening, and neither should be folded in here.

## The deferred inventory this RFC owns

**Seven resolve-then-send sites across five symbols.** Each resolves a link, awaits,
then sends on the value captured before that await with no re-walk:
`state._notify_inbound_unbind`, `chat_mirror.api_chat_slot_mirror_link` (three
distinct paths in one function), `slack/gateway._deliver_channel_reply`,
`handlers/messaging._deliver_channel_dm`, and
`chat_compaction_notice._deliver_via_transport`. Named by symbol rather than by line
so this list does not rot as the files move.

**Four direct Slack senders** that bypass `channel_egress_permitted` entirely, being
the three tier-3 modules above plus the compaction notice's own Slack leg.

**A third copy of the gate itself.** `upload_destination._slack_egress_permitted`
carries the same `vet_and_audit("channels", …, fail_closed=True)` body as the shared
`channel_egress_permitted`. It is the one sibling of the extracted mechanism that the
census cannot see, because the census greps for `post_message(` and a file upload
never calls it. Adopting the shared gate there is a behaviour change to the upload
path, so it is listed rather than folded, and it is listed here because a copy
recorded nowhere is exactly how the two copies drift.

**The two per-chunk revalidation chains are now ONE driver, and this item is done.**
`messaging.renderer.send_parts_revalidating` owns the revalidate-then-send ordering
and the abort-on-refusal contract, and both `handlers/messaging.deliver_to_channel`
and `slack_egress._deliver_slack_governed` drive it. That was the trigger the note
module deferred, and keying it on "the second adopter of this chain" was the wrong
key: the count that matters is hand-written per-chunk loops, and that reached two in
this change rather than one.

What stayed with each caller is what they genuinely do not share, measured against
both loops: the revalidation predicate (four-authority basis plus governance
generation, versus a ladder re-walk with a pause skip), the chunker, the audit
vocabulary, the refusal reason strings, the send call, and their FIRST-PART
semantics. The Slack path re-asks on every part including part 1; the channel path
rechecks only from part 2, because part 1 there is already covered by the caller's
own resolve. Both are passed in as callbacks for exactly that reason. Neutralising
the driver's abort arm fails seven existing tests across both callers, so the shared
skeleton is load-bearing on each path rather than a wrapper.

## Why it is not one change with the note mirror

Adopting the chain at any of these sites WIDENS that site's refusal set, which is a
behaviour change to a surface the note feature does not otherwise touch. That is the
reason each was deferred rather than folded in, and it is also the reason this needs
its own review: the sites that matter most are the ones whose new refusals an
operator would notice.

## What consolidation should look like

1. One shared per-chunk revalidation driver, so the two chains cannot drift.
2. Tier 2 and tier 3 adopt it, highest-consequence first, in that order:
   `api_send_message`'s Slack leg, the compaction notice, then the two DMs.
3. The seven resolve-then-send sites re-walk after their await.
4. `upload_destination._slack_egress_permitted` adopts the shared gate.
5. Fix the governance profile store's cold load to BLOCK concurrent first readers
   instead of denying them. This is the item that unlocks concurrent legs: the note
   mirror runs its two legs sequentially only to avoid that fail-closed denial, so a
   wedged leg currently delays a healthy sibling channel by the wedged leg's whole
   budget. Ordering in one caller does not fix the store, and any other pair of
   concurrent governance checks can still hit it.
6. The census test that pins the tier map comes down when the map has one tier, and
   the per-site markers with it.

## What is actually on main today

The full chain, its single consumer, the shared gate and the shared audit emitter.
The tier map is documented at the note module's dispatch seam, marked at each
plain-client send site, and mechanically pinned: a census test fails if a new
dashboard module sends to Slack without being classified, if the plain-client tier
changes size, or if any plain-client site drops its marker. None of the
consolidation above exists.

## Sequencing, and what is not yet owned

The order is the one in the plan above, and it is chosen by consequence rather than
by ease. Step 2's first item, `api_send_message`'s Slack leg, is the whole point: it
is the LLM-facing tool, it is the highest-consequence sender in the inventory, and it
is the one whose live TOCTOU an agent can reach. Steps 1 and 5 are its prerequisites,
because a shared driver has to exist before three callers can adopt it, and
concurrent legs stay impossible while the profile store denies concurrent first
readers. Steps 3, 4 and 6 can land in any order after that.

`tracking-issues` and `implementation-prs` above are EMPTY, and that is a real gap
rather than an oversight: nothing in this repository drives the inventory SHRINKING.
The census test stops it growing, and the markers stop a site being silently
unclassified, but neither makes anyone adopt the chain. Until a tracked item exists
and its id is recorded in the front matter, the baseline pinning
`{server.py, hooks.py, messaging.py}` as unhardened can outlive the memory of it
being temporary. Filing that item is a maintainer action, and this paragraph exists
so the gap is visible in the document rather than only as two empty lists.
