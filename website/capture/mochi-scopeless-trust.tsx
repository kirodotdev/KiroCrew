/**
 * Evidence for Mochi's approval card when the gateway proves a SESSION grant but
 * no command scope.
 *
 * On that card there is no tier list to open, so the single Trust click IS the
 * broadest grant. The button therefore names the scope it grants, and the hint
 * under it describes the session rather than the pending tool: a label reading
 * only "Trust" beside one tool name reads as trusting that tool, while the click
 * auto-approves every later tool in the session.
 *
 * ONE scene, and it is real. There is no before frame here on purpose: the
 * component renders only the current copy, so an old-state frame would have to be
 * hand-drawn in this harness, and a mock is not evidence.
 *
 * The scene mounts the REAL ChatPanel Bubble, which parses a real `__approval__`
 * payload and renders the real buttons and the real catalog strings, against
 * Mochi's fallback palette (the documented no-stylesheet escape hatch in
 * shared/themes.ts). Nothing re-implements the card or its copy.
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { Bubble } from '../src/apps/mochi/src/renderer/ChatPanel'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

/** A proven card with NO command scope: the transport redacted the command or the
 *  gateway could not canonicalize it, so `fullCommand` and `baseCommand` are
 *  absent while `trustGrantable` still holds. */
const PAYLOAD = {
  id: 'req-capture',
  tool: 'Run hidden command',
  toolInput: 'cd ~/.kiro/crew/workspace/docs/structured-payments',
  trustGrantable: true,
}

const message = {
  id: 'approval-capture',
  role: 'assistant' as const,
  content: `__approval__${JSON.stringify(PAYLOAD)}`,
  timestamp: 1,
}

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text p-5 w-[420px] flex flex-col gap-3">
    {/* Harness chrome: names the state that produced this card. */}
    <div className="text-[11px] text-muted font-mono break-all">
      <span className="not-italic text-subtle">
        gateway proved a session grant; the command could not be derived
      </span>
    </div>
    <Bubble animate={false} message={message} onApproval={() => {}} />
  </div>,
)
