import { useEffect, useRef } from 'react'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import { registerPopout, returnSelfToMain } from '../utils/terminalPopout'
import { useBottomTerminal, openBottomTerminal } from '../hooks/useBottomTerminal'

import { i18nT } from '../i18n/t'

/**
 * The window shell for the popped-out terminal panel (`/popout/terminal`).
 *
 * Renders the shared tabbed terminal view (`TerminalTabsView popout`) filling
 * the whole window — its own JS context, so each tab's CliPanel opens a fresh
 * WebSocket to the still-live PTY and the backend replays that session's
 * scrollback. Tab membership rides the localStorage-persisted bottom-terminal
 * store (cross-window `storage` sync), so this window and the main dashboard
 * always agree on the tab list.
 *
 * On top it (1) registers as the live terminal popout so the main dashboard
 * can track / focus / bring it back, and (2) sets the OS window/taskbar title.
 * The "Return" control lives in the tab strip (rendered by the popout
 * variant), where the dock variant shows its pop-out control.
 */
export default function TerminalPopoutFrame() {
  const { tabs } = useBottomTerminal()

  // Announce presence and wire the heartbeat/control responder.
  useEffect(() => registerPopout(), [])

  useEffect(() => {
    document.title = i18nT('pages.terminalPopoutFrame.window_title', {
      label: i18nT('pages.terminalPopoutFrame.terminal'),
    })
  }, [])

  // A deep-linked popout with no tabs mints one (an empty terminal window is
  // useless); afterwards, closing the LAST tab returns the panel to the main
  // window instead of leaving an empty shell behind.
  const sawTabs = useRef(false)
  useEffect(() => {
    if (tabs.length > 0) { sawTabs.current = true; return }
    if (sawTabs.current) returnSelfToMain()
    else openBottomTerminal()
  }, [tabs.length])

  return (
    <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col relative">
      <div className="flex-1 min-h-0">
        <TerminalTabsView variant="popout" />
      </div>
    </div>
  )
}
