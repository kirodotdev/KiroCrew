/**
 * Which pages get a composition layer of their own, and in which order the layers are handed to the session.
 *
 * A session that grants `layers` can show a page on a quad layer: the headset's own compositor then lays the page's
 * pixels onto the display once, where a textured mesh is filtered twice. A session has few layers, so only the pages
 * nearest to where the person looks get one; every other page stays on its mesh. Nothing here draws or touches a
 * session: it is the plain choice, kept apart so that it can be tested. A session with no layers has a budget of 0,
 * and then every page is on its mesh.
 */

/** No more page layers than this, until a device says that it holds more. OpenXR promises 16 composition layers at the least, which a browser halves for two eyes and shares with the scene's own: six pages. */
export const MOST_PAGE_LAYERS = 6

/**
 * How many pages may have a layer in this session: none unless the session granted `layers`; else the session's own
 * limit less one for the scene, and never more than `MOST_PAGE_LAYERS`. A session that grants layers and names no
 * limit is taken at the least a runtime must offer.
 */
export function layerBudget(enabledFeatures: readonly string[] | null | undefined, maxRenderLayers: number | null | undefined): number {
  if (!enabledFeatures || !enabledFeatures.includes('layers')) return 0
  if (typeof maxRenderLayers !== 'number' || !Number.isFinite(maxRenderLayers)) return MOST_PAGE_LAYERS
  return Math.max(0, Math.min(MOST_PAGE_LAYERS, Math.floor(maxRenderLayers) - 1))
}

/** A page, as the choice sees it. */
export interface LayerPage {
  id: string
  /** How far the page's middle is from where the person looks, in degrees. */
  offGazeDeg: number
  /** How far the page's middle is from the head, in metres. */
  distanceM: number
  /** Some of the page is in the person's view. */
  inView: boolean
  /** The page has a picture that a layer can show (a live page with a picture; not a still, not a sheet). */
  ready: boolean
}

/** A page that has a layer keeps it until another page is nearer to the gaze by more than this: a head that rests between two pages does not swap their layers on every frame. */
export const KEEP_LAYER_DEG = 5

/**
 * The pages that get a layer: those in view with a picture, nearest to where the person looks first, up to the
 * budget. `held` names the pages that have a layer now. A page out of view never gets one, so one that leaves the
 * view gives its layer back.
 */
export function pickLayers(pages: readonly LayerPage[], budget: number, held: ReadonlySet<string> = new Set()): string[] {
  if (!(budget > 0)) return []
  const rank = (p: LayerPage) => p.offGazeDeg - (held.has(p.id) ? KEEP_LAYER_DEG : 0)
  return pages.filter(p => p.inView && p.ready)
    .sort((a, b) => rank(a) - rank(b) || a.offGazeDeg - b.offGazeDeg || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .slice(0, Math.floor(budget))
    .map(p => p.id)
}

/**
 * The order of the layers in the session's list: the farthest page first, so that a nearer page lies over a farther
 * one where they cross (layers are laid one over the other in the order of the list, with no depth test among
 * them). The scene's own layer comes after all of them, which the framework sees to.
 */
export function orderLayers(pages: readonly Pick<LayerPage, 'id' | 'distanceM'>[]): string[] {
  return [...pages].sort((a, b) => b.distanceM - a.distanceM || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)).map(p => p.id)
}

/** How far a point is from where a head looks, in degrees. `forward` need not be of length 1. */
export function offGaze(head: readonly [number, number, number], forward: readonly [number, number, number], at: readonly [number, number, number]): number {
  const d = [at[0] - head[0], at[1] - head[1], at[2] - head[2]]
  const dl = Math.hypot(d[0], d[1], d[2]), fl = Math.hypot(forward[0], forward[1], forward[2])
  if (dl < 1e-9 || fl < 1e-9) return 0
  const cos = (d[0] * forward[0] + d[1] * forward[1] + d[2] * forward[2]) / (dl * fl)
  return (Math.acos(Math.min(1, Math.max(-1, cos))) * 180) / Math.PI
}
