import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { LanguageProvider, useLanguage } from './LanguageProvider'
import { LANG_STORAGE_KEY } from './detect'
import { api } from '../api/client'

/** Fresh QueryClient per test so the ['theme-boot'] cache never leaks across cases. */
function wrap(children: ReactNode, boot: { language?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(api, 'themeBoot').mockResolvedValue(boot as never)
  return render(
    <QueryClientProvider client={qc}>
      <LanguageProvider>{children}</LanguageProvider>
    </QueryClientProvider>,
  )
}

/** Surfaces the context so assertions can read it. */
function Probe() {
  const { language, resolved, detected, setLanguage, syncFailed } = useLanguage()
  return (
    <div>
      <span data-testid="choice">{language || '(auto)'}</span>
      <span data-testid="resolved">{resolved}</span>
      <span data-testid="detected">{detected}</span>
      <span data-testid="sync">{syncFailed ? 'failed' : 'ok'}</span>
      <button onClick={() => setLanguage('zh-CN')}>to-zh</button>
      <button onClick={() => setLanguage('')}>to-auto</button>
    </div>
  )
}

let patch: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  localStorage.clear()
  patch = vi.spyOn(api, 'updateThemeConfig').mockResolvedValue({} as never)
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['en-US'])
})

afterEach(() => {
  vi.restoreAllMocks()
  document.documentElement.removeAttribute('lang')
})

describe('LanguageProvider', () => {
  it('defaults to browser detection when nothing is stored', async () => {
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['zh-CN', 'en'])
    wrap(<Probe />)
    expect(screen.getByTestId('choice')).toHaveTextContent('(auto)')
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
  })

  it('honours a stored explicit choice over the browser', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'en')
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['zh-CN'])
    wrap(<Probe />)
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('en'))
  })

  it('reports what Auto would give independently of the explicit choice', async () => {
    // The Settings picker annotates its Auto row with this. It must describe the
    // BROWSER, so an explicit English choice on a zh-CN browser still reads
    // "Auto — 简体中文" rather than echoing "— English".
    localStorage.setItem(LANG_STORAGE_KEY, 'en')
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['zh-CN'])
    wrap(<Probe />)
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('en'))
    expect(screen.getByTestId('detected')).toHaveTextContent('zh-CN')

    // …and switching the choice must not move it.
    await userEvent.click(screen.getByText('to-zh'))
    expect(screen.getByTestId('detected')).toHaveTextContent('zh-CN')
  })

  it('falls back to the default language when the browser matches nothing', async () => {
    // Klingon is deliberately not a product locale. A plausible future language
    // would silently invert this test when its catalog lands.
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['tlh-US'])
    wrap(<Probe />)
    await waitFor(() => expect(screen.getByTestId('detected')).toHaveTextContent('en'))
  })

  it('persists a new choice to config and localStorage', async () => {
    wrap(<Probe />)
    await userEvent.click(screen.getByText('to-zh'))
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
    expect(localStorage.getItem(LANG_STORAGE_KEY)).toBe('zh-CN')
    expect(patch).toHaveBeenCalledWith({ language: 'zh-CN' })
  })

  it('writes the auto sentinel when returning to Auto', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    wrap(<Probe />)
    await userEvent.click(screen.getByText('to-auto'))
    // '' must be transmitted so the server-side choice is actually CLEARED —
    // omitting the field would leave the old language pinned in config.
    expect(patch).toHaveBeenCalledWith({ language: '' })
    expect(screen.getByTestId('choice')).toHaveTextContent('(auto)')
  })

  it('adopts the server value when it differs from the local cache', async () => {
    // Simulates the user having picked Chinese in another browser.
    wrap(<Probe />, { language: 'zh-CN' })
    await waitFor(() => expect(screen.getByTestId('choice')).toHaveTextContent('zh-CN'))
    expect(localStorage.getItem(LANG_STORAGE_KEY)).toBe('zh-CN')
  })

  it('does not echo the adopted server value back to the server', async () => {
    // The read path must not write, or two tabs race into a write loop.
    wrap(<Probe />, { language: 'zh-CN' })
    await waitFor(() => expect(screen.getByTestId('choice')).toHaveTextContent('zh-CN'))
    expect(patch).not.toHaveBeenCalled()
  })

  it('keeps the UI switched when the config write fails', async () => {
    patch.mockRejectedValue(new Error('offline'))
    wrap(<Probe />)
    await userEvent.click(screen.getByText('to-zh'))
    // Local state + cache are authoritative for rendering; a failed sync must
    // not strand the user on the old language.
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
    expect(localStorage.getItem(LANG_STORAGE_KEY)).toBe('zh-CN')
  })

  it('sets <html lang> to the resolved language', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    wrap(<Probe />)
    await waitFor(() => expect(document.documentElement.lang).toBe('zh-CN'))
  })

  // A regional tag must land on the bare code, or the locale-specific font
  // override keyed on it — `html:lang(ja)`, `html:lang(ko)` — never matches and the
  // script silently renders through the Simplified Chinese alias.
  it.each([['ja-JP', 'ja'], ['ko-KR', 'ko']])(
    'normalizes the %s browser tag for the locale-specific font override',
    async (tag, expected) => {
      vi.spyOn(navigator, 'languages', 'get').mockReturnValue([tag])
      wrap(<Probe />)
      await waitFor(() => expect(document.documentElement.lang).toBe(expected))
    },
  )

  it('is inert but does not crash outside a provider', () => {
    // An isolated component test shouldn't have to mount the provider.
    render(<Probe />)
    expect(screen.getByTestId('resolved')).toHaveTextContent('en')
  })
})

describe('LanguageProvider — server value must not fight the user', () => {
  /**
   * Regression: `staleTime: Infinity` means `bootData.language` keeps returning
   * the value fetched at mount. An adopt-effect that re-ran on every local change
   * therefore re-asserted the stale server value the instant the user picked a
   * language, making the picker a silent no-op. These use a REALISTIC boot payload
   * — the real `/api/theme/boot` always includes `language` (see `_theme_payload`)
   * — which is what the earlier tests, passing `{}`, failed to exercise.
   */
  it('lets the user switch away from the server value (fresh install, language "")', async () => {
    wrap(<Probe />, { language: '' })
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('en'))
    await userEvent.click(screen.getByText('to-zh'))
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
    expect(localStorage.getItem(LANG_STORAGE_KEY)).toBe('zh-CN')
  })

  it('lets the user switch away from an explicit server value ("en")', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'en')
    wrap(<Probe />, { language: 'en' })
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('en'))
    await userEvent.click(screen.getByText('to-zh'))
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
  })

  it('adopts the server value only once, so a later local pick sticks', async () => {
    wrap(<Probe />, { language: 'zh-CN' })
    await waitFor(() => expect(screen.getByTestId('choice')).toHaveTextContent('zh-CN'))
    await userEvent.click(screen.getByText('to-auto'))
    // Must settle on Auto, not snap back to the server's zh-CN.
    await waitFor(() => expect(screen.getByTestId('choice')).toHaveTextContent('(auto)'))
    expect(patch).toHaveBeenCalledWith({ language: '' })
  })
})

describe('LanguageProvider — a failed config write is reported', () => {
  /**
   * Why this matters: the switch is optimistic and writes localStorage, so a user
   * whose PUT failed believes it stuck — but the next load adopts the server's
   * unchanged value and overwrites the local mirror, silently reverting them.
   * There is no retry, so nothing reconciles it. Reporting the failure is what
   * makes it recoverable instead of a mystery.
   */
  it('exposes syncFailed when the config write rejects', async () => {
    patch.mockRejectedValue(new Error('offline'))
    wrap(<Probe />, { language: '' })
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('en'))
    await userEvent.click(screen.getByText('to-zh'))
    // The UI still switches — the failure must not block it.
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
    await waitFor(() => expect(screen.getByTestId('sync')).toHaveTextContent('failed'))
  })

  it('stays clean when the write succeeds', async () => {
    wrap(<Probe />, { language: '' })
    await userEvent.click(screen.getByText('to-zh'))
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
    expect(screen.getByTestId('sync')).toHaveTextContent('ok')
  })

  it('clears a previous failure on a later successful write', async () => {
    patch.mockRejectedValueOnce(new Error('offline'))
    wrap(<Probe />, { language: '' })
    await userEvent.click(screen.getByText('to-zh'))
    await waitFor(() => expect(screen.getByTestId('sync')).toHaveTextContent('failed'))
    await userEvent.click(screen.getByText('to-auto'))
    await waitFor(() => expect(screen.getByTestId('sync')).toHaveTextContent('ok'))
  })
})

describe('LanguageProvider — an empty server value must not erase a local choice', () => {
  /**
   * Regression: a browser with an explicit `mc-lang` came back English because
   * the boot payload's `language: ''` (the workspace default) was adopted over
   * it. `''` means "nothing recorded", not "the workspace chose Auto", so it
   * must not clobber the more specific local signal — otherwise a user whose PUT
   * never landed is reset on every load and can never make the choice stick.
   */
  it('keeps the local choice when the server reports no language', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    wrap(<Probe />, { language: '' })
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('zh-CN'))
    expect(screen.getByTestId('choice')).toHaveTextContent('zh-CN')
    expect(localStorage.getItem(LANG_STORAGE_KEY)).toBe('zh-CN')
  })

  it('still adopts a CONCRETE server language over the local choice', async () => {
    // Cross-browser sync must keep working — this is the case the latch exists for.
    localStorage.setItem(LANG_STORAGE_KEY, 'en')
    wrap(<Probe />, { language: 'zh-CN' })
    await waitFor(() => expect(screen.getByTestId('choice')).toHaveTextContent('zh-CN'))
  })

  it('leaves Auto alone when neither side recorded a choice', async () => {
    wrap(<Probe />, { language: '' })
    await waitFor(() => expect(screen.getByTestId('resolved')).toHaveTextContent('en'))
    expect(screen.getByTestId('choice')).toHaveTextContent('(auto)')
  })
})
