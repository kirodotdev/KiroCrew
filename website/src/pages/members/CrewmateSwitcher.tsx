import { useMemo, useState } from 'react'
import { Check, ChevronDown, PanelLeft, Plus, Search } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import type { MemberRosterRow } from '../../api/client'
import { crewDisplayName } from '../../components/AgentSelector'
import CrewStateAvatar from '../../components/CrewStateAvatar'
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover'
import { fmtList } from '../../i18n/format'
import { cn } from '../../lib/utils'
import { rosterPopulation, rosterShows, type MemberSignals } from './rosterFilter'

/** How many faces the closed chip stacks. Three is enough to read as "a crew"
 *  without the chip growing with the roster; the count beside them says how many
 *  there really are. */
const STACK_FACES = 3

/** The signals a row carries when the page hands none in: presence from the
 *  roster row itself, nothing parked on the user, nothing unread. */
const rowOnlySignals = (m: MemberRosterRow): MemberSignals => ({
  running: !!m.running,
  needsYou: false,
  unread: false,
  patrolling: false,
})

/**
 * The crewmate switcher — the roster folded into one header chip.
 *
 * The Crewmates page used to keep the whole roster as a standing left column.
 * That column is gone from the DM view (product decision, crewmate-panel IA):
 * the thread and the crewmate's own panel get the width, and the roster is
 * reached on demand from this chip. Closed, it shows the page's name, the first
 * faces and the crewmate count; open, it is a searchable list of every
 * crewmate, the current one marked, and a "New crewmate" footer that opens the same create dialog the
 * old column's "+" did (one write path, one more front door). A second footer
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
  defaultAgent,
  activeName,
  onPick,
  onCreate,
  rosterShown = false,
  onToggleRoster,
  signals = rowOnlySignals,
  className,
}: {
  /** The FULL roster, in the page's display order. The hide rule below decides
   *  which of these rows the chip lists, so hidden rows must be passed: they are
   *  what the search reaches. */
  members: MemberRosterRow[]
  /** The default crew's name, for the hide rule -- the page's `['default-agent']`
   *  read. `''` while unknown, `null` when that read FAILED, which lists every
   *  row rather than risk hiding the default crew (see `listedByDefault`). */
  defaultAgent: string | null
  /** The crewmate whose thread is open, by exact name. */
  activeName: string
  onPick: (name: string) => void
  /** Opens the New crewmate dialog. Omitted while creation is held. */
  onCreate?: () => void
  /** Whether the roster column is currently showing beside the thread; names
   *  the footer action (show / hide). */
  rosterShown?: boolean
  /** Shows or hides the roster column beside the thread. Omitted, the footer
   *  action is not drawn. */
  onToggleRoster?: () => void
  /** The page's per-row live facts — the same resolver its roster filters
   *  read, so the dot here can never disagree with the filter that counts it.
   *  Omitted, a row shows only its own `running`. */
  signals?: (m: MemberRosterRow) => MemberSignals
  className?: string
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  // The rows the roster is ABOUT, through the column's own rule: created and
  // chatted-with crewmates, the starred, the default crew, plus whichever one is
  // open. Searched over below, but the chip's count and faces read THIS -- a
  // tally over `members` counted rows the user cannot see in the column.
  const population = useMemo(
    () => rosterPopulation(members, { search: '', defaultAgent, chosen: activeName }),
    [members, defaultAgent, activeName],
  )
  // Somebody you are NOT talking to is waiting on you.
  const othersNeedYou = useMemo(
    () => population.some((m) => m.name !== activeName && signals(m).needsYou),
    [population, activeName, signals],
  )
  // The chip carries the page's own name as visible text: a bare /members opens
  // the last crewmate's thread, so this chip is the roster a first look sees,
  // and faces plus a number alone did not read as "the crew list". The
  // accessible name starts with that visible word (label in name), then the
  // signal when there is one; the action stays in the tooltip.
  const rosterLabel = t('pages.membersPage.title')
  const switchLabel = t('pages.membersPage.switch_crewmate')
  const needsYouLabel = t('pages.membersPage.filter_status_needs_you')
  // Joined by the locale's list formatter, not a literal separator (i18n-catalog).
  const chipLabel = othersNeedYou ? fmtList([rosterLabel, needsYouLabel], { type: 'unit' }) : rosterLabel
  // With a search typed the search decides alone, over the FULL roster, so a
  // hidden row is reachable here the same way it is from the column's search.
  const shown = useMemo(
    () => members.filter((m) => rosterShows(m, { search: q, defaultAgent, chosen: activeName })),
    [members, q, defaultAgent, activeName],
  )
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
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (!next) setQuery('')
      }}
    >
      <PopoverTrigger asChild>
        {/* A plain header button, not a second Glass pill: two pills of
            different heights side by side read as a mismatched pair. */}
        <button
          type="button"
          className={cn(
            'group flex items-center gap-1.5 h-[42px] pl-2.5 pr-3 rounded-full text-text hover:bg-bg-hover data-[state=open]:bg-bg-hover transition-colors cursor-pointer focus-ring shrink-0',
            className,
          )}
          aria-label={chipLabel}
          title={switchLabel}
          aria-haspopup="dialog"
          aria-expanded={open}
          data-testid="crewmate-switcher"
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
                style={{ zIndex: STACK_FACES - i }}
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
          <span className="text-[12.5px] font-semibold" aria-hidden="true" data-testid="crewmate-switcher-label">{rosterLabel}</span>
          <span className="text-[12.5px] text-muted tabular-nums" aria-hidden="true" data-testid="crewmate-switcher-count">{population.length}</span>
          <ChevronDown size={13} className={cn('text-muted transition-transform', open && 'rotate-180')} aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={8}
        variant="list"
        className="w-80"
        aria-label={t('pages.membersPage.title')}
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
              const label = crewDisplayName(m)
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
                  <span className="relative shrink-0">
                    <CrewStateAvatar seed={m.name} avatar={m.avatar} slotKey={m.slot_key} running={s.running} size={32} working="subtle" />
                    {/* The roster row's presence dot: shown only while the
                        crewmate works, and spelled by the "Working" line below
                        rather than a second name. */}
                    {s.running && (
                      <span
                        className="absolute -right-0.5 -bottom-0.5 w-2.5 h-2.5 rounded-full border-2 border-bg-elevated bg-ok"
                        aria-hidden="true"
                        data-testid="crewmate-switcher-presence-dot"
                      />
                    )}
                  </span>
                  <span className="flex-1 min-w-0 leading-tight">
                    <span className="block text-[13px] font-semibold truncate">{label}</span>
                    <span className="block text-[11.5px] text-muted truncate">
                      {s.running ? t('pages.membersPage.drawer_working') : (m.kiro_agent || m.last_message || t('pages.membersPage.pill_idle'))}
                    </span>
                  </span>
                  {/* Right-edge markers, the IM convention the roster row uses
                      for its unread dot. Each is a named image, so the state is
                      part of the option's accessible name, not only a colour.
                      Needs-you leads: it is the one that asks something of
                      the reader. */}
                  {/* Each marker is a dot AND a word: colour alone is not a
                      state a reader can be sure of (two small dots, one amber
                      and one purple, are the same mark to a third of readers).
                      The word is `aria-hidden` because the named image already
                      carries the fuller sentence into the option's name — a
                      visible word that was also spoken would say it twice. */}
                  {s.needsYou && (
                    <span className="flex items-center gap-1 shrink-0" data-testid="crewmate-switcher-row-needs-you">
                      <span
                        className="w-2 h-2 rounded-full shrink-0 bg-warn"
                        role="img"
                        aria-label={needsYouLabel}
                        title={needsYouLabel}
                        data-testid="crewmate-switcher-needs-you-dot"
                      />
                      <span className="text-[11px] text-muted whitespace-nowrap" aria-hidden="true">{t('pages.membersPage.team_needs_you')}</span>
                    </span>
                  )}
                  {s.unread && (
                    <span className="flex items-center gap-1 shrink-0" data-testid="crewmate-switcher-unread">
                      <span
                        className="w-2 h-2 rounded-full shrink-0 bg-accent"
                        role="img"
                        aria-label={t('pages.membersPage.unread_message')}
                        title={t('pages.membersPage.unread_message')}
                        data-testid="crewmate-switcher-unread-dot"
                      />
                      <span className="text-[11px] text-muted whitespace-nowrap" aria-hidden="true">{t('pages.membersPage.switcher_unread')}</span>
                    </span>
                  )}
                  <Check size={15} className={cn('shrink-0 text-accent', !on && 'opacity-0')} aria-hidden="true" />
                </button>
              )
            })
          )}
        </div>
        {(onCreate || onToggleRoster) && (
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
              >
                <span className="w-8 h-8 rounded-full bg-accent-subtle grid place-items-center" aria-hidden="true">
                  <Plus size={15} />
                </span>
                {t('pages.membersPage.add_member')}
              </button>
            )}
            {onToggleRoster && (
              <button
                type="button"
                onClick={() => {
                  setOpen(false)
                  onToggleRoster()
                }}
                className="flex items-center gap-2.5 w-full px-2.5 py-2 rounded-xl text-left text-[13px] font-semibold text-text hover:bg-bg-hover cursor-pointer"
                aria-pressed={rosterShown}
                data-testid="crewmate-switcher-roster"
              >
                <span className="w-8 h-8 rounded-full bg-bg-hover grid place-items-center text-muted" aria-hidden="true">
                  <PanelLeft size={15} />
                </span>
                {rosterShown ? t('pages.membersPage.roster_hide') : t('pages.membersPage.roster_show')}
              </button>
            )}
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}
