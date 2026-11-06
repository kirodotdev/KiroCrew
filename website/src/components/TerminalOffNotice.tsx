import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useBottomTerminal } from '../hooks/useBottomTerminal'
import StatusNotice from './StatusNotice'
import { settingsPath } from './settingsPath'

import { i18nT } from '../i18n/t'

export const TERMINAL_SWITCH_ROUTE = settingsPath({
  tab: 'display',
  sub: 'terminal',
  highlight: 'key:dashboard.terminal.enabled',
})

// Per page load: the strip returns on reload while the panel stays off, and
// dismissing it leaves the restored tabs for the owner who turns it back on.
let dismissed = false

/**
 * The strip that takes the docked terminal's slot while the gateway says the
 * terminal is turned off by configuration (`dashboard.terminal.enabled`) and the
 * tab store still holds tabs: the one place the hidden dock is explained. Zero
 * tabs renders nothing, like any other feature that is simply off.
 */
export default function TerminalOffNotice() {
  const { tabs } = useBottomTerminal()
  const [, rerender] = useState(0)
  if (tabs.length === 0 || dismissed) return null
  return (
    <div className="shrink-0 flex flex-wrap items-center gap-2 px-3 py-1.5 border-t border-border bg-bg" data-testid="terminal-off-notice">
      <StatusNotice
        className="min-w-0 flex-1"
        message={i18nT('components.terminalOffNotice.message')}
        onDismiss={() => { dismissed = true; rerender(n => n + 1) }}
      />
      <Link
        to={TERMINAL_SWITCH_ROUTE}
        className="shrink-0 inline-flex items-center px-2.5 py-1 rounded-md border border-border text-[13px] text-text no-underline hover:border-border-strong hover:bg-bg-hover"
      >
        {i18nT('components.terminalOffNotice.open_settings')}
      </Link>
    </div>
  )
}
