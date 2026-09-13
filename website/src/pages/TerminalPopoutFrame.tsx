import { useEffect, useRef, useState } from 'react'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import { registerPopout, closeSelfToMain } from '../utils/terminalPopout'
import { useBottomTerminal, openBottomTerminal } from '../hooks/useBottomTerminal'
import { disposeTerminalConnection } from '../utils/terminalRegistry'

import { i18nT } from '../i18n/t'

/**
 * The window shell for the popped-out terminal panel (`/popout/terminal`).
 *
 * Renders the shared tabbed terminal view (`TerminalTabsView popout`) filling
 * the whole window — its own JS context, so each tab's CliPanel opens a fresh
 * WebSocket to the still-live PTY and the backend replays that session's
 * scrollback. Tab membership comes from the canonical IndexedDB terminal
 * store; cross-window notifications trigger reads of committed state for
 * this window's captured session scope.
 *
 * On top it (1) registers as the live terminal popout so the main dashboard
 * can track / focus / bring it back, and (2) sets the OS window/taskbar title.
 * The "Return" control lives in the tab strip (rendered by the popout
 * variant), where the dock variant shows its pop-out control.
 */
export default function TerminalPopoutFrame() {
  // A popup belongs to its original chat even if another window selects a
  // different session or updates shared preferences.
  const [sessionScope] = useState(() => new URLSearchParams(window.location.search).get('sid'))
  const { tabs, epoch, retired, preparing } = useBottomTerminal(sessionScope)
  const ownedEpoch = useRef<number | null>(null)
  if (ownedEpoch.current === null && tabs.length) ownedEpoch.current = epoch
  const retiredView = retired || (ownedEpoch.current !== null && ownedEpoch.current !== epoch)
  const tabsRef = useRef(tabs)
  if (!retiredView) tabsRef.current = tabs

  // Release sockets BEFORE announcing close, so the main window cannot
  // reconnect while this document still owns them. Keep the server PTYs alive.
  useEffect(() => {
    const release = () => tabsRef.current.forEach(tab => disposeTerminalConnection(tab.id))
    window.addEventListener('beforeunload', release)
    window.addEventListener('pagehide', release)
    const unregister = registerPopout(sessionScope, release)
    return () => {
      release()
      window.removeEventListener('beforeunload', release)
      window.removeEventListener('pagehide', release)
      unregister()
    }
  }, [sessionScope])

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
    if (retiredView) { closeSelfToMain(); return }
    if (tabs.length > 0) { sawTabs.current = true; return }
    if (sawTabs.current) closeSelfToMain()
    else if (!preparing) openBottomTerminal(undefined, sessionScope)
  }, [tabs.length, sessionScope, epoch, retiredView, preparing])

  return (
    <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col relative">
      <div className="flex-1 min-h-0">
        {!retiredView && <TerminalTabsView variant="popout" sessionScope={sessionScope} />}
      </div>
    </div>
  )
}
