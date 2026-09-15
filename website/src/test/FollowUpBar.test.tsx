import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import FollowUpBar, { FOLLOWUP_CHIP_DEBOUNCE_MS } from '../components/FollowUpBar'

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
      // Third arg is the click-time `sourceKey` snapshot — `undefined` here
      // because this caller supplies no `sourceKey` prop at all.
      expect(onSelect).toHaveBeenCalledWith('Ship it', expect.any(Object), undefined)
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
      expect(onSend).toHaveBeenCalledWith('Go', undefined)
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
      expect(onSend).toHaveBeenCalledWith(undefined, undefined)
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
      expect(onSend).toHaveBeenCalledWith('Go', undefined)
      expect(onSelect).not.toHaveBeenCalled()
    })

    it('clicking the send segment on a picked chip calls onSend(undefined) — uses current input', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={['Go']} picked={new Set(['Go'])} onSelect={onSelect} onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Send now: Go' }))
      expect(onSend).toHaveBeenCalledWith(undefined, undefined)
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

  // ─── Long labels: bounded width, clamped text, full text on hover ────────
  // Regression: an option is a full user-voice instruction and can be
  // hundreds of characters. Unbounded, a `shrink-0` chip in the scroll layout
  // sized to max-content, consumed the whole strip and pushed the tail of its
  // own text out of the visible box.
  describe('long option labels', () => {
    const LONG = 'Implement blockers 3 & 4 plus the safe follow-ups and push, but leave blocker 1 (team access) and blocker 2 (CI) for me to handle myself'

    it('caps chip width and clamps the label in the scroll layout', () => {
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} layout="scroll" />)
      const chip = screen.getByRole('button', { name: LONG })
      expect(chip.className).toContain('followup-chip')
      // The clamp must sit on an unpadded inner element, not on the padded
      // button — otherwise a sliver of the next line shows in the padding.
      const label = chip.querySelector('span')
      expect(label?.className).toContain('truncate')
      expect(label?.className).toContain('block')
      expect(chip.className).not.toContain('truncate')
    })

    it('caps the split-button wrapper too, not just the button', () => {
      // The wrapper is the flex item when a send segment is present; without the
      // cap it sizes to the label's untruncated max-content width and leaves a
      // wide gap before the next chip.
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} onSend={() => {}} layout="scroll" />)
      const wrapper = screen.getByRole('button', { name: LONG }).parentElement
      expect(wrapper?.className).toContain('followup-chip')
    })

    it('lets the wrapped button flex inside the cap so the send segment cannot overlap the next chip', () => {
      // Regression: in the scroll layout the button carried both `shrink-0` and
      // the width cap, so it claimed the wrapper's full width and pushed the
      // send segment past the wrapper box — over the next chip. The button must
      // instead flex (`flex-1 min-w-0`) and leave the cap + `shrink-0` to the
      // wrapper alone, which stays the sole capped flex item.
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} onSend={() => {}} layout="scroll" />)
      const btn = screen.getByRole('button', { name: LONG })
      expect(btn.className).toContain('flex-1')
      expect(btn.className).toContain('min-w-0')
      expect(btn.className).not.toContain('followup-chip')
      expect(btn.className).not.toContain('shrink-0')
      // The wrapper remains the capped, non-shrinking flex item.
      const wrapper = btn.parentElement
      expect(wrapper?.className).toContain('followup-chip')
      expect(wrapper?.className).toContain('shrink-0')
    })

    it('backs the cap class with a real max-width rule', () => {
      // jsdom does not load index.css, so the class assertions above would pass
      // with the rule deleted. Read the stylesheet directly.
      const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')
      expect(css).toMatch(
        /\.followup-chip\s*\{[^}]*max-width:\s*min\(100%,\s*clamp\(18rem,\s*calc\(50% - 0\.1875rem\),\s*26rem\)\)/,
      )
    })

    // Regression (#5397): the cap used to be an absolute `min(100%, 26rem)`,
    // sized against the 900px fallback in ChatInput's `--mc-input-width`. The
    // real compact width is 816px, so the inner row was 784px and two 416px
    // chips (+6px gap = 838px) could never share a line — the multiline layout
    // stacked every option one per row and ate the vertical space above the
    // composer. Nothing tied the CSS number to the composer width, so the two
    // drifted silently. These two tests are that tie.
    //
    // Reads the relative part of the cap. Kept as one helper so a deleted or
    // reshaped rule fails both tests below with this message instead of a
    // TypeError on a null match.
    const chipCapPreferred = (): { pct: number, halfGapRem: number } => {
      const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')
      const m = css.match(/\.followup-chip\s*\{[^}]*calc\((\d+)% - ([\d.]+)rem\)/)
      expect(m, '.followup-chip must cap width relative to the row (calc(<pct>% - <half-gap>rem))').not.toBeNull()
      return { pct: Number(m![1]), halfGapRem: Number(m![2]) }
    }

    it('caps a chip at half the row so two chips always fit a line', () => {
      const { pct, halfGapRem } = chipCapPreferred()
      // Two chips + one gap must fit the row: 2 × (pct% − halfGap) + gap ≤ 100%
      // for any row width, which holds iff pct ≤ 50 and the subtracted amount is
      // at least half the gap (pinned to the rendered gap class below).
      expect(pct).toBeLessThanOrEqual(50)
      expect(halfGapRem).toBeGreaterThan(0)
    })

    it('pins the CSS half-gap to the gap class both layouts actually render', () => {
      // The cap subtracts HALF the row gap from its 50%. If someone widens the
      // gap class without widening that subtraction, two chips stop fitting and
      // the multiline layout silently regresses to one per row.
      const { halfGapRem } = chipCapPreferred()

      for (const layout of ['multiline', 'scroll'] as const) {
        const { container, unmount } = render(
          <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} layout={layout} />,
        )
        const gapClass = container.querySelector('[class*="gap-"]')?.className.match(/gap-([\d.]+)/)
        expect(gapClass, `${layout} layout renders no gap-* class`).not.toBeNull()
        // Tailwind's spacing scale: gap-N === N × 0.25rem.
        const gapRem = Number(gapClass![1]) * 0.25
        expect(halfGapRem, `${layout} gap is ${gapRem}rem, so the CSS must subtract ${gapRem / 2}rem`).toBeCloseTo(gapRem / 2, 5)
        unmount()
      }
    })

    it('caps chip width and clamps the label to one line in the multiline layout', () => {
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} />)
      const chip = screen.getByRole('button', { name: LONG })
      expect(chip.className).toContain('followup-chip')
      expect(chip.querySelector('span')?.className).toContain('truncate')
    })

    it('clamps to ONE line so a long label cannot make its chip taller than its neighbours', () => {
      // A chip's height is its label's line count, so a wrapping label is what
      // produced a row of sibling controls at two different heights. One line
      // removes the cause instead of equalising it with an alignment rule.
      // jsdom reports no layout, so the clamp class is the assertable part.
      for (const layout of ['scroll', 'multiline'] as const) {
        const { unmount } = render(
          <FollowUpBar options={[LONG, 'Ship it']} picked={new Set()} onSelect={() => {}} onSend={() => {}} layout={layout} />,
        )
        for (const label of [LONG, 'Ship it']) {
          const span = screen.getByRole('button', { name: label }).querySelector('span')
          expect(span?.className).toContain('truncate')
          expect(span?.className).not.toContain('line-clamp-2')
        }
        unmount()
      }
    })

    it('keeps the full label in the DOM so the accessible name is not truncated', () => {
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} layout="scroll" />)
      expect(screen.getByRole('button', { name: LONG }).textContent).toBe(LONG)
    })

    it('puts the full text in the tooltip for a clamped label, followed by the hint', () => {
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} onSend={() => {}} />)
      const title = screen.getByRole('button', { name: LONG }).getAttribute('title') ?? ''
      // Full label FIRST so the reader gets the unreadable part before the hint.
      expect(title.startsWith(LONG)).toBe(true)
      expect(title).toMatch(/double-click/i)
    })

    // With the one-line clamp every chip is already the same height, so these
    // two only pin where a taller chip WOULD sit if one is ever introduced. The
    // row is read against the composer directly below it, so that edge is the
    // bottom, and centring is the specific wrong answer: it would float every
    // ordinary chip into the middle of the taller one's box.
    it('bottom-aligns the chips in the scroll layout so a taller chip cannot float its neighbours', () => {
      const { container } = render(<FollowUpBar options={['Go', LONG]} picked={new Set()} onSelect={() => {}} layout="scroll" />)
      const strip = screen.getByRole('button', { name: 'Go' }).parentElement
      expect(strip?.className).toContain('items-end')
      expect(strip?.className).not.toContain('items-center')
      expect(strip?.className).not.toContain('items-start')
      // Pin the queried node as the scrolling strip, so the assertion cannot
      // pass by having landed on some other ancestor.
      expect(strip?.className).toContain('overflow-x-auto')
      expect(container.querySelector('.items-center.overflow-x-auto')).toBeNull()
    })

    it('bottom-aligns the chips in the multiline layout', () => {
      render(<FollowUpBar options={['Go', LONG]} picked={new Set()} onSelect={() => {}} />)
      const row = screen.getByRole('button', { name: 'Go' }).parentElement
      expect(row?.className).toContain('flex-wrap')
      expect(row?.className).toContain('items-end')
      expect(row?.className).not.toContain('items-center')
      expect(row?.className).not.toContain('items-start')
    })

    // The send segment still centres its arrow against the full chip height —
    // aligning the row on one edge must not collapse the segment to one line.
    it('keeps the send segment stretched to the chip height', () => {
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} onSend={() => {}} layout="scroll" />)
      const wrapper = screen.getByRole('button', { name: LONG }).parentElement
      expect(wrapper?.className).toContain('items-stretch')
    })

    it('carries the full label on EVERY chip, with no length threshold deciding it', () => {
      // Regression: the tooltip used to switch to the full text only past 60
      // characters, a number chosen when the label wrapped to two lines. At one
      // clamped line the cut starts around 44, so every label in between was
      // visibly truncated with the hover showing only the gesture hint. Length
      // must not gate it — a 12-char label and a 200-char one behave the same.
      for (const option of ['Merge it now', 'x'.repeat(50), LONG]) {
        const { unmount } = render(
          <FollowUpBar options={[option]} picked={new Set()} onSelect={() => {}} onSend={() => {}} />,
        )
        const title = screen.getByRole('button', { name: option }).getAttribute('title') ?? ''
        expect(title.startsWith(option)).toBe(true)
        expect(title).toMatch(/double-click/i)
        unmount()
      }
    })

    it('still passes the untruncated option text to onSelect', () => {
      const onSelect = vi.fn()
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={onSelect} layout="scroll" />)
      fireEvent.click(screen.getByRole('button', { name: LONG }))
      expect(onSelect).toHaveBeenCalledWith(LONG, expect.any(Object))
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

  // ─── sourceKey is snapshotted at CLICK time, not at debounce-fire time ────
  // The 220ms debounce means the transcript row these chips came from can be
  // REPLACED while the timer is pending — and a byte-identical replacement
  // footer (same labels, so the same chip keys) re-renders the chip WITHOUT
  // remounting it, so the timer survives. A callee that acts on the click
  // (the orchestrator plan dispatch) must therefore be told which row the
  // user actually clicked, not whichever row happens to be current when the
  // timer fires — otherwise one click on a stale footer approves the stage
  // that replaced it.
  describe('sourceKey (click-time row identity)', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    const PLAN = ['Go', 'Go All', 'Cancel']

    it('hands onSelect the sourceKey from CLICK time after the row advances mid-debounce', () => {
      const onSelect = vi.fn()
      const bar = (sourceKey: string) => (
        <FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} onSend={() => {}} sourceKey={sourceKey} />
      )
      const { rerender } = render(bar('row-1'))
      const go = screen.getByRole('button', { name: 'Go' })
      fireEvent.click(go, { detail: 1 })
      expect(onSelect).not.toHaveBeenCalled() // timer pending

      // The replacement footer: identical options (so `key={o}` matches and the
      // chip is REUSED, not recreated) but a new row identity.
      rerender(bar('row-2'))
      // Pin the no-remount premise the whole race rests on — if React replaced
      // the element, the pending timer would have been cleaned up and this test
      // would pass for the wrong reason.
      expect(screen.getByRole('button', { name: 'Go' })).toBe(go)

      act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 30) })
      expect(onSelect).toHaveBeenCalledTimes(1)
      expect(onSelect).toHaveBeenCalledWith('Go', expect.any(Object), 'row-1')
    })

    it('hands onSelect the current sourceKey when the row does not change', () => {
      const onSelect = vi.fn()
      render(<FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} onSend={() => {}} sourceKey="row-1" />)
      fireEvent.click(screen.getByRole('button', { name: 'Go All' }), { detail: 1 })
      act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 30) })
      expect(onSelect).toHaveBeenCalledWith('Go All', expect.any(Object), 'row-1')
    })

    it('hands onSend the sourceKey from FIRST click when the row advances mid-double-click', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      const bar = (sourceKey: string) => (
        <FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} onSend={onSend} sourceKey={sourceKey} />
      )
      const { rerender } = render(bar('row-1'))
      const go = screen.getByRole('button', { name: 'Go' })
      fireEvent.click(go, { detail: 1 })
      rerender(bar('row-2'))
      expect(screen.getByRole('button', { name: 'Go' })).toBe(go)
      fireEvent.click(go, { detail: 2 })
      fireEvent.dblClick(go)
      expect(onSend).toHaveBeenCalledTimes(1)
      expect(onSend).toHaveBeenCalledWith('Go', 'row-1')
      expect(onSelect).not.toHaveBeenCalled()
    })

    it('hands onSend the current sourceKey on a Send-now click with no prior arm', () => {
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(<FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} onSend={onSend} sourceKey="row-1" />)
      fireEvent.click(screen.getByRole('button', { name: 'Send now: Go' }))
      expect(onSend).toHaveBeenCalledWith('Go', 'row-1')
      expect(onSelect).not.toHaveBeenCalled()
    })
  })
})
