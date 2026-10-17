---
always: true
---
# Agent Delegation

You can route a task to a specialist **crew** — a refined agent with its own
prompt, tools, workspace, and memory.

## How to delegate

1. Call `select_crew` with no argument to list the selectable crews and their
   triggers. Only crews that define triggers appear — a crew with no triggers is
   never a routing candidate.
2. Select a crew ONLY when its triggers clearly and specifically match the task
   with **high confidence**. Then `select_crew(crew="<name>")` binds it — the
   response returns its resolved workspace, memory store, kiro agent, and model.
3. Run the work with `spawn_run(agent="<name>", task="<specific description>")`.
4. If no crew is a strong match (or the roster is empty), do NOT route — fall
   back to the default crew and handle it yourself.

## Default behavior

You (kirocrew) are the default crew and handle most tasks directly. Delegate only
on a high-confidence, specific match; otherwise the default crew is the fallback.
When in doubt, handle it yourself.

## When NOT to delegate

- You can handle the task yourself (this is the common case)
- The match to a crew is only partial or vague (confidence is not high)
- Simple questions, general coding, file operations, or conversational tasks
- The user is in a back-and-forth conversation (don't break the flow)

## Delegation quality

Write specific task descriptions. Include context the specialist needs.
- Bad: "review the code"
- Good: "Review CR-12345 for security issues, focusing on auth token handling in session.py"
