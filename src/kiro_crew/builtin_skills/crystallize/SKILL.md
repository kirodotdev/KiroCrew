---
name: crystallize
description: Capture the current session as a reusable skill — staged as a candidate by default, or live only when the user explicitly asks for a live/active skill.
triggers: crystallize, create a skill, create a skill from this, save this as a skill, make this reusable, turn this into a skill, create a live skill, create an active skill
---

# Crystallize a session into a skill

Use this whenever the user asks to capture work as a reusable skill —
"crystallize this", "create a skill", "save this as a skill", "make this
reusable", or "turn this into a skill". This is the **on-demand** counterpart to
the automatic post-session skill generation: the user is telling you *now* that
the work is worth keeping.

**Two modes, chosen from the user's wording:**

- **Candidate (default).** Stage the skill in the pending queue for human
  approval. Every phrasing above means this unless the user says otherwise.
- **Live (explicit only).** Write the skill straight to a live,
  immediately-loadable location, bypassing approval. Take this path **ONLY**
  when the user explicitly says "create a live skill" or "create an active
  skill" (or confirms it when asked). Never infer it from a plain "create a
  skill" — that stays a candidate.

## When to use

- The user says any trigger phrase above (candidate mode), or explicitly asks
  for a "live" / "active" skill (live mode).
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

3. **Write prose by default; add a script only when determinism earns it.**
   Most skills are judgment or workflow guidance and should be plain **prose
   steps** — that is the expected shape. Reach for a helper script *only* when
   part of the procedure is genuinely deterministic and error-prone to
   re-improvise: a fixed multi-command chain, a set API sequence, or a fiddly
   file transform. If prose captures it clearly, do not write a script. When a
   script truly is warranted, it must be **Python** (so it runs on
   macOS/Linux/Windows), must not access credentials, wipe files, or call
   unknown network hosts, and must stay under 4 KB. A staged candidate's script
   is statically validated and requires human approval before it can run; a
   live-mode script (step 4b) gets no such check, so you must hold it to these
   same limits yourself.

4. **Choose the destination — candidate by default, live only on an explicit
   request.** First resolve your KiroCrew skills directory — the SAME directory
   that holds the `auto/` group you inspected in step 2 (honor `$KIROCREW_HOME`
   if set; do **not** assume a literal `~/.kirocrew`, since migrated installs
   live elsewhere).

   **(a) Candidate — the default.** For "crystallize", "create a skill",
   "save this as a skill", "make this reusable" and every other phrasing, stage
   to the pending queue so a human approves before anything loads. Create
   `<skills-dir>/auto/.pending/<slug>/` (`<slug>` kebab-case, 3–60 chars) with
   `SKILL.md`:

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

   Always add a `.meta.json` next to `SKILL.md` — the pending list/detail API
   reads the candidate's `description`, `triggers`, `name`, and `source` from it
   (there is **no** SKILL.md-frontmatter fallback), so without it the candidate
   shows blank in **Skills → Pending review** and dedup loses its match data:
   `{"slug": "<slug>", "name": "auto/<slug>", "source": "crystallize",
   "created_at": "<ISO>", "description": "...", "triggers": "...",
   "has_scripts": <bool>, "scripts": [...]}`.
   Only `scripts/` is conditional: if you generated a script, put it under
   `scripts/<name>.py` in that folder, set `"has_scripts": true`, and list it in
   `scripts`; for a prose-only candidate use `"has_scripts": false, "scripts": []`.

   **(b) Live — ONLY on an explicit "create a live skill" / "create an active
   skill".** The user must actually say "live" or "active" (or confirm it when
   asked) — never take this path by inference. Write directly to a top-level
   live directory `<skills-dir>/<slug>/SKILL.md` — no `auto/` prefix, no
   `.meta.json`, no pending stage — using this frontmatter:

   ```
   ---
   name: <slug>
   description: <=150 chars, starts with a verb
   triggers: <3-8 comma-separated keywords/phrases>
   source: crystallize
   ---

   # <slug>

   ## When to use
   ...
   ## Steps
   ...
   ## Gotchas
   ...
   ```

   **Do not overwrite an existing skill:** if `<skills-dir>/<slug>/` already
   exists (a live or builtin skill), pick a different slug or ask the user —
   the live path has no collision guard, so writing blindly clobbers it. Put
   any script under `scripts/<name>.py` and, since no approval step runs for
   you, mark it executable yourself — on POSIX, `chmod +x`; skip that on
   Windows, where the executable bit is a no-op.

   In BOTH cases: do **not** include absolute paths, credentials, tokens, or
   user PII in the body or the script.

5. **Hand off.**
   - **Candidate:** tell the user it is staged and they can review it in
     **Skills → Pending review** — approve to make it live (and mark any script
     executable), or dismiss it. Nothing loads until they approve.
   - **Live:** tell the user it is active immediately and discoverable by its
     triggers (no approval needed), and point them at the file in case they want
     to edit or remove it.

## Gotchas

- **Default to the pending queue.** Only write directly to a live location
  (`<skills-dir>/<slug>/`) when the user explicitly asked for a "live" or
  "active" skill — otherwise always stage under `auto/.pending/` so a human
  reviews it first.
- One skill per distinct procedure — don't bundle unrelated workflows.
- Keep the description trigger-class-focused (it is matched on, and truncated
  in the system-prompt skill index).
