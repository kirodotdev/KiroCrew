import { useMemo } from 'react'
import { motion } from 'framer-motion'
import { LayoutDashboard } from 'lucide-react'

import { i18nT } from '../i18n/t'
const COLS = 24
const ROWS = 12

function seededRandom(seed: number): number {
  const x = Math.sin(seed * 127.1 + seed * 311.7) * 43758.5453
  return x - Math.floor(x)
}

/**
 * "Mesh flow" placeholder — dense grid of dots filling the card.
 * Uses Framer Motion for smooth opacity + scale animation per dot.
 */
export default function WidgetPlaceholder({ title = 'Widget' }: { title?: string }) {
  const dots = useMemo(() => {
    const result: { delay: number; dur: number }[] = []
    for (let r = 0; r < ROWS; r++) {
      for (let c = 0; c < COLS; c++) {
        const i = r * COLS + c
        result.push({ delay: seededRandom(i) * 3.5, dur: 2.0 + seededRandom(i + 999) * 2.5 })
      }
    }
    return result
  }, [])

  return (
    <div className="rounded-xl border border-border bg-card overflow-hidden my-2">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-bg-elevated">
        <LayoutDashboard size={13} className="text-accent" />
        <span className="text-[13px] font-medium text-text">{title}</span>
        <span className="text-[12px] text-muted ml-1 animate-pulse">{i18nT('components.widgetPlaceholder.generating')}</span>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: `repeat(${COLS}, 1fr)`,
          gridTemplateRows: `repeat(${ROWS}, 1fr)`,
          gap: 0,
          padding: '16px',
          height: 440,
        }}
      >
        {dots.map((d, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <motion.div
              style={{
                width: 4,
                height: 4,
                borderRadius: '50%',
                backgroundColor: 'var(--accent)',
              }}
              animate={{
                opacity: [0.08, 0.5, 0.35, 0.08],
                scale: [0.4, 1.3, 0.9, 0.4],
              }}
              transition={{
                duration: d.dur,
                delay: d.delay,
                repeat: Infinity,
                ease: 'easeInOut',
              }}
            />
          </div>
        ))}
      </div>
    </div>
  )
}
