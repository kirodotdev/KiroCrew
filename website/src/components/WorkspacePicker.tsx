import { useState, useEffect, useRef, useCallback, RefObject } from 'react'
import { createPortal } from 'react-dom'
import { FolderOpen, ChevronRight, ChevronLeft } from 'lucide-react'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  anchorRef: RefObject<HTMLElement | null>
  onCreated: (name: string) => void
}

export default function WorkspacePicker({ open, onOpenChange, anchorRef, onCreated }: Props) {
  const [input, setInput] = useState('')
  const [browsePath, setBrowsePath] = useState('')
  const [browseParent, setBrowseParent] = useState('')
  const [browseDirs, setBrowseDirs] = useState<{ name: string; path: string }[]>([])
  const [selectedDir, setSelectedDir] = useState('')
  const [wsName, setWsName] = useState('')
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const btnRef = anchorRef
  const dropRef = useRef<HTMLDivElement>(null)

  const browse = useCallback((path?: string) => {
    api.browseDirs(path).then(d => {
      setBrowsePath(d.path)
      setBrowseParent(d.parent)
      setBrowseDirs(d.dirs)
      setInput(d.path)
    }).catch(() => {})
  }, [])

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
    if (!name) { setError('Name required'); return }
    setCreating(true); setError('')
    try {
      const res = await api.createWorkspace({ name, dir: selectedDir }) as { ok?: boolean; error?: string }
      if (res.error) { setError(res.error); setCreating(false); return }
      onCreated(name)
      onOpenChange(false); setSelectedDir(''); setWsName('')
    } catch { setError('Failed to create workspace') }
    setCreating(false)
  }

  if (!open || !btnRef.current) return null

  const q = input.toLowerCase()
  const filteredBrowse = q && q !== browsePath.toLowerCase() ? browseDirs.filter(d => d.name.toLowerCase().includes(q.split('/').pop() || '') || d.path.toLowerCase().includes(q)) : browseDirs

  return createPortal(
        <div ref={dropRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg w-[400px] max-h-[460px] flex flex-col overflow-hidden animate-slide-up" style={(() => { const r = btnRef.current!.getBoundingClientRect(); const maxH = window.innerHeight - r.bottom - 8; return { top: r.bottom + 4, left: Math.max(8, r.right - 400), maxHeight: Math.max(200, maxH) } })()}>
          {selectedDir ? (
            <div className="p-3 flex flex-col gap-2">
              <div className="text-[12px] text-muted font-medium uppercase tracking-wider">{i18nT('components.workspacePicker.create_workspace')}</div>
              <div className="text-[13px] font-mono text-text truncate bg-bg-elevated rounded px-2 py-1.5 border border-border">{selectedDir}</div>
              <input autoFocus type="text" aria-label={i18nT('components.workspacePicker.workspace_name')} placeholder={i18nT('components.workspacePicker.workspace_name_2')} value={wsName} onChange={e => { setWsName(e.target.value); setError('') }} onKeyDown={e => { if (e.key === 'Enter') create(); if (e.key === 'Escape') { setSelectedDir(''); setWsName('') } }} className="bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus:border-accent" />
              {error && <div className="text-[11px] text-red-400">{error}</div>}
              <div className="flex gap-2 justify-end">
                <button onClick={() => { setSelectedDir(''); setWsName('') }} className="px-3 py-1.5 text-[12px] text-muted hover:text-text rounded">{i18nT('components.workspacePicker.back')}</button>
                <button onClick={create} disabled={creating} className="px-3 py-1.5 text-[12px] bg-accent text-accent-fg rounded hover:bg-accent/80 disabled:opacity-50">{creating ? 'Creating…' : 'Create'}</button>
              </div>
            </div>
          ) : (
            <>
              <div className="p-2 border-b border-border flex gap-1 items-center">
                {browseParent && browseParent !== browsePath && (
                  <button onClick={() => browse(browseParent)} className="p-1 text-muted hover:text-text rounded hover:bg-bg-hover shrink-0" title={i18nT('components.workspacePicker.back')} aria-label={i18nT('components.workspacePicker.back')}><ChevronLeft size={16} /></button>
                )}
                <input autoFocus type="text" aria-label={i18nT('components.workspacePicker.project_directory_path')} placeholder={i18nT('components.workspacePicker.path_to_project')} value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && input.trim()) selectDir(input.trim()); if (e.key === 'Escape') onOpenChange(false) }} className="flex-1 bg-bg-elevated border border-border rounded px-2 py-1.5 text-[13px] font-mono text-text placeholder:text-muted focus:outline-none focus:border-accent" />
                <button onClick={() => selectDir(input.trim() || browsePath)} className="px-2 py-1 text-[11px] bg-accent/20 text-accent rounded hover:bg-accent/30 shrink-0">{i18nT('components.workspacePicker.select')}</button>
              </div>
              <div className="overflow-y-auto flex-1 min-h-0">
                {filteredBrowse.length === 0 && <div className="px-3 py-4 text-[12px] text-muted text-center">{i18nT('components.workspacePicker.no_subdirectories')}</div>}
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
