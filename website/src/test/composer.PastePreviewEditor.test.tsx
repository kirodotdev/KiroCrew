import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import PastePreviewEditor from '../composer/PastePreviewEditor'
import { FOCUSABLE } from '../hooks/useDialogFocusTrap'

/** Build a DOMRect-like anchor at a given top/bottom (only fields the editor reads). */
function rectAt(top: number, bottom: number, left = 100): DOMRect {
  return {
    top, bottom, left, right: left + 40, width: 40, height: bottom - top,
    x: left, y: top, toJSON: () => ({}),
  } as DOMRect
}

describe('PastePreviewEditor', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('renders nothing when closed', () => {
    render(<PastePreviewEditor open={false} anchorRect={rectAt(300, 320)} content="hi" lines={1} onSave={() => {}} onClose={() => {}} />)
    expect(screen.queryByTestId('paste-preview-editor')).toBeNull()
  })

  it('renders the content, the line count, and Save/Cancel when open', () => {
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content={'a\nb\nc'} lines={3} onSave={() => {}} onClose={() => {}} />)
    const ta = screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement
    expect(ta.value).toBe('a\nb\nc')
    expect(screen.getByTestId('paste-preview-editor-lines').textContent).toBe('3 lines')
    expect(screen.getByText('Save')).toBeTruthy()
    expect(screen.getByText('Cancel')).toBeTruthy()
  })

  it('edits and saves the new value', () => {
    const onSave = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="old" lines={1} onSave={onSave} onClose={() => {}} />)
    const ta = screen.getByTestId('paste-preview-editor-textarea')
    fireEvent.change(ta, { target: { value: 'new content' } })
    fireEvent.click(screen.getByText('Save'))
    expect(onSave).toHaveBeenCalledWith('new content')
  })

  it('reports every edit live through onChange, before any Save', () => {
    // The host writes these through to the pill as they happen, so the panel is
    // never the only holder of the text (a reload or Back mid-edit loses nothing).
    const onChange = vi.fn()
    const onSave = vi.fn()
    const onClose = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="old" lines={1} onChange={onChange} onSave={onSave} onClose={onClose} />)
    const ta = screen.getByTestId('paste-preview-editor-textarea')
    fireEvent.change(ta, { target: { value: 'o' } })
    fireEvent.change(ta, { target: { value: 'on' } })
    fireEvent.change(ta, { target: { value: 'one' } })
    expect(onChange.mock.calls.map(c => c[0])).toEqual(['o', 'on', 'one'])
    expect(onSave).not.toHaveBeenCalled()
    // Cancel after live edits is `onClose` — the host's cue to restore `content`.
    fireEvent.click(screen.getByText('Cancel'))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('closes on Cancel', () => {
    const onClose = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={onClose} />)
    fireEvent.click(screen.getByText('Cancel'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on Escape', () => {
    const onClose = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={onClose} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on Escape pressed inside the textarea, without letting it reach a document listener', () => {
    const onClose = vi.fn()
    const documentEscape = vi.fn()
    document.addEventListener('keydown', documentEscape)
    try {
      render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={onClose} />)
      fireEvent.keyDown(screen.getByTestId('paste-preview-editor-textarea'), { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)
      expect(documentEscape).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', documentEscape)
    }
  })

  it('does not close on an Escape the IME owns (candidate-list cancel), inside or outside the panel', () => {
    // Regression for the GPT review blocker: a capture-phase document handler
    // used to close on ANY Escape, so cancelling an IME candidate list threw
    // away the in-progress edit for every CJK user.
    const onClose = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={onClose} />)
    const ta = screen.getByTestId('paste-preview-editor-textarea')
    fireEvent.compositionStart(ta)
    fireEvent.keyDown(ta, { key: 'Escape', isComposing: true })
    fireEvent.keyDown(ta, { key: 'Escape', keyCode: 229 })
    fireEvent.keyDown(document, { key: 'Escape', isComposing: true })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('paste-preview-editor')).toBeTruthy()
  })

  it('does not let a shortcut chord typed inside reach a bubble-phase document or window listener', () => {
    // Same boundary Modal / ui/dialog draw: useKeyboardShortcuts binds
    // bubble-phase document keydown and the Ctrl+digit session jumps fire from
    // inside inputs — unguarded, Ctrl+3 in the textarea switched sessions and
    // unmounted the panel with the edit still in it.
    const documentShortcut = vi.fn()
    const windowShortcut = vi.fn()
    document.addEventListener('keydown', documentShortcut)
    window.addEventListener('keydown', windowShortcut)
    try {
      render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
      fireEvent.keyDown(screen.getByTestId('paste-preview-editor-textarea'), { key: '3', code: 'Digit3', ctrlKey: true })
      fireEvent.keyDown(screen.getByText('Save'), { key: ',', code: 'Comma', metaKey: true })
      expect(documentShortcut).not.toHaveBeenCalled()
      expect(windowShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', documentShortcut)
      window.removeEventListener('keydown', windowShortcut)
    }
  })

  it('with unsaved edits, the first Escape keeps the panel open and shows the hint; the second discards', () => {
    const onClose = vi.fn()
    const onSave = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="old" lines={1} onSave={onSave} onClose={onClose} />)
    const ta = screen.getByTestId('paste-preview-editor-textarea')
    fireEvent.change(ta, { target: { value: 'edited' } })
    fireEvent.keyDown(ta, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('paste-preview-editor-unsaved').textContent).toBe('Unsaved changes — press Esc again to discard, click away to keep')
    // Typing again withdraws the pending discard.
    fireEvent.change(ta, { target: { value: 'edited more' } })
    expect(screen.queryByTestId('paste-preview-editor-unsaved')).toBeNull()
    fireEvent.keyDown(ta, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    fireEvent.keyDown(ta, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onSave).not.toHaveBeenCalled()
  })

  it('a pointerdown outside with unsaved edits SAVES them instead of discarding', () => {
    // The UX review's top risk: trimming a long paste and clicking the composer
    // to keep typing destroyed the trim. The pointerdown runs before the click
    // it belongs to, so the edit is in the pill before a click that switches
    // session can tear the panel down.
    const onClose = vi.fn()
    const onSave = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="old" lines={1} onSave={onSave} onClose={onClose} />)
    fireEvent.change(screen.getByTestId('paste-preview-editor-textarea'), { target: { value: 'trimmed' } })
    fireEvent.pointerDown(document.body)
    expect(onSave).toHaveBeenCalledWith('trimmed')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('the header line count follows the edited text', () => {
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content={'a\nb\nc\nd\ne\nf'} lines={6} onSave={() => {}} onClose={() => {}} />)
    expect(screen.getByTestId('paste-preview-editor-lines').textContent).toBe('6 lines')
    fireEvent.change(screen.getByTestId('paste-preview-editor-textarea'), { target: { value: 'a\nb\nc' } })
    expect(screen.getByTestId('paste-preview-editor-lines').textContent).toBe('3 lines')
  })

  it('closes on a pointerdown outside the panel', () => {
    const onClose = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={onClose} />)
    fireEvent.pointerDown(document.body)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not close on a pointerdown inside the panel', () => {
    const onClose = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={onClose} />)
    fireEvent.pointerDown(screen.getByTestId('paste-preview-editor-textarea'))
    expect(onClose).not.toHaveBeenCalled()
  })

  it('positions ABOVE the anchor when there is room above', () => {
    // Anchor top at 400; panel measures 0 in the test DOM (offsetHeight) so the
    // DEFAULT height (220) applies: roomAbove 392 >= 226 -> above. Above is
    // height-independent: the panel's BOTTOM edge sits 6px over the anchor.
    vi.stubGlobal('innerHeight', 800)
    render(<PastePreviewEditor open anchorRect={rectAt(400, 420)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor') as HTMLElement
    expect(panel.style.bottom).toBe(`${800 - 400 + 6}px`)
    expect(panel.style.top).toBe('')
  })

  it('flips BELOW when there is not enough room above', () => {
    // Anchor top at 10 (< default panel height 220) -> below, top = bottom + 6.
    vi.stubGlobal('innerHeight', 800)
    render(<PastePreviewEditor open anchorRect={rectAt(10, 30)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor') as HTMLElement
    expect(panel.style.top).toBe(`${30 + 6}px`)
  })

  it('clamps the left edge into the viewport', () => {
    vi.stubGlobal('innerWidth', 500)
    // anchor.left far right (900): clamp to innerWidth - PANEL_WIDTH(420) - 8 = 72.
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320, 900)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor') as HTMLElement
    expect(panel.style.left).toBe('72px')
  })

  it('focuses the textarea on open', () => {
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    expect(document.activeElement).toBe(screen.getByTestId('paste-preview-editor-textarea'))
  })

  it('keeps Tab inside the panel: forward from the last control wraps to the first, Shift+Tab from the first wraps to the last', () => {
    render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor')
    const cancel = screen.getByText('Cancel')
    const grip = screen.getByTestId('paste-preview-editor-resize')
    // DOM order: Cancel, Save, textarea, resize grip — the grip is the last control.
    grip.focus()
    fireEvent.keyDown(grip, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(grip)
    expect(panel.contains(document.activeElement)).toBe(true)
  })

  it('a dirty panel survives the keyboard-only path Tab-out → Ctrl+digit: focus never leaves, so the chord never reaches the page', () => {
    // GPT review on #11100: without a trap, Tab from the last control parked
    // focus behind the panel; the next Ctrl+digit session jump then fired
    // un-isolated and unmounted the panel with the edit still in it — the
    // outside-click save only runs on a pointerdown. happy-dom does not move
    // focus on Tab itself, so the browser's default is modelled: an un-prevented
    // Tab from the LAST control (now the resize grip) lands on the next
    // focusable behind the panel.
    const documentShortcut = vi.fn()
    const onSave = vi.fn()
    const onClose = vi.fn()
    const behind = document.createElement('button')
    behind.textContent = 'behind the panel'
    document.body.appendChild(behind)
    document.addEventListener('keydown', documentShortcut)
    try {
      render(<PastePreviewEditor open anchorRect={rectAt(300, 320)} content="x" lines={1} onSave={onSave} onClose={onClose} />)
      const panel = screen.getByTestId('paste-preview-editor')
      const textarea = screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement
      const grip = screen.getByTestId('paste-preview-editor-resize')
      fireEvent.change(textarea, { target: { value: 'edited' } })
      grip.focus()
      const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
      grip.dispatchEvent(tab)
      if (!tab.defaultPrevented) behind.focus()
      expect(panel.contains(document.activeElement)).toBe(true)
      fireEvent.keyDown(document.activeElement!, { key: '3', code: 'Digit3', ctrlKey: true })
      expect(documentShortcut).not.toHaveBeenCalled()
      expect(onClose).not.toHaveBeenCalled()
      expect(screen.getByTestId('paste-preview-editor')).toBeInTheDocument()
      expect(textarea.value).toBe('edited')
    } finally {
      document.removeEventListener('keydown', documentShortcut)
      behind.remove()
    }
  })
})

describe('PastePreviewEditor resize handle', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('sits on the corner AWAY from the chip and drags outward to grow both axes (panel above)', () => {
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('innerWidth', 1200)
    render(<PastePreviewEditor open anchorRect={rectAt(600, 620)} content="a\nb" lines={2} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor') as HTMLElement
    const ta = screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement
    const handle = screen.getByTestId('paste-preview-editor-resize') as HTMLElement
    // Above → handle on the TOP-right corner; native textarea resize is off.
    expect(handle.className).toContain('top-0')
    expect(handle.className).toContain('cursor-nesw-resize')
    expect(ta.style.resize).toBe('none')
    expect(panel.style.width).toContain('420px')
    const h0 = parseFloat(ta.style.height)
    // Drag up-right: dx=+100, dy=-80 → wider by 100 and taller by 80.
    fireEvent.pointerDown(handle, { clientX: 500, clientY: 300, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: 600, clientY: 220, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: 600, clientY: 220, pointerId: 1 })
    expect(panel.style.width).toContain('520px')
    expect(parseFloat(ta.style.height)).toBe(h0 + 80)
    // Drag back down-left far past the minimums → clamped at 280 × 120.
    fireEvent.pointerDown(handle, { clientX: 600, clientY: 220, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: 0, clientY: 900, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: 0, clientY: 900, pointerId: 1 })
    expect(panel.style.width).toContain('280px')
    expect(parseFloat(ta.style.height)).toBe(120)
  })

  it('moves to the bottom-right corner when the panel opens below', () => {
    vi.stubGlobal('innerHeight', 800)
    render(<PastePreviewEditor open anchorRect={rectAt(10, 30)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const handle = screen.getByTestId('paste-preview-editor-resize') as HTMLElement
    expect(handle.className).toContain('bottom-0')
    expect(handle.className).toContain('cursor-nwse-resize')
  })

  it('reserves the grip\'s width on the header row when the panel opens above, so Save stays clickable', () => {
    // The 16px grip is absolutely positioned against the panel's padding box
    // at `top-0 right-0`; the `p-2` panel puts the header's right edge only 8px
    // in, so without extra padding the grip paints over the tail of `Save` and
    // its pointerdown (preventDefault + stopPropagation) eats the click. The
    // header must end 16px from the panel edge in the above case. jsdom has no
    // layout, so this pins the class contract.
    vi.stubGlobal('innerHeight', 800)
    const { unmount } = render(<PastePreviewEditor open anchorRect={rectAt(600, 620)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const above = screen.getByTestId('paste-preview-editor-header') as HTMLElement
    expect(screen.getByTestId('paste-preview-editor-resize').className).toContain('top-0')
    expect(above.className.split(/\s+/)).toContain('pr-2')
    unmount()
    // Below: the grip sits on the bottom edge, nowhere near the header — no reservation.
    render(<PastePreviewEditor open anchorRect={rectAt(10, 30)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const below = screen.getByTestId('paste-preview-editor-header') as HTMLElement
    expect(screen.getByTestId('paste-preview-editor-resize').className).toContain('bottom-0')
    expect(below.className.split(/\s+/)).not.toContain('pr-2')
  })

  it('clamps the left edge against the panel\'s ACTUAL width, so a widened panel stays inside a narrowed viewport (round-10 finding)', () => {
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('innerWidth', 1200)
    const { rerender } = render(<PastePreviewEditor open anchorRect={rectAt(600, 620, 500)} content="a\nb" lines={2} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor') as HTMLElement
    const handle = screen.getByTestId('paste-preview-editor-resize') as HTMLElement
    // Widen to 620px: the anchor at 500 still fits (500 + 620 + 8 <= 1200).
    fireEvent.pointerDown(handle, { clientX: 500, clientY: 300, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: 700, clientY: 300, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: 700, clientY: 300, pointerId: 1 })
    expect(panel.style.width).toContain('620px')
    expect(panel.style.left).toBe('500px')
    // The viewport narrows to 900 and the anchor re-reports (a resize re-anchors
    // the open panel). Clamped against 620, not the 420 default: 900 - 620 - 8 = 272.
    // (Against the default it would have stayed at 472 and put the right edge —
    // Save and the grip — at 1092, off a 900px viewport.)
    vi.stubGlobal('innerWidth', 900)
    rerender(<PastePreviewEditor open anchorRect={rectAt(600, 620, 501)} content="a\nb" lines={2} onSave={() => {}} onClose={() => {}} />)
    expect(panel.style.left).toBe('272px')
    expect(panel.style.width).toContain('620px')
  })

  it('during a shrink drag on a right-clamped panel the LEFT edge stays put, so the grip follows the pointer (round-11 finding)', () => {
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('innerWidth', 1000)
    // Anchor far right: left is right-clamped to 1000 - 420 - 8 = 572 and the
    // panel can only shrink (its width cap is the room to the viewport edge).
    render(<PastePreviewEditor open anchorRect={rectAt(600, 620, 900)} content="a\nb" lines={2} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor') as HTMLElement
    const handle = screen.getByTestId('paste-preview-editor-resize') as HTMLElement
    expect(panel.style.left).toBe('572px')
    fireEvent.pointerDown(handle, { clientX: 992, clientY: 300, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: 892, clientY: 300, pointerId: 1 })
    // Mid-gesture: narrower by 100, and the left edge has NOT moved right — the
    // right edge (where the grip is) is what tracked the pointer.
    expect(panel.style.width).toContain('320px')
    expect(panel.style.left).toBe('572px')
    fireEvent.pointerUp(handle, { clientX: 892, clientY: 300, pointerId: 1 })
    expect(panel.style.left).toBe('572px')
  })

  it('a new preview session (another pill while open) drops the previous drag: default width, auto-fit height (round-10 finding)', () => {
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('innerWidth', 1200)
    const { rerender } = render(<PastePreviewEditor open sessionKey="pill-a" anchorRect={rectAt(600, 620)} content={'a\nb'} lines={2} onSave={() => {}} onClose={() => {}} />)
    const panel = screen.getByTestId('paste-preview-editor') as HTMLElement
    const ta = screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement
    const handle = screen.getByTestId('paste-preview-editor-resize') as HTMLElement
    const autoFit2 = parseFloat(ta.style.height)
    fireEvent.pointerDown(handle, { clientX: 500, clientY: 300, pointerId: 1 })
    fireEvent.pointerMove(handle, { clientX: 600, clientY: 100, pointerId: 1 })
    fireEvent.pointerUp(handle, { clientX: 600, clientY: 100, pointerId: 1 })
    expect(panel.style.width).toContain('520px')
    expect(parseFloat(ta.style.height)).toBe(autoFit2 + 200)
    // Another pill is clicked while the panel stays open: `open` never flips, only
    // the session (and its anchor/content) changes.
    rerender(<PastePreviewEditor open sessionKey="pill-b" anchorRect={rectAt(500, 520)} content={'a\nb'} lines={2} onSave={() => {}} onClose={() => {}} />)
    expect(panel.style.width).toContain('420px')
    expect(parseFloat(ta.style.height)).toBe(autoFit2)
  })
})

describe('PastePreviewEditor resize grip: keyboard', () => {
  // Opus review on #11100: the grip carried only `onPointerDown` — no tab stop,
  // no key handler — so it fell outside `useDialogFocusTrap`'s FOCUSABLE
  // selector and the tab order, and the width it advertised to AT via
  // `resize_aria` could not be changed without a pointer. Same model as
  // `ResizeHandle`: 16px per arrow, 64px with Shift, the window-splitter
  // value attributes on the width axis.
  afterEach(() => { vi.restoreAllMocks() })

  function mountAbove() {
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('innerWidth', 1200)
    const onClose = vi.fn()
    render(<PastePreviewEditor open anchorRect={rectAt(600, 620)} content="a\nb" lines={2} onSave={() => {}} onClose={onClose} />)
    return {
      onClose,
      panel: screen.getByTestId('paste-preview-editor') as HTMLElement,
      ta: screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement,
      grip: screen.getByTestId('paste-preview-editor-resize') as HTMLElement,
    }
  }

  it('is in the tab order, matches the focus trap\'s FOCUSABLE selector, and exposes the width as a separator value', () => {
    const { grip } = mountAbove()
    expect(grip.tabIndex).toBe(0)
    expect(grip.matches(FOCUSABLE)).toBe(true)
    expect(grip.getAttribute('role')).toBe('separator')
    expect(grip.getAttribute('aria-label')).toBe('Resize preview')
    // Left/Right move the value, so per ARIA the splitter is `vertical`.
    expect(grip.getAttribute('aria-orientation')).toBe('vertical')
    expect(grip.getAttribute('aria-valuenow')).toBe('420')
    expect(grip.getAttribute('aria-valuemin')).toBe('280')
    // innerWidth 1200 - left 100 - 8px margin = the same cap the drag uses.
    expect(grip.getAttribute('aria-valuemax')).toBe('1092')
  })

  it('ArrowRight/ArrowLeft step the width by 16px (64px with Shift), clamped at the drag\'s min/max, and update aria-valuenow', () => {
    const { panel, grip } = mountAbove()
    expect(panel.style.width).toBe('420px')
    fireEvent.keyDown(grip, { key: 'ArrowRight' })
    expect(panel.style.width).toBe('436px')
    expect(grip.getAttribute('aria-valuenow')).toBe('436')
    fireEvent.keyDown(grip, { key: 'ArrowLeft' })
    expect(panel.style.width).toBe('420px')
    fireEvent.keyDown(grip, { key: 'ArrowRight', shiftKey: true })
    expect(panel.style.width).toBe('484px')
    // Max: 420 + 11 × 64 = 1188 would pass the 1092 cap → clamped exactly there.
    for (let i = 0; i < 11; i++) fireEvent.keyDown(grip, { key: 'ArrowRight', shiftKey: true })
    expect(panel.style.width).toBe('1092px')
    expect(grip.getAttribute('aria-valuenow')).toBe('1092')
    // Min: far below 280 → clamped exactly at 280, as the drag does.
    for (let i = 0; i < 20; i++) fireEvent.keyDown(grip, { key: 'ArrowLeft', shiftKey: true })
    expect(panel.style.width).toBe('280px')
    expect(grip.getAttribute('aria-valuenow')).toBe('280')
  })

  it('ArrowUp/ArrowDown step the textarea height with the drag\'s clamps; the arrow that moves the grip AWAY from the chip grows it (panel above)', () => {
    const { ta, grip } = mountAbove()
    // 2 lines auto-fit below the 120px floor → starts at the floor.
    expect(parseFloat(ta.style.height)).toBe(120)
    // Above: the grip is on the top edge, so Up grows (mirrors the drag's dy).
    fireEvent.keyDown(grip, { key: 'ArrowUp' })
    expect(parseFloat(ta.style.height)).toBe(136)
    fireEvent.keyDown(grip, { key: 'ArrowUp', shiftKey: true })
    expect(parseFloat(ta.style.height)).toBe(200)
    fireEvent.keyDown(grip, { key: 'ArrowDown' })
    expect(parseFloat(ta.style.height)).toBe(184)
    // Max: room above (600 - 14) minus the 52px chrome fallback = 534.
    for (let i = 0; i < 10; i++) fireEvent.keyDown(grip, { key: 'ArrowUp', shiftKey: true })
    expect(parseFloat(ta.style.height)).toBe(534)
    // Min: back past the floor → 120, never below.
    for (let i = 0; i < 10; i++) fireEvent.keyDown(grip, { key: 'ArrowDown', shiftKey: true })
    expect(parseFloat(ta.style.height)).toBe(120)
  })

  it('below the chip the vertical arrows flip with the grip: Down grows, Up shrinks', () => {
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('innerWidth', 1200)
    render(<PastePreviewEditor open anchorRect={rectAt(10, 30)} content="x" lines={1} onSave={() => {}} onClose={() => {}} />)
    const ta = screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement
    const grip = screen.getByTestId('paste-preview-editor-resize') as HTMLElement
    expect(grip.className).toContain('bottom-0')
    expect(parseFloat(ta.style.height)).toBe(120)
    fireEvent.keyDown(grip, { key: 'ArrowDown' })
    expect(parseFloat(ta.style.height)).toBe(136)
    fireEvent.keyDown(grip, { key: 'ArrowUp' })
    expect(parseFloat(ta.style.height)).toBe(120)
  })

  it('preventDefaults only the arrows it handles; other keys keep their default and are still isolated from the page', () => {
    const { grip } = mountAbove()
    const documentShortcut = vi.fn()
    document.addEventListener('keydown', documentShortcut)
    try {
      const right = new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true })
      grip.dispatchEvent(right)
      expect(right.defaultPrevented).toBe(true)
      const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
      grip.dispatchEvent(enter)
      expect(enter.defaultPrevented).toBe(false)
      // The panel's key boundary still stops both at the panel.
      expect(documentShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', documentShortcut)
    }
  })

  it('does not swallow Escape: on the focused grip it still runs the panel\'s two-step dismissal', () => {
    const { onClose, ta, grip } = mountAbove()
    const documentEscape = vi.fn()
    document.addEventListener('keydown', documentEscape)
    try {
      grip.focus()
      expect(document.activeElement).toBe(grip)
      // Clean → one Escape closes; it is handled at the panel, not the document.
      fireEvent.keyDown(grip, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)
      expect(documentEscape).not.toHaveBeenCalled()
      // Dirty → the first Escape on the grip arms the hint, the second discards.
      fireEvent.change(ta, { target: { value: 'edited' } })
      fireEvent.keyDown(grip, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(1)
      expect(screen.getByTestId('paste-preview-editor-unsaved')).toBeInTheDocument()
      fireEvent.keyDown(grip, { key: 'Escape' })
      expect(onClose).toHaveBeenCalledTimes(2)
    } finally {
      document.removeEventListener('keydown', documentEscape)
    }
  })

  it('a keyboard resize marks the panel user-sized, so a later re-measure keeps the chosen height (as a drag does)', () => {
    vi.stubGlobal('innerHeight', 800)
    vi.stubGlobal('innerWidth', 1200)
    const { rerender } = render(<PastePreviewEditor open anchorRect={rectAt(600, 620)} content="a\nb" lines={2} onSave={() => {}} onClose={() => {}} />)
    const ta = screen.getByTestId('paste-preview-editor-textarea') as HTMLTextAreaElement
    const grip = screen.getByTestId('paste-preview-editor-resize') as HTMLElement
    fireEvent.keyDown(grip, { key: 'ArrowUp', shiftKey: true })
    expect(parseFloat(ta.style.height)).toBe(184)
    // The anchor re-reports (a scroll, a viewport resize): auto-fit would put a
    // 2-line paste back at 120; user-sized keeps 184.
    rerender(<PastePreviewEditor open anchorRect={rectAt(601, 621)} content="a\nb" lines={2} onSave={() => {}} onClose={() => {}} />)
    expect(parseFloat(ta.style.height)).toBe(184)
  })
})
