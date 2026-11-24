/**
 * What each XR panel shows. Every function here draws one panel from data
 * the dashboard already has, in the same words the dashboard uses.
 *
 * Three kinds of text never look alike:
 *   - what the page itself states: upright, no bar ("Atlas asks", "Read 2m ago")
 *   - an exact command: a fixed-width box, at the largest text size on the card
 *   - what a Bot or a worker wrote, titles included: a grey bar on its left
 * A title is crew-written, so it is never drawn in the page's own warning line.
 *
 * Font sizes come from `metrics.ts`, in degrees at the eye, so no text here
 * can fall under the reading floor.
 */
import { fmtRelative } from '../../i18n/format'
import { XR_NUMBER_OLD_S, type XrGlance, type XrNumber } from './glance'
import type { Fan } from './choices'
import { BODY_DEG, CARD_FONT as C, CHOICE, FULL_FONT as F, pxFor } from './metrics'
import type { XrBot, XrCrew, XrFeedState, XrNeed, XrNotice, XrThing } from './model'
import type { PageKind } from './pages'
import type { Painter, Tone } from './painter'
import type { Part } from './parts'
import type { BoundValue } from './sceneDoc'
import type { XrStrings } from './strings'
import { commandFits, crewText } from './text'

export interface XrLine {
  who: 'you' | 'bot' | 'tool'
  text: string
  ts: number
  /** Characters cut from the end of a very long message. */
  cut?: number
  /** How many tool rows this one line stands for. */
  run?: number
}

export interface XrThread { lines: XrLine[]; more: boolean; state: XrFeedState }

/** Words that wait for a Bot: typed, spoken and not sent, or handed over. Only a guarded press sends them. */
export interface XrDraft {
  bot: string
  text: string
  /** The keyboard types into this draft right now. */
  typing: boolean
}

export type FullKind = PageKind

export interface FullView {
  kind: FullKind
  bot: XrBot | null
  face: CanvasImageSource | null
  /** The thing whose thread this is; '' for the Bot's own thread. */
  topic: string
  scroll: number
  thread: XrThread | null
  glance: XrGlance | null
  draft: XrDraft | null
  crew: XrCrew
  notices: readonly XrNotice[]
  toast: string
  now: number
  /** Whether Send can act in this build. */
  writes: boolean
  /** The fields of this Bot that were pulled out of a page and stand in the air as things. Their rows carry a marker. */
  out: ReadonlySet<string>
}

export const FULL_W = 1024
/**
 * Wider than tall: about 20 degrees tall at reading distance. At that height two panels, every Bot from head
 * to feet and the count fit what the glasses show at one head pose (`stage.test.ts`). A taller panel does not.
 */
export const FULL_H = 720
export const CARD_W = 720
/** A command longer than this many lines cannot be read whole, so it cannot be approved here. */
export const COMMAND_LINES = 6
const PAD = 40
const lh = (px: number) => Math.round(px * 1.32)

const stateTone: Record<XrBot['state'], Tone> = { idle: 'plain', working: 'accent', needs: 'warn', failed: 'danger' }

function stateWord(S: XrStrings, s: XrBot['state']): string {
  return s === 'working' ? S.stateWorking : s === 'needs' ? S.stateNeeds : s === 'failed' ? S.stateFailed : S.stateIdle
}

const ago = (ts: number, now: number): string => fmtRelative(ts * 1000, { now })

/** The sentence an ask reads as, for a screen reader. On a panel its parts are drawn apart. */
export function askLine(S: XrStrings, n: XrNeed): string {
  const who = n.who || S.hub
  return n.topic ? S.asksFor(who, n.topic, n.action) : S.asks(who, n.action)
}

function dot(p: Painter, x: number, y: number, tone: Tone, filled = true): void {
  p.ctx.beginPath()
  p.ctx.arc(x, y, 9, 0, Math.PI * 2)
  if (filled) { p.ctx.fillStyle = p.tone(tone); p.ctx.fill() } else { p.ctx.strokeStyle = p.pal.faint; p.ctx.lineWidth = 3; p.ctx.stroke() }
}

/** Text a Bot or a worker wrote: a grey bar on the left marks it as theirs. */
function crewPara(p: Painter, text: string, x: number, y: number, max: number, px: number, maxLines: number, color = p.pal.ink, bold = false): number {
  p.font(px, { bold })
  const end = p.para(crewText(text), x + 18, y + px, max - 18, lh(px), color, maxLines).y - px + 6
  p.box(x, y, 5, end - y - 4, 2, p.pal.faint)
  return end
}

function section(p: Painter, label: string, y: number): number {
  p.font(F.small, { bold: true })
  p.text(label.toUpperCase(), PAD, y + F.small, p.pal.dim)
  return y + F.small + 18
}

/** How a read went, in the page's own words, when it did not simply succeed. */
function feedNote(p: Painter, S: XrStrings, state: XrFeedState, x: number, y: number, px: number, max: number): number {
  if (state === 'ok') return y
  p.font(px, { bold: state === 'failed' })
  p.text(state === 'failed' ? S.feedFailed : S.feedLoading, x, y + px, state === 'failed' ? p.pal.danger : p.pal.dim, max)
  return y + lh(px) + 8
}

function numberTile(p: Painter, S: XrStrings, n: XrNumber, x: number, y: number, w: number, h: number, f: typeof F, now: number): void {
  p.box(x, y, w, h, 20, p.pal.raised, p.pal.edge)
  p.font(f.big, { bold: true })
  const vw = p.text(crewText(n.value), x + 18, y + f.big + 8, p.pal.ink, w - 96)
  if (n.target) { p.font(f.small); p.text('/ ' + crewText(n.target), x + 26 + vw, y + f.big + 8, p.pal.dim, w - vw - 90) }
  if (n.trend !== 'none') p.icon(n.trend, x + w - 54, y + 16, 36)
  p.font(f.small)
  const labelLines = Math.max(1, Math.floor((h - f.big - 30 - lh(f.small)) / lh(f.small)))
  p.para(crewText(n.label), x + 18, y + f.big + 22 + f.small, w - 36, lh(f.small), p.pal.dim, labelLines)
  // Each number says when it was read. An old reading is marked, so it cannot pass for a new one.
  const old = n.at > 0 && now / 1000 - n.at > XR_NUMBER_OLD_S
  p.text(n.at ? S.readAt(ago(n.at, now)) : S.noUpdate, x + 18, y + h - 14, old || !n.at ? p.pal.warn : p.pal.faint, w - 36)
}

/** One ask: who asks (the page's words), about what (theirs), the request (theirs), the exact command. */
function needBlock(p: Painter, S: XrStrings, n: XrNeed, y: number, max: number, buttons: boolean): number {
  p.font(F.body, { bold: true })
  p.text(S.asksSys(crewText(n.who) || S.hub), PAD, y + F.body, p.pal.warn, max)
  let yy = y + lh(F.body) + 4
  if (n.topic) yy = crewPara(p, n.topic, PAD, yy, max, F.small, 2, p.pal.dim)
  if (n.kind === 'attention') { p.font(F.small); yy = p.para(S.attention, PAD, yy + F.small, max, lh(F.small), p.pal.dim, 2).y - F.small + 8 }
  if (n.action) yy = crewPara(p, n.action, PAD, yy, max, F.body, 4)
  let fits = true
  if (n.command) {
    const shown = crewText(n.command)
    p.font(F.command, { mono: true })
    fits = commandFits(p.measure, shown, max - 36, COMMAND_LINES)
    const ls = p.lines(shown, max - 36, COMMAND_LINES)
    const boxH = ls.length * lh(F.command) + 22
    p.box(PAD, yy + 4, max, boxH, 12, p.pal.raised, p.pal.edge)
    p.ctx.fillStyle = p.pal.ink
    ls.forEach((l, i) => p.ctx.fillText(l, PAD + 18, yy + 10 + F.command + i * lh(F.command)))
    yy += boxH + 12
    // The other fields of the call are part of what is approved, so they are shown too.
    p.font(F.small, { mono: true })
    for (const e of n.extra.slice(0, 4)) {
      const el = p.lines(crewText(e), max - 18, 2)
      if (el.length > 1 && p.measure(crewText(e)) > (max - 18) * 2) fits = false
      el.forEach((l, i) => p.text(l, PAD + 18, yy + F.small + i * lh(F.small), p.pal.dim, max - 18))
      yy += el.length * lh(F.small) + 4
    }
    if (n.extra.length > 4) fits = false
  }
  for (const q of n.questions) {
    if (q.header) { p.font(F.small, { bold: true }); p.text(crewText(q.header), PAD + 18, yy + F.small, p.pal.dim, max - 18); yy += lh(F.small) }
    if (q.question !== n.action) yy = crewPara(p, q.question, PAD, yy, max, F.body, 3)
    p.font(F.small)
    for (const o of q.options) { p.text('· ' + crewText(o), PAD + 18, yy + F.small, p.pal.dim, max - 18); yy += lh(F.small) }
    yy += 6
  }
  if (!buttons) return yy + 10
  const bw = 210
  let bx = PAD
  if (n.kind === 'approval') {
    // A press never approves. It asks the Bot to hold the request out as a card; the yes is given on that card.
    if (fits) { p.btn(`offer:${n.id}`, S.bringCard, bx, yy + 8, bw, 70, { tone: 'ok', px: F.body }); bx += bw + 14 } else {
      p.font(F.small, { bold: true }); yy = p.para(S.tooLong, PAD, yy + F.small + 4, max, lh(F.small), p.pal.warn, 2).y - F.small + 2
    }
  }
  p.btn(`need:${n.id}`, S.open, bx, yy + 8, bw, 70, { icon: 'chat', px: F.body })
  return yy + 96
}

/** How high a row of work is on its page, in canvas pixels. A hand pinches it, so it may not be lower than `TUNE.rowMinM`. */
export const THING_ROW_PX = lh(F.body) + lh(F.small) + 28

function thingRow(p: Painter, S: XrStrings, t: XrThing, y: number, max: number, now: number, out: boolean): number {
  const h = THING_ROW_PX
  p.box(PAD - 12, y, max + 24, h - 8, 18, p.state.hot === `thing:${t.slot}` ? p.pal.raised : null, null)
  dot(p, PAD + 8, y + 26, t.state === 'needs' ? 'warn' : t.state === 'working' ? 'accent' : t.state === 'failed' ? 'danger' : 'plain', t.state !== 'idle')
  p.font(F.small)
  const tw = t.ts ? p.text(ago(t.ts, now), PAD + max, y + F.body + 6, p.pal.faint, 220, 'right') : 0
  // A thing's title is crew-written: bar, not the page's own style. With no title, the page says whose chat it is.
  if (t.topic) { p.box(PAD + 30, y + 10, 5, F.body + 6, 2, p.pal.faint); p.font(F.body, { bold: true }); p.text(crewText(t.topic), PAD + 46, y + F.body + 6, p.pal.ink, max - 60 - tw) } else { p.font(F.body, { bold: true }); p.text(S.theirThread, PAD + 34, y + F.body + 6, p.pal.ink, max - 48 - tw) }
  p.font(F.small)
  p.text(crewText(t.line), PAD + 46, y + F.body + 12 + lh(F.small), p.pal.dim, max - 46)
  // A row that was pulled out keeps its place, with a mark that the thing itself is in the air now.
  if (out) p.box(PAD - 12, y, 6, h - 8, 3, p.pal.accent)
  p.hit(`thing:${t.slot}`, PAD - 12, y, max + 24, h - 8, { drag: 'thing' })
  return y + h
}

/* ---------------- cards next to a Bot ---------------- */

/** What a look or a resting finger shows: who it is, how it stands, and its one line: the oldest ask, or else how the work stands. */
export function paintBrief(p: Painter, S: XrStrings, bot: XrBot, g: XrGlance): void {
  p.begin(30)
  const max = p.w - PAD * 2
  p.font(C.title, { bold: true })
  const nw = p.text(crewText(bot.label), PAD, 54, p.pal.ink, max - 230)
  p.font(C.small)
  p.text(stateWord(S, bot.state), PAD + nw + 16, 54, p.tone(stateTone[bot.state]), 210)
  const need = g.needs.reduce<XrNeed | null>((a, n) => (a && a.ts <= n.ts ? a : n), null)
  if (need) crewPara(p, need.topic ? `${need.topic}: ${need.action}` : need.action, PAD, 76, max, C.body, 3)
  else if (g.verdict) crewPara(p, g.verdict, PAD, 76, max, C.body, 3)
  else feedNote(p, S, g.briefFeed === 'failed' ? 'failed' : g.goalsFeed, PAD, 76, C.small, max)
}

/** What a Bot hears: who it is for, the words so far, and what became of them. Spoken words have no button. */
export function paintCaption(p: Painter, S: XrStrings, label: string, text: string, note: string, about = ''): void {
  p.begin(30)
  const max = p.w - PAD * 2
  p.font(C.small, { bold: true })
  const tw = p.text(S.draftTo(crewText(label)), PAD, 50, p.pal.accent, max)
  // What the other hand holds is what "this" means in the sentence. It is named, so the person sees what was understood.
  if (about) { p.font(C.small); p.text(S.aboutThis(crewText(about)), PAD + tw + 18, 50, p.pal.dim, max - tw - 18) }
  p.font(C.title)
  p.para(text || S.listening, PAD, 98, max, lh(C.title), text ? p.pal.ink : p.pal.dim, 3)
  if (note) { p.font(C.small, { bold: true }); p.para(note, PAD, p.h - 62, max, lh(C.small), p.pal.warn, 2) }
}

/** What a thing on the land is: its own title, how it stands, and how many more like it are not drawn. */
export function paintTip(p: Painter, S: XrStrings, v: BoundValue, fallback: string, more: number): void {
  p.begin(30)
  const max = p.w - PAD * 2
  const y = crewPara(p, v.label || fallback, PAD, 30, max, C.body, 2)
  p.font(C.small)
  if (v.found && v.state !== 'idle' && v.state !== 'done') p.text(stateWord(S, v.state), PAD + 18, y + C.small, p.tone(stateTone[v.state]), max - 18)
  else if (v.level !== null) p.text(`${Math.round(v.level * 100)}%`, PAD + 18, y + C.small, p.pal.dim, max - 18)
  if (more > 0) p.text(S.more(more), p.w - PAD, y + C.small, p.pal.dim, 240, 'right')
}

export interface ShelfRow { key: string; label: string; kind: string; face: CanvasImageSource | null }

/** One part of a thing that was pulled apart. Its heading is the page's own word; what follows is the crew's. */
export function paintPart(p: Painter, S: XrStrings, part: Part, now: number): void {
  p.begin(30)
  // The part in trouble is lit: a part that waits on the person, or that failed. The light is its edge, clear of the text.
  if (part.lit) p.box(3, 3, p.w - 6, p.h - 6, 30, null, p.tone(part.state === 'failed' ? 'danger' : 'warn'))
  const max = p.w - PAD * 2
  const heads: Record<Part['head'], string> = { what: part.state && part.state !== 'done' ? stateWord(S, part.state) : '', latest: S.tabChat, ask: S.stateNeeds, goal: S.goals, work: S.tabWork }
  p.font(C.small, { bold: true })
  p.text(heads[part.head].toUpperCase(), PAD, 46, part.state && part.state !== 'done' ? p.tone(stateTone[part.state]) : p.pal.dim, max - 220)
  p.font(C.small)
  if (part.at) p.text(ago(part.at, now), p.w - PAD, 46, p.pal.faint, 210, 'right')
  let y = crewPara(p, part.text, PAD, 62, max, C.body, part.command ? 2 : 4)
  if (part.command) {
    // The exact command, as on every ask: fixed width, and cut only where the box ends.
    p.font(C.small, { mono: true })
    const ls = p.lines(crewText(part.command), max - 28, 2)
    p.box(PAD, y + 2, max, ls.length * lh(C.small) + 16, 10, p.pal.raised, p.pal.edge)
    p.ctx.fillStyle = p.pal.ink
    ls.forEach((l, i) => p.ctx.fillText(l, PAD + 14, y + 8 + C.small + i * lh(C.small)))
    y += ls.length * lh(C.small) + 22
  }
  if (part.more > 0) { p.font(C.small); p.text(S.more(part.more), p.w - PAD, p.h - 26, p.pal.dim, 240, 'right') }
}

/** One choice card on its canvas, in pixels: 10.5 by 6.5 cm at 0.6 m, which is 14 by 8.7 cm at the surface's own distance. */
export const CHOICE_PX = { w: 320, h: 196, gap: 16 }

/** Where each choice card stands on a canvas `pw` by `ph`: rows centred, standing on the bottom edge, a gap between any two. */
export function choiceRects(rows: readonly number[], pw: number, ph: number): { x: number; y: number; w: number; h: number }[] {
  const { w, h, gap } = CHOICE_PX
  const top = ph - rows.length * (h + gap)
  return rows.flatMap((n, r) => Array.from({ length: n }, (_, k) => ({ x: (pw - (n * w + (n - 1) * gap)) / 2 + k * (w + gap), y: top + r * (h + gap), w, h })))
}

/**
 * The cards a Bot holds out for a thing that was given to it. Each card is its own: none touches another, and the
 * land shows between them. The card under the hand lights before the pinch. `title` is the thing, in the crew's
 * words; `note` is why the last pinch did not count. A card that sends is a guarded press; "more" is a plain one.
 */
export function paintChoices(p: Painter, S: XrStrings, title: string, note: string, f: Fan, versionOf: (i: number) => string): void {
  p.begin(0)
  p.ctx.clearRect(0, 0, p.w, p.h)
  const px = pxFor(CHOICE, 1)
  // The rows stand on the bottom edge, so one row sits just above its Bot.
  const rects = choiceRects(f.rows, p.w, p.h)
  const top = rects[0]?.y ?? p.h
  rects.forEach(({ x, y, w, h }, i) => {
    const c = f.cards[i]
    const id = c.id === 'more' ? 'more' : `choose:${i}`
    const hot = p.state.hot === id
    p.box(x, y, w, h, 22, hot ? p.pal.raised : p.pal.glass, hot ? p.pal.accent : p.pal.edge, hot ? 5 : 2)
    if (p.state.hold && p.state.hold.id === id && p.state.hold.p > 0) {
      p.ctx.save()
      p.ctx.beginPath()
      p.ctx.roundRect(x, y, w * p.state.hold.p, h, 22)
      p.ctx.globalAlpha = 0.35
      p.ctx.fillStyle = p.pal.accent
      p.ctx.fill()
      p.ctx.restore()
    }
    p.font(px, { bold: true })
    p.para(c.label, x + 18, y + 14 + px, w - 36, lh(px), c.id === 'more' ? p.pal.dim : p.pal.ink, 3)
    p.hit(id, x, y, w, h, c.id === 'more' ? {} : { guarded: true, version: versionOf(i) })
  })
  // What the cards are about, or why the last pinch did not count, on a small plate above the first row.
  const head = note || S.aboutThis(crewText(title))
  const hp = pxFor(CHOICE, BODY_DEG)
  p.font(hp, { bold: !!note })
  const tw = Math.min(p.measure(head), p.w - 120)
  p.box((p.w - tw) / 2 - 20, top - hp - 30, tw + 40, hp + 22, 14, p.pal.glass, p.pal.edge)
  p.text(head, (p.w - tw) / 2, top - 22, note ? p.pal.warn : p.pal.dim, tw)
}

/** The card a Bot holds out for one request: who asks, what, and the exact command. `on` lights its edge while the ray of a hand is on it or a hand holds it. Returns whether all of it can be read. */
export function paintAsk(p: Painter, S: XrStrings, n: XrNeed, note: string, on: boolean): boolean {
  p.begin(30)
  if (on) p.box(3, 3, p.w - 6, p.h - 6, 30, null, p.pal.accent)
  const max = p.w - PAD * 2
  p.font(C.small, { bold: true })
  p.text(S.asksSys(crewText(n.who) || S.hub), PAD, 46, p.pal.warn, max)
  let y = crewPara(p, n.action || n.topic, PAD, 60, max, C.body, 2)
  p.font(C.command, { mono: true })
  const shown = crewText(n.command)
  const fits = commandFits(p.measure, shown, max - 28, COMMAND_LINES) && n.extra.length <= 2
  const ls = p.lines(shown, max - 28, COMMAND_LINES)
  if (ls.length) {
    p.box(PAD, y + 2, max, ls.length * lh(C.command) + 16, 10, p.pal.raised, p.pal.edge)
    p.ctx.fillStyle = p.pal.ink
    ls.forEach((l, i) => p.ctx.fillText(l, PAD + 14, y + 8 + C.command + i * lh(C.command)))
    y += ls.length * lh(C.command) + 24
  }
  p.font(C.small, { mono: true })
  for (const e of n.extra.slice(0, 2)) { p.text(crewText(e), PAD + 14, y + C.small, p.pal.dim, max - 14); y += lh(C.small) }
  p.font(C.small, { bold: true })
  p.para(note, PAD, p.h - 62, max, lh(C.small), p.pal.accent, 2)
  return fits
}

/** The pages that are put away, each one press from coming back. */
export function paintShelf(p: Painter, S: XrStrings, rows: readonly ShelfRow[]): void {
  p.begin(30)
  const max = p.w - PAD * 2
  p.font(C.small, { bold: true })
  p.text(S.shelfTitle.toUpperCase(), PAD, 48, p.pal.dim, max)
  if (!rows.length) { p.font(C.body); p.text(S.shelfEmpty, PAD, 104, p.pal.dim, max) }
  rows.forEach((r, i) => {
    const y = 66 + i * 82
    p.box(PAD - 10, y, max + 20, 74, 18, p.state.hot === `page:${r.key}` ? p.pal.edge : p.pal.raised, p.pal.edge)
    if (r.face) p.ctx.drawImage(r.face, PAD, y + 9, 56, 56)
    p.font(C.body, { bold: true })
    const lw = p.text(crewText(r.label), PAD + 70, y + 46, p.pal.ink, max - 250)
    p.font(C.small)
    p.text(r.kind, PAD + 86 + lw, y + 46, p.pal.dim, max - lw - 100)
    p.hit(`page:${r.key}`, PAD - 10, y, max + 20, 74)
  })
}

/** One short line a Bot said, next to it. It is the Bot's words, so it carries the bar. */
export function paintBubble(p: Painter, text: string): void {
  p.begin(30)
  crewPara(p, text, PAD, 30, p.w - PAD * 2, C.title, 3)
}

/** The crew-wide count. It is the only number that is always on, so it must never lie. */
export function paintCount(p: Painter, S: XrStrings, crew: XrCrew): void {
  const { ctx } = p
  const count = crew.needs.length
  const failed = crew.feeds.asks === 'failed' || crew.feeds.roster === 'failed'
  p.hits = []
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, p.w, p.h)
  ctx.textBaseline = 'alphabetic'
  ctx.globalAlpha = 0.9
  p.box(2, 2, p.w - 4, p.h - 4, p.h / 2, p.pal.glass, failed ? p.pal.danger : count ? p.pal.warn : p.pal.edge)
  ctx.globalAlpha = 1
  p.font(30, { bold: true })
  // A failed read shows a question mark, not a zero. A dropped connection says the number may be old.
  const label = failed ? S.countUnknown : crew.feeds.asks === 'loading' ? S.feedLoading : (count ? S.needsYou(count) : S.needsNone) + (crew.feeds.live ? '' : ' · ' + S.stale)
  const tw = Math.min(p.measure(label), p.w - 60)
  p.text(label, (p.w - tw) / 2, p.h / 2 + 11, failed ? p.pal.danger : !crew.feeds.live ? p.pal.dim : count ? p.pal.warn : p.pal.dim, p.w - 60)
  p.hit('hub', 0, 0, p.w, p.h)
}

/** What the wearer must know about this build and its data, drawn in the scene itself. */
export function paintStatus(p: Painter, parts: readonly string[], alarm: boolean): void {
  const { ctx } = p
  p.hits = []
  ctx.setTransform(1, 0, 0, 1, 0, 0)
  ctx.clearRect(0, 0, p.w, p.h)
  ctx.textBaseline = 'alphabetic'
  p.font(26, { bold: alarm })
  // Set against the right edge, next to the count: a longer notice grows away from it.
  p.text(parts.join('  ·  '), p.w - 10, p.h / 2 + 9, alarm ? p.pal.danger : p.pal.dim, p.w - 20, 'right')
}

/** The hand strip: three tools and a grip. Icons only: the count has one place, by the land. */
export function paintStrip(p: Painter, S: XrStrings, shelf: boolean, whole: boolean): void {
  p.begin(p.h / 2)
  const tools: [string, string, string][] = [
    ['tool:next', 'next', S.handNext],
    ['tool:shelf', 'store', S.handShelf],
    ['tool:map', 'map', S.handMap],
    ['grip', 'full', S.handFull],
  ]
  const cell = (p.w - 40) / tools.length
  tools.forEach(([id, icon, label], i) => {
    const x = 20 + i * cell
    const on = (id === 'tool:shelf' && shelf) || (id === 'tool:map' && whole)
    const hot = p.state.hot === id
    if (i === tools.length - 1) p.box(x - 1, 34, 2, p.h - 68, 1, p.pal.edge)
    p.box(x + cell / 2 - 50, 26, 100, 100, 50, hot ? p.pal.edge : p.pal.raised, on ? p.pal.accent : hot ? p.pal.ink : p.pal.edge, on ? 4 : 2)
    p.icon(icon, x + cell / 2 - 26, 50, 52)
    p.font(20, { bold: true })
    p.text(label, x + cell / 2, 158, on ? p.pal.accent : p.pal.dim, cell - 8, 'center')
    p.hit(id, x + 4, 14, cell - 8, p.h - 28, id === 'grip' ? { drag: 'grip' } : {})
  })
}

/* ---------------- the full panels ---------------- */

const HEAD_H = 118
const TABS_Y = 126
const BODY_Y = 216
const COMPOSER_H = 232
/** The composer with no draft in it: the row of buttons alone, so the chat keeps the room. */
const COMPOSER_IDLE_H = 110

function chrome(p: Painter, S: XrStrings, v: FullView): void {
  p.begin(40)
  p.hit('move', 0, 0, p.w, HEAD_H, { drag: 'move' })
  let x = PAD
  if (v.face) { p.ctx.drawImage(v.face, x, 24, 72, 72); x += 88 }
  p.font(F.title, { bold: true })
  const tw = p.text(v.bot ? crewText(v.bot.label) : S.hub, x, 58, p.pal.ink, p.w - x - 250)
  p.font(F.small)
  if (v.bot) p.text(stateWord(S, v.bot.state), x + tw + 16, 58, p.tone(stateTone[v.bot.state]), 220)
  // Which thing this is, under the Bot's name. It is crew-written, so it carries the bar.
  if (v.topic) { p.box(x, 70, 5, F.small + 6, 2, p.pal.faint); p.text(crewText(v.topic), x + 16, 68 + F.small, p.pal.dim, p.w - x - 260) } else p.text(v.bot ? (v.kind === 'chat' ? S.theirThread : '') : (v.writes ? '' : S.readOnly), x, 68 + F.small, p.pal.dim, p.w - x - 250)
  p.btn('store', '', p.w - 184, 26, 70, 70, { icon: 'store' })
  p.btn('close', '', p.w - 104, 26, 70, 70, { icon: 'close' })
  if (v.bot) {
    const tabs: [FullKind, string, string][] = [['chat', S.tabChat, 'chat'], ['dash', S.tabDash, 'dash'], ['work', S.tabWork, 'work']]
    const w = (p.w - PAD * 2 - 24) / 3
    tabs.forEach(([k, label, icon], i) => {
      const bx = PAD + i * (w + 12)
      p.btn(`tab:${k}`, label, bx, TABS_Y, w, 70, { icon, tone: v.kind === k ? 'accent' : 'plain', px: F.small })
      if (v.kind === k) p.box(bx + 24, TABS_Y + 72, w - 48, 4, 2, p.pal.accent)
    })
  }
  p.hit('resize', p.w - 88, p.h - 88, 88, 88, { drag: 'resize' })
}

function toast(p: Painter, text: string, y: number): void {
  if (!text) return
  p.font(F.small, { bold: true })
  const w = Math.min(p.w - PAD * 2, p.measure(text) + 56)
  p.box((p.w - w) / 2, y, w, 56, 28, p.pal.raised, p.pal.warn)
  p.text(text, p.w / 2, y + 38, p.pal.warn, w - 40, 'center')
}

function paintChat(p: Painter, S: XrStrings, v: FullView): number {
  const max = p.w - PAD * 2
  const top = BODY_Y
  const bottom = p.h - (v.draft ? COMPOSER_H : COMPOSER_IDLE_H)
  const t = v.thread
  const lines = t?.lines ?? []
  // Measure every bubble first: the newest sits at the bottom, as in the dashboard.
  const bubbles = lines.map(l => {
    const tool = l.who === 'tool'
    const px = tool ? F.small : F.body
    p.font(px, { mono: tool })
    const text = tool && (l.run ?? 1) > 1 ? `${l.text}  (${S.steps(l.run ?? 1)})` : l.who === 'you' ? l.text : crewText(l.text)
    const wrap = p.lines(text, max - (tool ? 40 : 110), tool ? 1 : Infinity)
    const cutH = l.cut ? lh(F.small) + 6 : 0
    return { l, px, wrap, cutH, h: wrap.length * lh(px) + (tool ? 14 : 44) + cutH }
  })
  const headH = t?.more ? 76 : 0
  const total = bubbles.reduce((a, b) => a + b.h + 10, 0) + headH
  const maxScroll = Math.max(0, total - (bottom - top))
  const scroll = Math.max(0, Math.min(maxScroll, v.scroll))
  p.scrollBegin(top, bottom - top, 0)
  let y = Math.max(top, bottom - total) + (total > bottom - top ? scroll - maxScroll : 0)
  // Older messages are one press away, never silently missing.
  if (t?.more) { p.btn('earlier', S.loadEarlier, PAD + max / 2 - 230, y + 4, 460, 60, { px: F.small }); y += headH }
  if (!t || t.state === 'loading') { p.font(F.body); p.text(S.feedLoading, p.w / 2, (top + bottom) / 2, p.pal.dim, max, 'center') } else if (t.state === 'failed') { p.font(F.body, { bold: true }); p.text(S.feedFailed, p.w / 2, (top + bottom) / 2, p.pal.danger, max, 'center') } else if (!lines.length) { p.font(F.body); p.text(S.emptyChat, p.w / 2, (top + bottom) / 2, p.pal.dim, max, 'center') }
  for (const b of bubbles) {
    if (y + b.h >= top - 20 && y <= bottom + 20) {
      if (b.l.who === 'tool') {
        p.font(b.px, { mono: true })
        p.text(b.wrap[0] ?? '', PAD + 24, y + b.px + 2, p.pal.faint, max - 40)
      } else {
        const mine = b.l.who === 'you'
        p.font(b.px)
        const bw = Math.min(max - 70, Math.max(...b.wrap.map(p.measure), 160) + 44)
        const bx = mine ? p.w - PAD - bw : PAD
        p.box(bx, y, bw, b.h, 20, mine ? p.pal.raised : null, mine ? p.pal.edge : null)
        if (!mine) p.box(bx, y + 8, 5, b.h - 16, 2, p.pal.faint)
        p.ctx.fillStyle = p.pal.ink
        b.wrap.forEach((s, i) => p.ctx.fillText(s, bx + 22, y + 14 + b.px + i * lh(b.px)))
        if (b.l.cut) { p.font(F.small, { bold: true }); p.text(S.messageCut(b.l.cut), bx + 22, y + b.h - 14, p.pal.warn, bw - 40) }
      }
    }
    y += b.h + 10
  }
  p.scrollEnd()
  // The composer is one row of buttons until there are words to show; then it grows to hold them.
  // Send is a guarded press; in a read-only build it says so when tried.
  const d = v.draft
  const cy = bottom + 14
  if (d) {
    p.box(PAD - 12, cy, max + 24, COMPOSER_H - 44, 24, p.pal.raised, p.pal.edge)
    p.font(F.body)
    p.para(d.text || S.draftEmpty, PAD + 8, cy + F.body + 14, max - 16, lh(F.body), d.text ? p.pal.ink : p.pal.faint, 2)
  }
  const by = d ? cy + 106 : cy
  const bw = 190
  p.btn('kbd', S.keyboard, PAD, by, bw + 40, 66, { icon: 'keyboard', px: F.small, tone: d?.typing ? 'accent' : 'plain' })
  p.btn('send', S.send, p.w - PAD - bw - 76, by, bw, 66, { icon: 'send', px: F.small, tone: 'accent', guarded: true, off: !v.writes, version: `${d?.bot ?? ''}|${d?.text ?? ''}` })
  toast(p, v.toast, cy - 66)
  return maxScroll
}

function paintDash(p: Painter, S: XrStrings, v: FullView): number {
  const max = p.w - PAD * 2
  const top = BODY_Y
  const viewH = p.h - top - 30
  const g = v.glance
  p.scrollBegin(top, viewH, v.scroll)
  let y = top + 8
  if (!g || !v.bot) { p.scrollEnd(); return 0 }
  if (g.verdict) {
    y = crewPara(p, g.verdict, PAD, y, max, F.title, 4)
    p.font(F.small); p.text(g.verdictAt ? S.readAt(ago(g.verdictAt, v.now)) : S.noUpdate, PAD, y + F.small, p.pal.faint, max); y += lh(F.small) + 12
  }
  y = feedNote(p, S, g.briefFeed, PAD, y, F.small, max)
  y = section(p, S.numbers, y)
  y = feedNote(p, S, g.goalsFeed, PAD, y, F.small, max)
  // Every key result, three to a row. Nothing is left out of the full view.
  for (let i = 0; i < g.numbers.length; i += 3) {
    const row = g.numbers.slice(i, i + 3)
    const gap = 14
    const tw = (max - gap * 2) / 3
    row.forEach((n, j) => {
      const x = PAD + j * (tw + gap)
      numberTile(p, S, n, x, y, tw, 214, F, v.now)
      // A key result can be pulled out of the page too, and stays live in the air.
      if (v.out.has(`goal:${i + j}`)) p.box(x, y, 6, 214, 3, p.pal.accent)
      p.hit(`goal:${i + j}`, x, y, tw, 214, { drag: 'thing' })
    })
    y += 230
  }
  if (!g.numbers.length && g.goalsFeed === 'ok') { p.font(F.small); p.text(S.noNumbers, PAD, y + F.small, p.pal.dim, max); y += lh(F.small) + 16 }
  if (v.bot.needs.length) {
    y = section(p, S.stateNeeds, y)
    for (const n of v.bot.needs) y = needBlock(p, S, n, y, max, true) + 4
  }
  if (g.next) { y = section(p, S.onNowLabel, y); y = crewPara(p, g.next, PAD, y, max, F.body, 2) + 12 }
  if (g.goals.length) {
    y = section(p, S.goals, y)
    for (const goal of g.goals) y = crewPara(p, goal, PAD, y, max, F.body, 3) + 6
    y += 8
  }
  if (g.reports.length) {
    y = section(p, S.reports, y)
    for (const r of g.reports) {
      p.font(F.small); const tw = p.text(ago(r.at, v.now), PAD + max, y + F.body, p.pal.faint, 220, 'right')
      p.font(F.body); p.text(crewText(r.title), PAD, y + F.body, p.pal.ink, max - tw - 20)
      y += lh(F.body) + 4
    }
    y += 8
  }
  // The Bot's own page is free-form HTML. It opens in the dashboard, never here.
  p.box(PAD - 12, y + 6, max + 24, 152, 20, p.pal.raised, p.pal.edge)
  p.font(F.small)
  p.text(g.page === 'empty' ? S.noPage : g.page === 'stale' ? S.pageStale : g.page === 'error' ? S.feedFailed : S.leavesXr, PAD + 8, y + 22 + F.small, g.page === 'error' ? p.pal.danger : p.pal.dim, max - 16)
  p.btn(`leave:dash:${v.bot.slug}`, S.openFull, PAD + 8, y + 70, 420, 70, { icon: 'open', px: F.body })
  y += 180
  p.scrollEnd()
  return Math.max(0, y - top - viewH)
}

function paintWork(p: Painter, S: XrStrings, v: FullView): number {
  const max = p.w - PAD * 2
  const top = BODY_Y
  const viewH = p.h - top - 30
  p.scrollBegin(top, viewH, v.scroll)
  let y = top + 8
  const bot = v.bot
  if (bot) {
    if (!bot.things.length) { p.font(F.body); p.text(stateWord(S, 'idle'), PAD, y + F.body, p.pal.dim); y += 60 }
    // All of the Bot's work, grouped by how it stands. The list scrolls; nothing is dropped.
    for (const state of ['needs', 'failed', 'working', 'idle'] as const) {
      const group = bot.things.filter(t => t.state === state)
      if (!group.length) continue
      y = section(p, `${stateWord(S, state)} · ${group.length}`, y + 4)
      for (const t of group) y = thingRow(p, S, t, y, max, v.now, v.out.has(`thing:${t.slot}`))
    }
  }
  p.scrollEnd()
  return Math.max(0, y - top - viewH)
}

function paintHub(p: Painter, S: XrStrings, v: FullView): number {
  const max = p.w - PAD * 2
  const top = HEAD_H + 12
  const viewH = p.h - top - 30
  const f = v.crew.feeds
  p.scrollBegin(top, viewH, v.scroll)
  let y = top + 8
  if (!f.live) { p.font(F.small, { bold: true }); p.text(S.stale, PAD, y + F.small, p.pal.warn, max); y += lh(F.small) + 10 }
  y = section(p, `${S.approvals} · ${f.asks === 'failed' ? '?' : v.crew.needs.length}`, y)
  y = feedNote(p, S, f.asks, PAD, y, F.small, max)
  if (!v.crew.needs.length && f.asks === 'ok') { p.font(F.body); p.text(S.needsNone, PAD, y + F.body, p.pal.dim); y += lh(F.body) + 14 }
  for (const n of v.crew.needs) {
    y = needBlock(p, S, n, y, max, true)
    p.box(PAD, y - 6, max, 2, 1, p.pal.edge)
    y += 10
  }
  if (v.crew.unowned) { p.font(F.small); p.text(S.unowned(v.crew.unowned), PAD, y + F.small, p.pal.dim, max); y += lh(F.small) + 14 }
  // Every Bot by name, so one that is not in view can still be reached.
  y = section(p, `${S.restBots} · ${v.crew.bots.length}`, y + 6)
  y = feedNote(p, S, f.roster, PAD, y, F.small, max)
  for (const b of v.crew.bots) {
    const hot = p.state.hot === `bot:${b.slug}`
    p.box(PAD - 12, y, max + 24, lh(F.body) + 14, 16, hot ? p.pal.raised : null, null)
    dot(p, PAD + 8, y + 24, stateTone[b.state], b.state !== 'idle')
    p.font(F.body, { bold: true })
    const lw = p.text(crewText(b.label), PAD + 34, y + F.body + 6, p.pal.ink, max * 0.5)
    p.font(F.small)
    p.text(b.needs.length ? S.needsYou(b.needs.length) : stateWord(S, b.state), PAD + 50 + lw, y + F.body + 6, p.tone(stateTone[b.state]), max - lw - 60)
    p.hit(`bot:${b.slug}`, PAD - 12, y, max + 24, lh(F.body) + 14)
    y += lh(F.body) + 18
  }
  y = section(p, S.notifications, y + 10)
  if (!v.notices.length) { p.font(F.body); p.text('—', PAD, y + F.body, p.pal.dim); y += lh(F.body) + 8 }
  for (const n of v.notices) {
    p.font(F.small); const tw = n.ts ? p.text(ago(n.ts, v.now), PAD + max, y + F.body, p.pal.faint, 220, 'right') : 0
    p.font(F.body, { bold: true }); p.text(crewText(n.title), PAD, y + F.body, p.pal.ink, max - tw - 20)
    y += lh(F.body)
    if (n.body) y = crewPara(p, n.body, PAD, y + 4, max, F.small, 2)
    y += 12
  }
  y = section(p, S.rest, y + 6)
  p.font(F.small); p.text(S.leavesXr, PAD, y + F.small - 4, p.pal.faint, max); y += lh(F.small) + 6
  const rest: [string, string, string][] = [['leave:chat', S.restChat, 'chat'], ['leave:bots', S.restBots, 'full'], ['leave:notes', S.restNotes, 'next']]
  const bw = (max - 28) / 3
  rest.forEach(([id, label, icon], i) => p.btn(id, label, PAD + i * (bw + 14), y, bw, 70, { icon, px: F.small }))
  y += 100
  p.scrollEnd()
  toast(p, v.toast, p.h - 100)
  return Math.max(0, y - top - viewH)
}

/** Draw one full panel. Returns how far it can scroll. */
export function paintFull(p: Painter, S: XrStrings, v: FullView): number {
  chrome(p, S, v)
  const max = v.kind === 'chat' ? paintChat(p, S, v) : v.kind === 'dash' ? paintDash(p, S, v) : v.kind === 'work' ? paintWork(p, S, v) : paintHub(p, S, v)
  if (v.kind !== 'chat' && v.kind !== 'hub') toast(p, v.toast, p.h - 100)
  p.icon('resize', p.w - 64, p.h - 64, 38, p.state.hot === 'resize' ? 1 : 0.5)
  return max
}
