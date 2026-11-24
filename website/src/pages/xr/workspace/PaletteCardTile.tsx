/**
 * The palette's wide buttons (`Palette.tsx`), each one wide tile over the tiles of pages, in the palette's own
 * frame (dp at the eye). "New chat": a pinch that begins and ends on it brings the new chat card out
 * (`chatCard.ts`). "Two in view", over it when two pages are open: the same pinch stands two pages side by side
 * in the lens, and again puts them back (`twoUp.ts`).
 */
import type { ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, SRGBColorSpace } from 'three'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css } from '../palette'
import { PX_PER_DP } from './palettePicture'
import { PALM_MENU } from './palm'
import { pinchRoute } from './pinchRoute'

/** The button's size in dp: as wide as two tiles of pages and the air between them, and as tall as a label needs. */
export const NEW_CHAT_DP: readonly [number, number] = [PALM_MENU.tileDp[0] * 2 + PALM_MENU.gapDp, 52]

/** Where the button's middle is over a palette that is `heightDp` tall, in the palette's frame. */
export const newChatY = (heightDp: number): number => heightDp / 2 + PALM_MENU.gapDp + NEW_CHAT_DP[1] / 2
/** Where the "two in view" button's middle is: one row over the "new chat" button. */
export const twoTileY = (heightDp: number): number => newChatY(heightDp) + NEW_CHAT_DP[1] + PALM_MENU.gapDp

interface Props {
  heightDp: number
  /** Whether a pointer is the palette's own hand's: that hand does not press its own palette. */
  own: (e: ThreeEvent<PointerEvent>) => boolean
  onPress: () => void
  /** The "two in view" button: its words, in place of "new chat"'s, one row higher. `on`: the pair stands. */
  two?: { on: boolean }
}

/** How long the pointer may be off the tile while a press is down on it before the press is dropped (ms, D86 5). */
const OFF_MS = 150

export function PaletteCardTile({ heightDp, own, onPress, two }: Props) {
  const [lit, setLit] = useState(false)
  const pressed = useRef<number | null>(null)
  const tileName = two ? 'palette-two-in-view' : 'palette-new-chat'
  /** When the pointer last left the tile while a press was down on it; null while it is on the tile. */
  const leftAt = useRef<number | null>(null)
  /** How many pinches had gone down at this tile's own down (`pinchRoute.downs`). */
  const pressedPinch = useRef(-1)
  const offTooLong = () => leftAt.current !== null && performance.now() - leftAt.current > OFF_MS
  const [w, h] = NEW_CHAT_DP
  const label = two ? i18nT(two.on ? 'xr.two_in_view_back' : 'xr.two_in_view') : `+ ${i18nT('pages.chat.sessionFlyout.new_chat')}`
  const picture = useMemo(() => {
    const canvas = document.createElement('canvas')
    const k = PX_PER_DP
    canvas.width = w * k
    canvas.height = h * k
    const c = canvas.getContext('2d')
    if (c) {
      const line = 1.5 * k
      c.fillStyle = css(XR_GL.dim)
      c.beginPath()
      c.roundRect(0, 0, w * k, h * k, (h * k) / 2)
      c.fill()
      c.fillStyle = css(XR_GL.raised)
      c.beginPath()
      c.roundRect(line, line, w * k - 2 * line, h * k - 2 * line, (h * k) / 2 - line)
      c.fill()
      c.fillStyle = css(XR_GL.ink)
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      c.font = `600 ${(PALM_MENU.labelDp + 2) * k}px ${getComputedStyle(document.body).fontFamily || 'sans-serif'}`
      c.fillText(label, (w * k) / 2, (h * k) / 2 + k, w * k - 24 * k)
    }
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    return texture
  }, [w, h, label])
  useEffect(() => () => picture.dispose(), [picture])
  return (
    <group position={[0, two ? twoTileY(heightDp) : newChatY(heightDp), 0]} userData={{ xrPress: 'tile' }}>
      <mesh scale={[w, h, 1]} pointerEvents="none">
        <planeGeometry />
        <meshBasicMaterial map={picture} transparent toneMapped={false} />
      </mesh>
      <mesh
        position={[0, 0, 0]}
        renderOrder={1}
        scale={[w, h, 1]}
        name={tileName}
        userData={{ xrPress: true }}
        onPointerEnter={(e: ThreeEvent<PointerEvent>) => {
          if (!own(e)) setLit(true)
          if (offTooLong()) pressed.current = null
          leftAt.current = null
        }}
        // A palm that trembles the tile off the held ray for a frame keeps the press (D86 5): it is dropped only after
        // `OFF_MS` off the tile, or when the release is off it (the up then never comes here).
        onPointerLeave={() => { setLit(false); leftAt.current = performance.now() }}
        onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); leftAt.current = null; if (!own(e)) { pressed.current = e.pointerId; pressedPinch.current = pinchRoute.downs } }}
        onPointerUp={(e: ThreeEvent<PointerEvent>) => {
          e.stopPropagation()
          const began = offTooLong() ? null : pressed.current
          pressed.current = null
          // A pinch that went down on something else and ends here presses nothing, whatever a press before left in
          // `pressed` (its up came elsewhere, G5): a pinch has gone down since this tile's own down (J1).
          if (pinchRoute.downs !== pressedPinch.current) return
          // A pinch whose hand travelled is a drag: it presses nothing (D84 2).
          if (pinchRoute.ending === 'drag') return
          if (began === e.pointerId) onPress()
        }}
      >
        <planeGeometry />
        <meshBasicMaterial color={XR_GL.white} visible={lit} transparent opacity={0.16} depthWrite={false} polygonOffset polygonOffsetFactor={-1} polygonOffsetUnits={-4} toneMapped={false} />
      </mesh>
    </group>
  )
}
