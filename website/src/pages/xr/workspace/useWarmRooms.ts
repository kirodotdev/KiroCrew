/**
 * Warm rooms (D46), as state: the pages of the rooms he left, kept open at the gateway and out of view
 * (`warmRooms.ts` has the rules). A warm page has no panel: its source is told that nobody sees it, so the gateway
 * makes no picture for it, and it keeps its last picture in the tab. When he comes back, the page stands with that
 * picture at its first frame, and the gateway's fresh one follows.
 *
 * With another tab of this browser open on the room there are no warm pages: the gateway's twelve are for both tabs,
 * and a warm page of one would take a page from the other. The twelve are also shared with every other socket of
 * the gateway (his Mac and the glasses), which this tab cannot hear, so a warm page gives way three more ways
 * (D48): a page of his that the gateway refuses as one too many closes the oldest warm room and is asked for again;
 * outside a session, a hidden tab keeps no warm page, and a tab in which he has done nothing for five minutes lets
 * its warm rooms go until his next input.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { setFullHook } from './fullHook'
import { MOST_LIVE_PAGES, type PageSource } from './source'
import { AHEAD, WHOLE_MS, filled, fitWarm, giveWay, holding, mayWarm, takeWarm, type WarmPage, type WarmRoom } from './warmRooms'

/** A warm page with its source, and what stops the watch on it. */
export interface WarmEntry extends WarmPage { source: PageSource; stop?: () => void }

/** The tabs of this browser that show the room say so to each other on this channel. */
export const WARM_CHANNEL = 'kirocrew:xr-warm'
/**
 * How often a tab says that it is there, and how long another tab's silence means it is gone (ms). A tab hidden for
 * over five minutes has its timers run once a minute by Chrome, so its beat comes once a minute: the silence that
 * counts must be well over that. A tab that closes says "bye" at `pagehide`.
 */
export const TAB_BEAT_MS = 4000
export const TAB_GONE_MS = 150_000
/** A warm room's pages told early that he may see them (his dot rested on its card) are hidden again this long after he left the card (ms, D52). */
export const PEEK_AFTER_MS = 2000
/** A rest on a card tells its pages "seen" for at most this long, however long it lasts (ms, D53 4). */
export const PEEK_MOST_MS = 10_000
/** A tab in which he has done nothing for this long, outside a session, lets its warm rooms go (ms). */
export const IDLE_MS = 300_000
/** How often the tab looks whether it is hidden or idle (ms). */
const AWAKE_EVERY_MS = 1000

/** What the warm set reads from the workspace: whether a session is on (a session in the glasses is never idle). */
export interface WarmInputs { inSession?: () => boolean }

export function useWarmRooms({ inSession }: WarmInputs = {}) {
  const inSessionNow = useRef(inSession)
  inSessionNow.current = inSession
  const set = useRef<WarmRoom<WarmEntry>[]>([])
  /** Outside a session, the tab is hidden or he has done nothing in it for `IDLE_MS`: no warm page is kept or made. */
  const asleep = useRef(false)
  /** The last room he left: whether its pages were kept, and if not, why (for a row to read). */
  const last = useRef<{ room: string; kept: boolean; why: 'tab' | 'asleep' | 'page' | 'fit' | null; pages: [string, boolean, boolean][] } | null>(null)
  const [warm, setWarm] = useState<readonly string[]>([])
  const sync = useCallback(() => {
    const now = set.current.map(r => r.room).sort()
    setWarm(was => (was.join('\n') === now.join('\n') ? was : now))
  }, [])
  /** These rooms are cold again: their pages close at the gateway. */
  const close = useCallback((rooms: readonly WarmRoom<WarmEntry>[], but: ReadonlySet<string> = new Set()) => {
    if (!rooms.length) return
    const gone = new Set(rooms.map(r => r.room))
    set.current = set.current.filter(r => !gone.has(r.room))
    for (const r of rooms) for (const p of r.pages) { p.stop?.(); if (!but.has(p.id)) p.source.dispose() }
    sync()
  }, [sync])
  // Another tab of this browser on the room: none while it lives.
  const others = useRef(new Map<string, number>())
  const alone = useCallback(() => { const now = Date.now(); for (const [id, at] of others.current) if (now - at > TAB_GONE_MS) others.current.delete(id); return others.current.size === 0 }, [])
  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return
    const me = `${Date.now()}-${Math.random()}`
    const channel = new BroadcastChannel(WARM_CHANNEL)
    const say = (what: 'here' | 'bye') => channel.postMessage({ what, id: me })
    channel.onmessage = e => {
      const m = e.data as { what?: string; id?: string } | null
      if (!m?.id || m.id === me) return
      if (m.what === 'bye') { others.current.delete(m.id); return }
      const fresh = !others.current.has(m.id)
      others.current.set(m.id, Date.now())
      // A tab that is new to this one is answered at once, and this tab's warm pages close: the twelve are for both.
      if (fresh) { say('here'); close(set.current) }
    }
    say('here')
    const beat = setInterval(() => say('here'), TAB_BEAT_MS)
    const bye = () => say('bye')
    window.addEventListener('pagehide', bye)
    return () => { clearInterval(beat); window.removeEventListener('pagehide', bye); bye(); channel.close() }
  }, [close])

  /**
   * He leaves `room`, and these pages of it do not stand in the room he goes to, which has `standing` pages. True:
   * they are kept warm (told that nobody sees them), and the caller only takes their panels away. False: they are
   * not kept (one was not whole, another tab is open, or they do not fit), and the caller closes them as before.
   */
  const leave = useCallback((room: string, pages: readonly WarmEntry[], standing: number, leftAt = Date.now()): boolean => {
    const others = set.current.filter(r => r.room !== room)
    const stale = set.current.filter(r => r.room === room)
    // A page is whole once it showed for 4 s in his view, or once it filled out of view (D48, A: a page he never
    // looked at, or only glanced at, goes on filling out of view until it has).
    const facts = pages.map(p => ({ live: (p.source.status?.() ?? 'live') === 'live', whole: (p.source.wasWhole?.(WHOLE_MS) ?? false) || (p.source.filledOutOfView?.() ?? false) }))
    const single = alone()
    const awake = !asleep.current
    const ok = single && awake && mayWarm(facts)
    const mine: WarmRoom<WarmEntry> = { room, leftAt, pages: [...pages] }
    const fit = fitWarm(ok ? [...others, mine] : others, standing)
    const kept = ok && fit.keep.includes(mine)
    last.current = { room, kept, why: kept ? null : !single ? 'tab' : !awake ? 'asleep' : !ok ? 'page' : 'fit', pages: pages.map((p, i) => [p.id, facts[i].live, facts[i].whole]) }
    close([...stale, ...fit.close.filter(r => r !== mine)])
    if (!kept) return false
    for (const p of mine.pages) {
      p.source.visible?.(false)
      // A warm page that is no longer live (the gateway closed it, refused it, or the socket went) would come back as
      // a page that loads: its room is cold again.
      p.stop = p.source.subscribe(() => { if ((p.source.status?.() ?? 'live') !== 'live') close(set.current.filter(r => r.room === room)) })
    }
    set.current = [...set.current, mine]
    sync()
    return true
  }, [alone, close, sync])

  const dropAhead = useRef<() => void>(() => undefined)
  /**
   * He goes to `to`, whose record keeps these pages: for each, the warm page that it takes, or `null` (it comes
   * fresh). The taken pages leave the set and are the caller's from here on. The room's warm pages that the record
   * no longer keeps close, and so does the rest of another room that gave one of its pages.
   */
  const take = useCallback((to: string, kept: readonly { path: string }[]): (WarmEntry | null)[] => {
    // A room that is still being warmed ahead has pages that have not filled: they are not handed over. The switch
    // asks for its pages as for a cold room, and the half-filled ones close.
    if (ahead.current) dropAhead.current()
    const got = takeWarm(set.current, to, kept)
    const taken = new Set(got.taken.flatMap(p => (p ? [p.id] : [])))
    for (const p of got.taken) p?.stop?.()
    close(set.current.filter(r => r.room === to || got.robbed.includes(r.room)), taken)
    return got.taken
  }, [close])

  // Warm ahead: one room at a time is opened out of his sight and kept once each of its pages has filled.
  const ahead = useRef<{ room: string; pages: WarmEntry[]; since: number } | null>(null)
  /** The rooms that were given up while they were warmed ahead, and when: none is tried again at once. */
  const gaveUp = useRef(new Map<string, number>())
  const [warming, setWarming] = useState<string | null>(null)
  /** The room that is being warmed ahead is given up: its pages close. */
  const drop = useCallback(() => {
    const a = ahead.current
    if (!a) return
    ahead.current = null
    for (const p of a.pages) p.source.dispose()
    setWarming(null)
  }, [])
  /** Whether `room` may be warmed ahead now: no other tab, not warm already, and not given up a little while ago. */
  const mayAhead = useCallback((room: string): boolean => alone() && !asleep.current && !set.current.some(r => r.room === room) && Date.now() - (gaveUp.current.get(room) ?? -Infinity) >= AHEAD.againMs, [alone])
  /**
   * Open `room` ahead: its pages that would have to come at a switch, made by the caller (asked for at the gateway,
   * with no panel). False: not now (another tab, one is being warmed already, or the room is warm).
   */
  const warmAhead = useCallback((room: string, pages: readonly WarmEntry[]): boolean => {
    if (ahead.current || !pages.length || !mayAhead(room)) return false
    ahead.current = { room, pages: [...pages], since: Date.now() }
    setWarming(room)
    return true
  }, [mayAhead])
  /**
   * Looked at every little while: the room that is being warmed ahead is kept once every page of it has filled
   * (`filled`), and is given up when a page of it is refused or closed, or when it takes too long. `standing`: how
   * many pages stand now (a room that no longer fits is given up).
   */
  const tend = useCallback((standing: number) => {
    const a = ahead.current
    if (!a) return
    const facts = a.pages.map(p => ({ status: p.source.status?.() ?? 'live', rest: p.source.restFacts?.() ?? null }))
    if (!alone() || asleep.current || facts.some(f => f.status !== 'live' && f.status !== 'loading') || Date.now() - a.since > AHEAD.giveUpMs) { gaveUp.current.set(a.room, Date.now()); drop(); return }
    if (!facts.every(f => f.status === 'live' && f.rest !== null && filled(f.rest))) return
    ahead.current = null
    setWarming(null)
    for (const p of a.pages) p.source.countWhole?.()
    // A room warmed ahead was never left: every room he really left outlives it (stamped as left before any of them).
    if (!leave(a.room, a.pages, standing, 0)) for (const p of a.pages) p.source.dispose()
  }, [alone, drop, leave])
  /** How many pages are warm now, with those of the room that is being warmed ahead. */
  const count = useCallback(() => set.current.reduce((n, r) => n + r.pages.length, 0) + (ahead.current?.pages.length ?? 0), [])
  /** `need` more pages are about to open beside the `open` that stand: the oldest warm rooms close until they fit. */
  const makeRoom = useCallback((open: number, need = 1) => {
    // The room that is only being warmed ahead goes first: nothing of his is lost with it.
    if (ahead.current && open + count() + need > MOST_LIVE_PAGES) drop()
    close(giveWay(set.current, open + count(), need))
  }, [close, count, drop])
  /** A page of this path opens in the room: a warm room that holds it is cold, so no page is open twice. */
  const clearPath = useCallback((path: string) => { if (ahead.current?.pages.some(p => p.path === path)) drop(); close(holding(set.current, path)) }, [close, drop])
  /** The rooms there are now: a warm room that is no longer one of them closes (removed, here or in another tab). */
  const only = useCallback((rooms: readonly string[]) => { if (ahead.current && !rooms.includes(ahead.current.room)) drop(); close(set.current.filter(r => !rooms.includes(r.room))) }, [close, drop])
  /**
   * His dot or pointer rests on the card of `room`, or no longer (`RoomCards.tsx`, D52). Its warm pages are told he
   * may see them, so their fresh pictures are on the way before the press; 2 s after he leaves the card they are told
   * hidden again. Nothing for a room that is not warm (the room he stands in is never), or while the tab is asleep or
   * hidden.
   */
  const peekTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>())
  const rest = useCallback((room: string, on: boolean) => {
    const pending = peekTimers.current.get(room)
    if (pending) { clearTimeout(pending); peekTimers.current.delete(room) }
    const pagesOf = () => set.current.find(r => r.room === room)?.pages ?? []
    if (on) {
      if (asleep.current || document.visibilityState === 'hidden') return
      for (const p of pagesOf()) p.source.peek?.(true)
      // A rest tells them "seen" for at most `PEEK_MOST_MS`; then they are told hidden until the dot leaves the card
      // and rests again (D53 4): a dot left on a card, or a headset set down, keeps no page cast for nobody.
      peekTimers.current.set(room, setTimeout(() => { peekTimers.current.delete(room); for (const p of pagesOf()) p.source.peek?.(false) }, PEEK_MOST_MS))
      return
    }
    // The pages that he did not go to are hidden again; a page taken by the switch has left the set.
    peekTimers.current.set(room, setTimeout(() => { peekTimers.current.delete(room); for (const p of pagesOf()) p.source.peek?.(false) }, PEEK_AFTER_MS))
  }, [])
  useEffect(() => () => { for (const t of peekTimers.current.values()) clearTimeout(t); peekTimers.current.clear() }, [])
  /** The sources that the warm pages hold: the workspace does not free them when their panels go. */
  const sources = useCallback((): PageSource[] => [...set.current.flatMap(r => r.pages.map(p => p.source)), ...(ahead.current?.pages.map(p => p.source) ?? [])], [])
  // The page is left: every warm page closes.
  useEffect(() => () => { for (const r of set.current) for (const p of r.pages) { p.stop?.(); p.source.dispose() } set.current = []; for (const p of ahead.current?.pages ?? []) p.source.dispose(); ahead.current = null }, [])
  const left = useCallback(() => last.current, [])
  dropAhead.current = drop

  // A page of his that the gateway refuses as one too many: the twelve are shared with sockets that this tab cannot
  // hear. The room being warmed ahead goes first, then the warm room he left longest ago, and the page is asked for
  // again; each refusal frees one room, until the page is given or no warm room is left.
  useEffect(() => {
    setFullHook(source => {
      if (sources().includes(source)) return false
      if (ahead.current) { gaveUp.current.set(ahead.current.room, Date.now()); drop(); return true }
      const oldest = [...set.current].sort((a, b) => a.leftAt - b.leftAt)[0]
      if (!oldest) return false
      close([oldest])
      return true
    })
    return () => setFullHook(null)
  }, [close, drop, sources])

  // Hidden, or idle outside a session: every warm page closes and none is made. Awake again: warm ahead starts over
  // as after a load (no room is held back for having been given up).
  const inputAt = useRef(Date.now())
  const [sleeping, setSleeping] = useState(false)
  useEffect(() => {
    const touched = () => {
      inputAt.current = Date.now()
      if (asleep.current && document.visibilityState !== 'hidden') look()
    }
    const look = () => {
      // Inside a session the tab is never asleep, whatever the browser says of its visibility.
      const session = inSessionNow.current?.() ?? false
      const hidden = !session && document.visibilityState === 'hidden'
      const idle = !session && Date.now() - inputAt.current >= IDLE_MS
      const now = hidden || idle
      if (now === asleep.current) return
      asleep.current = now
      setSleeping(now)
      if (now) { drop(); close(set.current); return }
      gaveUp.current.clear()
    }
    const kinds = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart'] as const
    for (const k of kinds) window.addEventListener(k, touched, { capture: true, passive: true })
    document.addEventListener('visibilitychange', look)
    const every = setInterval(look, AWAKE_EVERY_MS)
    return () => { for (const k of kinds) window.removeEventListener(k, touched, { capture: true }); document.removeEventListener('visibilitychange', look); clearInterval(every) }
  }, [close, drop])

  /**
   * A warm room whose switch would not be served whole by the warm pages (`need` says what a switch to it would ask
   * for, as `takeWarm` takes: its own pages first, then another warm room's) is cold: a page it needs stood in
   * another room and closed since. Its dot goes out. Rooms are looked at until none changes.
   */
  const prune = useCallback((need: (room: string) => readonly { path: string }[] | null) => {
    for (;;) {
      const gone = set.current.filter(r => { const come = need(r.room); return come === null || !takeWarm(set.current, r.room, come).taken.every(Boolean) })
      if (!gone.length) return
      close(gone)
    }
  }, [close])

  return useMemo(() => ({ warm, warming, sleeping, leave, take, makeRoom, clearPath, only, sources, count, left, warmAhead, mayAhead, tend, dropAhead: drop, prune, rest }), [warm, warming, sleeping, leave, take, makeRoom, clearPath, only, sources, count, left, warmAhead, mayAhead, tend, drop, prune, rest])
}
