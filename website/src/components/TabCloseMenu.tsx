import type { ReactNode } from 'react'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from './ui/context-menu'
import { i18nT } from '../i18n/t'

/**
 * Open a chip's close menu from a touch hold that lifted in place
 * (`useLongPressReorder`'s `onHoldRelease`). The Radix trigger opens on
 * `contextmenu` and positions at the event's point, so the release becomes the
 * event a right-click at that point would have produced. A trigger that is
 * `disabled` ignores it, exactly as it ignores a right-click.
 */
export function openTabCloseMenu(e: PointerEvent, target: HTMLElement): void {
  target.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: e.clientX, clientY: e.clientY }))
}

/** Close, Close other tabs, Close tabs to the right, Close all tabs — on
 *  right-click, or on touch by holding the chip and lifting without moving
 *  (see `openTabCloseMenu`). */
export default function TabCloseMenu({ children, disabled = false, closeOthersDisabled, closeRightDisabled, onClose, onCloseOthers, onCloseRight, onCloseAll }: {
  children: ReactNode
  disabled?: boolean
  closeOthersDisabled: boolean
  closeRightDisabled: boolean
  onClose: () => void
  onCloseOthers: () => void
  onCloseRight: () => void
  onCloseAll: () => void
}) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild disabled={disabled}>{children}</ContextMenuTrigger>
      <ContextMenuContent className="min-w-[190px]">
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
      </ContextMenuContent>
    </ContextMenu>
  )
}
