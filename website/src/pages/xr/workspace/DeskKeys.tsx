/**
 * The keyboard of the room (`deskKeys.ts`), drawn: one board of keys that lies on the desk or stands in front of
 * the person. A key is pressed as a tile of the desk is: the dot and a pinch, a raised hand's ray, the mouse in the
 * flat preview; on the desk also by a finger's touch (`deskTouch.ts`). A press that travels presses nothing.
 */
import { useFrame, type ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, DoubleSide, LinearFilter, Quaternion, SRGBColorSpace, Vector3, type Group } from 'three'
import { XR_GL, css, readPalette } from '../palette'
import { touchTargets } from './deskTouch'
import { PRESS, layIn } from './drawOrder'
import { KEYS, KEYS_LINE, boardSize, cutToFit, keyRect, keysLine, keysOf, layerAfter, type Key, type KeysLayer } from './deskKeys'
import { facing } from './layout'
import { pinchRoute } from './pinchRoute'
import { TURN_DRAG_PX } from './previewLook'
import type { KeysPlace } from './useChatCard'

type P3 = [number, number, number]
/** What a test reads of the drawn keys: the layer, each key's middle, the line over them and the board's corners, in the room. */
export interface KeysDrawn { layer: string; centers: Record<string, P3>; line?: { shown: string; said: string | null; saidShown: string | null; corners: P3[]; letterM: number } | null; board?: P3[] }

/** Canvas pixels for a metre of the board: a key's sign is sharp at arm's length. */
const PX_PER_M = 2400

interface Props {
  place: KeysPlace
  /** Where the head is: a board that stands faces it. */
  head: Vector3
  /** One character, `Backspace` or `Enter`. */
  onKey: (key: string) => void
  /** The flat preview's quiet time: nothing takes a pointer. */
  hushed: boolean
  /**
   * What the keys write, shown just over their far row in letters as large as a key's own (D97 1): he sees his letters
   * where he types. `said`: one line beside them (why a name was refused). With none, nothing stands there.
   */
  line?: { text: string; interim?: string; said?: string | null } | null
  /** Tells a test which layer shows and where each key's middle is in the room, by what the key types. */
  drawn?: { keys: KeysDrawn | null }
  /** A pointer (his dot, a hand's ray), named by its id, came onto the keys (their board or any key: `true`) or left them all. */
  onDot?: (on: boolean, pointerId: number) => void
}

/** Draws the keys on a canvas that has the board's size already: a texture's picture keeps its size once the renderer has taken it. */
function drawBoard(canvas: HTMLCanvasElement, keys: readonly Key[], stepM: number): void {
  const [w, h] = boardSize(stepM)
  const c = canvas.getContext('2d')
  if (!c) return
  c.clearRect(0, 0, canvas.width, canvas.height)
  const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
  c.textAlign = 'center'
  c.textBaseline = 'middle'
  for (const key of keys) {
    const r = keyRect(key, stepM)
    const [kw, kh] = [r.w * PX_PER_M, r.h * PX_PER_M]
    const left = (r.x + w / 2) * PX_PER_M - kw / 2
    const top = (h / 2 - r.y) * PX_PER_M - kh / 2
    c.fillStyle = css(XR_GL.faint)
    c.beginPath()
    c.roundRect(left, top, kw, kh, kh * 0.18)
    c.fill()
    c.fillStyle = css(XR_GL.raised)
    c.beginPath()
    c.roundRect(left + 2, top + 2, kw - 4, kh - 4, kh * 0.16)
    c.fill()
    c.fillStyle = css(XR_GL.ink)
    c.font = `600 ${Math.round(kh * (key.label.length > 1 ? 0.34 : 0.5))}px ${family}`
    c.fillText(key.label, left + kw / 2, top + kh / 2 + 2)
  }
}

/** Draws the line over the keys: his letters with the place of the next one, and what is said beside them. */
function drawLine(canvas: HTMLCanvasElement, line: NonNullable<Props['line']>, keyPx: number): { shown: string; told: string } {
  const c = canvas.getContext('2d')
  if (!c) return { shown: '', told: '' }
  const [w, h] = [canvas.width, canvas.height]
  c.clearRect(0, 0, w, h)
  const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
  c.fillStyle = css(XR_GL.room)
  c.beginPath()
  c.roundRect(0, 0, w, h, h * 0.18)
  c.fill()
  c.textBaseline = 'middle'
  // A key's own letter is half its height: the line's letters are the same.
  const letter = Math.round(keyPx * 0.5)
  c.font = `600 ${letter}px ${family}`
  const pad = Math.round(keyPx * 0.3)
  let said = 0
  let drawnSaid = ''
  if (line.said) {
    c.textAlign = 'right'
    c.fillStyle = readPalette(document.body).warn
    // What is said takes at most `saidShare` of the line, cut by whole signs with "…" at its end, never squeezed
    // (M68): his letters keep the rest.
    const told = cutToFit(line.said, shown => c.measureText(shown).width <= w * KEYS_LINE.saidShare)
    said = c.measureText(told).width
    drawnSaid = told
    c.fillText(told, w - pad, h / 2 + 2)
    said += pad
  }
  c.textAlign = 'left'
  const room = w - 2 * pad - said - 8
  const firm = keysLine(line.text + (line.interim ? `${line.text ? ' ' : ''}${line.interim}` : ''), shown => c.measureText(shown).width <= room)
  c.fillStyle = css(XR_GL.ink)
  c.fillText(firm, pad, h / 2 + 2, room)
  c.fillRect(pad + Math.min(c.measureText(firm).width, room) + 4, h / 2 - letter * 0.6, 3, letter * 1.2)
  return { shown: firm, told: drawnSaid }
}

export function DeskKeys({ place, head, onKey, hushed, line = null, drawn, onDot }: Props) {
  const [layer, setLayer] = useState<KeysLayer>('abc')
  const [lit, setLit] = useState('')
  const keys = useMemo(() => keysOf(layer), [layer])
  // The keys and their line have their own band, under the held card and over the room's name line (M87, M68).
  const root = useRef<Group>(null)
  useFrame(() => { if (root.current) layIn(root.current, 'keys') })
  const [w, h] = boardSize(place.stepM)
  const picture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(w * PX_PER_M)
    canvas.height = Math.round(h * PX_PER_M)
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [w, h])
  useEffect(() => () => picture.texture.dispose(), [picture])
  useEffect(() => { drawBoard(picture.canvas, keys, place.stepM); picture.texture.needsUpdate = true }, [picture, keys, place.stepM])
  // The line over the far row: as wide as the board, as tall as a key.
  const keyM = place.stepM * (1 - KEYS.gapShare)
  const lineOn = !!line
  const linePic = useMemo(() => {
    if (!lineOn) return null
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(w * PX_PER_M)
    canvas.height = Math.round(keyM * PX_PER_M)
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [lineOn, w, keyM])
  useEffect(() => () => linePic?.texture.dispose(), [linePic])
  const [lineText, lineInterim, lineSaid] = [line?.text ?? '', line?.interim ?? '', line?.said ?? null]
  /** What the line drew last, for a test: his letters and what is said, each cut to what fits, as he sees them. */
  const lineShown = useRef({ shown: '', told: '' })
  useEffect(() => {
    if (!linePic) return
    lineShown.current = drawLine(linePic.canvas, { text: lineText, interim: lineInterim, said: lineSaid }, keyM * PX_PER_M)
    linePic.texture.needsUpdate = true
  }, [linePic, lineText, lineInterim, lineSaid, keyM])
  const lineY = h / 2 + place.stepM * KEYS_LINE.gapShare + keyM / 2

  // The board's own frame in the room: flat on the desk it faces up and its far row is away from the person; standing it faces the head.
  const turn = useMemo(() => {
    const at = new Vector3(...place.at)
    if (!place.flat) return facing(at, head)
    const yaw = new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), (-place.yawDeg * Math.PI) / 180)
    return yaw.multiply(new Quaternion().setFromAxisAngle(new Vector3(1, 0, 0), -Math.PI / 2))
    // The head at the time the board came: a board that stands does not swing with every move of the head.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [place])
  const centerOf = (key: Key): [number, number, number] => {
    const r = keyRect(key, place.stepM)
    const p = new Vector3(r.x, r.y, 0).applyQuaternion(turn).add(new Vector3(...place.at))
    return [p.x, p.y, p.z]
  }

  const press = (key: Key) => {
    if (key.key === 'Shift' || key.key === 'Layer') { setLayer(now => layerAfter(now, key.key)); return }
    setLayer(now => layerAfter(now, key.key))
    onKey(key.key)
  }
  const pressNow = useRef(press)
  pressNow.current = press

  // On the desk each key is a target of a finger's touch; the space bar is one target for each key's width of it.
  useEffect(() => {
    if (!place.flat || hushed) return
    const ids: string[] = []
    for (const key of keys) {
      const r = keyRect(key, place.stepM)
      for (let i = 0; i < key.span; i += 1) {
        const x = r.x - r.w / 2 + ((i + 0.5) * r.w) / key.span
        const p = new Vector3(x, r.y, 0).applyQuaternion(turn).add(new Vector3(...place.at))
        const id = `key:${key.id}:${i}`
        touchTargets.set(id, { center: [p.x, p.y, p.z], radiusM: place.stepM * 0.6, press: () => pressNow.current(key), box: { halfM: place.stepM / 2, heightM: KEYS.touchM, yawDeg: place.yawDeg } })
        ids.push(id)
      }
    }
    return () => { for (const id of ids) touchTargets.delete(id) }
  }, [keys, place, hushed, turn])

  useEffect(() => {
    if (!drawn) return
    const corner = (x: number, y: number): [number, number, number] => { const p = new Vector3(x, y, 0).applyQuaternion(turn).add(new Vector3(...place.at)); return [p.x, p.y, p.z] }
    drawn.keys = {
      layer, centers: Object.fromEntries(keys.map(key => [key.key, centerOf(key)])),
      line: line ? { shown: lineShown.current.shown, said: lineSaid, saidShown: lineShown.current.told || null, corners: [corner(-w / 2, lineY + keyM / 2), corner(w / 2, lineY + keyM / 2), corner(w / 2, lineY - keyM / 2), corner(-w / 2, lineY - keyM / 2)], letterM: keyM * 0.5 } : null,
      board: [corner(-w / 2, h / 2), corner(w / 2, h / 2), corner(w / 2, -h / 2), corner(-w / 2, -h / 2)],
    }
    return () => { drawn.keys = null }
  })

  const held = useRef<{ id: string; pointerId: number; at: [number, number] } | null>(null)
  return (
    // The dot is read on the group: a group is entered while any mesh under it is hit (its board, a key), and left
    // only when none is. A key is a mesh beside the board, so the board alone would say "left" at each key.
    <group ref={root} position={place.at} quaternion={turn} pointerEvents={hushed ? 'none' : undefined} pointerEventsOrder={PRESS.keys} userData={{ xrPress: 'keys' }} onPointerEnter={(e: ThreeEvent<PointerEvent>) => onDot?.(true, e.pointerId)} onPointerLeave={(e: ThreeEvent<PointerEvent>) => onDot?.(false, e.pointerId)}>
      <mesh pointerEvents="none" renderOrder={0}>
        <planeGeometry args={[w, h]} />
        <meshBasicMaterial map={picture.texture} transparent side={DoubleSide} depthWrite={false} toneMapped={false} />
      </mesh>
      {linePic && (
        <mesh position={[0, lineY, 0]} pointerEvents="none" renderOrder={1} name="keys-line">
          <planeGeometry args={[w, keyM]} />
          <meshBasicMaterial map={linePic.texture} transparent side={DoubleSide} depthWrite={false} toneMapped={false} />
        </mesh>
      )}
      {/* The board takes a near miss: a press between two keys or on its margin does nothing, and does not reach the page or the Bot behind it. It is never drawn. */}
      <mesh
        position={[0, 0, -0.0005]}
        userData={{ xrKeysBoard: true, xrPress: true }}
        name="chat-keys-board"
        onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation() }}
        onPointerUp={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation() }}
        onPointerMove={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation() }}
        onWheel={(e: ThreeEvent<WheelEvent>) => { e.stopPropagation() }}
      >
        <planeGeometry args={[w + 2 * KEYS.marginM, h + 2 * KEYS.marginM]} />
        <meshBasicMaterial visible={false} side={DoubleSide} />
      </mesh>
      {keys.map(key => {
        const r = keyRect(key, place.stepM)
        return (
          <mesh
            key={key.id}
            position={[r.x, r.y, KEYS.aimM / 2]}
            scale={[r.w, r.h, KEYS.aimM]}
            userData={{ xrKey: key.key, xrPress: true }}
            renderOrder={2}
            onPointerEnter={() => setLit(key.id)}
            onPointerLeave={() => { setLit(now => (now === key.id ? '' : now)); if (held.current?.id === key.id) held.current = null }}
            onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); held.current = { id: key.id, pointerId: e.pointerId, at: [e.nativeEvent.clientX, e.nativeEvent.clientY] } }}
            onPointerCancel={() => { held.current = null }}
            onPointerUp={(e: ThreeEvent<PointerEvent>) => {
              e.stopPropagation()
              const began = held.current
              held.current = null
              if (!began || began.id !== key.id || began.pointerId !== e.pointerId) return
              // A pinch that travelled or was held long, and a mouse that moved, press nothing.
              if (pinchRoute.ending === 'drag') return
              if (Math.hypot(e.nativeEvent.clientX - began.at[0], e.nativeEvent.clientY - began.at[1]) > TURN_DRAG_PX) return
              press(key)
            }}
          >
            <boxGeometry />
            {/* What a pinch would type lights before the pinch. Unlit, the key is still there for a ray to find. */}
            <meshBasicMaterial color={XR_GL.white} visible={lit === key.id} transparent opacity={0.22} depthWrite={false} toneMapped={false} />
          </mesh>
        )
      })}
    </group>
  )
}
