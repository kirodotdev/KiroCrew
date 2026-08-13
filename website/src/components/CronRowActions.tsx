import { useState } from 'react'
import { MoreHorizontal, Check, Clock, Pause, Play, MessageSquare, Folder, FolderPlus } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent,
} from './ui/dropdown-menu'
import { Btn } from './ui'
import type { CronJob } from '../types'
import type { CronFolder } from '../utils/cronFolders'
import { i18nT } from '../i18n/t'

/**
 * Overflow menu for a job row's secondary actions.
 *
 * The row used to carry SIX controls (Strict, Run/Cancel, View/Continue,
 * Pause/Resume, Move-to-folder, Delete) in a 210px cell. They did not fit — the
 * column was clipped under the old wrapping layout and pushed clean off the
 * horizontal scroll area once cells stopped wrapping. Only the two the user acts
 * on per-glance stay in the row (Run/Cancel, Delete); the rest live here.
 *
 * `Delete` deliberately does NOT move in: it is an arm→Confirm state machine on
 * the row button itself, and a menu that closes on select cannot host the armed
 * state. Folder moves are a SUBMENU rather than a reuse of `CronJobMoveMenu`,
 * which is a standalone dropdown (still used by the batch bar) and cannot nest.
 */
export default function CronRowActions({
  job, folders, running, cancelling, onRun, onCancelRun, onOpenInChat, onToggleEnabled,
  onToggleStrict, onMove, onNewFolder,
}: {
  job: CronJob
  folders: CronFolder[]
  running: boolean
  cancelling: boolean
  onRun: () => void
  onCancelRun: () => void
  onOpenInChat: () => void
  onToggleEnabled: () => void
  onToggleStrict: () => void
  onMove: (folderId: string) => void
  onNewFolder: (moveTo?: boolean) => Promise<string | undefined> | void
}) {
  const [open, setOpen] = useState(false)
  const sortedFolders = [...folders].sort((a, b) => a.order - b.order)
  const hasResult = !!job.has_result || !!job.has_slot

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Btn
          className="!px-1.5"
          aria-label={i18nT('pages.schedulePage.actions')}
          title={i18nT('pages.schedulePage.actions')}
        >
          <MoreHorizontal size={14} />
        </Btn>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[190px]">
        {/* Run / Cancel is also in the row; repeated here so the menu is a
            complete account of what can be done to the job. */}
        {job.is_running ? (
          <DropdownMenuItem disabled={cancelling} onSelect={onCancelRun}>
            <Pause size={13} className="shrink-0 text-danger" />
            <span>{i18nT('pages.schedulePage.cancel_running_execution')}</span>
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem disabled={!job.enabled || running} onSelect={onRun}>
            <Play size={13} className="shrink-0 text-accent" />
            <span>{i18nT('pages.schedulePage.run_now')}</span>
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onSelect={onToggleEnabled}>
          {job.enabled
            ? <><Pause size={13} className="shrink-0 text-muted" /><span>{i18nT('pages.schedulePage.pause')}</span></>
            : <><Play size={13} className="shrink-0 text-muted" /><span>{i18nT('pages.schedulePage.resume')}</span></>}
        </DropdownMenuItem>
        <DropdownMenuItem
          disabled={!hasResult}
          title={job.has_slot
            ? i18nT('pages.schedulePage.continue_session')
            : job.has_result ? i18nT('pages.schedulePage.view_last_result') : i18nT('pages.schedulePage.no_result')}
          onSelect={onOpenInChat}
        >
          <MessageSquare size={13} className="shrink-0 text-muted" />
          <span>{job.has_slot ? i18nT('pages.schedulePage.continue_session') : i18nT('pages.schedulePage.view_last_result')}</span>
        </DropdownMenuItem>

        <DropdownMenuSeparator />

        <DropdownMenuItem
          title={job.strict_schedule
            ? i18nT('pages.schedulePage.disable_strict_schedule_allow_jitter')
            : i18nT('pages.schedulePage.enable_strict_schedule_no_jitter')}
          onSelect={onToggleStrict}
        >
          <Clock size={13} className="shrink-0 text-muted" />
          <span>{i18nT('pages.schedulePage.strict')}</span>
          {job.strict_schedule && <Check size={13} className="ml-auto shrink-0 text-accent" />}
        </DropdownMenuItem>

        <DropdownMenuSub>
          <DropdownMenuSubTrigger>
            <Folder size={13} className="shrink-0 text-muted" />
            <span>{i18nT('pages.schedulePage.cronFolders.move_to_folder')}</span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent className="min-w-[160px] max-h-[240px] overflow-y-auto">
            <DropdownMenuItem onSelect={() => { onMove(''); setOpen(false) }}>
              <Folder size={13} className="shrink-0 text-muted" />
              <span>{i18nT('pages.schedulePage.cronFolders.ungrouped')}</span>
              {!job.folder_id && <Check size={13} className="ml-auto shrink-0 text-accent" />}
            </DropdownMenuItem>
            {sortedFolders.length > 0 && <DropdownMenuSeparator />}
            {sortedFolders.map(f => (
              <DropdownMenuItem key={f.id} onSelect={() => { onMove(f.id); setOpen(false) }}>
                <Folder size={13} className="shrink-0 text-accent" />
                <span className="truncate">{f.name}</span>
                {job.folder_id === f.id && <Check size={13} className="ml-auto shrink-0 text-accent" />}
              </DropdownMenuItem>
            ))}
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={async () => {
              setOpen(false)
              const newId = await onNewFolder(true)
              if (newId) onMove(newId)
            }}>
              <FolderPlus size={13} className="shrink-0 text-accent" />
              <span className="font-medium">{i18nT('pages.schedulePage.cronFolders.new_folder')}</span>
            </DropdownMenuItem>
          </DropdownMenuSubContent>
        </DropdownMenuSub>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
