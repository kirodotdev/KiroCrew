import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

/**
 * The connect flow (provider fetch, recent-repo query, the sequential connect
 * loop) belongs to `useConnectFlow` and is covered by the connect-flow tests.
 * Stubbing the whole ConnectPanel module leaves the CAROUSEL under test: page
 * navigation, the two-level Back, and which action occupies the nav slot.
 */
const flow = {
  provider: null as string | null,
  clearProvider: vi.fn(),
  submit: vi.fn(),
  pending: false,
  targets: [] as unknown[],
  progress: null as { done: number; total: number } | null,
}
vi.mock('../apps/issue-radar/ConnectPanel', () => ({
  default: () => <div data-testid="connect-panel" />,
  useConnectFlow: () => flow,
  expandsCard: (provider: string | null) => provider === 'github',
  COLLAPSED_CARD: 'zzq-collapsed',
  EXPANDED_CARD: 'zzq-expanded',
}))

const WelcomeCarousel = (await import('../apps/issue-radar/WelcomeCarousel')).default

const onConnected = vi.fn()

const back = () => screen.getByRole('button', { name: /Back/ }) as HTMLButtonElement
const next = () => screen.getByRole('button', { name: /Next/ })

/** Click Next until the connect slide (one past the five content slides). */
async function toConnectSlide() {
  for (let i = 0; i < 5; i++) await userEvent.click(next())
}

beforeEach(() => {
  vi.clearAllMocks()
  flow.provider = null
  flow.pending = false
  flow.targets = []
  flow.progress = null
})

describe('WelcomeCarousel — navigation', () => {
  it('opens on the first slide with Back disabled', () => {
    render(<WelcomeCarousel onConnected={onConnected} />)
    expect(back()).toHaveProperty('disabled', true)
    expect(screen.queryByTestId('connect-panel')).toBeNull()
  })

  it('walks forward through every content slide, then reaches the connect slide', async () => {
    render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    expect(screen.getByTestId('connect-panel')).toBeInTheDocument()
    // Next is gone on the connect slide — its slot belongs to Connect.
    expect(screen.queryByRole('button', { name: /Next/ })).toBeNull()
  })

  it('walks back off the connect slide to the last content slide', async () => {
    render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    await userEvent.click(back())
    expect(screen.queryByTestId('connect-panel')).toBeNull()
    expect(screen.getByRole('button', { name: /Next/ })).toBeInTheDocument()
  })
})

describe('WelcomeCarousel — the connect slide', () => {
  it('leaves the nav slot empty until a provider is chosen', async () => {
    render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    expect(screen.queryByRole('button', { name: /Connect/ })).toBeNull()
  })

  it('offers Connect once a provider is chosen, disabled with nothing queued', async () => {
    flow.provider = 'github'
    render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    const connect = screen.getByRole('button', { name: /Connect/ }) as HTMLButtonElement
    expect(connect).toHaveProperty('disabled', true)
    await userEvent.click(connect)
    expect(flow.submit).not.toHaveBeenCalled()
  })

  it('submits once a target is queued, and counts multiple targets', async () => {
    flow.provider = 'github'
    flow.targets = [{}, {}]
    render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    const connect = screen.getByRole('button', { name: /Connect 2/ })
    await userEvent.click(connect)
    expect(flow.submit).toHaveBeenCalledTimes(1)
  })

  it('collapses the provider selection before it leaves the slide', async () => {
    // A one-level Back here yanked a user who merely opened the wrong provider
    // all the way to the previous content slide.
    flow.provider = 'gitlab'
    render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    await userEvent.click(back())
    expect(flow.clearProvider).toHaveBeenCalledTimes(1)
    // Still on the connect slide — the pop happens on the NEXT Back.
    expect(screen.getByTestId('connect-panel')).toBeInTheDocument()
  })

  it('widens the card only for a provider that expands in place', async () => {
    flow.provider = 'gitlab'          // stub: does not expand
    const view = render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    expect(view.container.querySelector('.zzq-collapsed')).not.toBeNull()
    view.unmount()

    flow.provider = 'github'          // stub: expands
    const wide = render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()
    expect(wide.container.querySelector('.zzq-expanded')).not.toBeNull()
  })

  it('blocks navigation while a connect is in flight', async () => {
    // Navigating away does not cancel the sequential loop, so repos would keep
    // connecting behind a screen that looks like the user backed out.
    flow.provider = 'github'
    flow.targets = [{}]
    flow.progress = { done: 1, total: 3 }
    flow.pending = true
    render(<WelcomeCarousel onConnected={onConnected} />)
    await toConnectSlide()

    expect(back()).toHaveProperty('disabled', true)
    await userEvent.click(back())
    expect(flow.clearProvider).not.toHaveBeenCalled()
    // The action slot reports progress rather than the plain "Connect" label, and
    // is itself blocked so a second submit cannot stack onto the running loop.
    const action = screen.getAllByRole('button').at(-1) as HTMLButtonElement
    expect(action).toHaveProperty('disabled', true)
    expect(action.textContent).not.toBe('Connect')
  })
})
