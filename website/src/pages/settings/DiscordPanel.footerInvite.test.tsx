/**
 * The Discord panel's timing-footer switch and the bot's invite links.
 *
 * The switch reads `show_timing_footer` (default on) and a save sends it. The
 * invite links come from GET /api/discord/config, which builds them only while
 * the bot is connected; without them the card says when they will appear, and a
 * value that is not a Discord authorize URL is never rendered as a link.
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

const THREADS = 'https://discord.com/oauth2/authorize?client_id=123456789012345678&scope=bot+applications.commands&permissions=309237711936'
const DM_ONLY = 'https://discord.com/oauth2/authorize?client_id=123456789012345678&scope=bot+applications.commands&permissions=0'

function config(extra: Record<string, unknown> = {}) {
  return {
    connected: true,
    connect_error: '',
    configured: true,
    read_only: false,
    bot_token_set: true,
    bot_token_preview: 'MTA5…GhIj',
    enabled: true,
    allowed_user_ids: ['42'],
    allowed_thread_ids: [],
    allowed_channel_ids: [],
    auto_thread: true,
    reactions_enabled: true,
    show_thinking: false,
    soft_threshold_pct: 80,
    session_folder: '',
    ...extra,
  }
}

beforeEach(() => {
  getDiscordConfig.mockReset()
  saveDiscordConfig.mockReset()
  saveDiscordConfig.mockResolvedValue({ ok: true, restart_required: false, verify_warning: '', reconnect_error: '' })
})

describe('DiscordPanel timing footer', () => {
  it('defaults the switch on when the backend omits the field', async () => {
    getDiscordConfig.mockResolvedValue(config())
    renderWithProviders(<DiscordPanel />)
    const toggle = await screen.findByLabelText('Reply time and context line')
    expect(toggle).toHaveAttribute('aria-checked', 'true')
  })

  it('sends the switched-off value on save', async () => {
    getDiscordConfig.mockResolvedValue(config({ show_timing_footer: true }))
    renderWithProviders(<DiscordPanel />)
    fireEvent.click(await screen.findByLabelText('Reply time and context line'))
    fireEvent.click(await screen.findByRole('button', { name: /Save Discord settings/i }))
    await waitFor(() => expect(saveDiscordConfig).toHaveBeenCalledTimes(1))
    expect(saveDiscordConfig.mock.calls[0][0]).toMatchObject({ show_timing_footer: false })
  })

  it('reads a stored off value', async () => {
    getDiscordConfig.mockResolvedValue(config({ show_timing_footer: false }))
    renderWithProviders(<DiscordPanel />)
    expect(await screen.findByLabelText('Reply time and context line')).toHaveAttribute('aria-checked', 'false')
  })
})

describe('DiscordPanel invite links', () => {
  it('links both installs while the bot is connected', async () => {
    getDiscordConfig.mockResolvedValue(config({ install_url: THREADS, install_url_dm_only: DM_ONLY }))
    renderWithProviders(<DiscordPanel />)
    const dm = await screen.findByRole('link', { name: /Invite for DMs only/ })
    const threads = screen.getByRole('link', { name: /Invite with thread access/ })
    expect(dm).toHaveAttribute('href', DM_ONLY)
    expect(threads).toHaveAttribute('href', THREADS)
    expect(dm).toHaveAttribute('rel', 'noopener noreferrer')
    // The two choices are told apart by when to pick each, and the copy does not
    // promise the invite limits where the bot answers: the allow-lists decide that.
    const body = screen.getByText(/Pick DMs only if you talk to the bot only in direct messages/)
    expect(body).toHaveTextContent(/answers only where the allow-lists below let it/)
  })

  it('says when the links will appear while the bot is not connected', async () => {
    getDiscordConfig.mockResolvedValue(config({ connected: false, install_url: '', install_url_dm_only: '' }))
    renderWithProviders(<DiscordPanel />)
    expect(await screen.findByText(/Invite links appear here once the bot is connected/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Invite for DMs only/ })).not.toBeInTheDocument()
  })

  it('renders no link for a value that is not a Discord authorize URL', async () => {
    getDiscordConfig.mockResolvedValue(config({ install_url: 'javascript:alert(1)', install_url_dm_only: DM_ONLY }))
    renderWithProviders(<DiscordPanel />)
    expect(await screen.findByText(/Invite links appear here once the bot is connected/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Invite with thread access/ })).not.toBeInTheDocument()
  })
})
