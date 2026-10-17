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

    it('a touch tap selects on the first tap and mounts no tooltip (iOS drops a click whose tap reveals one)', () => {
      vi.useFakeTimers()
      try {
        const onSelect = vi.fn()
        render(<FollowUpBar options={['Ship it']} picked={new Set()} onSelect={onSelect} />)
        const chip = screen.getByRole('button', { name: 'Ship it' })
        fireEvent.pointerEnter(chip, { pointerType: 'touch' })
        fireEvent.pointerDown(chip, { pointerType: 'touch' })
        fireEvent.pointerUp(chip, { pointerType: 'touch' })
        fireEvent.mouseEnter(chip)
        fireEvent.click(chip)
        act(() => { vi.advanceTimersByTime(500) })
        expect(onSelect).toHaveBeenCalledTimes(1)
        expect(screen.queryByRole('tooltip')).toBeNull()
      } finally {
        vi.useRealTimers()
      }
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
      // Glass pill: the button IS the Liquid Glass pane (components/Glass.tsx
      // rendered as the button), and the accent is mixed INTO the material
      // (`glass-accent`) rather than an opaque wash.
      expect(pickedBtn.className).toContain('liquid-glass')
      expect(pickedBtn.className).toContain('glass-accent')
      expect(pickedBtn.className).toContain('text-accent')
      fireEvent.focus(pickedBtn)
      expect(screen.getByRole('tooltip').textContent).toMatch(/remove/i)
      fireEvent.blur(pickedBtn)
      expect(unpickedBtn.className).toContain('text-muted')
      expect(unpickedBtn.className).toContain('liquid-glass')
      expect(unpickedBtn.className).not.toContain('glass-accent')
      fireEvent.focus(unpickedBtn)
      expect(screen.getByRole('tooltip').textContent).toMatch(/add to input/i)
      fireEvent.blur(unpickedBtn)
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
      expect(screen.getByRole('button', { name: 'X' }).className).toContain('glass-accent')
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

    it('chip hover tooltip hints at double-click capability, shown instantly', () => {
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={() => {}} onSend={() => {}} />)
      // No timer advance between the enter and the assertion: the tooltip must
      // be synchronous — that is the point of replacing the native `title`,
      // whose OS hover delay made clamped labels look like they had no
      // recovery at all.
      fireEvent.focus(screen.getByRole('button', { name: 'Go' }))
      expect(screen.getByRole('tooltip').textContent).toMatch(/double-click/i)
    })

    it('names the ↑ send segment in the tooltip hint when the segment is visible', () => {
      // The click/double-click sentence never mentioned the visible arrow, so a
      // first-time reader could not tell "where the safe click ends and the
      // send click begins" — the fragment exists exactly when the segment does.
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={() => {}} onSend={() => {}} />)
      fireEvent.focus(screen.getByRole('button', { name: 'Go' }))
      expect(screen.getByRole('tooltip').textContent).toContain('↑ sends now')
    })

    it('omits the ↑ fragment when there is no send segment (no onSend)', () => {
      render(<FollowUpBar options={['Go']} picked={new Set()} onSelect={() => {}} />)
      fireEvent.focus(screen.getByRole('button', { name: 'Go' }))
      expect(screen.getByRole('tooltip').textContent).not.toContain('↑')
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

  // ─── Single-select [OPTION:] rows: tooltip must not promise multi-select ──
  // A single-select pick REPLACES the previous one, so "Shift+Click to select
  // multiple" / "Click to add to selection" would describe a gesture whose
  // result is silently dropped.
  describe('quickSend tooltip wording by selection mode', () => {
    const tipFor = (label: string) => {
      fireEvent.focus(screen.getByRole('button', { name: label }))
      return screen.getByRole('tooltip').textContent ?? ''
    }

    it('single-select with no picks says Shift+Click adds without sending', () => {
      render(<FollowUpBar options={['Deploy']} picked={new Set()} onSelect={() => {}} onSend={() => {}} quickSend multi={false} />)
      const tip = tipFor('Deploy')
      expect(tip).toContain('Click to send instantly, Shift+Click to add to the message without sending')
      expect(tip).not.toMatch(/select multiple/i)
    })

    it('single-select with a pick says the click replaces the selection', () => {
      render(<FollowUpBar options={['Roll back']} picked={new Set(['Deploy'])} onSelect={() => {}} onSend={() => {}} quickSend multi={false} />)
      const tip = tipFor('Roll back')
      expect(tip).toContain('Click to replace selection')
      expect(tip).not.toMatch(/add to selection/i)
    })

    it('multi omitted keeps the multi-select wording (control)', () => {
      const { unmount } = render(<FollowUpBar options={['Deploy']} picked={new Set()} onSelect={() => {}} onSend={() => {}} quickSend />)
      expect(tipFor('Deploy')).toContain('Shift+Click to select multiple')
      unmount()
      render(<FollowUpBar options={['Roll back']} picked={new Set(['Deploy'])} onSelect={() => {}} onSend={() => {}} quickSend />)
      expect(tipFor('Roll back')).toContain('Click to add to selection')
    })
  })

  // ─── Quick Send off: a single-select pick still REPLACES ─────────────────
  // selectSingleFollowUpOption swaps the pick whether or not Quick Send is on,
  // so "Click to add to input" would promise an add that never happens.
  describe('non-quickSend tooltip wording by selection mode', () => {
    const tipFor = (label: string) => {
      fireEvent.focus(screen.getByRole('button', { name: label }))
      return screen.getByRole('tooltip').textContent ?? ''
    }

    it('single-select with a pick and onSend says the click replaces (double-click sends)', () => {
      render(<FollowUpBar options={['Roll back']} picked={new Set(['Deploy'])} onSelect={() => {}} onSend={() => {}} multi={false} />)
      const tip = tipFor('Roll back')
      expect(tip).toContain('Click to replace selection (double-click to send)')
      expect(tip).not.toMatch(/add to input/i)
    })

    it('single-select with a pick and no onSend says the click replaces', () => {
      render(<FollowUpBar options={['Roll back']} picked={new Set(['Deploy'])} onSelect={() => {}} multi={false} />)
      const tip = tipFor('Roll back')
      expect(tip).toContain('Click to replace selection')
      expect(tip).not.toMatch(/add to input/i)
    })

    it('multi omitted with a pick keeps the add-to-input wording (control)', () => {
      render(<FollowUpBar options={['Roll back']} picked={new Set(['Deploy'])} onSelect={() => {}} onSend={() => {}} />)
      expect(tipFor('Roll back')).toContain('Click to add to input (double-click to select and send)')
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
    // The chip IS a Glass host (components/Glass.tsx): its first <span>s are the
    // material layers (empty), so the label is the leaf span carrying the text.
    const labelSpan = (chip: HTMLElement) =>
      [...chip.querySelectorAll('span')].find(s => s.children.length === 0 && s.textContent === chip.textContent)

    it('caps chip width and clamps the label in the scroll layout', () => {
      render(<FollowUpBar options={[LONG]} picked={new Set()} onSelect={() => {}} layout="scroll" />)
      const chip = screen.getByRole('button', { name: LONG })
      expect(chip.className).toContain('followup-chip')
      // The clamp must sit on an unpadded inner element, not on the padded
      // button — otherwise a sliver of the next line shows in the padding.
      const label = labelSpan(chip)
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
      expect(labelSpan(chip)?.className).toContain('truncate')
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
          const span = labelSpan(screen.getByRole('button', { name: label }))
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
      fireEvent.focus(screen.getByRole('button', { name: LONG }))
      const tipEl = screen.getByRole('tooltip')
      const tip = tipEl.textContent ?? ''
      // Full label FIRST so the reader gets the unreadable part before the hint.
      expect(tip.startsWith(LONG)).toBe(true)
      expect(tip).toMatch(/double-click/i)
      // The width cap is viewport-bounded (narrow-viewport-required): a 26rem
      // bubble cannot fit a 320px screen, so the cap must yield to the viewport.
      expect(tipEl.className).toContain('max-w-[min(26rem,calc(100vw-1rem))]')
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
        fireEvent.focus(screen.getByRole('button', { name: option }))
        const tip = screen.getByRole('tooltip').textContent ?? ''
        expect(tip.startsWith(option)).toBe(true)
        expect(tip).toMatch(/double-click/i)
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
  /* Dispatch states (#6056). A plan chip dispatches a state-changing action, so
   * the bar has to say the click landed: without this a slow or failed dispatch
   * is a dead button, and the held-until-ack latch makes a re-click silently
   * refused on top of that.
   *
   * Four states, one visual vocabulary: idle, in-flight, latched-awaiting-ack
   * (deliberately the SAME spinner as in-flight — the two differ in duration,
   * not in what the user can do), and failed. */
  describe('dispatch states (#6056)', () => {
    const PLAN = ['Go', 'Go All', 'Cancel']
    const FAILED = 'Could not send that plan action.'
    const bar = (extra: Record<string, unknown> = {}) =>
      render(<FollowUpBar options={PLAN} picked={new Set()} onSelect={() => {}} {...extra} />)

    it('idle: a bar given neither prop is exactly what it was', () => {
      // This is also the SideChat / ChatEmbed contract: those surfaces render
      // chips but dispatch no plan action, so they pass nothing and draw nothing.
      const { container } = bar()
      expect(container.querySelector('.animate-spin')).toBeNull()
      expect(screen.queryByRole('alert')).toBeNull()
      for (const o of PLAN) {
        const b = screen.getByRole('button', { name: o })
        expect(b).not.toHaveAttribute('aria-disabled')
        expect(b.className).not.toContain('opacity-70')
      }
    })

    it('in-flight: the clicked chip spins and stops taking clicks', () => {
      bar({ pendingOptions: new Set(['Go']), refusedOptions: new Set(['Go', 'Go All']) })
      const go = screen.getByRole('button', { name: 'Go' })
      expect(go.querySelector('.animate-spin')).toBeTruthy()
      expect(go).toHaveAttribute('aria-disabled', 'true')
      expect(go).toHaveAttribute('aria-busy', 'true')
    })

    it('in-flight: only REFUSED chips dim, and live Cancel stays at full strength', () => {
      // `dim` means "this click is refused", not "a sibling is busy". Go and Go All
      // share one latch, so Go All dims; Cancel keeps its own and must not, because
      // a cold reader told us the dimmed stop control reads as locked — the dead
      // control this whole affordance exists to remove.
      const onSelect = vi.fn()
      render(<FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} pendingOptions={new Set(['Go'])} refusedOptions={new Set(['Go', 'Go All'])} />)
      const cancel = screen.getByRole('button', { name: 'Cancel' })
      const goAll = screen.getByRole('button', { name: 'Go All' })
      // Refused by the shared Go latch: dimmed, AND announced unavailable, because
      // its activation really is dropped. A dim that assistive tech could not see
      // would have left the dead button intact for anyone not looking at pixels.
      expect(goAll.className).toContain('opacity-70')
      expect(goAll).toHaveAttribute('aria-disabled', 'true')
      // Not refused: full strength, no spinner, and still live.
      expect(cancel.className).not.toContain('opacity-70')
      expect(cancel).not.toHaveAttribute('aria-disabled')
      expect(goAll).not.toBeDisabled()  // announced, never actually disabled
      expect(cancel.querySelector('.animate-spin')).toBeNull()
      fireEvent.click(cancel)
      expect(onSelect).toHaveBeenCalledWith('Cancel', expect.any(Object))
    })

    it('latched-awaiting-ack reuses the in-flight visual: exactly one spinner, no second affordance', () => {
      // `pendingOptions` is the bar's ONLY busy input. The host drives it from
      // the latch, which outlives the HTTP response, so the spinner covers the
      // silent window too — there is no third state for the user to decode.
      const { container } = bar({ pendingOptions: new Set(['Go All']), refusedOptions: new Set(['Go', 'Go All']) })
      expect(container.querySelectorAll('.animate-spin')).toHaveLength(1)
      expect(screen.getByRole('button', { name: 'Go All' }).querySelector('.animate-spin')).toBeTruthy()
    })

    it('two chips can spin at once, because Go and Cancel latch independently', () => {
      // Cancel is deliberately never blocked by a pending Go, so both classes can
      // be outstanding together. One busy LABEL could not say that: whichever
      // dispatch came second would un-spin the first chip, and an idle-looking
      // chip over a held latch is the dead button this whole affordance removes.
      const { container } = bar({ pendingOptions: new Set(['Go', 'Cancel']), refusedOptions: new Set(['Go', 'Go All', 'Cancel']) })
      expect(container.querySelectorAll('.animate-spin')).toHaveLength(2)
      expect(screen.getByRole('button', { name: 'Go' })).toHaveAttribute('aria-disabled', 'true')
      expect(screen.getByRole('button', { name: 'Cancel' })).toHaveAttribute('aria-disabled', 'true')
      // Go All dispatched nothing but shares Go's latch, so it is refused: it dims
      // AND is announced unavailable, while never being actually `disabled`.
      expect(screen.getByRole('button', { name: 'Go All' }).className).toContain('opacity-70')
      expect(screen.getByRole('button', { name: 'Go All' })).toHaveAttribute('aria-disabled', 'true')
      expect(screen.getByRole('button', { name: 'Go All' })).not.toBeDisabled()
    })

    it('a busy chip refuses clicks WITHOUT the disabled attribute, so its tooltip can still dismiss', () => {
      // A disabled control dispatches no mouse or focus events, so `InstantTip`'s
      // onMouseLeave / onBlur — its only pointer and keyboard dismissals — would
      // never fire, and a chip hovered then clicked would strand its tooltip over
      // the strip for as long as the latch is held.
      // BOTH chip shapes: without `onSend` the chip is a plain button, with it the
      // chip is a split button on a different code path — and the split one is
      // what both chat hosts actually render, so an attribute check that covered
      // only the plain shape would leave production unpinned.
      vi.useFakeTimers()
      for (const onSend of [undefined, vi.fn()]) {
        const onSelect = vi.fn()
        const { unmount } = render(
          <FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} onSend={onSend} pendingOptions={new Set(['Go'])} refusedOptions={new Set(['Go', 'Go All'])} />,
        )
        const go = screen.getByRole('button', { name: 'Go' })
        expect(go).toHaveAttribute('aria-disabled', 'true')
        expect(go).not.toBeDisabled()
        if (onSend) expect(screen.getByRole('button', { name: 'Send now: Go' })).not.toBeDisabled()
        // Still inert: the refusal lives in the handler, not the attribute.
        fireEvent.click(go)
        act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 30) })
        expect(onSelect).not.toHaveBeenCalled()
        if (onSend) { fireEvent.dblClick(go); expect(onSend).not.toHaveBeenCalled() }
        unmount()
      }
      vi.useRealTimers()
    })

    it('a DIMMED chip cannot dispatch even if its latch is released mid-debounce', () => {
      // The refused chip's click is DEBOUNCED, so the dispatch happens when the
      // timer fires and not at the click. Releasing the refusal inside that window
      // is the ordinary case, not a corner: a definitive 4xx on the sibling frees
      // the shared latch for retry. So the guard has to reject at CLICK time —
      // the hook's own `latch.has(vars.slot)` check cannot help, because by the
      // time the timer runs the latch it would have tested is gone.
      vi.useFakeTimers()
      // Both shapes: the split one is what the hosts render, and it is the one
      // whose handler owns the timer.
      for (const onSend of [undefined, vi.fn()]) {
        const onSelect = vi.fn()
        const { rerender, unmount } = render(
          <FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} onSend={onSend} pendingOptions={new Set(['Go'])} refusedOptions={new Set(['Go', 'Go All'])} />,
        )
        const goAll = screen.getByRole('button', { name: 'Go All' })
        // The dim sits on whichever element is the flex item: the button when the
        // chip is standalone, the WRAPPER when it is a split button.
        const dimHost = onSend ? goAll.parentElement! : goAll
        expect(dimHost.className).toContain('opacity-70')
        // Dimmed means refused, so it must also read as refused to the pointer —
        // on the button itself, which is what the cursor is resolved against.
        expect(goAll.className).toContain('cursor-default')
        expect(goAll.className).not.toContain('cursor-pointer')
        fireEvent.click(goAll)
        // Go's dispatch is rejected 400 and releases the shared latch: nothing is
        // pending or refused any more, and the chip is live again...
        rerender(
          <FollowUpBar options={PLAN} picked={new Set()} onSelect={onSelect} onSend={onSend} error="unknown action for this plan stage" />,
        )
        // ...and only NOW does the armed timer fire.
        act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 30) })
        expect(onSelect).not.toHaveBeenCalled()
        unmount()
      }
      vi.useRealTimers()
    })

    it('failed: the row renders through ErrorNotice, with the agent hand-off', () => {
      // A hand-rolled red div is banned by AUTOSDE `errors-use-error-notice`:
      // ErrorNotice is the one place that recovers the structured context and
      // offers it to the agent, so a bare role="alert" box is a dead end.
      bar({ error: 'bad action' })
      const row = screen.getByRole('alert')
      // The detail is the journal lookup key, so it must arrive UNPREFIXED.
      expect(row.querySelector('strong')).toHaveTextContent(FAILED)
      expect(row).toHaveTextContent('bad action')
      expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    })

    it('failed: one error row carries the sentence and the detail, and every chip is idle again', () => {
      bar({ error: 'bad action' })
      const row = screen.getByRole('alert')
      expect(row).toHaveTextContent(FAILED)
      expect(row).toHaveTextContent('bad action')
      for (const o of PLAN) {
        expect(screen.getByRole('button', { name: o })).not.toHaveAttribute('aria-disabled')
        expect(screen.getByRole('button', { name: o }).className).not.toContain('opacity-70')
      }
    })

    it('failed with no readable message still draws the row', () => {
      // `''` is a failure that said nothing, not the absence of a failure. It has
      // no structured context to look up, so the bar's sentence becomes the
      // message — ErrorNotice renders nothing at all for a falsy one.
      bar({ error: '' })
      const row = screen.getByRole('alert')
      expect(row).toHaveTextContent(FAILED)
      expect(row.querySelector('strong')).toBeNull()
    })

    it('draws at most ONE error row, and none without an error', () => {
      const { unmount } = bar({ error: 'bad action' })
      expect(screen.getAllByRole('alert')).toHaveLength(1)
      unmount()
      bar({ error: null })
      expect(screen.queryByRole('alert')).toBeNull()
    })

    it('the scroll layout draws the same states as the multiline one', () => {
      const { container } = bar({ layout: 'scroll', pendingOptions: new Set(['Go']), refusedOptions: new Set(['Go', 'Go All']), error: 'bad action' })
      expect(screen.getByRole('button', { name: 'Go' }).querySelector('.animate-spin')).toBeTruthy()
      expect(screen.getByRole('alert')).toHaveTextContent(FAILED)
      // Go All, refused by Go's latch. Cancel is untouched in either layout.
      expect(screen.getByRole('button', { name: 'Go All' }).className).toContain('opacity-70')
      expect(screen.getByRole('button', { name: 'Cancel' }).className).not.toContain('opacity-70')
      void container
    })

    it('a split chip dims as one piece and its Send-now segment goes inert', () => {
      // With onSend the WRAPPER is the flex item, so the dim has to sit there or
      // the arrow segment would stay bright beside a faded label.
      render(<FollowUpBar options={PLAN} picked={new Set()} onSelect={() => {}} onSend={() => {}} pendingOptions={new Set(['Go'])} refusedOptions={new Set(['Go', 'Go All'])} />)
      const go = screen.getByRole('button', { name: 'Go' })
      expect(go).toHaveAttribute('aria-disabled', 'true')
      expect(screen.getByRole('button', { name: 'Send now: Go' })).toHaveAttribute('aria-disabled', 'true')
      // The refused sibling dims on its WRAPPER, which is the split chip's flex item.
      expect(screen.getByRole('button', { name: 'Go All' }).parentElement?.className).toContain('opacity-70')
      // Cancel is not refused, so neither its wrapper nor its button dims.
      expect(screen.getByRole('button', { name: 'Cancel' }).parentElement?.className).not.toContain('opacity-70')
      expect(screen.getByRole('button', { name: 'Cancel' })).not.toHaveAttribute('aria-disabled')
      expect(go.parentElement?.className).not.toContain('opacity-70')
      // Item 2: the pending SPLIT chip must not keep the pointer hand. Asserting
      // `cursor-default` is PRESENT proves nothing on its own — both utilities at
      // equal specificity means Tailwind's emission order decides, and
      // `cursor-default` loses it. The load-bearing assertion is that
      // `cursor-pointer` is ABSENT, i.e. the two are mutually exclusive.
      expect(go.className).toContain('cursor-default')
      expect(go.className).not.toContain('cursor-pointer')
      // ...and that an idle chip still gets the pointer hand at all.
      const cancel = screen.getByRole('button', { name: 'Cancel' })
      expect(cancel.className).toContain('cursor-pointer')
      // The ARROW half of the same split chip refuses the click too
      // (`handleImmediateSend` opens with `if (pending) return`), so it must not
      // keep the pointer hand either. Same exclusivity rule as the body.
      const goSend = screen.getByRole('button', { name: 'Send now: Go' })
      expect(goSend.className).toContain('cursor-default')
      expect(goSend.className).not.toContain('cursor-pointer')
      // A live chip's arrow is still a pointer, so the assertion above is about
      // refusal and not about arrows in general.
      expect(screen.getByRole('button', { name: 'Send now: Cancel' }).className).toContain('cursor-pointer')
    })
  })
})
