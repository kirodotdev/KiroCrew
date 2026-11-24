/**
 * The land and what stands on it, drawn the framework way: the far ring and the ridge, the grid, each plot's
 * outline, one stand-in shape for each object of the scene document, and the Bots.
 *
 * The scene document (`sceneDoc.ts`) is the plain data. The world (`world.ts`) reads each bound field and keeps
 * where each thing stands, as a plain transform. This file draws what they say and nothing else: give the world
 * another document and other shapes appear. Every shape here is a stand-in; the art pass replaces them.
 */
import { useFrame } from '@react-three/fiber'
import { useMemo, useRef, useSyncExternalStore } from 'react'
import { BufferGeometry, DoubleSide, Float32BufferAttribute, Vector3, type LineBasicMaterial, type MeshBasicMaterial, type Object3D } from 'three'
import { Bots } from './Bot'
import { PLOT_M } from './land'
import { At } from './Marks'
import { XR_GL } from './palette'
import type { BoundValue, ObjectKind } from './sceneDoc'
import type { World, WorldColors } from './world'

const stateColor = (v: BoundValue, c: WorldColors): number => (v.state === 'done' ? XR_GL.dim : v.state === 'idle' ? XR_GL.faint : c.state[v.state])

/** What a stand-in shape is made of, for the frame that colours and moves it. */
interface Parts { body: MeshBasicMaterial | null; accent: MeshBasicMaterial | null; part: Object3D | null }

/** One plain shape per kind. Each shows the bound value in the simplest way that can be read. */
function Shape({ kind, hold }: { kind: ObjectKind; hold: Parts }) {
  const body = <meshBasicMaterial ref={m => { hold.body = m }} color={XR_GL.faint} />
  const accent = <meshBasicMaterial ref={m => { hold.accent = m }} color={XR_GL.dim} side={DoubleSide} />
  const part = (m: Object3D | null) => { hold.part = m }
  switch (kind) {
    case 'home': return <mesh position-y={0.5}><coneGeometry args={[0.75, 1, 4]} />{body}</mesh>
    case 'work': return (
      <>
        <mesh position-y={0.275}><boxGeometry args={[0.55, 0.55, 0.55]} />{body}</mesh>
        <mesh ref={part} position-y={0.82}><boxGeometry args={[0.16, 0.16, 0.16]} />{accent}</mesh>
      </>
    )
    case 'ask': return (
      <>
        <mesh position-y={0.55}><cylinderGeometry args={[0.04, 0.04, 1.1, 8]} />{body}</mesh>
        <mesh ref={part} position-y={1.2}><sphereGeometry args={[0.17, 14, 10]} />{accent}</mesh>
      </>
    )
    case 'goal': return (
      <>
        <mesh position-y={0.8}><cylinderGeometry args={[0.03, 0.03, 1.6, 8]} />{body}</mesh>
        <mesh ref={part}><cylinderGeometry args={[0.1, 0.1, 1, 12]} />{accent}</mesh>
        <mesh position-y={1.33} rotation-x={-Math.PI / 2}><ringGeometry args={[0.17, 0.215, 24]} /><meshBasicMaterial color={XR_GL.ink} side={DoubleSide} /></mesh>
      </>
    )
    case 'clock': return (
      <>
        <mesh position-y={0.5}><cylinderGeometry args={[0.035, 0.035, 1, 8]} />{body}</mesh>
        <mesh position-y={1.2}><circleGeometry args={[0.24, 24]} />{accent}</mesh>
        {/* The hand turns about its foot, a hair in front of the face. */}
        <group ref={part} position-y={1.2}>
          <mesh position={[0, 0.1, 0.012]}><boxGeometry args={[0.03, 0.2, 0.01]} /><meshBasicMaterial color={XR_GL.ink} /></mesh>
        </group>
      </>
    )
    case 'built': return (
      <>
        <mesh position-y={0.225}><boxGeometry args={[0.6, 0.45, 0.6]} />{body}</mesh>
        <mesh position-y={0.625}><coneGeometry args={[0.5, 0.35, 4]} />{accent}</mesh>
      </>
    )
    case 'marker': return (
      <>
        <mesh position-y={0.45}><cylinderGeometry args={[0.02, 0.02, 0.9, 6]} />{body}</mesh>
        <mesh position-y={0.95}><sphereGeometry args={[0.14, 14, 10]} />{accent}</mesh>
      </>
    )
    // The outline of a screen, upright and facing the way the object is turned.
    case 'screen': return (
      <>
        <mesh position-y={0.15}><boxGeometry args={[0.52, 0.3, 0.004]} /><meshBasicMaterial color={XR_GL.raised} transparent opacity={0.55} depthWrite={false} /></mesh>
        <lineLoop geometry={SCREEN_EDGE}><lineBasicMaterial color={XR_GL.ink} /></lineLoop>
      </>
    )
    default: return <mesh position-y={0.25}><boxGeometry args={[0.5, 0.5, 0.5]} />{body}</mesh>
  }
}

const SCREEN_EDGE = new BufferGeometry().setFromPoints([new Vector3(-0.26, 0, 0), new Vector3(0.26, 0, 0), new Vector3(0.26, 0.3, 0), new Vector3(-0.26, 0.3, 0)])

/** One object of the scene document: its shape, coloured and moved by the real field it is bound to. */
function Thing({ world, id, kind }: { world: World; id: string; kind: ObjectKind }) {
  const hold = useMemo<Parts>(() => ({ body: null, accent: null, part: null }), [])
  const at = world.placeOf(id)
  useFrame(() => {
    const d = world.objectValue(id)
    if (!d) return
    const { value: v } = d
    const bound = !!d.o.bind
    const c = world.colors
    const clock = performance.now()
    const { body, accent, part } = hold
    if (body) {
      body.color.setHex(bound ? stateColor(v, c) : XR_GL.dim)
      // An object whose field is gone is still there, and plainly not live.
      body.opacity = bound && !v.found ? 0.3 : 1
      body.transparent = body.opacity < 1
    }
    if (kind === 'home' && body && bound && v.found) body.color.setHex(v.state === 'idle' ? XR_GL.dim : c.state[v.state as keyof WorldColors['state']] ?? XR_GL.dim)
    if (kind === 'work' && part) {
      accent?.color.setHex(XR_GL.ink)
      // Work that runs is seen to move.
      part.visible = v.state === 'working'
      if (!world.still) { part.rotation.y = clock / 500; part.position.y = 0.82 + Math.sin(clock / 300) * 0.05 }
    }
    if (kind === 'ask' && part) {
      accent?.color.setHex(bound && v.found ? c.state.needs : XR_GL.dim)
      part.scale.setScalar(world.still ? 1 : 1 + Math.sin(clock / 260) * 0.12)
    }
    if (kind === 'goal' && part) {
      body?.color.setHex(XR_GL.faint)
      accent?.color.setHex(c.state.working)
      // The ring is the target. The column is the reading; it is left out when the reading is not a plain number.
      part.visible = v.level !== null
      const h = Math.max(0.02, (v.level ?? 0) * 1.33)
      part.scale.y = h
      part.position.y = h / 2
    }
    if (kind === 'clock' && part) {
      accent?.color.setHex(v.at > 0 && v.at - world.nowS < 3600 ? c.state.working : XR_GL.raised)
      // The hand shows how long until the next run, once round for twelve hours. No hand: no next run is known.
      part.visible = v.at > 0
      part.rotation.z = -(((v.at - world.nowS) / 3600) % 12) / 12 * Math.PI * 2
    }
    if (kind === 'built') accent?.color.setHex(XR_GL.faint)
    if (kind === 'marker') accent?.color.setHex(bound && v.found ? stateColor(v, c) : XR_GL.ink)
  })
  return at ? <At at={at}><Shape kind={kind} hold={hold} /></At> : null
}

/** The outline of one Bot's plot. It takes the Bot's colour when the Bot needs the person or has failed. */
function Outline({ world, slug }: { world: World; slug: string }) {
  const node = world.nodes.get(slug)
  const line = useRef<LineBasicMaterial>(null)
  useFrame(() => {
    const bot = world.nodes.get(slug)?.bot
    if (bot && line.current) line.current.color.setHex(bot.state === 'needs' || bot.state === 'failed' ? world.colors.state[bot.state] : XR_GL.edge)
  })
  return node ? <At at={node.outline}><lineLoop geometry={PLOT_EDGE}><lineBasicMaterial ref={line} color={XR_GL.edge} /></lineLoop></At> : null
}

const HALF = (PLOT_M * 0.94) / 2
const PLOT_EDGE = new BufferGeometry().setFromPoints([new Vector3(-HALF, 0.02, -HALF), new Vector3(HALF, 0.02, -HALF), new Vector3(HALF, 0.02, HALF), new Vector3(-HALF, 0.02, HALF)])

/** A far, low ridge all round the seat: a stand-in for the landscape the person sits in. It does not move with the land. */
function ridge(): BufferGeometry {
  const pts: number[] = []
  const idx: number[] = []
  const N = 120
  for (let i = 0; i <= N; i += 1) {
    const a = (i / N) * Math.PI * 2
    const h = 2.6 + 1.8 * Math.sin(3 * a + 1) + 1.1 * Math.sin(7 * a + 2) + 0.6 * Math.sin(13 * a)
    pts.push(Math.sin(a) * 44, -1.3, -Math.cos(a) * 44, Math.sin(a) * 44, h, -Math.cos(a) * 44)
    if (i < N) idx.push(i * 2, i * 2 + 1, i * 2 + 2, i * 2 + 1, i * 2 + 3, i * 2 + 2)
  }
  const g = new BufferGeometry()
  g.setAttribute('position', new Float32BufferAttribute(pts, 3))
  g.setIndex(idx)
  return g
}

/** The grid covers the plots and stops: in the miniature it is the edge of the model on the desk. */
function grid(world: World): BufferGeometry {
  const { cx, cz, radius } = world.bounds
  const r = Math.floor(radius / (PLOT_M / 2)) * (PLOT_M / 2)
  const pts: number[] = []
  for (let v = -r; v <= r + 1e-6; v += PLOT_M / 2) pts.push(cx + v, 0, cz - r, cx + v, 0, cz + r, cx - r, 0, cz + v, cx + r, 0, cz + v)
  const g = new BufferGeometry()
  g.setAttribute('position', new Float32BufferAttribute(pts, 3))
  return g
}

export function Land({ world }: { world: World }) {
  // Who is in the crew, which things there are and how the plots lie: the world says when any of them changes.
  const stamp = useSyncExternalStore(world.watch, () => world.stamp)
  const far = useRef<MeshBasicMaterial>(null)
  const ridgeGeo = useMemo(ridge, [])
  // `stamp` counts up when the plots are laid out again: the grid is cut to the plots as they are then.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const gridGeo = useMemo(() => grid(world), [world, stamp])
  useFrame(() => {
    // The far ring comes in as the land grows to full size.
    if (far.current) far.current.opacity = 0.5 * world.view.t * world.view.t
  })
  const { env } = world.doc
  return (
    <>
      <At at={world.land}>
        <mesh rotation-x={-Math.PI / 2} position-y={0.01} visible={env.horizon}>
          <ringGeometry args={[58, 58.3, 96]} />
          <meshBasicMaterial ref={far} color={XR_GL.faint} transparent opacity={0} depthWrite={false} />
        </mesh>
        {env.ground === 'grid' && (
          <lineSegments geometry={gridGeo}>
            <lineBasicMaterial color={XR_GL.edge} transparent opacity={0.5} />
          </lineSegments>
        )}
        <Bots world={world} />
      </At>
      <At at={world.seat}>
        <mesh geometry={ridgeGeo} renderOrder={-1} visible={env.horizon}>
          <meshBasicMaterial color={XR_GL.faint} transparent opacity={0.28} depthWrite={false} side={DoubleSide} />
        </mesh>
      </At>
      {[...world.nodes.keys()].map(slug => <Outline key={slug} world={world} slug={slug} />)}
      {world.doc.objects.map(o => <Thing key={`${o.id}:${o.kind}`} world={world} id={o.id} kind={o.kind} />)}
    </>
  )
}
