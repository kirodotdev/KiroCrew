import { useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { ChevronDown, FileUp, PanelLeft, Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewStateAvatar from '../../components/CrewStateAvatar'
import { Glass } from '../../components/Glass'
import type { AutomationRecord } from '../../monitoring/automation'
import CrewmateRowBody from './CrewmateRowBody'
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover'
import { fmtList } from '../../i18n/format'
import { cn } from '../../lib/utils'
import { rosterPopulation, rosterShows, type MemberSignals } from './rosterFilter'
import { uiLocation } from '../../uiLocations/uiLocation'
import CrewmateImportDialog from './CrewmateImportDialog'

/** How many faces the closed chip stacks. Three is enough to read as "a crew"
 *  without the chip growing with the roster; the open list says how many there
 *  really are. */
const STACK_FACES = 3

/** How many unread crewmates get a pill of their own under the chip before the
 *  rest fold into one "+N" pill. Four pills is a glance; a tower over the
 *  conversation is not, and past four the open list reads faster anyway. */
const UNREAD_PILLS = 4

/** The signals a row carries when the page hands none in: presence from the
 *  roster row itself, nothing parked on the user, nothing unread. */
const rowOnlySignals = (m: MemberRosterRow): MemberSignals => ({
  running: !!m.running,
  needsYou: false,
  unread: false,
  patrolling: !!m.heartbeat_on,
  goal: !!m.has_goal,
})

/**
 * The crewmate switcher — the roster folded into one header chip.
 *
 * The Crewmates page used to keep the whole roster as a standing left column.
 * That column is gone from the DM view (product decision, crewmate-panel IA):
 * the thread and the crewmate's own panel get the width, and the roster is
 * reached on demand from this chip. Closed, it shows the page's name, the first
 * faces and a chevron (the roster's name slides in on hover or focus, and is
 * always the chip's accessible name); open, it is a searchable list of every
 * crewmate, the current one marked, and a "New crewmate" footer that opens the same create dialog the
 * old column's "+" did (one write path, one more front door). A second footer
 * Under the chip the page draws `CrewmateUnreadColumn` (below): one small pill
 * per crewmate with an UNREAD message who is not the open one (issue #19045).
 * The word "Crewmates" and the roster count the chip used to carry said what
 * the page is and how big the crew is — neither is what a reader comes to this
 * corner for, which is "who said something I have not read".
 *
 * action, "Show the full roster", brings the roster column back beside the
 * thread: this list holds crewmates only, and the column is where the team
 * headers, the star, the filters and the sort live — on desktop,
 * with the thread's back control hidden, nothing else reaches them. While the
 * column is showing the same action reads "Hide the roster".
 *
 * Switching here REPLACES the open crewmate the same way a roster row click did:
 * the parent owns the URL, so this component only reports the pick.
 *
 * The roster column also carried each crewmate's live state, and with the
 * column gone this chip is the only place on the desktop DM view that state can
 * live — a bare /members reopens a crewmate, so the folded roster is the norm,
 * not an edge. Each open-list row therefore shows the roster row's own cues
 * (the presence dot on the face while working, the unread dot on the right
 * edge) plus the one the roster never had a mark for: a turn parked on an
 * approval or a question. And the closed chip carries that last signal for
 * every crewmate OTHER than the open one, so another crewmate blocked on a
 * question is visible without opening the list. The open crewmate's own
 * needs-you state is excluded from the chip on purpose: its thread is on
 * screen, where the parked turn already shows itself.
 *
 * Folded or standing, it is ONE roster: this list runs the column's own hide
 * rule (`rosterPopulation` / `rosterShows` over the full roster the page hands
 * in), so the chip's count can never disagree with the column's count and a row
 * the column keeps hidden -- an app's row, a sync-generated template nobody has
 * chatted with -- is not listed here either. Typing in the search box reaches
 * those hidden rows exactly as the column's search does.
 */
export default function CrewmateSwitcher({
  members,
  activeName,
  onPick,
  onCreate,
  onShowRoster,
  signals = rowOnlySignals,
  automationOf,
  openRequest = 0,
  faceOnStageFor = '',
  className,
}: {
  /** The FULL roster, in the page's display order. The hide rule below decides
   *  which of these rows the chip lists, so hidden rows must be passed: they are
   *  what the search reaches. */
  members: MemberRosterRow[]
  /** The crewmate whose thread is open, by exact name. */
  activeName: string
  onPick: (name: string) => void
  /** Opens the New crewmate dialog. Omitted while creation is held. */
  onCreate?: () => void
  /** Pins the roster column back beside the thread. The page hides this chip
   *  while the column is open (it is the list the chip folds), so the action
   *  only ever shows; the column's own close lifts the pin. Omitted, the
   *  footer action is not drawn. */
  onShowRoster?: () => void
  /** The page's per-row live facts — the same resolver its roster filters
   *  read, so the dot here can never disagree with the filter that counts it.
   *  Omitted, a row shows only its own `running`. */
  signals?: (m: MemberRosterRow) => MemberSignals
  /** The automation record on each crewmate's own slot, for the on-watch ring
   *  the row's face draws -- the roster's `automationOf`. Omitted, the ring
   *  reads the heartbeat alone (`signals(m).patrolling`). */
  automationOf?: (m: MemberRosterRow) => AutomationRecord | null
  /** Bumped by the page to open the list from outside (the unread column's
   *  "+N" pill). A counter, not a boolean, so two requests in a row both open. */
  openRequest?: number
  /** The crewmate whose face is centre stage in the first Mate's entrance
   *  (#23207): its face in the stack holds its place unpainted until the face
   *  lands, as the header pill's slot and the roster row do. `''` for none. */
  faceOnStageFor?: string
  className?: string
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [importing, setImporting] = useState(false)
  const [query, setQuery] = useState('')
  // An outside request opens the list; the first render's 0 is not a request.
  const seenRequest = useRef(openRequest)
  if (openRequest !== seenRequest.current) {
    seenRequest.current = openRequest
    if (!open) setOpen(true)
  }
  // Set by the footer's roster action: the page moves focus into the column it
  // opens, so the popover must not hand focus back to this (now hidden) chip.
  const toRosterRef = useRef(false)
  const q = query.trim().toLowerCase()
  // The rows the roster is ABOUT, through the column's own rule: created and
  // chatted-with crewmates, the starred, the default crew, plus whichever one is
  // open. Searched over below, but the chip's count and faces read THIS -- a
  // tally over `members` counted rows the user cannot see in the column.
  const population = useMemo(
    () => rosterPopulation(members, { search: '', chosen: activeName }),
    [members, activeName],
  )
  // Somebody you are NOT talking to is waiting on you.
  const othersNeedYou = useMemo(
    () => population.some((m) => m.name !== activeName && signals(m).needsYou),
    [population, activeName, signals],
  )
  // The chip's accessible name is the page's own word, then the signal when
  // there is one; the action stays in the tooltip. The word also slides into
  // view on hover or focus, so a reader who wonders what the faces are gets
  // the answer without opening the list.
  const rosterLabel = t('pages.membersPage.title')
  const needsYouLabel = t('pages.membersPage.filter_status_needs_you')
  // Joined by the locale's list formatter, not a literal separator (i18n-catalog).
  const chipLabel = othersNeedYou ? fmtList([rosterLabel, needsYouLabel], { type: 'unit' }) : rosterLabel
  // With a search typed the search decides alone, over the FULL roster, so a
  // hidden row is reachable here the same way it is from the column's search.
  // The open crewmate is pinned to the top, the rest keep the roster's order:
  // the list opens to "who am I talking to, and who else is there", and with
  // no check mark on the row its place is what says which one is open.
  const shown = useMemo(() => {
    const rows = members.filter((m) => rosterShows(m, { search: q, chosen: activeName }))
    const i = rows.findIndex((m) => m.name === activeName)
    return i > 0 ? [rows[i], ...rows.slice(0, i), ...rows.slice(i + 1)] : rows
  }, [members, q, activeName])
  // The OTHER crewmates lead the stack, in roster order: the open one is already
  // named by the identity pill beside this chip, and a second copy of its face
  // here read as a duplicate rather than as "the rest of the crew". It joins the
  // stack only when it is the whole roster, so the chip never draws empty.
  const stack = useMemo(() => {
    const rest = population.filter((m) => m.name !== activeName)
    if (rest.length > 0) return rest.slice(0, STACK_FACES)
    const active = population.find((m) => m.name === activeName)
    return active ? [active] : []
  }, [population, activeName])

  return (
    <Popover
      guideScope="menu:members.switcher"
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setQuery('')
      }}
    >
      <PopoverTrigger asChild>
        {/* A Glass chip (#18325): the header floats over the thread with no
            background of its own, so a bare button here sat on scrolled rows
            and read as text printed over text. The chip is the identity
            pill's material at a smaller size; `glass-hover` is its hover step
            and its open state, where the old button tinted itself. */}
        <Glass
          as="button"
          type="button"
          variant="chip"
          radius={999}
          className={cn(
            'group glass-hover flex items-center gap-1.5 h-[42px] pl-2.5 pr-2.5 rounded-full text-text data-[state=open]:[--glass-tint:color-mix(in_srgb,var(--text)_8%,var(--glass-tint-step))] transition-colors cursor-pointer focus-ring shrink-0',
            className,
          )}
          aria-label={chipLabel}
          // A static key: the generator reads the registered label off this attribute.
          title={t('pages.membersPage.switch_crewmate')}
          aria-haspopup="dialog"
          aria-expanded={open}
          data-testid="crewmate-switcher"
          {...uiLocation('members.switcher')}
          data-needs-you={othersNeedYou || undefined}
        >
          {/* `relative` so the needs-you dot can sit over the stack's corner
              without taking a column of its own: the chip's width is the same
              with the dot and without it. */}
          <span className="relative flex items-center" aria-hidden="true">
            {stack.map((m, i) => (
              <span
                key={m.name}
                className={cn('rounded-full ring-2 ring-bg group-hover:ring-bg-hover group-data-[state=open]:ring-bg-hover transition-colors', i > 0 && '-ml-2')}
                style={{ zIndex: STACK_FACES - i, ...(faceOnStageFor === m.name ? { visibility: 'hidden' as const } : {}) }}
                data-testid="crewmate-switcher-face"
                data-entrance-empty={faceOnStageFor === m.name ? '' : undefined}
              >
                <CrewStateAvatar seed={m.name} avatar={m.avatar} slotKey={m.slot_key} running={signals(m).running} size={22} working="subtle" />
              </span>
            ))}
            {othersNeedYou && (
              <span
                className="absolute -right-0.5 -top-0.5 z-10 w-2.5 h-2.5 rounded-full border-2 border-bg group-hover:border-bg-hover group-data-[state=open]:border-bg-hover bg-warn"
                data-testid="crewmate-switcher-needs-you"
              />
            )}
          </span>
          {/* The roster's name, folded to a zero-width grid column at rest and
              slid open on hover, keyboard focus or while the list is open
              (0fr -> 1fr animates the width; cut under reduced motion). It
              stays in the DOM folded, but the button is named by `aria-label`
              above, so the fold changes nothing for a screen reader. */}
          <span
            className="grid grid-cols-[0fr] group-hover:grid-cols-[1fr] group-focus-visible:grid-cols-[1fr] group-data-[state=open]:grid-cols-[1fr] transition-[grid-template-columns] duration-200 ease-out motion-reduce:transition-none"
            aria-hidden="true"
            data-testid="crewmate-switcher-label"
          >
            <span className="overflow-hidden whitespace-nowrap text-[12.5px] font-semibold">
              <span className="pl-0.5 pr-1">{rosterLabel}</span>
            </span>
          </span>
          <ChevronDown size={13} className={cn('text-muted transition-transform', open && 'rotate-180')} aria-hidden="true" />
        </Glass>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={8}
        variant="list"
        className="w-80"
        aria-label={t('pages.membersPage.title')}
        onCloseAutoFocus={(e) => { if (toRosterRef.current) { toRosterRef.current = false; e.preventDefault() } }}
        data-testid="crewmate-switcher-list"
      >
        <label className="flex items-center gap-2 h-9 px-3 mx-0.5 mt-0.5 mb-1.5 rounded-xl bg-bg border border-border text-muted focus-within:border-accent">
          <Search size={14} aria-hidden="true" className="shrink-0" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('pages.membersPage.find_crewmate')}
            aria-label={t('pages.membersPage.find_crewmate')}
            className="flex-1 min-w-0 bg-transparent outline-hidden text-[13px] text-text placeholder:text-muted"
            data-testid="crewmate-switcher-search"
            autoFocus
          />
        </label>
        <div className="max-h-[min(60vh,420px)] overflow-y-auto" role="listbox" aria-label={t('pages.membersPage.title')}>
          {shown.length === 0 ? (
            <div className="px-3 py-5 text-center text-[13px] text-muted" data-testid="crewmate-switcher-empty">
              {t('pages.membersPage.no_crewmate_match')}
            </div>
          ) : (
            shown.map((m) => {
              const on = m.name === activeName
              const s = signals(m)
              return (
                <button
                  key={m.name}
                  type="button"
                  role="option"
                  aria-selected={on}
                  onClick={() => {
                    setOpen(false)
                    setQuery('')
                    if (!on) onPick(m.name)
                  }}
                  className={cn(
                    'flex items-center gap-2.5 w-full px-2.5 py-2 rounded-xl text-left transition-colors cursor-pointer',
                    on ? 'bg-accent-subtle' : 'hover:bg-bg-hover',
                  )}
                  data-testid="crewmate-switcher-row"
                  data-active={on || undefined}
                >
                  {/* The roster row itself (face, name, status line, right-edge
                      markers): CrewmateRowBody, so this list and the full
                      roster draw one row. The popover is an elevated surface,
                      so the presence dot's cut-out border is `bg-elevated`. */}
                  <CrewmateRowBody
                    m={m}
                    signals={s}
                    automation={automationOf?.(m) ?? null}
                    slotKey={m.slot_key}
                    size={32}
                    surface="elevated"
                    testIdPrefix="crewmate-switcher-row"
                  />
                  {/* No check mark: the open crewmate is already the row with
                      `aria-selected` and the accent-subtle fill. */}
                </button>
              )
            })
          )}
        </div>
        {(onCreate || onShowRoster) && (
          <div className="mt-1 pt-1 border-t border-border">
            {onCreate && (
              <button
                type="button"
                onClick={() => {
                  setOpen(false)
                  onCreate()
                }}
                className="flex items-center gap-2.5 w-full px-2.5 py-2 rounded-xl text-left text-[13px] font-semibold text-accent hover:bg-bg-hover cursor-pointer"
                data-testid="crewmate-switcher-create"
                {...uiLocation('members.switcher.new')}
              >
                <span className="w-8 h-8 rounded-full bg-accent-subtle grid place-items-center" aria-hidden="true">
                  <Plus size={15} />
                </span>
                {t('pages.membersPage.add_member')}
              </button>
            )}
            {/* Import from file sits under New crewmate and is held with it:
                both end in a new crewmate, and only the owner's press on the
                import card creates one (rfc-crewmate-share.md §5.4). */}
            {onCreate && (
              <button
                type="button"
                onClick={() => {
                  setOpen(false)
                  setImporting(true)
                }}
                className="flex items-center gap-2.5 w-full px-2.5 py-2 rounded-xl text-left text-[13px] font-semibold text-text hover:bg-bg-hover cursor-pointer"
                data-testid="crewmate-switcher-import"
                {...uiLocation('members.switcher.import')}
              >
                <span className="w-8 h-8 rounded-full bg-bg-hover grid place-items-center text-muted" aria-hidden="true">
                  <FileUp size={15} />
                </span>
                {t('pages.membersPage.import_from_file')}
              </button>
            )}
            {onShowRoster && (
              <button
                type="button"
                onClick={() => {
                  toRosterRef.current = true
                  setOpen(false)
                  onShowRoster()
                }}
                className="flex items-center gap-2.5 w-full px-2.5 py-2 rounded-xl text-left text-[13px] font-semibold text-text hover:bg-bg-hover cursor-pointer"
                data-testid="crewmate-switcher-roster"
                {...uiLocation('members.switcher.show-roster')}
              >
                <span className="w-8 h-8 rounded-full bg-bg-hover grid place-items-center text-muted" aria-hidden="true">
                  <PanelLeft size={15} />
                </span>
                {t('pages.membersPage.roster_show')}
              </button>
            )}
          </div>
        )}
      </PopoverContent>
      {/* Mounted only while open: it reads the import slot's cards. */}
      {onCreate && importing && <CrewmateImportDialog open onOpenChange={setImporting} />}
    </Popover>
  )
}

/** How a pill enters and leaves: it grows out of the row above (height, so the
 *  pills below move with it) while fading in; leaving runs the same in reverse.
 *  The column floats over the conversation, so nothing else moves. */
const PILL_SPRING = { duration: 0.22, ease: [0.2, 0.8, 0.2, 1] as const }

/**
 * The unread column under the switcher chip (issue #19045): one small pill per
 * crewmate with an unread message who is not the open one, in roster order --
 * the face with the unread dot on its corner, then the display name. A pill
 * opens that thread the way a list row does; the page clears the unread, and
 * the pill leaves. Past `UNREAD_PILLS` the rest fold into one "+N" pill that
 * hands the page `onMore` (it opens the switcher list, whose rows carry the
 * same dot). Nothing unread, nothing drawn.
 *
 * It is a separate component, not a child of the chip, because the page hangs
 * it from the thread header's bottom edge, absolutely: it adds nothing to the
 * header's measured height (the thread's top inset), so the conversation
 * keeps scrolling up under the band, and under these Glass pills, the way it
 * scrolls under the identity pill. Pills float over the text; they never push
 * it. The same `rosterPopulation` rule as the chip decides who can have a
 * pill, so a row the chip does not list never gets one.
 */
export function CrewmateUnreadColumn({
  members,
  activeName,
  onPick,
  onMore,
  signals = rowOnlySignals,
  className,
}: {
  members: MemberRosterRow[]
  activeName: string
  onPick: (name: string) => void
  /** Opens the switcher's list; the "+N" pill's action. */
  onMore: () => void
  signals?: (m: MemberRosterRow) => MemberSignals
  className?: string
}) {
  const { t } = useTranslation()
  const reduced = useReducedMotion()
  const population = useMemo(
    () => rosterPopulation(members, { search: '', chosen: activeName }),
    [members, activeName],
  )
  const unreadRows = useMemo(
    () => population.filter((m) => m.name !== activeName && signals(m).unread),
    [population, activeName, signals],
  )
  const pills = unreadRows.slice(0, UNREAD_PILLS)
  const rest = unreadRows.length - pills.length
  const unreadLabel = t('pages.membersPage.unread_message')
  // Height + opacity, from the row above; reduced motion cuts to the end state.
  const grow = reduced
    ? { initial: false as const, animate: { height: 'auto', opacity: 1 }, exit: { height: 0, opacity: 0, transition: { duration: 0 } }, transition: { duration: 0 } }
    : { initial: { height: 0, opacity: 0, y: -6 }, animate: { height: 'auto', opacity: 1, y: 0 }, exit: { height: 0, opacity: 0, y: -6 }, transition: PILL_SPRING }

  return (
    <div className={cn('flex flex-col items-start', className)} data-testid="crewmate-switcher-unread-column" data-count={unreadRows.length}>
      <AnimatePresence initial={false}>
        {pills.map((m) => {
          const label = crewDisplayName(m)
          return (
            <motion.div key={m.name} {...grow} className="overflow-hidden flex px-[3px] -mx-[3px] pb-[3px] -mb-[3px]" data-testid="crewmate-switcher-unread-row">
              <Glass
                as="button"
                type="button"
                variant="chip"
                radius={999}
                // The gap between pills (and above the first) rides INSIDE the
                // animated box as a top margin, so it grows and shrinks with it.
                className="glass-hover mt-1.5 flex items-center gap-1.5 h-[34px] pl-1.5 pr-2.5 max-w-[240px] rounded-full text-text transition-colors cursor-pointer focus-ring pointer-events-auto"
                // Name, then state: the pill IS the unread signal, so the dot's
                // meaning rides in the button's name rather than a second image.
                aria-label={fmtList([label, unreadLabel], { type: 'unit' })}
                // A static key: the generator reads the registered label off this attribute.
                title={t('pages.membersPage.unread_message')}
                onClick={() => onPick(m.name)}
                data-testid="crewmate-switcher-unread-pill"
                data-member={m.name}
                {...uiLocation('members.switcher.unread')}
              >
                <span className="relative flex shrink-0" aria-hidden="true">
                  <CrewStateAvatar seed={m.name} avatar={m.avatar} slotKey={m.slot_key} running={signals(m).running} size={22} working="subtle" />
                  {/* The roster row's unread dot, on the face's corner: accent,
                      never the green of the working presence dot. */}
                  <span className="absolute -right-0.5 -bottom-0.5 w-2.5 h-2.5 rounded-full border-2 border-bg bg-accent" />
                </span>
                <span className="text-[12.5px] font-medium truncate" aria-hidden="true">{label}</span>
              </Glass>
            </motion.div>
          )
        })}
        {rest > 0 && (
          <motion.div key="more" {...grow} className="overflow-hidden flex px-[3px] -mx-[3px] pb-[3px] -mb-[3px]" data-testid="crewmate-switcher-unread-row">
            <Glass
              as="button"
              type="button"
              variant="chip"
              radius={999}
              className="glass-hover mt-1.5 flex items-center h-[34px] px-3 rounded-full text-[12.5px] font-medium text-muted transition-colors cursor-pointer focus-ring pointer-events-auto"
              onClick={onMore}
              // A static key: the generator reads the registered label off this attribute.
              title={t('pages.membersPage.switch_crewmate')}
              data-testid="crewmate-switcher-unread-more"
              {...uiLocation('members.switcher.unread-more')}
            >
              {t('pages.membersPage.switcher_more_unread', { n: rest })}
            </Glass>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}