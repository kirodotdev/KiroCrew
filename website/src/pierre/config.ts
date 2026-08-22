/**
 * Central configuration for every Pierre (`@pierre/diffs` / `@pierre/trees`)
 * surface in the dashboard.
 *
 * All code/diff/tree rendering goes through the wrappers in `./index`, and
 * every wrapper resolves its options through this module — so the look and
 * behavior of ALL surfaces (chat blocks, diff panels, file viewer, workspace
 * tree) is changed here, once, rather than per-surface.
 *
 * Type-only imports keep this module out of the heavy lazy chunk: the actual
 * `@pierre/diffs` runtime is only reachable through `./PierreImpl`.
 */
import type { BaseCodeOptions, BaseDiffOptions, HunkSeparators, ThemesType, ThemeTypes } from '@pierre/diffs'
import type { CodeViewReactOptions } from '@pierre/diffs/react'

/** Diff options as our surfaces use them: the deprecated `custom` hunk
 *  separator (a function renderer) is excluded so the shape stays assignable
 *  to Pierre's component-level `FileDiffOptions`. */
export type PierreDiffOptions = Omit<BaseDiffOptions, 'hunkSeparators'> & {
  hunkSeparators?: Exclude<HunkSeparators, 'custom'>
}

/** The Shiki theme pair every Pierre surface renders with. One knob for the
 *  whole dashboard: swap the pair here to restyle all code and diff surfaces
 *  at once. Accepts any Shiki bundled theme name or a registered custom one. */
export const PIERRE_THEMES: ThemesType = { dark: 'pierre-dark', light: 'pierre-light' }

/** Dashboard themes are user-selected (not OS-derived), so Pierre is always
 *  driven with an explicit light/dark — never 'system'. */
export function pierreThemeType(isDark: boolean): ThemeTypes {
  return isDark ? 'dark' : 'light'
}

/** Compact file-header geometry for the chat surfaces (inline file rows, chat
 *  diff blocks), where Pierre's default header reads oversized next to 14px
 *  chat prose.
 *
 *  Pierre derives the band from `min-height: calc(1lh + (--diffs-gap-block * 3))`
 *  — its 44px default is a 20px line-height plus 8px × 3 — so shrinking the
 *  line-height and the block gap shrinks the band without hardcoding a height.
 *  Both are scoped to the header element: `--diffs-line-height` also drives code
 *  row layout, so it must NOT be narrowed globally.
 *
 *  The change icon is a 16×16 SVG sized by width/height ATTRIBUTES, which CSS
 *  overrides. Applied through `unsafeCSS`, which lands in the shadow root under
 *  `@layer unsafe` and so outranks the library's own `@layer base`.
 *
 *  Band: 18px + 6px × 3 = 36px. */
export const PIERRE_COMPACT_HEADER_CSS = `
[data-diffs-header]{--diffs-gap-block:6px;font-size:12px;line-height:18px}
[data-change-icon]{width:13px;height:13px}
`

/** Gives every collapsed-region separator the separator tint.
 *
 *  Pierre applies `--diffs-bg-separator` only to its `metadata`, `line-info-basic`
 *  and `simple` separator variants; the rest sit on the plain canvas, so a
 *  collapsed stretch can read as a gap rather than a band. This puts them all on
 *  the same tone. Layout and label stay Pierre's. */
export const PIERRE_SEPARATOR_BG_CSS = `
[data-separator]{background-color:var(--diffs-bg-separator)}
`

/** Removes the phantom horizontal scroll on `overflow: 'wrap'` surfaces.
 *
 *  The `overflow` option controls line WRAPPING, not the container: the code
 *  element always carries `overflow: var(--diffs-overflow-override, scroll) clip`,
 *  so it stays scrollable on the x-axis even when every line wraps and there is
 *  nothing to reach — which reads as a few px of drift plus a reserved scrollbar
 *  gutter. `auto` rather than `hidden` because wrapping breaks on whitespace: a
 *  single unbreakable token (minified source, a base64 blob) genuinely does
 *  overflow, and `hidden` would put it out of reach instead of just quiet. */
export const PIERRE_WRAP_NO_HSCROLL_CSS = `
[data-code]{--diffs-overflow-override:auto}
`

/** Aligns the edit caret and selection overlay with the text they mark.
 *
 *  `Editor.#getLineY` sums `lineElement.offsetTop + metrics.paddingTop`, reading
 *  that padding off `[data-code]`. On the SPLIT surface the library also sets
 *  `[data-code] { display: contents }`, so that element generates no box and its
 *  `padding-top` contributes nothing to layout — yet the metric still reports it,
 *  so the sum carries a term the rows never moved by and the caret, the
 *  drag-selection range and the selection corners land that many pixels below
 *  their row. The row background is laid out by the grid, which is why only the
 *  overlays drift.
 *
 *  Zeroing the padding costs nothing on split, where it was already inert, and
 *  deliberately tightens unified by the 8px it really did apply there — unified
 *  keeps a real box, so its rows move up with the caret and stay aligned. Rows
 *  and caret, measured on the editable diff surface as `row.top / caret.top`:
 *  split 194/202 -> 194/194, unified 194/194 -> 186/186
 *  (`scripts/capture-pierre-caret-align.mjs` prints both). */
export const PIERRE_EDIT_CARET_ALIGN_CSS = `
[data-code]{padding-top:0}
`

/** Highlighting worker pool size. Each worker is spawned eagerly at pool
 *  init and loads its own copy of the highlighter bundle plus the WASM regex
 *  engine, so this is a startup cost paid whether or not a diff is on screen.
 *  A file is one task on one worker and is never split, so this only governs
 *  how many files tokenize concurrently — four covers a chat message or PR
 *  with several diffs open at once without spawning the library's default 8. */
export const PIERRE_WORKER_POOL_SIZE = 4
/** Row windowing for whole-file surfaces.
 *
 *  Pierre only windows rows when a `<Virtualizer>` is an ancestor; without one
 *  it renders a DOM row per source line, so a 3k-line file lands ~12k elements
 *  and a 12k-line file ~48k, and every scroll pays hit-testing and paint across
 *  the whole tree.
 *
 *  These are the library's own defaults, and both halves matter. The rendered
 *  window is `viewportHeight + overscrollSize * 2`, so 1000 keeps roughly two
 *  screens of rows built above and below the visible ones — enough that a fast
 *  flick lands on painted rows rather than on the spacer. The observer margin is
 *  the lead time for computing the NEXT window: at 0 the IntersectionObserver
 *  only fires once a row reaches the viewport edge, which is too late and shows
 *  as blank rows while scrolling.
 *
 *  Pierre's CodeView runs tighter (200 / 0) for its own compact surface; a
 *  full-height file panel needs the wider window. */
export const PIERRE_VIRTUALIZER_CONFIG = { overscrollSize: 1000, intersectionObserverMargin: 4000 }
/** Extensions whose default grammar is coarser than the one Shiki ships.
 *
 *  Pierre's own extension table sends `.tex` to the `tex` grammar, which scopes
 *  control sequences and math but treats everything inside braces as body text.
 *  The `latex` grammar additionally scopes SECTION TITLES and, more usefully,
 *  cross-reference and citation KEYS — so a mistyped `\\cite{...}` key stands out
 *  instead of reading as prose, which matters because a bad key compiles to a
 *  bold `[?]` that is easy to miss in the PDF.
 *
 *  `.sty` and `.cls` are LaTeX package and class sources, so they take the same
 *  grammar. `.bib` is deliberately absent: it already resolves to `bibtex`. */
export const PIERRE_EXTENSION_OVERRIDES: Record<string, string> = {
  tex: 'latex',
  ltx: 'latex',
  sty: 'latex',
  cls: 'latex',
}

/** Shared defaults for single-file (code view) surfaces. */
export const PIERRE_CODE_DEFAULTS: BaseCodeOptions = {
  theme: PIERRE_THEMES,
  // Surfaces provide their own chrome (block headers, panel toolbars), so the
  // built-in file header is opt-in per call site rather than default-on.
  disableFileHeader: true,
  overflow: 'scroll',
}

/** Shared defaults for diff surfaces (patch blocks, file-pair diff panels). */
export const PIERRE_DIFF_DEFAULTS: PierreDiffOptions = {
  ...PIERRE_CODE_DEFAULTS,
  diffStyle: 'unified',
  diffIndicators: 'bars',
  hunkSeparators: 'line-info',
  lineDiffType: 'word',
  expandUnchanged: false,
}

/** Merge per-surface overrides over the shared code-view defaults. */
export function pierreFileOptions(overrides?: BaseCodeOptions): BaseCodeOptions {
  return { ...PIERRE_CODE_DEFAULTS, ...overrides }
}

/** Merge per-surface overrides over the shared diff defaults. */
export function pierreDiffOptions(overrides?: PierreDiffOptions): PierreDiffOptions {
  return { ...PIERRE_DIFF_DEFAULTS, ...overrides }
}

/** Options for the multi-file CodeView surface.
 *
 *  Pierre's own React type, minus nothing: `CodeViewReactOptions` already drops
 *  the three members the React binding owns (`controlledSelection`,
 *  `createEditor`, `onSelectedLinesChange`), and it already narrows
 *  `hunkSeparators` away from the deprecated `custom` renderer — so unlike
 *  `PierreDiffOptions` there is nothing left for us to exclude. */
export type PierreCodeViewOptions = CodeViewReactOptions

/** A hairline above every file header, so the end of one file reads as separate
 *  from the start of the next.
 *
 *  Drawn as an INSET BOX-SHADOW rather than a `border-top`, and that choice is
 *  load-bearing. In CodeView the header band is not just CSS: `diffHeaderHeight`
 *  (44px) feeds `computeVirtualFileMetrics`, which is how the virtualizer
 *  estimates the height of items it has not rendered yet. A border adds a real
 *  pixel to the measured box, so every un-rendered item would be estimated 1px
 *  short and the scroll offsets would drift further the more files there are —
 *  the exact failure `__devOnlyValidateItemHeights` exists to catch. An inset
 *  shadow paints inside the existing box and changes no geometry.
 *
 *  Applied through `unsafeCSS`, which lands in the shadow root under
 *  `@layer unsafe` and so outranks the library's own `@layer base`.
 *
 *  The second rule hides the header's own +/- counts. They are arithmetic over
 *  the parsed hunks, so a file whose patch the provider withheld can only ever
 *  read `-0 +0` — flatly contradicting the provider-reported counts the caller
 *  puts in the metadata slot right next to them. There is no per-item hook to
 *  hide them for just those rows, so the slot carries the provider's counts for
 *  EVERY file and the stock pair stays off: one count source, no contradiction. */
export const PIERRE_CODE_VIEW_DIVIDER_CSS = `
[data-diffs-header]{box-shadow:inset 0 1px 0 var(--border)}
[data-diffs-header] [data-additions-count],[data-diffs-header] [data-deletions-count]{display:none}
`

/** Defaults for CodeView: the same diff look as every other surface, with the
 *  two things only CodeView can do turned on.
 *
 *  `layout` and `itemMetrics` are what make the change set read as ONE continuous
 *  list rather than a stack of cards. Pierre's own defaults reserve 8px between
 *  items (`layout.gap`), 8px above the first and below the last
 *  (`layout.paddingTop`/`paddingBottom`, applied as margins on its item
 *  container), and a further 8px inside each item below its last code row
 *  (`itemMetrics.paddingBottom`, which defaults to `spacing`). All four are
 *  zeroed so consecutive files sit flush and the hairline below is the only
 *  separator. Note `layout` is read as `options.layout ?? DEFAULT_CODE_VIEW_LAYOUT`
 *  with NO per-key merge, so all three of its keys have to be given; `itemMetrics`
 *  does merge per key, so it names only what it changes.
 *
 *  Both are the library's declared options rather than CSS overrides, which
 *  matters: the same numbers feed the virtualizer's height estimates for items it
 *  has not rendered yet, so styling the spacing away instead would desync the
 *  scroll offsets.
 *
 *  The file header keeps Pierre's stock tone — the same canvas as the code it
 *  labels. A lighter band was tried to make each file read as a row and reverted:
 *  the hairline above each header separates them without a second competing tint.
 *
 *  `disableFileHeader` flips to false here — and that is not cosmetic. CodeView
 *  measures the header band into its own layout and pins it while the file
 *  scrolls; `getStickyHeaderOffset` returns 0 whenever the header is disabled,
 *  so `stickyHeaders` without it is a silent no-op. The single-file surfaces
 *  keep the header off because their call sites draw their own chrome, but here
 *  the header IS the row: it carries the change icon, the path, the +/- counts,
 *  and the prefix/metadata slots a caller hangs its own affordances on. */
export const PIERRE_CODE_VIEW_DEFAULTS: PierreCodeViewOptions = {
  ...PIERRE_DIFF_DEFAULTS,
  disableFileHeader: false,
  stickyHeaders: true,
  layout: { gap: 0, paddingTop: 0, paddingBottom: 0 },
  itemMetrics: { paddingBottom: 0 },
  unsafeCSS: PIERRE_CODE_VIEW_DIVIDER_CSS,
}

/** Merge per-surface overrides over the shared CodeView defaults. */
export function pierreCodeViewOptions(overrides?: PierreCodeViewOptions): PierreCodeViewOptions {
  return { ...PIERRE_CODE_VIEW_DEFAULTS, ...overrides }
}

/** Sibling order for the PR change-set tree AND the diff that mirrors it:
 *  folders before files, dot-prefixed names first within each group, then
 *  case-insensitive alphabetical. This is the shape of `@pierre/trees`' own
 *  default comparator, with one deliberate difference: the collation is pinned
 *  to `'en'` instead of the host locale, so the tree (which takes this as its
 *  `sort` option) and the CodeView file order (sorted with `treeOrderCompare`
 *  below) are decided by the SAME rule on every machine and can never drift
 *  from each other — a host-locale sort could put the two surfaces in
 *  different orders on different machines. Repo paths are overwhelmingly
 *  ASCII, where `'en'` and the host default agree. */
export function treeSiblingCompare(aName: string, aIsDir: boolean, bName: string, bIsDir: boolean): number {
  if (aIsDir !== bIsDir) return aIsDir ? -1 : 1
  const aDot = aName.charCodeAt(0) === 46
  const bDot = bName.charCodeAt(0) === 46
  if (aDot !== bDot) return aDot ? -1 : 1
  return aName.toLowerCase().localeCompare(bName.toLowerCase(), 'en')
    || aName.localeCompare(bName, 'en')
}

/** Orders full paths the way the tree displays them, applying
 *  `treeSiblingCompare` at the first differing segment; an ancestor sorts
 *  above its own descendants. */
export function treeOrderCompare(a: string, b: string): number {
  const as = a.split('/')
  const bs = b.split('/')
  const n = Math.min(as.length, bs.length)
  for (let i = 0; i < n; i++) {
    if (as[i] === bs[i]) continue
    return treeSiblingCompare(as[i], i < as.length - 1, bs[i], i < bs.length - 1)
  }
  return as.length - bs.length
}

/** Lucide's `check` glyph as a sprite symbol for the PR change-set tree's
 *  viewed decoration. Decorations name a symbol rather than taking JSX -- the
 *  tree renders inside a shadow root where a React node from the host tree
 *  cannot land -- so the icon ships as sprite markup instead of a
 *  `lucide-react` component. The path data IS lucide's, so the mark still
 *  matches every other check in the app.
 *
 *  The `<svg>` wrapper is load-bearing: the tree's sprite parser accepts only
 *  an `<svg>` ROOT (`parseSpriteSheet` does `wrapper.querySelector('svg')`),
 *  and a bare `<symbol>` string is silently dropped -- no symbol, no visible
 *  decoration, no error anywhere. */
export const PR_TREE_VIEWED_ICON = 'pr-viewed-check'
export const PR_TREE_VIEWED_SPRITE = `<svg xmlns="http://www.w3.org/2000/svg" style="display:none"><symbol id="${PR_TREE_VIEWED_ICON}" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></symbol></svg>`

/** Injected stylesheet for the viewed decoration. The decoration section is
 *  the row's flexible FILLER in the tree's own CSS (`flex: 1 1 0;
 *  min-width: 0; overflow: hidden`), so a long filename crushes it to zero
 *  width at narrow rail widths -- while the git lane survives any width
 *  because it is `flex: none` with a fixed lane width. A viewed mark is
 *  review STATE, not a hint, so a row that holds one gets the git lane's
 *  never-shrink treatment. Scoped with `:has` to the check icon, so rows
 *  without a mark keep the stock crushable filler; the NAME column is the one
 *  the tree marks shrinkable, so it still ellipsizes first. The color comes
 *  from the tree's own palette so every theme that retints the git lanes
 *  retints the check with them. */
export const PR_TREE_VIEWED_CSS = `
[data-item-section="decoration"]:has([data-icon-name="${PR_TREE_VIEWED_ICON}"]),
[data-item-section="decoration"] > span:has([data-icon-name="${PR_TREE_VIEWED_ICON}"]) {
  min-width: fit-content;
}
[data-item-section="decoration"] [data-icon-name="${PR_TREE_VIEWED_ICON}"] {
  color: var(--trees-git-added-color);
}
`
