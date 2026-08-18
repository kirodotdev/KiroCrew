import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, ListChecks, Server as ServerIcon } from 'lucide-react'
import {
  api,
  type McpManagedServer,
  type McpShareRecommendation,
  type McpShareReason,
} from '../../api/client'
import UnderlineTabs, { type UnderlineTab } from '../../components/UnderlineTabs'
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
  no_objection: 'pages.mcpManagement.assessment.strength_no_objection',
  unknown: 'pages.mcpManagement.assessment.strength_unknown',
}

/** One key per reason code the verdict engine emits. */
const REASON_LABEL_KEY: Record<string, string> = {
  observed_hazard: 'pages.mcpManagement.assessment.reason_observed_hazard',
  not_stdio: 'pages.mcpManagement.assessment.reason_not_stdio',
  first_party_session_scoped: 'pages.mcpManagement.assessment.reason_first_party',
  rotating_secret_env: 'pages.mcpManagement.assessment.reason_rotating_secret_env',
  not_probed: 'pages.mcpManagement.assessment.reason_not_probed',
  per_client_capability: 'pages.mcpManagement.assessment.reason_per_client_capability',
  caller_sensitive_initialize: 'pages.mcpManagement.assessment.reason_caller_sensitive',
  declares_caller_identity: 'pages.mcpManagement.assessment.reason_declares_caller_identity',
  all_tools_read_only: 'pages.mcpManagement.assessment.reason_all_tools_read_only',
  preflight_passed: 'pages.mcpManagement.assessment.reason_preflight_passed',
  preflight_not_run: 'pages.mcpManagement.assessment.reason_preflight_not_run',
  no_objection_found: 'pages.mcpManagement.assessment.reason_no_objection_found',
  no_tool_annotations: 'pages.mcpManagement.assessment.reason_no_tool_annotations',
  no_tools_listed: 'pages.mcpManagement.assessment.reason_no_tools_listed',
}

/**
 * What the server is running as, as ONE function used by both sub-views.
 *
 * The assessment table has to name the current state to be able to show it
 * disagreeing with the verdict, and two copies of this mapping would be free to
 * drift into saying different things about the same row.
 */
function stateLabelKey(s: McpManagedServer, sharingOn: boolean): string {
  if (!s.can_stub) return 'pages.mcpManagement.state_no_stub'
  if (s.stub && sharingOn) return 'pages.mcpManagement.state_shared'
  if (s.stub) return 'pages.mcpManagement.state_stub'
  return 'pages.mcpManagement.state_direct'
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
 */
function sharedWithoutSupport(s: McpManagedServer, sharingOn: boolean): boolean {
  if (!(s.stub && sharingOn)) return false
  const rec = s.recommendation
  if (!rec) return false
  return CONTRARY_STRENGTHS.has(rec.strength)
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
            'inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[11px]',
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
          {i18nT(stateLabelKey(server, sharingOn))}
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
function AssessmentView({
  servers,
  sharingOn,
  loading,
  isError,
  onOpenServers,
  unsupportedCount,
}: {
  servers: McpManagedServer[]
  sharingOn: boolean
  loading: boolean
  isError: boolean
  onOpenServers: () => void
  unsupportedCount: number
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
      </section>
    </div>
  )
}

export function McpManagement() {
  const qc = useQueryClient()
  const [confirmSharing, setConfirmSharing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const statusQ = useQuery<GatewayStatus>({
    queryKey: ['mcpGatewayStatus'],
    queryFn: () => api.mcpGatewayStatus(),
  })
  const serversQ = useQuery<{ servers: McpManagedServer[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers(),
  })

  const status = statusQ.data
  const servers = serversQ.data?.servers ?? []
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
    // wanted state: a failed start still answers 200 with `applied: false`.
    // Reporting that as success would draw a live-looking switch over routing
    // that never came up.
    onSuccess: res => {
      invalidate()
      if (res && res.applied === false) {
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

  // Local state, not a URL param. The sibling in-pane tab rails in this repo
  // (ConnectionsPage, knowledge) hold it the same way, this pane is already
  // addressed by the Developer page's own `?tab=`, and a second param would need
  // to coexist with it for a read-only view nobody deep-links to. The shared
  // component is still what draws the rail, so the keyboard and aria behaviour
  // come along for free.
  const [view, setView] = useState<McpView>('servers')
  // A function, not a module constant, so the labels re-translate on a language
  // switch instead of freezing at first import.
  const views: Array<UnderlineTab<McpView>> = [
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
    <div className="space-y-4">
      <UnderlineTabs<McpView>
        tabs={views}
        value={view}
        onChange={setView}
        ariaLabel={i18nT('pages.mcpManagement.views_aria')}
        layoutId="mcp-management-view"
      />

      {view === 'assessment' ? (
        <AssessmentView
          servers={servers}
          sharingOn={!!status?.enabled}
          loading={serversQ.isLoading}
          isError={serversQ.isError}
          onOpenServers={() => setView('servers')}
          unsupportedCount={unsupportedCount}
        />
      ) : (
        <>
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
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="w-[34%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_server')}
              </th>
              <th className="w-[34%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_used_by')}
              </th>
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
              const shared = s.stub && !!status?.enabled
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
                        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-mono text-[11px]',
                        flagged
                          ? 'border border-[var(--danger)] text-[var(--danger)]'
                          : shared
                          ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
                          : 'border border-[var(--border)] text-[var(--muted)]',
                      ].join(' ')}
                    >
                      {flagged && <AlertTriangle size={11} aria-hidden="true" />}
                      {i18nT(stateLabelKey(s, !!status?.enabled))}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Switch
                      on={s.stub}
                      disabled={!s.can_stub || busy || (!s.stub && !supported)}
                      label={i18nT('pages.mcpManagement.stub_aria', { name: s.name })}
                      onClick={() => {
                        setError(null)
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
      </section>
        </>
      )}

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
    </div>
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
