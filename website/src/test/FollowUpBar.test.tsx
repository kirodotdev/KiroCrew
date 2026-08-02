import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'

// jsdom polyfill: scroll-layout uses ResizeObserver to track when the chip
// strip can scroll left/right.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

describe('FollowUpBar', () => {
  // ─── Legacy behavior: no onSend → direct onSelect, no debounce ───────────
  describe('without onSend (legacy callers)', () => {
    it('renders a button per option', () => {
      render(<FollowUpBar options={['Alpha', 'Beta', 'Gamma']} picked={new Set()} onSelect={() => {}} />)
      expect(screen.getByRole('button', { name: 'Alpha' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Beta' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Gamma' })).toBeInTheDocument()
    })

    it('calls onSelect with the exact option text on click (no debounce)', () => {
      const onSelect = vi.fn()
      render(<FollowUpBar options={['Ship it', 'Pause']} picked={new Set()} onSelect={onSelect} />)
      fireEvent.click(screen.getByRole('button', { name: 'Ship it' }))
      expect(onSelect).toHaveBeenCalledTimes(1)
      expect(onSelect).toHaveBeenCalledWith('Ship it', expect.any(Object))
    })

    it('fires onSelect for both picked and unpicked chips', () => {
      const onSelect = vi.fn()
      render(<FollowUpBar options={['A', 'B']} picked={new Set(['A'])} onSelect={onSelect} />)
      fireEvent.click(screen.getByRole('button', { name: 'A' }))
      fireEvent.click(screen.getByRole('button', { name: 'B' }))
      expect(onSelect).toHaveBeenCalledTimes(2)
      expect(onSelect).toHaveBeenNthCalledWith(1, 'A', expect.any(Object))
      expect(onSelect).toHaveBeenNthCalledWith(2, 'B', expect.any(Object))
    })

    it('highlights picked chips and leaves unpicked chips muted', () => {
      render(<FollowUpBar options={['Picked', 'Unpicked']} picked={new Set(['Picked'])} onSelect={() => {}} />)
      const pickedBtn = screen.getByRole('button', { name: 'Picked' })
      const unpickedBtn = screen.getByRole('button', { name: 'Unpicked' })
      expect(pickedBtn.className).toContain('border-accent')
      expect(pickedBtn.className).toContain('text-accent')
      expect(pickedBtn.className).toContain('bg-accent-subtle')
      expect(pickedBtn.getAttribute('title')).toMatch(/remove/i)
      expect(unpickedBtn.className).toContain('text-muted')
      expect(unpickedBtn.className).toContain('bg-bg-elevated')
      expect(unpickedBtn.getAttribute('title')).toMatch(/add to input/i)
    })

    it('is stateless — chip style changes only when the picked prop changes', () => {
      const { rerender } = render(
        <FollowUpBar options={['X']} picked={new Set()} onSelect={() => {}} />
      )
      const btn = screen.getByRole('button', { name: 'X' })
      expect(btn.className).toContain('text-muted')
      fireEvent.click(btn)
      expect(btn.className).toContain('text-muted')
      rerender(<FollowUpBar options={['X']} picked={new Set(['X'])} onSelect={() => {}} />)
      expect(screen.getByRole('button', { name: 'X' }).className).toContain('bg-accent-subtle')
    })
  })

  // ─── Layout variants ─────────────────────────────────────────────────────
  describe('layout', () => {
    it('defaults to multiline layout (flex-wrap, no shrink-0)', () => {
      const { container } = render(
        <FollowUpBar options={['A', 'B']} picked={new Set()} onSelect={() => {}} />
      )
      expect(container.querySelector('.flex-wrap')).toBeInTheDocument()
      expect(container.querySelector('.overflow-x-auto')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'A' }).className).not.toContain('shrink-0')
    })

    it('renders single-line scrollable layout when layout="scroll"', () => {
      const { container } = render(
        <FollowUpBar options={['A', 'B']} picked={new Set()} onSelect={() => {}} layout="scroll" />
      )
      expect(container.querySelector('.overflow-x-auto')).toBeInTheDocument()
      expect(container.querySelector('.flex-wrap')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'A' }).className).toContain('shrink-0')
      const onSelect = vi.fn()
      const { rerender } = render(
        <FollowUpBar options={['Ship']} picked={new Set()} onSelect={onSelect} layout="scroll" />
      )
      void rerender
      fireEvent.click(screen.getByRole('button', { name: 'Ship' }))
      expect(onSelect).toHaveBeenCalledWith('Ship', expect.any(Object))
    })
  })

  // ─── New behavior: with onSend → debounced single click + double-click sends
  describe('with onSend (double-click to send)', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    it('debounces single click 220ms before calling onSelect (detail=1)', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Ship it']} picked={new Set()} onSelect={onSelect} onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Ship it' }), { detail: 1 })
      expect(onSelect).toHaveBeenCalledTimes(0) // timer pending
      act(() => { vi.advanceTimersByTime(250) })
      expect(onSelect).toHaveBeenCalledTimes(1)
      expect(onSelect).toHaveBeenCalledWith('Ship it', expect.any(Object))
      expect(onSend).not.toHaveBeenCalled()
    })

    it('ignores click with detail >= 2 (second click of double-click sequence)', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={onSelect} onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 2 })
      act(() => { vi.advanceTimersByTime(250) })
      expect(onSelect).not.toHaveBeenCalled()
    })

    it('double-click on unpicked chip calls onSend(text) and skips onSelect', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={onSelect} onSend={onSend} />)
      // Real browser fires click(detail=1) → click(detail=2) → dblclick
      // detail=1 starts timer; detail=2 is ignored; dblclick cancels timer + calls onSend('Go')
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 1 })
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 2 })
      fireEvent.dblClick(screen.getByRole('button', { name: 'Go' }))
      expect(onSend).toHaveBeenCalledWith('Go')
      expect(onSend).toHaveBeenCalledTimes(1)
      expect(onSelect).not.toHaveBeenCalled() // timer cancelled
      act(() => { vi.advanceTimersByTime(250) })
      expect(onSend).toHaveBeenCalledTimes(1) // not called again
      expect(onSelect).not.toHaveBeenCalled()
    })

    it('double-click on picked chip calls onSend(undefined) — uses current input', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set(['Go'])} onSelect={onSelect} onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 1 })
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 2 })
      fireEvent.dblClick(screen.getByRole('button', { name: 'Go' }))
      expect(onSelect).not.toHaveBeenCalled()
      expect(onSend).toHaveBeenCalledTimes(1)
      expect(onSend).toHaveBeenCalledWith(undefined)
    })

    it('chip title hints at double-click capability', () => {
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={() => {}} onSend={() => {}} />)
      expect(screen.getByRole('button', { name: 'Go' }).getAttribute('title')).toMatch(/double-click/i)
    })
  })

  // ─── Split-button "send now" segment ─────────────────────────
  // Discoverable form of the double-click-to-send gesture: a distinct
  // send-arrow segment next to the chip body that sends immediately.
  describe('send-now split segment', () => {
    it('renders a distinct "Send" button alongside the chip when onSend is provided', () => {
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={() => {}} onSend={() => {}} />)
      expect(screen.getByRole('button', { name: 'Go' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Send now: Go' })).toBeInTheDocument()
    })

    it('does not render the send segment without onSend (legacy callers)', () => {
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={() => {}} />)
      expect(screen.queryByRole('button', { name: 'Send now: Go' })).not.toBeInTheDocument()
    })

    it('clicking the send segment calls onSend(option) directly and skips onSelect', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={onSelect} onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Send now: Go' }))
      expect(onSend).toHaveBeenCalledTimes(1)
      expect(onSend).toHaveBeenCalledWith('Go')
      expect(onSelect).not.toHaveBeenCalled()
    })

    it('clicking the send segment on a picked chip calls onSend(undefined) — uses current input', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set(['Go'])} onSelect={onSelect} onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Send now: Go' }))
      expect(onSend).toHaveBeenCalledWith(undefined)
    })

    it('clicking the send segment cancels a pending debounced onSelect from the main chip', () => {
      vi.useFakeTimers()
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={onSelect} onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 1 })
      fireEvent.click(screen.getByRole('button', { name: 'Send now: Go' }))
      act(() => { vi.advanceTimersByTime(250) })
      expect(onSend).toHaveBeenCalledTimes(1)
      expect(onSelect).not.toHaveBeenCalled()
      vi.useRealTimers()
    })

    it('suppresses the send segment in quickSend instant-send state (single click already sends)', () => {
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={() => {}} onSend={() => {}} quickSend />)
      expect(screen.queryByRole('button', { name: 'Send now: Go' })).not.toBeInTheDocument()
    })

    it('shows the send segment once a pick exists even with quickSend on (debounced path)', () => {
      render(<FollowUpBar options={['Go']} picked={new Set(['First'])} onSelect={() => {}} onSend={() => {}} quickSend />)
      expect(screen.getByRole('button', { name: 'Send now: Go' })).toBeInTheDocument()
    })
  })

  // ─── Quick-send instant-send state preserves no-lag UX ───────────────────
  describe('with onSend + quickSend (instant-send state)', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    it('skips debounce when quickSend is on, no picks, and chip is not picked', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={onSelect} onSend={onSend} quickSend />)
      // Click should fire onSelect immediately without 220ms wait — the parent's
      // onSelect implementation is responsible for calling tryQuickSend.
      fireEvent.click(screen.getByRole('button', { name: 'Go' }))
      expect(onSelect).toHaveBeenCalledTimes(1)
      expect(onSelect).toHaveBeenCalledWith('Go', expect.any(Object))
    })

    it('uses debounced path once a chip is picked (multi-select state)', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set(['First'])} onSelect={onSelect} onSend={onSend} quickSend />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 1 })
      expect(onSelect).toHaveBeenCalledTimes(0)
      act(() => { vi.advanceTimersByTime(250) })
      expect(onSelect).toHaveBeenCalledTimes(1)
    })

    it('uses debounced path on a picked chip (so double-click can send the current input)', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set(['Go'])} onSelect={onSelect} onSend={onSend} quickSend />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }), { detail: 1 })
      expect(onSelect).toHaveBeenCalledTimes(0)
      act(() => { vi.advanceTimersByTime(250) })
      expect(onSelect).toHaveBeenCalledTimes(1)
    })
  })

  // ─── Focus management: clicking a chip must NOT steal keyboard focus ──────
  // Keeps keyboard focus in the textarea on chip click. If a chip took focus on
  // click, a follow-up Enter would re-activate the (now picked) chip and run its
  // toggle-off branch, deleting the composed input. type=button + onMouseDown
  // preventDefault keep focus in the textarea so Enter sends. The toggle still
  // works via mouse re-click and via deliberate keyboard (tab) activation — only
  // the mouse-click focus steal is suppressed.
  describe('focus management (does not steal focus on click)', () => {
    it('legacy chip (no onSend) is type=button and prevents mousedown default', () => {
      render(<FollowUpBar options={['Alpha']} picked={new Set()} onSelect={() => {}} />)
      const chip = screen.getByRole('button', { name: 'Alpha' })
      expect(chip).toHaveAttribute('type', 'button')
      // fireEvent returns false when the cancelable event had preventDefault called.
      expect(fireEvent.mouseDown(chip)).toBe(false)
    })

    it('debounced chip (with onSend) is type=button and prevents mousedown default', () => {
      render(<FollowUpBar options={['Beta']} picked={new Set()} onSelect={() => {}} onSend={() => {}} />)
      const chip = screen.getByRole('button', { name: 'Beta' })
      expect(chip).toHaveAttribute('type', 'button')
      expect(fireEvent.mouseDown(chip)).toBe(false)
    })

    it('picked chip prevents mousedown default (so Enter in textarea sends, not toggles off)', () => {
      render(<FollowUpBar options={['Gamma']} picked={new Set(['Gamma'])} onSelect={() => {}} onSend={() => {}} />)
      const chip = screen.getByRole('button', { name: 'Gamma' })
      expect(chip).toHaveAttribute('type', 'button')
      expect(fireEvent.mouseDown(chip)).toBe(false)
    })
  })
})
