/**
 * A Bot's words as sheets on its clipboard (D19, D26 (2); `iterations/iter-15/TALK-PLAN.md`, slice 1). A Bot speaks on
 * paper: no chat page comes up for what it said. Plain rules with no clock and nothing drawn; `useSays.ts` reads the
 * Bot's own thread and `useBoards.ts` holds the board up.
 *
 * - Only the Bot's own words are read: a message whose role is the Bot's (`assistant`), never his own last message.
 * - An answer is what the Bot said in one turn, its code blocks and tables taken out and counted: a sheet never shows
 *   them cut or reflowed, they are read on the real page.
 * - Sheet 1 has the first sentence large, then the words after it; each sheet has `SAYS.lines` lines at most, and an
 *   answer `SAYS.sheets` sheets of words at most. An answer with more, or with code or a table, ends on a sheet that
 *   counts what is left out and has the one mark that opens the Bot's page.
 * - After the newest answer's sheets come the sheets of the turn before, and then the first sheet again.
 */
import type { ChatMessage } from '../../../types'
import { firstLine, type XrCut, type XrRest } from '../model'
import { plainText } from '../text'

export const SAYS = {
  /** Lines on a sheet, the first sentence's own included. */
  lines: 8,
  /** Sheets of words of one answer; what is past them is counted on the sheet after. */
  sheets: 5,
  /** Answers on the clipboard: the newest and the turn before. */
  answers: 2,
  /** Messages read of the Bot's own thread. */
  read: 40,
  /** Lines the first sentence takes at most on the sheet in his hand. */
  headLines: 3,
  /** Lines of an answer's last paragraph on its count sheet. */
  tailLines: 3,
  /** The choices a sheet shows as boxes, at most, and the lines a box takes. */
  choices: 4,
  boxRows: 2,
} as const

/** One turn's words of the Bot. `key` stands for the turn's last message of the Bot, so a new answer is a new key. */
export interface Answer {
  key: string
  first: string
  rest: string
  code: number
  tables: number
  headings?: readonly string[]
  /** The choices the Bot offers him at the answer's end (`[OPTIONS: a | b]`): never cut away without a word. */
  choices?: readonly string[]
}

/** What is left out of an answer, counted. */
export interface Left { words: number; tables: number; code: number }

/** One sheet. `n` of `of` within its answer; `before`: the sheet is of the turn before. */
export interface Sheet {
  head: string[]
  body: string[]
  n: number
  of: number
  before: boolean
  left: Left | null
  /** On the sheet where the answer ends: the choices it offers him, as boxes that the stamp marks (as a question's). */
  choices?: readonly string[]
  /** On the count sheet of an answer that the cap cut: its last paragraph, which is often its question to him. */
  tail?: string[]
  /** On the first sheet of a turn that ended (P3, the done sheet): its head, what it links and what it left open. */
  done?: DoneFoot
}

/**
 * What a done sheet says besides the answer (`iterations/iter-15/DONE-PLAN.md`), every line from a field the gateway
 * sends: the head ("Juno: done · 10:42"), a line for each change or issue it links in that one's state, and the todo
 * tasks it left open. A PR's line opens nothing: he reads it on the paper (D35, the chip).
 */
export interface DoneFoot {
  head: string
  /** The one line a Bot at a desk far off holds up: "done · 10:42" (its name is on its sign). */
  short: string
  made: readonly { text: string; state: string }[]
  left: readonly string[]
  /** The sheet of a Bot that works now (D83 4): it takes no note; its foot opens the Bot's page. */
  working?: boolean
  /** Its words could not be read (D92 1): the sheet's foot also opens the Bot's page. */
  pageFoot?: boolean
  /**
   * A failed sheet (slice 2): the session was cut. Its paper has a red edge, its head says "stopped", and its one box
   * ("Try again") picks the cut turn up again (`writes.cutRetry`). `cut` is the cut's own id.
   */
  failed?: { cut: string }
}

/** The done sheet's most lines of what it links, and of what it left open. */
export const DONE = { made: 2, left: 2 } as const

/** The body rows the foot of a done sheet takes on its first sheet: its head, its links, and its open tasks. */
export const doneRows = (d: DoneFoot): number => 1 + d.made.length + d.left.length

/**
 * A turn that only says "ok" is a plain words board (D35): its whole answer is one short sentence, with nothing more,
 * no code and no table, and the session links nothing and left nothing open.
 */
export function onlyAck(a: Answer, rest: { made: readonly unknown[]; left: readonly unknown[] }): boolean {
  if (a.rest || a.code || a.tables || a.choices?.length || rest.made.length || rest.left.length) return false
  const words = a.first.trim().split(/\s+/).filter(Boolean)
  return words.length === 1 ? [...words[0]].length <= 6 : words.length <= 3
}

const FENCE = /```[\s\S]*?(?:```|$)/g
const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/

/** A message's own text with its code blocks and tables taken out, and how many of each there were. */
export function proseOf(markdown: string): { prose: string; code: number; tables: number; headings: string[]; choices: string[] } {
  const code = (markdown.match(FENCE) ?? []).length
  // The choices are read before the text is made plain: `plainText` takes `[OPTIONS: …]` out.
  const choices = [...markdown.replace(FENCE, '\n').matchAll(/\[OPTIONS:\s*([^\]]*)\]/g)].flatMap(m => m[1].split('|').map(c => c.trim()).filter(Boolean))
  const lines = markdown.replace(FENCE, '\n').split('\n')
  const kept: string[] = []
  let tables = 0
  const headings: string[] = []
  for (let i = 0; i < lines.length; i += 1) {
    // A table is a row of cells over its rule line; the rows after it go with it.
    if (/^\s*\|/.test(lines[i]) && TABLE_RULE.test(lines[i + 1] ?? '')) {
      tables += 1
      i += 1
      while (i + 1 < lines.length && /^\s*\|/.test(lines[i + 1])) i += 1
      continue
    }
    const heading = /^\s{0,3}#{1,6}\s+(.+)$/.exec(lines[i])
    if (heading) headings.push(plainText(heading[1]).replace(/`([^`\n]+)`/g, '$1').trim())
    kept.push(lines[i])
  }
  // Inline code is read as its words: on paper the backticks are noise.
  const prose = plainText(kept.join('\n')).replace(/`([^`\n]+)`/g, '$1').replace(/\n{3,}/g, '\n\n').trim()
  return { prose, code, tables, headings, choices }
}

/** Whether the thread's newest message is the Bot's: after his own message the Bot has not answered yet. */
export function botSpokeLast(messages: readonly ChatMessage[]): boolean {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i]
    if (m.role === 'user') return false
    if (m.role === 'assistant' && (m.rawText ?? m.content)?.trim()) return true
  }
  return false
}

/** The Bot's answers, newest first, at most `most`: each turn's messages of the Bot, as one. */
export function answersOf(messages: readonly ChatMessage[], most: number = SAYS.answers): Answer[] {
  const turns: { parts: string[]; key: string }[] = []
  let open = false
  messages.forEach((m, i) => {
    if (m.role === 'user') { open = false; return }
    if (m.role !== 'assistant') return
    const text = m.rawText ?? m.content
    if (!text?.trim()) return
    if (!open) { turns.push({ parts: [], key: '' }); open = true }
    const turn = turns[turns.length - 1]
    turn.parts.push(text)
    turn.key = `${m.ts ?? ''}|${i}`
  })
  return turns.slice(-most).reverse().map(t => {
    const { prose, code, tables, headings, choices } = proseOf(t.parts.join('\n\n'))
    const first = firstLine(prose)
    const at = first ? prose.indexOf(first) : -1
    const rest = at >= 0 ? prose.slice(at + first.length).trim() : prose
    return { key: t.key, first: first || prose.slice(0, 140), rest, code, tables, headings, choices }
  })
}

const wordCount = (lines: readonly string[]) => lines.join(' ').split(/\s+/).filter(Boolean).length
/** A line that ends a sentence (or a clause he can stop at): it is not cut. */
const SENTENCE_END = /[.!?:;。！？：；…]["'”’)\]]*$/u

/**
 * The sheets of the answers, in the order the corner turns them. `wrap` breaks a text into the lines the picture
 * draws: the first sentence in its own size (`head`), the rest in the body's (`body`).
 */
export function sheetsOf(answers: readonly Answer[], wrap: { head: (s: string) => string[]; body: (s: string) => string[] }, done?: DoneFoot): Sheet[] {
  const out: Sheet[] = []
  answers.forEach((a, k) => {
    // The newest answer's first sheet keeps rows for the done sheet's foot.
    const foot = k === 0 && done ? done : undefined
    const head = wrap.head(a.first).slice(0, SAYS.headLines)
    const body = wrap.body(a.rest).filter((line, i, all) => line.trim() || (i > 0 && all[i - 1].trim()))
    while (body.length && !body[0].trim()) body.shift()
    const headings = new Set(a.headings ?? [])
    /**
     * The next `n` lines of the words. A heading never stands as a sheet's last line: it goes to the next sheet with
     * at least one line of its words (and so does a blank line before it), unless it is all the sheet would hold.
     */
    const take = (n: number) => {
      const lines = body.splice(0, n)
      const ends = (l: string) => !l.trim() || headings.has(l.trim())
      let cut = lines.length
      while (body.length && cut > 1 && ends(lines[cut - 1])) cut -= 1
      if (cut < lines.length && lines.slice(0, cut).some(l => l.trim())) body.unshift(...lines.splice(cut))
      // A sentence that goes on to the next sheet ends this one with an ellipsis: a line never looks finished when
      // it is cut (the lead's 12:42).
      const last = lines.length - 1
      if (last >= 0 && lines[last].trim() && body[0]?.trim() && !SENTENCE_END.test(lines[last].trim())) lines[last] = lines[last].trimEnd().concat('\u2026')
      return lines
    }
    const pages: { head: string[]; body: string[] }[] = [{ head, body: take(Math.max(0, SAYS.lines - head.length - (foot ? doneRows(foot) : 0))) }]
    // A sheet never starts with the blank line between two paragraphs.
    const trim = () => { while (body.length && !body[0].trim()) body.shift() }
    trim()
    while (body.length && pages.length < SAYS.sheets) { pages.push({ head: [], body: take(SAYS.lines) }); trim() }
    // The cap cut the answer: its last paragraph goes on the count sheet, above the counts, as it is often its
    // question to him.
    const cutOff = body.some(l => l.trim())
    const last = cutOff ? a.rest.split(/\n\s*\n/).map(p => p.trim()).filter(Boolean).at(-1) ?? '' : ''
    const tail = last ? wrap.body(last).slice(-SAYS.tailLines) : []
    const left: Left = { words: Math.max(0, wordCount(body) - wordCount(tail)), tables: a.tables, code: a.code }
    const counts = left.words > 0 || left.tables > 0 || left.code > 0
    const choices = a.choices?.length ? a.choices.slice(0, SAYS.choices) : undefined
    const lastPage = pages[pages.length - 1]
    const end = counts || tail.length > 0
    // The choices are boxes on the sheet where the answer ends (each takes `SAYS.boxRows` lines): its last sheet of
    // words or its count sheet when they fit there, else a sheet of their own after it.
    const rows = (choices?.length ?? 0) * SAYS.boxRows
    const countRows = [left.words, left.tables, left.code].filter(n => n > 0).length + (counts ? 2 : 0)
    const onLast = !!choices && !end && lastPage.head.length + lastPage.body.length + (foot && pages.length === 1 ? doneRows(foot) : 0) + rows <= SAYS.lines
    const onEnd = !!choices && end && tail.length + countRows + rows <= SAYS.lines
    const own = !!choices && !onLast && !onEnd
    const of = pages.length + (end ? 1 : 0) + (own ? 1 : 0)
    pages.forEach((p, i) => out.push({ ...p, n: i + 1, of, before: k > 0, left: null, ...(onLast && i === pages.length - 1 ? { choices } : {}), ...(foot && i === 0 ? { done: foot } : {}) }))
    if (end) out.push({ head: [], body: [], n: pages.length + 1, of, before: k > 0, left: counts ? left : null, ...(tail.length ? { tail } : {}), ...(onEnd ? { choices } : {}) })
    if (own) out.push({ head: [], body: [], n: of, of, before: k > 0, left: null, choices })
  })
  return out
}

/** The sheet after `sheet` when the corner turns: the next one, and after the last the first again. */
export const nextSheet = (sheet: number, count: number): number => (count > 0 ? (sheet + 1) % count : 0)

/**
 * What a Bot's clipboard says of its words now. `raise`: the board is held up by itself now (T1: the Bot's own
 * thread has words of its own he has not read, and the Bot is not working), or for a tap on the Bot (T3).
 * `failed`: the thread could not be read; the board says so (T10), never an empty sheet.
 */
export interface Says {
  /** The board's id: one words board for each Bot, of its own thread. */
  id: string
  /** The newest answer's key: a new answer is a new version. */
  version: string
  slot: string
  raise: boolean
  state: 'ok' | 'none' | 'failed'
  /** The first sentence of the newest answer, as the board at the Bot shows it. */
  sentence: string
  sheets: Sheet[]
  /** A turn that ended (P3): the done sheet's head and foot, as the board at the Bot shows them too; else none. */
  done?: DoneFoot
}

/**
 * The cut session a Bot holds a failed sheet up for (P3 slice 2, D35): the oldest of its cuts whose rim is red
 * (`rims.rimOf`) and that he did not file or hand back here (`put`, by the cut's own id: a new cut of the session is
 * a new sheet). A failure goes before its words. A Bot that works holds none; null for none.
 */
export function cutToRaise(bot: { state: string; cuts: readonly Pick<XrCut, 'slot' | 'id' | 'red' | 'ts'>[] }, put: ReadonlySet<string> = new Set()): Pick<XrCut, 'slot' | 'id'> | null {
  if (bot.state === 'working') return null
  let pick: Pick<XrCut, 'slot' | 'id' | 'ts'> | null = null
  for (const c of bot.cuts) if (c.red && !put.has(c.id) && (!pick || (c.ts ?? 0) < (pick.ts ?? 0))) pick = c
  return pick ? { slot: pick.slot, id: pick.id } : null
}

/**
 * What a failed sheet says of its error row (the lead's 15:28): when the text holds a `message`, that message is the
 * large line and its kind and code the small line under it ("Prompt error · -32603"); else the text as it is. No
 * word is rewritten or guessed.
 */
export function errorWords(text: string): { line: string; small: string } {
  const flat = text.replace(/\s+/g, ' ').trim()
  const msg = /["']message["']\s*:\s*(["'])((?:\\.|(?!\1).)*)\1/.exec(flat)
  if (!msg) return { line: flat, small: '' }
  const brace = flat.indexOf('{')
  const kind = (brace > 0 ? flat.slice(0, brace) : '').replace(/[\s:]+$/, '')
  const code = /["']code["']\s*:\s*(-?\d+)/.exec(flat)?.[1] ?? ''
  return { line: msg[2].replace(/\\(["'\\])/g, '$1'), small: [kind, code].filter(Boolean).join(' · ') }
}

/** At most `most` wrapped lines; when more were cut away, the last one kept ends with an ellipsis. */
export function capLines(lines: readonly string[], most: number): string[] {
  if (lines.length <= most) return [...lines]
  const kept = lines.slice(0, most)
  kept[most - 1] = kept[most - 1].trimEnd().concat('\u2026')
  return kept
}

/**
 * Whether "Try again" on a failed sheet may still go (REVIEW-15 P3 M1, D39 ruling 5): the Bot still has the cut that
 * the sheet names and does not work. Picked up elsewhere meanwhile (Resume on his Mac, the gateway's own recovery),
 * the same route would start a new turn on a thread that had finished.
 */
export const retryStands = (cut: string, bot: { state: string; cuts: readonly { id: string }[] } | undefined): boolean => !!bot && bot.state !== 'working' && bot.cuts.some(c => c.id === cut)

/** A session of a Bot as its done sheet is picked from (`XrRest`). */
type Rest = Pick<XrRest, 'slot' | 'ts' | 'unread'> & { direct?: boolean }

/**
 * The session whose answer a Bot holds up by itself (P3, D35): of its own thread's sessions at rest (D39, ruling 2:
 * a worker's result is what its Bot says of it; a worker's failure still holds its failed sheet, `cutToRaise`), the
 * one he has not read that ended first, past those he handed back unread since they last changed
 * (`back`: the session's time at the hand-back). A Bot that is working holds up none; '' for none.
 */
export function sessionToRaise(bot: { state: string; rest: readonly Rest[] }, back: ReadonlyMap<string, number> = new Map()): string {
  if (bot.state === 'working') return ''
  let pick: Rest | null = null
  for (const r of bot.rest) if (r.direct !== false && r.unread && back.get(r.slot) !== r.ts && (!pick || r.ts < pick.ts)) pick = r
  return pick?.slot ?? ''
}

/** A tap's pin (P3 M3): the session that a tap holds up at its Bot, and the Bot's newest time when he tapped. */
export interface Pin { slot: string; at: number }

/**
 * Whether a tap still pins its Bot to one session (REVIEW-15 P3 M3): it ends once the Bot works (no sheet stands while
 * it does) or once a newer unread session of it comes. A red cut goes before a pin anyway (`useSays`).
 */
export function pinStands(pin: Pin, bot: { state: string; lastTs: number; rest: readonly Rest[] }, back: ReadonlyMap<string, number> = new Map()): boolean {
  if (bot.state === 'working') return false
  const next = sessionToRaise(bot, back)
  return !(next && next !== pin.slot && bot.lastTs > pin.at)
}

/**
 * Which session a Bot holds up, and whether by itself (`auto`) or for a tap, with the cut when it is a failed sheet. A
 * failure goes before its Bot's words (slice 2) and before a tap's pin (P3 M3): its cut session's sheet, the one he
 * has not put aside. A pin holds while `pinStands`. A tap is a press on the Bot, which dims its rim (`rims.ts`): the
 * cut it raises need not be red, only not old. Null: it holds up nothing.
 */
export function whatToRaise<C extends Pick<XrCut, 'slot' | 'id' | 'red' | 'ts' | 'stale'>>(b: { state: string; lastTs: number; rest: readonly Rest[]; cuts: readonly C[] }, pin: Pin | undefined, put: ReadonlySet<string>, back: ReadonlyMap<string, number>): { slot: string; auto: boolean; cut: C | null } | null {
  const pinned = pin && pinStands(pin, b, back) ? pin.slot : undefined
  const red = cutToRaise(b, put)
  const slot = red?.slot ?? pinned ?? sessionToRaise(b, back)
  if (!slot) return null
  const cut = red ? b.cuts.find(c => c.id === red.id) ?? null : pinned ? b.cuts.find(c => c.slot === pinned && !c.stale) ?? null : null
  return { slot, auto: !(pinned && slot === pinned), cut }
}

/** How many answers wait to be read in the room (Home's "2 to read"): every unread session at rest of a Bot's own thread (D39). */
export const toRead = (bots: readonly { rest: readonly Rest[] }[]): number => bots.reduce((n, b) => n + b.rest.filter(r => r.unread && r.direct !== false).length, 0)

/** The session a words board reads, from its id (`saysId`). */
export const slotOfSays = (id: string): string => (id.startsWith('w:') ? id.slice(2) : '')

export const saysId = (slot: string) => `w:${slot}`

/**
 * What reading the answer `version` (an answer's `key`: its message's time, then its place) marks read: the thread up
 * to that message's time, and here too (`local`) only when no newer message came since (`slotLastTs`, the thread's
 * newest). `null` for a board that showed no answer (a failed read, a Bot that said nothing): nothing was read.
 */
export function readMark(version: string, slotLastTs: string | undefined): { ts: string; local: boolean } | null {
  const ts = version.includes('|') ? version.split('|')[0] : ''
  if (!ts || Number.isNaN(Date.parse(ts))) return null
  const newest = slotLastTs ? Date.parse(slotLastTs) : NaN
  return { ts, local: Number.isNaN(newest) || Date.parse(ts) >= newest }
}



/** What a working Bot does now (`useSays.workingSheet`, D77) is versioned by this: it is no answer, so no take reads it. */
export const WORKING_VERSION = 'now:'
export const isWorkingSheet = (version: string): boolean => version.startsWith(WORKING_VERSION)
