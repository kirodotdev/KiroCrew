/**
 * AppLoadingFallback — the delayed, app-shaped Suspense fallback.
 *
 * Unit level: the delay window, its cleanup, the reduced-motion fade switch,
 * each of the four layouts, and `resolveAppLoadingLayout` over the nearby inputs
 * a hand-edited `app.json` can hold. The AppHost wiring is pinned separately in
 * `AppHostLoading.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import AppLoadingFallback, {
  APP_LOADING_DELAY_MS,
  APP_LOADING_LAYOUTS,
  resolveAppLoadingLayout,
} from '../components/AppLoadingFallback'

function setReducedMotion(reduce: boolean) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: reduce && query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

const origMatchMedia = window.matchMedia

beforeEach(() => {
  vi.useFakeTimers()
  setReducedMotion(false)
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  Object.defineProperty(window, 'matchMedia', { configurable: true, writable: true, value: origMatchMedia })
})

describe('resolveAppLoadingLayout', () => {
  it.each(APP_LOADING_LAYOUTS)('accepts %s exactly', (layout) => {
    expect(resolveAppLoadingLayout({ layout })).toBe(layout)
  })

  it.each([
    ['missing (undefined)', undefined],
    ['null', null],
    ['a bare string', 'rail'],
    ['a number', 7],
    ['a boolean', true],
    ['an array', ['rail']],
    ['an array carrying a layout key', Object.assign(['x'], { layout: 'rail' })],
    ['an empty object', {}],
    ['layout: null', { layout: null }],
    ['a numeric layout', { layout: 1 }],
    ['an array layout', { layout: ['rail'] }],
    ['an object layout', { layout: { name: 'rail' } }],
    ['wrong case', { layout: 'Rail' }],
    ['upper case', { layout: 'CENTERED' }],
    ['leading space', { layout: ' split' }],
    ['trailing space', { layout: 'split ' }],
    ['empty string', { layout: '' }],
    ['an unknown name', { layout: 'grid' }],
    ['markup', { layout: '<div>' }],
    ['a prototype key', { layout: 'toString' }],
    ['a layout only on the prototype', Object.create({ layout: 'rail' })],
  ])('falls back to page for %s', (_label, value) => {
    expect(resolveAppLoadingLayout(value)).toBe('page')
  })
})

describe('AppLoadingFallback — delay', () => {
  it('renders nothing before the delay elapses', () => {
    render(<AppLoadingFallback appName="Ledger Lens" />)
    expect(screen.queryByTestId('app-loading-fallback')).toBeNull()
    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS - 1) })
    expect(screen.queryByTestId('app-loading-fallback')).toBeNull()
  })

  it('shows the skeleton once the delay has elapsed', () => {
    render(<AppLoadingFallback appName="Ledger Lens" />)
    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS) })
    const el = screen.getByTestId('app-loading-fallback')
    expect(el.getAttribute('role')).toBe('status')
    expect(el.textContent).toContain('Loading Ledger Lens')
  })

  it('a fallback unmounted inside the window never shows and leaves no timer behind', () => {
    const clearSpy = vi.spyOn(window, 'clearTimeout')
    const { unmount } = render(<AppLoadingFallback appName="Ledger Lens" />)
    expect(vi.getTimerCount()).toBe(1)
    unmount()
    expect(clearSpy).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
    // Advancing past the window after unmount must not throw a set-state-on-unmounted warning.
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    act(() => { vi.advanceTimersByTime(APP_LOADING_DELAY_MS * 2) })
    expect(errSpy).not.toHaveBeenCalled()
    errSpy.mockRestore()
    clearSpy.mockRestore()
  })

  it('delayMs 0 shows immediately, with no timer', () => {
    render(<AppLoadingFallback appName="Ledger Lens" delayMs={0} />)
    expect(screen.getByTestId('app-loading-fallback')).toBeTruthy()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('AppLoadingFallback — motion', () => {
  it('fades in when motion is allowed', () => {
    render(<AppLoadingFallback appName="X" delayMs={0} />)
    expect(screen.getByTestId('app-loading-fallback').style.animation).toContain('ft-fade')
  })

  it('does not animate under prefers-reduced-motion', () => {
    setReducedMotion(true)
    render(<AppLoadingFallback appName="X" delayMs={0} />)
    expect(screen.getByTestId('app-loading-fallback').style.animation).toBe('')
  })
})

describe('AppLoadingFallback — layouts', () => {
  function shown(loading: unknown) {
    render(<AppLoadingFallback appName="Ledger Lens" loading={loading} delayMs={0} />)
    return screen.getByTestId('app-loading-fallback')
  }

  it('page (the default) keeps the original skeleton: title, subtitle, six rows', () => {
    const el = shown(undefined)
    expect(el.dataset.layout).toBe('page')
    const shape = screen.getByTestId('app-loading-shape-page')
    const bars = shape.querySelectorAll(':scope > .skeleton')
    expect([...bars].map(b => b.className)).toEqual([
      'skeleton h-7 w-48 rounded mb-2',
      'skeleton h-4 w-72 rounded mb-6',
    ])
    // ContentSkeleton rows={6}: six icon+line rows.
    expect(shape.querySelectorAll('.space-y-2.mt-4 > div')).toHaveLength(6)
    expect(shape.querySelector('p')?.className).toBe('text-[12px] text-muted mt-4')
  })

  it('rail draws a 232px left column of list rows beside the content', () => {
    expect(shown({ layout: 'rail' }).dataset.layout).toBe('rail')
    const rail = screen.getByTestId('app-loading-rail')
    expect(rail.className).toContain('w-[232px]')
    // Narrow-first: the rail is only drawn from md up.
    expect(rail.className.split(' ')).toEqual(expect.arrayContaining(['hidden', 'md:block']))
    expect(rail.querySelectorAll('.flex.items-center')).toHaveLength(6)
    expect(screen.getByTestId('app-loading-shape-rail').textContent).toContain('Loading Ledger Lens')
  })

  it('centered draws one narrow card', () => {
    expect(shown({ layout: 'centered' }).dataset.layout).toBe('centered')
    expect(screen.getByTestId('app-loading-card').className).toContain('max-w-md')
    expect(screen.getAllByTestId('app-loading-card')).toHaveLength(1)
  })

  it('split draws two panes', () => {
    expect(shown({ layout: 'split' }).dataset.layout).toBe('split')
    expect(screen.getAllByTestId('app-loading-pane')).toHaveLength(2)
    // Narrow-first: stacked below md, side by side from md.
    expect(screen.getByTestId('app-loading-shape-split').className.split(' '))
      .toEqual(expect.arrayContaining(['flex-col', 'md:flex-row']))
  })

  it.each([
    ['unknown', { layout: 'grid' }],
    ['wrong case', { layout: 'Rail' }],
    ['null', null],
    ['non-object', 'rail'],
  ])('%s renders the page shape', (_label, loading) => {
    expect(shown(loading).dataset.layout).toBe('page')
    expect(screen.getByTestId('app-loading-shape-page')).toBeTruthy()
  })

  it('every skeleton block is hidden from assistive tech; only the label is read', () => {
    for (const layout of APP_LOADING_LAYOUTS) {
      const { unmount } = render(<AppLoadingFallback appName="Z" loading={{ layout }} delayMs={0} />)
      const el = screen.getByTestId('app-loading-fallback')
      const exposed = [...el.querySelectorAll('.skeleton, [data-slot="skeleton"]')]
        .filter(n => !n.closest('[aria-hidden="true"]'))
      expect(exposed, layout).toHaveLength(0)
      unmount()
    }
  })
})
