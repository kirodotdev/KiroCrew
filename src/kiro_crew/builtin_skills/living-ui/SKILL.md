---
name: living-ui
description: "Use when building an animated or interactive widget, explainer or dashboard page: how motion feels, where the eye goes, where words sit, reduced motion, keyboard and screen readers. Style-neutral: it never picks colours, fonts or shapes. Pairs with the widgets skill."
---

# Living UI

These rules make a live figure easy to follow in its first second. A live figure
is an inline `<mcwidget>` explainer, a crewmate's Dashboard page, or a standalone
HTML artifact. The rules say how motion feels, where the eye goes, where words go
and how to check the result.

They never pick a colour, a font or a shape. The theme, the user's request or the
author decides the look. Where a rule needs a colour, it names a theme variable:
`var(--accent)` for the one lit element, `var(--muted)` or a lower opacity for
everything else.

Load the [`widgets`](../widgets/SKILL.md) skill as well. It owns the widget
format, the theme variables, the frame's CSP and the animation table these rules
start from. On a Dashboard page, the `dashboard` skill owns the page contract.

## The gate: freeze one frame

Run the `widgets` skill's freeze-one-frame test first: picture one still frame of
the figure. If that frame carries the meaning, the figure stays still and the
motion rules M, T, L and X do not apply. It may still take input, as a slider or
a clickable diagram does, so rules F (focus), W (words), V (verify) and the
Accessibility section apply all the same.

The motion rules say how to move once motion has earned its place. None of them
is a reason to make something move.

## M. Motion feels like a real thing

- **Time, not frames.** Compute `dt`, the seconds since the last frame, from the
  `requestAnimationFrame` timestamp. Clamp it to at most 1/30 s, so a stalled tab
  does not jump.
- **Input sets a target, and the value follows it.** A drag, a click or a key sets
  where a value is going. The shown value moves there and never snaps. Use an
  exponential follow, `x += (target - x) * (1 - Math.exp(-dt / tau))` with `tau`
  between 0.1 and 0.3 s, or a spring.
- **A spring overshoots once, then settles.** Where an overshoot would show a
  false value, such as a count or a cost, settle with critical damping so the
  value never passes its target.
- **A one-shot event is an envelope over its age.** When an approval arrives or a
  run fails, the signal rises over 40 to 120 ms, starting flat (zero slope), then
  fades more slowly. Never jump a value to full in one frame.
- **One loop per figure.** It stops once every value has settled, and starts again
  on the next input or change.

## T. Truth comes from one model

Keep one plain state object with no DOM in it. The moving parts, the visible
readout and the accessible value all read from it, so they cannot disagree.

The readout reports where the parts are now (the eased value), not where they are
heading. When it shows a phase word and a number, both come from the same
instant.

## F. Focus is one thing

Exactly one element is lit, in `var(--accent)`. Every other element steps down
one tone: a lower opacity, or `var(--muted)`. Hover, the keyboard and the idle
tour all choose the lit element. Nothing lights a second element.

## W. Words stay out of the drawing

- Title, hint and live readout each sit in a fixed corner of the card. They never
  ride on a moving part.
- Legend and caption sit below the card.
- The drawing carries no labels, apart from at most one small label on a part.

## L. Life without nagging

- **An explainer may run an idle tour.** It starts about 1.4 s after the figure
  comes into view, pauses on hover, focus or press, and resumes about 3.6 s after
  the last input. It drives the same targets the user would. It plays at most
  three loops and then rests, so the `widgets` rule "stops when it is done" still
  holds. A visible play/pause control restarts it.
- **Offscreen means stopped.** Watch the figure with an `IntersectionObserver`,
  act on the newest entry in each callback, and stop the loop while the figure is
  out of view.
- **Reduced motion.** Under `prefers-reduced-motion: reduce`, every value jumps
  straight to its target, there is no tour, and every envelope is zero. The figure
  still answers input.

## X. Exploded view (optional)

Where seeing the parts apart helps, hover or a key opens the figure up:

- Each part moves on its own spring, and the parts start about 35 ms apart.
- Closing runs in reverse order.
- A reversal part-way through re-staggers only the parts whose target changed.
- The selected part is the lit one (rule F).

## V. Verify before you ship

Look at the figure before you show it:

- a still at desktop width and a still at 390 px wide;
- a still with reduced motion on;
- a slowed contact sheet: three or four frames taken with time slowed down (for
  example, `dt` multiplied by 0.05 while you capture), so a short envelope lands
  on more than one frame;
- each of these in a light theme and a dark theme.

In every frame, check for one lit element, no words in the drawing, a readout
that matches the parts, and nothing clipped at 390 px. The reduced-motion still
must be complete on its own. The `web-verify` skill has the capture backends.
With no browser backend, say that you checked the code, not the pixels. A
Dashboard page still goes through the check `dashboard_save` runs.

## Accessibility

- **Reduced motion** is still but interactive (rule L). Listen for the media
  query's `change` event, so turning it on mid-figure stops motion at once.
- **Keyboard.** Make the stage focusable with `tabindex="0"`. Give it
  `role="slider"` (with `aria-valuemin`, `aria-valuemax` and `aria-valuenow`) for
  one value, or `role="group"` with buttons for discrete choices. Arrow keys step,
  Shift with an arrow steps further, Home and End go to the ends, Space or Enter
  toggles, and Escape releases the current selection. Show a visible focus ring
  drawn from a theme variable, for example `outline: 1px solid var(--muted)` on
  `:focus-visible`.
- **Screen readers.** Update `aria-valuenow` and `aria-valuetext` when the state
  changes, never on every frame: a focused slider announces each change, so a
  per-frame value becomes a stream of numbers. Phase words go to a polite live
  region (`aria-live="polite"`). It is empty at load, takes the current phase
  when the stage is focused, and is updated each time input or new data changes
  the phase. The idle tour does not write to it, so the region speaks only when
  the user acts or the data changes.
- **Colour is never the only signal.** The readout's words also name the lit
  element.
- **The tour yields.** Any input stops it at once. It never moves keyboard focus.

## Toolkit

`LivingUI` is a small helper script for these rules. It needs nothing else, and
it has no colours, fonts or shapes of its own. Each helper is one block below
that stands on its own: copy only the blocks a figure uses, unchanged, into the
figure's inline `<script>`. Nothing serves the toolkit as a file, and the
Dashboard frame runs inline script only, so the copy is how it ships. An agent
with file tools can read the same code in `toolkit/living-ui.js` beside this file.

- **Still first.** Write the markup in its settled state: the right numbers, the
  lit part, the readout. The script only moves things toward that state, so a
  copy that fails still leaves a correct still picture.
- **Copy with care.** One wrong character stops the whole script.
  `dashboard_save` reports a Dashboard page's script errors with their line and
  column; fix every one before you show the page.
- **Pick units where 0.0005 is too small to see**, such as pixels or a share
  from 0 to 1. `follow`, `spring` and `settle` land exactly on their target once
  they are that close.

### API

```js
// One requestAnimationFrame loop per stage (the figure's root element).
// tick returns true while anything moves; the loop sleeps when it returns
// false or the stage is offscreen. Call kick() after an input or a change.
LivingUI.loop(stage, tick: (dt, calm) => boolean) -> { kick(), stop(), readonly calm }

// Easing. Under calm (reduced motion) each one jumps to its target.
LivingUI.follow(x, target, dt, tau = 0.2, calm = false) -> number // the new x
LivingUI.spring(s = { x, v }, target, dt, k = 420, c = 26, calm = false, substeps = 4) -> boolean
LivingUI.settle(s = { x, v }, target, dt, seconds = 0.4, calm = false) -> boolean
LivingUI.smooth(a, b, x) -> number // smoothstep, 0 to 1
LivingUI.burst(age, rise = 0.08, fall = 0.13) -> number // event envelope, 0 to 1

// The CSS rules the helpers rely on, as a string for a <style> element.
LivingUI.css -> string
```

`spring` and `settle` move `s.x` and `s.v` in place and return true while they
still move. Use `spring` where one overshoot looks real, such as a needle or a
drawer, and `settle` where an overshoot would show a false value, such as a
count or a cost. For a one-shot event, keep its start time and read `burst` from
its age on each frame; under `calm`, use 0. `LivingUI.css` lights the element
marked `data-lui-on`, dims the other `data-lui-dim` elements inside a
`data-lui-focus` root, and draws the focus ring on a `data-lui-stage` element.

A value that follows the pointer, with its readout:

```js
const state = { x: 0.5, target: 0.5 }; // one model (rule T)
const run = LivingUI.loop(stage, (dt, calm) => {
  state.x = LivingUI.follow(state.x, state.target, dt, 0.2, calm);
  knob.setAttribute('transform', `translate(${state.x * 200} 0)`);
  readout.textContent = `${Math.round(state.x * 100)}%`;
  return state.x !== state.target; // sleep once it lands
});
stage.addEventListener('pointermove', (event) => {
  const box = stage.getBoundingClientRect();
  state.target = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  run.kick();
});
```

### The blocks

Each block adapts techniques from anatomy (see Credits) and keeps a one-line
notice that says so.

<!-- living-ui:blocks begin: copied from toolkit/living-ui.js by a script; edit that file -->

#### `loop`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// One requestAnimationFrame loop for a stage, the figure's root element.
// tick(dt, calm) runs each frame and returns true while anything moves. dt is
// in seconds, 1/60 on the first frame after a wake and never over 1/30, and
// calm is true under prefers-reduced-motion, following its change event. The
// loop sleeps when tick returns false or the stage is offscreen; kick() wakes
// it after an input or a data change, and stop() ends it for good.
LivingUI.loop = (stage, tick) => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  let frame = 0, last = -1, inView = false, ended = false;
  const step = (now) => {
    const dt = last < 0 ? 1 / 60 : Math.min((now - last) / 1000, 1 / 30);
    last = now;
    frame = 0;
    const moving = tick(dt, reduce.matches);
    if (moving && inView && !ended && !frame) frame = requestAnimationFrame(step);
  };
  const kick = () => {
    if (frame || !inView || ended) return;
    last = -1;
    frame = requestAnimationFrame(step);
  };
  // One callback can carry several entries for the stage, oldest first, so
  // only the newest one says whether the stage is in view now.
  const view = new IntersectionObserver((entries) => {
    inView = entries[entries.length - 1].isIntersecting;
    if (inView) return kick();
    cancelAnimationFrame(frame);
    frame = 0;
  }, { rootMargin: '120px 0px' });
  view.observe(stage);
  reduce.addEventListener('change', kick);
  return {
    kick,
    stop() {
      ended = true;
      cancelAnimationFrame(frame);
      view.disconnect();
      reduce.removeEventListener('change', kick);
    },
    get calm() { return reduce.matches; },
  };
};
```

#### `follow`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// Exponential follow. Each frame x covers 1 - e^(-dt / tau) of its gap to
// target, so it slows as it arrives and moves the same at any frame rate.
// tau is in seconds; 0.1 to 0.3 feels right. Returns the new x, which is
// exactly target under calm or once it is within 0.0005, so x === target
// means it has arrived.
LivingUI.follow = (x, target, dt, tau = 0.2, calm = false) => {
  const next = target + (x - target) * Math.exp(-dt / tau);
  return calm || Math.abs(target - next) < 5e-4 ? target : next;
};
```

#### `spring`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// A spring pulling s = { x, v } (position and speed) toward target, with
// stiffness k and damping c on a unit mass. The defaults overshoot once, by
// about 7%, then settle. Each frame is cut into substeps so a stiff spring
// stays stable. It lands exactly on target with v = 0 under calm, or once
// both the gap and the speed are under 0.0005. Returns true while moving.
LivingUI.spring = (s, target, dt, k = 420, c = 26, calm = false, substeps = 4) => {
  const h = dt / substeps;
  for (let i = 0; i < substeps && !calm; i++) {
    s.v += (k * (target - s.x) - c * s.v) * h;
    s.x += s.v * h;
  }
  if (calm || (Math.abs(target - s.x) < 5e-4 && Math.abs(s.v) < 5e-4)) {
    s.x = target;
    s.v = 0;
    return false;
  }
  return true;
};
```

#### `settle`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// A critically damped spring on s = { x, v }: it moves toward target and
// never passes it, for a value where an overshoot would show something
// false, such as a count or a cost. It covers about 90% of the way in
// `seconds`, and each frame is solved exactly, so any dt is stable. It lands
// exactly on target with v = 0 under calm or when `seconds` is not above 0,
// when a step would reach or cross target, or once both the gap and the
// speed are under 0.0005. Returns true while moving.
LivingUI.settle = (s, target, dt, seconds = 0.4, calm = false) => {
  const w = 4 / seconds;
  const gap = s.x - target;
  const push = (s.v + w * gap) * dt;
  const fade = Math.exp(-w * dt);
  s.x = target + (gap + push) * fade;
  s.v = (s.v - w * push) * fade;
  const crossed = (s.x - target) * gap <= 0;
  const close = Math.abs(target - s.x) < 5e-4 && Math.abs(s.v) < 5e-4;
  if (calm || !(seconds > 0) || crossed || close) {
    s.x = target;
    s.v = 0;
    return false;
  }
  return true;
};
```

#### `smooth`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// Smoothstep: 0 at or below a, 1 at or above b, and between them an S-curve
// that starts and ends flat. a must be less than b.
LivingUI.smooth = (a, b, x) => {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
};
```

#### `burst`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// The envelope of a one-shot event, such as an approval arriving or a run
// failing, read from the event's age in seconds. It is 0 before the event,
// rises over `rise` seconds from a flat start to 1, then fades with time
// constant `fall`. Once faded under 0.001 it returns exactly 0, so a value
// above 0 means the event still shows. Under calm, use 0 instead.
LivingUI.burst = (age, rise = 0.08, fall = 0.13) => {
  if (!(age > 0)) return 0;
  const t = Math.min(1, age / rise);
  const level = t * t * (3 - 2 * t) * Math.exp(Math.min(0, rise - age) / fall);
  return age > rise && level < 1e-3 ? 0 : level;
};
```

#### `css`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// The few CSS rules the helpers rely on. Colours come from theme variables
// only. Add it once, for example:
// document.head.append(Object.assign(document.createElement('style'), { textContent: LivingUI.css }));
LivingUI.css = `
[data-lui-focus] [data-lui-dim]:not([data-lui-on]) { opacity: .45 }
[data-lui-on] { color: var(--accent); stroke: var(--accent) }
[data-lui-stage]:focus-visible { outline: 1px solid var(--muted); outline-offset: 6px }
@media (prefers-reduced-motion: reduce) { [data-lui-stage] * { transition: none !important } }
`;
```

<!-- living-ui:blocks end -->

## Credits

Several of these rules restate ideas from
[anatomy](https://github.com/wheresryan22/anatomy), an MIT-licensed agent skill by
Ryan. Its licence notice is in [LICENSE-anatomy.md](LICENSE-anatomy.md) beside
this file.
