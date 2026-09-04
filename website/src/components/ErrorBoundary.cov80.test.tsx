import { render, screen, fireEvent } from '@testing-library/react'
import ErrorBoundary from './ErrorBoundary'
import { recordEvent } from '../rum'
import { recordError } from '../utils/errorReport'

vi.mock('../rum', () => ({ recordEvent: vi.fn() }))
vi.mock('../utils/errorReport', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/errorReport')>()
  return { ...mod, recordError: vi.fn(mod.recordError) }
})
vi.mock('./AskAgentButton', () => ({
  default: ({ message }: { message?: string }) => <button type="button">zzq-ask|{message}</button>,
  askAgentHard: vi.fn(),
}))

const { askAgentHard } = await import('./AskAgentButton')

/**
 * Throw driven by a PROP, never by a mutable counter: React re-invokes a
 * throwing render a second time to rebuild the component stack, so a
 * "throw once" component silently succeeds on the retry and the boundary
 * never engages.
 */
function Boom({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('zzq-render-broke')
  return <div>zzq-recovered</div>
}

describe('ErrorBoundary', () => {
  let consoleError: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.mocked(recordEvent).mockReset()
    vi.mocked(recordError).mockReset()
    vi.mocked(askAgentHard).mockReset()
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => consoleError.mockRestore())

  it('renders children untouched when nothing throws', () => {
    render(<ErrorBoundary><Boom shouldThrow={false} /></ErrorBoundary>)
    expect(screen.getByText('zzq-recovered')).toBeInTheDocument()
    expect(recordEvent).not.toHaveBeenCalled()
  })

  it('journals and RUM-logs a caught throw, attributing it to the scope', () => {
    render(
      <ErrorBoundary scope="zzq-scope">
        <Boom shouldThrow />
      </ErrorBoundary>,
    )
    expect(screen.getByText('zzq-render-broke')).toBeInTheDocument()
    expect(consoleError).toHaveBeenCalled()
    expect(vi.mocked(recordError).mock.calls[0][0]).toMatchObject({
      source: 'render',
      message: 'zzq-render-broke',
      code: 'zzq-scope',
    })
    expect(vi.mocked(recordEvent).mock.calls[0][1]).toMatchObject({ scope: 'zzq-scope' })
  })

  it('attributes an unscoped root boundary to "root" and a route one to "route"', () => {
    const { unmount } = render(<ErrorBoundary root><Boom shouldThrow /></ErrorBoundary>)
    expect(vi.mocked(recordEvent).mock.calls[0][1]).toMatchObject({ scope: 'root' })
    unmount()

    vi.mocked(recordEvent).mockReset()
    render(<ErrorBoundary><Boom shouldThrow /></ErrorBoundary>)
    expect(vi.mocked(recordEvent).mock.calls[0][1]).toMatchObject({ scope: 'route' })
  })

  it('a throwing journal or RUM sink never masks the original error', () => {
    vi.mocked(recordError).mockImplementation(() => { throw new Error('zzq-journal-broke') })
    vi.mocked(recordEvent).mockImplementation(() => { throw new Error('zzq-rum-broke') })
    render(<ErrorBoundary><Boom shouldThrow /></ErrorBoundary>)
    expect(screen.getByText('zzq-render-broke')).toBeInTheDocument()
  })

  it('an explicit fallback={null} renders nothing at all', () => {
    const { container } = render(
      <ErrorBoundary fallback={null}><Boom shouldThrow /></ErrorBoundary>,
    )
    expect(container.textContent).toBe('')
  })

  it('a supplied fallback replaces the default card', () => {
    render(
      <ErrorBoundary fallback={<div>zzq-custom</div>}><Boom shouldThrow /></ErrorBoundary>,
    )
    expect(screen.getByText('zzq-custom')).toBeInTheDocument()
    expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
  })

  it('the route fallback offers Ask-the-agent plus a recovering Try Again', () => {
    const { rerender } = render(<ErrorBoundary><Boom shouldThrow /></ErrorBoundary>)
    expect(screen.getByText('zzq-ask|zzq-render-broke')).toBeInTheDocument()

    // The cause is fixed underneath, then the user retries.
    rerender(<ErrorBoundary><Boom shouldThrow={false} /></ErrorBoundary>)
    fireEvent.click(screen.getByRole('button', { name: 'Try Again' }))
    expect(screen.getByText('zzq-recovered')).toBeInTheDocument()
  })

  it('the root fallback paints theme-independent inline colors and offers three actions', () => {
    const { container, rerender } = render(
      <ErrorBoundary root><Boom shouldThrow /></ErrorBoundary>,
    )

    const shell = container.firstElementChild as HTMLElement
    expect(shell.style.backgroundColor).toBe('#1a1a1a')
    expect(shell.style.minHeight).toBe('100vh')

    fireEvent.click(screen.getByRole('button', { name: 'Ask the agent' }))
    expect(askAgentHard).toHaveBeenCalledWith('zzq-render-broke')

    const reload = vi.fn()
    const original = window.location
    Object.defineProperty(window, 'location', {
      value: { ...original, reload },
      writable: true,
      configurable: true,
    })
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Reload page' }))
      expect(reload).toHaveBeenCalledTimes(1)
    } finally {
      Object.defineProperty(window, 'location', {
        value: original,
        writable: true,
        configurable: true,
      })
    }

    rerender(<ErrorBoundary root><Boom shouldThrow={false} /></ErrorBoundary>)
    fireEvent.click(screen.getByRole('button', { name: 'Try Again' }))
    expect(screen.getByText('zzq-recovered')).toBeInTheDocument()
  })

  it('falls back to the error name when the message is empty', () => {
    function Nameless() {
      const e = new Error('')
      e.name = 'ZzqNamedError'
      throw e
    }
    render(<ErrorBoundary><Nameless /></ErrorBoundary>)
    expect(vi.mocked(recordError).mock.calls[0][0]).toMatchObject({ message: 'ZzqNamedError' })
  })
})
