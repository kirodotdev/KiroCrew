---
name: cron-cost-optimize
description: Audit the cron jobs a user already registered and recommend a cheaper execution mode for each one, either a zero-token script or command job, or an agent job with minimal context. Use when the user asks why their scheduled jobs cost so much, wants to cut cron or automation token spend, asks which crons could be scripts, or is porting existing scheduled jobs into Kiro Crew and wants cost-efficient defaults. Read-only by default, and it never edits a job on its own.
triggers: cron cost, cron token cost, expensive cron, cron spend, optimize my crons, optimise my crons, scan my crons, cron audit, script mode, zero token cron, minimal context, cheaper cron
---

# Optimize what registered cron jobs cost

A cron job that dispatches to the model pays for its whole injected context on
every wake, whether or not that wake had anything to do. Two cheaper modes exist
and neither reaches a job that was already registered without them.

| Mode | What it costs | What it gives up |
|---|---|---|
| `script` or `command` job | no model turn at all | all reasoning, and the carried previous result |
| agent job with `minimal_context` | a model turn on a small context | memory, lessons, steering, skills, prior session history |
| plain agent job | a model turn on the full context | nothing |

Your job in this skill is to move each of the user's jobs to the cheapest mode it
can safely use, and to leave alone the ones that genuinely reason.

## Run the scan first

The scan is deterministic, so it costs no inference. Run it before forming any
opinion.

It reads nothing from disk. Two steps, and the first one matters:

1. Call `cron_list` with `json` set to true. That is the only reader that can see
   what the scan needs. The job store is scoped to the jobs your session owns, and
   the run history is readable only by the gateway, so a script cannot fetch either
   itself.
2. Write that JSON to a file and pass it in. Do NOT put the document on the command
   line -- a prompt inside it containing backticks or `$(...)` would be executed by
   the shell.

```bash
python3 <this skill's dir>/scripts/cron_cost_scan.py --input <the file you wrote>
```

Resolve `<this skill's dir>` from wherever you loaded this SKILL.md, rather than
hardcoding a path. The data home moves with `KIROCREW_HOME`, so a hardcoded
`~/.kiro/crew/skills/...` is wrong for some users.

Useful flags:

- `--json` for machine-readable output when you need to fold the result into a table.
- `--min-runs N` to change how many recorded runs count as evidence. Default is 5.

To look at one job, narrow the `cron_list` call with its `ids` instead of filtering
afterwards.

`cron_list` answers a plain sentence rather than JSON in three real cases: an empty
registry, no jobs owned by this session, and a caller the gateway cannot identify.
The script says which one it got. Relay it as-is; none of the three is an error.

## When the run history is missing

The report can say the run history could not be read. Take that seriously and pass
it on, because every verdict then rests on the prompt's wording alone, which is the
weaker signal. The usual cause is that no gateway is serving on this host, so the
one reader that can see the history is not there to ask.

## Read the verdicts

The scan emits one of four verdicts per job.

**`already-zero-token`** is a `script` or `command` job. Nothing to do. Do not
suggest changes to it.

**`move-to-script`** is deterministic work. The confidence field matters here
more than the verdict does, because the evidence behind it differs:

- `high` means the prompt reads as a mechanical check AND the run history shows the job never actually reasons.
- `medium` means the history shows a job repeating itself even though the wording is not obviously mechanical. Read the prompt yourself before agreeing.
- `low` means there is not enough run history yet. Say so to the user rather than presenting it as a finding.

**`enable-minimal-context`** still needs a model but not the full context. This is
the safest recommendation in the set and usually the one to lead with, because it
changes no job logic. It is also the right answer for a job you cannot turn into a
script.

**`leave-as-is`** either already uses the cheapest safe mode, or has a blocker.
Read the blocker line and pass it on. Do not argue with it.

## Never apply a change without asking

Both changes alter what a job can see at run time, so recommend and let the user
decide. Two specific traps to state plainly when they apply.

A script rewrite drops the carried previous result. An agent job with
`persistent_session` gets its last run's output prepended to the next prompt, and
that is how many jobs avoid re-reporting the same thing. A script receives no such
carry, and the job object inside a cron script exposes only its id and its message.
So a job whose dedup depends on the model recognizing "I already said this" will
silently start repeating itself after the rewrite. The scan flags this as a note.
When you see it, the dedup has to be rewritten as state the script writes and reads
itself, usually a marker file or a stored timestamp.

`minimal_context` injects no skill at all. A job whose prompt drives a skill, by an
inline `$name` token or by naming one in prose, stops working. The scan blocks the
verdict in that case, but check the prompt yourself if you are unsure.

## The scan only reads English prompts

Every blocker the scan applies is an English word list, so a prompt written in
another language matches none of them. Read naively that looks like a clean bill of
health, and it points at the dangerous verdict: a Chinese job asking the model to
summarize and decide would be cleared for a script precisely because the blockers
could not see it.

The scan therefore refuses instead of guessing. A prompt it cannot read comes back
`leave-as-is` with the blocker `the prompt is in a language this scan cannot read`,
the same refusal it uses for a prompt truncated before it arrived. When you see it,
read that prompt yourself and judge it. Do not treat the refusal as a verdict that
the job is already optimal.

A Latin-script language passes the readability test and is judged on the English
word lists anyway, so a German or French prompt can come back with an empty blocker
set that means "no English blocker matched" rather than "nothing blocks". Read those
prompts yourself too before relaying a cheaper mode.

## Applying an accepted recommendation

Once the user agrees, use `cron_update` on that job id, or point them at the
dashboard Schedule page.

For `enable-minimal-context`, set `minimal_context` to true. For a polling job that
also floods the chat, `hide_in_chat` is a safe companion change because it affects
presentation only. Leave `persistent_session` alone unless the user asks, since
turning it off removes the carried result exactly as a script rewrite would.

For `move-to-script`, the job cannot be converted in place: `cron_update` does not
accept a `script`, so a replacement job has to be created and the original retired.
Do it in THIS order, because the obvious order leaves both jobs running and a job
with side effects then does its work twice:

1. Write the script to a file under `~/.kiro/crew/crons/`.
2. Dry-run it with `kirocrew cron preview` before it is registered anywhere.
3. **Pause the original** with `cron_pause`. Do this BEFORE creating the
   replacement, not after. Pausing first means there is never a moment when both
   the old job and the new one are live, and it leaves the original recoverable
   with `cron_resume` if the next step goes wrong.
4. Register the replacement with `cron_add` using its `script` parameter. For a
   polling job also set `persistent_session` to false and `hide_in_chat` to true,
   since a zero-token job has no conversation worth keeping and usually nothing
   worth posting.
5. Verify the replacement actually works, with `cron_trigger` on the new job id.
   The script gate can refuse a body at registration time, and a script that
   registers can still fail on its first real run.
6. Only then remove the original with `cron_remove`. Tell the user this step
   happened, and name the id you removed.

If step 5 does not produce the result the original produced, resume the original
and stop. Two jobs paused-and-broken is a worse outcome than one expensive job
that works.

Four rules the script gate enforces, so write to them from the start:

1. A cron script reads its arguments as `ctx.message`, delivers with `ctx.notify()`, and controls the job by raising `Skip()` to retry quietly, `Done(msg)` to deliver and remove the job, or `Report(msg)` to deliver and keep it.
2. The script body is scanned before it is stored. Never spell a credential directory name or a protected secret environment variable name anywhere in the file, including inside comments and docstrings, because the scan is a plain regex over the source text and a mention in a comment is refused the same as a real read.
3. Do not reach a third-party host over plain HTTP from the script. Use the tool surface instead.
4. Prefer a `script` over a `command`. The command gate refuses substitution, brace expansion, positional parameters, loop keywords and unresolved variables, so anything with real logic in it belongs in a script whose body is scanned in full.

## Reporting back

Lead with the count of jobs that can move and the change for each, not with the
methodology. Give real numbers from the scan, which reports runs and results rather
than estimates.

One number needs a caveat. The product's own estimate for a minimal-context wake is
roughly 200 tokens against 30,000 to 55,000 for a full one. That range is an
estimate written into the product, not a measured benchmark. If the user is putting
it in a cost document, say so.
