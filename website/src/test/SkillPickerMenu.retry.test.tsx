import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useRef } from 'react'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({ skills: vi.fn() }))
vi.mock('../api/client', () => ({ api: mockApi }))

import SkillPickerMenu from '../components/SkillPickerMenu'

/**
 * #11405: the skill picker's load-failure notice gained the same retry
 * affordance the four sibling pickers already carry (#7068). A failed fetch is
 * recoverable in place — `refetch()` re-issues the SAME query, and on success
 * the menu re-renders from `data` with no error, so the notice clears itself.
 *
 * The control reuses the sibling pickers' inert-while-busy contract:
 * `aria-disabled` (NOT `disabled`, which drops focus to <body>) plus an
 * in-handler guard, so a second press while the re-ask is out sends nothing.
 */

function Harness({ query = '', open = true, onSelect = vi.fn(), onClose = vi.fn(), client }: {
  query?: string; open?: boolean; onSelect?: (i: { leaf: string; key: string }) => void
  onClose?: () => void; client?: QueryClient
}) {
  const ref = useRef<HTMLDivElement>(null)
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  return (
    <QueryClientProvider client={qc}>
      <div>
        <div ref={ref} data-testid="anchor">anchor</div>
        <SkillPickerMenu query={query} anchorRef={ref} open={open} onSelect={onSelect} onClose={onClose} />
      </div>
    </QueryClientProvider>
  )
}

const FAILED = /Couldn't load skills/

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

describe('SkillPickerMenu — retrying a failed skills load', () => {
  it('offers a Retry beside the load-failure notice', async () => {
    mockApi.skills.mockRejectedValue(new Error('gateway down'))
    render(<Harness query="grill" />)
    expect(await screen.findByText(FAILED)).toBeInTheDocument()
    expect(screen.getByText('Retry')).toBeInTheDocument()
  })

  it('re-issues the load on Retry and drops the notice when it lands', async () => {
    // A test-controlled gate, not a call-count: react-query can dispatch the
    // query more than once before the user acts, so "fail the first call" is
    // fragile. The payload is a BARE ARRAY (the legacy /api/skills shape).
    let fail = true
    mockApi.skills.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : Promise.resolve([{ key: 'local/grill', name: 'grill', description: 'x' }]))
    render(<Harness query="grill" />)
    await screen.findByText(FAILED)
    const callsAtFailure = mockApi.skills.mock.calls.length

    fail = false
    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(screen.queryByText(FAILED)).not.toBeInTheDocument())
    expect(mockApi.skills.mock.calls.length).toBeGreaterThan(callsAtFailure)
    expect(await screen.findByText('$grill')).toBeInTheDocument()
  })

  it('lets the keyboard reach Retry without closing and restores composer focus on activation', async () => {
    let fail = true
    mockApi.skills.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : Promise.resolve([{ key: 'local/grill', name: 'grill', description: 'x' }]))
    const onClose = vi.fn()
    render(<><textarea aria-label="composer" /><Harness query="grill" onClose={onClose} /></>)
    await screen.findByText(FAILED)
    const composer = screen.getByRole('textbox', { name: 'composer' })
    const retry = screen.getByRole('button', { name: 'Retry' })
    composer.focus()

    expect(fireEvent.keyDown(composer, { key: 'Tab' })).toBe(false)
    expect(onClose).not.toHaveBeenCalled()
    expect(retry).toHaveFocus()
    expect(fireEvent.keyDown(retry, { key: 'Enter' })).toBe(true)
    expect(onClose).not.toHaveBeenCalled()

    const callsAtFailure = mockApi.skills.mock.calls.length
    fail = false
    // fireEvent dispatches keydown only; click models the browser's native activation.
    fireEvent.click(retry)
    expect(composer).toHaveFocus()
    await waitFor(() => expect(mockApi.skills.mock.calls.length).toBeGreaterThan(callsAtFailure))
    expect(await screen.findByText('$grill')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(composer).toHaveFocus()
    expect(onClose).not.toHaveBeenCalled()
  })

  it.each([
    ['rows', [{ key: 'local/grill', name: 'grill', description: 'x' }], '$grill'],
    ['zero rows', [], /No matching skills/],
  ] as const)('returns focus to the composer when an in-flight retry lands with %s while Retry holds focus', async (_label, payload, settled) => {
    let fail = true
    let land: (v: unknown) => void = () => {}
    mockApi.skills.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : new Promise(r => { land = r }))
    render(<><textarea aria-label="composer" /><Harness query="grill" /></>)
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
    mockApi.skills.mockRejectedValue(new Error('gateway down'))
    render(<Harness query="grill" />)
    await screen.findByText(FAILED)

    fireEvent.click(screen.getByText('Retry'))

    await waitFor(() => expect(mockApi.skills).toHaveBeenCalledTimes(2))
    expect(screen.getByText(FAILED)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Retry')).not.toHaveAttribute('aria-disabled'))
  })

  it('is an inert "Retrying…" while the re-ask is out, and a second press sends nothing', async () => {
    // Reject until the notice is up; then the re-ask returns a hanging promise
    // the test controls, so the busy state is observable.
    let fail = true
    let land: (v: { key: string; name: string; description: string }[]) => void = () => {}
    mockApi.skills.mockImplementation(() =>
      fail ? Promise.reject(new Error('gateway down'))
           : new Promise(r => { land = r }))
    render(<Harness query="grill" />)
    await screen.findByText(FAILED)

    fail = false
    fireEvent.click(screen.getByText('Retry'))
    const busy = await screen.findByText('Retrying…')
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    const callsWhileBusy = mockApi.skills.mock.calls.length

    // A second press while busy must not start another fetch.
    fireEvent.click(busy)
    expect(mockApi.skills.mock.calls.length).toBe(callsWhileBusy)

    await act(async () => { land([{ key: 'local/grill', name: 'grill', description: 'x' }]) })
    await waitFor(() => expect(screen.queryByText(FAILED)).not.toBeInTheDocument())
  })

  it('offers no Retry on a genuine zero-match — only a failed load is retryable', async () => {
    // The list loaded fine; the typed query simply matches nothing. Re-asking
    // the same successful read would change nothing, so no control is offered.
    mockApi.skills.mockResolvedValue([{ key: 'local/deploy', name: 'deploy', description: 'x' }])
    render(<Harness query="nomatch" />)
    expect(await screen.findByText(/No matching skills/)).toBeInTheDocument()
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })

  it('a retry that succeeds with zero rows settles on the no-match copy, not the error copy', async () => {
    // The `retryInFlight` guard (retrying && isFetching) keeps `loadFailed` from
    // lingering past the settle commit, so a Retry whose re-ask returns an empty
    // list ends on "No matching skills", never the role="alert" failed copy.
    let fail = true
    mockApi.skills.mockImplementation(() => fail ? Promise.reject(new Error('gateway down')) : Promise.resolve([]))
    render(<Harness query="grill" />)
    await screen.findByText(FAILED)

    fail = false
    fireEvent.click(screen.getByText('Retry'))

    expect(await screen.findByText(/No matching skills/)).toBeInTheDocument()
    expect(screen.queryByText(FAILED)).not.toBeInTheDocument()
    expect(screen.queryByText('Retry')).not.toBeInTheDocument()
  })
})
