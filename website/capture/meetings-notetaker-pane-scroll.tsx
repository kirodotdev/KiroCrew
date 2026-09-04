/**
 * Visual + measured evidence for "the note-taker output pane scrolls" (#7664).
 *
 * WHY ISOLATED: the defect is pure CSS cascade — `.card-glow{overflow:hidden}`
 * is declared after `@tailwind utilities`, so it beats an `overflow-y-auto`
 * utility on the same element. happy-dom computes no layout, so only a real
 * browser can prove the pane actually scrolls (the unit test in
 * src/test/MeetingsAgentPanel.test.tsx pins the class shape; this harness pins
 * the rendered behaviour).
 *
 * WHAT IS FAITHFUL: the REAL AgentPanel with a markdown note-taker agent and a
 * long generated document, with the real index.css loaded — the cascade under
 * test is the production cascade.
 *
 * The `before` scene neutralizes the fix via an injected stylesheet: the inner
 * pane's own bound and scroller are disabled and the 520px cap is re-imposed on
 * the Card, whose card-glow `overflow:hidden` then clips it — precisely what
 * the pre-fix code rendered (max-h + overflow-y-auto on the Card, both defeated
 * by card-glow). `after` is the current code, untouched.
 *
 *   ?scene=before|after &theme=dark|light
 *
 * Two shells, from website/:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort
 *   node scripts/capture-meetings-notetaker-pane-scroll.mjs http://127.0.0.1:6817 \
 *     ../temp-screenshots/meetings-notetaker-scroll
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import AgentPanel from '../src/apps/meetings/components/AgentPanel'
import type { AgentDef } from '../src/apps/meetings/api'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'before' ? 'before' : 'after'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

initI18n()

// `before` = the pre-fix rendering: the scroll request lived on the Card, where
// card-glow's overflow:hidden wins the cascade. Re-imposing the cap on the Card
// and disabling the inner pane's bound reproduces exactly that: content clipped
// at 520px, nothing scrollable.
if (scene === 'before') {
  const style = document.createElement('style')
  style.textContent = `
    [data-testid="agent-output-pane"] { max-height: none !important; overflow: visible !important; }
    .card-glow { max-height: 520px; }
  `
  document.head.appendChild(style)
}

const NOTE_TAKER: AgentDef = { id: 'note-taker', name: 'Note Taker', widget_type: 'markdown' }

const LONG_NOTES =
  '# Standup — long notes\n\n' +
  Array.from(
    { length: 40 },
    (_, i) =>
      `## Topic ${i + 1}\n\n- Decision recorded for topic ${i + 1}.\n- Action item assigned with a due date.\n`,
  ).join('\n') +
  '\n## END OF NOTES — only reachable by scrolling\n'

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="p-6 max-w-[720px] mx-auto grid grid-cols-2 gap-3 bg-bg min-h-screen">
    <AgentPanel
      agent={NOTE_TAKER}
      output={LONG_NOTES}
      listening={true}
      chatView={false}
      onToggleListening={() => undefined}
      onToggleChatView={() => undefined}
      onSendMessage={() => undefined}
    />
  </div>,
)
