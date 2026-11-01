import { useEffect, useState, useCallback, useRef } from 'react'
import { isLookPreviewFrame } from '../utils/lookPreview'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAppSelector, useAppDispatch, useAppStore } from '../store'
import { fetchSlots, sseStatus, setEnabledAppIds } from '../store/dashboardSlice'
import { setActiveId } from '../store/instancesSlice'
import { selectAllSurfacesAttention, surfaceLabel, surfaceMachineValue } from '../surfaces/registry'
import { switchSlot, selectActiveSlotProject, stageToMainComposer, setPendingInput } from '../store/chatSlice'
import { setNavIntentHandler as setArtifactNavIntentHandler } from '../utils/artifactPopout'
import { setNavIntentHandler as setAppNavIntentHandler } from '../utils/appPopout'
import { appPopoutRouteOf } from '../utils/appPopoutRoute'
import type { NavIntent } from '../utils/popoutController'
import {
  applyNavIntentInMain,
  chatDeepLinkSlot,
  takeDeferredPendingInput,
  type DeferredPendingInput,
} from '../utils/navIntent'
import { installSoftNavigate } from '../utils/errorReport'
import { fetchNotifications, armBootNotificationsFallback } from '../store/notificationsSlice'
import { useWebSocket } from '../hooks/useWebSocket'
import { useDashboardHealthProbe } from '../hooks/useDashboardHealthProbe'
import { useConfigAutolinkRules } from '../hooks/useConfigAutolinkRules'
import { usePreviewFlagServerSync } from '../hooks/usePreviewFlagServerSync'
import { useTheme } from '../hooks/useTheme'
import { useShellBranding } from './branding'
import { useRumPageView } from '../hooks/useRumPageView'
import { useIsMobile } from '../hooks/useIsMobile'
import { useKeyboardViewportReset } from '../hooks/useKeyboardViewportReset'
import { useSidePanelDock } from '../hooks/useSidePanelDock'
import { useAppRailOrder } from './nav/appRail'
import { setRailWidth, railWidthFor } from '../hooks/useRailWidth'
import { useFocusChrome } from './focus/focusChrome'
import { useNativeNotification } from '../hooks/useNativeNotification'
import { useNotificationSound } from '../hooks/useNotificationSound'
import { recordSessionStart } from '../rum'
import { api, isAuthBannerShown } from '../api/client'
import { useKiroUsageReadout } from './topbar/kiroUsageReadout'
import { safeSetItem } from '../utils/safeStorage'
import { gcOrphanedStorage } from '../utils/storageGc'
import { useMetricsReadout } from './topbar/metricsReadout'
import { Package } from 'lucide-react'
import { useFirstRunChapters } from './boot/firstRun'
import { PREVIEW_EXPAND_EVENT } from '../components/WebPreviewPanel'
import { useMobileConnect } from '../components/mobileConnect'
import { useMayLeaveForNavigation } from '../components/NavigationLeaveGuard'
import { useMotionValue, useTransform } from 'framer-motion'
import { useDrawerSwipe, animateDrawer, registerDrawerTargets, takeOverDrawer, safeAreaLeft } from '../hooks/useDrawerSwipe'
import { useTopbarCollapse } from '../lib/useTopbarCollapse'
import { showNativeDevTools, subscribeNativeNavigate, useMacFullscreen } from './platform/electronBridge'
import { useNativeHistoryStep } from './platform/nativeHistoryStep'
import { labelCurrentRouteEntry } from '../lib/routeHistoryLabels'
import AppIcon from '../components/AppIcon'
import AppNavStateIcon from './nav/AppNavStateIcon'
import { useUpdateFlow } from './updates/updateFlow'
import { useUpdateSubscription, usePolicyGovernedNotification } from '../hooks/useUpdateSubscription'
import { useBottomTerminalOpen, useTerminalPosition } from '../hooks/useBottomTerminal'
import { useTerminalRestoreProbe } from './boot/terminalRestore'
import { useTerminalPoppedOut } from '../utils/terminalPopout'
import { getBuiltinIcon } from '../apps/builtinIcons'
import { useRequestFeature } from './topbar/requestFeature'
import { useShellKeyboard } from './shortcuts/shellKeyboard'
import { formatShortcut, shortcutDefFromChord, useShortcutBindings } from '../hooks/useKeyboardShortcuts'
import { shortcutEntry } from '../lib/shortcutRegistry'
import { ariaKeyshortcutsFor, useShortcutsEnabled } from '../hooks/useNavShortcutHint'
import type { MobileRosterBarState } from '../components/MobileNavRailContext'
import { useStartupVideo } from './boot/startupVideo'
import { i18nT } from '../i18n/t'
import { useGuideRevealScope } from '../guide/GuideRevealScope'
import { useGuideGate, useGuidePredicate } from '../guide/guidePredicates'
import { appNavTarget } from '../appNav'
import { useGlobalApprovalCount, useRailBadges } from './nav/railBadges'
import { resolveSlotOverlays, type SlotOwners } from '../apps/overlaySlots'
import { isChatRoute, useRouteActiveModel } from './nav/routeActive'
import { appPathname } from '../lib/basePath'
import { slotRowByKey } from '../store/slotIndex'

/** Mobile nav drawer travel: its 220px width + the 8px mx-2 inset + border. */
/** Mobile nav drawer width. Shared with its travel below so the two cannot drift
 *  — a travel wider than the panel spends the settle's tail moving something
 *  already off the screen. */
export const MOBILE_NAV_WIDTH = 220
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

// Apps-nav fetch resilience (see refreshAppNav). The dashboard loads
// `/api/apps` once on mount; right after a `kirocrew update` the gateway is
// mid-restart (cold backend, apps-dir scan) and that first request can fail or
// time out. Retry with bounded backoff so the Apps rail self-heals instead of
// staying empty until a manual reload or an app enable/disable.
const APP_NAV_MAX_RETRIES = 4
const APP_NAV_RETRY_BASE_MS = 500

/**
 * The dashboard shell's state and effects: the routing context, the boot reads,
 * the listener registrations, the nav drawer, the Apps rail, the top-bar readouts
 * and the calls into the owners under `shell/`. React runs a component's effects in
 * call order, so every hook the shell needs runs here, in that order, inside `App`,
 * which calls this first. `App` and the shell's views (`ShellHeader`,
 * `ShellNavRail`) only read what it returns.
 */
export function useAppShell() {
  const location = useLocation()
  const isEmbed = location.pathname.startsWith('/embed/')
  // Sticky popout-ness: computed from the pathname at DOCUMENT LOAD, not the
  // live route. A window that loaded as /popout/* stays in the popout branch
  // for its whole SPA lifetime, so no soft navigate() — present or future —
  // can ever mount the full dashboard chrome inside a popout window.
  // Deliberately a ref (not window.name-based): returnSelfToMain()'s deep-link
  // fallback does a full location.assign to the main view, which is a fresh
  // document load and correctly re-evaluates to false there.
  const isPopout = useRef(appPathname().startsWith('/popout/')).current
  // The load-time popout URL: App's popout wildcard route re-pins any stray
  // in-window navigation back to this frame instead of escaping to '/'.
  const initialPopoutPath = useRef(appPathname() + window.location.search).current
  // The app an APP pop-out window is pinned to (`/popout/app/<route>/…` at
  // load), null in every other window. Sticky for the same reason as
  // `isPopout`: the router inside that window is mounted at `/popout/app`, so
  // the live route reads as the app's own path and cannot re-derive this.
  const appPopoutRoute = useRef(appPopoutRouteOf(appPathname())).current
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
  // Settings nav dot in the rails.
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
  // The capsule's announced cause, computed here rather than in the header
  // because two places speak it: the capsule inside the header, and the
  // stand-in live region App mounts while focus mode has made the header inert.
  // Auth takes precedence over transport: `authRequired` (a 403 auth flag) and
  // `connected` (Redux transport state) are independently sourced, so the
  // session can expire while the socket is still up, and "Gateway connected"
  // would then be a reassuring lie nothing else corrects.
  const gatewayStatusMsg = authRequired
    ? i18nT('app.gateway_offline_session_expired_see_banner_above')
    : connected
      ? i18nT('app.gateway_connected')
      : i18nT('app.gateway_offline_reconnecting')
  // The chord that turns focus mode off, as the user has it bound RIGHT NOW. In
  // focus mode the top bar (and its toggle) is inert, so this chord is the only
  // keyboard route back; it is spoken when focus mode turns on and declared on
  // the toggle as `aria-keyshortcuts`. Read from the live bindings, not the
  // registry default: a rebound shortcut announced as its default would send the
  // user to a key that no longer does anything. `null` when the user unbound it.
  // The ARIA spelling (Alt+Shift+M) rather than the display glyphs (⌥⇧M): the
  // glyphs are for the eye and a screen reader has no names for them.
  //
  // `null` = unbound. Nothing in the product can produce that today: the only
  // writer of the overrides store is the Settings > Keyboard UI, which does not
  // exist yet (shortcutRegistry.ts, "No writer here on purpose"). What unbinding
  // the one chord that leaves a mode with no reachable controls should mean is
  // that writer's decision; until it exists the carriers simply say nothing
  // about a chord, rather than naming a page the inert chrome cannot reach.
  //
  // Shortcuts switched off globally (Settings > Shortcuts) is the same as
  // unbound: the keydown handler gives up before it reaches the chord, so it
  // must not be advertised.
  const shortcutBindings = useShortcutBindings()
  const shortcutsEnabled = useShortcutsEnabled()
  const focusModeChordDef = (() => {
    if (!shortcutsEnabled) return null
    const entry = shortcutEntry('toggle-focus-mode')
    const chord = shortcutBindings['toggle-focus-mode']?.primary
    return entry && chord ? shortcutDefFromChord(entry, chord) : null
  })()
  const focusModeChord = focusModeChordDef ? ariaKeyshortcutsFor(focusModeChordDef) : null
  const focusModeExitMsg = focusModeChord ? i18nT('app.focus_mode_on_announce', { chord: focusModeChord }) : ''
  // The same sentence for the EYE, in the product's display spelling
  // (formatShortcut: ⌥⇧M on a Mac, Alt + Shift + M elsewhere) -- a Mac user shown
  // "Alt" has to work out which key that is, and this is their way out.
  const focusModeExitMsgVisible = focusModeChordDef
    ? i18nT('app.focus_mode_on_announce', { chord: formatShortcut(focusModeChordDef) })
    : ''
  // Hidden chrome turns inert only while that chord can bring it back. With no
  // live chord (shortcuts off, or the chord unbound) an inert header would leave
  // a keyboard user no way out of focus mode at all, so the chrome keeps main's
  // behaviour: reachable by Tab, which reveals it (useHoverIntent's onFocus).
  const focusChromeInert = focusModeChordDef !== null
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
  const mobileConnect = useMobileConnect(location.key)
  const { mobileConnectOpen, setMobileConnectOpen, hasRenderableMobileConnect } = mobileConnect
  // Selected session's project directory: a terminal opened from the nav row
  // starts there (server default when no session is selected or it has none).
  const activeSlotProject = useAppSelector(selectActiveSlotProject)
  const terminalPosition = useTerminalPosition()
  const navigate = useNavigate()
  const mayLeaveForNavigation = useMayLeaveForNavigation()
  const appStore = useAppStore()
  const pathnameRef = useRef(location.pathname)
  pathnameRef.current = location.pathname
  const locationKeyRef = useRef(location.key)
  locationKeyRef.current = location.key
  const deferredPendingInputRef = useRef<DeferredPendingInput | null>(null)

  // Publish a forwarded prompt only on its own commit: the destination itself,
  // or ChatPage's `/chat[/<slug>]?sid=` rewrite for the named active session.
  // Any other commit inside the pending transition, including another intent,
  // a sidebar click, or a session switch, drops it rather than seed a draft the
  // intent never named. ChatPage renders under App, so its intake runs first in
  // the same commit and the dispatch re-runs it against the committed route and
  // query.
  useEffect(() => {
    const staged = deferredPendingInputRef.current
    if (!staged || location.key === staged.fromKey) return
    deferredPendingInputRef.current = null
    const text = takeDeferredPendingInput(
      staged,
      {
        key: location.key,
        href: `${location.pathname}${location.search}`,
        activeSlot: appStore.getState().chat.activeSlot,
      },
      Date.now(),
    )
    if (text !== null) dispatch(setPendingInput(text))
  }, [location.key, location.pathname, location.search, dispatch, appStore])

  // Main-dashboard role for the artifact popout nav-intent handshake: perform
  // navigation intents forwarded from popout windows (activity-timeline
  // session links, "Draft a prompt in chat", …). Popout and embed windows never
  // register — only handler-registered windows answer nav-requests, which is
  // what keeps a second popout from claiming another popout's navigation.
  // A forwarded intent replaces the page on screen HERE, so that page's leave
  // check runs first — the popout cannot see this window's unsaved work.
  //
  // A hand-off to the session /chat already shows is staged on
  // `mainComposerAppend`, whose consumers (ChatPage, or the split-view grid
  // pane for that slot) merge it into the live draft. Read through the store at
  // call time so the handler is not re-registered on every slot switch.
  //
  // One applier, two channels: artifact pop-outs and app pop-outs coordinate on
  // separate BroadcastChannels (so neither feature's liveness map sees the
  // other's windows), but a forwarded navigation means the same thing from
  // either, so both register the same handler here.
  useEffect(() => {
    if (isPopout || isEmbed) return
    const perform = (intent: NavIntent) =>
      applyNavIntentInMain(intent, {
        navigate,
        switchSlot: (slotKey) => { dispatch(switchSlot({ key: slotKey, announceOnMissing: true })) },
        mayLeave: mayLeaveForNavigation,
        appendToOpenComposer: (slotKey, text) => {
          const path = pathnameRef.current
          const onChat = path === '/chat' || path.startsWith('/chat/')
          if (!onChat || appStore.getState().chat.activeSlot !== slotKey) return false
          dispatch(stageToMainComposer({ slot: slotKey, text }))
          return true
        },
        setPendingInput: (text, to, slotKey) => {
          // Last writer wins for one destination and session.
          deferredPendingInputRef.current = {
            text,
            fromKey: locationKeyRef.current,
            to,
            slotKey,
            ts: Date.now(),
          }
        },
      })
    const unsetArtifact = setArtifactNavIntentHandler(perform)
    const unsetApp = setAppNavIntentHandler(perform)
    return () => { unsetArtifact(); unsetApp() }
  }, [isPopout, isEmbed, navigate, dispatch, mayLeaveForNavigation, appStore])

  // Publish the router navigator and the current page's leave answer for the
  // error → agent hand-off. AskAgentButton is deliberately hook-free (its
  // callers include ErrorBoundary fallbacks, where router context may be what
  // threw), so it navigates through this seam and falls back to a full page
  // load when nothing is installed. The guard runs before prompt staging, so a
  // veto leaves both the page and the hand-off queue untouched.
  //
  // Embed windows never register. Chat and artifact pop-outs do not either:
  // routing those windows to /chat would replace the surface the user chose to
  // keep open. An app pop-out does register because its frame forwards a /chat
  // navigation to the main window and steps back, so the soft path preserves
  // the popped-out app while carrying the staged prompt across windows.
  useEffect(() => {
    if ((isPopout && !appPopoutRoute) || isEmbed) return
    installSoftNavigate(navigate, mayLeaveForNavigation)
    return () => installSoftNavigate(null)
  }, [isPopout, appPopoutRoute, isEmbed, navigate, mayLeaveForNavigation])

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
  // Decided once per document: the URL does not change under a mounted App.
  const lookPreviewFrame = isLookPreviewFrame()
  const { showAgentImport, showPrivacy, showOnboarding } = firstRun
  // Capture Electron update lifecycle events app-wide so UpdateModal fires on
  // any page, not just after the user has opened Settings > About.
  useUpdateSubscription()
  // Notify the main process whenever the gateway status reports a security
  // policy provider governs this install's updates, so the feed lane stands down.
  usePolicyGovernedNotification()
  const { branding, botName, avatar } = useShellBranding({ colorTheme, brandName, brandLogo, brandFavicon })
  useRumPageView()
  // iPad/iPhone Safari: put the page back at scroll 0 after the on-screen
  // keyboard hides, so the header stays in view and no gap appears below.
  useKeyboardViewportReset()
  useNotificationSound()
  // The rail starts collapsed until the user expands it: only a stored '0'
  // (the user's own expand) opens it on load.
  const [navCollapsed, setNavCollapsed] = useState(() => localStorage.getItem('mc-nav') !== '0')
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
  // active (`display:none` in App's layout) so its state and websocket survive the
  // switch. But a CSS *animation* restarts when an element goes from
  // `display:none` back to displayed — unlike a transition, and unlike
  // framer-motion's JS-driven animations. Left unguarded, `animate-rise`
  // therefore replays its 350ms opacity-0 -> 1 + 8px lift over the WHOLE
  // dashboard every time the user returns to the Local tab, which reads as the
  // entire UI (side panel included) flashing in again.
  const [shellEntered, setShellEntered] = useState(false)
  // Backstop for the shell's onAnimationEnd latch. `animationend` does NOT fire when a running
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
   * to the body level (components/overlayRoot), so they are outside this
   * element entirely, and a page
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
  // What the Crewmates page last reported about the bar (see
  // `MobileRosterBarContext`); `pending` until it has said anything on this
  // visit. Reset whenever the route is not the Crewmates page, so each visit
  // starts on the single bar instead of whatever the last visit ended on.
  const onMembersRoute = location.pathname === '/members' || location.pathname.startsWith('/members/')
  const [membersBar, setMembersBar] = useState<MobileRosterBarState>('pending')
  if (!onMembersRoute && membersBar !== 'pending') setMembersBar('pending')
  const membersSingleBar = onMembersRoute && (membersBar === 'open' || membersBar === 'pending')
  useDrawerSwipe(shellRef, {
    // Off on the phone chat page: that page's sessions drawer carries the main
    // navigation as a rail (see `mobileNavRail`), so the nav drawer has no
    // trigger there and must not be reachable by a swipe on the header either
    // — two drawers for one gesture is the state this bar exists to remove.
    // The chat container already claims its own swipe via `data-owns-swipe`;
    // this gate covers the header above it.
    // The phone Crewmates page with a crewmate open is the same single bar (see
    // `mobileSingle`), with the rail in its roster drawer, so it is off there too.
    // Only once the page has said it has a crewmate open: before that its
    // drawer is not bound, so while a crewmate is still being read (or the
    // page threw before it could report) the swipe stays a way to the
    // navigation and the bar does not have to change under the user.
    enabled: isMobile && !isChatRoute(location.pathname) && !(onMembersRoute && membersBar === 'open'),
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
            // An app that declares `ui.pages[].iconInactiveUrl` gets that file on
            // its row while the row is not selected, the way built-in icons go
            // grey (see AppNavStateIcon).
            const stateIcon = target.inactiveIconUrl
              ? <AppNavStateIcon icon={baseIcon} inactiveSrc={target.inactiveIconUrl} />
              : baseIcon
            // Orphaned apps get a warn-colored icon to signal migration needed
            const icon = target.orphaned
              ? <span className="text-warn">{stateIcon}</span>
              : stateIcon
            return [{
              path: target.route,
              id: target.id,
              label: target.label,
              group: surfaceMachineValue('Apps'),
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
    toggleFocusMode, toggleNav: () => toggleNav(), terminalEnabled, isPopout, isEmbed, terminalPoppedOut, activeSlotProject,
  })

  const { kiroUsageOpen, setKiroUsageOpen, kiroUsageState, usageSegmentHidden, kiroAccountEntry } = useKiroUsageReadout()
  // Feature previews live on the gateway; this keeps the browser cache of them in
  // step. Called beside the credit readout, which already reads ['kirocrewConfig'].
  usePreviewFlagServerSync()
  // Both forms use the side groups' container-query ladders. On desktop the
  // measured ladder (`.topbar.tb-measured`, index.css) adds folds when those
  // rungs still leave a group's contents overflowing; its level classes can
  // hide metric numbers without resizing the box the metrics probe observes.
  const topbarLevels = useTopbarCollapse(topPeekSurface, !isMobile)
  const metrics = useMetricsReadout(isMobile, updateAvailable, topbarLevels.right)
  const { capsuleCollapsed, setCapsuleCollapsed, capsuleLayoutPulse, pulseCapsuleLayout, sysMetrics, metricsProbeRef, metricsGroupRef } = metrics

  // Guide facts the shell knows: the gate a guide pauses on (it never flips
  // it), the phone menu's scope, and whether this is the Sessions page (whose
  // own drawer replaces the menu button there).
  useGuideGate('terminal_enabled', terminalEnabled)
  useEffect(() => { showNativeDevTools() }, [])
  useGuidePredicate('not_on_sessions_page', location.pathname !== '/sessions')
  // Whether the rail draws Connect your phone at all: the pairing guide's blocker otherwise.
  useGuidePredicate('phone_connect_available', hasRenderableMobileConnect)
  useGuideRevealScope(isMobile ? 'menu:shell.mobile-menu' : undefined, mobileNavPhase === 'open')
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
        // The session lives in the local Crew. Over a Remote Crew pane, bring
        // the local Crew forward first (as the notification banner does), so
        // the leave prompt and the switched session are both on screen.
        if (appStore.getState().instances?.activeId != null) dispatch(setActiveId(null))
        applyNavIntentInMain(
          // `path` is deliberately dropped in favour of the bare route: a
          // NavIntent carries no query string, and ChatPage writes `?sid=` back
          // into the URL itself once the session is active.
          { path: '/chat', slotKey },
          {
            navigate,
            switchSlot: (key) => { dispatch(switchSlot({ key, announceOnMissing: true })) },
            mayLeave: mayLeaveForNavigation,
          },
        )
        return
      }
      // Same leave check as every other in-app exit: a native menu entry
      // replaces the page on screen just like a sidebar click does.
      if (!mayLeaveForNavigation()) return
      navigate(path)
    })
  }, [navigate, dispatch, mayLeaveForNavigation, appStore])
  // A mouse's Back/Forward buttons on macOS, relayed by the shell as a swipe.
  useNativeHistoryStep(isMobile)

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

  const updateFlow = useUpdateFlow()
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
  // The active session's title, for the tab title below. A slot whose title
  // is still its key has no real title yet (same rule as the chat popout).
  const activeSessionTitle = useAppSelector(s => {
    const key = s.chat.activeSlot
    const title = key ? slotRowByKey(s.dashboard.slots, key)?.title : undefined
    return title && title !== key ? title : ''
  })

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
  // in App's shell grid; railWidthFor is the single source for both.
  useEffect(() => {
    setRailWidth(focusActive ? 0 : railWidthFor({ isMobile, collapsed: effectiveCollapsed }))
  }, [isMobile, effectiveCollapsed, focusActive])
  // The header's three grid tracks (see `.topbar` in index.css) size themselves:
  // the search width is a function of the window and the two side groups split
  // the remainder. Each group re-lays-out its own contents with container
  // queries on both forms; on desktop (`tb-measured`) useTopbarCollapse also
  // reads the contents through the group's `.tb-measure` wrapper and adds folds
  // wherever the container rungs still overflow. The drag-region reporter
  // addresses the header itself and the layout tests match the group classes,
  // so no cluster ref is kept for either form.
  const closeMobileNav = isMobile ? closeMobileNavDrawer : undefined
  const { activePath, libraryNavActive, discoverNavActive, isChat, needsFixedHeight, navRowActive } =
    useRouteActiveModel(location.pathname, location.search, advertisedNavItems)
  // Browser tab title, detail first like the popout windows
  // (`{{label}} — {{productName}}`): `[(N) ]<session> — Chat — <bot>` on chat,
  // `[(N) ]<panel> — <bot>` on any other rail destination, bare `<bot>` when
  // the route matches no row. The count stays in front so it survives tab
  // truncation. Labels are resolved here, at render, so a language switch
  // re-titles the tab.
  const chatPanelLabel = i18nT('app.tab_title_chat')
  const titlePanelRow = isChat ? null : [...advertisedNavItems, ...sortedAppGroup].find(n => navRowActive(n.path))
  const titleLabel = isChat
    ? (activeSessionTitle ? i18nT('app.tab_title_session', { session: activeSessionTitle, panel: chatPanelLabel }) : chatPanelLabel)
    : (titlePanelRow ? surfaceLabel(titlePanelRow) : '')
  useEffect(() => {
    const base = titleLabel ? i18nT('app.tab_title', { label: titleLabel, productName: botName }) : botName
    document.title = totalAttention > 0 ? `(${totalAttention}) ${base}` : base
  }, [totalAttention, botName, titleLabel])
  // Name the history entry on screen for the Back/Forward arrows' history
  // menu (#17453): the session on chat, the page anywhere else, and nothing on
  // a route with no rail row, so that entry is left out of the list. Re-run on
  // every entry, because the name belongs to the entry, not to the route.
  const historyLabel = isChat ? (activeSessionTitle || chatPanelLabel) : titleLabel
  useEffect(() => {
    labelCurrentRouteEntry(historyLabel)
  }, [historyLabel, location.key])
  // Phone chat page: the header is ONE bar for both the shell and the
  // conversation. The chat page fills `#mobile-topbar-slot` (sessions toggle,
  // session title + menu) and `#mobile-topbar-trail-slot` (its overflow menu)
  // through portals, and the shell keeps only the crew switcher and the bell.
  // The nav drawer's logo trigger, the readout capsule and the search square
  // are not rendered here: search and the main destinations live in the rail
  // the chat page's sessions drawer shows (see `mobileNavRail` in App.tsx).
  //
  // The phone Crewmates page joins it while a crewmate or a team is open: its
  // header (drawer toggle, identity pill, panel toggle) moves into the same
  // slots, and the rail lives in its roster drawer. Keyed on the URL naming one,
  // and on a bare `/members` until the page reports its roster: that page has
  // no drawer and still needs the bar's logo to reach the navigation, but a
  // bare visit that is about to open a crewmate must not flash the logo row.
  const membersRoute = activePath === '/members' || activePath.startsWith('/members/')
  const mobileSingle = isMobile && (isChat || membersSingleBar)
  // The phone Crewmates single bar before the page has a crewmate open: the
  // page has no drawer bound yet, so the bar carries a nav-drawer toggle.
  const membersBarPending = mobileSingle && onMembersRoute && membersBar === 'pending'
  const mobileNavOpen = mobileNavPhase === 'open'

  return {
    isEmbed, isPopout, initialPopoutPath, appPopoutRoute, dispatch, connected, updateAvailable, authRequired, terminalEnabled,
    gatewayStatusMsg, focusModeChord, focusModeExitMsg, focusModeExitMsgVisible, focusChromeInert,
    terminalPoppedOut, bottomTerminalOpen, mobileConnect, mobileConnectOpen, setMobileConnectOpen,
    hasRenderableMobileConnect, activeSlotProject, terminalPosition, navigate, mayLeaveForNavigation,
    colorTheme, resolvedMode, onboarded, privacyAcked, markOnboarded, markImportOnboarded, markPrivacyAcked,
    firstRun, lookPreviewFrame, branding, botName, avatar, isMobile, bottomDock, activeInstanceId,
    macFullscreen, macInset, topReservePx, focusMode, toggleFocusMode, focusActive, topPeek, railPeek,
    topPeekTrigger, topPeekSurface, railPeekTrigger, railPeekSurface, topChromeShown, localHeaderDragGaps,
    shellEntered, setShellEntered, mobileNavMounted, mobileNavX, mobileNavPanelRef, mobileNavScrimRef, shellRef,
    mobileNavScrim, closeMobileNavDrawer, setMembersBar, advertisedNavItems, sortedAppGroup, activeAppDragId,
    appDndSensors, handleAppDragStart, handleAppDragEnd, handleAppDragCancel, APPS_NAV_LIMIT, appsExpanded,
    toggleAppsExpanded, slotOwners, appBadges, discoverBadges, railAppBadges, railAppRunStates, shortcutsOpen,
    setShortcutsOpen, toggleShortcutsModal, commandPalette, agentSwitchNotice, kiroUsageOpen, setKiroUsageOpen,
    kiroUsageState, usageSegmentHidden, kiroAccountEntry, metrics, capsuleCollapsed, setCapsuleCollapsed,
    capsuleLayoutPulse, pulseCapsuleLayout, sysMetrics, metricsProbeRef, metricsGroupRef,
    subscribeLogs, subscribeSubagents, forceReconnect, updateFlow, updating, setUpdating, showUpdateModal,
    setShowUpdateModal, showChangelog, updateError, setUpdateError, startupVideo, reportProblemOpen,
    setReportProblemOpen, requestFeature, toggleNav, effectiveCollapsed, closeMobileNav, activePath,
    libraryNavActive, discoverNavActive, isChat, needsFixedHeight, navRowActive, membersRoute, mobileSingle,
    membersBarPending, mobileNavOpen,
  }
}

/** What {@link useAppShell} hands `App` and the shell's views. */
export type AppShell = ReturnType<typeof useAppShell>
