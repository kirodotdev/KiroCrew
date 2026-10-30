import { useRef, useMemo, useState, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { useNavigate, useNavigationType, useLocation, useSearchParams } from 'react-router-dom'
import { useQueryClient, useQuery } from '@tanstack/react-query'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { useIsTouchDevice } from '../../../hooks/useIsTouchDevice'
import { useShellSlot } from '../../../hooks/useShellSlot'
import { useMobileNavRail } from '../../../components/MobileNavRailContext'
import { useVisualViewport } from '../../../hooks/useVisualViewport'
import { useAppDispatch, useAppSelector, useAppStore } from '../../../store'
import { useConnected } from '../../../hooks/useConnected'
import { useChatPopouts } from '../../../hooks/useChatPopouts'
import { mcpAppKey } from '../../../store/chatSlice'
import { useDeleteTerminalSession } from '../../../components/CliPanel'
import { useProvider } from '../../../providers'
import { useJevAutoSend } from '../useJevAutoSend'
import { useLanguageGeneration } from '../../../i18n/useLanguageGeneration'
import { useComposerArrivals } from '../../../utils/composerSendHolds'
import { useMoveSlotToFolder } from '../../../hooks/useMoveSlotToFolder'
import { setFileDraft } from '../../../utils/chatFileDrafts'
import type { ChatMessage, ChatSlot } from '../../../types'
import { crewModeView } from '../../../lib/crewMode'
import { useCrewMode } from '../../../hooks/usePreviewFlag'
import { useConnectionsUiEnabled } from '../../../hooks/useConnectionsUi'
import { isChatPageSurface } from '../../../utils/channelOrigin'
import { useGuidePredicate, useGuideSelection } from '../../../guide/guidePredicates'
import { fetchDashboardConfig } from '../../../api/dashboardConfigQuery'

/** The chat page's session rows: every chat-surface slot, a Crew Mode root's crew
 *  members INCLUDED. The page's active-slot rules read this list, and a crew member
 *  opened from its root's Crew panel is the active chat in the main pane, so it
 *  must stay listed here or the page would drop it and pick another session. */
export function chatPageSlots<T extends ChatSlot>(slots: T[]): T[] {
  return slots.filter(s => isChatPageSurface(s.surface ?? s.mode))
}

/** The person's rows of that list: crew members left out (`crewModeView`; the
 *  server keeps them in the slot frame under `?crew=all`, and their root's Crew
 *  panel shows them). What the collapsed-sidebar flyout lists and the unread count
 *  counts; the sidebar applies the same function at its own door. */
export function personChatPageSlots<T extends ChatSlot>(slots: T[]): T[] {
  return crewModeView(slots)
}

/** The unread keys among *visible* rows: a crew member's unread never counts on
 *  the person's surface, because it is not one of its rows. */
export function surfaceUnreadKeys(unread: string[], visible: readonly { key: string }[]): string[] {
  if (unread.length === 0) return []
  const visibleKeys = new Set(visible.map(s => s.key))
  return unread.filter(k => visibleKeys.has(k))
}

/** Stable empty set so the mcpApps-derived selector returns a referentially
 *  equal value when the slot has no app renders (avoids useless re-renders). */
const EMPTY_APP_ID_SET: ReadonlySet<string> = new Set()

interface ChatPageEnvironmentOptions {
  embedded?: boolean
  embedMode?: 'chat' | 'sessions'
  popout?: boolean
}

/**
 * What the page reads from its surroundings before anything else runs: the router,
 * the store and query client, the provider, the viewport (phone, touch, the visual
 * viewport and the keyboard inset), the shell's phone top-bar slots and nav rail, the
 * session roster this surface lists, the active slot and the guide facts about it, and
 * the dashboard config that decides whether MCP Apps open in the side panel.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useChatPageEnvironment({
  embedded, embedMode, popout,
}: ChatPageEnvironmentOptions) {
  const dispatch = useAppDispatch()
  const moveSlotToFolder = useMoveSlotToFolder()
  const navigate = useNavigate()
  const navigationType = useNavigationType()
  const location = useLocation()
  const queryClient = useQueryClient()
  const provider = useProvider()
  // Kills a Run-in-terminal tab's backend PTY when the dispatch rolls back
  // (see useChatEventBridges' mc:run-in-terminal handler). Routed through the
  // same mutation the tab-close paths use (per the use-react-query guideline);
  // read through a ref because that handler's effect deliberately registers once.
  const deleteTerminalSession = useDeleteTerminalSession()
  const deleteTerminalSessionRef = useRef(deleteTerminalSession)
  deleteTerminalSessionRef.current = deleteTerminalSession
  const [searchParams, setSearchParams] = useSearchParams()
  // Declared with the other top-of-component hooks because the ?sid= URL-sync
  // effect reads it (mobile replaces rather than pushes a session switch), and
  // ChatPage defines that effect well before the layout hooks start.
  const isMobile = useIsMobile()
  // Width alone cannot tell a phone from a narrow desktop pane, which still has
  // Enter: only a narrow TOUCH device forces Ctrl+Enter over the saved choice.
  const isTouch = useIsTouchDevice()
  /**
   * Phone: the App shell's top bar is the ONE bar, and this page's title row
   * lives in it. The shell renders `#mobile-topbar-slot` (centre cell) and
   * `#mobile-topbar-trail-slot` (after the bell) only on the chat route below
   * the breakpoint; this page fills them through `createPortal` — the same
   * hand-off `#activity-bar-slot` uses on desktop. Both resolve to `null`
   * anywhere the shell does not render them (the popout window, an embedded
   * host, a desktop width), and the page then keeps its own inline title row
   * — so a missing slot degrades to today's layout rather than to no title.
   *
   * `mobileNavRail` is the shell's main navigation as an icon rail, rendered
   * beside the sessions pane inside this page's ONE drawer (see the
   * OverlayDrawer SessionsSidebarChrome renders). `null` off the phone chat route.
   */
  const singleBar = isMobile && !embedded && !popout
  const topbarSlot = useShellSlot('mobile-topbar-slot', singleBar)
  const topbarTrailSlot = useShellSlot('mobile-topbar-trail-slot', singleBar)
  const mobileNavRail = useMobileNavRail()
  /**
   * Whether the title row is in the shell's bar rather than inline. Derived
   * from the slot ELEMENT, not from `isMobile`: the two can disagree for a
   * render on a breakpoint crossing, and the row must be in exactly one place
   * — the inline copy stands down only once the portal target exists, and the
   * transcript's header spacer, the fixed sessions button and the split view's
   * inline toggle all key off this same flag so none of them doubles up.
   */
  const titleInTopbar = topbarSlot !== null
  // The page's memoized composer props hold i18nT labels; their memo re-keys on a catalog load.
  const langGen = useLanguageGeneration()
  // The mobile sessions drawer and its scrim are `fixed` overlays that autofocus
  // a search input, so a software keyboard is open whenever they are. iOS Safari
  // shrinks only the VISUAL viewport for the keyboard (`interactive-widget`
  // default `resizes-visual`), so `fixed inset-0` / `top-safe`/`bottom-safe`
  // insets keep measuring the full layout viewport and strand the drawer's lower
  // content behind the keyboard. Inset both boxes to the visible band instead —
  // only on mobile; desktop morph mode is untouched.
  const vv = useVisualViewport()
  // Height of the band an interactive widget covers at the bottom of the fixed
  // positioning viewport — the same expression SidePanelLayout derives its own
  // keyboard inset from. 0 on desktop and whenever no keyboard is up, so every
  // consumer is a strict no-op at rest.
  const keyboardInset =
    typeof window === 'undefined' ? 0 : Math.max(0, window.innerHeight - vv.offsetTop - vv.height)
  const slots = useAppSelector(s => s.dashboard.slots)
  // A user-facing switch gesture hit a session the server no longer has
  // (#6372); rendered through the pane ErrorNotice in ChatPage (errors-use-error-notice).
  const switchSlotGone = useAppSelector(s => s.chat.switchSlotGone)
  // Unified chat view: show default and crew slots together, crew members included
  // (`chatPageSlots`), so a crew member opened in the main pane stays the active
  // slot. App-owned worker slots (s.app) are excluded by the sidebar itself.
  const filteredSlots = useMemo(() => chatPageSlots(slots), [slots])
  // The person's rows: what the collapsed-sidebar flyout lists and the unread
  // count counts. The sidebar filters its own prop the same way.
  // Keyed on the Crew Mode preview too, which decides whether crew members are
  // left out: off, `crewModeView` hands the rows back unchanged.
  const crewMode = useCrewMode()
  const personSlots = useMemo(() => (crewMode ? personChatPageSlots(filteredSlots) : filteredSlots), [filteredSlots, crewMode])
  const filteredSlotsRef = useRef(filteredSlots)
  filteredSlotsRef.current = filteredSlots
  // Live guide predicates the sessions sidebar toggle needs to be drawn (see
  // `guide/guidePredicates.ts`): a guide step through it shows a blocker
  // instead of pointing when either is unmet.
  useGuidePredicate('has_open_sessions', filteredSlots.length > 0)
  useGuidePredicate('full_dashboard', embedMode !== 'chat' && embedMode !== 'sessions')
  const unreadSlots = useAppSelector(s => s.dashboard.unreadSlots)
  // Unified view: unread keys for all chat-like slots.
  const surfaceUnreadSlots = useMemo(() => surfaceUnreadKeys(unreadSlots, personSlots), [unreadSlots, personSlots])
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const connected = useConnected()
  // Create-in-flight, so the flyout's New button can go inert exactly like the
  // sidebar's does instead of accepting a second click.
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
  // Read synchronously by the slot-change effect, so held in a ref updated every
  // render. The same reducer case sets it and `activeSlot`, so the render that
  // sees the new active slot sees this too.
  const foregroundCreateId = useAppSelector(s => s.chat.foregroundCreateId)
  const foregroundCreateIdRef = useRef(foregroundCreateId); foregroundCreateIdRef.current = foregroundCreateId
  const lastCreatedActivation = useAppSelector(s => s.chat.lastCreatedActivation)
  const lastCreatedActivationRef = useRef(lastCreatedActivation); lastCreatedActivationRef.current = lastCreatedActivation
  // The one post-resolve answer for every resume entry point (#5925); rendered
  // above the composer, which is the only place all of them can see.
  const unresumableResume = useAppSelector(s => s.chat.unresumableResume)
  const undeletableHistory = useAppSelector(s => s.chat.undeletableHistory)
  const activeSlot = useAppSelector(s => s.chat.activeSlot)
  // The session picker's selection fact for a guide's "choose the session"
  // step: whether one is open here, whether any is listed, and which one by
  // its key, so a choice made there stays bound to that session. The key
  // stays in this tab: only the selection's state is reported.
  const sessionPicked = !!activeSlot && filteredSlots.some(s => s.key === activeSlot)
  useGuideSelection('session_open', {
    selected: sessionPicked,
    available: filteredSlots.length > 0,
    identity: sessionPicked ? activeSlot ?? undefined : undefined,
  })
  // The store this page is rendered under (not the module singleton): the
  // opener reads live state after an await, and it must be the same store
  // its dispatches went to. Also read by the MCP-app openers ChatPage wires later, so it is
  // declared ahead of the auto-open effect.
  const boundStore = useAppStore()
  // Reveal eligible completed replies while recovery is offered, including an
  // older reply the user chose to read aloud. Slot identity prevents bleed-over.
  const [voiceRecoverySlot, setVoiceRecoverySlot] = useState<string | null>(null)
  // tool_call_ids in THIS slot that have a live MCP App render payload. Passed
  // to TurnBlock so app-bearing rows (which mount an interactive iframe) never
  // fold into a collapsible pane — collapsing hides the app, and re-expanding
  // remounts the iframe and loses in-canvas state. Kept here rather than inside
  // TurnBlock because that component is also rendered by app-sdk/ChatEmbed with
  // no Redux Provider mounted. The custom equality fn keeps the derived Set
  // referentially stable across unrelated chat-state updates.
  const appToolCallIds = useAppSelector(s => {
    const apps = s.chat.mcpApps
    if (!activeSlot || !apps) return EMPTY_APP_ID_SET
    const prefix = mcpAppKey(activeSlot, '')
    const ids = Object.keys(apps).filter(k => k.startsWith(prefix)).map(k => k.slice(prefix.length))
    return ids.length ? new Set(ids) : EMPTY_APP_ID_SET
  }, (a, b) => a.size === b.size && [...a].every(id => b.has(id)))
  // MCP Apps in the side panel (dashboard.mcp_app_panel, opt-in). When on, a new
  // render opens the panel to its own `app` tab instead of drawing inline in the
  // bubble — same auto-open path the web-preview marker uses.
  const { data: appPanelCfg, isError: appPanelCfgError } = useQuery<{ mcp_app_panel?: boolean; auto_open_git_panel?: boolean; side_panel_always_open?: boolean; side_panel_pin_summary?: boolean }>({
    queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000,
  })
  const mcpAppPanel = appPanelCfg?.mcp_app_panel === true
  // Opt-in: expand the side panel to the Git tab on sight of a git project
  // (dashboard.auto_open_git_panel). See the git-panel effect for why it is off
  // by default.
  const autoOpenGitPanel = appPanelCfg?.auto_open_git_panel === true
  // Whether that value is KNOWN yet. The git effect consumes a one-shot
  // localStorage marker, so acting while this query is still in flight would
  // burn the marker with the flag reading false and an opted-in user would never
  // get the panel. A FAILED query counts as known and resolves to the documented
  // default (off) — otherwise a config endpoint that is down would withhold the
  // Git tab itself, which the flag does not govern.
  const autoOpenGitPanelKnown = appPanelCfg !== undefined || appPanelCfgError
  // Opt-in side panel settings (Settings → Chat → Side panel): open the panel in
  // every chat, and pin Summary first in it. Strict reads, so a failed or
  // pending config read leaves both off, which is what an unconfigured install
  // gets. The page does not report that off to the store as the user's setting
  // (see `sidePanelAlwaysOpenKnown`).
  const sidePanelAlwaysOpen = appPanelCfg?.side_panel_always_open === true
  const sidePanelPinSummary = appPanelCfg?.side_panel_pin_summary === true
  // Whether the read has returned data, which is when the page reports Keep side
  // panel open to the store. Unlike `autoOpenGitPanelKnown`, a FAILED read with
  // no data does not count: the store would keep its off as the user's setting,
  // so a later answer that is on would look like a turn-on and open a panel the
  // user closed meanwhile. A failed refetch keeps the data it had, which still
  // counts.
  const sidePanelAlwaysOpenKnown = appPanelCfg !== undefined
  return {
    dispatch, moveSlotToFolder, navigate, navigationType, location, queryClient, provider, deleteTerminalSessionRef, searchParams, setSearchParams, isMobile, isTouch, topbarSlot, topbarTrailSlot,
    mobileNavRail, titleInTopbar, langGen, vv, keyboardInset, slots, switchSlotGone, filteredSlots, filteredSlotsRef, personSlots, surfaceUnreadSlots, refreshTrigger, connected, creatingSlot, foregroundCreateId,
    foregroundCreateIdRef, lastCreatedActivationRef, unresumableResume, undeletableHistory, activeSlot, boundStore, voiceRecoverySlot, setVoiceRecoverySlot, appToolCallIds, mcpAppPanel,
    autoOpenGitPanel, autoOpenGitPanelKnown, sidePanelAlwaysOpen, sidePanelAlwaysOpenKnown, sidePanelPinSummary,
  }
}

interface PageFlagsOptions {
  activeSlot: string | null
  messages: ChatMessage[]
  splitMode: boolean
  fileDrafts: MutableRefObject<Record<string, string[]>>
  saveDrafts: () => void
  prevSlot: MutableRefObject<string | null>
  activeSlotRef: MutableRefObject<string | null>
  composerSlotRef: MutableRefObject<string | null>
  pendingFilesRef: MutableRefObject<string[]>
  setPendingFiles: Dispatch<SetStateAction<string[]>>
}

/**
 * The dashboard-config switches this page reads (split view, link previews, social
 * share, Auto (Jev) send, the Connections gallery), the composer's file arrivals (held
 * while split view owns the composers), the pop-out state for the title bar, and the
 * plan task id the transcript carries.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function usePageFlags({
  activeSlot, messages, splitMode, fileDrafts, saveDrafts, prevSlot, activeSlotRef, composerSlotRef, pendingFilesRef, setPendingFiles,
}: PageFlagsOptions) {
  const { data: dashCfg } = useQuery<{ quick_send?: boolean; session_grid?: boolean; link_previews?: boolean; social_share_enabled?: boolean }>({ queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000 })
  // Session grid (split view) is an opt-in feature flag (Settings › Chat › Split View). Gates ⌘D, the Columns2 button, and the grid render.
  const splitFeatureEnabled = dashCfg?.session_grid === true
  // ChatPage shows the slot only while its own composer is rendered. The ref is
  // written here, not only by the sync effect, because the producer releases
  // the hold right after this runs and a send in that gap reads the ref.
  useComposerArrivals(
    activeSlot,
    (paths, arrivalSlot) => {
      // activeSlotRef changes during render, so it names the session this page
      // commits before any layout-phase arrival can mutate the composer.
      if (arrivalSlot !== activeSlotRef.current) return false
      const merge = (previous: string[]) => [...previous, ...paths.filter(path => !previous.includes(path))]
      // The live list is the base only once the draft restore has run for this
      // slot (`prevSlot` advances in that effect). Before it, during a switch
      // commit or on a fresh mount, where the layout-phase drain runs ahead of
      // the passive restore, the live list is not yet this slot's, so the
      // arrival goes into the stored draft and the restore picks up both.
      const base = composerSlotRef.current === arrivalSlot && prevSlot.current === arrivalSlot
        ? pendingFilesRef.current
        : fileDrafts.current[arrivalSlot] ?? []
      const merged = merge(base)
      pendingFilesRef.current = merged
      setFileDraft(fileDrafts.current, arrivalSlot, merged)
      saveDrafts()
      setPendingFiles(merged)
      return true
    },
    !(splitMode && splitFeatureEnabled),
  )
  // Link previews are opt-in too (Settings › Chat › Link Previews): enabling them
  // lets this machine fetch every http(s) link the model emits. Hoisted to a
  // stable primitive so it can sit in the transcript renderer's dep list — flipping
  // the toggle has to re-render already-rendered messages, not just the next one.
  const linkPreviewsOn = dashCfg?.link_previews === true
  // "Share as image" is a governance-gated entry (`capabilities.social_share`),
  // not a preference: the server resolves the ceiling and reports it here, and the
  // entry stays hidden until it says true — the endpoint is the authority, the
  // frontend never guesses (same posture as the mobile-connect rail row).
  const socialShareOn = dashCfg?.social_share_enabled === true
  // Whether the split send button may offer `Auto (Jev)`: the fleet ceiling and
  // the owner's consent, both the gateway's answers (see useJevAutoSend).
  const jevAutoConsented = useJevAutoSend()
  // Connections cards own consent for the providers they render, so chat drops
  // the duplicate OAuth banner — but only while that gallery is reachable.
  const connectionsUiOn = useConnectionsUiEnabled()
  // Pop-out state for the title-bar control (shared singleton — same channel the menus use).
  const { isPoppedOut: isSlotPoppedOut, open: openActivePopout, focus: focusActivePopout, returnSelfToMain } = useChatPopouts()
  const activePoppedOut = !!activeSlot && isSlotPoppedOut(activeSlot)
  const planTaskId = useMemo(() => {
    for (const m of messages) {
      const match = m.content?.match(/<!-- plan_task_id:(\S+) -->/)
      if (match) return match[1]
    }
    return ''
  }, [messages])
  return {
    dashCfg, splitFeatureEnabled, linkPreviewsOn, socialShareOn, jevAutoConsented, connectionsUiOn, openActivePopout, focusActivePopout, returnSelfToMain, activePoppedOut, planTaskId,
  }
}
