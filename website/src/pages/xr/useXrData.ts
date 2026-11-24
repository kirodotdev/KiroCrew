/**
 * The XR page's data: the same reads the dashboard already makes, folded
 * per Bot. Live data comes from the roster query, the slots the app shell's
 * one websocket keeps in the store, and the approvals and question reads.
 * `?fixture=1` swaps all of it for sample data in the same shapes.
 *
 * Every read here reports how it went (loading, ok, failed), because a failed
 * read must never look like "nothing to show". Goals and the briefing use the
 * dashboard's own query keys, so whatever refreshes them there refreshes them
 * here. This module only reads; writes live in `writes.ts`.
 */
import { useMemo } from 'react'
import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { cronJobsQuery } from '../../api/cronJobsQuery'
import { memberBriefingQueryKey, membersRosterQuery } from '../../api/membersQuery'
import { useAppSelector } from '../../store'
import { memberGoalsKey } from '../members/CrewGoals'
import { FIXTURE_NOW_MS, fixtureBriefing, fixtureGoals, fixtureMessages, fixturePage, xrFixture } from './fixture'
import { buildGlance, type XrGlance, type XrPageState } from './glance'
import { buildXrCrew, noticesFrom, type XrCrew, type XrFeedState, type XrFeeds, type XrNotice } from './model'
import type { XrThread, XrWant } from './scene'
import { XR_THREAD_PAGE, linesFrom } from './thread'

export interface XrCrewData {
  crew: XrCrew
  notices: XrNotice[]
  loading: boolean
  error: unknown
}

const NONE: never[] = []

const feedOf = (q: { isError: boolean; isPending: boolean }): XrFeedState => (q.isError ? 'failed' : q.isPending ? 'loading' : 'ok')

export function useXrCrew(fixture: boolean, late: boolean, reply = 0): XrCrewData {
  const live = !fixture
  const members = useQuery({ ...membersRosterQuery, enabled: live })
  const approvals = useQuery({ queryKey: ['global-approvals'], queryFn: () => api.approvals(), enabled: live, staleTime: 0, refetchInterval: 30_000 })
  const questions = useQuery({ queryKey: ['command-center', 'questions'], queryFn: api.pendingQuestions, enabled: live, refetchInterval: 30_000 })
  // The dashboard's own list of scheduled jobs: each Bot's plot shows its schedule.
  const jobs = useQuery({ ...cronJobsQuery, enabled: live })
  const slots = useAppSelector(s => s.dashboard.slots)
  const unread = useAppSelector(s => s.dashboard.unreadSlots)
  const connected = useAppSelector(s => s.dashboard.connected)
  // The list of sessions is whole: loaded, and not a part of it. An ask is new or gone only by a whole list.
  const rows = useAppSelector(s => s.dashboard.slotsLoaded && !s.dashboard.slotsListPartial)
  const notes = useAppSelector(s => s.notifications.items)

  const roster = feedOf(members)
  const a = feedOf(approvals)
  const q = feedOf(questions)
  const asks: XrFeedState = a === 'failed' || q === 'failed' ? 'failed' : a === 'loading' || q === 'loading' ? 'loading' : 'ok'

  return useMemo(() => {
    if (fixture) {
      const f = xrFixture(late, reply)
      return { crew: buildXrCrew(f), notices: noticesFrom(f.notices), loading: false, error: null }
    }
    const feeds: XrFeeds = { roster, asks, live: connected, rows }
    const crew = buildXrCrew({
      members: members.data ?? NONE,
      slots: slots ?? NONE,
      approvals: approvals.data ?? NONE,
      questions: questions.data ?? NONE,
      unread: unread ?? NONE,
      feeds,
      jobs: jobs.data ?? NONE,
    })
    return { crew, notices: noticesFrom(notes ?? NONE), loading: members.isPending, error: members.error }
  }, [fixture, late, reply, members.data, members.isPending, members.error, slots, approvals.data, questions.data, jobs.data, unread, notes, roster, asks, connected, rows])
}

export interface XrDetail {
  threads: Record<string, XrThread>
  glances: Record<string, XrGlance>
}

const PAGE_STATES: readonly string[] = ['live', 'stale', 'empty', 'error']

/** The feeds the open cards and panels need: transcripts by thread, goals and page state by Bot. */
export function useXrDetail(fixture: boolean, want: XrWant, crew: XrCrew): XrDetail {
  const live = !fixture
  const slots = useAppSelector(s => s.dashboard.slots)
  const bots = useMemo(() => crew.bots.filter(b => want.bots.includes(b.slug)), [crew.bots, want.bots])

  const threads = useQueries({
    queries: (live ? want.threads : NONE).map((slot, i) => {
      const row = slots?.find(s => s.key === slot)
      // The stamp changes when the thread does, so an open panel follows the conversation.
      const stamp = `${row?.last_activity_ts ?? row?.last_ts ?? ''}|${row?.running ? 1 : 0}`
      const limit = XR_THREAD_PAGE * (want.pages[i] ?? 1)
      return {
        queryKey: ['xr', 'thread', slot, limit, stamp],
        queryFn: async ({ signal }: { signal: AbortSignal }): Promise<XrThread> => {
          const r = await api.chatSlotDetail(slot, limit, undefined, signal)
          return { lines: linesFrom(r.messages, limit), more: !!r.has_more, state: 'ok' }
        },
        placeholderData: keepPreviousData,
        retry: 1,
      }
    }),
  })
  const goals = useQueries({
    queries: (live ? bots : NONE).map(b => ({
      queryKey: memberGoalsKey(b.slug, b.name),
      queryFn: () => api.memberGoals.get(b.slug, b.name),
      staleTime: 0,
      refetchInterval: 60_000,
      retry: false,
    })),
  })
  const briefings = useQueries({
    queries: (live ? bots : NONE).map(b => ({
      queryKey: memberBriefingQueryKey(b.slug, b.name),
      queryFn: () => api.memberBriefing(b.slug, b.name),
      refetchInterval: 60_000,
      retry: false,
    })),
  })
  const pages = useQueries({
    queries: (live ? bots : NONE).map(b => ({
      // Under the dashboard's own key, so the frames that refresh a Bot's page refresh this too.
      queryKey: ['member-dashboard', b.slug, 'xr-state'],
      // Only the page's state is kept. Its HTML is never used by the XR page.
      queryFn: async (): Promise<XrPageState> => {
        const d = await api.memberDashboard(b.slug, b.name)
        return d && PAGE_STATES.includes(d.state) ? (d.state as XrPageState) : 'empty'
      },
      staleTime: 30_000,
      retry: false,
    })),
  })

  const threadData = threads.map(t => t.data)
  const threadState = threads.map(feedOf).join()
  const goalData = goals.map(g => g.data)
  const goalState = goals.map(feedOf).join()
  const briefData = briefings.map(b => b.data)
  const briefState = briefings.map(feedOf).join()
  const pageData = pages.map(p => (p.isError ? ('error' as const) : p.data))

  return useMemo(() => {
    const out: XrDetail = { threads: {}, glances: {} }
    if (fixture) {
      want.threads.forEach((slot, i) => {
        const all = fixtureMessages(slot)
        const limit = XR_THREAD_PAGE * (want.pages[i] ?? 1)
        out.threads[slot] = { lines: linesFrom(all, limit), more: all.length > limit, state: 'ok' }
      })
      for (const b of crew.bots) out.glances[b.slug] = buildGlance(b, fixtureGoals(b.slug), fixtureBriefing(b.slug), fixturePage(b.slug))
      return out
    }
    const tStates = threadState.split(',') as XrFeedState[]
    want.threads.forEach((slot, i) => {
      const t = threadData[i]
      out.threads[slot] = t ? { ...t, state: tStates[i] === 'failed' ? 'failed' : 'ok' } : { lines: [], more: false, state: tStates[i] ?? 'loading' }
    })
    const gStates = goalState.split(',') as XrFeedState[]
    const bStates = briefState.split(',') as XrFeedState[]
    // Every Bot has a glance from the roster alone; goals and the briefing fill it in when they land.
    for (const b of crew.bots) {
      const i = bots.indexOf(b)
      const brief = i >= 0 ? briefData[i] : undefined
      out.glances[b.slug] = buildGlance(
        b,
        i >= 0 ? goalData[i] ?? null : null,
        brief ? { text: brief.text ?? '', updated_ts: brief.updated_ts ?? 0 } : null,
        i >= 0 ? pageData[i] ?? 'unknown' : 'unknown',
        { goals: i >= 0 ? gStates[i] ?? 'loading' : 'loading', briefing: i >= 0 ? bStates[i] ?? 'loading' : 'loading' },
      )
    }
    return out
    // The arrays are rebuilt each render; their items are the real inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fixture, want.threads, want.pages, crew.bots, bots, threadState, goalState, briefState, ...threadData, ...goalData, ...briefData, ...pageData])
}

/** The clock the scene dates things by: fixed for sample data, so pictures do not change by the minute. */
export const xrNow = (fixture: boolean) => (fixture ? () => FIXTURE_NOW_MS : () => Date.now())
