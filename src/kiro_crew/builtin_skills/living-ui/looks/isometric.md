Adapted from anatomy by Ryan (MIT License): see [living-ui/LICENSE-anatomy.md](../LICENSE-anatomy.md).

# Isometric look

A look is a drawing style. This one draws the idea as a small machine seen from
above and to one side: a solid object built from many parts, each face shaded in
one of four tones mixed from the theme. It sits on top of the rules and the
toolkit in the living-ui [SKILL.md](../SKILL.md), and every rule there still
applies.

## Opt-in only

This look is opt-in. Use it only when the user asks for it, for example "draw it
as a machine" or "make it isometric". It is never the default, and the agent
never picks it because it likes the style. A request covers the figures it was
made for and does not carry over to later ones. Without one, draw the figure
with no look, in the theme's own style.

## Make a real object

- Pick an object a reader can name at a glance, and invent it for the idea: a
  stand of cards for a queue of work, a rack of pigeonholes for routing, a row
  of switches for settings. Do not trace a picture of a real product.
- Build it from small parts that each rest on another and each have a job:
  feet under a base, a block on the base, cards in the block's slots, screws at
  the corners. The parts are the detail; add no texture or decoration.
- Turn layers into things that hold each other, such as cards in a stand or
  trays on rails, never loose slabs hanging in space.
- Every solid is opaque, so no hidden edge shows through, and both ends of
  every line touch a part.
- Keep the drawing free of labels, arrows and numbers. Words go in the card's
  corners and in the caption below it (rule W).

## The grid

Draw on the 2:1 grid most isometric art uses. A point `(x, y, z)` lands on the
screen at

```text
X = x - y
Y = (x + y) / 2 - z
```

so `x` runs to the lower right, `y` to the lower left and `z` straight up. Whole
numbers in give whole or half numbers out, so the points can be worked out by
hand. One `transform="translate(...)"` on a group places the whole object.

A box from corner `(x, y, z)`, `w` long in x, `d` deep in y and `h` high, shows
three faces. Each one is a four-point `<polygon>`:

| Face | Its corners | Tone |
|---|---|---|
| left, at `y + d` | `(x, y+d, z)`, `(x+w, y+d, z)`, `(x+w, y+d, z+h)`, `(x, y+d, z+h)` | `t1` |
| right, at `x + w` | `(x+w, y, z)`, `(x+w, y+d, z)`, `(x+w, y+d, z+h)`, `(x+w, y, z+h)` | `t3` |
| top, at `z + h` | `(x, y, z+h)`, `(x+w, y, z+h)`, `(x+w, y+d, z+h)`, `(x, y+d, z+h)` | `t2` |

Paint from back to front: a part with a smaller `x + y` comes first, and a part
comes before anything that rests on it. A part that moves sits in its own `<g>`
at its place in that order, and only the group's `transform` changes.

## Tones

Each tone is `color-mix(in oklab, var(--card) N%, var(--text))`, the card colour
with a share of the text colour. The lit element mixes toward `var(--accent)`
instead, with deeper steps, so it is the one thing that stands out.

| Tone | Paints | Plain part, N | Lit element, N |
|---|---|---|---|
| `t1` | left faces | 92 | 70 |
| `t2` | top faces | 84 | 55 |
| `t3` | right faces | 74 | 40 |
| `t4` | edges, slots, screw heads | 50 | edges in `var(--accent)` |

- **Tops take the middle step.** In a light theme the left faces are the
  lightest, so the light seems to come from the left. A dark theme flips the
  order and the light seems to come from the right. The tops stay in the middle
  either way, so the object never looks lit from below.
- **Keep the three face tones close**, so the object reads as one material.
  Only the edges sit far from the card colour.
- **Draw on `var(--card)`.** The tones start from it, so the object sits on the
  colour it is mixed from.
- **No colour of your own.** Every tone comes from theme variables, so the
  figure follows light, dark and custom palettes with no change.
- **One lit element.** Put `data-lui-on`, the attribute `LivingUI.css` lights,
  on the `<g>` that holds that part's faces. The look's CSS then gives those
  faces the accent steps and their edges `var(--accent)` (rule F).
- **No dimming by opacity.** A solid at lower opacity turns see-through, so leave
  the other parts in their plain tones, without `data-lui-dim`. Beside the lit
  element they already read as quieter.

## Example

A still figure: a card stand holding a queue of four work items, with the third
card raised because it is in progress. It works as a widget body or inside any
page that sets the theme variables. If the freeze-one-frame test says it should
move, keep this markup as the settled state and drive the lit card's group
`transform` with the toolkit's `spring`.

```html
<style>
  .iso { margin: 0 }
  .iso-card { position: relative; padding: 2.5em 1em 1em; color: var(--text); background: var(--card); border: 1px solid var(--border) }
  .iso-title, .iso-readout { position: absolute; top: .9em; font-size: .85em }
  .iso-title { left: 1em; color: var(--muted) }
  .iso-readout { right: 1em; color: var(--text-strong); font-variant-numeric: tabular-nums }
  .iso svg { display: block; width: 100%; height: auto }
  .iso polygon, .iso ellipse { stroke: color-mix(in oklab, var(--card) 50%, var(--text)); stroke-width: .75; stroke-linejoin: round }
  .iso .t1 { fill: color-mix(in oklab, var(--card) 92%, var(--text)) }
  .iso .t2 { fill: color-mix(in oklab, var(--card) 84%, var(--text)) }
  .iso .t3 { fill: color-mix(in oklab, var(--card) 74%, var(--text)) }
  .iso .t4 { fill: color-mix(in oklab, var(--card) 50%, var(--text)) }
  .iso [data-lui-on] polygon { stroke: var(--accent) }
  .iso [data-lui-on] .t1 { fill: color-mix(in oklab, var(--card) 70%, var(--accent)) }
  .iso [data-lui-on] .t2 { fill: color-mix(in oklab, var(--card) 55%, var(--accent)) }
  .iso [data-lui-on] .t3 { fill: color-mix(in oklab, var(--card) 40%, var(--accent)) }
  .iso figcaption { margin-top: .6em; color: var(--muted); font-size: .85em }
</style>
<figure class="iso">
  <div class="iso-card">
    <span class="iso-title">Work queue</span>
    <span class="iso-readout">Item 3 of 4 · in progress</span>
    <svg viewBox="0 0 280 208" role="img" aria-label="A card stand holding four cards. The third card is raised.">
      <g transform="translate(82 71)">
        <polygon class="t1" points="-12,10 0,16 0,8 -12,2"/>
        <polygon class="t3" points="12,10 0,16 0,8 12,2"/>
        <polygon class="t2" points="0,-4 12,2 0,8 -12,2"/>
        <polygon class="t1" points="-56,32 -44,38 -44,30 -56,24"/>
        <polygon class="t3" points="-32,32 -44,38 -44,30 -32,24"/>
        <polygon class="t2" points="-44,18 -32,24 -44,30 -56,24"/>
        <polygon class="t1" points="148,90 160,96 160,88 148,82"/>
        <polygon class="t3" points="172,90 160,96 160,88 172,82"/>
        <polygon class="t2" points="160,76 172,82 160,88 148,82"/>
        <polygon class="t1" points="104,112 116,118 116,110 104,104"/>
        <polygon class="t3" points="128,112 116,118 116,110 128,104"/>
        <polygon class="t2" points="116,98 128,104 116,110 104,104"/>
        <polygon class="t1" points="-64,24 116,114 116,106 -64,16"/>
        <polygon class="t3" points="180,82 116,114 116,106 180,74"/>
        <polygon class="t2" points="0,-16 180,74 116,106 -64,16"/>
        <ellipse class="t4" cx="0" cy="-9" rx="3.5" ry="1.8"/>
        <ellipse class="t4" cx="-50" cy="16" rx="3.5" ry="1.8"/>
        <polygon class="t1" points="-32,12 124,90 124,72 -32,-6"/>
        <polygon class="t3" points="160,72 124,90 124,72 160,54"/>
        <polygon class="t2" points="4,-24 160,54 124,72 -32,-6"/>
        <polygon class="t4" points="20,-11 26,-8 0,5 -6,2"/>
        <polygon class="t4" points="56,7 62,10 36,23 30,20"/>
        <polygon class="t4" points="92,25 98,28 72,41 66,38"/>
        <polygon class="t4" points="128,43 134,46 108,59 102,56"/>
        <polygon class="t1" points="-2,1 2,3 2,-41 -2,-43"/>
        <polygon class="t3" points="22,-7 2,3 2,-41 22,-51"/>
        <polygon class="t2" points="18,-53 22,-51 2,-41 -2,-43"/>
        <polygon class="t1" points="34,19 38,21 38,-23 34,-25"/>
        <polygon class="t3" points="58,11 38,21 38,-23 58,-33"/>
        <polygon class="t2" points="54,-35 58,-33 38,-23 34,-25"/>
        <g data-lui-on>
          <polygon class="t1" points="70,37 74,39 74,-25 70,-27"/>
          <polygon class="t3" points="94,29 74,39 74,-25 94,-35"/>
          <polygon class="t2" points="90,-37 94,-35 74,-25 70,-27"/>
        </g>
        <polygon class="t1" points="106,55 110,57 110,13 106,11"/>
        <polygon class="t3" points="130,47 110,57 110,13 130,3"/>
        <polygon class="t2" points="126,1 130,3 110,13 106,11"/>
        <polygon class="t1" points="-2,56 50,82 50,76 -2,50"/>
        <polygon class="t3" points="52,81 50,82 50,76 52,75"/>
        <polygon class="t2" points="0,49 52,75 50,76 -2,50"/>
        <ellipse class="t4" cx="166" cy="74" rx="3.5" ry="1.8"/>
        <ellipse class="t4" cx="116" cy="99" rx="3.5" ry="1.8"/>
      </g>
    </svg>
  </div>
  <figcaption>Each card is a work item, waiting in order. The raised card is the one in progress.</figcaption>
</figure>
```
