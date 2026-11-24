/**
 * M37 (D83 2): ONE ORDER, drawn and pressed the same, nearest first: the dot; a sheet he speaks on; the palette while
 * his palm shows it; the readout plate; a held board and its stamp, and the new chat card while he holds it
 * (`chatCard.ts` `cardLayer`, D105 3); Home's strip and names; a Bot's plate line (M51,
 * `BotNote.tsx`, drawn over the pages and under Home, pressed never); pages; Bots. Whatever is
 * drawn over another takes the press over it. Every layer reads its draw band and its press order here: the meshes'
 * `renderOrder` (`layIn`) and the roots' `pointerEventsOrder`, which the head's dot sorts by (`frameFirst.ts`) and
 * every other pointer by the library's own rule (the higher order first).
 *
 * Pages and Bots share the room's band: they are drawn by depth and pressed by distance, so a Bot in front of a page's
 * content is drawn over it and keeps its pinch (D77), and the one exception, a page's frame before a Bot or a desk
 * board that stands nearer, fades that Bot so the picture still says which takes it (D78 1, `fadedBots`).
 */
import type { Material, Mesh, Object3D } from 'three'

/** Nearest first. */
export const LAYERS = ['dot', 'speak', 'palette', 'readout', 'held', 'keys', 'roomname', 'home', 'botnote', 'page', 'bot'] as const
export type Layer = (typeof LAYERS)[number]

/** Room for a layer's own parts inside its band (a board's paper over its backing, Home's names over its plate). */
export const BAND = 10

/**
 * The `renderOrder` each layer's meshes start at. Every band over the room's is drawn in three's transparent pass
 * (`layIn`), since an opaque mesh is drawn before every transparent one whatever its order: an opaque dot or held
 * board would lie under Home's strip, which is transparent and has no depth test.
 */
export const DRAW: Readonly<Record<Layer, number>> = { dot: 100, speak: 80, palette: 70, readout: 60, held: 50, keys: 45, roomname: 40, home: 30, botnote: 20, page: 0, bot: 0 }

/**
 * Whether a layer's meshes test depth: the held board does, so the paper and its stamp cover each other as they stand,
 * and pages and Bots do, as the room's things. Every other band is drawn over what it lies on, with no depth test.
 */
export const DEPTH: Readonly<Record<Layer, boolean>> = { dot: false, speak: false, palette: false, readout: false, held: true, keys: false, roomname: false, home: false, botnote: false, page: true, bot: true }

/**
 * The `pointerEventsOrder` of each layer's root, the higher pressed first. The dot is the pointer and takes none. A Bot's
 * plate line takes no press either (`pointerEvents="none"`, `BotNote.tsx`): its number only keeps the table in order.
 * The room's name line takes a press on its "Copy this room" mark alone, over Home's, and none once the mark is gone
 * (`RoomNameLine.tsx`, M58).
 */
export const PRESS: Readonly<Record<Exclude<Layer, 'dot'>, number>> = { speak: 50, palette: 40, readout: 30, held: 20, keys: 18, roomname: 15, home: 10, botnote: 5, page: 0, bot: 0 }

/**
 * A board's face while it stands at its Bot's desk and nothing stands nearer on his eye's line to it (D39, ruling 4):
 * drawn in front of the other Bots and pressed before them, still under Home and every page that stands nearer.
 */
export const DESK_FRONT = 1

/**
 * Each mesh's own order inside its band, and each material's own pass, kept beside the objects (not in their
 * `userData`, which the probe and the rows read as the thing's marks).
 */
const OWN_ORDER = new WeakMap<Object3D, number>()
const OWN_TRANSPARENT = new WeakMap<Material, boolean>()

/** A caller that orders its parts itself (a board's backing, paper and clip) says each part's own order here. */
export function ownOrder(mesh: Object3D, order: number): void {
  OWN_ORDER.set(mesh, order)
}

/**
 * Puts every mesh under `root` in `layer`'s band: its own order (`ownOrder`, else the mesh's order the first time it
 * is seen) is its place inside the band. Over the room's band a mesh goes to the transparent pass; back in it, each
 * material has its own pass again. Cheap enough for every frame.
 */
/** How much room a band that starts at `band` has for its parts' own orders: up to the next nearer band, at most `BAND`. */
export function bandRoom(band: number): number {
  const nearer = Math.min(...Object.values(DRAW).filter(v => v > band))
  return Math.min(BAND, nearer - band)
}

export function layIn(root: Object3D, layer: Layer): void {
  const band = DRAW[layer]
  const over = band > DRAW.page
  root.traverse(o => {
    const m = o as Mesh
    if (!m.isMesh) return
    let own = OWN_ORDER.get(m)
    if (own === undefined) { own = m.renderOrder; OWN_ORDER.set(m, own) }
    // A mesh's own order never reaches the next band (G2): past it, it would be drawn as the layer nearer. A band is
    // `BAND` wide, or narrower when the nearer band starts sooner (the keys' line, 5 under the held band).
    const inside = Math.min(Math.max(own, 0), bandRoom(band) - 0.01)
    if (m.renderOrder !== band + inside) m.renderOrder = band + inside
    for (const mat of [m.material].flat() as Material[]) {
      let mine = OWN_TRANSPARENT.get(mat)
      if (mine === undefined) { mine = mat.transparent; OWN_TRANSPARENT.set(mat, mine) }
      const want = over || mine
      if (mat.transparent !== want) { mat.transparent = want; mat.needsUpdate = true }
    }
  })
}
