/**
 * The artifacts page's remote-provider heading and an imported card's author
 * line are one catalog string each, with the name interpolated.
 *
 * Both were a bare word glued to a name (`{on} {provider}`, `{by} {author}`). The
 * word alone could not say which sense it meant, so six languages translated
 * "On" as the on/off switch ("Ein", "Activado", "켜짐") and the heading read
 * "switched on Quip". One key per sentence lets each language word and order
 * the whole phrase.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { i18next } from '../i18n/all'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import en from '../i18n/locales/en.json'
import de from '../i18n/locales/de.json'
import es from '../i18n/locales/es.json'
import ko from '../i18n/locales/ko.json'
import type { PublishProviderDescriptor, RemoteArtifact } from '../types'

vi.mock('../api/client')

vi.mock('@virtuoso.dev/masonry', () => ({
  VirtuosoMasonry: () => null,
}))

const provider: PublishProviderDescriptor = {
  name: 'companion',
  display_name: 'Companion Provider',
  capabilities: ['content_versions', 'sharing'],
  kind_support: 'native',
  capable: true,
  sharing_model: {
    supports_private: true,
    supports_shared: true,
    supports_public: true,
    principal_kind: 'user',
    supports_roles: false,
    supports_expiration: false,
    programmable: true,
    out_of_band_url: '',
  },
  sync_model: { authority: 'mirror', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: true,
    list_shared_with_me: true,
    list_public: true,
    full_text_search: false,
    pull_by_id: true,
  },
}

const remote: RemoteArtifact = {
  external_id: 'ext-1',
  title: 'Remote ext-1',
  owner: 'someone',
  view_url: 'https://remote.example.com/a/ext-1',
  updated_at: '2026-07-01T00:00:00Z',
  snippet: '',
  tags: [],
  local_slug: null,
}

// The heading appears only after the providers query and then the browse query
// settle, one after the other; under a loaded coverage runner that chain can
// outlast waitFor's one-second default, so each wait names its own ceiling.
const CHAINED_QUERY_TIMEOUT_MS = 5_000

describe('artifacts page glued labels', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({ providers: [provider], kind: 'widget' })
    vi.mocked(api).browseRemoteArtifacts = vi.fn().mockResolvedValue({ artifacts: [remote] })
  })
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('reads the remote heading as located on the provider in Spanish', async () => {
    await i18next.changeLanguage('es')
    renderWithProviders(<ArtifactsPage />)
    await waitFor(
      () => expect(screen.getByText('En Companion Provider')).toBeInTheDocument(),
      { timeout: CHAINED_QUERY_TIMEOUT_MS },
    )
    expect(screen.queryByText(/Activado/)).not.toBeInTheDocument()
  })

  it('keeps the English remote heading', async () => {
    renderWithProviders(<ArtifactsPage />)
    await waitFor(
      () => expect(screen.getByText('On Companion Provider')).toBeInTheDocument(),
      { timeout: CHAINED_QUERY_TIMEOUT_MS },
    )
  })

  it('carries the name inside each sentence, not after a bare word', () => {
    for (const cat of [en, de, es, ko]) {
      const page = cat.pages.artifactsPage as Record<string, string>
      expect(page.on_provider).toContain('{{provider}}')
      expect(page.by_author).toContain('{{author}}')
      expect(page.on).toBeUndefined()
      expect(page.by).toBeUndefined()
    }
    expect((de.pages.artifactsPage as Record<string, string>).on_provider).toBe('Auf {{provider}}')
    expect((ko.pages.artifactsPage as Record<string, string>).on_provider).toBe('{{provider}}에서')
  })

  // The same bare "by" glued before a name, on the skill browser's author line
  // and on an Issue Radar linked ref's actor.
  it('names the skill author and the ref actor inside one sentence too', () => {
    for (const cat of [en, de, es, ko]) {
      const skill = cat.components.skillBrowserModal as Record<string, string>
      const ref = cat.apps.issueRadar.components.issueDetail as Record<string, string>
      expect(skill.by_author).toContain('{{author}}')
      expect(ref.by_actor).toContain('{{actor}}')
      expect(skill.by).toBeUndefined()
      expect(ref.by).toBeUndefined()
    }
    expect((de.components.skillBrowserModal as Record<string, string>).by_author).toBe('Von {{author}}')
  })
})
