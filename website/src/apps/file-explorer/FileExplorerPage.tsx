import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useQuery, useQueryClient, useMutation, useQueries } from '@tanstack/react-query'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { AlertTriangle, MessageSquare, Eye, CornerDownRight, Copy, ArrowUpFromLine, ChevronDown, ChevronUp, X } from 'lucide-react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAppDispatch } from '../../store'
import { setPendingInput } from '../../store/chatSlice'
import { Skeleton, Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useIsMobile } from '../../hooks/useIsMobile'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from '../../components/ui/context-menu'
import { fileExplorerApi } from './api'
import { basename, dirname, loadState, saveState, isShortcut } from './utils'
import { FILE_EXPLORER_PATH_PARAM, underRoot, parentDir, revealChain } from './deepLink'
import { isAbsolutePath } from '../../utils/fileReadUrl'
import { copyToClipboard } from '../../utils/clipboard'
import { FE_CSS } from './styles'
import TabStrip from './TabStrip'
import PathBar from './PathBar'
import TreeNode from './TreeNode'
import FileViewer from './FileViewer'
import SearchPanel from './SearchPanel'
import type { FolderTab, FileTab, TreeEntry, GitInfo } from './types'

import { i18nT } from '../../i18n/t'
const newFolderTab = (rootPath = '/', label = ''): FolderTab => ({
  id: `ft-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
  rootPath, label,
  expanded: { [rootPath]: true },
  showSearch: false,
})

const newFileTab = (path: string, folderId: string): FileTab => ({
  id: `of-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
  path, folderId,
})

export default function FileExplorerPage() {
  const navigate = useNavigate()
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()

  const [folderTabs, setFolderTabs] = useState<FolderTab[]>([])
  const [fileTabs, setFileTabs] = useState<FileTab[]>([])
  const [activeFolderId, setActiveFolderId] = useState<string | null>(null)
  const [activeFileId, setActiveFileId] = useState<string | null>(null)
  const [leftWidth, setLeftWidth] = useState(280)
  const isMobile = useIsMobile()
  // The tree is a fixed 280px `flex-shrink:0` pane, so at 390px it left the
  // viewer 106px inside a `overflow:hidden` split -- unreadable and impossible
  // to scroll into view. While narrow it becomes a drawer reached from a bar at
  // the TOP, so the viewer owns the full width. Drawer-only state: the desktop
  // always shows the tree, and `leftWidth` stays the user's desktop preference.
  const [treeOpen, setTreeOpen] = useState(false)
  const treeBar = isMobile && !treeOpen
  const treeFull = isMobile && treeOpen
  const [contextNode, setContextNode] = useState<TreeEntry | null>(null)
  const [initialized, setInitialized] = useState(false)
  // The health-derived default root, for tabs opened after initialization.
  const defaultRootRef = useRef('/')

  // ── Deep link (`?path=`) ──
  // Captured off the URL and consumed immediately (history REPLACE, never
  // push): a reload or Back must not reopen the file a second time, and the
  // saved tab state — not the URL — is what this page restores from. The value
  // lives in component state from here on, so consuming the param cannot drop
  // a link the still-loading health read has yet to satisfy. Same shape as the
  // notifications page's `?note=` capture.
  const [searchParams, setSearchParams] = useSearchParams()
  const [pendingLink, setPendingLink] = useState<string | null>(null)
  useEffect(() => {
    const raw = searchParams.get(FILE_EXPLORER_PATH_PARAM)
    if (raw === null) return
    setPendingLink(raw)
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete(FILE_EXPLORER_PATH_PARAM)
      return next
    }, { replace: true })
  }, [searchParams, setSearchParams])
  /** Why the last link could not be opened, shown where the tree errors show. */
  const [linkError, setLinkError] = useState<{ path: string; message: string } | null>(null)
  /** A link this page declined to follow (relative path) — nothing failed, so
   *  this is kept apart from `linkError` and never dressed as an error. */
  const [linkRefused, setLinkRefused] = useState<string | null>(null)

  const activeFolder = useMemo(() => folderTabs.find((t) => t.id === activeFolderId) || folderTabs[0] || null, [folderTabs, activeFolderId])
  const activeFile = useMemo(() => fileTabs.find((t) => t.id === activeFileId) || null, [fileTabs, activeFileId])

  // ── Health (React Query) ──
  const { data: healthData, error: healthError } = useQuery({
    queryKey: ['file-explorer', 'health'],
    queryFn: () => fileExplorerApi.health(),
  })

  // ── Initialization from health data ──
  useEffect(() => {
    if (!healthData || initialized) return
    const roots = healthData.allowedRoots || []
    // Open at the user's home dir. Prefer the backend-reported `home` (must be
    // an allowed root); for older backends that don't send it, recognize
    // home-style roots on Linux (/home/<u>) and macOS (/Users/<u>). Never fall
    // back to "shortest root" — on macOS that picked /opt over /Users/<u>.
    const home =
      (healthData.home && roots.includes(healthData.home) ? healthData.home : undefined) ??
      roots.find((r) => r.includes('/home/') || r.startsWith('/Users/'))
    const defaultRoot = home || roots[0] || '/'
    defaultRootRef.current = defaultRoot
    const saved = loadState()
    if (saved && saved.folderTabs?.length) {
      const ft = saved.folderTabs.map((t: Partial<FolderTab>) => {
        // A tab outside every allowed root (e.g. the old '/' default) can only
        // 403, so it reopens at the default root instead.
        const ok = !!t.rootPath && (!roots.length || roots.some((r) => underRoot(t.rootPath!, r)))
        return ok
          ? { ...newFolderTab(t.rootPath, t.label), id: t.id!, expanded: t.expanded || { [t.rootPath!]: true } }
          : { ...newFolderTab(defaultRoot, t.label), id: t.id! }
      })
      setFolderTabs(ft)
      setActiveFolderId(saved.activeFolderId || ft[0].id)
      if (saved.fileTabs?.length) {
        setFileTabs(saved.fileTabs.map((f: Partial<FileTab>) => ({ ...newFileTab(f.path!, f.folderId!), id: f.id! })))
        setActiveFileId(saved.activeFileId || null)
      }
      if (saved.leftWidth) setLeftWidth(saved.leftWidth)
    } else {
      const t = newFolderTab(defaultRoot)
      setFolderTabs([t])
      setActiveFolderId(t.id)
    }
    setInitialized(true)
  }, [healthData, initialized])

  // ── Persist state (debounced to avoid jank during resize) ──
  useEffect(() => {
    if (!initialized) return
    const timer = setTimeout(() => {
      saveState({
        folderTabs: folderTabs.map((t) => ({ id: t.id, rootPath: t.rootPath, label: t.label, expanded: t.expanded })),
        fileTabs: fileTabs.map((f) => ({ id: f.id, path: f.path, folderId: f.folderId })),
        activeFolderId, activeFileId, leftWidth,
      })
    }, 300)
    return () => clearTimeout(timer)
  }, [folderTabs, fileTabs, activeFolderId, activeFileId, leftWidth, initialized])

  // ── Tab updaters ──
  const updateFolderTab = useCallback((id: string, patch: Partial<FolderTab> | ((t: FolderTab) => FolderTab)) => {
    setFolderTabs((tabs) => tabs.map((t) => (t.id === id ? (typeof patch === 'function' ? patch(t) : { ...t, ...patch }) : t)))
  }, [])

  // ── Tree (React Query) — consumed directly at render, no local state sync ──
  const { data: treeData, error: treeError } = useQuery({
    queryKey: ['file-explorer', 'tree', activeFolder?.rootPath],
    queryFn: () => fileExplorerApi.tree(activeFolder!.rootPath, 2),
    enabled: !!activeFolder?.rootPath && initialized,
  })

  // ── Git status (React Query — derived via useMemo, no local state) ──
  const { data: rootGitData } = useQuery({
    queryKey: ['file-explorer', 'git-status', activeFolder?.rootPath],
    queryFn: () => fileExplorerApi.gitStatus(activeFolder!.rootPath),
    enabled: !!activeFolder?.rootPath && initialized,
    staleTime: 30_000,
  })

  // Git status for discovered repos in tree (useQueries)
  const discoveredRoots = useMemo(() => {
    const found = new Set<string>()
    function walk(node: TreeEntry | null) {
      if (!node) return
      if (node.isGitRoot && node.path) found.add(node.path)
      if (Array.isArray(node.children)) node.children.forEach(walk)
    }
    if (treeData?.entries) treeData.entries.forEach(walk)
    return [...found]
  }, [treeData])

  const gitQueries = useQueries({
    queries: discoveredRoots.map((p) => ({
      queryKey: ['file-explorer', 'git-status', p],
      queryFn: () => fileExplorerApi.gitStatus(p),
      staleTime: 30_000,
      enabled: !!activeFolder,
    })),
  })

  // Derive gitMap directly from query results (no useState)
   
  const gitQueryData = gitQueries.map(q => q.data)
  const tabGitMap = useMemo(() => {
    const map = new Map<string, GitInfo>()
    if (rootGitData?.repoRoot) map.set(rootGitData.repoRoot, rootGitData)
    for (const d of gitQueryData) {
      if (d?.repoRoot) map.set(d.repoRoot, d)
    }
    return map
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootGitData, ...gitQueryData])

  const toggleExpand = useCallback(async (node: TreeEntry) => {
    if (!activeFolder) return
    const wasOpen = !!activeFolder.expanded[node.path]
    updateFolderTab(activeFolder.id, (cur: FolderTab) => ({ ...cur, expanded: { ...cur.expanded, [node.path]: !wasOpen } }))
  }, [activeFolder, updateFolderTab])

  // ── File read (React Query) — consumed directly at render, no local state sync ──
  const { data: fileData, error: fileError, isLoading: fileLoading } = useQuery({
    queryKey: ['file-explorer', 'read', activeFile?.path],
    queryFn: () => fileExplorerApi.read(activeFile!.path),
    enabled: !!activeFile?.path,
  })

  // ── File operations ──
  const fileTabsRef = useRef(fileTabs)
  fileTabsRef.current = fileTabs

  /** Open `path` as a file tab under the folder tab `folderId`, and make both
   *  active. Reuses an existing tab for the same path in that folder. */
  const openFileInFolder = useCallback((folderId: string, path: string) => {
    // Close the drawer on pick, or the full-width tree is a one-way door: the
    // file opens behind it with nothing on screen to say so.
    if (isMobile) setTreeOpen(false)
    setActiveFolderId(folderId)
    const existing = fileTabsRef.current.find((ft) => ft.path === path && ft.folderId === folderId)
    if (existing) { setActiveFileId(existing.id); return }
    const ft = newFileTab(path, folderId)
    setFileTabs((tabs) => [...tabs, ft])
    setActiveFileId(ft.id)
  }, [isMobile])

  const openFile = useCallback(async (path: string, _opts: { reveal?: boolean } = {}) => {
    if (!activeFolder) return
    openFileInFolder(activeFolder.id, path)
  }, [activeFolder, openFileInFolder])

  const reloadFile = useCallback(() => {
    if (!activeFile) return
    queryClient.invalidateQueries({ queryKey: ['file-explorer', 'read', activeFile.path] })
  }, [activeFile, queryClient])

  const downloadFile = useCallback(() => {
    if (!activeFile || !fileData) return
    const content = fileData.content || ''
    const blob = fileData.encoding === 'base64'
      ? (() => { const b = atob(content); const u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return new Blob([u], { type: fileData.mime || 'application/octet-stream' }) })()
      : new Blob([content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = basename(activeFile.path)
    document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url)
  }, [activeFile, fileData])

  // ── Tab management ──
  const newFolderTabAction = useCallback(() => {
    const root = activeFolder?.rootPath || defaultRootRef.current
    const t = newFolderTab(root)
    setFolderTabs((tabs) => [...tabs, t])
    setActiveFolderId(t.id); setActiveFileId(null)
  }, [activeFolder])

  const closeFolderTab = useCallback((id: string) => {
    setFileTabs((tabs) => tabs.filter((ft) => ft.folderId !== id))
    setActiveFileId((cur) => {
      if (!cur) return null
      const ft = fileTabsRef.current.find(t => t.id === cur)
      return ft?.folderId === id ? null : cur
    })
    setFolderTabs((tabs) => {
      const remaining = tabs.filter((t) => t.id !== id)
      if (remaining.length === 0) { const fresh = newFolderTab(defaultRootRef.current); setActiveFolderId(fresh.id); return [fresh] }
      setActiveFolderId((cur) => cur === id ? remaining[0].id : cur)
      return remaining
    })
  }, [])

  const closeFileTab = useCallback((id: string) => {
    setFileTabs((prev) => {
      const next = prev.filter((t) => t.id !== id)
      setActiveFileId((curActive) => {
        if (curActive !== id) return curActive
        const remaining = next.filter((t) => t.folderId === activeFolderId)
        return remaining.length > 0 ? remaining[remaining.length - 1].id : null
      })
      return next
    })
  }, [activeFolderId])

  const activateFolder = useCallback((id: string) => { setActiveFolderId(id); setActiveFileId(null) }, [])
  const activateFile = useCallback((id: string) => {
    const ft = fileTabsRef.current.find((t) => t.id === id)
    if (ft) { setActiveFolderId(ft.folderId); setActiveFileId(id) }
  }, [])
  const renameFolderTab = useCallback((id: string, label: string) => { updateFolderTab(id, { label }) }, [updateFolderTab])

  // ── Path bar ──
  const changeRoot = useCallback((newPath: string) => {
    if (!newPath || !activeFolder || newPath === activeFolder.rootPath) return
    setFileTabs((tabs) => tabs.filter((ft) => ft.folderId !== activeFolder.id))
    setActiveFileId(null)
    updateFolderTab(activeFolder.id, { rootPath: newPath, expanded: { [newPath]: true } })
  }, [activeFolder, updateFolderTab])

  // ── Resolve (React Query mutation) ──
  const resolveMutation = useMutation({
    mutationFn: (path: string) => fileExplorerApi.resolve(path),
    onSuccess: (r, path) => {
      if (r && r.exists && r.type === 'dir') changeRoot(path)
      else if (r && r.exists && r.type === 'file') {
        const parent = dirname(path)
        if (activeFolder && parent !== activeFolder.rootPath) updateFolderTab(activeFolder.id, { rootPath: parent, expanded: { [parent]: true } })
        openFile(path)
      } else changeRoot(path)
    },
    onError: (_, path) => changeRoot(path),
  })

  const openMaybe = useCallback((path: string) => { resolveMutation.mutate(path) }, [resolveMutation])

  // ── Deep link: resolve, then reveal ──
  // The link only NAMES a path. The backend decides what it is and whether this
  // app may show it: `/resolve` runs the same `_safe_path` gate as every read
  // (outside the allowed roots or in a sensitive location is a 403; a vanished
  // file is `exists: false`), so a link can never make the page show something
  // the tree would refuse.
  //
  // Unlike the path bar's `openMaybe`, which re-roots the ACTIVE tab, a link
  // reveals the path where it already is: in the tab whose root contains it,
  // with the folders between that root and the path expanded so the tree shows
  // it; only when no open tab contains it does a new tab open, rooted at its
  // folder. The reader's tabs are theirs — a link must not re-root one.
  const revealMutation = useMutation({
    mutationFn: (path: string) => fileExplorerApi.resolve(path),
    onSuccess: (r, asked) => {
      if (!r?.exists) {
        setLinkError({ path: asked, message: i18nT('apps.fileExplorer.fileExplorerPage.link_target_missing') })
        return
      }
      const isDir = r.type === 'dir'
      if (!isDir && r.type !== 'file') {
        setLinkError({ path: asked, message: i18nT('apps.fileExplorer.fileExplorerPage.link_target_unsupported') })
        return
      }
      // Tab state is keyed by the backend's spelling of the path (symlinks
      // followed, `~` expanded) — the one its tree nodes carry — not by the
      // string the link asked about; `expanded` is matched byte for byte.
      const path = r.path || asked
      const dir = isDir ? path : parentDir(path) ?? path
      // The active tab first, so a link into the folder the reader is already
      // looking at never switches tabs under them; then any other open tab.
      const containing = [activeFolder, ...folderTabs].find(
        (t): t is FolderTab => !!t && underRoot(path, t.rootPath),
      )
      const tab = containing ?? newFolderTab(dir)
      if (containing) {
        const chain = revealChain(tab.rootPath, dir)
        updateFolderTab(tab.id, (cur) => ({
          ...cur,
          expanded: { ...cur.expanded, ...Object.fromEntries(chain.map((d) => [d, true])) },
        }))
      } else {
        setFolderTabs((tabs) => [...tabs, tab])
      }
      if (isDir) { setActiveFolderId(tab.id); setActiveFileId(null) }
      else openFileInFolder(tab.id, path)
    },
    onError: (err, asked) => {
      setLinkError({ path: asked, message: (err as Error).message })
    },
  })

  // Consume the captured link once the saved tabs exist to match it against.
  // `setPendingLink(null)` is what makes it once: the capture effect above only
  // re-arms when the URL carries the param again.
  const revealMutate = revealMutation.mutate
  useEffect(() => {
    if (!initialized || pendingLink === null) return
    setPendingLink(null)
    setLinkError(null)
    setLinkRefused(null)
    // The backend resolves a relative path against ITS working directory, which
    // the link's author cannot know — refuse here rather than open whatever
    // happens to sit there. `~` and `~/…` are absolute in this sense (they expand
    // to the gateway user's own home); `~name` is not, see `isAbsolutePath`.
    // No request is sent, so this is a refusal, not an error (see render).
    if (!isAbsolutePath(pendingLink)) {
      setLinkRefused(pendingLink)
      return
    }
    revealMutate(pendingLink)
  }, [initialized, pendingLink, revealMutate])

  const toggleSearch = useCallback(() => {
    if (!activeFolder) return
    updateFolderTab(activeFolder.id, (cur: FolderTab) => ({ ...cur, showSearch: !cur.showSearch }))
  }, [activeFolder, updateFolderTab])

  // ── Chat launcher ──
  // Hand the prompt to ChatPage via Redux pendingInput, then navigate with
  // ?prefill=1 so it lands in the composer (ChatPage clears the param and
  // shows the prefill hint). ChatPage does not read `?message=`, and `?msg=`
  // is a scroll-to-timestamp deep-link, not a prefill.
  const chatAboutPath = useCallback((path: string, kind = 'file') => {
    const noun = kind === 'dir' ? 'folder' : 'file'
    const message = `I'd like to discuss the ${noun} \`${path}\`. Please read it and help me understand or modify it.`
    dispatch(setPendingInput(message))
    navigate('/chat?prefill=1')
  }, [dispatch, navigate])

  // ── Context menu ──
  const onTreeContextMenu = useCallback((_e: React.MouseEvent, node: TreeEntry) => { setContextNode(node) }, [])

  // ── Keyboard shortcuts ──
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isShortcut(e) && e.key.toLowerCase() === 'f') { e.preventDefault(); toggleSearch() }
      else if (isShortcut(e) && e.key.toLowerCase() === 't') { e.preventDefault(); newFolderTabAction() }
      else if (isShortcut(e) && e.key.toLowerCase() === 'w') { e.preventDefault(); if (activeFile) closeFileTab(activeFile.id); else if (activeFolder) closeFolderTab(activeFolder.id) }
      else if (e.key === 'Escape' && activeFolder?.showSearch) { e.preventDefault(); toggleSearch() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggleSearch, newFolderTabAction, closeFolderTab, closeFileTab, activeFile, activeFolder])

  // ── Resizer ──
  const startWRef = useRef(0)
  const leftDraggingRef = useRef(false)
  const leftResize = usePointerDrag({
    threshold: 0,
    onStart: () => { startWRef.current = leftWidth; leftDraggingRef.current = true; document.body.style.cursor = 'col-resize' },
    onMove: ({ dx }) => { setLeftWidth(Math.max(180, Math.min(640, startWRef.current + dx))) },
    onEnd: () => { leftDraggingRef.current = false; document.body.style.cursor = '' },
  })
  // Unmount guard: onEnd can't fire if the pane unmounts mid-drag, so clear the
  // global resize cursor here to avoid leaving it stuck.
  useEffect(() => () => { if (leftDraggingRef.current) document.body.style.cursor = '' }, [])

  // ── Derived state (must be above early returns for Rules of Hooks) ──
  const rootGitInfo = useMemo(() => {
    if (!activeFolder) return null
    if (tabGitMap.has(activeFolder.rootPath)) return tabGitMap.get(activeFolder.rootPath)!
    let best: GitInfo | null = null
    for (const [root, info] of tabGitMap.entries()) {
      if (activeFolder.rootPath === root || activeFolder.rootPath.startsWith(root + '/')) {
        if (!best || root.length > best.repoRoot.length) best = info
      }
    }
    return best
    // The memo only reads activeFolder.rootPath (and its null-ness, which
    // rootPath being present implies); depending on the whole activeFolder
    // object would recompute on unrelated tab-field changes with no effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabGitMap, activeFolder?.rootPath])

  // ── Render ──
  if (!initialized) return <div className="mc-fe-root"><style>{FE_CSS}</style><div className="mc-fe-empty" style={{ height: '100%' }}><Skeleton className="h-full w-full" /></div></div>
  if (!activeFolder) return null

  const currentFileTabs = fileTabs.filter((ft) => ft.folderId === activeFolder.id)
  const showSearch = activeFolder.showSearch
  const viewingFile = activeFile && activeFile.folderId === activeFolder.id ? activeFile : null

  // Derive treeRoot from React Query data — child lazy-loading handled by TreeNode's own useQuery
  const treeRoot: TreeEntry | null = treeData
    ? { name: basename(activeFolder.rootPath) || activeFolder.rootPath, path: activeFolder.rootPath, type: 'dir', children: treeData.entries }
    : null

  return (
    <div className="mc-fe-root">
      <style>{FE_CSS}</style>
      <TabStrip
        folderTabs={folderTabs}
        fileTabs={currentFileTabs}
        activeFolderId={activeFolderId}
        activeFileId={viewingFile?.id || null}
        onActivateFolder={activateFolder}
        onActivateFile={activateFile}
        onCloseFolder={closeFolderTab}
        onCloseFile={closeFileTab}
        onNewFolder={newFolderTabAction}
        onRenameFolder={renameFolderTab}
      />
      {healthError && <div className="mc-fe-banner"><AlertTriangle size={12} /> {i18nT('apps.fileExplorer.fileExplorerPage.backend_not_reachable')} {(healthError as Error).message}</div>}
      {treeError && <ErrorNotice variant="block" title={i18nT('apps.fileExplorer.fileExplorerPage.cannot_open_folder')} message={(treeError as Error).message} askAgent />}
      {/* A link the backend could not open: refused (403), vanished, or not a
          file/folder. The reason sits BELOW the lead: it is the backend's own
          sentence (the journal lookup key the hand-off recovers endpoint and
          status from), which reads as a detail under the plain-language title
          rather than as the lead. askAgent on: this page holds no draft, and a
          refused or vanished path is exactly what the agent can explain. */}
      {linkError && (
        <ErrorNotice
          variant="block"
          title={i18nT('apps.fileExplorer.fileExplorerPage.link_open_failed', { path: linkError.path })}
          message={linkError.message}
          messagePlacement="below"
          onDismiss={() => setLinkError(null)}
          askAgent
          testId="file-explorer-link-error"
        />
      )}
      {/* A link this page declined to follow. Deliberately NOT an ErrorNotice:
          nothing failed — no request was sent — so there is no journal entry
          for a hand-off to recover and nothing the agent could explain beyond
          this sentence. It wears the page's own warn banner (the dress of the
          backend-not-reachable notice above), not danger tokens, and is
          dismissable because it sits above a tree that is still the page. */}
      {linkRefused !== null && (
        <div className="mc-fe-banner" role="status" data-testid="file-explorer-link-refused">
          <AlertTriangle size={12} aria-hidden="true" />
          <span className="mc-fe-banner-text">{i18nT('apps.fileExplorer.fileExplorerPage.link_target_relative', { path: linkRefused })}</span>
          <button type="button" className="mc-fe-iconbtn mc-fe-banner-dismiss" aria-label={i18nT('app.dismiss')} onClick={() => setLinkRefused(null)}>
            <X size={12} />
          </button>
        </div>
      )}
      <PathBar rootPath={activeFolder.rootPath} gitInfo={rootGitInfo} onChangeRoot={changeRoot} onNavigate={openMaybe} />
      <div className={`mc-fe-split${isMobile ? ' is-stacked' : ''}`}>
        {/* Narrow: the control that reaches the tree sits at the TOP, so no
            horizontal space is reserved for it and the viewer gets the full
            width. Hidden rather than absent on a desktop, where the tree pane
            is always on screen. */}
        {isMobile && (
          <Btn
            onClick={() => setTreeOpen(!treeOpen)}
            className="mc-fe-treebar"
            aria-expanded={treeOpen}
          >
            {treeOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
            {activeFolder.label || basename(activeFolder.rootPath) || activeFolder.rootPath}
          </Btn>
        )}
        <div
          className={`mc-fe-left${treeBar ? ' is-hidden' : ''}`}
          style={{ width: treeFull ? '100%' : leftWidth }}
        >
          <ContextMenu onOpenChange={(open) => { if (!open) setContextNode(null) }}>
            <ContextMenuTrigger asChild>
              {treeRoot ? (
                <div className="mc-fe-tree">
                  <TreeNode
                    node={treeRoot}
                    depth={0}
                    expanded={activeFolder.expanded}
                    toggleExpand={toggleExpand}
                    selectedPath={viewingFile?.path || ''}
                    onSelect={(n) => openFile(n.path)}
                    gitMap={tabGitMap}
                    onContextMenu={onTreeContextMenu}
                  />
                </div>
              ) : treeError ? (
                <div className="mc-fe-empty" style={{ flexDirection: 'column', gap: 8 }}>
                  {i18nT('apps.fileExplorer.fileExplorerPage.folder_unavailable')}
                  <Btn onClick={() => changeRoot(dirname(activeFolder.rootPath))}>
                    <CornerDownRight size={13} /> {i18nT('apps.fileExplorer.fileExplorerPage.go_to_parent_folder')}
                  </Btn>
                </div>
              ) : <div className="mc-fe-empty"><Skeleton className="h-full w-full" /></div>}
            </ContextMenuTrigger>
            {contextNode && (
              <ContextMenuContent className="min-w-[200px]">
                <ContextMenuItem className="gap-2 text-[12px]" onSelect={() => chatAboutPath(contextNode.path, contextNode.type)}>
                  <MessageSquare size={12} /> {i18nT('apps.fileExplorer.fileExplorerPage.chat_about_this')} {contextNode.type === 'dir' ? 'folder' : 'file'}
                </ContextMenuItem>
                {contextNode.type !== 'dir' && (
                  <ContextMenuItem className="gap-2 text-[12px]" onSelect={() => openFile(contextNode.path)}>
                    <Eye size={12} /> {i18nT('apps.fileExplorer.fileExplorerPage.open')}
                  </ContextMenuItem>
                )}
                {contextNode.type === 'dir' && (
                  <ContextMenuItem className="gap-2 text-[12px]" onSelect={() => changeRoot(contextNode.path)}>
                    <CornerDownRight size={12} /> {i18nT('apps.fileExplorer.fileExplorerPage.open_as_workspace_root')}
                  </ContextMenuItem>
                )}
                <ContextMenuItem className="gap-2 text-[12px]" onSelect={() => copyToClipboard(contextNode.path)}>
                  <Copy size={12} /> {i18nT('apps.fileExplorer.fileExplorerPage.copy_path')}
                </ContextMenuItem>
                <ContextMenuItem className="gap-2 text-[12px]" onSelect={() => changeRoot(dirname(contextNode.path))}>
                  <ArrowUpFromLine size={12} /> {i18nT('apps.fileExplorer.fileExplorerPage.reveal_parent')}
                </ContextMenuItem>
              </ContextMenuContent>
            )}
          </ContextMenu>
        </div>
        {/* Pane splitter: mouse-drag-only resize affordance; role=separator is
            correct for a window splitter but is non-interactive per jsx-a11y.
            Absent while narrow -- it is pointer-only, so on touch it would cost
            width and buy nothing. */}
        {!isMobile && <div className="mc-fe-resizer" aria-label={i18nT('apps.fileExplorer.fileExplorerPage.resize_panel')} aria-orientation="vertical" role="separator" tabIndex={-1} style={{ touchAction: 'none' }} {...leftResize} />}
        <div className={`mc-fe-right${treeFull ? ' is-hidden' : ''}`}>
          {showSearch ? (
            <SearchPanel
              rootPath={activeFolder.rootPath}
              onClose={toggleSearch}
              onJump={(r) => { openFile(r.file, { reveal: true }); updateFolderTab(activeFolder.id, { showSearch: false }) }}
            />
          ) : (
            <FileViewer
              filePath={viewingFile?.path || null}
              fileMeta={fileData || null}
              content={fileData?.content ?? ''}
              loading={fileLoading}
              error={fileError ? (fileError as Error).message : null}
              onReload={reloadFile}
              onDownload={downloadFile}
            />
          )}
        </div>
      </div>
    </div>
  )
}
