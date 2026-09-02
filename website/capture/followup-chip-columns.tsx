/**
 * Isolated capture + measurement entry for the follow-up option chips that
 * never form two columns (#5397).
 *
 * WHY ISOLATED: the defect IS layout. The chip cap is a max-width and the row is
 * `flex-wrap`, so whether two chips share a line is decided by Chromium's flex
 * line-breaking against a real container width — happy-dom computes none, which
 * is why the unit suite (src/test/FollowUpBar.test.tsx) can only pin the CSS
 * text and the class contract. A cap that is wider than half the row leaves
 * every class assertion passing and still stacks one chip per row.
 *
 * What is rebuilt here is only the box chain between the chat pane and the bar:
 * ChatPage sets `--mc-input-width` from CONTENT_WIDTH, and ChatInput's
 * `input-area` applies it with `px-4`. Those two are the container width the
 * percentage cap resolves against. The REAL FollowUpBar mounts inside it.
 *
 * `window.__measure()` reports the distinct row count (chips grouped by
 * offsetTop) and the chip width. `rows` is the whole assertion: 4 options that
 * render as 4 rows are the bug, 2 rows are the fix.
 *
 * `fix=off` reverts the cap to the pre-fix `min(100%, 26rem)`, so one harness
 * captures both sides and the before arm is asserted to reproduce rather than
 * assumed.
 *
 * Query string: ?width=compact&theme=dark&layout=multiline&fix=on
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import FollowUpBar from '../src/components/FollowUpBar'
import { CONTENT_WIDTH, type ContentWidth } from '../src/pages/chat/ChatSettings'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const contentWidth = (params.get('width') || 'compact') as ContentWidth
const layout = (params.get('layout') || 'multiline') as 'multiline' | 'scroll'
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-fix', fixOn ? 'on' : 'off')

/**
 * The pre-fix rule, verbatim. Reverting the same property the fix changes is
 * what makes the before arm a faithful revert rather than a differently-broken
 * page.
 */
const BEFORE_CSS = `
html[data-fix="off"] .followup-chip { max-width: min(100%, 26rem); }
`

/**
 * Four labels of the shape the bar actually receives — full user-voice
 * instructions, each past the ~60-char clamp threshold. Short labels cannot
 * demonstrate the defect: they size below the cap and already share a row.
 */
const OPTIONS = [
  'Design the throttle-exhaustion fallback for kiro-cli with the per-turn model record',
  'Show me what the Case 2.5 change would actually look like',
  'Leave kiro-cli failing loudly and just fix the Claude Code chain',
  'Tell me how often the mid-stream no-retry case has actually bitten us',
]

function Scene() {
  useEffect(() => {
    if (fixOn) return
    const style = document.createElement('style')
    style.textContent = BEFORE_CSS
    document.head.appendChild(style)
    return () => style.remove()
  }, [])

  return (
    // ChatPage.tsx sets --mc-input-width on the chat pane; ChatInput.tsx's
    // `input-area` consumes it with px-4. Both verbatim — they are the container
    // the percentage cap resolves against.
    <div
      className="bg-bg text-text flex flex-col justify-end min-h-screen"
      style={{ '--mc-input-width': CONTENT_WIDTH[contentWidth].input } as React.CSSProperties}
    >
      <div className="input-area px-4 pb-1 pt-1 mx-auto w-full flex flex-col" style={{ maxWidth: 'var(--mc-input-width, 900px)' }}>
        <div data-bar>
          <FollowUpBar options={OPTIONS} picked={new Set()} onSelect={() => {}} onSend={() => {}} layout={layout} />
        </div>
        {/* The composer box itself, for scale: the bar's cost is vertical space
            taken from the transcript directly above it. */}
        <div className="mt-1 rounded-2xl border border-border bg-bg-elevated px-3 py-3 text-[13px] text-muted">
          Message Kiro Crew… (/command · @file · $skill)
        </div>
      </div>
    </div>
  )
}

interface ChipMeasure {
  contentWidth: ContentWidth
  layout: string
  /** Inner width of `input-area` — what the percentage cap resolves against. */
  rowWidth: number
  chipWidth: number
  /** Distinct visual rows. 4 options in 4 rows is the defect, 2 is the fix. */
  rows: number
  /** Distinct columns. 2 is the whole point of the cap. */
  columns: number
  /** Total height the bar occupies above the composer. */
  barHeight: number
}

declare global {
  interface Window {
    __measure: () => ChipMeasure
  }
}

/**
 * Groups edge positions into buckets within `tolerance` px. A plain
 * `new Set(Math.round(...))` over-counts: the chips carry a staggered entrance
 * (`animate-chip-hop`) whose sub-pixel offsets survive rounding, and rows are
 * `items-end` so a two-line chip and its single-line neighbour share a baseline
 * without sharing a top.
 */
function bucket(values: number[], tolerance = 4): number {
  const sorted = [...values].sort((a, b) => a - b)
  let groups = 0
  let anchor = Number.NEGATIVE_INFINITY
  for (const v of sorted) {
    if (v - anchor > tolerance) {
      groups++
      anchor = v
    }
  }
  return groups
}

window.__measure = () => {
  const bar = document.querySelector<HTMLElement>('[data-bar]')!
  // The flex item is the split-button wrapper (a send segment is present), so
  // measure that rather than the inner button.
  const chips = Array.from(bar.querySelectorAll<HTMLElement>('span.followup-chip'))
  const row = bar.querySelector<HTMLElement>('.flex')!
  const rects = chips.map(c => c.getBoundingClientRect())
  return {
    contentWidth,
    layout,
    rowWidth: Math.round(row.getBoundingClientRect().width),
    chipWidth: rects.length ? Math.round(rects[0].width) : 0,
    // Bottom edges, because the row is `items-end`.
    rows: bucket(rects.map(r => r.bottom)),
    columns: bucket(rects.map(r => r.left)),
    barHeight: Math.round(bar.getBoundingClientRect().height),
  }
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
