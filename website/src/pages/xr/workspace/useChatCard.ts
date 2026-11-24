/**
 * The new chat card of the workspace (`chatCard.ts`), as state: the one card that is out, where it stands, what it
 * hears (`chatSpeech.ts`) and its send. The workspace mounts it; `ChatCard.tsx` draws it.
 *
 * A send goes out when the card lands with words on it, through the card's own write (`cardSend`) with the card's own id, so that one
 * card is sent once. A card thrown at a Bot goes to that Bot's own thread; one thrown into the open starts a new
 * chat, and that chat's page opens where the card went.
 */
import type { KeysDrawn } from './DeskKeys'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Vector3 } from 'three'
import { activeLocale } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { mintSendId } from '../../../utils/sendDelivery'
import type { XrBot } from '../model'
import { cardChatLeft, cardSend } from '../writes'
import { cardKeysOver, cardKeysSpot } from './cardKeysPlace'
import { CARD, cardStep, keysShown, listens, newCard, type BotAt, type Card, type CardEvent, type CardTarget } from './chatCard'
import { LANG_KEY, langAfterRefusal, listen, nextLang, speechHere, speechLangs, startLang } from './chatSpeech'
import { deskPoint, type DeskPlace } from './desk'
import { KEYS, boardSize, deskKeysV } from './deskKeys'
import { placeAt } from './layout'
import { chatSessionPath } from './streamPath'

/** A Bot's body in the room, as the crew's row or the desk's seats draw it now. */
export interface BotBody { slug: string; feet: readonly [number, number, number]; top: readonly [number, number, number]; widthM: number }

export interface CardHost {
  head: () => Vector3
  /** Where the person looks, to the right of straight ahead. */
  lookYawDeg: () => number
  /** The desk, with `desk=1`; else `null`. */
  desk: () => DeskPlace | null
  bots: () => readonly XrBot[]
  bodies: () => readonly BotBody[]
  /** Opens a page of the dashboard on a panel in front of where the person looks. */
  openPage: (path: string) => void
  /** Says one line under the room. */
  say: (line: string) => void
  /** The room he is in. When it changes, a card that is out and its keys may glide to a free side, once (D144). */
  room?: string
  /**
   * When the quiet time ends or ended, on `performance.now()`'s clock. In it his dot's pointer is off
   * (`pinchRoute.light`): what the dot lay on is told a leave that is none of his head's, and an enter when it is up.
   */
  quietUntil?: () => number
  /**
   * Whether the dot's pointer has had two frames of its own since `t` (`pinchRoute.dotSeenSince`): in the first it
   * is turned on again, by the end of the second what it lies on has been told so.
   */
  dotSeenSince?: (t: number) => boolean
}

/** How long the card and its keys glide to their new side at a room switch (ms). */
export const CARD_SWITCH_GLIDE_MS = 300
/** The time between two steps of that glide (ms). */
const GLIDE_STEP_MS = 16
/** The glide starts this long after his dot left the card and its keys (ms): never while it is on them. */
export const CARD_DOT_AWAY_MS = 400
/**
 * After the quiet time nothing is judged for this long (ms), and then not before the dot's pointer has had two frames
 * of its own (`CardHost.dotSeenSince`): a frame of any length at the quiet end is waited out.
 * shortcut: where the host cannot say so (it gives no `dotSeenSince`, or it is the flat preview, whose mouse is told
 * again by the workspace's own timer and not by the dot's frame), this time stands alone; a frame longer than 100 ms
 * at the quiet end then lets the judging run before the pointer is on: one glide under it. Upgrade: have that
 * pointer say when it is on, as the dot does.
 */
export const CARD_DOT_BACK_MS = 100
/** How long the judging waits before it asks again whether the dot's pointer has had its frames (ms): about a frame. */
const DOT_FRAME_WAIT_MS = 16
/** The id a pointer is counted under when its enter names none (a caller with one pointer). */
const NO_POINTER_ID = -1
/** Whether the card can have his dot on it: it is there and takes the pointer (as `ChatCard` lets its group). */
export const cardTakesDot = (card: Card | null) => !!card && (card.phase === 'out' || card.phase === 'typing' || card.phase === 'held')

/** A key pressed this short a time ago holds the card and its keys where they are at a room switch: it is tried again once it has passed (ms). */
export const KEYS_SWITCH_QUIET_MS = 1000
/**
 * How long after a room switch the card's place is judged: the new room's Bots are read where they stand then (ms).
 * shortcut: a fixed wait, not the Bots' own "arrived"; upgrade when a row shows a Bot still walking in at this time.
 */
export const KEYS_SWITCH_SETTLE_MS = 400

/** How long the keys still wait at a room switch for a key pressed `sinceKeyMs` ago: 0 when they may move now. */
export const keysSwitchWaitMs = (sinceKeyMs: number): number => Math.max(0, KEYS_SWITCH_QUIET_MS - sinceKeyMs)

/** Where the keyboard is: its middle, which way it faces, whether it lies on the desk, and its step from key to key. */
export interface KeysPlace { at: [number, number, number]; yawDeg: number; flat: boolean; stepM: number }

/** What a test reads of the card. */
export interface CardProbe {
  card: Card | null
  home: [number, number, number] | null
  /** Where the card is drawn now, and where its marks are (`close`, `mic`, `lang`) while they take a press. */
  at: [number, number, number] | null
  marks: Record<string, [number, number, number]> | null
  /** The pointer that holds the card: whether its aim is the head's, and the pointer's own type. */
  grip: { head: boolean; pointerType: string } | null
  /** Held: where a let-go now would send it (`null`: back to its place), and the line its top says. */
  aim?: CardTarget | null
  top?: string
  /**
   * At the last let-go: what its top line said then, where it was sent (`null`: back), the aim's raw target then
   * (before it stood `STEADY_MS`) and how long that raw target had stood (ms).
   */
  letGo?: { top: string; target: CardTarget | null; raw: CardTarget | null; stoodMs: number } | null
  keys: (KeysPlace & { layer: string; centers: Record<string, [number, number, number]> }) | null
  /** How many sends were started, and how many went through. */
  sends: number
  sent: number
  /** The thread that the last send went to. */
  slot: string
  speech: boolean
}

export interface ChatCardState {
  card: Card | null
  home: [number, number, number] | null
  keys: KeysPlace | null
  /** The palette's "new chat": a card comes out, unless one is out already. */
  open: () => void
  /** The card is put away with nothing sent. */
  close: () => void
  act: (e: CardEvent) => void
  /**
   * A key of a real keyboard: taken (`true`) while the card's keyboard shows. While that keyboard is put away (a board
   * in his hand) a typing key is taken too and does nothing: it goes neither to the card nor to a page behind it.
   */
  typed: (key: string) => boolean
  botsAt: () => BotAt[]
  /** `ChatCard.tsx` and `DeskKeys.tsx` tell where they draw, for a test. */
  drawn: { at: [number, number, number] | null; marks: Record<string, [number, number, number]> | null; keys: KeysDrawn | null; grip: CardProbe['grip']; aim: CardTarget | null; top: string; letGo: { top: string; target: CardTarget | null; raw: CardTarget | null; stoodMs: number } | null }
  /** A pinch on the language's mark: the next language, kept in this browser for the next card. */
  switchLang: () => void
  read: () => CardProbe
  /**
   * While a board is in his hand the keyboard is put away (`true`), as a taken tray is: the board stands in front of
   * it. It comes back, with the card's words, when the board leaves his hand.
   */
  keysAway: (away: boolean) => void
  /**
   * His dot came onto the card or onto its keys (`on`), or left it: `card` is the card's whole group (its face and
   * every mark), `keys` the keys' whole group (the board and every key). While it is on either, a room switch does
   * not start their glide: a key would move from under his finger. It starts `CARD_DOT_AWAY_MS` after the dot left.
   * A part that goes takes its dot with it: a new card starts with none, and keys that are put away lose theirs.
   * `pointerId` names the pointer that came or left: each is counted, so his dot and a hand's ray hold a part apart
   * from each other. A leave with no pointer named frees every pointer on the part (the part itself went).
   */
  dot: (part: 'card' | 'keys', on: boolean, pointerId?: number) => void
  /**
   * While a hold's sheet listens (P2) the card's recognizer does not (`true`): one listener at a time, and his words go
   * to the hold. When the hold ends the card listens again, as before.
   */
  micAway: (away: boolean) => void
}

const v3 = (v: Vector3): [number, number, number] => [v.x, v.y, v.z]

export function useChatCard(host: CardHost): ChatCardState {
  const hostNow = useRef(host)
  hostNow.current = host
  const [card, setCard] = useState<Card | null>(null)
  const cardNow = useRef<Card | null>(null)
  const [home, setHome] = useState<[number, number, number] | null>(null)
  const [keys, setKeys] = useState<KeysPlace | null>(null)
  const [away, setAway] = useState(false)
  const [micAway, setMicAway] = useState(false)
  const awayNow = useRef(false)
  awayNow.current = away
  const counts = useRef({ sends: 0, sent: 0, slot: '' })
  const drawn = useMemo<ChatCardState['drawn']>(() => ({ at: null, marks: null, keys: null, grip: null, aim: null, top: '', letGo: null }), [])

  /** Where he stood and looked when the card came out, and the place of his look that it and its keys have now. */
  const cameOut = useRef<{ head: Vector3; yaw: number; place: readonly [number, number] } | null>(null)
  /** The room in which the card's place was last judged: inside it neither the card nor its keys move (D110). */
  const judgedIn = useRef('')
  const glideTimer = useRef(0)
  /** The parts of the card and its keys that his dot is on, when it last left them all, and a judging that waits for that. */
  /** The pointers that lie on each part, by their ids: a part is held while any pointer is on it (M170). */
  const dotOn = useRef(new Map<'card' | 'keys', Set<number>>())
  const dotLeftAt = useRef(-Infinity)
  const switchTimer = useRef(0)
  const waitsForDot = useRef<(() => void) | null>(null)
  const lastKeyAt = useRef(-Infinity)
  const put = useCallback((next: Card | null) => { cardNow.current = next; setCard(next) }, [])
  /** The feet of the Bots that ask or failed: the keys lie over none of them where a place is free (D101 5). */
  const busyFeet = useCallback(() => { const h = hostNow.current; return h.bots().filter(b => b.state === 'needs' || b.state === 'failed').flatMap(b => h.bodies().filter(x => x.slug === b.slug).map(x => x.feet)) }, [])
  const act = useCallback((e: CardEvent) => {
    const now = cardNow.current
    if (!now) return
    if (e.type === 'key') lastKeyAt.current = performance.now()
    const next = cardStep(now, e)
    if (next !== now) put(next)
  }, [put])

  const open = useCallback(() => {
    if (cardNow.current) return
    const h = hostNow.current
    const head = h.head()
    const desk = h.desk()
    const yaw = h.lookYawDeg()
    // Standing, the keys and their line come out where they lie over no Bot that asks or failed (D101 5); the card
    // stands over them, on the same side.
    const busy = busyFeet()
    const [side, down] = desk ? [CARD.outYawDeg, KEYS.standPitchDeg] : cardKeysSpot(head, yaw, busy)
    cameOut.current = desk ? null : { head: head.clone(), yaw, place: [side, down] }
    judgedIn.current = h.room ?? ''
    window.clearTimeout(glideTimer.current)
    setHome(desk ? deskPoint(desk, CARD.deskU, CARD.deskV, CARD.deskUpM) : v3(placeAt(head, yaw + side, CARD.outPitchDeg, CARD.outM)))
    setKeys(desk
      ? { at: deskPoint(desk, 0, deskKeysV(), 0.002), yawDeg: desk.yawDeg, flat: true, stepM: KEYS.deskStepM }
      : { at: v3(placeAt(head, yaw + side, down, KEYS.standM)), yawDeg: yaw + side, flat: false, stepM: KEYS.standStepM })
    let kept: string | null = null
    try { kept = window.localStorage.getItem(LANG_KEY) } catch { /* no store: the dashboard's language */ }
    put(newCard(mintSendId(), speechHere(), startLang(kept, activeLocale())))
  }, [put, busyFeet])

  // A room switch (D144): the card and its keys are one thing, and move together or not at all. They stay where they
  // are unless the card or the keys now lie over a Bot of the new room that asks or failed; then both glide once to
  // the nearest free side, as a card that comes out is placed. Judged once, after the last switch of a row; back in
  // the room where it was last judged, nothing moves (D110: inside one room neither ever moves). Not while a key was
  // pressed in the last second: it is tried again once that second is over. Not while his dot is on the card or its
  // keys: the glide starts `CARD_DOT_AWAY_MS` after the dot left them. A glide under way runs to its end. A card on
  // the desk, a failed card, and a card on its way or sent are not moved.
  const room = host.room ?? ''
  const roomWas = useRef(room)
  useEffect(() => {
    if (roomWas.current === room) return
    roomWas.current = room
    const judge = () => {
      const now = cardNow.current
      const from = cameOut.current
      if (!now || !from || now.failed || !(now.phase === 'out' || now.phase === 'held' || now.phase === 'typing')) return
      if (judgedIn.current === room) return
      const wait = keysSwitchWaitMs(performance.now() - lastKeyAt.current)
      if (wait > 0) { switchTimer.current = window.setTimeout(judge, wait); return }
      // The pages of the new room come, and the quiet time runs: his dot's pointer is off in it, so the card was told
      // a leave with his dot still on it. Nothing is judged in the quiet time, nor in `CARD_DOT_BACK_MS` after it:
      // by then the pointer is on again, and the dot is on them once more or he has looked away.
      const quietEnd = hostNow.current.quietUntil?.() ?? -Infinity
      const quiet = quietEnd + CARD_DOT_BACK_MS - performance.now()
      if (quiet > 0) { switchTimer.current = window.setTimeout(judge, quiet); return }
      // And not before the dot's pointer is on again: a long frame at the quiet end is waited out (M171).
      if (hostNow.current.dotSeenSince?.(quietEnd) === false) { switchTimer.current = window.setTimeout(judge, DOT_FRAME_WAIT_MS); return }
      // His dot is on them: the judging waits for it to leave (`dot`), and then `CARD_DOT_AWAY_MS` more.
      if (dotOn.current.size > 0) { waitsForDot.current = judge; return }
      const away = CARD_DOT_AWAY_MS - (performance.now() - dotLeftAt.current)
      if (away > 0) { switchTimer.current = window.setTimeout(judge, away); return }
      judgedIn.current = room
      const busy = busyFeet()
      if (!cardKeysOver(from.head, from.yaw, from.place, busy)) return
      const to = cardKeysSpot(from.head, from.yaw, busy, true)
      const [fromSide, t0] = [from.place[0], performance.now()]
      from.place = to
      if (to[0] === fromSide) return
      const step = () => {
        const k = Math.min(1, (performance.now() - t0) / CARD_SWITCH_GLIDE_MS)
        const side = fromSide + (to[0] - fromSide) * k * k * (3 - 2 * k)
        setHome(v3(placeAt(from.head, from.yaw + side, CARD.outPitchDeg, CARD.outM)))
        setKeys({ at: v3(placeAt(from.head, from.yaw + side, to[1], KEYS.standM)), yawDeg: from.yaw + side, flat: false, stepM: KEYS.standStepM })
        if (k < 1) glideTimer.current = window.setTimeout(step, GLIDE_STEP_MS)
      }
      window.clearTimeout(glideTimer.current)
      step()
    }
    switchTimer.current = window.setTimeout(judge, KEYS_SWITCH_SETTLE_MS)
    return () => { window.clearTimeout(switchTimer.current); waitsForDot.current = null }
  }, [room, busyFeet])
  // Pointers are counted for each part, by their ids (M170): his dot and a raised hand's ray are two pointers, and
  // one's leave does not free what the other still lies on. What frees a pointer's count: its own leave, which the
  // pointer library also sends when the pointer is turned off (`setEnabled`, the quiet time) and when it is taken
  // away (`exit`: a hand that drops out of view); and the part's own going, which frees every pointer on it.
  const dot = useCallback((part: 'card' | 'keys', on: boolean, pointerId?: number) => {
    const parts = dotOn.current
    const was = parts.size
    const ids = parts.get(part) ?? new Set<number>()
    if (on) ids.add(pointerId ?? NO_POINTER_ID)
    // A leave with no pointer named is the part's own going: every pointer on it is freed.
    else if (pointerId === undefined) ids.clear()
    else ids.delete(pointerId)
    if (ids.size > 0) parts.set(part, ids)
    else parts.delete(part)
    if (was === 0 || parts.size > 0) return
    dotLeftAt.current = performance.now()
    const judge = waitsForDot.current
    waitsForDot.current = null
    if (judge) switchTimer.current = window.setTimeout(judge, CARD_DOT_AWAY_MS)
  }, [])
  useEffect(() => () => window.clearTimeout(glideTimer.current), [])

  const switchLang = useCallback(() => {
    const now = cardNow.current
    if (!now) return
    const lang = nextLang(now.lang, speechLangs(activeLocale()))
    try { window.localStorage.setItem(LANG_KEY, lang) } catch { /* no store: the choice lasts as long as the card */ }
    act({ type: 'lang', lang })
  }, [act])
  const close = useCallback(() => {
    // A new chat that was started for a send that did not go through stays in the list of chats: he is told so.
    const now = cardNow.current
    if (now && cardChatLeft(now.id)) hostNow.current.say(i18nT('xr.card_chat_kept'))
    put(null)
  }, [put])

  // The recognizer listens while the card is out or in the hand, in the card's language, and at no other time.
  // Not while a hold's sheet listens (`micAway`).
  const hears = listens(card) && !micAway
  const cardId = card?.id ?? ''
  const lang = card?.lang ?? ''
  useEffect(() => {
    if (!hears) return
    const heard = listen(lang || activeLocale(), {
      started: () => act({ type: 'mic', state: 'on' }),
      heard: (final, interim) => act({ type: 'heard', final, interim, lang }),
      none: () => act({ type: 'mic', state: 'none' }),
      quiet: () => act({ type: 'quiet' }),
      // A language that the recognizer does not know: the first of the list, for this card alone (his choice stays
      // kept). When the first is refused too the microphone is turned off; the mark stays, so he can go on to another.
      noLang: () => {
        const first = langAfterRefusal(lang, speechLangs(activeLocale()))
        act(first ? { type: 'lang', lang: first } : { type: 'quiet' })
      },
    })
    if (!heard) { act({ type: 'mic', state: 'none' }); return }
    return () => { heard.stop(); act({ type: 'mic', state: 'off' }) }
  }, [hears, cardId, lang, act])

  // The microphone goes off when the page is hidden: nobody is there to see that it is on. It comes on again by his
  // press alone. (A session's end and its blur are the card's own to hear: `ChatCard.tsx`.)
  useEffect(() => {
    const hidden = () => { if (document.visibilityState === 'hidden') act({ type: 'quiet' }) }
    document.addEventListener('visibilitychange', hidden)
    return () => document.removeEventListener('visibilitychange', hidden)
  }, [act])


  // The send: once, when the card has landed with words on it.
  const phase = card?.phase ?? null
  useEffect(() => {
    const c = cardNow.current
    if (phase !== 'sending' || !c || !c.target) return
    const h = hostNow.current
    const target = c.target
    const bot = target.to === 'bot' ? h.bots().find(b => b.slug === target.slug) : null
    const done = (ok: boolean) => {
      // A card that was put away meanwhile has no result to show.
      if (cardNow.current?.id !== c.id) return
      act({ type: 'result', ok })
      // The request may have reached the gateway with only its reply lost: the line says no more than is known.
      if (!ok) h.say(i18nT('xr.card_not_sure'))
    }
    if (target.to === 'bot' && !bot) { done(false); return }
    counts.current.sends += 1
    cardSend(c.text, bot ? { bot: bot.slug, thread: bot.thread } : 'new', c.id).then(slot => {
      counts.current.sent += 1
      counts.current.slot = slot
      done(true)
      // A new chat's page opens in front of where the person looks, which is where the card went: its answer is read there.
      const page = target.to === 'open' ? chatSessionPath(slot) : null
      if (page) h.openPage(page)
    }, () => done(false))
  }, [phase, act])

  // A card that was sent shows so for a moment, and goes.
  useEffect(() => {
    if (phase !== 'sent') return
    const id = cardNow.current?.id
    const timer = window.setTimeout(() => { if (cardNow.current?.id === id) put(null) }, CARD.sentMs)
    return () => window.clearTimeout(timer)
  }, [phase, put])

  const typed = useCallback((key: string) => {
    // The card's keys only: an arrow still turns the head, and Tab still leaves the room.
    if (!keysShown(cardNow.current) || !(key === 'Enter' || key === 'Backspace' || [...key].length === 1)) return false
    // Its keyboard is put away for a while: the key was meant for the card, so no page behind gets it.
    if (awayNow.current) return true
    act({ type: 'key', key })
    return true
  }, [act])

  const botsAt = useCallback((): BotAt[] => hostNow.current.bodies().map(b => ({
    slug: b.slug,
    at: [(b.feet[0] + b.top[0]) / 2, (b.feet[1] + b.top[1]) / 2, (b.feet[2] + b.top[2]) / 2],
    // A throw reaches a Bot as a press does: across its body's own width.
    radiusM: b.widthM / 2,
    markM: Math.max(b.widthM, b.top[1] - b.feet[1]) / 2,
  })), [])

  const shown = keysShown(card) && !away
  // A part that goes takes its dot with it: what is no longer there to be pointed at sends no leave of its own.
  // Keys that are put away lose theirs. So does a card that takes no pointer: one that went, failed to be sent and
  // is flying, or is not out yet. A new card so starts with none. A mark that goes (the microphone turns `none`, the
  // card fails) leaves the card's group entered while the face behind it is hit: the group tells that itself.
  useEffect(() => { if (!shown) dot('keys', false) }, [shown, dot])
  const takesDot = cardTakesDot(card)
  useEffect(() => { if (!takesDot) dot('card', false) }, [takesDot, dot])
  const read = useCallback((): CardProbe => ({
    card: cardNow.current, home, at: cardNow.current ? drawn.at : null, marks: cardNow.current ? drawn.marks : null, grip: drawn.grip, aim: drawn.aim, top: drawn.top, letGo: drawn.letGo,
    keys: keys && !awayNow.current && keysShown(cardNow.current) && drawn.keys ? { ...keys, ...drawn.keys } : null,
    ...counts.current, speech: speechHere(),
  }), [home, keys, drawn])

  return { card, home: card ? home : null, keys: shown ? keys : null, open, close, act, typed, botsAt, drawn, switchLang, read, keysAway: setAway, micAway: setMicAway, dot }
}

/** How big the keyboard's board is at its place. */
export const keysSize = (place: KeysPlace): [number, number] => boardSize(place.stepM)
