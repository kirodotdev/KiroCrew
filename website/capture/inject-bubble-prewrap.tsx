/**
 * Isolated capture entry for the inject bubble's preserved-whitespace conflict.
 *
 * Isolated because the defect is CSS inheritance into markdown block layout, so
 * the app shell adds nothing the measurement uses. What must be real is the
 * markdown renderer and the compiled stylesheet, so both are imported.
 *
 * Scenes, from the query string:
 *   ?scene=before — the container classes as they shipped, with pre-wrap.
 *   ?scene=after  — the same minus pre-wrap, plus softBreaks.
 *
 * `before` is built from the shipping class list, so it reproduces the defect
 * whether or not the source is fixed. Theme: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const scene = params.get('scene') === 'after' ? 'after' : 'before'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/**
 * The shipping container classes, as a list so the delta under test is one
 * entry — a hand-edited second string could drift and measure two changes.
 */
const BASE_CLASSES = [
  'msg-content', 'px-4', 'py-3', 'text-sm', 'leading-6',
  'rounded-lg', 'bg-warn-subtle', 'text-text', 'ring-1', 'ring-inset',
  'forced-colors:border', 'ring-warn/30', 'rounded-bl-[4px]', 'overflow-hidden', 'min-w-0',
]
const PRE_WRAP = 'whitespace-pre-wrap'

/** A note body shaped like the one that surfaced this: prose, then a wide table. */
const MD = `## Release friction summary

Rows below are the pipelines whose manual-intervention load exceeded the goal.

| Pipeline | Stage | Interventions | Median delay | Owner tier |
| --- | --- | --- | --- | --- |
| billing-core | prod | 14 | 3h 20m | tier-1 |
| billing-core | preprod | 9 | 1h 05m | tier-1 |
| ledger-sync | prod | 12 | 4h 41m | tier-1 |
| ledger-sync | preprod | 6 | 0h 52m | tier-2 |
| notify-fanout | prod | 11 | 2h 13m | tier-2 |
| notify-fanout | preprod | 4 | 0h 31m | tier-2 |
| search-index | prod | 18 | 6h 02m | tier-1 |
| search-index | preprod | 7 | 1h 44m | tier-2 |
| media-transcode | prod | 8 | 2h 58m | tier-3 |
| media-transcode | preprod | 3 | 0h 22m | tier-3 |
| auth-edge | prod | 15 | 5h 11m | tier-1 |
| auth-edge | preprod | 5 | 0h 47m | tier-2 |
| report-rollup | prod | 10 | 3h 36m | tier-2 |
| report-rollup | preprod | 2 | 0h 18m | tier-3 |
| cache-warm | prod | 13 | 4h 09m | tier-2 |
| cache-warm | preprod | 6 | 1h 12m | tier-3 |

Every tier-1 row above is over the one-intervention-per-28-days goal.`

/** A plain multi-line notification: the case `softBreaks` has to keep intact. */
const PROSE = `Nightly adapter audit finished.
3 adapters probed, 0 regressions.
Next run: tomorrow 02:00 UTC.`

function Scene() {
  const className = scene === 'before'
    ? [...BASE_CLASSES.slice(0, 5), PRE_WRAP, ...BASE_CLASSES.slice(5)].join(' ')
    : BASE_CLASSES.join(' ')
  // Part of the fix rather than of the container, so it rides with `after`.
  const softBreaks = scene === 'after'

  return (
    <div data-capture-root style={{ width: 820, padding: 24 }}>
      <div data-case="table" style={{ marginBottom: 32 }}>
        <div data-bubble className={className} style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
          <MarkdownRenderer content={MD} softBreaks={softBreaks} />
        </div>
      </div>
      <div data-case="prose">
        <div data-bubble-prose className={className} style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
          <MarkdownRenderer content={PROSE} softBreaks={softBreaks} />
        </div>
      </div>
    </div>
  )
}

initI18n()
createRoot(document.getElementById('root')!).render(<Scene />)
