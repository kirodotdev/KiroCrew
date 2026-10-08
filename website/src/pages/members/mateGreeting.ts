/**
 * The ONE "mate greeting on open" seam of the Crewmates page: what a crewmate
 * says when the user opens its chat, picked once per open.
 *
 * Two kinds, picked in one hook so the page never shows two greetings for one
 * open:
 *
 * - `warm`: the user comes back while the crewmate is in the middle of a goal,
 *   and the chat opens on where that goal stands (what finished, what is in
 *   progress, what needs a look) and the next step. Warm wins.
 * - `cold`: no goal in flight, and the thread is new or idle for
 *   `COLD_AFTER_MS`. The chat opens on a welcome that recaps the work the
 *   crewmate holds -- goals it left open, its recent sessions -- and asks what
 *   to pick up (`GET /api/members/{slug}/recap`).
 *
 * Built from the crewmate's own work ledger (`GET /api/crew-board`, the masked
 * read the Crew board already uses, through the same query key), never from a
 * model call: the status is already recorded, so reading it is cheap and says
 * the same thing every time.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type MemberRecap } from '../../api/client'
import { isNotFoundError } from '../../api/apiError'
import { crewBoardQueryKey, type WorkBoardItem, type WorkBoardResponse } from '../../api/crewBoard'
import { retryPolicy } from '../../api/queryClient'
import { partitionBoardRows } from '../crewBoardRows'

/** Why an open item needs a look, most urgent first. */
export type ResumeReason = 'question' | 'blocked' | 'quiet' | 'done'

export interface ResumeItem {
  title: string
  reason: ResumeReason
  /** For a question: the worker's own last report, which is where it asks. */
  detail?: string
}

export interface MateResume {
  goal: string
  /** Closed items: the Crew board's Finished band. */
  finished: number
  /** Open items that need no look, split by whether a worker is running them.
   *  Every open item is in `running`, `idle` or `attention`. */
  running: number
  idle: number
  /** Open items that need a look, most urgent first. */
  attention: ResumeItem[]
  /** The next step, picked from the most urgent item (`attention[0]`). */
  next:
    | { kind: 'attention'; item: ResumeItem }
    | { kind: 'wait' }
    | { kind: 'continue'; title: string }
  /** What the card says, as one string: a return to the same card soon after
   *  it was shown says nothing new, so it is not shown again. */
  fingerprint: string
}

export type MateGreeting =
  | { kind: 'warm'; slot: string; resume: MateResume }
  | { kind: 'cold'; slot: string; recap: MemberRecap }

/** Which crewmate a cold welcome is for, and when its thread was last active
 *  (epoch seconds, 0 for never: the roster row's `last_active_ts`). */
export interface ColdOpen {
  slug: string
  member: string
  lastActiveTs: number
}

/** Idle time after which an open is a cold start. Past a lunch break, short of
 *  the next working day. */
export const COLD_AFTER_MS = 6 * 60 * 60_000

/** Whether an open with no goal in flight greets cold: the thread is new or
 *  idle long enough, and this idle stretch was not welcomed already. */
export function isColdOpen(lastActiveTs: number, now: number, welcomedTs: string | null): boolean {
  if (welcomedTs === String(lastActiveTs)) return false
  return lastActiveTs <= 0 || now - lastActiveTs * 1000 >= COLD_AFTER_MS
}

const WELCOMED_PREFIX = 'kc-mate-welcomed-'

function readWelcomed(slot: string): string | null {
  try {
    return localStorage.getItem(WELCOMED_PREFIX + slot)
  } catch {
    return null
  }
}

function markWelcomed(slot: string, lastActiveTs: number): void {
  try {
    localStorage.setItem(WELCOMED_PREFIX + slot, String(lastActiveTs))
  } catch {
    // Blocked storage: the welcome may repeat on the next open, the harmless side.
  }
}

const REASON_RANK: Record<ResumeReason, number> = { question: 0, blocked: 1, quiet: 2, done: 3 }

/** Precedence follows the board's own row label (`rowKindLabelKey`): an
 *  orphaned item has no one left to answer it, so it reads as gone quiet even
 *  while it carries a question. */
function reasonOf(item: WorkBoardItem): ResumeReason | null {
  if (item.orphaned) return 'quiet'
  if (item.outstanding) return 'question'
  if (item.stale) return 'quiet'
  if (item.status === 'blocked') return 'blocked'
  if (item.status === 'done') return 'done'
  return null
}

/** The warm greeting for a board, or null when no goal is in flight: a board
 *  whose every item is closed is a goal that ended, not one to resume. */
export function summarizeResume(board: WorkBoardResponse): MateResume | null {
  const { ruling, working, finished } = partitionBoardRows(board.items)
  const open = [...ruling, ...working]
  if (open.length === 0) return null
  const attention: ResumeItem[] = []
  let running = 0
  let idle = 0
  for (const item of open) {
    const reason = reasonOf(item)
    if (reason === 'question' && item.summary) attention.push({ title: item.title, reason, detail: item.summary })
    else if (reason) attention.push({ title: item.title, reason })
    else if (item.alive === 'running') running += 1
    else idle += 1
  }
  attention.sort((a, b) => REASON_RANK[a.reason] - REASON_RANK[b.reason])
  // Nothing needs a look: wait only when every open item has a worker running;
  // an idle one is work nobody is doing, so the step is to pick it up.
  const idleItem = open.find((i) => !reasonOf(i) && i.alive !== 'running')
  const next: MateResume['next'] = attention.length
    ? { kind: 'attention', item: attention[0] }
    : idleItem
      ? { kind: 'continue', title: idleItem.title }
      : { kind: 'wait' }
  const said = { goal: board.conductor.goal, finished: finished.length, running, idle, attention, next }
  // Built from what the card shows rather than from the item fields, so any
  // change a reader would see (a worker dying, an item going idle) is a change.
  return { ...said, fingerprint: JSON.stringify(said) }
}

/** How long a shown, unchanged status stays quiet on the next open. Long
 *  enough that hopping between crewmates, or a reconnect re-confirming the
 *  thread, does not bring back a card the user just dismissed; short enough
 *  that coming back after a break shows it again. */
export const RESUME_REPEAT_MS = 15 * 60_000

const SEEN_PREFIX = 'kc-mate-resume-seen-'

function seenRecently(slot: string, fingerprint: string, now: number): boolean {
  try {
    const raw = sessionStorage.getItem(SEEN_PREFIX + slot)
    if (!raw) return false
    const seen = JSON.parse(raw) as { fp?: unknown; at?: unknown }
    return seen.fp === fingerprint && typeof seen.at === 'number' && now - seen.at < RESUME_REPEAT_MS
  } catch {
    return false
  }
}

function markSeen(slot: string, fingerprint: string, now: number): void {
  try {
    sessionStorage.setItem(SEEN_PREFIX + slot, JSON.stringify({ fp: fingerprint, at: now }))
  } catch {
    // Storage full or blocked: the greeting may repeat on the next open, which
    // is the harmless side.
  }
}

/** A read that failed for a reason other than "this crewmate has no ledger". */
export interface MateGreetingFailure {
  slot: string
  /** Which read failed: the board (warm) or the recap (cold). */
  kind: 'warm' | 'cold'
  error: unknown
}

/** A 404 is the crewmate that never ran a goal: an answer, not a failure, so it
 *  is not retried. Anything else takes the dashboard's shared retry ladder. */
const retryUnlessNoLedger = (failureCount: number, error: unknown): boolean =>
  !isNotFoundError(error) && retryPolicy(failureCount, error)

/**
 * The greeting for the crewmate chat open on `slotKey`.
 *
 * Fires once per open: when `slotKey` turns to a slot (the thread confirmed),
 * not on every render. A slot going blank and back (a reconnect's re-confirm)
 * is a new open, and the seen-record above keeps it from repeating. Nothing is
 * read while the crewmate is mid-turn (`idle` false): its own reply is about to
 * say where things stand. A turn starting takes a shown greeting down.
 *
 * The read goes through the Crew board's query key with `staleTime: 0`, so a
 * board cached by the menu or page is refreshed, never shown as this return's
 * status. It is enabled only while this open waits on it, so it never polls.
 *
 * `cold` names the crewmate for the cold welcome (null: none known yet). When
 * the board says no goal is in flight, the recap is read through its own query,
 * enabled only while this open stays idle, so a turn starting before it answers
 * drops it. A recap with nothing in it draws no card. A cold welcome is
 * remembered against the thread's last activity, so a reload with nothing new
 * stays quiet and the next idle stretch welcomes once more.
 */
export function useMateGreeting(slotKey: string, idle: boolean, cold: ColdOpen | null): {
  greeting: MateGreeting | null
  failure: MateGreetingFailure | null
  dismiss: () => void
} {
  const [greeting, setGreeting] = useState<MateGreeting | null>(null)
  const [failure, setFailure] = useState<MateGreetingFailure | null>(null)
  // The slot whose open is waiting on its read.
  const [armed, setArmed] = useState('')
  const handled = useRef('')
  // Read at settle time, not a dependency: the roster row moving under an open
  // chat is not a new open.
  const coldRef = useRef(cold)
  coldRef.current = cold
  // The open waiting on its recap, and the crewmate it is for.
  const [coldArmed, setColdArmed] = useState<(ColdOpen & { slot: string }) | null>(null)
  useEffect(() => {
    if (!slotKey) {
      // Leaving the chat ends this open: a reopen shows only what the repeat
      // guards admit, never a card kept from before.
      handled.current = ''
      setGreeting(null)
      return
    }
    if (handled.current === slotKey) return
    handled.current = slotKey
    setGreeting(null)
    setFailure(null)
    setColdArmed(null)
    // An open that begins mid-turn reads nothing, and is not re-armed when the
    // turn ends: this open is handled.
    setArmed(idle ? slotKey : '')
  }, [slotKey, idle])
  useEffect(() => {
    if (idle) return
    setGreeting(null)
    setArmed('')
    setColdArmed(null)
  }, [idle])

  const armedSlot = armed === slotKey ? armed : ''
  const read = useQuery({
    queryKey: crewBoardQueryKey(armedSlot),
    queryFn: () => api.crewBoard(armedSlot),
    enabled: !!armedSlot,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: retryUnlessNoLedger,
  })
  // Decided once the read settles. A cached board is stale under
  // `staleTime: 0`, so enabling the read reports it fetching at once and the
  // old data never decides this open.
  const { data, error, fetchStatus } = read
  useEffect(() => {
    if (!armedSlot || fetchStatus !== 'idle') return
    // No goal in flight: a new or long-idle thread opens on the cold welcome.
    const welcome = () => {
      const c = coldRef.current
      if (!c || !isColdOpen(c.lastActiveTs, Date.now(), readWelcomed(armedSlot))) return
      setColdArmed({ ...c, slot: armedSlot })
    }
    if (error) {
      setArmed('')
      // No ledger: a crewmate that never ran a goal, the common cold case.
      if (isNotFoundError(error)) welcome()
      else setFailure({ slot: armedSlot, kind: 'warm', error })
      return
    }
    if (!data) return
    setArmed('')
    const resume = summarizeResume(data)
    if (!resume) return welcome()
    const now = Date.now()
    if (seenRecently(armedSlot, resume.fingerprint, now)) return
    markSeen(armedSlot, resume.fingerprint, now)
    setGreeting({ kind: 'warm', slot: armedSlot, resume })
  }, [armedSlot, data, error, fetchStatus])

  const recapFor = coldArmed && coldArmed.slot === slotKey && idle ? coldArmed : null
  const recapRead = useQuery({
    queryKey: ['memberRecap', recapFor?.slug ?? '', recapFor?.member ?? ''],
    queryFn: () => api.memberRecap(recapFor!.slug, recapFor!.member),
    enabled: !!recapFor,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: retryPolicy,
  })
  useEffect(() => {
    if (!recapFor || recapRead.fetchStatus !== 'idle') return
    setColdArmed(null)
    if (recapRead.error) {
      setFailure({ slot: recapFor.slot, kind: 'cold', error: recapRead.error })
      return
    }
    const recap = recapRead.data
    if (!recap || (recap.paused.length === 0 && recap.recent.length === 0)) return
    markWelcomed(recapFor.slot, recapFor.lastActiveTs)
    setGreeting({ kind: 'cold', slot: recapFor.slot, recap })
  }, [recapFor, recapRead.data, recapRead.error, recapRead.fetchStatus])

  const dismiss = useCallback(() => {
    setGreeting(null)
    setFailure(null)
  }, [])
  return {
    greeting: greeting && greeting.slot === slotKey ? greeting : null,
    failure: failure && failure.slot === slotKey ? failure : null,
    dismiss,
  }
}
