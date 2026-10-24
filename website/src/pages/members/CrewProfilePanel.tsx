import { createContext, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type HTMLAttributes, type ReactNode, type Ref } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { AlarmClock, Brain, ChevronLeft, ChevronRight, Download, FolderOpen, Goal, IdCard, NotebookPen, Pencil, RotateCcw, Route, X } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewMonitoredAvatar from '../../components/crew/CrewMonitoredAvatar'
import Tablist, { type TablistTab } from '../../components/Tablist'
import type { AutomationRecord } from '../../monitoring/automation'
import type { CronJob } from '../../types'
import CrewMonitorDetails, { type MonitorReadState } from './CrewMonitorDetails'
import CrewScheduleList from './CrewScheduleList'
// Loaded on first open, so the export view stays out of the profile card's chunk.
const CrewExportView = lazy(() => import('./CrewExportView'))
import { cn } from '../../lib/utils'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { Row, TONE } from './profileRow'
import { uiLocation } from '../../uiLocations/uiLocation'
import { useGuideRevealScope } from '../../guide/GuideRevealScope'

/** The card's four tabs, in strip order (crewmate-panel IA sketch). */
export const PROFILE_TABS = ['sessions', 'schedule', 'goals', 'profile'] as const
export type ProfileTab = (typeof PROFILE_TABS)[number]
/** The head face's side. Exported so the page's face flight can render its
 *  copy at this size (the larger of the two faces, so the pill-sized end is a
 *  scale-down and never a blurry scale-up). */
export const PROFILE_FACE_PX = 84

/** A page the card pushes OVER its tabs (iOS push: one back control pointing at
 *  the card), for the things a tab opens in place: the full description, the
 *  crewmate's notes, the Export confirm view, and the schedule create form. */
type Pushed = 'about' | 'notes' | 'export' | 'new-schedule' | 'tab-page'

/** A tab's own pages pushed over the card (the Goals tab's milestone, goal and
 *  check-in pages; the Profile tab's Settings page and the picker pages over
 *  it). The tab keeps each page's state and renders it into that depth's host
 *  through a portal; the card owns the slide, the back control and Escape, so a
 *  tab page moves exactly like About and Notes do. Pages stack (iOS push): each
 *  level's back control names the page under it, the first names the crewmate. */
export interface ProfilePagePush {
  /** Push a page at `depth` (default 0, the page over the card), or retitle the
   *  page already at that depth (the pages over it stay). `onPop` runs when the
   *  user goes back from it, before the card slides it away. Stable across
   *  host changes: key an effect on it, never on the whole context value. */
  open: (title: string, onPop: () => void, depth?: number) => void
  /** Pop the pages from `depth` up (default: the top one), e.g. after a save
   *  landed. Their `onPop`s do not run: the tab is already past them. */
  close: (depth?: number) => void
  /** Where the tab portals the depth-0 page body; null until it has mounted. */
  host: HTMLElement | null
  /** One host per pushed depth, in order; an entry is null until it has mounted. */
  hosts: Array<HTMLElement | null>
}
export const ProfilePagePushContext = createContext<ProfilePagePush | null>(null)

/** One level of the tab-page stack: its title, what runs when the user goes
 *  back from it, and the control that opened it (focus goes back there). */
interface TabPage { title: string; onPop: () => void; opener: HTMLElement | null }

export interface CrewProfilePanelProps {
  member: MemberRosterRow
  running: boolean
  slotKey?: string | null
  /** The one normalized monitor record for this crewmate's own slot. */
  automation: AutomationRecord | null
  /** Whether both monitor registries were read, are pending, or failed. */
  monitorReadState: MonitorReadState
  /** The crewmate's description from its crew config; '' while unread or unset. */
  description: string
  /** The crew registry read failed; retained cached description data may still be shown. */
  descriptionError?: boolean
  /** The crewmate's first goal; About shows it when there is no description. */
  goal?: string
  /** The goal record read failed, so About cannot tell whether there is a goal. */
  goalError?: boolean
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
  /** The Heartbeat row at the top of the Schedules tab (CrewHeartbeatRow):
   *  the one wake that is neither a schedule nor a goal check-in. */
  heartbeatBody?: ReactNode
  /** Whether that Heartbeat is on: the schedule list's empty line then says
   *  "No schedules yet" instead of "Nothing wakes this mate on its own yet",
   *  which the row above it would contradict. */
  heartbeatOn?: boolean
  /** A Heartbeat write is in flight: the tab rail refuses to leave Schedules
   *  until it settles (the host's leave guard refuses close and navigation). */
  heartbeatBusy?: boolean
  sessionsBody: ReactNode
  notesBody: ReactNode
  /** The Goals tab: the goals, the milestones, the check-in switches
   *  and the reports (CrewGoalsPane). */
  goalsBody?: ReactNode
  /** The crewmate's own settings group (CrewProfileSettings): permission, model,
   *  effort, the worker pair and the dashboard lock. They are set here, never in
   *  the chat composer. Its pickers are pages it pushes over the card through
   *  `ProfilePagePushContext`, like the Goals tab's pages. */
  settingsBody?: ReactNode
  onClose: () => void
  /** Runs a pushed-page back action through the host's draft guard. */
  onRequestBack: (proceed: () => void) => void
  /** The Export view's lessons text is typed: the host's exit guards then ask
   *  before any exit (back, close, backdrop, pill, switch, route) drops it. */
  onExportDraftChange?: (dirty: boolean) => void
  onEdit: () => void
  onOpenFiles: () => void
  /** Fresh start: stop any turn and start a clean conversation on the same
   *  thread. Absent while the thread is unbound or a reset runs. */
  onFreshStart?: () => void
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
 * what wakes it, the Heartbeat row first; Sessions is the sessions it is driving; Goals is the goal
 * record the user and the crewmate share (CrewGoalsPane).
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
  // The guide's reveal scope for the Profile tab: a `ui.show` step that
  // selects it completes the moment the tab is current.
  useGuideRevealScope('members.profile-tab', tab === 'profile')
  const [pushed, setPushed] = useState<Pushed | null>(null)
  // The tab-page stack and one host per level. `tabPagesRef` mirrors the stack
  // for the push callbacks, which decide "new level or retitle" synchronously.
  const [tabPages, setTabPages] = useState<TabPage[]>([])
  const tabPagesRef = useRef<TabPage[]>([])
  const [tabPageHosts, setTabPageHosts] = useState<Array<HTMLDivElement | null>>([])
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

  const captureOpener = () => {
    const active = document.activeElement
    return active instanceof HTMLElement && rootRef.current?.contains(active) ? active : null
  }
  const setTabStack = useCallback((next: TabPage[]) => {
    tabPagesRef.current = next
    setTabPages(next)
    if (next.length === 0) setPushed(current => (current === 'tab-page' ? null : current))
  }, [])

  // Going back from any pushed page: one level. A tab page first hands its own
  // state back (its `onPop`), so the tab is already on the page under it when
  // the slide ends; the opener that pushed it takes focus back.
  const popPage = useCallback(() => {
    const stack = tabPagesRef.current
    if (stack.length) {
      const top = stack[stack.length - 1]
      openerRef.current = top.opener
      top.onPop()
      setTabStack(stack.slice(0, -1))
      return
    }
    setPushed(null)
  }, [setTabStack])

  // Escape belongs to this card only. A key event in the roster switcher,
  // the chat composer, or another overlay never reaches it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || !rootRef.current?.contains(e.target as Node)) return
      const target = e.target as HTMLElement
      if (!pushed && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
      e.stopPropagation()
      if (pushed) p.onRequestBack(popPage)
      else p.onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pushed, p, popPage])

  const push = (page: Pushed) => {
    openerRef.current = captureOpener()
    setPushed(page)
  }

  // A pushed page makes what it covers `inert`, which drops the focus that was
  // on the opener onto the body — outside the card, where Escape above cannot
  // see it. Land it on the back control instead (the New schedule page focuses
  // its own form), and hand it back to the opener when the page pops.
  // An opener that unmounted while its page was open (a settings row that
  // folded on the pick made from its own page) cannot take focus back; the
  // back control of the page now on top does, or the card itself when none is
  // left, so Escape still works. Only after a pop: the card opening must never
  // pull focus off the composer.
  const hadPage = useRef(false)
  const prevDepth = useRef(0)
  const depth = tabPages.length
  useEffect(() => {
    const deeper = depth > prevDepth.current
    prevDepth.current = depth
    if (pushed === 'about' || pushed === 'notes' || pushed === 'export' || (pushed === 'tab-page' && deeper)) {
      backRef.current?.focus({ preventScroll: true })
    } else if (!pushed || pushed === 'tab-page') {
      const opener = openerRef.current
      openerRef.current = null
      if (opener?.isConnected) opener.focus({ preventScroll: true })
      else if (pushed) backRef.current?.focus({ preventScroll: true })
      else if (hadPage.current) rootRef.current?.focus({ preventScroll: true })
    }
    if (pushed) hadPage.current = true
  }, [pushed, depth])

  // Strip order = PROFILE_TABS: the work first (what the crewmate is doing,
  // when it wakes, what it is after), the identity card last.
  const tabs: Array<TablistTab<ProfileTab>> = [
    { key: 'sessions', label: t('pages.membersPage.profile_sessions_tab'), icon: <Route size={15} aria-hidden="true" /> },
    { key: 'schedule', label: t('pages.membersPage.schedules_tab'), icon: <AlarmClock size={15} aria-hidden="true" /> },
    { key: 'goals', label: t('pages.membersPage.profile_goals_tab'), icon: <Goal size={15} aria-hidden="true" /> },
    { key: 'profile', label: t('pages.membersPage.profile_tab'), icon: <IdCard size={15} aria-hidden="true" /> },
  ]
  const openTabPage = useCallback((title: string, onPop: () => void, at = 0) => {
    const stack = tabPagesRef.current
    // A depth past the top lands on the top: a stack has no holes.
    const level = Math.min(Math.max(0, at), stack.length)
    // Re-opening the level already there retitles it in place and KEEPS the
    // pages over it (a tab's effect may re-run while a deeper page is open;
    // only `close` drops pages). A new level records the control the focus is
    // on now, so a pop can hand focus back to it.
    const next = stack.slice()
    next[level] = { title, onPop, opener: level < stack.length ? stack[level].opener : captureOpener() }
    tabPagesRef.current = next
    setTabPages(next)
    setPushed('tab-page')
  }, [])
  const closeTabPage = useCallback((from?: number) => {
    const stack = tabPagesRef.current
    const level = from === undefined ? stack.length - 1 : from
    if (level < 0 || level >= stack.length) return
    openerRef.current = stack[level].opener
    setTabStack(stack.slice(0, level))
  }, [setTabStack])
  const pagePush = useMemo<ProfilePagePush>(
    // A host outlives its level for the exit slide (the node unmounts when the
    // slide ends), so the tab can keep painting its page while it leaves.
    // `open` and `close` are stable across host changes, so a tab's effect
    // keyed on them does not re-run when a host mounts.
    () => ({ open: openTabPage, close: closeTabPage, host: tabPageHosts[0] ?? null, hosts: tabPageHosts }),
    [openTabPage, closeTabPage, tabPageHosts],
  )
  // One STABLE ref callback per level: a fresh closure each render would be
  // re-attached (null, then the node) on every commit and loop the state.
  const hostRefs = useRef<Array<(el: HTMLDivElement | null) => void>>([])
  const tabPageHostRef = (level: number) => {
    hostRefs.current[level] ??= (el) => setTabPageHosts(hosts => {
      if (hosts[level] === el) return hosts
      const next = hosts.slice()
      next[level] = el
      // Drop trailing unmounted levels so `hosts.length` tracks the stack.
      while (next.length && next[next.length - 1] === null) next.pop()
      return next
    })
    return hostRefs.current[level]
  }
  // The back control names what is under the top page: the page below it, or
  // the crewmate (the card) under the first.
  const backLabel = depth >= 2 ? tabPages[depth - 2].title : name

  const pushedTitle = pushed === 'tab-page'
    ? (tabPages[depth - 1]?.title ?? '')
    : pushed === 'about'
    ? t('pages.membersPage.profile_about')
    : pushed === 'notes'
      ? t('pages.membersPage.notes_tab')
      : pushed === 'export'
        ? t('pages.membersPage.export_title')
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
      // Programmatic focus target only (the pop fallback above); not in the tab order.
      tabIndex={-1}
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
            onClick={() => p.onRequestBack(popPage)}
            className="inline-flex items-center gap-0.5 pl-1 pr-2 py-1 rounded-lg text-accent font-medium text-[13px] hover:bg-accent-subtle cursor-pointer"
            data-testid="crew-profile-back"
          >
            <ChevronLeft size={18} aria-hidden="true" />
            {backLabel}
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
              <CrewMonitoredAvatar
                seed={p.member.name}
                avatar={p.member.avatar}
                slotKey={p.slotKey}
                running={p.running}
                automation={p.automation}
                indicatorTestId="crew-profile-loop-indicator"
                size={PROFILE_FACE_PX}
                working="full"
              />
            </span>
            {/* Face and name only: the id, the role and the activity line were
                dropped from the head (they live in the pill and the editor). */}
            <div className="mt-1 text-[19px] font-bold tracking-tight text-text-strong leading-tight" data-testid="crew-profile-name">{name}</div>
            {/* Fresh start: a small pill button under the name, where the
                person is. A real button shape (border, filled background on
                hover) rather than a bare caption-weight link, so a first-time
                reader can tell it is a control before deciding whether to
                risk it; muted colour and no danger tone keep it quiet. The
                label carries the action alone: what survives the reset is the
                confirm dialog's job, where the person is deciding. Held while
                its own flow runs.

                The shared `Btn` primitive carries the press feedback every
                other button on a page has (`active:scale`), and `twMerge`
                lets the pill keep its own radius, height, size and muted
                colour over Btn's defaults. `hover:border-border` is spelled
                out to hold Btn's brighter hover border back: this control
                stays quiet, and only its fill and label move on hover. */}
            {p.onFreshStart && (
              <Btn
                type="button"
                onClick={p.onFreshStart}
                className="gap-1.5 h-7 px-3 mt-1 rounded-full bg-bg text-[12px] text-muted hover:border-border hover:bg-bg-hover hover:text-text focus-ring"
                data-testid="crew-profile-fresh-start"
              >
                <RotateCcw size={12} aria-hidden="true" />
                <span>{t('pages.membersPage.fresh_start')}</span>
              </Btn>
            )}
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
              onChange={next => {
                if (tab === 'goals' && next !== tab) p.onRequestBack(() => { popPage(); setTab(next) })
                // A Heartbeat write still in flight would be unmounted by the
                // switch and its failure lost: refuse the exit until it settles,
                // as the leave guard refuses a busy Goals write.
                else if (tab === 'schedule' && p.heartbeatBusy) return
                else setTab(next)
              }}
              ariaLabel={t('pages.membersPage.profile_card')}
              layoutId="crew-profile-tab-indicator"
              labels="active"
              // The Profile tab is a find_ui location: a guide to the Settings
              // row (which lives on it) selects this tab first.
              segments={[{ key: 'profile', label: t('pages.membersPage.profile_tab'), ...uiLocation('members.profile-tab') }]}
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
                  ) : !p.descriptionError && p.goalError ? (
                    <ErrorNotice
                      message={t('pages.membersPage.goals_load_failed')}
                      askAgent
                      testId="crew-profile-goal-error"
                    />
                  ) : !p.descriptionError && p.goal ? (
                    <>
                      <p className="text-[13px] leading-relaxed text-text line-clamp-2 whitespace-pre-wrap" data-testid="crew-profile-goal-fallback">{p.goal}</p>
                      <p className="mt-1 text-[12px] text-muted">{t('pages.membersPage.profile_about_from_goal')}</p>
                    </>
                  ) : !p.descriptionError ? (
                    <p className="text-[13px] text-muted">{t('pages.membersPage.profile_no_description')}</p>
                  ) : null}
                </section>

                <CrewMonitorDetails automation={p.automation} readState={p.monitorReadState} nowTs={p.nowTs} />

                <div className="grid grid-cols-2 gap-2.5">
                  <Tile icon={<Brain size={18} />} tone="accent" label={t('pages.membersPage.profile_memory')} value={p.memoryLabel} onClick={p.onOpenMemory} testId="crew-profile-memory" />
                  <Tile icon={<FolderOpen size={18} />} tone="info" label={t('pages.membersPage.profile_workspace')} value={ws || t('pages.membersPage.profile_workspace_none')} title={p.member.workspace} onClick={p.onOpenFiles} testId="crew-profile-workspace" />
                </div>

                {/* The doors: Notes, and the Settings row the settings body
                    contributes (its pages push over the card through the context). */}
                <div className="rounded-2xl border border-border bg-bg overflow-hidden">
                  <Row icon={<NotebookPen size={16} />} tone="warn" label={t('pages.membersPage.notes_tab')} sub={t('pages.membersPage.profile_notes_sub')} onClick={() => push('notes')} testId="crew-profile-notes" />
                  <Row icon={<Download size={16} />} tone="info" label={t('pages.membersPage.export_row')} sub={t('pages.membersPage.export_row_sub')} onClick={() => push('export')} testId="crew-profile-export" />
                  <ProfilePagePushContext.Provider value={pagePush}>{p.settingsBody}</ProfilePagePushContext.Provider>
                </div>
              </div>
            )}
            {tab === 'schedule' && (
              <div className="flex flex-col gap-3">
                {p.heartbeatBody}
                <CrewScheduleList jobs={p.schedules} loading={p.schedulesLoading} error={p.schedulesError} nowTs={p.nowTs} onOpenAll={p.onOpenSchedule} onOpenJob={p.onOpenScheduleJob} onCreate={() => push('new-schedule')} selfWaking={p.heartbeatOn} />
              </div>
            )}
            {tab === 'sessions' && p.sessionsBody}
            {tab === 'goals' && <ProfilePagePushContext.Provider value={pagePush}>{p.goalsBody}</ProfilePagePushContext.Provider>}
          </div>
        </motion.div>

        <AnimatePresence initial={false}>
          {pushed && pushed !== 'tab-page' && (
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
              ) : pushed === 'export' ? (
                <Suspense fallback={<div className="px-4 py-4 text-[13px] text-muted" data-testid="crew-export-loading">{t('pages.membersPage.export_loading')}</div>}>
                  <CrewExportView slug={p.member.slug} member={p.member.name} onDirtyChange={p.onExportDraftChange} />
                </Suspense>
              ) : (
                p.newScheduleBody
              )}
            </motion.div>
          )}
          {/* The tab-page stack: one layer per level. A layer under a deeper one
              slides a quarter-width left and goes inert, like the card under
              the first; its host stays mounted so the tab keeps painting it. */}
          {pushed === 'tab-page' && tabPages.map((page, level) => {
            const covered = level < depth - 1
            return (
              <motion.div
                key={`tab-page-${level}`}
                initial={slide.initial}
                animate={covered && !reduce ? { x: '-25%', opacity: 0.4 } : slide.animate}
                exit={slide.exit}
                transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
                className="absolute inset-0 overflow-y-auto bg-bg-elevated"
                data-testid={level === 0 ? 'crew-profile-page-tab-page' : `crew-profile-page-tab-page-${level}`}
                data-page-title={page.title}
                {...((covered ? { inert: '', 'aria-hidden': true } : { 'aria-hidden': false }) as typeof coveredProps)}
              >
                <div ref={tabPageHostRef(level)} className="px-4 py-4" />
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>
    </div>
  )
}
