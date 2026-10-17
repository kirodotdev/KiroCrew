import { render, act } from '@testing-library/react'
import TypewriterText from './TypewriterText'

describe('TypewriterText', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('renders the initial text immediately, with class and title passthrough', () => {
    const { container } = render(
      <TypewriterText text="zzq-initial" className="zzq-cls" title="zzq-title" />,
    )
    const span = container.querySelector('span')!
    expect(span.textContent).toBe('zzq-initial')
    expect(span.className).toBe('zzq-cls')
    expect(span.getAttribute('title')).toBe('zzq-title')
  })

  it('snaps without animating when the previous value was not a slot key', () => {
    const { container, rerender } = render(<TypewriterText text="zzq-plain" />)
    rerender(<TypewriterText text="zzq-other" />)
    expect(container.querySelector('span')!.textContent).toBe('zzq-other')
  })

  it('snaps when the NEW value is also a slot key', () => {
    const { container, rerender } = render(<TypewriterText text="chat-1" />)
    rerender(<TypewriterText text="chat-2" />)
    expect(container.querySelector('span')!.textContent).toBe('chat-2')
  })

  it('types out character by character when a slot key becomes a real title', () => {
    const { container, rerender } = render(<TypewriterText text="chat-abc" speed={10} />)
    rerender(<TypewriterText text="Zzq" speed={10} />)

    const span = () => container.querySelector('span')!
    expect(span().textContent).toBe('')
    act(() => { vi.advanceTimersByTime(10) })
    expect(span().textContent).toBe('Z')
    act(() => { vi.advanceTimersByTime(10) })
    expect(span().textContent).toBe('Zz')
    act(() => { vi.advanceTimersByTime(10) })
    expect(span().textContent).toBe('Zzq')
    // Interval cleared at completion: further ticks change nothing.
    act(() => { vi.advanceTimersByTime(100) })
    expect(span().textContent).toBe('Zzq')
  })

  it('a second animating change restarts the run from empty', () => {
    const { container, rerender } = render(<TypewriterText text="chat-abc" speed={10} />)
    rerender(<TypewriterText text="Zzq" speed={10} />)
    act(() => { vi.advanceTimersByTime(10) })
    expect(container.querySelector('span')!.textContent).toBe('Z')

    // Back to a slot key (snaps), then out again (animates from scratch).
    rerender(<TypewriterText text="chat-def" speed={10} />)
    expect(container.querySelector('span')!.textContent).toBe('chat-def')
    rerender(<TypewriterText text="Qq" speed={10} />)
    expect(container.querySelector('span')!.textContent).toBe('')
    act(() => { vi.advanceTimersByTime(20) })
    expect(container.querySelector('span')!.textContent).toBe('Qq')
  })

  it('an identical text does not restart the animation', () => {
    const { container, rerender } = render(<TypewriterText text="zzq-same" />)
    rerender(<TypewriterText text="zzq-same" />)
    expect(container.querySelector('span')!.textContent).toBe('zzq-same')
  })

  it('forwards double-click to the parent handler', () => {
    const onDoubleClick = vi.fn()
    const { container } = render(<TypewriterText text="zzq-dbl" onDoubleClick={onDoubleClick} />)
    container.querySelector('span')!.dispatchEvent(
      new MouseEvent('dblclick', { bubbles: true }),
    )
    expect(onDoubleClick).toHaveBeenCalledTimes(1)
  })

  it('clears its interval on unmount mid-animation', () => {
    const clearInterval = vi.spyOn(window, 'clearInterval')
    const { rerender, unmount } = render(<TypewriterText text="chat-abc" speed={10} />)
    rerender(<TypewriterText text="Zzq" speed={10} />)
    unmount()
    expect(clearInterval).toHaveBeenCalled()
    clearInterval.mockRestore()
  })
})
