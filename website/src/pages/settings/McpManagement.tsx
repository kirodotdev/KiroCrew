import { Fragment, useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, ChevronRight, ExternalLink, ListChecks, RefreshCw, Server as ServerIcon } from 'lucide-react'
import {
  api,
  ApiError,
  type McpGatewayFootprint,
  type McpManagedServer,
  type McpMeasureProgress,
} from '../../api/client'
import {
  Tabs,
  TabsContent,
  TabsCount,
  TabsList,
  TabsTrigger,
  type TabItem,
} from '../../components/ui/tabs'
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
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import {
  DOCS_URL,
  type GatewayStatus,
  launchIsApprovable,
  launchPreviewErrorKey,
  readLaunchPreviews,
  stubErrorKey,
  type McpView,
  rowState,
  stateLabelKey,
  CONTRARY_STRENGTHS,
  sharedWithoutSupport,
  waitForMeasurePass,
  autoShareCandidates,
  reloadChats,
  waitingForApproval,
} from './mcpManagementModel'
import {
  DetailsToggle,
  detailsRowId,
  detailsWhyId,
  ServerDetailsBody,
  serverNameId,
  SHARE_BUTTON_CLASS,
  ShareAction,
} from './McpManagementLaunchReview'
import { AssessmentView } from './McpManagementAssessment'
import { ApprovalCard, ConfirmReload, type ReviewItem, ReviewApprovalsDialog, SharingHero } from './McpManagementHero'

/**
 * MCP Management, top to bottom: the hero (title, Learn more, the page-wide
 * sharing switch, the measured numbers, the open-chats banner), the approval
 * card, and a collapsed "Advanced" holding the per-server table, npm
 * pre-install and the Sharing assessment.
 *
 * Turning sharing ON asks first, naming every server it shares: the ones
 * enabled by hand and the shareable ones nobody chose for, which it enables,
 * each pinned to the launch read for it. It never enables a server turned off
 * by hand, a credential vendor, a non-eligible one or one waiting for an
 * approval. Turning it OFF writes no per-server choice, so the next ON
 * restores them.
 *
 * Each row's Share switch is one `<button role="switch">` named "Enable MCP
 * sharing" plus the server name, and each row's Details toggle opens a
 * full-width sub-row. No string on the page says "stub". Turning the switch on
 * is one click: it reads the launch the server runs right then and, if that
 * launch can be approved, writes the share carrying its `expected_launch`. A
 * row shows its launch only in its Details, in the gateway's display-safe
 * form; the compare-and-set on `expected_launch` is what pins the
 * approval to the exact launch the gateway resolved. When the command, args or
 * declared env later change, the row reads "Paused · command changed" and
 * waits for "Approve update", which pins the refused launch the same way.
 * Turning the switch off stops sharing. "Enable all shareable" lists the
 * servers by name first and shares only on the button under that list.
 *
 * The second sub-view, "Sharing assessment", is read-only evidence for anyone
 * who wants to know why a server is or is not a good fit. It changes nothing.
 */

export function McpManagement({
  initialAdvancedOpen = false,
}: {
  /** Advanced starts collapsed; a test of what lives inside it opens it. */
  initialAdvancedOpen?: boolean
} = {}) {
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // A change that waits for a restart is information, not a failure.
  const [restartNotice, setRestartNotice] = useState<string | null>(null)
  // Which rows have their Details sub-row open.
  const [openDetails, setOpenDetails] = useState<ReadonlySet<string>>(() => new Set())
  const [resolveNotice, setResolveNotice] = useState<string | null>(null)
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
  const servers = useMemo(() => serversQ.data?.servers ?? [], [serversQ.data])
  const stubCount = useMemo(() => servers.filter(s => s.stub).length, [servers])
  // Marked servers the assessment argues against: they start the moment
  // page-wide sharing turns on, so the page names them before that press.
  // Marked servers that turning sharing on actually shares: not one waiting
  // for an approval and not one the rewriter keeps unshared for its env.
  const willShare = useMemo(
    () =>
      servers.filter(
        s => s.stub && !status?.launch_refused?.[s.name] && s.pooling_blocked_by_env !== true,
      ),
    [servers, status?.launch_refused],
  )
  const contraryMarked = useMemo(
    () =>
      willShare
        .filter(s => s.recommendation && CONTRARY_STRENGTHS.has(s.recommendation.strength))
        .map(s => s.name),
    [willShare],
  )
  // The header counts the rows whose chip reads "Shared": a row waiting for its
  // new command, or one paused because sharing is off, is not counted.
  const switchedOnCount = useMemo(
    () =>
      servers.filter(s => rowState(s, !!status?.enabled, status?.launch_refused) === 'shared').length,
    [servers, status],
  )
  const supported = status?.supported ?? true

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
    void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
  }

  const [confirmSharing, setConfirmSharing] = useState(false)

  const setSharing = useMutation({
    mutationFn: (enabled: boolean) => api.mcpGatewayEnable(enabled),
    // Enabling can persist and still leave sharing unreachable; `ping_ok` says so.
    onSuccess: (res, enabled) => {
      invalidate()
      void qc.invalidateQueries({ queryKey: ['mcpGatewayFootprint'] })
      if (enabled && !res.ping_ok) setError(i18nT('pages.mcpManagement.sharing_not_live'))
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.sharing_failed'))
    },
  })

  // The write half of a share. Sharing records the exact launch the server runs
  // and marks it shared; the endpoint refuses the classes that can never be
  // shared. Every stub=true carries the `expected_launch` that was read, so a
  // launch that moved in between is refused rather than approved. It never
  // turns page-wide sharing on by itself: that moves every marked server at
  // once. A server marked while sharing is off opens the switch's own
  // confirm, so the press reaches sharing in the same flow, still asked first.
  // The confirm counts and names the marked servers, so it opens only once the
  // rows a write changed have been read back, and only if that fresh status
  // still has sharing off (another tab may have turned it on). A failed read
  // keeps it closed rather than asking about a list that is not the current one.
  const askToTurnOnSharing = async () => {
    const reads = await Promise.allSettled([
      qc.refetchQueries({ queryKey: ['mcpGatewayStatus'] }, { throwOnError: true }),
      qc.refetchQueries({ queryKey: ['mcpGatewayServers'] }, { throwOnError: true }),
    ])
    const fresh = qc.getQueryData<GatewayStatus>(['mcpGatewayStatus'])
    if (reads.every(r => r.status === 'fulfilled') && fresh && !fresh.enabled) setConfirmSharing(true)
  }

  const setStub = useMutation({
    mutationFn: ({ name, stub, expectedLaunch }: { name: string; stub: boolean; expectedLaunch?: string }) =>
      api.mcpGatewaySetStub(name, stub, expectedLaunch),
    onSuccess: async (res, vars) => {
      invalidate()
      if (vars.stub && supported && res?.sharing_on !== true && !status?.enabled) {
        void askToTurnOnSharing()
      }
      if (res && res.restart_required) {
        setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      } else if (res && res.applied === false) {
        setError(i18nT('pages.mcpManagement.stub_not_live'))
      }
    },
    onError: (err, vars) => {
      invalidate()
      setError(i18nT(stubErrorKey(err), { name: vars.name }))
    },
  })

  // The read half, and the only gate: one click reads the launch and, if it can
  // be approved, writes the share straight away carrying its identity. An
  // unapprovable launch turns nothing on and says so; a read failure (a separate
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
    onError: (err, name) => {
      setError(i18nT(launchPreviewErrorKey(err), { name }))
    },
  })

  const resolveRefresh = useMutation({
    mutationFn: () => api.mcpResolveRefresh(),
    onSuccess: res => {
      invalidate()
      if (!res.ok) {
        setResolveNotice(i18nT('pages.mcpManagement.resolve_no_targets'))
        return
      }
      const ready = res.ready?.length ?? 0
      // Every install failing must not read as "already up to date".
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
      // 409 is a pass already running elsewhere: information, not a failure.
      if (err instanceof ApiError && err.status === 409) {
        setResolveNotice(i18nT('pages.mcpManagement.resolve_already_running'))
        return
      }
      setError(i18nT('pages.mcpManagement.resolve_failed'))
    },
  })

  // One count shared by the tab badge and the assessment banner.
  const unsupportedCount = useMemo(
    () =>
      servers.filter(s => sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)).length,
    [servers, status?.enabled, status?.launch_refused],
  )
  // Shareable servers nobody chose for: turning sharing on enables them too.
  const autoCandidates = useMemo(
    () => autoShareCandidates(servers, status?.launch_refused),
    [servers, status?.launch_refused],
  )
  const canEnableSharing = supported && (stubCount > 0 || autoCandidates.length > 0)
  const waiting = useMemo(
    () => waitingForApproval(servers, status?.launch_refused),
    [servers, status?.launch_refused],
  )
  const [advancedOpen, setAdvancedOpen] = useState(initialAdvancedOpen)

  // The hero's numbers and the stale-chat banner: one reading of this host.
  const footprintQ = useQuery<McpGatewayFootprint>({
    queryKey: ['mcpGatewayFootprint'],
    queryFn: () => api.mcpGatewayFootprint(),
    refetchInterval: 30_000,
  })
  const staleChats = useMemo(() => footprintQ.data?.stale_chats ?? [], [footprintQ.data])

  // Turning sharing ON: read the launch of every server it would also enable,
  // so the confirm names exactly what gets shared and each write is pinned to
  // the launch read here. A launch that cannot be approved is named as
  // skipped, never shared.
  const [sharePlan, setSharePlan] = useState<{
    auto: Array<{ name: string; expected: string }>
    skipped: string[]
    failed: string[]
  } | null>(null)
  const prepareSharing = useMutation({
    mutationFn: async () => {
      const [freshStatus, fresh] = await Promise.all([api.mcpGatewayStatus(), api.mcpGatewayServers()])
      qc.setQueryData(['mcpGatewayStatus'], freshStatus)
      qc.setQueryData(['mcpGatewayServers'], fresh)
      const auto: Array<{ name: string; expected: string }> = []
      const skipped: string[] = []
      // A read that failed is an error, not "cannot be approved": kept apart
      // so the confirm shows it through the error notice.
      const failed: string[] = []
      const names = autoShareCandidates(fresh?.servers ?? [], freshStatus?.launch_refused).map(s => s.name)
      for (const { name, preview } of await readLaunchPreviews(names)) {
        if (!preview) failed.push(name)
        else if (launchIsApprovable(preview)) auto.push({ name, expected: preview.expected_launch as string })
        else skipped.push(name)
      }
      return { auto, skipped, failed }
    },
    onSuccess: plan => {
      setSharePlan(plan)
      setConfirmSharing(true)
    },
    onError: () => setError(i18nT('pages.mcpManagement.sharing_failed')),
  })
  // Sharing goes on FIRST, so each write is checked against the bar for a
  // pooled copy; then each auto server is written, pinned to its launch. The
  // endpoint's eligibility check may still skip one, and that is reported.
  const turnOn = useMutation({
    mutationFn: async (auto: Array<{ name: string; expected: string }>) => {
      const res = await api.mcpGatewayEnable(true)
      const skippedNames: string[] = []
      const failed: string[] = []
      let shared = 0
      let restart = false
      for (const { name, expected } of auto) {
        try {
          const w = await api.mcpGatewaySetStub(name, true, expected, true)
          if (w.stubbed?.includes(name)) shared++
          else skippedNames.push(name)
          if (w.restart_required) restart = true
        } catch {
          failed.push(name)
        }
      }
      return { pingOk: res.ping_ok, shared, skippedNames, failed, restart }
    },
    onSuccess: r => {
      invalidate()
      void qc.invalidateQueries({ queryKey: ['mcpGatewayFootprint'] })
      if (!r.pingOk) setError(i18nT('pages.mcpManagement.sharing_not_live'))
      if (r.skippedNames.length > 0) {
        setNotice(i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: r.skippedNames.join(', ') }))
      }
      if (r.failed.length > 0) {
        setError(i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: r.failed.join(', ') }))
      }
      if (r.restart) setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.sharing_failed'))
    },
  })

  const busy = setStub.isPending || setSharing.isPending || previewLaunch.isPending || turnOn.isPending

  // "Reload <m> chats": the dashboard's own per-chat reload, one chat at a
  // time, after a confirm. A busy chat is refused by the gateway and skipped.
  const [confirmReload, setConfirmReload] = useState(false)
  const [reloadNote, setReloadNote] = useState<string | null>(null)
  const [reloadError, setReloadError] = useState<string | null>(null)
  const reload = useMutation({
    mutationFn: (slots: string[]) => reloadChats(slots, slot => api.chatSlotReload(slot)),
    onSuccess: r => {
      void qc.invalidateQueries({ queryKey: ['mcpGatewayFootprint'] })
      setReloadNote(r.skipped.length > 0 ? i18nT('pages.mcpManagement.reload_skipped', { count: r.skipped.length }) : null)
      setReloadError(r.failed.length > 0 ? i18nT('pages.mcpManagement.reload_failed', { count: r.failed.length }) : null)
    },
  })

  // Rows the measurement pass would act on, including an older gateway's row
  // with no verdict and a handshake that did not reproduce (re-measured every pass).
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

  const { data: measureProgress } = useQuery<McpMeasureProgress>({
    queryKey: ['mcp-measure-progress'],
    queryFn: () => api.mcpMeasureProgress(),
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

  // "Share all": measure the unmeasured first, then read each candidate's launch
  // and list the candidates by name. Nothing is shared until the person presses
  // the button under the list, and each share is pinned to the launch read for
  // it here. No command is shown.
  const [allPlan, setAllPlan] = useState<Array<{ name: string; expected: string }> | null>(null)
  // Candidates whose launch could not be approved: named inside the list, so a
  // server left out does not just vanish from it.
  const [allSkipped, setAllSkipped] = useState<string[]>([])
  const prepareAll = useMutation({
    mutationFn: async () => {
      if (unmeasuredCount > 0) {
        await api.mcpMeasureStart()
        if (!(await waitForMeasurePass(qc))) return { pending: true as const }
      }
      // Re-read directly: the shared QueryClient never treats its cache as stale.
      // The fresh rows go back into the cache, so the assessment view shows the
      // verdicts this pass just measured, not the ones read before it.
      const fresh = await api.mcpGatewayServers()
      qc.setQueryData(['mcpGatewayServers'], fresh)
      // Not an eligibility test: each write sends `resolve_eligibility`, and the
      // server's `_stub_eligibility` decides and reports what it skipped.
      const candidates = (fresh?.servers ?? [])
        .filter(s => s.can_stub && !s.stub)
        .map(s => s.name)
      const plan: Array<{ name: string; expected: string }> = []
      const unreadable: string[] = []
      const failed: string[] = []
      for (const { name, preview } of await readLaunchPreviews(candidates)) {
        // A failed read is an error, not "nothing to share".
        if (!preview) failed.push(name)
        else if (launchIsApprovable(preview)) plan.push({ name, expected: preview.expected_launch as string })
        else unreadable.push(name)
      }
      return { pending: false as const, plan, unreadable, failed }
    },
    onSuccess: r => {
      if (r.pending) {
        setNotice(i18nT('pages.mcpManagement.enable_all_still_measuring'))
        return
      }
      if (r.failed.length > 0) {
        setError(i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: r.failed.join(', ') }))
      }
      if (r.plan.length === 0) {
        // Servers whose launch can't be approved are still unshared: name them
        // rather than claiming there is nothing left.
        if (r.unreadable.length > 0) {
          setNotice(i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: r.unreadable.join(', ') }))
        } else if (r.failed.length === 0) {
          setNotice(i18nT('pages.mcpManagement.enable_all_review_nothing'))
        }
        return
      }
      setAllPlan(r.plan)
      setAllSkipped(r.unreadable)
    },
    onError: () => setError(i18nT('pages.mcpManagement.stub_failed')),
  })
  const enableAll = useMutation({
    mutationFn: async (plan: Array<{ name: string; expected: string }>) => {
      let shared = 0
      let skipped = 0
      const failed: string[] = []
      let restart = false
      // What the writes themselves report, not the status read before them:
      // another tab may have turned sharing on in between.
      let sharingOn = !!status?.enabled
      const skippedNames: string[] = []
      for (const { name, expected } of plan) {
        try {
          const res = await api.mcpGatewaySetStub(name, true, expected, true)
          if (res.stubbed?.includes(name)) shared++
          else {
            skipped++
            skippedNames.push(name)
          }
          if (res.restart_required) restart = true
          if (typeof res.sharing_on === 'boolean') sharingOn = res.sharing_on
        } catch {
          failed.push(name)
        }
      }
      return { shared, skipped, skippedNames, failed, restart, sharingOn }
    },
    onSuccess: r => {
      invalidate()
      setAllPlan(null)
      setAllSkipped([])
      setNotice(
        [
          // With sharing off the writes only mark: say so, then ask about sharing.
          i18nT(
            r.sharingOn
              ? 'pages.mcpManagement.enable_all_review_result_shared'
              : 'pages.mcpManagement.enable_all_review_result_marked',
            { count: r.shared },
          ),
          ...(r.skipped > 0
            ? [i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: r.skippedNames.join(', ') })]
            : []),
        ].join(' '),
      )
      // Failed requests are errors: they go through ErrorNotice, not the status line.
      if (r.failed.length > 0) {
        setError(i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: r.failed.join(', ') }))
      }
      if (!r.sharingOn && r.shared > 0 && supported) void askToTurnOnSharing()
      if (r.restart) setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.stub_failed'))
    },
  })

  const [view, setView] = useState<McpView>('servers')
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
      count: unsupportedCount,
    },
  ]

  // "Review and approve": a dialog over the servers that wait, opened on a
  // snapshot of their refusals, so each Approve pins the launch it shows.
  const [reviewItems, setReviewItems] = useState<ReviewItem[] | null>(null)
  const [reviewOpener, setReviewOpener] = useState<HTMLElement | null>(null)
  const openReview = () => {
    setReviewOpener(document.activeElement instanceof HTMLElement ? document.activeElement : null)
    const refused = status?.launch_refused ?? {}
    setReviewItems(waiting.map(server => ({ server, refusal: refused[server.name] })))
  }
  const closeReview = useCallback(() => setReviewItems(null), [])
  // The table button's own write and checks: one stub POST pinned to the
  // shown `expected_launch`. A launch that moved is refused by the endpoint,
  // and the refusal comes back as the line the dialog shows in that block.
  const approveFromReview = async (name: string, expectedLaunch: string): Promise<string | null> => {
    try {
      const res = await api.mcpGatewaySetStub(name, true, expectedLaunch)
      if (res && res.restart_required) setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      return null
    } catch (err) {
      return i18nT(stubErrorKey(err), { name })
    } finally {
      // The card, the table and the nav dot all read this one status query.
      invalidate()
    }
  }

  return (
    <div className="min-w-0 space-y-4">
      <ErrorNotice
        message={statusQ.isError ? i18nT('pages.mcpManagement.status_failed') : null}
        askAgent
      />
      {/* Page-wide, not inside Advanced: a failed server list disables the
          hero switch, so its reason must show while Advanced is closed. */}
      <ErrorNotice
        message={serversQ.isError ? i18nT('pages.mcpManagement.servers_failed') : null}
        askAgent
      />
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

      <SharingHero
        on={!!status?.enabled}
        switchDisabled={
          busy || prepareSharing.isPending || turnOn.isPending || statusQ.isLoading
          || (!status?.enabled && !canEnableSharing)
        }
        switchNote={!supported ? i18nT('pages.mcpManagement.unsupported_platform') : null}
        onToggle={() => {
          setError(null)
          setNotice(null)
          // Turning sharing ON moves servers out of the chat sandboxes, so it
          // asks first, naming every server it shares. Turning it OFF only ever
          // narrows, and keeps every per-server choice for the next ON.
          if (!status?.enabled) prepareSharing.mutate()
          else setSharing.mutate(false)
        }}
        footprint={footprintQ.data}
        footprintFailed={footprintQ.isError}
        staleCount={staleChats.length}
        reloadBusy={reload.isPending}
        reloadNote={reloadNote}
        reloadError={reloadError}
        onReload={() => setConfirmReload(true)}
      />

      <ApprovalCard waiting={waiting} onReview={openReview} />

      <section className="min-w-0 rounded-xl border border-[var(--border)] bg-[var(--card)]">
        <h2 className="m-0">
          <button
            type="button"
            aria-expanded={advancedOpen}
            aria-controls="mcp-advanced"
            onClick={() => setAdvancedOpen(o => !o)}
            className="flex w-full items-center gap-2 rounded-xl px-4 py-3 text-left text-[14px] font-medium text-[var(--text)] hover:bg-[var(--hover)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
          >
            <ChevronRight
              size={15}
              aria-hidden="true"
              className={['shrink-0 text-[var(--muted)] transition-transform', advancedOpen ? 'rotate-90' : ''].join(' ')}
            />
            {i18nT('pages.mcpManagement.advanced')}
          </button>
        </h2>
        {advancedOpen && (
          <div id="mcp-advanced" className="min-w-0 border-t border-[var(--border)] p-3 md:p-4">
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
      <TabsContent value="servers" className="min-w-0 space-y-4">
      {/* Per server: one labelled button is the decision. */}
      <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 border-b border-[var(--border)] px-4 py-2.5">
          <span className="text-[12.5px] text-[var(--muted)]">
            {i18nT('pages.mcpManagement.stubbed_of_total', {
              stubbed: switchedOnCount,
              // Every row below, so the count and the list always agree.
              total: servers.length,
            })}
          </span>
          <Btn
            type="button"
            className={SHARE_BUTTON_CLASS}
            onClick={() => {
              setError(null)
              setNotice(null)
              setRestartNotice(null)
              prepareAll.mutate()
            }}
            disabled={
              busy
              || prepareAll.isPending
              || enableAll.isPending
              || allPlan !== null
              || serversQ.isLoading
              || !supported
              || (stubbableNames.length === 0 && unmeasuredCount === 0)
            }
          >
            {i18nT('pages.mcpManagement.enable_all')}
          </Btn>
          {(prepareAll.isPending || enableAll.isPending || notice) && (
            <span role="status" className="basis-full text-[12.5px] text-[var(--muted)]">
              {prepareAll.isPending && (measureProgress?.running || unmeasuredCount > 0)
                ? i18nT('pages.mcpManagement.assessment.measure_running', {
                    done: measureProgress?.running ? measureProgress.done : 0,
                    total: measureProgress?.running ? measureProgress.total : unmeasuredCount,
                  })
                : prepareAll.isPending
                  ? i18nT('pages.mcpManagement.share_checking')
                  : notice}
            </span>
          )}
        </div>
        {allPlan && (
          <div
            role="region"
            aria-label={i18nT('pages.mcpManagement.share_all_list_aria')}
            className="min-w-0 space-y-3 border-b border-[var(--border)] px-4 py-3 text-[12px] leading-relaxed text-[var(--muted)] [overflow-wrap:anywhere]"
          >
            <p className="font-medium text-[var(--text)]">{i18nT('pages.mcpManagement.share_all_runs')}</p>
            {/* A plain list of names: nothing here can be edited. */}
            <ul className="list-disc space-y-1 pl-5">
              {allPlan.map(item => (
                <li key={item.name} className="font-mono text-[12.5px] text-[var(--text)]">
                  {item.name}
                </li>
              ))}
            </ul>
            {allSkipped.length > 0 && (
              <p>{i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: allSkipped.join(', ') })}</p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Btn
                type="button"
                primary
                className={SHARE_BUTTON_CLASS}
                disabled={enableAll.isPending || busy}
                onClick={() => enableAll.mutate(allPlan)}
              >
                <Check size={14} aria-hidden="true" />
                {i18nT('pages.mcpManagement.share_all_confirm', { count: allPlan.length })}
              </Btn>
              <Btn
                type="button"
                className={SHARE_BUTTON_CLASS}
                disabled={enableAll.isPending}
                onClick={() => {
                  setAllPlan(null)
                  setAllSkipped([])
                  setNotice(null)
                }}
              >
                {i18nT('pages.mcpManagement.share_all_cancel')}
              </Btn>
            </div>
          </div>
        )}
        {/* The columns of the page before this change: SERVER | USED BY | STATE |
            SHARE. Below md the table collapses to stacked rows, each cell
            carrying its own small heading. A row's hint and its Details open as
            full-width sub-rows under it. */}
        <table className="block w-full border-collapse md:table">
          <thead className="hidden md:table-header-group">
            <tr>
              <th className="w-[30%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_server')}
              </th>
              <th className="w-[30%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_used_by')}
              </th>
              <th className="w-[20%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_state')}
              </th>
              <th className="w-[20%] px-4 pb-2.5 pt-3.5 text-right text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_share')}
              </th>
            </tr>
          </thead>
          <tbody className="block md:table-row-group">
            {servers.map(s => {
              const state = rowState(s, !!status?.enabled, status?.launch_refused)
              const waiting = state === 'needs_reapproval'
              const refusal = waiting ? status?.launch_refused?.[s.name] : undefined
              const flagged = sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)
              // The row keeps a line only where it has no control that could
              // fix things: a refused launch with no approvable identity. Every
              // other "why" lives in the row's Details.
              const hint =
                refusal && !launchIsApprovable(refusal)
                  ? i18nT('pages.mcpManagement.approval_not_approvable_here')
                  : null
              const hintId = `mcp-share-hint-${s.name}`
              const detailsOpen = openDetails.has(s.name)
              const describedBy = hint ? hintId : detailsOpen ? detailsWhyId(s.name) : undefined
              return (
                <Fragment key={s.name}>
                  <tr className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-2 border-t border-[var(--border)] px-3 py-3 md:table-row md:px-0 md:py-0">
                    <td className="col-start-1 row-start-1 min-w-0 p-0 align-top md:table-cell md:px-4 md:py-3">
                      <span
                        id={serverNameId(s.name)}
                        className="block break-words font-mono text-[13px] text-[var(--text)] [overflow-wrap:anywhere]"
                      >
                        {s.name}
                      </span>
                      <DetailsToggle
                        server={s}
                        open={detailsOpen}
                        onToggle={() =>
                          setOpenDetails(prev => {
                            const next = new Set(prev)
                            if (next.has(s.name)) next.delete(s.name)
                            else next.add(s.name)
                            return next
                          })
                        }
                      />
                    </td>
                    <td className="col-start-1 row-start-2 min-w-0 break-words p-0 align-top text-[12.5px] text-[var(--muted)] [overflow-wrap:anywhere] md:table-cell md:px-4 md:py-3">
                      <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
                        {i18nT('pages.mcpManagement.col_used_by')}
                      </span>
                      {s.agents.join(', ')}
                    </td>
                    <td className="col-start-1 row-start-3 min-w-0 p-0 align-top md:table-cell md:px-4 md:py-3">
                      <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] md:hidden">
                        {i18nT('pages.mcpManagement.col_state')}
                      </span>
                      <span
                        // main's chip: a term, never wrapped, and coloured by the
                        // one derivation of this row's state.
                        className={[
                          'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 font-mono text-[11px]',
                          flagged || refusal?.reason === 'changed_needs_reapproval'
                            ? 'border border-[var(--danger)] text-[var(--danger)]'
                            : refusal
                              ? 'border border-[var(--warn)] text-[var(--warn)]'
                              : state === 'shared'
                                ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
                                : 'border border-[var(--border)] text-[var(--muted)]',
                        ].join(' ')}
                      >
                        {(flagged || refusal) && <AlertTriangle size={11} aria-hidden="true" />}
                        {i18nT(stateLabelKey(state, refusal))}
                      </span>
                    </td>
                    <td className="col-start-2 row-start-1 p-0 text-right align-top md:table-cell md:px-4 md:py-2.5">
                      <ShareAction
                        server={s}
                        refusal={refusal}
                        busy={busy}
                        reading={previewLaunch.isPending && previewLaunch.variables === s.name}
                        supported={supported}
                        describedBy={describedBy}
                        onShare={() => {
                          setError(null)
                          setRestartNotice(null)
                          previewLaunch.mutate(s.name)
                        }}
                        onConfirm={expectedLaunch => {
                          setError(null)
                          setRestartNotice(null)
                          setStub.mutate({ name: s.name, stub: true, expectedLaunch })
                        }}
                        onStop={() => {
                          setError(null)
                          setRestartNotice(null)
                          setStub.mutate({ name: s.name, stub: false })
                        }}
                      />
                    </td>
                  </tr>
                  {hint && (
                    <tr className="block md:table-row">
                      <td
                        id={hintId}
                        colSpan={4}
                        className="block px-3 pb-3 pt-0 text-[12.5px] text-[var(--muted)] md:table-cell md:px-4"
                      >
                        {hint}
                      </td>
                    </tr>
                  )}
                  {detailsOpen && (
                    <tr id={detailsRowId(s.name)} className="block md:table-row">
                      <td colSpan={4} className="block px-3 pb-3 pt-0 md:table-cell md:px-4">
                        <ServerDetailsBody server={s} state={state} refusal={refusal} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
            {servers.length === 0 && !serversQ.isLoading && !serversQ.isError && (
              <tr className="block border-t border-[var(--border)] md:table-row">
                <td colSpan={4} className="block px-4 py-6 text-center text-[13px] text-[var(--muted)] md:table-cell">
                  {i18nT('pages.mcpManagement.no_servers')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {/* Page-wide: install npm servers once so chats start without a download. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-4 py-3">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-[var(--text)]">
              {i18nT('pages.mcpManagement.resolve_label')}
            </div>
            {resolveNotice && (
              <p role="status" className="mt-1 text-[12.5px] text-[var(--text)]">
                {resolveNotice}
              </p>
            )}
            <ErrorNotice variant="inline" className="mt-2" message={resolveError} askAgent />
          </div>
          <button
            type="button"
            disabled={resolveRefresh.isPending}
            onClick={() => {
              setError(null)
              setResolveNotice(null)
              setResolveError(null)
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
      </TabsContent>
    </Tabs>
          </div>
        )}
      </section>

      <ConfirmSharing
        open={confirmSharing}
        marked={[...willShare.map(s => s.name), ...(sharePlan?.auto.map(a => a.name) ?? [])]}
        skipped={sharePlan?.skipped ?? []}
        failed={sharePlan?.failed ?? []}
        unsupported={contraryMarked}
        busy={setSharing.isPending || setStub.isPending || turnOn.isPending}
        // A stale or failed list must not be what the press confirms.
        ready={!statusQ.isFetching && !serversQ.isFetching && !statusQ.isError && !serversQ.isError}
        onCancel={() => {
          setConfirmSharing(false)
          setSharePlan(null)
        }}
        onConfirm={() => {
          setConfirmSharing(false)
          turnOn.mutate(sharePlan?.auto ?? [])
          setSharePlan(null)
        }}
      />
      <ReviewApprovalsDialog
        open={reviewItems !== null}
        items={reviewItems ?? []}
        supported={supported}
        returnFocusTo={reviewOpener}
        onApprove={approveFromReview}
        onClose={closeReview}
      />
      <ConfirmReload
        open={confirmReload}
        count={staleChats.length}
        busy={reload.isPending}
        onCancel={() => setConfirmReload(false)}
        onConfirm={() => {
          setConfirmReload(false)
          reload.mutate(staleChats)
        }}
      />
    </div>
  )
}

function ConfirmSharing({
  open,
  marked,
  skipped,
  failed,
  unsupported,
  ready,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  /** Every server this switch starts sharing, by name: the ones marked by
   *  hand and the ones nobody chose for, which it enables. */
  marked: string[]
  /** Servers it would have enabled but whose launch could not be approved. */
  skipped: string[]
  /** Servers whose launch could not be read at all: a request failure. */
  failed: string[]
  unsupported: string[]
  /** False while the list it shows is being re-read, or could not be. */
  ready: boolean
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
            {i18nT('pages.mcpManagement.confirm_will_share', { count: marked.length })}{' '}
            {i18nT('pages.mcpManagement.share_risk')}
          </DialogDescription>
          {/* Named, so the count is not one more number to decode. Data, not
              prose, so no catalog entry. */}
          {marked.length > 0 && (
            <p className="mt-1 break-words font-mono text-[12px] text-[var(--text)] [overflow-wrap:anywhere]">
              {marked.join(', ')}
            </p>
          )}
          <ErrorNotice
            variant="inline"
            className="mt-1"
            message={
              failed.length > 0
                ? i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: failed.join(', ') })
                : null
            }
            askAgent
          />
          {skipped.length > 0 && (
            <p className="mt-1 break-words text-[12.5px] text-muted [overflow-wrap:anywhere]">
              {i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: skipped.join(', ') })}
            </p>
          )}
          {/* The verdict belongs at the decision point, not only after the fact:
              an operator who never opens the assessment view would otherwise
              reach the exact state this page exists to warn about. */}
          {unsupported.length > 0 && (
            <div className="mt-2 flex items-start gap-2 text-[13.5px] text-[var(--danger)]">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
              <div>
                <p>
                  {i18nT('pages.mcpManagement.confirm_unsupported')}
                </p>
                {/* Naming them is the difference between a number the operator has
                    to go hunting for and one they can act on here. Listed in full,
                    so nothing is hidden. Data, not prose, so no catalog entry. */}
                <p className="mt-1 break-words font-mono text-[12px] [overflow-wrap:anywhere]">
                  {unsupported.join(', ')}
                </p>
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
              disabled={busy || !ready}
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
