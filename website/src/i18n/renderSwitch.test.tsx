/**
 * End-to-end rendering guard: does a language change actually repaint the UI?
 *
 * This is the test that catches the failure the rest of the suite cannot. Every
 * other i18n test asserts catalog SHAPE (parity, placeholders, key sets) or
 * provider STATE (what's stored, what's persisted). None of them prove the thing
 * the user actually experiences: that picking a language swaps the visible text.
 *
 * It caught a real defect. `i18next.changeLanguage()` is async, so keying the
 * remount on the *desired* language remounted the tree BEFORE the catalog
 * swapped — the tree rendered the old strings and, since standalone `i18nT()`
 * subscribes to nothing, never re-rendered. Settings appeared to do nothing.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useEffect, useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { LanguageProvider, useLanguage } from './LanguageProvider'
import { i18nT } from './t'
import { LANG_STORAGE_KEY } from './detect'
import { api } from '../api/client'

/** Renders through the standalone `i18nT` — the path 250 converted files use. */
function Sample() {
  const { setLanguage } = useLanguage()
  return (
    <div>
      <span data-testid="text">{i18nT('pages.settings.displayPanel.view')}</span>
      <button onClick={() => setLanguage('zh-CN')}>zh</button>
      <button onClick={() => setLanguage('en')}>en</button>
    </div>
  )
}

function mount() {
  vi.spyOn(api, 'themeBoot').mockResolvedValue({} as never)
  vi.spyOn(api, 'updateThemeConfig').mockResolvedValue({} as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LanguageProvider><Sample /></LanguageProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['en-US'])
})

describe('language switch re-renders the UI', () => {
  it('renders English by default', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('text')).toHaveTextContent('View'))
  })

  it('renders the translated string when the stored language is zh-CN', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    mount()
    await waitFor(() => expect(screen.getByTestId('text').textContent).toMatch(/[一-鿿]/))
  })

  it('repaints existing components when the language is switched at runtime', async () => {
    mount()
    await waitFor(() => expect(screen.getByTestId('text')).toHaveTextContent('View'))
    await userEvent.click(screen.getByText('zh'))
    // The actual regression: without the languageChanged-keyed remount this
    // stays 'View' forever.
    await waitFor(() => expect(screen.getByTestId('text').textContent).toMatch(/[一-鿿]/))
  })

  it('switches back to English', async () => {
    localStorage.setItem(LANG_STORAGE_KEY, 'zh-CN')
    mount()
    await waitFor(() => expect(screen.getByTestId('text').textContent).toMatch(/[一-鿿]/))
    await userEvent.click(screen.getByText('en'))
    await waitFor(() => expect(screen.getByTestId('text')).toHaveTextContent('View'))
  })
})

describe('a language switch preserves component state', () => {
  /**
   * The load-bearing property of the `cloneElement` repaint. A `key`-based
   * remount also repaints, but destroys state — and the theme-install URL input
   * lives in the SAME Settings panel as the language picker, so switching
   * language is precisely when a user would lose what they typed. This test is
   * what distinguishes the two implementations; without it, a future refactor
   * back to `key={active}` would look correct (strings still switch) while
   * silently reintroducing the data loss.
   */
  it('keeps local component state across a language change', async () => {
    function Draft() {
      const [v, setV] = useState('')
      const { setLanguage } = useLanguage()
      return (
        <div>
          <input data-testid="draft" value={v} onChange={e => setV(e.target.value)} />
          <span data-testid="text">{i18nT('pages.settings.displayPanel.view')}</span>
          <button onClick={() => setLanguage('zh-CN')}>zh</button>
        </div>
      )
    }
    vi.spyOn(api, 'themeBoot').mockResolvedValue({ language: '' } as never)
    vi.spyOn(api, 'updateThemeConfig').mockResolvedValue({} as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <LanguageProvider><Draft /></LanguageProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('text')).toHaveTextContent('View'))
    await userEvent.type(screen.getByTestId('draft'), 'unsaved work')
    expect(screen.getByTestId('draft')).toHaveValue('unsaved work')

    await userEvent.click(screen.getByText('zh'))
    await waitFor(() => expect(screen.getByTestId('text').textContent).toMatch(/[一-鿿]/))
    // The strings switched AND the unsaved input survived.
    expect(screen.getByTestId('draft')).toHaveValue('unsaved work')
  })

  it('does not remount the subtree on a language change', async () => {
    // Mount count is the direct signal: a key-based implementation increments it.
    let mounts = 0
    function Counted() {
      useEffect(() => { mounts++ }, [])
      return <span data-testid="text">{i18nT('pages.settings.displayPanel.view')}</span>
    }
    vi.spyOn(api, 'themeBoot').mockResolvedValue({ language: '' } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <LanguageProvider><Counted /></LanguageProvider>
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByTestId('text')).toHaveTextContent('View'))
    expect(mounts).toBe(1)
  })
})
