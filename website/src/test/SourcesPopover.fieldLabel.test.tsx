import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { i18next } from '../i18n/all'
import zh from '../i18n/locales/zh-CN.json'

// The Install-from-Path box has an accessible name. Its placeholder is a
// sample path, which is not a name and vanishes once text is typed.

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
}))
vi.mock('../rum', () => ({ recordEvent: vi.fn() }))
vi.mock('../components/RegistryManager', () => ({ default: () => <div /> }))

import SourcesPopover from '../components/appstore/SourcesPopover'

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

describe('SourcesPopover install path name', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('names the install path box', () => {
    wrap(<SourcesPopover open onOpenChange={() => {}} onError={() => {}} />)
    expect(screen.getByRole('textbox', { name: 'App directory path' })).toBeInTheDocument()
  })

  it('takes the name from the catalog in another language', async () => {
    await i18next.changeLanguage('zh-CN')
    wrap(<SourcesPopover open onOpenChange={() => {}} onError={() => {}} />)
    expect(screen.getByRole('textbox', { name: zh.components.appstore.sourcesPopover.app_directory_path_label })).toBeInTheDocument()
  })
})
