---
cron: ops-mission-control/reconcile
schedule: "*/15 * * * *"
tier: always
silent: true
---

# SOP: Reconcile board against provider truth

Keeps the board honest. Without this it drifts into fiction: incidents sit
`investigating` for signals that cleared an hour ago, so the board stops being a
statement about the world and people learn to ignore it.

**You do not manage Slack.** The pin board maintains itself — recording a status
change updates that incident's Slack message in place, automatically. There is
nothing here to pin, unpin, or react to; do not try, and do not hand-post state
changes. Your whole job in this SOP is to make the *incident status* true, and the
channel follows.


## Authenticate first

Every app API call goes through the `ops_mission_control_api` MCP tool — it
carries the gateway's own credential and always reaches this instance. Never
call the API over raw HTTP and never hunt for a credential — see SKILL.md
§ Calling the API for why. Paths below are relative to the app base, exactly
as the tool takes them.

## Pass 1 — signals that cleared

1. `GET /incidents` with `query="status=investigating"`, then again
   for `needs_human` and `dispatched`.
2. `GET /signals`. Read five things from the response:
   - **`firing`** — what is still firing (already state-filtered for you; do NOT use
     the raw `signals` list, which includes signals a provider reported as recovered).
   - **`cleared`** — signals a provider POSITIVELY reports as recovered.
   - **`suppressed`** — signals a *human* parked at the provider (an Alertmanager
     silence or inhibition, a maintenance window). Not firing, and not recovered.
   - **`errors`** — sources that contributed nothing this cycle *and why*.
   - **`poll_health`** — per source, whether its last poll actually succeeded.

   Compare on the signal **`id`** (e.g. `github-issues:cli/cli#14001`) — comparing
   titles will mismatch the moment a provider edits one.

3. **Absence is not evidence.** A signal missing from `firing` means one of three
   opposite things: it cleared, *we could not look*, or *this source cannot tell us*. A
   provider returning 429, a timeout, an expired token, or a storm that pushed the signal
   off the first page all read exactly like "resolved" if you only check for absence.

   So resolve an incident on absence **only when the poll for that signal's own source
   succeeded** — `poll_health["<source>"].ok == true`. If that source is absent from
   `poll_health`, is `ok: false`, or appears in `errors`, **leave every incident from it
   alone** and do not mention it as resolved. A source in backoff will answer on a later
   run; a wrongly-resolved incident will not come back, because `resolved` is terminal
   and recovery needs the alarm to fire again as brand-new work.

   **`ok` is necessary and not sufficient. Also require
   `poll_health["<source>"].snapshot != false`.** `ok` says we looked; `snapshot` says the
   result was a complete picture of what is firing. It is `false` for a source that
   delivers by PUSH into a spool — the inbound `webhook` source — where a signal leaves the
   spool as soon as an incident claims it and is absent from every cycle afterwards whether
   or not the fault is still live, because a push sender announces a fault once and never
   re-asserts it. So absence there is the normal steady state,
   not recovery, and resolving on it would close live work on a successful poll. For those
   sources, resolve only from an explicit entry in `cleared` (step 4).

   ```
   # Only for an incident whose source polled OK, or one listed in `cleared`.
   ops_mission_control_api(method="POST", path="/incident/transition",
     body_json='{"id": "INV-7", "status": "resolved",
                 "resolution": "signal cleared at the provider — no longer firing"}')
   ```

   A `409` means that transition is not legal from the incident's current state.
   Read the error; do not retry blindly.

4. A signal in **`cleared`** needs no health check — an explicit `ok` from the provider
   is positive evidence rather than an inference, so resolve those directly.

5. **A signal in `suppressed` must NOT be resolved.** A human parked it; nothing was
   fixed, so `resolution: "signal cleared at the provider"` would be false — and
   `resolved` is terminal, so the work does not come back when the silence expires.

   Do not silently leave it reading "still investigating" either, because that claims the
   agent is working something it will never pick up (dispatch claims only `firing`). The
   honest move is a note on the incident saying it is parked at the provider, naming
   `suppressed_by` when the provider published one, and leaving the status alone.

   Do not invent a status for this. There is no `STATUS_SUPPRESSED`, on purpose: the
   suppression is a fact about the *signal* at the provider and can be re-read on every
   poll, while a copy on the incident would go stale the moment the silence expired.
   The stronger rule in Pass 1 step 3 still wins — if the source did not poll, you know
   nothing about this signal at all, parked or otherwise.

## Pass 1a — an action this app took may not have landed

Read `verification` on each open incident (`GET /incidents`). It is `""` on almost
everything, which means no action was ever taken — that is not a problem to report.

- **`still_firing`** — the app executed an action, the provider accepted it, and the
  signal was still firing when the heartbeat looked again. This is the one verification
  state worth a line to the channel, because the board previously said the action was
  applied. Do NOT resolve the incident; the condition it was opened for is live. Say what
  was attempted (`last_action`) and that it did not hold.
- **`pending`** — the recheck has not come due yet. Say nothing; the heartbeat will fill
  it in.
- **`unknown`** — the recheck was due and the source could not be read. Say nothing about
  the action either way. A later cycle retries this by itself; "we could not look" is not
  a finding.
- **`not_checkable`** — the verb's outcome is not observable (an acknowledgement leaves
  an alert firing by design). Do not treat it as failed, and do not treat it as
  confirmed.

Never write `verification` yourself. It is produced by the heartbeat's own re-read, and a
hand-set verdict would be exactly the unearned claim the field exists to prevent.

## Pass 2 — a cleared signal is not always a fixed problem

Before resolving, check whether the incident already carries a diagnosis. A signal
that stopped firing because someone fixed it and one that stopped because it flapped
look identical from here. If there is no diagnosis and the signal cleared on its own,
say so in the `resolution` ("cleared without diagnosis — may recur") rather than
implying it was solved. A ledger entry written from a guess is worse than none,
because the next responder inherits it with confidence attached.

## Pass 3 — stale release

Incidents idle beyond the stale window are already released by the dispatch
heartbeat, so there is normally nothing to do. Verify none are stuck in `stale` while
their signal is still firing — that combination means work is being dropped, and it
is worth one line to the channel.

## Rules

- Cap at 10 incidents per run to stay inside provider rate limits.
- **Never resolve an incident whose source failed to poll.** See Pass 1 step 3 — this
  is the one rule in this SOP whose violation destroys real work rather than merely
  leaving the board untidy.
- **If nothing changed, exit silently.** No "board is clean" message.
