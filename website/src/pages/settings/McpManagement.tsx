import { Fragment, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, ListChecks, RefreshCw, Server as ServerIcon } from 'lucide-react'
import {
  api,
  ApiError,
  type McpManagedServer,
  type McpMeasureProgress,
} from '../../api/client'
import { Tabs, TabsContent, TabsCount, TabsList, TabsTrigger, type TabItem } from '../../components/ui/tabs'
import { TABS_RAIL_ROW_CLASS } from '../../components/ui/tabsPill'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog'
import { splitOnPlaceholder } from '../../lib/splitOnPlaceholder'
import { i18nT } from '../../i18n/t'
import {
  DOCS_URL,
  type GatewayStatus,
  type BulkCandidate,
  type BulkIssue,
  launchIsApprovable,
  stateLabelKey,
  stubErrorKey,
  launchChangedSinceDisplay,
  launchPreviewErrorKey,
  type McpView,
  rowState,
  CONTRARY_STRENGTHS,
  sharedWithoutSupport,
  waitForMeasurePass,
} from './mcpManagementModel'
import { RefusalPanel } from './McpManagementLaunchReview'
import { AssessmentView } from './McpManagementAssessment'

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

export function McpManagement() {
  const qc = useQueryClient()
  const [confirmSharing, setConfirmSharing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [bulkReview, setBulkReview] = useState<BulkCandidate[] | null>(null)
  const [bulkIssues, setBulkIssues] = useState<BulkIssue[]>([])
  // Pending-effect information, kept separate from `error` so a stub change that
  // needs a restart is not painted as a failure. Distinct from the enable-all
  // `notice` below, which is that control's own inline hint rather than a
  // page-level statement about the gateway.
  const [restartNotice, setRestartNotice] = useState<string | null>(null)
  // A third state rather than a reuse of either sibling above: `notice` reports a
  // stub-set change and `restartNotice` reports a pending restart, and one banner
  // shared by unrelated actions would let either overwrite the other's result.
  const [resolveNotice, setResolveNotice] = useState<string | null>(null)
  // The failed-install outcomes of that same pass, kept apart from
  // `resolveNotice` so a per-package install failure renders as an error (with
  // the agent hand-off) while "already fresh" and "already running" stay plain
  // status. One variable carrying both made every outcome look like a status.
  const [resolveError, setResolveError] = useState<string | null>(null)

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
  // The header counts what the STUB column SHOWS. A row waiting for re-approval
  // keeps its opt-in in config while its switch reads off, so a count of `s.stub`
  // claims a server the column beneath it denies, and the operator is left to work
  // out which of the two numbers is real. The rows that are waiting are a clause of
  // their own rather than a silent omission -- they are the ones with work to do.
  const switchedOnCount = useMemo(
    () =>
      servers.filter(
        s =>
          s.stub
          && rowState(s, !!status?.enabled, status?.launch_refused) !== 'needs_reapproval',
      ).length,
    [servers, status],
  )
  const waitingCount = useMemo(
    () =>
      servers.filter(
        s => rowState(s, !!status?.enabled, status?.launch_refused) === 'needs_reapproval',
      ).length,
    [servers, status],
  )
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

  // The approve button of each refused row's panel, by server name, so the row's
  // switch can move the keyboard to the panel it already shows.
  const approveButtons = useRef<Record<string, HTMLButtonElement | null>>({})
  // Reading the launch is the first half of turning a stub ON, and the only gate:
  // the page shows no command, so a readable launch is approved straight away and
  // an unapprovable one turns nothing on and says so. A read failure (a separate
  // table from the write's) likewise reports that nothing was turned on.
  const previewLaunch = useMutation({
    mutationFn: (name: string) => api.mcpGatewayLaunchPreview(name),
    onSuccess: (preview, name) => {
      if (launchIsApprovable(preview)) {
        setStub.mutate({ name, stub: true, expectedLaunch: preview.expected_launch as string })
      } else {
        setError(i18nT('pages.mcpManagement.approval_not_approvable_here'))
      }
    },
    onError: (error, name) => {
      setError(i18nT(launchPreviewErrorKey(error), { name }))
    },
  })

  const setStub = useMutation({
    mutationFn: ({ name, stub, expectedLaunch }: { name: string; stub: boolean; expectedLaunch?: string }) =>
      api.mcpGatewaySetStub(name, stub, expectedLaunch),
    // A 200 means the config was persisted, NOT that the broker reached the
    // wanted state. A stub change is never applied in place -- the daemon's
    // routing is built with the agent-spec rewrite at startup -- so the normal
    // outcome is `restart_required`, which is pending information rather than a
    // failure. `applied: false` with no restart hint is the real fault case:
    // the gateway never wired the apply callback, so nothing was recorded.
    onSuccess: (res, vars) => {
      invalidate()
      // The question the panel asked has been answered, so it has nothing left to
      // show. Closed here rather than at the click: closing optimistically would
      // drop the row while the write that names it can still be refused.
      if (vars.stub) {
        setBulkIssues(issues => issues.filter(issue => issue.name !== vars.name))
        setNotice(null)
      }
      if (res && res.restart_required) {
        setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      } else if (res && res.applied === false) {
        setError(i18nT('pages.mcpManagement.stub_not_live'))
      }
    },
    onError: (error, vars) => {
      invalidate()
      setError(i18nT(stubErrorKey(error), { name: vars.name }))
    },
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
        setResolveError(
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

  const busy = setStub.isPending || setSharing.isPending || previewLaunch.isPending
  // An unsupported platform must never TRAP an operator in a state they cannot
  // leave: a config carried over from another machine can arrive with sharing on
  // or servers stubbed, so turning things OFF stays available and only turning
  // them ON is blocked. Enabling sharing over an empty stub set is blocked for
  // the same reason it no longer exists as a state: it would do nothing.
  // Shared by the tab badge, the confirm dialog and the assessment banner, so
  // the three can never disagree about how many rows are flagged.
  const unsupportedCount = useMemo(
    () =>
      servers.filter(s => sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)).length,
    [servers, status?.enabled, status?.launch_refused],
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
  // The candidates the bulk action has read and is asking about live in
  // `bulkReview`; `null` is "not asking" and an empty list never opens the dialog.
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
    mutationFn: async (): Promise<{ pending: boolean; review: BulkCandidate[] }> => {
      // Measure the unmeasured FIRST. An unmeasured row is never eligible, so
      // skipping this step would make the button quietly ignore exactly the
      // servers just installed — the ones the operator is most likely to be
      // here for. The pass is uncapped and can run for minutes; it reports
      // progress, and if it is still going when the wait runs out nothing is
      // stubbed and the operator is told to come back, which is better than
      // acting on a half-measured fleet.
      if (unmeasuredCount > 0) {
        await api.mcpMeasureStart()
        if (!(await waitForMeasurePass(qc))) return { pending: true, review: [] }
      }
      // Rows are re-read rather than reused from the render this click came from,
      // because the measurement pass above changes which servers are candidates at
      // all. Called through `api` DIRECTLY, not `qc.fetchQuery`: this app's shared
      // QueryClient sets `staleTime: Infinity` (freshness comes from WebSocket
      // invalidation, not from age), so a cache-backed read would resolve from that
      // very render and the re-read would be a decoration.
      const fresh = await api.mcpGatewayServers()
      const rows = fresh?.servers ?? []
      const candidates = rows.filter(s => s.can_stub && !s.stub).map(s => s.name)
      if (candidates.length === 0) return { pending: false, review: [] }
      // Read what each candidate would run before writing anything: each write
      // below carries that launch's `expected_launch`, and this read is where it
      // comes from. Eligibility is still the server's to decide -- each write
      // requests that decision inside the lock hold that performs it. The confirm
      // lists the candidates by name and shows no command.
      return {
        pending: false,
        review: await Promise.all(
          candidates.map(async name => {
            try {
              return { name, preview: await api.mcpGatewayLaunchPreview(name) }
            } catch (error) {
              return { name, error }
            }
          }),
        ),
      }
    },
    onSuccess: r => {
      invalidate()
      if (r.pending) {
        setNotice(i18nT('pages.mcpManagement.enable_all_still_measuring'))
        return
      }
      if (r.review.length === 0) {
        setNotice(i18nT('pages.mcpManagement.enable_all_review_nothing'))
        return
      }
      setBulkReview(r.review)
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.stub_failed'))
    },
  })

  // The confirm half: one request per server, sent one after another.
  //
  // Not a batch, and not in parallel. Each write carries `expected_launch` -- the
  // identity of the launch read for that server -- so one body cannot
  // stand for the whole set, and the endpoint refuses a batch stub=true for that
  // reason. Serial because these are config writes behind one lock: firing them
  // together only queues them somewhere less visible.
  //
  // Three outcomes, counted apart, because they need different answers from the
  // operator: written, left alone (nothing to approve, or the server declined it),
  // and refused because the command moved after it was read — the last is the one
  // worth looking at again.
  const enableAllApprove = useMutation({
    mutationFn: async (items: BulkCandidate[]) => {
      let enabled = 0
      let skipped = 0
      const changed: BulkIssue[] = []
      const failed: BulkIssue[] = []
      let restart = false
      for (const item of items) {
        const preview = item.preview
        if (!preview) {
          if ('error' in item) {
            failed.push({ name: item.name, error: item.error, kind: 'preview_failed' })
          } else {
            skipped++
          }
          continue
        }
        if (!launchIsApprovable(preview)) {
          skipped++
          continue
        }
        try {
          const res = await api.mcpGatewaySetStub(
            item.name,
            true,
            preview.expected_launch as string,
            true,
          )
          if (res.stubbed?.includes(item.name)) {
            enabled++
          } else if (res.skipped?.some(entry => entry.name === item.name)) {
            skipped++
          } else {
            failed.push({
              name: item.name,
              error: new Error('Stub eligibility response did not identify an outcome'),
              kind: 'write_failed',
            })
          }
          if (res.restart_required) restart = true
        } catch (error) {
          if (launchChangedSinceDisplay(error)) {
            changed.push({ name: item.name, error, kind: 'changed' })
          } else {
            failed.push({ name: item.name, error, kind: 'write_failed' })
          }
        }
      }
      return { enabled, skipped, changed, failed, restart }
    },
    onSuccess: r => {
      invalidate()
      setBulkReview(null)
      setBulkIssues([...r.changed, ...r.failed])
      setNotice(
        [
          i18nT('pages.mcpManagement.enable_all_review_result_shared', { count: r.enabled }),
          ...(r.skipped > 0
            ? [i18nT('pages.mcpManagement.enable_all_review_result_left', { count: r.skipped })]
            : []),
          ...(r.changed.length > 0
            ? [i18nT('pages.mcpManagement.enable_all_review_result_changed', { count: r.changed.length })]
            : []),
          ...(r.failed.length > 0
            ? [i18nT('pages.mcpManagement.enable_all_review_result_failed', { count: r.failed.length })]
            : []),
        ].join(' '),
      )
      // A stub change is never applied in place, so `restart_required` is the
      // normal answer and must not read as a fault.
      if (r.restart) {
        setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      }
    },
    onError: () => {
      invalidate()
      setBulkReview(null)
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
      className="min-w-0 space-y-4"
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

      {/* `supported` defaults to true and `enabled` to off, so a failed status
          read used to paint a healthy, switchable card over a state this tab
          knows nothing about. Above both views because both derive from it. */}
      <ErrorNotice
        message={statusQ.isError ? i18nT('pages.mcpManagement.status_failed') : null}
        askAgent
      />

      <TabsContent value="assessment" className="min-w-0">
        <AssessmentView
          servers={servers}
          sharingOn={!!status?.enabled}
          refused={status?.launch_refused}
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
      <TabsContent value="servers" className="min-w-0 space-y-4">
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

      {/* Hand-off is safe here: this page is switches only, every input is
          already persisted before any of these errors can show. */}
      <ErrorNotice message={error} askAgent onDismiss={() => setError(null)} />

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
        <div className="flex min-w-0 flex-col gap-3 md:flex-row md:items-start md:gap-5">
          <div className="min-w-0 flex-1">
            <div className="text-[15px] font-semibold text-[var(--text)]">
              {i18nT('pages.mcpManagement.sharing_label')}
            </div>
            <p
              id="mcp-sharing-desc"
              className="mt-1.5 max-w-[64ch] break-words text-[13px] leading-relaxed text-[var(--muted)] [overflow-wrap:anywhere]"
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
          <div className="flex min-w-0 items-start justify-between gap-3 md:shrink-0 md:gap-5">
            <span className="min-w-0 break-words font-mono text-[12px] text-[var(--muted)] [overflow-wrap:anywhere] md:whitespace-nowrap md:pt-1">
              {i18nT('pages.mcpManagement.stubbed_of_total', {
                stubbed: switchedOnCount,
                total: eligibleCount,
              })}
              {waitingCount > 0
                && ` · ${i18nT('pages.mcpManagement.stub_count_waiting', { count: waitingCount })}`}
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
        </div>
      </section>

      {/* Global: pre-resolve npm-launcher servers so launches skip resolution. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-5 py-4">
        <div className="flex min-w-0 flex-col gap-3 md:flex-row md:items-start md:gap-5">
          <div className="min-w-0 flex-1">
            <div className="text-[15px] font-semibold text-[var(--text)]">
              {i18nT('pages.mcpManagement.resolve_label')}
            </div>
            <p
              id="mcp-resolve-desc"
              className="mt-1.5 max-w-[64ch] break-words text-[13px] leading-relaxed text-[var(--muted)] [overflow-wrap:anywhere]"
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
            <ErrorNotice variant="inline" className="mt-2" message={resolveError} askAgent />
          </div>
          <button
            type="button"
            aria-describedby="mcp-resolve-desc"
            disabled={resolveRefresh.isPending}
            onClick={() => {
              setError(null)
              setResolveNotice(null)
              setResolveError(null)
              resolveRefresh.mutate()
            }}
            className="self-start shrink-0 rounded-lg border border-[var(--border)] px-3 py-1.5 text-[13px] text-[var(--text)] transition-colors hover:bg-[var(--hover)] disabled:cursor-not-allowed disabled:opacity-60"
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
              setBulkIssues([])
              setRestartNotice(null)
              enableAll.mutate()
            }}
            disabled={
              busy
              || enableAll.isPending
              || enableAllApprove.isPending
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
              : notice}
          </span>
          {bulkIssues.length > 0 && (
            <div className="flex basis-full flex-col gap-2 pt-1">
              {bulkIssues.some(issue => issue.kind === 'changed') && (
                <p className="text-[12px] text-[var(--muted)]">
                  {i18nT('pages.mcpManagement.enable_all_review_changed_names', {
                    names: bulkIssues
                      .filter(issue => issue.kind === 'changed')
                      .map(issue => issue.name)
                      .join(', '),
                  })}
                </p>
              )}
              {bulkIssues.some(issue => issue.kind !== 'changed') && (
                <p className="text-[12px] text-[var(--muted)]">
                  {i18nT('pages.mcpManagement.enable_all_review_failed_names', {
                    names: bulkIssues
                      .filter(issue => issue.kind !== 'changed')
                      .map(issue => issue.name)
                      .join(', '),
                  })}
                </p>
              )}
              {bulkIssues.map(issue => (
                <div key={`${issue.kind}:${issue.name}`} className="flex flex-wrap items-center gap-2">
                  {/* Every issue reads as an error, including a launch that
                      changed between the review and the write: it is the reason
                      nothing was approved for that server, and a muted line of
                      body text is not how this dashboard says so. The two
                      summary lines above index which rows are which; the notice
                      here is the cause, per row. */}
                  <ErrorNotice
                    variant="inline"
                    message={i18nT(
                      issue.kind === 'preview_failed'
                        ? launchPreviewErrorKey(issue.error)
                        : stubErrorKey(issue.error),
                      { name: issue.name },
                    )}
                    askAgent
                  />
                  <Btn
                    type="button"
                    disabled={previewLaunch.isPending}
                    onClick={() => {
                      setError(null)
                      previewLaunch.mutate(issue.name)
                    }}
                  >
                    {i18nT('pages.mcpManagement.enable_all_review_again', { name: issue.name })}
                  </Btn>
                </div>
              ))}
            </div>
          )}
        </div>
        <table className="block w-full border-collapse md:table">
          <thead className="hidden md:table-header-group">
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
          <tbody className="block md:table-row-group">
            {servers.map(s => {
              // `rowState` is the ONE derivation of this row's state, and the
              // colour and the reason line read its answer rather than recomputing
              // it. Deriving them separately is what produced this defect -- the
              // text can be corrected while the colour still says `shared`, and an
              // operator scanning the column by colour reads the old answer every
              // visit -- so a second spelling of "is shared" here would rebuild the
              // divergence one state later.
              const state = rowState(s, !!status?.enabled, status?.launch_refused)
              const shared = state === 'shared'
              const directEnv = state === 'direct_env'
              const refusal = state === 'needs_reapproval' ? status?.launch_refused?.[s.name] : undefined
              // The assessment view's warning sends the operator here, so the
              // rows it counted have to be findable without memorising names.
              const flagged = sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)
              return (
                <Fragment key={s.name}>
                <tr className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-3 border-t border-[var(--border)] px-3 py-3 md:table-row md:px-0 md:py-0">
                  <td
                    className={[
                      'col-start-1 row-start-1 min-w-0 break-words p-0 font-mono text-[13px] [overflow-wrap:anywhere] md:table-cell md:px-4 md:py-3',
                      s.stub ? 'text-[var(--text)]' : 'text-[var(--muted)]',
                    ].join(' ')}
                  >
                    {s.name}
                  </td>
                  <td className="col-start-1 row-start-2 min-w-0 break-words p-0 text-[12.5px] text-[var(--muted)] [overflow-wrap:anywhere] md:table-cell md:px-4 md:py-3">
                    <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
                      {i18nT('pages.mcpManagement.col_used_by')}
                    </span>
                    {s.agents.join(', ')}
                  </td>
                  <td className="col-start-1 row-start-3 min-w-0 p-0 md:table-cell md:px-4 md:py-3">
                    <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
                      {i18nT('pages.mcpManagement.col_state')}
                    </span>
                    <span
                      className={[
                        // A state is a term, not a sentence: breaking `not shared
                        // (env)` across two ragged lines reads as a broken badge
                        // beside the single-line `shared` and `direct` pills, and
                        // every shipped locale is longer than the English.
                        'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 font-mono text-[11px]',
                        flagged || refusal?.reason === 'changed_needs_reapproval'
                          ? 'border border-[var(--danger)] text-[var(--danger)]'
                          : refusal
                          ? 'border border-[var(--warn)] text-[var(--warn)]'
                          : shared
                          ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
                          : 'border border-[var(--border)] text-[var(--muted)]',
                      ].join(' ')}
                    >
                      {(flagged || refusal) && <AlertTriangle size={11} aria-hidden="true" />}
                      {i18nT(stateLabelKey(state, refusal))}
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
                  <td className="col-start-2 row-start-1 p-0 text-right md:table-cell md:px-4 md:py-3">
                    <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
                      {i18nT('pages.mcpManagement.col_stub')}
                    </span>
                    <Switch
                      on={state !== 'needs_reapproval' && s.stub}
                      disabled={!s.can_stub || busy || (!s.stub && !supported)}
                      label={i18nT('pages.mcpManagement.stub_aria', { name: s.name })}
                      onClick={() => {
                        setError(null)
                        setRestartNotice(null)
                        // OFF is one click: it only ever narrows what runs outside a
                        // session. ON is also one click now: it reads the launch and,
                        // if that launch can be approved, writes the stub straight away
                        // carrying its identity -- no command is shown to review.
                        //
                        // A row waiting for approval already shows its own Approve
                        // button in the sub-row below. The switch moves the keyboard
                        // to that button instead of reading the launch again.
                        //
                        // A row that button cannot approve -- the launch arrived with
                        // no identity to write an approval against -- has no button, so
                        // the click is the operator's only control over the opt-in and
                        // turns the stub off.
                        if (state === 'needs_reapproval') {
                          const approve = approveButtons.current[s.name]
                          if (approve) {
                            approve.scrollIntoView?.({ block: 'nearest' })
                            approve.focus()
                            return
                          }
                          setStub.mutate({ name: s.name, stub: false })
                          return
                        }
                        if (s.stub) {
                          setStub.mutate({ name: s.name, stub: false })
                          return
                        }
                        previewLaunch.mutate(s.name)
                      }}
                    />
                  </td>
                </tr>
                {/* A full-width sub-row for a refused launch: its reason and the
                    Approve button. The pill stays in STATE, because a state IS a
                    column value. */}
                {refusal && (
                  <tr className="block border-t border-[var(--border)] md:table-row md:border-0">
                    <td colSpan={4} className="block px-3 pb-3.5 pt-0 md:table-cell md:px-4">
                      <RefusalPanel
                        server={s}
                        refusal={refusal}
                        busy={busy}
                        approveRef={node => {
                          approveButtons.current[s.name] = node
                        }}
                        onApprove={() => {
                          setError(null)
                          setRestartNotice(null)
                          setStub.mutate({
                            name: s.name,
                            stub: true,
                            expectedLaunch: refusal.expected_launch as string,
                          })
                        }}
                      />
                    </td>
                  </tr>
                )}
                </Fragment>
              )
            })}
            {serversQ.isError && (
              <tr className="block border-t border-[var(--border)] md:table-row">
                <td colSpan={4} className="block px-4 py-4 md:table-cell">
                  {/* Distinct from the empty state on purpose: a failed request
                      knows nothing about the operator's servers, and saying
                      "none are configured" would be a claim we cannot make. */}
                  <ErrorNotice message={i18nT('pages.mcpManagement.servers_failed')} askAgent />
                </td>
              </tr>
            )}
            {servers.length === 0 && !serversQ.isLoading && !serversQ.isError && (
              <tr className="block border-t border-[var(--border)] md:table-row">
                <td colSpan={4} className="block px-4 py-6 text-center text-[13px] text-[var(--muted)] md:table-cell">
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

      <ConfirmEnableAll
        items={bulkReview ?? []}
        busy={enableAllApprove.isPending}
        onCancel={() => setBulkReview(null)}
        onConfirm={() => enableAllApprove.mutate(bulkReview ?? [])}
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

/**
 * The confirm the bulk action opens before it writes anything.
 *
 * "Stub every server the evidence allows" used to send one batch with stub=true;
 * it now lists the candidate servers by name and one confirm sends one request
 * per server. Each write carries that server's expected_launch, so the exact
 * launch the gateway resolved is what the approval covers -- the command itself
 * is not shown, here or anywhere else on this page. A candidate whose launch
 * cannot be approved is skipped by the write.
 */
function ConfirmEnableAll({
  items,
  busy,
  onCancel,
  onConfirm,
}: {
  items: BulkCandidate[]
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const approvable = items.filter(item => item.preview && launchIsApprovable(item.preview))
  return (
    <Dialog
      open={items.length > 0}
      onOpenChange={next => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent maxWidth={640}>
        {/* The dialog shows names only, so it reuses the bulk button's own label
            as its title (and, via Radix, its accessible name) rather than a title
            that promises to show what each server runs. */}
        <DialogHeader>
          <DialogTitle>{i18nT('pages.mcpManagement.enable_all')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          {/* Server names, no command text: the command lists this dialog used to
              show were long, clipped and unread. Each per-server write still
              carries that server's expected_launch. A candidate whose launch could
              not be read, or cannot be approved, still says so here -- otherwise a
              server that will be skipped looks like one that will be shared. */}
          <ul className="mt-1 space-y-2">
            {items.map(item => (
              <li key={item.name}>
                <p className="font-mono text-[12.5px] text-[var(--text)]">{item.name}</p>
                {item.preview ? (
                  !launchIsApprovable(item.preview) && (
                    <p className="mt-1 text-[12.5px] leading-snug text-[var(--muted)]">
                      {i18nT('pages.mcpManagement.approval_not_approvable_here')}
                    </p>
                  )
                ) : 'error' in item ? (
                  <ErrorNotice
                    variant="inline"
                    className="mt-1"
                    message={i18nT(launchPreviewErrorKey(item.error), { name: item.name })}
                    askAgent
                    onHandoff={onCancel}
                  />
                ) : (
                  <p className="mt-1 text-[12.5px] leading-snug text-[var(--muted)]">
                    {i18nT('pages.mcpManagement.enable_all_review_unreadable')}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </DialogBody>
        <DialogFooter>
          <div className="flex gap-2.5">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border px-3.5 py-2 text-[13.5px] text-text"
            >
              {i18nT('pages.mcpManagement.cancel')}
            </button>
            {/* Disabled when the list holds nothing approvable: the button would
                otherwise report "shared 0" for a press that could never do
                anything, and the per-row lines already say why. */}
            <button
              type="button"
              autoFocus
              disabled={busy || approvable.length === 0}
              onClick={onConfirm}
              className="rounded-md bg-accent px-3.5 py-2 text-[13.5px] font-medium text-accent-fg disabled:opacity-60"
            >
              {i18nT('pages.mcpManagement.enable_all_review_confirm')}
            </button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default McpManagement
