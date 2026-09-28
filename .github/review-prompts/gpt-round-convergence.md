
══════════════════════════════════════════════════════════════════
ROUND CONVERGENCE — adjudicated findings do not come back
══════════════════════════════════════════════════════════════════
If an ADJUDICATION LEDGER section is appended below, it lists
rulings a repository writer has already made on findings of THIS PR
(fixed, rebutted, or overridden). Rules, in order:
  1. Do NOT re-report a finding a recorded ruling covers. A ruling
     covers the exact instance it names, plus any variant its
     recorded RATIONALE equally applies to — a design-level
     acceptance of a tradeoff covers every instance of that
     tradeoff, wherever it moves. A covered variant may be reported
     as an advisory FINDING at most, never BLOCKING.
  2. A ruling does NOT cover an independent same-class defect at a
     different site whose existence its rationale never addressed —
     a single-site fix or rebuttal rules on that site only. That is
     a NEW finding; judge it normally.
  3. Exception: if THIS head materially changed the specific lines a
     ruling was about, the ruling is stale for those lines and the
     finding may be re-raised as new.
  4. The ledger only ever DOWNGRADES repetition that a recorded
     rationale covers (rule 1 is the one deliberate exception to
     the no-softening principle). It never waives a defect no
     ruling covers, and for anything outside covered repetition it
     is never a reason to soften a finding that meets WHAT BLOCKS.
  5. A DEFERRAL is not an adjudication for security, data-loss, or
     corruption defects. If a recorded ruling's rationale merely
     postpones such a finding (accepted-and-deferred, a follow-up
     issue, "fix later") without rebutting it as not-a-defect or
     showing it fixed, the ruling does NOT cover it: re-raise it at
     its original severity every round until it is fixed, rebutted,
     or human-overridden. Deferral covers advisory-class findings
     only.
Your review must CONVERGE: each round's blocking set must be a
consequence of what changed since the adjudications, not a fresh
re-litigation of the whole diff at a lower confidence bar.

