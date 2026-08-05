---
cron: ops-mission-control/rotation-check
schedule: "*/5 * * * *"
tier: always
silent: true
---

# SOP: Rotation check

Arms and disarms the `on_shift` automation tier so nobody has to remember to turn
anything on when their rotation starts.


## Authenticate first

```bash
URL=$(kirocrew token 2>/dev/null | grep -oE 'http://[^ ]+' | head -1)
BASE="${URL%%\?*}"; TOKEN="${URL#*token=}"
```

Reuse `$BASE`/`$TOKEN` for every call below and pass `?token=$TOKEN`. Never hardcode a
port and never hunt for a token elsewhere — see SKILL.md § Calling the API for why.

## Steps

0. **Stop immediately if the app is not set up.** `GET
   /api/apps/ops-mission-control/state`; if no entry in `providers` has
   `configured: true`, produce NO output and stop. There is nothing to arm, and this
   job runs every 5 minutes — on a fresh install it must cost nothing at all.

   This job ships enabled because it is the only thing that resumes the `on_shift`
   tier: `dispatch` and `reconcile` both ship paused, and the tier is armed here. Ship
   this one paused and nothing ever fires no matter what the operator configures.
   (`ledger-hygiene` also ships enabled — it sits on the `primary` tier, which nothing
   arms from here. Its gate is in the ROUTE: `POST /ledger/hygiene` refuses with 409
   `not_primary` unless `rotation.is_primary()`, because the pass prunes a shared ledger
   and a duplicate prune deletes knowledge where a duplicate claim only wastes a turn.
   Naming the enforcement point matters: this line previously said the job "self-gates at
   runtime", which was true of no code, and an operator who trusted it had no reason to
   look for the missing check.)

1. `POST /api/apps/ops-mission-control/rotation/arm` — this does the arming. The route
   resolves the shift, computes the tier map, and pauses or resumes the app's crons to
   match. Returns `changed` (the crons it actually moved, `[]` when the live state was
   already correct) and `tiers`.

   **You do not choose which crons to pause, and you no longer hold `cron_pause` at
   all.** That is the point: off shift the armed set still contains
   `ops-mission-control/rotation-check` — this job — and pausing it strands the instance
   with no way to re-arm itself, silently ending the team's incident response until a
   human notices. That used to be prevented only by this SOP telling you not to, which
   is not an enforcement mechanism. The route now refuses to pause an always-tier job
   unconditionally, so one misread turn cannot cause it.

2. If `changed` is empty, **exit silently** — nothing moved.

3. Notify only on a genuine transition (shift started or ended), once. A
   five-minute poll that announced its own findings would post 288 times a day.

`GET /api/apps/ops-mission-control/rotation` remains available for reading `on_shift`,
`who`, `until`, `unknown`, `tiers`, `tier_crons` and `armed_crons` when you need to
explain a transition to the operator. It changes nothing.

## Rules

- `unknown: true` means the rotation source could not answer. **Do not infer arming
  from it — read `tiers.on_shift` and do exactly what it says.** The two sources
  answer an indeterminate case differently, on purpose: a rotation *API* reports
  `on_shift: true, unknown: true` (a network fault must never silently switch off a
  team's incident response), while a committed `rotation.yaml` reports
  `on_shift: false, unknown: true` (if the file cannot say this operator owns the
  shift, assuming they do is how every teammate ends up claiming the same alarm).
  `unknown` is an explanation for the operator, never an arming input.
- Arming is the route's job, not yours. Do not pause or resume this app's crons by hand
  even if you can reach a tool that would: the `always` tier includes this job, and
  pausing it is unrecoverable without a human.
- With no rotation source configured the default is always-on, so a solo operator
  gets continuous coverage rather than a tier that never fires.
