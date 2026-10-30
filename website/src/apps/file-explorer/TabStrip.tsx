import { useCallback, useEffect, useState } from 'react'
import { Folder, File, Image, X, Plus } from 'lucide-react'
import Clickable from '../../components/Clickable'
import { AllTabsMenu } from '../../components/AllTabsMenu'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { useWheelToHorizontal } from '../../hooks/useWheelToHorizontal'
import { TAB_ID_ATTR, useRevealActiveTab } from '../../hooks/useRevealActiveTab'
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

const folderName = (t: FolderTab) => t.label || basename(t.rootPath) || '/'

export default function TabStrip({ folderTabs, fileTabs, activeFolderId, activeFileId, onActivateFolder, onActivateFile, onCloseFolder, onCloseFile, onNewFolder, onRenameFolder }: TabStripProps) {
  // Only one rename input renders at a time, so a single instance is safe.
  const ime = useImeGuard()
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameDraft, setRenameDraft] = useState('')
  const [attachEdges, fades, remeasure] = useScrollEdges<HTMLDivElement>()
  const attachWheel = useWheelToHorizontal<HTMLDivElement>()
  const activeId = activeFileId ?? activeFolderId
  const [attachReveal, reveal] = useRevealActiveTab<HTMLDivElement>(activeId)
  const attachScroller = useCallback((node: HTMLDivElement | null) => {
    attachEdges(node)
    attachWheel(node)
    attachReveal(node)
  }, [attachEdges, attachWheel, attachReveal])

  // Opening a file appends a tab: the strip keeps its own box, so no resize is
  // observed and no scroll fires, and the cue would stay dark over tabs that
  // just went off-screen.
  useEffect(() => { remeasure() }, [folderTabs, fileTabs, remeasure])

  return (
    <div className="mc-fe-tabstrip-outer">
      <div className="mc-fe-tabstrip-scroll">
      {fades.left && <div className="mc-fe-tabstrip-fade mc-fe-fade-left" />}
      {fades.right && <div className="mc-fe-tabstrip-fade mc-fe-fade-right" />}
      <div className="mc-fe-tabs" ref={attachScroller}>
        {folderTabs.map((t) => (
          <div
            key={t.id}
            {...{ [TAB_ID_ATTR]: t.id }}
            className={'mc-fe-tab mc-fe-tab-folder' + (t.id === activeFolderId && !activeFileId ? ' is-active' : '') + (t.id === activeFolderId ? ' is-current-folder' : '')}
            onClick={() => onActivateFolder(t.id)}
            onDoubleClick={() => { setRenameId(t.id); setRenameDraft(t.label || basename(t.rootPath)) }}
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
                  onEnter: () => { onRenameFolder(t.id, renameDraft.trim()); setRenameId(null) },
                  onEscape: () => setRenameId(null),
                  onBlur: () => { onRenameFolder(t.id, renameDraft.trim()); setRenameId(null) },
                })}
              />
            ) : (
              <span className="mc-fe-tab-label">{folderName(t)}</span>
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
            {...{ [TAB_ID_ATTR]: ft.id }}
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
      <AllTabsMenu
        groups={[
          { id: 'folders', rows: folderTabs.map(t => ({ id: t.id, glyph: <Folder size={11} />, title: folderName(t), tooltip: t.rootPath })) },
          { id: 'files', rows: fileTabs.map(ft => ({ id: ft.id, glyph: IMAGE_EXTS.has(extOf(ft.path)) ? <Image size={11} /> : <File size={11} />, title: basename(ft.path), tooltip: ft.path })) },
        ]}
        currentId={activeId}
        onSelect={id => {
          if (id === activeId) reveal(id)
          else if (fileTabs.some(ft => ft.id === id)) onActivateFile(id)
          else onActivateFolder(id)
        }}
        label={i18nT('pages.chat.sidePanel.all_tabs')}
      />
    </div>
  )
}
