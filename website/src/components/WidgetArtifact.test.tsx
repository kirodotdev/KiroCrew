/**
 * The widget title links to its artifact; the link's tooltip names the slug.
 * It was a hardcoded English template, so every language showed English.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { WidgetArtifactTitle, type WidgetArtifact } from './WidgetArtifact'
import { i18next } from '../i18n/all'

vi.mock('../api/client', () => ({ api: {}, ApiError: class extends Error {} }))

const artifact: WidgetArtifact = {
  effectiveSlug: 'zzq-weekly-board',
  existingSlug: 'zzq-weekly-board',
  savedSlug: null,
  saving: false,
  saveError: null,
  probeError: null,
  toggleArtifact: async () => {},
}

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('WidgetArtifactTitle link tooltip', () => {
  it.each([
    ['en', 'Open artifact \u201czzq-weekly-board\u201d'],
    ['de', 'Artefakt \u201ezzq-weekly-board\u201c öffnen'],
    ['ja', 'アーティファクト「zzq-weekly-board」を開く'],
  ])('names the artifact in %s', async (lng, title) => {
    await i18next.changeLanguage(lng)
    render(<WidgetArtifactTitle title="Weekly board" artifact={artifact} />)
    const link = screen.getByRole('link', { name: 'Weekly board' })
    expect(link).toHaveAttribute('href', '/artifacts/zzq-weekly-board')
    expect(link).toHaveAttribute('title', title)
  })
})
