/**
 * The destructive action chip must be BLOCKED while the composer still holds
 * unsent work.
 *
 * `picked.size > 0` was the whole gate, and picks are only one of the ways the
 * composer comes to hold something. Typed text and staged attachments arrive by
 * other routes entirely — the user types, pastes, drops a file — and none of
 * them touch `picked`. So the chip stayed live, `close` unmounted the pane, and
 * the composer's local state went with it. There is no undo for that: the chip
 * is rendered by `ChatInput`, whose `value`/`pendingFiles` are the only copy.
 *
 * Two levels are asserted deliberately, because the gate and the wiring fail
 * independently:
 *  - the GATE, in `FollowUpBar`, given the flag directly; and
 *  - the WIRING, through `ChatInput`, which is the component that actually holds
 *    the draft. A correct gate that nothing feeds is still a live close button.
 *
 * The REASON is asserted alongside the disabled state, not as decoration. A
 * disabled chip with a wrong explanation is worse than none: the pre-existing
 * copy says "clear your selected options", which for a typed draft names an
 * action the user has not taken and cannot undo to recover the chip.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import FollowUpBar from '../components/FollowUpBar'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'
import { hasUnsentComposerWork } from '../utils/composerWork'
import type { OptionAction } from '../app-sdk/protocol/options'

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const CLOSE: OptionAction = { action: 'close', label: 'Nothing else, close this tab' }
// The chip's accessible NAME states the effect and carries the label inside it
// (`Closes this session — <label>`) in every state, because a bare model-authored
// label named no consequence. Match the label as a substring: an exact-name match
// would pass only for the old name this PR removed.
const CLOSE_NAME = new RegExp(CLOSE.label, 'i')
const chip = () => screen.getByRole('button', { name: CLOSE_NAME })

describe('action chip is blocked while the composer holds unsent work', () => {
  describe('the gate, in FollowUpBar', () => {
    it('a typed draft disables the chip', () => {
      render(
        <FollowUpBar
          options={[]} picked={new Set()} onSelect={vi.fn()}
          action={CLOSE} onAction={vi.fn()} composerHasUnsentWork
        />,
      )
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('keeps the blocked chip FOCUSABLE, so its reason is reachable', () => {
      // A natively `disabled` button takes no focus, so `aria-describedby` reached neither a
      // keyboard nor a touch user: they met a grey chip with no reason and no way to ask.
      const onAction = vi.fn()
      render(
        <FollowUpBar
          options={[]} picked={new Set()} onSelect={vi.fn()}
          action={CLOSE} onAction={onAction} composerHasUnsentWork
        />,
      )
      const button = chip()
      expect(button).not.toHaveAttribute('disabled')
      button.focus()
      expect(document.activeElement).toBe(button)
      expect(button).toHaveAttribute('aria-describedby')

      // Focusable must not mean actionable: the click is refused in the handler instead.
      fireEvent.click(button)
      expect(onAction).not.toHaveBeenCalled()
    })

    it('shows the short reason without needing hover or focus', () => {
      // Touch has no hover, so a reveal gated on either one left the reason unreachable and
      // the reserved width showing as a blank gap.
      render(
        <FollowUpBar
          options={[]} picked={new Set()} onSelect={vi.fn()}
          action={CLOSE} onAction={vi.fn()} composerHasUnsentWork
        />,
      )
      const note = document.querySelector('[role="note"] [aria-hidden="true"]')
      expect(note?.textContent?.trim()).not.toBe('')
      expect(note?.className ?? '').not.toContain('opacity-0')
    })

    it('the disabled reason names the DRAFT, not the picks', () => {
      render(
        <FollowUpBar
          options={[]} picked={new Set()} onSelect={vi.fn()}
          action={CLOSE} onAction={vi.fn()} composerHasUnsentWork
        />,
      )
      const title = chip().getAttribute('title') ?? ''
      expect(title).toMatch(/unsent|draft|typed/i)
      expect(title).toMatch(/discard/i)
      // The picks copy would misdirect: there are no picks to clear here.
      expect(title).not.toMatch(/options are selected/i)
    })

    describe('the visible reason must not grow the bar above the caret', () => {
      /**
       * `composerHasUnsentWork` flips true on the FIRST typed character, so this node
       * appears the instant a user starts a reply — directly above their caret — and
       * disappears on send. Rendering the ~90-char consequence there at `text-[11px]`
       * in a narrow column is several wrapped lines of churn on every draft.
       *
       * The fix may NOT be "truncate the explanation away", so these two assertions
       * are a pair: the VISIBLE text is short, and the FULL text is still in the
       * accessibility tree. Either one alone would pass for a broken fix.
       */
      const renderBlocked = () =>
        render(
          <FollowUpBar
            options={[]} picked={new Set()} onSelect={vi.fn()}
            action={CLOSE} onAction={vi.fn()} composerHasUnsentWork
          />,
        )

      it('renders a SHORT visible line, not the full consequence', () => {
        renderBlocked()
        const note = screen.getByRole('note')
        const visible = note.querySelector('[aria-hidden="true"]')
        expect(visible).not.toBeNull()
        expect(visible!.textContent ?? '').toMatch(/unavailable/i)
        // The long form is what caused the churn; it must not be the visible text.
        expect(visible!.textContent ?? '').not.toMatch(/text or attachments/i)
        expect((visible!.textContent ?? '').length).toBeLessThan(45)
      })

      it('keeps the FULL consequence in the accessibility tree', () => {
        renderBlocked()
        const note = screen.getByRole('note')
        const srOnly = note.querySelector('.sr-only')
        expect(srOnly).not.toBeNull()
        expect(srOnly!.textContent ?? '').toMatch(/text or attachments/i)
        expect(srOnly!.textContent ?? '').toMatch(/discard/i)
      })

      it('negative control: the pair can distinguish the two nodes', () => {
        // Both assertions above read the SAME element in a naive implementation
        // (one string in one span), which would let a truncated-away explanation
        // pass the second test. Requiring the two nodes to hold DIFFERENT text is
        // what makes them independent.
        renderBlocked()
        const note = screen.getByRole('note')
        const visible = note.querySelector('[aria-hidden="true"]')!.textContent ?? ''
        const srOnly = note.querySelector('.sr-only')!.textContent ?? ''
        expect(visible).not.toBe(srOnly)
        expect(srOnly.length).toBeGreaterThan(visible.length)
      })

      it('the chip still points at the note it is described by', () => {
        renderBlocked()
        const note = screen.getByRole('note')
        expect(chip()).toHaveAttribute('aria-describedby', note.id)
      })

      it('the blocked chip keeps an accessible name that IDENTIFIES it', () => {
        // The reason belongs in the DESCRIPTION, not the name. Making the name
        // "Unavailable while…" leaves a screen-reader user with a control whose name
        // never says what it would do, and it changes the name out from under
        // anything addressing the button by it.
        renderBlocked()
        const label = chip().getAttribute('aria-label') ?? ''
        expect(label).toMatch(/closes this session/i)
        expect(label).toContain(CLOSE.label)
        // And the reason is NOT the name — that is the specific confusion avoided.
        expect(label).not.toMatch(/unavailable/i)
        // It is still reachable, as the description.
        expect(screen.getByRole('note').textContent ?? '').toMatch(/unavailable/i)
      })
    })

    it('a blocked chip cannot dispatch', () => {
      const onAction = vi.fn()
      render(
        <FollowUpBar
          options={[]} picked={new Set()} onSelect={vi.fn()}
          action={CLOSE} onAction={onAction} composerHasUnsentWork
        />,
      )
      fireEvent.click(chip())
      expect(onAction).not.toHaveBeenCalled()
    })

    it('picks keep their OWN reason — precedence is unchanged', () => {
      // Picking stages text, so a picked row also has unsent work. The picks
      // message is the more actionable of the two and must still win.
      render(
        <FollowUpBar
          options={['Alpha']} picked={new Set(['Alpha'])} onSelect={vi.fn()}
          action={CLOSE} onAction={vi.fn()} composerHasUnsentWork
        />,
      )
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
      expect(chip().getAttribute('title') ?? '').toMatch(/options are selected/i)
    })

    it('an empty composer leaves the chip live, naming its effect', () => {
      render(
        <FollowUpBar
          options={[]} picked={new Set()} onSelect={vi.fn()}
          action={CLOSE} onAction={vi.fn()}
        />,
      )
      expect(chip()).toBeEnabled()
      // The live chip's hover text names the EFFECT and carries the label inside it.
      // It used to be the bare label, which named no consequence at all.
      const title = chip().getAttribute('title') ?? ''
      expect(title).toMatch(/closes this session/i)
      expect(title).toContain(CLOSE.label)
    })
  })

  describe('the wiring, through ChatInput', () => {
    const base = {
      onChange: vi.fn(),
      onSend: vi.fn(),
      followUpAction: CLOSE,
      onFollowUpAction: vi.fn(),
    }

    beforeEach(() => {
      vi.restoreAllMocks()
      stubStripHeights()
      localStorage.clear()
    })

    it('typed text in the composer disables the chip', () => {
      renderWithProviders(<ChatInput {...base} value="half-written thought" />)
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('a staged file disables the chip even with no text', () => {
      renderWithProviders(<ChatInput {...base} value="" pendingFiles={['/tmp/shot.png']} />)
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('whitespace alone is not unsent work', () => {
      // Otherwise a stray space disables the chip with no visible cause.
      renderWithProviders(<ChatInput {...base} value="   " />)
      expect(chip()).toBeEnabled()
    })

    it('staged KNOWLEDGE disables the chip even with an empty composer', () => {
      // The one kind of staged work that leaves NO trace in the text: a knowledge
      // selection is not a token, so `value` stays empty, `pendingFiles` stays
      // empty, and every text-derived term reads false. The chip stayed live and
      // the close destroyed the selection.
      //
      // `knowledgeChip` is the only knowledge signal this component receives, and
      // the host renders it if and only if a selection is pending — pinned by
      // `the chip presence tracks pendingKnowledge` below, so this guard is not
      // resting on an unasserted coupling.
      renderWithProviders(<ChatInput {...base} value="" knowledgeChip={<div data-testid="kchip">knowledge</div>} />)
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('staged PASTE BLOCKS disable the chip', () => {
      // Defence in depth rather than a proven loss: a block is normally reachable
      // from a token in the text, so `value` would already be non-empty. It is
      // included because the predicate must enumerate ALL staged content — a
      // predicate listing a SUBSET is exactly the drift that produced this class
      // of defect twice now.
      renderWithProviders(
        <ChatInput {...base} value="" pasteBlocks={[{ id: 'p1', text: 'pasted body', lines: 1 } as never]} />,
      )
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('no knowledge and no pastes leaves the chip live', () => {
      renderWithProviders(<ChatInput {...base} value="" knowledgeChip={undefined} pasteBlocks={[]} />)
      expect(chip()).toBeEnabled()
    })

    it('an ACTIVE VOICE CAPTURE disables the chip with an empty composer', () => {
      // `voiceCaptureActive` is the UNGATED flag on purpose: `voiceRecording` is
      // `owned && recording`, and ownership lands only after the server handshake,
      // so it reads false while real audio is already buffering — the cold window
      // the finding names. The gate has to close on the ungated one.
      renderWithProviders(<ChatInput {...base} value="" voiceCaptureActive />)
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('a TRANSCRIPTION still resolving also disables the chip', () => {
      // Capture has ended but the text has not arrived; closing now loses it just
      // the same. Asserted separately because it is a different flag, and one of
      // the two passing is not evidence for the other.
      renderWithProviders(<ChatInput {...base} value="" voiceTranscribing />)
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('negative control: no capture and no transcription leaves the chip live', () => {
      // Ties the two assertions above to the voice flags rather than to something
      // else in this fixture disabling the chip.
      renderWithProviders(
        <ChatInput {...base} value="" voiceCaptureActive={false} voiceTranscribing={false} />,
      )
      expect(chip()).toBeEnabled()
    })

    it('an upload IN FLIGHT disables the chip with an empty composer', () => {
      // The host already tells ChatInput an upload is running — it drives the
      // spinner on the attach button — so the render gate can close on the same
      // signal the settle-time recheck uses. Before this, the chip stayed live
      // through the whole request and a close discarded the selected file.
      renderWithProviders(<ChatInput {...base} value="" pendingFiles={[]} uploading />)
      expect(chip()).toHaveAttribute('aria-disabled', 'true')
    })

    it('negative control: the identical row is live once the upload settles', () => {
      // Pins the assertion above to `uploading` specifically, rather than to the
      // chip being disabled for some unrelated reason in this fixture.
      renderWithProviders(<ChatInput {...base} value="" pendingFiles={[]} uploading={false} />)
      expect(chip()).toBeEnabled()
    })

    it('an empty composer leaves the chip live', () => {
      renderWithProviders(<ChatInput {...base} value="" />)
      expect(chip()).toBeEnabled()
    })
  })
})

describe('hasUnsentComposerWork counts a derived boolean flag', () => {
  // The knowledge and chip-presence terms arrive as already-derived booleans
  // rather than arrays, so the predicate has to honour them or the guard above
  // silently does nothing.
  it('a true flag is work; a false flag is not', () => {
    const none = {
      text: '', files: [], dirs: [], sessionRefs: [], pasteBlocks: [],
      knowledge: false, uploading: false, voiceCapture: false,
    }
    expect(hasUnsentComposerWork({ ...none, knowledge: true })).toBe(true)
    expect(hasUnsentComposerWork(none)).toBe(false)
    expect(hasUnsentComposerWork({ ...none, pasteBlocks: [{}] })).toBe(true)
    expect(hasUnsentComposerWork({ ...none, text: '   ' })).toBe(false)
  })

  it('an upload IN FLIGHT is unsent work, with nothing staged yet', () => {
    /**
     * `pendingFiles` is written by the upload RESULT, not by the file picker. So
     * between the picker closing and the response landing, every other term reads
     * false while the user has already committed an attachment — and a close in
     * that window deletes the pane, the upload resolves into a slot that no longer
     * exists, and the file is gone with no error raised anywhere.
     *
     * The same shape of hole `knowledge` was: real unsent work leaving no trace in
     * any collection the predicate walks.
     */
    const none = {
      text: '', files: [], dirs: [], sessionRefs: [], pasteBlocks: [],
      knowledge: false, uploading: false, voiceCapture: false,
    }
    expect(hasUnsentComposerWork({ ...none, uploading: true })).toBe(true)
    // Negative control: the identical shape without the upload must be FALSE, or
    // the assertion above would pass for a predicate that answers true always.
    expect(hasUnsentComposerWork(none)).toBe(false)
  })

  it('an active voice CAPTURE is unsent work, with nothing staged yet', () => {
    /**
     * The widest window of the three, and it is widest exactly when the composer
     * looks emptiest: a streaming capture that has produced no partial yet leaves
     * `text` empty, so every text-derived term reads false while the user is
     * mid-sentence. Closing there disarms voice as the slot goes away, and the
     * final transcript is dropped with nothing on screen to show it existed.
     */
    const none = {
      text: '', files: [], dirs: [], sessionRefs: [], pasteBlocks: [],
      knowledge: false, uploading: false, voiceCapture: false,
    }
    expect(hasUnsentComposerWork({ ...none, voiceCapture: true })).toBe(true)
    // Negative control: same shape, no capture — FALSE. Without it the assertion
    // above passes for a predicate that has started answering true for anything.
    expect(hasUnsentComposerWork(none)).toBe(false)
  })
})
