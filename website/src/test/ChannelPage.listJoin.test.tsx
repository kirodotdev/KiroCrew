// The Channels page shows two natural-language lists: the @mentions a message
// was addressed to, and the roles a New Channel preset brings. Both must join
// the way the active language joins items (zh-CN reads `A、B和C`), not with a
// hard-coded English comma.
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChannelPage from '../pages/ChannelPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
// `/all` registers the zh-CN catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

vi.mock('../api/client')

beforeAll(() => {
  // jsdom doesn't implement scrollIntoView
  Element.prototype.scrollIntoView = vi.fn()
})

const previous = i18next.language
afterEach(async () => { await i18next.changeLanguage(previous) })

type Raw = Record<string, unknown>

const member = (id: string, role: string): Raw => ({
  id, role, agent_name: 'kiro-crew-default',
  state: 'listening', listen_mode: 'mention', approval_policy: 'writes',
})

function mockApi(channel: Raw) {
  vi.mocked(api).channelsList = vi.fn().mockResolvedValue({ channels: [channel] })
  vi.mocked(api).channelGet = vi.fn().mockResolvedValue(channel)
  // Undefined presets keep the built-in fallback list (incident has three roles).
  vi.mocked(api).channelPresets = vi.fn().mockResolvedValue({ presets: undefined })
}

async function renderPage() {
  renderWithProviders(<ChannelPage />)
  await waitFor(() => expect(screen.queryByText('Loading channels...')).not.toBeInTheDocument())
}

// The page reads its strings through `i18nT`, not a hook, so the locale is set
// before render rather than switched under a mounted page.
describe('ChannelPage — natural-language lists follow the locale', () => {
  it.each([
    ['en', '→ @Researcher, @Coder, and @ghost'],
    ['zh-CN', '→ @Researcher、@Coder和@ghost'],
  ])('joins a multi-agent mention the way %s does', async (lng, expected) => {
    await i18next.changeLanguage(lng)
    mockApi({
      id: 'ch1', topic: 'Gamma rollout',
      members: { a1: member('a1', 'Researcher'), a2: member('a2', 'Coder') },
      messages: [{
        id: 'm1', from_id: 'human', from_role: 'You', content: 'Any update?',
        msg_type: 'progress', timestamp: 1_700_000_000, reply_count: 0,
        mention: ['a1', 'a2', 'ghost'],
      }],
    })
    await renderPage()
    expect(await screen.findByText(expected)).toBeInTheDocument()
  })

  it.each([
    ['en', '(Orchestrator, Logs Agent, and Code Agent)'],
    ['zh-CN', '(Orchestrator、Logs Agent和Code Agent)'],
  ])('joins a preset\'s agent roles the way %s does', async (lng, expected) => {
    await i18next.changeLanguage(lng)
    mockApi({ id: 'ch1', topic: 'Gamma rollout', members: {}, messages: [] })
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: i18next.t('pages.channelPage.new') }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(expected)).toBeInTheDocument()
  })
})
