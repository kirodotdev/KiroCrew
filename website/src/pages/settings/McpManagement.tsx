import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, Gauge, ListChecks, RefreshCw, Server as ServerIcon } from 'lucide-react'
import {
  api,
  ApiError,
  type McpManagedServer,
  type McpMeasureProgress,
  type McpShareRecommendation,
  type McpShareReason,
} from '../../api/client'
import { Tabs, TabsContent, TabsCount, TabsList, TabsTrigger, type TabItem } from '../../components/ui/tabs'
import { TABS_RAIL_ROW_CLASS } from '../../components/ui/tabsPill'
import { Btn } from '../../components/ui'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog'
import { splitOnPlaceholder } from '../../apps/crew-companion/splitOnPlaceholder'
import { i18nT } from '../../i18n/t'

/**
 * MCP Management — two decisions, one per layer, and nothing else.
 *
 * Per server (the table): interpose Kiro Crew's stub. That alone is what lets the
 * server render its own UI, and it leaves the backend private to each session —
 * the useful state for a server that holds per-session state.
 *
 * Global (the card): route those stubs to ONE shared backend process. Sharing is
 * the only thing this switch does, and it can only act on servers that already
 * have a stub, so the two layers never overlap.
 *
 * The words matter here: "stub" is the per-server layer and "routing to a shared
 * backend" is the global one. Naming the per-server switch "route" would claim the
 * global layer's job for it, which is exactly the confusion this page has to avoid.
 *
 * There is deliberately no per-server sharing control. The previous page had one,
 * which is how an operator could end up with sharing "on" while the allowlist it
 * acted on was empty — a switch with no observable effect.
 *
 * The second sub-view, "Sharing assessment", adds NO third decision. It is
 * read-only evidence: what the gateway managed to observe about each server, and
 * how that lines up with what the server is running as right now. It lives beside
 * the table rather than inside it because a verdict is not a control — folding it
 * into the stub column would read as a fifth switch — and it lives on this page
 * rather than under Connections because this is where the decision it informs is
 * made.
 *
 * Why the assessment is worth a screen even though it rarely says "share this":
 * a share recommendation requires the server to advertise the caller-identity
 * extension, which is a high bar almost nothing clears, so a column that only
 * showed "recommended / not recommended" would read as permanently broken. What
 * has content for every row is the REASON, plus the disagreement between the
 * verdict and the state the server is actually running in.
 */

const DOCS_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/architecture/design-notes/mcp-stub-decoupling.md'

type GatewayStatus = {
  enabled: boolean
  stub: string[]
  stub_count: number
  running: boolean
  ping_ok: boolean
  supported: boolean
}

/** The two sub-views: the decisions, and the evidence behind them. */
type McpView = 'servers' | 'assessment'

/**
 * Catalog KEYS, not prose. The strict i18n lint reads inside ALL-CAPS module
 * constants, and a table of English labels here would both trip it and freeze the
 * copy in code; the key is resolved at render instead.
 *
 * Every tier the verdict engine can emit is present. An unrecognised tier — an
 * older or newer gateway naming one we do not know — falls back to "not
 * measured", which is the honest reading of a verdict we cannot interpret.
 */
const STRENGTH_LABEL_KEY: Record<string, string> = {
  refuted: 'pages.mcpManagement.assessment.strength_refuted',
  disqualified: 'pages.mcpManagement.assessment.strength_disqualified',
  declared: 'pages.mcpManagement.assessment.strength_declared',
  measured: 'pages.mcpManagement.assessment.strength_measured',
  no_objection: 'pages.mcpManagement.assessment.strength_no_objection',
  unknown: 'pages.mcpManagement.assessment.strength_unknown',
}

/** One key per reason code the verdict engine emits. */
const REASON_LABEL_KEY: Record<string, string> = {
  observed_hazard: 'pages.mcpManagement.assessment.reason_observed_hazard',
  not_stdio: 'pages.mcpManagement.assessment.reason_not_stdio',
  session_bound_by_construction:
    'pages.mcpManagement.assessment.reason_session_bound_by_construction',
  rotating_secret_env: 'pages.mcpManagement.assessment.reason_rotating_secret_env',
  not_probed: 'pages.mcpManagement.assessment.reason_not_probed',
  degrades_when_shared: 'pages.mcpManagement.assessment.reason_degrades_when_shared',
  handshake_not_reproducible:
    'pages.mcpManagement.assessment.reason_handshake_not_reproducible',
  declares_caller_identity: 'pages.mcpManagement.assessment.reason_declares_caller_identity',
  all_tools_read_only: 'pages.mcpManagement.assessment.reason_all_tools_read_only',
  preflight_passed: 'pages.mcpManagement.assessment.reason_preflight_passed',
  preflight_not_run: 'pages.mcpManagement.assessment.reason_preflight_not_run',
  no_objection_found: 'pages.mcpManagement.assessment.reason_no_objection_found',
  no_tool_annotations: 'pages.mcpManagement.assessment.reason_no_tool_annotations',
  no_tools_listed: 'pages.mcpManagement.assessment.reason_no_tools_listed',
}

/**
 * The states a row can be in, as a closed set.
 *
 * Deliberately NOT the i18n keys. Every reader other than the label itself asks
 * "is this row shared" or "was it declined", and comparing against a catalog key
 * makes that question depend on a string that lives in 13 JSON files: rename the
 * key and `!== 'pages.mcpManagement.state_shared'` quietly becomes "never shared",
 * which silently drops the accent colour, the sharing-without-support warning and
 * the assessment count with nothing for `tsc` to catch. This PR renamed one of
 * these keys once already. A discriminant makes the same mistake a type error.
 */
type McpRowState = 'no_stub' | 'direct_env' | 'shared' | 'stub' | 'direct'

const STATE_LABEL_KEY: Record<McpRowState, string> = {
  no_stub: 'pages.mcpManagement.state_no_stub',
  direct_env: 'pages.mcpManagement.state_direct_env',
  shared: 'pages.mcpManagement.state_shared',
  stub: 'pages.mcpManagement.state_stub',
  direct: 'pages.mcpManagement.state_direct',
}

/**
 * What the server is running as, as ONE function used by both sub-views.
 *
 * The assessment table has to name the current state to be able to show it
 * disagreeing with the verdict, and two copies of this mapping would be free to
 * drift into saying different things about the same row.
 *
 * This is the SOLE derivation of a row's state. The chip's text, the chip's
 * colour, the per-row note and the sharing-without-support warning all read its
 * answer instead of recomputing their own, because a second spelling of any of
 * these states is the defect this change exists to remove: one copy gets corrected
 * and the other keeps saying `shared`.
 */
function rowState(s: McpManagedServer, sharingOn: boolean): McpRowState {
  if (!s.can_stub) return 'no_stub'
  // The rewriter's decision outranks allowlist membership and the global switch,
  // but only for a row the operator opted IN, because that is the only row whose
  // state would otherwise be reported as shared.
  //
  // The state is `direct`, not a fourth thing: on this branch the rewriter passes
  // the ORIGINAL spec through and never reaches `_build_stub_entry`, so no stub is
  // created and the session launches the server itself -- which is what `direct`
  // means everywhere else on this page. `(env)` marks WHY an opted-in server ended
  // up there, and is the only part that is new.
  //
  // Scoped to the ENV obstacle on purpose: the rewriter also declines when it
  // cannot resolve the command, and the row payload carries no signal for that, so
  // such a row still reads `shared`. Naming it here would be a claim this data
  // cannot support -- it belongs with the backend-computed-state follow-up.
  //
  // A row the operator did NOT opt in reads plain `direct`: there the field is
  // forward-looking ("stubbing this would still not pool it"), which the batch
  // action uses as a skip reason, and nothing has been declined yet to explain.
  if (s.stub && s.pooling_blocked_by_env === true) return 'direct_env'
  if (s.stub && sharingOn) return 'shared'
  if (s.stub) return 'stub'
  return 'direct'
}

/** Evidence tiers that argue AGAINST sharing, as opposed to merely not endorsing it.
 *
 *  `refuted` is an observation of the server misbehaving while shared, and
 *  `disqualified` is a declaration we trust ruling sharing out up front. Those are
 *  the two that say something is wrong.
 */
const CONTRARY_STRENGTHS = new Set(['refuted', 'disqualified'])

/**
 * True when the server is sharing a backend right now and the evidence argues
 * against it.
 *
 * This is the one thing on the page worth colouring, so what trips it has to be
 * evidence CONTRARY to sharing, never the mere absence of an endorsement. The two
 * are easy to conflate because both leave `recommendShare` false, and conflating
 * them makes the warning useless in opposite directions:
 *
 *   - `unknown` means nobody has measured this server. Flagging it would assert a
 *     finding from a measurement that never ran.
 *   - `no_objection` means nothing disqualifying was found. It is the weakest
 *     useful verdict and the one most servers sit at, so flagging it would put a
 *     permanent warning over a healthy fleet and teach the operator to ignore the
 *     only coloured signal on the page.
 *
 * Both of those are quiet. What speaks is `refuted` or `disqualified`.
 *
 * A row the rewriter declined to stub is not sharing at all, so it cannot be
 * sharing-without-support however damning its evidence is. That exclusion is not
 * spelled here: this asks `rowState` whether the row's state IS `shared`,
 * because a second spelling of "is sharing right now" is the same mistake as the
 * colour that used to disagree with the label -- one copy gets a new state added
 * to it and the other does not. Reading the label fixes both readers at once: the
 * warning icon on the row and the count the assessment view sends the operator
 * over to find.
 */
function sharedWithoutSupport(s: McpManagedServer, sharingOn: boolean): boolean {
  if (rowState(s, sharingOn) !== 'shared') return false
  const rec = s.recommendation
  if (!rec) return false
  return CONTRARY_STRENGTHS.has(rec.strength)
}

/** How long the batch action waits for an uncapped measurement pass, in ms. */
const MEASURE_WAIT_MS = 4 * 60 * 1000
const MEASURE_POLL_MS = 1500

/**
 * Poll the measurement pass until it stops. True when it finished in time.
 *
 * The pass measures every unmeasured server with no budget, two spawns each, so
 * on a fresh install it is minutes rather than seconds. Returning false rather
 * than proceeding is deliberate: acting on a half-measured fleet would stub
 * whatever happened to be done and silently skip the rest, which is exactly the
 * "reported more than it did" failure this control has to avoid.
 *
 * Each reading is written into the SAME query cache entry `MeasureControl` reads,
 * so the page has one source of displayed progress rather than two. Consuming the
 * readings here and throwing them away left the button's own line rendering a
 * hardcoded zero — a counter frozen at "0 of N" for up to four minutes, which
 * reads as a stalled pass on exactly the fresh install this control exists for.
 */
async function waitForMeasurePass(qc: QueryClient): Promise<boolean> {
  const deadline = Date.now() + MEASURE_WAIT_MS
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, MEASURE_POLL_MS))
    // A failed read is not a finished pass. Treat it as "keep waiting" so a
    // single dropped request cannot make the caller act early; the deadline is
    // what ends the loop.
    try {
      const p = await api.mcpMeasureProgress()
      qc.setQueryData(['mcp-measure-progress'], p)
      if (!p.running) return true
    } catch {
      // keep polling
    }
  }
  return false
}

function Switch({
  on,
  disabled,
  onClick,
  label,
  describedBy,
}: {
  on: boolean
  disabled?: boolean
  onClick: () => void
  label: string
  describedBy?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onClick}
      className={[
        'relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full transition-colors',
        on ? 'bg-accent' : 'bg-[var(--border-strong,var(--border))]',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          // The knob rides ON the accent fill, so it needs the same light face in
          // every theme; there is no token for that pairing (`--accent-fg` is for
          // text) and the app-scoped switches paint theirs from CSS we cannot use
          // from a settings page.
          'absolute h-[18px] w-[18px] rounded-full bg-white shadow transition-all',
          on ? 'left-[18px]' : 'left-[2px]',
        ].join(' ')}
      />
    </button>
  )
}

/** One reason line: a translated sentence, and the server's own verbatim detail.
 *
 *  The detail sits on its own line as a chip rather than inline. Appending a raw
 *  token to the end of a sentence made the two read as one broken sentence
 *  ("...belongs to one client logging_level"), and it quietly required every
 *  reason string to be worded so that a token could follow it. As a chip the
 *  sentence stands alone and the token reads as the tag it is.
 */
function ReasonLine({ reason }: { reason: McpShareReason }) {
  const key = REASON_LABEL_KEY[reason.code]
  return (
    <li className="leading-relaxed">
      {/* An unknown code still has to say something, and its raw code is more
          use to whoever has to look it up than a blank cell. */}
      <span>{key ? i18nT(key) : reason.code}</span>
      {reason.detail && (
        <span className="mt-0.5 block w-fit rounded border border-[var(--border)] px-1 font-mono text-[11px] text-[var(--text)]">
          {reason.detail}
        </span>
      )}
    </li>
  )
}

function AssessmentRow({
  server,
  sharingOn,
}: {
  server: McpManagedServer
  sharingOn: boolean
}) {
  const rec: McpShareRecommendation | undefined = server.recommendation
  const strengthKey = rec ? STRENGTH_LABEL_KEY[rec.strength] : undefined
  // `no_objection_found` says only what the Assessment pill already says, so it
  // yields whenever the row has a reason specific to this server. It is kept when
  // it is the only one, because an empty Evidence cell beside a filled verdict
  // reads as missing data rather than as nothing further to report.
  const reasons = (rec?.reasons ?? []).filter(
    (r, _i, all) => r.code !== 'no_objection_found' || all.length === 1,
  )
  const unsupported = sharedWithoutSupport(server, sharingOn)
  return (
    <tr className="border-t border-[var(--border)]">
      <td className="px-4 py-3 align-top font-mono text-[13px] text-[var(--text)]">
        {server.name}
      </td>
      <td className="px-4 py-3 align-top">
        <span
          className={[
            'inline-block rounded-full px-2 py-0.5 font-mono text-[11px]',
            rec?.recommendShare
              ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
              : 'border border-[var(--border)] text-[var(--muted)]',
          ].join(' ')}
        >
          {i18nT(strengthKey || 'pages.mcpManagement.assessment.strength_unknown')}
        </span>
      </td>
      <td className="px-4 py-3 align-top text-[12.5px] text-[var(--muted)]">
        {reasons.length > 0 ? (
          <ul className="list-none space-y-0.5">
            {reasons.map((r, i) => (
              <ReasonLine key={`${r.code}-${i}`} reason={r} />
            ))}
          </ul>
        ) : (
          <span aria-hidden="true">{'\u2014'}</span>
        )}
      </td>
      <td className="px-4 py-3 align-top text-right">
        <span
          className={[
            'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 font-mono text-[11px]',
            unsupported
              ? 'border border-[var(--danger)] text-[var(--danger)]'
              : 'border border-[var(--border)] text-[var(--muted)]',
          ].join(' ')}
        >
          {/* Colour cannot be the only thing that separates a flagged row from a
              healthy one, so the flag carries a mark of its own. It is decorative
              to a screen reader because the row's Assessment cell already states
              the verdict in words. */}
          {unsupported && <AlertTriangle size={11} aria-hidden="true" />}
          {i18nT(STATE_LABEL_KEY[rowState(server, sharingOn)])}
        </span>
      </td>
    </tr>
  )
}

/**
 * Sharing assessment — read-only. Reads the SAME query the servers table does, so
 * the two views can never disagree about which servers exist, and opening this tab
 * costs no request and starts no server.
 */
/**
 * The one control on this view: measure the servers that have no verdict yet.
 *
 * It exists because the assessment is only as useful as the number of rows that
 * carry a measurement, and the pass that produces them was previously reachable
 * only as a side effect of an icon-only refresh that measures a couple of servers
 * per press. A fleet of thirty needs fifteen presses and a guess about what the
 * icon does, which is why this button says what it does and how much is left.
 *
 * Progress is polled rather than streamed: the pass is minutes long at worst, a
 * two-second read costs nothing next to two process spawns per server, and a
 * dropped poll self-corrects on the next one where a dropped stream event does
 * not.
 */
function MeasureControl({ unmeasuredCount }: { unmeasuredCount: number }) {
  const qc = useQueryClient()
  const [asked, setAsked] = useState(false)

  const progress = useQuery({
    queryKey: ['mcp-measure-progress'],
    queryFn: () => api.mcpMeasureProgress(),
    // Only poll while a pass is actually running. Polling a finished pass forever
    // would keep a timer alive on a settings page nobody is looking at.
    refetchInterval: (q) => (q.state.data?.running ? 2000 : false),
    // A pass started from another tab (or before this page mounted) still has to
    // show up here, so the first read happens on mount rather than on click --
    // which is the default, stated here because the interval above is not.
  })

  const start = useMutation({
    mutationFn: () => api.mcpMeasureStart(),
    onSuccess: (data) => {
      setAsked(true)
      qc.setQueryData(['mcp-measure-progress'], data)
    },
  })

  const running = progress.data?.running === true
  const done = progress.data?.done ?? 0
  const measured = progress.data?.measured ?? 0
  const total = progress.data?.total ?? 0

  // The verdicts the pass just wrote live in a DIFFERENT query, and nothing else
  // refetches it: without this the operator watches progress reach the end and
  // then reads a table still saying "not measured", beside a button still
  // offering the same count. That contradiction is the end of every single use of
  // this control, so the refresh belongs here and not in a manual reload.
  //
  // Keyed on the running edge rather than on the mutation, because a pass started
  // from another tab settles here too and its result is just as stale.
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !running) {
      void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
    }
    wasRunning.current = running
  }, [running, qc])

  // A pass that stopped early. Deliberately NOT gated on this session: a died
  // pass is otherwise indistinguishable from one that had nothing to do, which is
  // the difference between measured and never measured.
  const failed = !!progress.data?.error && !running

  // A pass this session that has stopped and measured something. Not shown for a
  // pass that only ever ran in some earlier session: "finished" with no numbers
  // attached tells the reader nothing they can use. Excludes a failed pass, and
  // counts what was actually MEASURED rather than what was attempted -- a pass
  // that died at 1 of 5 must not close with "Measured 5 servers".
  //
  // `measured` and `done` are separate fields because a pass can attempt a server
  // and measure nothing: a missing credential or a host where the probe cannot
  // spawn leaves no verdict, so the row stays unmeasured and this button keeps
  // offering it. Gating on `done` closed with "Measured 30 servers" beside a table
  // still showing thirty unmeasured rows. Gating on `measured` withholds the line
  // entirely when nothing was measured, which is silent but never false -- the
  // button's own unchanged count is what tells the reader nothing landed.
  const settled = asked && !running && measured > 0 && !failed

  return (
    <div className="flex flex-wrap items-center gap-3">
      <button
        type="button"
        onClick={() => start.mutate()}
        disabled={running || start.isPending || unmeasuredCount === 0}
        className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border)] px-3 py-1.5 text-[13px] text-[var(--text)] hover:border-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Gauge size={14} className="shrink-0" />
        {unmeasuredCount > 0
          ? i18nT('pages.mcpManagement.assessment.measure_unmeasured', {
              count: unmeasuredCount,
            })
          : i18nT('pages.mcpManagement.assessment.measure_none_left')}
      </button>

      {running && (
        <span role="status" className="text-[12.5px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.assessment.measure_running', { done, total })}
        </span>
      )}
      {settled && (
        <span role="status" className="text-[12.5px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.assessment.measure_finished', { count: measured })}
        </span>
      )}
      {/* A pass that stopped early is reported here rather than only in the log:
          the operator is watching this readout and would otherwise read a short
          pass as a completed one. */}
      {failed && (
        <span role="status" className="text-[12.5px] text-[var(--danger)]">
          {i18nT('pages.mcpManagement.assessment.measure_failed')}
        </span>
      )}
      {start.isError && (
        <span role="status" className="text-[12.5px] text-[var(--danger)]">
          {i18nT('pages.mcpManagement.assessment.measure_failed')}
        </span>
      )}
    </div>
  )
}

function AssessmentView({
  servers,
  sharingOn,
  loading,
  isError,
  onOpenServers,
  unsupportedCount,
  unmeasuredCount,
}: {
  servers: McpManagedServer[]
  sharingOn: boolean
  loading: boolean
  isError: boolean
  onOpenServers: () => void
  unsupportedCount: number
  unmeasuredCount: number
}) {
  return (
    <div className="space-y-4">
      <p className="max-w-[76ch] text-[13px] leading-relaxed text-[var(--muted)]">
        {i18nT('pages.mcpManagement.assessment.lede')}
      </p>
      {/* Where the verdicts come from. Without this, a fleet whose rows mostly read
          "not measured" looks like a feature that does not work, rather than one
          waiting on a probe that runs elsewhere and a couple of servers at a time.
          The destination is a link because naming a place the reader has to find
          themselves is most of the friction this line exists to remove. */}
      <p className="max-w-[76ch] text-[12.5px] leading-relaxed text-[var(--muted)]">
        {splitOnPlaceholder(i18nT('pages.mcpManagement.assessment.how_measured'), 'link').map(
          (part, i) =>
            part === null ? (
              <Link
                key="link"
                to="/capabilities?tab=mcp"
                className="text-[var(--accent)] hover:underline"
              >
                {i18nT('pages.mcpManagement.assessment.connections_link')}
              </Link>
            ) : (
              <span key={i}>{part}</span>
            ),
        )}
      </p>

      <MeasureControl unmeasuredCount={unmeasuredCount} />

      {/* Only ever shown when there is something to show. A count of zero is the
          normal state and saying so every time trains people to ignore the line. */}
      {unsupportedCount > 0 && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-lg border border-[var(--danger)] bg-[var(--danger-subtle,transparent)] px-3.5 py-2.5 text-[13px] text-[var(--text)]"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-[var(--danger)]" />
          <span className="flex-1">
            {i18nT('pages.mcpManagement.assessment.shared_without_support', {
              count: unsupportedCount,
            })}
          </span>
          {/* The remedy is a switch on the other tab, so the warning carries the
              way there. Navigation, not a control: nothing about a server changes
              from this view. */}
          <button
            type="button"
            onClick={onOpenServers}
            className="shrink-0 rounded-md border border-[var(--border)] px-2 py-0.5 text-[12.5px] text-[var(--text)] hover:border-[var(--accent)]"
          >
            {i18nT('pages.mcpManagement.assessment.open_servers')}
          </button>
        </div>
      )}

      <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="w-[26%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_server')}
              </th>
              <th className="w-[20%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.assessment.col_assessment')}
              </th>
              <th className="w-[38%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.assessment.col_evidence')}
              </th>
              <th className="w-[16%] px-4 pb-2.5 pt-3.5 text-right text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.assessment.col_running_as')}
              </th>
            </tr>
          </thead>
          <tbody>
            {servers.map(s => (
              <AssessmentRow key={s.name} server={s} sharingOn={sharingOn} />
            ))}
            {isError && (
              <tr className="border-t border-[var(--border)]">
                <td colSpan={4} className="px-4 py-6 text-center text-[13px] text-[var(--danger)]">
                  {i18nT('pages.mcpManagement.servers_failed')}
                </td>
              </tr>
            )}
            {servers.length === 0 && !loading && !isError && (
              <tr className="border-t border-[var(--border)]">
                <td colSpan={4} className="px-4 py-6 text-center text-[13px] text-[var(--muted)]">
                  {i18nT('pages.mcpManagement.no_servers')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {/* The bar for recommending SHARING is high enough that most healthy
            servers never clear it. Without saying so, a table of "no objection"
            rows reads as a broken feature rather than a conservative one. */}
        <div className="border-t border-[var(--border)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--muted)]">
          {i18nT('pages.mcpManagement.assessment.legend')}
        </div>
        {/* This view renders the same state chips through the same derivation, so a
            term defined only under the OTHER tab is undecodable to the operator
            auditing sharing here -- which is this fix's whole audience. ONE key,
            rendered wherever the chip can appear, in its own block on both tabs. */}
        <div className="border-t border-[var(--border)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--muted)]">
          {i18nT('pages.mcpManagement.state_direct_env_legend')}
        </div>
      </section>
    </div>
  )
}

export function McpManagement() {
  const qc = useQueryClient()
  const [confirmSharing, setConfirmSharing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Pending-effect information, kept separate from `error` so a stub change that
  // needs a restart is not painted as a failure. Distinct from the enable-all
  // `notice` below, which is that control's own inline hint rather than a
  // page-level statement about the gateway.
  const [restartNotice, setRestartNotice] = useState<string | null>(null)
  // A third state rather than a reuse of either sibling above: `notice` reports a
  // stub-set change and `restartNotice` reports a pending restart, and one banner
  // shared by unrelated actions would let either overwrite the other's result.
  const [resolveNotice, setResolveNotice] = useState<string | null>(null)

  const statusQ = useQuery<GatewayStatus>({
    queryKey: ['mcpGatewayStatus'],
    queryFn: () => api.mcpGatewayStatus(),
  })
  const serversQ = useQuery<{ servers: McpManagedServer[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers(),
  })

  const status = statusQ.data
  // Memoized so the six derived sets below keep a stable dependency: the `?? []`
  // built a fresh array on every render, which re-ran each `useMemo` every time
  // and is what the exhaustive-deps warnings on this component were about.
  const servers = useMemo(() => serversQ.data?.servers ?? [], [serversQ.data])
  const stubCount = useMemo(() => servers.filter(s => s.stub).length, [servers])
  const eligibleCount = useMemo(() => servers.filter(s => s.can_stub).length, [servers])
  const supported = status?.supported ?? true

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
    void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
  }

  // Both endpoints persist to config.json BEFORE the in-process apply, so a 500
  // means "saved but not live" — not "nothing happened". Claiming nothing was
  // saved would leave the operator with a setting that quietly takes effect on
  // the next restart, so the failure path refetches and says so.
  const onApplyError = (key: string) => () => {
    invalidate()
    setError(i18nT(key))
  }

  const setStub = useMutation({
    mutationFn: ({ name, stub }: { name: string; stub: boolean }) =>
      api.mcpGatewaySetStub(name, stub),
    // A 200 means the config was persisted, NOT that the broker reached the
    // wanted state. A stub change is never applied in place -- the daemon's
    // routing is built with the agent-spec rewrite at startup -- so the normal
    // outcome is `restart_required`, which is pending information rather than a
    // failure. `applied: false` with no restart hint is the real fault case:
    // the gateway never wired the apply callback, so nothing was recorded.
    onSuccess: res => {
      invalidate()
      if (res && res.restart_required) {
        setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      } else if (res && res.applied === false) {
        setError(i18nT('pages.mcpManagement.stub_not_live'))
      }
    },
    onError: onApplyError('pages.mcpManagement.stub_failed'),
  })

  const setSharing = useMutation({
    mutationFn: (enabled: boolean) => api.mcpGatewayEnable(enabled),
    // Same asymmetry: enabling sharing can persist and still leave the broker
    // unreachable, and `ping_ok` is the only thing that says so.
    onSuccess: (res, enabled) => {
      invalidate()
      if (enabled && !res.ping_ok) {
        setError(i18nT('pages.mcpManagement.sharing_not_live'))
      }
    },
    onError: onApplyError('pages.mcpManagement.sharing_failed'),
  })

  // Pre-resolving an npm-launcher server lets its launch exec the installed
  // tree, so session start does no dependency resolution. The timed pass keeps
  // an unpinned spec current on its own; this is the operator asking to check
  // upstream now, so it reports what the pass produced rather than only that it
  // ran. A 409 means a pass is already in flight, which is information, not a
  // failure to retry.
  const resolveRefresh = useMutation({
    mutationFn: () => api.mcpResolveRefresh(),
    onSuccess: res => {
      invalidate()
      if (!res.ok) {
        // Not an error: the operator has nothing routed, which is the same class
        // of fact the sharing card states as plain text rather than as an alarm.
        setResolveNotice(i18nT('pages.mcpManagement.resolve_no_targets'))
        return
      }
      const ready = res.ready?.length ?? 0
      // `ready === 0` has two causes that must NOT read the same. Everything was
      // already fresh (nothing to do), or every install failed -- a registry
      // outage, a rejected token. Reporting the second as "nothing needed" tells
      // someone who just pressed this button that launches now skip the network
      // when not one of them does. The per-package outcome is already in the
      // response; count it rather than inferring from `ready` alone.
      const failed = Object.values(res.resolved ?? {}).filter(state => state === 'error').length
      if (failed > 0) {
        setResolveNotice(
          ready > 0
            ? i18nT('pages.mcpManagement.resolve_partly_ready', {
                ready: String(ready),
                failed: String(failed),
              })
            : i18nT('pages.mcpManagement.resolve_all_failed', { failed: String(failed) }),
        )
        return
      }
      setResolveNotice(
        ready > 0
          ? i18nT('pages.mcpManagement.resolve_ready', { ready: String(ready) })
          : i18nT('pages.mcpManagement.resolve_none_ready'),
      )
    },
    onError: err => {
      invalidate()
      // 409 is the endpoint reporting an in-flight pass, which it deliberately
      // encodes as information rather than a failure to retry. Painting it as
      // "could not pre-resolve" contradicts the state the response carries: the
      // pass the second tab is being told about is running fine.
      if (err instanceof ApiError && err.status === 409) {
        setResolveNotice(i18nT('pages.mcpManagement.resolve_already_running'))
        return
      }
      setError(i18nT('pages.mcpManagement.resolve_failed'))
    },
  })

  const busy = setStub.isPending || setSharing.isPending
  // An unsupported platform must never TRAP an operator in a state they cannot
  // leave: a config carried over from another machine can arrive with sharing on
  // or servers stubbed, so turning things OFF stays available and only turning
  // them ON is blocked. Enabling sharing over an empty stub set is blocked for
  // the same reason it no longer exists as a state: it would do nothing.
  // Shared by the tab badge, the confirm dialog and the assessment banner, so
  // the three can never disagree about how many rows are flagged.
  const unsupportedCount = useMemo(
    () => servers.filter(s => sharedWithoutSupport(s, !!status?.enabled)).length,
    [servers, status?.enabled],
  )
  // What turning sharing ON would put into that state, which is a different
  // question from what is in it now: nothing is shared until the switch is on.
  const wouldBeUnsupported = useMemo(
    () => servers
      .filter(s => s.stub && s.recommendation
        && CONTRARY_STRENGTHS.has(s.recommendation.strength))
      .map(s => s.name),
    [servers],
  )

  const canEnableSharing = supported && stubCount > 0

  // How many rows the measurement pass would actually act on. A row with no
  // ``recommendation`` at all counts too: an older gateway reached through Make
  // Live sends no verdict field, and that row is exactly as unmeasured as one
  // whose verdict says so.
  //
  // A row whose handshake did not reproduce counts as well, and this is load
  // bearing rather than a nicety. The backend deliberately re-measures such a row
  // every pass (a divergence is reported, never frozen), and the row's own text
  // tells the operator that measuring again retests it. Leaving it out of this
  // count disabled the only control that does so, which is an offered action with
  // no path to it.
  const unmeasuredCount = useMemo(
    () =>
      servers.filter(
        s =>
          !s.recommendation ||
          s.recommendation.strength === 'unknown' ||
          s.recommendation.reasons.some(r => r.code === 'handshake_not_reproducible'),
      ).length,
    [servers],
  )

  // One gesture for "stub everything the evidence allows", because the
  // alternative on a 35-server install is 35 clicks and the operator reads the
  // verdict column for each one — which is how a row the evidence argues against
  // gets stubbed by hand anyway.
  const [notice, setNotice] = useState<string | null>(null)
  // The live pass, read from the same cache entry `MeasureControl` polls into and
  // `waitForMeasurePass` writes to. Subscribing here rather than keeping a second
  // copy in this component is what keeps the two lines on this page from
  // disagreeing about how far one pass has got.
  const { data: measureProgress } = useQuery<McpMeasureProgress>({
    queryKey: ['mcp-measure-progress'],
    queryFn: () => api.mcpMeasureProgress(),
    // No interval: the batch action's own poll writes into this key while it
    // waits, and `MeasureControl` already polls on its own schedule when it is
    // mounted. A third schedule would just add requests.
    enabled: false,
  })
  // Deliberately NOT an eligibility test — only "is there anything worth asking
  // about". Eligibility is the server's to decide, and a second copy of that rule
  // here could disagree with it: too strict and the button is dead while servers
  // do qualify, too loose and it promises work that gets skipped. Gating on
  // stubbable-and-not-yet-stubbed keeps the control honest either way, because a
  // press that finds nothing qualifying reports exactly that.
  const stubbableNames = useMemo(
    () => servers.filter(s => s.can_stub && !s.stub).map(s => s.name),
    [servers],
  )

  const enableAll = useMutation({
    mutationFn: async () => {
      // Measure the unmeasured FIRST. An unmeasured row is never eligible, so
      // skipping this step would make the button quietly ignore exactly the
      // servers just installed — the ones the operator is most likely to be
      // here for. The pass is uncapped and can run for minutes; it reports
      // progress, and if it is still going when the wait runs out nothing is
      // stubbed and the operator is told to come back, which is better than
      // acting on a half-measured fleet.
      if (unmeasuredCount > 0) {
        await api.mcpMeasureStart()
        if (!(await waitForMeasurePass(qc))) return { enabled: 0, skipped: 0, pending: true, restart_required: false }
      }
      // Send every row that COULD be stubbed and let the server decide which
      // ones the evidence allows. The client cannot answer that soundly: the
      // sharing switch is a separate control and the verdicts move as
      // measurements land, so any answer computed here describes the moment it
      // was computed, not the moment of the write. The server resolves it inside
      // the same lock hold that performs the write, so one state decides both.
      //
      // Rows are still re-read rather than reused from the render this click came
      // from, because the measurement pass above changes which servers exist as
      // candidates at all. Called through `api` DIRECTLY, not `qc.fetchQuery`:
      // this app's shared QueryClient sets `staleTime: Infinity` (freshness comes
      // from WebSocket invalidation, not from age), so a cache-backed read would
      // resolve from that very render and the re-read would be a decoration.
      const fresh = await api.mcpGatewayServers()
      const rows = fresh?.servers ?? []
      const candidates = rows.filter(s => s.can_stub && !s.stub).map(s => s.name)
      if (candidates.length === 0) return { enabled: 0, skipped: 0, pending: false, restart_required: false }
      // Batch form: one config write, so the allowlist cannot land half-flipped.
      const res = await api.mcpGatewaySetStubMany(candidates, true, true)
      // Counts come from the RESPONSE, never from the request: the two differ by
      // design now, and reporting the request would claim stubs that were skipped.
      const stubbed = res.stubbed ?? candidates
      return {
        enabled: stubbed.length,
        skipped: (res.skipped ?? []).length,
        pending: false,
        applied: res.applied,
        // Carried through so the success handler can tell "waiting for a restart"
        // (the normal answer) from "nothing was recorded" (a fault).
        restart_required: res.restart_required === true,
      }
    },
    onSuccess: r => {
      invalidate()
      if (r.pending) {
        setNotice(i18nT('pages.mcpManagement.enable_all_still_measuring'))
        return
      }
      setNotice(
        i18nT('pages.mcpManagement.enable_all_result', {
          enabled: r.enabled,
          skipped: r.skipped,
        }),
      )
      // A stub change is never applied in place, so `restart_required` is the
      // normal answer and must not read as a fault. The not-live error is gated on
      // something having been WRITTEN: when the server skips every candidate it
      // deliberately never calls the apply hook, so `applied: false` there means
      // "nothing to apply", not "the gateway could not start" -- and the
      // "0 of N enabled" notice above already says what happened.
      if (r.restart_required) {
        setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      } else if (r.enabled > 0 && r.applied === false) {
        setError(i18nT('pages.mcpManagement.stub_not_live'))
      }
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.stub_failed'))
    },
  })

  // Local state, not a URL param. The sibling in-pane tab rails in this repo
  // (ConnectionsPage, knowledge) hold it the same way, this pane is already
  // addressed by the Developer page's own `?tab=`, and a second param would need
  // to coexist with it for a read-only view nobody deep-links to. The shared
  // component is still what draws the rail, so the keyboard and aria behaviour
  // come along for free.
  const [view, setView] = useState<McpView>('servers')
  // A function, not a module constant, so the labels re-translate on a language
  // switch instead of freezing at first import.
  const views: Array<TabItem<McpView>> = [
    {
      key: 'servers',
      label: i18nT('pages.mcpManagement.view_servers'),
      icon: <ServerIcon size={14} />,
    },
    {
      key: 'assessment',
      label: i18nT('pages.mcpManagement.view_assessment'),
      icon: <ListChecks size={14} />,
      // Zero renders nothing, so this appears only when there is something to
      // find. Without it the page's one coloured signal sits on a tab the
      // operator has no reason to open.
      count: unsupportedCount,
    },
  ]

  return (
    <Tabs
      value={view}
      onValueChange={v => setView(v as McpView)}
      layoutId="mcp-management-view"
      className="space-y-4"
    >
      <div className={TABS_RAIL_ROW_CLASS}>
        <TabsList aria-label={i18nT('pages.mcpManagement.views_aria')}>
          {views.map(v => (
            <TabsTrigger key={v.key} value={v.key}>
              {v.icon}
              <span>{v.label}</span>
              <TabsCount value={v.count} />
            </TabsTrigger>
          ))}
        </TabsList>
      </div>

      <TabsContent value="assessment">
        <AssessmentView
          servers={servers}
          sharingOn={!!status?.enabled}
          loading={serversQ.isLoading}
          isError={serversQ.isError}
          onOpenServers={() => setView('servers')}
          unsupportedCount={unsupportedCount}
          unmeasuredCount={unmeasuredCount}
        />
      </TabsContent>
      {/* The servers view stacks a lede header, two inline banners and three
          cards as SIBLINGS. The `space-y-4` on the <Tabs> root only gaps the tab
          rail from the panel below it -- it cannot reach inside a panel -- so
          without a gap class here the cards render flush against each other,
          unlike the assessment view (which wraps its body in `space-y-4`) and
          every other settings panel. Match that rhythm on the panel itself. */}
      <TabsContent value="servers" className="space-y-4">
      {/* No <h2> here: the Developer tab header already names this surface, and a
          second copy of the title read as two stacked headings. */}
      <header>
        <p className="max-w-[76ch] text-[13px] leading-relaxed text-[var(--muted)]">
          {/*
           * One key holds the whole sentence with a {{link}} placeholder, rather
           * than joining a lede key and a link-label key side by side. Halves
           * that each end mid-sentence cannot be reordered by a translator, and
           * plenty of languages need the link somewhere other than the end.
           */}
          {splitOnPlaceholder(i18nT('pages.mcpManagement.lede'), 'link').map((part, i) =>
            part === null ? (
              <a
                key="link"
                href={DOCS_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-[var(--accent)] hover:underline"
              >
                {i18nT('pages.mcpManagement.learn_more')}
                <ExternalLink size={12} />
              </a>
            ) : (
              <span key={i}>{part}</span>
            ),
          )}
        </p>
      </header>

      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-[var(--danger)] bg-[var(--danger-subtle,transparent)] px-3.5 py-2.5 text-[13px] text-[var(--text)]"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-[var(--danger)]" />
          <span>{error}</span>
        </div>
      )}

      {restartNotice && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-lg border border-[var(--accent)] bg-[var(--accent-subtle,transparent)] px-3.5 py-2.5 text-[13px] text-[var(--text)]"
        >
          <RefreshCw size={14} className="mt-0.5 shrink-0 text-[var(--accent)]" aria-hidden="true" />
          <span>{restartNotice}</span>
        </div>
      )}

      {/* Global: route every stub to one shared backend. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-5 py-4">
        <div className="flex items-start gap-5">
          <div className="flex-1">
            <div className="text-[15px] font-semibold text-[var(--text)]">
              {i18nT('pages.mcpManagement.sharing_label')}
            </div>
            <p
              id="mcp-sharing-desc"
              className="mt-1.5 max-w-[64ch] text-[13px] leading-relaxed text-[var(--muted)]"
            >
              {i18nT('pages.mcpManagement.sharing_description')}
            </p>
            {!supported && (
              <p className="mt-2 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.unsupported_platform')}
              </p>
            )}
            {/* A disabled control has to say why. This is the page's headline
                switch, so with nothing stubbed a first-time user's very first
                click silently did nothing and only the lede's last clause
                hinted at the gate. */}
            {supported && !status?.enabled && stubCount === 0 && (
              <p className="mt-2 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.sharing_needs_a_stub')}
              </p>
            )}
            {/* Sharing left ON over an empty stub set is the exact "switch with
                no observable effect" state this page exists to eliminate —
                reachable by unstubbing the last server. Name it instead of
                showing a live switch that governs nothing. */}
            {supported && status?.enabled && stubCount === 0 && (
              <p className="mt-2 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.sharing_on_but_nothing_stubbed')}
              </p>
            )}
          </div>
          <span className="shrink-0 whitespace-nowrap pt-1 font-mono text-[12px] text-[var(--muted)]">
            {i18nT('pages.mcpManagement.stubbed_of_total', {
              stubbed: stubCount,
              total: eligibleCount,
            })}
          </span>
          <Switch
            on={!!status?.enabled}
            disabled={
              busy || statusQ.isLoading || (!status?.enabled && !canEnableSharing)
            }
            label={i18nT('pages.mcpManagement.sharing_label')}
            describedBy="mcp-sharing-desc"
            onClick={() => {
              setError(null)
              // Turning sharing ON changes the topology of every stubbed server
              // at once, so it asks first. Turning it OFF only ever narrows,
              // and a confirm on the safe direction trains people to click
              // through the dangerous one.
              if (!status?.enabled) setConfirmSharing(true)
              else setSharing.mutate(false)
            }}
          />
        </div>
      </section>

      {/* Global: pre-resolve npm-launcher servers so launches skip resolution. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-5 py-4">
        <div className="flex items-start gap-5">
          <div className="flex-1">
            <div className="text-[15px] font-semibold text-[var(--text)]">
              {i18nT('pages.mcpManagement.resolve_label')}
            </div>
            <p
              id="mcp-resolve-desc"
              className="mt-1.5 max-w-[64ch] text-[13px] leading-relaxed text-[var(--muted)]"
            >
              {i18nT('pages.mcpManagement.resolve_description')}
            </p>
            {/* A control that can do nothing has to say so BEFORE it is pressed.
                Pre-resolving acts on the routed set, and routing is what a stub
                creates, so with nothing stubbed there is nothing to resolve --
                the same gate the sharing card states in plain text above. */}
            {stubCount === 0 && (
              <p className="mt-2 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.resolve_needs_a_stub')}
              </p>
            )}
            {resolveNotice && (
              <p className="mt-2 text-[12.5px] text-[var(--text)]" role="status">
                {resolveNotice}
              </p>
            )}
          </div>
          <button
            type="button"
            aria-describedby="mcp-resolve-desc"
            disabled={resolveRefresh.isPending}
            onClick={() => {
              setError(null)
              setResolveNotice(null)
              resolveRefresh.mutate()
            }}
            className="shrink-0 rounded-lg border border-[var(--border)] px-3 py-1.5 text-[13px] text-[var(--text)] transition-colors hover:bg-[var(--hover)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {resolveRefresh.isPending
              ? i18nT('pages.mcpManagement.resolve_updating')
              : i18nT('pages.mcpManagement.resolve_update_now')}
          </button>
        </div>
      </section>

      {/* Per server: interpose the stub. */}
      <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]">
        {/* Both switches on this page are next-chat scoped: the apply path
            rebuilds the provider factory and drains the warm pool, but
            deliberately does not touch live sessions — a running session has
            already sent session/new and cannot be retrofitted. Say so, because
            the row toggle is the control people use routinely and a silently
            partial apply reads as a broken switch. */}
        <p className="border-b border-[var(--border)] px-4 py-2.5 text-[12.5px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.open_sessions_note')}
        </p>
        {/* Bulk action. Lives here rather than on the assessment view because
            that view states it changes nothing — and this is the page that owns
            the switches it drives. */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-[var(--border)] px-4 py-2.5">
          {/* The shared primitive, not a hand-styled button: it carries the
              dashboard's focus, active and disabled behaviour, which a local
              class string only approximates. */}
          <Btn
            type="button"
            onClick={() => {
              setError(null)
              setNotice(null)
              setRestartNotice(null)
              enableAll.mutate()
            }}
            disabled={
              busy
              || enableAll.isPending
              || serversQ.isLoading
              || (stubbableNames.length === 0 && unmeasuredCount === 0)
            }
          >
            {i18nT('pages.mcpManagement.enable_all')}
          </Btn>
          <span className="text-[12.5px] text-[var(--muted)]">
            {/* Acknowledge the press immediately and then track the pass. Gating
                only on a live reading left the line showing the generic hint for
                the first poll interval, so a click that starts a minutes-long
                pass looked like it did nothing; gating only on the click and
                hardcoding zero left it frozen for the whole pass. Take the real
                numbers the moment a reading lands and the pending count until
                then. */}
            {enableAll.isPending && (measureProgress?.running || unmeasuredCount > 0)
              ? i18nT('pages.mcpManagement.assessment.measure_running', {
                  done: measureProgress?.running ? measureProgress.done : 0,
                  total: measureProgress?.running ? measureProgress.total : unmeasuredCount,
                })
              : notice || i18nT('pages.mcpManagement.enable_all_hint')}
          </span>
        </div>
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="w-[34%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_server')}
              </th>
              <th className="w-[34%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_used_by')}
              </th>
              {/* Column widths are unchanged from before this PR. An earlier
                  revision widened STATE to hold a full cause sentence; that
                  sentence now lives once, in the legend, so the table needs no
                  extra room and no data-dependent reflow. */}
              <th className="w-[16%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_state')}
              </th>
              <th className="w-[16%] px-4 pb-2.5 pt-3.5 text-right text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_stub')}
              </th>
            </tr>
          </thead>
          <tbody>
            {servers.map(s => {
              // `rowState` is the ONE derivation of this row's state, and the
              // colour and the reason line read its answer rather than recomputing
              // it. Deriving them separately is what produced this defect -- the
              // text can be corrected while the colour still says `shared`, and an
              // operator scanning the column by colour reads the old answer every
              // visit -- so a second spelling of "is shared" here would rebuild the
              // divergence one state later.
              const state = rowState(s, !!status?.enabled)
              const shared = state === 'shared'
              const directEnv = state === 'direct_env'
              // The assessment view's warning sends the operator here, so the
              // rows it counted have to be findable without memorising names.
              const flagged = sharedWithoutSupport(s, !!status?.enabled)
              return (
                <tr key={s.name} className="border-t border-[var(--border)]">
                  <td
                    className={[
                      'px-4 py-3 font-mono text-[13px]',
                      s.stub ? 'text-[var(--text)]' : 'text-[var(--muted)]',
                    ].join(' ')}
                  >
                    {s.name}
                  </td>
                  <td className="px-4 py-3 text-[12.5px] text-[var(--muted)]">
                    {s.agents.join(', ')}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={[
                        // A state is a term, not a sentence: breaking `not shared
                        // (env)` across two ragged lines reads as a broken badge
                        // beside the single-line `shared` and `direct` pills, and
                        // every shipped locale is longer than the English.
                        'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 font-mono text-[11px]',
                        flagged
                          ? 'border border-[var(--danger)] text-[var(--danger)]'
                          : shared
                          ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
                          : 'border border-[var(--border)] text-[var(--muted)]',
                      ].join(' ')}
                    >
                      {flagged && <AlertTriangle size={11} aria-hidden="true" />}
                      {i18nT(STATE_LABEL_KEY[state])}
                    </span>
                    {/* Only on a row the operator opted in, and it carries ONLY what
                        the legend cannot: that the opt-in on the lit toggle beside it
                        survives. The cause is defined once, in the legend -- keeping
                        a copy of it here made the row and the legend two catalogs
                        that must agree about one state in 13 locales, which is a
                        drift surface for the very defect this change removes.
                        What no legend can do is reach the operator BEFORE they
                        resolve the contradiction themselves: STUB is lit, the state
                        says no stub, and switching the toggle off throws away an
                        opt-in that the rewriter will honour as soon as the env
                        obstacle clears. On a row that was never opted in there is
                        nothing to have been declined. */}
                    {directEnv && (
                      <span className="mt-1 block text-[11px] leading-snug text-[var(--muted)]">
                        {i18nT('pages.mcpManagement.state_direct_env_reason')}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Switch
                      on={s.stub}
                      disabled={!s.can_stub || busy || (!s.stub && !supported)}
                      label={i18nT('pages.mcpManagement.stub_aria', { name: s.name })}
                      onClick={() => {
                        setError(null)
                        setRestartNotice(null)
                        setStub.mutate({ name: s.name, stub: !s.stub })
                      }}
                    />
                  </td>
                </tr>
              )
            })}
            {serversQ.isError && (
              <tr className="border-t border-[var(--border)]">
                <td colSpan={4} className="px-4 py-6 text-center text-[13px] text-[var(--danger)]">
                  {/* Distinct from the empty state on purpose: a failed request
                      knows nothing about the operator's servers, and saying
                      "none are configured" would be a claim we cannot make. */}
                  {i18nT('pages.mcpManagement.servers_failed')}
                </td>
              </tr>
            )}
            {servers.length === 0 && !serversQ.isLoading && !serversQ.isError && (
              <tr className="border-t border-[var(--border)]">
                <td colSpan={4} className="px-4 py-6 text-center text-[13px] text-[var(--muted)]">
                  {i18nT('pages.mcpManagement.no_servers')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="border-t border-[var(--border)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--muted)]">
          {i18nT('pages.mcpManagement.legend')}
        </div>
        {/* Its OWN block, not a second string inside the paragraph above. Two
            catalog values sharing one text run is a reordering defect in any
            language whose clause order differs -- the repo's render gate calls it
            `fragment/multi-unit` and AGENTS.md says merge, not join. Kept separate
            rather than merged INTO the paragraph because that paragraph must stay
            byte-identical to its base value in 13 locales; as its own block the new
            term is also findable by scanning. */}
        <div className="border-t border-[var(--border)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--muted)]">
          {i18nT('pages.mcpManagement.state_direct_env_legend')}
        </div>
      </section>
      </TabsContent>

      <ConfirmSharing
        open={confirmSharing}
        stubCount={stubCount}
        unsupported={wouldBeUnsupported}
        busy={setSharing.isPending}
        onCancel={() => setConfirmSharing(false)}
        onConfirm={() => {
          setConfirmSharing(false)
          setSharing.mutate(true)
        }}
      />
    </Tabs>
  )
}

function ConfirmSharing({
  open,
  stubCount,
  unsupported,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  stubCount: number
  unsupported: string[]
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  // Built on the repo's Radix Dialog rather than a bare `<div role="dialog">`:
  // that primitive owns the focus trap, initial focus, Escape-to-dismiss and
  // focus return. Hand-rolling the markup looked identical but let a keyboard
  // user Tab into the page behind the overlay and gave them no way out.
  return (
    <Dialog
      open={open}
      onOpenChange={next => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent maxWidth={520}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.mcpManagement.confirm_title')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <DialogDescription className="text-text">
            {i18nT('pages.mcpManagement.confirm_lede', { count: stubCount })}
          </DialogDescription>
          {/* The verdict belongs at the decision point, not only after the fact:
              an operator who never opens the assessment view would otherwise
              reach the exact state this page exists to warn about. */}
          {unsupported.length > 0 && (
            <div className="mt-2 flex items-start gap-2 text-[13.5px] text-[var(--danger)]">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
              <div>
                <p>
                  {i18nT('pages.mcpManagement.confirm_unsupported', { count: unsupported.length })}
                </p>
                {/* Naming them is the difference between a number the operator has
                    to go hunting for and one they can act on here. Listed in full:
                    the count is on the line above, so a cap would only raise the
                    question of what it hid. Data, not prose, so no catalog entry. */}
                <p className="mt-0.5 font-mono text-[12px]">{unsupported.join(', ')}</p>
              </div>
            </div>
          )}
          <ul className="mt-2.5 list-disc space-y-1.5 pl-5 text-[13.5px] leading-relaxed text-muted">
            <li>{i18nT('pages.mcpManagement.confirm_stateful')}</li>
            <li>{i18nT('pages.mcpManagement.confirm_restart')}</li>
            <li>{i18nT('pages.mcpManagement.confirm_reversible')}</li>
          </ul>
        </DialogBody>
        <DialogFooter className="justify-between">
          <a
            href={DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline"
          >
            {i18nT('pages.mcpManagement.learn_more_docs')}
            <ExternalLink size={12} />
          </a>
          <div className="flex gap-2.5">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border px-3.5 py-2 text-[13.5px] text-text"
            >
              {i18nT('pages.mcpManagement.cancel')}
            </button>
            <button
              type="button"
              autoFocus
              disabled={busy}
              onClick={onConfirm}
              className="rounded-md bg-accent px-3.5 py-2 text-[13.5px] font-medium text-accent-fg disabled:opacity-60"
            >
              {i18nT('pages.mcpManagement.confirm_turn_on')}
            </button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default McpManagement
