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
the figure. If that frame carries the meaning, the figure stays still, and only
rules F (focus) and W (words) apply.

The rules after the gate say how to move once motion has earned its place. None
of them is a reason to make something move.

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
tour all move that one focus. Nothing lights a second element.

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
- **Offscreen means stopped.** Watch the figure with an `IntersectionObserver`
  (`rootMargin: "120px 0px"`), act on the newest entry in each callback, and stop
  the loop while the figure is out of view.
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
  region (`aria-live="polite"`) that is empty at load and filled on focus.
- **Colour is never the only signal.** The readout's words also name the lit
  element.
- **The tour yields.** Any input stops it at once, and it never moves focus.

## Credits

Several of these rules restate ideas from
[anatomy](https://github.com/wheresryan22/anatomy), an MIT-licensed agent skill by
Ryan. Its licence notice is in [LICENSE-anatomy.md](LICENSE-anatomy.md) beside
this file.
