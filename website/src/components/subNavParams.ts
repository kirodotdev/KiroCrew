/** Shared vocabulary of the Settings second-level navigation, in its own tiny
 *  module so the generic SidePanelLayout never imports from the Settings
 *  SubNav component (which would drag the SubNav module graph into every
 *  SidePanelLayout consumer's bundle — Developer, Agent Capabilities). */

/** Canonical URL param for a second-level selection inside a Settings tab. */
export const SUBNAV_PARAM = 'sub'

/** Historical per-host params still honoured on READ so old bookmarks keep
 *  landing. Write paths (settingsRoute, the legacy ?tab=slack remap, SubNav
 *  selection) all emit the canonical `sub` — these exist only for URLs minted
 *  before the unification. */
export const SUBNAV_LEGACY_PARAMS = ['channel', 'section'] as const

/** Whether ANY second-level selection is present — canonical or legacy alias.
 *  This is the level test SidePanelLayout uses to yield its chrome to the
 *  SubNav's back bar: reading only the canonical name while old links still
 *  carry aliases is how two stacked back bars come back. */
export function hasSubSelection(params: URLSearchParams): boolean {
  if (params.get(SUBNAV_PARAM) != null) return true
  return SUBNAV_LEGACY_PARAMS.some(p => params.get(p) != null)
}

/** Remove every second-level selection param — used when the FIRST level
 *  changes (tab switch, back to root): a `sub` is scoped to the tab that
 *  hosts it, and one that rides across a tab change strands a phone view
 *  whose new tab hosts no SubNav (chrome yielded, nothing replacing it). */
export function deleteSubSelection(params: URLSearchParams): void {
  params.delete(SUBNAV_PARAM)
  for (const p of SUBNAV_LEGACY_PARAMS) params.delete(p)
}

/** Apple-HIG 44pt touch target, gated to coarse pointers so desktop density
 *  is unchanged. One definition — the same rule expressed twice had already
 *  drifted (one call site unconditional, one gated). */
export const COARSE_TOUCH_TARGET = '[@media(pointer:coarse)]:min-h-11'

/** Encode one navigation key as exactly one URL path segment. Percent-encoding
 *  keeps a key containing `/`, `?`, `#` or `%` from minting extra depth or a
 *  query/hash boundary. Dot-only keys are the one thing encoding cannot make
 *  safe — the WHATWG URL parser treats `.` / `..` AND their percent-forms
 *  (`%2E`, `%2E%2E`) as dot-segments and resolves them against the tree, so a
 *  crafted `?tab=..` would escape /settings entirely. Those return null and
 *  the caller drops that level (the same fallback an unknown key gets). */
export function toPathSegment(key: string): string | null {
  if (key === '.' || key === '..') return null
  return encodeURIComponent(key)
}

/** Parse the pathname under `basePath` into POSITIONAL segments:
 *  segment[0] = tab, segment[1] = a SubNav's second-level selection, deeper
 *  reserved. Positional means an empty segment (double slash, trailing slash)
 *  stays in place as `''` — never matching a key — instead of letting deeper
 *  segments shift forward (`/settings/channels//slack` must NOT open the
 *  slack pane). Segments are percent-DECODED so keys round-trip through
 *  toPathSegment; a malformed escape decodes to itself (still matches no key,
 *  so it falls back exactly like any unknown value). Empty when the location
 *  is outside the base — e.g. for one render during a cross-page navigate
 *  before the consumer unmounts. */
export function parsePathSegments(basePath: string, pathname: string): string[] {
  if (pathname === basePath) return []
  if (!pathname.startsWith(basePath + '/')) return []
  return pathname
    .slice(basePath.length + 1)
    .split('/')
    .map(seg => {
      try {
        return decodeURIComponent(seg)
      } catch {
        return seg
      }
    })
}

/** history.state marker set on entries MINTED BY A DRILL-IN PUSH (root -> tab,
 *  list -> pane). The matching back control checks it to decide between a real
 *  `history.back()` (entry is ours: popping keeps push/pop symmetric, so the
 *  platform back gesture never lands on a dead duplicate entry) and a
 *  replace-write (cold deep link: `back()` would exit the app entirely). */
export const SUBNAV_PUSH_STATE = 'subnavPush'
