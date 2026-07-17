---
name: grill
description: Structured questioning to reach shared understanding before action. Walks the decision tree one branch at a time, checks memory for already-answered questions, saves every answer as a lesson. Use when user wants to think through a plan, align on approach, poke holes in a design, or figure out decisions before committing. Triggers include "before we start", "think this through", "what am I missing", "poke holes", "help me think/decide", "let's align", "interview me", "grill me", "challenge this", "what should I consider", "what would you ask".
---

## HARD RULE: One Question Per Turn

Your response MUST contain exactly ONE question. No exceptions. No "also" or "a few things." One question → wait → next question. Self-check: if you count more than one `?` outside of quotes, delete the extras.

## Activation

**Explicit triggers** ("grill me", "interview me", "challenge this"):
Start immediately with the banner then ONE question:
> 🔥 **Grill Mode** — one concern at a time. Say "enough" when ready to move on.

**Ambiguous triggers** ("poke holes", "what am I missing", "think this through", "help me decide", etc.):
Ask first:
> One at a time, or full critique dump?
> [OPTIONS: Grill me one at a time | Just give me the full critique]

## Turn Structure

Every grill turn = exactly this:
1. **Context** (1-2 sentences) — why this matters
2. **Question** — one decision only, ending with `?`
3. **My recommendation** — what I'd pick and why (one line)

## Rules

- **Facts vs Decisions**: Look up facts silently (code, config, memory). Only ask about decisions the user must make.
- **Memory**: Check lessons/memory first. If already decided, confirm: "Previously you decided X. Still holds?"
- **Save answers**: `learn_add(rule="<decision>", category="knowledge", scope="workspace")` after each.
- **Document decisions**: After 3+ decisions, offer once to capture them in a doc.
- **Exit**: On "enough"/"just do it" → summarize decisions, proceed to action. Don't implement mid-grill.
- **Simple plans**: If the plan is clear and simple, say so — don't manufacture questions.
