/**
 * The hook form's inert-skills warning lists the skills the way the language does.
 *
 * A fixed ", " join would put an ASCII comma between names inside a Japanese or
 * Simplified Chinese sentence; `fmtList` with the narrow style uses the
 * language's separator and keeps English as "a, b".
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

// A saved hook whose skills can no longer fire: `Stop` with a command.
const HOOK = {
  id: 'h1', name: 'backup', event: 'Stop', matcher: '', matcher_mode: 'glob',
  command: 'true', skills: ['alpha', 'beta'], timeout: 30, enabled: true,
  last_run: 0, last_status: '', run_count: 0,
}

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => ({ hooks: [HOOK] }))
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: false },
    labels: { hooksSection: 'Provider hooks' },
    fetchProviderHooks: () => Promise.resolve({}),
  }),
}))

import HooksPage from '../pages/HooksPage'

async function inertWarning(lng: string): Promise<string> {
  await act(async () => { await i18next.changeLanguage(lng) })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  const row = within((await screen.findByText('backup')).closest('tr')!)
  fireEvent.keyDown(row.getByRole('button', { name: i18next.t('pages.hooksPage.more_actions') }), { key: 'Enter' })
  fireEvent.click(await screen.findByRole('menuitem', { name: i18next.t('pages.hooksPage.edit') }))
  const warning = await screen.findByText((text) => text.includes('alpha') && text.includes('beta'))
  return warning.textContent ?? ''
}

describe('hook form inert-skills warning', () => {
  afterEach(async () => {
    cleanup()
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('keeps the English skill list as "a, b"', async () => {
    expect(await inertWarning('en')).toContain('configuration: alpha, beta.')
  })

  it('joins Japanese and Chinese skill names with an ideographic comma', async () => {
    for (const lng of ['ja', 'zh-CN']) {
      const text = await inertWarning(lng)
      expect(text).toContain('alpha、beta')
      expect(text).not.toContain('alpha, beta')
      cleanup()
    }
  })
})
