import { useState, useEffect, useRef, useCallback } from 'react'
import { Folder, File, Image, X, Plus } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { IMAGE_EXTS } from './constants'
import { extOf, basename } from './utils'
import type { FolderTab, FileTab } from './types'

import { i18nT } from '../../i18n/t'
interface TabStripProps {
  folderTabs: FolderTab[]
  fileTabs: FileTab[]
  activeFolderId: string | null
  activeFileId: string | null
  onActivateFolder: (id: string) => void
  onActivateFile: (id: string) => void
  onCloseFolder: (id: string) => void
  onCloseFile: (id: string) => void
  onNewFolder: () => void
  onRenameFolder: (id: string, label: string) => void
}

export default function TabStrip({ folderTabs, fileTabs, activeFolderId, activeFileId, onActivateFolder, onActivateFile, onCloseFolder, onCloseFile, onNewFolder, onRenameFolder }: TabStripProps) {
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const [showFadeLeft, setShowFadeLeft] = useState(false)
  const [showFadeRight, setShowFadeRight] = useState(false)

  const updateFades = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    setShowFadeLeft(el.scrollLeft > 4)
    setShowFadeRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 4)
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    updateFades()
    el.addEventListener('scroll', updateFades, { passive: true })
    const resObs = new ResizeObserver(updateFades)
    resObs.observe(el)
    return () => { el.removeEventListener('scroll', updateFades); resObs.disconnect() }
  }, [updateFades])

  return (
    <div className="mc-fe-tabstrip-outer">
      {showFadeLeft && <div className="mc-fe-tabstrip-fade mc-fe-fade-left" />}
      {showFadeRight && <div className="mc-fe-tabstrip-fade mc-fe-fade-right" />}
      <div className="mc-fe-tabs" ref={scrollRef}>
        {folderTabs.map((t) => (
          <div
            key={t.id}
            className={'mc-fe-tab mc-fe-tab-folder' + (t.id === activeFolderId && !activeFileId ? ' is-active' : '') + (t.id === activeFolderId ? ' is-current-folder' : '')}
            onClick={() => onActivateFolder(t.id)}
            onDoubleClick={() => { setRenameId(t.id); setRenameDraft(t.label || basename(t.rootPath)) }}
            title={t.rootPath}
            role="tab"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onActivateFolder(t.id) }}
          >
            <Folder size={11} style={{ opacity: 0.7, marginRight: 5, flexShrink: 0 }} />
            {renameId === t.id ? (
              <input
                className="mc-fe-tab-rename"
                aria-label={i18nT('apps.fileExplorer.tabStrip.rename_workspace_tab')}
                autoFocus
                value={renameDraft}
                onChange={(e) => setRenameDraft(e.target.value)}
                onClick={(e) => e.stopPropagation()}
                onBlur={() => { onRenameFolder(t.id, renameDraft.trim()); setRenameId(null) }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { onRenameFolder(t.id, renameDraft.trim()); setRenameId(null) }
                  if (e.key === 'Escape') setRenameId(null)
                }}
              />
            ) : (
              <span className="mc-fe-tab-label">{t.label || basename(t.rootPath) || '/'}</span>
            )}
            <Clickable className="mc-fe-tab-close" onClick={(e) => { e?.stopPropagation(); onCloseFolder(t.id) }} title={i18nT('apps.fileExplorer.tabStrip.close_workspace_tab')} aria-label={i18nT('apps.fileExplorer.tabStrip.close_workspace_tab')}>
              <X size={11} />
            </Clickable>
          </div>
        ))}
        {fileTabs.length > 0 && <div className="mc-fe-tab-sep" />}
        {fileTabs.map((ft) => (
          <div
            key={ft.id}
            className={'mc-fe-tab mc-fe-tab-file' + (ft.id === activeFileId ? ' is-active' : '')}
            onClick={() => onActivateFile(ft.id)}
            title={ft.path}
            role="tab"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onActivateFile(ft.id) }}
          >
            {IMAGE_EXTS.has(extOf(ft.path)) ? <Image size={11} style={{ opacity: 0.7, marginRight: 5, flexShrink: 0 }} /> : <File size={11} style={{ opacity: 0.7, marginRight: 5, flexShrink: 0 }} />}
            <span className="mc-fe-tab-label">{basename(ft.path)}</span>
            <Clickable className="mc-fe-tab-close" onClick={(e) => { e?.stopPropagation(); onCloseFile(ft.id) }} aria-label={i18nT('apps.fileExplorer.tabStrip.close_file_tab')}>
              <X size={11} />
            </Clickable>
          </div>
        ))}
        <button className="mc-fe-tab-new" onClick={onNewFolder} title={i18nT('apps.fileExplorer.tabStrip.new_workspace_tab_ctrl_cmd_t')} aria-label={i18nT('apps.fileExplorer.tabStrip.new_workspace_tab')}>
          <Plus size={13} />
        </button>
      </div>
    </div>
  )
}
