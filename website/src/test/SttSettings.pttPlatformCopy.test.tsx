/**
 * The push-to-talk panel's own prose is platform-specific and must follow the
 * platform, exactly as the key NAMES already do.
 *
 * The mac default is a bare right Option; the non-mac default is the
 * Alt+Shift+Space chord. Rendering the mac copy unconditionally put a false
 * claim — "Right Option ⌥ works out of the box" — directly above a test strip
 * that reads "Press Alt + Shift + Space", so every Windows/Linux visitor was
 * told the wrong thing about the single fact the feature depends on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { initI18n } from '../i18n'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: { sttConfig: vi.fn(), saveSttConfig: vi.fn(), sttInstall: vi.fn() },
}))

// IS_MAC is resolved once at module load from the UA, so the only way to
// exercise the other platform is to override it on the module. Everything else
// in the module is kept real — the copy selection is what is under test.
vi.mock('../lib/pushToTalk', async importOriginal => {
  const actual = await importOriginal<typeof import('../lib/pushToTalk')>()
  return { ...actual, IS_MAC: false }
})

const mockApi = api as unknown as { sttConfig: ReturnType<typeof vi.fn>; saveSttConfig: ReturnType<typeof vi.fn> }

beforeEach(async () => {
  await initI18n()
  localStorage.clear()
  mockApi.sttConfig.mockResolvedValue({
    enabled: true,
    provider: 'whisper',
    streaming: false,
    providers: ['whisper'],
    streaming_providers: [],
    models: { turbo: '1.5 GB' },
    installed: ['turbo'],
    model: 'turbo',
  })
  mockApi.saveSttConfig.mockResolvedValue({ ok: true })
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

async function renderPanel() {
  const SttSettings = (await import('../pages/settings/SttSettings')).default
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}><SttSettings /></QueryClientProvider>
    </Provider>,
  )
  await waitFor(() => expect(mockApi.sttConfig).toHaveBeenCalled())
}

describe('push-to-talk panel copy follows the platform', () => {
  it('does not claim Right Option works out of the box on non-mac', async () => {
    await renderPanel()
    // The mac-only promise must be absent: this platform's default is a chord,
    // and the key is not even present on the keyboard.
    await waitFor(() => {
      expect(screen.queryByText(/Right Option/i)).toBeNull()
    })
    // And the copy names what actually works here, matching the test strip.
    expect(screen.getByText(/Alt \+ Shift \+ Space works out of the box/i)).toBeTruthy()
  })

  it('does not offer Option as an example key on non-mac', async () => {
    await renderPanel()
    // "a key like Option, Control or Shift" named a key these keyboards lack.
    expect(screen.queryByText(/a key like Option/i)).toBeNull()
  })
})
