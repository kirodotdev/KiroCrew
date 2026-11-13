---
title: The structured credential detectors are on by default
status: draft
author: Crew Tasks Bot, directed by mbriones98
created: 2026-11-09
last-audited: 2026-11-09
audited-at: 76fc1c61e8
doc-pr:
implementation-prs: [29369, 31863, 29423]
tracking-issues: [22552, 31895, 31690, 31692]
supersedes: []
superseded-by: []
---

# RFC: The structured credential detectors are on by default

Status: draft. This document lands on its own, before
[#29423](https://github.com/kirodotdev/KiroCrew/pull/29423), because that PR
changes a default for every install. Code citations name a file and a symbol,
not a line number, and were read at #29423's head `76fc1c61e8` unless they say
main (`ad553518b0`). Paths are under `src/kiro_crew/` unless they start with
`test/` or `docs/`.

The maintainer decision
[docs/decisions/2026-10-20-redaction-classifies-by-what-a-value-can-do.md](../decisions/2026-10-20-redaction-classifies-by-what-a-value-can-do.md)
already sets the tier rules: Secrets and Bearer links are redacted, an
Identifier never is. That entry says the tiers take effect phase by phase as
the PRs of [#22552](https://github.com/kirodotdev/KiroCrew/issues/22552) land.
This RFC asks for one narrower decision the entry does not record: that the
structured engine which applies those tiers runs by default on every install,
and what the way back is.

## 1. Summary

`redaction_structured.compat.use_rule_table()` answers `True` by default, so
every credential redaction call reads the structured detectors first. AWS
access key IDs, git object names, request ids, bitmap image data-URI payloads,
commit permalink SHAs and the macOS per-user temp directory stay visible.
Private keys, JWTs and AWS secret and session-token values are removed by the
detectors. Main's regex passes keep running after them, unchanged except that
pass 1 skips the branches the detectors cover.

Owners get a setting to put the old engine back without a new release. It
lands before or in the same release as the flip (section 6).

## 2. Current state

- [#29369](https://github.com/kirodotdev/KiroCrew/pull/29369) adds the
  structured detectors under `security/redaction_structured/` behind a
  module-level switch, `_USE_RULE_TABLE` in `compat.py`, which is `False`. It
  is changed only through `publish_use_rule_table()`, whose docstring says no
  user setting reads it. There is no config key, CLI flag or API route.
- With the switch off, redaction runs only main's four passes: pass 1 (fixed
  credential shapes, including the bare AWS key ID), pass 2 (base64-decoded
  credentials), pass 3 (entropy) and pass 4 (`?token=` values).
- `enabled` in `credential_redaction.json` is read only for the owner's own
  Files view and its diff. With it off there, the whole credential plan stands
  down. Every other path still redacts: chat and transcripts, channel egress,
  the ACP prompt, logs and the exfil-URL pass
  ([#31895](https://github.com/kirodotdev/KiroCrew/issues/31895)).
- Stack: #29369 (detectors, switch off) → #31863 (fixture swaps that pass with
  the switch either way) → #29423 (flip). None is merged.

## 3. Problem

Pass 1 decides sensitivity by shape alone. It removes values that cannot
authenticate:

- AWS access key IDs. A key ID names a key, is sent in cleartext in every
  signed request and appears in IAM errors, policies and CLI output. Removing
  it was the largest single source of false removals.
- Git object names and commit permalink SHAs.
- Request ids (`x-amz-id-2`, `HostId`).
- Long runs in image data URIs and the macOS temp path.

Every false removal hides something a person needed to read and teaches people
to ignore the redaction tag. Pass 1 also misses credentials whose shape it does
not know, for example a JWT glued to a sentence's full stop (`Done.eyJ...`).

## 4. Proposed default change

At #29423's head:

- `_USE_RULE_TABLE = True` in `compat.py`.
- Secret hits are tagged. Identifier values are swapped for a private-use
  placeholder while main's passes read the text, then restored
  (`protection.py`, `identifier_values.py`), so a regex pass can neither
  remove them nor borrow their characters to build a match.
- ARNs, public OAuth parameter values and `${...}` placeholders are reported
  but not masked, so main's passes still judge them.
- Pass 1 skips `COVERED_RULES` (`private_key`, `jwt`,
  `aws_secret_access_key`, `aws_session_token`) and the bare AWS key ID.
  Every other pass-1 branch, including URL userinfo, and passes 2 to 4 run
  unchanged.
- A `token=` value that a detector hit touches is claimed before the passes
  read the text.
- `StreamRedactor` (`security/text_redaction.py`) moves its detector cut back
  to a word start and out of any pass-1 match. `release_at_boundary()` commits
  what only the detectors' tail hold kept back. The dashboard main turn calls
  it at a tool call or prompt cut; Slack calls it at a tool card, a wait and
  the turn's end.
- Stored display verdicts and caches carry the engine's mark
  (`display_verdicts.engine_mark`), so a verdict computed with the switch off
  is recomputed, not reused.
- If the detectors fail to load, `_LOAD_FAILED` is set and every plan fails
  closed.

## 5. User-visible behaviour and false-positive risk

What users see change:

- Fewer `[REDACTED]` tags on identifiers in chat, transcripts, logs and tool
  output. Key IDs, SHAs and request ids read as written.
- A label stays and only the value goes: `aws_secret_access_key =
  [REDACTED: credential]`.
- Some secrets main missed are now removed (the glued JWT case above; a
  credential-shaped file name under `/var/folders/<b>/<id>/T/`, which is no
  longer hidden as part of the temp-path Identifier).
- Transcripts already written are not rewritten.

New false-positive risk (values that may now be redacted and were not), all
named by the Security Scope Review on #29423 as unadjudicated:

- Prose where a full stop is glued to a base64url run starting `eyJ`.
- A hash-named file under the macOS per-user temp directory, now read by the
  entropy and shape passes.
- A non-bitmap data URI (`image/svg+xml`, `application/json`, `font/woff2`)
  with a long base64 payload, now fed to the later passes.

New false-negative risk (secrets that may now survive):

- The GPT review on #29423 raised two blocking findings, both upheld:
  1. `compat.py` drops every legacy pass-1 match for a rule in
     `COVERED_RULES` even when the detectors did not handle that value, so a
     labelled AWS secret in a form the detectors reject (inline spaced
     assignment, newline-separated value, wide separator) can survive.
  2. Identifier masks are applied before pass 2 runs, so an encoded secret
     inside a masked span (a bitmap data-URI payload, an `x-amz-id-2` or
     `HostId` value) is never inspected.
- Both are regressions against switch-off behaviour. This RFC treats fixing
  them as an entry condition for the flip (phase 3), not as follow-ups.

Streaming surfaces: `release_at_boundary()` is called only from the dashboard
main turn and Slack. On side chats, threads, the channel-neutral TurnDriver
(Telegram, WeCom, Discord) and ACP native children, the detectors' tail hold
keeps the last words back until `flush()`. The final text is the same; it
arrives later at tool cuts.

## 6. The off switch and how owners opt out

Two different settings are in play. They answer different questions and
should not be merged into one.

| Setting | Effect | Default | Where |
|---|---|---|---|
| Engine selector | Off puts back main's regex-only plan: identifiers are removed again, nothing else changes | on (structured) | the "later stacked PR" named in #29423's description |
| Redaction off everywhere | Off stops credential redaction on every path, skipping both the structured engine and the credential plan | on | [#31895](https://github.com/kirodotdev/KiroCrew/issues/31895) |

Proposal:

- The engine selector is an owner setting in `credential_redaction.json`
  (name to be decided, see open question 1) that `publish_use_rule_table()`
  reads at startup and on config reload. It is the rollback for a bad detector
  verdict. Until it exists, the only rollback is a code revert and a new
  release, which the Design Review on #29423 also flags.
- The selector lands before, or in the same release as, the flip. Without it,
  an owner hit by a false negative has no choice but to wait.
- #31895 is a loosening that a maintainer has already agreed can follow
  separately (audit comment on #29369, point 3). It is not a precondition for
  this flip, because turning redaction off entirely is not the remedy for a
  wrong detector verdict.
- How an owner opts out once the selector ships: set the selector to off in
  `credential_redaction.json` and reload. Stored display verdicts carry the
  engine mark, so they are recomputed under the old engine rather than reused.

Maintainer conditions (direction signed on #34048, 2026-11-09). Phase 3 in
section 7 cannot exit without both:

1. The owner engine selector (the rollback switch) ships in the same PR as
   the default-on flip (#29423), not as a later PR. This answers open
   question 2.
2. The two blocking findings on #29423 are fixed first, each with a
   regression test: legacy pass-1 matches dropped for covered rules even when
   the detectors did not handle the value, and identifier masks applied
   before pass 2 so an encoded secret inside a masked span is never read.

## 7. Rollout and migration

Each phase is its own PR.

| Phase | PR | Exit criteria |
|---|---|---|
| 0 | This RFC | Merged on main with a non-draft status, then merged into the stack's base branches so First Principles reads it from #29423's base. |
| 1 | #29369 | Detectors on main, switch off. Main's redaction tests pass with the switch off. |
| 2 | #31863 | Fixture swaps on main. Tests pass with the switch either way. |
| 3 | #29423 | Both GPT security findings fixed, each with a regression test that fails with its fix removed. Switch on. CI green. Both maintainer conditions in section 6 met: the owner engine selector ships in this same PR, not a later one; the two blocking findings (legacy pass-1 matches dropped for covered rules even when the detectors did not handle the value; identifier masks applied before pass 2 so an encoded secret inside a masked span is never read) are fixed first, each with a regression test. |
| 4 | engine selector | Owner setting reads into `publish_use_rule_table()`; a test shows off reproduces main's plan. Ships in the same release as phase 3 or earlier. Blocked on open question 1. |
| 5 | #31895 | Redaction-off-everywhere setting, tested on every listed path with it on and off. |
| later | #31690, #31692 | Retire shape-only passes; detect labelled values in the engine. Out of scope here. |

Migration: none. No config key, API or stored format is removed or renamed.
An install that never touches the new setting gets the structured engine.
Stored verdicts recompute on first read because the engine mark changed.

## 8. Testing evidence

From #29423's description and code at `76fc1c61e8`:

- `test/test_redaction_structured_wiring.py`, 29 tests, pins the switch-on
  plan, starting with `test_the_switch_ships_on`, and including
  `test_a_bare_aws_key_id_stays_visible`,
  `test_a_labelled_aws_secret_streams_as_the_batch_pass` and
  `test_the_url_userinfo_branch_keeps_running`.
- `test/test_redaction_structured_protect.py`, 19 tests, pins identifier
  protection, including `test_protect_text_equals_redact`,
  `test_an_arn_is_not_masked` and
  `test_only_an_image_data_uri_payload_is_masked`.
- 28 further test files hold expectations that pass only with the switch on;
  per the PR description they fail on #29369's head plus #31863.
- Three Slack golden digests follow the stream's tail hold; final text is
  unchanged.
- Re-score on the real code with the switch on (`redact` policy, head
  `46eab0d264`):
  - factorial corpora, 9,000 records: recall 0.802, precision 0.954, break
    rate 0.031;
  - r2mix corpora, 9,000 records: recall 0.832, precision 0.899, break rate
    0.099.

From #29369:

- Its "Measured against main" table reports main at recall .748, precision
  .656, break rate .319, and the detectors at .811 / .945 / .038. #29423's
  description says those detector numbers came from a scoring script that ran
  main's URL and base64 checks differently from the real code. The main
  column likely came from the same script, so it is not directly comparable to
  the real-code re-score above (open question 3).
- Main's redaction test files pass with the switch off.

Current review state at `76fc1c61e8`: Design Review PASS; Opus review no
findings; Security Scope Review comment reports zero newly refused commands;
GPT review BLOCK (section 5); First Principles BLOCK pending this RFC.

## 9. Security considerations

- The flip removes less in one place on purpose: Identifier-tier values. The
  2026-10-20 decision records why that is safe: an Identifier cannot
  authenticate alone or with public data.
- The flip must not remove less anywhere else. The two GPT findings are the
  places where it currently does, so phase 3 cannot exit until both are fixed
  and tested.
- `ABIA` and `ACCA` key IDs stay as they are until the security owner confirms
  their tier, per the decision entry.
- The detectors fail closed when they cannot load (`_LOAD_FAILED`).
- The exfiltration URL check and the shell deny tiers do not use redaction
  results to decide what runs (Security Scope Review), so the flip changes
  what output shows, not what commands are allowed.
- The engine selector loosens nothing beyond main: its off state is main's
  behaviour today. The redaction-off setting (#31895) does loosen, and stays
  default on.

## 10. Alternatives considered

**Keep the default off.** Ship #29369 and leave the switch for owners to try.
No behaviour risk, and it needs no RFC. But there is no owner setting today,
so in practice nobody runs the engine, the false removals stay for everyone,
and #29369's code is dead weight that rots. It also leaves the 2026-10-20
decision unimplemented.

**Opt-in setting.** Ship the selector first with default off and flip later.
Owners who want readable key IDs get them now and the risk falls only on
those who chose it. The cost is a second default-change round later, and an
opt-in redaction setting gathers little signal because few owners change
security defaults. It also keeps the over-redaction for the large majority.

**Staged rollout.** Default on for new installs, or for a share of installs,
or for some surfaces (for example the dashboard first, channels later).
Limits blast radius, but Kiro Crew has no percentage-rollout mechanism for
config defaults, and a per-surface split would make the same text redact
differently in the dashboard and in Slack, which is the inconsistency the
single rule table exists to remove. A new-installs-only default leaves
existing installs on the worse engine indefinitely.

**Recommended:** default on for every install, gated on the two security
fixes, with the engine selector shipped no later than the flip. That gives
the full benefit at once and keeps a no-release rollback.

## 11. Open questions

1. What is the engine selector called and where does it live: a new key in
   `credential_redaction.json`, or a value of the existing `enabled` key
   (for example `structured` / `legacy` / `off`) that #31895 then widens to
   every path?
2. Should the selector ship as part of #29423 rather than as a separate PR, to
   guarantee phase 3 and phase 4 land in the same release?
3. Can main's numbers be re-scored on the real code against the factorial and
   r2mix corpora, so section 8 has a like-for-like comparison?
4. Should `release_at_boundary()` be called at the TurnDriver's boundary before
   the flip, so channel and side-chat surfaces do not hold back their last
   words at tool cuts?
5. Does this RFC need a maintainer other than the decision entry's author to
   accept it, given that it changes a security default?
