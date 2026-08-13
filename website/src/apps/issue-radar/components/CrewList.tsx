// Issue Radar — column 2 of the crews surface: the crew roster.
//
// Layout follows the issue and PR list columns: one rounded card per row in a
// scrolling stack under a `CREW · N` group label. The filter and sort controls
// are in the rail's Crews accordion, not here.
//
// The group-label row also carries the ONE way to hire a crew. It sits with the
// roster rather than in the main column because a crew IS a roster entry, and
// because the main column addresses a single crew — a create control there would
// disappear the moment one was selected.
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Plus, Users } from 'lucide-react'
import { useIssueRadar } from '../context'
import type { Crew } from '../api'
import { compareText, fmtNumber } from '../../../i18n/format'
import { Btn, EmptyState } from '../../../components/ui'
import Clickable from '../../../components/Clickable'
import CrewGhost from './CrewGhost'
import ListSkeleton from './ListSkeleton'

/** The three statuses `GET /crews` derives per crew, mirroring `_crew_status` in
 * `backend/crew_routes.py`. A dot can only be one colour, so the backend picks
 * one by what it says about the crew: paused (doing nothing regardless of what it
 * holds) → working → idle (on duty, nothing in flight). */
const CREW_STATUSES = ['paused', 'working', 'idle'] as const
type CrewStatus = (typeof CREW_STATUSES)[number]

/**
 * Read the route-derived `status` field off a crew record.
 *
 * `Crew` in `api.ts` mirrors the STORE record, and `status` is computed by the
 * crews ROUTE from each crew's open work items (`_crews_page`) — it exists so the
 * phase taxonomy stays in one language instead of `PARKED_PHASES` being re-encoded
 * in TypeScript. Read structurally, and validated against the list above, because
 * `api.ts` is owned elsewhere in this change: the moment `Crew` declares
 * `status: CrewStatus` this collapses to `c.status`. Unknown/absent → `idle`,
 * which renders a neutral dot rather than claiming work that may not exist.
 */
function crewStatus(c: Crew): CrewStatus {
  const raw = (c as { status?: unknown }).status
  return (CREW_STATUSES as readonly unknown[]).includes(raw) ? (raw as CrewStatus) : 'idle'
}

/** Dot colour per status — a traffic light, so the roster is readable without
 * reading any word: green is making progress, yellow is on duty with nothing in
 * flight, grey is switched off.
 *
 * `--ok` / `--warn` rather than the accent, because the accent is the theme's
 * brand colour and changes per theme: a dot that means "healthy" cannot be the
 * same hue as a selected border. */
const DOT_CLASS: Record<CrewStatus, string> = {
  working: 'bg-ok',
  idle: 'bg-warn',
  paused: 'bg-muted-strong',
}

/** Status word per status, as FULL literal catalog keys.
 *
 * Not `t(`…status_${status}`)`: a key assembled from parts exists nowhere in the
 * source, so the extractor cannot see it and the dangling-reference gate cannot
 * verify it — it simply renders as the raw key the day it goes missing. Same
 * pattern as `STATUS_LABEL_KEY` in `pages/chat/McpToolsPanel.tsx`. */
const STATUS_KEY: Record<CrewStatus, string> = {
  working: 'apps.issueRadar.views.crews.status_working',
  paused: 'apps.issueRadar.views.crews.status_paused',
  idle: 'apps.issueRadar.views.crews.status_idle',
}

/** Rank for the `status` sort, ascending = most active first.
 *
 * Mirrors the priority `_crew_status` resolves ties with, but ordered for a
 * READER rather than for a dot's colour: a crew with work in flight is the row
 * worth reading, and paused sinks to the bottom because it is doing nothing by
 * your own instruction. */
const STATUS_RANK: Record<CrewStatus, number> = {
  working: 0,
  idle: 1,
  paused: 2,
}

/** True when a crew is switched off but not retired — the backend's own `paused`
 * flag (`_crew_flags`), re-derived here rather than read from `status` because
 * `status` picks ONE label: a paused crew is filtered on this predicate, so the
 * Paused chip's rows always match its count. */
const isPaused = (c: Crew) => c.enabled === false && !c.retired_at

export interface CrewListProps {
  /** Raise the create-crew dialog, which lives in `Workspace` (both this button
   *  and the crew page's Edit open the same form, so one owner means one dialog).
   *
   *  REQUIRED: this button is the only way to hire a crew, so a caller that
   *  forgets it must be a type error rather than a column that silently cannot
   *  create anything. */
  onCreate: () => void
}

export default function CrewList({ onCreate }: CrewListProps) {
  const { t } = useTranslation()
  const {
    crews, crewsLoading, crewsError,
    crewView, setCrewView, crewFilter, crewSortKey, crewSortDir, listDetail,
  } = useIssueRadar()

  /** The rows the active filter shows.
   *
   * `paused` uses the backend's own flag (see `isPaused`), so those rows and that
   * filter's count always agree. `working` falls back to the single-valued
   * `status`, which differs from the flags in exactly one case: a PAUSED crew that
   * also has work in flight is shown under Paused only, while the working count
   * still includes it. Closing that gap needs the per-crew booleans in the
   * payload, not a different client-side rule — re-deriving them here would mean
   * re-encoding the phase taxonomy the `status` field exists to keep server-side. */
  const shown = useMemo(() => {
    const filtered = crews.filter((c) => {
      if (crewFilter === 'all') return true
      if (crewFilter === 'paused') return isPaused(c)
      return crewStatus(c) === crewFilter
    })
    const dir = crewSortDir === 'asc' ? 1 : -1
    // Name is the tiebreak on every field, so a poll that returns the roster in a
    // different order cannot reshuffle equal rows under the reader's cursor.
    // `compareText` collates in the APP's language; `localeCompare` would use the
    // host's and ignore the language the user picked.
    const byName = (a: Crew, b: Crew) => compareText(a.name, b.name)
    return [...filtered].sort((a, b) => {
      if (crewSortKey === 'name') return dir * byName(a, b)
      if (crewSortKey === 'created') {
        const d = Date.parse(a.created_at) - Date.parse(b.created_at)
        return d !== 0 ? dir * d : byName(a, b)
      }
      const r = STATUS_RANK[crewStatus(a)] - STATUS_RANK[crewStatus(b)]
      return r !== 0 ? dir * r : byName(a, b)
    })
  }, [crews, crewFilter, crewSortKey, crewSortDir])

  /** Card contract copied from the issue and PR list columns, so all three
   * columns read as one component: rounded, bordered, `bg-card`, accent border
   * when selected. */
  const cardClass = (isSel: boolean) =>
    `w-full text-left rounded-lg border p-2.5 cursor-pointer bg-card hover:bg-bg-hover transition-colors ${
      isSel ? 'border-accent' : 'border-border'
    }`

  return (
    // No right border, matching the issue and PR list columns: in this app the
    // ResizeHandle is the seam between columns, and a border here would make the
    // roster the only one of the three with a hard edge.
    <section className="flex flex-col min-h-0 h-full">
      {/* The filter and sort controls for this column live in the rail's Crews
          accordion, exactly as the issue and PR columns take theirs from theirs —
          this column is the roster and nothing else. */}
      <div
        className="flex-1 min-h-0 overflow-y-auto scrollbar-none px-4 pt-2 pb-2 flex flex-col gap-2"
        style={{ scrollbarWidth: 'none' }}
      >
        {/* Group label — the roster's size AS SHOWN, so a filter that hides rows
            is visible in the count rather than contradicting it — and, on the same
            line, the create control. No band or rule of its own now that each row
            is a card. */}
        <div className="flex items-center justify-between gap-2 px-1 pt-1 flex-shrink-0">
          <span className="text-[11px] uppercase tracking-[.06em] text-muted">
            {t('apps.issueRadar.views.crews.group_crew')} · {fmtNumber(shown.length)}
          </span>
          {/* Deliberately NOT `primary`: reading what the crews are doing is this
              surface's primary act, and an accent button on a navigation column
              would pull the eye off the roster it sits above. */}
          <Btn onClick={onCreate} className="flex-shrink-0" data-testid="crew-create">
            <Plus className="lucide-inline" />
            {t('apps.issueRadar.views.crews.new_crew')}
          </Btn>
        </div>

        {crewsError && (
          <div className="px-1 text-[13px] text-danger">{crewsError.message}</div>
        )}

        {crewsLoading && <ListSkeleton count={3} />}

        {!crewsLoading && !crewsError && shown.length === 0 && (
          <EmptyState
            icon={<Users size={34} strokeWidth={1.5} />}
            title={crews.length === 0
              ? t('apps.issueRadar.views.crews.empty_title')
              : t('apps.issueRadar.views.crews.filtered_empty_title')}
            subtitle={crews.length === 0
              ? t('apps.issueRadar.views.crews.empty_sub')
              : t('apps.issueRadar.views.crews.filtered_empty_sub')}
            testId="crew-list-empty"
          />
        )}

        {shown.map((c) => (
          <CrewRow
            key={c.id}
            crew={c}
            selected={crewView.kind === 'crew' && crewView.id === c.id}
            onSelect={() => { setCrewView({ kind: 'crew', id: c.id }); listDetail.openDetail() }}
            cardClass={cardClass}
          />
        ))}
      </div>
    </section>
  )
}

/** One roster card: the crew's face, a status dot, its name, the status word, and
 * a one-line summary of what it is doing. */
function CrewRow({ crew, selected, onSelect, cardClass }: {
  crew: Crew
  selected: boolean
  onSelect: () => void
  /** The column's shared card style, so one row cannot drift from another's. */
  cardClass: (isSel: boolean) => string
}) {
  const { t } = useTranslation()
  const status = crewStatus(crew)
  const statusWord = t(STATUS_KEY[status])

  /** The row's second line.
   *
   * `GET /crews` answers with crew RECORDS plus repo-wide tallies — it carries no
   * work items, so the newest item's issue number and phase are not available to
   * this list without one request per crew. The line therefore says the most
   * specific true thing the record supports: why it is paused, or what it is
   * scoped to. Once the payload carries a per-crew summary of the newest open
   * item, this is the one function to change.
   */
  const summary = status === 'paused'
    ? (crew.paused_reason || t('apps.issueRadar.views.crews.sub_paused'))
    : crew.labels.length > 0
      ? crew.labels.join(' · ')
      : t('apps.issueRadar.views.crews.sub_any_issue')

  return (
    <Clickable
      onClick={onSelect}
      aria-current={selected ? 'page' : undefined}
      title={crew.name}
      data-testid={`crew-row-${crew.id}`}
      className={`${cardClass(selected)} flex items-center gap-2.5 flex-shrink-0`}
    >
      {/* The ghost sprite is near-white, so on a light theme it disappears into
          the card. The tinted rounded tile is what makes it legible there; it is
          a theme token rather than a fixed grey so a dark theme keeps the same
          gentle lift instead of a bright patch. */}
      <span className="w-[38px] h-[38px] rounded-lg bg-bg-accent border border-border flex items-center justify-center flex-shrink-0 overflow-hidden">
        {/* Decorative: the crew's name is rendered as text beside it, so the
            avatar adds no information a reader would otherwise miss. */}
        <CrewGhost seed={crew.avatar_seed} variant={crew.avatar_variant} size={34} />
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="text-[13px] font-semibold text-text-strong truncate">{crew.name}</span>
          <span className="text-[11px] text-muted flex-shrink-0">{statusWord}</span>
        </div>
        {/* One line, clipped: a row is a summary, and a wrapping reason would
            reflow the whole roster as phases change under a poll. */}
        <div className="text-[12px] text-muted mt-0.5 leading-snug truncate">{summary}</div>
      </div>
      {/* Far right, vertically centred: the dots line up in a single column down
          the roster, so "which crew is doing something" is one glance rather than
          a scan through names of differing length. `title` carries the same word
          the row already shows, for a pointer that lands on the dot itself. */}
      <span
        className={`w-[9px] h-[9px] rounded-full flex-shrink-0 ${DOT_CLASS[status]}`}
        title={statusWord}
        data-testid={`crew-row-dot-${crew.id}`}
      />
    </Clickable>
  )
}
