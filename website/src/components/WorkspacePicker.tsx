import { useState, useEffect, useRef, useCallback, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { FolderOpen, ChevronRight, ChevronLeft } from 'lucide-react'
import { api } from '../api/client'
import ErrorNotice from './ErrorNotice'

import { i18nT } from '../i18n/t'
import { useImeGuard } from '../hooks/useImeGuard'
import { LISTING_FAILURE_KEYS, searchErrorCause, type SearchErrorCause } from '../lib/searchErrorCause'
interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  anchorRef: RefObject<HTMLElement | null>
  onCreated: (name: string) => void
}

export default function WorkspacePicker({ open, onOpenChange, anchorRef, onCreated }: Props) {
  // One instance covers both inputs; the binding's focus/blur reset makes sharing safe.
  const ime = useImeGuard()
  const [input, setInput] = useState('')
  const [browsePath, setBrowsePath] = useState('')
  const [browseParent, setBrowseParent] = useState('')
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([])
  const [selectedDir, setSelectedDir] = useState('')
  const [wsName, setWsName] = useState('')
  /** Client-side hint ("name is required"): not a failure, so not an ErrorNotice. */
  const [error, setError] = useState('')
  /** A request failure, from the backend or the transport. */
  const [requestError, setRequestError] = useState('')
  const [requestErrorCause, setRequestErrorCause] = useState<SearchErrorCause | null>(null)
  const [failedBrowsePath, setFailedBrowsePath] = useState<string | undefined>(undefined)
  const [creating, setCreating] = useState(false)
  // A listing request is unsettled for the CURRENT ticket. Set by every `browse`, cleared only by
  // the settlement that still holds the ticket -- a superseded request's landing must not read
  // as the live one having settled. Renders as a disabled "Retrying…" in place of Retry: without
  // it the failure notice and an enabled Retry sat unchanged for the whole re-ask, and a second
  // press took a fresh ticket and restarted the wait. The sites that retire a ticket without a
  // new request (Select, close) leave it as is: each also clears the notice the control sits in,
  // and the next `browse` resets it, so a stale `true` has nothing to disable.
  const [browsing, setBrowsing] = useState(false)
  const btnRef = anchorRef
  const dropRef = useRef<HTMLDivElement>(null)
  // Every listing request takes the next ticket. Only the latest settlement may
  // replace the visible rows or the failure notice.
  const listingSeq = useRef(0)
  const clearRequestFailure = useCallback(() => {
    setRequestError('')
    setRequestErrorCause(null)
    setFailedBrowsePath(undefined)
  }, [])

  const browse = useCallback((path?: string) => {
    const ticket = ++listingSeq.current
    setBrowsing(true)
    api.browseDirs(path).then(d => {
      if (ticket !== listingSeq.current) return
      setBrowsing(false)
      clearRequestFailure()
      setBrowsePath(d.path)
      setBrowseParent(d.parent)
      setBrowseDirs(d.dirs)
      setInput(d.path)
    }).catch((err: unknown) => {
      if (ticket !== listingSeq.current) return
      setBrowsing(false)
      const cause = searchErrorCause(err)
      setRequestError(i18nT(LISTING_FAILURE_KEYS[cause]))
      setRequestErrorCause(cause)
      setFailedBrowsePath(path)
    })
  }, [clearRequestFailure])

  useEffect(() => {
    if (!open) return
    browse()
  }, [open, browse])

  useEffect(() => {
    if (!open) return
    const timer = setTimeout(() => {
      const handler = (e: MouseEvent) => {
        if (dropRef.current && !dropRef.current.contains(e.target as Node) &&
            btnRef.current && !btnRef.current.contains(e.target as Node)) {
          listingSeq.current++; onOpenChange(false); setSelectedDir(''); setWsName(''); setError(''); clearRequestFailure()
        }
      }
      document.addEventListener('mousedown', handler)
      cleanup = () => document.removeEventListener('mousedown', handler)
    }, 0)
    let cleanup = () => {}
    return () => { clearTimeout(timer); cleanup() }
    // `btnRef` is a stable ref object and the handler reads `.current` fresh;
    // `onOpenChange` is a parent callback that may not be memoized, so we only
    // (re)attach the click-outside listener on `open` transitions to avoid
    // tearing it down on every parent re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const selectDir = (dir: string) => {
    listingSeq.current++
    setSelectedDir(dir)
    setWsName(dir.split('/').filter(Boolean).pop() || '')
    setInput(dir)
    setError('')
    clearRequestFailure()
  }

  const create = async () => {
    const name = wsName.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-')
    if (!name) { setError(i18nT('components.workspacePicker.name_required')); return }
    setCreating(true); setError(''); clearRequestFailure()
    try {
      const res = await api.createWorkspace({ name, dir: selectedDir }) as { ok?: boolean; error?: string }
      if (res.error) { setRequestError(res.error); setCreating(false); return }
      onCreated(name)
      onOpenChange(false); setSelectedDir(''); setWsName('')
    } catch { setRequestError(i18nT('components.workspacePicker.failed_to_create_workspace')) }
    setCreating(false)
  }

  if (!open || !btnRef.current) return null

  const q = input.toLowerCase()
  const filteredBrowse = q && q !== browsePath.toLowerCase() ? browseDirs.filter(d => d.name.toLowerCase().includes(q.split('/').pop() || '') || d.path.toLowerCase().includes(q)) : browseDirs
  const canRetryBrowse = requestErrorCause === 'timed_out' || requestErrorCause === 'failed'

  return createPortal(
        <div ref={dropRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg w-[400px] max-h-[460px] flex flex-col overflow-hidden animate-slide-up" style={(() => { const r = btnRef.current!.getBoundingClientRect(); const maxH = window.innerHeight - r.bottom - 8; return { top: r.bottom + 4, left: Math.max(8, r.right - 400), maxHeight: Math.max(200, maxH) } })()}>
          {selectedDir ? (
            <div className="p-3 flex flex-col gap-2">
              <div className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.workspacePicker.create_workspace')}</div>
              <div className="text-[13px] font-mono text-text truncate bg-bg-elevated rounded px-2 py-1.5 border border-border">{selectedDir}</div>
              <input autoFocus type="text" aria-label={i18nT('components.workspacePicker.workspace_name')} placeholder={i18nT('components.workspacePicker.workspace_name_2')} value={wsName} onChange={e => { setWsName(e.target.value); setError(''); clearRequestFailure() }} {...ime.bindEnter({ onEnter: create, onEscape: () => { setSelectedDir(''); setWsName(''); clearRequestFailure() } })} className="bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-hidden focus-visible:border-accent" />
              {error && <div className="text-[11px] text-danger">{error}</div>}
              {/* No hand-off: the workspace name in `wsName` and the chosen directory are unsaved until Create. */}
              <ErrorNotice message={requestError} />
              <div className="flex gap-2 justify-end">
                <button onClick={() => { setSelectedDir(''); setWsName(''); clearRequestFailure() }} className="px-3 py-1.5 text-[12px] text-muted hover:text-text rounded">{i18nT('components.workspacePicker.back')}</button>
                <button onClick={create} disabled={creating} className="px-3 py-1.5 text-[12px] bg-accent text-accent-fg rounded hover:bg-accent/80 disabled:opacity-50">{creating ? i18nT('components.workspacePicker.creating') : i18nT('components.workspacePicker.create')}</button>
              </div>
            </div>
          ) : (
            <>
              <div className="p-2 border-b border-border flex gap-1 items-center">
                {browseParent && browseParent !== browsePath && (
                  <button onClick={() => browse(browseParent)} className="p-1 text-muted hover:text-text rounded hover:bg-bg-hover shrink-0" title={i18nT('components.workspacePicker.back')} aria-label={i18nT('components.workspacePicker.back')}><ChevronLeft size={16} /></button>
                )}
                <input autoFocus type="text" aria-label={i18nT('components.workspacePicker.project_directory_path')} placeholder={i18nT('components.workspacePicker.path_to_project')} value={input} onChange={e => setInput(e.target.value)} {...ime.bindEnter({ onEnter: () => { if (input.trim()) selectDir(input.trim()) }, onEscape: () => { listingSeq.current++; clearRequestFailure(); onOpenChange(false) } })} className="flex-1 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-hidden focus-visible:border-accent" />
                <button onClick={() => selectDir(input.trim() || browsePath)} className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 shrink-0">{i18nT('components.workspacePicker.select')}</button>
              </div>
              {/* No hand-off: the path typed into `input` and the browse position
                  (`browsePath`) are unsaved until Select, and a hand-off would navigate
                  away from both. The remedy is local instead: Retry re-runs the listing
                  that failed, since the surface has no Refresh of its own. */}
              {requestError && (
                <div className="flex items-center gap-2 pr-2" aria-busy={browsing || undefined}>
                  <ErrorNotice className="flex-1 min-w-0" message={requestError} />
                  {canRetryBrowse && (
                    // Same shape as Create below: disabled and relabelled while its request is
                    // in flight, so the wait is visible and a second press is impossible.
                    <button
                      type="button"
                      onClick={() => browse(failedBrowsePath)}
                      disabled={browsing}
                      className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 shrink-0 disabled:opacity-50"
                    >
                      {browsing ? i18nT('components.workspacePicker.retrying') : i18nT('components.workspacePicker.retry')}
                    </button>
                  )}
                </div>
              )}
              <div className="overflow-y-auto flex-1 min-h-0">
                {!requestError && filteredBrowse.length === 0 && <div className="px-3 py-4 text-[12px] text-muted text-center">{i18nT('components.workspacePicker.no_subdirectories')}</div>}
                {filteredBrowse.map(d => (
                  <button key={d.path} className="w-full text-left px-3 py-1.5 flex items-center gap-2 cursor-pointer hover:bg-bg-hover transition-colors" onClick={() => browse(d.path)}>
                    <FolderOpen size={12} className="text-accent shrink-0" />
                    <span className="text-[13px] font-mono text-text truncate">{d.name}</span>
                    <ChevronRight size={12} className="text-muted ml-auto shrink-0" />
                  </button>
                ))}
              </div>
            </>
          )}
        </div>,
        document.body
      )
}
