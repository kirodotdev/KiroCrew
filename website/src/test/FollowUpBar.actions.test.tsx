/**
 * Action chips (`[OPTION-ACTIONS:]`) in the follow-up bar.
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
 *  2. **Disabled while any content pick exists.** Picks live as staged text in the
 *     composer and the shipped action (`close`) tears the composer down, so
 *     dispatching would silently discard text the user assembled. Blocking is
 *     recoverable; the discard is not.
 */
import { StrictMode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import FollowUpBar, { FOLLOWUP_CHIP_DEBOUNCE_MS } from '../components/FollowUpBar'
import type { OptionAction, UnsentWorkSource } from '../components/FollowUpBar'

// jsdom polyfill: the scroll layout uses ResizeObserver for its edge fades.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const CLOSE: OptionAction = { action: 'close', label: 'Nothing else, close this tab' }

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
      expect(actionChip().textContent).toContain('Closes this session')
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
      // DANGER, not accent. The accent palette was byte-identical to a SELECTED
      // content chip's apart from the background tint, so the chip that deletes the
      // session wore the paint that elsewhere means "you already picked this". The
      // same product paints the same operation `text-danger` in SessionActionsMenu.
      expect(content.className).toContain('text-muted')
      expect(action.className).toContain('text-danger')
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
      expect(onAction).toHaveBeenCalledWith(CLOSE, undefined)
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

    it('hands onAction the sourceKey as it was at click time', () => {
      const onAction = vi.fn()
      const { rerender } = render(
        <FollowUpBar
          options={[]}
          picked={new Set()}
          onSelect={() => {}}
          sourceKey="row-1"
          action={CLOSE}
          onAction={onAction}
        />,
      )
      fireEvent.click(actionChip())
      expect(onAction).toHaveBeenCalledWith(CLOSE, 'row-1')
      // A later row does not retroactively change what the first click reported —
      // the callee's own await is where the row can advance, and this snapshot is
      // what it compares against.
      rerender(
        <FollowUpBar
          options={[]}
          picked={new Set()}
          onSelect={() => {}}
          sourceKey="row-2"
          action={CLOSE}
          onAction={onAction}
        />,
      )
      fireEvent.click(actionChip())
      expect(onAction).toHaveBeenNthCalledWith(1, CLOSE, 'row-1')
      expect(onAction).toHaveBeenNthCalledWith(2, CLOSE, 'row-2')
    })

    it('does not throw when no onAction is supplied', () => {
      render(<FollowUpBar options={[]} picked={new Set()} onSelect={() => {}} action={CLOSE} />)
      expect(() => fireEvent.click(actionChip())).not.toThrow()
    })
  })

  describe('disabled while content picks exist', () => {
    it('is disabled and explains why once anything is picked', () => {
      render(
        <FollowUpBar
          options={['Alpha', 'Beta']}
          picked={new Set(['Alpha'])}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
        />,
      )
      const chip = actionChip()
      expect(chip).toHaveAttribute('aria-disabled', 'true')
      // The reason is the point of the disabled state: without it the chip reads as
      // broken rather than as blocked, and the user cannot learn how to unblock it.
      const title = chip.getAttribute('title') ?? ''
      expect(title).toMatch(/a selection is staged/i)
      expect(title).toMatch(/discard/i)
    })

    it('does not dispatch when clicked while disabled', () => {
      const onAction = vi.fn()
      render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set(['Alpha'])}
          onSelect={() => {}}
          action={CLOSE}
          onAction={onAction}
        />,
      )
      fireEvent.click(actionChip())
      expect(onAction).not.toHaveBeenCalled()
    })

    it('is enabled with no picks, and re-enables when the picks are cleared', () => {
      const { rerender } = render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set()}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
        />,
      )
      expect(actionChip()).toHaveAttribute('aria-disabled', 'false')
      // The block is recoverable, which is the argument for blocking rather than
      // discarding: unpick and the chip comes back.
      rerender(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set(['Alpha'])}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
        />,
      )
      expect(actionChip()).toHaveAttribute('aria-disabled', 'true')
      rerender(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set()}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
        />,
      )
      expect(actionChip()).toHaveAttribute('aria-disabled', 'false')
    })

    it('names the EFFECT in the hover text and the accessible name', () => {
      // The label alone was the whole hover text, and nothing anywhere on screen said
      // what the chip does: the label is model-authored free text, so
      // `close=That's all` rendered as `✕ That's all` and tore the session down on
      // one click. `confirmCloseSession` defaults to false, so there was no dialog to
      // state it either. A user cannot consent to an effect nobody named.
      render(<FollowUpBar options={[]} picked={new Set()} onSelect={() => {}} action={CLOSE} onAction={() => {}} />)
      const title = actionChip().getAttribute('title') ?? ''
      expect(title).toMatch(/closes this session/i)
      // The label survives inside it — a one-line clamp truncates a long one, and
      // hover is still how it stays readable.
      expect(title).toContain(CLOSE.label)
      // Same string as the accessible NAME, so a screen-reader user hears the
      // consequence rather than only the model's prose.
      expect(actionChip().getAttribute('aria-label')).toBe(title)
    })

    it('disables in the scroll layout on the same rule', () => {
      render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set(['Alpha'])}
          onSelect={() => {}}
          layout="scroll"
          action={CLOSE}
          onAction={() => {}}
        />,
      )
      expect(actionChip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('leaves content chips fully interactive while an action chip is disabled', () => {
      // The disable is scoped to the action, not a row-wide freeze: the user has to be
      // able to unpick, which is the recovery path.
      const onSelect = vi.fn()
      render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set(['Alpha'])}
          onSelect={onSelect}
          action={CLOSE}
          onAction={() => {}}
        />,
      )
      expect(screen.getByRole('button', { name: 'Alpha' })).toBeEnabled()
      fireEvent.click(screen.getByRole('button', { name: 'Alpha' }))
      expect(onSelect).toHaveBeenCalledTimes(1)
    })
  })

  describe('every block reason a host can supply', () => {
    // Each of these props is fed by a PR that is not open yet, so without these cases
    // the copy and the branch behind it would first execute untested in whichever PR
    // wires them. Driving them here from a fixture host is the whole point: the
    // component's contract is exercised now, against the strings it actually ships.
    const blockedNote = () => screen.getByRole('note').textContent ?? ''

    type BlockProps = {
      composerHasUnsentWork?: boolean
      composerCaptureInFlight?: boolean
      actionBlockedBySlot?: UnsentWorkSource | null
    }

    const cases: Array<[string, BlockProps, RegExp]> = [
      ['an unsent draft in this composer', { composerHasUnsentWork: true }, /send or clear your draft first/i],
      ['a recording or upload in flight', { composerCaptureInFlight: true }, /finish or cancel the recording first/i],
      ['a draft in another browser window', { actionBlockedBySlot: 'other-window' }, /another window holds a draft/i],
      ['a draft in another pane of this session', { actionBlockedBySlot: 'elsewhere' }, /another pane holds a draft/i],
      ['a draft that could not be checked', { actionBlockedBySlot: 'unverifiable' }, /couldn't check another window's draft/i],
    ]

    for (const [what, props, note] of cases) {
      it(`blocks on ${what}, with its own visible reason`, () => {
        const onAction = vi.fn()
        render(
          <FollowUpBar
            options={['Alpha']}
            picked={new Set()}
            onSelect={() => {}}
            action={CLOSE}
            onAction={onAction}
            {...props}
          />,
        )
        const chip = actionChip()
        expect(chip).toHaveAttribute('aria-disabled', 'true')
        // Visible, not title-only: a disabled button takes no focus and gets no hover.
        expect(blockedNote()).toMatch(note)
        // And the block is real, not just paint.
        fireEvent.click(chip)
        expect(onAction).not.toHaveBeenCalled()
      })
    }

    it('distinguishes a draft in another WINDOW from one in another PANE', () => {
      // `other-window` and `elsewhere` are separate union members precisely so the
      // copy can name a place the user can go. Folding them together would send
      // someone to the wrong surface, so pin that they differ.
      const { unmount } = render(
        <FollowUpBar
          options={[]}
          picked={new Set()}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
          actionBlockedBySlot="other-window"
        />,
      )
      const windowNote = blockedNote()
      unmount()
      render(
        <FollowUpBar
          options={[]}
          picked={new Set()}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
          actionBlockedBySlot="elsewhere"
        />,
      )
      expect(blockedNote()).not.toBe(windowNote)
    })

    it('a pick outranks every other reason, and a capture outranks a draft', () => {
      // The ladder's order is load-bearing: the most recoverable cause is the one to
      // name first, and two causes at once must not produce a note naming the wrong one.
      const { unmount } = render(
        <FollowUpBar
          options={['Alpha']}
          picked={new Set(['Alpha'])}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
          composerCaptureInFlight
          composerHasUnsentWork
          actionBlockedBySlot="other-window"
        />,
      )
      expect(blockedNote()).toMatch(/clear your selection first/i)
      unmount()
      render(
        <FollowUpBar
          options={[]}
          picked={new Set()}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
          composerCaptureInFlight
          composerHasUnsentWork
        />,
      )
      expect(blockedNote()).toMatch(/finish or cancel the recording first/i)
    })
  })

  describe('two bars blocked for the same reason describe themselves separately', () => {
    it('gives each instance its own reason id, so aria-describedby cannot cross panes', () => {
      // The main chat and a side chat both holding an unsent draft is the ordinary case.
      // An id keyed on the reason alone is identical in both, and `aria-describedby` then
      // resolves to whichever note is first in the document — a screen-reader user in one
      // pane hears the other pane's reason.
      const bar = (key: string) => (
        <FollowUpBar
          key={key}
          options={['Alpha']}
          picked={new Set()}
          onSelect={() => {}}
          action={CLOSE}
          onAction={() => {}}
          composerHasUnsentWork
        />
      )
      render(<div>{bar('main')}{bar('side')}</div>)

      const notes = screen.getAllByRole('note')
      expect(notes).toHaveLength(2)
      const ids = notes.map(n => n.id)
      expect(ids.every(Boolean)).toBe(true)
      expect(new Set(ids).size).toBe(2)

      // And each chip points at its OWN note, not merely at some note.
      const chips = screen.getAllByRole('button', { name: new RegExp(CLOSE.label, 'i') })
      expect(chips).toHaveLength(2)
      const described = chips.map(c => c.getAttribute('aria-describedby'))
      expect(new Set(described).size).toBe(2)
      for (const [i, chip] of chips.entries()) {
        expect(described[i]).toBe(notes[i].id)
        expect(document.getElementById(described[i] ?? '')).toBe(notes[i])
        expect(chip).toHaveAttribute('aria-disabled', 'true')
      }
    })
  })

  describe('the chip does not latch disabled under a double-invoked mount', () => {
    it('re-enables after an awaited action when React mounts, cleans up and remounts', async () => {
      // `main.tsx` wraps the app in <StrictMode>, so in development React runs
      // mount -> cleanup -> mount. A cleanup-only mounted-flag effect leaves the flag
      // `false` on a chip that is still on screen; the release then clears `inFlightRef`
      // but skips `setInFlight(false)`, so the chip paints and announces as permanently
      // unavailable while still dispatching. Rendering inside <StrictMode> reproduces the
      // double-invoke, so this case fails if the effect body stops re-arming the flag.
      let settle: (() => void) | undefined
      const onAction = vi.fn(() => new Promise<void>(resolve => { settle = resolve }))
      render(
        <StrictMode>
          <FollowUpBar
            options={['Alpha']}
            picked={new Set()}
            onSelect={() => {}}
            action={CLOSE}
            onAction={onAction}
          />
        </StrictMode>,
      )
      const chip = actionChip()
      fireEvent.click(chip)
      expect(onAction).toHaveBeenCalledTimes(1)
      // In flight, so disabled is correct here.
      expect(chip).toHaveAttribute('aria-disabled', 'true')
      await act(async () => {
        settle?.()
      })
      // Settled and still mounted, so the chip must be live again -- not a greyed control
      // that nonetheless dispatches.
      expect(chip).toHaveAttribute('aria-disabled', 'false')
    })
  })
})
