/**
 * The line that asks a room its name (D93 2, 3): it stands in the lens where he faced, with the keys under it and
 * the microphone listening (`useComment`, as a note on a board is written). It never stops him: every other press
 * goes where it went. His letters stand over the keys (`DeskKeys`, D97 1); this line is their own place when he looks
 * up, and it is drawn only while Home is large, under it, where it covers no page and no Bot (D97 2). Why a name is
 * refused is said on it (D97 3). It takes no press. It is drawn after pages and boards with no depth test, before Home.
 */
import { useEffect, useMemo } from 'react'
import { CanvasTexture, LinearFilter, SRGBColorSpace, type Vector3 } from 'three'
import { i18nT } from '../../../i18n/t'
import { XR_GL, readPalette } from '../palette'
import { DEPTH, DRAW } from './drawOrder'
import { facing, placeAt } from './layout'
import { signsOf } from './rooms'

/**
 * Where it stands from his head (under Home large), how large it is, and its picture. Its place in the draw order is
 * its own layer, between the held board and Home, over Home with no depth test (`drawOrder.ts` `roomname`, M58).
 */
export const ROOM_NAME_LINE = { pitchDeg: -16, distanceM: 0.9, widthM: 0.5, px: 1024, rowPx: 150 } as const
const css = (hex: number) => `#${hex.toString(16).padStart(6, '0')}`

interface Props {
  head: Vector3
  yawDeg: number
  /** The room's own name now, the letters typed so far, and what is heard and not yet written. */
  name: string
  text: string
  interim: string
  /** Why the name he typed was refused; with none, nothing. */
  said?: string | null
}

export function RoomNameLine({ head, yawDeg, name, text, interim, said = null }: Props) {
  const rows = ROOM_NAME_LINE.rowPx
  const plate = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = ROOM_NAME_LINE.px
    canvas.height = rows
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [rows])
  useEffect(() => () => plate.texture.dispose(), [plate])
  useEffect(() => {
    const c = plate.canvas.getContext('2d')
    if (!c) return
    const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
    const w = ROOM_NAME_LINE.px
    c.clearRect(0, 0, w, rows)
    c.fillStyle = css(XR_GL.room)
    c.globalAlpha = 0.95
    c.beginPath(); c.roundRect(4, 4, w - 8, rows - 8, 28); c.fill()
    c.globalAlpha = 1
    c.textBaseline = 'middle'
    c.textAlign = 'left'
    // The label and the room's own name, small; then what he has typed, large, with the place the next letter goes.
    c.font = `500 30px ${family}`
    c.fillStyle = css(XR_GL.faint)
    c.fillText(`${i18nT('xr.room_name_field')} · ${name}`, 36, 38, w - 72)
    c.font = `700 56px ${family}`
    c.fillStyle = css(XR_GL.ink)
    const typed = text + (interim ? ` ${interim}` : '')
    // A long name is cut at its front by whole signs, with "…" there: the letters he types last are the ones he sees, at their own width.
    let shown = typed || ''
    if (c.measureText(shown).width > w - 96) { let signs = signsOf(shown); while (signs.length > 1 && c.measureText(`…${signs.join('')}`).width > w - 96) signs = signs.slice(1); shown = `…${signs.join('')}` }
    const tw = Math.min(w - 96, c.measureText(shown).width)
    c.fillText(shown, 36, 100, w - 96)
    c.fillRect(36 + tw + 6, 72, 4, 56)
    if (!typed) { c.font = `500 34px ${family}`; c.fillStyle = css(XR_GL.faint); c.fillText(i18nT('xr.room_name_say'), 60, 100, w - 120) }
    if (said) { c.font = `600 30px ${family}`; c.fillStyle = readPalette(document.body).warn; c.textAlign = 'right'; c.fillText(said, w - 36, 38, w * 0.55) }
    plate.texture.needsUpdate = true
  }, [plate, rows, name, text, interim, said])
  const at = useMemo(() => placeAt(head, yawDeg, ROOM_NAME_LINE.pitchDeg, ROOM_NAME_LINE.distanceM), [head, yawDeg])
  const turn = useMemo(() => facing(at, head), [at, head])
  const heightM = (ROOM_NAME_LINE.widthM * rows) / ROOM_NAME_LINE.px
  return (
    <group position={at} quaternion={turn} name="room-name-line" userData={{ roomName: { text, widthM: ROOM_NAME_LINE.widthM, heightM } }}>
      <mesh scale={[ROOM_NAME_LINE.widthM, heightM, 1]} renderOrder={DRAW.roomname} pointerEvents="none">
        <planeGeometry />
        <meshBasicMaterial map={plate.texture} transparent depthTest={DEPTH.roomname} depthWrite={false} toneMapped={false} />
      </mesh>
    </group>
  )
}
