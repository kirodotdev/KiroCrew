// Adapted from anatomy by Ryan (MIT License): see living-ui/LICENSE-anatomy.md.
//
// Living UI toolkit: small motion helpers for live figures, with no colours,
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
