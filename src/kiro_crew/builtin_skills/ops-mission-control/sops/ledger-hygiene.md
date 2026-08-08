---
cron: ops-mission-control/ledger-hygiene
schedule: "0 3 * * *"
tier: primary
silent: true
---

# SOP: Ledger hygiene

Keeps the knowledge ledger from rotting. A ledger that only accumulates becomes
noise, and noise is exactly what an undocumented, word-of-mouth approach
already suffered from.

Runs on the `primary` tier only — a team with five instances sharing one ledger
must not run this five times.


## Authenticate first

Every app API call goes through the `ops_mission_control_api` MCP tool — it
carries the gateway's own credential and always reaches this instance. Never
call the API over raw HTTP and never hunt for a credential — see SKILL.md
§ Calling the API for why. Paths below are relative to the app base, exactly
as the tool takes them.

## Steps

1. Run the hygiene pass. It is an HTTP endpoint, not a Python call — there is no
   interpreter for you to call `ledger.hygiene()` from:

   ```
   ops_mission_control_api(method="POST", path="/ledger/hygiene")
   ```

   It returns `{"summary": {"deduped": N, "decayed": N, "demoted": N, "pruned": N}, ...}`
   and performs four things deterministically, so no judgement is needed here:
   - **Dedupe** by content-addressed id, merging fingerprints and keeping the
     highest use count. Duplicates arrive when two people learn the same lesson.
   - **Decay** confidence one step for entries unused past the decay window. An
     entry nobody has needed in three months should not still claim `high`.
   - **Demote** confidence one step for entries whose `miss_count` has outgrown their
     `use_count` ratio — a fix that was applied and the signal kept firing.
   - **Prune** to the entry cap, dropping weakest first, now ordered by
     `use_count - miss_count` so an entry that keeps matching the wrong failure is not
     the last thing kept. The cap exists because matched entries are read into an
     investigation's context — an unbounded ledger is an unbounded token cost.

   **`decayed` and `demoted` are opposite findings and must not be reported as one
   number.** `decayed` means the estate moved on and nobody needed these; `demoted` means
   these were tried and did not work. If `demoted` is non-zero, say WHICH entries — that
   is the only line in this job's output an operator will act on tonight.

2. Resolve contradictions. Do NOT scan the ledger by eye — the pass already found them
   for you, and `summary.contradictions` in step 1's response is the count:

   ```
   ops_mission_control_api(method="GET", path="/ledger/contradictions")
   ```

   Each result is a pair of entries sharing a fingerprint but claiming DIFFERENT fixes,
   ordered most-used first — so the pairs actively misleading responders come before the
   speculative ones. If the count is 0, skip this step entirely and say nothing about it.

   Two fixes for one fingerprint almost always means the failure has more than one cause.
   **Split the pattern descriptions so each names its own cause**, then re-POST both
   entries. Do not delete one: it is a real fix that worked for somebody, and deleting it
   means the next responder rediscovers it from scratch. If you genuinely cannot tell the
   two causes apart, leave the pair alone and report it — an honest "these two conflict
   and I could not separate them" is worth more than a confident wrong merge.

3. Promote `observed` → `verified` for any entry whose fix has now been applied
   successfully more than once. That promotion is one of FOUR conditions for the fast
   path — the others are `confidence: high`, `use_count >= 2`, and `miss_count == 0` — so
   it is worth doing deliberately, and it does not on its own make an entry an answer.

   **Never promote an entry with a non-zero `miss_count`.** That entry has been applied
   and the signal kept firing; marking it `verified` asserts you saw it work, about the
   one entry there is recorded evidence against. If you believe the failure has more than
   one cause, that is step 2's job — split the patterns — not this one's.

   You cannot post `miss_count` and must not try: the field is only ever written by an
   observed recheck, and the route ignores it. Re-posting an entry therefore cannot clear
   its recorded failures, deliberately — otherwise this promotion step would double as a
   way to erase them.

   There is no separate "update" route: re-post the entry with its **exact same
   `pattern` and `fix`**. Ids are content-addressed over those two fields, so this
   merges into the existing entry — fingerprints union, `use_count` carries forward,
   and trust upgrades to `verified`. It can never weaken what is already known.

   ```
   ops_mission_control_api(method="POST", path="/ledger",
     body_json='{"pattern": "<byte-identical to the stored pattern>",
                 "fix": "<byte-identical to the stored fix>",
                 "confidence": "high", "trust": "verified"}')
   ```

   Change a single character of `pattern` or `fix` and you get a NEW entry rather
   than a promotion, leaving a near-duplicate for the next hygiene run to dedupe.
   Read the entry first (`GET /ledger`) and copy the fields verbatim.

4. Report only if something changed. A no-op night produces no output.

## Rules

- Never delete an entry that has been used. Decay it instead — use count is
  evidence that it described something real.
- Do not invent entries here. This job curates what investigations recorded; it
  does not author knowledge.
