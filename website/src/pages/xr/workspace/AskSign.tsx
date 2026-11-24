/**
 * What stands over an ask's panel (`useAskPanels.ts`), drawn by the XR page from its own model. It is no page:
 *
 * - until the page's first picture is in, the whole panel shows the Bot's face, its name and how many asks wait,
 *   so that a panel that came up by itself says at once whose it is;
 * - once nothing is asked any more, a plate over the page's middle says so, and that the page closes, and that a
 *   press keeps it.
 *
 * It takes no pointer: a press there is the panel's own, and the panel's gate decides what becomes of it.
 */
import { useFrame } from '@react-three/fiber'
import { useEffect, useMemo, useRef } from 'react'
import { CanvasTexture, LinearFilter, SRGBColorSpace, Vector3, type Group } from 'three'
import { i18nT } from '../../../i18n/t'
import { faceArtFor } from '../faces'
import { XR_GL, css } from '../palette'
import { loadImage } from '../sceneParts'
import { M_PER_PX } from './layout'
import type { PanelProbe } from './Panel'
import type { AskSignNow } from './useAskPanels'

/** The plate that says that nothing is asked any more: its size on the page, in CSS pixels. */
const DONE = { width: 340, height: 132 } as const
/** Canvas pixels for each CSS pixel of the page: letters stay sharp with the eye near. */
const DETAIL = 2

/** Break words into lines no wider than `width`. */
function lines(c: CanvasRenderingContext2D, words: string, width: number): string[] {
  const out: string[] = []
  let line = ''
  for (const word of words.split(/\s+/)) {
    const next = line ? `${line} ${word}` : word
    if (line && c.measureText(next).width > width) { out.push(line); line = word } else line = next
  }
  if (line) out.push(line)
  return out
}

export function AskSign({ sign, probe }: { sign: AskSignNow; probe: (id: string) => PanelProbe | undefined }) {
  const group = useRef<Group>(null)
  const first = probe(sign.id)
  const cssW = sign.what === 'cover' ? first?.css[0] ?? 390 : DONE.width
  const cssH = sign.what === 'cover' ? first?.css[1] ?? 600 : DONE.height
  const plate = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = cssW * DETAIL
    canvas.height = cssH * DETAIL
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [cssW, cssH])
  useEffect(() => () => plate.texture.dispose(), [plate])

  // The words, and the face when its picture has loaded.
  useEffect(() => {
    let alive = true
    const c = plate.canvas.getContext('2d')
    if (!c) return
    const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
    const paint = (face: HTMLImageElement | null) => {
      const w = plate.canvas.width, h = plate.canvas.height
      c.setTransform(1, 0, 0, 1, 0, 0)
      c.clearRect(0, 0, w, h)
      c.fillStyle = css(sign.what === 'cover' ? XR_GL.glass : XR_GL.raised)
      c.beginPath()
      c.roundRect(0, 0, w, h, sign.what === 'cover' ? 0 : 24)
      c.fill()
      if (sign.what === 'done') { c.strokeStyle = css(XR_GL.edge); c.lineWidth = 3; c.stroke() }
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      c.fillStyle = css(XR_GL.ink)
      if (sign.what === 'done') {
        c.font = `500 ${15 * DETAIL}px ${family}`
        const rows = lines(c, i18nT(sign.restarted ? 'xr.fail_restarted' : sign.fail ? 'xr.fail_done' : 'xr.ask_done'), w - 32 * DETAIL)
        rows.forEach((row, i) => c.fillText(row, w / 2, h / 2 + (i - (rows.length - 1) / 2) * 22 * DETAIL))
      } else {
        const size = 96 * DETAIL
        const top = h * 0.3 - size / 2
        if (face) { c.save(); c.beginPath(); c.arc(w / 2, top + size / 2, size / 2, 0, Math.PI * 2); c.clip(); c.drawImage(face, w / 2 - size / 2, top, size, size); c.restore() }
        c.font = `600 ${22 * DETAIL}px ${family}`
        c.fillText(sign.label, w / 2, top + size + 30 * DETAIL, w - 24 * DETAIL)
        c.fillStyle = css(XR_GL.dim)
        c.font = `500 ${15 * DETAIL}px ${family}`
        const rows = lines(c, sign.fail ? i18nT('xr.fail_line', { name: sign.label }) : i18nT('xr.ask_line', { name: sign.label, count: sign.count }), w - 48 * DETAIL)
        rows.forEach((row, i) => c.fillText(row, w / 2, top + size + (66 + i * 22) * DETAIL))
      }
      plate.texture.needsUpdate = true
    }
    paint(null)
    if (sign.what === 'cover') void loadImage(faceArtFor(sign.avatar, sign.name).flat).then(face => { if (alive) paint(face) }).catch(() => undefined)
    return () => { alive = false }
  }, [plate, sign.what, sign.label, sign.count, sign.avatar, sign.name, sign.fail, sign.restarted])

  const at = useMemo(() => ({ position: new Vector3(), right: new Vector3(), up: new Vector3(), front: new Vector3() }), [])
  // It stands where its panel stands, a hair in front of the page, and moves with it.
  useFrame(() => {
    const g = group.current
    const now = probe(sign.id)
    if (!g) return
    // Its panel came up in this very step: it shows with the panel, a frame later.
    g.visible = !!now
    if (!now) return
    at.right.set(...now.right)
    at.up.set(...now.up)
    at.front.crossVectors(at.right, at.up)
    g.position.set(...now.aim.page).addScaledVector(at.front, 0.004)
    g.matrix.makeBasis(at.right, at.up, at.front).setPosition(g.position)
    g.matrixAutoUpdate = false
    g.matrixWorldNeedsUpdate = true
  })

  return (
    <group ref={group} pointerEvents="none" visible={false} userData={{ xrPress: 'ask' }}>
      <mesh scale={[cssW * M_PER_PX, cssH * M_PER_PX, 1]} renderOrder={4}>
        <planeGeometry />
        <meshBasicMaterial map={plate.texture} transparent={sign.what === 'done'} toneMapped={false} />
      </mesh>
    </group>
  )
}
