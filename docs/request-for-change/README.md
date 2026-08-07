# Request for Change

Design documents for changes that are large enough, risky enough, or contested
enough to be worth writing down before building. An RFC here is a **proposal and
a record of a decision** — not a description of what the code does today. For
that, read the code, or the `status` field described below.

## Index

Last audited **2026-08-03** against main `0ab6ed48`. Every status below was
verified against the code (definitions *and* callers) and against merged/open PR
history, not taken from the document's own claims. The `rfc-tailnet-dashboard-access`
row was added later and re-verified against `429cbad8`; the other twelve rows have
not been re-audited since 2026-08-03.

| Document | Status | What is actually on main |
|---|---|---|
| [rfc-orchestrator-chat-sessions.md](rfc-orchestrator-chat-sessions.md) | `in-progress` | Nothing yet — all of it is in open PR [#1295](https://github.com/kirodotdev/KiroCrew/pull/1295) (`feat/crew-mode`) |
| [rfc-channel-plugin-architecture.md](rfc-channel-plugin-architecture.md) | `partial` | Shared turn pipeline shipped; **4 of 7** channels adopted. Registry/seam collapse, telegram+discord, Feishu unstarted |
| [rfc-local-notification-bus.md](rfc-local-notification-bus.md) | `partial` | Phases 1/3/4 complete. Phase 2 wired but has no producer; Phase 5 shipped 2 of 3 |
| [rfc-federated-app-platform.md](rfc-federated-app-platform.md) | `partial` | Phase 1 substantially shipped, Phase 3 half-built. Phase 2, Phase 1's removals, Phase 4, Phase 5 unstarted |
| [rfc-workspace-config-evolution.md](rfc-workspace-config-evolution.md) | `partial` | Phases 1–2 shipped. Phase 3's vector isolation was **reversed** on purpose; Phase 4 unstarted |
| [rfc-resumable-subagent-sessions.md](rfc-resumable-subagent-sessions.md) | `partial` | Phase 0 ran and **redirected the design**: continuable conversations shipped instead of the record-store ladder |
| [rfc-i18n-measurement.md](rfc-i18n-measurement.md) | `partial` | Overflow gate shipped, `localeCompare` migration partial. All three *measurement* proposals unstarted |
| [rfc-appstore-official-registry.md](rfc-appstore-official-registry.md) | `accepted` | Nothing in this repo. Rollout R1 merged in the sibling `KiroCrewApps` repo |
| [rfc-notification-bridge.md](rfc-notification-bridge.md) | `accepted` | Nothing — zero implementation code |
| [rfc-tips-kit.md](rfc-tips-kit.md) | `draft` | Nothing. T1 was built and **retracted** ([#775](https://github.com/kirodotdev/KiroCrew/pull/775)); the design section needs revising first |
| [rfc-update-architecture.md](rfc-update-architecture.md) | `draft` | Nothing — zero of three phases |
| [rfc-app-sandbox-isolation.md](rfc-app-sandbox-isolation.md) | `draft` | Nothing. Apps still run in-process with full privileges (see `src/kiro_crew/docs/app-platform-trust-model.md`); no isolation code exists |
| [rfc-tailnet-dashboard-access.md](rfc-tailnet-dashboard-access.md) | `partial` | Phase 1 landed ([#1761](https://github.com/kirodotdev/KiroCrew/pull/1761), `f8afcff7`) — reports the pin's real scope, does not fix it. Phases 2–4 unstarted; the pin repair is tracked as [#1762](https://github.com/kirodotdev/KiroCrew/issues/1762) |
| [version-compliance-framework.md](version-compliance-framework.md) | `draft` | Nothing. Framework doc, not an RFC; premise is pre-fork and stale |

Nothing in this directory is `implemented` or `superseded` today.

## Front matter

Every document carries YAML front matter as the machine-readable record. The
prose header below it stays human-readable and carries the *why*; front matter
carries the *what*.

```yaml
---
title: Channel Plugin Architecture — shared runtime, channels as app extension points
status: partial            # see vocabulary below
author: zezhexu
created: 2026-07-28
last-audited: 2026-08-03   # when status was last verified against code
audited-at: 0ab6ed48       # the commit it was verified against
doc-pr: 689                # the PR that merged this document
implementation-prs: [777, 1019, 1234]
tracking-issues: []
supersedes: []
superseded-by: []
---
```

Optional keys: `kind: framework` for docs that are policy rather than a
reviewable change to a named component, and `revision:` where a document is
versioned across review rounds.

`last-audited` and `audited-at` exist because a bare `status: partial` rots
silently. If those two fields are far behind main, distrust the status.

### Status vocabulary

| Status | Meaning |
|---|---|
| `draft` | Proposed. Nothing built. |
| `accepted` | Design agreed and locked. Nothing built yet. |
| `in-progress` | Implementation is live in an open PR or an active branch. |
| `partial` | Some phases are on main; the rest are open. The prose status line names which. |
| `implemented` | Every phase is verifiably on main. |
| `superseded` | Replaced. `superseded-by` names the replacement. |

`partial` is the most common status and the most dangerous one to read
carelessly — several documents here describe a plan that main only partly
follows, and two describe a plan main **deliberately diverged from**.

## Reading a `partial` or divergent RFC

Three failure modes are live in this directory. Each document's prose status line
calls out its own, but the patterns are worth knowing before you trust any of them:

1. **The plan was overtaken.** `rfc-resumable-subagent-sessions.md` had its
   Phase 0 probe return a negative verdict, which redirected the whole design —
   what shipped (continuable conversations) is not what the phases below it
   describe. `rfc-workspace-config-evolution.md` had its Phase 3 vector-store
   isolation affirmatively reversed by a later commit. Neither document was
   revised afterwards.
2. **The credit is not the RFC's.** `rfc-i18n-measurement.md` shows `partial`,
   but the proposals that shipped were already in flight under a separate
   program, one of them merging 18 hours before the document did.
3. **A dependency claim is overstated.** `rfc-notification-bridge.md` asserts the
   bus RFC's phases "all shipped". The phases the bridge actually needs are real;
   the blanket claim is not.

When a document and the code disagree, the code wins and the document is a bug.
Fix it in the same PR that discovers the drift.

## Writing a new RFC

[GOVERNANCE.md](../../GOVERNANCE.md) covers who decides whether an RFC is
accepted, and the scope test for when a change needs one at all.

- File as `rfc-<topic>.md`, kebab-case. Framework or policy docs that propose no
  reviewable change to a named component drop the prefix and set `kind: framework`.
- Open with front matter, then an H1 `# RFC: <Title>`, then the prose header.
- Write in English.
- Structure that has worked here: Summary → Motivation (current state, problems)
  → Goals → Non-goals → Design → Migration plan (phased, each phase PR-sized with
  **exit criteria**) → Backward compatibility → Security considerations →
  Alternatives considered → Open questions.
- Phases earn their keep by being independently shippable and independently
  abandonable. State exit criteria as assertions someone can test, and mark any
  phase whose entry depends on an unanswered open question as blocked on it.
- **Verify before asserting.** Claims of the form "X does not exist" or "Y is
  unused" are the ones that most often turn out wrong. Grep for callers, not just
  definitions — a defined-but-uncalled symbol means the behavior does not happen,
  which is a different (and usually more interesting) finding than absence.
  Quote `file:line`. Name the commit you measured at, as
  `rfc-i18n-measurement.md` does.
- A probe phase that exists to answer a question must write its verdict down
  somewhere durable and the RFC must be updated to point at it. PR #1023 recorded
  its Phase 0 verdict in the PR description; the RFC still does not reference it,
  which is why that document now needs a reader's warning.

## Keeping this honest

When you land an implementation PR for anything here, update the document's
`status`, `implementation-prs`, `last-audited` and `audited-at` in the same PR,
and re-audit the whole directory whenever the table above starts feeling
plausible rather than checked. The audit is cheap: for each document, extract its
named deliverables, grep for each one's definition and callers, and check the PR
history for the phase that claims to have landed it.
