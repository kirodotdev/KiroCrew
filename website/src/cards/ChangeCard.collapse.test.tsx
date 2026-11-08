/**
 * #24534: a finishing change card collapses by animating its real height to
 * the measured content, never by a framer-motion `layout` size scale (which
 * stretched the text and let the next message draw over the still-tall box).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { forwardRef, type ReactNode } from 'react'
import { store as reduxStore } from '../store'
import type { Card } from '../api/cards'

const reduced = vi.hoisted(() => ({ value: false }))

// Record what each motion element is asked to do, as attributes on a plain element.
vi.mock('framer-motion', async importOriginal => {
  const actual = await importOriginal<typeof import('framer-motion')>()
  const record = (Tag: 'div' | 'section') =>
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    forwardRef<HTMLElement, any>(({ layout, animate, transition, initial: _i, children, ...rest }, ref) => {
      const Element = Tag as unknown as (p: Record<string, unknown>) => ReactNode
      return (
        <Element
          ref={ref}
          data-motion-layout={layout === undefined ? undefined : String(layout)}
          data-motion-animate={animate === undefined ? undefined : JSON.stringify(animate)}
          data-motion-transition={transition === undefined ? undefined : JSON.stringify(transition)}
          {...rest}
        >
          {children}
        </Element>
      )
    })
  return {
    ...actual,
    useReducedMotion: () => reduced.value,
    motion: { ...actual.motion, div: record('div'), section: record('section') },
  }
})

import ChangeCard from './ChangeCard'

const card = (over: Partial<Card> = {}): Card => ({
  id: 'c1',
  slot_key: 'slot-A',
  kind: 'setting.change',
  revision: 1,
  status: 'cancelled',
  risk: 'normal',
  title: 'Shorter replies',
  changes: [{ label: 'Reply length', before: 'standard', after: 'brief' }],
  editable: [],
  params: { path: 'chat.verbosity', value: 'brief' },
  plan: { apply: [], undo: [] },
  created_at: 1_700_000_000,
  // A finished card ignores expiry; a fixed instant keeps the input host-free.
  expires_at: 1_700_003_600_000,
  ...over,
})

function mount(c: Card) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={reduxStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <ChangeCard card={c} store={() => {}} refresh={() => {}} remove={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  reduced.value = false
})

describe('ChangeCard collapse (#24534)', () => {
  it('animates the measured height and never a layout scale', () => {
    // A scaling ancestor shrinks the rect; the sizer must follow the layout height.
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ height: 42 } as DOMRect)
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(44)
    mount(card())
    const root = screen.getByTestId('change-card')
    expect(root.querySelector('[data-motion-layout]')).toBeNull()
    expect(root.getAttribute('data-motion-layout')).toBeNull()
    const sizer = screen.getByTestId('change-card-sizer')
    expect(JSON.parse(sizer.getAttribute('data-motion-animate') ?? '{}')).toEqual({ height: 44 })
    expect(sizer.getAttribute('data-motion-animate')).not.toMatch(/scale/)
    expect(JSON.parse(sizer.getAttribute('data-motion-transition') ?? '{}')).toEqual({ duration: 0.2 })
    expect(sizer.contains(screen.getByTestId('change-card-result'))).toBe(true)
  })

  it('does not animate under reduced motion', () => {
    reduced.value = true
    mount(card())
    const sizer = screen.getByTestId('change-card-sizer')
    expect(JSON.parse(sizer.getAttribute('data-motion-transition') ?? '{}')).toEqual({ duration: 0 })
  })
})
