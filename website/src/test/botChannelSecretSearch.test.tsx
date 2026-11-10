/**
 * BotChannelPanel's credential fields take their labels from each channel's spec,
 * so the extractor never sees them and `settingsManual.ts` indexes them by hand.
 * Each entry must be found by the words a user types, and must land on a row the
 * panel draws under the exact label the highlight looks for.
 */
import type { ReactElement } from 'react'
import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { DiscordPanel } from '../pages/settings/DiscordPanel'
import { TelegramPanel } from '../pages/settings/TelegramPanel'
import { WeComPanel } from '../pages/settings/WeComPanel'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { scoreSettingEntry } from '../components/commandPalette/settingsSearchCore'
import { i18nT } from '../i18n/t'

const getConfig = vi.hoisted(() => () => Promise.resolve({
  connected: false,
  connect_error: '',
  configured: true,
  read_only: false,
  bot_token_set: false,
  bot_token_preview: '',
  enabled: true,
  allowed_user_ids: [],
  soft_threshold_pct: 80,
}))

vi.mock('../api/client', () => ({
  api: { getDiscordConfig: getConfig, getTelegramConfig: getConfig, getWeComConfig: getConfig },
}))

const CASES: [id: string, query: string, panel: ReactElement][] = [
  ['channels.discord-bot-token', 'bot token', <DiscordPanel />],
  ['channels.telegram-bot-token', 'bot token', <TelegramPanel />],
  ['channels.wecom-bot-secret', 'bot token', <WeComPanel />],
  ['channels.wecom-bot-id', 'bot id', <WeComPanel />],
]

describe.each(CASES)('%s', (id, query, panel) => {
  it(`is found by "${query}" and highlights a row its panel draws`, async () => {
    const entry = SETTINGS_REGISTRY.find(e => e.id === id)
    expect(entry, `${id} missing from the registry`).toBeDefined()
    expect(scoreSettingEntry(query, entry!)).not.toBeNull()
    render(
      <MemoryRouter>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          {panel}
        </QueryClientProvider>
      </MemoryRouter>,
    )
    const label = i18nT(entry!.labelKey!)
    await waitFor(() => expect(document.querySelectorAll(`[data-setting-label="${CSS.escape(label)}"]`)).toHaveLength(1))
  })
})
