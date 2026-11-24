/**
 * A pinch on a Bot at its desk, in a session, brings what it has (D77): ONE sheet, into his hand, where he faces, by
 * the held board's own place rules (`Board.tsx`, D71, D76). Which sheet is the Bot's state's:
 *
 * - it asks: its board, the one that stands at it;
 * - it failed: its last words and why it stopped; it finished and he has not read it: what it finished; it works:
 *   what it does now (`useSays`). These are raised at the Bot by the desk's tap and taken as they come, within
 *   `WORDS_WAIT_MS`, as a failed Bot's name on Home's line takes them (`useHomeNames`);
 * - it has nothing to say: no sheet; its page opens beside the page he reads, by the path a line of Home uses.
 *
 * A Bot whose sheet is in his hand already gets nothing new, and the line says so. A Bot that walks, or that he
 * carries, gives no sheet for that pinch. Outside a session a pinch is the desk's tap as before.
 */
import { useCallback, useEffect, useRef, type MutableRefObject } from 'react'
import { i18nT } from '../../../i18n/t'
import type { XrBot } from '../model'
import { heldFull } from './board'
import { hasSheet } from './useSays'
import { WORDS_WAIT_MS } from './useHomeNames'

/** What the row reports of a Bot's body (`Crew.tsx`): a press carries it, or it walks (at the desk, `walking`). */
type Body = { carried?: boolean; walking?: boolean }
type View = { bot: string; needId: string; where: 'bot' | 'held' | 'floor'; kind?: string; writing?: boolean }

/** What a pinch on a Bot does now: take the sheet at it, raise one and take it, say it is in his hand, open its page, or nothing. */
export type BotSheetDoes = 'take' | 'raise' | 'held' | 'page' | 'none'

export function botSheetDoes(bot: Pick<XrBot, 'slug' | 'state' | 'rest' | 'cuts' | 'needs'> | undefined, views: readonly View[], body: Body | undefined, again = false): BotSheetDoes {
  if (!bot || body?.carried || body?.walking) return 'none'
  const own = views.filter(v => v.bot === bot.slug && v.kind !== 'speak')
  if (own.some(v => v.where === 'held')) return 'held'
  // Two boards in his hand that he writes on: a take does nothing (D79 2).
  if (heldFull(views as never)) return 'none'
  if (own.some(v => v.where === 'bot')) return 'take'
  // `again`: he took its sheet here before and let it go; read, it is new no more, and a pinch still brings it (D82 2).
  if (bot.needs.length || hasSheet(bot) || again) return 'raise'
  return 'page'
}

interface Inputs {
  crew: readonly XrBot[]
  views: readonly View[]
  inSession: boolean
  body: (slug: string) => Body | undefined
  take: (needId: string) => boolean
  /** The desk's tap (`Workspace.tapBot`): it raises the Bot's words, or brings its ask's panel. */
  tap: (slug: string) => void
  openBot: (slug: string) => void
  /** One line, on the Bot's own plate and over the canvas. */
  say: (slug: string, words: string) => void
  /** When a pointer last went down anywhere: a sheet that comes after he pressed something else is not taken. */
  anyDownAt: MutableRefObject<number>
}

export function useBotSheet({ crew, views, inSession, body, take, tap, openBot, say, anyDownAt }: Inputs) {
  const viewsNow = useRef(views)
  viewsNow.current = views
  const crewNow = useRef(crew)
  crewNow.current = crew
  /** The Bot whose sheet is on its way: taken as it stands at the Bot, within `WORDS_WAIT_MS`. */
  const waits = useRef<{ slug: string; at: number; asks: ReadonlySet<string>; timer: ReturnType<typeof setTimeout> } | null>(null)
  const endWait = useCallback(() => { const w = waits.current; waits.current = null; if (w) clearTimeout(w.timer) }, [])
  useEffect(() => endWait, [endWait])
  useEffect(() => { endWait() }, [inSession, endWait])
  /** The Bots whose words he took into his hand here: each brings them again, read or not. */
  const brought = useRef(new Set<string>())
  for (const v of views) if (v.where === 'held' && v.kind === 'words') brought.current.add(v.bot)
  /** For the probe: the last pinch on a Bot and what it did. */
  const last = useRef<{ slug: string; does: BotSheetDoes } | null>(null)
  const atBot = (slug: string) => viewsNow.current.find(v => v.bot === slug && v.where === 'bot' && v.kind !== 'speak')
  const press = useCallback((slug: string) => {
    if (!inSession) { tap(slug); return }
    const bot = crewNow.current.find(b => b.slug === slug)
    const does = botSheetDoes(bot, viewsNow.current, body(slug), brought.current.has(slug))
    last.current = { slug, does }
    if (does === 'none') return
    endWait()
    if (does === 'held') { say(slug, i18nT('xr.sheet_in_hand', { name: bot?.label ?? slug })); return }
    if (does === 'page') { openBot(slug); return }
    const board = atBot(slug)
    if (board) { take(board.needId); return }
    tap(slug)
    // Taken a moment after the pointer went down for this pinch: a later down is another press.
    const at = performance.now() + 1
    // Only what this pinch raised is taken (D83 6): the board of an ask it had then, or its words; never a new ask.
    waits.current = { slug, at, asks: new Set(bot?.needs.map(n => n.id) ?? []), timer: setTimeout(endWait, WORDS_WAIT_MS) }
  }, [inSession, body, take, tap, openBot, say, endWait])
  useEffect(() => {
    const w = waits.current
    if (!w) return
    if (anyDownAt.current > w.at) { endWait(); return }
    const board = atBot(w.slug)
    if (board && (board.kind === 'words' || w.asks.has(board.needId))) { endWait(); take(board.needId) }
  }, [views, anyDownAt, endWait, take])
  return { press, last: () => last.current }
}
