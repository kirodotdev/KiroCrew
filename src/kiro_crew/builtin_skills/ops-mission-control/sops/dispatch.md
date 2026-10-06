---
cron: ops-mission-control/dispatch
schedule: "*/2 * * * *"
tier: on_shift
silent: true
---

# SOP: Dispatch heartbeat

Claim-based dispatch. This job is **silent** — it must produce no output at all
when there is nothing new. That silence is what keeps the ops channel readable:
a heartbeat that never speaks unless there is news is what makes the channel
survivable at all.


## Authenticate first

Every app API call goes through the `ops_mission_control_api` MCP tool — it
carries the gateway's own credential and always reaches this instance. Never
call the API over raw HTTP and never hunt for a credential — see SKILL.md
§ Calling the API for why. Paths below are relative to the app base, exactly
as the tool takes them.

## Steps

1. `POST /dispatch` — runs one whole cycle server-side: polls every configured
   source, claims up to the per-cycle cap (3 by default; the cap exists so a
   provider fanning out 200 alarms cannot spawn 200 sessions), attaches ledger
   matches and evidence, rechecks pending actions and runs the stale sweep. It
   returns `claimed` (each with `incident`, `matches`, `fast_path`), a `briefs`
   map keyed by incident id, `released`, `errors` and `changed`. A single
   unreachable provider in `errors` is normal and is not worth a message. Do NOT
   claim with `POST /incident/claim` here: that is the board's manual claim (body
   `{"signal": {...}}`, recorded as `claimed_by: operator`) and it skips the
   stale sweep and the post-action recheck.

2. For each entry in `claimed` (already claimed; a race another instance won
   simply does not appear):

   a. Use `briefs[<incident_id>]` as the investigation kickoff's context.

   b. Read the entry's `incident.operating_mode` and `incident.ledger_matches`.

   c. Create the investigation chat slot. **The slot key MUST be exactly
      `ops-mission-control-<incident_id>`** (e.g. `ops-mission-control-INV-7`) —
      the dashboard's incident panel polls that key to show the user what you are
      doing, so any other key leaves them staring at an empty conversation beside
      a live investigation. Title it `<incident_id> — <signal title>`, then post the
      investigation kickoff referencing the `ops-mission-control` skill. Include the
      operating mode in the kickoff so the investigator knows its authority on turn one.

      **You do not link the Slack thread yourself.** Step (d) below does it: recording
      `slot_key` registers the board thread with the session map, which is what makes a
      reply in that thread reach the investigation. The response reports
      `slack_thread_replyable` so you can see whether it took.

      The slot is created through the chat API (this one is outside the app
      base, so it is a plain gateway route, not an `ops_mission_control_api`
      path): `POST /api/chat/slots` with body `{"name": "ops-mission-control-INV-7"}`
      — use the tool or route your session runtime provides for slot creation.
      **If your runtime provides neither** (no slot tool and no credentialed
      HTTP route — the normal case for an unattended cron run), do NOT
      improvise with raw HTTP: launch the investigator with `spawn_run`
      instead, passing the same investigation kickoff (incident id, signal
      title, operating mode, and the `ops-mission-control` skill reference) as
      the task -- the investigator must start from the kickoff alone, not
      from this dispatch turn's view of the other incidents. The incident panel shows "no session yet" without the
      conventional slot key, but the investigation genuinely runs and its
      outcome lands through the transition in (d).

      **Only continue to (d)'s `investigating` transition after the
      investigator actually launched** — a created slot with the kickoff
      posted, or a `spawn_run` that returned a spawned agent id. If BOTH
      launch paths fail, do not transition it and report the launch failure
      instead: transitioning to `investigating` with no investigator
      running marks the claim as handled and suppresses redispatch until the
      stale sweep, which is exactly how an alert goes quietly uninvestigated
      for the whole stale window.

      Because the user is watching that panel, they can also approve tool calls
      from it: an approval card rendered in the embed resolves through the
      slot route, `/api/chat/slots/<slot>/approve`. So when you need permission for a read-only
      probe, ASK — do not silently skip the step.

   d. `POST /incident/transition` to
      `investigating`, attaching `slot_key` (the same
      `ops-mission-control-<incident_id>`) and `slack_thread_ts`. Do this even when you
      have nothing else to record: it is what makes the Slack thread answerable.

3. Stale sweep (run by `POST /dispatch` in step 1; nothing for you to call):
   incidents idle beyond the stale window are released back to `stale` for
   re-pickup and listed in `released`. This is what stops a dead investigation from holding a
   signal claimed and therefore unworked forever — **including one parked at
   `needs_human`**, which gets a longer window (6× by default) because waiting on a
   person is legitimately slower than an agent dying, but must not wait forever.

4. If the response reports `changed: false` (nothing claimed, nothing went
   stale): **exit silently.** No message,
   no notification, no channel post.

## Rules

- Never claim a signal that already has an incident in a non-stale status.
- Never post the raw provider payload — it may contain credentials. Everything
  that leaves this job goes through the app's redaction path first.
- Post to the ops channel, never a DM. A silent cron using the default
  `send_message` target sends a DM, which is the wrong surface and a known trap.
