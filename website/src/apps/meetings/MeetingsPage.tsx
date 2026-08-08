// Meetings — entry point.
//
// Three views behind one route: the meeting list (upcoming from the configured
// calendar, plus every meeting this app has touched), one meeting's live
// workspace, and settings. The route is kept in component state rather than the
// URL because a builtin app resolves from a single top-level path segment (see
// `apps/builtinRegistry.ts`), so a nested path would never route back here.

import { useCallback, useMemo, useState, type MouseEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CalendarClock,
  CalendarPlus,
  CircleDot,
  FileText,
  RefreshCw,
  Settings2,
  X,
} from 'lucide-react'

import { i18nT } from '../../i18n/t'
import { fmtDateFields } from '../../i18n/format'
import Clickable from '../../components/Clickable'
import {
  Badge,
  Btn,
  Card,
  CardTitle,
  EmptyState,
  IconButton,
  PageHeader,
  SearchInput,
  Skeleton,
  StatCard,
} from '../../components/ui'
import { useDispatch } from 'react-redux'
import { addNotification } from '../../store/notificationsSlice'
import { meetingsApi, safeMeetingId, type CalendarEvent, type MeetingSummary } from './api'
import MeetingView from './MeetingView'
import SettingsView from './SettingsView'

type Route =
  | { view: 'list' }
  | { view: 'meeting'; eventId: string; title?: string }
  | { view: 'settings' }

/** A row in the unified list: either an upcoming calendar event or a meeting
 *  this app has already worked on. */
interface Row {
  eventId: string
  title: string
  start: string
  end: string
  status: MeetingSummary['status'] | 'scheduled'
  touched: boolean
}

const LIVE_STATUSES = new Set<Row['status']>(['active', 'paused', 'reviewing'])

function mergeRows(events: CalendarEvent[], meetings: MeetingSummary[]): Row[] {
  const byId = new Map<string, Row>()
  for (const event of events) {
    byId.set(safeMeetingId(event.event_id), {
      eventId: event.event_id,
      title: event.title,
      start: event.start,
      end: event.end,
      status: 'scheduled',
      touched: false,
    })
  }
  for (const meeting of meetings) {
    const key = safeMeetingId(meeting.event_id)
    const existing = byId.get(key)
    byId.set(key, {
      eventId: meeting.event_id,
      title: meeting.title || existing?.title || i18nT('apps.meetings.session.untitled'),
      start: existing?.start ?? meeting.started_at,
      end: existing?.end ?? meeting.ended_at,
      status: meeting.status,
      touched: true,
    })
  }
  // Byte comparison, NOT compareText: these are ISO-8601 timestamps, which sort
  // correctly as bytes and must not be reordered by a locale's collation rules.
  return [...byId.values()].sort((a, b) => {
    const [x, y] = [a.start || '', b.start || '']
    return x === y ? 0 : x < y ? 1 : -1
  })
}

function formatWhen(row: Row): string {
  if (!row.start) return ''
  const start = new Date(row.start)
  if (Number.isNaN(start.getTime())) return ''
  // `fmtDateFields`, not the raw `toLocale*`: a meeting row sits inside a
  // translated UI, so the weekday and the 12h/24h choice have to follow the app's
  // language rather than whatever locale the browser happens to be set to.
  const date = fmtDateFields(start, { weekday: 'short', month: 'short', day: 'numeric' })
  const time = fmtDateFields(start, { hour: '2-digit', minute: '2-digit' })
  return `${date} · ${time}`
}

/**
 * The notifier for this page.
 *
 * NOT the App SDK's `useNotify()`: that hook reads `AppSdkContext`, which only
 * exists inside `AppApiProvider` — the wrapper the host mounts around an
 * EXTERNALLY-loaded app bundle. An in-tree builtin page renders directly in the
 * core React tree with no such provider, so calling it throws
 * "useAppApi() must be used inside <AppApiProvider>" and the whole page renders
 * as an error boundary. `DevFleetPage` hit this first and carries the same
 * workaround; this dispatches to the notification store the header already reads.
 */
function useNotifier(): (message: string, opts?: { type?: 'info' | 'success' | 'error' }) => void {
  const dispatch = useDispatch()
  return useCallback(
    (message, opts) => {
      dispatch(
        addNotification({
          ts: String(Date.now()),
          title: message,
          body: '',
          kind: opts?.type === 'error' ? 'error' : opts?.type === 'success' ? 'success' : 'info',
        }),
      )
    },
    [dispatch],
  )
}

export default function MeetingsPage() {
  const notify = useNotifier()
  const queryClient = useQueryClient()
  const [route, setRoute] = useState<Route>({ view: 'list' })
  const [filter, setFilter] = useState('')

  const configQuery = useQuery({ queryKey: ['meetings', 'config'], queryFn: meetingsApi.config })
  const calendarQuery = useQuery({ queryKey: ['meetings', 'calendar'], queryFn: meetingsApi.calendar })
  const meetingsQuery = useQuery({ queryKey: ['meetings', 'list'], queryFn: meetingsApi.meetings })

  const sync = useMutation({
    mutationFn: meetingsApi.syncCalendar,
    onSuccess: response => {
      notify(i18nT('apps.meetings.list.synced', { count: response.count }), { type: 'success' })
      void queryClient.invalidateQueries({ queryKey: ['meetings', 'calendar'] })
    },
    onError: (error: Error) =>
      notify(error.message || i18nT('apps.meetings.list.syncFailed'), { type: 'error' }),
  })

  const deleteMeeting = useMutation({
    mutationFn: ({ eventId }: { eventId: string; title: string }) =>
      meetingsApi.deleteMeeting(eventId),
    onSuccess: (_response, { title }) => {
      notify(i18nT('apps.meetings.list.deleted', { title }), { type: 'success' })
      void queryClient.invalidateQueries({ queryKey: ['meetings', 'list'] })
    },
    onError: () =>
      notify(i18nT('apps.meetings.list.deleteFailed'), { type: 'error' }),
  })

  const rows = useMemo(
    () => mergeRows(calendarQuery.data?.events ?? [], meetingsQuery.data?.meetings ?? []),
    [calendarQuery.data, meetingsQuery.data],
  )

  const filtered = useMemo(() => {
    const query = filter.trim().toLowerCase()
    return query ? rows.filter(row => row.title.toLowerCase().includes(query)) : rows
  }, [rows, filter])

  const liveRow = rows.find(row => row.status === 'active' || row.status === 'reviewing')

  if (route.view === 'meeting') {
    return (
      <MeetingView
        eventId={route.eventId}
        fallbackTitle={route.title}
        config={configQuery.data?.config}
        onBack={() => {
          setRoute({ view: 'list' })
          void queryClient.invalidateQueries({ queryKey: ['meetings', 'list'] })
        }}
        onOpenSettings={() => setRoute({ view: 'settings' })}
        notify={notify}
      />
    )
  }

  if (route.view === 'settings') {
    return <SettingsView onBack={() => setRoute({ view: 'list' })} notify={notify} />
  }

  const startAdHoc = () => {
    // An ad-hoc meeting needs no calendar: its id is a timestamp slug, which is
    // already inside the backend's `[A-Za-z0-9._-]` charset.
    const eventId = `adhoc-${new Date().toISOString().replace(/[:.]/g, '-')}`
    setRoute({ view: 'meeting', eventId, title: i18nT('apps.meetings.list.adHocTitle') })
  }

  const requestDelete = (event: MouseEvent<HTMLButtonElement>, row: Row) => {
    event.stopPropagation()
    if (window.confirm(i18nT('apps.meetings.list.deleteConfirm', { title: row.title }))) {
      deleteMeeting.mutate({ eventId: row.eventId, title: row.title })
    }
  }

  return (
    <>
      <PageHeader
        title={i18nT('apps.meetings.list.title')}
        subtitle={i18nT('apps.meetings.list.subtitle')}
        actions={
          <>
            <Btn
              onClick={() => sync.mutate()}
              disabled={sync.isPending}
              aria-label={i18nT('apps.meetings.list.syncCalendar')}
            >
              <RefreshCw className={`lucide-inline ${sync.isPending ? 'animate-spin' : ''}`} />
              {i18nT('apps.meetings.list.syncCalendar')}
            </Btn>
            <Btn
              onClick={() => setRoute({ view: 'settings' })}
              aria-label={i18nT('apps.meetings.list.settings')}
            >
              <Settings2 className="lucide-inline" />
              {i18nT('apps.meetings.list.settings')}
            </Btn>
            <Btn primary onClick={startAdHoc} aria-label={i18nT('apps.meetings.list.newMeeting')}>
              <CalendarPlus className="lucide-inline" />
              {i18nT('apps.meetings.list.newMeeting')}
            </Btn>
          </>
        }
      />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] my-6">
          <StatCard
            label={i18nT('apps.meetings.list.statScheduled')}
            value={rows.filter(row => row.status === 'scheduled').length}
            accent
          />
          <StatCard
            label={i18nT('apps.meetings.list.statWorked')}
            value={rows.filter(row => row.touched).length}
          />
          <StatCard
            label={i18nT('apps.meetings.list.statLive')}
            value={rows.filter(row => row.status === 'active').length}
          />
        </div>

        {liveRow && (
          <Card className="mb-4 border-ok/40">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-text inline-flex items-center gap-2">
                <CircleDot className="lucide-inline text-ok" />
                {liveRow.status === 'reviewing'
                  ? i18nT('apps.meetings.list.reviewingBanner', { title: liveRow.title })
                  : i18nT('apps.meetings.list.liveBanner', { title: liveRow.title })}
              </span>
              <Btn
                primary
                onClick={() =>
                  setRoute({ view: 'meeting', eventId: liveRow.eventId, title: liveRow.title })
                }
              >
                {i18nT('apps.meetings.list.returnToMeeting')}
              </Btn>
            </div>
          </Card>
        )}

        <Card>
          <CardTitle>{i18nT('apps.meetings.list.sectionTitle')}</CardTitle>
          <SearchInput
            className="my-3"
            placeholder={i18nT('apps.meetings.list.filterPlaceholder')}
            aria-label={i18nT('apps.meetings.list.filterPlaceholder')}
            value={filter}
            onChange={e => setFilter(e.target.value)}
          />
          {calendarQuery.isLoading || meetingsQuery.isLoading ? (
            <Skeleton className="h-24" />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<CalendarClock className="lucide-inline" />}
              title={i18nT('apps.meetings.list.empty')}
              subtitle={
                calendarQuery.data?.configured
                  ? i18nT('apps.meetings.list.emptyHintSynced')
                  : i18nT('apps.meetings.list.emptyHintNoCalendar')
              }
            />
          ) : (
            <div className="flex flex-col gap-2">
              {filtered.map(row => (
                <Clickable
                  key={row.eventId}
                  onClick={() =>
                    setRoute({ view: 'meeting', eventId: row.eventId, title: row.title })
                  }
                  className="flex items-center gap-3 px-4 py-3 border border-border rounded-md cursor-pointer hover:border-border-strong transition-colors"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-text font-medium truncate">{row.title}</div>
                    {formatWhen(row) && (
                      <div className="text-[12px] text-muted font-mono">{formatWhen(row)}</div>
                    )}
                  </div>
                  {row.status === 'active' && (
                    <Badge variant="ok">{i18nT('apps.meetings.meeting.live')}</Badge>
                  )}
                  {row.status === 'reviewing' && (
                    <Badge variant="warn">{i18nT('apps.meetings.list.reviewing')}</Badge>
                  )}
                  {row.status === 'paused' && (
                    <Badge variant="warn">{i18nT('apps.meetings.meeting.paused')}</Badge>
                  )}
                  {row.status === 'ended' && (
                    <Badge variant="muted">{i18nT('apps.meetings.meeting.ended')}</Badge>
                  )}
                  {row.touched && (
                    <FileText
                      className="lucide-inline text-muted"
                      aria-label={i18nT('apps.meetings.list.hasNotes')}
                    />
                  )}
                  {row.touched && (
                    <IconButton
                      variant="danger"
                      className="shrink-0 [&>svg]:relative [&>svg]:top-[0.5px]"
                      disabled={deleteMeeting.isPending || LIVE_STATUSES.has(row.status)}
                      title={
                        LIVE_STATUSES.has(row.status)
                          ? i18nT('apps.meetings.list.deleteUnavailable')
                          : i18nT('apps.meetings.list.deleteMeeting', { title: row.title })
                      }
                      aria-label={
                        LIVE_STATUSES.has(row.status)
                          ? i18nT('apps.meetings.list.deleteUnavailable')
                          : i18nT('apps.meetings.list.deleteMeeting', { title: row.title })
                      }
                      onClick={event => requestDelete(event, row)}
                    >
                      {deleteMeeting.isPending && deleteMeeting.variables?.eventId === row.eventId
                        ? <RefreshCw className="lucide-inline animate-spin" aria-hidden="true" />
                        : <X className="lucide-inline" aria-hidden="true" />}
                    </IconButton>
                  )}
                </Clickable>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  )
}
