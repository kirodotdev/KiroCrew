/**
 * Isolated capture + measurement entry for the reply-thread footer that floats
 * on the far right of a user bubble CLI mode has moved to the left.
 *
 * WHY ISOLATED: the defect is a cascade outcome -- `align-self` on the footer
 * beating `align-items` on the row wrapper -- and happy-dom computes neither,
 * so the unit guards (src/test/cliModeThreadFooter.test.ts) can only pin the
 * rule's source text. Whether the rule actually WINS, and where the footer
 * lands once it does, is observable only in a real engine. The whole chat page
 * would need a gateway, an open slot and a crewmate thread that are no part of
 * the bug; what IS part of it is the box chain between the transcript's content
 * column and the footer, so this rebuilds that chain with the literal classes
 * ChatMessageList emits and mounts the REAL UserMessage, AssistantMessage and
 * ThreadFooter inside it.
 *
 * `window.__measure()` reports each footer's first painted mark against its own
 * bubble's box, which is the edge a reader lines it up with (the hover row's
 * icons sit on that same edge). `fix=off` reverts the cli-mode rule to the
 * BEFORE state, so one harness captures both sides and the before frame is
 * asserted to reproduce rather than assumed to.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort   # in another shell
 *   node scripts/capture-thread-footer-cli-align.mjs http://127.0.0.1:6814 <outdir>
 *
 * Query string: ?ui=cli&theme=dark&fix=on
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n'
import UserMessage from '../src/pages/chat/UserMessage'
import AssistantMessage from '../src/pages/chat/AssistantMessage'
import ThreadFooter from '../src/pages/chat/ThreadFooter'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'
// main.tsx imports this separately from index.css, so the capture must too --
// without it `data-ui="cli"` would be an inert attribute and both arms would
// draw the normal theme, which is exactly the false pass to avoid.
import '../src/styles/cli-mode.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const ui = params.get('ui') || 'cli'
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
if (ui === 'cli') document.documentElement.setAttribute('data-ui', 'cli')
document.documentElement.setAttribute('data-fix', fixOn ? 'on' : 'off')

/** The reported row: four replies, the last a couple of minutes ago. Relative to
 *  NOW rather than a fixed instant, so the frame never reads "in 9h" — the
 *  wording is not under test, but evidence a reader has to discount is worse
 *  evidence. */
const SUMMARY = {
  count: 4,
  last_reply_ts: new Date(Date.now() - 2 * 60 * 1000).toISOString(),
  participants: ['user', 'assistant'],
}

/**
 * Reverts BOTH cli-mode footer rules, so the before frame is a faithful revert
 * rather than a page that is half fixed. The values restored are the ones the
 * footer's own `self-end -mr-1.5` / `self-start -ml-1.5` utilities carry, which
 * is the state before either rule existed.
 */
const BEFORE_CSS = `
html[data-fix="off"][data-ui="cli"] [style*="--mc-content-width"] .items-end:has([data-role="user"]) > [data-testid="thread-footer"] {
  align-self: flex-end !important;
  margin-left: 0 !important;
  margin-right: -0.375rem !important;
}
html[data-fix="off"][data-ui="cli"] [style*="--mc-content-width"] :has(> [data-role="assistant"]) > [data-testid="thread-footer"] {
  margin-left: -0.375rem !important;
}
`

function Scene() {
  useEffect(() => {
    if (fixOn) return
    const style = document.createElement('style')
    style.textContent = BEFORE_CSS
    document.head.appendChild(style)
    return () => style.remove()
  }, [])

  return (
    <div className="bg-bg text-text flex flex-col overflow-hidden" style={{ width: 900, height: 460 }}>
      <div
        className="flex-1 overflow-y-auto overflow-x-hidden"
        style={{ ['--mc-content-width' as string]: '820px' }}
      >
        {/* ChatMessageList.renderMessage's `wrapper(children, true)` -- the user
            row's content column, group row and inner column, verbatim. The
            footer is a SIBLING of the bubble inside the inner column, which is
            why the wrapper's alignment is meant to be what places it. */}
        <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 820px)' }}>
          <div className="group flex flex-col min-w-0 items-end">
            <div className="flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full items-end">
              <UserMessage
                content={'CREW-18723 keep the crew context in our own hands, do not let it compact'}
                timestamp="12:48 PM"
                messageIndex={0}
                messageTs="1700000000.0"
                renderContent={(c: string) => <MarkdownRenderer content={c} softBreaks />}
              />
              <ThreadFooter summary={SUMMARY} crewmateName="Radar" align="end" onOpen={() => {}} />
            </div>
          </div>
        </div>
        {/* renderAssistantBubble's own column: `flex flex-col gap-0`, whose
            default `align-items: stretch` is what the footer's `self-start`
            escapes. Reproduced so a fix that drops that class shows up here as a
            full-width button instead of passing unseen. */}
        <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 820px)' }}>
          <div className="flex flex-col gap-0">
            <AssistantMessage
              content={'Understood -- the ledger holds the state, so a compaction costs nothing.'}
              isStreaming={false}
              timestamp="12:49 PM"
            />
            <ThreadFooter summary={SUMMARY} crewmateName="Radar" align="start" onOpen={() => {}} />
          </div>
        </div>
      </div>
    </div>
  )
}

interface SideMeasure {
  /** Left edge of the footer's first painted mark (the faces), padding excluded. */
  markLeft: number
  /** Right edge of its last mark -- the counterpart for a right-aligned row. */
  markRight: number
  footerLeft: number
  footerRight: number
  /** The bubble box the footer is read against; the hover row sits on this edge. */
  bubbleLeft: number
  bubbleRight: number
  /** The defect, as one number: how far the footer's mark sits from that edge. */
  offsetFromBubble: number
  /** The same, measured from the right, for a row aligned that way. */
  offsetFromBubbleRight: number
  /** Resolved `align-self`, so a rule that lost the cascade is visible as data. */
  alignSelf: string
  /** A stretched footer spans its column; the button must stay shrink-wrapped. */
  footerWidth: number
  columnWidth: number
}

declare global {
  interface Window {
    __measure: () => { ui: string, fix: string, user: SideMeasure, assistant: SideMeasure }
  }
}

function measureSide(role: 'user' | 'assistant'): SideMeasure {
  const root = document.querySelector<HTMLElement>(`[data-role="${role}"]`)!
  const column = root.parentElement!
  const footer = column.querySelector<HTMLElement>('[data-testid="thread-footer"]')!
  const bubble = root.querySelector<HTMLElement>('.msg-content')!
  const f = footer.getBoundingClientRect()
  const b = bubble.getBoundingClientRect()
  // The faces span is the footer's first painted content, so its own left edge
  // is what a reader compares against the bubble -- not the button box, whose
  // padding and negative margin are deliberately outside the visible mark.
  const marks = footer.querySelectorAll<HTMLElement>(':scope > *')
  const mark = marks[0].getBoundingClientRect()
  // A right-aligned footer is read against the bubble's RIGHT edge, so the last
  // mark's own right edge is the counterpart number. Measuring only the left one
  // would call the normal theme's correct right placement a 336px error.
  const markEnd = marks[marks.length - 1].getBoundingClientRect()
  return {
    markLeft: Math.round(mark.left),
    markRight: Math.round(markEnd.right),
    footerLeft: Math.round(f.left),
    footerRight: Math.round(f.right),
    bubbleLeft: Math.round(b.left),
    bubbleRight: Math.round(b.right),
    offsetFromBubble: Math.round(mark.left - b.left),
    offsetFromBubbleRight: Math.round(markEnd.right - b.right),
    alignSelf: getComputedStyle(footer).alignSelf,
    footerWidth: Math.round(f.width),
    columnWidth: Math.round(column.getBoundingClientRect().width),
  }
}

window.__measure = () => ({
  ui,
  fix: fixOn ? 'on' : 'off',
  user: measureSide('user'),
  assistant: measureSide('assistant'),
})

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <Scene />
  </MemoryRouter>,
)
