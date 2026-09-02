/**
 * Isolated capture + measurement entry for the user bubble that hangs off the
 * LEFT edge of a phone viewport when it contains a fenced code block.
 *
 * WHY ISOLATED: the defect needs real layout at 320-390px, which happy-dom
 * cannot compute, and the whole chat page would need a gateway and an open slot
 * that are no part of it. What IS part of it is the box chain between the
 * transcript's content column and the bubble, so this rebuilds that chain with
 * the literal classes both hosts use and mounts the REAL UserMessage in it, fed
 * by the REAL MarkdownRenderer.
 * window.__measure() reports the bubble's width and its left edge against the
 * viewport. `unreachableLeft` is the whole assertion: pixels left of 0 cannot be
 * scrolled back into view. `fix=off` reverts the caps to the BEFORE state, so one
 * harness captures both sides. Query string: ?scene=plain&theme=dark&w=390
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n'
import UserMessage from '../src/pages/chat/UserMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const width = parseInt(params.get('w') || '390', 10)
const scene = params.get('scene') || 'plain'
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-fix', fixOn ? 'on' : 'off')

/** A message whose fenced block's max-content width far exceeds any phone. */
const CONTENT = [
  'Preflight output follows.',
  '',
  '```',
  'CR GATES  verdict=BLOCKED',
  "  1. [gate] sandbox: '/a/very/long/absolute/path/that/will/not/wrap/SomePackageCDK' is not this package's own test module",
  '  2. [gate] verdict: no verdict applied at the current revision',
  '```',
].join('\n')

/**
 * Reverts the two caps that live inside UserMessage. Resetting the same
 * properties the fix sets is what makes the before arm a faithful revert rather
 * than a differently-broken page.
 */
const BEFORE_CSS = `
html[data-fix="off"] [data-role="user"] { max-width: none; }
html[data-fix="off"] [data-role="user"] .relative { max-width: none; }
html[data-fix="off"] .message-bubble { max-width: 550px; }
html[data-fix="off"] .edit-grow { max-width: 550px; }
`

function Scene() {
  useEffect(() => {
    if (fixOn) return
    const style = document.createElement('style')
    style.textContent = BEFORE_CSS
    document.head.appendChild(style)
    return () => style.remove()
  }, [])

  const capped = fixOn ? 'max-w-full ' : ''
  return (
    // The outer frame is overflow-hidden and the scroller only scrolls on Y,
    // which is why leftward overflow has nowhere to go.
    <div className="bg-bg text-text flex flex-col overflow-hidden" style={{ width, height: 560 }}>
      <div className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden">
        <div className="flex-1 overflow-y-auto overflow-x-hidden">
          {/* The transcript content column, verbatim from both hosts. */}
          <div className="px-5 mx-auto w-full py-1" style={{ maxWidth: 800 }}>
            {/* ChatPage.tsx / ChatMessageList.tsx row + wrapper pair. */}
            <div className="group flex flex-col min-w-0 items-end">
              <div data-wrapper className={`flex flex-col gap-0.5 min-w-0 overflow-hidden ${capped}items-end`}>
                <UserMessage
                  content={CONTENT}
                  meta={scene === 'steer' ? { steer: true } : undefined}
                  timestamp="12:34"
                  canEdit={scene === 'edit'}
                  messageIndex={0}
                  messageTs="1700000000.0"
                  onEditResend={scene === 'edit' ? () => {} : undefined}
                  renderContent={(c: string) => <MarkdownRenderer content={c} softBreaks />}
                />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

interface BubbleMeasure {
  scene: string
  viewportWidth: number
  /** The bubble under test: the read-only bubble, or the edit box in `edit`. */
  bubbleWidth: number
  bubbleLeft: number
  bubbleRight: number
  /** Pixels left of the viewport origin. No scroll can reach these. */
  unreachableLeft: number
  /** A document that does not scroll horizontally is what makes them lost. */
  docScrollWidth: number
  hasHorizontalScroll: boolean
  /** The `<pre>` keeps its own scroll — the fix must not take that away. */
  preScrolls: boolean
}

declare global {
  interface Window {
    __measure: () => BubbleMeasure
  }
}

window.__measure = () => {
  const root = document.querySelector<HTMLElement>('[data-role="user"]')!
  const bubble = root.querySelector<HTMLElement>('.message-bubble, .edit-grow')!
  const rect = bubble.getBoundingClientRect()
  const pre = bubble.querySelector<HTMLElement>('pre')
  return {
    scene,
    viewportWidth: window.innerWidth,
    bubbleWidth: Math.round(rect.width),
    bubbleLeft: Math.round(rect.left),
    bubbleRight: Math.round(rect.right),
    unreachableLeft: Math.max(0, Math.round(-rect.left)),
    docScrollWidth: document.documentElement.scrollWidth,
    hasHorizontalScroll: document.documentElement.scrollWidth > window.innerWidth,
    preScrolls: !!pre && pre.scrollWidth > pre.clientWidth,
  }
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <Scene />
  </MemoryRouter>,
)
