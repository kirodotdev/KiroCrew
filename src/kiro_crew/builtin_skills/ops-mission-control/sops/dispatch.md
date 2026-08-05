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

```bash
URL=$(kirocrew token 2>/dev/null | grep -oE 'http://[^ ]+' | head -1)
BASE="${URL%%\?*}"; TOKEN="${URL#*token=}"
```

Reuse `$BASE`/`$TOKEN` for every call below and pass `?token=$TOKEN`. Never hardcode a
port and never hunt for a token elsewhere — see SKILL.md § Calling the API for why.

## Steps

1. `GET /api/apps/ops-mission-control/signals` — polls every configured source
   concurrently and returns `unclaimed` already diffed against the dispatch index.
   Per-source errors come back in `errors`; a single unreachable provider is
   normal and is not worth a message.

2. For each unclaimed signal, up to **3 per run** (the cap exists so a provider
   fanning out 200 alarms cannot spawn 200 sessions):

   a. `POST /api/apps/ops-mission-control/incident/claim` with the signal.
      A `409` means another instance won the race — skip it, do not retry.

   b. Read the returned incident's `operating_mode` and `ledger_matches`.

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

      ```bash
      curl -sS -X POST "$GATEWAY/api/chat/slots" \
        -H 'Content-Type: application/json' \
        -d '{"name": "ops-mission-control-INV-7"}'
      ```

      Because the user is watching that panel, they can also approve tool calls
      from it: an approval card rendered in the embed resolves through
      `/api/approvals/<id>/approve`. So when you need permission for a read-only
      probe, ASK — do not silently skip the step.

   d. `POST /api/apps/ops-mission-control/incident/transition` to
      `investigating`, attaching `slot_key` (the same
      `ops-mission-control-<incident_id>`) and `slack_thread_ts`. Do this even when you
      have nothing else to record: it is what makes the Slack thread answerable.

3. Stale sweep: incidents idle beyond the stale window are released back to
   `stale` for re-pickup. This is what stops a dead investigation from holding a
   signal claimed and therefore unworked forever — **including one parked at
   `needs_human`**, which gets a longer window (6× by default) because waiting on a
   person is legitimately slower than an agent dying, but must not wait forever.

4. If nothing was claimed and nothing went stale: **exit silently.** No message,
   no notification, no channel post.

## Rules

- Never claim a signal that already has an incident in a non-stale status.
- Never post the raw provider payload — it may contain credentials. Everything
  that leaves this job goes through the app's redaction path first.
- Post to the ops channel, never a DM. A silent cron using the default
  `send_message` target sends a DM, which is the wrong surface and a known trap.
