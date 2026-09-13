import { Btn, SMALL_BTN } from './ui'
import { useState, useEffect, useRef, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { FolderOpen, ChevronRight, ChevronLeft, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import { useBrowseDirs } from './useBrowseDirs'

import { i18nT } from '../i18n/t'
import { useImeGuard } from '../hooks/useImeGuard'
import ErrorNotice from './ErrorNotice'
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
  const [retrying, setRetrying] = useState(false)
  const [browseParent, setBrowseParent] = useState('')
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([])
  const [selectedDir, setSelectedDir] = useState('')
  const [wsName, setWsName] = useState('')
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const btnRef = anchorRef
  const dropRef = useRef<HTMLDivElement>(null)
  const pathRef = useRef<HTMLInputElement>(null)
  const retryRef = useRef<HTMLButtonElement | null>(null)
  // `disabled` blurs the pressed button to <body>, and only a RECOVERY unmounts it, so a
  // repeated failure has to hand focus back once the re-enabling render commits.
  const refocusRetry = useRef(false)
  useEffect(() => {
    if (retrying || !refocusRetry.current) return
    refocusRetry.current = false
    retryRef.current?.focus()
  }, [retrying])

  const { listError, browse, retry } = useBrowseDirs(open, (
    d: { path: string; parent: string; dirs: { name: string; path: string }[] },
    preserveInput: boolean,
  ) => {
    setBrowsePath(d.path)
    setBrowseParent(d.parent)
    setBrowseDirs(d.dirs)
    // Honour the flag rather than always overwriting: a retry of the path already shown
    // must not replace what the user has typed since.
    if (!preserveInput) setInput(d.path)
  })

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
          onOpenChange(false); setSelectedDir(''); setWsName(''); setError('')
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
    setSelectedDir(dir)
    setWsName(dir.split('/').filter(Boolean).pop() || '')
    setInput(dir)
    setError('')
  }

  const create = async () => {
    const name = wsName.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-')
    if (!name) { setError(i18nT('components.workspacePicker.name_required')); return }
    setCreating(true); setError('')
    try {
      const res = await api.createWorkspace({ name, dir: selectedDir }) as { ok?: boolean; error?: string }
      if (res.error) { setError(res.error); setCreating(false); return }
      onCreated(name)
      onOpenChange(false); setSelectedDir(''); setWsName('')
    } catch { setError(i18nT('components.workspacePicker.failed_to_create_workspace')) }
    setCreating(false)
  }

  if (!open || !btnRef.current) return null

  const q = input.toLowerCase()
  // Cleared, unlike the twins that re-read the SAME directory: a failed drill-down here would
  // leave the previous directory's rows sitting under the new path's header.
  // Held while its retry is in flight: `refetch` on a key that never succeeded clears the error,
  // so gating on the error alone unmounts the notice, its button and the cleared rows mid-read.
  const listBusy = listError !== false || retrying
  const visibleDirs = listBusy ? [] : browseDirs
  // Re-asking a refusal returns the same answer, so its Retry would spin and fail
  // identically -- the @-menu withholds it on the same rule.
  const listRecoverable = listError === 'timed_out' || listError === 'failed' || retrying
  const filteredBrowse = q && q !== browsePath.toLowerCase() ? visibleDirs.filter(d => d.name.toLowerCase().includes(q.split('/').pop() || '') || d.path.toLowerCase().includes(q)) : visibleDirs

  return createPortal(
        <div ref={dropRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg w-[400px] max-h-[460px] flex flex-col overflow-hidden animate-slide-up" style={(() => { const r = btnRef.current!.getBoundingClientRect(); const maxH = window.innerHeight - r.bottom - 8; return { top: r.bottom + 4, left: Math.max(8, r.right - 400), maxHeight: Math.max(200, maxH) } })()}>
          {selectedDir ? (
            <div className="p-3 flex flex-col gap-2">
              <div className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.workspacePicker.create_workspace')}</div>
              <div className="text-[13px] font-mono text-text truncate bg-bg-elevated rounded px-2 py-1.5 border border-border">{selectedDir}</div>
              <input autoFocus type="text" aria-label={i18nT('components.workspacePicker.workspace_name')} placeholder={i18nT('components.workspacePicker.workspace_name_2')} value={wsName} onChange={e => { setWsName(e.target.value); setError('') }} {...ime.bindEnter({ onEnter: create, onEscape: () => { setSelectedDir(''); setWsName('') } })} className="bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus-visible:border-accent" />
              {error && <div className="text-[11px] text-red-400">{error}</div>}
              <div className="flex gap-2 justify-end">
                <button onClick={() => { setSelectedDir(''); setWsName('') }} className="px-3 py-1.5 text-[12px] text-muted hover:text-text rounded">{i18nT('components.workspacePicker.back')}</button>
                <button onClick={create} disabled={creating} className="px-3 py-1.5 text-[12px] bg-accent text-accent-fg rounded hover:bg-accent/80 disabled:opacity-50">{creating ? i18nT('components.workspacePicker.creating') : i18nT('components.workspacePicker.create')}</button>
              </div>
            </div>
          ) : (
            <>
              <div className="p-2 border-b border-border flex gap-1 items-center">
                {browseParent && browseParent !== browsePath && (
                  <button onClick={() => browse(browseParent)} className="p-1 text-muted hover:text-text rounded hover:bg-bg-hover shrink-0" title={i18nT('components.workspacePicker.back')} aria-label={i18nT('components.workspacePicker.back')}><ChevronLeft size={16} /></button>
                )}
                <input autoFocus ref={pathRef} type="text" aria-label={i18nT('components.workspacePicker.project_directory_path')} placeholder={i18nT('components.workspacePicker.path_to_project')} value={input} onChange={e => setInput(e.target.value)} {...ime.bindEnter({ onEnter: () => { if (input.trim()) selectDir(input.trim()) }, onEscape: () => onOpenChange(false) })} className="flex-1 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus-visible:border-accent" />
                <button onClick={() => selectDir(input.trim() || browsePath)} className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 shrink-0">{i18nT('components.workspacePicker.select')}</button>
              </div>
              <div className="overflow-y-auto flex-1 min-h-0">
                {/* No hand-off: the workspace name and path typed into this picker are
                    unsaved, so a navigation would discard both of them. */}
                {listBusy && (
                  <div className="px-3 py-4 flex items-center gap-2">
                    <ErrorNotice variant="inline" message={i18nT(listError === 'timed_out'
                      ? 'pages.chat.folderPanel.listing_timed_out'
                      : 'pages.chat.folderPanel.unable_to_list_folder')} />
{listRecoverable && <Btn
  type="button"
  ref={retryRef}
  // Same pair its ProjectPicker twins carry: preventDefault on mousedown keeps the path
  // field focused, and a successful retry unmounts this button so focus is handed back.
  onMouseDown={e => e.preventDefault()}
  onClick={() => {
    setRetrying(true)
    void retry().then(ok => {
      setRetrying(false)
      if (ok) pathRef.current?.focus()
      // A repeated failure leaves the button mounted, and it was blurred to <body> when it
      // disabled, so without this a keyboard user blind-Tabs back on every attempt.
      else refocusRetry.current = true
    })
  }}
  disabled={retrying}
  aria-busy={retrying}
  aria-label={`${i18nT('components.workspacePicker.retry')}: `
    + i18nT(listError === 'timed_out'
      ? 'pages.chat.folderPanel.listing_timed_out'
      : 'pages.chat.folderPanel.unable_to_list_folder')}
  className={SMALL_BTN}
>
  <RefreshCw size={10} className={retrying ? 'animate-spin inline-block mr-1' : 'invisible inline-block mr-1'} />
  {i18nT('components.workspacePicker.retry')}
</Btn>}
                  </div>
                )}
                {!listBusy && filteredBrowse.length === 0 && <div className="px-3 py-4 text-[12px] text-muted text-center">{i18nT('components.workspacePicker.no_subdirectories')}</div>}
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
