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
 *   ?cmd=api_config|api_secrets|spaced
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
  // A quoted argument carrying a RUN of whitespace (#4700). The row grants an
  // exact-STRING match, and HTML collapses runs by default, so without
  // `whiteSpace: 'pre-wrap'` this renders as the one-space command while
  // granting the two-space one. Only a real browser shows it: the DOM text is
  // exact either way.
  spaced: 'grep -r "two  spaces" /path/to/dir',
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
 *  produces. `trustGrantable` is the server's proof that a standing grant can be
 *  recorded: the card withholds EVERY trust control without it (#5400/#5434),
 *  so a payload missing it photographs a card with no trust rows at all. */
const message = {
  id: 'cap-1',
  role: 'assistant' as const,
  content: '__approval__' + JSON.stringify({
    id: 'req-1',
    tool: 'execute_bash',
    toolInput: JSON.stringify({ command: cmd }),
    fullCommand: cmd,
    // The command's OWN binary, not a hard-coded one: the card renders a family
    // row beside the exact row ("Trust all gh commands"), and a base that does
    // not match the request photographs a card the product never produces --
    // a cold reader cannot connect the two rows and refuses both.
    baseCommand: cmd.split(/\s+/)[0],
    trustGrantable: true,
  }),
  timestamp: Date.now(),
}

/** 320 = BASE_PANEL_WIDTH (mochiApi.ts): the shipped chat column. The label is a
 *  width-sensitive change, so the evidence must render at the width the product
 *  actually has — padding is inside the 320 so the content box matches. */
createRoot(document.getElementById('root')!).render(
  <div data-capture-root style={{ background: 'var(--bg)', color: 'var(--text)', padding: 12, width: 320, boxSizing: 'border-box', display: 'flex', flexDirection: 'column', gap: 10 }}>
    {/* Harness chrome: names the command whose label is under test.
        whiteSpace:'pre-wrap' for the same reason the row under test carries it —
        a chrome line that collapses a run the row keeps makes the two disagree
        in the frame, and a reader cannot tell which one is wrong. */}
    <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: 'monospace', wordBreak: 'break-all', whiteSpace: 'pre-wrap' }}>
      <span>the agent wants to run: </span>{cmd}
    </div>
    <Bubble message={message} animate={false} />
  </div>,
)
