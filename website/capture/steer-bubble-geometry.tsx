/**
 * Isolated capture + measurement entry for the STEERED user bubble's geometry.
 *
 * WHY ISOLATED: the two defects photographed here need a steered message whose
 * text exceeds the 550px bubble cap, injected mid-turn — not reproducible on
 * demand in a live session, and happy-dom computes no layout. What IS the
 * defect is the box chain between the transcript's content column and the
 * bubble, so this rebuilds that chain with the literal ChatPage wrapper
 * classes and mounts the REAL UserMessage fed by the REAL MarkdownRenderer.
 *
 * The two assertions `window.__measure()` exposes:
 *  - endGap: every user bubble's right edge must sit the same distance from
 *    the column's right edge, steered or not, long or short. Before the fix a
 *    long steered bubble's animation wrapper (`w-fit max-w-full`, no 550px
 *    cap) inflated to the full column — percentage max-widths are treated as
 *    none while intrinsic widths resolve — and the capped bubble inside it
 *    sat at the wrapper's LEFT edge while the steer badge stayed right.
 *  - ring containment: the one-shot entrance ring must stay inside the row
 *    wrapper's overflow-hidden clip box mid-animation. Before the fix it was
 *    drawn 2px OUTSIDE the bubble (-inset-0.5) and scaled outward to 1.04,
 *    so its right edge was always clipped flat.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n/all'
import UserMessage from '../src/pages/chat/UserMessage'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Long enough to exceed the 550px bubble cap — the width that made the
 *  animation wrapper inflate. Mixed CJK + latin mirrors the reporting case. */
const LONG = 'fallback的UI也得和新版一样啊，对我们来说fallback应该只是data层面的fallback而不是ui层面的fallback，UI都不应该知道fallback了'
const SHORT = 'lead 用issue radar'

const render = (c: string) => <MarkdownRenderer content={c} softBreaks />

/** The literal ChatPage per-row wrapper + user-row chain (ChatPage.tsx). */
function Row({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="group flex flex-col min-w-0 items-end">
        <div className="flex flex-col gap-0.5 min-w-0 overflow-hidden max-w-full items-end">
          {children}
        </div>
      </div>
    </div>
  )
}

function Scene() {
  return (
    <div className="bg-bg text-text min-h-screen py-6" data-capture-root>
      <Row><UserMessage content={LONG} meta={{ steer: true }} messageTs="steer-long" renderContent={render} /></Row>
      <Row><UserMessage content={SHORT} meta={{ steer: true }} messageTs="steer-short" renderContent={render} /></Row>
      <Row><UserMessage content={LONG} messageTs="plain-long" renderContent={render} /></Row>
    </div>
  )
}

interface BubbleGeom { i: number; endGap: number; ring: { clippedRight: number } | null }
declare global { interface Window { __measure: () => BubbleGeom[] } }

window.__measure = () => {
  const out: BubbleGeom[] = []
  document.querySelectorAll('[data-role="user"]').forEach((u, i) => {
    const bubble = u.querySelector('.message-bubble')
    if (!bubble) return
    const br = bubble.getBoundingClientRect()
    const col = (u.closest('[class*="mx-auto"]') as HTMLElement).getBoundingClientRect()
    // Nearest overflow-clipping ancestor = the row wrapper. The ring must not
    // extend past its right edge, or the clip cuts it flat mid-animation.
    const ringEl = u.querySelector('[aria-hidden="true"].absolute')
    let ring: BubbleGeom['ring'] = null
    if (ringEl) {
      let el: HTMLElement | null = ringEl.parentElement
      while (el) {
        const o = getComputedStyle(el).overflow
        if (o.includes('hidden') || o.includes('clip')) break
        el = el.parentElement
      }
      const rr = ringEl.getBoundingClientRect()
      ring = { clippedRight: el ? Math.round(rr.right - el.getBoundingClientRect().right) : 0 }
    }
    out.push({ i, endGap: Math.round(col.right - br.right), ring })
  })
  return out
}

initI18n()
createRoot(document.getElementById('root')!).render(<MemoryRouter><Scene /></MemoryRouter>)
