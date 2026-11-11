/** The bar the Sessions sidebar shows while rows are picked (`./selection`): how
 *  many, a way out, and the batch actions. Each action runs the single-session
 *  action of the row menu once per picked session, then ends the selection. */
import { Circle, MoreHorizontal, Pin, X } from 'lucide-react'
import FolderMoveSubmenu from '../../components/FolderMoveSubmenu'
import OfflineMenuReason from '../../components/OfflineMenuReason'
import { Btn, IconButton } from '../../components/ui'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '../../components/ui/dropdown-menu'
import { useConnected } from '../../hooks/useConnected'
import { useFolderSortMode } from '../../hooks/useFolderSortMode'
import type { SessionActions } from '../../hooks/useSessionActions'
import { i18nT } from '../../i18n/t'
import type { ChatFolder } from '../../types'
import { isMateManaged } from '../../utils/mateFolders'
import { offlineMenuRow } from '../../utils/offline'
import type { Slot } from './types'

/** A folder id no folder has: the move picker's "current folder" while the picked
 *  sessions sit in several, so it ticks neither a folder nor the root entry. */
const NO_SHARED_FOLDER = '\u0000'

export default function SelectionBar({ selected, slots, unread, folders, actions, onClear }: {
  selected: ReadonlySet<string>
  /** The local sessions, in which the picked ones are looked up. */
  slots: Slot[]
  unread: ReadonlySet<string>
  folders: ChatFolder[]
  actions: Pick<SessionActions, 'togglePin' | 'toggleRead' | 'move' | 'closeMany'>
  onClear: () => void
}) {
  const connected = useConnected()
  const { mode: folderSortMode } = useFolderSortMode()
  const picked = slots.filter(s => selected.has(s.key))
  const keys = picked.map(s => s.key)
  const count = keys.length
  // One label per action, as on a row: Unpin once every picked session is
  // pinned, Mark as unread once none is unread.
  const allPinned = picked.every(s => s.pinned)
  const anyUnread = keys.some(key => unread.has(key))
  const pinLabel = allPinned ? i18nT('components.sessionActionsMenu.unpin') : i18nT('components.sessionActionsMenu.pin')
  const readLabel = anyUnread ? i18nT('components.sessionActionsMenu.mark_as_read') : i18nT('components.sessionActionsMenu.mark_as_unread')
  const closeLabel = i18nT('pages.chatSidebar.selection.close', { count })
  // A chat inside a mate's folder stays there, and no chat moves into one.
  const movableFolders = folders.filter(f => !isMateManaged(f, folders))
  const movable = picked.filter(s => !isMateManaged(folders.find(f => f.id === s.folder_id), folders))
  // The picker ticks the folder the sessions are in only when they share one.
  const homes = new Set(movable.map(s => s.folder_id || ''))
  const sharedFolder = homes.size === 1 ? [...homes][0] : NO_SHARED_FOLDER
  const then = (run: () => void) => () => { run(); onClear() }
  return (
    <div role="toolbar" aria-label={i18nT('pages.chatSidebar.selection.actions_label')} data-testid="session-selection-bar"
      className="mx-2 my-1 shrink-0 rounded-lg border border-border bg-bg px-2 py-1.5 shadow-sm animate-rise">
      <div className="flex items-center gap-1.5">
        <IconButton aria-label={i18nT('pages.chatSidebar.selection.clear')} onClick={onClear} data-testid="session-selection-clear"><X size={13} /></IconButton>
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-text-strong" data-testid="session-selection-count">
          {i18nT('pages.chatSidebar.selection.count', { count })}
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Btn className="shrink-0" data-testid="session-selection-actions">
              <MoreHorizontal size={13} /> {i18nT('pages.chatSidebar.selection.actions')}
            </Btn>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-[200px]">
            {movableFolders.length > 0 && movable.length > 0 && (
              <FolderMoveSubmenu
                variant="dropdown"
                folders={movableFolders}
                currentFolderId={sharedFolder}
                onPick={folderId => { if (!connected) return; movable.forEach(s => actions.move(s.key, folderId)); onClear() }}
                offlineVerb={i18nT('utils.offline.move_sessions')}
                label={i18nT('components.sessionActionsMenu.move_to_folder')}
                sortMode={folderSortMode}
              />
            )}
            <DropdownMenuItem {...offlineMenuRow(connected, i18nT('utils.offline.change_pin'), pinLabel, then(() => picked.filter(s => !!s.pinned === allPinned).forEach(s => actions.togglePin(s.key))))}>
              <Pin size={13} className="shrink-0 text-muted" /> {pinLabel}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={then(() => keys.filter(key => unread.has(key) === anyUnread).forEach(key => actions.toggleRead(key)))}>
              <Circle size={13} className="shrink-0 text-muted" /> {readLabel}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              {...offlineMenuRow(connected, i18nT('utils.offline.close_sessions'), closeLabel, () => { void actions.closeMany(keys).then(ok => { if (ok) onClear() }) })}
              data-testid="session-selection-close"
            >
              <X size={13} className="shrink-0 text-danger" /> <span className="text-danger">{closeLabel}</span>
            </DropdownMenuItem>
            {!connected && <OfflineMenuReason />}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <p className="m-0 mt-1 text-[11px] leading-snug text-muted">{i18nT('pages.chatSidebar.selection.hint')}</p>
    </div>
  )
}
