# Screenshots for the dashboard package renderers (#18642)

Evidence branch for the renderers PR of the Dynamic Dashboard v3 stack. Images
only, so the implementation branch stays free of binaries.

Every block type is shown in BOTH themes, because the renderers read theme
tokens rather than hard-coded colour, and a single-theme shot cannot show that.

## R1 -- every block type renders its own markup

One pair per block type. The claim each pair proves is that the type draws its
own structure rather than falling back to a generic box.

| block type | light | dark |
|---|---|---|
| `stat` | `shots/block-stat-light.png` | `shots/block-stat-dark.png` |
| `stat-band` | `shots/block-stat-band-light.png` | `shots/block-stat-band-dark.png` |
| `table` | `shots/block-table-light.png` | `shots/block-table-dark.png` |
| `list` | `shots/block-list-light.png` | `shots/block-list-dark.png` |
| `timeline` | `shots/block-timeline-light.png` | `shots/block-timeline-dark.png` |
| `bars` | `shots/block-bars-light.png` | `shots/block-bars-dark.png` |
| `gauge` | `shots/block-gauge-light.png` | `shots/block-gauge-dark.png` |
| `pills` | `shots/block-pills-light.png` | `shots/block-pills-dark.png` |
| `note` | `shots/block-note-light.png` | `shots/block-note-dark.png` |
| `orbit` | `shots/block-orbit-light.png` | `shots/block-orbit-dark.png` |
| `orbit`, reduced-motion flat form | `shots/block-orbit-flat-light.png` | `shots/block-orbit-flat-dark.png` |

## R2 -- a whole page composes from a package

| file | what it proves |
|---|---|
| `shots/page-briefing-light.png`, `shots/page-briefing-dark.png` | a realistic single-purpose page, the shape a crewmate's report actually takes |
| `shots/page-every-block-light.png`, `shots/page-every-block-dark.png` | every block type on one page, so a type that breaks its neighbours' layout is visible |

## R3 -- a page can be patched one block at a time

| file | what it proves |
|---|---|
| `shots/patch-before-dark.png` | the page before the patch |
| `shots/patch-after-dark.png` | after it: only the patched block's values changed, and no other block re-initialised |

## R4 -- the states a page can be in, drawn honestly

| file | what it proves |
|---|---|
| `shots/state-no-values-light.png`, `shots/state-no-values-dark.png` | a layout whose folds carry nothing yet: the blocks are drawn with their labels and no invented numbers |
| `shots/state-stale-band-light.png`, `shots/state-stale-band-dark.png` | the stale band, which says the values on screen are not current rather than letting them read as live |
| `shots/state-reduced-motion-light.png`, `shots/state-reduced-motion-dark.png` | `prefers-reduced-motion`: the animated blocks render their flat form instead |

## R5 -- theme tokens are applied, and hostile ones are dropped

| file | what it proves |
|---|---|
| `shots/theme-tokens-applied-light.png`, `shots/theme-tokens-applied-dark.png` | the package's own theme tokens reach the page |
| `shots/theme-hostile-dropped-light.png`, `shots/theme-hostile-dropped-dark.png` | a token that tries to close its own CSS declaration is dropped, and the page still renders with the default |

## What is real in these captures

The renderer output is real: every page here is the actual HTML
`dashboard_package_render` produced for a validated package, loaded in a
browser with the document's own stylesheet and the vendored scripts. The VALUES
are fixtures. The numbers themselves prove nothing beyond being formatted by
the renderer's own rules.
