import React, { useRef, useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { ChevronDown } from 'lucide-react'

export interface Segment<T extends string = string> {
  key: T
  label: string
  icon?: React.ReactNode
  count?: number
  tooltip?: string
}

interface SegmentedControlProps<T extends string = string> {
  segments: Segment<T>[]
  value: T
  onChange: (value: T) => void
  layoutId?: string
  /**
   * Responsive collapsing (full -> compact -> dropdown) is measured against the
   * PARENT element, so it only works when the parent's width is independent of
   * this control. Pass false when the parent hugs its content (`shrink-0`,
   * `inline-flex`, a table cell): the measurement is then circular and the
   * control collapses to the dropdown for no reason. Also pass false inside a
   * `.card-glow` Card, where `> * { z-index: 1 }` traps the dropdown overlay
   * beneath the rows that follow it.
   */
  collapse?: boolean
}

type Mode = 'full' | 'compact' | 'dropdown'

export default function SegmentedControl<T extends string = string>({ segments, value, onChange, layoutId = 'segment', collapse = true }: SegmentedControlProps<T>) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [mode, setMode] = useState<Mode>('full')
  const [dropdownOpen, setDropdownOpen] = useState(false)

  useEffect(() => {
    if (!dropdownOpen) return
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) setDropdownOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [dropdownOpen])

  const measure = useCallback(() => {
    if (!collapse) { setMode('full'); return }
    const el = containerRef.current?.parentElement
    if (!el) return
    const w = el.clientWidth
    // ~80px per tab full, ~40px compact, dropdown below 120
    const fullWidth = segments.length * 80 + 16
    const compactWidth = segments.length * 44 + 16
    if (w >= fullWidth) setMode('full')
    else if (w >= compactWidth) setMode('compact')
    else setMode('dropdown')
  }, [segments.length, collapse])

  useEffect(() => {
    measure()
    if (!collapse) return
    const ro = new ResizeObserver(measure)
    if (containerRef.current?.parentElement) ro.observe(containerRef.current.parentElement)
    return () => ro.disconnect()
  }, [measure, collapse])

  const active = segments.find(s => s.key === value)

  if (mode === 'dropdown') {
    return (
      <div ref={containerRef} className="relative inline-flex">
        <button
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-bg-elevated border border-border text-[12px] font-medium cursor-pointer text-accent"
          onClick={() => setDropdownOpen(!dropdownOpen)}
        >
          {active?.icon && <span>{active.icon}</span>}
          <span>{active?.label}</span>
          <ChevronDown size={12} className="text-muted" />
        </button>
        <AnimatePresence>
          {dropdownOpen && (
            <motion.div
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.1 }}
              className="absolute top-full left-0 mt-1 z-50 rounded-lg bg-bg-elevated border border-border shadow-lg py-1 min-w-[140px]"
            >
              {segments.map(s => (
                <button
                  key={s.key}
                  onClick={() => { onChange(s.key); setDropdownOpen(false) }}
                  className={`flex items-center gap-2 w-full px-3 py-1.5 text-[12px] font-medium cursor-pointer border-none bg-transparent text-left hover:bg-bg-hover ${
                    s.key === value ? 'text-accent' : 'text-muted'
                  }`}
                >
                  {s.icon}
                  <span>{s.label}</span>
                  {(s.count ?? 0) > 0 && <span className="text-[11px] text-muted/40 ml-auto">{s.count}</span>}
                </button>
              ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  return (
    <>
      <div ref={containerRef} className="inline-flex rounded-lg bg-bg-elevated border border-border p-0.5 gap-0.5">
        {segments.map(s => {
          const isActive = s.key === value
          return (
            <motion.button
              key={s.key}
              layout
              onClick={() => onChange(s.key)}
              title={s.tooltip || s.label}
              whileTap={isActive ? { scale: 0.95 } : undefined}
              transition={{ duration: 0.15 }}
              className={`relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium cursor-pointer border-none transition-colors z-[1] ${
                isActive ? 'text-accent' : 'text-muted hover:text-text hover:bg-bg-hover'
              }`}
            >
              {isActive && (
                <motion.div
                  layoutId={`${layoutId}-indicator`}
                  className="absolute inset-0 bg-card rounded-md shadow-sm border border-border"
                  transition={{ type: 'spring', stiffness: 500, damping: 35 }}
                />
              )}
              {s.icon && <span className="relative z-[1]">{s.icon}</span>}
              <AnimatePresence initial={false}>
                {(mode === 'full' || isActive) && (
                  <motion.span
                    key={`label-${s.key}`}
                    layout
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.15 }}
                    className="relative z-[1] whitespace-nowrap"
                  >
                    {s.label}
                  </motion.span>
                )}
              </AnimatePresence>
              {(s.count ?? 0) > 0 && <span className={`relative z-[1] text-[11px] ${isActive ? 'text-accent/60' : 'text-muted/40'}`}>{s.count}</span>}
            </motion.button>
          )
        })}
      </div>
    </>
  )
}
