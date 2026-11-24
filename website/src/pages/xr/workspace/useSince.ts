/**
 * SINCE YOU LEFT (D36), as state (`since.ts` has the rules). It reads the last visit from the room's record once,
 * compares the crew with it at the first read in which the states can be known, and from then on only drops what
 * he has dealt with. It hands the record what to keep: until that first read, what the record held (so a write
 * before the states are known cannot wipe the visit); after it, this visit as it is now and what is left to read.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'
import { ROOM_KEY, readSeen, readSince } from './room'
import { dealt, enter, lookedAt, seenNow, sinceCounts, sinceCuts, sinceLit, stillSince, type Seen, type Since, type SinceBot, type SinceGroup } from './since'

/**
 * A visit ends when the page is hidden, or the XR session has ended, for longer than this (D39, ruling 3): the record
 * stops following the crew then, and when he is back the line is worked out again against the visit as he left it.
 */
export const VISIT_GAP_MS = 5 * 60_000

/** What the room's record asks for when it is written. */
export interface SinceKept { seen: () => Seen | null; since: () => Since | null }

export interface SinceState {
  /** When the visit was (ms) and how many Bots are in each group; null: no line. */
  line: { at: number; counts: Record<SinceGroup, number> } | null
  /** The Bots that are lit: exactly the Bots that the line counts. */
  lit: ReadonlySet<string>
  /** The cuts that are new since his last visit: the rim draws each red until he has dealt with it. */
  cuts: readonly string[]
  /** A press on the line: he has read it, and all of it goes. */
  clear: () => void
  /** A tap on a Bot: one that failed is dealt with by it (its last words come up). */
  tapped: (slug: string) => void
  /** He took a Bot's sheet in his hand (D39.1): only what he took is cleared; "to read" still counts it. */
  looked: (slug: string, took: 'done' | 'failed') => void
  /** What a test reads (`?probe=1`). */
  probe: () => { at: number | null; counts: Record<SinceGroup, number>; lit: string[]; groups: Record<string, SinceGroup>; entered: boolean; hadVisit: boolean }
}

/**
 * `known`: the states can be known now, from a whole read (the list of sessions loaded too: `model.wholeRead`). `now`: the room's clock. `kept`: where the record's writer
 * finds what to keep. `changed`: write the record soon. `seenCuts`: the cuts the rim's book calls seen.
 */
export function useSince(bots: readonly SinceBot[], known: boolean, now: () => number, kept: MutableRefObject<SinceKept | null>, changed: () => void, seenCuts: ReadonlySet<string>, inSession = false): SinceState {
  const stored = useRef<{ seen: Seen | null; since: Since | null } | null>(null)
  if (!stored.current) {
    let text: string | null = null
    try { text = window.localStorage.getItem(ROOM_KEY) } catch { /* no store to read: no visit was kept */ }
    stored.current = { seen: readSeen(text), since: readSince(text) }
  }
  const [since, setSince] = useState<Since | null>(null)
  const entered = useRef(false)
  const sinceNow = useRef(since)
  sinceNow.current = since
  const botsNow = useRef(bots)
  botsNow.current = bots
  const nowRef = useRef(now)
  nowRef.current = now
  // The visit as it stood at the last whole read: a write between two (a reload, a closed tab) keeps that, never a
  // part-list's "nothing asks, nothing is cut" (REVIEW-15 P4 M1).
  const wholeSeen = useRef<SinceBot[] | null>(null)
  // Away: the page hidden, or an XR session that ended. From when, and the visit as he left it then.
  const [away, setAway] = useState(() => typeof document !== 'undefined' && document.visibilityState === 'hidden')
  useEffect(() => {
    const look = () => setAway(document.visibilityState === 'hidden')
    document.addEventListener('visibilitychange', look)
    return () => document.removeEventListener('visibilitychange', look)
  }, [])
  const hadSession = useRef(false)
  if (inSession) hadSession.current = true
  const gone = away || (hadSession.current && !inSession)
  const awayFrom = useRef<{ at: number; seen: Seen | null } | null>(null)
  const [visits, setVisits] = useState(0)
  useEffect(() => {
    if (gone) {
      if (!awayFrom.current) {
        const at = nowRef.current()
        awayFrom.current = { at, seen: entered.current && wholeSeen.current ? seenNow(wholeSeen.current, at) : stored.current?.seen ?? null }
      }
      return
    }
    const from = awayFrom.current
    awayFrom.current = null
    if (!from || nowRef.current() - from.at <= VISIT_GAP_MS) return
    // Back after a visit's gap: a new entry, against the visit as he left it and what was left of the line then.
    stored.current = { seen: from.seen, since: sinceNow.current }
    entered.current = false
    setVisits(n => n + 1)
  }, [gone, inSession])
  useEffect(() => {
    if (!known) return
    wholeSeen.current = [...bots]
    if (!entered.current) {
      entered.current = true
      const first = enter(stored.current?.since ?? null, stored.current?.seen ?? null, bots)
      sinceNow.current = first
      setSince(first)
      return
    }
    setSince(was => stillSince(was, bots, seenCuts))
  }, [known, bots, seenCuts, visits])
  kept.current = {
    // Away, the record keeps the visit as he left it: it does not follow the crew while nobody looks.
    seen: () => (awayFrom.current ? awayFrom.current.seen : entered.current && wholeSeen.current ? seenNow(wholeSeen.current, nowRef.current()) : stored.current?.seen ?? null),
    since: () => (entered.current ? sinceNow.current : stored.current?.since ?? null),
  }
  // The record follows: what each Bot asks and which cuts it has, and what is left to read.
  const stamp = useMemo(() => JSON.stringify([known ? seenNow(bots, 0).bots : null, since]), [known, bots, since])
  const changedNow = useRef(changed)
  changedNow.current = changed
  useEffect(() => { if (entered.current) changedNow.current() }, [stamp])
  const clear = useCallback(() => setSince(null), [])
  const tapped = useCallback((slug: string) => setSince(was => (was?.bots[slug]?.group === 'failed' ? dealt(was, slug) : was)), [])
  const looked = useCallback((slug: string, took: 'done' | 'failed') => setSince(was => lookedAt(was, slug, took)), [])
  const lit = useMemo(() => new Set(sinceLit(since)), [since])
  const cuts = useMemo(() => sinceCuts(since), [since])
  const line = useMemo(() => (since ? { at: since.at, counts: sinceCounts(since) } : null), [since])
  const probe = useCallback(() => {
    const s = sinceNow.current
    return { at: s?.at ?? null, counts: sinceCounts(s), lit: sinceLit(s), groups: Object.fromEntries(Object.entries(s?.bots ?? {}).map(([slug, item]) => [slug, item.group])), entered: entered.current, hadVisit: !!stored.current?.seen }
  }, [])
  return { line, lit, cuts, clear, tapped, looked, probe }
}
