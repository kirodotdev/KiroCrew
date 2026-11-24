import { describe, expect, it } from 'vitest'
import { Box3, BoxGeometry, Group, Mesh, MeshBasicMaterial, PerspectiveCamera } from 'three'
import { picturesOverlap } from './dash3d'
import { whatIs } from './pinchRoute'
import { boxCorners, drawnLensNdc, lensNdc, ndcOf, drawnBox, setTakenPicture, takenCovers, takenPicture, type TakenPicture } from './takenPicture'

/** An eye at 1.6 m looking ahead (-z), and a board 0.4 x 0.3 m standing `far` metres ahead, its middle at `y`. */
function scene(far: number, y = 1.6) {
  const camera = new PerspectiveCamera(40, 1.2, 0.05, 100)
  camera.position.set(0, 1.6, 0)
  camera.updateMatrixWorld(true)
  const board = new Group()
  board.add(new Mesh(new BoxGeometry(0.4, 0.3, 0.01), new MeshBasicMaterial()))
  board.position.set(0, y, -far)
  board.updateMatrixWorld(true)
  return { camera, board }
}

describe('a taken dashboard over a desk line (D114 2, M94)', () => {
  it('covers a board that it lies over in the picture and stands nearer than', () => {
    const { camera, board } = scene(2)
    // The form, 0.6 m ahead, over the board's lower part only.
    const form: TakenPicture = { slug: 'atlas', ndc: [-0.3, -0.5, 0.3, -0.02], distM: 0.6 }
    expect(takenCovers(board, camera, form)).toBe(true)
  })

  it('covers nothing that lies outside its picture, nothing nearer than it, and nothing when no form is taken', () => {
    const { camera, board } = scene(2)
    const seen = ndcOf(drawnBox(board), camera)
    expect(takenCovers(board, camera, { slug: 'atlas', ndc: [seen[2] + 0.05, -0.5, seen[2] + 0.4, 0.5], distM: 0.6 })).toBe(false)
    expect(takenCovers(board, camera, { slug: 'atlas', ndc: [-0.3, -0.5, 0.3, 0.5], distM: 2.5 })).toBe(false)
    expect(takenCovers(board, camera, null)).toBe(false)
    // A board behind the eye is on no picture.
    const behind = scene(-2)
    expect(takenCovers(behind.board, behind.camera, { slug: 'atlas', ndc: [-1, -1, 1, 1], distM: 0.6 })).toBe(false)
  })

  it('counts each take and each hand back, and only the form that wrote the picture clears it', () => {
    const seq = takenPicture.seq
    setTakenPicture('atlas', { slug: 'atlas', ndc: [0, 0, 0.1, 0.1], distM: 0.6 })
    expect(takenPicture.seq).toBe(seq + 1)
    // The same take, a frame later: no new count.
    setTakenPicture('atlas', { slug: 'atlas', ndc: [0, 0, 0.2, 0.1], distM: 0.6 })
    expect(takenPicture.seq).toBe(seq + 1)
    setTakenPicture('mira', null)
    expect(takenPicture.at?.slug).toBe('atlas')
    setTakenPicture('atlas', null)
    expect(takenPicture.at).toBe(null)
    expect(takenPicture.seq).toBe(seq + 2)
  })
})

describe('what the lens shows of a taken form: its press box (D137)', () => {
  /** A form 0.8 x 0.3 x 0.2 m, its middle at (x, 1.6, z), seen by the eye of `scene`. */
  const form = (x: number, z: number) => {
    const g = new Group()
    g.add(new Mesh(new BoxGeometry(0.8, 0.3, 0.2), new MeshBasicMaterial()))
    g.position.set(x, 1.6, z)
    g.updateMatrixWorld(true)
    return g
  }
  const { camera } = scene(2)

  it('has no box when it stands wholly behind the eye', () => {
    expect(drawnLensNdc(form(0, 2), camera)).toBe(null)
    expect(lensNdc(boxCorners(drawnBox(form(0, 2))), camera)).toBe(null)
  })

  it('cuts a corner behind the eye: the box stays in the lens', () => {
    // Its middle 0.05 m ahead, its back half behind the eye's near plane: `ndcOf` runs far past the lens.
    const g = form(0, -0.05)
    expect(Math.max(...ndcOf(drawnBox(g), camera).map(Math.abs))).toBeGreaterThan(1.5)
    const n = drawnLensNdc(g, camera)
    expect(n).not.toBe(null)
    expect(n?.every(v => v >= -1 && v <= 1)).toBe(true)
  })

  it('keeps the seen part of a form half out of the lens, to each side', () => {
    for (const side of [-1, 1]) {
      const g = form(side * 0.8, -1)
      const all = ndcOf(drawnBox(g), camera)
      const n = drawnLensNdc(g, camera)
      expect(n).not.toBe(null)
      if (!n) continue
      // Cut at the lens's edge on the side it leaves by, as drawn on the other.
      expect(side > 0 ? all[2] : -all[0]).toBeGreaterThan(1)
      expect(side > 0 ? n[2] : n[0]).toBe(side)
      expect(side > 0 ? n[0] : n[2]).toBeCloseTo(side > 0 ? all[0] : all[2], 6)
    }
  })

  it('has no box when it stands in front but wholly beside the lens', () => {
    expect(drawnLensNdc(form(3, -1), camera)).toBe(null)
  })

  it('is its drawn box when it stands wholly in the lens', () => {
    const g = form(0, -2)
    const all = ndcOf(drawnBox(g), camera)
    expect(all.every(v => Math.abs(v) < 1)).toBe(true)
    const n = drawnLensNdc(g, camera)
    expect(n).not.toBe(null)
    n?.forEach((v, i) => expect(v).toBeCloseTo(all[i], 6))
  })

  it('keeps the points where a line from a corner in front to one behind crosses the near plane (M126)', () => {
    // A rod from 2 m ahead to 1 m behind the eye, 0.3 m to the right: its corners in front alone stand near the middle
    // of the picture; where it crosses the near plane it stands far to the right, so its seen part runs to the edge.
    const rod = new Group()
    rod.add(new Mesh(new BoxGeometry(0.02, 0.02, 3), new MeshBasicMaterial()))
    rod.position.set(0.3, 1.6, -0.5)
    rod.updateMatrixWorld(true)
    const n = drawnLensNdc(rod, camera)
    expect(n?.[2]).toBe(1)
    const front = boxCorners(drawnBox(rod)).filter(c => c.clone().applyMatrix4(camera.matrixWorldInverse).z < -camera.near)
    expect(Math.max(...front.map(c => c.clone().project(camera).x))).toBeLessThan(0.5)
  })

  it('joins the seen parts of every mesh of a form (M126)', () => {
    const g = new Group()
    for (const x of [-0.5, 0.5]) { const m = new Mesh(new BoxGeometry(0.2, 0.2, 0.2), new MeshBasicMaterial()); m.position.set(x, 0, 0); g.add(m) }
    g.position.set(0, 1.6, -2)
    g.updateMatrixWorld(true)
    const n = drawnLensNdc(g, camera)
    const left = lensNdc(boxCorners(new Box3().setFromObject(g.children[0], true)), camera)
    const right = lensNdc(boxCorners(new Box3().setFromObject(g.children[1], true)), camera)
    expect(n?.[0]).toBeCloseTo(left?.[0] ?? NaN, 6)
    expect(n?.[2]).toBeCloseTo(right?.[2] ?? NaN, 6)
  })

  it('judges a board by its seen part when its middle stands in front but a corner is behind the eye (M126)', () => {
    const { camera: c } = scene(2)
    // A long board from 3 m ahead to 0.5 m behind the eye, at the right; its middle is 1.25 m ahead.
    const long = new Group()
    long.add(new Mesh(new BoxGeometry(0.2, 0.3, 3.5), new MeshBasicMaterial()))
    long.position.set(0.6, 1.6, -1.25)
    long.updateMatrixWorld(true)
    const form: TakenPicture = { slug: 'atlas', ndc: [-0.9, -0.3, -0.5, 0.3], distM: 0.6 }
    // Its whole box, projected through the eye, reaches the form on the left; what the lens shows of it does not.
    expect(picturesOverlap(ndcOf(drawnBox(long), c), form.ndc)).toBe(true)
    expect(takenCovers(long, c, form)).toBe(false)
  })

  it('a desk line keeps its front while no taken form is in the lens, and a board half behind the eye is judged by its seen part', () => {
    const { camera: c, board } = scene(2)
    expect(takenCovers(board, c, null)).toBe(false)
    // A long board from 1 m ahead to 2 m behind the eye, at the right: what the lens shows of it is the right edge only,
    // so a form on the left does not cover it, though `ndcOf` puts its corners behind the eye on the left side.
    const side = new Group()
    side.add(new Mesh(new BoxGeometry(0.4, 0.3, 3), new MeshBasicMaterial()))
    side.position.set(0.5, 1.6, 0.5)
    side.updateMatrixWorld(true)
    const seen = lensNdc(boxCorners(drawnBox(side)), c)
    expect(seen?.[0]).toBeGreaterThan(0.5)
    expect(ndcOf(drawnBox(side), c)[0]).toBeLessThan(0)
    expect(takenCovers(side, c, { slug: 'atlas', ndc: [-0.9, -0.3, -0.5, 0.3], distM: 0.6 })).toBe(false)
  })
})

describe('what the probe says the dot lies on (M93)', () => {
  it('says a dashboard by its mark, whatever piece of it was hit', () => {
    const form = new Group()
    form.userData = { dash3d: 'mira', xrPress: true }
    const part = new Mesh(new BoxGeometry(), new MeshBasicMaterial())
    const inner = new Group()
    inner.add(part)
    form.add(inner)
    expect(whatIs({ object: part })).toBe('dash:mira')
    expect(whatIs({ object: new Mesh() })).toBe('room')
  })
})
