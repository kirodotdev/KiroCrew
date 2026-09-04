# Main Checklist — Design Critique

This is the atom-level rubric for Mode A. Each atom has a stable ID, evidence requirement, and
supporting framework. Record each applicable atom as `pass`, `severity 1-4`, or `not evaluated`.
Apply the global severity calibration in `../SKILL.md`; practitioner guidance is a diagnostic
prompt, not a universal requirement.

Legend: 🖼 `image-visible` · 🧩 `needs-html` · ▶ `needs-runtime` · 🔀 `needs-flow` ·
⇄ `needs-state-pair` (two states of the **same** view — before/after an action)

## Evaluation rules

- Observable evidence is required for every issue.
- A concern estimated from pixels is not a measured standards failure.
- Do not enforce a grid, type scale, target size, or component rule unless it is a relevant
  platform guideline or a supplied Mode B requirement.
- Consolidate one root cause into one finding; cite multiple atoms/frameworks as supporting
  badges without adding duplicate penalties.
- Do not penalize unprovided screens or states.

---

## 1. Visual design & hierarchy

### Typography

- **V01 🖼 Type hierarchy coherence** — Text treatments create a stable title/body/metadata
  hierarchy without unnecessary near-duplicates. Do not require a fixed number of sizes.
  *[Refactoring UI: hierarchy; Gestalt: similarity]*
- **V02 🖼 Emphasis channels** — Size, weight, color, and spacing reinforce rather than contradict
  the intended reading order. *[Refactoring UI: hierarchy]*
- **V03 🖼 Text measure** — Paragraph-length prose has a readable line length; roughly 45-90
  characters is a diagnostic range, not a universal rule for tables, labels, or dense tools.
  *[Practical typography]*
- **V04 🖼 Line height and density** — Leading supports the typeface, size, and content density;
  flag demonstrated reading friction rather than enforcing one ratio. *[Practical typography]*
- **V05 🖼 Typographic consistency** — Type families and styles have distinguishable roles and
  repeated roles use consistent treatment. *[Refactoring UI; Gestalt: similarity]*

### Spacing, grouping, and layout

- **V06 🖼 Spacing rhythm** — Repeated relationships use a coherent spacing rhythm. An 8-point or
  4-point grid is supporting vocabulary, not a Mode A requirement. *[Spacing-system practice]*
- **V07 🖼 Alignment** — Related elements share meaningful axes; exceptions appear intentional.
  *[Gestalt: continuity]*
- **V08 🖼 Grouping** — Proximity and common regions match logical relationships; unrelated items
  are not accidentally grouped. *[Gestalt: proximity, common region]*
- **V09 🖼 Focal hierarchy** — Visual weight leads attention toward the primary information or
  action for the stated task. *[Gestalt: figure/ground]*

### Color and surface language

- **V10 🖼 Color roles** — Repeated semantic roles use color consistently; flag role confusion,
  not palette size by itself. *[Refactoring UI: color roles]*
- **V11 🖼 Purposeful emphasis** — Accent and contrast support the intended hierarchy instead of
  creating competing focal points. *[Refactoring UI: hierarchy]*
- **V12 🖼 Repeated component appearance** — Equivalent controls and containers look equivalent
  unless their state or priority differs. *[Nielsen #4; Gestalt: similarity]*
- **V13 🖼 Shape, border, and elevation language** — Radius, border, and elevation differences
  communicate a role or state rather than appearing accidental. *[Visual-system practice]*

### Content legibility & detail-on-demand

- **V14 🖼 Critical-content legibility & detail-on-demand** — Content the user must read to act —
  above all the primary artifact, image, or media the screen is built around — is presented at a
  legible size. Detail-carrying images/media offer zoom or a larger view (lightbox); primary
  evidence is never shrunk into an unreadable thumbnail while usable space sits empty. Follow
  "overview → zoom → details-on-demand." Rate up to **3** when illegible content blocks
  comprehension of the core task. *[Shneiderman: visual information-seeking mantra; WCAG:
  legibility/resize; Aesthetic-Usability]*

Apply Gestalt—proximity, similarity, common region, continuity, closure, and figure/ground—as
supporting explanations across these atoms, not as a separate score.

---

## 2. Usability & interaction

### Assessable from a static frame

- **U01 🖼 Consistency and standards** — Controls use recognizable patterns and equivalent actions
  are presented consistently. *[Nielsen #4]*
- **U02 🖼 Match to users' language** — Labels, icons, and concepts fit the evaluation brief rather
  than exposing unexplained system terminology. *[Nielsen #2]*
- **U03 🖼 Recognition over recall** — Necessary options, context, and signifiers are available at
  the point of use. *[Nielsen #6]*
- **U04 🖼 Relevant minimalism** — Nonessential elements visibly compete with the stated task.
  Minimalism is not an aesthetic preference or a demand to remove useful information.
  *[Nielsen #8]*
- **U05 🖼 Affordances and signifiers** — Interactive elements look actionable and their likely
  action is understandable. *[Norman: affordances and signifiers]*
- **U06 🖼 Mapping** — Control placement and labels make their affected object or outcome clear.
  *[Norman: mapping]*

### Designer-facing lenses

- **U07 🖼 Choice complexity** — Simultaneous choices create evidenced decision friction; count is
  contextual and grouping can reduce complexity. *[Hick's Law]*
- **U08 🖼/▶ Target acquisition** — Target size and placement appear usable for the stated platform.
  Exact Fitts analysis requires target dimensions, a task, and a starting point; a screenshot
  supports only a concern. *[Fitts's Law]*
- **U09 🖼 Cognitive load and chunking** — Dense information lacks meaningful grouping or external
  memory support. Do not use “7±2” as a maximum number of UI items. *[Miller; cognitive load]*
- **U10 🖼 Convention fit** — A nonstandard pattern creates a learning cost without a visible
  benefit. *[Jakob's Law]*

### Runtime or flow evidence required

- **U11 ▶/🔀 System status and feedback** — Actions expose progress, completion, and current state.
  *[Nielsen #1; Norman: feedback]*
- **U12 ▶/🔀 User control and reversibility** — Users can leave, cancel, undo, or safely recover
  where the supplied task requires it. *[Nielsen #3]*
- **U13 ▶/🔀 Error prevention and recovery** — Constraints prevent likely errors and recovery
  explains the next step. *[Nielsen #5 and #9]*
- **U19 ▶/🔀 Destructive actions & data loss** — An action that destroys or loses something asks
  first, or can be taken back. Look for: delete / remove / revoke / cancel-subscription with **no
  confirmation and no undo**; a confirmation that doesn't say *what* will be lost or that it is
  permanent; **entered data lost** on back, refresh, timeout, or navigating away mid-form; bulk
  actions that hit more than the selection implies; the destructive button styled and placed exactly
  like the safe one beside it. Rate this by what is lost and whether it can be recovered — an
  unconfirmed permanent delete is the textbook **Catastrophe (4)**, not a Minor. If the flow wasn't
  supplied, do not assume the confirmation is missing — say you couldn't see it.
  *[Nielsen #5 error prevention; Nielsen #3 user control; NN/g destructive-action guidance]*
- **U20 🖼/▶ Help & documentation at the point of confusion** — When something is genuinely hard to
  understand, help is reachable *where the difficulty is*, not only in a separate manual: inline hints
  on unfamiliar fields, a "what's this?" next to a consequential choice, an example of the expected
  format, searchable docs one click away. Nielsen's tenth heuristic — and the one most often skipped.
  Judge it only where confusion is plausible for the stated user; a self-evident screen needs no help
  and cluttering it would be worse. *[Nielsen #10 help and documentation]*
- **U14 🔀 Ending quality** — The supplied flow's consequential moments and ending provide clear
  closure appropriate to the task. *[Peak-end rule; Shneiderman: closure]*
- **U15 ▶/🔀 Flexibility and efficiency** — Repeated expert tasks have appropriate accelerators
  without obscuring the default path. *[Nielsen #7]*

### State quality (only when the state is shown)

- **U16 🖼 Empty / first-run state quality** — When a blank, empty, or first-run state is shown, it
  orients the user (what this is), previews the value or output, offers a low-effort way to start
  (a sample/demo or a clear primary action), and uses the space purposefully — rather than a
  mostly-empty "nothing here yet" screen. A first-run screen is a prime onboarding moment.
  *[NN/g empty states; "blank slate" onboarding; Aesthetic-Usability]*

Critique error, loading, and success/"all-clear" states' quality here too when they are shown
(helpful, recoverable, clear feedback that uses the space well). Never report the absence of
states that were not supplied.

**Judge what the state is FOR, not how its disabled parts look.** On a first-run or empty screen the
primary action is often disabled and therefore muted, fields are blank, and counts read zero. That is
the state working correctly — it is not a hierarchy, contrast, or affordance defect. Critique whether
the state orients and starts the user (above); do not file findings about the visual weight of
controls that are simply switched off. If you need to judge the active styling, say you'd need the
filled/enabled state.

### Across a state change (needs two states of the same view)

A state pair is the **same view before and after an action** — empty vs filled, collapsed vs
expanded, idle vs loading. This is a different axis from a flow (🔀), which compares *different*
screens in a journey. Never guess at these from a single frame.

- **U17 ⇄/▶ State-change continuity** — When a view changes state, things that exist in both states
  stay put. Elements that persist keep their position, size, and alignment; what changed should read
  as one local change, not a new page. Flag when the whole layout re-flows, the primary action moves
  or changes alignment, or persistent framing (heading, explanation, divider) silently disappears —
  because the user then can't tell what their action actually did, and a control they had already
  aimed at is no longer under the cursor. Judge by **how far persistent elements move**, whether the
  **primary action** is one of them, and whether the real change is **buried** by everything else
  moving. Legitimate exceptions: a deliberate full-view change (navigating somewhere new), or a
  first-run→working transition that is announced and animated. Note: "layout shift" (CLS) is useful
  vocabulary, but a shift right after user input is **excluded** from the CLS metric — so report this
  as a usability problem, never as a Core Web Vitals failure.
  *[Change blindness (Rensink, O'Regan & Clark); Norman: gulf of evaluation; Gestalt: common fate,
  continuity; Fitts's Law — moving target; Nielsen #4]*

- **U18 ⇄/▶ Motion, transition & response timing** — Motion explains the change instead of
  decorating it: the transition connects what became what, its length suits the distance and
  importance, and it never delays the user's next action. Timing: feedback within ~0.1s reads as
  instant, keeping a step under ~1s preserves flow, and anything past ~1s needs visible progress
  (with the ~0.4s Doherty threshold as the target for interactive work). **Accessibility is
  assertive here:** non-essential motion must honour `prefers-reduced-motion`, and anything moving,
  blinking, or auto-playing for more than 5 seconds needs a way to pause, stop, or hide it.
  Requires runtime, a recording, or at minimum a state pair — **never infer motion or speed from a
  static screenshot**; say you couldn't see it instead.
  *[WCAG 2.2 SC 2.3.3 Animation from Interactions; SC 2.2.2 Pause, Stop, Hide; Doherty threshold;
  Nielsen response-time limits (0.1s / 1s / 10s); Material: container transform]*

---

## 3. Accessibility — evidence-bounded checks

- **A01 🖼/🧩 Text contrast** — From an image, report only a likely contrast concern. With declared
  colors or computed styles, calculate WCAG 2.2 contrast for SC 1.4.3. APCA may be reported as a
  separate supplemental computation; it is not a visual estimate and not the WCAG 2.2 AA test.
  *[WCAG 2.2 SC 1.4.3; APCA supplemental]*
- **A02 🖼/🧩 Non-text contrast** — Evaluate essential control boundaries, states, and graphics;
  exact conformance requires colors/computed styles. *[WCAG 2.2 SC 1.4.11]*
- **A03 🖼 Text legibility** — Flag evidenced difficulty caused by size, weight, typeface, or
  rendering. WCAG does not define a universal 16px minimum. *[Inclusive typography practice]*
- **A04 ▶ Resize and reflow** — Test text resizing and responsive reflow at runtime; do not infer
  conformance from source or one screenshot. *[WCAG 2.2 SC 1.4.4 and 1.4.10]*
- **A05 🖼/▶ Target size** — A screenshot can show a likely concern only when viewport/scale is
  known. Runtime measurement can test WCAG 2.2 SC 2.5.8's 24×24 CSS-pixel minimum and its
  exceptions. Apple 44×44 **points** is separate platform guidance, not a WCAG AA requirement.
  *[WCAG 2.2 SC 2.5.8; Apple HIG where applicable]*
- **A06 🖼 Color as the only cue** — Status, selection, required fields, or links rely on color
  without another visible cue. *[WCAG 2.2 SC 1.4.1]*
- **A07 🖼 Color-vision robustness** — Use an actual deutan/protan/tritan simulation when available.
  Without simulation or source colors, report only a concern about confusable color pairs.
  *[Inclusive color practice]*
- **A08 ▶ Focus visibility** — Exercise keyboard focus at runtime and inspect indicator visibility;
  HTML source alone cannot pass this check. *[WCAG 2.2 SC 2.4.7 and 2.4.11]*
- **A09 ▶ Keyboard operation and focus order** — Exercise controls and navigation at runtime.
  *[WCAG 2.2 SC 2.1.1 and 2.4.3]*
- **A10 🧩 Accessible name, role, value, and alternatives** — Inspect the rendered accessibility
  tree/DOM, not appearance alone. *[WCAG 2.2 SC 4.1.2 and 1.1.1; ARIA APG]*

Accessibility findings describe evidenced barriers. The report is not an accessibility audit or
certification unless the required technical and manual testing has actually been completed.

---

## 4. Content & language

- **C01 🖼 Label clarity** — Buttons, links, fields, and headings communicate their purpose in the
  task context. *[Content Design: clear]*
- **C02 🖼 Plain language** — Unexplained jargon or internal terminology creates a comprehension
  barrier for the stated users. *[Content Design; plain-language guidance]*
- **C03 🖼 Terminology consistency** — The same concept is named consistently within the supplied
  artifact or flow. *[Content Design: consistent; Nielsen #4]*
- **C04 🖼 Scannability** — Paragraph content or instruction blocks lack structure needed for the
  stated task. Do not demand headings for short, already-scannable copy. *[Content Design]*
- **C05 🖼/🔀 Recovery copy** — When an error is supplied, it explains what happened and the next
  useful action without blame. *[Content Design: useful; Nielsen #9]*
- **C06 🧩 Readability computation** — Use Flesch or similar only for enough sentence-based prose
  to make the result meaningful; never apply it to labels, fragments, or as the sole basis for
  severity. *[Readability analysis]*

Do not report subjective tone preferences, generic “make it shorter” advice, or copyediting that
does not improve comprehension, trust, or task success.

---

## Mode B — Conformance with supplied references

Evaluate literal assertions from supplied tokens, design systems, specifications, and project
conventions:

- **Token compliance (L1)** — color, spacing, typography, and shape values
- **Component correctness (L1)** — component, variant, state, prop, and composition usage
- **Layout correctness (L2)** — required elements and spatial relationships
- **Convention adherence (L1/L2)** — documented project-specific patterns
- **Flows (L2)** — supplied steps, branches, and outcomes
- **Other assertions** — motion, visualization, density, and responsive behavior

Use `conforms`, `deviates`, or `not verifiable` and quote the exact reference assertion. These
results are separate from Mode A severity scoring unless a deviation also creates an independently
evidenced usability issue.
