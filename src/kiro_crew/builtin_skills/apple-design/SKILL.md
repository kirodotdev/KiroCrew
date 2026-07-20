---
name: apple-design
description: Apple's fluid-interface + material + typography principles, tailored to the KiroCrew dashboard (React 18 + framer-motion 12 + Tailwind 3). Use ONLY when the user explicitly asks to apply, review, or optimize KiroCrew frontend UI for an Apple-style / native fluid feel — springs & interruptible motion, gesture physics (momentum, rubber-band), translucent glass materials, or size-tiered typography. Do NOT auto-load for generic frontend/CSS/modal work that doesn't ask for the Apple aesthetic.
always: false
triggers: apple design, apple-design, apple style, apple-style, fluid interface, fluid-interface, native-feeling ui, make it feel native, optimize ui for feel, ui feel polish, gesture physics, momentum and spring feel, apple-style motion
---

# Apple Design — KiroCrew edition

How Apple builds interfaces that feel like an extension of the user, **translated to the KiroCrew web stack** and grounded in this repo's actual patterns. The knowledge comes from Apple's WWDC design talks (chiefly *Designing Fluid Interfaces*, WWDC 2018); the KiroCrew layer below maps each principle to concrete files in `website/`.

> Core idea: an interface feels alive when motion **starts from the current on-screen value, inherits the user's velocity, projects momentum forward, and can be grabbed and reversed at any instant.** Springs make this natural because they are inherently interruptible and velocity-aware.

## When to use

Load this skill whenever you touch the KiroCrew dashboard frontend (`website/`, or the served build in `src/kiro_crew/static/`):
- Building a new component with motion, gestures, modals, drawers, popovers, or glass surfaces.
- Reviewing or optimizing existing UI for "feel."
- Deciding animation config (spring vs tween), gesture handling, material/blur, typography, or accessibility fallbacks.

---

## KiroCrew stack (know this before editing)

- **React 18 + framer-motion 12** (`framer-motion`'s `bounce` + `duration` spring API maps directly to Apple's damping + response).
- **Tailwind 3** (`tailwind.config.js`) — note it currently extends colors/fonts/radius/shadow but **no** `fontSize`/`letterSpacing`/`lineHeight` scales.
- Global CSS + design tokens live in `website/src/index.css` (theme vars: `--chrome`, `--border`, `--shadow-sm/md/lg`, `--muted-strong`; utilities: `.glass-surface`, `.glass-refract`, `.topbar-glass`, `.scroll-shadow`).
- Radix primitives (`@radix-ui/*`) back some popovers/menus.

---

## House rules (the short version)

1. **Springs, not tweens, for anything a user can touch.** Default to `{ type: 'spring', bounce: 0, duration: 0.3–0.4 }` (critically damped). Reserve `bounce: ~0.2` **only** when the gesture itself carried momentum (a flick/throw/drag release). Never put fixed-duration `ease` tweens on a `width`/`height`/`x`/`y` channel of a draggable or resizable panel.
2. **Feedback on pointer-down, instantly.** Every button gets an `:active`/`whileTap` press transform (~75–100ms). Never wait for `click`/release to show state.
3. **Interruptible always.** Animate from the *presentation* (live) value, never the target. Never lock out input during a transition. Avoid CSS `transition`/`@keyframes` for gesture-driven motion (they can't be grabbed mid-flight); CSS keyframes are fine for ambient/decorative loops.
4. **Gestures track 1:1**, respect the grab offset, use Pointer Events + `setPointerCapture`, require a ~10px movement threshold, hand off release **velocity**, **project momentum** to the resting point, and **rubber-band** at boundaries instead of hard-clamping.
5. **Glass must actually be glass.** A translucent surface needs an effective background alpha ≈ 0.60–0.75 — not 0.95+. Bigger surfaces read thicker (more blur + deeper shadow). Never stack a translucent surface on another translucent surface. Dim-to-focus modals pair the panel with a **blurred** scrim; parallel non-blocking panels use translucency **without** a scrim.
6. **Typography is size-specific.** Negative tracking only on large display text (`~-0.02em`); body near `0`; small labels slightly positive. Tight leading on headings (`~1.05`), loose on body (`~1.5`). Size in `rem`, not `px`. Prefer `system-ui`.
7. **Ship the three accessibility media queries** (`prefers-reduced-motion`, `prefers-reduced-transparency`, `prefers-contrast`) and gate framer-motion with `useReducedMotion()`.

---

## Reference-good patterns — copy these

These already follow the principles; use them as the house template.

- **`layoutId` spring morph** — `website/src/components/Modal.tsx` and `McpDetailModal.tsx` (spring, `bounce: 0`). The canonical open/expand pattern.
- **Slider knob** — `website/src/components/ui.tsx` (~L483): position via a motion value, knob scale/shadow via spring, scale reacts to `dragging` state. The template for velocity-aware direct manipulation.
- **Pointer capture** — `BrowserLiveView` and the slider use `setPointerCapture(e.pointerId)`; copy this for any new drag handle.
- **Reduced-motion CSS fallbacks** — streaming-text effects in `index.css` (`.ft-block-reveal`, `.ft-word`, `.ft-resize`, streaming caret) each set `animation: none` under `prefers-reduced-motion`. Mirror this for any new keyframe animation.

## Anti-patterns to avoid (seen in this repo)

- Fixed-duration tween on a draggable/resizable dimension, or a `duration: 0` mid-gesture snap (was in `OverlayDrawer.tsx`). Use a spring; let the gesture end settle via spring.
- Hard `Math.min/Math.max` clamp at a drag boundary with no resistance. Add rubber-banding.
- `onMouseDown` + `document` mousemove/mouseup for resizers (no touch support). Use Pointer Events + capture; prefer a shared `usePointerDrag` hook.
- `backdrop-filter: blur()` behind a 0.95–0.98 alpha background (blur is invisible — it's just an opaque bar). Drop effective alpha to ~0.70.
- A translucent card (`bg-card/80 backdrop-blur-sm`) rendered inside already-translucent context. Make it solid + shadow.
- One global `letter-spacing` applied to every size; fixed-`px` font sizes; a static display font as the UI body face.
- framer-motion components with no `useReducedMotion()` check; `backdrop-filter` with no `prefers-reduced-transparency` fallback.

---

## Review / build checklist

Run this before shipping any KiroCrew UI change. Each line maps to a principle section below.

**Motion**
- [ ] Touchable elements animate with a spring (`bounce: 0` default), not a fixed-duration tween. (§4)
- [ ] Animations start from the live/presentation value and are interruptible mid-flight. (§3)
- [ ] Gesture end hands off release velocity to the settle animation. (§5, §6-momentum)

**Gesture**
- [ ] Drag tracks the pointer 1:1 and respects the grab offset. (§2)
- [ ] Pointer Events + `setPointerCapture`; works on touch, not just mouse. (§2)
- [ ] ~10px movement threshold before committing to a drag/direction. (§10)
- [ ] Momentum projected to a resting point on flick; boundaries rubber-band, not hard-stop. (§6, §9)

**Material**
- [ ] Translucent chrome has effective bg alpha ≈ 0.6–0.75; blur scales with surface size. (§12)
- [ ] No translucent-on-translucent stacking. (§12)
- [ ] Dim-to-focus modal = blurred scrim; parallel panel = no scrim. (§12)
- [ ] Bright top edge on glass; scroll-edge fade instead of a hard 1px divider under sticky chrome. (§12)

**Typography**
- [ ] Tracking is size-specific (negative only on large text); body `rem`, not `px`; leading tight on headings. (§15)

**Response & a11y**
- [ ] Every button has instant `:active`/`whileTap` press feedback. (§1)
- [ ] `prefers-reduced-motion`, `prefers-reduced-transparency`, `prefers-contrast` all handled; framer-motion reads `useReducedMotion()`. (§14)

---

## The principles (canonical reference)

### 1. Response — kill latency
Respond on pointer-**down**, not release. Feedback must be continuous *during* the interaction (drags/sliders/drawers update 1:1 the whole way), not only at the end. Audit every debounce, timer, and transition wait on the input path.
```css
.button:active { transform: scale(0.97); transition: transform 100ms ease-out; }
```

### 2. Direct manipulation — 1:1 tracking
Dragged content stays glued to the finger and respects the offset from *where it was grabbed* (never snap to center on grab). Use Pointer Events + `setPointerCapture` so tracking survives leaving bounds. Track a short position/velocity history for release velocity.

### 3. Interruptibility — the single most important principle
Every animation must be grabbable and reversible at any moment. Never lock out input during a transition. **Always animate from the presentation (current) value**, never the target — starting from the target causes a visible jump. Avoid CSS transitions/`@keyframes` for gesture-driven motion. On reversal, blend velocity (don't hard-cut). Decompose 2D motion into independent X and Y springs.

### 4. Behavior over animation — use springs
A fixed-duration animation can't respond to new input; a spring can (new input just changes the target). Think in two designer parameters:
- **Damping ratio** — overshoot. `1.0` = critically damped (no bounce). `<1.0` = bouncier.
- **Response** — how quickly it reaches target, in seconds. Not a duration; settle time emerges from the parameters.

Defaults: start most UI at **damping 1.0**; add **~0.8** bounce only when the gesture carried momentum.

| Interaction | Damping | Response |
| --- | --- | --- |
| Move / reposition | `1.0` | `0.4` |
| Rotation | `0.8` | `0.4` |
| Drawer / sheet | `0.8` | `0.3` |

framer-motion mapping (`bounce` + `duration` ≈ Apple's damping + response):
```js
import { animate } from 'motion';
animate(el, { y: 0 }, { type: 'spring', bounce: 0,   duration: 0.4 }); // critically damped default
animate(el, { y: t }, { type: 'spring', bounce: 0.2, duration: 0.4 }); // momentum interaction only
```

### 5. Velocity handoff
When a gesture ends, the animation must continue at the finger's exact velocity — no seam. framer-motion takes absolute px/s via the `velocity` option. If an API wants relative velocity: `relativeVelocity = gestureVelocity / (target − current)`.

### 6. Momentum projection
Don't snap from the release point — project the resting position from velocity (like scroll deceleration), then snap to the nearest target.
```js
function project(initialVelocity /* px/s */, decelerationRate = 0.998) {
  return (initialVelocity / 1000) * decelerationRate / (1 - decelerationRate);
}
const projectedEndpoint = currentPosition + project(releaseVelocity);
const target = nearestSnapPoint(projectedEndpoint);
animateSpringTo(target, { velocity: releaseVelocity });
```
Use the exponential-decay form above, not the textbook `v²/(2·decel)`.

### 7. Spatial consistency
Enter and exit along the same path (in-from-right → out-to-right). Anchor interactions to their source: set `transform-origin` to the trigger so a menu/popover/sheet grows from the button that opened it. Mirror the easing on reversible transitions.

### 8. Hint in the direction of the gesture
Intermediate motion should telegraph the outcome (Control Center modules "grow up and out toward your finger"). Make in-between frames point at the result.

### 9. Rubber-banding — soft boundaries
Resist progressively past an edge instead of stopping hard.
```js
function rubberband(overshoot, dimension, constant = 0.55) {
  return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
}
```

### 10. Gesture detail checklist
Tap: highlight on touch-down, commit on touch-up; ~10px hit padding; allow cancel-by-dragging-away. Drag/swipe: ~10px threshold before committing a direction, then 1:1. Detect plausible gestures in parallel and cancel the losers; avoid recognizers that only report a final state.

### 11. Frame-level smoothness
Smoothness is what's *in* the frames. Keep per-frame positional change below the perception threshold. Animate only compositor-friendly properties (`transform`, `opacity`); hint with `will-change`. `requestAnimationFrame` is the display-synced clock.

### 12. Materials & depth — translucency conveys hierarchy
- Build nav/toolbars/sheets as **translucent layers** (`backdrop-filter: blur()` + semi-transparent bg) with content scrolling under — not opaque strips.
- Material weight encodes hierarchy; **never stack a light translucent surface on another** (legibility collapses).
- Bigger surfaces read thicker: stronger blur + deeper shadow than small chips.
- **Dim to focus, separate to keep flow.** Modal → dim scrim + push background back. Parallel panel → translucency + offset, **no scrim**. Stacked sheets → progressively dim/push back each parent.
- **Vibrancy:** over translucent surfaces use higher-contrast, slightly heavier text + a small letter-spacing bump — not flat gray. Put color on a solid layer.
- **Scroll edge effects, not hard dividers:** fade a blur/gradient mask where content meets floating chrome, only where they overlap.
- **Materialize, don't just fade:** animate blur radius + scale together on enter/exit.
```css
.toolbar {
  background: rgba(255, 255, 255, 0.6);
  backdrop-filter: blur(20px) saturate(180%);
  border-top: 1px solid rgba(255, 255, 255, 0.4); /* bright top edge */
}
```

### 13. Multimodal feedback
**Causality** (fire on the actual causal event), **Harmony** (visual + sound + haptic on the same frame), **Utility** (reserve haptics/sound for meaningful moments — over-feedback trains users to ignore it).

### 14. Reduced motion & accessibility
Reduced motion means a *gentler* equivalent, not none. Handle three independent signals:
- **`prefers-reduced-motion: reduce`** → replace slides/springs/parallax with short opacity cross-fades; drop overshoot; keep comprehension cues. Also gate framer-motion with `useReducedMotion()` (CSS resets don't stop JS-driven transforms).
- **`prefers-reduced-transparency: reduce`** → raise background opacity, drop the blur.
- **`prefers-contrast: more`** → near-solid backgrounds with a defined contrasting border.
```css
@media (prefers-reduced-motion: reduce) {
  .sheet { transition: opacity 200ms ease; transform: none !important; }
}
@media (prefers-reduced-transparency: reduce) {
  .toolbar { background: white; backdrop-filter: none; }
}
```

### 15. Typography — optical sizing, tracking, leading
- **Tracking is size-specific.** Large display → *negative* (`~-0.02em`); body → near `0`; small text → slightly *positive*. A single fixed `letter-spacing` is wrong somewhere.
- **Leading tracks size inversely.** Tight on headings (`~1.05`), looser on body (`~1.5`).
- **Hierarchy = weight + size + leading as a set.** Emphasize with weight.
- **Respect user text size:** spacing in `rem`/`em`, not fixed `px`.
- **Default to the platform system font** (`system-ui`) — it ships optical sizing and tracking tables. Use `font-optical-sizing: auto` for variable fonts.
```css
:root { font: 100%/1.5 system-ui, sans-serif; }
.display {
  font-size: clamp(2rem, 5vw, 4rem);
  line-height: 1.05;
  letter-spacing: -0.02em;
  font-optical-sizing: auto;
}
```

### 16. Design foundations — the eight principles
Purpose · Agency (offer choices, easy undo, confirm only genuinely destructive actions) · Responsibility (privacy, safety, previews) · Familiarity (metaphors, consistency) · Flexibility (contexts, devices, accessibility) · Simplicity (not minimalism — strip the unnecessary, show the common path first) · Craft (every spacing/timing/alignment is deliberate) · Delight (the result of getting the other seven right).

Tactical: feedback comes in four kinds (status, completion, warning, error — validate inline, not on submit); wayfinding (every screen answers where am I / where can I go / how do I leave); grouping & mapping (place a control near what it affects); direct specific labels beat generic ones.

### 17. Process
Prototype interactively (a working demo beats a million static designs). Design interaction and visuals together. Review motion with fresh eyes — slow-motion / frame-by-frame.

---

## Quick reference

| Need | Technique | Concrete value |
| --- | --- | --- |
| Default UI spring | Critically damped, no overshoot | `bounce: 0`, `duration: 0.3–0.4` |
| Momentum / flick spring | Slight bounce | `bounce: ~0.2`, `duration: 0.3–0.4` |
| Gesture → spring velocity | Hand off release velocity | framer `velocity:` (px/s) |
| Flick landing point | Project momentum | `current + (v/1000)·d/(1−d)`, `d ≈ 0.998` |
| Interrupt cleanly | Start from presentation value | read live transform |
| 1:1 drag | Pointer Events + capture | respect grab offset, ~10px threshold |
| Feedback | Pointer-down, continuous | `active:scale-95 active:duration-75` |
| Boundary | Rubber-band | progressive resistance |
| Translucent chrome | `backdrop-filter` layer, effective alpha ~0.7 | content scrolls under |
| Type tracking | Size-specific | large `-0.02em`, body `~0` |
| Reduced motion | Cross-fade + `useReducedMotion()` | drop overshoot/parallax |
