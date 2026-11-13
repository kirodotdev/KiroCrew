import { useCallback, useEffect, useRef, useState } from 'react'
import { Folder, File, Image, X, Plus } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { useWheelToHorizontal } from '../../hooks/useWheelToHorizontal'
import { IMAGE_EXTS } from './constants'
import { extOf, basename } from './utils'
import type { FolderTab, FileTab } from './types'

import { i18nT } from '../../i18n/t'
import { useImeGuard } from '../../hooks/useImeGuard'
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
  // Only one rename input renders at a time, so a single instance is safe.
  const ime = useImeGuard()
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  // The tab the rename was opened from. Closing the rename unmounts the focused
  // input and a browser then drops focus to document.body, so Enter and Escape
  // hand focus back to the tab (the pattern in MeetingTitle). A pointer blur
  // saves but moves nothing: redirecting focus off the element the user clicked
  // is the context change WCAG 3.2.2 forbids.
  const renameTriggerRef = useRef<HTMLDivElement | null>(null)
  // restore=true for a keyboard close (Enter/Escape); a pointer blur passes false.
  const closeRename = (restore: boolean) => {
    setRenameId(null)
    if (!restore) return
    // Only take back focus the unmount dropped.
    requestAnimationFrame(() => {
      const at = document.activeElement
      if (at === null || at === document.body) renameTriggerRef.current?.focus({ preventScroll: true })
    })
  }
  const [attachEdges, fades, remeasure] = useScrollEdges<HTMLDivElement>()
  const attachWheel = useWheelToHorizontal<HTMLDivElement>()
  const attachScroller = useCallback((node: HTMLDivElement | null) => {
    attachEdges(node)
    attachWheel(node)
  }, [attachEdges, attachWheel])

  // Opening a file appends a tab: the strip keeps its own box, so no resize is
  // observed and no scroll fires, and the cue would stay dark over tabs that
  // just went off-screen.
  useEffect(() => { remeasure() }, [folderTabs, fileTabs, remeasure])

  return (
    <div className="mc-fe-tabstrip-outer">
      {fades.left && <div className="mc-fe-tabstrip-fade mc-fe-fade-left" />}
      {fades.right && <div className="mc-fe-tabstrip-fade mc-fe-fade-right" />}
      <div className="mc-fe-tabs" ref={attachScroller}>
        {folderTabs.map((t) => (
          <div
            key={t.id}
            className={'mc-fe-tab mc-fe-tab-folder' + (t.id === activeFolderId && !activeFileId ? ' is-active' : '') + (t.id === activeFolderId ? ' is-current-folder' : '')}
            onClick={() => onActivateFolder(t.id)}
            onDoubleClick={(e) => { renameTriggerRef.current = e.currentTarget; setRenameId(t.id); setRenameDraft(t.label || basename(t.rootPath)) }}
            title={t.rootPath}
            role="tab"
            tabIndex={0}
            // Row-only activation: the rename input below is a descendant, so
            // without the target check a Space typed into it re-activates this
            // tab on every keystroke.
            onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) onActivateFolder(t.id) }}
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
                {...ime.bindEnter({
                  onEnter: () => { onRenameFolder(t.id, renameDraft.trim()); closeRename(true) },
                  onEscape: () => closeRename(true),
                  onBlur: () => { onRenameFolder(t.id, renameDraft.trim()); closeRename(false) },
                })}
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
