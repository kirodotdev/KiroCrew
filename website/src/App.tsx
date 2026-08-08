import { useEffect, useState, useCallback, useRef, useMemo, createContext, type HTMLAttributes, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Routes, Route, Navigate, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useAppSelector, useAppDispatch, store } from './store'
import { fetchSlots, sseStatus, setUpdateProgress, setEnabledAppIds, changeApprovalMode } from './store/dashboardSlice'
// Side-effect: registers every built-in surface in the registry. MUST run
// before `getBuiltinSurfaces()` is invoked below to compute `NAV_ITEMS`.
import './surfaces/builtins'
import { getBuiltinSurfaces, getBuiltinSurface, selectSurfaceBadgeCount, selectSurfaceActivityCount, selectAllSurfacesAttention, surfaceLabel } from './surfaces/registry'
import { createSlot, appendMessage, setSlotRunning, switchSlot } from './store/chatSlice'
import { setNavIntentHandler as setArtifactNavIntentHandler } from './utils/artifactPopout'
import { applyNavIntentInMain } from './utils/navIntent'
import { installSoftNavigate } from './utils/errorReport'
import { fetchNotifications, ackNotification } from './store/notificationsSlice'
import { useWebSocket } from './hooks/useWebSocket'
import { useDashboardHealthProbe } from './hooks/useDashboardHealthProbe'
import { useTheme } from './hooks/useTheme'
import { useBranding } from './hooks/useBranding'
import { useRumPageView } from './hooks/useRumPageView'
import { useIsMobile } from './hooks/useIsMobile'
import { setRailWidth, railWidthFor } from './hooks/useRailWidth'
import { useNativeNotification } from './hooks/useNativeNotification'
import { useNotificationSound } from './hooks/useNotificationSound'
import { recordSessionStart, recordEvent } from './rum'
import { ZoomProvider } from './hooks/ZoomProvider'
import { api, isAuthBannerShown } from './api/client'
import { safeSetItem } from './utils/safeStorage'
import { gcOrphanedStorage } from './utils/storageGc'
import { Rocket, Menu, Bell, Code, RefreshCw, Package, Loader2, Download, Hammer, XCircle, Check, AlertTriangle, CheckCircle, X, AudioWaveform, ChevronUp, MoreHorizontal, Coins, ArrowLeftToLine, LayoutGrid, ExternalLink, SquareTerminal, Bot } from 'lucide-react'
import { GithubIcon, DiscordIcon } from './components/BrandIcon'
import { Toggle } from './components/ui'
import OnboardingFlow from './components/OnboardingFlow'
import AgentImportFlow from './components/AgentImportFlow'
import PrivacyChapter from './components/PrivacyChapter'
import { OnboardingShellHost } from './components/OnboardingChapterShell'
import { PREVIEW_FOCUS_EVENT } from './components/WebPreviewPanel'
import { motion, AnimatePresence } from 'framer-motion'
import { usePersistedBool } from './hooks/usePersistedBool'
import { isMacElectron, isWinElectron } from './lib/electron'
import { DndContext, closestCenter, MouseSensor, TouchSensor, useSensor, useSensors, DragOverlay, type DragStartEvent, type DragEndEvent } from '@dnd-kit/core'
import { SortableContext, useSortable, verticalListSortingStrategy, arrayMove } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import ChatPage from './pages/ChatPage'
import PopoutFrame from './pages/PopoutFrame'
import ArtifactPopoutFrame from './pages/ArtifactPopoutFrame'
import TerminalPopoutFrame from './pages/TerminalPopoutFrame'

import ErrorBoundary from './components/ErrorBoundary'
import AppIcon from './components/AppIcon'
import Clickable from './components/Clickable'
import MarkdownRenderer, { Lightbox } from './components/MarkdownRenderer'
import NotificationsPage from './pages/NotificationsPage'
import NotificationDetailPanel from './components/notifications/NotificationDetailPanel'
import NotificationFeed from './components/notifications/NotificationFeed'
import LogsPage from './pages/LogsPage'
import HooksPage from './pages/HooksPage'
import WebhooksPage from './pages/WebhooksPage'
import CapabilitiesPage from './pages/CapabilitiesPage'
import KnowledgePage from './pages/KnowledgePage'
import ArtifactsPage from './pages/ArtifactsPage'
import ArtifactDetailPage from './pages/ArtifactDetailPage'
import RemoteArtifactDetailPage from './pages/RemoteArtifactDetailPage'
import ArtifactDeployPage from './pages/ArtifactDeployPage'
import SettingsPage from './pages/SettingsPage'
import EmbedSettingsPage from './pages/EmbedSettingsPage'
import KiroCrewNavBridge from './components/KiroCrewNavBridge'
import InstanceTabBar from './components/InstanceTabBar'
import InstancesViewport from './components/InstancesViewport'
import EmbeddedHostBridge from './components/EmbeddedHostBridge'
import EmbedTabStrip from './components/EmbedTabStrip'
import DeveloperPage from './pages/DeveloperPage'
import SchedulePage from './pages/SchedulePage'
import { useUpdateSubscription } from './hooks/useUpdateSubscription'
import UpdateModal from './components/UpdateModal'

import ComputerUseLiveView from './components/ComputerUseLiveView'
import BottomTerminalPanel, { TerminalDetachedBar } from './components/BottomTerminalPanel'
import { toggleBottomTerminal, useBottomTerminalOpen } from './hooks/useBottomTerminal'
import { useTerminalPoppedOut, focusPopout as focusTerminalPopout } from './utils/terminalPopout'
import { setTerminalEnabledFlag } from './utils/terminalRegistry'
import AppsPage from './pages/AppsPage'
import AppPage from './pages/AppPage'
import AppDetailPage from './pages/AppDetailPage'
import MigrationPage from './pages/MigrationPage'
import MigrationCheck from './components/MigrationCheck'
import BuiltinAppRoute from './apps/BuiltinAppRoute'
import { getBuiltinIcon } from './apps/builtinIcons'
import { getThemeBranding } from './themeBranding'
import { getTopBarWidgets } from './apps/topBarWidgets'
import { getCapsuleSegments } from './apps/capsuleSegments'
import { FEATURE_REQUEST_PROMPT_WITH_SKILL, FEATURE_REQUEST_PROMPT_FALLBACK } from './prompts/featureRequest'
import { useKeyboardShortcuts } from './hooks/useKeyboardShortcuts'
import { useInstanceShortcuts } from './hooks/useInstanceShortcuts'
import { useCommandPalette } from './hooks/useCommandPalette'
import { useProvider } from './providers/context'
import { useAgents } from './hooks/useAgents'
import ShortcutsModal from './components/ShortcutsModal'
import CommandPalette from './components/CommandPalette'
import Modal from './components/Modal'
import ReportProblemModal from './components/ReportProblemModal'
import FeedbackPill from './components/FeedbackPill'

import { i18nT } from './i18n/t'
import { appNavTarget } from './appNav'
import { fmtCompact, fmtNumber, fmtPercent } from './i18n/format'
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
    }
  }
}
export const WsContext = createContext<{
  subscribeLogs: LogSubscribeFn
  subscribeSubagents: (s: boolean) => void
  forceReconnect: () => void
}>({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} })

/**
 * Built-in nav items. Sourced from the surface registry (see
 * `src/surfaces/builtins.tsx`) so each item is registered exactly once and
 * its badge wiring lives next to its registration. Adding a new built-in
 * destination is a single registry entry — no code change needed here.
 *
 * Shape and order are preserved for back-compat with the rest of `App.tsx`
 * (group filtering, sortedAppGroup merge with dynamic apps, settings lookup).
 */
/**
 * Static nav descriptors. `label` is intentionally NOT resolved here — this is a
 * module-level constant, so a translated string baked in at import time would be
 * frozen in whatever language happened to be active then (and the rail would
 * stay English while the rest of the dashboard switched). `labelKey` is carried
 * through and resolved per render via `surfaceLabel()`.
 */
const NAV_ITEMS = getBuiltinSurfaces().map(s => ({
  path: s.route,
  id: s.navId,
  label: s.label,
  labelKey: s.labelKey,
  group: s.group,
  icon: s.icon,
}))

/** Usage color class: green (<70%), yellow (70-90%), red (>90%). */
export function metricColor(pct: number): string {
  return pct > 0.9 ? 'text-danger' : pct > 0.7 ? 'text-warn' : 'text-muted'
}
export const memColorClass = metricColor

const TOPBAR_SEARCH_GAP = 12
const TOPBAR_SEARCH_MIN_WIDTH = 240

export function calculateTopbarSearchLayout(brandWidth: number, actionsWidth: number, viewportWidth: number) {
  const gutter = Math.ceil(Math.max(brandWidth, actionsWidth)) + TOPBAR_SEARCH_GAP
  return { gutter, visible: viewportWidth - (gutter * 2) >= TOPBAR_SEARCH_MIN_WIDTH }
}

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
}

const STEP_ORDER = ['pulling', 'syncing', 'building', 'installing', 'restarting']
const STUCK_THRESHOLD_MS = 5 * 60 * 1000 // 5 minutes

const REASONING_EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'xhigh', 'max']
// Approval-mode DISCRIMINANTS in escalating order, cycled by keyboard shortcut.
// Sent to the backend and compared, never rendered — the picker has its own copy.
const APPROVAL_MODE_LEVELS = ['normal', 'trust_reads', 'trust', 'yolo']

function UpdateOverlay({ onCancel }: { onCancel: () => void }) {
  const progress = useAppSelector(s => s.dashboard.updateProgress)
  const dispatch = useAppDispatch()
  const step = progress?.step || ''
  const detail = progress?.detail || ''
  const info = UPDATE_STEPS[step]
  const currentIdx = STEP_ORDER.indexOf(step)
  const isFailed = step === 'failed'
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
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-rise">
      <div className="bg-card border border-border rounded-xl p-8 max-w-md w-full mx-4 shadow-xl text-center">
        <div className="text-4xl mb-4 animate-pulse">{info?.icon || <RefreshCw className="lucide-inline" />}</div>
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
            <div className="text-sm text-danger">{detail || i18nT('app.check_logs_for_details')}</div>
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
        ) : (
          <div className="text-[13px] text-muted">{i18nT('app.page_will_reconnect_when_ready')}</div>
        )}
      </div>
    </div>
  )
}

function BadgeIndicator({ count, collapsed, label }: { count: number; collapsed: boolean; label: string }) {
  if (count <= 0) return null
  const ariaLabel = `${count} ${label}`
  return collapsed
    ? <span className="absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10" role="status" aria-label={ariaLabel} />
    : <span className="absolute right-2 top-1/2 -translate-y-1/2 bg-accent text-accent-fg text-[12px] font-bold px-1 py-[2px] rounded-full min-w-[18px] text-center inline-block leading-[12px]" aria-label={ariaLabel}>{count}</span>
}

/** Live-activity dot for a nav item — distinct from the unread BadgeIndicator:
 *  a small pulsing accent ring rather than a count, so "3 unread" and "agents
 *  working" never overwrite each other on the same row. Positioned left of the
 *  badge when expanded, and offset from the collapsed dot. */
function ActivityIndicator({ count, collapsed, label }: { count: number; collapsed: boolean; label: string }) {
  if (count <= 0) return null
  const ariaLabel = `${count} ${label}`
  return collapsed
    ? <span className="absolute bottom-1 right-1 w-2 h-2 bg-accent rounded-full animate-pulse z-10" role="status" aria-label={ariaLabel} />
    : <span className="absolute right-8 top-1/2 -translate-y-1/2 flex items-center gap-1 text-[11px] text-accent" role="status" aria-label={ariaLabel}>
        <Bot size={11} className="animate-pulse" aria-hidden />
        {count}
      </span>
}

/**
 * Badge slot for a nav item. Resolves the count from the surface registry
 * (built-in surfaces) and falls back to the `mc:app:badge`-driven `appBadges`
 * map (dynamic apps + bridges from non-Redux sources like global approvals)
 * when the surface itself doesn't declare a badge source. This preserves the
 * prior two-pipeline behavior without leaving per-id branches in the
 * renderer.
 */
function NavBadge({ navId, collapsed, appBadges }: { navId: string; collapsed: boolean; appBadges: Record<string, number> }) {
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
      <BadgeIndicator count={builtinCount} collapsed={collapsed} label={builtinLabel} />
      <BadgeIndicator count={dynamicCount} collapsed={collapsed} label={builtinLabel} />
    </>
  )
}

/** Shared hover-label state for collapsed (icon-only) nav rows. The label is
 *  rendered through a portal anchored to the row's screen position rather than
 *  as an in-flow absolute child, because the nav's scroll container clips
 *  vertically (so a tall icon list scrolls instead of spilling out of the rail)
 *  and a vertical clip forces horizontal clipping too, which would chop the
 *  flyout at the 58px rail edge. Repositions while shown so it follows the row
 *  when the rail is scrolled/resized. */
function useNavTip<T extends HTMLElement>(enabled: boolean) {
  const [tip, setTip] = useState<{ top: number; left: number; height: number } | null>(null)
  const [tipOn, setTipOn] = useState(false) // drives the opacity fade
  const rowRef = useRef<T | null>(null)
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const rafId = useRef<number | null>(null)
  const place = useCallback(() => {
    if (!rowRef.current) return
    const r = rowRef.current.getBoundingClientRect()
    // Overlay the row exactly (same top-left + height) so the flyout reads as
    // the collapsed row expanding in place. Bail out (return the same object) if
    // nothing moved — the scroll listener fires on any document scroll, so this
    // avoids needless re-renders when the rail itself didn't move.
    setTip(prev =>
      prev && prev.top === r.top && prev.left === r.left && prev.height === r.height
        ? prev
        : { top: r.top, left: r.left, height: r.height }
    )
  }, [])
  const showTip = useCallback(() => {
    if (!enabled || !rowRef.current) return
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    place()
    // Mount at opacity 0, then flip next frame so the CSS opacity transition
    // runs (a portal can't fade if it mounts already-visible). Track the handle
    // so a fast hover-out can cancel it — otherwise the rAF fires after hideTip
    // and flashes the label to full opacity before the unmount timer.
    if (rafId.current != null) cancelAnimationFrame(rafId.current)
    rafId.current = requestAnimationFrame(() => { rafId.current = null; setTipOn(true) })
  }, [enabled, place])
  const hideTip = useCallback(() => {
    if (rafId.current != null) { cancelAnimationFrame(rafId.current); rafId.current = null }
    setTipOn(false)
    hideTimer.current = setTimeout(() => setTip(null), 150) // keep mounted for fade-out
  }, [])
  // Dismiss with NO fade-out, for rows whose label text changes on activation
  // (the Apps overflow toggle flips "N more" <-> "Show less"). A fading label
  // stays mounted through the re-render, so it would flash the OPPOSITE label
  // as a ghost at the old coordinates before unmounting.
  const dismissTip = useCallback(() => {
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    if (rafId.current != null) { cancelAnimationFrame(rafId.current); rafId.current = null }
    setTipOn(false)
    setTip(null)
  }, [])
  // While shown, follow the row on scroll/resize (capture:true catches the
  // nav's inner scroll container, which doesn't bubble scroll to window).
  // Depend on a stable boolean — not `tip` itself — so the listeners subscribe
  // once when the label appears and unsubscribe once when it goes, instead of
  // churning on every position update `place()` makes during a scroll.
  const tipVisible = tip !== null
  useEffect(() => {
    if (!tipVisible) return
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [tipVisible, place])
  // Reset when the row stops being collapsible (sidebar expands while a tip is
  // up). mouseLeave may never fire if the cursor stays over the row as it grows,
  // which would otherwise leave the scroll/resize listeners attached and firing
  // place() on every document scroll even though the portal no longer renders.
  useEffect(() => {
    if (enabled) return
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    if (rafId.current != null) { cancelAnimationFrame(rafId.current); rafId.current = null }
    setTip(null)
    setTipOn(false)
  }, [enabled])
  useEffect(() => () => {
    if (hideTimer.current) clearTimeout(hideTimer.current)
    if (rafId.current != null) cancelAnimationFrame(rafId.current)
  }, [])
  return { tip, tipOn, rowRef, showTip, hideTip, dismissTip }
}

function NavItem({ path, label, icon, active, collapsed, badge, onClickOverride, onClick, navId, pressed }: {
  path: string; label: string; icon: React.ReactNode; active: boolean; collapsed: boolean; badge?: React.ReactNode; onClickOverride?: () => void; onClick?: () => void; navId?: string
  /** Set on rows that TOGGLE a surface rather than navigate (e.g. the docked
   *  terminal). `active` only paints the row; without aria-pressed a screen
   *  reader announces an identical button whether the panel is open or shut. */
  pressed?: boolean
}) {
  const navigate = useNavigate()
  const iconEl = <span className={`app-icon-nav w-4 h-4 flex items-center justify-center shrink-0 transition-opacity ${active ? 'opacity-100 text-accent is-lit' : 'opacity-70'}`}>{icon}</span>
  const { tip, tipOn, rowRef, showTip, hideTip } = useNavTip<HTMLDivElement>(collapsed)
  const activate = () => { onClick?.(); (onClickOverride || (() => navigate(path)))() }
  return (
    <motion.div layout="position"
      ref={rowRef}
      data-onboarding-nav={navId}
      // role+tabIndex+key handler make this a real keyboard-operable control
      // (Enter/Space activate, preventing Space page-scroll). aria-label names
      // it when collapsed (icon-only, no text).
      role="button"
      tabIndex={0}
      whileHover={collapsed ? undefined : { scale: 1.02 }}
      whileTap={{ scale: 0.97 }}
      transition={{ duration: 0.15 }}
      className={`nav-item group/nav relative flex items-center rounded-md cursor-pointer text-sm font-medium whitespace-nowrap gap-2.5 py-2 pl-3 pr-3 transition-colors duration-200 ${collapsed ? '' : 'overflow-hidden'} ${active ? 'nav-active text-text-strong bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover'}`}
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
    >
      {badge}
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
          className="whitespace-nowrap overflow-hidden"
        >
          {label}
        </span>
      )}
      {collapsed && tip && createPortal(
        <div
          className={`fixed flex items-center gap-2.5 pl-3 pr-3 rounded-md bg-card border border-border shadow-lg text-text text-sm font-medium z-[9999] pointer-events-none whitespace-nowrap transition-opacity duration-150 ${tipOn ? 'opacity-100' : 'opacity-0'}`}
          style={{ top: tip.top, left: tip.left, height: tip.height }}
        >
          <span className={`app-icon-nav w-4 h-4 flex items-center justify-center shrink-0 ${active ? 'text-accent is-lit' : ''}`}>{icon}</span>
          {label}
        </div>,
        document.body
      )}
    </motion.div>
  )
}

/** dnd-kit sortable wrapper for one Apps-nav row. Mirrors SortableFolderBlock in
 *  ChatSidebar: setNodeRef + sortable transform position the row so siblings
 *  reflow to open a gap as it's dragged; the source dims while a DragOverlay
 *  renders the floating ghost. Only `listeners` are spread (not `attributes`),
 *  so the inner NavItem keeps its own role="button"/tabIndex and no nested
 *  drag role is exposed on the wrapper (role="presentation"). Sensor activation
 *  constraints (see appDndSensors) let a plain click/tap reach NavItem
 *  navigation; only a deliberate mouse-drag or touch press-and-hold reorders. */
function SortableAppNavRow({ id, children }: { id: string; children: React.ReactNode }) {
  const { setNodeRef, listeners, transform, transition, isDragging } = useSortable({ id })
  return (
    <div
      ref={setNodeRef}
      role="presentation"
      style={{
        transform: transform ? CSS.Transform.toString(transform) : undefined,
        transition: transition || undefined,
        opacity: isDragging ? 0.4 : 1,
        // 'manipulation' (not 'none') keeps native vertical scroll working when
        // a swipe starts on a row — the TouchSensor's press-and-hold delay is
        // what arms a drag, so the row doesn't need to suppress all gestures.
        touchAction: 'manipulation',
      }}
      {...listeners}
    >
      {children}
    </div>
  )
}

/** The "N more" / "Show less" Apps-overflow toggle. Mirrors NavItem: a text row
 *  when expanded, an icon-only button with a portaled hover label when the
 *  sidebar is collapsed, so the collapse-to-more behavior works in both modes. */
function NavToggle({ collapsed, expanded, hiddenCount, onClick }: {
  collapsed: boolean; expanded: boolean; hiddenCount: number; onClick: () => void
}) {
  const { tip, tipOn, rowRef, showTip, hideTip, dismissTip } = useNavTip<HTMLButtonElement>(collapsed)
  // `hiddenCount === 0 && !expanded` happens when the only overflow item is the
  // active app (kept visible) — nothing is actually hidden, so the toggle just
  // offers to re-collapse rather than reveal "0 more".
  const showsCollapse = expanded || hiddenCount === 0
  const Icon = showsCollapse ? ChevronUp : MoreHorizontal
  const labelText = showsCollapse ? i18nT('app.show_less') : `${hiddenCount} more`
  const titleText = showsCollapse ? i18nT('app.show_fewer_apps') : `Show ${hiddenCount} more app${hiddenCount === 1 ? '' : 's'}`
  return (
    <button ref={rowRef}
      className="group/nav relative flex items-center rounded-md cursor-pointer text-sm font-medium whitespace-nowrap gap-2.5 py-2 pl-3 pr-3 transition-colors duration-200 text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none w-full"
      // Dismiss the hover label on activation, without the fade-out. Unlike a
      // NavItem (which stays put when clicked, so the pointer is still
      // legitimately over it), activating this toggle re-flows the Apps list and
      // moves the row out from under a stationary cursor — no mouseleave is
      // dispatched, so the flyout used to hang at the old coordinates until the
      // click's focus was lost. Fading it out is not enough either: the label
      // text flips on activation, so a fading ghost flashes the OPPOSITE label.
      // This runs after the focus a pointer press produces (focus precedes
      // click), so it also clears a label that focus had just re-armed.
      onClick={() => { dismissTip(); onClick() }}
      aria-expanded={expanded}
      aria-label={titleText}
      title={titleText}
      onMouseEnter={showTip}
      onMouseLeave={hideTip}
      // Surface the collapsed-mode hover label on keyboard focus too (button is
      // already focusable). Inert when expanded — showTip/hideTip gate on collapsed.
      onFocus={showTip}
      onBlur={hideTip}
    >
      <span className="w-4 h-4 flex items-center justify-center shrink-0 opacity-70"><Icon size={16} /></span>
      {/* Same reason as the nav-item label above: clipped by `whitespace-nowrap
          overflow-hidden`, so the full string lives on `aria-label` (not `title` — see the
          getByTitle collision note on the NavItem span above). */}
      {!collapsed && (
        <span aria-label={labelText} className="whitespace-nowrap overflow-hidden">
          {labelText}
        </span>
      )}
      {collapsed && tip && createPortal(
        <div
          className={`fixed flex items-center gap-2.5 pl-3 pr-3 rounded-md bg-card border border-border shadow-lg text-text text-sm font-medium z-[9999] pointer-events-none whitespace-nowrap transition-opacity duration-150 ${tipOn ? 'opacity-100' : 'opacity-0'}`}
          style={{ top: tip.top, left: tip.left, height: tip.height }}
        >
          <span className="w-4 h-4 flex items-center justify-center shrink-0"><Icon size={16} /></span>
          {labelText}
        </div>,
        document.body
      )}
    </button>
  )
}

function TasksRedirect() { const { search } = useLocation(); return <Navigate to={'/projects' + search} replace /> }
function ChatRedirect() { const { search } = useLocation(); return <Navigate to={'/chat' + search} replace /> }
function OrchestratedRedirect() { const { slug } = useParams(); const { search } = useLocation(); return <Navigate to={`/chat${slug ? '/' + slug : ''}${search}`} replace /> }

/** How long the notification sheet's exit animation runs before the portal is
 *  unmounted. MUST match the `nc-slide-out` duration in tailwind.config.js. */
const NC_CLOSE_MS = 240

/**
 * Topbar Notifications bell. The Notifications surface is `hiddenFromNav`, so
 * this is its entry point. Click opens an Activity Feed popover
 * (portaled to <body> to escape the topbar's backdrop-filter containing
 * block); clicking an item slides out a detail panel. The full page is
 * preserved at /notifications via the popover's "Open inbox" link.
 */
function NotificationsBellButton() {
  const navigate = useNavigate()
  const location = useLocation()
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const isMobile = useIsMobile()
  const [open, setOpen] = useState(false)
  // Exit animation: the sheet must stay mounted long enough to slide back out,
  // so dismissal flips `closing` (portal still rendered, sheet plays
  // nc-slide-out) and a timer does the real unmount. Must match the
  // animation duration in tailwind.config.js (`nc-slide-out`).
  const [closing, setClosing] = useState(false)
  const [selectedTs, setSelectedTs] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const bellRef = useRef<HTMLButtonElement>(null)
  // Badge counts attention-worthy rows only (RFC Phase 3): passive and
  // muted-channel (silenced) rows are excluded, mirroring the backend's
  // _unread_count semantics.
  const unacked = items.filter(n => !n.acked && n.priority !== 'passive' && !n.silenced)

  // RFC Phase 4: mirror the unread count onto the desktop dock/taskbar badge.
  useEffect(() => {
    const api = (window as Window & { electronAPI?: { setBadgeCount?: (n: number) => void } }).electronAPI
    api?.setBadgeCount?.(unacked.length)
  }, [unacked.length])
  const selected = selectedTs ? items.find(n => n.ts === selectedTs) || null : null

  // Single dismissal path: every close (bell toggle, outside click, Escape,
  // navigation, error fallback) goes through here so the sheet always gets its
  // slide-out instead of being torn down instantly.
  const closePanel = useCallback(() => {
    if (open) setClosing(true)
    setOpen(false)
    setSelectedTs(null)
  }, [open])

  const openPanel = useCallback(() => {
    setClosing(false)
    setOpen(true)
    setSelectedTs(null)
    recordEvent('notifications_open', { source: 'topbar' })
  }, [])

  // Unmount the portal once the exit animation has played. Reopening mid-flight
  // clears `closing` first, which cancels this timer via the cleanup.
  useEffect(() => {
    if (!closing) return
    const t = window.setTimeout(() => setClosing(false), NC_CLOSE_MS)
    return () => window.clearTimeout(t)
  }, [closing])

  // While the sheet plays its exit animation it is STILL in the DOM, so it must
  // stop being interactive in every modality — not just the pointer. `inert`
  // removes it from the tab order and the accessibility tree too, which is what
  // keeps a leaving panel from stealing a Tab stop or being announced. React 18
  // has no `inert` prop, so it rides through as a plain string attribute;
  // pointer-events-none stays as the floor for browsers without `inert`.
  const leavingProps = (closing
    ? { inert: '', 'aria-hidden': true }
    : {}) as HTMLAttributes<HTMLDivElement>
  // Desktop slides a fixed 400px sheet by px; mobile is full-width, so it needs
  // the percentage variant (see the keyframe comment in tailwind.config.js).
  const sheetAnim = closing
    ? (isMobile ? 'animate-nc-slide-out-full' : 'animate-nc-slide-out')
    : (isMobile ? 'animate-nc-slide-in-full' : 'animate-nc-slide-in')

  // Close popover when navigating (e.g. detail panel's "Go to Chat" buttons)
  const lastPathRef = useRef(location.pathname)
  useEffect(() => {
    if (location.pathname !== lastPathRef.current) {
      lastPathRef.current = location.pathname
      if (open) closePanel()
    }
  }, [location.pathname, open, closePanel])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null
      if (!target) return
      const inButton = containerRef.current?.contains(target) ?? false
      const inPopover = popoverRef.current?.contains(target) ?? false
      if (!inButton && !inPopover) {
        closePanel()
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedTs) setSelectedTs(null)
        // Escape is the keyboard dismissal, so return focus to the trigger.
        // The pointer paths deliberately do NOT do this: at pointerdown the
        // click's own focus move hasn't happened yet, so forcing focus here
        // would steal it from whatever the user just clicked.
        else { closePanel(); bellRef.current?.focus() }
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('pointerdown', onPointerDown); document.removeEventListener('keydown', onKey) }
  }, [open, selectedTs, closePanel])

  // Auto-mark-read when opening a notification's detail
  useEffect(() => {
    if (selected && !selected.acked) dispatch(ackNotification(selected.ts))
  }, [selected, dispatch])

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={bellRef}
        className={`flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 relative ${open ? 'text-accent' : 'text-muted hover:text-text'}`}
        onClick={() => { if (open) closePanel(); else openPanel() }}
        title={unacked.length > 0 ? `${unacked.length} notification${unacked.length === 1 ? '' : 's'}` : i18nT('app.notifications')}
        aria-label={i18nT('app.notifications')}
        aria-expanded={open}
      >
        <Bell size={15} />
        {unacked.length > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[16px] h-[16px] px-1 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center shadow-[0_0_8px_var(--accent-glow)]" aria-hidden="true">
            {unacked.length > 99 ? '99+' : unacked.length}
          </span>
        )}
      </button>
      {(open || closing) && createPortal(
        <div
          ref={popoverRef}
          className="fixed z-[60] pointer-events-none"
          style={isMobile ? { top: 48, bottom: 0, left: 0, right: 0 } : { top: 48, bottom: 0, right: 0, left: 12 }}
        >
          <ErrorBoundary
            scope="notifications-bell"
            fallback={
              <div {...leavingProps} className={`absolute top-0 right-0 ${closing ? 'pointer-events-none' : 'pointer-events-auto'} ${isMobile ? 'w-full' : 'w-[400px]'} glass-surface glass-static rounded-xl shadow-xl flex flex-col items-center justify-center gap-2 p-6 text-center`} style={{ maxHeight: 240 }}>
                <AlertTriangle size={20} className="text-warn" />
                <div className="text-[13px] font-semibold text-text-strong">{i18nT('app.notifications_failed_to_load')}</div>
                <button className="text-[12px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer" onClick={() => { closePanel(); navigate('/notifications') }}>{i18nT('app.open_the_full_inbox')}</button>
              </div>
            }
          >
          {/* Sheet — macOS Notification Center style: the panel itself is fully
              transparent (a tinted/blurred panel paints a hard edge at its left
              boundary — exactly what NC doesn't have). Every readable element
              (header, controls, notification rows) is its own floating
              material card instead. */}
          <div
            {...leavingProps}
            className={`absolute top-0 bottom-0 right-0 ${closing ? 'pointer-events-none' : 'pointer-events-auto'} ${isMobile ? 'w-full' : 'w-[400px]'} flex flex-col isolate ${sheetAnim}`}
          >
            {/* Column scrim — macOS NC dims/blurs only the strip behind the
                cards and it travels WITH the sheet. The layer extends 80px
                past the sheet's left edge and a mask fades both the dim and
                the blur to nothing there, so there is no hard boundary.
                -z-10 + isolate on the sheet keeps it behind the cards without
                forming a backdrop root (isolation is not a root trigger, so
                the cards' own backdrop-blur still samples the page). */}
            <div
              aria-hidden="true"
              className="absolute inset-y-0 -left-20 right-0 -z-10 pointer-events-none bg-black/[.03] [mask-image:linear-gradient(to_right,transparent,black_80px)] [-webkit-mask-image:linear-gradient(to_right,transparent,black_80px)]"
            />
            <div className="flex-1 min-h-0 px-3 py-2 flex flex-col">
              <NotificationFeed
                variant="mac"
                header={
                  <div className="flex items-center px-1 pb-1.5">
                    <span className="text-[14px] font-bold text-text-strong">{i18nT('app.notifications')}</span>
                  </div>
                }
                footer={
                  <div className="flex justify-end px-1 pb-1">
                    <button
                      className="text-[12px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer"
                      onClick={() => { closePanel(); navigate('/notifications') }}
                    >
                      {i18nT('app.open_inbox')}
                    </button>
                  </div>
                }
                selectedTs={selectedTs}
                onSelect={n => setSelectedTs(n.ts)}
              />
            </div>
          </div>
          {/* Detail panel — overlays feed on mobile, sits beside it on desktop.
              Rendered plainly (no AnimatePresence): an exit animation here races
              the portal teardown when the popover closes and throws removeChild. */}
          {selected && (
            <div
              className={`absolute top-0 bottom-0 pointer-events-auto ${isMobile ? 'left-0 right-0' : 'left-0 right-[408px]'} bg-card border border-border rounded-xl shadow-xl overflow-hidden`}
            >
              <NotificationDetailPanel
                n={selected}
                onClose={() => setSelectedTs(null)}
              />
            </div>
          )}
          </ErrorBoundary>
        </div>,
        document.body
      )}
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
  const { connected, updateProgress } = useAppSelector(s => s.dashboard)
  // Gateway (web) update flag OR desktop updater availability (mirrored from
  // Electron update-state by useUpdateSubscription) -- both light the same
  // Settings nav dot below.
  const updateAvailable = useAppSelector(s => s.dashboard.status?.update_available || s.dashboard.desktopUpdateAvailable)
  const version = useAppSelector(s => s.dashboard.status?.version) || '—'
  // Track whether the session-expired auth banner is currently injected by
  // api/client.ts. When auth is the real reason the gateway is unreachable,
  // the red top-banner already tells the user what to do (paste a fresh
  // `kirocrew token` URL) -- showing the loud pulsing "Offline" pill on top
  // of that just stacks two banners arguing about the same root cause. So
  // when authRequired is true, we suppress the offline pill in the top bar;
  // auth banner is the single canonical signal. `isAuthBannerShown()` seeds
  // initial state in case the banner was injected before App mounted (e.g.
  // a 403 fired during the very first /api/status before React hydrated).
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
  // Autopilot (slot-based), Notifications (notifications slice), Secretary
  // (attention slice), etc. App badges (dynamic, via `mc:app:badge` and the
  // global-approvals query below) are added below since they live outside
  // the Redux store and outside the registry.
  const builtinAttention = useAppSelector(selectAllSurfacesAttention)
  // Global approvals (project task-gates) — sourced from React Query, not
  // Redux, so it can't go through `selectAllSurfacesAttention` directly.
  // Routed through `appBadges` (the existing dynamic-app channel) so the
  // Projects nav item picks it up via `NavBadge`'s app-badge fallback path.
  const { data: pendingApprovals = [] } = useQuery({
    queryKey: ['global-approvals'],
    queryFn: () => api.approvals(),
    staleTime: 0,
    refetchInterval: 30_000,
  })
  const approvalCount = pendingApprovals.filter((a: { id?: string }) => a.id?.startsWith('task-gate-')).length
  const { data: terminalConfig } = useQuery({
    queryKey: ['terminal-enabled'],
    queryFn: async () => {
      const r = await fetch('/api/terminal/sessions')
      // Default-on: the terminal is enabled unless the server explicitly says
      // otherwise. A transient/auth-timing failure of this probe must NOT hide
      // an enabled terminal by falling back to {enabled:false}, which with
      // staleTime would keep the panel hidden for 60s.
      if (!r.ok) return { enabled: true }
      return r.json()
    },
    staleTime: 60_000,
  })
  // Hide only on an explicit opt-out (dashboard.terminal.enabled=false).
  // While the probe is loading (terminalConfig undefined) the terminal shows,
  // so there is no hidden-until-fetch-resolves flash.
  const terminalEnabled = terminalConfig?.enabled !== false
  useEffect(() => { setTerminalEnabledFlag(terminalEnabled) }, [terminalEnabled])
  // True while the terminal panel lives in its own popped-out window: the
  // docked panel is suppressed here and the sidebar toggle focuses that
  // window instead of opening an (empty-handed) panel.
  const terminalPoppedOut = useTerminalPoppedOut()
  // Only the `open` flag, not the whole store — the panel's height changes on
  // every mousemove during a grip-drag, and a primitive snapshot lets
  // useSyncExternalStore's Object.is check skip those re-renders of App.
  const bottomTerminalOpen = useBottomTerminalOpen()
  const navigate = useNavigate()

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
        switchSlot: (slotKey) => { dispatch(switchSlot(slotKey)) },
      }),
    )
  }, [isPopout, isEmbed, navigate, dispatch])

  // Publish the router navigator for the error → agent hand-off. AskAgentButton
  // is deliberately hook-free (its callers include ErrorBoundary fallbacks, where
  // router context may be what threw), so it navigates through this seam and
  // falls back to a full page load when nothing is installed.
  //
  // Popout and embed windows never register, for the same reason the nav-intent
  // handler above skips them: routing THAT window to /chat would replace the
  // surface the user deliberately popped out (an artifact editor renders error
  // banners of its own). They fall through to the hard-nav path instead.
  useEffect(() => {
    if (isPopout || isEmbed) return
    installSoftNavigate(navigate)
    return () => installSoftNavigate(null)
  }, [isPopout, isEmbed, navigate])

  const {
    colorTheme,
    theme: resolvedMode,
    onboarded,
    importOnboarded,
    privacyAcked,
    themeBootReady,
    markOnboarded,
    markImportOnboarded,
    markPrivacyAcked,
  } = useTheme()
  // The E2E Playwright suite depends on this onboarding gate: playwright/auth.setup.ts
  // seeds localStorage['mc-onboarded']='1' so the first-run "Choose your look" modal
  // never overlays the shell and intercepts every spec's interactions. If this flag is
  // renamed or the modal moves off localStorage, update auth.setup.ts to match.
  const locallyImportOnboarded =
    !!localStorage.getItem('mc-import-onboarded') || !!localStorage.getItem('mc-onboarded')
  // Mirrors `privacyAcked`'s own seed in useTheme. The tour's seed below MUST
  // consult it: a tree whose import chapter was completed by a build that
  // predates the Privacy chapter has `mc-import-onboarded` set and no
  // `mc-privacy-acked`, and seeding the tour open on that alone would put
  // Customize on screen ahead of Privacy until theme boot resolves — and its
  // "Done" would end first run from there. Same formula as the derive effect.
  const locallyPrivacyAcked =
    !!localStorage.getItem('mc-privacy-acked') || !!localStorage.getItem('mc-onboarded')
  const [showAgentImport, setShowAgentImport] = useState(false)
  const [showPrivacy, setShowPrivacy] = useState(false)
  const [showOnboarding, setShowOnboarding] = useState(
    () => locallyImportOnboarded && locallyPrivacyAcked && !localStorage.getItem('mc-onboarded'),
  )
  const continueTourAfterImport = useRef(false)
  // Where the mandatory Privacy chapter leads. 'customize' hands off to the
  // onboarding tour (the normal chapter order); 'finish' ends first run right
  // there, which is what "Skip all" from Import setup means — the user still has
  // to pass through Privacy, but nothing follows it.
  const privacyExit = useRef<'customize' | 'finish'>('customize')
  // The ONLY way the tour chapter ends first run — deliberately shared by BOTH
  // its exits ("Done" and every skip: "Skip all", a popover Skip, Escape).
  // Privacy is mandatory, so no exit may mark onboarding complete while it is
  // unacknowledged; handing the two props one function is what makes that
  // symmetric by construction instead of by two closures agreeing. In the normal
  // chapter order Privacy is already behind the user here and this just ends
  // first run; the branch is what holds the mandate for a tree whose import
  // chapter predates the Privacy chapter.
  const endFirstRun = useCallback(() => {
    setShowOnboarding(false)
    if (!privacyAcked) {
      privacyExit.current = 'finish'
      setShowPrivacy(true)
      return
    }
    markOnboarded()
  }, [privacyAcked, markOnboarded])
  // Dismiss onboarding when server reports user is already onboarded
  // (handles the race: boot fetch completes after useState initializer ran).
  useEffect(() => { if (onboarded) setShowOnboarding(false) }, [onboarded])
  // Seeds — and re-derives — which first-run chapter is open from the three
  // completion flags. Chapter order is Import setup → Privacy → Customize/tour,
  // so each chapter opens only once its predecessor is marked done. Runs on
  // every flag change (not just boot) so the hand-offs below and this effect
  // can never disagree about what should be on screen.
  useEffect(() => {
    if (!themeBootReady) return
    setShowAgentImport(!importOnboarded)
    setShowPrivacy(importOnboarded && !privacyAcked)
    setShowOnboarding(importOnboarded && privacyAcked && !onboarded)
  }, [importOnboarded, privacyAcked, onboarded, themeBootReady])
  useEffect(() => {
    const replay = (event: Event) => {
      continueTourAfterImport.current =
        !!(event as CustomEvent<{ continueOnboarding?: boolean }>).detail?.continueOnboarding
      setShowOnboarding(false)
      setShowAgentImport(true)
    }
    window.addEventListener('mc-start-import', replay)
    return () => window.removeEventListener('mc-start-import', replay)
  }, [])
  // Capture Electron update lifecycle events app-wide so UpdateModal fires on
  // any page, not just after the user has opened Settings > About.
  useUpdateSubscription()
  const { botName: _botName, avatar: _avatar } = useBranding()

  // Per-theme branding (bot name, logo, favicon, top-bar decoration, overlays,
  // activation side-effect) comes from the theme-branding registry so the shell
  // never hard-codes `colorTheme === 'x' ? …` chains. Falls back to the
  // configured branding when the active theme registers none.
  const branding = getThemeBranding(colorTheme)
  const botName = branding?.botName ?? _botName
  const avatar = branding?.logo ?? _avatar
  // Swap the browser favicon to the active theme's brand mark (falls back to
  // the default /logo.png when the theme declares none). The core has no
  // per-theme favicon of its own; this drives registered theme brandings.
  useEffect(() => {
    const link = document.querySelector<HTMLLinkElement>('link[rel~="icon"]')
    if (link) link.href = branding?.favicon ?? '/logo.png'
  }, [branding])
  // Fire a theme's activation side-effect (e.g. a boot chime) on each off→on
  // switch to that theme. Generic via the branding registry; the effect itself
  // is owned by the theme's registration, so the core stays silent by default.
  const prevColorThemeRef = useRef<string | null>(null)
  useEffect(() => {
    if (colorTheme !== prevColorThemeRef.current) {
      prevColorThemeRef.current = colorTheme
      // Guarded: a registered theme's activation side-effect (owned by the
      // downstream edition) must not crash the effect / shell if it throws.
      try {
        branding?.onActivate?.()
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[themeBranding] onActivate threw', err)
      }
    }
  }, [colorTheme]) // eslint-disable-line react-hooks/exhaustive-deps
  useRumPageView()
  useNotificationSound()
  const [navCollapsed, setNavCollapsed] = useState(() => localStorage.getItem('mc-nav') === '1')
  // Preview focus (expand) mode from the Web Preview tab: force the left nav
  // collapsed while active (restored automatically when it turns off, since we
  // OR a transient flag rather than mutating navCollapsed).
  const [previewFocused, setPreviewFocused] = useState(false)
  useEffect(() => {
    const onFocus = (e: Event) => setPreviewFocused(!!(e as CustomEvent<{ focused?: boolean }>).detail?.focused)
    window.addEventListener(PREVIEW_FOCUS_EVENT, onFocus)
    return () => window.removeEventListener(PREVIEW_FOCUS_EVENT, onFocus)
  }, [])
  const isMobile = useIsMobile()
  // Multi-instance: which instance fills the pane below the tab bar. null = Local
  // (the native dashboard); a non-null id means a remote instance's embedded
  // dashboard is shown instead, so the Local pane is hidden (not unmounted).
  const activeInstanceId = useAppSelector(s => s.instances.activeId)
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
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  // Dynamic app nav items — all apps (builtin + installed) with UI pages
  const [appNavItems, setAppNavItems] = useState<Array<{ path: string; id: string; label: string; group: string; icon: React.ReactElement }>>([])
  const [appNavOrder, setAppNavOrder] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem('mc-app-nav-order') || '[]') } catch { return [] } })
  // Apps nav reorder is dnd-kit sortable (mirrors QueueStack): rows reflow to
  // open a gap as you drag, and a DragOverlay renders the floating ghost.
  // activeAppDragId tracks the app being dragged, for the overlay + source dim.
  const [activeAppDragId, setActiveAppDragId] = useState<string | null>(null)
  // Split mouse/touch sensors so touch can both scroll AND drag:
  //  - MouseSensor: 8px distance lets a plain click reach NavItem navigation;
  //    only a deliberate drag past the threshold starts a reorder (desktop).
  //  - TouchSensor: 250ms press-and-hold (5px tolerance) arms a drag, so a
  //    quick finger-swipe still scrolls the nav rail natively and only a
  //    deliberate hold starts a reorder. A single PointerSensor can't do this:
  //    its `touch-action: none` requirement steals every swipe for dragging.
  const appDndSensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 5 } }),
  )
  // Collapse a long Apps list behind a "N more" toggle so the nav can't grow
  // unbounded. Above APPS_NAV_LIMIT visible entries the overflow is hidden until
  // the user expands (persisted).
  const APPS_NAV_LIMIT = 6
  const [appsExpanded, setAppsExpanded] = useState(() => localStorage.getItem('mc-apps-expanded') === '1')
  const toggleAppsExpanded = useCallback(() => setAppsExpanded(v => { const next = !v; safeSetItem('mc-apps-expanded', next ? '1' : '0'); return next }), [])
  const sortedAppGroup = useMemo(() => {
    const items = [...NAV_ITEMS.filter(n => n.group === 'Apps'), ...appNavItems]
    if (appNavOrder.length === 0) return items
    const orderMap = new Map(appNavOrder.map((id, i) => [id, i]))
    return items.sort((a, b) => (orderMap.get(a.id) ?? 999) - (orderMap.get(b.id) ?? 999))
  }, [appNavItems, appNavOrder])
  const handleAppDragStart = useCallback((e: DragStartEvent) => setActiveAppDragId(e.active.id as string), [])
  const handleAppDragEnd = useCallback((e: DragEndEvent) => {
    setActiveAppDragId(null)
    const { active, over } = e
    if (!over || active.id === over.id) return
    const ids = sortedAppGroup.map(n => n.id)
    const from = ids.indexOf(active.id as string)
    const to = ids.indexOf(over.id as string)
    if (from < 0 || to < 0) return
    const next = arrayMove(ids, from, to)
    setAppNavOrder(next)
    safeSetItem('mc-app-nav-order', JSON.stringify(next))
  }, [sortedAppGroup])
  // Drag cancel (e.g. Escape) fires onDragCancel, NOT onDragEnd — clear the
  // active id here too, else the source row stays dimmed and the overlay ghost
  // lingers. Mirrors ChatSidebar's handleSidebarDragCancel.
  const handleAppDragCancel = useCallback(() => setActiveAppDragId(null), [])
  const appNavRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const refreshAppNav = useCallback((attempt = 0) => {
    // Cancel any pending retry up-front so external triggers (the reconnect
    // effect, the mc:apps-changed handler) or a just-fired retry can never run
    // overlapping fetch chains — exactly one chain is ever active.
    if (appNavRetryRef.current) { clearTimeout(appNavRetryRef.current); appNavRetryRef.current = null }
    api.listApps()
      .then((apps: AppListEntry[]) => {
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
            const baseIcon = customIconUrl
              ? <AppIcon iconUrl={customIconUrl} icon={iconName} size={16} />
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
            }]
          })
        setAppNavItems(items)
        dispatch(setEnabledAppIds(items.map(i => i.id)))
      })
      .catch(() => {
        // A transient failure (e.g. the gateway mid-restart right after a
        // `kirocrew update`, or the cold apps-dir scan) used to be swallowed
        // here, leaving the Apps rail empty until a manual reload or an app
        // enable/disable. Retry with bounded exponential backoff so it
        // self-heals. The reconnect effect below covers the WS-drop case.
        if (attempt >= APP_NAV_MAX_RETRIES) return
        appNavRetryRef.current = setTimeout(() => refreshAppNav(attempt + 1), APP_NAV_RETRY_BASE_MS * 2 ** attempt)
      })
  }, [dispatch])
  useEffect(() => {
    refreshAppNav()
    return () => { if (appNavRetryRef.current) clearTimeout(appNavRetryRef.current) }
  }, [refreshAppNav])
  useEffect(() => {
    const handler = () => refreshAppNav()
    window.addEventListener('mc:apps-changed', handler)
    return () => window.removeEventListener('mc:apps-changed', handler)
  }, [refreshAppNav])
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

  // App badge counts — apps call useNavBadge() to push counts
  const [appBadges, setAppBadges] = useState<Record<string, number>>({})
  useEffect(() => {
    const handler = (e: Event) => {
      const { appName, count } = (e as CustomEvent).detail || {}
      if (appName) setAppBadges(prev => ({ ...prev, [appName]: count || 0 }))
    }
    window.addEventListener('mc:app:badge', handler)
    return () => window.removeEventListener('mc:app:badge', handler)
  }, [])
  // Surface the global-approvals count on the Projects nav item via the same
  // `appBadges` channel external apps use. The `projects` surface declares no
  // slotMode/unreadSelector, so `NavBadge` falls back to `appBadges['projects']`.
  useEffect(() => {
    setAppBadges(prev => prev.projects === approvalCount ? prev : { ...prev, projects: approvalCount })
  }, [approvalCount])

  const [updating, setUpdating] = useState(false)
  const [showUpdateModal, setShowUpdateModal] = useState(false)
  const [kiroUsageOpen, setKiroUsageOpen] = useState(false)
  const [changes, setChanges] = useState('')
  const [showChangelog, setShowChangelog] = useState(false)
  const [autoUpdate, setAutoUpdate] = useState(true)
  const [fullChangelog, setFullChangelog] = useState('')
  const [showFull, setShowFull] = useState(false)
  const [devMode, setDevMode] = useState(() => localStorage.getItem('mc-dev-mode') === '1')
  const [devPageSeen, setDevPageSeen] = useState(true)
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const toggleShortcutsModal = useCallback(() => setShortcutsOpen(p => !p), [])
  // Search Everywhere command palette — global double-Shift / ⌘K
  // trigger + open state. Mounted once below at the app shell.
  const commandPalette = useCommandPalette()
  const newChatMutation = useMutation({
    mutationFn: () => dispatch(createSlot(undefined)).unwrap(),
    onSuccess: () => {
      navigate('/chat')
      requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus())
    },
  })
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const { agents: installedAgents, defaultAgent } = useAgents(refreshTrigger)
  const queryClient = useQueryClient()
  const provider = useProvider()
  useKeyboardShortcuts({ onToggleShortcutsModal: toggleShortcutsModal, onNewChat: () => newChatMutation.mutate(), disabled: shortcutsOpen,
    onCycleAgent: () => {
      const slots = store.getState().dashboard.slots
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot || installedAgents.length === 0) return
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const currentAgent = currentSlot?.agent || defaultAgent
      const idx = installedAgents.findIndex((a: { name: string }) => a.name === currentAgent)
      const nextIdx = (idx + 1) % installedAgents.length
      api.chatSlotAgent(activeSlot, installedAgents[nextIdx].name)
    },
    onCyclePrevAgent: () => {
      const slots = store.getState().dashboard.slots
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot || installedAgents.length === 0) return
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const currentAgent = currentSlot?.agent || defaultAgent
      const idx = installedAgents.findIndex((a: { name: string }) => a.name === currentAgent)
      const prevIdx = (idx - 1 + installedAgents.length) % installedAgents.length
      api.chatSlotAgent(activeSlot, installedAgents[prevIdx].name)
    },
    onCycleReasoningEffort: () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const current = currentSlot?.reasoning_effort || ''
      const idx = REASONING_EFFORT_LEVELS.indexOf(current)
      const nextIdx = (idx + 1) % REASONING_EFFORT_LEVELS.length
      api.chatSlotReasoningEffort(activeSlot, REASONING_EFFORT_LEVELS[nextIdx])
    },
    onCyclePrevReasoningEffort: () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const current = currentSlot?.reasoning_effort || ''
      const idx = REASONING_EFFORT_LEVELS.indexOf(current)
      const prevIdx = (idx - 1 + REASONING_EFFORT_LEVELS.length) % REASONING_EFFORT_LEVELS.length
      api.chatSlotReasoningEffort(activeSlot, REASONING_EFFORT_LEVELS[prevIdx])
    },
    onCycleApprovalMode: () => {
      const state = store.getState()
      const activeSlot = state.chat.activeSlot
      if (!activeSlot) return
      const current = state.dashboard.approvalMode || 'normal'
      const idx = APPROVAL_MODE_LEVELS.indexOf(current)
      const next = APPROVAL_MODE_LEVELS[(idx + 1) % APPROVAL_MODE_LEVELS.length]
      store.dispatch(changeApprovalMode({ mode: next, slot: activeSlot }))
    },
    onCyclePrevApprovalMode: () => {
      const state = store.getState()
      const activeSlot = state.chat.activeSlot
      if (!activeSlot) return
      const current = state.dashboard.approvalMode || 'normal'
      const idx = APPROVAL_MODE_LEVELS.indexOf(current)
      const prev = APPROVAL_MODE_LEVELS[(idx - 1 + APPROVAL_MODE_LEVELS.length) % APPROVAL_MODE_LEVELS.length]
      store.dispatch(changeApprovalMode({ mode: prev, slot: activeSlot }))
    },
    onCycleModel: () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const models = queryClient.getQueryData<{ name: string }[]>(['available-models', provider.id])
      if (!models || models.length === 0) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const currentModel = currentSlot?.model || ''
      const idx = currentModel ? models.findIndex(m => m.name === currentModel) : -1
      const nextIdx = (idx + 1) % models.length
      api.chatSlotModel(activeSlot, models[nextIdx].name)
    },
    onCyclePrevModel: () => {
      const activeSlot = store.getState().chat.activeSlot
      if (!activeSlot) return
      const models = queryClient.getQueryData<{ name: string }[]>(['available-models', provider.id])
      if (!models || models.length === 0) return
      const slots = store.getState().dashboard.slots
      const currentSlot = slots.find((s: { key: string }) => s.key === activeSlot)
      const currentModel = currentSlot?.model || ''
      const idx = currentModel ? models.findIndex(m => m.name === currentModel) : -1
      const prevIdx = idx <= 0 ? models.length - 1 : idx - 1
      api.chatSlotModel(activeSlot, models[prevIdx].name)
    },
  })
  // Cmd+1..9 (⌘ mac / Ctrl win-linux) switches instance panes: 1=Local,
  // 2=first remote, … — matching the InstanceTabBar left-to-right tab order.
  // Registered here (once) rather than in InstanceTabBar, which can mount more
  // than once (strip + inline header copies).
  useInstanceShortcuts()

  // Kiro CLI monthly credit usage. /api/sessions/usage TRIGGERS the background
  // `kiro-cli /usage` fetch AND returns the cached result, so the pill is
  // self-sufficient on any page. Month-to-date total = credits_used, which the
  // backend already sets to the TRUE total (covered + overage). Do NOT add
  // credits_covered on top — that double-counts the in-plan portion and is the
  // bug that rendered a capped 10K plan as "20.0K". Returns null until the
  // background cache warms.
  const { data: kiroUsage } = useQuery({
    queryKey: ['kiro-usage'],
    queryFn: () => api.sessionsUsage().then(d => {
      const u = d?.usage || {}
      // Kiro credit plan (internal) — the only usage this pill surfaces.
      // Number.isFinite guards against a stray NaN ever rendering as "NaN / NaN".
      if (Number.isFinite(u.credits_plan)) {
        const limit = Math.round(u.credits_plan)
        // credits_used is the real total (backend sets it to covered + overage);
        // fall back to 0 (not the limit) when the source omits it, so a partial
        // payload never implies a maxed plan.
        const used = Number.isFinite(u.credits_used) ? Math.round(u.credits_used) : 0
        const overage = Number.isFinite(u.credits_overage) ? u.credits_overage : Math.max(0, used - limit)
        // Bonus / welcome-credit pool (spent before the plan). Present only when
        // the backend surfaced it; when absent the pill/modal behave exactly as
        // before (plan-only).
        const bonus = (Number.isFinite(u.bonus_limit) && u.bonus_limit > 0)
          ? {
              used: Number.isFinite(u.bonus_used) ? Math.round(u.bonus_used) : 0,
              limit: Math.round(u.bonus_limit),
              label: (typeof u.bonus_label === 'string' && u.bonus_label) ? u.bonus_label : i18nT('app.bonus_credits'),
              expiresLabel: typeof u.bonus_expires_label === 'string' ? u.bonus_expires_label : undefined,
            }
          : undefined
        const str = (v: unknown) => (typeof v === 'string' && v ? v : undefined)
        return { used, limit, overage, resets: u.resets, plan: u.plan, costUsd: u.cost_usd, overageRate: u.overage_rate, bonus, stale: u.stale === true, account: str(u.account), email: str(u.email), accountType: str(u.account_type), startUrl: str(u.start_url) }
      }
      // Non-Kiro provider (kiro-cli absent) -> hide. Empty cache (Kiro warming) -> spinner.
      if (u.available === false) return 'none' as const
      return null
    }),
    refetchInterval: 30_000,
  })
  // Auto-close the details modal if usage resolves to unavailable — the pill
  // hides in that case, so a modal opened during loading would otherwise be stuck.
  useEffect(() => {
    if (kiroUsage === 'none') setKiroUsageOpen(false)
  }, [kiroUsage])
  const [metricsOpen, setMetricsOpen] = useState(() => localStorage.getItem('mc-topbar-metrics') === '1')
  // Readout capsule collapse: clicking the connection dot folds the capsule
  // down to just the dot; clicking again restores the full readout.
  const [capsuleCollapsed, setCapsuleCollapsed] = usePersistedBool('mc-topbar-capsule-collapsed', false)
  const [capsuleLayoutPulse, setCapsuleLayoutPulse] = useState(false)
  const capsulePulseTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const pulseCapsuleLayout = useCallback(() => {
    setCapsuleLayoutPulse(true)
    clearTimeout(capsulePulseTimer.current)
    capsulePulseTimer.current = setTimeout(() => setCapsuleLayoutPulse(false), 350)
  }, [])
  useEffect(() => () => clearTimeout(capsulePulseTimer.current), [])
  // macOS fullscreen hides the native traffic lights, so the header's 84px
  // clearance inset drops while fullscreen (mac-fullscreen class on the root).
  const [macFullscreen, setMacFullscreen] = useState(false)
  useEffect(() => {
    if (!isMacElectron) return
    const api = (window as { electronAPI?: { onFullScreenChanged?: (cb: (fs: boolean) => void) => () => void } }).electronAPI
    return api?.onFullScreenChanged?.(setMacFullscreen)
  }, [])
  // Native traffic lights sit over the consolidated 42px header, so there is no
  // separate strip inset to relay to Electron — positionTrafficLights centers on
  // the header height directly. Remote panes get their own inset via `macInset`.
  const macInset = isMacElectron && !macFullscreen
  const { data: sysMetrics, isError: sysMetricsError, dataUpdatedAt: sysMetricsUpdatedAt } = useQuery({ queryKey: ['system-metrics'], queryFn: () => api.system().then(d => ({ memUsed: d.mem_used_gb, memTotal: d.mem_total_gb, cpuPct: d.cpu_pct, diskTotal: d.disk_total_gb, diskFree: d.disk_free_gb })), refetchInterval: metricsOpen ? 30_000 : false, enabled: metricsOpen })
  // Tick every 10s while widget is open so `sysMetricsStale` re-evaluates even when the query stops refetching (backgrounded tab, network drop).
  const [, setStaleTick] = useState(0)
  useEffect(() => {
    if (!metricsOpen) return
    const id = setInterval(() => setStaleTick(t => t + 1), 10_000)
    return () => clearInterval(id)
  }, [metricsOpen])
  // Consider metrics stale if last successful fetch was > 90s ago (3x the 30s poll interval) while the widget is open.
  const sysMetricsStale = metricsOpen && (sysMetricsError || (sysMetricsUpdatedAt > 0 && Date.now() - sysMetricsUpdatedAt > 90_000))

  // Listen for dev mode changes from Settings > Developer
  useEffect(() => {
    const handler = (e: Event) => {
      const enabled = (e as CustomEvent).detail
      setDevMode(enabled)
      if (enabled) setDevPageSeen(false)
    }
    window.addEventListener('mc-dev-mode-changed', handler)
    return () => window.removeEventListener('mc-dev-mode-changed', handler)
  }, [])
  // Sync dev-mode state to Electron on startup (so View > DevTools menu is correct)
  useEffect(() => {
    const electronAPI = (window as Window & { electronAPI?: { setDevMode?: (v: boolean) => void } }).electronAPI
    electronAPI?.setDevMode?.(devMode)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // Native app-menu navigation (Settings…, About): the Electron main process
  // sends an in-app path; route to it. Accept only plain absolute app paths —
  // rejects protocol-relative ("//host") and external URLs by construction.
  useEffect(() => {
    const electronAPI = (window as Window & { electronAPI?: { onNavigate?: (cb: (path: string) => void) => () => void } }).electronAPI
    if (!electronAPI?.onNavigate) return
    return electronAPI.onNavigate(path => {
      if (typeof path === 'string' && /^\/(?!\/)/.test(path)) navigate(path)
    })
  }, [navigate])
  // Dismiss the dev-page notification dot once the user visits /developer
  useEffect(() => {
    if (location.pathname === '/developer') setDevPageSeen(true)
  }, [location.pathname])

  useEffect(() => {
    dispatch(fetchSlots()).then(action => {
      // Run localStorage GC after we know which sessions are alive
      if (fetchSlots.fulfilled.match(action)) {
        const liveIds = new Set((action.payload as Array<{ key: string }>).map(s => s.key))
        gcOrphanedStorage(liveIds)
      }
    })
    dispatch(fetchNotifications())
    // Fetch status immediately to sync YOLO state (WS status push is periodic)
    api.status().then(s => { dispatch(sseStatus(s)); recordSessionStart(s) }).catch(() => {})
  }, [dispatch])
  const { subscribeLogs, subscribeSubagents, forceReconnect } = useWebSocket()
  useDashboardHealthProbe(forceReconnect)

  // Close update modal when progress clears (simulation complete or cancelled)
  useEffect(() => {
    if (!updateProgress && (updating || showUpdateModal)) {
      setUpdating(false)
      setShowUpdateModal(false)
    }
  }, [updateProgress]) // eslint-disable-line react-hooks/exhaustive-deps

  // Show changelog on first load after version change (auto-update)
  useEffect(() => {
    if (!version || version === '—') return
    const lastSeen = localStorage.getItem('mc-last-version')
    if (lastSeen === version) return
    // First visit — no baseline to diff, just record current version
    if (!lastSeen) { safeSetItem('mc-last-version', version); return }
    // Version changed — show only new entries since lastSeen
    api.changelog().then(d => {
      if (!d.content) return
      const lines = d.content.split('\n')
      const filtered: string[] = []
      let include = false
      for (const line of lines) {
        if (line.startsWith('## [')) {
          const v = line.match(/## \[([^\]]+)\]/)?.[1]
          if (v && lastSeen && v === lastSeen) break
          include = true
        }
        if (include) filtered.push(line)
      }
      const text = filtered.join('\n').trim()
      if (text) { setChanges(text); setShowChangelog(true) }
    }).catch(() => {}).finally(() => safeSetItem('mc-last-version', version))
  }, [version])  

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

  const [updateError, setUpdateError] = useState('')
  // Nav-rail "Report issue" → the shared diagnostics flow. Held at shell level
  // (not in the rail) so the modal is not unmounted when the rail collapses.
  const [reportProblemOpen, setReportProblemOpen] = useState(false)

  const handleUpdate = useCallback(async () => {
    setShowChangelog(false)
    setUpdateError('')
    setUpdating(true)
    try {
      await api.applyUpdate()
    } catch (err: unknown) {
      setUpdating(false)
      let msg = i18nT('app.update_failed_2')
      const errMessage = err instanceof Error ? err.message : ''
      try {
        const parsed = JSON.parse(errMessage || '')
        if (parsed.error) msg = parsed.error
      } catch { if (errMessage) msg = errMessage }
      setUpdateError(msg)
    }
  }, [])

  const requestFeature = useCallback(async () => {
    // Resolve skill availability in the dashboard so the agent never needs
    // to probe the filesystem (which would trigger a tool-approval prompt).
    let msg = FEATURE_REQUEST_PROMPT_FALLBACK
    try {
      const skills: { name: string }[] = await api.skills()
      if (skills.some(s => s.name === 'feature-request')) {
        msg = FEATURE_REQUEST_PROMPT_WITH_SKILL
      }
    } catch { /* skill list unavailable — use the self-contained fallback */ }
    const result = await dispatch(createSlot(undefined)).unwrap()
    const slot = result.key
    navigate('/chat')
    dispatch(appendMessage({ role: 'user', content: i18nT('app.i_d_like_to_request_a_feature'), cls: '', ts: new Date().toISOString() }))
    dispatch(setSlotRunning(true))
    try {
      await api.sendChat(msg, slot, colorTheme)
    } catch { /* WS will handle response */ }
  }, [dispatch, navigate, colorTheme])

  const toggleNav = () => {
    if (isMobile) { setMobileNavOpen(p => !p) }
    else {
      setNavCollapsed(prev => { const next = !prev; safeSetItem('mc-nav', next ? '1' : '0'); return next })
    }
  }
  // Close mobile nav on route change
  useEffect(() => { if (isMobile) setMobileNavOpen(false) }, [location.pathname]) // eslint-disable-line react-hooks/exhaustive-deps
  // Reset mobile nav state when leaving mobile viewport
  useEffect(() => { if (!isMobile) setMobileNavOpen(false) }, [isMobile])
  const effectiveCollapsed = (navCollapsed || previewFocused) && !isMobile
  // Publish the rail track so consumers outside the shell can size against the
  // space actually left for content — ChatPage's activity panel decides
  // beside-vs-fill from it. Kept in sync with the gridTemplateColumns value
  // below; railWidthFor is the single source for both.
  useEffect(() => {
    setRailWidth(railWidthFor({ isMobile, collapsed: effectiveCollapsed }))
  }, [isMobile, effectiveCollapsed])
  const topbarBrandRef = useRef<HTMLDivElement>(null)
  const topbarActionsRef = useRef<HTMLDivElement>(null)
  const [topbarSearchLayout, setTopbarSearchLayout] = useState({ gutter: 360, visible: true })
  useEffect(() => {
    if (isMobile) return
    const brand = topbarBrandRef.current
    const actions = topbarActionsRef.current
    if (!brand || !actions) return
    const update = () => {
      const brandWidth = brand.getBoundingClientRect().width
      const actionsWidth = actions.getBoundingClientRect().width
      if (brandWidth <= 0 || actionsWidth <= 0) return
      const next = calculateTopbarSearchLayout(brandWidth, actionsWidth, window.innerWidth)
      setTopbarSearchLayout(current => current.gutter === next.gutter && current.visible === next.visible ? current : next)
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(brand)
    observer.observe(actions)
    window.addEventListener('resize', update)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [isMobile])
  const closeMobileNav = isMobile ? () => setMobileNavOpen(false) : undefined
  const activePath = location.pathname
  const isChat = activePath === '/chat' || activePath.startsWith('/chat/') || activePath === '/'
  // /webhooks is a full-height rail-and-detail shell (like /capabilities), so it
  // owns its own scrolling and must not sit inside <main>'s scroll container.
  const needsFixedHeight = isChat || activePath === '/settings' || activePath === '/developer' || activePath === '/capabilities' || activePath === '/webhooks'

  // Render one standard nav row (used by the top-fixed mains, the Apps list,
  // and the bottom-fixed section). Active-state, mobile close, chat pin
  // toggle, and badge wiring are identical across sections.
  // `surfaceLabel` resolves `labelKey` against the active language at render
  // time; a surface with no key (app-contributed) falls back to its literal.
  const renderNavRow = (
    n: { path: string; id: string; label: string; labelKey?: string; icon: React.ReactNode },
  ) => (
    <NavItem
      navId={n.id}
      path={n.path}
      label={surfaceLabel(n)}
      icon={n.icon}
      active={n.path === '/apps' ? activePath === '/apps' : (activePath === n.path || activePath.startsWith(n.path + '/'))}
      collapsed={effectiveCollapsed}
      onClick={closeMobileNav}
      onClickOverride={isChat && (activePath === n.path || activePath.startsWith(n.path + '/')) ? () => window.dispatchEvent(new Event('toggle-pin-chat-sidebar')) : undefined}
      badge={<NavBadge navId={n.id} collapsed={effectiveCollapsed} appBadges={appBadges} />}
    />
  )

  return (
    <ZoomProvider>
    <WsContext.Provider value={{ subscribeLogs, subscribeSubagents, forceReconnect }}>
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
      <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col">
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
    <div className="h-screen w-screen flex flex-col overflow-hidden bg-bg">
      {/* Embedded remote panes receive their switcher model from the parent via
          this bridge (option B) — no-op in the top-level dashboard. */}
      <EmbeddedHostBridge />
      <div className="flex-1 min-h-0 relative">
      {/* Local pane: the native dashboard. Hidden (not unmounted) while a remote
          instance tab is active, so local state/websocket survive the switch. */}
      <div className="absolute inset-0" style={{ display: activeInstanceId === null ? 'block' : 'none' }}>
    <div
      data-testid="dashboard-shell"
      className={`relative z-[1] h-full grid ${shellEntered ? '' : 'animate-rise'} overflow-hidden bg-bg ${isMacElectron ? `mac-electron ${macFullscreen ? 'mac-fullscreen' : ''}` : ''} ${isWinElectron ? 'win-electron' : ''} ${isMobile ? 'grid-cols-[minmax(0,1fr)] grid-rows-[42px_minmax(0,1fr)]' : 'grid-rows-[42px_minmax(0,1fr)]'}`}
      // Retire the entrance animation once it has played, so re-showing this
      // pane cannot replay it. Guarded on BOTH the keyframe name and the event
      // target: `animationend` bubbles, and descendants (banners, cards) use
      // `animate-rise` too, so an unguarded handler would retire the shell's
      // entrance from an unrelated child's animation.
      onAnimationEnd={e => {
        if (e.target === e.currentTarget && e.animationName === 'rise') setShellEntered(true)
      }}
      style={{
        gridTemplateAreas: isMobile ? '"topbar" "content"' : '"topbar topbar topbar" "nav content actbar"',
        ...(!isMobile && {
          gridTemplateColumns: `${railWidthFor({ isMobile, collapsed: effectiveCollapsed })}px minmax(0,1fr) auto`,
          // Transition fires only when the template string itself changes (the
          // collapse toggle) — content-driven resizes of the auto track (e.g.
          // the Activity panel opening) don't alter the value, so keeping this
          // unconditional is safe and avoids the gated-pulse snap regression.
          transition: 'grid-template-columns 150ms cubic-bezier(0.2, 0, 0, 1)',
        }),
      }}
    >

      {/* Full-height activity bar slot: ChatPage portals its
          Activity panel here on desktop so it spans the window top-to-bottom
          instead of sitting below the header row. Empty (0 width) when the
          panel is closed or on non-chat routes. */}
      {!isMobile && <div id="activity-bar-slot" className="h-full min-h-0 min-w-0" style={{ gridArea: 'actbar' }} />}

      {/* Skip to content — visible only on focus for keyboard users */}
      <a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[9999] focus:px-4 focus:py-2 focus:rounded-lg focus:bg-accent focus:text-accent-fg focus:text-sm focus:font-medium">{i18nT('app.skip_to_content')}</a>

      {/* Topbar */}
      {/* stable theming hook — see website/docs/theming-contract.md */}
      <header className="topbar topbar-glass relative flex items-center pl-3 pr-3 z-[45]" style={{ gridArea: 'topbar' }}>
        {/* Left: mobile menu toggle + inline instance selector. The brand now
            lives in the sidebar (item 1.1). The selector reuses InstanceTabBar's
            visibility rule — it renders nothing unless >=1 remote instance
            exists, so the common single-instance header-left is empty (only the
            macOS traffic-light clearance remains). */}
        <div
          ref={topbarBrandRef}
          className={`relative flex items-center h-full shrink-0 gap-2 ${isMobile ? 'px-2' : ''}`}
        >
          {isMobile && (
            <button className="p-2 rounded-md bg-transparent border-none cursor-pointer text-muted hover:text-text shrink-0" onClick={toggleNav} aria-label={i18nT('app.open_menu')}>
              <Menu size={20} />
            </button>
          )}
          {!isMobile && <InstanceTabBar variant="inline" />}
        </div>
        {!isMobile && topbarSearchLayout.visible && (
          <button
            type="button"
            data-topbar-overlay
            onClick={commandPalette.openPalette}
            className="absolute h-7 px-3 rounded-md border border-border bg-card text-muted hover:text-text hover:border-border-hover transition-colors flex items-center justify-center gap-2 cursor-pointer shadow-none"
            style={{ left: '50vw', transform: 'translateX(-50%)', width: 'calc(33.3333vw - 40px)', minWidth: TOPBAR_SEARCH_MIN_WIDTH }}
            aria-label={i18nT('app.search_sessions_files_and_commands')}
            title={i18nT('app.search_everywhere_k')}
          >
            <span className="text-[13px] truncate min-w-0">{i18nT('app.k_search_for_anything')}</span>
          </button>
        )}
        {/* Theme decoration: the active theme's center top-bar element (e.g. a
            scanner sweep), chosen by resolved mode. Absent unless a registered
            theme declares one; the flex:1 spacer keeps the actions right-aligned.
            Wrapped in a slot-level ErrorBoundary (fallback=null) so a faulty
            registered extension disables only itself instead of crashing the
            whole shell via the root boundary. */}
        {(() => {
          if (branding?.topBarHideOnMobile && isMobile) return null
          const TB = resolvedMode === 'light' ? branding?.topBar?.light : branding?.topBar?.dark
          return TB ? (
            <ErrorBoundary key={`${colorTheme}:${resolvedMode}`} scope="theme-topbar" fallback={null}>
              <div className="flex-1 min-w-0 h-full"><TB /></div>
            </ErrorBoundary>
          ) : null
        })()}
        <div ref={topbarActionsRef} className="flex items-center gap-1.5 relative ml-auto">
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
              this fork's usage pill is Kiro-credits-only.) */}
          {(() => {
            const offline = !connected
            const seg = `flex items-center gap-1 -my-0.5 px-1.5 py-0.5 rounded-md bg-transparent border-none cursor-pointer transition-colors hover:bg-bg-hover ${offline ? 'opacity-70' : ''}`
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
                title={`${connected ? i18nT('app.gateway_connected') : authRequired ? i18nT('app.gateway_offline_session_expired_see_banner_above') : i18nT('app.gateway_offline_reconnecting')} · ${capsuleCollapsed ? i18nT('app.click_to_expand_readouts') : i18nT('app.click_to_collapse_readouts')}`}
                aria-label={connected ? i18nT('app.gateway_connected') : i18nT('app.gateway_offline')}
                aria-expanded={!capsuleCollapsed}
              >
                <span aria-hidden="true" className={`w-1.5 h-1.5 rounded-full transition-colors duration-300 ${offline ? 'bg-danger animate-pulse motion-reduce:animate-none' : 'bg-ok shadow-[0_0_8px_rgba(34,197,94,.4)]'}`} />
                {/* Live-region announcement lives in its own hidden span:
                    role="status" on the button itself would override its
                    implicit button role for screen readers. */}
                <span role="status" className="sr-only">{connected ? i18nT('app.gateway_connected') : i18nT('app.gateway_offline')}</span>
              </button>
            )
            if (!capsuleCollapsed) {
            if (!isMobile) {
              if (!metricsOpen) {
                segments.push(<button key="metrics" className={`${seg} text-muted hover:text-text`} onClick={() => { setMetricsOpen(true); safeSetItem('mc-topbar-metrics', '1') }} title={i18nT('app.system_metrics')} aria-label={i18nT('app.system_metrics')}><AudioWaveform size={12} /></button>)
              } else if (!sysMetrics) {
                if (sysMetricsError) segments.push(<button key="metrics" className={`${seg} text-danger text-[11px]`} title={i18nT('app.click_to_hide')} onClick={() => { setMetricsOpen(false); safeSetItem('mc-topbar-metrics', '0') }}><AudioWaveform size={11} /> {i18nT('app.metrics_unavailable')}</button>)
              } else {
                const m = sysMetrics
                const memPct = m.memTotal > 0 ? m.memUsed / m.memTotal : 0
                const dskUsed = m.diskTotal - m.diskFree
                const dskPct = m.diskTotal > 0 ? dskUsed / m.diskTotal : 0
                const memValid = m.memTotal > 0
                const dskValid = m.diskTotal > 0
                const cpuValid = typeof m.cpuPct === 'number' && Number.isFinite(m.cpuPct)
                const staleTitle = sysMetricsStale ? ` ${i18nT('app.stale_fetch_failing')}` : ''
                segments.push(<button key="metrics" className={`${seg} gap-2 text-[11px] font-mono ${sysMetricsStale ? 'opacity-60' : ''}`} title={sysMetricsStale ? i18nT('app.metrics_are_stale_latest_fetch_failed') : i18nT('app.click_to_hide')} onClick={() => { setMetricsOpen(false); safeSetItem('mc-topbar-metrics', '0') }}>
                  <span className={cpuValid ? metricColor(m.cpuPct / 100) : 'text-muted'} title={cpuValid ? `CPU: ${m.cpuPct.toFixed(0)}%${staleTitle}` : i18nT('app.cpu_unavailable')}>{i18nT('app.cpu')} {cpuValid ? `${m.cpuPct.toFixed(0)}%` : '—'}</span>
                  <span className={memValid ? metricColor(memPct) : 'text-muted'} title={memValid ? `Memory: ${m.memUsed.toFixed(1)}/${m.memTotal.toFixed(1)} GB${staleTitle}` : i18nT('app.memory_unavailable')}>{i18nT('app.mem')} {memValid ? `${(memPct * 100).toFixed(0)}%` : '—'}</span>
                  <span className={dskValid ? metricColor(dskPct) : 'text-muted'} title={dskValid ? `Disk: ${dskUsed.toFixed(0)}/${m.diskTotal.toFixed(0)} GB${staleTitle}` : i18nT('app.disk_unavailable')}>{i18nT('app.dsk')} {dskValid ? `${(dskPct * 100).toFixed(0)}%` : '—'}</span>
                </button>)
              }
            }
            // Usage segment — Kiro credit plan from KiroCrew's own usage
            // cache. Spinner while the cache warms; hidden when unavailable.
            if (kiroUsage !== 'none') {
              if (!kiroUsage) {
                segments.push(<button key="usage" className={`${seg} text-muted`} onClick={() => setKiroUsageOpen(true)} title={i18nT('app.kiro_credit_usage_checking')} aria-label={i18nT('app.kiro_credit_usage_checking_2')}><Coins size={12} /> {!isMobile && <Loader2 size={11} className="animate-spin" />}</button>)
              } else {
                // Pool the plan and any bonus/welcome credits into one total so
                // the pill reflects what the user is actually spending (bonus is
                // drawn down first). fmtK renders 1000 -> "1K" and 1500 -> "1.5K"
                // (the old toFixed(0) turned 1.5K into a misleading "2K").
                const totalUsed = kiroUsage.used + (kiroUsage.bonus ? kiroUsage.bonus.used : 0)
                const totalLimit = kiroUsage.limit + (kiroUsage.bonus ? kiroUsage.bonus.limit : 0)
                const pct = totalLimit > 0 ? (totalUsed / totalLimit) * 100 : 0
                // `fmtCompact`, not a `/1000 + 'K'` ladder: de has no short form at
                // these magnitudes and renders `447.500`, zh abbreviates on 万 as
                // `44.8万`. English is unchanged (`447.5K`). German is therefore
                // WIDER than before — that is CLDR's answer for the language, not a
                // bug, so the pill is kept nowrap so it can never break mid-number.
                const fmtK = (n: number) => fmtCompact(n)
                const usedStr = fmtK(totalUsed)
                const limitStr = fmtK(totalLimit)
                // Two whole-sentence keys rather than a base string plus an
                // appended bonus clause: the bonus phrase carries its own
                // grammar and word order, so concatenating it would strand the
                // translator with a fragment.
                const title = kiroUsage.bonus
                  ? i18nT('app.kiro_credits_title_with_bonus', {
                    used: fmtNumber(totalUsed),
                    limit: fmtNumber(totalLimit),
                    planUsed: fmtNumber(kiroUsage.used),
                    planLimit: fmtNumber(kiroUsage.limit),
                    bonusLabel: kiroUsage.bonus.label,
                    bonusUsed: fmtNumber(kiroUsage.bonus.used),
                    bonusLimit: fmtNumber(kiroUsage.bonus.limit),
                  })
                  : i18nT('app.kiro_credits_title', {
                    used: fmtNumber(totalUsed),
                    limit: fmtNumber(totalLimit),
                    pct: fmtPercent(pct / 100),
                  })
                segments.push(<button key="usage" className={kiroUsage.stale ? `${seg} opacity-60` : seg} onClick={() => setKiroUsageOpen(true)} title={title} aria-label={title}>
                  <Coins size={12} /> {!isMobile && <span className="font-mono text-[11px] whitespace-nowrap tabular-nums">{usedStr}<span className="text-muted">/{limitStr}</span></span>}
                </button>)
              }
            }
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
                className={`flex items-center gap-2 h-7 px-2.5 rounded-xl transition-colors duration-300 ${offline ? 'bg-danger-subtle' : 'bg-card'}`}
              >
                {segments.flatMap((s, i) => (i === 0 ? [s] : [<span key={`sep-${i}`} className="w-px h-3.5 bg-border shrink-0" aria-hidden="true" />, s]))}
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
          {/* Feedback — "Request a Feature" plus, on a prerelease build, a
              channel chip that opens the same Report a Problem flow. Its own
              bordered pill (28px tall, 12px radius), separated from the readout
              capsule (item 2.3). */}
          {!isMobile && (
            <FeedbackPill
              onRequestFeature={requestFeature}
              onReportProblem={() => setReportProblemOpen(true)}
            />
          )}
          {/* Notifications bell — borderless icon button, rightmost control.
              (The activity-panel open toggle now lives in the session header,
              beside the pop-out control — see ChatPage — so opening the panel
              no longer narrows this full-width header.) */}
          <NotificationsBellButton />
        </div>
      </header>

      {/* Report a Problem — mounted by the nav rail's "Report issue" link. */}
      <ReportProblemModal open={reportProblemOpen} onClose={() => setReportProblemOpen(false)} />

      {/* Update error modal */}
      {updateError && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-rise" role="dialog" aria-modal="true" aria-label={i18nT('app.update_error')}>
          <div className="bg-card border border-border rounded-xl p-8 max-w-md w-full mx-4 shadow-xl text-center">
            <div className="text-4xl mb-4"><AlertTriangle className="lucide-inline" /></div>
            <div className="text-lg font-bold text-text-strong mb-2">{i18nT('app.update_failed')}</div>
            <div className="text-sm text-danger mb-6">{updateError}</div>
            <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-card border border-border text-text hover:border-border-strong transition-colors" onClick={() => setUpdateError('')}>
              {i18nT('app.dismiss')}
            </button>
          </div>
        </div>
      )}

      {/* Changelog modal */}
      {showChangelog && !updating && (
        <Clickable className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/60 backdrop-blur-sm animate-rise" onClick={e => { if (e && e.target === e.currentTarget) { setShowChangelog(false); setShowFull(false) } }}>
          <div role="dialog" aria-modal="true" aria-label={i18nT('app.changelog')} className={`bg-card border border-border rounded-xl p-6 w-full mx-4 shadow-xl transition-all duration-300 ${showFull ? 'max-w-2xl' : 'max-w-md'}`}>
            <div className="flex justify-between items-center mb-4">
              <div className="text-sm font-bold text-text-strong"><Package className="lucide-inline" /> {i18nT('app.v')}{version}</div>
              <button aria-label={i18nT('app.close')} className="text-muted text-[13px] cursor-pointer hover:text-text" onClick={() => { setShowChangelog(false); setShowFull(false) }}><X className="lucide-inline" /></button>
            </div>
            {updateAvailable ? (
              <>
                {changes ? (
                  <>
                    <div className="text-[13px] font-medium text-muted uppercase tracking-wider mb-2">{i18nT('app.what_s_new')}</div>
                    <div className="p-3 bg-bg rounded-lg border border-border max-h-56 overflow-y-auto mb-4">
                      <div className="text-[13px] text-text leading-relaxed"><MarkdownRenderer content={changes} /></div>
                    </div>
                  </>
                ) : (
                  <div className="p-3 bg-bg rounded-lg border border-border mb-4">
                    <div className="text-[13px] text-muted leading-relaxed">{i18nT('app.a_newer_version_is_available_no_changelog_entry')}</div>
                  </div>
                )}
                <button className="w-full py-2 rounded-lg text-[13px] font-medium cursor-pointer bg-accent text-accent-fg border-none hover:opacity-90 transition-opacity" onClick={handleUpdate}>
                  {i18nT('app.update_now')}
                </button>
              </>
            ) : (
              <div className="text-sm text-muted py-4 text-center"><CheckCircle className="lucide-inline" /> {i18nT('app.you_re_on_the_latest_version')}</div>
            )}
            <div className="flex items-center justify-between mt-4 pt-3 border-t border-border">
              <span className="text-[13px] text-muted">{i18nT('app.auto_update_on_restart')}</span>
              <Toggle checked={autoUpdate} label={i18nT('app.auto_update_on_restart')}
                onChange={async next => { setAutoUpdate(next); await api.setAutoUpdate(next) }} />
            </div>
            <div className="mt-3 pt-3 border-t border-border">
              <button className="text-[13px] text-muted cursor-pointer hover:text-text transition-colors bg-transparent border-none p-0 font-body" onClick={async () => {
                if (!showFull) { if (!fullChangelog) { const d = await api.changelog(); setFullChangelog(d.content || '') }; setShowFull(true) } else { setShowFull(false) }
              }}>{showFull ? i18nT('app.hide_full_changelog') : i18nT('app.view_full_changelog')}</button>
              {showFull && fullChangelog && (
                <div className="mt-2 p-3 bg-bg rounded-lg border border-border max-h-72 overflow-y-auto">
                  <div className="text-[13px] text-text leading-relaxed"><MarkdownRenderer content={fullChangelog} /></div>
                </div>
              )}
            </div>
          </div>
        </Clickable>
      )}

      {/* Updating overlay */}
      {(updating || showUpdateModal) && <UpdateOverlay onCancel={() => { setUpdating(false); setShowUpdateModal(false) }} />}
      <UpdateModal />

      {/* First-run modal chrome mounted ONCE (scrim + accent panel + floating
          mascots) so the import→customize hand-off swaps only the right-column
          content — the mascots never remount/replay, killing the transition
          glitch. Both flows portal their content into this single shell; each
          still renders standalone (its own chrome) when used outside a host. */}
      <OnboardingShellHost>
        {/* First-run chapter 1 — import gate. Existing users inherit the old
            onboarding marker, while new users reach Privacy (and then the
            feature tour) only after this flow. */}
        <AgentImportFlow
          initialOpen={showAgentImport}
          onComplete={() => {
            markImportOnboarded()
            setShowAgentImport(false)
            const wantsTour = !onboarded || continueTourAfterImport.current
            continueTourAfterImport.current = false
            if (!privacyAcked) {
              privacyExit.current = wantsTour ? 'customize' : 'finish'
              setShowPrivacy(true)
              return
            }
            if (wantsTour) setShowOnboarding(true)
          }}
          onSkipAll={() => {
            // Skip the rest of first run — but NOT the Privacy chapter, which is
            // mandatory: show it, and let its Continue mark onboarding done so
            // the user lands in the product (new chat) straight after it.
            markImportOnboarded()
            setShowAgentImport(false)
            continueTourAfterImport.current = false
            if (!privacyAcked) {
              privacyExit.current = 'finish'
              setShowPrivacy(true)
              return
            }
            markOnboarded()
            setShowOnboarding(false)
          }}
        />

        {/* First-run chapter 2 — Privacy. Mandatory and un-skippable: every path
            out of chapter 1 (finish, "Skip import", nothing to import, "Skip
            all") arrives here. */}
        <PrivacyChapter
          open={showPrivacy}
          onContinue={() => {
            markPrivacyAcked()
            setShowPrivacy(false)
            if (privacyExit.current === 'finish') markOnboarded()
            else setShowOnboarding(true)
          }}
        />

        {/* First-run chapter 3 — Customize + feature tour (theme → about you →
            Schedule → Apps → Sessions). Rendered unconditionally so the
            `/onboarding` slash command can reopen it anytime; internal
            visibility is seeded by `initialOpen`. */}
        <OnboardingFlow
          initialOpen={showOnboarding}
          onComplete={endFirstRun}
          onSkipAll={endFirstRun}
        />
      </OnboardingShellHost>

      {/* Mobile backdrop */}
      <AnimatePresence>
        {isMobile && mobileNavOpen && (
          <motion.div
            key="nav-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25 }}
            className="fixed inset-0 z-[46] bg-black/50 backdrop-blur-sm"
            onClick={() => setMobileNavOpen(false)}
          />
        )}
      </AnimatePresence>

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
          {/* mb-1.5 (6px) + the container's gap-0.5 (2px) = 8px between the
              header and the first nav item, without widening the 2px item gaps. */}
          <div className={`relative flex items-center mb-1.5 ${effectiveCollapsed ? 'justify-start' : ''}`}>
            {/* One persistent click target that toggles the rail. The logo
                never unmounts, so it stays perfectly still across collapse/
                expand (no swap, no shift). Only the brand text + collapse arrow
                animate — fading in on expand and out on collapse via
                AnimatePresence. No hover tint on the row; on hover only the
                logo rotates (group-hover). */}
            {/* No overflow-hidden here: the logo's hover-rotate paints a few
                px past its box, and clipping it looked cut off. Rotation is a
                transform so it doesn't affect the header's layout height
                (row height tracks the logo, collapse-icon alignment
                unchanged); horizontal spill on collapse is still clipped by
                the rail (motion.nav) and the brand text clips itself via
                `truncate`.
                Logo is DUAL-SIZE: w-7 (28px) expanded — 1px card border +
                pt-2 + 14 puts the header row's center on the 23px shared
                control baseline — and w-10 (40px) collapsed, where the
                icons-only rail keeps the full brand mark (a branding
                logoClass overrides both). The collapse arrow no longer centers
                in the row — it pins to top-[6px] so its center stays on the
                23px shared control baseline (chat title row, its sessions
                toggle, and the activity strip icons) while the two-line
                brand block makes the row taller. */}
            <button
              type="button"
              className="group relative flex items-center gap-2 w-full p-0 bg-transparent border-none cursor-pointer text-left"
              onClick={toggleNav}
              title={effectiveCollapsed ? i18nT('app.expand_sidebar') : i18nT('app.collapse_sidebar')}
              aria-label={effectiveCollapsed ? i18nT('app.expand_sidebar') : i18nT('app.collapse_sidebar')}
              aria-expanded={!effectiveCollapsed}
            >
              <span className="flex items-center gap-2.5 min-w-0">
                <img src={avatar} alt="" aria-hidden="true" className={`${branding?.logoClass ?? (effectiveCollapsed ? 'w-10 h-10' : 'w-7 h-7')} rounded-md shrink-0 object-contain transition-all duration-300 group-hover:rotate-[-8deg]`} />
                <AnimatePresence initial={false}>
                  {!effectiveCollapsed && (
                    <motion.span
                      key="brand-text"
                      initial={{ opacity: 0, x: -6 }}
                      animate={{ opacity: 1, x: 0 }}
                      exit={{ opacity: 0, x: -6, transition: { duration: 0.12, ease: 'easeIn' } }}
                      transition={{ duration: 0.2, ease: 'easeOut' }}
                      className="text-[13px] font-bold tracking-[.14em] uppercase whitespace-nowrap truncate min-w-0"
                    >
                      {/* Last word of the bot name carries the accent (KIRO
                          CREW: muted brand, accent product); single-word names
                          render all-muted. */}
                      {botName.includes(' ') ? (
                        <>
                          <span className="text-muted">{botName.slice(0, botName.lastIndexOf(' ') + 1)}</span>
                          <span className="text-accent/90">{botName.slice(botName.lastIndexOf(' ') + 1)}</span>
                        </>
                      ) : (
                        <span className="text-muted">{botName}</span>
                      )}
                    </motion.span>
                  )}
                </AnimatePresence>
              </span>
              {/* Arrow is ABSOLUTE (out of flex flow), pinned to the right.
                  If it were a flex child it would reserve ~16px on the right
                  from frame 1 of expand — but the rail is still at collapsed
                  width (74px) for that frame, so logo + gap + arrow overflowed
                  and the logo got crammed/clipped against the arrow (the
                  "blink"). Absolute-positioning removes that reserved space, so
                  the logo stays put and the arrow just fades in at the edge. */}
              <AnimatePresence initial={false}>
                {!effectiveCollapsed && (
                  <motion.span
                    key="collapse-arrow"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1, transition: { duration: 0.18, ease: 'easeOut', delay: 0.12 } }}
                    exit={{ opacity: 0, transition: { duration: 0.12, ease: 'easeIn' } }}
                    className="absolute right-0 top-[6px] h-4 flex items-center text-muted pointer-events-none"
                  >
                    {/* Arrow-to-edge, not a hide-panel glyph: the rail
                        collapses to an icon rail rather than hiding. */}
                    <ArrowLeftToLine size={15} />
                  </motion.span>
                )}
              </AnimatePresence>
            </button>
          </div>
          {/* Hairline under the expanded header (collapsed rail has none —
              the big logo alone separates well). */}
          {!effectiveCollapsed && <div aria-hidden="true" className="h-px bg-border shrink-0 mt-0.5 mb-[7px]" />}
          {NAV_ITEMS.filter(n => n.group === 'Main').map(n => <div key={n.id}>{renderNavRow(n)}</div>)}
          {/* Apps section header. "Explore" (the App Store) rides the header
              row in accent when expanded; collapsed it becomes a regular
              muted icon row like its neighbors. No shared-layout fly-across:
              the header link simply unmounts and the collapsed row fades in
              and slides up into place. */}
          {!effectiveCollapsed ? (
            <div className="nav-section flex items-center justify-between gap-2 pl-3 pr-1 pt-3 pb-1">
              <span
                // `overflow-hidden` + `whitespace-nowrap` means this clips
                // silently once the label grows — which it does in a longer
                // locale. The `title` keeps the full string reachable instead
                // of losing the tail with no affordance.
                title={i18nT('app.apps')}
                className="text-[13px] font-medium text-muted whitespace-nowrap overflow-hidden"
              >{i18nT('app.apps')}</span>
              <Clickable
                data-onboarding-nav="apps"
                onClick={() => { closeMobileNav?.(); navigate('/apps') }}
                className={`flex items-center gap-1.5 px-1.5 py-1 rounded-md cursor-pointer text-[12px] font-medium whitespace-nowrap transition-colors ${activePath === '/apps' ? 'text-accent bg-accent-subtle' : 'text-accent hover:bg-bg-hover'}`}
                aria-label={i18nT('app.explore_apps')}
              >
                <LayoutGrid size={14} className="shrink-0" />
                {i18nT('app.explore')}
              </Clickable>
            </div>
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
                label={i18nT('app.explore_apps')}
                icon={<LayoutGrid size={16} />}
                active={activePath === '/apps'}
                collapsed
                onClick={closeMobileNav}
                badge={<NavBadge navId="apps" collapsed appBadges={appBadges} />}
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
                  badge={!devPageSeen && activePath !== devPath ? <span className={dotClass} /> : undefined}
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
                  active={bottomTerminalOpen || terminalPoppedOut}
                  pressed={bottomTerminalOpen || terminalPoppedOut}
                  collapsed={effectiveCollapsed}
                  onClick={closeMobileNav}
                  /* While popped out: focus only (a refused programmatic
                     focus is a harmless no-op). Explicit re-dock lives in the
                     TerminalDetachedBar below -- never a timing heuristic. */
                  onClickOverride={() => { if (terminalPoppedOut) focusTerminalPopout(); else toggleBottomTerminal() }}
                />
              )}
              <div>{renderNavRow(cap)}</div>
              <NavItem
                path={s.path}
                label={surfaceLabel(s)}
                icon={s.icon}
                active={activePath === s.path}
                collapsed={effectiveCollapsed}
                onClick={closeMobileNav}
                badge={updateAvailable ? <span title={i18nT('app.update_available')} role="status" aria-label={i18nT('app.update_available_2')} className={effectiveCollapsed ? 'absolute top-1 right-1 w-2 h-2 bg-accent rounded-full z-10' : 'absolute top-1/2 -translate-y-1/2 right-2 w-2 h-2 bg-accent rounded-full z-10'} /> : undefined}
              />
              {/* Community row — a leading GitHub mark, then two links on ONE
                  line separated by a middot, then the icon-only Discord link.

                  This line is tight by construction, and the numbers are
                  MEASURED against real font advance widths, not estimated.
                  The rail is 236px, which leaves a 143px text group after the
                  mark, the Discord icon and padding; the middot plus its gaps
                  costs ~10-15px depending on family.

                  CRITICAL: size this against the WIDEST font the user can pick,
                  not the default. `useZoom` lets them set --font-body to sans
                  (Space Grotesk), mono (JetBrains Mono) or system (-apple-system),
                  and mono is ~20% wider. A 12px row measured only against Space
                  Grotesk truncates for every mono user.

                  "Star us · Report issue" at 12px, measured:
                    Space Grotesk   114.0px against a 132.8px budget — 18.7 spare
                    JetBrains Mono  136.8px against a 127.8px budget — 9.0 OVER
                  Rather than shrink the type for everyone or drop the Discord
                  link, mono alone is tightened to -0.05em, which brings it to
                  125.4px (+3.0 spare). That rule lives in index.css keyed on
                  html[data-font-family="mono"] via the `rail-community-links`
                  class, and its measurement table is there. Mono's margin is only
                  ~3px, so ANY copy growth here must be re-measured IN MONO first.

                  The separator is a middot because " / " is wider, and the row's
                  right padding is trimmed for the same budget reason.

                  The mark sits 2px from the text (ml-0.5) while the middot keeps
                  4px gaps. That asymmetry is an OPTICAL correction, not an
                  oversight: github-mark.svg is a circle filling its whole 16x16
                  viewBox (no internal padding), and a circle beside a capital "S"
                  curves away from it, so an equal metric gap reads as a wider
                  one. Matching the middot's 4px here looked detached. Font and
                  letter-spacing are deliberately NOT overridden — the row
                  inherits --font-body and letter-spacing:normal from body, so it
                  follows the user's own font choice like everything else.

                  Order of yielding under pressure is deliberate: "Star us" and
                  the middot are shrink-0, so a longer locale (Spanish's "Informar
                  de un problema") ellipsizes the TAIL of the second link rather
                  than mangling both. Both links keep a title tooltip, so a
                  clipped label is still readable on hover.

                  One mark for two links is correct — both destinations ARE
                  GitHub. It is decorative (BrandGlyph is aria-hidden) and each
                  link carries its own descriptive aria-label, since "Star us"
                  alone names no target. Hidden while the rail is collapsed (folds
                  away via max-height so the collapse stays smooth). */}
              <div {...(effectiveCollapsed ? { inert: '' } : {})} className={`overflow-hidden transition-all duration-200 ${effectiveCollapsed ? 'max-h-0 opacity-0' : 'max-h-16 opacity-100 mt-1'}`}>
                <div className="flex items-center border-t border-border-strong pl-3 pr-0.5 pt-2.5 pb-0.5 whitespace-nowrap">
                  {/* pl-3 puts the mark on the same 12px x-offset as the
                      nav-item icons above. No `gap` on this row ON PURPOSE: a row
                      gap applies between ALL THREE children (mark, links,
                      Discord), so pairing it with ml-0.5 would silently double
                      the mark-to-text distance to 6px and cost 4px the budget
                      below never accounts for. Spacing is explicit per child instead. */}
                  <span className="flex items-center shrink-0 text-muted"><GithubIcon size={15} /></span>
                  <div className="rail-community-links flex items-center gap-[5px] flex-1 min-w-0 ml-1.5 text-[12px]">
                    <a href="https://github.com/kirodotdev/KiroCrew" target="_blank" rel="noopener noreferrer" title={i18nT('app.star_kirocrew_on_github')} aria-label={i18nT('app.star_kirocrew_on_github')} className="shrink-0 rounded text-muted hover:text-text transition-colors">{i18nT('app.star_us')}</a>
                    <span aria-hidden="true" className="shrink-0 opacity-40">·</span>
                    {/* "Report issue" opens the SAME diagnostics flow as Settings ›
                        About › Support rather than linking to the bare issue list.
                        A user who reaches for this link is reporting a failure, and
                        an empty issue form loses exactly what triage needs (logs +
                        crash reports); the collector scrubs secrets, zips them, and
                        still ends at a pre-filled GitHub issue, so the old
                        destination is reachable WITH evidence attached. A <button>
                        (not an <a>) because it no longer navigates — styled to match
                        its sibling link so the row's width budget above is unchanged. */}
                    <button type="button" onClick={() => setReportProblemOpen(true)} title={i18nT('app.report_a_problem_with_diagnostics')} aria-label={i18nT('app.report_a_problem_with_diagnostics')} className="min-w-0 overflow-hidden text-ellipsis rounded text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-0 p-0 text-[12px]">{i18nT('app.report_issue')}</button>
                  </div>
                  <a href="https://kiro.dev/discord/" target="_blank" rel="noopener noreferrer" title={i18nT('app.discord_community')} aria-label={i18nT('app.kiro_discord_community')} className="flex items-center justify-center ml-1 w-6 h-6 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors shrink-0"><DiscordIcon size={15} /></a>
                </div>
              </div>
            </div>
          )
        })()}
        </>)
        return isMobile ? (
          <AnimatePresence>
            {mobileNavOpen && (
              <motion.nav
                key="mobile-nav-drawer"
                initial={{ x: -240 }}
                animate={{ width: 220, x: 0 }}
                exit={{ x: -240 }}
                transition={{ duration: 0.25, ease: [0.32, 0.72, 0, 1] }}
                className="bg-bg-elevated border border-border rounded-xl flex flex-col mx-2 mt-0 mb-2 shadow-sm z-50 overflow-hidden fixed top-0 left-0 bottom-0"
                role="navigation"
                aria-label={i18nT('app.main_navigation')}
              >
                {navBody}
              </motion.nav>
            )}
          </AnimatePresence>
        ) : (
          <nav
            className="bg-bg-elevated border border-border rounded-xl flex flex-col mx-2 mt-0 mb-2 shadow-sm z-50 overflow-hidden"
            style={{ gridArea: 'nav', width: 'auto' }}
            role="navigation"
            aria-label={i18nT('app.main_navigation')}
          >
            {navBody}
          </nav>
        )
      })()}

      {/* Content */}
      <div className="flex flex-col min-h-0 min-w-0" style={{ gridArea: 'content' }}>
        <main id="main-content" tabIndex={-1} className={`flex flex-col min-h-0 min-w-0 flex-1 overflow-x-hidden ${needsFixedHeight ? 'overflow-hidden p-0' : 'overflow-y-auto'}`}>
          <MigrationCheck />
          <Routes>
            <Route path="/chat/:slug?" element={<ErrorBoundary><ChatPage /></ErrorBoundary>} />
            <Route path="/orchestrated/:slug?" element={<OrchestratedRedirect />} />
            <Route path="/notifications" element={<ErrorBoundary><NotificationsPage /></ErrorBoundary>} />
            <Route path="/knowledge" element={<ErrorBoundary><KnowledgePage /></ErrorBoundary>} />
            <Route path="/overview" element={<Navigate to="/settings?tab=overview" replace />} />
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
            <Route path="/instances" element={<Navigate to="/settings?tab=instances" replace />} />
            <Route path="/apps" element={<AppsPage />} />
            <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            <Route path="/apps/migrate/:name" element={<MigrationPage />} />
            <Route path="/apps/:name" element={<AppPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/developer" element={<DeveloperPage />} />
            <Route path="/artifacts" element={<ArtifactsPage />} />
            <Route path="/artifacts/deploy" element={<Navigate to="/deploy" replace />} />
            <Route path="/artifacts/remote/:provider/:externalId" element={<ErrorBoundary><RemoteArtifactDetailPage /></ErrorBoundary>} />
            <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
            <Route path="/deploy" element={<ArtifactDeployPage />} />
            {/* Builtin app routes — auto-discovered from registry. React Router v6
                ranks static paths higher than parameterized ones, so /settings, /agents
                etc. still match first. Unrecognized paths fall through to /chat. */}
            <Route path="/:builtinApp" element={<BuiltinAppRoute />} />
            <Route path="*" element={<ChatRedirect />} />
          </Routes>
        </main>
        {/* App-wide docked terminal panel — spans every route, below <main>.
            Toggled from the sidebar Terminal icon; hosts app-wide
            shells. Distinct from the chat-scoped activity-bar terminal tabs. */}
        {terminalEnabled && (terminalPoppedOut ? <TerminalDetachedBar /> : <BottomTerminalPanel />)}

        {/* Self-managed floating panels: lifecycle-driven (hidden → small → chip),
            not motion.* children, so they live outside AnimatePresence. The browse
            mirror docks bottom-right and the computer-use PiP bottom-left, so both
            can be open at once. */}
        <ComputerUseLiveView />
      </div>
    </div>{/* /Local dashboard grid */}
      </div>{/* /Local pane */}
      {/* Remote instance panes — embedded dashboards kept warm (mounted, hidden)
          so switching is instant; the active instance fills the pane. */}
      <InstancesViewport macInset={macInset} />
      </div>{/* /pane stack */}
    </div>
    )}
    </WsContext.Provider>
    {shortcutsOpen && <ShortcutsModal onClose={() => setShortcutsOpen(false)} />}
    <Modal open={kiroUsageOpen} onClose={() => setKiroUsageOpen(false)} title={<span className="flex items-center gap-2"><Coins size={16} /> {i18nT('app.kiro_credits')}</span>} maxWidth={460}>
      {!kiroUsage || kiroUsage === 'none' ? (
        <div className="flex items-center gap-2 text-sm text-muted py-4">
          <Loader2 size={14} className="animate-spin shrink-0" />
          <span>{i18nT('app.checking_usage_running')} <code className="font-mono">{i18nT('app.kiro_cli_usage')}</code>…</span>
        </div>
      ) : (() => {
        const bonus = kiroUsage.bonus
        const totalUsed = kiroUsage.used + (bonus ? bonus.used : 0)
        const totalLimit = kiroUsage.limit + (bonus ? bonus.limit : 0)
        const pct = totalLimit > 0 ? (totalUsed / totalLimit) * 100 : 0
        const planPct = kiroUsage.limit > 0 ? (kiroUsage.used / kiroUsage.limit) * 100 : 0
        const bonusPct = bonus && bonus.limit > 0 ? (bonus.used / bonus.limit) * 100 : 0
        const barColor = 'var(--accent)'
        const Row = ({ label, value }: { label: string; value: React.ReactNode }) => (
          <div className="flex justify-between items-baseline py-1.5 border-b" style={{ borderColor: 'var(--border)' }}>
            <span className="text-[12px] text-muted">{label}</span>
            <span className="text-[13px] font-medium text-text">{value}</span>
          </div>
        )
        // One pool card (bonus or plan) with its own mini progress bar. Used
        // only when a bonus pool exists, so the plan-only modal is unchanged.
        const Pool = ({ name, used, limit, poolPct, color, meta }: { name: string; used: number; limit: number; poolPct: number; color: string; meta?: React.ReactNode }) => (
          <div className="rounded-lg border p-2.5" style={{ borderColor: 'var(--border)' }}>
            <div className="flex items-baseline gap-2">
              <span className="flex items-center gap-1.5 text-[13px] font-medium text-text">
                <span className="w-2 h-2 rounded-sm shrink-0" style={{ background: color }} />{name}
              </span>
              <span className="ml-auto font-mono text-[12px] text-text">{fmtNumber(used)}<span className="text-muted">/{fmtNumber(limit)}</span></span>
            </div>
            <div className="w-full h-1.5 rounded-full overflow-hidden mt-2" style={{ background: 'var(--border)' }}>
              <div className="h-full rounded-full" style={{ width: `${Math.min(poolPct, 100)}%`, background: color }} />
            </div>
            {meta && <div className="text-[11px] text-muted mt-1.5">{meta}</div>}
          </div>
        )
        // Sign-in description shown under the identity: account type + issuer
        // host ("IAM Identity Center · amzn.awsapps.com"). Collapses gracefully
        // when either half is missing.
        // kiro-cli distinguishes four auth kinds (social | idc | builderId |
        // external_idp); social login covers Google/GitHub and reports
        // accountType "Social". Unmapped values pass through verbatim rather
        // than being hidden, so a new kind still says something truthful.
        const acctKind = kiroUsage.accountType === 'IamIdentityCenter' ? i18nT('app.iam_identity_center')
          : kiroUsage.accountType === 'BuilderId' ? i18nT('app.builder_id')
          : kiroUsage.accountType === 'Social' ? i18nT('app.social_login')
          : kiroUsage.accountType
        let issuerHost: string | undefined
        if (kiroUsage.startUrl) { try { issuerHost = new URL(kiroUsage.startUrl).host } catch { issuerHost = undefined } }
        const signedInWith = [acctKind, issuerHost].filter(Boolean).join(' · ')
        // Identity line prefers the real email; the org profile name is only a
        // fallback for accounts where whoami gave us nothing.
        const who = kiroUsage.email || kiroUsage.account
        return (
          <div className="flex flex-col gap-3">
            {who && (
              <div className="flex items-center gap-3 pb-3" style={{ borderBottom: '1px solid var(--border)' }}>
                <div
                  className="shrink-0 rounded-full flex items-center justify-center text-[15px] font-semibold uppercase"
                  style={{ width: 40, height: 40, background: 'var(--accent)', color: '#fff' }}
                  aria-hidden="true"
                >
                  {who.slice(0, 1)}
                </div>
                <div className="min-w-0">
                  <div className="text-[15px] font-medium text-text truncate" title={who}>{who}</div>
                  {signedInWith && <div className="text-[12px] text-muted break-words" title={signedInWith}>{i18nT('app.signed_in_with', { provider: signedInWith })}</div>}
                </div>
              </div>
            )}
            <div className="flex items-baseline gap-2">
              <span className="text-2xl font-bold text-text">{fmtNumber(totalUsed)}</span>
              <span className="text-sm text-muted">/ {fmtNumber(totalLimit)} {bonus ? i18nT('app.credits_total') : i18nT('app.credits')}</span>
              <span className="ml-auto text-[12px] font-medium px-2 py-0.5 rounded-md" style={{ background: barColor, color: '#fff' }}>{fmtPercent(pct / 100)}</span>
            </div>
            <div className="w-full h-2 rounded-full overflow-hidden" style={{ background: 'var(--border)' }}>
              <div className="h-full rounded-full transition-all" style={{ width: `${Math.min(pct, 100)}%`, background: barColor }} />
            </div>
            {bonus && (
              <div className="flex flex-col gap-2">
                <div className="text-[11px] uppercase tracking-wide text-muted mt-1">{i18nT('app.breakdown')}</div>
                <Pool name={bonus.label} used={bonus.used} limit={bonus.limit} poolPct={bonusPct} color="var(--warn)" meta={bonus.expiresLabel} />
                <Pool name={kiroUsage.plan || i18nT('app.plan')} used={kiroUsage.used} limit={kiroUsage.limit} poolPct={planPct} color="var(--accent)" meta={kiroUsage.resets ? `${i18nT('app.resets')} ${kiroUsage.resets}` : undefined} />
              </div>
            )}
            <div className="mt-1">
              {!bonus && kiroUsage.plan && <Row label={i18nT('app.plan')} value={kiroUsage.plan} />}
              {!bonus && kiroUsage.resets && <Row label={i18nT('app.resets')} value={kiroUsage.resets} />}
              <Row label={i18nT('app.overage_used')} value={`${fmtNumber(kiroUsage.overage)} credits`} />
              {kiroUsage.overageRate && <Row label={i18nT('app.overage_rate')} value={`$${kiroUsage.overageRate} / credit`} />}
              {kiroUsage.costUsd != null && <Row label={i18nT('app.est_overage_cost')} value={`$${kiroUsage.costUsd.toFixed(2)} USD`} />}
            </div>
            <p className="text-[11px] text-muted leading-relaxed mt-1">
              {i18nT('app.monthly_kiro_credit_usage_from')} <code className="font-mono">{i18nT('app.kiro_cli_usage')}</code> {i18nT('app.across_chat_agents_mcp_and_subagents')}
            </p>
            <a
              href="https://app.kiro.dev/settings/account"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-[12px] text-accent hover:underline mt-1 self-start"
            >
              {i18nT('app.manage_account')} <ExternalLink size={12} />
            </a>
          </div>
        )
      })()}
    </Modal>
    <CommandPalette
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
