---
name: papyrus-make-fluent
description: Polish the English of a LaTeX passage — fluency, grammar, and clarity — as tracked suggestions, without changing structure or meaning. Use when the author asks to make text fluent/readable, fix the English, proofread, or tighten wording. Includes a mandatory global-consistency (ripple) check.
triggers: make fluent, polish my writing, proofread latex, fix english, tighten prose, rephrase paragraph
---

# Make Fluent

Polish mode: improve HOW it reads, never WHAT it says or WHERE it sits. Follow
`papyrus-writing` for conduct, the clarity principles (Gopen & Swan), and the
house style; produce every change as a tracked suggestion via
`papyrus-latex-suggestions` (paired with a comment giving the reason) — never
overwrite silently. This skill adds only what is specific to polishing: the scope
fence and chameleon-mode.

## Scope fence
- **MAY change:** grammar, spelling, agreement, articles, tense; awkward or wordy
  phrasing → tighter and clearer (topic/stress position, subject close to verb,
  old→new flow); hedging and redundancy.
- **MUST NOT change:** the **structure** — don't move, merge, split, add, or
  delete sentences/paragraphs (flag it instead); the **meaning** — never a claim,
  number, result, definition, or citation; the author's **voice** — match their
  terminology, notation, macros, and bib keys, don't flatten to generic prose;
  the **layout** — don't rewrap paragraphs you weren't asked to touch.

## Chameleon mode
Before editing, read a few passages the author has already written *fluently* to
learn their register and level of English, then match it — don't homogenize a
distinctive or non-native voice into generic prose. Read enough surrounding text
to keep terminology consistent, and keep every edit sentence-local and minimal
(one suggestion per issue).

## After polishing
Run the ripple / global-consistency check from `papyrus-writing` §7 — a local
rewording can desync the abstract, captions, notation, or cross-references. FLAG
anything affected and offer to propagate; never propagate silently.
