/**
 * Which view of `/xr` an address opens: the workspace of pages by default, plain `/xr` and `?view=pages` too; the land
 * with the Bots only with `?view=land`, or `?view=aura`, the land framed as the glasses show it. The other words of the
 * address (`desk=1`, `source=dom`, `fps=1`) need no `view`.
 */
export type XrView = 'pages' | 'land' | 'dash'

const LAND_VIEWS = new Set(['land', 'aura'])

/** `?view=dash&slug=<slug>`: one Bot's 3D dashboard alone, from fixed cameras (`workspace/dashView.ts`). */
export const xrView = (search: string | URLSearchParams): XrView => {
  const view = (typeof search === 'string' ? new URLSearchParams(search) : search).get('view') ?? ''
  return LAND_VIEWS.has(view) ? 'land' : view === 'dash' ? 'dash' : 'pages'
}
