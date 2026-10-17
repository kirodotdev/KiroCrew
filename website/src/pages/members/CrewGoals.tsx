import { useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { SendReceiptStatus } from '../../chat-core/transport/sendTurn'
import { deliveryUnconfirmedCopy } from '../../chat-core/transport/receiptCopy'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { AlarmClock, CalendarDays, CalendarRange, ChevronLeft, ChevronRight, FileText, Flag, Goal, MessageSquare, Pencil, Plus, Sun, Trash2, Undo2, type LucideIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api, type GoalKeyResult, type GoalSchedules, type MemberGoal, type MemberGoals } from '../../api/client'
import { Btn, Input, Toggle } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { MEMBER_GOALS_QUERY_PREFIX } from '../../api/membersQuery'
import { fmtRelative, fmtWeekday } from '../../i18n/format'
import { cn } from '../../lib/utils'
import { ProfilePagePushContext } from './CrewProfilePanel'

/** On the wrapper, not the trigger: on touch the native `<select>` is `w-full`
 *  inside a block wrapper, so a width on the control left its chevron at the
 *  row's far edge, and a fixed height clipped the 16px touch text. */
const PICKER_STYLE = { width: '11rem' } as const

/** The query key every view of one crewmate's goal record shares. */
export const memberGoalsKey = (slug: string, member: string) => [...MEMBER_GOALS_QUERY_PREFIX, slug, member] as const

export function useMemberGoals(slug: string, member: string, enabled = true) {
  return useQuery({
    queryKey: memberGoalsKey(slug, member),
    queryFn: () => api.memberGoals.get(slug, member),
    enabled: enabled && Boolean(slug && member),
    // The crewmate writes readings and reports, and an applied card writes the
    // goal, outside this tab: read it fresh each time it opens, not once.
    staleTime: 0,
    refetchOnMount: 'always',
  })
}

/** How far a reading is toward its target, 0..1, or null when it cannot be told.
 *  Numbers compare as numbers ("19" of "27"); a word target ("yes") is reached
 *  when the reading is that word. */
export function keyResultProgress(kr: Pick<GoalKeyResult, 'current' | 'target'>): number | null {
  if (!kr.current) return null
  const cur = parseFloat(kr.current)
  const tgt = parseFloat(kr.target)
  if (Number.isFinite(cur) && Number.isFinite(tgt) && tgt !== 0) return Math.max(0, Math.min(1, cur / tgt))
  return kr.current.trim().toLowerCase() === kr.target.trim().toLowerCase() ? 1 : 0
}

type Tone = 'accent' | 'info' | 'ok' | 'warn' | 'quiet'
const TONE_TILE: Record<Tone, string> = {
  accent: 'bg-accent-subtle text-accent',
  info: 'bg-info-subtle text-info-text',
  ok: 'bg-ok-subtle text-ok-text',
  warn: 'bg-warn-subtle text-warn-text',
  quiet: 'bg-bg-hover text-muted',
}

/** A card's small caps label, led by its icon on a tinted tile centred on
 *  the label (the tile's box, not the glyph's weight, sets the alignment). */
function SectionLabel({ title, icon: Icon, tone, trailing }: { title: string; icon: LucideIcon; tone: Tone; trailing?: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className={cn('w-5 h-5 rounded-md grid place-items-center shrink-0', TONE_TILE[tone])} aria-hidden="true" data-testid="goal-section-icon">
        <Icon size={12} />
      </span>
      <span className="text-[11px] leading-none font-semibold tracking-wide uppercase text-muted">{title}</span>
      <span className="flex-1" />
      {trailing && <span className="-my-2 flex items-center">{trailing}</span>}
    </div>
  )
}

/** One card of the first page, in the Profile tab's language: a hairline
 *  rounded card that SHOWS state. A card with `onOpen` is a door: the whole
 *  card highlights on hover and opens its page, where the editing lives. */
function Section({ title, icon, tone, children, testId, trailing, flush, onOpen, openLabel, openTestId }: {
  title: string
  icon: LucideIcon
  tone: Tone
  children: ReactNode
  testId: string
  trailing?: ReactNode
  flush?: boolean
  onOpen?: () => void
  openLabel?: string
  openTestId?: string
}) {
  const label = <SectionLabel title={title} icon={icon} tone={tone} trailing={trailing} />
  if (onOpen) {
    return (
      <section className="rounded-2xl border border-border bg-bg overflow-hidden" data-testid={testId}>
        <button type="button" onClick={onOpen} aria-label={openLabel} className="block w-full text-left px-3.5 pt-3.5 pb-3 hover:bg-bg-hover transition-colors cursor-pointer focus-ring" data-testid={openTestId}>
          <div className="mb-2.5">{label}</div>
          {children}
        </button>
      </section>
    )
  }
  return (
    <section className={cn('rounded-2xl border border-border bg-bg', flush ? 'overflow-hidden' : 'px-3.5 pt-3.5 pb-3')} data-testid={testId}>
      <div className={flush ? 'mx-3.5 mt-3.5 mb-2.5' : 'mb-2.5'}>{label}</div>
      {children}
    </section>
  )
}

/** The icon tile (the Profile tab's Row/Tile tile). */
function RowTile({ children, tone, size = 'md' }: { children: ReactNode; tone: Tone; size?: 'sm' | 'md' }) {
  return <span className={cn('rounded-[9px] grid place-items-center shrink-0', size === 'sm' ? 'w-7 h-7' : 'w-8 h-8', TONE_TILE[tone])} aria-hidden="true">{children}</span>
}

/** The goal colours: a muted palette (index.css `--goal-*`), none of them the
 *  ok/warn green and yellow. A goal's colour is its place in the list, cycling
 *  past five. */
export const GOAL_COLORS = ['var(--goal-1)', 'var(--goal-2)', 'var(--goal-3)', 'var(--goal-4)', 'var(--goal-5)'] as const
export const goalColor = (index: number) => GOAL_COLORS[((index % GOAL_COLORS.length) + GOAL_COLORS.length) % GOAL_COLORS.length]

/** A goal's coloured dot. Screen readers and the hover `title` (the goal's
 *  text) name it; the colour is the visible mark. */
function GoalNumber({ goal, size = 'md', title }: { goal: Pick<MemberGoal, 'number'>; size?: 'sm' | 'md'; title?: string }) {
  const { t } = useTranslation()
  return (
    <span
      role="img"
      aria-label={t('pages.membersPage.goals_goal_number', { number: goal.number })}
      title={title}
      className={cn('inline-block rounded-full shrink-0', size === 'sm' ? 'w-2 h-2' : 'w-2.5 h-2.5')}
      style={{ background: goalColor(goal.number - 1) }}
      data-testid="goal-number"
      data-goal-number={goal.number}
    />
  )
}

/** The circles of the goals a milestone serves; nothing when it serves none. */
function LinkedGoals({ kr, goals }: { kr: GoalKeyResult; goals: MemberGoal[] }) {
  const linked = goals.filter(g => (kr.goal_ids ?? []).includes(g.id))
  if (!linked.length) return null
  return (
    <span className="inline-flex items-center gap-1 ml-1.5 align-[-2px]" data-testid="goal-key-result-goals">
      {linked.map(g => <GoalNumber key={g.id} goal={g} size="sm" title={g.text} />)}
    </span>
  )
}

/** "Aiming for · 5 releases · 2d ago". */
function AimLine({ kr, className }: { kr: GoalKeyResult; className: string }) {
  const { t } = useTranslation()
  return (
    <span className={className}>
      {t('pages.membersPage.goals_kr_target')} · {kr.target}
      {kr.updated_at ? <> · {fmtRelative(kr.updated_at * 1000)}</> : null}
    </span>
  )
}

/** One milestone on the first page: what it is, the latest reading in the
 *  crewmate's own words, what it is aiming for. A door to its page. */
function KeyResultRow({ kr, goals, onOpen }: { kr: GoalKeyResult; goals: MemberGoal[]; onOpen: () => void }) {
  return (
    <li className="border-t border-border" data-testid="goal-key-result">
      <button type="button" onClick={onOpen} className="block w-full px-3.5 py-3 text-left hover:bg-bg-hover transition-colors cursor-pointer focus-ring" data-testid="goal-key-result-open">
        <span className="block text-[13px] font-semibold text-text leading-snug">{kr.text}<LinkedGoals kr={kr} goals={goals} /></span>
        {kr.current ? <span className="block mt-0.5 text-[13px] text-text leading-snug whitespace-pre-wrap">{kr.current}</span> : null}
        <AimLine kr={kr} className="block mt-1 text-[12px] text-muted leading-snug" />
      </button>
    </li>
  )
}

/** A milestone's page: the latest reading large, what it aims for, every
 *  earlier update, and the edit and remove controls. */
function KeyResultPage({ kr, goals, actions }: { kr: GoalKeyResult; goals: MemberGoal[]; actions: ReactNode }) {
  const { t } = useTranslation()
  const readings = [...(kr.readings ?? [])].reverse().slice(1)
  return (
    <div className="flex flex-col gap-3" data-testid="crew-goals-kr-page">
      <section className="rounded-2xl border border-border bg-bg px-4 py-4">
        <div className="text-[15px] font-semibold text-text-strong leading-snug">{kr.text}<LinkedGoals kr={kr} goals={goals} /></div>
        {kr.current ? <div className="mt-2 text-[17px] font-semibold text-text leading-snug whitespace-pre-wrap" data-testid="crew-goals-kr-current">{kr.current}</div> : null}
        <AimLine kr={kr} className="block mt-2 text-[12.5px] text-muted leading-snug" />
      </section>
      {readings.length > 0 && (
        <section className="rounded-2xl border border-border bg-bg px-3.5 pt-3.5 pb-3">
          <div className="text-[11px] leading-none font-semibold tracking-wide uppercase text-muted mb-2.5" aria-hidden="true">{t('pages.membersPage.goals_kr_history')}</div>
          <ol className="relative flex flex-col gap-2 border-l border-border pl-3 ml-0.5" aria-label={t('pages.membersPage.goals_kr_history')}>
            {readings.map((r, i) => (
              <li key={r.at + ':' + i} className="relative">
                <span className="absolute -left-[17px] top-1.5 w-2 h-2 rounded-full bg-border" aria-hidden="true" />
                <span className="block text-[12.5px] text-text whitespace-pre-wrap">{r.value}</span>
                <span className="block text-[11.5px] text-muted">{fmtRelative(r.at * 1000)}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
      {actions}
    </div>
  )
}

/** One check-in on the first page: on or off, and when it runs. */
function CheckInTile({ icon: Icon, tone, label, status, on, testId }: { icon: LucideIcon; tone: Tone; label: string; status: string; on: boolean; testId: string }) {
  return (
    <span className="flex flex-col items-start gap-1.5 rounded-xl border border-border p-2.5 min-w-0" data-testid={testId} data-on={on ? 'true' : 'false'}>
      <RowTile tone={on ? tone : 'quiet'} size="sm"><Icon size={15} /></RowTile>
      <span className={cn('text-[12.5px] font-semibold leading-tight line-clamp-2', on ? 'text-text' : 'text-muted')}>{label}</span>
      <span className="text-[11.5px] text-muted truncate w-full">{status}</span>
    </span>
  )
}

function Reports({ goals, limit }: { goals: MemberGoals; limit: number }) {
  const { t } = useTranslation()
  if (!goals.reports.length) return <p className="px-3.5 pb-3 text-[13px] text-muted">{t('pages.membersPage.goals_reports_none')}</p>
  return (
    <ul data-testid="goal-reports">
      {goals.reports.slice(0, limit).map(r => (
        <li key={r.slug} className="border-t border-border">
          <Link to={'/artifacts/' + encodeURIComponent(r.slug)} className="flex items-center gap-3 px-3.5 py-2.5 hover:bg-bg-hover transition-colors focus-ring">
            <span className="flex-1 min-w-0 leading-tight">
              <span className="block text-[13px] font-semibold text-text truncate">{r.title}</span>
              <span className="block text-[12px] text-muted">
                {t(r.kind === 'weekly' ? 'pages.membersPage.goals_weekly' : 'pages.membersPage.goals_monthly')} · {fmtRelative(r.at * 1000)}
              </span>
            </span>
            <ChevronRight size={15} className="text-muted shrink-0" aria-hidden="true" />
          </Link>
        </li>
      ))}
    </ul>
  )
}

const WEEKDAYS = [1, 2, 3, 4, 5, 6, 0] as const
const MONTH_DAYS = Array.from({ length: 28 }, (_, i) => i + 1)

/** The page pushed over the first page: one goal (`id: null` is a new one),
 *  one milestone, or the check-in settings. */
type GoalsPage = { kind: 'goal'; id: string | null } | { kind: 'kr'; id: string } | { kind: 'checkins' } | { kind: 'track' }

/** The crewmate's Goals tab. The first page only SHOWS state: the goals, the
 *  milestones and which goals each serves, which check-ins are on, the saved
 *  reports. Every edit and every detail is a page pushed over it (the Profile
 *  tab's About and Notes move the same way). What the crewmate is doing right
 *  now is its task list, not part of the goals: "Add something {name} should track" asks
 *  the crewmate to track an item there, and nothing tracked shows on this tab.
 *  `onTrackInChat` sends that ask as the user's own message in the crewmate's
 *  thread and returns the transport's delivery verdict. */
export default function CrewGoalsPane({ slug, member, name, onTrackInChat, onTracked, onDraftStateChange, onRequestLeave }: {
  slug: string
  member: string
  name: string
  onTrackInChat?: (message: string) => Promise<SendReceiptStatus>
  onTracked?: () => void
  onDraftStateChange?: (dirty: boolean, busy: boolean) => void
  onRequestLeave?: (proceed: () => void) => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const q = useMemberGoals(slug, member)
  const goals = q.data
  const push = useContext(ProfilePagePushContext)
  const [page, setPage] = useState<GoalsPage | null>(null)
  // The page last opened, kept while the card slides it away so it does not go
  // blank mid-slide. The host goes away when the slide ends.
  const [shown, setShown] = useState<GoalsPage | null>(null)
  // Each draft remembers what it was started from: its save compares against
  // that (the routes' compare-and-set), never against whatever the list holds by
  // the time Save is pressed, so a change made meanwhile is refused, not overwritten.
  const [editing, setEditing] = useState<{ text: string; expected: string } | null>(null)
  const [krEdit, setKrEdit] = useState<{
    id: string
    text: string
    target: string
    /** The whole set as it read when editing began, ids included. */
    base: { id: string; text: string; target: string }[]
  } | null>(null)
  const timeId = useId()
  const [tracking, setTracking] = useState<'idle' | 'sending' | 'failed' | 'unconfirmed'>('idle')
  const [trackText, setTrackText] = useState('')
  // Synchronous latch: a second click or an exit in the same tick cannot abandon
  // a request before React has committed its pending state.
  const busyRef = useRef(false)
  const save = useMutation({
    mutationFn: (fn: () => Promise<MemberGoals>) => fn(),
    onSuccess: data => { qc.setQueryData(memberGoalsKey(slug, member), data) },
    // A failed save keeps the draft and its original compare-and-set baseline.
    onError: () => { void qc.invalidateQueries({ queryKey: memberGoalsKey(slug, member) }) },
    onSettled: () => { busyRef.current = false },
  })
  const krBase = krEdit?.base.find(k => k.id === krEdit.id)
  const dirty = !!(editing && editing.text !== editing.expected)
    || !!(krEdit && (krEdit.text !== krBase?.text || krEdit.target !== krBase?.target))
    || !!trackText
  const busy = save.isPending || tracking === 'sending'
  useLayoutEffect(() => { onDraftStateChange?.(dirty, busy) }, [dirty, busy, onDraftStateChange])
  useLayoutEffect(() => () => { onDraftStateChange?.(false, false) }, [onDraftStateChange])
  const requestLeave = (proceed: () => void) => {
    if (busyRef.current || busy) return
    if (onRequestLeave) onRequestLeave(proceed)
    else proceed()
  }
  const trackInChat = async () => {
    const text = trackText.trim()
    if (!onTrackInChat || busyRef.current || !text) return
    busyRef.current = true
    onDraftStateChange?.(dirty, true)
    setTracking('sending')
    let status: SendReceiptStatus
    try {
      status = await onTrackInChat(t('pages.membersPage.goals_track_message', { text }))
    } catch {
      // A rejected callback cannot prove the request never reached the gateway.
      status = 'transport-error'
    }
    busyRef.current = false
    if (status === 'dispatched' || status === 'queued') {
      setTracking('idle')
      closePage()
      onDraftStateChange?.(false, false)
      onTracked?.()
    } else {
      setTracking(status === 'refused' ? 'failed' : 'unconfirmed')
      onDraftStateChange?.(dirty, false)
    }
  }
  // A successful write alone clears its draft. Serialize the writes so one
  // settled mutation cannot release the guard for another pending request.
  const run = (fn: () => Promise<MemberGoals>, onSaved?: () => void) => {
    if (busyRef.current) return
    busyRef.current = true
    onDraftStateChange?.(dirty, true)
    save.mutate(fn, { onSuccess: () => onSaved?.() })
  }

  const pageKr = page?.kind === 'kr' ? goals?.key_results.find(k => k.id === page.id) : undefined
  const pageGoal = page?.kind === 'goal' && page.id ? goals?.goals.find(g => g.id === page.id) : undefined
  const pageTitle = !page ? ''
    : page.kind === 'goal' ? (pageGoal ? t('pages.membersPage.goals_goal_number', { number: pageGoal.number }) : t('pages.membersPage.goals_new_goal'))
      : page.kind === 'checkins' ? t('pages.membersPage.goals_checkins')
        : page.kind === 'track' ? t('pages.membersPage.goals_track_title')
        : pageKr?.text ?? ''

  // The page's goal or milestone is gone (removed here or elsewhere): go back to
  // the first page, where the Undo row is. Retitling instead would re-push the
  // page as "New goal" in the same moment the removal closes it.
  const pageGone = (page?.kind === 'goal' && !!page.id && !!goals && !pageGoal)
    || (page?.kind === 'kr' && !!goals && !pageKr)

  const clearDraft = () => {
    setPage(null)
    setEditing(null)
    setKrEdit(null)
    setTrackText('')
    setTracking('idle')
  }
  // The Profile host asks its leave guard before invoking this pop callback.
  useEffect(() => {
    if (!page || !push || pageGone) return
    push.open(pageTitle, clearDraft)
  }, [page, pageTitle, push, pageGone])

  // Undo is offered only for a removal made while this tab is open: close and
  // reopen it and the row is gone (the record still holds the removed item).
  const [undoable, setUndoable] = useState<{ goal?: string; kr?: string }>({})

  const openPage = (next: GoalsPage) => {
    if (busyRef.current) return
    save.reset()
    setPage(next)
    setShown(next)
  }
  const closePage = () => {
    clearDraft()
    push?.close()
  }
  // An unsaved draft outlives its record: the page stays open showing that the
  // item is gone, and the text is cleared only by an explicit Discard (a removal
  // made here closes the page through its own success callback).
  useEffect(() => { if (pageGone && !dirty) closePage() })

  if (q.isError) return <ErrorNotice message={t('pages.membersPage.goals_load_failed')} askAgent testId="crew-goals-error" />
  if (!goals) return <p role="status" className="text-[13px] text-muted">{t('pages.membersPage.goals_loading')}</p>

  const hasGoal = goals.goals.length > 0
  const removedKr = goals.removed_key_result && goals.removed_key_result.id === undoable.kr ? goals.removed_key_result : null
  const removedGoal = goals.removed_goal && goals.removed_goal.id === undoable.goal ? goals.removed_goal : null
  const setSchedule = (changes: Partial<GoalSchedules>) => run(() => api.memberGoals.setSchedules(slug, member, changes))
  const sched = goals.schedules
  const anyCheckIn = sched.daily || sched.weekly || sched.monthly
  const off = t('pages.membersPage.goals_checkin_off')
  const openGoal = (goal?: MemberGoal) => {
    setEditing({ text: goal?.text ?? '', expected: goal?.text ?? '' })
    openPage({ kind: 'goal', id: goal?.id ?? null })
  }

  // No hand-off: an open goal or milestone draft holds unsaved text that
  // navigation would lose. Check-in, Undo and draft-free removal failures can hand off.
  const saveError = save.isError && <ErrorNotice message={t('pages.membersPage.goals_save_failed')} askAgent={!editing && !krEdit} testId="crew-goals-save-error" />

  const checkRow = (opts: { label: string; checked: boolean; onChange: (v: boolean) => void; extra?: ReactNode }) => (
    <div className="border-t border-border first:border-t-0 px-3.5 py-2.5">
      <div className="flex items-center gap-3">
        <span className={cn('flex-1 min-w-0 text-[13px]', hasGoal ? 'text-text' : 'text-muted')}>{opts.label}</span>
        <Toggle checked={opts.checked} disabled={!hasGoal} onChange={opts.onChange} label={opts.label} />
      </div>
      {opts.checked && opts.extra && <div className="mt-2">{opts.extra}</div>}
    </div>
  )

  const goalPage = (goal: MemberGoal | undefined) => {
    const linked = goal ? goals.key_results.filter(k => (k.goal_ids ?? []).includes(goal.id)) : []
    const saveGoal = () => {
      if (!editing) return
      const { text, expected } = editing
      run(() => api.memberGoals.setGoal(slug, member, text, goal?.id, goal ? expected : undefined), closePage)
    }
    return (
      <div className="flex flex-col gap-3" data-testid="crew-goals-goal-page">
        {saveError}
        {!goal && (
          <section className="rounded-2xl border border-dashed border-border px-4 py-5 flex flex-col items-center gap-2.5 text-center" data-testid="crew-goals-new-goal-intro">
            <RowTile tone="accent"><Goal size={17} /></RowTile>
            <p className="text-[13px] text-muted max-w-[30ch]">{t('pages.membersPage.goals_intro', { name })}</p>
          </section>
        )}
        <section className="rounded-2xl border border-border bg-bg px-3.5 py-3.5 flex flex-col gap-2" data-testid="crew-goals-long-term-editor">
          {goal && <div className="flex items-center gap-2"><GoalNumber goal={goal} /><span className="text-[12px] text-muted">{t('pages.membersPage.goals_goal_number', { number: goal.number })}</span></div>}
          <textarea
            value={editing?.text ?? ''}
            disabled={busy}
            onChange={e => setEditing({ text: e.target.value, expected: editing?.expected ?? goal?.text ?? '' })}
            maxLength={goals.limits.goal_text_max_chars}
            rows={5}
            aria-label={goal ? t('pages.membersPage.goals_goal_number', { number: goal.number }) : t('pages.membersPage.goals_new_goal')}
            className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus-ring"
            data-testid="crew-goals-long-term-input"
          />
          <div className="flex gap-2 justify-end">
            {goal && (
              <Btn type="button" onClick={() => run(async () => { const r = await api.memberGoals.removeGoal(slug, member, goal.id); setUndoable(u => ({ ...u, goal: goal.id })); return r }, closePage)} className="gap-1.5 whitespace-nowrap text-danger mr-auto" data-testid="crew-goals-goal-remove">
                <Trash2 size={13} aria-hidden="true" />
                {t('pages.membersPage.goals_remove')}
              </Btn>
            )}
            <Btn type="button" onClick={() => requestLeave(closePage)}>{t('pages.membersPage.create_cancel')}</Btn>
            <Btn type="button" primary disabled={!(editing?.text ?? '').trim()} onClick={saveGoal} data-testid="crew-goals-long-term-save">
              {t('pages.membersPage.goals_save')}
            </Btn>
          </div>
        </section>
        {goal && (
          <section className="rounded-2xl border border-border bg-bg overflow-hidden" data-testid="crew-goals-goal-milestones">
            <div className="mx-3.5 mt-3.5 mb-2.5"><SectionLabel title={t('pages.membersPage.goals_linked_milestones')} icon={Flag} tone="ok" /></div>
            {linked.length === 0 ? (
              <p className="px-3.5 pb-3 text-[13px] text-muted">{t('pages.membersPage.goals_linked_none')}</p>
            ) : (
              <ul>
                {linked.map(kr => (
                  <li key={kr.id} className="border-t border-border px-3.5 py-2.5" data-testid="crew-goals-goal-milestone">
                    <span className="block text-[13px] font-semibold text-text leading-snug">{kr.text}</span>
                    <AimLine kr={kr} className="block mt-0.5 text-[12px] text-muted leading-snug" />
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
      </div>
    )
  }

  const krPage = (kr: GoalKeyResult) => (
    <>
      {saveError}
      <KeyResultPage
        kr={kr}
        goals={goals.goals}
        actions={krEdit?.id === kr.id ? (
          <section className="rounded-2xl border border-border bg-bg px-3.5 py-3.5 flex flex-col gap-1.5" data-testid="crew-goals-kr-editor">
            <label className="flex flex-col gap-1 text-[12px] text-muted">
              {t('pages.membersPage.goals_kr_text')}
              <Input disabled={busy} value={krEdit.text} maxLength={goals.limits.key_result_text_max_chars} onChange={e => setKrEdit({ ...krEdit, text: e.target.value })} />
            </label>
            <label className="flex flex-col gap-1 text-[12px] text-muted">
              {t('pages.membersPage.goals_kr_target')}
              <Input disabled={busy} value={krEdit.target} maxLength={goals.limits.key_result_value_max_chars} onChange={e => setKrEdit({ ...krEdit, target: e.target.value })} />
            </label>
            <div className="flex gap-2 justify-end">
              <Btn type="button" onClick={() => requestLeave(() => setKrEdit(null))}>{t('pages.membersPage.create_cancel')}</Btn>
              <Btn type="button" primary onClick={() => {
                const { base } = krEdit
                // Each milestone keeps its id, so an edit to its wording keeps its readings.
                const items = base.map(k => (k.id === krEdit.id ? { id: k.id, text: krEdit.text, target: krEdit.target } : { id: k.id, text: k.text, target: k.target }))
                const expected = base.map(k => ({ text: k.text, target: k.target }))
                run(() => api.memberGoals.setKeyResults(slug, member, items, expected), () => setKrEdit(null))
              }}>{t('pages.membersPage.goals_save')}</Btn>
            </div>
          </section>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Btn type="button" onClick={() => setKrEdit({ id: kr.id, text: kr.text, target: kr.target, base: goals.key_results.map(k => ({ id: k.id, text: k.text, target: k.target })) })} className="gap-1.5 whitespace-nowrap">
              <Pencil size={13} aria-hidden="true" />
              {t('pages.membersPage.goals_kr_edit')}
            </Btn>
            <Btn type="button" onClick={() => run(async () => { const r = await api.memberGoals.removeKeyResult(slug, member, kr.id); setUndoable(u => ({ ...u, kr: kr.id })); return r }, closePage)} className="gap-1.5 whitespace-nowrap text-danger" data-testid="crew-goals-kr-remove">
              <Trash2 size={13} aria-hidden="true" />
              {t('pages.membersPage.goals_kr_remove')}
            </Btn>
          </div>
        )}
      />
    </>
  )

  const checkinsPage = (
    <div className="flex flex-col gap-3" data-testid="crew-goals-checkins-page">
      {saveError}
      {!hasGoal && <p className="text-[12.5px] text-muted px-1">{t('pages.membersPage.goals_checkins_need_goal')}</p>}
      <section className="rounded-2xl border border-border bg-bg overflow-hidden">
        {checkRow({
          label: t('pages.membersPage.goals_daily'),
          checked: sched.daily, onChange: v => setSchedule({ daily: v }),
        })}
        {checkRow({
          label: t('pages.membersPage.goals_weekly'),
          checked: sched.weekly, onChange: v => setSchedule({ weekly: v }),
          extra: (
            <SimpleSelect
              options={WEEKDAYS.map(String)}
              optionLabels={WEEKDAYS.map(d => fmtWeekday(d === 0 ? 7 : d, 'long'))}
              value={String(sched.weekday)}
              onChange={v => setSchedule({ weekday: Number(v) })}
              disabled={!hasGoal || !sched.weekly}
              aria-label={t('pages.membersPage.goals_weekday')}
              style={PICKER_STYLE}
            />
          ),
        })}
        {checkRow({
          label: t('pages.membersPage.goals_monthly'),
          checked: sched.monthly, onChange: v => setSchedule({ monthly: v }),
          extra: (
            <SimpleSelect
              options={MONTH_DAYS.map(String)}
              optionLabels={MONTH_DAYS.map(day => t('pages.membersPage.goals_month_day_option', { day }))}
              value={String(sched.month_day)}
              onChange={v => setSchedule({ month_day: Number(v) })}
              disabled={!hasGoal || !sched.monthly}
              aria-label={t('pages.membersPage.goals_month_day')}
              style={PICKER_STYLE}
            />
          ),
        })}
        {anyCheckIn && (
          <label htmlFor={timeId} className="flex items-center gap-3 border-t border-border px-3.5 py-2.5 text-[13px] text-text">
            <span className="flex-1">{t('pages.membersPage.goals_time')}</span>
            <input
              id={timeId}
              type="time"
              aria-label={t('pages.membersPage.goals_time')}
              value={sched.time}
              onChange={e => { if (e.target.value) setSchedule({ time: e.target.value }) }}
              className="h-8 rounded-lg border border-border bg-bg px-2 text-[13px] text-text focus-ring"
              data-testid="crew-goals-time"
            />
          </label>
        )}
      </section>
    </div>
  )

  const trackPage = (
    <div className="flex flex-col gap-3" data-testid="crew-goals-track-page">
      {/* No hand-off: the text the user wants tracked is still unsent in the box
          below; a hand-off would open a chat and leave that draft behind. */}
      {tracking === 'failed' && <ErrorNotice message={t('pages.membersPage.goals_track_failed')} testId="crew-goals-track-error" />}
      {tracking === 'unconfirmed' && <ErrorNotice message={deliveryUnconfirmedCopy(true)} testId="crew-goals-track-unconfirmed" />}
      <section className="rounded-2xl border border-border bg-bg px-3.5 py-3.5 flex flex-col gap-2">
        <textarea
          value={trackText}
          disabled={busy}
          onChange={e => setTrackText(e.target.value)}
          rows={4}
          aria-label={t('pages.membersPage.goals_track_in_chat', { name })}
          className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus-ring"
          data-testid="crew-goals-track-input"
        />
        <div className="flex gap-2 justify-end">
          <Btn type="button" onClick={() => requestLeave(closePage)}>{t('pages.membersPage.create_cancel')}</Btn>
          <Btn
            type="button"
            primary
            disabled={!trackText.trim() || tracking === 'sending'}
            onClick={() => { void trackInChat() }}
            data-testid="crew-goals-track-send"
          >
            {t('pages.membersPage.goals_track_send')}
          </Btn>
        </div>
      </section>
    </div>
  )

  // The record this page edits was removed elsewhere while a draft was open.
  const goneDraft = [editing?.text, krEdit?.text, krEdit?.target].filter(Boolean).join('\n')
  const gonePage = (
    <div className="flex flex-col gap-3" data-testid="crew-goals-page-gone">
      <ErrorNotice message={t('pages.membersPage.goals_page_gone')} testId="crew-goals-gone-notice" />
      <textarea
        readOnly
        value={goneDraft}
        rows={5}
        aria-label={t('pages.membersPage.goals_page_gone_draft')}
        className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus-ring"
        data-testid="crew-goals-gone-draft"
      />
      <div className="flex justify-end">
        <Btn type="button" onClick={closePage} className="h-8" data-testid="crew-goals-gone-discard">
          {t('pages.membersPage.goals_page_gone_discard')}
        </Btn>
      </div>
    </div>
  )

  const pageBody = (pg: GoalsPage): ReactNode => {
    if (pg.kind === 'goal') {
      const goal = pg.id ? goals.goals.find(g => g.id === pg.id) : undefined
      return pg.id && !goal ? (dirty ? gonePage : null) : goalPage(goal)
    }
    if (pg.kind === 'checkins') return checkinsPage
    if (pg.kind === 'track') return trackPage
    const kr = goals.key_results.find(k => k.id === pg.id)
    return kr ? krPage(kr) : (dirty ? gonePage : null)
  }

  const firstPage = (
    <div className="flex flex-col gap-3" data-testid="crew-goals-pane">
      {/* A failed save keeps its draft on the page it was typed on, so the user
          retries there; the first page has nothing to retry. */}
      {!page && saveError}

      {!hasGoal && !removedGoal ? (
        <section className="rounded-2xl border border-dashed border-border px-4 py-5 flex flex-col items-center gap-2.5 text-center" data-testid="crew-goals-long-term">
          <RowTile tone="accent"><Goal size={17} /></RowTile>
          <p className="text-[13px] text-muted max-w-[30ch]">{t('pages.membersPage.goals_none', { name })}</p>
          <Btn type="button" primary onClick={() => openGoal()} className="gap-1 h-8" data-testid="crew-goals-edit">
            <Pencil size={13} aria-hidden="true" />{t('pages.membersPage.goals_write_goal')}
          </Btn>
        </section>
      ) : (
        <Section
          flush
          title={t('pages.membersPage.goals_long_term')}
          icon={Goal}
          tone="accent"
          testId="crew-goals-long-term"
          trailing={(
            <button
              type="button"
              onClick={() => openGoal()}
              aria-label={t('pages.membersPage.goals_add')}
              title={t('pages.membersPage.goals_add')}
              className="w-7 h-7 rounded-lg grid place-items-center text-muted hover:text-text hover:bg-bg-hover transition-colors cursor-pointer focus-ring"
              data-testid="crew-goals-add"
            >
              <Plus size={15} aria-hidden="true" />
            </button>
          )}
        >
          {removedGoal && (
            <div className="flex items-center gap-2 mx-3.5 mb-2 rounded-lg bg-bg-hover px-2.5 py-1.5 text-[12px] text-muted" data-testid="crew-goals-goal-undo-row">
              <span className="flex-1 min-w-0 truncate" title={removedGoal.text}>{t('pages.membersPage.goals_removed')}</span>
              <Btn type="button" onClick={() => run(() => api.memberGoals.restoreGoal(slug, member, removedGoal.id))} className="gap-1 h-7 px-2" data-testid="crew-goals-goal-undo">
                <Undo2 size={12} aria-hidden="true" />{t('pages.membersPage.goals_undo')}
              </Btn>
            </div>
          )}
          {hasGoal ? (
            <ul data-testid="crew-goals-goal-list">
              {goals.goals.map(g => (
                <li key={g.id} className="border-t border-border">
                  <button
                    type="button"
                    onClick={() => openGoal(g)}
                    aria-label={`${t('pages.membersPage.goals_goal_number', { number: g.number })}: ${g.text}`}
                    className="flex w-full items-start gap-2.5 px-3.5 py-2.5 text-left hover:bg-bg-hover transition-colors cursor-pointer focus-ring"
                    data-testid="crew-goals-goal"
                  >
                    <span className="h-[22.75px] flex items-center shrink-0"><GoalNumber goal={g} /></span>
                    <span className="flex-1 min-w-0 text-[14px] leading-relaxed text-text-strong font-medium whitespace-pre-wrap" data-testid="crew-goals-long-term-text">{g.text}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-3.5 pb-3 text-[13px] text-muted">{t('pages.membersPage.goals_none', { name })}</p>
          )}
        </Section>
      )}

      <Section
        flush
        title={t('pages.membersPage.goals_key_results')}
        icon={Flag}
        tone="ok"
        testId="crew-goals-key-results"
        trailing={goals.key_results.length > 0 ? <span className="text-[12px] text-muted tabular-nums">{goals.key_results.length}</span> : null}
      >
        {removedKr && (
          <div className="flex items-center gap-2 mx-3.5 mb-2 rounded-lg bg-bg-hover px-2.5 py-1.5 text-[12px] text-muted" data-testid="crew-goals-kr-undo-row">
            <span className="flex-1 min-w-0 truncate" title={removedKr.text}>{t('pages.membersPage.goals_kr_removed')}</span>
            <Btn type="button" onClick={() => run(() => api.memberGoals.restoreKeyResult(slug, member, removedKr.id))} className="gap-1 h-7 px-2" data-testid="crew-goals-kr-undo">
              <Undo2 size={12} aria-hidden="true" />{t('pages.membersPage.goals_undo')}
            </Btn>
          </div>
        )}
        {goals.key_results.length === 0 ? (
          <p className="px-3.5 pb-3 text-[13px] text-muted">{t('pages.membersPage.goals_kr_empty', { name })}</p>
        ) : (
          <ul>
            {goals.key_results.map(kr => <KeyResultRow key={kr.id} kr={kr} goals={goals.goals} onOpen={() => openPage({ kind: 'kr', id: kr.id })} />)}
          </ul>
        )}
      </Section>

      <Section
        title={t('pages.membersPage.goals_checkins')}
        icon={AlarmClock}
        tone="info"
        testId="crew-goals-checkins"
        onOpen={() => openPage({ kind: 'checkins' })}
        openTestId="crew-goals-checkins-open"
      >
        {!hasGoal && <span className="block mb-2 text-[12px] text-muted">{t('pages.membersPage.goals_checkins_need_goal')}</span>}
        <span className="grid grid-cols-3 gap-2">
          <CheckInTile icon={Sun} tone="warn" label={t('pages.membersPage.goals_daily')} on={sched.daily} status={sched.daily ? sched.time : off} testId="crew-goals-checkin-daily" />
          <CheckInTile icon={CalendarDays} tone="info" label={t('pages.membersPage.goals_weekly')} on={sched.weekly} status={sched.weekly ? `${fmtWeekday(sched.weekday === 0 ? 7 : sched.weekday, 'short')} · ${sched.time}` : off} testId="crew-goals-checkin-weekly" />
          <CheckInTile icon={CalendarRange} tone="accent" label={t('pages.membersPage.goals_monthly')} on={sched.monthly} status={sched.monthly ? `${t('pages.membersPage.goals_month_day_option', { day: sched.month_day })} · ${sched.time}` : off} testId="crew-goals-checkin-monthly" />
        </span>
      </Section>

      {onTrackInChat && (
        <div className="flex flex-col gap-1.5">
          <button
            type="button"
            onClick={() => { setTrackText(''); setTracking('idle'); openPage({ kind: 'track' }) }}
            className="flex w-full items-center gap-3 rounded-2xl border border-border bg-bg px-3.5 py-3 text-left hover:bg-bg-hover transition-colors cursor-pointer focus-ring disabled:opacity-60 disabled:cursor-default"
            data-testid="crew-goals-track"
          >
            <RowTile tone="info" size="sm"><MessageSquare size={15} /></RowTile>
            <span className="flex-1 min-w-0 text-[13px] font-semibold text-text">{t('pages.membersPage.goals_track_in_chat', { name })}</span>
            <ChevronRight size={15} className="text-muted shrink-0" aria-hidden="true" />
          </button>
        </div>
      )}

      <Section flush title={t('pages.membersPage.goals_reports')} icon={FileText} tone="warn" testId="crew-goals-reports">
        <Reports goals={goals} limit={6} />
      </Section>
    </div>
  )

  // Inside the profile card the page slides over the card. Anywhere else (no
  // card to push onto) it replaces the first page, with its own back control.
  if (push) {
    return (
      <>
        {firstPage}
        {push.host && shown ? createPortal(pageBody(shown), push.host) : null}
      </>
    )
  }
  if (page) {
    return (
      <div className="flex flex-col gap-3" data-testid="crew-goals-pane">
        <button type="button" onClick={() => requestLeave(closePage)} className="self-start inline-flex items-center gap-0.5 pl-1 pr-2 py-1 rounded-lg text-accent font-medium text-[13px] hover:bg-accent-subtle cursor-pointer" data-testid="crew-goals-back">
          <ChevronLeft size={18} aria-hidden="true" />
          {pageTitle}
        </button>
        {pageBody(page)}
      </div>
    )
  }
  return firstPage
}
