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

Look at the figure before you show it, as pictures, not as code. Below,
`scripts/harness.py` stands for `<skill-dir>/scripts/harness.py`, beside this
file.

1. **Make a preview page.** Give the harness a widget body, or one whole
   `<mcwidget>` tag, and it writes one HTML file:

   ```bash
   python3 scripts/harness.py figure.html -o preview.html
   ```

   That file frames the body the way the chat does: the same CSP, the same
   sandbox and the default theme's variables. Nothing is served; open the file
   itself, with query flags to pick what you see:
   - `?theme=light` or `?theme=dark`;
   - `?reduced=1` answers `prefers-reduced-motion` as `reduce`, so `loop` runs
     with `calm` true;
   - `?slow=0.05` runs `requestAnimationFrame` and `performance.now` at 1/20
     speed, so a 100 ms envelope lands on several frames.

   For a Dashboard page, pass its `view.html`, a JSON file of sample field
   values, and one `--push` file of new values for each change to watch:

   ```bash
   python3 scripts/harness.py view.html --fields now.json --push next.json
   ```

   The frame is then the Dashboard tab's: inline script only, so a CDN script
   fails as it does there, and the host fills the bound cells and answers
   `window.kirocrew.fields` and `subscribe`. A button above the frame sends
   each push file in as the next refill, so a slowed run can show the change.

   A script error shows above the frame with its line in your file, and a
   load the CSP blocks shows there with its address. Fix every one.
2. **Capture** each of these with a `web-verify` backend, in light and in dark:
   - a still 1440 px wide and a still 390 px wide;
   - a still with `?reduced=1` taken right after an input, which must land at
     once;
   - a slowed run: with `?slow=0.05`, start a change, then take three or four
     shots a second or more apart.
3. **Read every PNG** before you ship. In each one, check for one lit element,
   no words in the drawing, a readout that matches the parts, and nothing
   clipped at 390 px. Light and dark must both read, and the reduced-motion
   still must be complete on its own. Fix what fails, then shoot again.

`examples/build-queue.html` beside this file passes this check; start from it.
The preview has no Tailwind runtime, so Tailwind classes show unstyled there.
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

// The idle tour (rule L). Each frame it calls at(t): t is the ms since the
// current loop began, from 0 up to period, the length of one loop in ms. It
// starts once the stage has been in view for `start` ms, pauses at once on
// input inside the stage, resumes `resume` ms after the last one, and rests
// after `loops` loops with one last call, at(period). active is true only
// while it drives.
LivingUI.tour(stage, at: (t) => void, { start = 1400, resume = 3600, period, loops = 3 } = {})
  -> { readonly active, play(), pause(), takeOver() }

// The figure's words. say() writes the visible readout and may run every
// frame; describe() writes target's aria-valuetext, on a state change only;
// phase() writes the live region once 700 ms pass with no newer phase.
// Each one writes only when its text changes.
LivingUI.readout(el, { target, live } = {}) -> { say(text), describe(text), phase(text) }

// The one lit element: chosen gets data-lui-on and root gets data-lui-focus;
// null takes both away. It never moves keyboard focus.
LivingUI.focusOne(root, chosen) -> void

// The exploded view (rule X): one spring per part, items in the order the
// parts leave. open(true) starts each part `stagger` ms after the one
// before; open(false) brings them home in reverse order. step moves each el
// by its share of move and returns true while a part still moves or waits.
LivingUI.explode(items: [{ el, move: [dx, dy] }], { stagger = 35, k = 64, c = 14.2 } = {})
  -> { open(apart), toggle(), step(dt, calm) -> boolean }

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

`readout` keeps three kinds of words apart (rule T). Call `say` from the loop
with where the parts are now. Call `describe` from the input or data handler
with the new state, never from the loop: a screen reader speaks every new
value text of a slider that has keyboard focus, so a value text written each
frame becomes a stream of numbers. Call `phase` when the phase word changes,
and with the current phase when the stage gets keyboard focus. It speaks only
once 700 ms pass with no newer phase, so a drag is spoken once, when it rests.
Make `live` a visually hidden element with `aria-live="polite"`, empty in the
markup. `focusOne` moves the lit element (rule F): hover, the keyboard and the
tour each call it with the part they pick, or with `null` when none is picked.
It takes `data-lui-on` from every other element inside `root`, so a part lit
in the still markup steps down with the rest when another part is picked.

`tour` is for an explainer, never a Dashboard page. Its `at(t)` acts as an
input would: it sets the targets for time `t`, calls the loop's `kick()` and
calls `focusOne` to pick the lit element. Leave `phase` out of `at`, since the
idle tour never writes the live region. Give `tour` a `period` above 0, since
without one it never runs, and start and end each loop on the settled state
the markup shows: the last call, `at(period)`, lands the figure there when the
tour rests. Input inside the stage pauses the tour, so put the play/pause
button outside the stage and have it call `pause()` and `play()`. It says
Pause while the tour is on, even while the tour waits out an input, and Play
once its own click pauses the tour or `at` gets `t === period`. Any other
control outside the stage that moves the figure calls `takeOver()`.

`explode` takes a figure apart (rule X), and the markup draws it assembled.
Give each part an element of its own with no transform, such as a `<g>`, and
a `move` in px (user units inside an SVG) that clears its neighbours. Order
the parts so nothing passes through another: a part that sits in another's
way out comes first. Call `step(dt, calm)` in every tick, not behind a `||`,
and keep the loop running while it returns true. Hover, a tap or a key opens
it. `pointerenter` and `pointerleave` on the stage call `open(true)` and
`open(false)` only when `pointerType` is `mouse`, since every tap fires both;
a `pointerup` that is not from a mouse, and Space or Enter, call `toggle()`,
while a swipe that scrolls the page ends in `pointercancel` and toggles
nothing. Each one then calls `kick()`. Light the part the reader picks with
`focusOne` (rule F), and name it in the readout.

A value that follows the pointer, with its readout:

```js
const state = { x: 0.5, target: 0.5 }; // one model (rule T)
const words = LivingUI.readout(readout);
const run = LivingUI.loop(stage, (dt, calm) => {
  state.x = LivingUI.follow(state.x, state.target, dt, 0.2, calm);
  knob.setAttribute('transform', `translate(${state.x * 200} 0)`);
  words.say(`${Math.round(state.x * 100)}%`);
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

#### `tour`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// The idle tour of an explainer (rule L). Each frame it calls at(t), and at
// sets the same targets an input would. t is the tour's own time in ms since
// the current loop began, from 0 up to period, and it stands still whenever
// the tour does not drive. The tour starts once the stage has been in view
// for `start` ms and rests after `loops` loops, with at(period) as its last
// call, so the figure lands on the loop's end. Pointer, keyboard focus and
// key input inside the stage pause it at once, and it resumes `resume` ms
// after the last one; takeOver() pauses it the same way, for a control
// outside the stage. pause() holds it until play(), and play() after a rest
// starts the loops over. It never calls at offscreen, in a hidden tab, under
// reduced motion or without a period above 0, and active is true only while
// it drives. It never moves keyboard focus and writes nothing to the page.
LivingUI.tour = (stage, at, { start = 1400, resume = 3600, period, loops = 3 } = {}) => {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)');
  const end = loops * period;
  // delay is how long the tour waits before it drives: `resume` after an input
  // until it drives again, even across a spell offscreen, and `start` otherwise.
  let frame = 0, timer = 0, last = -1, time = 0, delay = start, inView = false, held = false;
  const ready = () => inView && !held && !reduce.matches && !document.hidden && time < end;
  const stop = () => {
    cancelAnimationFrame(frame);
    clearTimeout(timer);
    frame = timer = 0;
  };
  // The first frame after a wait counts no time, and a gap counts 1/30 s at most.
  const step = (now) => {
    time += last < 0 ? 0 : Math.min(now - last, 100 / 3);
    last = now;
    frame = time < end ? requestAnimationFrame(step) : 0;
    at(frame ? time % period : period);
  };
  const drive = () => {
    stop();
    if (!ready()) return;
    delay = start;
    last = -1;
    frame = requestAnimationFrame(step);
  };
  // Stops at once, then drives after `delay` ms unless something stops it first.
  const wait = () => {
    stop();
    if (ready()) timer = setTimeout(drive, delay);
  };
  const takeOver = () => {
    delay = resume;
    wait();
  };
  for (const type of ['pointerdown', 'pointermove', 'focusin', 'keydown']) {
    stage.addEventListener(type, takeOver);
  }
  // In view means in the viewport itself, with no margin, so the reader sees
  // the still figure first. Only the newest entry says where the stage is now.
  new IntersectionObserver((entries) => {
    inView = entries[entries.length - 1].isIntersecting;
    wait();
  }).observe(stage);
  reduce.addEventListener('change', wait);
  document.addEventListener('visibilitychange', wait);
  return {
    get active() { return frame > 0; },
    play() {
      held = false;
      if (time >= end) time = 0;
      if (!frame) drive();
    },
    pause() {
      held = true;
      stop();
    },
    takeOver,
  };
};
```

#### `readout`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// The figure's words, kept apart, each written only when its text differs
// from the last one it wrote. say(text) sets the visible readout el. Call it
// from the loop with where the parts are now. describe(text) sets
// aria-valuetext on target, the stage with role="slider". Call it when the
// state changes and never from the loop, since a screen reader speaks every
// new value text of a slider that has keyboard focus. phase(text) puts a
// phase word in live, a visually hidden element with aria-live="polite",
// once 700 ms pass with no newer phase, so a drag is spoken once it rests.
// With no target, describe does nothing; with no live, phase does nothing.
LivingUI.readout = (el, { target, live } = {}) => {
  let said, described, spoken, timer = 0;
  return {
    say(text) {
      if (text !== said) el.textContent = said = text;
    },
    describe(text) {
      if (target && text !== described) target.setAttribute('aria-valuetext', described = text);
    },
    phase(text) {
      clearTimeout(timer);
      if (!live) return;
      timer = setTimeout(() => {
        if (text !== spoken) live.textContent = spoken = text;
      }, 700);
    },
  };
};
```

#### `focusOne`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// Lights one element of a figure (rule F). chosen gets data-lui-on, every
// other element inside root loses it, and root gets data-lui-focus, so
// LivingUI.css lights chosen and dims the other data-lui-dim elements.
// chosen = null takes both away, so nothing is lit or dimmed. Calling it
// again with the same element changes nothing, and it never moves keyboard
// focus.
LivingUI.focusOne = (root, chosen) => {
  for (const el of root.querySelectorAll('[data-lui-on]')) {
    if (el !== chosen) el.removeAttribute('data-lui-on');
  }
  if (chosen) chosen.toggleAttribute('data-lui-on', true);
  root.toggleAttribute('data-lui-focus', !!chosen);
};
```

#### `explode`

```js
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// The exploded view (rule X). items holds one { el, move: [dx, dy] } for
// each part, in the order the parts leave. Each part rides its own spring
// with stiffness k and damping c, from 0, where the markup draws the part,
// to 1, where it sits moved by move. The defaults sit just under critical
// damping: a part passes its target by under 1% and lands about 1.4 s after
// it starts. open(true) starts the parts in order, each `stagger` ms after
// the one before, and open(false) brings them home in reverse order. A
// reversal part-way re-staggers only the parts whose target changes: a part
// still waiting to leave stays home, and the last one out is the first one
// back, at once. toggle() flips between open and closed. step(dt, calm)
// moves the parts, with dt in seconds as loop passes it, and returns true
// while a part still moves or waits; under calm every part lands on its
// target at once. A part's el gets a CSS translate in px (user units inside
// an SVG), written only when the part moves and taken away at 0, so the
// assembled figure is the markup itself.
LivingUI.explode = (items, { stagger = 35, k = 64, c = 14.2 } = {}) => {
  // clock is the view's own time in ms. Each part keeps its spring (x, v),
  // its target `to`, and `at`, when its target turns to goal.
  let clock = 0, goal = 0;
  const parts = items.map(({ el, move }) => ({ el, move, x: 0, v: 0, to: 0, at: Infinity }));
  const open = (apart) => {
    const next = apart ? 1 : 0;
    if (next === goal) return;
    goal = next;
    let n = 0;
    for (const part of apart ? parts : [...parts].reverse()) {
      part.at = part.to === goal ? Infinity : clock + stagger * n++;
    }
  };
  return {
    open,
    toggle() { open(!goal); },
    step(dt, calm) {
      clock += dt * 1000;
      let moving = false;
      for (const part of parts) {
        const from = part.x;
        if (calm || part.at <= clock) {
          part.to = goal;
          part.at = Infinity;
        }
        // Four substeps a frame keep a stiff spring stable.
        for (let i = 0; i < 4 && !calm; i++) {
          part.v += (k * (part.to - part.x) - c * part.v) * dt / 4;
          part.x += part.v * dt / 4;
        }
        if (calm || (Math.abs(part.to - part.x) < 5e-4 && Math.abs(part.v) < 5e-4)) {
          part.x = part.to;
          part.v = 0;
        } else {
          moving = true;
        }
        if (part.at < Infinity) moving = true;
        if (part.x !== from) {
          const [dx, dy] = part.move;
          part.el.style.transform = part.x ? `translate(${dx * part.x}px, ${dy * part.x}px)` : '';
        }
      }
      return moving;
    },
  };
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

## Looks

A look is an optional drawing style built on these rules and the toolkit. Every
look is opt-in: use one only when the user asks for it, never by default and
never because you like it.

- [`looks/isometric.md`](looks/isometric.md) draws the idea as a small machine
  shaded in four tones mixed from theme variables, for a request such as "draw
  it as a machine" or "make it isometric".

## Credits

Several of these rules restate ideas from
[anatomy](https://github.com/wheresryan22/anatomy), an MIT-licensed agent skill by
Ryan. Its licence notice is in [LICENSE-anatomy.md](LICENSE-anatomy.md) beside
this file.
