import { memo, useRef, useState, useEffect, useCallback } from 'react'
import { ChevronLeft, ChevronRight, ArrowUp } from 'lucide-react'

import { i18nT } from '../i18n/t'
export type FollowUpLayout = 'multiline' | 'scroll'

interface FollowUpBarProps {
  options: string[]
  picked: Set<string>
  onSelect: (option: string, event: React.MouseEvent) => void
  /** Double-click sends with this option's text directly (bypasses setInput race). */
  onSend?: (text?: string) => void
  quickSend?: boolean
  /** 'multiline' (default) wraps onto multiple rows; 'scroll' is a single-line horizontally-scrollable view. */
  layout?: FollowUpLayout
}

function chipClassName(isPicked: boolean, extra: string = '') {
  return `${extra} px-3 py-1.5 rounded-lg text-[13px] cursor-pointer transition-all border ${
    isPicked
      ? 'border-solid border-accent/50 text-accent bg-accent-subtle'
      : 'border-border text-muted hover:text-text hover:border-accent/40 bg-bg-elevated'
  }`
}
/** Right-hand "send now" segment class — same palette as the chip body, divided by a border. */
function sendSegmentClassName(isPicked: boolean) {
  return `px-1.5 py-1.5 rounded-r-lg cursor-pointer transition-all border border-l-0 ${
    isPicked
      ? 'border-solid border-accent/50 text-accent bg-accent-subtle hover:bg-accent/20'
      : 'border-border text-muted hover:text-accent hover:border-accent/40 bg-bg-elevated'
  }`
}

function chipTitle(isPicked: boolean, quickSend: boolean | undefined, picked: Set<string>, hasOnSend: boolean) {
  if (isPicked) {
    return hasOnSend
      ? i18nT('components.followUpBar.click_to_remove_from_input_double_click_to_send')
      : i18nT('components.followUpBar.click_to_remove_from_input')
  }
  if (quickSend && picked.size === 0) return i18nT('components.followUpBar.click_to_send_instantly_shift_click_to_select_mu')
  if (quickSend) return i18nT('components.followUpBar.click_to_add_to_selection')
  return hasOnSend
    ? i18nT('components.followUpBar.click_to_add_to_input_double_click_to_select_and')
    : i18nT('components.followUpBar.click_to_add_to_input_editable_before_sending')
}

interface ChipProps {
  option: string
  isPicked: boolean
  picked: Set<string>
  quickSend: boolean | undefined
  onSelect: (option: string, event: React.MouseEvent) => void
  onSend?: (text?: string) => void
  className: string
}

/**
 * Single follow-up chip. Handles click/double-click semantics:
 * - When `onSend` is not provided, falls through to direct `onSelect` (legacy callers).
 * - When `quickSend` is active in instant-send state (not picked, no prior picks), falls through
 *   to direct `onSelect` to preserve the no-lag instant-send UX.
 * - Otherwise: single click is debounced 220ms (timer cancelled by double-click) so the user can
 *   double-click to fire `onSend(text)` directly without going through setInput (which would
 *   race with the React state update and cause send() to read a stale inputRef.current).
 */
function Chip({ option, isPicked, picked, quickSend, onSelect, onSend, className }: ChipProps) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const useDebouncedClick = !!onSend && !(quickSend && !isPicked && picked.size === 0)
  const title = chipTitle(isPicked, quickSend, picked, !!onSend)
  // The visible "send now" segment is the discoverable form of the existing
  // double-click-to-send gesture. Redundant (and hidden) in the quickSend
  // instant-send state, where a single click on an unpicked chip already
  // sends — so it's suppressed there to avoid two controls doing the same
  // thing side by side.
  const showSendSegment = useDebouncedClick

  if (!useDebouncedClick) {
    return (
      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onClick={(e) => onSelect(option, e)}
        className={className}
        title={title}
      >
        {option}
      </button>
    )
  }

  const handleClick = (e: React.MouseEvent) => {
    // detail >= 2 means this click is part of a double-click sequence — let
    // onDoubleClick handle it so we don't start a timer that races with it.
    if (e.detail >= 2) return
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // Capture the parts of the event that survive the timer (React pools events).
    const shiftKey = e.shiftKey
    const synth = { shiftKey, detail: 1 } as unknown as React.MouseEvent
    timerRef.current = setTimeout(() => {
      timerRef.current = null
      onSelect(option, synth)
    }, 220)
  }

  const handleDoubleClick = () => {
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    // Pass option text directly to send() so it doesn't race with setInput.
    // If already picked, send() will use the current input (which already contains o).
    onSend?.(isPicked ? undefined : option)
  }

  const mainChip = (
    <button
      type="button"
      // Keep keyboard focus in the textarea on click. Without this the chip
      // takes focus, and a follow-up Enter re-activates this (now picked) chip,
      // running the toggle-off branch that deletes the composed input ("the
      // prompt clears"). Deliberate keyboard (tab) activation still toggles.
      onMouseDown={(e) => e.preventDefault()}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      className={showSendSegment ? className.replace('rounded-lg', 'rounded-l-lg') : className}
      title={title}
    >
      {option}
    </button>
  )

  if (!showSendSegment) return mainChip

  return (
    <span className="inline-flex items-stretch shrink-0">
      {mainChip}
      <button
        type="button"
        aria-label={i18nT('components.followUpBar.send_now_2', { option })}
        title={i18nT('components.followUpBar.send_now')}
        onMouseDown={(e) => e.preventDefault()}
        onClick={(e) => { e.stopPropagation(); if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }; onSend?.(isPicked ? undefined : option) }}
        className={sendSegmentClassName(isPicked)}
      >
        <ArrowUp size={13} />
      </button>
    </span>
  )
}

function ScrollLayout({ options, picked, onSelect, onSend, quickSend }: Omit<FollowUpBarProps, 'layout'>) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [canScrollL, setCanScrollL] = useState(false)
  const [canScrollR, setCanScrollR] = useState(false)

  const updateScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    setCanScrollL(el.scrollLeft > 2)
    setCanScrollR(el.scrollLeft + el.clientWidth < el.scrollWidth - 2)
  }, [])

  // Scroll by ~80% of the visible width in the given direction, so a click
  // reveals the next set of chips while keeping one in view for continuity.
  const scrollByDir = useCallback((dir: -1 | 1) => {
    const el = scrollRef.current
    if (!el) return
    el.scrollBy({ left: dir * Math.max(el.clientWidth * 0.8, 120), behavior: 'smooth' })
  }, [])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    updateScroll()
    el.addEventListener('scroll', updateScroll, { passive: true })
    const ro = new ResizeObserver(updateScroll)
    ro.observe(el)
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return
      if (el.scrollWidth <= el.clientWidth) return
      e.preventDefault()
      el.scrollLeft += e.deltaY
    }
    el.addEventListener('wheel', onWheel, { passive: false })

    return () => {
      el.removeEventListener('scroll', updateScroll)
      el.removeEventListener('wheel', onWheel)
      ro.disconnect()
    }
  }, [updateScroll, options])

  // Small solid, vertically-centered pill button so the arrow reads as a
  // distinct control instead of a transparent icon colliding with the chip
  // text underneath it. The opaque background masks the faded edge chip.
  const arrowClass = 'absolute top-1/2 -translate-y-1/2 z-20 flex items-center justify-center h-6 w-6 rounded-full bg-bg-elevated border border-border text-muted hover:text-text hover:border-accent/40 shadow-sm cursor-pointer p-0'

  return (
    <div className="pt-1">
      <div className="relative">
      {canScrollL && <div className="absolute left-0 top-0 bottom-0 w-10 z-10 pointer-events-none bg-gradient-to-r from-bg to-transparent" />}
      {canScrollR && <div className="absolute right-0 top-0 bottom-0 w-10 z-10 pointer-events-none bg-gradient-to-l from-bg to-transparent" />}
      {canScrollL && (
        <button
          type="button"
          aria-label={i18nT('components.followUpBar.scroll_suggestions_left')}
          title={i18nT('components.followUpBar.scroll_left')}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => scrollByDir(-1)}
          className={`${arrowClass} left-0.5`}
        >
          <ChevronLeft size={16} />
        </button>
      )}
      {canScrollR && (
        <button
          type="button"
          aria-label={i18nT('components.followUpBar.scroll_suggestions_right')}
          title={i18nT('components.followUpBar.scroll_right')}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => scrollByDir(1)}
          className={`${arrowClass} right-0.5`}
        >
          <ChevronRight size={16} />
        </button>
      )}
      <div ref={scrollRef} className="flex gap-1.5 overflow-x-auto items-center" style={{ scrollbarWidth: 'none', msOverflowStyle: 'none' }}>
        {options.map(o => {
          const isPicked = picked.has(o)
          return (
            <Chip
              key={o}
              option={o}
              isPicked={isPicked}
              picked={picked}
              quickSend={quickSend}
              onSelect={onSelect}
              onSend={onSend}
              className={chipClassName(isPicked, 'shrink-0')}
            />
          )
        })}
      </div>
      </div>
    </div>
  )
}

function MultilineLayout({ options, picked, onSelect, onSend, quickSend }: Omit<FollowUpBarProps, 'layout'>) {
  return (
    <div className="flex gap-1.5 flex-wrap pt-1 items-center">
      {options.map(o => {
        const isPicked = picked.has(o)
        return (
          <Chip
            key={o}
            option={o}
            isPicked={isPicked}
            picked={picked}
            quickSend={quickSend}
            onSelect={onSelect}
            onSend={onSend}
            className={chipClassName(isPicked)}
          />
        )
      })}
    </div>
  )
}

function FollowUpBar({ options, picked, onSelect, onSend, quickSend, layout = 'multiline' }: FollowUpBarProps) {
  if (layout === 'scroll') {
    return <ScrollLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} />
  }
  return <MultilineLayout options={options} picked={picked} onSelect={onSelect} onSend={onSend} quickSend={quickSend} />
}

export default memo(FollowUpBar)
