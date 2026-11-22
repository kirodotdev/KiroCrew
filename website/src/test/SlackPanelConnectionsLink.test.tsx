/**
 * Settings ▸ Slack links to the Slack card under Settings ▸ Connections, where
 * each person signs in to Slack's MCP server for their agents. The link uses the
 * Connections page's card param (`?provider=slack` rings, expands and scrolls to
 * that card) and opens in a new tab, so the Slack form and its unsaved fields
 * stay put. It is absent while the Connections gallery is off.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import { api, type SlackConfigData } from '../api/client'
import { SlackPanel } from '../pages/settings/SlackPanel'

const CONFIG: SlackConfigData = {
  connected: true,
  connect_error: '',
  configured: true,
  read_only: false,
  bot_token_set: true,
  app_token_set: true,
  bot_token_preview: '',
  app_token_preview: '',
  owner_id: 'U000OWNER',
  command: 'kirocrew',
  allowed_enterprise_ids: [],
  reactions_enabled: false,
  show_thinking: false,
}

function mount(kirocrewConfig: Record<string, unknown>) {
  vi.spyOn(api, 'getSlackConfig').mockResolvedValue(CONFIG)
  vi.spyOn(api, 'getSlackManifest').mockRejectedValue(new Error('not needed here'))
  vi.spyOn(api, 'kirocrewConfig').mockResolvedValue(kirocrewConfig as Awaited<ReturnType<typeof api.kirocrewConfig>>)
  return renderWithProviders(<SlackPanel />, { route: '/settings/slack' })
}

beforeEach(() => {
  // The restart badge fetches the config schema; answer it so it renders quietly.
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ entries: [] }) })) as unknown as typeof fetch)
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('SlackPanel → Connections Slack card link', () => {
  it('opens the Slack card on Settings → Connections in a new tab', async () => {
    mount({})
    const link = await screen.findByTestId('slack-connections-link')
    expect(link).toHaveTextContent('Set it up on the Slack card in Connections')
    expect(link).toHaveAttribute('href', '/settings/connections?provider=slack')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('is absent while the Connections gallery is off', async () => {
    const { queryClient } = mount({ connections_ui: false })
    await screen.findByText(/Talk to your agents from Slack over Socket Mode/)
    // Assert only once the flag has actually been read: an unread config also hides the link.
    await waitFor(() => expect(queryClient.getQueryData(['kirocrewConfig'])).toEqual({ connections_ui: false }))
    expect(screen.queryByTestId('slack-connections-link')).toBeNull()
  })
})
