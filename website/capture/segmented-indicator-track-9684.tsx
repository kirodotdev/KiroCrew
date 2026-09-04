/**
 * Isolated measurement entry for issue #9684: the SegmentedControl active-pill
 * indicator (a `layoutId` `motion.div`, `absolute inset-0`) used to track the
 * button's box while the label animated its `width` from 0 to auto, so during
 * the reveal the pill was sized to an intermediate box rather than the settled
 * one. The shipped fix drops the button's `layout` and gives the pill
 * `layout="position"` (size from CSS `inset-0`, only position springs).
 *
 * WHY ISOLATED: the defect is a real-layout + framer-projection interaction.
 * happy-dom computes no layout, so the unit suite can only pin the prop
 * contract; the divergence itself is only observable in a real browser sampling
 * getBoundingClientRect across animation frames. Same isolated capture
 * technique as flex-input-min-w-0.tsx.
 *
 * fix=on renders the REAL shipped `SegmentedControl`. fix=off renders
 * `PreFixSegments` below -- a DEV-ONLY replica that reproduces the pre-fix
 * shape (button `layout`, indicator size spring) with the same classes,
 * `layoutId` and width-reveal label. The pre-fix path lives HERE, in the
 * dev-only harness, so the shipped component carries no switch back into its
 * own bug (First Principles subtraction on #9715).
 *
 * window.__sampleReveal() drives one selection change and returns, per frame,
 * the indicator rect and the selected button's rect plus their width delta. The
 * probe asserts the peak delta is large with fix=off and ~0 with fix=on.
 *
 * Query string: ?theme=dark&fix=off  (fix defaults on)
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { motion, AnimatePresence } from 'framer-motion'
import { Grid3x3, List } from 'lucide-react'
import SegmentedControl from '../src/components/SegmentedControl'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
// fix=off renders the dev-only PRE-FIX replica; anything else runs the real
// shipped component.
const preFix = params.get('fix') === 'off'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SEGMENTS = [
  { key: 'grid' as const, label: 'Gallery', icon: <Grid3x3 size={13} /> },
  { key: 'table' as const, label: 'Table', icon: <List size={13} /> },
]

/**
 * DEV-ONLY replica of the PRE-FIX SegmentedControl in compact mode, kept in the
 * harness so the shipped component does not carry a path whose purpose is to be
 * wrong. It mirrors the pre-fix markup exactly -- segment `motion.button` with
 * bare `layout`, active pill `motion.div` with `layoutId` and NO
 * `layout="position"` (so it size-springs), and the `width: 0 -> auto` label --
 * which is what reproduces the tracking defect the probe measures. Any drift
 * from the real component's classes would weaken the before/after comparison,
 * so this stays a faithful copy of the compact branch only.
 */
function PreFixSegments({ value, onChange }: { value: 'grid' | 'table'; onChange: (v: 'grid' | 'table') => void }) {
  return (
    <div className="inline-flex rounded-lg bg-bg-elevated border border-border p-0.5 gap-0.5">
      {SEGMENTS.map(s => {
        const isActive = s.key === value
        const labelShown = isActive // compact: only the selected segment shows its label
        return (
          <motion.button
            key={s.key}
            layout
            aria-label={labelShown ? undefined : s.label}
            onClick={() => onChange(s.key)}
            title={s.label}
            whileTap={isActive ? { scale: 0.95 } : undefined}
            transition={{ duration: 0.15 }}
            className={`relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium border-none transition-colors z-[1] ${
              isActive ? 'text-accent cursor-pointer' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'
            }`}
          >
            {isActive && (
              <motion.div
                layoutId="segment-indicator"
                className="absolute inset-0 bg-card rounded-md shadow-sm border border-border"
                transition={{ type: 'spring', stiffness: 500, damping: 35 }}
              />
            )}
            {s.icon && <span className="relative z-[1]">{s.icon}</span>}
            <AnimatePresence>
              {labelShown && (
                <motion.span
                  key={`label-${s.key}`}
                  initial={{ width: 0 }}
                  animate={{ width: 'auto' }}
                  exit={{ width: 0 }}
                  transition={{ type: 'spring', bounce: 0, duration: 0.2 }}
                  className="relative z-[1] overflow-hidden whitespace-nowrap"
                >
                  {s.label}
                </motion.span>
              )}
            </AnimatePresence>
          </motion.button>
        )
      })}
    </div>
  )
}

function Harness() {
  const [value, setValue] = useState<'grid' | 'table'>('grid')
  return (
    <div className="p-8 bg-bg text-text" style={{ width: 320 }}>
      {/* compact: the unselected segment is icon-only, the selected one reveals
          its label -- the width animation the indicator must not track. */}
      <div data-host className="inline-flex">
        {preFix
          ? <PreFixSegments value={value} onChange={setValue} />
          : <SegmentedControl segments={SEGMENTS} value={value} onChange={setValue} compact />}
      </div>
      <div data-caption className="mt-3 text-[11px] text-muted">
        {preFix ? 'BEFORE (#9684): pill oversizes during the reveal' : 'AFTER: pill matches the segment box every frame'}
      </div>
    </div>
  )
}

interface RevealFrame {
  t: number
  indicatorW: number
  buttonW: number
  delta: number
}

declare global {
  interface Window {
    __sampleReveal: () => Promise<{ frames: RevealFrame[]; peakDelta: number }>
  }
}

function rectOf(el: Element | null): DOMRect | null {
  return el ? el.getBoundingClientRect() : null
}

/**
 * Click the currently-unselected segment and sample, every animation frame for
 * ~450ms, the indicator's width against the newly-selected button's width. The
 * indicator is the `.absolute.inset-0` motion.div; the selected button is the
 * one that now carries it.
 */
window.__sampleReveal = () =>
  new Promise(resolve => {
    const host = document.querySelector('[data-host]')!
    const buttons = Array.from(host.querySelectorAll('button'))
    // Select the OTHER button (Table), forcing its label to reveal from 0.
    const target = buttons.find(b => (b.getAttribute('aria-label') || b.textContent || '').includes('Table'))!
    target.click()

    const frames: RevealFrame[] = []
    const start = performance.now()
    const tick = () => {
      const t = performance.now() - start
      // The indicator is the absolute inset-0 pill; find it wherever it now lives.
      const indicator = host.querySelector('.absolute.inset-0')
      // Its offset parent is the button that currently owns it.
      const ownerButton = indicator ? (indicator as HTMLElement).closest('button') : null
      const ir = rectOf(indicator)
      const br = rectOf(ownerButton)
      if (ir && br) {
        frames.push({
          t: Math.round(t),
          indicatorW: Math.round(ir.width * 100) / 100,
          buttonW: Math.round(br.width * 100) / 100,
          delta: Math.round(Math.abs(ir.width - br.width) * 100) / 100,
        })
      }
      if (t < 450) requestAnimationFrame(tick)
      else {
        const peakDelta = frames.reduce((m, f) => Math.max(m, f.delta), 0)
        resolve({ frames, peakDelta })
      }
    }
    requestAnimationFrame(tick)
  })

/**
 * Click Table (reveals its label from width 0) and resolve after `holdMs` of
 * wall clock, so a screenshot taken right after lands mid-reveal on the SAME
 * instant for both fix states -- the frame where the before-shape's pill has
 * overshot the segment box and the after-shape's has not. framer keeps
 * animating; this only fixes WHEN the still is grabbed.
 */
;(window as unknown as { __toggleAndHoldAtMs: (holdMs: number) => Promise<void> }).__toggleAndHoldAtMs = (holdMs: number) =>
  new Promise<void>(resolve => {
    const host = document.querySelector('[data-host]')!
    const target = Array.from(host.querySelectorAll('button'))
      .find(b => (b.getAttribute('aria-label') || b.textContent || '').includes('Table'))!
    target.click()
    setTimeout(resolve, holdMs)
  })

createRoot(document.getElementById('root')!).render(<Harness />)
