import { batchCloseConfirm } from '../../components/TabCloseMenu'
import { useConfirm } from '../../components/ConfirmDialog'
import { useState, useRef, useEffect, useCallback, useMemo, Fragment, Suspense, lazy, type ReactNode } from 'react'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { AllTabsMenu } from '../../components/AllTabsMenu'
import { useSidePanelTabNav } from './SidePanelTabNav'
import { iconForKind } from './SidePanelTabIcons'
import { Reorder } from 'framer-motion'
import { Plus, MoreHorizontal, Globe, PanelRight, PanelBottom } from 'lucide-react'
import { SidePanelDockHost, SidePanelGlyph } from '../../components/SidePanelGlyph'
import ActivityViewer from './ActivityViewer'
// Loaded with its tab, not the shell: the panel (attention cards, tile lists,
// the session card frame) is only mounted once a Dashboard tab exists.
const CommandCenterPanel = lazy(() => import('./command-center/CommandCenterPanel'))
// Remote-environment sessions only.
const RemoteFilesPanel = lazy(() => import('./RemoteFilesPanel'))
import { CrewWorkersTab } from './command-center/CrewWorkerList'
import FilesHomePanel from './FilesHomePanel'
import { slotEnvironmentQueryKey, useSlotEnvironmentPlacement } from '../../components/sessionEnvironmentHooks'
import { disposeTerminalSession, useDeleteTerminalSession } from '../../components/CliPanel'
import { useIsFetching, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { useTerminalEnabled } from '../../utils/terminalRegistry'
import type { usePanelTabs, ViewKind, PanelTab, TabKind } from '../../hooks/usePanelTabs'
import { MAX_TERMINALS_PER_CHAT, offerPanelView, useAllAppTabs, usePanelTerminalsPending } from '../../hooks/usePanelTabs'
import { usePinnedViews } from './usePinnedViews'
import { usePanelTabDescriptors, isPanelTabKind, type PanelTabDescriptor } from '../../hooks/panelTabRegistry'
import { appIcon } from '../../apps/appIcons'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { useDiffSplit } from '../../hooks/useDiffSplit'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator
} from '../../components/ui/dropdown-menu'
import { Btn, ContentSkeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsLink } from '../../components/SettingsLink'
import type { ExternalTerminalFallbackNote } from '../../lib/externalTerminal'
import ErrorBoundary from '../../components/ErrorBoundary'
import { fetchFileRead } from '../../utils/fileReadQuery'
import { useAppSelector } from '../../store'
import { selectSlotSubagents, selectSlotToolLog } from '../../store/chatSlice'
import type { ExtractedLink } from '../../utils/extractChatLinks'
import type { PullRequestLink } from '../../utils/pullRequestLinks'
import type { ChatPin } from '../../api/pins'

import { i18nT } from '../../i18n/t'
import { uiLocation } from '../../uiLocations/uiLocation'
import { AddMenuItem, NEW_MENU_DESC_KEY, NEW_MENU_LABEL_KEY, newMenuSections, type SidePanelWithholdable } from './SidePanelAddMenu'
import { DraggableTabItem, TabChip } from './SidePanelTabChip'
import { AppPanelTabBody, AppPanelTabsErrorNotice, McpAppTabBody, TabBody } from './SidePanelTabBodies'
import { useSidePanelSize } from './useSidePanelSize'

// The panel's parts live in sibling owners that never import this module:
// `SidePanelAddMenu.tsx` (the + menu's catalog and gates), `SidePanelTabChip.tsx`
// (one strip chip), `SidePanelTabBodies.tsx` (the kept-mounted tab bodies) and
// `useSidePanelSize.ts` (the panel's size). Callers keep importing every name
// exported below from this module.
export { NEW_MENU_DESC_KEY, NEW_MENU_LABEL_KEY, newMenuSections }
export type { SidePanelWithholdable }
export { CHAT_PANE_MIN_W, SIDE_PANEL_MIN_W, SIDE_PANEL_RESERVED_W, sidePanelEffectiveWidth, sidePanelFillWidth } from './useSidePanelSize'

const VIEW_KINDS = new Set<TabKind>(['changes', 'issues', 'links', 'files', 'artifacts', 'subagents', 'workflows', 'logs', 'crewlog', 'context', 'side', 'git', 'summary', 'pins'])

export interface SidePanelLeadingTab {
  /** Stable id — the value `usePanelTabs` stores as `activeId` while this tab is
   *  focused. Must not collide with a `TabKind` (`'summary'` is the chat's
   *  session-summary view; the Crewmates page uses `'crew-notes'`,
   *  `'crew-work-log'` and `'crew-dashboard'`). */
  id: string
  title: string
  /** Strip glyph. The host owns it — these are the chips whose icon `KIND_ICON`
   *  does not own. */
  icon: ReactNode
  /** Preserve a visited body that owns drafts or a published iframe. */
  keepMounted?: boolean
  /** Body; query-only views normally mount just while active. */
  render: () => ReactNode
  /** Optional count / status pill after the label, for a tab whose headline
   *  fact is worth reading WITHOUT opening it (the Crewmates page's Schedules
   *  count). The host owns the node so it can distinguish an answer of none
   *  from an unreadable list; omit it rather than passing a zero that would
   *  assert "none" about a list that failed to load. */
  badge?: ReactNode
  /** Asked before the strip switches AWAY from this tab, for a body that holds
   *  unsaved work. Only the ACTIVE tab's hook is consulted, and answering false
   *  refuses the switch.
   *
   *  It exists because only the active leading tab's body is mounted, so every
   *  other chip is a destruction path for an editor living in this one — and the
   *  host cannot see a chip click to guard it. Opt-in: a tab that declares
   *  nothing behaves exactly as before, which is every tab but the Crewmates
   *  page's Schedules.
   *
   *  May answer asynchronously, because the honest answer is usually a themed
   *  confirm dialog (`useConfirm`) rather than a synchronous guess. */
  onBeforeLeave?: () => boolean | Promise<boolean>
}

interface SidePanelProps {
  tabsCtl: ReturnType<typeof usePanelTabs>
  slot: string
  /** Stable identity of the chat the panel shows, held constant while the host
   *  is still confirming its `slot` (the Members page passes the member's name,
   *  since its slot stays '' until the thread POST answers). A resize drag that
   *  starts on an empty slot is saved under the key confirmed mid-drag ONLY when
   *  this identity is the same at release as at the start, so the key is that
   *  chat's own. Unset, or changed mid-drag (a keyboard switch to another chat),
   *  an empty-start drag is saved to the shared default alone. */
  slotOwner?: string
  /** The key the host has CONFIRMED is this chat's own, when that can differ
   *  from `slot`. `slot` is the bodies' identity and may hold a stale key the
   *  endpoint has since refused (the Members page keeps its last good key
   *  through a 409 so live bodies are not re-keyed). A released drag is saved
   *  under a per-chat key only when that key is the confirmed one at the start
   *  or at the release; otherwise it goes to the shared default alone, so a
   *  refused chat never writes into the session that owns the key. Unset, the
   *  panel treats `slot` as confirmed. */
  persistSlot?: string
  onFileOpen?: (path: string, opts?: { replaceId?: string; line?: number; endLine?: number; diffMode?: boolean; canReplace?: () => boolean }) => void
  /** Open a file's WORKING-TREE DIFF as its OWN tab (keyed `wtdiff:<path>`),
   *  coexisting with the plain file tab instead of flipping it into diff mode
   *  (#9695). The Files > Changed segment's diff control routes here. Both
   *  standing hosts — ChatPage AND MembersPage — supply it. A host that does
   *  not (e.g. a lightweight embedding) falls back to flipping the file tab via
   *  onFileOpen, the documented pre-#9695 behaviour the fallback test pins. */
  onOpenWorkingTreeDiff?: (path: string) => void
  /** Open an artifact as a panel tab (the artifact twin of onFileOpen).
   *  Threaded to the Artifacts tab so its rows open here instead of
   *  hard-navigating to the standalone detail page. */
  onArtifactOpen?: (slug: string) => void
  /** Right-click "Add to context" on a file-browser row: forwards the ABSOLUTE
   *  path and whether it is a file or a directory to the composer host, which
   *  inserts the same `@`-mention the file picker does. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  /** Put a reply a dashboard page offered into this chat's composer; the person sends it. */
  onComposerInsert?: (text: string) => void
  projectDir?: string
  navLinks?: ExtractedLink[]
  navResolving?: boolean
  sources?: PullRequestLink[]
  selectedSourceUrl?: string
  onSelectSource?: (url: string) => void
  onReconcileSource?: (url: string) => void
  /** Issue links mentioned in this session (the `kind: 'issue'` half of the
   *  extractor's output). Separate props — not a merged list — so the Changes
   *  and Issues tabs each keep their own selection. */
  issues?: PullRequestLink[]
  selectedIssueUrl?: string
  onSelectIssue?: (url: string) => void
  onReconcileIssue?: (url: string) => void
  onAddSourceToChat?: (text: string) => void
  onSubmitComments?: (message: string, count?: number) => void | boolean | Promise<void | boolean>
  /** Gateway connection flag — forwarded to document tab bodies to gate
   *  their submit-comments-to-chat affordances while offline. */
  connected?: boolean
  /** Pinned messages for this session, plus the two actions the Pins tab needs.
   *  Prop-drilled rather than re-queried here because the JUMP is ChatPage's:
   *  landing on a pin that is not in the loaded window has to page older
   *  history in, which only ChatPage's transcript state can drive. */
  pins?: ChatPin[]
  pinsLoading?: boolean
  onJumpToPin?: (messageTs: string, mid?: string) => void
  onUnpin?: (id: string) => void
  /** Only shape the copyable deep link a pin row offers. */
  slotTitle?: string
  chatMode?: string
  onFileSave: (filePath: string, content: string) => Promise<void>
  /** Close the whole panel (hides the side column). ABSENT means the panel is
   *  permanent: no close control renders in the strip and Escape inside a view
   *  does nothing. A host whose panel the user opens and dismisses passes it:
   *  ChatPage, and the Crew Members page in both placements (docked, it hides
   *  the column; as a narrow-window overlay, it dismisses the drawer). */
  onClose?: () => void
  /** HOST-OWNED tabs pinned AHEAD of the pinned views, in strip order:
   *  non-closable, not draggable, never in the + menu, and not stored in the
   *  tab bucket — the host renders each body. The Crew Members page uses one
   *  (Dashboard). Their ids must not collide with a
   *  `TabKind`, and the same ids must be handed to `usePanelTabs` as
   *  `leadingIds` so a fresh strip opens on the first one and focus can fall
   *  back to it. Always labelled: several icon-only chips would be unlabelled
   *  navigation. */
  leadingTabs?: readonly SidePanelLeadingTab[]
  /** Extra px the panel must keep clear to its left, on top of the shell's
   *  own reserve (the live nav rail width plus `CHAT_PANE_MIN_W`). A host with
   *  more siblings in the row -- the chat page's session sidebar, the Members
   *  page's roster column -- passes their live width so a drag can never fold
   *  the pane beside the panel to nothing. */
  extraReserveW?: number
  /** Resting width before the user has ever dragged the handle. The chat page
   *  keeps the built-in 460; the Crewmates page rests at 60% of its row. */
  defaultWidth?: number
  /** Views this host WITHDRAWS from the strip: dropped from the pinned block
   *  and the + menu alike (a stored tab of such a kind is left in the bucket,
   *  just not offered). For a host that cannot feed a view's data — the
   *  Members page has no transcript link index or pins query — an empty view
   *  would assert "nothing here", which is false, so the view is withheld until
   *  the host can populate it. `'terminal'` may be withheld too: a terminal
   *  opened while the host's strip is still keyed provisionally would be
   *  orphaned (live PTY, unreachable tab) when the strip re-keys. `'app'`
   *  withholds EVERY app-contributed tab (`contributes.panelTabs`) for the same
   *  reason: its `AppHost` body holds the app's own unsaved state, which the
   *  re-key's remount would discard. */
  hiddenViews?: ReadonlySet<SidePanelWithholdable>
  /** Host-specific strip labels for singleton views, already localised (the
   *  Members page calls Files "Workspace": there it is the mate's own
   *  directory). Only the displayed title changes; the tab id stays the kind. */
  viewTitles?: Partial<Record<ViewKind, string>>
  /** Pin Summary (`dashboard.side_panel_pin_summary`); see `usePinnedViews`. */
  pinSummary?: boolean
  /** Reports the tab the strip actually SHOWS whenever it changes — the stored
   *  focus, or the fallback the panel resolves when that focus is on a withheld
   *  or absent tab (the leading tab, else the first visible one). A host that
   *  keys its own work on the active tab (the Members page's summary reads)
   *  must listen here rather than read `tabsCtl.activeId`, which is the STORED
   *  focus and is deliberately left untouched by a withdrawal. */
  onActiveTabChange?: (id: string | null) => void
  /** Is the whole panel mounted-but-invisible? A live app or browser tab keeps
   *  the subtree mounted through a close (its iframe / WebContentsView cannot
   *  survive a remount), and the find pane hides it while owning the dock — so
   *  "panel closed" is a visibility state, not an unmount. Tab bodies that bind
   *  document-level keys need it: the SELECTED tab is still selected while the
   *  panel is hidden, so tab selection alone does not mean the user can see it. */
  panelHidden?: boolean
  /** Preview "focus" mode: when true the panel takes its maximum width (chat
   *  shrinks to its minimum), driven by the Web Preview tab's expand toggle. */
  expanded?: boolean
  /** FILL mode (set by ChatPage): an explicit px width covering the whole chat
   *  column, used when the space left after the nav rail and session sidebar
   *  cannot seat the panel BESIDE a usable chat pane. Overrides the responsive
   *  clamp and the user's persisted width, and retires the resize handle —
   *  there is nothing to resize against. Undefined = beside mode. */
  fillWidth?: number
  /** Whether this frame can host the bottom dock (the main App shell has a
   *  bottom grid row; embed/popout/artifact frames do not). When false, the
   *  panel always renders as the right column and the dock toggle is hidden,
   *  regardless of the global dock preference. */
  canDockBottom?: boolean
}

/**
 * Tabbed side panel. One strip holds singleton view tabs
 * (Changes / Files / Subagents / Workflows / Logs / Side / Terminal, opened
 * from +) and document tabs (file / diff / artifact, opened on demand — file
 * chips, the Files picker, artifact refs). Each tab renders its own body;
 * documents live as tabs instead of replacing the panel.
 */
export default function SidePanel({
  tabsCtl, slot, slotOwner, persistSlot, onFileOpen, onOpenWorkingTreeDiff, onArtifactOpen, onAddToContext, onComposerInsert,
  projectDir, navLinks, navResolving, sources, selectedSourceUrl, onSelectSource, onReconcileSource,
  issues, selectedIssueUrl, onSelectIssue, onReconcileIssue,
  onAddSourceToChat, onSubmitComments, connected = true, onFileSave, onClose, panelHidden,
  pins, pinsLoading, onJumpToPin, onUnpin,
  slotTitle, chatMode,
  expanded, fillWidth, canDockBottom = true,
  leadingTabs, extraReserveW = 0, defaultWidth = 460, hiddenViews: hostHiddenViews, onActiveTabChange, pinSummary = false,
  viewTitles,
}: SidePanelProps) {
  const { tabs, activeId: storedActiveId, openView, openPanelTab, openTerminal, openFolder, setActive, closeTab, patchTab, setOrder } = tabsCtl
  const hiddenViews = hostHiddenViews
  // A permanent panel has no close control and answers Escape with nothing —
  // the views' `onToggle` still needs a function, so it gets a no-op.
  const closable = !!onClose
  // App-contributed side-panel tabs from the installed-app manifests. Empty ⇒
  // the "+" menu and launcher show nothing extra and the strip renders no app tab.
  // A host that withholds `'app'` (the Members page before its thread is
  // confirmed) offers none of them either, whatever is installed.
  const allPanelTabDescriptors = usePanelTabDescriptors()
  const panelTabDescriptors = useMemo(
    () => (hiddenViews?.has('app') ? [] : allPanelTabDescriptors),
    [hiddenViews, allPanelTabDescriptors],
  )
  // EVERY app frame, every slot, rendered from one stable-keyed list below so a
  // chat switch cannot change a frame's React key and remount its iframe.
  const allAppTabs = useAllAppTabs()
  // Subscribed HERE rather than passed down from ChatPage. Both maps are
  // mutated per streamed sub-agent / tool chunk, and this panel is closed by
  // default — holding the subscription in ChatPage re-rendered the whole page
  // for data nothing was displaying. This component only mounts while the
  // panel is open, so the subscription now costs nothing when it is closed.
  const subagents = useAppSelector(s => selectSlotSubagents(s, slot))
  const toolLog = useAppSelector(s => selectSlotToolLog(s, slot))
  const { remote: remoteEnvironment, error: environmentError } = useSlotEnvironmentPlacement(slot)
  const terminalEnabled = useTerminalEnabled() && remoteEnvironment !== undefined
  const placementRef = useRef(remoteEnvironment)
  placementRef.current = remoteEnvironment
  // Whether to offer the Summary row at all. Read from the panel's OWN endpoint
  // and under the SAME query key the Summary tab uses, so this is one cheap
  // request per slot that doubles as that tab's prefetch rather than a second
  // source of truth. The endpoint is read-only and never triggers generation.
  //
  // Fails OPEN (`!== false`): while the flag is unknown the row is offered, so a
  // slow request can never hide a feature that IS enabled. The reverse default
  // would make the panel look missing, which is worse than the brief window it
  // would close.
  const { data: summaryMeta } = useQuery({
    queryKey: ['session-summary', slot],
    queryFn: () => api.sessionSummary(slot),
    // No slot (a host whose thread is not confirmed yet) ⇒ nothing to ask about.
    enabled: !!slot,
    staleTime: Infinity,
    retry: false,
  })
  const summaryEnabled = summaryMeta?.enabled !== false
  const { pinnedViews, summaryPinned } = usePinnedViews({
    tabsCtl, slot, panelHidden: !!panelHidden, pinSummary,
    summaryOn: summaryMeta ? summaryMeta.enabled === true : undefined, summaryWithheld: !!hiddenViews?.has('summary'),
  })
  // The + menu / empty-state launcher hide Terminal when the feature is
  // disabled server-side, and never list the pinned views (Changes / Files /
  // Artifacts, and Summary while Pin Summary holds it) — those are always
  // present in the strip.
  // A Crew Mode root or one of its workers: its panel offers Workers, and the
  // tab is put on its strip whenever the session is shown (a fresh browser, a
  // reload, a reopened browser tab). It is the tab the panel opens on until the
  // user picks another; a picked tab keeps focus. Closing it holds until the
  // next visit; the + menu brings it back at any time.
  const crewMode = useAppSelector(st => {
    const row = slot ? st.dashboard.slots.find(r => r.key === slot) : undefined
    return !!(row?.crew_mode_root || row?.crew_root)
  })
  useEffect(() => { if (crewMode && slot) offerPanelView(slot, 'workers') }, [crewMode, slot])
  const menuSections = newMenuSections({ terminalEnabled, summaryEnabled: summaryEnabled && !summaryPinned, hiddenViews, crewMode })
  // The empty-state launcher shows the same entries flat: its two-column grid
  // has nowhere to put a separator, but it must not disagree with the menu
  // about ORDER, so it reads the groups rather than its own list.
  const menuItems = menuSections.flatMap(section => section.items)
  // Split the strip: pinned (fixed, non-closable) vs. dynamic (draggable).
  // A host withdrawal (`hiddenViews`) also applies to tabs ALREADY in the bucket:
  // the strip is keyed per slot and a member's DM slot can have been opened on the
  // chat page, which stores views (Pins, Issues…) this host cannot feed. Such a
  // tab is left in storage (it returns when the chat page opens the slot) but is
  // neither rendered as a chip nor given a body here, and focus on it falls back
  // to the leading tab — otherwise the withdrawal would hold only for a fresh
  // strip, which is not what the feature map promises.
  const isWithheld = useCallback((kind: TabKind): boolean => {
    if (!hiddenViews) return false
    if (kind === 'terminal') return hiddenViews.has('terminal')
    if (kind === 'app' || isPanelTabKind(kind)) return hiddenViews.has('app')
    // Document tabs are not views themselves. Every one of them — a file, diff,
    // working-tree diff or folder editor, an artifact preview — is slot-bound,
    // so all of them are withheld together, keyed on 'files': a host that
    // withholds the Files view is withholding slot-bound content as such (the
    // Crewmates page's unconfirmed window), and a persisted document tab must
    // not stay on the strip — and stay ACTIVE — while every slot-bound view is
    // withdrawn. Deliberately NOT keyed on the document's own LIST view: a host
    // may withdraw the Artifacts list and still open one artifact from a reply
    // link (the Crewmates page, #18320); tying the document to the list made
    // that tab appear and vanish in the same frame.
    if (kind === 'file' || kind === 'diff' || kind === 'wtdiff' || kind === 'folder' || kind === 'artifact') return hiddenViews.has('files')
    return hiddenViews.has(kind)
  }, [hiddenViews])
  // Restored terminal chips wait for the liveness ruling too, as the dock's do.
  const terminalsPending = usePanelTerminalsPending()
  // Every chip click routes through here so the tab being LEFT can refuse, which is
  // what `SidePanelLeadingTab.onBeforeLeave` is for: only the active leading tab's
  // body is mounted, so switching to any other chip destroys an editor living in it.
  // A tab that declares no hook takes the old path exactly, so this is inert for the
  // chat page and for every crewmate tab but Schedules.
  const leadingTabsRef = useRef(leadingTabs)
  leadingTabsRef.current = leadingTabs
  const requestActive = useCallback(async (id: string, currentId: string | null) => {
    if (id === currentId) return
    const leaving = leadingTabsRef.current?.find(t => t.id === currentId)
    if (leaving?.onBeforeLeave && !(await leaving.onBeforeLeave())) return
    setActive(id)
  }, [setActive])
  // Opening any OTHER tab leaves the active leading one just as clicking a chip does, so
  // the + menu, the launcher cards and an app-tab row ask the same question. Every path
  // that can make a different tab active runs through this.
  // Returns the active leading tab's hook, or null when there is nothing to ask. Callers
  // branch on null and stay SYNCHRONOUS: awaiting an answer nobody has to give would make
  // every tab-opening gesture on every host async, including the chat page, which has no
  // leading tabs at all.
  const activeLeadingIdRef = useRef<string | null>(null)
  const leavingHook = useCallback(() => {
    const leaving = leadingTabsRef.current?.find(t => t.id === activeLeadingIdRef.current)
    return leaving?.onBeforeLeave ?? null
  }, [])
  const visibleTabs = useMemo(() => {
    const shown = hiddenViews || terminalsPending
      ? tabs.filter(t => !isWithheld(t.kind) && !(terminalsPending && t.kind === 'terminal'))
      : tabs
    if (!viewTitles) return shown
    return shown.map(t => {
      const title = Object.prototype.hasOwnProperty.call(viewTitles, t.kind) ? viewTitles[t.kind as ViewKind] : undefined
      return title && title !== t.title ? { ...t, title } : t
    })
  }, [tabs, hiddenViews, isWithheld, terminalsPending, viewTitles])
  const activeId = useMemo(() => {
    if (storedActiveId === null) return null
    if (leadingTabs?.some(t => t.id === storedActiveId)) return storedActiveId
    if (visibleTabs.some(t => t.id === storedActiveId)) return storedActiveId
    return leadingTabs?.[0]?.id ?? visibleTabs[0]?.id ?? null
  }, [storedActiveId, visibleTabs, leadingTabs])
  // The fallback is REPORTED to the host, never written back into the store.
  // A host reads what the strip actually shows through `onActiveTabChange`
  // (the Crewmates page gates each leading tab's data reads on it), so a stored
  // focus on a withheld tab cannot leave a tab body on its loading placeholders
  // — while the stored focus itself survives. Writing the fallback into the
  // bucket would wipe it: a withdrawal can be TEMPORARY (the Members page
  // withholds every slot view for the moment its thread POST is in flight), and
  // a stored focus on Files must come back as Files once the views return.
  useEffect(() => { onActiveTabChange?.(activeId) }, [activeId, onActiveTabChange])
  activeLeadingIdRef.current = activeId
  // Closing the panel destroys the active tab's body exactly as switching away from it
  // does -- the host stops mounting the panel at all -- so it asks the same question a
  // chip click asks. Without this the close control and the narrow-viewport scrim were
  // the one way out that dropped an unsaved schedule draft with no confirm.
  const closePanel = useCallback(async () => {
    const leaving = leadingTabsRef.current?.find(t => t.id === activeId)
    if (leaving?.onBeforeLeave && !(await leaving.onBeforeLeave())) return
    onClose?.()
  }, [onClose, activeId])
  const pinnedTabs = useMemo(() => visibleTabs.filter(t => (pinnedViews as readonly string[]).includes(t.id)), [visibleTabs, pinnedViews])
  const dynamicTabs = useMemo(() => visibleTabs.filter(t => !(pinnedViews as readonly string[]).includes(t.id)), [visibleTabs, pinnedViews])
  // Edge cues for the dynamic session-tab group — the real overflow site when
  // many sessions are open. It hides its scrollbar, so a gradient is the only
  // signal that tabs continue past the clipped edge. remeasure on the tab
  // lists (the scroller keeps its box as tabs open/close/reorder).
  const [attachTabEdges, tabEdges, remeasureTabEdges] = useScrollEdges<HTMLUListElement>()
  useEffect(() => { remeasureTabEdges() }, [dynamicTabs, pinnedTabs, remeasureTabEdges])
  // An active dynamic tab (Workers, a file, a terminal) is the panel's heading:
  // the idle pinned chips drop to their icons sooner (`data-dynamic-active`,
  // sidebar-and-side-panel.css) and the active chip is scrolled fully into view
  // on open and on resize, so its label is never left clipped under a fade.
  const dynamicActive = useMemo(() => dynamicTabs.some(t => t.id === activeId), [dynamicTabs, activeId])
  const dynamicWrapRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    const wrap = dynamicWrapRef.current
    if (!wrap || !dynamicActive) return
    const reveal = () => {
      const chip = wrap.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
      chip?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
    }
    reveal()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(reveal)
    observer.observe(wrap)
    return () => observer.disconnect()
  }, [activeId, dynamicActive, dynamicTabs.length])
  // The pinned group scrolls under the same scrollbar-none, and its own comment
  // notes it overflows at 320px once a host prepends leading chips — so it
  // needs the same cue. Separate hook: it is a different scroller, and the two
  // groups clip independently. remeasure when the leading/pinned chips change.
  const [attachFixedEdges, fixedEdges, remeasureFixedEdges] = useScrollEdges<HTMLDivElement>()
  useEffect(() => { remeasureFixedEdges() }, [leadingTabs, pinnedTabs, remeasureFixedEdges])
  const glyphFor = useCallback((t: PanelTab) => iconForKind(t.kind, allPanelTabDescriptors), [allPanelTabDescriptors])
  const tabNav = useSidePanelTabNav({
    leadingTabs, pinnedTabs, dynamicTabs, activeId, requestActive, glyphFor, attachFixedEdges, attachTabEdges, projectDir,
  })
  // Terminal opens a NEW tab (its own PTY session) starting in the chat's
  // working dir; every other menu item is a singleton view.
  // Spawn a terminal whose cwd is the chat's project directory. Shared with the
  // Files header's per-project quick action (issue #1142) so the two entry
  // points cannot drift on WHERE the shell starts — that cwd is the whole point
  // of the affordance.
  const [workspaceTerminalCapped, setWorkspaceTerminalCapped] = useState(false)
  const openProjectTerminal = useCallback(() => {
    // A remote session's terminal is a shell in its workspace, opened by the
    // gateway over `coder ssh`; unread placement is never permission to run
    // on the gateway. Read again after a pending leave confirmation.
    const placement = placementRef.current
    if (placement) {
      // At the per-chat cap with no workspace terminal to reuse, openTerminal
      // opens nothing and returns ''; say so instead of doing nothing.
      setWorkspaceTerminalCapped(!openTerminal({ remoteSlot: slot, title: placement.name }))
    } else if (placement === null) openTerminal({ cwd: projectDir })
  }, [openTerminal, projectDir, slot])
  // The note an "Open in <app>" fallback leaves (see the banner below the tab
  // bodies). Held here rather than in the Files tab, which that fallback takes
  // out of view.
  const [externalTerminalNote, setExternalTerminalNote] = useState<ExternalTerminalFallbackNote | null>(null)
  const openMenuItem = useCallback((kind: ViewKind | 'terminal') => {
    const run = () => {
      if (kind === 'terminal') openProjectTerminal()
      else openView(kind)
    }
    const ask = leavingHook()
    if (!ask) { run(); return }
    void Promise.resolve(ask()).then(ok => { if (ok) run() })
  }, [openProjectTerminal, openView, leavingHook])
  const requestPanelTab = useCallback((d: PanelTabDescriptor) => {
    const ask = leavingHook()
    if (!ask) { openPanelTab(d); return }
    void Promise.resolve(ask()).then(ok => { if (ok) openPanelTab(d) })
  }, [openPanelTab, leavingHook])
  // Closing a terminal tab kills its PTY (server) and disposes local state. The
  // server delete goes through a React Query mutation (use-react-query
  // guideline); the synchronous WS + xterm teardown stays in disposeTerminalSession.
  // A rejected delete lands in the shared close-failed flag (set by the hook),
  // rendered by the always-mounted BottomTerminalPanel root — this tab is
  // already gone by then.
  const deleteTerminalSession = useDeleteTerminalSession()
  const handleCloseTab = useCallback((id: string) => {
    const t = tabs.find(x => x.id === id)
    if (t?.kind === 'terminal' && t.sessionId) {
      deleteTerminalSession.mutate(t.sessionId)
      disposeTerminalSession(t.sessionId)
    }
    closeTab(id)
  }, [tabs, closeTab, deleteTerminalSession])
  const { confirm, confirmDialog, confirmOpen } = useConfirm()
  const currentSlotRef = useRef(slot)
  currentSlotRef.current = slot
  // The live tab list, read AFTER the disk preflight's await so a buffer the
  // user edited during that round trip is re-evaluated, not closed from a stale
  // snapshot.
  const tabsRef = useRef(tabs)
  tabsRef.current = tabs
  const handleCloseTabs = async (targets: PanelTab[]) => {
    if (confirmOpen || targets.length === 0) return
    const shells = targets.filter(tab => tab.kind === 'terminal' && tab.sessionId).map(tab => tab.title)
    // A clean file tab whose on-disk copy is gone (or cannot be read) holds the
    // only remaining copy of its contents. The single × runs through
    // MarkdownPanel's last-copy guard, but a batch calls handleCloseTab
    // directly and would skip it, so the batch re-checks the disk for each
    // clean, non-empty file target and asks before discarding one whose copy it
    // cannot confirm. A partial (truncated) buffer is still the only surviving
    // prefix once its disk copy is gone, so it is preflighted too — only binary
    // buffers, which are an envelope rather than the file, are excluded. A
    // working-tree diff tab carries the file's full text too, and its single ×
    // runs the same guard, so it is preflighted alongside file tabs. Dirty
    // tabs are named separately below.
    const cleanFiles = targets.filter(tab =>
      (tab.kind === 'file' || tab.kind === 'wtdiff') && tab.content === tab.savedContent
      && !tab.binary && (tab.content ?? '') !== '' && !!tab.path)
    const lastCopy: PanelTab[] = []
    const unread: PanelTab[] = []
    await Promise.all(cleanFiles.map(async tab => {
      try {
        const r = await fetchFileRead(tab.path!)
        // 404 = gone from disk; any other non-ok (or a transport throw) is a
        // failed read that could not verify the copy. Either way the buffer may
        // be the only copy left, but only the first is a missing file.
        if (r.status === 404) lastCopy.push(tab)
        else if (!r.ok) unread.push(tab)
      } catch { unread.push(tab) }
    }))
    if (currentSlotRef.current !== slot) return
    // Re-resolve each target from the LIVE tab state after the await: a tab the
    // user edited during the disk read is now dirty and must be named, not
    // closed on the pre-read snapshot. A target that vanished mid-read drops out.
    const live = new Map(tabsRef.current.map(t => [t.id, t]))
    const liveTargets = targets.map(t => live.get(t.id)).filter((t): t is PanelTab => !!t)
    const dirty = liveTargets.filter(tab => tab.kind === 'file' && tab.content !== tab.savedContent)
    // A file that turned dirty during the read is no longer a silent last-copy
    // close — it is named under the unsaved-edits list instead.
    const dirtyIds = new Set(dirty.map(t => t.id))
    const lastCopyLive = lastCopy.filter(t => !dirtyIds.has(t.id))
    const unreadLive = unread.filter(t => !dirtyIds.has(t.id))
    const ask = batchCloseConfirm(
      dirty.map(tab => ({ name: tab.title, path: tab.path })),
      shells,
      lastCopyLive.map(tab => ({ name: tab.title, path: tab.path })),
      unreadLive.map(tab => ({ name: tab.title, path: tab.path })),
    )
    if (ask && !await confirm(ask)) return
    if (currentSlotRef.current !== slot) return
    liveTargets.forEach(tab => handleCloseTab(tab.id))
  }
  // Move a terminal tab OUT of this chat into the app-wide bottom panel. Unlike
  // handleCloseTab this must NOT dispose the session — the PTY + xterm live in
  // Diff view preferences — persisted; 'mc-diff-split' is shared with the
  // file view's git-diff toggle so split/unified is one app-wide preference.
  const [diffLineNumbers, setDiffLineNumbers] = usePersistedBool('mc-diff-linenums', false)
  const [diffSideBySide, setDiffSideBySide] = useDiffSplit()

  // The panel's size: the width and height remembered per chat, the dock
  // placement, the clamp to the window, the two resize gestures and the eased
  // edge on a chat switch (`useSidePanelSize.ts`).
  const {
    rootRef, isMobile, isBottom, setDock, effectiveWidth, effectiveHeight, dimTransition,
    resizing, slotSwitched, dimAnimating, panelResize, panelResizeV,
  } = useSidePanelSize({ slot, slotOwner, persistSlot, defaultWidth, extraReserveW, canDockBottom, expanded, fillWidth })

  return (
    <SidePanelDockHost value={canDockBottom}>
    <div
      ref={rootRef}
      data-testid="side-panel-root"
      className={`shrink-0 flex flex-col bg-bg overflow-hidden relative ${isBottom ? 'min-w-0 w-full border-t border-border' : 'min-h-0 mt-0 mb-2 border-l border-t border-b border-border rounded-l-xl'}`}
      style={isBottom
        ? { height: effectiveHeight, maxHeight: '85vh', width: '100%', ...dimTransition }
        : { width: effectiveWidth, maxWidth: '100vw', ...dimTransition }}
    >
      {isBottom ? (
        /* Top-edge resize handle — drag up/down to size the bottom dock. */
        <div role="separator" aria-orientation="horizontal" aria-label={i18nT('pages.chat.sidePanel.resize_panel')} className="absolute left-0 right-0 top-0 h-[6px] cursor-row-resize z-30 group/drag" style={{ touchAction: 'none' }} {...panelResizeV}>
          <div className="absolute left-0 right-0 top-0 h-[2px] transition-colors duration-200 bg-transparent group-hover/drag:bg-accent resize-accent" />
        </div>
      ) : fillWidth == null ? (
        /* Left-edge resize handle */
        <div role="separator" aria-orientation="vertical" aria-label={i18nT('pages.chat.sidePanel.resize_panel')} className="absolute left-0 top-0 bottom-0 w-[6px] cursor-col-resize z-30 group/drag" style={{ touchAction: 'none' }} {...panelResize}>
          <div className="absolute left-0 top-0 bottom-0 w-[2px] transition-colors duration-200 bg-transparent group-hover/drag:bg-accent resize-accent" />
        </div>
      ) : null}
      {/* Tab strip — the row scrolls by touch/wheel; a chip is reordered by
          dragging it (press and hold first on touch, see useLongPressReorder).
          Lifting that hold without moving opens the close menu.
          Browser-tab construction: the strip is an elevated band whose chips
          BOTTOM-ALIGN (items-end, pb-0) so the active chip's background runs
          straight into the panel body below — the strip/body seam is what the
          tab shape fuses across, in both dock placements (right dock and
          bottom dock render this same row above their content).
          side-panel-strip punches the strip out of the Electron window-drag
          region (see index.css) so chips receive events. */}
      {/* border-b draws the seam hairline the corner arcs land on: the flare
          curve ends tangent-horizontal, and without a line to continue into it
          would truncate mid-air. The chip rows drop 1px over the border row
          (-mb-px on the GROUPS, not the chips — the tablist scrolls and would
          clip an overflowing chip) so the active chip's opaque background
          covers the line across its own span, keeping the mouth open. */}
      {/* focus-caption-reserve (right dock only): in focus mode this strip is
          the surface at the window's top-trailing corner, where Windows and
          frameless Linux paint their caption controls — the panel chrome below
          would sit under them, covered and unclickable. Bottom-docked the strip
          is nowhere near that corner, so it takes no reserve. */}
      <div data-dynamic-active={dynamicActive ? 'true' : undefined} className={`side-panel-strip side-panel-strip-cq flex items-end gap-1.5 shrink-0 px-2 pt-2 pb-0 min-h-10 rounded-tl-xl bg-bg-elevated border-b border-border${isBottom ? '' : ' focus-caption-reserve'}${summaryPinned ? ' side-panel-strip-summary-pinned' : ''}`}>
        {/* Pinned views (Changes / Files / Artifacts): always present, fixed at
            the front, non-closable, not draggable, compact. The group's 8px gap
            matches the active chip's corner-piece width, so a piece lands in the
            gap instead of over a neighbour.

            SCROLLS, same class set as the dynamic tablist below (`min-w-0
            overflow-x-auto scrollbar-none`), and deliberately NOT `shrink-0`:
            a host may add leading chips ahead of the three pinned ones, and at
            a narrow width (320px, where the Crewmates overlay takes the whole
            window) an unshrinkable group pushes its own last chip AND the
            trailing strip controls — + menu, dock toggle, close — past the
            panel root's `overflow-hidden` edge, where nothing at that width
            brings them back. The chips inside stay `shrink-0`: they scroll,
            they never squeeze. */}
        {/* Wrapper for the edge cues: the fades anchor to this non-scrolling
            parent, not the scrolled group. min-w-0 keeps the scroller
            shrinkable (the group must stay shrinkable per the comment above). */}
        <div className="relative min-w-0 flex items-end">
        <div
          ref={tabNav.fixedScrollerRef}
          className="flex items-end gap-2 min-w-0 overflow-x-auto scrollbar-none -mb-px"
          data-testid="side-panel-fixed-tabs"
        >
          {/* The host's leading tabs, ahead of the pinned views: non-closable
              chips, never Reorder items — they are the strip's identity, not
              documents. Named by visible text and `host`'s aria-label; a
              badge rides after the label. No `role="tablist"` here —
              the strip already carries one on the dynamic group, and the
              pinned chips beside these have never had their own. */}
          {!!leadingTabs?.length && (
            <div className="flex items-end gap-2 shrink-0" data-testid="side-panel-leading-tabs">
              {leadingTabs.map(lt => (
                <TabChip
                  key={lt.id}
                  chipId={lt.id}
                  tab={{ title: lt.title }}
                  icon={lt.icon}
                  badge={lt.badge}
                  active={lt.id === activeId}
                  closable={false}
                  pinned={false}
                  host
                  onSelect={() => { void requestActive(lt.id, activeId) }}
                  onClose={() => {}}
                  testId={`side-panel-leading-tab-${lt.id}`}
                />
              ))}
            </div>
          )}
          {pinnedTabs.map(t => (
            <TabChip key={t.id} chipId={t.id} tab={t} active={t.id === activeId} closable={false} pinned onSelect={() => { void requestActive(t.id, activeId) }} onClose={() => {}} />
          ))}
        </div>
        {/* from-bg-elevated matches the side-panel-strip surface. */}
        {fixedEdges.left && (
          <div aria-hidden="true" data-testid="side-panel-fixed-tabs-cue-left" className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-r from-bg-elevated to-transparent" />
        )}
        {fixedEdges.right && (
          <div aria-hidden="true" data-testid="side-panel-fixed-tabs-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-l from-bg-elevated to-transparent" />
        )}
        </div>
        {/* Chrome's separator rule, extended to the pinned↔dynamic divider: a
            hairline adjacent to the ACTIVE chip goes transparent. The active
            chip's 8px corner piece travels across this 6px gap, and a divider
            slicing through it reads as a detached blob on any theme where --bg
            differs from --bg-elevated (glaring on light). Transparent rather
            than unmounted, so activating an adjacent tab cannot shift the row
            by the divider's layout width. The dynamic group's own separators
            already follow the same suppression rule. */}
        {pinnedTabs.length > 0 && dynamicTabs.length > 0 && (
          <span
            aria-hidden="true"
            data-testid="strip-divider"
            className={`w-px h-5 shrink-0 self-center relative z-10 ${
              pinnedTabs[pinnedTabs.length - 1].id === activeId || dynamicTabs[0].id === activeId
                ? 'bg-transparent'
                : 'bg-border'
            }`}
          />
        )}
        {/* Wrapper for the edge cues: the fades anchor to this non-scrolling
            parent, not the scrolled group. min-w-0 keeps the scroller
            shrinkable. The -mb-px (border overlap) stays on the group itself,
            which the seam-hairline contract pins. */}
        <div ref={dynamicWrapRef} className="relative min-w-0 flex items-end">
        <Reorder.Group
          ref={tabNav.dynamicScrollerRef}
          axis="x"
          values={dynamicTabs}
          onReorder={(next) => setOrder([...pinnedTabs, ...next])}
          role="tablist"
          className="flex items-end gap-2 min-w-0 overflow-x-auto scrollbar-none list-none m-0 p-0 px-2 -mb-px"
        >
          {dynamicTabs.map((t, i) => (
            <DraggableTabItem
              key={t.id}
              tab={t}
              active={t.id === activeId}
              // Chrome-style separator: hairline between adjacent chips,
              // suppressed on both edges of the selected tab (its pill
              // background already delineates it).
              separator={i > 0 && t.id !== activeId && dynamicTabs[i - 1].id !== activeId}
              instantLayout={resizing || slotSwitched || dimAnimating}
              onSelect={() => { void requestActive(t.id, activeId) }}
              onClose={() => handleCloseTabs([t])}
              onCloseDirect={() => handleCloseTab(t.id)}
              closeOthersDisabled={dynamicTabs.length === 1}
              closeRightDisabled={i === dynamicTabs.length - 1}
              onCloseOthers={() => handleCloseTabs(dynamicTabs.filter(tab => tab.id !== t.id))}
              onCloseRight={() => handleCloseTabs(dynamicTabs.slice(i + 1))}
              onCloseAll={() => handleCloseTabs(dynamicTabs)}
            />
          ))}
        </Reorder.Group>
        {/* from-bg-elevated matches the side-panel-strip surface. */}
        {tabEdges.left && (
          <div aria-hidden="true" data-testid="side-panel-tabs-cue-left" className="pointer-events-none absolute left-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-r from-bg-elevated to-transparent" />
        )}
        {tabEdges.right && (
          <div aria-hidden="true" data-testid="side-panel-tabs-cue-right" className="pointer-events-none absolute right-0 top-0 bottom-0 w-6 z-10 bg-gradient-to-l from-bg-elevated to-transparent" />
        )}
        </div>
        {/* + menu — the shared shadcn/Radix dropdown, so this strip gets the
            same pill hover, portalled positioning, focus trap/restore, roving
            arrow-key focus and Escape handling as every other menu in the app
            (previously hand-rolled with an outside-click listener and
            useListboxKeyboard). */}
        <DropdownMenu guideScope="menu:chat.side-panel.add">
          <DropdownMenuTrigger asChild>
            <button
              className="flex items-center justify-center w-7 h-7 shrink-0 self-center rounded-md text-muted hover:text-text hover:bg-bg-hover data-[state=open]:bg-bg-hover data-[state=open]:text-text transition-colors bg-transparent border-none cursor-pointer"
              title={i18nT('pages.chat.sidePanel.open_side_panel_tab')}
              aria-label={i18nT('pages.chat.sidePanel.open_side_panel_tab')}
              {...uiLocation('chat.side-panel.add')}
            >
              <Plus size={15} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" sideOffset={6} className="min-w-[200px]">
            {menuSections.map((section, i) => (
              // Keyed by the group's stable declared id — see NEW_MENU_GROUPS.
              // Keying on contents (the first surviving row) or on the index
              // makes the key move when a gate resolves, and React then remounts
              // the group, detaching the row mid-click.
              <Fragment key={section.id}>
                {i > 0 && <DropdownMenuSeparator />}
                {/* Browser is its own render site so find_ui can name it (the
                    `chat.side-panel.browser` location): a marker spread on the
                    shared row would land on every row, under a label read from
                    data. Same element, classes and label key as the others. */}
                {section.items.map(item => item.kind === 'browser' ? (
                  <AddMenuItem
                    key={item.kind}
                    onSelect={() => openMenuItem(item.kind)}
                    {...uiLocation('chat.side-panel.browser')}
                  >
                    <span className="text-muted shrink-0"><Globe size={15} /></span>
                    <span className="flex-1">{i18nT('pages.chat.sidePanel.menu_browser')}</span>
                  </AddMenuItem>
                ) : (
                  <AddMenuItem
                    key={item.kind}
                    onSelect={() => openMenuItem(item.kind)}
                  >
                    <span className="text-muted shrink-0">{item.icon}</span>
                    <span className="flex-1">{i18nT(NEW_MENU_LABEL_KEY[item.kind])}</span>
                  </AddMenuItem>
                ))}
              </Fragment>
            ))}
            {/* App-contributed tabs (contributes.panelTabs). Their labels are the
                app's own literals — the core has no i18n key for a tab it does not
                know — so they render descriptor.menuLabel directly rather than
                through NEW_MENU_LABEL_KEY. No contributing app ⇒ nothing. */}
            {panelTabDescriptors.length > 0 && (
              <Fragment key="app-panel-tabs">
                <DropdownMenuSeparator />
                {panelTabDescriptors.map(d => (
                  <DropdownMenuItem
                    key={d.kind}
                    className="gap-2.5 py-2"
                    onSelect={() => requestPanelTab(d)}
                  >
                    <span className="text-muted shrink-0">{appIcon(d.icon)}</span>
                    <span className="flex-1">{d.menuLabel}</span>
                  </DropdownMenuItem>
                ))}
              </Fragment>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
        {/* Flexible gap: the tabs and + hug the leading edge; this absorbs the
            slack so the panel chrome sits at the trailing edge. */}
        <div aria-hidden="true" className="flex-1 min-w-0" />
        {/* Every open tab, for the ones the scrollers clip. Fixed at the trailing
            edge, outside both scrollers, so it never scrolls away or reads as part of +. */}
        <AllTabsMenu groups={tabNav.groups} currentId={activeId} onSelect={tabNav.onSelect} label={i18nT('pages.chat.sidePanel.all_tabs')} />
        {/* Panel chrome, trailing edge. Collapse (frequent) stays a one-tap
            button; the rarely-used dock toggle moves into a ⋯ menu so the two
            panel-square glyphs are never adjacent look-alikes. A permanent
            panel (no onClose) that cannot dock has no chrome here at all — the
            divider goes with it rather than ruling off an empty group. */}
        {(closable || (canDockBottom && !isMobile)) && (
          <span aria-hidden="true" className="w-px h-5 bg-border shrink-0 self-center relative z-10" />
        )}
        <div className="flex items-center gap-0.5 shrink-0 self-center">
        {canDockBottom && !isMobile && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className="flex items-center justify-center w-7 h-7 shrink-0 rounded-md text-muted hover:text-text hover:bg-bg-hover data-[state=open]:bg-bg-hover data-[state=open]:text-text transition-colors bg-transparent border-none cursor-pointer"
                title={i18nT('pages.chatSidebar.more_options')}
                aria-label={i18nT('pages.chatSidebar.more_options')}
              >
                <MoreHorizontal size={15} />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" sideOffset={6} className="min-w-[200px]">
              <DropdownMenuItem
                className="gap-2.5 py-2"
                onSelect={() => setDock(isBottom ? 'right' : 'bottom')}
              >
                <span className="text-muted shrink-0">{isBottom ? <PanelRight size={16} /> : <PanelBottom size={16} />}</span>
                <span className="flex-1">{isBottom ? i18nT('pages.chat.sidePanel.dock_right') : i18nT('pages.chat.sidePanel.dock_bottom')}</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
        {closable && (
        <button
          className="pi-morph flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0"
          onClick={() => { void closePanel() }}
          title={i18nT('pages.chat.sidePanel.close_panel')}
          aria-label={i18nT('pages.chat.sidePanel.close_panel')}
        >
          <SidePanelGlyph light size={15} />
        </button>
        )}
        </div>
      </div>

      {/* Body — render every doc/terminal tab mounted (hidden when inactive) so
          xterm sessions and editor scroll state survive tab switches; category
          views mount only when active (cheap + query-driven). */}
      {/* The Files tab's "Open in <app>" fell back to the integrated terminal,
          which this panel has just brought to the front; the note lives here so
          it outlives the Files tab going out of view. In flow above the bodies,
          not layered over them, so it never covers a shell's first lines. The
          launch failed, so it is an ErrorNotice (errors-use-error-notice) even
          though a terminal did open. The notes that send the user to the picker
          link straight to it, inside the notice as its footer so the link reads
          as part of it. No hand-off: the adjacent chat composer can hold
          unsent text. */}
      {workspaceTerminalCapped && (
        <div className="shrink-0 px-3 pt-3">
          <ErrorNotice
            className="w-full"
            message={i18nT('sessionEnvironments.workspaceTerminalCapped', { max: MAX_TERMINALS_PER_CHAT })}
            onDismiss={() => setWorkspaceTerminalCapped(false)}
            testId="side-panel-workspace-terminal-capped"
          />
        </div>
      )}
      {externalTerminalNote && (
        <div className="shrink-0 px-3 pt-3">
          <ErrorNotice
            className="w-full"
            message={externalTerminalNote.message}
            onDismiss={() => setExternalTerminalNote(null)}
            testId="side-panel-external-terminal-fallback"
            footer={externalTerminalNote.offerSettings ? (
              <SettingsLink
                tab="display"
                sub="terminal"
                highlight="display.external-terminal-app"
                className="text-[12px] text-accent hover:underline"
                data-testid="side-panel-external-terminal-settings"
              >
                {i18nT('pages.chat.sidePanel.external_terminal_open_settings')}
              </SettingsLink>
            ) : undefined}
          />
        </div>
      )}
      {/* Content area: left + top border (square corner) so the border wraps
          only the content, NOT the tab strip above (which stays borderless). */}
      {confirmDialog}
      <div className="flex-1 min-h-0 relative">
        {/* Query-only leading views mount while active. A host can retain a
            visited dashboard so tab switches preserve its drafts and iframe. */}
        {leadingTabs?.filter(lt => lt.id === activeId || lt.keepMounted).map(lt => (
          <div key={lt.id} className="absolute inset-0 overflow-y-auto" hidden={lt.id !== activeId} data-testid="side-panel-leading-body" data-leading-id={lt.id}>
            {lt.render()}
          </div>
        ))}
        {visibleTabs.length === 0 && !leadingTabs?.length && (
          /* Empty state: launcher — the available views themselves, roomy and
             clickable, instead of a hint pointing at the + menu. */
          <div className="flex items-center justify-center h-full px-6">
            <div className="flex flex-col items-center gap-4 w-full max-w-[420px]">
              <div className="text-[22px] text-muted font-semibold">{i18nT('pages.chat.sidePanel.pick_a_panel_to_view')}</div>
              <div className="grid grid-cols-2 gap-2.5 w-full">
              {menuItems.map(item => {
                // Live badges from data already flowing into the panel — a
                // quiet accent pill when non-zero, muted otherwise. Files
                // carries none: it browses the project tree rather than
                // listing what this session touched, so there is no count.
                const badge = item.kind === 'subagents' && Object.values(subagents).some(s => s.status === 'running' || s.status === 'tool')
                  ? `${Object.values(subagents).filter(s => s.status === 'running' || s.status === 'tool').length} running`
                  : item.kind === 'logs' && toolLog.length > 0
                    ? `${toolLog.length} calls`
                    : null
                const descKey = NEW_MENU_DESC_KEY[item.kind]
                return (
                  <button
                    key={item.kind}
                    className="flex flex-col items-start gap-1.5 px-3.5 py-3 rounded-xl border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong text-left cursor-pointer transition-colors"
                    onClick={() => openMenuItem(item.kind)}
                  >
                    <div className="flex items-center gap-2.5 w-full text-text">
                      <span className="shrink-0 opacity-80">{item.icon}</span>
                      <span className="text-[13px] font-medium">{i18nT(NEW_MENU_LABEL_KEY[item.kind])}</span>
                      {badge && (
                        <span className="ml-auto text-[10px] px-2 py-0.5 rounded-full bg-accent-subtle text-accent font-medium shrink-0">{badge}</span>
                      )}
                    </div>
                    {descKey && (
                      <div className="text-[11px] text-muted leading-snug">{i18nT(descKey)}</div>
                    )}
                  </button>
                )
              })}
              {/* App-contributed tabs share the launcher grid, so it presents the
                  full set rather than the "+" menu carrying tabs the launcher
                  hides. This is the one surface that renders menuDescription. */}
              {panelTabDescriptors.map(d => (
                <button
                  key={d.kind}
                  className="flex flex-col items-start gap-1.5 px-3.5 py-3 rounded-xl border border-border bg-transparent hover:bg-bg-hover hover:border-border-strong text-left cursor-pointer transition-colors"
                  onClick={() => requestPanelTab(d)}
                >
                  <div className="flex items-center gap-2.5 w-full text-text">
                    <span className="shrink-0 opacity-80">{appIcon(d.icon)}</span>
                    <span className="text-[13px] font-medium">{d.menuLabel}</span>
                  </div>
                  {d.menuDescription && (
                    <div className="text-[11px] text-muted leading-snug">{d.menuDescription}</div>
                  )}
                </button>
              ))}
              </div>
            </div>
          </div>
        )}
        {/* ALL stored tabs, not only the visible ones: a withheld tab can never be
            the active one (see `activeId`), so a withheld category view simply
            renders nothing below, while a withheld body-owning tab — a Browser's
            WebContentsView, a terminal's xterm, an editor buffer — stays MOUNTED
            and hidden. A withdrawal can be temporary (the Members page withholds
            every slot view for the moment its thread POST is in flight), and
            unmounting a live browser for that moment would destroy its page. */}
        {tabs.map(t => {
          const isActive = t.id === activeId
          // Category views mount only while active (VIEW_KINDS below): they hold
          // no editable buffer, so unmounting an inactive one loses nothing, and
          // it keeps exactly one panel-level Escape handler live at a time. Files
          // and Command Center are kept mounted instead, each for its own reason.
          // App tabs render from `allAppTabs` below (one stable key for every slot);
          // rendering them here too would mount the same body twice. Both
          // body-owning kinds skip: an MCP frame's iframe and an app-contributed
          // tab's `AppHost` are equally destroyed by a key change on chat switch.
          if (t.kind === 'app' || isPanelTabKind(t.kind)) return null
          // Keep the authored document and per-question drafts alive on tab switches.
          if (t.kind === 'command-center') return (
            <div key={`${t.id}:${slot}`} className="absolute inset-0" hidden={!isActive}>
              {/* Local boundary: the panel is a lazy chunk, and a chunk that fails
                  to load after main.tsx's preload-reload heal declined would
                  otherwise reject up to the ROUTE boundary and replace the whole
                  chat page with an error card. The fallback is the shared error
                  surface, not nothing, so the tab says why it is empty. */}
              {/* No hand-off: the adjacent chat composer holds unsent text and the
                  panel's own answer drafts live in this tab. */}
              <ErrorBoundary scope="command-center" fallback={<ErrorNotice className="m-3" message={i18nT('commandCenter.panel_load_failed')} />}>
                <Suspense fallback={null}><CommandCenterPanel slot={slot ?? null} active={isActive && !panelHidden} onAct={onComposerInsert} /></Suspense>
              </ErrorBoundary>
            </div>
          )
          if (remoteEnvironment === undefined && (
            t.kind === 'files' || t.kind === 'git' || t.kind === 'terminal'
            || t.kind === 'file' || t.kind === 'diff' || t.kind === 'wtdiff' || t.kind === 'folder'
          )) {
            if (!isActive) return null
            return (
              <div key={t.id} className="absolute inset-0 p-4">
                {/* No hand-off: the chat composer and open files may hold unsaved drafts. */}
                {environmentError && slot
                  ? <EnvironmentPlacementError slot={slot} />
                  : <ContentSkeleton rows={8} />}
              </div>
            )
          }
          // A remote chat's files live in its workspace. These tabs read and write
          // the GATEWAY's copy, so a remote chat gets a pointer to its Files tab
          // instead; the tab and any draft it holds stay, unrendered.
          if (remoteEnvironment && (
            t.kind === 'git' || t.kind === 'file' || t.kind === 'diff' || t.kind === 'wtdiff' || t.kind === 'folder'
          )) {
            if (!isActive) return null
            return (
              <div key={t.id} className="absolute inset-0 p-4">
                <p role="status" data-testid="remote-gateway-tab-withheld" className="text-[13px] text-muted">
                  {i18nT('sessionEnvironments.gatewayTabWithheld', { name: remoteEnvironment.name })}
                </p>
              </div>
            )
          }
          // A terminal opened on THIS machine before the chat was bound keeps its
          // gateway shell; once the chat is confirmed remote it is no longer
          // typed into here (a command meant for the workspace would run on the
          // gateway). The tab stays so the user can close it.
          if (remoteEnvironment && t.kind === 'terminal' && !t.remoteSlot) {
            if (!isActive) return null
            return (
              <div key={t.id} className="absolute inset-0 p-4">
                <p role="status" data-testid="remote-gateway-terminal-withheld" className="text-[13px] text-muted">
                  {i18nT('sessionEnvironments.gatewayTerminalWithheld', { name: remoteEnvironment.name })}
                </p>
              </div>
            )
          }
          // A Crew Mode root's workers, for the root and for each worker on screen.
          if (t.kind === 'workers') {
            if (!isActive) return null
            return (
              <div key={`${t.id}:${slot}`} className="absolute inset-0">
                <ErrorBoundary scope="crew-workers" fallback={<ErrorNotice className="m-3" message={i18nT('commandCenter.workers_error')} askAgent />}>
                  <CrewWorkersTab slot={slot ?? null} active={!panelHidden} />
                </ErrorBoundary>
              </div>
            )
          }
          // The pinned Files tab renders the file-browser home directly — it
          // is not one of ActivityViewer's multiplexed session views.
          if (t.kind === 'files') {
            // A remote session's files live in its guest workspace, not in the
            // gateway project the local browser reads.
            if (remoteEnvironment) {
              if (!isActive) return null
              return (
                <div key={t.id} className="absolute inset-0">
                  <Suspense fallback={null}>
                    <RemoteFilesPanel
                      slot={slot}
                      name={remoteEnvironment.name}
                      workdir={remoteEnvironment.workdir}
                      onOpenTerminal={terminalEnabled && !isWithheld('terminal') ? openProjectTerminal : undefined}
                    />
                  </Suspense>
                </div>
              )
            }
            // Kept mounted while another tab is selected, as a hidden panel keeps
            // it: unmounting drops the tree's scroll position, which the user then
            // has to find again. `active` pauses its polling while it is not shown.
            return (
              <div key={t.id} className="absolute inset-0" hidden={!isActive}>
                <FilesHomePanel
                  active={isActive && !panelHidden}
                  projectDir={projectDir ?? ''}
                  // A diff open from the Changed segment opens the working-tree
                  // diff as its OWN tab (coexisting with any plain file tab for
                  // the same path, #9695) when the host supplies the opener;
                  // otherwise it falls back to flipping the file tab into diff
                  // mode, the pre-#9695 behaviour.
                  onFileOpen={(abs, diff, opts) => {
                    if (diff && onOpenWorkingTreeDiff) { onOpenWorkingTreeDiff(abs); return }
                    onFileOpen?.(abs, { diffMode: diff, line: opts?.line })
                  }}
                  onAddToContext={onAddToContext}
                  // Withheld, not disabled, when the terminal feature is off or
                  // the host withdraws the terminal view — the same withdrawal
                  // that removes Terminal from the + menu must remove its
                  // per-project shortcut, or the button promises a shell this
                  // panel will not open.
                  onOpenTerminal={terminalEnabled && !isWithheld('terminal') ? openProjectTerminal : undefined}
                  onOpenFolder={p => openFolder(p, t.slot ?? null)}
                  onExternalTerminalFailed={setExternalTerminalNote}
                />
              </div>
            )
          }
          if (VIEW_KINDS.has(t.kind)) {
            // A panel kept mounted but hidden (a Dynamic Dashboard or app tab holds it)
            // must not keep these bodies polling: they unmount as a closed panel's did.
            if (!isActive || panelHidden) return null
            return (
              <div key={t.id} className="absolute inset-0">
                <ActivityViewer
                  view={t.kind as 'changes' | 'issues' | 'links' | 'artifacts' | 'subagents' | 'workflows' | 'logs' | 'crewlog' | 'context' | 'side' | 'git' | 'summary' | 'pins'}
                  open onToggle={() => { void closePanel() }} slot={slot}
                  subagents={subagents} toolLog={toolLog}
                  sources={sources}
                  selectedSourceUrl={selectedSourceUrl}
                  onSelectSource={onSelectSource}
                  onReconcileSource={onReconcileSource}
                  issues={issues}
                  selectedIssueUrl={selectedIssueUrl}
                  onSelectIssue={onSelectIssue}
                  onReconcileIssue={onReconcileIssue}
                  onAddToChat={onAddSourceToChat}
                  // Of the pinned tabs, only Artifacts and Changes reach this
                  // component — Files short-circuits to FilesHomePanel above.
                  // Changes renders the session's pull-request sources; Artifacts
                  // opens document rows as file tabs via onFileOpen and artifact
                  // rows as artifact tabs via onArtifactOpen.
                  onFileOpen={onFileOpen}
                  onArtifactOpen={onArtifactOpen}
                  pins={pins} pinsLoading={pinsLoading} onJumpToPin={onJumpToPin} onUnpin={onUnpin}
                  slotTitle={slotTitle} chatMode={chatMode}
                  navLinks={navLinks} navResolving={navResolving}
                />
              </div>
            )
          }
          // Terminal + documents: keep mounted, toggle visibility.
          return (
            <div key={t.id} className="absolute inset-0" style={{ display: isActive ? 'block' : 'none' }}>
              <TabBody
                // Visible to the user means BOTH: this tab is the selected one
                // AND the panel itself is on screen. A hidden panel still has a
                // selected tab, so selection alone would let a closed panel's
                // editor answer Escape and Cmd+S.
                tab={t} active={isActive && !panelHidden}
                slot={slot}
                onClose={() => handleCloseTab(t.id)}
                onContentChange={(c) => patchTab(t.id, { content: c })}
                onDiskContent={(c, binary, partial) => patchTab(t.id, { content: c, savedContent: c, ...(binary === undefined ? {} : { binary }), ...(partial === undefined ? {} : { partial }) })}
                onDiffModeChange={(diffMode) => patchTab(t.id, { diffMode })}
                onRevealConsumed={() => patchTab(t.id, { revealLine: undefined })}
                onPathChange={(p) => patchTab(t.id, { path: p, title: p.replace(/\/+$/, '').split('/').pop() || p })}
                onFileSave={onFileSave}
                onFileOpen={onFileOpen}
                // A folder opened from a file tab keeps that tab's origin chat.
                onFolderOpen={(p) => openFolder(p, t.slot ?? null)}
                onAddToContext={onAddToContext}
                projectDir={projectDir}
                onSubmitComments={onSubmitComments}
                connected={connected}
                onTerminalSendToChat={onAddSourceToChat}
                diffLineNumbers={diffLineNumbers}
                setDiffLineNumbers={setDiffLineNumbers}
                diffSideBySide={diffSideBySide}
                setDiffSideBySide={setDiffSideBySide}
              />
            </div>
          )
        })}
        {/* Every body-owning tab — MCP App frames and app-contributed tabs — from
            every chat slot, in ONE list keyed by slot + the tab's own id. Only the
            tab that is active in the CURRENT slot is shown; the rest stay mounted and
            hidden. Keying and mounting here (rather than splitting active vs
            background) is what lets a body survive a chat switch: its key never
            changes, so React never remounts the iframe or the app's `AppHost`. */}
        {/* Panel-level and above the bodies, so a failed app list is reported even though
            the pruning it causes has already moved focus off every contributed tab. */}
        <AppPanelTabsErrorNotice />
        {allAppTabs.map(t => {
          // Key and visibility BOTH carry the slot. A tool-call id is only unique
          // within a session -- `chat.mcpApps` keys by session + tool-call id for
          // exactly that reason -- so keying on the tab id alone let two slots
          // collide: duplicate React keys, and `shown` true for both, so another
          // session's frame overlaid the current one and took its interactions.
          // The tab's OWN slot never changes, so this key is still stable across a
          // chat switch (which is what stops the iframe remounting).
          const tabSlot = t.slot ?? slot
          // A withheld app tab (`hiddenViews` has 'app') is never the shown one.
          const shown = t.id === activeId && tabSlot === slot && !isWithheld(t.kind)
          return (
            <div key={`${tabSlot}\u001F${t.id}`} className="absolute inset-0" style={{ display: shown ? 'block' : 'none' }} aria-hidden={!shown}>
              {t.kind === 'app'
                ? <McpAppTabBody tab={t} slot={tabSlot} />
                /* `active` means visible to the USER, so it carries `panelHidden` for the
                   reason the `TabBody` above gives: a hidden panel still has a selected
                   tab, and `shown` alone would leave a collapsed panel's app polling and
                   holding global handlers. `display` stays on `shown` alone -- the body
                   must keep its box when the panel is merely collapsed, or it would
                   remount. */
                : <AppPanelTabBody kind={t.kind} active={shown && !panelHidden} slot={tabSlot} />}
            </div>
          )
        })}
      </div>
    </div>
    </SidePanelDockHost>
  )
}

/** The placement read failed (its only code is `choices_unreadable`), so the
 *  local tabs stay closed. A fixed message, not the raw error; Retry re-reads. */
function EnvironmentPlacementError({ slot }: { slot: string }) {
  const qc = useQueryClient()
  const queryKey = slotEnvironmentQueryKey(slot)
  const fetching = useIsFetching({ queryKey }) > 0
  return (
    <ErrorNotice message={i18nT('sessionEnvironments.placementError')} footer={
      <Btn disabled={fetching} onClick={() => void qc.refetchQueries({ queryKey })}>{i18nT('sessionEnvironments.retry')}</Btn>
    } />
  )
}
