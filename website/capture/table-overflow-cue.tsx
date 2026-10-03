import { createRoot } from 'react-dom/client'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

// Mounts the REAL MarkdownRenderer (and so the real MarkdownTable with its
// `mask-image` overflow fade) at a phone width, with a table wide enough that
// eight columns overflow — the issue's own case. No hand-written cue markup:
// the fade here is exactly the one that ships, so the screenshot is evidence of
// the component, not of a copy of it. `window.__sync()` lets the runner force a
// remeasure after it sets scrollLeft, since jsdom-free Chromium does lay out.
const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'light'
document.documentElement.dataset.theme = `kiro-${theme}`
document.documentElement.dataset.mode = theme

// Eight columns so the row overflows a 390px phone; a mid-glyph clip is what
// makes the fade legible, so the headers are ordinary words, not short codes.
const WIDE = [
  '| Symbol | Price | Change | MACD Hist | Signal | RSI | Volume | Overall |',
  '| --- | --- | --- | --- | --- | --- | --- | --- |',
  '| GOOGL | $344.82 | -1.4% | -0.57 | crossunder | 38.2 | 1.2M | STRONG SELL |',
  '| MSFT | $512.09 | +0.8% | +0.21 | crossover | 61.5 | 2.8M | BUY |',
].join('\n')

// A narrow table (?fits) that fits a phone, to show neither edge fades.
const NARROW = '| Symbol | Price |\n| --- | --- |\n| GOOGL | $344.82 |'

function Scene() {
  const md = params.has('fits') ? NARROW : WIDE
  return (
    <div className="bg-bg text-text min-h-screen p-3">
      <p>Before the table.</p>
      <MarkdownRenderer content={md} />
      <p>After the table.</p>
    </div>
  )
}

await initI18n()
createRoot(document.getElementById('root')!).render(<Scene />)
// The scroller is MarkdownTable's `data-testid="table-scroller"`; the runner
// scrolls it and the component's own scroll listener remeasures. This hook is
// only a convenience for forcing an immediate layout read in the runner.
;(window as unknown as { __scroller: () => HTMLElement | null }).__scroller =
  () => document.querySelector('[data-testid="table-scroller"]')
