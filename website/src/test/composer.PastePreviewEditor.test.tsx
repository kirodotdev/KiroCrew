import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import PastePreviewEditor from '../composer/PastePreviewEditor'

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
    expect(screen.getByTestId('paste-preview-editor-unsaved').textContent).toBe('Unsaved changes — press Esc again to discard')
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
    const textarea = screen.getByTestId('paste-preview-editor-textarea')
    // DOM order: Cancel, Save, textarea — the textarea is the last control.
    textarea.focus()
    fireEvent.keyDown(textarea, { key: 'Tab' })
    expect(document.activeElement).toBe(cancel)
    fireEvent.keyDown(cancel, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(textarea)
    expect(panel.contains(document.activeElement)).toBe(true)
  })

  it('a dirty panel survives the keyboard-only path Tab-out → Ctrl+digit: focus never leaves, so the chord never reaches the page', () => {
    // GPT review on #11100: without a trap, Tab from the last control parked
    // focus behind the panel; the next Ctrl+digit session jump then fired
    // un-isolated and unmounted the panel with the edit still in it — the
    // outside-click save only runs on a pointerdown. jsdom does not move focus
    // on Tab itself, so the browser's default is modelled: an un-prevented Tab
    // lands on the next focusable behind the panel.
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
      fireEvent.change(textarea, { target: { value: 'edited' } })
      textarea.focus()
      const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
      textarea.dispatchEvent(tab)
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
})
