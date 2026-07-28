import { useState, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FolderTree, Folder, File, GitBranch } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { fileExplorerApi } from './api'
import { basename, parentChain } from './utils'
import { useDebouncedValue } from './hooks'
import type { GitInfo, TreeEntry } from './types'

import { i18nT } from '../../i18n/t'
interface PathBarProps {
  rootPath: string
  gitInfo: GitInfo | null
  onChangeRoot: (path: string) => void
  onNavigate: (path: string) => void
}

export default function PathBar({ rootPath, gitInfo, onChangeRoot, onNavigate }: PathBarProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(rootPath)
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const inputRef = useRef<HTMLInputElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => { setDraft(rootPath); setEditing(false); setOpen(false); setActiveIdx(-1) }, [rootPath])
  useEffect(() => { if (editing) inputRef.current?.focus() }, [editing])

  const debouncedDraft = useDebouncedValue(draft, 150)

  const { data: suggestions = [] } = useQuery({
    queryKey: ['file-explorer', 'complete', debouncedDraft],
    queryFn: () => fileExplorerApi.complete(debouncedDraft, 'dir', 30).then(r => r.entries || []),
    enabled: open && debouncedDraft.length > 0,
    staleTime: 10_000,
  })

  useEffect(() => { setActiveIdx(-1) }, [suggestions])

  useEffect(() => {
    if (!editing) return
    const onDocClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setEditing(false); setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [editing])

  const accept = (entry: TreeEntry) => {
    setDraft(entry.path); setOpen(false); setEditing(false)
    onChangeRoot(entry.path.replace(/\/$/, ''))
  }

  const commit = () => {
    setOpen(false); setEditing(false)
    if (draft.trim() && draft.trim() !== rootPath) onNavigate(draft.trim())
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { setOpen(true); return }
    if (open && e.key === 'ArrowDown') { e.preventDefault(); setActiveIdx((i) => suggestions.length === 0 ? -1 : (i + 1) % suggestions.length) }
    else if (open && e.key === 'ArrowUp') { e.preventDefault(); setActiveIdx((i) => suggestions.length === 0 ? -1 : (i - 1 + suggestions.length) % suggestions.length) }
    else if (open && e.key === 'Enter') { e.preventDefault(); if (activeIdx >= 0 && suggestions[activeIdx]) accept(suggestions[activeIdx]); else commit() }
    else if (!open && e.key === 'Enter') { e.preventDefault(); commit() }
    else if (e.key === 'Escape') { setDraft(rootPath); setEditing(false); setOpen(false) }
    else if (e.key === 'Tab' && open && suggestions.length > 0) { e.preventDefault(); setDraft(suggestions[activeIdx >= 0 ? activeIdx : 0].path) }
  }

  const segs = parentChain(rootPath)

  return (
    <div className="mc-fe-pathbar" ref={containerRef}>
      <div className="mc-fe-pathbar-row">
        <FolderTree size={13} style={{ color: 'var(--accent)', flexShrink: 0 }} />
        {editing ? (
          <div className="mc-fe-pathbar-edit">
            <input
              ref={inputRef}
              className="mc-fe-pathbar-input"
              type="text"
              aria-label={i18nT('apps.fileExplorer.pathBar.folder_path')}
              value={draft}
              onChange={(e) => { setDraft(e.target.value); setOpen(true) }}
              onFocus={() => setOpen(true)}
              onKeyDown={onKeyDown}
              placeholder={i18nT('apps.fileExplorer.pathBar.path_to_folder')}
              spellCheck={false}
              autoComplete="off"
            />
            {open && suggestions.length > 0 && (
              <div className="mc-fe-ac">
                {suggestions.map((s, i) => (
                  <div
                    key={s.path}
                    className={'mc-fe-ac-row' + (i === activeIdx ? ' is-active' : '')}
                    onMouseDown={(e) => { e.preventDefault(); accept(s) }}
                    onMouseEnter={() => setActiveIdx(i)}
                    role="option"
                    tabIndex={-1}
                    aria-selected={i === activeIdx}
                  >
                    {s.type === 'dir' ? <Folder size={12} style={{ marginRight: 6, color: 'var(--accent)', opacity: 0.85 }} /> : <File size={12} style={{ marginRight: 6, color: 'var(--accent)', opacity: 0.85 }} />}
                    <span className="mc-fe-ac-name">{s.name}</span>
                    <span className="mc-fe-ac-path">{s.path}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          <Clickable className="mc-fe-breadcrumbs" onClick={() => { setDraft(rootPath); setEditing(true) }} title={i18nT('apps.fileExplorer.pathBar.click_to_edit_path')}>
            {segs.map((s, i) => (
              <span key={`seg${i}`}>
                {i > 0 && <span className="mc-fe-bc-sep">/</span>}
                <span className="mc-fe-bc" onClick={(e) => { e.stopPropagation(); onChangeRoot(s) }} role="button" tabIndex={0} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); onChangeRoot(s) } }}>
                  {i === 0 ? '/' : basename(s) || s}
                </span>
              </span>
            ))}
          </Clickable>
        )}
        {gitInfo && gitInfo.branch && (
          <span className="mc-fe-branch">
            <GitBranch size={11} /> {gitInfo.branch}
            {Object.keys(gitInfo.statuses || {}).length > 0 && (
              <span className="mc-fe-branch-count"> {Object.keys(gitInfo.statuses).length}</span>
            )}
          </span>
        )}
      </div>
    </div>
  )
}
