import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { FileText, Eye } from 'lucide-react'
import { api } from '../api/client'
import { useListKeyboardNav } from '../hooks/useListKeyboardNav'
import { menuGeometry, bottomUpOrder } from '../lib/pickerMenu'

import { i18nT } from '../i18n/t'
interface FileResult {
  path: string
  name: string
  size: number
  mtime: number
}

interface FileSearchResponse {
  results?: FileResult[]
  root?: string
}

interface Props {
  query: string
  anchorRef: React.RefObject<HTMLElement | null>
  open: boolean
  onSelect: (info: { path: string; relativePath: string }) => void
  onClose: () => void
  onFileOpen?: (path: string) => void
  project?: string
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return bytes + 'B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + 'KB'
  return (bytes / (1024 * 1024)).toFixed(1) + 'MB'
}

function formatAge(mtime: number): string {
  const diff = Date.now() / 1000 - mtime
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago'
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago'
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + 'd ago'
  return new Date(mtime * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function makeRelative(path: string, root: string): string {
  if (!root) return path
  const r = root.endsWith('/') ? root : root + '/'
  return path.startsWith(r) ? path.slice(r.length) : path
}

export default function FilePickerMenu({ query, anchorRef, open, onSelect, onClose, onFileOpen, project }: Props) {
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
  const { data, isFetching } = useQuery<FileSearchResponse>({
    queryKey: ['file-search', debounced, project],
    queryFn: ({ signal }) => api.fileSearch(debounced, project, signal),
    enabled: open && debounced.length >= 2,
    placeholderData: prev => prev,
    staleTime: 10_000,
  })
  rootRef.current = data?.root || ''

  // Order bottom-up (shared helper) when the menu opens above. Gate on the LIVE
  // `query` length (not the debounced one) so results clear immediately when the
  // user drops below 2 chars; `data` is keyed on `debounced`, so it lags by up to
  // one debounce tick — the intended debounce behavior.
  const { ordered: results, initialIndex } = useMemo(() => {
    const raw = (open && query.length >= 2 ? data?.results : []) || []
    const above = anchorRef.current ? menuGeometry(anchorRef.current, raw.length, 48).above : false
    return bottomUpOrder(raw, above)
  }, [data, open, query, anchorRef])

  // Open the highlighted file in the viewer (the eye/preview action) instead of
  // inserting an @-mention. Shared by the Cmd/Ctrl+Enter path (via onChoose's
  // withModifier flag) and the Alt+Enter path (via onAltEnter). Returns true so
  // the hook knows the default choose was superseded.
  const openInViewer = useCallback((idx: number): boolean => {
    const f = resultsRef.current[idx]
    if (f && onFileOpenRef.current) { onFileOpenRef.current(f.path); onClose(); return true }
    return false
  }, [onClose])

  // Enter inserts the @-mention. Cmd/Ctrl+Enter preserves the earlier
  // binding — open in the viewer — now that the shared useListKeyboardNav hook
  // threads the modifier state through onChoose's 2nd arg (withModifier).
  const choose = useCallback((idx: number, withModifier = false) => {
    const r = resultsRef.current
    const eff = idx >= r.length ? 0 : idx
    const f = r[eff]
    if (!f) return
    if (withModifier && openInViewer(eff)) return
    onSelect({ path: f.path, relativePath: makeRelative(f.path, rootRef.current) })
  }, [onSelect, openInViewer])

  // Shared Arrow/Enter/Tab/Escape + scroll-into-view (see useListKeyboardNav).
  const { selected, setSelected, selectedRef, itemRefs } = useListKeyboardNav({
    open,
    count: results.length,
    onChoose: choose,
    onClose,
    onAltEnter: openInViewer,
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

  const { top, left, width, maxHeight } = menuGeometry(anchorRef.current, results.length, 48)

  const empty = query.length < 2
    ? <div className="px-3 py-3 text-[12px] text-muted">{i18nT('components.filePickerMenu.type_2_chars_to_search_files')}</div>
    : isFetching
    ? <div className="px-3 py-3 text-[12px] text-muted">{i18nT('components.filePickerMenu.searching')}</div>
    : <div className="px-3 py-3 text-[12px] text-muted">{i18nT('components.filePickerMenu.no_matches')}</div>

  return createPortal(
    <div
      className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg overflow-y-auto py-1 animate-slide-up"
      role="listbox"
      style={{ top, left, width: Math.min(width, 420), maxHeight }}
    >
      {results.length === 0 ? empty : results.map((f, i) => (
        <div
          role="option"
          aria-selected={i === selected}
          tabIndex={-1}
          key={f.path}
          ref={el => { itemRefs.current[i] = el }}
          className={`w-full text-left px-3 py-2 flex items-center gap-3 cursor-pointer transition-colors ${i === selected ? 'bg-accent-subtle text-text' : 'text-muted hover:bg-bg-hover hover:text-text'}`}
          title={f.path}
          onMouseEnter={() => setSelected(i)}
          onMouseDown={e => { e.preventDefault(); onSelect({ path: f.path, relativePath: makeRelative(f.path, rootRef.current) }) }}
        >
          <FileText size={14} className="shrink-0 lucide-inline" />
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-mono font-semibold truncate">{f.name}</div>
            <div className="text-[11px] text-muted truncate">{f.path}</div>
          </div>
          <span className="text-[11px] text-muted shrink-0 whitespace-nowrap">{formatSize(f.size)} · {formatAge(f.mtime)}</span>
          {onFileOpen && (
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
      ))}
    </div>,
    document.body
  )
}
