import { useEffect, useRef, useState, type HTMLAttributes, type ReactNode, type Ref } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { AlarmClock, Brain, ChevronLeft, ChevronRight, FolderOpen, Goal, IdCard, NotebookPen, Pencil, Route, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewStateAvatar from '../../components/CrewStateAvatar'
import Tablist, { type TablistTab } from '../../components/Tablist'
import type { CronJob } from '../../types'
import CrewScheduleList from './CrewScheduleList'
import { cn } from '../../lib/utils'
import ErrorNotice from '../../components/ErrorNotice'

/** The card's four tabs, in strip order (crewmate-panel IA sketch). */
export const PROFILE_TABS = ['sessions', 'schedule', 'goals', 'profile'] as const
export type ProfileTab = (typeof PROFILE_TABS)[number]
/** The head face's side. Exported so the page's face flight can render its
 *  copy at this size (the larger of the two faces, so the pill-sized end is a
 *  scale-down and never a blurry scale-up). */
export const PROFILE_FACE_PX = 84

/** A page the card pushes OVER its tabs (iOS push: one back control pointing at
 *  the card), for the things a tab opens in place: the full description, the
 *  crewmate's notes, and the schedule create form. */
type Pushed = 'about' | 'notes' | 'new-schedule'

export interface CrewProfilePanelProps {
  member: MemberRosterRow
  running: boolean
  slotKey?: string | null
  /** The crewmate's description from its crew config; '' while unread or unset. */
  description: string
  /** The crew registry read failed; retained cached description data may still be shown. */
  descriptionError?: boolean
  /** Where its memory lives, already worded by the page (private / global / …). */
  memoryLabel: string
  /** Opens the management page of THIS crewmate's private memory store.
   *  Absent when the binding is not its own store: the tile stays a readout. */
  onOpenMemory?: () => void
  /** Which tab opens first. */
  initialTab?: ProfileTab
  /** The head face's element, for the page's face flight (#18236): the page
   *  measures where this face sits to fly the pill's face into it (and out of
   *  it again) ABOVE the card, since this face lives inside the card's clipped,
   *  scrolling body and could never travel there itself. */
  faceRef?: Ref<HTMLSpanElement>
  /** Hold the head face's place but paint nothing: the page's flight copy is
   *  the one face on screen until it lands here. */
  faceHidden?: boolean
  /** The schedules that wake this crewmate, as the page already reads them. */
  schedules: CronJob[]
  schedulesLoading: boolean
  schedulesError?: boolean
  nowTs: number
  onOpenSchedule: () => void
  onOpenScheduleJob: (id: string) => void
  /** The schedule CREATE form (the crew editor's own pane), pushed as a page.
   *  The card is a summary with doors, never a second source of truth. */
  newScheduleBody: ReactNode
  sessionsBody: ReactNode
  notesBody: ReactNode
  /** The crewmate's permission, model and effort controls (CrewProfileSettings).
   *  They are set here, never in the chat composer. */
  settingsBody?: ReactNode
  onClose: () => void
  /** Runs a pushed-page back action through the host's draft guard. */
  onRequestBack: (proceed: () => void) => void
  onEdit: () => void
  onOpenFiles: () => void
}

const TONE: Record<'accent' | 'ok' | 'warn' | 'info', string> = {
  accent: 'bg-accent-subtle text-accent',
  ok: 'bg-ok-subtle text-ok',
  warn: 'bg-warn-subtle text-warn',
  info: 'bg-info-subtle text-info',
}

function Row({ icon, tone = 'accent', label, sub, onClick, testId }: {
  icon: ReactNode
  tone?: keyof typeof TONE
  label: string
  sub?: string
  onClick: () => void
  testId: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-3 w-full px-3.5 py-3 text-left border-t border-border first:border-t-0 hover:bg-bg-hover transition-colors cursor-pointer"
      data-testid={testId}
    >
      <span className={cn('w-8 h-8 rounded-[9px] grid place-items-center shrink-0', TONE[tone])} aria-hidden="true">{icon}</span>
      <span className="flex-1 min-w-0 leading-tight">
        <span className="block text-[13px] font-semibold truncate">{label}</span>
        {sub && <span className="block text-[12px] text-muted truncate">{sub}</span>}
      </span>
      <ChevronRight size={15} className="text-muted shrink-0" aria-hidden="true" />
    </button>
  )
}

function Tile({ icon, tone, label, value, title, onClick, testId }: {
  icon: ReactNode
  tone: keyof typeof TONE
  label: string
  value: string
  title?: string
  onClick?: () => void
  testId: string
}) {
  const body = <>
    <span className={cn('w-9 h-9 rounded-[10px] grid place-items-center', TONE[tone])} aria-hidden="true">{icon}</span>
    <span className="text-[13px] font-semibold mt-1">{label}</span>
    <span className="text-[12px] text-muted truncate w-full" title={title}>{value}</span>
  </>
  const cls = 'flex flex-col items-start gap-1.5 rounded-2xl border border-border p-3.5 text-left min-w-0'
  // A static tile is a readout, not a door: a solid hairline, no fill, no hover,
  // no pointer. Not dashed — on this card a dashed border is the empty /
  // coming-soon language (Sessions empty, Goals), and a Memory binding that IS
  // set must not read as "not set up".
  return onClick ? (
    <button type="button" onClick={onClick} className={cn(cls, 'bg-bg hover:bg-bg-hover transition-colors cursor-pointer')} data-testid={testId}>{body}</button>
  ) : (
    <div className={cn(cls, 'bg-transparent')} data-testid={testId}>{body}</div>
  )
}

/**
 * The crewmate's profile card — the surface the DM header's identity pill opens
 * (crewmate-panel IA). One crewmate: face, name, role and what it is doing now
 * on top; four tabs under that, the shared `Tablist` rail with icons only and
 * the selected tab's word. Profile is a summary with doors (edit, description,
 * memory, workspace, notes) plus the crewmate's own settings (permission,
 * model, effort); Schedules is a readable list of
 * what wakes it; Sessions is the sessions it is driving; Goals holds its place
 * as coming soon.
 *
 * It is a floating card over the right of the thread — a hover card, not a
 * second side panel: rounded on every corner, a third of the row wide, the
 * side panel's height. The side panel itself carries only the dynamic Dashboard
 * and the Workspace file browser, which is where the Workspace tile leads; the
 * Memory tile opens the crewmate's private memory page in Settings.
 */
export default function CrewProfilePanel(p: CrewProfilePanelProps) {
  const { t } = useTranslation()
  const reduce = useReducedMotion()
  const [tab, setTab] = useState<ProfileTab>(p.initialTab ?? PROFILE_TABS[0])
  const [pushed, setPushed] = useState<Pushed | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const backRef = useRef<HTMLButtonElement>(null)
  // The control that pushed the current page, so a pop can hand focus back to it.
  const openerRef = useRef<HTMLElement | null>(null)
  const name = crewDisplayName(p.member)
  // The workspace NAME the crew record carries (a key into the configured
  // workspaces), shown as-is. It is not a directory: the host resolves the
  // name to the DM slot's project when it roots Files / Terminal, so there is
  // no path here to take a folder name from.
  const ws = p.member.workspace ?? ''

  // Escape belongs to this card only. A key event in the roster switcher,
  // the chat composer, or another overlay never reaches it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || !rootRef.current?.contains(e.target as Node)) return
      const target = e.target as HTMLElement
      if (!pushed && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
      e.stopPropagation()
      if (pushed) p.onRequestBack(() => setPushed(null))
      else p.onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pushed, p])

  const push = (page: Pushed) => {
    const active = document.activeElement
    openerRef.current = active instanceof HTMLElement && rootRef.current?.contains(active) ? active : null
    setPushed(page)
  }

  // A pushed page makes the tabs `inert`, which drops the focus that was on the
  // opener onto the body — outside the card, where Escape above cannot see it.
  // Land it on the back control instead (the New schedule page focuses its own
  // form), and hand it back to the opener when the page pops.
  useEffect(() => {
    if (pushed === 'about' || pushed === 'notes') {
      backRef.current?.focus({ preventScroll: true })
    } else if (!pushed) {
      const opener = openerRef.current
      openerRef.current = null
      if (opener?.isConnected) opener.focus({ preventScroll: true })
    }
  }, [pushed])

  // Strip order = PROFILE_TABS: the work first (what the crewmate is doing,
  // when it wakes, what it is after), the identity card last.
  const tabs: Array<TablistTab<ProfileTab>> = [
    { key: 'sessions', label: t('pages.membersPage.profile_sessions_tab'), icon: <Route size={15} aria-hidden="true" /> },
    { key: 'schedule', label: t('pages.membersPage.schedules_tab'), icon: <AlarmClock size={15} aria-hidden="true" /> },
    { key: 'goals', label: t('pages.membersPage.profile_goals_tab'), icon: <Goal size={15} aria-hidden="true" /> },
    { key: 'profile', label: t('pages.membersPage.profile_tab'), icon: <IdCard size={15} aria-hidden="true" /> },
  ]
  const pushedTitle = pushed === 'about'
    ? t('pages.membersPage.profile_about')
    : pushed === 'notes'
      ? t('pages.membersPage.notes_tab')
      : t('pages.membersPage.schedule_new')

  const slide = reduce
    ? { initial: { opacity: 1 }, animate: { opacity: 1 }, exit: { opacity: 1 } }
    : { initial: { x: '100%' }, animate: { x: 0 }, exit: { x: '100%' } }

  // React 18 has no typed `inert` prop. Pass the string attribute through a
  // typed spread so the covered tabs leave both the focus and accessibility trees.
  const coveredProps = (pushed
    ? { inert: '', 'aria-hidden': true }
    : { 'aria-hidden': false }) as Pick<HTMLAttributes<HTMLDivElement>, 'aria-hidden'> & { inert?: string }

  return (
    <div
      className="flex flex-col h-full min-h-0 bg-bg-elevated text-text"
      data-testid="crew-profile-panel"
      data-tab={tab}
      ref={rootRef}
      role="region"
      aria-label={t('pages.membersPage.profile_card')}
    >
      {/* Bar: one back control while a page is pushed (pointing at the card), the
          title, and the close. Sticky by construction — only the body scrolls. */}
      <div className="h-11 shrink-0 flex items-center gap-1 px-2">
        {pushed ? (
          <button
            type="button"
            ref={backRef}
            onClick={() => p.onRequestBack(() => setPushed(null))}
            className="inline-flex items-center gap-0.5 pl-1 pr-2 py-1 rounded-lg text-accent font-medium text-[13px] hover:bg-accent-subtle cursor-pointer"
            data-testid="crew-profile-back"
          >
            <ChevronLeft size={18} aria-hidden="true" />
            {name}
          </button>
        ) : null}
        {pushed && <div className="flex-1 text-center text-[13px] font-semibold truncate pr-8" data-testid="crew-profile-pushed-title">{pushedTitle}</div>}
        {!pushed && <div className="flex-1" />}
        <button
          type="button"
          onClick={p.onClose}
          className="w-8 h-8 rounded-lg grid place-items-center text-muted hover:text-text hover:bg-bg-hover cursor-pointer shrink-0"
          aria-label={t('pages.membersPage.close')}
          title={t('pages.membersPage.close')}
          data-testid="crew-profile-close"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      <div className="relative flex-1 min-h-0 overflow-hidden">
        {/* The card proper: head, rail, tab body. Slides a quarter-width left
            under a pushed page and comes back when it pops (iOS push). */}
        <motion.div
          className="absolute inset-0 overflow-y-auto px-4 pb-4"
          initial={false}
          animate={pushed && !reduce ? { x: '-25%', opacity: 0.4 } : { x: 0, opacity: 1 }}
          transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
          {...coveredProps}
        >
          {/* No hairline under the title bar, so the head starts right below it. */}
          <div className="relative flex flex-col items-center gap-1.5 pt-1 pb-3 text-center">
            <span
              ref={p.faceRef}
              className="flex rounded-full"
              style={p.faceHidden ? { visibility: 'hidden' } : undefined}
              data-testid="crew-profile-face"
            >
              <CrewStateAvatar seed={p.member.name} avatar={p.member.avatar} slotKey={p.slotKey} running={p.running} size={PROFILE_FACE_PX} working="full" />
            </span>
            {/* Face and name only: the id, the role and the activity line were
                dropped from the head (they live in the pill and the editor). */}
            <div className="mt-1 text-[19px] font-bold tracking-tight text-text-strong leading-tight" data-testid="crew-profile-name">{name}</div>
            <button
              type="button"
              onClick={p.onEdit}
              className="absolute right-0 top-0 w-9 h-9 rounded-full grid place-items-center bg-bg border border-border text-muted hover:text-accent hover:border-accent transition-colors cursor-pointer focus-ring"
              aria-label={t('pages.membersPage.edit_member')}
              title={t('pages.membersPage.edit_member')}
              data-testid="crew-profile-edit"
            >
              <Pencil size={15} aria-hidden="true" />
            </button>
          </div>

          {/* The shared pill rail: icons, and the selected tab's word. */}
          <div className="flex justify-center mb-4" data-testid="crew-profile-tabs">
            <Tablist<ProfileTab>
              tabs={tabs}
              value={tab}
              onChange={setTab}
              ariaLabel={t('pages.membersPage.profile_card')}
              layoutId="crew-profile-tab-indicator"
              labels="active"
            />
          </div>

          <div role="tabpanel" data-testid={`crew-profile-pane-${tab}`}>
            {tab === 'profile' && (
              <div className="flex flex-col gap-3">
                <section className="rounded-2xl border border-border bg-bg px-3.5 py-3" data-testid="crew-profile-about">
                  <div className="text-[11px] font-semibold tracking-wide uppercase text-muted mb-1">{t('pages.membersPage.profile_about')}</div>
                  {p.descriptionError && (
                    <ErrorNotice
                      message={t('pages.membersPage.roster_load_failed')}
                      askAgent
                      testId="crew-profile-description-error"
                    />
                  )}
                  {p.description ? (
                    <>
                      <p className="text-[13px] leading-relaxed text-text line-clamp-2" data-testid="crew-profile-description">{p.description}</p>
                      <button
                        type="button"
                        onClick={() => push('about')}
                        className="mt-1.5 inline-flex items-center gap-1 text-[12.5px] font-semibold text-accent cursor-pointer"
                        data-testid="crew-profile-read-more"
                      >
                        {t('pages.membersPage.profile_read_more')}
                        <ChevronRight size={13} aria-hidden="true" />
                      </button>
                    </>
                  ) : !p.descriptionError ? (
                    <p className="text-[13px] text-muted">{t('pages.membersPage.profile_no_description')}</p>
                  ) : null}
                </section>

                <div className="grid grid-cols-2 gap-2.5">
                  <Tile icon={<Brain size={18} />} tone="accent" label={t('pages.membersPage.profile_memory')} value={p.memoryLabel} onClick={p.onOpenMemory} testId="crew-profile-memory" />
                  <Tile icon={<FolderOpen size={18} />} tone="info" label={t('pages.membersPage.profile_workspace')} value={ws || t('pages.membersPage.profile_workspace_none')} title={p.member.workspace} onClick={p.onOpenFiles} testId="crew-profile-workspace" />
                </div>

                <div className="rounded-2xl border border-border bg-bg overflow-hidden">
                  <Row icon={<NotebookPen size={16} />} tone="warn" label={t('pages.membersPage.notes_tab')} sub={t('pages.membersPage.profile_notes_sub')} onClick={() => push('notes')} testId="crew-profile-notes" />
                </div>

                {p.settingsBody}
              </div>
            )}
            {tab === 'schedule' && (
              <CrewScheduleList jobs={p.schedules} loading={p.schedulesLoading} error={p.schedulesError} nowTs={p.nowTs} onOpenAll={p.onOpenSchedule} onOpenJob={p.onOpenScheduleJob} onCreate={() => push('new-schedule')} />
            )}
            {tab === 'sessions' && p.sessionsBody}
            {tab === 'goals' && (
              // Coming soon: goals need a record the crewmate can read and act on,
              // and a browser-local list it never sees would promise the opposite.
              <div className="rounded-2xl border border-dashed border-border-strong bg-bg px-4 py-8 text-center text-[13px] text-muted flex flex-col items-center gap-2" data-testid="crew-profile-goals-soon">
                <Goal size={40} className="opacity-50" aria-hidden="true" />
                {t('pages.membersPage.profile_goals_soon', { name })}
              </div>
            )}
          </div>
        </motion.div>

        <AnimatePresence initial={false}>
          {pushed && (
            <motion.div
              key={pushed}
              initial={slide.initial}
              animate={slide.animate}
              exit={slide.exit}
              transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
              className="absolute inset-0 overflow-y-auto bg-bg-elevated"
              data-testid={`crew-profile-page-${pushed}`}
            >
              {pushed === 'about' ? (
                <div className="px-4 py-4">
                  <div className="rounded-2xl border border-border bg-bg px-4 py-4 text-[14px] leading-relaxed text-text whitespace-pre-wrap" data-testid="crew-profile-about-full">{p.description}</div>
                  <button type="button" onClick={p.onEdit} className="mt-3 inline-flex items-center gap-1.5 h-9 px-4 rounded-full bg-accent text-white text-[13px] font-semibold cursor-pointer">
                    <Pencil size={14} aria-hidden="true" />
                    {t('pages.membersPage.edit_member')}
                  </button>
                </div>
              ) : pushed === 'notes' ? (
                p.notesBody
              ) : (
                p.newScheduleBody
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  )
}
