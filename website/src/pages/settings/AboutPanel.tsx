import { useEffect, useId, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Trans } from 'react-i18next'
import { RefreshCw, Scale, CheckCircle2, AlertCircle, Bug, GitBranch, GitCommitHorizontal, ExternalLink, ArrowUp, History, Download, Copy } from 'lucide-react'
import { SettingsLink } from '../../components/SettingsLink'
import { Progress } from '@/components/ui/progress'
import { Card, CardTitle, Btn } from '../../components/ui'
import { SettingsToggle } from '../../components/settings'
import { useBranding } from '../../hooks/useBranding'
import { useAppSelector } from '../../store'
import { codeBrowserBranchUrl, codeBrowserCommitUrl } from '../../lib/codeBrowser'
import SegmentedControl from '../../components/SegmentedControl'
import ReportProblemCard from './ReportProblemCard'
import { ARM_POLL_FAILURE_THRESHOLD, AGENT_REQUEST_POLL_MS, AgentUpdateRequestCard, copyCommand } from './AboutPanelUpdateFlow'
import { AboutPanelGatewayUpdates, AboutPanelUpdateConfirm, useAboutPanelGatewayUpdate } from './AboutPanelGatewayUpdate'
import { api } from '../../api/client'
import { AppUpdatesSection } from './AppUpdatesSection'
import { gatewayAutoUpdateCopy, useGatewayAutoUpdateEffect, useIsPolicyGoverned, useUpdateSwitchesShown } from '../../utils/updateSwitches'
import { useGatewayAutoUpdate, type SwitchState } from '../../hooks/useGatewayAutoUpdate'
import { useAppAutoDownload } from '../../hooks/useAppAutoDownload'
import { storeAnsweredUpdateInfo, updateInfoQuery } from '../../api/updateInfoQuery'
import { failedWithNoData } from '../../api/queryState'
import ErrorNotice from '../../components/ErrorNotice'
import { LockedByPolicyBadge } from '../../components/settingRef/LockedByPolicyBadge'
import { CopyCommand } from '../../components/agentHarness/CopyCommand'

import { i18nT } from '../../i18n/t'
import { fmtDateTimeNumeric, fmtList } from '../../i18n/format'
import { feedbackTracker } from '../../feedbackTracker'
import type { UpdateState } from '../../hooks/useUpdateSubscription'
import { gatewayStopCommand } from '../../hooks/useUpdateSubscription'
import { useInstallDispatch } from '../../hooks/useInstallDispatch'
import { foldStableStamp, sameBuildVersion } from '../../utils/displayVersion'
import { bytesAreTheStableRelease as followedLanePublishesRunningBytes } from '../../utils/laneMembership'

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
  stageInvalidated: 'pages.settings.aboutPanel.update_error_stage_invalidated',
  integrity: 'pages.settings.aboutPanel.update_error_integrity',
  misconfigured: 'pages.settings.aboutPanel.update_error_misconfigured',
  unknown: 'pages.settings.aboutPanel.update_error_unknown',
  installUnknown: 'pages.settings.aboutPanel.update_error_install_unknown',
  gatewayStillRunning: 'pages.settings.aboutPanel.update_error_gateway_still_running',
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
    case 'stage-invalidated': return i18nT(UPDATE_ERROR_KEYS.stageInvalidated)
    case 'integrity': return i18nT(UPDATE_ERROR_KEYS.integrity)
    case 'misconfigured': return i18nT(UPDATE_ERROR_KEYS.misconfigured)
    // The command itself renders beside this text (gatewayStopCommand), so the
    // sentence points at it; without a command the install generic applies.
    case 'gateway-still-running':
      return gatewayStopCommand(st)
        ? i18nT(UPDATE_ERROR_KEYS.gatewayStillRunning)
        : i18nT(UPDATE_ERROR_KEYS.installUnknown)
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

function getUpdateApi(): UpdateAPI | undefined {
  return window.updateAPI
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
 * Where the prerelease note sends a bug report: the feedback tracker's blank
 * new-issue page (`feedbackTracker().issueUrl`). Read through the seam, not
 * from the guided flow's own constants: an edition retargets every feedback
 * surface in one call, and the guided flow's prompt can change shape without
 * touching this link.
 */
const reportIssueUrl = (): string => feedbackTracker().issueUrl

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

export {
  AGENT_REQUEST_POLL_MS,
  AgentUpdateRequestCard,
  InAppUpdateFlow,
  RestartGatewayButton,
  resolveUnarmedPhase,
} from './AboutPanelUpdateFlow'

/**
 * A switch row's notices: the saved value could not be read (the switch then
 * shows its default), a read failed while the switch shows a value known
 * otherwise (the last value read, or a save's answer), or the write failed or
 * was refused. askAgent on: the switch shows the value still
 * in effect, so nothing is lost by leaving.
 */
function SwitchNotices({ state, unreadable, testIdPrefix, onHandoff }: {
  state: SwitchState
  /** Omitted where another notice on the page already reports the read. */
  unreadable?: string
  testIdPrefix: string
  onHandoff?: () => void
}) {
  return (
    <>
      {unreadable && state.readFailed && (
        <ErrorNotice variant="inline" message={unreadable} askAgent onHandoff={onHandoff}
          testId={`${testIdPrefix}-read-error`} />
      )}
      {state.refreshFailed && (
        <ErrorNotice variant="inline" message={state.refreshFailed}
          askAgent onHandoff={onHandoff} testId={`${testIdPrefix}-refresh-error`} />
      )}
      {state.saveError && (
        <ErrorNotice variant="inline" message={state.saveError} askAgent onHandoff={onHandoff}
          testId={`${testIdPrefix}-save-error`} />
      )}
    </>
  )
}

/**
 * The desktop app updater's auto-download opt-out, which is ON by default.
 * Shared with the What's-new modal; where each surface draws it is
 * `utils/updateSwitches`. Held at the shell's default, on, until the
 * updater's getInfo() answers.
 */
export function AppAutoDownloadSwitch({ unreadable, anchor = true, onHandoff }: {
  /** The read-failure copy; omitted where the page already reports it. */
  unreadable?: string
  /** `false` for a copy outside Settings, so deep links land on About's row. */
  anchor?: boolean
  onHandoff?: () => void
}) {
  const app = useAppAutoDownload()
  return (
    <div className="pt-1 border-t border-border">
      <SettingsToggle
        label={i18nT('pages.settings.aboutPanel.install_app_updates_automatically')}
        description={i18nT('pages.settings.aboutPanel.install_app_updates_automatically_hint')}
        checked={app.checked}
        disabled={app.held}
        onChange={app.set}
        anchor={anchor}
      />
      <SwitchNotices state={app} testIdPrefix="auto-download" onHandoff={onHandoff} unreadable={unreadable} />
    </div>
  )
}

/**
 * The gateway's `auto_update` switch, shared with the What's-new modal. One
 * row in every state: what changes with the gateway's `update_auto_effect` is
 * whether the switch can be flipped and the note under it
 * (`gatewayAutoUpdateCopy`), so a verdict that arrives while the row is on
 * screen never swaps it for another element. Held at the schema default, on,
 * until the saved value has been read. Where config.local.json sets the key,
 * a note says so (the Privacy tab's pin note); the switch stays usable, since
 * a click is how a removed override is noticed.
 */
export function GatewayAutoUpdateSwitch({ anchor = true, readOnMount = false, onHandoff, contextNote, sectionHeading }: {
  /** `false` for a copy outside Settings, so deep links land on About's row. */
  anchor?: boolean
  /** Re-read the saved value on every mount, not only once it is 30 s old. */
  readOnMount?: boolean
  onHandoff?: () => void
  /** Optional sub-heading rendered inside the gateway section (below the divider),
   *  before the context note and toggle. Makes the gateway controls visually
   *  separate from the app's own update card above. */
  sectionHeading?: string
  /** Optional context note rendered below the section heading, before the toggle. */
  contextNote?: string

}) {
  const canApply = useAppSelector(s => s.dashboard.status?.update_can_apply) === true
  const copy = gatewayAutoUpdateCopy(useGatewayAutoUpdateEffect(), { canApply })
  const gw = useGatewayAutoUpdate({ readOnMount })
  const reduceMotion = useReducedMotion()
  const noteId = useId()
  const pinNoteId = useId()
  // A refusal already says the same thing, through its notice.
  const showPinNote = gw.overlayPinned && !gw.saveError
  const describedBy = [copy.hint ? noteId : '', showPinNote ? pinNoteId : ''].filter(Boolean).join(' ') || undefined
  return (
    <div className="pt-2.5 border-t border-border">
      {sectionHeading && (
        <p className="text-[13px] font-semibold text-text mb-0.5">{sectionHeading}</p>
      )}
      {contextNote && (
        <p className="text-[13px] text-muted mb-2">{contextNote}</p>
      )}
      <SettingsToggle
        label={i18nT('pages.settings.aboutPanel.automatic_updates')}
        description={i18nT('pages.settings.aboutPanel.gateway_auto_update_description')}
        checked={gw.checked}
        disabled={copy.disabled || gw.held}
        onChange={gw.set}
        configKey="auto_update"
        describedBy={describedBy}
        anchor={anchor}
      />
      <AnimatePresence initial={false}>
        {copy.hint && (
          <motion.p
            key="note"
            id={noteId}
            className="text-[12px] text-muted overflow-hidden"
            initial={reduceMotion ? false : { opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={reduceMotion ? undefined : { opacity: 0, height: 0 }}
            data-testid="auto-update-note"
          >
            {copy.hint}
          </motion.p>
        )}
      </AnimatePresence>
      {showPinNote && (
        <p id={pinNoteId} className="text-[12px] text-muted mt-1" data-testid="auto-update-pin-note">
          {i18nT('pages.settings.privacyPanel.recordMetricsOverlayPinned')}
        </p>
      )}
      <SwitchNotices state={gw} testIdPrefix="auto-update" onHandoff={onHandoff}
        unreadable={i18nT('pages.settings.aboutPanel.auto_update_setting_unavailable')} />
    </div>
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
  // The background check's commit distance, from the status push. What lets
  // the hero badge tell a diverged checkout from a current one on first
  // visit, before any manual check has populated the local counts.
  const statusAhead = useAppSelector(s => s.dashboard.status?.update_commits_ahead) || 0
  const statusBehind = useAppSelector(s => s.dashboard.status?.update_commits_behind) || 0
  const lastCheckedAt = useAppSelector(s => s.dashboard.status?.update_last_checked_at) ?? null
  const checkIntervalSecs = useAppSelector(s => s.dashboard.status?.update_check_interval_secs) ?? 43200
  const queryClient = useQueryClient()
  const desktopApi = getUpdateApi()
  const isDesktop = !!desktopApi

  // Desktop (Electron) app info (version, channel, platform)
  const infoQuery = useQuery({ ...updateInfoQuery, enabled: isDesktop })
  const info = infoQuery.data
  // A shell with no getInfo() answers `null`: as much a failed read here as a
  // rejection, since the version, channel and auto-download rows all need it.
  // The last settled read, so the notice holds through a retry.
  const infoError = failedWithNoData(infoQuery) || (isDesktop && info === null)
  // A re-read rejected over cached info: the rows shown are the last ones read.
  const infoRefreshFailed = info !== undefined && infoQuery.isError

  // Desktop update lifecycle state, read from the shared cache that
  // useUpdateSubscription (mounted in shell/appShell.tsx) populates.
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
  const downloadMutation = useMutation({ mutationFn: () => desktopApi!.download!() })
  // Install-and-restart, with its one-way latch and the reset for a refused or
  // aborted handoff (see useInstallDispatch).
  const { installMutation, dispatched: installDispatched, dispatchInstall } = useInstallDispatch(updateState)
  // An agent's pending request for THIS packaged install. Polled while on
  // desktop; the poll is also how the card leaves — a decline, an expiry, or
  // the install itself (which quits the app under this tab) all end it.
  const agentRequest = useQuery({
    queryKey: ['update-arm-status'],
    queryFn: () => api.armStatus(),
    enabled: isDesktop,
    refetchInterval: AGENT_REQUEST_POLL_MS.value,
  })
  const pendingAgentRequest = agentRequest.data?.armed === true && agentRequest.data.managed_by === 'electron'
    ? agentRequest.data
    : null
  // React Query keeps the last successful data through errors, so a failing
  // poll leaves the card mounted with its error line rather than blanking it.
  // The human's click on the request card: the SAME download→install path the
  // ordinary card uses. With a stage already present, install; otherwise
  // download and let the `downloaded` state's Install button (or the
  // deferred-on-quit install) finish it — exactly as a manual click would.
  const [agentInstallArmed, setAgentInstallArmed] = useState(false)
  const agentInstallArmedRef = useRef(false)
  // The armed click has seen its download START (`downloading`, which the main
  // process emits at once for a click it re-checks). What makes a later
  // `checking` or `not-available` mean "that download was dropped" rather than
  // the state the click was made from (see agentDownloadDropped).
  const [agentArmSawDownload, setAgentArmSawDownload] = useState(false)
  const agentArmSawDownloadRef = useRef(false)
  // Before dispatching the install, retire the request by the id the card
  // showed. The install quits the app; on relaunch into the new version, a
  // request left on disk would put the card straight back up asking for a
  // version that is already running. Best-effort and NOT awaited: the human
  // said yes, and a slow gateway must not stand between that and the install.
  const retireAgentRequest = () => {
    const id = pendingAgentRequest?.request_id
    if (id) void api.dismissUpdateArm(id).catch(() => {})
  }
  const installFromAgentRequest = () => {
    if (updateState?.state === 'downloaded') {
      // Installs now, so nothing is armed: an armed click left standing would
      // re-fire on the next `downloaded` once the dispatch latch resets after a
      // refused or aborted handoff, installing with nobody clicking.
      retireAgentRequest()
      dispatchInstall()
    } else {
      agentArmSawDownloadRef.current = false
      agentInstallArmedRef.current = true
      setAgentArmSawDownload(false)
      setAgentInstallArmed(true)
      downloadMutation.mutate()
    }
  }
  // Query-cache listeners run for every updater payload, before React Query
  // batches observer renders. The refs preserve the event order for one armed
  // click even when `downloading` and its cancellation share a render.
  useEffect(() => queryClient.getQueryCache().subscribe(event => {
    if (event.type !== 'updated'
      || event.query.queryKey.length !== 1
      || event.query.queryKey[0] !== 'update-state') return
    const pushed = event.query.state.data as UpdateState | null | undefined
    if (!agentInstallArmedRef.current) return
    if (pushed?.state === 'downloading') {
      agentArmSawDownloadRef.current = true
      setAgentArmSawDownload(true)
    } else if (agentArmSawDownloadRef.current
      && (pushed?.state === 'not-available' || pushed?.state === 'checking')) {
      agentInstallArmedRef.current = false
      agentArmSawDownloadRef.current = false
      setAgentInstallArmed(false)
      setAgentArmSawDownload(false)
    }
  }), [queryClient])
  const agentDownloadLanded = agentInstallArmed && updateState?.state === 'downloaded'
  useEffect(() => {
    // The download the request card started has landed: finish it. One shot —
    // `agentInstallArmed` is cleared so a later, unrelated download does not
    // ride this click.
    if (!agentDownloadLanded || installDispatched) return
    agentInstallArmedRef.current = false
    agentArmSawDownloadRef.current = false
    setAgentInstallArmed(false)
    setAgentArmSawDownload(false)
    retireAgentRequest()
    dispatchInstall()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentDownloadLanded, installDispatched])
  // A download the card started that then FAILED — the updater's own
  // download-phase error, or the IPC rejecting — must release the click, or
  // both request actions stay disabled with no way forward.
  const agentDownloadFailed = agentInstallArmed
    && (downloadMutation.isError || (updateState?.state === 'error' && updateState.phase === 'download'))
  // A download the card asked for that the main process then DROPS must release the
  // click too: it re-checks the feed first, and a retraction (`not-available`) or a
  // channel switch (a fresh `checking`) ends that click without an error. A click
  // left armed keeps both request actions disabled and installs the next build that
  // downloads, hours later, with nobody clicking.
  //
  // Only AFTER the click's `downloading`: both states are also ones a click is made
  // FROM (the card stays enabled through them), and from there the main process
  // discovers first and then downloads. Read off the current state alone, that
  // click would be released on the next render and its download would land with
  // nothing left to install it.
  useEffect(() => {
    if (!agentInstallArmed || updateState?.state !== 'downloading') return
    agentArmSawDownloadRef.current = true
    setAgentArmSawDownload(true)
  }, [agentInstallArmed, updateState?.state])
  const agentDownloadDropped = agentInstallArmed && agentArmSawDownload
    && (updateState?.state === 'not-available' || updateState?.state === 'checking')
  const agentDownloadEnded = agentDownloadFailed || agentDownloadDropped
  useEffect(() => {
    if (!agentDownloadEnded) return
    agentInstallArmedRef.current = false
    agentArmSawDownloadRef.current = false
    setAgentInstallArmed(false)
    setAgentArmSawDownload(false)
  }, [agentDownloadEnded])
  // Failures the request card must SHOW (errors-use-error-notice): a poll that
  // keeps failing means an approval could land unseen; a refused download means
  // the click did nothing. The poll uses the same consecutive-failure threshold
  // InAppUpdateFlow uses for the same reason — one blip is left to the next
  // interval.
  const agentRequestPanelError = agentRequest.failureCount >= ARM_POLL_FAILURE_THRESHOLD
    ? i18nT('pages.settings.aboutPanel.arm_status_poll_failing')
    : agentDownloadFailed
      ? `${i18nT('pages.settings.aboutPanel.download_failed')}: ${updateState?.state === 'error' ? updateErrorText(updateState) : i18nT(UPDATE_ERROR_KEYS.unknown)}`
      : ''
  // While a request is live, its card owns the one Install control. Rendering
  // the ordinary card's "Download & Install" beneath it shows two buttons for
  // one update and the reader cannot tell whether they differ. The ordinary
  // card keeps everything else — progress, notes, the failure row, the manual
  // fallback — because those are what the request card's click drives.
  const ctaOwnedByRequest = !!pendingAgentRequest
  // Channel switcher (stable ⇄ insider opt-in). Switching persists the
  // preference and triggers a check; the other channel's build then arrives
  // as the normal consent card above -- never an automatic install. Nightly
  // builds report channelSwitchable=false (separate pinned install).
  const channelMutation = useMutation({
    mutationFn: (next: string) => desktopApi!.setChannel!(next),
    onSuccess: answer => storeAnsweredUpdateInfo(queryClient, answer),
  })
  // A refused switch arrives either as a rejection or as a RESOLVED
  // `{ ok: false, error }` — the bridge's contract — so both count as failed.
  // The shell's reason, when it gave one, is the only actionable detail.
  const desktopChannelFailed = channelMutation.isError
    || (channelMutation.data !== undefined && !channelMutation.data.ok)
  const desktopChannelReason = channelMutation.data && !channelMutation.data.ok
    ? (channelMutation.data.error || '')
    : ''

  // The version chip's DISPLAY text. For a gateway install the fold is the
  // backend's (`version_display`, raw `version` fallback for a gateway that
  // predates the field); for a desktop build it is computed locally, because
  // the Electron-reported version never crosses the gateway. Every functional
  // reader (`versionLooksPrerelease`, the updater compare gate, the SPA's
  // reload-on-upgrade comparison) keeps its raw source.
  const gatewayVersionDisplay = useAppSelector(s => s.dashboard.status?.version_display) || ''
  // The desktop lane pair, live-first. `info` is a one-shot `getInfo()` read from
  // mount, while every later check pushes its own answer on the lifecycle payload,
  // so preferring the push is what keeps the chip and the prerelease ask correct
  // after a check that ran while this panel was open. A replayed payload is the
  // same `getInfo()` seed, so it is no worse than the fallback. `undefined` on
  // either side means unknown and must never read as "ahead".
  const laneVersion = updateState?.laneVersion || info?.laneVersion || ''
  const runningAheadOfLane = updateState?.laneVersion !== undefined && updateState.laneVersion !== ''
    ? updateState.runningAheadOfLane
    : info?.runningAheadOfLane
  const versionDisplay = info?.version
    ? foldStableStamp(info.version, info.channel, runningAheadOfLane)
    : (gatewayVersionDisplay || gatewayVersion || '—')
  // Two version lanes. `versionDisplay` is the DESKTOP SHELL's stamp whenever
  // `getInfo()` answered and the gateway's only as a fallback, while the branch
  // and commit chips are always the gateway's (`status.branch` / `status.commit`,
  // stamped from the checkout the backend runs from). A shell that spawned its
  // own gateway keeps both lanes equal, so folding them into one row is
  // harmless there. A shell attached to a gateway it did not spawn (dev-fleet,
  // launchd, an SSH tunnel to a source checkout) is where the fold lies: the row
  // reads `v0.6.0-insider.6 · main · 2ed1f603d`, a build that never existed.
  // When the two disagree, label both and hang the chips off the gateway line,
  // since that is what they stamp. "Agree" is EITHER of two tests, because one
  // build is stamped in two spellings (`release_channel.py`: the shell carries
  // SemVer `0.6.0-insider.4`, the wheel it launched carries PEP 440 `0.6.0rc4`):
  // the raw strings name the same build (`sameBuildVersion`, so a self-spawned
  // insider install is one lane), or the DISPLAY strings match (a promoted
  // stable build folds its prerelease stamp on both sides — foldStableStamp
  // here, `_display_version` on the backend — while its raw stamps differ).
  const gatewayLaneVersion = gatewayVersionDisplay || gatewayVersion
  const versionLanesDiffer = !!info?.version && !!gatewayLaneVersion
    && versionDisplay !== gatewayLaneVersion
    && !sameBuildVersion(info.version, gatewayVersion || gatewayLaneVersion)
  const channel = info?.channel
  const updatesDisabled = info?.disabled
  // An externally-managed install (a distro/enterprise package) has no channel
  // its owner reads and no self-update lane, so both the switcher and the
  // channel row disappear rather than describing a control that changes nothing.
  const isExternallyManaged = updatesDisabled === 'externally-managed'
  // A marker with updateCommand routes to the managed lane: the external
  // package manager handles discovery and apply, but the managed-by notice
  // and the copyable command still apply alongside the regular update controls.
  const isManagedLane = !isExternallyManaged && !updatesDisabled && !!info?.updateCommand
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
  // Who manages updates on this gateway: 'command' = a policy-pinned provider
  // owns them, so self-managed installer copy would instruct the user to run
  // the exact mechanism the policy excluded.
  const gwManagedByCommand = useAppSelector(s => s.dashboard.status?.update_managed_by) === 'command'
  // True when the provider governs this bundled install and can apply on this
  // platform. Shared predicate: see selectPolicyGoverned in dashboardSlice.
  // True when the policy provider governs this bundled install.
  // Read from the main-process-confirmed update-state (useIsPolicyGoverned).
  const isGatewayPolicyManaged = useIsPolicyGoverned(infoQuery.data)
  // In-app arm+approve applies only where the backend probed the managed-venv
  // shape; managed_by alone also covers bare source installs whose arm would 409.
  const gwCanArm = useAppSelector(s => s.dashboard.status?.update_can_arm) === true
  // The background check's candidate version, so the Arm button can name its
  // target before the user ever presses the manual Check button (gwTarget is
  // only populated by an explicit check in this tab).
  const gwStatusLatest = useAppSelector(s => s.dashboard.status?.update_latest_version) || ''
  // DISPLAY-ONLY fold of the candidate above, so the channel-move note can name
  // the release the followed lane actually publishes (`0.4.1`) instead of its
  // promoted candidate's raw stamp (`0.4.1rc1`).
  const gwStatusLatestDisplay = useAppSelector(s => s.dashboard.status?.update_latest_version_display) || ''
  // Is the running build ahead of everything the followed channel publishes?
  // The backend derives this from the FEED (see `_channel_move_pending`), which
  // is the only honest source: the previous SPA-side rule compared
  // `update_channel` against the version-derived `release_channel`, and since a
  // promoted stable release keeps its candidate's `rc` stamp, that comparison
  // reported "mid-switch" permanently for every promoted-stable install.
  const gwChannelMovePending = useAppSelector(s => s.dashboard.status?.update_channel_move_pending) === true
  // Running prerelease bytes? See `isPrerelease` below — computed after the
  // gateway check state it consults, and rendered from JSX, so the ordering is
  // free.

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
    // askAgent on: a status line; the Check button above is the retry.
    status = (
      <ErrorNotice
        variant="inline"
        message={`${i18nT('pages.settings.aboutPanel.couldn_t_check_for_updates')}: ${updateErrorText(updateState)}`}
        askAgent
      />
    )
  } else if (checkMutation.isError) {
    // The IPC call itself rejected, so no updater push will ever describe this
    // failure — without this branch the click simply does nothing visible.
    status = (
      <ErrorNotice
        variant="inline"
        message={`${i18nT('pages.settings.aboutPanel.couldn_t_check_for_updates')}: ${i18nT(UPDATE_ERROR_KEYS.unknown)}`}
        askAgent
        testId="check-request-failed"
      />
    )
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
  // An install refused because the gateway kept running: the error text ends
  // by pointing at the command that stops it, rendered right under it.
  const refusedStopCommand = cardInstallFailed ? gatewayStopCommand(updateState) : null
  const showUpdateCard = !checking && (cardState === 'found' || cardState === 'available' || cardState === 'downloading' || cardState === 'downloaded' || cardFailed)
  const cardBusy = cardState === 'available' || cardState === 'downloading'
  const cardReady = cardState === 'downloaded'
  const showsWindowsInstaller = updateState?.installHandoff === 'windows-installer'
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
          {ctaOwnedByRequest ? null : cardReady ? (
            <Btn primary onClick={dispatchInstall} disabled={installDispatched}>
              <RefreshCw size={13} className={`lucide-inline ${installDispatched ? 'animate-spin' : ''}`} /> {installMutation.isSuccess
                ? i18nT('pages.settings.aboutPanel.restarting')
                : i18nT('pages.settings.aboutPanel.install_update_restart_app')}
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
      {/* askAgent on: the card is the retry surface (Retry / manual link stay
          siblings), and a download holds no user input to lose. */}
      {cardFailed && (
        <ErrorNotice
          variant="inline"
          message={`${i18nT(cardInstallFailed ? 'pages.settings.aboutPanel.install_failed' : 'pages.settings.aboutPanel.download_failed')}: ${updateErrorText(updateState)}`}
          askAgent
          testId="update-download-error"
        />
      )}
      {refusedStopCommand && (
        <CopyCommand><code data-testid="update-gateway-stop-command">{refusedStopCommand}</code></CopyCommand>
      )}
      {/* The IPC call rejected before the updater ever emitted a failure state,
          so no `cardFailed` row describes it: the button would just re-arm with
          no message. Gated on !cardFailed so a real updater failure is never
          reported twice. */}
      {!cardFailed && downloadMutation.isError && (
        <ErrorNotice
          variant="inline"
          message={`${i18nT('pages.settings.aboutPanel.download_failed')}: ${i18nT(UPDATE_ERROR_KEYS.unknown)}`}
          askAgent
          testId="update-download-request-error"
        />
      )}
      {!cardFailed && installMutation.isError && (
        <ErrorNotice
          variant="inline"
          message={`${i18nT('pages.settings.aboutPanel.install_failed')}: ${i18nT(UPDATE_ERROR_KEYS.installUnknown)}`}
          askAgent
          testId="update-install-request-error"
        />
      )}
      {cardReady && (
        <span className="text-[12px] text-muted">
          {/* Once dispatched, the gateway goes down ON PURPOSE and the dashboard
              disconnects during the platform installer handoff. This line is the
              last thing the card says, so it must explain what happens next. */}
          {installDispatched
            ? i18nT('pages.settings.aboutPanel.installing_quiet_note')
            : i18nT('pages.settings.aboutPanel.downloaded_and_verified_the_app_restarts_to_fini')}
          {showsWindowsInstaller && ` ${i18nT('components.updateModal.windows_installer_handoff')}`}
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

  // Copy feedback for the desktop managed lane's update command.
  const [managedCmdCopied, setManagedCmdCopied] = useState(false)
  const [managedCmdCopyFailed, setManagedCmdCopyFailed] = useState(false)

  // --- Gateway (web dashboard) update flow ---
  // The gateway exposes /api/update/check + /api/update; used when not running
  // inside the Electron shell. "Check for updates" flips to "Update to vX" when
  // status.update_available is set; the update itself is gated behind a
  // changelog confirm because applying restarts the gateway.
  const gwUpdate = useAboutPanelGatewayUpdate(queryClient)
  const {
    gwTargetDisplay, gwFound, gwAhead, gwBehind, gwChecked, gwSelfUpdatable, gwChannel,
    gwChannelError, gwCommand, showConfirm, setShowConfirm, restarting, gwCheck, gwApply,
    gwChannelMutation,
  } = gwUpdate
  // Diverged: local commits on top of a moved upstream. `update_available` is
  // false there BY DESIGN (the apply path is a destructive reset), so this is
  // derived from the counts rather than from any availability flag — it is the
  // third verdict between "update available" and "up to date". Both counts come
  // from the SAME check response, so this can never mix two checks' answers.
  const gwDiverged = gwAhead > 0 && gwBehind > 0
  // The badge's diverged verdict: a manual check's counts win once one has
  // run (they are the newer read of the same backend cache); before that, the
  // background check's counts from the status push carry the same fact, so a
  // fresh visit to a diverged install is told the truth without clicking
  // anything.
  const heroDiverged = gwChecked ? gwDiverged : statusAhead > 0 && statusBehind > 0
  // Running prerelease bytes? The question is about the BYTES, so it keys on the
  // build's own stamp (`stampedChannel` on desktop, the gateway's
  // version-derived `release_channel`) — a user who just opted INTO insider is
  // still running the stable build they have, and one who just opted back to
  // stable is still running insider bytes. Both directions are pinned by
  // AboutPanel.channelExplainer tests.
  //
  // The one case a stamp cannot answer is a PROMOTED stable release: promotion
  // re-points the soaked candidate's bytes without re-stamping them, so its
  // version reads `insider` while it IS the stable release, and the entire
  // stable population was shown "thanks for testing an early build". That case
  // is exempted by the feed's own answer — the followed lane is stable AND it
  // publishes exactly these bytes (not-ahead, from a comparison that actually
  // COMPLETED). UNKNOWN (no check yet) deliberately keeps the stamp's verdict:
  // an unproven exemption would hide the ask from a genuine prerelease user,
  // while showing a bug-report invitation one check early costs nothing.
  const stampedLane = isDesktop ? info?.stampedChannel : gatewayChannel
  // One rule, shared with the header chip (utils/laneMembership) so the two
  // cannot drift on what licenses the exemption. `laneAnswered` comes from the
  // SAME source as the verdict in both branches: the desktop's tri-state pair,
  // and — on the gateway — `statusChecked` alone. NOT `gwChecked ||`:
  // `gwChecked` is a local useState set by a manual check in this tab, while
  // `update_channel_move_pending` only ever arrives on the status frame, so
  // pairing them exempted a genuine prerelease install here while the header
  // still flagged it.
  const bytesAreTheStableRelease = followedLanePublishesRunningBytes(isDesktop
    ? {
      followedChannel: info?.channel,
      laneAnswered: runningAheadOfLane === true || runningAheadOfLane === false,
      runningAheadOfLane: runningAheadOfLane === true,
    }
    : {
      followedChannel: statusUpdateChannel,
      laneAnswered: statusChecked,
      runningAheadOfLane: gwChannelMovePending,
    })
  const isPrerelease = stampedLane === undefined
    ? !!info?.packaged && versionLooksPrerelease(info?.version)
    : !!stampedLane && stampedLane !== 'stable' && !bytesAreTheStableRelease
  // Update is available if either the redux status flag or the latest check
  // response says so — EXCEPT when the latest check said diverged. The redux
  // flag refreshes on the slower WS status push, so for up to one push interval
  // it can still carry `true` from a check that ran before the checkout gained
  // local commits; letting it win would offer an Update button whose backend
  // path is a bare `git pull` — a silent merge into the user's branch. A fresh
  // diverged verdict therefore outranks the stale flag (fail-safe: withholding
  // the button is recoverable, a surprise merge is not). `gwFound` needs no
  // such guard: it is set from the same response as the counts, and a diverged
  // check reports `update_available: false`.
  const showUpdate = (updateAvailable && !gwDiverged) || gwFound
  // Shared by the check-result line and the confirm modal, so the two surfaces
  // cannot drift while describing the same verdict.
  const gwDivergedText = gwDiverged
    ? i18nT('pages.settings.aboutPanel.checkout_diverged_from_upstream', {
        distance: fmtList([
          i18nT('pages.settings.aboutPanel.commits_ahead', { count: gwAhead }),
          i18nT('pages.settings.aboutPanel.commits_behind', { count: gwBehind }),
        ]),
      })
    : ''
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
  //
  // The predicate is the BACKEND's, computed from the followed lane's feed (see
  // `_channel_move_pending`). It replaces a local comparison of the followed
  // channel against `release_channel`, which is derived from the version string:
  // because promotion re-points the soaked candidate's bytes without re-stamping
  // them, a promoted stable release reports `release_channel: insider`, so that
  // comparison was permanently true for every promoted-stable install and this
  // whole branch — installer command included — rendered forever.
  const channelMovePending = !isDesktop && gwChannelMovePending
  const showManualUpdate = (showUpdate || channelMovePending) && !gwSelfUpdate
  const effectiveGwCanArm = typeof gwCheck.data?.can_arm === 'boolean'
    ? gwCheck.data.can_arm
    : gwCanArm
  const updateSwitches = useUpdateSwitchesShown()
  // The app switch says itself when a re-read fails; with no switch drawn,
  // the card's notice must say it instead.
  const showInfoError = infoError || (infoRefreshFailed && !updateSwitches.app)

  // Escape closes the confirm dialog (unless an apply/restart is in flight).
  useEffect(() => {
    if (!showConfirm) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !gwApply.isPending && !restarting) setShowConfirm(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showConfirm, gwApply.isPending, restarting, setShowConfirm])

  // Gateway-stamped build chips. Hoisted because they render in one of two
  // places: beside the license chip when the lanes agree, or on the gateway's
  // own version line when they do not (see `versionLanesDiffer`).
  const branchChip = buildBranch && (
    <a href={codeBrowserBranchUrl(buildBranch)} target="_blank" rel="noopener noreferrer"
       title={i18nT('pages.settings.aboutPanel.browse_this_branch_on_github')}
       className="inline-flex items-center gap-1.5 text-[12px] font-mono text-accent border rounded-lg px-2.5 py-1 no-underline hover:underline" style={ACCENT_TINT}>
      <GitBranch size={12} className="shrink-0" /> <span className="truncate max-w-[220px]">{buildBranch}</span> <ExternalLink size={10} className="opacity-60 shrink-0" />
    </a>
  )
  const commitChip = buildCommit && (
    <a href={codeBrowserCommitUrl(buildCommit)} target="_blank" rel="noopener noreferrer"
       title={i18nT('pages.settings.aboutPanel.view_this_commit_on_github')}
       className="inline-flex items-center gap-1.5 text-[12px] font-mono text-accent border rounded-lg px-2.5 py-1 no-underline hover:underline" style={ACCENT_TINT}>
      <GitCommitHorizontal size={12} className="shrink-0" /> {buildCommit} <ExternalLink size={10} className="opacity-60 shrink-0" />
    </a>
  )

  return (
    <>
      <Card style={HERO_BG}>
        {/* Identity hero */}
        <div className="flex items-center gap-4">
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- `onError` is the image's own load-failure hook (hide a broken avatar so the row keeps its layout), not a user interaction; the img is decorative (`alt=""`) and there is nothing here for a keyboard to reach */}
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
              <span className="text-[12px] font-mono font-semibold text-accent rounded-full px-2.5 py-0.5 border" style={ACCENT_TINT}
                    title={versionLanesDiffer ? i18nT('pages.settings.aboutPanel.app_lane_title') : undefined}
                    data-testid="about-version">
                {versionLanesDiffer && (
                  <span className="font-sans font-medium text-muted mr-1" data-testid="about-version-lane">{i18nT('pages.settings.aboutPanel.app_lane_label')}</span>
                )}
                {i18nT('pages.settings.aboutPanel.v')}{versionDisplay}</span>
              {!isDesktop && (heroDiverged
                // Diverged outranks BOTH other verdicts: `update_available` is
                // false here BY DESIGN (the no-auto-apply property), and a
                // stale `true` from a check that predates the local commits
                // must not paint "Update available" beside the divergence
                // warning below — the exact half-truth the neutral branch's
                // comment forbids. A manual check's counts win once one has
                // run; the status push's counts cover the first visit.
                ? <span className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold rounded-full px-2 py-0.5"
                    style={{ color: 'var(--warn)', background: 'color-mix(in oklab, var(--warn) 14%, transparent)' }}
                    data-testid="hero-diverged">
                    <GitBranch size={11} className="lucide-inline" aria-hidden /> {i18nT('pages.settings.aboutPanel.diverged')}</span>
                : updateAvailable
                ? <span className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold rounded-full px-2 py-0.5"
                    style={{ color: 'var(--warn)', background: 'color-mix(in oklab, var(--warn) 14%, transparent)' }}>
                    <ArrowUp size={11} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.update_available')}</span>
                // The followed lane has never published these bytes, so this
                // install is not on it yet. Outranks "Up to date", which is what
                // the panel used to say here: the feed comparison DOES come back
                // "nothing newer" (the running build is ahead), so a green pill
                // was technically about the version and a lie about the state —
                // it sat directly above a command telling the user to move.
                : gwChannelMovePending
                // Deliberately NOT the `ArrowUp` of "Update available": this state
                // is the one that does NOT progress on its own — only re-running
                // the installer moves the install — and an upward arrow beside
                // "not on stable" reads as an upgrade already under way. Same
                // warn pill (it IS an attention state), non-directional icon.
                ? <span className="inline-flex items-center gap-1.5 text-[11.5px] font-semibold rounded-full px-2 py-0.5"
                    style={{ color: 'var(--warn)', background: 'color-mix(in oklab, var(--warn) 14%, transparent)' }}
                    data-testid="hero-channel-move-pending">
                    <AlertCircle size={11} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.not_on_channel_yet', { channel: effectiveGwChannel })}</span>
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
            {/* The gateway's own line, only when it is not the build the shell
                badge above names. The branch and commit chips live here in that
                case — they are gateway stamps, so beside the shell badge they
                described a build that never existed. */}
            {versionLanesDiffer && (
              <div className="mt-2 flex items-center gap-2 flex-wrap" data-testid="about-gateway-lane">
                <span className="text-[12px] font-mono font-semibold text-accent rounded-full px-2.5 py-0.5 border" style={ACCENT_TINT}
                      title={i18nT('pages.settings.aboutPanel.gateway_lane_title')}
                      data-testid="about-gateway-version">
                  <span className="font-sans font-medium text-muted mr-1">{i18nT('pages.settings.aboutPanel.gateway_lane_label')}</span>
                  {i18nT('pages.settings.aboutPanel.v')}{gatewayLaneVersion}</span>
                {branchChip}
                {commitChip}
              </div>
            )}
            {/* getInfo() rejected: the version chip above fell back to the
                gateway's figure and the channel row is missing, with nothing
                to say why. askAgent on: a read failure on a status card. */}
            {showInfoError && (
              <ErrorNotice
                variant="inline"
                className="mt-1"
                message={i18nT('pages.settings.aboutPanel.update_info_unavailable')}
                askAgent
                testId="update-info-error"
              />
            )}
          </div>
        </div>

        {/* Build + license chips */}
        <div className="mt-4 flex flex-wrap gap-2">
          {!versionLanesDiffer && branchChip}
          {!versionLanesDiffer && commitChip}
          <span className="inline-flex items-center gap-1.5 text-[12px] text-muted border border-border rounded-lg px-2.5 py-1 bg-bg"
                title={i18nT('pages.settings.aboutPanel.open_source_under_the_apache_2_0_license')}>
            <Scale size={12} className="shrink-0" /> {i18nT('pages.settings.aboutPanel.apache_2_0')}
          </span>
        </div>

        {isDesktop && channel && !isExternallyManaged && !isGatewayPolicyManaged && (
          info?.channelSwitchable && desktopApi?.setChannel ? (
            <div className="flex flex-col" data-testid="channel-switcher" data-setting-label={i18nT('pages.settings.aboutPanel.update_channel')}>
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
              {/* Mirrors the gateway switcher's error line: a refused switch used
                  to leave the control silently sitting on the old lane. The
                  bridge reports a refusal either as a rejection or as
                  `{ ok: false, error }`, so both are read. askAgent on: the
                  preference is persisted by the shell, nothing here is a draft. */}
              {desktopChannelFailed && (
                <ErrorNotice
                  title={desktopChannelReason ? i18nT('pages.settings.aboutPanel.channel_switch_failed') : undefined}
                  message={desktopChannelReason || i18nT('pages.settings.aboutPanel.channel_switch_failed')}
                  askAgent
                  testId="channel-error"
                />
              )}
            </div>
          ) : (
            <Row label={i18nT('pages.settings.aboutPanel.update_channel')}>
              {info?.channelLockedByPolicy ? (
                <span className="inline-flex items-center gap-2">
                  {channel}
                  {/* A build that cannot follow the required channel names both:
                      the lane it is on, and the one the policy requires. */}
                  {info.requiredChannel && info.requiredChannel !== channel && (
                    <span className="text-muted" data-testid="channel-requires">
                      {i18nT('pages.settings.aboutPanel.update_channel_requires', { channel: info.requiredChannel })}
                    </span>
                  )}
                  <LockedByPolicyBadge />
                </span>
              ) : channel}
            </Row>
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
          <div className="flex flex-col" data-testid="gateway-channel-switcher" data-setting-label={i18nT('pages.settings.aboutPanel.update_channel')}>
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
            {/* askAgent on: the switch persists server-side or not at all; the
                control still shows the lane actually followed. The 409 reason,
                when given, is the message (and the journal key); the generic
                line becomes a bold lead in front of it rather than a prefix
                concatenated into it. */}
            {gwChannelMutation.isError && (
              <ErrorNotice
                title={gwChannelError ? i18nT('pages.settings.aboutPanel.channel_switch_failed') : undefined}
                message={gwChannelError || i18nT('pages.settings.aboutPanel.channel_switch_failed')}
                askAgent
                testId="gateway-channel-error"
              />
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

                Shown only while the followed lane has never published the RUNNING
                bytes — i.e. exactly the window where a move is outstanding. Once
                that lane's build is installed the feed comparison stops reporting
                it and the line retires itself.

                Names the version the lane publishes when the check knows it: a
                user switching back to Stable from a newer Insider build is
                performing a DOWNGRADE, and "run the command below" without the
                target version left them unable to tell what they were about to
                install. */}
            {!gwChannelMutation.isError
              && !showChannelHelp
              && !!effectiveCommand
              && channelMovePending && (
              <span className="text-[12px] text-muted flex items-start gap-1.5"
                data-testid="gateway-channel-pending-note">
                {/* Not `ArrowUp`: this sentence says the lane's release is OLDER
                    than the running build, and an upward arrow opening it reads
                    as an upgrade in flight — the same mis-cue the hero badge
                    rejected. No versionless fallback: `channel_move_pending` is
                    only ever written in the same `_set_update_info` call that
                    sets `latest_version` (and reset to False by every other),
                    so the display version cannot be empty in this branch. */}
                <AlertCircle size={13} className="lucide-inline shrink-0 text-warn" />
                <span>{i18nT('pages.settings.aboutPanel.channel_explainer_gateway_switch_note_version', {
                  channel: effectiveGwChannel,
                  version: gwTargetDisplay || gwStatusLatestDisplay,
                })}</span>
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
                  report: <a href={reportIssueUrl()} target="_blank" rel="noreferrer" className="text-accent hover:underline" />,
                }}
              />
            </span>
          </div>
        )}
        {isDesktop && info?.platform && <Row label={i18nT('pages.settings.aboutPanel.platform')}>{info.platform}</Row>}
      </Card>

      <Card>
        <CardTitle><RefreshCw size={15} className="lucide-inline" /> {i18nT('pages.settings.aboutPanel.updates')}</CardTitle>
        {isDesktop ? (<>
          {isExternallyManaged ? (
            // The marker's owner (a distro/enterprise package manager) replaces
            // the whole install, so there is no Check button and no channel —
            // just the "updates are managed by …" fact. A marker that ALSO
            // carries an updateCommand routes to the managed lane instead and
            // shows the notice + copyable command above the update controls.
            <div className="flex flex-col gap-2" data-testid="externally-managed-updates">
              <p className="text-sm text-muted">
                {info?.managedBy
                  ? i18nT('pages.settings.aboutPanel.updates_managed_externally_by', { managedBy: info.managedBy })
                  : i18nT('pages.settings.aboutPanel.updates_managed_externally')}
              </p>
            </div>
          ) : updatesDisabled ? (
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
                      : updatesDisabled === 'policy-channel'
                        // The policy names a channel this build cannot follow:
                        // name it, and link its build as the way back.
                        ? (
                          <span data-testid="updates-policy-channel">
                            {i18nT('pages.settings.aboutPanel.automatic_updates_unavailable_policy_channel', { build: channel, channel: info?.requiredChannel })}
                            {manualUrl && (
                              <>
                                {' '}
                                <Trans
                                  i18nKey="pages.settings.aboutPanel.automatic_updates_policy_channel_download"
                                  values={{ channel: info?.requiredChannel }}
                                  components={{
                                    // eslint-disable-next-line jsx-a11y/anchor-has-content, jsx-a11y/control-has-associated-label
                                    download: <a href={manualUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline" />,
                                  }}
                                />
                              </>
                            )}
                          </span>
                        )
                        : i18nT('pages.settings.aboutPanel.automatic_updates_unavailable_platform')}
            </p>
          ) : isGatewayPolicyManaged ? (
            // The gateway's security policy provider owns updates on this
            // bundled install. The intro line and the Check button are hidden:
            // clicking Check would return "not-available" from the feed lane
            // that stands down, which is false when the provider has an update
            // ready.
            <>
              <p className="text-sm text-text" data-testid="gateway-policy-managed-note">
                {i18nT('pages.settings.aboutPanel.updates_managed_by_policy_desktop')}
              </p>
              {/* When the provider has a pending update, render the gateway's
                  check/apply controls so the user can act on it. Shown ONLY
                  when showUpdate is true to avoid displaying a "Check for
                  updates" button that contradicts the policy notice above. */}
              {showUpdate && (
                <AboutPanelGatewayUpdates
                  gwUpdate={gwUpdate}
                  showUpdate={showUpdate}
                  channelMovePending={channelMovePending}
                  showManualUpdate={showManualUpdate}
                  effectiveGwCanArm={effectiveGwCanArm}
                  effectiveCommand={effectiveCommand}
                  effectiveGwChannel={effectiveGwChannel}
                  gwChannelMovePending={gwChannelMovePending}
                  gwManagedByCommand={gwManagedByCommand}
                  gwStatusLatest={gwStatusLatest}
                  gwStatusLatestDisplay={gwStatusLatestDisplay}
                  gwDiverged={gwDiverged}
                  gwDivergedText={gwDivergedText}
                  lastCheckedAt={lastCheckedAt}
                  checkIntervalSecs={checkIntervalSecs}
                  botName={botName}
                  autoUpdateSwitch={null}
                />
              )}
            </>
          ) : (
            <div className="flex flex-col gap-2.5">
              {/* When a gateway updates section is also shown in this window,
                  label the app's section so "Check for updates" is unambiguously
                  about this app, not the gateway. */}
              {updateSwitches.gateway && (
                <h4 className="text-[11px] font-medium uppercase tracking-wide text-muted mt-0">
                  {i18nT('pages.settings.aboutPanel.app_updates_heading')}
                </h4>
              )}
              {/* Managed lane: the marker names an external owner even though
                  the updater is armed; show the managed-by notice and the
                  copyable command above the regular update controls. */}
              {isManagedLane && (
                <div className="flex flex-col gap-2" data-testid="externally-managed-updates">
                  <p className="text-sm text-muted">
                    {info?.managedBy
                      ? i18nT('pages.settings.aboutPanel.updates_managed_lane_by', { managedBy: info.managedBy })
                      : i18nT('pages.settings.aboutPanel.updates_managed_lane')}
                  </p>
                  <div className="p-2.5 bg-bg rounded-lg border border-border font-mono text-[12px] text-text break-all"
                    data-testid="managed-update-command">
                    {info.updateCommand}
                  </div>
                  <div className="flex items-center gap-2 flex-wrap">
                    <Btn onClick={() => copyCommand(info.updateCommand!, setManagedCmdCopied, setManagedCmdCopyFailed)}>
                      <Copy size={13} className="lucide-inline" /> {managedCmdCopied
                        ? i18nT('pages.settings.aboutPanel.copied')
                        : i18nT('pages.settings.aboutPanel.copy_command')}
                    </Btn>
                    {managedCmdCopyFailed && (
                      <ErrorNotice
                        variant="inline"
                        message={i18nT('pages.settings.aboutPanel.copy_failed_select_the_command_and_copy_it_manually')}
                        askAgent
                      />
                    )}
                  </div>
                </div>
              )}
              <p className="text-sm text-muted">
                {botName || 'Kiro Crew'} {i18nT('pages.settings.aboutPanel.checks_for_updates_automatically_you_can_also_ch')}
              </p>
              <div>
                <Btn primary onClick={() => checkMutation.mutate()} disabled={checking}>
                  <RefreshCw size={13} className={`lucide-inline ${checking ? 'animate-spin' : ''}`} /> {i18nT('pages.settings.aboutPanel.check_for_updates')}
                </Btn>
              </div>
              {status && <div className="text-[13px]">{status}</div>}
              {/* The followed lane has never published these bytes (the running
                  build is ahead of its feed), so "up to date" above is about the
                  version and not about the state. This is what a user sees after
                  flipping the switcher back to Stable from a newer Insider build.

                  The unsolicited auto-path stays deliberately untouched: its
                  direction gate exists so a build running ahead of its channel is
                  never nagged (or silently auto-downloaded) into a downgrade, and
                  that protection covers the entire promoted-stable population. So
                  the move is offered here as an EXPLICIT download of the lane's
                  own release, using the same permalink the failed-install escape
                  hatch uses. */}
              {runningAheadOfLane === true && !!laneVersion && !!channel && !!manualUrl && (
                <p className="text-[12px] text-muted flex items-start gap-1.5"
                  data-testid="desktop-channel-move-pending">
                  {/* Same reason as the gateway twin: the sentence says "older". */}
                  <AlertCircle size={13} className="lucide-inline shrink-0 text-warn" />
                  <span>
                    {/* ONE catalog string carrying the anchor, rendered through
                        `Trans` for the same reason the prerelease ask above is:
                        a hand-rolled split on a mustache literal locks every
                        language into English clause order, and the translator
                        must be free to put the link where the grammar needs it. */}
                    <Trans
                      i18nKey="pages.settings.aboutPanel.channel_publishes_older_version"
                      values={{ channel, version: foldStableStamp(laneVersion, channel) }}
                      components={{
                        // eslint-disable-next-line jsx-a11y/anchor-has-content, jsx-a11y/control-has-associated-label
                        link: <a href={manualUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline" />,
                      }}
                    />
                  </span>
                </p>
              )}
              {/* Above the update card on purpose: a pending request is a
                  decision waiting for this person, and the card below is the
                  ordinary flow that decision rides on. */}
              {pendingAgentRequest && (
                <AgentUpdateRequestCard
                  request={pendingAgentRequest}
                  foundVersion={updateState?.version}
                  busy={agentInstallArmed || cardBusy || installDispatched}
                  panelError={agentRequestPanelError}
                  onInstall={installFromAgentRequest}
                  onDeclined={() => agentRequest.refetch()}
                />
              )}
              {updateCard}
              {/* The card's update-info notice already reports a failed read. */}
              {updateSwitches.app && <AppAutoDownloadSwitch />}
            </div>
          )}
          {/* Outside the app updater's branches on purpose: a gateway the app
              does not own (a reused cli.sh service, an SSH-forwarded one)
              installs on its own switch whatever this build's updater says.
              On a policy-governed install the switch still controls whether
              the provider's apply command runs unattended, as the existing
              WhatsNewAutoUpdateToggle comment documents. */}
          {updateSwitches.gateway && (
            <GatewayAutoUpdateSwitch
              sectionHeading={isDesktop && !isGatewayPolicyManaged
                ? i18nT('pages.settings.aboutPanel.gateway_updates_heading')
                : undefined}
              contextNote={isDesktop && !isGatewayPolicyManaged
                ? i18nT('pages.settings.aboutPanel.gateway_auto_update_desktop_context')
                : undefined}
            />
          )}
        </>) : (
          <AboutPanelGatewayUpdates
            gwUpdate={gwUpdate}
            showUpdate={showUpdate}
            channelMovePending={channelMovePending}
            showManualUpdate={showManualUpdate}
            effectiveGwCanArm={effectiveGwCanArm}
            effectiveCommand={effectiveCommand}
            effectiveGwChannel={effectiveGwChannel}
            gwChannelMovePending={gwChannelMovePending}
            gwManagedByCommand={gwManagedByCommand}
            gwStatusLatest={gwStatusLatest}
            gwStatusLatestDisplay={gwStatusLatestDisplay}
            gwDiverged={gwDiverged}
            gwDivergedText={gwDivergedText}
            lastCheckedAt={lastCheckedAt}
            checkIntervalSecs={checkIntervalSecs}
            botName={botName}
            autoUpdateSwitch={<GatewayAutoUpdateSwitch />}
          />
        )}

        {/* The full changelog used to be inlined here, open by default. It grows
            without bound while this card's job -- stating the identity of this
            install -- is bounded to one screen forever, so the archive moved to
            its own Releases panel and this is the link to it. See
            pages/settings/ReleasesPanel.tsx. */}
        <div className="mt-3 pt-3 border-t border-border">
          <SettingsLink
            tab="releases"
            className="text-[13px] text-accent hover:underline inline-flex items-center gap-1.5"
          >
            <History size={13} className="lucide-inline" aria-hidden="true" />
            {i18nT('pages.settings.aboutPanel.view_all_releases')}
          </SettingsLink>
        </div>
      </Card>

      {/* Installed apps' own updates, beside the gateway's: same page a user
          opens to decide how this install stays current. */}
      <AppUpdatesSection />

      {/* Web update confirm — shows the changelog, then applies (which restarts the gateway). */}
      {showConfirm && (
        <AboutPanelUpdateConfirm gwUpdate={gwUpdate} gwDiverged={gwDiverged} gwDivergedText={gwDivergedText} />
      )}

      <ReportProblemCard />
    </>
  )
}
