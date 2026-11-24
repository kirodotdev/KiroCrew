/**
 * The ask's panels of the workspace: a Bot's ask comes up by itself on a small page of that Bot, straight ahead of
 * the eye and nearer than everything there; the person answers on the page's own card; and the panel goes when
 * nothing is asked any more. The rules are `asks.ts`'s; this hook keeps their clock and acts on what they say.
 *
 * A Bot whose own thread was cut while he is in comes up the same way, on a failure's panel (`fails.ts`): its page,
 * with Resume at its lower edge. A press there goes to the page and makes it his; it goes by itself only when the
 * thread runs again with no press of his.
 *
 * Nothing turns for an ask, and no panel that the person opened is moved, changed or closed here.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { Quaternion, Vector3 } from 'three'
import { i18nT } from '../../../i18n/t'
import { wholeRead, type XrCrew } from '../model'
import { ASKS, ASK_AIR_PX, ASK_SESSION, askLeastFlat, askPanelFlat, askPlace, askingBots, cardsOf, facePoints, freeAsks, heldBack, isBusy, keepsPanel, lensGoesBack, lensMoved, lensSetBack, newBook, nextStep, pictureFor, pressFate, readAsks, settle, takeAnswers, tapAnswer, wheelFate, type AnswerBook, type AskHold, type AskPanelNow, type LensBack } from './asks'
import { CLOSE_MARK } from './closeMark'
import { failGone, failQuiet, failUp, newFailBook, nextFail, readFails } from './fails'
import { botPage } from './crewRow'
import type { CrewProbe } from './Crew'
import { M_PER_PX, PAGES, slotOf } from './layout'
import type { PanelProbe } from './Panel'
import { askPanelWay, usePanelWay } from './panelWay'
import { inPicture, lensFor, shiftShare, under, type Turn, type View } from './previewLook'
import { MOST_LIVE_PAGES, type PageSource } from './source'
import { WALLS } from './walls'

/** An open panel, as the workspace keeps it. */
interface Open { id: string; source: PageSource; position: Vector3; css: [number, number]; turned?: Quaternion }

/** What the workspace hands the ask's panels: where things are, and the few things they may do. */
export interface AskHost {
  /** The open panels, and what each panel says of itself now. */
  panels: () => readonly Open[]
  probe: (id: string) => PanelProbe | undefined
  crewNow: () => readonly CrewProbe[]
  /** The head, which way it is turned (a session), and the flat preview's own head, lens and shift. */
  head: () => Vector3
  quat: () => Quaternion
  view: () => View
  turn: MutableRefObject<Turn>
  near: MutableRefObject<number>
  shift: MutableRefObject<number>
  inXr: () => boolean
  /** The drawn room is on. */
  walls: () => boolean
  /** With `desk=1`: points of the set desk's top and its Bots' heads, which an ask's panel stands clear of instead of the drawn room's desk. */
  deskPoints?: () => [number, number, number][] | undefined
  /** A finger or a button is down, two fingers move, the quiet time runs, or the picture turns by itself. */
  busy: () => boolean
  /** The room of places (`placesWanted`): no Bot's page comes up by itself, for an ask or for a cut thread. */
  noPages?: () => boolean
  /** When a key last went to a page, and when the wheel last turned on one. */
  lastKeyAt: MutableRefObject<number>
  lastWheelAt: MutableRefObject<number>
  /** How much of the canvas's lower end the phone's own field for typing covers now, in px. */
  fieldPx: () => number
  /** The source for a live page: streamed, or drawn by the XR tab itself where the address asks for it (`domSource.ts`). */
  liveSource: (id: string, path: string, cssWidth: number, cssHeight: number) => PageSource
  /** Add a panel where it is told, and take panels away. */
  add: (panel: Open) => void
  move: (id: string, to: Vector3, turned?: Quaternion) => void
  drop: (id: string) => void
  /** The quiet time of the flat preview, as after a close. */
  hush: () => void
  /** An ask's panel came up. The keys stay with the page that has them (`typing.ts`, `keysAfter`). */
  cameUp: (id: string) => void
  /** Say one line over the canvas for a little while: at its lower edge, or at its upper edge, clear of an ask's panel's buttons. */
  say: (words: string, top?: boolean) => void
  /** The room changed in a way that is kept. */
  roomChanged: () => void
  /** The panel is his page from now on, where it stands: placed as by his hand, so a load does not stand it again. */
  placed?: (id: string) => void
  /** Light a panel's frame for a moment: a Bot whose page stands was cut. */
  flash?: (id: string) => void
  /** The asks (`need.id`) on a board, or about to be (`useBoards.ts`): their panel does not come up by itself. */
  boards?: () => ReadonlySet<string>
  /** The Bots whose board stands, or is about to: every other ask of theirs waits behind it (`freeAsks`). */
  boardBots?: () => ReadonlySet<string>
  /** Of those, the Bots whose board he handed back himself: their questions do not wait behind it (D27). */
  handedBack?: () => ReadonlySet<string>
}

/** One ask's panel that stands. */
interface Rec {
  bot: string
  /** The person made it his page: it stays, and no rule of an ask's panel holds for it any more. */
  mine: boolean
  /** Since when nothing is asked on it any more. `null` while its Bot asks. */
  doneAt: number | null
  /** Its first picture came: what covers it until then is gone for good. */
  shown: boolean
  /** When it came up. The workspace has it among its panels a moment later: until then it is not taken for closed. */
  since: number
  /** A failure's panel: the cut that brought it (`XrBot.cut`). '' for an ask's panel, and once its Bot asks on it. */
  fail: string
  /** Why a failure's panel waits to close: its turn ran again, or the gateway restarted (`failGone`). */
  gone?: 'restart' | 'ran'
}

/** What a panel of the workspace shows over an ask's panel (`AskSign.tsx`). */
export interface AskSignNow { id: string; bot: string; label: string; avatar: unknown; name: string; count: number; what: 'cover' | 'done'; fail: boolean; restarted: boolean }

const RAD = Math.PI / 180
const DESK: [number, number, number][] = (() => {
  const points: [number, number, number][] = []
  const d = WALLS.desk
  for (let yaw = -d.halfDeg; yaw <= d.halfDeg; yaw += 2) for (const r of [d.innerM, (2 * d.innerM + d.outerM) / 3, (d.innerM + 2 * d.outerM) / 3, d.outerM]) for (const y of [d.topM, d.topM - d.thickM]) points.push([Math.sin(yaw * RAD) * r, y, -Math.cos(yaw * RAD) * r])
  return points
})()
/** What the glasses show to each side of straight ahead, and up and down: a point inside it is in view. */
const IN_VIEW_DEG = { across: 28, up: 19 } as const

export function useAskPanels(crew: XrCrew, bots: () => XrCrew['bots'], host: AskHost) {
  const way = usePanelWay()
  const wayNow = useRef(way)
  wayNow.current = way
  const book = useRef(newBook())
  const fails = useRef(newFailBook())
  const recs = useRef(new Map<string, Rec>())
  const changedAt = useRef(new Map<string, number>())
  const pressAt = useRef(new Map<string, number>())
  const closedAt = useRef<number | null>(null)
  // Where each ask's panel's page last said that the controls that answer an ask are, and when each was last asked.
  const answers = useRef<AnswerBook>(new Map())
  const askedAt = useRef(new Map<string, number>())
  // The lens from before an ask set it back to the start's: it goes back there when the last ask's panel is gone.
  const lensBack = useRef<LensBack | null>(null)
  const calmSince = useRef<number | null>(null)
  const stopped = useRef(false)
  const saidOnce = useRef(new Set<string>())
  const opened = useRef(0)
  const h = useRef(host)
  h.current = host
  // What is drawn for the asks: the plates at the Bots, and what stands over an ask's panel. Told anew when it changes.
  const [shown, setShown] = useState<{ plates: Map<string, number>; signs: AskSignNow[] }>({ plates: new Map(), signs: [] })

  const botOf = (slug: string) => bots().find(b => b.slug === slug)
  /** The record of a panel that is an ask's panel still: not the person's page, and its page still at its Bot's address. */
  const askOf = useCallback((id: string): Rec | null => {
    const rec = recs.current.get(id)
    if (!rec || rec.mine) return null
    const bot = bots().find(b => b.slug === rec.bot)
    const panel = h.current.panels().find(p => p.id === id)
    // One that came up in this very step is the workspace's a moment later: it is an ask's panel from the start.
    if (bot && !panel && !rec.shown && performance.now() - rec.since < 2000) return rec
    return bot && panel && panel.source.pagePath?.() === botPage(bot) ? rec : null
    // `bots` reads a ref of the workspace.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** Whether a point of the room is in the picture; in a session, in view. */
  const seen = (point: Vector3): boolean => {
    const host = h.current
    if (host.inXr()) {
      const local = point.clone().sub(host.head()).applyQuaternion(host.quat().clone().invert())
      if (local.z >= 0) return false
      return Math.abs(Math.atan2(local.x, -local.z)) / RAD <= IN_VIEW_DEG.across && Math.abs(Math.atan2(local.y, -local.z)) / RAD <= IN_VIEW_DEG.up
    }
    return inPicture(host.turn.current, host.view(), slotOf(host.head(), point))
  }
  /** Where an ask's panel is to the eye: its middle in view, and any part of it. */
  const inView = (id: string): { inView: boolean; partInView: boolean } => {
    const now = h.current.probe(id)
    const panel = h.current.panels().find(p => p.id === id)
    if (!now) return { inView: !!panel && seen(panel.position), partInView: !!panel && seen(panel.position) }
    const at = (a: number, b: number) => new Vector3(...now.position).add(new Vector3(...now.right).multiplyScalar((a * now.size[0]) / 2)).add(new Vector3(...now.up).multiplyScalar((b * now.size[1]) / 2))
    const middle = seen(at(0, 0))
    return { inView: middle, partInView: middle || [-1, 0, 1].some(a => [-1, 0, 1].some(b => seen(at(a, b)))) }
  }

  /** Where an ask's panel would come up now, and how big its page is; `null` when something stands in the way. */
  const place = (skip: string | null): { at: Vector3; css: [number, number]; turned?: Quaternion } | null => {
    const host = h.current
    const things: (readonly [number, number, number])[] = []
    for (const p of host.panels()) {
      if (p.id === skip) continue
      const now = host.probe(p.id)
      if (!now) continue
      const half = (v: [number, number, number], size: number): [number, number, number] => [(v[0] * size) / 2, (v[1] * size) / 2, (v[2] * size) / 2]
      things.push(...facePoints(now.position, half(now.right, now.size[0]), half(now.up, now.size[1])))
    }
    const eye = host.head()
    const fronts: (readonly [number, number, number])[] = []
    for (const b of host.crewNow()) {
      // A body is round, as deep as it is wide, and its sign stands 12 mm in front of it: its front, as the person sees it, is that much nearer than its middle.
      const to: [number, number] = [eye.x - b.feet[0], eye.z - b.feet[2]]
      const len = Math.hypot(to[0], to[1]) || 1
      const tall = (b.top[1] - b.feet[1]) / 2
      const front = b.widthM / 2 + 0.012
      fronts.push(...facePoints([b.feet[0] + (to[0] / len) * front, b.feet[1] + tall, b.feet[2] + (to[1] / len) * front], [(to[1] / len) * (b.widthM / 2), 0, (-to[0] / len) * (b.widthM / 2)], [0, tall, 0], 2, 4))
    }
    if (host.inXr()) {
      // Straight ahead of the head, no farther up or down than a person holds a page to read it.
      const ahead = new Vector3(0, 0, -1).applyQuaternion(host.quat())
      const flat = Math.hypot(ahead.x, ahead.z) || 1
      const pitch = Math.max(-25 * RAD, Math.min(15 * RAD, Math.atan2(ahead.y, flat)))
      const dir: [number, number, number] = [(ahead.x / flat) * Math.cos(pitch), Math.sin(pitch), (ahead.z / flat) * Math.cos(pitch)]
      const css = ASK_SESSION.css
      const got = askPlace({ eye: [eye.x, eye.y, eye.z], ahead: dir, widthM: css[0] * M_PER_PX, heightM: css[1] * M_PER_PX, distanceM: ASK_SESSION.distanceM, leastM: PAGES.nearM, things, bots: fronts, desk: host.deskPoints?.() ?? (host.walls() ? DESK : undefined) })
      return got ? { at: new Vector3(...got.at), css: [css[0], css[1]] } : null
    }
    // The eye is nearer than at the start: a panel whose buttons are whole in the picture would stand behind the
    // pages. The lens goes back to the start's, and stays there.
    const seenNow = host.view()
    const near = Math.min(1, host.near.current)
    const view: View = { width: seenNow.width, height: seenNow.height, fovDeg: lensFor(seenNow.width, seenNow.height, near), shift: host.shift.current * shiftShare(near) }
    // The phone's own field for typing stays with the page that the person types in: the panel stands over it.
    const lowPx = host.fieldPx()
    const size = askPanelFlat(view, M_PER_PX, CLOSE_MARK.pressPx, lowPx)
    // The nearest it may stand: where the card's buttons are still whole in the picture.
    const leastM = askLeastFlat(view, size, PAGES.nearM)
    // Square to the lens, so that it is drawn upright and at one size all over. Its middle is a little under the
    // picture's middle, by half the close mark's height: the mark stands over the panel's upper edge. Where
    // something makes it come nearer than that, it grows about its middle as far as the picture allows (its
    // upper edge stays under the mark's room, its lower end over the picture's lower end); only then does its lower
    // end stay, and the mark is what leaves the picture (`askPlace`, `ends`).
    const quat = host.quat().clone()
    const axis = (x: number, y: number, z: number): [number, number, number] => { const v = new Vector3(x, y, z).applyQuaternion(quat); return [v.x, v.y, v.z] }
    const [ahead, right, up] = [axis(0, 0, -1), axis(1, 0, 0), axis(0, 1, 0)]
    const dir = under(host.turn.current, view.width / 2, (view.height - lowPx) / 2 + CLOSE_MARK.pressPx / 2, view)
    const along = (a: [number, number, number]) => dir[0] * a[0] + dir[1] * a[1] + dir[2] * a[2]
    const rise = (y: number) => { const d = under(host.turn.current, view.width / 2, y, view); return (d[0] * up[0] + d[1] * up[1] + d[2] * up[2]) / (d[0] * ahead[0] + d[1] * ahead[1] + d[2] * ahead[2]) }
    const ends: [number, number] = [rise(view.height - lowPx - ASK_AIR_PX), rise(ASK_AIR_PX + CLOSE_MARK.pressPx)]
    const got = askPlace({ eye: [eye.x, eye.y, eye.z], ahead, right, up, slope: [along(right) / along(ahead), 0], low: along(up) / along(ahead) - (size.css[1] * M_PER_PX) / (2 * size.distanceM), ends, widthM: size.css[0] * M_PER_PX, heightM: size.css[1] * M_PER_PX, distanceM: size.distanceM, leastM, things, bots: fronts, desk: host.deskPoints?.() ?? (host.walls() ? DESK : undefined) })
    if (!got) return null
    lensBack.current = lensSetBack(lensBack.current, host.near.current, near)
    host.near.current = near
    return { at: new Vector3(...got.at), css: size.css, turned: quat }
  }

  /** Say a line once for a thing: an ask that the room has no place for. */
  const sayOnce = (key: string, words: string) => {
    if (saidOnce.current.has(key)) return
    saidOnce.current.add(key)
    h.current.say(words)
  }
  const count = (slug: string) => book.current.counts.get(slug) ?? 0

  /** What is drawn for the asks now. */
  const draw = useCallback(() => {
    const now = performance.now()
    const plates = new Map<string, number>()
    const signs: AskSignNow[] = []
    const inFront = new Set<string>()
    for (const [id, rec] of recs.current) {
      const bot = botOf(rec.bot)
      const panel = h.current.panels().find(p => p.id === id)
      // A panel that came up in this very step is the workspace's a moment later: what covers it is drawn with it.
      if (!bot || (!panel && (rec.shown || now - rec.since > 2000))) continue
      if (askOf(id)) {
        if (inView(id).inView) inFront.add(rec.bot)
        const fail = rec.fail !== ''
        const restarted = rec.gone === 'restart'
        if (!rec.shown) signs.push({ id, bot: bot.slug, label: bot.label, avatar: bot.avatar, name: bot.name, count: Math.max(1, count(bot.slug)), what: 'cover', fail, restarted })
        else if (rec.doneAt !== null && now - rec.doneAt < ASKS.doneMs) signs.push({ id, bot: bot.slug, label: bot.label, avatar: bot.avatar, name: bot.name, count: 0, what: 'done', fail, restarted })
      }
    }
    // A plate while a Bot asks and its ask's panel is not in front, the Bot whose ask was seen first before the others.
    if (book.current.started) for (const slug of askingBots(book.current)) if (!inFront.has(slug)) plates.set(slug, count(slug))
    setShown(before => {
      const same = before.plates.size === plates.size && [...plates].every(([k, v]) => before.plates.get(k) === v)
        && before.signs.length === signs.length && signs.every((s, i) => before.signs[i].id === s.id && before.signs[i].what === s.what && before.signs[i].count === s.count && before.signs[i].fail === s.fail && before.signs[i].restarted === s.restarted)
      return same ? before : { plates, signs }
    })
    // Everything it reads is a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [askOf])

  /** The ask's panel of a Bot comes up: a new one, or the one that stands out of the picture. Says whether it did. */
  /** A press on an ask's panel carries where its page last drew the controls that answer an ask; once it is the person's page, none. */
  const wire = useCallback((id: string, source: PageSource) => {
    source.answersBy?.({
      heard: () => askOf(id) ? answers.current.get(id)?.places ?? null : null,
      moved: places => { if (askOf(id)) takeAnswers(answers.current, id, places, performance.now()) },
    })
  }, [askOf])

  const comeUp = useCallback((slug: string, fail = ''): boolean => {
    const host = h.current
    const bot = botOf(slug)
    const path = bot ? botPage(bot) : null
    if (!bot || !path) { settle(book.current, slug); return false }
    const standing = [...recs.current].find(([id, rec]) => rec.bot === slug && askOf(id))?.[0] ?? null
    if (!standing && host.panels().length >= MOST_LIVE_PAGES) { sayOnce(`full:${slug}:${count(slug)}`, i18nT('xr.ask_full', { name: bot.label, most: MOST_LIVE_PAGES })); return false }
    const lens = host.near.current
    const to = place(standing)
    if (!to) { sayOnce(`way:${slug}:${count(slug)}`, i18nT('xr.ask_in_the_way', { name: bot.label })); return false }
    // The lens was set back to the start's: the pages are drawn smaller from here on, and what a finger was aimed at is not where it was.
    if (lensMoved(lens, host.near.current)) { closedAt.current = performance.now(); host.hush() }
    settle(book.current, slug)
    if (standing) { host.move(standing, to.at, to.turned); host.cameUp(standing) }
    else {
      opened.current += 1
      const id = `ask-${opened.current}`
      host.cameUp(id)
      recs.current.set(id, { bot: slug, mine: false, doneAt: null, shown: false, since: performance.now(), fail })
      const source = host.liveSource(id, path, to.css[0], to.css[1])
      wire(id, source)
      host.add({ id, source, position: to.at, css: to.css, turned: to.turned })
    }
    host.say(fail ? i18nT('xr.fail_line', { name: bot.label }) : i18nT('xr.ask_line', { name: bot.label, count: Math.max(1, count(slug)) }), true)
    draw()
    return true
    // Everything it reads is a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [askOf, draw, wire])

  /** The panel is the person's page from now on. */
  const keep = (id: string) => {
    const rec = recs.current.get(id)
    if (!rec || rec.mine) return
    rec.mine = true
    if (rec.fail) failQuiet(fails.current, rec.bot, performance.now())
    // The person keeps it where it stands, as it is drawn now: the lens stays.
    lensBack.current = null
    // Where it stands is his choice now, as a page he placed: `settleRoom` leaves it after a load (`room.ts`).
    h.current.placed?.(id)
    h.current.roomChanged()
    draw()
  }

  // A read of the crew: what is new, what is gone, and which Bot's cards changed.
  useEffect(() => {
    const now = performance.now()
    for (const slug of readAsks(book.current, crew)) changedAt.current.set(slug, now)
    readFails(fails.current, crew, now)
    if (wholeRead(crew.feeds)) {
      for (const [id, rec] of recs.current) {
        if (!askOf(id)) continue
        // A new ask of that Bot ends the seconds before the close: the panel asks again. A failure's panel on which
        // its Bot asks is that Bot's ask's panel from then on.
        if (cardsOf(book.current, rec.bot) > 0) { rec.doneAt = null; rec.fail = '' }
        // A failure's panel waits to close once its thread is not that cut any more (it runs again: resumed elsewhere).
        else if (rec.fail) {
          const gone = failGone(rec.fail, crew.bots.find(b => b.slug === rec.bot)?.cut ?? '')
          if (gone === 'stays') { rec.doneAt = null; rec.gone = undefined } else if (rec.doneAt === null) { rec.doneAt = now; rec.gone = gone }
        }
        else if (rec.doneAt === null) rec.doneAt = now
      }
    }
    if (book.current.waiting.size > 0 || fails.current.waiting.size > 0) askPanelWay()
    draw()
  }, [crew, askOf, draw])

  // The clock: the person's calm, the close of a panel on which nothing is asked any more, and the next ask.
  useEffect(() => {
    const tick = () => {
      const host = h.current
      const now = performance.now()
      const busy = isBusy({ now, held: host.busy(), lastKeyAt: host.lastKeyAt.current, lastWheelAt: host.lastWheelAt.current })
      if (busy) calmSince.current = null
      else if (calmSince.current === null) calmSince.current = now
      const panels: AskPanelNow[] = []
      for (const id of [...answers.current.keys(), ...askedAt.current.keys()]) if (!recs.current.has(id)) { answers.current.delete(id); askedAt.current.delete(id) }
      for (const [id, rec] of [...recs.current]) {
        const panel = host.panels().find(p => p.id === id)
        if (!panel) { if (now - rec.since > 2000) recs.current.delete(id); continue }
        if (!rec.shown && panel.source.pictureAt?.() != null) rec.shown = true
        if (!askOf(id)) {
          // Its page moved on to another address: the person went somewhere in it, and it is his page.
          const bot = botOf(rec.bot)
          const at = panel.source.pagePath?.()
          if (!rec.mine && bot && at && at !== botPage(bot)) keep(id)
          continue
        }
        // The open was refused, or the page failed: its line was said; the panel goes, and nothing more comes up by itself.
        const status = panel.source.status?.()
        if (status === 'failed' || status === 'signed-out') {
          if (!saidOnce.current.has(`failed:${id}`)) { saidOnce.current.add(`failed:${id}`); stopped.current = true; setTimeout(() => { if (recs.current.delete(id)) h.current.drop(id) }, 4000) }
          continue
        }
        if (rec.doneAt !== null && now - rec.doneAt >= ASKS.doneMs) {
          // It closes by itself. What stood behind it is not what a finger was aimed at.
          recs.current.delete(id)
          closedAt.current = now
          host.hush()
          host.drop(id)
          continue
        }
        // Where the controls that answer an ask are on its page: asked a few times a second while it stands, one question at a time.
        if (rec.shown && panel.source.answerPlaces && now - (askedAt.current.get(id) ?? 0) >= ASKS.answersEveryMs) {
          askedAt.current.set(id, Infinity)
          void panel.source.answerPlaces().then(places => {
            if (!recs.current.has(id)) return
            askedAt.current.set(id, performance.now())
            takeAnswers(answers.current, id, places, performance.now())
          })
        }
        // One that waits to close holds the next ask back as well while it is in view: its line is read, and a press may keep it.
        panels.push({ bot: rec.bot, ...inView(id) })
      }
      if (book.current.waiting.size > 0) {
        if (wayNow.current !== 'open') askPanelWay(now)
        // An ask on a board: the board is how it asks, and its page comes when he asks for it. While a Bot's board
        // stands, every other ask of that Bot waits behind it, one board at a time (D24, `freeAsks`).
        // Behind a board he handed back, a question comes up as before (D27).
        const hold: AskHold = {
          onBoards: host.boards?.() ?? new Set<string>(),
          boardBots: host.boardBots?.() ?? new Set<string>(),
          handedBack: host.handedBack?.() ?? new Set<string>(),
          kindOf: id => { const bot = book.current.waiting.get(id); return bot ? botOf(bot)?.needs.find(n => n.id === id)?.kind : undefined },
        }
        const waiting = freeAsks(book.current.waiting, hold)
        const behind = (id: string, bot: string) => heldBack(id, bot, hold)
        const view = { ...book.current, waiting }
        const step = waiting.size > 0 ? nextStep(view, { now, way: wayNow.current, calmSince: calmSince.current, panels, open: host.panels().length, most: MOST_LIVE_PAGES, stopped: stopped.current }) : { do: 'nothing' as const }
        // What the step settled in its view is settled in the book.
        for (const [id, bot] of [...book.current.waiting]) if (!behind(id, bot) && !waiting.has(id)) book.current.waiting.delete(id)
        // `comeUp` settles all of its Bot's asks: the ones behind a board were not in the step's view, so they still wait.
        const onBoards = [...book.current.waiting].filter(([id, bot]) => behind(id, bot))
        // In the room of places no Bot's page comes by itself (D19): the ask is on its Bot's board, or it waits on
        // the Bot and on Home's count, and its page opens when he asks for it.
        if (step.do === 'up' || step.do === 'again') { if (host.noPages?.()) settle(book.current, step.bot); else comeUp(step.bot) }
        else if (step.do === 'full') { const bot = botOf(step.bot); if (bot) sayOnce(`full:${step.ask}`, i18nT('xr.ask_full', { name: bot.label, most: MOST_LIVE_PAGES })) }
        for (const [id, bot] of onBoards) if (!book.current.waiting.has(id)) book.current.waiting.set(id, bot)
      }
      // A Bot's own thread that was cut: after the asks, one at a time.
      if (fails.current.waiting.size > 0) {
        if (wayNow.current !== 'open') askPanelWay(now)
        const standing = (slug: string) => { const bot = botOf(slug); const path = bot ? botPage(bot) : null; return path ? host.panels().find(p => p.source.pagePath?.() === path)?.id ?? null : null }
        const step = nextFail(fails.current, { now, open: wayNow.current === 'open' && !stopped.current, calmSince: calmSince.current, calmMs: ASKS.calmMs, asks: slug => count(slug) > 0, standing, panelInView: panels.some(p => p.inView), askWaits: book.current.waiting.size > 0, pages: host.panels().length, most: MOST_LIVE_PAGES })
        if (step.do === 'light') host.flash?.(step.id)
        // A cut thread's step brings no page in any view (D35, the lead's 14:57): its Bot's failed sheet replaces it
        // (`useSays`, `says.cutToRaise`). The step is still taken, so the book says the cut was shown.
        else if (step.do === 'up') failUp(fails.current, step.bot)
      }
      // The lens that an ask set back goes to where it was, once the last ask's panel is gone and none is about to come up.
      if (lensBack.current) {
        const standing = [...recs.current.values()].filter(rec => !rec.mine).length
        const coming = book.current.waiting.size > 0 && wayNow.current === 'open' && !stopped.current
        if (standing === 0 && !coming) {
          const back = lensGoesBack(lensBack.current, { near: host.near.current, panels: standing, coming })
          lensBack.current = null
          // The pages are drawn larger from here on: what a finger was aimed at is not where it was.
          if (back !== null) { host.near.current = back; closedAt.current = now; host.hush() }
        }
      }
      draw()
    }
    const timer = setInterval(tick, 200)
    return () => clearInterval(timer)
    // Everything a tick reads is a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [askOf, comeUp, draw])

  /** The press gate's answer for a panel now, with what it knows of the panel; writes nothing (`pressFate`, `pressHeld`). */
  const fateOf = (id: string, now: number): { fate: 'send' | 'hold' | 'keep'; bot: boolean; fail: boolean } | null => {
    const panel = h.current.panels().find(p => p.id === id)
    if (!panel) return null
    const rec = askOf(id)
    const path = panel.source.pagePath?.()
    const bot = path ? bots().find(b => botPage(b) === path) : undefined
    // A failure's panel holds a press in its first second and after a close, as an ask's does; then the press goes
    // to the page (nothing on it is an ask's answer), and it is his page.
    const fail = rec !== null && rec.fail !== ''
    const fate = pressFate({
      now,
      pictureAt: pictureFor(panel.source),
      ask: rec !== null,
      done: rec !== null && !fail && rec.doneAt !== null,
      botPage: !!bot,
      botAsks: !!bot && cardsOf(book.current, bot.slug) > 0,
      pressAt: pressAt.current.get(id) ?? null,
      changedAt: bot ? changedAt.current.get(bot.slug) ?? null : null,
      closedAt: closedAt.current,
      answersAt: rec !== null && !fail && panel.source.answerPlaces ? answers.current.get(id)?.at ?? null : undefined,
    })
    return { fate, bot: !!bot, fail }
  }

  return useMemo(() => ({
    way,
    plates: shown.plates,
    signs: shown.signs,
    /**
     * Whether a press that ends on this panel's page goes to the page (`asks.ts`, `pressFate`). It goes at once: on
     * an ask's panel it carries where the page last drew the controls that answer an ask, and the gateway sends none
     * when a card came or moved since (`answersBy` above).
     */
    /** What becomes of a press on this panel's page: sent to it, held back, or only keeping the panel (`pressFate`). */
    pressFate: (id: string): 'send' | 'hold' | 'keep' => {
      const now = performance.now()
      const got = fateOf(id, now)
      if (!got) return 'hold'
      if (got.bot) pressAt.current.set(id, now)
      if (got.fate === 'keep' || got.fail) keep(id)
      return got.fate
    },
    /**
     * Whether a press on this panel's page would be held back now, by the same word the press gate reads, with
     * nothing written: the dot shows it, its light off (D53 2).
     */
    pressHeld: (id: string): boolean => (fateOf(id, performance.now())?.fate ?? 'hold') === 'hold',
    /** The person changed the lens himself (two fingers, the wheel beside a page, the home button): it stays where he put it. */
    lensChanged: () => { lensBack.current = null },
    /** Whether a notch of the wheel on this panel's page is the page's now. */
    mayWheel: (id: string): boolean => {
      const panel = h.current.panels().find(p => p.id === id)
      if (!panel) return false
      return wheelFate({ now: performance.now(), pictureAt: pictureFor(panel.source), ask: askOf(id) !== null }) === 'send'
    },
    /** A key went to this panel's page, or the wheel turned on it. */
    typedInto: (id: string) => { if (askOf(id) && keepsPanel('key', false)) keep(id) },
    wheelOn: (id: string) => { const rec = askOf(id); if (rec && keepsPanel('wheel', rec.doneAt !== null)) keep(id) },
    /** Whether this panel is an ask's panel that is not kept with the room, and that turns no picture when it closes. */
    isAsk: (id: string): boolean => { const rec = recs.current.get(id); return !!rec && !rec.mine },
    /** A new source for a panel that stands (a page handed to the stream): an ask's panel's carries its answers' places. */
    wire,
    /** The person closed this panel by its mark: while its ask waits, every ask of that Bot that waits is put off. */
    closedByMark: (id: string) => {
      const rec = recs.current.get(id)
      recs.current.delete(id)
      if (rec && !rec.mine && rec.doneAt === null) settle(book.current, rec.bot)
      if (rec && !rec.mine && rec.fail) failQuiet(fails.current, rec.bot, performance.now())
      draw()
    },
    /**
     * A tap on a Bot (`asks.ts`, `tapAnswer`). With an ask on a card and the panels open, its ask's panel comes up
     * straight ahead; when a part of it is in the picture already, the answer is the panel's id, for the picture to
     * turn to. `false`: the tap does what it does for a Bot that asks nothing, also when the panel could not come up.
     */
    tapBot: (slug: string): boolean | string => {
      // A page opens by a tap: what a refusal stopped may come up again.
      stopped.current = false
      const standing = [...recs.current].find(([id, rec]) => rec.bot === slug && askOf(id))?.[0] ?? null
      const up = tapAnswer({ open: wayNow.current === 'open', cards: cardsOf(book.current, slug), standing, partInView: standing !== null && inView(standing).partInView }, () => comeUp(slug))
      draw()
      return up
    },
    /** The gateway refused to open this panel's page: when it is an ask's panel, nothing more comes up by itself. */
    refused: (id: string) => { if (recs.current.has(id)) stopped.current = true },
    /** A page opened by the person's own tap: what a refusal stopped may come up again. */
    tapped: () => { stopped.current = false },
    /** What a test reads. */
    read: () => ({
      way: wayNow.current, lensBack: lensBack.current ? { ...lensBack.current } : null, answers: Object.fromEntries([...answers.current].map(([id, a]) => [id, { places: a.places, at: Math.round(a.at) }])), plates: [...shown.plates], signs: shown.signs.map(s => `${s.id}:${s.what}`), started: book.current.started, waiting: [...book.current.waiting.keys()], stopped: stopped.current, calm: calmSince.current !== null,
      panels: [...recs.current].map(([id, rec]) => ({ id, bot: rec.bot, mine: rec.mine, done: rec.doneAt !== null, shown: rec.shown, fail: rec.fail, ...inView(id) })),
      fails: { started: fails.current.started, waiting: [...fails.current.waiting], done: fails.current.done.size, quiet: [...fails.current.quietUntil.keys()] },
    }),
    // The rest reads refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [way, shown, askOf, comeUp, draw])
}
