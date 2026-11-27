/**
 * The Discord panel keeps what the live reconnect could not apply on screen.
 *
 * A save also reconnects the running bot. When Discord refuses part of it (a
 * rejected token, thread intents without Message Content Intent), the save still
 * succeeded, so the reason renders as a persistent ErrorNotice beside the panel.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

const { getDiscordConfig, saveDiscordConfig } = vi.hoisted(() => ({
  getDiscordConfig: vi.fn(),
  saveDiscordConfig: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  api: { getDiscordConfig, saveDiscordConfig },
}))

import { DiscordPanel } from './DiscordPanel'

const REFUSED =
  'Discord refused the server-thread intents (gateway close 4014). Turn on Message Content Intent on the Bot page of the Developer Portal, then save again. DMs still work.'

beforeEach(() => {
  getDiscordConfig.mockResolvedValue({
    connected: true,
    connect_error: '',
    configured: true,
    read_only: false,
    bot_token_set: true,
    bot_token_preview: 'MTA5…GhIj',
    enabled: true,
    allowed_user_ids: ['42'],
    allowed_thread_ids: ['234567890123456789'],
    allowed_channel_ids: [],
    auto_thread: true,
    reactions_enabled: true,
    show_thinking: false,
    soft_threshold_pct: 80,
    session_folder: '',
  })
  saveDiscordConfig.mockReset()
})

describe('DiscordPanel reconnect outcome', () => {
  it('shows what the reconnect could not apply as an error notice', async () => {
    saveDiscordConfig.mockResolvedValue({
      ok: true,
      restart_required: false,
      verify_warning: '',
      reconnect_error: REFUSED,
    })
    renderWithProviders(<DiscordPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /Save Discord settings/i }))
    await waitFor(() => expect(saveDiscordConfig).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/gateway close 4014/)).toBeInTheDocument()
  })

  it('shows no notice when the reconnect applied everything', async () => {
    saveDiscordConfig.mockResolvedValue({
      ok: true,
      restart_required: false,
      verify_warning: '',
      reconnect_error: '',
    })
    renderWithProviders(<DiscordPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /Save Discord settings/i }))
    await waitFor(() => expect(saveDiscordConfig).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/gateway close 4014/)).not.toBeInTheDocument()
  })
})
