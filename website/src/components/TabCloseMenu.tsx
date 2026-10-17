import type { ReactNode } from 'react'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuLabel, ContextMenuSeparator } from './ui/context-menu'
import ErrorNotice from './ErrorNotice'
import type { ConfirmOptions } from './ConfirmDialog'
import { i18nT } from '../i18n/t'

/**
 * Open a chip's close menu from a touch hold that lifted in place
 * (`useLongPressReorder`'s `onHoldRelease`). The Radix trigger opens on
 * `contextmenu` and positions at the event's point, so the release becomes the
 * event a right-click at that point would have produced. It is dispatched on
 * the tab inside the held item, so it bubbles through whichever element carries
 * the trigger: the item itself on the workspace strip, the chip on the terminal
 * strip (whose menu also holds Rename). A `disabled` trigger ignores it, exactly
 * as it ignores a right-click.
 */
export function openTabCloseMenu(e: PointerEvent, target: HTMLElement): void {
  const tab = target.matches('[role="tab"]') ? target : target.querySelector<HTMLElement>('[role="tab"]') ?? target
  tab.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: e.clientX, clientY: e.clientY }))
}

/** Dirty files listed by name before the rest collapse into "N more". */
const LISTED_DIRTY_FILES = 5

export interface DirtyFile {
  name: string
  path?: string
}

/**
 * The confirmation a close batch needs, or `null` when it needs none. Three
 * things in a batch cannot be taken back: unsaved file edits, which are named
 * so the reader knows what is lost; a clean file tab whose on-disk copy is gone
 * or could not be read, whose buffer is then the only remaining copy; and
 * running shells, counted and named once the batch stops more than one, so the
 * reader sees which shells stop. A single shell is not asked about, matching
 * its × control. A disk preflight (see
 * `handleCloseTabs`) sorts the clean tabs it could not confirm into two lists:
 * `lastCopyFiles`, whose disk copy is gone (404), and `unreadFiles`, whose read
 * FAILED, so the batch keeps `MarkdownPanel`'s last-copy guard for both while
 * reporting the failure as the error it is rather than as a missing file.
 */
export function batchCloseConfirm(
  dirtyFiles: readonly DirtyFile[],
  shells: readonly string[],
  lastCopyFiles: readonly DirtyFile[] = [],
  unreadFiles: readonly DirtyFile[] = [],
): ConfirmOptions | null {
  const shellCount = shells.length
  const stopsShells = shellCount > 1
  if (dirtyFiles.length === 0 && lastCopyFiles.length === 0 && unreadFiles.length === 0 && !stopsShells) return null
  const listed = dirtyFiles.slice(0, LISTED_DIRTY_FILES)
  const unlisted = dirtyFiles.length - listed.length
  const listedCopies = lastCopyFiles.slice(0, LISTED_DIRTY_FILES)
  const unlistedCopies = lastCopyFiles.length - listedCopies.length
  const listedUnread = unreadFiles.slice(0, LISTED_DIRTY_FILES)
  const unlistedUnread = unreadFiles.length - listedUnread.length
  const listedShells = shells.slice(0, LISTED_DIRTY_FILES)
  const unlistedShells = shellCount - listedShells.length
  const losesWholeFiles = lastCopyFiles.length > 0 || unreadFiles.length > 0
  const losesContent = dirtyFiles.length > 0 || losesWholeFiles
  // A tab whose disk copy is gone or unreadable loses the whole file, not
  // just edits, so its button says "Discard and close" rather than "Discard
  // changes"; a batch that also stops shells names both in title and button.
  return {
    title: losesContent
      ? (stopsShells
        ? i18nT('components.tabCloseMenu.discard_and_close_terminals_title')
        : i18nT('components.markdownPanel.discard_unsaved_changes'))
      : i18nT('components.tabCloseMenu.close_terminals_title'),
    confirmLabel: losesContent
      ? (stopsShells
        ? i18nT('components.tabCloseMenu.close_and_discard_button')
        : losesWholeFiles
          ? i18nT('components.tabCloseMenu.discard_and_close_button')
          : i18nT('components.markdownPanel.discard_changes_button'))
      : i18nT('components.tabCloseMenu.close_terminals_button'),
    body: (
      <div className="space-y-2">
        {dirtyFiles.length > 0 && (
          <div>
            <p>{i18nT('components.tabCloseMenu.unsaved_files_lost')}</p>
            <ul className="mt-1 list-none p-0 space-y-0.5">
              {listed.map((file, i) => (
                <li key={`${file.path ?? file.name}-${i}`} title={file.path} translate="no" data-i18n-opaque=""
                  className="truncate font-mono text-[12px] text-text">
                  {file.name}
                </li>
              ))}
              {unlisted > 0 && <li className="text-[12px] text-muted">{i18nT('app.n_more', { count: unlisted })}</li>}
            </ul>
          </div>
        )}
        {lastCopyFiles.length > 0 && (
          <div>
            <p>{i18nT('components.tabCloseMenu.last_copy_files_lost')}</p>
            <ul className="mt-1 list-none p-0 space-y-0.5">
              {listedCopies.map((file, i) => (
                <li key={`${file.path ?? file.name}-${i}`} title={file.path} translate="no" data-i18n-opaque=""
                  className="truncate font-mono text-[12px] text-text">
                  {file.name}
                </li>
              ))}
              {unlistedCopies > 0 && <li className="text-[12px] text-muted">{i18nT('app.n_more', { count: unlistedCopies })}</li>}
            </ul>
          </div>
        )}
        {unreadFiles.length > 0 && (
          <div>
            {/* No hand-off: the open tabs this dialog is about to close hold the
                only copies of these files, and the hand-off's navigation would
                unmount them before the reader decides. */}
            <ErrorNotice variant="inline" message={i18nT('components.tabCloseMenu.unread_files_error')} />
            <ul className="mt-1 list-none p-0 space-y-0.5">
              {listedUnread.map((file, i) => (
                <li key={`${file.path ?? file.name}-${i}`} title={file.path} translate="no" data-i18n-opaque=""
                  className="truncate font-mono text-[12px] text-text">
                  {file.name}
                </li>
              ))}
              {unlistedUnread > 0 && <li className="text-[12px] text-muted">{i18nT('app.n_more', { count: unlistedUnread })}</li>}
            </ul>
          </div>
        )}
        {stopsShells && (
          <div>
            <p>{i18nT('components.tabCloseMenu.close_terminals_body', { count: shellCount })}</p>
            <ul className="mt-1 list-none p-0 space-y-0.5">
              {listedShells.map((name, i) => (
                <li key={`${name}-${i}`} translate="no" data-i18n-opaque=""
                  className="truncate font-mono text-[12px] text-text">
                  {name}
                </li>
              ))}
              {unlistedShells > 0 && <li className="text-[12px] text-muted">{i18nT('app.n_more', { count: unlistedShells })}</li>}
            </ul>
          </div>
        )}
      </div>
    ),
  }
}

export interface TabCloseActions {
  closeOthersDisabled: boolean
  closeRightDisabled: boolean
  onClose: () => void
  onCloseOthers: () => void
  onCloseRight: () => void
  onCloseAll: () => void
}

/** Close, Close other tabs, Close tabs to the right, Close all tabs, for a
 *  chip whose context menu carries other items as well. A strip whose batch
 *  closes only terminals says so in the labels (`scope="terminals"`), so the
 *  reader does not wonder whether file tabs go too. */
export function TabCloseMenuItems({ closeOthersDisabled, closeRightDisabled, onClose, onCloseOthers, onCloseRight, onCloseAll, scope = 'tabs' }: TabCloseActions & {
  scope?: 'tabs' | 'terminals'
}) {
  const terminals = scope === 'terminals'
  return (
    <>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" onSelect={onClose}>
        {i18nT('components.bottomTerminalPanel.close_tab')}
      </ContextMenuItem>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" disabled={closeOthersDisabled} onSelect={onCloseOthers}>
        {terminals ? i18nT('components.tabCloseMenu.close_other_terminals') : i18nT('components.bottomTerminalPanel.close_other_tabs')}
      </ContextMenuItem>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" disabled={closeRightDisabled} onSelect={onCloseRight}>
        {terminals ? i18nT('components.tabCloseMenu.close_terminals_to_right') : i18nT('components.bottomTerminalPanel.close_tabs_to_right')}
      </ContextMenuItem>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" onSelect={onCloseAll}>
        {terminals ? i18nT('components.tabCloseMenu.close_all_terminals') : i18nT('components.bottomTerminalPanel.close_all_tabs')}
      </ContextMenuItem>
    </>
  )
}

/** The close menu on right-click, or on touch by holding the chip and lifting
 *  without moving (see `openTabCloseMenu`). `keepNote` names, under the
 *  items, the tabs a batch leaves open, so its scope is visible in the menu. */
export default function TabCloseMenu({ children, disabled = false, keepNote, ...actions }: TabCloseActions & {
  children: ReactNode
  disabled?: boolean
  keepNote?: string
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild disabled={disabled}>{children}</ContextMenuTrigger>
      <ContextMenuContent className="min-w-[190px]">
        <TabCloseMenuItems {...actions} />
        {keepNote && (
          <>
            <ContextMenuSeparator />
            <ContextMenuLabel>{keepNote}</ContextMenuLabel>
          </>
        )}
      </ContextMenuContent>
    </ContextMenu>
  )
}
