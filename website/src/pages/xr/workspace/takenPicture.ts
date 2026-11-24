/**
 * The picture of a dashboard he has taken, as the rest of the room reads it.
 *
 * While a form is taken, a press inside its drawn outline goes to the form and to nothing behind it (D114 2, D117 2).
 * A desk line in front of the other Bots (`Board.tsx`, `front`) is drawn with no depth test and pressed first, so it
 * gives that up while the taken form lies over it in the picture and stands nearer: then it is drawn and pressed by
 * depth, like any board. `Dash3d.tsx` writes the form's picture each frame from the take (M95); `Board.tsx` reads it.
 * The value lives here, in a file both read, so neither imports the other (M94).
 */
import { Box3, Vector3, type Camera, type Mesh, type Object3D } from 'three'
import { picturesOverlap, type PictureBox } from './dash3d'

/** A taken form's drawn box in the picture, and how far its middle is from the eye (m). */
export interface TakenPicture { slug: string; ndc: PictureBox; distM: number }

/** The taken form's picture now, or null; `seq` counts each take and each hand back, so a reader can look again at once. */
export const takenPicture: { at: TakenPicture | null; seq: number } = { at: null, seq: 0 }

/** A form's picture this frame (`null`: it is not taken, or no longer). Only the form that wrote it clears it. */
export function setTakenPicture(slug: string, at: TakenPicture | null): void {
  const was = takenPicture.at
  if (!at && was?.slug !== slug) return
  if (!at !== !was || (at && was && at.slug !== was.slug)) takenPicture.seq += 1
  takenPicture.at = at
}

/** The box of an object in the picture: least and most x and y, as the camera draws its corners. */
export function ndcOf(b: Box3, camera: Camera): [number, number, number, number] {
  const v = new Vector3()
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const x of [b.min.x, b.max.x]) for (const y of [b.min.y, b.max.y]) for (const z of [b.min.z, b.max.z]) {
    v.set(x, y, z).project(camera)
    x0 = Math.min(x0, v.x); x1 = Math.max(x1, v.x); y0 = Math.min(y0, v.y); y1 = Math.max(y1, v.y)
  }
  return [x0, y0, x1, y1]
}

/** A box's eight corners, in the room. */
export function boxCorners(b: Box3): Vector3[] {
  const out: Vector3[] = []
  for (const x of [b.min.x, b.max.x]) for (const y of [b.min.y, b.max.y]) for (const z of [b.min.z, b.max.z]) out.push(new Vector3(x, y, z))
  return out
}

/**
 * What the lens shows of a solid with these corners (room points), as a picture box (D137). A corner behind the
 * eye's near plane is not projected: in its place stand the points where the lines from it to the corners in front
 * cross that plane, which are on the solid. Then the box is cut to the lens. `null`: nothing of it is in the lens.
 * (`ndcOf` projects a corner behind the eye through the eye: such a box ran to -39.8 and 88.2, w1192.)
 */
export function lensNdc(corners: readonly Vector3[], camera: Camera): PictureBox | null {
  const near = (camera as { near?: number }).near ?? 0.01
  const view = corners.map(c => c.clone().applyMatrix4(camera.matrixWorldInverse))
  const seen: Vector3[] = view.filter(v => v.z < -near)
  for (let i = 0; i < view.length; i += 1) for (let j = i + 1; j < view.length; j += 1) {
    const a = view[i], b = view[j]
    if ((a.z < -near) === (b.z < -near)) continue
    seen.push(a.clone().lerp(b, (-near - a.z) / (b.z - a.z)))
  }
  if (!seen.length) return null
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const v of seen) {
    v.applyMatrix4(camera.projectionMatrix)
    x0 = Math.min(x0, v.x); x1 = Math.max(x1, v.x); y0 = Math.min(y0, v.y); y1 = Math.max(y1, v.y)
  }
  const box: PictureBox = [Math.max(x0, -1), Math.max(y0, -1), Math.min(x1, 1), Math.min(y1, 1)]
  return box[0] < box[2] && box[1] < box[3] ? box : null
}

/** As `lensNdc`, for each visible mesh of `o` by its own geometry's bounds (`Dash3d.tsx` `drawnNdc`), joined. */
export function drawnLensNdc(o: Object3D, camera: Camera): PictureBox | null {
  let out: PictureBox | null = null
  o.traverseVisible(x => {
    const m = x as Mesh
    if (!m.isMesh || !m.geometry) return
    if (!m.geometry.boundingBox) m.geometry.computeBoundingBox()
    const b = m.geometry.boundingBox
    if (!b) return
    const n = lensNdc(boxCorners(b).map(c => c.applyMatrix4(m.matrixWorld)), camera)
    if (n) out = out ? [Math.min(out[0], n[0]), Math.min(out[1], n[1]), Math.max(out[2], n[2]), Math.max(out[3], n[3])] : n
  })
  return out
}

/** The box of what of a thing is drawn: its visible meshes, not the hidden pieces that wait in it. */
export function drawnBox(o: Object3D): Box3 {
  const box = new Box3()
  const walk = (x: Object3D) => {
    if (!x.visible) return
    if ((x as { isMesh?: boolean }).isMesh) box.union(new Box3().setFromObject(x, true))
    else for (const c of x.children) walk(c)
  }
  walk(o)
  return box
}

/** Whether a box stands in front of the eye: only then is it on the picture as the eye sees it. */
export function inFront(b: Box3, camera: Camera): boolean {
  return !b.isEmpty() && b.getCenter(new Vector3()).applyMatrix4(camera.matrixWorldInverse).z < 0
}

/**
 * Whether the taken form lies over `board` in the picture and stands nearer the eye than the board's middle. A form at
 * rest in a Bot's hands is not taken, so it covers nothing; a board outside the form's picture is not covered.
 */
export function takenCovers(board: Object3D, camera: Camera, at: TakenPicture | null = takenPicture.at): boolean {
  if (!at) return false
  const box = drawnBox(board)
  if (!inFront(box, camera)) return false
  const seen = lensNdc(boxCorners(box), camera)
  const eye = camera.getWorldPosition(new Vector3())
  return !!seen && picturesOverlap(seen, at.ndc) && at.distM < eye.distanceTo(box.getCenter(new Vector3()))
}
