/**
 * The Artifacts page publication badge names where an artifact was published.
 *
 * The card (ArtifactsPageGallery) and the table row (LibraryTable) share one
 * label: "Published to <provider> (<visibility>)" when the provider list the page
 * already loads names the publication's provider, and "Published (<visibility>)"
 * when it does not. Sync issues and notices keep their own wording.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { i18next } from '../i18n/all'
import { publicationBadgeLabel } from '../lib/publicationBadge'
import { LibraryMasonry, type GridEntry } from '../pages/ArtifactsPageGallery'
import { ArtifactRow } from '../components/library/LibraryTable'
import type { Artifact, ArtifactPublication } from '../types'

vi.mock('../api/client')

function pub(over: Partial<ArtifactPublication> = {}): ArtifactPublication {
  return {
    provider: 'acme_drive',
    visibility: 'PRIVATE',
    shared_with: [],
    auto_sync: false,
    last_synced_kirocrew_version: 1,
    version_map: {},
    published_at: '2026-11-07T00:00:00Z',
    published_by: 'me',
    last_error: '',
    notice: '',
    ...over,
  } as ArtifactPublication
}

const art = {
  slug: 'weekly',
  name: 'weekly',
  kind: 'markdown',
  version: 1,
  updated_at: '2026-01-01T00:00:00.000Z',
  created_at: '2026-01-01T00:00:00.000Z',
  tags: [],
  description: '',
  source: 'chat',
  pinned: false,
  publication: pub(),
} as unknown as Artifact

function providers(list: { name: string; display_name: string }[]) {
  vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({ kind: 'widget', providers: list })
}

function renderCard(a: Artifact = art) {
  const entries: GridEntry[] = [{ kind: 'local', key: a.slug, art: a }]
  return renderWithProviders(
    <LibraryMasonry
      entries={entries}
      cols={1}
      widthRef={() => {}}
      scrollerRef={{ current: null }}
      fillPage={false}
      onOpen={vi.fn()}
      onDelete={vi.fn()}
      deletingSlug={null}
      onTogglePin={vi.fn()}
      pinningSlug={null}
    />,
  )
}

function renderRow(a: Artifact = art) {
  return renderWithProviders(
    <table><tbody><ArtifactRow a={a} onOpen={vi.fn()} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).artifact = vi.fn().mockResolvedValue({ ...art, content: '# hi' })
})

describe('publication badge label', () => {
  afterEach(async () => { await i18next.changeLanguage('en') })

  it('names the provider and the visibility', () => {
    expect(publicationBadgeLabel(pub(), 'Acme Drive')).toBe('Published to Acme Drive (private)')
  })

  it('keeps today\'s text when no provider name is known', () => {
    expect(publicationBadgeLabel(pub())).toBe('Published (private)')
  })

  it('treats a blank name as no name', () => {
    expect(publicationBadgeLabel(pub(), '   ')).toBe('Published (private)')
  })

  it('keeps the sync-issue wording over the provider name', () => {
    expect(publicationBadgeLabel(pub({ last_error: 'boom' }), 'Acme Drive')).toBe('Published (sync issue)')
  })

  it('keeps the notice wording over the provider name', () => {
    expect(publicationBadgeLabel(pub({ notice: 'x', notice_code: 'rolling_out' }), 'Acme Drive'))
      .toBe('Published — still rolling out')
  })

  it('uses the same wording as the detail page in each language', async () => {
    await i18next.changeLanguage('de')
    expect(publicationBadgeLabel(pub(), 'Acme Drive')).toBe('Veröffentlicht in Acme Drive (private)')
  })
})

describe('publication badge on the card and the row', () => {
  it('the card names the provider from the provider list', async () => {
    providers([{ name: 'acme_drive', display_name: 'Acme Drive' }])
    renderCard()
    expect(await screen.findByLabelText('Published to Acme Drive (private)')).toBeTruthy()
    expect(api.getArtifactPublishProviders).toHaveBeenCalledWith('widget')
  })

  it('the row names the provider from the provider list', async () => {
    providers([{ name: 'acme_drive', display_name: 'Acme Drive' }])
    renderRow()
    expect(await screen.findByLabelText('Published to Acme Drive (private)')).toBeTruthy()
  })

  it('an unlisted provider keeps today\'s text', async () => {
    providers([{ name: 'other', display_name: 'Other' }])
    renderRow()
    await waitFor(() => expect(api.getArtifactPublishProviders).toHaveBeenCalled())
    expect(screen.getByLabelText('Published (private)')).toBeTruthy()
  })

  it('an unpublished artifact asks for no provider list', () => {
    providers([{ name: 'acme_drive', display_name: 'Acme Drive' }])
    renderRow({ ...art, publication: null } as unknown as Artifact)
    expect(api.getArtifactPublishProviders).not.toHaveBeenCalled()
  })

  it('the card badge sits above the card\'s open button, so its tooltip gets the hover', async () => {
    providers([{ name: 'acme_drive', display_name: 'Acme Drive' }])
    renderCard()
    const icon = await screen.findByLabelText('Published to Acme Drive (private)')
    expect(icon.querySelector('title')?.textContent).toBe('Published to Acme Drive (private)')
    const badge = screen.getByTestId('publication-badge')
    expect(badge.contains(icon)).toBe(true)
    expect(badge.className).toMatch(/\brelative\b/)
    expect(badge.className).toMatch(/\bz-10\b/)
    expect(badge.closest('button')).toBeNull()
  })
})
