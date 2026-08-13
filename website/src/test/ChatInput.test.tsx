import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { safeSetItem } from '../utils/safeStorage'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import type { PasteBlock } from '../utils/pasteTokens'

// isTouchDevice gates the autoFocusKey effect (tapping a session must not pop
// the soft keyboard). Default false so the desktop-focus tests below behave as
// before; the touch-device case flips it on per-test.
const touchEnv = vi.hoisted(() => ({ touch: false }))
vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => touchEnv.touch }))

const mobileEnv = vi.hoisted(() => ({ mobile: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => mobileEnv.mobile }))

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  touchEnv.touch = false
  mobileEnv.mobile = false
})

describe('ChatInput', () => {
  describe('rendering', () => {
    it('renders textarea with Message input label', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByLabelText('Message input')).toBeInTheDocument()
    })

    it('renders Send button', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument()
    })

    it('renders drag handle', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByTitle(/Drag to resize/)).toBeInTheDocument()
    })

    it('uses custom placeholder', () => {
      renderWithProviders(<ChatInput {...defaultProps} placeholder="Type here…" />)
      expect(screen.getByPlaceholderText('Type here…')).toBeInTheDocument()
    })

    it('shows Stopping placeholder when disabled', () => {
      renderWithProviders(<ChatInput {...defaultProps} disabled />)
      expect(screen.getByPlaceholderText('Stopping…')).toBeInTheDocument()
    })

    it('shows offline placeholder when connected=false', () => {
      renderWithProviders(<ChatInput {...defaultProps} connected={false} />)
      expect(screen.getByPlaceholderText(/Gateway offline/)).toBeInTheDocument()
    })

    it('makes the touch-device + control open the native file picker directly in a wide viewport', () => {
      touchEnv.touch = true
      renderWithProviders(<ChatInput {...defaultProps} onUploadFiles={vi.fn()} />)

      const input = screen.getAllByLabelText('Attach files').find((element) => element.tagName === 'INPUT')
      const mobilePlus = screen.getByTitle('Attach files').closest('label')
      expect(input).toBeDefined()
      expect(mobilePlus).toHaveAttribute('for', input?.id)
      expect(screen.queryByRole('button', { name: 'Add files & options' })).not.toBeInTheDocument()
      expect(screen.queryByText('Upload file')).not.toBeInTheDocument()
    })
  })

  describe('above-composer stacking order', () => {
    // The tip / folder-suggestion band must stay flush against the input box:
    // options belong with the transcript above it, never between it and the
    // composer. DOCUMENT_POSITION_FOLLOWING === 4.
    const FOLLOWING = Node.DOCUMENT_POSITION_FOLLOWING

    it('renders aboveComposer below the options row and above the textarea', () => {
      renderWithProviders(
        <ChatInput
          {...defaultProps}
          aboveComposer={<div data-testid="tip-band">tip</div>}
          followUpOptions={['first option', 'second option']}
          onFollowUpSelect={vi.fn()}
        />
      )
      const option = screen.getByRole('button', { name: 'first option' })
      const tip = screen.getByTestId('tip-band')
      const textarea = screen.getByLabelText('Message input')

      expect(option.compareDocumentPosition(tip) & FOLLOWING).toBe(FOLLOWING)
      expect(tip.compareDocumentPosition(textarea) & FOLLOWING).toBe(FOLLOWING)
    })

    it('keeps aboveComposer below the knowledge chip', () => {
      renderWithProviders(
        <ChatInput
          {...defaultProps}
          aboveComposer={<div data-testid="tip-band">tip</div>}
          knowledgeChip={<div data-testid="knowledge-chip">ctx</div>}
        />
      )
      const chip = screen.getByTestId('knowledge-chip')
      const tip = screen.getByTestId('tip-band')

      expect(chip.compareDocumentPosition(tip) & FOLLOWING).toBe(FOLLOWING)
    })

    it('still renders aboveComposer when the options row is absent', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} aboveComposer={<div data-testid="tip-band">tip</div>} />
      )
      expect(screen.getByTestId('tip-band')).toBeInTheDocument()
    })
  })

  describe('offline state', () => {
    it('disables Send button when connected=false even with text', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hello" connected={false} />)
      const btn = screen.getByRole('button', { name: /Send disabled/ })
      expect(btn).toBeDisabled()
    })

    it('exposes offline-aware aria-label on Send button when offline', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hi" connected={false} />)
      expect(screen.getByLabelText('Send disabled — gateway offline')).toBeInTheDocument()
    })

    it('shows tooltip explaining offline state on Send button', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hi" connected={false} />)
      const btn = screen.getByRole('button', { name: /Send disabled/ })
      expect(btn).toHaveAttribute('title', 'Gateway offline — reconnect to send')
    })

    it('keeps Send enabled when connected=true (default)', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hi" />)
      expect(screen.getByRole('button', { name: 'Send' })).not.toBeDisabled()
    })

    it('disables Optimize button when connected=false even with text', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hello" connected={false} />)
      const btn = screen.getByRole('button', { name: /Optimize disabled/ })
      expect(btn).toBeDisabled()
    })

    it('exposes offline-aware aria-label and tooltip on Optimize button', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hi" connected={false} />)
      const btn = screen.getByLabelText('Optimize disabled — gateway offline')
      expect(btn).toHaveAttribute('title', 'Gateway offline — reconnect to optimize')
    })

    it('keeps Optimize enabled when connected=true with text', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hi" />)
      expect(screen.getByRole('button', { name: 'Optimize prompt' })).not.toBeDisabled()
    })

    // Keyboard send must not bypass the offline gate. Without it, hitting Enter
    // while offline would call onSend() directly via handleKeyDown, triggering
    // ChatPage.send() → setInput('') → createSlot network call → fail silently,
    // losing the user's typed draft with no recovery path. The sendKey branch is
    // gated on `connected`, with defense-in-depth in ChatPage.send(). These tests
    // pin both legs.
    it('Enter key when offline does NOT call onSend (default sendOnEnter=enter)', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="hello" onSend={onSend} connected={false} />)
      const ta = screen.getByLabelText('Message input')
      fireEvent.keyDown(ta, { key: 'Enter' })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('Ctrl+Enter when offline does NOT call onSend (sendOnEnter=ctrl-enter)', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="hello" onSend={onSend} connected={false} sendOnEnter="ctrl-enter" />)
      const ta = screen.getByLabelText('Message input')
      fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('Enter key when connected DOES call onSend (positive control)', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="hello" onSend={onSend} connected={true} />)
      const ta = screen.getByLabelText('Message input')
      fireEvent.keyDown(ta, { key: 'Enter' })
      expect(onSend).toHaveBeenCalled()
    })

    // Note: Cmd+Shift+Enter optimize-shortcut gating is exercised in code
    // (handleKeyDown line ~1043 has `&& connected`) but NOT pinned with a
    // dedicated test here. A meaningful test requires observing
    // optimizePrompt() directly, which goes through a network/dispatch
    // boundary not surfaced as a prop. The risk if the gate regresses is
    // a no-op network call when offline (no data loss), so the shielding
    // is lower-priority than the Enter→onSend path above.
  })

  describe('send behavior', () => {
    it('disables Send button when input is empty', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    })

    it('enables Send button when input has text', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hello" />)
      expect(screen.getByRole('button', { name: 'Send' })).not.toBeDisabled()
    })

    it('enables Send button when pendingFiles has items even without text', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/img.png']} />)
      expect(screen.getByRole('button', { name: 'Send' })).not.toBeDisabled()
    })

    it('disables Send button when disabled prop is true', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="hello" disabled />)
      expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    })

    it('calls onSend on button click', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} />)
      fireEvent.click(screen.getByRole('button', { name: 'Send' }))
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('calls onSend on Enter key when sendOnEnter is true', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('does not call onSend on Shift+Enter', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', shiftKey: true })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('does not call onSend on Enter during IME composition', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter" />)
      const ta = screen.getByLabelText('Message input')
      const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })
      Object.defineProperty(event, 'isComposing', { value: true })
      ta.dispatchEvent(event)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('does not send on Enter immediately after compositionEnd (50ms guard)', () => {
      vi.useFakeTimers()
      try {
        const onSend = vi.fn()
        renderWithProviders(<ChatInput {...defaultProps} value="hello" onSend={onSend} sendOnEnter={true} />)
        const ta = screen.getByLabelText('Message input')
        // Simulate: user types English in Chinese IME, presses Enter to commit
        fireEvent.compositionStart(ta)
        fireEvent.compositionEnd(ta)
        // Enter arrives immediately after compositionEnd — isComposing is false
        fireEvent.keyDown(ta, { key: 'Enter', isComposing: false })
        expect(onSend).not.toHaveBeenCalled()
        // After 50ms guard, Enter should work again
        vi.advanceTimersByTime(50)
        fireEvent.keyDown(ta, { key: 'Enter', isComposing: false })
        expect(onSend).toHaveBeenCalledOnce()
      } finally {
        vi.useRealTimers()
      }
    })

    it('consumes the swallowed Enter so no newline lands in the draft', () => {
      // The reported symptom: pick a candidate, press Enter to send, and the draft
      // gains a line break instead. The guard is allowed to decline the submit; it is
      // not allowed to let the textarea's default action edit the text. `fireEvent`
      // returns false when a handler called preventDefault.
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="你好" onSend={onSend} sendOnEnter="enter" />)
      const ta = screen.getByLabelText('Message input')
      fireEvent.compositionStart(ta)
      fireEvent.compositionEnd(ta)
      const notCancelled = fireEvent.keyDown(ta, { key: 'Enter', isComposing: false })
      expect(notCancelled).toBe(false)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('does not call onSend on Enter when sendOnEnter is false', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="ctrl-enter" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('calls onSend on Enter when sendOnEnter is enter-ctrl-newline', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter-ctrl-newline" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('does not call onSend on Ctrl+Enter when sendOnEnter is enter-ctrl-newline', () => {
      const onSend = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="test" onSend={onSend} sendOnEnter="enter-ctrl-newline" />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter', ctrlKey: true })
      expect(onSend).not.toHaveBeenCalled()
    })

    it('inserts newline on Ctrl+Enter when sendOnEnter is enter-ctrl-newline', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="hello" onChange={onChange} sendOnEnter="enter-ctrl-newline" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      Object.defineProperty(ta, 'selectionStart', { value: 3, writable: true })
      Object.defineProperty(ta, 'selectionEnd', { value: 3, writable: true })
      fireEvent.keyDown(ta, { key: 'Enter', ctrlKey: true })
      expect(onChange).toHaveBeenCalledWith('hel\nlo')
    })
  })

  describe('onChange', () => {
    it('calls onChange when typing', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} />)
      fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'hi' } })
      expect(onChange).toHaveBeenCalledWith('hi')
    })
  })

  describe('prefill hint', () => {
    it('shows prefill hint when enabled', () => {
      renderWithProviders(<ChatInput {...defaultProps} prefillHint />)
      expect(screen.getByText(/Plan pre-filled/)).toBeInTheDocument()
    })

    it('does not show prefill hint by default', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.queryByText(/Plan pre-filled/)).not.toBeInTheDocument()
    })
  })

  describe('file action buttons', () => {
    it('does not show attach/screenshot buttons by default', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.queryByTitle('Add files & options')).not.toBeInTheDocument()
      expect(screen.queryByText('Screenshot')).not.toBeInTheDocument()
    })

    it('shows attach button with onUploadFiles', () => {
      renderWithProviders(<ChatInput {...defaultProps} onUploadFiles={vi.fn()} />)
      expect(screen.getByTitle('Add files & options')).toBeInTheDocument()
    })

    it('shows Screenshot in the + menu on macOS with onScreenshot', () => {
      renderWithProviders(<ChatInput {...defaultProps} isMac onUploadFiles={vi.fn()} onScreenshot={vi.fn()} />)
      fireEvent.click(screen.getByTitle('Add files & options'))
      expect(screen.getByText('Screenshot')).toBeInTheDocument()
    })

    it('opening the menu and clicking "Upload file" triggers the hidden file input click', () => {
      const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click')
      renderWithProviders(<ChatInput {...defaultProps} onUploadFiles={vi.fn()} />)
      fireEvent.click(screen.getByTitle('Add files & options'))
      fireEvent.click(screen.getByText('Upload file'))
      expect(clickSpy).toHaveBeenCalled()
      clickSpy.mockRestore()
    })

    it('disables the + menu button when uploading', () => {
      renderWithProviders(<ChatInput {...defaultProps} isMac onUploadFiles={vi.fn()} onScreenshot={vi.fn()} uploading />)
      expect(screen.getByTitle('Add files & options')).toBeDisabled()
    })
  })

  describe('drag-to-resize handle', () => {
    it('initiates drag on pointerdown on handle', () => {
      renderWithProviders(<ChatInput {...defaultProps} value="test" />)
      const handle = screen.getByTitle(/Drag to resize/)
      fireEvent.pointerDown(handle, { clientX: 100, clientY: 200 })
      expect(document.body.style.cursor).toBe('row-resize')
      expect(document.body.style.userSelect).toBe('none')
      fireEvent.pointerUp(handle)
    })

    it('restores body styles if unmounted mid-drag', () => {
      const { unmount } = renderWithProviders(<ChatInput {...defaultProps} value="test" />)
      const handle = screen.getByTitle(/Drag to resize/)
      fireEvent.pointerDown(handle, { clientX: 100, clientY: 200 })
      expect(document.body.style.cursor).toBe('row-resize')
      // Unmount mid-drag with no pointerup — the teardown guard must restore the
      // global body styles (onEnd can't fire once the element is gone).
      unmount()
      expect(document.body.style.cursor).toBe('')
      expect(document.body.style.userSelect).toBe('')
    })

    it('resets height on double-click', () => {
      localStorage.setItem('mc-input-height', '300')
      renderWithProviders(<ChatInput {...defaultProps} value="test" />)
      const handle = screen.getByTitle(/Drag to resize/)
      fireEvent.doubleClick(handle)
      expect(localStorage.getItem('mc-input-height')).toBeNull()
    })
  })

  // applyHeight snaps an overflowing textarea to the bottom when the
  // caret is at the end, so typing past the ~6-line cap keeps the caret visible.
  // jsdom has no layout, so we stub the relevant props (and make height='0' zero
  // scrollTop, as a real browser does) and stub document.activeElement rather
  // than calling ta.focus() — real focus leaks into the autoFocusKey suite.
  describe('caret-follow scroll', () => {
    afterEach(() => {
      delete (document as unknown as { activeElement?: unknown }).activeElement
    })
    function setActive(el: Element | null) {
      Object.defineProperty(document, 'activeElement', { configurable: true, get: () => el })
    }

    function instrument(
      ta: HTMLTextAreaElement,
      opts: { initialScrollTop: number; scrollHeight?: number; clientHeight?: number },
    ) {
      let scrollTop = opts.initialScrollTop
      const scrollHeight = opts.scrollHeight ?? 400
      const clientHeight = opts.clientHeight ?? 140
      Object.defineProperty(ta, 'scrollTop', {
        configurable: true,
        get: () => scrollTop,
        set: v => { scrollTop = v },
      })
      Object.defineProperty(ta, 'scrollHeight', { configurable: true, get: () => scrollHeight })
      Object.defineProperty(ta, 'clientHeight', { configurable: true, get: () => clientHeight })
      let heightVal = ta.style.height
      Object.defineProperty(ta.style, 'height', {
        configurable: true,
        get: () => heightVal,
        set: v => { heightVal = v; if (v === '0') scrollTop = 0 }, // mirror browser: collapse zeroes scrollTop
      })
    }

    it('snaps to the bottom when typing at the end so the caret stays visible', () => {
      const value = 'a\nb\nc\nd\ne\nf\ng\nh'
      renderWithProviders(<ChatInput {...defaultProps} value={value} />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      ta.setSelectionRange(value.length, value.length) // caret at end (forward typing)
      setActive(ta)
      // Pre-measurement scrollTop is stale (e.g. the value-commit just reset it).
      instrument(ta, { initialScrollTop: 71, scrollHeight: 289 })
      fireEvent.input(ta)
      // Caret-at-end + overflowing ⇒ snap to bottom. Otherwise scrollTop would
      // be stuck at 71 and the caret line would sit below the fold.
      expect(ta.scrollTop).toBe(289)
    })

    it('preserves scroll position for a mid-text caret (no jump to bottom)', () => {
      const value = 'a\nb\nc\nd\ne\nf\ng\nh'
      renderWithProviders(<ChatInput {...defaultProps} value={value} />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      ta.setSelectionRange(2, 2) // caret mid-text, NOT at end
      setActive(ta)
      instrument(ta, { initialScrollTop: 120, scrollHeight: 289 })
      fireEvent.input(ta)
      // Mid-text edits must not yank the view to the bottom; keep the place.
      expect(ta.scrollTop).toBe(120)
    })

    it('does not snap when content fits within the cap (not overflowing)', () => {
      const value = 'a\nb'
      renderWithProviders(<ChatInput {...defaultProps} value={value} />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      ta.setSelectionRange(value.length, value.length)
      setActive(ta)
      // scrollHeight <= clientHeight ⇒ nothing to scroll.
      instrument(ta, { initialScrollTop: 0, scrollHeight: 100, clientHeight: 140 })
      fireEvent.input(ta)
      expect(ta.scrollTop).toBe(0)
    })
  })

  describe('drag-and-drop zone', () => {
    it('calls onDrop when files are dropped', () => {
      const onDrop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onDrop={onDrop} />)
      const ta = screen.getByLabelText('Message input')
      fireEvent.drop(ta)
      expect(onDrop).toHaveBeenCalledOnce()
    })

    it('forwards dragover on textarea to parent handler', () => {
      const onDragOver = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onDragOver={onDragOver} />)
      fireEvent.dragOver(screen.getByLabelText('Message input'))
      expect(onDragOver).toHaveBeenCalledOnce()
    })

    it('stops drop propagation so parent drop zone does not double-fire', () => {
      const childOnDrop = vi.fn()
      const parentOnDrop = vi.fn()
      renderWithProviders(
        // Intentionally-plain parent drop zone; only exists to assert the child
        // stops drop propagation. No a11y role needed for this test harness.
        // eslint-disable-next-line jsx-a11y/no-static-element-interactions
        <div onDrop={parentOnDrop}>
          <ChatInput {...defaultProps} onDrop={childOnDrop} />
        </div>
      )
      fireEvent.drop(screen.getByLabelText('Message input'))
      expect(childOnDrop).toHaveBeenCalledOnce()
      expect(parentOnDrop).not.toHaveBeenCalled()
    })
  })

  describe('file preview strip', () => {
    it('does not render strip when pendingFiles is empty', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={[]} />)
      expect(screen.queryByRole('img')).not.toBeInTheDocument()
    })

    it('renders thumbnails for pending images', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png', '/tmp/b.png']} />)
      const imgs = screen.getAllByRole('img')
      expect(imgs).toHaveLength(2)
    })

    it('calls onRemoveFile when ✕ clicked', () => {
      const onRemove = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} onRemoveFile={onRemove} />)
      fireEvent.click(screen.getByTitle('Remove'))
      expect(onRemove).toHaveBeenCalledWith('/tmp/a.png')
    })

    it('renders file chip for non-image files', () => {
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/code.ts']} />)
      expect(screen.getByText('code.ts')).toBeInTheDocument()
      expect(screen.queryByRole('img')).not.toBeInTheDocument()
    })

    it('dispatches lightbox event when thumbnail clicked', () => {
      const spy = vi.fn()
      window.addEventListener('lightbox', spy)
      renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      fireEvent.click(screen.getByRole('img'))
      expect(spy).toHaveBeenCalledOnce()
      window.removeEventListener('lightbox', spy)
    })

    it('increases wrapper minHeight when files are attached and manually sized', () => {
      localStorage.setItem('mc-input-height', '150')
      const { container } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      const wrapper = container.firstElementChild as HTMLElement
      // With files: minHeight should be INPUT_DRAG_MIN_H (93) + FILE_PREVIEW_H (81) = 174
      expect(wrapper.style.minHeight).toBe('174px')
    })

    it('uses base minHeight when no files attached and manually sized', () => {
      localStorage.setItem('mc-input-height', '150')
      const { container } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={[]} />)
      const wrapper = container.firstElementChild as HTMLElement
      // Without files: minHeight should be INPUT_DRAG_MIN_H (93)
      expect(wrapper.style.minHeight).toBe('93px')
    })

    it('wrapper uses flex-col layout for proper space distribution with file strip', () => {
      const { container } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      const wrapper = container.firstElementChild as HTMLElement
      expect(wrapper.className).toContain('flex-col')
    })

    it('grows wrapper height when files are added with manual sizing', () => {
      localStorage.setItem('mc-input-height', '200')
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={[]} />)
      const wrapper = screen.getByTestId('input-wrapper')
      expect(wrapper.style.height).toBe('200px')
      rerender(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      // 200 + FILE_PREVIEW_H (81) = 281
      expect(wrapper.style.height).toBe('281px')
    })

    it('shrinks wrapper height when files are removed with manual sizing', () => {
      localStorage.setItem('mc-input-height', '281')
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} pendingFiles={['/tmp/a.png']} />)
      rerender(<ChatInput {...defaultProps} pendingFiles={[]} />)
      const wrapper = screen.getByTestId('input-wrapper')
      // 281 - FILE_PREVIEW_H (81) = 200
      expect(wrapper.style.height).toBe('200px')
    })
  })

  describe('prompt history', () => {
    const sent = ['first', 'second', 'third']

    it('ArrowUp on empty input recalls newest message', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp' })
      expect(onChange).toHaveBeenLastCalledWith('third')
    })

    it('repeated ArrowUp walks from newest to oldest', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="second" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).toHaveBeenNthCalledWith(1, 'third')
      expect(onChange).toHaveBeenNthCalledWith(2, 'second')
      expect(onChange).toHaveBeenNthCalledWith(3, 'first')
    })

    it('ArrowUp at oldest stays on oldest (does not wrap)', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="second" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="first" />)
      ta.setSelectionRange(0, 0)
      onChange.mockClear()
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ArrowDown past newest restores saved draft', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="my draft" />)
      const ta = screen.getByLabelText('Message input')
      // Caret at start so ArrowUp engages history
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).toHaveBeenLastCalledWith('third')
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      // Caret at end so ArrowDown engages history-exit
      ta.setSelectionRange('third'.length, 'third'.length)
      fireEvent.keyDown(ta, { key: 'ArrowDown' })
      expect(onChange).toHaveBeenLastCalledWith('my draft')
    })

    it('ArrowDown within history recalls the next newer message', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      // Go up three times to reach "first"
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="second" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="first" />)
      // Now go down one step — should recall "second"
      ta.setSelectionRange('first'.length, 'first'.length)
      fireEvent.keyDown(ta, { key: 'ArrowDown' })
      expect(onChange).toHaveBeenLastCalledWith('second')
    })

    it('ArrowUp is ignored when caret is mid-text and value non-empty', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="hello world" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      ta.setSelectionRange(5, 5)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ArrowDown is ignored when not in history mode', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowDown' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ArrowUp with no sent messages is a no-op', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={[]} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ignores ArrowUp with modifier keys (leaves native navigation)', () => {
      const onChange = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'ArrowUp', metaKey: true })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('ignores ArrowUp when slash command menu is open', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      // Typing "/" opens the slash menu via internal onChange
      fireEvent.change(ta, { target: { value: '/' } })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="/" />)
      onChange.mockClear()
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('editing a recalled message exits history mode so next ArrowUp starts from newest', () => {
      const onChange = vi.fn()
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      // Enter history, recall 'third'
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
      // User edits the recalled text — useEffect resets historyIdxRef to -1
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third edited" />)
      // Next ArrowUp starts from newest again, not from the stale index
      ta.setSelectionRange(0, 0)
      onChange.mockClear()
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      expect(onChange).toHaveBeenLastCalledWith('third')
    })

    it('ArrowDown in history mode is ignored when caret is not at end', () => {
      const onChange = vi.fn()
      const multiLine = ['first', 'line1\nline2', 'third']
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={multiLine} value="" />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={multiLine} value="third" />)
      ta.setSelectionRange(0, 0)
      fireEvent.keyDown(ta, { key: 'ArrowUp' })
      // JS expression so the value contains a real newline matching the array entry.
      // A string attribute like value="line1\nline2" would pass literal backslash-n
      // and mismatch the recalled message, exiting history mode before ArrowDown fires.
      rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={multiLine} value={'line1\nline2'} />)
      // Caret mid-text, not at end — native cursor movement preserved
      ta.setSelectionRange(3, 3)
      onChange.mockClear()
      fireEvent.keyDown(ta, { key: 'ArrowDown' })
      expect(onChange).not.toHaveBeenCalled()
    })

    it('moves caret to start after ArrowUp recall so repeated ↑ re-engages history', () => {
      const rafCbs: FrameRequestCallback[] = []
      const rafSpy = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { rafCbs.push(cb); return 0 })
      try {
        const onChange = vi.fn()
        const { rerender } = renderWithProviders(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="" />)
        const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
        fireEvent.keyDown(ta, { key: 'ArrowUp' })
        // Commit the recalled value before the deferred caret-move runs (real browser ordering).
        rerender(<ChatInput {...defaultProps} onChange={onChange} sentMessages={sent} value="third" />)
        rafCbs.forEach(cb => cb(0))
        expect(ta.selectionStart).toBe(0)
        expect(ta.selectionEnd).toBe(0)
      } finally {
        rafSpy.mockRestore()
      }
    })
  })

  // ── Reasoning effort merged into model button ──
  describe('reasoning effort button', () => {
    it('renders for acp provider', () => {
      const onClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="acp"
          reasoningEffort="high"
          onReasoningEffortClick={onClick}
          modelName="claude-opus-4.7"
          onModelClick={vi.fn()}
        />
      )
      expect(screen.getByText('High')).toBeInTheDocument()
    })

    it('renders Default label when effort is empty', () => {
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="acp"
          reasoningEffort=""
          onReasoningEffortClick={vi.fn()}
          modelName="claude-opus-4.7"
          onModelClick={vi.fn()}
        />
      )
      expect(screen.getByText('Default')).toBeInTheDocument()
    })

    it('shown when onReasoningEffortClick provided regardless of providerId', () => {
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="acp"
          reasoningEffort="high"
          onReasoningEffortClick={vi.fn()}
          modelName="claude-opus-4.7"
          onModelClick={vi.fn()}
        />
      )
      expect(screen.getByText('High')).toBeInTheDocument()
    })

    it('hidden when handler missing even on supported provider', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} providerId="acp" reasoningEffort="high" modelName="claude-opus-4.7" onModelClick={vi.fn()} />
      )
      expect(screen.queryByText('High')).not.toBeInTheDocument()
    })

    it('shown when providerId is undefined but callback provided', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} reasoningEffort="high" onReasoningEffortClick={vi.fn()} modelName="claude-opus-4.7" onModelClick={vi.fn()} />
      )
      expect(screen.getByText('High')).toBeInTheDocument()
    })

    it('disabled while running', () => {
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="acp"
          reasoningEffort="medium"
          onReasoningEffortClick={vi.fn()}
          modelName="claude-opus-4.7"
          onModelClick={vi.fn()}
          isRunning
        />
      )
      const btn = screen.getByTitle('Stop the current response to switch model')
      expect(btn).toBeDisabled()
    })

    it('invokes onModelClick with click rect', () => {
      const onModelClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps}
          providerId="acp"
          reasoningEffort="low"
          onReasoningEffortClick={vi.fn()}
          modelName="claude-opus-4.7"
          onModelClick={onModelClick}
        />
      )
      fireEvent.click(screen.getByTitle('Model: claude-opus-4.7'))
      expect(onModelClick).toHaveBeenCalledOnce()
      // First arg should be a DOMRect-like object
      expect(onModelClick.mock.calls[0][0]).toBeTruthy()
    })
  })

  describe('prompt optimizer (slot binding)', () => {
    // jsdom doesn't implement document.execCommand. Install a stub before each
    // test so we can spy on the call site that setTextUndoable uses to write
    // optimized text into the textarea.
    let originalExec: typeof document.execCommand | undefined
    beforeEach(() => {
      originalExec = (document as any).execCommand // eslint-disable-line @typescript-eslint/no-explicit-any
        ; (document as any).execCommand = () => true // eslint-disable-line @typescript-eslint/no-explicit-any
    })
    afterEach(() => {
      if (originalExec === undefined) delete (document as any).execCommand // eslint-disable-line @typescript-eslint/no-explicit-any
      else (document as any).execCommand = originalExec // eslint-disable-line @typescript-eslint/no-explicit-any
    })

    // A session switch is a change to the slot the ChatInput binds to
    // (useSlotId), which the SlotProvider supplies. Wrapping ChatInput in a
    // provider and re-rendering with a new slotId reproduces exactly what
    // ChatPage does on session switch — it flips the value Stage 1/Stage 2 key
    // on (`slotId`), not merely the `value` prop. `value` also changes because
    // ChatPage swaps in the target session's draft.
    const renderInSlot = (
      slotId: string,
      props: Record<string, unknown>,
    ) =>
      renderWithProviders(
        <SlotProvider slotId={slotId}>
          <ChatInput {...defaultProps} {...props} />
        </SlotProvider>,
      )
    const rerenderInSlot = (
      rerender: (ui: React.ReactElement) => void,
      slotId: string,
      props: Record<string, unknown>,
    ) =>
      rerender(
        <SlotProvider slotId={slotId}>
          <ChatInput {...defaultProps} {...props} />
        </SlotProvider>,
      )

    it('routes optimize result to the originating session when the user switched away', async () => {
      // The originating session (slot A) starts an optimize, then the user
      // navigates to slot B before it settles. The result must NOT be written
      // into the on-screen textarea (that would corrupt slot B's draft), and
      // must NOT be silently dropped — it goes to the parent via
      // onOptimizeResult tagged with slot A so ChatPage can route it into A's
      // draft.
      let resolveFetch: ((value: Response) => void) | null = null
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>(res => { resolveFetch = res })
      )
      // execCommand is the in-place write path; it must never fire for the
      // cross-session case (that would write into the on-screen session).
      const execSpy = vi.spyOn(document, 'execCommand').mockReturnValue(true)
      try {
        const onChange = vi.fn()
        const onOptimizeResult = vi.fn()
        const { rerender } = renderInSlot('slot-A', { value: 'fix bug', onChange, onOptimizeResult })
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        // Let the mutation start so variables.slotId is captured as 'slot-A'.
        await new Promise(r => setTimeout(r, 10))
        // Switch to slot B (new slotId + that session's draft as value).
        rerenderInSlot(rerender, 'slot-B', { value: 'review CR-123', onChange, onOptimizeResult })
        await new Promise(r => setTimeout(r, 10))
        execSpy.mockClear()
        resolveFetch!(new Response(
          JSON.stringify({ optimized: 'OPTIMIZED FIX BUG WITH MORE DETAIL', changed: true }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ))
        await new Promise(r => setTimeout(r, 50))
        // No in-place write on the now-displayed slot B.
        const insertCalls = execSpy.mock.calls.filter(c => c[0] === 'insertText')
        expect(insertCalls).toEqual([])
        // Result handed back to the parent, tagged with the ORIGINATING slot.
        expect(onOptimizeResult).toHaveBeenCalledWith('slot-A', 'OPTIMIZED FIX BUG WITH MORE DETAIL')
      } finally {
        execSpy.mockRestore()
        fetchSpy.mockRestore()
      }
    })

    it('routes original prompt to the originating session when fetch fails after a switch (onError path)', async () => {
      // Same cross-session hazard on the failure path: onError must not touch
      // the on-screen textarea, and must hand the ORIGINAL prompt back to the
      // originating session so the user's text isn't lost.
      let rejectFetch: ((reason?: Error) => void) | null = null
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>((_res, rej) => { rejectFetch = rej })
      )
      const execSpy = vi.spyOn(document, 'execCommand').mockReturnValue(true)
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => { })
      try {
        const onChange = vi.fn()
        const onOptimizeResult = vi.fn()
        const { rerender } = renderInSlot('slot-A', { value: 'fix bug', onChange, onOptimizeResult })
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        await new Promise(r => setTimeout(r, 10))
        rerenderInSlot(rerender, 'slot-B', { value: 'review CR-123', onChange, onOptimizeResult })
        await new Promise(r => setTimeout(r, 10))
        execSpy.mockClear()
        rejectFetch!(new Error('network'))
        await new Promise(r => setTimeout(r, 50))
        const insertCalls = execSpy.mock.calls.filter(c => c[0] === 'insertText')
        expect(insertCalls).toEqual([])
        expect(onOptimizeResult).toHaveBeenCalledWith('slot-A', 'fix bug')
      } finally {
        warnSpy.mockRestore()
        execSpy.mockRestore()
        fetchSpy.mockRestore()
      }
    })

    it('writes the result in place (not via onOptimizeResult) when the originating session is still on screen', async () => {
      // No switch: the optimize completes on the same slot it started on. The
      // result is written in place (execCommand insertText) and onOptimizeResult
      // is never invoked — the cross-session escape hatch stays dormant.
      let resolveFetch: ((value: Response) => void) | null = null
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>(res => { resolveFetch = res })
      )
      const execSpy = vi.spyOn(document, 'execCommand').mockReturnValue(true)
      try {
        const onChange = vi.fn()
        const onOptimizeResult = vi.fn()
        renderInSlot('slot-A', { value: 'fix bug', onChange, onOptimizeResult })
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        await new Promise(r => setTimeout(r, 10))
        execSpy.mockClear()
        resolveFetch!(new Response(
          JSON.stringify({ optimized: 'OPTIMIZED FIX BUG', changed: true }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ))
        await new Promise(r => setTimeout(r, 50))
        const insertCalls = execSpy.mock.calls.filter(c => c[0] === 'insertText')
        expect(insertCalls.length).toBe(1)
        expect(insertCalls[0][2]).toBe('OPTIMIZED FIX BUG')
        expect(onOptimizeResult).not.toHaveBeenCalled()
      } finally {
        execSpy.mockRestore()
        fetchSpy.mockRestore()
      }
    })

    it('hides the optimizing overlay after switching to another session, and restores it on return', async () => {
      // The overlay must be scoped to the originating session. It shows
      // on slot A while optimizing, disappears when the user navigates to slot
      // B (even though the request is still in flight), and reappears if they
      // switch back to A before it settles.
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>(() => { /* never resolves — stays pending */ })
      )
      try {
        const onChange = vi.fn()
        const { rerender } = renderInSlot('slot-A', { value: 'fix bug', onChange })
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        await new Promise(r => setTimeout(r, 10))
        // Overlay present on the originating session.
        expect(screen.getByText(/Optimizing prompt/)).toBeInTheDocument()
        // Switch away → overlay gone even though the request is still pending.
        rerenderInSlot(rerender, 'slot-B', { value: 'review CR-123', onChange })
        await new Promise(r => setTimeout(r, 10))
        expect(screen.queryByText(/Optimizing prompt/)).not.toBeInTheDocument()
        // Switch back → overlay returns (request still in flight).
        rerenderInSlot(rerender, 'slot-A', { value: 'fix bug', onChange })
        await new Promise(r => setTimeout(r, 10))
        expect(screen.getByText(/Optimizing prompt/)).toBeInTheDocument()
      } finally {
        fetchSpy.mockRestore()
      }
    })

    it('disables the Optimize button on another session while an optimize is in flight', async () => {
      // A single mutation backs the instance, so only one optimize runs at a
      // time. The button must READ as busy (disabled) on the session the user
      // navigated to, matching the re-entrancy guard — not look clickable then
      // silently no-op. The originating session shows the spinner; the other
      // session shows a disabled Sparkles button with an explanatory label.
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>(() => { /* never resolves — stays pending */ })
      )
      try {
        const onChange = vi.fn()
        const { rerender } = renderInSlot('slot-A', { value: 'fix bug', onChange })
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        await new Promise(r => setTimeout(r, 10))
        // Navigate to slot B — its own draft, its own value, request still pending.
        rerenderInSlot(rerender, 'slot-B', { value: 'review CR-123', onChange })
        await new Promise(r => setTimeout(r, 10))
        // Button is present under a busy-aware label and is disabled.
        const btn = screen.getByRole('button', { name: /busy optimizing another chat/i })
        expect(btn).toBeDisabled()
        expect(btn).toHaveAttribute('title', 'Optimizing another chat — please wait')
      } finally {
        fetchSpy.mockRestore()
      }
    })

    it('collapses a streaming optimize into a single undo boundary', async () => {
      // The recording effect skips writes while the optimizer owns the textarea,
      // and the completion effect records one boundary when it finishes — so even
      // if runOptimize ever wrote its result incrementally, a single Ctrl+Z
      // reverses the whole optimize rather than peeling off streamed chunks.
      let resolveFetch: ((value: Response) => void) | null = null
      const fetchSpy = vi.spyOn(global, 'fetch').mockImplementation(() =>
        new Promise<Response>(res => { resolveFetch = res }),
      )
      try {
        const onChange = vi.fn()
        const { rerender } = renderWithProviders(
          <ChatInput {...defaultProps} value="fix bug" onChange={onChange} />,
        )
        fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
        await new Promise(r => setTimeout(r, 10)) // mutation starts → optimizing = true
        // Two streamed chunks arrive as prop updates while optimizing. Each must
        // be skipped by the recording effect (not recorded as its own boundary).
        rerender(<ChatInput {...defaultProps} value="fix bug WITH" onChange={onChange} />)
        rerender(<ChatInput {...defaultProps} value="fix bug WITH MORE DETAIL" onChange={onChange} />)
        // Completing the optimize flips optimizing → false; the completion effect
        // records the final value as a single boundary. (The mid-flight value
        // diverged from the prompt, so onSuccess drops its own write — irrelevant
        // here; we only need the optimizing transition.)
        resolveFetch!(new Response(
          JSON.stringify({ optimized: 'IGNORED', changed: true }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ))
        await new Promise(r => setTimeout(r, 50))
        fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'z', ctrlKey: true })
        // One undo lands on the pre-optimize text, not an intermediate chunk —
        // proving the streamed writes collapsed into a single boundary.
        expect(onChange).toHaveBeenLastCalledWith('fix bug')
      } finally {
        fetchSpy.mockRestore()
      }
    })
  })

  describe('autoFocusKey', () => {
    it('focuses textarea on first non-null key', () => {
      renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      expect(screen.getByLabelText('Message input')).toHaveFocus()
    })

    it('focuses textarea when key changes', () => {
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" />)
      expect(ta).toHaveFocus()
    })

    it('does not re-focus on a re-render with the same key', () => {
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" />)
      expect(ta).not.toHaveFocus()
    })

    it('does not focus on a touch device, even when the key changes', () => {
      // Tapping a session on a phone/tablet must not pop the soft keyboard.
      touchEnv.touch = true
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" />)
      expect(ta).not.toHaveFocus()
    })

    it('does not re-focus when disabled flips false on the same key (e.g. AI finishes responding)', () => {
      // Once a key has been focused, subsequent disabled flips for the SAME key
      // must not steal focus back from a user who is reading or scrolling.
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" />)
      const ta = screen.getByLabelText('Message input')
      expect(ta).toHaveFocus()
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" disabled />)
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="A" disabled={false} />)
      expect(ta).not.toHaveFocus()
    })

    it('defers focus when key changes while disabled, then applies it once disabled clears', () => {
      // User picks a different session while it is still stopping (disabled=true).
      // Focus must NOT be lost — it should land once disabled flips false.
      const { rerender } = renderWithProviders(<ChatInput {...defaultProps} autoFocusKey="A" disabled />)
      const ta = screen.getByLabelText('Message input')
      ta.blur()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" disabled />)
      expect(ta).not.toHaveFocus()
      rerender(<ChatInput {...defaultProps} autoFocusKey="B" disabled={false} />)
      expect(ta).toHaveFocus()
    })

    it('does not steal focus from another input element', () => {
      const { rerender } = renderWithProviders(
        <>
          <input data-testid="other" aria-label="Other input" />
          <ChatInput {...defaultProps} autoFocusKey="A" />
        </>,
      )
      const other = screen.getByTestId('other')
      other.focus()
      expect(other).toHaveFocus()
      rerender(
        <>
          <input data-testid="other" aria-label="Other input" />
          <ChatInput {...defaultProps} autoFocusKey="B" />
        </>,
      )
      expect(other).toHaveFocus()
    })
  })

  describe('Quick Send', () => {
    it('passes quickSend to FollowUpBar when options present', () => {
      renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['A', 'B']} followUpPicked={new Set()} onFollowUpSelect={vi.fn()} quickSend={true} />)
      expect(screen.getAllByTitle(/Click to send instantly/).length).toBeGreaterThan(0)
    })

    it('fires onFollowUpSelect with MouseEvent on option click', () => {
      const onSelect = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} followUpOptions={['Go']} followUpPicked={new Set()} onFollowUpSelect={onSelect} quickSend={true} />)
      fireEvent.click(screen.getByRole('button', { name: 'Go' }))
      expect(onSelect).toHaveBeenCalledWith('Go', expect.any(Object))
    })
  })

  describe('stop button', () => {
    it('shows armed Stop button while running, click calls onStop', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning onStop={onStop} />)
      const btn = screen.getByTestId('stop-button-armed')
      expect(btn).toHaveAttribute('aria-label', 'Stop generation')
      fireEvent.click(btn)
      expect(onStop).toHaveBeenCalled()
    })

    it('shows pulsing force-kill button when soft_pending, click calls onStop', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning onStop={onStop} stopState="soft_pending" />)
      const btn = screen.getByTestId('stop-button-pulsing')
      expect(btn).toHaveAttribute('aria-label', 'Force kill session (discards in-progress work and queued messages)')
      fireEvent.click(btn)
      expect(onStop).toHaveBeenCalled()
    })

    it('keeps showing the stop affordance when soft_pending even after isRunning flips false', () => {
      // Regression: the chat_done event clears isRunning before the backend
      // flips stop_state back to idle. The button must NOT revert to Send mid-stop.
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning={false} onStop={onStop} stopState="soft_pending" />)
      expect(screen.getByTestId('stop-button-pulsing')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Send' })).not.toBeInTheDocument()
    })

    it('shows disabled killing spinner when stopState is killing (after isRunning false)', () => {
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} isRunning={false} onStop={onStop} stopState="killing" />)
      const btn = screen.getByRole('button', { name: 'Killing session' })
      expect(btn).toBeDisabled()
      fireEvent.click(btn)
      expect(onStop).not.toHaveBeenCalled()
    })

    it('shows Queue message button (calls onSend) when running with pending text', () => {
      const onSend = vi.fn()
      const onStop = vi.fn()
      renderWithProviders(<ChatInput {...defaultProps} value="more" isRunning onStop={onStop} onSend={onSend} />)
      const btn = screen.getByRole('button', { name: 'Queue message' })
      fireEvent.click(btn)
      expect(onSend).toHaveBeenCalled()
      expect(onStop).not.toHaveBeenCalled()
    })
  })

  describe('a staged session reference does not arm the mid-turn button', () => {
    // Regression guard for a dead click this feature briefly introduced. A
    // staged reference correctly enables the IDLE send button, but the mid-turn
    // split button must stay out of it: its steer mode refuses a payload of refs
    // alone, so enabling it produced an enabled primary button whose press did
    // nothing. Before session refs existed, an empty composer mid-turn rendered
    // the stop button — that is the behaviour to preserve.
    const refProps = (overrides: Record<string, unknown> = {}) => ({
      ...defaultProps,
      value: '',
      pendingSessions: [{ key: 'chat-9', title: 'Release notes', messages: 12 }],
      isRunning: true,
      canSteer: true,
      onStop: vi.fn(),
      onSend: vi.fn(),
      onSteer: vi.fn(),
      ...overrides,
    })

    it('renders the stop button, not the split send button, with only a ref staged', () => {
      renderWithProviders(<ChatInput {...refProps()} />)
      expect(screen.queryByTestId('busy-send-button')).not.toBeInTheDocument()
      expect(screen.queryByTestId('busy-send-caret')).not.toBeInTheDocument()
    })

    it('still shows the chip so the reference is visibly staged, not lost', () => {
      renderWithProviders(<ChatInput {...refProps()} />)
      expect(screen.getByTestId('session-ref-chip')).toHaveAttribute('data-session-ref', 'chat-9')
    })

    it('arms the mid-turn button again as soon as there is real text to steer', () => {
      renderWithProviders(<ChatInput {...refProps({ value: 'also look at this' })} />)
      expect(screen.getByTestId('busy-send-button')).toBeInTheDocument()
    })

    it('does enable the IDLE send button with only a ref staged', () => {
      const p = refProps({ isRunning: false, canSteer: false })
      renderWithProviders(<ChatInput {...p} />)
      const send = screen.getByLabelText('Send')
      expect(send).not.toBeDisabled()
      fireEvent.click(send)
      expect(p.onSend).toHaveBeenCalledTimes(1)
    })
  })

  describe('split send button while running (steer default)', () => {
    const runningProps = () => ({
      ...defaultProps,
      value: 'more',
      isRunning: true,
      canSteer: true,
      onStop: vi.fn(),
      onSend: vi.fn(),
      onSteer: vi.fn(),
    })

    it('renders Steer as the default main action with a dropdown caret', () => {
      renderWithProviders(<ChatInput {...runningProps()} />)
      expect(screen.getByTestId('busy-send-button')).toHaveAttribute('aria-label', 'Steer')
      expect(screen.getByTestId('busy-send-caret')).toBeInTheDocument()
    })

    it('main button fires onSteer (not onSend) in steer mode', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('busy-send-button'))
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('Enter steers while running in steer mode', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSteer).toHaveBeenCalledTimes(1)
      expect(p.onSend).not.toHaveBeenCalled()
    })

    it('dropdown switches to Queue — main button and Enter then queue, choice persists', () => {
      const p = runningProps()
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('busy-send-caret'))
      fireEvent.click(screen.getByTestId('busy-send-mode-queue'))
      // The test store has no active slot, so the write lands on the slot-less
      // sentinel key; the unscoped legacy key is a read-only migration source.
      expect(localStorage.getItem('mc-busy-send-mode:no-slot')).toBe('queue')
      const main = screen.getByTestId('busy-send-button')
      expect(main).toHaveAttribute('aria-label', 'Queue message')
      fireEvent.click(main)
      expect(p.onSend).toHaveBeenCalledTimes(1)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSend).toHaveBeenCalledTimes(2)
      expect(p.onSteer).not.toHaveBeenCalled()
    })

    it('restores persisted queue mode from localStorage', () => {
      safeSetItem('mc-busy-send-mode', 'queue')
      renderWithProviders(<ChatInput {...runningProps()} />)
      expect(screen.getByTestId('busy-send-button')).toHaveAttribute('aria-label', 'Queue message')
    })

    it('dropdown is keyboard operable: focus on open, arrows roam, Escape returns to caret', async () => {
      renderWithProviders(<ChatInput {...runningProps()} />)
      fireEvent.click(screen.getByTestId('busy-send-caret'))
      // useListboxKeyboard focuses the first option on open (setTimeout 0)
      await waitFor(() => expect(screen.getByTestId('busy-send-mode-steer')).toHaveFocus())
      const menu = screen.getByRole('menu')
      fireEvent.keyDown(menu, { key: 'ArrowDown' })
      expect(screen.getByTestId('busy-send-mode-queue')).toHaveFocus()
      fireEvent.keyDown(menu, { key: 'ArrowUp' })
      expect(screen.getByTestId('busy-send-mode-steer')).toHaveFocus()
      fireEvent.keyDown(menu, { key: 'Escape' })
      expect(screen.queryByRole('menu')).not.toBeInTheDocument()
      expect(screen.getByTestId('busy-send-caret')).toHaveFocus()
    })

    it('shows split button with only pending files (image-only steer)', () => {
      const p = { ...runningProps(), value: '', pendingFiles: ['/tmp/shot.png'] }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.click(screen.getByTestId('busy-send-button'))
      expect(p.onSteer).toHaveBeenCalledTimes(1)
    })

    it('Enter falls back to queue while stopping (soft_pending) even in steer mode', () => {
      const p = { ...runningProps(), stopState: 'soft_pending' as const }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
    })

    it('Enter sends normally when not running even if onSteer is provided', () => {
      const p = { ...runningProps(), isRunning: false, onStop: undefined }
      renderWithProviders(<ChatInput {...p} />)
      fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
      expect(p.onSend).toHaveBeenCalledTimes(1)
      expect(p.onSteer).not.toHaveBeenCalled()
    })
  })

  describe('global "/" focus shortcut', () => {
    it('focuses textarea when "/" is pressed outside any input', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      const ta = screen.getByLabelText('Message input')
      ;(ta as HTMLElement).blur()
      fireEvent.keyDown(document, { key: '/' })
      expect(document.activeElement).toBe(ta)
    })

    it('does not intercept "/" when already typing in the textarea', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      const ta = screen.getByLabelText('Message input')
      ta.focus()
      fireEvent.keyDown(ta, { key: '/' })
      expect(document.activeElement).toBe(ta)
    })
  })

  describe('Project chip', () => {
    it('does NOT render the project chip when onProjectClick is undefined', () => {
      renderWithProviders(<ChatInput {...defaultProps} />)
      expect(screen.queryByLabelText(/Project:/)).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Select project')).not.toBeInTheDocument()
    })

    it('renders "Project" label when project prop is empty', () => {
      renderWithProviders(<ChatInput {...defaultProps} onProjectClick={vi.fn()} />)
      const btn = screen.getByLabelText('Select project')
      expect(btn).toBeInTheDocument()
      expect(btn.getAttribute('title')).toBe('Select project')
      expect(btn.textContent).toContain('Project')
    })

    it('renders the project basename as label when project is set', () => {
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={vi.fn()} project="/home/u/workplace/KiroCrew" />
      )
      const btn = screen.getByLabelText('Project: /home/u/workplace/KiroCrew')
      expect(btn).toBeInTheDocument()
      expect(btn.getAttribute('title')).toBe('Project: /home/u/workplace/KiroCrew')
      expect(btn.textContent).toContain('KiroCrew')
      expect(btn.textContent).not.toContain('/home/u/workplace')
    })

    it('falls back to full path label when project has no basename', () => {
      // project = "/" → split('/').filter(Boolean) = [] → pop() = undefined → falls back to project itself
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={vi.fn()} project="/" />
      )
      const btn = screen.getByLabelText('Project: /')
      expect(btn.textContent).toContain('/')
    })

    it('strips trailing slashes when computing the basename label', () => {
      // project = "/home/u/foo/" → filter(Boolean) drops empty trailing chunk → 'foo'
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={vi.fn()} project="/home/u/foo/" />
      )
      const btn = screen.getByLabelText('Project: /home/u/foo/')
      expect(btn.textContent).toContain('foo')
    })

    it('calls onProjectClick with the chip\'s bounding rect when clicked', () => {
      const onProjectClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={onProjectClick} project="/home/u/proj" />
      )
      const btn = screen.getByLabelText('Project: /home/u/proj')
      fireEvent.click(btn)
      expect(onProjectClick).toHaveBeenCalledTimes(1)
      const arg = onProjectClick.mock.calls[0][0]
      // jsdom returns a 0,0,0,0 rect but it MUST be a DOMRect-shaped object
      expect(arg).toBeDefined()
      expect(typeof arg.top).toBe('number')
      expect(typeof arg.left).toBe('number')
      expect(typeof arg.right).toBe('number')
      expect(typeof arg.bottom).toBe('number')
      expect(typeof arg.width).toBe('number')
      expect(typeof arg.height).toBe('number')
    })

    it('still calls onProjectClick when no project is set (chip in placeholder mode)', () => {
      const onProjectClick = vi.fn()
      renderWithProviders(
        <ChatInput {...defaultProps} onProjectClick={onProjectClick} />
      )
      fireEvent.click(screen.getByLabelText('Select project'))
      expect(onProjectClick).toHaveBeenCalledTimes(1)
    })
  })
})

// --- Prompt undo/redo ---
// A controlled harness so onChange -> value round-trips exactly like ChatPage,
// which is what makes the explicit undo history observable end-to-end.
function ControlledInput({ initial = '', extra = {} }: { initial?: string; extra?: Record<string, unknown> }) {
  const [v, setV] = React.useState(initial)
  return <ChatInput {...defaultProps} value={v} onChange={setV} {...extra} />
}

describe('ChatInput undo/redo', () => {
  const undo = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
  const redoShift = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true, shiftKey: true })

  it('restores text with Ctrl+Z after an accidental full erase', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'hello world' } })
    fireEvent.change(ta, { target: { value: '' } }) // select-all + delete
    expect(ta.value).toBe('')
    undo(ta)
    expect(ta.value).toBe('hello world')
  })

  it('supports Cmd+Z on macOS (metaKey)', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'draft text here' } })
    fireEvent.change(ta, { target: { value: '' } })
    fireEvent.keyDown(ta, { key: 'z', metaKey: true })
    expect(ta.value).toBe('draft text here')
  })

  it('Ctrl+Shift+Z redoes an undone change', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'redo me please' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('redo me please')
    redoShift(ta)
    expect(ta.value).toBe('')
  })

  it('Ctrl+Y redoes (Windows convention)', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'windows redo' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('windows redo')
    fireEvent.keyDown(ta, { key: 'y', ctrlKey: true })
    expect(ta.value).toBe('')
  })

  it('collapses a rapid typing burst into a single undo step', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    // Incremental keystrokes within the coalesce window merge into one entry.
    for (const v of ['a', 'ab', 'abc', 'abcd']) fireEvent.change(ta, { target: { value: v } })
    undo(ta)
    expect(ta.value).toBe('') // one undo clears the whole burst
  })

  it('is a no-op at the base of the history (nothing to undo)', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    undo(ta)
    expect(ta.value).toBe('')
  })

  it('does not undo while an IME composition is active', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'こんにちは' } })
    fireEvent.compositionStart(ta)
    fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
    expect(ta.value).toBe('こんにちは') // composition guard suppressed undo
  })

  it('discards the redo branch after a new edit following undo', () => {
    renderWithProviders(<ControlledInput />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'first version' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('first version')
    fireEvent.change(ta, { target: { value: 'different text' } }) // drops redo branch
    redoShift(ta)
    expect(ta.value).toBe('different text') // redo is now a no-op
  })

  it('reseeds across a deferred slot draft restore (no cross-slot bleed)', () => {
    const drafts: Record<string, string> = { 'slot-a': 'slot A draft', 'slot-b': '' }
    function Lagged() {
      const [afk, setAfk] = React.useState('slot-a')
      const [v, setV] = React.useState(drafts['slot-a'])
      // Mimic ChatPage: activeSlot changes first; the draft is restored in a
      // SEPARATE commit by an [activeSlot]-keyed effect — so `value` lags
      // `autoFocusKey` by one commit.
      React.useEffect(() => { setV(drafts[afk]) }, [afk])
      return (
        <>
          <button onClick={() => setAfk('slot-b')}>switch</button>
          <ChatInput {...defaultProps} value={v} onChange={setV} autoFocusKey={afk} />
        </>
      )
    }
    renderWithProviders(<Lagged />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toBe('slot A draft')
    fireEvent.click(screen.getByText('switch'))
    expect(ta.value).toBe('') // slot B's draft settled in the second commit
    undo(ta)
    expect(ta.value).toBe('') // MUST NOT restore slot A's draft
  })

  it('reseeds synchronously when the draft lands in the same commit as the switch', () => {
    // if ChatPage ever restores the draft in the SAME commit as the
    // slot switch (no lag), the first keystroke must not be folded into the undo
    // base — Ctrl+Z must still reach the restored draft.
    const drafts: Record<string, string> = { 'slot-a': 'alpha draft', 'slot-b': 'beta draft' }
    function SyncSwitch() {
      const [afk, setAfk] = React.useState('slot-a')
      const [v, setV] = React.useState(drafts['slot-a'])
      // Both activeSlot and the draft change in one commit (synchronous restore).
      const go = () => { setAfk('slot-b'); setV(drafts['slot-b']) }
      return (
        <>
          <button onClick={go}>switch</button>
          <ChatInput {...defaultProps} value={v} onChange={setV} autoFocusKey={afk} />
        </>
      )
    }
    renderWithProviders(<SyncSwitch />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toBe('alpha draft')
    fireEvent.click(screen.getByText('switch'))
    expect(ta.value).toBe('beta draft')
    fireEvent.change(ta, { target: { value: 'beta draft!' } }) // user's first keystroke (real DOM edit)
    undo(ta)
    expect(ta.value).toBe('beta draft') // first edit is undoable back to the restored draft (no fold)
  })

  it('starts a new undo step after a pause longer than the coalesce window', () => {
    vi.useFakeTimers()
    try {
      renderWithProviders(<ControlledInput />)
      const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
      fireEvent.change(ta, { target: { value: 'aa' } })
      vi.advanceTimersByTime(450) // past UNDO_COALESCE_MS (400)
      fireEvent.change(ta, { target: { value: 'aabb' } })
      undo(ta)
      expect(ta.value).toBe('aa') // the pause made 'aabb' its own undo step
    } finally {
      vi.useRealTimers()
    }
  })
})

// --- Undo restores deleted/expanded paste content ---
// A `[ Paste #N ]` token in the text is just a pointer; its content lives in a
// separate PasteBlock the parent owns. Deleting or expanding a token drops the
// block, so the undo snapshot must carry the blocks to make the paste
// recoverable — restoring only the text would resurrect a dead token literal.
describe('ChatInput undo/redo: paste content', () => {
  const undo = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true })
  const redoShift = (ta: HTMLElement) => fireEvent.keyDown(ta, { key: 'z', ctrlKey: true, shiftKey: true })

  // Harness mirroring ChatPage: value and pasteBlocks are separate controlled
  // state, wired to the two callbacks the way the real parent wires them.
  function PasteHarness({ initial, initialBlocks }: { initial: string; initialBlocks: PasteBlock[] }) {
    const [v, setV] = React.useState(initial)
    const [blocks, setBlocks] = React.useState<PasteBlock[]>(initialBlocks)
    return (
      <ChatInput
        {...defaultProps}
        value={v}
        onChange={setV}
        pasteBlocks={blocks}
        onPasteBlocksChange={setBlocks}
      />
    )
  }

  const block: PasteBlock = { id: 'p1', seq: 1, lines: 40, content: 'TRACEBACK: boom\n...40 lines...' }
  const token = '[ Paste #1 · 40 lines ]'

  it('recovers the paste content after a Backspace-delete of the token', () => {
    // Caret just past the token; Backspace removes the token AND its block.
    renderWithProviders(<PasteHarness initial={`${token}`} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(token.length, token.length)
    fireEvent.keyDown(ta, { key: 'Backspace' })
    expect(ta.value).toBe('') // token gone

    undo(ta)
    expect(ta.value).toBe(token) // token text back...
    // ...and its content is recoverable: copy of a full selection expands it.
    ta.setSelectionRange(0, ta.value.length)
    const clip = { data: '' as string, setData: (_t: string, d: string) => { clip.data = d } }
    fireEvent.copy(ta, { clipboardData: { setData: clip.setData } })
    expect(clip.data).toBe(block.content) // block was restored, not a dead literal
  })

  it('recovers the paste after expanding the token, then undo re-collapses it', () => {
    // Double-click expands the token to full content and drops the block.
    renderWithProviders(<PasteHarness initial={token} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(2, 2) // caret inside the token
    fireEvent.click(ta, { detail: 2 })
    expect(ta.value).toBe(block.content) // expanded inline

    undo(ta)
    expect(ta.value).toBe(token) // collapsed token restored
    ta.setSelectionRange(0, ta.value.length)
    const clip = { data: '' as string, setData: (_t: string, d: string) => { clip.data = d } }
    fireEvent.copy(ta, { clipboardData: { setData: clip.setData } })
    expect(clip.data).toBe(block.content)
  })

  it('redo re-drops the block so an undone delete can be redone', () => {
    renderWithProviders(<PasteHarness initial={token} initialBlocks={[block]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.setSelectionRange(token.length, token.length)
    fireEvent.keyDown(ta, { key: 'Backspace' })
    expect(ta.value).toBe('')
    undo(ta)
    expect(ta.value).toBe(token)
    redoShift(ta)
    expect(ta.value).toBe('') // deletion reinstated
  })

  it('leaves plain-text undo untouched when no pastes exist', () => {
    renderWithProviders(<PasteHarness initial="" initialBlocks={[]} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(ta, { target: { value: 'hello world' } })
    fireEvent.change(ta, { target: { value: '' } })
    undo(ta)
    expect(ta.value).toBe('hello world')
  })
})
