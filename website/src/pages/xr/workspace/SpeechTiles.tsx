/**
 * The tiles of the speech probe (`?fps=1&speech=1`, `speechProbe.ts`): one tile for each language to listen in,
 * low on the left of where the person looked when the room opened or the session began, nearer than any page. A
 * press on a tile starts the browser's speech recognition in that language; a press while it runs stops it. The
 * tile that listens says that the microphone is on. They are a probe's switch and nothing else.
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, LinearFilter, SRGBColorSpace, Vector3, type Group } from 'three'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css } from '../palette'
import { pinchRoute } from './pinchRoute'
import { TURN_DRAG_PX } from './previewLook'
import { sessionNow, speechProbe, type SpeechProbe } from './speechProbe'

/** The languages to listen in, as the tags the browser's recognition takes. */
export const SPEECH_LANGS = ['en-US', 'zh-CN'] as const

/** A tile's size in metres and its picture's in pixels; how far from the eye the tiles stand, and where: to the left and down, in degrees. */
const TILE = { w: 0.2, h: 0.05, gap: 0.012, px: [640, 160] as const, awayM: 0.9, leftDeg: 34, downDeg: 24 }

function useLabel(text: string, lit: boolean, on: boolean) {
  const picture = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = TILE.px[0]
    canvas.height = TILE.px[1]
    const c = canvas.getContext('2d')
    if (c) {
      c.fillStyle = css(on ? XR_GL.ink : lit ? XR_GL.edge : XR_GL.raised)
      c.fillRect(0, 0, canvas.width, canvas.height)
      c.strokeStyle = css(XR_GL.dim)
      c.lineWidth = 4
      c.strokeRect(2, 2, canvas.width - 4, canvas.height - 4)
      c.fillStyle = css(on ? XR_GL.glass : XR_GL.ink)
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
  }, [text, lit, on])
  useEffect(() => () => picture.dispose(), [picture])
  return picture
}

function Tile({ probe, lang, y, inSession }: { probe: SpeechProbe; lang: string; y: number; inSession: () => boolean }) {
  const [lit, setLit] = useState(false)
  const down = useRef<{ pointerId: number; at: [number, number] } | null>(null)
  const mine = probe.busy && probe.state.lang === lang
  const label = useLabel(i18nT(mine ? 'xr.speech_listening' : 'xr.speech_listen', { lang }), lit, mine && probe.state.phase === 'on')
  return (
    <mesh
      position-y={y}
      name={`xr-speech-${lang}`}
      userData={{ xrPress: 'speech' }}
      onPointerEnter={() => setLit(true)}
      onPointerLeave={() => { setLit(false); down.current = null }}
      onPointerCancel={() => { down.current = null }}
      onPointerDown={(e: ThreeEvent<PointerEvent>) => { e.stopPropagation(); down.current = { pointerId: e.pointerId, at: [e.nativeEvent.clientX, e.nativeEvent.clientY] } }}
      onPointerUp={(e: ThreeEvent<PointerEvent>) => {
        e.stopPropagation()
        const began = down.current
        down.current = null
        if (!began || began.pointerId !== e.pointerId) return
        // A pinch that was held long or that travelled is no press, as on every other thing of the room; nor is a
        // mouse or a finger that moved across the tile in the flat preview.
        if (pinchRoute.ending === 'drag') return
        if (Math.hypot(e.nativeEvent.clientX - began.at[0], e.nativeEvent.clientY - began.at[1]) > TURN_DRAG_PX) return
        probe.listen(lang, inSession())
      }}
    >
      <planeGeometry args={[TILE.w, TILE.h]} />
      <meshBasicMaterial map={label} toneMapped={false} />
    </mesh>
  )
}

export function SpeechTiles({ probe = speechProbe }: { probe?: SpeechProbe }) {
  const gl = useThree(s => s.gl)
  const camera = useThree(s => s.camera)
  const group = useRef<Group>(null)
  const [, redraw] = useState(0)
  const placed = useRef<boolean | null>(null)
  const eye = useMemo(() => ({ at: new Vector3(), ahead: new Vector3() }), [])
  useEffect(() => {
    probe.changed = () => redraw(v => v + 1)
    return () => { probe.changed = null; probe.dispose() }
  }, [probe])
  const inSession = () => gl.xr.isPresenting
  useFrame(() => {
    const presenting = gl.xr.isPresenting
    const session = gl.xr.getSession() as { visibilityState?: string } | null
    // How the session fares while the microphone is asked for or on: that is what the probe is for.
    if (probe.busy) probe.sessionIs(sessionNow(session ? session.visibilityState : null))
    const g = group.current
    // Placed once when the room opens and once more when a session starts or ends: after that they stand still.
    if (!g || placed.current === presenting) return
    placed.current = presenting
    camera.getWorldPosition(eye.at)
    camera.getWorldDirection(eye.ahead)
    const yaw = Math.atan2(eye.ahead.x, -eye.ahead.z) - (TILE.leftDeg * Math.PI) / 180
    const down = (TILE.downDeg * Math.PI) / 180
    g.position.set(eye.at.x + Math.sin(yaw) * Math.cos(down) * TILE.awayM, eye.at.y - Math.sin(down) * TILE.awayM, eye.at.z - Math.cos(yaw) * Math.cos(down) * TILE.awayM)
    g.lookAt(eye.at)
  })
  if (probe.state.phase === 'none') return null
  return (
    <group ref={group}>
      {SPEECH_LANGS.map((lang, i) => <Tile key={lang} probe={probe} lang={lang} y={-i * (TILE.h + TILE.gap)} inSession={inSession} />)}
    </group>
  )
}
