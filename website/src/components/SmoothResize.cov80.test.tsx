import { render } from '@testing-library/react'
import { SmoothResize } from './SmoothResize'

/** happy-dom has no layout engine, so offsetHeight is 0 and ResizeObserver may
 *  be absent — stub both so the effect's measure/observe path is exercised. */
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = []
  disconnect = vi.fn()
  observe = vi.fn()
  unobserve = vi.fn()
  constructor(public cb: ResizeObserverCallback) {
    FakeResizeObserver.instances.push(this)
  }
}

describe('SmoothResize', () => {
  const original = globalThis.ResizeObserver
  let heightSpy: PropertyDescriptor | undefined

  beforeEach(() => {
    FakeResizeObserver.instances = []
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver
    heightSpy = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
      configurable: true,
      get: () => 137,
    })
  })

  afterEach(() => {
    globalThis.ResizeObserver = original
    if (heightSpy) Object.defineProperty(HTMLElement.prototype, 'offsetHeight', heightSpy)
  })

  it('while enabled it clips, transitions, and drives height from the content', () => {
    const { container } = render(
      <SmoothResize enabled><span>zzq-child</span></SmoothResize>,
    )
    const outer = container.firstElementChild as HTMLElement
    expect(outer.className).toBe('ft-resize')
    expect(outer.style.overflow).toBe('hidden')
    expect(outer.style.transition).toContain('height')
    expect(outer.style.height).toBe('137px')
    expect(FakeResizeObserver.instances).toHaveLength(1)
    expect(FakeResizeObserver.instances[0].observe).toHaveBeenCalledTimes(1)
  })

  it('re-measures when the observer fires', () => {
    const { container } = render(
      <SmoothResize enabled><span>zzq-child</span></SmoothResize>,
    )
    const outer = container.firstElementChild as HTMLElement
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
      configurable: true,
      get: () => 240,
    })
    FakeResizeObserver.instances[0].cb([], FakeResizeObserver.instances[0] as never)
    expect(outer.style.height).toBe('240px')
  })

  it('is inert when disabled: auto height, visible overflow, no observer', () => {
    const { container } = render(
      <SmoothResize enabled={false}><span>zzq-child</span></SmoothResize>,
    )
    const outer = container.firstElementChild as HTMLElement
    expect(outer.style.height).toBe('auto')
    expect(outer.style.overflow).toBe('visible')
    expect(outer.style.transition).toBe('none')
    expect(FakeResizeObserver.instances).toHaveLength(0)
  })

  it('flipping enabled off disconnects the observer and releases the height', () => {
    const { container, rerender } = render(
      <SmoothResize enabled><span>zzq-child</span></SmoothResize>,
    )
    const observer = FakeResizeObserver.instances[0]
    rerender(<SmoothResize enabled={false}><span>zzq-child</span></SmoothResize>)
    expect(observer.disconnect).toHaveBeenCalledTimes(1)
    expect((container.firstElementChild as HTMLElement).style.height).toBe('auto')
  })

  it('renders its children inside the inner measuring layer', () => {
    const { container, getByText } = render(
      <SmoothResize enabled><span>zzq-child</span></SmoothResize>,
    )
    const inner = container.firstElementChild!.firstElementChild as HTMLElement
    expect(inner.style.minWidth).toBe('100%')
    expect(inner.contains(getByText('zzq-child'))).toBe(true)
  })
})
