/**
 * Notes — a markdown notebook backed by a git repository.
 *
 * Single-note view, no tabs: the left panel is the only navigation surface.
 * Search swaps the tree for flat ranked results; clearing restores the tree.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import type { CSSProperties } from 'react'
import {
  Check,
  ChevronDown,
  Code,
  FileText,
  ListFilter,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  RefreshCw,
} from 'lucide-react'
import { i18nT } from '../../i18n/t'
import {
  ACCENT,
  CHANGES_POLL_MS,
  COLUMN_MAX_WIDTH,
  COLUMN_PAD_X,
  DEFAULT_AUTO_SYNC_MINS,
  DEFAULT_SORT,
  DEFAULT_SYNC_SHORTCUT,
  FONT_BODY,
  FONT_MONO,
  LS,
  MAX_AUTO_SYNC_MINS,
  MIN_AUTO_SYNC_MINS,
  PANEL_DEFAULT_WIDTH,
  PANEL_MAX_WIDTH,
  PANEL_MIN_WIDTH,
  SAVE_DEBOUNCE_MS,
  SORTS,
} from './constants'
import { MDNB_CSS } from './styles'
import { listViewLabel, paneViewLabel, sortLabel, syncedAgoLabel } from './labels'
import { notesApi } from './api'
import { InlineTitle } from './BlockEditor'
import { Preview } from './Preview'
import { ConnectVault } from './ConnectVault'
import { SettingsBar, SettingsPage } from './SettingsPage'
import Clickable from '../../components/Clickable'
import { NoteRow, renderTree } from './NoteRow'
import {
  FM_RE,
  agoBucket,
  buildTree,
  loadPref,
  matchesShortcut,
  savePref,
  shiftListItem,
} from './utils'
import type { Backlink, EditRange, Note, SearchHit, Shortcut, Vault } from './types'

/** Backend capabilities this UI bundle needs. */
const REQUIRED_FEATURES = [
  'createdAt',
  'attach',
  'changes',
  'saveGuard',
  'forget',
  'pat',
  'newNote',
  'move',
  'knowledge',
  'pickFolder',
]

const iconBtn: CSSProperties = {
  width: '28px',
  height: '28px',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  borderRadius: '8px',
  background: 'transparent',
  border: 'none',
  color: 'var(--muted)',
  cursor: 'pointer',
  flexShrink: 0,
}

export default function MdNotebookPage() {
  const [vaults, setVaults] = useState<Vault[] | null>(null)
  const [notes, setNotes] = useState<Note[]>([])
  const [activePath, setActivePath] = useState<string | null>(null)
  const [content, setContent] = useState('')
  const [backlinks, setBacklinks] = useState<Backlink[]>([])
  const [dirty, setDirty] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchHit[]>([])
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editBlock, setEditBlock] = useState<EditRange | null>(null)
  const [showConnect, setShowConnect] = useState(false)
  const [vaultSelOpen, setVaultSelOpen] = useState(false)
  const [sortOpen, setSortOpen] = useState(false)
  const [fileConflict, setFileConflict] = useState<{ mtime: number; disk: string } | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [auth, setAuth] = useState({ hasPat: false, hasGhAuth: false })

  const [mode, setMode] = useState<'rendered' | 'raw'>(() =>
    loadPref<'rendered' | 'raw'>(LS.view, 'rendered'),
  )
  const [sortKey, setSortKey] = useState<string>(() => {
    const k = loadPref<string>(LS.sort, DEFAULT_SORT)
    return k in SORTS ? k : DEFAULT_SORT
  })
  const [view, setView] = useState<'folders' | 'list'>(() =>
    loadPref<'folders' | 'list'>('mdnb-list-view', 'folders'),
  )
  const [panelOpen, setPanelOpen] = useState(() => loadPref<boolean>(LS.panelOpen, true))
  const [panelW, setPanelW] = useState(() => {
    const w = loadPref<number>(LS.panelWidth, PANEL_DEFAULT_WIDTH)
    return w >= PANEL_MIN_WIDTH && w <= PANEL_MAX_WIDTH ? w : PANEL_DEFAULT_WIDTH
  })
  const [activeVaultId, setActiveVaultId] = useState<string | null>(() =>
    loadPref<string | null>(LS.activeVault, null),
  )
  const [autoSync, setAutoSync] = useState(() => loadPref<boolean>(LS.autoSync, false))
  const [autoSyncMins, setAutoSyncMins] = useState(() =>
    loadPref<number>(LS.autoSyncMins, DEFAULT_AUTO_SYNC_MINS),
  )
  const [syncShortcut, setSyncShortcut] = useState<Shortcut>(() =>
    loadPref<Shortcut>(LS.syncShortcut, DEFAULT_SYNC_SHORTCUT),
  )
  // True while Settings is capturing a shortcut, so the keys being recorded do
  // not also fire a sync.
  const recordingShortcutRef = useRef(false)

  const setAutoSyncPref = useCallback((on: boolean) => {
    setAutoSync(on)
    savePref(LS.autoSync, on)
  }, [])
  const setAutoSyncMinsPref = useCallback((n: number) => {
    const v = Math.min(MAX_AUTO_SYNC_MINS, Math.max(MIN_AUTO_SYNC_MINS, Math.round(n) || DEFAULT_AUTO_SYNC_MINS))
    setAutoSyncMins(v)
    savePref(LS.autoSyncMins, v)
  }, [])
  const setSyncShortcutPref = useCallback((sc: Shortcut) => {
    setSyncShortcut(sc)
    savePref(LS.syncShortcut, sc)
  }, [])
  const [lastSync, setLastSync] = useState<number | null>(null)

  const saveTimer = useRef<number | null>(null)
  const contentRef = useRef('')
  const pathRef = useRef<string | null>(null)
  const vaultRef = useRef(activeVaultId)
  const mtimeRef = useRef<number | null>(null)
  const dirtyRef = useRef(false)
  const revRef = useRef(0)
  // Monotonic id for note-open reads: a slower earlier read (note A) must not
  // overwrite the editor after a later open (note B), which would discard B's
  // unsaved edit. Only the latest request may apply its result.
  const openSeqRef = useRef(0)
  useEffect(() => {
    dirtyRef.current = dirty
  }, [dirty])

  // Warn on reload/close while an edit is still pending: the save is debounced,
  // so a reload within the debounce window would destroy the timer and lose the
  // latest content before it reaches disk.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        e.preventDefault()
        e.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])

  // Re-render every 30s so the "5m ago" label ages without interaction.
  const [, setTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => setTick(t => t + 1), 30_000)
    return () => window.clearInterval(id)
  }, [])

  // ---- capability probe -------------------------------------------------
  // The gateway keeps an app's backend alive across UI reloads, so an older
  // process can still be serving. Detect that and name what is missing.
  // Through React Query so the result is cached and refetched like every other
  // server read in the dashboard, rather than a bare promise per mount.
  const { data: health } = useQuery({
    queryKey: ['md-notebook', 'health'],
    queryFn: () => notesApi.health(),
    retry: false,
  })
  const staleBackend = useMemo(() => {
    if (!health) return null
    const missing = REQUIRED_FEATURES.filter(f => !(health.features ?? []).includes(f))
    return missing.length ? missing : null
  }, [health])

  const loadVaults = useCallback(async () => {
    try {
      const { vaults: list, hasPat, hasGhAuth } = await notesApi.listVaults()
      setVaults(list)
      setAuth({ hasPat, hasGhAuth })
      if (list.length && !list.some(v => v.id === vaultRef.current)) {
        vaultRef.current = list[0].id
        setActiveVaultId(list[0].id)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setVaults(null)
    }
  }, [])

  useEffect(() => {
    void loadVaults()
  }, [loadVaults])

  const loadNotes = useCallback(async () => {
    if (!vaultRef.current) return
    const requested = vaultRef.current
    try {
      const { notes: list } = await notesApi.listNotes(requested)
      // The user can switch vaults while this is in flight; applying the reply
      // then would show the previous vault's notes under the new one.
      if (vaultRef.current !== requested) return
      setNotes(list)
    } catch (e) {
      if (vaultRef.current !== requested) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const flushSave = useCallback(async () => {
    if (saveTimer.current) {
      window.clearTimeout(saveTimer.current)
      saveTimer.current = null
    }
    if (!pathRef.current || !dirtyRef.current) return
    try {
      // Save until what landed matches what the editor holds. `contentRef` can
      // move while the request is in flight, and the debounce timer was
      // cancelled above — clearing `dirty` against a stale snapshot would leave
      // that newer text with nothing scheduled to persist it. Bounded so a fast
      // typist cannot spin here; whatever is left stays dirty for the next
      // debounce to pick up.
      for (let attempt = 0; attempt < 3; attempt++) {
        const sent = contentRef.current
        const res = await notesApi.saveNote(
          vaultRef.current,
          pathRef.current,
          sent,
          mtimeRef.current ?? undefined,
        )
        mtimeRef.current = res.mtime
        if (contentRef.current === sent) {
          setDirty(false)
          dirtyRef.current = false
          break
        }
      }
    } catch (e) {
      const body = (e as { body?: { code?: string; mtime?: number; disk?: string } }).body
      if (body?.code === 'ESTALE') {
        // The note changed on disk since it was opened. Surface both versions
        // rather than clobbering either.
        setFileConflict({ mtime: body.mtime ?? 0, disk: body.disk ?? '' })
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
    }
  }, [])

  const openNote = useCallback(async (path: string) => {
    if (!vaultRef.current) return
    // Claim this open's sequence NOW, before any await: ordering must reflect
    // the order openNote was CALLED, not the order the debounced saves happen
    // to finish. If it were bumped after flushSave, a dirty note A whose save
    // is slow could claim a higher seq than a later open B and clobber B.
    const seq = ++openSeqRef.current
    // Persist the outgoing note BEFORE the refs below are repointed. The save is
    // debounced, so a quick A-edit-then-open-B strands A's pending edit with
    // nothing left holding its path or content to retry from.
    await flushSave()
    // A later openNote superseded this one while the flush was in flight.
    if (openSeqRef.current !== seq) return
    // flushSave leaves `dirty` set when the write failed — an ESTALE conflict is
    // waiting for the user to choose a version. Navigating would discard it.
    if (dirtyRef.current) return
    // Settings is a page in this pane, so opening a note navigates away from it.
    setSettingsOpen(false)
    try {
      const requested = vaultRef.current
      const doc = await notesApi.readNote(requested, path)
      // The user can switch vaults OR open another note while this is in
      // flight. Applying a superseded read would point the editor at the wrong
      // note/vault and the next save would write it there — discarding the
      // newer note's unsaved edit. Only the latest open may apply.
      if (vaultRef.current !== requested || openSeqRef.current !== seq) return
      pathRef.current = path
      setActivePath(path)
      savePref(LS.openNote, path)
      setContent(doc.content)
      contentRef.current = doc.content
      mtimeRef.current = doc.mtime
      setBacklinks(doc.backlinks ?? [])
      setDirty(false)
      setEditBlock(null)
      setFileConflict(null)
    } catch (e) {
      // Don't surface an error from a read this open already lost to a later one.
      if (openSeqRef.current !== seq) return
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [flushSave])

  // Load the note list whenever the vault changes, then restore the open note.
  useEffect(() => {
    vaultRef.current = activeVaultId
    if (!activeVaultId) return
    savePref(LS.activeVault, activeVaultId)
    setLastSync(loadPref<number | null>(`mdnb-last-sync-${activeVaultId}`, null))
    void loadNotes()
  }, [activeVaultId, loadNotes])

  const restored = useRef(false)
  useEffect(() => {
    if (restored.current || activePath || !notes.length) return
    restored.current = true
    const remembered = loadPref<string | null>(LS.openNote, null)
    const target = remembered && notes.some(n => n.path === remembered) ? remembered : null
    if (target) void openNote(target)
  }, [notes, activePath, openNote])

  /** Edit the note body, debouncing the save. */
  const edit = useCallback(
    (next: string) => {
      setContent(next)
      contentRef.current = next
      setDirty(true)
      dirtyRef.current = true
      if (saveTimer.current) window.clearTimeout(saveTimer.current)
      saveTimer.current = window.setTimeout(() => {
        void flushSave().then(() => loadNotes())
      }, SAVE_DEBOUNCE_MS)
    },
    [flushSave, loadNotes],
  )

  // Mark the note dirty WITHOUT committing content — fired on the first
  // keystroke inside a block editor, before its blur/commit. This flips
  // dirtyRef so a capture-phase sync shortcut bails out (it reloads only when
  // clean) instead of reloading the note and discarding the in-progress edit.
  const markDirty = useCallback(() => {
    setDirty(true)
    dirtyRef.current = true
  }, [])

  // ---- block editing ----------------------------------------------------
  // Preview strips frontmatter, so its line indices are body-relative; the
  // offset re-aligns them with the file.
  const fmOffset = useCallback(() => {
    const m = FM_RE.exec(contentRef.current)
    if (!m) return 0
    // Subtract one only when the frontmatter ends with a newline (the usual
    // `---\n…\n---\n`): split then counts the trailing empty segment. A
    // frontmatter that ends AT EOF with no newline (`---\n…\n---`) has no such
    // segment, so subtracting would place the body offset ON the closing `---`
    // and a block edit would overwrite it.
    const lines = m[0].split('\n').length
    return m[0].endsWith('\n') ? lines - 1 : lines
  }, [])

  const startBlockEdit = useCallback(
    (start: number, end: number) => setEditBlock({ start, end }),
    [],
  )
  const cancelBlockEdit = useCallback(() => setEditBlock(null), [])

  const commitBlockEdit = useCallback(
    (text: string) => {
      if (editBlock) {
        const offset = fmOffset()
        const lines = contentRef.current.split('\n')
        const count = Math.max(0, editBlock.end - editBlock.start + 1)
        const before = lines
          .slice(editBlock.start + offset, editBlock.start + offset + count)
          .join('\n')
        if (text !== before && !(count === 0 && text.trim() === '')) {
          lines.splice(editBlock.start + offset, count, ...text.split('\n'))
          edit(lines.join('\n'))
        }
      }
      setEditBlock(null)
    },
    [editBlock, edit, fmOffset],
  )

  /**
   * Enter inside a block: `before` stays, `after` becomes a new block below,
   * and the editor follows it. `caret` is the column to land on — past a
   * carried list marker, if any.
   */
  const splitBlockEdit = useCallback(
    (before: string, after: string, caret = 0) => {
      if (!editBlock) return
      const offset = fmOffset()
      const lines = contentRef.current.split('\n')
      const count = Math.max(0, editBlock.end - editBlock.start + 1)
      const beforeLines = before.split('\n')
      const afterLines = after.split('\n')
      lines.splice(editBlock.start + offset, count, ...beforeLines, ...afterLines)
      edit(lines.join('\n'))
      const start = editBlock.start + beforeLines.length
      setEditBlock({ start, end: start + afterLines.length - 1, caret })
    },
    [editBlock, edit, fmOffset],
  )

  const toggleCheckbox = useCallback(
    (bodyLine: number) => {
      const offset = fmOffset()
      const lines = contentRef.current.split('\n')
      const i = bodyLine + offset
      const line = lines[i]
      if (!line) return
      lines[i] = line.includes('- [ ] ')
        ? line.replace('- [ ] ', '- [x] ')
        : line.replace('- [x] ', '- [ ] ')
      edit(lines.join('\n'))
    },
    [edit, fmOffset],
  )

  // ---- vault + note actions --------------------------------------------
  const switchVault = useCallback(
    async (id: string) => {
      setVaultSelOpen(false)
      if (id === vaultRef.current) return
      if (dirtyRef.current) await flushSave()
      // Still dirty means the write failed — typically a stale-write conflict
      // waiting on the user to pick a version. Switching clears the editor, so
      // the unsaved content would be gone with no way back to it.
      if (dirtyRef.current) return
      vaultRef.current = id
      setActiveVaultId(id)
      setActivePath(null)
      pathRef.current = null
      setContent('')
      contentRef.current = ''
      setNotes([])
      restored.current = false
    },
    [flushSave],
  )

  const newNote = useCallback(async () => {
    try {
      const { path } = await notesApi.newNote(vaultRef.current)
      await loadNotes()
      await openNote(path)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [loadNotes, openNote])

  const relocate = useCallback(
    async (from: string, to: string) => {
      if (!to || to === from) return
      try {
        // Flush edits at the OLD path first, then retarget — otherwise a stray
        // autosave would recreate the file we just moved away from.
        if (pathRef.current === from && dirtyRef.current) await flushSave()
        // A failed flush leaves the editor dirty against the OLD path. Renaming
        // now would retarget it to `to` without ever reconciling that content,
        // so a later save could overwrite the moved file from a stale base.
        if (pathRef.current === from && dirtyRef.current) return
        await notesApi.moveNote(vaultRef.current, from, to)
        if (pathRef.current === from) {
          pathRef.current = to
          setActivePath(to)
          savePref(LS.openNote, to)
        }
        await loadNotes()
        if (pathRef.current === to) await openNote(to)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [flushSave, loadNotes, openNote],
  )

  /** File a note into `folder` ('' = vault root), keeping its filename. */
  const moveNote = useCallback(
    (from: string, folder: string) => {
      const name = from.split('/').pop()
      void relocate(from, folder ? `${folder}/${name}` : String(name))
    },
    [relocate],
  )

  /** Rename in place from the inline title, keeping the folder. */
  const renameNote = useCallback(
    (from: string, nextName: string) => {
      // Strip separators and characters illegal in filenames, so a title edit
      // can never move the note or produce an unopenable name.
      const clean = String(nextName)
        .replace(/[\\/:*?"<>|]/g, '')
        .trim()
        .slice(0, 120)
      if (!clean) return
      const dir = from.includes('/') ? from.slice(0, from.lastIndexOf('/')) : ''
      void relocate(from, `${dir ? `${dir}/` : ''}${clean}.md`)
    },
    [relocate],
  )

  const forgetVault = useCallback(
    async (id: string) => {
      // Forgetting the ACTIVE vault clears the editor below, so persist first —
      // and stop if the save could not land, exactly as switching does.
      if (id === vaultRef.current && dirtyRef.current) {
        await flushSave()
        if (dirtyRef.current) return
      }
      try {
        await notesApi.forgetVault(id)
        if (id === vaultRef.current) {
          pathRef.current = null
          setActivePath(null)
          setContent('')
          contentRef.current = ''
          setNotes([])
          restored.current = false
        }
        await loadVaults()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [loadVaults, flushSave],
  )

  const savePat = useCallback(
    async (value: string) => {
      const r = await notesApi.setPat(value)
      setAuth({ hasPat: r.hasPat, hasGhAuth: r.hasGhAuth })
    },
    [],
  )

  /** Persist a vault's knowledge preference and refresh the vault list. */
  const setVaultKnowledge = useCallback(
    async (id: string, on: boolean, sourceId?: string) => {
      await notesApi.setVaultKnowledge(id, on, sourceId)
      await loadVaults()
    },
    [loadVaults],
  )

  const runSync = useCallback(async () => {
    if (!vaultRef.current) return
    // A block editor holds its draft locally and only commits it to `content`
    // on blur/commit. Syncing now would flushSave the stale `contentRef`, then
    // reload and unmount the editor — losing the typed text. Refuse to sync
    // while a block is being edited; the user can sync after committing it.
    if (editBlock) return
    setSyncing(true)
    setError(null)
    try {
      if (dirtyRef.current) await flushSave()
      // Syncing after a failed save would commit and push the version on disk
      // while the user's unsaved edit sits unreconciled in the editor — backing
      // up content they did not choose. `finally` still clears the spinner.
      if (dirtyRef.current) return
      const { result } = await notesApi.sync(vaultRef.current)
      // Only a conflict-free run counts as synced — with conflicts nothing was
      // pushed, so reporting success would mislead.
      if (!result.conflicts.length && vaultRef.current) {
        const now = Date.now()
        setLastSync(now)
        savePref(`mdnb-last-sync-${vaultRef.current}`, now)
      } else if (result.conflicts.length) {
        setError(
          i18nT('apps.mdNotebook.banner.syncConflict', {
            paths: result.conflicts.map(c => c.path).join(', '),
          }),
        )
      }
      await loadNotes()
      if (pathRef.current) await openNote(pathRef.current)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSyncing(false)
    }
  }, [editBlock, flushSave, loadNotes, openNote])

  // Manual-sync shortcut, capture phase so Cmd+S never reaches the browser.
  const runSyncRef = useRef(runSync)
  useEffect(() => {
    runSyncRef.current = runSync
  }, [runSync])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (recordingShortcutRef.current) return
      if (!matchesShortcut(e, syncShortcut)) return
      e.preventDefault()
      e.stopPropagation()
      void runSyncRef.current()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [syncShortcut])

  // Auto sync on a timer: the same bidirectional operation as the button.
  useEffect(() => {
    if (!autoSync || !activeVaultId) return
    const id = window.setInterval(() => void runSyncRef.current(), autoSyncMins * 60_000)
    return () => window.clearInterval(id)
  }, [autoSync, autoSyncMins, activeVaultId])

  // External-change poll: refresh the listing, and the open note if it moved.
  useEffect(() => {
    if (!activeVaultId) return
    const id = window.setInterval(async () => {
      try {
        const r = await notesApi.changes(vaultRef.current, revRef.current)
        if (r.rev === revRef.current) return
        revRef.current = r.rev
        await loadNotes()
        if (pathRef.current && r.changed.includes(pathRef.current) && !dirtyRef.current) {
          await openNote(pathRef.current)
        }
      } catch {
        /* transient: the next tick tries again */
      }
    }, CHANGES_POLL_MS)
    return () => window.clearInterval(id)
  }, [activeVaultId, loadNotes, openNote])

  // Drop the previous vault's search hits the instant the vault changes, so a
  // stale result can't be clicked during the new vault's 150ms search debounce
  // and open that path in the wrong vault.
  useEffect(() => {
    setResults([])
  }, [activeVaultId])

  // Search, debounced.
  useEffect(() => {
    if (!query.trim()) {
      setResults([])
      return
    }
    // Capture the vault this search belongs to. Keying the effect on
    // `activeVaultId` re-runs it on a vault switch (cancelling the old query),
    // and the `vaultRef` guard drops a late resolve so vault A's results can
    // never populate the list while vault B is active — otherwise selecting a
    // result would read that path from the wrong vault.
    const vault = activeVaultId
    let cancelled = false
    const t = window.setTimeout(() => {
      notesApi
        .search(vault, query)
        .then(r => {
          if (!cancelled && vaultRef.current === vault) setResults(r.results)
        })
        .catch(() => undefined)
    }, 150)
    return () => {
      cancelled = true
      window.clearTimeout(t)
    }
  }, [query, activeVaultId])

  const tree = useMemo(() => buildTree(notes), [notes])
  const toggle = useCallback(
    (name: string) =>
      setCollapsed(prev => {
        const next = new Set(prev)
        if (next.has(name)) next.delete(name)
        else next.add(name)
        return next
      }),
    [],
  )

  const togglePanel = useCallback(() => {
    setPanelOpen(v => {
      savePref(LS.panelOpen, !v)
      return !v
    })
  }, [])

  const startResize = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault()
      const startX = e.clientX
      const startW = panelW
      const clamp = (x: number) =>
        Math.min(PANEL_MAX_WIDTH, Math.max(PANEL_MIN_WIDTH, startW + (x - startX)))
      const onMove = (ev: PointerEvent) => setPanelW(clamp(ev.clientX))
      const onUp = (ev: PointerEvent) => {
        savePref(LS.panelWidth, clamp(ev.clientX))
        window.removeEventListener('pointermove', onMove)
        window.removeEventListener('pointerup', onUp)
      }
      window.addEventListener('pointermove', onMove)
      window.addEventListener('pointerup', onUp)
    },
    [panelW],
  )

  const switchMode = useCallback((m: 'rendered' | 'raw') => {
    setMode(m)
    setEditBlock(null)
    savePref(LS.view, m)
  }, [])

  // ---- render -----------------------------------------------------------

  if (vaults === null) {
    return (
      <div
        style={{
          padding: '24px',
          fontSize: '12px',
          fontFamily: FONT_BODY,
          color: error ? 'var(--danger)' : 'var(--muted)',
        }}
      >
        {error ? i18nT('apps.mdNotebook.boot.unreachable', { message: error }) : i18nT('apps.mdNotebook.boot.loading')}
      </div>
    )
  }

  if (!vaults.length || showConnect) {
    return (
      <ConnectVault
        onCancel={vaults.length ? () => setShowConnect(false) : null}
        onConnected={v => {
          setVaults(prev => [...(prev ?? []), v])
          setShowConnect(false)
          void switchVault(v.id)
        }}
      />
    )
  }

  const activeVault = vaults.find(v => v.id === activeVaultId) ?? vaults[0]
  const ago = lastSync ? agoBucket(lastSync) : null
  const syncLabel = syncing
    ? i18nT('apps.mdNotebook.sync.syncing')
    : ago === null
      ? i18nT('apps.mdNotebook.sync.action')
      : syncedAgoLabel(ago.unit, ago.value)

  return (
    <div
      style={{
        display: 'flex',
        height: '100%',
        minHeight: '520px',
        position: 'relative',
        fontFamily: FONT_BODY,
        color: 'var(--text)',
        background: 'var(--bg)',
      }}
    >
      <style>{MDNB_CSS}</style>

      {/* Floating panel toggle: stays put when the panel hides. */}
      <button
        type="button"
        className="mdnb-collapse"
        onClick={togglePanel}
        aria-label={
          panelOpen
            ? i18nT('apps.mdNotebook.panel.hide')
            : i18nT('apps.mdNotebook.panel.show')
        }
        style={{
          position: 'absolute',
          top: '20px',
          left: '8px',
          zIndex: 10,
          width: '34px',
          height: '34px',
          borderRadius: '16px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          background: 'transparent',
          border: 'none',
          transition: 'color .15s',
        }}
      >
        {panelOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
      </button>

      {panelOpen && (
        <div
          style={{
            width: `${panelW}px`,
            flexShrink: 0,
            position: 'relative',
            display: 'flex',
            flexDirection: 'column',
            margin: '8px 0',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: '16px',
            boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
          }}
        >
          {/* Header: the vault selector doubles as the panel title. */}
          <div
            style={{
              height: '40px',
              marginTop: '8px',
              display: 'flex',
              alignItems: 'center',
              padding: '0 14px 0 8px',
              flexShrink: 0,
              position: 'relative',
            }}
          >
            <button
              type="button"
              className="mdnb-vault-trigger"
              onClick={() => setVaultSelOpen(o => !o)}
              aria-expanded={vaultSelOpen}
              aria-haspopup="listbox"
              aria-label={i18nT('apps.mdNotebook.panel.switchVault')}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
                marginLeft: '32px',
                minWidth: 0,
                background: 'transparent',
                border: 'none',
                padding: '4px 6px',
                borderRadius: '8px',
                cursor: 'pointer',
                fontFamily: FONT_BODY,
              }}
            >
              <span
                style={{
                  fontSize: '14px',
                  fontWeight: 500,
                  color: 'var(--muted)',
                  letterSpacing: '.04em',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {activeVault?.name ?? i18nT('apps.mdNotebook.title')}
              </span>
              <ChevronDown
                size={13}
                style={{
                  flexShrink: 0,
                  color: 'var(--muted)',
                  transform: vaultSelOpen ? 'rotate(180deg)' : 'none',
                  transition: 'transform .15s',
                }}
              />
            </button>
            {vaultSelOpen && (
              <div
                role="listbox"
                aria-label={i18nT('apps.mdNotebook.panel.vaults')}
                style={{
                  position: 'absolute',
                  top: '38px',
                  left: '38px',
                  minWidth: '180px',
                  maxWidth: 'calc(100% - 46px)',
                  maxHeight: '112px',
                  overflowY: 'auto',
                  background: 'var(--bg-elevated)',
                  border: '1px solid var(--border)',
                  borderRadius: '8px',
                  boxShadow: '0 4px 14px rgba(0,0,0,0.25)',
                  padding: '4px',
                  zIndex: 20,
                }}
              >
                {vaults.map(v => (
                  <div
                    key={v.id}
                    className="mdnb-row"
                    role="option"
                    aria-selected={v.id === activeVaultId}
                    tabIndex={0}
                    onKeyDown={e => {
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      e.preventDefault()
                      void switchVault(v.id)
                    }}
                    onClick={() => void switchVault(v.id)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '8px',
                      padding: '5px 8px',
                      borderRadius: '6px',
                      cursor: 'pointer',
                      fontSize: '13px',
                      color: v.id === activeVaultId ? 'var(--text)' : 'var(--muted)',
                    }}
                  >
                    <span
                      style={{
                        flex: 1,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {v.name}
                    </span>
                    {v.id === activeVaultId && (
                      <Check size={14} style={{ flexShrink: 0, color: ACCENT }} />
                    )}
                  </div>
                ))}
                <div style={{ height: '1px', background: 'var(--border)', margin: '4px 0' }} />
                <Clickable
                  className="mdnb-row"
                  onClick={() => {
                    setVaultSelOpen(false)
                    setShowConnect(true)
                  }}
                  style={{
                    padding: '5px 8px',
                    borderRadius: '6px',
                    cursor: 'pointer',
                    fontSize: '13px',
                    color: 'var(--muted)',
                  }}
                >
                  {i18nT('apps.mdNotebook.panel.connectVault')}
                </Clickable>
              </div>
            )}
          </div>

          {/* Search + sort */}
          <div
            style={{
              display: 'flex',
              gap: '6px',
              alignItems: 'center',
              padding: '0 12px 8px',
              flexShrink: 0,
            }}
          >
            <input
              className="mdnb-search"
              aria-label={i18nT('apps.mdNotebook.panel.searchPlaceholder')}
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={i18nT('apps.mdNotebook.panel.searchPlaceholder')}
              style={{
                flex: 1,
                height: '30px',
                minWidth: 0,
                boxSizing: 'border-box',
                background: 'var(--card)',
                border: '1px solid var(--border)',
                borderRadius: '8px',
                padding: '0 10px',
                fontSize: '12px',
                color: 'var(--text)',
                fontFamily: FONT_BODY,
              }}
            />
            <div style={{ position: 'relative', flexShrink: 0 }}>
              <button
                type="button"
                onClick={() => setSortOpen(o => !o)}
                aria-label={i18nT('apps.mdNotebook.panel.sortAndView')}
                style={{
                  ...iconBtn,
                  width: '30px',
                  height: '30px',
                  border: '1px solid var(--border)',
                }}
              >
                <ListFilter size={14} />
              </button>
              {sortOpen && (
                <div
                  style={{
                    position: 'absolute',
                    top: '34px',
                    right: 0,
                    minWidth: '200px',
                    background: 'var(--bg-elevated)',
                    border: '1px solid var(--border)',
                    borderRadius: '8px',
                    boxShadow: '0 4px 14px rgba(0,0,0,0.25)',
                    padding: '4px',
                    zIndex: 20,
                  }}
                >
                  <div
                    style={{
                      fontSize: '10px',
                      textTransform: 'uppercase',
                      letterSpacing: '.04em',
                      color: 'var(--muted)',
                      padding: '6px 8px 4px',
                    }}
                  >
                    {i18nT('apps.mdNotebook.panel.viewSection')}
                  </div>
                  {(['folders', 'list'] as const).map(v => (
                    <Clickable
                      key={v}
                      className="mdnb-row"
                      aria-label={listViewLabel(v)}
                      onClick={() => {
                        setView(v)
                        savePref('mdnb-list-view', v)
                        setSortOpen(false)
                      }}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '8px',
                        padding: '5px 8px',
                        borderRadius: '6px',
                        cursor: 'pointer',
                        fontSize: '12px',
                        color: view === v ? 'var(--text)' : 'var(--muted)',
                      }}
                    >
                      <span style={{ flex: 1 }}>
                        {listViewLabel(v)}
                      </span>
                      {view === v && <Check size={14} style={{ color: ACCENT }} />}
                    </Clickable>
                  ))}
                  <div style={{ height: '1px', background: 'var(--border)', margin: '4px 0' }} />
                  <div
                    style={{
                      fontSize: '10px',
                      textTransform: 'uppercase',
                      letterSpacing: '.04em',
                      color: 'var(--muted)',
                      padding: '6px 8px 4px',
                    }}
                  >
                    {i18nT('apps.mdNotebook.panel.sortSection')}
                  </div>
                  {Object.keys(SORTS).map((key) => (
                    <Clickable
                      key={key}
                      className="mdnb-row"
                      aria-label={sortLabel(key)}
                      onClick={() => {
                        setSortKey(key)
                        savePref(LS.sort, key)
                        setSortOpen(false)
                      }}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '8px',
                        padding: '5px 8px',
                        borderRadius: '6px',
                        cursor: 'pointer',
                        fontSize: '12px',
                        color: sortKey === key ? 'var(--text)' : 'var(--muted)',
                      }}
                    >
                      <span style={{ flex: 1, whiteSpace: 'nowrap' }}>
                        {sortLabel(key)}
                      </span>
                      {sortKey === key && <Check size={14} style={{ color: ACCENT }} />}
                    </Clickable>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Note list. Dropping on the background files a note at the root. */}
          <div
            style={{ flex: 1, overflowY: 'auto', padding: '8px' }}
            onDragOver={e => {
              e.preventDefault()
              e.dataTransfer.dropEffect = 'move'
            }}
            onDrop={e => {
              e.preventDefault()
              const from = e.dataTransfer.getData('text/plain')
              if (from) moveNote(from, '')
            }}
          >
            {query.trim()
              ? results.length
                ? results.map(r => (
                    <NoteRow
                      key={r.path}
                      note={{
                        path: r.path,
                        title: r.title,
                        modifiedAt:
                          notes.find(n => n.path === r.path)?.modifiedAt ?? Date.now(),
                        syncStatus: 'synced',
                      }}
                      active={!settingsOpen && r.path === activePath}
                      onOpen={p => void openNote(p)}
                    />
                  ))
                : (
                    <div style={{ padding: '10px', fontSize: '11px', color: 'var(--muted)' }}>
                      {i18nT('apps.mdNotebook.panel.noMatches')}
                    </div>
                  )
              : view === 'list'
                ? [...notes]
                    .sort(SORTS[sortKey].cmp)
                    .map(n => (
                      <NoteRow
                        key={n.path}
                        note={n}
                        active={!settingsOpen && n.path === activePath}
                        onOpen={p => void openNote(p)}
                        showFolder
                      />
                    ))
                : renderTree(tree, 0, '', {
                    activePath: settingsOpen ? null : activePath,
                    onOpen: p => void openNote(p),
                    collapsed,
                    toggle,
                    cmp: SORTS[sortKey].cmp,
                    onMove: moveNote,
                  })}
          </div>

          <SettingsBar open={settingsOpen} onOpen={() => setSettingsOpen(true)} />

          {/* Drag handle */}
          <div
            onPointerDown={startResize}
            style={{
              position: 'absolute',
              top: 0,
              bottom: 0,
              right: '-3px',
              width: '5px',
              cursor: 'col-resize',
            }}
          />
        </div>
      )}

      {/* ---- main column: Settings page, or the open note ---- */}
      {settingsOpen ? (
        <SettingsPage
          vaults={vaults}
          activeVaultId={activeVaultId}
          hasPat={auth.hasPat}
          hasGhAuth={auth.hasGhAuth}
          autoSync={autoSync}
          autoSyncMins={autoSyncMins}
          shortcut={syncShortcut}
          onClose={() => setSettingsOpen(false)}
          onSwitchVault={id => {
            void switchVault(id)
            setSettingsOpen(false)
          }}
          onConnect={() => {
            setSettingsOpen(false)
            setShowConnect(true)
          }}
          onForget={forgetVault}
          onSetPat={savePat}
          onSetKnowledge={setVaultKnowledge}
          onAutoSync={setAutoSyncPref}
          onAutoSyncMins={setAutoSyncMinsPref}
          onSetShortcut={setSyncShortcutPref}
          onRecordingChange={on => {
            recordingShortcutRef.current = on
          }}
        />
      ) : (
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div style={{ position: 'relative' }}>
          <div
            style={{
              position: 'absolute',
              top: '24px',
              right: `${COLUMN_PAD_X}px`,
              zIndex: 2,
              display: 'flex',
              gap: '4px',
              alignItems: 'center',
            }}
          >
            <button
              type="button"
              onClick={() => void newNote()}
              aria-label={i18nT('apps.mdNotebook.header.newNote')}
              title={i18nT('apps.mdNotebook.header.newNote')}
              style={iconBtn}
            >
              <Plus size={16} />
            </button>
            {/* Rendered / raw switch */}
            <div
              style={{
                display: 'flex',
                gap: '4px',
                padding: '4px',
                background: 'var(--card)',
                borderRadius: '8px',
              }}
            >
              {(
                [
                  ['rendered', FileText] as const,
                  ['raw', Code] as const,
                ]
              ).map(([m, Icon]) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => switchMode(m)}
                  aria-label={paneViewLabel(m)}
                  title={paneViewLabel(m)}
                  style={{
                    width: '26px',
                    height: '22px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    border: 'none',
                    borderRadius: '6px',
                    cursor: 'pointer',
                    background: mode === m ? ACCENT : 'transparent',
                    color: mode === m ? 'var(--accent-fg)' : 'var(--muted)',
                  }}
                >
                  <Icon size={16} />
                </button>
              ))}
            </div>
            <button
              type="button"
              onClick={() => void runSync()}
              disabled={syncing}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                height: '28px',
                padding: '0 12px',
                borderRadius: '12px',
                border: 'none',
                background: ACCENT,
                color: 'var(--accent-fg)',
                fontSize: '11px',
                fontWeight: 500,
                cursor: syncing ? 'default' : 'pointer',
                fontFamily: FONT_BODY,
              }}
            >
              <RefreshCw size={13} />
              {syncLabel}
              {syncing && (
                <span aria-hidden>
                  {[0, 0.2, 0.4].map(delay => (
                    <motion.span
                      key={delay}
                      style={{ display: 'inline-block' }}
                      animate={{ opacity: [0.25, 1, 0.25] }}
                      transition={{
                        duration: 1.4,
                        times: [0, 0.4, 0.8],
                        repeat: Infinity,
                        ease: 'easeInOut',
                        delay,
                      }}
                    >
                      .
                    </motion.span>
                  ))}
                </span>
              )}
            </button>
          </div>

          <div
            style={{
              margin: '0 auto',
              width: '100%',
              maxWidth: `${COLUMN_MAX_WIDTH}px`,
              boxSizing: 'border-box',
              padding: `24px ${COLUMN_PAD_X}px 14px`,
            }}
          >
            {activePath ? (
              <InlineTitle
                path={activePath}
                onRename={n => renameNote(activePath, n)}
                mb="0"
              />
            ) : (
              <div style={{ fontSize: '23px', fontWeight: 700, color: 'var(--muted)' }}>
                {i18nT('apps.mdNotebook.title')}
              </div>
            )}
            {activePath && (
              <div
                title={`${activeVault?.name ?? ''}/${activePath}`}
                style={{ fontSize: '11px', color: 'var(--muted)', marginTop: '2px' }}
              >
                {`${activeVault?.name ?? ''}/${
                  activePath.includes('/')
                    ? `${activePath.slice(0, activePath.lastIndexOf('/'))}/`
                    : ''
                }`}
              </div>
            )}
          </div>
          <div style={{ borderTop: '1px solid var(--border)', margin: `0 ${COLUMN_PAD_X}px` }} />
        </div>

        {/* Banners */}
        {staleBackend && (
          <div
            role="alert"
            style={{
              margin: `8px ${COLUMN_PAD_X}px 0`,
              padding: '8px 10px',
              borderRadius: '8px',
              background: 'var(--warn-subtle)',
              color: 'var(--warn)',
              fontSize: '11px',
            }}
          >
            {i18nT('apps.mdNotebook.banner.staleBackend', {
              features: staleBackend.join(', '),
            })}
          </div>
        )}
        {fileConflict && (
          <div
            role="alert"
            style={{
              margin: `8px ${COLUMN_PAD_X}px 0`,
              padding: '8px 10px',
              borderRadius: '8px',
              background: 'var(--danger-subtle)',
              color: 'var(--danger)',
              fontSize: '11px',
              display: 'flex',
              gap: '10px',
              alignItems: 'center',
              flexWrap: 'wrap',
            }}
          >
            <span style={{ flex: 1, minWidth: '200px' }}>
              {i18nT('apps.mdNotebook.banner.diskConflict')}
            </span>
            <button
              type="button"
              onClick={() => {
                // Take what is on disk, discarding the in-memory edit.
                setContent(fileConflict.disk)
                contentRef.current = fileConflict.disk
                mtimeRef.current = fileConflict.mtime
                setDirty(false)
                dirtyRef.current = false
                setFileConflict(null)
              }}
              style={{
                background: 'transparent',
                color: 'inherit',
                border: '1px solid currentColor',
                padding: '3px 10px',
                borderRadius: '8px',
                fontSize: '11px',
                cursor: 'pointer',
              }}
            >
              {i18nT('apps.mdNotebook.banner.useDisk')}
            </button>
            <button
              type="button"
              onClick={() => {
                // Keep the editor's version: adopt the fresh mtime so the next
                // save is accepted, then save.
                mtimeRef.current = fileConflict.mtime
                setFileConflict(null)
                void flushSave()
              }}
              style={{
                background: 'transparent',
                color: 'inherit',
                border: '1px solid currentColor',
                padding: '3px 10px',
                borderRadius: '8px',
                fontSize: '11px',
                cursor: 'pointer',
              }}
            >
              {i18nT('apps.mdNotebook.banner.keepMine')}
            </button>
          </div>
        )}
        {error && (
          <div
            role="alert"
            style={{
              margin: `8px ${COLUMN_PAD_X}px 0`,
              padding: '8px 10px',
              borderRadius: '8px',
              background: 'var(--danger-subtle)',
              color: 'var(--danger)',
              fontSize: '11px',
            }}
          >
            {error}
          </div>
        )}

        {/* Body */}
        {!activePath ? (
          <div
            style={{
              flex: 1,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--muted)',
              fontSize: '12px',
            }}
          >
            {i18nT('apps.mdNotebook.body.empty')}
          </div>
        ) : mode === 'raw' ? (
          // Full-width textarea so its scrollbar sits at the pane's far right;
          // the text is centred by padding instead.
          <div style={{ flex: 1, position: 'relative', display: 'flex', minHeight: 0 }}>
            <textarea
              value={content}
              spellCheck={false}
              aria-label={i18nT('apps.mdNotebook.header.view_raw')}
              onChange={e => edit(e.target.value)}
              onKeyDown={e => {
                if (e.key !== 'Tab') return
                const ta = e.currentTarget
                const next = shiftListItem(content, ta.selectionStart, e.shiftKey)
                if (!next) return
                e.preventDefault()
                ta.value = next.text
                ta.setSelectionRange(next.pos, next.pos)
                edit(next.text)
              }}
              style={{
                flex: 1,
                width: '100%',
                minHeight: 0,
                boxSizing: 'border-box',
                resize: 'none',
                border: 'none',
                outline: 'none',
                background: 'var(--bg)',
                color: 'var(--text)',
                paddingTop: '14px',
                paddingBottom: '14px',
                paddingLeft: `max(${COLUMN_PAD_X}px, calc((100% - ${
                  COLUMN_MAX_WIDTH - COLUMN_PAD_X * 2
                }px) / 2))`,
                paddingRight: `max(${COLUMN_PAD_X}px, calc((100% - ${
                  COLUMN_MAX_WIDTH - COLUMN_PAD_X * 2
                }px) / 2))`,
                fontSize: '13px',
                lineHeight: 1.55,
                fontFamily: FONT_MONO,
              }}
            />
          </div>
        ) : (
          <div style={{ flex: 1, overflowY: 'auto', minHeight: 0 }}>
            <div
              style={{
                margin: '0 auto',
                width: '100%',
                maxWidth: `${COLUMN_MAX_WIDTH}px`,
                boxSizing: 'border-box',
                padding: `14px ${COLUMN_PAD_X}px 32px`,
              }}
            >
              <Preview
                content={content}
                onToggleCheckbox={toggleCheckbox}
                editRange={editBlock}
                onStartEdit={startBlockEdit}
                onCommitEdit={commitBlockEdit}
                onCancelEdit={cancelBlockEdit}
                onSplitEdit={splitBlockEdit}
                onDirtyEdit={markDirty}
              />
              {backlinks.length > 0 && (
                <div style={{ marginTop: '24px', borderTop: '1px solid var(--border)', paddingTop: '12px' }}>
                  <div
                    style={{
                      fontSize: '10px',
                      textTransform: 'uppercase',
                      letterSpacing: '.04em',
                      color: 'var(--muted)',
                      marginBottom: '6px',
                    }}
                  >
                    {i18nT('apps.mdNotebook.body.backlinks', { n: backlinks.length })}
                  </div>
                  {backlinks.map(b => (
                    <Clickable
                      key={`${b.sourcePath}-${b.line}`}
                      className="mdnb-row"
                      aria-label={b.sourcePath}
                      onClick={() => void openNote(b.sourcePath)}
                      style={{
                        padding: '6px 8px',
                        borderRadius: '6px',
                        cursor: 'pointer',
                        fontSize: '11px',
                      }}
                    >
                      <span style={{ color: ACCENT }}>{b.sourcePath}</span>
                      <span style={{ color: 'var(--muted)' }}>{` — ${b.context}`}</span>
                    </Clickable>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      )}
    </div>
  )
}
