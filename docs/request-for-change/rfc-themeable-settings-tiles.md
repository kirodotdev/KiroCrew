---
title: Themeable Settings icon tiles — a custom theme recolours the tiles through palette roles it already sets
status: in-progress
author: leozhad
created: 2026-11-02
last-audited: 2026-11-08
audited-at: 25c084866d
doc-pr: 28765
implementation-prs: [28765]
tracking-issues: [23670]
supersedes: []
superseded-by: []
---

# RFC: Themeable Settings Icon Tiles

> **Status:** `in-progress`. The design and its implementation ship together in
> [#28765](https://github.com/kirodotdev/KiroCrew/pull/28765), which asks a
> maintainer to accept the design. The baseline below was verified at
> `25c084866d`. Tracks [#23670](https://github.com/kirodotdev/KiroCrew/issues/23670).

## Summary

In a custom theme (an installed pack or an editor theme), each Settings section
icon tile takes the fill of a palette role the theme already sets, and its glyph
takes that role's own `-fg` ink. Built-in themes keep today's fixed system hues
and white glyph. No variable is added to the pack allowlist and the pack format
does not change.

This reverses, for custom themes only, a recorded decision: that a section's
tile is an identity mark that must look the same in every theme. The decision
was deliberate, and the section "Does the identity-mark goal still hold?" below
says what this proposal keeps of it and what it gives up.

## Motivation

### Current state (verified at `25c084866d`)

- **The hues are fixed on purpose.** `website/src/styles/liquid-glass.css:80-95`
  defines nine `--tile-*` hues and one glyph ink, `--tile-fg: #fff`, in a
  `:root` block and a deeper `[data-mode="light"]` block. Its comment
  (lines 82-83): "Fixed hues by design -- a tile is an identity mark for its
  section, and the same section must look the same in every theme".
  `website/docs/theming-contract.md:120-126` records the same rule: "a section's
  tile is an identity mark that must look the same in every theme".
- **Where it came from.** [#14460](https://github.com/kirodotdev/KiroCrew/pull/14460)
  (@CrysisDeu, merged 2026-09-27 as `61e100dedd`) added the tiles and listed under
  Not a goal: "Making the glass or tile colours theme-pack overridable. They are
  polarity-fixed on purpose". The First Principles lane blocked an earlier
  revision of [#23552](https://github.com/kirodotdev/KiroCrew/pull/23552) on that
  rule, and the tile change was dropped from it before it merged.
- **One reader.** `SidePanelTab.tile` in
  `website/src/components/SidePanelLayout.tsx` is a CSS value (`tile?: string`),
  painted as `style={{ background: t.tile }}` with the glyph in
  `text-[color:var(--tile-fg)]`, on the mobile root list (30 px) and on the
  desktop rail (20 px). The only caller that sets it is `buildTabs()` in
  `website/src/pages/SettingsPage.tsx`: 23 tabs on 9 hues.
  `git grep -e '--tile-'` finds no other code reader.
  `docs/system-specs/modules/crew-mode.md:2621` says the crewmate goal colours
  are tile hues, but `GOAL_COLORS` in `website/src/pages/members/CrewGoals.tsx`
  reads `--goal-1` to `--goal-5`, so that spec line is stale.
- **Pinned.** The test "defines every --tile-* token for both polarities in
  index.css" in `website/src/test/SidePanelLayout.iconTile.test.tsx` checks that
  every `--tile-*` token `SettingsPage.tsx` uses is defined in both blocks.
- **Not pack-settable as a variable.** No `--tile-*` name is in
  `ALLOWED_CSS_VARS` (`website/src/hooks/themeCss.ts`) or the backend
  `_THEME_CSS_VARS` (`src/kiro_crew/dashboard/theme_validate.py`), so a
  `variables.json` and the theme editor cannot set them.
- **But a pack stylesheet can.** A Level 1 pack's `styles/overrides.css` may
  declare custom properties on its allowlisted surfaces (`_ALLOWED_ELEMENTS`
  and `_ALLOWED_CLASSES` in `themeCss.ts`). `scopeOverridesCss` keeps
  `body{--tile-blue:#ff0000}` and `[data-theme="custom-x-dark"] body{--tile-blue:#ff0000}`,
  and drops only `:root{...}`; the install validator `_validate_overrides_css`
  accepts both. The
  inherited value on `body` reaches every tile. So the hues are fixed for
  colour-only packs and editor themes, and only by convention for Level 1 packs;
  the contract's sentence that "a pack cannot override" the polarity-fixed
  variables (`theming-contract.md:90-92`) does not hold for this set.

### The problem

A custom theme is one palette someone chose. On a narrow palette such as a
green-on-black pack, every surface of the Settings page follows it except the
rail, which stays system blue, pink, orange and indigo:
[green pack, dark, on main](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/before-pack-dark.png),
[green pack, light, on main](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/before-pack-light.png).

The fixed hues also carry one glyph ink, white, whatever the hue. Measured on the
built bundle (computed styles in headless Chromium), white on the dark hues is
1.99:1 (teal) to 5.06:1 (indigo), and on the light hues 2.20:1 (orange) to
5.65:1 (indigo). 18 of the 20 tiles the rail renders in that setup are below
4.5:1, in either polarity, in every theme, custom ones included.

## Goals

1. A custom theme's Settings rail uses that theme's palette.
2. In a custom theme each glyph is paired with the ink the theme defines for
   text on that fill, so the glyph reads wherever the theme's own filled buttons
   and badges read.
3. Built-in themes render exactly as today.
4. Pack authors set nothing new, and every installed pack keeps working.

## Non-goals

- Changing any built-in theme's tiles, including the low contrast of the white
  glyph on them.
- Adding `--tile-*` (or any variable) to the allowlist, or changing the pack
  format.
- Changing which hue each Settings section uses, or the tiles' shape, size or
  glyphs.
- Recolouring other tiles. The welcome suggestions already use role tokens
  (`SUGGESTION_KIND_STYLE` in `website/src/components/WelcomeView.tsx`), and the crewmate goal marks
  use `--goal-*`.
- Deciding the fate of the `overrides.css` route described above (see Open
  questions).

## Design

### Which themes

`[data-theme^="custom-"]`. `buildCustomThemeCss` writes each custom palette as
`[data-theme="custom-<slug>-dark"]` and `-light`, and an installed pack and an
editor theme are both listed as `custom-<slug>` (the `ColorTheme` type in
`website/src/hooks/useTheme.tsx`: "built-in slug or 'custom-{slug}'"). No value
in `THEMES` (same file) starts with `custom-`. A downstream theme added through
`registerTheme()` is a built-in theme; one whose value started with `custom-`
would get the mapping too, and the core registers none.

### The role map

| Tile hue | Fill | Glyph |
|---|---|---|
| gray | `--muted` | `--muted-fg` |
| blue, teal | `--info` | `--info-fg` |
| green | `--ok` | `--ok-fg` |
| indigo | `--accent` | `--accent-fg` |
| orange | `--warn` | `--warn-fg` |
| pink, purple | `--aim` | `--aim-fg` |
| red | `--danger` | `--danger-fg` |

Each hue goes to the role nearest its meaning in the stock palette. Nine hues
share seven roles: teal joins blue on `--info`, and pink joins purple on `--aim`.
Every fill and every ink is already in the allowlist, and each `-fg` token exists
for text on its own fill: `bg-<role> text-<role>-fg` is how the theme's filled
buttons, badges and status pills are painted.

### Glyph ink per hue

Each hue gains a `--tile-<hue>-fg`. In `:root` every one is `var(--tile-fg)`, so a
built-in theme paints the same white glyph. The custom block sets it to the
role's `-fg`:

```css
[data-theme^="custom-"] {
  --tile-gray: var(--muted);    --tile-gray-fg: var(--muted-fg);
  --tile-blue: var(--info);     --tile-blue-fg: var(--info-fg);
  --tile-teal: var(--info);     --tile-teal-fg: var(--info-fg);
  --tile-green: var(--ok);      --tile-green-fg: var(--ok-fg);
  --tile-indigo: var(--accent); --tile-indigo-fg: var(--accent-fg);
  --tile-orange: var(--warn);   --tile-orange-fg: var(--warn-fg);
  --tile-pink: var(--aim);      --tile-pink-fg: var(--aim-fg);
  --tile-purple: var(--aim);    --tile-purple-fg: var(--aim-fg);
  --tile-red: var(--danger);    --tile-red-fg: var(--danger-fg);
}
```

The block sits after the `[data-mode="light"]` block. Both selectors have the
same specificity, so source order decides, and a test pins the order. Everything
resolves on `<html>`, where the custom palette block also applies.

`SidePanelTab.tile` becomes a hue name (`'gray' | 'blue' | … | 'red'`) instead of a
CSS value, so the layout can pair each fill with its ink:
`background: var(--tile-<hue>)`, `color: var(--tile-<hue>-fg)`. The type is
internal to `website/src`, and `SettingsPage.tsx` is its only caller.

### What does not change

The allowlist on both sides and its parity test, the pack format, the install
validator, the theme editor, and every built-in theme's pixels.

## Does the identity-mark goal still hold?

- **Built-in themes: yes.** The Settings rail renders byte-identical PNGs before
  and after the change, in built-in dark and built-in light.
- **Custom themes: partly.** The glyph, label, position and group of every section
  are unchanged, and a section keeps the same role in every custom theme (Chat is
  always the `--ok` tile). The hue itself becomes the theme's, and sections can
  share a colour when a theme gives several roles the same one. In the green
  test pack's light palette `--ok`, `--accent` and `--aim` are all `#007538`, so
  its 20 rendered tiles show 5 distinct fills (7 in its dark palette, 9 under the
  fixed hues).
- **What the colour identified before.** 23 tabs share 9 hues today (gray alone
  marks 5), so a tile's colour already grouped sections rather than naming one;
  the glyph and the label name it. The light block already shifts every hue but
  gray.

So the decision being reversed is that a constant colour outranks the palette
the user picked. This proposal holds that inside a theme someone chose, the
palette wins, and keeps the old rule wherever no one chose.

## Migration plan

### Phase 1 — the mapping (one PR: [#28765](https://github.com/kirodotdev/KiroCrew/pull/28765))

The custom-theme block and the `--tile-<hue>-fg` tokens in `liquid-glass.css`,
the hue-name `SidePanelTab.tile`, the 23 `SettingsPage.tsx` values, the theming
contract, the `theme-pack-authoring` skill, and the stale `crew-mode.md` line.

Exit criteria:

- Built-in dark and built-in light Settings rails render byte-identical PNGs
  from the built bundle before and after.
- In a custom theme every rendered tile's computed background equals its mapped
  role and its computed colour equals that role's `-fg`.
- `SidePanelLayout.iconTile.test.tsx` fails on main (all 9 tests) and passes
  with the change; it pins the map onto allowlisted roles and their own inks,
  the block order, and 4.5:1 or better on every tile for sparse packs that
  declare no ink.

### Phase 2 — an author opt-out (blocked on Open question 1)

Only if Open question 1 is answered "opt-in" or "default with opt-out": a
`theme.json` key, added to both validators and the skill, that keeps the fixed
hues for that pack. Skipped if the answer is "default, no flag".

## Backward compatibility

- **Built-in themes:** unchanged, as above.
- **Installed packs:** the rail changes on upgrade with no action from the
  author. The green test pack's palette (taken from the public
  `leozhad/kirocrew-theme-matrix` pack) declares every role and every ink; its
  tiles measure 6.14:1 to 18.63:1 in dark and 5.62:1 to 6.19:1 in light.
- **The contrast a custom theme gets:**
  - A role the theme omits keeps the `:root` pair: `--muted` 6.63, `--info`
    5.70, `--ok` 9.22, `--accent` 10.83, `--warn` 10.95, `--aim` 7.72,
    `--danger` 5.58 (ratio to 1).
  - An ink the theme omits for a fill it declares is derived black or white by
    the fill's luminance (`derivedDefaults` in `themeCss.ts`), which is at least
    4.58:1 on any opaque fill.
  - An omitted `--muted-fg` is derived as `--bg` and an omitted `--muted` as a
    75% step toward `--text` (the same function), so the gray tile
    reads as well as the theme's own secondary text does on its page.
  - An ink the theme declares is used as written, as it already is on that
    role's filled controls. Nothing in this proposal raises a declared ink that
    is too weak.
- **Measured across palettes.** Using each of the 42 built-in palettes as if a
  pack had copied it (the skill tells authors how to clone one): 22 of the 378
  hue cases are below 4.5:1 under the mapping, against 336 under the fixed white
  glyph. 17 cases are lower than today, and 6 of those drop below 4.5:1: the
  `--aim` pair of `amoled-grey-calm-dark` (2.72:1 on pink and purple, against
  3.52:1), of `kiro-light` (3.82:1) and of `intellij-light` (3.90:1) on purple,
  and the `--accent` pair of `amoled-dark` (3.65:1) and `amoled-midnight-dark`
  (3.85:1) on indigo. These are the same weak pairs
  `website/src/test/themeFillForeground.test.ts` records (its `KNOWN_BELOW_AA`
  list, and its header note that `aim` is not covered).
- **A pack that sets `--tile-*` in `overrides.css`** keeps its fills, but each
  glyph moves from `--tile-fg` to the role's `-fg`. No pack known to this
  document does this.

## Security considerations

The change is static CSS plus a narrower TypeScript type. It adds no pack input,
no allowlisted variable, and no parser or sanitizer path. The values it reads are
tokens `buildCustomThemeCss` already sanitizes (`escapeCssValue`) before they
reach the page. A hostile pack can already make these role pairs unreadable on
its own buttons; it can make the tiles unreadable the same way and gains nothing
else.

## Alternatives considered

1. **Keep the tiles fixed (today).** A section looks the same in every theme and
   nothing changes. Every custom palette gets an off-palette rail, and the white
   glyph stays below 4.5:1 on 18 of 20 tiles.
2. **Opt in per pack.** A `theme.json` flag turns the mapping on, and every
   other pack keeps the fixed hues. No installed pack changes without its author.
   It is a pack-format change (both validators and the skill), editor themes need
   a toggle or are left out, and the default stays off-palette for the authors
   who never learn the flag.
3. **Allowlist the `--tile-*` variables.** A pack sets each hue itself: the most
   control. It adds nine variables per mode (eighteen with inks) to both
   allowlists and their parity test, every author has to learn them, and nothing
   keeps a glyph readable unless the derivation gains nine more rules. Level 1
   authors who want this control can already get it through `overrides.css`, as
   described above.
4. **Generate nine hues from the pack's accent.** Rejected: it invents colours
   the author never chose, which is the complaint.

## Open questions

1. **Default or opt-in for custom themes?** This proposal makes the mapping the
   default for every custom theme, with no flag. The alternative is Phase 2.
2. **Do editor themes count?** Proposed: yes. They are custom palettes with the
   same roles and the same `custom-` selector. Excluding them would need a
   separate marker for installed packs, since both use `custom-<slug>`.
3. **Should the install validator or the theme editor warn when a declared `-fg`
   reads below 4.5:1 on its fill?** That would cover the tiles and the theme's
   buttons alike, and is a separate change.
4. **The `overrides.css` route for `--tile-*`.** Document it, close it, or leave
   it? Today the contract says a pack cannot override the polarity-fixed
   variables, and the scoper lets it.
5. **The built-in tiles' white glyph** is below 4.5:1 on 18 of 20 tiles. Is that
   acceptable for a decorative mark next to a text label, or a separate issue?

## Evidence

Frames are 2x captures of the Settings rail from the built bundle, served
locally with stubbed API answers, in headless Chromium with motion off. The
"after" frames come from the implementation's head; the custom theme is the green
test pack described above, loaded through the real `/api/themes` path with its
palette only (no fonts or stylesheet).

- On main: [green pack, dark](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/before-pack-dark.png),
  [green pack, light](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/before-pack-light.png)
- With the change: [built-in dark, unchanged](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/after-builtin-dark.png),
  [built-in light, unchanged](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/after-builtin-light.png),
  [green pack, dark](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/after-pack-dark.png),
  [green pack, light](https://raw.githubusercontent.com/leozhad/KiroCrew/71d8b32577412edc9afbf336492717a35badce59/temp-screenshots/themeable-settings-tiles/after-pack-light.png)

The per-palette ratios come from the repository's own resolver and contrast
helpers (`website/src/test/themePalette.ts`, `website/src/lib/iconContrast.ts`)
and `buildCustomThemeCss`.
