import { useState } from 'react'
import { Folder, FolderPlus, Check } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
} from './ui/dropdown-menu'
import { Btn } from './ui'
import type { CronFolder } from '../utils/cronFolders'
import { i18nT } from '../i18n/t'

interface Props {
  folders: CronFolder[]
  currentFolderId?: string
  onMove: (folderId: string) => void
  onNewFolder: (moveTo?: boolean) => Promise<string | undefined> | void
}

export default function CronJobMoveMenu({ folders, currentFolderId, onMove, onNewFolder }: Props) {
  const [open, setOpen] = useState(false)
  const sortedFolders = [...folders].sort((a, b) => a.order - b.order)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Btn
          aria-label={i18nT('pages.schedulePage.cronFolders.move_to_folder')}
          title={i18nT('pages.schedulePage.cronFolders.move_to_folder')}
        >
          <Folder size={13} />
        </Btn>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[160px] max-h-[240px] overflow-y-auto">
        <DropdownMenuItem onSelect={() => { onMove(''); setOpen(false) }}>
          <Folder size={13} className="text-muted shrink-0" />
          <span>{i18nT('pages.schedulePage.cronFolders.ungrouped')}</span>
          {(!currentFolderId || currentFolderId === '') && <Check size={13} className="ml-auto text-accent shrink-0" />}
        </DropdownMenuItem>
        {sortedFolders.length > 0 && (
          <>
            <DropdownMenuSeparator />
            {sortedFolders.map(f => (
              <DropdownMenuItem key={f.id} onSelect={() => { onMove(f.id); setOpen(false) }}>
                <Folder size={13} className="text-accent shrink-0" />
                <span className="truncate">{f.name}</span>
                {currentFolderId === f.id && <Check size={13} className="ml-auto text-accent shrink-0" />}
              </DropdownMenuItem>
            ))}
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={async () => {
          setOpen(false)
          const newId = await onNewFolder(true)
          if (newId) onMove(newId)
        }}>
          <FolderPlus size={13} className="text-accent shrink-0" />
          <span className="font-medium">{i18nT('pages.schedulePage.cronFolders.new_folder')}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
