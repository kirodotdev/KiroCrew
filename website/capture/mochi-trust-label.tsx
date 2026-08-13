/**
 * Evidence for Mochi's approval-card exact-trust label (#4462).
 *
 * THE PROBLEM: Mochi carries a duplicate of the dashboard's
 * `truncateCommandLabel`, and its 30-char budget rendered two different
 * commands — `gh api …/contents/config.json` vs the same call for
 * `secrets.json` — as the SAME label, so the one line the user reads before
 * granting an exact-string trust could not tell them apart. The dashboard copy
 * is fixed by PR #4393; this is the Mochi sibling.
 *
 * The scene mounts the REAL ChatPanel Bubble, which parses a real
 * `__approval__` payload and calls the REAL `truncateCommandLabel`, against
 * Mochi's fallback palette (the documented no-stylesheet escape hatch in
 * shared/themes.ts). Nothing re-implements the card or its strings. The line
 * above the card is harness chrome, labelled as such, so each frame shows
 * which command produced the label.
 *
 *   ?cmd=api_config|api_secrets
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { Bubble } from '../src/apps/mochi/src/renderer/ChatPanel'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

/** Commands shaped like the ones in the customer report (#4436): a shared long
 *  prefix — repo slug and `contents/` segment — so the old 30-char budget
 *  truncated both to the same string. */
const COMMANDS = {
  api_config: 'gh api repos/owner/some-repository/contents/config.json --jq .sha',
  api_secrets: 'gh api repos/owner/some-repository/contents/secrets.json --jq .sha',
} as const

const params = new URLSearchParams(location.search)
const key = (params.get('cmd') ?? 'api_config') as keyof typeof COMMANDS
const cmd = COMMANDS[key] ?? COMMANDS.api_config

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

/** A permission frame as ChatPanel stores it: `__approval__` + the payload the
 *  approval route writes. fullCommand/baseCommand are what unlock the scoped
 *  trust rows whose exact-command label is under test; toolInput is included
 *  because real execute_bash frames carry it and the card renders it above the
 *  trust rows — omitting it would photograph a card shape the product never
 *  produces. */
const message = {
  id: 'cap-1',
  role: 'assistant' as const,
  content: '__approval__' + JSON.stringify({
    id: 'req-1',
    tool: 'execute_bash',
    toolInput: JSON.stringify({ command: cmd }),
    fullCommand: cmd,
    baseCommand: 'gh',
  }),
  timestamp: Date.now(),
}

/** 320 = BASE_PANEL_WIDTH (mochiApi.ts): the shipped chat column. The label is a
 *  width-sensitive change, so the evidence must render at the width the product
 *  actually has — padding is inside the 320 so the content box matches. */
createRoot(document.getElementById('root')!).render(
  <div data-capture-root style={{ background: 'var(--bg)', color: 'var(--text)', padding: 12, width: 320, boxSizing: 'border-box', display: 'flex', flexDirection: 'column', gap: 10 }}>
    {/* Harness chrome: names the command whose label is under test. */}
    <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'monospace', wordBreak: 'break-all' }}>
      <span>the agent wants to run: </span>{cmd}
    </div>
    <Bubble message={message} animate={false} />
  </div>,
)
