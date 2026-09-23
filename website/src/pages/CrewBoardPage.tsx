/**
 * CrewBoardPage — the conductor's work items, as a board a human can scan.
 *
 * Implements Phase 4 ("the surfaces", Crew page half) of
 * `docs/request-for-change/rfc-conductor-work-ledger.md`. It reads the masked
 * projection at `GET /api/crew-board`, never the conductor's own MCP route: no
 * row here carries `worker_session_key`, and the text of a `bind` event arrives
 * blanked, so there is nothing on this page that could address a worker session.
 *
 * ## Why the bands, and why this order
 *
 * A conductor's board is read for ONE reason: to find the item that cannot move
 * without a human. So items waiting on a decision are lifted out of document order
 * into a band at the top — if finding a blocked worker needs a scroll, the board
 * has failed at the only job it has. Everything still open follows. Terminal items
 * collapse behind an expander, because a finished item is evidence rather than
 * work and a board that grows forever stops being scannable.
 *
 * ## Zero model turns
 *
 * The browser polls every 10 s. No agent wakes to render this, no turn is spent,
 * and nothing is pushed: the RFC's push half is PR A (the wake hook), which lands
 * separately. `channels_available` arrives `false` until the RFC's Phase 5 ships
 * the channel records, so that band turns on server-side with no edit here.
 *
 * ## Status badges render the store's own tokens
 *
 * `status`, `state`, `verdict` and `alive` are shown verbatim as the store spells
 * them. They are a technical vocabulary shared by the MCP tools, the event log and
 * this page, and a translated synonym would mean the page and the tool a conductor
 * just ran disagree about what an item is. Only prose is localized.
 */

import { useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, Inbox } from 'lucide-react'

import { Badge, Btn, Card, CardTitle, EmptyState, PageHeader } from '../components/ui'
import InfoTip from '../components/InfoTip'
import ErrorNotice from '../components/ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { fmtRelative } from '../i18n/format'
import { isNotFoundError } from '../api/apiError'
import {
  CREW_BOARD_POLL_MS,
  crewBoardQueryKey,
  type CrewBoardAction,
  type WorkBoardItem,
  type WorkBoardResponse,
} from '../api/crewBoard'
import { artifactEntries, partitionBoardRows, rowKindLabelKey } from './crewBoardRows'

/** Theme tokens only. A literal hex or a palette class fails `no-raw-colors`,
 *  and more to the point a fixed palette would ignore the user's theme. */
const C = {
  text: 'var(--text)',
  dim: 'var(--text-dim)',
  border: 'var(--border)',
  cardHl: 'var(--card-hl)',
  accent: 'var(--accent)',
  warn: 'var(--warn)',
} as const

/** The alive dot. Filled while running, hollow once idle, faint when closed —
 *  three states a scan can tell apart without reading the word beside it. */
function AliveDot({ alive }: { alive: WorkBoardItem['alive'] }) {
  const style =
    alive === 'running'
      ? { background: C.accent, borderColor: C.accent }
      : alive === 'idle'
        ? { background: 'transparent', borderColor: C.text }
        : { background: 'transparent', borderColor: C.dim }
  return (
    <span
      aria-hidden
      className="mt-[6px] size-[7px] shrink-0 rounded-full border"
      style={style}
    />
  )
}

/** One small token chip. No background and no box — a hairline and the word.
 *  Raymond rejects visible chrome on dense rows; the weight belongs on the text.
 *
 *  `role` names WHOSE claim the chip carries, dimmer than the value itself. Two
 *  adjacent chips reading `done` and `review` are a worker's report and a
 *  conductor's state, which can legitimately disagree; without the prefix a
 *  reader sees a contradiction and no cue for which party said which. */
function StatusBadge({
  children,
  role,
  tone,
}: {
  children: React.ReactNode
  role?: string
  tone?: 'warn'
}) {
  return (
    // The shared Badge, not a bespoke span: it owns the status-label tokens for
    // every surface, so a theme change reaches this board without an edit here.
    // `className` narrows only the metrics, because a row here holds up to four
    // badges and the default pill size turns a scanning surface into a stack.
    <Badge
      variant={tone === 'warn' ? 'warn' : 'muted'}
      className="shrink-0 px-1.5 py-px text-[11px] leading-[14px] tabular-nums"
    >
      {role ? <span className="opacity-60">{role} </span> : null}
      {children}
    </Badge>
  )
}

/** A band heading: the label, then the count, then a hairline across the rest.
 *  The rule carries the eye without drawing a container around the rows. */
function BandHeading({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center gap-2 pb-1 pt-3">
      <span className="text-[11px] uppercase tracking-wide" style={{ color: C.dim }}>
        {label}
      </span>
      <span className="text-[11px] tabular-nums" style={{ color: C.dim }}>
        {count}
      </span>
      <span className="h-px flex-1" style={{ background: C.border }} />
    </div>
  )
}

/** A rejection's HTTP status, or 0 when it carries none. Duck-typed for the same
 *  reason `isNotFoundError` is: `instanceof` fails across transports and mocks. */
function httpStatus(err: unknown): number {
  if (typeof err === 'object' && err !== null) {
    const status = (err as { status?: unknown }).status
    if (typeof status === 'number') return status
  }
  return 0
}

/**
 * The take-over and stop affordances Phase 4 names, on an orphaned row only.
 *
 * Both are keyed by `item_id`, never by a session: the masked read gives the page
 * no worker key, so the server resolves one from the store and never returns it.
 * That is the whole reason an action route exists rather than the page reusing the
 * ordinary Stop button.
 *
 * Take-over renders DISABLED on main. Nothing on the gateway performs one —
 * `session_control` has no re-own verb and `CONDUCTOR_ACTIONS` has no transfer —
 * so the button states why instead of being wired to something invented here. The
 * reason comes from the server as a CODE which this maps to a translated string,
 * so the page and the route cannot drift into disagreeing about what is possible.
 */
function RowActions({
  item,
  conductor,
  takeOverAvailable,
}: {
  item: WorkBoardItem
  conductor: string
  takeOverAvailable: boolean
}) {
  const queryClient = useQueryClient()
  // Two parts, because `ErrorNotice` treats them differently: `message` is the
  // error journal's lookup key that recovers the route, endpoint, HTTP status and
  // backend code for the agent hand-off, so the backend's own sentence belongs
  // there and the translated lead belongs in `title`. Collapsing them into one
  // string loses whichever of the two is not kept.
  const [failure, setFailure] = useState<{ title?: string; message: string } | null>(null)

  const act = useMutation({
    mutationFn: (action: CrewBoardAction) => api.crewBoardAction(conductor, item.item_id, action),
    onSuccess: (result) => {
      // HTTP 200 does not mean the worker stopped. ``stop_slot_turn`` answers 200
      // with ``ok: false`` when it cannot reach the worker's session, so clearing
      // the failure on any 200 reports a worker that is still running as stopped --
      // the one reading that matters here, because the reason to press Stop is that
      // the worker should not keep going.
      //
      // The board is re-read either way: on a failure it is what shows the row
      // still alive, which is the evidence for the message.
      setFailure(result.ok ? null : { message: i18nT('pages.crewBoard.stop_not_confirmed') })
      void queryClient.invalidateQueries({ queryKey: crewBoardQueryKey(conductor) })
    },
    onError: (err) => {
      // 409 is the one failure worth wording differently: it means the board this
      // click was made from is stale, not that the action is broken. Re-reading is
      // the remedy, so it fires one.
      //
      // Read off `status` rather than `instanceof ApiError`, for the reason
      // `isNotFoundError` documents next door: an ApiError built by a different
      // transport, or a mocked client, is still a 409 and must still be read as one.
      const stale = httpStatus(err) === 409
      const lead = i18nT(
        stale ? 'pages.crewBoard.action_stale_view' : 'pages.crewBoard.action_failed',
      )
      // The backend's own sentence is kept as the message and the translated line
      // becomes the lead. A request that failed has a journal entry keyed by that
      // exact sentence; substituting the translated one misses the lookup, and the
      // agent hand-off then opens a chat with no endpoint, status or backend code
      // attached -- the one thing this surface exists to offer. When the transport
      // gives no sentence at all there is nothing to look up, so the translated
      // line is the message instead of a lead over an empty notice.
      const detail = errMessage(err)
      setFailure(detail ? { title: lead, message: detail } : { message: lead })
      if (stale) void queryClient.invalidateQueries({ queryKey: crewBoardQueryKey(conductor) })
    },
  })

  const workerGone = item.alive === 'closed'
  const stopDisabled = workerGone || act.isPending
  const stopReason = workerGone ? i18nT('pages.crewBoard.stop_unavailable_closed') : ''

  // Shown as TEXT, not only as a `title`. A tooltip on a disabled button is not
  // reachable by keyboard and is unreliably surfaced by browsers, so a reason that
  // lives only there is a reason nobody reads.
  //
  // One line, two jobs. When Stop cannot run it carries the reason; when it CAN it
  // carries the consequence. A red button named for a final-sounding verb with no
  // stated outcome does not get clicked: the reader cannot tell what it throws
  // away, so they leave the orphaned worker running, which is the one thing this
  // board exists to prevent.
  //
  // Only the STOP hint appears here, because only it is a property of this row --
  // whether this item's own worker session is still open. Take-over renders NO
  // control while it is unavailable: a button that can never work is noise on every
  // row of a board whose whole job is scanning.
  const hint = stopReason || i18nT('pages.crewBoard.stop_consequence')

  return (
    <div className="ml-[15px] mt-1 flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <Btn
          danger
          type="button"
          disabled={stopDisabled}
          title={stopReason || undefined}
          onClick={() => act.mutate('stop')}
          className="px-1.5 py-0.5 text-[11px]"
        >
          {i18nT('pages.crewBoard.action_stop')}
        </Btn>

        {takeOverAvailable ? (
          <Btn
            type="button"
            disabled={act.isPending}
            onClick={() => act.mutate('take_over')}
            className="px-1.5 py-0.5 text-[11px]"
          >
            {i18nT('pages.crewBoard.action_take_over')}
          </Btn>
        ) : null}
      </div>

      {/* The notice is a sibling of the button row, not a member of it: it carries
          its own two controls (ask, dismiss), and inside the row they would sit
          beside Stop as a third and fourth button in one horizontal group. */}
      {failure ? (
        <ErrorNotice
          title={failure.title}
          message={failure.message}
          onDismiss={() => setFailure(null)}
          askAgent
          variant="inline"
        />
      ) : null}

      <span className="text-[11px]" style={{ color: C.dim }}>
        {hint}
      </span>
    </div>
  )
}

/** One item row: two lines. Identity and state above, the worker's own account
 *  below. The kind label is right-aligned so the right edge reads as a column. */
function ItemRow({
  item,
  conductor,
  takeOverAvailable,
}: {
  item: WorkBoardItem
  conductor: string
  takeOverAvailable: boolean
}) {
  const [open, setOpen] = useState(false)
  const kindKey = rowKindLabelKey(item)
  const artifacts = artifactEntries(item)

  return (
    <div className="border-b py-2" style={{ borderColor: C.border }}>
      {/* Narrow-first: one column on a phone, where the two metadata rails sit
          under the title instead of squeezing it. `md:` restores the desktop
          row, and the pinned kind-label width applies only there. */}
      <div className="flex flex-col gap-1 md:flex-row md:items-start md:gap-2">
        <div className="flex min-w-0 flex-1 items-start gap-2">
          <AliveDot alive={item.alive} />

          <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="text-[12px] font-medium" style={{ color: C.text }}>
              {item.title || item.item_id}
            </span>

            {/* status (worker) and state (conductor) stay TWO chips. They are
                different parties' claims about the same item and can legitimately
                disagree — a worker reporting `done` on an item the conductor has
                not accepted is the normal case, and one merged chip would have to
                pick a side and would hide exactly that. */}
            {item.status ? (
              <StatusBadge role={i18nT('pages.crewBoard.role_worker')}>{item.status}</StatusBadge>
            ) : (
              <StatusBadge role={i18nT('pages.crewBoard.role_worker')}>
                {i18nT('pages.crewBoard.no_report')}
              </StatusBadge>
            )}
            <StatusBadge role={i18nT('pages.crewBoard.role_conductor')}>{item.state}</StatusBadge>
            {item.verdict ? (
              <StatusBadge role={i18nT('pages.crewBoard.role_conductor')}>{item.verdict}</StatusBadge>
            ) : null}
            {item.stale ? <StatusBadge tone="warn">{i18nT('pages.crewBoard.kind_stale')}</StatusBadge> : null}
            {!item.acceptance_concrete && !item.terminal ? (
              <StatusBadge tone="warn">{i18nT('pages.crewBoard.bar_vague')}</StatusBadge>
            ) : null}
            </div>
          </div>
        </div>

        <div className="ml-[15px] flex items-baseline gap-2 md:ml-0 md:shrink-0">
          <span className="text-[11px] tabular-nums" style={{ color: C.dim }}>
            {fmtRelative(item.last_report_at ?? item.created_at)}
          </span>

          <span className="text-[11px] md:w-[5.5rem] md:text-right" style={{ color: C.dim }}>
            {kindKey ? i18nT(kindKey) : item.state}
          </span>
        </div>
      </div>

      {/* The worker's own account, in place of the session ledger's `next` —
          this store has no equivalent field, so `summary` is the closest true
          thing and is labelled as the worker's words, not as a plan. */}
      {item.summary ? (
        <div className="ml-[15px] mt-1 text-[12px]" style={{ color: C.dim }}>
          {item.summary}
        </div>
      ) : null}

      {item.decision ? (
        <div className="ml-[15px] mt-1 text-[12px]" style={{ color: C.text }}>
          <span className="mr-1 text-[11px]" style={{ color: C.dim }}>
            {i18nT('pages.crewBoard.decision')}
          </span>
          {item.decision}
        </div>
      ) : null}

      {item.orphaned ? (
        <div className="ml-[15px] mt-1 text-[12px]" style={{ color: C.warn }}>
          <AlertTriangle size={12} className="mr-1 inline align-[-2px]" />
          {i18nT('pages.crewBoard.orphaned_note')}
        </div>
      ) : null}

      {/* Only an orphaned item gets them, which is also the only state the action
          route accepts — so the page cannot offer a click the server will refuse. */}
      {item.orphaned ? (
        <RowActions item={item} conductor={conductor} takeOverAvailable={takeOverAvailable} />
      ) : null}

      <div className="ml-[15px] mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
        {item.pr !== null ? (
          <span className="text-[11px] tabular-nums" style={{ color: C.dim }}>
            {/* Reuses the pull-request panel's own key rather than adding a
                twelfth-locale translation for a string the product already has.
                One spelling of "PR #12" across the dashboard is also the point. */}
            {i18nT('components.pullRequestPanel.pr_number', { number: item.pr })}
          </span>
        ) : null}
        {artifacts.map(([key, value]) => (
          <span key={key} className="text-[11px]" style={{ color: C.dim }}>
            <span className="mr-1 opacity-70">{key}</span>
            <span style={{ color: C.text }}>{value}</span>
          </span>
        ))}
        {item.fails > 0 ? (
          <span className="text-[11px] tabular-nums" style={{ color: C.warn }}>
            {i18nT('pages.crewBoard.fails')} {item.fails}
          </span>
        ) : null}

        {item.events.length > 0 ? (
          <Btn
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="gap-1 rounded-none border-0 bg-transparent px-0 py-0 text-[11px] text-muted"
            aria-expanded={open}
          >
            {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            {i18nT('pages.crewBoard.events')}
            <span className="tabular-nums">{item.events.length}</span>
          </Btn>
        ) : null}
      </div>

      {open ? (
        <div className="ml-[15px] mt-1.5 flex flex-col gap-0.5">
          {item.events.map((event) => (
            <div key={event.id} className="flex flex-wrap items-baseline gap-x-2 text-[11px]">
              <span className="tabular-nums md:w-[4.5rem] md:shrink-0" style={{ color: C.dim }}>
                {fmtRelative(event.ts)}
              </span>
              <span className="md:w-[4rem] md:shrink-0" style={{ color: C.dim }}>
                {event.kind}
              </span>
              <span className="min-w-0 flex-1" style={{ color: C.text }}>
                {event.text}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

/** The board for ONE conductor. Exported so the Crew page can host it without
 *  going through the route, which is how it will be embedded there. */
export function CrewBoard({ conductor }: { conductor: string }) {
  const [showFinished, setShowFinished] = useState(false)

  const board = useQuery<WorkBoardResponse>({
    queryKey: crewBoardQueryKey(conductor),
    queryFn: () => api.crewBoard(conductor),
    refetchInterval: CREW_BOARD_POLL_MS,
    enabled: Boolean(conductor),
  })

  const bands = useMemo(
    () => partitionBoardRows(board.data?.items ?? []),
    [board.data?.items],
  )

  // Straight from the server's own answer, never inferred here: whether a
  // take-over can be performed is a property of the gateway, and a page that
  // guessed would offer a button the route refuses. Defaults to false so a board
  // still loading never renders an enabled action.
  const takeOverAvailable = board.data?.take_over_available ?? false

  if (!conductor) {
    return <EmptyState icon={<Inbox size={20} />} title={i18nT('pages.crewBoard.missing_conductor')} />
  }

  // A session that owns no work ledger is an expected GAP, not a failure: only a
  // session dispatched through the conductor tooling opens one, so an ad-hoc
  // conductor legitimately has none. Rendering it as an error would teach people
  // the board is broken.
  if (board.isError) {
    // A 404 is the ad-hoc-conductor GAP, not a failure, so it stays a neutral
    // EmptyState: rendering it as an error would teach people the board is broken.
    if (isNotFoundError(board.error)) {
      return (
        <EmptyState
          icon={<Inbox size={20} />}
          title={i18nT('pages.crewBoard.no_ledger_title')}
          subtitle={i18nT('pages.crewBoard.no_ledger_subtitle')}
        />
      )
    }
    // Everything else IS an error, and goes through ErrorNotice rather than a
    // neutral container, which is what keeps the structured context and the agent
    // hand-off instead of a dead end. `askAgent` is safe on this surface: the board
    // is read-only and holds no unsaved draft for the hand-off to navigate away from.
    return (
      <ErrorNotice
        title={i18nT('pages.crewBoard.error_title')}
        message={errMessage(board.error) || i18nT('components.errorBoundary.something_went_wrong')}
        askAgent
      />
    )
  }

  if (!board.data) return null

  const { conductor: record, items } = board.data

  // The goal belongs to the LEDGER, not to the item list, so it renders on an
  // empty board too. Returning the empty state alone left a reader who followed
  // the menu entry looking at a page that named nothing -- no goal, no round, no
  // way to tell it was even the right conductor's board.
  const header = record.goal ? (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 pb-1">
      <span className="text-[11px] uppercase tracking-wide" style={{ color: C.dim }}>
        {i18nT('pages.crewBoard.goal')}
      </span>
      <span className="min-w-0 flex-1 text-[12px]" style={{ color: C.text }}>
        {record.goal}
      </span>
      <span className="shrink-0 text-[11px] tabular-nums" style={{ color: C.dim }}>
        {i18nT('pages.crewBoard.round')} {record.round}
      </span>
    </div>
  ) : null

  if (items.length === 0) {
    return (
      <div className="flex flex-col">
        {header}
        <EmptyState icon={<Inbox size={20} />} title={i18nT('pages.crewBoard.empty_title')} />
      </div>
    )
  }

  return (
    <div className="flex flex-col">
      {header}

      {bands.ruling.length > 0 ? (
        <>
          <BandHeading label={i18nT('pages.crewBoard.band_ruling')} count={bands.ruling.length} />
          {bands.ruling.map((item) => (
            <ItemRow key={item.item_id} item={item} conductor={conductor} takeOverAvailable={takeOverAvailable} />
          ))}
        </>
      ) : null}
      {bands.working.length > 0 ? (
        <>
          <BandHeading label={i18nT('pages.crewBoard.band_working')} count={bands.working.length} />
          {bands.working.map((item) => (
            <ItemRow key={item.item_id} item={item} conductor={conductor} takeOverAvailable={takeOverAvailable} />
          ))}
        </>
      ) : null}

      {bands.finished.length > 0 ? (
        <>
          <Btn
            type="button"
            onClick={() => setShowFinished((v) => !v)}
            className="mt-3 gap-1.5 rounded-none border-0 bg-transparent px-0 py-0 text-[11px] uppercase tracking-wide text-muted"
            aria-expanded={showFinished}
          >
            {showFinished ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            {i18nT('pages.crewBoard.band_finished')}
            <span className="tabular-nums">{bands.finished.length}</span>
          </Btn>
          {showFinished
            ? bands.finished.map((item) => (
                <ItemRow key={item.item_id} item={item} conductor={conductor} takeOverAvailable={takeOverAvailable} />
              ))
            : null}
        </>
      ) : null}
    </div>
  )
}

/** Route wrapper: the conductor comes from `?conductor=`, so a board is a URL
 *  someone can bookmark or paste, and two conductors can be open side by side. */
export default function CrewBoardPage() {
  const [params] = useSearchParams()
  const conductor = (params.get('conductor') ?? '').trim()

  // PageHeader plus `overflow-y-auto flex-1 min-h-0`, which is the shell's own
  // contract: the dashboard renders a page inside a flex column, and a centred
  // fixed-width wrapper takes the scroll away from it. One gutter value at every
  // width, which the rule permits, and PageHeader shares it so the title is not
  // inset from the board it labels.
  return (
    <>
      {/* The page is the board; the Card below lists the items in it. Two distinct
          names, because one word repeated twice down the same column reads as a
          rendering fault rather than a hierarchy. */}
      <PageHeader title={i18nT('pages.crewBoard.page_title')} />
      <div className="px-4 pb-8 overflow-y-auto flex-1 min-h-0">
        {conductor ? (
          <div className="pb-2">
            <Link
              to={`/chat?sid=${encodeURIComponent(conductor)}`}
              className="text-[11px] underline-offset-2 hover:underline"
              style={{ color: C.dim }}
            >
              {i18nT('pages.crewBoard.open_conductor')}
            </Link>
          </div>
        ) : null}
        <Card className="p-3">
          <CardTitle>
            {i18nT('pages.crewBoard.title')}{' '}
            <InfoTip text={i18nT('pages.crewBoard.board_info')} />
          </CardTitle>
          <CrewBoard conductor={conductor} />
        </Card>
      </div>
    </>
  )
}
