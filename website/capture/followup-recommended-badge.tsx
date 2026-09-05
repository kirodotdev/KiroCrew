/**
 * Isolated capture + measurement entry for the `(recommended)` badge.
 *
 * WHY ISOLATED: the defect IS layout. Whether the marker survives depends on
 * where Chromium puts the ellipsis, which depends on the font, the chip's real
 * box and the container width — happy-dom computes none of it, so the unit suite
 * (src/test/FollowUpBar.test.tsx) can only pin the DOM contract: badge outside
 * the truncating span. That contract is necessary and not sufficient. A badge
 * rendered outside the span but pushed past the chip's right edge would satisfy
 * every class assertion and still be invisible.
 *
 * So the measurement here is the user-visible question and nothing softer: is
 * the marker inside the painted box? `__measure()` answers it by taking a Range
 * over the marker's own characters (before arm) or the badge element (after arm)
 * and comparing its rect against the chip's content edge.
 *
 * `fix=off` renders exactly what upstream renders — marker left inside the label,
 * no `recommended` prop — so the before arm is a faithful reproduction rather
 * than a differently-broken page, and the defect is asserted rather than assumed.
 *
 * Query string: ?width=compact&theme=dark&fix=on
 */
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import FollowUpBar from '../src/components/FollowUpBar'
import { parseOptions } from '../src/app-sdk/protocol/options'
import { CONTENT_WIDTH, type ContentWidth } from '../src/pages/chat/ChatSettings'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const contentWidth = (params.get('width') || 'compact') as ContentWidth
const fixOn = params.get('fix') !== 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/**
 * Labels as the agent actually writes them, marker included — the shape this
 * change is about. The first is long enough to truncate, which is the whole point:
 * the badge has to survive a label whose tail is clipped away. Its marker LEADS,
 * because that is the only form the grammar admits -- a trailing marker is not
 * recognised at all, so a fixture emitting one would render no badge and measure
 * nothing. The last is the ordering variant.
 */
const RAW = [
  '(recommended) Start the walk with the 4 badged items in board order',
  'Walk all 15 in board order, one per turn',
  'Fix the duplicate tab first',
]

/** After arm: the parser's own output, so this harness cannot describe a shape the
 *  parser stopped returning. Before arm: verbatim, as upstream leaves it. */
const parsed = parseOptions(`[OPTIONS: ${RAW.join(' | ')}]`)
const options = fixOn ? parsed.options : RAW
const recommended = fixOn ? parsed.recommended : undefined

function Scene() {
  return (
    // The box chain between the chat pane and the bar, verbatim from ChatPage
    // (--mc-input-width) and ChatInput (`input-area` + px-4): together they are
    // the container width the chip's percentage cap resolves against.
    <div
      className="bg-bg text-text flex flex-col justify-end min-h-screen"
      style={{ '--mc-input-width': CONTENT_WIDTH[contentWidth].input } as React.CSSProperties}
    >
      <div className="input-area px-4 pb-1 pt-1 mx-auto w-full flex flex-col" style={{ maxWidth: 'var(--mc-input-width, 900px)' }}>
        <div data-bar>
          <FollowUpBar options={options} recommended={recommended} picked={new Set()} onSelect={() => {}} onSend={() => {}} />
        </div>
        <div className="mt-1 rounded-2xl border border-border bg-bg-elevated px-3 py-3 text-[13px] text-muted">
          Message Kiro Crew… (/command · @file · $skill)
        </div>
      </div>
    </div>
  )
}

interface BadgeMeasure {
  fix: 'on' | 'off'
  contentWidth: ContentWidth
  /** Is the label truncated at all? A label that fits proves nothing either way. */
  labelClipped: boolean
  /**
   * Is the word "recommended" inside the painted box of its chip? This is the
   * user-visible question, and the only assertion that discriminates the arms.
   */
  markerVisible: boolean
  /** Present only in the after arm — the badge element itself. */
  badgeCount: number
  markerInLabel: boolean
}

declare global {
  interface Window {
    __measure: () => BadgeMeasure
  }
}

/** The rect of the first occurrence of `needle` inside `el`'s text, or null. */
function textRect(el: Element, needle: string): DOMRect | null {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const idx = (node.textContent || '').indexOf(needle)
    if (idx < 0) continue
    const range = document.createRange()
    range.setStart(node, idx)
    range.setEnd(node, idx + needle.length)
    return range.getBoundingClientRect()
  }
  return null
}

window.__measure = () => {
  const bar = document.querySelector<HTMLElement>('[data-bar]')!
  // The chip whose label is long enough to truncate — the first option.
  const label = bar.querySelector<HTMLElement>('.truncate')!
  const chip = label.closest<HTMLElement>('.followup-chip') ?? label.parentElement!
  const chipRect = chip.getBoundingClientRect()

  // A rect of all-zeros means the range is in clipped-away text, so treat a
  // zero-width rect as not painted rather than as sitting at the origin.
  //
  // BOTH axes, deliberately. The label truncates with `white-space: nowrap` +
  // `overflow: hidden`, so an over-long label now runs off to the RIGHT and is
  // clipped at the chip's right edge — a horizontal test is the load-bearing
  // half today. The vertical half is kept because it costs nothing and this
  // measurement has already survived one change of clipping mechanism: the
  // label previously used `line-clamp-1`, which clipped DOWNWARD onto a second
  // line, and a horizontal-only test reported the marker visible when it was
  // not. Testing the marker's rect against the chip's box on both axes is
  // correct under either mechanism, so it does not need revisiting next time.
  const painted = (r: DOMRect | null) =>
    !!r && r.width > 0
    && r.right <= chipRect.right + 1 && r.left >= chipRect.left - 1
    && r.bottom <= chipRect.bottom + 1 && r.top >= chipRect.top - 1

  const badges = Array.from(bar.querySelectorAll<HTMLElement>('span.border-accent\\/30'))
    .filter(n => /recommended/.test(n.textContent || ''))

  return {
    fix: fixOn ? 'on' : 'off',
    contentWidth,
    // HORIZONTAL: `truncate` is nowrap, so the label never wraps and its
    // overflow is along the inline axis. A height comparison would read equal
    // forever here and assert nothing.
    labelClipped: label.scrollWidth > label.clientWidth,
    markerVisible: fixOn
      ? badges.some(b => painted(b.getBoundingClientRect()))
      : painted(textRect(label, 'recommended')),
    // The harm the badge removes: a leading marker is never clipped, so the defect is
    // that the marker is part of the LABEL, and the label is what a click dispatches.
    markerInLabel: /recommended/i.test(label.textContent || ''),
    badgeCount: badges.length,
  }
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
