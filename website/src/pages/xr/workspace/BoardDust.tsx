/**
 * A board that leaves comes apart into many small pieces in its own colours (Ray's D4a and D21): thrown after its
 * stamp, from the edge that leads the throw, the pieces keep the throw's way, slow, spread and fade in about 1.5 s;
 * a board that ends by itself (answered elsewhere, the no sent from the floor) leaves the same way, smaller and
 * quieter, drifting up. The pieces are round and soft-edged with a little light at the core, mostly fine and a few
 * larger, and each fades on its own clock, so the cloud thins out. One points draw call, moved on the GPU. With
 * reduced motion it only fades in place.
 */
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import { BufferAttribute, BufferGeometry, NormalBlending, Points, Scene, ShaderMaterial, Vector3, type Matrix4 } from 'three'
import { DUST_FRAGMENT, DUST_VERTEX } from './boardDustShader'

/** The pieces, and how a leaving goes: loud is a throw, quiet a board that ends by itself. */
const DUST = {
  loud: { pieces: 3200, lifeS: [0.9, 1.3], leadS: 0.3, speedMps: 0.5, spreadMps: 0.16 },
  quiet: { pieces: 1400, lifeS: [0.6, 1.0], leadS: 0.25, speedMps: 0, spreadMps: 0.05 },
  /** A thrown board holds together this long before its leading edge starts to drift. */
  holdS: 0.05,
  /** Each piece's size, as a share of the size that tiles the board: from fine dust to a fleck. */
  grain: [0.6, 2.4],
  /**
   * The bare paper: a colour with every channel over this. A place on it is kept at this chance, else another is
   * drawn (at most `paperTries`); a paper piece lives this share of the others' time and is this much smaller.
   */
  paperFrom: 0.85, paperKeep: 0.4, paperLife: 0.28, paperTries: 10, paperGrain: 0.7,
  /** The ink's light: the words' and the clip's pieces glow this pale blue-white instead of their near-black. */
  inkLight: [0.8, 0.86, 1.0],
  /** A still person's leaving: no move, no shrink, a fade. */
  stillS: 0.6,
  /** How much smaller a piece is at its end. */
  shrink: 0.55,
} as const

/** For the probe only (`__xrPages.dustAt`): every leaving drawn at this time of its own, in seconds; `null`, its clock. */
export const dustClock: { pinnedS: number | null } = { pinnedS: null }

/** What a leaving board was at its last frame: its face's place in the room, its size and its picture. */
export interface DustFrom { matrix: Matrix4; widthM: number; heightM: number; canvas: HTMLCanvasElement }

/** `shrink`: how much smaller a piece is at its end; none with reduced motion. */
function dustMaterial(piece: number, shrink: number = DUST.shrink): ShaderMaterial {
  return new ShaderMaterial({
    vertexShader: DUST_VERTEX, fragmentShader: DUST_FRAGMENT, transparent: true, depthWrite: false,
    blending: NormalBlending,
    uniforms: { uTime: { value: 0 }, uScale: { value: 1 }, uSize: { value: piece }, uDrag: { value: 1.6 }, uShrink: { value: shrink } },
  })
}

/**
 * Builds the pieces' shader program while the boards stand, so a leaving does not stall its first frames compiling
 * it: the renderer keeps one program for every material with the same shaders.
 */
export function useDustWarm(session: boolean) {
  const gl = useThree(s => s.gl)
  const camera = useThree(s => s.camera)
  useEffect(() => {
    const g = new BufferGeometry()
    for (const [name, n] of [['position', 3], ['vel', 3], ['tint', 3], ['timing', 2], ['grain', 1]] as const) g.setAttribute(name, new BufferAttribute(new Float32Array(n), n))
    const m = dustMaterial(0)
    const scene = new Scene().add(new Points(g, m))
    // Again when a session starts: it draws through the session's own camera.
    gl.compile(scene, session && gl.xr.isPresenting ? gl.xr.getCamera() : camera)
    return () => { g.dispose(); m.dispose() }
  }, [gl, camera, session])
}

/** The pieces of one leaving board. `dir`: the throw's way in the room (null: it ends by itself). */
export function BoardDust({ from, dir, still, onDone }: { from: DustFrom; dir: Vector3 | null; still: boolean; onDone: () => void }) {
  const loud = dir !== null
  const kind = loud ? DUST.loud : DUST.quiet
  /** The first frame drawn: its clock starts there, so a slow mount does not eat the start of the leaving. */
  const born = useRef<number | null>(null)
  const size = useThree(s => s.size)
  const camera = useThree(s => s.camera)
  const { geometry, material, lastS } = useMemo(() => {
    const n = kind.pieces
    const pos = new Float32Array(n * 3)
    const vel = new Float32Array(n * 3)
    const tint = new Float32Array(n * 3)
    const timing = new Float32Array(n * 2)
    const grain = new Float32Array(n)
    const c = from.canvas.getContext('2d')
    const img = c ? c.getImageData(0, 0, from.canvas.width, from.canvas.height) : null
    // The throw's way in the board's own plane: the edge that leads it leaves first.
    const inv = from.matrix.clone().invert()
    const local = dir ? dir.clone().transformDirection(inv) : new Vector3(0, 1, 0)
    const lead = new Vector3(local.x, local.y, 0)
    if (lead.lengthSq() < 1e-6) lead.set(0, 1, 0)
    lead.normalize()
    const reach = Math.abs(lead.x) * from.widthM / 2 + Math.abs(lead.y) * from.heightM / 2
    const p = new Vector3()
    const go = new Vector3()
    let last = 0
    /** The picture's colour at (u, v), 0 to 1; a place of the dark room past a rounded corner takes the board's own. */
    const rgb = [0, 0, 0]
    const colourAt = (u: number, v: number) => {
      if (!img) { rgb[0] = 0.2; rgb[1] = 0.31; rgb[2] = 0.49; return }
      const at = (Math.min(img.height - 1, Math.floor(v * img.height)) * img.width + Math.min(img.width - 1, Math.floor(u * img.width))) * 4
      if (img.data[at + 3] > 40) { rgb[0] = img.data[at] / 255; rgb[1] = img.data[at + 1] / 255; rgb[2] = img.data[at + 2] / 255 } else { rgb[0] = 0.2; rgb[1] = 0.31; rgb[2] = 0.49 }
    }
    const paperish = () => Math.min(rgb[0], rgb[1], rgb[2]) > DUST.paperFrom
    for (let i = 0; i < n; i += 1) {
      // The board's colours carry the cloud: a place on the bare paper is mostly passed over for another, so the
      // blue backing, the clip, the words and the print give most of the pieces.
      let u = Math.random()
      let v = Math.random()
      colourAt(u, v)
      for (let k = 0; k < DUST.paperTries && paperish() && Math.random() > DUST.paperKeep; k += 1) { u = Math.random(); v = Math.random(); colourAt(u, v) }
      const paper = paperish()
      p.set((u - 0.5) * from.widthM, (0.5 - v) * from.heightM, 0)
      const ahead = (p.x * lead.x + p.y * lead.y) / (reach || 1)
      p.applyMatrix4(from.matrix)
      pos[i * 3] = p.x; pos[i * 3 + 1] = p.y; pos[i * 3 + 2] = p.z
      // Dark ink and the clip are lifted, so that they glow a little against the dark room; the paper is dimmed.
      const lum = 0.3 * rgb[0] + 0.59 * rgb[1] + 0.11 * rgb[2]
      // The ink of the words and the clip, near black, are drawn light, so that the words read as light for an instant.
      if (!paper && lum < 0.2) { tint[i * 3] = DUST.inkLight[0]; tint[i * 3 + 1] = DUST.inkLight[1]; tint[i * 3 + 2] = DUST.inkLight[2] }
      else {
        const lift = paper ? 0.5 : lum < 0.35 ? 1.5 : 1.1
        const add = !paper && lum < 0.35 ? 0.1 : 0
        tint[i * 3] = Math.min(1, rgb[0] * lift + add); tint[i * 3 + 1] = Math.min(1, rgb[1] * lift + add); tint[i * 3 + 2] = Math.min(1, rgb[2] * lift + add)
      }
      // The board holds together a moment from the edge that leads, then drifts; the paper's pieces fade first.
      const delay = still ? 0 : loud ? DUST.holdS + (1 - (ahead + 1) / 2) * kind.leadS : Math.random() * kind.leadS
      const life = (still ? DUST.stillS : kind.lifeS[0] + Math.random() * (kind.lifeS[1] - kind.lifeS[0])) * (paper && !still ? DUST.paperLife : 1)
      timing[i * 2] = delay; timing[i * 2 + 1] = life
      // Most pieces fine, a few larger: the size squared, so small ones are many.
      const gr = Math.random()
      grain[i] = (DUST.grain[0] + (DUST.grain[1] - DUST.grain[0]) * gr * gr) * (paper ? DUST.paperGrain : 1)
      last = Math.max(last, delay + life)
      const s = (Math.random() - 0.5) * 2
      // Its way: the throw's (or a drift up), and a spread of its own. One vector serves every piece.
      go.set((Math.random() - 0.5) * 2, (Math.random() - 0.5) * 2, (Math.random() - 0.5) * 2).multiplyScalar(kind.spreadMps)
      if (still) go.set(0, 0, 0)
      else if (dir) go.addScaledVector(dir, kind.speedMps * (0.7 + 0.3 * s))
      else go.y += 0.07 + Math.random() * 0.05
      vel[i * 3] = go.x; vel[i * 3 + 1] = go.y; vel[i * 3 + 2] = go.z
    }
    const g = new BufferGeometry()
    g.setAttribute('position', new BufferAttribute(pos, 3))
    g.setAttribute('vel', new BufferAttribute(vel, 3))
    g.setAttribute('tint', new BufferAttribute(tint, 3))
    g.setAttribute('timing', new BufferAttribute(timing, 2))
    g.setAttribute('grain', new BufferAttribute(grain, 1))
    // The size at which the pieces would tile the board; round pieces of about that size still read as it.
    const piece = Math.sqrt((from.widthM * from.heightM) / n) * 1.6
    return { geometry: g, material: dustMaterial(piece, still ? 0 : DUST.shrink), lastS: last }
    // One leaving is drawn once: what it is made of does not change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => () => { geometry.dispose(); material.dispose() }, [geometry, material])
  useFrame(() => {
    const now = performance.now()
    if (born.current === null) born.current = now
    const pinned = dustClock.pinnedS
    const t = pinned ?? (now - born.current) / 1000
    material.uniforms.uTime.value = t
    // Pixels a metre a metre away: the picture's height over the lens's height.
    const fov = 'fov' in camera ? (camera as { fov: number }).fov : 50
    material.uniforms.uScale.value = size.height / (2 * Math.tan((fov * Math.PI) / 360))
    if (pinned === null && t > lastS + 0.05) onDone()
  })
  return <points geometry={geometry} material={material} frustumCulled={false} pointerEvents="none" />
}
