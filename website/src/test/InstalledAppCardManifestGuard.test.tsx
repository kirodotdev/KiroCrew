/**
 * Regression for issue #3689: a manifest that declares `ui.entry` but no
 * `ui.pages` array made the page-count badge dereference
 * `m.ui!.pages!.length` and throw during render, which unmounted the whole
 * /apps route. The badge must render from an explicitly guarded `pageCount`
 * and simply stay hidden when `pages` is absent, while `hasUI` (and the Open
 * button it gates) stays true for an entry-only app.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { InstalledApp } from '../components/appstore/types'
import { i18nT } from '../i18n/t'

const mocks = vi.hoisted(() => ({ openApp: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))
vi.mock('../components/AppIcon', () => ({
  default: () => <div data-testid="zzq-app-icon" />,
}))

import InstalledAppCard from '../components/appstore/InstalledAppCard'

const T = (k: string, vars?: Record<string, unknown>) => i18nT(`components.appstore.installedAppCard.${k}`, vars)

function app(manifest: Partial<InstalledApp['manifest']> = {}): InstalledApp {
  return {
    name: 'zzq-entry-only',
    version: '1.0.0',
    displayName: 'Zzq Entry Only',
    enabled: true,
    installedAt: '2026-08-02T00:00:00Z',
    origin: 'registry',
    lifecycle: 'gateway',
    manifest: {
      name: 'zzq-entry-only',
      version: '1.0.0',
      displayName: 'Zzq Entry Only',
      description: 'zzq description',
      author: 'zzq-author',
      ...manifest,
    },
  } as InstalledApp
}

function renderCard(a: InstalledApp) {
  return render(
    <InstalledAppCard
      app={a}
      actionLoading={null}
      onAction={vi.fn()}
      onOpen={vi.fn()}
      onDetail={vi.fn()}
    />,
  )
}

afterEach(() => vi.clearAllMocks())

describe('InstalledAppCard manifest guard (#3689)', () => {
  it('renders an entry-only UI manifest (ui.entry, no ui.pages) without throwing', () => {
    // Before the fix this render threw
    // "TypeError: Cannot read properties of undefined (reading 'length')".
    expect(() => renderCard(app({ ui: { entry: 'index.html' } }))).not.toThrow()
    // hasUI is still true for entry-only apps, so Open stays available…
    expect(screen.getByText(T('open'))).toBeInTheDocument()
    // …but the page-count badge is hidden rather than crashing.
    expect(screen.queryByText(T('page', { count: 0 }))).not.toBeInTheDocument()
  })

  it('still shows the page-count badge when ui.pages is present', () => {
    renderCard(app({ ui: { entry: 'index.html', pages: [{ route: '/apps/zzq', label: 'Zzq' }] } }))
    expect(screen.getByText(T('page', { count: 1 }))).toBeInTheDocument()
  })

  it('renders a manifest with a ui object but neither entry nor pages', () => {
    expect(() => renderCard(app({ ui: {} as InstalledApp['manifest']['ui'] }))).not.toThrow()
    expect(screen.queryByText(T('open'))).not.toBeInTheDocument()
  })
})
