/**
 * The box composer on its own: asked to reveal itself (a drag elsewhere was
 * refused because this box holds typed text), it scrolls its panel until it is
 * on screen and takes focus.
 */
import { describe, it, expect, vi } from 'vitest'
import { useRef } from 'react'
import { act, render, screen, waitFor } from '@testing-library/react'
import { ImageCommentComposer } from '../components/ImageCommentComposer'

function Host({ reveal, scrollBy }: { reveal: number; scrollBy: (o: ScrollToOptions) => void }) {
  const scrollRef = useRef<HTMLDivElement | null>(null)
  return (
    <div ref={el => { if (el) el.scrollBy = scrollBy as never; scrollRef.current = el }}>
      <ImageCommentComposer x={100} y={100} onSubmit={() => {}} onCancel={() => {}} scrollRef={scrollRef} initialText="kept" reveal={reveal} />
    </div>
  )
}

describe('ImageCommentComposer', () => {
  it('scrolls itself back on screen and takes focus when asked to reveal', () => {
    const scrollBy = vi.fn()
    const { rerender } = render(<Host reveal={0} scrollBy={scrollBy} />)
    const input = screen.getByLabelText('Comment on the boxed area')
    input.blur()
    const root = input.closest('[data-image-comment-composer]') as HTMLElement
    root.getBoundingClientRect = () => ({ top: -300, bottom: -200, left: 0, right: 200, width: 200, height: 100, x: 0, y: -300, toJSON: () => ({}) }) as DOMRect
    expect(scrollBy).not.toHaveBeenCalled()
    rerender(<Host reveal={1} scrollBy={scrollBy} />)
    expect(scrollBy).toHaveBeenCalledTimes(1)
    expect(scrollBy.mock.calls[0][0].top).toBeLessThan(-300)
    expect(input).toHaveFocus()
  })

  it('does not scroll when it remounts after an earlier reveal, as on a tab coming back', () => {
    const scrollBy = vi.fn()
    const off = () => ({ top: -300, bottom: -200, left: 0, right: 200, width: 200, height: 100, x: 0, y: -300, toJSON: () => ({}) }) as DOMRect
    const spy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(off)
    render(<Host reveal={3} scrollBy={scrollBy} />)
    spy.mockRestore()
    expect(scrollBy).not.toHaveBeenCalled()
  })

  it('stays on its box when a window resize rescales the image under it', async () => {
    const rect = (left: number, top: number, width: number, height: number) => () => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) }) as DOMRect
    // The panel, and the box drawn in it, are on screen before the composer
    // opens, as in the file viewer.
    function ResizeHost({ open }: { open: boolean }) {
      const scrollRef = useRef<HTMLDivElement | null>(null)
      return (
        <div ref={scrollRef}>
          <span data-image-box-draft="" ref={el => { if (el) el.getBoundingClientRect = rect(100, 100, 200, 200) }} />
          {/* The drag ended at the box's bottom-middle. */}
          {open && <ImageCommentComposer x={200} y={300} onSubmit={() => {}} onCancel={() => {}} scrollRef={scrollRef} />}
        </div>
      )
    }
    const { container, rerender } = render(<ResizeHost open={false} />)
    rerender(<ResizeHost open />)
    const root = container.querySelector('[data-image-comment-composer]') as HTMLElement
    await waitFor(() => expect(root.style.top).toBe('312px'))
    // The window narrows: the image, and the box drawn on it, shrink and move.
    ;(container.querySelector('[data-image-box-draft]') as HTMLElement).getBoundingClientRect = rect(50, 80, 100, 100)
    act(() => { window.dispatchEvent(new Event('resize')) })
    await waitFor(() => expect(root.style.top).toBe('192px'))
    expect(root.style.left).toBe('100px')
  })

  it('takes focus without scrolling when it is already on screen', () => {
    const scrollBy = vi.fn()
    const { rerender } = render(<Host reveal={0} scrollBy={scrollBy} />)
    const input = screen.getByLabelText('Comment on the boxed area')
    input.blur()
    const root = input.closest('[data-image-comment-composer]') as HTMLElement
    root.getBoundingClientRect = () => ({ top: 100, bottom: 200, left: 0, right: 200, width: 200, height: 100, x: 0, y: 100, toJSON: () => ({}) }) as DOMRect
    rerender(<Host reveal={1} scrollBy={scrollBy} />)
    expect(scrollBy).not.toHaveBeenCalled()
    expect(input).toHaveFocus()
  })
})
