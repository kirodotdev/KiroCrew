import React, { useRef, useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence, useReducedMotion } from 'framer-motion'
import { ChevronDown } from 'lucide-react'

export interface Segment<T extends string = string> {
  key: T
  label: string
  icon?: React.ReactNode
  count?: number
  tooltip?: string
  /**
   * Render the segment but refuse selection — for an option the surface knows
   * about and cannot serve yet. Showing it greyed says "planned"; omitting it
   * says "does not exist", and silently accepting the click says "broken".
   * A disabled segment carries `aria-disabled` rather than the `disabled`
   * attribute so its explanation remains available to assistive technology and
   * its pointer tooltip remains readable. It is removed from the radio group's
   * sequential tab stop and skipped by radio-key navigation.
   */
  disabled?: boolean
}

interface SegmentedControlProps<T extends string = string> {
  segments: Segment<T>[]
  value: T
  onChange: (value: T) => void
  layoutId?: string
  ariaLabel?: string
  ariaDescribedBy?: string
  /** Allow full labels to wrap rather than hiding them. */
  wrap?: boolean
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
  /**
   * Pin the control to its icon-only form — every segment keeps its icon, only
   * the selected one keeps its label. For a row that must fit a phone while its
   * parent hugs its content, where the measured collapse above cannot help
   * (that measurement reads this control's own width and always answers
   * "plenty of room"). Wins over `collapse`, since it is a decision the caller
   * has already made.
   */
  compact?: boolean
  /**
   * Hide EVERY segment's label, the selected one included — the control is a
   * row of icon buttons. Each label moves to the segment's `aria-label` and
   * `title`, so the name survives for readers and hover. For a pair whose
   * icons are self-evident (grid/list) where even the selected label is noise.
   */
  iconOnly?: boolean
}

type Mode = 'full' | 'compact' | 'dropdown'

export default function SegmentedControl<T extends string = string>({ segments, value, onChange, layoutId = 'segment', ariaLabel, ariaDescribedBy, wrap = false, collapse = true, compact = false, iconOnly = false }: SegmentedControlProps<T>) {
  const containerRef = useRef<HTMLDivElement>(null)
  const dropdownToggleRef = useRef<HTMLButtonElement>(null)
  const radioRefs = useRef(new Map<T, HTMLButtonElement>())
  const returnFocusOnCloseRef = useRef(false)
  const dropdownGroupId = React.useId()
  // The label below animates its WIDTH while clipping overflow, so until it
  // settles the text is genuinely cut off. Anything measuring layout in that
  // window — a reader with reduced motion, or the render gate, which launches
  // with the preference set for exactly this reason — sees truncated labels that
  // are not truncated once the spring lands. framer-motion does not consult the
  // preference on its own, which is why the honouring is explicit here.
  const reduceMotion = useReducedMotion()
  const [mode, setMode] = useState<Mode>(compact ? 'compact' : 'full')
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const enabledSegments = segments.filter(segment => segment.disabled !== true)
  // When every segment is disabled the group still needs ONE sequential tab
  // stop, or the keyboard can never reach it to hear why it is greyed out
  // (the `ariaDescribedBy` reason is announced on the focused radio).
  const tabStopKey = enabledSegments.some(segment => segment.key === value)
    ? value
    : enabledSegments.length > 0
      ? enabledSegments[0].key
      : segments.some(segment => segment.key === value)
        ? value
        : segments[0]?.key

  const closeDropdown = useCallback((returnFocus: boolean) => {
    returnFocusOnCloseRef.current = returnFocus
    setDropdownOpen(false)
  }, [])

  useEffect(() => {
    if (!dropdownOpen) return
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        closeDropdown(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [closeDropdown, dropdownOpen])

  const measure = useCallback(() => {
    if (compact) { setMode('compact'); return }
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
  }, [segments.length, collapse, compact])

  useEffect(() => {
    measure()
    if (!collapse || compact) return
    const ro = new ResizeObserver(measure)
    if (containerRef.current?.parentElement) ro.observe(containerRef.current.parentElement)
    return () => ro.disconnect()
  }, [measure, collapse, compact])

  useEffect(() => {
    if (dropdownOpen) {
      if (tabStopKey !== undefined) radioRefs.current.get(tabStopKey)?.focus()
      return
    }
    if (returnFocusOnCloseRef.current) {
      returnFocusOnCloseRef.current = false
      dropdownToggleRef.current?.focus()
    }
  }, [dropdownOpen, tabStopKey])

  const setRadioRef = useCallback((key: T, node: HTMLButtonElement | null) => {
    if (node) radioRefs.current.set(key, node)
    else radioRefs.current.delete(key)
  }, [])

  // Arrow/Home/End move focus only; Space/Enter (a native button click)
  // commits. Several consumers write server state on every change (the update
  // channel, the Ops Mission Control autonomy ceiling, tag agent policy), so
  // keyboard traversal must never persist an option the user only passed over.
  const handleRadioKeyDown = useCallback((event: React.KeyboardEvent<HTMLButtonElement>, currentKey: T) => {
    const { key } = event
    const isNavigationKey = key === 'ArrowLeft' || key === 'ArrowUp' || key === 'ArrowRight'
      || key === 'ArrowDown' || key === 'Home' || key === 'End'
    if (enabledSegments.length === 0) {
      // Nothing to move to, but the key is still the group's: do not let it
      // scroll the page or reach an ancestor handler.
      if (isNavigationKey) {
        event.preventDefault()
        event.stopPropagation()
      }
      return
    }
    const currentIndex = enabledSegments.findIndex(segment => segment.key === currentKey)
    let targetIndex: number
    switch (event.key) {
      case 'ArrowLeft':
      case 'ArrowUp':
        targetIndex = currentIndex <= 0 ? enabledSegments.length - 1 : currentIndex - 1
        break
      case 'ArrowRight':
      case 'ArrowDown':
        targetIndex = currentIndex < 0 || currentIndex === enabledSegments.length - 1
          ? 0
          : currentIndex + 1
        break
      case 'Home':
        targetIndex = 0
        break
      case 'End':
        targetIndex = enabledSegments.length - 1
        break
      default:
        return
    }
    event.preventDefault()
    event.stopPropagation()
    const target = enabledSegments[targetIndex]
    radioRefs.current.get(target.key)?.focus()
  }, [enabledSegments])

  const active = segments.find(s => s.key === value)

  if (mode === 'dropdown') {
    return (
      <div ref={containerRef} className="relative inline-flex">
        <button
          ref={dropdownToggleRef}
          type="button"
          aria-expanded={dropdownOpen}
          aria-controls={dropdownOpen ? dropdownGroupId : undefined}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-bg-elevated border border-border text-[12px] font-medium cursor-pointer text-accent"
          onClick={() => {
            if (dropdownOpen) closeDropdown(false)
            else setDropdownOpen(true)
          }}
        >
          {active?.icon && <span>{active.icon}</span>}
          <span>{active?.label}</span>
          <ChevronDown size={12} className="text-muted" />
        </button>
        <AnimatePresence>
          {dropdownOpen && (
            <motion.div
              id={dropdownGroupId}
              role="radiogroup"
              aria-label={ariaLabel}
              aria-describedby={ariaDescribedBy}
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.1 }}
              className="absolute top-full left-0 mt-1 z-50 rounded-lg bg-bg-elevated border border-border shadow-lg py-1 min-w-[140px]"
            >
              {segments.map(s => {
                const isDisabled = s.disabled === true
                return (
                  <button
                    key={s.key}
                    ref={node => setRadioRef(s.key, node)}
                    type="button"
                    role="radio"
                    aria-checked={s.key === value}
                    aria-describedby={isDisabled ? ariaDescribedBy : undefined}
                    aria-disabled={isDisabled || undefined}
                    tabIndex={s.key === tabStopKey ? 0 : -1}
                    onKeyDown={event => {
                      if (event.key === 'Escape') {
                        event.preventDefault()
                        event.stopPropagation()
                        closeDropdown(true)
                        return
                      }
                      handleRadioKeyDown(event, s.key)
                    }}
                    onClick={() => {
                      if (isDisabled) return
                      onChange(s.key)
                      closeDropdown(true)
                    }}
                    className={`flex items-center gap-2 w-full px-3 py-1.5 text-[12px] font-medium border-none bg-transparent text-left ${
                      isDisabled
                        ? 'text-muted/40 cursor-not-allowed'
                        : `cursor-pointer hover:bg-bg-hover ${s.key === value ? 'text-accent' : 'text-muted'}`
                    }`}
                  >
                    {s.icon}
                    <span>{s.label}</span>
                    {(s.count ?? 0) > 0 && <span className="text-[11px] text-muted/40 ml-auto">{s.count}</span>}
                  </button>
                )
              })}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    )
  }

  return (
    <>
      <div
        ref={containerRef}
        role="radiogroup"
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        className={`inline-flex rounded-lg bg-bg-elevated border border-border p-0.5 gap-0.5 ${wrap ? 'flex-wrap w-full' : ''}`}
      >
        {segments.map(s => {
          const isActive = s.key === value
          const isDisabled = s.disabled === true
          // Compact hides an unselected segment's label, leaving an icon-only
          // button; `iconOnly` hides every label. Name it explicitly rather
          // than leaning on `title` as the accessible-name fallback: the
          // tooltip never appears on touch, which is the form factor compact
          // exists for.
          const labelShown = !iconOnly && (mode === 'full' || isActive)
          // #9684: the active-pill indicator (below) is `absolute inset-0`, so
          // its CSS box always equals the button's live box. The label reveal
          // animates its own `width` from 0 to auto, growing the button box
          // every frame of the transition. Two things used to make the pill
          // mis-size during that reveal, and BOTH had to change (measured: each
          // alone leaves ~6px of overshoot, together 0):
          //   1. the button carried `layout`, so framer re-measured and
          //      re-projected the whole button box every frame -- the pill,
          //      pinned to it, was dragged onto the intermediate box. Dropped
          //      here; the button still grows smoothly because the label's
          //      width is itself a spring, and the pill's travel BETWEEN
          //      segments is the indicator's own `layoutId`, not the button's.
          //   2. the indicator animated its SIZE via the shared-layout spring,
          //      so on selection it sprang from the old box to a NEW box read
          //      while the label was still at width 0. `layout="position"` below
          //      keeps the cross-segment position spring but takes the size from
          //      CSS `inset-0`, so the pill matches the button box on every
          //      frame, settled or mid-reveal.
          return (
            <motion.button
              key={s.key}
              ref={node => setRadioRef(s.key, node)}
              type="button"
              role="radio"
              aria-checked={isActive}
              aria-label={labelShown ? undefined : s.label}
              aria-describedby={isDisabled ? ariaDescribedBy : undefined}
              aria-disabled={isDisabled || undefined}
              tabIndex={s.key === tabStopKey ? 0 : -1}
              onKeyDown={event => handleRadioKeyDown(event, s.key)}
              onClick={() => {
                if (isDisabled) return
                onChange(s.key)
              }}
              title={s.tooltip || s.label}
              whileTap={isActive && !isDisabled ? { scale: 0.95 } : undefined}
              transition={{ duration: 0.15 }}
              className={`relative flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-[12px] font-medium border-none transition-colors z-[1] ${wrap ? 'flex-1 basis-32 justify-center' : ''} ${
                isDisabled
                  ? isActive
                    ? 'text-accent/60 cursor-not-allowed'
                    : 'text-muted/40 cursor-not-allowed'
                  : isActive
                    ? 'text-accent cursor-pointer'
                    : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'
              }`}
            >
              {/* The selected pill stays on a disabled segment: a group greyed
                *  out by a pending save or an unavailable store must still show
                *  which option is in force. */}
              {isActive && (
                <motion.div
                  layout="position"
                  layoutId={`${layoutId}-indicator`}
                  className="absolute inset-0 bg-card rounded-md shadow-sm border border-border"
                  transition={reduceMotion
                    ? { duration: 0 }
                    : { type: 'spring', stiffness: 500, damping: 35 }}
                />
              )}
              {s.icon && <span className="relative z-[1]">{s.icon}</span>}
              <AnimatePresence>
                {labelShown && (
                  <motion.span
                    key={`label-${s.key}`}
                    initial={reduceMotion ? false : { width: 0 }}
                    animate={{ width: 'auto' }}
                    exit={reduceMotion ? { width: 'auto' } : { width: 0 }}
                    transition={reduceMotion
                      ? { duration: 0 }
                      : { type: 'spring', bounce: 0, duration: 0.2 }}
                    className={`relative z-[1] overflow-hidden ${wrap ? 'whitespace-normal text-center' : 'whitespace-nowrap'}`}
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
