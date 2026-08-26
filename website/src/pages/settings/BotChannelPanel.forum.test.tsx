/**
 * Tests for the optional Telegram forum block in the shared BotChannelPanel.
 *
 * The forum toggle + supergroup-chat-id allowlist render ONLY when the channel
 * spec supplies a `forum` block, and the forum fields are added to the save
 * payload ONLY in that case — so Discord/Webex (which omit `forum`) never send
 * `allow_forum`/`allowed_forum_chat_ids`. These tests drive the panel with fake
 * specs (mock getConfig/saveConfig) so they exercise panel logic, not the API.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import {
  BotChannelPanel,
  type BotChannelSpec,
  type BotChannelConfigData,
} from './BotChannelPanel'

const FORUM_TOGGLE = 'Serve forum Topics as per-Topic sessions'
const FORUM_ALLOWLIST = 'Allowed supergroup chat IDs'
const FORUM_EMPTY_HINT = 'Forum enabled but no allow-listed chats'

function config(overrides: Partial<BotChannelConfigData> = {}): BotChannelConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: true,
    read_only: false,
    bot_token_set: true,
    bot_token_preview: '1102…saw',
    enabled: true,
    allowed_user_ids: ['42'],
    soft_threshold_pct: 80,
    allow_forum: false,
    allowed_forum_chat_ids: [],
    ...overrides,
  }
}

/** A minimal spec; pass `withForum` to attach the forum block (Telegram-style). */
function makeSpec(
  cfg: BotChannelConfigData,
  saveConfig: BotChannelSpec['saveConfig'],
  withForum: boolean,
): BotChannelSpec {
  const spec: BotChannelSpec = {
    name: 'Telegram',
    queryKey: `test-${withForum ? 'forum' : 'plain'}-${Math.random()}`,
    logo: <span />,
    description: 'desc',
    host: 'api.telegram.org',
    setupGuide: 'https://example.invalid/guide',
    guideBody: 'guide',
    guideLink: { label: 'Open', href: 'https://example.invalid' },
    tokenDescription: 'token desc',
    tokenPlaceholder: 'token',
    allowlistDescription: 'user ids',
    allowlistPlaceholder: '123',
    thresholdDescription: 'threshold',
    emptyAllowlistHint: 'no users',
    getConfig: () => Promise.resolve(cfg),
    saveConfig,
  }
  if (withForum) {
    spec.forum = {
      toggleLabel: FORUM_TOGGLE,
      toggleDescription: <>route each topic</>,
      allowlistLabel: FORUM_ALLOWLIST,
      allowlistDescription: 'negative supergroup chat ids',
      allowlistPlaceholder: '-1001234567890',
      emptyHint:
        'Forum enabled but no allow-listed chats: all forum traffic is denied.',
    }
  }
  return spec
}

describe('BotChannelPanel forum block', () => {
  it('renders the forum toggle + chat-id allowlist when spec.forum is present', async () => {
    const spec = makeSpec(config(), vi.fn(), true)
    renderWithProviders(<BotChannelPanel spec={spec} />)
    expect(await screen.findByText(FORUM_TOGGLE)).toBeInTheDocument()
    expect(screen.getByText(FORUM_ALLOWLIST)).toBeInTheDocument()
    expect(screen.getByText('Forum topics')).toBeInTheDocument()
  })

  it('omits the forum block entirely when spec.forum is absent (Discord/Webex)', async () => {
    const spec = makeSpec(config(), vi.fn(), false)
    renderWithProviders(<BotChannelPanel spec={spec} />)
    // Wait for the panel to hydrate (the enable toggle is always present).
    expect(await screen.findByText('Enable Telegram')).toBeInTheDocument()
    expect(screen.queryByText(FORUM_TOGGLE)).not.toBeInTheDocument()
    expect(screen.queryByText('Forum topics')).not.toBeInTheDocument()
  })

  it('shows the fail-closed hint when the toggle is on but no chat ids are listed', async () => {
    const spec = makeSpec(config({ allow_forum: true, allowed_forum_chat_ids: [] }), vi.fn(), true)
    renderWithProviders(<BotChannelPanel spec={spec} />)
    expect(await screen.findByText(new RegExp(FORUM_EMPTY_HINT))).toBeInTheDocument()
  })

  it('includes allow_forum + allowed_forum_chat_ids in the save payload when spec.forum is present', async () => {
    const saveConfig = vi
      .fn()
      .mockResolvedValue({ ok: true, restart_required: true, verify_warning: '' })
    const spec = makeSpec(
      config({ allow_forum: true, allowed_forum_chat_ids: ['-1001234567890'] }),
      saveConfig,
      true,
    )
    renderWithProviders(<BotChannelPanel spec={spec} />)
    fireEvent.click(await screen.findByRole('button', { name: /Save Telegram settings/i }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const payload = saveConfig.mock.calls[0][0]
    expect(payload).toMatchObject({
      allow_forum: true,
      allowed_forum_chat_ids: ['-1001234567890'],
    })
  })

  it('never includes forum fields in the save payload when spec.forum is absent', async () => {
    const saveConfig = vi
      .fn()
      .mockResolvedValue({ ok: true, restart_required: false, verify_warning: '' })
    const spec = makeSpec(config(), saveConfig, false)
    renderWithProviders(<BotChannelPanel spec={spec} />)
    fireEvent.click(await screen.findByRole('button', { name: /Save Telegram settings/i }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const payload = saveConfig.mock.calls[0][0]
    expect(payload).not.toHaveProperty('allow_forum')
    expect(payload).not.toHaveProperty('allowed_forum_chat_ids')
  })
})
