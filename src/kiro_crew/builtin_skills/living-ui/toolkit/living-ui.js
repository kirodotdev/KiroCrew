// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
//
// Living UI toolkit: small helpers for live figures, with no colours,
// fonts or shapes of their own. Each helper sits between a "living-ui:begin
// <name>" line and a "living-ui:end <name>" line and stands on its own, so a
// figure copies only the blocks it uses into its inline <script>. The
// living-ui SKILL.md carries the same blocks for agents that cannot open this
// file, and a test fails when the two differ: edit the code here, then copy
// each changed block across unchanged.

// living-ui:begin loop
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
// living-ui:end loop

// living-ui:begin follow
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
// living-ui:end follow

// living-ui:begin spring
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
// living-ui:end spring

// living-ui:begin settle
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
// living-ui:end settle

// living-ui:begin smooth
// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
var LivingUI = LivingUI || {};
// Smoothstep: 0 at or below a, 1 at or above b, and between them an S-curve
// that starts and ends flat. a must be less than b.
LivingUI.smooth = (a, b, x) => {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
};
// living-ui:end smooth

// living-ui:begin burst
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
// living-ui:end burst

// living-ui:begin tour
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
// living-ui:end tour

// living-ui:begin readout
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
// living-ui:end readout

// living-ui:begin focusOne
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
// living-ui:end focusOne

// living-ui:begin explode
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
// living-ui:end explode

// living-ui:begin css
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
// living-ui:end css
