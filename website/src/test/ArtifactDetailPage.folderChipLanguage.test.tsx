/**
 * The artifact page's folder chip shows the folder's name, or a placeholder
 * word when the artifact is in no folder. That placeholder was the English
 * literal 'folder', so under every other language the toolbar read "folder"
 * beside translated controls, while the chip's own tooltip and name were
 * translated.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
// `/all` for the non-English catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const mkArtifact = (): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# Doc body',
})

describe('artifact page folder chip — the unfiled placeholder follows the language', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).listSlots = vi.fn().mockResolvedValue([])
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
  })

  afterEach(async () => {
    await i18next.changeLanguage('en')
  })

  it('shows the German word on an unfiled artifact', async () => {
    await act(async () => {
      await i18next.changeLanguage('de')
    })
    renderWithProviders(
      <Routes>
        <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      </Routes>,
      { route: '/artifacts/cr-queue' },
    )
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const chip = screen.getByRole('button', { name: 'In Ordner verschieben' })
    expect(chip).toHaveTextContent('Ordner')
    expect(chip.textContent).not.toMatch(/folder/)
  })
})
