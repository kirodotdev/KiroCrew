/** The crewmate roster: a crew as a card or as a table row, their badges and
 *  bindings, and the roster's toolbar (filter, Cards / List, the change-default
 *  link, Add crewmate) and list. Picking a crew opens the editor the page owns. */
import { Link } from 'react-router-dom'
import { Boxes, FolderOpen, Database, Sparkles, Plus, Users, LayoutGrid, Rows3, ChevronRight } from 'lucide-react'
import Clickable from '../components/Clickable'
import { SendBtn, Badge, SearchInput, EmptyState } from '../components/ui'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '../components/ui/table'
import SegmentedControl from '../components/SegmentedControl'
import InfoTip from '../components/InfoTip'
import { FOCUSABLE } from '../hooks/useDialogFocusTrap'
import CrewAvatar from '../components/CrewAvatar'
import { crewDisplayName, type KiroCrewAgent } from '../components/AgentSelector'
import { SourceBadge } from '../components/SourceBadge'
import { templateSourceLabel } from '../lib/templateSource'
import { DEFAULT_CREWMATE_PATH } from './overview/defaultCrewmateLink'
import { i18nT } from '../i18n/t'
import { uiLocation } from '../uiLocations/uiLocation'
import { guidePick } from '../uiLocations/targetRegistry'
import ErrorNotice from '../components/ErrorNotice'
import { memoryStoreLabel, useRosterBuiltFrom, type MemoryStores } from './kiroCrewAgentsPageRosterLabels'

/** Roster layout. `cards` is the roomy grid, `list` the compact table. */
export type CrewView = 'cards' | 'list'

/** Where the roster layout is remembered. Mirrors `mc-artifacts-view`, which is
 *  how the Artifacts page persists the same grid/table choice — one convention
 *  for both surfaces rather than a second scheme for this one. */
export const VIEW_KEY = 'mc-crews-view'

/** Read the remembered layout. Guarded because `localStorage` throws outright
 *  in a partitioned/blocked-storage context rather than returning null. */
export function readStoredView(): CrewView {
  try {
    return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'cards'
  } catch {
    return 'cards'
  }
}

/**
 * Which of a crew's two stores another crew also points at. Drives a specific
 * badge instead of a bare "Shared", which a first-run reviewer read as "shared
 * with my teammates" — the scariest possible reading and the wrong one.
 */
export type SharedKind = 'none' | 'memory' | 'files' | 'both'

/** One binding shown on a roster card: icon, what it is, what it points at. */
function Binding({ icon, label, value, muted, plain, note, testId }: {
  icon: React.ReactNode
  label: string
  value: string
  muted?: boolean
  /** A display name or label rather than an identifier: not set in mono. */
  plain?: boolean
  /** Warning suffix, e.g. that another crew points at this same store. */
  note?: string
  testId?: string
}) {
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="text-muted">{icon}</span>
      <span className="min-w-0">
        <span className="block text-[10px] uppercase tracking-wider text-muted">{label}</span>
        {/* `pr-0.5` is load-bearing with `truncate`: an italic glyph leans past
            its own advance width, and `overflow:hidden` clips that overhang
            rather than showing an ellipsis — "Inherited" rendered as
            "Inheritea". Two pixels of gutter is enough for the lean. */}
        <span className="block truncate pr-0.5 text-[12px]">
          <span className={muted ? 'italic text-muted' : plain ? 'text-text' : 'font-mono text-text'} data-testid={testId}>{value}</span>
          {note && <span className="ml-1.5 text-[11px] text-warn">{note}</span>}
        </span>
      </span>
    </div>
  )
}

/** The roster's `default` badge: a status stamp, and only that. The path to
 *  CHANGE the default is the toolbar's "Change default crewmate ›" link
 *  (`ChangeDefaultLink`), not the badge — a badge that carried the verb ate the
 *  card's own name on a 290px card ("d…"), and a card header is one line by
 *  design. */
function DefaultBadge() {
  const label = i18nT('pages.kiroCrewAgentsPage.default_2')
  return (
    // `Badge` is an inline-flex box, and `text-overflow` never draws an ellipsis
    // on a flex container -- the text was clipped mid-glyph ("По умолч") on the
    // 290px card. The truncation lives on an inner inline span, where it works.
    <Badge variant="ok" className="min-w-0 max-w-[45%] shrink" title={label}>
      <span className="min-w-0 truncate">{label}</span>
    </Badge>
  )
}

/** The roster's one visible path to where the default crewmate is changed: a
 *  link in the toolbar, beside the view toggle, to the Default crewmate row on
 *  Developer → Config (ringed on arrival), with the row's own sentence as its
 *  title. Says its destination in words at rest — a hover title is read by
 *  nobody who did not already dare. Rendered only past one crewmate: with a
 *  single crewmate there is nothing to change to. It is a link, not a picker:
 *  the roster still opens on the roster. */
function ChangeDefaultLink() {
  return (
    <Link
      to={DEFAULT_CREWMATE_PATH}
      title={i18nT('pages.kiroCrewAgentsPage.change_default_hint')}
      className="inline-flex min-w-0 items-center gap-0.5 rounded text-[12.5px] leading-snug text-accent hover:underline focus-ring"
    >
      <span className="min-w-0">{i18nT('pages.kiroCrewAgentsPage.change_default_crewmate')}</span>
      <ChevronRight size={13} aria-hidden="true" className="shrink-0" />
    </Link>
  )
}

/** The roster's provenance badge, in the same words the Custom agents tab uses
 *  for a template's origin (`templateSourceLabel`): a crew record's `source` is
 *  `kirocrew` | `package` | `builtin` (agent_discovery), where `builtin` is a
 *  plain `~/.kiro/agents/<name>.json` spec — the classifier's `custom` — and
 *  never an author claim. An unknown token falls back to itself. */
export function CrewSourceBadge({ source }: { source: string }) {
  const label = templateSourceLabel({ source }) || source
  return <SourceBadge source={source}>{label}</SourceBadge>
}

/** Kiro Crew is product-owned; every other source carries useful provenance. */
export function showsCrewSourceBadge(source?: string): source is string {
  return Boolean(source && source !== 'kirocrew')
}

/** One crew in the roster. The whole card opens the editor panel. */
function CrewCard({ agent, isDefault, shared, memoryStores, onOpen }: {
  agent: KiroCrewAgent
  isDefault: boolean
  shared: SharedKind
  memoryStores: MemoryStores | undefined
  onOpen: () => void
}) {
  const { label: builtFrom, error: builtFromError, report: builtFromReport } = useRosterBuiltFrom(agent)
  const sharedNote = i18nT('pages.kiroCrewAgentsPage.shared_lower')
  const filesShared = shared === 'files' || shared === 'both'
  const memoryShared = shared === 'memory' || shared === 'both'
  const desc = describeCrew(agent, isDefault)
  return (
    <Clickable
      onClick={onOpen}
      aria-label={i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: crewDisplayName(agent) })}
      data-testid="crew-card"
      {...guidePick(crewDisplayName(agent))}
      className={`group flex flex-col gap-3 rounded-lg border bg-card p-3.5 transition-all
                  hover:border-border-strong hover:shadow-md focus-ring
                  ${isDefault ? 'border-accent-subtle' : 'border-border'}`}
      style={agent.session_color ? { borderLeftColor: agent.session_color, borderLeftWidth: '3px' } : undefined}
    >
      <div className="flex items-center gap-3">
        <CrewAvatar seed={agent.name} avatar={agent.avatar} size={38} />
        {/* Fixed height for the whole header block. Badges are slightly taller
            than plain text, so cards carrying a `default` badge would otherwise
            push their binding grid lower than a card without one, and the row
            would read as ragged. Sized for one name line plus TWO description
            lines: 20px + 34px, and the description reserves its 34px whether or
            not it fills them. */}
        <div className="flex h-[54px] min-w-0 flex-1 flex-col justify-center">
          {/* Kept to a single line: a wrapping badge row made this header one
              line taller than its neighbours', which knocked the binding grids
              out of alignment across the row. The name truncates and the
              badges hold their size, so the row can never wrap. */}
          <div className="flex items-center gap-2 min-w-0">
            <span className="min-w-[3rem] flex-1 truncate font-mono text-[14px] font-semibold text-text-strong">{crewDisplayName(agent)}</span>
            {isDefault && <DefaultBadge />}
            {showsCrewSourceBadge(agent.source) && <CrewSourceBadge source={agent.source} />}
          </div>
          {/* Two lines rather than one. A crew description is a sentence about
              what the crew is FOR, and a single truncated line cut nearly all
              of them mid-word. `line-clamp-2` with an explicit line-height and
              a matching fixed height: without the fixed height the clamp leaks
              a sliver of a third line at some font sizes, and cards with a
              one-line description sit shorter than their neighbours. The full
              text stays reachable via the native tooltip and the list view. */}
          <div className="mt-0.5 min-w-0">
            <span
              className={`line-clamp-2 h-[34px] text-[12px] leading-[17px] text-muted ${desc.placeholder ? 'italic' : ''}`}
              title={desc.placeholder ? undefined : desc.text}
            >
              {desc.text}
            </span>
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border pt-3">
        <Binding icon={<Boxes className="lucide-inline" aria-hidden="true" />} label={i18nT('pages.kiroCrewAgentsPage.built_from')} value={builtFrom} plain testId="crew-built-from" />
        {/* A sentence, not a word: it takes the whole row, the hand-off under it. */}
        {builtFromError && <ErrorNotice message={builtFromError} variant="inline" askAgent actionPlacement="below" report={builtFromReport} className="col-span-2" testId="crew-built-from-error" />}
        <Binding icon={<FolderOpen className="lucide-inline" aria-hidden="true" />} label={i18nT('pages.kiroCrewAgentsPage.workspace_2')} value={agent.workspace} note={filesShared ? sharedNote : undefined} />
        <Binding icon={<Database className="lucide-inline" aria-hidden="true" />} label={i18nT('pages.kiroCrewAgentsPage.memory_store')} value={memoryStoreLabel(agent.name, agent.memory_store, memoryStores)} plain note={memoryShared ? sharedNote : undefined} />
        <Binding
          icon={<Sparkles className="lucide-inline" aria-hidden="true" />}
          label={i18nT('pages.kiroCrewAgentsPage.model')}
          value={agent.model || i18nT('pages.kiroCrewAgentsPage.inherited')}
          muted={!agent.model}
        />
      </div>
    </Clickable>
  )
}

/**
 * What a crew's description line shows, so the card and the row cannot drift.
 *
 * A crew with no description read as blank in the card but italic "No
 * description" in the row, i.e. the same crew looked different per view. The
 * default crew keeps its own hint instead — that line is what tells a first-run
 * user why this crew matters, and a test asserts it.
 *
 * Returns `text` plus whether it is real copy: a placeholder must render italic
 * and must NOT become a tooltip, or every empty crew advertises a blank bubble.
 */
function describeCrew(agent: KiroCrewAgent, isDefault: boolean): { text: string; placeholder: boolean } {
  if (agent.description) return { text: agent.description, placeholder: false }
  if (isDefault) return { text: i18nT('pages.kiroCrewAgentsPage.used_for_all_new_chats'), placeholder: true }
  return { text: i18nT('pages.kiroCrewAgentsPage.no_description'), placeholder: true }
}

/** One crew as a table row. The row opens the editor; the accessible target is
 *  the real button in the name cell, so table semantics stay intact — a `<tr>`
 *  given `role="button"` stops being announced as a row at all. */
function CrewRow({ agent, isDefault, shared, memoryStores, onOpen }: {
  agent: KiroCrewAgent
  isDefault: boolean
  shared: SharedKind
  memoryStores: MemoryStores | undefined
  onOpen: () => void
}) {
  const { label: builtFrom, error: builtFromError, report: builtFromReport } = useRosterBuiltFrom(agent)
  const sharedNote = i18nT('pages.kiroCrewAgentsPage.shared_lower')
  const filesShared = shared === 'files' || shared === 'both'
  const memoryShared = shared === 'memory' || shared === 'both'
  const desc = describeCrew(agent, isDefault)
  return (
    <TableRow
      data-testid="crew-row"
      {...guidePick(crewDisplayName(agent))}
      className={`cursor-pointer ${isDefault ? 'bg-accent-subtle/30' : ''}`}
      // Convenience only: the whole row is a click target, but a click that
      // landed on the name control must not fire this too or the editor would be
      // asked to open twice for one gesture. Reuses the focus trap's FOCUSABLE
      // selector rather than spelling out a second list: `Clickable` renders a
      // `div[role=button][tabindex=0]`, so a hand-written `closest('button')`
      // would silently never match it, and one definition of "interactive
      // element" cannot drift out of step with itself.
      onClick={e => { if (!(e.target as HTMLElement).closest(FOCUSABLE)) onOpen() }}
    >
      <TableCell>
        <div className="flex items-center gap-2.5 min-w-0">
          <CrewAvatar seed={agent.name} avatar={agent.avatar} size={28} />
          <div className="min-w-0">
            <div className="flex items-center gap-2 min-w-0">
              <Clickable
                onClick={onOpen}
                aria-label={i18nT('pages.kiroCrewAgentsPage.edit_crew_named', { name: crewDisplayName(agent) })}
                className="truncate rounded font-mono text-[12.5px] font-semibold text-text-strong focus-ring"
              >
                {crewDisplayName(agent)}
              </Clickable>
              {crewDisplayName(agent) !== agent.name && (
                <span className="truncate font-mono text-[11px] text-muted shrink-0">{agent.name}</span>
              )}
              {isDefault && <DefaultBadge />}
              {showsCrewSourceBadge(agent.source) && <CrewSourceBadge source={agent.source} />}
            </div>
            {/* One line here is the point of this view — the row is wide, so a
                single line already carries far more of the sentence than the
                card's clamp does, and the full text is in the tooltip. Same
                fallback chain as the card (see describeCrew). */}
            <span
              className={`block max-w-[380px] truncate text-[11.5px] text-muted ${desc.placeholder ? 'italic' : ''}`}
              title={desc.placeholder ? undefined : desc.text}
            >
              {desc.text}
            </span>
          </div>
        </div>
      </TableCell>
      <TableCell className="text-muted" data-testid="crew-built-from">
        {builtFromError ? <ErrorNotice message={builtFromError} variant="inline" askAgent report={builtFromReport} testId="crew-built-from-error" /> : builtFrom}
      </TableCell>
      <TableCell className="font-mono text-muted">
        {agent.workspace}
        {filesShared && <Badge variant="warn" className="ml-1.5">{sharedNote}</Badge>}
      </TableCell>
      <TableCell className="text-muted">
        {memoryStoreLabel(agent.name, agent.memory_store, memoryStores)}
        {memoryShared && <Badge variant="warn" className="ml-1.5">{sharedNote}</Badge>}
      </TableCell>
      <TableCell className={`font-mono ${agent.model ? 'text-muted' : 'italic text-muted'}`}>
        {agent.model || i18nT('pages.kiroCrewAgentsPage.inherited')}
      </TableCell>
    </TableRow>
  )
}

/** The roster toolbar: the filter and the Cards / List toggle (both hidden on
 *  an empty roster), the change-default link and Add crewmate. */
export function CrewRosterToolbar({ agents, filter, setFilter, view, pickView, openCreate }: {
  agents: KiroCrewAgent[]
  filter: string
  setFilter: (v: string) => void
  view: CrewView
  pickView: (v: CrewView) => void
  openCreate: () => void
}) {
  return (
    <div className="mb-4 flex flex-wrap items-center gap-2">
      {/* No point offering a filter over an empty roster — it just adds a
          control a first-run user has to reason about. */}
      {agents.length > 0 && (
        <SearchInput
          className="w-[240px]"
          placeholder={i18nT('pages.kiroCrewAgentsPage.filter_agents')}
          aria-label={i18nT('pages.kiroCrewAgentsPage.filter_agents')}
          value={filter}
          onChange={e => setFilter(e.target.value)}
        />
      )}
      {/* Same control and the same persistence convention as the Artifacts
          page — NOT the same labels. Artifacts says "Gallery"/"Table"
          because its grid really is a preview gallery; a crew card is not a
          preview, so this reads "Cards"/"List". Hidden on an empty roster
          for the reason the filter is: there is no layout to choose.
          `collapse={false}` because this sits in a `flex-wrap` toolbar whose
          width the control itself contributes to — the responsive
          measurement would be circular and drop it to a dropdown. */}
      {agents.length > 0 && (
        <SegmentedControl<CrewView>
          layoutId="crews-view"
          collapse={false}
          value={view}
          onChange={pickView}
          segments={[
            {
              key: 'cards',
              label: i18nT('pages.kiroCrewAgentsPage.view_cards'),
              icon: <LayoutGrid size={13} />,
              tooltip: i18nT('pages.kiroCrewAgentsPage.view_cards_tooltip'),
            },
            {
              key: 'list',
              label: i18nT('pages.kiroCrewAgentsPage.view_list'),
              icon: <Rows3 size={13} />,
              tooltip: i18nT('pages.kiroCrewAgentsPage.view_list_tooltip'),
            },
          ]}
        />
      )}
      {/* Where the default is changed — a link, shown once there is a second
          crewmate to change to. Sits with the roster's other controls rather
          than on the default card, whose one-line header has no room for a
          verb next to the name. */}
      {/* The link takes the row's slack and wraps its own text, so a long
          translation (Russian) folds inside the link instead of pushing Add
          crewmate onto a second line — the primary action stays put per
          locale. */}
      <div className="flex min-w-0 flex-1 basis-0 items-center">
        {agents.length > 1 && <ChangeDefaultLink />}
      </div>
      <SendBtn onClick={openCreate} data-testid="new-crew" className="shrink-0" {...uiLocation('agents.add')}>
        <Plus className="lucide-inline" aria-hidden="true" />
        {i18nT('pages.kiroCrewAgentsPage.add_crew_member')}
      </SendBtn>
    </div>
  )
}

/** The crewmate list in either view, or the empty state that stands for it. */
export function CrewRosterList({ agents, filtered, view, defaultAgent, sharedKind, memoryStores, openEdit, openCreate }: {
  agents: KiroCrewAgent[]
  /** `config.json`'s `memory_stores`, which says whose a store is. */
  memoryStores: MemoryStores | undefined
  filtered: KiroCrewAgent[]
  view: CrewView
  defaultAgent: string
  sharedKind: (a: KiroCrewAgent) => SharedKind
  openEdit: (a: KiroCrewAgent) => void
  openCreate: () => void
}) {
  return (
    <div role="group" aria-label={i18nT('pages.kiroCrewAgentsPage.agents')} {...uiLocation('agents.crew-list')}>
    {agents.length === 0 ? (
      <div className="flex flex-col items-center">
        <EmptyState
          icon={<Users className="lucide-inline" aria-hidden="true" />}
          title={i18nT('pages.kiroCrewAgentsPage.no_crews_yet')}
          subtitle={i18nT('pages.kiroCrewAgentsPage.create_a_crew_to_give_an_agent_its_own_workspace')}
        />
        {/* The call to action belongs where the explanation is, not only in
            the toolbar above it. */}
        <SendBtn onClick={openCreate} {...uiLocation('agents.create-first')}>{i18nT('pages.kiroCrewAgentsPage.create_your_first_crew')}</SendBtn>
      </div>
    ) : filtered.length === 0 ? (
      <EmptyState
        icon={<Users className="lucide-inline" aria-hidden="true" />}
        title={i18nT('pages.kiroCrewAgentsPage.no_crews_match_your_filter')}
      />
    ) : view === 'list' ? (
      <div className="rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>{i18nT('pages.kiroCrewAgentsPage.crew_column')}</TableHead>
              <TableHead>{i18nT('pages.kiroCrewAgentsPage.built_from')}</TableHead>
              {/* `aria-label` keeps the column's accessible name to the
                  label itself. Without it the InfoTip's own name is
                  concatenated into the header, and a screen reader
                  announces every cell in the column as the label followed
                  by the whole paragraph of tip prose. */}
              <TableHead aria-label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}>
                <span className="inline-flex items-center gap-1.5">
                  {i18nT('pages.kiroCrewAgentsPage.workspace_2')}
                  <InfoTip text={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')} />
                </span>
              </TableHead>
              <TableHead aria-label={i18nT('pages.kiroCrewAgentsPage.memory_store')}>
                <span className="inline-flex items-center gap-1.5">
                  {i18nT('pages.kiroCrewAgentsPage.memory_store')}
                  <InfoTip text={i18nT('pages.kiroCrewAgentsPage.bindings_preview_info')} />
                </span>
              </TableHead>
              <TableHead>{i18nT('pages.kiroCrewAgentsPage.model')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map(a => (
              <CrewRow
                key={a.name}
                agent={a}
                isDefault={a.name === defaultAgent}
                shared={sharedKind(a)}
                memoryStores={memoryStores}
                onOpen={() => openEdit(a)}
              />
            ))}
          </TableBody>
        </Table>
      </div>
    ) : (
      <div className="grid gap-3.5 grid-cols-[repeat(auto-fill,minmax(290px,1fr))]">
        {filtered.map(a => (
          <CrewCard
            key={a.name}
            agent={a}
            isDefault={a.name === defaultAgent}
            shared={sharedKind(a)}
            memoryStores={memoryStores}
            onOpen={() => openEdit(a)}
          />
        ))}
        <Clickable
          onClick={openCreate}
          aria-label={i18nT('pages.kiroCrewAgentsPage.add_crew_member')}
          className="flex min-h-[150px] flex-col items-center justify-center gap-2 rounded-lg border
                     border-dashed border-border-strong text-muted transition-colors focus-ring
                     hover:border-accent hover:bg-accent-subtle hover:text-accent"
        >
          <Plus className="lucide-inline" aria-hidden="true" />
          <span className="text-[13px]">{i18nT('pages.kiroCrewAgentsPage.add_crew_member')}</span>
        </Clickable>
      </div>
    )}
    </div>
  )
}
