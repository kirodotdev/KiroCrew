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
 *  engine, so the whole pool is one up-front cost — which is why the pool is
 *  built on demand by the first surface that intends to highlight rather than at
 *  module scope, and why a surface that wants no colour (plain-diff mode) opts
 *  out of it instead of merely bypassing it.
 *  A file is one task on one worker and is never split, so this only governs
 *  how many files tokenize concurrently — four covers a chat message or PR
 *  with several diffs open at once without spawning the library's default 8. */
export const PIERRE_WORKER_POOL_SIZE = 4
/** Which regex engine the highlight workers tokenize with.
 *
 *  Pierre defaults to `shiki-js`, which runs TextMate grammar patterns through
 *  `oniguruma-to-es` on V8's own RegExp. That engine has no ceiling on a single
 *  match: a grammar pattern that backtracks catastrophically grows V8's
 *  backtracking stack until the renderer is killed, and because that stack is
 *  an external allocation inside the sandbox reservation the fatal reads as a
 *  CAGE OOM with an almost empty JS heap — which is why it never looked like a
 *  highlighter problem:
 *
 *      <--- Near heap limit --->
 *      Heap: used=10.1MB limit=4192.0MB
 *      Near V8 cage limit; stack trace capture may not succeed
 *      V8 javascript OOM (CALL_AND_RETRY_LAST).
 *
 *      #1 exec              (worker-portable-*.js)   <- EmulatedRegExp.exec
 *      #2 findNextMatchSync
 *      #8 _tokenize
 *      #9 tokenizeLine2
 *
 *  `shiki-wasm` is the reference oniguruma build, and it is compiled with
 *  `DEFAULT_RETRY_LIMIT_IN_MATCH 10000000`, so a pathological match aborts
 *  instead of running unbounded. The two guards that would otherwise cap this
 *  are both unavailable to us: the 1000-char `tokenizeMaxLineLength` does not
 *  apply (exponential backtracking needs only tens of characters), and
 *  `tokenizeTimeLimit` is hardcoded to `0` — no limit — inside `@pierre/diffs`,
 *  reachable through no option this module can pass.
 *
 *  Cost is a one-time WASM instantiation per worker; both engines are already
 *  in the worker bundle, so nothing new is fetched. */
export const PIERRE_REGEX_ENGINE = 'shiki-wasm'
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
