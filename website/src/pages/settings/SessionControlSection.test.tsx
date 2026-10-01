/**
 * Settings > Developer > Session control -- the `agent.session_control` row.
 *
 * The switch shows the server's value (on is the shipped default), PATCHes the
 * key, and refetches the shared config query; a failed read or save is said,
 * and the switch never moves ahead of the server.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

vi.mock('../../api/client', () => ({
  api: {
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SessionControlSection } from './SessionControlSection'

let qc: QueryClient
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
const SWITCH = { name: /Let agents control your other sessions/ }

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: true } })
  vi.mocked(api.patchConfig).mockResolvedValue({ ok: true })
})

describe('SessionControlSection', () => {
  it('shows the stored value and saves the switch through the config key', async () => {
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(toggle)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.session_control', false))
    // The shared config query is refetched, so the row follows the server.
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalledTimes(2))
  })

  it('shows the saved value right away, without waiting for the confirming refetch', async () => {
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    // The refetch after the save never answers: only the cache write can move the switch.
    vi.mocked(api.kirocrewConfig).mockReturnValue(new Promise(() => {}))
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.getByRole('switch', SWITCH)).toHaveAttribute('aria-checked', 'false'))
    expect(screen.getByRole('switch', SWITCH)).not.toHaveAttribute('aria-disabled')
  })

  it('an operator who stored false sees Off and can turn it back on', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: false } })
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(toggle)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.session_control', true))
  })

  it('the help text says what turning it on allows, what Off leaves, and when it applies', async () => {
    wrap(<SessionControlSection />)
    await screen.findByRole('switch', SWITCH)
    expect(screen.getByText(/open sessions and read, message, stop or close your other sessions/)).toBeInTheDocument()
    expect(screen.getByText(/every agent except a crew member's DM\./)).toBeInTheDocument()
    expect(screen.getByText(/takes effect immediately, running sessions included/)).toBeInTheDocument()
  })

  it('draws no switch while the value is loading, so a default-On setting never reads as Off', async () => {
    let resolve!: (v: unknown) => void
    vi.mocked(api.kirocrewConfig).mockReturnValue(new Promise((r) => { resolve = r }))
    wrap(<SessionControlSection />)
    expect(await screen.findByTestId('session-control-loading')).toHaveTextContent('Loading the current setting…')
    expect(screen.queryByRole('switch')).toBeNull()
    resolve({ agent: { session_control: true } })
    expect(await screen.findByRole('switch', SWITCH)).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByTestId('session-control-loading')).toBeNull()
  })

  it('a failed config read is said, draws no switch, and a retry brings the switch back', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValueOnce(new Error('boom'))
    wrap(<SessionControlSection />)
    await screen.findByTestId('session-control-config-error')
    expect(screen.getByText("Couldn't load the session control setting.")).toBeInTheDocument()
    expect(screen.queryByRole('switch')).toBeNull()
    fireEvent.click(screen.getByTestId('session-control-config-retry'))
    await waitFor(() => expect(screen.queryByTestId('session-control-config-error')).toBeNull())
    expect(await screen.findByRole('switch', SWITCH)).toHaveAttribute('aria-checked', 'true')
  })

  it('a failed refetch after a successful read keeps the switch on the cached value', async () => {
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    // Any panel's invalidation can trigger this refetch; it fails with data cached.
    vi.mocked(api.kirocrewConfig).mockRejectedValueOnce(new Error('boom'))
    await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    await screen.findByTestId('session-control-config-error')
    const kept = screen.getByRole('switch', SWITCH)
    expect(kept).toHaveAttribute('aria-checked', 'true')
    expect(kept).toHaveAttribute('aria-disabled', 'true')
  })

  it('a refused save is said and the switch stays where the server has it', async () => {
    vi.mocked(api.patchConfig).mockRejectedValue(new Error('boom'))
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(toggle)
    await screen.findByText("Couldn't save the session control setting. Try again.")
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('a save that config.local.json would shadow names that file instead of saying try again', async () => {
    vi.mocked(api.patchConfig).mockRejectedValue(Object.assign(new Error('conflict'), {
      status: 409,
      body: JSON.stringify({ error: 'x', code: 'session_control_overlay_owned' }),
    }))
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(toggle)
    await screen.findByText(/set in ~\/\.kiro\/crew\/config\.local\.json, which overrides config\.json/)
    expect(screen.queryByText(/Try again/)).toBeNull()
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })
})
