/**
 * The new/edit-hook card's text fields carry an accessible name.
 *
 * Name, Command and Matcher had only a placeholder, and Timeout only a
 * neighbouring word that was not tied to its field. A placeholder vanishes as
 * soon as the user types and is not a label, so a screen reader announced an
 * unnamed edit field. Each now reuses the page's existing catalog key, the same
 * way the Event and matcher-mode pickers beside them do.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { i18nT } from '../i18n/t'

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => ({ hooks: [] }))
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

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('hooks page — text field labels', () => {
  it('names the Name, Command, Matcher and Timeout inputs', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: '+ New Hook' }))

    // getByLabelText ignores placeholders and loose text, so these fail on an unnamed field.
    const name = await screen.findByLabelText(i18nT('pages.hooksPage.hook_name'))
    const command = screen.getByLabelText(i18nT('pages.hooksPage.command'))
    const matcher = screen.getByLabelText(i18nT('pages.hooksPage.matcher'))
    const timeout = screen.getByLabelText(i18nT('pages.hooksPage.timeout'))
    for (const el of [name, command, matcher, timeout]) expect(el.tagName).toBe('INPUT')
    expect(timeout).toHaveAttribute('type', 'number')

    fireEvent.change(command, { target: { value: 'echo hi' } })
    expect(command).toHaveValue('echo hi')
  })
})
