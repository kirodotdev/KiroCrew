/**
 * The box composer on its own: asked to reveal itself (a drag elsewhere was
 * refused because this box holds typed text), it scrolls its panel until it is
 * on screen and takes focus.
 */
import { describe, it, expect, vi } from 'vitest'
import { useRef } from 'react'
import { render, screen } from '@testing-library/react'
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
