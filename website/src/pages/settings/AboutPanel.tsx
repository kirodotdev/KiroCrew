import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { RefreshCw, Scale, CheckCircle2, AlertCircle, Bug, GitBranch, GitCommitHorizontal, ExternalLink, ArrowUp, History, Package, X, Download, Copy } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Progress } from '@/components/ui/progress'
import { Card, CardTitle, Btn, Toggle } from '../../components/ui'
import { useBranding } from '../../hooks/useBranding'
import { useAppSelector } from '../../store'
import { codeBrowserBranchUrl, codeBrowserCommitUrl } from '../../lib/codeBrowser'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import SegmentedControl from '../../components/SegmentedControl'
import ReportProblemCard from './ReportProblemCard'
import { api, ApiError } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'

import { i18nT } from '../../i18n/t'
import { fmtDateTimeNumeric, fmtRelative } from '../../i18n/format'
import type { UpdateState } from '../../hooks/useUpdateSubscription'

/** Human-readable transfer rate for the progress label. */
function formatRate(bps: number): string {
  if (!Number.isFinite(bps) || bps <= 0) return ''
  const mb = bps / (1024 * 1024)
  return mb >= 1 ? `${mb.toFixed(1)} MB/s` : `${Math.round(bps / 1024)} KB/s`
}

/**
 * Why the GATEWAY update check produced no verdict.
 *
 * Distinct from `updateErrorText` below, which speaks for the Electron updater's
 * download/install lifecycle. These codes come from `/api/update/check` and mean
 * "the comparison did not happen" — never "you are up to date".
 *
 * An unrecognised code deliberately falls back to the generic reason instead of
 * being dropped: a newer gateway paired with an older bundle must still say the
 * check failed rather than silently render the success line.
 */
const GATEWAY_CHECK_ERROR_KEYS: Record<string, string> = {
  feed_unreachable: 'pages.settings.aboutPanel.update_check_error_feed_unreachable',
  feed_malformed: 'pages.settings.aboutPanel.update_check_error_feed_malformed',
  git_fetch_failed: 'pages.settings.aboutPanel.update_check_error_git_fetch_failed',
  git_read_failed: 'pages.settings.aboutPanel.update_check_error_git_read_failed',
  version_unparseable: 'pages.settings.aboutPanel.update_check_error_version_unparseable',
  // Not failures: this gateway is not the update surface for the install it is
  // running inside. A desktop bundle embeds this same backend, so it reaches this
  // code and must defer to the Electron updater; a container is replaced by
  // pulling a new image.
  managed_by_app: 'pages.settings.aboutPanel.update_check_managed_by_app',
  managed_by_image: 'pages.settings.aboutPanel.update_check_managed_by_image',
}

function gwCheckErrorText(code: string): string {
  const key = GATEWAY_CHECK_ERROR_KEYS[code]
  return i18nT(key || 'pages.settings.aboutPanel.update_check_error_unknown')
}

/**
 * User-facing copy for a failure class. `message` from the updater is raw
 * library text (multi-line HttpError dumps, digest comparisons), so it is only
 * used as a last-resort detail for an unclassified failure.
 */
/**
 * Failure class → catalog key, written out in full.
 *
 * Each key is a plain string literal rather than a concatenation like
 * `i18nT(ap + 'update_error_offline')`: a concatenated key is invisible to
 * static analysis, so no extractor, linter or unused-key tool can see it — the
 * keys would look dead and a pruning pass would delete them. A missing key then
 * takes the whole panel down through the error boundary (see the `server`
 * branch below).
 *
 * `as const` on the literal map keeps the keys findable by tooling while the
 * lookup stays a single expression.
 */
const UPDATE_ERROR_KEYS = {
  offline: 'pages.settings.aboutPanel.update_error_offline',
  serverStatus: 'pages.settings.aboutPanel.update_error_server_status',
  server: 'pages.settings.aboutPanel.update_error_server',
  noRelease: 'pages.settings.aboutPanel.update_error_no_release',
  integrity: 'pages.settings.aboutPanel.update_error_integrity',
  misconfigured: 'pages.settings.aboutPanel.update_error_misconfigured',
  unknown: 'pages.settings.aboutPanel.update_error_unknown',
  installUnknown: 'pages.settings.aboutPanel.update_error_install_unknown',
} as const

function updateErrorText(st: UpdateState | null | undefined): string {
  switch (st?.code) {
    case 'offline': return i18nT(UPDATE_ERROR_KEYS.offline)
    case 'server': {
      // Guard the interpolation: i18nT returns undefined for a key missing from
      // every catalog, and calling .replace() on that would take the whole panel
      // down via the error boundary. A status-less fallback is strictly better
      // than a blank Settings page.
      const template = i18nT(UPDATE_ERROR_KEYS.serverStatus)
      return st.httpStatus && typeof template === 'string'
        ? template.replace('{{status}}', String(st.httpStatus))
        : i18nT(UPDATE_ERROR_KEYS.server)
    }
    case 'no-release': return i18nT(UPDATE_ERROR_KEYS.noRelease)
    case 'integrity': return i18nT(UPDATE_ERROR_KEYS.integrity)
    case 'misconfigured': return i18nT(UPDATE_ERROR_KEYS.misconfigured)
    // Unclassified failure. The localized generic WINS over st.message: the raw
    // value is electron-updater's exception text, written for a developer reading
    // logs ("ShipIt could not replace the application bundle") and always English.
    // The detail still reaches the log via the main process; only fall
    // back to it if the catalog key is somehow missing, since a raw string beats
    // an empty error line.
    //
    // The INSTALL phase gets its own generic: the shared one advises "try
    // checking for updates again", which sits directly beside the card's Retry
    // button — two conflicting next steps for the same failure. The install
    // copy names what failed and leaves the next step to the card's controls.
    default: return i18nT(st?.phase === 'install' ? UPDATE_ERROR_KEYS.installUnknown : UPDATE_ERROR_KEYS.unknown) || st?.message || ''
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

/**
 * Where the prerelease note sends a bug report.
 *
 * Same endpoint as `prompts/featureRequest.ts` FEATURE_REQUEST_URL, deliberately
 * NOT imported from it: that constant is named for (and used by) the guided
 * feature-request flow, and a rename or a redirect to an in-app form there must
 * not silently retarget this link.
 */
const REPORT_ISSUE_URL = 'https://github.com/kirodotdev/KiroCrew/issues/new'

/**
 * How long a primed Restart button stays armed.
 *
 * Long enough to read the changed label and click again, short enough that an
 * armed control does not sit there as a trap for an unrelated click a minute
 * later.
 */
const ARM_TIMEOUT_MS = 5000

/**
 * Last-resort prerelease test for an info payload with NO channel fields.
 *
 * `electron/main.js` has an init-failure fallback whose getInfo() returns only
 * `{version, packaged}` (updater handle `disabled: "init-failed"`), so both
 * `stampedChannel` and `channel` are absent there. Without this the note would
 * hide from a packaged insider/nightly build precisely when its updater is
 * broken — the user most likely to have something worth reporting.
 *
 * Mirrors auto-update.js channelForVersion's rule as far as it needs to: a bare
 * semver is stable, ANY prerelease suffix (-insider.N, -nightly.<stamp>, -rc.N)
 * is not. It deliberately does not try to name WHICH lane, because the copy no
 * longer interpolates the channel.
 */
function versionLooksPrerelease(version: string | undefined): boolean {
  return !!version && version.includes('-')
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

/**
 * Restart control with a two-step confirm.
 *
 * A restart drops every active session, so it must not fire on a single stray
 * click — but it is also recoverable (the gateway comes straight back), which is
 * why this is an armed button rather than the typed-token treatment reserved for
 * bulk-destructive actions.
 *
 * The arm auto-expires: an armed control left on screen becomes a trap for the
 * next click minutes later, when the user has forgotten what they armed. The
 * timer is cleared on unmount so a restart that navigates away cannot fire
 * `setState` on a dead component.
 *
 * Rendered at two call sites with the same behaviour and different emphasis, so
 * the confirm step cannot drift between them.
 */
function RestartGatewayButton({
  primary,
  pending,
  restarting,
  onConfirm,
  testId,
}: {
  primary?: boolean
  pending: boolean
  restarting: boolean
  onConfirm: () => void
  testId: string
}) {
  const [armed, setArmed] = useState(false)
  useEffect(() => {
    if (!armed) return
    const timer = window.setTimeout(() => setArmed(false), ARM_TIMEOUT_MS)
    return () => window.clearTimeout(timer)
  }, [armed])

  const busy = pending || restarting
  return (
    <Btn
      primary={primary && !armed}
      className={armed ? '!bg-[var(--warn)] !text-[var(--warn-fg)] hover:!opacity-80' : undefined}
      disabled={busy}
      data-testid={testId}
      // No static aria-label: the visible text IS the accessible name, and it is
      // what changes to announce the armed step. Pinning the label to "Restart
      // gateway" made a screen reader say the same thing on both clicks, hiding
      // the confirm from exactly the users who cannot see the colour change.
      onClick={() => {
        if (busy) return
        if (!armed) { setArmed(true); return }
        setArmed(false)
        onConfirm()
      }}
    >
      <RefreshCw size={13} className={`lucide-inline ${busy ? 'animate-spin' : ''}`} /> {restarting
        ? i18nT('pages.settings.aboutPanel.restarting')
        : armed
          ? i18nT('pages.settings.aboutPanel.restart_confirm')
          : i18nT('pages.settings.aboutPanel.restart_gateway')}
    </Btn>
  )
}

export function AboutPanel() {
  const { botName, avatar } = useBranding()
  const gatewayVersion = useAppSelector(s => s.dashboard.status?.version) || ''
  const buildBranch = useAppSelector(s => s.dashboard.status?.branch) || ''
  const buildCommit = useAppSelector(s => s.dashboard.status?.commit) || ''
  // `=== true` because the verdict is nullable: null means a check that never
  // ran or one that failed, and neither may light an update affordance.
  const updateAvailable = useAppSelector(s => s.dashboard.status?.update_available) === true
  // Undefined on a gateway that predates the field; `!== false` below is what
  // keeps that case behaving as before.
  const statusSelfUpdatable = useAppSelector(s => s.dashboard.status?.update_can_apply)
  // The background check's own verdict + command, so the 12-hourly check that
  // lights the nav badge lands the user on something actionable instead of an
  // Update button that 409s.
  const statusChecked = useAppSelector(
    s => s.dashboard.status?.update_check_status
  ) === 'succeeded'
  const statusCommand = useAppSelector(s => s.dashboard.status?.update_command) || ''
  const lastCheckedAt = useAppSelector(s => s.dashboard.status?.update_last_checked_at) ?? null
  const checkIntervalSecs = useAppSelector(s => s.dashboard.status?.update_check_interval_secs) ?? 43200
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
  // isPending alone lets the button re-arm during that window, so the user sees
  // a clickable "Restart & Update" followed by an unexplained quit -- which reads
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

  // "What's the difference?" disclosure next to the channel switcher. Collapsed
  // by default: the identity card is the densest surface in Settings, and the
  // explanation is reference material — needed once, when choosing.
  const [showChannelHelp, setShowChannelHelp] = useState(false)
  // The report ask is about the BYTES CURRENTLY RUNNING, so it keys on
  // stampedChannel (the build's own lane) and NOT on `channel`, which is the
  // feed being FOLLOWED: auto-update.js resolveChannel() returns the user's
  // switcher preference for any production build, so the two diverge for the
  // whole window between flipping the switcher and the other channel's build
  // actually landing. Keying on `channel` inverts the feature in both
  // directions — it hides the ask from someone still running insider bytes who
  // just opted back to stable, and shows "less tested than Stable" to someone
  // on a stable build who just opted into insider.
  //
  // Any non-stable lane ships less-tested bytes, so nightly is included as well
  // as insider. Nightly reports channelSwitchable=false (it is a pinned
  // side-by-side install), so the note is rendered OUTSIDE the
  // switchable/pinned branch below to cover both. An unstamped dev build has
  // stampedChannel=null and correctly gets no ask — there is no published
  // release for its bytes to be "less tested" than.
  //
  // ABSENT (undefined) is a third case, distinct from null: main.js's
  // init-failure fallback reports neither channel field, so fall back to the
  // version string for a packaged build. `null` keeps meaning "dev, no lane".
  // Desktop reports its own lane through the updater handle; a CLI/wheel
  // install has no updater handle at all, so the gateway's resolved
  // `release_channel` is the only source there. Preferring `stampedChannel`
  // when present keeps the desktop answer authoritative (it knows which FEED
  // the build tracks, not just how its version reads).
  const gatewayChannel = useAppSelector(s => s.dashboard.status?.release_channel)
  // The channel this INSTALL follows, as opposed to the lane the running bytes
  // were built on (`release_channel` above). Empty on a layout with no channel
  // at all — a git checkout, a desktop bundle, a container — which is exactly
  // when the switcher must not be offered, because the backend refuses it.
  const statusUpdateChannel = useAppSelector(s => s.dashboard.status?.update_channel) || ''
  const isPrerelease = info?.stampedChannel === undefined
    ? (!!info?.packaged && versionLooksPrerelease(info?.version))
      || (!isDesktop && !!gatewayChannel && gatewayChannel !== 'stable')
    : !!info.stampedChannel && info.stampedChannel !== 'stable'

  // Desktop status line under the Check button (simple states only — the
  // found/downloading/downloaded lifecycle renders as the update card below).
  let status: React.ReactNode = null
  if (checking) {
    status = <span className="text-muted flex items-center gap-1.5"><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.checking_for_updates')}</span>
  } else if (updateState?.state === 'not-available') {
    status = <span className="text-ok flex items-center gap-1.5"><CheckCircle2 size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.you_are_on_the_latest_version')}</span>
  } else if (updateState?.state === 'error' && updateState.phase !== 'download' && updateState.phase !== 'install') {
    // Download failures are NOT rendered here: they render inside the update
    // card so the found version stays on screen and can be retried.
    status = <span className="text-danger flex items-center gap-1.5"><AlertCircle size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.couldn_t_check_for_updates')}: {updateErrorText(updateState)}</span>
  }

  // Update card: shown whenever an update is found / downloading / ready.
  const cardState = updateState?.state
  // A download-phase failure keeps the card: the user consented to this
  // version, so losing it on a transient error would strand them with a check
  // complaint and no way back.
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
            {botName || 'Kiro Crew'} {updateState?.version || i18nT('pages.settings.aboutPanel.update_noun')}
          </span>
          <span className="text-[12px] text-muted">
            {channel ? `${channel} channel` : i18nT('pages.settings.aboutPanel.update_noun')}
            {cardPubDate && !isNaN(cardPubDate.getTime()) ? ` · ${i18nT('pages.settings.aboutPanel.published', { when: fmtDateTimeNumeric(cardPubDate) })}` : ''}
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
                  : (<><Download size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.download_install')}</>)}
            </Btn>
          )}
        </div>
      </div>
      {cardState === 'downloading' && (
        <>
          {/* value={null} = indeterminate (before the first download-progress
              event): Radix drops aria-valuenow and the indicator sweeps instead
              of filling -- a filled bar with no real value reads as progress
              and then jumps when the true percent arrives. */}
          <Progress value={cardPercent} data-testid="update-progress" />
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
  // The honesty trio, straight from /api/update/check.
  //
  // `gwChecked` is what licenses the "you're on the latest version" line. It used
  // to be enough that the request returned 200 — but for a wheel install the
  // backend's check never actually ran, so a check that did nothing reported an
  // out-of-date install as up to date. A 200 is now only a transport success;
  // `checked` is the verdict, and `gwError` names why there is none.
  const [gwChecked, setGwChecked] = useState(false)
  const [gwError, setGwError] = useState('')
  // Why there is no verdict when nothing FAILED: this gateway is not the update
  // surface for the install it runs inside. Separate from `gwError` because
  // rendering a deferral under "Couldn't check for updates" would be its own
  // lie — nothing broke, the update arrives through a different surface.
  const [gwUnavailableReason, setGwUnavailableReason] = useState('')
  // Null = not yet known from a check; the redux status flag below carries the
  // same fact for the pre-check case.
  const [gwSelfUpdatable, setGwSelfUpdatable] = useState<boolean | null>(null)
  const [gwChannel, setGwChannel] = useState('')
  // Server-supplied reason for a refused switch; '' means 'no detail, use the generic line'.
  const [gwChannelError, setGwChannelError] = useState('')
  const [gwCommand, setGwCommand] = useState('')
  const [gwCommandCopied, setGwCommandCopied] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [applyError, setApplyError] = useState('')
  const [restarting, setRestarting] = useState(false)
  const [autoUpdate, setAutoUpdate] = useState(true)
  const { data: mcCfg } = useQuery({ queryKey: ['mc-config-autoupdate'], queryFn: () => api.kirocrewConfig() })
  useEffect(() => {
    const v = (mcCfg as any)?.auto_update
    if (typeof v === 'boolean') setAutoUpdate(v)
  }, [mcCfg])
  const gwCheck = useMutation({
    mutationFn: () => api.checkUpdate(),
    onSuccess: (d: any) => {
      setGwChanges(d?.changes || '')
      // `latest_version` is the field the gateway actually emits; `version` is
      // read as a fallback only because it is what some older payloads carried.
      const target = d?.latest_version || d?.version
      if (target) setGwTarget(String(target))
      // Derive availability from the check response itself, not only the redux
      // status flag (which refreshes on a slower WS status push). Otherwise a
      // check that finds an update could still show "You're on the latest
      // version" until the flag catches up.
      setGwFound(d?.update_available === true)
      setGwChecked(d?.check_status === 'succeeded')
      // A DEFERRAL is not a failure: a desktop bundle reporting "the app
      // updates itself" has not malfunctioned, and its reason has its own slot.
      // Only `error_code` may render as an error.
      setGwError(typeof d?.error_code === 'string' ? d.error_code : '')
      setGwUnavailableReason(
        typeof d?.unavailable_reason === 'string' ? d.unavailable_reason : ''
      )
      setGwChannel(typeof d?.channel === 'string' ? d.channel : '')
      setGwCommand(
        typeof d?.remediation?.command === 'string' ? d.remediation.command : ''
      )
      setGwCommandCopied(false)
      if (typeof d?.can_apply === 'boolean') setGwSelfUpdatable(d.can_apply)
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
      if (e instanceof ApiError) setApplyError(e.message || i18nT('pages.settings.aboutPanel.update_failed'))
      else setRestarting(true)
    },
  })
  // Restart WITHOUT updating: the missing half of the non-self-updatable flow.
  // After the user runs the copied installer command in a terminal, the running
  // gateway is still executing the old code and had no in-app way to reload.
  //
  // Same error shape as gwApply: os.execv kills the connection, so a bare
  // network failure after the POST is the expected path, and only an ApiError
  // (a real server rejection) is a failure worth showing.
  const gwRestart = useMutation({
    mutationFn: () => api.restartGateway(),
    onSuccess: () => setRestarting(true),
    onError: (e: unknown) => {
      if (e instanceof ApiError) setApplyError(e.message || i18nT('pages.settings.aboutPanel.restart_failed'))
      else setRestarting(true)
    },
  })
  // Gateway channel switch (stable | insider | nightly). Unlike the desktop
  // switcher this is a THREE-way control: cli.sh publishes and installs all
  // three lanes, so every one of them is a real destination for a wheel install.
  // Refuses on a git checkout or an externally managed layout (409), which is
  // why the control is only rendered when the backend reported a channel.
  const gwChannelMutation = useMutation({
    mutationFn: (next: string) => api.setUpdateChannel(next),
    onMutate: () => setGwChannelError(''),
    onError: (e: unknown) => {
      // The backend's 409s carry the only actionable detail there is ("a git
      // checkout follows its git remote", or the externally-managed guidance
      // naming the real update surface). A bare "Couldn't switch channel" leaves
      // the user with no next step, so surface the server's reason when it gave
      // one and fall back to the generic line when it did not.
      setGwChannelError(e instanceof ApiError ? (e.message || '') : '')
    },
    onSuccess: (d: any) => {
      // The response is the re-run check against the new channel, so adopt it
      // wholesale rather than leaving the previous lane's verdict on screen.
      //
      // These MUST stay the same field names `gwCheck.onSuccess` reads: both
      // handlers consume the same update-check contract, and when this one was
      // left on the old names a successful switch that FOUND an update wrote
      // `gwFound=false` / `gwChecked=false` and blanked the target — silently
      // discarding the very verdict the switch was made to get.
      if (typeof d?.channel === 'string') setGwChannel(d.channel)
      setGwFound(d?.update_available === true)
      setGwChecked(d?.check_status === 'succeeded')
      // Only `error_code` may render as an error; a deferral is not a failure.
      setGwError(typeof d?.error_code === 'string' ? d.error_code : '')
      setGwCommand(typeof d?.update_command === 'string' ? d.update_command : '')
      setGwCommandCopied(false)
      setGwTarget(typeof d?.latest_version === 'string' ? d.latest_version : '')
      if (typeof d?.can_apply === 'boolean') setGwSelfUpdatable(d.can_apply)
    },
  })
  // Update is available if either the redux status flag or the latest check
  // response says so.
  const showUpdate = updateAvailable || gwFound
  // Can this install apply the update itself? A fresh check wins; before one has
  // run, the redux status flag carries the same fact from the gateway's own boot
  // check. Defaulting to TRUE when neither is known preserves the historical
  // behaviour for git checkouts (the only layout that could ever report an
  // update before this change).
  const gwSelfUpdate =
    gwSelfUpdatable !== null ? gwSelfUpdatable : statusSelfUpdatable !== false
  // A manual check's command wins; otherwise fall back to the one the background
  // check shipped in status. Without the fallback, a badge-driven visit had no
  // command and fell through to the Update button — the doomed 409 path.
  const effectiveCommand = gwCommand || statusCommand
  // An update exists and this install cannot pull it in. Note this does NOT
  // require a command: when `gwSelfUpdate` is false the Update button must be
  // suppressed unconditionally, because it POSTs to an endpoint that answers 409
  // for this layout. A missing command degrades to an explanation, never to a
  // button that cannot work.
  // A manual check's / switch's answer wins; otherwise the background check's,
  // shipped in status. Empty means this layout has no channel to switch.
  const effectiveGwChannel = gwChannel || statusUpdateChannel
  const showGwChannelSwitcher = !isDesktop && !!effectiveGwChannel
  // Nightly is not an OFFERABLE destination on any surface. Its builds are
  // untested `main` HEAD, so following that lane is a deliberate install
  // (`cli.sh --channel nightly`), never a one-click flip from a control that
  // sits next to Stable and reads like a third equal option.
  //
  // The segment is rendered only when this install is ALREADY on nightly —
  // either the lane it follows or the lane the running bytes came from — which
  // keeps the control truthful for a nightly user (a two-segment control with
  // neither segment selected shows no indicator at all) and leaves them a
  // one-click exit back to Stable. Reading BOTH channels matters for the window
  // after that click: the followed lane is stable while the running build is
  // still nightly, and dropping the segment there would strand an accidental
  // click with no way back. There is no path IN: a stable/insider install never
  // renders it. This matches the desktop switcher, which has only ever offered
  // stable ⇄ insider.
  const gwNightlyLane = effectiveGwChannel === 'nightly' || gatewayChannel === 'nightly'
  // Moving lanes needs the installer command whether or not the target lane's
  // version is numerically NEWER. Switching from nightly back to stable is a
  // downgrade, and the command is still the only thing that performs it — so
  // gating the command on `available` alone left the switcher's own note ("run the
  // command below") pointing at nothing in exactly that case.
  const channelMovePending =
    !isDesktop && !!effectiveGwChannel && !!gatewayChannel && effectiveGwChannel !== gatewayChannel
  const showManualUpdate = (showUpdate || channelMovePending) && !gwSelfUpdate

  // Escape closes the confirm dialog (unless an apply/restart is in flight).
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
                : (gwChecked || statusChecked)
                  ? <span className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold rounded-full px-2 py-0.5"
                      style={{ color: 'var(--ok)', background: 'color-mix(in oklab, var(--ok) 14%, transparent)' }}
                      data-testid="hero-up-to-date">
                    <span className="w-1.5 h-1.5 rounded-full inline-block" style={{ background: 'var(--ok)' }} /> {i18nT('pages.settings.aboutPanel.up_to_date')}</span>
                  // No verdict yet (never checked, or the check failed). A green
                  // "Up to date" here is the same half-truth the check contract
                  // kills: it would sit beside a red "Couldn't check for updates"
                  // on this very screen. Stay neutral until something is known.
                  : <span className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold rounded-full px-2 py-0.5 text-muted bg-bg-accent"
                      data-testid="hero-not-checked">
                    <span className="w-1.5 h-1.5 rounded-full inline-block bg-muted" /> {i18nT('pages.settings.aboutPanel.not_checked_yet')}</span>
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
            <div className="flex flex-col" data-testid="channel-switcher">
              <div className="flex items-center justify-between py-1.5 text-sm gap-3">
                <div className="flex flex-col items-start min-w-0">
                  <span className="text-muted">{i18nT('pages.settings.aboutPanel.update_channel')}</span>
                  <button
                    type="button"
                    aria-expanded={showChannelHelp}
                    data-testid="channel-help-toggle"
                    // Underlined AT REST, unlike the changelog disclosure lower
                    // in this file: that one is the only interactive thing in
                    // its row, while this one sits beside a full-size segmented
                    // control that wins every eye. A first-time reader read the
                    // un-underlined version as a category tint and never
                    // clicked, then flipped the channel without reading.
                    className="text-[11.5px] text-accent underline decoration-dotted underline-offset-2 hover:decoration-solid cursor-pointer bg-transparent border-none p-0 text-left"
                    onClick={() => setShowChannelHelp(v => !v)}
                  >
                    {showChannelHelp
                      ? i18nT('pages.settings.aboutPanel.channel_help_hide')
                      : i18nT('pages.settings.aboutPanel.channel_help_show')}
                  </button>
                </div>
                <div className="shrink-0 flex items-center gap-2">
                  {channelMutation.isPending && <RefreshCw size={13} className="lucide-inline animate-spin text-muted" />}
                  <SegmentedControl
                    segments={[{ key: 'stable', label: i18nT('pages.settings.aboutPanel.stable') }, { key: 'insider', label: i18nT('pages.settings.aboutPanel.insider') }]}
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
              {showChannelHelp && (
                // Term + definition rows rather than one prose sentence per
                // channel: the channel names are the same tokens the segmented
                // control shows, so reusing the `stable` / `insider` keys keeps
                // label and explanation from drifting apart per locale.
                <div className="mb-1 p-2.5 bg-bg rounded-lg border border-border flex flex-col gap-1.5 text-[12px]" data-testid="channel-help">
                  <div className="flex gap-2">
                    <span className="font-medium text-text shrink-0">{i18nT('pages.settings.aboutPanel.stable')}</span>
                    <span className="text-muted">{i18nT('pages.settings.aboutPanel.channel_explainer_stable')}</span>
                  </div>
                  <div className="flex gap-2">
                    <span className="font-medium text-text shrink-0">{i18nT('pages.settings.aboutPanel.insider')}</span>
                    <span className="text-muted">{i18nT('pages.settings.aboutPanel.channel_explainer_insider')}</span>
                  </div>
                  <span className="text-muted opacity-80 pt-1.5 border-t border-border">
                    {i18nT('pages.settings.aboutPanel.channel_explainer_switch_note')}
                  </span>
                </div>
              )}
            </div>
          ) : (
            <Row label={i18nT('pages.settings.aboutPanel.update_channel')}>{channel}</Row>
          )
        )}
        {showGwChannelSwitcher && (
          // Gateway (CLI / wheel / cloud source) channel switcher: the same two
          // lanes the desktop offers, stable ⇄ insider. Nightly is a deliberate
          // pinned install rather than a destination this control hands out, so
          // its segment appears only for an install already on that lane (see
          // `gwNightlyLane`) — as an exit, never an entrance.
          //
          // Switching persists the preference and re-checks; it never installs.
          // The new lane's build then arrives through the normal Update surface
          // below, so a channel change is never an unconsented version jump.
          <div className="flex flex-col" data-testid="gateway-channel-switcher">
            <div className="flex items-center justify-between py-1.5 text-sm gap-3">
              <div className="flex flex-col items-start min-w-0">
                <span className="text-muted">{i18nT('pages.settings.aboutPanel.update_channel')}</span>
                <button
                  type="button"
                  aria-expanded={showChannelHelp}
                  data-testid="gateway-channel-help-toggle"
                  className="text-[11.5px] text-accent underline decoration-dotted underline-offset-2 hover:decoration-solid cursor-pointer bg-transparent border-none p-0 text-left"
                  onClick={() => setShowChannelHelp(v => !v)}
                >
                  {showChannelHelp
                    ? i18nT('pages.settings.aboutPanel.channel_help_hide')
                    // The prompt names exactly the lanes the control shows: the
                    // three-lane wording on a two-lane control advertises a
                    // channel the user cannot pick here.
                    : gwNightlyLane
                      ? i18nT('pages.settings.aboutPanel.channel_help_show_all')
                      : i18nT('pages.settings.aboutPanel.channel_help_show')}
                </button>
              </div>
              <div className="shrink-0 flex items-center gap-2">
                {gwChannelMutation.isPending && <RefreshCw size={13} className="lucide-inline animate-spin text-muted" />}
                <SegmentedControl
                  segments={[
                    { key: 'stable', label: i18nT('pages.settings.aboutPanel.stable') },
                    { key: 'insider', label: i18nT('pages.settings.aboutPanel.insider') },
                    ...(gwNightlyLane
                      ? [{ key: 'nightly', label: i18nT('pages.settings.aboutPanel.nightly') }]
                      : []),
                  ]}
                  value={effectiveGwChannel}
                  onChange={next => {
                    if (next !== effectiveGwChannel && !gwChannelMutation.isPending) gwChannelMutation.mutate(next)
                  }}
                  layoutId="gateway-update-channel"
                  collapse={false}
                />
              </div>
            </div>
            {showChannelHelp && (
              <div className="mb-1 p-2.5 bg-bg rounded-lg border border-border flex flex-col gap-1.5 text-[12px]" data-testid="gateway-channel-help">
                <div className="flex gap-2">
                  <span className="font-medium text-text shrink-0">{i18nT('pages.settings.aboutPanel.stable')}</span>
                  <span className="text-muted">{i18nT('pages.settings.aboutPanel.channel_explainer_stable')}</span>
                </div>
                <div className="flex gap-2">
                  <span className="font-medium text-text shrink-0">{i18nT('pages.settings.aboutPanel.insider')}</span>
                  <span className="text-muted">{i18nT('pages.settings.aboutPanel.channel_explainer_insider')}</span>
                </div>
                {/* Explained only where it is selectable. A definition for a lane
                    the control does not offer reads as an invitation to look for
                    the missing segment. */}
                {gwNightlyLane && (
                  <div className="flex gap-2">
                    <span className="font-medium text-text shrink-0">{i18nT('pages.settings.aboutPanel.nightly')}</span>
                    <span className="text-muted">{i18nT('pages.settings.aboutPanel.channel_explainer_nightly')}</span>
                  </div>
                )}
                <span className="text-muted opacity-80 pt-1.5 border-t border-border">
                  {i18nT('pages.settings.aboutPanel.channel_explainer_gateway_switch_note')}
                </span>
              </div>
            )}
            {gwChannelMutation.isError && (
              <span className="text-[12px] text-danger flex items-start gap-1.5" data-testid="gateway-channel-error">
                <AlertCircle size={13} className="lucide-inline shrink-0" />
                <span>
                  {i18nT('pages.settings.aboutPanel.channel_switch_failed')}
                  {gwChannelError ? `: ${gwChannelError}` : ''}
                </span>
              </span>
            )}
            {/* Switching re-points the FEED; it installs nothing. The segmented
                control highlights the new lane the moment the switch succeeds, so
                without this line the UI reads as "you are on Nightly now" when no
                bytes have moved. It must not hide behind the disclosure: the
                misreading happens precisely to the user who did not open it.

                Requires a command to point at: the sentence says "run the
                command below", so with no command resolved (failed check, offline
                host) it would dangle exactly like the `available`-gated version
                did.

                Stands down while the explainer is open, because the explainer's
                closing line is this very sentence and the two rendered back to
                back read as a stutter at exactly the moment the user is reading
                carefully.

                Shown only while the followed channel differs from the lane the
                RUNNING bytes came from — i.e. exactly the window where the two
                disagree. Once the new lane's build is installed they converge and
                the line retires itself. */}
            {!gwChannelMutation.isError
              && !showChannelHelp
              && !!effectiveCommand
              && !!gatewayChannel
              && effectiveGwChannel !== gatewayChannel && (
              <span className="text-[12px] text-muted flex items-start gap-1.5"
                data-testid="gateway-channel-pending-note">
                <ArrowUp size={13} className="lucide-inline shrink-0 text-accent" />
                <span>{i18nT('pages.settings.aboutPanel.channel_explainer_gateway_switch_note')}</span>
              </span>
            )}
          </div>
        )}
        {isPrerelease && (
          // NOT behind the disclosure: a user already running prerelease bytes
          // is exactly who must see the ask, and hiding it behind a click means
          // the people whose bug reports matter most never read it.
          //
          // NOT gated on `isDesktop` either, which is what it used to be: a
          // wheel install is a first-class insider/nightly lane (release.yml
          // publishes to cli/<channel>/), and gating on the desktop shell meant
          // every CLI prerelease user — the ones with no updater and no app
          // menu — saw nothing here at all.
          // Deliberately NOT warn-tinted with an alert triangle: a first-time
          // reader took that as "something is wrong with my installation" and
          // was reluctant to click a link inside it. This is a request for help,
          // so it speaks in the app's own accent voice, and the anchor names
          // GitHub because the destination is a new-issue form — a surprise
          // worth spending four words to avoid.
          <div className="flex items-start gap-2 p-2.5 rounded-lg border border-border bg-[var(--accent-subtle)] text-[12px]"
               data-testid="prerelease-report-note">
            <Bug size={13} className="lucide-inline shrink-0 mt-0.5 text-accent" />
            <span className="text-text leading-relaxed">
              {/* ONE catalog string carrying the anchor: splitting it into
                  fragments around the link would lock every language into
                  English clause order. `Trans` (not a hand-rolled split on a
                  mustache literal) is what lets the translator move the anchor
                  to wherever the target grammar needs it. */}
              <Trans
                i18nKey="pages.settings.aboutPanel.prerelease_report_prompt"
                components={{
                  // eslint-disable-next-line jsx-a11y/anchor-has-content, jsx-a11y/control-has-associated-label
                  report: <a href={REPORT_ISSUE_URL} target="_blank" rel="noreferrer" className="text-accent hover:underline" />,
                }}
              />
            </span>
          </div>
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
                    : updatesDisabled === 'channel'
                      // Distinct from the platform message because the fix is
                      // different and it is in this same panel: the platform has
                      // an update lane, just not on the channel this install
                      // tracks, so switching channels above restores updates.
                      // Falling through to the platform string would blame the
                      // OS and hide the way back.
                      ? i18nT('pages.settings.aboutPanel.automatic_updates_unavailable_channel')
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
            {(showUpdate || channelMovePending) ? (
              <>
                {showUpdate && (
                  <p className="text-sm text-muted flex items-center gap-1.5">
                    <ArrowUp size={13} className="lucide-inline text-accent" /> {i18nT('pages.settings.aboutPanel.a_new_version')}{gwTarget ? ` (v${gwTarget})` : ''} {i18nT('pages.settings.aboutPanel.is_available')}
                  </p>
                )}
                {showManualUpdate ? (
                  // This install cannot replace its own code (a `cli.sh` wheel
                  // install, not a git checkout), so there is no Update button to
                  // offer — pressing one would 409. Show the command that does
                  // work instead. The channel is spelled out in it deliberately:
                  // the installer defaults to stable and never reads the channel
                  // file, so a bare re-run would silently move this install to a
                  // different lane.
                  <div className="flex flex-col gap-2" data-testid="manual-update-instructions">
                    <p className="text-[13px] text-muted">
                      {effectiveGwChannel
                        ? i18nT('pages.settings.aboutPanel.this_install_updates_by_re_running_the_installer_channel', { channel: effectiveGwChannel })
                        : i18nT('pages.settings.aboutPanel.this_install_updates_by_re_running_the_installer')}
                    </p>
                    {effectiveCommand && (
                      <>
                        <div className="p-2.5 bg-bg rounded-lg border border-border font-mono text-[12px] text-text break-all"
                          data-testid="manual-update-command">
                          {effectiveCommand}
                        </div>
                        <div className="flex items-center gap-2 flex-wrap">
                          {/* copyToClipboard, not navigator.clipboard directly: the
                              Clipboard API is unavailable on a plain-HTTP remote
                              gateway — exactly the deployment this command targets —
                              and flipping the label regardless would tell the user
                              their shell paste is ready when the clipboard still
                              holds something else. Await it, then confirm. */}
                          <Btn onClick={async () => { await copyToClipboard(effectiveCommand); setGwCommandCopied(true) }}>
                            <Copy size={13} className="lucide-inline" /> {gwCommandCopied
                              ? i18nT('pages.settings.aboutPanel.copied')
                              : i18nT('pages.settings.aboutPanel.copy_command')}
                          </Btn>
                          {/* The step the flow used to end without. The installer
                              replaced the code on disk; this process is still
                              running the old version until it re-execs. Primary
                              once the command has been copied, because that is
                              when restarting is the actual next action. */}
                          <RestartGatewayButton
                            primary={gwCommandCopied}
                            pending={gwRestart.isPending}
                            restarting={restarting}
                            onConfirm={() => { setApplyError(''); gwRestart.mutate() }}
                            testId="gateway-restart"
                          />
                        </div>
                        <p className="text-[12px] text-muted">
                          {i18nT('pages.settings.aboutPanel.restart_after_installer_note')}
                        </p>
                        {applyError && (
                          <span className="text-[12px] text-danger flex items-start gap-1.5">
                            <AlertCircle size={13} className="lucide-inline shrink-0" /> {applyError}
                          </span>
                        )}
                      </>
                    )}
                  </div>
                ) : (
                  <div>
                    <Btn primary onClick={() => { if (!gwChanges) gwCheck.mutate(); setApplyError(''); setRestarting(false); setShowConfirm(true) }}>
                      {/* Whole-sentence keys, not "Update" + " to vX": the version
                          does not follow the verb in every language. */}
                      <ArrowUp size={13} className="lucide-inline" /> {gwTarget
                        ? i18nT('pages.settings.aboutPanel.update_to_version', { version: gwTarget })
                        : i18nT('pages.settings.aboutPanel.update_now')}
                    </Btn>
                  </div>
                )}
              </>
            ) : (
              <>
                <p className="text-sm text-muted">
                  {lastCheckedAt
                    ? i18nT('pages.settings.aboutPanel.checks_for_updates_with_timing', {
                        name: botName || 'Kiro Crew',
                        timing: i18nT('pages.settings.aboutPanel.last_checked_ago_next_check_in', {
                          ago: fmtRelative(lastCheckedAt * 1000),
                          next: fmtRelative((lastCheckedAt + checkIntervalSecs) * 1000),
                        }),
                      })
                    : <>{botName || 'Kiro Crew'} {i18nT('pages.settings.aboutPanel.checks_for_updates_automatically_you_can_also_ch')}</>}
                </p>
                <div>
                  <Btn onClick={() => gwCheck.mutate()} disabled={gwCheck.isPending}>
                    <RefreshCw size={13} className={`lucide-inline ${gwCheck.isPending ? 'animate-spin' : ''}`} /> {i18nT('pages.settings.aboutPanel.check_for_updates')}
                  </Btn>
                </div>
                {/* A 200 is transport success, NOT a verdict: `checked` is the
                    verdict. Gating the success line on it is the fix for the
                    original bug, where a wheel install's no-op check rendered
                    "you're on the latest version" while being two releases
                    behind. An unrecognised error code still lands here (in the
                    error branch), never in the success branch. */}
                {gwCheck.isSuccess && gwChecked && !gwError && !gwUnavailableReason && !showUpdate && (
                  <span className="text-ok text-[13px] flex items-center gap-1.5" data-testid="up-to-date"><CheckCircle2 size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.you_re_on_the_latest_version')}</span>
                )}
                {gwCheck.isSuccess && !!gwUnavailableReason && (
                  <span className="text-muted text-[13px] flex items-center gap-1.5" data-testid="check-not-applicable"><Package size={13} className="lucide-inline" /> {gwCheckErrorText(gwUnavailableReason)}</span>
                )}
                {(gwCheck.isError || (gwCheck.isSuccess && !!gwError)) && (
                  <span className="text-danger text-[13px] flex items-center gap-1.5" data-testid="check-failed"><AlertCircle size={13} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.couldn_t_check_for_updates_2')}{gwError ? `: ${gwCheckErrorText(gwError)}` : ''}</span>
                )}
              </>
            )}
            {/* The auto-apply promise only holds where the gateway can replace its
                own code. On any other layout the backend deliberately downgrades
                auto-update to a notification (the `self_updatable` guard in
                `gateway.py`), so leaving an enabled toggle and an "automatically
                pull and apply" tooltip here would accept input for something that
                cannot happen. Say what it will actually do instead. */}
            <div className="flex items-center justify-between pt-2.5 border-t border-border"
              title={gwSelfUpdate
                ? i18nT('pages.settings.aboutPanel.automatically_pull_and_apply_updates_when_the_ga')
                : i18nT('pages.settings.aboutPanel.auto_update_notify_only_on_this_install')}>
              <span className={`text-sm ${gwSelfUpdate ? 'text-text' : 'text-muted'}`}>{gwSelfUpdate
                ? i18nT('pages.settings.aboutPanel.auto_update_on_restart')
                : i18nT('pages.settings.aboutPanel.notify_when_an_update_is_available')}</span>
              <Toggle checked={autoUpdate} label={gwSelfUpdate
                ? i18nT('pages.settings.aboutPanel.auto_update_on_restart')
                : i18nT('pages.settings.aboutPanel.notify_when_an_update_is_available')}
                onChange={async next => { setAutoUpdate(next); try { await api.setAutoUpdate(next) } catch { setAutoUpdate(!next) } }} />
            </div>

            {/* Standing maintenance action, NOT gated on an update being
                available. Restarting is how this install picks up code that
                already changed on disk — a re-run installer, a `pip install -e .`
                during development, a config change that needs a fresh process —
                and none of those imply a pending release. Keeping it out of the
                update branch is the difference between a control that is there
                when needed and one that only appears when an update happens to
                be offered.

                Hidden while the manual-update card is on screen: that card
                already renders a Restart button wired to the same mutation, and
                two identical buttons a few rows apart read as two different
                actions. */}
            {!showManualUpdate && (
              <div className="flex items-center justify-between gap-3 pt-2.5 border-t border-border"
                data-testid="gateway-restart-row">
                {/* One label, not a title + note pair: the button already names the
                    action, so a "Restart gateway" heading beside a "Restart gateway"
                    button says the same thing twice. This mirrors the auto-update
                    row above — a single explanatory label plus its control — and
                    spends the space on the consequence instead. */}
                <span className="text-sm text-muted">{i18nT('pages.settings.aboutPanel.restart_row_note')}</span>
                <div className="shrink-0">
                  <RestartGatewayButton
                    pending={gwRestart.isPending}
                    restarting={restarting}
                    onConfirm={() => { setApplyError(''); gwRestart.mutate() }}
                    testId="gateway-restart-standing"
                  />
                </div>
              </div>
            )}
            {/* The in-flow button renders its own error inside the update card;
                this one has no card, so the shared error surfaces here. Gated on
                the SAME condition as the row itself -- keying it on `showUpdate`
                instead left a self-updatable install with a pending update showing
                the row while swallowing its error, so a rejected restart gave the
                user no feedback whatsoever. */}
            {applyError && !showManualUpdate && (
              <span className="text-[12px] text-danger flex items-start gap-1.5" data-testid="gateway-restart-error">
                <AlertCircle size={13} className="lucide-inline shrink-0" /> {applyError}
              </span>
            )}
          </div>
        )}

        {/* The full changelog used to be inlined here, open by default. It grows
            without bound while this card's job -- stating the identity of this
            install -- is bounded to one screen forever, so the archive moved to
            its own Releases panel and this is the link to it. See
            pages/settings/ReleasesPanel.tsx. */}
        <div className="mt-3 pt-3 border-t border-border">
          <Link
            to="?tab=releases"
            className="text-[13px] text-accent hover:underline inline-flex items-center gap-1.5"
          >
            <History size={13} className="lucide-inline" aria-hidden="true" />
            {i18nT('pages.settings.aboutPanel.view_all_releases')}
          </Link>
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
                {gwApply.isPending ? <><RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('pages.settings.aboutPanel.updating')}</> : i18nT('pages.settings.aboutPanel.update_now')}
              </Btn>
            )}
          </div>
        </div>
      )}

      <ReportProblemCard />
    </>
  )
}
