---
name: explain-for
description: Explain a topic, a piece of code, an error, or a design decision calibrated to one named audience — a 5-year-old, a 5th grader, a manager, a designer, a graduate student, a parent. Resolves who the explanation is for (from the request, or from what memory already records about that person), establishes the ground truth before simplifying, then tunes vocabulary, analogy source, tone, depth and framing to that audience instead of producing one generic explanation.
triggers: explain like i am, explain like im, explain like a, explain to my, break down for, dumb it down, simplify this for, in plain english
---

# explain-for — write it for one named audience

One explanation cannot serve everyone. This skill makes you name the audience
first, verify the facts second, and only then write.

## Step 0 — check that this is actually an audience request

Trigger matching here is word-overlap against the phrases above, so it is
probabilistic and it will sometimes fire on a request that merely contains the
word "explain". Before entering the skill, confirm the user wants an explanation
**pitched at somebody**. If they just want to know why CI failed, answer that
question normally and ignore everything below.

## Step 1 — resolve the audience

Pick exactly one audience.

**Check memory before guessing.** Crew carries a persistent user profile, and a
request like "explain this to my daughter" or "break this down for Priya" often
resolves against something already recorded — an age, a role, an interest.
Search memory and lessons for that person and use what is there; guessing an age
the profile already contradicts is the most visible way this skill fails.

If nothing resolves and no audience is named, do not silently invent one:

- Explicit smallest-audience phrasing ("dumb it down", "explain like I am")
  means **Age 5**. That is the request.
- Anything else with no audience — ask which audience in one line, or state the
  row you assumed in one clause so the user can correct it.

If the request names someone you cannot classify (a specific colleague, a title
absent from the tables), map them onto the nearest row and say which row.

### Ages

| Audience | Calibration |
|---|---|
| Age 5 | Smallest words. Analogies from toys, animals, candy, playground. One idea per sentence. |
| Age 10 | Elementary. Cause-and-effect is fine. Analogies from school, sports, video games. |
| Age 15 | Some abstraction is fine. Analogies from phones, games, social apps. Casual, never try-hard. |
| Age 20-30 | Direct and clear. Analogies from work, money, daily logistics. |
| Age 40+ | Respectful, unhurried. Analogies from home ownership, career, running a household. |

### Education level

| Audience | Calibration |
|---|---|
| 5th grade | Concrete examples, zero jargon. |
| Middle school | Introduce a term only with its definition attached. Step-by-step logic. |
| Senior high | Moderate complexity. Proper terms, each explained once. |
| College | Academic framing. Technical terms with brief context. Theory plus one application. |
| Graduate | Assume the foundation. Spend the words on nuance, trade-offs, edge cases. Be precise. |

### Job role

| Audience | They care about | Frame it as |
|---|---|---|
| Manager | Impact, timeline, risk, cost | The business outcome and the decision that is now open |
| Engineer | Mechanism, architecture, trade-offs | Implementation, performance, maintainability |
| Designer | The user's experience | Interaction, flow, accessibility, what the user perceives |
| Director | Strategy, ROI | Market position, resource allocation, the big picture |
| Product manager | User value, scope | Feature impact, what to build and what to cut |
| Colleague | Their own work | What changes for them and what they must know to collaborate |
| Support | Symptoms and remedies | How it fails, how to detect it, what to do about it |

### Relationships

| Audience | Tone | Analogy source |
|---|---|---|
| Partner | Warm, patient, conversational | Shared routines, household tasks |
| Parents | Respectful, never condescending | Technology they already use, home analogies |
| Kids | Playful, encouraging, short | Games, cartoons, animals, school |
| Friend | Casual, some humour | Pop culture, shared interests |

## Step 2 — establish the ground truth before simplifying

A simplified wrong answer is worse than no answer, and simplification is exactly
where wrong answers hide. This is the step that gets skipped.

- **Code** — read the actual files with your file and code-search tools. Do not
  explain from the identifier names. Trace what calls it and what it returns.
- **An error** — find the root cause, not the surface string. The frame that
  reports the failure is often not the one that caused it.
- **A concept** — state it once, precisely, for yourself before you soften it. If
  you cannot, you do not understand it yet.
- **An internal design or decision** — the claim and what it depends on. Search
  the knowledge library when the answer plausibly lives in a stored doc.

When the source material is large — a whole subsystem, a long log, a wide search
— delegate the reading to a sub-agent and have it return the distilled mechanism.
The explanation is short; the reading behind it does not need to sit in your
context.

If the ground truth is genuinely unknown, say so plainly at the audience's level
rather than inventing a clean story.

## Step 3 — write it

1. **What it is** — one sentence, no analogy yet.
2. **One analogy** — anchored in something this audience touches daily.
3. **The detail that matters** — only the layers this audience can use.
4. **So what** — why it matters to *them*, in their terms.

Language rules that flip with the audience:

- **Non-technical** (young, family, business): no jargon at all; define a term on
  the spot if it is unavoidable. Concrete beats abstract. Use "you".
- **Technical** (engineer, graduate): use the real terminology — omitting it
  reads as condescension. Spend the words on trade-offs and edge cases. Be brief;
  they already hold the context.
- **Business** (manager, director): lead with impact, quantify where you honestly
  can, skip mechanism unless asked, and end on the decision.

Length follows the audience: short for a child, dense for a specialist.

## Draw it when the thing has a shape

One picture really does beat a page of prose — but only when what you are
explaining *is* a shape. Ask that first, because the answer is not always yes:

| The thing you are explaining | Draw it? |
|---|---|
| A sequence, a flow, a lifecycle | **Yes** — order is what a diagram shows best |
| A hierarchy, a containment, a topology | **Yes** — nesting is hard to hold in sentences |
| A state change, or a before/after | **Yes** — put both states side by side |
| A number that moved, a share, a trend | **Yes** — one chart, not a paragraph of figures |
| A definition, a single fact, a judgement call | **No** — a box with words in it is not a diagram |
| A trade-off between two options | Usually no — the reasoning is the content, not the layout |

Scale it to the audience the same way you scale the words. For a child the
picture often *is* the explanation and the text is a caption. For a manager, one
before/after or one impact chart, never an architecture diagram. For an engineer
or a graduate, the real mechanism, drawn precisely. For a parent, draw the
analogy, not the system.

What is available, in ascending cost:

| Tool | Reach for it when | What it buys |
|---|---|---|
| ` ```mermaid ` fence | a sequence, flow, state machine, hierarchy or ER | cheapest by far; renders inline, click to enlarge |
| ` ```excalidraw ` fence | the drawing should look hand-made rather than official | informal register, full colour control from the scene |
| `<mcwidget>` | quantities, a colour-coded comparison, a real chart, a before/after panel, a small interactive probe | sandboxed iframe with **Tailwind preloaded**, the dashboard theme palette as CSS variables, and Chart.js / D3 from the allowed CDNs |
| an image file via `image-authoring` | the result must outlive the chat or be pasted elsewhere | a real file you can attach or save as an artifact |

Two rules that matter more than the picture:

- **A wrong diagram is worse than wrong prose.** It reads as authoritative and it
  gets screenshotted into other people's documents. Step 2 applies with more
  force here, not less: do not draw a mechanism you have not actually traced.
- **A diagram that restates the text adds nothing.** If the caption already says
  everything the boxes say, delete one of them. The picture should carry the part
  the sentences were bad at.

### Colour it, and make every colour mean something

**The default is deliberately flat, so a plain diagram will look plain.** In chat,
mermaid is initialised with one accent seed plus greys — contrast-safe on any
theme pack, but it gives every node the same fill. Colour has to come from the
source: `classDef` for a role you reuse, `style` for a one-off.

```
flowchart LR
    classDef happy fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef bad fill:#fee2e2,stroke:#dc2626,color:#7f1d1d
    classDef focus fill:#fef3c7,stroke:#d97706,color:#78350f

    A[request] --> B{cache hit?}
    B -->|yes| C[serve instantly]:::happy
    B -->|no| D[fetch from origin]:::focus
    D -->|origin down| E[error]:::bad
```

Two rules keep that from turning into decoration:

- **A colour is a claim.** Green means this path is fine, red means it fails,
  amber means this is the part under discussion. Colour used only to look lively
  sends the reader hunting for a meaning that is not there, which is worse than
  one flat palette. If you cannot say what a colour *means*, leave the node
  default.
- **Pick values that read on light AND dark.** The same source renders under both
  themes, and the host pins mermaid's theme precisely because a theme pack can
  set an accent that disappears into the background. Mid-tone fills with a darker
  stroke and an explicit `color:` survive both; near-white and near-black fills do
  not.

Scale it like everything else: for a child colour is doing real work, separating
the parts before the labels can be read. For an engineer, colour only the two or
three nodes that carry the point. For a manager, colour the outcome, not the
machinery.

### Reach for a widget when the content is quantities or comparison

`<mcwidget>` is not only the interaction escape hatch, and it is under-used for
explanations. It is the right call whenever the honest answer is a **chart, a
colour-coded table, or two states side by side** — things markdown cannot
express. Inside one you get Tailwind with no setup, Chart.js or D3 from the
allowed CDNs, and the dashboard's own palette as CSS variables (`var(--accent)`,
`var(--ok)`, `var(--warn)`, `var(--danger)`, `var(--card)`, plus the `-subtle`
tints).

Use those variables instead of hardcoded colours so the widget matches whatever
theme the reader is on — that is why they are injected. Load the `widgets` skill
before emitting one; it carries the format rules and the full variable list. Keep
the body to a few KB and write a file instead once it grows past that.

Still prefer markdown when markdown suffices: a widget wrapping three bullet
points is overhead, not richness.

## Delivery in Crew

- **An explanation request lifts the ban on explaining, not the length bound.**
  Never flatten an Age-5 or Age-10 explanation into one clipped jargon line
  because a verbosity block says to — the register is what this skill is for, and
  every level keeps it. Length is the other axis and it stays with the active
  level: `answer_only` holds its few-plain-sentences bound unless the user asked
  for depth (a doc, a walkthrough, in detail), and it pins its own replies to the
  Age 10 row above, borrowing the calibration rather than a length licence.
  `concise` and below: write what the audience needs.
- **Persist what gets forwarded.** An explanation written for a manager, a
  director or a customer usually gets pasted somewhere else. Save it as an
  artifact so it outlives the chat scrollback and can be revised, instead of
  regenerating it next week from a different mood.

## Rules

- Never talk down. An Age-5 explanation should feel delightful, not diminishing;
  a manager explanation should leave them able to decide, not sidelined.
- For code, explain the **purpose** before the **mechanism**. Nobody needs the
  syntax before they know why the code exists.
- Ruthless simplification is allowed and often correct: the core idea at 80%
  accuracy beats a 100% accurate explanation the audience abandons. What is not
  allowed is a simplification that inverts the meaning or invents a mechanism
  that does not exist.
- Do not stack two audiences into one answer. If the user needs both, write two
  clearly labelled explanations.
- Keep the user's language, analogies included. A translated American analogy
  lands as noise — pick one from the audience's own daily life.

## Worked shapes

**"Dumb it down: what is a race condition"** — Age 5. Two children reach for the same
crayon at the same moment. Who ends up holding it depends on whose hand was
faster that second, so the picture comes out different every time you try. Nothing
is broken; the order just is not decided.

**"Explain this to my mother: why her login keeps signing her out"** — Parents.
Check memory first for what she already uses. Then: the app keeps a pass that is
deliberately short-lived, like a parking receipt that runs out, and it asks for the
password again rather than trusting an old one. Respectful, no "token", no
"session".

**"Explain to a graduate student why we picked at-least-once delivery"** — Graduate.
Use the real terms; skipping them here reads as condescension. Spend the words
where the actual content is: the duplicate-delivery obligation this pushes onto
every consumer, and why idempotency became the caller's problem rather than the
broker's.

Note the second one: the audience is a person, so the audience is looked up before
it is guessed, and the analogy is drawn from her life rather than translated from
somebody else's.
