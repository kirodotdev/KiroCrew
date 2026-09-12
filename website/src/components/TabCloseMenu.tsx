import type { ReactNode } from 'react'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from './ui/context-menu'
import { i18nT } from '../i18n/t'

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
