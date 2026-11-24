/**
 * A line that a Bot's own plate says for a little while (D82 3, D83 5): why no page came, or that its sheet is in his
 * hand. He must read it, so unlike a name's sign it is never cut by a page (D88 1): it is drawn in its own band of
 * `drawOrder.ts` (M51), after the pages and boards with no depth test and before Home, and a long line breaks into
 * two rows instead of growing wider than two names. It takes no press: what stands under it takes the pinch.
 */
import { useEffect, useMemo } from 'react'
import { CanvasTexture, LinearFilter, SRGBColorSpace } from 'three'
import { XR_GL } from '../palette'
import { DEPTH, DRAW } from './drawOrder'

/** The picture: a row's height, its letters, its air, its widest row (two names wide). */
const NOTE = { row: 80, font: 40, least: 26, pad: 16, rowPx: 768, namePx: 384, } as const
const css = (hex: number) => `#${hex.toString(16).padStart(6, '0')}`

/** The line as one row, or as two broken at the space nearest its middle when one row is wider than `fits`. */
export function noteRows(text: string, wide: (s: string) => number, fits: number): string[] {
  if (wide(text) <= fits) return [text]
  const spaces = [...text].flatMap((ch, i) => (ch === ' ' ? [i] : []))
  if (!spaces.length) return [text]
  const mid = text.length / 2
  const at = spaces.reduce((best, i) => (Math.abs(i - mid) < Math.abs(best - mid) ? i : best), spaces[0])
  return [text.slice(0, at), text.slice(at + 1)]
}

interface Props {
  text: string
  yaw: number
  /** How wide a name's widest sign is in the room (the body's width), and the sign's own scale. */
  nameM: number
  scale?: number
  /** The top of the head, and how far over it the line's lower edge region stands. */
  topM: number
  overM: number
  /** In front of the body by this much (a sign that stands on the body's face); 0 over the head. */
  outM?: number
}

export function BotNote({ text, yaw, nameM, scale = 1, topM, overM, outM = 0 }: Props) {
  const plate = useMemo(() => {
    const canvas = document.createElement('canvas')
    const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
    const c = canvas.getContext('2d')
    const fits = NOTE.rowPx - 2 * NOTE.pad
    let size: number = NOTE.font
    let rows = [text]
    let widths = [fits]
    if (c) {
      for (;;) {
        c.font = `600 ${size}px ${family}`
        rows = noteRows(text, s => c.measureText(s).width, fits)
        widths = rows.map(r => c.measureText(r).width)
        if (Math.max(...widths) <= fits || size <= NOTE.least) break
        size -= 2
      }
    }
    const width = Math.min(NOTE.rowPx, Math.ceil(Math.max(...widths)) + 2 * NOTE.pad)
    const height = NOTE.row + (rows.length - 1) * Math.round(size * 1.3)
    canvas.width = width
    canvas.height = height
    const rowY = (i: number) => height / 2 + (i - (rows.length - 1) / 2) * size * 1.3 + 2
    if (c) {
      c.fillStyle = css(XR_GL.room)
      c.globalAlpha = 0.94
      c.beginPath()
      c.roundRect(2, 2, width - 4, height - 4, 20)
      c.fill()
      c.globalAlpha = 1
      c.fillStyle = css(XR_GL.ink)
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      c.font = `600 ${size}px ${family}`
      rows.forEach((r, i) => c.fillText(r, width / 2, rowY(i), width - 12))
    }
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    // For a test: the middle of the last row's last letter, in the plate's own frame (-0.5 to 0.5 each way).
    const lastW = Math.min(widths[widths.length - 1], width - 12)
    const last: [number, number] = [(width / 2 + lastW / 2 - size * 0.3) / width - 0.5, 0.5 - rowY(rows.length - 1) / height]
    return { texture, widthM: (nameM * width) / NOTE.namePx, heightM: (nameM * height) / NOTE.namePx, rows: rows.length, last }
  }, [text, nameM])
  useEffect(() => () => plate.texture.dispose(), [plate])
  return (
    <mesh
      name="bot-note"
      position={[Math.sin(yaw) * outM, topM + overM + ((plate.heightM - (nameM * NOTE.row) / NOTE.namePx) * scale) / 2, Math.cos(yaw) * outM]}
      rotation-y={yaw}
      scale={[plate.widthM * scale, plate.heightM * scale, 1]}
      renderOrder={DRAW.botnote}
      pointerEvents="none"
      userData={{ xrPress: 'sign', note: { rows: plate.rows, last: plate.last } }}
    >
      <planeGeometry />
      <meshBasicMaterial map={plate.texture} transparent depthTest={DEPTH.botnote} depthWrite={false} toneMapped={false} />
    </mesh>
  )
}
