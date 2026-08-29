// ItemSessionsTable — L2: the agent sessions that worked one item, and what each
// cost.
//
// A retried item has SEVERAL sessions. The dispatcher retires a dead slot by
// pushing its key aside and creating a new one, so the current session shows only
// the tail of the spend: on the real trail one item read 187 credits from its live
// slot against 4059 across all three. The rows are therefore every slot the item
// has ever had, newest first, with the live one marked, and the totals strip sums
// across them -- that sum is the reason this table exists.
//
// Which numeric columns render is decided by the SERVER, not here. Several usage
// fields are structurally zero today (tokens, cost, and the rows' own turn
// counter), and a table that prints them puts a column of zeros beside a real
// credit total -- which reads as "this work was free" rather than as "this is not
// measured". `populatedColumns` names the ones that carry data.
//
// Type scale matches the dashboard's dense tables (12px values, 11px labels and
// headers). It previously rendered at 10px, which is smaller than any other app in
// the product uses for body text, on the one surface built for reading numbers.
//
// One action per row, and it is a READ: switch to the session and go look at it.
//
// The send-an-instruction box that used to live here is gone, at the maintainer's
// call. It was the only write in a view whose whole claim is to be a window, and
// three reviewers reached it from different directions: the send endpoint CREATES a
// slot it does not recognise rather than refusing, so a slot retiring between the
// liveness check and the delivery landed the instruction in a new empty session (a
// two-request race a client cannot close); the manifest's `permissions.api` declares
// four GET paths and named neither the send nor the slots probe, so the read-only
// boundary was not real; and the "retired" labelling that the gating required was
// itself the source of a false claim on every row. Closing the race needs the
// endpoint to refuse an unknown slot, which is not this app's to change.
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { MessageSquare, Radio } from 'lucide-react'
import { Card, EmptyState } from '../../../../components/ui'
import { i18nT } from '../../../../i18n/t'
import { fmtPercent } from '../../../../i18n/format'
import { api } from '../../../../api/client'
import type { ItemSession } from '../api'
import { EMPTY_PLACEHOLDER, formatCredits, formatDuration, formatRelativeTime } from '../lib/format'

const C = {
  card: 'var(--card)',
  cardHl: 'var(--card-hl)',
  border: 'var(--border)',
  text: 'var(--text)',
  dim: 'var(--text-dim)',
  accent: 'var(--accent)',
} as const

/** Localized header per numeric column, as an EXPLICIT map.
 *
 * Same reason as the step labels: a key built by concatenation cannot be proven to
 * exist, so a missing catalog entry would render the raw dotted key. The column set
 * is the server's, so an unrecognised name falls back to itself rather than being
 * dropped -- a column with data must never render headerless.
 */
const COLUMN_LABEL_KEYS: Record<string, string> = {
  credits: 'apps.autoTriagePipeline.global.col_credits',
  durationMs: 'apps.autoTriagePipeline.global.col_durationMs',
  contextUsed: 'apps.autoTriagePipeline.global.col_contextUsed',
  input: 'apps.autoTriagePipeline.global.col_input',
  output: 'apps.autoTriagePipeline.global.col_output',
  cacheCreate: 'apps.autoTriagePipeline.global.col_cacheCreate',
  cacheRead: 'apps.autoTriagePipeline.global.col_cacheRead',
  cost: 'apps.autoTriagePipeline.global.col_cost',
}

/** Read the slot keys out of whatever shape the slots endpoint returns.
 *
 * Shared by the row's cached liveness check and the send path's re-check, so the
 * two cannot disagree about what "this slot exists" means. Tolerant of an array of
 * strings, an array of records, or a wrapper object, and it never throws on a
 * partial payload -- an unreadable answer yields an EMPTY set, which disables
 * sending rather than enabling it against a list nobody could parse.
 */
export function slotKeysOf(raw: unknown): Set<string> {
  const keys = new Set<string>()
  const wrapped = (raw as { slots?: unknown } | null)?.slots
  const rows: unknown[] = Array.isArray(raw) ? raw : Array.isArray(wrapped) ? wrapped : []
  for (const row of rows) {
    if (typeof row === 'string') {
      if (row) keys.add(row)
      continue
    }
    const record = row as { key?: unknown; slot?: unknown; name?: unknown } | null
    const key = record?.key ?? record?.slot ?? record?.name
    if (typeof key === 'string' && key) keys.add(key)
  }
  return keys
}

export function columnLabel(column: string): string {
  const key = COLUMN_LABEL_KEYS[column]
  if (!key) return column
  const translated = i18nT(key)
  return translated === key ? column : translated
}

/** Render one numeric cell in the unit its column actually carries.
 *
 * Credits and durations are not plain numbers -- printing 4059.6534 raw, or a
 * duration in milliseconds, makes a table nobody can scan.
 */
export function cellText(column: string, session: ItemSession): string {
  switch (column) {
    case 'credits':
      return formatCredits(session.credits)
    case 'durationMs':
      return formatDuration(session.durationMs)
    case 'contextUsed':
      // fmtPercent takes the RATIO and applies ×100 itself, so the guard against
      // a zero window (divide-by-zero) stays. The unit and the digits are then
      // the locale's — `46%` in en, `46 %` in de — instead of a welded-on `%`.
      return session.contextWindow > 0
        ? fmtPercent(session.contextUsed / session.contextWindow)
        : String(session.contextUsed)
    case 'input':
      return String(session.input)
    case 'output':
      return String(session.output)
    case 'cacheCreate':
      return String(session.cacheCreate)
    case 'cacheRead':
      return String(session.cacheRead)
    case 'cost':
      return String(session.cost)
    default:
      return ''
  }
}

function SessionRow({
  session,
  columns,
  nowMs,
  liveness,
}: {
  session: ItemSession
  columns: string[]
  nowMs: number
  liveness: { state: 'loading' | 'ready' | 'error'; slots: Set<string> }
}) {
  // Three states, not two. Treating "we do not know yet" as "retired" made every
  // row -- including the one marked Current -- print "This session has been
  // retired" on every first expansion, and print it FOREVER when the liveness
  // probe failed. That is the view asserting a false fact, which is the mode the
  // error panels in this feature exist to prevent; only a resolved answer that
  // omits the slot means retired.
  const live = liveness.state === 'ready' && liveness.slots.has(session.slot)
  const retired = liveness.state === 'ready' && !live
  return (
    <div
      className="rounded-lg border p-2.5"
      style={{ background: session.current ? C.cardHl : C.card, borderColor: C.border }}
      data-testid={`atp-session-${session.slot}`}
    >
      <div className="flex flex-wrap items-center gap-2">
        {session.current ? (
          <Radio
            aria-label={i18nT('apps.autoTriagePipeline.global.session_current')}
            className="h-3 w-3 shrink-0"
            style={{ color: C.accent }}
          />
        ) : (
          <span className="w-3 shrink-0" aria-hidden="true" />
        )}

        {/* The KEY is the link -- an operator reading a row is already looking at
            the session's name, so that is where the affordance belongs, and a real
            anchor can be middle-clicked, copied and opened in a new tab.

            `?sid=` is the chat page's own deep-link parameter. It resolves ONLY
            against the slots the gateway currently lists, so this cannot conjure a
            phantom slot: an unknown key lands on the page's "session not found"
            notice rather than creating an empty conversation under a retired
            identity. That guard is what lets the key be a plain link with no
            dispatch of our own. */}
        {live ? (
          <Link
            to={`/chat?sid=${encodeURIComponent(session.slot)}`}
            className="shrink-0 font-mono text-[12px] hover:underline"
            style={{ color: C.accent, minWidth: '10rem' }}
          >
            {session.slot}
          </Link>
        ) : (
          <span
            className="shrink-0 font-mono text-[12px]"
            style={{ color: C.text, minWidth: '10rem' }}
          >
            {session.slot}
          </span>
        )}

        <span className="shrink-0 text-[12px]" style={{ color: C.dim, minWidth: '7rem' }}>
          {session.model || EMPTY_PLACEHOLDER}
        </span>

        <span
          className="shrink-0 text-[12px] tabular-nums"
          style={{ color: C.text, minWidth: '4.5rem' }}
        >
          {i18nT('apps.autoTriagePipeline.global.turns_n', { count: session.turns })}
        </span>

        {columns.map((column) => (
          <span
            key={column}
            className="flex shrink-0 items-baseline gap-1 text-[12px] tabular-nums"
            style={{ color: C.text, minWidth: '4.5rem' }}
          >
            {/* The label rides WITH the value below `sm`, where the header row is
                hidden because it would align with nothing. Hiding the header alone
                left "187 · 3m 4s · 46%" with nothing saying which was credits,
                duration or context -- recreating, on narrow, the unlabeled number
                strip the header row was added to remove. */}
            <span className="text-[11px] sm:hidden" style={{ color: C.dim }}>
              {columnLabel(column)}
            </span>
            {cellText(column, session)}
          </span>
        ))}

        <span className="shrink-0 text-[11px]" style={{ color: C.dim }}>
          {session.startedAt === null
            ? EMPTY_PLACEHOLDER
            : formatRelativeTime(session.startedAt, nowMs)}
        </span>

        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {/* No action button: the key above IS the link. What remains here is the
              REASON a row has no link, which only the non-live states need.

              A live row says nothing -- its link is self-evident. A retired one has
              no transcript the chat page can resolve, and the two unresolved states
              (probe loading, probe failed) must not borrow the retired wording:
              saying "retired" while the answer is merely unknown asserts something
              we have not established. */}
          {live ? null : retired ? (
            <span className="text-[11px]" style={{ color: C.dim }}>
              {i18nT('apps.autoTriagePipeline.global.session_retired')}
            </span>
          ) : liveness.state === 'error' ? (
            <span className="text-[11px]" style={{ color: 'var(--warn)' }}>
              {i18nT('apps.autoTriagePipeline.global.liveness_unknown')}
            </span>
          ) : (
            <span className="text-[11px]" style={{ color: C.dim }}>
              {i18nT('apps.autoTriagePipeline.global.liveness_checking')}
            </span>
          )}
        </span>
      </div>
    </div>
  )
}

/** One figure in the totals strip, label above value. */
function Total({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[11px] uppercase tracking-wide" style={{ color: C.dim }}>
        {label}
      </span>
      <span className="text-[13px] font-semibold tabular-nums" style={{ color: C.text }}>
        {value}
      </span>
    </div>
  )
}

export default function ItemSessionsTable({
  sessions,
  populatedColumns,
  nowMs,
}: {
  sessions: ItemSession[]
  populatedColumns: string[]
  nowMs: number
}) {
  // Which of these slots the gateway still knows. A retired slot is not offered as
  // a link: the chat page resolves `?sid=` only against slots it currently lists, so
  // a link to one would dead-end on "Session not found" after its resolve timeout.
  // Withholding the link and saying why is the honest form of the same answer.
  //
  // The result is carried as a THREE-state value. An empty set is not "everything
  // is retired": it is also what loading and failure look like, and collapsing
  // those together made the table state a falsehood about live sessions. The link
  // appears only once the answer resolved AND named the slot -- unknown never
  // renders a link, but it is no longer reported as a retirement either.
  const liveQuery = useQuery({
    queryKey: ['atp', 'live-slots'],
    queryFn: () => api.chatSlots().then(r => (Array.isArray(r) ? r : r.slots)),
    staleTime: 15_000,
  })
  const liveness = useMemo(
    () => ({
      state: liveQuery.isError
        ? ('error' as const)
        : liveQuery.isSuccess
          ? ('ready' as const)
          : ('loading' as const),
      slots: slotKeysOf(liveQuery.data),
    }),
    [liveQuery.isError, liveQuery.isSuccess, liveQuery.data],
  )

  if (sessions.length === 0) {
    return (
      <Card className="p-4">
        <EmptyState
          icon={<MessageSquare aria-hidden="true" className="h-5 w-5" />}
          title={i18nT('apps.autoTriagePipeline.global.sessions_empty_title')}
          subtitle={i18nT('apps.autoTriagePipeline.global.sessions_empty_subtitle')}
          testId="atp-sessions-empty"
        />
      </Card>
    )
  }

  const totalCredits = sessions.reduce((sum, s) => sum + s.credits, 0)
  const totalTurns = sessions.reduce((sum, s) => sum + s.turns, 0)
  const totalDurationMs = sessions.reduce((sum, s) => sum + s.durationMs, 0)
  // Earliest start across every slot, INCLUDING the retired ones. The live slot's
  // own start is when the last retry began, not when work on this item began.
  const firstStart = sessions.reduce<number | null>(
    (min, s) => (s.startedAt === null ? min : min === null ? s.startedAt : Math.min(min, s.startedAt)),
    null,
  )

  return (
    <div className="flex flex-col gap-2">
      {/* Summed across retries, and labelled as such: the live slot alone reads a
          fraction of the real spend, so an unlabelled figure here would be read as
          "what this item cost" while naming only its most recent attempt. */}
      <div
        className="flex flex-wrap items-start gap-x-6 gap-y-2 rounded-lg border px-3 py-2"
        style={{ background: C.cardHl, borderColor: C.border }}
        data-testid="atp-sessions-total"
      >
        <Total
          label={i18nT('apps.autoTriagePipeline.global.total_sessions')}
          value={String(sessions.length)}
        />
        <Total
          label={i18nT('apps.autoTriagePipeline.global.col_turns')}
          value={String(totalTurns)}
        />
        <Total
          label={i18nT('apps.autoTriagePipeline.global.col_credits')}
          value={formatCredits(totalCredits)}
        />
        <Total
          label={i18nT('apps.autoTriagePipeline.global.col_durationMs')}
          value={formatDuration(totalDurationMs)}
        />
        <Total
          label={i18nT('apps.autoTriagePipeline.global.first_started')}
          value={firstStart === null ? EMPTY_PLACEHOLDER : formatRelativeTime(firstStart, nowMs)}
        />
      </div>
      {/* VISIBLE column headers. These were a `title` attribute only, which is
          hover-only: it reaches neither touch nor keyboard, on the one surface built
          for reading cost. Hidden on the narrow layout, where the rows wrap and a
          header row would not align with anything. */}
      {populatedColumns.length > 0 ? (
        <div
          className="hidden items-center gap-2 px-2.5 text-[11px] uppercase tracking-wide sm:flex"
          style={{ color: C.dim }}
          data-testid="atp-session-headers"
        >
          <span className="w-3 shrink-0" aria-hidden="true" />
          <span className="shrink-0" style={{ minWidth: '10rem' }}>
            {i18nT('apps.autoTriagePipeline.global.col_slot')}
          </span>
          <span className="shrink-0" style={{ minWidth: '7rem' }}>
            {i18nT('apps.autoTriagePipeline.global.col_model')}
          </span>
          <span className="shrink-0" style={{ minWidth: '4.5rem' }}>
            {i18nT('apps.autoTriagePipeline.global.col_turns')}
          </span>
          {populatedColumns.map((column) => (
            <span key={column} className="shrink-0" style={{ minWidth: '4.5rem' }}>
              {columnLabel(column)}
            </span>
          ))}
        </div>
      ) : null}
      {sessions.map((session) => (
        <SessionRow
          key={session.slot}
          session={session}
          columns={populatedColumns}
          nowMs={nowMs}
          liveness={liveness}
        />
      ))}
    </div>
  )
}
