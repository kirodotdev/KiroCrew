import { useEffect, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Scale, CheckCircle2, AlertCircle, GitBranch, GitCommitHorizontal, ExternalLink, ArrowUp, Package, X, Download } from 'lucide-react'
import { Card, CardTitle, Btn, Toggle } from '../../components/ui'
import { useBranding } from '../../hooks/useBranding'
import { useAppSelector } from '../../store'
import { codeBrowserBranchUrl, codeBrowserCommitUrl } from '../../lib/codeBrowser'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import SegmentedControl from '../../components/SegmentedControl'
import { api, ApiError } from '../../api/client'
import { sanitize } from '../../api/helpers'

import { i18nT } from '../../i18n/t'
type UpdateState = {
  state: 'checking' | 'found' | 'available' | 'downloading' | 'downloaded' | 'not-available' | 'error'
  version?: string
  notes?: string
  pubDate?: string
  channel?: string
  message?: string
  /** Which stage failed. Absent on builds older than the phase-aware emit. */
  phase?: 'check' | 'download' | 'install'
  /** Stable failure class; the user-facing copy is chosen from this, not from `message`. */
  code?: string
  httpStatus?: number
  /** Download progress, 0-100. Absent until the first progress event arrives. */
  percent?: number
  bytesPerSecond?: number
}

/** Human-readable transfer rate for the progress label. */
function formatRate(bps: number): string {
  if (!Number.isFinite(bps) || bps <= 0) return ''
  const mb = bps / (1024 * 1024)
  return mb >= 1 ? `${mb.toFixed(1)} MB/s` : `${Math.round(bps / 1024)} KB/s`
}

/**
 * User-facing copy for a failure class. `message` from the updater is raw
 * library text (multi-line HttpError dumps, digest comparisons), so it is only
 * used as a last-resort detail for an unclassified failure.
 */
function updateErrorText(st: UpdateState | null | undefined): string {
  const ap = 'pages.settings.aboutPanel.'
  switch (st?.code) {
    case 'offline': return i18nT(ap + 'update_error_offline')
    case 'server': {
      // Guard the interpolation: i18nT returns undefined for a key missing from
      // every catalog, and calling .replace() on that took the whole panel down
      // via the error boundary. A status-less fallback is strictly better than a
      // blank Settings page.
      const template = i18nT(ap + 'update_error_server_status')
      return st.httpStatus && typeof template === 'string'
        ? template.replace('{{status}}', String(st.httpStatus))
        : i18nT(ap + 'update_error_server')
    }
    case 'no-release': return i18nT(ap + 'update_error_no_release')
    case 'integrity': return i18nT(ap + 'update_error_integrity')
    case 'misconfigured': return i18nT(ap + 'update_error_misconfigured')
    // Unclassified failure. The localized generic WINS over st.message: the raw
    // value is electron-updater's exception text, written for a developer reading
    // logs ("ShipIt could not replace the application bundle") and always English.
    // Preferring it was the exact defect #736 was filed for, just on the fallback
    // branch. The detail still reaches the log via the main process; only fall
    // back to it if the catalog key is somehow missing, since a raw string beats
    // an empty error line.
    default: return i18nT(ap + 'update_error_unknown') || st?.message || ''
  }
}

type UpdateInfo = {
  version?: string
  channel?: string
  stampedChannel?: string | null
  channelSwitchable?: boolean
  channelPreference?: string
  platform?: string
  /** Manual-reinstall permalink from the main process; absent when no lane. */
  downloadUrl?: string | null
  packaged?: boolean
  disabled?: string
}

type UpdateAPI = {
  onState: (cb: (payload: UpdateState) => void) => (() => void)
  check: () => Promise<unknown>
  download: () => Promise<unknown>
  install: () => Promise<unknown>
  getInfo: () => Promise<UpdateInfo>
  setChannel?: (channel: string) => Promise<{ ok: boolean; error?: string }>
}

function getUpdateApi(): UpdateAPI | undefined {
  return (window as unknown as { updateAPI?: UpdateAPI }).updateAPI
}

// Subtle accent tint for the version pill + build chips (works with any theme's
// --accent via color-mix; avoids depending on a tinted-bg token).
const ACCENT_TINT: React.CSSProperties = {
  background: 'color-mix(in oklab, var(--accent) 12%, transparent)',
  borderColor: 'color-mix(in oklab, var(--accent) 30%, transparent)',
}

// Accent gradient wash for the identity hero (overrides Card's flat bg-card).
const HERO_BG: React.CSSProperties = {
  background:
    'linear-gradient(135deg, color-mix(in oklab, var(--accent) 14%, transparent), color-mix(in oklab, var(--accent) 3%, transparent) 55%, var(--card))',
}

/** Row: label on the left, value on the right. */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1.5 text-sm">
      <span className="text-muted">{label}</span>
      <span className="text-text font-medium">{children}</span>
    </div>
  )
}

export function AboutPanel() {
  const { botName, avatar } = useBranding()
  const gatewayVersion = useAppSelector(s => s.dashboard.status?.version) || ''
  const buildBranch = useAppSelector(s => s.dashboard.status?.branch) || ''
  const buildCommit = useAppSelector(s => s.dashboard.status?.commit) || ''
  const updateAvailable = useAppSelector(s => s.dashboard.status?.update_available) || false
  const queryClient = useQueryClient()
  const desktopApi = getUpdateApi()
  const isDesktop = !!desktopApi

  // Desktop (Electron) app info (version, channel, platform)
  const { data: info } = useQuery({
    queryKey: ['update-info'],
    queryFn: () => desktopApi!.getInfo(),
    enabled: isDesktop,
    staleTime: Infinity, // static per session
  })

  // Desktop update lifecycle state, read from the shared cache that
  // useUpdateSubscription (mounted in App.tsx) populates.
  const { data: updateState } = useQuery<UpdateState | null>({
    queryKey: ['update-state'],
    queryFn: () => null,
    enabled: false,
    staleTime: Infinity,
  })

  // Desktop manual check action
  const checkMutation = useMutation({
    mutationFn: () => desktopApi!.check(),
    onMutate: () => queryClient.setQueryData(['update-state'], null),
  })
  // Explicit consent actions (macOS Software Update semantics): downloading
  // and installing each happen only when the user clicks.
  const downloadMutation = useMutation({ mutationFn: () => desktopApi!.download() })
  const installMutation = useMutation({ mutationFn: () => desktopApi!.install() })
  // Install is a ONE-WAY door, so the control must never become actionable
  // again. Note isSuccess, not just isPending: `update:install` resolves as soon
  // as the install is DISPATCHED, and on macOS the platform installer then works
  // for several more seconds before the app quits. Keying `disabled` on
  // isPending alone let the button re-arm during that window, so the user saw a
  // clickable "Restart & Update" followed by an unexplained quit -- which reads
  // as a crash.
  const installDispatched = installMutation.isPending || installMutation.isSuccess
  // Channel switcher (stable ⇄ insider opt-in). Switching persists the
  // preference and triggers a check; the other channel's build then arrives
  // as the normal consent card above -- never an automatic install. Nightly
  // builds report channelSwitchable=false (separate pinned install).
  const channelMutation = useMutation({
    mutationFn: (next: string) => desktopApi!.setChannel!(next),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['update-info'] }),
  })

  const version = info?.version || gatewayVersion || '—'
  const channel = info?.channel
  const updatesDisabled = info?.disabled
  const checking = checkMutation.isPending || updateState?.state === 'checking'

  // Desktop status line under the Check button (simple states only — the
  // found/downloading/downloaded lifecycle renders as the update card below).
  let status: React.ReactNode = null
  if (checking) {
    status = <span className="text-muted flex items-center gap-1.5"><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.checking_for_updates')}</span>
  } else if (updateState?.state === 'not-available') {
    status = <span className="text-ok flex items-center gap-1.5"><CheckCircle2 size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.you_are_on_the_latest_version')}</span>
  } else if (updateState?.state === 'error' && updateState.phase !== 'download' && updateState.phase !== 'install') {
    // Download failures are NOT rendered here: they render inside the update
    // card so the found version stays on screen and can be retried (#735).
    status = <span className="text-danger flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.couldn_t_check_for_updates')}: {updateErrorText(updateState)}</span>
  }

  // Update card: shown whenever an update is found / downloading / ready.
  const cardState = updateState?.state
  // A download-phase failure keeps the card: the user consented to this
  // version, so losing it on a transient error would strand them with a check
  // complaint and no way back (#735).
  // Both post-consent phases keep the card mounted: they are the states where a
  // Retry and the manual-reinstall link are the user's only way forward. A
  // CHECK failure has no card to keep (nothing was ever offered) and stays in
  // the status line.
  const cardFailedPhase = updateState?.phase === 'download' || updateState?.phase === 'install'
  const cardFailed = cardState === 'error' && cardFailedPhase
  const cardInstallFailed = cardState === 'error' && updateState?.phase === 'install'
  const showUpdateCard = !checking && (cardState === 'found' || cardState === 'available' || cardState === 'downloading' || cardState === 'downloaded' || cardFailed)
  const cardBusy = cardState === 'available' || cardState === 'downloading'
  const cardReady = cardState === 'downloaded'
  // Determinate only once a progress event has arrived; before that the label
  // stays indeterminate, since `percent` is optional in the emit.
  const cardPercent = cardState === 'downloading' && typeof updateState?.percent === 'number'
    ? Math.max(0, Math.min(100, updateState.percent))
    : null
  const cardPubDate = updateState?.pubDate ? new Date(updateState.pubDate) : null
  // Escape hatch shown once the installer is the thing that could fail. The URL
  // is built in the main process (auto-update.js manualDownloadUrl) because only
  // it knows the real platform -- getInfo().platform is a display string that
  // reports its darwin default everywhere.
  const manualUrl = info?.downloadUrl || null
  const showManualFallback = !!manualUrl && (cardReady || cardFailed)
  const updateCard: React.ReactNode = showUpdateCard ? (
    <div className="p-3 bg-bg rounded-lg border border-border flex flex-col gap-2" data-testid="update-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-0.5 min-w-0">
          <span className="text-[13px] font-medium text-text flex items-center gap-1.5">
            <ArrowUp size={13} className="lucide-inline text-accent" />
            {botName || 'Kiro Crew'} {updateState?.version || 'update'}
          </span>
          <span className="text-[12px] text-muted">
            {channel ? `${channel} channel` : 'update'}
            {cardPubDate && !isNaN(cardPubDate.getTime()) ? ` · published ${cardPubDate.toLocaleString()}` : ''}
          </span>
        </div>
        <div className="shrink-0">
          {cardReady ? (
            <Btn primary onClick={() => installMutation.mutate()} disabled={installDispatched}>
              <RefreshCw size={13} className={`lucide-inline ${installDispatched ? 'animate-spin' : ''}`} /> {installMutation.isSuccess
                ? i18nT('pages.settings.aboutPanel.restarting')
                : i18nT('pages.settings.aboutPanel.restart_update')}
            </Btn>
          ) : (
            <Btn primary onClick={() => downloadMutation.mutate()} disabled={cardBusy || downloadMutation.isPending}>
              {cardBusy || downloadMutation.isPending
                ? (<><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.downloading')}</>)
                : cardFailed
                  ? (<><RefreshCw size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.retry')}</>)
                  : (<><ArrowUp size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.download_install')}</>)}
            </Btn>
          )}
        </div>
      </div>
      {cardState === 'downloading' && (
        <>
          <div
            className="h-1 bg-border rounded-full overflow-hidden"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            {...(cardPercent === null ? {} : { 'aria-valuenow': Math.round(cardPercent) })}
            data-testid="update-progress"
          >
            <div
              className={`h-full bg-accent ${cardPercent === null ? 'w-1/3 animate-pulse' : ''}`}
              style={cardPercent === null ? undefined : { width: `${cardPercent}%` }}
            />
          </div>
          <span className="text-[12px] text-muted" data-testid="update-progress-label">
            {cardPercent === null
              ? i18nT('pages.settings.aboutPanel.downloading')
              : `${Math.round(cardPercent)}%${updateState?.bytesPerSecond ? ` · ${formatRate(updateState.bytesPerSecond)}` : ''}`}
          </span>
        </>
      )}
      {cardFailed && (
        <span className="text-[12px] text-danger flex items-start gap-1.5" data-testid="update-download-error">
          <AlertCircle size={13} className="lucide-inline shrink-0" />
          <span>{i18nT(cardInstallFailed ? 'pages.settings.aboutPanel.install_failed' : 'pages.settings.aboutPanel.download_failed')}: {updateErrorText(updateState)}</span>
        </span>
      )}
      {cardReady && (
        <span className="text-[12px] text-muted">
          {/* Once dispatched, the gateway goes down ON PURPOSE and the dashboard
              disconnects for the ~1-2 min Squirrel handoff. This line is the last
              thing the card says, so it must explain the coming silence. */}
          {installDispatched
            ? i18nT('pages.settings.aboutPanel.installing_quiet_note')
            : i18nT('pages.settings.aboutPanel.downloaded_and_verified_the_app_restarts_to_fini')}
        </span>
      )}
      {showManualFallback && (
        <span className="text-[12px] text-muted flex items-start gap-1.5 pt-0.5 border-t border-border" data-testid="update-manual-fallback">
          <Download size={13} className="lucide-inline shrink-0 mt-2" />
          <span className="pt-1.5">
            {/* ONE catalog string with a {{link}} placeholder: assembling the
                sentence from separate fragments would lock every language into
                English clause order. */}
            {(() => {
              const tpl = i18nT('pages.settings.aboutPanel.manual_install_fallback') || ''
              const [before, after] = tpl.split('{{link}}')
              return (
                <>
                  {before}
                  <a href={manualUrl!} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                    {i18nT('pages.settings.aboutPanel.download_the_latest_version')}
                  </a>
                  {after ?? ''}
                </>
              )
            })()}
          </span>
        </span>
      )}
      {updateState?.notes ? (
        <div className="p-2.5 bg-card rounded-md border border-border max-h-40 overflow-y-auto text-[12px] text-text whitespace-pre-wrap">{updateState.notes}</div>
      ) : null}
    </div>
  ) : null

  // --- Gateway (web dashboard) update flow ---
  // The gateway exposes /api/update/check + /api/update; used when not running
  // inside the Electron shell. "Check for updates" flips to "Update to vX" when
  // status.update_available is set; the update itself is gated behind a
  // changelog confirm because applying restarts the gateway.
  const [gwChanges, setGwChanges] = useState('')
  const [gwTarget, setGwTarget] = useState('')
  const [gwFound, setGwFound] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [applyError, setApplyError] = useState('')
  const [restarting, setRestarting] = useState(false)
  const [autoUpdate, setAutoUpdate] = useState(true)
  // Full changelog viewer (collapsible) — restores the changelog view that the
  // removed top-bar version pill used to provide; now lives in Settings > About.
  // Full changelog is open by default — this is a full page now, not a cramped
  // dropdown, so the changelog is primary content (bounded to a scroll box below).
  const [showFull, setShowFull] = useState(true)
  // Fetch via useQuery: dedups concurrent requests, caches, and gives proper
  // loading/error states (avoids the empty-content infinite-spinner and the
  // mount-vs-toggle double fetch). `enabled: showFull` loads it on mount.
  const {
    data: fullChangelog,
    isLoading: changelogLoading,
    isError: fullChangelogError,
  } = useQuery({
    queryKey: ['full-changelog'],
    queryFn: () => api.changelog().then(d => (d as { content?: string })?.content ?? ''),
    enabled: showFull,
  })
  // Memoize the DOMPurify pass so it doesn't re-run on every render.
  const safeChangelog = useMemo(() => (fullChangelog ? sanitize(fullChangelog) : ''), [fullChangelog])
  const { data: mcCfg } = useQuery({ queryKey: ['mc-config-autoupdate'], queryFn: () => api.kirocrewConfig() })
  useEffect(() => {
    const v = (mcCfg as any)?.auto_update
    if (typeof v === 'boolean') setAutoUpdate(v)
  }, [mcCfg])
  const gwCheck = useMutation({
    mutationFn: () => api.checkUpdate(),
    onSuccess: (d: any) => {
      setGwChanges(d?.changes || '')
      if (d?.version) setGwTarget(String(d.version))
      // Derive availability from the check response itself, not only the redux
      // status flag (which refreshes on a slower WS status push). Otherwise a
      // check that finds an update could still show "You're on the latest
      // version" until the flag catches up.
      setGwFound(!!d?.available)
      if (typeof d?.auto_update === 'boolean') setAutoUpdate(d.auto_update)
    },
  })
  const gwApply = useMutation({
    mutationFn: () => api.applyUpdate(),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      // A real server rejection (e.g. 409 dirty tree, 400) arrives as ApiError
      // with a status code — surface it. A bare network failure means the POST's
      // connection was reset by the gateway restart the update itself triggers;
      // that is the expected success path, not a failure.
      if (e instanceof ApiError) setApplyError(e.message || 'Update failed')
      else setRestarting(true)
    },
  })
  // Update is available if either the redux status flag or the latest check
  // response says so.
  const showUpdate = updateAvailable || gwFound

  // Escape closes the confirm dialog (unless an apply/restart is in flight),
  // matching the keyboard affordance of the settings dropdown it replaces.
  useEffect(() => {
    if (!showConfirm) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !gwApply.isPending && !restarting) setShowConfirm(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showConfirm, gwApply.isPending, restarting])

  return (
    <>
      <Card style={HERO_BG}>
        {/* Identity hero */}
        <div className="flex items-center gap-4">
          <img
            src={avatar}
            alt=""
            className="w-14 h-14 rounded-2xl object-cover bg-bg-hover shrink-0"
            style={{ boxShadow: '0 0 0 3px color-mix(in oklab, var(--accent) 22%, transparent)' }}
            onError={e => { (e.currentTarget as HTMLImageElement).style.visibility = 'hidden' }}
          />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2.5 flex-wrap">
              <span className="text-[19px] font-extrabold tracking-tight text-text-strong">{botName || 'Kiro Crew'}</span>
              <span className="text-[12px] font-mono font-semibold text-accent rounded-full px-2.5 py-0.5 border" style={ACCENT_TINT}>{i18nT('pages.settings.aboutPanel.v')}{version}</span>
              {!isDesktop && (updateAvailable
                ? <span className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold rounded-full px-2 py-0.5"
                    style={{ color: 'var(--warn)', background: 'color-mix(in oklab, var(--warn) 14%, transparent)' }}>
                    <ArrowUp size={11} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.update_available')}</span>
                : <span className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold rounded-full px-2 py-0.5"
                    style={{ color: 'var(--ok)', background: 'color-mix(in oklab, var(--ok) 14%, transparent)' }}>
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: 'var(--ok)' }} /> {i18nT('pages.settings.aboutPanel.up_to_date')}</span>
              )}
            </div>
            <div className="text-[12.5px] text-muted mt-1">{i18nT('pages.settings.aboutPanel.autonomous_agent_management_runs_locally_open_so')}</div>
          </div>
        </div>

        {/* Build + license chips */}
        <div className="mt-4 flex flex-wrap gap-2">
          {buildBranch && (
            <a href={codeBrowserBranchUrl(buildBranch)} target="_blank" rel="noopener noreferrer"
               title={i18nT('pages.settings.aboutPanel.browse_this_branch_on_github')}
               className="inline-flex items-center gap-1.5 text-[12px] font-mono text-accent border rounded-lg px-2.5 py-1 no-underline hover:underline" style={ACCENT_TINT}>
              <GitBranch size={12} className="shrink-0" /> <span className="truncate max-w-[220px]">{buildBranch}</span> <ExternalLink size={10} className="opacity-60 shrink-0" />
            </a>
          )}
          {buildCommit && (
            <a href={codeBrowserCommitUrl(buildCommit)} target="_blank" rel="noopener noreferrer"
               title={i18nT('pages.settings.aboutPanel.view_this_commit_on_github')}
               className="inline-flex items-center gap-1.5 text-[12px] font-mono text-accent border rounded-lg px-2.5 py-1 no-underline hover:underline" style={ACCENT_TINT}>
              <GitCommitHorizontal size={12} className="shrink-0" /> {buildCommit} <ExternalLink size={10} className="opacity-60 shrink-0" />
            </a>
          )}
          <span className="inline-flex items-center gap-1.5 text-[12px] text-muted border border-border rounded-lg px-2.5 py-1 bg-bg"
                title={i18nT('pages.settings.aboutPanel.open_source_under_the_apache_2_0_license')}>
            <Scale size={12} className="shrink-0" /> {i18nT('pages.settings.aboutPanel.apache_2_0')}
          </span>
        </div>

        {isDesktop && channel && (
          info?.channelSwitchable && desktopApi?.setChannel ? (
            <div className="flex items-center justify-between py-1.5 text-sm gap-3" data-testid="channel-switcher">
              <div className="flex flex-col min-w-0">
                <span className="text-muted">{i18nT('pages.settings.aboutPanel.update_channel')}</span>
                <span className="text-[11.5px] text-muted opacity-80">
                  {i18nT('pages.settings.aboutPanel.insider_gets_prerelease_builds_early_switching_o')}
                </span>
              </div>
              <div className="shrink-0 flex items-center gap-2">
                {channelMutation.isPending && <RefreshCw size={13} className="lucide-inline animate-spin text-muted" />}
                <SegmentedControl
                  segments={[{ key: 'stable', label: 'Stable' }, { key: 'insider', label: 'Insider' }]}
                  value={channel === 'insider' ? 'insider' : 'stable'}
                  onChange={next => { if (next !== channel && !channelMutation.isPending) channelMutation.mutate(next) }}
                  layoutId="update-channel"
                  // Both lanes stay visible: the wrapper is shrink-0 (so the
                  // responsive measurement would be circular) and Card's
                  // .card-glow rule would trap a dropdown overlay under the
                  // Platform row below.
                  collapse={false}
                />
              </div>
            </div>
          ) : (
            <Row label={i18nT('pages.settings.aboutPanel.update_channel')}>{channel}</Row>
          )
        )}
        {isDesktop && info?.platform && <Row label={i18nT('pages.settings.aboutPanel.platform')}>{info.platform}</Row>}
      </Card>

      <Card>
        <CardTitle><RefreshCw size={15} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.updates')}</CardTitle>
        {isDesktop ? (
          updatesDisabled ? (
            <p className="text-sm text-muted">
              {updatesDisabled === 'dev'
                ? i18nT('pages.settings.aboutPanel.automatic_updates_unavailable_dev_build')
                : updatesDisabled === 'translocated'
                  ? i18nT('pages.settings.aboutPanel.automatic_updates_unavailable_translocated')
                  : updatesDisabled === 'volume'
                    ? i18nT('pages.settings.aboutPanel.automatic_updates_unavailable_volume')
                    : i18nT('pages.settings.aboutPanel.automatic_updates_unavailable_platform')}
            </p>
          ) : (
            <div className="flex flex-col gap-2.5">
              <p className="text-sm text-muted">
                {botName || 'Kiro Crew'} {i18nT('pages.settings.aboutPanel.checks_for_updates_automatically_you_can_also_ch')}
              </p>
              <div>
                <Btn primary onClick={() => checkMutation.mutate()} disabled={checking}>
                  <RefreshCw size={13} className={`lucide-inline ${checking ? 'animate-spin' : ''}`} /> {i18nT('pages.settings.aboutPanel.check_for_updates')}
                </Btn>
              </div>
              {status && <div className="text-[13px]">{status}</div>}
              {updateCard}
            </div>
          )
        ) : (
          <div className="flex flex-col gap-2.5">
            {showUpdate ? (
              <>
                <p className="text-sm text-muted flex items-center gap-1.5">
                  <ArrowUp size={13} className="lucide-inline text-accent" /> {i18nT('pages.settings.aboutPanel.a_new_version')}{gwTarget ? ` (v${gwTarget})` : ''} {i18nT('pages.settings.aboutPanel.is_available')}
                </p>
                <div>
                  <Btn primary onClick={() => { if (!gwChanges) gwCheck.mutate(); setApplyError(''); setRestarting(false); setShowConfirm(true) }}>
                    <ArrowUp size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.update')}{gwTarget ? ` to v${gwTarget}` : ' now'}
                  </Btn>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-muted">
                  {botName || 'Kiro Crew'} {i18nT('pages.settings.aboutPanel.checks_for_updates_automatically_you_can_also_ch')}
                </p>
                <div>
                  <Btn onClick={() => gwCheck.mutate()} disabled={gwCheck.isPending}>
                    <RefreshCw size={13} className={`lucide-inline ${gwCheck.isPending ? 'animate-spin' : ''}`} /> {i18nT('pages.settings.aboutPanel.check_for_updates')}
                  </Btn>
                </div>
                {gwCheck.isSuccess && !showUpdate && (
                  <span className="text-ok text-[13px] flex items-center gap-1.5"><CheckCircle2 size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.you_re_on_the_latest_version')}</span>
                )}
                {gwCheck.isError && (
                  <span className="text-danger text-[13px] flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.couldn_t_check_for_updates_2')}</span>
                )}
              </>
            )}
            <div className="flex items-center justify-between pt-2.5 border-t border-border"
              title={i18nT('pages.settings.aboutPanel.automatically_pull_and_apply_updates_when_the_ga')}>
              <span className="text-sm text-text">{i18nT('pages.settings.aboutPanel.auto_update_on_restart')}</span>
              <Toggle checked={autoUpdate} label={i18nT('pages.settings.aboutPanel.auto_update_on_restart')}
                onChange={async next => { setAutoUpdate(next); try { await api.setAutoUpdate(next) } catch { setAutoUpdate(!next) } }} />
            </div>
          </div>
        )}

        {/* Full changelog — collapsible; restores the changelog view removed with
            the top-bar pill. Shared across desktop + web. */}
        <div className="mt-3 pt-3 border-t border-border">
          <button
            type="button"
            aria-expanded={showFull}
            className="text-[13px] text-muted hover:text-text cursor-pointer bg-transparent border-none px-0"
            onClick={() => setShowFull(v => !v)}
          >
            {showFull ? '▾ Hide Full Changelog' : '▸ View Full Changelog'}
          </button>
          {showFull && (
            <div className="mt-2 p-3 bg-bg rounded-lg border border-border max-h-[360px] overflow-y-auto text-[13px] text-text">
              {changelogLoading ? (
                <span className="text-muted flex items-center gap-1.5"><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.loading_changelog')}</span>
              ) : fullChangelogError ? (
                <span className="text-danger flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.couldn_t_load_the_changelog')}</span>
              ) : fullChangelog ? (
                // DOMPurify-sanitize the fetched changelog source before rendering:
                // MarkdownRenderer uses rehype-raw (raw HTML passes through), so strip
                // any HTML/script the /api/changelog response could carry (defense-in-depth).
                <MarkdownRenderer content={safeChangelog} />
              ) : (
                <span className="text-muted">{i18nT('pages.settings.aboutPanel.no_changelog_available')}</span>
              )}
            </div>
          )}
        </div>
      </Card>

      {/* Web update confirm — shows the changelog, then applies (which restarts the gateway). */}
      {showConfirm && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise"
             role="dialog" aria-modal="true" aria-label={i18nT('pages.settings.aboutPanel.update')}
             onClick={() => { if (!gwApply.isPending && !restarting) setShowConfirm(false) }}>
          <div role="document" className="bg-card border border-border rounded-xl p-6 max-w-md w-full mx-4 shadow-xl" onClick={e => e.stopPropagation()}>
            <div className="flex justify-between items-center mb-3">
              <div className="text-sm font-bold text-text-strong flex items-center gap-1.5"><Package size={15} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.update')}{gwTarget ? ` to v${gwTarget}` : ''}</div>
              <button aria-label={i18nT('pages.settings.aboutPanel.close')} className="text-muted hover:text-text cursor-pointer bg-transparent border-none disabled:opacity-40 disabled:cursor-default" disabled={gwApply.isPending || restarting} onClick={() => { if (!gwApply.isPending && !restarting) setShowConfirm(false) }}><X size={15} /></button>
            </div>
            {gwCheck.isPending ? (
              <div className="text-[13px] text-muted flex items-center gap-1.5 mb-4"><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.loading_changelog')}</div>
            ) : gwChanges ? (
              <>
                <div className="text-[12px] font-medium text-muted uppercase tracking-wider mb-2">{i18nT('pages.settings.aboutPanel.what_s_new')}</div>
                <div className="p-3 bg-bg rounded-lg border border-border max-h-56 overflow-y-auto mb-4 text-[13px] text-text"><MarkdownRenderer content={gwChanges} /></div>
              </>
            ) : (
              <p className="text-[13px] text-muted mb-4">{i18nT('pages.settings.aboutPanel.a_newer_version_is_available')}</p>
            )}
            <p className="text-[12px] text-muted mb-3">{i18nT('pages.settings.aboutPanel.updating_restarts_the_gateway_active_sessions_wi')}</p>
            {applyError && <div className="text-[13px] text-danger mb-3 flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> {applyError}</div>}
            {restarting ? (
              <div className="text-[13px] text-accent flex items-center justify-center gap-1.5 py-2" role="status">
                <RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.updating_gateway_restarting')}
              </div>
            ) : (
              <Btn primary className="w-full justify-center" disabled={gwApply.isPending} onClick={() => gwApply.mutate()}>
                {gwApply.isPending ? <><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.updating')}</> : 'Update now'}
              </Btn>
            )}
          </div>
        </div>
      )}
    </>
  )
}
