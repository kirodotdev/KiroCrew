/**
 * The crewmate's Heartbeat, on the Schedules tab of its Profile card.
 *
 * The Heartbeat wakes the mate every 30/60/120/240 minutes to move its own work
 * forward: it is the one thing that wakes the mate without a person or a goal
 * check-in asking, which is what the Schedules tab lists, so it lives there
 * (Zezhen Xu, 2026-10-22, issue #23262) and not among the Goals tab's
 * check-ins. Its switch and interval are still fields of the goal record's
 * `schedules` (`PUT /api/members/{slug}/goals/schedules`, one gateway-owned
 * cron per switch), so this row reads and writes the same record the Goals
 * tab does; with no goal the switch is disabled, as the check-ins are.
 */
import { useLayoutEffect, useRef } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { HeartPulse } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type GoalSchedules, type MemberGoals } from '../../api/client'
import { Toggle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { cn } from '../../lib/utils'
import { HEARTBEAT_INTERVALS, fmtInterval, memberGoalsKey, useMemberGoals, wakesPerDay } from './CrewGoals'

/** The heartbeat's options carry their daily bound, so the picker fits the longest one. */
const PICKER_STYLE = { width: '19rem', maxWidth: '100%' } as const

/** `onBusyChange` publishes an in-flight write into the Profile card's leave
 *  guard (the Goals tab's `onDraftStateChange`, same shape: this row never
 *  holds a draft, so `dirty` is always false), so a tab switch or a close
 *  cannot unmount the request and lose its failure notice. */
export default function CrewHeartbeatRow({ slug, member, onBusyChange }: { slug: string; member: string; onBusyChange?: (dirty: boolean, busy: boolean) => void }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const q = useMemberGoals(slug, member)
  // Synchronous latch, as the Goals tab's: a second flip in the same tick
  // cannot race the pending write.
  const busyRef = useRef(false)
  const save = useMutation({
    mutationFn: (changes: Partial<GoalSchedules>) => api.memberGoals.setSchedules(slug, member, changes),
    onSuccess: (data: MemberGoals) => { qc.setQueryData(memberGoalsKey(slug, member), data) },
    onError: () => { void qc.invalidateQueries({ queryKey: memberGoalsKey(slug, member) }) },
    onSettled: () => { busyRef.current = false },
  })
  const busy = save.isPending
  useLayoutEffect(() => { onBusyChange?.(false, busy) }, [busy, onBusyChange])
  useLayoutEffect(() => () => { onBusyChange?.(false, false) }, [onBusyChange])
  const set = (changes: Partial<GoalSchedules>) => {
    if (busyRef.current) return
    busyRef.current = true
    save.mutate(changes)
  }
  // The Schedules tab has no other surface for this record, so a failed read
  // is said here; nothing typed can be lost, so the hand-off is on.
  if (q.isError) return <ErrorNotice message={t('pages.membersPage.goals_load_failed')} askAgent testId="crew-heartbeat-load-error" />
  if (!q.data?.schedules) return null
  const sched = q.data.schedules
  const hasGoal = q.data.goals.length > 0
  const on = sched.heartbeat
  const label = t('pages.membersPage.goals_heartbeat')
  return (
    <div className="flex flex-col gap-2" data-testid="crew-heartbeat">
      {save.isError && <ErrorNotice message={t('pages.membersPage.goals_save_failed')} askAgent testId="crew-heartbeat-error" />}
      <div className="rounded-2xl border border-border bg-bg px-3.5 py-3 flex flex-col gap-2.5" data-testid="crew-heartbeat-row" data-on={on ? 'true' : 'false'}>
        <div className="flex items-center gap-3 min-w-0">
          <span className={cn('w-8 h-8 rounded-[9px] grid place-items-center shrink-0', on ? 'bg-ok-subtle text-ok-text' : 'bg-bg-hover text-muted')} aria-hidden="true"><HeartPulse size={15} /></span>
          <span className="flex-1 min-w-0 leading-tight">
            <span className={cn('block text-[13px] font-semibold', hasGoal ? 'text-text' : 'text-muted')}>{label}</span>
            <span className="block text-[12px] text-muted">
              {t('pages.membersPage.goals_heartbeat_hint', { interval: fmtInterval(sched.interval) })}
              {/* Where the Heartbeat is on, it also says what it costs. */}
              {on && <> · {t('pages.membersPage.goals_heartbeat_credits')} · {t('pages.membersPage.goals_heartbeat_per_day', { count: wakesPerDay(sched.interval) })}</>}
            </span>
            {!hasGoal && <span className="block text-[12px] text-muted">{t('pages.membersPage.goals_checkins_need_goal')}</span>}
          </span>
          <Toggle checked={on} disabled={!hasGoal || busy} onChange={v => set({ heartbeat: v })} label={label} />
        </div>
        {on && (
          <SimpleSelect
            options={HEARTBEAT_INTERVALS.map(String)}
            optionLabels={HEARTBEAT_INTERVALS.map(m => `${t('pages.membersPage.goals_heartbeat_every', { interval: fmtInterval(m) })} · ${t('pages.membersPage.goals_heartbeat_per_day', { count: wakesPerDay(m) })}`)}
            value={String(sched.interval)}
            onChange={v => set({ interval: Number(v) })}
            disabled={!hasGoal || busy}
            aria-label={t('pages.membersPage.goals_heartbeat_interval')}
            style={PICKER_STYLE}
          />
        )}
      </div>
    </div>
  )
}
