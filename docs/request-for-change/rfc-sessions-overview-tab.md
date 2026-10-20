---
title: Sessions overview tab in the SidePanel add-menu
status: draft
author: pstarkgit
created: 2026-10-18
last-audited: 2026-10-18
audited-at: 96c95887b
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
shipped-note: Implemented beside this document as a draft proposal; the tab described below is live in the SidePanel `+` menu (session-output group), not the workspaces group first sketched. Status vocabulary comes from the fleet command center (`buildCommandCenter`); only slots jump (`/chat?sid=`), runs stop in place.
---

# RFC: Sessions overview tab in the SidePanel add-menu

- Status: draft. Proposed by the author on 2026-10-18; no decision recorded yet.
- Verification: none yet — the implementation PR will carry the proof.

## 1. Problem

Session state is scattered: chat slots live in the sidebar, subagent runs in
the Subagents tab, member work on the Crew Board. There is no one place that
answers "what is running, who drives it, what model/template it uses, and is
it stuck". The existing `/sessions` route (`website/src/pages/SessionsPage.tsx`)
lists slot titles only — no status, no model, no owner, no runs.

## 2. Decision

Add a `Sessions` tab to the SidePanel `+` menu (`website/src/pages/chat/SidePanel.tsx`,
session-output group), rendering a table with one row per live
unit of work:

| Column | Source (no new backend in v1) |
|---|---|
| Session | live slot title/key; run task (or id) for subagent runs |
| Drive | owning crew member: `created_by` joined to `member-<slug>` bindings; blank for the operator's own tabs |
| Status | the fleet command center's `RunNode.state` folded onto three chips: Active (running, idle, or queued), Blocked (needs input), Complete (done or stopped) |
| Model | live slot `model`/`served_model`; run entry's model where present |
| Template | live slot `agent`/`effective_agent`; run `agent` (template/crew name) |

Row actions: jump-to for slots (`/chat?sid=<key>`); slot close through the
sidebar's `deleteSlot` (live slot goes away, transcript stays in history);
run stop through `spawnDelete` with its own confirmation. Data comes from the
redux live-slots mirror and the subagent SSE mirror plus their existing
REST reads — the same sources the sidebar and the Subagents tab already use,
so v1 adds no endpoint, no auth surface, and no migration.

## 3. Non-goals

- Archived/history transcripts (`GET /api/sessions` rows carry no status or
  model; joining them is a later change, not this one).
- New backend endpoints, new WS frames, or any change to slot/run persistence.
- Bulk actions, filtering beyond a status chip, or editing bindings.

## 4. Backwards compatibility

Additive only: one optional menu entry, one tab id. No default changes, no
removed capability, no route changes. Existing tabs, badges, and the
`/sessions` page are untouched.

## 5. Security considerations

The tab renders the same redacted projections the sidebar already shows
(titles redacted server-side, no transcript bodies). Close/archive reuses the
existing delete endpoints with their authorization. No new data reaches the
client.

## 6. Alternatives considered

- Extend `/sessions` `SessionsPage.tsx` instead: rejected — that page is a
  bookmarkable slot list deliberately outside the rail; the request is for the
  working surface next to Subagents, where the operator already watches runs.
- A composed `GET /api/sessions/overview` endpoint: rejected for v1 — every
  field already streams to the client; a new endpoint adds an auth surface and
  a join to maintain for no new data.

## 7. Open questions

- Should member DM threads (excluded from chat surfaces by `mode`) appear as
  rows, or stay reachable via `created_by` on the rows they spawned? v1
  proposes the latter.
