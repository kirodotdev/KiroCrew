# Credential redaction is one owner switch, off by default

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-15
Supersedes: 2026-10-20-redaction-classifies-by-what-a-value-can-do.md (in part: redaction runs only while the owner's switch is on; the tiers still say what it masks)

## Decision

Credential redaction is one owner switch, off by default, written in one place
(`redaction_switch.DEFAULT_REDACTION_ENABLED = False`) and read on every redaction path.
It is governed by `capabilities.credential_redaction_off`, whose meaning is widened on
purpose: a policy that grants it lets the owner turn all credential redaction off. An
explicit deny is never dropped: a deny of the retired
`capabilities.credential_redaction_seams_off` still denies the switch, even beside a grant
(tightest wins). The legacy redaction engine is removed; the rule table is the only
engine and masks only values that grant access. The owner's OFF applies only to what the
owner alone sees: the owner's own dashboard session, the owner's Files view, the model,
and the owner's 1:1 DM with the crew bot where the code knows it is one. Anything a
non-owner human or an app can see stays redacted: a channel or group with other members,
a Shared Crew teammate, an allowlist link, a `!dashboard` user, an app token. Safety
decisions and the security event log stay redacted whatever the switch says, and each
gateway boot that reads OFF only from the default writes one security event log record
saying so.

## Why

- The maintainer ruled: one owner switch, off by default, governed by
  `capabilities.credential_redaction_off` with a widened meaning, and the legacy engine
  removed.
- Redaction kept rewriting ordinary links and identifiers, and users asked for it to be
  invisible.
- Under the maintainer's delegation: main redacted every non-owner audience, and with OFF
  as the default anything else would hand the owner's credentials to teammates, links,
  apps and channel members without anyone opting in.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/37408#issuecomment-6894498871 -- the
  maintainer's decision, restated on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/37408#issuecomment-6901157275 -- the
  audience rule, decided under the maintainer's delegation and restated on the pull request.
- https://github.com/kirodotdev/KiroCrew/pull/37408 -- the pull request that delivers it.
- https://github.com/kirodotdev/KiroCrew/issues/22552 and
  https://github.com/kirodotdev/KiroCrew/issues/31895 -- the redaction issues it settles.
