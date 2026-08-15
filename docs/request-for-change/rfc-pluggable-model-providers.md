---
title: Pluggable agent model providers
status: draft
author: ptias
created: 2026-08-07
last-audited: 2026-08-07
audited-at: upstream/main
doc-pr: 1872
implementation-prs: []
tracking-issues: [1693]
supersedes: []
superseded-by: []
---
# RFC: Pluggable agent model providers

- Status: draft — **this RFC recommends that Kiro Crew support provider choice
  for the agent model, and asks the maintainers to amend the `AGENTS.md` "Other
  providers" invariant accordingly.** No design is proposed here: the shape of
  the seam is a separate discussion, and deliberately out of scope so the
  direction can be settled on its own merits.
- Author: ptias
- Created: 2026-08-07
- Related: issue [#1693](https://github.com/kirodotdev/KiroCrew/issues/1693)
  (the original request), `docs/system-specs/modules/providers.md` (records the
  current single-provider constraint)

## Summary

`agent.provider` is fixed to `acp`, so the agent model always arrives through
kiro-cli. `AGENTS.md` reinforces this under *Never re-add*: "Kiro Crew is
KiroACP-only".

**This RFC recommends lifting that invariant.** Not because it was wrong when
written — it fell out of de-Amazoning a single-backend internal package, and was
the right simplification then — but because the conditions that justified it
no longer hold, and keeping it now pushes cost onto this repo rather than saving
it. This document argues that case and asks for a direction decision. It does not
propose an implementation.

## Recommendation

Accept provider choice for the **agent** model in principle, and amend the
`AGENTS.md` "Other providers" bullet to say so. Treat the seam's shape,
capability floor and guarantees as a follow-up design discussion.

If full generality is the objection rather than the idea, accept it narrowly —
Amazon Bedrock only, or explicitly as a documented reduced-capability mode. A
narrow yes still removes most of the cost described below; a no does not.

## Why keeping the invariant is the expensive option

**The fork tax lands here, not only on forks.** A rule against providers does not
remove the need for them. It moves the work into private forks, where each one
carries its own copy of the same plumbing, re-does it on every upstream sync, and
diverges a little further with each release. None of that divergence is visible
to this repo, but its consequences are: bug reports arrive from users running
provider code no maintainer has reviewed, against a `main` that never had the
seam. One reviewed seam upstream is cheaper to support than N unreviewed ones
downstream — and cheaper to *reason about*, because the failure modes are in the
tree instead of in someone's branch. For a capability this basic, the
maintenance argument points toward main.

**Data residency is a harder constraint here than for a coding assistant.** Kiro
Crew runs unattended and reads whatever the work requires — source code and
proprietary repositories, internal infrastructure and its credentials, and for
users who wire up those surfaces, browser pages, messages and calendars. For many
teams the blocker is not a preference about models; it is that prompts and file
contents must not leave a boundary they control. A local or in-account model is
the only answer to that, and documentation cannot substitute for it. Today those
teams have no supported configuration, so they either fork or walk.

**Bedrock is the version of "yes" an organization can already give.** Teams
evaluating Kiro Crew commonly have Bedrock quota, IAM, VPC endpoints, model
guardrails and consolidated billing in place. Routing agent traffic through
infrastructure they already govern turns adoption from a procurement question
into a configuration change. That is a disproportionate adoption unlock for a
small surface, and it is the single cheapest option to accept.

**Cost shape follows the product's own direction.** Long autonomous runs — crons,
monitors, sub-agent fan-out — are where routine steps dominate spend, and they
are exactly the workloads Kiro Crew has been growing toward. `agent.tips_model`
and `agent.judge_model` already establish that per-role model selection is a
shape this project accepts. Provider choice is the same idea one level down.

**The precedent is already half-built.** Local inference ships today for
*embeddings*: `llama-cpp-python` is bundled and runs in-process, and
`configuration.md` records that a legacy `"ollama"` embedding value is coerced to
`"llama_cpp"` on load. So the codebase already treats local models as legitimate
here — just for one subsystem. The asymmetry is hard to explain to a user who
wants the same property for the agent, which is the part that actually sees their
data.

## Anticipated objections

**"Fork it and maintain the provider yourself."** This is the status quo, and it
is what the fork tax paragraph describes. It works, in the narrow sense that the
feature exists somewhere — but it multiplies the work, keeps it unreviewed, and
still routes the support burden back here. It is also self-reinforcing: the more
forks carry it, the more the upstream seam looks unnecessary, while the real cost
grows out of sight. For a peripheral feature that trade is reasonable. For "which
model runs the agent" it is not.

**"A non-`acp` provider will be materially less capable, and that is worse than
no option."** It will be less capable, and that should be stated as a guarantee
rather than discovered by users — a documented floor, a warning at configure
time, and honest release notes. But "less capable" is not "not useful": a user
who needs their data to stay inside a boundary is choosing between a reduced
agent and no agent. Framing the reduced mode explicitly is a doc-and-UX problem,
which this project already solves elsewhere; it is not a reason to withhold the
option.

**"This re-opens something de-Amazoning deliberately closed."** The
KiroACP-only rule collapsed a multi-provider seam that existed to serve an
internal product. That was the correct call for that goal. The rule as written
now reads as a permanent product invariant, and it is being applied to a
different question — whether an OSS project with external users should let those
users choose a model. Those are separable, and the second deserves its own
answer rather than inheriting the first one's.

**"Support ownership is unclear."** Reasonable, and answerable in the follow-up
design: a provider seam can be documented as best-effort, gated behind an
optional extra, and excluded from the support surface that the default path
enjoys. This is a scoping decision, not an obstacle.

## The decision this RFC asks for

A direction call, in one of two forms:

1. **Accept in principle** (recommended). The maintainers amend the `AGENTS.md`
   "Other providers" bullet, and scope/capability/guarantees move to a follow-up
   design discussion. Note the amendment should come from the maintainers, not
   from the PR that introduces the feature — a contributor PR editing the rule
   that forbids it is exactly the wrong shape, and this RFC does not ask for it.
2. **Accept narrowly.** Bedrock only, or an explicitly documented
   reduced-capability mode, if generality is the concern.

If the answer is instead to reaffirm KiroACP-only, this RFC asks that it be
recorded with its rationale in `AGENTS.md` rather than left as a bare
prohibition, and for #1693 to be closed explicitly — so that the next contributor
reaches the decision instead of rediscovering the argument.

## Non-goals

- Proposing a protocol, config schema, or adapter design. Out of scope until the
  direction is settled.
- Claiming parity with the kiro-cli path. Any non-`acp` provider starts
  materially less capable; that trade-off belongs in the follow-up design, stated
  as a guarantee.
- Changing embedding behaviour. In-process embeddings are settled; this concerns
  the **agent** model only.

## Open questions (shape, not direction)

1. Is a documented **reduced-capability** provider acceptable, or would the
   maintainers want a defined capability floor before any seam lands?
2. Is Bedrock specifically more acceptable than the general case, given it keeps
   traffic inside an account the user already controls?
3. Should provider choice be per-role (reusing the `tips_model` / `judge_model`
   precedent) or a single setting for the whole agent?

## Prior art in this repo

An implementation was explored on a branch and is **shelved pending this
decision** — PR #1872's earlier history, and tag
`litellm-provider-impl-shelved` on the author's fork. It is mentioned only as
evidence that the shape is achievable at modest size; it is explicitly not what
this RFC asks anyone to review.
