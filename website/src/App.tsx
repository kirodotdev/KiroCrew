import { useEffect, useState, useCallback, useRef, createContext, lazy, Suspense, type ReactNode } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { useAppSelector, useAppDispatch } from './store'
import { setUpdateProgress } from './store/dashboardSlice'
// Side-effect: registers every built-in surface in the registry, ahead of every
// module below that reads it (`shell/nav/navItems.ts` computes `NAV_ITEMS`).
import './surfaces/builtins'
import { surfaceLabel } from './surfaces/registry'
import AgentSwitchNoticeBanner from './components/AgentSwitchNoticeBanner'
import { GuideProvider } from './guide/GuideContext'
import { metricColor } from './utils/metricColor'
import { railWidthFor } from './hooks/useRailWidth'
import { FOCUS_INSET } from './hooks/useFocusMode'
import { OVERLAY_Z_MAX, THEME_DECOR_BEHIND_SLOT_ID, THEME_DECOR_SLOT_ID, registerThemeDecorBehindSlot, registerThemeDecorSlot } from './lib/themeDecorLayer'
import { ZoomProvider } from './hooks/ZoomProvider'
import { api } from './api/client'
import { MetricsCard } from './shell/topbar/metricsReadout'
import { Rocket, RefreshCw, Package, Download, Hammer, XCircle, Check, AlertTriangle, Coins, Compass, LayoutGrid, SquareTerminal, Search as SearchIcon } from 'lucide-react'
import { FirstRunChapters } from './shell/boot/firstRun'
import ErrorNotice from './components/ErrorNotice'
import { MobileConnectDialog } from './components/mobileConnect'
import { isMacElectron, isWinElectron, isLinuxFramelessElectron } from './lib/electron'
import ChatPage from './pages/ChatPage'
import PopoutFrame from './pages/PopoutFrame'
import ArtifactPopoutFrame from './pages/ArtifactPopoutFrame'
import TerminalPopoutFrame from './pages/TerminalPopoutFrame'
import AppPopoutFrame, { AppPopoutHardExit } from './pages/AppPopoutFrame'

import ErrorBoundary from './components/ErrorBoundary'
import AskAgentButton from './components/AskAgentButton'
import { Lightbox } from './components/MarkdownRenderer'
const SessionsPage = lazy(() => import('./pages/SessionsPage'))
import LogsPage from './pages/LogsPage'
// Lazy: /members is a standalone surface not needed at startup, and the main
// chunk sits at its size budget — the import() boundary keeps the page (and
// its drawer/roster tree) out of the initial bundle.
const MembersPage = lazy(() => import('./pages/members/MembersPage'))
// The guide pill renders only once a guide is offered; keep it off the entry chunk.
const GuideLayer = lazy(() => import('./guide/GuideLayer'))
// App-contributed link previews (`contributes.linkPreviews`): hover detection only;
// an app's module loads on the first hover of one of its links.
const LinkPreviewLayer = lazy(() => import('./linkPreview/LinkPreviewLayer'))
// Lazy for the same reason: the crew work-item board is opened from a conductor
// session or the Crew page, never at startup.
const CrewBoardPage = lazy(() => import('./pages/CrewBoardPage'))
const SessionDashboardsPage = lazy(() => import('./pages/chat/command-center/SessionDashboardsPage'))
import ArtifactDetailPage from './pages/ArtifactDetailPage'
import { ChangelogModal, UpdateErrorDialogFrame } from './shell/updates/updateFlow'
import KiroCrewNavBridge from './components/KiroCrewNavBridge'
import InstancesViewport from './components/InstancesViewport'
import { LocalOverlayRoot } from './components/overlayRoot'
import EmbeddedHostBridge from './components/EmbeddedHostBridge'
import EmbeddedDragRegionReporter from './components/EmbeddedDragRegionReporter'
import EmbedTabStrip from './components/EmbedTabStrip'
// Dev-only layout-editor harness (RFC §7 PR 2). Lazy so it never weighs the main
// bundle — it is a developer route, not a shipped surface.
const LayoutEditorHarnessPage = lazy(() => import('./pages/LayoutEditorHarnessPage'))

import UpdateModal from './components/UpdateModal'

import ComputerUseLiveView from './components/ComputerUseLiveView'
import BottomTerminalPanel, { TerminalDetachedBar } from './components/BottomTerminalPanel'
import { toggleBottomTerminal } from './hooks/useBottomTerminal'
import { RunInTerminalShellNotice, useRunInTerminalShellBridge } from './shell/runInTerminalShellBridge'
import { TerminalHostContext } from './hooks/useTerminalCommand'
// Side effect: closes a terminal tab when its shell exits (main window and popout).
import './utils/terminalExitClose'
import { focusPopout as focusTerminalPopout } from './utils/terminalPopout'
import MigrationCheck from './components/MigrationCheck'
import CrashReportNotice from './components/CrashReportNotice'
import RemoteUpdateOffer from './components/RemoteUpdateOffer'
import KiroSignInNoticeFallback from './components/KiroSignInNoticeFallback'
import { ImportSessionOutcomeNotice } from './components/ImportSessionItem'
import { FileMenuResultToastFrame } from './components/notifications/FileMenuResultToast'
import BuiltinAppRoute from './apps/BuiltinAppRoute'
import { MobileNavRailContext, MobileRosterBarContext, type MobileNavRailOptions } from './components/MobileNavRailContext'
import ShortcutsModal from './components/ShortcutsModal'
import QuickSearchSurface from './components/QuickSearchSurface'
import ReportProblemModal from './components/ReportProblemModal'
import KiroAccountModal from './components/KiroAccountModal'

import { StartupVideo } from './shell/boot/startupVideo'
import { i18nT } from './i18n/t'
import { isAppNavId } from './appNotificationBadges'
import { lazyPage, TasksRedirect, ChatRedirect, OrchestratedRedirect } from './shell/routes'
import { NAV_ITEMS } from './shell/nav/navItems'
import { RailHeaderGlyph } from './shell/nav/railChrome'
import { AdaptiveMobileRail } from './shell/nav/adaptiveMobileRail'
import { guideTarget } from './uiLocations/targetRegistry'
import { useAppShell } from './shell/appShell'
import { ShellHeader } from './shell/topbar/shellHeader'
import { ShellNavRail } from './shell/nav/navRail'
import { NavItem, NavBadge } from './shell/nav/navRow'

// Lazy on purpose: the update-found popup (its policy module, Trans runtime
// wiring, and mutation plumbing) is dead weight for every session without an
// update, and the app-core chunk is at its size budget. The `updateAvailable`
// mount gate at the render site means the chunk is fetched exactly when it
// can render.
const UpdateFoundModal = lazy(() => import('./components/UpdateFoundModal'))
// Lazy for the same budget: it decides its own visibility (KAS backend + a
// lapsed stored sign-in), so it is always mounted and its chunk is fetched once
// at boot; it renders nothing in the common case.
const KiroSignInNotice = lazy(() => import('./components/KiroSignInNotice'))

// Route-level code splitting for the App Store split (PR1): Discover and
// Library are independent surfaces, and neither belongs in the app-core
// chunk -- each rides its own on-demand chunk fetched on first navigation.
const DiscoverPage = lazy(() => import('./pages/apps/DiscoverPage'))
const LibraryPage = lazy(() => import('./pages/apps/LibraryPage'))

// Every page below is reached only through its own route, so each rides an
// on-demand chunk instead of the app-core chunk the chat route has to parse on
// first load. Pages another eager module imports statically stay eager above
// (LogsPage via the chat ActivityViewer, ArtifactDetailPage via the artifact
// popout frame): a lazy boundary there would not move their code.
const NotificationsPage = lazyPage(() => import('./pages/NotificationsPage'))
const WebhooksPage = lazyPage(() => import('./pages/WebhooksPage'))
const CapabilitiesPage = lazyPage(() => import('./pages/CapabilitiesPage'))
const ArtifactsPage = lazyPage(() => import('./pages/ArtifactsPage'))
const RemoteArtifactDetailPage = lazyPage(() => import('./pages/RemoteArtifactDetailPage'))
const ArtifactDeployPage = lazyPage(() => import('./pages/ArtifactDeployPage'))
const SettingsPage = lazyPage(() => import('./pages/SettingsPage'))
const EmbedSettingsPage = lazyPage(() => import('./pages/EmbedSettingsPage'))
const DeveloperPage = lazyPage(() => import('./pages/DeveloperPage'))
const SchedulePage = lazyPage(() => import('./pages/SchedulePage'))
const AppPage = lazyPage(() => import('./pages/AppPage'))
const AppDetailPage = lazyPage(() => import('./pages/AppDetailPage'))
const MigrationPage = lazyPage(() => import('./pages/MigrationPage'))
const HooksPage = lazyPage(() => import('./pages/HooksPage'))

type LogSubscribeFn = (cb: ((data: { level: string; msg: string }) => void) | null) => void

export const WsContext = createContext<{
  subscribeLogs: LogSubscribeFn
  subscribeSubagents: (s: boolean) => void
  forceReconnect: () => void
}>({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} })

/** Re-exported for the topbar readout's existing consumers; defined in
 *  `utils/metricColor` so a pure test need not import the app root.
 *  `memColorClass` is the historical alias for the same function — preserved so
 *  the moved definition does not silently drop a public `App.tsx` export a
 *  downstream edition might still name. */
export { metricColor }
export const memColorClass = metricColor

/** Re-exported where `RailHeaderGlyph.test.tsx` imports it from; owned by
 *  `shell/nav/railChrome.tsx`. */
export { RailHeaderGlyph }

const UPDATE_STEPS: Record<string, { icon: ReactNode }> = {
  pulling:    { icon: <Download className="lucide-inline" /> },
  syncing:    { icon: <RefreshCw className="lucide-inline" /> },
  building:   { icon: <Hammer className="lucide-inline" /> },
  installing: { icon: <Package className="lucide-inline" /> },
  restarting: { icon: <Rocket className="lucide-inline" /> },
  failed:     { icon: <XCircle className="lucide-inline" /> },
  // The per-step handlers report their failure as `error`; without an entry
  // the header fell back to the spinning glyph over a failure card.
  error:      { icon: <XCircle className="lucide-inline" /> },
}

/**
 * Catalog KEY per update step. Separate from UPDATE_STEPS and FLAT on purpose:
 * this table is evaluated at module load, so an `i18nT()` call here would freeze
 * the boot language, and `scripts/check-i18n-keys.mjs` only resolves a key that
 * is indexed in ONE step from a file-scope map — `i18nT(UPDATE_STEPS[s].labelKey)`
 * would be an unresolvable dynamic site.
 */
const UPDATE_STEP_LABEL_KEY: Record<string, string> = {
  pulling: 'app.pulling_latest_changes',
  syncing: 'app.syncing_workspace',
  building: 'app.rebuilding_package',
  installing: 'app.installing_packages',
  restarting: 'app.restarting_server',
  failed: 'app.update_failed_2',
  error: 'app.update_failed_2',
}

const STEP_ORDER = ['pulling', 'syncing', 'building', 'installing', 'restarting']
const STUCK_THRESHOLD_MS = 5 * 60 * 1000 // 5 minutes

// Exported for the isolated capture harness (capture/update-overlay.tsx):
// the overlay only mounts mid-update, a state a full-shell capture cannot
// reach without stubbing the update endpoints end to end.
export function UpdateOverlay({ onCancel }: { onCancel: () => void }) {
  const progress = useAppSelector(s => s.dashboard.updateProgress)
  // The restart step kills this tab's socket BY DESIGN (the gateway execs
  // itself), and progress events stop with it. Without naming that state the
  // overlay freezes on whatever step last arrived — indistinguishable from a
  // stall. `connected` is what tells "working, gateway is down on purpose"
  // from "stuck".
  const connected = useAppSelector(s => s.dashboard.connected)
  const dispatch = useAppDispatch()
  const step = progress?.step || ''
  const detail = progress?.detail || ''
  const info = UPDATE_STEPS[step]
  const currentIdx = STEP_ORDER.indexOf(step)
  // Both spellings are terminal: the apply path pushes `failed` from its
  // outer handler and `error` from its per-step handlers (pull, pip), and a
  // step the overlay does not recognise as final renders as a stall until the
  // stuck timer fires five minutes later.
  const isFailed = step === 'failed' || step === 'error'
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef(Date.now())

  // Track elapsed time for stuck detection
  useEffect(() => {
    startRef.current = Date.now()
    const timer = setInterval(() => setElapsed(Date.now() - startRef.current), 1000)
    return () => clearInterval(timer)
  }, [])

  // Reset timer when step changes (progress is being made)
  const stepRef = useRef(step)
  useEffect(() => {
    if (step !== stepRef.current) {
      startRef.current = Date.now()
      setElapsed(0)
      stepRef.current = step
    }
  }, [step])

  const isStuck = elapsed > STUCK_THRESHOLD_MS && !isFailed
  const elapsedSec = Math.floor(elapsed / 1000)
  const elapsedStr = elapsedSec >= 60 ? `${Math.floor(elapsedSec / 60)}m ${elapsedSec % 60}s` : `${elapsedSec}s`

  const handleCancel = useCallback(async () => {
    try { await api.cancelUpdate() } catch { /* ignore */ }
    dispatch(setUpdateProgress(null))
    onCancel()
  }, [dispatch, onCancel])

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 backdrop-blur-xs animate-rise">
      <div className="bg-card border border-border rounded-xl p-8 max-w-md w-full mx-4 shadow-xl text-center">
        {/* A terminal step is not in progress, so it does not pulse. */}
        <div className={`text-4xl mb-4 ${isFailed ? 'text-danger' : 'animate-pulse'}`} data-testid="update-overlay-step-icon">{info?.icon || <RefreshCw className="lucide-inline" />}</div>
        <div className="text-lg font-bold text-text-strong mb-2">{i18nT('app.updating_kirocrew')}</div>
        <div className="text-sm text-muted mb-5">{detail || i18nT('app.starting_update')}</div>
        {/* Step progress */}
        <div className="flex flex-col gap-2 text-left mb-5">
          {STEP_ORDER.map((s, i) => {
            const si = UPDATE_STEPS[s]
            const done = currentIdx > i
            const active = currentIdx === i && !isFailed
            return (
              <div key={s} className={`flex items-center gap-2.5 text-[13px] transition-colors ${done ? 'text-ok' : active ? 'text-accent font-medium' : 'text-muted/40'}`}>
                <span className="w-5 text-center">{done ? <Check className="lucide-inline" /> : active ? si.icon : '○'}</span>
                <span>{i18nT(UPDATE_STEP_LABEL_KEY[s])}</span>
                {active && <span className="ml-auto text-[11px] text-muted animate-pulse">{elapsedStr}</span>}
              </div>
            )
          })}
        </div>
        {isFailed ? (
          <div className="flex flex-col gap-3 items-center">
            {/* askAgent ON: a failed step has already stopped the worker, so
                the hand-off can destroy nothing; the causes (pull refused,
                pip refusing the merged revision) are diagnosable by the agent.
                The hand-off navigates to chat UNDER this z-[100] overlay, so
                it also dismisses the overlay -- the same clear as Dismiss. */}
            <ErrorNotice
              askAgent
              className="text-left"
              message={detail || i18nT('app.check_logs_for_details')}
              onHandoff={handleCancel}
              testId="update-overlay-error"
            />
            <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-card border border-border text-text hover:border-border-strong transition-colors" onClick={handleCancel}>
              {i18nT('app.dismiss')}
            </button>
          </div>
        ) : isStuck ? (
          <div className="flex flex-col gap-3 items-center">
            <div className="text-sm text-warn">{i18nT('app.this_step_seems_to_be_taking_longer_than_expecte')}</div>
            <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-danger/10 border border-danger/30 text-danger-text hover:bg-danger/20 transition-colors" onClick={handleCancel}>
              {i18nT('app.cancel_update')}
            </button>
          </div>
        ) : !connected ? (
          // The gateway went down mid-update — during the restart step that is
          // the exec doing its job, and the health probe + WS backoff are
          // already dialing. Say so, with the live elapsed count, instead of
          // leaving a frozen step list that reads as a hang. On reconnect the
          // restart latch (useWebSocket) reloads this tab, which is what
          // finally clears the overlay.
          <div className="text-[13px] text-accent flex items-center justify-center gap-1.5" role="status" data-testid="update-reconnecting">
            <RefreshCw size={13} className="lucide-inline animate-spin" /> {i18nT('app.gateway_restarting_reconnecting')} ({elapsedStr})
          </div>
        ) : (
          <div className="text-[13px] text-muted">{i18nT('app.page_will_reconnect_when_ready')}</div>
        )}
      </div>
    </div>
  )
}

/** Rendered by the shell owners, and re-exported here for the tests and capture
 *  harnesses that import them from the app root. */
export { MobileNavGlyph } from './shell/topbar/shellHeader'
export { NavItem, NavBadge }

export default function App() {
  const shell = useAppShell()
  const {
    isEmbed, isPopout, initialPopoutPath, appPopoutRoute, updateAvailable, terminalEnabled, terminalPoppedOut,
    bottomTerminalOpen, mobileConnect, activeSlotProject, terminalPosition, navigate, mayLeaveForNavigation,
    colorTheme, onboarded, privacyAcked, markOnboarded, markImportOnboarded, markPrivacyAcked, firstRun,
    lookPreviewFrame, branding, avatar, isMobile, bottomDock, activeInstanceId, macFullscreen, macInset,
    topReservePx, focusActive, topPeek, railPeek, topPeekTrigger, railPeekTrigger, topChromeShown,
    gatewayStatusMsg, focusModeExitMsg, focusChromeInert,
    localHeaderDragGaps, shellEntered, setShellEntered, shellRef, setMembersBar, advertisedNavItems,
    sortedAppGroup, slotOwners, appBadges, discoverBadges, railAppBadges, railAppRunStates, shortcutsOpen,
    setShortcutsOpen, toggleShortcutsModal, commandPalette, agentSwitchNotice, kiroUsageOpen, setKiroUsageOpen,
    kiroUsageState, kiroAccountEntry, metrics, subscribeLogs, subscribeSubagents, forceReconnect,
    updateFlow, updating, setUpdating, showUpdateModal, setShowUpdateModal, showChangelog, updateError,
    setUpdateError, startupVideo, reportProblemOpen, setReportProblemOpen, effectiveCollapsed, activePath,
    libraryNavActive, discoverNavActive, isChat, needsFixedHeight, navRowActive, membersRoute, mobileSingle,
  } = shell
  // "Run in terminal" from a code block outside every chat, on any route.
  const runInTerminal = useRunInTerminalShellBridge(activeSlotProject, !isPopout && !isEmbed)

  /**
   * Phone chat page: the main navigation as a 72px icon rail.
   *
   * The chat page renders this beside its sessions pane, inside the ONE drawer
   * a phone chat has (MobileNavRailContext). The rows are the shell's — the
   * same registry (`advertisedNavItems`, `sortedAppGroup`, the Bottom group),
   * the same `NavItem`, the same badges and active rules the desktop rail and
   * the nav drawer use — so a destination added to the registry appears here
   * without a second list to maintain. `touch` gives each row a 64x56
   * `rounded-xl` tile with a one-word caption under the glyph -- the desktop
   * rail names its collapsed rows with a hover tip a finger cannot summon; the
   * selected paint is the desktop rail's.
   *
   * Two rows behave differently from the nav drawer's, both because the host
   * drawer minted a duplicate history entry when it opened (see ChatPage's
   * `pushDrawerEntry`): the row for the page the user is ON only closes the
   * drawer (`onActivate`), and a row that leaves the chat page navigates with
   * `replace` so Back returns to the chat rather than to a second copy of it.
   * That is a property of the drawer that hosts the rail, so it is not an option.
   *
   * The only row the full nav drawer offers and this rail does not is
   * Connect-your-phone, which is moot on the phone itself. Terminal toggles the
   * docked panel and closes the drawer so the panel is not left behind it.
   *
   * The brand mark on top is a control -- the product's "home": it goes to the
   * chat root (the page every other app's logo returns to) and closes the
   * drawer. A cold reader tapped it expecting exactly that, and an inert mark
   * in the tap-target position of every other app read as broken. Search is
   * pinned at the bottom and opens the same command palette the header's
   * search square used to.
   *
   * `null` off the phone chat and Crewmates pages, so every other consumer
   * renders no rail. The Crewmates page hosts it the same way, beside its
   * roster in the drawer over the open chat.
   */
  const railHost = mobileSingle || (isMobile && membersRoute)
  const mobileNavRail = railHost
    ? ({ onActivate }: MobileNavRailOptions) => {
      const railRow = (
        n: { path: string; id: string; label: string; labelKey?: string; icon: React.ReactNode; appName?: string },
      ) => {
        const active = navRowActive(n.path)
        return (
          <NavItem
            key={n.id}
            navId={n.id}
            path={n.path}
            label={surfaceLabel(n)}
            icon={n.icon}
            active={active}
            collapsed
            touch
            replace
            caption={n.id === 'capabilities' ? i18nT('nav.agent_capabilities_short') : undefined}
            onClickOverride={active ? onActivate : undefined}
            badge={<NavBadge navId={n.id} collapsed appBadges={isAppNavId(n.id) ? railAppBadges : appBadges} runState={n.appName ? railAppRunStates[n.appName] : undefined} />}
          />
        )
      }
      const settingsSurface = NAV_ITEMS.find(n => n.id === 'settings')!
      const capabilitiesSurface = NAV_ITEMS.find(n => n.id === 'capabilities')!
      const searchLabel = slotOwners['quick-search']
        ? i18nT('app.open_command_bar')
        : i18nT('app.search_sessions_files_and_commands')
      return (
        <AdaptiveMobileRail
          data-testid="mobile-nav-rail"
          role="navigation"
          aria-label={i18nT('app.main_navigation')}
          className="w-[72px] shrink-0 h-full flex flex-col items-center gap-1 pt-1.5 pb-2.5 border-r border-border bg-bg-accent overflow-y-auto overflow-x-hidden overscroll-y-contain scrollbar-none"
          style={{ scrollbarWidth: 'none' }}
          top={<>
            <button
              type="button"
              data-testid="mobile-nav-rail-home"
              // On the chat page it closes the host drawer (and goes to the chat
              // root). From another host page it navigates with `replace` and
              // does NOT close first: closing pops the drawer's duplicate
              // entry, and that pop would land after the replace and carry the
              // user back to the page being left. Leaving asks the page's
              // leave guard first, as every rail row does, so a draft held
              // there (a Profile schedule form) is not dropped silently.
              onClick={() => {
                if (!isChat) { if (mayLeaveForNavigation()) navigate('/chat', { replace: true }); return }
                onActivate()
                if (!(activePath === '/chat' || activePath === '/')) navigate('/chat', { replace: true })
              }}
              className="w-11 h-11 mb-1 flex items-center justify-center shrink-0 rounded-xl bg-transparent border-none cursor-pointer"
              // Named for what it DOES (home = the chat root), not for the brand
              // it shows: an icon-only control announced as the product name told
              // a screen-reader user nothing about where the tap goes.
              aria-label={i18nT('nav.home')}
            >
              <RailHeaderGlyph avatar={avatar} boxClass={branding?.logoClass ?? 'w-7 h-7'} iconSize={18} />
            </button>
            {advertisedNavItems.filter(n => n.group === 'Main').map(railRow)}
            <NavItem
              navId="apps"
              path="/apps"
              label={i18nT('nav.discover')}
              icon={<Compass size={16} />}
              active={discoverNavActive}
              collapsed
              touch
              replace
              onClickOverride={discoverNavActive ? onActivate : undefined}
              badge={<NavBadge navId="apps" collapsed appBadges={discoverBadges} />}
            />
            <NavItem
              navId="apps-library"
              path="/apps/library"
              label={i18nT('nav.library')}
              icon={<LayoutGrid size={16} />}
              active={libraryNavActive}
              collapsed
              touch
              replace
              onClickOverride={libraryNavActive ? onActivate : undefined}
            />
          </>}
          apps={sortedAppGroup.map(railRow)}
          // Pinned above Settings while the rail has room; folded into the Apps
          // scroller (behind a divider) on screens where pinning them would leave
          // the app tiles less than four rows (shell/nav/adaptiveMobileRail.tsx).
          secondary={<>
            {terminalEnabled && (
              <NavItem
                navId="terminal"
                path="#"
                label={i18nT('app.terminal')}
                icon={<SquareTerminal size={16} />}
                active={bottomTerminalOpen || terminalPoppedOut}
                pressed={bottomTerminalOpen || terminalPoppedOut}
                collapsed
                touch
                onClickOverride={() => { onActivate(); if (terminalPoppedOut) focusTerminalPopout(); else toggleBottomTerminal(activeSlotProject) }}
              />
            )}
            {railRow(capabilitiesSurface)}
            {/* The account modal (balance, sign-in state): the desktop opens it
                from the readout capsule, which the phone does not render, so the
                rail carries it -- on exactly the readings the desktop segment
                shows (`kiroAccountEntry`). Toggles a surface, so `pressed`. */}
            {kiroAccountEntry && (
              <NavItem
                navId="account"
                path="#"
                label={i18nT('components.kiroAccountModal.kiro_account')}
                icon={<Coins size={16} />}
                active={kiroUsageOpen}
                pressed={kiroUsageOpen}
                collapsed
                touch
                onClickOverride={() => { onActivate(); setKiroUsageOpen(true) }}
              />
            )}
          </>}
          bottom={<>
            <NavItem
              path={settingsSurface.path}
              label={surfaceLabel(settingsSurface)}
              icon={settingsSurface.icon}
              active={navRowActive(settingsSurface.path)}
              collapsed
              touch
              replace
              onClickOverride={navRowActive(settingsSurface.path) ? onActivate : undefined}
              badge={updateAvailable ? <span title={i18nT('app.update_available')} role="status" aria-label={i18nT('app.update_available_2')} className="absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10" /> : undefined}
            />
            <button
              type="button"
              data-testid="mobile-nav-rail-search"
              // The same palette as the top bar's Search, under its id for a
              // guide (`guide/trustRoot.ts`): its label is a variable, so it
              // is not a registered site of its own.
              {...guideTarget('shell.search')}
              onClick={() => { onActivate(); commandPalette.openPalette() }}
              className="mt-1 w-16 h-14 px-0.5 rounded-xl border border-border bg-card text-text flex flex-col items-center justify-center gap-0.5 cursor-pointer shrink-0"
              aria-label={searchLabel}
              title={searchLabel}
            >
              <SearchIcon size={18} />
              <span aria-hidden="true" className="max-w-full whitespace-normal text-center text-[10px] leading-[1.1] font-medium tracking-tight line-clamp-2">{i18nT('nav.search_short')}</span>
            </button>
          </>}
        />
      )
    }
    : null

  return (
    <ZoomProvider>
    <WsContext.Provider value={{ subscribeLogs, subscribeSubagents, forceReconnect }}>
    {/* The import row's outcome lands after its menu has closed, so every layout
        that can host the row hosts the notice: the popout and embed frames here,
        the dashboard inside the local pane (below), where it hides with the
        local Crew. */}
    {(isPopout || isEmbed) && <ImportSessionOutcomeNotice />}
    {/* Same rule for a file-menu action's result: the dashboard shows it in the
        notification sheet's banner region, which these frames do not render. */}
    {(isPopout || isEmbed) && <FileMenuResultToastFrame />}
    {appPopoutRoute ? (
      /* An app pop-out window. Its router is mounted at `/popout/app`
         (main.tsx, via appPopoutRouterBasename), so the paths here are the
         app's OWN — `/dev-fleet/…`, `/apps/<name>` — exactly as the dashboard
         branch below spells them, and an app's absolute sub-path navigation
         resolves unchanged. The two arms are the dashboard's two app arms; the
         frame renders the pinned app for its own route and hands any other
         destination (an app's "go to chat" exit, a link to Settings) to the
         main window instead of mounting it here. The hard-exit sink is
         installed above the route boundary, so a hard "Ask the agent" from a
         crashed app (or a crashed frame) hands off to main instead of loading
         the dashboard here. */
      <>
        <AppPopoutHardExit />
        <Routes>
          <Route path="/apps/:name" element={<ErrorBoundary><AppPopoutFrame route={appPopoutRoute} /></ErrorBoundary>} />
          <Route path="/:builtinApp/*" element={<ErrorBoundary><AppPopoutFrame route={appPopoutRoute} /></ErrorBoundary>} />
          <Route path="*" element={<ErrorBoundary><AppPopoutFrame route={appPopoutRoute} /></ErrorBoundary>} />
        </Routes>
      </>
    ) : isPopout ? (
      <Routes>
        <Route path="/popout/chat/:slug?" element={<ErrorBoundary><PopoutFrame /></ErrorBoundary>} />
        <Route path="/popout/artifact/:slug" element={<ErrorBoundary><ArtifactPopoutFrame /></ErrorBoundary>} />
        <Route path="/popout/terminal" element={<ErrorBoundary><TerminalPopoutFrame /></ErrorBoundary>} />
        {/* Belt-and-braces: any stray in-window navigation re-pins to the
            frame this window loaded as (isPopout is sticky, so the dashboard
            branch is unreachable — without this the wildcard would bounce a
            stray path to '/', which no longer matches anything here). */}
        <Route path="*" element={<Navigate to={initialPopoutPath} replace />} />
      </Routes>
    ) : isEmbed ? (
      <div className="h-screen supports-[height:100dvh]:h-dvh w-screen overflow-hidden bg-bg flex flex-col">
        <KiroCrewNavBridge />
        <EmbedTabStrip />
        <div className="flex-1 min-h-0">
          <Routes>
            <Route path="/embed/chat/:slug?" element={<ErrorBoundary><ChatPage embedded embedMode="chat" /></ErrorBoundary>} />
            <Route path="/embed/sessions" element={<ErrorBoundary><ChatPage embedded embedMode="sessions" /></ErrorBoundary>} />
            <Route path="/embed/settings" element={<ErrorBoundary><EmbedSettingsPage /></ErrorBoundary>} />
            <Route path="*" element={<Navigate to="/embed/sessions" replace />} />
          </Routes>
        </div>
      </div>
    ) : (
    /* h-dvh (100vh fallback) so the shell tracks the visible viewport on
       mobile: a 100vh shell extends under the browser's collapsible UI,
       which hides the bottom row (the chat composer) on phones.
       w-full, not w-screen: 100vw resolves independently of layout, so it can
       disagree with the `(max-width: 767px)` query this shell branches on. */
    <TerminalHostContext.Provider value={terminalPoppedOut ? 'detached' : 'docked'}>
    <div className="h-screen supports-[height:100dvh]:h-dvh w-full flex flex-col overflow-hidden bg-bg"
      data-testid="app-frame"
      style={topReservePx ? { paddingTop: topReservePx } : undefined}>
      {/* Embedded remote panes receive their switcher model from the parent via
          this bridge (option B) — no-op in the top-level dashboard. */}
      <EmbeddedHostBridge />
      {/* Embedded remote panes report their header's control-free gaps up to the
          Electron host so it can make the pane title bar draggable — no-op in
          the top-level dashboard and under a browser host. */}
      <EmbeddedDragRegionReporter />
      <div className="flex-1 min-h-0 relative">
      {/* Local pane: the native dashboard. Hidden (not unmounted) while a remote
          instance tab is active, so local state/websocket survive the switch. */}
      <div className="absolute inset-0" style={{ display: activeInstanceId === null ? 'block' : 'none' }}>
      {/* Floating layers this pane opens portal into a container hidden with it
          (see components/overlayRoot). */}
      <LocalOverlayRoot hidden={activeInstanceId !== null}>
    <div
      ref={shellRef}
      data-testid="dashboard-shell"
      className={`relative z-[1] h-full grid ${shellEntered ? '' : 'animate-rise'} overflow-hidden bg-bg p-safe ${isMacElectron ? `mac-electron ${macFullscreen ? 'mac-fullscreen' : ''}` : ''} ${isWinElectron ? 'win-electron' : ''} ${isLinuxFramelessElectron ? 'linux-electron' : ''} ${isMobile ? 'grid-cols-[minmax(0,1fr)] grid-rows-[42px_minmax(0,1fr)]' : bottomDock ? 'grid-rows-[42px_minmax(0,1fr)_auto]' : 'grid-rows-[42px_minmax(0,1fr)]'}`}
      // Retire the entrance animation once it has played, so re-showing this
      // pane cannot replay it. Guarded on BOTH the keyframe name and the event
      // target: `animationend` bubbles, and descendants (banners, cards) use
      // `animate-rise` too, so an unguarded handler would retire the shell's
      // entrance from an unrelated child's animation.
      onAnimationEnd={e => {
        if (e.target === e.currentTarget && e.animationName === 'rise') setShellEntered(true)
      }}
      style={{
        gridTemplateAreas: isMobile ? '"topbar" "content"' : bottomDock ? '"topbar topbar" "nav content" "nav actbar"' : '"topbar topbar topbar" "nav content actbar"',
        ...(!isMobile && {
          gridTemplateColumns: bottomDock
            ? `${focusActive ? 0 : railWidthFor({ isMobile, collapsed: effectiveCollapsed })}px minmax(0,1fr)`
            : `${focusActive ? 0 : railWidthFor({ isMobile, collapsed: effectiveCollapsed })}px minmax(0,1fr) auto`,
          // Transition fires only when the template string itself changes (the
          // collapse toggle) — content-driven resizes of the auto track (e.g.
          // the Activity panel opening) don't alter the value, so keeping this
          // unconditional is safe and avoids the gated-pulse snap regression.
          transition: 'grid-template-columns 150ms cubic-bezier(0.2, 0, 0, 1)',
        }),
        // Focus mode collapses the chrome tracks. Inline so it beats the Tailwind
        // `grid-rows-[42px_...]` class rather than having to fight it there, and
        // so the one platform that needs a gutter (see FOCUS_INSET) can keep it.
        ...(focusActive && {
          gridTemplateRows: bottomDock
            ? `${FOCUS_INSET}px minmax(0,1fr) auto`
            : `${FOCUS_INSET}px minmax(0,1fr)`,
        }),
      }}
    >
      {/* Theme decoration slot (#7377). ThemeExperienceLayer portals a pack's
          decorative overlays here so they share the shell's stacking context
          with the header — rendered as a sibling of <App /> they compete with
          the shell's z-1 as a whole and paint OVER the top bar whatever their
          z-index (see lib/themeDecorLayer.ts). Fixed + inset-0 so it takes no
          grid cell; click-through so it never intercepts (an overlay declaring
          pointerEvents opts its own iframe back in); its own stacking context
          at OVERLAY_Z_MAX so nothing inside can outrank the header (TOPBAR_Z /
          TOPBAR_FOCUS_Z). Must precede the header in DOM order. */}
      <div
        id={THEME_DECOR_SLOT_ID}
        ref={registerThemeDecorSlot}
        data-testid="theme-decor-slot"
        className="fixed inset-0 pointer-events-none"
        style={{ zIndex: OVERLAY_Z_MAX }}
      />
      {/* Behind-content slot for `layer: "behind"` overlays. z-index -1 inside
          the shell's stacking context paints over the shell's bg-bg but under
          the non-positioned nav and content, so bubbles and cards cover it. */}
      <div
        id={THEME_DECOR_BEHIND_SLOT_ID}
        ref={registerThemeDecorBehindSlot}
        data-testid="theme-decor-slot-behind"
        className="fixed inset-0 pointer-events-none"
        style={{ zIndex: -1 }}
      />

      {/* Full-height activity bar slot: ChatSidePanels portals its
          Activity panel here on desktop so it spans the window top-to-bottom
          instead of sitting below the header row. Empty (0 width) when the
          panel is closed or on non-chat routes. */}
      {!isMobile && <div id="activity-bar-slot" className="h-full min-h-0 min-w-0" style={{ gridArea: 'actbar' }} />}

      {/* Skip to content — visible only on focus for keyboard users */}
      <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[9999] focus:px-4 focus:py-2 focus:rounded-lg focus:bg-accent focus:text-accent-fg focus:text-sm focus:font-medium">{i18nT('app.skip_to_content')}</a>
      {/* Spoken way out of focus mode. Turning it on makes the header and rail
          inert and moves focus to main, so a screen-reader user who clicked the
          toggle has lost every chrome control and hears nothing about how to get
          them back. This region is mounted permanently and its TEXT changes, so
          the message is announced (a live region does not speak content it
          mounts with). Outside the header on purpose: that is the element that
          turns aria-hidden. Empty while focus mode is off. */}
      <span role="status" className="sr-only" data-testid="focus-mode-announce">
        {focusActive ? focusModeExitMsg : ''}
      </span>

      {/* Focus mode: edge strips that summon the hidden chrome. Rendered before
          the chrome itself, but BELOW it in z-order (61 vs 62): the chrome covers
          the strip it was summoned by, so hover and clicks land on the chrome's own
          surface handlers and the strip never has to resize or opt out of
          hit-testing — a hit target that changes under a resting pointer is what
          made this flicker open/closed indefinitely. `.focus-peek-top` / `.focus-peek-rail`
          (index.css) carry `-webkit-app-region:no-drag`, which is load-bearing on the TOP one: Electron injects a 42px drag bar on
          document.body, and an ordinary div inside it becomes a window-drag
          region whose hover never reaches React. */}
      {focusActive && (
        <>
          <div
            ref={topPeekTrigger}
            data-testid="focus-peek-top"
            aria-hidden="true"
            className="focus-peek-top absolute left-0 right-0 top-0 z-[61]"
            {...topPeek.triggerProps}
          />
          <div
            ref={railPeekTrigger}
            data-testid="focus-peek-rail"
            aria-hidden="true"
            className="focus-peek-rail absolute left-0 bottom-0 z-[61]"
            // Starts below the top strip so the two tile the corner rather than
            // overlapping, where whichever won would be arbitrary.
            style={{ top: FOCUS_INSET }}
            {...railPeek.triggerProps}
          />
          {/* While the header is hidden it is aria-hidden, which silences the
              connection capsule's role="status" region -- the only
              screen-reader carrier of the gateway going offline. This stand-in
              carries the same message from outside the header for exactly that
              interval. Mounted only while hidden, so the two regions never
              announce the same change twice; a live region does not speak the
              text it mounts with, only later changes, which is the case that
              matters (the gateway dropping mid focus mode). Only while the
              header is actually aria-hidden (focusChromeInert). */}
          {!topChromeShown && focusChromeInert && (
            <span role="status" className="sr-only" data-testid="focus-gateway-status">{gatewayStatusMsg}</span>
          )}
        </>
      )}

      <ShellHeader shell={shell} />

      {agentSwitchNotice && <AgentSwitchNoticeBanner notice={agentSwitchNotice} />}

      {/* Report a Problem — mounted by the nav rail's "Report issue" link. */}
      <ReportProblemModal open={reportProblemOpen} onClose={() => setReportProblemOpen(false)} />

      {/* Update error modal */}
      {updateError && (
        <UpdateErrorDialogFrame setError={setUpdateError}>
          <div className="bg-card border border-border rounded-xl p-8 max-w-md w-full mx-4 shadow-xl text-center">
            <div className="text-4xl mb-4"><AlertTriangle className="lucide-inline" /></div>
            <div className="text-lg font-bold text-text-strong mb-2">{i18nT('app.update_failed')}</div>
            <div className="text-sm text-danger mb-6">{updateError}</div>
            {/* A failed self-update is exactly what the agent can diagnose
                (channel, feed, venv state), and a modal has no draft to lose. */}
            <div className="flex items-center justify-center gap-3">
              <AskAgentButton message={updateError} variant="solid" onHandoff={() => setUpdateError('')} />
              <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-card border border-border text-text hover:border-border-strong transition-colors" onClick={() => setUpdateError('')}>
                {i18nT('app.dismiss')}
              </button>
            </div>
          </div>
        </UpdateErrorDialogFrame>
      )}

      {/* Every surface that opens ITSELF at launch, mounted from one place so
          the look-preview frame (utils/lookPreview.ts: the scaled dashboard
          inside the first-run "Pick your look" step) can leave all of them out
          at once. A launch dialog that mounted inside that picture would be
          invisible to every test but the one that boots App in frame mode
          (App.lookPreviewFrame.test.tsx). Add a new auto-opening surface HERE,
          inside this block, never beside it. */}
      {!lookPreviewFrame && (
        <>
          {/* Changelog modal */}
          <ChangelogModal flow={updateFlow} updateAvailable={updateAvailable} />

          {/* Updating overlay */}
          {(updating || showUpdateModal) && <UpdateOverlay onCancel={() => { setUpdating(false); setShowUpdateModal(false) }} />}
          {/* Both held while What's new is open, so two update dialogs never stack;
              each takes its turn once What's new closes, with its state intact. */}
          <UpdateModal held={showChangelog} />
          {updateAvailable && (
            <Suspense fallback={null}>
              <UpdateFoundModal held={showChangelog} />
            </Suspense>
          )}
          <StartupVideo startupVideo={startupVideo} />
          <MobileConnectDialog mobileConnect={mobileConnect} />
          <RemoteUpdateOffer />

          <FirstRunChapters firstRun={firstRun} onboarded={onboarded} privacyAcked={privacyAcked} markOnboarded={markOnboarded} markImportOnboarded={markImportOnboarded} markPrivacyAcked={markPrivacyAcked} />
        </>
      )}

      <ShellNavRail shell={shell} />

      {/* Content */}
      <div
        className="flex flex-col min-h-0 min-w-0"
        // Focus mode reclaims the 236px rail column, which leaves everything in
        // this column — the chat sessions drawer first — flush against the
        // window's left edge, while the same surfaces stay inset 8px at the
        // bottom by their own `mb-2`/`pb-2`. The inset goes on the COLUMN rather
        // than on the drawer: the drawer's collapse animates a clip-path whose
        // insets are computed in its own container space against its `width`
        // prop, so padding it would desync the morph from the toggle it converges
        // on. Padding the column shifts the drawer and that toggle together.
        // Transition matched to the shell's own column animation so the 8px
        // arrives with the track change instead of snapping ahead of it.
        style={focusActive
          ? {
            gridArea: 'content',
            paddingLeft: FOCUS_INSET,
            transition: 'padding-left 150ms cubic-bezier(0.2, 0, 0, 1)',
          }
          : { gridArea: 'content' }}
      >
        <div className={`flex min-h-0 min-w-0 flex-1 ${terminalPosition === 'right' ? 'flex-row' : 'flex-col'}`}>
        {/* `outline-hidden`: main is a programmatic focus target (the skip
            link, and focus mode's hand-off when the chrome hides), not a
            control, so it opts out of the global :focus-visible ring the way
            the tabIndex={-1} page headings do. */}
        <main id="main-content" tabIndex={-1} className={`outline-hidden flex flex-col min-h-0 min-w-0 flex-1 overflow-x-hidden ${needsFixedHeight ? 'overflow-hidden p-0' : 'overflow-y-auto'}`}>
          <MigrationCheck />
          {/* Route-independent, unlike MigrationCheck: "you crashed" is true of
              the app, not of the page, and the launch after a crash rarely lands
              on the page the user was on when it happened. */}
          <CrashReportNotice />
          {/* Route-independent for the same reason: a lapsed Kiro sign-in
              stops every KAS agent, whatever page is open. It hides itself
              off KAS, while the sign-in works, and on the page with the card.
              A notice that fails to load or render says so, with the hand-off. */}
          <ErrorBoundary
            scope="kiro-sign-in-notice"
            fallback={error => <KiroSignInNoticeFallback error={error} />}
          >
            <Suspense fallback={null}>
              <KiroSignInNotice />
            </Suspense>
          </ErrorBoundary>
          {/* The rail renderer reaches the chat page through context rather than
              a prop: the route element is shared with the popout/embed frames. */}
          <MobileNavRailContext.Provider value={mobileNavRail}>
          <MobileRosterBarContext.Provider value={setMembersBar}>
          {/* Registered-action guide: offered in the chat it came from, driven
              only after the human presses Start; the pages it walks through
              read their draft and request-header seams from this provider. */}
          <GuideProvider>
          <Suspense fallback={null}><GuideLayer /></Suspense>
          <Suspense fallback={null}><LinkPreviewLayer /></Suspense>
          <Routes>
            <Route path="/chat/:slug?" element={<ErrorBoundary><ChatPage /></ErrorBoundary>} />
            <Route path="/orchestrated/:slug?" element={<OrchestratedRedirect />} />
            <Route path="/notifications" element={<ErrorBoundary><NotificationsPage /></ErrorBoundary>} />
            {/* Bookmarkable session chooser: neutral list, no auto-select; rows
                open the full /chat/<key> experience inside this same shell. */}
            <Route path="/sessions" element={<ErrorBoundary><Suspense fallback={null}><SessionsPage /></Suspense></ErrorBoundary>} />
            <Route path="/session-dashboards" element={<ErrorBoundary><Suspense fallback={null}><SessionDashboardsPage /></Suspense></ErrorBoundary>} />
            {/* Knowledge moved into Agent Capabilities; old bookmarks land on its tab. */}
            <Route path="/knowledge" element={<Navigate to="/capabilities?tab=knowledge" replace />} />

            <Route path="/members" element={<ErrorBoundary><Suspense fallback={null}><MembersPage /></Suspense></ErrorBoundary>} />
            <Route path="/overview" element={<Navigate to="/settings/overview" replace />} />
            <Route path="/crew-board" element={<ErrorBoundary><Suspense fallback={null}><CrewBoardPage /></Suspense></ErrorBoundary>} />
            <Route path="/schedule" element={<SchedulePage />} />
            {/* Agents and Connections live in the Agent Capabilities panel. */}
            <Route path="/agents" element={<Navigate to="/capabilities" replace />} />
            <Route path="/mc-agents" element={<Navigate to="/capabilities" replace />} />
            <Route path="/connections" element={<Navigate to="/capabilities?tab=mcp" replace />} />
            <Route path="/tasks" element={<TasksRedirect />} />
            <Route path="/logs" element={<LogsPage />} />
            <Route path="/hooks" element={<HooksPage />} />
            <Route path="/webhooks" element={<ErrorBoundary><WebhooksPage /></ErrorBoundary>} />
            <Route path="/capabilities" element={<CapabilitiesPage />} />
            {/* Instances setup moved into Settings; switching happens via the header tab strip. */}
            <Route path="/instances" element={<Navigate to="/settings/instances" replace />} />
            {/* Static segments (library, detail, migrate) MUST stay registered
                before the /apps/:name installed-app catch-all -- they are
                reserved app-name words enforced server-side. The '-/' prefix
                (e.g. /apps/-/updates) needs NO server-side reservation: '-' is
                not a valid app name, so it can never collide with an installed
                app -- the reserved set stays frozen at 'library'. */}
            <Route path="/apps" element={<Suspense fallback={null}><DiscoverPage /></Suspense>} />
            <Route path="/apps/-/updates" element={<Suspense fallback={null}><DiscoverPage /></Suspense>} />
            <Route path="/apps/library" element={<Suspense fallback={null}><LibraryPage /></Suspense>} />
            <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            <Route path="/apps/migrate/:name" element={<MigrationPage />} />
            <Route path="/apps/:name" element={<AppPage />} />
            {/* Splat route: SettingsPage parses the trailing segments itself
                (segment[0] = tab, segment[1] = sub; deeper segments reserved).
                Matches bare /settings too (empty splat). */}
            <Route path="/settings/*" element={<SettingsPage />} />
            <Route path="/developer" element={<DeveloperPage />} />
            {/* Dev-only layout-editor harness (RFC §7 PR 2) — a standalone route
                to exercise the editor in isolation. Not linked from nav. */}
            <Route path="/developer/layout-editor" element={<ErrorBoundary><Suspense fallback={null}><LayoutEditorHarnessPage /></Suspense></ErrorBoundary>} />
            <Route path="/artifacts" element={<ArtifactsPage />} />
            <Route path="/artifacts/deploy" element={<Navigate to="/deploy" replace />} />
            <Route path="/artifacts/remote/:provider/:externalId" element={<ErrorBoundary><RemoteArtifactDetailPage /></ErrorBoundary>} />
            <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
            <Route path="/deploy" element={<ArtifactDeployPage />} />
            {/* Builtin app routes — auto-discovered from registry. React Router v6
                ranks static paths higher than parameterized ones, so /settings, /agents
                etc. still match first. Unrecognized paths fall through to /chat.
                The trailing splat also matches the BARE app path (empty splat),
                so this one arm serves /aws-control and /aws-control/usage alike —
                an app carries sub-segments for its own path navigation, same
                shape as /settings/<tab>. */}
            <Route path="/:builtinApp/*" element={<BuiltinAppRoute />} />
            <Route path="*" element={<ChatRedirect />} />
          </Routes>
          </GuideProvider>
          </MobileRosterBarContext.Provider>
          </MobileNavRailContext.Provider>
        </main>
        {/* App-wide docked terminal panel — renders beside <main> (right) or
            below it (bottom). The detached bar (popped-out state) always renders
            below the flex wrapper as a full-width strip regardless of position. */}
        {terminalEnabled && !terminalPoppedOut && <BottomTerminalPanel />}
        </div>{/* /flex-row or flex-col wrapper */}
        {terminalEnabled && terminalPoppedOut && <TerminalDetachedBar />}

        {/* Self-managed floating panels: lifecycle-driven (hidden → small → chip),
            not motion.* children, so they live outside AnimatePresence. The browse
            mirror docks bottom-right and the computer-use PiP bottom-left, so both
            can be open at once. */}
        <ComputerUseLiveView />
      </div>
    </div>{/* /Local dashboard grid */}
      {/* Opened from the local Crew, so they hide with it (components/overlayRoot).
          The image viewer and the import notice render in place, fixed layers
          inside this pane, so the pane's own display:none hides them; the other
          two portal. The popout and embed frames mount their own copies. */}
      <MetricsCard metrics={metrics} />
      <KiroAccountModal open={kiroUsageOpen} onClose={() => setKiroUsageOpen(false)} usage={kiroUsageState} />
      <Lightbox />
      <ImportSessionOutcomeNotice navigateToChat />
      <RunInTerminalShellNotice error={runInTerminal.error} onDismiss={runInTerminal.dismiss} />
      </LocalOverlayRoot>
      </div>{/* /Local pane */}
      {/* Remote instance panes — embedded dashboards kept warm (mounted, hidden)
          so switching is instant; the active instance fills the pane. */}
      <InstancesViewport macInset={macInset} />
      {/* macOS focus mode: window-drag strips for the LOCAL header, placed to be
          structurally identical to the pane strips that provably work — the
          .host-drag-strip mechanism, in the same top-level container, OUTSIDE
          the shell's grid/overflow/stacking context, painted after everything
          drag-related. Every in-shell variant failed on the desktop app. */}
      {activeInstanceId === null && focusActive && macInset && topChromeShown &&
        localHeaderDragGaps.map((g, i) => (
          <div key={`fm-drag-${i}`} aria-hidden data-testid="focus-mac-drag-strip" className="host-drag-strip" style={{ left: g.x, width: g.w, zIndex: 63 }} />
        ))}
      </div>{/* /pane stack */}
    </div>
    </TerminalHostContext.Provider>
    )}
    </WsContext.Provider>
    {shortcutsOpen && <ShortcutsModal onClose={() => setShortcutsOpen(false)} />}
    <QuickSearchSurface
      owners={slotOwners}
      open={commandPalette.open}
      onClose={commandPalette.close}
      openShortcuts={toggleShortcutsModal}
    />
    {/* Theme decoration: always-mounted decorative overlays (widgets,
        transitions) contributed by the active theme's branding. Absent unless
        a registered theme declares them. Each overlay is isolated in its own
        ErrorBoundary (fallback=null) so a throwing overlay disables only itself,
        not the shell or its siblings. */}
    {branding?.overlays?.map((Overlay, i) => (
      <ErrorBoundary key={`${colorTheme}:${i}`} scope={`theme-overlay:${i}`} fallback={null}>
        <Overlay />
      </ErrorBoundary>
    ))}
    {(isPopout || isEmbed) && <Lightbox />}
    </ZoomProvider>
  )
}
