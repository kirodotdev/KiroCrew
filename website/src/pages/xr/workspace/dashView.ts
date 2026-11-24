/**
 * `/xr?view=dash&slug=<slug>`: one Bot's 3D dashboard alone in an empty room, seen from a fixed list of cameras
 * (`DashView.tsx`). For a still of it (D99 5, D100 6): a row shoots each camera; a gateway's offline run may one
 * day do the same. Plain values and pure functions.
 *
 * Words of the address: `slug` (a Bot's slug, refused unless it is one), `camera` (a name of the list, else the
 * first), `fixture` and `probe` as everywhere in `/xr`. Nothing else is read. A slug names a Bot of the owner's own
 * crew: the page asks the gateway only reads the room of pages makes, the roster (`membersRosterQuery`) and then,
 * for a Bot the roster holds, the two reads of its dashboard by its roster name (`useDash3d.ts`). A made-up slug
 * finds no row, reads nothing more and shows an empty room. With `fixture=1` the picture says "Sample data".
 */
import { TAKEN_DISTANCE_M } from './dash3d'

type V3 = readonly [number, number, number]

/** His eye, at the room's middle. */
export const DASH_EYE: V3 = [0, 1.6, 0]
/** Where the Bot's feet stand in the empty room: ahead, a little low in the view, as a Bot stands in the row. */
export const DASH_FEET: V3 = [0, 0.45, -1.1]

export interface DashCamera {
  name: 'rest' | 'taken' | 'taken-aside'
  /** Whether the dashboard is taken for it. */
  taken: boolean
  position: V3
  /** The point it looks at. */
  target: V3
}

/** Where the taken form stands for a level head (`takenTrayTopY`): its middle about at the eye line, ahead at its distance. */
export const DASH_TAKEN_AT: V3 = [0, DASH_EYE[1], -TAKEN_DISTANCE_M]
const TAKEN_AT = DASH_TAKEN_AT
const ASIDE_DEG = 30
const RAD = Math.PI / 180

/** The cameras, the first the default: at rest by its Bot from his eye; taken, straight; taken, 30 degrees aside. */
export const DASH_CAMERAS: readonly DashCamera[] = [
  { name: 'rest', taken: false, position: DASH_EYE, target: [DASH_FEET[0], DASH_FEET[1] + 0.06, DASH_FEET[2] + 0.1] },
  { name: 'taken', taken: true, position: DASH_EYE, target: TAKEN_AT },
  {
    name: 'taken-aside',
    taken: true,
    // The same distance from the form's middle as the eye, turned 30 degrees about it to his right.
    position: [TAKEN_AT[0] + Math.sin(ASIDE_DEG * RAD) * TAKEN_DISTANCE_M, DASH_EYE[1], TAKEN_AT[2] + Math.cos(ASIDE_DEG * RAD) * TAKEN_DISTANCE_M],
    target: TAKEN_AT,
  },
]

/** The camera an address names: one of the list, else the first. */
export const dashCamera = (name: string | null | undefined): DashCamera => DASH_CAMERAS.find(c => c.name === name) ?? DASH_CAMERAS[0]

/** A Bot's slug as the crew spells one; anything else is no slug. */
const SLUG = /^[a-z0-9][a-z0-9-]{0,63}$/
export const dashSlug = (raw: string | null | undefined): string | null => (raw && SLUG.test(raw) ? raw : null)
