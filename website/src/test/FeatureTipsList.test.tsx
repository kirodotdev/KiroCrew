/**
 * Settings ▸ Chat ▸ Discovery — the list of every curated tip.
 *
 * The list is read-only: it shows each tip with its own action, and opening an
 * action navigates without sending tip feedback, so browsing never changes
 * which tip the composer offers next.
 */
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { i18nT } from '../i18n/t'
// `/all` for the non-English catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

const { tipsAllMock, tipsFeedbackMock } = vi.hoisted(() => ({
  tipsAllMock: vi.fn(),
  tipsFeedbackMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: { tipsAll: tipsAllMock, tipsFeedback: tipsFeedbackMock },
}))

import { FeatureTipsList } from '../pages/settings/FeatureTipsList'

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/settings?tab=chat&sub=discovery']}>
      <QueryClientProvider client={qc}>
        <Routes>
          <Route path="/settings/secrets" element={<div>secrets page</div>} />
          <Route path="*" element={<FeatureTipsList />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('FeatureTipsList', () => {
  // Unmount first: a language change repaints the mounted list outside act().
  afterEach(async () => { cleanup(); await i18next.changeLanguage('en') })

  it('lists every tip with its action, and opening one sends no tip feedback', async () => {
    tipsAllMock.mockResolvedValue({
      tips: [
        { id: 'secrets-vault', title: 'Keep keys in the vault', body: 'Store **API keys** once.', action: { kind: 'route', label: 'Open Secrets settings', route: '/settings/secrets' } },
        { id: 'focus-mode', title: 'Hide the chrome', body: 'Press a key.' },
      ],
    })
    renderList()

    fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.settings.featureTipsList.title') }))
    expect(screen.getByText('Keep keys in the vault')).toBeInTheDocument()
    expect(screen.getByText('Hide the chrome')).toBeInTheDocument()
    // The tip without an action draws no button of its own.
    expect(screen.getAllByRole('button', { name: /^Open / })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Open Secrets settings' }))
    expect(await screen.findByText('secrets page')).toBeInTheDocument()
    expect(tipsFeedbackMock).not.toHaveBeenCalled()
  })

  it('lists a curated tip in the dashboard language', async () => {
    await i18next.changeLanguage('zh-CN')
    tipsAllMock.mockResolvedValue({
      tips: [{
        id: 'secrets-vault',
        title: 'Store credentials in the encrypted vault',
        body: 'For a command-based stdio MCP server, save the credential under **Settings > Secrets**, route the server through **MCP Management**, approve its launch, and restart Crew to use **secret://KEY** in its environment. HTTP/SSE servers cannot use this routing path.',
        action: { kind: 'route', label: 'Open Secrets settings', route: '/settings/secrets' },
      }],
    })
    renderList()

    fireEvent.click(await screen.findByRole('button', { name: i18nT('pages.settings.featureTipsList.title') }))
    const copy = zh.components.tipCard.curated
    expect(screen.getByText(copy.secrets_vault_title)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: copy.secrets_vault_action })).toBeInTheDocument()
  })
})
