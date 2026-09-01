/**
 * Notes — a markdown notebook backed by a git repository.
 *
 * Single-note view, no tabs: the left panel is the only navigation surface.
 * Search swaps the tree for flat ranked results; clearing restores the tree.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useImeGuard } from '../../hooks/useImeGuard'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { motion } from 'framer-motion'
import type { CSSProperties } from 'react'
import {
  Check,
  ChevronDown,
  Code,
  FileText,
  ListFilter,
  Plus,
  RefreshCw,
} from 'lucide-react'
// Same collapse glyphs the sessions sidebar uses (ChatPage), so the Notes
// panel's hide/show control reads as the identical affordance.
import { PanelLeftLight, PanelLeftSolid } from '../../components/icons/panels'
import { Trans } from 'react-i18next'
import { i18nT } from '../../i18n/t'
import {
  ACCENT,
  AUTO_COMMIT_MINS,
  CHANGES_POLL_MS,
  COLUMN_MAX_WIDTH,
  COLUMN_PAD_X,
  DEFAULT_AUTO_COMMIT,
  DEFAULT_AUTO_SYNC_MINS,
  DEFAULT_SORT,
  DEFAULT_SYNC_SHORTCUT,
  DOC_BODY_LINE_HEIGHT,
  DOC_BODY_PX,
  DOC_H1_PX,
  DOC_HEADING_WEIGHTS,
  FONT_BODY,
  FONT_MONO,
  HEADER_CONTROLS_GAP,
  LEGACY_LS,
  LS,
  MENU_SECTION_LABEL,
  PANEL_DEFAULT_WIDTH,
  PANEL_MAX_WIDTH,
  PANEL_MIN_WIDTH,
  RAIL_TYPE,
  SAVE_DEBOUNCE_MS,
  SORTS,
  TITLE_MIN_WIDTH,
  collapsedKey,
  pinnedKey,
} from './constants'
import { MDNB_CSS } from './styles'
import { listViewLabel, paneViewLabel, sortLabel, syncedAgoLabel } from './labels'
import { notesApi } from './api'
import { InlineTitle } from './BlockEditor'
import { Preview } from './Preview'
import { ConnectVault } from './ConnectVault'
import { ConfirmDialog } from './ConfirmDialog'
import { InlineLink } from './bits'
import { SettingsBar, SettingsPage } from './SettingsPage'
import Clickable from '../../components/Clickable'
import ReadingWidthToggle from '../../components/ReadingWidthToggle'
import { NoteRow, flattenVisibleNotes, orderNotes, renderTree } from './NoteRow'
import {
  FM_RE,
  agoBucket,
  buildTree,
  clampAutoSyncMins,
  clearPref,
  loadPref,
  matchesShortcut,
  neighborAfterDelete,
  noteDirPath,
  savePref,
  shiftListItem,
  targetsSameNote,
} from './utils'
import type { Backlink, EditRange, Note, NoteActions, SearchHit, Shortcut, Vault } from './types'

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
  'duplicate',
  'localOnly',
  'autoCommit',
  // The delete flow depends on both: the note is MOVED to `.trash` rather than
  // unlinked, and the confirmation's inline link reveals that folder. Without
  // them the row offers no delete button at all (see `canTrash`).
  'trash',
  'trashOpen',
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

/**
 * Ref callback reporting an element's box on attach and on every resize.
 *
 * Used where a layout decision is arithmetic on real widths rather than a CSS
 * expression, so it needs the numbers: `onMeasure` must be stable.
 */
function useMeasuredBox(onMeasure: (el: HTMLElement) => void) {
  const observer = useRef<ResizeObserver | null>(null)
  return useCallback(
    (el: HTMLElement | null) => {
      observer.current?.disconnect()
      observer.current = null
      if (!el) return
      const measure = () => onMeasure(el)
      measure()
      observer.current = new ResizeObserver(measure)
      observer.current.observe(el)
    },
    [onMeasure],
  )
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
  // Seeded from storage for the vault that will be restored, so returning to the
  // app keeps the tree the user shaped. An empty set means every folder open, so
  // initialising blind is what re-expanded the whole tree on every mount.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    const vault = loadPref<string | null>(LS.activeVault, null)
    return new Set(vault ? loadPref<string[]>(collapsedKey(vault), []) : [])
  })
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editBlock, setEditBlock] = useState<EditRange | null>(null)
  const [showConnect, setShowConnect] = useState(false)
  const [vaultSelOpen, setVaultSelOpen] = useState(false)
  const [sortOpen, setSortOpen] = useState(false)
  const [fileConflict, setFileConflict] = useState<{ mtime: number; disk: string } | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [auth, setAuth] = useState({ hasPat: false, hasGhAuth: false })
  // Pinned note paths for the ACTIVE vault, and the row (if any) being renamed
  // inline from its hover action bar.
  const [pinned, setPinned] = useState<Set<string>>(new Set())
  const [renamingPath, setRenamingPath] = useState<string | null>(null)
  // The note awaiting delete confirmation, and the one whose DELETE is in flight.
  const [confirmDelete, setConfirmDelete] = useState<{ path: string; title: string } | null>(null)
  // Identified by VAULT + PATH, never a bare path. A path here is vault-RELATIVE,
  // so a note at the same path in another vault would match a bare one: while a
  // delete was in flight in vault A, vault B's `One.md` would render dimmed with
  // a `deleting` badge and refuse to open, and the edit guard below would swallow
  // its keystrokes — in a vault this delete never touched.
  const [deleting, setDeleting] = useState<{ vault: string | null; path: string } | null>(null)
  // Mirror of `deleting` for the edit guard below. React state is async, so
  // a keystroke landing in the same tick as the confirmation would still see the
  // old value; a ref is what the guard can trust.
  const deletingRef = useRef<{ vault: string | null; path: string } | null>(null)

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
  const isMobile = useIsMobile()
  // Composition state for the raw-markdown textarea, whose Tab re-indents a list
  // item by rewriting the whole value — see the keydown handler below.
  const ime = useImeGuard()
  // The panel is a fixed 260px `flexShrink: 0` column, so at 390px it left the
  // editor 130px. `panelOpen` already exists but is a stored DESKTOP preference,
  // so it arrives open on a phone. While narrow the panel is a drawer that starts
  // closed and owns the pane when opened -- derived, never written back, or a
  // phone visit would silently change what the desktop shows.
  const [narrowPanelOpen, setNarrowPanelOpen] = useState(false)
  // With no note open the pane has nothing to show but the list, and the empty
  // state's copy points at the + button INSIDE the drawer -- so a closed drawer
  // there instructs an action whose control is off-screen. Forced open until a
  // note is picked, which is also when `openNote` closes it again.
  const panelShown = isMobile ? (narrowPanelOpen || !activePath) : panelOpen
  const [panelW, setPanelW] = useState(() => {
    const w = loadPref<number>(LS.panelWidth, PANEL_DEFAULT_WIDTH)
    return w >= PANEL_MIN_WIDTH && w <= PANEL_MAX_WIDTH ? w : PANEL_DEFAULT_WIDTH
  })
  const [activeVaultId, setActiveVaultId] = useState<string | null>(() =>
    loadPref<string | null>(LS.activeVault, null),
  )
  // Server-owned, so they start at the defaults and are replaced by the seed
  // effect below once `GET /settings` answers. Deliberately NOT read from
  // localStorage: the backend runs its own sync loop against these values, and a
  // per-browser copy would disagree with what it is actually doing.
  const [autoSync, setAutoSync] = useState(false)
  const [autoSyncMins, setAutoSyncMins] = useState(DEFAULT_AUTO_SYNC_MINS)
  // A failed settings WRITE, surfaced next to the controls in Settings. The
  // editor's own `error` banner does not render while Settings is open, so
  // reusing it would drop the report of a click that did not take effect.
  const [settingsWriteError, setSettingsWriteError] = useState<string | null>(null)
  const [autoCommit, setAutoCommit] = useState(() =>
    loadPref<boolean>(LS.autoCommit, DEFAULT_AUTO_COMMIT),
  )
  const [syncShortcut, setSyncShortcut] = useState<Shortcut>(() =>
    loadPref<Shortcut>(LS.syncShortcut, DEFAULT_SYNC_SHORTCUT),
  )
  // Lifts the reading-column cap on the note body. Per device, not per vault or
  // per note: it answers "how wide is this display", which is a property of the
  // screen the user is reading on.
  const [fullWidth, setFullWidth] = useState(() => loadPref<boolean>(LS.fullWidth, false))
  // True while Settings is capturing a shortcut, so the keys being recorded do
  // not also fire a sync.
  const recordingShortcutRef = useRef(false)

  /**
   * Persist part of the sync settings, reporting a rejection instead of dropping
   * it. Returns whether the server took the value, so a caller can roll its
   * optimistic local state back on failure. On success it invalidates the
   * settings query so the cache refetches the server's authoritative state.
   */
  const qc = useQueryClient()
  // Live mirrors of the two editable settings so a write can snapshot the FULL
  // desired state at the moment it is sent, not the value captured when a
  // debounce timer was armed — otherwise a delayed autoSyncMins write would
  // carry a stale autoSync and could revert a toggle the user made in between.
  const autoSyncRef = useRef(autoSync)
  const autoSyncMinsRef = useRef(autoSyncMins)
  useEffect(() => {
    autoSyncRef.current = autoSync
  }, [autoSync])
  useEffect(() => {
    autoSyncMinsRef.current = autoSyncMins
  }, [autoSyncMins])
  // Single-flight serialization: each write chains onto the previous so only one
  // PUT is ever in flight from this tab and they reach the server in the order
  // the user made the gestures. That, plus sending the full desired state, is
  // what stops a single tab racing itself; ordering ACROSS tabs is the server's
  // job (it applies writes in arrival order under its settings lock). The client
  // supplies no ordering token — a per-tab counter cannot form a global order.
  const writeChainRef = useRef<Promise<unknown>>(Promise.resolve())
  // How many settings writes this tab has in flight or debounced. The settings
  // poll reconciles the editable controls to server truth only while this is 0
  // (see the seed effect), so a refetch never yanks a value the user is mid-way
  // through changing.
  const writePendingRef = useRef(0)
  // The server's authoritative interval (updated on every settings response), so
  // a rejected interval write rolls the field back to server truth.
  const serverMinsRef = useRef(autoSyncMins)
  const putSyncSettings = useCallback(
    async (patch: { autoSync?: boolean; autoSyncMins?: number }): Promise<boolean> => {
      setSettingsWriteError(null)
      // Snapshot the FULL desired state from the live refs — a winning write must
      // carry both fields so it never drops the one it did not change.
      const bodyToSend = {
        autoSync: patch.autoSync ?? autoSyncRef.current,
        autoSyncMins: patch.autoSyncMins ?? autoSyncMinsRef.current,
      }
      writePendingRef.current += 1
      const run = writeChainRef.current.then(() => notesApi.saveSettings(bodyToSend))
      // Keep the chain alive on failure so a later write still runs.
      writeChainRef.current = run.then(
        () => undefined,
        () => undefined,
      )
      try {
        await run
        // Converge the fresh-forever settings cache to the server's ACTUAL state
        // by refetching, so every later read agrees with what the backend stored.
        void qc.invalidateQueries({ queryKey: ['md-notebook', 'settings'] })
        return true
      } catch (e) {
        setSettingsWriteError(
          i18nT('apps.mdNotebook.settings.prefsSaveFailed', {
            message: e instanceof Error ? e.message : String(e),
          }),
        )
        return false
      } finally {
        writePendingRef.current -= 1
      }
    },
    [qc],
  )

  const setAutoSyncPref = useCallback(
    (on: boolean) => {
      // Roll the toggle back if the write is rejected: otherwise a failed enable
      // leaves the control (and the foreground timer it gates) showing ON while
      // the server kept it OFF.
      const prev = autoSync
      setAutoSync(on)
      // A switch produces one value per gesture, so it writes straight through.
      void putSyncSettings({ autoSync: on }).then(ok => {
        if (!ok) setAutoSync(prev)
      })
    },
    [autoSync, putSyncSettings],
  )
  // The interval's number input fires on EVERY keystroke, so typing "45" would
  // PUT 4 and then 45 — and 4 is a real cadence the backend would start syncing
  // on. Debounced with the same timer-in-a-ref shape the note save uses, rather
  // than a new dependency.
  const minsTimer = useRef<number | null>(null)
  const pendingMins = useRef<number | null>(null)
  const setAutoSyncMinsPref = useCallback(
    (n: number) => {
      const v = clampAutoSyncMins(n)
      setAutoSyncMins(v)
      pendingMins.current = v
      if (minsTimer.current !== null) window.clearTimeout(minsTimer.current)
      minsTimer.current = window.setTimeout(() => {
        minsTimer.current = null
        pendingMins.current = null
        // Roll the field back to the server's value if the write is rejected —
        // otherwise a typed interval the server refused stays displayed as if it
        // had been saved (the toggle already rolls back the same way).
        void putSyncSettings({ autoSyncMins: v }).then(ok => {
          if (!ok) setAutoSyncMins(serverMinsRef.current)
        })
      }, SAVE_DEBOUNCE_MS)
    },
    [putSyncSettings],
  )
  // Leaving the app inside the debounce window would silently discard a value the
  // user watched land in the field, so a pending write is sent rather than
  // cancelled. Routed through the same write chain (after any in-flight write,
  // never concurrent with it) and carrying the FULL desired state, so it obeys
  // the same single-flight ordering as every other write. Fire-and-forget: there
  // is no section left to report a failure in.
  useEffect(
    () => () => {
      if (minsTimer.current === null) return
      window.clearTimeout(minsTimer.current)
      const v = pendingMins.current
      if (v === null) return
      const body = { autoSync: autoSyncRef.current, autoSyncMins: v }
      writeChainRef.current = writeChainRef.current
        .then(() => notesApi.saveSettings(body))
        .then(
          () => undefined,
          () => undefined,
        )
    },
    [],
  )
  /** Lift or restore the reading-column cap, remembered for this device. */
  const toggleFullWidth = useCallback(() => {
    setFullWidth(on => {
      savePref(LS.fullWidth, !on)
      return !on
    })
  }, [])

  // The rendered body caps its width directly; the raw textarea cannot cap its
  // own content box, so it centres its text with side padding instead (see the
  // override at that style block).
  const columnMaxWidth = fullWidth ? undefined : `${COLUMN_MAX_WIDTH}px`

  // The title band shares its row with the floating header controls, so a long
  // first line could run beneath them. Both boxes are measured rather than
  // guessed: the cluster's width is content-driven (the sync label varies by
  // locale and state) and the band's width is the pane's, which the user drags.
  const [headerControls, setHeaderControls] = useState({ width: 0, height: 0 })
  const [headerBandWidth, setHeaderBandWidth] = useState(0)
  const onMeasureControls = useCallback(
    (el: HTMLElement) => setHeaderControls({ width: el.offsetWidth, height: el.offsetHeight }),
    [],
  )
  const onMeasureBand = useCallback((el: HTMLElement) => setHeaderBandWidth(el.offsetWidth), [])
  const headerControlsRef = useMeasuredBox(onMeasureControls)
  const headerBandRef = useMeasuredBox(onMeasureBand)

  // Clearance is arithmetic on those two widths, not a CSS expression, so the
  // narrow case is readable in a unit test. `titleClearance` is the extra right
  // padding the title reserves beyond the regular pad; `stackTitle` says the
  // pane is too narrow to seat both on one row, so the title drops below the
  // cluster instead of being squeezed into a column of single letters (the
  // title wraps on any character, so a squeeze degrades far worse than a
  // stack).
  const { titleClearance, stackTitle } = useMemo(() => {
    if (!headerBandWidth || !headerControls.width) return { titleClearance: 0, stackTitle: false }
    const blockWidth = fullWidth ? headerBandWidth : Math.min(headerBandWidth, COLUMN_MAX_WIDTH)
    // Both edges in band coordinates: the block is centred, the cluster is
    // anchored to the band's right edge.
    const titleRight = (headerBandWidth + blockWidth) / 2 - COLUMN_PAD_X
    const clusterLeft = headerBandWidth - COLUMN_PAD_X - headerControls.width
    const clearance = Math.max(0, titleRight + HEADER_CONTROLS_GAP - clusterLeft)
    const titleWidth = blockWidth - COLUMN_PAD_X * 2 - clearance
    return titleWidth < TITLE_MIN_WIDTH
      ? { titleClearance: 0, stackTitle: true }
      : { titleClearance: clearance, stackTitle: false }
  }, [fullWidth, headerBandWidth, headerControls.width])

  const setSyncShortcutPref = useCallback((sc: Shortcut) => {
    setSyncShortcut(sc)
    savePref(LS.syncShortcut, sc)
  }, [])
  const setAutoCommitPref = useCallback((on: boolean) => {
    setAutoCommit(on)
    savePref(LS.autoCommit, on)
  }, [])
  // Feedback for the trash link in the delete dialog. Nothing is shown on
  // success — the file manager coming to the front IS the result.
  const [trashMsg, setTrashMsg] = useState<string | null>(null)
  const openTrash = useCallback(async () => {
    setTrashMsg(null)
    try {
      const r = await notesApi.openTrash(vaultRef.current)
      // No folder yet: nothing has ever been deleted from this vault. Say so
      // rather than creating an empty folder just to have something to open.
      if (r.empty) setTrashMsg(i18nT('apps.mdNotebook.trash.empty'))
    } catch (e) {
      const code = (e as { body?: { code?: string } }).body?.code
      setTrashMsg(
        code === 'folder_open_unsupported'
          ? i18nT('apps.mdNotebook.trash.unsupported')
          : i18nT('apps.mdNotebook.trash.failed', {
              message: e instanceof Error ? e.message : String(e),
            }),
      )
    }
  }, [])
  /**
   * Last conflict-free sync per vault id, as the server reports it.
   *
   * Keyed by vault rather than held as one value so switching vaults shows each
   * one's own time instead of the last one looked at, and sourced from the server
   * so a sync the BACKEND performed on its own timer still ages the label — a
   * page-written timestamp could only ever record syncs this tab ran itself.
   */
  const [lastSyncByVault, setLastSyncByVault] = useState<Record<string, number>>({})

  const saveTimer = useRef<number | null>(null)
  /**
   * Requests the unmount flush must wait for before it writes.
   *
   * Not just saves. The flush targets `pathRef`, so anything that RETARGETS
   * `pathRef` has to finish first: between a move's request and its reply the
   * open note's path names a file the server has already moved away, and a write
   * sent there is lost to a swallowed `ESTALE`. Entries are registered through
   * their retarget, not merely around the request, because it is the retarget --
   * not the response -- that makes the wait sufficient.
   */
  const writesInFlightRef = useRef(new Set<Promise<void>>())
  /**
   * The active disk save, shared by every caller that reaches the save barrier.
   *
   * A debounce and a rename/move can ask to flush the same dirty buffer at the
   * same time. Sending both requests lets one success clear `dirtyRef` after the
   * other request failed, so the mutation can move a note whose save was never
   * reconciled. Joining the active request makes its one outcome authoritative.
   */
  const saveInFlightRef = useRef<Promise<void> | null>(null)
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
  /**
   * True while the note that is OPEN RIGHT NOW has a DELETE in flight.
   *
   * Both halves of the identity are compared, because `deletingRef` holds a
   * vault-relative path: matching on path alone would report `true` for a
   * same-named note in a different vault and make the callers below drop that
   * note's edits.
   */
  const openNoteIsDeleting = useCallback(
    () => targetsSameNote(deletingRef.current, vaultRef.current, pathRef.current),
    [],
  )
  /**
   * Register a request the unmount flush must wait for; the returned function
   * releases it. Callers hold it across the retarget, not just the request.
   */
  const trackWrite = useCallback(() => {
    let release!: () => void
    const entry = new Promise<void>(resolve => {
      release = resolve
    })
    writesInFlightRef.current.add(entry)
    return () => {
      release()
      writesInFlightRef.current.delete(entry)
    }
  }, [])
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

  // A pending edit is SENT on unmount, not cancelled. In-app navigation tears
  // this page down without firing `beforeunload`, so inside the debounce window
  // that timer is the only thing that would ever have persisted what the user
  // just typed — cancelling it would trade a leaked timer for silent data loss.
  // Same call the `minsTimer` teardown above makes for the auto-sync interval,
  // for the same reason.
  useEffect(
    () => () => {
      // `dirtyRef` is part of the gate, not just the write below. A save that
      // FAILED clears the debounce on entry and releases its tracking on the way
      // out, yet leaves the buffer dirty and re-arms nothing — only a keystroke
      // arms the debounce. Gating on the timer and the tracked set alone would
      // send that edit nowhere on the next navigation, which is the loss this
      // effect exists to prevent.
      if (!saveTimer.current && !writesInFlightRef.current.size && !dirtyRef.current) return

      if (saveTimer.current) {
        window.clearTimeout(saveTimer.current)
        saveTimer.current = null
      }

      // Wait for the tracked requests before writing, for two different reasons.
      // An autosave may already be retrying newer buffer snapshots, so the write
      // needs the mtime that save produced: sending alongside its last attempt
      // would carry a superseded mtime, come back ESTALE, and lose the edit — the
      // exact outcome this effect exists to prevent. A move has to finish for an
      // unrelated reason: it is what retargets `pathRef` onto the note's new
      // path, and writing before that lands sends the edit to the path the move
      // is vacating.
      //
      // The wait is bounded by the requests it waits on and holds no timer,
      // listener or interval, only a promise closure, so unlike the debounce
      // cancelled above it cannot run component logic at an arbitrary later time.
      //
      // `flushSave` is deliberately NOT reused here, and not because of its state
      // setters — React 18 makes a post-unmount setState a silent no-op. The
      // reason is that it reads LIVE refs: its retry loop re-reads
      // `contentRef.current` on every attempt, and after unmount a move's own
      // continuation reopens the note at its new path and repoints `contentRef`
      // at DISK content. Reusing it would let that loop send the disk bytes back
      // and clear the dirty flag against them, dropping the edit this effect
      // exists to save. It also mutates `mtimeRef` and `saveTimer`, which a
      // still-running `relocate` reads after this component is gone.
      const activeWrites = [...writesInFlightRef.current]
      // The buffer is snapshotted HERE, before the wait. No keystroke can arrive
      // after unmount, and a request settling during the wait can repoint
      // `contentRef` at disk content — a move reopens the note at its new path —
      // which would write the file back unchanged and drop the edit.
      const pendingContent = contentRef.current
      const saveDirtySnapshot = async () => {
        const vault = vaultRef.current
        // The path is read AFTER the wait, so a completed move contributes its
        // NEW path. `dirtyRef` is read live for the same reason: an in-flight
        // save that succeeded during the wait already persisted this buffer.
        const path = pathRef.current
        // Read the delete state AFTER the wait above, because a delete can be
        // confirmed while that save is still in flight.
        //
        // This is DEFENSE IN DEPTH, not a live hazard: reaching it needs a dirty
        // buffer whose own note has a DELETE in flight, and three separate
        // invariants currently make that state unreachable. `removeNote` flushes
        // a pending edit and returns if it is still dirty BEFORE it arms
        // `deletingRef`, and `edit` and `markDirty` — the only two sites that set
        // the flag — both refuse while `openNoteIsDeleting()`. The check is here
        // anyway because this write bypasses `flushSave`, and with it the ESTALE
        // branch that recognises the backend's refusal to resurrect a deleted
        // note; the callers below swallow that rejection, so without this the
        // teardown write's safety would rest entirely on three invariants held
        // in two other functions. Keeping it local means a later change to
        // `removeNote`'s flush ordering cannot quietly turn this into a recreate.
        if (targetsSameNote(deletingRef.current, vault, path)) return
        if (vault && path && dirtyRef.current) {
          await notesApi.saveNote(vault, path, pendingContent, mtimeRef.current ?? undefined)
        }
      }
      if (activeWrites.length) {
        void Promise.all(activeWrites).then(saveDirtySnapshot).catch(() => undefined)
      } else {
        void saveDirtySnapshot().catch(() => undefined)
      }
    },
    [],
  )

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
  // POSITIVE confirmation, not "not yet known": while health is still loading
  // this is false, so a delete cannot be offered before the backend has said it
  // trashes rather than unlinks.
  const canTrash = (health?.features ?? []).includes('trash')

  // ---- sync settings ----------------------------------------------------
  // React Query for the read, like every other server read here, so it is cached
  // and shares the app's query-key namespace. The controls below then work off
  // local state seeded from it: a refetch must not yank a value out from under a
  // keystroke mid-edit.
  const { data: settingsData, error: settingsLoadError } = useQuery({
    queryKey: ['md-notebook', 'settings'],
    queryFn: () => notesApi.settings(),
    retry: false,
    // Poll so a sync the BACKEND ran (this page never initiates auto-sync) ages
    // the "Synced N ago" label and surfaces the server-owned lastSync without an
    // interaction. Only server-owned fields (lastSync, seq) are re-applied on a
    // refetch — the editable controls are seeded once (below), so the poll never
    // yanks the toggle or interval field mid-edit.
    refetchInterval: 60_000,
  })
  /**
   * A failed READ matters as much as a failed write: it leaves the page showing
   * the defaults, which is a different setting from the user's own, so the switch
   * would claim auto sync is off while the backend keeps pushing. Write errors win
   * because they report the more recent action.
   */
  const settingsError =
    settingsWriteError ?? (settingsLoadError instanceof Error ? settingsLoadError.message : null)

  const legacyCleared = useRef(false)
  useEffect(() => {
    const s = settingsData?.settings
    // Guard the shape, do not just truthiness-check `settingsData`: a malformed or
    // older backend response without `settings` must degrade to the defaults the
    // controls already hold, not crash the whole Notes page reading `s.autoSync`.
    if (!s) return
    // `lastSync` is SERVER-OWNED and moves as the background loop syncs, so merge
    // it on EVERY response. Merge rather than replace so a just-set per-vault
    // stamp from `runSync` is not dropped by a slightly older read.
    setLastSyncByVault(prev => ({ ...prev, ...(s.lastSync ?? {}) }))
    // The server's authoritative interval, tracked on every response, so a
    // REJECTED interval write can roll the field back to server truth.
    serverMinsRef.current = clampAutoSyncMins(s.autoSyncMins)
    // Clear the dead localStorage prefs exactly once. `autoSync`/`autoSyncMins`
    // used to live here; they are server-owned now and the old values are NEVER
    // read for migration — a stale `mdnb-auto-sync=true` must not re-authorize
    // unattended push for a user who had turned it off.
    if (!legacyCleared.current) {
      legacyCleared.current = true
      clearPref(LEGACY_LS.autoSync)
      clearPref(LEGACY_LS.autoSyncMins)
    }
    // Reconcile the EDITABLE controls to the server's value on every response —
    // so another tab's enable/disable/interval change, and the refetch after our
    // own write, both reach this tab — but ONLY while this tab has no write in
    // flight and no interval keystroke pending. Skipping then is what keeps the
    // poll from yanking a value the user is mid-way through changing; the write
    // already in flight (or about to fire) carries the newer intent.
    if (writePendingRef.current > 0 || minsTimer.current !== null) return
    setAutoSync(s.autoSync)
    // CLAMPED, not only on write: a stored 0 — written before the clamp existed,
    // or by an older backend — must not surface as a real cadence. 0 means "no
    // interval expressed" and lands on the default.
    setAutoSyncMins(clampAutoSyncMins(s.autoSyncMins))
  }, [settingsData])

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
    // A mutation arriving while the debounce save is active must observe that
    // request's outcome. Starting a second save here could clear `dirtyRef`
    // after the first one failed and let the mutation cross a failed barrier.
    if (saveInFlightRef.current) {
      await saveInFlightRef.current
      return
    }
    if (!pathRef.current || !dirtyRef.current) return
    // The note this flush is persisting, as a VAULT + PATH pair. A save can still
    // be in flight when the user deletes that note from the row action bar, and
    // the conflict handler below must be able to tell "the open note changed on
    // disk" from "the note I was saving is gone because I just deleted it".
    // The vault half is not optional: a path here is vault-RELATIVE, so on a
    // switch to another vault holding the same name a path-only comparison passes
    // and the banner offers "Use the file on disk" against the NEW vault's buffer.
    const savingPath = pathRef.current
    const saving = { vault: vaultRef.current, path: savingPath }
    const run = (async () => {
      const doneSave = trackWrite()
      try {
        // Save until what landed matches what the editor holds. `contentRef` can
        // move while the request is in flight, and the debounce timer was
        // cancelled above — clearing `dirty` against a stale snapshot would leave
        // that newer text with nothing scheduled to persist it. Bounded so a fast
        // typist cannot spin here; whatever is left stays dirty for the next
        // debounce to pick up.
        for (let attempt = 0; attempt < 3; attempt++) {
          // The CONTENT is re-read live each attempt — that is the point of the
          // loop. The TARGET is the captured pair, never the live refs: a vault
          // switch or a rename during the round trip would otherwise redirect
          // attempt 2 at whatever is open by then, writing this note's text
          // somewhere the user never asked for.
          const sent = contentRef.current
          const res = await notesApi.saveNote(
            saving.vault,
            saving.path,
            sent,
            mtimeRef.current ?? undefined,
          )
          mtimeRef.current = res.mtime
          if (contentRef.current === sent) {
            setDirty(false)
            dirtyRef.current = false
            break
          }
          // The buffer moved on. Only keep retrying while it is still THIS note in
          // THIS vault; otherwise the dirty flag being cleared below would belong to
          // a different buffer entirely.
          if (!targetsSameNote(saving, vaultRef.current, pathRef.current)) return
        }
      } catch (e) {
        const body = (e as { body?: { code?: string; mtime?: number; disk?: string } }).body
        if (body?.code === 'ESTALE') {
          // The note changed on disk since it was opened. Surface both versions
          // rather than clobbering either — but ONLY while that note is still
          // open. Deleting a note mid-flush also lands here (the backend refuses
          // to resurrect it and returns the recreate sentinel), and showing the
          // banner then would offer "Keep my version" for a note the user just
          // deleted — accepting it would put the file back.
          if (!targetsSameNote(saving, vaultRef.current, pathRef.current)) return
          setFileConflict({ mtime: body.mtime ?? 0, disk: body.disk ?? '' })
        } else {
          setError(e instanceof Error ? e.message : String(e))
        }
      } finally {
        doneSave()
      }
    })()
    saveInFlightRef.current = run
    try {
      await run
    } finally {
      if (saveInFlightRef.current === run) saveInFlightRef.current = null
    }
  }, [trackWrite])

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
      // The buffer went dirty WHILE this read was in flight — the user typed, or
      // a flush-then-mutate handler (sync, autosave) is mid-write. Applying disk
      // content now would overwrite those keystrokes, and a retrying `flushSave`
      // would then persist the disk text it just read. Sequence alone cannot see
      // this: no later open happened, so the read is still "current".
      //
      // Unconditional on purpose — NOT `&& pathRef.current === path`. The common
      // shape is cross-note: click B, then type before B's read lands. Those
      // keystrokes go to A (the pane still shows it, and `pathRef` is repointed
      // only below), so a path comparison is FALSE exactly when the loss is real,
      // and A's text would be replaced by B's with its save timer left a no-op.
      // Refusing means the click does not take effect and the user clicks again —
      // the same trade the identical guard above the read already makes.
      if (dirtyRef.current) return
      pathRef.current = path
      setActivePath(path)
      // Close the drawer on pick, or the full-width list is a one-way door.
      if (isMobile) setNarrowPanelOpen(false)
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
  }, [flushSave, isMobile])

  // Load the note list whenever the vault changes, then restore the open note.
  useEffect(() => {
    vaultRef.current = activeVaultId
    if (!activeVaultId) return
    savePref(LS.activeVault, activeVaultId)
    // No last-sync read here: it is server state, held per vault in
    // `lastSyncByVault`, so switching vaults needs no local lookup.
    // Pins are per-vault, so they are re-read here rather than carried over —
    // a path pinned in one vault means nothing in another. Collapsed folders
    // are per-vault for the same reason: the trees are unrelated.
    setPinned(new Set(loadPref<string[]>(pinnedKey(activeVaultId), [])))
    setCollapsed(new Set(loadPref<string[]>(collapsedKey(activeVaultId), [])))
    setRenamingPath(null)
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
      // Refuse edits to a note whose delete is already in flight. The editor
      // stays mounted for the round trip (the row, not the pane, shows the
      // pending state), so on a slow vault a keystroke can land after the
      // confirmation — and the completion handler then cancels the save and
      // clears the buffer, so the text would exist nowhere: not on disk, not in
      // `.trash`. Dropping the keystroke is the coherent outcome; the note is on
      // its way out, and the alternative is a pending save that recreates it.
      if (openNoteIsDeleting()) return
      setContent(next)
      contentRef.current = next
      setDirty(true)
      dirtyRef.current = true
      if (saveTimer.current) window.clearTimeout(saveTimer.current)
      saveTimer.current = window.setTimeout(() => {
        void flushSave().then(() => loadNotes())
      }, SAVE_DEBOUNCE_MS)
    },
    [flushSave, loadNotes, openNoteIsDeleting],
  )

  // Mark the note dirty WITHOUT committing content — fired on the first
  // keystroke inside a block editor, before its blur/commit. This flips
  // dirtyRef so a capture-phase sync shortcut bails out (it reloads only when
  // clean) instead of reloading the note and discarding the in-progress edit.
  const markDirty = useCallback(() => {
    // Same guard as `edit`: a block editor's first keystroke must not mark a
    // note dirty while its delete is in flight.
    if (openNoteIsDeleting()) return
    setDirty(true)
    dirtyRef.current = true
  }, [openNoteIsDeleting])

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
      // Invalidate any in-flight note read before flushing — see `removeNote`
      // for why: a read resolving mid-flush repoints `contentRef` at disk content
      // and the retry loop then persists THAT, dropping the pending edit.
      openSeqRef.current += 1
      if (dirtyRef.current) await flushSave()
      // Still dirty means the write failed — typically a stale-write conflict
      // waiting on the user to pick a version. Switching clears the editor, so
      // the unsaved content would be gone with no way back to it.
      if (dirtyRef.current) return
      vaultRef.current = id
      setActiveVaultId(id)
      // Drop the row state keyed by a vault-RELATIVE path. Carried across a
      // switch, an open confirmation or an inline rename input would re-target a
      // same-named note in the NEW vault, and confirming would act on a note the
      // user never chose.
      setConfirmDelete(null)
      setRenamingPath(null)
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

  /**
   * Persist a new pinned set for the ACTIVE vault. Pins are per-vault and local
   * to this machine — see `pinnedKey` for why they are not written into the note.
   */
  const writePinned = useCallback((next: Set<string>) => {
    setPinned(next)
    if (vaultRef.current) savePref(pinnedKey(vaultRef.current), [...next])
  }, [])

  /** Persist a new collapsed-folder set for the ACTIVE vault. Same shape as pins. */
  const writeCollapsed = useCallback((next: Set<string>) => {
    setCollapsed(next)
    if (vaultRef.current) savePref(collapsedKey(vaultRef.current), [...next])
  }, [])

  const isPinned = useCallback((path: string) => pinned.has(path), [pinned])

  const tree = useMemo(() => buildTree(notes), [notes])

  /**
   * The note paths in the order the panel is rendering them right now. Declared
   * here (above the actions that consume it) because "the next note down" is a
   * property of the VIEW, not of the note list: the three modes order
   * differently, and a collapsed folder's notes are off screen entirely.
   */
  const visibleNotePaths = useMemo(() => {
    if (query.trim()) return results.map(r => r.path)
    if (view === 'list') return orderNotes(notes, SORTS[sortKey].cmp, isPinned).map(n => n.path)
    return flattenVisibleNotes(tree, SORTS[sortKey].cmp, isPinned, collapsed)
  }, [query, results, view, notes, sortKey, isPinned, tree, collapsed])

  const togglePin = useCallback(
    (path: string) => {
      const next = new Set(pinned)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      writePinned(next)
    },
    [pinned, writePinned],
  )

  /** Follow a pin across a move/rename, or drop it when the note is deleted. */
  const repointPin = useCallback(
    (vault: string | null, from: string, to: string | null) => {
      // The vault is an ARGUMENT, not a read of `vaultRef`: this runs after an
      // await, and the pin list is stored per vault. Reading the ref here would
      // write vault A's pin change into vault B's key after a switch — or
      // rewrite B's own pin at the same relative path. Read-modify-write goes
      // through storage rather than `prev`, because the in-memory `pinned` set
      // belongs to whichever vault is active now, which may not be this one.
      if (!vault) return
      const key = pinnedKey(vault)
      const stored = new Set(loadPref<string[]>(key, []))
      if (!stored.has(from)) return
      stored.delete(from)
      if (to) stored.add(to)
      savePref(key, [...stored])
      // Only touch React state while this vault is still the one on screen.
      if (vaultRef.current === vault) setPinned(new Set(stored))
    },
    [],
  )

  const relocate = useCallback(
    async (from: string, to: string) => {
      if (!to || to === from) return
      // Captured before any await: every completion step below is scoped to the
      // vault that owned the note, since both the request and the pin list are.
      const vault = vaultRef.current
      try {
        // Flush edits at the OLD path first, then retarget — otherwise a stray
        // autosave would recreate the file we just moved away from.
        // Same in-flight-read invalidation as `removeNote` before the flush.
        openSeqRef.current += 1
        if (pathRef.current === from && dirtyRef.current) await flushSave()
        // A failed flush leaves the editor dirty against the OLD path. Renaming
        // now would retarget it to `to` without ever reconciling that content,
        // so a later save could overwrite the moved file from a stale base.
        if (pathRef.current === from && dirtyRef.current) return
        // Tracked THROUGH the retarget below, not just around the request. The
        // unmount flush writes against `pathRef`, and until that assignment lands
        // `pathRef` still names the path this move is vacating -- a flush that ran
        // in the gap would send the edit to a file the server has already moved,
        // and the swallowed ESTALE would lose it.
        const doneMove = trackWrite()
        try {
          await notesApi.moveNote(vault, from, to)
          repointPin(vault, from, to)
          if (vaultRef.current !== vault) return
          if (pathRef.current === from) {
            pathRef.current = to
            setActivePath(to)
            savePref(LS.openNote, to)
          }
        } finally {
          doneMove()
        }
        await loadNotes()
        if (pathRef.current === to) await openNote(to)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [flushSave, loadNotes, openNote, repointPin, trackWrite],
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

  /** Copy a note beside itself and open the copy. */
  const duplicateNote = useCallback(
    async (path: string) => {
      const vault = vaultRef.current
      try {
        // Persist a pending edit first: the backend copies what is ON DISK, so
        // duplicating mid-debounce would silently drop what was just typed.
        if (pathRef.current === path && dirtyRef.current) {
          // Same in-flight-read invalidation as `removeNote` before the flush.
          openSeqRef.current += 1
          await flushSave()
          if (dirtyRef.current) return
        }
        const { path: copy } = await notesApi.duplicateNote(vault, path)
        // A vault switch while the copy was in flight would make the reload and
        // the open below act on the WRONG vault — `copy` is a vault-relative
        // path, so opening it elsewhere reads a different note or 404s.
        if (vaultRef.current !== vault) return
        await loadNotes()
        await openNote(copy)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [flushSave, loadNotes, openNote],
  )

  /** Delete a note. The confirmation dialog is the caller's gate, not this. */
  const removeNote = useCallback(
    async (path: string) => {
      const vault = vaultRef.current
      // ONE delete at a time, ACROSS vaults. `deletingRef` holds a single entry,
      // so starting a second delete while the first is in flight reassigns it —
      // which silently re-opens the edit guard below for the FIRST note, and that
      // note's completion then clears the editor and cancels the save, discarding
      // whatever was typed in the gap. That is true of a delete in another vault
      // too, so the refusal stays global even though the identity below is
      // vault-scoped. Refused, not queued: the row's `deleting` badge shows why.
      if (deletingRef.current !== null) return
      // Invalidate any in-flight note read FIRST, before the flush below — not
      // just before the DELETE. `openNote` claims its sequence and then awaits;
      // a read that resolves during the flush would still be current and would
      // repoint `contentRef`/`mtimeRef` at disk content mid-write. `flushSave`
      // retries while `contentRef` keeps moving, so it would then persist the
      // DISK text and clear the dirty flag — dropping the very pending edit this
      // flush exists to save. Bumping here costs nothing: a superseded read has
      // no result worth applying either way.
      openSeqRef.current += 1
      // Persist a pending edit BEFORE the note is trashed. The save is debounced
      // by SAVE_DEBOUNCE_MS, so typing and then confirming the dialog inside that
      // window would trash the STALE file on disk and then clear the buffer — the
      // last edit would exist nowhere, not even in `.trash`, which is what makes
      // a delete recoverable in the first place.
      if (pathRef.current === path && dirtyRef.current) {
        await flushSave()
        // Still dirty means the write was refused — an ESTALE conflict awaiting
        // the user's decision. Deleting now would trash the disk version and drop
        // their unreconciled text with it, so leave the note (and the banner) be.
        if (dirtyRef.current) {
          return
        }
      }
      // Where to land afterwards, resolved from the order on screen BEFORE the
      // row disappears — the next note down, else the one above.
      const wasOpen = pathRef.current === path
      const next = wasOpen ? neighborAfterDelete(visibleNotePaths, path) : null
      setDeleting({ vault, path })
      deletingRef.current = { vault, path }
      try {
        await notesApi.deleteNote(vault, path)
        // Scope every mutation below to the vault that owned the note. The user
        // can switch vaults while the DELETE is in flight, and `pathRef` holds a
        // VAULT-RELATIVE path — a note at the same path in the new vault would
        // match, so this would cancel that note's pending save and clear its
        // editor, discarding an edit in a vault this delete never touched. The
        // pin write is scoped for the same reason.
        if (vaultRef.current !== vault) return
        if (pathRef.current === path) {
          // Cancel the debounced save BEFORE clearing the editor: a pending
          // flush would otherwise write the note straight back to disk and
          // resurrect the file that was just deleted.
          if (saveTimer.current) {
            window.clearTimeout(saveTimer.current)
            saveTimer.current = null
          }
          setDirty(false)
          dirtyRef.current = false
          pathRef.current = null
          setActivePath(null)
          setContent('')
          contentRef.current = ''
          setBacklinks([])
          setEditBlock(null)
          setFileConflict(null)
          savePref(LS.openNote, null)
        }
        repointPin(vault, path, null)
        await loadNotes()
        // Open the neighbour only after the listing refreshed, so the note being
        // opened is one the panel still knows about.
        if (next && vaultRef.current === vault) await openNote(next)
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        // Clear unconditionally: on failure the row must become interactive
        // again, and on success it is gone from the list anyway.
        setDeleting(prev => (prev?.vault === vault && prev.path === path ? null : prev))
        const d = deletingRef.current
        if (d?.vault === vault && d.path === path) deletingRef.current = null
      }
    },
    [flushSave, loadNotes, openNote, repointPin, visibleNotePaths],
  )

  // Deliberately does NOT catch, same reasoning as `savePat` below: it is
  // only ever invoked from SettingsPage's Remove-confirm button, which
  // catches this rejection itself and reports it inline, next to the button
  // that produced it — the shared `error` banner below only renders in the
  // main-editor branch and is invisible while Settings is open.
  const forgetVault = useCallback(
    async (id: string) => {
      // Forgetting the ACTIVE vault clears the editor below, so persist first —
      // and stop if the save could not land, exactly as switching does.
      if (id === vaultRef.current && dirtyRef.current) {
        // Same in-flight-read invalidation as `removeNote` before the flush.
        openSeqRef.current += 1
        await flushSave()
        if (dirtyRef.current) return
      }
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
    },
    [loadVaults, flushSave],
  )

  // Deliberately does NOT catch, unlike its siblings above (removeNote etc.)
  // that swallow into the shared `error` banner: that banner only renders in
  // the main-editor branch, invisible while Settings is open, so a failure
  // here needs to surface right next to the button the user clicked instead.
  // SettingsPage's Save/Clear handlers catch this rejection themselves and
  // report it inline.
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
      // Captured, not re-read: the reply's `lastSync` belongs to the vault this
      // run synced, and a vault switch during the round trip would otherwise
      // stamp the new vault's label with the old vault's time.
      const vault = vaultRef.current
      if (!vault) return
      const { result, lastSync: syncedAt } = await notesApi.sync(vault)
      // Only a conflict-free run counts as synced — with conflicts nothing was
      // pushed, so reporting success would mislead. The server says so too by
      // sending a null `lastSync`; the check is kept here so the label cannot
      // claim a sync even if a backend reports both.
      if (!result.conflicts.length) {
        if (typeof syncedAt === 'number') {
          setLastSyncByVault(prev => ({ ...prev, [vault]: syncedAt }))
        }
      } else {
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

  // No foreground auto-sync timer. Auto sync runs entirely in the app BACKEND
  // (syncer.py), which re-reads settings.json every few seconds and pushes every
  // writable vault on the configured interval — so it keeps working with the tab
  // closed, and it stops within one tick when the user turns auto sync off in ANY
  // tab. A page-scoped timer here would be redundant with it and, because this
  // page's autoSync state is seeded once, would keep pushing this vault after a
  // revocation made in another tab. The manual Sync button/shortcut below is the
  // only page-initiated sync.

  // Periodic autosave: commit pending edits to LOCAL git history, never push.
  // Separate from auto sync on purpose — a commit stays on this machine, so it is
  // safe to do unasked and defaults ON, while pushing to a remote remains an
  // explicit choice. The note file itself is already written to disk by the
  // editor's 1s debounce; this is what gives the user version history without
  // pressing anything.
  const runAutoCommit = useCallback(async () => {
    if (!vaultRef.current) return
    // A block editor holds its draft in local state until commit/blur, and the
    // reload below would unmount it and lose the typed text. Skip this cycle.
    if (editBlock) return
    const vault = vaultRef.current
    try {
      if (dirtyRef.current) await flushSave()
      // A failed save (an ESTALE conflict awaiting the user's decision) leaves
      // `dirty` set. Committing now would record the on-disk version while the
      // user's unreconciled edit sits in the editor.
      if (dirtyRef.current) return
      const { result } = await notesApi.commit(vault)
      // Only touch the listing when something was actually committed — the
      // pending badges are what change. A quiet tick must stay invisible.
      if (result.committed.length && vaultRef.current === vault) await loadNotes()
    } catch {
      // Deliberately silent: an autosave the user did not ask for must not throw
      // a banner over their writing. A real problem surfaces the moment they
      // press the button themselves, which reports errors normally.
    }
  }, [editBlock, flushSave, loadNotes])

  const runAutoCommitRef = useRef(runAutoCommit)
  useEffect(() => {
    runAutoCommitRef.current = runAutoCommit
  }, [runAutoCommit])
  useEffect(() => {
    if (!autoCommit || !activeVaultId) return
    // Read-only vaults refuse writes server-side; do not poke them every cycle.
    if (vaults?.find(v => v.id === activeVaultId)?.readOnly) return
    const id = window.setInterval(
      () => void runAutoCommitRef.current(),
      AUTO_COMMIT_MINS * 60_000,
    )
    return () => window.clearInterval(id)
  }, [autoCommit, activeVaultId, vaults])

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

  /** Row-level affordances, bundled so the tree renderer takes one prop. */
  const noteActions = useMemo<NoteActions>(
    () => ({
      isPinned,
      onTogglePin: togglePin,
      onDuplicate: p => void duplicateNote(p),
      // Offered ONLY when the backend confirms it moves notes to `.trash`. An
      // older bundle's DELETE unlinks the file, and the confirmation this opens
      // promises the note is restorable from `.trash` — so without the capability
      // the honest UI has no delete button at all, and the stale-backend banner
      // names `trash` as the reason.
      onDelete: canTrash ? (p, t) => setConfirmDelete({ path: p, title: t }) : undefined,
      onMove: moveNote,
      renamingPath,
      // Scoped to the vault on screen: `deleting.path` is vault-relative, so
      // handing it over unscoped would dim a same-named row in another vault and
      // refuse to open it while an unrelated delete was in flight.
      deletingPath: deleting?.vault === activeVaultId ? deleting.path : null,
      onRenameStart: setRenamingPath,
      onRenameEnd: () => setRenamingPath(null),
      onRename: renameNote,
    }),
    [
      isPinned,
      togglePin,
      duplicateNote,
      moveNote,
      renamingPath,
      deleting,
      activeVaultId,
      renameNote,
      canTrash,
    ],
  )

  const toggle = useCallback(
    (name: string) => {
      const next = new Set(collapsed)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      writeCollapsed(next)
    },
    [collapsed, writeCollapsed],
  )

  const togglePanel = useCallback(() => {
    if (isMobile) { setNarrowPanelOpen(v => !v); return }
    setPanelOpen(v => {
      savePref(LS.panelOpen, !v)
      return !v
    })
  }, [isMobile])

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
  // Directory of the open note, which is what resolves its relative image
  // sources. Derived here rather than in `Preview` because the vault's local
  // path lives in this page's state, not in the note body.
  const noteDir = noteDirPath(activeVault, activePath)
  // `pending` means "differs from the last commit", which is only actionable when
  // there is a remote the note has not reached yet. On a local-only vault it has
  // no destination, clears itself on the next autosave, and reads as "not saved"
  // — so the row shows nothing.
  const showSyncBadge = !activeVault?.localOnly
  const lastSync = activeVaultId ? (lastSyncByVault[activeVaultId] ?? null) : null
  const ago = lastSync ? agoBucket(lastSync) : null
  // A local-only vault has no remote, so the button commits to local git history
  // and nothing else — label it for what it does, and drop the "Synced N ago"
  // reading, which would claim a backup that never happened.
  const syncLabel = syncing
    ? i18nT('apps.mdNotebook.sync.syncing')
    : activeVault?.localOnly
      ? i18nT('apps.mdNotebook.sync.localAction')
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

      {confirmDelete && (
        <ConfirmDialog
          title={i18nT('apps.mdNotebook.row.deleteTitle', { title: confirmDelete.title })}
          // `<Trans>` rather than a plain string: `.trash` inside the sentence is a
          // link that opens the folder, and the app hides that folder from its own
          // listing, so this is the user's only way in at the moment they need it.
          // One catalog key with a `<trash>` tag keeps the sentence whole for
          // translators instead of splitting it around the link.
          body={
            <Trans
              i18nKey="apps.mdNotebook.row.deleteConfirm"
              components={{
                trash: (
                  <InlineLink
                    onClick={() => void openTrash()}
                    title={i18nT('apps.mdNotebook.trash.open')}
                  />
                ),
              }}
            />
          }
          // Only messages live here now: the two cases where clicking the link
          // produces nothing visible (no folder yet, no file manager on the host).
          footer={
            trashMsg && (
              <div style={{ fontSize: '11px', color: 'var(--muted)' }}>{trashMsg}</div>
            )
          }
          confirmLabel={i18nT('apps.mdNotebook.row.deleteAction')}
          cancelLabel={i18nT('apps.mdNotebook.connect.cancel')}
          onCancel={() => {
            setTrashMsg(null)
            setConfirmDelete(null)
          }}
          onConfirm={() => {
            const target = confirmDelete.path
            setTrashMsg(null)
            setConfirmDelete(null)
            void removeNote(target)
          }}
        />
      )}

      {/* Floating panel toggle: stays put when the panel hides. */}
      <button
        type="button"
        className="pi-morph mdnb-collapse"
        onClick={togglePanel}
        aria-label={
          panelShown
            ? i18nT('apps.mdNotebook.panel.hide')
            : i18nT('apps.mdNotebook.panel.show')
        }
        style={{
          position: 'absolute',
          // Same rect as the sessions sidebar's floating collapse button
          // (ChatPage: top-[9px] left-2 w-7 h-7): a 28px control whose center
          // lands on the 23px shared control baseline, so this button, the
          // sessions toggle, and the left-nav collapse arrow all line up.
          top: '9px',
          left: '8px',
          zIndex: 10,
          width: '28px',
          height: '28px',
          borderRadius: '8px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          // No inline `background`: `.mdnb-collapse` (styles.ts) owns rest +
          // hover painting. An inline `background: transparent` would outrank
          // the `:hover` rule (inline beats author stylesheet), leaving the
          // hover tint dead — the sessions toggle gets its tint from a
          // stylesheet class for the same reason.
          border: 'none',
          transition: 'color .15s, background .15s',
        }}
      >
        {panelShown ? <PanelLeftLight size={16} /> : <PanelLeftSolid size={16} />}
      </button>

      {panelShown && (
        <div
          style={{
            width: isMobile ? '100%' : `${panelW}px`,
            flexShrink: 0,
            position: 'relative',
            display: 'flex',
            flexDirection: 'column',
            // Top edge flush with the pane, mirroring the dashboard's left-nav
            // card (`mt-0 mb-2`): both cards sit in the same grid row, so a top
            // margin here would make this panel start lower than the nav beside it.
            margin: '0 0 8px',
            background: 'var(--bg-elevated)',
            border: '1px solid var(--border)',
            borderRadius: '16px',
            boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
          }}
        >
          {/* Header: the vault selector doubles as the panel title. Mirrors the
              sessions sidebar header (mt-0.5 h-10 → 40px row, its content on the
              23px baseline); the vault trigger starts at 36px to clear the
              floating collapse button (left 8px + 28px), same as the sidebar's
              pl-9 content inset. */}
          <div
            style={{
              height: '40px',
              marginTop: '2px',
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
                marginLeft: '28px',
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
                  ...RAIL_TYPE.panelTitle,
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
            {/* New note, at the top level of the vault — outside every folder.
                It lives here rather than beside the document controls because
                creating a note is a navigation act on this list, and "outside
                all folders" only reads as a location next to the tree. */}
            <button
              type="button"
              onClick={() => void newNote()}
              aria-label={i18nT('apps.mdNotebook.panel.newRootNote')}
              title={i18nT('apps.mdNotebook.panel.newRootNote')}
              style={{ ...iconBtn, marginLeft: 'auto' }}
              className="mdnb-act"
            >
              <Plus size={16} />
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
                      ...RAIL_TYPE.row,
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
                    ...RAIL_TYPE.row,
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
                ...RAIL_TYPE.row,
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
                      ...MENU_SECTION_LABEL,
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
                        ...RAIL_TYPE.row,
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
                      ...MENU_SECTION_LABEL,
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
                        ...RAIL_TYPE.row,
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
          {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions -- drag-drop only: this is the list's scroll background, which has no activation of its own, so a role and tab stop would add a focus stop that does nothing */}
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
                      actions={noteActions}
                    />
                  ))
                : (
                    <div style={{ padding: '10px', ...RAIL_TYPE.secondary, color: 'var(--muted)' }}>
                      {i18nT('apps.mdNotebook.panel.noMatches')}
                    </div>
                  )
              : view === 'list'
                ? orderNotes(notes, SORTS[sortKey].cmp, isPinned).map(n => (
                      <NoteRow
                        key={n.path}
                        note={n}
                        active={!settingsOpen && n.path === activePath}
                        onOpen={p => void openNote(p)}
                        showSyncBadge={showSyncBadge}
                        actions={noteActions}
                        showFolder
                      />
                    ))
                : renderTree(tree, 0, '', {
                    activePath: settingsOpen ? null : activePath,
                    onOpen: p => void openNote(p),
                    collapsed,
                    toggle,
                    showSyncBadge,
                    cmp: SORTS[sortKey].cmp,
                    actions: noteActions,
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
          autoCommit={autoCommit}
          syncPrefsError={settingsError}
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
          onAutoCommit={setAutoCommitPref}
          onSetShortcut={setSyncShortcutPref}
          onRecordingChange={on => {
            recordingShortcutRef.current = on
          }}
        />
      ) : (
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div ref={headerBandRef} style={{ position: 'relative' }}>
          <div
            ref={headerControlsRef}
            style={{
              position: 'absolute',
              top: '24px',
              right: `${COLUMN_PAD_X}px`,
              zIndex: 2,
              display: 'flex',
              gap: '4px',
              alignItems: 'center',
              // The cluster's width is content-driven (the sync label varies by
              // locale and state), so on a pane too narrow to seat it in one
              // row it would poke past the left edge and `overflow-x-hidden`
              // would clip the view controls. Cap it to the measured band and
              // wrap instead: every control stays reachable, and the wrapped
              // height flows into the stacking arithmetic above, which already
              // drops the title below whatever the cluster occupies.
              maxWidth: headerBandWidth
                ? `${Math.max(0, headerBandWidth - COLUMN_PAD_X * 2)}px`
                : undefined,
              flexWrap: 'wrap',
              justifyContent: 'flex-end',
            }}
          >
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
            {/* Reading column / full width. A sibling of the rendered-raw switch
                because both answer "how do I want to read this note". The
                control itself is `ReadingWidthToggle`, the component an artifact
                already uses for this exact choice, so the two views cannot drift
                apart on label, pressed state or accent. Only the stored
                preference is this view's own (`LS.fullWidth`): a note and an
                artifact are different reading surfaces, often open at the same
                time in two panes, and one shared key would silently reflow the
                other. */}
            <ReadingWidthToggle value={fullWidth ? 'full' : 'md'} onToggle={toggleFullWidth} />
            <button
              type="button"
              onClick={() => void runSync()}
              disabled={syncing}
              title={
                activeVault?.localOnly
                  ? i18nT('apps.mdNotebook.sync.localTitle')
                  : undefined
              }
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
              maxWidth: columnMaxWidth,
              boxSizing: 'border-box',
              // Stacked: the title clears the cluster from below, so the top
              // pad carries the cluster's own height and the row is free.
              padding: stackTitle
                ? `${24 + headerControls.height + 8}px ${COLUMN_PAD_X}px 14px`
                : `24px ${COLUMN_PAD_X + titleClearance}px 14px ${COLUMN_PAD_X}px`,
            }}
          >
            {activePath ? (
              <InlineTitle
                path={activePath}
                onRename={n => renameNote(activePath, n)}
                mb="0"
              />
            ) : (
              <div
                style={{
                  // The placeholder alternates with `InlineTitle` in this exact
                  // slot, so it takes the same derived size: a literal here is
                  // the same drift, one branch away.
                  fontSize: `${DOC_H1_PX}px`,
                  fontWeight: DOC_HEADING_WEIGHTS[0],
                  color: 'var(--muted)',
                }}
              >
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
              {...ime.bindComposition<HTMLTextAreaElement>()}
              onKeyDown={e => {
                if (e.key !== 'Tab') return
                const ta = e.currentTarget
                const next = shiftListItem(content, ta.selectionStart, e.shiftKey)
                if (!next) return
                // Claim BEFORE the rewrite: IMEs use Tab to cycle the candidate
                // list, and on WebKit the keydown that commits a candidate
                // arrives after `compositionend` with `isComposing` already
                // false — so an unclaimed Tab would re-indent the list item and
                // overwrite the text still being composed.
                if (!ime.claimKey(e)) return
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
                // Full width drops the centring measure. Declared after the two
                // above so it overrides them, leaving their expression untouched.
                ...(fullWidth
                  ? { paddingLeft: `${COLUMN_PAD_X}px`, paddingRight: `${COLUMN_PAD_X}px` }
                  : {}),
                fontSize: `${DOC_BODY_PX}px`,
                lineHeight: DOC_BODY_LINE_HEIGHT,
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
                maxWidth: columnMaxWidth,
                boxSizing: 'border-box',
                padding: `14px ${COLUMN_PAD_X}px 32px`,
              }}
            >
              <Preview
                content={content}
                noteDir={noteDir}
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
