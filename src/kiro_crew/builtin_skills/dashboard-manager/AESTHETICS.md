# Dashboard aesthetics

A dashboard is something a person opens every day. It should feel alive and made
for *this* work, not like an internal admin console. Engineering correctness is
the floor, not the goal.

Read this with `SKILL.md`. Where the two meet, section 2a's hard limits win:
nothing here licenses a second screen.

## 1. Start from the subject, not from "dashboard"

Before choosing anything visual, write one line: who opens this page, what their
work is about, and what mood it should carry.

- A launch week feels like a countdown. A research project feels like a
  notebook. A sales pipeline feels like a scoreboard. A hiring loop feels like a
  roster.
- Pick type, colour and motion from that subject. If you could swap in any other
  project and the page would look the same, start over.
- Follow the current theme of the work: a holiday campaign, a quarter close, a
  product launch, a conference week. Let the season show.

## 2. Be lively

The page should reward a glance.

- **Motion with meaning.** Numbers count up when they change. A finished task
  settles into place. A stuck item breathes slowly. Every animation says
  something happened; nothing loops for decoration.
- **Depth where it explains.** The `orbit` block turns numbers into a ring a
  person can drag: reach for it when the shape of the work is spatial or
  layered, not to add a 3D object to a flat page. Flat is fine when flat is
  clearer.
- **Character in the type.** `--display-font` for the headline and the big
  figures, `--text-font` for everything else, `--mono-font` for tabular figures.
  Large, confident numbers. Real hierarchy, not three sizes of bold.
- **Colour with a point of view.** Override `--accent` to something that fits
  the subject and let the rest stay quiet. Avoid the default blue-purple SaaS
  look and the default green-on-black terminal look unless the subject truly
  calls for them.
- **Small delights.** A headline that knows the time of day, a short celebration
  when a project closes, an empty state that is friendly rather than blank.

## 3. Stay readable

Lively must never cost the five answers.

- The 10-second test still applies: done, spent, left, stuck, needs you.
- Motion runs once per change and finishes in under a second. Honour
  `prefers-reduced-motion`: show the end state with no movement.
- `orbit` always has its flat fallback reachable, and every number inside it is
  also readable as text.
- Status is never colour alone. `pills` pairs a shape and a word with the colour
  for exactly this reason.
- Every colour you name has a light and a dark value, and text keeps contrast in
  both. Check both renders, not one.

## 4. Technical limits you design within

- The page runs in a sandbox with CSP `default-src 'none'`: no network, no CDN,
  no remote fonts or images. Three.js and the animation library come from the
  vendored set the renderer ships.
- No agent-authored code runs. You choose types, blocks and theme; the renderers
  are the product's.
- Keep it light: interactive in under a second on a laptop, and a 3D block holds
  60 fps at the fleet sizes we have.
- Style lives in `theme.tokens` (at most 64) and `theme.css` (at most 32 KB,
  sanitized). A token whose name or value would not survive the sanitizer is
  dropped silently, so verify a token by its **computed** value in a render, not
  by having declared it.

## 5. Self-check before you hand it over

1. Render headless in light and dark at 1280x800, and look at both screenshots.
2. Ask: does this look like *this* project, or like any dashboard?
3. Ask: would the person smile once, and can they still answer the five
   questions in 10 seconds?
4. Read the four numbers from `SKILL.md` section 9 out of the page.
5. If any answer is no, change it and render again.

## Example moods (starting points, not rules)

| Mood | Feels like | Typical moves |
|---|---|---|
| briefing | A printed weekly paper | Serif headline, one accent, numbered asks |
| mission | A launch control room | Countdown, `orbit` fleet, glowing `gauge` |
| studio | A design studio wall | Big colour fields, playful type, few figures |
| notebook | A research journal | Margin notes, soft ground, quiet accent |
| scoreboard | A sports scoreboard | Huge `stat`, bold accent, `stat_band` ticker |
