---
name: crystallize
description: Turn the current session into a reusable skill candidate on demand, staged for approval
triggers: crystallize, crystallize this session, create a skill from this, make this reusable, save this as a skill, turn this into a skill
---

# Crystallize a session into a skill

Use this when the user explicitly asks to capture the current session as a
reusable skill — e.g. "crystallize this", "create a skill from this", "make
this reusable", "save this as a skill". This is the **on-demand** counterpart
to the automatic post-session skill generation: the user is telling you *now*
that the work you just did is worth keeping.

## When to use

- The user says any of the trigger phrases above.
- The session contains a **non-trivial, reusable procedure** — a multi-step
  workflow, a debugging path for a class of error, a fixed command/API
  sequence, or a research-synthesis flow — that a future session would benefit
  from.

Do **not** crystallize a trivial one-shot answer, a one-off failure, or a
session that touched credentials / sensitive paths.

## Procedure

1. **Reconstruct the procedure from the whole session — including sub-agents.**
   Read back over the conversation and, critically, parse any
   `[Subagent completion event]` messages: each carries what a sub-agent was
   tasked with and the working path it found. Fold those into the procedure so
   the skill captures the *successful* route, not the dead ends.

2. **Check for an existing skill first (cross-source dedup).** Look at the
   current auto-generated skills (Skills tab → the `auto/` group, or ask). If
   this procedure essentially duplicates one that already exists, **freshen
   that existing skill** instead of creating a near-duplicate — and if a
   consolidation pass would also capture this same session, don't stage a
   second copy.

3. **Decide prose vs. script.** If part of the procedure is genuinely
   deterministic — a fixed command chain, a set API sequence, a predictable
   file transform — author a small **Python** helper script (Python only, so it
   runs on macOS/Linux/Windows) so the result is repeatable rather than
   re-improvised. Keep judgment-based / context-dependent steps as prose.
   Scripts must not access credentials, wipe files, or call unknown network
   hosts, and must stay under 4 KB — they are statically validated and always
   require human approval.

4. **Write the candidate to the pending queue** (never live). First resolve
   your KiroCrew skills directory — it is the SAME directory that holds the
   `auto/` group you inspected in step 3 (honor `$KIROCREW_HOME` if set; do
   **not** assume a literal `~/.kirocrew`, since migrated installs live
   elsewhere). Then create `<skills-dir>/auto/.pending/<slug>/` where `<slug>`
   is kebab-case, 3–60 chars. Put the skill in `SKILL.md` with this exact
   frontmatter shape:

   ```
   ---
   name: auto/<slug>
   description: <=150 chars, starts with a verb
   triggers: <3-8 comma-separated keywords/phrases>
   source: auto
   session_key: <this session>
   created_at: <ISO-8601 UTC>
   ---

   # <slug> (auto-generated)

   ## When to use
   ...
   ## Steps
   ...
   ## Gotchas
   ...
   ```

   If you generated a script, put it under `scripts/<name>.py` in that folder.
   Add a `.meta.json` next to `SKILL.md`:
   `{"slug": "<slug>", "name": "auto/<slug>", "source": "crystallize",
   "created_at": "<ISO>", "description": "...", "triggers": "...",
   "has_scripts": <bool>, "scripts": ["<name>.py"]}`.

   Do **not** include absolute paths, credentials, tokens, or user PII in the
   body or the script.

5. **Hand off to the user for approval.** Tell them the candidate is staged and
   they can review it in **Skills → Pending review** — approve to make it live
   (and mark any script executable), or dismiss it. Nothing goes live until
   they approve.

## Gotchas

- **Never write directly to the live `<skills-dir>/auto/<slug>/`.**
  Always stage under `auto/.pending/` so a human reviews it first.
- One skill per distinct procedure — don't bundle unrelated workflows.
- Keep the description trigger-class-focused (it is matched on, and truncated
  in the system-prompt skill index).
