/**
 * Three Settings labels that used to render as fixed English, rendered under the
 * `en-XA` pseudolocale.
 *
 * - BotChannelPanel's status pill: Connected / Not connected / Needs setup.
 * - InstancesPanel's "— not active until restart" header tail.
 * - NotificationsPanel's priority select: critical / default / passive were the
 *   raw wire values, so only "Channel default" was translated.
 *
 * The pseudolocale accents and brackets every catalog value, so a label read from
 * the catalog renders as its `en-XA` value and a hardcoded literal renders as
 * plain English. Each case asserts both halves. The priority case also pins that
 * the select still writes the wire value (`passive`), not the translated label.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listInstances: vi.fn(),
      notificationChannels: vi.fn(),
      updateNotificationChannelSettings: vi.fn(),
    },
  }
})

import { api } from '../../api/client'
import { i18next } from '../../i18n/all'
import { BotChannelPanel, type BotChannelSpec, type BotChannelConfigData } from './BotChannelPanel'
import { InstancesPanel } from './InstancesPanel'
import { NotificationsPanel } from './NotificationsPanel'
import type { NotificationChannel } from '../../types'

const XA = 'en-XA'
const xa = (key: string) => i18next.getFixedT(XA)(key)

beforeEach(async () => {
  vi.clearAllMocks()
  await i18next.changeLanguage(XA)
})

afterEach(async () => {
  // Unmount first: a language switch re-renders every mounted `useTranslation`
  // consumer, and doing it after the test's last barrier is an act() warning.
  cleanup()
  await i18next.changeLanguage('en')
})

function botConfig(overrides: Partial<BotChannelConfigData>): BotChannelConfigData {
  return {
    connected: false,
    connect_error: '',
    configured: false,
    read_only: false,
    bot_token_set: false,
    bot_token_preview: '',
    enabled: true,
    allowed_user_ids: [],
    soft_threshold_pct: 80,
    ...overrides,
  }
}

function botSpec(cfg: BotChannelConfigData): BotChannelSpec {
  return {
    name: 'Telegram',
    queryKey: `pseudo-status-${Math.random()}`,
    namespace: 'telegram',
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
    saveConfig: vi.fn(),
  }
}

describe('Settings status labels under the pseudolocale', () => {
  it.each([
    ['connected', { connected: true, configured: true }, 'connected', 'Connected'],
    ['configured but not linked', { connected: false, configured: true }, 'not_connected', 'Not connected'],
    ['not configured', { connected: false, configured: false }, 'needs_setup', 'Needs setup'],
  ] as const)('translates the bot channel status pill when %s', async (_state, cfg, key, english) => {
    renderWithProviders(<BotChannelPanel spec={botSpec(botConfig(cfg))} />)
    // The same keys the Channels list row reads, so the pill and the row agree.
    const label = xa(`pages.settings.channelsPanel.${key}`)
    expect(label).toMatch(/^\[/)
    expect(await screen.findByText(label)).toBeInTheDocument()
    expect(screen.queryByText(english)).not.toBeInTheDocument()
  })

  it('translates the "not active until restart" header tail', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, instances: [], warm_set_cap: 5 })
    renderWithProviders(<InstancesPanel />)
    const tail = xa('pages.settings.instancesPanel.not_active_until_restart')
    expect(tail).toMatch(/^\[/)
    await waitFor(() => expect(document.body.textContent).toContain(tail))
    expect(document.body.textContent).not.toContain('not active until restart')
  })

  it('translates the priority labels and keeps writing the wire value', async () => {
    const channels: NotificationChannel[] = [
      { channel: 'system.cron', source: 'system', registered: true, default_priority: 'passive', protected: false, settings: { priority: 'critical' } },
    ]
    vi.mocked(api.notificationChannels).mockResolvedValue({ channels })
    vi.mocked(api.updateNotificationChannelSettings).mockResolvedValue({} as never)
    renderWithProviders(<NotificationsPanel />, { route: '/settings?tab=notifications&sub=sources' })

    const critical = xa('pages.settings.notificationsPanel.priority_critical')
    const passive = xa('pages.settings.notificationsPanel.priority_passive')
    // The row's description interpolates the translated default priority.
    expect(await screen.findByText(xa('pages.settings.notificationsPanel.default_priority').replace('{{priority}}', passive)))
      .toBeInTheDocument()

    const trigger = screen.getByRole('combobox', { name: xa('pages.settings.notificationsPanel.priority_for').replace('{{name}}', 'system.cron') })
    expect(trigger).toHaveTextContent(critical)
    expect(trigger).not.toHaveTextContent(/^critical$/)

    fireEvent.keyDown(trigger, { key: 'Enter' })
    const listbox = await screen.findByRole('listbox')
    const names = within(listbox).getAllByRole('option').map(o => o.textContent)
    expect(names).toEqual([
      xa('pages.settings.notificationsPanel.channel_default'),
      critical,
      xa('pages.settings.notificationsPanel.priority_default'),
      passive,
    ])
    for (const raw of ['critical', 'default', 'passive']) expect(names).not.toContain(raw)

    fireEvent.click(within(listbox).getByRole('option', { name: passive }))
    await waitFor(() => expect(api.updateNotificationChannelSettings).toHaveBeenCalled())
    expect(vi.mocked(api.updateNotificationChannelSettings).mock.calls.at(-1)).toContainEqual(
      expect.objectContaining({ priority: 'passive' }),
    )
  })
})
