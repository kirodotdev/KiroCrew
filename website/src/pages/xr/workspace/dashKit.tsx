/**
 * The kit of a Bot's 3D dashboard (`dash3d.ts`): the four parts a layout can name, each a solid thing with depth
 * and light on it, like the pieces of a desk model, and the text that belongs to them. `Dash3d.tsx` stands them in a
 * row on one tray.
 *
 * A part stands in its own box (`FORM.part`: 8 cm wide, 16 cm tall, 7 cm deep), its middle at x = 0 and z = 0, its
 * foot on the tray at y = 0. What it shows is a number the page read (`DashValue`); how it looks is the page's: a
 * layout picks a part's kind and nothing of its look.
 *
 *   number  a block on a foot; its reading is the number on the lip under it
 *   bar     a glass column with a level inside; what is over the limit stands over the glass in another colour
 *   ring    a ring on a post, open in the middle, that fills clockwise from the top; a whole ring is done
 *   lamps   up to twelve bulbs on two rails, one for each thing; colour and shape are its state
 *
 * A part is two things. What never moves (`partPieces`: its foot, its post, its rails, the block) is a list of plain
 * pieces, which `Dash3d.tsx` draws one by one while the dashboard is taken and as ONE merged mesh with the tray
 * while it rests (`mergePieces`), so that a tray at rest costs a handful of draw calls however many pieces it has.
 * What moves (a ring's fill, a level, the bulbs) is drawn by `DashPartBody` in either case; the bulbs of one part
 * are one draw.
 *
 * The colours are the data's own (`dataHues`), never the ones the room draws a Bot's state in. A bulb says its state
 * by its shape too: one that works or is done is whole, one that failed is pressed flat, one that rests is small and
 * dark. A part with nothing to show, or whose values are old, is drawn in the room's grey.
 *
 * Text is drawn to a canvas with `fillText` and its `maxWidth`, as the board's is: a name is the Bot's characters as
 * pixels, never markup, and it cannot be wider than its box. It has no backing of its own: it lies on the tray's lip.
 */
import { useFrame } from '@react-three/fiber'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  BoxGeometry, BufferAttribute, CanvasTexture, Color, CylinderGeometry, Euler, LinearFilter, Matrix4, Object3D, Quaternion, SRGBColorSpace, SphereGeometry,
  TorusGeometry, Vector3, type BufferGeometry, type InstancedMesh, type Mesh,
} from 'three'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'
import { XR_GL, css } from '../palette'
import { wrapLines } from '../text'
import { DASH, FORM, type DashKind, type DashValue, type DataHues, type LampState } from './dash3d'

/** A value on its way to a new reading comes this near to it in a second (as a rate of an ease-out). */
const EASE_PER_S = 9

/** How tall each kind of part stands over the tray. */
export const PART_TOP: Record<DashKind, number> = { number: 0.062, bar: 0.146, ring: 0.146, lamps: 0.088 }

const BLOCK = { wide: 0.1, tall: 0.062, deep: 0.05, foot: 0.008 } as const
const BAR = { radius: 0.021, tall: 0.14, level: 0.016, foot: 0.006 } as const
const RING = { radius: 0.05, track: 0.006, tube: 0.012, post: 0.022, steps: 48 } as const
/** The bulbs: six on a rail; the second rail stands behind the first and higher. */
const LAMP = { radius: 0.0085, step: 0.019, perRail: 6, rail: [{ y: 0.026, z: 0.014 }, { y: 0.064, z: -0.014 }] } as const
const RAIL_WIDE = LAMP.perRail * LAMP.step + 0.006

/** The shapes of the kit: made once, shared by every dashboard, never thrown away. */
const GEO = {
  box: new BoxGeometry(1, 1, 1),
  barFoot: new CylinderGeometry(BAR.radius + 0.006, BAR.radius + 0.008, BAR.foot, 20),
  barRim: new TorusGeometry(BAR.radius, 0.0018, 8, 32),
  level: new CylinderGeometry(BAR.level, BAR.level, BAR.tall, 20),
  glass: new CylinderGeometry(BAR.radius, BAR.radius, BAR.tall, 20, 1, true),
  ringPost: new CylinderGeometry(0.006, 0.012, RING.post, 20),
  ringTrack: new TorusGeometry(RING.radius, RING.track, 10, 40),
  railPost: new CylinderGeometry(0.003, 0.003, 1, 10),
  socket: new TorusGeometry(0.028, 0.003, 8, 32),
  bulb: new SphereGeometry(LAMP.radius, 12, 10),
} as const

type V3 = readonly [number, number, number]
/** One piece that never moves: a shape of the kit, where it stands in its part, and its colour. */
export interface Piece { geo: BufferGeometry; at: V3; rot?: V3; scale?: V3; color: number }

/** The pieces of a part that never move, for what the part shows now. A socket is the place of a part that is wrong. */
export function partPieces(kind: DashKind | 'socket', value: DashValue | null, hues: DataHues, dim: boolean): Piece[] {
  const metal = dim ? XR_GL.faint : XR_GL.dim
  if (kind === 'socket') return [{ geo: GEO.socket, at: [0, 0.003, 0], rot: [Math.PI / 2, 0, 0], color: XR_GL.faint }]
  if (kind === 'number') {
    const on = !!value?.found && !dim
    return [
      { geo: GEO.box, at: [0, BLOCK.foot / 2, 0], scale: [BLOCK.wide, BLOCK.foot, BLOCK.deep + 0.008], color: metal },
      { geo: GEO.box, at: [0, BLOCK.foot + (BLOCK.tall - BLOCK.foot) / 2, 0], scale: [BLOCK.wide - 0.012, BLOCK.tall - BLOCK.foot, BLOCK.deep], color: on ? hues.fill : XR_GL.faint },
    ]
  }
  if (kind === 'bar') return [
    { geo: GEO.barFoot, at: [0, BAR.foot / 2, 0], color: metal },
    { geo: GEO.barRim, at: [0, BAR.foot + BAR.tall, 0], rot: [Math.PI / 2, 0, 0], color: metal },
  ]
  if (kind === 'ring') return [
    { geo: GEO.ringPost, at: [0, RING.post / 2, 0], color: metal },
    { geo: GEO.ringTrack, at: [0, RING.post + RING.radius + RING.tube, 0], color: dim ? XR_GL.faint : XR_GL.edge },
  ]
  const rails = (value?.found ? value.lamps.length : 0) > LAMP.perRail ? 2 : 1
  return LAMP.rail.slice(0, rails).flatMap((r): Piece[] => [
    { geo: GEO.box, at: [0, r.y, r.z], scale: [RAIL_WIDE, 0.005, 0.008], color: metal },
    ...[-1, 1].map((side): Piece => ({ geo: GEO.railPost, at: [side * (RAIL_WIDE / 2 - 0.004), r.y / 2, r.z], scale: [1, r.y, 1], color: metal })),
  ])
}

/** The tray's own pieces, `wideM` wide: first its body, then the rim on it that a pointer lights. */
export function trayPieces(wideM: number, grey: boolean, lit: boolean): Piece[] {
  return [
    { geo: GEO.box, at: [0, -FORM.trayThickM / 2, 0], scale: [wideM, FORM.trayThickM, FORM.trayDeepM], color: grey ? XR_GL.edge : XR_GL.raised },
    { geo: GEO.box, at: [0, 0.0012, 0], scale: [wideM - 0.008, 0.0024, FORM.trayDeepM - 0.008], color: lit ? XR_GL.dim : XR_GL.edge },
  ]
}

/** Pieces drawn one by one: while a dashboard is taken, and its parts slide. */
export function Pieces({ list }: { list: readonly Piece[] }) {
  return (
    <>
      {list.map((p, i) => (
        <mesh key={i} geometry={p.geo} position={p.at as [number, number, number]} rotation={(p.rot ?? [0, 0, 0]) as [number, number, number]} scale={(p.scale ?? [1, 1, 1]) as [number, number, number]}>
          <meshLambertMaterial color={p.color} toneMapped={false} />
        </mesh>
      ))}
    </>
  )
}

/**
 * Many pieces as one shape, each moved `dx` across: for a tray at rest, which is then one draw. The colour of a
 * piece is carried by its points. The caller throws the shape away when it makes another.
 */
export function mergePieces(list: readonly { piece: Piece; dx: number }[]): BufferGeometry | null {
  const m = new Matrix4()
  const q = new Quaternion()
  const e = new Euler()
  const c = new Color()
  const made = list.map(({ piece, dx }) => {
    const g = piece.geo.clone()
    g.deleteAttribute('uv')
    q.setFromEuler(e.set(...(piece.rot ?? [0, 0, 0]) as [number, number, number]))
    g.applyMatrix4(m.compose(new Vector3(piece.at[0] + dx, piece.at[1], piece.at[2]), q, new Vector3(...(piece.scale ?? [1, 1, 1]))))
    c.setHex(piece.color)
    const n = g.getAttribute('position').count
    const colors = new Float32Array(n * 3)
    for (let i = 0; i < n; i += 1) colors.set([c.r, c.g, c.b], i * 3)
    g.setAttribute('color', new BufferAttribute(colors, 3))
    return g
  })
  const merged = made.length ? mergeGeometries(made) : null
  for (const g of made) g.dispose()
  return merged
}

/** The share a part shows now, eased toward the one it was given; at once with reduced motion. */
function useEased(target: number, still: boolean, onStep: (shown: number) => void) {
  const shown = useRef(target)
  const step = useRef(onStep)
  step.current = onStep
  useEffect(() => { step.current(shown.current) }, [])
  useFrame((_s, dt) => {
    const d = target - shown.current
    if (d === 0) return
    shown.current = still || Math.abs(d) < 0.002 ? target : shown.current + d * Math.min(1, dt * EASE_PER_S)
    step.current(shown.current)
  })
}

interface PartProps { value: DashValue; hues: DataHues; dim: boolean; still: boolean }

/** What moves of a bar: the level in the glass, what stands over the limit, and the glass, drawn after them. */
function BarMoving({ value, hues, dim, still }: PartProps) {
  const fill = useRef<Mesh>(null)
  const over = useRef<Mesh>(null)
  const on = value.found && !dim
  useEased(on ? value.share ?? 0 : 0, still, s => {
    const f = fill.current
    const o = over.current
    if (!f || !o) return
    const inside = Math.min(1, s)
    f.visible = inside > 0.001
    f.scale.y = Math.max(0.001, inside)
    f.position.y = BAR.foot + (BAR.tall * inside) / 2
    const past = Math.max(0, s - 1)
    o.visible = past > 0.001
    o.scale.y = Math.max(0.001, past)
    o.position.y = BAR.foot + BAR.tall + 0.003 + (BAR.tall * past) / 2
  })
  return (
    <>
      <mesh ref={fill} geometry={GEO.level} visible={false}>
        <meshLambertMaterial color={hues.fill} emissive={hues.fill} emissiveIntensity={0.15} toneMapped={false} />
      </mesh>
      <mesh ref={over} geometry={GEO.level} visible={false}>
        <meshLambertMaterial color={hues.failed} emissive={hues.failed} emissiveIntensity={0.15} toneMapped={false} />
      </mesh>
      <mesh geometry={GEO.glass} position={[0, BAR.foot + BAR.tall / 2, 0]} renderOrder={2}>
        <meshLambertMaterial color={XR_GL.ink} transparent opacity={0.2} depthWrite={false} toneMapped={false} />
      </mesh>
    </>
  )
}

/** What moves of a ring: its fill, an arc made again only when the share moves by a step of the ring. */
function RingMoving({ value, hues, dim, still }: PartProps) {
  const on = value.found && !dim
  const [step, setStep] = useState(0)
  useEased(on ? value.share ?? 0 : 0, still, s => {
    const next = Math.round(Math.min(1, s) * RING.steps)
    setStep(was => (was === next ? was : next))
  })
  const past = on && (value.share ?? 0) > 1
  if (step <= 0) return null
  const color = past ? hues.failed : hues.fill
  return (
    // From the top, clockwise as the person sees it: the arc is turned to start at the top and mirrored.
    <mesh position={[0, RING.post + RING.radius + RING.tube, 0]} rotation={[0, Math.PI, Math.PI / 2]}>
      <torusGeometry args={[RING.radius, RING.tube, 12, Math.max(3, step), (step / RING.steps) * Math.PI * 2]} />
      <meshLambertMaterial color={color} emissive={color} emissiveIntensity={0.15} toneMapped={false} />
    </mesh>
  )
}

/** A bulb's colour and its shape: so tall against its width, and how large. */
function lampLook(state: LampState, hues: DataHues, dim: boolean): { color: number; flat: number; size: number } {
  if (dim) return { color: XR_GL.faint, flat: 1, size: 1 }
  if (state === 'working') return { color: hues.fill, flat: 1, size: 1 }
  if (state === 'done') return { color: hues.done, flat: 1, size: 1 }
  if (state === 'failed') return { color: hues.failed, flat: 0.4, size: 1 }
  if (state === 'idle') return { color: XR_GL.faint, flat: 1, size: 0.62 }
  return { color: XR_GL.dim, flat: 1, size: 0.8 }
}

/** What moves of the lamps: the bulbs, all of one part in one draw. */
function LampsMoving({ value, hues, dim }: PartProps) {
  const ref = useRef<InstancedMesh>(null)
  const lamps = value.found ? value.lamps : []
  const key = `${lamps.join()}|${dim}|${hues.fill}|${hues.done}|${hues.failed}`
  useLayoutEffect(() => {
    const mesh = ref.current
    if (!mesh) return
    const at = new Object3D()
    const color = new Color()
    lamps.forEach((state, i) => {
      const look = lampLook(state, hues, dim)
      const r = LAMP.rail[Math.floor(i / LAMP.perRail)] ?? LAMP.rail[0]
      at.position.set(((i % LAMP.perRail) - (LAMP.perRail - 1) / 2) * LAMP.step, r.y + 0.0025 + LAMP.radius * look.size * look.flat, r.z)
      at.scale.set(look.size, look.size * look.flat, look.size)
      at.updateMatrix()
      mesh.setMatrixAt(i, at.matrix)
      mesh.setColorAt(i, color.setHex(look.color))
    })
    mesh.count = lamps.length
    mesh.instanceMatrix.needsUpdate = true
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true
    // `key` stands for the lamps, the colours and whether they are old.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  return (
    // Its bulbs stand where the part is, so it is drawn whenever the part is: no test of its own against the view.
    <instancedMesh ref={ref} args={[GEO.bulb, undefined, DASH.mostLamps]} frustumCulled={false}>
      <meshLambertMaterial toneMapped={false} />
    </instancedMesh>
  )
}

/**
 * One part of the kit, by its kind: what moves of it, and with `solid` also its pieces that never move (while
 * the dashboard is taken; at rest they are in the tray's one merged shape).
 */
export function DashPartBody({ kind, solid, ...props }: PartProps & { kind: DashKind; solid: boolean }) {
  return (
    <>
      {solid && <Pieces list={partPieces(kind, props.value, props.hues, props.dim)} />}
      {kind === 'bar' && <BarMoving {...props} />}
      {kind === 'ring' && <RingMoving {...props} />}
      {kind === 'lamps' && <LampsMoving {...props} />}
    </>
  )
}

/** The picture of a label: so many px for a metre, so that a letter of 2 cm is 60 px tall. */
const PX_PER_M = 3000

interface LabelProps {
  text: string
  /** The box it is written in, in metres, and the text's size. */
  widthM: number
  heightM: number
  sizeM: number
  /** At most so many lines; a line that is still too wide is pressed into the box's width. */
  lines?: number
  /** False: the size stays `sizeM` and text past `lines` is cut by whole signs with "…", never made smaller. */
  shrink?: boolean
  color: number
  weight?: number
  position: [number, number, number]
  name?: string
}

/**
 * Text of the taken form, drawn to a canvas with no backing: middle of its box, wrapped at spaces to at most `lines`
 * lines, made smaller until its lines fit (down to 0.7 of its size), and then written with the box's width as
 * `maxWidth`.
 */
export function DashLabel({ text, widthM, heightM, sizeM, lines = 1, shrink = true, color, weight = 600, position, name }: LabelProps) {
  const texture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(2, Math.round(widthM * PX_PER_M))
    canvas.height = Math.max(2, Math.round(heightM * PX_PER_M))
    const c = canvas.getContext('2d')
    if (c) {
      const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
      const room = canvas.width - 8
      let size = sizeM * PX_PER_M
      let wrapped: string[] = [text]
      for (let i = 0; i < 4; i += 1) {
        c.font = `${weight} ${Math.round(size)}px ${family}`
        const got = wrapLines(s => c.measureText(s).width, text, room, lines)
        wrapped = got.lines.length ? got.lines : [text]
        if (!shrink || (!got.clipped && wrapped.every(l => c.measureText(l).width <= room))) break
        if (i < 3) size *= 0.9
      }
      c.fillStyle = css(color)
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      const lineH = size * 1.15
      const top = canvas.height / 2 - ((wrapped.length - 1) * lineH) / 2
      wrapped.forEach((line, i) => c.fillText(line, canvas.width / 2, top + i * lineH, room))
    }
    const t = new CanvasTexture(canvas)
    t.colorSpace = SRGBColorSpace
    t.minFilter = LinearFilter
    t.generateMipmaps = false
    return t
  }, [text, widthM, heightM, sizeM, lines, shrink, color, weight])
  useEffect(() => () => texture.dispose(), [texture])
  return (
    <mesh name={name} position={position} scale={[widthM, heightM, 1]} userData={{ text }} pointerEvents="none" renderOrder={3}>
      <planeGeometry />
      <meshBasicMaterial map={texture} transparent depthWrite={false} toneMapped={false} />
    </mesh>
  )
}

/** The lights the parts are seen by: theirs alone, since nothing else in the room is lit. From over his shoulder. */
export function DashLights() {
  return (
    <>
      <hemisphereLight args={[0xffffff, 0x2a3140, 1.2]} />
      <directionalLight position={[0.6, 1.6, 1.0]} intensity={1.8} />
      <directionalLight position={[-0.8, 0.4, -0.6]} intensity={0.6} />
    </>
  )
}
