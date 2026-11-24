/**
 * The new chat card in the room (`chatCard.ts`, `useChatCard.ts`): a small dark card with a few large words. It
 * says that the microphone is on while it listens, shows what was said or typed, and names where it goes.
 *
 * A pinch with the dot on it (a raised hand's ray, the mouse) takes it; the head (that ray, the mouse) turns to a
 * Bot or to the open; letting go throws it there. While it is held it rides just over the aim and a mark shows where
 * it would go: a ring on a Bot, a small frame in the open. It flies forward to its target and shrinks into it. The
 * mark at its corner puts it away. With its keyboard out (`DeskKeys.tsx`) it stands where it came out.
 *
 * Two more marks take a pinch while it stands: the microphone's line at its top turns the microphone off and on,
 * and the language's name at its lower right goes to the next language. Everything of it that takes a press
 * carries `userData.xrPress`, which the readout's plate reads so that it never covers a thing that can be pressed.
 */
import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useXR } from '@react-three/xr'
import { useEffect, useMemo, useRef, useState } from 'react'
import { CanvasTexture, DoubleSide, LinearFilter, SRGBColorSpace, Vector3, type Group, type Object3D } from 'three'
import { i18nT } from '../../../i18n/t'
import { XR_GL, css } from '../palette'
import { CARD, aimTarget, cardLayer, cardLines, steadyAim, flyShare, landing, markStep, showsLang, type Card, type CardTarget, type MarkDown, type MarkEvent } from './chatCard'
import { langName } from './chatSpeech'
import { DeskKeys } from './DeskKeys'
import { PRESS, layIn } from './drawOrder'
import { facing } from './layout'
import { pinchRoute } from './pinchRoute'
import { cardTakesDot, type ChatCardState } from './useChatCard'

/** The card's picture: this many pixels wide; its words at this share of its height, so many signs a line, so many lines. */
const PICTURE = { widthPx: 800, heightPx: 500, perLine: 22, lines: 3 } as const
/** The close mark's side, as a share of the card's height. */
const CLOSE_SHARE = 0.2
/**
 * The frame round the card's own place while a let-go would put it back: the open's frame (a square ring turned 45
 * degrees, 1.6 times as wide) scaled to stand a tenth outside the card (0.2 x 0.125 m).
 */
const CARD_BACK_MARK_M = (CARD.widthM / 2) * 1.1 / (Math.SQRT1_2 * 1.6)
const RAD = Math.PI / 180
/** The marks that take a pinch, in the picture's pixels: the microphone's line and the language's name (left, top, width, height). */
const MIC_BOX = [24, 34, 470, 84] as const
const LANG_BOX = [592, 420, 180, 58] as const
/** A box of the picture as a place on the card: its middle and its size, in metres, x to the right and y up. */
const onCard = (box: readonly [number, number, number, number]): { at: [number, number, number]; size: [number, number] } => ({
  at: [((box[0] + box[2] / 2) / PICTURE.widthPx - 0.5) * CARD.widthM, (0.5 - (box[1] + box[3] / 2) / PICTURE.heightPx) * CARD.heightM, 0.002],
  size: [(box[2] / PICTURE.widthPx) * CARD.widthM, (box[3] / PICTURE.heightPx) * CARD.heightM],
})
const MIC_AT = onCard(MIC_BOX)
const LANG_AT = onCard(LANG_BOX)
/** What takes a press for the language: about one and a half times its drawn frame, inside the card, under the words' lowest line, and far from the two marks at the top. */
const LANG_HIT_AT = onCard([522, 412, 270, 84])

interface Props {
  state: ChatCardState
  /** Where the person's head is, kept up to date by the workspace. */
  head: Vector3
  /** The name that a Bot shows. */
  labelOf: (slug: string) => string
  /** The flat preview's quiet time: nothing takes a pointer; a card that is held already is still let go. */
  hushed: boolean
}

/** What the top line says: `warm` when the line itself is the microphone's, `cold` while the microphone is turned off. */
interface Top { text: string; warm: boolean; cold?: boolean }

/** The line at the card's top: where it goes, or what the microphone does. */
function topLine(card: Card, aim: CardTarget | null, labelOf: (slug: string) => string): Top {
  const to = (t: CardTarget) => (t.to === 'bot' ? i18nT('xr.draft_to', { bot: labelOf(t.slug) }) : i18nT('pages.chat.sessionFlyout.new_chat'))
  if (card.phase === 'sent') return { text: i18nT('xr.sent'), warm: false }
  // A frozen card goes to its own target and nowhere else: it names it all the time, held or not.
  if (card.failed && card.target) return { text: to(card.target), warm: false }
  if (card.phase === 'held' && aim) return { text: to(aim), warm: false }
  // Held with the aim on its own place: let go now it goes back, and the line says so (D104 1).
  if (card.phase === 'held') return { text: i18nT('xr.card_back'), warm: false }
  if (card.phase !== 'out' && card.target) return { text: to(card.target), warm: false }
  if (card.paused && card.mic !== 'none') return { text: i18nT('xr.card_mic_off'), warm: false, cold: true }
  if (card.mic === 'on') return { text: i18nT('xr.card_mic_on'), warm: true }
  if (card.mic === 'none') return { text: i18nT('xr.card_no_speech'), warm: false }
  return { text: i18nT('pages.chat.sessionFlyout.new_chat'), warm: false }
}

function drawCard(canvas: HTMLCanvasElement, card: Card, top: Top, held: boolean, warm: string, lang: string): void {
  const { widthPx: w, heightPx: h } = PICTURE
  const c = canvas.getContext('2d')
  if (!c) return
  const family = getComputedStyle(document.body).fontFamily || 'sans-serif'
  c.clearRect(0, 0, w, h)
  c.fillStyle = css(held ? XR_GL.ink : XR_GL.faint)
  c.beginPath()
  c.roundRect(0, 0, w, h, 44)
  c.fill()
  c.fillStyle = css(XR_GL.raised)
  c.beginPath()
  c.roundRect(6, 6, w - 12, h - 12, 40)
  c.fill()
  // The top line: the microphone's dot whenever it listens, whatever the line says, then the words.
  c.textBaseline = 'middle'
  c.textAlign = 'left'
  c.font = `600 44px ${family}`
  let x = 48
  if (card.mic === 'on') {
    c.fillStyle = warm
    c.beginPath()
    c.arc(x + 14, 74, 14, 0, Math.PI * 2)
    c.fill()
    x += 44
  } else if (top.cold) {
    // Off: the dot's ring alone.
    c.strokeStyle = css(XR_GL.dim)
    c.lineWidth = 5
    c.beginPath()
    c.arc(x + 14, 74, 12, 0, Math.PI * 2)
    c.stroke()
    x += 44
  }
  c.fillStyle = css(top.warm ? XR_GL.ink : XR_GL.dim)
  c.fillText(top.text, x, 76, w - x - 48 - h * CLOSE_SHARE)
  // The language's own name, in a small frame at the lower right: a pinch there goes to the next language.
  if (lang) {
    const [bx, by, bw, bh] = LANG_BOX
    c.strokeStyle = css(XR_GL.faint)
    c.lineWidth = 4
    c.beginPath()
    c.roundRect(bx, by, bw, bh, bh / 2)
    c.stroke()
    c.fillStyle = css(XR_GL.dim)
    c.font = `600 34px ${family}`
    c.textAlign = 'center'
    c.fillText(lang, bx + bw / 2, by + bh / 2 + 2, bw - 24)
    c.textAlign = 'left'
  }
  // A frozen card has no language's mark: its lowest line says what is known of its send, and what a throw does.
  if (card.failed) {
    c.fillStyle = css(XR_GL.dim)
    c.font = `500 30px ${family}`
    c.fillText(i18nT('xr.card_not_sure'), 48, LANG_BOX[1] + LANG_BOX[3] / 2 + 2, w - 96)
  }
  // The words, large; the ones still worked on are dim. With none, one dim line says what to do.
  const said = card.text
  const all = card.interim ? `${said}${said ? ' ' : ''}${card.interim}` : said
  c.font = `600 62px ${family}`
  if (!all.trim()) {
    c.fillStyle = css(XR_GL.faint)
    c.font = `500 48px ${family}`
    c.fillText(i18nT('xr.draft_empty'), 48, 250, w - 96)
    return
  }
  const lines = cardLines(all, PICTURE.perLine, PICTURE.lines)
  // How many of the shown signs, from the end, are the ones still worked on.
  let dimLeft = [...card.interim].length
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const y = 190 + i * 96
    const signs = [...lines[i]]
    const dim = Math.min(dimLeft, signs.length)
    dimLeft -= dim
    const firm = signs.slice(0, signs.length - dim).join('')
    c.fillStyle = css(XR_GL.ink)
    c.fillText(firm, 48, y, w - 96)
    if (dim) {
      c.fillStyle = css(XR_GL.dim)
      c.fillText(signs.slice(signs.length - dim).join(''), 48 + Math.min(c.measureText(firm).width, w - 96), y)
    }
  }
}

const EYE = new Vector3()
const DIR = new Vector3()
const TO = new Vector3()
const arr = (v: Vector3): [number, number, number] => [v.x, v.y, v.z]

export function ChatCard({ state, head, labelOf, hushed }: Props) {
  const { card, home, keys, act } = state
  const camera = useThree(s => s.camera)
  const group = useRef<Group>(null)
  const mark = useRef<Group>(null)
  const [lit, setLit] = useState(false)
  const [litMark, setLitMark] = useState('')
  /** Where the card would go if it were let go now, as a word: for the top line and the mark. */
  const [aim, setAim] = useState<CardTarget | null>(null)
  const aimNow = useRef<CardTarget | null>(null)
  /** The raw reading the steady one is made from, and since when it stands (`steadyAim`). */
  const aimRaw = useRef<{ raw: CardTarget | null; since: number }>({ raw: null, since: 0 })
  /** The aim the card's picture shows now: the let-go does what this says (M77). */
  const shownAim = useRef<CardTarget | null>(null)
  /** The pinch that holds the card: its pointer, whether it is the head's dot, and the ray it last sent. */
  const grip = useRef<{ pointerId: number; head: boolean; origin: Vector3; dir: Vector3 } | null>(null)
  /** The press that went down on one of the card's marks (close, microphone, language). */
  const pressed = useRef<MarkDown | null>(null)
  /** Where the card lands in the open, kept from the throw: a card sent by Enter flies there. */
  const land = useRef<[number, number, number] | null>(null)
  const flight = useRef<{ from: Vector3; to: Vector3; t0: number; scale0: number; landed: boolean } | null>(null)
  const warm = useMemo(() => getComputedStyle(document.body).getPropertyValue('--danger').trim() || css(XR_GL.ink), [])

  const picture = useMemo(() => {
    // The canvas has its size before the texture is made: a texture's picture keeps its size once the renderer has taken it.
    const canvas = document.createElement('canvas')
    canvas.width = PICTURE.widthPx
    canvas.height = PICTURE.heightPx
    const texture = new CanvasTexture(canvas)
    texture.colorSpace = SRGBColorSpace
    texture.minFilter = LinearFilter
    texture.generateMipmaps = false
    return { canvas, texture }
  }, [])
  useEffect(() => () => picture.texture.dispose(), [picture])
  const top = card ? topLine(card, aim, labelOf) : null
  // What the probe reads of the top line (D104 1: the rows read what the card says before the let-go).
  useEffect(() => { state.drawn.top = top?.text ?? '' })
  const held = card?.phase === 'held'
  useEffect(() => {
    if (!card || !top) return
    drawCard(picture.canvas, card, top, held || lit, warm, showsLang(card) ? langName(card.lang) : '')
    picture.texture.needsUpdate = true
    // What the picture now says the let-go will do: the let-go reads this, so the act is what he read (M77).
    shownAim.current = card.phase === 'held' ? aim : null
    // `top` is made anew at each render: its fields stand for it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [picture, card, aim, top?.text, top?.warm, top?.cold, held, lit, warm])

  // A session that ends stops the microphone and puts the card away: it stood in the session's room, and in the
  // flat page it could listen on out of sight. A session that the system blurs (its own surface is over the room)
  // turns the microphone off, and only his press turns it on again.
  const session = useXR(s => s.session)
  const closeNow = useRef(state.close)
  closeNow.current = state.close
  useEffect(() => {
    if (!session) return
    const blurred = () => { if (session.visibilityState !== 'visible') act({ type: 'quiet' }) }
    session.addEventListener('visibilitychange', blurred)
    return () => { session.removeEventListener('visibilitychange', blurred); closeNow.current() }
  }, [session, act])

  // A card that is put away, or sent, leaves nothing behind.
  const phase = card?.phase ?? null
  useEffect(() => {
    if (phase !== 'held') { grip.current = null; aimNow.current = null; aimRaw.current = { raw: null, since: 0 }; setAim(null) }
    if (phase !== 'flying' && phase !== 'sending' && phase !== 'sent') flight.current = null
    if (phase === null) { land.current = null; state.drawn.at = null; state.drawn.marks = null }
  }, [phase, state.drawn])

  /** The ray that aims the held card: the head's own while the dot holds it, else the pointer's last. */
  const ray = (): { origin: Vector3; dir: Vector3 } | null => {
    const g = grip.current
    if (!g) return null
    if (g.head) { camera.getWorldPosition(g.origin); camera.getWorldDirection(g.dir) }
    return g
  }
  const letGo = (e: ThreeEvent<PointerEvent>, thrown: boolean) => {
    const g = grip.current
    if (!g || g.pointerId !== e.pointerId || !home) return
    e.stopPropagation()
    const r = ray()
    // The act is what the card says now (`shownAim`, set when its picture was drawn), never a reading of its own.
    const target = thrown ? (card?.failed ? card.target : shownAim.current) : null
    // A frozen card keeps where it landed the first time: it goes there again.
    if (target && r && !card?.failed) land.current = landing(arr(r.origin), arr(r.dir), target, state.botsAt())
    grip.current = null
    if (thrown) state.drawn.letGo = { top: state.drawn.top, target, raw: aimRaw.current.raw, stoodMs: Math.round(performance.now() - aimRaw.current.since) }
    if (thrown) act({ type: 'release', target })
    else act({ type: 'drop' })
  }

  // A mark acts on the up of its own down: a press that began elsewhere and is let go over it does nothing, and
  // one that left the mark is no press of it any more.
  // A press that is cancelled (a long press, a lost hand) is dropped too: no later up acts for it.
  const markDoes = (type: MarkEvent['type'], name: string, pointerId: number): boolean => {
    const next = markStep(pressed.current, { type, name, pointerId })
    pressed.current = next.down
    return next.acts
  }
  const markDown = (e: ThreeEvent<PointerEvent>, name: string) => { e.stopPropagation(); markDoes('down', name, e.pointerId) }
  const markLeft = (e: ThreeEvent<PointerEvent>, name: string) => { markDoes('leave', name, e.pointerId) }
  const markCancel = (e: ThreeEvent<PointerEvent>, name: string) => { e.stopPropagation(); markDoes('cancel', name, e.pointerId) }
  const markUp = (e: ThreeEvent<PointerEvent>, name: string): boolean => {
    e.stopPropagation()
    return markDoes('up', name, e.pointerId) && pinchRoute.ending !== 'drag' && pinchRoute.ending !== 'long'
  }

  useFrame(() => {
    const g = group.current
    if (!g || !card || !home) return
    const now = performance.now()
    // Held, over Home's strip; else the room's (`cardLayer`, D105 3).
    layIn(g, cardLayer(card.phase))
    // The mark of where it would go is the held card's too (S3): over Home's strip while he holds it.
    if (mark.current) layIn(mark.current, cardLayer(card.phase))
    let scale = 1
    if (card.phase === 'held') {
      const r = ray()
      if (r) {
        // A frozen card has its own target, wherever the aim is.
        // One reading for the words, the mark and the act (M77): a target that has stood `STEADY_MS`, else back.
        let target: CardTarget | null
        if (card.failed) target = card.target
        else { const st = steadyAim(aimTarget(arr(r.origin), arr(r.dir), home, state.botsAt()), aimRaw.current, now); aimRaw.current = { raw: st.raw, since: st.since }; target = st.shown }
        if (JSON.stringify(target) !== JSON.stringify(aimNow.current)) { aimNow.current = target; setAim(target) }
        state.drawn.aim = target
        // It rides over the aim, so that what the aim lies on stays in view under it.
        const flat = Math.hypot(r.dir.x, r.dir.z)
        const pitch = Math.atan2(r.dir.y, flat) + CARD.heldOverDeg * RAD
        const yaw = Math.atan2(r.dir.x, -r.dir.z)
        TO.set(Math.sin(yaw) * Math.cos(pitch), Math.sin(pitch), -Math.cos(yaw) * Math.cos(pitch)).multiplyScalar(CARD.heldM).add(r.origin)
        g.position.lerp(TO, 0.5)
        // The mark: on the Bot, where the card would land in the open, or on its own place when it would go back
        // (D104 1: what the let-go will do is shown before it).
        const m = mark.current
        if (m) {
          m.visible = true
          if (!target) {
            m.position.set(home[0], home[1], home[2])
            facing(m.position, head, m.quaternion)
            m.scale.setScalar(CARD_BACK_MARK_M)
          } else {
            m.position.set(...(card.failed && target.to === 'open' && land.current ? land.current : landing(arr(r.origin), arr(r.dir), target, state.botsAt())))
            facing(m.position, head, m.quaternion)
            const bot = target.to === 'bot' ? state.botsAt().find(b => b.slug === target.slug) : undefined
            m.scale.setScalar(bot ? (bot.markM ?? bot.radiusM) * 1.15 : 0.16)
          }
        }
      }
      scale = CARD.heldScale
    } else if (card.phase === 'flying' || card.phase === 'sending' || card.phase === 'sent') {
      if (!flight.current) {
        const bots = state.botsAt()
        const bot = card.target?.to === 'bot' ? bots.find(b => b.slug === (card.target as { slug: string }).slug) : undefined
        camera.getWorldPosition(EYE)
        camera.getWorldDirection(DIR)
        const to = bot ? bot.at : land.current ?? landing(arr(EYE), arr(DIR), { to: 'open' }, bots)
        flight.current = { from: g.position.clone(), to: new Vector3(...to), t0: now, scale0: g.scale.x, landed: false }
      }
      const f = flight.current
      const k = Math.min(1, (now - f.t0) / CARD.flyMs)
      g.position.lerpVectors(f.from, f.to, flyShare(k))
      // It shrinks into a Bot; in the open it stays a small card until its chat's page stands there.
      const end = card.target?.to === 'bot' ? 0.25 : 0.45
      scale = f.scale0 + (end - f.scale0) * k
      if (card.phase === 'sent') scale = end * Math.max(0, 1 - (now - f.t0 - CARD.flyMs) / CARD.sentMs)
      if (k >= 1 && !f.landed) { f.landed = true; act({ type: 'landed' }) }
    } else {
      TO.set(...home)
      g.position.lerp(TO, 0.3)
    }
    if (card.phase !== 'held' && mark.current) mark.current.visible = false
    g.scale.setScalar(g.scale.x + (Math.max(scale, 0.001) - g.scale.x) * 0.35)
    facing(g.position, head, g.quaternion)
    state.drawn.at = arr(g.position)
    // Where the marks are in the room, for a test: only the ones that take a press now.
    g.updateMatrixWorld()
    const marks: Record<string, [number, number, number]> = {}
    const corner = CARD.heightM * CLOSE_SHARE * 0.7
    if (card.phase === 'out' || card.phase === 'typing') marks.close = arr(g.localToWorld(TO.set(CARD.widthM / 2 - corner, CARD.heightM / 2 - corner, 0)))
    if (card.phase === 'out' && card.mic !== 'none' && !card.failed) {
      marks.mic = arr(g.localToWorld(TO.set(...MIC_AT.at)))
      marks.lang = arr(g.localToWorld(TO.set(...LANG_AT.at)))
    }
    state.drawn.marks = marks
  })

  if (!card || !home) return null
  const takes = cardTakesDot(card)
  const close = CARD.heightM * CLOSE_SHARE
  return (
    <>
      {/* The dot is read on the group: it is entered while any mesh under it is hit (the face, the close mark, the
          microphone's mark, the language's), and left only when none is. A mark that goes away under the dot leaves
          the group entered while the face behind it is hit, and left when nothing is. */}
      <group ref={group} position={home} pointerEvents={(hushed && card.phase !== 'held') || !takes ? 'none' : undefined} pointerEventsOrder={card.phase === 'held' ? PRESS.held : undefined} userData={{ xrPress: 'card' }} onPointerEnter={(e: ThreeEvent<PointerEvent>) => state.dot('card', true, e.pointerId)} onPointerLeave={(e: ThreeEvent<PointerEvent>) => state.dot('card', false, e.pointerId)}>
        <mesh
          userData={{ xrCard: card.id, xrPress: true }}
          name="chat-card"
          renderOrder={8}
          onPointerEnter={() => setLit(true)}
          onPointerLeave={() => setLit(false)}
          onPointerDown={(e: ThreeEvent<PointerEvent>) => {
            if (grip.current || (card.phase !== 'out' && card.phase !== 'typing')) return
            e.stopPropagation()
            ;(e.object as Object3D).setPointerCapture(e.pointerId)
            const p = e as unknown as { point: Vector3; pointerPosition: Vector3; pointerType?: string }
            grip.current = { pointerId: e.pointerId, head: p.pointerType === 'head', origin: p.pointerPosition.clone(), dir: p.point.clone().sub(p.pointerPosition).normalize() }
            state.drawn.grip = { head: grip.current.head, pointerType: String(p.pointerType) }
            act({ type: 'grab' })
          }}
          onPointerMove={(e: ThreeEvent<PointerEvent>) => {
            const g = grip.current
            if (!g || g.pointerId !== e.pointerId || g.head) return
            e.stopPropagation()
            const p = e as unknown as { point: Vector3; pointerPosition: Vector3 }
            g.origin.copy(p.pointerPosition)
            g.dir.copy(p.point).sub(p.pointerPosition).normalize()
          }}
          onPointerUp={(e: ThreeEvent<PointerEvent>) => letGo(e, true)}
          // A pinch held long with the hand still ends as a cancel (`HeadAim.tsx`): for a card that is a let-go. A hand that was lost is not.
          onPointerCancel={(e: ThreeEvent<PointerEvent>) => letGo(e, pinchRoute.ending === 'long')}
        >
          <planeGeometry args={[CARD.widthM, CARD.heightM]} />
          <meshBasicMaterial map={picture.texture} transparent side={DoubleSide} depthWrite={false} toneMapped={false} />
        </mesh>
        {/* The close mark: the card is put away, and nothing is sent. */}
        {card.phase !== 'held' && (
          <mesh
            position={[CARD.widthM / 2 - close * 0.7, CARD.heightM / 2 - close * 0.7, 0.002]}
            userData={{ xrCardClose: card.id, xrPress: true }}
            name="chat-card-close"
            renderOrder={9}
            onPointerLeave={(e: ThreeEvent<PointerEvent>) => markLeft(e, 'close')}
            onPointerCancel={(e: ThreeEvent<PointerEvent>) => markCancel(e, 'close')}
            onPointerDown={(e: ThreeEvent<PointerEvent>) => markDown(e, 'close')}
            onPointerUp={(e: ThreeEvent<PointerEvent>) => { if (markUp(e, 'close')) state.close() }}
          >
            <planeGeometry args={[close, close]} />
            <meshBasicMaterial map={closeTexture()} transparent depthWrite={false} toneMapped={false} />
          </mesh>
        )}
        {/* The microphone's mark and the language's: a pinch on one is its own, and takes the card nowhere. */}
        {card.phase === 'out' && card.mic !== 'none' && !card.failed && ([['mic', MIC_AT, MIC_AT, () => act({ type: 'micPress' })], ['lang', LANG_AT, LANG_HIT_AT, state.switchLang]] as const).map(([name, box, hit, press]) => (
          <group key={name}>
            {/* What takes the press: for the language a little more than its frame, so that a ray that drifts stays on it. */}
            <mesh
              position={hit.at}
              scale={[hit.size[0], hit.size[1], 1]}
              userData={{ xrCardMark: name, xrPress: true }}
              name={`chat-card-${name}`}
              renderOrder={9}
              onPointerEnter={() => setLitMark(name)}
              onPointerLeave={(e: ThreeEvent<PointerEvent>) => { setLitMark(now => (now === name ? '' : now)); markLeft(e, name) }}
              onPointerCancel={(e: ThreeEvent<PointerEvent>) => markCancel(e, name)}
              onPointerDown={(e: ThreeEvent<PointerEvent>) => markDown(e, name)}
              onPointerUp={(e: ThreeEvent<PointerEvent>) => { if (markUp(e, name)) press() }}
            >
              <planeGeometry />
              <meshBasicMaterial visible={false} />
            </mesh>
            {/* What a pinch would take lights before the pinch, as large as the mark is drawn. */}
            <mesh position={[box.at[0], box.at[1], box.at[2] + 0.0005]} scale={[box.size[0], box.size[1], 1]} renderOrder={9} pointerEvents="none" visible={litMark === name}>
              <planeGeometry />
              <meshBasicMaterial color={XR_GL.white} transparent opacity={0.14} depthWrite={false} toneMapped={false} />
            </mesh>
          </group>
        ))}
      </group>
      {/* Where a held card would go. */}
      <group ref={mark} visible={false} pointerEvents="none">
        {aim?.to === 'bot' ? (
          <mesh renderOrder={10}>
            <ringGeometry args={[0.95, 1, 64]} />
            <meshBasicMaterial color={XR_GL.white} transparent opacity={0.9} depthTest={false} depthWrite={false} toneMapped={false} />
          </mesh>
        ) : (
          // The same frame in the open (dim) and round the card's own place when it would go back (lit, D104 1).
          <mesh renderOrder={10} scale={[1.6, 1, 1]}>
            <ringGeometry args={[0.92, 1, 4, 1, Math.PI / 4]} />
            <meshBasicMaterial color={aim ? XR_GL.dim : XR_GL.white} transparent opacity={0.8} depthTest={false} depthWrite={false} toneMapped={false} />
          </mesh>
        )}
      </group>
      {keys && <DeskKeys place={keys} head={head} onKey={key => act({ type: 'key', key })} hushed={hushed} line={card ? { text: card.text, interim: card.interim } : null} drawn={state.drawn} onDot={(on, pointerId) => state.dot('keys', on, pointerId)} />}
    </>
  )
}

let closePicture: CanvasTexture | null = null
/** The close mark's picture: a cross on a round dark plate, drawn once. */
function closeTexture(): CanvasTexture {
  if (closePicture) return closePicture
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 96
  const c = canvas.getContext('2d')
  if (c) {
    c.fillStyle = css(XR_GL.glass)
    c.beginPath()
    c.arc(48, 48, 44, 0, Math.PI * 2)
    c.fill()
    c.strokeStyle = css(XR_GL.dim)
    c.lineWidth = 8
    c.lineCap = 'round'
    c.beginPath()
    c.moveTo(32, 32); c.lineTo(64, 64)
    c.moveTo(64, 32); c.lineTo(32, 64)
    c.stroke()
  }
  closePicture = new CanvasTexture(canvas)
  closePicture.colorSpace = SRGBColorSpace
  return closePicture
}
