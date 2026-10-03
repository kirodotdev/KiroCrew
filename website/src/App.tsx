import { useEffect, useState, useCallback, useRef, useMemo, createContext, lazy, Suspense, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAppSelector, useAppDispatch } from './store'
import { fetchSlots, sseStatus, setUpdateProgress, setEnabledAppIds } from './store/dashboardSlice'
// Side-effect: registers every built-in surface in the registry, ahead of every
// module below that reads it (`shell/nav/navItems.ts` computes `NAV_ITEMS`).
import './surfaces/builtins'
import { getBuiltinSurface, selectSurfaceBadgeCount, selectSurfaceActivityCount, selectAllSurfacesAttention, surfaceLabel } from './surfaces/registry'
import { setAgentSwitchNotice, switchSlot, selectActiveSlotProject } from './store/chatSlice'
import { setNavIntentHandler as setArtifactNavIntentHandler } from './utils/artifactPopout'
import { applyNavIntentInMain, chatDeepLinkSlot } from './utils/navIntent'
import { installSoftNavigate } from './utils/errorReport'
import { metricColor } from './utils/metricColor'
import { fetchNotifications, armBootNotificationsFallback } from './store/notificationsSlice'
import { useWebSocket } from './hooks/useWebSocket'
import { useDashboardHealthProbe } from './hooks/useDashboardHealthProbe'
import { useConfigAutolinkRules } from './hooks/useConfigAutolinkRules'
import { useTheme } from './hooks/useTheme'
import { useShellBranding } from './shell/branding'
import { useRumPageView } from './hooks/useRumPageView'
import { useIsMobile } from './hooks/useIsMobile'
import { useSidePanelDock } from './hooks/useSidePanelDock'
import { useAppRailOrder, SortableAppNavRow, NavToggle } from './shell/nav/appRail'
import { setRailWidth, railWidthFor } from './hooks/useRailWidth'
import { FOCUS_INSET } from './hooks/useFocusMode'
import { useFocusChrome } from './shell/focus/focusChrome'
import { OVERLAY_Z_MAX, THEME_DECOR_SLOT_ID, TOPBAR_FOCUS_Z, TOPBAR_Z, registerThemeDecorSlot } from './lib/themeDecorLayer'
import { useNativeNotification } from './hooks/useNativeNotification'
import { useNotificationSound } from './hooks/useNotificationSound'
import { recordSessionStart } from './rum'
import { ZoomProvider } from './hooks/ZoomProvider'
import { api, isAuthBannerShown } from './api/client'
import { useKiroUsageReadout, kiroUsageSegment } from './shell/topbar/kiroUsageReadout'
import { safeSetItem } from './utils/safeStorage'
import { gcOrphanedStorage } from './utils/storageGc'
import { useMetricsReadout, metricsSegment, MetricsCard } from './shell/topbar/metricsReadout'
import { Rocket, Bell, Code, RefreshCw, Package, Download, Hammer, XCircle, Check, AlertTriangle, X, Coins, Compass, LayoutGrid, Fullscreen, Menu, SquareTerminal, Bot, Smartphone, Search as SearchIcon } from 'lucide-react'
import { useFirstRunChapters, FirstRunChapters } from './shell/boot/firstRun'
import ErrorNotice from './components/ErrorNotice'
import { PREVIEW_EXPAND_EVENT } from './components/WebPreviewPanel'
import { useMobileConnect, MobileConnectDialog } from './shell/nav/mobileConnect'
import { useMayLeaveForNavigation, useIsCurrentUrl } from './components/NavigationLeaveGuard'
import { motion, useMotionValue, useTransform, useReducedMotion } from 'framer-motion'
import { useDrawerSwipe, animateDrawer, registerDrawerTargets, takeOverDrawer, safeAreaLeft } from './hooks/useDrawerSwipe'

/** Mobile nav drawer travel: its 220px width + the 8px mx-2 inset + border. */
/** Mobile nav drawer width. Shared with its travel below so the two cannot drift
 *  — a travel wider than the panel spends the settle's tail moving something
 *  already off the screen. */
const MOBILE_NAV_WIDTH = 220
/** The `mx-2` inset the panel sits at, so its left edge starts here. */
const MOBILE_NAV_INSET = 8
/** What it takes for the nav drawer to clear the screen: its own width, the
 *  `mx-2` inset it starts at, a hair for the 1px border and `shadow-sm`'s
 *  spread, and the safe-area inset — the panel is pinned at `left-safe`, so on a
 *  notched phone in landscape it starts that far in and has to cross it too.
 *  Was a flat 240, which both overshot the width by 9px (parking the panel
 *  offscreen at 96% of the slide, so the rest of the settle moved nothing) and
 *  ignored the inset (parking it with a strip still visible in landscape). */
const mobileNavTravel = () =>
  MOBILE_NAV_WIDTH + MOBILE_NAV_INSET + 3 + safeAreaLeft()
import { isMacElectron, isWinElectron, isLinuxFramelessElectron } from './lib/electron'
import { setNativeBadgeCount, subscribeNativeNavigate, useMacFullscreen } from './shell/platform/electronBridge'
import { DndContext, closestCenter, DragOverlay } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import ChatPage from './pages/ChatPage'
import PopoutFrame from './pages/PopoutFrame'
import ArtifactPopoutFrame from './pages/ArtifactPopoutFrame'
import TerminalPopoutFrame from './pages/TerminalPopoutFrame'

import ErrorBoundary from './components/ErrorBoundary'
import AskAgentButton from './components/AskAgentButton'
import AppIcon from './components/AppIcon'
import { Lightbox } from './components/MarkdownRenderer'
const SessionsPage = lazy(() => import('./pages/SessionsPage'))
import { useNotificationSheet, NotificationSheet } from './shell/notifications/notificationSheet'
import LogsPage from './pages/LogsPage'
// Lazy: /members is a standalone surface not needed at startup, and the main
// chunk sits at its size budget — the import() boundary keeps the page (and
// its drawer/roster tree) out of the initial bundle.
const MembersPage = lazy(() => import('./pages/members/MembersPage'))
// Lazy for the same reason: the crew work-item board is opened from a conductor
// session or the Crew page, never at startup.
const CrewBoardPage = lazy(() => import('./pages/CrewBoardPage'))
const SessionDashboardsPage = lazy(() => import('./pages/chat/command-center/SessionDashboardsPage'))
import ArtifactDetailPage from './pages/ArtifactDetailPage'
import { useUpdateFlow, ChangelogModal } from './shell/updates/updateFlow'
import KiroCrewNavBridge from './components/KiroCrewNavBridge'
import InstanceTabBar from './components/InstanceTabBar'
import InstancesViewport from './components/InstancesViewport'
import EmbeddedHostBridge from './components/EmbeddedHostBridge'
import EmbeddedDragRegionReporter from './components/EmbeddedDragRegionReporter'
import EmbedTabStrip from './components/EmbedTabStrip'
// Dev-only layout-editor harness (RFC §7 PR 2). Lazy so it never weighs the main
// bundle — it is a developer route, not a shipped surface.
const LayoutEditorHarnessPage = lazy(() => import('./pages/LayoutEditorHarnessPage'))
import { useUpdateSubscription } from './hooks/useUpdateSubscription'
import UpdateModal from './components/UpdateModal'

import ComputerUseLiveView from './components/ComputerUseLiveView'
import BottomTerminalPanel, { TerminalDetachedBar } from './components/BottomTerminalPanel'
import { toggleBottomTerminal, useBottomTerminalOpen, useTerminalPosition } from './hooks/useBottomTerminal'
import { useTerminalRestoreProbe } from './shell/boot/terminalRestore'
// Side effect: closes a terminal tab when its shell exits (main window and popout).
import './utils/terminalExitClose'
import { useTerminalPoppedOut, focusPopout as focusTerminalPopout } from './utils/terminalPopout'
import { activateTerminalEntry, isTerminalShown } from './lib/terminalEntry'
import MigrationCheck from './components/MigrationCheck'
import CrashReportNotice from './components/CrashReportNotice'
import { ImportSessionOutcomeNotice } from './components/ImportSessionItem'
import BuiltinAppRoute from './apps/BuiltinAppRoute'
import { getBuiltinIcon } from './apps/builtinIcons'
import { getTopBarWidgets } from './apps/topBarWidgets'
import { getCapsuleSegments } from './apps/capsuleSegments'
import { useRequestFeature } from './shell/topbar/requestFeature'
import { IS_MAC } from './hooks/useKeyboardShortcuts'
import { useNavShortcutHint } from './hooks/useNavShortcutHint'
import { useShellKeyboard } from './shell/shortcuts/shellKeyboard'
import { MobileNavRailContext, type MobileNavRailOptions } from './components/MobileNavRailContext'
import { WorkspacePanelContext, WorkspaceFullscreenContext } from './components/WorkspacePanelContext'
import ShortcutsModal from './components/ShortcutsModal'
import QuickSearchSurface from './components/QuickSearchSurface'
import ReportProblemModal from './components/ReportProblemModal'
import FeedbackPill from './components/FeedbackPill'
import { Glass } from './components/Glass'
import KiroAccountModal from './components/KiroAccountModal'
import WindowsTitlebarMenu from './components/WindowsTitlebarMenu'
import { NavHistoryArrows } from './components/NavHistoryArrows'

import { useStartupVideo, StartupVideo } from './shell/boot/startupVideo'
import { i18nT } from './i18n/t'
import { appNavTarget } from './appNav'
import { isAppNavId } from './appNotificationBadges'
import type { AppRunState } from './appRunState'
import { useGlobalApprovalCount, useRailBadges } from './shell/nav/railBadges'
import { resolveSlotOverlays, type SlotOwners } from './apps/overlaySlots'
import { lazyPage, TasksRedirect, ChatRedirect, OrchestratedRedirect } from './shell/routes'
import { NAV_ITEMS } from './shell/nav/navItems'
import { useNavTip } from './shell/nav/navTip'
import { isChatRoute, useRouteActiveModel } from './shell/nav/routeActive'
import { useDeveloperMode } from './shell/nav/developerMode'
import { RailHeaderGlyph, RailBrandToggle, RailCommunityLinks } from './shell/nav/railChrome'

// Lazy on purpose: the update-found popup (its policy module, Trans runtime
// wiring, and mutation plumbing) is dead weight for every session without an
// update, and the app-core chunk is at its size budget. The `updateAvailable`
// mount gate at the render site means the chunk is fetched exactly when it
// can render.
const UpdateFoundModal = lazy(() => import('./components/UpdateFoundModal'))
// Same boundary, same reason: the pill renders nothing without an update,
// so its code rides the on-demand chunk instead of the app core.
const UpdatePill = lazy(() => import('./components/UpdatePill'))

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

/** Minimal shape of an entry from `GET /api/apps`, limited to the fields the
 *  Apps-nav builder reads. */
interface AppListEntry {
  name: string
  displayName?: string
  enabled?: boolean
  origin?: string
  orphaned?: boolean
  manifest?: {
    iconUrl?: string
    ui?: {
      entry?: string
      pages?: Array<{ route: string; icon?: string; iconUrl?: string; label?: string }>
      overlays?: Array<{ id?: string; label?: string; replaces?: string }>
    }
  }
}
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

// Corner radius, in px, of the top bar's Liquid Glass pills (the search
// trigger, the readout capsule; components/FeedbackPill.tsx carries the same
// number): `rounded-xl`, which the update pill already wears, so the row reads
// as one family of boxes.
const TOPBAR_PILL_RADIUS = 12

// The top-bar search is laid out by CSS, not measured here: `.topbar` in
// index.css is a three-track grid whose centre track is
// `clamp(240px, 22vw, 480px)` and whose side tracks are equal `minmax(0,1fr)`
// remainders, so the search is window-centred by construction and each side
// group adapts its own contents with a container query. The previous
// implementation centred an absolutely-positioned overlay on `50vw`, which
// forced it to reserve `max(left, right)` on BOTH sides and drop itself entirely
// once that mirrored gutter fell under a floor — on an asymmetric header that
// discarded twice the difference between the two clusters.

// Apps-nav fetch resilience (see refreshAppNav). The dashboard loads
// `/api/apps` once on mount; right after a `kirocrew update` the gateway is
// mid-restart (cold backend, apps-dir scan) and that first request can fail or
// time out. Retry with bounded backoff so the Apps rail self-heals instead of
// staying empty until a manual reload or an app enable/disable.
const APP_NAV_MAX_RETRIES = 4
const APP_NAV_RETRY_BASE_MS = 500

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
            <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-danger/10 border border-danger/30 text-danger hover:bg-danger/20 transition-colors" onClick={handleCancel}>
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

/** Glyph inside the mobile nav toggle: the product logo once it has actually
 *  loaded, the Menu hamburger at every other instant. This button is the ONLY
 *  route to the nav rail on a narrow layout, and its logo is a network-fetched
 *  <img> with `alt=""` + `aria-hidden` — so a 404 (asset missing on a proxied
 *  serving path), a blocked request, or a hung fetch used to render NOTHING:
 *  an invisible button that still toggled the rail when tapped blind. The
 *  hamburger therefore shows by default and the swap happens on the img's
 *  `load` event, never on an assumption: `loadedSrc` records WHICH src loaded,
 *  so a branding/theme change falls back to the hamburger until the new asset
 *  proves itself, and an `error` clears the record. The img stays mounted
 *  (display:none) while hidden so the browser still fetches it. The hamburger
 *  sits in a w-6 box matching the img, keeping the 40px tap target and the
 *  16px page-gutter alignment identical through the swap. */
export function MobileNavGlyph({ avatar }: { avatar: string }) {
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null)
  const showLogo = !!avatar && loadedSrc === avatar
  return (
    <>
      {!showLogo && (
        <span data-testid="mobile-nav-fallback" className="w-6 h-6 flex items-center justify-center shrink-0" aria-hidden="true">
          <Menu size={20} />
        </span>
      )}
      {!!avatar && (
        <img src={avatar} alt="" aria-hidden="true" onLoad={() => setLoadedSrc(avatar)} onError={() => setLoadedSrc(null)} className={`w-6 h-6 rounded-md shrink-0 object-contain transition-transform duration-300 group-hover:rotate-[-8deg] ${showLogo ? '' : 'hidden'}`} />
      )}
    </>
  )
}

function BadgeIndicator({ count, collapsed, label }: { count: number; collapsed: boolean; label: string }) {
  if (count <= 0) return null
  const ariaLabel = `${count} ${label}`
  return collapsed
    ? <span className="absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10" role="status" aria-label={ariaLabel} />
    // Expanded: IN FLOW, not `absolute right-2`. The row's other right-edge
    // occupant is the hover/focus shortcut hint, which is an in-flow span — so an
    // absolutely-positioned badge sat ON TOP of it and a row with an unread count
    // advertised a chord the badge covered ("Sessions ⌥C" read as "Sessions (1)"
    // with a sliver of the modifier glyph showing). In flow the two are siblings
    // in the row's flex line and cannot overlap at any count width.
    //
    // `title` names what the number IS, for sighted users: a bare pill beside a
    // bare bot glyph does not say what either counts, and the fix above makes the
    // two reliably co-visible. It carries the LABEL ALONE, not `ariaLabel` — the
    // label is a plural phrase, so "1 unread conversations" would be visibly
    // wrong at count 1, and the count is already rendered in the pill an inch
    // away. `aria-label` keeps the count because a screen reader gets no pill.
    // The update dot (App.tsx) likewise carries different title and aria strings.
    : <span className="shrink-0 bg-accent text-accent-fg text-[12px] font-bold px-1 py-[2px] rounded-full min-w-[18px] text-center inline-block leading-[12px]" title={label} aria-label={ariaLabel}>{count}</span>
}

/** Sub-agent activity belongs in the expanded rail, where the bot icon and
 *  count communicate what is active. The collapsed rail omits it: a second
 *  anonymous dot competes with the unread badge without identifying a session,
 *  while the Sessions list provides the actionable per-session status. */
function ActivityIndicator({ count, collapsed, label }: { count: number; collapsed: boolean; label: string }) {
  if (count <= 0 || collapsed) return null
  const ariaLabel = `${count} ${label}`
  return <span className="shrink-0 flex items-center gap-1 text-[11px] text-accent" role="status" title={label} aria-label={ariaLabel}>
    {/* Size and spacing are main's. A 12px glyph was tried here to make the mark
        identify its own count at a glance, and a reader of the rendered frames
        still could not tell what it depicted -- so it bought nothing and cost the
        row's last pixel of label width (the Sessions label fits in exactly 61px;
        12px takes 62 and clips it to "Sessions" minus two characters). What this
        glyph depicts is a question about the rail's iconography rather than about
        the overlap, so it is left to the follow-up rather than guessed at here.
        The `title` carries the naming for a user who hovers. */}
    <Bot size={11} aria-hidden />
    {count}
  </span>
}

/**
 * Accessible name for a rail run-state mark.
 *
 * A literal key per state rather than one interpolated from the state value:
 * `dynamicKeys.test.ts` polices composed keys, and a literal also keeps the
 * three strings findable by grep from the catalog side.
 */
function runStateLabel(state: AppRunState | undefined): string {
  if (state === 'running') return i18nT('app.app_job_running')
  if (state === 'error') return i18nT('app.app_job_failed')
  if (state === 'success') return i18nT('app.app_job_succeeded')
  return ''
}

/**
 * Run-state mark for an installed app's rail row.
 *
 * Renders in BOTH rail modes, unlike `ActivityIndicator` above. That component
 * withholds itself when collapsed because a second anonymous dot there competes
 * with the unread badge without identifying a session; this mark is the opposite
 * case on both counts -- it names the row's own app, and the collapsed rail is
 * exactly where "is that job still going?" has to be answerable without opening
 * the app, which is the whole point of having it.
 *
 * Shares the row's flex line with the other two indicators rather than claiming
 * a corner of its own. An earlier revision placed each mark at a fixed offset
 * the others were assumed not to use, which is only true while every one of them
 * stays the width it was assumed to be -- a count pill grows with its digits.
 * Siblings in one flex line cannot overlap at any width, so the arrangement no
 * longer rests on an assumption about anyone's size. Independently of geometry,
 * exactly one surface in the registry carries an `activitySelector` -- `chat`
 * (Sessions), a Main-group HOST surface. A host surface row never carries an
 * `appName`, so it never receives this mark, and an app row never receives an
 * activity count: those two cannot meet on one row regardless.
 *
 * Colour alone does not carry the state. `running` is a filled dot inside a wide
 * halo ring, `error` is a plain solid fill, and `success` is HOLLOW -- a ring
 * with no fill. The error/success pair is the one that matters: red and green
 * are the classic confusion, and a static red dot beside a static green one
 * differing only in hue reads a failing job as a fresh success, silently, every
 * time. The fill carries that distinction so hue does not have to, and `title`
 * plus the accessible name state it in words for a sighted user and a screen
 * reader.
 *
 * Nothing animates. An earlier revision pulsed `running` to mirror the Schedule
 * page's badge, but this mark lives in persistent chrome on every page, so a
 * long job would pull peripheral attention for its whole duration -- a cost the
 * Schedule page does not pay, because the user goes there to watch. The halo
 * ring distinguishes `running` without motion.
 */
function RunStateIndicator({ state, collapsed, label }: { state: AppRunState | undefined; collapsed: boolean; label: string }) {
  if (!state) return null
  const shape = state === 'running'
    ? 'bg-accent ring-2 ring-accent/30'
    : state === 'error'
      ? 'bg-danger'
      : 'bg-transparent ring-1 ring-ok'
  // Collapsed: absolute in the icon's own corner, where there is no flex line to
  // join and no chord to cover. Expanded: IN FLOW with the other two right-edge
  // indicators. Keeping this one `absolute right-8` while the count pill moved
  // into the line would have left exactly the bug this change exists to fix, one
  // component over: an app row renders this mark AND an `appBadges`-driven pill,
  // and a 2-3 digit pill reaches left past 32px to sit under it. Flex items
  // cannot overlap, so joining the line closes it for this mark too rather than
  // relying on the pill staying narrow. `z-10` goes with the absolute arm: an
  // in-flow sibling needs no stacking order to avoid a box it cannot intersect.
  return collapsed
    ? <span className={`absolute bottom-1 right-1 w-2 h-2 rounded-full z-10 ${shape}`} role="status" aria-label={label} title={label} />
    : <span className={`shrink-0 w-2 h-2 rounded-full ${shape}`} role="status" aria-label={label} title={label} />
}

/**
 * Badge slot for a nav item. Resolves the count from the surface registry
 * (built-in surfaces) and falls back to the `mc:app:badge`-driven `appBadges`
 * map (dynamic apps + bridges from non-Redux sources like global approvals)
 * when the surface itself doesn't declare a badge source. This preserves the
 * prior two-pipeline behavior without leaving per-id branches in the
 * renderer.
 */
export function NavBadge({ navId, collapsed, appBadges, runState }: { navId: string; collapsed: boolean; appBadges: Record<string, number>; runState?: AppRunState }) {
  const surface = getBuiltinSurface(navId)
  // selectSurfaceBadgeCount caches per-navId so this stays referentially
  // stable across renders inside a `.map()`.
  const builtinCount = useAppSelector(selectSurfaceBadgeCount(navId))
  // Dynamic-app badges live outside Redux (set via a window event or a
  // direct setAppBadges sync). Consult them whenever the surface itself
  // doesn't own a badge source — including stub surfaces that only exist to
  // declare nav metadata. Surfaces with their own badge source (slotMode or
  // unreadSelector) skip the fallback to avoid double-counting.
  const surfaceHasBadgeSource = surface !== undefined && (surface.unreadSelector !== undefined || surface.slotMode !== undefined)
  const appName = navId.startsWith('app-') ? navId.slice(4) : navId
  const dynamicCount = surfaceHasBadgeSource ? 0 : (appBadges[appName] || 0)
  const builtinLabel = surface?.badgeLabel ?? i18nT('app.updates')
  const activityCount = useAppSelector(selectSurfaceActivityCount(navId))
  const activityLabel = surface?.activityLabel ?? 'in flight'
  return (
    <>
      <ActivityIndicator count={activityCount} collapsed={collapsed} label={activityLabel} />
      <RunStateIndicator state={runState} collapsed={collapsed} label={runStateLabel(runState)} />
      <BadgeIndicator count={builtinCount} collapsed={collapsed} label={builtinLabel} />
      <BadgeIndicator count={dynamicCount} collapsed={collapsed} label={builtinLabel} />
    </>
  )
}

/** Exported for `capture/nav-badge-chord.tsx`, which measures the row's
 *  right-edge geometry in a real browser — the one check that can see the
 *  badge-over-chord overlap this row's unit tests can only pin structurally
 *  (happy-dom computes no layout). Same seam `UpdateOverlay` is exported on. */
export function NavItem({ path, label, icon, active, collapsed, badge, onClickOverride, onClick, navId, pressed, touch, replace, caption }: {
  path: string; label: string; icon: React.ReactNode; active: boolean; collapsed: boolean; badge?: React.ReactNode; onClickOverride?: () => void; onClick?: () => void; navId?: string
  /** Set on rows that TOGGLE a surface rather than navigate (e.g. the docked
   *  terminal). `active` only paints the row; without aria-pressed a screen
   *  reader announces an identical button whether the panel is open or shut. */
  pressed?: boolean
  /** Phone rail geometry for a `collapsed` row: a 64x56 `rounded-xl` tile with a
   *  10px caption under the glyph, instead of the desktop rail's pointer-sized
   *  icon-only `rounded-md` row. Same icon, same selected paint. */
  touch?: boolean
  /** Navigate with `replace` instead of a push. The phone drawer that hosts the
   *  rail holds a duplicate history entry while open (see ChatPage's
   *  `pushDrawerEntry`); a row leaving the chat page overwrites it so Back
   *  lands on the chat, not on a second copy of it. */
  replace?: boolean
  /** Phone rail tile caption when the full label is too long for a 64px tile
   *  (e.g. "Agent Capabilities" -> "Capabilities"). The accessible name stays
   *  the full label. */
  caption?: string
}) {
  const navigate = useNavigate()
  // On mobile this row lives inside the nav DRAWER, whose slide runs on the
  // compositor (animateDrawer) — and a framer layout-projection node under a
  // compositor-driven ancestor transform mis-attributes the panel's travel to
  // itself, compounding a corrective offset (the ChatSidebar rows measured
  // >4,000px of it). The desktop rail is framer-free motion-wise, so it keeps
  // the row-reorder glide that `layout` buys there.
  const isMobileRow = useIsMobile()
  const iconEl = <span className={`app-icon-nav w-4 h-4 flex items-center justify-center shrink-0 transition-opacity ${active ? 'opacity-100 text-accent is-lit' : 'opacity-70'}`}>{icon}</span>
  const { tip, tipOn, rowRef, showTip, hideTip } = useNavTip<HTMLDivElement>(collapsed)
  // Derived from the shortcut registry by route, so a row with a bound panel
  // chord advertises it and a row without one is untouched. Null when the user
  // has turned shortcuts off. See useNavShortcutHint for why this resolves per
  // render rather than being written next to each row.
  const shortcut = useNavShortcutHint(path)
  const mayLeave = useMayLeaveForNavigation()
  const isCurrentUrl = useIsCurrentUrl()
  const activate = () => {
    // Navigating swaps the whole page, and the page leaving may hold a draft the
    // user typed — `beforeunload` cannot defend it, because a client-side route
    // change never unloads the document. Ask its guard first.
    //
    // Gated on this row actually going SOMEWHERE ELSE (see `useIsCurrentUrl` for
    // why that test is the whole URL and not `active`). A row with an
    // `onClickOverride` toggles a surface — the docked terminal, the phone
    // dialog — and unmounts nothing, so it keeps its exemption; an unqualified
    // ask would pop a discard-confirm over a click that was never going to
    // destroy anything.
    if (!onClickOverride && !isCurrentUrl(path) && !mayLeave()) return
    onClick?.(); (onClickOverride || (() => navigate(path, { replace })))()
  }
  return (
    <motion.div layout={isMobileRow ? undefined : 'position'}
      ref={rowRef}
      data-onboarding-nav={navId}
      // role+tabIndex+key handler make this a real keyboard-operable control
      // (Enter/Space activate, preventing Space page-scroll). aria-label names
      // it when collapsed (icon-only, no text).
      role="button"
      tabIndex={0}
      // Hover is a HOVER: the row paints (`hover:bg-bg-hover` / `hover:text-text`
      // below) and does not move. A scale on hover made every rail row grow a
      // couple of pixels under the cursor, nudging its neighbours and re-reading
      // as a layout change rather than as "you are pointing at this". Press still
      // scales — that one is feedback for an action the user actually took.
      whileTap={{ scale: 0.97 }}
      transition={{ duration: 0.15 }}
      // `touch`: the desktop rail dims an inactive icon to 70% (`opacity-70` on
      // the glyph span), which on the phone rail's flat 40x40 tiles measured
      // 3.4:1 against a light surface. The tile keeps the muted colour at full
      // opacity instead (>= 4.5:1); active tiles are unchanged.
      className={`nav-item group/nav relative flex items-center min-w-0 cursor-pointer text-sm font-medium whitespace-nowrap gap-2.5 transition-colors duration-200 ${touch ? 'w-16 h-14 px-0.5 flex-col justify-center gap-0.5 rounded-xl shrink-0 [&_.app-icon-nav]:w-5 [&_.app-icon-nav]:h-5 [&_.app-icon-nav>svg]:w-5 [&_.app-icon-nav>svg]:h-5 [&_.app-icon-nav]:opacity-100' : 'rounded-md py-2 pl-3 pr-3'} ${collapsed ? '' : 'overflow-hidden'} ${active ? 'nav-active text-text-strong bg-accent-subtle hover:brightness-110' : 'text-muted hover:text-text hover:bg-bg-hover/60'}`}
      onClick={activate}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate() } }}
      onMouseEnter={showTip}
      onMouseLeave={hideTip}
      // Keyboard-only users (no pointer) can't trigger the mouse-driven hover
      // label, so surface it on focus too. showTip/hideTip no-op unless collapsed,
      // making these inert in expanded mode where the text label is already shown.
      onFocus={showTip}
      onBlur={hideTip}
      aria-label={collapsed ? label : undefined}
      aria-pressed={pressed}
      // The chord declared to assistive tech, in the ARIA grammar rather than the
      // display glyphs — the same split MoveUndoBar's Undo button already ships.
      // This is what makes the hint reachable without a pointer: the visible
      // badge below is hover/focus-revealed decoration and is aria-hidden, so the
      // attribute is the non-visual route rather than a duplicate of one.
      aria-keyshortcuts={shortcut?.ariaKeyshortcuts}
    >
      {iconEl}
      {/* `aria-label` carries the FULL label: this span is `whitespace-nowrap overflow-hidden`, so
          a translation longer than the rail is silently cut off with no way to read it. Surfaced by
          the render gate under the en-XA pseudolocale at 2.2x once a new app entry narrowed the
          row (`layout/clipped-without-title`), which accepts `title` OR `aria-label`. Deliberately
          `aria-label`, NOT `title`: a page-wide `getByTitle('Settings'/'Board'/…)` in another app's
          Playwright specs (ops-mission-control) matches on `title`, and a sidebar nav item titled
          the same as one of those segment names would be clicked instead of the segment. `label`
          is already the resolved, translated string. */}
      {!collapsed && (
        <span
          aria-label={typeof label === 'string' ? label : undefined}
          className="flex-1 min-w-0 truncate"
        >
          {label}
        </span>
      )}
      {/* Phone rail tile: a one-word caption under the glyph. The desktop rail's
          collapsed rows name themselves with a hover tip, which a finger cannot
          summon, and a cold reader could not tell the Artifacts and
          Capabilities glyphs apart (UX lane). 10px is this project's floor. */}
      {collapsed && touch && (
        <span aria-hidden="true" className="max-w-full whitespace-normal text-center text-[10px] leading-[1.1] font-medium tracking-tight line-clamp-2 [overflow-wrap:anywhere]">{caption ?? label}</span>
      )}
      {/* Expanded rail: the chord rides the row's existing `group/nav` seam, so it
          appears on hover AND on keyboard focus-visible rather than on hover alone
          — the row is already `tabIndex={0}`, and a hover-only hint would be
          unreachable to a keyboard or touch user, which is the defect class #4120
          was fixed for and #3626 is still open on. `aria-hidden` because the
          accessible name must stay the label: the chord is declared exactly once,
          on `aria-keyshortcuts` above, rather than read out as glyphs. */}
      {!collapsed && shortcut && (
        <span
          aria-hidden="true"
          // Keycap DATA, not prose. `[data-i18n-opaque]` is the render-time i18n
          // gate's own marker for exactly this (render-scan.mjs OPAQUE_SELECTOR,
          // whose comment names "a keycap container span"). It costs nothing
          // visually and is not currently load-bearing -- the badge is opacity-0
          // until hover, so the scan does not see it -- but without it the class is
          // declared nowhere, and whoever makes this visible by default would get a
          // pseudolocale failure with no clue why.
          data-i18n-opaque=""
          data-testid={navId ? `nav-shortcut-${navId}` : undefined}
          className="shrink-0 text-[11px] leading-none text-muted opacity-0 transition-opacity duration-150 group-hover/nav:opacity-100 group-focus-visible/nav:opacity-100"
        >
          {shortcut.chord}
        </span>
      )}
      {/* LAST in the flex line, so the expanded unread/activity indicators sit to
          the RIGHT of the chord above rather than over it. Order matters only for
          the in-flow expanded indicators: every caller-supplied badge (the dev
          dot, the update dot) and the collapsed dot are absolutely positioned
          against the row, so they render where they always did regardless of
          where in the children they appear. */}
      {badge}
      {collapsed && tip && createPortal(
        <div
          className={`fixed flex items-center gap-2.5 pl-3 pr-3 rounded-md bg-card border border-border shadow-lg text-text text-sm font-medium z-[9999] pointer-events-none whitespace-nowrap transition-opacity duration-150 ${tipOn ? 'opacity-100' : 'opacity-0'}`}
          style={{ top: tip.top, left: tip.left, height: tip.height }}
        >
          <span className={`app-icon-nav w-4 h-4 flex items-center justify-center shrink-0 ${active ? 'text-accent is-lit' : ''}`}>{icon}</span>
          {label}
          {/* Collapsed rail: the row carries no text label, so this flyout IS its
              hover affordance — and it already opens on focus as well as hover
              (see onFocus/onBlur above), which is what carries the hint to a
              keyboard user on this width. */}
          {shortcut && (
            <span aria-hidden="true" data-i18n-opaque="" className="shrink-0 text-[11px] leading-none text-muted">
              {shortcut.chord}
            </span>
          )}
        </div>,
        document.body
      )}
    </motion.div>
  )
}

/**
 * Topbar Notifications bell. The Notifications surface is `hiddenFromNav`, so
 * this is its entry point. Click opens an Activity Feed popover
 * (portaled to <body> to escape the topbar's backdrop-filter containing
 * block); clicking an item slides out a detail panel. The full page is
 * preserved at /notifications via the popover's "Open inbox" link.
 */
function NotificationsBellButton() {
  // The Notifications surface is `hiddenFromNav`, so this bell — not a rail row —
  // is the control Alt+N operates. Resolved through the same route-keyed helper
  // the rail uses, so the chord has exactly one derivation in the dashboard.
  const shortcut = useNavShortcutHint('/notifications')
  const sheet = useNotificationSheet()
  const { items, open, containerRef, bellRef } = sheet
  // Badge counts attention-worthy rows only (RFC Phase 3): passive and
  // muted-channel (silenced) rows are excluded, mirroring the backend's
  // _unread_count semantics.
  const unacked = items.filter(n => !n.acked && n.priority !== 'passive' && !n.silenced)

  // RFC Phase 4: mirror the unread count onto the desktop dock/taskbar badge.
  useEffect(() => {
    setNativeBadgeCount(unacked.length)
  }, [unacked.length])

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={bellRef}
        className={`flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 relative ${open ? 'text-accent' : 'text-muted hover:text-text'}`}
        onClick={sheet.toggle}
        // Chord declared to assistive tech ONLY, deliberately not in the tooltip.
        // The render-time i18n gate scans `TEXT_ATTRS` (render-scan.mjs:293 --
        // title, aria-label, placeholder, alt, aria-placeholder) for Latin runs
        // under the en-XA pseudolocale, and its attribute branch (:499) has no
        // opaque escape: the `[data-i18n-opaque]` / `kbd` exemption applies to
        // ELEMENTS, so a keycap can be exempted in text but never inside an
        // attribute value. A chord appended here read as 220 untranslated-attribute
        // findings. `aria-keyshortcuts` is not in that list and is the standards
        // declaration anyway, so the non-visual route survives; the VISIBLE hint
        // stays a rail affordance, where it can be marked opaque.
        title={unacked.length > 0 ? i18nT('app.notification_count', { count: unacked.length }) : i18nT('app.notifications')}
        aria-label={i18nT('app.notifications')}
        aria-keyshortcuts={shortcut?.ariaKeyshortcuts}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <Bell size={15} />
        {unacked.length > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[16px] h-[16px] px-1 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center shadow-[0_0_2px_var(--accent-glow)]" aria-hidden="true">
            {unacked.length > 99 ? '99+' : unacked.length}
          </span>
        )}
      </button>
      <NotificationSheet sheet={sheet} />
    </div>
  )
}

export default function App() {
  const location = useLocation()
  const isEmbed = location.pathname.startsWith('/embed/')
  // Sticky popout-ness: computed from the pathname at DOCUMENT LOAD, not the
  // live route. A window that loaded as /popout/* stays in the popout branch
  // for its whole SPA lifetime, so no soft navigate() — present or future —
  // can ever mount the full dashboard chrome inside a popout window.
  // Deliberately a ref (not window.name-based): returnSelfToMain()'s deep-link
  // fallback does a full location.assign to the main view, which is a fresh
  // document load and correctly re-evaluates to false there.
  const isPopout = useRef(window.location.pathname.startsWith('/popout/')).current
  // The load-time popout URL: the wildcard route below re-pins any stray
  // in-window navigation back to this frame instead of escaping to '/'.
  const initialPopoutPath = useRef(window.location.pathname + window.location.search).current
  const dispatch = useAppDispatch()
  // Register the operator's link rules (dashboard.link_patterns) into the
  // autolink registry from the shell, so every surface linkifies — not only
  // after a chat page has rendered. Owns the registry; the chat page reuses
  // the same ['dashboardConfig'] query for its source hosts.
  useConfigAutolinkRules()
  // The slice also carries the slot list and the subagent maps, so selecting all of
  // it would re-render the root on dashboard traffic neither of these fields reads.
  const connected = useAppSelector(s => s.dashboard.connected)
  // Gateway (web) update flag OR desktop updater availability (mirrored from
  // Electron update-state by useUpdateSubscription) -- both light the same
  // Settings nav dot below.
  //
  // `=== true` because the gateway's verdict is NULLABLE: null means a check that
  // never ran or failed, and a truthiness test on it would be fine while a
  // `!== false` test would claim an update on no evidence. Availability alone
  // never licenses an apply action -- see `canApplyUpdate`.
  const updateAvailable = useAppSelector(
    s => s.dashboard.status?.update_available === true || s.dashboard.desktopUpdateAvailable
  )
  // Track whether the session-expired auth banner is currently injected by
  // api/client.ts. When auth is the real reason the gateway is unreachable,
  // the red top-banner tells the user what to do (paste a fresh
  // `kirocrew token` URL) and the capsule reddens quietly underneath it --
  // for a sighted user the tint does not argue with the banner. But the
  // banner is a plain <div> with no role="alert"/aria-live, so it is never
  // announced; the capsule's accessible name and role="status" region are the
  // only screen-reader carriers of the offline cause. So when authRequired is
  // true the capsule announces the auth-specific wording (session expired, see
  // banner) instead of the generic "Gateway offline", which would point a
  // screen-reader user at reconnection when pasting a token is the fix.
  // `isAuthBannerShown()` seeds initial state in case the banner was injected
  // before App mounted (e.g. a 403 fired during the very first /api/status
  // before React hydrated).
  const [authRequired, setAuthRequired] = useState<boolean>(isAuthBannerShown)
  useEffect(() => {
    const onRequired = () => setAuthRequired(true)
    const onCleared = () => setAuthRequired(false)
    window.addEventListener('mc-auth-required', onRequired)
    window.addEventListener('mc-auth-cleared', onCleared)
    return () => {
      window.removeEventListener('mc-auth-required', onRequired)
      window.removeEventListener('mc-auth-cleared', onCleared)
    }
  }, [])
  // Sum across every registered built-in surface — Chat (slot-based),
  // Notifications (notifications slice), Secretary
  // (attention slice), etc. App badges (dynamic, via `mc:app:badge` and the
  // global-approvals query below) are added below since they live outside
  // the Redux store and outside the registry.
  const builtinAttention = useAppSelector(selectAllSurfacesAttention)
  const approvalCount = useGlobalApprovalCount()
  const terminalEnabled = useTerminalRestoreProbe()
  // True while the terminal panel lives in its own popped-out window: the
  // docked panel is suppressed here and the sidebar toggle focuses that
  // window instead of opening an (empty-handed) panel.
  const terminalPoppedOut = useTerminalPoppedOut()
  // Only the `open` flag, not the whole store — the panel's height changes on
  // every mousemove during a grip-drag, and a primitive snapshot lets
  // useSyncExternalStore's Object.is check skip those re-renders of App.
  const bottomTerminalOpen = useBottomTerminalOpen()
  const workspacePanelOpen = useAppSelector(s => s.chat.activityOpen)
  const [workspaceSearchOpen, setWorkspaceSearchOpen] = useState(false)
  const reduceWorkspaceMotion = useReducedMotion()
  const [workspaceFullscreen, setWorkspaceFullscreen] = useState(false)
  const exitWorkspaceFullscreen = useCallback(() => setWorkspaceFullscreen(false), [])
  const toggleWorkspaceFullscreen = useCallback(() => setWorkspaceFullscreen(value => !value), [])
  const mobileConnect = useMobileConnect(location.key)
  const { mobileConnectOpen, setMobileConnectOpen, hasRenderableMobileConnect } = mobileConnect
  // Selected session's project directory: a terminal opened from the nav row
  // starts there (server default when no session is selected or it has none).
  const activeSlotProject = useAppSelector(selectActiveSlotProject)
  const terminalPosition = useTerminalPosition()
  const navigate = useNavigate()
  const mayLeaveForErrorHandoff = useMayLeaveForNavigation()

  // Main-dashboard role for the artifact popout nav-intent handshake: perform
  // navigation intents forwarded from popout windows (activity-timeline
  // session links, "Ask agent to address", …). Popout and embed windows never
  // register — only handler-registered windows answer nav-requests, which is
  // what keeps a second popout from claiming another popout's navigation.
  useEffect(() => {
    if (isPopout || isEmbed) return
    return setArtifactNavIntentHandler((intent) =>
      applyNavIntentInMain(intent, {
        navigate,
        switchSlot: (slotKey) => { dispatch(switchSlot({ key: slotKey, announceOnMissing: true })) },
      }),
    )
  }, [isPopout, isEmbed, navigate, dispatch])

  // Publish the router navigator and the current page's leave answer for the
  // error → agent hand-off. AskAgentButton is deliberately hook-free (its
  // callers include ErrorBoundary fallbacks, where router context may be what
  // threw), so it navigates through this seam and falls back to a full page
  // load when nothing is installed. The guard runs before prompt staging, so a
  // veto leaves both the page and the hand-off queue untouched.
  //
  // Popout and embed windows never register, for the same reason the nav-intent
  // handler above skips them: routing THAT window to /chat would replace the
  // surface the user deliberately popped out (an artifact editor renders error
  // banners of its own). They fall through to the hard-nav path instead.
  useEffect(() => {
    if (isPopout || isEmbed) return
    installSoftNavigate(navigate, mayLeaveForErrorHandoff)
    return () => installSoftNavigate(null)
  }, [isPopout, isEmbed, navigate, mayLeaveForErrorHandoff])

  const {
    colorTheme,
    theme: resolvedMode,
    brandName,
    brandLogo,
    brandFavicon,
    onboarded,
    importOnboarded,
    privacyAcked,
    themeBootReady,
    markOnboarded,
    markImportOnboarded,
    markPrivacyAcked,
  } = useTheme()
  const firstRun = useFirstRunChapters({ onboarded, importOnboarded, privacyAcked, themeBootReady, markOnboarded })
  const { showAgentImport, showPrivacy, showOnboarding } = firstRun
  // Capture Electron update lifecycle events app-wide so UpdateModal fires on
  // any page, not just after the user has opened Settings > About.
  useUpdateSubscription()
  const { branding, botName, avatar } = useShellBranding({ colorTheme, brandName, brandLogo, brandFavicon })
  useRumPageView()
  useNotificationSound()
  const [navCollapsed, setNavCollapsed] = useState(() => localStorage.getItem('mc-nav') === '1')
  const navCollapsedRef = useRef(navCollapsed)
  navCollapsedRef.current = navCollapsed
  // Preview expand mode from the Web Preview tab collapses the left nav
  // as a STARTING layout, not a lock — the brand toggle keeps its standard
  // behavior while expand mode is on, so the rail can be brought back without
  // leaving the preview. This ref holds the pre-expand state to restore on exit,
  // and is cleared the moment the user toggles the rail themselves so their
  // choice is not undone. `navCollapsed` is driven directly rather than ORed
  // with a transient flag, because an OR makes the toggle look broken.
  //
  // The ref is read and cleared HERE, in the handler, and only plain values are
  // passed to the setter: a state updater must be pure, and React invokes one
  // twice under StrictMode, which would make the second pass read an
  // already-cleared ref and lose the restore value.
  const navAutoCollapsed = useRef<boolean | null>(null)
  useEffect(() => {
    const onPreviewExpand = (e: Event) => {
      const expanded = !!(e as CustomEvent<{ expanded?: boolean }>).detail?.expanded
      if (expanded) {
        if (navAutoCollapsed.current === null) navAutoCollapsed.current = navCollapsedRef.current
        setNavCollapsed(true)
        return
      }
      const prior = navAutoCollapsed.current
      navAutoCollapsed.current = null
      if (prior !== null) setNavCollapsed(prior)
    }
    window.addEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
    return () => window.removeEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
  }, [])
  const isMobile = useIsMobile()
  const [sidePanelDock] = useSidePanelDock()
  // Side panel docked to the bottom (desktop only) swaps the shell from a
  // 3-column grid with a full-height right rail to a 2-column grid with an
  // extra bottom row that the panel fills.
  const bottomDock = sidePanelDock === 'bottom' && !isMobile
  // Multi-instance: which instance fills the pane below the tab bar. null = Local
  // (the native dashboard); a non-null id means a remote instance's embedded
  // dashboard is shown instead, so the Local pane is hidden (not unmounted).
  const activeInstanceId = useAppSelector(s => s.instances.activeId)
  const { macFullscreen, macInset, topReservePx } = useMacFullscreen()
  const {
    focusMode, toggleFocusMode, focusActive, topPeek, railPeek, topPeekTrigger, topPeekSurface,
    railPeekTrigger, railPeekSurface, topChromeShown, localHeaderDragGaps,
  } = useFocusChrome({ isMobile, navCollapsed, activeInstanceId, topReservePx })
  // Whether the shell's one-shot entrance animation has already played.
  //
  // The local pane is HIDDEN, not unmounted, while a remote instance tab is
  // active (`display:none` below) so its state and websocket survive the
  // switch. But a CSS *animation* restarts when an element goes from
  // `display:none` back to displayed — unlike a transition, and unlike
  // framer-motion's JS-driven animations. Left unguarded, `animate-rise`
  // therefore replays its 350ms opacity-0 -> 1 + 8px lift over the WHOLE
  // dashboard every time the user returns to the Local tab, which reads as the
  // entire UI (side panel included) flashing in again.
  const [shellEntered, setShellEntered] = useState(false)
  // Backstop for the latch below. `animationend` does NOT fire when a running
  // animation is INTERRUPTED — the browser fires `animationcancel`, which React
  // 18 has no synthetic handler for. Hiding the pane inside the entrance's
  // 350ms window would therefore leave the class applied and replay it once on
  // the next return. A timer comfortably past the duration closes that without
  // a ref + native listener, and cannot cut the entrance short.
  useEffect(() => {
    const t = window.setTimeout(() => setShellEntered(true), 600)
    return () => window.clearTimeout(t)
  }, [])
  /**
   * Mobile nav drawer, as ONE phase value (mirrors ChatPage's sessions drawer):
   * `closing` keeps the panel mounted while it slides out. The slide itself
   * runs on the COMPOSITOR via animateDrawer — the shell shares its main
   * thread with every streaming session, so a framer main-thread tween here
   * dropped frames exactly when the app was busiest. The width used by the
   * offset is the drawer's own 220px + its 8px inset, not the viewport.
   */
  const [mobileNavPhase, setMobileNavPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const mobileNavMounted = mobileNavPhase !== 'closed'
  const mobileNavPhaseRef = useRef(mobileNavPhase)
  mobileNavPhaseRef.current = mobileNavPhase
  /** Panel offset in px: -mobileNavTravel() offscreen, 0 at rest. */
  const mobileNavX = useMotionValue(0)
  const mobileNavPanelRef = useRef<HTMLElement | null>(null)
  const mobileNavScrimRef = useRef<HTMLDivElement | null>(null)
  /**
   * The dashboard shell — the common ancestor of `<main>`, the nav drawer's
   * panel and its scrim. Bound rather than `<main>` because the panel and scrim
   * are `fixed` siblings OUTSIDE it, so a gesture rooted at `<main>` never sees
   * the touches that should CLOSE the drawer: the finger lands on the scrim or
   * the panel, and the listener is on an element neither is inside.
   *
   * Widening the root does not widen what arms: dialogs render through a portal
   * to `document.body`, so they are outside this element entirely, and a page
   * with its own drawer claims its sides with `data-owns-swipe`.
   */
  const shellRef = useRef<HTMLDivElement | null>(null)
  // Safe against the projection bug only because the drawer's nav rows drop
  // their `layout` prop on mobile — see registerDrawerTargets' precondition.
  useEffect(() => registerDrawerTargets(mobileNavX, {
    panel: () => mobileNavPanelRef.current,
    scrim: () => mobileNavScrimRef.current,
    travel: mobileNavTravel,
  }), [mobileNavX])
  const openMobileNav = useCallback(() => {
    if (mobileNavPhaseRef.current === 'open') return
    if (mobileNavPhaseRef.current === 'closed') mobileNavX.set(-mobileNavTravel())
    mobileNavPhaseRef.current = 'open'
    setMobileNavPhase('open')
    animateDrawer(mobileNavX, 0)
  }, [mobileNavX])
  /** Scrim opacity derived from the panel's own offset: 1 at rest, 0 as it
   *  clears the edge, so a half-open drag is half-dimmed and a cancelled drag
   *  un-dims with the finger. Divided by the drawer's OWN travel, matching the
   *  sessions drawer. A literal `opacity: 0` was correct only while the tap was
   *  the sole mover — the compositor settle animates the scrim in lockstep and
   *  never reads this, but a DRAG writes the MotionValue and nothing else would
   *  paint the dim. */
  const mobileNavScrim = useTransform(mobileNavX, x =>
    Math.max(0, Math.min(1, 1 + x / Math.max(1, mobileNavTravel()))))
  const closeMobileNavDrawer = useCallback(() => {
    if (mobileNavPhaseRef.current !== 'open') return
    mobileNavPhaseRef.current = 'closing'
    setMobileNavPhase('closing')
    takeOverDrawer(mobileNavX)
    animateDrawer(mobileNavX, -mobileNavTravel(), () => {
      mobileNavPhaseRef.current = 'closed'
      setMobileNavPhase('closed')
    })
  }, [mobileNavX])
  /**
   * Mount the drawer for a gesture that has begun opening it, WITHOUT the slide
   * `openMobileNav` would start: the finger owns the offset from here until it
   * lifts, and a settle running against it would pull the panel out from under
   * the drag. Same split as the chat page's own drawer.
   */
  const beginMobileNavDrag = useCallback(() => {
    mobileNavPhaseRef.current = 'open'
    setMobileNavPhase('open')
  }, [])
  /**
   * The nav drawer is reachable by swipe on EVERY page, not just chat: the
   * gesture is bound on the shell, so it covers both the page content that opens
   * it and the scrim/panel that close it. A page owning the same side declares
   * `data-owns-swipe` on the element it binds, which suppresses this instance
   * there (the chat page keeps its sessions drawer on a rightward drag). The
   * hamburger stays the discoverable path.
   */
  useDrawerSwipe(shellRef, {
    // Off on the phone chat page: that page's sessions drawer carries the main
    // navigation as a rail (see `mobileNavRail`), so the nav drawer has no
    // trigger there and must not be reachable by a swipe on the header either
    // — two drawers for one gesture is the state this bar exists to remove.
    // The chat container already claims its own swipe via `data-owns-swipe`;
    // this gate covers the header above it.
    enabled: isMobile && !isChatRoute(location.pathname),
    travel: mobileNavTravel,
    open: mobileNavPhase === 'open',
    x: mobileNavX,
    onGestureOpen: beginMobileNavDrag,
    onSettle: open => {
      if (open) return
      mobileNavPhaseRef.current = 'closed'
      setMobileNavPhase('closed')
    },
  })

  // Dynamic app nav items — all apps (builtin + installed) with UI pages
  const [appNavItems, setAppNavItems] = useState<Array<{ path: string; id: string; label: string; group: string; icon: React.ReactElement }>>([])
  const {
    advertisedNavItems, sortedAppGroup, activeAppDragId, appDndSensors,
    handleAppDragStart, handleAppDragEnd, handleAppDragCancel,
  } = useAppRailOrder(appNavItems)
  // Collapse a long Apps list behind a "N more" toggle so the nav can't grow
  // unbounded. Above APPS_NAV_LIMIT visible entries the overflow is hidden until
  // the user expands (persisted).
  const APPS_NAV_LIMIT = 6
  const [appsExpanded, setAppsExpanded] = useState(() => localStorage.getItem('mc-apps-expanded') === '1')
  const toggleAppsExpanded = useCallback(() => setAppsExpanded(v => { const next = !v; safeSetItem('mc-apps-expanded', next ? '1' : '0'); return next }), [])
  const appNavRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Monotonic stamp for app-nav fetches. Cancelling a pending RETRY is not enough:
  // a fetch already in flight cannot be cancelled, so a slow mount response landing
  // after an enable/disable refresh would publish stale slot ownership and bind the
  // quick-search gesture to the wrong surface. Only the newest generation may write.
  const appNavGenRef = useRef(0)
  const [slotOwners, setSlotOwners] = useState<SlotOwners>({})
  const queryClient = useQueryClient()
  const refreshAppNav = useCallback((attempt = 0, joinPending = false) => {
    // Cancel any pending retry up-front so external triggers (the reconnect
    // effect, the mc:apps-changed handler) or a just-fired retry can never run
    // overlapping fetch chains — exactly one chain is ever active.
    if (appNavRetryRef.current) { clearTimeout(appNavRetryRef.current); appNavRetryRef.current = null }
    const gen = ++appNavGenRef.current
    // The mount read goes through the shared ['apps'] query so it joins the GET
    // an ['apps'] observer mounted in the same commit (the panel-tab registry,
    // the composer's session controls) has already started, instead of sending
    // a second identical one. staleTime 0 still fetches when nothing is in
    // flight; retry stays false because the backoff below owns retries.
    //
    // A refresh after a change or reconnect first cancels that shared boot
    // query. Its request may still finish at the transport, but React Query no
    // longer accepts its result, so it cannot overwrite the newer direct read.
    // The direct read then publishes one response to both the nav and cache.
    //
    // Both the cancel and the re-mark are `exact`: query filters match by key
    // PREFIX, so a bare ['apps'] filter would also cancel MigrationPage's
    // in-flight ['apps', 'migration', <name>] first load and revert it to
    // pending with no data, and would mark other ['apps', ...] queries stale
    // that this refresh does not refetch. Only the shared list query is ours.
    const read: Promise<AppListEntry[]> = joinPending
      ? queryClient.fetchQuery({ queryKey: ['apps'], queryFn: () => api.listApps(), staleTime: 0, retry: false })
      : queryClient.cancelQueries({ queryKey: ['apps'], exact: true }).then(() => {
        // Cancelling reverts the query to its state from before the cancelled
        // fetch started, which can drop a stale mark set since then (the
        // mc:apps-changed handler's). Re-mark it so a failed read below still
        // leaves the cache stale rather than fresh.
        queryClient.invalidateQueries({ queryKey: ['apps'], exact: true, refetchType: 'none' })
        return api.listApps()
      })
    read
      .then((apps: AppListEntry[]) => {
        if (gen !== appNavGenRef.current) return
        const items = apps
          .flatMap(a => {
            // Eligibility, route, id and label come from the shared derivation in
            // `appNav.ts` — the palette's Apps provider resolves destinations
            // through the same functions, so the rail and the palette cannot send
            // a user to different places for the same app. Only the icon is built
            // here, because the rail tints orphaned apps and sizes its glyph for a
            // 16px row.
            const target = appNavTarget(a)
            if (!target) return []
            const iconName = target.iconName
            // Prefer the app's custom top-level iconUrl (an absolute
            // /app-assets/... path — the same source the App Store card renders
            // via AppIcon) so builtin colorful SVG icons also show in the left
            // nav. Fall back to a page-relative ui/ icon (installed apps), then
            // the builtin lucide glyph, then the generic package icon.
            const customIconUrl = target.iconUrl
            const builtinIcon = target.builtin ? getBuiltinIcon(iconName) : undefined
            const baseIcon = customIconUrl || target.iconUrlDark
              ? <AppIcon iconUrl={customIconUrl} iconUrlDark={target.iconUrlDark} icon={iconName} size={16} />
              : target.pageIconUrl
                ? <img src={'/apps/' + a.name + '/ui/' + target.pageIconUrl} alt="" className="w-4 h-4 rounded-sm object-contain" />
                : builtinIcon
                  ? builtinIcon
                  : <Package size={16} />
            // Orphaned apps get a warn-colored icon to signal migration needed
            const icon = target.orphaned
              ? <span className="text-warn">{baseIcon}</span>
              : baseIcon
            return [{
              path: target.route,
              id: target.id,
              label: target.label,
              group: 'Apps',
              icon,
              // The app's own name, carried rather than re-derived from `id`.
              // `id` is `app-<name>` only for AppHost-routed apps and the BARE
              // name for a native builtin, so parsing it back cannot tell a
              // builtin app's row from a host surface's row -- and a helper that
              // guesses would have to choose between missing every builtin app
              // and letting an app named `schedule` claim host chrome. Passing
              // the name the target already resolved avoids that choice.
              appName: target.name,
            }]
          })
        setAppNavItems(items)
        dispatch(setEnabledAppIds(items.map(i => i.id)))
        // Publish this response under the shared apps key so readers that want the
        // list -- an overlay opened later, the palette's apps provider -- are served
        // from cache instead of issuing a second identical request.
        queryClient.setQueryData(['apps'], apps)
        // Which app (if any) currently owns a host overlay slot. Derived from the
        // SAME response as the nav rail — an app-contributed overlay costs no
        // extra request, and the shell never names a specific app.
        setSlotOwners(resolveSlotOverlays(apps))
      })
      .catch(() => {
        if (gen !== appNavGenRef.current) return
        // A transient failure (e.g. the gateway mid-restart right after a
        // `kirocrew update`, or the cold apps-dir scan) used to be swallowed
        // here, leaving the Apps rail empty until a manual reload or an app
        // enable/disable. Retry with bounded exponential backoff so it
        // self-heals. The reconnect effect below covers the WS-drop case.
        if (attempt >= APP_NAV_MAX_RETRIES) return
        appNavRetryRef.current = setTimeout(() => refreshAppNav(attempt + 1), APP_NAV_RETRY_BASE_MS * 2 ** attempt)
      })
  }, [dispatch, queryClient])
  useEffect(() => {
    refreshAppNav(0, true)
    return () => { if (appNavRetryRef.current) clearTimeout(appNavRetryRef.current) }
  }, [refreshAppNav])
  useEffect(() => {
    const handler = () => {
      // Mark the shared ['apps'] cache stale BEFORE the refetch: refreshAppNav
      // publishes fresh data only on fetch SUCCESS (setQueryData), so when its
      // bounded retry chain exhausts, an un-invalidated cache would keep
      // serving stale rows marked fresh. Invalidating up front makes that
      // failure mode stale-but-marked-stale, which is what lets dispatch
      // sites skip a local ['apps'] invalidation of their own.
      // refetchType 'none' keeps refreshAppNav the single fetcher: without it,
      // an active ['apps'] observer (the /apps page) would refetch immediately
      // on invalidation, duplicating the request refreshAppNav is about to make.
      queryClient.invalidateQueries({ queryKey: ['apps'], refetchType: 'none' })
      refreshAppNav()
      // The Explore shelf's install state lives in the server-computed
      // `installed` flag on the `['registry']` rows, which are cached with a
      // multi-minute staleTime. Every install/uninstall/enable surface
      // announces itself through this event, so drop that cache here too —
      // otherwise a just-installed registry app keeps rendering a "Get"
      // button until the cache expires.
      queryClient.invalidateQueries({ queryKey: ['registry'] })
    }
    window.addEventListener('mc:apps-changed', handler)
    return () => window.removeEventListener('mc:apps-changed', handler)
  }, [refreshAppNav, queryClient])
  // Refetch the Apps nav when the gateway connection is *re*-established after a
  // drop — e.g. a `kirocrew update` restart disconnects then reconnects the
  // WebSocket. Only fires on a connected→disconnected→connected cycle, NOT the
  // initial connect (the mount fetch already covers that), so a normal load
  // never double-fetches.
  const appNavConnStateRef = useRef<'init' | 'up' | 'down'>('init')
  useEffect(() => {
    if (connected) {
      if (appNavConnStateRef.current === 'down') refreshAppNav()
      appNavConnStateRef.current = 'up'
    } else if (appNavConnStateRef.current === 'up') {
      appNavConnStateRef.current = 'down'
    }
  }, [connected, refreshAppNav])

  const { appBadges, discoverBadges, railAppBadges, railAppRunStates } = useRailBadges(approvalCount)

  const { shortcutsOpen, setShortcutsOpen, toggleShortcutsModal, commandPalette, agentSwitchNotice } = useShellKeyboard({
    toggleFocusMode, toggleNav: () => toggleNav(), terminalEnabled, isPopout, isEmbed, terminalPoppedOut, activeSlotProject, workspaceFullscreen, exitWorkspaceFullscreen,
  })

  const { kiroUsageOpen, setKiroUsageOpen, kiroUsageState, kiroCreditSurface, kiroAccountEntry, refetchKirocrewCfg } = useKiroUsageReadout()
  const metrics = useMetricsReadout(isMobile, updateAvailable)
  const { capsuleCollapsed, setCapsuleCollapsed, capsuleLayoutPulse, pulseCapsuleLayout, sysMetrics, metricsProbeRef, metricsGroupRef } = metrics

  const { devMode, devPageSeen } = useDeveloperMode(location.pathname)
  // Native app-menu navigation (Settings…, About) and the Crew Companion's "Open
  // session" CTA: the Electron main process sends an in-app path; route to it.
  // The bridge hands over plain absolute app paths only (see
  // `subscribeNativeNavigate`).
  //
  // A session deep link takes the same route a popout's nav intent does — select
  // the session, then navigate — rather than a bare navigate. `?sid=` is read by
  // ChatPage only while it MOUNTS, so from an already-open /chat a bare navigate
  // would surface the dashboard with the previous session still on screen: the
  // window comes forward and the notification appears to have opened nothing.
  useEffect(() => {
    return subscribeNativeNavigate(path => {
      const slotKey = chatDeepLinkSlot(path)
      if (slotKey) {
        applyNavIntentInMain(
          // `path` is deliberately dropped in favour of the bare route: a
          // NavIntent carries no query string, and ChatPage writes `?sid=` back
          // into the URL itself once the session is active.
          { path: '/chat', slotKey },
          { navigate, switchSlot: (key) => { dispatch(switchSlot({ key, announceOnMissing: true })) } },
        )
        return
      }
      navigate(path)
    })
  }, [navigate, dispatch])

  useEffect(() => {
    dispatch(fetchSlots()).then(action => {
      // Run localStorage GC after we know which sessions are alive
      if (fetchSlots.fulfilled.match(action)) {
        const liveIds = new Set((action.payload as Array<{ key: string }>).map(s => s.key))
        gcOrphanedStorage(liveIds)
      }
    })
    // The boot notifications fetch is owned by the WebSocket first-connect
    // handler (its snapshot is taken after socket registration, so nothing
    // can fall between snapshot and push -- see notificationsSlice). This
    // only arms the fallback for a socket that never connects.
    // Return the thunk promise: a late first connect serializes its own fetch
    // behind this one via markBootNotificationsFetched() (see notificationsSlice).
    const disarmNotificationsFallback = armBootNotificationsFallback(() => dispatch(fetchNotifications()))
    // Fetch status immediately to sync YOLO state (WS status push is periodic)
    api.status().then(s => { dispatch(sseStatus(s)); recordSessionStart(s) }).catch(() => {})
    return disarmNotificationsFallback
  }, [dispatch])
  const { subscribeLogs, subscribeSubagents, forceReconnect } = useWebSocket()
  useDashboardHealthProbe(forceReconnect)

  const updateFlow = useUpdateFlow(refetchKirocrewCfg)
  const { updating, setUpdating, showUpdateModal, setShowUpdateModal, showChangelog, changelogDecided, updateError, setUpdateError } = updateFlow

  const startupVideo = useStartupVideo({
    showChangelog, changelogDecided, updateAvailable, showOnboarding, showAgentImport, showPrivacy,
    importOnboarded, privacyAcked, onboarded, themeBootReady,
  })

  // Browser tab title badge — sums every built-in surface's badge (chat,
  // orchestrated, notifications, secretary, ...) plus the orthogonal
  // `mc:app:badge`-driven dynamic app counts. Secretary's badge flows through
  // the surface registry.
  const totalAttention = builtinAttention + Object.values(appBadges).reduce((a, b) => a + b, 0)
  useEffect(() => {
    document.title = totalAttention > 0 ? `(${totalAttention}) ${botName}` : botName
  }, [totalAttention, botName])

  // Browser push notification on new notification — see src/hooks/useNativeNotification.ts
  useNativeNotification(botName, avatar)

  // Nav-rail "Report issue" → the shared diagnostics flow. Held at shell level
  // (not in the rail) so the modal is not unmounted when the rail collapses.
  const [reportProblemOpen, setReportProblemOpen] = useState(false)

  const requestFeature = useRequestFeature(colorTheme)

  const toggleNav = () => {
    if (isMobile) { if (mobileNavPhaseRef.current === 'open') closeMobileNavDrawer(); else openMobileNav() }
    else {
      // The user has taken ownership of the rail: leaving preview expand mode
      // must not overwrite this with the pre-expand state.
      navAutoCollapsed.current = null
      setNavCollapsed(prev => { const next = !prev; safeSetItem('mc-nav', next ? '1' : '0'); return next })
    }
  }
  // Close mobile nav on route change
  useEffect(() => { if (isMobile) closeMobileNavDrawer() }, [location.pathname]) // eslint-disable-line react-hooks/exhaustive-deps
  // Escape closes the open drawer — the keyboard's dismissal path. The scrim's
  // click-to-dismiss is pointer-only (it is aria-hidden and unfocusable, so a
  // full-screen tab stop never appears in the tab order).
  useEffect(() => {
    if (!isMobile || mobileNavPhase !== 'open') return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeMobileNavDrawer() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [isMobile, mobileNavPhase, closeMobileNavDrawer])
  // Reset mobile nav state when leaving mobile viewport
  // Leaving mobile: drop the panel with no slide (no drawer exists on desktop).
  useEffect(() => { if (!isMobile) { setMobileNavPhase('closed'); takeOverDrawer(mobileNavX) } }, [isMobile, mobileNavX])
  // Focus mode honours the collapse preference too: the overlay rail is as wide
  // as the docked rail would be, and the collapse control toggles it the same way.
  const effectiveCollapsed = navCollapsed && !isMobile
  // Publish the rail track so consumers outside the shell can size against the
  // space actually left for content — ChatPage's activity panel decides
  // beside-vs-fill from it. Kept in sync with the gridTemplateColumns value
  // below; railWidthFor is the single source for both.
  useEffect(() => {
    setRailWidth(focusActive ? 0 : railWidthFor({ isMobile, collapsed: effectiveCollapsed }))
  }, [isMobile, effectiveCollapsed, focusActive])
  // The header's three grid tracks (see `.topbar` in index.css) size themselves:
  // the search width is a function of the window, the two side groups split the
  // remainder, and each group re-lays-out its own contents with a container
  // query. Nothing measures a cluster any more — the drag-region reporter
  // addresses the header itself and the layout tests match the group classes, so
  // the two cluster refs this used to keep are gone with the measurement.
  const closeMobileNav = isMobile ? closeMobileNavDrawer : undefined
  const { activePath, libraryNavActive, discoverNavActive, isChat, needsFixedHeight, navRowActive } =
    useRouteActiveModel(location.pathname, location.search, advertisedNavItems)
  // Phone chat page: the header is ONE bar for both the shell and the
  // conversation. The chat page fills `#mobile-topbar-slot` (sessions toggle,
  // session title + menu) and `#mobile-topbar-trail-slot` (its overflow menu)
  // through portals, and the shell keeps only the crew switcher and the bell.
  // The nav drawer's logo trigger, the readout capsule and the search square
  // are not rendered here: search and the main destinations live in the rail
  // the chat page's sessions drawer shows (see `mobileNavRail` below).
  const mobileSingle = isMobile && isChat
  const panelFullscreen = workspaceFullscreen && isChat && !isMobile && workspacePanelOpen && !workspaceSearchOpen
  const workspaceFullscreenControls = useMemo(
    () => ({ fullscreen: panelFullscreen, exit: exitWorkspaceFullscreen, toggle: toggleWorkspaceFullscreen }),
    [panelFullscreen, exitWorkspaceFullscreen, toggleWorkspaceFullscreen],
  )
  useEffect(() => {
    if (!isChat || isMobile || !workspacePanelOpen || workspaceSearchOpen) setWorkspaceFullscreen(false)
  }, [isChat, isMobile, workspacePanelOpen, workspaceSearchOpen])

  // Render one standard nav row (used by the top-fixed mains, the Apps list,
  // and the bottom-fixed section). Active-state, mobile close, chat pin
  // toggle, and badge wiring are identical across sections.
  // `surfaceLabel` resolves `labelKey` against the active language at render
  // time; a surface with no key (app-contributed) falls back to its literal.

  const renderNavRow = (
    n: { path: string; id: string; label: string; labelKey?: string; icon: React.ReactNode; appName?: string },
  ) => (
    <NavItem
      navId={n.id}
      path={n.path}
      label={surfaceLabel(n)}
      icon={n.icon}
      active={navRowActive(n.path)}
      collapsed={effectiveCollapsed}
      onClick={closeMobileNav}
      onClickOverride={isChat && (activePath === n.path || activePath.startsWith(n.path + '/')) ? () => window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) : undefined}
      badge={<NavBadge navId={n.id} collapsed={effectiveCollapsed} appBadges={isAppNavId(n.id) ? railAppBadges : appBadges} runState={n.appName ? railAppRunStates[n.appName] : undefined} />}
    />
  )

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
   * `null` off the phone chat page, so every other consumer renders no rail.
   */
  const mobileNavRail = mobileSingle
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
        <nav
          data-testid="mobile-nav-rail"
          role="navigation"
          aria-label={i18nT('app.main_navigation')}
          className="w-[72px] shrink-0 h-full flex flex-col items-center gap-1 pt-1.5 pb-2.5 border-r border-border bg-bg-accent overflow-hidden"
        >
          <button
            type="button"
            data-testid="mobile-nav-rail-home"
            onClick={() => { onActivate(); if (!(activePath === '/chat' || activePath === '/')) navigate('/chat', { replace: true }) }}
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
          {/* Apps list: scrolls in its OWN frame when many apps are installed --
              the brand mark, the Main rows and Discover above it, and
              Capabilities / Settings / Search below it stay pinned, exactly as
              the desktop rail does. The scroller has no gap of its own so a
              short list sits flush under Discover. */}
          <div
            data-testid="mobile-nav-rail-apps"
            className="flex-1 min-h-0 w-full flex flex-col items-center gap-1 overflow-y-auto overflow-x-hidden overscroll-y-none scrollbar-none"
            style={{ scrollbarWidth: 'none' }}
          >
            {sortedAppGroup.map(railRow)}
          </div>
          {devMode && (
            <NavItem
              navId="developer"
              path="/developer"
              label={i18nT('app.developer')}
              icon={<Code size={16} />}
              active={activePath === '/developer'}
              collapsed
              touch
              replace
              onClickOverride={activePath === '/developer' ? onActivate : undefined}
            />
          )}
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
            onClick={() => { onActivate(); commandPalette.openPalette() }}
            className="mt-1 w-16 h-14 px-0.5 rounded-xl border border-border bg-card text-text flex flex-col items-center justify-center gap-0.5 cursor-pointer shrink-0"
            aria-label={searchLabel}
            title={searchLabel}
          >
            <SearchIcon size={18} />
            <span aria-hidden="true" className="max-w-full whitespace-normal text-center text-[10px] leading-[1.1] font-medium tracking-tight line-clamp-2">{i18nT('nav.search_short')}</span>
          </button>
        </nav>
      )
    }
    : null

  return (
    <ZoomProvider>
    <WsContext.Provider value={{ subscribeLogs, subscribeSubagents, forceReconnect }}>
    {/* Above the layout branch, so every layout that can host the import row
        also hosts its outcome: the row's menu has closed by the time it lands. */}
    <ImportSessionOutcomeNotice />
    {isPopout ? (
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
    <div
      ref={shellRef}
      data-testid="dashboard-shell"
      data-workspace-fullscreen={panelFullscreen || undefined}
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

      {/* Full-height activity bar slot: ChatPage portals its
          Activity panel here on desktop so it spans the window top-to-bottom
          instead of sitting below the header row. Empty (0 width) when the
          panel is closed or on non-chat routes. */}
      {!isMobile && (
        <motion.div
          id="activity-bar-slot"
          layout
          layoutDependency={panelFullscreen}
          transition={{ layout: { duration: reduceWorkspaceMotion ? 0 : 0.18 } }}
          className="h-full min-h-0 min-w-0"
          style={{ gridArea: 'actbar' }}
        />
      )}

      {/* Skip to content — visible only on focus for keyboard users */}
      <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[9999] focus:px-4 focus:py-2 focus:rounded-lg focus:bg-accent focus:text-accent-fg focus:text-sm focus:font-medium">{i18nT('app.skip_to_content')}</a>

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
        </>
      )}

      {/* Topbar */}
      {/* stable theming hook — see website/docs/theming-contract.md */}
      <header
        ref={topPeekSurface}
        // `topbar-single` (phone chat page) swaps the three-track grid for
        // `auto minmax(0,1fr) auto` (index.css): the centre cell is the chat
        // page's title slot and takes every pixel the two side cells leave.
        // Those side cells are a plain flex div / `tb-trail`, NOT `tb-left` /
        // `tb-right`: the latter are inline-size containers, and a size
        // container in an `auto` track has no content size to give, so it
        // collapses to its padding and clips whatever it holds
        // (test/topbarMenuButtonNarrow.test.ts records the measurement).
        className={`topbar topbar-glass relative pl-2 pr-3${mobileSingle ? ' topbar-single' : ''}`}
        // Both z-indexes come from lib/themeDecorLayer.ts, which derives the
        // theme-overlay ceiling from them — the header must outrank pack
        // decoration in both layouts (#7377), and a literal here could drift.
        //
        // In focus mode the header leaves the grid and becomes an overlay
        // positioned against the shell (which is already `relative`), NOT the
        // viewport: `position: fixed` would be measured against whichever
        // ancestor happens to establish a containing block, and the shell is the
        // app area either way. It stays MOUNTED and slides — unmounting it would
        // tear down the notification/metrics popovers it owns and lose their
        // state on every peek. TOPBAR_FOCUS_Z clears the whole chat-pane
        // stack (max 61), workspace fullscreen (65), and the rail (50), while
        // staying under the update banner (70), side sheets (89/90) and every
        // modal (100+).
        style={focusActive
          ? {
            position: 'absolute',
            top: 0, left: 0, right: 0, height: 42,
            zIndex: TOPBAR_FOCUS_Z,
            transform: topChromeShown ? 'translateY(0)' : 'translateY(-100%)',
            transition: 'transform 200ms cubic-bezier(0.2, 0, 0, 1)',
            // Hidden chrome must not eat clicks aimed at the content beneath it.
            pointerEvents: topChromeShown ? 'auto' : 'none',
          }
          : { gridArea: 'topbar', zIndex: TOPBAR_Z }}
        {...(focusActive ? topPeek.surfaceProps : {})}
      >
        {/* Left: mobile menu toggle + inline instance selector. The brand now
            lives in the sidebar (item 1.1). The selector reuses InstanceTabBar's
            visibility rule — it renders nothing unless >=1 remote instance
            exists, so the common single-instance header-left is empty (only the
            macOS traffic-light clearance remains). */}
        {/* No mobile-only `px-2` here on purpose. The icon buttons inside carry
            their own 8px, so this padding stacked on top of the header's `pl-2`
            and pushed the hamburger out past the page's own left edge. Dropping
            it lands the button's BOX at 8 + 8 = 16px, the page gutter; the glyph
            inside it then needs its own 2.5px correction because `Menu`'s artwork
            does not fill its box (see the button below). Box and glyph together
            put the hamburger, the page title and the chat session-list toggle on
            one line. Deliberately only the LEFT cluster:
            `.tb-right` carries a padding/negative-margin pair that keeps the
            notification badge's 4px overhang from being clipped, and re-tuning
            that needs a real WebKit check, not a local one. */}
        {!mobileSingle && (
        <div className="tb-left relative h-full">
          {/* Windows only: the application menu shares this cluster. It needs no
              width reservation of its own: the identity group is sized by its own
              grid track, and the menu growing from the hamburger to its six
              labels therefore consumes the GROUP's width -- which its container
              query responds to -- instead of eating the centred search's. */}
          {!isMobile && isWinElectron && <WindowsTitlebarMenu />}

          {/* Route-history Back/Forward (#8258). Desktop layout only: on mobile
              the platform owns Back (left-edge swipe), and the drill-in surfaces
              navigate by component state that pushes nothing, so arrows there
              would walk an unrelated stack. Order: after the Windows app menu,
              before the instance selector — the leftmost NAVIGATION control,
              matching where every browser puts it. */}
          {!isMobile && <NavHistoryArrows />}
          {isMobile && (
            <button className="group p-2 rounded-md bg-transparent border-none cursor-pointer text-muted hover:text-text shrink-0" onClick={toggleNav} aria-label={i18nT('app.open_menu')}>
              {/* The product logo, not a generic menu glyph. A narrow layout has exactly
                  one nav affordance, and it opens the same rail whose header carries this
                  same `avatar` on a wide one -- so it is the same asset, the same
                  `rounded-md object-contain` treatment and the same hover tilt, which is
                  live here because this bar is what a NARROW WINDOW gets, not only a
                  touch device. Reading `avatar` rather than importing a file is what
                  keeps a theme-supplied or user-configured logo in step: the branding
                  registry resolves it once for the whole shell.

                  A full-colour raster mark is an <img>, which is exactly what the
                  `use-lucide-icons` rule's brand-mark exception prescribes -- a CSS mask
                  over `currentColor` would flatten the art to one colour. But an <img>
                  can FAIL, and `alt=""` + `aria-hidden` means failure renders nothing --
                  an invisible button as the page's only nav route -- so MobileNavGlyph
                  holds the Menu hamburger up until the logo's own `load` event.

                  Square box, so no optical correction exists: the art is square and
                  `object-contain` fills the box, putting the ink on the 16px page gutter
                  (topbar pl-2 + this button's p-2) that the page title and every card's
                  left edge below it sit on, with the button's own box at 24 + 16 = 40px
                  for the tap target. `narrowFirstBaseline.test.ts` re-derives that sum. */}
              <MobileNavGlyph avatar={avatar} />
            </button>
          )}
          <InstanceTabBar variant="inline" />
        </div>
        )}
        {/* Phone chat page, leading cell: the crew switcher (renders nothing
            until a remote crew exists) and downstream widgets while they exist.
            The update pill is NOT here: with a remote crew the switcher already
            renders its chip and its dropdown, and the pill made a third action
            in the group — on this page the update is the first item of the
            chat page's overflow menu instead (UpdatePill variant="menu-item").
            The nav-drawer logo is not here either — on this page the main
            destinations are the rail inside the sessions drawer, opened by the
            toggle the chat page puts first in the centre slot, so the bar never
            offers two drawers. Sized `auto`, so the common empty cell costs no
            width and the sessions toggle stays on the gutter. */}
        {mobileSingle && (
          <div data-testid="topbar-lead" className="relative h-full flex items-center gap-1.5 min-w-0">
            <InstanceTabBar variant="inline" />
            {getTopBarWidgets().map(w => (
              <ErrorBoundary key={w.id} scope={`topbar-widget:${w.id}`} fallback={null}>
                <w.component />
              </ErrorBoundary>
            ))}
          </div>
        )}
        {/* Centre track: the ⌘K trigger. A flow item, not an overlay — its width
            is the track's width, so it can never sit under a sibling cluster and
            never has to be dropped to stay clear of one. On mobile the same
            track holds the icon-only form below.

            Wrapped with the focus-mode toggle in ONE flex cell rather than added
            as a fourth grid child: `.topbar` declares exactly three tracks, so a
            bare sibling would be auto-placed into `.tb-right` and land inside the
            readout capsule's cluster. Two controls, which is the ceiling
            website/AUTOSDE.yaml's max-two-buttons-per-row sets.

            `data-topbar-overlay` is read by the crew-pin capture harnesses
            (website/scripts/capture-crew-pin-chips.mjs, record-crew-pin-chips.mjs,
            capture-crew-chip-shrink.mjs) to find this cell; nothing in src/
            reads it any more. Keep it. */}
        {!isMobile && (
          <div data-topbar-overlay className="flex items-center gap-1.5 min-w-0">
          {/* The trigger IS a Liquid Glass pane (components/Glass.tsx, chip
              recipe) rendered as the button, the same material as the
              sidebar's search field and the composer dock: no border and no
              fill of its own, `glass-hover` for the hover step. */}
          <Glass
            as="button"
            type="button"
            variant="chip"
            radius={TOPBAR_PILL_RADIUS}
            onClick={commandPalette.openPalette}
            className="glass-shadow glass-hover h-7 flex-1 min-w-0 px-3 text-muted hover:text-text transition-colors flex items-center justify-center gap-2 cursor-pointer"
            /* The trigger has to describe the surface it actually opens. While an app
               owns the quick-search slot the gesture opens a launcher -- typing runs
               commands and does not search the corpora this label promises -- so
               naming "search for anything" there is the most visible mispromise in
               the product. */
            aria-label={
              slotOwners['quick-search']
                ? i18nT('app.open_command_bar')
                : i18nT('app.search_sessions_files_and_commands')
            }
            // Gated on the same condition as the label and aria-label above. Leaving
            // this one unconditional made the hover contradict the words under the
            // cursor and promise the corpus search the launcher deliberately omits --
            // the diff's own fix applied to two of three attributes. The owned branch
            // drops "(K)" because the chord is already printed in the visible label.
            title={
              slotOwners['quick-search']
                ? i18nT('app.open_command_bar')
                : i18nT('app.search_everywhere_k')
            }
          >
            <span className="text-[13px] truncate min-w-0">
              {slotOwners['quick-search']
                ? i18nT('app.k_run_a_command')
                : i18nT('app.k_search_for_anything')}
            </span>
          </Glass>
          {/* Focus mode. `aria-pressed` rather than a second label, so a screen
              reader gets the state from the control instead of from copy that
              would have to be kept in step with the icon. */}
          <button
            type="button"
            data-testid="focus-mode-toggle"
            onClick={toggleFocusMode}
            className={`flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 ${focusMode ? 'text-accent' : 'text-muted hover:text-text'}`}
            aria-label={i18nT('app.focus_mode')}
            aria-pressed={focusMode}
            title={i18nT(IS_MAC ? 'app.focus_mode_title_mac' : 'app.focus_mode_title')}
          >
            <Fullscreen size={15} />
          </button>
          </div>
        )}
        {/* Mobile centre cell. On the chat page it is the slot the chat page
            fills through a portal: [sessions toggle][session title + menu].
            `min-w-0` + `flex` so the title inside can truncate instead of
            growing the cell. Elsewhere the cell is an empty spacer: the search
            trigger that used to sit here moved into the chat drawer's rail, and
            the header still needs its third in-flow child so the actions group
            is not auto-placed into the `auto` centre track (where, as a size
            container, it would collapse to its padding). */}
        {isMobile && (mobileSingle
          ? <div id="mobile-topbar-slot" data-testid="mobile-topbar-slot" className="flex items-center gap-1 min-w-0 h-full" />
          : <span aria-hidden="true" data-testid="topbar-centre-spacer" className="w-0" />
        )}
        {/* Theme decoration: the active theme's center top-bar element (e.g. a
            scanner sweep), chosen by resolved mode. Absent unless a registered
            theme declares one. It renders as a BACKGROUND layer rather than a
            grid cell: the header's three tracks are load-bearing now (sides are
            pure remainder), so a fourth flow item would land in an implicit
            column and shift the search off centre. A sweep/scanline is visually
            a backdrop anyway, so it is inert to pointers and sits behind the
            controls. Wrapped in a slot-level ErrorBoundary (fallback=null) so a
            faulty registered extension disables only itself instead of crashing
            the whole shell via the root boundary. */}
        {(() => {
          if (branding?.topBarHideOnMobile && isMobile) return null
          const TB = resolvedMode === 'light' ? branding?.topBar?.light : branding?.topBar?.dark
          return TB ? (
            <ErrorBoundary key={`${colorTheme}:${resolvedMode}`} scope="theme-topbar" fallback={null}>
              <div className="absolute inset-0 pointer-events-none overflow-hidden" aria-hidden="true"><TB /></div>
            </ErrorBoundary>
          ) : null
        })()}
        {/* `tb-has-update` shifts the collapse ladder's rungs (index.css): the
            update pill is a conditional, non-shrinking sibling of the ladder,
            so while it is mounted the group's fixed content is wider by the
            pill's footprint and the ≥640px rungs fire that much earlier. Below
            640px no rung shifts (#7698): a phone hands the group ≤240px
            routinely, so a shifted terminal rung blanked the readouts for the
            whole time an update was pending; the nowrap backstop clips the
            squeeze instead. The class keys off the same selector the pill
            itself reads, so they move together; during the pill's lazy-chunk
            fetch the class can lead the pill by a moment, which costs readout
            room briefly and harms nothing. */}
        {!mobileSingle && (
        <div ref={metricsGroupRef} className={`tb-right relative${updateAvailable ? ' tb-has-update' : ''}`}>
          {/* Zero-footprint probe for the metrics rung. It carries the readings'
              own class, so JS reads the LADDER's verdict rather than a copy of
              its thresholds. Out of flow and 0x0, so it costs no ladder budget
              and adds no flex gap. */}
          <span ref={metricsProbeRef} className="tb-drop-metrics tb-metrics-probe" aria-hidden="true" />

          {/* Theme decoration: extra aside control (e.g. a stardate / clock). */}
          {branding?.topBarAside && !(branding?.topBarHideOnMobile && isMobile) && (
            <ErrorBoundary key={`${colorTheme}:${resolvedMode}`} scope="theme-aside" fallback={null}>
              <branding.topBarAside />
            </ErrorBoundary>
          )}
          {/* Unified readout capsule — connection dot . system metrics .
              kiro-credits usage pooled into one bordered pill. Offline: the
              whole capsule tints danger (red border + subtle red bg + red
              dot), no "Offline" text — the color shift is the signal. When
              auth expired the session-expired banner stays the primary signal;
              the capsule reddens quietly underneath it. (The upstream
              enterprise-SSO segment is dropped here: that SSO flow is stubbed
              in this fork. The Claude-cost usage branch is likewise dropped:
              this fork's usage pill is Kiro-credits-only.)

              Desktop only. A phone bar has room for two controls on the right
              (bell + the chat page's overflow menu) and a resource readout is
              not something a phone user acts on; the session-expired banner
              stays the offline signal there. */}
          {!isMobile && (() => {
            const offline = !connected
            // The accessible name and the role="status" live region are the
            // ONLY screen-reader carriers of the offline cause: the
            // session-expired banner api/client.ts injects is a plain <div>
            // with no role="alert"/aria-live, so it is never announced. When
            // auth is the real cause they must therefore say so -- announcing
            // the generic "Gateway offline" points a screen-reader user at
            // reconnection when pasting a token is the fix. This mirrors the
            // branch the button `title` already uses (minus the collapse-toggle
            // suffix, which is interaction text, not a status cause).
            // Auth takes precedence over transport for the ANNOUNCED cause:
            // `authRequired` (a 403 auth flag) and `connected` (Redux transport
            // state) are independently sourced, so the session can expire while
            // the socket is still up. In that state a transport-first ternary
            // announces "Gateway connected" -- a reassuring lie -- and the
            // session-expired banner api/client.ts injects is a plain <div>
            // with no role="alert"/aria-live, so nothing else corrects it. The
            // transport being up does not help a user whose session is dead, so
            // the auth wording wins whenever auth is the real blocker.
            const gatewayStatusMsg = authRequired
              ? i18nT('app.gateway_offline_session_expired_see_banner_above')
              : connected
                ? i18nT('app.gateway_connected')
                : i18nT('app.gateway_offline_reconnecting')
            // The connection dot doubles as the capsule collapse toggle, so its
            // accessible name must name that action -- not just the gateway
            // state. title and aria-label share this composed value; the
            // role="status" live region stays pure gatewayStatusMsg (a status
            // region announces the connection cause, not the button's toggle
            // affordance, which would speak "click to collapse" on every
            // reconnect).
            const capsuleActionMsg = `${gatewayStatusMsg} · ${capsuleCollapsed ? i18nT('app.click_to_expand_readouts') : i18nT('app.click_to_collapse_readouts')}`
            // whitespace-nowrap is the ladder's backstop for the BUILT-IN
            // segments that share this class string: if the group is ever
            // narrower than its contents (a locale wider than the measured
            // budget, the dev-only pseudolocale), a squeezed segment must clip
            // at the edge, never wrap into two lines the capsule's fixed h-7
            // then crops. Extension segments bring their own class strings and
            // are bounded by the capsule's terminal rung instead.
            const seg = `flex items-center gap-1 -my-0.5 px-1.5 py-0.5 rounded-md bg-transparent border-none cursor-pointer transition-colors hover:bg-bg-hover whitespace-nowrap ${offline ? 'opacity-70' : ''}`
            const segments: ReactNode[] = []
            // The dot doubles as the capsule's collapse toggle: click to
            // fold the readouts down to just the dot, click again to expand.
            // Padding + negative margin keep a usable hit target without
            // growing the visual dot.
            segments.push(
              <button
                key="conn"
                className="flex items-center justify-center p-1.5 -m-1.5 rounded-full bg-transparent border-none cursor-pointer shrink-0"
                onClick={() => { pulseCapsuleLayout(); setCapsuleCollapsed(c => !c) }}
                title={capsuleActionMsg}
                aria-label={capsuleActionMsg}
                aria-expanded={!capsuleCollapsed}
              >
                <span aria-hidden="true" className={`w-1.5 h-1.5 rounded-full transition-colors duration-300 ${offline ? 'bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none' : 'bg-ok shadow-[0_0_8px_rgba(34,197,94,.4)]'}`} />
                {/* Live-region announcement lives in its own hidden span:
                    role="status" on the button itself would override its
                    implicit button role for screen readers. */}
                <span role="status" className="sr-only">{gatewayStatusMsg}</span>
              </button>
            )
            // Resource pressure indicator — always visible when tight/critical
            if (sysMetrics?.posture && sysMetrics.posture !== 'ample' && sysMetrics.posture !== 'unknown') {
              segments.push(
                <span
                  key="resource-health"
                  className={`${seg} flex items-center gap-1 text-[11px] ${sysMetrics.posture === 'critical' ? 'text-danger' : 'text-warn'}`}
                  title={sysMetrics.posture === 'critical'
                    ? i18nT('app.resource_posture_tooltip_critical', { gb: sysMetrics.availableGb?.toFixed(1) ?? '?' })
                    : i18nT('app.resource_posture_tooltip_tight', { gb: sysMetrics.availableGb?.toFixed(1) ?? '?' })}
                >
                  <span aria-hidden="true" className={`inline-block w-2 h-2 rounded-full ${sysMetrics.posture === 'critical' ? 'bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none' : 'bg-warn'}`} />
                  {!isMobile && <span className="font-medium">{sysMetrics.posture === 'critical' ? i18nT('app.resource_critical') : i18nT('app.resource_tight')}</span>}
                  {!isMobile && sysMetrics.subagentCap != null && <span className="text-muted text-[10px]">· {i18nT('app.subagent_cap', { cap: String(sysMetrics.subagentCap) })}</span>}
                </span>
              )
            }
            if (!capsuleCollapsed) {
            if (!isMobile) segments.push(metricsSegment(metrics, seg))
            const usage = kiroUsageSegment({ kiroUsageState, kiroCreditSurface, setKiroUsageOpen }, seg, isMobile)
            if (usage) segments.push(usage)
            }
            // Extension slot: downstream-registered capsule segments (e.g. an
            // edition credential-TTL or spend segment) join the capsule INSIDE
            // its border/dividers/offline-tint, after the core segments, in
            // `order`. Each is isolated in its own ErrorBoundary (fallback=null)
            // so a throwing segment disables only itself. Empty in stock build.
            // Gated on !capsuleCollapsed exactly like the core readouts, so
            // collapsing reduces the capsule to the bare connection dot rather
            // than leaving extension segments + their dividers visible.
            if (!capsuleCollapsed) {
              for (const cs of getCapsuleSegments()) {
                if (cs.hideOnMobile && isMobile) continue
                const SegComp = cs.component
                segments.push(
                  <ErrorBoundary key={cs.id} scope={`capsule-segment:${cs.id}`} fallback={null}>
                    <SegComp offline={offline} />
                  </ErrorBoundary>
                )
              }
            }
            return (
              /* layout + tween (not spring: springs bounced in a prior
                 attempt) animates the capsule's width as segments mount and
                 unmount on collapse/expand. The layout transition is gated to
                 a pulse: 0.25s right after an intentional collapse/expand
                 click, else 0s so header reflows (panel open/close, resize)
                 snap the capsule into place instead of sliding it. */
              <motion.div
                layout
                transition={{ layout: { duration: capsuleLayoutPulse ? 0.25 : 0, ease: 'easeOut' } }}
                className="flex items-center shrink-0"
              >
                {/* The capsule IS a Liquid Glass pane (components/Glass.tsx,
                    chip recipe) hosting the segments directly, so the
                    `.tb-capsule > …` rungs in index.css still see them as its
                    children; the motion wrapper outside only animates width.
                    Offline is a tint step (`glass-danger`), never a fill. */}
                <Glass
                  variant="chip"
                  radius={TOPBAR_PILL_RADIUS}
                  className={`tb-capsule glass-shadow flex items-center gap-2 h-7 px-2.5 ${offline ? 'glass-danger' : ''}`}
                >
                  {segments.flatMap((s, i) => (i === 0 ? [s] : [<span key={`sep-${i}`} className="w-px h-3.5 bg-border shrink-0" aria-hidden="true" />, s]))}
                </Glass>
              </motion.div>
            )
          })()}
          {/* Extension slot: downstream-registered top-bar widgets (e.g. a
              credential-TTL capsule or spend pill). Empty in the stock build.
              Each widget is isolated in its own ErrorBoundary (fallback=null) so
              a throwing widget disables only itself, not the shell or its
              sibling widgets. */}
          {getTopBarWidgets().map(w => (
            <ErrorBoundary key={w.id} scope={`topbar-widget:${w.id}`} fallback={null}>
              <w.component />
            </ErrorBoundary>
          ))}
          {/* Update pill — present only while an update exists; deep-links to
              Settings › About. NOT gated on viewport: it is the download's
              only progress home, and hiding it on narrow windows would make
              "Download" consent produce zero visible feedback until the
              staged-build modal fires minutes later. */}
          {updateAvailable && (
            <Suspense fallback={null}>
              <UpdatePill />
            </Suspense>
          )}
          {/* Feedback — "Request a Feature" plus, on a prerelease build, a
              channel chip that opens the same Report a Problem flow. Its own
              bordered pill (28px tall, 12px radius), separated from the readout
              capsule (item 2.3). */}
          {!isMobile && (
            <span className="tb-drop-feedback flex items-center">
              <FeedbackPill
                onRequestFeature={requestFeature}
                onReportProblem={() => setReportProblemOpen(true)}
              />
            </span>
          )}
          {/* Notifications bell — borderless icon button, rightmost control.
              (The activity-panel open toggle now lives in the session header,
              beside the pop-out control — see ChatPage — so opening the panel
              no longer narrows this full-width header.) */}
          <NotificationsBellButton />
        </div>
        )}
        {/* Phone chat page, trailing cell: EXACTLY two controls (the
            max-two-buttons-per-row rule) — the bell, then the chat page's
            overflow menu, portaled into `#mobile-topbar-trail-slot`. The
            update pill is NOT here: with an update pending it made this a
            three-control group. On this page the update is the first item of
            that overflow menu (UpdatePill variant="menu-item"), carrying the
            same lifecycle label as the pill; downstream widgets sit in the
            leading cell. */}
        {mobileSingle && (
          <div className="tb-trail relative h-full flex items-center gap-1.5 shrink-0">
            <NotificationsBellButton />
            <div id="mobile-topbar-trail-slot" data-testid="mobile-topbar-trail-slot" className="flex items-center empty:hidden" />
          </div>
        )}
        {/* Phone: the generic offline signal. The readout capsule (whose dot
            turned red on a transport drop) is not rendered below 768px, so a
            phone would otherwise show nothing while the socket is down. One
            strip hanging off the bar, driven by the same `connected` state the
            capsule read; auth expiry keeps its own banner (api/client.ts), so
            this stays quiet then rather than saying "reconnecting" over a fix
            that is pasting a token. Absolute (out of the bar's grid flow) so
            the three-track invariant holds. */}
        {isMobile && !connected && !authRequired && (
          <div
            role="status"
            data-testid="mobile-offline-strip"
            // `pointer-events-none`: the strip hangs over the top 24px of
            // whatever <main> paints; a readout must not swallow the taps and
            // scroll starts that land there while the socket is down.
            className="absolute left-0 right-0 top-full z-[1] pointer-events-none flex items-center justify-center gap-1.5 h-6 text-[12px] font-medium text-danger bg-danger-subtle border-b border-danger/30"
          >
            <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full bg-danger animate-pulse [animation-iteration-count:3]! motion-reduce:animate-none" />
            {i18nT('app.gateway_offline_reconnecting')}
          </div>
        )}
        {/* Session expired on the phone: the auth banner (api/client.ts) is the
            visible message and has no live region, and the capsule's sr-only
            carrier is not rendered below 768px -- so this is the one
            announcement a screen-reader user gets. Nothing visible: the banner
            already says it. */}
        {isMobile && !connected && authRequired && (
          <span role="status" data-testid="mobile-offline-sr" className="sr-only">{i18nT('app.gateway_offline_session_expired_see_banner_above')}</span>
        )}
      </header>

      {agentSwitchNotice && (
        <div role="status" className="fixed z-[70] top-safe-offset-14 left-safe-offset-4 right-safe-offset-4 sm:left-auto sm:w-[440px] bg-bg-elevated border rounded-lg p-3 flex items-center gap-3 shadow-xl animate-rise" style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}>
          <span className="text-sm text-text flex-1">{agentSwitchNotice.message}</span>
          <button onClick={() => dispatch(setAgentSwitchNotice(null))} aria-label={i18nT('app.dismiss')} className="text-muted hover:text-text leading-none p-0.5"><X className="lucide-inline w-4 h-4" /></button>
        </div>
      )}

      {/* Report a Problem — mounted by the nav rail's "Report issue" link. */}
      <ReportProblemModal open={reportProblemOpen} onClose={() => setReportProblemOpen(false)} />

      {/* Update error modal */}
      {updateError && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 backdrop-blur-xs animate-rise" role="dialog" aria-modal="true" aria-label={i18nT('app.update_error')}>
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
        </div>
      )}

      {/* Changelog modal */}
      <ChangelogModal flow={updateFlow} updateAvailable={updateAvailable} />

      {/* Updating overlay */}
      {(updating || showUpdateModal) && <UpdateOverlay onCancel={() => { setUpdating(false); setShowUpdateModal(false) }} />}
      <UpdateModal />
      {updateAvailable && (
        <Suspense fallback={null}>
          <UpdateFoundModal />
        </Suspense>
      )}
      <StartupVideo startupVideo={startupVideo} />
      <MobileConnectDialog mobileConnect={mobileConnect} />

      <FirstRunChapters firstRun={firstRun} onboarded={onboarded} privacyAcked={privacyAcked} markOnboarded={markOnboarded} markImportOnboarded={markImportOnboarded} markPrivacyAcked={markPrivacyAcked} />

      {/* Mobile backdrop — opacity is animated by animateDrawer in lockstep
          with the panel (compositor), so there is no framer fade here; it
          mounts at 0 and the slide carries it. Mounted for the whole phase so
          the slide-out fade is not cut short.
          aria-hidden: the scrim is decorative — its click-to-dismiss is a
          pointer convenience, and keyboard users dismiss via Escape (handled
          where the drawer state lives). A focusable full-screen scrim would
          add a giant tab stop over the whole page, which is why this is NOT
          the Clickable component. */}
      {isMobile && mobileNavMounted && (
        <motion.div
          ref={mobileNavScrimRef}
          data-testid="nav-backdrop"
          aria-hidden="true"
          style={{ opacity: mobileNavScrim }}
          className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-xs"
          onClick={closeMobileNavDrawer}
        />
      )}

      {/* Nav */}
      {/* Desktop rail and mobile drawer share one body but get DIFFERENT
          wrappers, and only the mobile drawer sits inside AnimatePresence.
          An exit animation on the desktop rail is actively wrong: when the
          viewport crosses the mobile threshold, the shell grid drops its
          `nav` area in the same render — AnimatePresence would keep the
          exiting rail mounted with its frozen `gridArea: 'nav'` style, and
          CSS auto-places that orphaned item into an implicit row BELOW the
          content (the rail visibly jumped under the chat input before
          sliding away). The desktop rail therefore unmounts instantly at
          the threshold; only the fixed-position drawer animates in/out. */}
      {(() => {
        const navBody = (<>
        {/* Top-fixed: menu row + primary destinations + Apps section header.
            The sidebar toggle lives HERE (menu row), not in the topbar. */}
        <div className="shrink-0 flex flex-col gap-0.5 px-2 pt-2">
          <RailBrandToggle effectiveCollapsed={effectiveCollapsed} toggleNav={toggleNav} avatar={avatar} branding={branding} botName={botName} />
          {/* Hairline under the expanded header (collapsed rail has none —
              the big logo alone separates well). */}
          {!effectiveCollapsed && <div aria-hidden="true" className="h-px bg-border shrink-0 mb-[7px]" />}
          {advertisedNavItems.filter(n => n.group === 'Main').map(n => <div key={n.id}>{renderNavRow(n)}</div>)}
          {/* Apps section: the old single "Explore" header link split into two
              nav rows — Discover (the storefront, /apps) and Library
              (installed-app management, /apps/library). Expanded keeps the
              muted "Apps" section label above them; collapsed renders the two
              rows as regular icon rows like their neighbors. The unread-updates
              badge rides Discover (navId "apps"), matching where update
              discovery lives. NavItem carries data-onboarding-nav={navId}, so
              the onboarding anchor "apps" stays on the Discover row. */}
          {!effectiveCollapsed ? (
            <>
              <div className="nav-section flex items-center pl-3 pr-1 pt-3 pb-1">
                <span
                  // `overflow-hidden` + `whitespace-nowrap` means this clips
                  // silently once the label grows — which it does in a longer
                  // locale. The `title` keeps the full string reachable instead
                  // of losing the tail with no affordance.
                  title={i18nT('app.apps')}
                  className="text-[13px] font-medium text-muted whitespace-nowrap overflow-hidden"
                >{i18nT('app.apps')}</span>
              </div>
              <NavItem
                navId="apps"
                path="/apps"
                label={i18nT('nav.discover')}
                icon={<Compass size={16} />}
                active={discoverNavActive}
                collapsed={false}
                onClick={closeMobileNav}
                badge={<NavBadge navId="apps" collapsed={false} appBadges={discoverBadges} />}
              />
              <NavItem
                navId="apps-library"
                path="/apps/library"
                label={i18nT('nav.library')}
                icon={<LayoutGrid size={16} />}
                active={libraryNavActive}
                collapsed={false}
                onClick={closeMobileNav}
              />
            </>
          ) : (
            <motion.div
              className="mt-4"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2, ease: 'easeOut' }}
            >
              <NavItem
                navId="apps"
                path="/apps"
                label={i18nT('nav.discover')}
                icon={<Compass size={16} />}
                active={discoverNavActive}
                collapsed
                onClick={closeMobileNav}
                badge={<NavBadge navId="apps" collapsed appBadges={discoverBadges} />}
              />
              <NavItem
                navId="apps-library"
                path="/apps/library"
                label={i18nT('nav.library')}
                icon={<LayoutGrid size={16} />}
                active={libraryNavActive}
                collapsed
                onClick={closeMobileNav}
              />
            </motion.div>
          )}
        </div>

        {/* Apps list: scrolls in its OWN frame when many apps are enabled —
            the top (menu/mains/header) and bottom sections stay pinned.
            Collapsed hover labels are portaled to <body> (see NavItem /
            NavToggle) so this vertical clip never chops them at the rail
            edge. overscroll-y-none kills the macOS rubber-band bounce;
            scrollbar-none + scrollbarWidth hide the scrollbar across
            Firefox, modern WebKit, and older Safari (<16). */}
        <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden overscroll-y-none scrollbar-none px-2" style={{ scrollbarWidth: 'none' }}>
          <div className="grid gap-0.5">
            {(() => {
              const fullList = sortedAppGroup
              // Collapse a long Apps list behind a "N more" toggle (both expanded
              // and collapsed modes). Keep the active item visible even when it's
              // in the overflow, so navigation state is never hidden.
              const overflowing = !appsExpanded && fullList.length > APPS_NAV_LIMIT
              const visible = overflowing
                ? fullList.filter((n, i) => i < APPS_NAV_LIMIT || activePath === n.path || activePath.startsWith(n.path + '/'))
                : fullList
              const hiddenCount = fullList.length - visible.length
              // Apps rows are dnd-kit sortable. Rows reflow to open a gap as one
              // is dragged; the source dims and a DragOverlay renders the ghost.
              // SortableContext/DndContext add no DOM wrapper, so the parent grid
              // gap is unchanged.
              //
              // Overflow caveat: when collapsed behind "N more", the active app
              // may be PULLED IN from the overflow to keep its nav state visible
              // (`visible` keeps it past APPS_NAV_LIMIT). That pulled-in row must
              // NOT be sortable: handleAppDragEnd resolves from/to against the
              // FULL order, so dropping onto a row whose full-list index is
              // >= APPS_NAV_LIMIT would push the dragged app past the limit and
              // into the hidden overflow (it would disappear). Restrict the
              // sortable set to the always-visible window (first APPS_NAV_LIMIT)
              // and render any pulled-in overflow row as a plain static row —
              // still navigable, but it registers no droppable, so a drag can
              // never resolve to it and both endpoints stay in-window. (Trimming
              // only SortableContext.items is insufficient: useSortable registers
              // a droppable per wrapped row regardless of the items array.)
              const sortableRows = overflowing ? visible.slice(0, APPS_NAV_LIMIT) : visible
              const pulledInRows = overflowing ? visible.slice(APPS_NAV_LIMIT) : []
              const activeApp = activeAppDragId ? fullList.find(n => n.id === activeAppDragId) : null
              return (<>
              <DndContext sensors={appDndSensors} collisionDetection={closestCenter} onDragStart={handleAppDragStart} onDragEnd={handleAppDragEnd} onDragCancel={handleAppDragCancel}>
                <SortableContext items={sortableRows.map(n => n.id)} strategy={verticalListSortingStrategy}>
                  {sortableRows.map(n => (
                    <SortableAppNavRow key={n.id} id={n.id}>{renderNavRow(n)}</SortableAppNavRow>
                  ))}
                </SortableContext>
                {/* Pulled-in active overflow row(s): static, non-draggable. */}
                {pulledInRows.map(n => <div key={n.id} role="presentation">{renderNavRow(n)}</div>)}
                <DragOverlay>{activeApp ? renderNavRow(activeApp) : null}</DragOverlay>
              </DndContext>
              {/* Show the toggle whenever the list is collapsible, NOT only when
               *  hiddenCount > 0 — otherwise navigating to an app that's the sole
               *  overflow item pulls it into `visible` (hiddenCount → 0) and the
               *  toggle vanishes, causing a jarring layout shift as you move
               *  between apps. The toggle stays put; only its label changes. */}
              {fullList.length > APPS_NAV_LIMIT && (
                <NavToggle
                  collapsed={effectiveCollapsed}
                  expanded={appsExpanded}
                  hiddenCount={hiddenCount}
                  onClick={toggleAppsExpanded}
                />
              )}
              </>)
            })()}
          </div>
        </div>

        {/* Bottom-fixed: Agent Capabilities, Developer (only when dev mode is
            enabled), Settings, and the community row. Pinned to the
            rail's bottom edge — the Apps frame above absorbs the scroll. */}
        {(() => {
          const s = NAV_ITEMS.find(n => n.id === 'settings')!
          const cap = NAV_ITEMS.find(n => n.id === 'capabilities')!
          const devPath = '/developer'
          return (
            <div className="shrink-0 grid gap-0.5 px-2 pt-1 pb-2">
              {devMode && (() => {
                const dotClass = effectiveCollapsed
                  ? 'absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10 animate-pulse'
                  : 'absolute top-1/2 -translate-y-1/2 right-2 w-2 h-2 bg-accent rounded-full z-10 animate-pulse'
                return (
                <NavItem
                  path={devPath}
                  label={i18nT('app.developer')}
                  icon={<Code size={16} />}
                  active={activePath === devPath}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  badge={!devPageSeen && activePath !== devPath ? <span className={`${dotClass} [animation-iteration-count:3]!`} /> : undefined}
                />
                )
              })()}
              {terminalEnabled && (
                <NavItem
                  path="#"
                  label={i18nT('app.terminal')}
                  icon={<SquareTerminal size={16} />}
                  /* This row TOGGLES the docked panel instead of navigating, so
                     "active" tracks the panel's open flag rather than the route.
                     Without it the row only lit on hover, leaving no indication
                     the panel below was open once the pointer moved away. */
                  active={isTerminalShown(bottomTerminalOpen, panelFullscreen) || terminalPoppedOut}
                  pressed={isTerminalShown(bottomTerminalOpen, panelFullscreen) || terminalPoppedOut}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  /* While popped out: focus only (a refused programmatic
                     focus is a harmless no-op). Explicit re-dock lives in the
                     TerminalDetachedBar below -- never a timing heuristic.
                     Workspace fullscreen exits first, as the terminal chord
                     does, so the docked panel is not opened behind it, and a
                     terminal open beneath fullscreen reads as hidden. */
                  onClickOverride={() => activateTerminalEntry({
                    open: bottomTerminalOpen,
                    workspaceFullscreen: panelFullscreen,
                    poppedOut: terminalPoppedOut,
                    exitFullscreen: exitWorkspaceFullscreen,
                    focusPopout: focusTerminalPopout,
                    toggle: () => toggleBottomTerminal(activeSlotProject),
                  })}
                />
              )}
              {hasRenderableMobileConnect && (
                <NavItem
                  path="#"
                  label={i18nT('app.connect_your_phone')}
                  icon={<Smartphone size={16} />}
                  /* Toggles the connect dialog instead of navigating — same
                     contract as the terminal row above. */
                  active={mobileConnectOpen}
                  pressed={mobileConnectOpen}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  onClickOverride={() => setMobileConnectOpen(true)}
                />
              )}
              {/* Phone only: Search as a nav row. The bar's search square left
                  the phone (the chat page's bar has no room for it), so the nav
                  drawer — the one surface every non-chat phone page opens — is
                  where the command palette is reached; the chat page reaches it
                  from its own drawer's rail. Same label, same palette. */}
              {isMobile && (
                <NavItem
                  path="#"
                  label={slotOwners['quick-search'] ? i18nT('app.open_command_bar') : i18nT('app.search_sessions_files_and_commands')}
                  icon={<SearchIcon size={16} />}
                  active={false}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  onClickOverride={commandPalette.openPalette}
                  navId="search"
                />
              )}
              <div>{renderNavRow(cap)}</div>
              {/* Phone only: the account modal (balance, sign-in state) has no
                  other phone entry -- the readout capsule that opens it on the
                  desktop is not rendered on the phone. Shown on exactly the
                  readings the desktop segment shows (`kiroAccountEntry`); the
                  chat page reaches it from its own drawer's rail. */}
              {isMobile && kiroAccountEntry && (
                <NavItem
                  path="#"
                  label={i18nT('components.kiroAccountModal.kiro_account')}
                  icon={<Coins size={16} />}
                  active={kiroUsageOpen}
                  pressed={kiroUsageOpen}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  onClickOverride={() => setKiroUsageOpen(true)}
                  navId="account"
                />
              )}
              <NavItem
                path={s.path}
                label={surfaceLabel(s)}
                icon={s.icon}
                active={activePath === s.path || activePath.startsWith(s.path + '/')}
                collapsed={effectiveCollapsed}
                onClick={closeMobileNav}
                badge={updateAvailable ? <span title={i18nT('app.update_available')} role="status" aria-label={i18nT('app.update_available_2')} className={effectiveCollapsed ? 'absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10' : 'absolute top-1/2 -translate-y-1/2 right-2 w-2 h-2 bg-accent rounded-full z-10'} /> : undefined}
              />
              <RailCommunityLinks effectiveCollapsed={effectiveCollapsed} setReportProblemOpen={setReportProblemOpen} />
            </div>
          )
        })()}
        </>)
        return isMobile ? (
          <>
            {mobileNavMounted && (
              /* mt-2, unlike the desktop rail's mt-0: this form is `fixed` to the
                 VIEWPORT top rather than sitting in the grid row below the
                 topbar, so mt-0 pressed the card's rounded top edge flat against
                 the screen while mx-2/mb-2 inset the other three sides. Matching
                 the 8px inset on all four keeps the drawer reading as one
                 floating card. `top-0 bottom-0` with both margins resolves the
                 height to viewport-16px, so nothing is clipped. */
              /* motion.nav, like the sessions drawer and the right overlay: a
                 drag writes `mobileNavX` directly and ONLY a live binding paints
                 those frames. A plain <nav> reading `mobileNavX.get()` at render
                 time was correct while the tap was this panel's only mover —
                 a MotionValue deliberately does not re-render React, so once the
                 drawer gained a gesture the panel froze after the single
                 re-render the lock happens to cause, and moved only on release
                 when the settle took over. The settle still runs on the
                 COMPOSITOR through mobileNavPanelRef; framer and that animation
                 coexist here exactly as they do for the other two panels,
                 because `takeOverDrawer` adopts and cancels whatever is running
                 before either one writes. */
              <motion.nav
                key="mobile-nav-drawer"
                ref={mobileNavPanelRef}
                style={{ width: MOBILE_NAV_WIDTH, x: mobileNavX }}
                className="bg-bg-elevated border border-border rounded-xl flex flex-col mx-2 mt-2 mb-2 shadow-sm z-50 overflow-hidden fixed top-safe left-safe bottom-safe"
                role="navigation"
                aria-label={i18nT('app.main_navigation')}
              >
                {navBody}
              </motion.nav>
            )}
          </>
        ) : (
          <nav
            ref={railPeekSurface}
            className="focus-chrome-rail bg-bg-elevated border border-border rounded-xl flex flex-col mx-2 mt-0 mb-2 shadow-sm z-50 overflow-hidden"
            // Focus mode: same overlay treatment as the header. The rail's own
            // `mx-2` means translateX(-100%) would leave its 8px left margin
            // showing as a sliver, hence the extra 12px of travel. Width has to
            // become explicit — out of the grid there is no track to fill — and
            // it is the rail TRACK minus the 16px of horizontal margin, so the
            // overlay is exactly as wide as the docked rail would have been at
            // the user's current collapse state.
            style={focusActive
              ? {
                position: 'absolute',
                left: 0,
                top: FOCUS_INSET,
                bottom: 0,
                width: railWidthFor({ isMobile: false, collapsed: effectiveCollapsed }) - 16,
                // Both focus chrome surfaces must clear workspace fullscreen;
                // one shared layer keeps their peek behaviour symmetric.
                zIndex: TOPBAR_FOCUS_Z,
                transform: railPeek.open ? 'translateX(0)' : 'translateX(calc(-100% - 12px))',
                transition: 'transform 200ms cubic-bezier(0.2, 0, 0, 1)',
                pointerEvents: railPeek.open ? 'auto' : 'none',
              }
              : { gridArea: 'nav', width: 'auto' }}
            role="navigation"
            aria-label={i18nT('app.main_navigation')}
            {...(focusActive ? railPeek.surfaceProps : {})}
          >
            {navBody}
          </nav>
        )
      })()}

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
        <main id="main-content" tabIndex={-1} className={`flex flex-col min-h-0 min-w-0 flex-1 overflow-x-hidden ${needsFixedHeight ? 'overflow-hidden p-0' : 'overflow-y-auto'}`}>
          <MigrationCheck />
          {/* Route-independent, unlike MigrationCheck: "you crashed" is true of
              the app, not of the page, and the launch after a crash rarely lands
              on the page the user was on when it happened. */}
          <CrashReportNotice />
          {/* The rail renderer reaches the chat page through context rather than
              a prop: the route element is shared with the popout/embed frames. */}
          <MobileNavRailContext.Provider value={mobileNavRail}>
          <WorkspacePanelContext.Provider value={setWorkspaceSearchOpen}>
          <WorkspaceFullscreenContext.Provider value={isChat && !isMobile ? workspaceFullscreenControls : null}>
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
          </WorkspaceFullscreenContext.Provider>
          </WorkspacePanelContext.Provider>
          </MobileNavRailContext.Provider>
        </main>
        {/* App-wide docked terminal panel — renders beside <main> (right) or
            below it (bottom). The detached bar (popped-out state) always renders
            below the flex wrapper as a full-width strip regardless of position. */}
        {terminalEnabled && !terminalPoppedOut && <BottomTerminalPanel />}
        </div>{/* /flex-row or flex-col wrapper */}
        {terminalEnabled && terminalPoppedOut && <TerminalDetachedBar />}
      </div>

      {/* Self-managed floating panels: lifecycle-driven (hidden → small → chip),
          not motion.* children, so they live outside AnimatePresence. The browse
          mirror docks bottom-right and the computer-use PiP bottom-left, so both
          can be open at once. A direct shell child, outside the content column,
          because workspace fullscreen makes that column inert while the live view
          of an agent driving the real desktop must stay visible and in reach. */}
      <ComputerUseLiveView />
    </div>{/* /Local dashboard grid */}
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
    )}
    </WsContext.Provider>
    {shortcutsOpen && <ShortcutsModal onClose={() => setShortcutsOpen(false)} />}
    <MetricsCard metrics={metrics} />
    <KiroAccountModal open={kiroUsageOpen} onClose={() => setKiroUsageOpen(false)} usage={kiroUsageState} />
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
    <Lightbox />
    </ZoomProvider>
  )
}
