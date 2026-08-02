import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { LanguageProvider, useLanguage } from './LanguageProvider'
import { LANG_STORAGE_KEY } from './detect'
import { i18nT } from './t'
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
    // `ja-JP` is deliberately a language we do NOT ship. Using a shippable tag
    // here silently inverts the test the moment that language lands — this was
    // originally `fr-FR`, which stopped exercising the fallback once French
    // shipped.
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['ja-JP'])
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
   * Regression caught by driving the real dashboard: a browser with an explicit
   * `mc-lang` came back English because the boot payload's `language: ''` (the
   * workspace default) was adopted over it. `''` means "nothing recorded", not
   * "the workspace chose Auto", so it must not clobber the more specific local
   * signal — otherwise a user whose PUT never landed is reset on every load and
   * can never make the choice stick.
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

describe('LanguageProvider — cross-tab switch survives a delayed boot response', () => {
  /**
   * Regression: the `onStorage` handler (cross-tab sync) set `language` without
   * setting `userChose`, so a slow `/api/theme/boot` response arriving after a
   * cross-tab switch could overwrite it. The fix sets `userChose.current = true`
   * in the storage handler, blocking boot adoption the same way a local pick does.
   */
  it('a cross-tab switch is not reverted by a delayed boot response', async () => {
    // Simulate: tab A set localStorage to zh-CN (cross-tab switch), then boot
    // responds with language: 'en'.
    //
    // We control the boot response timing by making themeBoot a deferred promise.
    let resolveBoot!: (value: unknown) => void
    const bootPromise = new Promise(resolve => { resolveBoot = resolve })
    vi.spyOn(api, 'themeBoot').mockReturnValue(bootPromise as never)
    vi.spyOn(api, 'updateThemeConfig').mockResolvedValue({} as never)

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <LanguageProvider><Probe /></LanguageProvider>
      </QueryClientProvider>,
    )

    // Before boot resolves, simulate a cross-tab language change via StorageEvent
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: LANG_STORAGE_KEY,
        newValue: 'zh-CN',
        storageArea: localStorage,
      }))
    })

    await waitFor(() => expect(screen.getByTestId('choice')).toHaveTextContent('zh-CN'))

    // NOW let boot resolve with a DIFFERENT language. Wait until the boot query
    // has actually COMMITTED its data: waitFor wraps each poll in act(), so once
    // the cache holds the response the adopt effect it schedules has run to
    // completion (including any cascading setState). Only then is the final
    // assertion race-free - a plain expect right after resolveBoot would observe
    // the still-true initial value before adoption had a chance to revert it.
    resolveBoot({ language: 'en' })
    await waitFor(() => expect(qc.getQueryData(['theme-boot'])).toEqual({ language: 'en' }))

    // The adopt effect has now run. With the fix it was blocked by `userChose`;
    // without it, the delayed boot reverts the cross-tab choice to 'en'.
    expect(screen.getByTestId('choice')).toHaveTextContent('zh-CN')
  })

  it('a delayed boot IS adopted when no cross-tab switch preceded it', async () => {
    // Control proving the deferred-boot adoption path is live and observable:
    // with no prior explicit choice, the same delayed 'en' boot response IS
    // adopted. This is the exact code path the test above must suppress, so if
    // adoption silently stopped working this control fails instead.
    let resolveBoot!: (value: unknown) => void
    const bootPromise = new Promise(resolve => { resolveBoot = resolve })
    vi.spyOn(api, 'themeBoot').mockReturnValue(bootPromise as never)
    vi.spyOn(api, 'updateThemeConfig').mockResolvedValue({} as never)

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <LanguageProvider><Probe /></LanguageProvider>
      </QueryClientProvider>,
    )

    await act(async () => {
      resolveBoot({ language: 'en' })
      await bootPromise
    })
    await waitFor(() => expect(screen.getByTestId('choice')).toHaveTextContent('en'))
  })
})

describe('LanguageProvider — memoized components repaint on language switch', () => {
  /**
   * Guards that the `useI18nRevision()` hook correctly threads the language
   * through memo boundaries. A memo'd component using i18nT should display
   * translated text after a language switch, not stale English.
   */
  it('a memo-wrapped component repaints when language changes', async () => {
    const { memo: reactMemo } = await import('react')
    const { useI18nRevision: rev } = await import('./useI18nRevision')

    const MemoChild = reactMemo(function MemoChild({ id }: { id: number }) {
      rev()
      return <span data-testid="memo-text">{i18nT('pages.settings.displayPanel.view')}{id}</span>
    })

    function Parent() {
      const { setLanguage } = useLanguage()
      return (
        <div>
          <MemoChild id={1} />
          <button onClick={() => setLanguage('zh-CN')}>switch</button>
        </div>
      )
    }

    vi.spyOn(api, 'themeBoot').mockResolvedValue({} as never)
    vi.spyOn(api, 'updateThemeConfig').mockResolvedValue({} as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <LanguageProvider><Parent /></LanguageProvider>
      </QueryClientProvider>,
    )

    await waitFor(() => expect(screen.getByTestId('memo-text')).toHaveTextContent('View'))
    await userEvent.click(screen.getByText('switch'))
    await waitFor(() => {
      expect(screen.getByTestId('memo-text').textContent).not.toContain('View')
    })
  })

  it('a pre-set locale in storage renders translated on first paint', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')

    const { memo: reactMemo } = await import('react')
    const { useI18nRevision: rev } = await import('./useI18nRevision')

    const MemoChild = reactMemo(function MemoChild({ id }: { id: number }) {
      rev()
      return <span data-testid="memo-text">{i18nT('pages.settings.displayPanel.view')}{id}</span>
    })

    function Parent() {
      return <MemoChild id={1} />
    }

    vi.spyOn(api, 'themeBoot').mockResolvedValue({ language: 'zh-CN' } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <LanguageProvider><Parent /></LanguageProvider>
      </QueryClientProvider>,
    )

    // Should render Chinese on first paint, no English flash
    await waitFor(() => {
      expect(screen.getByTestId('memo-text').textContent).toMatch(/[一-鿿]/)
    })
  })
})
