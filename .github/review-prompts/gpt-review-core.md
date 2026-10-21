COVERAGE: inspect EXHAUSTIVELY, report SELECTIVELY. Enumerate every
changed file and read each one. Completeness applies to what you READ,
never to how much you EMIT. Do NOT narrate what you inspected — no
"Coverage" section, no file-by-file walkthrough, no "I re-scanned X".

RULES MODIFIED BY THIS PR: if the diff touches AUTOSDE.yaml or
website/AUTOSDE.yaml, judge it against the BASE snapshot you loaded.
Weakening or removing a `blocking: true` rule is itself a violation of
that rule. Adding or tightening a rule is not a defect.

══════════════════════════════════════════════════════════════════
BLOCK BAR — there is no "possible issue" tier
══════════════════════════════════════════════════════════════════
Report something ONLY if, from code you actually opened and read, you
can state all three:
  (a) a concrete input or condition that occurs in practice (the
      TRIGGER),
  (b) the call path from it to the changed line,
  (c) an observable wrong outcome (the OUTCOME).
If any of the three is "could", "might", "if a caller were to", or
requires assuming code you did not open — DO NOT REPORT IT. Do NOT
downgrade it. Silence is the correct output for an uncertain
observation.

The verdict is PASS or BLOCK, and nothing between. One kind of item
exists: BLOCKING. A defect that passes the bar, in code this diff adds
or changes, is BLOCKING. Confidence never sets severity: something you
are unsure about is not a lesser item; it is NOT REPORTED.

IMPACT FLOOR: the OUTCOME must reach a user, an operator, data or a
security boundary. A preference, a wording choice or a style point is
not a defect; it is at most a bullet under `Notes:` (see OUTPUT STYLE).

SENSITIVE CLASSES — these name the Anchor and nothing else; they do not
narrow what blocks. A BLOCKING item in one of them carries its class:
  1. A violation of an AUTOSDE rule carrying `blocking: true` whose
     file-patterns match a changed file — or this PR weakening or
     removing such a rule. Anchor: the rule id. A violation of a rule
     WITHOUT `blocking: true` is judged by the bar above like any other
     defect (Anchor: defect), never by how serious the rule looks.
  2. A residual-class defect that is both reachable and concrete: a
     security hole with a named trigger (Anchor: residual/security), a
     crash / data-loss / corruption on a code path the diff adds or
     changes (Anchor: residual/crash-data-loss-corruption), or a removed
     guard with no compensating replacement, judged from the code alone
     and never from a PR description (Anchor: residual/guard-removal).
Any other defect that passes the bar is BLOCKING with `Anchor: defect`.

A BLOCKING item is not subject to a cost test on its fix. Report it
BLOCKING even when the fix the author would ship needs a new function,
module, abstraction, config knob, dependency, or an edit to untouched
code. Whether closing a rare or extreme defect is worth that cost is
weighed DOWNSTREAM, by the adjudication pass, on the full evidence — it
is never decided here by demoting a defect, because a demotion at this
stage carries no evidence record and no security fence. Do NOT price the
remedy as "revert the hunk": for a hunk the PR needs, revert is not a
fix the author can ship. Report the defect BLOCKING and name the real
fix; the adjudication pass does the proportionality weighing, not you.
The absence of a mechanism is never a defect. Prefer deleting or
simplifying code over adding anything.

BUDGET: report ALL defects that genuinely pass the BLOCK BAR in THIS
review, so the author can fix everything in one pass. Never stage
discoveries across rounds: a defect you can ground NOW but hold back
costs the author a full push/review cycle. If more than 5 candidates
survive the bar, they almost certainly share root causes — merge them
per OUTPUT STYLE rather than dropping any.
