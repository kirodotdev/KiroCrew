import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useQuery, useQueryClient, useMutation, useQueries } from '@tanstack/react-query'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { AlertTriangle, MessageSquare, Eye, CornerDownRight, Copy, ArrowUpFromLine } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAppDispatch } from '../../store'
import { setPendingInput } from '../../store/chatSlice'
import { Skeleton } from '../../components/ui'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from '../../components/ui/context-menu'
import { fileExplorerApi } from './api'
import { basename, dirname, loadState, saveState, isShortcut } from './utils'
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
  const [contextNode, setContextNode] = useState<TreeEntry | null>(null)
  const [initialized, setInitialized] = useState(false)

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
    const saved = loadState()
    if (saved && saved.folderTabs?.length) {
      const ft = saved.folderTabs.map((t: Partial<FolderTab>) => ({ ...newFolderTab(t.rootPath, t.label), id: t.id!, expanded: t.expanded || { [t.rootPath!]: true } }))
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
  const { data: treeData } = useQuery({
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

  const openFile = useCallback(async (path: string, _opts: { reveal?: boolean } = {}) => {
    if (!activeFolder) return
    const folderId = activeFolder.id
    const existing = fileTabsRef.current.find((ft) => ft.path === path && ft.folderId === folderId)
    if (existing) { setActiveFileId(existing.id); return }
    const ft = newFileTab(path, folderId)
    setFileTabs((tabs) => [...tabs, ft])
    setActiveFileId(ft.id)
  }, [activeFolder])

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
    const root = activeFolder?.rootPath || '/'
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
      if (remaining.length === 0) { const fresh = newFolderTab('/'); setActiveFolderId(fresh.id); return [fresh] }
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
      <PathBar rootPath={activeFolder.rootPath} gitInfo={rootGitInfo} onChangeRoot={changeRoot} onNavigate={openMaybe} />
      <div className="mc-fe-split">
        <div className="mc-fe-left" style={{ width: leftWidth }}>
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
            correct for a window splitter but is non-interactive per jsx-a11y. */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <div className="mc-fe-resizer" aria-label={i18nT('apps.fileExplorer.fileExplorerPage.resize_panel')} aria-orientation="vertical" role="separator" tabIndex={-1} style={{ touchAction: 'none' }} {...leftResize} />
        <div className="mc-fe-right">
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
