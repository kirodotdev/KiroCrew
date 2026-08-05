---
cron: null
tier: on_shift
silent: false
---

# SOP: Shift handover

Run this when a rotation changes hands, or when the operator asks "what do I need to
know?". It is **not** a cron: a handover is read by a person at a moment they choose,
and a scheduled one that nobody reads is exactly the noise this app exists to avoid.


## Authenticate first

```bash
URL=$(kirocrew token 2>/dev/null | grep -oE 'http://[^ ]+' | head -1)
BASE="${URL%%\?*}"; TOKEN="${URL#*token=}"
```

Reuse `$BASE`/`$TOKEN` for every call below and pass `?token=$TOKEN`. Never hardcode a
port and never hunt for a token elsewhere — see SKILL.md § Calling the API for why.

## Steps

1. Fetch the digest. It is computed fresh — there is no cached version to go stale:

   ```bash
   curl -sS "$GATEWAY/api/apps/ops-mission-control/handover"
   ```

2. It returns both a structured digest and a pre-rendered `text` field. **Post the
   `text` field.** Do not rewrite it: the wording is deliberate (the headline orders
   "nothing is watching" above "work is waiting on you" above the ordinary case), and
   an agent-reworded version drifts from what the dashboard shows for the same shift.

3. Add only what the digest cannot know, and only if you actually know it:
   - Anything you were told in chat this shift that is not on the board.
   - A judgement call you made that the next responder would reasonably reverse.

   If you have nothing to add, add nothing. A handover padded with restatements
   trains people to skim it.

## Reading it yourself

If you are the incoming agent rather than the outgoing one, read the digest before
touching anything, and treat it as a work queue in order:

1. **Waiting on you** — an unanswered approval or question from the last shift. Nothing
   moves until someone responds, so this is first.
2. **Stopped with no diagnosis** — the investigation ended and recorded nothing. There
   is no thread to resume; re-dispatch it rather than guessing at what happened.
3. **Recurring patterns** — entries marked `proven` may be applied directly. Anything
   else is a hypothesis to test; the digest labels it with its confidence and trust
   precisely so you do not treat it as an answer.

## Rules

- **Never invent a roster, an owner, or a ticket id.** The digest deliberately omits
  them because they are organization-specific, and a fabricated assignment is worse
  than an absent one.
- **Blind spots are part of the handover.** If `coverage.not_configured` is non-empty,
  say so — a quiet board with nothing configured is not a quiet shift, and the incoming
  responder inherits that gap silently otherwise.
- Report the autonomy mode. "It will tell you" and "it will act" are different shifts.
