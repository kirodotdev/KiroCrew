/**
 * What each Bot's clipboard says of its words (`says.ts`): the read of one session of the Bot, the sheets it is cut
 * into, and when the Bot holds them up. A Bot holds them up by itself when a session of it (its own thread, or a worker
 * it handed work to) ended a turn with words he has not read and the Bot is not working (T1, P3's done sheet), and for
 * his tap on it (T3). Thrown in XR, the session is marked read as the dashboard marks a thread that he read
 * (`markSlotRead`, `emitSlotRead`); handed back, it stays unread and is not held up again until it changes (D35).
 */
import { keepPreviousData, useQueries } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../../api/client'
import { fmtTime } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { emitSlotRead } from '../../../lib/slotReadRelay'
import { useAppDispatch, useAppStore } from '../../../store'
import { markSlotRead } from '../../../store/dashboardSlice'
import { selectSlotLastTs } from '../../../store/slotLastTs'
import { firstLine, type XrBot, type XrCut, type XrMade, type XrRest } from '../model'
import type { ChatMessage } from '../../../types'
import { sheetWrap } from './boardPicture'
import { DONE, SAYS, WORKING_VERSION, answersOf, botSpokeLast, capLines, errorWords, onlyAck, pinStands, proseOf, readMark, saysId, sessionToRaise, sheetsOf, whatToRaise, type Answer, type DoneFoot, type Pin, type Says, type Sheet } from './says'

/** A sheet of one line, for a board that has no words to show: the line says why. */
const oneLine = (text: string): Sheet[] => [{ head: sheetWrap().head(text).slice(0, SAYS.headLines), body: [], n: 1, of: 1, before: false, left: null }]

const STATE = { open: 'xr.done_state_open', draft: 'xr.done_state_draft', merged: 'xr.done_state_merged', closed: 'xr.done_state_closed' } as const
const CI = { running: 'xr.done_ci_running', passed: 'xr.done_ci_passed', failed: 'xr.done_ci_failed' } as const

/** A change's or an issue's line: "PR #7 · open · CI passed", with only the parts the gateway reported. */
export function madeLine(m: XrMade): string {
  const state = STATE[m.state as keyof typeof STATE]
  const ci = CI[m.ci as keyof typeof CI]
  return [i18nT(m.kind === 'issue' ? 'xr.done_issue' : 'xr.done_change', { number: m.number }), state && i18nT(state), ci && i18nT(ci)].filter(Boolean).join(' \u00B7 ')
}

/** The done sheet's head and foot for a session at rest, or none for a turn that only says "ok" (D35). */
function doneFoot(name: string, answer: Answer, rest: XrRest | undefined): DoneFoot | undefined {
  if (!rest || onlyAck(answer, rest)) return undefined
  const at = Date.parse(answer.key.split('|')[0])
  const time = fmtTime(Number.isNaN(at) ? rest.ts * 1000 : at)
  return {
    head: i18nT('xr.done_head', { name, time }),
    short: i18nT('xr.done_short', { time }),
    made: rest.made.slice(0, DONE.made).map(m => ({ text: madeLine(m), state: m.state })),
    left: rest.left.slice(0, DONE.left).map((task, i) => (i === 0 ? i18nT('xr.done_left', { task }) : task)),
  }
}

/** The error row of a cut session's last turn, as the sheet says it: its first sentence and its time; none when absent. */
function errorOf(messages: readonly ChatMessage[]): { text: string; ts: string } | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i]
    // The row's own words, whole, with the warning sign the chat draws before them left off: on paper it is a blank.
    if (m.role === 'error') return { text: m.content.replace(/^[\s\p{Extended_Pictographic}\uFE0F]+/u, ''), ts: m.ts ?? '' }
    if (m.role === 'user') return null
  }
  return null
}

/**
 * The failed sheet of a cut session (P3 slice 2, D35): its head ("Juno: stopped · 10:42"), the error row's own words
 * (`errorWords`: its message large, its kind and code small; or a plain line when the turn left none), what it was doing (its first open todo task), and one box, "Try again".
 */
function failedSheet(name: string, cut: Pick<XrCut, 'id' | 'doing' | 'ts'>, error: { text: string; ts: string } | null, unread = false): { sentence: string; sheets: Sheet[]; done: DoneFoot } {
  const at = Date.parse(error?.ts ?? '')
  const time = fmtTime(Number.isNaN(at) ? (cut.ts ?? 0) * 1000 : at)
  // What failed, large: the error's message when its text holds one, its kind and code small under it (the lead's 15:28).
  const words = errorWords(error?.text ?? '')
  // `unread`: the session could not be read (D92 1). The sheet still comes, says so, and its foot opens the Bot's page.
  const what = unread ? i18nT('xr.fail_unread', { name }) : words.line || i18nT('xr.fail_cut')
  const done: DoneFoot = {
    head: i18nT('xr.fail_head', { name, time }),
    short: i18nT('xr.fail_short', { time }),
    made: [],
    left: cut.doing ? [i18nT('xr.fail_doing', { task: cut.doing })] : [],
    failed: { cut: cut.id },
    ...(unread ? { pageFoot: true } : {}),
  }
  return { sentence: what, done, sheets: [{ head: capLines(sheetWrap().head(what), SAYS.headLines), body: words.small ? [words.small] : [], n: 1, of: 1, before: false, left: null, choices: [i18nT('xr.fail_try')], done }] }
}

/**
 * `filedFailed`: he filed a Bot's failed sheet (thrown), so whoever else counts that failure counts it no more: the
 * line of what changed since he left (`useSince.tapped`, the lead's 16:14: one read of a sheet clears both).
 */
/**
 * The sample crew's stand-in for the two things a real crew goes to the gateway for (`fixtureCrew.ts`): a session's
 * messages, in the shape `GET /api/chat/slots/{slot}` gives them, and the mark that he read a session.
 */
export interface SampleWords {
  /** With none, the session is read from the gateway as a real crew's is (a row holds that request back or refuses it). */
  messages?: (slot: string) => ChatMessage[]
  onRead: (slot: string) => void
  /** Only a Bot he pinched is read: nothing is raised by itself (the sample with no words, whose reads fail). */
  onlyTapped?: boolean
}

/**
 * What a working Bot does now, as a sheet (D77, D83 3): its head ("Otto: working") and its own newest words, read
 * from its session as every other sheet is (`GET /api/chat/slots/{slot}`). It has no note line; its foot opens its page.
 */
/** What a working Bot said in the turn that runs, and what started that turn, from its session's messages. */
export interface NowWords { own: string; started: { ts: string; line: string } | null }
export function nowWords(messages: readonly ChatMessage[]): NowWords {
  let from = -1
  messages.forEach((m, i) => { if (m.role === 'user') from = i })
  // Its words as every other sheet reads a Bot's words (`says.answersOf`): prose only, with no code, table, heading
  // or `[OPTIONS: ...]` line (M44).
  const parts = messages.slice(from + 1).filter(m => m.role === 'assistant' && (m.rawText ?? m.content)?.trim()).map(m => m.rawText ?? m.content ?? '')
  const prose = parts.length ? proseOf(parts[parts.length - 1]).prose : ''
  const first = firstLine(prose)
  // The body is the rest of the prose after its first sentence end or line end, the split `firstLine` makes (M48):
  // never cut out by looking for the head's own letters, which have lost their marks and may be clipped.
  const lead = prose.trimStart()
  const cutAt = lead.search(/(?<=[.!?。！？])\s+|\n+/)
  const rest = first && cutAt >= 0 ? lead.slice(cutAt).replace(/[*`#>]+/g, '').replace(/\s+/g, ' ').trim() : ''
  const start = from >= 0 ? messages[from] : null
  // What started the turn: its first line of prose, and only with a time that reads. A row that begins with "[" is
  // an automation's envelope or a channel's stamp, not words to show as what it was asked.
  const asked = start ? (start.rawText ?? start.content ?? '').trim() : ''
  const line = asked && !asked.startsWith('[') ? firstLine(proseOf(asked).prose) : ''
  const ts = start?.ts ?? ''
  return { own: [first, rest].filter(Boolean).join('\n'), started: line && !Number.isNaN(Date.parse(ts)) ? { ts, line } : null }
}

export function workingSheet(b: Pick<XrBot, 'slug' | 'label' | 'thread' | 'things' | 'lastTs'>, now: NowWords | null): Says {
  // Its own newest words of this turn (an assistant line). With none yet: when the turn started and the words that
  // started it, said as such. With no read: only that it works. Never the list's `last_message` as the Bot's words:
  // that is the newest of a user's or the Bot's (`slot_row_memo.py:170-189`).
  const own = (now?.own ?? '').split('\n').map(l => l.trim()).filter(Boolean)
  const at = Date.parse(now?.started?.ts ?? '')
  const started = !own.length && now?.started && !Number.isNaN(at) ? i18nT('xr.work_started', { time: fmtTime(at), line: now.started.line }) : ''
  const first = own[0] ?? started
  const done: DoneFoot = { head: i18nT('xr.work_head', { name: b.label }), short: i18nT('xr.home_working'), made: [], left: [], working: true }
  const wrap = sheetWrap()
  const slot = b.thread || b.things[0]?.slot || ''
  return {
    id: saysId(slot || `now-${b.slug}`), version: `${WORKING_VERSION}${b.lastTs}`, slot, raise: true, state: 'ok', sentence: first, done,
    sheets: [{ head: first ? capLines(wrap.head(first), SAYS.headLines) : [], body: own[1] ? capLines(wrap.body(own.slice(1).join(' ')), 2) : [], n: 1, of: 1, before: false, left: null, done }],
  }
}

/** Whether a pinch on the Bot has a sheet to bring (D77): it failed, it finished something he has not read, or it works. */
// `failed` as the rim draws it (`rims.rimOf`): a cut he has seen, or one the gateway calls old, is no failure to bring.
export const hasSheet = (b: Pick<XrBot, 'state' | 'rest'>): boolean =>
  b.state === 'working' || b.state === 'failed' || sessionToRaise(b) !== ''

/** How long after a pinch a failed Bot's words may be on their way before its sheet says they could not be read: inside the 6 s a name waits (`useHomeNames`, `WORDS_WAIT_MS`). */
export const SAYS_SLOW_MS = 4000

export function useSays(bots: readonly XrBot[], live: boolean, filedFailed?: (slug: string) => void, sample?: SampleWords) {
  /** The working Bots he pinched: each holds up what it does now, until he lets that sheet go or the Bot stops working. */
  const [nowOf, setNowOf] = useState<ReadonlySet<string>>(() => new Set())
  /** The Bots he tapped, and the session each holds up for it, until he throws or hands it back or the pin ends. */
  const [tapped, setTapped] = useState<ReadonlyMap<string, Pin>>(() => new Map())
  /** The answers he read here, by Bot and answer: raised no more by themselves, before the read state follows. */
  const [readHere, setReadHere] = useState<ReadonlySet<string>>(() => new Set())
  /** The sessions he handed back unread, and each one's time then: held up again only once it changed (D35). */
  const [back, setBack] = useState<ReadonlyMap<string, number>>(() => new Map())
  /** The cuts whose failed sheet he filed or handed back here, by the cut's own id: a new cut is a new sheet. */
  const [putCuts, setPutCuts] = useState<ReadonlySet<string>>(() => new Set())
  const botsNow = useRef(bots)
  botsNow.current = bots
  // A pin that ended is let go, so it does not come back when the Bot rests again.
  useEffect(() => {
    setTapped(was => {
      const left = [...was].filter(([slug, pin]) => { const b = bots.find(x => x.slug === slug); return !!b && pinStands(pin, b, back) })
      return left.length === was.size ? was : new Map(left)
    })
  }, [bots, back])
  /** The pinched Bots whose words were not there `SAYS_SLOW_MS` after the pinch. */
  const [slow, setSlow] = useState<ReadonlySet<string>>(() => new Set())
  const slowTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  const wanted = useMemo(() => bots.flatMap(b => {
    if (sample?.onlyTapped && !tapped.has(b.slug)) return []
    const r = whatToRaise(b, tapped.get(b.slug), putCuts, back)
    return r ? [{ b, ...r }] : []
  }), [bots, tapped, back, putCuts, sample])
  const reads = useQueries({
    queries: (live ? wanted : []).map(({ b, slot }) => ({
      // The Bot's newest time stands for its sessions changing: the sheets follow a new answer.
      queryKey: ['xr', 'says', slot, SAYS.read, b.lastTs],
      queryFn: async ({ signal }: { signal: AbortSignal }): Promise<{ answers: Answer[]; last: boolean; error: { text: string; ts: string } | null }> => {
        const messages = sample?.messages ? sample.messages(slot) : (await api.chatSlotDetail(slot, SAYS.read, undefined, signal)).messages
        return { answers: answersOf(messages), last: botSpokeLast(messages), error: errorOf(messages) }
      },
      // A new read of the thread keeps the sheets of the last one until it lands: a board never loses its sheets.
      placeholderData: keepPreviousData,
      retry: 1,
    })),
  })
  // The working Bots he pinched: each one's session is read for its own words of the turn that runs.
  const nowBots = useMemo(() => bots.filter(b => nowOf.has(b.slug) && b.state === 'working'), [bots, nowOf])
  const nowReads = useQueries({
    queries: (live ? nowBots : []).map(b => {
      const slot = b.thread || b.things[0]?.slot || ''
      return {
        queryKey: ['xr', 'says', 'now', slot, b.lastTs],
        queryFn: async ({ signal }: { signal: AbortSignal }): Promise<NowWords | null> => (slot ? nowWords(sample?.messages ? sample.messages(slot) : (await api.chatSlotDetail(slot, SAYS.read, undefined, signal)).messages) : null),
        placeholderData: keepPreviousData,
        retry: 1,
      }
    }),
  })
  // Settled reads only: the sheet comes once, with its words, and is taken as it stands.
  const nowData = nowReads.map(r => (r.isPending && !r.data ? undefined : r.data ?? null))
  const readData = reads.map(r => r.data)
  const readState = reads.map(r => (r.isError && (!r.data || r.isPlaceholderData) ? 'failed' : r.data ? 'ok' : 'loading')).join()
  /**
   * Each Bot's last whole read of a session, by Bot and session: while a new read loads (a new answer, or the Bot left
   * and came back to the list that is read), the board keeps the sheets it shows. The query's own kept data is by its
   * place in the list.
   */
  const lastGood = useRef(new Map<string, Says>())
  const says = useMemo(() => {
    const out = new Map<string, Says>()
    const good = new Map<string, Says>()
    const states = readState.split(',')
    wanted.forEach(({ b, slot, auto, cut }, i) => {
      const state = states[i]
      if (!live) return
      const keyOf = `${b.slug}|${slot}`
      if (state === 'loading' || state === undefined) {
        const was = lastGood.current.get(keyOf)
        if (was) { out.set(b.slug, was); good.set(keyOf, was) }
        // He pinched this failed Bot `SAYS_SLOW_MS` ago and its words are still on their way, or never come: the
        // sheet comes now and says they could not be read (D92 1). The words take its place at the Bot when they land.
        else if (cut && slow.has(b.slug)) { const f = failedSheet(b.label, cut, null, true); out.set(b.slug, { id: saysId(slot), version: `|${cut.id}`, slot, raise: true, state: 'failed', sentence: f.sentence, sheets: f.sheets, done: f.done }) }
        return
      }
      const answers = readData[i]?.answers ?? []
      // A cut session's sheet is its failed sheet, keyed by its error row's time and the cut's own id: a print on
      // its box is kept by that key, and filing it marks the session read up to that row (`readMark`).
      if (cut && state === 'ok') {
        const error = readData[i]?.error ?? null
        const f = failedSheet(b.label, cut, error)
        const s: Says = { id: saysId(slot), version: `${error?.ts ?? ''}|${cut.id}`, slot, raise: true, state: 'ok', sentence: f.sentence, sheets: f.sheets, done: f.done }
        out.set(b.slug, s)
        good.set(keyOf, s)
        return
      }
      // A cut session whose read failed: its failed sheet all the same, with no words of the error (D92 1).
      if (cut && state === 'failed') {
        const f = failedSheet(b.label, cut, null, true)
        const s: Says = { id: saysId(slot), version: `|${cut.id}`, slot, raise: true, state: 'failed', sentence: f.sentence, sheets: f.sheets, done: f.done }
        out.set(b.slug, s)
        good.set(keyOf, s)
        return
      }
      const version = state === 'failed' ? 'failed' : answers[0]?.key ?? 'none'
      // A tap holds the last words up even when they were read; by itself a Bot raises only what he has not read.
      // A read that failed cannot tell who spoke last: the board comes, and says it could not read them (T10).
      const spoke = state === 'failed' || !!readData[i]?.last
      const raise = !auto || (spoke && !readHere.has(`${b.slug}|${version}`))
      const base = { id: saysId(slot), version, slot, raise }
      let s: Says
      // A read that failed says so on the board (T10); a Bot that said nothing yet says that.
      if (state === 'failed') { const line = i18nT('xr.says_failed', { name: b.label }); s = { ...base, state: 'failed', sentence: line, sheets: oneLine(line) } }
      else if (!answers.length) { const line = i18nT('xr.says_none', { name: b.label }); s = { ...base, state: 'none', sentence: line, sheets: oneLine(line) } }
      else {
        // The newest answer of a session at rest is a done sheet (P3); while a turn runs it is the words as they were.
        const done = spoke ? doneFoot(b.label, answers[0], b.rest.find(r => r.slot === slot)) : undefined
        s = { ...base, state: 'ok', sentence: answers[0].first, sheets: sheetsOf(answers, sheetWrap(), done), ...(done ? { done } : {}) }
      }
      out.set(b.slug, s)
      good.set(keyOf, s)
    })
    lastGood.current = good
    // A working Bot he pinched holds up what it does now; a failure or an ask of it goes first.
    nowBots.forEach((b, i) => { const now = nowData[i]; if (live && now !== undefined && !out.has(b.slug)) out.set(b.slug, workingSheet(b, now)) })
    return out
    // The arrays are rebuilt each render; their items are the real inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, wanted, readHere, readState, nowBots, slow, ...readData, ...nowData])
  const saysNow = useRef(says)
  saysNow.current = says
  /** A failed sheet that he filed or handed back is put aside by its cut: it comes again only for a new cut. */
  const putCut = (cut: string | undefined) => { if (cut) setPutCuts(was => new Set(was).add(cut)) }
  const dispatch = useAppDispatch()
  const store = useAppStore()
  /**
   * A tap on a Bot (T3): it holds up its words, the ones it holds up already, else those of its own thread, read or
   * not. A done line on Home names the session (`slot`): that sheet comes up at its Bot again.
   */
  const tap = useCallback((slug: string, slot?: string) => {
    const b = botsNow.current.find(x => x.slug === slug)
    if (b?.state === 'working' && !slot) { setNowOf(was => (was.has(slug) ? was : new Set(was).add(slug))); return }
    // A tap on a Bot that failed raises its failed sheet again, put aside or not.
    const pick = slot ?? (b ? b.cuts.find(c => !c.stale)?.slot ?? (sessionToRaise(b) || b.thread) : '')
    if (!pick) return
    setTapped(was => (was.get(slug)?.slot === pick ? was : new Map(was).set(slug, { slot: pick, at: b?.lastTs ?? 0 })))
    // Words that are not there this long after the pinch are told as not read.
    const old = slowTimers.current.get(slug)
    if (old) clearTimeout(old)
    setSlow(was => { if (!was.has(slug)) return was; const next = new Set(was); next.delete(slug); return next })
    slowTimers.current.set(slug, setTimeout(() => { slowTimers.current.delete(slug); setSlow(was => new Set(was).add(slug)) }, SAYS_SLOW_MS))
  }, [])
  useEffect(() => { const timers = slowTimers.current; return () => { for (const t of timers.values()) clearTimeout(t) } }, [])
  /** The Bots he pinched for their words: each may hold a sheet though its body is not drawn (`useBoards`, `drawn`). */
  const pinned = useMemo(() => new Set(tapped.keys()), [tapped])
  const untap = (slug: string) => {
    setTapped(was => { if (!was.has(slug)) return was; const next = new Map(was); next.delete(slug); return next })
    setNowOf(was => { if (!was.has(slug)) return was; const next = new Set(was); next.delete(slug); return next })
  }
  /**
   * He filed the words that the board showed (`version`, the answer it was taken with, of session `slot`): thrown, the
   * board is gone. The session is marked read up to that answer's own message, for the dashboard's other windows, and
   * here too when nothing newer came since: a newer answer stays unread and comes up as the next board.
   */
  const read = useCallback((slug: string, version: string, slot: string, cut?: string) => {
    // `cut`: the failed sheet that he threw names it; what the clipboard says now may be another session's (P3 M2, S3).
    if (cut) filedFailed?.(slug)
    putCut(cut)
    untap(slug)
    if (!slot) return
    setReadHere(was => new Set(was).add(`${slug}|${version}`))
    // The sample crew's read state is its own: nothing goes to the store or to the gateway.
    if (sample) { sample.onRead(slot); return }
    const mark = readMark(version, selectSlotLastTs(store.getState(), slot))
    if (!mark) return
    // A newer answer came since the one that he filed: the session stays unread anyway, and the relay would carry the
    // newest row this window received (`noteUnsavedRowTs`), marking that newer answer read too. So nothing is told.
    if (!mark.local) return
    dispatch(markSlotRead(slot))
    emitSlotRead(slot, mark.ts)
  }, [dispatch, store, filedFailed, sample])
  /**
   * He handed the words back (D35): kept for later. Nothing more is marked read here (the take read them, D77 4); the Bot lowers them, and holds that session
   * up by itself again only once it changed. Its next unread session comes up meanwhile.
   */
  const keep = useCallback((slug: string, slot: string, cut?: string) => {
    putCut(cut)
    untap(slug)
    const rest = botsNow.current.find(b => b.slug === slug)?.rest.find(r => r.slot === slot)
    if (rest?.unread) setBack(was => new Map(was).set(slot, rest.ts))
  }, [])
  return { says, tap, read, keep, pinned }
}
