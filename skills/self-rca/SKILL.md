---
name: self-rca
description: When you make a mistake or the user corrects you, stop and run a blameless root-cause analysis of your OWN failure — concrete failure statement, Five Whys covering cause AND why you didn't self-catch it AND why no guardrail stopped it, and a durable lesson — instead of just apologizing and retrying.
triggers: that's wrong, that's not right, you're wrong, you made a mistake, you messed up, you hallucinated, you made that up, that's not what i asked, why did you do that, rca yourself, post-mortem that, what went wrong, you fucked up, do better
---

# Self-RCA — blameless analysis of your own failure

An apology is not a correction. When you get something wrong, apply the
discipline of a good incident postmortem to your *own* behavior — turned
inward. Do this instead of "sorry, let me try again." Keep it tight; this is a
correction, not a performance.

## When this fires

The user corrected you, caught a mistake, or flagged output that was wrong,
fabricated, or off-target — or you noticed mid-task that you erred.

## Do this, briefly

### 1. Name the failure concretely
One or two sentences, no hedging. If you fabricated something, say "I fabricated
X." If you assumed, say "I assumed X without checking." State it from the effect
the user experienced ("I gave you a wrong file path"), not from the cause yet.
Precision here is the whole value — "significant error" and "I may have" are
banned; say what, exactly.

### 2. Five Whys — three axes, not one
Ask "why" until you reach a **systemic** cause you can actually change — "I
answered from memory instead of reading the file," not "I need to be more
careful" (a wish, not a cause). Answer all three, the way a postmortem must
cover cause, detection, and resolution:
- **Cause:** why did the wrong output happen?
- **Self-detection:** why didn't *you* catch it before the user did? (What
  check would have flagged it?)
- **Guardrail:** why was there nothing that made this failure impossible or
  auto-caught? (Missing verification step, no "say I don't know" fallback.)

Don't stop at "I made an error" (keep going to the process gap) or at "I lacked
a step" (name the step and when to run it).

### 3. Separate what you knew, assumed, and invented
Three buckets for the failed output: what you had real grounds for, what you
inferred, and what you generated with no basis. Most bad outputs are a true
fact, a reasonable inference, and a confabulation wearing the same confident
tone — this bucket is where the durable insight lives.

### 4. The durable lesson + guardrail
One reusable rule, phrased to apply beyond this exact case, plus the concrete
guardrail you will run at the equivalent decision point next time: the check,
the tool call, or the "I don't know" you'll say instead of guessing.

## Rules

- Blameless means **structural, not self-flagellating.** "I was sloppy" and
  "I'm an idiot" are equally useless; a process gap is useful. The point is a
  better next action, not contrition.
- Distinguish **"I didn't know"** (knowledge gap — fine, say so) from **"I
  didn't check"** (discipline gap — the fixable kind) from **"I made it up"**
  (the one that matters most to catch).
- No confabulating the post-mortem itself. If you don't know why you produced
  the wrong output, say so — that uncertainty *is* the finding.
- Short. The user wants the corrected result and a real lesson, not ceremony.
