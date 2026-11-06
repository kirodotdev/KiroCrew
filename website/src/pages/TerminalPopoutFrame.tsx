import { useEffect, useRef } from 'react'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import StatusNotice from '../components/StatusNotice'
import { Btn } from '../components/ui'
import { registerPopout, returnSelfToMain } from '../utils/terminalPopout'
import { useAnyTerminalDisabledByConfig } from '../utils/terminalRegistry'
import { useBottomTerminal, openBottomTerminal } from '../hooks/useBottomTerminal'
import { useTerminalEnabledQuery } from '../shell/boot/terminalRestore'

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
 *
 * The off state is read from the enabled query and the registry's
 * explicit-false mark, never from the registry flag: that flag starts false
 * and is raised only by the main shell's mirror effect, which this window
 * does not run.
 */
export default function TerminalPopoutFrame() {
  const { tabs } = useBottomTerminal()
  const { data, isError } = useTerminalEnabledQuery()
  const anyOff = useAnyTerminalDisabledByConfig()
  const off = data?.enabled === false || anyOff
  const settled = data !== undefined || isError

  // Announce presence and wire the heartbeat/control responder.
  useEffect(() => registerPopout(), [])

  useEffect(() => {
    document.title = i18nT('pages.terminalPopoutFrame.window_title', {
      label: i18nT('pages.terminalPopoutFrame.terminal'),
    })
  }, [])

  // A deep-linked popout with no tabs mints one (an empty terminal window is
  // useless) once the probe has answered enabled (a failed probe reads
  // enabled); afterwards, closing the LAST tab returns the panel to the main
  // window instead of leaving an empty shell behind. While the terminal is off
  // this window does nothing: the notice stays until the owner turns it on.
  const sawTabs = useRef(false)
  useEffect(() => {
    if (tabs.length > 0) sawTabs.current = true
    if (!settled || off) return
    if (tabs.length > 0) return
    if (sawTabs.current) returnSelfToMain()
    else openBottomTerminal()
  }, [tabs.length, off, settled])

  return (
    <div className="h-screen w-screen overflow-hidden bg-bg flex flex-col relative">
      <div className="flex-1 min-h-0">
        {off ? (
          <div className="p-4 flex flex-col items-start gap-3">
            <StatusNotice message={i18nT('pages.terminalPopoutFrame.terminal_off')} testId="terminal-popout-off" />
            <Btn type="button" onClick={returnSelfToMain}>
              {i18nT('pages.terminalPopoutFrame.back_to_dashboard')}
            </Btn>
          </div>
        ) : (
          <TerminalTabsView variant="popout" />
        )}
      </div>
    </div>
  )
}
