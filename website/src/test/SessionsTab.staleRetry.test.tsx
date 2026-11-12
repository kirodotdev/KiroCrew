/**
 * The "could not refresh" banner's Retry, over rows from an earlier read.
 *
 * The banner stays mounted while its retry runs (react-query keeps `isError`
 * with the old data until a read succeeds), so the button must stay focusable:
 * a native `disabled` took it out of the tab order and dropped a keyboard
 * user's focus to the page for the whole retry.
 *
 * The cache is seeded with a payload and the mounted read rejects, so the
 * banner appears without waiting on the 5s poll; no timers, no sleeps.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render, screen, cleanup, waitFor, fireEvent, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { useRef, type MutableRefObject } from 'react'
import SessionsTab from '../pages/system/SessionsTab'
import type { PlaneState } from '../pages/SystemPage'
import { api } from '../api/client'

const PAYLOAD = {
  sessions: [{
    key: 'dashboard:chat-1-1', title: 'Live session', slot_key: 'chat-1-1', untitled: false,
    agent: 'kirocrew', channel: 'dashboard', pid: 4242, owns_runtime: true, prompts: 3,
    rss_mb: 512, peak_mb: 600, cpu_cores: 0.4, procs: 2, mcp: 1, uptime_s: 300,
    shared: false, credits: 1, turns: 2,
  }],
  tasks: [],
  totals: { rss_mb: 512, host_mb: 16000, procs: 2, sessions: 1, tasks: 0 },
  history: [],
}

function Harness() {
  const planeStateRef = useRef<PlaneState>({}) as MutableRefObject<PlaneState>
  return <SessionsTab planeStateRef={planeStateRef} />
}

function mountWithStaleRows() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['sessionsMemory'], PAYLOAD)
  return render(
    <MemoryRouter initialEntries={['/developer?tab=system']}>
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

function mountWithNoRows() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/developer?tab=system']}>
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return qc
}

describe('SessionsTab could-not-read Retry', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('keeps keyboard focus on Retry while it runs, and ignores a second press', async () => {
    const spy = vi.spyOn(api, 'sessionsMemory').mockRejectedValue(new Error('boom') as never)
    mountWithNoRows()
    const panel = await screen.findByTestId('sessions-error')
    const button = within(panel).getByRole('button', { name: 'Retry' })

    let fail: (e: Error) => void = () => {}
    spy.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }) as never)
    const calls = spy.mock.calls.length
    button.focus()
    fireEvent.click(button)
    await waitFor(() => expect(button.textContent).toBe('Retrying…'))
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()

    fireEvent.click(button)
    expect(spy.mock.calls.length).toBe(calls + 1)

    await act(async () => { fail(new Error('boom')) })
    await waitFor(() => expect(button.textContent).toBe('Retry'))
    expect(button).not.toHaveAttribute('aria-disabled')
    expect(button).toHaveFocus()
  })
})

describe('SessionsTab could-not-read panel', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('stays up, with focus on Retry, through a background poll', async () => {
    const spy = vi.spyOn(api, 'sessionsMemory').mockRejectedValue(new Error('boom') as never)
    const qc = mountWithNoRows()
    const panel = await screen.findByTestId('sessions-error')
    // The failure reads through ErrorNotice, the one error surface.
    expect(within(panel).getByRole('alert').textContent).toContain('we can’t tell what is running')
    const button = within(panel).getByRole('button', { name: 'Retry' })
    button.focus()

    // The 5s poll's refetch, not a press: it must not swap the panel out either.
    let fail: (e: Error) => void = () => {}
    spy.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }) as never)
    let poll: Promise<void> = Promise.resolve()
    act(() => { poll = qc.refetchQueries({ queryKey: ['sessionsMemory'] }) })
    await waitFor(() => expect(button.textContent).toBe('Retrying…'))
    expect(screen.getByTestId('sessions-error')).toBe(panel)
    expect(button).toHaveFocus()

    await act(async () => { fail(new Error('boom')); await poll.catch(() => {}) })
    await waitFor(() => expect(button.textContent).toBe('Retry'))
    expect(button).toHaveFocus()
  })
})

describe('SessionsTab stale-rows Retry', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks() })

  it('keeps keyboard focus on Retry while it runs, and ignores a second press', async () => {
    const spy = vi.spyOn(api, 'sessionsMemory').mockRejectedValue(new Error('boom') as never)
    mountWithStaleRows()
    const banner = await screen.findByTestId('sessions-stale')
    const button = within(banner).getByRole('button', { name: 'Retry' })
    // The failure reads through ErrorNotice, the one error surface.
    expect(within(banner).getByText('Can’t refresh — showing the last values we read.')).toBeTruthy()

    let fail: (e: Error) => void = () => {}
    spy.mockImplementationOnce(() => new Promise((_, reject) => { fail = reject }) as never)
    const calls = spy.mock.calls.length
    button.focus()
    fireEvent.click(button)
    await waitFor(() => expect(button.textContent).toBe('Retrying…'))
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()

    fireEvent.click(button)
    expect(spy.mock.calls.length).toBe(calls + 1)

    await act(async () => { fail(new Error('boom')) })
    await waitFor(() => expect(button.textContent).toBe('Retry'))
    expect(button).not.toHaveAttribute('aria-disabled')
    expect(button).toHaveFocus()
    // The rows the user was reading are still there.
    expect(screen.getByText('Live session')).toBeTruthy()
  })
})
