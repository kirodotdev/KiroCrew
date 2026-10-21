---
title: Living UI — style-neutral motion, focus and readout rules plus a small toolkit for widgets and dashboards
status: implemented
author: iamwhatever
created: 2026-10-18
last-audited: 2026-10-21
audited-at: 0b89e0c6f7
doc-pr: 21797
implementation-prs: [21897, 21840, 22107, 22225, 22441, 22093, 22240, 21874, 22117, 22091, 22231]
tracking-issues: [21796]
supersedes: []
superseded-by: []
---
# RFC: Living UI

- Status: implemented. The maintainer iamwhatever accepted this RFC and asked
  to mark it implemented on 2026-10-21. Every required §11 row has landed:
  rows 1 and 2 in [#21897](https://github.com/kirodotdev/KiroCrew/pull/21897),
  whose squash merge carried row 1, with review fixes to `SKILL.md` in
  [#21840](https://github.com/kirodotdev/KiroCrew/pull/21840); row 3 in
  [#22107](https://github.com/kirodotdev/KiroCrew/pull/22107); row 4 in
  [#22225](https://github.com/kirodotdev/KiroCrew/pull/22225); row 5 in
  [#22441](https://github.com/kirodotdev/KiroCrew/pull/22441); row 6 in
  [#22093](https://github.com/kirodotdev/KiroCrew/pull/22093), plus
  [#22240](https://github.com/kirodotdev/KiroCrew/pull/22240) (the harness's
  Dashboard page mode, §7 step 1) and
  [#22231](https://github.com/kirodotdev/KiroCrew/pull/22231) (the example fix
  for [#22133](https://github.com/kirodotdev/KiroCrew/issues/22133)); row 7 in
  [#21874](https://github.com/kirodotdev/KiroCrew/pull/21874); row 8 in
  [#22117](https://github.com/kirodotdev/KiroCrew/pull/22117); row 10 in
  [#22091](https://github.com/kirodotdev/KiroCrew/pull/22091). Row 9 (measure,
  then decide on injection) is optional and not done, so §12 Q2 stays open.
  Verified at `0b89e0c6f7`.
- Author: iamwhatever
- Created: 2026-10-18
- Measured at: `9d088c85ad` (main).
- Related: `rfc-crewmate-dynamic-dashboard.md` (owns the Dashboard tab and its
  frame), `rfc-crewmate-dashboard-upkeep-skill.md` (a skill whose text must reach a
  crewmate with no file tools, the same constraint §6.4 meets).
- Inspiration: the MIT-licensed agent skill
  [anatomy](https://github.com/wheresryan22/anatomy) (`SKILL.md`,
  `references/craft.md`, `references/motion.md`, `references/verify.md`,
  `references/walkthrough.md`). §9 covers attribution.
- Tracks [#21796](https://github.com/kirodotdev/KiroCrew/issues/21796).

## 1. Summary

Agents in Kiro Crew draw two kinds of live HTML: inline `<mcwidget>` explainers in
chat, and the one-page Dashboard tab a crewmate writes for its person. Both are
correct and themed today, but most of them sit still and read like a form. This
RFC proposes **Living UI**: a way to make them pleasant to look at and easy to
follow in the first second, without telling anyone what colour, font or shape to
use.

It has three layers:

| Layer | What it is | Says anything about style? |
|---|---|---|
| 1. Rules | how motion feels, where the eye goes, where words go, how to check it | no |
| 2. Toolkit | a dependency-free script, at most 5 KB minified, copied inline as readable code | no: reads theme variables only |
| 3. Looks | optional drawing styles, such as an isometric "machine" look | yes, but only when the user asks |

All three live in one new built-in skill, `living-ui`. The `widgets`, `dashboard`
and `explain-for` skills point to it. Theme packs keep owning the look.

## 2. Motivation

### 2.1 What existed before this RFC

Verified at `9d088c85ad`:

- `src/kiro_crew/builtin_skills/widgets/SKILL.md:214` ("Animation") is the only
  motion guidance agents get. It says *when* to move (the freeze-one-frame test and
  a table of content kinds) and five hygiene rules (play/pause, reduced motion,
  stop when done, `requestAnimationFrame` plus `devicePixelRatio`, carry every
  value). It says nothing about *how* motion should feel, what the reader should
  look at, or how to check it.
- `src/kiro_crew/builtin_skills/dashboard/SKILL.md` is 267 lines, not the ~76 the
  task brief assumed. It covers the save/apply flow, honesty rules, the five
  manager questions and the frame contract. Its only motion line is
  `dashboard/SKILL.md:246`: "Motion off under `prefers-reduced-motion`."
- `src/kiro_crew/builtin_skills/explain-for/SKILL.md:123` ("Draw it when the thing
  has a shape") offers mermaid, excalidraw, `<mcwidget>` and image files. Its
  advice is about whether to draw and how to colour. It never suggests drawing the
  analogy as a working object.
- `src/kiro_crew/builtin_skills/frontend-design-workflow/SKILL.md` is a process for
  changing a product's own UI in a repository: mockups first, then code, then
  evidence. It does not cover runtime widgets.
- `skills/kirocrew-app-dev/SKILL.md` (a repo skill, not built-in) tells app authors
  to use host components and theme tokens ("Don't Reinvent the Dashboard").
- Screenshots exist: `web-verify` captures loopback pages with `playwright-cli`,
  `agent-browser` or scripted Playwright and caps frames at 2000 px;
  `browser-recording` makes videos. Neither knows about widget bodies, theme
  variants, reduced motion or slowed time. `dashboard_save` runs the page once
  offline in a headless browser (`src/kiro_crew/dashboard_verify.py`) and reports
  script errors only, no pixels.

### 2.2 The gap

| Concern | `widgets` / `dashboard` / `explain-for` before this RFC | anatomy |
|---|---|---|
| When to move | freeze-one-frame test | same idea: motion carries the model |
| How motion feels | not covered | exponential follow, τ 0.1–0.3 s; springs that overshoot once; event envelopes, 40–120 ms attack, slow decay; `dt` clamped to 1/30 s |
| Where the eye goes | "a colour is a claim" (explain-for) | exactly one lit accent; everything else steps down one tone |
| Words | free | title, hint and live readout in fixed card corners; legend and caption below; no labels in the drawing |
| Live numbers | widget values are static HTML | one pure model drives parts, readout and aria; the readout reads where parts *are*, not their target |
| First glance | nothing invites a look | idle tour: starts ~1.4 s in view, pauses on hover/focus/press, resumes ~3.6 s after input, drives the same targets as the user |
| Showing parts | not covered | exploded view: staggered springs ~35 ms apart, reverse order to close, selected part lit |
| Offscreen | not covered | `IntersectionObserver`, newest entry, loop stops |
| Reduced motion | "show an equal still version" | still but still interactive: jump to targets, no tour |
| Screen readers | not covered | `aria-valuetext` written on a state change, never per frame; a polite live region for phases |
| Checking it | `web-verify` for product UI | desktop and 390 px stills, hover and key shots, slowed contact sheets |
| Shared code | each widget hand-rolls its loop | one small kit |

The result today is widgets that are right but flat. A person skims past them. The
gap is not colour or decoration, which theme packs already own. It is behaviour.

## 3. Goals

1. A reader is drawn in at first glance and can follow what moves.
2. No visual style is imposed. Colour, font and shape stay with the theme pack, the
   user's request or the author.
3. One source of truth for the rules and the toolkit.
4. Works inside both existing frames with no change to their security policy.
5. Every agent can use it, including a crewmate with no file tools.
6. Accessible by construction: keyboard, reduced motion, screen readers.

## 4. Non-goals

- **Not a chart library.** Chart.js and D3 stay the answer for charts.
- **Not a theme.** The toolkit has no colours, fonts, radii or shadows.
- **Not mandatory animation.** The widgets skill's freeze-one-frame test stays the
  first gate. Living UI says how to move *once motion is justified*, never that
  something must move.
- **Not a framework.** No virtual DOM, no components, no build step for authors.
- **No change to the widget or dashboard CSP**, and no new network reach.
- **Not the isometric look by default.** That look is opt-in (§5.3).

## 5. Design

### 5.1 Layer 1 — the rules (style-neutral)

The `living-ui` skill states these rules. None names a colour, font or shape.

**Gate.** Apply the widgets freeze-one-frame test first. If a still frame carries
the meaning, stop: Living UI then only governs focus and words (rules F and W),
not motion.

**M — Motion feels like a real thing.**
- Time-based: `dt` from `requestAnimationFrame` timestamps, clamped to 1/30 s.
- Input sets a *target*; the value follows it, never snaps. Exponential follow with
  τ 0.1–0.3 s, or a spring.
- A spring overshoots once and settles. Use a critically damped settle where
  overshoot would lie (a count, a cost).
- A one-shot event (an approval arrives, a run fails) is an envelope over its age:
  attack 40–120 ms with zero slope at onset, slower decay. Never set a value to 1
  in one frame.
- One loop per figure. It stops when everything has settled.

**T — Truth comes from one model.** One pure state object drives the moving parts,
the visible readout and the accessible value. The readout reads where parts *are*
(the eased value), not where they are going, and takes a phase word and its number
from the same instant.

**F — Focus is one thing.** Exactly one element is lit, using the theme's
`--accent`. Every other element steps down one tone (lower opacity, or the muted
theme variable). Hover, keyboard and the tour all move that one focus.

**W — Words stay out of the drawing.** Title, hint and live readout sit in fixed
corners of the card. Legend and caption sit below it. The drawing holds no
labels, except at most one small label on a part.

**L — Life without nagging.**
- Explainers may run an idle tour: start about 1.4 s after the figure is in view,
  pause on hover, focus or press, resume about 3.6 s after the last input, and
  drive the same targets the user would. The widgets rule "stops when it is done"
  still holds: the tour plays at most three loops, then rests, and a visible
  play/pause control restarts it.
- Dashboards are watched all day (the widgets table already says "No" to motion
  there). On a dashboard, motion happens **on change only**: a bound value follows
  to its new number, a new item enters with an envelope, then the page is still.
  No idle tour on a dashboard.
- The loop stops when the figure is offscreen (`IntersectionObserver`, reading the
  newest entry, `rootMargin: "120px 0px"`).
- Under `prefers-reduced-motion: reduce` every value jumps to its target, there is
  no tour and every envelope is 0. The figure still answers input.

**X — Exploded view (optional).** Where showing parts helps, hover or a key opens
the figure: one spring per part, starts staggered ~35 ms apart, closing in reverse
order, a reversal re-staggers only the parts whose target changed, and the
selected part is lit (rule F).

**V — Verify before shipping** (§7): desktop and 390 px stills, reduced motion, a
slowed contact sheet, and both light and dark themes.

### 5.2 Layer 2 — the toolkit (style-neutral)

A single dependency-free script exposing one global, `LivingUI`. Each helper is
independent, so an author copies only the helpers they use.

```js
// Loop: one rAF loop per stage. dt is clamped to 1/30 s. Stops offscreen
// (IntersectionObserver, newest entry) and when tick returns false.
// calm is true under prefers-reduced-motion and follows its change event.
LivingUI.loop(stage: Element, tick: (dt: number, calm: boolean) => boolean)
  -> { kick(): void, stop(): void, readonly calm: boolean }

// Easing primitives. Each returns the new value or whether it is still moving.
LivingUI.follow(x: number, target: number, dt: number, tau = 0.2, calm = false) -> number
LivingUI.spring(s: { x: number, v: number }, target: number, dt: number,
                k = 420, c = 26, calm = false, substeps = 4) -> boolean
LivingUI.settle(s: { x: number, v: number }, target: number, dt: number,
                seconds = 0.4, calm = false) -> boolean   // critically damped
LivingUI.smooth(a: number, b: number, x: number) -> number // smoothstep
LivingUI.burst(age: number, rise = 0.08, fall = 0.13) -> number // 0..1 envelope

// Idle tour: drives the same targets as the user. Pauses on pointer, focus and
// key input inside stage; resumes resume ms after the last one; at most loops.
LivingUI.tour(stage: Element, at: (t: number) => void,
              { start = 1400, resume = 3600, period, loops = 3 })
  -> { readonly active: boolean, play(): void, pause(): void, takeOver(): void }

// Readout: say() writes the visible readout (only when the text changed);
// describe() writes aria-valuetext (call it on a state change, not per frame);
// phase() writes a polite live region, debounced ~700 ms for continuous input.
LivingUI.readout(el: Element, { target?: Element, live?: Element })
  -> { say(text: string): void, describe(text: string): void, phase(text: string): void }

// Focus: marks root with data-lui-focus and the chosen element with data-lui-on;
// null clears both. The toolkit CSS lights the chosen one and dims the rest.
LivingUI.focusOne(root: Element, chosen: Element | null) -> void

// Exploded view: one spring per item, starts staggered, reverse order to close.
LivingUI.explode(items: Array<{ el: Element, move: [number, number] }>,
                 { stagger = 35, k = 64, c = 14.2 })
  -> { open(apart: boolean): void, toggle(): void, step(dt: number, calm: boolean): boolean }

// The few rules the helpers rely on, as a string the author puts in a <style>.
LivingUI.css -> string
```

`LivingUI.css` is the only styling the toolkit carries, and all of it reads theme
variables:

```css
[data-lui-focus] [data-lui-dim]:not([data-lui-on]) { opacity: var(--lui-dim, .45) }
[data-lui-on] { color: var(--accent); stroke: var(--accent) }
[data-lui-stage]:focus-visible { outline: 1px solid var(--muted); outline-offset: 6px }
@media (prefers-reduced-motion: reduce) { [data-lui-stage] * { transition: none !important } }
```

**Size budget.** At most 5,120 bytes minified for the whole file, and the build
test in PR 2 fails past it. A rough split: `loop` 0.9 KB, easing primitives 0.6 KB,
`tour` 0.9 KB, `readout` 0.6 KB, `focusOne` plus `css` 0.4 KB, `explode` 0.9 KB.

The minified size is a budget, not what authors copy. Authors copy the readable
source of each helper they use (§6.4).

**How it ships: inlined source.** The author copies the readable source of the
helpers a figure uses into an inline `<script>`. Reasons, in order:

1. **It is the only path the dashboard frame allows.** The Dashboard tab's document
   is under two policies that intersect. The browser wrapper
   (`cspFor` in `website/src/lib/widgetSrcdoc.ts`, called with `offlineScripts` by
   `website/src/pages/members/CrewDynamicDashboard.tsx`) permits `'unsafe-inline'`
   plus one pinned Tailwind file. The composed document's own `FRAME_CSP`
   (`src/kiro_crew/dashboard_frame.py`) permits `'unsafe-inline'` plus the two
   CDN origins. Their intersection is inline script only.
2. **A served asset widens two security policies.** It needs a second pinned path
   in `cspFor` and in `FRAME_CSP`, plus a change to the test that pins them equal.
   The `cspFor` comment explains that each permitted script source is an
   exfiltration channel on a dashboard; adding one for convenience goes against
   that reasoning.
3. **A CDN copy is out.** Dashboards drop CDN origins, and a CDN adds a supply-chain
   dependency for 5 KB.
4. **It works everywhere else too:** in a saved standalone HTML artifact, in the
   offline `dashboard_save` check (no network), and in any browser.

**Retyping risk.** A model writes the copy out token by token, and one wrong
character can turn the script into an error or a silent no-op. Three things keep
that cheap:

1. **Readable, per-helper blocks.** `SKILL.md` carries each helper as its own
   short, unminified block (about 20 to 40 lines each), never one minified blob.
   An author copies only the blocks a figure needs, and readable code is far
   easier to reproduce without error than minified code.
2. **Still first.** The figure's markup already shows its settled state: the right
   numbers, the lit part, the readout. The script only moves things toward that
   state. If the copied script fails, the reader sees a correct still picture, not
   a blank or broken one. The skill states this as a rule, and PR 6's example
   follows it.
3. **Errors surface before shipping.** A copy error is a script error.
   `dashboard_save` already reports script errors with the `view.html` line and
   column, and the §7 harness prints console errors for a widget.

The cost is context: about 1,500 tokens if every helper is copied, less for the
usual two or three. Host injection of an inline preamble would remove both the
copy cost and the retyping risk without widening either CSP. It is not the
default here because it is a product change to two frames (the browser wrapper and
`dashboard_frame.compose`), and a standalone artifact would still need the copy
path. PR 9 measures the real cost and copy-error rate first and records a verdict
(§12 Q2).

### 5.3 Layer 3 — optional looks

A look is a drawing style built on Layers 1 and 2. The first is **isometric
machine**, adapted from anatomy: the concept drawn as a small, real, well-made
object (a gauge, a lock, a drawer cabinet) with four-tone shading and many small
parts.

Rules for any look:

- Used only when the user asks for it ("draw it as a machine", "isometric"). Never
  the default, and never chosen because the agent likes it.
- Its tones derive from theme variables, for example
  `color-mix(in oklab, var(--card) 85%, var(--text))` for one shade step. It
  introduces no literal colour, so it follows light, dark and custom palettes.
- It lives in its own file under `living-ui/looks/` and is not part of the 5 KB
  toolkit.

## 6. Where it lives

### 6.1 Placement

| Skill | Change |
|---|---|
| `living-ui` (new, `src/kiro_crew/builtin_skills/living-ui/`) | the rules, the toolkit, the looks, the harness; the single source of truth |
| `widgets` | the Animation section keeps its freeze-one-frame test and table, then defers "how to move" to `living-ui` in one paragraph |
| `dashboard` | §6 "Theme and reach" adds one pointer: use `living-ui` for motion on change, never an idle tour on this page |
| `explain-for` | "Draw it when the thing has a shape" adds: draw the analogy as a working object, not labelled boxes, and use `living-ui` when the widget moves |
| `theme-pack-authoring` | unchanged; style stays with theme packs |
| `frontend-design-workflow` | unchanged in substance. It governs changes to a product's own UI in a repo; `living-ui` governs runtime widgets and pages. One "see also" line for animated product UI |
| `skills/kirocrew-app-dev` | one pointer: the rules apply to app UI too; an app may bundle the toolkit instead of inlining it |

### 6.2 Files in the new skill

```
src/kiro_crew/builtin_skills/living-ui/
  SKILL.md                 rules (§5.1), API (§5.2), one readable block per helper
  toolkit/living-ui.js     readable source, the one place code is edited
  looks/isometric.md       the optional look (§5.3)
  scripts/harness.py       writes a preview page for §7
  LICENSE-anatomy.md       anatomy's MIT notice (§9)
```

### 6.3 One source of truth

`toolkit/living-ui.js` is the only file a person edits. Each helper in it sits
between two marker comments. A script copies each marked helper, unchanged, into
its own fenced block in `SKILL.md`. A test regenerates those blocks and fails when
any drifts, the same pinning style as
`test/test_runtime_skill_deploy_widget_facts.py`. The same test minifies the file
in memory with the `esbuild` the website already depends on and fails past the
5,120-byte budget. No minified file is checked in.

### 6.4 Reaching agents with no file tools

A crewmate may have no file tools, so it cannot open `toolkit/living-ui.js`. The
per-helper blocks therefore sit inside `SKILL.md`, which every agent receives
when it loads the skill. Agents with file tools may read the file instead. Either
way the author copies readable code, only the helpers it needs (§5.2, "Retyping
risk").

## 7. Visual self-check

An agent checks its own widget or page before it shows it, using backends that
already exist.

1. **Make a preview page.** `scripts/harness.py` takes a widget body (or a
   dashboard `view.html` plus a sample `fields` JSON) and writes one HTML file. The
   file sets the theme variables to a light set and a dark set (a query flag picks
   one), applies the same CSP as the frame, and for a dashboard stubs
   `window.kirocrew.fields` and `subscribe`. Two more flags: `?reduced=1` forces
   `calm`, and `?slow=0.05` runs `requestAnimationFrame` timestamps and
   `performance.now` at 1/20 speed, so a 100 ms envelope lands on several frames.
2. **Capture** with `web-verify`'s backends on a loopback server: a 1440 px element
   shot, a 390 px shot, a hover or key shot, a reduced-motion shot, and three or
   four shots at `?slow=0.05` as a contact sheet. Every frame stays under the
   2000 px cap that `web-verify` sets.
3. **Look** at each frame against a short checklist: one lit element; no words in
   the drawing; the readout matches the parts; nothing clipped at 390 px; light and
   dark both read; the reduced-motion frame is complete.

When no browser backend exists, the agent says it checked the code and not the
pixels, as `web-verify` already requires. A dashboard page still goes through
`dashboard_save`'s offline error check; this adds pixels, it does not replace it.

## 8. Accessibility

- **Reduced motion:** still but interactive (rule L). `loop` re-reads the media
  query on its change event, so turning it on mid-figure stops motion at once.
- **Keyboard:** the stage is focusable (`tabindex="0"`) with `role="slider"` for one
  value or `role="group"` with buttons for discrete choices. Arrows step, Shift
  steps further, Home and End go to the ends, Space or Enter toggles, Escape
  releases. A visible focus ring comes from `LivingUI.css`.
- **Screen readers:** `aria-valuetext` is written by `readout.describe()` on a
  state change only, because a focused slider announces every change and a
  per-frame value becomes a stream of numbers. Phases go to a polite live region,
  empty at load and filled on focus.
- **Colour is never the only signal:** the lit element also gets the readout's
  words (the dashboard skill's existing rule).
- **The tour yields:** any input stops it at once, and it never moves focus.

## 9. Licensing and attribution

anatomy is MIT-licensed ("Copyright (c) 2026 Ryan"). The rules in §5.1 are ideas
restated in our own words. The toolkit adapts specific techniques and constants
from `references/motion.md` (the follow formula, `burst`, the critically damped
`settle`, the newest-entry `IntersectionObserver` read, the explode stagger). The
isometric look adapts `references/craft.md`. So:

- `living-ui/LICENSE-anatomy.md` carries anatomy's full MIT notice, as
  `src/kiro_crew/_vendor/llama_cpp/LICENSE.md` does for that vendored code.
- `toolkit/living-ui.js` and `looks/isometric.md` start with a one-line notice
  naming the source and the licence file. Each helper block adapted from anatomy
  carries the same one-line comment, so a copied block keeps its notice.
- No anatomy file is copied verbatim; no anatomy example figure is shipped.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Motion everywhere; widgets get busy | the freeze-one-frame gate stays first; dashboards get motion on change only; the tour stops after three loops |
| The toolkit grows past its budget | the size test in PR 2 fails past 5,120 bytes |
| A colour sneaks into the toolkit or a look | the style-neutral test in PR 2 (§10.1) |
| Context cost of copying the toolkit | copy only the helpers used; PR 9 measures |
| A copy error breaks the script | readable per-helper blocks, still-first markup, script errors reported by `dashboard_save` and the harness (§5.2) |
| The copied code goes stale in old artifacts | it is self-contained, so an old widget keeps working; new widgets get the new copy |
| Battery and CPU | the loop stops when settled and when offscreen; no loop runs in a hidden tab |
| Agents ignore the rules | the gallery example in PR 6 and the skill pointers make the path the easy one; screenshots in §7 catch the rest |
| The isometric look becomes the house style | §5.3 makes it opt-in, and the skill text says so |

### 10.1 How style neutrality is checked

A test over `toolkit/living-ui.js`, `LivingUI.css` and every file under
`living-ui/looks/` fails on:

- a colour literal: `#rgb`/`#rrggbb`, `rgb(`, `rgba(`, `hsl(`, `hsla(`, or a CSS
  named colour as a value;
- `font-family`, `font-size` in px, `border-radius`, `box-shadow` in the toolkit;
- any `var(--name)` whose name is not in `THEME_VAR_NAMES`
  (`website/src/lib/widgetSrcdoc.ts`) or does not start with `--lui-`.

The check reads `THEME_VAR_NAMES` from the TypeScript source, so a theme variable
added or removed there moves the allowed set with it.

## 11. Implementation plan

Each PR is small, lands on its own, and can be dropped without breaking the others.
PRs 2 to 5 depend on PR 1; nothing else depends on order unless stated.

| # | PR | Scope | Test / acceptance |
|---|---|---|---|
| 1 | `living-ui` skill: rules | new `SKILL.md` with the gate, rules M T F W L X V, the a11y rules, the licence file | skill loads by name; a test pins that `SKILL.md` names the freeze-one-frame gate and links the widgets skill; brand and line-cap checks pass |
| 2 | Toolkit core | `toolkit/living-ui.js` with `loop`, `follow`, `spring`, `settle`, `smooth`, `burst`, `css`; the block-copy script; one block per helper in `SKILL.md` | unit tests for each helper with fake time; the reduced-motion jump; the newest-entry rule; minified size ≤ 5,120 bytes; each `SKILL.md` block equals its marked helper; the §10.1 style test |
| 3 | Toolkit: `readout` and `focusOne` | two helpers | tests: `say` writes only on change; `describe` never runs from `loop`; `phase` debounces; `focusOne` sets and clears the attributes; size still in budget |
| 4 | Toolkit: `tour` | idle tour helper | tests: starts at 1.4 s in view; pauses on pointer, focus and key; resumes at 3.6 s; stops after `loops`; never runs under `calm` |
| 5 | Toolkit: `explode` | exploded-view helper | tests: starts 35 ms apart; closing in reverse; a reversal re-staggers only changed parts; size still in budget |
| 6 | Harness and checklist | `scripts/harness.py` and the §7 section of `SKILL.md`; one example widget under `living-ui/examples/` | test: the harness output carries the frame's CSP and both theme sets, and the `?slow` and `?reduced` flags; PR body carries 1440, 390, reduced and slowed shots of the example in light and dark |
| 7 | Skill pointers | the `widgets`, `dashboard` and `explain-for` edits in §6.1; one line in `frontend-design-workflow` and `skills/kirocrew-app-dev` | a test that each of the three skills names `living-ui`; the widgets freeze-one-frame table is unchanged |
| 8 | Dashboard gallery example | one `dashboard/gallery/` package that uses `follow` on bound numbers and `burst` on a new approval, no tour | the existing gallery tests pass; `dashboard_save` verification passes on it; screenshots in the PR |
| 9 | (optional) Measure, then decide on injection | count how often widgets copy the toolkit, its token cost and how often a copy throws; propose a host-injected inline preamble only if the cost matters | blocked on §12 Q2; a written verdict recorded in this document either way |
| 10 | Isometric look | `looks/isometric.md`: when to use it, the tone recipe from theme variables, one example | the §10.1 style test covers it; screenshots in light, dark and one custom palette |

## 12. Open questions

1. **Tour length.** This RFC caps the idle tour at three loops to keep the widgets
   rule "stops when it is done". anatomy loops forever. Is three right, or should
   the tour run until the first input and then rest?
2. **Injection.** Should the host ever inject the toolkit as an inline preamble
   (no CSP change, no copy cost, no retyping risk)? PR 9 answers this with numbers before anyone
   builds it.
3. **Pixels in `dashboard_save`.** Should the offline check also return a
   screenshot, so a crewmate without a browser tool can see its own page? That is
   a product change to `dashboard_verify.py` and is out of scope here.
