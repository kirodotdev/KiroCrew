/**
 * The registry trust banner lists its hosts the way the language does.
 *
 * The host list was joined with an English ", " in code, so Japanese and
 * Simplified Chinese read an ASCII comma between full-width text. It now goes
 * through `fmtList` with the narrow style, which keeps English as "a, b".
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'
import RegistryManager from '../components/RegistryManager'

const mockListRegistries = vi.fn()
const mockUpdateRegistries = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    listRegistries: (...args: unknown[]) => mockListRegistries(...args),
    updateRegistries: (...args: unknown[]) => mockUpdateRegistries(...args),
    refreshRegistries: vi.fn(),
  },
}))

async function showBanner(lng: string, hosts: string[]): Promise<string> {
  await act(async () => { await i18next.changeLanguage(lng) })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  mockListRegistries.mockResolvedValue({ registries: [] })
  mockUpdateRegistries.mockResolvedValue({ ok: true, registries: [], newlyTrustedHosts: hosts })
  render(<QueryClientProvider client={qc}><RegistryManager /></QueryClientProvider>)
  const add = i18next.t('components.registryManager.add_registry')
  await waitFor(() => screen.getAllByText(add))
  fireEvent.click(screen.getAllByText(add)[0])
  fireEvent.change(screen.getByPlaceholderText(/app-registry/), {
    target: { value: 'https://github.com/org/app-registry' },
  })
  const buttons = screen.getAllByText(add)
  fireEvent.click(buttons[buttons.length - 1])
  const banner = await screen.findByText((text) => text.includes(hosts[0]) && text.includes(hosts[1]))
  return banner.textContent ?? ''
}

describe('registry trust banner host list', () => {
  afterEach(async () => {
    cleanup()
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('keeps the English host list as "a, b"', async () => {
    expect(await showBanner('en', ['a.dev', 'b.dev'])).toContain('apps from a.dev, b.dev.')
  })

  it('joins Japanese and Chinese hosts with an ideographic comma', async () => {
    for (const lng of ['ja', 'zh-CN']) {
      const text = await showBanner(lng, ['a.dev', 'b.dev'])
      expect(text).toContain('a.dev、b.dev')
      expect(text).not.toContain('a.dev, b.dev')
      cleanup()
    }
  })
})
