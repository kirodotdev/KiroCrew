import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { isDeadlineError } from '../api/queryClient'
import { FileText, Folder, Eye } from 'lucide-react'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'
import ErrorNotice from './ErrorNotice'
import type { SendMode } from '../pages/chat/ChatSettings'

import { i18nT } from '../i18n/t'
import { fmtBytes, fmtDateFields, fmtRelative } from '../i18n/format'

export type FileKind = 'file' | 'dir'

interface FileResult {
  path: string
  name: string
  size: number
  mtime: number
  /** Absent on responses from older backends — treated as 'file'. */
  kind?: FileKind
}

interface FileSearchResponse {
  results?: FileResult[]
  root?: string
}

interface Props {
  query: string
  anchorRef: React.RefObject<HTMLElement | null>
  open: boolean
  onSelect: (info: { path: string; relativePath: string; kind: FileKind }) => void
  onClose: () => void
  onFileOpen?: (path: string) => void
  project?: string
  /**
   * The composer's effective send binding (see ChatInput's SendMode). Consulted
   * for every empty-state copy: in 'ctrl-enter' mode a bare Enter is not a send
   * key, so the empty state must not name it as sending or as the held sender.
   */
  sendOnEnter?: SendMode
}

const formatSize = (bytes: number): string => fmtBytes(bytes)

function formatAge(mtime: number): string {
  const diff = Date.now() / 1000 - mtime
  // Under 30 days this is an elapsed age; beyond that a calendar date reads
  // better. Both halves now follow the app language instead of the browser's.
  if (diff < 86400 * 30) return fmtRelative(mtime)
  return fmtDateFields(mtime, { month: 'short', day: 'numeric' })
}

/**
 * Strip the project root prefix so the inserted token is a short relative path.
 *
 * Separator-aware: on native Windows the search result and the root both use
 * backslashes, so a `/`-only prefix check would never match and the picker
 * would insert the ABSOLUTE path instead of the short relative form.
 */
export function makeRelative(path: string, root: string): string {
  if (!root) return path
  const r = /[/\\]$/.test(root) ? root : root + (root.includes('\\') && !root.includes('/') ? '\\' : '/')
  return path.startsWith(r) ? path.slice(r.length) : path
}

/** Normalize a possibly-absent kind from the search response. */
export function resultKind(f: { kind?: FileKind }): FileKind {
  return f.kind === 'dir' ? 'dir' : 'file'
}

/**
 * Build the payload handed to onSelect. Directory paths get a trailing slash on
 * the relative form so the inserted @-token reads unambiguously as a folder.
 */
export function selectionFor(f: FileResult, root: string): { path: string; relativePath: string; kind: FileKind } {
  const kind = resultKind(f)
  const rel = makeRelative(f.path, root)
  return {
    path: f.path,
    // `endsWith` covers either separator so a Windows path is not given a second
    // trailing one; the inserted token then always ends in a slash.
    relativePath: kind === 'dir' && !/[/\\]$/.test(rel) ? rel + '/' : rel,
    kind,
  }
}

export default function FilePickerMenu({ query, anchorRef, open, onSelect, onClose, onFileOpen, project, sendOnEnter = 'enter' }: Props) {
  const rootRef = useRef('')
  const resultsRef = useRef<FileResult[]>([])
  const onFileOpenRef = useRef(onFileOpen)
  onFileOpenRef.current = onFileOpen

  // Debounce the query string — a timer + setState ONLY, not an API call, so the
  // fetch itself stays on React Query (below). React Query handles cancellation
  // (via the queryFn `signal`), caching, and dedup; this just throttles how often
  // the query key changes while the user types.
  const [debounced, setDebounced] = useState(query)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query), 200)
    return () => clearTimeout(t)
  }, [query])

  // File search via React Query (consistent with the $skill / /command pickers,
  // which already use useQuery). `enabled` gates on 2+ chars; the queryFn `signal`
  // aborts stale requests; `placeholderData` keeps the prior results on screen
  // while the next query resolves so the list doesn't flicker to empty.
  const { data, isFetching, isError, error } = useQuery<FileSearchResponse>({
    queryKey: ['file-search', debounced, project],
    queryFn: ({ signal }) => api.fileSearch(debounced, project, signal),
    enabled: open && debounced.length >= 2,
    // No `retry` override: the shared `retryPolicy` refuses to retry a deadline we
    // set ourselves, so the bound holds without one.
    placeholderData: prev => prev,
    staleTime: 10_000,
  })
  rootRef.current = data?.root || ''

  // Order bottom-up (shared helper) when the menu opens above. Gate on the LIVE
  // `query` length (not the debounced one) so results clear immediately when the
  // user drops below 2 chars; `data` is keyed on `debounced`, so it lags by up to
  // one debounce tick — the intended debounce behavior.
  // Gated on `isError` HERE, not at the render: keyboard nav counts this list too,
  // so a row merely hidden stays selectable via Arrow+Enter.
  const { ordered: results, initialIndex } = useMemo(() => {
    const raw = (open && !isError && query.length >= 2 ? data?.results : []) || []
    const above = anchorRef.current ? menuGeometry(anchorRef.current, raw.length, 48).above : false
    return bottomUpOrder(raw, above)
  }, [data, isError, open, query, anchorRef])

  // Open the highlighted file in the viewer (the eye/preview action) instead of
  // inserting an @-mention. Shared by the Cmd/Ctrl+Enter path (via onChoose's
  // withModifier flag) and the Alt+Enter path (via onAltEnter). Returns true so
  // the hook knows the default choose was superseded. Directories have nothing
  // to preview, so they fall through to the normal insert.
  const openInViewer = useCallback((idx: number): boolean => {
    const f = resultsRef.current[idx]
    if (f && resultKind(f) === 'file' && onFileOpenRef.current) {
      onFileOpenRef.current(f.path); onClose(); return true
    }
    return false
  }, [onClose])

  // Enter inserts the @-mention. Cmd/Ctrl+Enter opens in the viewer — the
  // shared useListKeyboardNav hook threads the modifier state through
  // onChoose's 2nd arg (withModifier).
  const choose = useCallback((idx: number, withModifier = false) => {
    const r = resultsRef.current
    const eff = idx >= r.length ? 0 : idx
    const f = r[eff]
    if (!f) return
    if (withModifier && openInViewer(eff)) return
    onSelect(selectionFor(f, rootRef.current))
  }, [onSelect, openInViewer])

  // "Settled and genuinely empty": only then does the menu have no claim on
  // the keyboard. During the debounce window (debounced lagging the live
  // query) or an in-flight fetch the results are transiently [] or stale, and
  // releasing Enter there would irreversibly send a draft whose mention the
  // user was still completing. A settled ERROR counts as settled-empty too —
  // the menu shows the same empty state and has nothing to offer, so keeping
  // the swallow there would recreate the trap on the error path.
  const releaseKeysWhenEmpty = query.length >= 2 && debounced === query && !isFetching && (data !== undefined || isError)

  // Shared Arrow/Enter/Tab/Escape + scroll-into-view (see useListKeyboardNav).
  // When the release gate is armed, Enter/Tab pass through and the menu closes
  // so the composer can still send the message (the #5029 prompt-mention trap).
  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    onChoose: choose,
    onClose,
    onAltEnter: openInViewer,
    releaseKeysWhenEmpty,
  })

  // Mirror the ordered results into the ref that choose()/openInViewer read at
  // keypress time, and set the initial selection (the bottom row when the menu
  // opens above) whenever the result set changes. Keyed on the memoized results,
  // so arrow-key navigation (which changes `selected` but not `results`) doesn't
  // reset the selection.
  useEffect(() => {
    resultsRef.current = results
    setSelected(initialIndex)
  }, [results, initialIndex, setSelected])

  // Scroll the selected row into view once results render (the selection is set
  // before rows mount, so the hook's own scrollIntoView no-ops on open). Keyed
  // on [results] so it fires on open + new search, not per-arrow (the hook
  // already scrolls on move). Matches the $skill picker.
  useEffect(() => {
    if (!open) return
    itemRefs.current[selectedRef.current]?.scrollIntoView({ block: 'nearest' })
  }, [results, open, itemRefs, selectedRef])

  if (!open || !anchorRef.current) return null

  const { above, top, bottom, left, width, maxHeight } = menuGeometry(anchorRef.current, results.length, 48)

  // 'ctrl-enter' makes a bare Enter a newline, so naming it as the send key —
  // held or releasing — would be false there.
  const ctrl = sendOnEnter === 'ctrl-enter'

  // A failed search is not an empty one: "no matching files" for a request that never
  // completed tells the user the file does not exist. The timeout arm names the cause.
  const timedOut = isDeadlineError(error)
  const failedKey = !releaseKeysWhenEmpty
    ? (timedOut
        ? 'components.filePickerMenu.search_timed_out'
        : 'components.filePickerMenu.search_failed')
    : ctrl
    ? (timedOut
        ? 'components.filePickerMenu.search_timed_out_ctrl_enter_sends'
        : 'components.filePickerMenu.search_failed_ctrl_enter_sends')
    : (timedOut
        ? 'components.filePickerMenu.search_timed_out_enter_sends'
        : 'components.filePickerMenu.search_failed_enter_sends')

  // Enter AND Tab are swallowed while the gate is closed, so Send is not
  // keyboard-reachable — the copy names Escape, whose branch runs before them.
  const emptyKey = query.length < 2
    ? (ctrl
        ? 'components.filePickerMenu.type_2_chars_to_search_files_ctrl_enter_held'
        : 'components.filePickerMenu.type_2_chars_to_search_files_enter_held')
    : isFetching
    ? (ctrl
        ? 'components.filePickerMenu.searching_ctrl_enter_held'
        : 'components.filePickerMenu.searching_enter_held')
    // Enter's meaning flips with the gate (pick → send), so the copy announces
    // it; the plain arm is the ≤200ms debounce flash between two announced ones.
    : !releaseKeysWhenEmpty
    ? 'components.filePickerMenu.no_matches'
    : ctrl
    ? 'components.filePickerMenu.no_matches_ctrl_enter_sends'
    : 'components.filePickerMenu.no_matches_enter_sends'

  // One region for every empty state, so a transition is a text change inside a
  // live region rather than a mount — what screen readers announce least well.
  // A failure leaves that region: an empty result is not an error.
  const empty = isError
    ? (
      <div className="px-3 py-3">
        {/* No hand-off: the composer's unsent message lives in this tree, so a
            navigation would discard the draft this picker was opened to complete. */}
        <ErrorNotice variant="inline" message={i18nT(failedKey)} />
      </div>
    )
    : <div role="status" className="px-3 py-3 text-[12px] text-muted">{i18nT(emptyKey)}</div>

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ ...(above ? { bottom } : { top }), left, width: Math.min(width, 420), maxHeight }}
    >
      {isError || results.length === 0 ? empty : results.map((f, i) => {
        const kind = resultKind(f)
        const isDir = kind === 'dir'
        return (
        <div
          role="option"
          aria-selected={i === selected}
          data-kind={kind}
          tabIndex={-1}
          key={f.path}
          ref={el => { itemRefs.current[i] = el }}
          className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
          title={f.path}
          onMouseEnter={() => setSelected(i)}
          onMouseDown={e => { e.preventDefault(); onSelect(selectionFor(f, rootRef.current)) }}
        >
          {isDir
            ? <Folder size={14} aria-label={i18nT('components.filePickerMenu.folder')} className="shrink-0 lucide-inline" />
            : <FileText size={14} className="shrink-0 lucide-inline" />}
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-mono font-semibold truncate">{isDir ? f.name + '/' : f.name}</div>
            <div className="text-[11px] text-muted truncate">{f.path}</div>
          </div>
          <span className="text-[11px] text-muted shrink-0 whitespace-nowrap">
            {isDir ? `${i18nT('components.filePickerMenu.folder_kind')} · ${formatAge(f.mtime)}` : `${formatSize(f.size)} · ${formatAge(f.mtime)}`}
          </span>
          {onFileOpen && !isDir && (
            <button
              type="button"
              aria-label={i18nT('components.filePickerMenu.open_in_viewer')}
              tabIndex={-1}
              className="shrink-0 p-1 rounded hover:bg-bg-hover text-muted hover:text-text cursor-pointer bg-transparent border-none"
              title={i18nT('components.filePickerMenu.open_in_viewer')}
              onMouseDown={e => { e.preventDefault(); e.stopPropagation(); onFileOpen(f.path); onClose() }}
            >
              <Eye size={16} />
            </button>
          )}
        </div>
        )
      })}
    </div>,
    document.body
  )
}
