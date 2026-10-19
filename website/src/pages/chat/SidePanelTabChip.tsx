/**
 * One chip on the chat side panel's tab strip.
 *
 * `TabChip` (glyph, label, badge, close control and middle-click close),
 * `DraggableTabItem` (a reorderable chip with its press-and-hold drag and its
 * close menu), `TerminalTabTitle` (a terminal chip's live title) and the
 * tab-kind glyphs (`KIND_ICON`, `iconForKind`). `SidePanel.tsx` lays out the
 * strip and renders these chips; this module never imports it.
 */
import TabCloseMenu, { openTabCloseMenu } from '../../components/TabCloseMenu'
import type { ReactNode } from 'react'
import { useLongPressReorder } from '../../hooks/useLongPressReorder'
import { Reorder } from 'framer-motion'
import { FileText, FileDiff, Bot, Workflow, ScrollText, MessageCircleQuestionMark, TerminalSquare, GitCompare, GitPullRequest, GitBranch, History, X, Component, Globe, CircleDot, Folder, Folders, Link as LinkIcon, PanelRight, Layers, ListTree, Pin } from 'lucide-react'
import { useTerminalTitle } from '../../utils/terminalRegistry'
import type { PanelTab, TabKind } from '../../hooks/usePanelTabs'
import { usePanelTabDescriptors, panelTabDescriptor, isPanelTabKind, type PanelTabDescriptor } from '../../hooks/panelTabRegistry'
import { appIcon } from '../../apps/appIcons'
import { i18nT } from '../../i18n/t'

// Every non-app tab kind maps to a glyph; app-contributed kinds (`app:<…>`) are
// excluded so this stays an EXHAUSTIVE map a forgotten built-in fails to satisfy
// — their icon comes from the manifest descriptor via `iconForKind` instead.
type BuiltinTabKind = Exclude<TabKind, `app:${string}`>
const KIND_ICON: Record<BuiltinTabKind, ReactNode> = {
  'command-center': <PanelRight size={16} />,
  changes: <GitPullRequest size={16} />, issues: <CircleDot size={16} />, files: <Folders size={16} />, links: <LinkIcon size={16} />, artifacts: <Component size={16} />, subagents: <Bot size={16} />, workflows: <Workflow size={16} />,
  logs: <ScrollText size={16} />, crewlog: <History size={16} />, context: <Layers size={16} />, side: <MessageCircleQuestionMark size={16} />, terminal: <TerminalSquare size={16} />, browser: <Globe size={16} />,
  summary: <ListTree size={16} />,
  pins: <Pin size={16} />,
  file: <FileText size={16} />, diff: <GitCompare size={16} />, wtdiff: <FileDiff size={16} />, artifact: <Component size={16} />, folder: <Folder size={16} />,
  app: <PanelRight size={16} />, git: <GitBranch size={16} />,
}

/** The strip/menu glyph for a tab kind. A built-in reads `KIND_ICON`; an
 *  app-contributed kind resolves its manifest lucide icon NAME through the
 *  app-facing icon set, falling back to a generic panel glyph. */
function iconForKind(kind: TabKind, descriptors: readonly PanelTabDescriptor[]): ReactNode {
  if (isPanelTabKind(kind)) {
    return appIcon(panelTabDescriptor(kind, descriptors)?.icon)
  }
  return KIND_ICON[kind]
}

/** Live terminal tab title — subscribes to the session's title (running command
 *  / cwd basename) pushed by the backend poller; falls back to the tab's default
 *  cwd title until the first frame arrives. Module-scope so it isn't redefined
 *  per render. */
function TerminalTabTitle({ sessionId, fallback }: { sessionId: string; fallback: string }) {
  const live = useTerminalTitle(sessionId)
  return <>{live || fallback}</>
}

/** One reorderable chip in the dynamic half of the strip.
 *
 *  A component rather than inline JSX inside the map: each chip owns its own
 *  long-press drag state, and a hook cannot be called from a loop. */
function DraggableTabItem({ tab, active, separator, instantLayout, onSelect, onClose, onCloseDirect, closeOthersDisabled, closeRightDisabled, onCloseOthers, onCloseRight, onCloseAll }: {
  tab: PanelTab
  active: boolean
  separator: boolean
  /** Skip the layout spring while the panel is being resized — see the caller. */
  instantLayout: boolean
  onSelect: () => void
  /** The close-menu's "Close" item: shares the batch path with the other menu
   *  items so a last-copy/unsaved file is still confirmed. */
  onClose: () => void
  /** The chip's × (and middle-click): closes this one tab directly, exactly as
   *  on main — the single-× control's behaviour is unchanged by this feature. */
  onCloseDirect: () => void
  closeOthersDisabled: boolean
  closeRightDisabled: boolean
  onCloseOthers: () => void
  onCloseRight: () => void
  onCloseAll: () => void
}) {
  // Same hold as the terminal strip: move to reorder, lift in place for the menu.
  const { itemProps, dragging } = useLongPressReorder({ onHoldRelease: openTabCloseMenu })
  return (
    <TabCloseMenu closeOthersDisabled={closeOthersDisabled} closeRightDisabled={closeRightDisabled}
      onClose={onClose} onCloseOthers={onCloseOthers} onCloseRight={onCloseRight} onCloseAll={onCloseAll}
      // Batches act on dynamic tabs only; the note says the fixed views stay.
      keepNote={i18nT('components.tabCloseMenu.fixed_views_stay_open')}>
    <Reorder.Item
      value={tab}
      {...itemProps}
      // The ring is the only feedback a press-and-hold gets before the finger
      // moves; without it an armed drag looks identical to a missed one.
      className={`relative shrink-0 list-none rounded-t-md rounded-b-none ${dragging ? 'ring-1 ring-accent' : ''}`}
      // Reorder.Item's layout prop can't be disabled (true | "position"
      // only) — instead make the layout correction instant while resizing so
      // chips track the panel edge 1:1. Otherwise use a tight spring (high
      // stiffness, near-critical damping) so the reorder shuffle snaps into
      // place instead of floating.
      transition={instantLayout ? { duration: 0 } : { type: 'spring', stiffness: 700, damping: 45 }}
    >
      {separator && (
        // Centered in the group's gap-2.
        <span aria-hidden="true" className="absolute -left-[4.5px] top-1/2 -translate-y-1/2 w-px h-4 bg-border" />
      )}
      <TabChip tab={tab} active={active} onSelect={onSelect} onClose={onCloseDirect} />
    </Reorder.Item>
    </TabCloseMenu>
  )
}

function TabChip({ tab, active, onSelect, onClose, closable = true, pinned = false, host = false, icon, badge, testId }: {
  /** A stored tab, or — for the host's leading tab — just a title: that chip has
   *  no `kind` (it is not a `PanelTab`) and brings its own `icon`. */
  tab: Pick<PanelTab, 'title'> & Partial<Pick<PanelTab, 'kind' | 'sessionId' | 'path'>>
  active: boolean; onSelect: () => void; onClose: () => void; closable?: boolean; pinned?: boolean
  /** A host-owned leading chip: always labelled like a document tab, but named
   *  (aria-label) like a pinned view, since it is the strip's own navigation. */
  host?: boolean
  /** Overrides the kind-derived glyph. Required when `tab.kind` is absent. */
  icon?: ReactNode
  /** Count / status pill rendered after the label. */
  badge?: ReactNode
  testId?: string
}) {
  // App-tab glyphs come from the manifest descriptor (resolved by name); a built-in
  // reads KIND_ICON. Reuses the shared ['apps'] query, so no extra fetch.
  const panelTabDescriptors = usePanelTabDescriptors()
  const glyph = icon ?? (tab.kind ? iconForKind(tab.kind, panelTabDescriptors) : null)
  // Every chip shows its label, pinned views (Changes / Artifacts / Files)
  // included: an icon-only pinned chip leaves a first-time user guessing which
  // glyph is Files. On a strip too narrow for three labels an inactive pinned
  // chip drops to its icon (`side-tab-pinned-idle`, index.css) so none of the
  // three scrolls out of view.
  const narrowIconOnly = pinned && !active
  return (
    <div
      role="tab"
      aria-selected={active}
      tabIndex={0}
      onClick={onSelect}
      // Guard on e.target so Enter/Space on the nested transfer/close buttons
      // activates them natively instead of also selecting the tab.
      onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onSelect() } }}
      // Chromium on Windows enters middle-click autoscroll on the middle-button
      // MOUSEDOWN when the target sits in a scroll container, and once it does,
      // `auxclick` never reaches the page, so the close below never runs. Each
      // tab group is `overflow-x-auto` (which computes overflow-y to auto too),
      // and the active chip's corner pieces (`.side-tab-active::before/::after`,
      // bottom:-1px in index.css) overflow its group by 1px, so the group
      // holding the ACTIVE chip is always scrollable: middle-clicking a file tab
      // while a file tab is active opened autoscroll instead of closing (#9406).
      // Cancelling mousedown is the only place that stops it, the same guard the
      // chat sidebar rows carry (SessionRow.tsx). Every chip, closable or not: a
      // pinned chip has no middle-button behaviour to keep. Middle button only:
      // the primary button's pointer sequence belongs to the reorder drag.
      onMouseDownCapture={(e) => { if (e.button === 1) e.preventDefault() }}
      onAuxClick={(e) => { if (closable && e.button === 1) { e.preventDefault(); onClose() } }}
      // Pinned and host chips carry an explicit accessible name, so a badge
      // after the label never becomes part of the tab's name.
      aria-label={pinned || host ? tab.title : undefined}
      // Labeled chips CSS-truncate at max-w-[240px], so the hover tooltip is
      // the only way to read a long title in full (e.g. an MCP app's
      // server/tool identity).
      // A file/diff/folder tab's label is only `basename(path)`, so a deep tree
      // and two same-named files in different directories are indistinguishable
      // from the label alone — prefer the full path whenever the tab carries
      // one, and fall back to the title for the tabs that have none (terminal,
      // app, pinned views).
      title={tab.path ?? tab.title}
      data-testid={testId}
      // Browser-tab chip: 32px tall, top corners only (8px), bottom edge fused
      // into the panel body. Active = the body's own background (--bg) plus a
      // top/side hairline (--border, bottom open) so the silhouette survives a
      // custom theme where --bg and --bg-elevated are equal; the ::before/::after
      // corner pieces carry a matching 1px arc in their gradient, so the hairline
      // FOLLOWS the outward curve instead of running straight through it (see
      // .side-tab-active in index.css). Inactive = muted text with a hover wash.
      // This deliberately supersedes the earlier Figma "Side Navigation" pill
      // spec (28px, 6px all-corner radius, --border active fill).
      className={`${narrowIconOnly ? 'side-tab-pinned-idle ' : ''}group relative isolate flex items-center gap-1 h-8 rounded-t-md rounded-b-none border cursor-pointer shrink-0 select-none transition-colors ${
        `max-w-[240px] ${closable ? 'pl-2 pr-1' : 'px-2'}`
      } ${
        active ? 'side-tab-active bg-bg text-accent border-x-border border-t-border border-b-transparent' : 'side-tab-inactive border-transparent text-muted hover:text-text'
      }`}
    >
      <span className="shrink-0">{glyph}</span>
      <span className="side-tab-label min-w-0 text-[12px] truncate text-left">
        {tab.kind === 'terminal' && tab.sessionId
          ? <TerminalTabTitle sessionId={tab.sessionId} fallback={tab.title} />
          : tab.title}
      </span>
      {badge != null && (
        <span className="side-tab-badge shrink-0" data-testid={testId ? `${testId}-badge` : undefined}>{badge}</span>
      )}
      {closable && (
        <div className="flex items-center gap-0.5 shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); onClose() }}
            className={`shrink-0 -ml-0.5 flex items-center justify-center w-[18px] h-[18px] rounded-full transition-all bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover ${active ? 'opacity-70' : 'opacity-0 group-hover:opacity-70 [@media(hover:none)]:opacity-70'}`}
            title={i18nT('pages.chat.sidePanel.close_tab')}
            aria-label={i18nT('pages.chat.sidePanel.close_tab')}
          >
            <X size={12} />
          </button>
        </div>
      )}
    </div>
  )
}

// The strip in `SidePanel.tsx` renders these.
export { DraggableTabItem, TabChip }
