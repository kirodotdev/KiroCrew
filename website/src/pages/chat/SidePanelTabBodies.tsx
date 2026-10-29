/**
 * The bodies of the chat side panel's kept-mounted tabs.
 *
 * `TabBody` renders a terminal, Browser, MCP app, file or working-tree diff,
 * folder, artifact or turn-diff tab: through `FileTabBody` (the editor with the
 * file-browser rail), `HydratingFileTab` (a restored file tab before its first
 * read) or the shared viewers. `McpAppTabBody` and `AppPanelTabBody` render the
 * cross-slot app frames, and `AppPanelTabsErrorNotice` reports a failed app list
 * at panel level. `SidePanel.tsx` decides which tabs mount and which one shows,
 * and renders the category views itself; this module never imports it.
 */
import { useState, useRef, useEffect, Suspense, lazy } from 'react'
import { Hash, Pen, Columns2 } from 'lucide-react'
import DiffPanel from '../../components/DiffPanel'
import DetailPanel from '../../components/DetailPanel'
import MarkdownPanel, { type MarkdownPanelHandle } from '../../components/MarkdownPanel'
import ArtifactPanel from '../../components/ArtifactPanel'
import FolderPanel from './FolderPanel'
import FileBrowserRail from './FileBrowserRail'
import WebPreviewPanel from '../../components/WebPreviewPanel'
import CliPanel from '../../components/CliPanel'
import { countLines } from '../../components/FileChangeChips'
import { useQueryClient } from '@tanstack/react-query'
import type { PanelTab } from '../../hooks/usePanelTabs'
import { usePanelTerminalsPending } from '../../hooks/usePanelTabs'
import { usePanelTabDescriptors, useInstalledApps, panelTabDescriptor } from '../../hooks/panelTabRegistry'
import AppHost from '../../components/AppHost'
import { scrollMemoryKeyFor } from '../../hooks/useScrollMemory'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { ContentSkeleton } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import { fetchFileRead, fileReadQueryKey, FILE_READ_STALE_MS, isPartialRead } from '../../utils/fileReadQuery'
import { errMessage } from '../../utils/thunkError'
import { useAppSelector } from '../../store'
import { mcpAppKey } from '../../store/chatSlice'
import McpAppFrame from '../../components/McpAppFrame'
import { dashboardPreviewTargetFromRef } from '../../utils/dashboardPreview'
import { i18nT } from '../../i18n/t'

const DashboardPreviewPanel = lazy(() => import('./DashboardPreviewPanel'))

/** Body renderer for terminal + document tabs. Module-scope (NOT nested inside
 *  SidePanel): a nested component definition would produce a new component
 *  type on every SidePanel render, forcing React to unmount/remount the whole
 *  subtree — which reset editor state and re-fired xterm's focus-on-visible
 *  effect, stealing focus from the chat input on every keystroke. */
/** Body for an app-contributed side-panel tab (`contributes.panelTabs`). Resolves
 *  the tab's descriptor and its installed-app record from the shared `['apps']`
 *  query and mounts the app's declared `entry` through the ESM `AppHost` — the
 *  same in-process host `ui.pages` use, so no app code crosses the boundary and
 *  no iframe is involved. Renders nothing while the app is absent (disabled /
 *  uninstalled); `active` is forwarded so a hidden body can pause work. */
function AppPanelTabBody({ kind, active, slot }: { kind: string; active: boolean; slot: string }) {
  const descriptors = usePanelTabDescriptors()
  // The shared, guarded `['apps']` observer rather than a second inline `useQuery`:
  // see `useInstalledApps` for why a hand-rolled copy breaks an unrelated consumer.
  const { apps } = useInstalledApps()
  const d = panelTabDescriptor(kind, descriptors)
  const app = d ? apps.find(a => a.name === d.appName) : undefined
  if (!d || !app) return null
  // The OWNING slot, not the active one: with cross-slot hosting this body may belong
  // to another chat, and the identity its requests carry has to be that chat's.
  return <AppHost app={app} entry={d.entry} active={active} sessionKey={`dashboard:${slot}`} />
}

/**
 * The app-list failure surface for contributed tabs.
 *
 * Deliberately NOT inside `AppPanelTabBody`: when the `['apps']` request fails there
 * are no descriptors, so every contributed tab is pruned from the visible strip and
 * `activeId` moves elsewhere — which puts that body's wrapper at `display:none` and
 * makes an error rendered inside it unreachable, exactly in the case it exists to
 * report (`errors-use-error-notice`). Rendering it at panel level is what survives the
 * pruning.
 *
 * Reported on EVERY failure, with no "does the reader have one stored" gate. Such a gate
 * looked like noise control and was really a second silence: on a FIRST load the bucket
 * holds no contributed tab yet, so the descriptors are simply empty, every contribution
 * is missing from the strip and the add menu, and nothing anywhere says why.
 *
 * Positioned as a top banner in an `absolute z-10` layer rather than in flow, because the
 * tab bodies beside it are `absolute inset-0` and would paint straight over a
 * normal-flow sibling. Anchored to the top edge instead of covering the panel so it does
 * not blanket the body a reader is working in.
 */
function AppPanelTabsErrorNotice() {
  const { isError, error } = useInstalledApps()
  if (!isError) return null
  return (
    <div className="absolute left-0 right-0 top-0 z-10 p-4">
      <ErrorNotice message={errMessage(error)} askAgent />
    </div>
  )
}

/** Host for one MCP App, keyed by session + tool-call id — the same
 *  `chat.mcpApps` store the inline path (`ToolCallLine`) reads, so the panel and
 *  the chat bubble are never two sources of truth.
 *
 *  A missing payload is a real, reachable state rather than a bug: render
 *  payloads carry multi-MB HTML and are capped per slot
 *  (`MCP_APPS_PER_SLOT_MAX`), so a long session's oldest app can be evicted
 *  while its tab is still open. Returning `null` there left an empty tab that
 *  read as a broken render, so say what happened instead. (A page reload cannot
 *  reach this: `serializeBucket` drops app tabs precisely because the payload
 *  never persists.) */
function McpAppTabBody({ tab, slot }: { tab: PanelTab; slot: string }) {
  const sk = tab.slot || slot
  const payload = useAppSelector(s =>
    tab.appToolCallId && sk ? s.chat.mcpApps?.[mcpAppKey(sk, tab.appToolCallId)] : undefined,
  )
  if (!payload) {
    return (
      <div className="flex items-center justify-center h-full px-6">
        <div className="text-[13px] text-muted text-center max-w-[280px]">
          {i18nT('pages.chat.sidePanel.this_app_render_is_no_longer_available')}
        </div>
      </div>
    )
  }
  return <div className="h-full w-full overflow-auto"><McpAppFrame payload={payload} /></div>
}

/**
 * The one true file surface: MarkdownPanel (full-width header, viewer/editor
 * body) with the file-browser rail docked on the right, under the header.
 * Every file-open path — chat file chips, a diff tab's title, the pinned
 * Files tab's tree, another file tab's tree — lands in a tab rendering this.
 *
 * Rail visibility is a single app-wide preference; with a project directory
 * and file-open host it stays mounted through tree-read failures so its notice,
 * toggle, and Refresh escape hatch remain reachable.
 */
function FileTabBody({ tab, active, projectDir, scrollMemoryKey, onContentChange, onDiskContent, onDiffModeChange, onFileSave, onFileOpen, onFolderOpen, onAddToContext, onClose, onSubmitComments, connected = true, onRevealConsumed }: {
  tab: PanelTab
  /** Is this the visible tab? Background file tabs stay mounted, so the panel
   *  needs this to keep its Cmd+F handler off a document the user cannot see. */
  active: boolean
  projectDir?: string
  /** Cross-remount scroll identity (slot + tab id) — see `useScrollMemory`. */
  scrollMemoryKey?: string
  onContentChange: (c: string) => void
  /** Disk-originated content (file watch / Refresh): the panel routes it here
   *  so the tab's saved baseline moves with the buffer it just replaced, and the
   *  binary verdict of that read moves with both. */
  onDiskContent: (c: string, binary?: boolean, partial?: boolean) => void
  onDiffModeChange: (diffMode: boolean) => void
  onFileSave: (fp: string, c: string) => Promise<void>
  onFileOpen?: (p: string, opts?: { diffMode?: boolean; line?: number; replaceId?: string; canReplace?: () => boolean }) => void
  /** The header path menu's "Show folder in side panel" target. */
  onFolderOpen?: (p: string) => void
  /** Right-click "Add to context" on a rail row. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  onClose: () => void
  onSubmitComments?: (m: string, count?: number) => void | boolean | Promise<void | boolean>
  connected?: boolean
  onRevealConsumed: () => void
}) {
  const [railOpen, setRailOpen] = usePersistedBool('mc-files-rail-open', false)
  const railUsable = !!projectDir && !!onFileOpen
  // The rail re-targets this tab in place, so the panel's own dirty guard has to
  // approve the navigation the way it approves a close.
  const panelRef = useRef<MarkdownPanelHandle>(null)
  return (
    <MarkdownPanel
      ref={panelRef}
      embedded
      active={active}
      filePath={tab.path || ''}
      content={tab.content || ''}
      binary={tab.binary}
      partial={tab.partial}
      scrollMemoryKey={scrollMemoryKey}
      onContentChange={onContentChange}
      onDiskContent={onDiskContent}
      savedBaseline={tab.savedContent}
      initialDiffMode={tab.diffMode}
      readOnly={tab.kind === 'wtdiff'}
      onDiffModeChange={onDiffModeChange}
      onSave={onFileSave}
      onClose={onClose}
      liveWatch
      onSubmitComments={tab.kind === 'wtdiff' ? undefined : onSubmitComments}
      connected={connected}
      revealLine={tab.revealLine}
      onRevealConsumed={onRevealConsumed}
      onFolderOpen={onFolderOpen}
      railOpen={railUsable && railOpen}
      onRailToggle={railUsable ? () => setRailOpen(v => !v) : undefined}
      browserRail={railUsable ? (
        <FileBrowserRail
          projectDir={projectDir}
          active={active}
          onAddToContext={onAddToContext}
          onOpenFolder={onFolderOpen}
          selectedPath={tab.path || null}
          // In-place navigation: a tree click RE-TARGETS this tab (replaceId)
          // rather than spawning a sibling — only the pinned Files tab fans
          // out into new tabs. Re-targeting discards the buffer, so it asks
          // through the panel's dirty guard first, exactly as closing does.
          // `canReplace` re-asks after the file read: the user can start typing
          // during a slow load, and by then the up-front answer is stale.
          onFileOpen={(abs, diff, opts) => {
            const nav = (stillClean?: () => boolean) =>
              onFileOpen(abs, {
                diffMode: diff, line: opts?.line, replaceId: tab.id, canReplace: stillClean,
              })
            const panel = panelRef.current
            if (panel) panel.requestNavigate(nav); else nav()
          }}
        />
      ) : undefined}
    />
  )
}

/**
 * The body of a RESTORED file tab, before anything has read the file.
 *
 * A persisted tab carries only metadata -- its buffer and its `binary` verdict
 * are both stripped on save -- so until a read lands, nothing about the file is
 * known. Mounting the editor on that empty buffer is not merely blank: a
 * restored `.zip` tab would offer a live editor over bytes that cannot be
 * decoded, and typing then saving would write text over the file. So this
 * placeholder renders instead, and it performs the read ITSELF rather than
 * leaning on one page's effect -- every host that mounts `SidePanel` (the chat
 * page and the members page) restores file tabs, and only a read that lives
 * here resolves on both. `onDiskContent` patches the buffer, the saved baseline
 * and the verdict together, which is what swaps this placeholder for the panel.
 *
 * A read that fails is shown AS a failure, not left on the skeleton: a skeleton
 * that never resolves reads as "still loading". Both strings it needs already
 * exist -- the notice's title is the panel's own `cannot_read_file`, and a 404
 * reuses the placeholder sentence `openFile` writes for a moved file.
 *
 * The read goes through `['file-read', path]`, the same React Query entry the
 * chip click and ChatPage's cold-tab hydration use, so a restored tab that BOTH
 * this placeholder and that page ask for costs one GET and yields one answer
 * rather than two racing reads of the same file.
 */
function HydratingFileTab({ path, onDiskContent }: { path: string; onDiskContent: (c: string, binary?: boolean, partial?: boolean) => void }) {
  const [error, setError] = useState<string | null>(null)
  const qc = useQueryClient()
  // Held in a ref so a new callback identity from the parent's render does not
  // re-trigger the read; only the path does.
  const applyRef = useRef(onDiskContent)
  useEffect(() => { applyRef.current = onDiskContent })
  useEffect(() => {
    const ac = new AbortController()
    setError(null)
    void (async () => {
      try {
        // `fetchQuery` on the shared key: a read already in flight for this path
        // (ChatPage's cold-tab query, a chip click) is JOINED rather than raced,
        // and a fresh entry is reused. The signal still belongs to this tab, so
        // unmounting stops this consumer without cancelling the shared read.
        const r = await qc.fetchQuery({
          queryKey: fileReadQueryKey(path),
          queryFn: ({ signal }) => fetchFileRead(path, signal),
          staleTime: FILE_READ_STALE_MS,
        })
        if (ac.signal.aborted) return
        if (r.ok) { applyRef.current(r.text, r.binary, isPartialRead(r)); return }
        if (r.status === 404) {
          applyRef.current(i18nT('pages.chatPage.file_not_found_on_disk_it_may_have_been_moved_or'), false)
          return
        }
        // The same sentence the chip click reports for a failed read: a human
        // line naming the file, with the status as the detail -- a bare "HTTP
        // 500" is a code machines produce, not something a reader can act on.
        setError(i18nT('pages.chatPage.could_not_read_file_reason', {
          path, reason: i18nT('pages.chatPage.http_status', { status: r.status }),
        }))
      } catch (e) {
        if (!ac.signal.aborted) {
          setError(i18nT('pages.chatPage.could_not_read_file_reason', {
            path, reason: errMessage(e) || i18nT('pages.chatPage.unknown_error'),
          }))
        }
      }
    })()
    return () => ac.abort()
  }, [path, qc])
  if (error !== null) {
    return (
      <div data-testid="file-tab-hydration-failed" className="h-full p-4">
        <ErrorNotice
          title={i18nT('components.markdownPanel.cannot_read_file')}
          message={error}
          askAgent
          testId="file-tab-hydration-error"
        />
      </div>
    )
  }
  return <div data-testid="file-tab-hydrating" className="h-full p-4"><ContentSkeleton rows={8} /></div>
}

function TabBody({ tab, active, slot, projectDir, onClose, onContentChange, onDiskContent, onDiffModeChange, onRevealConsumed, onPathChange, onFileSave, onFileOpen, onFolderOpen, onAddToContext, onSubmitComments, connected = true, onTerminalSendToChat, diffLineNumbers, setDiffLineNumbers, diffSideBySide, setDiffSideBySide }: {
  tab: PanelTab; active: boolean; slot: string
  /** The chat's project directory — the file-browser rail's tree root. */
  projectDir?: string
  onClose: () => void
  onContentChange: (c: string) => void
  /** Disk-originated content (file watch / Refresh): restamps the tab's saved
   *  baseline alongside the buffer, so a re-open still treats the tab clean. */
  onDiskContent: (c: string, binary?: boolean, partial?: boolean) => void
  onDiffModeChange: (diffMode: boolean) => void
  /** Drop the tab's one-shot line-reveal target once the panel has acted on it. */
  onRevealConsumed: () => void
  /** Folder tabs navigate internally; lift the new cwd back to the tab record
   *  so the strip label tracks where the user actually is. */
  onPathChange: (p: string) => void
  onFileSave: (fp: string, c: string) => Promise<void>
  onFileOpen?: (p: string, opts?: { diffMode?: boolean; line?: number; replaceId?: string; canReplace?: () => boolean }) => void
  /** Open a directory as a folder tab: the file tab header's "Show folder in
   *  side panel" row. */
  onFolderOpen?: (p: string) => void
  /** Right-click "Add to context" on a file-browser rail row. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  onSubmitComments?: (m: string, count?: number) => void | boolean | Promise<void | boolean>
  connected?: boolean
  onTerminalSendToChat?: (text: string) => void
  diffLineNumbers: boolean; setDiffLineNumbers: (fn: (v: boolean) => boolean) => void
  diffSideBySide: boolean; setDiffSideBySide: (fn: (v: boolean) => boolean) => void
}) {
  // An app-contributed tab (contributes.panelTabs) never reaches here: like the MCP
  // `app` kind, its body renders from the cross-slot `allAppTabs` list so a chat
  // switch cannot remount its `AppHost`. The tab loop in `SidePanel.tsx` returns null for both.
  const terminalsPending = usePanelTerminalsPending()
  // A restored shell may be gone: connecting before the ruling would spawn a new one.
  if (tab.kind === 'terminal') return terminalsPending ? null : <CliPanel sessionId={tab.sessionId ?? ''} cwd={tab.cwd} remoteSlot={tab.remoteSlot} chatSlot={slot || undefined} visible={active} onSendToChat={onTerminalSendToChat} />
  if (tab.kind === 'browser') return <WebPreviewPanel sessionKey={slot} active={active} />
  if (tab.kind === 'app') return <McpAppTabBody tab={tab} slot={slot} />
  // Cross-remount scroll identity for document bodies. Same slot+id key shape
  // as the app-frame list: the tab id is unique within a slot and stable in
  // the persisted bucket, so leaving and returning to this chat resolves the
  // same key.
  const scrollMemoryKey = scrollMemoryKeyFor(slot, tab.id)
  if (tab.kind === 'file' || tab.kind === 'wtdiff') {
    // `wtdiff` is a file-shaped tab opened in diff mode under a distinct id
    // (#9695): it renders through the same self-hydrating file body, which is
    // MarkdownPanel showing the current working-tree diff. Nothing is known
    // about a restored tab until a read lands, so it gets the self-hydrating
    // placeholder rather than an editor over an empty buffer.
    if (tab.content === undefined) {
      return <HydratingFileTab path={tab.path || ''} onDiskContent={onDiskContent} />
    }
    return (
      <FileTabBody
        tab={tab}
        active={active}
        projectDir={projectDir}
        scrollMemoryKey={scrollMemoryKey}
        onContentChange={onContentChange}
        onDiskContent={onDiskContent}
        onDiffModeChange={onDiffModeChange}
        onFileSave={onFileSave}
        onFileOpen={onFileOpen}
        onFolderOpen={onFolderOpen}
        onAddToContext={onAddToContext}
        onClose={onClose}
        onSubmitComments={onSubmitComments}
        connected={connected}
        onRevealConsumed={onRevealConsumed}
      />
    )
  }
  if (tab.kind === 'folder') {
    return (
      <FolderPanel
        path={tab.path || ''}
        projectDir={projectDir}
        onClose={onClose}
        onFileOpen={onFileOpen}
        onAddToContext={onAddToContext}
        onPathChange={onPathChange}
      />
    )
  }
  if (tab.kind === 'artifact') {
    // A crewmate's staged dashboard rides the artifact tab (see `dashboardPreviewRef`).
    const previewTarget = dashboardPreviewTargetFromRef(tab.artifactSlug)
    // A local boundary: a failed chunk load must not reach the route boundary and
    // unmount the host page (and any unsaved form on it).
    if (previewTarget) return <ErrorBoundary scope="dashboard-preview" retryOnly><Suspense fallback={null}><DashboardPreviewPanel target={previewTarget} /></Suspense></ErrorBoundary>
    return (
      <ArtifactPanel
        embedded
        active={active}
        slug={tab.artifactSlug || ''}
        kind={tab.artifactKind || 'markdown'}
        content={tab.content || ''}
        scrollMemoryKey={scrollMemoryKey}
        onClose={onClose}
        onSubmitComments={onSubmitComments}
        connected={connected}
      />
    )
  }
  if (tab.kind === 'diff') {
    const { added, removed } = countLines(tab.original || '', tab.modified || '')
    return (
      <DetailPanel
        embedded
        title={tab.title}
        onClose={onClose}
        noPadding
        customHeader={
          // Minimal single-bar toolbar: the tab chip owns identity + close, so the
          // bar carries breadcrumb (click → open editor), change stats, and the
          // two view controls. Divider to content lives on the bar's border-b.
          <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
            <button className="text-[12px] text-text-strong truncate hover:text-accent cursor-pointer transition-colors bg-transparent border-none p-0" onClick={() => { onFileOpen?.(tab.path || '') }} title={i18nT('pages.chat.sidePanel.open_in_editor_2', { path: tab.path || '' })}>
              {/* Bare filename: the tab title carries '- Diff', which would
                  read redundantly next to the Turn Diff badge here. */}
              {(tab.path || '').split('/').pop() || tab.title}
            </button>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/15 text-accent font-medium shrink-0">{i18nT('pages.chat.sidePanel.turn_diff')}</span>
            {(added > 0 || removed > 0) && <span className="text-[11px] font-mono font-semibold shrink-0">{added > 0 && <span className="text-ok">+{added}</span>}{removed > 0 && <span className="text-danger ml-1.5">-{removed}</span>}</span>}
            <span className="flex-1" />
            <button onClick={() => onFileOpen?.(tab.path || '')} className="flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none" title={i18nT('pages.chat.sidePanel.open_in_editor')} aria-label={i18nT('pages.chat.sidePanel.open_in_editor')}><Pen size={14} /></button>
            <button onClick={() => setDiffSideBySide(v => !v)} className={`flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors border-none ${diffSideBySide ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`} title={diffSideBySide ? i18nT('pages.chat.sidePanel.switch_to_unified_view') : i18nT('pages.chat.sidePanel.switch_to_split_view')} aria-label={diffSideBySide ? i18nT('pages.chat.sidePanel.switch_to_unified_view') : i18nT('pages.chat.sidePanel.switch_to_split_view')}><Columns2 size={14} /></button>
            <button onClick={() => setDiffLineNumbers(v => !v)} className={`flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors border-none ${diffLineNumbers ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`} title={diffLineNumbers ? i18nT('pages.chat.sidePanel.hide_line_numbers') : i18nT('pages.chat.sidePanel.show_line_numbers')} aria-label={diffLineNumbers ? i18nT('pages.chat.sidePanel.hide_line_numbers') : i18nT('pages.chat.sidePanel.show_line_numbers')}><Hash size={14} /></button>
          </div>
        }
      >
        <DiffPanel filePath={tab.path || ''} original={tab.original || ''} modified={tab.modified || ''} lineNumbers={diffLineNumbers} sideBySide={diffSideBySide} />
      </DetailPanel>
    )
  }
  return null
}

// The tab loop and the cross-slot app list in `SidePanel.tsx` render these.
export { AppPanelTabBody, AppPanelTabsErrorNotice, McpAppTabBody, TabBody }
