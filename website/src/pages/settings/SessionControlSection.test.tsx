/**
 * Settings > Developer > Feature Previews > Session control -- the
 * `agent.session_control` switch.
 *
 * It shows the stored value, PATCHes the key and refetches the shared config
 * query; it draws no switch until a value has been read.
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
import { ApiError } from '../../api/apiError'
import { SessionControlSection } from './SessionControlSection'

const SWITCH = { name: /Let agents control your other sessions/ }

let qc: QueryClient
const wrap = (ui: React.ReactElement) => render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)

beforeEach(() => {
  vi.clearAllMocks()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.patchConfig).mockResolvedValue({ ok: true })
})

describe('SessionControlSection', () => {
  it('shows a stored false as Off and saves true on toggle', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: false } })
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(toggle)
    await waitFor(() => expect(api.patchConfig).toHaveBeenCalledWith('agent.session_control', true))
    await waitFor(() => expect(api.kirocrewConfig).toHaveBeenCalledTimes(2))
  })

  it('draws no switch until the value is read, and Retry after a failed read brings it', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValueOnce(new Error('boom'))
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: true } })
    wrap(<SessionControlSection />)
    expect(screen.queryByRole('switch', SWITCH)).toBeNull()
    await screen.findByTestId('session-control-config-error')
    expect(screen.queryByRole('switch', SWITCH)).toBeNull()
    fireEvent.click(screen.getByTestId('session-control-config-retry'))
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('names config.local.json on an overlay refusal without "Try again", and keeps it for a retryable failure', async () => {
    const body = {
      error: 'agent.session_control is set in config.local.json, which overrides this setting. Change or remove it there.',
      code: 'config_overlay_owned',
      overlay_override: true,
    }
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { session_control: true } })
    vi.mocked(api.patchConfig).mockRejectedValueOnce(new ApiError(409, body.error, JSON.stringify(body)))
    wrap(<SessionControlSection />)
    const toggle = await screen.findByRole('switch', SWITCH)
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(toggle)
    const notice = await screen.findByTestId('session-control-save-error')
    expect(notice).toHaveTextContent('config.local.json')
    expect(notice).not.toHaveTextContent('Try again')
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    expect(toggle).toHaveAttribute('aria-checked', 'true')

    vi.mocked(api.patchConfig).mockRejectedValueOnce(new ApiError(500, 'Internal Server Error'))
    fireEvent.click(toggle)
    await waitFor(() => expect(screen.getByTestId('session-control-save-error'))
      .toHaveTextContent("Couldn't save the session control setting. Try again."))
    expect(screen.getByTestId('session-control-save-error')).not.toHaveTextContent('config.local.json')
    await waitFor(() => expect(toggle).not.toHaveAttribute('aria-disabled'))
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })
})
