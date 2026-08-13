import { useState } from 'react'
import { ChevronRight, Folder, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { Btn, Input } from './ui'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from './ui/dropdown-menu'
import type { CronFolder } from '../utils/cronFolders'
import { TableCell, TableRow } from './ui/table'
import { i18nT } from '../i18n/t'

interface Props {
  folder: CronFolder
  jobCount: number
  collapsed: boolean
  onToggleCollapse: () => void
  onRename: (name: string) => void
  onDelete: () => void
  colSpan: number
}

export default function CronFolderHeader({ folder, jobCount, collapsed, onToggleCollapse, onRename, onDelete, colSpan }: Props) {
  const [editing, setEditing] = useState(false)
  const [editName, setEditName] = useState(folder.name)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const commitRename = () => {
    const trimmed = editName.trim()
    if (trimmed && trimmed !== folder.name) onRename(trimmed)
    setEditing(false)
  }

  return (
    <>
      <TableRow className="bg-bg-elevated/50 hover:bg-transparent">
        <TableCell colSpan={colSpan} className="px-2.5 py-1.5">
          <div className="flex items-center gap-2">
            {editing ? (
              <>
                <Folder size={14} className="text-accent shrink-0" />
                <Input
                  autoFocus
                  aria-label={i18nT('pages.schedulePage.cronFolders.rename')}
                  className="bg-bg rounded px-2 py-0.5 flex-none min-w-[120px]"
                  value={editName}
                  onChange={e => setEditName(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter') commitRename()
                    if (e.key === 'Escape') setEditing(false)
                  }}
                  onBlur={commitRename}
                />
              </>
            ) : (
              <button
                type="button"
                onClick={onToggleCollapse}
                className="flex items-center gap-1.5 bg-transparent border-none cursor-pointer p-0 text-text hover:text-accent transition-colors"
                aria-label={`${collapsed ? i18nT('pages.schedulePage.cronFolders.expand_folder') : i18nT('pages.schedulePage.cronFolders.collapse_folder')} ${folder.name}`}
              >
                <ChevronRight size={14} className={`transition-transform ${collapsed ? '' : 'rotate-90'}`} />
                <Folder size={14} className="text-accent shrink-0" />
                <span className="text-sm font-medium">{folder.name}</span>
              </button>
            )}

            <span className="text-[12px] text-muted">
              {i18nT('pages.schedulePage.cronFolders.job_count', { count: jobCount })}
            </span>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Btn className="!p-1 !border-none ml-auto" aria-label={i18nT('pages.schedulePage.cronFolders.folder_actions')}>
                  <MoreHorizontal size={14} />
                </Btn>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[140px]">
                <DropdownMenuItem onSelect={() => { setEditName(folder.name); setTimeout(() => setEditing(true), 0) }}>
                  <Pencil size={13} className="shrink-0" />
                  <span>{i18nT('pages.schedulePage.cronFolders.rename')}</span>
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setConfirmingDelete(true)} className="text-danger">
                  <Trash2 size={13} className="shrink-0" />
                  <span>{i18nT('pages.schedulePage.cronFolders.delete_folder')}</span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </TableCell>
      </TableRow>
      {confirmingDelete && (
        <TableRow className="bg-danger/5 border-danger/20 hover:bg-transparent">
          <TableCell colSpan={colSpan} className="px-4 py-2">
            <div className="flex items-center gap-3 text-sm">
              <span className="text-text">{i18nT('pages.schedulePage.cronFolders.confirm_delete_folder', { name: folder.name })}</span>
              <Btn danger onClick={() => { onDelete(); setConfirmingDelete(false) }}>
                {i18nT('pages.schedulePage.cronFolders.delete_folder_named', { name: folder.name })}
              </Btn>
              <Btn onClick={() => setConfirmingDelete(false)}>
                {i18nT('pages.schedulePage.cancel')}
              </Btn>
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}
