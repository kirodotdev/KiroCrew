/**
 * The MCP page's read-only Sharing assessment view: one row per server with its
 * verdict, the evidence behind it and what it is running as now, and the Measure
 * control that fills in unmeasured rows. It changes no server.
 */
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Gauge } from 'lucide-react'
import {
  api,
  type McpManagedServer,
  type McpShareRecommendation,
  type McpShareReason,
} from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { splitOnPlaceholder } from '../../lib/splitOnPlaceholder'
import { MCP_SERVERS_HREF } from '../connections/useConnectionsSubTab'
import { i18nT } from '../../i18n/t'
import {
  type LaunchRefusal,
  stateLabelKey,
  STRENGTH_LABEL_KEY,
  REASON_LABEL_KEY,
  rowState,
  sharedWithoutSupport,
} from './mcpManagementModel'

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
    <li className="min-w-0 leading-relaxed">
      {/* An unknown code still has to say something, and its raw code is more
          use to whoever has to look it up than a blank cell. */}
      <span>{key ? i18nT(key) : reason.code}</span>
      {reason.detail && (
        <span className="mt-0.5 block w-fit max-w-full break-words rounded border border-[var(--border)] px-1 font-mono text-[11px] text-[var(--text)] [overflow-wrap:anywhere]">
          {reason.detail}
        </span>
      )}
    </li>
  )
}

function AssessmentRow({
  server,
  sharingOn,
  refused,
}: {
  server: McpManagedServer
  sharingOn: boolean
  refused?: Record<string, LaunchRefusal>
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
  const unsupported = sharedWithoutSupport(server, sharingOn, refused)
  return (
    <tr className="grid min-w-0 grid-cols-1 gap-3 border-t border-[var(--border)] px-3 py-3 md:table-row md:px-0 md:py-0">
      <td className="min-w-0 break-words p-0 align-top font-mono text-[13px] text-[var(--text)] [overflow-wrap:anywhere] md:table-cell md:px-4 md:py-3">
        {server.name}
      </td>
      <td className="min-w-0 p-0 align-top md:table-cell md:px-4 md:py-3">
        <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
          {i18nT('pages.mcpManagement.assessment.col_assessment')}
        </span>
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
      <td className="min-w-0 p-0 align-top text-[12.5px] text-[var(--muted)] [overflow-wrap:anywhere] md:table-cell md:px-4 md:py-3">
        <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
          {i18nT('pages.mcpManagement.assessment.col_evidence')}
        </span>
        {reasons.length > 0 ? (
          <ul className="min-w-0 list-none space-y-0.5">
            {reasons.map((r, i) => (
              <ReasonLine key={`${r.code}-${i}`} reason={r} />
            ))}
          </ul>
        ) : (
          <span aria-hidden="true">{'\u2014'}</span>
        )}
      </td>
      <td className="min-w-0 p-0 align-top text-left md:table-cell md:px-4 md:py-3 md:text-right">
        <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
          {i18nT('pages.mcpManagement.assessment.col_running_as')}
        </span>
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
          {i18nT(stateLabelKey(rowState(server, sharingOn, refused), refused?.[server.name]))}
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
    <div className="flex flex-col gap-2">
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
      </div>
      {/* Failures sit UNDER the Measure row, not beside the button: each notice
          carries a hand-off link, and a link in that row is a third action
          (max-two-buttons-per-row). */}
      {/* A pass that stopped early is reported here rather than only in the log:
          the operator is watching this readout and would otherwise read a short
          pass as a completed one. One notice for both causes -- a died pass and a
          start that never got going read the same string, and two copies of it
          side by side said nothing the first did not. */}
      <ErrorNotice
        variant="inline"
        message={failed || start.isError ? i18nT('pages.mcpManagement.assessment.measure_failed') : null}
        askAgent
      />
      {/* A failed progress read is not a failed pass: the pass may be running
          fine behind an endpoint this tab cannot reach. Left silent, the readout
          simply froze, which reads as "nothing is happening". */}
      <ErrorNotice
        variant="inline"
        message={progress.isError ? i18nT('pages.mcpManagement.assessment.measure_progress_failed') : null}
        askAgent
      />
    </div>
  )
}

export function AssessmentView({
  servers,
  sharingOn,
  refused,
  loading,
  isError,
  onOpenServers,
  unsupportedCount,
  unmeasuredCount,
}: {
  servers: McpManagedServer[]
  sharingOn: boolean
  refused?: Record<string, LaunchRefusal>
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
                to={MCP_SERVERS_HREF}
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
          className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-start gap-x-2 gap-y-2 rounded-lg border border-[var(--danger)] bg-[var(--danger-subtle,transparent)] px-3.5 py-2.5 text-[13px] text-[var(--text)] md:grid-cols-[auto_minmax(0,1fr)_auto]"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-[var(--danger)]" />
          <span className="min-w-0">
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
            className="col-start-2 justify-self-start rounded-md border border-[var(--border)] px-2 py-0.5 text-[12.5px] text-[var(--text)] hover:border-[var(--accent)] md:col-start-3 md:row-start-1"
          >
            {i18nT('pages.mcpManagement.assessment.open_servers')}
          </button>
        </div>
      )}

      <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]">
        <table className="block w-full border-collapse md:table">
          <thead className="hidden md:table-header-group">
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
          <tbody className="block md:table-row-group">
            {servers.map(s => (
              <AssessmentRow key={s.name} server={s} sharingOn={sharingOn} refused={refused} />
            ))}
            {isError && (
              <tr className="block border-t border-[var(--border)] md:table-row">
                <td colSpan={4} className="block px-4 py-4 md:table-cell">
                  <ErrorNotice message={i18nT('pages.mcpManagement.servers_failed')} askAgent />
                </td>
              </tr>
            )}
            {servers.length === 0 && !loading && !isError && (
              <tr className="block border-t border-[var(--border)] md:table-row">
                <td colSpan={4} className="block px-4 py-6 text-center text-[13px] text-[var(--muted)] md:table-cell">
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
