/**
 * The Artifacts page publication badge names where an artifact was published.
 *
 * The card (ArtifactsPageGallery) and the table row (LibraryTable) share one
 * label: "Published to <provider> (<visibility>)" when the provider list the page
 * already loads names the publication's provider, and "Published (<visibility>)"
 * when it does not. Sync issues and notices keep their own wording.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
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

function renderCard(a: Artifact = art, onOpen = vi.fn()) {
  const entries: GridEntry[] = [{ kind: 'local', key: a.slug, art: a }]
  return renderWithProviders(
    <LibraryMasonry
      entries={entries}
      cols={1}
      widthRef={() => {}}
      scrollerRef={{ current: null }}
      fillPage={false}
      onOpen={onOpen}
      onDelete={vi.fn()}
      deletingSlug={null}
      onTogglePin={vi.fn()}
      pinningSlug={null}
    />,
  )
}

function renderRow(a: Artifact = art, onOpen = vi.fn()) {
  return renderWithProviders(
    <table><tbody><ArtifactRow a={a} onOpen={onOpen} onDelete={vi.fn()} deletingSlug={null} onTogglePin={vi.fn()} /></tbody></table>,
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

  it('a click on the card badge still opens the artifact', async () => {
    providers([{ name: 'acme_drive', display_name: 'Acme Drive' }])
    const onOpen = vi.fn()
    renderCard(art, onOpen)
    await screen.findByLabelText('Published to Acme Drive (private)')
    fireEvent.click(screen.getByTestId('publication-badge'))
    expect(onOpen).toHaveBeenCalledWith('weekly')
  })

  it('the card badge is a keyboard control named like the open button and described by its label', async () => {
    providers([{ name: 'acme_drive', display_name: 'Acme Drive' }])
    const onOpen = vi.fn()
    renderCard(art, onOpen)
    await screen.findByLabelText('Published to Acme Drive (private)')
    const badge = screen.getByTestId('publication-badge')
    expect(badge.getAttribute('role')).toBe('button')
    expect(badge.tabIndex).toBe(0)
    expect(badge.getAttribute('aria-label')).toBe('weekly')
    const described = document.getElementById(badge.getAttribute('aria-describedby') || '')
    expect(described?.getAttribute('aria-label')).toBe('Published to Acme Drive (private)')
    fireEvent.keyDown(badge, { key: 'Enter' })
    expect(onOpen).toHaveBeenCalledWith('weekly')
  })
})

describe('a publication sync error is shown through ErrorNotice', () => {
  const failed = { ...art, publication: pub({ last_error: 'destination conflict' }) } as unknown as Artifact

  it('the card shows the error text with the agent hand-off, and a click on it does not open the card', async () => {
    providers([])
    const onOpen = vi.fn()
    renderCard(failed, onOpen)
    const notice = await screen.findByTestId('publication-sync-error')
    expect(notice.textContent).toContain('destination conflict')
    expect(notice.textContent).toContain('Published (sync issue)')
    const wrapper = notice.parentElement as HTMLElement
    expect(wrapper.className).toMatch(/\bz-10\b/)
    fireEvent.click(notice)
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('the row shows the error text, and a click on it does not open the row', async () => {
    providers([])
    const onOpen = vi.fn()
    renderRow(failed, onOpen)
    const notice = await screen.findByTestId('publication-sync-error')
    expect(notice.textContent).toContain('destination conflict')
    fireEvent.click(notice)
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('no error, no notice', async () => {
    providers([])
    renderRow()
    await waitFor(() => expect(api.getArtifactPublishProviders).toHaveBeenCalled())
    expect(screen.queryByTestId('publication-sync-error')).toBeNull()
  })
})
