import { LayoutDashboard } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { membersRosterQuery } from '../../api/membersQuery'
import { useAppDispatch, useAppSelector } from '../../store'
import { deleteSlot } from '../../store/chatSlice'
import type { SubagentActivity } from '../../types'
import { buildCommandCenter, type RunState } from './command-center/model'
import { Badge, Btn, EmptyState, FilteredEmpty } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { isChatPageSurface } from '../../utils/channelOrigin'
import { useConfirm } from '../../components/ConfirmDialog'
import { errMessage } from '../../utils/thunkError'

type RowStatus = 'active' | 'blocked' | 'complete'

interface SessionRow {
  id: string
  kind: 'slot' | 'run'
  title: string
  drive: string
  status: RowStatus
  model: string
  template: string
  slotKey: string
  runId: string
}

/** `created_by` spellings (`member-<slug>`, `dashboard:member-<slug>`,
 *  `dashboard_member-<slug>`) all name the same member thread. */
function memberSlugOf(createdBy: string | undefined): string | null {
  if (!createdBy) return null
  const bare = createdBy.replace(/^dashboard:/, '').replace(/^dashboard_/, '')
  const m = /^member-(.+)$/.exec(bare)
  return m ? m[1] : null
}

const STATUS_LABEL_KEYS = {
  active: 'pages.panelSessions.status_active',
  blocked: 'pages.panelSessions.status_blocked',
  complete: 'pages.panelSessions.status_complete',
} as const

/** The fleet command center owns the status vocabulary; this tab only folds
 * it onto three filter chips. `idle` sessions and `waiting` (queued) runs
 * read as Active: they are live and will progress without anyone stepping
 * in, while Blocked means the session waits on a person. Documented here
 * and in rfc-sessions-overview-tab because the chip cannot say it. */
const STATE_TO_STATUS: Record<RunState, RowStatus> = {
  running: 'active',
  idle: 'active',
  waiting: 'active',
  needs_input: 'blocked',
  blocked: 'blocked',
  done: 'complete',
  stopped: 'complete',
}

/** All live units of work: chat slots plus every slot's subagent runs, with
 * statuses read off the command-center nodes rather than a second rule. */
export default function SessionsBody({ active }: { active: boolean }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const slots = useAppSelector(s => s.dashboard.slots)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  const liveSubs = useAppSelector(s => s.chat.subagents)
  const slotActivity = useAppSelector(s => s.chat.slotActivity)
  const roster = useQuery({ ...membersRosterQuery, enabled: active })
  const [filter, setFilter] = useState<'all' | RowStatus>('all')
  const [failure, setFailure] = useState<string | null>(null)

  const namesBySlug = useMemo<Record<string, string>>(() => {
    const map: Record<string, string> = {}
    for (const row of roster.data ?? []) map[row.slug] = row.name
    return map
  }, [roster.data])

  const driveOf = (createdBy: string | undefined): string => {
    const slug = memberSlugOf(createdBy)
    if (slug === null) return ''
    return namesBySlug[slug] ?? slug
  }

  // Chat-page rows only: `mode: 'member'` DM threads open on the Crew
  // Members page (`opensElsewhereOf`), so listing them here would offer an
  // Open that the chat page's `filteredSlots` (`isChatPageSurface`) leaves
  // out and never switches to. Same gate `SessionsPage` applies.
  const chatSlots = useMemo(
    () => slots.filter(s => isChatPageSurface(s.surface ?? s.mode)),
    [slots],
  )
  const rows = useMemo<SessionRow[]>(() => {
    // The per-slot assembly mirrors useCommandCenter: the viewed slot's runs
    // are live in `chat.subagents`, every other slot's in its activity
    // bucket. A run is therefore filed under its own slot, never the first.
    const subagents = Object.fromEntries(
      chatSlots.map(s => [s.key, s.key === activeSlot ? liveSubs : (slotActivity[s.key]?.subagents ?? {})]),
    )
    const { nodes } = buildCommandCenter({
      root: null,
      slots: chatSlots,
      subagents,
      workflows: [],
      questions: [],
      approvals: [],
    })
    const byId = new Map(nodes.map(n => [n.id, n]))
    const activityOf = (slotKey: string): Record<string, SubagentActivity> => subagents[slotKey] ?? {}
    const out: SessionRow[] = []
    for (const s of chatSlots) {
      const node = byId.get(`session:${s.key}`)
      out.push({
        id: `slot:${s.key}`,
        kind: 'slot',
        title: s.title || s.key,
        drive: driveOf(s.created_by),
        status: STATE_TO_STATUS[node?.state ?? 'idle'],
        model: s.model || s.served_model || '',
        template: s.effective_agent || s.agent || '',
        slotKey: s.key,
        runId: '',
      })
      for (const [id, a] of Object.entries(activityOf(s.key))) {
        const runNode = byId.get(`subagent:${a.id ?? id}`)
        out.push({
          id: `run:${id}`,
          kind: 'run',
          title: a.task || id,
          drive: driveOf(s.created_by),
          status: STATE_TO_STATUS[runNode?.state ?? 'running'],
          model: a.model || a.requestedModel || '',
          template: a.agent || '',
          slotKey: s.key,
          runId: id,
        })
      }
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatSlots, activeSlot, liveSubs, slotActivity, namesBySlug])

  const visible = filter === 'all' ? rows : rows.filter(r => r.status === filter)

  const jump = (row: SessionRow) => {
    if (row.kind === 'slot') navigate(`/chat?sid=${encodeURIComponent(row.slotKey)}`)
  }

  const { confirm, confirmDialog } = useConfirm()

  const close = async (row: SessionRow) => {
    setFailure(null)
    try {
      if (row.kind === 'slot') {
        const ok = await confirm({
          title: t('pages.panelSessions.close_slot_confirm', { title: row.title }),
          confirmLabel: t('pages.panelSessions.action_close'),
        })
        if (!ok) return
        // The sidebar's close: the live slot goes away and the transcript
        // stays in history. Never the history delete, which is permanent.
        await dispatch(deleteSlot(row.slotKey)).unwrap()
      } else {
        const ok = await confirm({
          title: t('pages.panelSessions.close_run_confirm'),
          confirmLabel: t('pages.panelSessions.action_stop_run'),
        })
        if (!ok) return
        await api.spawnDelete(row.runId)
      }
    } catch (e) {
      setFailure(errMessage(e) || t('pages.panelSessions.close_failed'))
    }
  }

  const statusLabel = (status: RowStatus) => t(STATUS_LABEL_KEYS[status])

  return (
    <div className="flex h-full flex-col gap-2 overflow-y-auto p-3">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label={t('pages.panelSessions.filter_label')}>
        {(['all', 'active', 'blocked', 'complete'] as const).map(f => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            aria-pressed={filter === f}
            className={`rounded-full border border-[var(--border)] px-2.5 py-1 text-[12px] cursor-pointer bg-transparent ${filter === f ? 'text-[var(--text)] border-[var(--accent)]' : 'text-[var(--muted)]'}`}
          >
            {f === 'all' ? t('pages.panelSessions.filter_all') : statusLabel(f)}
          </button>
        ))}
      </div>
      {failure && <ErrorNotice message={failure} askAgent />}
      {roster.error && <ErrorNotice message={errMessage(roster.error)} askAgent />}
      {visible.length === 0 ? (
        filter === 'all' ? (
          <EmptyState
            icon={<LayoutDashboard size={20} />}
            title={t('pages.panelSessions.empty_title')}
            subtitle={t('pages.panelSessions.empty_hint')}
          />
        ) : (
          <FilteredEmpty query={statusLabel(filter)} onClear={() => setFilter('all')} />
        )
      ) : (
        <ul className="flex flex-col gap-1.5">
          {visible.map(row => (
            <li
              key={row.id}
              className="rounded-md border border-[var(--border)] bg-[var(--card)] p-2.5"
            >
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  onClick={() => jump(row)}
                  disabled={row.kind !== 'slot'}
                  title={row.title}
                  className="min-w-0 flex-1 truncate text-left text-[13px] text-[var(--text)] bg-transparent border-none p-0 cursor-pointer disabled:cursor-default"
                >
                  {row.title}
                </button>
                <Badge variant={row.status === 'blocked' ? 'warn' : row.status === 'complete' ? 'muted' : 'ok'}>
                  {statusLabel(row.status)}
                </Badge>
              </div>
              <dl className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 text-[12px]">
                {row.drive && (
                  <>
                    <dt className="text-[var(--muted)]">{t('pages.panelSessions.col_drive')}</dt>
                    <dd className="truncate text-[var(--text)]">{row.drive}</dd>
                  </>
                )}
                {row.model && (
                  <>
                    <dt className="text-[var(--muted)]">{t('pages.panelSessions.col_model')}</dt>
                    <dd className="truncate text-[var(--text)]">{row.model}</dd>
                  </>
                )}
                {row.template && (
                  <>
                    <dt className="text-[var(--muted)]">{t('pages.panelSessions.col_template')}</dt>
                    <dd className="truncate text-[var(--text)]">{row.template}</dd>
                  </>
                )}
              </dl>
              <div className="mt-1.5 flex items-center gap-2">
                {row.kind === 'slot' && (
                  <Btn type="button" onClick={() => jump(row)}>
                    {t('pages.panelSessions.action_jump')}
                  </Btn>
                )}
                <Btn type="button" danger onClick={() => void close(row)}>
                  {row.kind === 'slot' ? t('pages.panelSessions.action_close') : t('pages.panelSessions.action_stop_run')}
                </Btn>
              </div>
            </li>
          ))}
        </ul>
      )}
      {confirmDialog}
    </div>
  )
}
