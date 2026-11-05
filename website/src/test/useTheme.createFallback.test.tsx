/**
 * `useTheme().addCustomTheme` — the text it throws when the create call fails.
 *
 * The theme editor shows this text in its error notice, so a server reply with
 * no `error` of its own must fall back to catalog text in the active language,
 * and a reply that names its error must pass that error through unchanged.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { i18next } from '../i18n/all'

const createThemeFn = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    themes: () => Promise.resolve({ themes: [] }),
    themeDetail: () => Promise.resolve({}),
    themeBoot: () => Promise.resolve({ mode: 'dark', color: 'default' }),
    updateThemeConfig: () => Promise.resolve({}),
    createTheme: (data: unknown) => createThemeFn(data),
    deleteTheme: () => Promise.resolve({ ok: true }),
  },
}))

import { ThemeProvider, useTheme } from '../hooks/useTheme'

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  )
}

const THEME = { name: 'Fresh', emoji: '✨', dark: {}, light: {} }

/** Run one create against the mocked reply and return the message it threw. */
async function createError(): Promise<string> {
  const { result } = renderHook(() => useTheme(), { wrapper })
  await waitFor(() => expect(result.current.customThemesLoaded).toBe(true))
  let message = ''
  await act(async () => {
    try { await result.current.addCustomTheme(THEME as never) } catch (e) { message = (e as Error).message }
  })
  return message
}

describe('useTheme: addCustomTheme failure text', () => {
  beforeEach(() => {
    localStorage.clear()
    window.matchMedia = vi.fn().mockReturnValue({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }) as unknown as typeof window.matchMedia
    createThemeFn.mockReset()
  })

  afterEach(async () => {
    await i18next.changeLanguage('en')
  })

  it('falls back to catalog text in the active language', async () => {
    await i18next.changeLanguage('de')
    createThemeFn.mockResolvedValue({ ok: false })

    expect(await createError()).toBe(i18next.t('components.themeEditor.failed_to_save_theme'))
    expect(i18next.t('components.themeEditor.failed_to_save_theme')).toBe('Theme konnte nicht gespeichert werden')
  })

  it('passes a server error through unchanged', async () => {
    createThemeFn.mockResolvedValue({ ok: false, error: 'Theme name already exists' })

    expect(await createError()).toBe('Theme name already exists')
  })
})
