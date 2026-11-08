/**
 * Settings ▸ Chat ▸ Discovery — the list of every curated tip.
 *
 * The list is read-only: it shows each tip with its own action, and opening an
 * action navigates without sending tip feedback, so browsing never changes
 * which tip the composer offers next.
 */
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { i18nT } from '../i18n/t'

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
})
