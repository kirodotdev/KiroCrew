# Skill evaluations

Measured checks on bundled skills, one directory per skill.

A skill is a prompt, and a prompt cannot be reviewed for correctness by reading
it. These harnesses answer two questions a code review cannot:

1. **Does the skill load when trigger matching is on?** Word-overlap trigger
   matching is **opt-in**: `skills.max_triggered` defaults to `0`, which disables
   it entirely, and the default discovery route is the Available Skills index plus
   `skill_search` / `$skillname`. For an install that has turned it on
   (`max_triggered > 0`), a skill auto-injects only when one of its `triggers`
   phrases clears `_MIN_TRIGGER_OVERLAP` (0.7) — and beautiful guidance behind a
   trigger that never fires is invisible dead weight for that whole population.
   This is deterministic, so it is a CI gate.

2. **Does the skill change the answer?** Each prompt is answered twice, once with
   the skill body prepended and once bare, and both are graded against explicit
   per-case assertions. This needs a model, so it is a local command, not a gate.

What this does **not** cover is the default path. There, a skill is found through
its `description`, which the generic bundled-skill frontmatter test already pins.

## Layout

```
evals/<skill>/cases.json      prompts, expected audience/behaviour, assertions
evals/<skill>/run_evals.py    --check (deterministic) and --run (A/B)
evals/<skill>/iteration-N/    A/B outputs and grading, auto-incremented, gitignored
```

## Running

```bash
# Deterministic validation. No model, no tokens. What CI runs.
python3 evals/explain-for/run_evals.py --check -v

# The A/B measurement. Needs an agent CLI on PATH.
python3 evals/explain-for/run_evals.py --run

# One case, skill lane only
python3 evals/explain-for/run_evals.py --run --test=2 --with-skill-only
```

`--run` re-runs `--check` first and refuses to spend tokens on an inconsistent
case set. The CLI defaults to `kiro-cli chat --no-interactive --trust-tools=`;
override with `EXPLAIN_FOR_EVAL_CLI`.

## Adding a case

Append to the `cases` array:

```json
{
  "id": 13,
  "name": "explain-oauth-partner",
  "prompt": "Explain this to my wife: why login broke on her phone",
  "audience": "Partner",
  "expect_trigger": true,
  "assertions": [
    "Analogy comes from a shared daily routine, not from software",
    "Explains that the phone held an expired key",
    "Warm and patient in register",
    "No jargon -- no 'token', 'session', or 'refresh'"
  ]
}
```

`audience` must match a row label in the skill's own audience tables, and the
prompt must clear the trigger threshold — `--check` fails the case otherwise
rather than letting it silently measure nothing.

Set `expect_trigger: false` for a **control**: a prompt that contains explanation
vocabulary but is not an audience request. Those pin the upper bound on trigger
looseness, so a future widened trigger fails a test instead of quietly re-pitching
ordinary questions.

Do **not** reuse a prompt the skill spells out in its own "Worked shapes" section.
The skill body is injected ahead of the prompt, so a duplicated example is already
answered in the with-skill lane's context: the lane wins on recall and tells you
nothing about unseen phrasing. `--check` fails the case for this, so the two files
cannot silently converge as either one is edited.

Four assertions per case works well. Write them as things a grader can point at
in the text, not as tastes.

## A longer trigger is LOOSER, not tighter

Worth knowing before editing a skill's `triggers` line, because it reads backwards.
Matching scores `|trigger_words ∩ message_words| / |trigger_words|` against a `0.7`
floor — the threshold is a *fraction of the trigger*, so adding words buys slack:

| Trigger length | Words that must appear | Effect |
|---|---|---|
| 1-3 words | all of them | exact; the phrase means what it says |
| 4 words | any 3 | one word is optional, and you do not choose which |
| 5 words | any 4 | same, one slot free |

So `explain it to my` never actually required `my`: `{explain, it, to}` alone scores
0.75 and fires, which matched "can you explain it to me" — an ordinary request with
no audience in it at all. Collapsing it to the 3-word `explain to my` makes the
possessive mandatory again and drops that prompt to 0.67. `break this down for` had
the identical flaw (bare "break this down" scored 0.75) and became `break down for`.

The rule: if one word in a trigger is the part carrying the meaning, the trigger has
to be short enough that that word cannot be the one dropped. Control cases are how
you find out you got it wrong.
