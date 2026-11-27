import { useId, useState, type ReactNode } from 'react'
import { MoreHorizontal, Clock, Pause, Play, Folder, FolderPlus, Send, Trash2 } from 'lucide-react'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
  DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent,
} from './ui/dropdown-menu'
import { Btn } from './ui'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import { useTelegramDestinations } from './TelegramDestinationSelect'
import type { CronJob } from '../types'
import type { CronFolder } from '../utils/cronFolders'
import type { useMenuOpensDialog } from '../hooks/useMenuOpensDialog'
import { uiLocation } from '../uiLocations/uiLocation'
import { i18nT } from '../i18n/t'

export type CronBatchAction = 'pause' | 'resume' | 'strict_on' | 'strict_off' | 'run'

/** The checked jobs an action would change; the rest are skipped. */
export function batchTargets(action: CronBatchAction, jobs: CronJob[], running: Set<string>): CronJob[] {
  switch (action) {
    case 'pause': return jobs.filter(j => j.enabled)
    case 'resume': return jobs.filter(j => !j.enabled)
    case 'strict_on': return jobs.filter(j => !j.strict_schedule)
    case 'strict_off': return jobs.filter(j => j.strict_schedule)
    case 'run': return jobs.filter(j => j.enabled && !j.is_running && !running.has(j.id))
  }
}

/** The checked jobs Set Telegram destination changes; a job with a Slack channel keeps it. */
export function telegramTargets(jobs: CronJob[]): CronJob[] {
  return jobs.filter(j => !j.channel)
}

/** The menu label of an action; the failure notice names the action with it. */
export function batchActionLabel(action: CronBatchAction): string {
  switch (action) {
    case 'run': return i18nT('pages.schedulePage.run_now')
    case 'pause': return i18nT('pages.schedulePage.pause')
    case 'resume': return i18nT('pages.schedulePage.resume')
    case 'strict_on': return i18nT('pages.schedulePage.batchActions.strict_on')
    case 'strict_off': return i18nT('pages.schedulePage.batchActions.strict_off')
  }
}

/** Set Telegram destination: the job form's destinations, read only while the menu
 *  is open, and offered while Telegram is connected, like the form's field, or
 *  while the read failed, so a failure never reads as "not connected". */
function TelegramSubmenu({ jobs, busy, onSend }: {
  jobs: CronJob[]; busy: boolean; onSend: (targetId: string, destination: string) => void
}) {
  const { rows, connected, failed } = useTelegramDestinations()
  const errorId = useId()
  if (!connected && !failed) return null
  const n = telegramTargets(jobs).length
  const loadFailed = i18nT('components.jobForm.telegram_destination_load_failed')
  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger disabled={busy}>
        <Send size={13} className="shrink-0 text-muted" />
        <span>{i18nT('pages.schedulePage.batchActions.send_to_telegram')}</span>
        <span className="ml-auto pl-3 text-muted tabular-nums whitespace-nowrap">{i18nT('pages.schedulePage.batchActions.changes_n_jobs', { count: n })}</span>
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent className="min-w-[160px] max-h-[min(240px,var(--radix-dropdown-menu-content-available-height))]">
        {rows.map(r => (
          <DropdownMenuItem key={r.id} onSelect={() => onSend(r.id, r.label)}>
            <span className="truncate">{r.label}</span>
          </DropdownMenuItem>
        ))}
        {failed && (
          <>
            <ErrorNotice id={errorId} variant="inline" className="px-2 py-1.5" message={loadFailed} />
            <ErrorNoticeMenuItem Item={DropdownMenuItem} message={loadFailed} describedBy={errorId} />
          </>
        )}
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  )
}

/** `run` and delete open a confirm dialog, so they go through `layer.openLayer`. */
export default function CronBatchActionsMenu({
  jobs, running, busy, layer, onAction, folders, onMove, onNewFolder, onSendToTelegram, deleteCount, onDelete,
}: {
  jobs: CronJob[]
  running: Set<string>
  busy: boolean
  layer: ReturnType<typeof useMenuOpensDialog>
  onAction: (action: CronBatchAction) => void
  folders: CronFolder[]
  onMove: (folderId: string) => void
  onNewFolder: (moveTo?: boolean) => Promise<string | undefined> | void
  /** Point the checked jobs at one Telegram destination: its `target_id`, and
   *  its label (`destination`) for the line that reports the update. */
  onSendToTelegram: (targetId: string, destination: string) => void
  deleteCount: number
  onDelete: () => void
}) {
  const [open, setOpen] = useState(false)
  const sortedFolders = [...folders].sort((a, b) => a.order - b.order)
  const move = (folderId: string) => { setOpen(false); onMove(folderId) }
  const item = (action: CronBatchAction, icon: ReactNode, hint?: string) => {
    const n = batchTargets(action, jobs, running).length
    return (
      <DropdownMenuItem disabled={busy || n === 0} onSelect={() => action === 'run' ? layer.openLayer(() => onAction(action)) : onAction(action)}>
        {icon}
        {hint
          ? <span className="flex flex-col"><span>{batchActionLabel(action)}</span><span className="text-[12px] text-muted">{hint}</span></span>
          : <span>{batchActionLabel(action)}</span>}
        <span className="ml-auto pl-3 text-muted tabular-nums whitespace-nowrap">{i18nT('pages.schedulePage.batchActions.changes_n_jobs', { count: n })}</span>
      </DropdownMenuItem>
    )
  }

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Btn
          disabled={busy}
          onPointerDown={layer.rememberOpener}
          onKeyDown={layer.rememberOpener}
          onClick={layer.rememberOpener}
          aria-label={i18nT('pages.schedulePage.batchActions.actions_for_selected')}
          {...uiLocation('schedule.batch-actions')}
          title={i18nT('pages.schedulePage.batchActions.actions_for_selected')}
        >
          <span className="flex items-center gap-1.5"><MoreHorizontal size={14} /> {i18nT('pages.schedulePage.actions')}</span>
        </Btn>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[200px]" onCloseAutoFocus={layer.onCloseAutoFocus}>
        {item('run', <Play size={13} className="shrink-0 text-accent" />)}
        <DropdownMenuSeparator />
        {item('pause', <Pause size={13} className="shrink-0 text-muted" />)}
        {item('resume', <Play size={13} className="shrink-0 text-muted" />)}
        <DropdownMenuSeparator />
        {item('strict_on', <Clock size={13} className="shrink-0 text-muted" />, i18nT('pages.schedulePage.batchActions.strict_on_hint'))}
        {item('strict_off', <Clock size={13} className="shrink-0 text-muted" />, i18nT('pages.schedulePage.batchActions.strict_off_hint'))}
        <DropdownMenuSeparator />
        <DropdownMenuSub>
          <DropdownMenuSubTrigger disabled={busy} {...uiLocation('schedule.move-to-folder')}>
            <Folder size={13} className="shrink-0 text-muted" />
            <span>{i18nT('pages.schedulePage.cronFolders.move_to_folder')}</span>
          </DropdownMenuSubTrigger>
          {/* min() keeps the primitive's available-height floor under the 240px cap. */}
          <DropdownMenuSubContent className="min-w-[160px] max-h-[min(240px,var(--radix-dropdown-menu-content-available-height))]">
            <DropdownMenuItem onSelect={() => move('')}>
              <Folder size={13} className="shrink-0 text-muted" />
              <span>{i18nT('pages.schedulePage.cronFolders.ungrouped')}</span>
            </DropdownMenuItem>
            {sortedFolders.length > 0 && <DropdownMenuSeparator />}
            {sortedFolders.map(f => (
              <DropdownMenuItem key={f.id} onSelect={() => move(f.id)}>
                <Folder size={13} className="shrink-0 text-accent" />
                <span className="truncate">{f.name}</span>
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
        <TelegramSubmenu jobs={jobs} busy={busy} onSend={onSendToTelegram} />
        <DropdownMenuSeparator />
        <DropdownMenuItem
          disabled={busy || deleteCount === 0}
          title={i18nT('pages.schedulePage.delete_n_selected_jobs', { count: deleteCount })}
          onSelect={() => layer.openLayer(onDelete)}
        >
          <Trash2 size={13} className="shrink-0 text-danger" />
          <span className="text-danger">{i18nT('pages.schedulePage.delete_n_selected', { count: deleteCount })}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
