import { useState } from 'react'
import { Folder, FolderPlus, Check } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
} from './ui/dropdown-menu'
import { Btn } from './ui'
import type { CronFolder } from '../utils/cronFolders'
import { uiLocation } from '../uiLocations/uiLocation'
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
        {/* Label-bearing, like every other button in this toolbar: the folder
            trigger was the lone icon-only control in the row, so its purpose was
            legible only on hover (title) or to assistive tech. It now shows the
            same "Move to folder…" text in words. The aria-label is kept (and
            equals the visible text, so WCAG 2.5.3 Label-in-Name holds): it is
            the label the uiLocation control-index reads, which gen-ui-index
            requires. Icon sized to 14 to match its toolbar siblings (Delete's
            Trash2, New folder's FolderPlus); the gap-1.5 span mirrors their
            layout exactly. */}
        <Btn
          aria-label={i18nT('pages.schedulePage.cronFolders.move_to_folder')}
          title={i18nT('pages.schedulePage.cronFolders.move_to_folder')}
          {...uiLocation('schedule.move-to-folder')}
        >
          <span className="flex items-center gap-1.5">
            <Folder size={14} />
            {i18nT('pages.schedulePage.cronFolders.move_to_folder')}
          </span>
        </Btn>
      </DropdownMenuTrigger>
      {/* Intentional tighter cap: the folder list can grow long, so a fixed
          240px keeps this picker compact. Composed via min() with the
          primitive's available-height var so the viewport never-clip floor is
          preserved on a short viewport (a bare max-h-[240px] would override the
          primitive's cap, since cn()'s tailwind-merge dedupes max-h-*). overflow
          is left to the primitive. */}
      <DropdownMenuContent align="end" className="min-w-[160px] max-h-[min(240px,var(--radix-dropdown-menu-content-available-height))]">
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
