/**
 * The 3D dashboards of the room (`dash3d.ts`), as data that is read: for each Bot of the row, its layout and the
 * values its parts show.
 *
 * Who has one: a Bot whose APPLIED Dashboard page of its own declares the field `xr_layout`, seen by its owner. The
 * Bot saves such a page, applies it, and writes the layout into that field; a Bot on a shipped page has none.
 *
 * Three reads, all the Dashboard tab's own, and no other:
 * 1. Which page is in force (`dashPageQuery`): the Bot's Dashboard read, kept for its package and the names of its
 *    fields only. It also arms the Bot's live page on the gateway, and the gateway puts the Bot's whole page
 *    together to answer it, so it is made ONCE for a Bot and again only when the page itself may have changed: the
 *    frame that says so (a page applied, a version rolled back), a 409 of read 2, a clock, the socket coming back.
 *    It is kept under a key of its own, which the frames of a Bot's values and of its work do not refresh. The
 *    frame can be lost (the socket was down when the Bot applied its first page), so nothing rests on it alone
 *    (`pagesToReadAgain`): when the socket comes back every Bot's page is read again, once, and such rounds start
 *    at least `DASH_READ.reconnectMs` apart, so a socket that goes up and down does not cost a round each time; the
 *    socket's first connect after the page loads is no return (`socketReturned`), since the pages are being read
 *    then anyway; a Bot with a package is read again each minute; a Bot with none each ten minutes, so a first
 *    page is seen at the latest then.
 * 2. The values (`dashRead` in `writes.ts`, the Dashboard's whole read), asked only for a Bot whose page declares
 *    the field. The layout is one of the values.
 * 3. That something moved: the field frames of the Bot (`subscribeDashboardFields`). A frame is only a signal here;
 *    what is drawn always comes from a whole read, so nothing can be applied out of order.
 *
 * The whole read is paced per Bot (`Paced`): one out at a time, a start at most every `DASH_READ.everyMs`, and one
 * more after a burst ends, so a Bot that writes many times a second costs the gateway one read in two seconds. When
 * the gateway holds no live page for the Bot (it answers 409: the page was put out, the gateway started again),
 * read 1 is made again, which arms one; values that could not be read are marked old (`stale`) and drawn dim,
 * never kept as if they were new.
 */
import { useQueries, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api } from '../../../api/client'
import { handleDashboardFields, handleDashboardPageChanged, subscribeDashboardFields, subscribeDashboardPage } from '../../../hooks/websocket/dashboardFields'
import { handleDashboardMoved } from '../../../hooks/websocket/serverState'
import { useAppSelector } from '../../../store'
import { dashRead } from '../writes'
import { DASH_FIELD, readLayout, type DashLayout } from './dash3d'

/** What the room keeps of one read of a Bot's Dashboard: the package in force ('' when the page is a shipped one or there is none) and its fields' names. */
export interface DashPage { dashboard: string; fields: readonly string[] }

/** The key the room keeps a Bot's page facts under: outside the dashboard's own, so only the room asks for it again. */
export const dashPageKey = (slug: string) => ['xr-dash3d-page', slug] as const

/** Read 1: made once, and again only when it is asked for by name (`useDash3d`). Its HTML is never used. */
export const dashPageQuery = (slug: string, name: string) => ({
  queryKey: dashPageKey(slug),
  queryFn: async (): Promise<DashPage> => {
    const d = await api.memberDashboard(slug, name)
    return { dashboard: typeof d?.dashboard === 'string' ? d.dashboard : '', fields: Array.isArray(d?.fields) ? d.fields.filter((f): f is string => typeof f === 'string') : [] }
  },
  staleTime: Infinity,
  retry: false as const,
  refetchOnWindowFocus: false as const,
  refetchOnReconnect: false as const,
  refetchOnMount: false as const,
})

export const DASH_READ = {
  /** From one start of a Bot's whole read to the next, at least. */
  everyMs: 2000,
  /** The page of a Bot that has a package is read again this often, with no frame to ask for it. */
  fallbackMs: 60_000,
  /** The page of a Bot that has no package is read again this often: a first apply whose frame was lost is seen by then. */
  slowMs: 600_000,
  /** From one round of reads for the socket's return to the next, at least. */
  reconnectMs: 30_000,
  /** The sample values of `fixture=1` change this often. */
  fixtureTickMs: 2500,
} as const

/**
 * Whose page is read again with no frame asking for it. The socket coming back: every Bot's, since any frame may
 * have been lost while it was down, a first apply's too. The minute's clock: the Bots that have a package. The ten
 * minutes' clock: the Bots that have none.
 */
export function pagesToReadAgain(why: 'reconnect' | 'clock' | 'slow', all: readonly string[], packaged: readonly string[]): string[] {
  if (why === 'reconnect') return [...all]
  const has = new Set(packaged)
  return all.filter(slug => has.has(slug) === (why === 'clock'))
}

/**
 * Whether the socket came back: it is up now, was down before, and had been up at some time since the page loaded.
 * Its first connect after the load is no return: every page is being read then anyway.
 */
export const socketReturned = (was: boolean, now: boolean, everUp: boolean): boolean => now && !was && everUp

/**
 * One job that is asked for often and run seldom: one run out at a time, a start at most every `everyMs`, and one
 * more run after the last ask of a burst. An ask while a run is out, or too soon after a start, is kept as one.
 */
export class Paced {
  private out = false
  private wanted = false
  private lastStart = Number.NEGATIVE_INFINITY
  private timer: ReturnType<typeof setTimeout> | null = null
  private stopped = false
  /** How many runs started: for the rows. */
  starts = 0

  constructor(private readonly run: () => Promise<unknown>, private readonly everyMs: number = DASH_READ.everyMs, private readonly now: () => number = () => Date.now()) {}

  ask(): void {
    if (this.stopped) return
    this.wanted = true
    this.pump()
  }

  private pump(): void {
    if (this.stopped || this.out || this.timer !== null || !this.wanted) return
    const wait = this.lastStart + this.everyMs - this.now()
    if (wait > 0) {
      this.timer = setTimeout(() => { this.timer = null; this.pump() }, wait)
      return
    }
    this.wanted = false
    this.out = true
    this.lastStart = this.now()
    this.starts += 1
    void this.run().catch(() => undefined).finally(() => { this.out = false; this.pump() })
  }

  stop(): void {
    this.stopped = true
    if (this.timer !== null) clearTimeout(this.timer)
    this.timer = null
  }
}

export type DashFields = Readonly<Record<string, unknown>>

/** How one whole read ended: the values; no live page on the gateway (409); or a read that failed. */
export type DashRead = { fields: DashFields } | { gone: true } | { failed: true }

const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

/** One whole read of a Bot's values. Never throws: what went wrong is the answer. */
export async function readDash(ask: () => Promise<{ read?: unknown } | null>): Promise<DashRead> {
  try {
    const got = await ask()
    const read = got?.read
    const fields = record(read) ? read.fields : undefined
    return record(fields) ? { fields } : { failed: true }
  } catch (e) {
    return e instanceof ApiError && e.status === 409 ? { gone: true } : { failed: true }
  }
}

/**
 * The layout that a Bot's values hold, or null when it has none: the field is not there, or it was emptied (a clear
 * writes an empty record). Anything else is a layout that is drawn, or one that says why it is not.
 */
export function layoutOf(fields: DashFields | null | undefined): DashLayout | null {
  if (!fields || !Object.hasOwn(fields, DASH_FIELD)) return null
  const raw = fields[DASH_FIELD]
  if (raw === null || raw === undefined || (record(raw) && Object.keys(raw).length === 0)) return null
  return readLayout(raw)
}

/** One Bot's 3D dashboard, as `Dash3d.tsx` stands it. */
export interface DashView {
  slug: string
  /** The Bot's name as it is shown: the header of the taken form. */
  label: string
  layout: DashLayout
  fields: DashFields
  /** The values could not be read again: they are drawn dim. */
  stale: boolean
}

export interface DashBot { slug: string; name: string; label: string }

/** The sample dashboards of `fixture=1`: one whole, one with a part that is wrong, one that is no layout. */
const FIXTURE: Readonly<Record<string, DashFields>> = {
  atlas: {
    [DASH_FIELD]: { v: 1, parts: [
      { kind: 'ring', name: 'Aura checks', field: 'checks_done', max: 20 },
      { kind: 'bar', name: 'Spend', field: 'spend_today', max: 'spend_budget', unit: '$' },
      { kind: 'lamps', name: 'Workers', field: 'workers' },
      { kind: 'number', name: 'Open PRs', field: 'open_prs' },
    ] },
    checks_done: 7, spend_today: 12, spend_budget: 40, open_prs: 3,
    workers: [{ state: 'working' }, { state: 'working' }, { state: 'done' }, { state: 'idle' }, { state: 'failed' }],
  },
  juno: {
    [DASH_FIELD]: { v: 1, parts: [{ kind: 'number', name: 'Regions', field: 'regions' }, { kind: 'gauge', name: 'Load', field: 'load' }, { kind: 'ring', name: 'Rollout', field: 'rolled', max: 'regions' }] },
    regions: 6, rolled: 2,
  },
  mira: { [DASH_FIELD]: { v: 2, parts: [] } },
}

/** The sample values a moment later: the ring fills by one and starts again, the spend grows, a worker changes. */
function fixtureTick(now: DashFields, n: number): DashFields {
  if (typeof now.checks_done !== 'number') return now
  const states = ['working', 'done', 'idle', 'failed'] as const
  const list: unknown[] | null = Array.isArray(now.workers) ? now.workers : null
  const workers = list ? list.map((w, i) => (i === n % list.length ? { state: states[n % states.length] } : w)) : now.workers
  return { ...now, checks_done: now.checks_done >= 20 ? 7 : now.checks_done + 1, spend_today: 12 + ((n * 5) % 40), workers }
}

/** What a row reads of the dashboards' data, and what it may set with the sample crew. */
export interface DashDataProbe {
  /** For each Bot that is read: its package, how many whole reads started, and how its last one ended. */
  reads: Record<string, { dashboard: string; starts: number; last: 'none' | 'ok' | 'gone' | 'failed'; stale: boolean; why: string }>
}
interface TestHandle {
  read: () => DashDataProbe
  /**
   * A frame of the Bot as the socket hands it on: its field frame; with `value`, also the frame that says a value
   * was written (which refreshes the dashboard's own reads); with `apply`, the frame that says its page changed.
   */
  frame: (slug: string, dashboard: string, also?: 'value' | 'apply') => void
  /** With the sample crew: set a Bot's values, and stop or start their change over time. */
  set: (slug: string, fields: DashFields | null) => void
  hold: (on: boolean) => void
}

interface Reading { name: string; dashboard: string; gate: Paced; last: 'none' | 'ok' | 'gone' | 'failed'; again: boolean; at: number }

function frameNow(queryClient: QueryClient, slug: string, dashboard: string, also?: 'value' | 'apply') {
  if (also === 'apply') { handleDashboardMoved(queryClient, { slug }); handleDashboardPageChanged({ slug }); return }
  handleDashboardFields({ slug, dashboard, read: {} })
  if (also === 'value') handleDashboardMoved(queryClient, { slug })
}

/**
 * The 3D dashboards of the Bots of the row. `fixture` serves the samples and reads nothing; `probe` (or the sample
 * crew) puts a handle on the window for the rows (`__xrDash3d`).
 */
export function useDash3d(bots: readonly DashBot[], fixture: boolean, probe: boolean): { views: readonly DashView[] } {
  const queryClient = useQueryClient()
  const live = !fixture
  const pages = useQueries({ queries: live ? bots.map(b => dashPageQuery(b.slug, b.name)) : [] })
  // The Bots whose page in force declares the layout's field: slug, name, package, and when the page was read.
  const wanted = live
    ? bots.flatMap((b, i) => {
      const d = pages[i]?.data
      return d && d.dashboard && d.fields.includes(DASH_FIELD) ? [[b.slug, b.name, d.dashboard, pages[i].dataUpdatedAt] as const] : []
    })
    : []
  const wantedKey = JSON.stringify(wanted)
  // The Bots that have a package of their own, with the field or not: their page is read again by the minute's
  // clock. A Bot with none by the ten minutes' clock (`pagesToReadAgain`).
  const packagedKey = live ? bots.filter((_b, i) => pages[i]?.data?.dashboard).map(b => b.slug).join(',') : ''
  const slugsHere = live ? bots.map(b => b.slug).join(',') : ''
  const [values, setValues] = useState<Readonly<Record<string, { fields: DashFields; stale: boolean }>>>({})
  const readings = useRef(new Map<string, Reading>())

  useEffect(() => {
    const book = readings.current
    const now = JSON.parse(wantedKey) as [string, string, string, number][]
    const here = new Set(now.map(([slug]) => slug))
    for (const [slug, r] of book) if (!here.has(slug)) { r.gate.stop(); book.delete(slug) }
    setValues(was => (Object.keys(was).every(s => here.has(s)) ? was : Object.fromEntries(Object.entries(was).filter(([s]) => here.has(s)))))
    for (const [slug, name, dashboard, at] of now) {
      let r = book.get(slug)
      if (!r) {
        const made: Reading = {
          name, dashboard, last: 'none', again: false, at: -1,
          gate: new Paced(async () => {
            const got = await readDash(() => dashRead(slug, made.name, made.dashboard))
            if (book.get(slug) !== made) return
            if ('fields' in got) {
              made.last = 'ok'; made.again = false
              setValues(was => ({ ...was, [slug]: { fields: got.fields, stale: false } }))
              return
            }
            made.last = 'gone' in got ? 'gone' : 'failed'
            setValues(was => (was[slug] && !was[slug].stale ? { ...was, [slug]: { ...was[slug], stale: true } } : was))
            // No live page: the read that arms one is made again, once; its answer asks for the values again.
            if ('gone' in got && !made.again) { made.again = true; void queryClient.invalidateQueries({ queryKey: dashPageKey(slug), exact: true }) }
          }),
        }
        r = made
        book.set(slug, r)
      }
      r.name = name
      r.dashboard = dashboard
      // A new read of the page (the first, or one that armed a live page again): the values are asked for.
      if (r.at !== at) { r.at = at; r.gate.ask() }
    }
  }, [wantedKey, queryClient])

  // A field frame of a Bot that is read asks for its values.
  const slugsKey = wanted.map(([slug]) => slug).join(',')
  useEffect(() => {
    const book = readings.current
    const stops = (slugsKey ? slugsKey.split(',') : []).map(slug => subscribeDashboardFields(slug, (_read, dashboard) => {
      const r = book.get(slug)
      if (r && r.dashboard === dashboard) r.gate.ask()
    }))
    return () => { for (const stop of stops) stop() }
  }, [slugsKey])
  // The frame that says a Bot's page changed asks for that Bot's page again, whatever it had before.
  useEffect(() => {
    const here = new Set(slugsHere ? slugsHere.split(',') : [])
    return subscribeDashboardPage(slug => { if (here.has(slug)) void queryClient.invalidateQueries({ queryKey: dashPageKey(slug), exact: true }) })
  }, [slugsHere, queryClient])
  // The clocks, and the socket coming back (`pagesToReadAgain`): a page read again arms the Bot's live page, and
  // its answer asks for the values, so values never stand still, and no first page stays unseen, with nothing saying so.
  const again = useRef<(why: 'reconnect' | 'clock' | 'slow') => void>(() => undefined)
  useEffect(() => {
    again.current = why => {
      for (const r of readings.current.values()) r.again = false
      for (const slug of pagesToReadAgain(why, slugsHere ? slugsHere.split(',') : [], packagedKey ? packagedKey.split(',') : [])) void queryClient.invalidateQueries({ queryKey: dashPageKey(slug), exact: true })
    }
  }, [slugsHere, packagedKey, queryClient])
  useEffect(() => {
    const clock = setInterval(() => again.current('clock'), DASH_READ.fallbackMs)
    const slow = setInterval(() => again.current('slow'), DASH_READ.slowMs)
    return () => { clearInterval(clock); clearInterval(slow) }
  }, [])
  // The socket came back (`socketReturned`): one round of reads, and the next round at least `reconnectMs` later.
  const connected = useAppSelector(s => s.dashboard.connected)
  const wasConnected = useRef(connected)
  const everUp = useRef(connected)
  const returns = useRef<Paced | null>(null)
  useEffect(() => {
    const gate = new Paced(async () => again.current('reconnect'), DASH_READ.reconnectMs)
    returns.current = gate
    return () => gate.stop()
  }, [])
  useEffect(() => {
    if (socketReturned(wasConnected.current, connected, everUp.current)) returns.current?.ask()
    if (connected) everUp.current = true
    wasConnected.current = connected
  }, [connected])
  useEffect(() => {
    const book = readings.current
    return () => { for (const r of book.values()) r.gate.stop(); book.clear() }
  }, [])

  // The sample crew's dashboards: their values change over time, until a row holds them.
  const [samples, setSamples] = useState<Readonly<Record<string, DashFields>>>(FIXTURE)
  const held = useRef(false)
  useEffect(() => {
    if (!fixture) return
    let n = 0
    const clock = setInterval(() => {
      if (held.current) return
      n += 1
      setSamples(was => Object.fromEntries(Object.entries(was).map(([slug, f]) => [slug, fixtureTick(f, n)])))
    }, DASH_READ.fixtureTickMs)
    return () => clearInterval(clock)
  }, [fixture])

  const valuesNow = useRef(values)
  valuesNow.current = values
  useEffect(() => {
    if (!probe && !fixture) return
    const hooked = window as unknown as { __xrDash3d?: TestHandle }
    hooked.__xrDash3d = {
      read: () => ({
        reads: Object.fromEntries([...readings.current].map(([slug, r]) => {
          const v = valuesNow.current[slug]
          return [slug, { dashboard: r.dashboard, starts: r.gate.starts, last: r.last, stale: v?.stale ?? false, why: v ? layoutOf(v.fields)?.why ?? '' : '' }]
        })),
      }),
      frame: (slug, dashboard, also) => frameNow(queryClient, slug, dashboard, also),
      set: (slug, fields) => setSamples(was => (fields ? { ...was, [slug]: fields } : Object.fromEntries(Object.entries(was).filter(([s]) => s !== slug)))),
      hold: on => { held.current = on },
    }
    return () => { delete hooked.__xrDash3d }
  }, [probe, fixture, queryClient])

  const views = useMemo(() => {
    const source: Readonly<Record<string, { fields: DashFields; stale: boolean }>> = fixture
      ? Object.fromEntries(Object.entries(samples).map(([slug, fields]) => [slug, { fields, stale: false }]))
      : values
    return bots.flatMap((b): DashView[] => {
      const v = source[b.slug]
      const layout = v ? layoutOf(v.fields) : null
      return v && layout ? [{ slug: b.slug, label: b.label, layout, fields: v.fields, stale: v.stale }] : []
    })
  }, [bots, fixture, samples, values])
  return { views }
}
