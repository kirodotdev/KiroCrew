---
cron: null
schedule: null
tier: on_demand
silent: false
---

# SOP: Investigate one incident

Runs inside the per-incident chat slot the dispatch heartbeat created. The Slack
thread linked to that slot mirrors this conversation, so the operator can reply in
context and be talking to you.


## Authenticate first

Every app API call goes through the `ops_mission_control_api` MCP tool — it
carries the gateway's own credential and always reaches this instance. Never
call the API over raw HTTP and never hunt for a credential — see SKILL.md
§ Calling the API for why. Paths below are relative to the app base, exactly
as the tool takes them.

## Phase 0 — know your authority

Read the incident's `operating_mode` before planning anything.

- `observe` → change nothing, anywhere. Investigate and report.
- `propose` → draft the exact action and ask. Do not execute.
- `act` → execute only what a matching rule grants.

If the mode is missing or ambiguous, treat it as `observe`.

Never run a remediation command against infrastructure regardless of mode. This
app proposes; the human applies.

## Phase 1 — ledger first

The incident arrives with `ledger_matches` already populated from its fingerprint.

- **Read the brief's own framing rather than judging the entries yourself.** It says
  either `KNOWN PATTERN` or `Possible prior patterns`, and that line is computed by the
  same predicate the engine uses. Four things have to hold for the fast path:
  `trust: verified`, `confidence: high`, `use_count` of at least 2, and **no recorded
  failure**. Verified+high alone is not enough — anyone can POST an entry claiming both,
  so a `use_count` of 1 means "this has never been applied to anything but the incident
  in front of you".
- On the fast path: confirm it still applies, then go straight to a proposal citing it.
- Weaker matches are hypotheses, not answers. Say which one you are testing.
- **A match carrying `WARNING: this fix was applied and the signal was still firing
  afterwards` has already been tried and lost.** That is a stronger statement than "not
  proven" — an untested guess is worth more than a refuted one. Do not propose it as the
  answer. Either establish why it failed before, or say plainly in your diagnosis that
  you are retrying something that has already failed and what you changed.
- No match means this is new. That is fine — your job includes writing the entry
  that makes the next occurrence cheap.

## Phase 2 — read the evidence you were given

**The evidence is already in your brief.** You do NOT have credentials for the
operator's AWS account (or PagerDuty, or Datadog) and you are not meant to: the
gateway holds them, reads the alarm history and recent log lines under a budget,
redacts the result, and hands you the text. Do not try to run `aws` commands or fetch
provider APIs yourself — it will fail, and the failure is by design rather than a
misconfiguration to work around.

Look for the `Provider evidence, already gathered for you` block. If it is absent or
thin, that is itself information: say so explicitly rather than speculating with
confidence you do not have, and name what you would have wanted. An operator can add
a log group in Settings → Providers → AWS CloudWatch evidence, which is a far better
outcome than a guess.

Useful questions, in rough order of yield:
- Is the signal still firing, or did it self-clear?
- What changed recently — a deployment, a config change, a traffic shift?
- Is this one resource or many? A pattern across resources points at shared
  infrastructure.
- Has this fingerprint fired before, and what happened then?

## Phase 3 — decide

Pick exactly one and say which:

- **Resolved** — condition genuinely cleared, or a verified fix applies and you
  have the authority to apply it.
- **Propose** — you know what should happen; draft the precise action and note,
  and ask.
- **Needs human** — requires judgement, a credential you lack, or an
  infrastructure change.
- **Escalated** — belongs to another team or system. Name which, and why.

## Phase 4 — document

This phase is what makes the whole app worth running. **It is not optional, and it
is not done by writing the answer in chat.** An incident with no recorded diagnosis
shows on the board as `needs_human / "Stopped, no diagnosis"` — so if you skip this,
the operator is told you gave up even when your answer is sitting right there in the
transcript. Observed for real: two incidents read "Stopped, no
diagnosis" while a complete root-cause analysis was one scroll away.

1. **Record the diagnosis on the incident.** Do this FIRST, before posting anywhere
   — it is what clears the "no diagnosis" state:

   ```
   ops_mission_control_api(method="POST", path="/incident/transition",
     body_json='{"id": "INV-7",
                 "status": "needs_human",
                 "diagnosis": "<2-3 sentences: what broke and why>",
                 "resolution": ""}')
   ```

   Pick `status` from your Phase 3 decision:
   - **Resolved** → `"status": "resolved"` plus a `resolution`.
   - **Propose / Needs human** → `"status": "needs_human"` with the `diagnosis`.
     The board then shows *what you concluded*, not "stopped".
   - **Escalated** → `"status": "escalated"`, naming the owner in `resolution`.

   A `409` means the transition is not legal from the incident's current state —
   read the error, do not retry blindly.

2. **Slack is automatic — do not post it yourself.** When the operator has turned
   on the Slack channel, recording the diagnosis in step 1 already updates the
   incident's board message *and* posts your `diagnosis`/`resolution` into its
   thread. Writing it again by hand produces two copies of the same finding. Write
   the diagnosis to be read by someone on a phone, because that is where it lands.
3. If you learned a reusable pattern, add a ledger entry. Describe the
   **observable symptom** in `pattern` (that is what the next responder matches
   against), a **specific** `fix`, and be honest about `trust` — `verified` only
   if you saw it work.
4. If the obvious fix has a side effect, put that in the entry. The trap is the
   most expensive part to rediscover.

   ```
   ops_mission_control_api(method="POST", path="/ledger",
     body_json='{"pattern": "<observable symptom>",
                 "fix": "<specific fix + any trap>",
                 "fingerprints": ["<this incident'\''s fingerprint>"],
                 "provider_keys": ["<this incident'\''s provider_key, if it has one>"],
                 "confidence": "high", "trust": "observed"}')
   ```

   Bind the incident's own `fingerprint` (it is in your brief) or the entry will
   never match the recurrence it was written for.

   **Also bind `provider_key` when the signal has one.** The fingerprint is a hash of
   the alarm's *wording* with every bare number stripped, so two genuinely different
   alarms on one resource can share it — a 4xx-rate alarm and a 5xx-rate alarm do. The
   `provider_key` is the identity the provider itself assigned, so binding it is what
   makes the next occurrence an exact match rather than a guess that happens to rhyme.

5. **Write the diagnosis back to the system the alert came from**, when the incident
   came from a tracker a colleague reads (PagerDuty, GitHub, Datadog). A comment is the
   safest write there is: append-only, attributed, reversible, zero blast radius — and
   it is the only way your finding reaches someone who lives in the ticket rather than
   in Kiro Crew.

   ```
   ops_mission_control_api(method="POST", path="/incident/action",
     body_json='{"id": "INV-7", "action": "comment",
                 "note": "<one paragraph: what broke, why, what to do next>"}')
   ```

   **Always attempt it; never work around a refusal.** Under `observe`/`propose`, or
   without a matching rule, this returns `403` with the reason and the refusal is
   audited — which is the system working, not an obstacle. Do not then post the same
   text by another route, and do not ask the operator to widen a rule so it lands.

**If a prior ledger entry turned out to be wrong or incomplete, say so in a new
entry.** That has already happened once: an entry blamed a missing caller
permission when the real cause was target-side trust. Correcting it made the next
occurrence cheaper; leaving it would have sent the next responder down the wrong
path with `high` confidence behind it.

## Noise discipline

One incident, one thread. Do not post progress updates that say nothing. Check
what the thread already says before adding to it — re-notifying about an unchanged
condition is how a useful channel becomes an ignored one.

The board message itself is **edited in place** on every status change, so the
channel shows one line per incident rather than a running commentary. Nothing you
do needs to maintain that — it is why you should not hand-post state changes.
