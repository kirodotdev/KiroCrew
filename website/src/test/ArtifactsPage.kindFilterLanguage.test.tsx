/**
 * The Artifacts page's kind filter labels each option "kind: html", beside the
 * tag filter's "tag: ops". The tag prefix comes from the catalog; the kind
 * prefix was an English literal, so under German the two dropdowns read
 * "kind: markdown" next to "Tag: ops".
 */

import { describe, it, expect, vi, afterAll } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
// `/all` for the non-English catalogs: `../i18n` registers English only.
import { i18next } from '../i18n/all'

vi.mock('../api/client')

afterAll(async () => {
  await i18next.changeLanguage('en')
})

describe('ArtifactsPage kind filter — option labels follow the active language', () => {
  it('names a kind option with the German word for kind', async () => {
    await act(async () => {
      await i18next.changeLanguage('de')
    })
    vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
    renderWithProviders(<ArtifactsPage />)
    const trigger = await screen.findByRole('combobox', { name: 'Nach Art filtern' })
    await waitFor(() => expect(trigger).toHaveTextContent('alle Arten'))
    fireEvent.click(trigger)
    expect(await screen.findByRole('option', { name: 'Art: markdown' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'kind: markdown' })).toBeNull()
  })
})
