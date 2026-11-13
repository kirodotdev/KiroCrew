import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* Mock api/client BEFORE the component imports. */
const mockApi = vi.hoisted(() => ({ slashCommands: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlashCommandMenu from '../components/SlashCommandMenu'

/**
 * #11405: the slash-command menu's load-failure notice gained the same retry
 * affordance the four sibling pickers carry (#7068). `refetch()` re-issues the
 * SAME query; on success the menu re-renders from the live list with no error,
 * so the notice clears itself. The control is inert-but-focusable while the
 * re-ask is out (`aria-disabled` + an in-handler guard), matching the siblings.
 *
 * The failure notice shows only in the ZERO-ROW state: a bare `/` still lists
 * the offline fallback, so the retry surfaces on an unmatched prefix like
 * `/xyz`, where the failed live fetch is the whole reason there are no rows.
 */

function Harness({ input, onSelect = vi.fn(), onClose = vi.fn(), client }: {
  input: string; onSelect?: (c: string) => void; onClose?: () => void; client?: QueryClient
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SlashCommandMenu input={input} anchorRef={ref} onSelect={onSelect} onClose={onClose} />
      </div>
    </QueryClientProvider>
  )
}

const FAILED = /Couldn't load commands/

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('SlashCommandMenu — retrying a failed commands load', () => {
  it('offers a Retry beside the load-failure notice', async () => {
    mockApi.slashCommands.mockRejectedValue(new Error('gateway down'))
    render(<Harness input="/xyz" />)
    expect(await screen.findByText(FAILED)).toBeInTheDocument()
    expect(screen.getByText('Retry')).toBeInTheDocument()
  })

  it('re-issues the load on Retry and drops the notice when it lands', async () => {
    // A test-controlled gate, not a call-count: react-query can dispatch the
    // query more than once before the user acts.
    let fail = true
    mockApi.slashCommands.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : Promise.resolve([{ name: '/xyzzy', description: 'ok' }]))
    render(<Harness input="/xyz" />)
    await screen.findByText(FAILED)
    const callsAtFailure = mockApi.slashCommands.mock.calls.length

    fail = false
    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(screen.queryByText(FAILED)).not.toBeInTheDocument())
    expect(mockApi.slashCommands.mock.calls.length).toBeGreaterThan(callsAtFailure)
    expect(await screen.findByText('/xyzzy')).toBeInTheDocument()
  })

  it('lets the keyboard reach Retry without closing and restores composer focus on activation', async () => {
    let fail = true
    mockApi.slashCommands.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : Promise.resolve([{ name: '/xyzzy', description: 'ok' }]))
    const onClose = vi.fn()
    render(<><textarea aria-label="composer" /><Harness input="/xyz" onClose={onClose} /></>)
    await screen.findByText(FAILED)
    const composer = screen.getByRole('textbox', { name: 'composer' })
    const retry = screen.getByRole('button', { name: 'Retry' })
    composer.focus()

    expect(fireEvent.keyDown(composer, { key: 'Tab' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    expect(retry).toHaveFocus()
    expect(fireEvent.keyDown(retry, { key: 'Enter' })).toBe(true)
    expect(onClose).not.toHaveBeenCalled()

    const callsAtFailure = mockApi.slashCommands.mock.calls.length
    fail = false
    // fireEvent dispatches keydown only; click models the browser's native activation.
    fireEvent.click(retry)
    expect(composer).toHaveFocus()
    await waitFor(() => expect(mockApi.slashCommands.mock.calls.length).toBeGreaterThan(callsAtFailure))
    expect(await screen.findByText('/xyzzy')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(composer).toHaveFocus()
    expect(onClose).not.toHaveBeenCalled()
  })

  it.each([
    ['rows', [{ name: '/xyzzy', description: 'ok' }], '/xyzzy'],
    ['zero rows', [{ name: '/compact', description: 'ok' }], /No matching commands/],
  ] as const)('returns focus to the composer when an in-flight retry lands with %s while Retry holds focus', async (_label, payload, settled) => {
    let fail = true
    let land: (v: unknown) => void = () => {}
    mockApi.slashCommands.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : new Promise(r => { land = r }))
    render(<><textarea aria-label="composer" /><Harness input="/xyz" /></>)
    await screen.findByText(FAILED)
    const composer = screen.getByRole('textbox', { name: 'composer' })
    composer.focus()

    fireEvent.keyDown(composer, { key: 'Tab' })
    const retry = screen.getByRole('button', { name: 'Retry' })
    expect(retry).toHaveFocus()
    fail = false
    fireEvent.click(retry)
    expect(composer).toHaveFocus()
    expect(await screen.findByText('Retrying…')).toBeInTheDocument()

    // Tab during the in-flight re-ask lands on the inert control again.
    fireEvent.keyDown(composer, { key: 'Tab' })
    expect(screen.getByRole('button', { name: 'Retrying…' })).toHaveFocus()

    await act(async () => { land(payload) })
    expect(await screen.findByText(settled)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(composer).toHaveFocus()
    expect(document.activeElement).not.toBe(document.body)
  })

  it('keeps the notice and returns the control to Retry when the re-ask fails again', async () => {
    mockApi.slashCommands.mockRejectedValue(new Error('gateway down'))
    render(<Harness input="/xyz" />)
    await screen.findByText(FAILED)

    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(mockApi.slashCommands).toHaveBeenCalledTimes(2))
    expect(screen.getByText(FAILED)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Retry')).not.toHaveAttribute('aria-disabled'))
  })

  it('is an inert "Retrying…" while the re-ask is out, and a second press sends nothing', async () => {
    let fail = true
    let land: (v: { name: string; description: string }[]) => void = () => {}
    mockApi.slashCommands.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : new Promise(r => { land = r }))
    render(<Harness input="/xyz" />)
    await screen.findByText(FAILED)

    fail = false
    fireEvent.click(screen.getByText('Retry'))
    const busy = await screen.findByText('Retrying…')
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    const callsWhileBusy = mockApi.slashCommands.mock.calls.length

    fireEvent.click(busy)
    expect(mockApi.slashCommands.mock.calls.length).toBe(callsWhileBusy)

    await act(async () => { land([{ name: '/xyzzy', description: 'ok' }]) })
    await waitFor(() => expect(screen.queryByText(FAILED)).not.toBeInTheDocument())
  })

  it('offers no Retry on a genuine zero-match — only a failed load is retryable', async () => {
    // The live list loaded fine; `/xyz` just matches nothing in it. Re-asking
    // the same successful read would change nothing, so no control is offered.
    mockApi.slashCommands.mockResolvedValue([{ name: '/compact', description: 'ok' }])
    render(<Harness input="/xyz" />)
    expect(await screen.findByText(/No matching commands/)).toBeInTheDocument()
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })

  it('a retry that succeeds with no matching command settles on the no-match copy, not the error copy', async () => {
    // The `retryInFlight` guard (retrying && isFetching) keeps `loadFailed` from
    // lingering past the settle commit, so a Retry whose re-ask returns a list
    // with no `/xyz` match ends on "No matching commands", never the failed copy.
    let fail = true
    mockApi.slashCommands.mockImplementation(() => fail ? Promise.reject(new Error('gateway down')) : Promise.resolve([{ name: '/compact', description: 'ok' }]))
    render(<Harness input="/xyz" />)
    await screen.findByText(FAILED)

    fail = false
    fireEvent.click(screen.getByText('Retry'))

    expect(await screen.findByText(/No matching commands/)).toBeInTheDocument()
    expect(screen.queryByText(FAILED)).not.toBeInTheDocument()
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })
})
