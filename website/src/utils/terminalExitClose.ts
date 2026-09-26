/**
 * A shell that exits on its own closes its terminal tab, in the docked panel or
 * in a chat's side panel. Imported for its side effect by `App`, which runs in
 * both the main window and the terminal popout: the subscription must not
 * depend on any terminal component being mounted.
 */
import { onTerminalExit, type TerminalExit } from './terminalRegistry'
import { disposeTerminalSession } from '../components/CliPanel'
import { hasTab, removeTab } from '../hooks/useBottomTerminal'
import { hasPanelTerminalTab, closePanelTerminalTabs } from '../hooks/usePanelTabs'

/** Close the exiting shell's tab in whichever host holds it; a no-op when no
 *  host has one. Both hosts are asked independently, since a hand-over between
 *  them is two store writes. No DELETE: the server has already reaped the PTY. */
export const closeTabOnShellExit = (sessionId: string, exit: TerminalExit): void => {
  // The server decided (dashboard.terminal.close_on_exit): a kept tab shows
  // its exit line and keeps its scrollback; nothing here to do.
  if (!exit.close) return
  const inDock = hasTab(sessionId)
  const inPanel = hasPanelTerminalTab(sessionId)
  if (!inDock && !inPanel) return
  disposeTerminalSession(sessionId)
  if (inDock) removeTab(sessionId)
  if (inPanel) closePanelTerminalTabs(sessionId)
}

onTerminalExit(closeTabOnShellExit)
