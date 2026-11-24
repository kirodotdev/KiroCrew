/**
 * A Bot's body, drawn the framework way: a component, a loaded model, and motion in `useFrame`.
 *
 * The body is one ghost for every Bot: a round body with two black oval eyes, and nothing else on its face.
 * A Bot is told apart by its colour, by what it wears (the art the dashboard draws, laid on the front of the
 * body) and by its name. The model is `public/vendor/xr-bot/ghost.glb` (320,844 bytes, no texture): a body of
 * 6,136 triangles with a hand on each side (its two flicks, each the other's mirror picture), eyes of 168 that
 * carry four shapes (blink, wide, lookX, lookY), five bones (belly, head, hem, flick, flick2), and the front of
 * the skin once more (2,994 triangles), for what the Bot wears. A Bot that wears nothing is 6,304 triangles in 2
 * draw calls. The file's second scene holds things to wear as solids; the page loads the first scene only and
 * does not draw them.
 *
 * The scene decides what a Bot does and where it stands. It keeps that in a `BotDrive` (`botDrive.ts`), which
 * `botMotion.ts` turns into springs. The components here step it once a frame and write the result to the bones.
 * The skin is matte (`botSkin.ts`), and the lower body gives and trails as a light sheet does (`botFlow.ts`):
 * the flow is stepped last in the frame, from where the bones and the Bot then are. What a Bot wears is the
 * dashboard's flat art with its black ink drawn pale: on glasses that add light, black is clear glass.
 */
import { useFrame, useLoader } from '@react-three/fiber'
import { Suspense, useEffect, useLayoutEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from 'react'
import { DoubleSide, MeshBasicMaterial, Vector3, type Bone, type Group, type SkinnedMesh } from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { clone as cloneSkinned } from 'three/examples/jsm/utils/SkeletonUtils.js'
import type { BotState } from './body'
import { BOT_H, type BotDrive } from './botDrive'
import { FLOW, flowInto, WaveFlow } from './botFlow'
import type { BotFrame } from './botMotion'
import { paleInkInto, WEAR_KEY } from './botShader'
import { cloth, wornInk } from './botSkin'
import { GLOW } from './character'
import { ModelGuard } from './ModelGuard'
import type { BotLook, BotNode, World, WorldColors } from './world'

/** The model ships in the build, next to the other vendored files the gateway serves. No request leaves this origin. */
const BODY_URL = `${import.meta.env.BASE_URL}vendor/xr-bot/ghost.glb`
/** The body squashes about this height, not about its feet: a thing that floats has no floor. */
const SQUASH_AT = 0.4
/** The ring at a Bot's feet, wide enough to show all round the body. */
const RING = [0.56 * BOT_H, 0.6 * BOT_H] as const

/** The eyes of every Bot: black, and drawn just in front of the skin they lie on. */
const EYE_INK = new MeshBasicMaterial({ color: 0x000000, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 })

/** The ring at a Bot's feet. Its shape says the state: none, moving dashes, whole, or broken. */
function StateRing({ state, color, drive }: { state: BotState; color: number; drive: BotDrive }) {
  const ring = useRef<Group>(null)
  useFrame((_s, dt) => { if (ring.current && state === 'working' && !drive.still) ring.current.rotation.y += dt * 0.6 })
  const arcs: [number, number][] = state === 'working' ? [0, 1, 2, 3, 4, 5].map(i => [(i * Math.PI) / 3, Math.PI / 5])
    : state === 'needs' ? [[0, Math.PI * 2]] : state === 'failed' ? [[0.5, Math.PI * 0.8], [0.5 + Math.PI, Math.PI * 0.8]] : []
  return (
    <group ref={ring} position-y={0.003}>
      {arcs.map(([start, length]) => (
        <mesh key={start} rotation-x={-Math.PI / 2}>
          <ringGeometry args={[RING[0], RING[1], 48, 1, start, length]} />
          <meshBasicMaterial color={color} transparent opacity={GLOW[state].ring} side={DoubleSide} depthWrite={false} />
        </mesh>
      ))}
    </group>
  )
}

/** The beacon over a Bot that needs the person. It rides on the body, so it is as easy to find as the Bot at any zoom. */
function Beacon({ world, y }: { world: BotStage; y: number }) {
  const lamp = useRef<MeshBasicMaterial>(null)
  useFrame(() => { if (lamp.current) lamp.current.opacity = world.still ? 0.85 : 0.6 + 0.3 * Math.sin(performance.now() / 240) })
  return (
    <mesh position-y={y}>
      <boxGeometry args={[0.02, 0.5, 0.02]} />
      <meshBasicMaterial ref={lamp} color={world.colors.state.needs} transparent opacity={0.85} depthWrite={false} />
    </mesh>
  )
}

/** What a body needs of the place it stands in: the colours of the states, and whether motion is reduced. The land's `World` is one. */
export interface BotStage { colors: WorldColors; still: boolean }
/**
 * One Bot to draw. `bare` leaves out the ring at its feet and the beacon over its head: the land's own marks, sized
 * for the land. A place that draws Bots at another size brings its own (`workspace/Crew.tsx`).
 */
/**
 * The Bots (by slug) that stand in front of a page's edge which the head's dot takes now: drawn at `FADED_OPACITY`
 * (D78 1). Written by the workspace's dot each frame (`workspace/HeadAim.tsx`); empty elsewhere.
 */
export const fadedBots = new Set<string>()
export const FADED_OPACITY = 0.5

export interface BotProps { world: BotStage; node: Pick<BotNode, 'bot'>; look: BotLook; drive: BotDrive; bare?: boolean }

/**
 * Where a Bot stands, and what is round it whatever body it has: the ring at its feet and the beacon over its head.
 * It steps the Bot once a frame; `move` gets what that frame's motion is.
 */
function Stand({ world, node, drive, bare, move, children }: Omit<BotProps, 'look'> & { move?: (f: BotFrame, dt: number) => void; children: ReactNode }) {
  const root = useRef<Group>(null)
  useEffect(() => () => { drive.drawn = false }, [drive])
  useFrame((_state, delta) => {
    const g = root.current
    if (!g) return
    drive.drawn = true
    // A frame that came late is not a long step: the springs would jump.
    const dt = Math.min(delta, 0.1)
    const f = drive.frame(dt)
    g.position.copy(drive.root.position)
    g.rotation.y = drive.root.rotation.y
    g.scale.copy(drive.root.scale)
    move?.(f, dt)
  })
  const state = node.bot.state
  return (
    <group ref={root}>
      {children}
      {!bare && <StateRing state={state} color={world.colors.state[state]} drive={drive} />}
      {!bare && state === 'needs' && <Beacon world={world} y={BOT_H + 0.36} />}
    </group>
  )
}

export function Ghost({ world, node, look, drive, bare }: BotProps) {
  const gltf = useLoader(GLTFLoader, BODY_URL)
  const float = useRef<Group>(null)

  // Each Bot has its own bones. The skin's points are shared by all of them.
  const rig = useMemo(() => {
    const model = cloneSkinned(gltf.scene)
    const eyes = model.getObjectByName('eyes') as SkinnedMesh
    eyes.material = EYE_INK
    return {
      model, eyes, body: model.getObjectByName('body') as SkinnedMesh, wear: model.getObjectByName('wear') as SkinnedMesh, shape: eyes.morphTargetDictionary ?? {},
      belly: model.getObjectByName('belly') as Bone, head: model.getObjectByName('head') as Bone,
      // A body file without a flick's bone still flows: only that flick does not trail on its own.
      flick: (model.getObjectByName('flick') as Bone | undefined) ?? null,
      flick2: (model.getObjectByName('flick2') as Bone | undefined) ?? null,
    }
  }, [gltf])
  // The bones of this Bot go with it. The skin's points stay: every other Bot draws them too.
  useEffect(() => () => { for (const mesh of [rig.body, rig.eyes, rig.wear]) mesh.skeleton.dispose() }, [rig])
  const skin = useMemo(() => cloth(look.tint), [look.tint])
  // The flow puts its lines into the skin's shader, so it is made once the skin is on the body, and again for a new skin.
  const flow = useMemo(() => {
    rig.body.material = skin
    return new WaveFlow({ root: rig.model, body: rig.body, belly: rig.belly, flick: rig.flick, flick2: rig.flick2 }, { amount: 'middle', name: node.bot.slug })
  }, [rig, skin, node.bot.slug])
  useEffect(() => () => { skin.matcap?.dispose(); skin.dispose() }, [skin])
  // What the Bot wears, as the dashboard draws it, lies on the front of the body. A Bot that wears nothing has no such skin.
  const worn = useMemo(() => {
    if (!look.wear) return null
    // Art that faces the other way is on a body that faces the other way: it is read from its other side.
    look.wear.repeat.x = look.flip ? -1 : 1
    look.wear.offset.x = look.flip ? 1 : 0
    const art = new MeshBasicMaterial({ map: look.wear, transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 })
    // The dashboard draws what a Bot wears in black ink. Here a dark pixel is a hole in the body, so the ink is
    // drawn pale, in a tone of the Bot's own colour. The dashboard's art is not changed, and the eyes stay black.
    const ink = { value: new Vector3(...wornInk(look.tint)) }
    art.onBeforeCompile = shader => { if (paleInkInto(shader)) shader.uniforms.wearInk = ink }
    art.customProgramCacheKey = () => WEAR_KEY
    // The flat art bends exactly as the body under it: it reads the body's own numbers.
    flowInto(art, FLOW, flow.u)
    return art
  }, [look.wear, look.flip, look.tint, flow])
  useLayoutEffect(() => { rig.wear.visible = !!worn; if (worn) rig.wear.material = worn }, [rig, worn])
  useEffect(() => () => worn?.dispose(), [worn])
  // Half seen while the head's dot takes a page's edge behind it (D78 1): the picture says the page takes the press.
  // The skin and what it wears are this Bot's own; the eyes' ink is shared by every Bot and stays as it is.
  useFrame(() => {
    const opacity = fadedBots.has(node.bot.slug) ? FADED_OPACITY : 1
    if (skin.opacity === opacity) return
    const see = opacity < 1
    if (skin.transparent !== see) { skin.transparent = see; skin.depthWrite = !see; skin.needsUpdate = true }
    skin.opacity = opacity
    if (worn) worn.opacity = opacity
  })

  const move = (f: BotFrame, dt: number) => {
    const g = float.current
    if (!g) return
    g.position.y = BOT_H * (f.lift + SQUASH_AT * (1 - f.up))
    // The art faces left or right; the body faces the same way.
    g.scale.set(BOT_H * f.across * (look.flip ? -1 : 1), BOT_H * f.up, BOT_H * f.across)
    rig.belly.rotation.x = f.lean
    // To its +x side, in the frame it stands in: a body drawn mirrored has its own x the other way.
    rig.belly.rotation.z = -f.side * (look.flip ? -1 : 1)
    rig.head.rotation.set(f.head, 0, f.tilt)
    const m = rig.eyes.morphTargetInfluences
    if (m) {
      m[rig.shape.blink] = f.blink
      // A shut eye is shut, however big it was.
      m[rig.shape.wide] = f.wide * (1 - f.blink)
      m[rig.shape.lookX] = f.lookX
      m[rig.shape.lookY] = f.lookY
    }
    // Last in the frame: the flow reads where the bones and the Bot are now. With reduced motion the body is as the file has it.
    if (drive.still) flow.rest()
    else flow.step(dt, drive.poseNow, drive.roomMoves)
  }
  return (
    <Stand world={world} node={node} drive={drive} bare={bare} move={move}>
      <group ref={float}>
        <primitive object={rig.model} />
      </group>
    </Stand>
  )
}

/** The body of a Bot whose model could not be loaded: a plain egg in its colour. It stands, walks and shows its state as the ghost does. */
export function Plain({ world, node, look, drive, bare }: BotProps) {
  return (
    <Stand world={world} node={node} drive={drive} bare={bare}>
      <mesh position-y={BOT_H / 2} scale={[BOT_H * 0.34, BOT_H / 2, BOT_H * 0.34]}>
        <sphereGeometry args={[1, 20, 14]} />
        <meshBasicMaterial color={look.tint} />
      </mesh>
    </Stand>
  )
}

function Crew({ world, plain }: { world: World; plain: boolean }) {
  useSyncExternalStore(world.watch, () => world.stamp)
  return (
    <>
      {[...world.nodes.values()].map(node => {
        const { body, look } = node
        if (!body || !look) return null
        const key = node.bot.slug
        return plain ? <Plain key={key} world={world} node={node} look={look} drive={body} /> : <Ghost key={key} world={world} node={node} look={look} drive={body} />
      })}
    </>
  )
}

/** Every Bot whose art is ready, in the land's own space. A model that cannot be loaded leaves plain bodies, and the page stays up. */
export function Bots({ world }: { world: World }) {
  return (
    <ModelGuard fallback={<Crew world={world} plain />}>
      <Suspense fallback={null}><Crew world={world} plain={false} /></Suspense>
    </ModelGuard>
  )
}
