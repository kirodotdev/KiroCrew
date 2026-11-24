/**
 * The line of what changed since he left (D36; `since.ts` the rules, `useSince.ts` the state), drawn: one plate
 * above Home's strip, as wide as the strip, with when his last visit was and how many Bots finished, need him and
 * failed since. Its numbers are the strip's own (a number in the colour of what it counts, then a dim label), so it
 * reads at the strip's size. A press on it clears it: he has read it.
 *
 * It is a thing of its own beside Home (`Home.tsx`): it reads where Home stands (`homeLayout`) and changes nothing
 * of it. `HomeSinceMirror` says the same to a screen reader and to Tab.
 */
import { useThree } from '@react-three/fiber'
import { useEffect, useMemo, useState } from 'react'
import { CanvasTexture, LinearFilter, SRGBColorSpace, Vector3 } from 'three'
import { Btn } from '../../../components/ui'
import { fmtDateFields, fmtNumber, fmtTime } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css, readPalette } from '../palette'
import { usePress } from './Desk'
import { HOME, homeDoneRows, homeLayout } from './home'
import { homePlace } from './homePlace'
import { PAGES, facing, placeAt } from './layout'
import { SINCE_LINE, otherDay, sinceLinePitch, type SinceGroup } from './since'
import type { HomeState } from './useHome'
import type { SinceState } from './useSince'

/** Where the head is that Home stands round (`Home.tsx`). */
const HEAD = new Vector3(0, PAGES.eyeM, 0)
/** The groups in the order they are said: what is good news first, as the line of the roadmap has it. */
const SAID: readonly SinceGroup[] = ['finished', 'needs', 'failed']
const LABEL: Record<SinceGroup, string> = { finished: 'xr.since_finished', needs: 'xr.home_waiting', failed: 'xr.home_failed' }

/** When the visit was, as the line says it: the time, with the day when it was another day. */
export const sinceWhen = (atMs: number, nowMs: number): string =>
  otherDay(atMs, nowMs) ? fmtDateFields(atMs, { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : fmtTime(atMs)

/** The line as words, for the mirror and for a test. */
export function sinceWords(line: NonNullable<SinceState['line']>, nowMs: number): string {
  const parts = SAID.filter(g => line.counts[g] > 0).map(g => `${fmtNumber(line.counts[g])} ${i18nT(LABEL[g])}`)
  return `${i18nT('xr.since_at', { time: sinceWhen(line.at, nowMs) })} ${parts.join(' · ')}`
}

/** What a test reads of the line (`?probe=1`). */
export interface HomeSinceProbe { words: string; center: [number, number, number]; size: [number, number]; pitchDeg: number }

interface Props {
  /** Home as it is shown: its form and its lines say where it stands. */
  home: Pick<HomeState, 'form' | 'lines' | 'more' | 'done' | 'toRead'>
  /** Home's strip stands above the first page (`Home.tsx`, `above`). */
  above: boolean
  line: SinceState['line']
  hushed: boolean
  now: () => number
  onClear: () => void
  report?: (read: (() => HomeSinceProbe | null) | null) => void
}

export function HomeSince({ home, above, line, hushed, now, onClear, report }: Props) {
  const host = useThree(s => s.gl.domElement)
  const [lit, setLit] = useState(false)
  const press = usePress(onClear)
  // Home's own rows, its filed lines too: laid out without them the line stood inside large Home (REVIEW-15 S1).
  const { head: doneHead, lines: doneLines } = homeDoneRows(home)
  const layout = useMemo(() => homeLayout(home.form, home.lines.length, home.more > 0, above, { head: doneHead, lines: doneLines }), [home.form, home.lines.length, home.more, above, doneHead, doneLines])
  const widthM = Math.min(layout.widthM, HOME.compact.widthM)
  const pitchDeg = sinceLinePitch(layout.pitchDeg, layout.heightM, layout.distanceM)
  const at = useMemo(() => {
    const position = placeAt(HEAD, 0, pitchDeg, layout.distanceM)
    return { position, quaternion: facing(position, HEAD) }
  }, [pitchDeg, layout.distanceM])
  const plate = useMemo(() => {
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(widthM * HOME.pxPerM)
    canvas.height = Math.round(SINCE_LINE.heightM * HOME.pxPerM)
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [widthM])
  useEffect(() => () => plate.texture.dispose(), [plate])
  const when = line ? sinceWhen(line.at, now()) : ''
  useEffect(() => {
    const c = plate.canvas.getContext('2d')
    if (!c || !line) return
    const pal = readPalette(host.parentElement ?? document.body)
    const colour: Record<SinceGroup, string> = { finished: pal.ok, needs: pal.warn, failed: pal.danger }
    const k = HOME.pxPerM
    const w = plate.canvas.width
    const h = plate.canvas.height
    const font = (weight: number, m: number) => `${weight} ${Math.round(m * k)}px ${pal.sans}`
    c.setTransform(1, 0, 0, 1, 0, 0)
    c.clearRect(0, 0, w, h)
    c.globalAlpha = 0.94
    c.fillStyle = css(XR_GL.glass)
    c.beginPath()
    c.roundRect(2, 2, w - 4, h - 4, 0.02 * k)
    c.fill()
    c.globalAlpha = 1
    c.strokeStyle = css(XR_GL.edge)
    c.lineWidth = 3
    c.stroke()
    c.textBaseline = 'middle'
    c.textAlign = 'left'
    let left = 0.035 * k
    c.fillStyle = pal.ink
    c.font = font(600, SINCE_LINE.labelM)
    const head = i18nT('xr.since_at', { time: when })
    c.fillText(head, left, h / 2 + 0.003 * k, 0.42 * k)
    left += Math.min(0.42 * k, c.measureText(head).width) + 0.05 * k
    for (const group of SAID) {
      if (line.counts[group] <= 0) continue
      c.fillStyle = colour[group]
      c.font = font(700, SINCE_LINE.numberM)
      const n = fmtNumber(line.counts[group])
      c.fillText(n, left, h / 2 + 0.002 * k)
      left += c.measureText(n).width + 0.014 * k
      c.fillStyle = pal.dim
      c.font = font(500, SINCE_LINE.labelM)
      const label = i18nT(LABEL[group])
      c.fillText(label, left, h / 2 + 0.004 * k, 0.18 * k)
      left += Math.min(0.18 * k, c.measureText(label).width) + 0.045 * k
    }
    plate.texture.needsUpdate = true
  }, [plate, host, line, when])
  // What must keep clear of Home's band reads where the line stands too (`homePlace.ts`): the palette's room cards.
  useEffect(() => {
    homePlace.since = line ? { center: at.position, widthM, heightM: SINCE_LINE.heightM } : null
    return () => { homePlace.since = null }
  }, [line, at, widthM])
  useEffect(() => {
    if (!report) return
    report(() => (line ? { words: sinceWords(line, now()), center: [at.position.x, at.position.y, at.position.z], size: [widthM, SINCE_LINE.heightM], pitchDeg } : null))
    return () => report(null)
  })
  if (!line) return null
  return (
    // `xrPress`: the readout's plate reads this mark, so that it never stands over a thing that can be pressed.
    <group position={at.position} quaternion={at.quaternion} userData={{ xrPress: 'home-since' }} pointerEvents={hushed ? 'none' : undefined}>
      <mesh
        name="xr-home-since"
        scale={[widthM, SINCE_LINE.heightM, 1]}
        renderOrder={3}
        onPointerEnter={() => setLit(true)}
        {...press}
        onPointerLeave={() => { setLit(false); press.onPointerLeave() }}
      >
        <planeGeometry />
        <meshBasicMaterial map={plate.texture} transparent depthWrite={false} toneMapped={false} />
      </mesh>
      <mesh position-z={0.002} scale={[widthM, SINCE_LINE.heightM, 1]} raycast={() => null} renderOrder={4}>
        <planeGeometry />
        <meshBasicMaterial color={XR_GL.white} transparent opacity={lit ? 0.12 : 0} depthWrite={false} toneMapped={false} />
      </mesh>
    </group>
  )
}

/** The same line for a screen reader and for Tab: a press on it clears it. */
export function HomeSinceMirror({ line, now, onClear }: { line: SinceState['line']; now: () => number; onClear: () => void }) {
  if (!line) return null
  return (
    <section className="sr-only" aria-label={i18nT('xr.since_label')} data-testid="xr-home-since">
      <Btn onClick={onClear}>{sinceWords(line, now())}</Btn>
    </section>
  )
}
