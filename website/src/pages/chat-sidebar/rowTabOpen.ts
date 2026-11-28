/** How a sidebar session row opens a session tab: the row menu's foreground
 *  open, and the Cmd/Ctrl-click gesture that the "Switch to Session Tabs on
 *  Cmd/Ctrl-Click" chat setting can point at it. Middle-click stays a
 *  background open in the row itself and does not come through here. */
import { useMemo } from 'react'
import { loadChatConfig } from '../chat/ChatSettings'

type OpenSlotInNewTab = (key: string, opts?: { background?: boolean }) => void

/** A session row's foreground open, used by the row menu's "Open in a session
 *  tab" and, with the setting on, by Cmd/Ctrl-click. It is a selection, so it
 *  reports the row and the host clears Split View the way a plain click does.
 *  Offline, the host opens the tab but cannot switch to it, so the selection is
 *  not reported: clearing Split View then would leave the old session showing
 *  alone. */
export function useOpenInForegroundTab(
  open: OpenSlotInNewTab | undefined,
  onSelect: ((key: string) => void) | undefined,
  connected: boolean,
): ((key: string) => void) | undefined {
  return useMemo(() => open
    ? (key: string) => { open(key); if (connected) onSelect?.(key) }
    : undefined, [open, onSelect, connected])
}

/** Cmd/Ctrl-click on a session row. By default it queues the session as a
 *  background tab, the sidebar-wide rule in
 *  docs/decisions/2026-10-17-sidebar-create-controls-shift-opens-the-active-tab.md.
 *  The opt-in chat setting `sessionRowTabActive` makes it open the active tab
 *  instead, through the same foreground path as the row menu. It is read at
 *  click time, so a change in Settings applies without a reload. */
export function openRowTabOnModifierClick(
  key: string,
  open: OpenSlotInNewTab,
  openInForeground: ((key: string) => void) | undefined,
): void {
  if (openInForeground && loadChatConfig().sessionRowTabActive === true) openInForeground(key)
  else open(key, { background: true })
}
