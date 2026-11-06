/**
 * Action chips (`<!-- option-action:close=… -->`) in the follow-up bar.
 *
 * Two properties are the acceptance criteria for the render layer, and both are
 * things the plan-action precedent got wrong:
 *
 *  1. **Exactly ONE click entry point.** A content chip has three routes to the text
 *     path — an undebounced `onSelect`, a debounced `onSelect` plus a double-click
 *     `onSend`, and the `▲` split segment's `onSend`. A plan chip intercepts only the
 *     first, so a double-click or the `▲` segment still sends its label as chat text.
 *     An action chip must reach NONE of them: the tests below drive every one of those
 *     three gestures at an action chip and assert `onSelect`/`onSend` stay untouched,
 *     including advancing past the debounce window so a silently-armed timer cannot
 *     hide behind a synchronous assertion.
 *
 *  2. **Click twice to confirm.** Where the host's close would not ask on its own, the
 *     first click only arms the chip, and the action runs on a second click within
 *     `ACTION_CONFIRM_WINDOW_MS`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import FollowUpBar, { ACTION_CONFIRM_WINDOW_MS, FOLLOWUP_CHIP_DEBOUNCE_MS } from '../components/FollowUpBar'
import type { OfferedAction } from '../app-sdk/protocol/optionActionTag'

// jsdom polyfill: the scroll layout uses ResizeObserver for its edge fades.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const CLOSE: OfferedAction = { action: 'close', label: 'Nothing else, close this tab' }

/** The `▲` segments, i.e. the direct-send controls a content chip offers. */
const sendSegments = () => screen.queryAllByRole('button', { name: /^Send now:/ })

// The accessible name now NAMES THE EFFECT and carries the label inside it
// (`Closes this session — <label>`), so match on the label as a substring. A
// whole-name match would pass only for the old bare-label name this PR removed.
const actionChip = () => screen.getByRole('button', { name: new RegExp(CLOSE.label, 'i') })

describe('FollowUpBar action chips', () => {
  describe('rendering', () => {
    it('renders an action chip carrying its label and the action as a data attribute', () => {
      render(
        <FollowUpBar options={['Alpha']} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} />,
      )
      const chip = actionChip()
      expect(chip).toBeInTheDocument()
      // The data attribute is the stable hook for a host or a test that must find the
      // chip without matching model-authored label text.
      expect(chip.getAttribute('data-option-action')).toBe('close')
    })

    it('puts the multiline action on its own row, so two short options and the action never share one three-button row', () => {
      render(
        <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} />,
      )
      const group = actionChip().parentElement!
      expect(group.className).toMatch(/\bbasis-full\b/)
      expect(group.className).not.toMatch(/\bborder-l\b/)
    })

    it('draws the scroll-layout divider only side by side, never when the action stacks under a narrow strip', () => {
      render(
        <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} layout="scroll" />,
      )
      const group = actionChip().parentElement!
      expect(group.className).toMatch(/@\[32rem\]\/followup:border-l/)
      expect(group.className).not.toMatch(/(^|\s)border-l(\s|$)/)
      expect(group.className).not.toMatch(/\bbasis-full\b/)
    })

    it('pins the scroll-layout action outside the scrolling strip, so a long row can never scroll the close warning out of view', () => {
      render(
        <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} layout="scroll" />,
      )
      expect(screen.getByRole('button', { name: 'Alpha' }).closest('[data-tip-boundary]')).not.toBeNull()
      expect(actionChip().closest('[data-tip-boundary]')).toBeNull()
    })

    it('wraps the action label instead of truncating it, so the model suggestion is never clipped off screen', () => {
      render(<FollowUpBar options={[]} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} />)
      const label = actionChip().querySelector('[data-action-label]')!
      expect(label.className).toMatch(/(^|\s)break-words(\s|$)/)
      expect(actionChip().querySelector('.truncate')).toBeNull()
    })

    it('stacks the scroll-layout action under the strip below 32rem, so a long label cannot squeeze the options to zero width', () => {
      render(
        <FollowUpBar options={['Alpha', 'Beta']} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} layout="scroll" />,
      )
      const row = document.querySelector('[data-followup-scroll-row]')!
      expect(row.className).toMatch(/(^|\s)flex-col(\s|$)/)
      expect(row.className).toMatch(/@\[32rem\]\/followup:flex-row/)
      expect(row.closest('.\\@container\\/followup')).not.toBeNull()
    })

    it('renders action chips AFTER every content chip', () => {
      const { container } = render(
        <FollowUpBar
          options={['Alpha', 'Beta']}
          picked={new Set()}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
      />,
      )
      const labels = [...container.querySelectorAll('button')]
        .map(b => b.textContent?.trim())
        .filter((t): t is string => !!t)
      // The action chip's VISIBLE text carries the consequence around the label, so
      // find it by the label as a substring rather than by an exact match.
      expect(labels.findIndex(t => t.includes(CLOSE.label))).toBeGreaterThan(labels.indexOf('Beta'))
    })

    it('states the consequence in VISIBLE text, not only on hover', () => {
      // A touch user gets no hover and the model's label is free prose that need
      // never mention closing, so a label-only chip offered a one-tap session
      // teardown named nowhere on screen. Asserting the rendered text — not the
      // title or the accessible name, both of which already carried it.
      render(
        <FollowUpBar options={['Alpha']} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} />,
      )
      expect(actionChip().textContent).toContain('Close session')
    })

    it('renders an action-only row — no content options at all', () => {
      // The zero-turn case the feature exists for: a row whose only offer is the
      // action. An options-gated bar would drop it entirely.
      render(<FollowUpBar options={[]} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} />)
      expect(actionChip()).toBeInTheDocument()
    })

    it('renders action chips in the scroll layout too', () => {
      render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set()}
          onSelect={() => {}}
          layout="scroll"
          action={CLOSE}
          onAction={() => {}}
      />,
      )
      expect(actionChip()).toBeInTheDocument()
    })

    it('renders nothing extra when no actions are supplied', () => {
      // The prop is optional, so every existing caller must be byte-for-byte
      // unaffected — no stray chip, no empty wrapper button.
      render(<FollowUpBar options={['Alpha']} picked={new Set()} onSelect={() => {}} />)
      expect(screen.getAllByRole('button')).toHaveLength(1)
      expect(document.querySelector('[data-option-action]')).toBeNull()
    })

    it('is visually distinct from a content chip at rest', () => {
      render(
        <FollowUpBar options={['Alpha']} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} />,
      )
      const content = screen.getByRole('button', { name: 'Alpha' })
      const action = actionChip()
      // DANGER, not accent, but only on the border at rest: red text on an idle chip
      // read as an error on an ordinary choice. The text turns danger on hover.
      expect(content.className).toContain('text-muted')
      expect(action.className).toMatch(/(^|\s)text-muted(\s|$)/)
      expect(action.className).not.toMatch(/(^|\s)text-danger(\s|$)/)
      expect(action.className).toContain('hover:text-danger-text')
      expect(action.className).toContain('border-danger/50')
      // And NOT the accent palette, which is the specific confusion being removed —
      // without this the assertions above would pass for a chip carrying both.
      expect(action.className).not.toContain('text-accent')
      // Plus a glyph, which a low-contrast or colour-blind user cannot miss.
      expect(action.querySelector('svg')).not.toBeNull()
    })

  })

  describe('exactly one click entry point', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    it('a single click reaches onAction and NEITHER onSelect NOR onSend', () => {
      const onAction = vi.fn()
      const onSelect = vi.fn()
      const onSend = vi.fn()
      render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set()}
          onSelect={onSelect}
          onSend={onSend}
          action={CLOSE}
          onAction={onAction}
      />,
      )
      fireEvent.click(actionChip())
      // Past the content chip's debounce window: a timer armed by mistake would fire
      // here, and a synchronous-only assertion would never see it.
      act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 50) })
      expect(onAction).toHaveBeenCalledTimes(1)
      expect(onAction).toHaveBeenCalledWith(CLOSE)
      expect(onSelect).not.toHaveBeenCalled()
      expect(onSend).not.toHaveBeenCalled()
    })

    it('a double click does not send — it is two plain dispatches, never onSend', () => {
      const onAction = vi.fn()
      const onSend = vi.fn()
      const onSelect = vi.fn()
      render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set()}
          onSelect={onSelect}
          onSend={onSend}
          action={CLOSE}
          onAction={onAction}
      />,
      )
      const chip = actionChip()
      fireEvent.doubleClick(chip)
      act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 50) })
      // This is the exact gesture that leaks on a plan chip.
      expect(onSend).not.toHaveBeenCalled()
      expect(onSelect).not.toHaveBeenCalled()
      // POSITIVE CONTROL. Without it the two assertions above could pass because
      // `fireEvent.doubleClick` never reaches React's `onDoubleClick` at all, in which
      // case this test would report "no leak" no matter what the component did. The
      // same gesture on a CONTENT chip must send.
      fireEvent.doubleClick(screen.getByRole('button', { name: 'Alpha' }))
      act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 50) })
      expect(onSend).toHaveBeenCalledTimes(1)
    })

    it('offers no ▲ send segment, even where content chips have one', () => {
      render(
        <FollowUpBar
          options={['Alpha', 'Beta']}
          picked={new Set()}
          onSelect={() => {}}
          onSend={() => {}}
          action={CLOSE}
          onAction={() => {}}
      />,
      )
      // Two content chips each keep theirs — proving the assertion below is a fact
      // about the action chip, not about the segment being absent everywhere.
      expect(sendSegments()).toHaveLength(2)
      expect(actionChip().querySelector('[aria-label^="Send now"]')).toBeNull()
    })

    it('a quickSend row still cannot send an action chip', () => {
      // quickSend is the state where a content chip's single click sends immediately.
      // An action chip must be unaffected by it.
      const onAction = vi.fn()
      const onSend = vi.fn()
      const onSelect = vi.fn()
      render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set()}
          onSelect={onSelect}
          onSend={onSend}
          quickSend
          action={CLOSE}
          onAction={onAction}
      />,
      )
      fireEvent.click(actionChip())
      act(() => { vi.advanceTimersByTime(FOLLOWUP_CHIP_DEBOUNCE_MS + 50) })
      expect(onAction).toHaveBeenCalledTimes(1)
      expect(onSend).not.toHaveBeenCalled()
      expect(onSelect).not.toHaveBeenCalled()
    })

    it('does not throw when no onAction is supplied', () => {
      render(<FollowUpBar options={[]} picked={new Set()} onSelect={() => {}} action={CLOSE} />)
      expect(() => fireEvent.click(actionChip())).not.toThrow()
    })
  })

  describe('click twice to confirm', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    const renderArming = (onAction: () => void, needsSecondClick: () => boolean = () => true, sourceKey = 'row-1', scope = 'slot-a') =>
      <FollowUpBar options={['Alpha']} picked={new Set()} onSelect={() => {}} sourceKey={sourceKey} action={CLOSE} onAction={onAction} actionNeedsSecondClick={needsSecondClick} actionScope={scope} />

    it('arms on the first click and runs on the second', () => {
      const onAction = vi.fn()
      render(renderArming(onAction))
      fireEvent.click(actionChip())
      expect(onAction).not.toHaveBeenCalled()
      const armed = screen.getByRole('button', { name: /^Click again to close/ })
      expect(armed).toHaveAttribute('data-armed')
      expect(screen.getByRole('status')).toHaveTextContent(`Click again to close — ${CLOSE.label}`)
      fireEvent.click(armed)
      expect(onAction).toHaveBeenCalledTimes(1)
      expect(onAction).toHaveBeenCalledWith(CLOSE)
    })

    it('keeps its width when it arms, so the confirming click lands on the chip', () => {
      render(renderArming(vi.fn(), () => true))
      const rest = actionChip().querySelector('[data-action-label]')!
      const armedLabel = actionChip().querySelector('[data-action-armed-label]')!
      expect(rest.parentElement).toBe(armedLabel.parentElement)
      expect(rest.parentElement!.className).toMatch(/(^|\s)grid(\s|$)/)
      for (const el of [rest, armedLabel]) expect(el.className).toMatch(/col-start-1 row-start-1/)
      expect(rest.className).not.toMatch(/invisible/)
      expect(armedLabel.className).toMatch(/(^|\s)invisible(\s|$)/)
      fireEvent.click(actionChip())
      expect(rest.className).toMatch(/(^|\s)invisible(\s|$)/)
      expect(armedLabel.className).not.toMatch(/invisible/)
      expect(armedLabel.textContent).toBe(`Click again to close — ${CLOSE.label}`)
    })

    it('reverts after the confirm window, so a late second click only arms again', () => {
      const onAction = vi.fn()
      render(renderArming(onAction))
      fireEvent.click(actionChip())
      act(() => { vi.advanceTimersByTime(ACTION_CONFIRM_WINDOW_MS) })
      expect(actionChip()).not.toHaveAttribute('data-armed')
      expect(screen.getByRole('status')).toHaveTextContent('')
      fireEvent.click(actionChip())
      expect(onAction).not.toHaveBeenCalled()
      expect(screen.getByRole('button', { name: /^Click again to close/ })).toBeInTheDocument()
    })

    it('needs a fresh pair of clicks after it runs, so a third click closes nothing', () => {
      const onAction = vi.fn()
      render(renderArming(onAction))
      fireEvent.click(actionChip())
      fireEvent.click(screen.getByRole('button', { name: /^Click again to close/ }))
      fireEvent.click(actionChip())
      expect(onAction).toHaveBeenCalledTimes(1)
    })

    it('runs on one click when the host close asks on its own', () => {
      const onAction = vi.fn()
      render(renderArming(onAction, () => false))
      fireEvent.click(actionChip())
      expect(onAction).toHaveBeenCalledTimes(1)
      expect(actionChip()).not.toHaveAttribute('data-armed')
    })

    it('asks the host at click time, not at render time', () => {
      const onAction = vi.fn()
      let asks = false
      render(renderArming(onAction, () => asks))
      asks = true
      fireEvent.click(actionChip())
      expect(onAction).not.toHaveBeenCalled()
    })

    it('starts unarmed on a newer offer, so an old first click never carries over', () => {
      const onAction = vi.fn()
      const { rerender } = render(renderArming(onAction))
      fireEvent.click(actionChip())
      rerender(renderArming(onAction, () => true, 'row-2'))
      expect(actionChip()).not.toHaveAttribute('data-armed')
      fireEvent.click(actionChip())
      expect(onAction).not.toHaveBeenCalled()
    })

    it('starts unarmed in another session, even when its reply has the same sourceKey', () => {
      const onAction = vi.fn()
      const { rerender } = render(renderArming(onAction, () => true, 'row-1', 'slot-a'))
      fireEvent.click(actionChip())
      rerender(renderArming(onAction, () => true, 'row-1', 'slot-fork'))
      expect(actionChip()).not.toHaveAttribute('data-armed')
      fireEvent.click(actionChip())
      expect(onAction).not.toHaveBeenCalled()
    })
  })
})
