import type { ReactNode } from 'react'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from './ui/context-menu'
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
 * The confirmation a close batch needs, or `null` when it needs none. Two
 * things in a batch cannot be taken back: unsaved file edits, which are named
 * so the reader knows what is lost, and running shells, which are counted once
 * the batch stops more than one. A single shell is not asked about, matching
 * its × control.
 */
export function batchCloseConfirm(dirtyFiles: readonly DirtyFile[], shellCount: number): ConfirmOptions | null {
  const stopsShells = shellCount > 1
  if (dirtyFiles.length === 0 && !stopsShells) return null
  const listed = dirtyFiles.slice(0, LISTED_DIRTY_FILES)
  const unlisted = dirtyFiles.length - listed.length
  return {
    title: dirtyFiles.length > 0
      ? i18nT('components.markdownPanel.discard_unsaved_changes')
      : i18nT('components.tabCloseMenu.close_terminals_title'),
    confirmLabel: dirtyFiles.length > 0
      ? i18nT('components.markdownPanel.discard_changes_button')
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
        {stopsShells && <p>{i18nT('components.tabCloseMenu.close_terminals_body', { count: shellCount })}</p>}
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
 *  chip whose context menu carries other items as well. */
export function TabCloseMenuItems({ closeOthersDisabled, closeRightDisabled, onClose, onCloseOthers, onCloseRight, onCloseAll }: TabCloseActions) {
  return (
    <>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" onSelect={onClose}>
        {i18nT('components.bottomTerminalPanel.close_tab')}
      </ContextMenuItem>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" disabled={closeOthersDisabled} onSelect={onCloseOthers}>
        {i18nT('components.bottomTerminalPanel.close_other_tabs')}
      </ContextMenuItem>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" disabled={closeRightDisabled} onSelect={onCloseRight}>
        {i18nT('components.bottomTerminalPanel.close_tabs_to_right')}
      </ContextMenuItem>
      <ContextMenuItem className="[@media(pointer:coarse)]:min-h-11" onSelect={onCloseAll}>
        {i18nT('components.bottomTerminalPanel.close_all_tabs')}
      </ContextMenuItem>
    </>
  )
}

/** The close menu on right-click, or on touch by holding the chip and lifting
 *  without moving (see `openTabCloseMenu`). */
export default function TabCloseMenu({ children, disabled = false, ...actions }: TabCloseActions & {
  children: ReactNode
  disabled?: boolean
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild disabled={disabled}>{children}</ContextMenuTrigger>
      <ContextMenuContent className="min-w-[190px]">
        <TabCloseMenuItems {...actions} />
      </ContextMenuContent>
    </ContextMenu>
  )
}
