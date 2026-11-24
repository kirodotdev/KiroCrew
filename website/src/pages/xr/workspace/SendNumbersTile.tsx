/**
 * The tile that hands the readout's numbers back (`?fps=1&sendto=<slug>`, `sendNumbers.ts`): low on the left of where
 * he looked when the room opened or the session began, under the speech probe's tiles. A press sends what the readout
 * says now to that Bot's own thread, as one message; the plate and the tile say whether it went. A press while one
 * is on its way does nothing; a press after it sends the numbers of then, as a new message.
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, LinearFilter, SRGBColorSpace, Vector3, type Group } from 'three'
import { i18nT } from '../../../i18n/t'
import { mintSendId } from '../../../utils/sendDelivery'
import { XR_GL, css } from '../palette'
import { cardSend } from '../writes'
import { pinchRoute } from './pinchRoute'
import { TURN_DRAG_PX } from './previewLook'
import { useReadoutLines, type ReadoutProps } from './Readout'
import { shownPath } from './readoutLines'
import { NUMBERS_MOST_CHARS, numbersBook, numbersText } from './sendNumbers'

/** The tile's size in metres and its picture's in pixels; where it stands from the eye: to the left and down, in degrees; how long it says "sent". */
const TILE = { w: 0.24, h: 0.05, px: [768, 160] as const, awayM: 0.9, leftDeg: 34, downDeg: 33, sentMs: 3000, saveMs: 5000 }

/** Where the log is kept in this tab's store. */
const LOG_KEY = 'xr.numbersLog'

function useLabel(text: string, lit: boolean) {
  const picture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = TILE.px[0]
    canvas.height = TILE.px[1]
    const c = canvas.getContext('2d')
    if (c) {
      c.fillStyle = css(lit ? XR_GL.edge : XR_GL.raised)
      c.fillRect(0, 0, canvas.width, canvas.height)
      c.strokeStyle = css(XR_GL.dim)
      c.lineWidth = 4
      c.strokeRect(2, 2, canvas.width - 4, canvas.height - 4)
      c.fillStyle = css(XR_GL.ink)
      c.font = `48px ${getComputedStyle(document.body).fontFamily || 'sans-serif'}`
      c.textAlign = 'center'
      c.textBaseline = 'middle'
      c.fillText(text, canvas.width / 2, canvas.height / 2, canvas.width - 32)
    }
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return texture
  }, [text, lit])
  useEffect(() => () => picture.dispose(), [picture])
  return picture
}

export function SendNumbersTile({ to, ...readout }: ReadoutProps & { to: string }) {
  const { lines, pace } = useReadoutLines(readout)
  const pagesOf = readout.pages
  const linesNow = useRef(lines)
  linesNow.current = lines
  // The log of what the readout said on the way (`NumbersLog`), kept in this tab's store over a load of the tab.
  useEffect(() => {
    try { numbersBook.log.load(window.sessionStorage.getItem(LOG_KEY)) } catch { /* no store */ }
  }, [])
  const savedAt = useRef(0)
  useEffect(() => {
    if (!lines.length) return
    const now = Date.now()
    // N3: each page in view's last 20 round trips, for the message's held lines only (the plate does not show them).
    const rounds = pagesOf().flatMap(p => { const f = p.source.readout?.(); return f && f.visible && f.roundMs.length ? [`${shownPath(p.source.pagePath?.() ?? p.id)} · round trips ms · ${f.roundMs.slice(-20).map(v => Math.round(v)).join(' ')}`] : [] })
    numbersBook.log.take(lines, pace, now, rounds)
    if (now - savedAt.current < TILE.saveMs) return
    savedAt.current = now
    try { window.sessionStorage.setItem(LOG_KEY, numbersBook.log.save()) } catch { /* no store, or full: the log lives as long as the tab */ }
  }, [lines, pace, pagesOf])
  const gl = useThree(s => s.gl)
  const camera = useThree(s => s.camera)
  const group = useRef<Group>(null)
  const placed = useRef<boolean | null>(null)
  const eye = useMemo(() => ({ at: new Vector3(), ahead: new Vector3() }), [])
  const [lit, setLit] = useState(false)
  const [phase, setPhase] = useState(numbersBook.send.phase)
  const down = useRef<{ pointerId: number; at: [number, number] } | null>(null)
  useFrame(() => {
    const presenting = gl.xr.isPresenting
    const g = group.current
    // Placed once when the room opens and once more when a session starts or ends: after that it stands still.
    if (!g || placed.current === presenting) return
    placed.current = presenting
    camera.getWorldPosition(eye.at)
    camera.getWorldDirection(eye.ahead)
    const yaw = Math.atan2(eye.ahead.x, -eye.ahead.z) - (TILE.leftDeg * Math.PI) / 180
    const dip = (TILE.downDeg * Math.PI) / 180
    g.position.set(eye.at.x + Math.sin(yaw) * Math.cos(dip) * TILE.awayM, eye.at.y - Math.sin(dip) * TILE.awayM, eye.at.z - Math.cos(yaw) * Math.cos(dip) * TILE.awayM)
    g.lookAt(eye.at)
  })
  // "Sent" stands on the tile for a while, then it asks again; the plate keeps the line until the next press.
  useEffect(() => {
    if (phase !== 'sent') return
    const back = setTimeout(() => setPhase('none'), TILE.sentMs)
    return () => clearTimeout(back)
  }, [phase])
  const send = useCallback(() => {
    if (numbersBook.send.phase === 'sending') return
    // What the readout says now, then what it said on the way: one press at the end carries every check (P8).
    const now = linesNow.current
    const sentAt = numbersBook.log.mark()
    const text = numbersText([...now, ...numbersBook.log.lines(NUMBERS_MOST_CHARS - now.reduce((n, l) => n + l.length + 1, 0) - 400)], new Date(), navigator.userAgent)
    numbersBook.send = { phase: 'sending', at: Date.now() }
    setPhase('sending')
    void cardSend(text, { bot: to, thread: '' }, mintSendId()).then(
      () => {
        numbersBook.send = { phase: 'sent', at: Date.now(), chars: text.length }
        // N2 a: what went is not sent again, and what came on its way waits for the next press (R2); a send that
        // failed keeps the log, so the next press carries it all.
        numbersBook.log.clear(sentAt)
        try { window.sessionStorage.setItem(LOG_KEY, numbersBook.log.save()) } catch { /* no store */ }
        setPhase('sent')
      },
      (err: unknown) => { numbersBook.send = { phase: 'failed', at: Date.now(), why: err instanceof Error ? err.message : String(err) }; setPhase('failed') },
    )
  }, [to])
  const label = useLabel(i18nT(phase === 'sending' ? 'xr.numbers_tile_sending' : phase === 'sent' ? 'xr.numbers_tile_sent' : phase === 'failed' ? 'xr.numbers_tile_again' : 'xr.numbers_tile', { name: to }), lit)
  return (
    <group ref={group}>
      <mesh
        name="xr-send-numbers"
        userData={{ xrPress: 'numbers' }}
        onPointerEnter={() => setLit(true)}
        onPointerLeave={() => { setLit(false); down.current = null }}
        onPointerCancel={() => { down.current = null }}
        onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); down.current = { pointerId: e.pointerId, at: [e.nativeEvent.clientX, e.nativeEvent.clientY] } }}
        onPointerUp={(e: ThreeEvent<PointerEvent>) => {
          e.stopPropagation()
          const began = down.current
          down.current = null
          if (!began || began.pointerId !== e.pointerId) return
          // A pinch that was held long or that travelled is no press, as on every other thing of the room.
          if (pinchRoute.ending === 'drag') return
          if (Math.hypot(e.nativeEvent.clientX - began.at[0], e.nativeEvent.clientY - began.at[1]) > TURN_DRAG_PX) return
          send()
        }}
      >
        <planeGeometry args={[TILE.w, TILE.h]} />
        <meshBasicMaterial map={label} toneMapped={false} />
      </mesh>
    </group>
  )
}
